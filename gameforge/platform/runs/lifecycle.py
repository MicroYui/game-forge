"""Fenced Run-attempt lifecycle commands over transaction-bound capabilities."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gameforge.contracts.errors import (
    AttemptFenceConflict,
    AttemptFenceStateRejected,
    Conflict,
    IntegrityViolation,
    InvalidStateTransition,
)
from gameforge.contracts.jobs import (
    AttemptProgressDataV1,
    AttemptStartedDataV1,
    FailureClassifierV1,
    LeaseExpiredDataV1,
    OutcomeArtifactPolicyV1,
    PreparedRunFailure,
    PreparedRunOutcome,
    PreparedRunResult,
    RetryDecisionV1,
    RetryPolicySnapshot,
    RetryScheduledDataV1,
    RunAttempt,
    RunEvent,
    RunKindDefinition,
    RunLease,
    RunRecord,
    RunSucceededDataV1,
    RunTerminatedDataV1,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import UtcClock
from gameforge.platform.runs.state import validate_run_kind_binding

if TYPE_CHECKING:
    from gameforge.platform.terminal_staging import (
        StagedTerminalPublication,
        TerminalPublicationDraft,
        TerminalPublicationStager,
    )


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]


_TerminalResult = TypeVar("_TerminalResult")
_MAX_TERMINAL_STAGE_ATTEMPTS = 3


class _TerminalProjectionDrift(IntegrityViolation):
    """Fresh terminal authority no longer matches the blob-staged projection."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AttemptWriteFence(_FrozenModel):
    """Stable worker-write fence; heartbeat lease revisions are intentionally absent."""

    run_id: NonEmptyStr
    attempt_no: PositiveInt
    expected_run_revision: PositiveInt
    lease_id: NonEmptyStr
    fencing_token: PositiveInt


class PermitGroupBinding(_FrozenModel):
    permit_group_id: NonEmptyStr
    revision: PositiveInt


class StartAttemptRequest(_FrozenModel):
    fence: AttemptWriteFence
    trace_id: NonEmptyStr | None = None
    actor: AuditActor

    @model_validator(mode="after")
    def _worker_actor(self) -> "StartAttemptRequest":
        _validate_worker_actor(self.actor)
        return self


class StartAttemptResult(_FrozenModel):
    previous: RunRecord
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


class RenewLeaseRequest(_FrozenModel):
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    lease_id: NonEmptyStr
    fencing_token: PositiveInt
    expected_lease_version: PositiveInt
    expected_permit_revision: PositiveInt
    lease_duration_ns: PositiveInt
    actor: AuditActor

    @model_validator(mode="after")
    def _worker_actor(self) -> "RenewLeaseRequest":
        _validate_worker_actor(self.actor)
        return self


class RenewLeaseResult(_FrozenModel):
    lease: RunLease
    permit: PermitGroupBinding


class ProgressPublicationResult(_FrozenModel):
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


class RunResultPublication(_FrozenModel):
    result_artifact_id: NonEmptyStr
    attempt_cassette_artifact_id: NonEmptyStr | None = None
    terminal_cassette_artifact_id: NonEmptyStr | None = None


class AttemptFailurePublication(_FrozenModel):
    failure_artifact_id: NonEmptyStr
    cassette_bundle_artifact_id: NonEmptyStr | None = None


class RunFailurePublication(_FrozenModel):
    failure_artifact_id: NonEmptyStr
    terminal_cassette_artifact_id: NonEmptyStr | None = None


class PublishAttemptOutcomeRequest(_FrozenModel):
    fence: AttemptWriteFence
    prepared_outcome: PreparedRunOutcome
    actor: AuditActor

    @model_validator(mode="after")
    def _worker_actor(self) -> "PublishAttemptOutcomeRequest":
        _validate_worker_actor(self.actor)
        return self


class ReapExpiredLeaseRequest(_FrozenModel):
    run_id: NonEmptyStr
    expected_run_revision: PositiveInt
    actor: AuditActor

    @model_validator(mode="after")
    def _system_actor(self) -> "ReapExpiredLeaseRequest":
        if self.actor.principal_kind != "system":
            raise ValueError("lease reaping requires a system actor")
        return self


class SweepRunTimeoutRequest(_FrozenModel):
    run_id: NonEmptyStr
    expected_run_revision: PositiveInt
    actor: AuditActor

    @model_validator(mode="after")
    def _system_actor(self) -> "SweepRunTimeoutRequest":
        if self.actor.principal_kind != "system":
            raise ValueError("timeout sweeping requires a system actor")
        return self


class AttemptOutcomePublicationResult(_FrozenModel):
    run: RunRecord
    attempt: RunAttempt | None = None
    lease: RunLease | None = None
    event: RunEvent
    retry_decision: RetryDecisionV1 | None = None
    result_artifact_id: NonEmptyStr | None = None
    attempt_failure_artifact_id: NonEmptyStr | None = None
    run_failure_artifact_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _publication_shape(self) -> "AttemptOutcomePublicationResult":
        if self.run.status == "succeeded":
            if self.result_artifact_id is None or any(
                value is not None
                for value in (
                    self.retry_decision,
                    self.attempt_failure_artifact_id,
                    self.run_failure_artifact_id,
                )
            ):
                raise ValueError("successful outcome requires only its result manifest")
        else:
            if self.retry_decision is None or self.result_artifact_id is not None:
                raise ValueError("non-success outcome requires a retry decision")
            if self.lease is not None and self.attempt_failure_artifact_id is None:
                raise ValueError("closed attempt requires an attempt failure manifest")
            if self.run.status == "retry_wait":
                if self.run_failure_artifact_id is not None:
                    raise ValueError("retry outcome cannot publish a run failure manifest")
            elif self.run_failure_artifact_id is None:
                raise ValueError("terminal non-success outcome requires a run failure manifest")
        return self


class PersistedAttemptStart(Protocol):
    @property
    def run(self) -> RunRecord: ...

    @property
    def attempt(self) -> RunAttempt: ...

    @property
    def lease(self) -> RunLease: ...

    @property
    def event(self) -> RunEvent: ...


class PersistedAttemptProgress(Protocol):
    @property
    def run(self) -> RunRecord: ...

    @property
    def attempt(self) -> RunAttempt: ...

    @property
    def lease(self) -> RunLease: ...

    @property
    def event(self) -> RunEvent: ...


class PersistedAttemptClose(Protocol):
    @property
    def run(self) -> RunRecord: ...

    @property
    def attempt(self) -> RunAttempt: ...

    @property
    def lease(self) -> RunLease: ...

    @property
    def events(self) -> tuple[RunEvent, ...]: ...


class PersistedRunTerminal(Protocol):
    @property
    def run(self) -> RunRecord: ...

    @property
    def attempt(self) -> RunAttempt | None: ...

    @property
    def lease(self) -> RunLease | None: ...

    @property
    def event(self) -> RunEvent: ...


class RunLifecycleRepository(Protocol):
    """Command-specific persistence surface; implementations never commit."""

    def get(self, run_id: str) -> RunRecord | None: ...

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None: ...

    def get_current_lease(self, run_id: str) -> RunLease | None: ...

    def get_event(self, run_id: str, seq: int) -> RunEvent | None: ...

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
    ) -> PersistedAttemptStart: ...

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
    ) -> RunLease: ...

    def append_progress(
        self,
        *,
        fence: AttemptWriteFence,
        event: RunEvent,
    ) -> PersistedAttemptProgress: ...

    def close_attempt_for_retry(
        self,
        *,
        fence: AttemptWriteFence,
        ended_at: str,
        attempt_status: Literal["failed", "lease_expired"],
        lease_status: Literal["closed", "expired"],
        failure_class: str,
        failure_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        retry_decision: RetryDecisionV1,
        events: tuple[RunEvent, ...],
    ) -> PersistedAttemptClose: ...

    def complete_attempt_success(
        self,
        *,
        fence: AttemptWriteFence,
        ended_at: str,
        result_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        terminal_cassette_artifact_id: str | None,
        event: RunEvent,
    ) -> PersistedRunTerminal: ...

    def close_attempt_terminal(
        self,
        *,
        fence: AttemptWriteFence,
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
    ) -> PersistedRunTerminal: ...

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
    ) -> PersistedRunTerminal: ...


class RunLifecycleRegistryGateway(Protocol):
    def get_run_kind(self, kind: object) -> RunKindDefinition | None: ...

    def get_retry_policy(self, ref: object) -> RetryPolicySnapshot | None: ...

    def get_failure_classifier(self, ref: object) -> FailureClassifierV1 | None: ...


class RunLifecycleAccountingGateway(Protocol):
    def renew_execution_permits(
        self,
        *,
        permit_group_id: str,
        expected_revision: int,
        lease_id: str,
        fencing_token: int,
        expires_at: str,
    ) -> PermitGroupBinding: ...

    def retry_budget_available(self, *, run: RunRecord) -> bool: ...

    def release_attempt(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        retry_decision: RetryDecisionV1 | None,
    ) -> None: ...

    def close_run(
        self,
        *,
        run: RunRecord,
        terminal_status: Literal["succeeded", "failed", "cancelled", "timed_out"],
    ) -> None: ...


