"""SMTChecker (M1 Task 6): z3-encoded numeric defect checker, 5 numeric defect
classes, solver-budget degradation (M1-D7).

Constructed with the numeric `Constraint`s to check (routing structural vs.
numeric constraints to the right checker backend is `spine/dsl/compile.py`,
Task 7 — this module only implements the SMT backend itself). For each
constraint: `select(graph, constraint.scope or constraint.forall)` picks the
entities it applies to; for each entity, `constraint.assert_` — already parsed
into a typed `AssertNode` by `spine.dsl.ast.parse_assert` — is compiled into a
z3 expression with the entity's own numeric `attrs` bound as concrete
literals (a `Field("reward_gold")` resolves against `entity.attrs["reward_gold"]`;
a dotted `Field("reward.gold")` walks nested dicts). `Not(assert_expr)` is
asserted in a fresh `z3.Solver`; `sat` means a violation exists (the entity's
own concrete values ARE the violating assignment); `unsat` means the
constraint is satisfied — silently (oracle-FP=0: a satisfied constraint must
yield ZERO findings). `prob_sum` in particular accumulates entry
probabilities as exact `fractions.Fraction`s and hands z3 an exact rational
(`z3.Q`) — NOT a Python-float sum — because float accumulation is precisely
what produces oracle-FP=0 violations (e.g. `[0.7, 0.2, 0.1]` float-sums to
`0.9999999999999999`, not `1`).

Defect-class derivation (deterministic, from the assert's shape — no LLM
judgement anywhere in this module): the parsed `AssertNode` tree is walked for
whitelisted numeric-aggregate `Call`s; whichever of `prob_sum` / `monotonic` /
`in_range` / `gacha_expectation` occurs (first, preorder) selects the defect
class; a plain numeric comparison with none of these calls in it defaults to
`reward_out_of_range`:

  1. reward_out_of_range         — plain numeric Compare, e.g. `reward_gold <= 80`.
  2. prob_sum_ne_1                — `prob_sum(entries)` (sum of a list-attr's
                                     `probability`/`weight` field) compared to 1.
  3. non_monotonic_curve          — `monotonic(points)`: a numeric list/points
                                     attr is not non-decreasing.
  4. interval_violation           — `in_range(x, lo, hi)`: `lo <= x <= hi`.
  5. gacha_expectation_violation  — `gacha_expectation(base_rate, pity_threshold)`:
                                     closed-form E[min(X, N)] for X ~ Geometric(p)
                                     (attempts-to-first-success capped by a hard
                                     pity at N) = (1 - (1-p)**N) / p, compared
                                     against a designer-set expectation budget.

Budget (M1-D7): `z3.Solver(); s.set("timeout", 5000)` (default; overridable via
`timeout_ms` for callers/tests that need a tighter budget). If
`s.check() == z3.unknown` — whether from genuinely running out of the wall-clock
budget, or because the fragment is nonlinear/undecidable for z3's incomplete
decision procedures (e.g. nonlinear integer arithmetic) — the Finding gets
`status="unproven"`, NEVER treated as a pass. The same degradation path covers
a field that can't be bound/compiled at all (`SmtCompileError`, e.g. a
missing/non-numeric attr, or an assert that doesn't compile to a boolean
predicate): fail closed, never silently dropped.

A "ranged" numeric field — `attrs[path] == {"min": lo, "max": hi}` instead of a
plain scalar — binds to a *free* z3 constant (Int if both bounds are `int`,
else Real) constrained by `lo <= v <= hi`, rather than one concrete literal.
This is a generically useful modelling feature for a numeric attribute that is
only known to fall in a range (e.g. a `damage: {min: 10, max: 20}` roll table),
and it is also what actually exercises the M1-D7 degrade-to-unproven path with
real (non-mocked) z3 hardness: nonlinear *integer* arithmetic over free bounded
variables is incomplete for z3's `nlsat`/NIA procedures (Diophantine
equations are, in general, undecidable — Hilbert's 10th problem), so e.g. a
Fermat-cubes-shaped constraint over three ranged int fields reliably returns
`unknown` well within a small timeout budget, never `sat`/`unsat`.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _dc_field
from fractions import Fraction
from typing import Any, Callable

import z3

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import Entity
from gameforge.spine.dsl.ast import (
    AssertNode,
    BinOp,
    BoolOp,
    Call,
    Compare,
    Const,
    DslError,
    Field,
    UnaryOp,
    parse_assert,
    select,
)
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider

_DEFAULT_TIMEOUT_MS = 5000

_CALL_DEFECT_CLASS = {
    "prob_sum": "prob_sum_ne_1",
    "monotonic": "non_monotonic_curve",
    "in_range": "interval_violation",
    "gacha_expectation": "gacha_expectation_violation",
}

_UNSUPPORTED_CALLS = frozenset(
    {"exists", "forall", "reachable_in", "count", "semantically_reveals_identity"}
)
"""Whitelisted-by-`spine.dsl.ast` calls that are NOT SMT's domain (structural
predicates -> graph/asp; the llm-assisted placeholder -> M2 agent layer)."""


class SmtCompileError(Exception):
    """Raised when an assert-expression cannot be bound/compiled for a
    specific entity — e.g. a referenced field is absent or non-numeric, or the
    assert doesn't compile to a boolean predicate. Always caught by
    `SMTChecker._check_entity` and degraded to a `status="unproven"` Finding:
    fail closed, never silently dropped and never treated as satisfied.
    """


# --------------------------------------------------------------------------
# Binding context: per-(constraint, entity) state threaded through `_compile`.
# --------------------------------------------------------------------------


@dataclass
class _BindCtx:
    entity: Entity
    extra: list = _dc_field(default_factory=list)  # z3 BoolRef range constraints
    assignment: dict[str, Any] = _dc_field(default_factory=dict)  # evidence
    _vars: dict[str, Any] = _dc_field(default_factory=dict)  # path -> free z3 var


def _resolve_path(attrs: dict, path: str) -> Any:
    cur: Any = attrs
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise SmtCompileError(f"field not found on entity: {path!r}")
        cur = cur[part]
    return cur


def _numeral(value: Any):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SmtCompileError(f"non-numeric value: {value!r}")
    return z3.IntVal(value) if isinstance(value, int) else z3.RealVal(value)


def _z3_to_python(val: Any) -> Any:
    if z3.is_int_value(val):
        return val.as_long()
    if z3.is_rational_value(val):
        return val.numerator_as_long() / val.denominator_as_long()
    try:
        return float(val.as_decimal(10).rstrip("?"))
    except Exception:  # pragma: no cover - defensive fallback for exotic sorts
        return str(val)


def _bind_field(path: str, ctx: _BindCtx):
    """Resolve `path` against `ctx.entity.attrs` and return a z3 numeric term.

    A plain scalar (`int`/`float`) binds to a concrete literal. A "ranged"
    value (`{"min": lo, "max": hi}`) binds to a fresh free z3 constant bounded
    by `lo <= v <= hi` (registered in `ctx.extra`) instead — see module
    docstring.
    """
    if path in ctx._vars:
        return ctx._vars[path]
    raw = _resolve_path(ctx.entity.attrs, path)
    if isinstance(raw, dict) and "min" in raw and "max" in raw:
        lo, hi = raw["min"], raw["max"]
        is_int = isinstance(lo, int) and isinstance(hi, int)
        var = z3.Int(f"{ctx.entity.id}::{path}") if is_int else z3.Real(f"{ctx.entity.id}::{path}")
        ctx.extra.append(var >= _numeral(lo))
        ctx.extra.append(var <= _numeral(hi))
        ctx._vars[path] = var
        ctx.assignment[path] = {"min": lo, "max": hi}
        return var
    value = _numeral(raw)
    ctx.assignment[path] = raw
    return value


def _concrete_number(node: AssertNode, ctx: _BindCtx) -> int | float:
    """Evaluate a `Const`/`Field` node to a concrete Python number (not a z3
    term) — used where a closed-form Python computation needs an actual
    number, not a possibly-free z3 variable (e.g. `gacha_expectation`'s
    exponent). Raises `SmtCompileError` if the node isn't a concrete numeric
    leaf (a ranged/free field can't be concretely evaluated this way).
    """
    if isinstance(node, Const):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise SmtCompileError(f"non-numeric constant: {node.value!r}")
        return node.value
    if isinstance(node, Field):
        raw = _resolve_path(ctx.entity.attrs, node.path)
        if isinstance(raw, dict):
            raise SmtCompileError(
                f"expected a concrete numeric field, got a range: {node.path!r}"
            )
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise SmtCompileError(f"non-numeric field value: {raw!r}")
        ctx.assignment[node.path] = raw
        return raw
    raise SmtCompileError(f"expected a field or constant, got: {type(node).__name__}")


# --------------------------------------------------------------------------
# Whitelisted-call handlers.
# --------------------------------------------------------------------------


def _call_prob_sum(args: tuple[AssertNode, ...], ctx: _BindCtx):
    if len(args) != 1 or not isinstance(args[0], Field):
        raise SmtCompileError("prob_sum(entries) takes exactly one field argument")
    path = args[0].path
    entries = _resolve_path(ctx.entity.attrs, path)
    if not isinstance(entries, list):
        raise SmtCompileError(f"prob_sum field {path!r} is not a list")
    # Exact-rational accumulation (oracle-FP=0 anchor): a Python-float `total
    # += v` loses precision (e.g. [0.7, 0.2, 0.1] float-sums to
    # 0.9999999999999999, not 1), which would make a legitimately-correct
    # table spuriously SAT under `Not(prob_sum(entries) == 1)` -- a
    # false-positive Finding. `Fraction(str(v))` reads the *decimal* literal
    # exactly (0.7 -> 7/10), never `Fraction(v)` which would reproduce the
    # binary-float value verbatim.
    total = Fraction(0)
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SmtCompileError(f"prob_sum entry {i} is not an object")
        if "probability" in entry:
            v = entry["probability"]
        elif "weight" in entry:
            v = entry["weight"]
        else:
            raise SmtCompileError(f"prob_sum entry {i} has no probability/weight key")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SmtCompileError(f"prob_sum entry {i} value is not numeric: {v!r}")
        total += Fraction(str(v))
    ctx.assignment[path] = entries
    return z3.Q(total.numerator, total.denominator)


def _call_monotonic(args: tuple[AssertNode, ...], ctx: _BindCtx):
    if len(args) != 1 or not isinstance(args[0], Field):
        raise SmtCompileError("monotonic(points) takes exactly one field argument")
    path = args[0].path
    points = _resolve_path(ctx.entity.attrs, path)
    if not isinstance(points, list) or not points:
        raise SmtCompileError(f"monotonic field {path!r} is not a non-empty list")
    nums: list[float] = []
    for i, v in enumerate(points):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SmtCompileError(f"monotonic point {i} is not numeric: {v!r}")
        nums.append(v)
    ctx.assignment[path] = nums
    non_decreasing = all(nums[i] <= nums[i + 1] for i in range(len(nums) - 1))
    return z3.BoolVal(non_decreasing)


def _call_in_range(args: tuple[AssertNode, ...], ctx: _BindCtx):
    if len(args) != 3:
        raise SmtCompileError("in_range(x, lo, hi) takes exactly three arguments")
    x, lo, hi = (_compile(a, ctx) for a in args)
    return z3.And(lo <= x, x <= hi)


def _call_gacha_expectation(args: tuple[AssertNode, ...], ctx: _BindCtx):
    if len(args) != 2:
        raise SmtCompileError(
            "gacha_expectation(base_rate, pity_threshold) takes exactly two arguments"
        )
    p = _concrete_number(args[0], ctx)
    n = _concrete_number(args[1], ctx)
    if not (isinstance(p, (int, float)) and 0 < p <= 1):
        raise SmtCompileError(f"gacha_expectation: base_rate must be in (0, 1], got {p!r}")
    if not (isinstance(n, int) and n >= 1):
        raise SmtCompileError(
            f"gacha_expectation: pity_threshold must be a positive int, got {n!r}"
        )
    # E[min(X, N)] for X ~ Geometric(p) (attempts to first success):
    # sum_{k=0}^{N-1} P(X > k) = sum_{k=0}^{N-1} (1-p)**k = (1 - (1-p)**N) / p.
    expected = (1 - (1 - p) ** n) / p
    return _numeral(expected)


def _call_aggregate(args: tuple[AssertNode, ...], ctx: _BindCtx, reducer: Callable):
    """Generic `sum`/`max`/`min`: a single field-of-numbers, or a variadic
    list of field/const terms — whichever shape the call was written in."""
    if len(args) == 1 and isinstance(args[0], Field):
        raw = _resolve_path(ctx.entity.attrs, args[0].path)
        if isinstance(raw, list):
            nums = []
            for v in raw:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise SmtCompileError(f"aggregate list entry is not numeric: {v!r}")
                nums.append(v)
            ctx.assignment[args[0].path] = raw
            if not nums:
                raise SmtCompileError("aggregate over an empty list")
            return _numeral(reducer(nums))
    terms = [_compile(a, ctx) for a in args]
    if not terms:
        raise SmtCompileError("aggregate call requires at least one argument")
    if reducer is sum:
        out = terms[0]
        for t in terms[1:]:
            out = out + t
        return out
    # max/min over z3 terms: fold pairwise via z3.If (works for free vars too).
    is_max = reducer is max
    out = terms[0]
    for t in terms[1:]:
        out = z3.If(out >= t, out, t) if is_max else z3.If(out <= t, out, t)
    return out


_CALL_HANDLERS: dict[str, Callable[[tuple[AssertNode, ...], _BindCtx], Any]] = {
    "prob_sum": _call_prob_sum,
    "monotonic": _call_monotonic,
    "in_range": _call_in_range,
    "gacha_expectation": _call_gacha_expectation,
    "sum": lambda args, ctx: _call_aggregate(args, ctx, sum),
    "max": lambda args, ctx: _call_aggregate(args, ctx, max),
    "min": lambda args, ctx: _call_aggregate(args, ctx, min),
}


# --------------------------------------------------------------------------
# AssertNode -> z3 expression compiler.
# --------------------------------------------------------------------------

_COMPARE_BUILDERS: dict[str, Callable[[Any, Any], Any]] = {
    "==": lambda lhs, rhs: lhs == rhs,
    "!=": lambda lhs, rhs: lhs != rhs,
    "<": lambda lhs, rhs: lhs < rhs,
    "<=": lambda lhs, rhs: lhs <= rhs,
    ">": lambda lhs, rhs: lhs > rhs,
    ">=": lambda lhs, rhs: lhs >= rhs,
}

_BINOP_BUILDERS: dict[str, Callable[[Any, Any], Any]] = {
    "+": lambda lhs, rhs: lhs + rhs,
    "-": lambda lhs, rhs: lhs - rhs,
    "*": lambda lhs, rhs: lhs * rhs,
    "/": lambda lhs, rhs: z3.ToReal(lhs) / z3.ToReal(rhs)
    if z3.is_int(lhs) or z3.is_int(rhs) else lhs / rhs,
    "//": lambda lhs, rhs: z3.ToInt(z3.ToReal(lhs) / z3.ToReal(rhs)),
    "%": lambda lhs, rhs: lhs % rhs,
}


def _compile(node: AssertNode, ctx: _BindCtx):
    if isinstance(node, Const):
        return _numeral(node.value)
    if isinstance(node, Field):
        return _bind_field(node.path, ctx)
    if isinstance(node, UnaryOp):
        operand = _compile(node.operand, ctx)
        if node.op == "not":
            if not z3.is_bool(operand):
                raise SmtCompileError("`not` requires a boolean operand")
            return z3.Not(operand)
        if not z3.is_arith(operand):
            raise SmtCompileError("unary `-` requires a numeric operand")
        return -operand
    if isinstance(node, BinOp):
        left, right = _compile(node.left, ctx), _compile(node.right, ctx)
        if node.op == "%" and (not z3.is_int(left) or not z3.is_int(right)):
            raise SmtCompileError("`%` requires integer operands")
        if not z3.is_arith(left) or not z3.is_arith(right):
            raise SmtCompileError(f"arithmetic operator {node.op!r} requires numeric operands")
        return _BINOP_BUILDERS[node.op](left, right)
    if isinstance(node, Compare):
        left, right = _compile(node.left, ctx), _compile(node.right, ctx)
        return _COMPARE_BUILDERS[node.op](left, right)
    if isinstance(node, BoolOp):
        values = [_compile(v, ctx) for v in node.values]
        for v in values:
            if not z3.is_bool(v):
                raise SmtCompileError(f"`{node.op}` requires boolean operands")
        return z3.And(*values) if node.op == "and" else z3.Or(*values)
    if isinstance(node, Call):
        if node.func in _UNSUPPORTED_CALLS:
            raise SmtCompileError(f"{node.func}() is not evaluated by SMTChecker")
        handler = _CALL_HANDLERS.get(node.func)
        if handler is None:  # pragma: no cover - exhaustive over WHITELISTED_CALLS
            raise SmtCompileError(f"unsupported call: {node.func}()")
        return handler(node.args, ctx)
    raise SmtCompileError(f"unsupported assert-expression node: {type(node).__name__}")  # pragma: no cover


def _derive_defect_class(node: AssertNode) -> str:
    """Walk `node` preorder for the first whitelisted numeric-aggregate call
    that has a dedicated defect class; a plain comparison (no such call)
    defaults to `reward_out_of_range`."""
    for func in _walk_call_funcs(node):
        defect_class = _CALL_DEFECT_CLASS.get(func)
        if defect_class is not None:
            return defect_class
    return "reward_out_of_range"


def _walk_call_funcs(node: AssertNode) -> list[str]:
    out: list[str] = []

    def walk(n: AssertNode) -> None:
        if isinstance(n, Call):
            out.append(n.func)
            for a in n.args:
                walk(a)
        elif isinstance(n, Compare):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, BinOp):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, BoolOp):
            for v in n.values:
                walk(v)
        elif isinstance(n, UnaryOp):
            walk(n.operand)
        # Const / Field: no nested calls.

    walk(node)
    return out


# --------------------------------------------------------------------------
# SMTChecker.
# --------------------------------------------------------------------------


class SMTChecker:
    id = "smt"

    def __init__(self, constraints: list[Constraint], timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> None:
        self.constraints = constraints
        self.timeout_ms = timeout_ms

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]:
        g = snapshot.to_graph()
        run_id = f"smt@{snapshot.snapshot_id[:23]}"
        findings: list[Finding] = []
        counter = [0]

        def emit(
            constraint: Constraint,
            defect_class: str,
            entity_ids: list[str],
            evidence: dict[str, Any],
            message: str,
            status: str = "confirmed",
        ) -> None:
            f = Finding(
                id=f"{run_id}#{counter[0]}", source="checker", producer_id=self.id,
                producer_run_id=run_id, oracle_type="deterministic",
                defect_class=defect_class, severity=constraint.severity,
                snapshot_id=snapshot.snapshot_id, entities=entity_ids,
                constraint_id=constraint.id, evidence=evidence, status=status,
                message=message,
            )
            counter[0] += 1
            findings.append(f)

        for constraint in self.constraints:
            selector = constraint.scope or constraint.forall
            if selector is None:
                continue  # nothing to bind against: no verdict possible
            try:
                node = parse_assert(constraint.assert_)
            except DslError as exc:
                emit(
                    constraint, "reward_out_of_range", [], {"reason": str(exc)},
                    f"could not parse assert-expression for {constraint.id!r}: {exc}",
                    status="unproven",
                )
                continue
            defect_class = _derive_defect_class(node)
            try:
                entities = select(g, selector)
            except DslError as exc:
                emit(
                    constraint, defect_class, [], {"reason": str(exc)},
                    f"could not select entities for {constraint.id!r}: {exc}",
                    status="unproven",
                )
                continue
            for entity in entities:
                self._check_entity(constraint, node, defect_class, entity, emit)
        return findings

    def _check_entity(self, constraint, node, defect_class, entity, emit) -> None:
        ctx = _BindCtx(entity=entity)
        try:
            expr = _compile(node, ctx)
            if not z3.is_bool(expr):
                raise SmtCompileError("assert-expression did not compile to a boolean predicate")
        except SmtCompileError as exc:
            emit(
                constraint, defect_class, [entity.id], {"reason": str(exc)},
                f"SMTChecker could not bind/compile constraint {constraint.id!r} "
                f"for entity {entity.id!r}: {exc}",
                status="unproven",
            )
            return

        solver = z3.Solver()
        solver.set("timeout", self.timeout_ms)
        for extra in ctx.extra:
            solver.add(extra)
        solver.add(z3.Not(expr))
        result = solver.check()

        if result == z3.unknown:
            emit(
                constraint, defect_class, [entity.id],
                {"reason": "solver_could_not_decide", "budget_ms": self.timeout_ms,
                 "bound_fields": ctx.assignment},
                f"SMTChecker could not decide constraint {constraint.id!r} for "
                f"entity {entity.id!r} within budget ({self.timeout_ms}ms) — "
                f"degraded to unproven, never treated as pass",
                status="unproven",
            )
            return

        if result == z3.sat:
            violating = dict(ctx.assignment)
            model = solver.model()
            for path, var in ctx._vars.items():
                violating[path] = _z3_to_python(model.eval(var, model_completion=True))
            emit(
                constraint, defect_class, [entity.id],
                {"violating_assignment": violating, "assert": constraint.assert_},
                f"Entity {entity.id!r} violates constraint {constraint.id!r}: "
                f"{constraint.assert_}",
            )
            return

        # unsat: the constraint is satisfied for this entity -> silent
        # (oracle-FP=0 anchor — a satisfied constraint yields ZERO findings).
