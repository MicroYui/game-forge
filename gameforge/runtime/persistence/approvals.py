"""Transaction-bound SQLite persistence for approval workflow state."""

from __future__ import annotations

import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import typed_canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.workflow import ApprovalDecision, ApprovalItem, SubjectHead
from gameforge.runtime.persistence.models import (
    ApprovalDecisionRow,
    ApprovalItemRow,
    ArtifactRow,
    SubjectHeadRow,
)


_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ContractModel = TypeVar("_ContractModel", bound=BaseModel)
_SUBJECT_ARTIFACT_KIND = {
    "patch": "patch",
    "constraint_proposal": "constraint_proposal",
    "rollback_request": "rollback_request",
}
_IMMUTABLE_APPROVAL_FIELDS = (
    "approval_schema_version",
    "approval_id",
    "subject_series_id",
    "subject_revision",
    "subject_kind",
    "subject_artifact_id",
    "subject_digest",
    "supersedes_approval_id",
    "proposer",
    "domain_scope",
    "domain_registry_ref",
    "route_policy",
    "role_policy_version",
    "role_policy_digest",
    "approval_policy",
    "requirements",
    "created_at",
)
_DECISION_STATUSES = frozenset(
    {
        "pending_approval",
        "approved",
        "rejected",
        "changes_requested",
        "applied",
        "rolled_back",
        "superseded",
    }
)
_DECISION_REVISION_OFFSETS = {
    "pending_approval": 1,
    "approved": 1,
    "rejected": 1,
    "changes_requested": 1,
    "applied": 2,
    "rolled_back": 3,
    "superseded": 2,
}
_FINAL_DECISION_BY_STATUS = {
    "pending_approval": "approve",
    "approved": "approve",
    "rejected": "reject",
    "changes_requested": "request_changes",
    "applied": "approve",
    "rolled_back": "approve",
}


def _same_wire(left: BaseModel, right: BaseModel) -> bool:
    return typed_canonical_json(left.model_dump(mode="python")) == typed_canonical_json(
        right.model_dump(mode="python")
    )


def _revalidate_contract(
    value: _ContractModel,
    model_type: type[_ContractModel],
    *,
    label: str,
) -> _ContractModel:
    if type(value) is not model_type or set(value.__dict__) != set(model_type.model_fields):
        raise IntegrityViolation(f"{label} must be a canonical {model_type.__name__}")
    wire = value.model_dump(mode="python")
    try:
        parsed = model_type.model_validate(wire)
        parsed_wire = typed_canonical_json(parsed.model_dump(mode="python"))
        input_wire = typed_canonical_json(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"{label} wire is invalid") from exc
    if parsed_wire != input_wire:
        raise IntegrityViolation(f"{label} wire is not canonical")
    return parsed


