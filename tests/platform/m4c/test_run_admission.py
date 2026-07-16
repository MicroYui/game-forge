"""Real-SQLite Run admission tests (M4c Task 8).

Exercises the admission engine over the real ``RunCommandService.create_run`` UoW,
the real fenced ``SqlRunRepository`` queue authority, the real ``SqlCostLedger``
budget hold, and the real builtin registry/execution-profile catalog. No network.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from gameforge.contracts.api import (
    PatchValidationAdmissionRequestV1,
    RollbackValidationAdmissionRequestV1,
)
from gameforge.contracts.benchmark import (
    BenchmarkAgentResponseExecutorV1,
    BenchmarkAggregateInputBindingV1,
    BenchmarkBinaryMetricDefinitionV1,
    BenchmarkBinaryMetricTargetV1,
    BenchmarkCaseExecutionV1,
    BenchmarkCleanOracleFpExecutorV1,
    BenchmarkDatasetCaseV1,
    BenchmarkDatasetBindingV1,
    BenchmarkDatasetPartitionV1,
    BenchmarkDatasetV1,
    BenchmarkEqualsPredicateV1,
    BenchmarkMetricPolicyV1,
    BenchmarkMetricRefV1,
    BenchmarkOrderKeyV1,
    BenchmarkOrderingPolicyV1,
    BenchmarkPartitionV1,
    BenchmarkResourceLimitsV1,
    BenchmarkSamplingPolicyV1,
    BenchmarkSimulationExecutionV1,
    BenchmarkSpecV1,
    build_builtin_benchmark_evaluator_policy,
)
from gameforge.contracts.canonical import canonical_json, canonical_sha256, sha256_lowerhex
from gameforge.contracts.cassette_import import CassetteBundleV1
from gameforge.contracts.config_export import (
    ConfigExportFileV1,
    ConfigExportPackageV1,
    canonical_config_export_bytes,
)
from gameforge.contracts.cost import BudgetV1, CostAmountV1
from gameforge.contracts.errors import (
    Conflict,
    DependencyUnavailable,
    Forbidden,
    IntegrityViolation,
    QuotaExceeded,
    StaleTaskSuite,
)
from gameforge.contracts.execution_profiles import (
    ExecutionProfileCatalogSnapshotV1,
    PlaytestPlannerProfileConfigV1,
    ProfileRefV1,
    canonical_config_hash,
    execution_profile_catalog_digest,
)
from gameforge.contracts.findings import (
    FindingPayloadV1,
    FindingRevisionV1,
    PatchV2,
    finding_revision_digest,
)
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
    Permission,
    Principal,
    RoleAssignmentV1,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.jobs import (
    ArtifactMigrationPayloadV1,
    BenchRunPayloadV1,
    CheckerRunPayloadV1,
    DrDrillPayloadV1,
    GraphSelectionV1,
    PatchRepairPayloadV1,
    PatchValidationPayloadV1,
    PlaytestEpisodeBindingV1,
    PlaytestRunPayloadV1,
    ReviewRunPayloadV1,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunKindRef,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunResultSummaryV1,
    RunResultV1,
    PlannedAgentNodeVersionV1,
    ExecutionVersionPlanV1,
    RefReadBindingV1,
    SimulationRunPayloadV1,
    TaskSuiteDerivePayloadV1,
    ValidationSubjectBindingV1,
    VersionTransitionPolicyRefV1,
    canonical_payload_hash,
    execution_version_plan_digest,
)
from gameforge.contracts.playtest import (
    CompletionOracleRefV1,
    CompletionOracleRegistryRefV1,
    PlaytestEpisodeTraceV1,
    PlaytestTraceV1,
    ScenarioResetBindingV1,
    ScenarioSpecV1,
    TaskEpisodeV1,
    TaskSuiteV1,
)
from gameforge.contracts.review import ReviewReport
from gameforge.bench.metrics import default_constraints
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingPolicyV1,
    RoutingRuleV1,
    compute_model_catalog_digest,
    compute_routing_policy_digest,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    VersionTuple,
    build_artifact_v2,
    build_execution_identity,
)
from gameforge.contracts.observability import TraceContextV1
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    EvidenceRequirement,
    EvidenceSet,
    FindingEvidenceBindingV1,
    PatchTargetBindingV1,
    RollbackRequestV1,
    RollbackTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.cost_policy.run_accounting import SqlRunCostAccounting
from gameforge.platform.provenance import (
    AuthenticatedGoalSourceWriter,
    GoalProvenancePolicy,
    build_source_kind_registry,
)
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.runs.admission import (
    AdmissionReadPort,
    AdmissionRequestContext,
    AdmissionRunPublicationGateway,
    ConservativeAttemptUsageProvider,
    DefaultRunBudgetPlanProvider,
    RunAdmissionEngine,
    _MAX_APPLICABLE_BUDGETS_PER_SCOPE,
    _SourceWriteCapabilities,
    build_admission_capability_binder,
)
from gameforge.platform.runs.commands import RunCommandCapabilities, RunCommandService
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.models import Base, RunAttemptRow, RunRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from gameforge.runtime.observability.context import TraceCarrier, use_trace_context
from tests.platform.m4 import validation_testkit

NOW_DT = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-15T12:00:00Z"
CURSOR_KEY = b"m4c-run-admission-cursor-key"
OBJECT_CURSOR_KEY = b"m4c-run-admission-object-cursor-key"
AUDIT_CHAIN_ID = "platform-authority"

CHECKER_PROFILE = ProfileRefV1(profile_id="builtin.checker", version=1)
SIMULATION_PROFILE = ProfileRefV1(profile_id="builtin.simulation", version=1)
WORKLOAD_PROFILE = ProfileRefV1(profile_id="builtin.workload", version=1)
GENERATION_PROFILE = ProfileRefV1(profile_id="builtin.generation", version=1)
REVIEW_PROFILE = ProfileRefV1(profile_id="builtin.review", version=1)
LLM_TRIAGE_PROFILE = ProfileRefV1(profile_id="builtin.llm_triage", version=1)
CONFIG_EXPORT_PROFILE = ProfileRefV1(profile_id="builtin.config_export", version=1)
ENVIRONMENT_PROFILE = ProfileRefV1(profile_id="builtin.environment", version=1)
TASK_SUITE_PROFILE = ProfileRefV1(profile_id="builtin.task_suite_derivation", version=1)
PLAYTEST_PLANNER_PROFILE = ProfileRefV1(profile_id="builtin.playtest_planner", version=1)
VALIDATION_PROFILE = ProfileRefV1(profile_id="builtin.validation", version=1)
ROLLBACK_PROFILE = ProfileRefV1(profile_id="builtin.rollback", version=1)
SCHEMA_COMPATIBILITY_PROFILE = ProfileRefV1(profile_id="builtin.schema_compatibility", version=1)
BENCH_EVALUATOR_PROFILE = ProfileRefV1(profile_id="builtin.bench_evaluator", version=1)
ENV_CONTRACT_VERSION = "generic-agent-env@1"
RESET_SCHEMA_ID = "generic-env-reset@1"

ROLE_POLICY_VERSION = "run-admission-roles@1"
DOMAIN_REGISTRY_VERSION = "run-admission-domains@1"
DOMAIN_IDS = ("builtin", "combat", "economy", "narrative")
# Every dynamic content-run permission the admission engine authorizes. A grant with
# domain_scope="all" plus an assignment scope="all" covers every active domain, so
# a broad "tooling" operator is authorized for every RunKind's resolved domain.
_TOOLING_GRANTS: tuple[tuple[str, str], ...] = (
    ("replay", "run"),
    ("run", "checker"),
    ("run", "simulation"),
    ("run", "review"),
    ("run", "bench"),
    ("run", "playtest"),
    ("propose", "patch"),
    ("propose", "constraint_proposal"),
    ("derive", "task_suite"),
    ("validate", "patch"),
    ("validate", "constraint_proposal"),
    ("validate", "rollback_request"),
    ("migrate", "artifact"),
)


def _domain_registry() -> DomainRegistryV1:
    definitions = tuple(
        DomainDefinitionV1(domain_id=domain_id, display_name=domain_id.title(), status="active")
        for domain_id in DOMAIN_IDS
    )
    return DomainRegistryV1(
        registry_version=DOMAIN_REGISTRY_VERSION,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(DOMAIN_REGISTRY_VERSION, definitions),
    )


def _role_policy(registry: DomainRegistryV1) -> RolePolicy:
    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants = {
        "tooling": (
            *tuple(
                Permission(action=action, resource_kind=resource_kind, domain_scope="all")
                for action, resource_kind in _TOOLING_GRANTS
            ),
            Permission(action="drill", resource_kind="operations", domain_scope=None),
        ),
        # A patch proposer whose reach is narrowed to a single domain by its
        # assignment scope. Used to prove a client-declared domain cannot escalate.
        "content_designer": (
            Permission(action="propose", resource_kind="patch", domain_scope="all"),
        ),
    }
    effective_from = "2026-07-15T00:00:00Z"
    return RolePolicy(
        policy_version=ROLE_POLICY_VERSION,
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            ROLE_POLICY_VERSION, registry_ref, grants, effective_from
        ),
    )


def _assignment(
    *,
    role: str,
    scope: DomainScope | str | None,
    assignment_id: str,
    principal_id: str = "human:actor",
) -> RoleAssignmentV1:
    return RoleAssignmentV1(
        assignment_id=assignment_id,
        principal_id=principal_id,
        role=role,  # type: ignore[arg-type]
        scope=scope,
        status="active",
        revision=1,
        granted_at=NOW,
        granted_by=AuditActor(principal_id="human:admin", principal_kind="human"),
    )


def _principal(kind: str, *roles: RoleAssignmentV1) -> Principal:
    return Principal(
        id=f"{kind}:actor",
        kind=kind,  # type: ignore[arg-type]
        display_name=kind,
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=roles,
    )


def _actor(kind: str = "human", *roles: RoleAssignmentV1) -> ActorContext:
    mechanism = {"human": "session", "service": "api_key", "system": "trusted_internal"}[kind]
    return ActorContext(
        principal=_principal(kind, *roles),
        authentication=AuthenticationContext(
            mechanism=mechanism,  # type: ignore[arg-type]
            credential_id=None if kind == "system" else f"credential:{kind}",
        ),
        session_id=f"session:{kind}" if kind == "human" else None,
        request_id=f"request:{kind}",
    )


def _tooling_actor() -> ActorContext:
    """A human operator authorized (tooling, all domains) for every content RunKind."""

    return _actor("human", _assignment(role="tooling", scope="all", assignment_id="assign:tool"))


def _system_operator_actor(*, domainless: bool = False) -> ActorContext:
    return _actor(
        "system",
        _assignment(
            role="tooling",
            scope=None if domainless else "all",
            assignment_id="assign:system-operations",
            principal_id="system:actor",
        ),
    )


def _server(key: str) -> AdmissionRequestContext:
    return AdmissionRequestContext(
        idempotency_key=key,
        request_hash=canonical_sha256({"key": key}),
        request_id=f"request:{key}",
        trace_id=None,
    )


def _model_authorities() -> tuple[ModelCatalogSnapshotV1, RoutingPolicyV1]:
    descriptor = ModelDescriptorV1(
        provider="test",
        model_snapshot="test:model@1",
        tier="best",
        capabilities=("reasoning",),
        context_limit=100_000,
        max_output_tokens=10_000,
        prompt_cache_support=False,
        status="active",
    )
    catalog_body = {
        "catalog_version": 1,
        "models": (descriptor,),
        "created_at": NOW_DT,
    }
    catalog = ModelCatalogSnapshotV1(
        **catalog_body,
        catalog_digest=compute_model_catalog_digest(catalog_body),
    )
    node_ids = (
        "bench-agent-case",
        "extraction",
        "generation",
        "playtest.executor",
        "playtest.memory",
        "playtest.planner",
        "playtest.reflect",
        "repair",
        "review-triage",
    )
    rules = tuple(
        RoutingRuleV1(
            rule_id=f"route:{node_id}",
            task_kind=node_id,
            required_capabilities=("reasoning",),
            primary_model_snapshot=descriptor.model_snapshot,
            allowed_fallback_chain=(),
            budget_predicates=(),
        )
        for node_id in node_ids
    )
    policy_body = {
        "policy_version": 1,
        "catalog_version": catalog.catalog_version,
        "catalog_digest": catalog.catalog_digest,
        "rules": rules,
        "failure_classifier_version": "failure-classifier@1",
    }
    policy = RoutingPolicyV1(
        **policy_body,
        routing_policy_digest=compute_routing_policy_digest(policy_body),
    )
    return catalog, policy


def _catalog_for_domains(
    catalog: ExecutionProfileCatalogSnapshotV1,
    domain_ids: tuple[str, ...],
    profile_updates: dict[str, dict[str, Any]] | None = None,
    profile_lifecycle_states: dict[str, str] | None = None,
) -> ExecutionProfileCatalogSnapshotV1:
    updates_by_id = profile_updates or {}
    lifecycle_states_by_id = profile_lifecycle_states or {}
    definitions = tuple(
        definition.model_copy(
            update={
                "domain_scope": DomainScope(domain_ids=domain_ids),
                **updates_by_id.get(definition.profile.profile_id, {}),
            }
        )
        for definition in catalog.definitions
    )
    body = {
        "catalog_version": catalog.catalog_version,
        "definitions": definitions,
        "lifecycle": tuple(
            item.model_copy(
                update=(
                    {
                        "state": lifecycle_states_by_id[item.profile.profile_id],
                        "reason_code": "historical_replay_only",
                    }
                    if item.profile.profile_id in lifecycle_states_by_id
                    else {}
                )
            )
            for item in catalog.lifecycle
        ),
    }
    return ExecutionProfileCatalogSnapshotV1(
        **body,
        catalog_digest=execution_profile_catalog_digest(body),
    )


def _registry_with_catalog(
    source: ImmutablePlatformRegistry,
    catalog: ExecutionProfileCatalogSnapshotV1,
) -> ImmutablePlatformRegistry:
    return _registry_with_catalogs(source, (catalog,))


def _registry_with_catalogs(
    source: ImmutablePlatformRegistry,
    catalogs: tuple[ExecutionProfileCatalogSnapshotV1, ...],
) -> ImmutablePlatformRegistry:
    return ImmutablePlatformRegistry(
        run_kinds=source._run_kinds.values(),  # noqa: SLF001 - exact test composition clone
        retry_policies=source._retry_policies.values(),  # noqa: SLF001
        failure_classifiers=source._failure_classifiers.values(),  # noqa: SLF001
        lineage_policies=source._lineage_policies.values(),  # noqa: SLF001
        version_transition_policies=source._version_transition_policies.values(),  # noqa: SLF001
        runtime_parent_rule_sets=source._runtime_parent_rule_sets.values(),  # noqa: SLF001
        finding_output_policies=source._finding_output_policies.values(),  # noqa: SLF001
        run_event_registries=source._run_event_registries.values(),  # noqa: SLF001
        completion_oracle_registries=source._completion_oracle_registries.values(),  # noqa: SLF001
        agent_execution_graphs=source._agent_execution_graphs.values(),  # noqa: SLF001
        execution_profile_catalogs=catalogs,
        migration_capability_matrices=source._migration_capability_matrices.values(),  # noqa: SLF001
        profile_requirements=source._profile_requirements,  # noqa: SLF001
        permission_resolver_keys=source._permission_resolver_keys,  # noqa: SLF001
    )


def _shared_budget(
    *,
    budget_id: str,
    scope_kind: str,
    scope_id: str,
    request_limit: int = 10_000_000,
    status: str = "active",
) -> BudgetV1:
    return BudgetV1(
        budget_id=budget_id,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        policy_version="shared-budget-policy@1",
        limits=(
            CostAmountV1(dimension="request", value=request_limit, unit="request"),
            CostAmountV1(dimension="concurrent_run", value=3, unit="count"),
        ),
        reserved=(),
        consumed=(),
        status=status,  # type: ignore[arg-type]
        revision=1,
        created_at=NOW_DT,
    )


class _NullApprovals:
    def get(self, approval_id: str) -> Any:
        return None

    def get_subject_head(self, subject_series_id: str) -> None:
        return None


class _FixedApprovals:
    def __init__(self, item: ApprovalItem) -> None:
        self.item = item
        self.head = SubjectHead(
            subject_series_id=item.subject_series_id,
            current_subject_artifact_id=item.subject_artifact_id,
            current_approval_id=item.approval_id,
            revision=item.subject_revision,
        )

    def get(self, approval_id: str) -> ApprovalItem | None:
        return self.item if approval_id == self.item.approval_id else None

    def get_subject_head(self, subject_series_id: str) -> SubjectHead | None:
        return self.head if subject_series_id == self.head.subject_series_id else None

    def compare_and_set(
        self,
        approval_id: str,
        expected_revision: int,
        replacement: ApprovalItem,
    ) -> ApprovalItem:
        if approval_id != self.item.approval_id or expected_revision != self.item.workflow_revision:
            raise Conflict("test approval CAS is stale")
        self.item = replacement
        return replacement


class _TestValidationStartWriter:
    def start(
        self,
        transaction: Any,
        *,
        item: ApprovalItem,
        run_id: str,
        actor: ActorContext,
        request_id: str | None,
        trace_id: str | None,
    ) -> None:
        del actor, request_id, trace_id
        current = transaction.approvals.get(item.approval_id)
        if current != item or current.status != "draft":
            raise Conflict("test validation start requires the exact draft")
        replacement = current.model_copy(
            update={
                "status": "validating",
                "workflow_revision": current.workflow_revision + 1,
                "active_validation_run_id": run_id,
            }
        )
        transaction.approvals.compare_and_set(
            current.approval_id,
            current.workflow_revision,
            replacement,
        )


class _DriftingApprovals(_FixedApprovals):
    def __init__(self, item: ApprovalItem) -> None:
        super().__init__(item)
        self._reads = 0

    def get(self, approval_id: str) -> ApprovalItem | None:
        current = super().get(approval_id)
        if current is None:
            return None
        self._reads += 1
        if self._reads == 1:
            return current
        return current.model_copy(update={"workflow_revision": current.workflow_revision + 1})


class _DriftingSubjectHeadApprovals(_FixedApprovals):
    """Keep the item stable while changing only the head CAS revision."""

    def __init__(self, item: ApprovalItem) -> None:
        super().__init__(item)
        self._head_reads = 0

    def get_subject_head(self, subject_series_id: str) -> SubjectHead | None:
        head = super().get_subject_head(subject_series_id)
        if head is None:
            return None
        self._head_reads += 1
        if self._head_reads == 1:
            return head
        return head.model_copy(update={"revision": head.revision + 1})


class _FixedFindings:
    def __init__(self, revision: FindingRevisionV1) -> None:
        self.revision = revision

    def get(self, finding_id: str, revision: int) -> FindingRevisionV1 | None:
        if (finding_id, revision) == (self.revision.finding_id, self.revision.revision):
            return self.revision
        return None


class _FixedFindingLinks:
    def __init__(self, link: RunFindingLinkV1 | None) -> None:
        self.link = link

    def get_finding_link_by_revision(
        self,
        *,
        run_id: str,
        finding_id: str,
        finding_revision: int,
    ) -> RunFindingLinkV1 | None:
        link = self.link
        if link is None:
            return None
        if (run_id, finding_id, finding_revision) != (
            link.run_id,
            link.finding_id,
            link.finding_revision,
        ):
            return None
        return link


class _RunReadAuthority:
    def __init__(
        self,
        retained: SqlRunRepository,
        synthetic: dict[str, Any],
        prompt_links: dict[tuple[str, int, int, int], RunIntermediateArtifactLinkV1],
    ) -> None:
        self._retained = retained
        self._synthetic = synthetic
        self._prompt_links = prompt_links

    def get(self, run_id: str) -> Any:
        return self._synthetic.get(run_id) or self._retained.get(run_id)

    def get_intermediate_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int = 1,
    ) -> RunIntermediateArtifactLinkV1 | None:
        return self._prompt_links.get(
            (run_id, attempt_no, call_ordinal, route_ordinal)
        ) or self._retained.get_intermediate_link(
            run_id,
            attempt_no,
            call_ordinal,
            route_ordinal,
        )

    def list_prompt_render_links(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]:
        synthetic = tuple(
            link
            for link in self._prompt_links.values()
            if link.run_id == run_id and (attempt_no is None or link.attempt_no == attempt_no)
        )
        retained = self._retained.list_prompt_render_links(run_id, attempt_no=attempt_no)
        return tuple(
            sorted(
                (*retained, *synthetic),
                key=lambda link: (link.attempt_no, link.call_ordinal, link.route_ordinal),
            )
        )

    def list_prompt_render_links_by_artifact_id(
        self,
        artifact_id: str,
        *,
        limit: int,
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]:
        retained = self._retained.list_prompt_render_links_by_artifact_id(
            artifact_id,
            limit=limit,
        )
        synthetic = tuple(
            link for link in self._prompt_links.values() if link.artifact_id == artifact_id
        )
        combined = tuple(
            sorted(
                (*retained, *synthetic),
                key=lambda link: (link.run_id, link.attempt_no, link.call_ordinal),
            )
        )
        if len(combined) > limit:
            raise IntegrityViolation("synthetic prompt reverse lookup exceeds its test bound")
        return combined

    def __getattr__(self, name: str) -> Any:
        return getattr(self._retained, name)


def _exact_draft_item(
    harness: "Harness",
    item: ApprovalItem,
    *,
    domain_scope: DomainScope,
    status: str = "draft",
) -> ApprovalItem:
    registry_ref = DomainRegistryRefV1(
        registry_version=harness.domain_registry.registry_version,
        registry_digest=harness.domain_registry.registry_digest,
    )
    requirements = tuple(
        requirement.model_copy(
            update={
                "domain_scope": domain_scope,
                "required_permission": requirement.required_permission.model_copy(
                    update={"domain_scope": domain_scope}
                ),
            }
        )
        for requirement in item.requirements
    )
    return item.model_copy(
        update={
            "status": status,
            "active_validation_run_id": None,
            "domain_scope": domain_scope,
            "domain_registry_ref": registry_ref,
            "route_policy": item.route_policy.model_copy(
                update={"domain_registry_ref": registry_ref}
            ),
            "role_policy_version": harness.role_policy.policy_version,
            "role_policy_digest": harness.role_policy.policy_digest,
            "requirements": requirements,
        }
    )


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        budget_limits: tuple[CostAmountV1, ...] | None = None,
        profile_domain_ids: tuple[str, ...] = DOMAIN_IDS,
        profile_updates: dict[str, dict[str, Any]] | None = None,
        profile_lifecycle_states: dict[str, str] | None = None,
        provision_shared_budgets: bool = True,
    ):
        self.clock = FrozenUtcClock(NOW_DT)
        self.engine = get_engine(f"sqlite:///{tmp_path / 'admission.db'}")
        Base.metadata.create_all(self.engine)
        self.objects = LocalObjectStore(
            tmp_path / "objects",
            store_id="local",
            clock=self.clock,
            cursor_signing_key=OBJECT_CURSOR_KEY,
        )
        builtin_registry = build_builtin_registry()
        catalogs = builtin_registry.list_execution_profile_catalogs()
        assert len(catalogs) == 1
        self.catalog = _catalog_for_domains(
            catalogs[0],
            profile_domain_ids,
            profile_updates,
            profile_lifecycle_states,
        )
        self.registry = _registry_with_catalog(builtin_registry, self.catalog)
        self.domain_registry = _domain_registry()
        self.role_policy = _role_policy(self.domain_registry)
        self.approvals: _NullApprovals | _FixedApprovals = _NullApprovals()
        self.findings: _FixedFindings | None = None
        self.finding_links: _FixedFindingLinks | None = None
        self.synthetic_runs: dict[str, Any] = {}
        self.synthetic_prompt_links: dict[
            tuple[str, int, int, int], RunIntermediateArtifactLinkV1
        ] = {}
        from sqlalchemy.orm import Session

        with Session(self.engine) as session, session.begin():
            policies = SqlPolicySnapshotRepository(session, clock=self.clock)
            policies.put_execution_profile_catalog(self.catalog)
            policies.put_domain_registry(self.domain_registry)
            policies.put_role_policy(self.role_policy)
            catalog, routing = _model_authorities()
            costs = SqlCostLedger(session, clock=self.clock)
            costs.put_model_catalog(catalog)
            costs.put_routing_policy(routing)
            if provision_shared_budgets:
                costs.put_budget(
                    _shared_budget(
                        budget_id="budget:principal:human:actor",
                        scope_kind="principal",
                        scope_id="human:actor",
                    )
                )
                costs.put_budget(
                    _shared_budget(
                        budget_id="budget:system:global",
                        scope_kind="system",
                        scope_id="global",
                    )
                )
        self.uow = SqliteUnitOfWork(self.engine, self._capability_factory)
        if budget_limits is None:
            binder = build_admission_capability_binder(
                registry=self.registry, clock=self.clock, audit_chain_id=AUDIT_CHAIN_ID
            )
        else:
            binder = self._failing_binder(budget_limits)
        run_commands = RunCommandService(
            unit_of_work=self.uow, bind_capabilities=binder, clock=self.clock
        )
        goal_writer = AuthenticatedGoalSourceWriter(
            policy=GoalProvenancePolicy(registry=build_source_kind_registry())
        )
        self.engine_admission = RunAdmissionEngine(
            run_commands=run_commands,
            unit_of_work=self.uow,
            read_scope=self._read_scope,
            registry=self.registry,
            execution_profile_catalog=self.catalog,
            goal_writer=goal_writer,
            object_store=self.objects,
            clock=self.clock,
            source_uow_capabilities=lambda tx: _SourceWriteCapabilities(
                artifacts=tx.artifacts, object_bindings=tx.object_bindings
            ),
            current_principal_resolver=lambda _tx, actor: actor.principal,
            role_policy_version=ROLE_POLICY_VERSION,
            role_policy_digest=self.role_policy.policy_digest,
            validation_start_writer=_TestValidationStartWriter(),
        )

    def _capability_factory(self, session: Any) -> TransactionCapabilities:
        cursor_signer = CursorSigner(signing_key=CURSOR_KEY, clock=self.clock)
        bindings = SqlObjectBindingRepository(session, self.objects, "local")
        return TransactionCapabilities(
            refs=SqlRefStore(session, cursor_signer=cursor_signer, clock=self.clock),
            audit=SqlAuditSink(session),
            approvals=self.approvals,
            lineage=None,
            object_bindings=bindings,
            runs=SqlRunRepository(session),
            cost=SqlCostLedger(session, clock=self.clock),
            policies=SqlPolicySnapshotRepository(session, clock=self.clock),
            idempotency=SqlIdempotencyRepository(session, clock=self.clock),
            artifacts=SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=cursor_signer,
                clock=self.clock,
            ),
        )

    def install_catalog_history(
        self,
        *,
        current: ExecutionProfileCatalogSnapshotV1,
        retained: tuple[ExecutionProfileCatalogSnapshotV1, ...],
    ) -> None:
        """Recompose admission with a newer current catalog and retained history."""

        self.catalog = current
        self.registry = _registry_with_catalogs(self.registry, retained)
        from sqlalchemy.orm import Session

        with Session(self.engine) as session, session.begin():
            policies = SqlPolicySnapshotRepository(session, clock=self.clock)
            for catalog in retained:
                policies.put_execution_profile_catalog(catalog)
        run_commands = RunCommandService(
            unit_of_work=self.uow,
            bind_capabilities=build_admission_capability_binder(
                registry=self.registry,
                clock=self.clock,
                audit_chain_id=AUDIT_CHAIN_ID,
            ),
            clock=self.clock,
        )
        self.engine_admission = RunAdmissionEngine(
            run_commands=run_commands,
            unit_of_work=self.uow,
            read_scope=self._read_scope,
            registry=self.registry,
            execution_profile_catalog=current,
            goal_writer=AuthenticatedGoalSourceWriter(
                policy=GoalProvenancePolicy(registry=build_source_kind_registry())
            ),
            object_store=self.objects,
            clock=self.clock,
            source_uow_capabilities=lambda tx: _SourceWriteCapabilities(
                artifacts=tx.artifacts,
                object_bindings=tx.object_bindings,
            ),
            current_principal_resolver=lambda _tx, actor: actor.principal,
            role_policy_version=ROLE_POLICY_VERSION,
            role_policy_digest=self.role_policy.policy_digest,
            validation_start_writer=_TestValidationStartWriter(),
        )

    def _failing_binder(self, limits: tuple[CostAmountV1, ...]):
        def bind(transaction: Any) -> RunCommandCapabilities:
            provider = DefaultRunBudgetPlanProvider(
                ledger=transaction.cost,
                clock=self.clock,
                limits=limits,
                reservation=(CostAmountV1(dimension="request", value=1, unit="request"),),
            )
            accounting = SqlRunCostAccounting(
                ledger=transaction.cost,
                plan_provider=provider,
                settlement_provider=ConservativeAttemptUsageProvider(),
                clock=self.clock,
            )
            publication = AdmissionRunPublicationGateway(
                audit=AuditGate(sink=transaction.audit, clock=self.clock),
                chain_id=AUDIT_CHAIN_ID,
            )
            return RunCommandCapabilities(
                runs=transaction.runs,
                registry=self.registry,
                admission=accounting,
                publication=publication,
                accounting=None,
            )

        return bind

    @contextmanager
    def _read_scope(self) -> Iterator[AdmissionReadPort]:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            cursor_signer = CursorSigner(signing_key=CURSOR_KEY, clock=self.clock)
            bindings = SqlObjectBindingRepository(session, self.objects, "local")
            yield AdmissionReadPort(
                policies=SqlPolicySnapshotRepository(session, clock=self.clock),
                approvals=self.approvals,
                artifacts=SqlArtifactRepository(
                    session,
                    binding_repository=bindings,
                    cursor_signer=cursor_signer,
                    clock=self.clock,
                ),
                refs=SqlRefStore(session, cursor_signer=cursor_signer, clock=self.clock),
                object_bindings=bindings,
                findings=self.findings,
                finding_links=self.finding_links,
                runs=_RunReadAuthority(
                    SqlRunRepository(session),
                    self.synthetic_runs,
                    self.synthetic_prompt_links,
                ),
                routing=SqlCostLedger(session, clock=self.clock),
            )

    def seed_artifact(
        self,
        *,
        kind: str,
        tool_version: str,
        extra: str = "",
        domain_scope: DomainScope | None = None,
    ) -> str:
        from sqlalchemy.orm import Session

        payload = f"{kind}:{tool_version}:{extra}".encode("utf-8")
        stored = self.objects.put_verified(payload)
        payload_schema_id = {
            "ir_snapshot": "ir-core@1",
            "config_export": "config-export-package@1",
            "patch": "patch@2",
            "regression_suite": "regression-suite@1",
            "review_report": "review@1",
            "bench_dataset": "bench-dataset@1",
            "benchmark_spec": "benchmark-spec@1",
        }.get(kind)
        if payload_schema_id is None:
            raise AssertionError(f"test fixture has no schema for Artifact kind {kind!r}")
        artifact = build_artifact_v2(
            kind=kind,  # type: ignore[arg-type]
            version_tuple=VersionTuple(ir_snapshot_id=stored.ref.sha256, tool_version=tool_version),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": payload_schema_id,
                "domain_scope": (domain_scope or DomainScope(domain_ids=("economy",))).model_dump(
                    mode="json"
                ),
            },
            created_at=NOW,
        )
        with Session(self.engine) as session, session.begin():
            bindings = SqlObjectBindingRepository(session, self.objects, "local")
            bindings.bind_verified(stored.ref, stored.location, None)
            SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=self.clock),
                clock=self.clock,
            ).put(artifact)
        return artifact.artifact_id

    def load_artifact(self, artifact_id: str) -> ArtifactV2:
        artifact = self.artifact_record(artifact_id)
        assert isinstance(artifact, ArtifactV2)
        return artifact

    def artifact_record(self, artifact_id: str) -> ArtifactV2 | None:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            artifact = SqlArtifactRepository(
                session,
                binding_repository=SqlObjectBindingRepository(session, self.objects, "local"),
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=self.clock),
                clock=self.clock,
            ).get(artifact_id)
        assert artifact is None or isinstance(artifact, ArtifactV2)
        return artifact

    def bind_failed_repair_subject(
        self,
        *,
        subject_id: str,
        base_ref: RefValue,
        preview_id: str,
        evidence_id: str,
    ) -> ApprovalItem:
        subject = self.load_artifact(subject_id)
        preview = self.load_artifact(preview_id)
        initial = validation_testkit.approval_item(
            subject=subject,
            target=preview,
            kind="patch",
            approval_id=f"approval:patch:{subject_id}",
            workflow_revision=2,
        )
        binding = PatchTargetBindingV1(
            target_artifact_id=preview.artifact_id,
            target_snapshot_id=preview.version_tuple.ir_snapshot_id or "snapshot:preview",
            target_digest=preview.payload_hash,
            ref_name="content/head",
            expected_ref=base_ref,
        )
        item = ApprovalItem.model_validate(
            {
                **initial.model_dump(mode="json"),
                "status": "validation_failed",
                "active_validation_run_id": None,
                "last_validation_failure_artifact_id": None,
                "evidence_set_artifact_id": evidence_id,
                "target_binding": binding.model_dump(mode="json"),
            }
        )
        item = _exact_draft_item(
            self,
            item,
            domain_scope=DomainScope(domain_ids=("economy",)),
            status="validation_failed",
        )
        self.approvals = _FixedApprovals(item)
        return item

    def seed_payload_artifact(
        self,
        *,
        kind: str,
        payload: bytes | dict[str, Any],
        version_tuple: VersionTuple,
        lineage: tuple[str, ...] = (),
        payload_schema_id: str,
        domain_scope: DomainScope | None = None,
        meta_extra: dict[str, Any] | None = None,
    ) -> ArtifactV2:
        from sqlalchemy.orm import Session

        blob = payload if isinstance(payload, bytes) else canonical_json(payload).encode("utf-8")
        stored = self.objects.put_verified(blob)
        artifact = build_artifact_v2(
            kind=kind,  # type: ignore[arg-type]
            version_tuple=version_tuple,
            lineage=lineage,
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": payload_schema_id,
                **(
                    {}
                    if domain_scope is None
                    else {"domain_scope": domain_scope.model_dump(mode="json")}
                ),
                **(meta_extra or {}),
            },
            created_at=NOW,
        )
        with Session(self.engine) as session, session.begin():
            bindings = SqlObjectBindingRepository(session, self.objects, "local")
            bindings.bind_verified(stored.ref, stored.location, None)
            SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=self.clock),
                clock=self.clock,
            ).put(artifact)
        return artifact

    def run_record(self, run_id: str) -> Any:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            return SqlRunRepository(session).get(run_id)

    def reservation_group(self, run_id: str) -> Any:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            return SqlCostLedger(session, clock=self.clock).get_reservation_group(f"hold:{run_id}")

    def seed_budget(self, budget: BudgetV1) -> None:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session, session.begin():
            SqlCostLedger(session, clock=self.clock).put_budget(budget)

    def budget(self, budget_id: str) -> BudgetV1 | None:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            return SqlCostLedger(session, clock=self.clock).get_budget(budget_id)

    def budget_set(self, run_id: str) -> Any:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            return SqlCostLedger(session, clock=self.clock).get_budget_set(f"budget-set:{run_id}")

    def budget_reservations(self, run_id: str) -> Any:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            return SqlCostLedger(session, clock=self.clock).list_budget_reservations(
                f"hold:{run_id}"
            )

    def seed_ref(self, name: str, artifact_id: str) -> RefValue:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session, session.begin():
            return SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=self.clock),
                clock=self.clock,
            ).compare_and_set(name, None, artifact_id)

    def advance_ref(self, name: str, expected: RefValue, artifact_id: str) -> RefValue:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session, session.begin():
            return SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=self.clock),
                clock=self.clock,
            ).compare_and_set(name, expected, artifact_id)


def _seed_preview(
    harness: Harness,
    *,
    label: str,
    doc_version: str | None = None,
) -> ArtifactV2:
    return harness.seed_payload_artifact(
        kind="ir_snapshot",
        payload={"snapshot_id": f"snapshot:{label}", "entities": [], "relations": []},
        version_tuple=VersionTuple(
            doc_version=doc_version,
            ir_snapshot_id=f"snapshot:{label}",
            tool_version="ir@1",
        ),
        payload_schema_id="ir-core@1",
        domain_scope=DomainScope(domain_ids=("builtin",)),
    )


def _seed_patch_candidate(
    harness: Harness,
    *,
    label: str,
    base: ArtifactV2,
    constraint: ArtifactV2 | None = None,
) -> tuple[ArtifactV2, ArtifactV2]:
    target_snapshot_id = f"snapshot:{label}"
    patch = PatchV2(
        revision=1,
        base_snapshot_id=base.version_tuple.ir_snapshot_id or "",
        target_snapshot_id=target_snapshot_id,
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=[],
        produced_by="human",
        producer_run_id=None,
        rationale="patch validation fixture",
    )
    subject = harness.seed_payload_artifact(
        kind="patch",
        payload=patch.model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=base.version_tuple.ir_snapshot_id,
            constraint_snapshot_id=(
                None if constraint is None else constraint.version_tuple.constraint_snapshot_id
            ),
            tool_version="patch@2",
        ),
        lineage=(
            base.artifact_id,
            *(() if constraint is None else (constraint.artifact_id,)),
        ),
        payload_schema_id="patch@2",
        domain_scope=DomainScope(domain_ids=("builtin",)),
    )
    preview = harness.seed_payload_artifact(
        kind="ir_snapshot",
        payload={"snapshot_id": target_snapshot_id, "entities": [], "relations": []},
        version_tuple=VersionTuple(
            ir_snapshot_id=target_snapshot_id,
            constraint_snapshot_id=(
                None if constraint is None else constraint.version_tuple.constraint_snapshot_id
            ),
            tool_version="patch@2",
        ),
        lineage=(base.artifact_id, subject.artifact_id),
        payload_schema_id="ir-core@1",
        domain_scope=DomainScope(domain_ids=("builtin",)),
    )
    return subject, preview


def _seed_constraint(
    harness: Harness,
    *,
    snapshot_id: str = "constraint:snapshot@1",
) -> ArtifactV2:
    return harness.seed_payload_artifact(
        kind="constraint_snapshot",
        payload={"constraint_snapshot_id": snapshot_id, "constraints": []},
        version_tuple=VersionTuple(constraint_snapshot_id=snapshot_id, tool_version="constraint@1"),
        payload_schema_id="constraint-snapshot@1",
        domain_scope=DomainScope(domain_ids=("builtin",)),
    )


def _seed_config(
    harness: Harness,
    *,
    label: str,
    preview: ArtifactV2,
    constraint: ArtifactV2,
    doc_version_override: str | None = None,
) -> ArtifactV2:
    content = f"candidate={label}\n".encode()
    package = ConfigExportPackageV1(
        export_profile=CONFIG_EXPORT_PROFILE,
        target_environment_profile=ENVIRONMENT_PROFILE,
        env_contract_version=ENV_CONTRACT_VERSION,
        source_preview_artifact_id=preview.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        format_schema_id="config-export-files@1",
        files=(
            ConfigExportFileV1(
                relative_path="candidate.txt",
                media_type="text/plain",
                content_sha256=sha256_lowerhex(content),
                size_bytes=len(content),
                content_bytes=content,
            ),
        ),
    )
    return harness.seed_payload_artifact(
        kind="config_export",
        payload=canonical_config_export_bytes(package),
        version_tuple=VersionTuple(
            doc_version=(
                preview.version_tuple.doc_version
                if doc_version_override is None
                else doc_version_override
            ),
            ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
            constraint_snapshot_id=constraint.version_tuple.constraint_snapshot_id,
            env_contract_version=ENV_CONTRACT_VERSION,
            tool_version="config-export@1",
        ),
        lineage=(preview.artifact_id, constraint.artifact_id),
        payload_schema_id="config-export-package@1",
    )


def _seed_task_suite(
    harness: Harness,
    *,
    preview: ArtifactV2,
    config: ArtifactV2,
    constraint: ArtifactV2,
) -> tuple[ArtifactV2, ArtifactV2, TaskEpisodeV1]:
    reset = ScenarioResetBindingV1(
        reset_schema_id=RESET_SCHEMA_ID,
        payload_hash=canonical_sha256({"scenario_id": "scenario:old"}),
        payload={"scenario_id": "scenario:old"},
    )
    tuple_basis = VersionTuple(
        doc_version=preview.version_tuple.doc_version,
        ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
        constraint_snapshot_id=constraint.version_tuple.constraint_snapshot_id,
        env_contract_version=ENV_CONTRACT_VERSION,
        tool_version="task-suite@1",
    )
    scenario_payload = ScenarioSpecV1(
        scenario_id="scenario:old",
        source_preview_artifact_id=preview.artifact_id,
        config_export_artifact_id=config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        environment_profile=ENVIRONMENT_PROFILE,
        env_contract_version=ENV_CONTRACT_VERSION,
        domain_scope=DomainScope(domain_ids=("builtin",)),
        reset_binding=reset,
    )
    scenario = harness.seed_payload_artifact(
        kind="scenario_spec",
        payload=scenario_payload.model_dump(mode="json"),
        version_tuple=tuple_basis,
        lineage=(preview.artifact_id, config.artifact_id, constraint.artifact_id),
        payload_schema_id="scenario-spec@1",
    )
    oracle = CompletionOracleRefV1(
        oracle_id="bounded-progress",
        version=1,
        params_schema_id="bounded-progress-params@1",
        params={"maximum_steps": 32},
    )
    episode = TaskEpisodeV1(
        episode_id="episode:old",
        scenario_spec_artifact_id=scenario.artifact_id,
        completion_oracle=oracle,
        domain_scope=scenario_payload.domain_scope,
        reset_binding=reset,
        step_budget=32,
    )
    oracle_registry = harness.registry.completion_oracle_registries[0]
    suite_payload = TaskSuiteV1(
        suite_profile=TASK_SUITE_PROFILE,
        source_preview_artifact_id=preview.artifact_id,
        config_export_artifact_id=config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        environment_profile=ENVIRONMENT_PROFILE,
        env_contract_version=ENV_CONTRACT_VERSION,
        completion_oracle_registry_ref=CompletionOracleRegistryRefV1(
            registry_version=oracle_registry.registry_version,
            digest=oracle_registry.registry_digest,
        ),
        episodes=(episode,),
    )
    suite = harness.seed_payload_artifact(
        kind="task_suite",
        payload=suite_payload.model_dump(mode="json"),
        version_tuple=tuple_basis,
        lineage=(
            preview.artifact_id,
            config.artifact_id,
            constraint.artifact_id,
            scenario.artifact_id,
        ),
        payload_schema_id="task-suite@1",
    )
    return suite, scenario, episode


def _assert_no_admission_side_effects(
    harness: Harness,
    *,
    key: str,
    scope: str = "principal:human:actor",
) -> None:
    request_hash = canonical_sha256({"key": key})
    run_id = harness.engine_admission._derive_run_id(
        scope=scope,
        key=key,
        request_hash=request_hash,
    )
    assert harness.run_record(run_id) is None
    assert harness.reservation_group(run_id) is None


def _plan(
    run_kind: str = "generation.propose",
    *,
    graph_version: str | None = None,
) -> ExecutionVersionPlanV1:
    catalog, routing = _model_authorities()
    registry = build_builtin_registry()
    selected_graph_version = graph_version
    if selected_graph_version is None and run_kind == "playtest.run":
        selected_graph_version = "playtest-core-graph@1"
    graph = next(
        item
        for item in registry.list_agent_execution_graphs()
        if item.run_kind == RunKindRef(kind=run_kind, version=1)
        and item.status == "active"
        and (selected_graph_version is None or item.agent_graph_version == selected_graph_version)
    )
    plan = {
        "agent_graph_version": graph.agent_graph_version,
        "nodes": tuple(
            PlannedAgentNodeVersionV1(
                agent_node_id=node.agent_node_id,
                prompt_version=node.prompt_version,
                tool_version=node.tool_version,
                allowed_model_snapshots=("test:model@1",),
            )
            for node in graph.nodes
        ),
        "model_catalog_version": catalog.catalog_version,
        "model_catalog_digest": catalog.catalog_digest,
        "routing_policy_version": routing.policy_version,
        "routing_policy_digest": routing.routing_policy_digest,
    }
    return ExecutionVersionPlanV1(**plan, plan_digest=execution_version_plan_digest(plan))


def _replace_plan(
    plan: ExecutionVersionPlanV1,
    **updates: Any,
) -> ExecutionVersionPlanV1:
    body = plan.model_dump(mode="python", exclude={"plan_digest"})
    body.update(updates)
    return ExecutionVersionPlanV1(
        **body,
        plan_digest=execution_version_plan_digest(body),
    )


def _seed_failed_repair_case(
    harness: Harness,
) -> tuple[str, str, str, str, RefValue, ApprovalItem]:
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1", extra="base")
    base_artifact = harness.load_artifact(base)
    patch = PatchV2(
        revision=1,
        base_snapshot_id=base_artifact.version_tuple.ir_snapshot_id or "",
        target_snapshot_id="snapshot:repair-preview",
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=[],
        produced_by="human",
        producer_run_id=None,
        rationale="repair admission fixture",
    )
    subject = harness.seed_payload_artifact(
        kind="patch",
        payload=patch.model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=base_artifact.version_tuple.ir_snapshot_id,
            tool_version="patch@2",
        ),
        lineage=(base,),
        payload_schema_id="patch@2",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )
    preview = harness.seed_payload_artifact(
        kind="ir_snapshot",
        payload={"snapshot_id": patch.target_snapshot_id, "entities": [], "relations": []},
        version_tuple=VersionTuple(
            ir_snapshot_id=patch.target_snapshot_id,
            tool_version="patch@2",
        ),
        lineage=(base, subject.artifact_id),
        payload_schema_id="ir-core@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )
    base_ref = harness.seed_ref("content/head", base)
    initial = validation_testkit.approval_item(
        subject=subject,
        target=preview,
        kind="patch",
        approval_id=f"approval:patch:{subject.artifact_id}",
        workflow_revision=2,
    )
    binding = PatchTargetBindingV1(
        target_artifact_id=preview.artifact_id,
        target_snapshot_id=preview.version_tuple.ir_snapshot_id or "",
        target_digest=preview.payload_hash,
        ref_name="content/head",
        expected_ref=base_ref,
    )
    item = ApprovalItem.model_validate(
        {
            **initial.model_dump(mode="json"),
            "status": "draft",
            "active_validation_run_id": None,
            "last_validation_failure_artifact_id": None,
            "target_binding": binding.model_dump(mode="json"),
        }
    )
    item = _exact_draft_item(
        harness,
        item,
        domain_scope=DomainScope(domain_ids=("economy",)),
        status="draft",
    )
    support = harness.seed_payload_artifact(
        kind="regression_evidence",
        payload={"requirement_id": "repair-source", "status": "failed"},
        version_tuple=VersionTuple(
            ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
            tool_version="patch-validation@1",
        ),
        lineage=(preview.artifact_id,),
        payload_schema_id="regression-evidence@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )
    validation_run_id = "run:validation:repair-source"
    evidence = EvidenceSet(
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        policy_version="validation@1",
        validation_run_id=validation_run_id,
        target_binding=binding,
        supporting_artifact_ids=(support.artifact_id,),
        finding_bindings=(),
        requirements=(
            EvidenceRequirement(
                requirement_id="repair-source",
                kind="regression",
                applicability="required",
                status="failed",
                evidence_artifact_id=support.artifact_id,
                tool_version="patch-validation@1",
            ),
        ),
        overall_status="failed",
    )
    evidence_artifact = harness.seed_payload_artifact(
        kind="validation_evidence",
        payload=evidence.model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
            tool_version="patch-validation@1",
        ),
        lineage=(subject.artifact_id, preview.artifact_id, support.artifact_id),
        payload_schema_id="evidence-set@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )
    item = ApprovalItem.model_validate(
        {
            **item.model_dump(mode="json"),
            "status": "validation_failed",
            "evidence_set_artifact_id": evidence_artifact.artifact_id,
        }
    )
    harness.approvals = _FixedApprovals(item)
    validation_params = PatchValidationPayloadV1(
        subject=ValidationSubjectBindingV1(
            approval_id=item.approval_id,
            expected_workflow_revision=item.workflow_revision,
            subject_head_revision=item.subject_revision,
            subject_artifact_id=subject.artifact_id,
            subject_digest=subject.payload_hash,
            active_validation_run_id=validation_run_id,
        ),
        base_snapshot_artifact_id=base,
        preview_snapshot_artifact_id=preview.artifact_id,
        candidate_config_export_artifact_ids=(),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=base_ref),
        validation_policy=VALIDATION_PROFILE,
        checker_profiles=(),
        simulation_profiles=(),
        findings=(),
        review_artifact_ids=(),
        playtest_trace_artifact_ids=(),
        regression_suite_artifact_ids=(),
    )
    from tests.platform.m4c.handler_support import build_envelope, build_run_record

    validation_run = build_run_record(
        build_envelope(params=validation_params),
        RunKindRef(kind="patch.validate", version=1),
        run_id=validation_run_id,
    ).model_copy(update={"status": "succeeded"})
    harness.synthetic_runs[validation_run_id] = validation_run
    return (
        subject.artifact_id,
        base,
        preview.artifact_id,
        evidence_artifact.artifact_id,
        base_ref,
        item,
    )


def _bind_retained_finding(
    harness: Harness,
    *,
    evidence_artifact_id: str,
    retain_link: bool = True,
) -> FindingEvidenceBindingV1:
    evidence_artifact = harness.load_artifact(evidence_artifact_id)
    snapshot_id = evidence_artifact.version_tuple.ir_snapshot_id
    assert snapshot_id is not None
    revision = FindingRevisionV1(
        finding_id="finding:admission:1",
        revision=1,
        created_at=NOW,
        payload=FindingPayloadV1(
            source="checker",
            producer_id="checker:graph",
            producer_run_id="run:finding-producer",
            oracle_type="deterministic",
            defect_class="economy.balance",
            severity="major",
            snapshot_id=snapshot_id,
            entities=["reward:1"],
            relations=[],
            evidence={"predicate": "net_inflow <= sink"},
            minimal_repro={},
            status="confirmed",
            confidence=None,
            message="net inflow exceeds the retained sink",
        ),
    )
    digest = finding_revision_digest(revision)
    link = RunFindingLinkV1(
        run_id=revision.payload.producer_run_id,
        attempt_no=1,
        ordinal=1,
        finding_id=revision.finding_id,
        finding_revision=revision.revision,
        finding_digest=digest,
        evidence_artifact_id=evidence_artifact_id,
    )
    harness.findings = _FixedFindings(revision)
    harness.finding_links = _FixedFindingLinks(link if retain_link else None)
    return FindingEvidenceBindingV1(
        finding_id=revision.finding_id,
        finding_revision=revision.revision,
        evidence_artifact_id=evidence_artifact_id,
        finding_digest=digest,
    )


def _rollback_admission_fixture(
    harness: Harness,
    *,
    extra_lineage_artifact_id: str | None = None,
    omit_current_lineage: bool = False,
) -> tuple[ArtifactV2, ArtifactV2, RefValue, ApprovalItem, RollbackValidationAdmissionRequestV1]:
    target = harness.seed_payload_artifact(
        kind="constraint_snapshot",
        payload={"constraint_snapshot_id": "constraint:rollback-target", "constraints": []},
        version_tuple=VersionTuple(
            constraint_snapshot_id="constraint:rollback-target",
            tool_version="constraint@1",
        ),
        payload_schema_id="constraint-snapshot@1",
        domain_scope=DomainScope(domain_ids=("builtin",)),
    )
    current = harness.seed_payload_artifact(
        kind="constraint_snapshot",
        payload={"constraint_snapshot_id": "constraint:current", "constraints": []},
        version_tuple=VersionTuple(
            constraint_snapshot_id="constraint:current",
            tool_version="constraint@1",
        ),
        payload_schema_id="constraint-snapshot@1",
        domain_scope=DomainScope(domain_ids=("builtin",)),
    )
    historical_ref = harness.seed_ref("constraints/head", target.artifact_id)
    current_ref = harness.advance_ref("constraints/head", historical_ref, current.artifact_id)
    with harness._read_scope() as read:  # noqa: SLF001 - exact retained profile fixture
        profile_binding = read.policies.resolve_execution_profile(
            catalog_version=harness.catalog.catalog_version,
            catalog_digest=harness.catalog.catalog_digest,
            field_path="/params/rollback_profile",
            profile=ROLLBACK_PROFILE,
            expected_profile_kind="rollback",
        )
    rollback = RollbackRequestV1(
        ref_name="constraints/head",
        expected_current_ref=current_ref,
        target_artifact_id=target.artifact_id,
        target_history_revision=historical_ref.revision,
        rollback_profile_binding=profile_binding,
        reason="restore the retained constraint snapshot",
    )
    subject = harness.seed_payload_artifact(
        kind="rollback_request",
        payload=rollback.model_dump(mode="json"),
        version_tuple=target.version_tuple.model_copy(
            update={"tool_version": "rollback-request@1"}
        ),
        lineage=(
            *(() if omit_current_lineage else (current.artifact_id,)),
            target.artifact_id,
            *(() if extra_lineage_artifact_id is None else (extra_lineage_artifact_id,)),
        ),
        payload_schema_id="rollback-request@1",
    )
    item = validation_testkit.approval_item(
        subject=subject,
        target=target,
        kind="rollback_request",
        approval_id=f"approval:rollback_request:{subject.artifact_id}",
        rollback_profile_binding=profile_binding,
    )
    exact_binding = RollbackTargetBindingV1(
        target_artifact_kind=target.kind,
        target_artifact_id=target.artifact_id,
        target_snapshot_id=target.version_tuple.constraint_snapshot_id,
        target_digest=target.payload_hash,
        ref_name="constraints/head",
        expected_ref=current_ref,
        rollback_profile_binding=profile_binding,
    )
    item = ApprovalItem.model_validate(
        {
            **item.model_dump(mode="json"),
            "target_binding": exact_binding.model_dump(mode="json"),
        }
    )
    item = _exact_draft_item(
        harness,
        item,
        domain_scope=DomainScope(domain_ids=("builtin",)),
    )
    request = RollbackValidationAdmissionRequestV1(
        approval_id=item.approval_id,
        expected_subject_head_revision=item.subject_revision,
        expected_workflow_revision=item.workflow_revision,
        subject_digest=item.subject_digest,
        ref_name="constraints/head",
        expected_current_ref=current_ref,
        target_artifact_id=target.artifact_id,
        target_history_revision=historical_ref.revision,
        rollback_profile=ROLLBACK_PROFILE,
        schema_compatibility_policy=SCHEMA_COMPATIBILITY_PROFILE,
        impact_profiles=(),
        regression_suite_artifact_ids=(),
    )
    harness.approvals = _FixedApprovals(item)
    return target, current, current_ref, item, request


# ── generic POST /runs happy path (one UoW: record + event + hold + audit) ───
def test_generic_checker_run_creates_queued_run_with_budget_hold(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=CHECKER_PROFILE,
        checker_ids=(),
        defect_classes=(),
    )
    accepted = harness.engine_admission.admit_generic_run(
        params=params, actor=_tooling_actor(), server=_server("checker:1")
    )
    assert accepted.accepted_schema_version == "run-accepted@1"
    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.status == "queued"
    assert run.kind.kind == "checker.run"
    assert run.payload.llm_execution_mode == "not_applicable"
    assert run.payload.seed is None
    # budget hold retained by the same UoW
    hold = harness.reservation_group(accepted.run_id)
    assert hold is not None
    assert hold.status == "reserved"
    assert run.run_budget_hold_group_id == hold.reservation_group_id
    # initial run.queued event retained (event seq 1)
    from sqlalchemy.orm import Session

    with Session(harness.engine) as session:
        event = SqlRunRepository(session).get_event(accepted.run_id, 1)
    assert event is not None and event.event_type == "run.queued"


def test_review_without_llm_triage_admits_na_branch_without_agent_graph(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    params = ReviewRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=REVIEW_PROFILE,
        checker_profiles=(),
        simulation_profiles=(),
        llm_triage_policy=None,
    )

    accepted = harness.engine_admission.admit_generic_run(
        params=params,
        actor=_tooling_actor(),
        server=_server("review:deterministic-only"),
        llm_execution_mode="not_applicable",
        execution_version_plan=None,
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.payload.execution_version_plan is None
    assert run.payload.version_tuple.prompt_version is None
    assert run.payload.version_tuple.model_snapshot is None
    assert run.payload.version_tuple.agent_graph_version is None


def test_run_kind_mode_and_plan_shape_fail_before_run_creation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")

    with pytest.raises(Conflict, match="not allowed"):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=_server("checker:illegal-model-mode"),
            llm_execution_mode="record",
            execution_version_plan=_plan("review.run"),
        )
    with pytest.raises(Conflict, match="forbids a plan and cassette"):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=_server("checker:na-with-plan"),
            llm_execution_mode="not_applicable",
            execution_version_plan=_plan("review.run"),
        )

    _assert_no_admission_side_effects(harness, key="checker:illegal-model-mode")
    _assert_no_admission_side_effects(harness, key="checker:na-with-plan")


@pytest.mark.parametrize(
    ("llm_triage_policy", "mode", "plan", "message"),
    (
        (None, "record", "review.run", "without an LLM triage profile"),
        (LLM_TRIAGE_PROFILE, "not_applicable", None, "requires model execution"),
    ),
)
def test_review_triage_profile_and_execution_mode_are_exactly_cross_bound(
    tmp_path: Path,
    llm_triage_policy: ProfileRefV1 | None,
    mode: str,
    plan: str | None,
    message: str,
) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    params = ReviewRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=REVIEW_PROFILE,
        checker_profiles=(),
        simulation_profiles=(),
        llm_triage_policy=llm_triage_policy,
    )
    key = f"review:triage-mode:{mode}:{llm_triage_policy is not None}"

    with pytest.raises(Conflict, match=message):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(key),
            llm_execution_mode=mode,  # type: ignore[arg-type]
            execution_version_plan=None if plan is None else _plan(plan),
        )

    _assert_no_admission_side_effects(harness, key=key)


def test_seed_policy_is_preflighted_for_required_forbidden_and_profile_dependent(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    simulation = SimulationRunPayloadV1(
        snapshot_artifact_id=snapshot,
        simulation_profile=SIMULATION_PROFILE,
        workload_profile=WORKLOAD_PROFILE,
        replication_count=1,
        horizon_steps=1,
    )
    with pytest.raises(Conflict, match="requires an explicit root seed"):
        harness.engine_admission.admit_generic_run(
            params=simulation,
            actor=_tooling_actor(),
            server=_server("simulation:missing-seed"),
            seed=None,
        )
    with pytest.raises(Conflict, match="forbids a root seed"):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=_server("checker:fabricated-seed"),
            seed=7,
        )

    stochastic_review = ReviewRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=REVIEW_PROFILE,
        checker_profiles=(),
        simulation_profiles=(SIMULATION_PROFILE,),
        llm_triage_policy=None,
    )
    with pytest.raises(Conflict, match="profile-dependent seed is required"):
        harness.engine_admission.admit_generic_run(
            params=stochastic_review,
            actor=_tooling_actor(),
            server=_server("review:missing-profile-seed"),
            seed=None,
        )

    deterministic_review = stochastic_review.model_copy(update={"simulation_profiles": ()})
    with pytest.raises(Conflict, match="profile-dependent seed is forbidden"):
        harness.engine_admission.admit_generic_run(
            params=deterministic_review,
            actor=_tooling_actor(),
            server=_server("review:fabricated-profile-seed"),
            seed=7,
        )

    for key in (
        "simulation:missing-seed",
        "checker:fabricated-seed",
        "review:missing-profile-seed",
        "review:fabricated-profile-seed",
    ):
        _assert_no_admission_side_effects(harness, key=key)


def test_generic_checker_rejects_ir_kind_with_wrong_payload_schema(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_payload_artifact(
        kind="ir_snapshot",
        payload={"snapshot_id": "snapshot:wrong-schema", "entities": [], "relations": []},
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:wrong-schema",
            tool_version="snap@1",
        ),
        payload_schema_id="review@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )

    with pytest.raises(IntegrityViolation, match="payload schema is not allowed"):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot.artifact_id),
            actor=_tooling_actor(),
            server=_server("checker:wrong-input-schema"),
        )

    _assert_no_admission_side_effects(harness, key="checker:wrong-input-schema")


def test_generic_checker_rejects_profile_outside_resource_domain(tmp_path: Path) -> None:
    harness = Harness(tmp_path, profile_domain_ids=("builtin",))
    snapshot = harness.seed_artifact(
        kind="ir_snapshot",
        tool_version="snap@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )

    with pytest.raises(Conflict, match="profile does not cover"):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=_server("checker:profile-domain"),
        )

    _assert_no_admission_side_effects(harness, key="checker:profile-domain")


@pytest.mark.parametrize(
    ("profile_update", "message", "key"),
    [
        (
            {"compatible_run_kinds": (RunKindRef(kind="review.run", version=1),)},
            "incompatible",
            "checker:profile-kind-incompatible",
        ),
        (
            {"input_schema_ids": ("review-run@1",)},
            "does not accept",
            "checker:profile-input-incompatible",
        ),
        (
            {"output_schema_ids": ()},
            "output schema interface is incomplete",
            "checker:profile-output-incompatible",
        ),
    ],
)
def test_profile_contract_mismatch_is_conflict_without_run_or_hold(
    tmp_path: Path,
    profile_update: dict[str, Any],
    message: str,
    key: str,
) -> None:
    harness = Harness(
        tmp_path,
        profile_updates={"builtin.checker": profile_update},
    )
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")

    with pytest.raises(Conflict, match=message):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=_server(key),
        )

    _assert_no_admission_side_effects(harness, key=key)


def test_profile_kind_mismatch_is_conflict_without_run_or_hold(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=REVIEW_PROFILE,
        checker_ids=(),
        defect_classes=(),
    )

    with pytest.raises(Conflict, match="kind differs"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("checker:profile-kind-mismatch"),
        )

    _assert_no_admission_side_effects(harness, key="checker:profile-kind-mismatch")


def test_model_profile_capabilities_must_close_over_exact_agent_graph(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path,
        profile_updates={
            "builtin.generation": {"required_capabilities": ("vision",)},
        },
    )
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")

    with pytest.raises(Conflict, match="capabilities are absent"):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(),
            objective_goal_text="Generate a bounded economy proposal.",
            domain_scope=DomainScope(domain_ids=("economy",)),
            target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
            generation_policy=GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=_tooling_actor(),
            server=_server("generation:profile-capability"),
            llm_execution_mode="record",
            execution_version_plan=_plan("generation.propose"),
        )

    _assert_no_admission_side_effects(harness, key="generation:profile-capability")


@pytest.mark.parametrize("drift", ["graph", "prompt", "tool", "extra_node"])
def test_generation_rejects_execution_plan_outside_retained_agent_graph(
    tmp_path: Path,
    drift: str,
) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    plan = _plan()
    if drift == "graph":
        plan = _replace_plan(plan, agent_graph_version="unretained-generation-graph@1")
    elif drift in {"prompt", "tool"}:
        node = plan.nodes[0]
        plan = _replace_plan(
            plan,
            nodes=(
                node.model_copy(
                    update={
                        "prompt_version": (
                            "unretained-generation-prompt@1"
                            if drift == "prompt"
                            else node.prompt_version
                        ),
                        "tool_version": (
                            "unretained-generation-tool@1" if drift == "tool" else node.tool_version
                        ),
                    }
                ),
            ),
        )
    else:
        plan = _replace_plan(
            plan,
            nodes=(
                *plan.nodes,
                PlannedAgentNodeVersionV1(
                    agent_node_id="unretained-generation-node",
                    prompt_version="unretained-prompt@1",
                    tool_version="unretained-tool@1",
                    allowed_model_snapshots=("test:model@1",),
                ),
            ),
        )
    key = f"generation:graph-drift:{drift}"

    with pytest.raises(IntegrityViolation):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(),
            objective_goal_text="Reject an execution plan outside retained authority.",
            domain_scope=DomainScope(domain_ids=("economy",)),
            target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
            generation_policy=GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=_tooling_actor(),
            server=_server(key),
            llm_execution_mode="record",
            execution_version_plan=plan,
        )

    _assert_no_admission_side_effects(harness, key=key)


def test_run_admission_freezes_and_reserves_every_applicable_budget_scope(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, provision_shared_budgets=False)
    principal = _shared_budget(
        budget_id="budget:principal:human:actor",
        scope_kind="principal",
        scope_id="human:actor",
    )
    system = _shared_budget(
        budget_id="budget:system:global",
        scope_kind="system",
        scope_id="global",
    )
    harness.seed_budget(principal)
    harness.seed_budget(system)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")

    accepted = harness.engine_admission.admit_generic_run(
        params=_checker_params(snapshot),
        actor=_tooling_actor(),
        server=_server("checker:three-level-budget"),
    )

    budget_set = harness.budget_set(accepted.run_id)
    assert budget_set is not None
    assert tuple(
        (item.scope_kind, item.scope_id, item.budget_id) for item in budget_set.snapshots
    ) == (
        ("run", accepted.run_id, f"budget:run:{accepted.run_id}"),
        ("principal", "human:actor", principal.budget_id),
        ("system", "global", system.budget_id),
    )
    reservations = harness.budget_reservations(accepted.run_id)
    assert {item.budget_id for item in reservations} == {
        f"budget:run:{accepted.run_id}",
        principal.budget_id,
        system.budget_id,
    }
    expected_shared_reservation = (
        CostAmountV1(dimension="request", value=1_000_000, unit="request"),
    )
    by_budget_id = {item.budget_id: item for item in reservations}
    run_reservation = by_budget_id[f"budget:run:{accepted.run_id}"]
    run_snapshot = next(item for item in budget_set.snapshots if item.scope_kind == "run")
    assert run_reservation.reserved == tuple(
        item for item in run_snapshot.limits if item.dimension != "concurrent_run"
    )
    assert {item.dimension for item in run_reservation.reserved} == {
        "input_token",
        "output_token",
        "cache_read_token",
        "cache_write_token",
        "request",
        "agent_step",
        "wall_time_ns",
    }
    assert by_budget_id[principal.budget_id].reserved == expected_shared_reservation
    assert by_budget_id[system.budget_id].reserved == expected_shared_reservation
    for reservation in reservations:
        current = harness.budget(reservation.budget_id)
        assert current is not None
        assert current.reserved == reservation.reserved
        assert current.revision == 2


def test_run_admission_freezes_every_budget_within_each_applicable_scope(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, provision_shared_budgets=False)
    server = _server("checker:multi-budget-scope")
    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    configured = (
        _shared_budget(
            budget_id="budget:run:daily",
            scope_kind="run",
            scope_id=run_id,
        ),
        _shared_budget(
            budget_id="budget:principal:daily",
            scope_kind="principal",
            scope_id="human:actor",
        ),
        _shared_budget(
            budget_id="budget:principal:monthly",
            scope_kind="principal",
            scope_id="human:actor",
        ),
        _shared_budget(
            budget_id="budget:system:daily",
            scope_kind="system",
            scope_id="global",
        ),
        _shared_budget(
            budget_id="budget:system:monthly",
            scope_kind="system",
            scope_id="global",
        ),
    )
    for budget in configured:
        harness.seed_budget(budget)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")

    accepted = harness.engine_admission.admit_generic_run(
        params=_checker_params(snapshot),
        actor=_tooling_actor(),
        server=server,
    )

    assert accepted.run_id == run_id
    budget_set = harness.budget_set(run_id)
    assert budget_set is not None
    expected_ids = {
        f"budget:run:{run_id}",
        *(budget.budget_id for budget in configured),
    }
    assert {item.budget_id for item in budget_set.snapshots} == expected_ids
    assert {item.budget_id for item in harness.budget_reservations(run_id)} == expected_ids
    assert [item.scope_kind for item in budget_set.snapshots] == [
        "run",
        "run",
        "principal",
        "principal",
        "system",
        "system",
    ]


def test_rejecting_secondary_budget_cannot_be_bypassed_by_scope_selection(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, provision_shared_budgets=False)
    budgets = (
        _shared_budget(
            budget_id="budget:principal:active",
            scope_kind="principal",
            scope_id="human:actor",
        ),
        _shared_budget(
            budget_id="budget:principal:closed",
            scope_kind="principal",
            scope_id="human:actor",
            status="closed",
        ),
        _shared_budget(
            budget_id="budget:system:global",
            scope_kind="system",
            scope_id="global",
        ),
    )
    for budget in budgets:
        harness.seed_budget(budget)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    server = _server("checker:secondary-closed-budget")

    with pytest.raises(QuotaExceeded, match="not active"):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=server,
        )

    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    assert harness.run_record(run_id) is None
    assert harness.budget_set(run_id) is None
    assert harness.budget(f"budget:run:{run_id}") is None
    for budget in budgets:
        assert harness.budget(budget.budget_id) == budget


def test_budget_scope_enumeration_overflow_fails_closed_without_truncation(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, provision_shared_budgets=False)
    for ordinal in range(_MAX_APPLICABLE_BUDGETS_PER_SCOPE + 1):
        harness.seed_budget(
            _shared_budget(
                budget_id=f"budget:principal:{ordinal:03d}",
                scope_kind="principal",
                scope_id="human:actor",
            )
        )
    harness.seed_budget(
        _shared_budget(
            budget_id="budget:system:global",
            scope_kind="system",
            scope_id="global",
        )
    )
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    server = _server("checker:budget-selection-overflow")

    with pytest.raises(IntegrityViolation, match="bounded policy"):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=server,
        )

    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    assert harness.run_record(run_id) is None
    assert harness.budget_set(run_id) is None
    assert harness.budget(f"budget:run:{run_id}") is None


def test_missing_shared_budget_fails_closed_without_partial_run_hold(tmp_path: Path) -> None:
    harness = Harness(tmp_path, provision_shared_budgets=False)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    server = _server("checker:missing-shared-budget")

    with pytest.raises(DependencyUnavailable, match="shared budget"):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=server,
        )

    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    assert harness.run_record(run_id) is None
    assert harness.budget_set(run_id) is None
    assert harness.reservation_group(run_id) is None
    assert harness.budget(f"budget:run:{run_id}") is None


def test_permit_only_shared_budget_is_not_silently_omitted_from_run_hold(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, provision_shared_budgets=False)
    principal = BudgetV1(
        budget_id="budget:principal:concurrency-only",
        scope_kind="principal",
        scope_id="human:actor",
        policy_version="shared-budget-policy@1",
        limits=(CostAmountV1(dimension="concurrent_run", value=3, unit="count"),),
        reserved=(),
        consumed=(),
        status="active",
        revision=1,
        created_at=NOW_DT,
    )
    harness.seed_budget(principal)
    harness.seed_budget(
        _shared_budget(
            budget_id="budget:system:global",
            scope_kind="system",
            scope_id="global",
        )
    )
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    server = _server("checker:permit-only-shared-budget")

    with pytest.raises(IntegrityViolation, match="run-hold cost dimension"):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=server,
        )

    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    assert harness.run_record(run_id) is None
    assert harness.budget_set(run_id) is None
    assert harness.reservation_group(run_id) is None
    assert harness.budget(f"budget:run:{run_id}") is None


def test_retained_run_budget_identity_cannot_redirect_admission_scope(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    server = _server("checker:run-budget-identity-collision")
    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    conflicting = BudgetV1(
        budget_id=f"budget:run:{run_id}",
        scope_kind="system",
        scope_id="redirected",
        policy_version="untrusted-budget-policy@1",
        limits=(CostAmountV1(dimension="request", value=10_000_000, unit="request"),),
        reserved=(),
        consumed=(),
        status="active",
        revision=1,
        created_at=NOW_DT,
    )
    harness.seed_budget(conflicting)

    with pytest.raises(IntegrityViolation, match="versioned admission policy"):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=server,
        )

    assert harness.run_record(run_id) is None
    assert harness.budget_set(run_id) is None
    assert harness.reservation_group(run_id) is None
    assert harness.budget(conflicting.budget_id) == conflicting


@pytest.mark.parametrize(
    ("failing_scope", "failing_status", "request_limit"),
    (
        ("principal", "closed", 8),
        ("system", "active", 0),
    ),
)
def test_shared_budget_rejection_rolls_back_the_entire_run_admission(
    tmp_path: Path,
    failing_scope: str,
    failing_status: str,
    request_limit: int,
) -> None:
    harness = Harness(tmp_path, provision_shared_budgets=False)
    principal = _shared_budget(
        budget_id="budget:principal:human:actor",
        scope_kind="principal",
        scope_id="human:actor",
        request_limit=request_limit if failing_scope == "principal" else 10_000_000,
        status=failing_status if failing_scope == "principal" else "active",
    )
    system = _shared_budget(
        budget_id="budget:system:global",
        scope_kind="system",
        scope_id="global",
        request_limit=request_limit if failing_scope == "system" else 10_000_000,
        status=failing_status if failing_scope == "system" else "active",
    )
    harness.seed_budget(principal)
    harness.seed_budget(system)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    server = _server(f"checker:shared-budget-reject:{failing_scope}")

    with pytest.raises(QuotaExceeded):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_tooling_actor(),
            server=server,
        )

    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    assert harness.run_record(run_id) is None
    assert harness.budget_set(run_id) is None
    assert harness.reservation_group(run_id) is None
    assert harness.budget_reservations(run_id) == ()
    assert harness.budget(f"budget:run:{run_id}") is None
    assert harness.budget(principal.budget_id) == principal
    assert harness.budget(system.budget_id) == system


def test_checker_authorizes_exact_snapshot_domain_not_all_active_domains(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    economy = DomainScope(domain_ids=("economy",))
    snapshot = harness.seed_artifact(
        kind="ir_snapshot",
        tool_version="snap@1",
        domain_scope=economy,
    )
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=CHECKER_PROFILE,
        checker_ids=(),
        defect_classes=(),
    )
    scoped_actor = _actor(
        "human",
        _assignment(role="tooling", scope=economy, assignment_id="assign:economy-tooling"),
    )

    accepted = harness.engine_admission.admit_generic_run(
        params=params,
        actor=scoped_actor,
        server=_server("checker:economy-scope"),
    )

    assert harness.run_record(accepted.run_id) is not None


def test_checker_missing_resource_domain_fails_without_run_or_hold(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_payload_artifact(
        kind="ir_snapshot",
        payload={"snapshot_id": "snapshot:unscoped", "entities": [], "relations": []},
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:unscoped", tool_version="ir@1"),
        payload_schema_id="ir-core@1",
    )
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot.artifact_id,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=CHECKER_PROFILE,
        checker_ids=(),
        defect_classes=(),
    )

    with pytest.raises(IntegrityViolation, match="resource-domain binding"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("checker:missing-domain"),
        )

    _assert_no_admission_side_effects(harness, key="checker:missing-domain")


def _seed_benchmark_spec(
    harness: Harness,
    *,
    dataset_artifact_id: str,
    domain_scope: DomainScope,
    partitions: tuple[BenchmarkPartitionV1, ...],
    dataset_payload_hash: str | None = None,
    evaluator_profile: ProfileRefV1 = BENCH_EVALUATOR_PROFILE,
    sampling_strategy: str = "all",
    sample_size_per_partition: int | None = None,
    seed_derivation_version: str = "subseed@1",
    minimum_repetitions: int = 1,
    maximum_repetitions: int = 3,
    aggregate_artifacts: tuple[ArtifactV2, ...] = (),
    simulation_execution: BenchmarkSimulationExecutionV1 | None = None,
    max_result_metrics_bytes_total: int | None = None,
    agent_prompt_count: int = 1,
    checker_constraint_count: int = 1,
    max_checker_work_units: int | None = None,
    max_checker_work_units_total: int | None = None,
) -> ArtifactV2:
    source_dataset = harness.load_artifact(dataset_artifact_id)
    report_template = Path("scenarios/bench/bench-report.json").read_text(encoding="utf-8")
    snapshot = Snapshot(entities={}, relations={})
    constraints = tuple(default_constraints()[:checker_constraint_count])
    constraints_payload = [item.model_dump(mode="json") for item in constraints]
    evaluator_policy = build_builtin_benchmark_evaluator_policy()
    deterministic_metric = BenchmarkMetricRefV1(metric_id="oracle-fp", metric_version=1)
    agent_metric = BenchmarkMetricRefV1(metric_id="agent-fix", metric_version=1)
    dataset_partitions: list[BenchmarkDatasetPartitionV1] = []
    for partition in partitions:
        dataset_cases: list[BenchmarkDatasetCaseV1] = []
        for case in partition.cases:
            if case.execution_mode == "agent":
                executor = BenchmarkAgentResponseExecutorV1(
                    prompts=("evaluate the frozen benchmark case",) * agent_prompt_count,
                    response_format="text",
                    oracle=BenchmarkEqualsPredicateV1(operator="equals", expected="pass"),
                )
                metric_refs = (agent_metric,)
            else:
                selected_constraints = constraints if simulation_execution is None else ()
                selected_constraint_payload = (
                    constraints_payload if simulation_execution is None else []
                )
                executor = BenchmarkCleanOracleFpExecutorV1(
                    snapshot_payload=snapshot.content_payload,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_payload_hash=sha256_lowerhex(
                        canonical_json(snapshot.content_payload).encode("utf-8")
                    ),
                    constraints=selected_constraints,
                    constraints_digest=sha256_lowerhex(
                        canonical_json(selected_constraint_payload).encode("utf-8")
                    ),
                    max_checker_work_units=(
                        evaluator_policy.max_checker_work_units_total
                        if max_checker_work_units is None
                        else max_checker_work_units
                    ),
                    needs_navigation=False,
                    simulation=simulation_execution,
                    failure_buckets=(
                        ("deterministic", "unproven")
                        if simulation_execution is None
                        else ("simulation",)
                    ),
                )
                metric_refs = (deterministic_metric,)
            dataset_cases.append(
                BenchmarkDatasetCaseV1(
                    case_id=case.case_id,
                    execution_mode=case.execution_mode,
                    executor=executor,
                    aggregate_oracle=(
                        BenchmarkEqualsPredicateV1(
                            operator="equals",
                            actual_pointer="/payload_schema_version",
                            expected="review@1",
                        )
                        if aggregate_artifacts
                        else None
                    ),
                    metric_refs=metric_refs,
                )
            )
        dataset_partitions.append(
            BenchmarkDatasetPartitionV1(
                partition_id=partition.partition_id,
                cases=tuple(dataset_cases),
            )
        )
    metric_refs = {
        (ref.metric_id, ref.metric_version)
        for partition in dataset_partitions
        for case in partition.cases
        for ref in case.metric_refs
    }
    metric_definitions = []
    if (deterministic_metric.metric_id, deterministic_metric.metric_version) in metric_refs:
        metric_definitions.append(
            BenchmarkBinaryMetricDefinitionV1(
                metric=deterministic_metric,
                target=BenchmarkBinaryMetricTargetV1(
                    collection="false_positives",
                    name="oracle_fp",
                    bucket="deterministic_fp",
                ),
                result_pointer="/metrics/false_positive",
                positive_value=True,
            )
        )
    if (agent_metric.metric_id, agent_metric.metric_version) in metric_refs:
        metric_definitions.append(
            BenchmarkBinaryMetricDefinitionV1(
                metric=agent_metric,
                target=BenchmarkBinaryMetricTargetV1(
                    collection="agent",
                    name="fix_pass_rate",
                    bucket="agent",
                ),
                result_pointer="/metrics/oracle_passed",
                positive_value=True,
            )
        )
    dataset_contract = BenchmarkDatasetV1(
        partitions=tuple(dataset_partitions),
        binary_metrics=tuple(metric_definitions),
        report_template_utf8=report_template,
        report_template_sha256=sha256_lowerhex(report_template.encode("utf-8")),
    )
    dataset = harness.seed_payload_artifact(
        kind="bench_dataset",
        payload=canonical_json(dataset_contract.model_dump(mode="json")).encode("utf-8"),
        version_tuple=source_dataset.version_tuple,
        payload_schema_id="bench-dataset@1",
        domain_scope=domain_scope,
    )
    all_cases = tuple(case for partition in partitions for case in partition.cases)
    if aggregate_artifacts and len(aggregate_artifacts) != len(all_cases):
        raise AssertionError("aggregate fixture must bind every benchmark case")
    aggregate_inputs = (
        tuple(
            BenchmarkAggregateInputBindingV1(
                case_id=case.case_id,
                replication_index=0,
                artifact_id=artifact.artifact_id,
                payload_hash=artifact.payload_hash,
                payload_size_bytes=artifact.object_ref.size_bytes,
                artifact_kind=artifact.kind,  # type: ignore[arg-type]
                payload_schema_id=str(artifact.meta["payload_schema_id"]),
            )
            for case, artifact in zip(all_cases, aggregate_artifacts, strict=True)
        )
        if aggregate_artifacts
        else ()
    )
    spec = BenchmarkSpecV1(
        dataset=BenchmarkDatasetBindingV1(
            artifact_id=dataset.artifact_id,
            payload_hash=dataset.payload_hash
            if dataset_payload_hash is None
            else dataset_payload_hash,
        ),
        evaluator_profile=evaluator_profile,
        evaluator_policy=evaluator_policy.ref,
        metric_policy=BenchmarkMetricPolicyV1(
            policy_id="bench-metrics",
            policy_version=1,
            metrics=tuple(item.metric for item in metric_definitions),
        ),
        sampling_policy=BenchmarkSamplingPolicyV1(
            policy_id="bench-sampling",
            policy_version=1,
            strategy=sampling_strategy,  # type: ignore[arg-type]
            sample_size_per_partition=sample_size_per_partition,
            minimum_repetitions=minimum_repetitions,
            maximum_repetitions=maximum_repetitions,
            seed_derivation_version=seed_derivation_version,
        ),
        ordering_policy=BenchmarkOrderingPolicyV1(
            policy_id="bench-ordering",
            policy_version=1,
            keys=(
                BenchmarkOrderKeyV1(
                    field_path="/case_id",
                    direction="ascending",
                    nulls="forbidden",
                ),
            ),
        ),
        resource_limits=BenchmarkResourceLimitsV1(
            max_case_executions=evaluator_policy.max_case_executions,
            max_prepared_report_bytes=evaluator_policy.max_prepared_report_bytes,
            max_aggregate_input_bytes_per_artifact=(
                evaluator_policy.max_aggregate_input_bytes_per_artifact
            ),
            max_aggregate_input_bytes_total=(evaluator_policy.max_aggregate_input_bytes_total),
            max_checker_work_units_total=(
                evaluator_policy.max_checker_work_units_total
                if max_checker_work_units_total is None
                else max_checker_work_units_total
            ),
            max_simulation_work_units_total=(evaluator_policy.max_simulation_work_units_total),
            max_result_metrics_bytes_total=(
                evaluator_policy.max_result_metrics_bytes_total
                if max_result_metrics_bytes_total is None
                else max_result_metrics_bytes_total
            ),
            max_agent_model_calls_total=evaluator_policy.max_agent_model_calls_total,
        ),
        aggregate_repetition_count=1 if aggregate_inputs else None,
        aggregate_inputs=aggregate_inputs,
        partitions=partitions,
    )
    return harness.seed_payload_artifact(
        kind="benchmark_spec",
        payload=spec.model_dump(mode="json"),
        version_tuple=VersionTuple(
            doc_version=dataset.version_tuple.doc_version,
            ir_snapshot_id=dataset.version_tuple.ir_snapshot_id,
            constraint_snapshot_id=dataset.version_tuple.constraint_snapshot_id,
            env_contract_version=dataset.version_tuple.env_contract_version,
            tool_version="benchmark-spec@1",
        ),
        lineage=(dataset.artifact_id,),
        payload_schema_id="benchmark-spec@1",
        domain_scope=domain_scope,
    )


def _benchmark_partition(
    partition_id: str,
    *cases: tuple[str, str],
) -> BenchmarkPartitionV1:
    return BenchmarkPartitionV1(
        partition_id=partition_id,
        cases=tuple(
            BenchmarkCaseExecutionV1(case_id=case_id, execution_mode=mode)
            for case_id, mode in cases
        ),
    )


def test_bench_domain_is_derived_from_typed_dataset_bound_spec(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    economy = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset",
        tool_version="bench-dataset@1",
        domain_scope=economy,
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=economy,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=(),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )
    scoped_actor = _actor(
        "human",
        _assignment(role="tooling", scope=economy, assignment_id="assign:bench-economy"),
    )

    accepted = harness.engine_admission.admit_generic_run(
        params=params,
        actor=scoped_actor,
        server=_server("bench:economy-domain"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.payload.llm_execution_mode == "not_applicable"
    assert run.payload.execution_version_plan is None
    assert run.payload.version_tuple.prompt_version is None
    assert run.payload.version_tuple.model_snapshot is None
    assert run.payload.version_tuple.agent_graph_version is None


def test_bench_admission_accepts_a_stricter_result_metrics_total_limit(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
        max_result_metrics_bytes_total=2,
    )
    params = BenchRunPayloadV1(
        dataset_artifact_id=spec.lineage[0],
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    accepted = harness.engine_admission.admit_generic_run(
        params=params,
        actor=_tooling_actor(),
        server=_server("bench:strict-metrics-total"),
    )

    assert harness.run_record(accepted.run_id) is not None


def test_bench_simulation_work_is_rejected_during_admission_before_run_creation(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("sim", ("case:sim", "deterministic")),),
        simulation_execution=BenchmarkSimulationExecutionV1(
            seed_policy="fixed",
            fixed_seed=1,
            agents=100_000,
            ticks=1_000_000,
        ),
    )
    params = BenchRunPayloadV1(
        dataset_artifact_id=spec.lineage[0],
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("sim",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(Conflict, match="frozen work budget"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:simulation-work-overflow"),
        )

    _assert_no_admission_side_effects(
        harness,
        key="bench:simulation-work-overflow",
    )


def test_bench_checker_work_total_is_rejected_during_admission_before_run_creation(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(
            _benchmark_partition(
                "det",
                ("case:1", "deterministic"),
                ("case:2", "deterministic"),
            ),
        ),
        checker_constraint_count=2,
        max_checker_work_units=2,
        max_checker_work_units_total=3,
    )
    params = BenchRunPayloadV1(
        dataset_artifact_id=spec.lineage[0],
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(Conflict, match="Run-total work budget"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:checker-work-total"),
        )

    _assert_no_admission_side_effects(harness, key="bench:checker-work-total")


def test_bench_rejects_spec_bound_to_another_dataset_without_run_or_hold(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        dataset_payload_hash="f" * 64,
        domain_scope=scope,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(Conflict, match="exact dataset"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:wrong-dataset"),
        )

    _assert_no_admission_side_effects(harness, key="bench:wrong-dataset")


def test_bench_rejects_evaluator_profile_different_from_spec_without_run_or_hold(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=REVIEW_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(Conflict, match="differs from the typed spec"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:evaluator-mismatch"),
        )

    _assert_no_admission_side_effects(harness, key="bench:evaluator-mismatch")


def test_bench_rejects_sampling_seed_derivation_drift_without_run_or_hold(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
        seed_derivation_version="unretained-subseed@9",
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(Conflict, match="seed derivation differs"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:seed-derivation-drift"),
        )

    _assert_no_admission_side_effects(harness, key="bench:seed-derivation-drift")


@pytest.mark.parametrize("repetition_count", [1, 4])
def test_bench_rejects_repetition_outside_typed_sampling_policy_without_run_or_hold(
    tmp_path: Path,
    repetition_count: int,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
        minimum_repetitions=2,
        maximum_repetitions=3,
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=repetition_count,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )
    key = f"bench:repetition-outside-policy:{repetition_count}"

    with pytest.raises(Conflict, match="repetition count violates"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(key),
        )

    _assert_no_admission_side_effects(harness, key=key)


@pytest.mark.parametrize(
    ("strategy", "sample_size", "key"),
    [
        ("all", None, "bench:all-extra-seed"),
        ("deterministic_prefix", 1, "bench:prefix-extra-seed"),
    ],
)
def test_bench_deterministic_sampling_forbids_root_seed_without_run_or_hold(
    tmp_path: Path,
    strategy: str,
    sample_size: int | None,
    key: str,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
        sampling_strategy=strategy,
        sample_size_per_partition=sample_size,
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(Conflict, match="forbids a root seed"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(key),
            seed=7,
        )

    _assert_no_admission_side_effects(harness, key=key)


def test_bench_seeded_sampling_rejects_deterministic_evaluator_without_run_or_hold(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
        sampling_strategy="seeded_without_replacement",
        sample_size_per_partition=1,
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(Conflict, match="requires a stochastic evaluator"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:seeded-deterministic-profile"),
            seed=7,
        )

    _assert_no_admission_side_effects(
        harness,
        key="bench:seeded-deterministic-profile",
    )


@pytest.mark.parametrize(
    ("strategy", "sample_size", "suffix"),
    [
        ("all", None, "all"),
        ("deterministic_prefix", 1, "prefix"),
        ("seeded_without_replacement", 1, "sampled"),
    ],
)
def test_bench_stochastic_evaluation_requires_and_freezes_root_seed(
    tmp_path: Path,
    strategy: str,
    sample_size: int | None,
    suffix: str,
) -> None:
    harness = Harness(
        tmp_path,
        profile_updates={"builtin.bench_evaluator": {"stochastic": True}},
    )
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
        sampling_strategy=strategy,
        sample_size_per_partition=sample_size,
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(Conflict, match="requires a root seed"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(f"bench:stochastic-{suffix}-missing-seed"),
        )
    _assert_no_admission_side_effects(
        harness,
        key=f"bench:stochastic-{suffix}-missing-seed",
    )

    accepted = harness.engine_admission.admit_generic_run(
        params=params,
        actor=_tooling_actor(),
        server=_server(f"bench:stochastic-{suffix}-seeded"),
        seed=7,
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.payload.seed == 7


@pytest.mark.parametrize(
    ("stochastic", "seed", "message", "suffix"),
    [
        (False, 7, "forbids a root seed", "deterministic-extra-seed"),
        (True, None, "requires a root seed", "stochastic-missing-seed"),
    ],
)
def test_bench_aggregate_keeps_exact_profile_dependent_seed_policy(
    tmp_path: Path,
    stochastic: bool,
    seed: int | None,
    message: str,
    suffix: str,
) -> None:
    harness = Harness(
        tmp_path,
        profile_updates={"builtin.bench_evaluator": {"stochastic": stochastic}},
    )
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    case_result = harness.seed_artifact(
        kind="review_report",
        tool_version="review@1",
        domain_scope=scope,
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
        aggregate_artifacts=(harness.load_artifact(case_result),),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="aggregate_results",
        case_result_artifact_ids=(case_result,),
    )
    key = f"bench:aggregate-{suffix}"

    with pytest.raises(Conflict, match=message):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(key),
            seed=seed,
        )

    _assert_no_admission_side_effects(harness, key=key)


def _bench_aggregate_inputs(
    harness: Harness,
    *,
    dataset_scope: DomainScope | None = None,
    case_scope: DomainScope | None = None,
) -> tuple[BenchRunPayloadV1, str, ArtifactV2, str]:
    dataset_domain = dataset_scope or DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset",
        tool_version="dataset@1",
        domain_scope=dataset_domain,
    )
    case_result = harness.seed_artifact(
        kind="review_report",
        tool_version="review@1",
        domain_scope=case_scope or dataset_domain,
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=dataset_domain,
        partitions=(_benchmark_partition("det", ("case:1", "deterministic")),),
        aggregate_artifacts=(harness.load_artifact(case_result),),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="aggregate_results",
        case_result_artifact_ids=(case_result,),
    )
    return params, dataset, spec, case_result


def test_bench_aggregate_freezes_exact_content_addressed_inputs(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    params, dataset, spec, case_result = _bench_aggregate_inputs(harness)

    accepted = harness.engine_admission.admit_generic_run(
        params=params,
        actor=_tooling_actor(),
        server=_server("bench:aggregate-exact-inputs"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.payload.llm_execution_mode == "not_applicable"
    assert run.payload.execution_version_plan is None
    assert run.payload.cassette_artifact_id is None
    assert run.payload.input_artifact_ids == tuple(sorted((dataset, spec.artifact_id, case_result)))
    assert run.payload.params.case_result_artifact_ids == (case_result,)


@pytest.mark.parametrize(
    ("mode", "with_plan", "with_cassette", "suffix", "message"),
    [
        ("record", True, False, "record", "deterministic-only"),
        (
            "not_applicable",
            True,
            False,
            "plan",
            "deterministic-only",
        ),
        (
            "not_applicable",
            False,
            True,
            "cassette",
            "deterministic-only",
        ),
    ],
)
def test_bench_aggregate_rejects_model_execution_authority_without_side_effects(
    tmp_path: Path,
    mode: str,
    with_plan: bool,
    with_cassette: bool,
    suffix: str,
    message: str,
) -> None:
    harness = Harness(tmp_path)
    params, _, _, _ = _bench_aggregate_inputs(harness)
    cassette = None
    if with_cassette:
        cassette = harness.seed_payload_artifact(
            kind="cassette_bundle",
            payload=b"closed-test-cassette",
            version_tuple=VersionTuple(tool_version="cassette@1"),
            payload_schema_id="cassette-bundle@1",
        ).artifact_id
    key = f"bench:aggregate-model-authority:{suffix}"

    with pytest.raises(Conflict, match=message):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(key),
            llm_execution_mode=mode,  # type: ignore[arg-type]
            execution_version_plan=_plan("bench.run") if with_plan else None,
            cassette_artifact_id=cassette,
        )

    _assert_no_admission_side_effects(harness, key=key)


def test_bench_aggregate_rejects_case_result_outside_dataset_domain(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    params, _, _, _ = _bench_aggregate_inputs(
        harness,
        dataset_scope=DomainScope(domain_ids=("economy",)),
        case_scope=DomainScope(domain_ids=("combat",)),
    )

    with pytest.raises(Conflict, match="bench input domain"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:aggregate-domain-drift"),
        )

    _assert_no_admission_side_effects(harness, key="bench:aggregate-domain-drift")


def test_bench_aggregate_rejects_non_case_result_kind_without_side_effects(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    params, _, _, _ = _bench_aggregate_inputs(harness)
    invalid = harness.seed_artifact(kind="ir_snapshot", tool_version="ir@1")
    params = params.model_copy(update={"case_result_artifact_ids": (invalid,)})

    with pytest.raises(Conflict, match="kind is not allowed"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:aggregate-wrong-kind"),
        )

    _assert_no_admission_side_effects(harness, key="bench:aggregate-wrong-kind")


def test_bench_aggregate_authenticates_case_result_bytes_before_admission(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    params, _, _, case_result_id = _bench_aggregate_inputs(harness)
    artifact = harness.load_artifact(case_result_id)
    stat = next(
        item for item in harness.objects.list_versions().items if item.ref == artifact.object_ref
    )
    # Corrupt the supposedly immutable generation in place.  The metadata and
    # Artifact still claim the original size/hash, so admission must authenticate
    # actual bytes instead of trusting the database row alone.
    data_path = (
        harness.objects._key_directory(stat.location.key)  # noqa: SLF001 - corruption fixture
        / stat.location.backend_generation
        / "data"
    )
    original = data_path.read_bytes()
    data_path.write_bytes(b"x" * len(original))

    with pytest.raises(IntegrityViolation):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:aggregate-corrupt-case-result"),
        )

    _assert_no_admission_side_effects(
        harness,
        key="bench:aggregate-corrupt-case-result",
    )


def test_bench_rejects_unknown_partition_without_run_or_hold(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("known", ("case:1", "deterministic")),),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("missing",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(Conflict, match="partition selection"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:unknown-partition"),
        )

    _assert_no_admission_side_effects(harness, key="bench:unknown-partition")


@pytest.mark.parametrize(
    ("partition_id", "mode", "plan", "key", "message"),
    [
        ("agent", "not_applicable", None, "bench:agent-na", "contain Agent cases"),
        (
            "agent",
            "live",
            None,
            "bench:agent-live-no-plan",
            "require an execution version plan",
        ),
        (
            "det",
            "record",
            "bench",
            "bench:det-record",
            "deterministic-only",
        ),
    ],
)
def test_bench_selected_case_modes_reject_wrong_llm_mode_without_run_or_hold(
    tmp_path: Path,
    partition_id: str,
    mode: str,
    plan: str | None,
    key: str,
    message: str,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(
            _benchmark_partition("agent", ("case:agent", "agent")),
            _benchmark_partition("det", ("case:det", "deterministic")),
        ),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=(partition_id,),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(Conflict, match=message):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(key),
            llm_execution_mode=mode,  # type: ignore[arg-type]
            execution_version_plan=None if plan is None else _plan("bench.run"),
        )

    _assert_no_admission_side_effects(harness, key=key)


@pytest.mark.parametrize(("repetitions", "accepted"), ((2, True), (3, False)))
def test_bench_agent_call_product_honors_the_manifest_safe_boundary(
    tmp_path: Path,
    repetitions: int,
    accepted: bool,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("agent", ("case:agent", "agent")),),
        maximum_repetitions=5,
        agent_prompt_count=32,
    )
    params = BenchRunPayloadV1(
        dataset_artifact_id=spec.lineage[0],
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("agent",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=repetitions,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    key = f"bench:agent-call-product:{repetitions}"
    if accepted:
        result = harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(key),
            llm_execution_mode="live",
            execution_version_plan=_plan("bench.run"),
        )
        assert harness.run_record(result.run_id) is not None
    else:
        with pytest.raises(Conflict, match="Agent calls exceed"):
            harness.engine_admission.admit_generic_run(
                params=params,
                actor=_tooling_actor(),
                server=_server(key),
                llm_execution_mode="live",
                execution_version_plan=_plan("bench.run"),
            )
        _assert_no_admission_side_effects(harness, key=key)


@pytest.mark.parametrize(
    ("mode", "with_cassette", "suffix", "message"),
    [
        (
            "replay",
            False,
            "replay-missing-cassette",
            "benchmark replay mode requires exactly one cassette bundle",
        ),
        (
            "live",
            True,
            "live-with-cassette",
            "benchmark replay mode requires exactly one cassette bundle",
        ),
        (
            "record",
            True,
            "record-with-cassette",
            "benchmark replay mode requires exactly one cassette bundle",
        ),
    ],
)
def test_bench_agent_cases_require_exact_cassette_mode_shape_without_side_effects(
    tmp_path: Path,
    mode: str,
    with_cassette: bool,
    suffix: str,
    message: str,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("agent", ("case:agent", "agent")),),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("agent",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )
    cassette = None
    if with_cassette:
        cassette = harness.seed_payload_artifact(
            kind="cassette_bundle",
            payload=b"closed-test-cassette",
            version_tuple=VersionTuple(tool_version="cassette@1"),
            payload_schema_id="cassette-bundle@1",
        ).artifact_id
    key = f"bench:agent-cassette-shape:{suffix}"

    with pytest.raises(Conflict, match=message):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(key),
            llm_execution_mode=mode,  # type: ignore[arg-type]
            execution_version_plan=_plan("bench.run"),
            cassette_artifact_id=cassette,
        )

    _assert_no_admission_side_effects(harness, key=key)


def test_bench_unselected_agent_partition_does_not_enable_model_execution(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(
            _benchmark_partition("agent", ("case:agent", "agent")),
            _benchmark_partition("det", ("case:det", "deterministic")),
        ),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("det",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    accepted = harness.engine_admission.admit_generic_run(
        params=params,
        actor=_tooling_actor(),
        server=_server("bench:unselected-agent-partition"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.payload.llm_execution_mode == "not_applicable"
    assert run.payload.execution_version_plan is None


def test_bench_agent_partition_rejects_plan_for_another_agent_graph(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(_benchmark_partition("agent", ("case:agent", "agent")),),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("agent",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    with pytest.raises(IntegrityViolation, match="not retained"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("bench:wrong-agent-graph"),
            llm_execution_mode="record",
            execution_version_plan=_plan("generation.propose"),
        )

    _assert_no_admission_side_effects(harness, key="bench:wrong-agent-graph")


def test_bench_agent_partition_requires_and_freezes_exact_execution_mode(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    scope = DomainScope(domain_ids=("economy",))
    dataset = harness.seed_artifact(
        kind="bench_dataset", tool_version="dataset@1", domain_scope=scope
    )
    spec = _seed_benchmark_spec(
        harness,
        dataset_artifact_id=dataset,
        domain_scope=scope,
        partitions=(
            _benchmark_partition("agent", ("case:agent", "agent")),
            _benchmark_partition("det", ("case:det", "deterministic")),
        ),
    )
    dataset = spec.lineage[0]
    params = BenchRunPayloadV1(
        dataset_artifact_id=dataset,
        benchmark_spec_artifact_id=spec.artifact_id,
        partition_ids=("agent",),
        evaluator_profile=BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )

    accepted = harness.engine_admission.admit_generic_run(
        params=params,
        actor=_tooling_actor(),
        server=_server("bench:agent-record"),
        llm_execution_mode="record",
        execution_version_plan=_plan("bench.run"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.payload.llm_execution_mode == "record"
    assert run.payload.params.partition_ids == ("agent",)


def test_admission_persists_current_dispatch_trace_and_correlates_audit(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=CHECKER_PROFILE,
        checker_ids=(),
        defect_classes=(),
    )
    context = TraceContextV1(
        trace_id="1" * 32,
        span_id="2" * 16,
        trace_flags="01",
        trace_state="gameforge=test",
    )

    with use_trace_context(context):
        accepted = harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("checker:trace"),
        )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.dispatch_trace_carrier is not None
    assert TraceCarrier.extract(run.dispatch_trace_carrier) == context
    from sqlalchemy.orm import Session

    with Session(harness.engine) as session:
        audit = SqlAuditSink(session).get(AUDIT_CHAIN_ID, 1)
    assert audit is not None
    assert audit.correlation.request_id == "request:checker:trace"
    assert audit.correlation.trace_id == context.trace_id


def test_generic_simulation_run_requires_and_carries_seed(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    params = SimulationRunPayloadV1(
        snapshot_artifact_id=snapshot,
        simulation_profile=SIMULATION_PROFILE,
        workload_profile=WORKLOAD_PROFILE,
        replication_count=4,
        horizon_steps=1000,
    )
    accepted = harness.engine_admission.admit_generic_run(
        params=params, actor=_tooling_actor(), server=_server("sim:1"), seed=12345
    )
    run = harness.run_record(accepted.run_id)
    assert run is not None and run.payload.seed == 12345
    assert run.payload.version_tuple.seed == 12345
    assert run.payload.version_tuple.ir_snapshot_id is not None
    # two resolved profile bindings (simulation + workload), one per field-path
    assert {b.field_path for b in run.payload.resolved_profiles} == {
        "/params/simulation_profile",
        "/params/workload_profile",
    }


def test_rollback_validation_binds_non_ir_target_history_and_profile_exactly(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    target, _current, _current_ref, item, request = _rollback_admission_fixture(harness)

    accepted = harness.engine_admission.admit(
        operation="rollback.validate",
        resource_id=item.subject_artifact_id,
        request=request,
        actor=_tooling_actor(),
        server=_server("rollback-validation:exact"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None and run.status == "queued"
    assert run.payload.version_tuple.constraint_snapshot_id == (
        target.version_tuple.constraint_snapshot_id
    )
    assert run.payload.version_tuple.ir_snapshot_id is None
    assert run.payload.version_tuple.seed is None


@pytest.mark.parametrize("lineage_drift", ["missing_current", "extra_parent"])
def test_rollback_validation_rejects_non_exact_request_lineage(
    tmp_path: Path,
    lineage_drift: str,
) -> None:
    harness = Harness(tmp_path)
    extra = (
        harness.seed_artifact(kind="ir_snapshot", tool_version="unrelated@1")
        if lineage_drift == "extra_parent"
        else None
    )
    _target, _current, _current_ref, item, request = _rollback_admission_fixture(
        harness,
        extra_lineage_artifact_id=extra,
        omit_current_lineage=lineage_drift == "missing_current",
    )
    server = _server(f"rollback-validation:lineage:{lineage_drift}")

    with pytest.raises(Conflict, match="lineage must exactly bind current and target"):
        harness.engine_admission.admit(
            operation="rollback.validate",
            resource_id=item.subject_artifact_id,
            request=request,
            actor=_tooling_actor(),
            server=server,
        )

    _assert_no_admission_side_effects(harness, key=server.idempotency_key)


def test_validation_subject_head_revision_is_rechecked_in_create_uow(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _target, _current, _current_ref, item, request = _rollback_admission_fixture(harness)
    harness.approvals = _DriftingSubjectHeadApprovals(item)
    server = _server("rollback-validation:head-toctou")

    with pytest.raises(Conflict, match="workflow subject changed"):
        harness.engine_admission.admit(
            operation="rollback.validate",
            resource_id=item.subject_artifact_id,
            request=request,
            actor=_tooling_actor(),
            server=server,
        )

    run_id = harness.engine_admission._derive_run_id(  # noqa: SLF001
        scope=f"approval:{item.approval_id}",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    assert harness.run_record(run_id) is None
    assert harness.reservation_group(run_id) is None


@pytest.mark.parametrize("drift", ["history", "current"])
def test_rollback_validation_stale_authority_leaves_no_run_or_hold(
    tmp_path: Path,
    drift: str,
) -> None:
    harness = Harness(tmp_path)
    _target, _current, current_ref, item, request = _rollback_admission_fixture(harness)
    if drift == "history":
        request = request.model_copy(update={"target_history_revision": current_ref.revision})
    else:
        later = harness.seed_payload_artifact(
            kind="constraint_snapshot",
            payload={"constraint_snapshot_id": "constraint:later", "constraints": []},
            version_tuple=VersionTuple(
                constraint_snapshot_id="constraint:later",
                tool_version="constraint@1",
            ),
            payload_schema_id="constraint-snapshot@1",
        )
        harness.advance_ref("constraints/head", current_ref, later.artifact_id)
    server = _server(f"rollback-validation:{drift}")

    with pytest.raises(Conflict):
        harness.engine_admission.admit(
            operation="rollback.validate",
            resource_id=item.subject_artifact_id,
            request=request,
            actor=_tooling_actor(),
            server=server,
        )

    run_id = harness.engine_admission._derive_run_id(  # noqa: SLF001
        scope=f"approval:{item.approval_id}",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    assert harness.run_record(run_id) is None
    assert harness.reservation_group(run_id) is None


def test_rollback_validation_rejects_profile_binding_drift(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _target, _current, _current_ref, item, request = _rollback_admission_fixture(harness)
    binding = item.target_binding
    assert isinstance(binding, RollbackTargetBindingV1)
    drifted_profile = binding.rollback_profile_binding.model_copy(
        update={"profile_payload_hash": "f" * 64}
    )
    drifted = ApprovalItem.model_validate(
        {
            **item.model_dump(mode="json"),
            "target_binding": binding.model_copy(
                update={"rollback_profile_binding": drifted_profile}
            ).model_dump(mode="json"),
        }
    )
    harness.approvals = _FixedApprovals(drifted)

    with pytest.raises(Conflict, match="exact draft binding"):
        harness.engine_admission.admit(
            operation="rollback.validate",
            resource_id=item.subject_artifact_id,
            request=request,
            actor=_tooling_actor(),
            server=_server("rollback-validation:profile-drift"),
        )

    _assert_no_admission_side_effects(harness, key="rollback-validation:profile-drift")


# ── generation:propose mints source_raw BEFORE Run creation ──────────────────
def test_generation_mints_source_raw_and_hides_naked_text(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    goal = "Reduce the boss gold reward so net gold inflow is non-positive."
    accepted = harness.engine_admission.admit_generation(
        base_snapshot_artifact_id=base,
        constraint_snapshot_artifact_id=None,
        findings=(),
        objective_goal_text=goal,
        domain_scope=DomainScope(domain_ids=("economy",)),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
        generation_policy=GENERATION_PROFILE,
        candidate_export_profiles=(),
        actor=_tooling_actor(),
        server=_server("generation:1"),
        llm_execution_mode="record",
        execution_version_plan=_plan(),
    )
    run = harness.run_record(accepted.run_id)
    assert run is not None and run.status == "queued"
    assert run.payload.version_tuple.prompt_version == "generation@1"
    assert run.payload.version_tuple.model_snapshot == "test:model@1"
    assert run.payload.version_tuple.agent_graph_version == "generation-graph@1"
    assert run.payload.version_tuple.tool_version == "generation@1"
    # the payload references only the source_raw artifact id/hash, never the text
    goal_binding = run.payload.params.objective_goal
    source_id = goal_binding.source_artifact_id
    assert source_id in run.payload.input_artifact_ids
    payload_json = run.payload.model_dump_json()
    assert "Reduce the boss gold reward" not in payload_json
    # §7.F: the naked goal text must not leak into telemetry/log — assert it is
    # absent from the created run.queued RunEvent AND the create-scope audit detail,
    # not only the immutable Run payload.
    from sqlalchemy.orm import Session

    with Session(harness.engine) as session:
        event = SqlRunRepository(session).get_event(accepted.run_id, 1)
    assert event is not None and event.event_type == "run.queued"
    assert "Reduce the boss gold reward" not in event.model_dump_json()
    with Session(harness.engine) as session:
        audit = SqlAuditSink(session).get(AUDIT_CHAIN_ID, 1)
    assert audit is not None
    assert "Reduce the boss gold reward" not in audit.model_dump_json()
    # source_raw artifact was persisted with kind source_raw and matching hash
    with Session(harness.engine) as session:
        artifact = SqlArtifactRepository(
            session,
            binding_repository=SqlObjectBindingRepository(session, harness.objects, "local"),
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=harness.clock),
            clock=harness.clock,
        ).get(source_id)
    assert artifact is not None and artifact.kind == "source_raw"
    assert artifact.payload_hash == goal_binding.expected_payload_hash
    assert artifact.version_tuple.doc_version == artifact.payload_hash
    assert artifact.meta["payload_schema_id"] == "source-raw@1"

    # Idempotency is authoritative once the Run exists: later ref movement must
    # not invalidate an exact replay of the already-admitted request.
    harness.seed_ref("content/head", base)
    replay = harness.engine_admission.admit_generation(
        base_snapshot_artifact_id=base,
        constraint_snapshot_artifact_id=None,
        findings=(),
        objective_goal_text=goal,
        domain_scope=DomainScope(domain_ids=("economy",)),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
        generation_policy=GENERATION_PROFILE,
        candidate_export_profiles=(),
        actor=_tooling_actor(),
        server=_server("generation:1"),
        llm_execution_mode="record",
        execution_version_plan=_plan(),
    )
    assert replay == accepted


def test_generation_admission_revalidates_exact_retained_finding_link(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    base_artifact = harness.load_artifact(base)
    evidence = harness.seed_payload_artifact(
        kind="review_report",
        payload=ReviewReport(
            snapshot_id=base_artifact.version_tuple.ir_snapshot_id or ""
        ).model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=base_artifact.version_tuple.ir_snapshot_id,
            tool_version="review@1",
        ),
        lineage=(base,),
        payload_schema_id="review@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
    ).artifact_id
    finding = _bind_retained_finding(harness, evidence_artifact_id=evidence)

    accepted = harness.engine_admission.admit_generation(
        base_snapshot_artifact_id=base,
        constraint_snapshot_artifact_id=None,
        findings=(finding,),
        objective_goal_text="Repair the exact confirmed finding.",
        domain_scope=DomainScope(domain_ids=("economy",)),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
        generation_policy=GENERATION_PROFILE,
        candidate_export_profiles=(),
        actor=_tooling_actor(),
        server=_server("generation:finding-exact"),
        llm_execution_mode="record",
        execution_version_plan=_plan(),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.payload.params.findings == (finding,)
    assert finding.evidence_artifact_id in run.payload.input_artifact_ids


@pytest.mark.parametrize("drift", ["digest", "evidence", "missing_link"])
def test_generation_finding_drift_leaves_no_run_or_budget_hold(
    tmp_path: Path,
    drift: str,
) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    base_artifact = harness.load_artifact(base)
    evidence = harness.seed_payload_artifact(
        kind="review_report",
        payload=ReviewReport(
            snapshot_id=base_artifact.version_tuple.ir_snapshot_id or ""
        ).model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=base_artifact.version_tuple.ir_snapshot_id,
            tool_version="review@1",
        ),
        lineage=(base,),
        payload_schema_id="review@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
    ).artifact_id
    finding = _bind_retained_finding(
        harness,
        evidence_artifact_id=evidence,
        retain_link=drift != "missing_link",
    )
    if drift == "digest":
        finding = finding.model_copy(update={"finding_digest": "f" * 64})
    elif drift == "evidence":
        other = harness.seed_artifact(kind="review_report", tool_version="other-review@1")
        finding = finding.model_copy(update={"evidence_artifact_id": other})
    server = _server(f"generation:finding-{drift}")

    with pytest.raises(Conflict):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(finding,),
            objective_goal_text="Do not admit stale Finding evidence.",
            domain_scope=DomainScope(domain_ids=("economy",)),
            target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
            generation_policy=GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=_tooling_actor(),
            server=server,
            llm_execution_mode="record",
            execution_version_plan=_plan(),
        )

    _assert_no_admission_side_effects(harness, key=server.idempotency_key)


def test_constraint_proposal_goal_is_prompt_input_not_document_version_peer(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    document = harness.seed_payload_artifact(
        kind="source_raw",
        payload={"doc_text": "All rewards must be non-negative.", "doc_version": "design@7"},
        version_tuple=VersionTuple(doc_version="design@7", tool_version="source@1"),
        payload_schema_id="source-raw@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )

    accepted = harness.engine_admission.admit_constraint_proposal(
        source_artifact_ids=(document.artifact_id,),
        base_constraint_snapshot_artifact_id=None,
        authoring_goal_text="Extract deterministic constraints from the design.",
        domain_scope=DomainScope(domain_ids=("economy",)),
        dsl_grammar_version="constraint-dsl@1",
        extraction_policy=ProfileRefV1(profile_id="builtin.constraint_extraction", version=1),
        actor=_tooling_actor(),
        server=_server("constraint-proposal:doc-version"),
        llm_execution_mode="record",
        execution_version_plan=_plan("constraint_proposal.propose"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.payload.version_tuple.doc_version == "design@7"
    goal_id = run.payload.params.authoring_goal.source_artifact_id
    goal = harness.load_artifact(goal_id)
    assert goal.version_tuple.doc_version == goal.payload_hash
    assert goal.version_tuple.doc_version != run.payload.version_tuple.doc_version


def test_fresh_generation_ref_conflict_leaves_no_run_or_budget_hold(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    harness.seed_ref("content/head", base)
    server = _server("generation:stale-ref")

    with pytest.raises(Conflict):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(),
            objective_goal_text="Propose a bounded economy adjustment.",
            domain_scope=DomainScope(domain_ids=("economy",)),
            target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
            generation_policy=GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=_tooling_actor(),
            server=server,
            llm_execution_mode="record",
            execution_version_plan=_plan(),
        )

    run_id = harness.engine_admission._derive_run_id(  # noqa: SLF001 - exact no-write proof
        scope=f"principal:{_tooling_actor().principal.id}",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    assert harness.run_record(run_id) is None
    assert harness.reservation_group(run_id) is None


def test_generation_freezes_exact_profile_gate_requirements(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")

    accepted = harness.engine_admission.admit_generation(
        base_snapshot_artifact_id=base,
        constraint_snapshot_artifact_id=None,
        findings=(),
        objective_goal_text="add one bounded quest",
        domain_scope=DomainScope(domain_ids=("economy",)),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
        generation_policy=GENERATION_PROFILE,
        candidate_export_profiles=(),
        actor=_tooling_actor(),
        server=_server("generation:resolved-policy"),
        llm_execution_mode="record",
        execution_version_plan=_plan(),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert len(run.payload.resolved_policy_snapshots) == 1
    snapshot = run.payload.resolved_policy_snapshots[0]
    assert snapshot.resolved_policy_id == "generation-gate"
    assert snapshot.source_profile_field_path == "/params/generation_policy"
    assert (
        snapshot.source_profile_payload_hash
        == run.payload.resolved_profiles[0].profile_payload_hash
    )
    assert {
        (requirement.outcome_rule_id, requirement.requirement_id)
        for requirement in snapshot.requirements
    } == {
        ("checker", "generation-gate:checker"),
        ("simulation", "generation-gate:simulation"),
        ("review", "generation-gate:review"),
    }


# ── one-UoW atomicity: mid-admission failure leaves no Run and no hold ────────
def test_budget_exceeded_leaves_no_run_and_no_hold(tmp_path: Path) -> None:
    tiny = (
        CostAmountV1(dimension="request", value=0, unit="request"),
        CostAmountV1(dimension="concurrent_run", value=1, unit="count"),
    )
    harness = Harness(tmp_path, budget_limits=tiny)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=CHECKER_PROFILE,
        checker_ids=(),
        defect_classes=(),
    )
    with pytest.raises(QuotaExceeded):
        harness.engine_admission.admit_generic_run(
            params=params, actor=_tooling_actor(), server=_server("checker:fail")
        )
    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key="checker:fail",
        request_hash=canonical_sha256({"key": "checker:fail"}),
    )
    assert harness.run_record(run_id) is None
    assert harness.reservation_group(run_id) is None


def test_budget_failure_rolls_back_goal_artifact_publication(tmp_path: Path) -> None:
    tiny = (
        CostAmountV1(dimension="request", value=0, unit="request"),
        CostAmountV1(dimension="concurrent_run", value=1, unit="count"),
    )
    harness = Harness(tmp_path, budget_limits=tiny)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    actor = _tooling_actor()
    goal = "this goal remains only a GC-eligible blob when admission fails"
    pending = harness.engine_admission._mint_goal_source(actor=actor, text=goal)  # noqa: SLF001

    with pytest.raises(QuotaExceeded):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(),
            objective_goal_text=goal,
            domain_scope=DomainScope(domain_ids=("economy",)),
            target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
            generation_policy=GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=actor,
            server=_server("generation:budget-source-atomic"),
            llm_execution_mode="record",
            execution_version_plan=_plan(),
        )

    assert harness.artifact_record(pending.minted.artifact.artifact_id) is None
    _assert_no_admission_side_effects(harness, key="generation:budget-source-atomic")


# ── POST /runs accepts only generic kinds; internal-only rejected everywhere ──
def test_generic_endpoint_rejects_internal_only_kind(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    params = ArtifactMigrationPayloadV1(
        source_artifact_id="artifact:x",
        target_payload_schema_id="schema@1",
        target_meta_schema_version="meta@1",
        migrator=ProfileRefV1(profile_id="builtin.artifact_migrator", version=1),
        publish_mode="report_only",
    )
    with pytest.raises(IntegrityViolation):
        harness.engine_admission.admit_generic_run(
            params=params, actor=_actor(), server=_server("migrate:generic")
        )


def test_internal_run_requires_trusted_system_actor(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    params = ArtifactMigrationPayloadV1(
        source_artifact_id="artifact:x",
        target_payload_schema_id="schema@1",
        target_meta_schema_version="meta@1",
        migrator=ProfileRefV1(profile_id="builtin.artifact_migrator", version=1),
        publish_mode="report_only",
    )
    with pytest.raises(IntegrityViolation):
        harness.engine_admission.admit_internal_run(
            params=params, actor=_actor("human"), server=_server("migrate:human")
        )


def test_internal_migration_rejects_unknown_profile_edge_before_run_or_hold(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    source = _seed_preview(harness, label="migration-unknown-edge")
    params = ArtifactMigrationPayloadV1(
        source_artifact_id=source.artifact_id,
        target_payload_schema_id="ir-core@2",
        target_meta_schema_version="artifact-meta@2",
        migrator=ProfileRefV1(profile_id="builtin.artifact_migrator", version=1),
        publish_mode="report_only",
    )
    key = "migrate:unknown-profile-edge"

    with pytest.raises(Conflict, match="migrator profile allowlist"):
        harness.engine_admission.admit_internal_run(
            params=params,
            actor=_system_operator_actor(),
            server=_server(key),
        )

    _assert_no_admission_side_effects(
        harness,
        key=key,
        scope="internal:system:actor",
    )


def test_internal_dr_drill_fails_typed_until_recovery_catalog_is_composed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    params = DrDrillPayloadV1(
        dr_plan=ProfileRefV1(profile_id="builtin.dr_plan", version=1),
        recovery_catalog_entry_id="recovery-entry:1",
        expected_checkpoint_id="checkpoint:1",
        restore_target_profile=ProfileRefV1(
            profile_id="builtin.restore_target",
            version=1,
        ),
        verification_profile=ProfileRefV1(profile_id="builtin.dr_verifier", version=1),
        destroy_restored_target_after_verification=True,
    )
    key = "dr:recovery-catalog-unavailable"

    with pytest.raises(Forbidden):
        harness.engine_admission.admit_internal_run(
            params=params,
            actor=_actor("system"),
            server=_server(f"{key}:unauthorized"),
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            harness.engine_admission,
            "_current_principal",
            lambda _transaction, _actor: None,
        )
        with pytest.raises(Forbidden, match="principal changed"):
            harness.engine_admission.admit_internal_run(
                params=params,
                actor=_system_operator_actor(domainless=True),
                server=_server(f"{key}:stale-principal"),
            )

    with pytest.raises(
        DependencyUnavailable,
        match="signed recovery catalog authority",
    ):
        harness.engine_admission.admit_internal_run(
            params=params,
            actor=_system_operator_actor(domainless=True),
            server=_server(key),
        )

    _assert_no_admission_side_effects(
        harness,
        key=key,
        scope="internal:system:actor",
    )
    _assert_no_admission_side_effects(
        harness,
        key=f"{key}:unauthorized",
        scope="internal:system:actor",
    )
    _assert_no_admission_side_effects(
        harness,
        key=f"{key}:stale-principal",
        scope="internal:system:actor",
    )


def test_generic_endpoint_rejects_resource_only_kind(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    # generation.propose is resource_endpoint_only; a params payload cannot be
    # submitted through the generic POST /runs surface.
    from gameforge.contracts.identity import DomainScope
    from gameforge.contracts.jobs import GenerationProposePayloadV1, PromptGoalBindingV1

    params = GenerationProposePayloadV1(
        base_snapshot_artifact_id=base,
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal", expected_payload_hash="c" * 64
        ),
        domain_scope=DomainScope(domain_ids=("economy",)),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
        generation_policy=GENERATION_PROFILE,
        candidate_export_profiles=(),
    )
    with pytest.raises(IntegrityViolation):
        harness.engine_admission.admit_generic_run(
            params=params, actor=_actor(), server=_server("gen:generic")
        )


# ── C1: admission RBAC-authorizes and derives the domain server-side ─────────
def _checker_params(snapshot: str) -> CheckerRunPayloadV1:
    return CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=CHECKER_PROFILE,
        checker_ids=(),
        defect_classes=(),
    )


def test_generic_run_rejects_roleless_actor_with_no_run_or_hold(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    with pytest.raises(Forbidden):
        harness.engine_admission.admit_generic_run(
            params=_checker_params(snapshot),
            actor=_actor(),  # roleless: authentication without authorization
            server=_server("checker:roleless"),
        )
    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key="checker:roleless",
        request_hash=canonical_sha256({"key": "checker:roleless"}),
    )
    assert harness.run_record(run_id) is None
    assert harness.reservation_group(run_id) is None


def test_generation_rejects_wrong_role_actor(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    # qa holds no propose/patch grant, so it cannot admit a generation proposal.
    wrong_role = _actor("human", _assignment(role="qa", scope="all", assignment_id="assign:qa"))
    with pytest.raises(Forbidden):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(),
            objective_goal_text="tune the economy",
            domain_scope=DomainScope(domain_ids=("economy",)),
            target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
            generation_policy=GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=wrong_role,
            server=_server("gen:wrongrole"),
            llm_execution_mode="record",
            execution_version_plan=_plan(),
        )
    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key="gen:wrongrole",
        request_hash=canonical_sha256({"key": "gen:wrongrole"}),
    )
    assert harness.run_record(run_id) is None


def test_forbidden_generation_does_not_publish_goal_source_artifact(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    wrong_role = _actor("human", _assignment(role="qa", scope="all", assignment_id="assign:qa"))
    goal = "do not publish this unauthorized goal"
    # Minting is blob-first and deterministic under the frozen clock.  It lets the
    # test name the would-be Artifact while intentionally leaving the SQL binding
    # and Artifact repositories untouched.
    pending = harness.engine_admission._mint_goal_source(  # noqa: SLF001
        actor=wrong_role,
        text=goal,
    )

    with pytest.raises(Forbidden):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(),
            objective_goal_text=goal,
            domain_scope=DomainScope(domain_ids=("economy",)),
            target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
            generation_policy=GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=wrong_role,
            server=_server("gen:source-atomic"),
            llm_execution_mode="record",
            execution_version_plan=_plan(),
        )

    assert harness.artifact_record(pending.minted.artifact.artifact_id) is None
    _assert_no_admission_side_effects(harness, key="gen:source-atomic")


def test_generation_client_domain_cannot_escalate(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    # A content_designer whose reach is narrowed to "economy" by its assignment scope.
    scoped = _actor(
        "human",
        _assignment(
            role="content_designer",
            scope=DomainScope(domain_ids=("economy",)),
            assignment_id="assign:cd",
        ),
    )

    def _generate(domain: DomainScope, key: str) -> Any:
        return harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(),
            objective_goal_text="tune the economy",
            domain_scope=domain,
            target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
            generation_policy=GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=scoped,
            server=_server(key),
            llm_execution_mode="record",
            execution_version_plan=_plan(),
        )

    # A client-declared domain the actor lacks — or a broader superset — is rejected;
    # the actor cannot escalate by naming a domain outside its grant.
    with pytest.raises(Conflict):
        _generate(DomainScope(domain_ids=("combat",)), "gen:combat")
    with pytest.raises(Forbidden):
        _generate(DomainScope(domain_ids=("combat", "economy")), "gen:both")
    # Only the exact authorized domain admits; the authorized domain governs the stamp.
    accepted = _generate(DomainScope(domain_ids=("economy",)), "gen:economy")
    run = harness.run_record(accepted.run_id)
    assert run is not None and run.status == "queued"
    assert run.payload.params.domain_scope == DomainScope(domain_ids=("economy",))


def test_generation_declared_domain_must_be_covered_by_exact_base(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(
        kind="ir_snapshot",
        tool_version="snap@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )

    with pytest.raises(Conflict, match="generation declaration"):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(),
            objective_goal_text="attempt a cross-domain proposal",
            domain_scope=DomainScope(domain_ids=("combat",)),
            target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
            generation_policy=GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=_tooling_actor(),
            server=_server("generation:domain-crossing"),
            llm_execution_mode="record",
            execution_version_plan=_plan(),
        )

    _assert_no_admission_side_effects(harness, key="generation:domain-crossing")


# ── I1: repair/playtest/task-suite input existence/kind is fail-closed ───────
def _repair_params(*, subject_patch: str) -> PatchRepairPayloadV1:
    return PatchRepairPayloadV1(
        subject_patch_artifact_id=subject_patch,
        expected_subject_head_revision=1,
        expected_workflow_revision=2,
        base_snapshot_artifact_id="artifact:base",
        preview_snapshot_artifact_id="artifact:preview",
        validation_evidence_artifact_id="artifact:evidence",
        findings=(),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
        repair_policy=ProfileRefV1(profile_id="builtin.patch_repair", version=1),
        checker_profiles=(),
        simulation_profiles=(),
        regression_suite_artifact_ids=(),
        candidate_export_profiles=(),
    )


def test_repair_rejects_wrong_kind_subject_input(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    # A repair subject must be a `patch`; an ir_snapshot must be rejected at admission.
    not_a_patch = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    with pytest.raises(Conflict, match="kind is not allowed"):
        harness.engine_admission.admit_resource_run(
            params=_repair_params(subject_patch=not_a_patch),
            actor=_tooling_actor(),
            server=_server("repair:wrongkind"),
        )


def test_repair_freezes_exact_profile_verifier_requirements(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    subject, base, preview, evidence, base_ref, _item = _seed_failed_repair_case(harness)
    regression = harness.seed_artifact(kind="regression_suite", tool_version="suite@1")
    params = PatchRepairPayloadV1(
        subject_patch_artifact_id=subject,
        expected_subject_head_revision=1,
        expected_workflow_revision=2,
        base_snapshot_artifact_id=base,
        preview_snapshot_artifact_id=preview,
        validation_evidence_artifact_id=evidence,
        findings=(),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=base_ref),
        repair_policy=ProfileRefV1(profile_id="builtin.patch_repair", version=1),
        checker_profiles=(CHECKER_PROFILE,),
        simulation_profiles=(SIMULATION_PROFILE,),
        regression_suite_artifact_ids=(regression,),
        candidate_export_profiles=(),
    )

    economy_actor = _actor(
        "human",
        _assignment(
            role="tooling",
            scope=DomainScope(domain_ids=("economy",)),
            assignment_id="assign:repair-economy",
        ),
    )
    accepted = harness.engine_admission.admit_resource_run(
        params=params,
        actor=economy_actor,
        server=_server("repair:resolved-policy"),
        llm_execution_mode="record",
        seed=7,
        execution_version_plan=_plan("patch.repair"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert len(run.payload.resolved_policy_snapshots) == 1
    snapshot = run.payload.resolved_policy_snapshots[0]
    assert snapshot.resolved_policy_id == "repair-verifier"
    assert snapshot.source_profile_field_path == "/params/repair_policy"
    assert {
        (
            requirement.outcome_rule_id,
            requirement.requirement_id,
            requirement.producer_profile_field_path,
        )
        for requirement in snapshot.requirements
    } == {
        ("checker", "builtin.checker@1", "/params/checker_profiles/0"),
        ("simulation", "builtin.simulation@1", "/params/simulation_profiles/0"),
        ("regression", regression, None),
    }


def test_regression_only_repair_requires_and_admits_exact_root_seed(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    subject, base, preview, evidence, base_ref, _item = _seed_failed_repair_case(harness)
    regression = harness.seed_artifact(kind="regression_suite", tool_version="suite@1")
    params = PatchRepairPayloadV1(
        subject_patch_artifact_id=subject,
        expected_subject_head_revision=1,
        expected_workflow_revision=2,
        base_snapshot_artifact_id=base,
        preview_snapshot_artifact_id=preview,
        validation_evidence_artifact_id=evidence,
        findings=(),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=base_ref),
        repair_policy=ProfileRefV1(profile_id="builtin.patch_repair", version=1),
        checker_profiles=(CHECKER_PROFILE,),
        simulation_profiles=(),
        regression_suite_artifact_ids=(regression,),
        candidate_export_profiles=(),
    )
    actor = _actor(
        "human",
        _assignment(
            role="tooling",
            scope=DomainScope(domain_ids=("economy",)),
            assignment_id="assign:repair-regression-seed",
        ),
    )

    with pytest.raises(Conflict, match="profile-dependent seed is required"):
        harness.engine_admission.admit_resource_run(
            params=params,
            actor=actor,
            server=_server("repair:regression-seed:missing"),
            llm_execution_mode="record",
            execution_version_plan=_plan("patch.repair"),
        )
    _assert_no_admission_side_effects(harness, key="repair:regression-seed:missing")

    accepted = harness.engine_admission.admit_resource_run(
        params=params,
        actor=actor,
        server=_server("repair:regression-seed:exact"),
        llm_execution_mode="record",
        seed=23,
        execution_version_plan=_plan("patch.repair"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.payload.seed == 23


def test_repair_subject_drift_rolls_back_run_and_budget_hold(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    subject, base, preview, evidence, base_ref, item = _seed_failed_repair_case(harness)
    harness.approvals = _DriftingApprovals(item)
    params = PatchRepairPayloadV1(
        subject_patch_artifact_id=subject,
        expected_subject_head_revision=1,
        expected_workflow_revision=2,
        base_snapshot_artifact_id=base,
        preview_snapshot_artifact_id=preview,
        validation_evidence_artifact_id=evidence,
        findings=(),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=base_ref),
        repair_policy=ProfileRefV1(profile_id="builtin.patch_repair", version=1),
        checker_profiles=(),
        simulation_profiles=(),
        regression_suite_artifact_ids=(),
        candidate_export_profiles=(),
    )
    server = _server("repair:subject-drift")

    with pytest.raises(Conflict):
        harness.engine_admission.admit_resource_run(
            params=params,
            actor=_tooling_actor(),
            server=server,
            llm_execution_mode="record",
            seed=None,
            execution_version_plan=_plan("patch.repair"),
        )

    run_id = harness.engine_admission._derive_run_id(  # noqa: SLF001
        scope=f"principal:{_tooling_actor().principal.id}",
        key=server.idempotency_key,
        request_hash=server.request_hash,
    )
    assert harness.run_record(run_id) is None
    assert harness.reservation_group(run_id) is None


def test_playtest_rejects_missing_config_input(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    params = PlaytestRunPayloadV1(
        config_artifact_id="artifact:missing-config",  # never persisted
        constraint_snapshot_artifact_id="artifact:constraint",
        task_suite_artifact_id="artifact:suite",
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id="episode:1", scenario_spec_artifact_id="artifact:scenario"
            ),
        ),
        environment_profile=ProfileRefV1(profile_id="builtin.environment", version=1),
        planner_policy=ProfileRefV1(profile_id="builtin.playtest_planner", version=1),
        max_steps_per_episode=16,
        interaction_mode="autonomous",
    )
    with pytest.raises(Conflict):
        harness.engine_admission.admit_resource_run(
            params=params, actor=_tooling_actor(), server=_server("playtest:missing")
        )


def test_task_suite_rejects_wrong_kind_source_preview(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    # source_preview must be an ir_snapshot; a config_export must be rejected.
    wrong = harness.seed_artifact(kind="config_export", tool_version="cfg@1")
    params = TaskSuiteDerivePayloadV1(
        source_preview_artifact_id=wrong,
        config_artifact_id="artifact:config",
        constraint_snapshot_artifact_id="artifact:constraint",
        derivation_profile=ProfileRefV1(profile_id="builtin.task_suite_derivation", version=1),
        environment_profile=ProfileRefV1(profile_id="builtin.environment", version=1),
        completion_oracle_registry_ref=CompletionOracleRegistryRefV1(
            registry_version=1, digest="d" * 64
        ),
    )
    with pytest.raises(Conflict, match="kind is not allowed"):
        harness.engine_admission.admit_resource_run(
            params=params, actor=_tooling_actor(), server=_server("task-suite:wrongkind")
        )


def test_task_suite_rejects_config_bound_to_a_different_preview_without_run_or_hold(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    old_preview = _seed_preview(harness, label="old")
    requested_preview = _seed_preview(harness, label="requested")
    constraint = _seed_constraint(harness)
    stale_config = _seed_config(
        harness,
        label="old",
        preview=old_preview,
        constraint=constraint,
    )
    oracle_registry = harness.registry.completion_oracle_registries[0]
    params = TaskSuiteDerivePayloadV1(
        source_preview_artifact_id=requested_preview.artifact_id,
        config_artifact_id=stale_config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        derivation_profile=TASK_SUITE_PROFILE,
        environment_profile=ENVIRONMENT_PROFILE,
        completion_oracle_registry_ref=CompletionOracleRegistryRefV1(
            registry_version=oracle_registry.registry_version,
            digest=oracle_registry.registry_digest,
        ),
    )

    with pytest.raises(StaleTaskSuite):
        harness.engine_admission.admit_resource_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("task-suite:stale-config"),
        )

    _assert_no_admission_side_effects(harness, key="task-suite:stale-config")


def test_task_suite_accepts_exact_preview_config_constraint_and_environment(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    preview = _seed_preview(harness, label="exact", doc_version="design-doc@7")
    constraint = _seed_constraint(harness)
    config = _seed_config(
        harness,
        label="exact",
        preview=preview,
        constraint=constraint,
    )
    oracle_registry = harness.registry.completion_oracle_registries[0]
    params = TaskSuiteDerivePayloadV1(
        source_preview_artifact_id=preview.artifact_id,
        config_artifact_id=config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        derivation_profile=TASK_SUITE_PROFILE,
        environment_profile=ENVIRONMENT_PROFILE,
        completion_oracle_registry_ref=CompletionOracleRegistryRefV1(
            registry_version=oracle_registry.registry_version,
            digest=oracle_registry.registry_digest,
        ),
    )

    accepted = harness.engine_admission.admit_resource_run(
        params=params,
        actor=_tooling_actor(),
        server=_server("task-suite:exact"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None and run.status == "queued"
    assert run.payload.version_tuple.doc_version == "design-doc@7"
    assert harness.reservation_group(accepted.run_id) is not None


def test_task_suite_rejects_config_with_different_document_version(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    preview = _seed_preview(harness, label="doc-drift", doc_version="design-doc@7")
    constraint = _seed_constraint(harness)
    config = _seed_config(
        harness,
        label="doc-drift",
        preview=preview,
        constraint=constraint,
        doc_version_override="unrelated-doc@99",
    )
    oracle_registry = harness.registry.completion_oracle_registries[0]
    params = TaskSuiteDerivePayloadV1(
        source_preview_artifact_id=preview.artifact_id,
        config_artifact_id=config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        derivation_profile=TASK_SUITE_PROFILE,
        environment_profile=ENVIRONMENT_PROFILE,
        completion_oracle_registry_ref=CompletionOracleRegistryRefV1(
            registry_version=oracle_registry.registry_version,
            digest=oracle_registry.registry_digest,
        ),
    )
    key = "task-suite:document-version-drift"

    with pytest.raises(StaleTaskSuite, match="VersionTuple"):
        harness.engine_admission.admit_resource_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(key),
        )

    _assert_no_admission_side_effects(harness, key=key)


def _patch_validation_request(
    harness: Harness,
    *,
    subject: ArtifactV2,
    base: ArtifactV2,
    preview: ArtifactV2,
    candidate_config_ids: tuple[str, ...],
    review_ids: tuple[str, ...],
    trace_ids: tuple[str, ...],
) -> tuple[ApprovalItem, PatchValidationAdmissionRequestV1]:
    current_ref = harness.seed_ref("content/head", base.artifact_id)
    item = validation_testkit.approval_item(
        subject=subject,
        target=preview,
        kind="patch",
        approval_id=f"approval:patch:{subject.artifact_id}",
    )
    binding = PatchTargetBindingV1(
        target_artifact_id=preview.artifact_id,
        target_snapshot_id=preview.version_tuple.ir_snapshot_id or "",
        target_digest=preview.payload_hash,
        ref_name="content/head",
        expected_ref=current_ref,
    )
    item = ApprovalItem.model_validate(
        {
            **item.model_dump(mode="json"),
            "target_binding": binding.model_dump(mode="json"),
        }
    )
    item = _exact_draft_item(
        harness,
        item,
        domain_scope=DomainScope(domain_ids=("builtin",)),
    )
    harness.approvals = _FixedApprovals(item)
    request = PatchValidationAdmissionRequestV1(
        approval_id=item.approval_id,
        expected_subject_head_revision=item.subject_revision,
        expected_workflow_revision=item.workflow_revision,
        subject_digest=item.subject_digest,
        base_snapshot_artifact_id=base.artifact_id,
        preview_snapshot_artifact_id=preview.artifact_id,
        candidate_config_export_artifact_ids=candidate_config_ids,
        target=RefReadBindingV1(ref_name="content/head", expected_ref=current_ref),
        validation_policy=VALIDATION_PROFILE,
        checker_profiles=(),
        simulation_profiles=(),
        findings=(),
        review_artifact_ids=review_ids,
        playtest_trace_artifact_ids=trace_ids,
        regression_suite_artifact_ids=(),
    )
    return item, request


def _seed_review_with_runtime_prompt(
    harness: Harness,
    *,
    preview: ArtifactV2,
    constraint: ArtifactV2,
    retain_producer: bool,
) -> ArtifactV2:
    prompt = harness.seed_payload_artifact(
        kind="source_rendered",
        payload={"messages": [{"role": "user", "content": "review candidate"}]},
        version_tuple=VersionTuple(
            prompt_version="review@1",
            model_snapshot="test:model@1",
            agent_graph_version="graph@1",
            tool_version="prompt-renderer@1",
        ),
        payload_schema_id="source-rendered@1",
    )
    review = harness.seed_payload_artifact(
        kind="review_report",
        payload=ReviewReport(snapshot_id=preview.version_tuple.ir_snapshot_id or "").model_dump(
            mode="json"
        ),
        version_tuple=VersionTuple(
            ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
            constraint_snapshot_id=constraint.version_tuple.constraint_snapshot_id,
            tool_version="review@1",
        ),
        lineage=(preview.artifact_id, constraint.artifact_id, prompt.artifact_id),
        payload_schema_id="review@1",
    )
    if not retain_producer:
        return review

    from tests.platform.m4c.handler_support import build_envelope, build_run_record

    producer_run_id = f"run:review-producer:{review.artifact_id}"
    producer_params = ReviewRunPayloadV1(
        snapshot_artifact_id=preview.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=REVIEW_PROFILE,
        checker_profiles=(),
        simulation_profiles=(),
        llm_triage_policy=LLM_TRIAGE_PROFILE,
    )
    envelope = build_envelope(
        params=producer_params,
        llm_execution_mode="live",
        plan=_plan(),
    )
    projection = RunManifestVersionProjectionV1(
        manifest_scope="run",
        attempt_no=1,
        run_kind=RunKindRef(kind="review.run", version=1),
        run_payload_hash=canonical_payload_hash(envelope),
        frozen_input_version_tuple=envelope.version_tuple,
        terminal_version_tuple=review.version_tuple,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="run-manifest-transition",
            policy_version=1,
            digest="a" * 64,
        ),
        parents=(
            RunManifestParentBindingV1(
                artifact_id=review.artifact_id,
                role="output",
                publication="run_published",
            ),
            RunManifestParentBindingV1(
                artifact_id=prompt.artifact_id,
                role="intermediate",
                publication="run_published",
                attempt_no=1,
                ordinal=1,
            ),
        ),
    )
    result = RunResultV1(
        run_id=producer_run_id,
        attempt_no=1,
        run_kind=RunKindRef(kind="review.run", version=1),
        primary_artifact_id=review.artifact_id,
        produced_artifact_ids=(review.artifact_id,),
        finding_count=0,
        outcome_code="review_completed",
        summary=RunResultSummaryV1(
            outcome_code="review_completed",
            primary_artifact_kind="review_report",
            produced_artifact_count=1,
            finding_count=0,
        ),
        requirement_dispositions=(),
        version_projection=projection,
    )
    result_artifact = harness.seed_payload_artifact(
        kind="run_result",
        payload=result.model_dump(mode="json"),
        version_tuple=review.version_tuple,
        lineage=(review.artifact_id, prompt.artifact_id),
        payload_schema_id="run-result@1",
    )
    harness.synthetic_runs[producer_run_id] = build_run_record(
        envelope,
        RunKindRef(kind="review.run", version=1),
        run_id=producer_run_id,
    ).model_copy(
        update={
            "status": "succeeded",
            "result_artifact_id": result_artifact.artifact_id,
            "concurrency_permit_group_id": None,
        }
    )
    link = RunIntermediateArtifactLinkV1(
        run_id=producer_run_id,
        attempt_no=1,
        call_ordinal=1,
        artifact_id=prompt.artifact_id,
        role="prompt_rendered",
        request_hash="b" * 64,
        fencing_token=1,
        published_at=NOW,
    )
    harness.synthetic_prompt_links[(producer_run_id, 1, 1, 1)] = link
    return review


def _seed_playtest_trace_with_runtime_prompt(
    harness: Harness,
    *,
    preview: ArtifactV2,
    config: ArtifactV2,
    constraint: ArtifactV2,
    suite: ArtifactV2,
    scenario: ArtifactV2,
    episode: TaskEpisodeV1,
    producer_config_artifact_id: str | None = None,
) -> ArtifactV2:
    prompt = harness.seed_payload_artifact(
        kind="source_rendered",
        payload={"messages": [{"role": "user", "content": "plan next action"}]},
        version_tuple=VersionTuple(
            prompt_version="playtest.plan@1",
            model_snapshot="test:model@1",
            agent_graph_version="graph@1",
            tool_version="prompt-renderer@1",
        ),
        payload_schema_id="source-rendered@1",
    )
    trace_payload = PlaytestTraceV1(
        config_artifact_id=config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        task_suite_artifact_id=suite.artifact_id,
        environment_profile=ENVIRONMENT_PROFILE,
        planner_policy=PLAYTEST_PLANNER_PROFILE,
        env_contract_version=ENV_CONTRACT_VERSION,
        interaction_mode="autonomous",
        seed=7,
        episodes=(
            PlaytestEpisodeTraceV1(
                episode_id=episode.episode_id,
                scenario_spec_artifact_id=scenario.artifact_id,
                seed=11,
                step_budget=episode.step_budget,
                completion_oracle=episode.completion_oracle,
                completed=True,
                action_trace=(),
            ),
        ),
    )
    trace = harness.seed_payload_artifact(
        kind="playtest_trace",
        payload=trace_payload.model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
            constraint_snapshot_id=constraint.version_tuple.constraint_snapshot_id,
            env_contract_version=ENV_CONTRACT_VERSION,
            prompt_version="playtest.plan@1",
            model_snapshot="test:model@1",
            agent_graph_version="graph@1",
            tool_version="playtest@1",
            seed=7,
        ),
        lineage=(
            config.artifact_id,
            constraint.artifact_id,
            suite.artifact_id,
            scenario.artifact_id,
            prompt.artifact_id,
        ),
        payload_schema_id="playtest-trace@1",
    )
    from tests.platform.m4c.handler_support import build_envelope, build_run_record

    producer_run_id = f"run:playtest-producer:{trace.artifact_id}"
    producer_params = PlaytestRunPayloadV1(
        config_artifact_id=producer_config_artifact_id or config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        task_suite_artifact_id=suite.artifact_id,
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id=episode.episode_id,
                scenario_spec_artifact_id=scenario.artifact_id,
            ),
        ),
        environment_profile=ENVIRONMENT_PROFILE,
        planner_policy=PLAYTEST_PLANNER_PROFILE,
        max_steps_per_episode=episode.step_budget,
        interaction_mode="autonomous",
    )
    envelope = build_envelope(
        params=producer_params,
        seed=7,
        llm_execution_mode="live",
        plan=_plan(),
    )
    projection = RunManifestVersionProjectionV1(
        manifest_scope="run",
        attempt_no=1,
        run_kind=RunKindRef(kind="playtest.run", version=1),
        run_payload_hash=canonical_payload_hash(envelope),
        frozen_input_version_tuple=envelope.version_tuple,
        terminal_version_tuple=trace.version_tuple,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="run-manifest-transition",
            policy_version=1,
            digest="a" * 64,
        ),
        parents=(
            RunManifestParentBindingV1(
                artifact_id=trace.artifact_id,
                role="output",
                publication="run_published",
            ),
            RunManifestParentBindingV1(
                artifact_id=prompt.artifact_id,
                role="intermediate",
                publication="run_published",
                attempt_no=1,
                ordinal=1,
            ),
        ),
    )
    result = RunResultV1(
        run_id=producer_run_id,
        attempt_no=1,
        run_kind=RunKindRef(kind="playtest.run", version=1),
        primary_artifact_id=trace.artifact_id,
        produced_artifact_ids=(trace.artifact_id,),
        finding_count=0,
        outcome_code="playtest_completed",
        summary=RunResultSummaryV1(
            outcome_code="playtest_completed",
            primary_artifact_kind="playtest_trace",
            produced_artifact_count=1,
            finding_count=0,
        ),
        requirement_dispositions=(),
        version_projection=projection,
    )
    result_artifact = harness.seed_payload_artifact(
        kind="run_result",
        payload=result.model_dump(mode="json"),
        version_tuple=trace.version_tuple,
        lineage=(trace.artifact_id, prompt.artifact_id),
        payload_schema_id="run-result@1",
    )
    harness.synthetic_runs[producer_run_id] = build_run_record(
        envelope,
        RunKindRef(kind="playtest.run", version=1),
        run_id=producer_run_id,
    ).model_copy(
        update={
            "status": "succeeded",
            "result_artifact_id": result_artifact.artifact_id,
            "concurrency_permit_group_id": None,
        }
    )
    link = RunIntermediateArtifactLinkV1(
        run_id=producer_run_id,
        attempt_no=1,
        call_ordinal=1,
        artifact_id=prompt.artifact_id,
        role="prompt_rendered",
        request_hash="c" * 64,
        fencing_token=1,
        published_at=NOW,
    )
    harness.synthetic_prompt_links[(producer_run_id, 1, 1, 1)] = link
    return trace


def test_patch_validation_cross_binds_review_and_playtest_to_exact_candidate(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    base = _seed_preview(harness, label="support-base")
    constraint = _seed_constraint(harness)
    subject, preview = _seed_patch_candidate(
        harness,
        label="support-preview",
        base=base,
        constraint=constraint,
    )
    config = _seed_config(
        harness,
        label="support",
        preview=preview,
        constraint=constraint,
    )
    suite, scenario, episode = _seed_task_suite(
        harness,
        preview=preview,
        config=config,
        constraint=constraint,
    )
    review = harness.seed_payload_artifact(
        kind="review_report",
        payload=ReviewReport(snapshot_id=preview.version_tuple.ir_snapshot_id or "").model_dump(
            mode="json"
        ),
        version_tuple=VersionTuple(
            ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
            constraint_snapshot_id=constraint.version_tuple.constraint_snapshot_id,
            tool_version="review@1",
        ),
        lineage=(preview.artifact_id, constraint.artifact_id),
        payload_schema_id="review@1",
    )
    trace_payload = PlaytestTraceV1(
        config_artifact_id=config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        task_suite_artifact_id=suite.artifact_id,
        environment_profile=ENVIRONMENT_PROFILE,
        planner_policy=PLAYTEST_PLANNER_PROFILE,
        env_contract_version=ENV_CONTRACT_VERSION,
        interaction_mode="autonomous",
        seed=7,
        episodes=(
            PlaytestEpisodeTraceV1(
                episode_id=episode.episode_id,
                scenario_spec_artifact_id=scenario.artifact_id,
                seed=11,
                step_budget=episode.step_budget,
                completion_oracle=episode.completion_oracle,
                completed=True,
                action_trace=(),
            ),
        ),
    )
    trace = harness.seed_payload_artifact(
        kind="playtest_trace",
        payload=trace_payload.model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
            constraint_snapshot_id=constraint.version_tuple.constraint_snapshot_id,
            env_contract_version=ENV_CONTRACT_VERSION,
            tool_version="playtest@1",
            seed=7,
        ),
        lineage=(
            config.artifact_id,
            constraint.artifact_id,
            suite.artifact_id,
            scenario.artifact_id,
        ),
        payload_schema_id="playtest-trace@1",
    )
    item, request = _patch_validation_request(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        candidate_config_ids=(config.artifact_id,),
        review_ids=(review.artifact_id,),
        trace_ids=(trace.artifact_id,),
    )

    accepted = harness.engine_admission.admit(
        operation="patch.validate",
        resource_id=item.subject_artifact_id,
        request=request,
        actor=_tooling_actor(),
        server=_server("patch-validation:supporting-exact"),
    )

    assert harness.run_record(accepted.run_id) is not None


def test_patch_validation_accepts_review_runtime_prompt_from_exact_producer(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    base = _seed_preview(harness, label="prompt-review-base")
    subject, preview = _seed_patch_candidate(
        harness,
        label="prompt-review-preview",
        base=base,
    )
    constraint = _seed_constraint(harness)
    review = _seed_review_with_runtime_prompt(
        harness,
        preview=preview,
        constraint=constraint,
        retain_producer=True,
    )
    item, request = _patch_validation_request(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        candidate_config_ids=(),
        review_ids=(review.artifact_id,),
        trace_ids=(),
    )

    accepted = harness.engine_admission.admit(
        operation="patch.validate",
        resource_id=item.subject_artifact_id,
        request=request,
        actor=_tooling_actor(),
        server=_server("patch-validation:prompt-producer"),
    )

    assert harness.run_record(accepted.run_id) is not None


def test_patch_validation_preserves_subject_constraint_without_candidate_config(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    base = _seed_preview(harness, label="subject-constraint-base")
    constraint = _seed_constraint(
        harness,
        snapshot_id="constraint:subject-only@1",
    )
    subject, preview = _seed_patch_candidate(
        harness,
        label="subject-constraint-preview",
        base=base,
        constraint=constraint,
    )
    item, request = _patch_validation_request(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        candidate_config_ids=(),
        review_ids=(),
        trace_ids=(),
    )

    accepted = harness.engine_admission.admit(
        operation="patch.validate",
        resource_id=item.subject_artifact_id,
        request=request,
        actor=_tooling_actor(),
        server=_server("patch-validation:subject-constraint-without-config"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert (
        run.payload.version_tuple.constraint_snapshot_id
        == constraint.version_tuple.constraint_snapshot_id
    )


def test_patch_validation_rejects_candidate_config_from_another_subject_constraint(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    base = _seed_preview(harness, label="foreign-constraint-base")
    subject_constraint = _seed_constraint(
        harness,
        snapshot_id="constraint:subject@1",
    )
    candidate_constraint = _seed_constraint(
        harness,
        snapshot_id="constraint:candidate@1",
    )
    subject, preview = _seed_patch_candidate(
        harness,
        label="foreign-constraint-preview",
        base=base,
        constraint=subject_constraint,
    )
    config = _seed_config(
        harness,
        label="foreign-constraint-config",
        preview=preview,
        constraint=candidate_constraint,
    )
    item, request = _patch_validation_request(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        candidate_config_ids=(config.artifact_id,),
        review_ids=(),
        trace_ids=(),
    )
    key = "patch-validation:foreign-subject-constraint"

    with pytest.raises(Conflict, match="differs from the Patch subject"):
        harness.engine_admission.admit(
            operation="patch.validate",
            resource_id=item.subject_artifact_id,
            request=request,
            actor=_tooling_actor(),
            server=_server(key),
        )

    _assert_no_admission_side_effects(harness, key=key)


def test_patch_validation_rejects_forged_review_runtime_prompt_parent(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    base = _seed_preview(harness, label="forged-prompt-base")
    subject, preview = _seed_patch_candidate(
        harness,
        label="forged-prompt-preview",
        base=base,
    )
    constraint = _seed_constraint(harness)
    review = _seed_review_with_runtime_prompt(
        harness,
        preview=preview,
        constraint=constraint,
        retain_producer=False,
    )
    item, request = _patch_validation_request(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        candidate_config_ids=(),
        review_ids=(review.artifact_id,),
        trace_ids=(),
    )

    with pytest.raises(Conflict, match="unretained prompt parent"):
        harness.engine_admission.admit(
            operation="patch.validate",
            resource_id=item.subject_artifact_id,
            request=request,
            actor=_tooling_actor(),
            server=_server("patch-validation:forged-prompt-parent"),
        )

    _assert_no_admission_side_effects(
        harness,
        key="patch-validation:forged-prompt-parent",
    )


def test_patch_validation_rejects_llm_review_with_stripped_prompt_lineage(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    base = _seed_preview(harness, label="stripped-prompt-base")
    subject, preview = _seed_patch_candidate(
        harness,
        label="stripped-prompt-preview",
        base=base,
    )
    constraint = _seed_constraint(harness)
    review = harness.seed_payload_artifact(
        kind="review_report",
        payload=ReviewReport(snapshot_id=preview.version_tuple.ir_snapshot_id or "").model_dump(
            mode="json"
        ),
        version_tuple=VersionTuple(
            ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
            constraint_snapshot_id=constraint.version_tuple.constraint_snapshot_id,
            prompt_version="review@1",
            model_snapshot="test:model@1",
            agent_graph_version="graph@1",
            tool_version="review@1",
        ),
        lineage=(preview.artifact_id, constraint.artifact_id),
        payload_schema_id="review@1",
    )
    item, request = _patch_validation_request(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        candidate_config_ids=(),
        review_ids=(review.artifact_id,),
        trace_ids=(),
    )

    with pytest.raises(Conflict, match="omits its source_rendered"):
        harness.engine_admission.admit(
            operation="patch.validate",
            resource_id=item.subject_artifact_id,
            request=request,
            actor=_tooling_actor(),
            server=_server("patch-validation:stripped-prompt-lineage"),
        )

    _assert_no_admission_side_effects(
        harness,
        key="patch-validation:stripped-prompt-lineage",
    )


@pytest.mark.parametrize("producer_matches", [True, False], ids=["exact", "forged"])
def test_patch_validation_authenticates_playtest_runtime_prompt_producer(
    tmp_path: Path,
    producer_matches: bool,
) -> None:
    harness = Harness(tmp_path)
    base = _seed_preview(harness, label=f"playtest-prompt-base:{producer_matches}")
    constraint = _seed_constraint(harness)
    subject, preview = _seed_patch_candidate(
        harness,
        label=f"playtest-prompt-preview:{producer_matches}",
        base=base,
        constraint=constraint,
    )
    config = _seed_config(
        harness,
        label=f"playtest-prompt:{producer_matches}",
        preview=preview,
        constraint=constraint,
    )
    suite, scenario, episode = _seed_task_suite(
        harness,
        preview=preview,
        config=config,
        constraint=constraint,
    )
    trace = _seed_playtest_trace_with_runtime_prompt(
        harness,
        preview=preview,
        config=config,
        constraint=constraint,
        suite=suite,
        scenario=scenario,
        episode=episode,
        producer_config_artifact_id=(
            None if producer_matches else "artifact:foreign-producer-config"
        ),
    )
    item, request = _patch_validation_request(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        candidate_config_ids=(config.artifact_id,),
        review_ids=(),
        trace_ids=(trace.artifact_id,),
    )
    server = _server(f"patch-validation:playtest-prompt:{producer_matches}")

    if producer_matches:
        accepted = harness.engine_admission.admit(
            operation="patch.validate",
            resource_id=item.subject_artifact_id,
            request=request,
            actor=_tooling_actor(),
            server=server,
        )
        assert harness.run_record(accepted.run_id) is not None
    else:
        with pytest.raises(Conflict, match="no exact producer Run"):
            harness.engine_admission.admit(
                operation="patch.validate",
                resource_id=item.subject_artifact_id,
                request=request,
                actor=_tooling_actor(),
                server=server,
            )
        _assert_no_admission_side_effects(harness, key=server.idempotency_key)


def test_patch_validation_rejects_review_from_another_preview(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = _seed_preview(harness, label="review-base")
    subject, preview = _seed_patch_candidate(
        harness,
        label="review-preview",
        base=base,
    )
    other = _seed_preview(harness, label="review-other")
    review = harness.seed_payload_artifact(
        kind="review_report",
        payload=ReviewReport(snapshot_id=preview.version_tuple.ir_snapshot_id or "").model_dump(
            mode="json"
        ),
        version_tuple=VersionTuple(
            ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
            tool_version="review@1",
        ),
        lineage=(other.artifact_id,),
        payload_schema_id="review@1",
    )
    item, request = _patch_validation_request(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        candidate_config_ids=(),
        review_ids=(review.artifact_id,),
        trace_ids=(),
    )

    with pytest.raises(Conflict, match="exact preview"):
        harness.engine_admission.admit(
            operation="patch.validate",
            resource_id=item.subject_artifact_id,
            request=request,
            actor=_tooling_actor(),
            server=_server("patch-validation:foreign-review"),
        )

    _assert_no_admission_side_effects(harness, key="patch-validation:foreign-review")


def test_playtest_rejects_old_suite_with_new_config_without_run_or_hold(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    constraint = _seed_constraint(harness)
    old_preview = _seed_preview(harness, label="old")
    old_config = _seed_config(
        harness,
        label="old",
        preview=old_preview,
        constraint=constraint,
    )
    old_suite, old_scenario, old_episode = _seed_task_suite(
        harness,
        preview=old_preview,
        config=old_config,
        constraint=constraint,
    )
    new_preview = _seed_preview(harness, label="new")
    new_config = _seed_config(
        harness,
        label="new",
        preview=new_preview,
        constraint=constraint,
    )
    params = PlaytestRunPayloadV1(
        config_artifact_id=new_config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        task_suite_artifact_id=old_suite.artifact_id,
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id=old_episode.episode_id,
                scenario_spec_artifact_id=old_scenario.artifact_id,
            ),
        ),
        environment_profile=ENVIRONMENT_PROFILE,
        planner_policy=PLAYTEST_PLANNER_PROFILE,
        max_steps_per_episode=16,
        interaction_mode="autonomous",
    )

    with pytest.raises(StaleTaskSuite):
        harness.engine_admission.admit_resource_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("playtest:old-suite-new-config"),
            llm_execution_mode="live",
            seed=7,
            execution_version_plan=_plan("playtest.run"),
        )

    _assert_no_admission_side_effects(harness, key="playtest:old-suite-new-config")


def test_playtest_accepts_exact_nonempty_episode_subset(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    constraint = _seed_constraint(harness)
    preview = _seed_preview(
        harness,
        label="playtest-exact",
        doc_version="playtest-design@3",
    )
    config = _seed_config(
        harness,
        label="playtest-exact",
        preview=preview,
        constraint=constraint,
    )
    suite, scenario, episode = _seed_task_suite(
        harness,
        preview=preview,
        config=config,
        constraint=constraint,
    )
    params = PlaytestRunPayloadV1(
        config_artifact_id=config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        task_suite_artifact_id=suite.artifact_id,
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id=episode.episode_id,
                scenario_spec_artifact_id=scenario.artifact_id,
            ),
        ),
        environment_profile=ENVIRONMENT_PROFILE,
        planner_policy=PLAYTEST_PLANNER_PROFILE,
        max_steps_per_episode=16,
        interaction_mode="autonomous",
    )

    accepted = harness.engine_admission.admit_resource_run(
        params=params,
        actor=_tooling_actor(),
        server=_server("playtest:exact"),
        llm_execution_mode="live",
        seed=7,
        execution_version_plan=_plan("playtest.run"),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None and run.status == "queued"
    assert run.payload.params.episodes == params.episodes
    assert run.payload.version_tuple.doc_version == "playtest-design@3"


def test_playtest_rejects_graph_selected_for_another_exact_memory_mode(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    constraint = _seed_constraint(harness)
    preview = _seed_preview(harness, label="playtest-memory-selector")
    config = _seed_config(
        harness,
        label="playtest-memory-selector",
        preview=preview,
        constraint=constraint,
    )
    suite, scenario, episode = _seed_task_suite(
        harness,
        preview=preview,
        config=config,
        constraint=constraint,
    )
    params = PlaytestRunPayloadV1(
        config_artifact_id=config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        task_suite_artifact_id=suite.artifact_id,
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id=episode.episode_id,
                scenario_spec_artifact_id=scenario.artifact_id,
            ),
        ),
        environment_profile=ENVIRONMENT_PROFILE,
        planner_policy=PLAYTEST_PLANNER_PROFILE,
        max_steps_per_episode=16,
        interaction_mode="autonomous",
    )
    key = "playtest:wrong-memory-graph"

    with pytest.raises(Conflict, match="exact profile config"):
        harness.engine_admission.admit_resource_run(
            params=params,
            actor=_tooling_actor(),
            server=_server(key),
            llm_execution_mode="live",
            seed=7,
            execution_version_plan=_plan(
                "playtest.run",
                graph_version="playtest-memory-graph@1",
            ),
        )

    _assert_no_admission_side_effects(harness, key=key)


def test_playtest_memory_profile_selects_exact_core_plus_memory_graph(
    tmp_path: Path,
) -> None:
    planner_config = PlaytestPlannerProfileConfigV1(memory_mode="llm_compaction").model_dump(
        mode="json"
    )
    harness = Harness(
        tmp_path,
        profile_updates={
            PLAYTEST_PLANNER_PROFILE.profile_id: {
                "config": planner_config,
                "config_hash": canonical_config_hash(planner_config),
            }
        },
    )
    constraint = _seed_constraint(harness)
    preview = _seed_preview(harness, label="playtest-memory-on")
    config = _seed_config(
        harness,
        label="playtest-memory-on",
        preview=preview,
        constraint=constraint,
    )
    suite, scenario, episode = _seed_task_suite(
        harness,
        preview=preview,
        config=config,
        constraint=constraint,
    )
    params = PlaytestRunPayloadV1(
        config_artifact_id=config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        task_suite_artifact_id=suite.artifact_id,
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id=episode.episode_id,
                scenario_spec_artifact_id=scenario.artifact_id,
            ),
        ),
        environment_profile=ENVIRONMENT_PROFILE,
        planner_policy=PLAYTEST_PLANNER_PROFILE,
        max_steps_per_episode=16,
        interaction_mode="autonomous",
    )

    accepted = harness.engine_admission.admit_resource_run(
        params=params,
        actor=_tooling_actor(),
        server=_server("playtest:memory-on"),
        llm_execution_mode="live",
        seed=7,
        execution_version_plan=_plan(
            "playtest.run",
            graph_version="playtest-memory-graph@1",
        ),
    )

    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.payload.execution_version_plan is not None
    assert run.payload.execution_version_plan.agent_graph_version == ("playtest-memory-graph@1")
    assert {node.agent_node_id for node in run.payload.execution_version_plan.nodes} == {
        "playtest.planner",
        "playtest.executor",
        "playtest.reflect",
        "playtest.memory",
    }


def test_exact_playtest_replay_accepts_replay_only_artifact_profiles_only_in_replay(
    tmp_path: Path,
) -> None:
    replay_only = {
        CONFIG_EXPORT_PROFILE.profile_id: "replay_only",
        ENVIRONMENT_PROFILE.profile_id: "replay_only",
        TASK_SUITE_PROFILE.profile_id: "replay_only",
    }
    harness = Harness(tmp_path, profile_lifecycle_states=replay_only)
    constraint = _seed_constraint(harness)
    preview = _seed_preview(harness, label="historical-playtest")
    config = _seed_config(
        harness,
        label="historical-playtest",
        preview=preview,
        constraint=constraint,
    )
    suite, scenario, episode = _seed_task_suite(
        harness,
        preview=preview,
        config=config,
        constraint=constraint,
    )
    params = PlaytestRunPayloadV1(
        config_artifact_id=config.artifact_id,
        constraint_snapshot_artifact_id=constraint.artifact_id,
        task_suite_artifact_id=suite.artifact_id,
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id=episode.episode_id,
                scenario_spec_artifact_id=scenario.artifact_id,
            ),
        ),
        environment_profile=ENVIRONMENT_PROFILE,
        planner_policy=PLAYTEST_PLANNER_PROFILE,
        max_steps_per_episode=16,
        interaction_mode="autonomous",
    )
    artifacts = {
        preview.artifact_id: preview,
        config.artifact_id: config,
        constraint.artifact_id: constraint,
        suite.artifact_id: suite,
        scenario.artifact_id: scenario,
    }

    with harness._read_scope() as read:  # noqa: SLF001 - exact binding gate unit
        scope = harness.engine_admission._verify_task_suite_and_playtest_bindings(  # noqa: SLF001
            params=params,
            artifacts=artifacts,
            read=read,
            llm_execution_mode="replay",
        )
        assert scope == DomainScope(domain_ids=("builtin",))

        with pytest.raises(StaleTaskSuite, match="lifecycle"):
            harness.engine_admission._verify_task_suite_and_playtest_bindings(  # noqa: SLF001
                params=params,
                artifacts=artifacts,
                read=read,
                llm_execution_mode="live",
            )

        derive = TaskSuiteDerivePayloadV1(
            source_preview_artifact_id=preview.artifact_id,
            config_artifact_id=config.artifact_id,
            constraint_snapshot_artifact_id=constraint.artifact_id,
            derivation_profile=TASK_SUITE_PROFILE,
            environment_profile=ENVIRONMENT_PROFILE,
            completion_oracle_registry_ref=CompletionOracleRegistryRefV1(
                registry_version=harness.registry.completion_oracle_registries[0].registry_version,
                digest=harness.registry.completion_oracle_registries[0].registry_digest,
            ),
        )
        with pytest.raises(StaleTaskSuite, match="lifecycle"):
            harness.engine_admission._verify_task_suite_and_playtest_bindings(  # noqa: SLF001
                params=derive,
                artifacts=artifacts,
                read=read,
                llm_execution_mode="not_applicable",
            )


@pytest.mark.parametrize("current_state", ["replay_only", "disabled"])
def test_native_replay_honors_current_lifecycle_while_freezing_source_catalog(
    tmp_path: Path,
    current_state: str,
) -> None:
    harness = Harness(tmp_path)
    old_catalog = harness.catalog
    snapshot = _seed_preview(harness, label="historical-catalog-review")
    params = ReviewRunPayloadV1(
        snapshot_artifact_id=snapshot.artifact_id,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=REVIEW_PROFILE,
        checker_profiles=(),
        simulation_profiles=(),
        llm_triage_policy=LLM_TRIAGE_PROFILE,
    )
    plan = _plan("review.run")
    source_accepted = harness.engine_admission.admit_generic_run(
        params=params,
        actor=_tooling_actor(),
        server=_server("review:record-before-catalog-transition"),
        llm_execution_mode="record",
        execution_version_plan=plan,
    )
    source = harness.run_record(source_accepted.run_id)
    assert source is not None
    assert source.payload.execution_profile_catalog_version == old_catalog.catalog_version

    def seed_bundle(
        bundle: CassetteBundleV1,
        *,
        lineage: tuple[str, ...],
        scope: str,
    ) -> ArtifactV2:
        identity = build_execution_identity(
            scope=scope,  # type: ignore[arg-type]
            bindings=(),
            agent_graph_version=plan.agent_graph_version,
        )
        blob = canonical_json(bundle.model_dump(mode="json")).encode("utf-8")
        digest = sha256_lowerhex(blob)
        return harness.seed_payload_artifact(
            kind="cassette_bundle",
            payload=blob,
            version_tuple=VersionTuple(
                prompt_version=identity.prompt_projection.tuple_value,
                model_snapshot=identity.model_projection.tuple_value,
                agent_graph_version=identity.agent_graph_version,
                tool_version="cassette-bundle@1",
                cassette_id=f"sha256:{digest}",
            ),
            lineage=lineage,
            payload_schema_id="cassette-bundle@1",
            meta_extra={"execution_identity": identity.model_dump(mode="json")},
        )

    attempt_bundle = CassetteBundleV1(
        scope="attempt",
        run_id=source.run_id,
        attempt_no=1,
    )
    attempt = seed_bundle(attempt_bundle, lineage=(), scope="attempt")
    root_bundle = CassetteBundleV1(
        scope="run",
        run_id=source.run_id,
        child_bundle_artifact_ids=(attempt.artifact_id,),
        outcome_code="review_completed",
    )
    root = seed_bundle(root_bundle, lineage=(attempt.artifact_id,), scope="run")

    terminal_tuple = source.payload.version_tuple.model_copy(
        update={"cassette_id": root.version_tuple.cassette_id}
    )
    primary = harness.seed_payload_artifact(
        kind="review_report",
        payload={"status": "completed"},
        version_tuple=terminal_tuple,
        payload_schema_id="review@1",
        domain_scope=DomainScope(domain_ids=("builtin",)),
    )
    projection = RunManifestVersionProjectionV1(
        manifest_scope="run",
        attempt_no=1,
        run_kind=source.kind,
        run_payload_hash=source.payload_hash,
        frozen_input_version_tuple=source.payload.version_tuple,
        terminal_version_tuple=terminal_tuple,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="run-manifest-transition",
            policy_version=1,
            digest="a" * 64,
        ),
        parents=(
            RunManifestParentBindingV1(
                artifact_id=primary.artifact_id,
                role="output",
                publication="run_published",
            ),
            RunManifestParentBindingV1(
                artifact_id=root.artifact_id,
                role="intermediate",
                publication="run_published",
                cassette_scope="run_bundle",
            ),
        ),
    )
    result_payload = RunResultV1(
        run_id=source.run_id,
        attempt_no=1,
        run_kind=source.kind,
        primary_artifact_id=primary.artifact_id,
        produced_artifact_ids=(primary.artifact_id, root.artifact_id),
        finding_count=0,
        outcome_code="review_completed",
        summary=RunResultSummaryV1(
            outcome_code="review_completed",
            primary_artifact_kind="review_report",
            produced_artifact_count=2,
            finding_count=0,
        ),
        requirement_dispositions=(),
        version_projection=projection,
    )
    result = harness.seed_payload_artifact(
        kind="run_result",
        payload=result_payload.model_dump(mode="json"),
        version_tuple=terminal_tuple,
        lineage=tuple(sorted((primary.artifact_id, root.artifact_id))),
        payload_schema_id="run-result@1",
    )
    from sqlalchemy.orm import Session

    with Session(harness.engine) as session, session.begin():
        row = session.get(RunRow, source.run_id)
        assert row is not None
        row.status = "succeeded"
        row.revision = 8
        row.current_attempt_no = 1
        row.next_attempt_no = 2
        row.next_fencing_token = 2
        # This focused fixture promotes the retained row without fabricating the
        # terminal publisher's event stream; preserve the one queued event head.
        row.next_event_seq = 2
        row.result_artifact_id = result.artifact_id
        row.terminal_cassette_artifact_id = root.artifact_id
        row.updated_at = NOW
        session.add(
            RunAttemptRow(
                run_id=source.run_id,
                attempt_no=1,
                status="succeeded",
                fencing_token=1,
                worker_principal_id="service:worker",
                trace_id=None,
                next_call_ordinal=1,
                started_at=NOW,
                attempt_deadline_utc="2026-07-15T12:30:00Z",
                ended_at=NOW,
                failure_class=None,
                retryable=None,
                failure_artifact_id=None,
                cassette_bundle_artifact_id=attempt.artifact_id,
            )
        )

    current_body = {
        "catalog_version": old_catalog.catalog_version + 1,
        "definitions": old_catalog.definitions,
        "lifecycle": tuple(
            item.model_copy(
                update=(
                    {
                        "state": current_state,
                        "revision": item.revision + 1,
                        "reason_code": f"historical_{current_state}",
                        "changed_at": "2026-07-16T00:00:00Z",
                    }
                    if item.profile == LLM_TRIAGE_PROFILE
                    else {}
                )
            )
            for item in old_catalog.lifecycle
        ),
    }
    current_catalog = ExecutionProfileCatalogSnapshotV1(
        **current_body,
        catalog_digest=execution_profile_catalog_digest(current_body),
    )
    harness.install_catalog_history(
        current=current_catalog,
        retained=(old_catalog, current_catalog),
    )

    if current_state == "disabled":
        with pytest.raises(Conflict, match="currently disabled"):
            harness.engine_admission.admit_generic_run(
                params=params,
                actor=_tooling_actor(),
                server=_server("review:replay-after-disabled-transition"),
                llm_execution_mode="replay",
                execution_version_plan=plan,
                cassette_artifact_id=root.artifact_id,
            )
        return

    replay = harness.engine_admission.admit_generic_run(
        params=params,
        actor=_tooling_actor(),
        server=_server("review:replay-after-catalog-transition"),
        llm_execution_mode="replay",
        execution_version_plan=plan,
        cassette_artifact_id=root.artifact_id,
    )
    replay_run = harness.run_record(replay.run_id)
    assert replay_run is not None
    assert replay_run.payload.execution_profile_catalog_version == old_catalog.catalog_version
    assert replay_run.payload.execution_profile_catalog_digest == old_catalog.catalog_digest
    assert {
        (binding.catalog_version, binding.catalog_digest)
        for binding in replay_run.payload.resolved_profiles
    } == {(old_catalog.catalog_version, old_catalog.catalog_digest)}

    with pytest.raises(Conflict, match="lifecycle"):
        harness.engine_admission.admit_generic_run(
            params=params,
            actor=_tooling_actor(),
            server=_server("review:live-after-catalog-transition"),
            llm_execution_mode="live",
            execution_version_plan=plan,
        )


def test_patch_validation_requires_active_artifact_profiles(tmp_path: Path) -> None:
    replay_only = {
        CONFIG_EXPORT_PROFILE.profile_id: "replay_only",
        ENVIRONMENT_PROFILE.profile_id: "replay_only",
    }
    harness = Harness(tmp_path, profile_lifecycle_states=replay_only)
    base = _seed_preview(harness, label="validation-profile-base")
    constraint = _seed_constraint(harness)
    subject, preview = _seed_patch_candidate(
        harness,
        label="validation-profile-preview",
        base=base,
        constraint=constraint,
    )
    config = _seed_config(
        harness,
        label="validation-profile",
        preview=preview,
        constraint=constraint,
    )
    item, request = _patch_validation_request(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        candidate_config_ids=(config.artifact_id,),
        review_ids=(),
        trace_ids=(),
    )
    server = _server("patch-validation:replay-only-artifact-profile")

    with pytest.raises(StaleTaskSuite, match="lifecycle"):
        harness.engine_admission.admit(
            operation="patch.validate",
            resource_id=item.subject_artifact_id,
            request=request,
            actor=_tooling_actor(),
            server=server,
        )

    _assert_no_admission_side_effects(harness, key=server.idempotency_key)


# ── I2: referenced target/ref artifact ids are kind-checked ──────────────────
def test_generation_kind_checks_target_ref_artifact(tmp_path: Path) -> None:
    from gameforge.contracts.storage import RefValue

    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    # The target ref must resolve to an ir_snapshot; a patch id in the exact input
    # set must be rejected (the target ref id is no longer an unchecked extra).
    wrong_target = harness.seed_artifact(kind="patch", tool_version="patch@1")
    with pytest.raises(Conflict, match="kind is not allowed"):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(),
            objective_goal_text="tune the economy",
            domain_scope=DomainScope(domain_ids=("economy",)),
            target=RefReadBindingV1(
                ref_name="content/head",
                expected_ref=RefValue(artifact_id=wrong_target, revision=1),
            ),
            generation_policy=GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=_tooling_actor(),
            server=_server("gen:target-kind"),
            llm_execution_mode="record",
            execution_version_plan=_plan(),
        )
