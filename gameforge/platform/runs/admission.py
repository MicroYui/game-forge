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

import json
from hmac import compare_digest
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import islice
from typing import Any, Literal, Protocol

from gameforge.contracts.api import (
    ConstraintValidationAdmissionRequestV1,
    PatchValidationAdmissionRequestV1,
    RollbackValidationAdmissionRequestV1,
    RunAcceptedV1,
)
from gameforge.contracts.benchmark import (
    MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL,
    MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT,
    MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_TOTAL,
    MAX_BENCHMARK_CASE_EXECUTIONS,
    MAX_BENCHMARK_CHECKER_WORK_UNITS,
    MAX_BENCHMARK_REPORT_BYTES,
    MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL,
    MAX_BENCHMARK_SIMULATION_WORK_UNITS,
    BenchmarkAggregateInputBindingV1,
    BenchmarkDatasetV1,
    BenchmarkEvaluatorProfileConfigV1,
    BenchmarkSpecV1,
    sampled_partition_cases,
    validate_benchmark_aggregate_producer_seed_authority,
)
from gameforge.contracts.canonical import canonical_json, canonical_sha256, sha256_lowerhex
from gameforge.contracts.config_export import (
    MAX_CONFIG_EXPORT_MANIFEST_BYTES,
    MAX_CONFIG_EXPORT_PACKAGE_BYTES,
    ConfigExportPackageV1,
    decode_config_export_bytes,
)
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
    IdempotencyConflict,
    IntegrityViolation,
    StaleTaskSuite,
)
from gameforge.contracts.execution_graphs import AgentExecutionGraphV1
from gameforge.contracts.execution_profiles import (
    ArtifactCollectionResolvedPolicyRequirementConfigV1,
    ConfigExportProfileDetailsV1,
    EnvironmentProfileDetailsV1,
    ExecutionProfileCatalogSnapshotV1,
    ExecutionProfileKindV1,
    FixedResolvedPolicyRequirementConfigV1,
    GenerationProfileConfigV1,
    MigrationProfileDetailsV1,
    PatchRepairProfileConfigV1,
    PlaytestPlannerProfileConfigV2,
    ProfileRefV1,
    ProfileCollectionResolvedPolicyRequirementConfigV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    TaskSuiteDerivationProfileConfigV2,
    execution_profile_payload_hash,
)
from gameforge.contracts.findings import (
    FindingRevisionV1,
    PatchV2,
    finding_revision_digest,
)
from gameforge.contracts.identity import (
    ActorContext,
    DomainRegistryV1,
    DomainScope,
    DomainScopeValue,
    Permission,
    Principal,
    RolePolicy,
)
from gameforge.contracts.jobs import (
    ArtifactMigrationPayloadV1,
    BenchRunPayloadV1,
    CheckerRunPayloadV1,
    ConstraintProposalProposePayloadV1,
    ConstraintValidationPayloadV1,
    DrDrillPayloadV1,
    ExecutionVersionPlanV1,
    GenerationProposePayloadV1,
    MAX_COLLECTION_ITEMS,
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
    RunDispatchTraceCarrierV1,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunKindDefinition,
    RunKindPayload,
    RunPayloadEnvelope,
    RunRecord,
    RunResultV1,
    SimulationRunPayloadV1,
    TaskSuiteDerivePayloadV1,
    ValidationSubjectBindingV1,
    patch_repair_requires_root_seed,
    referenced_input_artifact_ids,
    resolved_policy_snapshot_digest,
    run_kind_definition_digest,
    validation_regression_requires_root_seed,
)
from gameforge.contracts.lineage import (
    ArtifactKind,
    ArtifactV2,
    AuditActor,
    AuditCorrelation,
    AuditSubject,
    VersionTuple,
    build_version_set_projection,
)
from gameforge.contracts.storage import ObjectStore, RefValue, UtcClock
from gameforge.contracts.playtest import (
    PlaytestTraceV1,
    ScenarioSpecV1,
    TaskEpisodeV1,
    TaskSuiteV1,
    playtest_resource_upper_bounds,
    resolve_completion_oracle,
)
from gameforge.contracts.provenance import ProvenanceV1
from gameforge.contracts.review import ReviewReport
from gameforge.contracts.workflow import (
    ApprovalItem,
    ConstraintProposalV1,
    EvidenceSet,
    FindingEvidenceBindingV1,
    RollbackRequestV1,
    RollbackTargetBindingV1,
)
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.platform.cost_policy.run_accounting import (
    AttemptConservativeUsageProvider,
    RunBudgetPlan,
    RunBudgetPlanProvider,
    SqlRunCostAccounting,
)
from gameforge.platform.provenance.writer import AuthenticatedGoalSourceWriter, MintedSource
from gameforge.platform.playtest_payload_schemas import PlaytestPayloadValidationService
from gameforge.platform.rbac import AuthorizationDecision, authorize
from gameforge.platform.registry.defaults import (
    ARTIFACT_PAYLOAD_SCHEMAS,
    PROFILE_OUTPUT_SCHEMA_REQUIREMENTS,
)
from gameforge.platform.registry.model import FROZEN_RUN_KIND_IDENTITIES_BY_PAYLOAD_SCHEMA
from gameforge.platform.run_handlers.checker import validate_checker_work_budget
from gameforge.platform.run_handlers.constraint_validation import (
    BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1,
)
from gameforge.platform.run_handlers.simulation import (
    validate_economy_simulation_work_budget,
)
from gameforge.platform.runs.commands import (
    CapabilityBinder,
    RunCommandCapabilities,
    RunCommandService,
    RunCreateRequest,
    RunPublicationGateway,
    RunRegistryGateway,
    RunUnitOfWork,
)
from gameforge.platform.runs.execution_plan import (
    ExecutionVersionPlanAuthorityValidator,
    LegacyExecutionVersionPlanAuthorityValidator,
)
from gameforge.platform.runs.replay import (
    MAX_REPLAY_ARTIFACT_BYTES,
    ReplayAdmissionPreparation,
    ReplayAdmissionReader,
    ReplayAdmissionValidator,
)
from gameforge.runtime.cassette.legacy_import import (
    LegacyImportAuthority,
    LegacyImportDecisionRepository,
)
from gameforge.runtime.observability.context import TraceCarrier, current_trace_context
from gameforge.spine.sim.economy import EconomyModel


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
    _cost_amount("cache_read_token", 100_000_000, unit="token"),
    _cost_amount("cache_write_token", 100_000_000, unit="token"),
    _cost_amount("request", 1_000_000, unit="request"),
    _cost_amount("agent_step", 1_000_000, unit="step"),
    _cost_amount("wall_time_ns", 3_600_000_000_000, unit="ns"),
    _cost_amount("concurrent_run", 16, unit="count"),
)
_SYSTEM_BUDGET_SCOPE_ID = "global"
# ``run-budget-selection@1`` is deliberately bounded.  Fetching one sentinel row
# beyond the retained maximum proves completeness without silently truncating an
# applicable rejecting budget.
_MAX_APPLICABLE_BUDGETS_PER_SCOPE = 64


class RunBudgetLedger(Protocol):
    """Narrow ledger surface the default budget-plan provider needs."""

    def get_budget(self, budget_id: str) -> BudgetV1 | None: ...

    def put_budget(self, budget: BudgetV1) -> BudgetV1: ...

    def list_budgets_by_scope_identity(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        limit: int,
    ) -> tuple[BudgetV1, ...]: ...


