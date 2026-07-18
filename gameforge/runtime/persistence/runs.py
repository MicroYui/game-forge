"""SQLite persistence for immutable Run inputs and monotonic execution heads."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Literal, Protocol, Sequence, TypeVar
from weakref import WeakKeyDictionary, WeakSet

from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, bindparam, case, delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import typed_canonical_json
from gameforge.contracts.errors import (
    Conflict,
    IdempotencyConflict,
    IntegrityViolation,
    InvalidStateTransition,
)
from gameforge.contracts.findings import FindingRevisionV1, finding_revision_digest
from gameforge.contracts.jobs import (
    AttemptLeasedDataV1,
    AttemptStartedDataV1,
    MAX_COLLECTION_ITEMS,
    MAX_RUN_COMMAND_CLIENT_SEQ,
    MAX_RUN_MANIFEST_PARENT_BINDINGS,
    RetryDecisionV1,
    RunAttempt,
    RunCommandRecordV1,
    RunEvent,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunLease,
    RunModelResponseConsumptionV1,
    RunModelRouteLinkV1,
    RunQueuedDataV1,
    RunRecord,
    RunToolIntermediateLinkV1,
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
    RunModelResponseConsumptionRow,
    RunModelRouteLinkRow,
    RunRow,
    RunToolIntermediateLinkRow,
    ReservationGroupRow,
    UsageEntryRow,
)
from gameforge.runtime.persistence.cost import SqlCostRepository


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_ACTIVE_RUN_STATUSES = frozenset({"leased", "running"})
_ACTIVE_ATTEMPT_STATUSES = frozenset({"leased", "running"})
# SQLite builds before 3.32 commonly cap one statement at 999 bound variables.
# Leave room for LIMIT/OFFSET and future fixed predicates instead of relying on
# the larger limit of the developer machine's bundled SQLite.
_MAX_SQL_IN_ITEMS = 900
_RUN_FINDING_PREFLIGHT_AUTHORITY = object()
_RUN_TERMINAL_PREFLIGHT_AUTHORITY = object()
_TERMINAL_COMMAND_PREFLIGHT_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class _PreflightedRunTerminalClosureState:
    """Complete DML projection retained outside its opaque one-shot handle."""

    owner: SqlRunRepository
    session: Session
    transaction: object
    result: RunAttemptClose | RunTerminal
    run_statement: object
    attempt_statement: object | None
    lease_statement: object | None
    event_parameters: tuple[dict[str, object], ...]
    command_mode: Literal["retry", "terminal"]
    command_statement: object | None
    command_parameters: tuple[dict[str, object], ...]


class _PreflightedRunTerminalClosure:
    """Unforgeable weak key for a transaction-bound lifecycle closure."""

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        *,
        _authority: object,
        _state: _PreflightedRunTerminalClosureState,
    ) -> None:
        if _authority is not _RUN_TERMINAL_PREFLIGHT_AUTHORITY:
            raise IntegrityViolation(
                "Run terminal closure preflight seal does not belong to this repository"
            )
        with _RUN_TERMINAL_PREFLIGHT_LOCK:
            _RUN_TERMINAL_PREFLIGHT_STATES[self] = _state

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("Run terminal closure preflight seal is immutable")


_RUN_TERMINAL_PREFLIGHT_LOCK = Lock()
_RUN_TERMINAL_PREFLIGHT_STATES: WeakKeyDictionary[
    _PreflightedRunTerminalClosure,
    _PreflightedRunTerminalClosureState,
] = WeakKeyDictionary()
_CONSUMED_RUN_TERMINAL_PREFLIGHT_SEALS: WeakSet[_PreflightedRunTerminalClosure] = WeakSet()


@dataclass(frozen=True, slots=True)
class _PreflightedTerminalCommandAcceptanceState:
    owner: SqlRunRepository
    session: Session
    transaction: object
    result: RunCommandAcceptance
    run_statement: object
    event_parameters: tuple[dict[str, object], ...]
    command_parameters: dict[str, object]
    rejection_statement: object | None
    rejection_parameters: tuple[dict[str, object], ...]


class _PreflightedTerminalCommandAcceptance:
    __slots__ = ("__weakref__",)

    def __init__(
        self,
        *,
        _authority: object,
        _state: _PreflightedTerminalCommandAcceptanceState,
    ) -> None:
        if _authority is not _TERMINAL_COMMAND_PREFLIGHT_AUTHORITY:
            raise IntegrityViolation("terminal command preflight seal is not trusted")
        with _TERMINAL_COMMAND_PREFLIGHT_LOCK:
            _TERMINAL_COMMAND_PREFLIGHT_STATES[self] = _state

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("terminal command preflight seal is immutable")


_TERMINAL_COMMAND_PREFLIGHT_LOCK = Lock()
_TERMINAL_COMMAND_PREFLIGHT_STATES: WeakKeyDictionary[
    _PreflightedTerminalCommandAcceptance,
    _PreflightedTerminalCommandAcceptanceState,
] = WeakKeyDictionary()
_CONSUMED_TERMINAL_COMMAND_PREFLIGHT_SEALS: WeakSet[_PreflightedTerminalCommandAcceptance] = (
    WeakSet()
)


@dataclass(frozen=True, slots=True)
class _PreflightedRunFindingLinkState:
    """Opaque, transaction-bound authority for one Finding-link batch."""

    owner: SqlRunRepository
    session: Session
    transaction: object | None
    results: tuple[RunFindingLinkV1, ...]
    row_parameters: tuple[dict[str, object], ...]


class _PreflightedRunFindingLinks:
    """Unforgeable weak key for externally retained Finding-link authority."""

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        *,
        _authority: object,
        _owner: SqlRunRepository,
        _session: Session,
        _transaction: object | None,
        _results: tuple[RunFindingLinkV1, ...],
        _row_parameters: tuple[dict[str, object], ...],
    ) -> None:
        if _authority is not _RUN_FINDING_PREFLIGHT_AUTHORITY:
            raise IntegrityViolation(
                "Run Finding-link preflight seal does not belong to the current transaction"
            )
        state = _PreflightedRunFindingLinkState(
            owner=_owner,
            session=_session,
            transaction=_transaction,
            results=_results,
            row_parameters=_row_parameters,
        )
        with _RUN_FINDING_PREFLIGHT_LOCK:
            _RUN_FINDING_PREFLIGHT_STATES[self] = state

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("Run Finding-link preflight seal is immutable")


_RUN_FINDING_PREFLIGHT_LOCK = Lock()
_RUN_FINDING_PREFLIGHT_STATES: WeakKeyDictionary[
    _PreflightedRunFindingLinks,
    _PreflightedRunFindingLinkState,
] = WeakKeyDictionary()
_CONSUMED_RUN_FINDING_PREFLIGHT_SEALS: WeakSet[_PreflightedRunFindingLinks] = WeakSet()


@dataclass(frozen=True, slots=True)
class RunClaim:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


@dataclass(frozen=True, slots=True)
class ReplayRunAuthorityProjection:
    """Set-based current rows for one replay admission's prevalidated dependencies."""

    runs: dict[str, RunRecord | None]
    attempts: dict[tuple[str, int], RunAttempt | None]
    prompt_links: dict[tuple[str, int, int, int], RunIntermediateArtifactLinkV1 | None]
    model_route_links: dict[tuple[str, int, int, int], RunModelRouteLinkV1 | None]
    model_consumptions: dict[tuple[str, int, int, int], RunModelResponseConsumptionV1 | None]


@dataclass(frozen=True, slots=True)
class TerminalRunAuthorityProjection:
    """Bounded raw rows used only to detect terminal-plan drift under write lock."""

    run: RunRecord
    attempts: tuple[RunAttempt, ...]
    prompt_links: tuple[RunIntermediateArtifactLinkV1, ...]
    tool_links: tuple[RunToolIntermediateLinkV1, ...]
    model_routes: tuple[RunModelRouteLinkV1, ...]
    model_consumptions: tuple[RunModelResponseConsumptionV1, ...]
    closed_attempt_failures: tuple[tuple[int, str], ...]


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


def _require_command_client_sequence(value: object) -> int:
    selected = _require_positive(value, field_name="client_seq")
    if selected > MAX_RUN_COMMAND_CLIENT_SEQ:
        raise IntegrityViolation("client_seq exceeds the durable integer range")
    return selected


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


def _utc_sql_key(value: Any) -> Any:
    """Normalize canonical UTC text to a fixed-width, microsecond-exact key.

    Python's canonical form is either ``...:SSZ`` or ``...:SS.ffffffZ``. Bare
    lexical comparison orders the ``Z`` form after the fractional form, while
    SQLite ``julianday`` rounds sub-millisecond differences. Normalizing the
    zero-fraction form preserves exact chronological ordering for both shapes.
    """

    return case(
        (
            func.length(value) == 20,
            func.printf("%s.000000Z", func.substr(value, 1, 19)),
        ),
        else_=value,
    )


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
        "resource_domain_scope": row.resource_domain_scope,
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
            _require_canonical_utc(getattr(parsed, field_name), field_name=field_name)
        if parsed.cancel_requested_at is not None:
            _require_canonical_utc(
                parsed.cancel_requested_at,
                field_name="cancel_requested_at",
            )
        if parsed.retry_not_before_utc is not None:
            _require_canonical_utc(
                parsed.retry_not_before_utc,
                field_name="retry_not_before_utc",
            )
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
                _require_canonical_utc(value, field_name=field_name)
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
            _require_canonical_utc(getattr(parsed, field_name), field_name=field_name)
        if row.released_at is not None:
            _require_canonical_utc(row.released_at, field_name="released_at")
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
        _require_canonical_utc(parsed.occurred_at, field_name="occurred_at")
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
                _require_canonical_utc(value, field_name=field_name)
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
        "route_ordinal": row.route_ordinal,
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
    expected_route_ordinal: int,
) -> RunIntermediateArtifactLinkV1:
    wire = _intermediate_wire(row)
    try:
        if (
            row.run_id != expected_run_id
            or row.attempt_no != expected_attempt_no
            or row.call_ordinal != expected_call_ordinal
            or row.route_ordinal != expected_route_ordinal
        ):
            raise ValueError("intermediate-link storage key differs from requested identity")
        parsed = RunIntermediateArtifactLinkV1.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("intermediate-link row is not canonical")
        _require_canonical_utc(parsed.published_at, field_name="published_at")
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation(
            "stored RunIntermediateArtifactLink is invalid",
            run_id=expected_run_id,
            attempt_no=expected_attempt_no,
            call_ordinal=expected_call_ordinal,
        ) from exc
    return parsed


def _tool_intermediate_wire(row: RunToolIntermediateLinkRow) -> dict[str, Any]:
    return {
        "link_schema_version": row.link_schema_version,
        "run_id": row.run_id,
        "attempt_no": row.attempt_no,
        "target_call_ordinal": row.target_call_ordinal,
        "artifact_id": row.artifact_id,
        "role": row.role,
        "agent_node_id": row.agent_node_id,
        "prompt_version": row.prompt_version,
        "payload_hash": row.payload_hash,
        "fencing_token": row.fencing_token,
        "published_at": row.published_at,
    }


def _parse_tool_intermediate_row(
    row: RunToolIntermediateLinkRow,
    *,
    expected_run_id: str,
    expected_attempt_no: int,
    expected_target_call_ordinal: int,
) -> RunToolIntermediateLinkV1:
    wire = _tool_intermediate_wire(row)
    try:
        if (
            row.run_id != expected_run_id
            or row.attempt_no != expected_attempt_no
            or row.target_call_ordinal != expected_target_call_ordinal
        ):
            raise ValueError("tool-intermediate storage key differs from requested identity")
        parsed = RunToolIntermediateLinkV1.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("tool-intermediate row is not canonical")
        _require_canonical_utc(parsed.published_at, field_name="published_at")
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation(
            "stored RunToolIntermediateLink is invalid",
            run_id=expected_run_id,
            attempt_no=expected_attempt_no,
            target_call_ordinal=expected_target_call_ordinal,
        ) from exc
    return parsed


def _model_route_wire(row: RunModelRouteLinkRow) -> dict[str, Any]:
    return {
        "link_schema_version": row.link_schema_version,
        "run_id": row.run_id,
        "attempt_no": row.attempt_no,
        "call_ordinal": row.call_ordinal,
        "route_ordinal": row.route_ordinal,
        "prompt_artifact_id": row.prompt_artifact_id,
        "request_hash": row.request_hash,
        "routing_decision_kind": row.routing_decision_kind,
        "routing_decision_id": row.routing_decision_id,
        "fencing_token": row.fencing_token,
        "published_at": row.published_at,
    }


def _parse_model_route_row(row: RunModelRouteLinkRow) -> RunModelRouteLinkV1:
    wire = _model_route_wire(row)
    try:
        parsed = RunModelRouteLinkV1.model_validate(wire)
        expected_native = (
            parsed.routing_decision_id if parsed.routing_decision_kind == "native" else None
        )
        expected_legacy = (
            parsed.routing_decision_id if parsed.routing_decision_kind == "legacy_import" else None
        )
        if (
            _canonical_wire(parsed) != typed_canonical_json(wire)
            or row.native_routing_decision_id != expected_native
            or row.legacy_routing_decision_id != expected_legacy
        ):
            raise ValueError("model-route row is not canonical")
        _require_canonical_utc(parsed.published_at, field_name="published_at")
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation(
            "stored RunModelRouteLink is invalid",
            run_id=row.run_id,
            attempt_no=row.attempt_no,
            call_ordinal=row.call_ordinal,
            route_ordinal=row.route_ordinal,
        ) from exc
    return parsed


def _model_consumption_wire(row: RunModelResponseConsumptionRow) -> dict[str, Any]:
    return {
        "consumption_schema_version": row.consumption_schema_version,
        "run_id": row.run_id,
        "attempt_no": row.attempt_no,
        "call_ordinal": row.call_ordinal,
        "route_ordinal": row.route_ordinal,
        "execution_source": row.execution_source,
        "reservation_group_id": row.reservation_group_id,
        "transport_attempt": row.transport_attempt,
        "cassette_shard_artifact_id": row.cassette_shard_artifact_id,
        "response_digest": row.response_digest,
        "consumed_at": row.consumed_at,
    }


