"""Real local adapters for the bounded API read services."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from gameforge.bench.payload_codec import BENCH_PAYLOAD_DECODERS
from gameforge.apps.api.content_persistence import (
    ApprovalEvidenceStateProjector,
    SqlApprovalContentAuthority,
    SqlApprovalPayloadBindingProvider,
    SqlContentReadRepository,
    SqlImmutableArtifactPageProvider,
    SqlRefHistoryReadProvider,
)
from gameforge.apps.api.observability_paging import SqlCostUsagePageAdapter
from gameforge.apps.api.pagination import OpaquePageCursorCodec
from gameforge.apps.api.read_paging import SqlMaterializedPageAdapter
from gameforge.apps.api.run_read_domain import resolve_run_read_domain
from gameforge.contracts.canonical import sha256_lowerhex
from gameforge.contracts.errors import (
    DependencyUnavailable,
    IntegrityViolation,
    PayloadTooLarge,
    QueryTooBroad,
)
from gameforge.contracts.diff import ConflictSet
from gameforge.contracts.execution_profiles import ExecutionProfileCatalogSnapshotV1
from gameforge.contracts.findings import FindingRevisionV1, finding_revision_digest
from gameforge.contracts.identity import (
    DomainRegistryV1,
    DomainScope,
    Permission,
    RolePolicy,
)
from gameforge.contracts.jobs import PlaytestRunPayloadV1, RunRecord, RunResultV1
from gameforge.contracts.lineage import ArtifactV1, ArtifactV2
from gameforge.contracts.playtest import PlaytestTraceV1, ScenarioSpecV1, TaskSuiteV1
from gameforge.contracts.observability import (
    LogErrorV1,
    LogQueryV1,
    LogRecordViewV1,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricPageV1,
    MetricQueryV1,
    SpanPageV1,
    SpanViewV1,
    TraceSummaryPageV1,
    TraceSummaryV1,
)
from gameforge.contracts.storage import ObjectStore, UtcClock
from gameforge.platform.read_models.artifacts import (
    ArtifactPayloadReader,
    read_exact_object_bytes,
    strict_canonical_json_object,
)
from gameforge.platform.read_models.authorization import (
    ReadAuthorizationBinding,
    ReadAuthorizationService,
)
from gameforge.platform.read_models.content import (
    ContentReadCapabilities,
    ContentReadService,
)
from gameforge.platform.read_models.observability import (
    AuthorizedLogReadPage,
    AuthorizedTelemetryRunScope,
    LogTraceScopeProof,
    ObservabilityReadCapabilities,
    ObservabilityReadService,
    RunCostReadPage,
    RunObservabilityScope,
)
from gameforge.platform.read_models.workflows import (
    CurrentApprovalProgressProjector,
    WorkflowReadCapabilities,
    WorkflowReadService,
)
from gameforge.platform.runs.state import validate_run_result_binding
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.observability._fields import (
    is_sensitive_key,
    redact_sensitive_text,
    redact_span_values,
    sanitize_telemetry_value,
)
from gameforge.runtime.observability.local_store import LocalTelemetryStore
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.conflicts import SqlConflictSetRepository
from gameforge.runtime.persistence.cost import SqlCostRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import sqlite_read_snapshot_session
from gameforge.runtime.persistence.findings import SqlFindingRepository
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import RunFindingLinkRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.workflow_reads import SqlWorkflowReadRepository
from gameforge.contracts.workflow import ConstraintProposalV1


_SNAPSHOT_TTL = timedelta(minutes=5)
_MAX_MATERIALIZED_ITEMS = 1_000
_MAX_ARTIFACT_PAYLOAD_BYTES = 4 * 1024 * 1024
_MAX_ARTIFACT_DOMAIN_LINEAGE_ITEMS = 1_000
_MAX_ARTIFACT_DOMAIN_LINEAGE_EDGES = 10_000


class _UnavailableAuthority:
    """Fail closed for producer-owned bindings that M4c does not yet persist."""

    def __init__(self, component: str) -> None:
        self._component = component

    def __getattr__(self, method: str):
        def unavailable(*args: object, **kwargs: object) -> Any:
            del args, kwargs
            raise DependencyUnavailable(
                "read authority is unavailable until its producer publishes a binding",
                component=self._component,
                authority_method=method,
            )

        return unavailable


class _ArtifactDomainPayloadReader:
    """Verify and decode only typed payloads that carry domain authority."""

    def __init__(
        self,
        *,
        object_bindings: SqlObjectBindingRepository,
        object_store: ObjectStore,
    ) -> None:
        self._object_bindings = object_bindings
        self._object_store = object_store

    def load[T: BaseModel](
        self,
        artifact: ArtifactV1 | ArtifactV2,
        *,
        schema_id: str,
        model: type[T],
    ) -> T:
        if not isinstance(artifact, ArtifactV2):
            raise IntegrityViolation(
                "typed Artifact domain authority requires lineage@2",
                artifact_id=artifact.artifact_id,
            )
        metadata_schema = artifact.meta.get("payload_schema_id")
        if metadata_schema is not None and metadata_schema != schema_id:
            raise IntegrityViolation(
                "typed Artifact domain schema binding is invalid",
                artifact_id=artifact.artifact_id,
            )
        try:
            binding = self._object_bindings.resolve(artifact.object_ref)
            stat = self._object_store.stat(binding.location)
        except FileNotFoundError as exc:
            raise IntegrityViolation(
                "typed Artifact domain payload binding is unavailable",
                artifact_id=artifact.artifact_id,
            ) from exc
        except OSError as exc:
            raise DependencyUnavailable(
                "typed Artifact domain payload is unavailable",
                component="object_store",
                artifact_id=artifact.artifact_id,
            ) from exc
        if (
            binding.object_ref != artifact.object_ref
            or stat.ref != artifact.object_ref
            or stat.location != binding.location
        ):
            raise IntegrityViolation(
                "typed Artifact domain ObjectBinding differs from its Artifact",
                artifact_id=artifact.artifact_id,
            )
        if artifact.object_ref.size_bytes > _MAX_ARTIFACT_PAYLOAD_BYTES:
            raise PayloadTooLarge(
                "typed Artifact domain payload exceeds the read cap",
                artifact_id=artifact.artifact_id,
                max_payload_bytes=_MAX_ARTIFACT_PAYLOAD_BYTES,
            )
        payload_bytes = read_exact_object_bytes(
            self._object_store,
            binding,
            artifact_id=artifact.artifact_id,
            expected_size=artifact.object_ref.size_bytes,
        )
        if sha256_lowerhex(payload_bytes) != artifact.payload_hash:
            raise IntegrityViolation(
                "typed Artifact domain payload differs from its content address",
                artifact_id=artifact.artifact_id,
            )
        try:
            payload = strict_canonical_json_object(
                payload_bytes,
                artifact_id=artifact.artifact_id,
            )
            parsed = model.model_validate(payload)
        except (TypeError, ValueError, ValidationError, RecursionError) as exc:
            raise IntegrityViolation(
                "typed Artifact domain payload is invalid",
                artifact_id=artifact.artifact_id,
            ) from exc
        return parsed


class _ArtifactDomainAuthority:
    """Resolve immutable Artifact scope from canonical producer metadata + lineage."""

    def __init__(
        self,
        *,
        artifacts: SqlArtifactRepository,
        registry: DomainRegistryV1,
        payloads: _ArtifactDomainPayloadReader,
        payload_bindings: SqlApprovalPayloadBindingProvider,
    ) -> None:
        self._artifacts = artifacts
        self._payloads = payloads
        self._payload_bindings = payload_bindings
        self._schema_cache: dict[str, str | None] = {}
        self._known_domains = frozenset(definition.domain_id for definition in registry.definitions)
        if not self._known_domains:
            raise IntegrityViolation("content domain registry has no retained domains")

    def resolve(self, artifact: ArtifactV1 | ArtifactV2) -> DomainScope:
        # M4 producers freeze the resource scope on lineage@2 Artifacts.  Lineage is
        # provenance, so only legacy unscoped Artifacts need ancestry fallback.
        if isinstance(artifact, ArtifactV2):
            direct_scope = self._bound_scope(artifact)
            if direct_scope is not None:
                return direct_scope

        ordered, parents = self._bounded_lineage(artifact)
        resolved: dict[str, DomainScope | None] = {}
        for current in ordered:
            if self._domain_neutral_leaf(current):
                resolved[current.artifact_id] = None
                continue
            resolved_authority = self._bound_scope(current)
            parent_scopes = tuple(
                scope
                for parent in parents[current.artifact_id]
                if (scope := resolved[parent.artifact_id]) is not None
            )
            lineage_scope = self._union(parent_scopes) if parent_scopes else None
            if resolved_authority is None:
                if lineage_scope is None:
                    raise DependencyUnavailable(
                        "Artifact has no authoritative resource-domain binding",
                        component="content_producer_binding",
                        artifact_id=current.artifact_id,
                    )
                scope = lineage_scope
            else:
                scope = resolved_authority
                if lineage_scope is not None and not set(scope.domain_ids).issubset(
                    lineage_scope.domain_ids
                ):
                    raise IntegrityViolation(
                        "Artifact domain metadata exceeds its lineage authority",
                        artifact_id=current.artifact_id,
                    )
            resolved[current.artifact_id] = scope
        root_scope = resolved[artifact.artifact_id]
        if root_scope is None:
            raise DependencyUnavailable(
                "Artifact has no authoritative resource-domain binding",
                component="content_producer_binding",
                artifact_id=artifact.artifact_id,
            )
        return root_scope

    def _bound_scope(self, artifact: ArtifactV1 | ArtifactV2) -> DomainScope | None:
        explicit = self._explicit_scope(artifact)
        typed = self._typed_scope(artifact)
        if explicit is not None and typed is not None and explicit != typed:
            raise IntegrityViolation(
                "Artifact metadata and typed payload domains disagree",
                artifact_id=artifact.artifact_id,
            )
        return explicit or typed

    def legacy_workflow_fallback_allowed(self, artifact: ArtifactV1 | ArtifactV2) -> bool:
        """Return true only when the complete retained lineage predates domain claims."""

        ordered, _parents = self._bounded_lineage(artifact)
        return all(
            current.meta.get("domain_scope") is None and not self._has_typed_domain_schema(current)
            for current in ordered
        )

    def _bounded_lineage(
        self,
        root: ArtifactV1 | ArtifactV2,
    ) -> tuple[
        tuple[ArtifactV1 | ArtifactV2, ...],
        dict[str, tuple[ArtifactV1 | ArtifactV2, ...]],
    ]:
        """Load one bounded acyclic lineage in deterministic post-order."""

        loaded: dict[str, ArtifactV1 | ArtifactV2] = {root.artifact_id: root}
        discovered = {root.artifact_id}
        state: dict[str, str] = {}
        parents: dict[str, tuple[ArtifactV1 | ArtifactV2, ...]] = {}
        ordered: list[ArtifactV1 | ArtifactV2] = []
        stack: list[tuple[ArtifactV1 | ArtifactV2, bool]] = [(root, False)]
        edge_count = 0
        while stack:
            current, expanded = stack.pop()
            artifact_id = current.artifact_id
            if expanded:
                if state.get(artifact_id) == "done":
                    continue
                state[artifact_id] = "done"
                ordered.append(current)
                continue
            current_state = state.get(artifact_id)
            if current_state == "done":
                continue
            if current_state == "visiting":
                raise IntegrityViolation(
                    "Artifact domain lineage contains a cycle",
                    artifact_id=artifact_id,
                )
            state[artifact_id] = "visiting"
            next_edge_count = edge_count + len(current.lineage)
            if next_edge_count > _MAX_ARTIFACT_DOMAIN_LINEAGE_EDGES:
                raise QueryTooBroad(
                    "Artifact domain lineage exceeds the configured edge bound",
                    max_items=_MAX_ARTIFACT_DOMAIN_LINEAGE_EDGES,
                )
            lineage_ids = tuple(current.lineage)
            if len(lineage_ids) != len(set(lineage_ids)):
                raise IntegrityViolation(
                    "Artifact domain lineage repeats a parent",
                    artifact_id=artifact_id,
                )
            edge_count = next_edge_count
            unseen = set(lineage_ids) - discovered
            if len(discovered) - 1 + len(unseen) > _MAX_ARTIFACT_DOMAIN_LINEAGE_ITEMS:
                raise QueryTooBroad(
                    "Artifact domain lineage exceeds the configured traversal bound",
                    max_items=_MAX_ARTIFACT_DOMAIN_LINEAGE_ITEMS,
                )
            discovered.update(unseen)
            retained_parents: list[ArtifactV1 | ArtifactV2] = []
            for parent_id in lineage_ids:
                parent = loaded.get(parent_id)
                if parent is None:
                    retained = self._artifacts.get(parent_id)
                    if not isinstance(retained, (ArtifactV1, ArtifactV2)):
                        raise IntegrityViolation(
                            "Artifact domain lineage parent is unavailable",
                            artifact_id=artifact_id,
                            parent_artifact_id=parent_id,
                        )
                    parent = retained
                    loaded[parent_id] = parent
                retained_parents.append(parent)
            parents[artifact_id] = tuple(retained_parents)
            stack.append((current, True))
            for parent in reversed(retained_parents):
                parent_state = state.get(parent.artifact_id)
                if parent_state == "visiting":
                    raise IntegrityViolation(
                        "Artifact domain lineage contains a cycle",
                        artifact_id=parent.artifact_id,
                    )
                if parent_state != "done":
                    stack.append((parent, False))
        return tuple(ordered), parents

    @staticmethod
    def _domain_neutral_leaf(artifact: ArtifactV1 | ArtifactV2) -> bool:
        return (
            artifact.kind in {"source_raw", "source_rendered"}
            and artifact.meta.get("domain_scope") is None
            and not artifact.lineage
        )

    def _explicit_scope(self, artifact: ArtifactV1 | ArtifactV2) -> DomainScope | None:
        raw = artifact.meta.get("domain_scope")
        if raw is None:
            return None
        try:
            scope = DomainScope.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "Artifact domain_scope metadata is invalid",
                artifact_id=artifact.artifact_id,
            ) from exc
        if raw != scope.model_dump(mode="json"):
            raise IntegrityViolation(
                "Artifact domain_scope metadata is noncanonical",
                artifact_id=artifact.artifact_id,
            )
        if not set(scope.domain_ids).issubset(self._known_domains):
            raise IntegrityViolation(
                "Artifact domain_scope selects an unknown domain",
                artifact_id=artifact.artifact_id,
            )
        return scope

    def _typed_scope(self, artifact: ArtifactV1 | ArtifactV2) -> DomainScope | None:
        schema_id = self._trusted_schema_id(artifact)
        scope: DomainScope | None = None
        if artifact.kind == "constraint_proposal" and schema_id == "constraint-proposal@1":
            proposal = self._payloads.load(
                artifact,
                schema_id="constraint-proposal@1",
                model=ConstraintProposalV1,
            )
            scope = proposal.domain_scope
        elif artifact.kind == "scenario_spec" and schema_id == "scenario-spec@1":
            scenario = self._payloads.load(
                artifact,
                schema_id="scenario-spec@1",
                model=ScenarioSpecV1,
            )
            scope = scenario.domain_scope
        elif artifact.kind == "task_suite" and schema_id == "task-suite@1":
            suite = self._payloads.load(
                artifact,
                schema_id="task-suite@1",
                model=TaskSuiteV1,
            )
            scope = self._union(tuple(episode.domain_scope for episode in suite.episodes))
        if scope is not None and not set(scope.domain_ids).issubset(self._known_domains):
            raise IntegrityViolation(
                "typed Artifact selects an unknown domain",
                artifact_id=artifact.artifact_id,
            )
        return scope

    def _has_typed_domain_schema(self, artifact: ArtifactV1 | ArtifactV2) -> bool:
        return (artifact.kind, self._trusted_schema_id(artifact)) in {
            ("constraint_proposal", "constraint-proposal@1"),
            ("scenario_spec", "scenario-spec@1"),
            ("task_suite", "task-suite@1"),
        }

    def _trusted_schema_id(self, artifact: ArtifactV1 | ArtifactV2) -> str | None:
        if artifact.artifact_id in self._schema_cache:
            return self._schema_cache[artifact.artifact_id]
        if isinstance(artifact, ArtifactV1):
            # lineage@1 metadata was self-declared and has no ObjectRef/payload
            # binding that the typed V2 reader can authenticate.
            self._schema_cache[artifact.artifact_id] = None
            return None
        if artifact.kind == "constraint_proposal":
            binding = self._payload_bindings.resolve(artifact.artifact_id)
            schema_id = None if binding is None else binding.payload_schema_id
        else:
            metadata_schema = artifact.meta.get("payload_schema_id")
            schema_id = metadata_schema if isinstance(metadata_schema, str) else None
        self._schema_cache[artifact.artifact_id] = schema_id
        return schema_id

    @staticmethod
    def _union(scopes: tuple[DomainScope, ...]) -> DomainScope:
        return DomainScope(
            domain_ids=tuple(
                sorted({domain_id for scope in scopes for domain_id in scope.domain_ids})
            )
        )


class _ContentPermissionAuthority:
    """Cross-check workflow bindings with immutable producer domain authority."""

    def __init__(
        self,
        *,
        approvals: SqlApprovalContentAuthority,
        domains: _ArtifactDomainAuthority,
    ) -> None:
        self._approvals = approvals
        self._domains = domains

    def for_artifact(
        self,
        artifact: ArtifactV1 | ArtifactV2,
        *,
        resource_kind: str,
    ) -> Permission:
        try:
            workflow_permission = self._approvals.for_artifact(
                artifact,
                resource_kind=resource_kind,
            )
        except DependencyUnavailable:
            workflow_permission = None
        if workflow_permission is not None:
            try:
                scope = self._domains.resolve(artifact)
            except DependencyUnavailable:
                # Retained ApprovalItem authority is sufficient for legacy workflow
                # graphs only when their complete retained lineage predates all
                # immutable domain claims. A partial modern graph remains fail-closed.
                if self._domains.legacy_workflow_fallback_allowed(artifact):
                    return workflow_permission
                raise
            if workflow_permission.domain_scope != scope:
                raise IntegrityViolation(
                    "workflow and Artifact domain authorities disagree",
                    artifact_id=artifact.artifact_id,
                )
            return workflow_permission
        scope = self._domains.resolve(artifact)
        return Permission(action="read", resource_kind=resource_kind, domain_scope=scope)

    def for_ref(
        self,
        ref_name: str,
        value: object,
        artifact: ArtifactV1 | ArtifactV2,
    ) -> Permission:
        if not ref_name or getattr(value, "artifact_id", None) != artifact.artifact_id:
            raise IntegrityViolation("ref read does not bind its exact Artifact")
        return Permission(
            action="read",
            resource_kind="ref",
            domain_scope=self._domains.resolve(artifact),
        )


class _RunDomainAuthority:
    def __init__(
        self,
        *,
        registry: DomainRegistryV1,
        approvals: SqlApprovalRepository,
    ) -> None:
        self._registry = registry
        self._approvals = approvals

    def scope(self, run: RunRecord):
        return resolve_run_read_domain(run, self._registry, self._approvals)


class _WorkflowPermissionAuthority:
    """Resolve workflow resource domains from retained Run/link/context authority."""

    def __init__(
        self,
        session: Session,
        *,
        runs: SqlRunRepository,
        approvals: SqlApprovalRepository,
        conflicts: SqlConflictSetRepository,
        run_domains: _RunDomainAuthority,
    ) -> None:
        self._session = session
        self._runs = runs
        self._approvals = approvals
        self._conflicts = conflicts
        self._run_domains = run_domains

    def for_run(self, run: RunRecord) -> Permission:
        return Permission(
            action="read",
            resource_kind="run",
            domain_scope=self._run_domains.scope(run),
        )

    def for_finding(self, finding: FindingRevisionV1) -> Permission:
        digest = finding_revision_digest(finding)
        links = (
            self._session.execute(
                select(RunFindingLinkRow.run_id)
                .where(
                    RunFindingLinkRow.finding_id == finding.finding_id,
                    RunFindingLinkRow.finding_revision == finding.revision,
                    RunFindingLinkRow.finding_digest == digest,
                )
                .distinct()
                .limit(2)
            )
            .scalars()
            .all()
        )
        if len(links) != 1 or links[0] != finding.payload.producer_run_id:
            raise IntegrityViolation(
                "Finding revision has no unique producer Run binding",
                finding_id=finding.finding_id,
                finding_revision=finding.revision,
            )
        run = self._runs.get(links[0])
        if run is None:
            raise IntegrityViolation("Finding producer Run is unavailable")
        return Permission(
            action="read",
            resource_kind="finding",
            domain_scope=self._run_domains.scope(run),
        )

    def for_conflict_set(self, conflict_set: ConflictSet) -> Permission:
        context = self._conflicts.get_context(conflict_set.id)
        if context is None:
            raise IntegrityViolation("ConflictSet context is unavailable")
        item = self._approvals.get(context.expected_approval_id)
        if (
            item is None
            or item.subject_series_id != context.subject_series_id
            or item.subject_artifact_id != context.expected_subject_artifact_id
            or conflict_set.proposed_patch_artifact_id != context.expected_subject_artifact_id
        ):
            raise IntegrityViolation("ConflictSet context differs from its approval subject")
        return Permission(
            action="read",
            resource_kind="conflict_set",
            domain_scope=item.domain_scope,
        )


@dataclass(frozen=True, slots=True)
class _PinnedExecutionProfileCatalog:
    catalog: ExecutionProfileCatalogSnapshotV1

    def __post_init__(self) -> None:
        if type(self.catalog) is not ExecutionProfileCatalogSnapshotV1:
            raise TypeError("local execution-profile catalog must be exact v1")

    def current_catalog(self) -> ExecutionProfileCatalogSnapshotV1:
        return self.catalog


class _RunResultPlaytestSelection:
    """Resolve one successful playtest's primary trace through its exact manifest."""

    def __init__(
        self,
        *,
        runs: SqlRunRepository,
        artifacts: SqlArtifactRepository,
        payloads: ArtifactPayloadReader,
        domains: _ArtifactDomainAuthority,
    ) -> None:
        self._runs = runs
        self._artifacts = artifacts
        self._payloads = payloads
        self._domains = domains

    def result_artifact_id(self, run_id: str) -> str | None:
        run = self._runs.get(run_id)
        if (
            run is None
            or run.kind.kind != "playtest.run"
            or run.kind.version != 1
            or run.status != "succeeded"
        ):
            return None
        params = run.payload.params
        if not isinstance(params, PlaytestRunPayloadV1):
            raise IntegrityViolation("playtest Run payload has the wrong typed params")
        if run.result_artifact_id is None:
            raise IntegrityViolation("successful playtest Run has no result manifest")
        verified = self._payloads.read(run.result_artifact_id)
        if verified.artifact.kind != "run_result" or verified.payload_schema_id != "run-result@1":
            raise IntegrityViolation("playtest Run result manifest has the wrong kind or schema")
        try:
            result = RunResultV1.model_validate(verified.payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("playtest Run result manifest is invalid") from exc
        expected_input_ids = set(run.payload.input_artifact_ids)
        validate_run_result_binding(
            run=run,
            manifest=verified.artifact,
            result=result,
            expected_outcome_code="playtest_completed",
            expected_primary_kind="playtest_trace",
        )
        primary = self._artifacts.get(result.primary_artifact_id)
        if (
            not isinstance(primary, ArtifactV2)
            or primary.kind != "playtest_trace"
            or primary.meta.get("payload_schema_id") != "playtest-trace@1"
            or primary.version_tuple != result.version_projection.terminal_version_tuple
        ):
            raise IntegrityViolation("playtest Run primary result has the wrong kind or schema")
        required_trace_inputs = expected_input_ids - {
            artifact_id
            for artifact_id in (run.payload.cassette_artifact_id,)
            if artifact_id is not None
        }
        if not required_trace_inputs.issubset(primary.lineage):
            raise IntegrityViolation("playtest trace omits a frozen Run input")
        verified_primary = self._payloads.read(primary.artifact_id)
        if (
            verified_primary.artifact != primary
            or verified_primary.payload_schema_id != "playtest-trace@1"
        ):
            raise IntegrityViolation("playtest trace payload binding is invalid")
        try:
            trace = PlaytestTraceV1.model_validate(verified_primary.payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("playtest trace payload is invalid") from exc
        expected_episodes = tuple(
            (episode.episode_id, episode.scenario_spec_artifact_id) for episode in params.episodes
        )
        actual_episodes = tuple(
            (episode.episode_id, episode.scenario_spec_artifact_id) for episode in trace.episodes
        )
        planner_binding = next(
            (
                binding
                for binding in run.payload.resolved_profiles
                if binding.field_path == "/params/planner_policy"
            ),
            None,
        )
        if (
            trace.config_artifact_id != params.config_artifact_id
            or trace.constraint_snapshot_artifact_id != params.constraint_snapshot_artifact_id
            or trace.task_suite_artifact_id != params.task_suite_artifact_id
            or trace.environment_profile != params.environment_profile
            or trace.planner_policy != params.planner_policy
            or trace.requested_max_steps_per_episode != params.max_steps_per_episode
            or trace.interaction_mode != params.interaction_mode
            or trace.env_contract_version != run.payload.version_tuple.env_contract_version
            or trace.seed != run.payload.seed
            or actual_episodes != expected_episodes
            or planner_binding is None
            or trace.execution_envelope.planner_profile_payload_hash
            != planner_binding.profile_payload_hash
        ):
            raise IntegrityViolation("playtest trace differs from its frozen Run payload")
        if self._domains.resolve(primary) != run.resource_domain_scope:
            raise IntegrityViolation("playtest Run and primary result domains differ")
        return primary.artifact_id


@dataclass(frozen=True, slots=True)
class _PinnedBenchReportSelection:
    artifact_id: str | None

    def selected_artifact_id(self) -> str | None:
        return self.artifact_id


class _LocalTelemetryRedactor:
    def redact_span(self, view: SpanViewV1) -> SpanViewV1:
        redacted = redact_span_values(view.span)
        keys = tuple(
            sorted(
                key
                for key, value in view.span.attributes.items()
                if redacted.attributes.get(key) != value
            )
        )
        return SpanViewV1(
            span=redacted,
            redacted_attribute_keys=keys,
            redacted_event_fields=view.redacted_event_fields,
        )

    def redact_log(self, view: LogRecordViewV1) -> LogRecordViewV1:
        record = view.record
        fields: dict[str, object] = {}
        redacted_fields = set(view.redacted_fields)
        for key, value in record.fields.items():
            if is_sensitive_key(key):
                fields[key] = "[REDACTED]"
                redacted_fields.add(key)
            else:
                fields[key] = sanitize_telemetry_value(value)[0]
        error = record.error
        if error is not None:
            error = LogErrorV1(
                error_type=error.error_type,
                message=redact_sensitive_text(error.message)[0],
                stack_fingerprint=error.stack_fingerprint,
            )
        safe = record.model_copy(
            update={
                "message": redact_sensitive_text(record.message)[0],
                "error": error,
                "fields": fields,
            }
        )
        return LogRecordViewV1(
            record=safe,
            redacted_fields=tuple(sorted(redacted_fields)),
        )


class _LocalObservabilityReadPort:
    def __init__(
        self,
        *,
        telemetry: LocalTelemetryStore,
        runs: SqlRunRepository,
        run_domains: _RunDomainAuthority,
        costs: SqlCostRepository,
        cost_pages: SqlCostUsagePageAdapter,
    ) -> None:
        self._telemetry = telemetry
        self._runs = runs
        self._run_domains = run_domains
        self._costs = costs
        self._cost_pages = cost_pages

    def get_run_scope(self, run_id: str) -> RunObservabilityScope | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        return RunObservabilityScope(
            run_id=run_id,
            domain_scope=self._run_domains.scope(run),
            run_revision=run.revision,
        )

    def get_trace_summary(self, trace_id: str) -> TraceSummaryV1 | None:
        return self._telemetry.get_trace_summary(trace_id)

    def page_trace_spans(
        self,
        trace_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: ReadAuthorizationBinding,
        scope: AuthorizedTelemetryRunScope,
    ) -> SpanPageV1:
        return self._telemetry.page_spans(
            trace_id,
            cursor=cursor,
            limit=limit,
            authz_fingerprint=authorization.authz_fingerprint,
            principal_binding=authorization.principal_binding,
            run_scope_mode=scope.mode,
            allowed_run_ids=scope.allowed_run_ids,
        )

    def get_run_trace_scope(self, run_id: str) -> tuple[str, ...]:
        return self._telemetry.get_run_trace_scope(run_id)

    def page_run_traces(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: ReadAuthorizationBinding,
        scope: AuthorizedTelemetryRunScope,
    ) -> TraceSummaryPageV1:
        return self._telemetry.page_run_traces(
            run_id,
            cursor=cursor,
            limit=limit,
            authz_fingerprint=authorization.authz_fingerprint,
            principal_binding=authorization.principal_binding,
            run_scope_mode=scope.mode,
            allowed_run_ids=scope.allowed_run_ids,
        )

    def query_logs(
        self,
        query: LogQueryV1,
        *,
        authorization: ReadAuthorizationBinding,
        scope: AuthorizedTelemetryRunScope,
    ) -> AuthorizedLogReadPage:
        retained = self._telemetry.query_logs_with_scope(
            query,
            principal_binding=authorization.principal_binding,
            run_scope_mode=scope.mode,
            allowed_run_ids=scope.allowed_run_ids,
        )
        return AuthorizedLogReadPage(
            page=retained.page,
            trace_scopes=tuple(
                LogTraceScopeProof(trace_id=value.trace_id, run_ids=value.run_ids)
                for value in retained.trace_scopes
            ),
        )

    def get_metric_descriptor_registry(self) -> MetricDescriptorRegistryV1 | None:
        return self._telemetry.get_metric_descriptor_registry()

    def resolve_metric_descriptors(
        self,
        refs: tuple[MetricDescriptorRefV1, ...],
    ) -> tuple[MetricDescriptorV1, ...]:
        return self._telemetry.resolve_metric_descriptors(refs)

    def query_metrics(
        self,
        query: MetricQueryV1,
        *,
        authorization: ReadAuthorizationBinding,
    ) -> MetricPageV1:
        return self._telemetry.query_metrics(
            query,
            principal_binding=authorization.principal_binding,
        )

    def get_run_cost(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: ReadAuthorizationBinding,
        query_hash: str,
    ) -> RunCostReadPage | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        budget_set = self._costs.get_budget_set(run.budget_set_snapshot_id)
        if budget_set is None:
            raise IntegrityViolation(
                "Run budget-set snapshot is unavailable",
                run_id=run_id,
            )
        return self._cost_pages.page(
            run_id=run_id,
            budget_set=budget_set,
            cursor=cursor,
            limit=limit,
            authorization=authorization,
            query_hash=query_hash,
        )


@dataclass(frozen=True, slots=True)
class LocalReadServices:
    content: ContentReadService
    workflows: WorkflowReadService
    observability: ObservabilityReadService


def build_local_read_services(
    *,
    engine: Engine,
    object_store: ObjectStore,
    object_store_id: str,
    telemetry_store: LocalTelemetryStore,
    role_policy_version: str,
    role_policy_digest: str,
    execution_profile_catalog: ExecutionProfileCatalogSnapshotV1,
    cursor_signing_key: bytes,
    clock: UtcClock | None = None,
    selected_bench_report_artifact_id: str | None = None,
) -> LocalReadServices:
    """Bind each public read to one short SQLite transaction/request scope."""

    selected_clock = clock or SystemUtcClock()
    cursor_signer = CursorSigner(
        signing_key=cursor_signing_key,
        clock=selected_clock,
    )
    cursor_codec = OpaquePageCursorCodec()

    @contextmanager
    def session_scope():
        with sqlite_read_snapshot_session(engine) as session:
            yield session

    @contextmanager
    def snapshot_write_scope():
        session = Session(engine)
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def authorization(session: Session) -> ReadAuthorizationService:
        return ReadAuthorizationService(
            policy_repository=SqlPolicySnapshotRepository(session, clock=selected_clock),
            role_policy_version=role_policy_version,
            role_policy_digest=role_policy_digest,
        )

    def policy_authority(
        session: Session,
    ) -> tuple[SqlPolicySnapshotRepository, DomainRegistryV1]:
        policies = SqlPolicySnapshotRepository(session, clock=selected_clock)
        role_policy = policies.get_role_policy(role_policy_version, role_policy_digest)
        if not isinstance(role_policy, RolePolicy):
            raise DependencyUnavailable(
                "local read role policy is unavailable",
                component="read_authorization",
            )
        registry = policies.get_domain_registry(role_policy.domain_registry_ref)
        if not isinstance(registry, DomainRegistryV1):
            raise DependencyUnavailable(
                "local read domain registry is unavailable",
                component="read_authorization",
            )
        return policies, registry

    def page_factory(session: Session):
        return lambda page_size: SqlMaterializedPageAdapter(
            session,
            cursor_signer=cursor_signer,
            clock=selected_clock,
            page_size=page_size,
            snapshot_ttl=_SNAPSHOT_TTL,
            max_materialized_items=_MAX_MATERIALIZED_ITEMS,
            snapshot_session_factory=snapshot_write_scope,
        )

    @contextmanager
    def content_uow():
        with session_scope() as session:
            unavailable = _UnavailableAuthority("content_producer_binding")
            _policies, registry = policy_authority(session)
            approvals = SqlApprovalRepository(session)
            object_bindings = SqlObjectBindingRepository(
                session,
                object_store,
                object_store_id,
            )
            artifacts = SqlArtifactRepository(
                session,
                binding_repository=object_bindings,
                cursor_signer=cursor_signer,
                clock=selected_clock,
                snapshot_ttl=_SNAPSHOT_TTL,
            )
            refs = SqlRefStore(
                session,
                cursor_signer=cursor_signer,
                clock=selected_clock,
                snapshot_ttl=_SNAPSHOT_TTL,
            )
            runs = SqlRunRepository(session)
            payload_bindings = SqlApprovalPayloadBindingProvider(
                session,
                approvals=approvals,
                artifacts=artifacts,
            )
            payload_reader = ArtifactPayloadReader(
                artifacts=artifacts,
                trusted_bindings=payload_bindings,
                object_bindings=object_bindings,
                object_store=object_store,
                max_payload_bytes=_MAX_ARTIFACT_PAYLOAD_BYTES,
                external_decoders=BENCH_PAYLOAD_DECODERS,
            )
            approval_authority = SqlApprovalContentAuthority(
                session,
                approvals=approvals,
                evidence=ApprovalEvidenceStateProjector(
                    artifacts=artifacts,
                    payload_reader=payload_reader,
                ),
            )
            artifact_domains = _ArtifactDomainAuthority(
                artifacts=artifacts,
                registry=registry,
                payloads=_ArtifactDomainPayloadReader(
                    object_bindings=object_bindings,
                    object_store=object_store,
                ),
                payload_bindings=payload_bindings,
            )
            content_permissions = _ContentPermissionAuthority(
                approvals=approval_authority,
                domains=artifact_domains,
            )
            yield ContentReadCapabilities(
                repository=SqlContentReadRepository(artifacts),
                immutable_artifact_pages=SqlImmutableArtifactPageProvider(
                    session,
                    artifacts=artifacts,
                    cursor_signer=cursor_signer,
                    clock=selected_clock,
                    snapshot_ttl=_SNAPSHOT_TTL,
                    snapshot_session_factory=snapshot_write_scope,
                ),
                payload_reader=payload_reader,
                payload_bindings=payload_bindings,
                authorization=authorization(session),
                permission_resolver=content_permissions,
                specs=unavailable,
                schema_registry=unavailable,
                proposal_workflows=approval_authority,
                subject_workflows=approval_authority,
                playtest_results=_RunResultPlaytestSelection(
                    runs=runs,
                    artifacts=artifacts,
                    payloads=payload_reader,
                    domains=artifact_domains,
                ),
                refs=SqlRefHistoryReadProvider(
                    session,
                    refs=refs,
                    cursor_signer=cursor_signer,
                    clock=selected_clock,
                    snapshot_ttl=_SNAPSHOT_TTL,
                    snapshot_session_factory=snapshot_write_scope,
                ),
                diffs=unavailable,
                bench_reports=_PinnedBenchReportSelection(selected_bench_report_artifact_id),
                execution_profiles=_PinnedExecutionProfileCatalog(execution_profile_catalog),
                page_factory=page_factory(session),
            )

    @contextmanager
    def workflow_uow():
        with session_scope() as session:
            policies, registry = policy_authority(session)
            identities = SqlIdentityRepository(session, clock=selected_clock)
            runs = SqlRunRepository(session)
            findings = SqlFindingRepository(
                session,
                cursor_signer=cursor_signer,
                clock=selected_clock,
                snapshot_ttl=_SNAPSHOT_TTL,
            )
            conflicts = SqlConflictSetRepository(
                session,
                cursor_signer=cursor_signer,
                clock=selected_clock,
                snapshot_ttl=_SNAPSHOT_TTL,
            )
            approvals = SqlApprovalRepository(session)
            run_domains = _RunDomainAuthority(registry=registry, approvals=approvals)
            yield WorkflowReadCapabilities(
                repository=SqlWorkflowReadRepository(
                    session,
                    approvals=approvals,
                    runs=runs,
                    findings=findings,
                    conflicts=conflicts,
                ),
                authorization=ReadAuthorizationService(
                    policy_repository=policies,
                    role_policy_version=role_policy_version,
                    role_policy_digest=role_policy_digest,
                ),
                permission_resolver=_WorkflowPermissionAuthority(
                    session,
                    runs=runs,
                    approvals=approvals,
                    conflicts=conflicts,
                    run_domains=run_domains,
                ),
                approval_projector=CurrentApprovalProgressProjector(
                    policy_repository=policies,
                    role_policy_version=role_policy_version,
                    role_policy_digest=role_policy_digest,
                    principal_resolver=identities.project,
                ),
                page_factory=page_factory(session),
            )

    @contextmanager
    def observability_uow():
        with session_scope() as session:
            _policies, registry = policy_authority(session)
            runs = SqlRunRepository(session)
            approvals = SqlApprovalRepository(session)
            costs = SqlCostRepository(session)
            cost_pages = SqlCostUsagePageAdapter(
                repository=costs,
                page_factory=page_factory(session),
                cursor_codec=cursor_codec,
                max_materialized_items=_MAX_MATERIALIZED_ITEMS,
            )
            yield ObservabilityReadCapabilities(
                port=_LocalObservabilityReadPort(
                    telemetry=telemetry_store,
                    runs=runs,
                    run_domains=_RunDomainAuthority(
                        registry=registry,
                        approvals=approvals,
                    ),
                    costs=costs,
                    cost_pages=cost_pages,
                ),
                authorization=authorization(session),
                redactor=_LocalTelemetryRedactor(),
            )

    return LocalReadServices(
        content=ContentReadService(
            uow_factory=content_uow,
            max_materialized_items=_MAX_MATERIALIZED_ITEMS,
        ),
        workflows=WorkflowReadService(
            unit_of_work=workflow_uow,
            max_materialized_items=_MAX_MATERIALIZED_ITEMS,
        ),
        observability=ObservabilityReadService(unit_of_work=observability_uow),
    )


__all__ = ["LocalReadServices", "build_local_read_services"]