class DefaultRunBudgetPlanProvider:
    """Mint a per-run budget-set snapshot + hold group for the admitted Run.

    A run-scoped :class:`BudgetV1` is created once (idempotent per run_id), then all
    retained budgets for the exact run, principal, and global-system scope identities
    are selected. Missing shared authority and bounded-enumeration overflow fail
    closed; provisioning those administrative budgets is not an admission side
    effect. Every selected head is snapshotted into the
    ``budget_set_snapshot_id`` stamped on the payload, so
    ``SqlRunCostAccounting.reserve_run_budget`` freezes and reserves all applicable
    scopes inside the same UnitOfWork.
    """

    def __init__(
        self,
        *,
        ledger: RunBudgetLedger,
        clock: UtcClock,
        selection_policy_version: str = "run-budget-selection@1",
        budget_policy_version: str = "run-budget-policy@1",
        limits: tuple[CostAmountV1, ...] = _DEFAULT_RUN_LIMITS,
        reservation: tuple[CostAmountV1, ...] | None = None,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._selection_policy_version = selection_policy_version
        self._budget_policy_version = budget_policy_version
        self._limits = limits
        self._reservation = (
            tuple(item for item in limits if item.dimension != "concurrent_run")
            if reservation is None
            else reservation
        )
        limit_by_dimension = {item.dimension: item for item in limits}
        reservation_by_dimension = {item.dimension: item for item in self._reservation}
        hold_dimensions = {item.dimension for item in limits if item.dimension != "concurrent_run"}
        if (
            not self._reservation
            or len(limit_by_dimension) != len(limits)
            or len(reservation_by_dimension) != len(self._reservation)
            or set(reservation_by_dimension) != hold_dimensions
            or any(
                item.unit != limit_by_dimension[item.dimension].unit
                or item.currency != limit_by_dimension[item.dimension].currency
                for item in self._reservation
            )
        ):
            raise IntegrityViolation(
                "run budget reservation must cover every non-permit limit dimension"
            )

    def resolve_run_budget(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        request_hash: str,
        initiated_by: AuditActor,
    ) -> RunBudgetPlan:
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
        elif (
            budget.scope_kind != "run"
            or budget.scope_id != run_id
            or budget.policy_version != self._budget_policy_version
            or budget.limits != self._limits
        ):
            raise IntegrityViolation(
                "retained run budget differs from the versioned admission policy",
                budget_id=budget_id,
            )
        selected: list[BudgetV1] = []
        for scope_kind, scope_id in (
            ("run", run_id),
            ("principal", initiated_by.principal_id),
            ("system", _SYSTEM_BUDGET_SCOPE_ID),
        ):
            matches = self._ledger.list_budgets_by_scope_identity(
                scope_kind=scope_kind,
                scope_id=scope_id,
                limit=_MAX_APPLICABLE_BUDGETS_PER_SCOPE + 1,
            )
            if len(matches) > _MAX_APPLICABLE_BUDGETS_PER_SCOPE:
                raise IntegrityViolation(
                    "applicable budget selection exceeds its bounded policy",
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    max_applicable_budgets=_MAX_APPLICABLE_BUDGETS_PER_SCOPE,
                )
            if not matches:
                if scope_kind == "run":
                    raise DependencyUnavailable(
                        "required run budget scope is not provisioned",
                        scope_kind=scope_kind,
                        scope_id=scope_id,
                        selection_policy_version=self._selection_policy_version,
                    )
                continue
            if scope_kind == "run" and budget not in matches:
                raise IntegrityViolation(
                    "versioned admission run budget is absent from applicable selection",
                    budget_id=budget.budget_id,
                )
            selected.extend(matches)
        snapshots = tuple(
            BudgetSnapshotV1(
                snapshot_id=f"snapshot:{budget_set_snapshot_id}:{item.budget_id}",
                budget_id=item.budget_id,
                scope_kind=item.scope_kind,
                scope_id=item.scope_id,
                policy_version=item.policy_version,
                budget_revision_at_freeze=item.revision,
                limits=item.limits,
                reserved=item.reserved,
                consumed=item.consumed,
                captured_at=now,
            )
            for item in selected
        )
        budget_set = BudgetSetSnapshotV1(
            budget_set_snapshot_id=budget_set_snapshot_id,
            run_id=run_id,
            selection_policy_version=self._selection_policy_version,
            snapshots=snapshots,
            captured_at=now,
        )
        group_id = f"hold:{run_id}"
        reservations: list[BudgetReservationV1] = []
        forecast = {item.dimension: item for item in self._reservation}
        for snapshot in budget_set.snapshots:
            governed = tuple(item for item in snapshot.limits if item.dimension != "concurrent_run")
            if not governed:
                continue
            missing = tuple(item.dimension for item in governed if item.dimension not in forecast)
            if missing:
                raise IntegrityViolation(
                    "applicable budget governs dimensions absent from the run forecast",
                    budget_id=snapshot.budget_id,
                    dimensions=missing,
                )
            projected: list[CostAmountV1] = []
            for limit in governed:
                amount = forecast[limit.dimension]
                if amount.unit != limit.unit or amount.currency != limit.currency:
                    raise IntegrityViolation(
                        "run forecast cost identity differs from an applicable budget",
                        budget_id=snapshot.budget_id,
                        dimension=limit.dimension,
                    )
                projected.append(amount)
            reservations.append(
                BudgetReservationV1(
                    reservation_id=f"reservation:{group_id}:{snapshot.budget_id}",
                    reservation_group_id=group_id,
                    budget_id=snapshot.budget_id,
                    reserved=tuple(projected),
                    status="reserved",
                    revision=1,
                )
            )
        reservation_tuple = tuple(reservations)
        hold = ReservationGroupV1(
            reservation_group_id=group_id,
            scope="run_budget_hold",
            run_id=run_id,
            budget_set_snapshot_id=budget_set_snapshot_id,
            request_hash=f"sha256:{request_hash}",
            idempotency_key=f"hold-idempotency:{run_id}",
            budget_reservation_ids=tuple(item.reservation_id for item in reservation_tuple),
            status="reserved",
            revision=1,
            created_at=now,
        )
        return RunBudgetPlan(
            budget_set=budget_set,
            hold_group=hold,
            reservations=reservation_tuple,
        )


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

    def record_run_created(
        self,
        *,
        run: Any,
        event: Any,
        request_id: str | None = None,
    ) -> None:
        trace_context = (
            TraceCarrier.extract(run.dispatch_trace_carrier)
            if run.dispatch_trace_carrier is not None
            else None
        )
        self._audit.append(
            chain_id=self._chain_id,
            actor=run.initiated_by,
            initiated_by=None,
            action="run.queued",
            subject=AuditSubject(resource_kind="run", resource_id=run.run_id),
            correlation=AuditCorrelation(
                request_id=request_id,
                run_id=run.run_id,
                trace_id=None if trace_context is None else trace_context.trace_id,
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
    object_bindings: Any | None = None
    findings: Any | None = None
    finding_links: Any | None = None
    runs: Any | None = None
    routing: Any | None = None


AdmissionReadScope = Callable[[], AbstractContextManager[AdmissionReadPort]]
CurrentPrincipalResolver = Callable[[Any, ActorContext], Principal | None]


class _AdmissionReplayReader(ReplayAdmissionReader):
    """Adapt one admission read transaction to the replay proof surface."""

    def __init__(self, *, read: AdmissionReadPort, objects: ObjectStore) -> None:
        self._read = read
        self._objects = objects
        self._artifacts: dict[str, ArtifactV2 | None] = {}
        self._bindings: dict[str, Any] = {}
        self._runs: dict[str, RunRecord | None] = {}
        self._attempts: dict[tuple[str, int], Any] = {}
        self._prompt_links: dict[tuple[str, int, int, int], Any] = {}
        self._routing_decisions: dict[str, Any] = {}
        self._model_route_links: dict[tuple[str, int, int, int], Any] = {}
        self._model_consumptions: dict[tuple[str, int, int, int], Any] = {}

    @staticmethod
    def _record_exact(
        retained: dict[Any, Any],
        key: Any,
        value: Any,
        *,
        label: str,
    ) -> Any:
        if key in retained and retained[key] != value:
            raise Conflict(f"{label} changed during replay admission proof")
        retained[key] = value
        return value

    def get_artifact(self, artifact_id: str) -> ArtifactV2 | None:
        artifact = self._read.artifacts.get(artifact_id)
        parsed = artifact if isinstance(artifact, ArtifactV2) else None
        return self._record_exact(
            self._artifacts,
            artifact_id,
            parsed,
            label="replay dependency Artifact",
        )

    def read_artifact_bytes(self, artifact_id: str) -> bytes:
        artifact = self.get_artifact(artifact_id)
        if artifact is None or self._read.object_bindings is None:
            raise FileNotFoundError(artifact_id)
        binding = self._read.object_bindings.resolve(artifact.object_ref)
        self._record_exact(
            self._bindings,
            artifact_id,
            binding,
            label="replay dependency ObjectBinding",
        )
        with self._objects.open(binding.location) as handle:
            payload = handle.read(MAX_REPLAY_ARTIFACT_BYTES + 1)
        if len(payload) > MAX_REPLAY_ARTIFACT_BYTES:
            raise IntegrityViolation("replay Artifact exceeds the admission byte limit")
        return payload

    def get_run(self, run_id: str) -> RunRecord | None:
        if self._read.runs is None:
            parsed = None
        else:
            run = self._read.runs.get(run_id)
            parsed = run if isinstance(run, RunRecord) else None
        return self._record_exact(
            self._runs,
            run_id,
            parsed,
            label="replay dependency Run",
        )

    def get_attempt(self, run_id: str, attempt_no: int) -> Any:
        value = None if self._read.runs is None else self._read.runs.get_attempt(run_id, attempt_no)
        return self._record_exact(
            self._attempts,
            (run_id, attempt_no),
            value,
            label="replay dependency RunAttempt",
        )

    def get_prompt_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> Any:
        value = (
            None
            if self._read.runs is None
            else self._read.runs.get_intermediate_link(
                run_id,
                attempt_no,
                call_ordinal,
                route_ordinal,
            )
        )
        return self._record_exact(
            self._prompt_links,
            (run_id, attempt_no, call_ordinal, route_ordinal),
            value,
            label="replay dependency prompt link",
        )

    def get_routing_decision(self, decision_id: str) -> Any:
        value = (
            None
            if self._read.routing is None
            else self._read.routing.get_routing_decision(decision_id)
        )
        return self._record_exact(
            self._routing_decisions,
            decision_id,
            value,
            label="replay dependency routing decision",
        )

    def get_model_route_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> Any:
        value = (
            None
            if self._read.runs is None
            else self._read.runs.get_model_route_link(
                run_id,
                attempt_no,
                call_ordinal,
                route_ordinal,
            )
        )
        return self._record_exact(
            self._model_route_links,
            (run_id, attempt_no, call_ordinal, route_ordinal),
            value,
            label="replay dependency model route link",
        )

    def get_model_response_consumption(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> Any:
        value = (
            None
            if self._read.runs is None
            else self._read.runs.get_model_response_consumption(
                run_id,
                attempt_no,
                call_ordinal,
                route_ordinal,
            )
        )
        return self._record_exact(
            self._model_consumptions,
            (run_id, attempt_no, call_ordinal, route_ordinal),
            value,
            label="replay dependency model consumption",
        )

    def revalidate(
        self,
        transaction: Any,
        *,
        already_checked_artifact_ids: set[str],
    ) -> None:
        """Set-compare recorded rows without reopening blobs or issuing N+1 reads."""

        artifacts = getattr(transaction, "artifacts", None)
        bindings = getattr(transaction, "object_bindings", None)
        runs = getattr(transaction, "runs", None)
        routing = getattr(transaction, "cost", None)
        if artifacts is None or bindings is None or runs is None or routing is None:
            raise IntegrityViolation("replay admission UoW lacks retained authorities")
        get_artifacts = getattr(artifacts, "get_many", None)
        resolve_bindings = getattr(bindings, "resolve_many", None)
        project_runs = getattr(runs, "replay_authority_projection", None)
        get_routing = getattr(routing, "get_routing_decisions_many", None)
        if not all(
            callable(item) for item in (get_artifacts, resolve_bindings, project_runs, get_routing)
        ):
            raise IntegrityViolation("replay admission UoW lacks batch authority reads")

        remaining_artifacts = {
            artifact_id: artifact
            for artifact_id, artifact in self._artifacts.items()
            if artifact_id not in already_checked_artifact_ids
        }
        remaining_bindings = {
            artifact_id: binding
            for artifact_id, binding in self._bindings.items()
            if artifact_id not in already_checked_artifact_ids
        }
        current_artifacts = get_artifacts(tuple(remaining_artifacts))
        for artifact_id, expected in remaining_artifacts.items():
            if current_artifacts.get(artifact_id) != expected:
                raise Conflict(
                    "replay dependency Artifact changed before atomic admission",
                    artifact_id=artifact_id,
                )
        binding_artifacts: dict[str, ArtifactV2] = {}
        for artifact_id in remaining_bindings:
            artifact = self._artifacts.get(artifact_id)
            if not isinstance(artifact, ArtifactV2):
                raise IntegrityViolation("replay binding snapshot lacks its exact Artifact")
            binding_artifacts[artifact_id] = artifact
        current_bindings = resolve_bindings(
            tuple(artifact.object_ref for artifact in binding_artifacts.values())
        )
        for artifact_id, artifact in binding_artifacts.items():
            if current_bindings.get(artifact.object_ref.key) != remaining_bindings[artifact_id]:
                raise Conflict(
                    "replay dependency ObjectBinding changed before atomic admission",
                    artifact_id=artifact_id,
                )

        projection = project_runs(
            run_ids=tuple(self._runs),
            attempt_keys=tuple(self._attempts),
            prompt_link_keys=tuple(self._prompt_links),
            model_route_keys=tuple(self._model_route_links),
            model_consumption_keys=tuple(self._model_consumptions),
        )
        for run_id, expected in self._runs.items():
            if projection.runs.get(run_id) != expected:
                raise Conflict(
                    "replay dependency Run changed before atomic admission",
                    run_id=run_id,
                )
        for (run_id, attempt_no), expected in self._attempts.items():
            if projection.attempts.get((run_id, attempt_no)) != expected:
                raise Conflict(
                    "replay dependency RunAttempt changed before atomic admission",
                    run_id=run_id,
                    attempt_no=attempt_no,
                )
        for key, expected in self._prompt_links.items():
            if projection.prompt_links.get(key) != expected:
                raise Conflict("replay dependency prompt link changed before atomic admission")
        current_routing = get_routing(tuple(self._routing_decisions))
        for decision_id, expected in self._routing_decisions.items():
            if current_routing.get(decision_id) != expected:
                raise Conflict(
                    "replay dependency routing decision changed before atomic admission",
                    decision_id=decision_id,
                )
        for key, expected in self._model_route_links.items():
            if projection.model_route_links.get(key) != expected:
                raise Conflict("replay dependency model route link changed before atomic admission")
        for key, expected in self._model_consumptions.items():
            if projection.model_consumptions.get(key) != expected:
                raise Conflict(
                    "replay dependency model consumption changed before atomic admission"
                )


class _RecordingLegacyImportAuthority:
    """Record legacy authority reads so the create UoW can recheck exact values."""

    def __init__(self, delegate: LegacyImportAuthority) -> None:
        self._delegate = delegate
        self._calls: dict[tuple[str, tuple[Any, ...]], Any] = {}

    def _call(self, method: str, *args: Any) -> Any:
        value = getattr(self._delegate, method)(*args)
        _AdmissionReplayReader._record_exact(
            self._calls,
            (method, args),
            value,
            label="legacy replay authority",
        )
        return value

    @property
    def verification_policy_registry(self) -> Any:
        value = self._delegate.verification_policy_registry
        _AdmissionReplayReader._record_exact(
            self._calls,
            ("verification_policy_registry", ()),
            value,
            label="legacy replay authority",
        )
        return value

    def resolve_model_catalog(self, *args: Any) -> Any:
        return self._call("resolve_model_catalog", *args)

    def resolve_input_binding(self, *args: Any) -> Any:
        return self._call("resolve_input_binding", *args)

    def resolve_profile_binding(self, *args: Any) -> Any:
        return self._call("resolve_profile_binding", *args)

    def resolve_policy_binding(self, *args: Any) -> Any:
        return self._call("resolve_policy_binding", *args)

    def resolve_schema_binding(self, *args: Any) -> Any:
        return self._call("resolve_schema_binding", *args)

    def resolve_rendered_request(self, *args: Any) -> Any:
        return self._call("resolve_rendered_request", *args)

    def resolve_frozen_version_tuple(self, *args: Any) -> Any:
        return self._call("resolve_frozen_version_tuple", *args)

    def resolve_call_tool_version(self, *args: Any) -> Any:
        return self._call("resolve_call_tool_version", *args)

    def revalidate(self) -> None:
        for (method, args), expected in self._calls.items():
            current = (
                self._delegate.verification_policy_registry
                if method == "verification_policy_registry"
                else getattr(self._delegate, method)(*args)
            )
            if current != expected:
                raise Conflict("legacy replay authority changed before atomic admission")


class _RecordingLegacyDecisionRepository:
    """Read-only recorder for retained legacy routing decisions."""

    def __init__(self, delegate: LegacyImportDecisionRepository) -> None:
        self._delegate = delegate
        self._decisions: dict[str, Any] = {}

    def put_legacy_import_routing_decision(self, decision: Any) -> Any:
        del decision
        raise IntegrityViolation("replay admission cannot mutate legacy routing decisions")

    def get_legacy_import_routing_decision(self, decision_id: str) -> Any:
        value = self._delegate.get_legacy_import_routing_decision(decision_id)
        return _AdmissionReplayReader._record_exact(
            self._decisions,
            decision_id,
            value,
            label="legacy replay routing decision",
        )

    def revalidate(
        self,
        repository: LegacyImportDecisionRepository,
        *,
        require_batch: bool,
    ) -> None:
        get_many = getattr(repository, "get_legacy_import_routing_decisions_many", None)
        if callable(get_many):
            current = get_many(tuple(self._decisions))
        elif require_batch:
            raise IntegrityViolation("replay admission UoW lacks batch legacy decision reads")
        else:
            current = {
                decision_id: repository.get_legacy_import_routing_decision(decision_id)
                for decision_id in self._decisions
            }
        for decision_id, expected in self._decisions.items():
            if current.get(decision_id) != expected:
                raise Conflict(
                    "legacy replay routing decision changed before atomic admission",
                    decision_id=decision_id,
                )


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
    request_id: str | None = None
    trace_id: str | None = None
    dispatch_trace_carrier: RunDispatchTraceCarrierV1 | None = None


@dataclass(frozen=True, slots=True)
class _AuthorizationBinding:
    """Exact authorization facts rechecked inside the Run-create transaction."""

    role_policy: RolePolicy
    domain_registry: DomainRegistryV1
    resource_domain: DomainScopeValue
    permissions: tuple[Permission, ...]


@dataclass(frozen=True, slots=True)
class _ReplayAdmissionContext:
    """One immutable cassette parse reused throughout a single admission."""

    reader: _AdmissionReplayReader
    validator: ReplayAdmissionValidator
    preparation: ReplayAdmissionPreparation
    legacy_authority: _RecordingLegacyImportAuthority | None
    legacy_decisions: _RecordingLegacyDecisionRepository | None

    def revalidate(
        self,
        transaction: Any,
        *,
        explicit_legacy_decisions: LegacyImportDecisionRepository | None,
        already_checked_artifact_ids: set[str],
    ) -> None:
        self.reader.revalidate(
            transaction,
            already_checked_artifact_ids=already_checked_artifact_ids,
        )
        if self.legacy_decisions is not None and explicit_legacy_decisions is None:
            repository = getattr(transaction, "cost", None)
            if repository is None:
                raise IntegrityViolation("replay admission UoW lacks legacy decision authority")
            self.legacy_decisions.revalidate(repository, require_batch=True)


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

_MODEL_CAPABILITY_PROFILE_KINDS: frozenset[ExecutionProfileKindV1] = frozenset(
    {
        "generation",
        "patch_repair",
        "constraint_extraction",
        "llm_triage",
        "playtest_planner",
        "bench_evaluator",
    }
)


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
        values = (
            *(f"checker:{p.profile_id}@{p.version}" for p in params.checker_profiles),
            *(f"simulation:{p.profile_id}@{p.version}" for p in params.simulation_profiles),
            *(f"regression:{suite_id}" for suite_id in params.regression_suite_artifact_ids),
            *(
                f"expected-finding:{binding.finding_id}@{binding.finding_revision}"
                for binding in params.expected_findings
            ),
            *(
                f"finding:{binding.finding_id}@{binding.finding_revision}"
                for binding in params.findings
            ),
            *(f"review:{artifact_id}" for artifact_id in params.review_artifact_ids),
            *(f"playtest:{artifact_id}" for artifact_id in params.playtest_trace_artifact_ids),
        )
        return values or ("validation:required-dimension",)
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
_BENCH_CASE_RESULT_KINDS: tuple[ArtifactKind, ...] = (
    "checker_run",
    "simulation_run",
    "playtest_trace",
    "review_report",
    "run_result",
    "validation_evidence",
    "regression_evidence",
)
_MAX_ADMISSION_BLOB_BYTES = (
    MAX_CONFIG_EXPORT_PACKAGE_BYTES + MAX_CONFIG_EXPORT_MANIFEST_BYTES + 16 * 1024 * 1024
)
_MAX_FINDING_EVIDENCE_ANCESTRY_NODES = 4096
_MAX_FINDING_EVIDENCE_ANCESTRY_DEPTH = 64
_MAX_LEGACY_DOMAIN_LINEAGE_NODES = 1_000
_MAX_LEGACY_DOMAIN_LINEAGE_EDGES = 10_000


@dataclass(slots=True)
class _LegacyDomainTraversalContext:
    """One admission-wide budget for legacy domain-lineage fallback."""

    visited_nodes: int = 0
    visited_edges: int = 0

    def charge_node(self, *, root_artifact_id: str) -> None:
        self.visited_nodes += 1
        if self.visited_nodes > _MAX_LEGACY_DOMAIN_LINEAGE_NODES:
            raise IntegrityViolation(
                "Artifact legacy domain lineage exceeds the node limit",
                artifact_id=root_artifact_id,
                max_nodes=_MAX_LEGACY_DOMAIN_LINEAGE_NODES,
            )

    def charge_edges(self, count: int, *, root_artifact_id: str) -> None:
        self.visited_edges += count
        if self.visited_edges > _MAX_LEGACY_DOMAIN_LINEAGE_EDGES:
            raise IntegrityViolation(
                "Artifact legacy domain lineage exceeds the edge limit",
                artifact_id=root_artifact_id,
                max_edges=_MAX_LEGACY_DOMAIN_LINEAGE_EDGES,
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


class DrRecoveryManifestAuthority(Protocol):
    """Resolve one recovery-catalog manifest already verified and imported.

    M4c owns only this narrow trusted seam. Signature/catalog receipt verification
    and importing the manifest are M4e responsibilities; admission still proves the
    returned immutable Artifact and active ObjectBinding against its own retained
    authorities before it can become a Run input.
    """

    def resolve_verified_manifest(
        self,
        *,
        recovery_catalog_entry_id: str,
        expected_checkpoint_id: str,
    ) -> ArtifactV2: ...


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
        current_principal_resolver: CurrentPrincipalResolver,
        role_policy_version: str,
        role_policy_digest: str,
        playtest_payload_validator: PlaytestPayloadValidationService | None = None,
        legacy_import_authority: LegacyImportAuthority | None = None,
        legacy_import_decisions: LegacyImportDecisionRepository | None = None,
        deadline_policy: AdmissionDeadlinePolicy | None = None,
        validation_start_writer: ValidationStartWriter | None = None,
        dr_recovery_manifest_authority: DrRecoveryManifestAuthority | None = None,
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
        self._current_principal = current_principal_resolver
        self._legacy_import_authority = legacy_import_authority
        self._legacy_import_decisions = legacy_import_decisions
        self._validation_start_writer = validation_start_writer
        self._dr_recovery_manifest_authority = dr_recovery_manifest_authority
        self._playtest_payload_validator = playtest_payload_validator
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
        if self._validation_start_writer is None:
            raise DependencyUnavailable(
                "validation admission requires an atomic workflow start writer",
                component="validation_start_writer",
            )
        expected_subject_kind = {
            "patch.validate": "patch",
            "constraint.validate": "constraint_proposal",
            "rollback.validate": "rollback_request",
        }.get(operation)
        if expected_subject_kind is None:
            raise IntegrityViolation("unknown validation admission operation")
        run_id = self._derive_run_id(
            scope=f"approval:{request.approval_id}",
            key=server.idempotency_key,
            request_hash=server.request_hash,
        )
        subject = ValidationSubjectBindingV1(
            approval_id=request.approval_id,
            expected_workflow_revision=request.expected_workflow_revision + 1,
            subject_head_revision=request.expected_subject_head_revision,
            subject_artifact_id=resource_id,
            subject_digest=request.subject_digest,
            active_validation_run_id=run_id,
        )
        kind, params = self._validation_params(
            operation=operation,
            request=request,
            subject=subject,
        )
        with self._read_scope() as read:
            replay = self._replay_existing(
                read=read,
                run_id=run_id,
                kind=kind,
                creation_mode="resource_endpoint_only",
                params=params,
                actor=actor,
                idempotency_scope=f"approval:{request.approval_id}",
                idempotency_key=server.idempotency_key,
                request_hash=server.request_hash,
                llm_execution_mode="not_applicable",
                seed=request.seed,
                execution_version_plan=None,
                cassette_artifact_id=None,
            )
            if replay is not None:
                return replay
            item = self._load_validation_subject(
                read=read,
                approval_id=request.approval_id,
                expected_workflow_revision=request.expected_workflow_revision,
                expected_subject_head_revision=request.expected_subject_head_revision,
                subject_digest=request.subject_digest,
            )
            if (
                item.subject_kind != expected_subject_kind
                or item.subject_artifact_id != resource_id
            ):
                raise Conflict(
                    "validation endpoint does not bind the exact workflow subject",
                    expected_subject_kind=expected_subject_kind,
                    actual_subject_kind=item.subject_kind,
                    expected_subject_artifact_id=resource_id,
                    actual_subject_artifact_id=item.subject_artifact_id,
                )
            schema_version = {
                "patch.validate": "patch-validation@1",
                "constraint.validate": "constraint-validation@1",
                "rollback.validate": "rollback-validation@1",
            }[operation]
            version_tuple = self._subject_version_tuple(
                item,
                schema_version,
                root_seed=request.seed,
            )

            return self._admit_run(
                run_id=run_id,
                kind=kind,
                creation_mode="resource_endpoint_only",
                params=params,
                version_tuple=version_tuple,
                llm_execution_mode="not_applicable",
                seed=request.seed,
                read=read,
                actor=actor,
                idempotency_scope=f"approval:{request.approval_id}",
                idempotency_key=server.idempotency_key,
                request_hash=server.request_hash,
                request_id=getattr(server, "request_id", None),
                trace_id=getattr(server, "trace_id", None),
                dispatch_trace_carrier=getattr(server, "dispatch_trace_carrier", None),
                validation_item=item,
                companion_write=self._validation_companion(
                    item=item, run_id=run_id, actor=actor, server=server
                ),
            )

    @staticmethod
    def _validation_params(
        *,
        operation: str,
        request: PatchValidationAdmissionRequestV1
        | ConstraintValidationAdmissionRequestV1
        | RollbackValidationAdmissionRequestV1,
        subject: ValidationSubjectBindingV1,
    ) -> tuple[RunKindRef, RunKindPayload]:
        if operation == "patch.validate":
            if not isinstance(request, PatchValidationAdmissionRequestV1):
                raise IntegrityViolation("patch validation received the wrong request type")
            return RunKindRef(kind="patch.validate", version=1), PatchValidationPayloadV1(
                subject=subject,
                base_snapshot_artifact_id=request.base_snapshot_artifact_id,
                preview_snapshot_artifact_id=request.preview_snapshot_artifact_id,
                constraint_snapshot_artifact_id=request.constraint_snapshot_artifact_id,
                candidate_config_export_artifact_ids=(request.candidate_config_export_artifact_ids),
                target=request.target,
                validation_policy=request.validation_policy,
                checker_profiles=request.checker_profiles,
                simulation_profiles=request.simulation_profiles,
                expected_findings=request.expected_findings,
                findings=request.findings,
                review_artifact_ids=request.review_artifact_ids,
                playtest_trace_artifact_ids=request.playtest_trace_artifact_ids,
                regression_suite_artifact_ids=request.regression_suite_artifact_ids,
            )
        if operation == "constraint.validate":
            if not isinstance(request, ConstraintValidationAdmissionRequestV1):
                raise IntegrityViolation("constraint validation received the wrong request type")
            return RunKindRef(
                kind="constraint_proposal.validate", version=1
            ), ConstraintValidationPayloadV1(
                subject=subject,
                base_constraint_snapshot_artifact_id=(request.base_constraint_snapshot_artifact_id),
                target=request.target,
                dsl_grammar_version=request.dsl_grammar_version,
                compiler_profile=request.compiler_profile,
                differential_engines=request.differential_engines,
                golden_suite_artifact_id=request.golden_suite_artifact_id,
                regression_suite_artifact_ids=request.regression_suite_artifact_ids,
                validation_policy=request.validation_policy,
            )
        if operation == "rollback.validate":
            if not isinstance(request, RollbackValidationAdmissionRequestV1):
                raise IntegrityViolation("rollback validation received the wrong request type")
            return RunKindRef(kind="rollback.validate", version=1), RollbackValidationPayloadV1(
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
        raise IntegrityViolation("unknown validation admission operation")

    def _validation_companion(
        self,
        *,
        item: ApprovalItem,
        run_id: str,
        actor: ActorContext,
        server: Any,
    ) -> Callable[[Any], None]:
        """The ``draft->validating`` CAS to run in the Run-create UoW."""

        writer = self._validation_start_writer
        if writer is None:
            raise IntegrityViolation("validation start writer disappeared during admission")

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

        if not isinstance(
            params,
            (PatchRepairPayloadV1, TaskSuiteDerivePayloadV1, PlaytestRunPayloadV1),
        ):
            raise IntegrityViolation(
                "resource Run must use its dedicated typed platform admission surface"
            )
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

        if actor.principal.kind not in {"service", "system"}:
            raise IntegrityViolation(
                "internal-only Run admission requires a trusted service or system actor"
            )
        if not isinstance(params, (ArtifactMigrationPayloadV1, DrDrillPayloadV1)):
            raise IntegrityViolation("internal admission accepts only internal-only Run kinds")
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

    # ── constraint-proposals:propose (mints authenticated source_raw goal) ────
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
        identities = FROZEN_RUN_KIND_IDENTITIES_BY_PAYLOAD_SCHEMA.get(params.schema_version)
        if identities is None:
            raise IntegrityViolation("Run params schema is not a retained Run kind")
        if len(identities) != 1:
            raise IntegrityViolation("Run params schema does not select one exact Run kind")
        kind, version = identities[0]
        return RunKindRef(kind=kind, version=version)

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
        # The naked goal bytes are written blob-first by ``_mint_goal_source``.  The
        # immutable Artifact/ObjectBinding publication is deliberately deferred to
        # the fresh Run-create UnitOfWork: an authorization, CAS, registry, or budget
        # failure may leave an unreferenced blob for GC, but it must never publish a
        # durable source Artifact outside the atomic admission boundary.
        with self._read_scope() as read:
            replay = self._replay_existing(
                read=read,
                run_id=run_id,
                kind=kind,
                creation_mode=creation_mode,
                params=params,
                actor=actor,
                idempotency_scope=idempotency_scope,
                idempotency_key=server.idempotency_key,
                request_hash=server.request_hash,
                llm_execution_mode=llm_execution_mode,
                seed=seed,
                execution_version_plan=execution_version_plan,
                cassette_artifact_id=cassette_artifact_id,
            )
            if replay is not None:
                return replay
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
                request_id=server.request_id,
                trace_id=server.trace_id,
                dispatch_trace_carrier=server.dispatch_trace_carrier,
                execution_version_plan=execution_version_plan,
                cassette_artifact_id=cassette_artifact_id,
                source_writes=source_writes,
            )

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

    def _replay_existing(
        self,
        *,
        read: AdmissionReadPort,
        run_id: str,
        kind: RunKindRef,
        creation_mode: Literal["generic_runs_endpoint", "resource_endpoint_only", "internal_only"],
        params: RunKindPayload,
        actor: ActorContext,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        seed: int | None,
        execution_version_plan: ExecutionVersionPlanV1 | None,
        cassette_artifact_id: str | None,
    ) -> RunAcceptedV1 | None:
        """Replay a retained admission before consulting mutable request-time state.

        A successful first admission already atomically froze every authority in its
        ``RunRecord``.  Exact retries therefore re-drive the command service with the
        retained server-owned deadlines/payload rather than re-reading a ref,
        ApprovalItem, current profile lifecycle, or role assignment that may have
        legitimately changed after creation.  A reused key with different semantics
        still conflicts before any new authority is written.
        """

        runs = read.runs
        if runs is None:
            return None
        retained = runs.get_by_idempotency(
            scope=idempotency_scope,
            key=idempotency_key,
        )
        if retained is None:
            return None
        if not isinstance(retained, RunRecord):
            raise IntegrityViolation("Run idempotency authority returned an invalid record")
        if retained.request_hash != request_hash:
            raise IdempotencyConflict(
                "Run idempotency key is bound to a different request",
                expected_request_hash=request_hash,
                actual_request_hash=retained.request_hash,
            )
        expected_actor = AuditActor(
            principal_id=actor.principal.id,
            principal_kind=actor.principal.kind,
        )
        exact = (
            retained.run_id == run_id
            and retained.kind == kind
            and retained.idempotency_scope == idempotency_scope
            and retained.idempotency_key == idempotency_key
            and retained.initiated_by == expected_actor
            and retained.payload.params == params
            and retained.payload.llm_execution_mode == llm_execution_mode
            and retained.payload.seed == seed
            and retained.payload.execution_version_plan == execution_version_plan
            and retained.payload.cassette_artifact_id == cassette_artifact_id
        )
        if not exact:
            raise IdempotencyConflict(
                "Run idempotency request differs despite a matching request hash"
            )
        result = self._run_commands.create_run(
            RunCreateRequest(
                run_id=retained.run_id,
                kind=retained.kind,
                creation_mode=creation_mode,
                idempotency_scope=retained.idempotency_scope,
                idempotency_key=retained.idempotency_key,
                request_hash=retained.request_hash,
                request_id=actor.request_id,
                payload=retained.payload,
                resource_domain_scope=retained.resource_domain_scope,
                dispatch_trace_carrier=retained.dispatch_trace_carrier,
                initiated_by=retained.initiated_by,
                queue_deadline_utc=retained.queue_deadline_utc,
                attempt_timeout_ns=retained.attempt_timeout_ns,
                overall_deadline_utc=retained.overall_deadline_utc,
            )
        )
        if not result.replayed or result.run != retained:
            raise IntegrityViolation("Run command replay did not retain the exact authority")
        return RunAcceptedV1(
            run_id=retained.run_id,
            status_url=f"/api/v1/runs/{retained.run_id}",
            events_url=f"/api/v1/runs/{retained.run_id}/events",
        )

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
        request_id: str | None,
        trace_id: str | None,
        dispatch_trace_carrier: RunDispatchTraceCarrierV1 | None = None,
        execution_version_plan: ExecutionVersionPlanV1 | None = None,
        cassette_artifact_id: str | None = None,
        source_writes: tuple[_SourceWrite, ...] = (),
        validation_item: ApprovalItem | None = None,
        companion_write: Callable[[Any], None] | None = None,
    ) -> RunAcceptedV1:
        definition = self._resolve_definition(kind, creation_mode)
        catalog, replay_context = self._execution_profile_catalog_for_request(
            kind=kind,
            llm_execution_mode=llm_execution_mode,
            cassette_artifact_id=cassette_artifact_id,
            read=read,
        )
        # Resolve immutable inputs before resource-derived authorization so the
        # permission resolver never trusts a client-declared subject kind/id.
        artifacts = self._verify_input_artifacts(
            params=params,
            cassette_artifact_id=cassette_artifact_id,
            read=read,
            pending_sources=source_writes,
        )
        self._verify_prompt_goal_binding(
            params=params,
            artifacts=artifacts,
            read=read,
            actor=actor,
            pending_sources=source_writes,
        )
        self._verify_finding_bindings(params=params, artifacts=artifacts, read=read)
        self._verify_benchmark_admission(
            params=params,
            artifacts=artifacts,
            read=read,
            run_definition=definition,
            llm_execution_mode=llm_execution_mode,
            execution_version_plan=execution_version_plan,
            cassette_artifact_id=cassette_artifact_id,
            seed=seed,
            catalog=catalog,
        )
        self._verify_execution_request_shape(
            definition=definition,
            params=params,
            llm_execution_mode=llm_execution_mode,
            execution_version_plan=execution_version_plan,
            cassette_artifact_id=cassette_artifact_id,
        )
        permission_item = validation_item
        if isinstance(params, PatchRepairPayloadV1):
            permission_item = self._load_repair_subject(
                read=read,
                params=params,
                artifacts=artifacts,
            )
        resolved_profiles = self._resolve_profiles(
            params=params,
            kind=kind,
            read=read,
            llm_execution_mode=llm_execution_mode,
            catalog=catalog,
        )
        self._verify_seed_policy(
            definition=definition,
            params=params,
            resolved_profiles=resolved_profiles,
            seed=seed,
            catalog=catalog,
        )
        if execution_version_plan is not None:
            if read.routing is None:
                raise DependencyUnavailable(
                    "execution-plan retained authority is unavailable",
                    component="execution_plan_authority",
                )
            execution_graph = self._registry.get_agent_execution_graph(
                kind,
                execution_version_plan.agent_graph_version,
            )
            if execution_graph is None:
                raise IntegrityViolation(
                    "execution plan Agent graph is not retained for this Run kind"
                )
            allowed_graph_states = (
                {"active", "replay_only"} if llm_execution_mode == "replay" else {"active"}
            )
            if execution_graph.status not in allowed_graph_states:
                raise Conflict(
                    "Agent execution graph lifecycle does not permit this Run mode",
                    agent_graph_version=execution_graph.agent_graph_version,
                    graph_status=execution_graph.status,
                    llm_execution_mode=llm_execution_mode,
                )
            if execution_graph.executor_key != definition.executor_key:
                raise IntegrityViolation("Agent execution graph executor differs from the Run kind")
            self._verify_agent_execution_graph_selector(
                execution_graph,
                resolved_profiles=resolved_profiles,
                catalog=catalog,
            )
            if (
                replay_context is not None
                and replay_context.preparation.execution_profile_authority.source_kind
                == "legacy_import"
            ):
                LegacyExecutionVersionPlanAuthorityValidator(read.routing).validate(
                    execution_version_plan,
                    expected_graph=execution_graph,
                )
            else:
                ExecutionVersionPlanAuthorityValidator(read.routing).validate(
                    execution_version_plan,
                    expected_graph=execution_graph,
                )
            self._verify_profile_capability_closure(
                resolved_profiles=resolved_profiles,
                execution_graph=execution_graph,
                catalog=catalog,
            )
        if validation_item is not None:
            self._verify_validation_subject_binding(
                item=validation_item,
                params=params,
                artifacts=artifacts,
                read=read,
                resolved_profiles=resolved_profiles,
            )
        verified_suite_scope = self._verify_task_suite_and_playtest_bindings(
            params=params,
            artifacts=artifacts,
            read=read,
            llm_execution_mode=llm_execution_mode,
            catalog=catalog,
        )
        # Resolve and authorize the real resource domain only after every typed
        # payload/lineage cross-binding needed by its resolver has been verified.
        # This still precedes the fresh create UoW, budget hold, and Run writes.
        authorization = self._authorize(
            definition=definition,
            kind=kind,
            params=params,
            artifacts=artifacts,
            resolved_profiles=resolved_profiles,
            read=read,
            actor=actor,
            pending_sources=source_writes,
            validation_item=permission_item,
            verified_suite_scope=verified_suite_scope,
            catalog=catalog,
        )
        additional_input_artifact_ids: tuple[str, ...] = ()
        if isinstance(params, DrDrillPayloadV1):
            manifest = self._resolve_dr_recovery_manifest(params=params, read=read)
            artifacts[manifest.artifact_id] = manifest
            additional_input_artifact_ids = (manifest.artifact_id,)
        # Internal-only deferred kinds cross the same exact RBAC and typed request
        # boundary as every executable Run. Artifact migration additionally closes
        # its retained edge here; the M4c deferred executor owns the honest
        # asynchronous unavailable outcome for both internal kinds.
        self._verify_migration_admission(
            definition=definition,
            params=params,
            artifacts=artifacts,
            resolved_profiles=resolved_profiles,
            catalog=catalog,
        )
        resolved_policy_snapshots = self._resolve_policy_snapshots(
            params=params,
            definition=definition,
            resolved_profiles=resolved_profiles,
            catalog=catalog,
        )
        policy_bindings, schema_bindings = self._registry.resolve_required_run_bindings(
            definition=definition,
            resolved_profiles=resolved_profiles,
        )
        version_tuple = self._freeze_version_tuple(
            params=params,
            artifacts=artifacts,
            execution_version_plan=execution_version_plan,
            cassette_artifact_id=cassette_artifact_id,
            seed=seed,
            producer_basis=version_tuple,
        )
        budget_set_snapshot_id = f"budget-set:{run_id}"
        payload = RunPayloadEnvelope(
            payload_schema_version=definition.payload_schema_id,
            input_artifact_ids=self._input_artifact_ids(
                params,
                cassette_artifact_id,
                additional_input_artifact_ids=additional_input_artifact_ids,
            ),
            version_tuple=version_tuple,
            execution_version_plan=execution_version_plan,
            policy_bindings=policy_bindings,
            schema_bindings=schema_bindings,
            execution_profile_catalog_version=catalog.catalog_version,
            execution_profile_catalog_digest=catalog.catalog_digest,
            resolved_profiles=resolved_profiles,
            resolved_policy_snapshots=resolved_policy_snapshots,
            budget_set_snapshot_id=budget_set_snapshot_id,
            seed=seed,
            llm_execution_mode=llm_execution_mode,
            cassette_artifact_id=cassette_artifact_id,
            params=params,
        )
        authorization = self._validate_replay_authority(
            kind=kind,
            payload=payload,
            authorization=authorization,
            actor=actor,
            replay_context=replay_context,
        )

        carrier = dispatch_trace_carrier
        if carrier is None:
            trace_context = current_trace_context()
            if trace_context is not None:
                carrier = TraceCarrier.inject(trace_context)

        now = _utc_now(self._clock)
        request = RunCreateRequest(
            run_id=run_id,
            kind=kind,
            creation_mode=creation_mode,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            request_id=request_id,
            payload=payload,
            resource_domain_scope=(
                authorization.resource_domain
                if isinstance(authorization.resource_domain, DomainScope)
                else None
            ),
            dispatch_trace_carrier=carrier,
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
        result = self._run_commands.create_run(
            request,
            companion_write=companion_write,
            fresh_admission_guard=self._fresh_admission_guard(
                actor=actor,
                authorization=authorization,
                artifacts=artifacts,
                params=params,
                subject_item=permission_item,
                source_writes=source_writes,
                replay_context=replay_context,
            ),
        )
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
        head = read.approvals.get_subject_head(item.subject_series_id)
        if (
            head is None
            or head.current_approval_id != item.approval_id
            or head.current_subject_artifact_id != item.subject_artifact_id
        ):
            raise Conflict("validation subject is not the current workflow head")
        if item.workflow_revision != expected_workflow_revision:
            raise Conflict(
                "validation workflow revision differs",
                expected_revision=expected_workflow_revision,
                actual_revision=item.workflow_revision,
            )
        if head.revision != expected_subject_head_revision:
            raise Conflict(
                "validation subject head revision differs",
                expected_revision=expected_subject_head_revision,
                actual_revision=head.revision,
            )
        if item.subject_digest != subject_digest:
            raise Conflict("validation subject digest differs from the retained subject")
        return item

    def _verify_validation_subject_binding(
        self,
        *,
        item: ApprovalItem,
        params: RunKindPayload,
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
    ) -> None:
        subject = (
            artifacts[params.subject.subject_artifact_id]
            if isinstance(
                params,
                (
                    PatchValidationPayloadV1,
                    ConstraintValidationPayloadV1,
                    RollbackValidationPayloadV1,
                ),
            )
            else None
        )
        if (
            subject is None
            or subject.artifact_id != item.subject_artifact_id
            or subject.payload_hash != item.subject_digest
        ):
            raise Conflict("validation subject Artifact differs from the workflow item")

        if isinstance(params, ConstraintValidationPayloadV1):
            proposal = self._load_json_artifact(
                subject,
                read=read,
                payload_schema_id="constraint-proposal@1",
                model=ConstraintProposalV1,
            )
            if not isinstance(proposal, ConstraintProposalV1):
                raise IntegrityViolation("constraint subject payload is invalid")
            if (
                proposal.produced_by != "human"
                or proposal.producer_run_id is not None
                or proposal.revision != item.subject_revision
                or proposal.dsl_grammar_version != params.dsl_grammar_version
                or proposal.domain_scope != item.domain_scope
                or item.target_binding is not None
            ):
                raise Conflict(
                    "constraint validation requires the exact human-authored workflow revision"
                )
            base = (
                None
                if params.base_constraint_snapshot_artifact_id is None
                else artifacts[params.base_constraint_snapshot_artifact_id]
            )
            if base is None:
                if (
                    proposal.base_constraint_snapshot_id is not None
                    or subject.version_tuple.constraint_snapshot_id is not None
                    or subject.version_tuple.doc_version != subject.payload_hash
                ):
                    raise Conflict("constraint proposal base binding is not exact")
            elif (
                proposal.base_constraint_snapshot_id != base.version_tuple.constraint_snapshot_id
                or subject.version_tuple.constraint_snapshot_id
                != base.version_tuple.constraint_snapshot_id
            ):
                raise Conflict("constraint proposal base binding is not exact")
            expected_lineage = {
                *(binding.source_artifact_id for binding in proposal.source_bindings),
                *(() if base is None else (base.artifact_id,)),
                *(
                    ()
                    if proposal.supersedes_artifact_id is None
                    else (proposal.supersedes_artifact_id,)
                ),
            }
            if set(subject.lineage) != expected_lineage:
                raise Conflict("constraint proposal lineage differs from its typed bindings")
            for binding in proposal.source_bindings:
                source = read.artifacts.get(binding.source_artifact_id)
                if (
                    not isinstance(source, ArtifactV2)
                    or source.payload_hash != binding.provenance_hash
                ):
                    raise Conflict("constraint proposal source binding is unavailable or stale")
            expected_ref = params.target.expected_ref
            if expected_ref is not None and (
                base is None or expected_ref.artifact_id != base.artifact_id
            ):
                raise Conflict("constraint target ref does not bind the exact base snapshot")
            compiler_bindings = tuple(
                binding
                for binding in resolved_profiles
                if binding.field_path == "/params/compiler_profile"
            )
            if len(compiler_bindings) != 1:
                raise IntegrityViolation(
                    "constraint validation lacks one exact compiler profile binding"
                )
            compiler_definition, _ = self._registry.resolve_execution_profile_binding(
                compiler_bindings[0]
            )
            if (
                compiler_definition.profile != params.compiler_profile
                or compiler_definition.handler_key != "builtin_constraint_compiler_profile@1"
            ):
                raise Conflict("constraint compiler implementation is not available exactly")
            if params.differential_engines != (BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1):
                raise Conflict(
                    "constraint differential engines differ from the exact compiler authority"
                )
            return

        binding = item.target_binding
        if isinstance(params, PatchValidationPayloadV1):
            base = artifacts[params.base_snapshot_artifact_id]
            preview = artifacts[params.preview_snapshot_artifact_id]
            self._verify_patch_subject_binding(
                item=item,
                subject=subject,
                base=base,
                preview=preview,
                constraint_snapshot_artifact_id=params.constraint_snapshot_artifact_id,
                expected_findings=params.expected_findings,
                read=read,
            )
            if (
                binding is None
                or binding.subject_kind != "patch"
                or binding.target_artifact_id != params.preview_snapshot_artifact_id
                or binding.target_snapshot_id != preview.version_tuple.ir_snapshot_id
                or binding.target_digest != preview.payload_hash
                or binding.ref_name != params.target.ref_name
                or binding.expected_ref != params.target.expected_ref
                or (
                    params.target.expected_ref is not None
                    and params.target.expected_ref.artifact_id != params.base_snapshot_artifact_id
                )
            ):
                raise Conflict("patch validation payload differs from its exact target binding")
            self._verify_patch_supporting_artifacts(
                params=params,
                preview=preview,
                artifacts=artifacts,
                read=read,
            )
            return

        if not isinstance(params, RollbackValidationPayloadV1):
            raise IntegrityViolation("unknown validation payload type")
        if not isinstance(binding, RollbackTargetBindingV1):
            raise Conflict("rollback validation lacks its exact target binding")
        target = artifacts[params.target_artifact_id]
        actual_snapshot_id = {
            "ir_snapshot": target.version_tuple.ir_snapshot_id,
            "constraint_snapshot": target.version_tuple.constraint_snapshot_id,
        }.get(target.kind)
        rollback_profile = next(
            (
                profile
                for profile in resolved_profiles
                if profile.field_path == "/params/rollback_profile"
            ),
            None,
        )
        request = self._load_json_artifact(
            subject,
            read=read,
            payload_schema_id="rollback-request@1",
            model=RollbackRequestV1,
        )
        if set(subject.lineage) != {
            params.expected_current_ref.artifact_id,
            params.target_artifact_id,
        }:
            raise Conflict(
                "rollback request lineage must exactly bind current and target Artifacts"
            )
        history = read.refs.get_history_entry(params.ref_name, params.target_history_revision)
        if (
            not isinstance(request, RollbackRequestV1)
            or request.ref_name != params.ref_name
            or request.expected_current_ref != params.expected_current_ref
            or request.target_artifact_id != params.target_artifact_id
            or request.target_history_revision != params.target_history_revision
            or rollback_profile is None
            or request.rollback_profile_binding != rollback_profile
            or binding.rollback_profile_binding != rollback_profile
            or binding.target_artifact_kind != target.kind
            or binding.target_artifact_id != target.artifact_id
            or binding.target_snapshot_id != actual_snapshot_id
            or binding.target_digest != target.payload_hash
            or binding.ref_name != params.ref_name
            or binding.expected_ref != params.expected_current_ref
            or history
            != RefValue(
                artifact_id=params.target_artifact_id,
                revision=params.target_history_revision,
            )
        ):
            raise Conflict("rollback validation payload differs from its exact draft binding")

    def _verify_patch_subject_binding(
        self,
        *,
        item: ApprovalItem,
        subject: ArtifactV2,
        base: ArtifactV2,
        preview: ArtifactV2,
        constraint_snapshot_artifact_id: str | None,
        expected_findings: tuple[FindingEvidenceBindingV1, ...],
        read: AdmissionReadPort,
    ) -> PatchV2:
        patch = self._load_json_artifact(
            subject,
            read=read,
            payload_schema_id="patch@2",
            model=PatchV2,
        )
        if not isinstance(patch, PatchV2):
            raise IntegrityViolation("Patch subject payload is invalid")
        expected_finding_ids = tuple(sorted(item.finding_id for item in expected_findings))
        constraint = (
            None
            if constraint_snapshot_artifact_id is None
            else read.artifacts.get(constraint_snapshot_artifact_id)
        )
        constraint_parent_ids = {
            parent_id
            for parent_id in subject.lineage
            if isinstance(parent := read.artifacts.get(parent_id), ArtifactV2)
            and parent.kind == "constraint_snapshot"
        }
        expected_constraint_parent_ids = (
            set() if constraint_snapshot_artifact_id is None else {constraint_snapshot_artifact_id}
        )
        semantic_constraint_id = (
            None
            if not isinstance(constraint, ArtifactV2)
            else constraint.version_tuple.constraint_snapshot_id
        )
        if (
            patch.revision != item.subject_revision
            or patch.base_snapshot_id != base.version_tuple.ir_snapshot_id
            or patch.target_snapshot_id != preview.version_tuple.ir_snapshot_id
            or subject.version_tuple.ir_snapshot_id != base.version_tuple.ir_snapshot_id
            or set(preview.lineage) != {base.artifact_id, subject.artifact_id}
            or tuple(sorted(set(patch.expected_to_fix))) != expected_finding_ids
            or len(patch.expected_to_fix) != len(set(patch.expected_to_fix))
            or constraint_parent_ids != expected_constraint_parent_ids
            or subject.version_tuple.constraint_snapshot_id != semantic_constraint_id
        ):
            raise Conflict("Patch payload/preview does not bind the exact workflow candidate")
        if constraint_snapshot_artifact_id is not None and (
            not isinstance(constraint, ArtifactV2)
            or constraint.kind != "constraint_snapshot"
            or semantic_constraint_id is None
        ):
            raise Conflict("Patch constraint snapshot Artifact is unavailable or stale")
        if base.artifact_id not in subject.lineage:
            raise Conflict("Patch subject does not directly bind its exact base Artifact")
        if patch.revision == 1 and patch.supersedes_artifact_id is not None:
            raise IntegrityViolation("Patch revision-one supersedes binding is invalid")
        if patch.revision > 1 and (
            patch.supersedes_artifact_id is None
            or patch.supersedes_artifact_id not in subject.lineage
        ):
            raise Conflict("Patch superseding revision lineage is incomplete")
        return patch

    @staticmethod
    def _subject_version_tuple(
        item: ApprovalItem, schema_version: str, *, root_seed: int | None
    ) -> VersionTuple:
        # Stamp the validation executor's producer version and the exact root seed.
        # For a profile-dependent kind with only deterministic profiles the seed is
        # deliberately null; ``0`` is not an interchangeable root seed and must not
        # be fabricated merely to accommodate a child executor's local default.
        binding = item.target_binding
        snapshot_id = getattr(binding, "target_snapshot_id", None)
        tool_version = _PRODUCER_TOOL_VERSIONS.get(schema_version, f"admission-{schema_version}")
        return VersionTuple(
            ir_snapshot_id=snapshot_id,
            tool_version=tool_version,
            seed=root_seed,
        )

    def _verify_patch_supporting_artifacts(
        self,
        *,
        params: PatchValidationPayloadV1,
        preview: ArtifactV2,
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
    ) -> None:
        subject = artifacts[params.subject.subject_artifact_id]
        subject_constraint_snapshot_id = subject.version_tuple.constraint_snapshot_id
        configs: dict[str, tuple[ConfigExportPackageV1, ArtifactV2]] = {}
        for config_id in params.candidate_config_export_artifact_ids:
            config = artifacts[config_id]
            package = self._load_config_export(config, read=read)
            constraint = read.artifacts.get(package.constraint_snapshot_artifact_id)
            if not isinstance(constraint, ArtifactV2) or constraint.kind != "constraint_snapshot":
                raise Conflict(
                    "candidate config constraint Artifact is unavailable",
                    artifact_id=package.constraint_snapshot_artifact_id,
                )
            self._verify_config_binding(
                package=package,
                config=config,
                preview=preview,
                constraint=constraint,
                environment_profile=package.target_environment_profile,
            )
            if constraint.version_tuple.constraint_snapshot_id != subject_constraint_snapshot_id:
                raise Conflict(
                    "candidate config constraint differs from the Patch subject",
                    artifact_id=config_id,
                )
            if package.constraint_snapshot_artifact_id != params.constraint_snapshot_artifact_id:
                raise Conflict(
                    "candidate config does not bind the exact Patch constraint Artifact",
                    artifact_id=config_id,
                )
            configs[config_id] = (package, constraint)

        for review_id in params.review_artifact_ids:
            review_artifact = artifacts[review_id]
            report = self._load_json_artifact(
                review_artifact,
                read=read,
                payload_schema_id="review@1",
                model=ReviewReport,
            )
            if not isinstance(report, ReviewReport):
                raise IntegrityViolation("review Artifact did not parse as ReviewReport")
            preview_snapshot_id = preview.version_tuple.ir_snapshot_id
            runtime_parent_ids = self._runtime_prompt_parent_ids(
                artifact=review_artifact,
                read=read,
            )
            content_parent_ids = set(review_artifact.lineage) - runtime_parent_ids
            if (
                report.snapshot_id != preview_snapshot_id
                or review_artifact.version_tuple.ir_snapshot_id != preview_snapshot_id
                or preview.artifact_id not in content_parent_ids
                or len(content_parent_ids) > 2
            ):
                raise Conflict("Review evidence does not directly consume the exact preview")
            constraint_id: str | None = None
            if len(content_parent_ids) == 2:
                constraint_id = next(
                    parent_id
                    for parent_id in content_parent_ids
                    if parent_id != preview.artifact_id
                )
                constraint = read.artifacts.get(constraint_id)
                if (
                    not isinstance(constraint, ArtifactV2)
                    or constraint.kind != "constraint_snapshot"
                    or review_artifact.version_tuple.constraint_snapshot_id
                    != constraint.version_tuple.constraint_snapshot_id
                ):
                    raise Conflict("Review evidence constraint lineage is unavailable or stale")
            elif review_artifact.version_tuple.constraint_snapshot_id is not None:
                raise Conflict("Review evidence has an unbound constraint VersionTuple")
            if constraint_id != params.constraint_snapshot_artifact_id:
                raise Conflict("Review evidence does not bind the exact Patch constraint")
            self._verify_runtime_prompt_producer(
                artifact=review_artifact,
                runtime_parent_ids=runtime_parent_ids,
                expected_kind=RunKindRef(kind="review.run", version=1),
                read=read,
                params_match=lambda source: (
                    isinstance(source, ReviewRunPayloadV1)
                    and source.snapshot_artifact_id == preview.artifact_id
                    and source.constraint_snapshot_artifact_id == constraint_id
                ),
            )

        for trace_id in params.playtest_trace_artifact_ids:
            trace_artifact = artifacts[trace_id]
            trace = self._load_json_artifact(
                trace_artifact,
                read=read,
                payload_schema_id="playtest-trace@1",
                model=PlaytestTraceV1,
            )
            if not isinstance(trace, PlaytestTraceV1):
                raise IntegrityViolation("playtest trace payload did not parse as PlaytestTraceV1")
            config_binding = configs.get(trace.config_artifact_id)
            if config_binding is None:
                raise Conflict("Playtest trace config is not an exact candidate config")
            package, constraint = config_binding
            suite_artifact = read.artifacts.get(trace.task_suite_artifact_id)
            if not isinstance(suite_artifact, ArtifactV2) or suite_artifact.kind != "task_suite":
                raise Conflict("Playtest trace TaskSuite is unavailable")
            suite = self._load_json_artifact(
                suite_artifact,
                read=read,
                payload_schema_id="task-suite@1",
                model=TaskSuiteV1,
            )
            if not isinstance(suite, TaskSuiteV1):
                raise IntegrityViolation("Playtest trace TaskSuite payload is invalid")
            by_episode = {episode.episode_id: episode for episode in suite.episodes}
            selected_scenarios: set[str] = set()
            for episode_trace in trace.episodes:
                episode = by_episode.get(episode_trace.episode_id)
                if (
                    episode is None
                    or episode.scenario_spec_artifact_id != episode_trace.scenario_spec_artifact_id
                ):
                    raise Conflict("Playtest trace episode binding is stale")
                selected_scenarios.add(episode_trace.scenario_spec_artifact_id)
            expected_lineage = {
                trace.config_artifact_id,
                trace.constraint_snapshot_artifact_id,
                trace.task_suite_artifact_id,
                *selected_scenarios,
            }
            runtime_parent_ids = self._runtime_prompt_parent_ids(
                artifact=trace_artifact,
                read=read,
            )
            if (
                trace.constraint_snapshot_artifact_id != constraint.artifact_id
                or suite.source_preview_artifact_id != preview.artifact_id
                or suite.config_export_artifact_id != trace.config_artifact_id
                or suite.constraint_snapshot_artifact_id != constraint.artifact_id
                or suite.environment_profile != trace.environment_profile
                or suite.env_contract_version != trace.env_contract_version
                or package.source_preview_artifact_id != preview.artifact_id
                or package.constraint_snapshot_artifact_id != constraint.artifact_id
                or set(trace_artifact.lineage) - runtime_parent_ids != expected_lineage
                or trace_artifact.version_tuple.ir_snapshot_id
                != preview.version_tuple.ir_snapshot_id
                or trace_artifact.version_tuple.constraint_snapshot_id
                != constraint.version_tuple.constraint_snapshot_id
                or trace_artifact.version_tuple.env_contract_version != trace.env_contract_version
                or trace_artifact.version_tuple.seed != trace.seed
            ):
                raise Conflict("Playtest evidence is not bound to the exact Patch candidate")
            expected_episodes = tuple(
                (episode.episode_id, episode.scenario_spec_artifact_id)
                for episode in trace.episodes
            )
            self._verify_runtime_prompt_producer(
                artifact=trace_artifact,
                runtime_parent_ids=runtime_parent_ids,
                expected_kind=RunKindRef(kind="playtest.run", version=1),
                read=read,
                params_match=lambda source: (
                    isinstance(source, PlaytestRunPayloadV1)
                    and source.config_artifact_id == trace.config_artifact_id
                    and source.constraint_snapshot_artifact_id
                    == trace.constraint_snapshot_artifact_id
                    and source.task_suite_artifact_id == trace.task_suite_artifact_id
                    and source.environment_profile == trace.environment_profile
                    and source.planner_policy == trace.planner_policy
                    and source.interaction_mode == trace.interaction_mode
                    and tuple(
                        (episode.episode_id, episode.scenario_spec_artifact_id)
                        for episode in source.episodes
                    )
                    == expected_episodes
                ),
                run_match=lambda run: run.payload.seed == trace.seed,
            )

    def _runtime_prompt_parent_ids(
        self,
        *,
        artifact: ArtifactV2,
        read: AdmissionReadPort,
    ) -> set[str]:
        """Return typed prompt parents; missing/invalid lineage never becomes ignorable."""

        prompt_ids: set[str] = set()
        for parent_id in artifact.lineage:
            parent = read.artifacts.get(parent_id)
            if not isinstance(parent, ArtifactV2):
                raise Conflict(
                    "supporting evidence lineage parent is unavailable",
                    artifact_id=artifact.artifact_id,
                    parent_artifact_id=parent_id,
                )
            if parent.kind != "source_rendered":
                continue
            self._require_payload_schema(parent, "source-rendered@1")
            self._load_artifact_blob(parent, read=read)
            prompt_ids.add(parent_id)
        has_llm_identity = (
            any(
                value is not None
                for value in (
                    artifact.version_tuple.prompt_version,
                    artifact.version_tuple.model_snapshot,
                    artifact.version_tuple.agent_graph_version,
                )
            )
            or artifact.meta.get("execution_identity") is not None
        )
        if has_llm_identity and not prompt_ids:
            raise Conflict(
                "LLM supporting evidence omits its source_rendered runtime lineage",
                artifact_id=artifact.artifact_id,
            )
        return prompt_ids

    def _verify_runtime_prompt_producer(
        self,
        *,
        artifact: ArtifactV2,
        runtime_parent_ids: set[str],
        expected_kind: RunKindRef,
        read: AdmissionReadPort,
        params_match: Callable[[RunKindPayload], bool],
        run_match: Callable[[RunRecord], bool] | None = None,
    ) -> None:
        """Authenticate extra prompt lineage against one exact successful producer Run."""

        if not runtime_parent_ids:
            return
        runs = read.runs
        list_for_run = getattr(runs, "list_prompt_render_links", None)
        get_link = getattr(runs, "get_intermediate_link", None)
        if runs is None or not all(callable(item) for item in (list_for_run, get_link)):
            raise DependencyUnavailable(
                "supporting evidence prompt-link authority is unavailable",
                component="supporting_evidence_prompt_lineage",
            )

        producer_run_ids: set[str] = set()
        exact_links: list[RunIntermediateArtifactLinkV1] = []
        for prompt_id in sorted(runtime_parent_ids):
            prompt = read.artifacts.get(prompt_id)
            if not isinstance(prompt, ArtifactV2) or prompt.kind != "source_rendered":
                raise IntegrityViolation(
                    "supporting prompt Artifact disappeared after lineage validation",
                    prompt_artifact_id=prompt_id,
                )
            producer_run_id = prompt.meta.get("producer_run_id")
            producer_attempt_no = prompt.meta.get("producer_attempt_no")
            logical_call_ordinal = prompt.meta.get("logical_call_ordinal")
            route_ordinal = prompt.meta.get("route_ordinal")
            ordinals = (producer_attempt_no, logical_call_ordinal, route_ordinal)
            if (
                not isinstance(producer_run_id, str)
                or not producer_run_id
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 1
                    for value in ordinals
                )
            ):
                raise IntegrityViolation(
                    "source_rendered producer identity metadata is invalid",
                    prompt_artifact_id=prompt_id,
                )
            assert isinstance(producer_attempt_no, int)
            assert isinstance(logical_call_ordinal, int)
            assert isinstance(route_ordinal, int)
            link = get_link(
                producer_run_id,
                producer_attempt_no,
                logical_call_ordinal,
                route_ordinal,
            )
            if (
                not isinstance(link, RunIntermediateArtifactLinkV1)
                or link.run_id != producer_run_id
                or link.attempt_no != producer_attempt_no
                or link.call_ordinal != logical_call_ordinal
                or link.route_ordinal != route_ordinal
                or link.artifact_id != prompt_id
                or link.role != "prompt_rendered"
            ):
                raise Conflict(
                    "supporting evidence has an unretained prompt parent; "
                    "source_rendered producer identity has no exact prompt link",
                    prompt_artifact_id=prompt_id,
                )
            producer_run_ids.add(producer_run_id)
            exact_links.append(link)
        if len(producer_run_ids) != 1:
            raise Conflict("supporting evidence prompt parents do not share one producer Run")

        run_id = next(iter(producer_run_ids))
        run = runs.get(run_id)
        if (
            not isinstance(run, RunRecord)
            or run.status != "succeeded"
            or run.kind != expected_kind
            or run.result_artifact_id is None
            or not params_match(run.payload.params)
            or (run_match is not None and not run_match(run))
        ):
            raise Conflict("supporting evidence prompt lineage has no exact producer Run")

        producer_links = tuple(list_for_run(run_id, attempt_no=None))
        if not all(isinstance(link, RunIntermediateArtifactLinkV1) for link in producer_links):
            raise IntegrityViolation("producer Run prompt projection contains an invalid link")
        expected_link_projection = {
            (link.run_id, link.attempt_no, link.call_ordinal, link.route_ordinal, link.artifact_id)
            for link in exact_links
        }
        retained_link_projection = {
            (link.run_id, link.attempt_no, link.call_ordinal, link.route_ordinal, link.artifact_id)
            for link in producer_links
        }
        if retained_link_projection != expected_link_projection:
            raise Conflict("supporting evidence omits or adds producer Run prompt lineage")
        if any(
            get_link(
                link.run_id,
                link.attempt_no,
                link.call_ordinal,
                link.route_ordinal,
            )
            != link
            for link in producer_links
        ):
            raise IntegrityViolation("producer Run prompt projection differs from retained links")

        result_artifact = read.artifacts.get(run.result_artifact_id)
        if not isinstance(result_artifact, ArtifactV2) or result_artifact.kind != "run_result":
            raise IntegrityViolation("supporting evidence producer has no exact RunResult")
        result = self._load_json_artifact(
            result_artifact,
            read=read,
            payload_schema_id="run-result@1",
            model=RunResultV1,
        )
        if not isinstance(result, RunResultV1):
            raise IntegrityViolation("supporting evidence producer RunResult is invalid")
        projection = result.version_projection
        projected_parent_ids = {parent.artifact_id for parent in projection.parents}
        prompt_parent_ids = {
            parent.artifact_id for parent in projection.parents if parent.role == "intermediate"
        }
        output_parent_ids = {
            parent.artifact_id for parent in projection.parents if parent.role == "output"
        }
        # The payload hash/frozen tuple close params, resolved policies, tool/model
        # plan, and replay cassette authority; the terminal tuple plus exact parent
        # projection close the supporting Artifact version and complete lineage.
        if (
            result.run_id != run.run_id
            or result.run_kind != run.kind
            or result.attempt_no != run.current_attempt_no
            or result.primary_artifact_id != artifact.artifact_id
            or artifact.artifact_id not in result.produced_artifact_ids
            or projection.manifest_scope != "run"
            or projection.attempt_no != run.current_attempt_no
            or projection.run_kind != run.kind
            or not compare_digest(projection.run_payload_hash, run.payload_hash)
            or projection.frozen_input_version_tuple != run.payload.version_tuple
            or projection.terminal_version_tuple != artifact.version_tuple
            or result_artifact.version_tuple != projection.terminal_version_tuple
            or projected_parent_ids != set(result_artifact.lineage)
            or not runtime_parent_ids.issubset(prompt_parent_ids)
            or artifact.artifact_id not in output_parent_ids
        ):
            raise Conflict("supporting evidence prompt lineage has no exact producer Run")

    def _load_repair_subject(
        self,
        *,
        read: AdmissionReadPort,
        params: PatchRepairPayloadV1,
        artifacts: dict[str, ArtifactV2],
    ) -> ApprovalItem:
        approval_id = f"approval:patch:{params.subject_patch_artifact_id}"
        item = read.approvals.get(approval_id)
        if not isinstance(item, ApprovalItem):
            raise Conflict("repair subject approval item is unavailable")
        head = read.approvals.get_subject_head(item.subject_series_id)
        binding = item.target_binding
        if (
            item.subject_kind != "patch"
            or item.subject_artifact_id != params.subject_patch_artifact_id
            or item.status != "validation_failed"
            or item.workflow_revision != params.expected_workflow_revision
            or item.last_validation_failure_artifact_id is not None
            or item.evidence_set_artifact_id != params.validation_evidence_artifact_id
            or head is None
            or head.current_approval_id != item.approval_id
            or head.current_subject_artifact_id != item.subject_artifact_id
            or head.revision != params.expected_subject_head_revision
            or binding is None
            or binding.subject_kind != "patch"
            or binding.target_artifact_id != params.preview_snapshot_artifact_id
            or binding.ref_name != params.target.ref_name
            or binding.expected_ref != params.target.expected_ref
            or (
                params.target.expected_ref is not None
                and params.target.expected_ref.artifact_id != params.base_snapshot_artifact_id
            )
        ):
            raise Conflict("repair payload does not bind the exact current failed subject")
        subject = artifacts[params.subject_patch_artifact_id]
        base = artifacts[params.base_snapshot_artifact_id]
        preview = artifacts[params.preview_snapshot_artifact_id]
        self._verify_patch_subject_binding(
            item=item,
            subject=subject,
            base=base,
            preview=preview,
            constraint_snapshot_artifact_id=params.constraint_snapshot_artifact_id,
            expected_findings=params.findings,
            read=read,
        )
        evidence_artifact = artifacts[params.validation_evidence_artifact_id]
        evidence = self._load_json_artifact(
            evidence_artifact,
            read=read,
            payload_schema_id="evidence-set@1",
            model=EvidenceSet,
        )
        if not isinstance(evidence, EvidenceSet):
            raise IntegrityViolation("repair validation evidence payload is invalid")
        if (
            evidence.overall_status not in {"failed", "unproven"}
            or evidence.subject_artifact_id != subject.artifact_id
            or evidence.subject_digest != subject.payload_hash
            or evidence.target_binding != item.target_binding
            or evidence.finding_bindings != params.findings
        ):
            raise Conflict("repair EvidenceSet differs from the failed workflow subject")
        evidence_parent_ids = {
            subject.artifact_id,
            *(
                ()
                if evidence.target_binding is None
                else (evidence.target_binding.target_artifact_id,)
            ),
            *evidence.supporting_artifact_ids,
            *(item.evidence_artifact_id for item in evidence.finding_bindings),
            *(
                requirement.evidence_artifact_id
                for requirement in evidence.requirements
                if requirement.evidence_artifact_id is not None
            ),
        }
        if set(evidence_artifact.lineage) != evidence_parent_ids:
            raise Conflict("repair EvidenceSet lineage is not closed over its typed evidence")
        for artifact_id in evidence_parent_ids:
            retained = artifacts.get(artifact_id) or read.artifacts.get(artifact_id)
            if not isinstance(retained, ArtifactV2):
                raise Conflict(
                    "repair EvidenceSet references unavailable evidence",
                    artifact_id=artifact_id,
                )
        if read.runs is None:
            raise DependencyUnavailable(
                "repair validation Run authority is unavailable",
                component="repair_validation_run",
            )
        validation_run = read.runs.get(evidence.validation_run_id)
        validation_params = (
            validation_run.payload.params if isinstance(validation_run, RunRecord) else None
        )
        if (
            not isinstance(validation_run, RunRecord)
            or validation_run.status != "succeeded"
            or validation_run.kind != RunKindRef(kind="patch.validate", version=1)
            or not isinstance(validation_params, PatchValidationPayloadV1)
            or validation_params.subject.subject_artifact_id != subject.artifact_id
            or validation_params.subject.subject_digest != subject.payload_hash
            or validation_params.base_snapshot_artifact_id != base.artifact_id
            or validation_params.preview_snapshot_artifact_id != preview.artifact_id
            or validation_params.findings != params.findings
            or validation_params.target != params.target
        ):
            raise Conflict("repair EvidenceSet is not backed by the exact validation Run")
        return item

    def _resolve_policy_snapshots(
        self,
        *,
        params: RunKindPayload,
        definition: RunKindDefinition,
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
        catalog: ExecutionProfileCatalogSnapshotV1,
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

        Validation requirements are derived from the frozen validation payload.
        Generation and repair requirements are instead materialized from the exact
        versioned profile ``config`` retained in the Run's catalog binding. This is
        what makes their resolved count/subset rules reachable without consulting a
        process-current/default policy at worker or publication time.
        """

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

        field_path = _VALIDATION_POLICY_FIELD.get(type(params))
        if field_path is None:
            return self._resolve_profile_policy_snapshots(
                params=params,
                referenced=referenced,
                resolved_profiles=resolved_profiles,
                catalog=catalog,
            )

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

    def _resolve_profile_policy_snapshots(
        self,
        *,
        params: RunKindPayload,
        referenced: dict[str, set[str]],
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> tuple[ResolvedPolicySnapshotV1, ...]:
        profile_contract: tuple[str, type[GenerationProfileConfigV1 | PatchRepairProfileConfigV1]]
        if isinstance(params, GenerationProposePayloadV1):
            profile_contract = ("/params/generation_policy", GenerationProfileConfigV1)
        elif isinstance(params, PatchRepairPayloadV1):
            profile_contract = ("/params/repair_policy", PatchRepairProfileConfigV1)
        else:
            raise IntegrityViolation(
                "resolved-policy bindings lack a supported admission resolver",
                run_payload_schema=params.schema_version,
            )

        field_path, config_model = profile_contract
        source_binding = next(
            (binding for binding in resolved_profiles if binding.field_path == field_path),
            None,
        )
        if source_binding is None:
            raise IntegrityViolation(
                "resolved-policy snapshot requires its exact source profile",
                field_path=field_path,
            )
        profile_definition = next(
            (item for item in catalog.definitions if item.profile == source_binding.profile),
            None,
        )
        if (
            profile_definition is None
            or execution_profile_payload_hash(profile_definition)
            != source_binding.profile_payload_hash
        ):
            raise IntegrityViolation(
                "resolved-policy source profile differs from the exact catalog binding"
            )
        try:
            profile_config = config_model.model_validate(profile_definition.config)
        except ValueError as exc:
            raise IntegrityViolation("resolved-policy source profile config is invalid") from exc
        policy = profile_config.resolved_policy
        if set(referenced) != {policy.resolved_policy_id}:
            raise IntegrityViolation(
                "resolved-policy profile config does not close outcome policy references"
            )

        document = {"params": params.model_dump(mode="python")}
        requirements: list[ResolvedArtifactRequirementV1] = []
        configured_rules: set[str] = set()
        resolved_by_path = {binding.field_path: binding for binding in resolved_profiles}
        for source in policy.requirement_sources:
            configured_rules.add(source.outcome_rule_id)
            if isinstance(source, FixedResolvedPolicyRequirementConfigV1):
                if source.producer_profile_field_path not in resolved_by_path:
                    raise IntegrityViolation(
                        "fixed resolved-policy requirement names an unresolved producer profile",
                        field_path=source.producer_profile_field_path,
                    )
                requirements.append(
                    ResolvedArtifactRequirementV1(
                        requirement_id=source.requirement_id,
                        outcome_rule_id=source.outcome_rule_id,
                        artifact_kind=source.artifact_kind,
                        payload_schema_id=source.payload_schema_id,
                        producer_profile_field_path=source.producer_profile_field_path,
                        ordinal=source.ordinal,
                    )
                )
                continue

            values = _resolve_pointer(document, source.collection_field_path)
            if not isinstance(values, (tuple, list)):
                raise IntegrityViolation(
                    "resolved-policy collection source does not resolve to an array",
                    field_path=source.collection_field_path,
                )
            for ordinal, value in enumerate(values, start=1):
                producer_field_path: str | None = None
                if isinstance(source, ProfileCollectionResolvedPolicyRequirementConfigV1):
                    profile = ProfileRefV1.model_validate(value)
                    requirement_id = f"{profile.profile_id}@{profile.version}"
                    producer_field_path = f"{source.collection_field_path}/{ordinal - 1}"
                    producer = resolved_by_path.get(producer_field_path)
                    if producer is None or producer.profile != profile:
                        raise IntegrityViolation(
                            "resolved-policy profile collection differs from resolved profiles",
                            field_path=producer_field_path,
                        )
                elif isinstance(source, ArtifactCollectionResolvedPolicyRequirementConfigV1):
                    if not isinstance(value, str) or not value:
                        raise IntegrityViolation(
                            "resolved-policy artifact collection requires artifact ids"
                        )
                    requirement_id = value
                else:  # pragma: no cover - discriminated contract is exhaustive
                    raise IntegrityViolation("unknown resolved-policy requirement source")
                requirements.append(
                    ResolvedArtifactRequirementV1(
                        requirement_id=requirement_id,
                        outcome_rule_id=source.outcome_rule_id,
                        artifact_kind=source.artifact_kind,
                        payload_schema_id=source.payload_schema_id,
                        producer_profile_field_path=producer_field_path,
                        ordinal=ordinal,
                    )
                )

        expected_rules = referenced[policy.resolved_policy_id]
        if configured_rules != expected_rules:
            raise IntegrityViolation(
                "resolved-policy profile requirements do not close outcome rule references"
            )
        return (
            _build_resolved_policy_snapshot(
                resolved_policy_id=policy.resolved_policy_id,
                source_profile_field_path=field_path,
                source_profile_payload_hash=source_binding.profile_payload_hash,
                requirements=tuple(requirements),
            ),
        )

    # ── profile resolution (reuses registry requirement metadata) ────────────
    def _execution_profile_catalog_for_request(
        self,
        *,
        kind: RunKindRef,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        cassette_artifact_id: str | None,
        read: AdmissionReadPort,
    ) -> tuple[
        ExecutionProfileCatalogSnapshotV1,
        _ReplayAdmissionContext | None,
    ]:
        """Select current authority, except when REPLAY freezes historical authority.

        Native cassettes derive the catalog from their retained RECORD Run; verified
        legacy imports derive it from their exact profile bindings.  The request
        cannot choose an old catalog itself, and the engine's process-current
        catalog is never mutated while serving concurrent admissions.
        """

        if llm_execution_mode != "replay":
            return self._catalog, None
        if cassette_artifact_id is None:
            # The generic/specialized request-shape validator rejects this before
            # any profile is resolved or a Run is written.  Keeping current here
            # preserves the more specific Bench/Review admission diagnostic while
            # never treating it as replay authority for an executable request.
            return self._catalog, None
        if read.runs is None or read.object_bindings is None:
            raise DependencyUnavailable(
                "replay admission retained authorities are unavailable",
                component="replay_admission",
            )
        replay_reader = _AdmissionReplayReader(read=read, objects=self._objects)
        legacy_authority = (
            None
            if self._legacy_import_authority is None
            else _RecordingLegacyImportAuthority(self._legacy_import_authority)
        )
        legacy_decision_delegate = self._legacy_import_decisions or read.routing
        legacy_decisions = (
            None
            if legacy_decision_delegate is None
            else _RecordingLegacyDecisionRepository(legacy_decision_delegate)
        )
        validator = ReplayAdmissionValidator(
            replay_reader,
            legacy_authority=legacy_authority,
            legacy_decisions=legacy_decisions,
        )
        preparation = validator.prepare(
            kind=kind,
            cassette_artifact_id=cassette_artifact_id,
        )
        replay_authority = preparation.execution_profile_authority
        catalog = read.policies.get_execution_profile_catalog(
            catalog_version=replay_authority.catalog_version,
            catalog_digest=replay_authority.catalog_digest,
        )
        if not isinstance(catalog, ExecutionProfileCatalogSnapshotV1):
            raise IntegrityViolation(
                "replay execution-profile catalog history is unavailable",
                catalog_version=replay_authority.catalog_version,
            )
        return catalog, _ReplayAdmissionContext(
            reader=replay_reader,
            validator=validator,
            preparation=preparation,
            legacy_authority=legacy_authority,
            legacy_decisions=legacy_decisions,
        )

    def _resolve_profiles(
        self,
        *,
        params: RunKindPayload,
        kind: RunKindRef,
        read: AdmissionReadPort,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        catalog: ExecutionProfileCatalogSnapshotV1,
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
                        catalog=catalog,
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
                            catalog=catalog,
                        )
                    )
        definitions = {definition.profile: definition for definition in catalog.definitions}
        lifecycle = {item.profile: item for item in catalog.lifecycle}
        for binding in resolved:
            definition = definitions.get(binding.profile)
            state = lifecycle.get(binding.profile)
            if definition is None or state is None:
                raise IntegrityViolation(
                    "resolved execution profile is absent from the exact catalog closure"
                )
            if kind not in definition.compatible_run_kinds:
                raise Conflict(
                    "execution profile is incompatible with the admitted Run kind",
                    profile_id=binding.profile.profile_id,
                    profile_version=binding.profile.version,
                )
            if params.schema_version not in definition.input_schema_ids:
                raise Conflict(
                    "execution profile does not accept the admitted payload schema",
                    profile_id=binding.profile.profile_id,
                    payload_schema_id=params.schema_version,
                )
            required_outputs = set(PROFILE_OUTPUT_SCHEMA_REQUIREMENTS[definition.profile_kind])
            missing_outputs = tuple(
                sorted(required_outputs.difference(definition.output_schema_ids))
            )
            if missing_outputs:
                raise Conflict(
                    "execution profile output schema interface is incomplete",
                    profile_id=binding.profile.profile_id,
                    profile_version=binding.profile.version,
                    missing_output_schema_ids=missing_outputs,
                )
            allowed_states = (
                {"active", "replay_only"} if llm_execution_mode == "replay" else {"active"}
            )
            if state.state not in allowed_states:
                raise Conflict(
                    "execution profile lifecycle does not permit this Run execution mode",
                    profile_id=binding.profile.profile_id,
                    profile_version=binding.profile.version,
                    profile_state=state.state,
                    llm_execution_mode=llm_execution_mode,
                )
            if llm_execution_mode == "replay":
                current_definition = next(
                    (item for item in self._catalog.definitions if item.profile == binding.profile),
                    None,
                )
                current_state = next(
                    (item for item in self._catalog.lifecycle if item.profile == binding.profile),
                    None,
                )
                if current_definition is None or current_state is None:
                    raise Conflict(
                        "replay execution profile is absent from the current catalog",
                        profile_id=binding.profile.profile_id,
                        profile_version=binding.profile.version,
                    )
                if (
                    execution_profile_payload_hash(current_definition)
                    != binding.profile_payload_hash
                ):
                    raise IntegrityViolation(
                        "current catalog redefines a historical execution profile"
                    )
                if current_state.state == "disabled":
                    raise Conflict(
                        "replay execution profile is currently disabled",
                        profile_id=binding.profile.profile_id,
                        profile_version=binding.profile.version,
                    )
        return tuple(resolved)

    def _verify_execution_request_shape(
        self,
        *,
        definition: RunKindDefinition,
        params: RunKindPayload,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        execution_version_plan: ExecutionVersionPlanV1 | None,
        cassette_artifact_id: str | None,
    ) -> None:
        """Reject client execution-control drift before constructing the envelope.

        ``RunPayloadEnvelope`` and ``RunCommandService`` retain the same guards as
        defence in depth, but fresh admission must return a typed domain conflict
        instead of leaking a Pydantic ``ValidationError`` or waiting until the
        authoritative write transaction to discover malformed mode/plan/cassette
        combinations.
        """

        if llm_execution_mode not in definition.allowed_llm_execution_modes:
            raise Conflict(
                "Run execution mode is not allowed by its retained Run kind",
                run_kind=f"{definition.kind}@{definition.version}",
                llm_execution_mode=llm_execution_mode,
            )
        if execution_version_plan is not None and not isinstance(
            execution_version_plan, ExecutionVersionPlanV1
        ):
            raise IntegrityViolation("execution version plan must use the exact wire contract")
        if llm_execution_mode == "not_applicable":
            if execution_version_plan is not None or cassette_artifact_id is not None:
                raise Conflict("not_applicable execution forbids a plan and cassette")
        elif llm_execution_mode in {"live", "record"}:
            if execution_version_plan is None or cassette_artifact_id is not None:
                raise Conflict("live/record execution requires only an exact execution plan")
        elif execution_version_plan is None or cassette_artifact_id is None:
            raise Conflict("replay execution requires a plan and exact cassette Artifact")

        if isinstance(params, ReviewRunPayloadV1):
            has_triage_profile = params.llm_triage_policy is not None
            if has_triage_profile and llm_execution_mode == "not_applicable":
                raise Conflict("review LLM triage profile requires model execution")
            if not has_triage_profile and llm_execution_mode != "not_applicable":
                raise Conflict("review without an LLM triage profile must be not_applicable")

    def _verify_seed_policy(
        self,
        *,
        definition: RunKindDefinition,
        params: RunKindPayload,
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
        seed: int | None,
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> None:
        policy = definition.seed_policy
        if policy == "required":
            if seed is None:
                raise Conflict("Run kind requires an explicit root seed")
            return
        if policy == "forbidden":
            if seed is not None:
                raise Conflict("Run kind forbids a root seed")
            return
        if policy != "profile_dependent":
            raise IntegrityViolation(
                "Run kind has an unknown seed policy",
                run_kind=f"{definition.kind}@{definition.version}",
                seed_policy=policy,
            )

        definitions = {item.profile: item for item in catalog.definitions}
        profile_definitions = []
        for binding in resolved_profiles:
            profile = definitions.get(binding.profile)
            if profile is None:
                raise IntegrityViolation(
                    "resolved profile is absent from the exact seed-policy catalog",
                    field_path=binding.field_path,
                )
            profile_definitions.append(profile)
        stochastic = any(item.stochastic for item in profile_definitions) or (
            patch_repair_requires_root_seed(params)
            or validation_regression_requires_root_seed(params)
        )
        if stochastic and seed is None:
            raise Conflict("profile-dependent seed is required by stochastic profiles")
        if not stochastic and seed is not None:
            raise Conflict("profile-dependent seed is forbidden by deterministic profiles")

    def _verify_migration_admission(
        self,
        *,
        definition: RunKindDefinition,
        params: RunKindPayload,
        artifacts: dict[str, ArtifactV2],
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> None:
        """Close an internal migration request over one exact retained edge.

        The source Artifact is server-resolved, the migrator's versioned edge is
        the target allowlist, and the RunKind's frozen capability matrix determines
        whether that edge may publish.  Wildcards/current defaults never become an
        executable migration path.
        """

        if not isinstance(params, ArtifactMigrationPayloadV1):
            return
        matrix_ref = definition.migration_capability_matrix
        if matrix_ref is None:
            raise IntegrityViolation("artifact migration Run kind lacks its capability matrix")
        matrix = self._registry.get_migration_capability_matrix(matrix_ref)
        if matrix is None:
            raise IntegrityViolation("artifact migration capability matrix is unavailable")

        source = artifacts[params.source_artifact_id]
        source_schema = source.meta.get("payload_schema_id")
        if not isinstance(source_schema, str) or not source_schema:
            raise IntegrityViolation("migration source lacks its exact payload schema")
        binding = next(
            (item for item in resolved_profiles if item.field_path == "/params/migrator"),
            None,
        )
        if binding is None or binding.profile != params.migrator:
            raise IntegrityViolation("migration request lacks its exact migrator binding")
        profile = next(
            (item for item in catalog.definitions if item.profile == binding.profile),
            None,
        )
        if (
            profile is None
            or profile.profile_kind != "artifact_migrator"
            or execution_profile_payload_hash(profile) != binding.profile_payload_hash
            or not isinstance(profile.details, MigrationProfileDetailsV1)
        ):
            raise IntegrityViolation("migration profile binding is not retained exactly")

        edge_key = (
            source.kind,
            source_schema,
            params.target_payload_schema_id,
            params.target_meta_schema_version,
            params.target_dsl_grammar_version,
        )
        profile_edge = next(
            (
                edge
                for edge in profile.details.edges
                if (
                    edge.source_kind,
                    edge.source_payload_schema_id,
                    edge.target_payload_schema_id,
                    edge.target_meta_schema_version,
                    edge.target_dsl_grammar_version,
                )
                == edge_key
            ),
            None,
        )
        if profile_edge is None:
            raise Conflict(
                "migration target is absent from the exact migrator profile allowlist",
                source_kind=source.kind,
                source_payload_schema_id=source_schema,
                target_payload_schema_id=params.target_payload_schema_id,
            )

        matrix_edge = next(
            (
                edge
                for edge in matrix.edges
                if (
                    edge.source_kind,
                    edge.source_payload_schema_id,
                    edge.target_payload_schema_id,
                    edge.target_meta_schema_version,
                    edge.target_dsl_grammar_version,
                )
                == edge_key
            ),
            None,
        )
        if matrix_edge is None:
            default = next(
                (item for item in matrix.kind_defaults if item.source_kind == source.kind),
                None,
            )
            if default is None:
                raise IntegrityViolation("migration matrix omits the source-kind default")
            if default.unsupported_edge_action == "reject_409":
                raise Conflict(
                    "migration edge is rejected by the exact capability matrix",
                    source_kind=source.kind,
                    source_payload_schema_id=source_schema,
                    target_payload_schema_id=params.target_payload_schema_id,
                )
            capability = default.unsupported_edge_action
        else:
            capability = matrix_edge.capability

        if params.publish_mode == "publish_migrated_artifact" and capability != "publish_same_kind":
            raise Conflict(
                "migration capability does not permit publishing a migrated Artifact",
                capability=capability,
            )

    def _resolve_dr_recovery_manifest(
        self,
        *,
        params: DrDrillPayloadV1,
        read: AdmissionReadPort,
    ) -> ArtifactV2:
        authority = self._dr_recovery_manifest_authority
        if authority is None:
            raise DependencyUnavailable(
                "DR drill admission requires a verified recovery catalog authority",
                component="recovery_catalog_admission",
            )
        manifest = authority.resolve_verified_manifest(
            recovery_catalog_entry_id=params.recovery_catalog_entry_id,
            expected_checkpoint_id=params.expected_checkpoint_id,
        )
        if not isinstance(manifest, ArtifactV2):
            raise IntegrityViolation("recovery catalog returned an invalid manifest Artifact")
        if (
            manifest.kind != "operational_evidence"
            or manifest.meta.get("payload_schema_id") != "backup-object-manifest@1"
        ):
            raise IntegrityViolation(
                "recovery catalog manifest does not bind backup-object-manifest@1"
            )
        retained = read.artifacts.get(manifest.artifact_id)
        if retained != manifest:
            raise Conflict(
                "verified recovery manifest is not retained exactly",
                artifact_id=manifest.artifact_id,
            )
        self._load_artifact_blob(manifest, read=read)
        return manifest

    def _resolve_one(
        self,
        *,
        read: AdmissionReadPort,
        field_path: str,
        profile: ProfileRefV1,
        expected_profile_kind: ExecutionProfileKindV1,
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> ResolvedExecutionProfileBindingV1:
        definition = next(
            (item for item in catalog.definitions if item.profile == profile),
            None,
        )
        if definition is None:
            raise Conflict(
                "execution profile is absent from the exact catalog",
                profile_id=profile.profile_id,
                profile_version=profile.version,
            )
        if definition.profile_kind != expected_profile_kind:
            raise Conflict(
                "execution profile kind differs from the required field binding",
                profile_id=profile.profile_id,
                profile_version=profile.version,
                expected_profile_kind=expected_profile_kind,
                actual_profile_kind=definition.profile_kind,
            )
        return read.policies.resolve_execution_profile(
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.catalog_digest,
            field_path=field_path,
            profile=profile,
            expected_profile_kind=expected_profile_kind,
        )

    def _verify_profile_capability_closure(
        self,
        *,
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
        execution_graph: AgentExecutionGraphV1,
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> None:
        """Bind model-facing profile capabilities to the retained Agent graph."""

        definitions = {item.profile: item for item in catalog.definitions}
        required: set[str] = set()
        for binding in resolved_profiles:
            definition = definitions.get(binding.profile)
            if definition is None:
                raise IntegrityViolation(
                    "resolved profile capability binding has no exact definition"
                )
            # Solver/environment/workload capabilities are closed by their trusted
            # handler registries. These profile kinds directly drive model nodes.
            if definition.profile_kind in _MODEL_CAPABILITY_PROFILE_KINDS:
                required.update(definition.required_capabilities)
        graph_capabilities = {
            capability
            for node in execution_graph.nodes
            for capability in node.required_capabilities
        }
        missing = tuple(sorted(required.difference(graph_capabilities)))
        if missing:
            raise Conflict(
                "execution profile capabilities are absent from the retained Agent graph",
                missing_capabilities=missing,
                agent_graph_version=execution_graph.agent_graph_version,
            )

    def _verify_agent_execution_graph_selector(
        self,
        graph: AgentExecutionGraphV1,
        *,
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> None:
        selector = graph.profile_selector
        if selector is None:
            return
        binding = next(
            (item for item in resolved_profiles if item.field_path == selector.profile_field_path),
            None,
        )
        if binding is None:
            raise IntegrityViolation(
                "Agent graph selector profile is absent from resolved bindings",
                field_path=selector.profile_field_path,
            )
        definition = next(
            (item for item in catalog.definitions if item.profile == binding.profile),
            None,
        )
        if definition is None:
            raise IntegrityViolation(
                "Agent graph selector profile is absent from the exact catalog"
            )
        if execution_profile_payload_hash(definition) != binding.profile_payload_hash:
            raise IntegrityViolation(
                "Agent graph selector profile differs from its exact catalog binding"
            )
        if definition.profile_kind == "playtest_planner":
            try:
                PlaytestPlannerProfileConfigV2.model_validate(definition.config)
            except ValueError as exc:
                raise IntegrityViolation(
                    "playtest planner profile has an invalid versioned config"
                ) from exc
        actual = _resolve_pointer(definition.config, selector.config_pointer)
        if actual != selector.expected_value:
            raise Conflict(
                "execution plan Agent graph does not match the exact profile config",
                agent_graph_version=graph.agent_graph_version,
                field_path=selector.profile_field_path,
                config_pointer=selector.config_pointer,
            )

    # ── RBAC authorization with the server-resolved resource domain ──────────
    def _authorize(
        self,
        *,
        definition: RunKindDefinition,
        kind: RunKindRef,
        params: RunKindPayload,
        artifacts: dict[str, ArtifactV2],
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
        read: AdmissionReadPort,
        actor: ActorContext,
        pending_sources: tuple[_SourceWrite, ...],
        validation_item: ApprovalItem | None,
        verified_suite_scope: DomainScope | None,
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> _AuthorizationBinding:
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
        if validation_item is not None and (
            validation_item.domain_registry_ref != role_policy.domain_registry_ref
            or validation_item.role_policy_version != role_policy.policy_version
            or validation_item.role_policy_digest != role_policy.policy_digest
        ):
            raise IntegrityViolation(
                "workflow subject governance differs from the exact admission policy"
            )
        resolver = self._registry.get_permission_resolver_key(kind)
        if definition.required_permission.domain_scope == "all" and resolver != (
            f"{kind.kind}-domain-resolver@1"
        ):
            raise IntegrityViolation("Run kind lacks its exact permission-domain resolver")
        resource_domain = self._resolve_resource_domain(
            base=definition.required_permission,
            params=params,
            artifacts=artifacts,
            read=read,
            registry=registry,
            validation_item=validation_item,
            verified_suite_scope=verified_suite_scope,
        )
        requested = Permission(
            action=definition.required_permission.action,
            resource_kind=definition.required_permission.resource_kind,
            domain_scope=resource_domain,
        )
        self._verify_profile_scope(
            kind=kind,
            params=params,
            resolved_profiles=resolved_profiles,
            resource_domain=resource_domain,
            catalog=catalog,
        )
        permissions = (requested,)
        for permission in permissions:
            if (
                authorize(
                    principal=actor.principal,
                    role_policy=role_policy,
                    requested_permission=permission,
                    domain_registry=registry,
                )
                is not AuthorizationDecision.ALLOW
            ):
                raise Forbidden(
                    "actor is not authorized to admit this run kind in the resolved domain",
                    action=permission.action,
                    resource_kind=permission.resource_kind,
                )
        del pending_sources
        return _AuthorizationBinding(
            role_policy=role_policy,
            domain_registry=registry,
            resource_domain=resource_domain,
            permissions=permissions,
        )

    def _validate_replay_authority(
        self,
        *,
        kind: RunKindRef,
        payload: RunPayloadEnvelope,
        authorization: _AuthorizationBinding,
        actor: ActorContext,
        replay_context: _ReplayAdmissionContext | None,
    ) -> _AuthorizationBinding:
        if payload.llm_execution_mode != "replay":
            return authorization
        if replay_context is None:
            raise IntegrityViolation("replay admission lacks its exact prepared cassette authority")
        proof = replay_context.validator.validate(
            kind=kind,
            payload=payload,
            preparation=replay_context.preparation,
        )
        # Injected legacy facades are immutable/content-bound process authorities,
        # not transaction rows. Recheck them here so the SQLite writer is never held
        # while defensive-copying an arbitrarily large imported authority set.
        if replay_context.legacy_authority is not None:
            replay_context.legacy_authority.revalidate()
        if (
            replay_context.legacy_decisions is not None
            and self._legacy_import_decisions is not None
        ):
            replay_context.legacy_decisions.revalidate(
                self._legacy_import_decisions,
                require_batch=False,
            )
        replay_permission = proof.required_permission(authorization.resource_domain)
        if (
            authorize(
                principal=actor.principal,
                role_policy=authorization.role_policy,
                requested_permission=replay_permission,
                domain_registry=authorization.domain_registry,
            )
            is not AuthorizationDecision.ALLOW
        ):
            raise Forbidden(
                "actor is not authorized to replay this Run in the resolved domain",
                action=replay_permission.action,
                resource_kind=replay_permission.resource_kind,
            )
        return _AuthorizationBinding(
            role_policy=authorization.role_policy,
            domain_registry=authorization.domain_registry,
            resource_domain=authorization.resource_domain,
            permissions=(*authorization.permissions, replay_permission),
        )

    def _verify_profile_scope(
        self,
        *,
        kind: RunKindRef,
        params: RunKindPayload,
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
        resource_domain: DomainScopeValue,
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> None:
        definitions = {definition.profile: definition for definition in catalog.definitions}
        for binding in resolved_profiles:
            definition = definitions.get(binding.profile)
            if definition is None:
                raise IntegrityViolation("resolved profile has no exact catalog definition")
            if kind not in definition.compatible_run_kinds:
                raise Conflict("resolved profile is incompatible with the Run kind")
            if params.schema_version not in definition.input_schema_ids:
                raise Conflict("resolved profile rejects the Run payload schema")
            if isinstance(resource_domain, DomainScope) and not set(
                resource_domain.domain_ids
            ).issubset(definition.domain_scope.domain_ids):
                raise Conflict(
                    "execution profile does not cover the resolved resource domain",
                    profile_id=binding.profile.profile_id,
                    profile_version=binding.profile.version,
                )

    def _resolve_resource_domain(
        self,
        *,
        base: Permission,
        params: RunKindPayload,
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
        registry: DomainRegistryV1,
        validation_item: ApprovalItem | None,
        verified_suite_scope: DomainScope | None,
    ) -> DomainScopeValue:
        if base.domain_scope != "all":
            return base.domain_scope
        memo: dict[str, DomainScope] = {}
        traversal = _LegacyDomainTraversalContext()

        def scope_for(artifact_id: str) -> DomainScope:
            return self._artifact_domain_scope(
                artifacts[artifact_id],
                read=read,
                registry=registry,
                memo=memo,
                visiting=set(),
                traversal=traversal,
            )

        if isinstance(
            params,
            (
                PatchRepairPayloadV1,
                PatchValidationPayloadV1,
                ConstraintValidationPayloadV1,
                RollbackValidationPayloadV1,
            ),
        ):
            # Validation subject domain is authoritative: the loaded ApprovalItem's
            # exact ``domain_scope`` (subject binding), never a client field.
            if validation_item is None:
                raise IntegrityViolation("Run admission lost its loaded subject domain")
            subject_scope = self._validate_domain_scope(validation_item.domain_scope, registry)
            for artifact in artifacts.values():
                if artifact.kind in {"cassette_bundle", "source_raw", "source_rendered"}:
                    continue
                actual_scope = self._artifact_domain_scope(
                    artifact,
                    read=read,
                    registry=registry,
                    memo=memo,
                    visiting=set(),
                    traversal=traversal,
                )
                self._require_scope_subset(
                    actual_scope,
                    subject_scope,
                    label="workflow subject",
                )
            return subject_scope

        if isinstance(params, GenerationProposePayloadV1):
            target = params.target.expected_ref
            if target is not None and target.artifact_id != params.base_snapshot_artifact_id:
                raise Conflict("generation target ref does not bind the exact base snapshot")
            declared = self._validate_domain_scope(params.domain_scope, registry)
            consumed_ids = [params.base_snapshot_artifact_id]
            if params.constraint_snapshot_artifact_id is not None:
                consumed_ids.append(params.constraint_snapshot_artifact_id)
            consumed_ids.extend(item.evidence_artifact_id for item in params.findings)
            for artifact_id in consumed_ids:
                self._require_scope_subset(
                    scope_for(artifact_id),
                    declared,
                    label="generation declaration",
                )
            return declared

        if isinstance(params, ConstraintProposalProposePayloadV1):
            declared = self._validate_domain_scope(params.domain_scope, registry)
            consumed_ids = [*params.source_artifact_ids]
            if params.base_constraint_snapshot_artifact_id is not None:
                consumed_ids.append(params.base_constraint_snapshot_artifact_id)
            for artifact_id in consumed_ids:
                self._require_scope_subset(
                    scope_for(artifact_id),
                    declared,
                    label="constraint proposal declaration",
                )
            return declared

        if isinstance(params, (ReviewRunPayloadV1, CheckerRunPayloadV1)):
            ids = [params.snapshot_artifact_id]
            if params.constraint_snapshot_artifact_id is not None:
                ids.append(params.constraint_snapshot_artifact_id)
            return self._union_domain_scopes(tuple(scope_for(artifact_id) for artifact_id in ids))

        if isinstance(params, SimulationRunPayloadV1):
            ids = [params.snapshot_artifact_id]
            if params.constraint_snapshot_artifact_id is not None:
                ids.append(params.constraint_snapshot_artifact_id)
            if params.scenario_artifact_id is not None:
                scenario_artifact = artifacts[params.scenario_artifact_id]
                scenario = self._load_json_artifact(
                    scenario_artifact,
                    read=read,
                    payload_schema_id="scenario-spec@1",
                    model=ScenarioSpecV1,
                )
                if (
                    not isinstance(scenario, ScenarioSpecV1)
                    or scenario.source_preview_artifact_id != params.snapshot_artifact_id
                    or scenario.constraint_snapshot_artifact_id
                    != params.constraint_snapshot_artifact_id
                    or scenario_artifact.version_tuple.ir_snapshot_id
                    != artifacts[params.snapshot_artifact_id].version_tuple.ir_snapshot_id
                ):
                    raise Conflict("simulation scenario does not bind the exact inputs")
                ids.append(params.scenario_artifact_id)
            return self._union_domain_scopes(tuple(scope_for(artifact_id) for artifact_id in ids))

        if isinstance(params, TaskSuiteDerivePayloadV1):
            return self._union_domain_scopes(
                tuple(
                    self._artifact_domain_scope(
                        artifacts[artifact_id],
                        read=read,
                        registry=registry,
                        memo=memo,
                        visiting=set(),
                        traversal=traversal,
                    )
                    for artifact_id in (
                        params.source_preview_artifact_id,
                        params.config_artifact_id,
                        params.constraint_snapshot_artifact_id,
                    )
                )
            )

        if isinstance(params, PlaytestRunPayloadV1):
            if verified_suite_scope is None:
                raise IntegrityViolation("Playtest domain resolver lacks a verified TaskSuite")
            ids = (
                params.config_artifact_id,
                params.constraint_snapshot_artifact_id,
                params.task_suite_artifact_id,
                *(item.scenario_spec_artifact_id for item in params.episodes),
            )
            return self._union_domain_scopes(
                (
                    self._validate_domain_scope(verified_suite_scope, registry),
                    *(scope_for(artifact_id) for artifact_id in ids),
                )
            )

        if isinstance(params, BenchRunPayloadV1):
            dataset_scope = self._artifact_domain_scope(
                artifacts[params.dataset_artifact_id],
                read=read,
                registry=registry,
                memo=memo,
                visiting=set(),
                traversal=traversal,
            )
            for artifact_id in (
                params.benchmark_spec_artifact_id,
                *params.case_result_artifact_ids,
            ):
                scope = self._artifact_domain_scope(
                    artifacts[artifact_id],
                    read=read,
                    registry=registry,
                    memo=memo,
                    visiting=set(),
                    traversal=traversal,
                )
                self._require_scope_subset(scope, dataset_scope, label="bench input")
            return dataset_scope

        if isinstance(params, ArtifactMigrationPayloadV1):
            return self._artifact_domain_scope(
                artifacts[params.source_artifact_id],
                read=read,
                registry=registry,
                memo=memo,
                visiting=set(),
                traversal=traversal,
            )
        raise IntegrityViolation("Run kind has no resource-domain resolver implementation")

    def _artifact_domain_scope(
        self,
        artifact: ArtifactV2,
        *,
        read: AdmissionReadPort,
        registry: DomainRegistryV1,
        memo: dict[str, DomainScope],
        visiting: set[str],
        traversal: _LegacyDomainTraversalContext | None = None,
    ) -> DomainScope:
        traversal = traversal or _LegacyDomainTraversalContext()
        root_id = artifact.artifact_id
        retained = memo.get(root_id)
        if retained is not None:
            return retained
        if root_id in visiting:
            raise IntegrityViolation("Artifact domain lineage contains a cycle")

        initial_visiting = set(visiting)
        declared_scopes: dict[str, DomainScope | None] = {}
        parents_by_artifact: dict[str, tuple[ArtifactV2, ...]] = {}
        stack: list[tuple[ArtifactV2, bool]] = [(artifact, False)]
        try:
            while stack:
                current, expanded = stack.pop()
                current_id = current.artifact_id
                if current_id in memo:
                    continue
                if not expanded:
                    if current_id in visiting:
                        raise IntegrityViolation("Artifact domain lineage contains a cycle")
                    visiting.add(current_id)
                    declared = self._declared_artifact_domain_scope(
                        current,
                        read=read,
                        registry=registry,
                    )
                    declared_scopes[current_id] = declared
                    # Modern publishers bind the authoritative scope on the Artifact
                    # itself (either canonical metadata or a typed payload). Lineage
                    # is provenance, not an authorization delegation chain, so only
                    # legacy unscoped Artifacts need ancestry fallback.
                    if declared is not None:
                        memo[current_id] = declared
                        visiting.remove(current_id)
                        continue

                    traversal.charge_node(root_artifact_id=root_id)
                    traversal.charge_edges(
                        len(current.lineage),
                        root_artifact_id=root_id,
                    )
                    parents: list[ArtifactV2] = []
                    for parent_id in current.lineage:
                        parent = read.artifacts.get(parent_id)
                        if not isinstance(parent, ArtifactV2):
                            raise IntegrityViolation(
                                "Artifact domain lineage parent is unavailable",
                                artifact_id=current_id,
                                parent_artifact_id=parent_id,
                            )
                        # Authenticated/rendered prompt sources are intentionally
                        # domain-neutral; they may support a domain Artifact only
                        # alongside a content parent.
                        if (
                            parent.kind in {"source_raw", "source_rendered"}
                            and parent.meta.get("domain_scope") is None
                            and not parent.lineage
                        ):
                            continue
                        parents.append(parent)
                    parents_by_artifact[current_id] = tuple(parents)
                    stack.append((current, True))
                    for parent in reversed(parents):
                        if parent.artifact_id in memo:
                            continue
                        if parent.artifact_id in visiting:
                            raise IntegrityViolation("Artifact domain lineage contains a cycle")
                        stack.append((parent, False))
                    continue

                parents = parents_by_artifact[current_id]
                parent_scopes = tuple(memo[parent.artifact_id] for parent in parents)
                lineage_scope = self._union_domain_scopes(parent_scopes) if parent_scopes else None
                resolved = declared_scopes[current_id]
                if resolved is not None:  # modern scoped leaves never reach expansion
                    raise IntegrityViolation(
                        "Artifact domain lineage traversal expanded a scoped leaf",
                        artifact_id=current_id,
                    )
                if lineage_scope is None:
                    raise IntegrityViolation(
                        "Artifact has no authoritative resource-domain binding",
                        artifact_id=current_id,
                    )
                resolved = lineage_scope
                memo[current_id] = resolved
                visiting.remove(current_id)
        finally:
            visiting.intersection_update(initial_visiting)
        return memo[root_id]

    def _declared_artifact_domain_scope(
        self,
        artifact: ArtifactV2,
        *,
        read: AdmissionReadPort,
        registry: DomainRegistryV1,
    ) -> DomainScope | None:
        raw_scope = artifact.meta.get("domain_scope")
        explicit: DomainScope | None = None
        if raw_scope is not None:
            try:
                explicit = DomainScope.model_validate(raw_scope)
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("Artifact domain_scope metadata is invalid") from exc
            if raw_scope != explicit.model_dump(mode="json"):
                raise IntegrityViolation("Artifact domain_scope metadata is not canonical")
            explicit = self._validate_domain_scope(explicit, registry)

        typed: DomainScope | None = None
        schema = artifact.meta.get("payload_schema_id")
        if artifact.kind == "constraint_proposal" and schema == "constraint-proposal@1":
            from gameforge.contracts.workflow import ConstraintProposalV1

            payload = self._load_json_artifact(
                artifact,
                read=read,
                payload_schema_id="constraint-proposal@1",
                model=ConstraintProposalV1,
            )
            typed = self._validate_domain_scope(payload.domain_scope, registry)
        elif artifact.kind == "scenario_spec" and schema == "scenario-spec@1":
            payload = self._load_json_artifact(
                artifact,
                read=read,
                payload_schema_id="scenario-spec@1",
                model=ScenarioSpecV1,
            )
            typed = self._validate_domain_scope(payload.domain_scope, registry)
        elif artifact.kind == "task_suite" and schema == "task-suite@1":
            payload = self._load_json_artifact(
                artifact,
                read=read,
                payload_schema_id="task-suite@1",
                model=TaskSuiteV1,
            )
            typed = self._validate_domain_scope(
                self._union_domain_scopes(
                    tuple(episode.domain_scope for episode in payload.episodes)
                ),
                registry,
            )
        if explicit is not None and typed is not None and explicit != typed:
            raise IntegrityViolation("Artifact metadata and typed payload domains disagree")
        return explicit or typed

    @staticmethod
    def _union_domain_scopes(scopes: tuple[DomainScope, ...]) -> DomainScope:
        domain_ids = tuple(sorted({domain for scope in scopes for domain in scope.domain_ids}))
        if not domain_ids:
            raise IntegrityViolation("resource domain resolution produced an empty scope")
        return DomainScope(domain_ids=domain_ids)

    @staticmethod
    def _require_scope_subset(
        candidate: DomainScope,
        authority: DomainScope,
        *,
        label: str,
    ) -> None:
        if not set(candidate.domain_ids).issubset(authority.domain_ids):
            raise Conflict(f"resource domain exceeds the {label} domain")

    @staticmethod
    def _validate_domain_scope(
        scope: DomainScope,
        registry: DomainRegistryV1,
    ) -> DomainScope:
        active = {
            definition.domain_id
            for definition in registry.definitions
            if definition.status == "active"
        }
        if not set(scope.domain_ids).issubset(active):
            raise IntegrityViolation("resource domain is absent or inactive in the exact registry")
        return scope

    # ── exact mutable-ref and frozen VersionTuple closure ───────────────────
    @staticmethod
    def _expected_refs(params: RunKindPayload) -> tuple[tuple[str, RefValue | None], ...]:
        values: list[tuple[str, RefValue | None]] = []
        if isinstance(params, (GenerationProposePayloadV1, PatchRepairPayloadV1)):
            values.append((params.target.ref_name, params.target.expected_ref))
        elif isinstance(params, (PatchValidationPayloadV1, ConstraintValidationPayloadV1)):
            values.append((params.target.ref_name, params.target.expected_ref))
        elif isinstance(params, RollbackValidationPayloadV1):
            values.append((params.ref_name, params.expected_current_ref))

        by_name: dict[str, RefValue | None] = {}
        for name, expected in values:
            if name in by_name and by_name[name] != expected:
                raise IntegrityViolation("Run payload binds one ref name to conflicting values")
            by_name[name] = expected
        return tuple(sorted(by_name.items()))

    def _fresh_admission_guard(
        self,
        *,
        actor: ActorContext,
        authorization: _AuthorizationBinding,
        artifacts: dict[str, ArtifactV2],
        params: RunKindPayload,
        subject_item: ApprovalItem | None,
        source_writes: tuple[_SourceWrite, ...],
        replay_context: _ReplayAdmissionContext | None,
    ) -> Callable[[Any], None]:
        expected = self._expected_refs(params)
        pending_ids = {write.minted.artifact.artifact_id for write in source_writes}

        def guard(transaction: Any) -> None:
            current_principal = self._current_principal(transaction, actor)
            expected_security_projection = (
                actor.principal.id,
                actor.principal.kind,
                actor.principal.credential_epoch,
                actor.principal.status == "active",
            )
            current_security_projection = (
                None
                if not isinstance(current_principal, Principal)
                else (
                    current_principal.id,
                    current_principal.kind,
                    current_principal.credential_epoch,
                    current_principal.status == "active",
                )
            )
            if current_security_projection != expected_security_projection:
                raise Forbidden("authenticated principal changed before atomic Run admission")
            assert isinstance(current_principal, Principal)
            policies = getattr(transaction, "policies", None)
            if policies is None:
                raise IntegrityViolation("Run admission UoW has no policy authority")
            role_policy = policies.get_role_policy(
                authorization.role_policy.policy_version,
                authorization.role_policy.policy_digest,
            )
            if role_policy != authorization.role_policy:
                raise Conflict("Run admission role policy changed before atomic creation")
            registry = policies.get_domain_registry(role_policy.domain_registry_ref)
            if registry != authorization.domain_registry:
                raise Conflict("Run admission domain registry changed before atomic creation")
            for permission in authorization.permissions:
                if (
                    authorize(
                        principal=current_principal,
                        role_policy=role_policy,
                        requested_permission=permission,
                        domain_registry=registry,
                    )
                    is not AuthorizationDecision.ALLOW
                ):
                    raise Forbidden(
                        "principal authorization changed before atomic Run admission",
                        action=permission.action,
                        resource_kind=permission.resource_kind,
                    )

            artifact_repository = getattr(transaction, "artifacts", None)
            object_bindings = getattr(transaction, "object_bindings", None)
            if artifact_repository is None or object_bindings is None:
                raise IntegrityViolation("Run admission UoW lacks Artifact authorities")
            get_artifacts = getattr(artifact_repository, "get_many", None)
            resolve_bindings = getattr(object_bindings, "resolve_many", None)
            if not callable(get_artifacts) or not callable(resolve_bindings):
                raise IntegrityViolation("Run admission UoW lacks batch Artifact authorities")
            checked_artifacts = {
                artifact_id: artifact
                for artifact_id, artifact in artifacts.items()
                if artifact_id not in pending_ids
            }
            current_artifacts = get_artifacts(tuple(checked_artifacts))
            current_bindings = resolve_bindings(
                tuple(artifact.object_ref for artifact in checked_artifacts.values())
            )
            for artifact_id, artifact in checked_artifacts.items():
                if current_artifacts.get(artifact_id) != artifact:
                    raise Conflict(
                        "Run input Artifact changed before atomic admission",
                        artifact_id=artifact_id,
                    )
                binding = current_bindings.get(artifact.object_ref.key)
                if binding is None:
                    raise Conflict(
                        "Run input Artifact lost its active ObjectBinding",
                        artifact_id=artifact_id,
                    )
                if binding.object_ref != artifact.object_ref or binding.status != "active":
                    raise IntegrityViolation(
                        "Run input ObjectBinding differs from the exact Artifact"
                    )
            if replay_context is not None:
                replay_context.revalidate(
                    transaction,
                    explicit_legacy_decisions=self._legacy_import_decisions,
                    already_checked_artifact_ids=set(checked_artifacts),
                )
            if expected:
                refs = getattr(transaction, "refs", None)
                if refs is None:
                    raise IntegrityViolation("Run admission UoW has no ref authority")
                for ref_name, expected_ref in expected:
                    if refs.get(ref_name) != expected_ref:
                        raise Conflict(
                            "Run target ref changed during atomic admission",
                            ref_name=ref_name,
                        )
                if isinstance(params, RollbackValidationPayloadV1):
                    historical = refs.get_history_entry(
                        params.ref_name,
                        params.target_history_revision,
                    )
                    if historical != RefValue(
                        artifact_id=params.target_artifact_id,
                        revision=params.target_history_revision,
                    ):
                        raise Conflict("rollback target changed during atomic admission")
            if subject_item is not None:
                approvals = getattr(transaction, "approvals", None)
                if approvals is None:
                    raise IntegrityViolation("subject admission UoW has no approval authority")
                current = approvals.get(subject_item.approval_id)
                head = approvals.get_subject_head(subject_item.subject_series_id)
                if (
                    current != subject_item
                    or head is None
                    or head.current_approval_id != subject_item.approval_id
                    or head.current_subject_artifact_id != subject_item.subject_artifact_id
                    or head.revision
                    != (
                        params.subject.subject_head_revision
                        if isinstance(
                            params,
                            (
                                PatchValidationPayloadV1,
                                ConstraintValidationPayloadV1,
                                RollbackValidationPayloadV1,
                            ),
                        )
                        else getattr(params, "expected_subject_head_revision", head.revision)
                    )
                ):
                    raise Conflict("workflow subject changed during atomic admission")
            if source_writes:
                capabilities = self._source_capabilities(transaction)
                for write in source_writes:
                    capabilities.object_bindings.bind_verified(
                        write.minted.stored.ref,
                        write.minted.stored.location,
                        None,
                    )
                    capabilities.artifacts.put(write.minted.artifact)

        return guard

    def _freeze_version_tuple(
        self,
        *,
        params: RunKindPayload,
        artifacts: dict[str, ArtifactV2],
        execution_version_plan: ExecutionVersionPlanV1 | None,
        cassette_artifact_id: str | None,
        seed: int | None,
        producer_basis: VersionTuple,
    ) -> VersionTuple:
        """Project the complete applicable Run input basis from exact parents.

        This is an explicit role projection, not a mechanical merge of every input
        tuple. Only fields that causally define the Run's primary output are copied;
        equality peers are checked when the frozen lineage policy requires them.
        """

        values = producer_basis.model_dump(mode="python")

        def project(
            primary_id: str | None,
            fields: tuple[str, ...],
            *,
            equal_ids: tuple[str, ...] = (),
            label: str,
        ) -> None:
            if primary_id is None:
                return
            primary = artifacts.get(primary_id)
            if primary is None:
                raise IntegrityViolation(
                    "VersionTuple projection parent is absent from the exact input set",
                    role=label,
                    artifact_id=primary_id,
                )
            peers = []
            for artifact_id in equal_ids:
                peer = artifacts.get(artifact_id)
                if peer is None:
                    raise IntegrityViolation(
                        "VersionTuple equality parent is absent from the exact input set",
                        role=label,
                        artifact_id=artifact_id,
                    )
                peers.append(peer)
            for field in fields:
                projected = getattr(primary.version_tuple, field)
                if any(getattr(peer.version_tuple, field) != projected for peer in peers):
                    raise IntegrityViolation(
                        "Run input VersionTuple parents disagree",
                        role=label,
                        field=field,
                    )
                values[field] = projected

        if isinstance(params, (GenerationProposePayloadV1, PatchRepairPayloadV1)):
            project(
                params.base_snapshot_artifact_id,
                ("doc_version", "ir_snapshot_id"),
                label="base_snapshot",
            )
            project(
                params.constraint_snapshot_artifact_id,
                ("constraint_snapshot_id",),
                label="constraint",
            )
        elif isinstance(params, ConstraintProposalProposePayloadV1):
            # Only the design-document sources causally define ``doc_version``.
            # ``authoring_goal`` is an authenticated instruction source and is a
            # Run input for prompt closure, but it is intentionally absent from the
            # proposal's domain lineage/source_bindings.  Requiring its content hash
            # to equal the document revision makes every normal proposal impossible.
            source_ids = params.source_artifact_ids
            project(
                source_ids[0],
                ("doc_version",),
                equal_ids=tuple(source_ids[1:]),
                label="source",
            )
            project(
                params.base_constraint_snapshot_artifact_id,
                ("ir_snapshot_id", "constraint_snapshot_id"),
                label="base_constraint",
            )
        elif isinstance(params, (ReviewRunPayloadV1, CheckerRunPayloadV1)):
            project(
                params.snapshot_artifact_id,
                ("doc_version", "ir_snapshot_id"),
                label="snapshot",
            )
            project(
                params.constraint_snapshot_artifact_id,
                ("constraint_snapshot_id",),
                label="constraint",
            )
        elif isinstance(params, SimulationRunPayloadV1):
            equality = () if params.scenario_artifact_id is None else (params.scenario_artifact_id,)
            project(
                params.snapshot_artifact_id,
                ("doc_version",),
                label="snapshot",
            )
            project(
                params.snapshot_artifact_id,
                ("ir_snapshot_id",),
                equal_ids=equality,
                label="snapshot",
            )
            project(
                params.constraint_snapshot_artifact_id,
                ("constraint_snapshot_id",),
                equal_ids=equality,
                label="constraint",
            )
            project(
                params.scenario_artifact_id,
                ("env_contract_version",),
                label="scenario",
            )
        elif isinstance(params, TaskSuiteDerivePayloadV1):
            project(
                params.source_preview_artifact_id,
                ("doc_version",),
                equal_ids=(params.config_artifact_id,),
                label="preview",
            )
            project(
                params.source_preview_artifact_id,
                ("ir_snapshot_id",),
                equal_ids=(params.config_artifact_id,),
                label="preview",
            )
            project(
                params.constraint_snapshot_artifact_id,
                ("constraint_snapshot_id",),
                equal_ids=(params.config_artifact_id,),
                label="constraint",
            )
            project(
                params.config_artifact_id,
                ("env_contract_version",),
                label="config",
            )
        elif isinstance(params, PlaytestRunPayloadV1):
            peers = (
                params.task_suite_artifact_id,
                *(item.scenario_spec_artifact_id for item in params.episodes),
            )
            project(
                params.config_artifact_id,
                ("doc_version", "ir_snapshot_id", "env_contract_version"),
                equal_ids=peers,
                label="config",
            )
            project(
                params.constraint_snapshot_artifact_id,
                ("constraint_snapshot_id",),
                equal_ids=(params.config_artifact_id, *peers),
                label="constraint",
            )
        elif isinstance(params, PatchValidationPayloadV1):
            config_ids = params.candidate_config_export_artifact_ids
            project(
                params.preview_snapshot_artifact_id,
                ("doc_version",),
                label="target",
            )
            project(
                params.preview_snapshot_artifact_id,
                ("ir_snapshot_id",),
                equal_ids=config_ids,
                label="target",
            )
            constraint_source_id = (
                params.subject.subject_artifact_id
                if params.constraint_snapshot_artifact_id is None
                else params.constraint_snapshot_artifact_id
            )
            constraint_peers = (
                *(
                    ()
                    if params.constraint_snapshot_artifact_id is None
                    else (params.subject.subject_artifact_id,)
                ),
                *config_ids,
            )
            project(
                constraint_source_id,
                ("constraint_snapshot_id",),
                equal_ids=constraint_peers,
                label="constraint",
            )
            if config_ids:
                project(
                    config_ids[0],
                    ("env_contract_version",),
                    equal_ids=tuple(config_ids[1:]),
                    label="candidate_config",
                )
        elif isinstance(params, ConstraintValidationPayloadV1):
            project(
                params.subject.subject_artifact_id,
                ("doc_version", "ir_snapshot_id"),
                label="proposal",
            )
            project(
                params.base_constraint_snapshot_artifact_id,
                ("constraint_snapshot_id",),
                label="base_constraint",
            )
        elif isinstance(params, RollbackValidationPayloadV1):
            project(
                params.target_artifact_id,
                (
                    "doc_version",
                    "ir_snapshot_id",
                    "constraint_snapshot_id",
                    "env_contract_version",
                ),
                label="rollback_target",
            )
        elif isinstance(params, BenchRunPayloadV1):
            project(
                params.dataset_artifact_id,
                (
                    "doc_version",
                    "ir_snapshot_id",
                    "constraint_snapshot_id",
                    "env_contract_version",
                ),
                label="dataset",
            )
        elif isinstance(params, ArtifactMigrationPayloadV1):
            project(
                params.source_artifact_id,
                tuple(field for field in VersionTuple.model_fields if field != "tool_version"),
                label="migration_source",
            )

        if execution_version_plan is None:
            values.update(
                prompt_version=None,
                model_snapshot=None,
                agent_graph_version=None,
            )
        else:
            values.update(
                prompt_version=build_version_set_projection(
                    "prompt_version",
                    (node.prompt_version for node in execution_version_plan.nodes),
                ).tuple_value,
                model_snapshot=build_version_set_projection(
                    "model_snapshot",
                    (
                        model
                        for node in execution_version_plan.nodes
                        for model in node.allowed_model_snapshots
                    ),
                ).tuple_value,
                agent_graph_version=execution_version_plan.agent_graph_version,
            )

        if cassette_artifact_id is None:
            values["cassette_id"] = None
        else:
            cassette = artifacts[cassette_artifact_id]
            cassette_id = cassette.version_tuple.cassette_id
            if cassette_id != f"sha256:{cassette.payload_hash}":
                raise IntegrityViolation("replay cassette VersionTuple is not content-bound")
            values["cassette_id"] = cassette_id
        # Seed is producer-local execution identity.  A deterministic child must
        # clear a parent's historical seed rather than accidentally inheriting it.
        values["seed"] = seed
        return VersionTuple.model_validate(values)

    # ── exact input-set kind verification ────────────────────────────────────
    @staticmethod
    def _finding_bindings(params: RunKindPayload) -> tuple[FindingEvidenceBindingV1, ...]:
        if isinstance(
            params,
            (GenerationProposePayloadV1, PatchRepairPayloadV1),
        ):
            return params.findings
        if isinstance(params, PatchValidationPayloadV1):
            return (*params.expected_findings, *params.findings)
        return ()

    def _verify_finding_bindings(
        self,
        *,
        params: RunKindPayload,
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
    ) -> None:
        bindings = self._finding_bindings(params)
        target_evidence_ids = (
            tuple(
                sorted(
                    {
                        *params.review_artifact_ids,
                        *params.playtest_trace_artifact_ids,
                        *(item.evidence_artifact_id for item in params.findings),
                    }
                )
            )
            if isinstance(params, PatchValidationPayloadV1)
            else ()
        )
        if not bindings and not target_evidence_ids:
            return
        if read.findings is None or read.finding_links is None:
            raise DependencyUnavailable(
                "Finding admission authority is unavailable",
                component="finding_admission",
            )
        if isinstance(params, PatchValidationPayloadV1):
            self._verify_patch_target_finding_closure(
                params=params,
                evidence_artifact_ids=target_evidence_ids,
                read=read,
            )
        binding_scopes: list[tuple[tuple[FindingEvidenceBindingV1, ...], tuple[str, ...]]] = []
        if isinstance(params, GenerationProposePayloadV1):
            binding_scopes.append((params.findings, (params.base_snapshot_artifact_id,)))
        elif isinstance(params, PatchRepairPayloadV1):
            binding_scopes.append((params.findings, (params.preview_snapshot_artifact_id,)))
        elif isinstance(params, PatchValidationPayloadV1):
            subject = artifacts[params.subject.subject_artifact_id]
            historical_snapshot_ids = tuple(
                sorted(
                    parent_id
                    for parent_id in subject.lineage
                    if isinstance(
                        parent := artifacts.get(parent_id) or read.artifacts.get(parent_id),
                        ArtifactV2,
                    )
                    and parent.kind == "ir_snapshot"
                )
            )
            if params.expected_findings and not historical_snapshot_ids:
                raise Conflict("Patch expected Findings have no historical snapshot lineage")
            binding_scopes.extend(
                (
                    (params.expected_findings, historical_snapshot_ids),
                    (params.findings, (params.preview_snapshot_artifact_id,)),
                )
            )
        ancestry_cache: dict[tuple[str, tuple[str, ...]], bool] = {}
        ancestry_node_budget = [0]
        for scoped_bindings, expected_artifact_ids in binding_scopes:
            allowed_snapshot_ids: set[str] = set()
            for artifact_id in expected_artifact_ids:
                artifact = artifacts.get(artifact_id) or read.artifacts.get(artifact_id)
                if not isinstance(artifact, ArtifactV2):
                    raise Conflict(
                        "Finding subject snapshot Artifact is unavailable",
                        artifact_id=artifact_id,
                    )
                snapshot_id = artifact.version_tuple.ir_snapshot_id
                if snapshot_id is None:
                    raise IntegrityViolation(
                        "Finding subject Artifact omits its IR snapshot identity"
                    )
                allowed_snapshot_ids.add(snapshot_id)
            for binding in scoped_bindings:
                self._verify_one_finding_binding(
                    binding=binding,
                    expected_artifact_ids=expected_artifact_ids,
                    allowed_snapshot_ids=allowed_snapshot_ids,
                    artifacts=artifacts,
                    read=read,
                    ancestry_cache=ancestry_cache,
                    ancestry_node_budget=ancestry_node_budget,
                )

    @staticmethod
    def _verify_patch_target_finding_closure(
        *,
        params: PatchValidationPayloadV1,
        evidence_artifact_ids: tuple[str, ...],
        read: AdmissionReadPort,
    ) -> None:
        """Reject caller omission of Findings atomically linked to evidence."""

        if not evidence_artifact_ids:
            if params.findings:
                raise IntegrityViolation("Patch target Finding evidence closure is empty")
            return
        list_links = getattr(
            read.finding_links,
            "list_finding_links_by_evidence_artifact_ids",
            None,
        )
        if not callable(list_links):
            raise DependencyUnavailable(
                "Finding evidence-link enumeration authority is unavailable",
                component="finding_admission",
            )
        enumerated_result = list_links(
            evidence_artifact_ids,
            max_items=MAX_COLLECTION_ITEMS,
        )
        try:
            enumerated = iter(enumerated_result)
        except TypeError as exc:
            raise IntegrityViolation(
                "Finding evidence-link enumeration returned a non-iterable result"
            ) from exc
        links = tuple(islice(enumerated, MAX_COLLECTION_ITEMS + 1))
        if len(links) > MAX_COLLECTION_ITEMS:
            raise IntegrityViolation(
                "Finding evidence-link closure exceeds its contract bound",
                maximum=MAX_COLLECTION_ITEMS,
            )
        if any(not isinstance(link, RunFindingLinkV1) for link in links):
            raise IntegrityViolation("Finding evidence-link enumeration returned an invalid link")
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
            raise IntegrityViolation("Finding evidence-link enumeration is not canonically ordered")
        selected_evidence_ids = frozenset(evidence_artifact_ids)
        linked_keys: list[tuple[str, str, int, str]] = []
        seen_revisions: set[tuple[str, int]] = set()
        for link in links:
            if link.evidence_artifact_id not in selected_evidence_ids:
                raise IntegrityViolation(
                    "Finding enumeration returned an unrequested evidence Artifact"
                )
            identity = (link.finding_id, link.finding_revision)
            if identity in seen_revisions:
                raise IntegrityViolation(
                    "selected evidence repeats an immutable Finding revision",
                    finding_id=link.finding_id,
                    finding_revision=link.finding_revision,
                )
            seen_revisions.add(identity)
            revision = read.findings.get(link.finding_id, link.finding_revision)
            if not isinstance(revision, FindingRevisionV1):
                raise IntegrityViolation(
                    "evidence link names an unavailable Finding revision",
                    finding_id=link.finding_id,
                    finding_revision=link.finding_revision,
                )
            digest = finding_revision_digest(revision)
            if not compare_digest(digest, link.finding_digest):
                raise IntegrityViolation(
                    "evidence link digest differs from its Finding revision",
                    finding_id=link.finding_id,
                    finding_revision=link.finding_revision,
                )
            linked_keys.append(
                (
                    link.evidence_artifact_id,
                    link.finding_id,
                    link.finding_revision,
                    link.finding_digest,
                )
            )
        provided_keys = tuple(
            sorted(
                (
                    item.evidence_artifact_id,
                    item.finding_id,
                    item.finding_revision,
                    item.finding_digest,
                )
                for item in params.findings
            )
        )
        if provided_keys != tuple(sorted(linked_keys)):
            raise Conflict("Patch target Finding bindings do not exactly cover selected evidence")

    @staticmethod
    def _finding_evidence_descends_from_subject(
        *,
        evidence: ArtifactV2,
        expected_artifact_ids: tuple[str, ...],
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
        ancestry_node_budget: list[int],
    ) -> bool:
        """Prove subject ancestry through trusted immutable Artifact envelopes.

        Playtest evidence is intentionally several hops away from its source IR
        (trace -> config -> preview).  A VersionTuple match alone would trust a
        producer-asserted field, while requiring a direct parent rejects that valid
        topology.  Traverse retained ``ArtifactV2.lineage`` instead, with fixed node
        and depth bounds and an active-path cycle guard.  The admitted subject is a
        trust boundary: once reached, its older history is irrelevant here.
        """

        expected = frozenset(expected_artifact_ids)
        if not expected:
            return False
        # (artifact id, depth, exit marker).  The exit marker implements iterative
        # DFS so a diamond is accepted but a back-edge on the active path is not.
        stack: list[tuple[str, int, bool]] = [(evidence.artifact_id, 0, False)]
        active: set[str] = set()
        complete: set[str] = set()
        loaded: dict[str, ArtifactV2] = {evidence.artifact_id: evidence, **artifacts}
        found_subject = False
        while stack:
            artifact_id, depth, exiting = stack.pop()
            if exiting:
                active.discard(artifact_id)
                complete.add(artifact_id)
                continue
            if artifact_id in complete:
                continue
            if artifact_id in active:
                raise IntegrityViolation("Finding evidence lineage contains a cycle")
            if depth > _MAX_FINDING_EVIDENCE_ANCESTRY_DEPTH:
                raise IntegrityViolation(
                    "Finding evidence ancestry exceeds its depth bound",
                    maximum=_MAX_FINDING_EVIDENCE_ANCESTRY_DEPTH,
                )
            ancestry_node_budget[0] += 1
            if ancestry_node_budget[0] > _MAX_FINDING_EVIDENCE_ANCESTRY_NODES:
                raise IntegrityViolation(
                    "Finding evidence ancestry exceeds its node bound",
                    maximum=_MAX_FINDING_EVIDENCE_ANCESTRY_NODES,
                )
            if artifact_id in expected:
                found_subject = True
                complete.add(artifact_id)
                continue
            artifact = loaded.get(artifact_id)
            if artifact is None:
                retained = read.artifacts.get(artifact_id)
                if not isinstance(retained, ArtifactV2):
                    raise Conflict(
                        "Finding evidence ancestry Artifact is unavailable",
                        artifact_id=artifact_id,
                    )
                if retained.artifact_id != artifact_id:
                    raise IntegrityViolation(
                        "Finding evidence ancestry returned another Artifact envelope"
                    )
                artifact = retained
                loaded[artifact_id] = retained
            active.add(artifact_id)
            stack.append((artifact_id, depth, True))
            stack.extend((parent_id, depth + 1, False) for parent_id in reversed(artifact.lineage))
        return found_subject

    @staticmethod
    def _verify_one_finding_binding(
        *,
        binding: FindingEvidenceBindingV1,
        expected_artifact_ids: tuple[str, ...],
        allowed_snapshot_ids: set[str],
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
        ancestry_cache: dict[tuple[str, tuple[str, ...]], bool],
        ancestry_node_budget: list[int],
    ) -> None:
        revision = read.findings.get(binding.finding_id, binding.finding_revision)
        if not isinstance(revision, FindingRevisionV1):
            raise Conflict(
                "Finding revision is unavailable",
                finding_id=binding.finding_id,
                finding_revision=binding.finding_revision,
            )
        digest = finding_revision_digest(revision)
        if not compare_digest(digest, binding.finding_digest):
            raise Conflict(
                "Finding digest differs from the retained revision",
                finding_id=binding.finding_id,
                finding_revision=binding.finding_revision,
            )
        link = read.finding_links.get_finding_link_by_revision(
            run_id=revision.payload.producer_run_id,
            finding_id=binding.finding_id,
            finding_revision=binding.finding_revision,
        )
        if link is None:
            raise Conflict(
                "Finding revision has no retained producer Run link",
                finding_id=binding.finding_id,
                finding_revision=binding.finding_revision,
            )
        if link.run_id != revision.payload.producer_run_id or not compare_digest(
            link.finding_digest, digest
        ):
            raise IntegrityViolation("retained Finding link disagrees with its Finding revision")
        if (
            link.evidence_artifact_id != binding.evidence_artifact_id
            or link.finding_digest != binding.finding_digest
            or binding.evidence_artifact_id not in artifacts
        ):
            raise Conflict(
                "Finding evidence binding differs from the retained producer link",
                finding_id=binding.finding_id,
                finding_revision=binding.finding_revision,
            )
        evidence = artifacts[binding.evidence_artifact_id]
        expected_schema = {
            "review_report": "review@1",
            "checker_run": "checker-report@1",
            "simulation_run": "simulation-result@1",
            "playtest_trace": "playtest-trace@1",
            "validation_evidence": "evidence-set@1",
            "regression_evidence": "regression-evidence@1",
        }.get(evidence.kind)
        ancestry_key = (evidence.artifact_id, expected_artifact_ids)
        if ancestry_key in ancestry_cache:
            subject_ancestry = ancestry_cache[ancestry_key]
        else:
            subject_ancestry = RunAdmissionEngine._finding_evidence_descends_from_subject(
                evidence=evidence,
                expected_artifact_ids=expected_artifact_ids,
                artifacts=artifacts,
                read=read,
                ancestry_node_budget=ancestry_node_budget,
            )
            ancestry_cache[ancestry_key] = subject_ancestry
        if (
            revision.payload.snapshot_id not in allowed_snapshot_ids
            or evidence.version_tuple.ir_snapshot_id != revision.payload.snapshot_id
            or not subject_ancestry
            or expected_schema is None
            or evidence.meta.get("payload_schema_id") != expected_schema
        ):
            raise Conflict(
                "Finding revision/evidence does not bind an admitted subject snapshot",
                finding_id=binding.finding_id,
                finding_revision=binding.finding_revision,
            )

    def _verify_prompt_goal_binding(
        self,
        *,
        params: RunKindPayload,
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
        actor: ActorContext,
        pending_sources: tuple[_SourceWrite, ...],
    ) -> None:
        if isinstance(params, GenerationProposePayloadV1):
            binding = params.objective_goal
        elif isinstance(params, ConstraintProposalProposePayloadV1):
            binding = params.authoring_goal
        else:
            return
        artifact = artifacts[binding.source_artifact_id]
        if (
            not compare_digest(artifact.payload_hash, binding.expected_payload_hash)
            or artifact.kind != "source_raw"
            or artifact.meta.get("payload_schema_id") != "source-raw@1"
            or artifact.lineage
            or artifact.version_tuple != VersionTuple(doc_version=binding.expected_payload_hash)
        ):
            raise IntegrityViolation("Prompt goal binding differs from its exact source Artifact")
        raw_provenance = artifact.meta.get("provenance")
        try:
            provenance = ProvenanceV1.model_validate(raw_provenance)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Prompt goal provenance is invalid") from exc
        expected = self._goal_writer.policy.assign(
            actor=actor,
            source_hash=artifact.payload_hash,
        )
        if provenance != expected:
            raise IntegrityViolation(
                "Prompt goal provenance differs from the authenticated actor authority"
            )
        pending_ids = {write.minted.artifact.artifact_id for write in pending_sources}
        if artifact.artifact_id in pending_ids:
            return
        blob = self._load_artifact_blob(artifact, read=read)
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrityViolation("Prompt goal source is not UTF-8") from exc
        if not text:
            raise IntegrityViolation("Prompt goal source is empty")

    def _verify_input_artifacts(
        self,
        *,
        params: RunKindPayload,
        cassette_artifact_id: str | None,
        read: AdmissionReadPort,
        pending_sources: tuple[_SourceWrite, ...] = (),
    ) -> dict[str, ArtifactV2]:
        pending: dict[str, ArtifactV2] = {}
        for write in pending_sources:
            artifact = write.minted.artifact
            retained = pending.get(artifact.artifact_id)
            if retained is not None and retained != artifact:
                raise IntegrityViolation("pending source id binds different immutable content")
            pending[artifact.artifact_id] = artifact

        resolved: dict[str, ArtifactV2] = {}
        for artifact_id, allowed in self._input_kind_requirements(params):
            artifact = pending.get(artifact_id)
            if artifact is None:
                artifact = read.artifacts.get(artifact_id)
            if not isinstance(artifact, ArtifactV2):
                raise Conflict("Run input Artifact is unavailable", artifact_id=artifact_id)
            if allowed and artifact.kind not in allowed:
                raise Conflict(
                    "Run input Artifact kind is not allowed for this field",
                    artifact_id=artifact_id,
                    kind=artifact.kind,
                )
            allowed_schemas = ARTIFACT_PAYLOAD_SCHEMAS.get(artifact.kind)
            if (
                allowed_schemas is None
                or artifact.meta.get("payload_schema_id") not in allowed_schemas
            ):
                raise IntegrityViolation(
                    "Run input Artifact payload schema is not allowed for its kind",
                    artifact_id=artifact_id,
                    kind=artifact.kind,
                    payload_schema_id=artifact.meta.get("payload_schema_id"),
                )
            resolved[artifact_id] = artifact
        if cassette_artifact_id is not None:
            cassette = read.artifacts.get(cassette_artifact_id)
            if not isinstance(cassette, ArtifactV2):
                raise Conflict(
                    "replay cassette Artifact is unavailable",
                    artifact_id=cassette_artifact_id,
                )
            if cassette.kind != "cassette_bundle":
                raise Conflict(
                    "replay cassette Artifact kind is not allowed",
                    artifact_id=cassette_artifact_id,
                    kind=cassette.kind,
                )
            if cassette.meta.get("payload_schema_id") != "cassette-bundle@1":
                raise IntegrityViolation("replay cassette does not bind a closed bundle")
            resolved[cassette_artifact_id] = cassette
        expected_ids = set(referenced_input_artifact_ids(params))
        if cassette_artifact_id is not None:
            expected_ids.add(cassette_artifact_id)
        if set(resolved) != expected_ids:
            raise IntegrityViolation("verified Run inputs differ from the exact envelope set")
        if set(pending) - expected_ids:
            raise IntegrityViolation("pending source is not part of the exact Run input set")
        return resolved

    def _verify_task_suite_and_playtest_bindings(
        self,
        *,
        params: RunKindPayload,
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        catalog: ExecutionProfileCatalogSnapshotV1 | None = None,
    ) -> DomainScope | None:
        exact_catalog = self._catalog if catalog is None else catalog
        if isinstance(params, TaskSuiteDerivePayloadV1):
            self._verify_task_suite_derivation(
                params=params,
                artifacts=artifacts,
                read=read,
                catalog=exact_catalog,
            )
        elif isinstance(params, PlaytestRunPayloadV1):
            return self._verify_playtest_admission(
                params=params,
                artifacts=artifacts,
                read=read,
                llm_execution_mode=llm_execution_mode,
                catalog=exact_catalog,
            )
        return None

    def _verify_benchmark_admission(
        self,
        *,
        params: RunKindPayload,
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
        run_definition: RunKindDefinition,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        execution_version_plan: ExecutionVersionPlanV1 | None,
        cassette_artifact_id: str | None,
        seed: int | None = None,
        catalog: ExecutionProfileCatalogSnapshotV1 | None = None,
    ) -> None:
        """Close Bench mode/config over the typed immutable benchmark spec.

        The selected partitions' enumerated case modes are authoritative.  A client
        cannot turn model execution on or off with a detached boolean, and a spec
        cannot be reused against another dataset revision.
        """

        if not isinstance(params, BenchRunPayloadV1):
            return
        dataset_artifact = artifacts[params.dataset_artifact_id]
        spec_artifact = artifacts[params.benchmark_spec_artifact_id]
        dataset = self._load_json_artifact(
            dataset_artifact,
            read=read,
            payload_schema_id="bench-dataset@1",
            model=BenchmarkDatasetV1,
        )
        spec = self._load_json_artifact(
            spec_artifact,
            read=read,
            payload_schema_id="benchmark-spec@1",
            model=BenchmarkSpecV1,
        )
        if (
            spec.dataset.artifact_id != dataset_artifact.artifact_id
            or not compare_digest(spec.dataset.payload_hash, dataset_artifact.payload_hash)
            or spec.dataset.payload_schema_id != dataset_artifact.meta.get("payload_schema_id")
        ):
            raise Conflict("benchmark spec does not bind the exact dataset Artifact")
        if set(spec_artifact.lineage) != {dataset_artifact.artifact_id}:
            raise Conflict("benchmark spec lineage does not bind exactly one dataset")
        for field in (
            "doc_version",
            "ir_snapshot_id",
            "constraint_snapshot_id",
            "env_contract_version",
        ):
            if getattr(spec_artifact.version_tuple, field) != getattr(
                dataset_artifact.version_tuple, field
            ):
                raise Conflict(
                    "benchmark spec VersionTuple differs from its exact dataset",
                    field=field,
                )
        if spec.evaluator_profile != params.evaluator_profile:
            raise Conflict("benchmark evaluator profile differs from the typed spec")
        exact_catalog = self._catalog if catalog is None else catalog
        evaluator_definition = next(
            (item for item in exact_catalog.definitions if item.profile == spec.evaluator_profile),
            None,
        )
        if evaluator_definition is None or evaluator_definition.profile_kind != "bench_evaluator":
            raise Conflict("benchmark evaluator profile kind is incompatible")
        try:
            evaluator_config = BenchmarkEvaluatorProfileConfigV1.model_validate(
                evaluator_definition.config
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("benchmark evaluator profile config is invalid") from exc
        evaluator_policy = evaluator_config.policy
        if spec.evaluator_policy != evaluator_policy.ref:
            raise Conflict("benchmark evaluator policy differs from the typed spec")
        if (
            spec.resource_limits.max_case_executions > evaluator_policy.max_case_executions
            or spec.resource_limits.max_case_executions > MAX_BENCHMARK_CASE_EXECUTIONS
            or spec.resource_limits.max_prepared_report_bytes
            > evaluator_policy.max_prepared_report_bytes
            or spec.resource_limits.max_prepared_report_bytes > MAX_BENCHMARK_REPORT_BYTES
            or spec.resource_limits.max_aggregate_input_bytes_per_artifact
            > evaluator_policy.max_aggregate_input_bytes_per_artifact
            or spec.resource_limits.max_aggregate_input_bytes_per_artifact
            > MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT
            or spec.resource_limits.max_aggregate_input_bytes_total
            > evaluator_policy.max_aggregate_input_bytes_total
            or spec.resource_limits.max_aggregate_input_bytes_total
            > MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_TOTAL
            or spec.resource_limits.max_checker_work_units_total
            > evaluator_policy.max_checker_work_units_total
            or spec.resource_limits.max_checker_work_units_total > MAX_BENCHMARK_CHECKER_WORK_UNITS
            or spec.resource_limits.max_simulation_work_units_total
            > evaluator_policy.max_simulation_work_units_total
            or spec.resource_limits.max_simulation_work_units_total
            > MAX_BENCHMARK_SIMULATION_WORK_UNITS
            or spec.resource_limits.max_result_metrics_bytes_total
            > evaluator_policy.max_result_metrics_bytes_total
            or spec.resource_limits.max_result_metrics_bytes_total
            > MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL
            or spec.resource_limits.max_agent_model_calls_total
            > evaluator_policy.max_agent_model_calls_total
            or spec.resource_limits.max_agent_model_calls_total
            > MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL
        ):
            raise Conflict("benchmark resource limits exceed the evaluator policy")
        template_bytes = dataset.report_template_utf8.encode("utf-8")
        if len(template_bytes) > spec.resource_limits.max_prepared_report_bytes:
            raise Conflict("benchmark report template exceeds the spec byte limit")

        dataset_shape = tuple(
            (
                partition.partition_id,
                tuple((case.case_id, case.execution_mode) for case in partition.cases),
            )
            for partition in dataset.partitions
        )
        spec_shape = tuple(
            (
                partition.partition_id,
                tuple((case.case_id, case.execution_mode) for case in partition.cases),
            )
            for partition in spec.partitions
        )
        if dataset_shape != spec_shape:
            raise Conflict("benchmark dataset and spec case authority differ")
        if tuple(item.metric for item in dataset.binary_metrics) != spec.metric_policy.metrics:
            raise Conflict("benchmark dataset and spec metric authority differ")
        sampling = spec.sampling_policy
        if sampling.seed_derivation_version != run_definition.seed_derivation_version:
            raise Conflict(
                "benchmark sampling seed derivation differs from the Run kind",
                expected_seed_derivation_version=run_definition.seed_derivation_version,
                actual_seed_derivation_version=sampling.seed_derivation_version,
            )
        if (
            not sampling.minimum_repetitions
            <= params.repetition_count
            <= sampling.maximum_repetitions
        ):
            raise Conflict("benchmark repetition count violates the sampling policy")
        if (
            sampling.strategy == "seeded_without_replacement"
            and not evaluator_definition.stochastic
        ):
            raise Conflict("seeded benchmark sampling requires a stochastic evaluator profile")
        if evaluator_definition.stochastic != (seed is not None):
            if evaluator_definition.stochastic:
                raise Conflict("stochastic benchmark evaluation requires a root seed")
            raise Conflict("deterministic benchmark evaluation forbids a root seed")

        try:
            sampled = sampled_partition_cases(
                spec,
                params.partition_ids,
                root_seed=seed,
            )
        except KeyError as exc:
            raise Conflict(
                "benchmark partition selection is absent from the typed spec",
                partition_ids=tuple(exc.args[0]),
            ) from exc
        except ValueError as exc:
            raise Conflict("benchmark sampling policy cannot resolve this Run") from exc
        case_execution_count = len(sampled) * params.repetition_count
        if (
            case_execution_count > spec.resource_limits.max_case_executions
            or case_execution_count > evaluator_policy.max_case_executions
            or case_execution_count > MAX_BENCHMARK_CASE_EXECUTIONS
        ):
            raise Conflict("benchmark case replications exceed frozen resource limits")
        has_agent_cases = any(case.execution_mode == "agent" for _, case in sampled)
        dataset_cases = {
            case.case_id: case for partition in dataset.partitions for case in partition.cases
        }
        if params.execution_scope == "execute_cases":
            total_agent_model_calls = sum(
                len(dataset_cases[sampled_case.case_id].executor.prompts) * params.repetition_count
                for _, sampled_case in sampled
                if dataset_cases[sampled_case.case_id].execution_mode == "agent"
            )
            if total_agent_model_calls > spec.resource_limits.max_agent_model_calls_total:
                raise Conflict("benchmark Agent calls exceed the typed Run-total limit")
        total_checker_work_units = 0
        total_simulation_work_units = 0
        for _, sampled_case in sampled:
            dataset_case = dataset_cases[sampled_case.case_id]
            executor = dataset_case.executor
            snapshot = None
            constraints = getattr(executor, "constraints", ())
            if constraints:
                if (
                    executor.max_checker_work_units
                    > spec.resource_limits.max_checker_work_units_total
                ):
                    raise Conflict("benchmark checker work limit exceeds the typed spec")
                try:
                    snapshot = snapshot_from_canonical_view(executor.snapshot_payload)
                    per_replication_checker_work = validate_checker_work_budget(
                        snapshot=snapshot,
                        execution_count=len(constraints),
                        max_work_units=executor.max_checker_work_units,
                    )
                except (IntegrityViolation, TypeError, ValueError, KeyError) as exc:
                    raise Conflict(
                        "benchmark checker workload exceeds its frozen work budget"
                    ) from exc
                total_checker_work_units += per_replication_checker_work * params.repetition_count
                if total_checker_work_units > spec.resource_limits.max_checker_work_units_total:
                    raise Conflict("benchmark checkers exceed the typed Run-total work budget")
            simulation = getattr(executor, "simulation", None)
            if simulation is None:
                if dataset_case.execution_mode == "deterministic" and params.repetition_count != 1:
                    raise Conflict("pure deterministic benchmark cases require one repetition")
                continue
            if simulation.max_work_units > spec.resource_limits.max_simulation_work_units_total:
                raise Conflict("benchmark simulation work limit exceeds the typed spec")
            try:
                if snapshot is None:
                    snapshot = snapshot_from_canonical_view(executor.snapshot_payload)
                model = EconomyModel.from_snapshot(snapshot)
                per_replication_work = validate_economy_simulation_work_budget(
                    model,
                    n_agents=simulation.agents,
                    n_ticks=simulation.ticks,
                    replication_count=1,
                    max_work_units=simulation.max_work_units,
                )
            except (IntegrityViolation, TypeError, ValueError, KeyError) as exc:
                raise Conflict(
                    "benchmark simulation workload exceeds its frozen work budget"
                ) from exc
            total_simulation_work_units += per_replication_work * params.repetition_count
            if total_simulation_work_units > spec.resource_limits.max_simulation_work_units_total:
                raise Conflict("benchmark simulations exceed the typed Run-total work budget")
            if simulation.seed_policy == "run_subseed":
                if not evaluator_definition.stochastic or seed is None:
                    raise Conflict("run-subseed benchmark simulation requires a stochastic profile")
            elif params.repetition_count != 1:
                raise Conflict("fixed-seed benchmark simulation requires a one-shot Run")

        if params.execution_scope == "aggregate_results":
            if spec.aggregate_repetition_count != params.repetition_count:
                raise Conflict("benchmark aggregate repetition count differs from the typed spec")
            selected_case_ids = {case.case_id for _, case in sampled}
            expected_bindings = tuple(
                item for item in spec.aggregate_inputs if item.case_id in selected_case_ids
            )
            expected_ids = {item.artifact_id for item in expected_bindings}
            if len(expected_bindings) != case_execution_count or expected_ids != set(
                params.case_result_artifact_ids
            ):
                raise Conflict("benchmark aggregate inputs differ from exact spec bindings")
            if any(binding.root_seed != seed for binding in expected_bindings):
                raise Conflict(
                    "benchmark aggregate root seed differs from immutable input provenance"
                )
            if any(
                item.payload_size_bytes
                > spec.resource_limits.max_aggregate_input_bytes_per_artifact
                for item in expected_bindings
            ) or sum(item.payload_size_bytes for item in expected_bindings) > (
                spec.resource_limits.max_aggregate_input_bytes_total
            ):
                raise Conflict("benchmark aggregate inputs exceed spec byte limits")
            for binding in expected_bindings:
                artifact = artifacts[binding.artifact_id]
                if (
                    artifact.kind != binding.artifact_kind
                    or artifact.meta.get("payload_schema_id") != binding.payload_schema_id
                    or not compare_digest(artifact.payload_hash, binding.payload_hash)
                    or artifact.object_ref.size_bytes != binding.payload_size_bytes
                ):
                    raise Conflict(
                        "benchmark aggregate Artifact differs from its exact binding",
                        artifact_id=binding.artifact_id,
                    )
                try:
                    validate_benchmark_aggregate_producer_seed_authority(
                        binding,
                        dataset_cases[binding.case_id],
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise Conflict(
                        "benchmark aggregate producer seed differs from dataset authority",
                        artifact_id=binding.artifact_id,
                    ) from exc
                self._verify_benchmark_aggregate_producer(
                    binding=binding,
                    artifact=artifact,
                    read=read,
                )
                self._load_artifact_blob(artifact, read=read)
            requires_model = False
        else:
            requires_model = has_agent_cases
        if requires_model:
            if llm_execution_mode not in {"live", "record", "replay"}:
                raise Conflict("selected benchmark partitions contain Agent cases")
            if execution_version_plan is None:
                raise Conflict("Agent benchmark cases require an execution version plan")
            if (llm_execution_mode == "replay") != (cassette_artifact_id is not None):
                raise Conflict("benchmark replay mode requires exactly one cassette bundle")
        elif (
            llm_execution_mode != "not_applicable"
            or execution_version_plan is not None
            or cassette_artifact_id is not None
        ):
            raise Conflict("selected benchmark operation is deterministic-only")

    def _verify_benchmark_aggregate_producer(
        self,
        *,
        binding: BenchmarkAggregateInputBindingV1,
        artifact: ArtifactV2,
        read: AdmissionReadPort,
    ) -> None:
        """Authenticate one aggregate input through its source Run and RunResult."""

        if read.runs is None:
            raise DependencyUnavailable(
                "benchmark producer Run authority is unavailable",
                component="benchmark_producer_run",
            )
        run = read.runs.get(binding.producer_run_id)
        definition = self._registry.get_run_kind(binding.producer_run_kind)
        if (
            not isinstance(run, RunRecord)
            or run.run_id != binding.producer_run_id
            or run.status != "succeeded"
            or run.kind != binding.producer_run_kind
            or not compare_digest(run.payload_hash, binding.producer_run_payload_hash)
            or run.current_attempt_no != binding.producer_attempt_no
            or run.result_artifact_id != binding.producer_result_artifact_id
            or run.payload.seed != binding.producer_root_seed
            or run.payload.resolved_profiles != binding.producer_resolved_profiles
            or definition is None
            or definition.seed_derivation_version != binding.producer_seed_derivation_version
            or (
                binding.producer_seed_binding.relation == "bench_child"
                and run.payload.seed != binding.execution_seed
            )
        ):
            raise Conflict(
                "benchmark aggregate producer Run differs from its immutable binding",
                producer_run_id=binding.producer_run_id,
            )
        assert definition is not None
        if run.run_kind_definition_digest != run_kind_definition_digest(definition):
            raise Conflict(
                "benchmark aggregate producer Run kind authority differs from its binding",
                producer_run_id=binding.producer_run_id,
            )
        try:
            self._registry.validate_payload_bindings(
                payload=run.payload,
                definition=definition,
            )
        except IntegrityViolation as exc:
            raise Conflict(
                "benchmark aggregate producer Run execution authority is invalid",
                producer_run_id=binding.producer_run_id,
            ) from exc

        result_artifact = read.artifacts.get(binding.producer_result_artifact_id)
        if (
            not isinstance(result_artifact, ArtifactV2)
            or result_artifact.kind != "run_result"
            or result_artifact.meta.get("payload_schema_id") != "run-result@1"
            or not compare_digest(
                result_artifact.payload_hash,
                binding.producer_result_payload_hash,
            )
        ):
            raise Conflict(
                "benchmark aggregate producer RunResult differs from its immutable binding",
                producer_run_id=binding.producer_run_id,
            )
        result = self._load_json_artifact(
            result_artifact,
            read=read,
            payload_schema_id="run-result@1",
            model=RunResultV1,
        )
        artifact_is_manifest = artifact.artifact_id == result_artifact.artifact_id
        if (
            result.run_id != run.run_id
            or result.run_kind != run.kind
            or result.attempt_no != binding.producer_attempt_no
            or not compare_digest(
                result.version_projection.run_payload_hash,
                run.payload_hash,
            )
            or result_artifact.version_tuple != result.version_projection.terminal_version_tuple
            or (
                not artifact_is_manifest
                and (
                    artifact.artifact_id not in result.produced_artifact_ids
                    or artifact.artifact_id not in result_artifact.lineage
                )
            )
        ):
            raise Conflict(
                "benchmark aggregate producer RunResult does not authorize the bound Artifact",
                producer_run_id=binding.producer_run_id,
                artifact_id=artifact.artifact_id,
            )

    def _verify_task_suite_derivation(
        self,
        *,
        params: TaskSuiteDerivePayloadV1,
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> None:
        preview = artifacts[params.source_preview_artifact_id]
        config = artifacts[params.config_artifact_id]
        constraint = artifacts[params.constraint_snapshot_artifact_id]
        package = self._load_config_export(config, read=read)
        self._verify_config_binding(
            package=package,
            config=config,
            preview=preview,
            constraint=constraint,
            environment_profile=params.environment_profile,
            catalog=catalog,
        )
        definition = self._catalog_definition(
            params.derivation_profile,
            "task_suite_derivation",
            catalog=catalog,
        )
        profile_config = self._task_suite_derivation_config(definition)
        if profile_config.target_environment_profile != params.environment_profile:
            self._stale("task-suite derivation profile targets another environment")
        if (
            profile_config.completion_oracle_registry_version
            != params.completion_oracle_registry_ref.registry_version
            or not compare_digest(
                profile_config.completion_oracle_registry_digest,
                params.completion_oracle_registry_ref.digest,
            )
        ):
            self._stale("task-suite derivation profile binds another oracle registry")
        self._completion_oracle_registry(params.completion_oracle_registry_ref)

    def _verify_playtest_admission(
        self,
        *,
        params: PlaytestRunPayloadV1,
        artifacts: dict[str, ArtifactV2],
        read: AdmissionReadPort,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> DomainScope:
        config = artifacts[params.config_artifact_id]
        constraint = artifacts[params.constraint_snapshot_artifact_id]
        suite_artifact = artifacts[params.task_suite_artifact_id]
        suite = self._load_json_artifact(
            suite_artifact,
            read=read,
            payload_schema_id="task-suite@1",
            model=TaskSuiteV1,
        )
        if not isinstance(suite, TaskSuiteV1):  # type narrowing for static checkers
            raise IntegrityViolation("task suite payload did not parse as TaskSuiteV1")
        if suite.config_export_artifact_id != params.config_artifact_id:
            self._stale("task suite is bound to a different config export")
        if suite.constraint_snapshot_artifact_id != params.constraint_snapshot_artifact_id:
            self._stale("task suite is bound to a different constraint snapshot")
        if suite.environment_profile != params.environment_profile:
            self._stale("task suite is bound to a different environment profile")

        preview = read.artifacts.get(suite.source_preview_artifact_id)
        if not isinstance(preview, ArtifactV2) or preview.kind != "ir_snapshot":
            raise IntegrityViolation(
                "task suite source preview Artifact is unavailable or has the wrong kind",
                artifact_id=suite.source_preview_artifact_id,
            )
        package = self._load_config_export(config, read=read)
        contract = self._verify_config_binding(
            package=package,
            config=config,
            preview=preview,
            constraint=constraint,
            environment_profile=params.environment_profile,
            allow_replay_only=llm_execution_mode == "replay",
            catalog=catalog,
        )
        if suite.env_contract_version != contract.env_contract_version:
            self._stale("task suite env-contract version differs from the environment profile")
        suite_profile_definition = self._catalog_definition(
            suite.suite_profile,
            "task_suite_derivation",
            allow_replay_only=llm_execution_mode == "replay",
            catalog=catalog,
        )
        suite_profile_config = self._task_suite_derivation_config(suite_profile_definition)
        if suite_profile_config.target_environment_profile != suite.environment_profile:
            self._stale("task suite derivation profile targets another environment")
        if (
            suite_profile_config.completion_oracle_registry_version
            != suite.completion_oracle_registry_ref.registry_version
            or not compare_digest(
                suite_profile_config.completion_oracle_registry_digest,
                suite.completion_oracle_registry_ref.digest,
            )
        ):
            self._stale("task suite derivation profile binds another oracle registry")
        oracle_registry = self._completion_oracle_registry(suite.completion_oracle_registry_ref)

        planner_definition = self._catalog_definition(
            params.planner_policy,
            "playtest_planner",
            allow_replay_only=llm_execution_mode == "replay",
            catalog=catalog,
        )
        planner_config = self._playtest_planner_config(planner_definition)
        try:
            playtest_resource_upper_bounds(
                planner_config,
                episode_count=len(params.episodes),
                max_steps_per_episode=params.max_steps_per_episode,
            )
        except ValueError as exc:
            raise Conflict("Playtest request exceeds its exact planner resource envelope") from exc

        self._verify_derived_version_tuple(
            artifact=suite_artifact,
            preview=preview,
            constraint=constraint,
            env_contract_version=contract.env_contract_version,
            label="task suite",
        )
        expected_suite_lineage = {
            preview.artifact_id,
            config.artifact_id,
            constraint.artifact_id,
            *(episode.scenario_spec_artifact_id for episode in suite.episodes),
        }
        if set(suite_artifact.lineage) != expected_suite_lineage:
            self._stale("task suite lineage does not exactly cover its bound artifacts")

        by_episode = {episode.episode_id: episode for episode in suite.episodes}
        for episode in suite.episodes:
            if episode.reset_binding.reset_schema_id != contract.reset_schema_id:
                self._stale("task suite episode binds another reset schema")
            try:
                oracle_definition = resolve_completion_oracle(
                    oracle_registry,
                    suite.completion_oracle_registry_ref,
                    episode.completion_oracle,
                )
            except ValueError as exc:
                raise StaleTaskSuite(
                    "task suite completion oracle no longer resolves in its exact registry"
                ) from exc
            self._validate_playtest_payload_exact(
                schema_id=contract.reset_schema_id,
                purpose="scenario_reset",
                payload=episode.reset_binding.payload,
                stale_message="task suite episode reset payload violates its exact schema",
            )
            self._validate_playtest_payload_exact(
                schema_id=oracle_definition.params_schema_id,
                purpose="completion_oracle_params",
                payload=episode.completion_oracle.params,
                stale_message="task suite completion-oracle params violate their exact schema",
            )

        for selected in params.episodes:
            episode = by_episode.get(selected.episode_id)
            if episode is None:
                self._stale("selected playtest episode is absent from the task suite")
            assert isinstance(episode, TaskEpisodeV1)
            if episode.scenario_spec_artifact_id != selected.scenario_spec_artifact_id:
                self._stale("selected playtest episode points to a different scenario")
            if params.max_steps_per_episode > episode.step_budget:
                self._stale("playtest max steps exceed the selected episode step budget")
            scenario_artifact = artifacts.get(selected.scenario_spec_artifact_id)
            if scenario_artifact is None:
                raise IntegrityViolation("selected scenario is absent from the exact input set")
            scenario = self._load_json_artifact(
                scenario_artifact,
                read=read,
                payload_schema_id="scenario-spec@1",
                model=ScenarioSpecV1,
            )
            if not isinstance(scenario, ScenarioSpecV1):
                raise IntegrityViolation("scenario payload did not parse as ScenarioSpecV1")
            self._validate_playtest_payload_exact(
                schema_id=contract.reset_schema_id,
                purpose="scenario_reset",
                payload=scenario.reset_binding.payload,
                stale_message="selected scenario reset payload violates its exact schema",
            )
            self._verify_scenario_binding(
                scenario=scenario,
                scenario_artifact=scenario_artifact,
                episode=episode,
                preview=preview,
                config=config,
                constraint=constraint,
                environment_profile=params.environment_profile,
                env_contract_version=contract.env_contract_version,
                reset_schema_id=contract.reset_schema_id,
            )
        return self._union_domain_scopes(tuple(episode.domain_scope for episode in suite.episodes))

    @staticmethod
    def _task_suite_derivation_config(definition: Any) -> TaskSuiteDerivationProfileConfigV2:
        try:
            return TaskSuiteDerivationProfileConfigV2.model_validate(definition.config)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("task-suite derivation profile config is invalid") from exc

    @staticmethod
    def _playtest_planner_config(definition: Any) -> PlaytestPlannerProfileConfigV2:
        try:
            return PlaytestPlannerProfileConfigV2.model_validate(definition.config)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("playtest planner profile config is invalid") from exc

    def _validate_playtest_payload_exact(
        self,
        *,
        schema_id: str,
        purpose: Any,
        payload: Any,
        stale_message: str,
    ) -> None:
        validator = self._playtest_payload_validator
        if validator is None:
            raise IntegrityViolation("Run admission lacks the trusted playtest payload validator")
        try:
            validator.validate_exact(
                schema_id=schema_id,
                purpose=purpose,
                payload=payload,
            )
        except IntegrityViolation as exc:
            raise StaleTaskSuite(stale_message) from exc

    def _verify_scenario_binding(
        self,
        *,
        scenario: ScenarioSpecV1,
        scenario_artifact: ArtifactV2,
        episode: TaskEpisodeV1,
        preview: ArtifactV2,
        config: ArtifactV2,
        constraint: ArtifactV2,
        environment_profile: ProfileRefV1,
        env_contract_version: str,
        reset_schema_id: str,
    ) -> None:
        expected = (
            scenario.source_preview_artifact_id == preview.artifact_id
            and scenario.config_export_artifact_id == config.artifact_id
            and scenario.constraint_snapshot_artifact_id == constraint.artifact_id
            and scenario.environment_profile == environment_profile
            and scenario.env_contract_version == env_contract_version
            and scenario.reset_binding.reset_schema_id == reset_schema_id
            and episode.domain_scope == scenario.domain_scope
            and episode.reset_binding == scenario.reset_binding
        )
        if not expected:
            self._stale("selected scenario, episode, and environment bindings differ")
        if set(scenario_artifact.lineage) != {
            preview.artifact_id,
            config.artifact_id,
            constraint.artifact_id,
        }:
            self._stale("scenario lineage does not exactly bind preview, config, and constraint")
        self._verify_derived_version_tuple(
            artifact=scenario_artifact,
            preview=preview,
            constraint=constraint,
            env_contract_version=env_contract_version,
            label="scenario",
        )

    def _verify_config_binding(
        self,
        *,
        package: ConfigExportPackageV1,
        config: ArtifactV2,
        preview: ArtifactV2,
        constraint: ArtifactV2,
        environment_profile: ProfileRefV1,
        allow_replay_only: bool = False,
        catalog: ExecutionProfileCatalogSnapshotV1 | None = None,
    ) -> Any:
        export_definition = self._catalog_definition(
            package.export_profile,
            "config_export",
            allow_replay_only=allow_replay_only,
            catalog=catalog,
        )
        details = export_definition.details
        if not isinstance(details, ConfigExportProfileDetailsV1):
            raise IntegrityViolation("config-export profile has the wrong details variant")
        environment_definition = self._catalog_definition(
            environment_profile,
            "environment",
            allow_replay_only=allow_replay_only,
            catalog=catalog,
        )
        environment_details = environment_definition.details
        if not isinstance(environment_details, EnvironmentProfileDetailsV1):
            raise IntegrityViolation("environment profile has the wrong details variant")
        contract = environment_details.contract
        if not (
            package.source_preview_artifact_id == preview.artifact_id
            and package.constraint_snapshot_artifact_id == constraint.artifact_id
            and package.target_environment_profile == environment_profile
            and details.target_environment_profile == environment_profile
            and package.env_contract_version == details.env_contract_version
            and package.env_contract_version == contract.env_contract_version
            and package.format_schema_id == details.format_schema_id
            and package.package_schema_version == details.package_schema_version
        ):
            self._stale("config export does not exactly bind preview, constraint, and environment")
        if set(config.lineage) != {preview.artifact_id, constraint.artifact_id}:
            self._stale("config export lineage must exactly bind preview and constraint")
        self._verify_derived_version_tuple(
            artifact=config,
            preview=preview,
            constraint=constraint,
            env_contract_version=contract.env_contract_version,
            label="config export",
        )
        return contract

    @staticmethod
    def _verify_derived_version_tuple(
        *,
        artifact: ArtifactV2,
        preview: ArtifactV2,
        constraint: ArtifactV2,
        env_contract_version: str,
        label: str,
    ) -> None:
        preview_snapshot_id = preview.version_tuple.ir_snapshot_id
        constraint_snapshot_id = constraint.version_tuple.constraint_snapshot_id
        if preview_snapshot_id is None or constraint_snapshot_id is None:
            raise IntegrityViolation(
                f"{label} parent artifacts omit required snapshot version fields"
            )
        if (
            artifact.version_tuple.doc_version != preview.version_tuple.doc_version
            or artifact.version_tuple.ir_snapshot_id != preview_snapshot_id
            or artifact.version_tuple.constraint_snapshot_id != constraint_snapshot_id
            or artifact.version_tuple.env_contract_version != env_contract_version
        ):
            raise StaleTaskSuite(f"{label} VersionTuple differs from its exact parents")

    def _catalog_definition(
        self,
        profile: ProfileRefV1,
        expected_kind: str,
        *,
        allow_replay_only: bool = False,
        catalog: ExecutionProfileCatalogSnapshotV1 | None = None,
    ) -> Any:
        exact_catalog = self._catalog if catalog is None else catalog
        definition = next(
            (item for item in exact_catalog.definitions if item.profile == profile),
            None,
        )
        lifecycle = next(
            (item for item in exact_catalog.lifecycle if item.profile == profile),
            None,
        )
        allowed_states = {"active", "replay_only"} if allow_replay_only else {"active"}
        if (
            definition is None
            or lifecycle is None
            or lifecycle.state not in allowed_states
            or definition.profile_kind != expected_kind
        ):
            self._stale("artifact profile lifecycle does not permit this exact admission mode")
        return definition

    def _completion_oracle_registry(self, ref: Any) -> Any:
        resolver = getattr(self._registry, "get_completion_oracle_registry", None)
        if not callable(resolver):
            raise IntegrityViolation("Run registry cannot resolve completion-oracle registries")
        registry = resolver(ref)
        if registry is None:
            self._stale("completion-oracle registry binding is unavailable")
        return registry

    def _load_config_export(
        self, artifact: ArtifactV2, *, read: AdmissionReadPort
    ) -> ConfigExportPackageV1:
        self._require_payload_schema(artifact, "config-export-package@1")
        blob = self._load_artifact_blob(artifact, read=read)
        try:
            return decode_config_export_bytes(blob)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "config export Artifact payload is not a valid canonical package",
                artifact_id=artifact.artifact_id,
            ) from exc

    def _load_json_artifact(
        self,
        artifact: ArtifactV2,
        *,
        read: AdmissionReadPort,
        payload_schema_id: str,
        model: Any,
    ) -> Any:
        self._require_payload_schema(artifact, payload_schema_id)
        blob = self._load_artifact_blob(artifact, read=read)
        try:
            value = json.loads(blob.decode("utf-8"))
            if canonical_json(value).encode("utf-8") != blob:
                raise ValueError("payload JSON is not canonical")
            return model.model_validate(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "Artifact payload does not match its declared schema",
                artifact_id=artifact.artifact_id,
                payload_schema_id=payload_schema_id,
            ) from exc

    @staticmethod
    def _require_payload_schema(artifact: ArtifactV2, expected: str) -> None:
        if artifact.meta.get("payload_schema_id") != expected:
            raise IntegrityViolation(
                "Artifact payload schema binding differs from the required schema",
                artifact_id=artifact.artifact_id,
                expected_payload_schema_id=expected,
            )

    def _load_artifact_blob(self, artifact: ArtifactV2, *, read: AdmissionReadPort) -> bytes:
        bindings = read.object_bindings
        if bindings is None:
            raise IntegrityViolation("admission read scope has no ObjectBinding authority")
        if artifact.object_ref.size_bytes > _MAX_ADMISSION_BLOB_BYTES:
            raise IntegrityViolation(
                "admission Artifact payload exceeds the hard byte limit",
                artifact_id=artifact.artifact_id,
            )
        try:
            binding = bindings.resolve(artifact.object_ref)
            if binding.object_ref != artifact.object_ref or binding.status != "active":
                raise IntegrityViolation(
                    "active ObjectBinding differs from the admitted Artifact",
                    artifact_id=artifact.artifact_id,
                )
            stream = self._objects.open(binding.location)
        except FileNotFoundError as exc:
            raise IntegrityViolation(
                "committed Artifact object binding is unavailable",
                artifact_id=artifact.artifact_id,
            ) from exc
        except OSError as exc:
            raise DependencyUnavailable(
                "committed Artifact object payload is unavailable",
                component="object_store",
                artifact_id=artifact.artifact_id,
            ) from exc
        chunks: list[bytes] = []
        observed = 0
        try:
            while observed <= artifact.object_ref.size_bytes:
                chunk = stream.read(min(64 * 1024, artifact.object_ref.size_bytes + 1 - observed))
                if not isinstance(chunk, bytes):
                    raise IntegrityViolation(
                        "ObjectStore payload stream returned non-bytes content",
                        artifact_id=artifact.artifact_id,
                    )
                if not chunk:
                    break
                chunks.append(chunk)
                observed += len(chunk)
        except OSError as exc:
            raise DependencyUnavailable(
                "committed Artifact object payload cannot be read",
                component="object_store",
                artifact_id=artifact.artifact_id,
            ) from exc
        finally:
            stream.close()
        blob = b"".join(chunks)
        if (
            len(blob) != artifact.object_ref.size_bytes
            or len(blob) > _MAX_ADMISSION_BLOB_BYTES
            or sha256_lowerhex(blob) != artifact.payload_hash
        ):
            raise IntegrityViolation(
                "committed Artifact bytes differ from its ObjectRef",
                artifact_id=artifact.artifact_id,
            )
        return blob

    @staticmethod
    def _stale(detail: str) -> None:
        raise StaleTaskSuite(detail)

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
            add(params.constraint_snapshot_artifact_id, ("constraint_snapshot",))
            add_ref(params.target, ("ir_snapshot",))
            for config in params.candidate_config_export_artifact_ids:
                add(config, ("config_export",))
            for review in params.review_artifact_ids:
                add(review, ("review_report",))
            for trace in params.playtest_trace_artifact_ids:
                add(trace, ("playtest_trace",))
            for suite in params.regression_suite_artifact_ids:
                add(suite, ("regression_suite",))
            for binding in (*params.expected_findings, *params.findings):
                add(binding.evidence_artifact_id, _FINDING_EVIDENCE_KINDS)
        elif isinstance(params, ConstraintValidationPayloadV1):
            add(params.subject.subject_artifact_id, ("constraint_proposal",))
            add(params.base_constraint_snapshot_artifact_id, ("constraint_snapshot",))
            add_ref(params.target, ("constraint_snapshot",))
            add(params.golden_suite_artifact_id, ("golden_suite",))
            for suite in params.regression_suite_artifact_ids:
                add(suite, ("regression_suite",))
        elif isinstance(params, RollbackValidationPayloadV1):
            add(params.subject.subject_artifact_id, ("rollback_request",))
            # A revisioned ref may govern any retained ArtifactKind.  The immutable
            # rollback draft's exact target binding supplies the authoritative kind,
            # digest, snapshot (when applicable), and profile closure below.
            add(params.expected_current_ref.artifact_id, _ANY_KIND)
            add(params.target_artifact_id, _ANY_KIND)
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
            for case_result in params.case_result_artifact_ids:
                add(case_result, _BENCH_CASE_RESULT_KINDS)
        elif isinstance(params, ArtifactMigrationPayloadV1):
            add(params.source_artifact_id, ())
        return tuple(checks)

    # ── envelope helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _input_artifact_ids(
        params: RunKindPayload,
        cassette_artifact_id: str | None,
        *,
        additional_input_artifact_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        referenced = list(referenced_input_artifact_ids(params))
        if cassette_artifact_id is not None:
            referenced.append(cassette_artifact_id)
        referenced.extend(additional_input_artifact_ids)
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