def _parse_model_consumption_row(
    row: RunModelResponseConsumptionRow,
) -> RunModelResponseConsumptionV1:
    wire = _model_consumption_wire(row)
    try:
        parsed = RunModelResponseConsumptionV1.model_validate(wire)
        if _canonical_wire(parsed) != typed_canonical_json(wire):
            raise ValueError("model-response-consumption row is not canonical")
        _require_canonical_utc(parsed.consumed_at, field_name="consumed_at")
    except (TypeError, ValueError, ValidationError, IntegrityViolation) as exc:
        raise IntegrityViolation(
            "stored RunModelResponseConsumption is invalid",
            run_id=row.run_id,
            attempt_no=row.attempt_no,
            call_ordinal=row.call_ordinal,
            route_ordinal=row.route_ordinal,
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
        _require_canonical_utc(parsed.created_at, field_name="created_at")
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

    def replay_authority_projection(
        self,
        *,
        run_ids: Sequence[str],
        attempt_keys: Sequence[tuple[str, int]],
        prompt_link_keys: Sequence[tuple[str, int, int, int]],
        model_route_keys: Sequence[tuple[str, int, int, int]],
        model_consumption_keys: Sequence[tuple[str, int, int, int]],
    ) -> ReplayRunAuthorityProjection:
        """Read prevalidated replay rows in five set-based statements.

        Full semantic validation happens before the write transaction. This projection
        only detects a row change while the SQLite writer is held, so it deliberately
        avoids recursively reopening each link's already-proved authority closure.
        """

        selected_run_ids = tuple(
            dict.fromkeys(
                (
                    *run_ids,
                    *(key[0] for key in attempt_keys),
                    *(key[0] for key in prompt_link_keys),
                    *(key[0] for key in model_route_keys),
                    *(key[0] for key in model_consumption_keys),
                )
            )
        )
        if any(not isinstance(run_id, str) or not run_id for run_id in selected_run_ids):
            raise ValueError("replay authority run ids must be non-empty strings")
        attempt_key_set = set(attempt_keys)
        prompt_key_set = set(prompt_link_keys)
        route_key_set = set(model_route_keys)
        consumption_key_set = set(model_consumption_keys)

        run_values: dict[str, RunRecord] = {}
        attempt_values: dict[tuple[str, int], RunAttempt] = {}
        prompt_values: dict[tuple[str, int, int, int], RunIntermediateArtifactLinkV1] = {}
        route_values: dict[tuple[str, int, int, int], RunModelRouteLinkV1] = {}
        consumption_values: dict[tuple[str, int, int, int], RunModelResponseConsumptionV1] = {}
        if selected_run_ids:
            for row in self._session.scalars(
                select(RunRow).where(RunRow.run_id.in_(selected_run_ids))
            ).all():
                run_values[row.run_id] = _parse_run_row(row, expected_run_id=row.run_id)
            for row in self._session.scalars(
                select(RunAttemptRow).where(RunAttemptRow.run_id.in_(selected_run_ids))
            ).all():
                key = (row.run_id, row.attempt_no)
                if key in attempt_key_set:
                    attempt_values[key] = _parse_attempt_row(
                        row,
                        expected_run_id=row.run_id,
                        expected_attempt_no=row.attempt_no,
                    )
            for row in self._session.scalars(
                select(RunIntermediateArtifactLinkRow).where(
                    RunIntermediateArtifactLinkRow.run_id.in_(selected_run_ids)
                )
            ).all():
                key = (row.run_id, row.attempt_no, row.call_ordinal, row.route_ordinal)
                if key in prompt_key_set:
                    prompt_values[key] = _parse_intermediate_row(
                        row,
                        expected_run_id=row.run_id,
                        expected_attempt_no=row.attempt_no,
                        expected_call_ordinal=row.call_ordinal,
                        expected_route_ordinal=row.route_ordinal,
                    )
            for row in self._session.scalars(
                select(RunModelRouteLinkRow).where(
                    RunModelRouteLinkRow.run_id.in_(selected_run_ids)
                )
            ).all():
                key = (row.run_id, row.attempt_no, row.call_ordinal, row.route_ordinal)
                if key in route_key_set:
                    route_values[key] = _parse_model_route_row(row)
            for row in self._session.scalars(
                select(RunModelResponseConsumptionRow).where(
                    RunModelResponseConsumptionRow.run_id.in_(selected_run_ids)
                )
            ).all():
                key = (row.run_id, row.attempt_no, row.call_ordinal, row.route_ordinal)
                if key in consumption_key_set:
                    consumption_values[key] = _parse_model_consumption_row(row)

        return ReplayRunAuthorityProjection(
            runs={run_id: run_values.get(run_id) for run_id in dict.fromkeys(run_ids)},
            attempts={key: attempt_values.get(key) for key in dict.fromkeys(attempt_keys)},
            prompt_links={key: prompt_values.get(key) for key in dict.fromkeys(prompt_link_keys)},
            model_route_links={
                key: route_values.get(key) for key in dict.fromkeys(model_route_keys)
            },
            model_consumptions={
                key: consumption_values.get(key) for key in dict.fromkeys(model_consumption_keys)
            },
        )

    def terminal_authority_projection(
        self,
        run_id: str,
        *,
        limit: int = MAX_RUN_MANIFEST_PARENT_BINDINGS,
    ) -> TerminalRunAuthorityProjection:
        """Read terminal drift facts in bounded set queries without recursive closure.

        The read-phase publisher already performed the expensive Artifact/cost/blob
        validation.  Under ``BEGIN IMMEDIATE`` this method only reproduces the exact
        mutable rows whose change would invalidate that immutable plan.
        """

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RUN_MANIFEST_PARENT_BINDINGS
        ):
            raise IntegrityViolation("terminal authority limit exceeds the runtime hard cap")
        run_row = self._session.get(RunRow, selected_run_id)
        if run_row is None:
            raise IntegrityViolation("terminal authority Run is unavailable", run_id=run_id)
        run = _parse_run_row(run_row, expected_run_id=selected_run_id)

        def bounded_rows(model: type[Any], *order: Any) -> list[Any]:
            rows = self._session.scalars(
                select(model)
                .where(model.run_id == selected_run_id)
                .order_by(*order)
                .limit(limit + 1)
            ).all()
            if len(rows) > limit:
                raise IntegrityViolation(
                    "terminal runtime authority exceeds its hard cap",
                    run_id=selected_run_id,
                    authority=model.__tablename__,
                )
            return list(rows)

        attempt_rows = bounded_rows(RunAttemptRow, RunAttemptRow.attempt_no)
        prompt_rows = bounded_rows(
            RunIntermediateArtifactLinkRow,
            RunIntermediateArtifactLinkRow.attempt_no,
            RunIntermediateArtifactLinkRow.call_ordinal,
            RunIntermediateArtifactLinkRow.route_ordinal,
        )
        tool_rows = bounded_rows(
            RunToolIntermediateLinkRow,
            RunToolIntermediateLinkRow.attempt_no,
            RunToolIntermediateLinkRow.target_call_ordinal,
        )
        route_rows = bounded_rows(
            RunModelRouteLinkRow,
            RunModelRouteLinkRow.attempt_no,
            RunModelRouteLinkRow.call_ordinal,
            RunModelRouteLinkRow.route_ordinal,
        )
        consumption_rows = bounded_rows(
            RunModelResponseConsumptionRow,
            RunModelResponseConsumptionRow.attempt_no,
            RunModelResponseConsumptionRow.call_ordinal,
            RunModelResponseConsumptionRow.route_ordinal,
        )
        attempts = tuple(
            _parse_attempt_row(
                row,
                expected_run_id=selected_run_id,
                expected_attempt_no=row.attempt_no,
            )
            for row in attempt_rows
        )
        return TerminalRunAuthorityProjection(
            run=run,
            attempts=attempts,
            prompt_links=tuple(
                _parse_intermediate_row(
                    row,
                    expected_run_id=selected_run_id,
                    expected_attempt_no=row.attempt_no,
                    expected_call_ordinal=row.call_ordinal,
                    expected_route_ordinal=row.route_ordinal,
                )
                for row in prompt_rows
            ),
            tool_links=tuple(
                _parse_tool_intermediate_row(
                    row,
                    expected_run_id=selected_run_id,
                    expected_attempt_no=row.attempt_no,
                    expected_target_call_ordinal=row.target_call_ordinal,
                )
                for row in tool_rows
            ),
            model_routes=tuple(_parse_model_route_row(row) for row in route_rows),
            model_consumptions=tuple(_parse_model_consumption_row(row) for row in consumption_rows),
            closed_attempt_failures=tuple(
                (attempt.attempt_no, attempt.failure_artifact_id)
                for attempt in attempts
                if attempt.failure_artifact_id is not None
            ),
        )

    def terminal_attempt_authority_projection(
        self,
        run_id: str,
        *,
        limit: int = MAX_RUN_MANIFEST_PARENT_BINDINGS,
    ) -> TerminalRunAuthorityProjection:
        """Project only authority mutable for the current retry attempt.

        Closed Attempts and their runtime links are immutable publication inputs.
        Re-reading them for every retry makes total writer-lock work quadratic in
        retry history.  The Run head fences attempt allocation; this projection
        therefore covers the current Attempt plus every current-attempt link whose
        append does not necessarily advance that head (fallback routes, tool links,
        model-route bindings, and response consumptions).
        """

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RUN_MANIFEST_PARENT_BINDINGS
        ):
            raise IntegrityViolation("terminal authority limit exceeds the runtime hard cap")
        run_row = self._session.get(RunRow, selected_run_id)
        if run_row is None:
            raise IntegrityViolation("terminal authority Run is unavailable", run_id=run_id)
        run = _parse_run_row(run_row, expected_run_id=selected_run_id)
        attempt_no = run.current_attempt_no
        if attempt_no is None:
            return TerminalRunAuthorityProjection(
                run=run,
                attempts=(),
                prompt_links=(),
                tool_links=(),
                model_routes=(),
                model_consumptions=(),
                closed_attempt_failures=(),
            )
        attempt_row = self._session.get(RunAttemptRow, (selected_run_id, attempt_no))
        if attempt_row is None:
            raise IntegrityViolation(
                "terminal authority current Attempt is unavailable",
                run_id=run_id,
                attempt_no=attempt_no,
            )
        attempt = _parse_attempt_row(
            attempt_row,
            expected_run_id=selected_run_id,
            expected_attempt_no=attempt_no,
        )

        def bounded_current_rows(model: type[Any], *order: Any) -> list[Any]:
            rows = self._session.scalars(
                select(model)
                .where(
                    model.run_id == selected_run_id,
                    model.attempt_no == attempt_no,
                )
                .order_by(*order)
                .limit(limit + 1)
            ).all()
            if len(rows) > limit:
                raise IntegrityViolation(
                    "terminal current-attempt authority exceeds its hard cap",
                    run_id=selected_run_id,
                    attempt_no=attempt_no,
                    authority=model.__tablename__,
                )
            return list(rows)

        prompt_rows = bounded_current_rows(
            RunIntermediateArtifactLinkRow,
            RunIntermediateArtifactLinkRow.call_ordinal,
            RunIntermediateArtifactLinkRow.route_ordinal,
        )
        tool_rows = bounded_current_rows(
            RunToolIntermediateLinkRow,
            RunToolIntermediateLinkRow.target_call_ordinal,
        )
        route_rows = bounded_current_rows(
            RunModelRouteLinkRow,
            RunModelRouteLinkRow.call_ordinal,
            RunModelRouteLinkRow.route_ordinal,
        )
        consumption_rows = bounded_current_rows(
            RunModelResponseConsumptionRow,
            RunModelResponseConsumptionRow.call_ordinal,
            RunModelResponseConsumptionRow.route_ordinal,
        )
        return TerminalRunAuthorityProjection(
            run=run,
            attempts=(attempt,),
            prompt_links=tuple(
                _parse_intermediate_row(
                    row,
                    expected_run_id=selected_run_id,
                    expected_attempt_no=attempt_no,
                    expected_call_ordinal=row.call_ordinal,
                    expected_route_ordinal=row.route_ordinal,
                )
                for row in prompt_rows
            ),
            tool_links=tuple(
                _parse_tool_intermediate_row(
                    row,
                    expected_run_id=selected_run_id,
                    expected_attempt_no=attempt_no,
                    expected_target_call_ordinal=row.target_call_ordinal,
                )
                for row in tool_rows
            ),
            model_routes=tuple(_parse_model_route_row(row) for row in route_rows),
            model_consumptions=tuple(_parse_model_consumption_row(row) for row in consumption_rows),
            closed_attempt_failures=(),
        )

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
            if retained.resource_domain_scope != parsed.resource_domain_scope:
                raise Conflict(
                    "Run idempotency key is bound to a different resource domain scope",
                    scope=parsed.idempotency_scope,
                    key=parsed.idempotency_key,
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

    def list_claim_candidates(
        self,
        *,
        now_utc: str,
        limit: int,
        after_created_at: str | None = None,
        after_run_id: str | None = None,
    ) -> tuple[RunRecord, ...]:
        _require_canonical_utc(now_utc, field_name="now_utc")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
            raise IntegrityViolation("Run claim candidate limit must be between 1 and 1024")
        if (after_created_at is None) != (after_run_id is None):
            raise IntegrityViolation("Run claim rotation cursor must be complete")
        rotation_order: tuple[Any, ...] = ()
        if after_created_at is not None and after_run_id is not None:
            _require_canonical_utc(after_created_at, field_name="after_created_at")
            _require_nonempty(after_run_id, field_name="after_run_id")
            after_cursor = or_(
                _utc_sql_key(RunRow.created_at) > _utc_sql_key(after_created_at),
                and_(
                    _utc_sql_key(RunRow.created_at) == _utc_sql_key(after_created_at),
                    RunRow.run_id > after_run_id,
                ),
            )
            # The cursor only rotates this bounded discovery view. It is never a
            # claim/fencing authority: every selected Run is re-read and CASed in
            # its own UoW. Wrapping here also means a disappeared cursor row cannot
            # hide the beginning of the persistent queue.
            rotation_order = (case((after_cursor, 0), else_=1),)
        run_ids = (
            self._session.execute(
                select(RunRow.run_id)
                .where(
                    RunRow.cancel_requested_at.is_(None),
                    _utc_sql_key(RunRow.overall_deadline_utc) > _utc_sql_key(now_utc),
                    or_(
                        and_(
                            RunRow.status == "queued",
                            _utc_sql_key(RunRow.queue_deadline_utc) > _utc_sql_key(now_utc),
                        ),
                        and_(
                            RunRow.status == "retry_wait",
                            RunRow.retry_not_before_utc.is_not(None),
                            _utc_sql_key(RunRow.retry_not_before_utc) <= _utc_sql_key(now_utc),
                        ),
                    ),
                )
                .order_by(
                    *rotation_order,
                    _utc_sql_key(RunRow.created_at),
                    RunRow.run_id,
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )
        candidates: list[RunRecord] = []
        for run_id in run_ids:
            run = self.get(run_id)
            if run is not None:
                candidates.append(run)
        return tuple(candidates)

    def get_claim_candidate(self, *, now_utc: str) -> RunRecord | None:
        candidates = self.list_claim_candidates(now_utc=now_utc, limit=1)
        return candidates[0] if candidates else None

    def list_inactive_timeout_candidates(
        self,
        *,
        now_utc: str,
        limit: int,
    ) -> tuple[RunRecord, ...]:
        """Discover queued/retry-wait Runs whose authoritative deadline elapsed.

        Claim discovery deliberately excludes these rows. Without a complementary
        persisted scan, a lost process hint or worker restart leaves them stranded
        forever in a non-terminal state. The caller feeds each retained revision to
        ``RunLifecycleService.sweep_timeout``; its write UoW rechecks the deadline,
        lack of an active lease, and the revision before publishing authority.
        """

        _require_canonical_utc(now_utc, field_name="now_utc")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
            raise IntegrityViolation("inactive-timeout scan limit must be between 1 and 1024")
        effective_deadline = case(
            (RunRow.status == "queued", RunRow.queue_deadline_utc),
            else_=RunRow.overall_deadline_utc,
        )
        run_ids = (
            self._session.execute(
                select(RunRow.run_id)
                .where(
                    or_(
                        and_(
                            RunRow.status == "queued",
                            _utc_sql_key(RunRow.queue_deadline_utc) <= _utc_sql_key(now_utc),
                        ),
                        and_(
                            RunRow.status == "retry_wait",
                            _utc_sql_key(RunRow.overall_deadline_utc) <= _utc_sql_key(now_utc),
                        ),
                    )
                )
                .order_by(
                    _utc_sql_key(effective_deadline),
                    _utc_sql_key(RunRow.created_at),
                    RunRow.run_id,
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )
        runs: list[RunRecord] = []
        for run_id in run_ids:
            run = self.get(run_id)
            if run is not None:
                runs.append(run)
        return tuple(runs)

    def list_timeout_candidates(
        self,
        *,
        now_utc: str,
        limit: int,
    ) -> tuple[RunRecord, ...]:
        """Discover every non-terminal Run whose applicable deadline elapsed.

        Active attempt deadlines are deliberately discovered separately from
        lease expiry. A healthy heartbeat may keep a lease live until the frozen
        attempt deadline; routing that row through the lease reaper would classify
        the attempt as ``lease_expired`` and could retry it instead of publishing
        the required ``timed_out`` outcome.
        """

        _require_canonical_utc(now_utc, field_name="now_utc")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
            raise IntegrityViolation("timeout scan limit must be between 1 and 1024")
        attempt_deadline = RunAttemptRow.attempt_deadline_utc
        active_effective_deadline = case(
            (attempt_deadline.is_(None), RunRow.overall_deadline_utc),
            (
                _utc_sql_key(attempt_deadline) <= _utc_sql_key(RunRow.overall_deadline_utc),
                attempt_deadline,
            ),
            else_=RunRow.overall_deadline_utc,
        )
        effective_deadline = case(
            (RunRow.status == "queued", RunRow.queue_deadline_utc),
            (RunRow.status == "retry_wait", RunRow.overall_deadline_utc),
            else_=active_effective_deadline,
        )
        run_ids = (
            self._session.execute(
                select(RunRow.run_id)
                .outerjoin(
                    RunAttemptRow,
                    and_(
                        RunAttemptRow.run_id == RunRow.run_id,
                        RunAttemptRow.attempt_no == RunRow.current_attempt_no,
                    ),
                )
                .where(
                    or_(
                        and_(
                            RunRow.status == "queued",
                            _utc_sql_key(RunRow.queue_deadline_utc) <= _utc_sql_key(now_utc),
                        ),
                        and_(
                            RunRow.status == "retry_wait",
                            _utc_sql_key(RunRow.overall_deadline_utc) <= _utc_sql_key(now_utc),
                        ),
                        and_(
                            RunRow.status.in_(("leased", "running")),
                            or_(
                                _utc_sql_key(RunRow.overall_deadline_utc) <= _utc_sql_key(now_utc),
                                and_(
                                    attempt_deadline.is_not(None),
                                    _utc_sql_key(attempt_deadline) <= _utc_sql_key(now_utc),
                                ),
                            ),
                        ),
                    )
                )
                .order_by(
                    _utc_sql_key(effective_deadline),
                    _utc_sql_key(RunRow.created_at),
                    RunRow.run_id,
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )
        runs: list[RunRecord] = []
        for run_id in run_ids:
            run = self.get(run_id)
            if run is not None:
                runs.append(run)
        return tuple(runs)

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
                    # Canonical UTC permits optional fractional seconds, so lexical
                    # ordering is not chronological at ``...00Z``/``...00.5Z``.
                    _utc_sql_key(RunLeaseRow.expires_at) <= _utc_sql_key(now_utc),
                    RunRow.status.in_(("leased", "running")),
                )
                .order_by(_utc_sql_key(RunLeaseRow.expires_at), RunLeaseRow.run_id)
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
        trace_id: str | None = None,
    ) -> RunAttemptStart:
        started = _require_canonical_utc(started_at, field_name="started_at")
        attempt_deadline = _require_canonical_utc(
            attempt_deadline_utc,
            field_name="attempt_deadline_utc",
        )
        if attempt_deadline <= started:
            raise IntegrityViolation("attempt deadline must be after its start")
        selected_trace_id = (
            None if trace_id is None else _require_nonempty(trace_id, field_name="trace_id")
        )
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
        if attempt.trace_id is not None and selected_trace_id not in {
            None,
            attempt.trace_id,
        }:
            raise IntegrityViolation("attempt start trace differs from the claimed trace")
        effective_trace_id = attempt.trace_id or selected_trace_id
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
            trace_id=effective_trace_id,
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
                "trace_id": effective_trace_id,
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
                (
                    RunAttemptRow.trace_id.is_(None)
                    if attempt.trace_id is None
                    else RunAttemptRow.trace_id == attempt.trace_id
                ),
            )
            .values(
                status="running",
                started_at=started_at,
                attempt_deadline_utc=attempt_deadline_utc,
                trace_id=effective_trace_id,
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
        result = self.apply_preflighted_terminal_closure(
            self.preflight_close_attempt_for_retry(
                fence=fence,
                ended_at=ended_at,
                attempt_status=attempt_status,
                lease_status=lease_status,
                failure_class=failure_class,
                failure_artifact_id=failure_artifact_id,
                attempt_cassette_artifact_id=attempt_cassette_artifact_id,
                retry_decision=retry_decision,
                events=events,
            )
        )
        if not isinstance(result, RunAttemptClose):  # pragma: no cover - sealed internally
            raise IntegrityViolation("Run retry closure returned another result type")
        return result

    def preflight_close_attempt_for_retry(
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
    ) -> object:
        transaction = self._require_terminal_transaction()
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
        run, attempt, lease = self._load_terminal_active_authority(
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
        self._validate_preflight_terminal_events(run, parsed_events)
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
        result = RunAttemptClose(updated_run, updated_attempt, updated_lease, parsed_events)
        command_statement, command_parameters = self._preflight_terminal_commands(
            run_id=run.run_id,
            mode="retry",
            event_seq=parsed_events[-1].seq,
            occurred_at=ended_at,
            fence=fence,
        )
        return self._issue_terminal_closure(
            transaction=transaction,
            result=result,
            run=run,
            updated_run=updated_run,
            attempt=attempt,
            updated_attempt=updated_attempt,
            lease=lease,
            lease_status=lease_status,
            released_at=ended_at,
            events=parsed_events,
            command_mode="retry",
            command_statement=command_statement,
            command_parameters=command_parameters,
        )

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
        result = self.apply_preflighted_terminal_closure(
            self.preflight_complete_attempt_success(
                fence=fence,
                ended_at=ended_at,
                result_artifact_id=result_artifact_id,
                attempt_cassette_artifact_id=attempt_cassette_artifact_id,
                terminal_cassette_artifact_id=terminal_cassette_artifact_id,
                event=event,
            )
        )
        if not isinstance(result, RunTerminal):  # pragma: no cover - sealed internally
            raise IntegrityViolation("Run success closure returned another result type")
        return result

    def preflight_complete_attempt_success(
        self,
        *,
        fence: _AttemptWriteFence,
        ended_at: str,
        result_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        terminal_cassette_artifact_id: str | None,
        event: RunEvent,
    ) -> object:
        transaction = self._require_terminal_transaction()
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
        run, attempt, lease = self._load_terminal_active_authority(
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
        self._validate_preflight_terminal_events(run, (parsed_event,))
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
        result = RunTerminal(updated_run, updated_attempt, updated_lease, parsed_event)
        command_statement, command_parameters = self._preflight_terminal_commands(
            run_id=run.run_id,
            mode="terminal",
            event_seq=parsed_event.seq,
            occurred_at=ended_at,
            fence=fence,
        )
        return self._issue_terminal_closure(
            transaction=transaction,
            result=result,
            run=run,
            updated_run=updated_run,
            attempt=attempt,
            updated_attempt=updated_attempt,
            lease=lease,
            lease_status="closed",
            released_at=ended_at,
            events=(parsed_event,),
            command_mode="terminal",
            command_statement=command_statement,
            command_parameters=command_parameters,
        )

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
        result = self.apply_preflighted_terminal_closure(
            self.preflight_close_attempt_terminal(
                fence=fence,
                ended_at=ended_at,
                attempt_status=attempt_status,
                lease_status=lease_status,
                run_status=run_status,
                failure_class=failure_class,
                attempt_failure_artifact_id=attempt_failure_artifact_id,
                run_failure_artifact_id=run_failure_artifact_id,
                attempt_cassette_artifact_id=attempt_cassette_artifact_id,
                terminal_cassette_artifact_id=terminal_cassette_artifact_id,
                retry_decision=retry_decision,
                leading_events=leading_events,
                terminal_event=terminal_event,
            )
        )
        if not isinstance(result, RunTerminal):  # pragma: no cover - sealed internally
            raise IntegrityViolation("Run terminal closure returned another result type")
        return result

    def preflight_close_attempt_terminal(
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
    ) -> object:
        transaction = self._require_terminal_transaction()
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
        run, attempt, lease = self._load_terminal_active_authority(
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
        self._validate_preflight_terminal_events(run, events)
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
        result = RunTerminal(updated_run, updated_attempt, updated_lease, parsed_terminal)
        command_statement, command_parameters = self._preflight_terminal_commands(
            run_id=run.run_id,
            mode="terminal",
            event_seq=parsed_terminal.seq,
            occurred_at=ended_at,
            fence=fence,
        )
        return self._issue_terminal_closure(
            transaction=transaction,
            result=result,
            run=run,
            updated_run=updated_run,
            attempt=attempt,
            updated_attempt=updated_attempt,
            lease=lease,
            lease_status=lease_status,
            released_at=ended_at,
            events=events,
            command_mode="terminal",
            command_statement=command_statement,
            command_parameters=command_parameters,
        )

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
        result = self.apply_preflighted_terminal_closure(
            self.preflight_terminate_inactive_run(
                run_id=run_id,
                expected_run_revision=expected_run_revision,
                run_status=run_status,
                failure_artifact_id=failure_artifact_id,
                terminal_cassette_artifact_id=terminal_cassette_artifact_id,
                retry_decision=retry_decision,
                event=event,
            )
        )
        if not isinstance(result, RunTerminal):  # pragma: no cover - sealed internally
            raise IntegrityViolation("inactive Run closure returned another result type")
        return result

    def preflight_terminate_inactive_run(
        self,
        *,
        run_id: str,
        expected_run_revision: int,
        run_status: Literal["failed", "cancelled", "timed_out"],
        failure_artifact_id: str,
        terminal_cassette_artifact_id: str | None,
        retry_decision: RetryDecisionV1,
        event: RunEvent,
    ) -> object:
        transaction = self._require_terminal_transaction()
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
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
        run, latest_attempt = self._load_terminal_inactive_authority(
            run_id=selected_run_id,
            expected_run_revision=expected_run_revision,
        )
        if decision.decision != "terminal":
            raise Conflict("inactive Run terminal compare-and-set did not match")
        _validate_cassette_publication(
            run,
            attempt_cassette_artifact_id=None,
            terminal_cassette_artifact_id=terminal_cassette_id,
            closes_attempt=False,
            closes_run=True,
        )
        self._validate_preflight_terminal_events(run, (parsed_event,))
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
        result = RunTerminal(updated_run, latest_attempt, None, parsed_event)
        command_statement, command_parameters = self._preflight_terminal_commands(
            run_id=run.run_id,
            mode="terminal",
            event_seq=parsed_event.seq,
            occurred_at=parsed_event.occurred_at,
            fence=None,
        )
        return self._issue_terminal_closure(
            transaction=transaction,
            result=result,
            run=run,
            updated_run=updated_run,
            attempt=None,
            updated_attempt=None,
            lease=None,
            lease_status=None,
            released_at=parsed_event.occurred_at,
            events=(parsed_event,),
            command_mode="terminal",
            command_statement=command_statement,
            command_parameters=command_parameters,
        )

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

    def get_attempt_write_authority(
        self,
        fence: _AttemptWriteFence,
    ) -> tuple[RunRecord, RunAttempt, RunLease] | None:
        """Read one exact mutable lifecycle head without historical verification.

        Ordinary ``get`` readers deliberately validate the complete retained Event,
        Attempt, and prompt-link history.  A fenced writer instead relies on the
        Run/Attempt CAS heads and uniqueness constraints, so repeating those range
        aggregates while ``BEGIN IMMEDIATE`` is held adds latency but no authority.
        """

        run_id = _require_nonempty(fence.run_id, field_name="fence.run_id")
        attempt_no = _require_positive(fence.attempt_no, field_name="fence.attempt_no")
        lease_id = _require_nonempty(fence.lease_id, field_name="fence.lease_id")
        rows = self._session.execute(
            select(RunRow, RunAttemptRow, RunLeaseRow)
            .join(
                RunAttemptRow,
                and_(
                    RunAttemptRow.run_id == RunRow.run_id,
                    RunAttemptRow.attempt_no == attempt_no,
                ),
            )
            .join(
                RunLeaseRow,
                and_(
                    RunLeaseRow.run_id == RunRow.run_id,
                    RunLeaseRow.attempt_no == attempt_no,
                    RunLeaseRow.lease_id == lease_id,
                ),
            )
            .where(RunRow.run_id == run_id)
            .execution_options(populate_existing=True)
        ).all()
        if len(rows) != 1:
            return None
        run_row, attempt_row, lease_row = rows[0]
        return (
            _parse_run_row(run_row, expected_run_id=run_id),
            _parse_attempt_row(
                attempt_row,
                expected_run_id=run_id,
                expected_attempt_no=attempt_no,
            ),
            _parse_lease_row(lease_row, expected_lease_id=lease_id),
        )

    def get_run_write_authority(
        self,
        run_id: str,
    ) -> tuple[RunRecord, RunAttempt | None, RunLease | None] | None:
        """Read a Run's bounded latest mutable head without historical scans."""

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        rows = self._session.execute(
            select(RunRow, RunAttemptRow, RunLeaseRow)
            .outerjoin(
                RunAttemptRow,
                and_(
                    RunAttemptRow.run_id == RunRow.run_id,
                    RunAttemptRow.attempt_no == RunRow.next_attempt_no - 1,
                ),
            )
            .outerjoin(
                RunLeaseRow,
                and_(
                    RunLeaseRow.run_id == RunRow.run_id,
                    RunLeaseRow.status == "active",
                ),
            )
            .where(RunRow.run_id == selected_run_id)
            .execution_options(populate_existing=True)
        ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise IntegrityViolation(
                "Run write authority has more than one active head",
                run_id=selected_run_id,
            )
        run_row, attempt_row, lease_row = rows[0]
        run = _parse_run_row(run_row, expected_run_id=selected_run_id)
        attempt = (
            None
            if attempt_row is None
            else _parse_attempt_row(
                attempt_row,
                expected_run_id=selected_run_id,
                expected_attempt_no=attempt_row.attempt_no,
            )
        )
        lease = (
            None
            if lease_row is None
            else _parse_lease_row(lease_row, expected_lease_id=lease_row.lease_id)
        )
        return run, attempt, lease

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

        Identical to :meth:`get` except it skips the ENTIRE write-side event-head check
        (:meth:`_verify_run_heads`) — both the event-count/first/last contiguity guard
        AND its future attempt/fencing-head guards. Event retention legitimately removes
        the oldest events, so a resumable-read consumer (e.g. the SSE stream) must be able
        to load the Run and derive its scope even when its earliest events have been
        pruned. The non-event execution-state invariants are still enforced by
        :meth:`_verify_run_state` (attempt/lease/permit consistency for the Run's status),
        so the projection remains a coherent Run view; only the event-log head guarantee
        is relaxed.
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

    def prune_terminal_event_prefix(self, run_id: str, *, before_seq: int) -> int:
        """Delete a terminal Run's retained prefix while preserving its terminal event.

        This is the persistence primitive used by retention policy code. Active Runs
        remain fully contiguous because they can still append events. A terminal Run is
        immutable, so a contiguous suffix ending at ``next_event_seq - 1`` remains a
        complete resumable-read authority. The final event and any event still referenced
        by a durable RunCommand are never pruned here; command retention is a separate
        authority and may not be weakened implicitly.
        """

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_before_seq = _require_positive(before_seq, field_name="before_seq")
        run = self.get(selected_run_id)
        if run is None:
            raise IntegrityViolation("Run event retention target does not exist")
        if run.status not in {"succeeded", "failed", "cancelled", "timed_out"}:
            raise InvalidStateTransition("Run event retention requires a terminal Run")
        terminal_seq = run.next_event_seq - 1
        terminal_event = self.get_event(selected_run_id, terminal_seq)
        if terminal_event is None or terminal_event.event_type != f"run.{run.status}":
            raise IntegrityViolation("terminal Run does not retain its terminal event")
        command_event_seq = self._session.execute(
            select(func.min(RunCommandRow.result_event_seq)).where(
                RunCommandRow.run_id == selected_run_id,
                RunCommandRow.result_event_seq.is_not(None),
            )
        ).scalar_one_or_none()
        protected_seq = min(
            terminal_seq,
            int(command_event_seq) if command_event_seq is not None else terminal_seq,
        )
        result = self._session.execute(
            delete(RunEventRow).where(
                RunEventRow.run_id == selected_run_id,
                RunEventRow.seq < selected_before_seq,
                RunEventRow.seq < protected_seq,
            )
        )
        self._session.flush()
        retained = self.get(selected_run_id)
        if retained != run:
            raise IntegrityViolation("Run event retention changed the Run projection")
        return int(result.rowcount or 0)

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
            parsed.route_ordinal,
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
        if parsed.route_ordinal == 1:
            if attempt.next_call_ordinal != parsed.call_ordinal:
                raise Conflict(
                    "call ordinal does not match the Attempt head",
                    expected_call_ordinal=attempt.next_call_ordinal,
                    actual_call_ordinal=parsed.call_ordinal,
                )
        else:
            if parsed.call_ordinal >= attempt.next_call_ordinal:
                raise Conflict(
                    "fallback route requires an already-open logical call",
                    call_ordinal=parsed.call_ordinal,
                    next_call_ordinal=attempt.next_call_ordinal,
                )
            predecessor = self.get_intermediate_link(
                parsed.run_id,
                parsed.attempt_no,
                parsed.call_ordinal,
                parsed.route_ordinal - 1,
            )
            if predecessor is None:
                raise Conflict(
                    "fallback route ordinal is not contiguous",
                    call_ordinal=parsed.call_ordinal,
                    route_ordinal=parsed.route_ordinal,
                )
            consumed = self._session.execute(
                select(RunModelResponseConsumptionRow.route_ordinal)
                .where(
                    RunModelResponseConsumptionRow.run_id == parsed.run_id,
                    RunModelResponseConsumptionRow.attempt_no == parsed.attempt_no,
                    RunModelResponseConsumptionRow.call_ordinal == parsed.call_ordinal,
                )
                .limit(1)
            ).scalar_one_or_none()
            if consumed is not None:
                raise Conflict(
                    "fallback prompt cannot extend an already-consumed logical call",
                    call_ordinal=parsed.call_ordinal,
                    consumed_route_ordinal=consumed,
                )
        artifact = self._session.get(ArtifactRow, parsed.artifact_id)
        if artifact is None or artifact.kind != "source_rendered":
            raise IntegrityViolation(
                "prompt-rendered link requires a source_rendered Artifact",
                artifact_id=parsed.artifact_id,
            )

        if parsed.route_ordinal == 1:
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
        route_ordinal: int = 1,
    ) -> RunIntermediateArtifactLinkV1 | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_attempt = _require_positive(attempt_no, field_name="attempt_no")
        selected_ordinal = _require_positive(call_ordinal, field_name="call_ordinal")
        selected_route = _require_positive(route_ordinal, field_name="route_ordinal")
        row = self._session.get(
            RunIntermediateArtifactLinkRow,
            (selected_run_id, selected_attempt, selected_ordinal, selected_route),
        )
        if row is None:
            return None
        parsed = _parse_intermediate_row(
            row,
            expected_run_id=selected_run_id,
            expected_attempt_no=selected_attempt,
            expected_call_ordinal=selected_ordinal,
            expected_route_ordinal=selected_route,
        )
        attempt = self.get_attempt(selected_run_id, selected_attempt)
        if (
            attempt is None
            or parsed.fencing_token != attempt.fencing_token
            or parsed.call_ordinal >= attempt.next_call_ordinal
        ):
            raise IntegrityViolation("stored intermediate link disagrees with its Attempt head")
        return parsed

    def list_prompt_render_links(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
        limit: int = MAX_RUN_MANIFEST_PARENT_BINDINGS,
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]:
        """All committed ``prompt_rendered`` intermediate links for a Run.

        Bounded to one attempt when ``attempt_no`` is given (attempt-scope manifest
        projection) or the whole Run when ``None`` (run-scope). Ordered by
        ``(attempt_no, call_ordinal, route_ordinal)`` so the terminal manifest projection is
        deterministic. A ``not_applicable``/``live`` Run has no source_rendered links
        and this returns ``()``.
        """

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_limit = _require_positive(limit, field_name="limit")
        if selected_limit > MAX_RUN_MANIFEST_PARENT_BINDINGS:
            raise IntegrityViolation("prompt-link limit exceeds the runtime authority hard cap")
        query = select(RunIntermediateArtifactLinkRow).where(
            RunIntermediateArtifactLinkRow.run_id == selected_run_id,
            RunIntermediateArtifactLinkRow.role == "prompt_rendered",
        )
        if attempt_no is not None:
            selected_attempt = _require_positive(attempt_no, field_name="attempt_no")
            query = query.where(RunIntermediateArtifactLinkRow.attempt_no == selected_attempt)
        query = query.order_by(
            RunIntermediateArtifactLinkRow.attempt_no,
            RunIntermediateArtifactLinkRow.call_ordinal,
            RunIntermediateArtifactLinkRow.route_ordinal,
        )
        rows = self._session.scalars(query.limit(selected_limit + 1)).all()
        if len(rows) > selected_limit:
            raise IntegrityViolation("prompt-link listing exceeds its runtime authority bound")
        return tuple(
            _parse_intermediate_row(
                row,
                expected_run_id=row.run_id,
                expected_attempt_no=row.attempt_no,
                expected_call_ordinal=row.call_ordinal,
                expected_route_ordinal=row.route_ordinal,
            )
            for row in rows
        )

    def list_prompt_render_links_by_artifact_id(
        self,
        artifact_id: str,
        *,
        limit: int,
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]:
        """Bounded reverse lookup used to authenticate runtime lineage parents."""

        selected_artifact_id = _require_nonempty(artifact_id, field_name="artifact_id")
        selected_limit = _require_positive(limit, field_name="limit")
        rows = self._session.scalars(
            select(RunIntermediateArtifactLinkRow)
            .where(
                RunIntermediateArtifactLinkRow.artifact_id == selected_artifact_id,
                RunIntermediateArtifactLinkRow.role == "prompt_rendered",
            )
            .order_by(
                RunIntermediateArtifactLinkRow.run_id,
                RunIntermediateArtifactLinkRow.attempt_no,
                RunIntermediateArtifactLinkRow.call_ordinal,
                RunIntermediateArtifactLinkRow.route_ordinal,
            )
            .limit(selected_limit + 1)
        ).all()
        if len(rows) > selected_limit:
            raise IntegrityViolation(
                "prompt Artifact reverse lookup exceeds the admission bound",
                artifact_id=selected_artifact_id,
                limit=selected_limit,
            )
        return tuple(
            _parse_intermediate_row(
                row,
                expected_run_id=row.run_id,
                expected_attempt_no=row.attempt_no,
                expected_call_ordinal=row.call_ordinal,
                expected_route_ordinal=row.route_ordinal,
            )
            for row in rows
        )

    def put_tool_intermediate_link(
        self,
        link: RunToolIntermediateLinkV1,
    ) -> RunToolIntermediateLinkV1:
        parsed = _revalidate(
            link,
            RunToolIntermediateLinkV1,
            label="Run tool intermediate link put",
        )
        existing = self.get_tool_intermediate_link(
            parsed.run_id,
            parsed.attempt_no,
            parsed.target_call_ordinal,
        )
        if existing is not None:
            if _canonical_wire(existing) != _canonical_wire(parsed):
                raise IntegrityViolation(
                    "immutable Run tool intermediate differs from retained content",
                    run_id=parsed.run_id,
                    attempt_no=parsed.attempt_no,
                    target_call_ordinal=parsed.target_call_ordinal,
                )
            return existing

        run = self.get(parsed.run_id)
        attempt = self.get_attempt(parsed.run_id, parsed.attempt_no)
        lease = self.get_current_lease(parsed.run_id)
        if (
            run is None
            or run.current_attempt_no != parsed.attempt_no
            or run.status not in _ACTIVE_RUN_STATUSES
            or attempt is None
            or attempt.status not in _ACTIVE_ATTEMPT_STATUSES
            or lease is None
            or lease.attempt_no != parsed.attempt_no
            or lease.fencing_token != parsed.fencing_token
            or attempt.fencing_token != parsed.fencing_token
        ):
            raise InvalidStateTransition("tool intermediate requires the current fenced lease")
        if attempt.next_call_ordinal != parsed.target_call_ordinal:
            raise Conflict(
                "tool intermediate target differs from the Attempt call head",
                expected_call_ordinal=attempt.next_call_ordinal,
                target_call_ordinal=parsed.target_call_ordinal,
            )
        if (
            self.get_intermediate_link(
                parsed.run_id,
                parsed.attempt_no,
                parsed.target_call_ordinal,
                1,
            )
            is not None
        ):
            raise Conflict("tool intermediate must be published before its rendered prompt")
        artifact = self._session.get(ArtifactRow, parsed.artifact_id)
        if (
            artifact is None
            or artifact.kind != "source_raw"
            or artifact.payload_hash != parsed.payload_hash
            or not isinstance(artifact.meta, dict)
            or artifact.meta.get("payload_schema_id") != "agent-prompt-context@1"
            or artifact.meta.get("producer_run_id") != parsed.run_id
            or artifact.meta.get("producer_attempt_no") != parsed.attempt_no
            or artifact.meta.get("target_call_ordinal") != parsed.target_call_ordinal
            or artifact.meta.get("agent_node_id") != parsed.agent_node_id
            or artifact.meta.get("prompt_version") != parsed.prompt_version
        ):
            raise IntegrityViolation(
                "tool intermediate requires its exact Agent prompt-context Artifact",
                artifact_id=parsed.artifact_id,
            )

        self._session.add(RunToolIntermediateLinkRow(**parsed.model_dump(mode="json")))
        self._session.flush()
        return parsed

    def get_tool_intermediate_link(
        self,
        run_id: str,
        attempt_no: int,
        target_call_ordinal: int,
    ) -> RunToolIntermediateLinkV1 | None:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_attempt = _require_positive(attempt_no, field_name="attempt_no")
        selected_call = _require_positive(
            target_call_ordinal,
            field_name="target_call_ordinal",
        )
        row = self._session.get(
            RunToolIntermediateLinkRow,
            (selected_run_id, selected_attempt, selected_call),
        )
        if row is None:
            return None
        parsed = _parse_tool_intermediate_row(
            row,
            expected_run_id=selected_run_id,
            expected_attempt_no=selected_attempt,
            expected_target_call_ordinal=selected_call,
        )
        attempt = self.get_attempt(selected_run_id, selected_attempt)
        if (
            attempt is None
            or parsed.fencing_token != attempt.fencing_token
            or parsed.target_call_ordinal > attempt.next_call_ordinal
        ):
            raise IntegrityViolation("stored tool intermediate disagrees with its RunAttempt")
        return parsed

    def get_tool_intermediate_for_call(
        self,
        run_id: str,
        attempt_no: int,
        target_call_ordinal: int,
    ) -> RunToolIntermediateLinkV1 | None:
        return self.get_tool_intermediate_link(
            run_id,
            attempt_no,
            target_call_ordinal,
        )

    def list_tool_intermediate_links(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
        limit: int = MAX_RUN_MANIFEST_PARENT_BINDINGS,
    ) -> tuple[RunToolIntermediateLinkV1, ...]:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_limit = _require_positive(limit, field_name="limit")
        if selected_limit > MAX_RUN_MANIFEST_PARENT_BINDINGS:
            raise IntegrityViolation(
                "tool intermediate limit exceeds the runtime authority hard cap"
            )
        query = select(RunToolIntermediateLinkRow).where(
            RunToolIntermediateLinkRow.run_id == selected_run_id
        )
        if attempt_no is not None:
            selected_attempt = _require_positive(attempt_no, field_name="attempt_no")
            query = query.where(RunToolIntermediateLinkRow.attempt_no == selected_attempt)
        rows = self._session.scalars(
            query.order_by(
                RunToolIntermediateLinkRow.attempt_no,
                RunToolIntermediateLinkRow.target_call_ordinal,
            ).limit(selected_limit + 1)
        ).all()
        if len(rows) > selected_limit:
            raise IntegrityViolation(
                "tool intermediate lookup exceeds the admission bound",
                run_id=selected_run_id,
                limit=selected_limit,
            )
        return tuple(
            _parse_tool_intermediate_row(
                row,
                expected_run_id=row.run_id,
                expected_attempt_no=row.attempt_no,
                expected_target_call_ordinal=row.target_call_ordinal,
            )
            for row in rows
        )

    def put_model_route_link(self, link: RunModelRouteLinkV1) -> RunModelRouteLinkV1:
        parsed = _revalidate(link, RunModelRouteLinkV1, label="Run model route link put")
        existing = self.get_model_route_link(
            parsed.run_id,
            parsed.attempt_no,
            parsed.call_ordinal,
            parsed.route_ordinal,
        )
        if existing is not None:
            if _canonical_wire(existing) != _canonical_wire(parsed):
                raise IntegrityViolation(
                    "immutable Run model route differs from retained content",
                    run_id=parsed.run_id,
                    attempt_no=parsed.attempt_no,
                    call_ordinal=parsed.call_ordinal,
                    route_ordinal=parsed.route_ordinal,
                )
            return existing

        run = self.get(parsed.run_id)
        attempt = self.get_attempt(parsed.run_id, parsed.attempt_no)
        lease = self.get_current_lease(parsed.run_id)
        if (
            run is None
            or run.current_attempt_no != parsed.attempt_no
            or run.status not in _ACTIVE_RUN_STATUSES
            or attempt is None
            or attempt.status not in _ACTIVE_ATTEMPT_STATUSES
            or lease is None
            or lease.attempt_no != parsed.attempt_no
            or lease.fencing_token != parsed.fencing_token
            or attempt.fencing_token != parsed.fencing_token
        ):
            raise InvalidStateTransition("model route requires the current fenced lease")
        prompt = self.get_intermediate_link(
            parsed.run_id,
            parsed.attempt_no,
            parsed.call_ordinal,
            parsed.route_ordinal,
        )
        if (
            prompt is None
            or prompt.artifact_id != parsed.prompt_artifact_id
            or prompt.request_hash != parsed.request_hash
            or prompt.fencing_token != parsed.fencing_token
            or prompt.published_at != parsed.published_at
        ):
            raise IntegrityViolation("model route differs from its exact rendered prompt")
        predecessor: RunModelRouteLinkV1 | None = None
        if parsed.route_ordinal > 1:
            predecessor = self.get_model_route_link(
                parsed.run_id,
                parsed.attempt_no,
                parsed.call_ordinal,
                parsed.route_ordinal - 1,
            )
            if predecessor is None:
                raise Conflict("fallback model route lacks its retained predecessor")
        consumed = self._session.execute(
            select(RunModelResponseConsumptionRow.route_ordinal)
            .where(
                RunModelResponseConsumptionRow.run_id == parsed.run_id,
                RunModelResponseConsumptionRow.attempt_no == parsed.attempt_no,
                RunModelResponseConsumptionRow.call_ordinal == parsed.call_ordinal,
            )
            .limit(1)
        ).scalar_one_or_none()
        if consumed is not None:
            raise Conflict("model route cannot extend an already-consumed logical call")

        native_id: str | None = None
        legacy_id: str | None = None
        plan = run.payload.execution_version_plan
        if plan is None:
            raise IntegrityViolation("model route requires a frozen execution version plan")
        expected_sources = (
            {"cassette_replay"}
            if run.payload.llm_execution_mode == "replay"
            else (
                {"online", "full_response_cache"}
                if run.payload.llm_execution_mode in {"live", "record"}
                else set()
            )
        )
        cost_authority = SqlCostRepository(self._session)
        if parsed.routing_decision_kind == "native":
            decision = cost_authority.get_routing_decision(parsed.routing_decision_id)
            if (
                decision is None
                or decision.run_id != parsed.run_id
                or decision.attempt_no != parsed.attempt_no
                or decision.request_hash != f"sha256:{parsed.request_hash}"
                or decision.budget_set_snapshot_id != run.payload.budget_set_snapshot_id
                or decision.policy_version != plan.routing_policy_version
                or decision.routing_policy_digest != plan.routing_policy_digest
                or decision.catalog_version != plan.model_catalog_version
                or decision.catalog_digest != plan.model_catalog_digest
                or decision.execution_source not in expected_sources
                or decision.fallback_index + 1 != parsed.route_ordinal
            ):
                raise IntegrityViolation("model route differs from native RoutingDecision")
            if predecessor is not None:
                if predecessor.routing_decision_kind != "native":
                    raise IntegrityViolation("native fallback route has a non-native predecessor")
                predecessor_decision = cost_authority.get_routing_decision(
                    predecessor.routing_decision_id
                )
                if (
                    predecessor_decision is None
                    or decision.fallback_index != predecessor_decision.fallback_index + 1
                    or decision.fallback_from != predecessor_decision.model_snapshot
                    or decision.rule_id != predecessor_decision.rule_id
                ):
                    raise IntegrityViolation(
                        "native fallback decision differs from its retained predecessor"
                    )
            native_id = parsed.routing_decision_id
        else:
            decision = cost_authority.get_legacy_import_routing_decision(parsed.routing_decision_id)
            node = (
                None
                if decision is None
                else next(
                    (item for item in plan.nodes if item.agent_node_id == decision.agent_node_id),
                    None,
                )
            )
            if (
                decision is None
                or decision.request_hash != f"sha256:{parsed.request_hash}"
                or run.payload.llm_execution_mode != "replay"
                or decision.model_catalog_version != plan.model_catalog_version
                or decision.model_catalog_digest != plan.model_catalog_digest
                or node is None
                or decision.model_snapshot not in node.allowed_model_snapshots
                or parsed.route_ordinal != 1
            ):
                raise IntegrityViolation("model route differs from legacy routing authority")
            legacy_id = parsed.routing_decision_id

        self._session.add(
            RunModelRouteLinkRow(
                **parsed.model_dump(mode="json"),
                native_routing_decision_id=native_id,
                legacy_routing_decision_id=legacy_id,
            )
        )
        self._session.flush()
        return parsed

    def get_model_route_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelRouteLinkV1 | None:
        key = (
            _require_nonempty(run_id, field_name="run_id"),
            _require_positive(attempt_no, field_name="attempt_no"),
            _require_positive(call_ordinal, field_name="call_ordinal"),
            _require_positive(route_ordinal, field_name="route_ordinal"),
        )
        row = self._session.get(RunModelRouteLinkRow, key)
        if row is None:
            return None
        parsed = _parse_model_route_row(row)
        prompt = self.get_intermediate_link(*key)
        if (
            prompt is None
            or prompt.artifact_id != parsed.prompt_artifact_id
            or prompt.request_hash != parsed.request_hash
            or prompt.fencing_token != parsed.fencing_token
            or prompt.published_at != parsed.published_at
        ):
            raise IntegrityViolation("stored model route differs from rendered-prompt authority")
        run = self.get(parsed.run_id)
        plan = None if run is None else run.payload.execution_version_plan
        if run is None or plan is None:
            raise IntegrityViolation("stored model route lacks its frozen execution plan")
        cost_authority = SqlCostRepository(self._session)
        if parsed.routing_decision_kind == "native":
            decision = cost_authority.get_routing_decision(parsed.routing_decision_id)
            expected_sources = (
                {"cassette_replay"}
                if run.payload.llm_execution_mode == "replay"
                else (
                    {"online", "full_response_cache"}
                    if run.payload.llm_execution_mode in {"live", "record"}
                    else set()
                )
            )
            if (
                decision is None
                or decision.run_id != parsed.run_id
                or decision.attempt_no != parsed.attempt_no
                or decision.request_hash != f"sha256:{parsed.request_hash}"
                or decision.budget_set_snapshot_id != run.payload.budget_set_snapshot_id
                or decision.policy_version != plan.routing_policy_version
                or decision.routing_policy_digest != plan.routing_policy_digest
                or decision.catalog_version != plan.model_catalog_version
                or decision.catalog_digest != plan.model_catalog_digest
                or decision.execution_source not in expected_sources
                or decision.fallback_index + 1 != parsed.route_ordinal
            ):
                raise IntegrityViolation("stored model route differs from RoutingDecision")
        else:
            decision = cost_authority.get_legacy_import_routing_decision(parsed.routing_decision_id)
            node = (
                None
                if decision is None
                else next(
                    (item for item in plan.nodes if item.agent_node_id == decision.agent_node_id),
                    None,
                )
            )
            if (
                decision is None
                or run.payload.llm_execution_mode != "replay"
                or decision.request_hash != f"sha256:{parsed.request_hash}"
                or decision.model_catalog_version != plan.model_catalog_version
                or decision.model_catalog_digest != plan.model_catalog_digest
                or node is None
                or decision.model_snapshot not in node.allowed_model_snapshots
                or parsed.route_ordinal != 1
            ):
                raise IntegrityViolation("stored model route differs from legacy authority")
        return parsed

    def list_model_route_links(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
        limit: int = MAX_RUN_MANIFEST_PARENT_BINDINGS,
    ) -> tuple[RunModelRouteLinkV1, ...]:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RUN_MANIFEST_PARENT_BINDINGS
        ):
            raise IntegrityViolation("model-route limit exceeds the runtime authority hard cap")
        query = select(RunModelRouteLinkRow).where(RunModelRouteLinkRow.run_id == selected_run_id)
        if attempt_no is not None:
            query = query.where(
                RunModelRouteLinkRow.attempt_no
                == _require_positive(attempt_no, field_name="attempt_no")
            )
        rows = self._session.scalars(
            query.order_by(
                RunModelRouteLinkRow.attempt_no,
                RunModelRouteLinkRow.call_ordinal,
                RunModelRouteLinkRow.route_ordinal,
            ).limit(limit + 1)
        ).all()
        if len(rows) > limit:
            raise IntegrityViolation("model-route listing exceeds its admission bound")
        retained: list[RunModelRouteLinkV1] = []
        for row in rows:
            link = self.get_model_route_link(
                row.run_id,
                row.attempt_no,
                row.call_ordinal,
                row.route_ordinal,
            )
            if link is None:  # pragma: no cover - selected row cannot disappear in one UoW
                raise IntegrityViolation("listed model route disappeared")
            retained.append(link)
        return tuple(retained)

    def put_model_response_consumption(
        self,
        consumption: RunModelResponseConsumptionV1,
    ) -> RunModelResponseConsumptionV1:
        parsed = _revalidate(
            consumption,
            RunModelResponseConsumptionV1,
            label="Run model response consumption put",
        )
        key = (
            parsed.run_id,
            parsed.attempt_no,
            parsed.call_ordinal,
            parsed.route_ordinal,
        )
        existing = self.get_model_response_consumption(*key)
        if existing is not None:
            if _canonical_wire(existing) != _canonical_wire(parsed):
                raise IntegrityViolation(
                    "immutable model response consumption differs from retained content"
                )
            return existing
        prior_consumption = self._session.execute(
            select(RunModelResponseConsumptionRow.route_ordinal)
            .where(
                RunModelResponseConsumptionRow.run_id == parsed.run_id,
                RunModelResponseConsumptionRow.attempt_no == parsed.attempt_no,
                RunModelResponseConsumptionRow.call_ordinal == parsed.call_ordinal,
            )
            .limit(1)
        ).scalar_one_or_none()
        if prior_consumption is not None:
            raise Conflict(
                "logical model call already consumed another route",
                consumed_route_ordinal=prior_consumption,
            )
        latest_route_ordinal = self._session.execute(
            select(func.max(RunModelRouteLinkRow.route_ordinal)).where(
                RunModelRouteLinkRow.run_id == parsed.run_id,
                RunModelRouteLinkRow.attempt_no == parsed.attempt_no,
                RunModelRouteLinkRow.call_ordinal == parsed.call_ordinal,
            )
        ).scalar_one()
        if latest_route_ordinal != parsed.route_ordinal:
            raise Conflict(
                "model response can consume only the latest committed route",
                requested_route_ordinal=parsed.route_ordinal,
                latest_route_ordinal=latest_route_ordinal,
            )
        route = self.get_model_route_link(*key)
        run = self.get(parsed.run_id)
        attempt = self.get_attempt(parsed.run_id, parsed.attempt_no)
        lease = self.get_current_lease(parsed.run_id)
        if (
            route is None
            or run is None
            or run.current_attempt_no != parsed.attempt_no
            or run.status not in _ACTIVE_RUN_STATUSES
            or attempt is None
            or attempt.status not in _ACTIVE_ATTEMPT_STATUSES
            or lease is None
            or lease.attempt_no != parsed.attempt_no
            or lease.fencing_token != route.fencing_token
            or attempt.fencing_token != route.fencing_token
        ):
            raise InvalidStateTransition("response consumption requires the current fenced route")

        cost_authority = SqlCostRepository(self._session)
        decision = (
            cost_authority.get_routing_decision(route.routing_decision_id)
            if route.routing_decision_kind == "native"
            else cost_authority.get_legacy_import_routing_decision(route.routing_decision_id)
        )
        if decision is None or decision.execution_source != parsed.execution_source:
            raise IntegrityViolation("response consumption differs from routing authority")

        group = self._session.get(ReservationGroupRow, parsed.reservation_group_id)
        if (
            group is None
            or group.scope != "attempt_call"
            or group.status not in {"reconciled", "conservatively_settled", "late_reconciled"}
            or group.run_id != parsed.run_id
            or group.attempt_no != parsed.attempt_no
            or group.request_hash != f"sha256:{route.request_hash}"
            or group.fencing_token != route.fencing_token
        ):
            raise IntegrityViolation("response consumption differs from reservation authority")
        usages = self._session.scalars(
            select(UsageEntryRow).where(
                UsageEntryRow.reservation_group_id == parsed.reservation_group_id,
                UsageEntryRow.adjustment_of_usage_id.is_(None),
            )
        ).all()
        if len(usages) != 1:
            raise IntegrityViolation("response consumption requires one reconciled base usage")
        usage = usages[0]
        if (
            usage.run_id != parsed.run_id
            or usage.attempt_no != parsed.attempt_no
            or usage.request_hash != f"sha256:{route.request_hash}"
            or usage.execution_source != parsed.execution_source
            or usage.routing_decision_kind != route.routing_decision_kind
            or usage.routing_decision_id != route.routing_decision_id
            or usage.fencing_token_at_reserve != route.fencing_token
            or (
                parsed.execution_source == "online"
                and usage.transport_attempt != parsed.transport_attempt
            )
        ):
            raise IntegrityViolation("response consumption differs from reconciled usage")

        shard_id = parsed.cassette_shard_artifact_id
        if (run.payload.llm_execution_mode == "record") != (shard_id is not None):
            raise IntegrityViolation("RECORD response consumption requires exactly one shard")
        if shard_id is not None:
            artifact = self._session.get(ArtifactRow, shard_id)
            if (
                artifact is None
                or artifact.kind != "cassette_bundle"
                or not isinstance(artifact.meta, dict)
                or artifact.meta.get("payload_schema_id") != "cassette-record-shard@1"
                or tuple(artifact.lineage) != (route.prompt_artifact_id,)
            ):
                raise IntegrityViolation(
                    "response shard is not the exact prompt-derived cassette bundle"
                )

        self._session.add(RunModelResponseConsumptionRow(**parsed.model_dump(mode="json")))
        self._session.flush()
        return parsed

    def get_model_response_consumption(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelResponseConsumptionV1 | None:
        key = (
            _require_nonempty(run_id, field_name="run_id"),
            _require_positive(attempt_no, field_name="attempt_no"),
            _require_positive(call_ordinal, field_name="call_ordinal"),
            _require_positive(route_ordinal, field_name="route_ordinal"),
        )
        row = self._session.get(RunModelResponseConsumptionRow, key)
        if row is None:
            return None
        parsed = _parse_model_consumption_row(row)
        route = self.get_model_route_link(*key)
        if route is None:
            raise IntegrityViolation("stored response consumption lacks its exact model route")
        run = self.get(parsed.run_id)
        group = self._session.get(ReservationGroupRow, parsed.reservation_group_id)
        usages = self._session.scalars(
            select(UsageEntryRow).where(
                UsageEntryRow.reservation_group_id == parsed.reservation_group_id,
                UsageEntryRow.adjustment_of_usage_id.is_(None),
            )
        ).all()
        if (
            run is None
            or group is None
            or group.scope != "attempt_call"
            or group.status not in {"reconciled", "conservatively_settled", "late_reconciled"}
            or group.run_id != parsed.run_id
            or group.attempt_no != parsed.attempt_no
            or group.request_hash != f"sha256:{route.request_hash}"
            or group.fencing_token != route.fencing_token
            or len(usages) != 1
        ):
            raise IntegrityViolation("stored response consumption differs from cost authority")
        usage = usages[0]
        if (
            usage.run_id != parsed.run_id
            or usage.attempt_no != parsed.attempt_no
            or usage.request_hash != f"sha256:{route.request_hash}"
            or usage.execution_source != parsed.execution_source
            or usage.routing_decision_kind != route.routing_decision_kind
            or usage.routing_decision_id != route.routing_decision_id
            or usage.fencing_token_at_reserve != route.fencing_token
            or (
                parsed.execution_source == "online"
                and usage.transport_attempt != parsed.transport_attempt
            )
        ):
            raise IntegrityViolation("stored response consumption differs from usage authority")
        shard_id = parsed.cassette_shard_artifact_id
        if (run.payload.llm_execution_mode == "record") != (shard_id is not None):
            raise IntegrityViolation("stored response consumption has the wrong shard mode")
        if shard_id is not None:
            artifact = self._session.get(ArtifactRow, shard_id)
            if (
                artifact is None
                or artifact.kind != "cassette_bundle"
                or not isinstance(artifact.meta, dict)
                or artifact.meta.get("payload_schema_id") != "cassette-record-shard@1"
                or tuple(artifact.lineage) != (route.prompt_artifact_id,)
            ):
                raise IntegrityViolation("stored response consumption has a detached shard")
        return parsed

    def list_model_response_consumptions(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
        limit: int = MAX_RUN_MANIFEST_PARENT_BINDINGS,
    ) -> tuple[RunModelResponseConsumptionV1, ...]:
        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RUN_MANIFEST_PARENT_BINDINGS
        ):
            raise IntegrityViolation(
                "model-consumption limit exceeds the runtime authority hard cap"
            )
        query = select(RunModelResponseConsumptionRow).where(
            RunModelResponseConsumptionRow.run_id == selected_run_id
        )
        if attempt_no is not None:
            query = query.where(
                RunModelResponseConsumptionRow.attempt_no
                == _require_positive(attempt_no, field_name="attempt_no")
            )
        rows = self._session.scalars(
            query.order_by(
                RunModelResponseConsumptionRow.attempt_no,
                RunModelResponseConsumptionRow.call_ordinal,
                RunModelResponseConsumptionRow.route_ordinal,
            ).limit(limit + 1)
        ).all()
        if len(rows) > limit:
            raise IntegrityViolation("model-consumption listing exceeds its admission bound")
        retained: list[RunModelResponseConsumptionV1] = []
        for row in rows:
            consumption = self.get_model_response_consumption(
                row.run_id,
                row.attempt_no,
                row.call_ordinal,
                row.route_ordinal,
            )
            if consumption is None:  # pragma: no cover - selected row cannot disappear in one UoW
                raise IntegrityViolation("listed model response consumption disappeared")
            retained.append(consumption)
        return tuple(retained)

    def list_closed_attempt_failures(self, run_id: str) -> tuple[tuple[int, str], ...]:
        """``(attempt_no, failure_artifact_id)`` for each closed failed attempt.

        Feeds the terminal run-aggregate manifest's ``closed_attempt_failure`` runtime
        parents. A first-attempt success Run has no closed failure and returns ``()``.
        """

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        rows = self._session.execute(
            select(RunAttemptRow.attempt_no, RunAttemptRow.failure_artifact_id)
            .where(
                RunAttemptRow.run_id == selected_run_id,
                RunAttemptRow.failure_artifact_id.is_not(None),
            )
            .order_by(RunAttemptRow.attempt_no)
        ).all()
        return tuple(
            (int(attempt_no), str(failure_artifact_id)) for attempt_no, failure_artifact_id in rows
        )

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

    def put_finding_links_many(
        self,
        links: Sequence[RunFindingLinkV1],
    ) -> tuple[RunFindingLinkV1, ...]:
        """Preflight and publish one Finding-link aggregate in this transaction."""

        return self.put_preflighted_finding_links_many(self.preflight_finding_links_many(links))

    def preflight_finding_links_many(
        self,
        links: Sequence[RunFindingLinkV1],
        *,
        planned_findings: Sequence[FindingRevisionV1] = (),
        planned_artifact_ids: Sequence[str] = (),
    ) -> _PreflightedRunFindingLinks:
        """Validate all link authority without adding, flushing, or writing rows."""

        parsed_links = tuple(
            _revalidate(link, RunFindingLinkV1, label="Run finding link put") for link in links
        )
        planned_findings_by_key = self._validate_planned_finding_overlay(planned_findings)
        planned_artifact_id_set = self._validate_planned_artifact_overlay(planned_artifact_ids)
        if not parsed_links:
            return _PreflightedRunFindingLinks(
                _authority=_RUN_FINDING_PREFLIGHT_AUTHORITY,
                _owner=self,
                _session=self._session,
                _transaction=self._current_transaction(),
                _results=(),
                _row_parameters=(),
            )
        with self._session.no_autoflush:
            return self._preflight_finding_links_many(
                parsed_links,
                planned_findings_by_key=planned_findings_by_key,
                planned_artifact_ids=planned_artifact_id_set,
            )

    def _preflight_finding_links_many(
        self,
        parsed_links: tuple[RunFindingLinkV1, ...],
        *,
        planned_findings_by_key: dict[tuple[str, int], FindingRevisionV1],
        planned_artifact_ids: frozenset[str],
    ) -> _PreflightedRunFindingLinks:

        requested_by_primary: dict[tuple[str, int, int], RunFindingLinkV1] = {}
        primary_by_semantic: dict[tuple[str, str, int], tuple[str, int, int]] = {}
        for parsed in parsed_links:
            primary = (parsed.run_id, parsed.attempt_no, parsed.ordinal)
            retained = requested_by_primary.setdefault(primary, parsed)
            if _canonical_wire(retained) != _canonical_wire(parsed):
                raise IntegrityViolation(
                    "immutable Run finding link differs within one batch",
                    run_id=parsed.run_id,
                    attempt_no=parsed.attempt_no,
                    ordinal=parsed.ordinal,
                )
            semantic = (parsed.run_id, parsed.finding_id, parsed.finding_revision)
            retained_primary = primary_by_semantic.setdefault(semantic, primary)
            if retained_primary != primary:
                raise IntegrityViolation(
                    "Finding revision is already linked at another ordinal",
                    finding_id=parsed.finding_id,
                    finding_revision=parsed.finding_revision,
                )

        existing_by_primary: dict[tuple[str, int, int], RunFindingLinkV1] = {}
        primary_keys = tuple(requested_by_primary)
        primary_chunk_size = _MAX_SQL_IN_ITEMS // 3
        for offset in range(0, len(primary_keys), primary_chunk_size):
            rows = self._session.scalars(
                select(RunFindingLinkRow).where(
                    tuple_(
                        RunFindingLinkRow.run_id,
                        RunFindingLinkRow.attempt_no,
                        RunFindingLinkRow.ordinal,
                    ).in_(primary_keys[offset : offset + primary_chunk_size])
                )
            ).all()
            for row in rows:
                key = (row.run_id, row.attempt_no, row.ordinal)
                existing_by_primary[key] = _parse_finding_link_row(
                    row,
                    expected_run_id=row.run_id,
                    expected_attempt_no=row.attempt_no,
                    expected_ordinal=row.ordinal,
                )

        existing_by_semantic: dict[tuple[str, str, int], tuple[str, int, int]] = {}
        semantic_keys = tuple(primary_by_semantic)
        for offset in range(0, len(semantic_keys), primary_chunk_size):
            rows = self._session.scalars(
                select(RunFindingLinkRow).where(
                    tuple_(
                        RunFindingLinkRow.run_id,
                        RunFindingLinkRow.finding_id,
                        RunFindingLinkRow.finding_revision,
                    ).in_(semantic_keys[offset : offset + primary_chunk_size])
                )
            ).all()
            for row in rows:
                semantic = (row.run_id, row.finding_id, row.finding_revision)
                primary = (row.run_id, row.attempt_no, row.ordinal)
                retained_primary = existing_by_semantic.setdefault(semantic, primary)
                if retained_primary != primary:
                    raise IntegrityViolation(
                        "stored Run finding links duplicate an immutable Finding revision",
                        run_id=row.run_id,
                        finding_id=row.finding_id,
                        finding_revision=row.finding_revision,
                    )

        pending_by_primary: dict[tuple[str, int, int], RunFindingLinkV1] = {}
        results: list[RunFindingLinkV1] = []
        for parsed in parsed_links:
            primary = (parsed.run_id, parsed.attempt_no, parsed.ordinal)
            retained = existing_by_primary.get(primary)
            if retained is None:
                retained = pending_by_primary.get(primary)
            if retained is not None:
                if _canonical_wire(retained) != _canonical_wire(parsed):
                    raise IntegrityViolation(
                        "immutable Run finding link differs from retained content",
                        run_id=parsed.run_id,
                        attempt_no=parsed.attempt_no,
                        ordinal=parsed.ordinal,
                    )
                results.append(retained)
                continue
            semantic = (parsed.run_id, parsed.finding_id, parsed.finding_revision)
            duplicate_primary = existing_by_semantic.get(semantic)
            if duplicate_primary is not None and duplicate_primary != primary:
                raise IntegrityViolation(
                    "Finding revision is already linked at another ordinal",
                    finding_id=parsed.finding_id,
                    finding_revision=parsed.finding_revision,
                )
            pending_by_primary[primary] = parsed
            results.append(parsed)

        pending_links = tuple(pending_by_primary.values())
        attempt_keys = tuple(
            dict.fromkeys((link.run_id, link.attempt_no) for link in pending_links)
        )
        attempt_rows: dict[tuple[str, int], RunAttempt] = {}
        pair_chunk_size = _MAX_SQL_IN_ITEMS // 2
        for offset in range(0, len(attempt_keys), pair_chunk_size):
            rows = self._session.scalars(
                select(RunAttemptRow).where(
                    tuple_(RunAttemptRow.run_id, RunAttemptRow.attempt_no).in_(
                        attempt_keys[offset : offset + pair_chunk_size]
                    )
                )
            ).all()
            for row in rows:
                attempt_rows[(row.run_id, row.attempt_no)] = _parse_attempt_row(
                    row,
                    expected_run_id=row.run_id,
                    expected_attempt_no=row.attempt_no,
                )
        missing_attempt = next(
            (key for key in attempt_keys if key not in attempt_rows),
            None,
        )
        if missing_attempt is not None:
            raise IntegrityViolation(
                "finding link Attempt does not exist",
                run_id=missing_attempt[0],
                attempt_no=missing_attempt[1],
            )

        prompt_routes: dict[tuple[str, int], dict[int, list[int]]] = {
            key: {} for key in attempt_keys
        }
        for offset in range(0, len(attempt_keys), pair_chunk_size):
            rows = self._session.execute(
                select(
                    RunIntermediateArtifactLinkRow.run_id,
                    RunIntermediateArtifactLinkRow.attempt_no,
                    RunIntermediateArtifactLinkRow.call_ordinal,
                    RunIntermediateArtifactLinkRow.route_ordinal,
                )
                .where(
                    tuple_(
                        RunIntermediateArtifactLinkRow.run_id,
                        RunIntermediateArtifactLinkRow.attempt_no,
                    ).in_(attempt_keys[offset : offset + pair_chunk_size])
                )
                .order_by(
                    RunIntermediateArtifactLinkRow.run_id,
                    RunIntermediateArtifactLinkRow.attempt_no,
                    RunIntermediateArtifactLinkRow.call_ordinal,
                    RunIntermediateArtifactLinkRow.route_ordinal,
                )
            ).all()
            for run_id, attempt_no, call_ordinal, route_ordinal in rows:
                prompt_routes[(run_id, attempt_no)].setdefault(call_ordinal, []).append(
                    route_ordinal
                )
        for key, attempt in attempt_rows.items():
            routes_by_call = prompt_routes[key]
            expected_calls = list(range(1, attempt.next_call_ordinal))
            if list(routes_by_call) != expected_calls or any(
                routes != list(range(1, len(routes) + 1)) for routes in routes_by_call.values()
            ):
                raise IntegrityViolation(
                    "Attempt call-ordinal head and route chains are not closed over prompt links",
                    run_id=attempt.run_id,
                    attempt_no=attempt.attempt_no,
                )

        finding_keys = tuple(
            dict.fromkeys(
                (
                    *((link.finding_id, link.finding_revision) for link in parsed_links),
                    *planned_findings_by_key,
                )
            )
        )
        findings: dict[tuple[str, int], FindingRevisionV1] = {}
        for offset in range(0, len(finding_keys), pair_chunk_size):
            rows = self._session.scalars(
                select(FindingRevisionRow).where(
                    tuple_(
                        FindingRevisionRow.finding_id,
                        FindingRevisionRow.revision,
                    ).in_(finding_keys[offset : offset + pair_chunk_size])
                )
            ).all()
            for row in rows:
                key = (row.finding_id, row.revision)
                retained = _parse_linked_finding(row)
                planned = planned_findings_by_key.get(key)
                if planned is not None and _canonical_wire(retained) != _canonical_wire(planned):
                    raise IntegrityViolation(
                        "planned Finding overlay conflicts with retained immutable content",
                        finding_id=row.finding_id,
                        finding_revision=row.revision,
                    )
                findings[key] = retained
        for parsed in parsed_links:
            key = (parsed.finding_id, parsed.finding_revision)
            retained_finding = findings.get(key) or planned_findings_by_key.get(key)
            if retained_finding is None:
                raise IntegrityViolation(
                    "finding link does not match the retained Finding revision",
                    finding_id=parsed.finding_id,
                    finding_revision=parsed.finding_revision,
                )
            if finding_revision_digest(retained_finding) != parsed.finding_digest:
                raise IntegrityViolation(
                    "finding link digest differs from the retained Finding revision",
                    finding_id=parsed.finding_id,
                    finding_revision=parsed.finding_revision,
                )

        evidence_ids = tuple(dict.fromkeys(link.evidence_artifact_id for link in pending_links))
        retained_evidence_ids: set[str] = set()
        for offset in range(0, len(evidence_ids), _MAX_SQL_IN_ITEMS):
            retained_evidence_ids.update(
                self._session.scalars(
                    select(ArtifactRow.artifact_id).where(
                        ArtifactRow.artifact_id.in_(
                            evidence_ids[offset : offset + _MAX_SQL_IN_ITEMS]
                        )
                    )
                ).all()
            )
        missing_evidence = next(
            (
                artifact_id
                for artifact_id in evidence_ids
                if artifact_id not in retained_evidence_ids
                and artifact_id not in planned_artifact_ids
            ),
            None,
        )
        if missing_evidence is not None:
            raise IntegrityViolation(
                "finding link evidence artifact does not exist",
                evidence_artifact_id=missing_evidence,
            )

        return _PreflightedRunFindingLinks(
            _authority=_RUN_FINDING_PREFLIGHT_AUTHORITY,
            _owner=self,
            _session=self._session,
            _transaction=self._current_transaction(),
            _results=tuple(results),
            _row_parameters=tuple(link.model_dump(mode="json") for link in pending_links),
        )

    def put_preflighted_finding_links_many(
        self,
        seal: _PreflightedRunFindingLinks,
    ) -> tuple[RunFindingLinkV1, ...]:
        """Consume one opaque preflight seal using DML-only row parameters."""

        state = None
        consumed = False
        if type(seal) is _PreflightedRunFindingLinks:
            with _RUN_FINDING_PREFLIGHT_LOCK:
                state = _RUN_FINDING_PREFLIGHT_STATES.get(seal)
                consumed = seal in _CONSUMED_RUN_FINDING_PREFLIGHT_SEALS
        if (
            state is None
            or state.owner is not self
            or state.session is not self._session
            or state.transaction is not self._current_transaction()
        ):
            raise IntegrityViolation(
                "Run Finding-link preflight seal does not belong to the current transaction"
            )
        if consumed:
            raise IntegrityViolation("Run Finding-link preflight seal has already been consumed")
        with _RUN_FINDING_PREFLIGHT_LOCK:
            if (
                _RUN_FINDING_PREFLIGHT_STATES.get(seal) is not state
                or seal in _CONSUMED_RUN_FINDING_PREFLIGHT_SEALS
            ):
                raise IntegrityViolation(
                    "Run Finding-link preflight seal has already been consumed"
                )
            _CONSUMED_RUN_FINDING_PREFLIGHT_SEALS.add(seal)

        if state.row_parameters:
            result = self._session.connection().execute(
                RunFindingLinkRow.__table__.insert(),
                state.row_parameters,
            )
            if result.rowcount != len(state.row_parameters):
                raise IntegrityViolation("Run Finding-link batch insert count differs")
            self._session.expire_all()
        return state.results

    def _current_transaction(self) -> object | None:
        return self._session.get_nested_transaction() or self._session.get_transaction()

    @staticmethod
    def _validate_planned_finding_overlay(
        findings: Sequence[FindingRevisionV1],
    ) -> dict[tuple[str, int], FindingRevisionV1]:
        retained: dict[tuple[str, int], FindingRevisionV1] = {}
        digest_keys: dict[str, tuple[str, int]] = {}
        for finding in findings:
            parsed = _revalidate(
                finding,
                FindingRevisionV1,
                label="planned Finding overlay",
            )
            key = (parsed.finding_id, parsed.revision)
            if key in retained:
                raise IntegrityViolation(
                    "planned Finding overlay contains a duplicate revision identity",
                    finding_id=parsed.finding_id,
                    finding_revision=parsed.revision,
                )
            digest = finding_revision_digest(parsed)
            digest_key = digest_keys.setdefault(digest, key)
            if digest_key != key:
                raise IntegrityViolation(
                    "planned Finding overlay contains a digest collision",
                    finding_digest=digest,
                )
            retained[key] = parsed
        return retained

    @staticmethod
    def _validate_planned_artifact_overlay(
        artifact_ids: Sequence[str],
    ) -> frozenset[str]:
        if isinstance(artifact_ids, (str, bytes)):
            raise IntegrityViolation("planned Artifact overlay must be a sequence of ids")
        selected = tuple(
            _require_nonempty(artifact_id, field_name="planned_artifact_id")
            for artifact_id in artifact_ids
        )
        if len(selected) != len(set(selected)):
            raise IntegrityViolation("planned Artifact overlay contains a duplicate id")
        return frozenset(selected)

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

    def get_finding_link_by_revision(
        self,
        *,
        run_id: str,
        finding_id: str,
        finding_revision: int,
    ) -> RunFindingLinkV1 | None:
        """Read the exact immutable Finding revision linked by one Run."""

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        selected_finding_id = _require_nonempty(finding_id, field_name="finding_id")
        selected_revision = _require_positive(
            finding_revision,
            field_name="finding_revision",
        )
        rows = self._session.scalars(
            select(RunFindingLinkRow)
            .where(
                RunFindingLinkRow.run_id == selected_run_id,
                RunFindingLinkRow.finding_id == selected_finding_id,
                RunFindingLinkRow.finding_revision == selected_revision,
            )
            .order_by(RunFindingLinkRow.attempt_no, RunFindingLinkRow.ordinal)
            .limit(2)
        ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise IntegrityViolation(
                "stored Run finding links duplicate an immutable Finding revision",
                run_id=selected_run_id,
                finding_id=selected_finding_id,
                finding_revision=selected_revision,
            )

        row = rows[0]
        parsed = self.get_finding_link(row.run_id, row.attempt_no, row.ordinal)
        if parsed is None or (
            parsed.run_id != selected_run_id
            or parsed.finding_id != selected_finding_id
            or parsed.finding_revision != selected_revision
        ):
            raise IntegrityViolation(
                "stored Run finding link disagrees with its semantic identity",
                run_id=selected_run_id,
                finding_id=selected_finding_id,
                finding_revision=selected_revision,
            )
        return parsed

    def list_finding_links_by_evidence_artifact_ids(
        self,
        evidence_artifact_ids: tuple[str, ...],
        *,
        max_items: int = MAX_COLLECTION_ITEMS,
    ) -> tuple[RunFindingLinkV1, ...]:
        """Enumerate the bounded immutable Finding-link closure for evidence.

        Evidence payload shape is deliberately irrelevant: notably a
        ``playtest-trace@1`` contains no embedded Finding array.  The atomically
        published RunFindingLink rows are the complete producer-side authority.
        """

        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items < 1
            or max_items > MAX_COLLECTION_ITEMS
        ):
            raise IntegrityViolation(
                "Finding link read bound must be within the contract bound",
                minimum=1,
                maximum=MAX_COLLECTION_ITEMS,
            )
        if not isinstance(evidence_artifact_ids, tuple):
            raise IntegrityViolation("Finding evidence selection must be an immutable tuple")
        if len(evidence_artifact_ids) > MAX_COLLECTION_ITEMS:
            raise IntegrityViolation(
                "Finding evidence selection exceeds its contract bound",
                selected_count=len(evidence_artifact_ids),
                maximum=MAX_COLLECTION_ITEMS,
            )
        selected_ids = tuple(
            sorted(
                {
                    _require_nonempty(item, field_name="evidence_artifact_id")
                    for item in evidence_artifact_ids
                }
            )
        )
        if not selected_ids:
            return ()
        rows: list[RunFindingLinkRow] = []
        for start in range(0, len(selected_ids), _MAX_SQL_IN_ITEMS):
            chunk = selected_ids[start : start + _MAX_SQL_IN_ITEMS]
            remaining = max_items + 1 - len(rows)
            if remaining < 1:
                break
            rows.extend(
                self._session.scalars(
                    select(RunFindingLinkRow)
                    .where(RunFindingLinkRow.evidence_artifact_id.in_(chunk))
                    .order_by(
                        RunFindingLinkRow.evidence_artifact_id,
                        RunFindingLinkRow.run_id,
                        RunFindingLinkRow.attempt_no,
                        RunFindingLinkRow.ordinal,
                    )
                    .limit(remaining)
                ).all()
            )
        if len(rows) > max_items:
            raise IntegrityViolation(
                "selected evidence Finding-link closure exceeds its bound",
                maximum=max_items,
            )
        links: list[RunFindingLinkV1] = []
        for row in rows:
            parsed = self.get_finding_link(row.run_id, row.attempt_no, row.ordinal)
            if parsed is None:
                raise IntegrityViolation(
                    "selected evidence Finding link disappeared during one read transaction"
                )
            if parsed.evidence_artifact_id not in selected_ids:
                raise IntegrityViolation(
                    "selected evidence Finding enumeration returned another Artifact"
                )
            links.append(parsed)
        order_keys = tuple(
            (
                link.evidence_artifact_id,
                link.run_id,
                link.attempt_no,
                link.ordinal,
            )
            for link in links
        )
        if order_keys != tuple(sorted(order_keys)) or len(order_keys) != len(set(order_keys)):
            raise IntegrityViolation("selected evidence Finding links are not canonically ordered")
        return tuple(links)

    def put_command(self, record: RunCommandRecordV1) -> RunCommandRecordV1:
        parsed = _revalidate(record, RunCommandRecordV1, label="Run command put")
        existing = self.get_command_by_id(parsed.command.command_id)
        if existing is not None:
            if existing.run_id != parsed.run_id:
                raise Conflict("Run command identity is already retained by another Run")
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

    def preflight_accept_terminal_command(
        self,
        *,
        expected_run_revision: int,
        record: RunCommandRecordV1,
        events: tuple[RunEvent, ...],
        terminal_status: Literal["cancelled"],
        terminal_failure_artifact_id: str,
        terminal_cassette_artifact_id: str | None,
    ) -> object:
        """Seal an inactive cancel command before any terminal participant writes."""

        transaction = self._require_terminal_transaction()
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
            label="terminal Run command acceptance",
        )
        parsed_events = tuple(
            _revalidate(event, RunEvent, label="terminal Run command event") for event in events
        )
        authority = self.get_run_write_authority(parsed_record.run_id)
        if authority is None:
            raise Conflict("Run command acceptance revision did not match")
        run, latest_attempt, active_lease = authority
        if run.revision != expected_revision:
            raise Conflict("Run command acceptance revision did not match")
        if parsed_record.command.expected_run_revision != expected_revision:
            raise IntegrityViolation("Run command embeds a different expected Run revision")
        if self.get_command_by_id(parsed_record.command.command_id) is not None:
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
        expected_attempt_no = run.next_attempt_no - 1 if run.status == "retry_wait" else None
        terminal_event = parsed_events[-1] if parsed_events else None
        if (
            terminal_status != "cancelled"
            or run.status not in {"queued", "retry_wait"}
            or run.current_attempt_no is not None
            or run.concurrency_permit_group_id is not None
            or active_lease is not None
            or (run.status == "queued" and latest_attempt is not None)
            or (
                run.status == "retry_wait"
                and (
                    latest_attempt is None
                    or latest_attempt.attempt_no != expected_attempt_no
                    or latest_attempt.status in _ACTIVE_ATTEMPT_STATUSES
                )
            )
            or parsed_record.command.type != "cancel"
            or parsed_record.status != "applied"
            or len(parsed_events) != 2
            or parsed_record.result_event_seq != parsed_events[0].seq
            or parsed_events[0].event_type != "run.cancel_requested"
            or getattr(parsed_events[0].data, "command_id", None)
            != parsed_record.command.command_id
            or getattr(parsed_events[0].data, "reason_code", None)
            != getattr(parsed_record.command.payload, "reason_code", None)
            or terminal_event is None
            or terminal_event.event_type != "run.cancelled"
            or terminal_event.attempt_no is not None
            or getattr(terminal_event.data, "attempt_no", None) != expected_attempt_no
            or getattr(terminal_event.data, "failure_artifact_id", None)
            != terminal_failure_artifact_id
            or getattr(terminal_event.data, "cause_code", None) != "cancelled"
        ):
            raise IntegrityViolation("inactive cancel acceptance shape is invalid")
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
        updates: dict[str, Any] = {
            "revision": run.revision + 1,
            "next_event_seq": run.next_event_seq + len(parsed_events),
            "updated_at": terminal_event.occurred_at,
            "cancel_requested_at": parsed_events[0].occurred_at,
            "cancel_requested_by": parsed_record.actor.model_dump(mode="json"),
            "status": terminal_status,
            "failure_artifact_id": artifact_id,
            "terminal_cassette_artifact_id": terminal_cassette_id,
            "retry_not_before_utc": None,
            "concurrency_permit_group_id": None,
        }
        updated_run = RunRecord.model_validate({**run.model_dump(mode="python"), **updates})
        run_statement = (
            update(RunRow)
            .where(
                *self._run_fence_predicates(run),
                RunRow.terminal_cassette_artifact_id.is_(None),
            )
            .values(**updates)
        )
        rejection_statement, rejection_parameters = self._preflight_terminal_commands(
            run_id=run.run_id,
            mode="terminal",
            event_seq=terminal_event.seq,
            occurred_at=terminal_event.occurred_at,
            fence=None,
        )
        result = RunCommandAcceptance(updated_run, parsed_record, parsed_events)
        state = _PreflightedTerminalCommandAcceptanceState(
            owner=self,
            session=self._session,
            transaction=transaction,
            result=result,
            run_statement=run_statement,
            event_parameters=tuple(_event_values(event) for event in parsed_events),
            command_parameters=_command_values(parsed_record),
            rejection_statement=rejection_statement,
            rejection_parameters=rejection_parameters,
        )
        return _PreflightedTerminalCommandAcceptance(
            _authority=_TERMINAL_COMMAND_PREFLIGHT_AUTHORITY,
            _state=state,
        )

    def apply_preflighted_terminal_command(
        self,
        seal: object,
    ) -> RunCommandAcceptance:
        """Consume a terminal command seal using CAS/INSERT DML only."""

        if not isinstance(seal, _PreflightedTerminalCommandAcceptance):
            raise IntegrityViolation("terminal command lacks its trusted preflight seal")
        with _TERMINAL_COMMAND_PREFLIGHT_LOCK:
            state = _TERMINAL_COMMAND_PREFLIGHT_STATES.get(seal)
            if state is None:
                raise IntegrityViolation("terminal command lacks its trusted preflight seal")
            if seal in _CONSUMED_TERMINAL_COMMAND_PREFLIGHT_SEALS:
                raise IntegrityViolation("terminal command preflight seal was already consumed")
            if (
                state.owner is not self
                or state.session is not self._session
                or state.transaction is not self._current_transaction()
            ):
                raise IntegrityViolation("terminal command seal belongs to another transaction")
            _CONSUMED_TERMINAL_COMMAND_PREFLIGHT_SEALS.add(seal)

        connection = self._session.connection()
        run_result = connection.execute(state.run_statement)
        if run_result.rowcount != 1:
            raise Conflict("Run command acceptance Run CAS did not match")
        event_result = connection.execute(
            RunEventRow.__table__.insert(),
            state.event_parameters,
        )
        if event_result.rowcount != len(state.event_parameters):
            raise IntegrityViolation("terminal command Event insert count differs")
        try:
            command_result = connection.execute(
                RunCommandRow.__table__.insert(),
                state.command_parameters,
            )
        except IntegrityError as exc:
            record = state.result.record
            context = {
                "run_id": record.run_id,
                "command_id": record.command.command_id,
                "client_id": record.command.client_id,
                "client_seq": record.command.client_seq,
                "idempotency_key": record.command.idempotency_key,
            }
            if getattr(exc.orig, "sqlite_errorcode", None) in {
                sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
                sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            }:
                raise IdempotencyConflict(
                    "terminal Run command identity became bound after preflight",
                    **context,
                ) from exc
            raise IntegrityViolation(
                "terminal Run command insert violated persistence integrity",
                **context,
            ) from exc
        if command_result.rowcount != 1:
            raise IntegrityViolation("terminal command insert count differs")
        if state.rejection_parameters:
            if state.rejection_statement is None:
                raise IntegrityViolation("terminal command rejection DML is incomplete")
            rejection_result = connection.execute(
                state.rejection_statement,
                state.rejection_parameters,
            )
            if rejection_result.rowcount != len(state.rejection_parameters):
                raise Conflict("terminal command rejection CAS did not match")
        self._session.expire_all()
        return state.result

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
        if terminal_status is not None:
            if terminal_failure_artifact_id is None:
                raise IntegrityViolation("terminal command omitted its failure Artifact")
            return self.apply_preflighted_terminal_command(
                self.preflight_accept_terminal_command(
                    expected_run_revision=expected_run_revision,
                    record=record,
                    events=events,
                    terminal_status=terminal_status,
                    terminal_failure_artifact_id=terminal_failure_artifact_id,
                    terminal_cassette_artifact_id=terminal_cassette_artifact_id,
                )
            )
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
        if self.get_command_by_id(parsed_record.command.command_id) is not None:
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

    def get_command_by_id(self, command_id: str) -> RunCommandRecordV1 | None:
        selected_command_id = _require_nonempty(command_id, field_name="command_id")
        row = self._session.execute(
            select(RunCommandRow).where(RunCommandRow.command_id == selected_command_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return _parse_command_row(
            row,
            expected_run_id=row.run_id,
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
        selected_client_seq = _require_command_client_sequence(client_seq)
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

    def _require_terminal_transaction(self) -> object:
        transaction = self._current_transaction()
        if transaction is None:
            # ``SqliteUnitOfWork`` starts ``BEGIN IMMEDIATE`` on the bound
            # Connection before constructing the Session.  Joining that already
            # active transaction creates the SessionTransaction identity without
            # issuing a statement.
            self._session.connection()
            transaction = self._current_transaction()
        if transaction is None:
            raise IntegrityViolation(
                "Run terminal closure preflight requires an active transaction"
            )
        return transaction

    def _load_terminal_active_authority(
        self,
        fence: _AttemptWriteFence,
        *,
        occurred_at: str,
        allowed_statuses: frozenset[str],
        allow_expired_lease: bool = False,
        allow_deadline_exceeded: bool = False,
    ) -> tuple[RunRecord, RunAttempt, RunLease]:
        """Load the bounded mutable active head without scanning immutable history.

        ``RunRow.next_*`` is the persisted head authority.  The terminal Run CAS
        fences all three counters, the Attempt CAS fences ``next_call_ordinal``,
        and Event/Attempt uniqueness rejects a conflicting insert.  Recounting all
        preceding Events, Attempts, or prompt links here would turn retry-heavy
        terminal work into cumulative quadratic time without adding write authority.
        """

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
        rows = self._session.execute(
            select(RunRow, RunAttemptRow, RunLeaseRow)
            .join(
                RunAttemptRow,
                and_(
                    RunAttemptRow.run_id == RunRow.run_id,
                    RunAttemptRow.attempt_no == attempt_no,
                ),
            )
            .join(
                RunLeaseRow,
                and_(
                    RunLeaseRow.run_id == RunRow.run_id,
                    RunLeaseRow.attempt_no == attempt_no,
                    RunLeaseRow.lease_id == lease_id,
                ),
            )
            .where(RunRow.run_id == run_id)
            .execution_options(populate_existing=True)
        ).all()
        if len(rows) != 1:
            raise Conflict("Run attempt write fence did not match")
        run_row, attempt_row, lease_row = rows[0]
        run = _parse_run_row(run_row, expected_run_id=run_id)
        attempt = _parse_attempt_row(
            attempt_row,
            expected_run_id=run_id,
            expected_attempt_no=attempt_no,
        )
        lease = _parse_lease_row(lease_row, expected_lease_id=lease_id)
        if (
            run.revision != expected_revision
            or run.status not in allowed_statuses
            or run.current_attempt_no != attempt_no
            or run.concurrency_permit_group_id is None
            or run.next_attempt_no != attempt_no + 1
            or run.next_fencing_token != fencing_token + 1
            or attempt.status != run.status
            or attempt.fencing_token != fencing_token
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
            if occurred >= _parse_utc(
                run.overall_deadline_utc,
                field_name="run.overall_deadline_utc",
            ):
                raise Conflict("Run attempt overall deadline is exhausted")
            if attempt.attempt_deadline_utc is not None and occurred >= _parse_utc(
                attempt.attempt_deadline_utc,
                field_name="attempt.attempt_deadline_utc",
            ):
                raise Conflict("Run attempt deadline is exhausted")
        return run, attempt, lease

    def _load_terminal_inactive_authority(
        self,
        *,
        run_id: str,
        expected_run_revision: int,
    ) -> tuple[RunRecord, RunAttempt | None]:
        """Load the bounded inactive head and prove there is no active lease.

        The outer Attempt join is the one PK-addressed latest head
        (``next_attempt_no - 1``), never the complete attempt history.
        """

        selected_run_id = _require_nonempty(run_id, field_name="run_id")
        expected_revision = _require_positive(
            expected_run_revision,
            field_name="expected_run_revision",
        )
        rows = self._session.execute(
            select(RunRow, RunAttemptRow, RunLeaseRow)
            .outerjoin(
                RunAttemptRow,
                and_(
                    RunAttemptRow.run_id == RunRow.run_id,
                    RunAttemptRow.attempt_no == RunRow.next_attempt_no - 1,
                ),
            )
            .outerjoin(
                RunLeaseRow,
                and_(
                    RunLeaseRow.run_id == RunRow.run_id,
                    RunLeaseRow.status == "active",
                ),
            )
            .where(RunRow.run_id == selected_run_id)
            .execution_options(populate_existing=True)
        ).all()
        if len(rows) != 1:
            raise Conflict("inactive Run terminal compare-and-set did not match")
        run_row, attempt_row, active_lease_row = rows[0]
        run = _parse_run_row(run_row, expected_run_id=selected_run_id)
        latest_attempt = (
            None
            if attempt_row is None
            else _parse_attempt_row(
                attempt_row,
                expected_run_id=selected_run_id,
                expected_attempt_no=attempt_row.attempt_no,
            )
        )
        if (
            run.revision != expected_revision
            or run.status not in {"queued", "retry_wait"}
            or run.current_attempt_no is not None
            or run.concurrency_permit_group_id is not None
            or active_lease_row is not None
            or (run.next_attempt_no == 1) != (latest_attempt is None)
        ):
            raise Conflict("inactive Run terminal compare-and-set did not match")
        if latest_attempt is not None and (
            latest_attempt.attempt_no != run.next_attempt_no - 1
            or latest_attempt.fencing_token != run.next_fencing_token - 1
            or latest_attempt.status in _ACTIVE_ATTEMPT_STATUSES
        ):
            raise IntegrityViolation("inactive Run attempt head is inconsistent")
        return run, latest_attempt

    def _preflight_terminal_commands(
        self,
        *,
        run_id: str,
        mode: Literal["retry", "terminal"],
        event_seq: int,
        occurred_at: str,
        fence: _AttemptWriteFence | None,
    ) -> tuple[object | None, tuple[dict[str, object], ...]]:
        statuses = ("claimed",) if mode == "retry" else ("pending", "claimed")
        rows = self._session.scalars(
            select(RunCommandRow)
            .where(
                RunCommandRow.run_id == run_id,
                RunCommandRow.status.in_(statuses),
            )
            .order_by(RunCommandRow.created_at, RunCommandRow.command_id)
            .limit(MAX_COLLECTION_ITEMS + 1)
            .execution_options(populate_existing=True)
        ).all()
        if len(rows) > MAX_COLLECTION_ITEMS:
            raise IntegrityViolation("Run command authority exceeds its hard cap")
        parameters: list[dict[str, object]] = []
        for row in rows:
            record = _parse_command_row(
                row,
                expected_run_id=run_id,
                expected_command_id=row.command_id,
            )
            if mode == "retry":
                if fence is None or (
                    record.claimed_attempt_no != fence.attempt_no
                    or record.claimed_fencing_token != fence.fencing_token
                ):
                    raise IntegrityViolation("claimed command is bound to a stale Run attempt")
                updated = RunCommandRecordV1.model_validate(
                    {
                        **record.model_dump(mode="python"),
                        "status": "pending",
                        "revision": record.revision + 1,
                        "claimed_at": None,
                        "claimed_attempt_no": None,
                        "claimed_fencing_token": None,
                    }
                )
                parameters.append(
                    {
                        "cas_run_id": run_id,
                        "cas_command_id": record.command.command_id,
                        "cas_status": "claimed",
                        "cas_revision": record.revision,
                        "cas_attempt_no": fence.attempt_no,
                        "cas_fencing_token": fence.fencing_token,
                        "new_status": "pending",
                        "new_revision": updated.revision,
                        "new_claimed_at": None,
                        "new_claimed_attempt_no": None,
                        "new_claimed_fencing_token": None,
                    }
                )
            else:
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
                parameters.append(
                    {
                        "cas_run_id": run_id,
                        "cas_command_id": record.command.command_id,
                        "cas_status": record.status,
                        "cas_revision": record.revision,
                        "new_status": "rejected",
                        "new_revision": updated.revision,
                        "new_applied_at": occurred_at,
                        "new_result_event_seq": event_seq,
                        "new_rejection_code": "run_terminal",
                    }
                )
        if not parameters:
            return None, ()
        if mode == "retry":
            statement = (
                update(RunCommandRow)
                .where(
                    RunCommandRow.run_id == bindparam("cas_run_id"),
                    RunCommandRow.command_id == bindparam("cas_command_id"),
                    RunCommandRow.status == bindparam("cas_status"),
                    RunCommandRow.revision == bindparam("cas_revision"),
                    RunCommandRow.claimed_attempt_no == bindparam("cas_attempt_no"),
                    RunCommandRow.claimed_fencing_token == bindparam("cas_fencing_token"),
                )
                .values(
                    status=bindparam("new_status"),
                    revision=bindparam("new_revision"),
                    claimed_at=bindparam("new_claimed_at"),
                    claimed_attempt_no=bindparam("new_claimed_attempt_no"),
                    claimed_fencing_token=bindparam("new_claimed_fencing_token"),
                )
            )
        else:
            statement = (
                update(RunCommandRow)
                .where(
                    RunCommandRow.run_id == bindparam("cas_run_id"),
                    RunCommandRow.command_id == bindparam("cas_command_id"),
                    RunCommandRow.status == bindparam("cas_status"),
                    RunCommandRow.revision == bindparam("cas_revision"),
                )
                .values(
                    status=bindparam("new_status"),
                    revision=bindparam("new_revision"),
                    applied_at=bindparam("new_applied_at"),
                    result_event_seq=bindparam("new_result_event_seq"),
                    rejection_code=bindparam("new_rejection_code"),
                )
            )
        return statement, tuple(parameters)

    @staticmethod
    def _validate_preflight_terminal_events(
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

    def _issue_terminal_closure(
        self,
        *,
        transaction: object,
        result: RunAttemptClose | RunTerminal,
        run: RunRecord,
        updated_run: RunRecord,
        attempt: RunAttempt | None,
        updated_attempt: RunAttempt | None,
        lease: RunLease | None,
        lease_status: Literal["closed", "expired"] | None,
        released_at: str,
        events: tuple[RunEvent, ...],
        command_mode: Literal["retry", "terminal"],
        command_statement: object | None,
        command_parameters: tuple[dict[str, object], ...],
    ) -> _PreflightedRunTerminalClosure:
        run_values = _run_values(updated_run)
        previous_run_values = _run_values(run)
        run_statement = (
            update(RunRow)
            .where(
                *self._run_fence_predicates(run),
                RunRow.terminal_cassette_artifact_id.is_(None),
            )
            .values(
                **{
                    key: value
                    for key, value in run_values.items()
                    if previous_run_values[key] != value
                }
            )
        )
        attempt_statement: object | None = None
        lease_statement: object | None = None
        if attempt is not None:
            if updated_attempt is None or lease is None or lease_status is None:
                raise IntegrityViolation("active terminal closure omitted attempt authority")
            attempt_values = _attempt_values(updated_attempt)
            previous_attempt_values = _attempt_values(attempt)
            attempt_statement = (
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
                .values(
                    **{
                        key: value
                        for key, value in attempt_values.items()
                        if previous_attempt_values[key] != value
                    }
                )
            )
            lease_statement = (
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
        state = _PreflightedRunTerminalClosureState(
            owner=self,
            session=self._session,
            transaction=transaction,
            result=result,
            run_statement=run_statement,
            attempt_statement=attempt_statement,
            lease_statement=lease_statement,
            event_parameters=tuple(_event_values(event) for event in events),
            command_mode=command_mode,
            command_statement=command_statement,
            command_parameters=command_parameters,
        )
        return _PreflightedRunTerminalClosure(
            _authority=_RUN_TERMINAL_PREFLIGHT_AUTHORITY,
            _state=state,
        )

    def apply_preflighted_terminal_closure(
        self,
        seal: object,
    ) -> RunAttemptClose | RunTerminal:
        """Consume one trusted closure using CAS/INSERT DML only."""

        if not isinstance(seal, _PreflightedRunTerminalClosure):
            raise IntegrityViolation("Run terminal closure lacks its trusted preflight seal")
        with _RUN_TERMINAL_PREFLIGHT_LOCK:
            state = _RUN_TERMINAL_PREFLIGHT_STATES.get(seal)
            if state is None:
                raise IntegrityViolation("Run terminal closure lacks its trusted preflight seal")
            if seal in _CONSUMED_RUN_TERMINAL_PREFLIGHT_SEALS:
                raise IntegrityViolation("Run terminal closure seal has already been consumed")
            if (
                state.owner is not self
                or state.session is not self._session
                or state.transaction is not self._current_transaction()
            ):
                raise IntegrityViolation("Run terminal closure seal belongs to another transaction")
            _CONSUMED_RUN_TERMINAL_PREFLIGHT_SEALS.add(seal)

        connection = self._session.connection()
        run_result = connection.execute(state.run_statement)
        if run_result.rowcount != 1:
            raise Conflict("Run terminal closure Run CAS did not match")
        if state.attempt_statement is not None:
            attempt_result = connection.execute(state.attempt_statement)
            if attempt_result.rowcount != 1:
                raise Conflict("Run terminal closure Attempt CAS did not match")
        if state.lease_statement is not None:
            lease_result = connection.execute(state.lease_statement)
            if lease_result.rowcount != 1:
                raise Conflict("Run terminal closure Lease CAS did not match")
        if state.event_parameters:
            event_result = connection.execute(
                RunEventRow.__table__.insert(),
                state.event_parameters,
            )
            if event_result.rowcount != len(state.event_parameters):
                raise IntegrityViolation("Run terminal closure Event insert count differs")
        if state.command_parameters:
            if state.command_statement is None:
                raise IntegrityViolation("Run terminal closure command DML is incomplete")
            command_result = connection.execute(
                state.command_statement,
                state.command_parameters,
            )
            if command_result.rowcount != len(state.command_parameters):
                if state.command_mode == "retry":
                    raise Conflict("Run retry command reset CAS did not match")
                raise Conflict("Run terminal command rejection CAS did not match")
        self._session.expire_all()
        return state.result

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
        terminal = run.status in {"succeeded", "failed", "cancelled", "timed_out"}
        if terminal:
            event_head_invalid = (
                event_count < 1
                or not isinstance(first_event, int)
                or first_event < 1
                or last_event != expected_event_count
                or event_count != expected_event_count - first_event + 1
            )
        else:
            event_head_invalid = (
                event_count != expected_event_count
                or first_event != 1
                or last_event != expected_event_count
            )
        if event_head_invalid:
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
        rows = self._session.execute(
            select(
                RunIntermediateArtifactLinkRow.call_ordinal,
                RunIntermediateArtifactLinkRow.route_ordinal,
            )
            .where(
                RunIntermediateArtifactLinkRow.run_id == attempt.run_id,
                RunIntermediateArtifactLinkRow.attempt_no == attempt.attempt_no,
            )
            .order_by(
                RunIntermediateArtifactLinkRow.call_ordinal,
                RunIntermediateArtifactLinkRow.route_ordinal,
            )
        ).all()
        routes_by_call: dict[int, list[int]] = {}
        for call_ordinal, route_ordinal in rows:
            routes_by_call.setdefault(call_ordinal, []).append(route_ordinal)
        expected_calls = list(range(1, attempt.next_call_ordinal))
        if list(routes_by_call) != expected_calls or any(
            routes != list(range(1, len(routes) + 1)) for routes in routes_by_call.values()
        ):
            raise IntegrityViolation(
                "Attempt call-ordinal head and route chains are not closed over prompt links",
                run_id=attempt.run_id,
                attempt_no=attempt.attempt_no,
            )
