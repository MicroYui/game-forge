"""Resource-specific and generic Run admission (M4c Task 8).

Admission turns a resource/generic Run-creation request into a queued
``RunRecord`` (``202 RunAccepted``) with all authority in one UnitOfWork. It reuses
:class:`gameforge.platform.runs.commands.RunCommandService.create_run` for the
single all-or-nothing UoW (idempotency, budget hold, ``RunRecord`` + initial
``run.queued`` event, DB queue authority, create-scope audit) and never duplicates
that logic. On top of it, admission:

* Fixes ``creation_mode`` and ``kind@version`` per endpoint. ``POST /runs`` accepts
  only ``generic_runs_endpoint`` kinds; each resource endpoint fixes its own kind;
  ``internal_only`` (migrate/DR) is reachable only through a trusted internal path.
* Builds the exact :class:`RunPayloadEnvelope`: the precise Artifact input set,
  resolved execution-profile bindings (one per registry requirement field-path,
  resolved through the exact catalog), LLM mode and seed per the RunKind table.
* Mints the authenticated ``source_raw`` Artifact BEFORE Run creation for
  generation/constraint goal text — the naked text never enters the Run payload,
  telemetry, or event. Failure leaves no executable Run.
* Re-reads subject/target revisions for the three validation RunKinds and closes
  the Task-7 ``ValidationAdmissionPort`` seam.

This module also supplies the concrete admission-scope collaborators the
composition root wires into ``create_run`` (a per-run budget-plan provider, a
conservative usage provider, and a create-scope audit/publication gateway).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol

from gameforge.contracts.api import (
    ConstraintValidationAdmissionRequestV1,
    PatchValidationAdmissionRequestV1,
    RollbackValidationAdmissionRequestV1,
    RunAcceptedV1,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cost import (
    BudgetReservationV1,
    BudgetSetSnapshotV1,
    BudgetSnapshotV1,
    BudgetV1,
    CostAmountV1,
    ReservationGroupV1,
)
from gameforge.contracts.errors import (
    Conflict,
    DependencyUnavailable,
    Forbidden,
    IntegrityViolation,
)
from gameforge.contracts.execution_profiles import (
    ExecutionProfileCatalogSnapshotV1,
    ExecutionProfileKindV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.identity import (
    ActorContext,
    DomainRegistryV1,
    DomainScope,
    DomainScopeValue,
    Permission,
    RolePolicy,
)
from gameforge.contracts.jobs import (
    BenchRunPayloadV1,
    CheckerRunPayloadV1,
    ConstraintProposalProposePayloadV1,
    ConstraintValidationPayloadV1,
    ExecutionVersionPlanV1,
    GenerationProposePayloadV1,
    PatchRepairPayloadV1,
    PatchValidationPayloadV1,
    PlaytestRunPayloadV1,
    PromptGoalBindingV1,
    RefReadBindingV1,
    ResolvedArtifactRequirementV1,
    ResolvedPolicyCountBindingV1,
    ResolvedPolicySnapshotV1,
    ResolvedPolicySubsetCountBindingV1,
    ReviewRunPayloadV1,
    RollbackValidationPayloadV1,
    RunKindDefinition,
    RunKindPayload,
    RunPayloadEnvelope,
    SimulationRunPayloadV1,
    TaskSuiteDerivePayloadV1,
    ValidationSubjectBindingV1,
    referenced_input_artifact_ids,
    resolved_policy_snapshot_digest,
)
from gameforge.contracts.lineage import (
    ArtifactKind,
    ArtifactV2,
    AuditActor,
    AuditCorrelation,
    AuditSubject,
    VersionTuple,
)
from gameforge.contracts.storage import ObjectStore, UtcClock
from gameforge.contracts.workflow import (
    ApprovalItem,
    FindingEvidenceBindingV1,
)
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.cost_policy.run_accounting import (
    AttemptConservativeUsageProvider,
    RunBudgetPlan,
    RunBudgetPlanProvider,
    SqlRunCostAccounting,
)
from gameforge.platform.provenance.writer import AuthenticatedGoalSourceWriter, MintedSource
from gameforge.platform.rbac import AuthorizationDecision, authorize
from gameforge.platform.runs.commands import (
    CapabilityBinder,
    RunCommandCapabilities,
    RunCommandService,
    RunCreateRequest,
    RunPublicationGateway,
    RunRegistryGateway,
    RunUnitOfWork,
)


# ── UTC helpers ──────────────────────────────────────────────────────────────
def _utc_now(clock: UtcClock) -> datetime:
    now = clock.now_utc()
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("admission clock must return UTC")
    return now.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_pointer(document: Any, pointer: str) -> Any:
    """Resolve a simple RFC 6901 JSON pointer (``/a/b/0``) or return ``None``."""

    if pointer == "":
        return document
    current = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return None
            current = current[token]
        elif isinstance(current, (list, tuple)):
            if not token.isdecimal() or str(int(token)) != token:
                return None
            index = int(token)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


# ── admission-scope cost collaborators (Task-8 BUILD gap) ────────────────────
def _cost_amount(dimension: str, value: int, *, unit: str) -> CostAmountV1:
    return CostAmountV1(dimension=dimension, value=Decimal(value), unit=unit)  # type: ignore[arg-type]


# A generous per-run default limit set. concurrent_run is a permit-only dimension
# and is required so attempt-time permit acquisition (worker, Task 10) can resolve.
_DEFAULT_RUN_LIMITS: tuple[CostAmountV1, ...] = (
    _cost_amount("input_token", 100_000_000, unit="token"),
    _cost_amount("output_token", 100_000_000, unit="token"),
    _cost_amount("request", 1_000_000, unit="request"),
    _cost_amount("agent_step", 1_000_000, unit="step"),
    _cost_amount("wall_time_ns", 3_600_000_000_000, unit="ns"),
    _cost_amount("concurrent_run", 16, unit="count"),
)
_DEFAULT_RUN_RESERVATION: tuple[CostAmountV1, ...] = (_cost_amount("request", 1, unit="request"),)


class RunBudgetLedger(Protocol):
    """Narrow ledger surface the default budget-plan provider needs."""

    def get_budget(self, budget_id: str) -> BudgetV1 | None: ...

    def put_budget(self, budget: BudgetV1) -> BudgetV1: ...


class DefaultRunBudgetPlanProvider:
    """Mint a per-run budget-set snapshot + hold group for the admitted Run.

    A run-scoped :class:`BudgetV1` is created once (idempotent per run_id) and
    snapshotted into the exact ``budget_set_snapshot_id`` the admission engine
    stamped on the payload, so ``SqlRunCostAccounting.reserve_run_budget`` can freeze
    it inside the same UnitOfWork.
    """

    def __init__(
        self,
        *,
        ledger: RunBudgetLedger,
        clock: UtcClock,
        selection_policy_version: str = "run-budget-selection@1",
        budget_policy_version: str = "run-budget-policy@1",
        limits: tuple[CostAmountV1, ...] = _DEFAULT_RUN_LIMITS,
        reservation: tuple[CostAmountV1, ...] = _DEFAULT_RUN_RESERVATION,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._selection_policy_version = selection_policy_version
        self._budget_policy_version = budget_policy_version
        self._limits = limits
        self._reservation = reservation

    def resolve_run_budget(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        request_hash: str,
        initiated_by: AuditActor,
    ) -> RunBudgetPlan:
        del initiated_by
        now = _utc_now(self._clock)
        budget_id = f"budget:run:{run_id}"
        budget = self._ledger.get_budget(budget_id)
        if budget is None:
            budget = BudgetV1(
                budget_id=budget_id,
                scope_kind="run",
                scope_id=run_id,
                policy_version=self._budget_policy_version,
                limits=self._limits,
                reserved=(),
                consumed=(),
                status="active",
                revision=1,
                created_at=now,
            )
            budget = self._ledger.put_budget(budget)
        snapshot = BudgetSnapshotV1(
            snapshot_id=f"snapshot:{budget_set_snapshot_id}:{budget_id}",
            budget_id=budget_id,
            scope_kind="run",
            scope_id=run_id,
            policy_version=budget.policy_version,
            budget_revision_at_freeze=budget.revision,
            limits=budget.limits,
            reserved=budget.reserved,
            consumed=budget.consumed,
            captured_at=now,
        )
        budget_set = BudgetSetSnapshotV1(
            budget_set_snapshot_id=budget_set_snapshot_id,
            run_id=run_id,
            selection_policy_version=self._selection_policy_version,
            snapshots=(snapshot,),
            captured_at=now,
        )
        group_id = f"hold:{run_id}"
        reservation = BudgetReservationV1(
            reservation_id=f"reservation:{group_id}:{budget_id}",
            reservation_group_id=group_id,
            budget_id=budget_id,
            reserved=self._reservation,
            status="reserved",
            revision=1,
        )
        hold = ReservationGroupV1(
            reservation_group_id=group_id,
            scope="run_budget_hold",
            run_id=run_id,
            budget_set_snapshot_id=budget_set_snapshot_id,
            request_hash=f"sha256:{request_hash}",
            idempotency_key=f"hold-idempotency:{run_id}",
            budget_reservation_ids=(reservation.reservation_id,),
            status="reserved",
            revision=1,
            created_at=now,
        )
        return RunBudgetPlan(budget_set=budget_set, hold_group=hold, reservations=(reservation,))


class ConservativeAttemptUsageProvider:
    """Upper-bound observation provider for a stranded attempt group.

    Admission never strands attempt groups (it only creates run-level holds), so the
    provider is present for the ``SqlRunCostAccounting`` contract but its worker-time
    settlement path is exercised by Task 10, not by admission.
    """

    def conservative_usage(self, *, group: Any, reservations: Any, recorded_at: datetime) -> Any:
        raise IntegrityViolation("attempt settlement is not an admission-scope operation")


class AdmissionRunPublicationGateway:
    """Create-scope audit gateway (terminal publication is Task 9)."""

    def __init__(self, *, audit: AuditGate, chain_id: str) -> None:
        self._audit = audit
        self._chain_id = chain_id

    def record_run_created(self, *, run: Any, event: Any) -> None:
        self._audit.append(
            chain_id=self._chain_id,
            actor=run.initiated_by,
            initiated_by=None,
            action="run.queued",
            subject=AuditSubject(resource_kind="run", resource_id=run.run_id),
            correlation=AuditCorrelation(
                request_id=None,
                run_id=run.run_id,
                trace_id=(
                    run.dispatch_trace_carrier.traceparent
                    if run.dispatch_trace_carrier is not None
                    else None
                ),
            ),
        )

    def _unsupported(self, *_: Any, **__: Any) -> Any:
        raise IntegrityViolation("only run creation is published at admission scope")

    record_run_claimed = _unsupported
    get_prompt_replay = _unsupported
    publish_prompt_rendered = _unsupported
    publish_run_failure = _unsupported
    record_command_submitted = _unsupported
    record_command_completed = _unsupported
    record_run_terminal = _unsupported


# ── read authorities admission needs before Run creation ─────────────────────
@dataclass(frozen=True, slots=True)
class AdmissionReadPort:
    """One short read transaction's authorities for pre-admission validation."""

    policies: Any
    approvals: Any
    artifacts: Any
    refs: Any


