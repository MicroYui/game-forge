"""Constraint -> Checker compiler (M1 Task 7): `compile(constraint) -> Checker`.

This module ONLY routes: it never re-implements a defect-detection algorithm
and never runs a solver budget itself (that's the backends' job — ASPChecker's
grounding-budget/wall-clock, SMTChecker's z3 timeout; see Task 5/6). `compile`
picks *which* backend decides a given `Constraint` and binds the two together:

  1. `constraint.has_llm_predicate()` -> `LlmRoutedChecker`. M1 defines the
     routing interface but never evaluates an llm-assisted predicate
     deterministically (不简化只延后: the *interface* is complete now, the
     agent-layer *evaluation* is M2). `check()` always returns exactly one
     placeholder Finding — `oracle_type="llm-assisted"`, `status="unproven"`,
     `source="llm"` — which can never land in `ReviewReport.deterministic_findings`
     (contract §6 strict-partition invariant).
  2. `kind == "numeric"` -> `SMTChecker([constraint])` (z3). The constraint's
     `assert_` mini-expression IS parsed here (via `parse_assert`, inside
     SMTChecker) — numeric constraints are exactly what `parse_assert` /
     the z3 compiler in `spine/checkers/smt.py` are for.
  3. `kind == "structural"` -> a structural backend. Structural `assert_` text
     (e.g. `"acyclic(quest_steps)"`) is NEVER run through `parse_assert` —
     it isn't the same mini-expression grammar the SMT compiler consumes, it's
     free-form predicate-naming prose. Instead the raw text is inspected for
     keywords naming one of ASPChecker's two shared-with-Graph defect classes
     (`_classify_structural`): a cycle/acyclic predicate routes to
     `ASPChecker` filtered to `cyclic_dependency`; a source/reachable/collect
     predicate routes to `ASPChecker` filtered to `missing_drop_source`;
     anything else routes to `GraphChecker` (covers all 7 structural defect
     classes) with no filter — every Finding it produces passes through.
  4. `kind == "narrative"` without an llm-assisted predicate -> routed exactly
     like structural (same `_classify_structural` + ASP/Graph choice) — a
     narrative constraint that turns out to be fully deterministic (no
     predicate opted into `oracle="llm-assisted"`) has nothing narrative left
     to distinguish it from a structural one at the routing layer.

Every Finding returned by a compiled checker's `check()` — including ones
SMTChecker/ASPChecker/GraphChecker already stamped `constraint_id` on, or
ones the underlying backend leaves unset — is re-stamped with
`constraint_id = constraint.id` by `CompiledChecker`, so the caller can always
trust that field regardless of what the wrapped backend did on its own.
"""

from __future__ import annotations

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import Finding
from gameforge.spine.checkers.asp import ASPChecker
from gameforge.spine.checkers.base import Checker
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.checkers.smt import SMTChecker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider

_CYCLE_KEYWORDS = ("cycle", "cyclic", "acyclic")
_SOURCE_KEYWORDS = ("source", "reachable", "collect")


class CompiledChecker:
    """Wraps a deterministic backend `Checker` bound to one `Constraint`.

    `check()` runs the backend, optionally filters its Findings down to a
    single `defect_class` (used for the ASP-routed shared defect classes —
    see `_classify_structural`), and stamps `constraint_id` on every Finding
    that survives.
    """

    id: str

    def __init__(
        self,
        backend: Checker,
        constraint: Constraint,
        defect_class_filter: str | None = None,
    ) -> None:
        self.backend = backend
        self.constraint = constraint
        self.defect_class_filter = defect_class_filter
        self.id = f"compiled:{backend.id}:{constraint.id}"

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]:
        findings = self.backend.check(snapshot, nav=nav)
        if self.defect_class_filter is not None:
            findings = [f for f in findings if f.defect_class == self.defect_class_filter]
        return [
            f if f.constraint_id == self.constraint.id
            else f.model_copy(update={"constraint_id": self.constraint.id})
            for f in findings
        ]


class LlmRoutedChecker:
    """M1's routing target for any constraint with an llm-assisted predicate.

    Never deterministic: `check()` always returns exactly one Finding —
    `oracle_type="llm-assisted"`, `status="unproven"`, `source="llm"` — noting
    that evaluation is routed to the agent layer (M2). This is a placeholder
    by design (不简化只延后), not a stub standing in for missing work: the
    contract §6 partition invariant requires every llm-assisted Finding to be
    unambiguously excluded from `ReviewReport.deterministic_findings`, and this
    Finding's shape guarantees exactly that.
    """

    id = "llm-routed"

    def __init__(self, constraint: Constraint) -> None:
        self.constraint = constraint

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]:
        run_id = f"{self.id}@{snapshot.snapshot_id[:23]}"
        return [
            Finding(
                id=f"{run_id}#0",
                source="llm",
                producer_id=self.id,
                producer_run_id=run_id,
                oracle_type="llm-assisted",
                defect_class="llm_assisted_predicate",
                severity=self.constraint.severity,
                snapshot_id=snapshot.snapshot_id,
                constraint_id=self.constraint.id,
                status="unproven",
                message=(
                    f"constraint {self.constraint.id!r} has an llm-assisted "
                    f"predicate; routed to agent layer (M2) — not evaluated "
                    f"deterministically in M1"
                ),
            )
        ]


def _classify_structural(assert_expr: str) -> str | None:
    """Inspect (never `parse_assert`) a structural/narrative constraint's raw
    `assert_` text for keywords naming one of ASPChecker's two shared-with-
    GraphChecker defect classes. Returns `None` when neither pattern matches —
    such a constraint isn't specific to a single ASP-encodable defect class,
    so it routes to `GraphChecker` (all 7 structural defect classes)
    unfiltered instead.
    """
    lowered = assert_expr.lower()
    if any(kw in lowered for kw in _CYCLE_KEYWORDS):
        return "cyclic_dependency"
    if any(kw in lowered for kw in _SOURCE_KEYWORDS):
        return "missing_drop_source"
    return None


def _compile_structural(constraint: Constraint) -> Checker:
    defect_class = _classify_structural(constraint.assert_)
    backend: Checker = ASPChecker() if defect_class is not None else GraphChecker()
    return CompiledChecker(backend, constraint, defect_class_filter=defect_class)


def compile(constraint: Constraint) -> Checker:
    """Route `constraint` to the Checker backend that decides it (contract §3).

    See module docstring for the full routing table. Budgets/degradation to
    `status="unproven"` are entirely the backends' responsibility (Task 5/6,
    M1-D7) — this function only chooses which backend runs.
    """
    if constraint.has_llm_predicate():
        return LlmRoutedChecker(constraint)
    if constraint.kind == "numeric":
        return CompiledChecker(SMTChecker([constraint]), constraint)
    # kind in ("structural", "narrative") with no llm-assisted predicate.
    return _compile_structural(constraint)


def compile_all(constraints: list[Constraint]) -> list[Checker]:
    return [compile(c) for c in constraints]
