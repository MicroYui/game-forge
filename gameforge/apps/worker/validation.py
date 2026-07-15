"""Concrete composition for the three M4c validation handlers (Task 13).

The platform validation handlers are game-agnostic and drive their solver /
analysis work through injected ports; this module (the ``apps`` composition
boundary, which may import ``spine``) supplies the concrete implementations wired
by the worker composition root.

``Z3DifferentialEngine`` / ``ClingoDifferentialEngine`` are the two initial EXACT
differential engines the constraint validator cross-checks: each wraps a REAL
spine solver backend (``spine/checkers/smt.py`` z3 / ``spine/checkers/asp.py``
Clingo) and returns a deterministic per-candidate consistency verdict. The
constraint validator requires ALL engines to AGREE the candidate is consistent —
a numeric contradiction z3 derives but Clingo cannot see surfaces as a
disagreement (``failed``), and a budget-exhausted solver degrades to ``unproven``,
NEVER a pass. No LLM SDK is imported here.
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
    """Cross-check every numeric constraint's satisfiability with REAL z3.

    Each deterministic numeric constraint's ``assert`` predicate is compiled over
    FREE bounded z3 variables (one per referenced field) and checked for
    satisfiability: ``unsat`` is a genuine contradiction (``inconsistent``);
    ``unknown`` degrades the whole run to ``timed_out`` (never a pass); a predicate
    this engine cannot bind (aggregate/list calls that are Clingo/structural's or
    another engine's domain) is skipped rather than falsely rejected. A candidate
    with no z3-decidable numeric constraint is vacuously ``consistent``.
    """

    engine_id: str = "z3"
    engine_version: int = 1

    def evaluate(self, request: DifferentialEvalRequest) -> DifferentialEngineResultV1:
        for constraint in request.constraints:
            if constraint.kind != "numeric" or constraint.has_llm_predicate():
                continue
            verdict = self._check(constraint)
            if verdict == "timed_out":
                return DifferentialEngineResultV1(
                    status="timed_out", reason_code="z3_budget_exhausted"
                )
            if verdict == "inconsistent":
                return DifferentialEngineResultV1(status="executed", consistency="inconsistent")
        return DifferentialEngineResultV1(status="executed", consistency="consistent")

    def _check(self, constraint: Constraint) -> str:
        try:
            node = parse_assert(constraint.assert_)
        except Exception:  # noqa: BLE001 - unparseable numeric assert -> contradiction
            return "inconsistent"
        entity = Entity(
            id="differential-probe",
            type="ITEM",
            attrs={path: {"min": -_FREE_BOUND, "max": _FREE_BOUND} for path in _field_paths(node)},
        )
        ctx = _BindCtx(entity=entity)
        try:
            expr = _smt_compile(node, ctx)
            if not z3.is_bool(expr):
                return "skip"
        except SmtCompileError:
            # not a plain numeric predicate this engine binds (e.g. prob_sum over a
            # list attr) — outside z3's differential domain, not a rejection.
            return "skip"
        solver = z3.Solver()
        solver.set("timeout", _Z3_TIMEOUT_MS)
        for extra in ctx.extra:
            solver.add(extra)
        solver.add(expr)
        result = solver.check()
        if result == z3.unsat:
            return "inconsistent"
        if result == z3.unknown:
            return "timed_out"
        return "consistent"


@dataclass(frozen=True, slots=True)
class ClingoDifferentialEngine:
    """Cross-check every structural constraint grounds cleanly with REAL Clingo.

    Each deterministic structural/narrative constraint is routed through the spine
    DSL compiler to its ASP/graph backend and run against a canonical single-node
    probe snapshot; a backend that raises while grounding is an ``inconsistent``
    candidate, otherwise the structural candidate is ``consistent``. A candidate
    with no structural constraint is vacuously ``consistent``.
    """

    engine_id: str = "clingo"
    engine_version: int = 1

    def evaluate(self, request: DifferentialEvalRequest) -> DifferentialEngineResultV1:
        probe = Snapshot({}, {})
        for constraint in request.constraints:
            if constraint.kind == "numeric" or constraint.has_llm_predicate():
                continue
            try:
                checker = compile_constraint(constraint)
                checker.check(probe)
            except Exception:  # noqa: BLE001 - a structural backend that cannot ground
                return DifferentialEngineResultV1(status="executed", consistency="inconsistent")
        return DifferentialEngineResultV1(status="executed", consistency="consistent")


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
