"""SQLite persistence for immutable Run inputs and monotonic execution heads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import typed_canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation, InvalidStateTransition
from gameforge.contracts.findings import FindingRevisionV1, finding_revision_digest
from gameforge.contracts.jobs import (
    AttemptLeasedDataV1,
    AttemptStartedDataV1,
    RetryDecisionV1,
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


@dataclass(frozen=True, slots=True)
class RunAttemptStart:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


@dataclass(frozen=True, slots=True)
class RunAttemptProgress:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


@dataclass(frozen=True, slots=True)
class RunAttemptClose:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    events: tuple[RunEvent, ...]


@dataclass(frozen=True, slots=True)
class RunTerminal:
    run: RunRecord
    attempt: RunAttempt | None
    lease: RunLease | None
    event: RunEvent


@dataclass(frozen=True, slots=True)
class RunCommandAcceptance:
    run: RunRecord
    record: RunCommandRecordV1
    events: tuple[RunEvent, ...]


class _AttemptWriteFence(Protocol):
    run_id: str
    attempt_no: int
    expected_run_revision: int
    lease_id: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class _RepositoryAttemptFence:
    run_id: str
    attempt_no: int
    expected_run_revision: int
    lease_id: str
    fencing_token: int


def _require_nonempty(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityViolation(f"{field_name} must be a non-empty string")
    return value


def _require_optional_nonempty(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_nonempty(value, field_name=field_name)


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
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset() != timedelta(0):
        raise IntegrityViolation(f"{field_name} must be a UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _require_canonical_utc(value: object, *, field_name: str) -> datetime:
    parsed = _parse_utc(value, field_name=field_name)
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if value != canonical:
        raise IntegrityViolation(f"{field_name} must use canonical UTC Z form")
    return parsed


def _canonical_wire(value: BaseModel) -> str:
    return typed_canonical_json(value.model_dump(mode="python"))


def _validate_cassette_publication(
    run: RunRecord,
    *,
    attempt_cassette_artifact_id: str | None,
    terminal_cassette_artifact_id: str | None,
    closes_attempt: bool,
    closes_run: bool,
) -> None:
    mode = run.payload.llm_execution_mode
    if closes_attempt:
        if mode == "record" and attempt_cassette_artifact_id is None:
            raise IntegrityViolation("RECORD attempt closure requires an attempt cassette bundle")
        if mode != "record" and attempt_cassette_artifact_id is not None:
            raise IntegrityViolation(f"{mode.upper()} attempt cannot publish a cassette bundle")
    elif attempt_cassette_artifact_id is not None:
        raise IntegrityViolation(
            "Run closure without an attempt cannot publish an attempt cassette"
        )

    if closes_run:
        if mode == "record" and terminal_cassette_artifact_id is None:
            raise IntegrityViolation("RECORD terminal Run requires a run cassette bundle")
        if mode == "replay" and terminal_cassette_artifact_id != run.payload.cassette_artifact_id:
            raise IntegrityViolation("REPLAY terminal Run requires its exact input cassette")
        if mode in {"live", "not_applicable"} and terminal_cassette_artifact_id is not None:
            raise IntegrityViolation(
                f"{mode.upper()} terminal Run cannot publish a cassette bundle"
            )
    elif terminal_cassette_artifact_id is not None:
        raise IntegrityViolation("nonterminal attempt closure cannot publish a run cassette bundle")

    if (
        attempt_cassette_artifact_id is not None
        and attempt_cassette_artifact_id == terminal_cassette_artifact_id
    ):
        raise IntegrityViolation("attempt and terminal cassette bundles must be distinct")


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

    def get_claim_candidate(self, *, now_utc: str) -> RunRecord | None:
        _require_canonical_utc(now_utc, field_name="now_utc")
        row = self._session.execute(
            select(RunRow)
            .where(
                RunRow.cancel_requested_at.is_(None),
                func.julianday(RunRow.overall_deadline_utc) > func.julianday(now_utc),
                or_(
                    and_(
                        RunRow.status == "queued",
                        func.julianday(RunRow.queue_deadline_utc) > func.julianday(now_utc),
                    ),
                    and_(
                        RunRow.status == "retry_wait",
                        RunRow.retry_not_before_utc.is_not(None),
                        func.julianday(RunRow.retry_not_before_utc) <= func.julianday(now_utc),
                    ),
                ),
            )
            .order_by(func.julianday(RunRow.created_at), RunRow.run_id)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return self.get(row.run_id)

    def list_expired_leases(self, *, now_utc: str, limit: int) -> tuple[RunRecord, ...]:
        """Bounded scan of active Runs whose current lease has already expired.

        The queue-authority ``get_claim_candidate`` only surfaces queued/retry_wait
        Runs; the reaper needs the complementary discovery over ``leased``/``running``
        Runs whose sole active ``RunLease`` is past ``expires_at``. The scan is bounded
        by ``limit`` and stably ordered (oldest expiry first) so a worker can drain
        expired leases deterministically across restarts. Each returned ``RunRecord``
        carries the current ``revision`` the caller feeds to ``reap_expired_lease`` /
        ``sweep_timeout`` as the fenced ``expected_run_revision``.
        """

        _require_canonical_utc(now_utc, field_name="now_utc")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
            raise IntegrityViolation("expired-lease scan limit must be between 1 and 1024")
        rows = (
            self._session.execute(
                select(RunLeaseRow.run_id)
                .join(RunRow, RunRow.run_id == RunLeaseRow.run_id)
                .where(
                    RunLeaseRow.status == "active",
                    RunLeaseRow.released_at.is_(None),
                    # Canonical UTC strings sort lexically == chronologically, so a
                    # bare string range keeps the ``ix_run_leases_expiry`` index
                    # usable for both the predicate and the ORDER BY.
                    RunLeaseRow.expires_at < now_utc,
                    RunRow.status.in_(("leased", "running")),
                )
                .order_by(RunLeaseRow.expires_at, RunLeaseRow.run_id)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        runs: list[RunRecord] = []
        for run_id in rows:
            run = self.get(run_id)
            if run is not None:
                runs.append(run)
        return tuple(runs)

    def claim(
        self,
        *,
        run_id: str,
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
        acquired = _require_canonical_utc(acquired_at, field_name="acquired_at")
        expires = _require_canonical_utc(expires_at, field_name="expires_at")
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
        if current.status not in {"queued", "retry_wait"}:
            raise InvalidStateTransition(
                "Run claim requires a queued or due retry-wait Run",
                run_id=selected_run_id,
                status=current.status,
            )
        if current.current_attempt_no is not None:
            raise IntegrityViolation("claimable Run retains a current attempt")
        if acquired >= _parse_utc(
            current.overall_deadline_utc,
            field_name="overall_deadline_utc",
        ):
            raise InvalidStateTransition("Run claim arrived at or after its overall deadline")
        if current.status == "queued" and acquired >= _parse_utc(
            current.queue_deadline_utc,
            field_name="queue_deadline_utc",
        ):
            raise InvalidStateTransition("queued Run claim arrived at or after its queue deadline")
        if current.status == "retry_wait":
            if current.retry_not_before_utc is None:
                raise IntegrityViolation("retry-wait Run lacks its not-before timestamp")
            if acquired < _parse_utc(
                current.retry_not_before_utc,
                field_name="retry_not_before_utc",
            ):
                raise InvalidStateTransition("retry-wait Run is not eligible for another claim")
        if self._session.get(RunLeaseRow, selected_lease_id) is not None:
            raise Conflict("Run lease id is already allocated", lease_id=selected_lease_id)

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
                (
                    RunRow.retry_not_before_utc.is_(None)
                    if current.retry_not_before_utc is None
                    else RunRow.retry_not_before_utc == current.retry_not_before_utc
                ),
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

    def start_attempt(
        self,
        *,
        run_id: str,
        attempt_no: int,
        expected_run_revision: int,
        lease_id: str,
        fencing_token: int,
        started_at: str,
        attempt_deadline_utc: str,
    ) -> RunAttemptStart:
        started = _require_canonical_utc(started_at, field_name="started_at")
        attempt_deadline = _require_canonical_utc(
            attempt_deadline_utc,
            field_name="attempt_deadline_utc",
        )
        if attempt_deadline <= started:
            raise IntegrityViolation("attempt deadline must be after its start")
        fence = _RepositoryAttemptFence(
            run_id=_require_nonempty(run_id, field_name="run_id"),
            attempt_no=_require_positive(attempt_no, field_name="attempt_no"),
            expected_run_revision=_require_positive(
                expected_run_revision,
                field_name="expected_run_revision",
            ),
            lease_id=_require_nonempty(lease_id, field_name="lease_id"),
            fencing_token=_require_positive(fencing_token, field_name="fencing_token"),
        )
        run, attempt, lease = self._load_fenced_attempt(
            fence,
            allowed_statuses=frozenset({"leased"}),
            occurred_at=started_at,
        )
        if attempt.status != "leased" or attempt.started_at is not None:
            raise Conflict("Run attempt start compare-and-set did not match")
        overall_deadline = _parse_utc(
            run.overall_deadline_utc,
            field_name="overall_deadline_utc",
        )
        timeout_deadline = started + timedelta(microseconds=(run.attempt_timeout_ns + 999) // 1000)
        expected_deadline = min(timeout_deadline, overall_deadline)
        if attempt_deadline != expected_deadline:
            raise IntegrityViolation(
                "attempt deadline differs from the exact frozen timeout/deadline ceiling"
            )

        event = RunEvent(
            run_id=run.run_id,
            seq=run.next_event_seq,
            event_type="attempt.started",
            attempt_no=attempt.attempt_no,
            occurred_at=started_at,
            data_schema_version="attempt-started@1",
            data=AttemptStartedDataV1(
                attempt_no=attempt.attempt_no,
                started_at=started_at,
                attempt_deadline_utc=attempt_deadline_utc,
            ),
            trace_id=attempt.trace_id,
        )
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": "running",
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + 1,
                "updated_at": started_at,
            }
        )
        updated_attempt = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "status": "running",
                "started_at": started_at,
                "attempt_deadline_utc": attempt_deadline_utc,
            }
        )
        run_result = self._session.execute(
            update(RunRow)
            .where(*self._run_fence_predicates(run))
            .values(
                status="running",
                revision=updated_run.revision,
                next_event_seq=updated_run.next_event_seq,
                updated_at=started_at,
            )
        )
        if run_result.rowcount != 1:
            raise Conflict("Run attempt start Run CAS did not match")
        attempt_result = self._session.execute(
            update(RunAttemptRow)
            .where(
                RunAttemptRow.run_id == run.run_id,
                RunAttemptRow.attempt_no == attempt.attempt_no,
                RunAttemptRow.status == "leased",
                RunAttemptRow.fencing_token == fence.fencing_token,
                RunAttemptRow.started_at.is_(None),
                RunAttemptRow.attempt_deadline_utc.is_(None),
                RunAttemptRow.ended_at.is_(None),
            )
            .values(
                status="running",
                started_at=started_at,
                attempt_deadline_utc=attempt_deadline_utc,
            )
        )
        if attempt_result.rowcount != 1:
            raise Conflict("Run attempt start Attempt CAS did not match")
        self._session.add(RunEventRow(**_event_values(event)))
        self._session.flush()
        return RunAttemptStart(updated_run, updated_attempt, lease, event)

    def renew_lease(
        self,
        *,
        run_id: str,
        attempt_no: int,
        lease_id: str,
        fencing_token: int,
        expected_lease_version: int,
        heartbeat_at: str,
        expires_at: str,
    ) -> RunLease:
        heartbeat = _require_canonical_utc(heartbeat_at, field_name="heartbeat_at")
        expires = _require_canonical_utc(expires_at, field_name="expires_at")
        if expires <= heartbeat:
            raise IntegrityViolation("renewed lease expiry must be after its heartbeat")
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_attempt_no = _require_positive(attempt_no, field_name="attempt_no")
        selected_lease_id = _require_nonempty(lease_id, field_name="lease_id")
        selected_token = _require_positive(fencing_token, field_name="fencing_token")
        selected_version = _require_positive(
            expected_lease_version,
            field_name="expected_lease_version",
        )
        run = self.get(selected_run_id)
        attempt = self.get_attempt(selected_run_id, selected_attempt_no)
        lease = self.get_current_lease(selected_run_id)
        if (
            run is None
            or attempt is None
            or lease is None
            or run.status not in _ACTIVE_RUN_STATUSES
            or run.current_attempt_no != selected_attempt_no
            or attempt.status != run.status
            or attempt.fencing_token != selected_token
            or lease.lease_id != selected_lease_id
            or lease.attempt_no != selected_attempt_no
            or lease.fencing_token != selected_token
            or lease.lease_version != selected_version
            or lease.status != "active"
        ):
            raise Conflict("Run lease renewal compare-and-set did not match")
        if heartbeat < _parse_utc(lease.heartbeat_at, field_name="lease.heartbeat_at"):
            raise IntegrityViolation("lease heartbeat cannot move backwards")
        if heartbeat >= _parse_utc(lease.expires_at, field_name="lease.expires_at"):
            raise Conflict("Run lease is expired and cannot be renewed")
        deadline = _parse_utc(
            attempt.attempt_deadline_utc or run.overall_deadline_utc,
            field_name="lease deadline ceiling",
        )
        if heartbeat >= deadline or expires > deadline:
            raise IntegrityViolation("renewed lease exceeds its execution deadline")
        updated = RunLease.model_validate(
            {
                **lease.model_dump(mode="python"),
                "lease_version": lease.lease_version + 1,
                "heartbeat_at": heartbeat_at,
                "expires_at": expires_at,
            }
        )
        result = self._session.execute(
            update(RunLeaseRow)
            .where(
                RunLeaseRow.lease_id == selected_lease_id,
                RunLeaseRow.run_id == selected_run_id,
                RunLeaseRow.attempt_no == selected_attempt_no,
                RunLeaseRow.fencing_token == selected_token,
                RunLeaseRow.lease_version == selected_version,
                RunLeaseRow.status == "active",
                RunLeaseRow.released_at.is_(None),
            )
            .values(
                lease_version=updated.lease_version,
                heartbeat_at=heartbeat_at,
                expires_at=expires_at,
            )
        )
        if result.rowcount != 1:
            raise Conflict("Run lease renewal CAS did not match")
        self._session.flush()
        return updated

    def append_progress(
        self,
        *,
        fence: _AttemptWriteFence,
        event: RunEvent,
    ) -> RunAttemptProgress:
        parsed_event = _revalidate(event, RunEvent, label="Run progress event")
        _require_canonical_utc(parsed_event.occurred_at, field_name="event.occurred_at")
        run, attempt, lease = self._load_fenced_attempt(
            fence,
            allowed_statuses=frozenset({"running"}),
            occurred_at=parsed_event.occurred_at,
        )
        self._validate_event_sequence(run, (parsed_event,))
        if (
            parsed_event.event_type != "attempt.progress"
            or parsed_event.attempt_no != attempt.attempt_no
            or parsed_event.trace_id != attempt.trace_id
        ):
            raise IntegrityViolation("Run progress event differs from its current attempt")
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + 1,
                "updated_at": parsed_event.occurred_at,
            }
        )
        result = self._session.execute(
            update(RunRow)
            .where(*self._run_fence_predicates(run))
            .values(
                revision=updated_run.revision,
                next_event_seq=updated_run.next_event_seq,
                updated_at=parsed_event.occurred_at,
            )
        )
        if result.rowcount != 1:
            raise Conflict("Run progress CAS did not match")
        self._session.add(RunEventRow(**_event_values(parsed_event)))
        self._session.flush()
        return RunAttemptProgress(updated_run, attempt, lease, parsed_event)

    def close_attempt_for_retry(
        self,
        *,
        fence: _AttemptWriteFence,
        ended_at: str,
        attempt_status: Literal["failed", "lease_expired"],
        lease_status: Literal["closed", "expired"],
        failure_class: str,
        failure_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        retry_decision: RetryDecisionV1,
        events: tuple[RunEvent, ...],
    ) -> RunAttemptClose:
        ended = _require_canonical_utc(ended_at, field_name="ended_at")
        attempt_cassette_id = _require_optional_nonempty(
            attempt_cassette_artifact_id,
            field_name="attempt_cassette_artifact_id",
        )
        decision = _revalidate(
            retry_decision,
            RetryDecisionV1,
            label="Run retry decision",
        )
        if decision.decision != "retry" or decision.retry_not_before_utc is None:
            raise IntegrityViolation("retry close requires a scheduled retry decision")
        if decision.failure_class != failure_class:
            raise IntegrityViolation("retry decision failure class differs from attempt closure")
        retry_not_before = _require_canonical_utc(
            decision.retry_not_before_utc,
            field_name="retry_not_before_utc",
        )
        if retry_not_before < ended:
            raise IntegrityViolation("retry not-before cannot precede attempt closure")
        if (attempt_status == "lease_expired") != (lease_status == "expired"):
            raise IntegrityViolation("lease-expired attempt and lease status must agree")
        parsed_events = tuple(
            _revalidate(event, RunEvent, label="Run retry event") for event in events
        )
        run, attempt, lease = self._load_fenced_attempt(
            fence,
            allowed_statuses=_ACTIVE_RUN_STATUSES,
            occurred_at=ended_at,
            allow_expired_lease=attempt_status == "lease_expired",
            allow_deadline_exceeded=attempt_status == "lease_expired",
        )
        _validate_cassette_publication(
            run,
            attempt_cassette_artifact_id=attempt_cassette_id,
            terminal_cassette_artifact_id=None,
            closes_attempt=True,
            closes_run=False,
        )
        if lease_status == "expired" and ended < _parse_utc(
            lease.expires_at,
            field_name="lease.expires_at",
        ):
            raise IntegrityViolation("lease cannot be marked expired before its expiry")
        self._validate_event_sequence(run, parsed_events)
        if (
            not parsed_events
            or parsed_events[-1].event_type != "attempt.retry_scheduled"
            or any(event.occurred_at != ended_at for event in parsed_events)
            or getattr(parsed_events[-1].data, "failure_artifact_id", None) != failure_artifact_id
        ):
            raise IntegrityViolation("retry close must end with attempt.retry_scheduled")
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": "retry_wait",
                "revision": run.revision + 1,
                "current_attempt_no": None,
                "next_event_seq": run.next_event_seq + len(parsed_events),
                "concurrency_permit_group_id": None,
                "retry_not_before_utc": decision.retry_not_before_utc,
                "updated_at": ended_at,
            }
        )
        updated_attempt = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "status": attempt_status,
                "ended_at": ended_at,
                "failure_class": _require_nonempty(
                    failure_class,
                    field_name="failure_class",
                ),
                "retryable": True,
                "failure_artifact_id": _require_nonempty(
                    failure_artifact_id,
                    field_name="failure_artifact_id",
                ),
                "cassette_bundle_artifact_id": attempt_cassette_id,
            }
        )
        updated_lease = RunLease.model_validate(
            {**lease.model_dump(mode="python"), "status": lease_status}
        )
        self._close_active_attempt(
            run=run,
            attempt=attempt,
            lease=lease,
            updated_run=updated_run,
            updated_attempt=updated_attempt,
            lease_status=lease_status,
            released_at=ended_at,
        )
        for event in parsed_events:
            self._session.add(RunEventRow(**_event_values(event)))
        self._session.flush()
        self._reset_claimed_commands_for_retry(fence)
        self._session.flush()
        return RunAttemptClose(updated_run, updated_attempt, updated_lease, parsed_events)

    def complete_attempt_success(
        self,
        *,
        fence: _AttemptWriteFence,
        ended_at: str,
        result_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        terminal_cassette_artifact_id: str | None,
        event: RunEvent,
    ) -> RunTerminal:
        _require_canonical_utc(ended_at, field_name="ended_at")
        artifact_id = _require_nonempty(
            result_artifact_id,
            field_name="result_artifact_id",
        )
        attempt_cassette_id = _require_optional_nonempty(
            attempt_cassette_artifact_id,
            field_name="attempt_cassette_artifact_id",
        )
        terminal_cassette_id = _require_optional_nonempty(
            terminal_cassette_artifact_id,
            field_name="terminal_cassette_artifact_id",
        )
        parsed_event = _revalidate(event, RunEvent, label="Run success event")
        run, attempt, lease = self._load_fenced_attempt(
            fence,
            allowed_statuses=frozenset({"running"}),
            occurred_at=ended_at,
        )
        _validate_cassette_publication(
            run,
            attempt_cassette_artifact_id=attempt_cassette_id,
            terminal_cassette_artifact_id=terminal_cassette_id,
            closes_attempt=True,
            closes_run=True,
        )
        self._validate_event_sequence(run, (parsed_event,))
        if (
            parsed_event.event_type != "run.succeeded"
            or parsed_event.attempt_no != attempt.attempt_no
            or parsed_event.trace_id != attempt.trace_id
            or parsed_event.occurred_at != ended_at
            or getattr(parsed_event.data, "result_artifact_id", None) != artifact_id
        ):
            raise IntegrityViolation("Run success event differs from its attempt outcome")
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": "succeeded",
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + 1,
                "concurrency_permit_group_id": None,
                "result_artifact_id": artifact_id,
                "terminal_cassette_artifact_id": terminal_cassette_id,
                "updated_at": ended_at,
            }
        )
        updated_attempt = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "status": "succeeded",
                "ended_at": ended_at,
                "cassette_bundle_artifact_id": attempt_cassette_id,
            }
        )
        updated_lease = RunLease.model_validate(
            {**lease.model_dump(mode="python"), "status": "closed"}
        )
        self._close_active_attempt(
            run=run,
            attempt=attempt,
            lease=lease,
            updated_run=updated_run,
            updated_attempt=updated_attempt,
            lease_status="closed",
            released_at=ended_at,
        )
        self._session.add(RunEventRow(**_event_values(parsed_event)))
        self._session.flush()
        self._reject_outstanding_commands(
            run_id=run.run_id,
            event_seq=parsed_event.seq,
            occurred_at=ended_at,
        )
        self._session.flush()
        return RunTerminal(updated_run, updated_attempt, updated_lease, parsed_event)

    def close_attempt_terminal(
        self,
        *,
        fence: _AttemptWriteFence,
        ended_at: str,
        attempt_status: Literal["failed", "cancelled", "timed_out", "lease_expired"],
        lease_status: Literal["closed", "expired"],
        run_status: Literal["failed", "cancelled", "timed_out"],
        failure_class: str,
        attempt_failure_artifact_id: str,
        run_failure_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        terminal_cassette_artifact_id: str | None,
        retry_decision: RetryDecisionV1,
        leading_events: tuple[RunEvent, ...],
        terminal_event: RunEvent,
    ) -> RunTerminal:
        _require_canonical_utc(ended_at, field_name="ended_at")
        attempt_cassette_id = _require_optional_nonempty(
            attempt_cassette_artifact_id,
            field_name="attempt_cassette_artifact_id",
        )
        terminal_cassette_id = _require_optional_nonempty(
            terminal_cassette_artifact_id,
            field_name="terminal_cassette_artifact_id",
        )
        decision = _revalidate(
            retry_decision,
            RetryDecisionV1,
            label="Run terminal decision",
        )
        if decision.decision != "terminal":
            raise IntegrityViolation("terminal attempt close requires a terminal decision")
        if decision.failure_class != failure_class:
            raise IntegrityViolation("terminal decision failure class differs from attempt closure")
        if attempt_status == "lease_expired" and lease_status != "expired":
            raise IntegrityViolation("lease-expired attempt requires an expired lease")
        events = tuple(
            _revalidate(event, RunEvent, label="Run terminal leading event")
            for event in leading_events
        ) + (_revalidate(terminal_event, RunEvent, label="Run terminal event"),)
        run, attempt, lease = self._load_fenced_attempt(
            fence,
            allowed_statuses=_ACTIVE_RUN_STATUSES,
            occurred_at=ended_at,
            allow_expired_lease=lease_status == "expired" or attempt_status == "timed_out",
            allow_deadline_exceeded=(
                lease_status == "expired"
                or attempt_status == "timed_out"
                or decision.reason_code
                in {"attempt_deadline_exhausted", "overall_deadline_exhausted"}
            ),
        )
        _validate_cassette_publication(
            run,
            attempt_cassette_artifact_id=attempt_cassette_id,
            terminal_cassette_artifact_id=terminal_cassette_id,
            closes_attempt=True,
            closes_run=True,
        )
        if lease_status == "expired" and _parse_utc(
            ended_at,
            field_name="ended_at",
        ) < _parse_utc(lease.expires_at, field_name="lease.expires_at"):
            raise IntegrityViolation("lease cannot be marked expired before its expiry")
        self._validate_event_sequence(run, events)
        parsed_terminal = events[-1]
        if (
            parsed_terminal.event_type != f"run.{run_status}"
            or parsed_terminal.occurred_at != ended_at
            or any(event.occurred_at != ended_at for event in events)
            or getattr(parsed_terminal.data, "failure_artifact_id", None) != run_failure_artifact_id
        ):
            raise IntegrityViolation("terminal event differs from the requested Run status")
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": run_status,
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + len(events),
                "concurrency_permit_group_id": None,
                "retry_not_before_utc": None,
                "failure_artifact_id": _require_nonempty(
                    run_failure_artifact_id,
                    field_name="run_failure_artifact_id",
                ),
                "terminal_cassette_artifact_id": terminal_cassette_id,
                "updated_at": ended_at,
            }
        )
        updated_attempt = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "status": attempt_status,
                "ended_at": ended_at,
                "failure_class": _require_nonempty(
                    failure_class,
                    field_name="failure_class",
                ),
                "retryable": False,
                "failure_artifact_id": _require_nonempty(
                    attempt_failure_artifact_id,
                    field_name="attempt_failure_artifact_id",
                ),
                "cassette_bundle_artifact_id": attempt_cassette_id,
            }
        )
        updated_lease = RunLease.model_validate(
            {**lease.model_dump(mode="python"), "status": lease_status}
        )
        self._close_active_attempt(
            run=run,
            attempt=attempt,
            lease=lease,
            updated_run=updated_run,
            updated_attempt=updated_attempt,
            lease_status=lease_status,
            released_at=ended_at,
        )
        for selected_event in events:
            self._session.add(RunEventRow(**_event_values(selected_event)))
        self._session.flush()
        self._reject_outstanding_commands(
            run_id=run.run_id,
            event_seq=parsed_terminal.seq,
            occurred_at=ended_at,
        )
        self._session.flush()
        return RunTerminal(updated_run, updated_attempt, updated_lease, parsed_terminal)

    def terminate_inactive_run(
        self,
        *,
        run_id: str,
        expected_run_revision: int,
        run_status: Literal["failed", "cancelled", "timed_out"],
        failure_artifact_id: str,
        terminal_cassette_artifact_id: str | None,
        retry_decision: RetryDecisionV1,
        event: RunEvent,
    ) -> RunTerminal:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        expected_revision = _require_positive(
            expected_run_revision,
            field_name="expected_run_revision",
        )
        terminal_cassette_id = _require_optional_nonempty(
            terminal_cassette_artifact_id,
            field_name="terminal_cassette_artifact_id",
        )
        decision = _revalidate(
            retry_decision,
            RetryDecisionV1,
            label="Run inactive terminal decision",
        )
        parsed_event = _revalidate(event, RunEvent, label="Run inactive terminal event")
        _require_canonical_utc(parsed_event.occurred_at, field_name="event.occurred_at")
        run = self.get(selected_run_id)
        if (
            run is None
            or run.revision != expected_revision
            or run.status not in {"queued", "retry_wait"}
            or run.current_attempt_no is not None
            or run.concurrency_permit_group_id is not None
            or self.get_current_lease(selected_run_id) is not None
            or decision.decision != "terminal"
        ):
            raise Conflict("inactive Run terminal compare-and-set did not match")
        _validate_cassette_publication(
            run,
            attempt_cassette_artifact_id=None,
            terminal_cassette_artifact_id=terminal_cassette_id,
            closes_attempt=False,
            closes_run=True,
        )
        self._validate_event_sequence(run, (parsed_event,))
        if (
            parsed_event.event_type != f"run.{run_status}"
            or getattr(parsed_event.data, "failure_artifact_id", None) != failure_artifact_id
        ):
            raise IntegrityViolation("inactive terminal event differs from requested Run status")
        artifact_id = _require_nonempty(
            failure_artifact_id,
            field_name="failure_artifact_id",
        )
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": run_status,
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + 1,
                "retry_not_before_utc": None,
                "failure_artifact_id": artifact_id,
                "terminal_cassette_artifact_id": terminal_cassette_id,
                "updated_at": parsed_event.occurred_at,
            }
        )
        result = self._session.execute(
            update(RunRow)
            .where(
                *self._run_fence_predicates(run),
                RunRow.terminal_cassette_artifact_id.is_(None),
            )
            .values(
                status=run_status,
                revision=updated_run.revision,
                next_event_seq=updated_run.next_event_seq,
                retry_not_before_utc=None,
                failure_artifact_id=artifact_id,
                terminal_cassette_artifact_id=terminal_cassette_id,
                updated_at=parsed_event.occurred_at,
            )
        )
        if result.rowcount != 1:
            raise Conflict("inactive Run terminal CAS did not match")
        self._session.add(RunEventRow(**_event_values(parsed_event)))
        self._session.flush()
        self._reject_outstanding_commands(
            run_id=run.run_id,
            event_seq=parsed_event.seq,
            occurred_at=parsed_event.occurred_at,
        )
        self._session.flush()
        latest_attempt = (
            self.get_attempt(run.run_id, run.next_attempt_no - 1)
            if run.next_attempt_no > 1
            else None
        )
        return RunTerminal(updated_run, latest_attempt, None, parsed_event)

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
        rows = (
            self._session.execute(
                select(RunLeaseRow)
                .where(
                    RunLeaseRow.run_id == selected_run_id,
                    RunLeaseRow.status == "active",
                )
                .limit(2)
            )
            .scalars()
            .all()
        )
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

    def get_run_projection(self, run_id: str) -> RunRecord | None:
        """Load a Run for a read projection that tolerates a retention-pruned event log.

        Identical to :meth:`get` except it does NOT assert the write-side event-head
        contiguity invariant (:meth:`_verify_run_heads`). Event retention legitimately
        removes the oldest events, so a resumable-read consumer (e.g. the SSE stream)
        must be able to load the Run and derive its scope even when its earliest events
        have been pruned. The non-event execution-state invariants still hold.
        """

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        row = self._session.get(RunRow, selected_run_id)
        if row is None:
            return None
        run = _parse_run_row(row, expected_run_id=selected_run_id)
        self._verify_run_state(run)
        return run

    def earliest_event_seq(self, run_id: str) -> int | None:
        """Return MIN(seq) of the Run's retained events, or None when none remain.

        Derived from the actual event store — the earliest retained cursor for
        resumable reads — never a speculative retention column.
        """

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        earliest = self._session.execute(
            select(func.min(RunEventRow.seq)).where(RunEventRow.run_id == selected_run_id)
        ).scalar_one_or_none()
        return int(earliest) if earliest is not None else None

    def stream_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> tuple[RunEvent, ...]:
        """Bounded, ordered read of retained events after ``after_seq``.

        Like :meth:`list_events` but tolerant of a retention-pruned prefix: it reads
        the same ``RunEventRow`` store with the same canonical parser and never asserts
        head contiguity, so a start-gap left by retention yields a bounded page rather
        than an integrity failure. ``seq`` is always the persisted seq.
        """

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise IntegrityViolation("after_seq must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
            raise IntegrityViolation("event limit must be between 1 and 1024")
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
        self._session.add(RunIntermediateArtifactLinkRow(**parsed.model_dump(mode="json")))
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

    def accept_command(
        self,
        *,
        expected_run_revision: int,
        record: RunCommandRecordV1,
        events: tuple[RunEvent, ...],
        terminal_status: Literal["cancelled"] | None = None,
        terminal_failure_artifact_id: str | None = None,
        terminal_cassette_artifact_id: str | None = None,
    ) -> RunCommandAcceptance:
        expected_revision = _require_positive(
            expected_run_revision,
            field_name="expected_run_revision",
        )
        terminal_cassette_id = _require_optional_nonempty(
            terminal_cassette_artifact_id,
            field_name="terminal_cassette_artifact_id",
        )
        parsed_record = _revalidate(
            record,
            RunCommandRecordV1,
            label="Run command acceptance",
        )
        parsed_events = tuple(
            _revalidate(event, RunEvent, label="Run command event") for event in events
        )
        run = self.get(parsed_record.run_id)
        if run is None or run.revision != expected_revision:
            raise Conflict("Run command acceptance revision did not match")
        if parsed_record.command.expected_run_revision != expected_revision:
            raise IntegrityViolation("Run command embeds a different expected Run revision")
        if self.get_command(parsed_record.run_id, parsed_record.command.command_id) is not None:
            raise Conflict("Run command identity is already retained")
        if (
            self.get_command_by_idempotency(
                run_id=parsed_record.run_id,
                idempotency_key=parsed_record.command.idempotency_key,
            )
            is not None
        ):
            raise Conflict("Run command idempotency key is already retained")
        if (
            self.get_command_by_client_sequence(
                run_id=parsed_record.run_id,
                client_id=parsed_record.command.client_id,
                client_seq=parsed_record.command.client_seq,
            )
            is not None
        ):
            raise Conflict("Run command client sequence is already retained")
        self._validate_event_sequence(run, parsed_events)
        if not parsed_events:
            raise IntegrityViolation("Run command acceptance requires an event")
        if parsed_record.command.type == "provide_input":
            if (
                parsed_record.status != "pending"
                or terminal_status is not None
                or len(parsed_events) != 1
                or parsed_events[0].event_type != "run.command_accepted"
                or run.status not in _ACTIVE_RUN_STATUSES
                or getattr(parsed_events[0].data, "command_id", None)
                != parsed_record.command.command_id
                or getattr(parsed_events[0].data, "command_revision", None)
                != parsed_record.revision
            ):
                raise IntegrityViolation("provide-input command acceptance shape is invalid")
        else:
            if (
                parsed_record.status != "applied"
                or parsed_record.result_event_seq != parsed_events[0].seq
                or parsed_events[0].event_type != "run.cancel_requested"
                or getattr(parsed_events[0].data, "command_id", None)
                != parsed_record.command.command_id
                or getattr(parsed_events[0].data, "reason_code", None)
                != getattr(parsed_record.command.payload, "reason_code", None)
            ):
                raise IntegrityViolation("cancel command acceptance shape is invalid")
            if terminal_status is None:
                if len(parsed_events) != 1 or run.status not in _ACTIVE_RUN_STATUSES:
                    raise IntegrityViolation("active cancel acceptance shape is invalid")
            else:
                expected_attempt_no = (
                    run.next_attempt_no - 1 if run.status == "retry_wait" else None
                )
                terminal_event = parsed_events[-1]
                if (
                    run.status not in {"queued", "retry_wait"}
                    or len(parsed_events) != 2
                    or terminal_event.event_type != "run.cancelled"
                    or terminal_event.attempt_no is not None
                    or getattr(terminal_event.data, "attempt_no", None) != expected_attempt_no
                    or getattr(terminal_event.data, "failure_artifact_id", None)
                    != terminal_failure_artifact_id
                    or getattr(terminal_event.data, "cause_code", None) != "cancelled"
                ):
                    raise IntegrityViolation("inactive cancel acceptance shape is invalid")

        updates: dict[str, Any] = {
            "revision": run.revision + 1,
            "next_event_seq": run.next_event_seq + len(parsed_events),
            "updated_at": parsed_events[-1].occurred_at,
        }
        if parsed_record.command.type == "cancel":
            updates.update(
                cancel_requested_at=parsed_events[0].occurred_at,
                cancel_requested_by=parsed_record.actor.model_dump(mode="json"),
            )
        if terminal_status is not None:
            artifact_id = _require_nonempty(
                terminal_failure_artifact_id,
                field_name="terminal_failure_artifact_id",
            )
            _validate_cassette_publication(
                run,
                attempt_cassette_artifact_id=None,
                terminal_cassette_artifact_id=terminal_cassette_id,
                closes_attempt=False,
                closes_run=True,
            )
            updates.update(
                status=terminal_status,
                failure_artifact_id=artifact_id,
                terminal_cassette_artifact_id=terminal_cassette_id,
                retry_not_before_utc=None,
                concurrency_permit_group_id=None,
            )
        elif terminal_failure_artifact_id is not None:
            raise IntegrityViolation("nonterminal command cannot publish a failure artifact")
        elif terminal_cassette_id is not None:
            raise IntegrityViolation("nonterminal command cannot publish a cassette")
        updated_run = RunRecord.model_validate({**run.model_dump(mode="python"), **updates})
        run_predicates = self._run_fence_predicates(run)
        if terminal_status is not None:
            run_predicates = (
                *run_predicates,
                RunRow.terminal_cassette_artifact_id.is_(None),
            )
        run_result = self._session.execute(update(RunRow).where(*run_predicates).values(**updates))
        if run_result.rowcount != 1:
            raise Conflict("Run command acceptance Run CAS did not match")
        for parsed_event in parsed_events:
            self._session.add(RunEventRow(**_event_values(parsed_event)))
        self._session.flush()
        self._session.add(RunCommandRow(**_command_values(parsed_record)))
        self._session.flush()
        if terminal_status is not None:
            self._reject_outstanding_commands(
                run_id=run.run_id,
                event_seq=parsed_events[-1].seq,
                occurred_at=parsed_events[-1].occurred_at,
            )
            self._session.flush()
        return RunCommandAcceptance(updated_run, parsed_record, parsed_events)

    def claim_command(
        self,
        *,
        fence: _AttemptWriteFence,
        command_id: str,
        claimed_at: str,
    ) -> RunCommandRecordV1:
        _require_canonical_utc(claimed_at, field_name="claimed_at")
        run, _, _ = self._load_fenced_attempt(
            fence,
            allowed_statuses=frozenset({"running"}),
            occurred_at=claimed_at,
        )
        selected_command_id = _require_nonempty(command_id, field_name="command_id")
        record = self.get_command(run.run_id, selected_command_id)
        if record is None or record.status != "pending":
            raise Conflict("Run command claim target is not pending")
        updated = RunCommandRecordV1.model_validate(
            {
                **record.model_dump(mode="python"),
                "status": "claimed",
                "revision": record.revision + 1,
                "claimed_at": claimed_at,
                "claimed_attempt_no": fence.attempt_no,
                "claimed_fencing_token": fence.fencing_token,
            }
        )
        result = self._session.execute(
            update(RunCommandRow)
            .where(
                RunCommandRow.run_id == run.run_id,
                RunCommandRow.command_id == selected_command_id,
                RunCommandRow.status == "pending",
                RunCommandRow.revision == record.revision,
                RunCommandRow.claimed_at.is_(None),
                RunCommandRow.claimed_attempt_no.is_(None),
                RunCommandRow.claimed_fencing_token.is_(None),
                RunCommandRow.result_event_seq.is_(None),
            )
            .values(
                status="claimed",
                revision=updated.revision,
                claimed_at=claimed_at,
                claimed_attempt_no=fence.attempt_no,
                claimed_fencing_token=fence.fencing_token,
            )
        )
        if result.rowcount != 1:
            raise Conflict("Run command claim CAS did not match")
        self._session.flush()
        return updated

    def complete_command(
        self,
        *,
        fence: _AttemptWriteFence,
        command_id: str,
        expected_command_revision: int,
        outcome: Literal["applied", "rejected"],
        outcome_code: str,
        occurred_at: str,
        event: RunEvent,
    ) -> RunCommandRecordV1:
        _require_canonical_utc(occurred_at, field_name="occurred_at")
        expected_revision = _require_positive(
            expected_command_revision,
            field_name="expected_command_revision",
        )
        selected_outcome_code = _require_nonempty(
            outcome_code,
            field_name="outcome_code",
        )
        parsed_event = _revalidate(event, RunEvent, label="Run command outcome event")
        run, attempt, _ = self._load_fenced_attempt(
            fence,
            allowed_statuses=frozenset({"running"}),
            occurred_at=occurred_at,
        )
        self._validate_event_sequence(run, (parsed_event,))
        selected_command_id = _require_nonempty(command_id, field_name="command_id")
        record = self.get_command(run.run_id, selected_command_id)
        if (
            record is None
            or record.status != "claimed"
            or record.revision != expected_revision
            or record.claimed_attempt_no != fence.attempt_no
            or record.claimed_fencing_token != fence.fencing_token
        ):
            raise Conflict("Run command completion fence did not match")
        expected_event_type = (
            "run.command_applied" if outcome == "applied" else "run.command_rejected"
        )
        if (
            parsed_event.event_type != expected_event_type
            or parsed_event.attempt_no != attempt.attempt_no
            or parsed_event.occurred_at != occurred_at
            or parsed_event.trace_id != attempt.trace_id
            or getattr(parsed_event.data, "command_id", None) != selected_command_id
            or getattr(parsed_event.data, "command_revision", None) != record.revision + 1
            or getattr(parsed_event.data, "outcome_code", None) != selected_outcome_code
        ):
            raise IntegrityViolation("Run command outcome event differs from its attempt")
        updated_record = RunCommandRecordV1.model_validate(
            {
                **record.model_dump(mode="python"),
                "status": outcome,
                "revision": record.revision + 1,
                "applied_at": occurred_at,
                "result_event_seq": parsed_event.seq,
                "rejection_code": selected_outcome_code if outcome == "rejected" else None,
            }
        )
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + 1,
                "updated_at": occurred_at,
            }
        )
        run_result = self._session.execute(
            update(RunRow)
            .where(*self._run_fence_predicates(run))
            .values(
                revision=updated_run.revision,
                next_event_seq=updated_run.next_event_seq,
                updated_at=occurred_at,
            )
        )
        if run_result.rowcount != 1:
            raise Conflict("Run command completion Run CAS did not match")
        self._session.add(RunEventRow(**_event_values(parsed_event)))
        self._session.flush()
        command_result = self._session.execute(
            update(RunCommandRow)
            .where(
                RunCommandRow.run_id == run.run_id,
                RunCommandRow.command_id == selected_command_id,
                RunCommandRow.status == "claimed",
                RunCommandRow.revision == expected_revision,
                RunCommandRow.claimed_attempt_no == fence.attempt_no,
                RunCommandRow.claimed_fencing_token == fence.fencing_token,
                RunCommandRow.result_event_seq.is_(None),
            )
            .values(
                status=outcome,
                revision=updated_record.revision,
                applied_at=occurred_at,
                result_event_seq=parsed_event.seq,
                rejection_code=(selected_outcome_code if outcome == "rejected" else None),
            )
        )
        if command_result.rowcount != 1:
            raise Conflict("Run command completion Command CAS did not match")
        self._session.flush()
        return updated_record

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

    def get_command_by_idempotency(
        self,
        *,
        run_id: str,
        idempotency_key: str,
    ) -> RunCommandRecordV1 | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_key = _require_nonempty(idempotency_key, field_name="idempotency_key")
        row = self._session.execute(
            select(RunCommandRow).where(
                RunCommandRow.run_id == selected_run_id,
                RunCommandRow.idempotency_key == selected_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _parse_command_row(
            row,
            expected_run_id=selected_run_id,
            expected_command_id=row.command_id,
        )

    def get_command_by_client_sequence(
        self,
        *,
        run_id: str,
        client_id: str,
        client_seq: int,
    ) -> RunCommandRecordV1 | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_client_id = _require_nonempty(client_id, field_name="client_id")
        selected_client_seq = _require_positive(client_seq, field_name="client_seq")
        row = self._session.execute(
            select(RunCommandRow).where(
                RunCommandRow.run_id == selected_run_id,
                RunCommandRow.client_id == selected_client_id,
                RunCommandRow.client_seq == selected_client_seq,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _parse_command_row(
            row,
            expected_run_id=selected_run_id,
            expected_command_id=row.command_id,
        )

    def _load_fenced_attempt(
        self,
        fence: _AttemptWriteFence,
        *,
        allowed_statuses: frozenset[str],
        occurred_at: str,
        allow_expired_lease: bool = False,
        allow_deadline_exceeded: bool = False,
    ) -> tuple[RunRecord, RunAttempt, RunLease]:
        run_id = _require_nonempty(fence.run_id, field_name="fence.run_id")
        attempt_no = _require_positive(fence.attempt_no, field_name="fence.attempt_no")
        expected_revision = _require_positive(
            fence.expected_run_revision,
            field_name="fence.expected_run_revision",
        )
        lease_id = _require_nonempty(fence.lease_id, field_name="fence.lease_id")
        fencing_token = _require_positive(
            fence.fencing_token,
            field_name="fence.fencing_token",
        )
        occurred = _require_canonical_utc(occurred_at, field_name="occurred_at")
        run = self.get(run_id)
        attempt = self.get_attempt(run_id, attempt_no)
        lease = self.get_current_lease(run_id)
        if (
            run is None
            or attempt is None
            or lease is None
            or run.revision != expected_revision
            or run.status not in allowed_statuses
            or run.current_attempt_no != attempt_no
            or attempt.status != run.status
            or attempt.fencing_token != fencing_token
            or lease.lease_id != lease_id
            or lease.run_id != run_id
            or lease.attempt_no != attempt_no
            or lease.fencing_token != fencing_token
            or lease.owner_principal_id != attempt.worker_principal_id
            or lease.status != "active"
        ):
            raise Conflict("Run attempt write fence did not match")
        if not allow_expired_lease and occurred >= _parse_utc(
            lease.expires_at,
            field_name="lease.expires_at",
        ):
            raise Conflict("Run attempt lease is expired")
        if not allow_deadline_exceeded:
            overall_deadline = _parse_utc(
                run.overall_deadline_utc,
                field_name="run.overall_deadline_utc",
            )
            if occurred >= overall_deadline:
                raise Conflict("Run attempt overall deadline is exhausted")
            if attempt.attempt_deadline_utc is not None and occurred >= _parse_utc(
                attempt.attempt_deadline_utc,
                field_name="attempt.attempt_deadline_utc",
            ):
                raise Conflict("Run attempt deadline is exhausted")
        return run, attempt, lease

    def _run_fence_predicates(self, run: RunRecord) -> tuple[Any, ...]:
        current_attempt = (
            RunRow.current_attempt_no.is_(None)
            if run.current_attempt_no is None
            else RunRow.current_attempt_no == run.current_attempt_no
        )
        retry_not_before = (
            RunRow.retry_not_before_utc.is_(None)
            if run.retry_not_before_utc is None
            else RunRow.retry_not_before_utc == run.retry_not_before_utc
        )
        permit_group = (
            RunRow.concurrency_permit_group_id.is_(None)
            if run.concurrency_permit_group_id is None
            else RunRow.concurrency_permit_group_id == run.concurrency_permit_group_id
        )
        return (
            RunRow.run_id == run.run_id,
            RunRow.revision == run.revision,
            RunRow.status == run.status,
            current_attempt,
            RunRow.next_attempt_no == run.next_attempt_no,
            RunRow.next_fencing_token == run.next_fencing_token,
            RunRow.next_event_seq == run.next_event_seq,
            retry_not_before,
            permit_group,
        )

    def _validate_event_sequence(
        self,
        run: RunRecord,
        events: tuple[RunEvent, ...],
    ) -> None:
        if tuple(event.seq for event in events) != tuple(
            range(run.next_event_seq, run.next_event_seq + len(events))
        ):
            raise Conflict("Run event sequence does not match its persisted head")
        for event in events:
            _require_canonical_utc(event.occurred_at, field_name="event.occurred_at")
            if event.run_id != run.run_id:
                raise IntegrityViolation("Run event belongs to a different Run")
            if event.attempt_no not in {None, run.current_attempt_no}:
                raise IntegrityViolation("Run event belongs to a noncurrent attempt")
            if self._session.get(RunEventRow, (run.run_id, event.seq)) is not None:
                raise Conflict("Run event sequence is already retained", seq=event.seq)

    def _close_active_attempt(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        updated_run: RunRecord,
        updated_attempt: RunAttempt,
        lease_status: Literal["closed", "expired"],
        released_at: str,
    ) -> None:
        run_values = _run_values(updated_run)
        previous_run_values = _run_values(run)
        changed_run_values = {
            key: value for key, value in run_values.items() if previous_run_values[key] != value
        }
        run_result = self._session.execute(
            update(RunRow)
            .where(
                *self._run_fence_predicates(run),
                RunRow.terminal_cassette_artifact_id.is_(None),
            )
            .values(**changed_run_values)
        )
        if run_result.rowcount != 1:
            raise Conflict("Run attempt close Run CAS did not match")
        attempt_values = _attempt_values(updated_attempt)
        previous_attempt_values = _attempt_values(attempt)
        changed_attempt_values = {
            key: value
            for key, value in attempt_values.items()
            if previous_attempt_values[key] != value
        }
        attempt_result = self._session.execute(
            update(RunAttemptRow)
            .where(
                RunAttemptRow.run_id == run.run_id,
                RunAttemptRow.attempt_no == attempt.attempt_no,
                RunAttemptRow.status == attempt.status,
                RunAttemptRow.fencing_token == attempt.fencing_token,
                RunAttemptRow.next_call_ordinal == attempt.next_call_ordinal,
                RunAttemptRow.ended_at.is_(None),
                RunAttemptRow.failure_class.is_(None),
                RunAttemptRow.retryable.is_(None),
                RunAttemptRow.failure_artifact_id.is_(None),
                RunAttemptRow.cassette_bundle_artifact_id.is_(None),
            )
            .values(**changed_attempt_values)
        )
        if attempt_result.rowcount != 1:
            raise Conflict("Run attempt close Attempt CAS did not match")
        lease_result = self._session.execute(
            update(RunLeaseRow)
            .where(
                RunLeaseRow.lease_id == lease.lease_id,
                RunLeaseRow.run_id == run.run_id,
                RunLeaseRow.attempt_no == attempt.attempt_no,
                RunLeaseRow.fencing_token == attempt.fencing_token,
                RunLeaseRow.status == "active",
                RunLeaseRow.released_at.is_(None),
            )
            .values(status=lease_status, released_at=released_at)
        )
        if lease_result.rowcount != 1:
            raise Conflict("Run attempt close Lease CAS did not match")

    def _reset_claimed_commands_for_retry(self, fence: _AttemptWriteFence) -> None:
        rows = self._session.execute(
            select(RunCommandRow).where(
                RunCommandRow.run_id == fence.run_id,
                RunCommandRow.status == "claimed",
            )
        ).scalars()
        for row in rows:
            record = _parse_command_row(
                row,
                expected_run_id=fence.run_id,
                expected_command_id=row.command_id,
            )
            if (
                record.claimed_attempt_no != fence.attempt_no
                or record.claimed_fencing_token != fence.fencing_token
            ):
                raise IntegrityViolation("claimed command is bound to a stale Run attempt")
            result = self._session.execute(
                update(RunCommandRow)
                .where(
                    RunCommandRow.run_id == fence.run_id,
                    RunCommandRow.command_id == row.command_id,
                    RunCommandRow.status == "claimed",
                    RunCommandRow.revision == record.revision,
                    RunCommandRow.claimed_attempt_no == fence.attempt_no,
                    RunCommandRow.claimed_fencing_token == fence.fencing_token,
                )
                .values(
                    status="pending",
                    revision=record.revision + 1,
                    claimed_at=None,
                    claimed_attempt_no=None,
                    claimed_fencing_token=None,
                )
            )
            if result.rowcount != 1:
                raise Conflict("Run retry command reset CAS did not match")

    def _reject_outstanding_commands(
        self,
        *,
        run_id: str,
        event_seq: int,
        occurred_at: str,
    ) -> None:
        rows = tuple(
            self._session.execute(
                select(RunCommandRow).where(
                    RunCommandRow.run_id == run_id,
                    RunCommandRow.status.in_(("pending", "claimed")),
                )
            ).scalars()
        )
        for row in rows:
            record = _parse_command_row(
                row,
                expected_run_id=run_id,
                expected_command_id=row.command_id,
            )
            updated = RunCommandRecordV1.model_validate(
                {
                    **record.model_dump(mode="python"),
                    "status": "rejected",
                    "revision": record.revision + 1,
                    "applied_at": occurred_at,
                    "result_event_seq": event_seq,
                    "rejection_code": "run_terminal",
                }
            )
            result = self._session.execute(
                update(RunCommandRow)
                .where(
                    RunCommandRow.run_id == run_id,
                    RunCommandRow.command_id == row.command_id,
                    RunCommandRow.status == record.status,
                    RunCommandRow.revision == record.revision,
                )
                .values(
                    status="rejected",
                    revision=updated.revision,
                    applied_at=occurred_at,
                    result_event_seq=event_seq,
                    rejection_code="run_terminal",
                )
            )
            if result.rowcount != 1:
                raise Conflict("Run terminal command rejection CAS did not match")

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
            raise IntegrityViolation(
                "Run event head is not the next free sequence", run_id=run.run_id
            )

        future_attempt = self._session.execute(
            select(RunAttemptRow.attempt_no)
            .where(
                RunAttemptRow.run_id == run.run_id,
                RunAttemptRow.attempt_no >= run.next_attempt_no,
            )
            .limit(1)
        ).scalar_one_or_none()
        future_fencing = self._session.execute(
            select(RunAttemptRow.fencing_token)
            .where(
                RunAttemptRow.run_id == run.run_id,
                RunAttemptRow.fencing_token >= run.next_fencing_token,
            )
            .limit(1)
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
            if run.current_attempt_no != run.next_attempt_no - 1 or run.next_fencing_token < 2:
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
            return
        if run.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            if run.concurrency_permit_group_id is not None or active_lease is not None:
                raise IntegrityViolation("terminal Run retains active execution state")
            active_attempt = self._session.execute(
                select(RunAttemptRow.attempt_no)
                .where(
                    RunAttemptRow.run_id == run.run_id,
                    RunAttemptRow.status.in_(_ACTIVE_ATTEMPT_STATUSES),
                )
                .limit(1)
            ).scalar_one_or_none()
            if active_attempt is not None:
                raise IntegrityViolation("terminal Run retains an active Attempt")
            if run.status == "succeeded" and run.current_attempt_no is None:
                raise IntegrityViolation("successful Run does not identify its completed Attempt")
            if run.current_attempt_no is not None:
                if run.current_attempt_no != run.next_attempt_no - 1:
                    raise IntegrityViolation("terminal Run does not point at its latest Attempt")
                attempt = self.get_attempt(run.run_id, run.current_attempt_no)
                if attempt is None or (
                    (run.status == "succeeded") != (attempt.status == "succeeded")
                ):
                    raise IntegrityViolation("terminal Run/Attempt projection is inconsistent")

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