AdmissionReadScope = Callable[[], AbstractContextManager[AdmissionReadPort]]


@dataclass(frozen=True, slots=True)
class AdmissionDeadlinePolicy:
    queue_ttl_seconds: int = 300
    overall_ttl_seconds: int = 1800
    attempt_timeout_ns: int = 600_000_000_000


@dataclass(frozen=True, slots=True)
class AdmissionRequestContext:
    """Server-owned metadata forwarded from the transport for one admission."""

    idempotency_key: str
    request_hash: str
    trace_id: str | None = None


# Reverse of the frozen Run-kind payload schema map: a params schema selects its
# exact RunKind. Admission never trusts a client-supplied kind — the payload type
# (and the endpoint) fixes it.
_KIND_BY_SCHEMA: dict[str, RunKindRef] = {
    "generation-propose@1": RunKindRef(kind="generation.propose", version=1),
    "patch-repair@1": RunKindRef(kind="patch.repair", version=1),
    "constraint-proposal-propose@1": RunKindRef(kind="constraint_proposal.propose", version=1),
    "review-run@1": RunKindRef(kind="review.run", version=1),
    "checker-run@1": RunKindRef(kind="checker.run", version=1),
    "simulation-run@1": RunKindRef(kind="simulation.run", version=1),
    "task-suite-derive@1": RunKindRef(kind="task_suite.derive", version=1),
    "playtest-run@1": RunKindRef(kind="playtest.run", version=1),
    "bench-run@1": RunKindRef(kind="bench.run", version=1),
    "artifact-migration@1": RunKindRef(kind="artifact.migrate", version=1),
    "dr-drill@1": RunKindRef(kind="dr.drill", version=1),
}


# The producer ``tool_version`` the run's PRIMARY output Artifact carries. The terminal
# publisher re-derives the primary Artifact's VersionTuple with a ``producer_value``
# projection off the frozen run payload's ``version_tuple.tool_version`` and fails closed
# unless it matches the executor's emitted tuple. Admission therefore stamps the exact
# producer tool so ``executor primary artifact tuple == publisher re-projection``. (The
# Task-17a first-full-composition surfaced that the prior ``admission-<schema>``
# placeholder leaked into that authoritative projection and blocked terminal
# publication for every deterministic Run kind.) Unknown schemas keep the placeholder.
_PRODUCER_TOOL_VERSIONS: dict[str, str] = {
    "checker-run@1": "checker@1",
    "simulation-run@1": "economy-sim@1",
    "task-suite-derive@1": "task-suite@1",
    "review-run@1": "review@1",
    "bench-run@1": "bench@1",
    "playtest-run@1": "playtest@1",
    "generation-propose@1": "generation@1",
    "constraint-proposal-propose@1": "extraction@1",
    "patch-repair@1": "repair@1",
    "patch-validation@1": "patch-validation@1",
    "constraint-validation@1": "constraint-validation@1",
    "rollback-validation@1": "rollback-validation@1",
}


# The exact resolved profile field-path a validation kind's resolved-policy
# snapshot is anchored to (the profile that determines its re-verification
# dimensions). Patch/constraint validate under ``validation_policy``; rollback
# under ``rollback_profile``.
_VALIDATION_POLICY_FIELD: dict[type, str] = {
    PatchValidationPayloadV1: "/params/validation_policy",
    ConstraintValidationPayloadV1: "/params/validation_policy",
    RollbackValidationPayloadV1: "/params/rollback_profile",
}


