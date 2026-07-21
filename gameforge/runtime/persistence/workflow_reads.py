"""Narrow bounded SQLite source reads for M4c workflow projections."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from gameforge.contracts.api import RunFindingLinkViewV1
from gameforge.contracts.diff import ConflictSet, MergeConflict
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import FindingRevisionV1, finding_revision_digest
from gameforge.contracts.jobs import RunCommandRecordV1, RunFindingLinkV1, RunRecord, RunStatus
from gameforge.contracts.workflow import ApprovalItem
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.conflicts import SqlConflictSetRepository
from gameforge.runtime.persistence.findings import SqlFindingRepository
from gameforge.runtime.persistence.models import (
    ApprovalItemRow,
    FindingHeadRow,
    RunCommandRow,
    RunFindingLinkRow,
    RunRow,
)
from gameforge.runtime.persistence.runs import SqlRunRepository


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_items must be positive")
    return value


class SqlWorkflowReadRepository:
    """Read existing authorities without adding a generic CRUD or owning commits."""

    def __init__(
        self,
        session: Session,
        *,
        approvals: SqlApprovalRepository,
        runs: SqlRunRepository,
        findings: SqlFindingRepository,
        conflicts: SqlConflictSetRepository,
    ) -> None:
        for name, repository in (
            ("approvals", approvals),
            ("runs", runs),
            ("findings", findings),
            ("conflicts", conflicts),
        ):
            if getattr(repository, "_session", None) is not session:
                raise ValueError(f"{name} read repository must share the workflow read Session")
        self._session = session
        self._approvals = approvals
        self._runs = runs
        self._findings = findings
        self._conflicts = conflicts

    def get_approval(self, approval_id: str) -> ApprovalItem | None:
        return self._approvals.get(approval_id)

    def list_approvals(self, *, max_items: int) -> Sequence[ApprovalItem]:
        identifiers = self._session.scalars(
            select(ApprovalItemRow.approval_id)
            .order_by(ApprovalItemRow.created_at, ApprovalItemRow.approval_id)
            .limit(_limit(max_items))
        ).all()
        return tuple(
            self._required(self._approvals.get(identifier), "ApprovalItem", identifier)
            for identifier in identifiers
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def list_runs(
        self,
        *,
        status: RunStatus | None,
        max_items: int,
    ) -> Sequence[RunRecord]:
        statement = select(RunRow.run_id)
        if status is not None:
            statement = statement.where(RunRow.status == status)
        identifiers = self._session.scalars(
            statement.order_by(RunRow.created_at, RunRow.run_id).limit(_limit(max_items))
        ).all()
        return tuple(
            self._required(self._runs.get(identifier), "Run", identifier)
            for identifier in identifiers
        )

    def get_finding(
        self,
        finding_id: str,
        revision: int | None = None,
    ) -> FindingRevisionV1 | None:
        return (
            self._findings.current(finding_id)
            if revision is None
            else self._findings.get(finding_id, revision)
        )

    def list_findings(self, *, max_items: int) -> Sequence[FindingRevisionV1]:
        identifiers = self._session.scalars(
            select(FindingHeadRow.finding_id)
            .order_by(FindingHeadRow.finding_id)
            .limit(_limit(max_items))
        ).all()
        return tuple(
            self._required(self._findings.current(identifier), "Finding", identifier)
            for identifier in identifiers
        )

    def list_run_findings(
        self,
        run_id: str,
        *,
        max_items: int,
    ) -> Sequence[FindingRevisionV1]:
        return tuple(
            item.finding for item in self.list_run_finding_links(run_id, max_items=max_items)
        )

    def list_run_finding_links(
        self,
        run_id: str,
        *,
        max_items: int,
    ) -> Sequence[RunFindingLinkViewV1]:
        rows = self._session.execute(
            select(
                RunFindingLinkRow.attempt_no,
                RunFindingLinkRow.ordinal,
            )
            .where(RunFindingLinkRow.run_id == run_id)
            .order_by(RunFindingLinkRow.attempt_no, RunFindingLinkRow.ordinal)
            .limit(_limit(max_items))
        ).all()
        result: list[RunFindingLinkViewV1] = []
        for attempt_no, ordinal in rows:
            link = self._required(
                self._runs.get_finding_link(run_id, attempt_no, ordinal),
                "RunFindingLink",
                f"{run_id}:{attempt_no}:{ordinal}",
            )
            assert isinstance(link, RunFindingLinkV1)
            finding = self._required(
                self._findings.get(link.finding_id, link.finding_revision),
                "Finding revision",
                f"{link.finding_id}:{link.finding_revision}",
            )
            assert isinstance(finding, FindingRevisionV1)
            if finding_revision_digest(finding) != link.finding_digest:
                raise IntegrityViolation("Run Finding link digest differs from its exact revision")
            if finding.payload.producer_run_id != link.run_id:
                raise IntegrityViolation("Run Finding link differs from the Finding producer Run")
            result.append(
                RunFindingLinkViewV1(
                    run_id=link.run_id,
                    attempt_no=link.attempt_no,
                    ordinal=link.ordinal,
                    finding=finding,
                    finding_digest=link.finding_digest,
                    evidence_artifact_id=link.evidence_artifact_id,
                )
            )
        return tuple(result)

    def list_run_commands(
        self,
        run_id: str,
        *,
        max_items: int,
    ) -> Sequence[RunCommandRecordV1]:
        identifiers = self._session.scalars(
            select(RunCommandRow.command_id)
            .where(RunCommandRow.run_id == run_id)
            .order_by(RunCommandRow.created_at, RunCommandRow.command_id)
            .limit(_limit(max_items))
        ).all()
        return tuple(
            self._required(
                self._runs.get_command(run_id, identifier),
                "Run command",
                f"{run_id}:{identifier}",
            )
            for identifier in identifiers
        )

    def get_conflict_set(self, conflict_set_id: str) -> ConflictSet | None:
        return self._conflicts.get(conflict_set_id)

    def list_conflicts(
        self,
        conflict_set_id: str,
        *,
        max_items: int,
    ) -> Sequence[MergeConflict]:
        retained = self._conflicts.load_bounded(conflict_set_id)
        if retained is None:
            return ()
        return retained[2][: _limit(max_items)]

    @staticmethod
    def _required[T](value: T | None, label: str, identifier: str) -> T:
        if value is None:
            raise IntegrityViolation(
                f"bounded {label} index points to a missing authority",
                resource_id=identifier,
            )
        return value


__all__ = ["SqlWorkflowReadRepository"]
