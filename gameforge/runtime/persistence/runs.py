"""SQLite persistence for immutable Run inputs and monotonic execution heads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import typed_canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation, InvalidStateTransition
from gameforge.contracts.findings import FindingRevisionV1, finding_revision_digest
from gameforge.contracts.jobs import (
    AttemptLeasedDataV1,
    RunAttempt,
    RunCommandRecordV1,
    RunEvent,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunLease,
    RunQueuedDataV1,
    RunRecord,
)
from gameforge.runtime.persistence.models import (
    ArtifactRow,
    FindingRevisionRow,
    RunAttemptRow,
    RunCommandRow,
    RunEventRow,
    RunFindingLinkRow,
    RunIntermediateArtifactLinkRow,
    RunLeaseRow,
    RunRow,
)


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_ACTIVE_RUN_STATUSES = frozenset({"leased", "running"})
_ACTIVE_ATTEMPT_STATUSES = frozenset({"leased", "running"})


@dataclass(frozen=True, slots=True)
class RunClaim:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


def _require_nonempty(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityViolation(f"{field_name} must be a non-empty string")
    return value


def _require_positive(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise IntegrityViolation(f"{field_name} must be a positive integer")
    return value


def _parse_utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise IntegrityViolation(f"{field_name} must be a non-empty UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrityViolation(f"{field_name} must be a UTC timestamp") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation(f"{field_name} must be a UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _canonical_wire(value: BaseModel) -> str:
    return typed_canonical_json(value.model_dump(mode="python"))


def _revalidate(value: object, model_type: type[_ModelT], *, label: str) -> _ModelT:
    if not isinstance(value, model_type):
        raise IntegrityViolation(f"{label} requires {model_type.__name__}")
    wire = value.model_dump(mode="python")
    try:
        parsed = model_type.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("wire is not canonical")
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"{label} wire is invalid") from exc
    return parsed


def _run_wire(row: RunRow) -> dict[str, Any]:
    return {
        "run_schema_version": row.run_schema_version,
        "run_id": row.run_id,
        "kind": {"kind": row.kind, "version": row.kind_version},
        "status": row.status,
        "revision": row.revision,
        "idempotency_scope": row.idempotency_scope,
        "idempotency_key": row.idempotency_key,
        "request_hash": row.request_hash,
        "payload": row.payload,
        "payload_hash": row.payload_hash,
        "run_kind_definition_digest": row.run_kind_definition_digest,
        "outcome_policy_set_digest": row.outcome_policy_set_digest,
        "migration_capability_matrix": row.migration_capability_matrix,
        "failure_classifier": row.failure_classifier,
        "dispatch_trace_carrier": row.dispatch_trace_carrier,
        "initiated_by": row.initiated_by,
        "queue_deadline_utc": row.queue_deadline_utc,
        "attempt_timeout_ns": row.attempt_timeout_ns,
        "overall_deadline_utc": row.overall_deadline_utc,
        "cancel_requested_at": row.cancel_requested_at,
        "cancel_requested_by": row.cancel_requested_by,
        "current_attempt_no": row.current_attempt_no,
        "next_attempt_no": row.next_attempt_no,
        "next_fencing_token": row.next_fencing_token,
        "next_event_seq": row.next_event_seq,
        "budget_set_snapshot_id": row.budget_set_snapshot_id,
        "run_budget_hold_group_id": row.run_budget_hold_group_id,
        "concurrency_permit_group_id": row.concurrency_permit_group_id,
        "retry_policy": row.retry_policy,
        "max_attempts": row.max_attempts,
        "retry_not_before_utc": row.retry_not_before_utc,
        "result_artifact_id": row.result_artifact_id,
        "failure_artifact_id": row.failure_artifact_id,
        "terminal_cassette_artifact_id": row.terminal_cassette_artifact_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _parse_run_row(row: RunRow, *, expected_run_id: str) -> RunRecord:
    wire = _run_wire(row)
    try:
        if row.run_id != expected_run_id:
            raise ValueError("Run storage key differs from requested id")
        parsed = RunRecord.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("Run row is not canonical")
        for field_name in (
            "queue_deadline_utc",
            "overall_deadline_utc",
            "created_at",
            "updated_at",
        ):
            _parse_utc(getattr(parsed, field_name), field_name=field_name)
        if parsed.cancel_requested_at is not None:
            _parse_utc(parsed.cancel_requested_at, field_name="cancel_requested_at")
        if parsed.retry_not_before_utc is not None:
            _parse_utc(parsed.retry_not_before_utc, field_name="retry_not_before_utc")
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation("stored Run is invalid", run_id=expected_run_id) from exc
    return parsed


def _run_values(run: RunRecord) -> dict[str, Any]:
    wire = run.model_dump(mode="json")
    kind = wire.pop("kind")
    wire["kind"] = kind["kind"]
    wire["kind_version"] = kind["version"]
    return wire


def _attempt_wire(row: RunAttemptRow) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "attempt_no": row.attempt_no,
        "status": row.status,
        "fencing_token": row.fencing_token,
        "worker_principal_id": row.worker_principal_id,
        "trace_id": row.trace_id,
        "next_call_ordinal": row.next_call_ordinal,
        "started_at": row.started_at,
        "attempt_deadline_utc": row.attempt_deadline_utc,
        "ended_at": row.ended_at,
        "failure_class": row.failure_class,
        "retryable": row.retryable,
        "failure_artifact_id": row.failure_artifact_id,
        "cassette_bundle_artifact_id": row.cassette_bundle_artifact_id,
    }


def _parse_attempt_row(
    row: RunAttemptRow,
    *,
    expected_run_id: str,
    expected_attempt_no: int,
) -> RunAttempt:
    wire = _attempt_wire(row)
    try:
        if row.run_id != expected_run_id or row.attempt_no != expected_attempt_no:
            raise ValueError("attempt storage key differs from requested identity")
        parsed = RunAttempt.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("attempt row is not canonical")
        for field_name in ("started_at", "attempt_deadline_utc", "ended_at"):
            value = getattr(parsed, field_name)
            if value is not None:
                _parse_utc(value, field_name=field_name)
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation(
            "stored RunAttempt is invalid",
            run_id=expected_run_id,
            attempt_no=expected_attempt_no,
        ) from exc
    return parsed


def _attempt_values(attempt: RunAttempt) -> dict[str, Any]:
    return attempt.model_dump(mode="json")


def _lease_wire(row: RunLeaseRow) -> dict[str, Any]:
    return {
        "lease_id": row.lease_id,
        "run_id": row.run_id,
        "attempt_no": row.attempt_no,
        "fencing_token": row.fencing_token,
        "lease_version": row.lease_version,
        "owner_principal_id": row.owner_principal_id,
        "acquired_at": row.acquired_at,
        "heartbeat_at": row.heartbeat_at,
        "expires_at": row.expires_at,
        "status": row.status,
    }


def _parse_lease_row(row: RunLeaseRow, *, expected_lease_id: str) -> RunLease:
    wire = _lease_wire(row)
    try:
        if row.lease_id != expected_lease_id:
            raise ValueError("lease storage key differs from requested id")
        parsed = RunLease.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("lease row is not canonical")
        for field_name in ("acquired_at", "heartbeat_at", "expires_at"):
            _parse_utc(getattr(parsed, field_name), field_name=field_name)
        if row.released_at is not None:
            _parse_utc(row.released_at, field_name="released_at")
        if parsed.status == "active" and row.released_at is not None:
            raise ValueError("active lease cannot have a release timestamp")
        if parsed.status != "active" and row.released_at is None:
            raise ValueError("closed lease requires a release timestamp")
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation("stored RunLease is invalid", lease_id=expected_lease_id) from exc
    return parsed


def _lease_values(lease: RunLease) -> dict[str, Any]:
    values = lease.model_dump(mode="json")
    values["released_at"] = None
    return values


def _event_wire(row: RunEventRow) -> dict[str, Any]:
    return {
        "event_schema_version": row.event_schema_version,
        "run_id": row.run_id,
        "seq": row.seq,
        "event_type": row.event_type,
        "attempt_no": row.attempt_no,
        "occurred_at": row.occurred_at,
        "data_schema_version": row.data_schema_version,
        "data": row.data,
        "trace_id": row.trace_id,
    }


def _parse_event_row(
    row: RunEventRow,
    *,
    expected_run_id: str,
    expected_seq: int,
) -> RunEvent:
    wire = _event_wire(row)
    try:
        if row.run_id != expected_run_id or row.seq != expected_seq:
            raise ValueError("event storage key differs from requested identity")
        parsed = RunEvent.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("event row is not canonical")
        _parse_utc(parsed.occurred_at, field_name="occurred_at")
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation(
            "stored RunEvent is invalid",
            run_id=expected_run_id,
            seq=expected_seq,
        ) from exc
    return parsed


def _event_values(event: RunEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")


def _command_wire(row: RunCommandRow) -> dict[str, Any]:
    return {
        "record_schema_version": row.record_schema_version,
        "run_id": row.run_id,
        "command": {
            "command_schema_version": row.command_schema_version,
            "command_id": row.command_id,
            "client_id": row.client_id,
            "client_seq": row.client_seq,
            "idempotency_key": row.idempotency_key,
            "expected_run_revision": row.expected_run_revision,
            "type": row.type,
            "payload_schema_id": row.payload_schema_id,
            "payload": row.payload,
        },
        "request_hash": row.request_hash,
        "actor": row.actor,
        "status": row.status,
        "revision": row.revision,
        "created_at": row.created_at,
        "claimed_at": row.claimed_at,
        "claimed_attempt_no": row.claimed_attempt_no,
        "claimed_fencing_token": row.claimed_fencing_token,
        "applied_at": row.applied_at,
        "result_event_seq": row.result_event_seq,
        "rejection_code": row.rejection_code,
    }


def _parse_command_row(
    row: RunCommandRow,
    *,
    expected_run_id: str,
    expected_command_id: str,
) -> RunCommandRecordV1:
    wire = _command_wire(row)
    try:
        if row.run_id != expected_run_id or row.command_id != expected_command_id:
            raise ValueError("command storage key differs from requested identity")
        parsed = RunCommandRecordV1.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("command row is not canonical")
        for field_name in ("created_at", "claimed_at", "applied_at"):
            value = getattr(parsed, field_name)
            if value is not None:
                _parse_utc(value, field_name=field_name)
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation(
            "stored RunCommand is invalid",
            run_id=expected_run_id,
            command_id=expected_command_id,
        ) from exc
    return parsed


def _command_values(record: RunCommandRecordV1) -> dict[str, Any]:
    values = record.model_dump(mode="json")
    command = values.pop("command")
    values.update(command)
    return values


def _intermediate_wire(row: RunIntermediateArtifactLinkRow) -> dict[str, Any]:
    return {
        "link_schema_version": row.link_schema_version,
        "run_id": row.run_id,
        "attempt_no": row.attempt_no,
        "call_ordinal": row.call_ordinal,
        "artifact_id": row.artifact_id,
        "role": row.role,
        "request_hash": row.request_hash,
        "fencing_token": row.fencing_token,
        "published_at": row.published_at,
    }


def _parse_intermediate_row(
    row: RunIntermediateArtifactLinkRow,
    *,
    expected_run_id: str,
    expected_attempt_no: int,
    expected_call_ordinal: int,
) -> RunIntermediateArtifactLinkV1:
    wire = _intermediate_wire(row)
    try:
        if (
            row.run_id != expected_run_id
            or row.attempt_no != expected_attempt_no
            or row.call_ordinal != expected_call_ordinal
        ):
            raise ValueError("intermediate-link storage key differs from requested identity")
        parsed = RunIntermediateArtifactLinkV1.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("intermediate-link row is not canonical")
        _parse_utc(parsed.published_at, field_name="published_at")
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation(
            "stored RunIntermediateArtifactLink is invalid",
            run_id=expected_run_id,
            attempt_no=expected_attempt_no,
            call_ordinal=expected_call_ordinal,
        ) from exc
    return parsed


def _finding_link_wire(row: RunFindingLinkRow) -> dict[str, Any]:
    return {
        "link_schema_version": row.link_schema_version,
        "run_id": row.run_id,
        "attempt_no": row.attempt_no,
        "ordinal": row.ordinal,
        "finding_id": row.finding_id,
        "finding_revision": row.finding_revision,
        "finding_digest": row.finding_digest,
        "evidence_artifact_id": row.evidence_artifact_id,
    }


def _parse_finding_link_row(
    row: RunFindingLinkRow,
    *,
    expected_run_id: str,
    expected_attempt_no: int,
    expected_ordinal: int,
) -> RunFindingLinkV1:
    wire = _finding_link_wire(row)
    try:
        if (
            row.run_id != expected_run_id
            or row.attempt_no != expected_attempt_no
            or row.ordinal != expected_ordinal
        ):
            raise ValueError("finding-link storage key differs from requested identity")
        parsed = RunFindingLinkV1.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("finding-link row is not canonical")
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored RunFindingLink is invalid",
            run_id=expected_run_id,
            attempt_no=expected_attempt_no,
            ordinal=expected_ordinal,
        ) from exc
    return parsed


def _parse_linked_finding(row: FindingRevisionRow) -> FindingRevisionV1:
    wire = {
        "revision_schema_version": row.revision_schema_version,
        "finding_id": row.finding_id,
        "revision": row.revision,
        "supersedes_revision": row.supersedes_revision,
        "created_at": row.created_at,
        "payload": row.payload,
    }
    try:
        parsed = FindingRevisionV1.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("Finding revision row is not canonical")
        if finding_revision_digest(parsed) != row.finding_digest:
            raise ValueError("Finding revision digest differs from its content")
        _parse_utc(parsed.created_at, field_name="created_at")
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation(
            "linked Finding revision is invalid",
            finding_id=row.finding_id,
            finding_revision=row.revision,
        ) from exc
    return parsed


class SqlRunRepository:
    """Transaction-bound Run store; callers own commit through their UnitOfWork."""

    def __init__(self, session: Session) -> None:
        if session.get_bind().dialect.name != "sqlite":
            raise ValueError("SqlRunRepository requires a SQLite session")
        self._session = session

    def get(self, run_id: str) -> RunRecord | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        row = self._session.get(RunRow, selected_run_id)
        if row is None:
            return None
        run = _parse_run_row(row, expected_run_id=selected_run_id)
        self._verify_run_heads(run)
        self._verify_run_state(run)
        return run

    def get_by_idempotency(self, *, scope: str, key: str) -> RunRecord | None:
        selected_scope = _require_nonempty(scope, field_name="idempotency scope")
        selected_key = _require_nonempty(key, field_name="idempotency key")
        row = self._session.execute(
            select(RunRow).where(
                RunRow.idempotency_scope == selected_scope,
                RunRow.idempotency_key == selected_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return self.get(row.run_id)

    def create_queued(self, run: RunRecord, initial_event: RunEvent) -> RunRecord:
        parsed = _revalidate(run, RunRecord, label="Run create")
        event = _revalidate(initial_event, RunEvent, label="Run initial event")
        self._validate_initial_run(parsed, event)

        idempotent_row = self._session.execute(
            select(RunRow).where(
                RunRow.idempotency_scope == parsed.idempotency_scope,
                RunRow.idempotency_key == parsed.idempotency_key,
            )
        ).scalar_one_or_none()
        if idempotent_row is not None:
            retained = self.get(idempotent_row.run_id)
            if retained is None:  # pragma: no cover - row was loaded above
                raise IntegrityViolation("idempotent Run disappeared during its read")
            if retained.request_hash != parsed.request_hash:
                raise Conflict(
                    "Run idempotency key is bound to a different request",
                    scope=parsed.idempotency_scope,
                    key=parsed.idempotency_key,
                    expected_request_hash=parsed.request_hash,
                    actual_request_hash=retained.request_hash,
                )
            return retained

        existing = self._session.get(RunRow, parsed.run_id)
        if existing is not None:
            retained = self.get(parsed.run_id)
            if retained is None:  # pragma: no cover - row was loaded above
                raise IntegrityViolation("Run disappeared during its immutable comparison")
            if _canonical_wire(retained) != _canonical_wire(parsed):
                raise IntegrityViolation(
                    "immutable Run id is already bound to different content",
                    run_id=parsed.run_id,
                )
            retained_event = self.get_event(parsed.run_id, 1)
            if retained_event != event:
                raise IntegrityViolation(
                    "immutable Run initial event differs from retained content",
                    run_id=parsed.run_id,
                )
            return retained

        result = self._session.execute(
            sqlite_insert(RunRow)
            .values(**_run_values(parsed))
            .on_conflict_do_nothing(index_elements=[RunRow.run_id])
        )
        if result.rowcount != 1:
            self._session.expire_all()
            retained = self.get(parsed.run_id)
            if retained is None or _canonical_wire(retained) != _canonical_wire(parsed):
                raise IntegrityViolation(
                    "immutable Run insert conflicted with different content",
                    run_id=parsed.run_id,
                )
            return retained
        self._session.add(RunEventRow(**_event_values(event)))
        self._session.flush()
        return parsed

    def get_claim_candidate(self, now_utc: str) -> RunRecord | None:
        _parse_utc(now_utc, field_name="now_utc")
        row = self._session.execute(
            select(RunRow)
            .where(RunRow.status == "queued")
            .order_by(RunRow.created_at, RunRow.run_id)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return self.get(row.run_id)

    def claim(
        self,
        run_id: str,
        *,
        expected_revision: int,
        worker_principal_id: str,
        lease_id: str,
        acquired_at: str,
        expires_at: str,
        permit_group_id: str,
        trace_id: str | None = None,
    ) -> RunClaim:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        expected = _require_positive(expected_revision, field_name="expected_revision")
        owner = _require_nonempty(worker_principal_id, field_name="worker_principal_id")
        selected_lease_id = _require_nonempty(lease_id, field_name="lease_id")
        permit_group = _require_nonempty(permit_group_id, field_name="permit_group_id")
        if trace_id is not None:
            _require_nonempty(trace_id, field_name="trace_id")
        acquired = _parse_utc(acquired_at, field_name="acquired_at")
        expires = _parse_utc(expires_at, field_name="expires_at")
        if expires <= acquired:
            raise IntegrityViolation("lease expiry must be after acquisition")

        current = self.get(selected_run_id)
        if current is None:
            raise IntegrityViolation("Run claim target does not exist", run_id=selected_run_id)
        if current.revision != expected:
            raise Conflict(
                "Run claim revision does not match",
                run_id=selected_run_id,
                expected_revision=expected,
                actual_revision=current.revision,
            )
        if current.status != "queued":
            raise InvalidStateTransition(
                "Task 13 claim accepts only a queued Run",
                run_id=selected_run_id,
                status=current.status,
            )
        if current.current_attempt_no is not None:
            raise IntegrityViolation("claimable Run retains a current attempt")

        attempt_no = current.next_attempt_no
        fencing_token = current.next_fencing_token
        event_seq = current.next_event_seq
        updated = RunRecord.model_validate(
            {
                **current.model_dump(mode="python"),
                "status": "leased",
                "revision": current.revision + 1,
                "current_attempt_no": attempt_no,
                "next_attempt_no": attempt_no + 1,
                "next_fencing_token": fencing_token + 1,
                "next_event_seq": event_seq + 1,
                "concurrency_permit_group_id": permit_group,
                "retry_not_before_utc": None,
                "updated_at": acquired_at,
            }
        )
        attempt = RunAttempt(
            run_id=selected_run_id,
            attempt_no=attempt_no,
            status="leased",
            fencing_token=fencing_token,
            worker_principal_id=owner,
            trace_id=trace_id,
            next_call_ordinal=1,
        )
        lease = RunLease(
            lease_id=selected_lease_id,
            run_id=selected_run_id,
            attempt_no=attempt_no,
            fencing_token=fencing_token,
            lease_version=1,
            owner_principal_id=owner,
            acquired_at=acquired_at,
            heartbeat_at=acquired_at,
            expires_at=expires_at,
            status="active",
        )
        event = RunEvent(
            run_id=selected_run_id,
            seq=event_seq,
            event_type="attempt.leased",
            attempt_no=attempt_no,
            occurred_at=acquired_at,
            data_schema_version="attempt-leased@1",
            data=AttemptLeasedDataV1(
                attempt_no=attempt_no,
                lease_expires_at=expires_at,
            ),
            trace_id=trace_id,
        )

        result = self._session.execute(
            update(RunRow)
            .where(
                RunRow.run_id == selected_run_id,
                RunRow.revision == current.revision,
                RunRow.status == current.status,
                RunRow.current_attempt_no.is_(None),
                RunRow.next_attempt_no == attempt_no,
                RunRow.next_fencing_token == fencing_token,
                RunRow.next_event_seq == event_seq,
            )
            .values(**_run_values(updated))
        )
        if result.rowcount != 1:
            raise Conflict(
                "Run claim compare-and-set did not match",
                run_id=selected_run_id,
                expected_revision=current.revision,
            )
        self._session.add(RunAttemptRow(**_attempt_values(attempt)))
        self._session.add(RunLeaseRow(**_lease_values(lease)))
        self._session.add(RunEventRow(**_event_values(event)))
        self._session.flush()
        return RunClaim(run=updated, attempt=attempt, lease=lease, event=event)

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_attempt = _require_positive(attempt_no, field_name="attempt_no")
        row = self._session.get(RunAttemptRow, (selected_run_id, selected_attempt))
        if row is None:
            return None
        attempt = _parse_attempt_row(
            row,
            expected_run_id=selected_run_id,
            expected_attempt_no=selected_attempt,
        )
        self._verify_call_ordinal_head(attempt)
        return attempt

    def get_current_lease(self, run_id: str) -> RunLease | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        rows = self._session.execute(
            select(RunLeaseRow).where(
                RunLeaseRow.run_id == selected_run_id,
                RunLeaseRow.status == "active",
            ).limit(2)
        ).scalars().all()
        if len(rows) > 1:
            raise IntegrityViolation("Run has more than one active lease", run_id=selected_run_id)
        if not rows:
            return None
        return _parse_lease_row(rows[0], expected_lease_id=rows[0].lease_id)

    def get_event(self, run_id: str, seq: int) -> RunEvent | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_seq = _require_positive(seq, field_name="seq")
        row = self._session.get(RunEventRow, (selected_run_id, selected_seq))
        if row is None:
            return None
        return _parse_event_row(
            row,
            expected_run_id=selected_run_id,
            expected_seq=selected_seq,
        )

    def list_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> tuple[RunEvent, ...]:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise IntegrityViolation("after_seq must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
            raise IntegrityViolation("event limit must be between 1 and 1024")
        if self.get(selected_run_id) is None:
            return ()
        rows = self._session.execute(
            select(RunEventRow)
            .where(
                RunEventRow.run_id == selected_run_id,
                RunEventRow.seq > after_seq,
            )
            .order_by(RunEventRow.seq)
            .limit(limit)
        ).scalars()
        return tuple(
            _parse_event_row(row, expected_run_id=selected_run_id, expected_seq=row.seq)
            for row in rows
        )

    def put_intermediate_link(
        self,
        link: RunIntermediateArtifactLinkV1,
    ) -> RunIntermediateArtifactLinkV1:
        parsed = _revalidate(
            link,
            RunIntermediateArtifactLinkV1,
            label="Run intermediate link put",
        )
        existing = self.get_intermediate_link(
            parsed.run_id,
            parsed.attempt_no,
            parsed.call_ordinal,
        )
        if existing is not None:
            if _canonical_wire(existing) != _canonical_wire(parsed):
                raise IntegrityViolation(
                    "immutable Run intermediate link differs from retained content",
                    run_id=parsed.run_id,
                    attempt_no=parsed.attempt_no,
                    call_ordinal=parsed.call_ordinal,
                )
            return existing

        run = self.get(parsed.run_id)
        if run is None or run.current_attempt_no != parsed.attempt_no:
            raise InvalidStateTransition("intermediate link requires the current Run attempt")
        attempt = self.get_attempt(parsed.run_id, parsed.attempt_no)
        lease = self.get_current_lease(parsed.run_id)
        if (
            run.status not in _ACTIVE_RUN_STATUSES
            or attempt is None
            or attempt.status not in _ACTIVE_ATTEMPT_STATUSES
            or lease is None
            or lease.attempt_no != parsed.attempt_no
            or lease.fencing_token != parsed.fencing_token
            or attempt.fencing_token != parsed.fencing_token
        ):
            raise InvalidStateTransition("intermediate link requires the current fenced lease")
        if attempt.next_call_ordinal != parsed.call_ordinal:
            raise Conflict(
                "call ordinal does not match the Attempt head",
                expected_call_ordinal=attempt.next_call_ordinal,
                actual_call_ordinal=parsed.call_ordinal,
            )
        artifact = self._session.get(ArtifactRow, parsed.artifact_id)
        if artifact is None or artifact.kind != "source_rendered":
            raise IntegrityViolation(
                "prompt-rendered link requires a source_rendered Artifact",
                artifact_id=parsed.artifact_id,
            )

        result = self._session.execute(
            update(RunAttemptRow)
            .where(
                RunAttemptRow.run_id == parsed.run_id,
                RunAttemptRow.attempt_no == parsed.attempt_no,
                RunAttemptRow.fencing_token == parsed.fencing_token,
                RunAttemptRow.status.in_(_ACTIVE_ATTEMPT_STATUSES),
                RunAttemptRow.next_call_ordinal == parsed.call_ordinal,
            )
            .values(next_call_ordinal=parsed.call_ordinal + 1)
        )
        if result.rowcount != 1:
            raise Conflict("Attempt call-ordinal compare-and-set did not match")
        self._session.add(
            RunIntermediateArtifactLinkRow(**parsed.model_dump(mode="json"))
        )
        self._session.flush()
        return parsed

    def get_intermediate_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
    ) -> RunIntermediateArtifactLinkV1 | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_attempt = _require_positive(attempt_no, field_name="attempt_no")
        selected_ordinal = _require_positive(call_ordinal, field_name="call_ordinal")
        row = self._session.get(
            RunIntermediateArtifactLinkRow,
            (selected_run_id, selected_attempt, selected_ordinal),
        )
        if row is None:
            return None
        parsed = _parse_intermediate_row(
            row,
            expected_run_id=selected_run_id,
            expected_attempt_no=selected_attempt,
            expected_call_ordinal=selected_ordinal,
        )
        attempt = self.get_attempt(selected_run_id, selected_attempt)
        if (
            attempt is None
            or parsed.fencing_token != attempt.fencing_token
            or parsed.call_ordinal >= attempt.next_call_ordinal
        ):
            raise IntegrityViolation("stored intermediate link disagrees with its Attempt head")
        return parsed

    def put_finding_link(self, link: RunFindingLinkV1) -> RunFindingLinkV1:
        parsed = _revalidate(link, RunFindingLinkV1, label="Run finding link put")
        existing = self.get_finding_link(parsed.run_id, parsed.attempt_no, parsed.ordinal)
        if existing is not None:
            if _canonical_wire(existing) != _canonical_wire(parsed):
                raise IntegrityViolation(
                    "immutable Run finding link differs from retained content",
                    run_id=parsed.run_id,
                    attempt_no=parsed.attempt_no,
                    ordinal=parsed.ordinal,
                )
            return existing
        attempt = self.get_attempt(parsed.run_id, parsed.attempt_no)
        if attempt is None:
            raise IntegrityViolation("finding link Attempt does not exist")
        finding = self._session.get(
            FindingRevisionRow,
            (parsed.finding_id, parsed.finding_revision),
        )
        if finding is None:
            raise IntegrityViolation(
                "finding link does not match the retained Finding revision",
                finding_id=parsed.finding_id,
                finding_revision=parsed.finding_revision,
            )
        retained_finding = _parse_linked_finding(finding)
        if finding_revision_digest(retained_finding) != parsed.finding_digest:
            raise IntegrityViolation(
                "finding link digest differs from the retained Finding revision",
                finding_id=parsed.finding_id,
                finding_revision=parsed.finding_revision,
            )
        if self._session.get(ArtifactRow, parsed.evidence_artifact_id) is None:
            raise IntegrityViolation("finding link evidence artifact does not exist")
        duplicate = self._session.execute(
            select(RunFindingLinkRow).where(
                RunFindingLinkRow.run_id == parsed.run_id,
                RunFindingLinkRow.finding_id == parsed.finding_id,
                RunFindingLinkRow.finding_revision == parsed.finding_revision,
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise IntegrityViolation(
                "Finding revision is already linked at another ordinal",
                finding_id=parsed.finding_id,
                finding_revision=parsed.finding_revision,
            )
        self._session.add(RunFindingLinkRow(**parsed.model_dump(mode="json")))
        self._session.flush()
        return parsed

    def get_finding_link(
        self,
        run_id: str,
        attempt_no: int,
        ordinal: int,
    ) -> RunFindingLinkV1 | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_attempt = _require_positive(attempt_no, field_name="attempt_no")
        selected_ordinal = _require_positive(ordinal, field_name="ordinal")
        row = self._session.get(
            RunFindingLinkRow,
            (selected_run_id, selected_attempt, selected_ordinal),
        )
        if row is None:
            return None
        parsed = _parse_finding_link_row(
            row,
            expected_run_id=selected_run_id,
            expected_attempt_no=selected_attempt,
            expected_ordinal=selected_ordinal,
        )
        finding = self._session.get(
            FindingRevisionRow,
            (parsed.finding_id, parsed.finding_revision),
        )
        if finding is None:
            raise IntegrityViolation("stored finding link disagrees with its Finding revision")
        retained_finding = _parse_linked_finding(finding)
        if finding_revision_digest(retained_finding) != parsed.finding_digest:
            raise IntegrityViolation("stored finding link disagrees with its Finding revision")
        return parsed

    def put_command(self, record: RunCommandRecordV1) -> RunCommandRecordV1:
        parsed = _revalidate(record, RunCommandRecordV1, label="Run command put")
        existing = self.get_command(parsed.run_id, parsed.command.command_id)
        if existing is not None:
            if _canonical_wire(existing) != _canonical_wire(parsed):
                raise IntegrityViolation(
                    "immutable Run command differs from retained content",
                    run_id=parsed.run_id,
                    command_id=parsed.command.command_id,
                )
            return existing
        idempotent_row = self._session.execute(
            select(RunCommandRow).where(
                RunCommandRow.run_id == parsed.run_id,
                RunCommandRow.idempotency_key == parsed.command.idempotency_key,
            )
        ).scalar_one_or_none()
        if idempotent_row is not None:
            retained = _parse_command_row(
                idempotent_row,
                expected_run_id=parsed.run_id,
                expected_command_id=idempotent_row.command_id,
            )
            if retained.request_hash != parsed.request_hash:
                raise Conflict("Run command idempotency key has a different request hash")
            return retained
        client_row = self._session.execute(
            select(RunCommandRow).where(
                RunCommandRow.run_id == parsed.run_id,
                RunCommandRow.client_id == parsed.command.client_id,
                RunCommandRow.client_seq == parsed.command.client_seq,
            )
        ).scalar_one_or_none()
        if client_row is not None:
            raise Conflict("Run command client sequence is already bound")
        if self.get(parsed.run_id) is None:
            raise IntegrityViolation("Run command target does not exist")
        self._session.add(RunCommandRow(**_command_values(parsed)))
        self._session.flush()
        return parsed

    def get_command(self, run_id: str, command_id: str) -> RunCommandRecordV1 | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_command_id = _require_nonempty(command_id, field_name="command_id")
        row = self._session.get(RunCommandRow, (selected_run_id, selected_command_id))
        if row is None:
            return None
        return _parse_command_row(
            row,
            expected_run_id=selected_run_id,
            expected_command_id=selected_command_id,
        )

    def _validate_initial_run(self, run: RunRecord, event: RunEvent) -> None:
        if (
            run.status != "queued"
            or run.revision != 1
            or run.current_attempt_no is not None
            or run.next_attempt_no != 1
            or run.next_fencing_token != 1
            or run.next_event_seq != 2
            or run.concurrency_permit_group_id is not None
            or run.retry_not_before_utc is not None
            or run.cancel_requested_at is not None
            or run.cancel_requested_by is not None
        ):
            raise IntegrityViolation("queued Run has noninitial state or preallocated heads")
        expected_data = RunQueuedDataV1(
            run_kind=run.kind,
            queue_deadline_utc=run.queue_deadline_utc,
            overall_deadline_utc=run.overall_deadline_utc,
        )
        if (
            event.run_id != run.run_id
            or event.seq != 1
            or event.event_type != "run.queued"
            or event.attempt_no is not None
            or event.occurred_at != run.created_at
            or event.data != expected_data
        ):
            raise IntegrityViolation("queued Run initial event does not match its immutable input")
        if _parse_utc(run.updated_at, field_name="updated_at") != _parse_utc(
            run.created_at,
            field_name="created_at",
        ):
            raise IntegrityViolation("queued Run must begin with equal create/update timestamps")

    def _verify_run_heads(self, run: RunRecord) -> None:
        event_count, first_event, last_event = self._session.execute(
            select(
                func.count(RunEventRow.seq),
                func.min(RunEventRow.seq),
                func.max(RunEventRow.seq),
            ).where(RunEventRow.run_id == run.run_id)
        ).one()
        expected_event_count = run.next_event_seq - 1
        if (
            event_count != expected_event_count
            or first_event != 1
            or last_event != expected_event_count
        ):
            raise IntegrityViolation("Run event head is not the next free sequence", run_id=run.run_id)

        future_attempt = self._session.execute(
            select(RunAttemptRow.attempt_no).where(
                RunAttemptRow.run_id == run.run_id,
                RunAttemptRow.attempt_no >= run.next_attempt_no,
            ).limit(1)
        ).scalar_one_or_none()
        future_fencing = self._session.execute(
            select(RunAttemptRow.fencing_token).where(
                RunAttemptRow.run_id == run.run_id,
                RunAttemptRow.fencing_token >= run.next_fencing_token,
            ).limit(1)
        ).scalar_one_or_none()
        if future_attempt is not None or future_fencing is not None:
            raise IntegrityViolation("Run attempt/fencing head is not next-free", run_id=run.run_id)
        if run.next_attempt_no == 1:
            if run.next_fencing_token != 1:
                raise IntegrityViolation("unclaimed Run has a noninitial fencing head")
        else:
            predecessor = self.get_attempt(run.run_id, run.next_attempt_no - 1)
            if predecessor is None or predecessor.fencing_token != run.next_fencing_token - 1:
                raise IntegrityViolation("Run attempt/fencing head has no direct predecessor")

    def _verify_run_state(self, run: RunRecord) -> None:
        active_lease = self.get_current_lease(run.run_id)
        if run.status == "queued":
            attempt_exists = self._session.execute(
                select(RunAttemptRow.attempt_no).where(RunAttemptRow.run_id == run.run_id).limit(1)
            ).scalar_one_or_none()
            if (
                run.current_attempt_no is not None
                or run.concurrency_permit_group_id is not None
                or active_lease is not None
                or attempt_exists is not None
            ):
                raise IntegrityViolation("queued Run contains execution state", run_id=run.run_id)
            return
        if run.status == "retry_wait":
            if (
                run.current_attempt_no is not None
                or run.concurrency_permit_group_id is not None
                or active_lease is not None
            ):
                raise IntegrityViolation("retry-wait Run retains active execution state")
            return
        if run.status in _ACTIVE_RUN_STATUSES:
            if run.current_attempt_no is None or run.concurrency_permit_group_id is None:
                raise IntegrityViolation("active Run lacks current attempt or permit group")
            if (
                run.current_attempt_no != run.next_attempt_no - 1
                or run.next_fencing_token < 2
            ):
                raise IntegrityViolation("active Run does not point at its consumed attempt head")
            attempt = self.get_attempt(run.run_id, run.current_attempt_no)
            if (
                attempt is None
                or active_lease is None
                or active_lease.attempt_no != attempt.attempt_no
                or active_lease.fencing_token != attempt.fencing_token
                or active_lease.owner_principal_id != attempt.worker_principal_id
                or attempt.status != run.status
                or attempt.fencing_token != run.next_fencing_token - 1
            ):
                raise IntegrityViolation("active Run/Attempt/Lease projection is inconsistent")

    def _verify_call_ordinal_head(self, attempt: RunAttempt) -> None:
        link_count, first_ordinal, last_ordinal = self._session.execute(
            select(
                func.count(RunIntermediateArtifactLinkRow.call_ordinal),
                func.min(RunIntermediateArtifactLinkRow.call_ordinal),
                func.max(RunIntermediateArtifactLinkRow.call_ordinal),
            ).where(
                RunIntermediateArtifactLinkRow.run_id == attempt.run_id,
                RunIntermediateArtifactLinkRow.attempt_no == attempt.attempt_no,
            )
        ).one()
        expected_link_count = attempt.next_call_ordinal - 1
        expected_first = 1 if expected_link_count else None
        expected_last = expected_link_count if expected_link_count else None
        if (
            link_count != expected_link_count
            or first_ordinal != expected_first
            or last_ordinal != expected_last
        ):
            raise IntegrityViolation(
                "Attempt call-ordinal head is not closed over prompt links",
                run_id=attempt.run_id,
                attempt_no=attempt.attempt_no,
            )
