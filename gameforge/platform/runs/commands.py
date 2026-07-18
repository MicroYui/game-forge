"""Transactional Run creation, first claim, and ordinal-bound publication commands."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gameforge.contracts.errors import (
    Conflict,
    IdempotencyConflict,
    IntegrityViolation,
    InvalidStateTransition,
    QuotaExceeded,
)
from gameforge.contracts.execution_graphs import AgentExecutionGraphV1
from gameforge.contracts.execution_profiles import (
    MigrationCapabilityMatrixRefV1,
    MigrationCapabilityMatrixV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.jobs import (
    CancelRequestedDataV1,
    CancelRunPayloadV1,
    CommandAcceptedDataV1,
    CommandOutcomeDataV1,
    FailureClassifierV1,
    MAX_RUN_MANIFEST_PARENT_BINDINGS,
    OutcomeArtifactPolicyV1,
    PreparedRunFailure,
    RetryDecisionV1,
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
    RunModelResponseConsumptionV1,
    RunModelRouteLinkV1,
    RunPayloadEnvelope,
    RunPolicyBindingV1,
    RunQueuedDataV1,
    RunRecord,
    RunSchemaBindingV1,
    RunTerminatedDataV1,
    RunToolIntermediateLinkV1,
    RunCommandV1,
    canonical_payload_hash,
    outcome_policy_set_digest,
    run_kind_definition_digest,
)
from gameforge.contracts.lineage import AuditActor, AuditCorrelation
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.storage import UtcClock
from gameforge.platform.runs.state import (
    validate_claim_transition,
    validate_prompt_link_binding,
    validate_queued_creation,
    validate_run_kind_binding,
)
from gameforge.platform.runs.lifecycle import (
    AttemptWriteFence,
    RunFailurePublication,
    RunLifecycleAccountingGateway,
    resolve_lifecycle_bindings,
    select_outcome_policy,
    validate_attempt_write_fence,
    validate_prepared_failure,
    validate_terminal_cassette_publication,
)
from gameforge.platform.registry.model import ProfileRequirement
from gameforge.runtime.observability.context import TraceCarrier

if TYPE_CHECKING:
    from gameforge.platform.terminal_staging import (
        StagedTerminalPublication,
        TerminalPublicationDraft,
        TerminalPublicationStager,
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
    request_id: NonEmptyStr | None = None
    payload: RunPayloadEnvelope
    resource_domain_scope: DomainScope | None = None
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
    max_candidate_count: PositiveInt = 32

    @model_validator(mode="after")
    def _trusted_worker_kind(self) -> "RunClaimRequest":
        if self.worker.principal_kind not in {"service", "system"}:
            raise ValueError("Run claims require a service or system worker")
        if self.max_candidate_count > 1024:
            raise ValueError("Run claim candidate bound cannot exceed 1024")
        return self


class RunClaimResult(_FrozenModel):
    previous: RunRecord
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


class PromptRenderPublicationRequest(_FrozenModel):
    fence: AttemptWriteFence
    logical_call_ordinal: PositiveInt
    call_ordinal: PositiveInt | None = None
    route_ordinal: PositiveInt = 1
    artifact_id: NonEmptyStr
    request_hash: Sha256Hex
    idempotency_scope: NonEmptyStr
    idempotency_key: NonEmptyStr
    actor: AuditActor

    @model_validator(mode="after")
    def _worker_actor(self) -> "PromptRenderPublicationRequest":
        if self.actor.principal_kind not in {"service", "system"}:
            raise ValueError("prompt publication requires a service or system actor")
        if self.route_ordinal == 1 and self.call_ordinal is not None:
            raise ValueError("first route must acquire call_ordinal from the Attempt head")
        if self.route_ordinal > 1 and self.call_ordinal is None:
            raise ValueError("fallback route requires the already-open call_ordinal")
        if self.call_ordinal is not None and self.call_ordinal != self.logical_call_ordinal:
            raise ValueError("fallback call_ordinal differs from the logical call identity")
        return self


class PromptRenderPublicationResult(_FrozenModel):
    link: RunIntermediateArtifactLinkV1
    replayed: bool


class AgentPromptContextPublicationRequest(_FrozenModel):
    fence: AttemptWriteFence
    target_call_ordinal: PositiveInt
    artifact_id: NonEmptyStr
    payload_hash: Sha256Hex
    agent_node_id: NonEmptyStr
    prompt_version: NonEmptyStr
    idempotency_scope: NonEmptyStr
    idempotency_key: NonEmptyStr
    actor: AuditActor

    @model_validator(mode="after")
    def _worker_actor(self) -> "AgentPromptContextPublicationRequest":
        if self.actor.principal_kind not in {"service", "system"}:
            raise ValueError("Agent prompt-context publication requires a worker actor")
        return self


class AgentPromptContextPublicationResult(_FrozenModel):
    link: RunToolIntermediateLinkV1
    replayed: bool


class RunCommandSubmissionResult(_FrozenModel):
    status: Literal["accepted", "duplicate"]
    persisted_status: Literal["pending", "claimed", "applied", "rejected"]
    command_revision: PositiveInt
    run_revision: PositiveInt
    event: RunEvent | None = None


class PersistedCommandAcceptance(Protocol):
    @property
    def run(self) -> RunRecord: ...

    @property
    def record(self) -> RunCommandRecordV1: ...

    @property
    def events(self) -> tuple[RunEvent, ...]: ...


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

    def list_claim_candidates(
        self,
        *,
        now_utc: str,
        limit: int,
        after_created_at: str | None = None,
        after_run_id: str | None = None,
    ) -> tuple[RunRecord, ...]: ...

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

    def get_run_write_authority(
        self,
        run_id: str,
    ) -> tuple[RunRecord, RunAttempt | None, RunLease | None] | None: ...

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
        route_ordinal: int = 1,
    ) -> RunIntermediateArtifactLinkV1 | None: ...

    def list_prompt_render_links_by_artifact_id(
        self,
        artifact_id: str,
        *,
        limit: int,
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]: ...

    def list_prompt_render_links(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
        limit: int = MAX_RUN_MANIFEST_PARENT_BINDINGS,
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]: ...

    def put_intermediate_link(
        self,
        link: RunIntermediateArtifactLinkV1,
    ) -> RunIntermediateArtifactLinkV1: ...

    def get_tool_intermediate_link(
        self,
        run_id: str,
        attempt_no: int,
        target_call_ordinal: int,
    ) -> RunToolIntermediateLinkV1 | None: ...

    def get_tool_intermediate_for_call(
        self,
        run_id: str,
        attempt_no: int,
        target_call_ordinal: int,
    ) -> RunToolIntermediateLinkV1 | None: ...

    def put_tool_intermediate_link(
        self,
        link: RunToolIntermediateLinkV1,
    ) -> RunToolIntermediateLinkV1: ...

    def list_tool_intermediate_links(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
        limit: int = MAX_RUN_MANIFEST_PARENT_BINDINGS,
    ) -> tuple[RunToolIntermediateLinkV1, ...]: ...

    def get_model_route_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelRouteLinkV1 | None: ...

    def put_model_route_link(self, link: RunModelRouteLinkV1) -> RunModelRouteLinkV1: ...

    def list_model_route_links(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
        limit: int = MAX_RUN_MANIFEST_PARENT_BINDINGS,
    ) -> tuple[RunModelRouteLinkV1, ...]: ...

    def get_model_response_consumption(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelResponseConsumptionV1 | None: ...

    def put_model_response_consumption(
        self,
        consumption: RunModelResponseConsumptionV1,
    ) -> RunModelResponseConsumptionV1: ...

    def list_model_response_consumptions(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
        limit: int = MAX_RUN_MANIFEST_PARENT_BINDINGS,
    ) -> tuple[RunModelResponseConsumptionV1, ...]: ...

    def get_finding_link(
        self,
        run_id: str,
        attempt_no: int,
        ordinal: int,
    ) -> RunFindingLinkV1 | None: ...

    def put_finding_link(self, link: RunFindingLinkV1) -> RunFindingLinkV1: ...

    def get_command(self, run_id: str, command_id: str) -> RunCommandRecordV1 | None: ...

    def get_command_by_id(self, command_id: str) -> RunCommandRecordV1 | None: ...

    def get_command_by_idempotency(
        self,
        *,
        run_id: str,
        idempotency_key: str,
    ) -> RunCommandRecordV1 | None: ...

    def get_command_by_client_sequence(
        self,
        *,
        run_id: str,
        client_id: str,
        client_seq: int,
    ) -> RunCommandRecordV1 | None: ...

    def put_command(self, record: RunCommandRecordV1) -> RunCommandRecordV1: ...

    def accept_command(
        self,
        *,
        expected_run_revision: int,
        record: RunCommandRecordV1,
        events: tuple[RunEvent, ...],
        terminal_status: Literal["cancelled"] | None = None,
        terminal_failure_artifact_id: str | None = None,
        terminal_cassette_artifact_id: str | None = None,
    ) -> PersistedCommandAcceptance: ...

    def preflight_accept_terminal_command(
        self,
        *,
        expected_run_revision: int,
        record: RunCommandRecordV1,
        events: tuple[RunEvent, ...],
        terminal_status: Literal["cancelled"],
        terminal_failure_artifact_id: str,
        terminal_cassette_artifact_id: str | None,
    ) -> object: ...

    def apply_preflighted_terminal_command(
        self,
        seal: object,
    ) -> PersistedCommandAcceptance: ...

    def claim_command(
        self,
        *,
        fence: AttemptWriteFence,
        command_id: str,
        claimed_at: str,
    ) -> RunCommandRecordV1: ...

    def complete_command(
        self,
        *,
        fence: AttemptWriteFence,
        command_id: str,
        expected_command_revision: int,
        outcome: Literal["applied", "rejected"],
        outcome_code: str,
        occurred_at: str,
        event: RunEvent,
    ) -> RunCommandRecordV1: ...


class RunRegistryGateway(Protocol):
    """Resolve exact retained definitions and validate every typed binding set."""

    def get_run_kind(self, kind: RunKindRef) -> RunKindDefinition | None: ...

    def get_agent_execution_graph(
        self,
        run_kind: RunKindRef,
        agent_graph_version: str,
    ) -> AgentExecutionGraphV1 | None: ...

    def get_profile_requirements(
        self,
        kind: RunKindRef,
    ) -> tuple[ProfileRequirement, ...] | None: ...

    def get_permission_resolver_key(self, kind: RunKindRef) -> str | None: ...

    def get_migration_capability_matrix(
        self,
        ref: MigrationCapabilityMatrixRefV1,
    ) -> MigrationCapabilityMatrixV1 | None: ...

    def resolve_required_run_bindings(
        self,
        *,
        definition: RunKindDefinition,
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
    ) -> tuple[tuple[RunPolicyBindingV1, ...], tuple[RunSchemaBindingV1, ...]]: ...

    def get_retry_policy(self, ref: RetryPolicyRefV1) -> RetryPolicySnapshot | None: ...

    def get_failure_classifier(self, ref: object) -> FailureClassifierV1 | None: ...

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
        lease_id: str,
        expires_at: str,
    ) -> str: ...


class RunCommandSubmissionAuthorizationGateway(Protocol):
    """Authorize one loaded Run inside the authoritative command write UoW."""

    def authorize_submission(self, *, run: RunRecord, actor: AuditActor) -> None: ...


class RunPublicationGateway(Protocol):
    """Same-UoW audit/publication hooks; there is no permissive no-op adapter."""

    def record_run_created(
        self,
        *,
        run: RunRecord,
        event: RunEvent,
        request_id: str | None = None,
    ) -> None: ...

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

    def preflight_outcome(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunFailure,
    ) -> PreparedRunFailure: ...

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
        actor: AuditActor,
    ) -> RunIntermediateArtifactLinkV1:
        """Atomically consume the call head and retain the exact link/replay key.

        The command service supplies the fenced deadline guard while this
        transaction-bound participant publishes Artifact/ObjectRef and audit state.
        """
        ...

    def get_agent_prompt_context_replay(
        self,
        *,
        idempotency_scope: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> RunToolIntermediateLinkV1 | None: ...

    def publish_agent_prompt_context(
        self,
        *,
        link: RunToolIntermediateLinkV1,
        idempotency_scope: str,
        idempotency_key: str,
        payload_hash: str,
        actor: AuditActor,
    ) -> RunToolIntermediateLinkV1:
        """Publish one staged tool context without consuming the prompt call head."""

        ...

    def plan_run_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        attempt_failure_artifact_id: str | None,
        occurred_at: str,
        actor: AuditActor,
    ) -> TerminalPublicationDraft: ...

    def commit_planned_run_failure(
        self,
        draft: TerminalPublicationDraft,
        staged: StagedTerminalPublication,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        attempt_failure_artifact_id: str | None,
        occurred_at: str,
        actor: AuditActor,
        command_audit_correlation: AuditCorrelation | None = None,
    ) -> object: ...

    def record_command_submitted(
        self,
        *,
        run: RunRecord,
        record: RunCommandRecordV1,
        events: tuple[RunEvent, ...],
        actor: AuditActor,
        request_id: str | None = None,
    ) -> None: ...

    def record_command_completed(
        self,
        *,
        run: RunRecord,
        record: RunCommandRecordV1,
        event: RunEvent,
        actor: AuditActor,
    ) -> None: ...

    def record_run_terminal(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        event: RunEvent,
        actor: AuditActor,
        request_id: str | None = None,
    ) -> None: ...


@dataclass(slots=True)
class RunCommandCapabilities:
    runs: RunRepository | None
    registry: RunRegistryGateway | None
    admission: RunAdmissionGateway | None
    publication: RunPublicationGateway | None
    accounting: RunLifecycleAccountingGateway | None = None
    submission_authorization: RunCommandSubmissionAuthorizationGateway | None = None


class RunUnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...


CapabilityBinder = Callable[[Any], RunCommandCapabilities]
PlanningScope = Callable[[], AbstractContextManager[Any]]


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


def _bounded_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("request_id must be a non-empty bounded string or None")
    return value


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
        planning_scope: PlanningScope | None = None,
        bind_planning_capabilities: CapabilityBinder | None = None,
        stage_publications: TerminalPublicationStager | None = None,
    ) -> None:
        staging_parts = (
            planning_scope,
            bind_planning_capabilities,
            stage_publications,
        )
        if any(part is not None for part in staging_parts) and not all(
            part is not None for part in staging_parts
        ):
            raise ValueError(
                "terminal staging requires planning scope, planning binder, and stager"
            )
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._clock = clock
        self._planning_scope = planning_scope
        self._bind_planning_capabilities = bind_planning_capabilities
        self._stage_publications = stage_publications
        # Scheduling fairness is an operational hint, never queue authority. The
        # persistent Run rows remain the complete source of candidates and each
        # claim is re-read/fenced in its own UoW. Losing this cursor on restart only
        # restarts a bounded rotation; it cannot lose or mutate a Run.
        self._claim_rotation_lock = Lock()
        self._claim_rotation_cursor: tuple[str, str] | None = None

    def create_run(
        self,
        request: RunCreateRequest,
        *,
        companion_write: Callable[[Any], None] | None = None,
        fresh_admission_guard: Callable[[Any], None] | None = None,
    ) -> RunCreateResult:
        """Create (or idempotently replay) a queued Run.

        ``companion_write`` runs an ADDITIONAL authoritative write inside the SAME
        UnitOfWork as the Run creation (it receives the bound transaction). The
        validation-admission composition uses it to CAS the ApprovalItem
        ``draft->validating`` atomically with the queued Run, so a crash between the
        two can never leave the Run and the workflow subject inconsistent. It runs on
        both the create and the idempotent-replay path (a ``:validate`` retry re-drives
        the CAS, which must be idempotent).

        ``fresh_admission_guard`` is a read/CAS guard for mutable authorities used to
        build a new Run (for example an exact content ref). It runs only after the
        idempotency lookup proves this is a fresh creation, so a legitimate replay of
        an already-created Run is not invalidated by later external state changes.
        """

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
                if companion_write is not None:
                    companion_write(transaction)
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
            if fresh_admission_guard is not None:
                fresh_admission_guard(transaction)

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
                resource_domain_scope=request.resource_domain_scope,
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
                raise IntegrityViolation(
                    "Run repository did not retain the exact queued publication"
                )
            publication.record_run_created(
                run=run,
                event=initial_event,
                request_id=request.request_id,
            )
            if companion_write is not None:
                companion_write(transaction)
            return RunCreateResult(run=run, replayed=False)

    def claim_next(self, request: RunClaimRequest) -> RunClaimResult | None:
        discovery_now = _utc_text(_utc_now(self._clock))
        with self._claim_rotation_lock:
            cursor = self._claim_rotation_cursor
            with self._unit_of_work.begin() as transaction:
                capabilities = self._bind_capabilities(transaction)
                runs = _required(capabilities.runs, "runs")
                candidates = runs.list_claim_candidates(
                    now_utc=discovery_now,
                    limit=request.max_candidate_count,
                    after_created_at=None if cursor is None else cursor[0],
                    after_run_id=None if cursor is None else cursor[1],
                )
            self._claim_rotation_cursor = (
                None if not candidates else (candidates[-1].created_at, candidates[-1].run_id)
            )
        for candidate in candidates:
            try:
                return self._claim_candidate(request=request, candidate=candidate)
            except (Conflict, InvalidStateTransition, QuotaExceeded):
                # Each candidate owns a distinct UoW, so a lost revision race or
                # rejected permit group rolls back completely before trying the
                # next bounded candidate. This prevents an oldest quota-blocked Run
                # from starving unrelated principal/domain scopes.
                continue
        return None

    def _claim_candidate(
        self,
        *,
        request: RunClaimRequest,
        candidate: RunRecord,
    ) -> RunClaimResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            registry = _required(capabilities.registry, "registry")
            admission = _required(capabilities.admission, "admission")
            publication = _required(capabilities.publication, "publication")
            now = _utc_now(self._clock)
            now_text = _utc_text(now)
            previous = runs.get(candidate.run_id)
            if previous is None or previous.revision != candidate.revision:
                raise Conflict("Run claim candidate revision is no longer current")
            if previous.status not in {"queued", "retry_wait"}:
                raise Conflict("Run claim candidate is no longer claimable")
            if previous.cancel_requested_at is not None:
                raise Conflict("Run claim candidate is now cancel-requested")
            _resolve_bindings(run=previous, registry=registry)
            queue_deadline = _parse_utc(
                previous.queue_deadline_utc,
                field_name="stored queue_deadline_utc",
            )
            overall_deadline = _parse_utc(
                previous.overall_deadline_utc,
                field_name="stored overall_deadline_utc",
            )
            if now >= overall_deadline:
                raise Conflict("Run claim candidate expired before claim")
            if previous.status == "queued" and now >= queue_deadline:
                raise Conflict("queued Run candidate expired before claim")
            if previous.status == "retry_wait":
                if previous.retry_not_before_utc is None:
                    raise IntegrityViolation("retry-wait Run omitted retry_not_before_utc")
                retry_not_before = _parse_utc(
                    previous.retry_not_before_utc,
                    field_name="stored retry_not_before_utc",
                )
                if now < retry_not_before:
                    raise Conflict("retry-wait Run candidate is not yet eligible")
            carrier_context = (
                TraceCarrier.extract(previous.dispatch_trace_carrier)
                if previous.dispatch_trace_carrier is not None
                else None
            )
            authoritative_trace_id = (
                carrier_context.trace_id if carrier_context is not None else request.trace_id
            )
            if (
                carrier_context is not None
                and request.trace_id is not None
                and request.trace_id != carrier_context.trace_id
            ):
                raise IntegrityViolation("worker claim trace differs from the persisted carrier")
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
                lease_id=request.lease_id,
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
                trace_id=authoritative_trace_id,
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
                trace_id=authoritative_trace_id,
            )
            if (
                runs.get(previous.run_id) != persisted.run
                or runs.get_attempt(previous.run_id, persisted.attempt.attempt_no)
                != persisted.attempt
                or runs.get_current_lease(previous.run_id) != persisted.lease
                or runs.get_event(previous.run_id, persisted.event.seq) != persisted.event
            ):
                raise IntegrityViolation(
                    "Run repository did not retain the exact claim publication"
                )
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

    def submit(
        self,
        *,
        run_id: str,
        command: RunCommandV1,
        actor: AuditActor,
        request_id: str | None = None,
    ) -> RunCommandSubmissionResult:
        selected_request_id = _bounded_request_id(request_id)
        stager = self._stage_publications
        if stager is None:
            raise IntegrityViolation("Run command submission requires terminal staging authority")
        operation_now = _utc_now(self._clock)
        draft = self._plan_inactive_cancel(
            run_id=run_id,
            command=command,
            actor=actor,
            now=operation_now,
        )
        staged: StagedTerminalPublication | None = None
        if draft is not None:
            staged_batch = stager.stage((draft,))
            if len(staged_batch) != 1:
                raise IntegrityViolation("terminal stager returned another publication count")
            staged = staged_batch[0]
            try:
                draft = draft.seal_for_commit(staged)
            except ValueError as exc:
                raise IntegrityViolation(
                    "terminal command draft changed before its write-UoW seal"
                ) from exc
        return self._submit_in_write_uow(
            run_id=run_id,
            command=command,
            actor=actor,
            operation_now=operation_now,
            planned_publication=draft,
            staged_publication=staged,
            request_id=selected_request_id,
        )

    def _plan_inactive_cancel(
        self,
        *,
        run_id: str,
        command: RunCommandV1,
        actor: AuditActor,
        now: datetime,
    ) -> TerminalPublicationDraft | None:
        planning_scope = self._planning_scope
        bind_planning = self._bind_planning_capabilities
        if planning_scope is None or bind_planning is None:
            raise IntegrityViolation("terminal planning authority is unavailable")
        with planning_scope() as read_context:
            capabilities = bind_planning(read_context)
            runs = _required(capabilities.runs, "runs")
            publication = _required(capabilities.publication, "publication")
            request_hash = canonical_payload_hash(command)
            retained = runs.get_command_by_id(command.command_id)
            if retained is not None:
                if retained.run_id != run_id:
                    raise IdempotencyConflict(
                        "Run command identity is already bound to another Run"
                    )
                self._validate_command_replay(
                    retained=retained,
                    command=command,
                    request_hash=request_hash,
                )
                return None
            idempotent = runs.get_command_by_idempotency(
                run_id=run_id,
                idempotency_key=command.idempotency_key,
            )
            sequenced = runs.get_command_by_client_sequence(
                run_id=run_id,
                client_id=command.client_id,
                client_seq=command.client_seq,
            )
            if idempotent is not None or sequenced is not None:
                raise IdempotencyConflict(
                    "Run command identity is already bound to another request"
                )
            run = runs.get(run_id)
            if run is None:
                raise IntegrityViolation("Run command target does not exist")
            registry = _required(capabilities.registry, "registry")
            definition, _ = _resolve_bindings(run=run, registry=registry)
            if command.expected_run_revision != run.revision:
                raise Conflict(
                    "Run command expected revision differs",
                    expected_revision=command.expected_run_revision,
                    actual_revision=run.revision,
                )
            if command.payload_schema_id not in definition.allowed_command_schema_ids:
                raise InvalidStateTransition("Run command is not allowed for this Run kind")
            if now >= _parse_utc(
                run.overall_deadline_utc,
                field_name="run.overall_deadline_utc",
            ):
                raise InvalidStateTransition("Run command arrived after the overall deadline")
            if command.type != "cancel" or run.status in {"leased", "running"}:
                return None
            if not isinstance(command.payload, CancelRunPayloadV1):
                raise IntegrityViolation("cancel command has the wrong typed payload")
            if run.status not in {"queued", "retry_wait"}:
                raise InvalidStateTransition("Run is already terminal")
            lifecycle_definition, retry_policy, classifier = resolve_lifecycle_bindings(
                run=run,
                registry=registry,
            )
            latest_attempt = None
            if run.status == "retry_wait":
                latest_attempt = runs.get_attempt(run.run_id, run.next_attempt_no - 1)
                if latest_attempt is None or latest_attempt.status in {
                    "leased",
                    "running",
                }:
                    raise IntegrityViolation("retry-wait Run lacks its closed latest attempt")
            if runs.get_current_lease(run.run_id) is not None:
                raise IntegrityViolation("inactive Run unexpectedly retains an active lease")
            prepared = PreparedRunFailure(
                run_id=run.run_id,
                attempt_no=(latest_attempt.attempt_no if latest_attempt is not None else None),
                run_kind=run.kind,
                artifacts=(),
                requirement_dispositions=(),
                cause_code="cancelled",
                failure_class="cancelled",
                intrinsic_retry_eligible=False,
                classifier=run.failure_classifier,
                redacted_message="Run cancellation requested",
            )
            preflighted = publication.preflight_outcome(
                run=run,
                attempt=latest_attempt,
                prepared=prepared,
            )
            if not isinstance(preflighted, PreparedRunFailure):
                raise IntegrityViolation("cancel preflight returned a success outcome")
            prepared = preflighted
            validate_prepared_failure(
                run=run,
                attempt=latest_attempt,
                prepared=prepared,
                classifier=classifier,
            )
            decision = RetryDecisionV1(
                cause_code=prepared.cause_code,
                failure_class=prepared.failure_class,
                intrinsic_retry_eligible=prepared.intrinsic_retry_eligible,
                decision="terminal",
                reason_code="not_retry_eligible",
                classifier=run.failure_classifier,
                retry_policy=run.retry_policy,
                evaluated_at_utc=_utc_text(now),
            )
            if retry_policy.retry_policy_digest != run.retry_policy.retry_policy_digest:
                raise IntegrityViolation("cancel retry policy differs from Run")
            policy = select_outcome_policy(
                definition=lifecycle_definition,
                outcome_code=prepared.cause_code,
                prepared_outcome="failure",
                publication_scope="run",
                run_status="cancelled",
                attempt_status=None,
                failure_class=prepared.failure_class,
                retry_disposition="terminal",
            )
            return publication.plan_run_failure(
                run=run,
                attempt=latest_attempt,
                prepared=prepared,
                retry_decision=decision,
                policy=policy,
                attempt_failure_artifact_id=None,
                occurred_at=_utc_text(now),
                actor=actor,
            )

    def _submit_in_write_uow(
        self,
        *,
        run_id: str,
        command: RunCommandV1,
        actor: AuditActor,
        operation_now: datetime,
        planned_publication: TerminalPublicationDraft | None,
        staged_publication: StagedTerminalPublication | None,
        request_id: str | None,
    ) -> RunCommandSubmissionResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            publication = _required(capabilities.publication, "publication")
            authorization = _required(
                capabilities.submission_authorization,
                "submission authorization",
            )
            request_hash = canonical_payload_hash(command)
            load_write_authority = getattr(runs, "get_run_write_authority", None)
            if not callable(load_write_authority):
                raise IntegrityViolation("bounded Run write authority capability is required")
            bounded_authority = load_write_authority(run_id)
            if bounded_authority is None:
                raise IntegrityViolation("Run command target does not exist")
            run, authority_attempt, authority_lease = bounded_authority
            authorization.authorize_submission(run=run, actor=actor)
            retained = runs.get_command_by_id(command.command_id)
            if retained is not None:
                if retained.run_id != run_id:
                    raise IdempotencyConflict(
                        "Run command identity is already bound to another Run"
                    )
                self._validate_command_replay(
                    retained=retained,
                    command=command,
                    request_hash=request_hash,
                )
                event = (
                    runs.get_event(run_id, retained.result_event_seq)
                    if retained.result_event_seq is not None
                    else None
                )
                return RunCommandSubmissionResult(
                    status="duplicate",
                    persisted_status=retained.status,
                    command_revision=retained.revision,
                    run_revision=run.revision,
                    event=event,
                )
            idempotent = runs.get_command_by_idempotency(
                run_id=run_id,
                idempotency_key=command.idempotency_key,
            )
            sequenced = runs.get_command_by_client_sequence(
                run_id=run_id,
                client_id=command.client_id,
                client_seq=command.client_seq,
            )
            if idempotent is not None or sequenced is not None:
                raise IdempotencyConflict(
                    "Run command identity is already bound to another request"
                )

            registry = _required(capabilities.registry, "registry")
            definition, _ = _resolve_bindings(run=run, registry=registry)
            if command.expected_run_revision != run.revision:
                raise Conflict(
                    "Run command expected revision differs",
                    expected_revision=command.expected_run_revision,
                    actual_revision=run.revision,
                )
            if command.payload_schema_id not in definition.allowed_command_schema_ids:
                raise InvalidStateTransition("Run command is not allowed for this Run kind")
            guard_now = _utc_now(self._clock)
            now = operation_now
            now_text = _utc_text(now)
            if guard_now >= _parse_utc(
                run.overall_deadline_utc,
                field_name="run.overall_deadline_utc",
            ):
                raise InvalidStateTransition("Run command arrived after the overall deadline")

            if command.type == "provide_input":
                if run.status not in {"leased", "running"}:
                    raise InvalidStateTransition("provide_input requires an active Run")
                record = RunCommandRecordV1(
                    run_id=run.run_id,
                    command=command,
                    request_hash=request_hash,
                    actor=actor,
                    status="pending",
                    revision=1,
                    created_at=now_text,
                )
                event = RunEvent(
                    run_id=run.run_id,
                    seq=run.next_event_seq,
                    event_type="run.command_accepted",
                    occurred_at=now_text,
                    data_schema_version="command-accepted@1",
                    data=CommandAcceptedDataV1(
                        command_id=command.command_id,
                        command_type=command.type,
                        command_revision=record.revision,
                    ),
                )
                persisted = runs.accept_command(
                    expected_run_revision=run.revision,
                    record=record,
                    events=(event,),
                )
                publication.record_command_submitted(
                    run=persisted.run,
                    record=persisted.record,
                    events=persisted.events,
                    actor=actor,
                    request_id=request_id,
                )
                return RunCommandSubmissionResult(
                    status="accepted",
                    persisted_status=persisted.record.status,
                    command_revision=persisted.record.revision,
                    run_revision=persisted.run.revision,
                    event=persisted.events[0],
                )

            if not isinstance(command.payload, CancelRunPayloadV1):
                raise IntegrityViolation("cancel command has the wrong typed payload")
            if run.status not in {"queued", "leased", "running", "retry_wait"}:
                raise InvalidStateTransition("Run is already terminal")
            cancel_event = RunEvent(
                run_id=run.run_id,
                seq=run.next_event_seq,
                event_type="run.cancel_requested",
                occurred_at=now_text,
                data_schema_version="cancel-requested@1",
                data=CancelRequestedDataV1(
                    command_id=command.command_id,
                    reason_code=command.payload.reason_code,
                ),
            )
            record = RunCommandRecordV1(
                run_id=run.run_id,
                command=command,
                request_hash=request_hash,
                actor=actor,
                status="applied",
                revision=1,
                created_at=now_text,
                applied_at=now_text,
                result_event_seq=cancel_event.seq,
            )
            if run.status in {"leased", "running"}:
                if staged_publication is not None:
                    raise Conflict("inactive cancel projection changed before terminal commit")
                persisted = runs.accept_command(
                    expected_run_revision=run.revision,
                    record=record,
                    events=(cancel_event,),
                )
                publication.record_command_submitted(
                    run=persisted.run,
                    record=persisted.record,
                    events=persisted.events,
                    actor=actor,
                    request_id=request_id,
                )
                return RunCommandSubmissionResult(
                    status="accepted",
                    persisted_status=persisted.record.status,
                    command_revision=persisted.record.revision,
                    run_revision=persisted.run.revision,
                    event=persisted.events[0],
                )

            accounting = _required(capabilities.accounting, "accounting")
            lifecycle_definition, retry_policy, classifier = resolve_lifecycle_bindings(
                run=run,
                registry=registry,
            )
            latest_attempt = None
            if run.status == "retry_wait":
                latest_attempt = (
                    authority_attempt
                    if callable(load_write_authority)
                    else runs.get_attempt(run.run_id, run.next_attempt_no - 1)
                )
                if (
                    latest_attempt is None
                    or latest_attempt.attempt_no != run.next_attempt_no - 1
                    or latest_attempt.status in {"leased", "running"}
                ):
                    raise IntegrityViolation("retry-wait Run lacks its closed latest attempt")
            active_lease = (
                authority_lease
                if callable(load_write_authority)
                else runs.get_current_lease(run.run_id)
            )
            if active_lease is not None:
                raise IntegrityViolation("inactive Run unexpectedly retains an active lease")
            prepared = PreparedRunFailure(
                run_id=run.run_id,
                attempt_no=(latest_attempt.attempt_no if latest_attempt is not None else None),
                run_kind=run.kind,
                artifacts=(),
                requirement_dispositions=(),
                cause_code="cancelled",
                failure_class="cancelled",
                intrinsic_retry_eligible=False,
                classifier=run.failure_classifier,
                redacted_message="Run cancellation requested",
            )
            preflighted = publication.preflight_outcome(
                run=run,
                attempt=latest_attempt,
                prepared=prepared,
            )
            if not isinstance(preflighted, PreparedRunFailure):
                raise IntegrityViolation("cancel preflight returned a success outcome")
            prepared = preflighted
            validate_prepared_failure(
                run=run,
                attempt=latest_attempt,
                prepared=prepared,
                classifier=classifier,
            )
            decision = RetryDecisionV1(
                cause_code=prepared.cause_code,
                failure_class=prepared.failure_class,
                intrinsic_retry_eligible=prepared.intrinsic_retry_eligible,
                decision="terminal",
                reason_code="not_retry_eligible",
                classifier=run.failure_classifier,
                retry_policy=run.retry_policy,
                evaluated_at_utc=now_text,
            )
            if retry_policy.retry_policy_digest != run.retry_policy.retry_policy_digest:
                raise IntegrityViolation("cancel retry policy differs from Run")
            policy = select_outcome_policy(
                definition=lifecycle_definition,
                outcome_code=prepared.cause_code,
                prepared_outcome="failure",
                publication_scope="run",
                run_status="cancelled",
                attempt_status=None,
                failure_class=prepared.failure_class,
                retry_disposition="terminal",
            )
            publication_kwargs = {
                "run": run,
                "attempt": latest_attempt,
                "prepared": prepared,
                "retry_decision": decision,
                "policy": policy,
                "attempt_failure_artifact_id": None,
                "occurred_at": now_text,
                "actor": actor,
            }
            if planned_publication is None or staged_publication is None:
                raise IntegrityViolation(
                    "inactive cancel authority changed without a staged terminal projection"
                )
            planned_run_publication = planned_publication.result
            if not isinstance(planned_run_publication, RunFailurePublication):
                raise IntegrityViolation("staged inactive cancel has another result projection")
            validate_terminal_cassette_publication(
                run=run,
                cassette_artifact_id=(planned_run_publication.terminal_cassette_artifact_id),
            )
            failure_artifact_id = planned_run_publication.failure_artifact_id
            terminal_event = RunEvent(
                run_id=run.run_id,
                seq=run.next_event_seq + 1,
                event_type="run.cancelled",
                occurred_at=now_text,
                data_schema_version="run-terminated@1",
                data=RunTerminatedDataV1(
                    attempt_no=(latest_attempt.attempt_no if latest_attempt is not None else None),
                    failure_artifact_id=failure_artifact_id,
                    cause_code=prepared.cause_code,
                ),
            )
            command_closure_kwargs: dict[str, object] = {
                "expected_run_revision": run.revision,
                "record": record,
                "events": (cancel_event, terminal_event),
                "terminal_status": "cancelled",
                "terminal_failure_artifact_id": failure_artifact_id,
                "terminal_cassette_artifact_id": (
                    planned_run_publication.terminal_cassette_artifact_id
                ),
            }
            preflight_terminal_command = getattr(
                runs,
                "preflight_accept_terminal_command",
                None,
            )
            apply_terminal_command = getattr(
                runs,
                "apply_preflighted_terminal_command",
                None,
            )
            if not callable(preflight_terminal_command) or not callable(apply_terminal_command):
                raise IntegrityViolation(
                    "staged terminal command requires preflight/apply capabilities"
                )
            command_closure = preflight_terminal_command(**command_closure_kwargs)
            cost_closure = accounting.preflight_terminal_closure(
                run=run,
                attempt=None,
                lease=None,
                retry_decision=decision,
                terminal_status="cancelled",
            )
            run_publication = publication.commit_planned_run_failure(
                planned_publication,
                staged_publication,
                **publication_kwargs,
                command_audit_correlation=AuditCorrelation(
                    request_id=request_id,
                    run_id=run.run_id,
                    trace_id=(
                        terminal_event.trace_id
                        or cancel_event.trace_id
                        or (latest_attempt.trace_id if latest_attempt is not None else None)
                    ),
                ),
            )
            if not isinstance(run_publication, RunFailurePublication):
                raise IntegrityViolation(
                    "inactive cancel commit returned another result projection"
                )
            if run_publication != planned_run_publication:
                raise IntegrityViolation(
                    "inactive cancel commit returned another immutable projection"
                )
            accounting.apply_preflighted_terminal_closure(cost_closure)
            persisted = apply_terminal_command(command_closure)
            publication.record_command_submitted(
                run=persisted.run,
                record=persisted.record,
                events=persisted.events,
                actor=actor,
                request_id=request_id,
            )
            publication.record_run_terminal(
                run=persisted.run,
                attempt=latest_attempt,
                event=persisted.events[-1],
                actor=actor,
                request_id=request_id,
            )
            return RunCommandSubmissionResult(
                status="accepted",
                persisted_status=persisted.record.status,
                command_revision=persisted.record.revision,
                run_revision=persisted.run.revision,
                event=persisted.events[0],
            )

    def claim_command(
        self,
        *,
        fence: AttemptWriteFence,
        command_id: str,
        actor: AuditActor,
    ) -> RunCommandRecordV1:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            now = _utc_now(self._clock)
            run, attempt, lease = self._load_command_fence(runs=runs, fence=fence)
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=fence,
                actor=actor,
                now=now,
                allowed_statuses=frozenset({"running"}),
            )
            record = runs.get_command(run.run_id, command_id)
            if record is None:
                raise IntegrityViolation("Run command does not exist")
            if record.status != "pending":
                raise InvalidStateTransition("Run command is not pending")
            return runs.claim_command(
                fence=fence,
                command_id=command_id,
                claimed_at=_utc_text(now),
            )

    def complete_command(
        self,
        *,
        fence: AttemptWriteFence,
        command_id: str,
        expected_command_revision: int,
        outcome: Literal["applied", "rejected"],
        outcome_code: str,
        actor: AuditActor,
    ) -> RunCommandRecordV1:
        if not outcome_code:
            raise ValueError("outcome_code must be non-empty")
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            publication = _required(capabilities.publication, "publication")
            now = _utc_now(self._clock)
            now_text = _utc_text(now)
            run, attempt, lease = self._load_command_fence(runs=runs, fence=fence)
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=fence,
                actor=actor,
                now=now,
                allowed_statuses=frozenset({"running"}),
            )
            record = runs.get_command(run.run_id, command_id)
            if record is None:
                raise IntegrityViolation("Run command does not exist")
            if (
                record.status != "claimed"
                or record.revision != expected_command_revision
                or record.claimed_attempt_no != attempt.attempt_no
                or record.claimed_fencing_token != attempt.fencing_token
            ):
                raise Conflict("Run command completion fence differs")
            event = RunEvent(
                run_id=run.run_id,
                seq=run.next_event_seq,
                event_type=(
                    "run.command_applied" if outcome == "applied" else "run.command_rejected"
                ),
                attempt_no=attempt.attempt_no,
                occurred_at=now_text,
                data_schema_version="command-outcome@1",
                data=CommandOutcomeDataV1(
                    command_id=command_id,
                    command_type=record.command.type,
                    command_revision=record.revision + 1,
                    outcome_code=outcome_code,
                ),
                trace_id=attempt.trace_id,
            )
            completed = runs.complete_command(
                fence=fence,
                command_id=command_id,
                expected_command_revision=expected_command_revision,
                outcome=outcome,
                outcome_code=outcome_code,
                occurred_at=now_text,
                event=event,
            )
            updated_run = runs.get(run.run_id)
            if updated_run is None:
                raise IntegrityViolation("completed command Run disappeared")
            publication.record_command_completed(
                run=updated_run,
                record=completed,
                event=event,
                actor=actor,
            )
            return completed

    def publish_agent_prompt_context(
        self,
        request: AgentPromptContextPublicationRequest,
    ) -> AgentPromptContextPublicationResult:
        """Publish one exact tool context under the current lease without head CAS."""

        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            publication = _required(capabilities.publication, "publication")
            now = _utc_now(self._clock)
            replay = publication.get_agent_prompt_context_replay(
                idempotency_scope=request.idempotency_scope,
                idempotency_key=request.idempotency_key,
                payload_hash=request.payload_hash,
            )
            run = runs.get(request.fence.run_id)
            if run is None:
                raise IntegrityViolation("Agent prompt-context Run does not exist")
            _resolve_bindings(
                run=run,
                registry=_required(capabilities.registry, "registry"),
            )
            attempt = runs.get_attempt(request.fence.run_id, request.fence.attempt_no)
            lease = runs.get_current_lease(request.fence.run_id)
            if attempt is None:
                raise IntegrityViolation("Agent prompt-context attempt does not exist")
            if lease is None:
                raise Conflict("Agent prompt-context publication has no current lease")
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=request.fence,
                actor=request.actor,
                now=now,
                allowed_statuses=frozenset({"running"}),
            )
            if attempt.status != "running":
                raise InvalidStateTransition("Agent prompt-context attempt is not running")

            if replay is not None:
                expected = {
                    "run_id": request.fence.run_id,
                    "attempt_no": request.fence.attempt_no,
                    "target_call_ordinal": request.target_call_ordinal,
                    "artifact_id": request.artifact_id,
                    "agent_node_id": request.agent_node_id,
                    "prompt_version": request.prompt_version,
                    "payload_hash": request.payload_hash,
                    "fencing_token": request.fence.fencing_token,
                    "role": "agent_prompt_context",
                }
                for field_name, expected_value in expected.items():
                    if getattr(replay, field_name) != expected_value:
                        raise Conflict(
                            "Agent prompt-context replay differs from the request",
                            field_name=field_name,
                        )
                if (
                    runs.get_tool_intermediate_for_call(
                        replay.run_id,
                        replay.attempt_no,
                        replay.target_call_ordinal,
                    )
                    != replay
                ):
                    raise IntegrityViolation(
                        "Agent prompt-context replay is detached from its exact link"
                    )
                return AgentPromptContextPublicationResult(link=replay, replayed=True)

            if attempt.next_call_ordinal != request.target_call_ordinal:
                raise Conflict(
                    "Agent prompt-context target differs from the Attempt call head",
                    expected_call_ordinal=attempt.next_call_ordinal,
                    target_call_ordinal=request.target_call_ordinal,
                )
            plan = run.payload.execution_version_plan
            node = (
                None
                if plan is None
                else next(
                    (item for item in plan.nodes if item.agent_node_id == request.agent_node_id),
                    None,
                )
            )
            if (
                run.payload.llm_execution_mode == "not_applicable"
                or node is None
                or node.prompt_version != request.prompt_version
            ):
                raise IntegrityViolation(
                    "Agent prompt-context node escapes the frozen execution plan"
                )
            link = RunToolIntermediateLinkV1(
                run_id=request.fence.run_id,
                attempt_no=request.fence.attempt_no,
                target_call_ordinal=request.target_call_ordinal,
                artifact_id=request.artifact_id,
                agent_node_id=request.agent_node_id,
                prompt_version=request.prompt_version,
                payload_hash=request.payload_hash,
                fencing_token=request.fence.fencing_token,
                published_at=_utc_text(now),
            )
            stored = publication.publish_agent_prompt_context(
                link=link,
                idempotency_scope=request.idempotency_scope,
                idempotency_key=request.idempotency_key,
                payload_hash=request.payload_hash,
                actor=request.actor,
            )
            if stored != link:
                raise IntegrityViolation("Agent prompt-context gateway retained another link")
            if (
                runs.get_tool_intermediate_for_call(
                    link.run_id,
                    link.attempt_no,
                    link.target_call_ordinal,
                )
                != link
            ):
                raise IntegrityViolation(
                    "Agent prompt-context publication did not retain its exact link"
                )
            unchanged_attempt = runs.get_attempt(link.run_id, link.attempt_no)
            if unchanged_attempt != attempt:
                raise IntegrityViolation(
                    "Agent prompt-context publication changed the prompt call head"
                )
            return AgentPromptContextPublicationResult(link=link, replayed=False)

    def publish_prompt_rendered(
        self,
        request: PromptRenderPublicationRequest,
    ) -> PromptRenderPublicationResult:
        """Consume a call head only through an atomic publication gateway.

        The service validates the current deadline/fence and deliberately exposes no
        bare ordinal allocator; Artifact/ObjectRef and audit publication stay inside
        the transaction-bound publisher.
        """

        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            publication = _required(capabilities.publication, "publication")
            now = _utc_now(self._clock)
            replay = publication.get_prompt_replay(
                idempotency_scope=request.idempotency_scope,
                idempotency_key=request.idempotency_key,
                request_hash=request.request_hash,
            )
            if replay is not None:
                registry = _required(capabilities.registry, "registry")
                run = runs.get(request.fence.run_id)
                if run is None:
                    raise IntegrityViolation("prompt replay Run does not exist")
                _resolve_bindings(run=run, registry=registry)
                attempt = runs.get_attempt(
                    request.fence.run_id,
                    request.fence.attempt_no,
                )
                if attempt is None:
                    raise IntegrityViolation("prompt replay attempt does not exist")
                lease = runs.get_current_lease(request.fence.run_id)
                if lease is None:
                    raise Conflict("prompt replay has no current active lease")
                validate_attempt_write_fence(
                    run=run,
                    attempt=attempt,
                    lease=lease,
                    fence=request.fence,
                    actor=request.actor,
                    now=now,
                    allowed_statuses=frozenset({"running"}),
                )
                if attempt.status != "running":
                    raise InvalidStateTransition("prompt replay attempt is not running")
                authoritative_link = runs.get_intermediate_link(
                    replay.run_id,
                    replay.attempt_no,
                    replay.call_ordinal,
                    replay.route_ordinal,
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
            run = runs.get(request.fence.run_id)
            if run is None:
                raise IntegrityViolation("prompt publication Run does not exist")
            _resolve_bindings(run=run, registry=registry)
            attempt = runs.get_attempt(
                request.fence.run_id,
                request.fence.attempt_no,
            )
            if attempt is None:
                raise IntegrityViolation("prompt publication attempt does not exist")
            lease = runs.get_current_lease(request.fence.run_id)
            if lease is None:
                raise Conflict("prompt publication has no current active lease")
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=request.fence,
                actor=request.actor,
                now=now,
                allowed_statuses=frozenset({"running"}),
            )
            if attempt.status != "running":
                raise InvalidStateTransition("prompt publication attempt is not running")
            call_ordinal = (
                attempt.next_call_ordinal if request.route_ordinal == 1 else request.call_ordinal
            )
            if call_ordinal is None:  # closed by request validation; defensive at authority edge
                raise IntegrityViolation("fallback prompt publication has no logical call")
            if call_ordinal != request.logical_call_ordinal:
                raise Conflict(
                    "prompt logical call differs from the authoritative Attempt head",
                    expected_call_ordinal=call_ordinal,
                    requested_call_ordinal=request.logical_call_ordinal,
                )
            link = RunIntermediateArtifactLinkV1(
                run_id=request.fence.run_id,
                attempt_no=request.fence.attempt_no,
                call_ordinal=call_ordinal,
                route_ordinal=request.route_ordinal,
                artifact_id=request.artifact_id,
                role="prompt_rendered",
                request_hash=request.request_hash,
                fencing_token=request.fence.fencing_token,
                published_at=_utc_text(now),
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
                actor=request.actor,
            )
            if stored != link:
                raise IntegrityViolation("prompt publication gateway retained a different link")
            retained = runs.get_intermediate_link(
                link.run_id,
                link.attempt_no,
                link.call_ordinal,
                link.route_ordinal,
            )
            advanced_attempt = runs.get_attempt(link.run_id, link.attempt_no)
            if retained != link or advanced_attempt is None:
                raise IntegrityViolation("prompt publication did not atomically retain its link")
            expected_attempt = (
                RunAttempt.model_validate(
                    {
                        **attempt.model_dump(mode="python"),
                        "next_call_ordinal": attempt.next_call_ordinal + 1,
                    }
                )
                if request.route_ordinal == 1
                else attempt
            )
            if advanced_attempt != expected_attempt:
                raise IntegrityViolation("prompt publication changed the wrong logical-call head")
            return PromptRenderPublicationResult(link=link, replayed=False)

    @staticmethod
    def _load_command_fence(
        *,
        runs: RunRepository,
        fence: AttemptWriteFence,
    ) -> tuple[RunRecord, RunAttempt, RunLease]:
        run = runs.get(fence.run_id)
        attempt = runs.get_attempt(fence.run_id, fence.attempt_no)
        lease = runs.get_current_lease(fence.run_id)
        if run is None or attempt is None or lease is None:
            raise Conflict("Run command worker fence is no longer current")
        return run, attempt, lease

    @staticmethod
    def _validate_command_replay(
        *,
        retained: RunCommandRecordV1,
        command: RunCommandV1,
        request_hash: str,
    ) -> None:
        if retained.command != command or retained.request_hash != request_hash:
            raise IdempotencyConflict("Run command identity is bound to a different request")

    @staticmethod
    def _validate_create_replay(
        *,
        request: RunCreateRequest,
        retained: RunRecord,
        definition: RunKindDefinition,
    ) -> None:
        if retained.request_hash != request.request_hash:
            raise IdempotencyConflict(
                "Run idempotency key is bound to a different request",
                expected_request_hash=request.request_hash,
                actual_request_hash=retained.request_hash,
            )
        semantic_fields = {
            "kind": request.kind,
            "idempotency_scope": request.idempotency_scope,
            "idempotency_key": request.idempotency_key,
            "payload": request.payload,
            "resource_domain_scope": request.resource_domain_scope,
            "initiated_by": request.initiated_by,
        }
        for field_name, expected in semantic_fields.items():
            if getattr(retained, field_name) != expected:
                raise IdempotencyConflict(
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
            "run_id": request.fence.run_id,
            "attempt_no": request.fence.attempt_no,
            "artifact_id": request.artifact_id,
            "request_hash": request.request_hash,
            "fencing_token": request.fence.fencing_token,
            "role": "prompt_rendered",
            "route_ordinal": request.route_ordinal,
            "call_ordinal": request.logical_call_ordinal,
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
    "AgentPromptContextPublicationRequest",
    "AgentPromptContextPublicationResult",
    "CapabilityBinder",
    "PersistedRunClaim",
    "PromptRenderPublicationRequest",
    "PromptRenderPublicationResult",
    "RunAdmissionGateway",
    "RunClaimRequest",
    "RunClaimResult",
    "RunCommandCapabilities",
    "RunCommandSubmissionAuthorizationGateway",
    "RunCommandSubmissionResult",
    "RunCommandService",
    "RunCreateRequest",
    "RunCreateResult",
    "RunPublicationGateway",
    "RunRegistryGateway",
    "RunRepository",
    "RunUnitOfWork",
]