class RunLifecyclePublicationGateway(Protocol):
    def preflight_outcome(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunOutcome,
    ) -> PreparedRunOutcome: ...

    def record_attempt_started(
        self,
        *,
        previous: RunRecord,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        event: RunEvent,
        actor: AuditActor,
    ) -> None: ...

    def record_attempt_progress(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        event: RunEvent,
        actor: AuditActor,
    ) -> None: ...

    def plan_run_result(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunResult,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> TerminalPublicationDraft: ...

    def plan_attempt_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> TerminalPublicationDraft: ...

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

    def plan_active_failure_aggregate(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        attempt_policy: OutcomeArtifactPolicyV1,
        run_policy: OutcomeArtifactPolicyV1 | None,
        occurred_at: str,
        actor: AuditActor,
    ) -> tuple[TerminalPublicationDraft, ...]: ...

    def commit(
        self,
        fresh_draft: TerminalPublicationDraft,
        staged: StagedTerminalPublication,
    ) -> object: ...

    def commit_many(
        self,
        publications: tuple[tuple[TerminalPublicationDraft, StagedTerminalPublication], ...],
    ) -> tuple[object, ...]: ...

    def record_attempt_closed(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        events: tuple[RunEvent, ...],
        actor: AuditActor,
    ) -> None: ...

    def record_run_terminal(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        event: RunEvent,
        actor: AuditActor,
    ) -> None: ...


@dataclass(slots=True)
class RunLifecycleCapabilities:
    runs: RunLifecycleRepository | None
    registry: RunLifecycleRegistryGateway | None
    accounting: RunLifecycleAccountingGateway | None
    publication: RunLifecyclePublicationGateway | None


class RunLifecycleUnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...


LifecycleCapabilityBinder = Callable[[Any], RunLifecycleCapabilities]
LifecyclePlanningScope = Callable[[], AbstractContextManager[Any]]


def _required[T](value: T | None, name: str) -> T:
    if value is None:
        raise IntegrityViolation(f"{name} Run lifecycle capability is unavailable")
    return value


def validate_attempt_cassette_publication(
    *,
    run: RunRecord,
    cassette_artifact_id: str | None,
) -> None:
    if run.payload.llm_execution_mode == "record":
        if cassette_artifact_id is None:
            raise IntegrityViolation("RECORD attempt publication requires a cassette bundle")
    elif cassette_artifact_id is not None:
        raise IntegrityViolation("only RECORD attempts may publish a cassette bundle")


def validate_terminal_cassette_publication(
    *,
    run: RunRecord,
    cassette_artifact_id: str | None,
) -> None:
    mode = run.payload.llm_execution_mode
    if mode == "record":
        if cassette_artifact_id is None:
            raise IntegrityViolation("RECORD terminal publication requires a run cassette bundle")
    elif mode == "replay":
        if cassette_artifact_id != run.payload.cassette_artifact_id:
            raise IntegrityViolation(
                "REPLAY terminal publication must retain its exact input cassette"
            )
    elif cassette_artifact_id is not None:
        raise IntegrityViolation("live or non-LLM terminal publication cannot attach a cassette")


def validate_cassette_scope_pair(
    *,
    run: RunRecord,
    attempt_cassette_artifact_id: str | None,
    terminal_cassette_artifact_id: str | None,
) -> None:
    validate_attempt_cassette_publication(
        run=run,
        cassette_artifact_id=attempt_cassette_artifact_id,
    )
    validate_terminal_cassette_publication(
        run=run,
        cassette_artifact_id=terminal_cassette_artifact_id,
    )
    if (
        attempt_cassette_artifact_id is not None
        and attempt_cassette_artifact_id == terminal_cassette_artifact_id
    ):
        raise IntegrityViolation("attempt and run cassette bundles must be distinct")


def _validate_worker_actor(actor: AuditActor) -> None:
    if actor.principal_kind not in {"service", "system"}:
        raise ValueError("Run attempt writes require a service or system actor")


def _utc_now(clock: UtcClock) -> datetime:
    try:
        value = clock.now_utc()
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("Run lifecycle clock must return UTC") from exc
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("Run lifecycle clock must return UTC")
    return value.astimezone(timezone.utc)


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


def _add_ns(value: datetime, duration_ns: int) -> datetime:
    return value + timedelta(microseconds=(duration_ns + 999) // 1_000)


def validate_attempt_write_fence(
    *,
    run: RunRecord,
    attempt: RunAttempt,
    lease: RunLease,
    fence: AttemptWriteFence,
    actor: AuditActor,
    now: datetime,
    allowed_statuses: frozenset[str],
) -> None:
    """Validate the immutable attempt fence and all authoritative deadlines."""

    _validate_worker_actor(actor)
    if run.revision != fence.expected_run_revision:
        raise AttemptFenceConflict(
            "Run write revision differs",
            expected_revision=fence.expected_run_revision,
            actual_revision=run.revision,
        )
    if run.status not in allowed_statuses:
        raise AttemptFenceStateRejected(
            "Run status does not allow this attempt write",
            status=run.status,
        )
    if (
        run.run_id != fence.run_id
        or run.current_attempt_no != fence.attempt_no
        or attempt.run_id != fence.run_id
        or attempt.attempt_no != fence.attempt_no
        or attempt.fencing_token != fence.fencing_token
        or lease.lease_id != fence.lease_id
        or lease.run_id != fence.run_id
        or lease.attempt_no != fence.attempt_no
        or lease.fencing_token != fence.fencing_token
        or lease.status != "active"
    ):
        raise AttemptFenceConflict("Run attempt write fence differs from the current lease")
    if actor.principal_kind == "service" and lease.owner_principal_id != actor.principal_id:
        raise AttemptFenceConflict("Run attempt write actor does not own the current lease")

    lease_expiry = _parse_utc(lease.expires_at, field_name="lease.expires_at")
    overall_deadline = _parse_utc(
        run.overall_deadline_utc,
        field_name="run.overall_deadline_utc",
    )
    if now >= lease_expiry:
        raise AttemptFenceStateRejected("Run attempt lease is expired")
    if now >= overall_deadline:
        raise AttemptFenceStateRejected("Run overall deadline is exhausted")
    if attempt.attempt_deadline_utc is not None:
        attempt_deadline = _parse_utc(
            attempt.attempt_deadline_utc,
            field_name="attempt.attempt_deadline_utc",
        )
        if now >= attempt_deadline:
            raise AttemptFenceStateRejected("Run attempt deadline is exhausted")


def resolve_lifecycle_bindings(
    *,
    run: RunRecord,
    registry: RunLifecycleRegistryGateway,
) -> tuple[RunKindDefinition, RetryPolicySnapshot, FailureClassifierV1]:
    definition = registry.get_run_kind(run.kind)
    retry_policy = registry.get_retry_policy(run.retry_policy)
    classifier = registry.get_failure_classifier(run.failure_classifier)
    if definition is None or retry_policy is None or classifier is None:
        raise IntegrityViolation("Run lifecycle exact registry binding is unavailable")
    validate_run_kind_binding(
        run=run,
        definition=definition,
        retry_policy=retry_policy,
    )
    if (
        definition.failure_classifier != run.failure_classifier
        or definition.retry_policy != run.retry_policy
        or retry_policy.retry_policy_id != run.retry_policy.retry_policy_id
        or retry_policy.retry_policy_version != run.retry_policy.retry_policy_version
        or retry_policy.retry_policy_digest != run.retry_policy.retry_policy_digest
        or retry_policy.max_attempts != run.max_attempts
        or classifier.classifier_version != run.failure_classifier.classifier_version
        or classifier.classifier_digest != run.failure_classifier.classifier_digest
    ):
        raise IntegrityViolation("Run lifecycle retained registry differs from the Run binding")
    return definition, retry_policy, classifier


def validate_prepared_failure(
    *,
    run: RunRecord,
    attempt: RunAttempt | None,
    prepared: PreparedRunFailure,
    classifier: FailureClassifierV1,
) -> None:
    if (
        prepared.run_id != run.run_id
        or prepared.run_kind != run.kind
        or prepared.classifier != run.failure_classifier
        or prepared.attempt_no != (attempt.attempt_no if attempt is not None else None)
    ):
        raise IntegrityViolation("prepared failure differs from the authoritative Run binding")
    rule = next(
        (item for item in classifier.rules if item.cause_code == prepared.cause_code),
        None,
    )
    if rule is None:
        raise IntegrityViolation("prepared failure cause is absent from the exact classifier")
    if (
        rule.failure_class != prepared.failure_class
        or rule.intrinsic_retry_eligible != prepared.intrinsic_retry_eligible
        or rule.dependency_required != (prepared.dependency is not None)
    ):
        raise IntegrityViolation("prepared failure disagrees with the exact classifier")
    if prepared.dependency is not None and (
        prepared.dependency.dependency_kind not in rule.allowed_dependency_kinds
        or prepared.dependency.classifier_code != prepared.cause_code
    ):
        raise IntegrityViolation("prepared dependency differs from the classifier allowlist")


def _preflight_control_failure(
    *,
    publication: RunLifecyclePublicationGateway,
    run: RunRecord,
    attempt: RunAttempt | None,
    prepared: PreparedRunFailure,
) -> PreparedRunFailure:
    """Apply the same validation-subject fence used by worker outcomes.

    Reapers, timeout sweepers and inactive control terminals manufacture their
    failure locally, so they do not pass through ``publish_attempt_outcome``.  They
    must nevertheless observe a subject supersede before selecting the retained
    failure policy or publishing any authority.
    """

    preflighted = publication.preflight_outcome(
        run=run,
        attempt=attempt,
        prepared=prepared,
    )
    if not isinstance(preflighted, PreparedRunFailure):
        raise IntegrityViolation("control failure preflight returned a success outcome")
    return preflighted


def select_outcome_policy(
    *,
    definition: RunKindDefinition,
    outcome_code: str,
    prepared_outcome: Literal["success", "failure"],
    publication_scope: Literal["attempt", "run"],
    run_status: Literal["retry_wait", "succeeded", "failed", "cancelled", "timed_out"],
    attempt_status: Literal["failed", "cancelled", "timed_out", "lease_expired"] | None,
    failure_class: str | None,
    retry_disposition: Literal["retry", "terminal"] | None,
) -> OutcomeArtifactPolicyV1:
    matches = tuple(
        policy
        for policy in definition.outcome_policies
        if (
            policy.outcome_code == outcome_code
            and policy.prepared_outcome == prepared_outcome
            and policy.publication_scope == publication_scope
            and policy.run_status_after_publication == run_status
            and policy.attempt_terminal_status == attempt_status
            and policy.failure_class == failure_class
            and policy.retry_disposition == retry_disposition
        )
    )
    if len(matches) != 1:
        raise IntegrityViolation(
            "Run outcome requires exactly one retained publication policy",
            outcome_code=outcome_code,
            publication_scope=publication_scope,
        )
    return matches[0]


def _attempt_terminal_status(
    *,
    cause_code: str,
    failure_class: str,
) -> Literal["failed", "cancelled", "timed_out", "lease_expired"]:
    if cause_code == "lease_expired" or failure_class == "lease":
        return "lease_expired"
    if failure_class in {"cancelled", "subject_superseded"}:
        return "cancelled"
    if failure_class == "timeout":
        return "timed_out"
    return "failed"


def _run_terminal_status(
    *,
    failure_class: str,
    terminal_reason: str,
) -> Literal["failed", "cancelled", "timed_out"]:
    if failure_class in {"cancelled", "subject_superseded"}:
        return "cancelled"
    if failure_class == "timeout" or (
        failure_class == "lease"
        and terminal_reason in {"attempt_deadline_exhausted", "overall_deadline_exhausted"}
    ):
        return "timed_out"
    return "failed"


def _require_exact_staged_projection(
    *,
    fresh_drafts: tuple[TerminalPublicationDraft, ...],
    staged_publications: tuple[StagedTerminalPublication, ...],
) -> None:
    """Reject stage/write drift before any terminal authority is mutated."""

    fresh_digests = tuple(draft.projection_digest for draft in fresh_drafts)
    staged_digests = tuple(item.projection_digest for item in staged_publications)
    if fresh_digests != staged_digests:
        raise _TerminalProjectionDrift(
            "fresh terminal authority differs from the staged projection",
            fresh_publication_count=len(fresh_digests),
            staged_publication_count=len(staged_digests),
        )


class RunLifecycleService:
    def __init__(
        self,
        *,
        unit_of_work: RunLifecycleUnitOfWork,
        bind_capabilities: LifecycleCapabilityBinder,
        clock: UtcClock,
        planning_scope: LifecyclePlanningScope | None = None,
        bind_planning_capabilities: LifecycleCapabilityBinder | None = None,
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

    def _stage_then_publish(
        self,
        *,
        plan: Callable[[datetime], tuple[TerminalPublicationDraft, ...]],
        publish: Callable[[datetime, tuple[StagedTerminalPublication, ...]], _TerminalResult],
    ) -> _TerminalResult:
        """Bound the read -> blob stage -> fresh write-authority convergence loop.

        Blob staging deliberately happens outside the database UoW.  Authority can
        therefore change after a valid draft was staged.  A fresh write snapshot
        detects that drift before the first repository/effect/accounting write and
        aborts its UoW with ``_TerminalProjectionDrift``.  Replanning and restaging
        from a new read snapshot makes a stable supersede/cancel/deadline decision
        publish through its exact typed terminal policy.  Persistent churn remains
        fail-closed after a small fixed number of attempts; staged blobs are only
        verified, content-addressed GC-eligible orphans.
        """

        stager = self._stage_publications
        if stager is None:
            raise IntegrityViolation("terminal publication stager is unavailable")
        last_drift: _TerminalProjectionDrift | None = None
        for stage_attempt in range(1, _MAX_TERMINAL_STAGE_ATTEMPTS + 1):
            operation_now = _utc_now(self._clock)
            drafts = plan(operation_now)
            staged = stager.stage(drafts)
            if len(staged) != len(drafts):
                raise IntegrityViolation("terminal stager returned another publication count")
            try:
                return publish(operation_now, staged)
            except _TerminalProjectionDrift as exc:
                last_drift = exc
                if stage_attempt == _MAX_TERMINAL_STAGE_ATTEMPTS:
                    break
        raise IntegrityViolation(
            "terminal authority did not stabilize across bounded replan/restage",
            stage_attempts=_MAX_TERMINAL_STAGE_ATTEMPTS,
        ) from last_drift

    def start_attempt(self, request: StartAttemptRequest) -> StartAttemptResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            publication = _required(capabilities.publication, "publication")
            now = _utc_now(self._clock)
            run, attempt, lease = self._load_fenced_attempt(
                runs=runs,
                fence=request.fence,
            )
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=request.fence,
                actor=request.actor,
                now=now,
                allowed_statuses=frozenset({"leased"}),
            )
            if attempt.status != "leased":
                raise InvalidStateTransition("Run attempt is not leased")
            overall_deadline = _parse_utc(
                run.overall_deadline_utc,
                field_name="run.overall_deadline_utc",
            )
            attempt_deadline = min(_add_ns(now, run.attempt_timeout_ns), overall_deadline)
            persisted = runs.start_attempt(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                expected_run_revision=run.revision,
                lease_id=lease.lease_id,
                fencing_token=attempt.fencing_token,
                started_at=_utc_text(now),
                attempt_deadline_utc=_utc_text(attempt_deadline),
                trace_id=request.trace_id,
            )
            self._validate_start_result(
                previous=run,
                previous_attempt=attempt,
                previous_lease=lease,
                persisted=persisted,
                started_at=_utc_text(now),
                attempt_deadline_utc=_utc_text(attempt_deadline),
                trace_id=request.trace_id,
            )
            publication.record_attempt_started(
                previous=run,
                run=persisted.run,
                attempt=persisted.attempt,
                lease=persisted.lease,
                event=persisted.event,
                actor=request.actor,
            )
            return StartAttemptResult(
                previous=run,
                run=persisted.run,
                attempt=persisted.attempt,
                lease=persisted.lease,
                event=persisted.event,
            )

    def renew_lease(self, request: RenewLeaseRequest) -> RenewLeaseResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            accounting = _required(capabilities.accounting, "accounting")
            now = _utc_now(self._clock)
            run = runs.get(request.run_id)
            attempt = runs.get_attempt(request.run_id, request.attempt_no)
            lease = runs.get_current_lease(request.run_id)
            if run is None or attempt is None or lease is None:
                raise Conflict("Run lease renewal target is no longer current")
            fence = AttemptWriteFence(
                run_id=request.run_id,
                attempt_no=request.attempt_no,
                expected_run_revision=run.revision,
                lease_id=request.lease_id,
                fencing_token=request.fencing_token,
            )
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=fence,
                actor=request.actor,
                now=now,
                allowed_statuses=frozenset({"leased", "running"}),
            )
            if run.cancel_requested_at is not None:
                # The cancellation flag and this renewal are serialized in the
                # same authoritative UoW. A pre-renewal worker probe is only a
                # latency optimization and cannot authorize one extra extension
                # after cancellation committed.
                raise InvalidStateTransition("cancel-requested Run lease cannot renew")
            if lease.lease_version != request.expected_lease_version:
                raise Conflict("Run lease version differs")
            if run.concurrency_permit_group_id is None:
                raise IntegrityViolation("active Run has no concurrency permit group")
            ceiling = _parse_utc(
                attempt.attempt_deadline_utc or run.overall_deadline_utc,
                field_name="attempt deadline ceiling",
            )
            expires_at = min(_add_ns(now, request.lease_duration_ns), ceiling)
            if expires_at <= now:
                raise InvalidStateTransition("Run lease cannot renew past its deadline")
            renewed = runs.renew_lease(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                lease_id=lease.lease_id,
                fencing_token=attempt.fencing_token,
                expected_lease_version=request.expected_lease_version,
                heartbeat_at=_utc_text(now),
                expires_at=_utc_text(expires_at),
            )
            permit = accounting.renew_execution_permits(
                permit_group_id=run.concurrency_permit_group_id,
                expected_revision=request.expected_permit_revision,
                lease_id=lease.lease_id,
                fencing_token=attempt.fencing_token,
                expires_at=_utc_text(expires_at),
            )
            if (
                renewed.lease_version != lease.lease_version + 1
                or renewed.heartbeat_at != _utc_text(now)
                or renewed.expires_at != _utc_text(expires_at)
                or permit.permit_group_id != run.concurrency_permit_group_id
                or permit.revision != request.expected_permit_revision + 1
            ):
                raise IntegrityViolation("lease or permit renewal returned an invalid projection")
            return RenewLeaseResult(lease=renewed, permit=permit)

    def publish_progress(
        self,
        *,
        fence: AttemptWriteFence,
        data: AttemptProgressDataV1,
        actor: AuditActor,
    ) -> ProgressPublicationResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            publication = _required(capabilities.publication, "publication")
            now = _utc_now(self._clock)
            run, attempt, lease = self._load_fenced_attempt(runs=runs, fence=fence)
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=fence,
                actor=actor,
                now=now,
                allowed_statuses=frozenset({"running"}),
            )
            if attempt.status != "running":
                raise InvalidStateTransition("Run attempt is not running")
            if data.attempt_no != attempt.attempt_no:
                raise IntegrityViolation("progress data attempt differs from the current attempt")
            event = RunEvent(
                run_id=run.run_id,
                seq=run.next_event_seq,
                event_type="attempt.progress",
                attempt_no=attempt.attempt_no,
                occurred_at=_utc_text(now),
                data_schema_version="attempt-progress@1",
                data=data,
                trace_id=attempt.trace_id,
            )
            persisted = runs.append_progress(fence=fence, event=event)
            expected_run = RunRecord.model_validate(
                {
                    **run.model_dump(mode="python"),
                    "revision": run.revision + 1,
                    "next_event_seq": run.next_event_seq + 1,
                    "updated_at": event.occurred_at,
                }
            )
            if (
                persisted.run != expected_run
                or persisted.attempt != attempt
                or persisted.lease != lease
                or persisted.event != event
                or runs.get_event(run.run_id, event.seq) != event
            ):
                raise IntegrityViolation("progress publication returned an invalid projection")
            publication.record_attempt_progress(
                run=persisted.run,
                attempt=persisted.attempt,
                event=persisted.event,
                actor=actor,
            )
            return ProgressPublicationResult(
                run=persisted.run,
                attempt=persisted.attempt,
                lease=persisted.lease,
                event=persisted.event,
            )

    def publish_attempt_outcome(
        self,
        request: PublishAttemptOutcomeRequest,
    ) -> AttemptOutcomePublicationResult:
        return self._stage_then_publish(
            plan=lambda operation_now: self._plan_attempt_outcome(
                request=request,
                now=operation_now,
            ),
            publish=lambda operation_now, staged: self._publish_attempt_outcome_in_write_uow(
                request=request,
                operation_now=operation_now,
                staged_publications=staged,
            ),
        )

    def _publish_attempt_outcome_in_write_uow(
        self,
        *,
        request: PublishAttemptOutcomeRequest,
        operation_now: datetime,
        staged_publications: tuple[StagedTerminalPublication, ...],
    ) -> AttemptOutcomePublicationResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            registry = _required(capabilities.registry, "registry")
            accounting = _required(capabilities.accounting, "accounting")
            publication = _required(capabilities.publication, "publication")
            guard_now = _utc_now(self._clock)
            now = operation_now
            run, attempt, lease = self._load_fenced_attempt(
                runs=runs,
                fence=request.fence,
            )
            if run.status != "running" or attempt.status != "running":
                raise InvalidStateTransition("Run attempt outcome requires a running attempt")
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=request.fence,
                actor=request.actor,
                now=guard_now,
                allowed_statuses=frozenset({"running"}),
            )
            definition, retry_policy, classifier = resolve_lifecycle_bindings(
                run=run,
                registry=registry,
            )
            prepared = publication.preflight_outcome(
                run=run,
                attempt=attempt,
                prepared=request.prepared_outcome,
            )
            if run.cancel_requested_at is not None and (
                not isinstance(prepared, PreparedRunFailure)
                or prepared.failure_class != "subject_superseded"
            ):
                # Cancellation is fresh control-plane authority and wins over a
                # concurrently completed success as well as ordinary failures.
                # Discard prepared business outputs and close through the typed
                # cancellation policy in this same terminal UoW.
                prepared = PreparedRunFailure(
                    run_id=run.run_id,
                    attempt_no=attempt.attempt_no,
                    run_kind=run.kind,
                    artifacts=(),
                    requirement_dispositions=(),
                    cause_code="cancelled",
                    failure_class="cancelled",
                    intrinsic_retry_eligible=False,
                    classifier=run.failure_classifier,
                    redacted_message="Run cancellation requested",
                )
            if isinstance(prepared, PreparedRunResult):
                if (
                    prepared.run_id != run.run_id
                    or prepared.attempt_no != attempt.attempt_no
                    or prepared.run_kind != run.kind
                ):
                    raise IntegrityViolation("prepared result differs from the current Run attempt")
                policy = select_outcome_policy(
                    definition=definition,
                    outcome_code=prepared.summary.outcome_code,
                    prepared_outcome="success",
                    publication_scope="run",
                    run_status="succeeded",
                    attempt_status=None,
                    failure_class=None,
                    retry_disposition=None,
                )
                publication_kwargs = {
                    "run": run,
                    "attempt": attempt,
                    "prepared": prepared,
                    "policy": policy,
                    "occurred_at": _utc_text(now),
                    "actor": request.actor,
                }
                fresh_draft = publication.plan_run_result(**publication_kwargs)
                _require_exact_staged_projection(
                    fresh_drafts=(fresh_draft,),
                    staged_publications=staged_publications,
                )
                published_result = publication.commit(
                    fresh_draft,
                    staged_publications[0],
                )
                if not isinstance(published_result, RunResultPublication):
                    raise IntegrityViolation(
                        "success terminal commit returned another result projection"
                    )
                validate_cassette_scope_pair(
                    run=run,
                    attempt_cassette_artifact_id=(published_result.attempt_cassette_artifact_id),
                    terminal_cassette_artifact_id=(published_result.terminal_cassette_artifact_id),
                )
                result_artifact_id = published_result.result_artifact_id
                accounting.release_attempt(
                    run=run,
                    attempt=attempt,
                    lease=lease,
                    retry_decision=None,
                )
                accounting.close_run(run=run, terminal_status="succeeded")
                event = RunEvent(
                    run_id=run.run_id,
                    seq=run.next_event_seq,
                    event_type="run.succeeded",
                    attempt_no=attempt.attempt_no,
                    occurred_at=_utc_text(now),
                    data_schema_version="run-succeeded@1",
                    data=RunSucceededDataV1(
                        attempt_no=attempt.attempt_no,
                        result_artifact_id=result_artifact_id,
                    ),
                    trace_id=attempt.trace_id,
                )
                persisted = runs.complete_attempt_success(
                    fence=request.fence,
                    ended_at=_utc_text(now),
                    result_artifact_id=result_artifact_id,
                    attempt_cassette_artifact_id=(published_result.attempt_cassette_artifact_id),
                    terminal_cassette_artifact_id=(published_result.terminal_cassette_artifact_id),
                    event=event,
                )
                self._validate_success_close(
                    previous=run,
                    previous_attempt=attempt,
                    previous_lease=lease,
                    persisted=persisted,
                    ended_at=_utc_text(now),
                    result_artifact_id=result_artifact_id,
                    attempt_cassette_artifact_id=(published_result.attempt_cassette_artifact_id),
                    terminal_cassette_artifact_id=(published_result.terminal_cassette_artifact_id),
                    event=event,
                )
                publication.record_run_terminal(
                    run=persisted.run,
                    attempt=persisted.attempt,
                    event=persisted.event,
                    actor=request.actor,
                )
                return AttemptOutcomePublicationResult(
                    run=persisted.run,
                    attempt=persisted.attempt,
                    lease=persisted.lease,
                    event=persisted.event,
                    result_artifact_id=result_artifact_id,
                )

            validate_prepared_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                classifier=classifier,
            )
            retry_decision = self._decide_retry(
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_policy=retry_policy,
                accounting=accounting,
                now=now,
                guard_now=guard_now,
            )
            return self._publish_active_failure(
                runs=runs,
                publication=publication,
                accounting=accounting,
                definition=definition,
                run=run,
                attempt=attempt,
                lease=lease,
                fence=request.fence,
                prepared=prepared,
                retry_decision=retry_decision,
                actor=request.actor,
                now=now,
                lease_expiry_event=False,
                staged_publications=staged_publications,
            )

    def _plan_attempt_outcome(
        self,
        *,
        request: PublishAttemptOutcomeRequest,
        now: datetime,
    ) -> tuple[TerminalPublicationDraft, ...]:
        planning_scope = self._planning_scope
        bind_planning = self._bind_planning_capabilities
        if planning_scope is None or bind_planning is None:
            raise IntegrityViolation("terminal planning authority is unavailable")
        with planning_scope() as read_context:
            capabilities = bind_planning(read_context)
            runs = _required(capabilities.runs, "runs")
            registry = _required(capabilities.registry, "registry")
            accounting = _required(capabilities.accounting, "accounting")
            publication = _required(capabilities.publication, "publication")
            run, attempt, lease = self._load_fenced_attempt(
                runs=runs,
                fence=request.fence,
            )
            if run.status != "running" or attempt.status != "running":
                raise InvalidStateTransition("Run attempt outcome requires a running attempt")
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=request.fence,
                actor=request.actor,
                now=now,
                allowed_statuses=frozenset({"running"}),
            )
            definition, retry_policy, classifier = resolve_lifecycle_bindings(
                run=run,
                registry=registry,
            )
            prepared = publication.preflight_outcome(
                run=run,
                attempt=attempt,
                prepared=request.prepared_outcome,
            )
            if run.cancel_requested_at is not None and (
                not isinstance(prepared, PreparedRunFailure)
                or prepared.failure_class != "subject_superseded"
            ):
                prepared = PreparedRunFailure(
                    run_id=run.run_id,
                    attempt_no=attempt.attempt_no,
                    run_kind=run.kind,
                    artifacts=(),
                    requirement_dispositions=(),
                    cause_code="cancelled",
                    failure_class="cancelled",
                    intrinsic_retry_eligible=False,
                    classifier=run.failure_classifier,
                    redacted_message="Run cancellation requested",
                )
            if isinstance(prepared, PreparedRunResult):
                if (
                    prepared.run_id != run.run_id
                    or prepared.attempt_no != attempt.attempt_no
                    or prepared.run_kind != run.kind
                ):
                    raise IntegrityViolation("prepared result differs from the current Run attempt")
                policy = select_outcome_policy(
                    definition=definition,
                    outcome_code=prepared.summary.outcome_code,
                    prepared_outcome="success",
                    publication_scope="run",
                    run_status="succeeded",
                    attempt_status=None,
                    failure_class=None,
                    retry_disposition=None,
                )
                return (
                    publication.plan_run_result(
                        run=run,
                        attempt=attempt,
                        prepared=prepared,
                        policy=policy,
                        occurred_at=_utc_text(now),
                        actor=request.actor,
                    ),
                )

            validate_prepared_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                classifier=classifier,
            )
            retry_decision = self._decide_retry(
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_policy=retry_policy,
                accounting=accounting,
                now=now,
            )
            return self._plan_active_failure_publications(
                publication=publication,
                definition=definition,
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_decision=retry_decision,
                actor=request.actor,
                now=now,
            )

    def reap_expired_lease(
        self,
        request: ReapExpiredLeaseRequest,
    ) -> AttemptOutcomePublicationResult:
        return self._stage_then_publish(
            plan=lambda operation_now: self._plan_reap_expired_lease(
                request=request,
                now=operation_now,
            ),
            publish=lambda operation_now, staged: self._reap_expired_lease_in_write_uow(
                request=request,
                operation_now=operation_now,
                staged_publications=staged,
            ),
        )

    def _reap_expired_lease_in_write_uow(
        self,
        *,
        request: ReapExpiredLeaseRequest,
        operation_now: datetime,
        staged_publications: tuple[StagedTerminalPublication, ...],
    ) -> AttemptOutcomePublicationResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            registry = _required(capabilities.registry, "registry")
            accounting = _required(capabilities.accounting, "accounting")
            publication = _required(capabilities.publication, "publication")
            guard_now = _utc_now(self._clock)
            now = operation_now
            run = runs.get(request.run_id)
            if run is None:
                raise IntegrityViolation("lease reaper Run does not exist")
            if run.revision != request.expected_run_revision:
                raise Conflict("lease reaper Run revision differs")
            if run.status not in {"leased", "running"} or run.current_attempt_no is None:
                raise InvalidStateTransition("lease reaper requires an active Run attempt")
            attempt = runs.get_attempt(run.run_id, run.current_attempt_no)
            lease = runs.get_current_lease(run.run_id)
            if attempt is None or lease is None:
                raise IntegrityViolation("active Run is missing its attempt or lease")
            if (
                lease.run_id != run.run_id
                or lease.attempt_no != attempt.attempt_no
                or lease.fencing_token != attempt.fencing_token
                or lease.status != "active"
            ):
                raise IntegrityViolation("active Run lease projection is inconsistent")
            if guard_now < _parse_utc(
                lease.expires_at,
                field_name="lease.expires_at",
            ):
                raise InvalidStateTransition("Run lease has not expired")
            definition, retry_policy, classifier = resolve_lifecycle_bindings(
                run=run,
                registry=registry,
            )
            cancellation_requested = run.cancel_requested_at is not None
            prepared = PreparedRunFailure(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                run_kind=run.kind,
                artifacts=(),
                requirement_dispositions=(),
                cause_code="cancelled" if cancellation_requested else "lease_expired",
                failure_class="cancelled" if cancellation_requested else "lease",
                intrinsic_retry_eligible=not cancellation_requested,
                classifier=run.failure_classifier,
                redacted_message=(
                    "cancelled Run worker lease expired"
                    if cancellation_requested
                    else "worker lease expired"
                ),
            )
            prepared = _preflight_control_failure(
                publication=publication,
                run=run,
                attempt=attempt,
                prepared=prepared,
            )
            validate_prepared_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                classifier=classifier,
            )
            retry_decision = self._decide_retry(
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_policy=retry_policy,
                accounting=accounting,
                now=now,
                guard_now=guard_now,
            )
            fence = AttemptWriteFence(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                expected_run_revision=run.revision,
                lease_id=lease.lease_id,
                fencing_token=attempt.fencing_token,
            )
            return self._publish_active_failure(
                runs=runs,
                publication=publication,
                accounting=accounting,
                definition=definition,
                run=run,
                attempt=attempt,
                lease=lease,
                fence=fence,
                prepared=prepared,
                retry_decision=retry_decision,
                actor=request.actor,
                now=now,
                lease_expiry_event=True,
                staged_publications=staged_publications,
            )

    def _plan_reap_expired_lease(
        self,
        *,
        request: ReapExpiredLeaseRequest,
        now: datetime,
    ) -> tuple[TerminalPublicationDraft, ...]:
        planning_scope = self._planning_scope
        bind_planning = self._bind_planning_capabilities
        if planning_scope is None or bind_planning is None:
            raise IntegrityViolation("terminal planning authority is unavailable")
        with planning_scope() as read_context:
            capabilities = bind_planning(read_context)
            runs = _required(capabilities.runs, "runs")
            registry = _required(capabilities.registry, "registry")
            accounting = _required(capabilities.accounting, "accounting")
            publication = _required(capabilities.publication, "publication")
            run = runs.get(request.run_id)
            if run is None:
                raise IntegrityViolation("lease reaper Run does not exist")
            if run.revision != request.expected_run_revision:
                raise Conflict("lease reaper Run revision differs")
            if run.status not in {"leased", "running"} or run.current_attempt_no is None:
                raise InvalidStateTransition("lease reaper requires an active Run attempt")
            attempt = runs.get_attempt(run.run_id, run.current_attempt_no)
            lease = runs.get_current_lease(run.run_id)
            if attempt is None or lease is None:
                raise IntegrityViolation("active Run is missing its attempt or lease")
            if (
                lease.run_id != run.run_id
                or lease.attempt_no != attempt.attempt_no
                or lease.fencing_token != attempt.fencing_token
                or lease.status != "active"
            ):
                raise IntegrityViolation("active Run lease projection is inconsistent")
            if now < _parse_utc(lease.expires_at, field_name="lease.expires_at"):
                raise InvalidStateTransition("Run lease has not expired")
            definition, retry_policy, classifier = resolve_lifecycle_bindings(
                run=run,
                registry=registry,
            )
            cancellation_requested = run.cancel_requested_at is not None
            prepared = PreparedRunFailure(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                run_kind=run.kind,
                artifacts=(),
                requirement_dispositions=(),
                cause_code="cancelled" if cancellation_requested else "lease_expired",
                failure_class="cancelled" if cancellation_requested else "lease",
                intrinsic_retry_eligible=not cancellation_requested,
                classifier=run.failure_classifier,
                redacted_message=(
                    "cancelled Run worker lease expired"
                    if cancellation_requested
                    else "worker lease expired"
                ),
            )
            prepared = _preflight_control_failure(
                publication=publication,
                run=run,
                attempt=attempt,
                prepared=prepared,
            )
            validate_prepared_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                classifier=classifier,
            )
            retry_decision = self._decide_retry(
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_policy=retry_policy,
                accounting=accounting,
                now=now,
            )
            return self._plan_active_failure_publications(
                publication=publication,
                definition=definition,
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_decision=retry_decision,
                actor=request.actor,
                now=now,
            )

    def sweep_timeout(
        self,
        request: SweepRunTimeoutRequest,
    ) -> AttemptOutcomePublicationResult:
        return self._stage_then_publish(
            plan=lambda operation_now: self._plan_sweep_timeout(
                request=request,
                now=operation_now,
            ),
            publish=lambda operation_now, staged: self._sweep_timeout_in_write_uow(
                request=request,
                operation_now=operation_now,
                staged_publications=staged,
            ),
        )

    def _sweep_timeout_in_write_uow(
        self,
        *,
        request: SweepRunTimeoutRequest,
        operation_now: datetime,
        staged_publications: tuple[StagedTerminalPublication, ...],
    ) -> AttemptOutcomePublicationResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            runs = _required(capabilities.runs, "runs")
            registry = _required(capabilities.registry, "registry")
            accounting = _required(capabilities.accounting, "accounting")
            publication = _required(capabilities.publication, "publication")
            guard_now = _utc_now(self._clock)
            now = operation_now
            run = runs.get(request.run_id)
            if run is None:
                raise IntegrityViolation("timeout sweeper Run does not exist")
            if run.revision != request.expected_run_revision:
                raise Conflict("timeout sweeper Run revision differs")
            definition, retry_policy, classifier = resolve_lifecycle_bindings(
                run=run,
                registry=registry,
            )
            if run.status in {"queued", "retry_wait"}:
                if runs.get_current_lease(run.run_id) is not None:
                    raise IntegrityViolation("inactive Run unexpectedly retains an active lease")
                if run.status == "queued":
                    deadline = _parse_utc(
                        run.queue_deadline_utc,
                        field_name="run.queue_deadline_utc",
                    )
                    reason = "queue_deadline_exhausted"
                    cause_code = "queue_timed_out"
                    attempt = None
                else:
                    deadline = _parse_utc(
                        run.overall_deadline_utc,
                        field_name="run.overall_deadline_utc",
                    )
                    reason = "overall_deadline_exhausted"
                    cause_code = "timed_out"
                    latest_no = run.next_attempt_no - 1
                    attempt = runs.get_attempt(run.run_id, latest_no)
                    if attempt is None or attempt.status in {"leased", "running"}:
                        raise IntegrityViolation("retry-wait Run lacks its closed latest attempt")
                if guard_now < deadline:
                    raise InvalidStateTransition("Run timeout deadline has not elapsed")
                prepared = PreparedRunFailure(
                    run_id=run.run_id,
                    attempt_no=attempt.attempt_no if attempt is not None else None,
                    run_kind=run.kind,
                    artifacts=(),
                    requirement_dispositions=(),
                    cause_code=cause_code,
                    failure_class="timeout",
                    intrinsic_retry_eligible=False,
                    classifier=run.failure_classifier,
                    redacted_message="Run deadline exhausted",
                )
                prepared = _preflight_control_failure(
                    publication=publication,
                    run=run,
                    attempt=attempt,
                    prepared=prepared,
                )
                validate_prepared_failure(
                    run=run,
                    attempt=attempt,
                    prepared=prepared,
                    classifier=classifier,
                )
                decision = self._terminal_decision(
                    run=run,
                    prepared=prepared,
                    retry_policy=retry_policy,
                    reason=reason,
                    now=now,
                )
                return self._publish_inactive_failure(
                    runs=runs,
                    publication=publication,
                    accounting=accounting,
                    definition=definition,
                    run=run,
                    attempt=attempt,
                    prepared=prepared,
                    retry_decision=decision,
                    actor=request.actor,
                    now=now,
                    staged_publications=staged_publications,
                )

            if run.status not in {"leased", "running"} or run.current_attempt_no is None:
                raise InvalidStateTransition("Run is not eligible for timeout sweeping")
            attempt = runs.get_attempt(run.run_id, run.current_attempt_no)
            lease = runs.get_current_lease(run.run_id)
            if attempt is None or lease is None:
                raise IntegrityViolation("active Run is missing its attempt or lease")
            overall = _parse_utc(
                run.overall_deadline_utc,
                field_name="run.overall_deadline_utc",
            )
            attempt_deadline = (
                _parse_utc(
                    attempt.attempt_deadline_utc,
                    field_name="attempt.attempt_deadline_utc",
                )
                if attempt.attempt_deadline_utc is not None
                else None
            )
            if guard_now >= overall:
                reason = "overall_deadline_exhausted"
            elif attempt_deadline is not None and guard_now >= attempt_deadline:
                reason = "attempt_deadline_exhausted"
            else:
                raise InvalidStateTransition("Run timeout deadline has not elapsed")
            cancellation_requested = run.cancel_requested_at is not None
            prepared = PreparedRunFailure(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                run_kind=run.kind,
                artifacts=(),
                requirement_dispositions=(),
                cause_code="cancelled" if cancellation_requested else "timed_out",
                failure_class="cancelled" if cancellation_requested else "timeout",
                intrinsic_retry_eligible=False,
                classifier=run.failure_classifier,
                redacted_message=(
                    "Run cancellation requested"
                    if cancellation_requested
                    else "Run deadline exhausted"
                ),
            )
            prepared = _preflight_control_failure(
                publication=publication,
                run=run,
                attempt=attempt,
                prepared=prepared,
            )
            validate_prepared_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                classifier=classifier,
            )
            decision = self._terminal_decision(
                run=run,
                prepared=prepared,
                retry_policy=retry_policy,
                reason=reason,
                now=now,
            )
            fence = AttemptWriteFence(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                expected_run_revision=run.revision,
                lease_id=lease.lease_id,
                fencing_token=attempt.fencing_token,
            )
            return self._publish_active_failure(
                runs=runs,
                publication=publication,
                accounting=accounting,
                definition=definition,
                run=run,
                attempt=attempt,
                lease=lease,
                fence=fence,
                prepared=prepared,
                retry_decision=decision,
                actor=request.actor,
                now=now,
                lease_expiry_event=False,
                staged_publications=staged_publications,
            )

    def _plan_sweep_timeout(
        self,
        *,
        request: SweepRunTimeoutRequest,
        now: datetime,
    ) -> tuple[TerminalPublicationDraft, ...]:
        planning_scope = self._planning_scope
        bind_planning = self._bind_planning_capabilities
        if planning_scope is None or bind_planning is None:
            raise IntegrityViolation("terminal planning authority is unavailable")
        with planning_scope() as read_context:
            capabilities = bind_planning(read_context)
            runs = _required(capabilities.runs, "runs")
            registry = _required(capabilities.registry, "registry")
            publication = _required(capabilities.publication, "publication")
            run = runs.get(request.run_id)
            if run is None:
                raise IntegrityViolation("timeout sweeper Run does not exist")
            if run.revision != request.expected_run_revision:
                raise Conflict("timeout sweeper Run revision differs")
            definition, retry_policy, classifier = resolve_lifecycle_bindings(
                run=run,
                registry=registry,
            )
            if run.status in {"queued", "retry_wait"}:
                if runs.get_current_lease(run.run_id) is not None:
                    raise IntegrityViolation("inactive Run unexpectedly retains an active lease")
                if run.status == "queued":
                    deadline = _parse_utc(
                        run.queue_deadline_utc,
                        field_name="run.queue_deadline_utc",
                    )
                    reason = "queue_deadline_exhausted"
                    cause_code = "queue_timed_out"
                    attempt = None
                else:
                    deadline = _parse_utc(
                        run.overall_deadline_utc,
                        field_name="run.overall_deadline_utc",
                    )
                    reason = "overall_deadline_exhausted"
                    cause_code = "timed_out"
                    attempt = runs.get_attempt(run.run_id, run.next_attempt_no - 1)
                    if attempt is None or attempt.status in {"leased", "running"}:
                        raise IntegrityViolation("retry-wait Run lacks its closed latest attempt")
                if now < deadline:
                    raise InvalidStateTransition("Run timeout deadline has not elapsed")
                prepared = PreparedRunFailure(
                    run_id=run.run_id,
                    attempt_no=(attempt.attempt_no if attempt is not None else None),
                    run_kind=run.kind,
                    artifacts=(),
                    requirement_dispositions=(),
                    cause_code=cause_code,
                    failure_class="timeout",
                    intrinsic_retry_eligible=False,
                    classifier=run.failure_classifier,
                    redacted_message="Run deadline exhausted",
                )
                prepared = _preflight_control_failure(
                    publication=publication,
                    run=run,
                    attempt=attempt,
                    prepared=prepared,
                )
                validate_prepared_failure(
                    run=run,
                    attempt=attempt,
                    prepared=prepared,
                    classifier=classifier,
                )
                decision = self._terminal_decision(
                    run=run,
                    prepared=prepared,
                    retry_policy=retry_policy,
                    reason=reason,  # type: ignore[arg-type]
                    now=now,
                )
                run_status = _run_terminal_status(
                    failure_class=prepared.failure_class,
                    terminal_reason=decision.reason_code,
                )
                policy = select_outcome_policy(
                    definition=definition,
                    outcome_code=prepared.cause_code,
                    prepared_outcome="failure",
                    publication_scope="run",
                    run_status=run_status,
                    attempt_status=None,
                    failure_class=prepared.failure_class,
                    retry_disposition="terminal",
                )
                return (
                    publication.plan_run_failure(
                        run=run,
                        attempt=attempt,
                        prepared=prepared,
                        retry_decision=decision,
                        policy=policy,
                        attempt_failure_artifact_id=None,
                        occurred_at=_utc_text(now),
                        actor=request.actor,
                    ),
                )

            if run.status not in {"leased", "running"} or run.current_attempt_no is None:
                raise InvalidStateTransition("Run is not eligible for timeout sweeping")
            attempt = runs.get_attempt(run.run_id, run.current_attempt_no)
            lease = runs.get_current_lease(run.run_id)
            if attempt is None or lease is None:
                raise IntegrityViolation("active Run is missing its attempt or lease")
            overall = _parse_utc(
                run.overall_deadline_utc,
                field_name="run.overall_deadline_utc",
            )
            attempt_deadline = (
                _parse_utc(
                    attempt.attempt_deadline_utc,
                    field_name="attempt.attempt_deadline_utc",
                )
                if attempt.attempt_deadline_utc is not None
                else None
            )
            if now >= overall:
                reason = "overall_deadline_exhausted"
            elif attempt_deadline is not None and now >= attempt_deadline:
                reason = "attempt_deadline_exhausted"
            else:
                raise InvalidStateTransition("Run timeout deadline has not elapsed")
            cancellation_requested = run.cancel_requested_at is not None
            prepared = PreparedRunFailure(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                run_kind=run.kind,
                artifacts=(),
                requirement_dispositions=(),
                cause_code="cancelled" if cancellation_requested else "timed_out",
                failure_class="cancelled" if cancellation_requested else "timeout",
                intrinsic_retry_eligible=False,
                classifier=run.failure_classifier,
                redacted_message=(
                    "Run cancellation requested"
                    if cancellation_requested
                    else "Run deadline exhausted"
                ),
            )
            prepared = _preflight_control_failure(
                publication=publication,
                run=run,
                attempt=attempt,
                prepared=prepared,
            )
            validate_prepared_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                classifier=classifier,
            )
            decision = self._terminal_decision(
                run=run,
                prepared=prepared,
                retry_policy=retry_policy,
                reason=reason,  # type: ignore[arg-type]
                now=now,
            )
            return self._plan_active_failure_publications(
                publication=publication,
                definition=definition,
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_decision=decision,
                actor=request.actor,
                now=now,
            )

    @staticmethod
    def _decide_retry(
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_policy: RetryPolicySnapshot,
        accounting: RunLifecycleAccountingGateway,
        now: datetime,
        guard_now: datetime | None = None,
    ) -> RetryDecisionV1:
        boundary_now = guard_now or now
        overall = _parse_utc(
            run.overall_deadline_utc,
            field_name="run.overall_deadline_utc",
        )
        terminal_reason: str | None = None
        if boundary_now >= overall:
            terminal_reason = "overall_deadline_exhausted"
        elif attempt.attempt_deadline_utc is not None and boundary_now >= _parse_utc(
            attempt.attempt_deadline_utc,
            field_name="attempt.attempt_deadline_utc",
        ):
            terminal_reason = "attempt_deadline_exhausted"
        elif not prepared.intrinsic_retry_eligible:
            terminal_reason = "not_retry_eligible"
        elif attempt.attempt_no >= retry_policy.max_attempts:
            terminal_reason = "max_attempts_exhausted"
        elif prepared.failure_class not in retry_policy.retryable_failure_classes:
            terminal_reason = "policy_forbidden"
        elif not accounting.retry_budget_available(run=run):
            terminal_reason = "budget_exhausted"

        if terminal_reason is not None:
            return RetryDecisionV1(
                cause_code=prepared.cause_code,
                failure_class=prepared.failure_class,
                intrinsic_retry_eligible=prepared.intrinsic_retry_eligible,
                decision="terminal",
                reason_code=terminal_reason,
                classifier=run.failure_classifier,
                retry_policy=run.retry_policy,
                evaluated_at_utc=_utc_text(now),
            )

        if retry_policy.jitter_policy != "none@1":
            raise IntegrityViolation("unsupported retry jitter policy has no deterministic adapter")
        exponent = max(attempt.attempt_no - 1, 0) if retry_policy.backoff == "exponential" else 0
        delay_ms = min(
            retry_policy.base_delay_ms * (2**exponent),
            retry_policy.max_delay_ms,
        )
        reason: Literal["transient_eligible", "retry_after"] = "transient_eligible"
        if (
            prepared.dependency is not None
            and prepared.dependency.retry_after_ms is not None
            and retry_policy.honor_retry_after
            and prepared.dependency.retry_after_ms > delay_ms
        ):
            delay_ms = prepared.dependency.retry_after_ms
            reason = "retry_after"
        retry_at = now + timedelta(milliseconds=delay_ms)
        if retry_at >= overall:
            return RetryDecisionV1(
                cause_code=prepared.cause_code,
                failure_class=prepared.failure_class,
                intrinsic_retry_eligible=prepared.intrinsic_retry_eligible,
                decision="terminal",
                reason_code="overall_deadline_exhausted",
                classifier=run.failure_classifier,
                retry_policy=run.retry_policy,
                evaluated_at_utc=_utc_text(now),
            )
        return RetryDecisionV1(
            cause_code=prepared.cause_code,
            failure_class=prepared.failure_class,
            intrinsic_retry_eligible=prepared.intrinsic_retry_eligible,
            decision="retry",
            reason_code=reason,
            retry_not_before_utc=_utc_text(retry_at),
            classifier=run.failure_classifier,
            retry_policy=run.retry_policy,
            evaluated_at_utc=_utc_text(now),
        )

    @staticmethod
    def _terminal_decision(
        *,
        run: RunRecord,
        prepared: PreparedRunFailure,
        retry_policy: RetryPolicySnapshot,
        reason: Literal[
            "queue_deadline_exhausted",
            "attempt_deadline_exhausted",
            "overall_deadline_exhausted",
            "not_retry_eligible",
        ],
        now: datetime,
    ) -> RetryDecisionV1:
        if retry_policy.retry_policy_digest != run.retry_policy.retry_policy_digest:
            raise IntegrityViolation("terminal decision retry policy differs from Run")
        return RetryDecisionV1(
            cause_code=prepared.cause_code,
            failure_class=prepared.failure_class,
            intrinsic_retry_eligible=prepared.intrinsic_retry_eligible,
            decision="terminal",
            reason_code=reason,
            classifier=run.failure_classifier,
            retry_policy=run.retry_policy,
            evaluated_at_utc=_utc_text(now),
        )

    @staticmethod
    def _plan_active_failure_publications(
        *,
        publication: RunLifecyclePublicationGateway,
        definition: RunKindDefinition,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        actor: AuditActor,
        now: datetime,
    ) -> tuple[TerminalPublicationDraft, ...]:
        attempt_status = _attempt_terminal_status(
            cause_code=prepared.cause_code,
            failure_class=prepared.failure_class,
        )
        retrying = retry_decision.decision == "retry"
        run_status: Literal["retry_wait", "failed", "cancelled", "timed_out"]
        if retrying:
            run_status = "retry_wait"
        else:
            run_status = _run_terminal_status(
                failure_class=prepared.failure_class,
                terminal_reason=retry_decision.reason_code,
            )
        attempt_policy = select_outcome_policy(
            definition=definition,
            outcome_code=prepared.cause_code,
            prepared_outcome="failure",
            publication_scope="attempt",
            run_status=run_status,
            attempt_status=attempt_status,
            failure_class=prepared.failure_class,
            retry_disposition="retry" if retrying else "terminal",
        )
        run_policy = (
            None
            if retrying
            else select_outcome_policy(
                definition=definition,
                outcome_code=prepared.cause_code,
                prepared_outcome="failure",
                publication_scope="run",
                run_status=run_status,
                attempt_status=attempt_status,
                failure_class=prepared.failure_class,
                retry_disposition="terminal",
            )
        )
        occurred_at = _utc_text(now)
        aggregate = publication.plan_active_failure_aggregate(
            run=run,
            attempt=attempt,
            prepared=prepared,
            retry_decision=retry_decision,
            attempt_policy=attempt_policy,
            run_policy=run_policy,
            occurred_at=occurred_at,
            actor=actor,
        )
        expected_count = 1 if retrying else 2
        if not isinstance(aggregate, tuple) or len(aggregate) != expected_count:
            raise IntegrityViolation("active failure aggregate planner returned another projection")
        return aggregate

    @staticmethod
    def _publish_active_failure(
        *,
        runs: RunLifecycleRepository,
        publication: RunLifecyclePublicationGateway,
        accounting: RunLifecycleAccountingGateway,
        definition: RunKindDefinition,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        fence: AttemptWriteFence,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        actor: AuditActor,
        now: datetime,
        lease_expiry_event: bool,
        staged_publications: tuple[StagedTerminalPublication, ...],
    ) -> AttemptOutcomePublicationResult:
        attempt_status = _attempt_terminal_status(
            cause_code=prepared.cause_code,
            failure_class=prepared.failure_class,
        )
        retrying = retry_decision.decision == "retry"
        run_status: Literal["retry_wait", "failed", "cancelled", "timed_out"]
        if retrying:
            run_status = "retry_wait"
        else:
            run_status = _run_terminal_status(
                failure_class=prepared.failure_class,
                terminal_reason=retry_decision.reason_code,
            )
        attempt_policy = select_outcome_policy(
            definition=definition,
            outcome_code=prepared.cause_code,
            prepared_outcome="failure",
            publication_scope="attempt",
            run_status=run_status,
            attempt_status=attempt_status,
            failure_class=prepared.failure_class,
            retry_disposition="retry" if retrying else "terminal",
        )
        run_policy = (
            None
            if retrying
            else select_outcome_policy(
                definition=definition,
                outcome_code=prepared.cause_code,
                prepared_outcome="failure",
                publication_scope="run",
                run_status=run_status,
                attempt_status=attempt_status,
                failure_class=prepared.failure_class,
                retry_disposition="terminal",
            )
        )
        occurred_at = _utc_text(now)
        expected_count = 1 if retrying else 2
        fresh_drafts = publication.plan_active_failure_aggregate(
            run=run,
            attempt=attempt,
            prepared=prepared,
            retry_decision=retry_decision,
            attempt_policy=attempt_policy,
            run_policy=run_policy,
            occurred_at=occurred_at,
            actor=actor,
        )
        if len(fresh_drafts) != expected_count:
            raise IntegrityViolation("fresh active failure aggregate has another draft count")
        _require_exact_staged_projection(
            fresh_drafts=fresh_drafts,
            staged_publications=staged_publications,
        )
        committed = publication.commit_many(
            tuple(zip(fresh_drafts, staged_publications, strict=True))
        )
        if len(committed) != expected_count:
            raise IntegrityViolation(
                "active failure aggregate commit returned another result count"
            )
        attempt_publication = committed[0]
        if not isinstance(attempt_publication, AttemptFailurePublication):
            raise IntegrityViolation("attempt failure commit returned another result projection")
        run_publication = committed[1] if not retrying else None
        if not retrying and not isinstance(run_publication, RunFailurePublication):
            raise IntegrityViolation("run failure commit returned another result projection")
        validate_attempt_cassette_publication(
            run=run,
            cassette_artifact_id=attempt_publication.cassette_bundle_artifact_id,
        )
        attempt_failure_id = attempt_publication.failure_artifact_id
        accounting.release_attempt(
            run=run,
            attempt=attempt,
            lease=lease,
            retry_decision=retry_decision,
        )

        leading_events: tuple[RunEvent, ...] = ()
        next_seq = run.next_event_seq
        if lease_expiry_event:
            leading_events = (
                RunEvent(
                    run_id=run.run_id,
                    seq=next_seq,
                    event_type="attempt.lease_expired",
                    attempt_no=attempt.attempt_no,
                    occurred_at=occurred_at,
                    data_schema_version="lease-expired@1",
                    data=LeaseExpiredDataV1(
                        attempt_no=attempt.attempt_no,
                        failure_artifact_id=attempt_failure_id,
                        will_retry=retrying,
                    ),
                    trace_id=attempt.trace_id,
                ),
            )
            next_seq += 1

        if retrying:
            retry_at = retry_decision.retry_not_before_utc
            if retry_at is None:
                raise IntegrityViolation("retry decision omitted its not-before timestamp")
            retry_event = RunEvent(
                run_id=run.run_id,
                seq=next_seq,
                event_type="attempt.retry_scheduled",
                attempt_no=attempt.attempt_no,
                occurred_at=occurred_at,
                data_schema_version="retry-scheduled@1",
                data=RetryScheduledDataV1(
                    attempt_no=attempt.attempt_no,
                    failure_artifact_id=attempt_failure_id,
                    cause_code=prepared.cause_code,
                    failure_class=prepared.failure_class,
                    retry_decision=retry_decision,
                    retry_not_before_utc=retry_at,
                ),
                trace_id=attempt.trace_id,
            )
            events = (*leading_events, retry_event)
            persisted = runs.close_attempt_for_retry(
                fence=fence,
                ended_at=occurred_at,
                attempt_status=attempt_status,
                lease_status="expired" if lease_expiry_event else "closed",
                failure_class=prepared.failure_class,
                failure_artifact_id=attempt_failure_id,
                attempt_cassette_artifact_id=(attempt_publication.cassette_bundle_artifact_id),
                retry_decision=retry_decision,
                events=events,
            )
            RunLifecycleService._validate_retry_close(
                previous=run,
                previous_attempt=attempt,
                previous_lease=lease,
                persisted=persisted,
                ended_at=occurred_at,
                attempt_status=attempt_status,
                lease_status="expired" if lease_expiry_event else "closed",
                failure_class=prepared.failure_class,
                failure_artifact_id=attempt_failure_id,
                attempt_cassette_artifact_id=(attempt_publication.cassette_bundle_artifact_id),
                retry_decision=retry_decision,
                events=events,
            )
            publication.record_attempt_closed(
                run=persisted.run,
                attempt=persisted.attempt,
                events=persisted.events,
                actor=actor,
            )
            return AttemptOutcomePublicationResult(
                run=persisted.run,
                attempt=persisted.attempt,
                lease=persisted.lease,
                event=persisted.events[-1],
                retry_decision=retry_decision,
                attempt_failure_artifact_id=attempt_failure_id,
            )

        if run_policy is None:  # pragma: no cover - retry path returned above
            raise IntegrityViolation("terminal failure omitted its run policy")
        if run_publication is None:  # pragma: no cover - checked after commit_many
            raise IntegrityViolation("terminal failure aggregate omitted run result")
        validate_terminal_cassette_publication(
            run=run,
            cassette_artifact_id=run_publication.terminal_cassette_artifact_id,
        )
        if (
            attempt_publication.cassette_bundle_artifact_id is not None
            and attempt_publication.cassette_bundle_artifact_id
            == run_publication.terminal_cassette_artifact_id
        ):
            raise IntegrityViolation("attempt and run cassette bundles must be distinct")
        run_failure_id = run_publication.failure_artifact_id
        if run_failure_id == attempt_failure_id:
            raise IntegrityViolation("run and attempt failure manifests must be distinct")
        accounting.close_run(run=run, terminal_status=run_status)
        terminal_event = RunEvent(
            run_id=run.run_id,
            seq=next_seq,
            event_type=f"run.{run_status}",
            attempt_no=attempt.attempt_no,
            occurred_at=occurred_at,
            data_schema_version="run-terminated@1",
            data=RunTerminatedDataV1(
                attempt_no=attempt.attempt_no,
                failure_artifact_id=run_failure_id,
                cause_code=prepared.cause_code,
            ),
            trace_id=attempt.trace_id,
        )
        persisted_terminal = runs.close_attempt_terminal(
            fence=fence,
            ended_at=occurred_at,
            attempt_status=attempt_status,
            lease_status="expired" if lease_expiry_event else "closed",
            run_status=run_status,
            failure_class=prepared.failure_class,
            attempt_failure_artifact_id=attempt_failure_id,
            run_failure_artifact_id=run_failure_id,
            attempt_cassette_artifact_id=(attempt_publication.cassette_bundle_artifact_id),
            terminal_cassette_artifact_id=(run_publication.terminal_cassette_artifact_id),
            retry_decision=retry_decision,
            leading_events=leading_events,
            terminal_event=terminal_event,
        )
        RunLifecycleService._validate_terminal_close(
            previous=run,
            previous_attempt=attempt,
            previous_lease=lease,
            persisted=persisted_terminal,
            ended_at=occurred_at,
            attempt_status=attempt_status,
            lease_status="expired" if lease_expiry_event else "closed",
            run_status=run_status,
            failure_class=prepared.failure_class,
            attempt_failure_artifact_id=attempt_failure_id,
            run_failure_artifact_id=run_failure_id,
            attempt_cassette_artifact_id=(attempt_publication.cassette_bundle_artifact_id),
            terminal_cassette_artifact_id=(run_publication.terminal_cassette_artifact_id),
            events=(*leading_events, terminal_event),
        )
        publication.record_attempt_closed(
            run=persisted_terminal.run,
            attempt=persisted_terminal.attempt,
            events=(*leading_events, terminal_event),
            actor=actor,
        )
        publication.record_run_terminal(
            run=persisted_terminal.run,
            attempt=persisted_terminal.attempt,
            event=persisted_terminal.event,
            actor=actor,
        )
        return AttemptOutcomePublicationResult(
            run=persisted_terminal.run,
            attempt=persisted_terminal.attempt,
            lease=persisted_terminal.lease,
            event=persisted_terminal.event,
            retry_decision=retry_decision,
            attempt_failure_artifact_id=attempt_failure_id,
            run_failure_artifact_id=run_failure_id,
        )

    @staticmethod
    def _publish_inactive_failure(
        *,
        runs: RunLifecycleRepository,
        publication: RunLifecyclePublicationGateway,
        accounting: RunLifecycleAccountingGateway,
        definition: RunKindDefinition,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        actor: AuditActor,
        now: datetime,
        staged_publications: tuple[StagedTerminalPublication, ...],
    ) -> AttemptOutcomePublicationResult:
        run_status = _run_terminal_status(
            failure_class=prepared.failure_class,
            terminal_reason=retry_decision.reason_code,
        )
        policy = select_outcome_policy(
            definition=definition,
            outcome_code=prepared.cause_code,
            prepared_outcome="failure",
            publication_scope="run",
            run_status=run_status,
            attempt_status=None,
            failure_class=prepared.failure_class,
            retry_disposition="terminal",
        )
        occurred_at = _utc_text(now)
        publication_kwargs = {
            "run": run,
            "attempt": attempt,
            "prepared": prepared,
            "retry_decision": retry_decision,
            "policy": policy,
            "attempt_failure_artifact_id": None,
            "occurred_at": occurred_at,
            "actor": actor,
        }
        fresh_draft = publication.plan_run_failure(**publication_kwargs)
        _require_exact_staged_projection(
            fresh_drafts=(fresh_draft,),
            staged_publications=staged_publications,
        )
        run_publication = publication.commit(
            fresh_draft,
            staged_publications[0],
        )
        if not isinstance(run_publication, RunFailurePublication):
            raise IntegrityViolation("inactive failure commit returned another result projection")
        validate_terminal_cassette_publication(
            run=run,
            cassette_artifact_id=run_publication.terminal_cassette_artifact_id,
        )
        run_failure_id = run_publication.failure_artifact_id
        accounting.close_run(run=run, terminal_status=run_status)
        event = RunEvent(
            run_id=run.run_id,
            seq=run.next_event_seq,
            event_type=f"run.{run_status}",
            attempt_no=None,
            occurred_at=occurred_at,
            data_schema_version="run-terminated@1",
            data=RunTerminatedDataV1(
                attempt_no=attempt.attempt_no if attempt is not None else None,
                failure_artifact_id=run_failure_id,
                cause_code=prepared.cause_code,
            ),
            trace_id=None,
        )
        persisted = runs.terminate_inactive_run(
            run_id=run.run_id,
            expected_run_revision=run.revision,
            run_status=run_status,
            failure_artifact_id=run_failure_id,
            terminal_cassette_artifact_id=(run_publication.terminal_cassette_artifact_id),
            retry_decision=retry_decision,
            event=event,
        )
        RunLifecycleService._validate_inactive_terminal(
            previous=run,
            persisted=persisted,
            run_status=run_status,
            failure_artifact_id=run_failure_id,
            terminal_cassette_artifact_id=(run_publication.terminal_cassette_artifact_id),
            event=event,
        )
        publication.record_run_terminal(
            run=persisted.run,
            attempt=persisted.attempt,
            event=persisted.event,
            actor=actor,
        )
        return AttemptOutcomePublicationResult(
            run=persisted.run,
            attempt=persisted.attempt,
            lease=persisted.lease,
            event=persisted.event,
            retry_decision=retry_decision,
            run_failure_artifact_id=run_failure_id,
        )

    @staticmethod
    def _validate_success_close(
        *,
        previous: RunRecord,
        previous_attempt: RunAttempt,
        previous_lease: RunLease,
        persisted: PersistedRunTerminal,
        ended_at: str,
        result_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        terminal_cassette_artifact_id: str | None,
        event: RunEvent,
    ) -> None:
        expected_run = RunRecord.model_validate(
            {
                **previous.model_dump(mode="python"),
                "status": "succeeded",
                "revision": previous.revision + 1,
                "next_event_seq": previous.next_event_seq + 1,
                "concurrency_permit_group_id": None,
                "result_artifact_id": result_artifact_id,
                "terminal_cassette_artifact_id": terminal_cassette_artifact_id,
                "updated_at": ended_at,
            }
        )
        expected_attempt = RunAttempt.model_validate(
            {
                **previous_attempt.model_dump(mode="python"),
                "status": "succeeded",
                "ended_at": ended_at,
                "cassette_bundle_artifact_id": attempt_cassette_artifact_id,
            }
        )
        expected_lease = previous_lease.model_copy(update={"status": "closed"})
        if (
            persisted.run != expected_run
            or persisted.attempt != expected_attempt
            or persisted.lease != expected_lease
            or persisted.event != event
        ):
            raise IntegrityViolation("successful attempt close returned an invalid projection")

    @staticmethod
    def _validate_retry_close(
        *,
        previous: RunRecord,
        previous_attempt: RunAttempt,
        previous_lease: RunLease,
        persisted: PersistedAttemptClose,
        ended_at: str,
        attempt_status: str,
        lease_status: str,
        failure_class: str,
        failure_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        retry_decision: RetryDecisionV1,
        events: tuple[RunEvent, ...],
    ) -> None:
        expected_run = RunRecord.model_validate(
            {
                **previous.model_dump(mode="python"),
                "status": "retry_wait",
                "revision": previous.revision + 1,
                "current_attempt_no": None,
                "next_event_seq": previous.next_event_seq + len(events),
                "concurrency_permit_group_id": None,
                "retry_not_before_utc": retry_decision.retry_not_before_utc,
                "updated_at": ended_at,
            }
        )
        expected_attempt = RunAttempt.model_validate(
            {
                **previous_attempt.model_dump(mode="python"),
                "status": attempt_status,
                "ended_at": ended_at,
                "failure_class": failure_class,
                "retryable": True,
                "failure_artifact_id": failure_artifact_id,
                "cassette_bundle_artifact_id": attempt_cassette_artifact_id,
            }
        )
        expected_lease = previous_lease.model_copy(update={"status": lease_status})
        if (
            persisted.run != expected_run
            or persisted.attempt != expected_attempt
            or persisted.lease != expected_lease
            or persisted.events != events
        ):
            raise IntegrityViolation("retry attempt close returned an invalid projection")

    @staticmethod
    def _validate_terminal_close(
        *,
        previous: RunRecord,
        previous_attempt: RunAttempt,
        previous_lease: RunLease,
        persisted: PersistedRunTerminal,
        ended_at: str,
        attempt_status: str,
        lease_status: str,
        run_status: str,
        failure_class: str,
        attempt_failure_artifact_id: str,
        run_failure_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        terminal_cassette_artifact_id: str | None,
        events: tuple[RunEvent, ...],
    ) -> None:
        expected_run = RunRecord.model_validate(
            {
                **previous.model_dump(mode="python"),
                "status": run_status,
                "revision": previous.revision + 1,
                "next_event_seq": previous.next_event_seq + len(events),
                "concurrency_permit_group_id": None,
                "retry_not_before_utc": None,
                "failure_artifact_id": run_failure_artifact_id,
                "terminal_cassette_artifact_id": terminal_cassette_artifact_id,
                "updated_at": ended_at,
            }
        )
        expected_attempt = RunAttempt.model_validate(
            {
                **previous_attempt.model_dump(mode="python"),
                "status": attempt_status,
                "ended_at": ended_at,
                "failure_class": failure_class,
                "retryable": False,
                "failure_artifact_id": attempt_failure_artifact_id,
                "cassette_bundle_artifact_id": attempt_cassette_artifact_id,
            }
        )
        expected_lease = previous_lease.model_copy(update={"status": lease_status})
        if (
            persisted.run != expected_run
            or persisted.attempt != expected_attempt
            or persisted.lease != expected_lease
            or persisted.event != events[-1]
        ):
            raise IntegrityViolation("terminal attempt close returned an invalid projection")

    @staticmethod
    def _validate_inactive_terminal(
        *,
        previous: RunRecord,
        persisted: PersistedRunTerminal,
        run_status: str,
        failure_artifact_id: str,
        terminal_cassette_artifact_id: str | None,
        event: RunEvent,
    ) -> None:
        expected = RunRecord.model_validate(
            {
                **previous.model_dump(mode="python"),
                "status": run_status,
                "revision": previous.revision + 1,
                "next_event_seq": previous.next_event_seq + 1,
                "retry_not_before_utc": None,
                "failure_artifact_id": failure_artifact_id,
                "terminal_cassette_artifact_id": terminal_cassette_artifact_id,
                "updated_at": event.occurred_at,
            }
        )
        if persisted.run != expected or persisted.lease is not None or persisted.event != event:
            raise IntegrityViolation("inactive terminal close returned an invalid projection")

    @staticmethod
    def _load_fenced_attempt(
        *,
        runs: RunLifecycleRepository,
        fence: AttemptWriteFence,
    ) -> tuple[RunRecord, RunAttempt, RunLease]:
        run = runs.get(fence.run_id)
        attempt = runs.get_attempt(fence.run_id, fence.attempt_no)
        lease = runs.get_current_lease(fence.run_id)
        if run is None or attempt is None or lease is None:
            raise Conflict("Run attempt write target is no longer current")
        return run, attempt, lease

    @staticmethod
    def _validate_start_result(
        *,
        previous: RunRecord,
        previous_attempt: RunAttempt,
        previous_lease: RunLease,
        persisted: PersistedAttemptStart,
        started_at: str,
        attempt_deadline_utc: str,
        trace_id: str | None,
    ) -> None:
        if previous_attempt.trace_id is not None and trace_id not in {
            None,
            previous_attempt.trace_id,
        }:
            raise IntegrityViolation("attempt start trace differs from the claimed trace")
        effective_trace_id = previous_attempt.trace_id or trace_id
        expected_run = RunRecord.model_validate(
            {
                **previous.model_dump(mode="python"),
                "status": "running",
                "revision": previous.revision + 1,
                "next_event_seq": previous.next_event_seq + 1,
                "updated_at": started_at,
            }
        )
        expected_attempt = RunAttempt.model_validate(
            {
                **previous_attempt.model_dump(mode="python"),
                "status": "running",
                "started_at": started_at,
                "attempt_deadline_utc": attempt_deadline_utc,
                "trace_id": effective_trace_id,
            }
        )
        expected_event = RunEvent(
            run_id=previous.run_id,
            seq=previous.next_event_seq,
            event_type="attempt.started",
            attempt_no=previous_attempt.attempt_no,
            occurred_at=started_at,
            data_schema_version="attempt-started@1",
            data=AttemptStartedDataV1(
                attempt_no=previous_attempt.attempt_no,
                started_at=started_at,
                attempt_deadline_utc=attempt_deadline_utc,
            ),
            trace_id=effective_trace_id,
        )
        if (
            persisted.run != expected_run
            or persisted.attempt != expected_attempt
            or persisted.lease != previous_lease
            or persisted.event != expected_event
        ):
            raise IntegrityViolation("attempt start returned an invalid projection")


__all__ = [
    "AttemptFailurePublication",
    "AttemptWriteFence",
    "AttemptOutcomePublicationResult",
    "LifecycleCapabilityBinder",
    "PermitGroupBinding",
    "PublishAttemptOutcomeRequest",
    "ProgressPublicationResult",
    "RenewLeaseRequest",
    "RenewLeaseResult",
    "ReapExpiredLeaseRequest",
    "RunFailurePublication",
    "RunLifecycleAccountingGateway",
    "RunLifecycleCapabilities",
    "RunLifecyclePublicationGateway",
    "RunLifecycleRepository",
    "RunLifecycleService",
    "RunResultPublication",
    "StartAttemptRequest",
    "StartAttemptResult",
    "SweepRunTimeoutRequest",
    "resolve_lifecycle_bindings",
    "select_outcome_policy",
    "validate_attempt_cassette_publication",
    "validate_attempt_write_fence",
    "validate_prepared_failure",
    "validate_terminal_cassette_publication",
]
