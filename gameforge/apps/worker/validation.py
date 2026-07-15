"""Concrete composition for the three M4c validation handlers (Task 13).

The platform validation handlers are game-agnostic and drive their solver /
analysis work through injected ports; this module (the ``apps`` composition
boundary, which may import ``spine``) supplies the concrete implementations wired
by the worker composition root.

``Z3DifferentialEngine`` / ``ClingoDifferentialEngine`` are the two initial EXACT
differential engines the constraint validator cross-checks: each wraps a REAL
spine solver backend (``spine/checkers/smt.py`` z3 / ``spine/checkers/asp.py``
Clingo) and reports whether ITS domain applies to the candidate and, if so, a
deterministic consistency verdict. The two initial engines are DOMAIN-PARTITIONED
(z3 = numeric SMT, Clingo = structural ASP), so an engine whose domain does not
apply to the candidate reports ``not_applicable`` (recorded as an ``unproven``
differential stage — never a vacuous pass), and an in-domain constraint it cannot
decide reports ``undecided`` (also ``unproven``). Only a genuinely-evaluated
consistent domain is a ``passed`` stage; any ``unsat`` is a genuine contradiction
(``failed``). No LLM SDK is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass

import z3

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.ir import Entity
from gameforge.spine.checkers.smt import (
    SmtCompileError,
    _BindCtx,
    _compile as _smt_compile,
)
from gameforge.spine.dsl.ast import (
    AssertNode,
    BinOp,
    BoolOp,
    Call,
    Compare,
    Field,
    UnaryOp,
    parse_assert,
)
from gameforge.spine.dsl.compile import compile as compile_constraint
from gameforge.spine.ir.snapshot import Snapshot

from gameforge.platform.run_handlers.constraint_validation import (
    ConstraintDifferentialEngine,
    DifferentialEngineResultV1,
    DifferentialEvalRequest,
)

_Z3_TIMEOUT_MS = 5000
_FREE_BOUND = 10**12


def _field_paths(node: AssertNode) -> set[str]:
    """Collect every ``Field`` path referenced by a parsed assert-expression."""

    paths: set[str] = set()

    def walk(current: AssertNode) -> None:
        if isinstance(current, Field):
            paths.add(current.path)
        elif isinstance(current, Compare):
            walk(current.left)
            walk(current.right)
        elif isinstance(current, BinOp):
            walk(current.left)
            walk(current.right)
        elif isinstance(current, BoolOp):
            for value in current.values:
                walk(value)
        elif isinstance(current, UnaryOp):
            walk(current.operand)
        elif isinstance(current, Call):
            for arg in current.args:
                walk(arg)

    walk(node)
    return paths


@dataclass(frozen=True, slots=True)
class Z3DifferentialEngine:
    """Cross-check every NUMERIC constraint's satisfiability with REAL z3.

    z3's domain is the numeric constraints. When the candidate has NONE, this
    engine reports ``not_applicable`` (the handler records that as an ``unproven``
    differential stage, NEVER a vacuous pass). Otherwise each deterministic numeric
    ``assert`` predicate is compiled over FREE bounded z3 variables (one per
    referenced field) and checked for satisfiability: any ``unsat`` = a genuine
    contradiction (``evaluated`` / ``inconsistent``); any ``unknown`` OR any
    in-domain numeric predicate this probe cannot bind (an aggregate/list call) =
    ``undecided`` (the engine's domain applies but it could not decide — ``unproven``,
    never a pass). Only when EVERY numeric constraint is satisfiable does it report
    ``evaluated`` / ``consistent``.
    """

    engine_id: str = "z3"
    engine_version: int = 1

    def evaluate(self, request: DifferentialEvalRequest) -> DifferentialEngineResultV1:
        numeric = [
            constraint
            for constraint in request.constraints
            if constraint.kind == "numeric" and not constraint.has_llm_predicate()
        ]
        if not numeric:
            return DifferentialEngineResultV1(
                status="not_applicable", reason_code="engine_domain_not_applicable"
            )
        undecided_reason: str | None = None
        for constraint in numeric:
            verdict, reason = self._check(constraint)
            if verdict == "inconsistent":
                return DifferentialEngineResultV1(status="evaluated", consistency="inconsistent")
            if verdict == "undecided":
                undecided_reason = reason
        if undecided_reason is not None:
            return DifferentialEngineResultV1(status="undecided", reason_code=undecided_reason)
        return DifferentialEngineResultV1(status="evaluated", consistency="consistent")

    def _check(self, constraint: Constraint) -> tuple[str, str | None]:
        try:
            node = parse_assert(constraint.assert_)
        except Exception:  # noqa: BLE001 - an unparseable numeric assert is a contradiction
            return "inconsistent", None
        entity = Entity(
            id="differential-probe",
            type="ITEM",
            attrs={path: {"min": -_FREE_BOUND, "max": _FREE_BOUND} for path in _field_paths(node)},
        )
        ctx = _BindCtx(entity=entity)
        try:
            expr = _smt_compile(node, ctx)
            if not z3.is_bool(expr):
                # a numeric constraint IN z3's domain it cannot bind to a boolean
                # predicate -> undecided (never silently skipped-as-passed).
                return "undecided", "z3_non_boolean_predicate"
        except SmtCompileError:
            # an in-domain numeric predicate this free-var probe cannot bind (e.g.
            # prob_sum over a list attr) -> undecided, NEVER a pass.
            return "undecided", "z3_cannot_bind_predicate"
        solver = z3.Solver()
        solver.set("timeout", _Z3_TIMEOUT_MS)
        for extra in ctx.extra:
            solver.add(extra)
        solver.add(expr)
        result = solver.check()
        if result == z3.unsat:
            return "inconsistent", None
        if result == z3.unknown:
            return "undecided", "z3_budget_exhausted"
        return "consistent", None


@dataclass(frozen=True, slots=True)
class ClingoDifferentialEngine:
    """Cross-check every STRUCTURAL constraint grounds cleanly with REAL Clingo.

    Clingo's domain is the structural/narrative constraints. When the candidate has
    NONE, this engine reports ``not_applicable`` (the handler records an ``unproven``
    differential stage, NEVER a vacuous pass). Otherwise each constraint is routed
    through the spine DSL compiler to its ASP/graph backend and grounded against a
    canonical probe snapshot; a backend that raises while grounding is ``undecided``
    (the engine could not decide — ``unproven``), otherwise the structural candidate
    is ``evaluated`` / ``consistent``.
    """

    engine_id: str = "clingo"
    engine_version: int = 1

    def evaluate(self, request: DifferentialEvalRequest) -> DifferentialEngineResultV1:
        structural = [
            constraint
            for constraint in request.constraints
            if not constraint.has_llm_predicate() and constraint.kind in ("structural", "narrative")
        ]
        if not structural:
            return DifferentialEngineResultV1(
                status="not_applicable", reason_code="engine_domain_not_applicable"
            )
        probe = Snapshot({}, {})
        for constraint in structural:
            try:
                compile_constraint(constraint).check(probe)
            except Exception:  # noqa: BLE001 - a structural backend that could not ground
                return DifferentialEngineResultV1(
                    status="undecided", reason_code="clingo_grounding_error"
                )
        return DifferentialEngineResultV1(status="evaluated", consistency="consistent")


def build_differential_engines() -> dict[str, ConstraintDifferentialEngine]:
    """The default ``engine_id -> engine`` map for ``constraint_validator@1``."""

    engines: tuple[ConstraintDifferentialEngine, ...] = (
        Z3DifferentialEngine(),
        ClingoDifferentialEngine(),
    )
    return {engine.engine_id: engine for engine in engines}


__all__ = [
    "ClingoDifferentialEngine",
    "Z3DifferentialEngine",
    "build_differential_engines",
]