def _require_nonempty(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityViolation(f"{field_name} must be a non-empty string")
    return value


def _require_revision(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise IntegrityViolation(f"{field_name} must be a positive integer")
    return value


def _decision_wire(row: ApprovalDecisionRow) -> dict[str, Any]:
    return {
        "decision_id": row.decision_id,
        "requirement_ids": row.requirement_ids,
        "decision": row.decision,
        "actor": row.actor,
        "expected_workflow_revision": row.expected_workflow_revision,
        "reason_code": row.reason_code,
        "comment": row.comment,
        "occurred_at": row.occurred_at,
    }


def _parse_decision_row(
    row: ApprovalDecisionRow,
    *,
    expected_decision_id: str,
    expected_approval_id: str | None = None,
) -> ApprovalDecision:
    wire = _decision_wire(row)
    try:
        if row.decision_id != expected_decision_id:
            raise ValueError("decision storage key differs from requested id")
        if expected_approval_id is not None and row.approval_id != expected_approval_id:
            raise ValueError("decision belongs to a different ApprovalItem")
        parsed = ApprovalDecision.model_validate(wire)
        if typed_canonical_json(parsed.model_dump(mode="python")) != typed_canonical_json(wire):
            raise ValueError("decision row is not canonical")
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored ApprovalDecision is invalid",
            decision_id=expected_decision_id,
        ) from exc
    return parsed


def _decision_values(approval_id: str, decision: ApprovalDecision) -> dict[str, Any]:
    values = decision.model_dump(mode="json")
    values["approval_id"] = approval_id
    return values


def _approval_wire(
    row: ApprovalItemRow,
    decisions: tuple[ApprovalDecision, ...],
) -> dict[str, Any]:
    return {
        "approval_schema_version": row.approval_schema_version,
        "approval_id": row.approval_id,
        "subject_series_id": row.subject_series_id,
        "subject_revision": row.subject_revision,
        "subject_kind": row.subject_kind,
        "subject_artifact_id": row.subject_artifact_id,
        "subject_digest": row.subject_digest,
        "status": row.status,
        "workflow_revision": row.workflow_revision,
        "supersedes_approval_id": row.supersedes_approval_id,
        "proposer": row.proposer,
        "domain_scope": row.domain_scope,
        "domain_registry_ref": row.domain_registry_ref,
        "route_policy": row.route_policy,
        "role_policy_version": row.role_policy_version,
        "role_policy_digest": row.role_policy_digest,
        "approval_policy": row.approval_policy,
        "requirements": row.requirements,
        "decisions": [item.model_dump(mode="json") for item in decisions],
        "active_validation_run_id": row.active_validation_run_id,
        "last_validation_failure_artifact_id": row.last_validation_failure_artifact_id,
        "evidence_set_artifact_id": row.evidence_set_artifact_id,
        "regression_evidence_artifact_ids": row.regression_evidence_artifact_ids,
        "target_binding": row.target_binding,
        "auto_apply_proof": row.auto_apply_proof,
        "created_at": row.created_at,
        "submitted_at": row.submitted_at,
        "decided_at": row.decided_at,
        "applied_at": row.applied_at,
    }


def _parse_approval_row(
    row: ApprovalItemRow,
    decisions: tuple[ApprovalDecision, ...],
    *,
    expected_approval_id: str,
) -> ApprovalItem:
    wire = _approval_wire(row, decisions)
    try:
        if row.approval_id != expected_approval_id:
            raise ValueError("approval storage key differs from requested id")
        parsed = ApprovalItem.model_validate(wire)
        if typed_canonical_json(parsed.model_dump(mode="python")) != typed_canonical_json(wire):
            raise ValueError("approval row is not canonical")
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored ApprovalItem is invalid",
            approval_id=expected_approval_id,
        ) from exc
    return parsed


def _approval_values(item: ApprovalItem) -> dict[str, Any]:
    values = item.model_dump(mode="json")
    del values["decisions"]
    return values


def _validate_decision_history(item: ApprovalItem) -> None:
    decisions = sorted(
        item.decisions,
        key=lambda decision: (decision.expected_workflow_revision, decision.decision_id),
    )
    if not decisions:
        if item.status in {"approved", "rejected", "changes_requested"}:
            raise IntegrityViolation(
                "stored ApprovalItem decision history is missing its terminal decision",
                approval_id=item.approval_id,
            )
        return
    if item.status not in _DECISION_STATUSES:
        raise IntegrityViolation(
            "stored ApprovalItem decision history is not valid for its status",
            approval_id=item.approval_id,
            status=item.status,
        )

    revisions = [decision.expected_workflow_revision for decision in decisions]
    if revisions != list(range(revisions[0], revisions[0] + len(revisions))):
        raise IntegrityViolation(
            "stored ApprovalItem decision history has a revision gap or duplicate",
            approval_id=item.approval_id,
        )
    if any(decision.decision != "approve" for decision in decisions[:-1]):
        raise IntegrityViolation(
            "stored ApprovalItem decision history continues after a terminal decision",
            approval_id=item.approval_id,
        )

    final = decisions[-1]
    expected_item_revision = (
        final.expected_workflow_revision + _DECISION_REVISION_OFFSETS[item.status]
    )
    if item.workflow_revision != expected_item_revision:
        raise IntegrityViolation(
            "stored ApprovalItem decision history is not absorbed by its workflow revision",
            approval_id=item.approval_id,
            expected_workflow_revision=expected_item_revision,
            actual_workflow_revision=item.workflow_revision,
        )
    expected_final_decision = _FINAL_DECISION_BY_STATUS.get(item.status)
    if expected_final_decision is not None and final.decision != expected_final_decision:
        raise IntegrityViolation(
            "stored ApprovalItem decision history does not match its terminal status",
            approval_id=item.approval_id,
            status=item.status,
        )