def _validation_regression_requirement_ids(params: RunKindPayload) -> tuple[str, ...]:
    """The ordered ``requirement_id``s the deterministic validation handler seals as
    ``regression_evidence`` for the given payload.

    This MUST mirror each handler's dimension enumeration exactly (the frozen
    resolved-policy snapshot is the publisher's cardinality oracle): patch seals one
    per checker + simulation + regression suite; constraint one per regression
    suite; rollback the fixed history/artifact/schema/profile dimensions + one per
    impact profile + regression suite.
    """

    if isinstance(params, PatchValidationPayloadV1):
        return (
            *(f"checker:{p.profile_id}@{p.version}" for p in params.checker_profiles),
            *(f"simulation:{p.profile_id}@{p.version}" for p in params.simulation_profiles),
            *(f"regression:{suite_id}" for suite_id in params.regression_suite_artifact_ids),
        )
    if isinstance(params, ConstraintValidationPayloadV1):
        return tuple(f"regression:{suite_id}" for suite_id in params.regression_suite_artifact_ids)
    if isinstance(params, RollbackValidationPayloadV1):
        return (
            "history",
            "artifact",
            "schema",
            "profile",
            *(f"impact:{p.profile_id}@{p.version}" for p in params.impact_profiles),
            *(f"regression:{suite_id}" for suite_id in params.regression_suite_artifact_ids),
        )
    return ()


def _build_resolved_policy_snapshot(
    *,
    resolved_policy_id: str,
    source_profile_field_path: str,
    source_profile_payload_hash: str,
    requirements: tuple[ResolvedArtifactRequirementV1, ...],
) -> ResolvedPolicySnapshotV1:
    body = {
        "snapshot_schema_version": "resolved-policy@1",
        "resolved_policy_id": resolved_policy_id,
        "source_profile_field_path": source_profile_field_path,
        "source_profile_payload_hash": source_profile_payload_hash,
        "requirements": [requirement.model_dump(mode="json") for requirement in requirements],
    }
    return ResolvedPolicySnapshotV1(
        resolved_policy_id=resolved_policy_id,
        source_profile_field_path=source_profile_field_path,
        source_profile_payload_hash=source_profile_payload_hash,
        requirements=requirements,
        digest=resolved_policy_snapshot_digest(body),
    )


# ── artifact-kind allowlist per §5.3 (exact-input-set kind check) ────────────
_ANY_KIND: tuple[ArtifactKind, ...] = ()  # sentinel: any ArtifactKind allowed
_FINDING_EVIDENCE_KINDS: tuple[ArtifactKind, ...] = (
    "review_report",
    "checker_run",
    "simulation_run",
    "playtest_trace",
    "validation_evidence",
    "regression_evidence",
)


# ── source-write instruction produced before Run creation ────────────────────
@dataclass(frozen=True, slots=True)
class _SourceWrite:
    minted: MintedSource


class ValidationStartWriter(Protocol):
    """Atomically CAS the ApprovalItem ``draft->validating`` in the Run-create UoW.

    Injected by the composition root; when present, the validation-admission path binds
    the ``draft->validating`` workflow CAS (with its ``active_validation_run_id``) into
    the SAME UnitOfWork that queues the validation Run (design §"validation start": one
    all-or-nothing UoW). ``start`` receives the bound write transaction directly, so it
    writes through the exact same authority. Absent (a generic ``POST /runs`` engine, or
    a test that drives the CAS itself), admission never mutates the subject.
    """

    def start(
        self,
        transaction: Any,
        *,
        item: ApprovalItem,
        run_id: str,
        actor: ActorContext,
        request_id: str | None,
        trace_id: str | None,
    ) -> None: ...


