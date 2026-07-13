"""Transactional Run creation, first claim, and ordinal-bound publication commands."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.jobs import (
    RetryPolicyRefV1,
    RetryPolicySnapshot,
    RunAttempt,
    RunCommandRecordV1,
    RunDispatchTraceCarrierV1,
    RunEvent,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunKindDefinition,
    RunLease,
    RunPayloadEnvelope,
    RunQueuedDataV1,
    RunRecord,
    canonical_payload_hash,
    outcome_policy_set_digest,
    run_kind_definition_digest,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import UtcClock
from gameforge.platform.runs.state import (
    validate_claim_transition,
    validate_prompt_link_binding,
    validate_queued_creation,
    validate_run_kind_binding,
)


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(gt=0)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RunCreateRequest(_FrozenModel):
    run_id: NonEmptyStr
    kind: RunKindRef
    creation_mode: Literal[
        "generic_runs_endpoint",
        "resource_endpoint_only",
        "internal_only",
    ]
    idempotency_scope: NonEmptyStr
    idempotency_key: NonEmptyStr
    request_hash: Sha256Hex
    payload: RunPayloadEnvelope
    dispatch_trace_carrier: RunDispatchTraceCarrierV1 | None = None
    initiated_by: AuditActor
    queue_deadline_utc: NonEmptyStr
    attempt_timeout_ns: PositiveInt
    overall_deadline_utc: NonEmptyStr


class RunCreateResult(_FrozenModel):
    run: RunRecord
    replayed: bool


class RunClaimRequest(_FrozenModel):
    worker: AuditActor
    lease_id: NonEmptyStr
    lease_duration_ns: PositiveInt
    trace_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _trusted_worker_kind(self) -> "RunClaimRequest":
        if self.worker.principal_kind not in {"service", "system"}:
            raise ValueError("Run claims require a service or system worker")
        return self


class RunClaimResult(_FrozenModel):
    previous: RunRecord
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


class PromptRenderPublicationRequest(_FrozenModel):
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    expected_fencing_token: PositiveInt
    artifact_id: NonEmptyStr
    request_hash: Sha256Hex
    idempotency_scope: NonEmptyStr
    idempotency_key: NonEmptyStr


class PromptRenderPublicationResult(_FrozenModel):
    link: RunIntermediateArtifactLinkV1
    replayed: bool


class PersistedRunClaim(Protocol):
    @property
    def run(self) -> RunRecord: ...

    @property
    def attempt(self) -> RunAttempt: ...

    @property
    def lease(self) -> RunLease: ...

    @property
    def event(self) -> RunEvent: ...


class RunRepository(Protocol):
    """Strict transaction-bound Run persistence; implementations never commit."""

    def get(self, run_id: str) -> RunRecord | None: ...

    def get_by_idempotency(self, *, scope: str, key: str) -> RunRecord | None: ...

    def create_queued(self, run: RunRecord, initial_event: RunEvent) -> RunRecord: ...

    def get_claim_candidate(self, *, now_utc: str) -> RunRecord | None: ...

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
    ) -> PersistedRunClaim: ...

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None: ...

    def get_current_lease(self, run_id: str) -> RunLease | None: ...

    def get_event(self, run_id: str, seq: int) -> RunEvent | None: ...

    def list_events(
        self,
        run_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[RunEvent, ...]: ...

    def get_intermediate_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
    ) -> RunIntermediateArtifactLinkV1 | None: ...

    def put_intermediate_link(
        self,
        link: RunIntermediateArtifactLinkV1,
    ) -> RunIntermediateArtifactLinkV1: ...

    def get_finding_link(
        self,
        run_id: str,
        attempt_no: int,
        ordinal: int,
    ) -> RunFindingLinkV1 | None: ...

    def put_finding_link(self, link: RunFindingLinkV1) -> RunFindingLinkV1: ...

    def get_command(self, run_id: str, command_id: str) -> RunCommandRecordV1 | None: ...

    def put_command(self, record: RunCommandRecordV1) -> RunCommandRecordV1: ...


class RunRegistryGateway(Protocol):
    """Resolve exact retained definitions and validate every typed binding set."""

    def get_run_kind(self, kind: RunKindRef) -> RunKindDefinition | None: ...

    def get_retry_policy(self, ref: RetryPolicyRefV1) -> RetryPolicySnapshot | None: ...

    def validate_payload_bindings(
        self,
        *,
        payload: RunPayloadEnvelope,
        definition: RunKindDefinition,
    ) -> None: ...


class RunAdmissionGateway(Protocol):
    """M4b composition point for persistent budget holds and execution permits."""

    def reserve_run_budget(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        request_hash: str,
        initiated_by: AuditActor,
    ) -> str: ...

    def acquire_execution_permits(
        self,
        *,
        run: RunRecord,
        attempt_no: int,
        fencing_token: int,
        worker_principal_id: str,
        expires_at: str,
    ) -> str: ...


class RunPublicationGateway(Protocol):
    """Same-UoW audit/publication hooks; there is no permissive no-op adapter."""

    def record_run_created(self, *, run: RunRecord, event: RunEvent) -> None: ...

    def record_run_claimed(
        self,
        *,
        previous: RunRecord,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        event: RunEvent,
        actor: AuditActor,
    ) -> None: ...

    def get_prompt_replay(
        self,
        *,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> RunIntermediateArtifactLinkV1 | None: ...

    def publish_prompt_rendered(
        self,
        *,
        link: RunIntermediateArtifactLinkV1,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> RunIntermediateArtifactLinkV1:
        """Atomically consume the call head and retain the exact link/replay key.

        Task 14 composes prepared Artifact/ObjectRef, deadline, and audit publication
        around this transaction-bound participant.
        """
        ...


@dataclass(slots=True)
class RunCommandCapabilities:
    runs: RunRepository | None
    registry: RunRegistryGateway | None
    admission: RunAdmissionGateway | None
    publication: RunPublicationGateway | None


class RunUnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...


CapabilityBinder = Callable[[Any], RunCommandCapabilities]


def _required[T](value: T | None, name: str) -> T:
    if value is None:
        raise IntegrityViolation(f"{name} Run command capability is unavailable")
    return value


def _utc_now(clock: UtcClock) -> datetime:
    try:
        now = clock.now_utc()
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("Run command clock must return UTC") from exc
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("Run command clock must return UTC")
    return now.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise IntegrityViolation(f"{field_name} must be a canonical UTC timestamp") from exc
    if (
        not value.endswith("Z")
        or parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
        or _utc_text(parsed) != value
    ):
        raise IntegrityViolation(f"{field_name} must be a canonical UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _resolve_bindings(
    *,
    run: RunRecord,
    registry: RunRegistryGateway,
) -> tuple[RunKindDefinition, RetryPolicySnapshot]:
    definition = registry.get_run_kind(run.kind)
    if definition is None:
        raise IntegrityViolation("Run kind is not retained in the exact registry")
    retry = registry.get_retry_policy(run.retry_policy)
    if retry is None:
        raise IntegrityViolation("Run retry policy is not retained in the exact registry")
    validate_run_kind_binding(run=run, definition=definition, retry_policy=retry)
    registry.validate_payload_bindings(payload=run.payload, definition=definition)
    return definition, retry


class RunCommandService:
    def __init__(
        self,
        *,
        unit_of_work: RunUnitOfWork,
        bind_capabilities: CapabilityBinder,
        clock: UtcClock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._clock = clock

    def create_run(self, request: RunCreateRequest) -> RunCreateResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            retained = runs.get_by_idempotency(
                scope=request.idempotency_scope,
                key=request.idempotency_key,
            )
            if retained is not None:
                registry = _required(capabilities.registry, "registry")
                definition, _ = _resolve_bindings(run=retained, registry=registry)
                self._validate_create_replay(
                    request=request,
                    retained=retained,
                    definition=definition,
                )
                return RunCreateResult(run=retained, replayed=True)

            registry = _required(capabilities.registry, "registry")
            admission = _required(capabilities.admission, "admission")
            publication = _required(capabilities.publication, "publication")
            definition = registry.get_run_kind(request.kind)
            if definition is None:
                raise IntegrityViolation("Run kind is not retained in the exact registry")
            if definition.status != "active":
                raise IntegrityViolation("Run kind definition is not active")
            if request.creation_mode != definition.creation_mode:
                raise IntegrityViolation("Run kind is not allowed at this creation surface")
            if request.payload.payload_schema_version != definition.payload_schema_id:
                raise IntegrityViolation("Run payload schema differs from its Run kind")
            if request.payload.llm_execution_mode not in definition.allowed_llm_execution_modes:
                raise IntegrityViolation("Run execution mode is not allowed by its Run kind")
            if definition.seed_policy == "required" and request.payload.seed is None:
                raise IntegrityViolation("Run kind requires an explicit seed")
            if definition.seed_policy == "forbidden" and request.payload.seed is not None:
                raise IntegrityViolation("Run kind forbids an explicit seed")
            retry = registry.get_retry_policy(definition.retry_policy)
            if retry is None:
                raise IntegrityViolation("Run retry policy is not retained in the exact registry")
            registry.validate_payload_bindings(
                payload=request.payload,
                definition=definition,
            )

            now = _utc_now(self._clock)
            now_text = _utc_text(now)
            queue_deadline = _parse_utc(
                request.queue_deadline_utc,
                field_name="queue_deadline_utc",
            )
            overall_deadline = _parse_utc(
                request.overall_deadline_utc,
                field_name="overall_deadline_utc",
            )
            if not now < queue_deadline <= overall_deadline:
                raise IntegrityViolation("Run deadlines must satisfy now < queue <= overall")
            hold_group_id = admission.reserve_run_budget(
                run_id=request.run_id,
                budget_set_snapshot_id=request.payload.budget_set_snapshot_id,
                request_hash=request.request_hash,
                initiated_by=request.initiated_by,
            )
            if not hold_group_id:
                raise IntegrityViolation("Run admission returned an empty budget hold group")

            run = RunRecord(
                run_id=request.run_id,
                kind=request.kind,
                status="queued",
                revision=1,
                idempotency_scope=request.idempotency_scope,
                idempotency_key=request.idempotency_key,
                request_hash=request.request_hash,
                payload=request.payload,
                payload_hash=canonical_payload_hash(request.payload),
                run_kind_definition_digest=run_kind_definition_digest(definition),
                outcome_policy_set_digest=outcome_policy_set_digest(
                    request.kind,
                    definition.outcome_policies,
                ),
                migration_capability_matrix=definition.migration_capability_matrix,
                failure_classifier=definition.failure_classifier,
                dispatch_trace_carrier=request.dispatch_trace_carrier,
                initiated_by=request.initiated_by,
                queue_deadline_utc=request.queue_deadline_utc,
                attempt_timeout_ns=request.attempt_timeout_ns,
                overall_deadline_utc=request.overall_deadline_utc,
                next_attempt_no=1,
                next_fencing_token=1,
                next_event_seq=2,
                budget_set_snapshot_id=request.payload.budget_set_snapshot_id,
                run_budget_hold_group_id=hold_group_id,
                retry_policy=definition.retry_policy,
                max_attempts=retry.max_attempts,
                created_at=now_text,
                updated_at=now_text,
            )
            validate_run_kind_binding(
                run=run,
                definition=definition,
                retry_policy=retry,
            )
            initial_event = RunEvent(
                run_id=run.run_id,
                seq=1,
                event_type="run.queued",
                occurred_at=now_text,
                data_schema_version="run-queued@1",
                data=RunQueuedDataV1(
                    run_kind=run.kind,
                    queue_deadline_utc=run.queue_deadline_utc,
                    overall_deadline_utc=run.overall_deadline_utc,
                ),
            )
            validate_queued_creation(run=run, initial_event=initial_event)
            stored = runs.create_queued(run, initial_event)
            if (
                stored != run
                or runs.get(run.run_id) != run
                or runs.get_by_idempotency(
                    scope=run.idempotency_scope,
                    key=run.idempotency_key,
                )
                != run
                or runs.get_event(run.run_id, 1) != initial_event
            ):
                raise IntegrityViolation("Run repository did not retain the exact queued publication")
            publication.record_run_created(run=run, event=initial_event)
            return RunCreateResult(run=run, replayed=False)

    def claim_next(self, request: RunClaimRequest) -> RunClaimResult | None:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            registry = _required(capabilities.registry, "registry")
            admission = _required(capabilities.admission, "admission")
            publication = _required(capabilities.publication, "publication")
            now = _utc_now(self._clock)
            now_text = _utc_text(now)
            previous = runs.get_claim_candidate(now_utc=now_text)
            if previous is None:
                return None
            if previous.status != "queued":
                raise IntegrityViolation("Task 13 claim candidate is not queued")
            _resolve_bindings(run=previous, registry=registry)
            queue_deadline = _parse_utc(
                previous.queue_deadline_utc,
                field_name="stored queue_deadline_utc",
            )
            overall_deadline = _parse_utc(
                previous.overall_deadline_utc,
                field_name="stored overall_deadline_utc",
            )
            if now >= queue_deadline or now >= overall_deadline:
                raise IntegrityViolation("Run repository returned an expired claim candidate")
            remaining = overall_deadline - now
            remaining_microseconds = (
                remaining.days * 86_400_000_000
                + remaining.seconds * 1_000_000
                + remaining.microseconds
            )
            requested_microseconds = (request.lease_duration_ns + 999) // 1_000
            if requested_microseconds >= remaining_microseconds:
                lease_expiry = overall_deadline
            else:
                lease_expiry = now + timedelta(microseconds=requested_microseconds)
            expires_at = _utc_text(lease_expiry)
            permit_group_id = admission.acquire_execution_permits(
                run=previous,
                attempt_no=previous.next_attempt_no,
                fencing_token=previous.next_fencing_token,
                worker_principal_id=request.worker.principal_id,
                expires_at=expires_at,
            )
            if not permit_group_id:
                raise IntegrityViolation("Run admission returned an empty permit group")
            persisted = runs.claim(
                run_id=previous.run_id,
                expected_revision=previous.revision,
                worker_principal_id=request.worker.principal_id,
                lease_id=request.lease_id,
                acquired_at=now_text,
                expires_at=expires_at,
                permit_group_id=permit_group_id,
                trace_id=request.trace_id,
            )
            validate_claim_transition(
                previous=previous,
                current=persisted.run,
                attempt=persisted.attempt,
                lease=persisted.lease,
                event=persisted.event,
                permit_group_id=permit_group_id,
                acquired_at=now_text,
                expires_at=expires_at,
                worker_principal_id=request.worker.principal_id,
                lease_id=request.lease_id,
                trace_id=request.trace_id,
            )
            if (
                runs.get(previous.run_id) != persisted.run
                or runs.get_attempt(previous.run_id, persisted.attempt.attempt_no)
                != persisted.attempt
                or runs.get_current_lease(previous.run_id) != persisted.lease
                or runs.get_event(previous.run_id, persisted.event.seq) != persisted.event
            ):
                raise IntegrityViolation("Run repository did not retain the exact claim publication")
            publication.record_run_claimed(
                previous=previous,
                run=persisted.run,
                attempt=persisted.attempt,
                lease=persisted.lease,
                event=persisted.event,
                actor=request.worker,
            )
            return RunClaimResult(
                previous=previous,
                run=persisted.run,
                attempt=persisted.attempt,
                lease=persisted.lease,
                event=persisted.event,
            )

    def publish_prompt_rendered(
        self,
        request: PromptRenderPublicationRequest,
    ) -> PromptRenderPublicationResult:
        """Consume a call head only through an atomic publication gateway.

        Task 14 adds the complete deadline, prepared Artifact/ObjectRef, and audit
        publication guard. This Task-13 primitive deliberately has no bare ordinal
        allocator and cannot be composed without a transaction-bound publisher.
        """

        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            publication = _required(capabilities.publication, "publication")
            replay = publication.get_prompt_replay(
                idempotency_scope=request.idempotency_scope,
                idempotency_key=request.idempotency_key,
                request_hash=request.request_hash,
            )
            if replay is not None:
                registry = _required(capabilities.registry, "registry")
                run = runs.get(request.run_id)
                if run is None:
                    raise IntegrityViolation("prompt replay Run does not exist")
                _resolve_bindings(run=run, registry=registry)
                attempt = runs.get_attempt(request.run_id, request.attempt_no)
                if attempt is None:
                    raise IntegrityViolation("prompt replay attempt does not exist")
                authoritative_link = runs.get_intermediate_link(
                    replay.run_id,
                    replay.attempt_no,
                    replay.call_ordinal,
                )
                self._validate_prompt_replay(
                    request=request,
                    replay=replay,
                    run=run,
                    attempt=attempt,
                    authoritative_link=authoritative_link,
                )
                return PromptRenderPublicationResult(link=replay, replayed=True)

            registry = _required(capabilities.registry, "registry")
            run = runs.get(request.run_id)
            if run is None:
                raise IntegrityViolation("prompt publication Run does not exist")
            _resolve_bindings(run=run, registry=registry)
            attempt = runs.get_attempt(request.run_id, request.attempt_no)
            if attempt is None:
                raise IntegrityViolation("prompt publication attempt does not exist")
            if attempt.fencing_token != request.expected_fencing_token:
                raise Conflict(
                    "prompt publication fencing token differs",
                    expected_fencing_token=request.expected_fencing_token,
                    actual_fencing_token=attempt.fencing_token,
                )
            lease = runs.get_current_lease(request.run_id)
            if lease is None:
                raise IntegrityViolation("prompt publication has no active lease")
            link = RunIntermediateArtifactLinkV1(
                run_id=request.run_id,
                attempt_no=request.attempt_no,
                call_ordinal=attempt.next_call_ordinal,
                artifact_id=request.artifact_id,
                role="prompt_rendered",
                request_hash=request.request_hash,
                fencing_token=request.expected_fencing_token,
                published_at=_utc_text(_utc_now(self._clock)),
            )
            validate_prompt_link_binding(
                run=run,
                attempt=attempt,
                lease=lease,
                link=link,
            )
            stored = publication.publish_prompt_rendered(
                link=link,
                idempotency_scope=request.idempotency_scope,
                idempotency_key=request.idempotency_key,
                request_hash=request.request_hash,
            )
            if stored != link:
                raise IntegrityViolation("prompt publication gateway retained a different link")
            retained = runs.get_intermediate_link(
                link.run_id,
                link.attempt_no,
                link.call_ordinal,
            )
            advanced_attempt = runs.get_attempt(link.run_id, link.attempt_no)
            if retained != link or advanced_attempt is None:
                raise IntegrityViolation("prompt publication did not atomically retain its link")
            expected_attempt = RunAttempt.model_validate(
                {
                    **attempt.model_dump(mode="python"),
                    "next_call_ordinal": attempt.next_call_ordinal + 1,
                }
            )
            if advanced_attempt != expected_attempt:
                raise IntegrityViolation("prompt publication did not consume exactly one call head")
            return PromptRenderPublicationResult(link=link, replayed=False)

    @staticmethod
    def _validate_create_replay(
        *,
        request: RunCreateRequest,
        retained: RunRecord,
        definition: RunKindDefinition,
    ) -> None:
        if retained.request_hash != request.request_hash:
            raise Conflict(
                "Run idempotency key is bound to a different request",
                expected_request_hash=request.request_hash,
                actual_request_hash=retained.request_hash,
            )
        semantic_fields = {
            "kind": request.kind,
            "idempotency_scope": request.idempotency_scope,
            "idempotency_key": request.idempotency_key,
            "payload": request.payload,
            "initiated_by": request.initiated_by,
            "queue_deadline_utc": request.queue_deadline_utc,
            "attempt_timeout_ns": request.attempt_timeout_ns,
            "overall_deadline_utc": request.overall_deadline_utc,
        }
        for field_name, expected in semantic_fields.items():
            if getattr(retained, field_name) != expected:
                raise Conflict(
                    "Run idempotency request differs despite a matching request hash",
                    field_name=field_name,
                )
        if request.creation_mode != definition.creation_mode:
            raise IntegrityViolation(
                "Run idempotency replay is not allowed at this creation surface"
            )

    @staticmethod
    def _validate_prompt_replay(
        *,
        request: PromptRenderPublicationRequest,
        replay: RunIntermediateArtifactLinkV1,
        run: RunRecord,
        attempt: RunAttempt,
        authoritative_link: RunIntermediateArtifactLinkV1 | None,
    ) -> None:
        expected = {
            "run_id": request.run_id,
            "attempt_no": request.attempt_no,
            "artifact_id": request.artifact_id,
            "request_hash": request.request_hash,
            "fencing_token": request.expected_fencing_token,
            "role": "prompt_rendered",
        }
        for field_name, expected_value in expected.items():
            if getattr(replay, field_name) != expected_value:
                raise Conflict(
                    "prompt publication idempotency result differs from the request",
                    field_name=field_name,
                )
        if authoritative_link != replay:
            raise IntegrityViolation(
                "prompt replay is detached from its authoritative intermediate link"
            )
        if (
            run.run_id != replay.run_id
            or attempt.run_id != replay.run_id
            or attempt.attempt_no != replay.attempt_no
            or attempt.fencing_token != replay.fencing_token
        ):
            raise IntegrityViolation("prompt replay differs from its retained RunAttempt binding")
        if attempt.next_call_ordinal <= replay.call_ordinal:
            raise IntegrityViolation(
                "prompt replay ordinal was not consumed by the retained Attempt head"
            )


__all__ = [
    "CapabilityBinder",
    "PersistedRunClaim",
    "PromptRenderPublicationRequest",
    "PromptRenderPublicationResult",
    "RunAdmissionGateway",
    "RunClaimRequest",
    "RunClaimResult",
    "RunCommandCapabilities",
    "RunCommandService",
    "RunCreateRequest",
    "RunCreateResult",
    "RunPublicationGateway",
    "RunRegistryGateway",
    "RunRepository",
    "RunUnitOfWork",
]