def _head_from_row(row: SubjectHeadRow, *, expected_series_id: str) -> SubjectHead:
    try:
        if row.subject_series_id != expected_series_id:
            raise ValueError("SubjectHead storage key differs from requested series")
        if (
            isinstance(row.current_subject_revision, bool)
            or not isinstance(row.current_subject_revision, int)
            or row.current_subject_revision < 1
        ):
            raise ValueError("stored current subject revision is invalid")
        if not isinstance(row.current_subject_digest, str) or not _LOWER_SHA256.fullmatch(
            row.current_subject_digest
        ):
            raise ValueError("stored current subject digest is invalid")
        return SubjectHead(
            subject_series_id=row.subject_series_id,
            current_subject_artifact_id=row.current_subject_artifact_id,
            current_approval_id=row.current_approval_id,
            revision=row.revision,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored SubjectHead is invalid",
            subject_series_id=expected_series_id,
        ) from exc


class SqlApprovalRepository:
    """Persist ApprovalItem state, decisions, and SubjectHead in an owned UoW."""

    def __init__(self, session: Session) -> None:
        if session.get_bind().dialect.name != "sqlite":
            raise ValueError("SqlApprovalRepository requires a SQLite session")
        self._session = session

    def insert_draft(self, item: ApprovalItem) -> ApprovalItem:
        candidate = _revalidate_contract(item, ApprovalItem, label="approval draft")
        if (
            candidate.status != "draft"
            or candidate.workflow_revision != 1
            or candidate.decisions
        ):
            raise IntegrityViolation(
                "approval draft must have status=draft, workflow_revision=1, and no decisions",
                approval_id=candidate.approval_id,
            )
        self._verify_subject_artifact(candidate)

        existing = self.get(candidate.approval_id)
        if existing is not None:
            if _same_wire(existing, candidate):
                return existing
            raise IntegrityViolation(
                "approval id is already bound to different immutable content",
                approval_id=candidate.approval_id,
            )
        collision = self._subject_revision_collision(candidate)
        if collision is not None:
            raise Conflict(
                "approval subject revision already exists",
                subject_series_id=candidate.subject_series_id,
                subject_revision=candidate.subject_revision,
                approval_id=collision,
            )

        try:
            result = self._session.execute(
                sqlite_insert(ApprovalItemRow)
                .values(**_approval_values(candidate))
                .on_conflict_do_nothing()
            )
        except IntegrityError as exc:
            raise IntegrityViolation(
                "approval draft violates persisted workflow references",
                approval_id=candidate.approval_id,
            ) from exc
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self.get(candidate.approval_id)
            if actual is not None:
                if _same_wire(actual, candidate):
                    return actual
                raise IntegrityViolation(
                    "approval id is already bound to different immutable content",
                    approval_id=candidate.approval_id,
                )
            collision = self._subject_revision_collision(candidate)
            if collision is not None:
                raise Conflict(
                    "approval subject revision already exists",
                    subject_series_id=candidate.subject_series_id,
                    subject_revision=candidate.subject_revision,
                    approval_id=collision,
                )
            raise IntegrityViolation(
                "approval draft insert did not publish a row",
                approval_id=candidate.approval_id,
            )
        return candidate

    def get(self, approval_id: str) -> ApprovalItem | None:
        identifier = _require_nonempty(approval_id, field_name="approval_id")
        row = self._session.get(ApprovalItemRow, identifier)
        if row is None:
            return None
        decision_rows = self._session.scalars(
            select(ApprovalDecisionRow)
            .where(ApprovalDecisionRow.approval_id == identifier)
            .order_by(ApprovalDecisionRow.decision_id)
        ).all()
        decisions = tuple(
            _parse_decision_row(
                decision_row,
                expected_decision_id=decision_row.decision_id,
                expected_approval_id=identifier,
            )
            for decision_row in decision_rows
        )
        item = _parse_approval_row(row, decisions, expected_approval_id=identifier)
        _validate_decision_history(item)
        self._verify_subject_artifact(item)
        return item

    def compare_and_set(
        self,
        approval_id: str,
        expected_workflow_revision: int,
        replacement: ApprovalItem,
    ) -> ApprovalItem:
        identifier = _require_nonempty(approval_id, field_name="approval_id")
        expected = _require_revision(
            expected_workflow_revision,
            field_name="expected_workflow_revision",
        )
        candidate = _revalidate_contract(
            replacement,
            ApprovalItem,
            label="approval replacement",
        )
        self._validate_replacement_identity(identifier, expected, candidate)
        _validate_decision_history(candidate)
        current = self.get(identifier)
        if current is None:
            raise Conflict("ApprovalItem does not exist", approval_id=identifier)
        if _same_wire(current, candidate):
            return current
        if current.workflow_revision != expected:
            raise Conflict(
                "ApprovalItem workflow revision did not match",
                approval_id=identifier,
                expected_workflow_revision=expected,
                actual_workflow_revision=current.workflow_revision,
            )
        self._verify_replacement(current, candidate, allow_new_decision=None)
        self._write_replacement(identifier, expected, candidate)
        return candidate

    def append_decision_and_compare_and_set(
        self,
        approval_id: str,
        expected_workflow_revision: int,
        decision: ApprovalDecision,
        replacement: ApprovalItem,
    ) -> ApprovalItem:
        identifier = _require_nonempty(approval_id, field_name="approval_id")
        expected = _require_revision(
            expected_workflow_revision,
            field_name="expected_workflow_revision",
        )
        decision_candidate = _revalidate_contract(
            decision,
            ApprovalDecision,
            label="approval decision",
        )
        candidate = _revalidate_contract(
            replacement,
            ApprovalItem,
            label="approval replacement",
        )
        self._validate_replacement_identity(identifier, expected, candidate)
        _validate_decision_history(candidate)
        if decision_candidate.expected_workflow_revision != expected:
            raise IntegrityViolation(
                "ApprovalDecision expected_workflow_revision differs from the CAS precondition",
                decision_id=decision_candidate.decision_id,
            )

        decision_row = self._session.get(ApprovalDecisionRow, decision_candidate.decision_id)
        existing_decision: ApprovalDecision | None = None
        if decision_row is not None:
            existing_decision = _parse_decision_row(
                decision_row,
                expected_decision_id=decision_candidate.decision_id,
            )
            if decision_row.approval_id != identifier or not _same_wire(
                existing_decision, decision_candidate
            ):
                raise Conflict(
                    "decision id is already bound to different content",
                    decision_id=decision_candidate.decision_id,
                )

        current = self.get(identifier)
        if current is None:
            raise Conflict("ApprovalItem does not exist", approval_id=identifier)
        if existing_decision is not None and _same_wire(current, candidate):
            return current
        if existing_decision is not None:
            raise IntegrityViolation(
                "stored decision exists without its workflow revision",
                approval_id=identifier,
                decision_id=decision_candidate.decision_id,
            )
        if current.workflow_revision != expected:
            raise Conflict(
                "ApprovalItem workflow revision did not match",
                approval_id=identifier,
                expected_workflow_revision=expected,
                actual_workflow_revision=current.workflow_revision,
            )
        self._verify_replacement(
            current,
            candidate,
            allow_new_decision=decision_candidate,
        )

        try:
            result = self._session.execute(
                sqlite_insert(ApprovalDecisionRow)
                .values(**_decision_values(identifier, decision_candidate))
                .on_conflict_do_nothing(index_elements=[ApprovalDecisionRow.decision_id])
            )
        except IntegrityError as exc:
            raise IntegrityViolation(
                "ApprovalDecision violates persisted workflow references",
                decision_id=decision_candidate.decision_id,
            ) from exc
        if result.rowcount != 1:
            self._session.expire_all()
            actual_row = self._session.get(
                ApprovalDecisionRow,
                decision_candidate.decision_id,
            )
            if actual_row is None:
                raise IntegrityViolation(
                    "ApprovalDecision insert did not publish a row",
                    decision_id=decision_candidate.decision_id,
                )
            actual = _parse_decision_row(
                actual_row,
                expected_decision_id=decision_candidate.decision_id,
            )
            if actual_row.approval_id != identifier or not _same_wire(
                actual, decision_candidate
            ):
                raise Conflict(
                    "decision id is already bound to different content",
                    decision_id=decision_candidate.decision_id,
                )

        self._write_replacement(identifier, expected, candidate)
        return candidate

    def get_decision(self, decision_id: str) -> ApprovalDecision | None:
        identifier = _require_nonempty(decision_id, field_name="decision_id")
        row = self._session.get(ApprovalDecisionRow, identifier)
        if row is None:
            return None
        parent = self.get(row.approval_id)
        if parent is None:
            raise IntegrityViolation(
                "stored ApprovalDecision has no parent ApprovalItem",
                decision_id=identifier,
            )
        decision = _parse_decision_row(
            row,
            expected_decision_id=identifier,
            expected_approval_id=parent.approval_id,
        )
        if decision not in parent.decisions:
            raise IntegrityViolation(
                "stored ApprovalDecision is not absorbed by its parent workflow revision",
                decision_id=identifier,
                approval_id=parent.approval_id,
            )
        return decision

    def get_subject_head(self, subject_series_id: str) -> SubjectHead | None:
        series_id = _require_nonempty(subject_series_id, field_name="subject_series_id")
        row = self._session.get(SubjectHeadRow, series_id)
        if row is None:
            return None
        head = _head_from_row(row, expected_series_id=series_id)
        self._verify_head_binding(head, row=row)
        return head

    def compare_and_set_subject_head(
        self,
        subject_series_id: str,
        expected: SubjectHead | None,
        replacement: SubjectHead,
    ) -> SubjectHead:
        series_id = _require_nonempty(subject_series_id, field_name="subject_series_id")
        candidate = _revalidate_contract(
            replacement,
            SubjectHead,
            label="SubjectHead replacement",
        )
        if candidate.subject_series_id != series_id:
            raise IntegrityViolation("SubjectHead replacement uses a different subject series")
        approval = self._verify_head_binding(candidate)
        values = {
            "subject_series_id": series_id,
            "current_subject_artifact_id": candidate.current_subject_artifact_id,
            "current_subject_revision": approval.subject_revision,
            "current_subject_digest": approval.subject_digest,
            "current_approval_id": candidate.current_approval_id,
            "revision": candidate.revision,
        }

        current = self.get_subject_head(series_id)
        if expected is None:
            if candidate.revision != 1:
                raise IntegrityViolation("new SubjectHead must start at revision 1")
            if approval.subject_revision != 1 or approval.supersedes_approval_id is not None:
                raise IntegrityViolation(
                    "new SubjectHead must bind the first unsuperseded subject revision",
                    subject_series_id=series_id,
                )
            if current is not None:
                if current == candidate:
                    return current
                raise Conflict(
                    "SubjectHead create expected no current head",
                    subject_series_id=series_id,
                    actual=current.model_dump(mode="json"),
                )
            try:
                result = self._session.execute(
                    sqlite_insert(SubjectHeadRow)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[SubjectHeadRow.subject_series_id])
                )
            except IntegrityError as exc:
                raise IntegrityViolation(
                    "SubjectHead violates persisted workflow references",
                    subject_series_id=series_id,
                ) from exc
            if result.rowcount != 1:
                self._session.expire_all()
                actual = self.get_subject_head(series_id)
                if actual == candidate:
                    return candidate
                raise Conflict(
                    "SubjectHead create expected no current head",
                    subject_series_id=series_id,
                    actual=None if actual is None else actual.model_dump(mode="json"),
                )
            return candidate

        expected_head = _revalidate_contract(
            expected,
            SubjectHead,
            label="expected SubjectHead",
        )
        if expected_head.subject_series_id != series_id:
            raise IntegrityViolation("expected SubjectHead uses a different subject series")
        if candidate.revision != expected_head.revision + 1:
            raise IntegrityViolation(
                "SubjectHead replacement revision must increment by exactly one"
            )
        expected_approval = self._verify_head_binding(expected_head)
        if (
            approval.supersedes_approval_id != expected_approval.approval_id
            or approval.subject_revision != expected_approval.subject_revision + 1
            or approval.subject_kind != expected_approval.subject_kind
        ):
            raise IntegrityViolation(
                "SubjectHead replacement does not supersede the expected subject revision",
                subject_series_id=series_id,
            )
        if current == candidate:
            return current
        if current != expected_head:
            raise Conflict(
                "SubjectHead compare-and-set precondition did not match",
                subject_series_id=series_id,
                expected=expected_head.model_dump(mode="json"),
                actual=None if current is None else current.model_dump(mode="json"),
            )

        result = self._session.execute(
            update(SubjectHeadRow)
            .where(
                SubjectHeadRow.subject_series_id == series_id,
                SubjectHeadRow.current_subject_artifact_id
                == expected_head.current_subject_artifact_id,
                SubjectHeadRow.current_approval_id == expected_head.current_approval_id,
                SubjectHeadRow.revision == expected_head.revision,
            )
            .values(**{key: value for key, value in values.items() if key != "subject_series_id"})
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self.get_subject_head(series_id)
            raise Conflict(
                "SubjectHead compare-and-set precondition did not match",
                subject_series_id=series_id,
                expected=expected_head.model_dump(mode="json"),
                actual=None if actual is None else actual.model_dump(mode="json"),
            )
        self._session.expire_all()
        return candidate

    def current(
        self,
        subject_series_id: str,
    ) -> tuple[SubjectHead, ApprovalItem] | None:
        head = self.get_subject_head(subject_series_id)
        if head is None:
            return None
        approval = self.get(head.current_approval_id)
        if approval is None:
            raise IntegrityViolation(
                "SubjectHead references a missing ApprovalItem",
                subject_series_id=head.subject_series_id,
            )
        return head, approval

    def _subject_revision_collision(self, item: ApprovalItem) -> str | None:
        return self._session.scalar(
            select(ApprovalItemRow.approval_id).where(
                ApprovalItemRow.subject_series_id == item.subject_series_id,
                ApprovalItemRow.subject_revision == item.subject_revision,
            )
        )

    def _verify_subject_artifact(self, item: ApprovalItem) -> None:
        row = self._session.get(ArtifactRow, item.subject_artifact_id)
        if row is None:
            raise IntegrityViolation(
                "ApprovalItem subject Artifact is missing",
                approval_id=item.approval_id,
                subject_artifact_id=item.subject_artifact_id,
            )
        expected_kind = _SUBJECT_ARTIFACT_KIND[item.subject_kind]
        if row.kind != expected_kind:
            raise IntegrityViolation(
                "ApprovalItem subject Artifact kind does not match",
                approval_id=item.approval_id,
                expected_kind=expected_kind,
                actual_kind=row.kind,
            )
        if row.payload_hash != item.subject_digest:
            raise IntegrityViolation(
                "ApprovalItem subject digest does not match the Artifact payload hash",
                approval_id=item.approval_id,
            )

    def _validate_replacement_identity(
        self,
        approval_id: str,
        expected_workflow_revision: int,
        replacement: ApprovalItem,
    ) -> None:
        if replacement.approval_id != approval_id:
            raise IntegrityViolation("approval replacement uses a different approval_id")
        if replacement.workflow_revision != expected_workflow_revision + 1:
            raise IntegrityViolation(
                "approval replacement workflow revision must increment by exactly one"
            )

    def _verify_replacement(
        self,
        current: ApprovalItem,
        replacement: ApprovalItem,
        *,
        allow_new_decision: ApprovalDecision | None,
    ) -> None:
        current_wire = current.model_dump(mode="python")
        replacement_wire = replacement.model_dump(mode="python")
        for field_name in _IMMUTABLE_APPROVAL_FIELDS:
            if typed_canonical_json(current_wire[field_name]) != typed_canonical_json(
                replacement_wire[field_name]
            ):
                raise IntegrityViolation(
                    "approval replacement changes immutable approval fields",
                    approval_id=current.approval_id,
                    field_name=field_name,
                )

        if current.target_binding != replacement.target_binding:
            can_bind_constraint_once = (
                current.subject_kind == "constraint_proposal"
                and current.target_binding is None
                and replacement.target_binding is not None
            )
            if not can_bind_constraint_once:
                raise IntegrityViolation(
                    "approval replacement changes an immutable target binding",
                    approval_id=current.approval_id,
                )

        expected_decisions = current.decisions
        if allow_new_decision is not None:
            expected_decisions = tuple(
                sorted((*expected_decisions, allow_new_decision), key=lambda item: item.decision_id)
            )
        if typed_canonical_json(
            [item.model_dump(mode="python") for item in expected_decisions]
        ) != typed_canonical_json(replacement_wire["decisions"]):
            raise IntegrityViolation(
                "approval replacement decisions do not match append-only decision history",
                approval_id=current.approval_id,
            )
        self._verify_subject_artifact(replacement)

    def _write_replacement(
        self,
        approval_id: str,
        expected_workflow_revision: int,
        replacement: ApprovalItem,
    ) -> None:
        values = _approval_values(replacement)
        del values["approval_id"]
        try:
            result = self._session.execute(
                update(ApprovalItemRow)
                .where(
                    ApprovalItemRow.approval_id == approval_id,
                    ApprovalItemRow.workflow_revision == expected_workflow_revision,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
        except IntegrityError as exc:
            raise IntegrityViolation(
                "approval replacement violates persisted workflow references",
                approval_id=approval_id,
            ) from exc
        if result.rowcount != 1:
            self._session.expire_all()
            actual_row = self._session.get(ApprovalItemRow, approval_id)
            raise Conflict(
                "ApprovalItem workflow revision changed during compare-and-set",
                approval_id=approval_id,
                expected_workflow_revision=expected_workflow_revision,
                actual_workflow_revision=(
                    None if actual_row is None else actual_row.workflow_revision
                ),
            )
        self._session.expire_all()

    def _verify_head_binding(
        self,
        head: SubjectHead,
        *,
        row: SubjectHeadRow | None = None,
    ) -> ApprovalItem:
        approval = self.get(head.current_approval_id)
        if approval is None:
            raise IntegrityViolation(
                "SubjectHead references a missing ApprovalItem",
                subject_series_id=head.subject_series_id,
            )
        if (
            approval.subject_series_id != head.subject_series_id
            or approval.subject_artifact_id != head.current_subject_artifact_id
        ):
            raise IntegrityViolation(
                "SubjectHead ApprovalItem does not bind the replacement subject",
                subject_series_id=head.subject_series_id,
                approval_id=head.current_approval_id,
            )
        if head.revision != approval.subject_revision:
            raise IntegrityViolation(
                "SubjectHead revision differs from its ApprovalItem subject revision",
                subject_series_id=head.subject_series_id,
                approval_id=head.current_approval_id,
            )
        if row is not None and (
            row.current_subject_revision != approval.subject_revision
            or row.current_subject_digest != approval.subject_digest
        ):
            raise IntegrityViolation(
                "SubjectHead subject metadata differs from its ApprovalItem",
                subject_series_id=head.subject_series_id,
            )
        return approval


__all__ = ["SqlApprovalRepository"]