class RunAdmissionEngine:
    """Compose resource/generic Run admission on top of ``RunCommandService``."""

    def __init__(
        self,
        *,
        run_commands: RunCommandService,
        unit_of_work: RunUnitOfWork,
        read_scope: AdmissionReadScope,
        registry: RunRegistryGateway,
        execution_profile_catalog: ExecutionProfileCatalogSnapshotV1,
        goal_writer: AuthenticatedGoalSourceWriter,
        object_store: ObjectStore,
        clock: UtcClock,
        source_uow_capabilities: Callable[[Any], "_SourceWriteCapabilities"],
        role_policy_version: str,
        role_policy_digest: str,
        deadline_policy: AdmissionDeadlinePolicy | None = None,
        validation_start_writer: ValidationStartWriter | None = None,
    ) -> None:
        self._run_commands = run_commands
        self._unit_of_work = unit_of_work
        self._read_scope = read_scope
        self._registry = registry
        self._catalog = execution_profile_catalog
        self._goal_writer = goal_writer
        self._objects = object_store
        self._clock = clock
        self._source_capabilities = source_uow_capabilities
        self._validation_start_writer = validation_start_writer
        if not isinstance(role_policy_version, str) or not role_policy_version:
            raise IntegrityViolation("run admission requires an exact role policy version")
        if (
            not isinstance(role_policy_digest, str)
            or len(role_policy_digest) != 64
            or any(character not in "0123456789abcdef" for character in role_policy_digest)
        ):
            raise IntegrityViolation(
                "run admission requires a lowercase SHA-256 role policy digest"
            )
        self._role_policy_version = role_policy_version
        self._role_policy_digest = role_policy_digest
        self._deadlines = deadline_policy or AdmissionDeadlinePolicy()

    # ── Task-7 ValidationAdmissionPort seam ──────────────────────────────────
    def admit(
        self,
        *,
        operation: str,
        resource_id: str,
        request: PatchValidationAdmissionRequestV1
        | ConstraintValidationAdmissionRequestV1
        | RollbackValidationAdmissionRequestV1,
        actor: ActorContext,
        server: Any,
    ) -> RunAcceptedV1:
        with self._read_scope() as read:
            item = self._load_validation_subject(
                read=read,
                approval_id=request.approval_id,
                expected_workflow_revision=request.expected_workflow_revision,
                expected_subject_head_revision=request.expected_subject_head_revision,
                subject_digest=request.subject_digest,
            )
            run_id = self._derive_run_id(
                scope=f"approval:{request.approval_id}",
                key=server.idempotency_key,
                request_hash=server.request_hash,
            )
            subject = ValidationSubjectBindingV1(
                approval_id=item.approval_id,
                expected_workflow_revision=item.workflow_revision + 1,
                subject_head_revision=item.subject_revision,
                subject_artifact_id=item.subject_artifact_id,
                subject_digest=item.subject_digest,
                active_validation_run_id=run_id,
            )
            if operation == "patch.validate":
                kind = RunKindRef(kind="patch.validate", version=1)
                if not isinstance(request, PatchValidationAdmissionRequestV1):
                    raise IntegrityViolation("patch validation received the wrong request type")
                params: RunKindPayload = PatchValidationPayloadV1(
                    subject=subject,
                    base_snapshot_artifact_id=request.base_snapshot_artifact_id,
                    preview_snapshot_artifact_id=request.preview_snapshot_artifact_id,
                    candidate_config_export_artifact_ids=(
                        request.candidate_config_export_artifact_ids
                    ),
                    target=request.target,
                    validation_policy=request.validation_policy,
                    checker_profiles=request.checker_profiles,
                    simulation_profiles=request.simulation_profiles,
                    findings=request.findings,
                    review_artifact_ids=request.review_artifact_ids,
                    playtest_trace_artifact_ids=request.playtest_trace_artifact_ids,
                    regression_suite_artifact_ids=request.regression_suite_artifact_ids,
                )
                version_tuple = self._subject_version_tuple(item, "patch-validation@1")
            elif operation == "constraint.validate":
                kind = RunKindRef(kind="constraint_proposal.validate", version=1)
                if not isinstance(request, ConstraintValidationAdmissionRequestV1):
                    raise IntegrityViolation(
                        "constraint validation received the wrong request type"
                    )
                params = ConstraintValidationPayloadV1(
                    subject=subject,
                    base_constraint_snapshot_artifact_id=(
                        request.base_constraint_snapshot_artifact_id
                    ),
                    target=request.target,
                    dsl_grammar_version=request.dsl_grammar_version,
                    compiler_profile=request.compiler_profile,
                    differential_engines=request.differential_engines,
                    golden_suite_artifact_id=request.golden_suite_artifact_id,
                    regression_suite_artifact_ids=request.regression_suite_artifact_ids,
                    validation_policy=request.validation_policy,
                )
                version_tuple = self._subject_version_tuple(item, "constraint-validation@1")
            elif operation == "rollback.validate":
                kind = RunKindRef(kind="rollback.validate", version=1)
                if not isinstance(request, RollbackValidationAdmissionRequestV1):
                    raise IntegrityViolation("rollback validation received the wrong request type")
                params = RollbackValidationPayloadV1(
                    subject=subject,
                    ref_name=request.ref_name,
                    expected_current_ref=request.expected_current_ref,
                    target_artifact_id=request.target_artifact_id,
                    target_history_revision=request.target_history_revision,
                    rollback_profile=request.rollback_profile,
                    schema_compatibility_policy=request.schema_compatibility_policy,
                    impact_profiles=request.impact_profiles,
                    regression_suite_artifact_ids=request.regression_suite_artifact_ids,
                )
                version_tuple = self._subject_version_tuple(item, "rollback-validation@1")
            else:
                raise IntegrityViolation("unknown validation admission operation")

            del resource_id
            return self._admit_run(
                run_id=run_id,
                kind=kind,
                creation_mode="resource_endpoint_only",
                params=params,
                version_tuple=version_tuple,
                llm_execution_mode="not_applicable",
                seed=None,
                read=read,
                actor=actor,
                idempotency_scope=f"approval:{request.approval_id}",
                idempotency_key=server.idempotency_key,
                request_hash=server.request_hash,
                trace_id=getattr(server, "trace_id", None),
                validation_item=item,
                companion_write=self._validation_companion(
                    item=item, run_id=run_id, actor=actor, server=server
                ),
            )

    def _validation_companion(
        self,
        *,
        item: ApprovalItem,
        run_id: str,
        actor: ActorContext,
        server: Any,
    ) -> Callable[[Any], None] | None:
        """The ``draft->validating`` CAS to run in the Run-create UoW (or ``None``)."""

        writer = self._validation_start_writer
        if writer is None:
            return None

        def _companion(transaction: Any) -> None:
            writer.start(
                transaction,
                item=item,
                run_id=run_id,
                actor=actor,
                request_id=getattr(server, "request_id", None),
                trace_id=getattr(server, "trace_id", None),
            )

        return _companion

    # ── generic POST /runs (generic_runs_endpoint kinds only) ────────────────
    def admit_generic_run(
        self,
        *,
        params: RunKindPayload,
        actor: ActorContext,
        server: AdmissionRequestContext,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"] = (
            "not_applicable"
        ),
        seed: int | None = None,
        execution_version_plan: ExecutionVersionPlanV1 | None = None,
        cassette_artifact_id: str | None = None,
    ) -> RunAcceptedV1:
        """Admit a generic review/checker/simulation/bench Run from ``POST /runs``.

        The kind is fixed by the params schema, never a client-supplied kind, and
        must be a ``generic_runs_endpoint`` kind. ``resource_endpoint_only`` and
        ``internal_only`` kinds are rejected here.
        """

        kind = self._kind_for_params(params)
        return self._admit_public(
            kind=kind,
            creation_mode="generic_runs_endpoint",
            params=params,
            actor=actor,
            server=server,
            idempotency_scope=f"principal:{actor.principal.id}",
            llm_execution_mode=llm_execution_mode,
            seed=seed,
            execution_version_plan=execution_version_plan,
            cassette_artifact_id=cassette_artifact_id,
            source_writes=(),
        )

    # ── resource endpoints that pre-build their params ───────────────────────
    def admit_resource_run(
        self,
        *,
        params: RunKindPayload,
        actor: ActorContext,
        server: AdmissionRequestContext,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"] = (
            "not_applicable"
        ),
        seed: int | None = None,
        execution_version_plan: ExecutionVersionPlanV1 | None = None,
        cassette_artifact_id: str | None = None,
    ) -> RunAcceptedV1:
        """Admit a resource-endpoint Run (repair/task_suite/playtest) with fixed kind."""

        kind = self._kind_for_params(params)
        return self._admit_public(
            kind=kind,
            creation_mode="resource_endpoint_only",
            params=params,
            actor=actor,
            server=server,
            idempotency_scope=f"principal:{actor.principal.id}",
            llm_execution_mode=llm_execution_mode,
            seed=seed,
            execution_version_plan=execution_version_plan,
            cassette_artifact_id=cassette_artifact_id,
            source_writes=(),
        )

    def admit_internal_run(
        self,
        *,
        params: RunKindPayload,
        actor: ActorContext,
        server: AdmissionRequestContext,
    ) -> RunAcceptedV1:
        """Admit an ``internal_only`` Run (migrate/DR) via a trusted internal actor."""

        if actor.principal.kind != "system":
            raise IntegrityViolation("internal-only Run admission requires a trusted system actor")
        kind = self._kind_for_params(params)
        return self._admit_public(
            kind=kind,
            creation_mode="internal_only",
            params=params,
            actor=actor,
            server=server,
            idempotency_scope=f"internal:{actor.principal.id}",
            llm_execution_mode="not_applicable",
            seed=None,
            execution_version_plan=None,
            cassette_artifact_id=None,
            source_writes=(),
        )

    # ── generation:propose (mints authenticated source_raw goal) ─────────────
    def admit_generation(
        self,
        *,
        base_snapshot_artifact_id: str,
        constraint_snapshot_artifact_id: str | None,
        findings: tuple[FindingEvidenceBindingV1, ...],
        objective_goal_text: str,
        domain_scope: DomainScope,
        target: RefReadBindingV1,
        generation_policy: ProfileRefV1,
        candidate_export_profiles: tuple[ProfileRefV1, ...],
        actor: ActorContext,
        server: AdmissionRequestContext,
        llm_execution_mode: Literal["live", "record", "replay"] = "record",
        execution_version_plan: ExecutionVersionPlanV1 | None = None,
        cassette_artifact_id: str | None = None,
    ) -> RunAcceptedV1:
        source = self._mint_goal_source(actor=actor, text=objective_goal_text)
        params = GenerationProposePayloadV1(
            base_snapshot_artifact_id=base_snapshot_artifact_id,
            constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
            findings=findings,
            objective_goal=PromptGoalBindingV1(
                source_artifact_id=source.minted.artifact.artifact_id,
                expected_payload_hash=source.minted.artifact.payload_hash,
            ),
            domain_scope=domain_scope,
            target=target,
            generation_policy=generation_policy,
            candidate_export_profiles=candidate_export_profiles,
        )
        return self._admit_public(
            kind=RunKindRef(kind="generation.propose", version=1),
            creation_mode="resource_endpoint_only",
            params=params,
            actor=actor,
            server=server,
            idempotency_scope=f"principal:{actor.principal.id}",
            llm_execution_mode=llm_execution_mode,
            seed=None,
            execution_version_plan=execution_version_plan,
            cassette_artifact_id=cassette_artifact_id,
            source_writes=(source,),
        )

    # ── constraints:propose (mints authenticated source_raw goal) ────────────
    def admit_constraint_proposal(
        self,
        *,
        source_artifact_ids: tuple[str, ...],
        base_constraint_snapshot_artifact_id: str | None,
        authoring_goal_text: str,
        domain_scope: DomainScope,
        dsl_grammar_version: str,
        extraction_policy: ProfileRefV1,
        actor: ActorContext,
        server: AdmissionRequestContext,
        llm_execution_mode: Literal["live", "record", "replay"] = "record",
        execution_version_plan: ExecutionVersionPlanV1 | None = None,
        cassette_artifact_id: str | None = None,
    ) -> RunAcceptedV1:
        source = self._mint_goal_source(actor=actor, text=authoring_goal_text)
        params = ConstraintProposalProposePayloadV1(
            source_artifact_ids=source_artifact_ids,
            base_constraint_snapshot_artifact_id=base_constraint_snapshot_artifact_id,
            domain_scope=domain_scope,
            authoring_goal=PromptGoalBindingV1(
                source_artifact_id=source.minted.artifact.artifact_id,
                expected_payload_hash=source.minted.artifact.payload_hash,
            ),
            dsl_grammar_version=dsl_grammar_version,
            extraction_policy=extraction_policy,
        )
        return self._admit_public(
            kind=RunKindRef(kind="constraint_proposal.propose", version=1),
            creation_mode="resource_endpoint_only",
            params=params,
            actor=actor,
            server=server,
            idempotency_scope=f"principal:{actor.principal.id}",
            llm_execution_mode=llm_execution_mode,
            seed=None,
            execution_version_plan=execution_version_plan,
            cassette_artifact_id=cassette_artifact_id,
            source_writes=(source,),
        )

    def _kind_for_params(self, params: RunKindPayload) -> RunKindRef:
        kind = _KIND_BY_SCHEMA.get(params.schema_version)
        if kind is None:
            raise IntegrityViolation("Run params schema is not a retained Run kind")
        return kind

    def _admit_public(
        self,
        *,
        kind: RunKindRef,
        creation_mode: Literal["generic_runs_endpoint", "resource_endpoint_only", "internal_only"],
        params: RunKindPayload,
        actor: ActorContext,
        server: AdmissionRequestContext,
        idempotency_scope: str,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        seed: int | None,
        execution_version_plan: ExecutionVersionPlanV1 | None,
        cassette_artifact_id: str | None,
        source_writes: tuple[_SourceWrite, ...],
    ) -> RunAcceptedV1:
        run_id = self._derive_run_id(
            scope=idempotency_scope,
            key=server.idempotency_key,
            request_hash=server.request_hash,
        )
        # Blob-first: persist the minted source_raw Artifact(s) BEFORE Run creation
        # so the payload can reference their ids/hashes and input verification finds
        # them. A later create_run failure leaves them as GC-eligible orphans, never
        # an executable Run.
        self._persist_sources(source_writes)
        with self._read_scope() as read:
            return self._admit_run(
                run_id=run_id,
                kind=kind,
                creation_mode=creation_mode,
                params=params,
                version_tuple=self._params_version_tuple(params),
                llm_execution_mode=llm_execution_mode,
                seed=seed,
                read=read,
                actor=actor,
                idempotency_scope=idempotency_scope,
                idempotency_key=server.idempotency_key,
                request_hash=server.request_hash,
                trace_id=server.trace_id,
                execution_version_plan=execution_version_plan,
                cassette_artifact_id=cassette_artifact_id,
            )

    def _persist_sources(self, source_writes: tuple[_SourceWrite, ...]) -> None:
        if not source_writes:
            return
        with self._unit_of_work.begin() as transaction:
            capabilities = self._source_capabilities(transaction)
            for write in source_writes:
                capabilities.object_bindings.bind_verified(
                    write.minted.stored.ref,
                    write.minted.stored.location,
                    None,
                )
                capabilities.artifacts.put(write.minted.artifact)

    @staticmethod
    def _params_version_tuple(params: RunKindPayload) -> VersionTuple:
        # Admission stamps the run's producer tool_version; the executor's PRIMARY
        # output Artifact carries the same value and the publisher re-derives it via a
        # producer_value projection (see _PRODUCER_TOOL_VERSIONS). Unknown schemas keep
        # a deterministic placeholder.
        tool_version = _PRODUCER_TOOL_VERSIONS.get(
            params.schema_version, f"admission-{params.schema_version}"
        )
        return VersionTuple(tool_version=tool_version)

    # ── shared admission path ────────────────────────────────────────────────
    def _admit_run(
        self,
        *,
        run_id: str,
        kind: RunKindRef,
        creation_mode: Literal["generic_runs_endpoint", "resource_endpoint_only", "internal_only"],
        params: RunKindPayload,
        version_tuple: VersionTuple,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        seed: int | None,
        read: AdmissionReadPort,
        actor: ActorContext,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
        trace_id: str | None,
        execution_version_plan: Any = None,
        cassette_artifact_id: str | None = None,
        validation_item: ApprovalItem | None = None,
        companion_write: Callable[[Any], None] | None = None,
    ) -> RunAcceptedV1:
        definition = self._resolve_definition(kind, creation_mode)
        # Authorize the actor against the RunKind's required permission with the
        # server-resolved concrete domain BEFORE any input probing, budget hold, or
        # Run creation. Fail-closed in one UoW: an unauthorized actor never leaves a
        # RunRecord or a partial reservation.
        self._authorize(
            definition=definition,
            params=params,
            read=read,
            actor=actor,
            validation_item=validation_item,
        )
        # Verify every referenced input Artifact resolves with an allowed kind and
        # the exact set (no hidden extras) before the Run is created.
        self._verify_input_artifacts(params=params, read=read)

        resolved_profiles = self._resolve_profiles(params=params, kind=kind, read=read)
        resolved_policy_snapshots = self._resolve_policy_snapshots(
            params=params, definition=definition, resolved_profiles=resolved_profiles
        )
        budget_set_snapshot_id = f"budget-set:{run_id}"
        payload = RunPayloadEnvelope(
            payload_schema_version=definition.payload_schema_id,
            input_artifact_ids=self._input_artifact_ids(params, cassette_artifact_id),
            version_tuple=version_tuple,
            execution_version_plan=execution_version_plan,
            policy_bindings=(),
            schema_bindings=(),
            execution_profile_catalog_version=self._catalog.catalog_version,
            execution_profile_catalog_digest=self._catalog.catalog_digest,
            resolved_profiles=resolved_profiles,
            resolved_policy_snapshots=resolved_policy_snapshots,
            budget_set_snapshot_id=budget_set_snapshot_id,
            seed=seed,
            llm_execution_mode=llm_execution_mode,
            cassette_artifact_id=cassette_artifact_id,
            params=params,
        )

        now = _utc_now(self._clock)
        request = RunCreateRequest(
            run_id=run_id,
            kind=kind,
            creation_mode=creation_mode,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            payload=payload,
            dispatch_trace_carrier=None,
            initiated_by=AuditActor(
                principal_id=actor.principal.id,
                principal_kind=actor.principal.kind,
            ),
            queue_deadline_utc=_utc_text(
                now + timedelta(seconds=self._deadlines.queue_ttl_seconds)
            ),
            attempt_timeout_ns=self._deadlines.attempt_timeout_ns,
            overall_deadline_utc=_utc_text(
                now + timedelta(seconds=self._deadlines.overall_ttl_seconds)
            ),
        )
        del trace_id
        result = self._run_commands.create_run(request, companion_write=companion_write)
        run_id = result.run.run_id
        return RunAcceptedV1(
            run_id=run_id,
            status_url=f"/api/v1/runs/{run_id}",
            events_url=f"/api/v1/runs/{run_id}/events",
        )

    # ── validation subject re-read ───────────────────────────────────────────
    def _load_validation_subject(
        self,
        *,
        read: AdmissionReadPort,
        approval_id: str,
        expected_workflow_revision: int,
        expected_subject_head_revision: int,
        subject_digest: str,
    ) -> ApprovalItem:
        item = read.approvals.get(approval_id)
        if not isinstance(item, ApprovalItem):
            raise Conflict("validation subject approval item is unavailable")
        if item.workflow_revision != expected_workflow_revision:
            raise Conflict(
                "validation workflow revision differs",
                expected_revision=expected_workflow_revision,
                actual_revision=item.workflow_revision,
            )
        if item.subject_revision != expected_subject_head_revision:
            raise Conflict(
                "validation subject head revision differs",
                expected_revision=expected_subject_head_revision,
                actual_revision=item.subject_revision,
            )
        if item.subject_digest != subject_digest:
            raise Conflict("validation subject digest differs from the retained subject")
        if item.target_binding is None:
            raise IntegrityViolation("validation subject has no exact target binding")
        return item

    @staticmethod
    def _subject_version_tuple(item: ApprovalItem, schema_version: str) -> VersionTuple:
        # Stamp the run's producer ``tool_version`` (the validation executor's tool,
        # not the ``admission-<schema>`` placeholder) and ``seed=0`` so the terminal
        # publisher's ``producer_value`` projection matches the executor's primary
        # ``evidence-set@1`` VersionTuple (see ``_PRODUCER_TOOL_VERSIONS``). Task 17a
        # added this alignment for the generic Run kinds; the validation resource
        # admission was the remaining producer-tuple gap that blocked publishing a
        # ``patch.validate`` EvidenceSet through ``TerminalPublisher``.
        binding = item.target_binding
        snapshot_id = getattr(binding, "target_snapshot_id", None)
        tool_version = _PRODUCER_TOOL_VERSIONS.get(schema_version, f"admission-{schema_version}")
        return VersionTuple(ir_snapshot_id=snapshot_id, tool_version=tool_version, seed=0)

    def _resolve_policy_snapshots(
        self,
        *,
        params: RunKindPayload,
        definition: RunKindDefinition,
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
    ) -> tuple[ResolvedPolicySnapshotV1, ...]:
        """Freeze the resolved-policy snapshots the outcome-policy cardinality
        bindings re-project at terminal publication.

        A ``ResolvedPolicyCountBindingV1`` / ``ResolvedPolicySubsetCountBindingV1``
        on an outcome rule asserts the published artifact count equals the frozen
        requirement count for ``(resolved_policy_id, outcome_rule_id)``. Admission
        resolves those requirements from the exact validation payload (one
        regression requirement per re-verification dimension the deterministic
        handler will seal) so the publisher's ``validate_rule_cardinality`` closes.
        The snapshot is anchored to the resolved validation/rollback profile
        (field-path + payload hash) and fails closed if that profile is absent.

        Scope: 17b wires the validation family (patch/constraint/rollback), whose
        dimensions are exactly payload-derivable. ``generation-gate`` /
        ``repair-verifier`` also carry resolved-policy count bindings, but their
        dimensions are not payload-derivable the same way (generation's are declared
        by the resolved gate profile, not the payload); wiring those snapshots is a
        separate follow-up, so their prior (unwired) admission behavior is preserved
        here — they are not part of Journey B and do not publish end-to-end today.
        """

        field_path = _VALIDATION_POLICY_FIELD.get(type(params))
        if field_path is None:
            return ()

        referenced: dict[str, set[str]] = {}
        for policy in definition.outcome_policies:
            for rule in policy.artifact_rules:
                binding = rule.count_binding
                if isinstance(
                    binding,
                    (ResolvedPolicyCountBindingV1, ResolvedPolicySubsetCountBindingV1),
                ):
                    referenced.setdefault(binding.resolved_policy_id, set()).add(
                        binding.outcome_rule_id
                    )
        if not referenced:
            return ()

        source = next(
            (binding for binding in resolved_profiles if binding.field_path == field_path),
            None,
        )
        if source is None:
            raise IntegrityViolation(
                "resolved-policy snapshot requires the resolved validation profile",
                field_path=field_path,
            )
        regression_ids = _validation_regression_requirement_ids(params)

        snapshots: list[ResolvedPolicySnapshotV1] = []
        for resolved_policy_id in sorted(referenced):
            outcome_rule_ids = referenced[resolved_policy_id]
            requirements: list[ResolvedArtifactRequirementV1] = []
            for outcome_rule_id in sorted(outcome_rule_ids):
                if outcome_rule_id != "regression":
                    raise IntegrityViolation(
                        "resolved-policy binding references an unsupported outcome rule",
                        resolved_policy_id=resolved_policy_id,
                        outcome_rule_id=outcome_rule_id,
                    )
                for ordinal, requirement_id in enumerate(regression_ids, start=1):
                    requirements.append(
                        ResolvedArtifactRequirementV1(
                            requirement_id=requirement_id,
                            outcome_rule_id="regression",
                            artifact_kind="regression_evidence",
                            payload_schema_id="regression-evidence@1",
                            ordinal=ordinal,
                        )
                    )
            snapshots.append(
                _build_resolved_policy_snapshot(
                    resolved_policy_id=resolved_policy_id,
                    source_profile_field_path=field_path,
                    source_profile_payload_hash=source.profile_payload_hash,
                    requirements=tuple(requirements),
                )
            )
        return tuple(snapshots)

    # ── profile resolution (reuses registry requirement metadata) ────────────
    def _resolve_profiles(
        self,
        *,
        params: RunKindPayload,
        kind: RunKindRef,
        read: AdmissionReadPort,
    ) -> tuple[ResolvedExecutionProfileBindingV1, ...]:
        requirements = self._registry.get_profile_requirements(kind)
        if requirements is None:
            raise IntegrityViolation("Run kind has no exact profile requirement metadata")
        document = {"params": params.model_dump(mode="python")}
        resolved: list[ResolvedExecutionProfileBindingV1] = []
        for requirement in requirements:
            value = _resolve_pointer(document, requirement.field_path)
            kind_name: ExecutionProfileKindV1 = requirement.expected_profile_kind
            if requirement.cardinality in {"one", "optional"}:
                if value is None:
                    if requirement.cardinality == "one":
                        raise IntegrityViolation("Run payload omits a required profile binding")
                    continue
                resolved.append(
                    self._resolve_one(
                        read=read,
                        field_path=requirement.field_path,
                        profile=ProfileRefV1.model_validate(value),
                        expected_profile_kind=kind_name,
                    )
                )
            else:
                if not isinstance(value, (list, tuple)):
                    raise IntegrityViolation("many profile requirement must bind an array field")
                for index, item in enumerate(value):
                    resolved.append(
                        self._resolve_one(
                            read=read,
                            field_path=f"{requirement.field_path}/{index}",
                            profile=ProfileRefV1.model_validate(item),
                            expected_profile_kind=kind_name,
                        )
                    )
        return tuple(resolved)

    def _resolve_one(
        self,
        *,
        read: AdmissionReadPort,
        field_path: str,
        profile: ProfileRefV1,
        expected_profile_kind: ExecutionProfileKindV1,
    ) -> ResolvedExecutionProfileBindingV1:
        return read.policies.resolve_execution_profile(
            catalog_version=self._catalog.catalog_version,
            catalog_digest=self._catalog.catalog_digest,
            field_path=field_path,
            profile=profile,
            expected_profile_kind=expected_profile_kind,
        )

    # ── RBAC authorization with the server-resolved resource domain ──────────
    def _authorize(
        self,
        *,
        definition: RunKindDefinition,
        params: RunKindPayload,
        read: AdmissionReadPort,
        actor: ActorContext,
        validation_item: ApprovalItem | None,
    ) -> None:
        """Reject any actor lacking the RunKind permission for the resolved domain.

        The RunKind's ``required_permission`` carries a registry-only ``domain_scope``
        marker (``"all"`` for the dynamic content kinds, ``None`` for the non-domain
        DR kind). Admission replaces that marker with the concrete domain resolved
        server-side from the loaded subject/resource — never from a client field —
        and authorizes it with the same pure function every other write path uses
        (:func:`gameforge.platform.rbac.authorization.authorize`).
        """

        role_policy = read.policies.get_role_policy(
            self._role_policy_version,
            self._role_policy_digest,
        )
        if not isinstance(role_policy, RolePolicy):
            raise DependencyUnavailable(
                "run admission role policy is unavailable",
                component="run_admission_authorization",
            )
        registry = read.policies.get_domain_registry(role_policy.domain_registry_ref)
        if not isinstance(registry, DomainRegistryV1):
            raise DependencyUnavailable(
                "run admission domain registry is unavailable",
                component="run_admission_authorization",
            )
        requested = self._requested_permission(
            definition=definition,
            params=params,
            registry=registry,
            validation_item=validation_item,
        )
        if (
            authorize(
                principal=actor.principal,
                role_policy=role_policy,
                requested_permission=requested,
                domain_registry=registry,
            )
            is not AuthorizationDecision.ALLOW
        ):
            raise Forbidden(
                "actor is not authorized to admit this run kind in the resolved domain",
                action=requested.action,
                resource_kind=requested.resource_kind,
            )

    def _requested_permission(
        self,
        *,
        definition: RunKindDefinition,
        params: RunKindPayload,
        registry: DomainRegistryV1,
        validation_item: ApprovalItem | None,
    ) -> Permission:
        base = definition.required_permission
        scope = self._resolve_permission_domain(
            base=base,
            params=params,
            registry=registry,
            validation_item=validation_item,
        )
        return Permission(
            action=base.action,
            resource_kind=base.resource_kind,
            domain_scope=scope,
        )

    def _resolve_permission_domain(
        self,
        *,
        base: Permission,
        params: RunKindPayload,
        registry: DomainRegistryV1,
        validation_item: ApprovalItem | None,
    ) -> DomainScopeValue:
        """Resolve the concrete authorization domain server-side (never a client field).

        ``base.domain_scope`` is the frozen RunKind marker: ``None`` for the sole
        non-domain kind (``dr.drill``) which stands as-is, or ``"all"`` for the
        dynamic content kinds whose marker MUST be replaced by the resource-derived
        scope before authz (see the frozen registry table and design §5.4).
        """

        if base.domain_scope != "all":
            return base.domain_scope
        if isinstance(
            params,
            (
                PatchValidationPayloadV1,
                ConstraintValidationPayloadV1,
                RollbackValidationPayloadV1,
            ),
        ):
            # Validation subject domain is authoritative: the loaded ApprovalItem's
            # exact ``domain_scope`` (subject binding), never a client field.
            if validation_item is None:
                raise IntegrityViolation("validation admission lost its loaded subject domain")
            return validation_item.domain_scope
        if isinstance(params, (GenerationProposePayloadV1, ConstraintProposalProposePayloadV1)):
            # The declared target domain is the requested authorization scope; the
            # actor must independently hold a grant covering it, so a client cannot
            # escalate to (or smuggle in) a domain it lacks — authorize() governs.
            return params.domain_scope
        # repair / checker / simulation / review / bench / task_suite / playtest carry
        # their subject/selection/dataset domain on resources that do not yet expose a
        # domain on the admission read port. Until that provenance lands, admission is
        # fail-closed: it requires authority over every active registry domain rather
        # than trusting any narrower client claim.
        return self._all_active_domains(registry)

    @staticmethod
    def _all_active_domains(registry: DomainRegistryV1) -> DomainScope:
        active = tuple(
            definition.domain_id
            for definition in registry.definitions
            if definition.status == "active"
        )
        if not active:
            raise IntegrityViolation("domain registry has no active domain for run admission")
        return DomainScope(domain_ids=active)

    # ── exact input-set kind verification ────────────────────────────────────
    def _verify_input_artifacts(self, *, params: RunKindPayload, read: AdmissionReadPort) -> None:
        for artifact_id, allowed in self._input_kind_requirements(params):
            artifact = read.artifacts.get(artifact_id)
            if not isinstance(artifact, ArtifactV2):
                raise Conflict("Run input Artifact is unavailable", artifact_id=artifact_id)
            if allowed and artifact.kind not in allowed:
                raise IntegrityViolation(
                    "Run input Artifact kind is not allowed for this field",
                    artifact_id=artifact_id,
                    kind=artifact.kind,
                )

    @staticmethod
    def _input_kind_requirements(
        params: RunKindPayload,
    ) -> tuple[tuple[str, tuple[ArtifactKind, ...]], ...]:
        checks: list[tuple[str, tuple[ArtifactKind, ...]]] = []

        def add(artifact_id: str | None, allowed: tuple[ArtifactKind, ...]) -> None:
            if artifact_id is not None:
                checks.append((artifact_id, allowed))

        def add_ref(target: RefReadBindingV1, allowed: tuple[ArtifactKind, ...]) -> None:
            expected = target.expected_ref
            if expected is not None:
                checks.append((expected.artifact_id, allowed))

        if isinstance(params, PatchValidationPayloadV1):
            add(params.subject.subject_artifact_id, ("patch",))
            add(params.base_snapshot_artifact_id, ("ir_snapshot",))
            add(params.preview_snapshot_artifact_id, ("ir_snapshot",))
            add_ref(params.target, ("ir_snapshot",))
            for config in params.candidate_config_export_artifact_ids:
                add(config, ("config_export",))
            for review in params.review_artifact_ids:
                add(review, ("review_report",))
            for trace in params.playtest_trace_artifact_ids:
                add(trace, ("playtest_trace",))
            for suite in params.regression_suite_artifact_ids:
                add(suite, ("regression_suite",))
            for binding in params.findings:
                add(binding.evidence_artifact_id, _FINDING_EVIDENCE_KINDS)
        elif isinstance(params, ConstraintValidationPayloadV1):
            add(params.subject.subject_artifact_id, ("constraint_proposal",))
            add(params.base_constraint_snapshot_artifact_id, ("constraint_snapshot",))
            add_ref(params.target, ("ir_snapshot",))
            add(params.golden_suite_artifact_id, ("golden_suite",))
            for suite in params.regression_suite_artifact_ids:
                add(suite, ("regression_suite",))
        elif isinstance(params, RollbackValidationPayloadV1):
            add(params.subject.subject_artifact_id, ("rollback_request",))
            add(params.expected_current_ref.artifact_id, ("ir_snapshot",))
            add(params.target_artifact_id, ("ir_snapshot",))
            for suite in params.regression_suite_artifact_ids:
                add(suite, ("regression_suite",))
        elif isinstance(params, GenerationProposePayloadV1):
            add(params.base_snapshot_artifact_id, ("ir_snapshot",))
            add(params.constraint_snapshot_artifact_id, ("constraint_snapshot",))
            add(params.objective_goal.source_artifact_id, ("source_raw",))
            add_ref(params.target, ("ir_snapshot",))
            for binding in params.findings:
                add(binding.evidence_artifact_id, _FINDING_EVIDENCE_KINDS)
        elif isinstance(params, PatchRepairPayloadV1):
            add(params.subject_patch_artifact_id, ("patch",))
            add(params.base_snapshot_artifact_id, ("ir_snapshot",))
            add(params.preview_snapshot_artifact_id, ("ir_snapshot",))
            add(params.constraint_snapshot_artifact_id, ("constraint_snapshot",))
            add(params.validation_evidence_artifact_id, ("validation_evidence",))
            add_ref(params.target, ("ir_snapshot",))
            for binding in params.findings:
                add(binding.evidence_artifact_id, _FINDING_EVIDENCE_KINDS)
            for suite in params.regression_suite_artifact_ids:
                add(suite, ("regression_suite",))
        elif isinstance(params, ConstraintProposalProposePayloadV1):
            add(params.authoring_goal.source_artifact_id, ("source_raw",))
            add(params.base_constraint_snapshot_artifact_id, ("constraint_snapshot",))
            for source in params.source_artifact_ids:
                add(source, ("source_raw", "source_rendered"))
        elif isinstance(params, CheckerRunPayloadV1):
            add(params.snapshot_artifact_id, ("ir_snapshot",))
            add(params.constraint_snapshot_artifact_id, ("constraint_snapshot",))
        elif isinstance(params, SimulationRunPayloadV1):
            add(params.snapshot_artifact_id, ("ir_snapshot",))
            add(params.constraint_snapshot_artifact_id, ("constraint_snapshot",))
            add(params.scenario_artifact_id, ("scenario_spec",))
        elif isinstance(params, ReviewRunPayloadV1):
            add(params.snapshot_artifact_id, ("ir_snapshot",))
            add(params.constraint_snapshot_artifact_id, ("constraint_snapshot",))
        elif isinstance(params, PlaytestRunPayloadV1):
            add(params.config_artifact_id, ("config_export",))
            add(params.constraint_snapshot_artifact_id, ("constraint_snapshot",))
            add(params.task_suite_artifact_id, ("task_suite",))
            for episode in params.episodes:
                add(episode.scenario_spec_artifact_id, ("scenario_spec",))
        elif isinstance(params, TaskSuiteDerivePayloadV1):
            add(params.source_preview_artifact_id, ("ir_snapshot",))
            add(params.config_artifact_id, ("config_export",))
            add(params.constraint_snapshot_artifact_id, ("constraint_snapshot",))
        elif isinstance(params, BenchRunPayloadV1):
            add(params.dataset_artifact_id, ("bench_dataset",))
            add(params.benchmark_spec_artifact_id, ("benchmark_spec",))
        return tuple(checks)

    # ── envelope helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _input_artifact_ids(
        params: RunKindPayload, cassette_artifact_id: str | None
    ) -> tuple[str, ...]:
        referenced = list(referenced_input_artifact_ids(params))
        if cassette_artifact_id is not None:
            referenced.append(cassette_artifact_id)
        return tuple(sorted(set(referenced)))

    def _resolve_definition(
        self,
        kind: RunKindRef,
        creation_mode: str,
    ) -> RunKindDefinition:
        definition = self._registry.get_run_kind(kind)
        if definition is None:
            raise IntegrityViolation("Run kind is not retained in the exact registry")
        if definition.status != "active":
            raise IntegrityViolation("Run kind definition is not active")
        if definition.creation_mode != creation_mode:
            raise IntegrityViolation("Run kind is not allowed at this creation surface")
        return definition

    def _derive_run_id(self, *, scope: str, key: str, request_hash: str) -> str:
        digest = canonical_sha256(
            {
                "run_id_schema_version": "run-admission-id@1",
                "idempotency_scope": scope,
                "idempotency_key": key,
                "request_hash": request_hash,
            }
        )
        return f"run:{digest}"

    # ── source_raw minting for goal text ─────────────────────────────────────
    def _mint_goal_source(self, *, actor: ActorContext, text: str) -> _SourceWrite:
        minted = self._goal_writer.mint(
            object_store=self._objects,
            actor=actor,
            text=text,
            created_at=_utc_text(_utc_now(self._clock)),
        )
        return _SourceWrite(minted=minted)


