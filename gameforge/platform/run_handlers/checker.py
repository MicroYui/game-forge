"""``checker_runner@1`` — the deterministic structural/numeric checker handler.

Thin adapter over ``gameforge.spine.checkers``: it loads the input IR snapshot,
selects the requested checkers by id (``graph`` / ``asp`` / ``smt``), runs each,
and seals the concatenated ``Finding``s into a single primary
``checker_run[checker-report@1]`` plus one ``PreparedFinding`` per finding under
the frozen ``checker-findings`` policy. ASP/SMT budget degradation stays
``status="unproven"`` (fail-closed) exactly as the spine checkers emit it — this
handler never re-classifies a verdict.

``outcome_code=checker_completed``; LLM execution mode is ``not_applicable``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import CheckerRunPayloadV1, PreparedRunOutcome
from gameforge.contracts.lineage import VersionTuple
from gameforge.spine.checkers.base import Checker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    FindingEvidence,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    store_prepared_artifact,
)
from gameforge.platform.run_handlers.readers import (
    ConstraintLoader,
    NavLoader,
    SnapshotLoader,
    load_constraints,
    load_nav,
    load_snapshot,
)

CHECKER_TOOL_VERSION = "checker@1"
CHECKER_REPORT_SCHEMA_ID = "checker-report@1"


class CheckerFactory(Protocol):
    """Build one spine ``Checker`` for a checker id, given resolved constraints."""

    def build(self, checker_id: str, *, constraints: list[Constraint]) -> Checker: ...


class DefaultCheckerFactory:
    """The production checker factory (``graph``/``asp``/``smt``).

    ``clingo`` (ASP) and ``z3`` (SMT) are imported lazily so a checker run that
    only selects ``graph`` never pays for the solver backends.
    """

    def build(self, checker_id: str, *, constraints: list[Constraint]) -> Checker:
        if checker_id == "graph":
            from gameforge.spine.checkers.graph import GraphChecker

            return GraphChecker()
        if checker_id == "asp":
            from gameforge.spine.checkers.asp import ASPChecker

            return ASPChecker()
        if checker_id == "smt":
            from gameforge.spine.checkers.smt import SMTChecker

            return SMTChecker(constraints)
        raise ValueError(f"unknown checker id {checker_id!r}")


@dataclass(frozen=True, slots=True)
class CheckerRunHandler:
    """A ``RunExecutor`` producing the primary checker report + findings."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    checker_factory: CheckerFactory = field(default_factory=DefaultCheckerFactory)
    snapshot_loader: SnapshotLoader = load_snapshot
    constraint_loader: ConstraintLoader = load_constraints
    nav_loader: NavLoader = load_nav

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, CheckerRunPayloadV1):
            raise TypeError("checker_runner@1 requires a checker-run@1 payload")

        snapshot = self.snapshot_loader(self.blobs, payload.snapshot_artifact_id)
        constraints = self._constraints(payload)
        nav = self.nav_loader(self.blobs, payload.snapshot_artifact_id)

        findings = self._run_checkers(payload, snapshot, constraints, nav)
        findings = _filter_defect_classes(findings, payload.defect_classes)

        lineage = _snapshot_lineage(payload)
        primary = store_prepared_artifact(
            self.store,
            kind="checker_run",
            payload_schema_id=CHECKER_REPORT_SCHEMA_ID,
            version_tuple=VersionTuple(
                ir_snapshot_id=snapshot.snapshot_id,
                constraint_snapshot_id=_constraint_snapshot_id(payload),
                tool_version=CHECKER_TOOL_VERSION,
            ),
            lineage=lineage,
            payload=_checker_report_payload(payload, snapshot, findings),
        )

        prepared_findings = build_prepared_findings(
            tuple(
                FindingEvidence(finding=finding, evidence_artifact_index=0) for finding in findings
            ),
            run_id=context.run.run_id,
        )
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code="checker_completed",
            primary_index=0,
            artifacts=(primary,),
            findings=prepared_findings,
        )

    def _constraints(self, payload: CheckerRunPayloadV1) -> list[Constraint]:
        if "smt" not in payload.checker_ids:
            return []
        if payload.constraint_snapshot_artifact_id is None:
            return []
        return self.constraint_loader(self.blobs, payload.constraint_snapshot_artifact_id)

    def _run_checkers(
        self,
        payload: CheckerRunPayloadV1,
        snapshot: Snapshot,
        constraints: list[Constraint],
        nav: NavProvider | None,
    ) -> list[Finding]:
        findings: list[Finding] = []
        for checker_id in payload.checker_ids:
            checker = self.checker_factory.build(checker_id, constraints=constraints)
            findings.extend(checker.check(snapshot, nav=nav))
        return findings


def _filter_defect_classes(
    findings: list[Finding], defect_classes: tuple[str, ...]
) -> list[Finding]:
    if not defect_classes:
        return findings
    allowed = set(defect_classes)
    return [finding for finding in findings if finding.defect_class in allowed]


def _snapshot_lineage(payload: CheckerRunPayloadV1) -> tuple[str, ...]:
    lineage = [payload.snapshot_artifact_id]
    if payload.constraint_snapshot_artifact_id is not None:
        lineage.append(payload.constraint_snapshot_artifact_id)
    return tuple(lineage)


def _constraint_snapshot_id(payload: CheckerRunPayloadV1) -> str | None:
    return payload.constraint_snapshot_artifact_id


def _checker_report_payload(
    payload: CheckerRunPayloadV1,
    snapshot: Snapshot,
    findings: list[Finding],
) -> dict[str, object]:
    return {
        "payload_schema_version": CHECKER_REPORT_SCHEMA_ID,
        "snapshot_id": snapshot.snapshot_id,
        "checker_ids": list(payload.checker_ids),
        "defect_classes": list(payload.defect_classes),
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


__all__ = [
    "CHECKER_REPORT_SCHEMA_ID",
    "CheckerFactory",
    "CheckerRunHandler",
    "DefaultCheckerFactory",
]