@dataclass(frozen=True, slots=True)
class _SourceWriteCapabilities:
    """The narrow write authorities admission needs to persist a source_raw."""

    artifacts: Any
    object_bindings: Any


def build_admission_capability_binder(
    *,
    registry: RunRegistryGateway,
    clock: UtcClock,
    audit_chain_id: str,
    budget_limits: tuple[CostAmountV1, ...] = _DEFAULT_RUN_LIMITS,
) -> CapabilityBinder:
    """Bind ``RunCommandCapabilities`` for create-scope admission over a write UoW."""

    def bind(transaction: Any) -> RunCommandCapabilities:
        plan_provider: RunBudgetPlanProvider = DefaultRunBudgetPlanProvider(
            ledger=transaction.cost,
            clock=clock,
            limits=budget_limits,
        )
        settlement: AttemptConservativeUsageProvider = ConservativeAttemptUsageProvider()
        accounting = SqlRunCostAccounting(
            ledger=transaction.cost,
            plan_provider=plan_provider,
            settlement_provider=settlement,
            clock=clock,
        )
        publication: RunPublicationGateway = AdmissionRunPublicationGateway(
            audit=AuditGate(sink=transaction.audit, clock=clock),
            chain_id=audit_chain_id,
        )
        return RunCommandCapabilities(
            runs=transaction.runs,
            registry=registry,
            admission=accounting,
            publication=publication,
            accounting=None,
        )

    return bind


__all__ = [
    "AdmissionDeadlinePolicy",
    "AdmissionReadPort",
    "AdmissionReadScope",
    "AdmissionRequestContext",
    "AdmissionRunPublicationGateway",
    "ConservativeAttemptUsageProvider",
    "DefaultRunBudgetPlanProvider",
    "RunAdmissionEngine",
    "RunBudgetLedger",
    "_SourceWriteCapabilities",
    "build_admission_capability_binder",
]
