"""Bounded, authorized read models for immutable content resources.

The service deliberately owns no persistence implementation.  Every source of
authority (Artifact indexes, schema registries, refs, workflow heads, selected
Bench reports, and execution-profile catalogs) is injected, while object-backed
payload bytes are read only through the verified ``ArtifactPayloadReader``
contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, JsonValue, ValidationError

from gameforge.contracts.api import (
    ArtifactPayloadViewV1,
    ArtifactSummaryV1,
    ConstraintProposalReadViewV1,
    ConstraintSnapshotViewV1,
    ExecutionProfileReadViewV1,
    GraphItemV1,
    LineageEntryV1,
    PatchArtifactReadViewV1,
    RefHistoryEntryV1,
    ReviewArtifactViewV1,
    RollbackRequestReadViewV1,
    SchemaRegistryDocumentV1,
    SpecViewV1,
    TaskSuiteArtifactViewV1,
)
from gameforge.contracts.canonical import (
    canonical_sha256,
    compute_snapshot_id,
    typed_canonical_json,
)
from gameforge.contracts.diff import SnapshotDiff, SnapshotDiffEntry
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import (
    DependencyUnavailable,
    Forbidden,
    IntegrityViolation,
    NotFound,
    QueryTooBroad,
    RequestSchemaInvalid,
)
from gameforge.contracts.execution_profiles import (
    ConfigExportProfileDetailsV1,
    EnvironmentProfileDetailsV1,
    ExecutionProfileCatalogSnapshotV1,
    ExecutionProfileDefinitionV1,
    ExecutionProfileKindV1,
    ExecutionProfileLifecycleV1,
    ExecutionProfileViewV1,
    RunKindRef,
    execution_profile_payload_hash,
)
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import DomainScope, DomainScopeValue, Permission, Principal
from gameforge.contracts.ir import Entity, Relation
from gameforge.contracts.lineage import (
    ArtifactKind,
    ArtifactV1,
    ArtifactV2,
    parse_artifact,
)
from gameforge.contracts.playtest import TaskSuiteV1
from gameforge.contracts.review import ReviewReport
from gameforge.contracts.storage import MAX_PAGE_ITEMS, PageCursorV1, PageV1, RefValue
from gameforge.contracts.workflow import ApprovalStatus, ConstraintProposalV1, RollbackRequestV1
from gameforge.platform.read_models.artifacts import (
    ArtifactPayloadBindingProvider,
    ArtifactPayloadReader,
    TrustedArtifactPayloadBinding,
    VerifiedArtifactPayload,
)
from gameforge.platform.read_models.authorization import ReadAuthorizationService
from gameforge.platform.read_models.paging import (
    MaterializedPageFactory,
    ReadPageBinding,
    ReadPageCandidate,
)


_READ_ACTION = "read"
_SHA256_LENGTH = 64
_T = TypeVar("_T", bound=BaseModel)
ArtifactWire = ArtifactV1 | ArtifactV2
ApprovalStatusText = str


@dataclass(frozen=True, slots=True)
class SpecReadBinding:
    """Server-trusted binding between a registered spec and its immutable snapshot."""

    artifact_id: str
    snapshot_id: str
    schema_registry_version: str
    ref_name: str | None = None
    ref_value: RefValue | None = None

    def __post_init__(self) -> None:
        for name in ("artifact_id", "snapshot_id", "schema_registry_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError(f"{name} must be a non-empty bounded string")
        if (self.ref_name is None) != (self.ref_value is None):
            raise ValueError("ref_name and ref_value must be supplied together")
        if self.ref_name is not None and (not self.ref_name or len(self.ref_name) > 512):
            raise ValueError("ref_name must be a non-empty bounded string")
        if self.ref_value is not None and self.ref_value.artifact_id != self.artifact_id:
            raise ValueError("Spec ref_value must point to artifact_id")


@dataclass(frozen=True, slots=True)
class ConstraintProposalWorkflowBinding:
    workflow_revision: int
    approval_status: ApprovalStatusText

    def __post_init__(self) -> None:
        if isinstance(self.workflow_revision, bool) or self.workflow_revision < 1:
            raise ValueError("workflow_revision must be positive")
        if (
            not isinstance(self.approval_status, str)
            or not self.approval_status
            or len(self.approval_status) > 512
        ):
            raise ValueError("approval_status must be a non-empty bounded string")


@dataclass(frozen=True, slots=True)
class PatchWorkflowReadBinding:
    workflow_revision: int
    validation_status: str
    regression_status: str
    approval_status: ApprovalStatus

    def __post_init__(self) -> None:
        if isinstance(self.workflow_revision, bool) or self.workflow_revision < 1:
            raise ValueError("workflow_revision must be positive")
        for name in ("validation_status", "regression_status", "approval_status"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError(f"{name} must be a non-empty bounded string")


@dataclass(frozen=True, slots=True)
class RollbackWorkflowReadBinding:
    workflow_revision: int
    approval_status: ApprovalStatus

    def __post_init__(self) -> None:
        if isinstance(self.workflow_revision, bool) or self.workflow_revision < 1:
            raise ValueError("workflow_revision must be positive")
        if not isinstance(self.approval_status, str) or not self.approval_status:
            raise ValueError("approval_status must be non-empty")


@dataclass(frozen=True, slots=True)
class SnapshotDiffRead:
    """Complete bounded diff returned by the canonical snapshot authority."""

    diff: SnapshotDiff
    entries: tuple[SnapshotDiffEntry, ...]


@dataclass(frozen=True, slots=True)
class LineageSourceEntry:
    artifact_id: str
    depth: int

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, str) or not self.artifact_id:
            raise ValueError("artifact_id must be non-empty")
        if isinstance(self.depth, bool) or not isinstance(self.depth, int) or self.depth < 1:
            raise ValueError("lineage depth must be positive")


class ContentReadRepository(Protocol):
    """Exact immutable Artifact lookup used by singular and derived reads."""

    def get_artifact(self, artifact_id: str) -> ArtifactWire | None: ...


class ImmutableArtifactPageProvider(Protocol):
    """Retained high-watermark pages over append-only Artifact indexes."""

    def page(
        self,
        *,
        index_kind: Literal[
            "specs",
            "constraints",
            "constraint_proposals",
            "patches",
            "rollback_requests",
            "reviews",
            "task_suites",
        ],
        expected_artifact_kind: ArtifactKind,
        filters: Mapping[str, JsonValue],
        cursor: PageCursorV1 | None,
        binding: ReadPageBinding,
        page_size: int,
    ) -> PageV1[ArtifactWire]: ...

    def page_lineage(
        self,
        *,
        root_artifact_id: str,
        cursor: PageCursorV1 | None,
        binding: ReadPageBinding,
        page_size: int,
    ) -> PageV1[LineageSourceEntry]: ...


class SpecBindingProvider(Protocol):
    def resolve(self, artifact_id: str) -> SpecReadBinding | None: ...

    def resolve_snapshot_id(self, snapshot_id: str) -> SpecReadBinding | None: ...


class SchemaRegistryProvider(Protocol):
    def get(self, version: str) -> SchemaRegistryDocumentV1 | None: ...


class ConstraintProposalWorkflowProvider(Protocol):
    def resolve(self, artifact_id: str) -> ConstraintProposalWorkflowBinding | None: ...


class SubjectWorkflowReadProvider(Protocol):
    def resolve_patch(self, artifact_id: str) -> PatchWorkflowReadBinding | None: ...

    def resolve_rollback(self, artifact_id: str) -> RollbackWorkflowReadBinding | None: ...


class PlaytestResultReadProvider(Protocol):
    def result_artifact_id(self, run_id: str) -> str | None: ...


class RefHistoryReadProvider(Protocol):
    def get_current(self, ref_name: str) -> RefValue | None: ...

    def page_history(
        self,
        ref_name: str,
        *,
        cursor: PageCursorV1 | None,
        binding: ReadPageBinding,
        page_size: int,
    ) -> PageV1[RefValue]: ...


class SnapshotDiffReadProvider(Protocol):
    def read(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
        *,
        max_items: int,
    ) -> SnapshotDiffRead: ...


class BenchReportSelectionProvider(Protocol):
    def selected_artifact_id(self) -> str | None: ...


class ExecutionProfileCatalogProvider(Protocol):
    def current_catalog(self) -> ExecutionProfileCatalogSnapshotV1 | None: ...


class ContentDomainPermissionResolver(Protocol):
    """Prove exact resource/domain permissions from retained server authority."""

    def for_artifact(
        self,
        artifact: ArtifactWire,
        *,
        resource_kind: str,
    ) -> Permission | None: ...

    def for_ref(
        self,
        ref_name: str,
        value: RefValue,
        artifact: ArtifactWire,
    ) -> Permission | None: ...


@dataclass(frozen=True, slots=True)
class ContentReadCapabilities:
    repository: ContentReadRepository
    immutable_artifact_pages: ImmutableArtifactPageProvider
    payload_reader: ArtifactPayloadReader
    payload_bindings: ArtifactPayloadBindingProvider
    authorization: ReadAuthorizationService
    permission_resolver: ContentDomainPermissionResolver
    specs: SpecBindingProvider
    schema_registry: SchemaRegistryProvider
    proposal_workflows: ConstraintProposalWorkflowProvider
    subject_workflows: SubjectWorkflowReadProvider
    playtest_results: PlaytestResultReadProvider
    refs: RefHistoryReadProvider
    diffs: SnapshotDiffReadProvider
    bench_reports: BenchReportSelectionProvider
    execution_profiles: ExecutionProfileCatalogProvider
    page_factory: MaterializedPageFactory


ContentReadUnitOfWorkFactory = Callable[[], AbstractContextManager[ContentReadCapabilities]]


@dataclass(frozen=True, slots=True)
class _ListDefinition:
    resource_kind: str
    item_resource_kind: str
    stable_sort_schema_id: str
    view_schema_id: str
    projection: str


def _page_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PAGE_ITEMS:
        raise QueryTooBroad(
            "page limit is outside the configured bound",
            max_page_items=MAX_PAGE_ITEMS,
        )
    return value


def _bounded(values: Sequence[Any], *, label: str, max_items: int) -> tuple[Any, ...]:
    selected = tuple(values)
    if len(selected) > max_items:
        raise QueryTooBroad(
            f"{label} query exceeds the configured bound",
            max_items=max_items,
        )
    return selected


def _query_hash(
    *,
    resource_kind: str,
    filters: Mapping[str, JsonValue],
    sort: tuple[str, ...],
    projection: str,
    page_size: int | None = None,
) -> str:
    return canonical_sha256(
        {
            "query_schema_version": "api-read-query@1",
            "api_version": "v1",
            "resource_kind": resource_kind,
            "filters": dict(filters),
            "sort": sort,
            "projection": projection,
            "page_size": page_size,
        }
    )


def _parse_artifact_wire(value: Any, *, expected_id: str | None = None) -> ArtifactWire:
    if not isinstance(value, (ArtifactV1, ArtifactV2)):
        raise IntegrityViolation("content authority returned an invalid Artifact")
    wire = value.model_dump(mode="json")
    try:
        parsed = parse_artifact(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation("content authority returned an invalid Artifact") from exc
    if expected_id is not None and parsed.artifact_id != expected_id:
        raise IntegrityViolation("content authority returned a different Artifact identity")
    if typed_canonical_json(parsed.model_dump(mode="json")) != typed_canonical_json(wire):
        raise IntegrityViolation("content authority returned a noncanonical Artifact")
    return parsed


def _permission(
    value: Permission | None,
    *,
    resource_kind: str,
) -> Permission | None:
    if value is None:
        return None
    if type(value) is not Permission:
        raise IntegrityViolation("content permission resolver returned an invalid value")
    if value.action != _READ_ACTION or value.resource_kind != resource_kind:
        raise IntegrityViolation("content permission resolver returned the wrong permission")
    return value


def _collection_permission(resource_kind: str, scope: DomainScopeValue = "all") -> Permission:
    try:
        return Permission(
            action=_READ_ACTION,
            resource_kind=resource_kind,
            domain_scope=scope,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation("collection permission scope is invalid") from exc


def _payload_binding(
    provider: ArtifactPayloadBindingProvider,
    artifact: ArtifactV2,
) -> TrustedArtifactPayloadBinding:
    value = _optional_payload_binding(provider, artifact)
    if value is None:
        raise IntegrityViolation(
            "trusted Artifact payload binding is unavailable",
            artifact_id=artifact.artifact_id,
        )
    return value


def _optional_payload_binding(
    provider: ArtifactPayloadBindingProvider,
    artifact: ArtifactV2,
) -> TrustedArtifactPayloadBinding | None:
    value = provider.resolve(artifact.artifact_id)
    if value is None:
        return None
    if type(value) is not TrustedArtifactPayloadBinding:
        raise IntegrityViolation(
            "trusted Artifact payload binding is invalid",
            artifact_id=artifact.artifact_id,
        )
    if (
        value.artifact_id != artifact.artifact_id
        or value.artifact_kind != artifact.kind
        or value.payload_hash != artifact.payload_hash
    ):
        raise IntegrityViolation(
            "trusted Artifact payload binding differs from its Artifact",
            artifact_id=artifact.artifact_id,
        )
    return value


def _artifact_summary(
    artifact: ArtifactWire,
    *,
    permission: Permission,
    payload_schema_id: str | None,
) -> ArtifactSummaryV1:
    digest = artifact.payload_hash
    if isinstance(artifact, ArtifactV2) and (
        not isinstance(digest, str)
        or len(digest) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise IntegrityViolation(
            "Artifact payload hash is unavailable for the safe API projection",
            artifact_id=artifact.artifact_id,
        )
    if permission.domain_scope is None:
        raise IntegrityViolation(
            "Artifact domain scope is not proved for content disclosure",
            artifact_id=artifact.artifact_id,
        )
    return ArtifactSummaryV1(
        artifact_id=artifact.artifact_id,
        lineage_schema_version=artifact.lineage_schema_version,
        kind=artifact.kind,
        version_tuple=artifact.version_tuple,
        parent_artifact_ids=tuple(sorted(set(artifact.lineage))),
        payload_hash=digest,
        payload_schema_id=payload_schema_id,
        domain_scope=permission.domain_scope,
        created_at=artifact.created_at,
    )


def _model_payload(
    payload: Mapping[str, Any],
    model: type[_T],
    *,
    label: str,
) -> _T:
    unknown = set(payload) - set(model.model_fields)
    if unknown:
        raise IntegrityViolation(f"{label} payload contains unknown fields")
    try:
        return model.model_validate(payload)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"{label} payload violates its typed schema") from exc


def _scope_union(left: DomainScopeValue, right: DomainScopeValue) -> DomainScopeValue:
    if left is None or right is None:
        raise IntegrityViolation("diff input domain scope is not proved")
    if left == "all" or right == "all":
        return "all"
    return DomainScope(domain_ids=tuple(sorted(set(left.domain_ids) | set(right.domain_ids))))


def _profile_view(
    definition: ExecutionProfileDefinitionV1,
    lifecycle: ExecutionProfileLifecycleV1,
) -> ExecutionProfileViewV1:
    env_contract_version = None
    target_environment_profile = None
    if isinstance(definition.details, EnvironmentProfileDetailsV1):
        env_contract_version = definition.details.contract.env_contract_version
    elif isinstance(definition.details, ConfigExportProfileDetailsV1):
        env_contract_version = definition.details.env_contract_version
        target_environment_profile = definition.details.target_environment_profile
    return ExecutionProfileViewV1(
        profile=definition.profile,
        profile_payload_hash=execution_profile_payload_hash(definition),
        profile_kind=definition.profile_kind,
        status=lifecycle.state,
        compatible_run_kinds=definition.compatible_run_kinds,
        domain_scope=definition.domain_scope,
        stochastic=definition.stochastic,
        input_schema_ids=definition.input_schema_ids,
        output_schema_ids=definition.output_schema_ids,
        required_capabilities=definition.required_capabilities,
        display_name=definition.display_name,
        env_contract_version=env_contract_version,
        target_environment_profile=target_environment_profile,
    )


class _ContentReadOperations:
    def __init__(
        self,
        *,
        capabilities: ContentReadCapabilities,
        max_materialized_items: int,
    ) -> None:
        if (
            isinstance(max_materialized_items, bool)
            or not isinstance(max_materialized_items, int)
            or max_materialized_items < 1
        ):
            raise ValueError("max_materialized_items must be positive")
        self._capabilities = capabilities
        self._repository = capabilities.repository
        self._reader = capabilities.payload_reader
        self._bindings = capabilities.payload_bindings
        self._authorization = capabilities.authorization
        self._permissions = capabilities.permission_resolver
        self._pages = capabilities.page_factory
        self._max_items = max_materialized_items

    def get_artifact(self, principal: Principal, artifact_id: str) -> ArtifactPayloadViewV1:
        artifact, permission = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="artifact",
            projection="artifact-payload-view@1",
        )
        if artifact.kind in {"source_raw", "source_rendered", "cassette_bundle"}:
            raise Forbidden(
                "sensitive source and cassette payloads are not exposed by the generic "
                "Artifact endpoint"
            )
        verified = self._read_verified(artifact)
        return ArtifactPayloadViewV1(
            artifact=_artifact_summary(
                artifact,
                permission=permission,
                payload_schema_id=verified.payload_schema_id,
            ),
            payload=verified.payload,
        )

    def get_spec(self, principal: Principal, artifact_id: str) -> SpecViewV1:
        artifact, permission = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="spec",
            projection="spec-view@1",
        )
        binding = self._spec_binding(artifact)
        verified = self._read_verified(
            artifact,
            expected_kind="ir_snapshot",
            expected_schema="ir-core@1",
        )
        self._snapshot_graph(verified, binding=binding)
        return self._spec_view(artifact, permission, verified, binding)

    def list_specs(
        self,
        principal: Principal,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[SpecViewV1]:
        definition = _ListDefinition(
            "specs", "spec", "spec-artifact-id@1", "spec-view@1", "spec-view@1"
        )
        return self._artifact_page(
            principal,
            definition=definition,
            filters={},
            cursor=cursor,
            limit=limit,
            index_kind="specs",
            expected_kind="ir_snapshot",
            projector=self._project_spec,
            identity=lambda value: (value.artifact.artifact_id, 1),
        )

    def list_graph(
        self,
        principal: Principal,
        artifact_id: str,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[GraphItemV1]:
        limit = _page_limit(limit)
        artifact, permission = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="spec",
            projection="graph-item@1",
        )
        binding = self._spec_binding(artifact)
        query = _query_hash(
            resource_kind="spec_graph",
            filters={"artifact_id": artifact_id},
            sort=("item_kind:asc", "item_id:asc"),
            projection="graph-item@1",
            page_size=limit,
        )
        if cursor is None:
            verified = self._read_verified(
                artifact,
                expected_kind="ir_snapshot",
                expected_schema="ir-core@1",
            )
            values = self._snapshot_graph(verified, binding=binding)
            values = _bounded(values, label="graph", max_items=self._max_items)
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=values,
                collection_permission=permission,
                permission_for=lambda _: permission,
                query_hash=query,
            )
            items = tuple(authorized.items)
        else:
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=(),
                collection_permission=permission,
                permission_for=lambda _: permission,
                query_hash=query,
            )
            items = ()
        return self._materialized_page(
            items,
            binding=authorized.binding,
            query_hash=query,
            definition=_ListDefinition(
                "spec_graph", "spec", "graph-kind-id@1", "graph-item@1", "graph-item@1"
            ),
            cursor=cursor,
            limit=limit,
            model=GraphItemV1,
            identity=lambda value: (f"{value.item_kind}:{value.item_id}", 1),
        )

    def get_schema_registry(
        self,
        principal: Principal,
        version: str,
    ) -> SchemaRegistryDocumentV1:
        value = self._capabilities.schema_registry.get(version)
        if value is None:
            raise DependencyUnavailable(
                "schema registry authority is unavailable",
                component="schema_registry",
            )
        if type(value) is not SchemaRegistryDocumentV1 or value.registry_version != version:
            raise IntegrityViolation("schema registry authority returned an invalid document")
        query = _query_hash(
            resource_kind="schema_registry",
            filters={"version": version},
            sort=(),
            projection="schema-registry-document@1",
        )
        self._authorization.require_singular(
            principal=principal,
            permission=_collection_permission("schema_registry", None),
            query_hash=query,
        )
        return value

    def get_constraint(
        self,
        principal: Principal,
        artifact_id: str,
    ) -> ConstraintSnapshotViewV1:
        artifact, permission = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="constraint",
            projection="constraint-snapshot-view@1",
        )
        verified = self._read_verified(
            artifact,
            expected_kind="constraint_snapshot",
            expected_schema="constraint-snapshot@1",
        )
        return self._constraint_view(artifact, permission, verified)

    def list_constraints(
        self,
        principal: Principal,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[ConstraintSnapshotViewV1]:
        return self._artifact_page(
            principal,
            definition=_ListDefinition(
                "constraints",
                "constraint",
                "constraint-artifact-id@1",
                "constraint-snapshot-view@1",
                "constraint-snapshot-view@1",
            ),
            filters={},
            cursor=cursor,
            limit=limit,
            index_kind="constraints",
            expected_kind="constraint_snapshot",
            projector=self._constraint_view,
            identity=lambda value: (value.artifact.artifact_id, 1),
        )

    def get_constraint_proposal(
        self,
        principal: Principal,
        artifact_id: str,
    ) -> ConstraintProposalReadViewV1:
        artifact, permission = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="constraint_proposal",
            projection="constraint-proposal-read-view@1",
        )
        verified = self._read_verified(
            artifact,
            expected_kind="constraint_proposal",
            expected_schema="constraint-proposal@1",
        )
        return self._constraint_proposal_view(artifact, permission, verified)

    def list_constraint_proposals(
        self,
        principal: Principal,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[ConstraintProposalReadViewV1]:
        return self._artifact_page(
            principal,
            definition=_ListDefinition(
                "constraint_proposals",
                "constraint_proposal",
                "constraint-proposal-artifact-id@1",
                "constraint-proposal-read-view@1",
                "constraint-proposal-read-view@1",
            ),
            filters={},
            cursor=cursor,
            limit=limit,
            index_kind="constraint_proposals",
            expected_kind="constraint_proposal",
            projector=self._constraint_proposal_view,
            projection_model=ConstraintProposalReadViewV1,
            materialize_projection=True,
            identity=lambda value: (
                value.artifact.artifact_id,
                value.workflow_revision,
            ),
        )

    def get_patch(
        self,
        principal: Principal,
        artifact_id: str,
    ) -> PatchArtifactReadViewV1:
        artifact, permission = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="patch",
            projection="patch-artifact-read-view@1",
        )
        if self._patch_workflow(artifact.artifact_id) is None:
            raise NotFound(
                "workflow Patch does not exist",
                artifact_id=artifact.artifact_id,
            )
        verified = self._read_verified(
            artifact,
            expected_kind="patch",
            expected_schema="patch@2",
        )
        return self._patch_view(artifact, permission, verified)

    def list_patches(
        self,
        principal: Principal,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[PatchArtifactReadViewV1]:
        return self._artifact_page(
            principal,
            definition=_ListDefinition(
                "patches",
                "patch",
                "patch-artifact-id@1",
                "patch-artifact-read-view@1",
                "patch-artifact-read-view@1",
            ),
            filters={},
            cursor=cursor,
            limit=limit,
            index_kind="patches",
            expected_kind="patch",
            projector=self._patch_view,
            projection_model=PatchArtifactReadViewV1,
            materialize_projection=True,
            eligible=lambda artifact: self._patch_workflow(artifact.artifact_id) is not None,
            identity=lambda value: (
                value.artifact.artifact_id,
                value.workflow_revision,
            ),
        )

    def get_rollback_request(
        self,
        principal: Principal,
        artifact_id: str,
    ) -> RollbackRequestReadViewV1:
        artifact, permission = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="rollback_request",
            projection="rollback-request-read-view@1",
        )
        verified = self._read_verified(
            artifact,
            expected_kind="rollback_request",
            expected_schema="rollback-request@1",
        )
        return self._rollback_view(artifact, permission, verified)

    def list_rollback_requests(
        self,
        principal: Principal,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[RollbackRequestReadViewV1]:
        return self._artifact_page(
            principal,
            definition=_ListDefinition(
                "rollback_requests",
                "rollback_request",
                "rollback-request-artifact-id@1",
                "rollback-request-read-view@1",
                "rollback-request-read-view@1",
            ),
            filters={},
            cursor=cursor,
            limit=limit,
            index_kind="rollback_requests",
            expected_kind="rollback_request",
            projector=self._rollback_view,
            projection_model=RollbackRequestReadViewV1,
            materialize_projection=True,
            identity=lambda value: (
                value.artifact.artifact_id,
                value.workflow_revision,
            ),
        )

    def get_review(self, principal: Principal, artifact_id: str) -> ReviewArtifactViewV1:
        artifact, permission = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="review",
            projection="review-artifact-view@1",
        )
        verified = self._read_verified(
            artifact,
            expected_kind="review_report",
            expected_schema="review@1",
        )
        return self._review_view(artifact, permission, verified)

    def list_reviews(
        self,
        principal: Principal,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[ReviewArtifactViewV1]:
        return self._artifact_page(
            principal,
            definition=_ListDefinition(
                "reviews",
                "review",
                "review-artifact-id@1",
                "review-artifact-view@1",
                "review-artifact-view@1",
            ),
            filters={},
            cursor=cursor,
            limit=limit,
            index_kind="reviews",
            expected_kind="review_report",
            projector=self._review_view,
            identity=lambda value: (value.artifact.artifact_id, 1),
        )

    def get_task_suite(
        self,
        principal: Principal,
        artifact_id: str,
    ) -> TaskSuiteArtifactViewV1:
        artifact, permission = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="task_suite",
            projection="task-suite-artifact-view@1",
        )
        verified = self._read_verified(
            artifact,
            expected_kind="task_suite",
            expected_schema="task-suite@1",
        )
        return self._task_suite_view(artifact, permission, verified)

    def get_playtest_result(
        self,
        principal: Principal,
        run_id: str,
    ) -> ArtifactPayloadViewV1:
        artifact_id = self._capabilities.playtest_results.result_artifact_id(run_id)
        if artifact_id is None:
            raise NotFound("playtest result does not exist", run_id=run_id)
        artifact, permission = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="playtest_result",
            projection="artifact-payload-view@1",
        )
        verified = self._read_verified(
            artifact,
            expected_kind="playtest_trace",
            expected_schema="playtest-trace@1",
        )
        return ArtifactPayloadViewV1(
            artifact=_artifact_summary(
                artifact,
                permission=permission,
                payload_schema_id=verified.payload_schema_id,
            ),
            payload=verified.payload,
        )

    def list_task_suites(
        self,
        principal: Principal,
        *,
        config_artifact_id: str | None,
        constraint_artifact_id: str | None,
        environment_profile_id: str | None,
        environment_profile_version: int | None,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[TaskSuiteArtifactViewV1]:
        if (environment_profile_id is None) != (environment_profile_version is None):
            raise RequestSchemaInvalid(
                "environment_profile_id and environment_profile_version must be supplied together"
            )
        if environment_profile_version is not None and (
            isinstance(environment_profile_version, bool) or environment_profile_version < 1
        ):
            raise RequestSchemaInvalid("environment_profile_version must be positive")
        filters: dict[str, JsonValue] = {
            "config_artifact_id": config_artifact_id,
            "constraint_artifact_id": constraint_artifact_id,
            "environment_profile_id": environment_profile_id,
            "environment_profile_version": environment_profile_version,
        }
        return self._artifact_page(
            principal,
            definition=_ListDefinition(
                "task_suites",
                "task_suite",
                "task-suite-artifact-id@1",
                "task-suite-artifact-view@1",
                "task-suite-artifact-view@1",
            ),
            filters=filters,
            cursor=cursor,
            limit=limit,
            index_kind="task_suites",
            expected_kind="task_suite",
            projector=self._task_suite_view,
            identity=lambda value: (value.artifact.artifact_id, 1),
        )

    def diff(
        self,
        principal: Principal,
        *,
        base_snapshot_id: str,
        target_snapshot_id: str,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> tuple[SnapshotDiff, PageV1[SnapshotDiffEntry]]:
        limit = _page_limit(limit)
        base_binding = self._capabilities.specs.resolve_snapshot_id(base_snapshot_id)
        target_binding = self._capabilities.specs.resolve_snapshot_id(target_snapshot_id)
        if type(base_binding) is not SpecReadBinding or type(target_binding) is not SpecReadBinding:
            raise NotFound("snapshot diff input does not resolve to a registered spec")
        if (
            base_binding.snapshot_id != base_snapshot_id
            or target_binding.snapshot_id != target_snapshot_id
        ):
            raise IntegrityViolation("snapshot-id authority returned a different binding")
        base, base_permission = self._load_authorized_artifact(
            principal,
            base_binding.artifact_id,
            resource_kind="spec",
            projection="snapshot-diff@1",
        )
        target, target_permission = self._load_authorized_artifact(
            principal,
            target_binding.artifact_id,
            resource_kind="spec",
            projection="snapshot-diff@1",
        )
        if base.kind != "ir_snapshot" or target.kind != "ir_snapshot":
            raise IntegrityViolation("snapshot diff inputs must be ir_snapshot Artifacts")
        if self._spec_binding(base) != base_binding or self._spec_binding(target) != target_binding:
            raise IntegrityViolation("snapshot diff binding differs from registered spec authority")
        permission = _collection_permission(
            "spec",
            _scope_union(base_permission.domain_scope, target_permission.domain_scope),
        )
        query = _query_hash(
            resource_kind="snapshot_diff",
            filters={"base": base_snapshot_id, "target": target_snapshot_id},
            sort=("path:asc",),
            projection="snapshot-diff-entry@1",
            page_size=limit,
        )
        if cursor is None:
            result = self._capabilities.diffs.read(
                base_snapshot_id,
                target_snapshot_id,
                max_items=self._max_items + 1,
            )
            if type(result) is not SnapshotDiffRead:
                raise IntegrityViolation("snapshot diff authority returned an invalid result")
            entries = _bounded(result.entries, label="snapshot diff", max_items=self._max_items)
            paths = tuple(item.path for item in entries)
            if (
                result.diff.base_snapshot_id != base_snapshot_id
                or result.diff.target_snapshot_id != target_snapshot_id
                or result.diff.entry_count != len(entries)
                or paths != tuple(sorted(set(paths)))
            ):
                raise IntegrityViolation("snapshot diff authority returned inconsistent entries")
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=entries,
                collection_permission=permission,
                permission_for=lambda _: permission,
                query_hash=query,
            )
            values = tuple(authorized.items)
            diff = result.diff
        else:
            result = self._capabilities.diffs.read(
                base_snapshot_id,
                target_snapshot_id,
                max_items=self._max_items + 1,
            )
            if type(result) is not SnapshotDiffRead:
                raise IntegrityViolation("snapshot diff authority returned an invalid result")
            diff = result.diff
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=(),
                collection_permission=permission,
                permission_for=lambda _: permission,
                query_hash=query,
            )
            values = ()
        page = self._materialized_page(
            values,
            binding=authorized.binding,
            query_hash=query,
            definition=_ListDefinition(
                "snapshot_diff",
                "spec",
                "diff-path@1",
                "snapshot-diff-entry@1",
                "snapshot-diff-entry@1",
            ),
            cursor=cursor,
            limit=limit,
            model=SnapshotDiffEntry,
            identity=lambda value: (f"diff-entry:{canonical_sha256(value.path)}", 1),
        )
        return diff, page

    def lineage(
        self,
        principal: Principal,
        artifact_id: str,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[LineageEntryV1]:
        limit = _page_limit(limit)
        root, _ = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="artifact",
            projection="lineage-entry@1",
        )
        query = _query_hash(
            resource_kind="artifact_lineage",
            filters={"artifact_id": artifact_id},
            sort=("depth:asc", "artifact_id:asc"),
            projection="lineage-entry@1",
            page_size=limit,
        )
        collection_permission = _collection_permission("artifact")
        query_authorization = self._authorization.filter_collection(
            principal=principal,
            candidates=(),
            collection_permission=collection_permission,
            permission_for=lambda _: collection_permission,
            query_hash=query,
        )
        read_binding = ReadPageBinding(
            resource_kind="artifact_lineage",
            query_hash=query,
            authz_fingerprint=query_authorization.binding.authz_fingerprint,
            stable_sort_schema_id="lineage-depth-artifact@1",
            view_schema_id="lineage-entry@1",
            principal_binding=query_authorization.binding.principal_binding,
        )
        source_page = self._capabilities.immutable_artifact_pages.page_lineage(
            root_artifact_id=root.artifact_id,
            cursor=cursor,
            binding=read_binding,
            page_size=limit,
        )
        if not isinstance(source_page, PageV1) or len(source_page.items) > limit:
            raise IntegrityViolation("lineage authority returned an invalid page")
        source_entries = tuple(source_page.items)
        keys = tuple((value.depth, value.artifact_id) for value in source_entries)
        if any(type(value) is not LineageSourceEntry for value in source_entries) or keys != tuple(
            sorted(set(keys))
        ):
            raise IntegrityViolation("lineage page is duplicate or unsorted")
        artifacts = {
            value.artifact_id: self._get_artifact(value.artifact_id) for value in source_entries
        }
        permissions = {
            artifact_id: _permission(
                self._permissions.for_artifact(artifact, resource_kind="artifact"),
                resource_kind="artifact",
            )
            for artifact_id, artifact in artifacts.items()
        }
        authorized = self._authorization.filter_collection(
            principal=principal,
            candidates=source_entries,
            collection_permission=collection_permission,
            permission_for=lambda value: permissions[value.artifact_id],
            query_hash=query,
        )
        if authorized.binding != query_authorization.binding:
            raise IntegrityViolation("lineage authorization binding changed during the read")
        values = tuple(
            LineageEntryV1(
                artifact=_artifact_summary(
                    artifacts[value.artifact_id],
                    permission=permissions[value.artifact_id],  # type: ignore[arg-type]
                    payload_schema_id=self._summary_schema(artifacts[value.artifact_id]),
                ),
                depth=value.depth,
            )
            for value in authorized.items
        )
        return PageV1[LineageEntryV1](
            read_snapshot_id=source_page.read_snapshot_id,
            items=values,
            next_cursor=source_page.next_cursor,
            expires_at=source_page.expires_at,
        )

    def ref_history(
        self,
        principal: Principal,
        ref_name: str,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[RefHistoryEntryV1]:
        limit = _page_limit(limit)
        current = self._capabilities.refs.get_current(ref_name)
        if current is None:
            raise NotFound("ref does not exist", ref_name=ref_name)
        current_artifact = self._get_artifact(current.artifact_id)
        current_permission = _permission(
            self._permissions.for_ref(ref_name, current, current_artifact),
            resource_kind="ref",
        )
        query = _query_hash(
            resource_kind="ref_history",
            filters={"ref_name": ref_name},
            sort=("revision:asc",),
            projection="ref-history-entry@1",
            page_size=limit,
        )
        self._authorization.require_singular(
            principal=principal,
            permission=current_permission,
            query_hash=query,
        )
        collection_permission = _collection_permission("ref")
        query_authorization = self._authorization.filter_collection(
            principal=principal,
            candidates=(),
            collection_permission=collection_permission,
            permission_for=lambda _: collection_permission,
            query_hash=query,
        )
        read_binding = ReadPageBinding(
            resource_kind="ref_history",
            query_hash=query,
            authz_fingerprint=query_authorization.binding.authz_fingerprint,
            stable_sort_schema_id="ref-history-revision@1",
            view_schema_id="ref-history-entry@1",
            principal_binding=query_authorization.binding.principal_binding,
        )
        source_page = self._capabilities.refs.page_history(
            ref_name,
            cursor=cursor,
            binding=read_binding,
            page_size=limit,
        )
        if not isinstance(source_page, PageV1) or len(source_page.items) > limit:
            raise IntegrityViolation("ref history authority returned an invalid page")
        history = tuple(source_page.items)
        revisions = tuple(value.revision for value in history)
        if revisions != tuple(sorted(set(revisions))) or any(
            revision > current.revision for revision in revisions
        ):
            raise IntegrityViolation("ref history page is duplicate, unsorted, or beyond current")
        if cursor is None and (not revisions or revisions[0] != 1):
            raise IntegrityViolation("ref history first page does not start at revision 1")
        artifacts = {value.revision: self._get_artifact(value.artifact_id) for value in history}
        permissions = {
            value.revision: _permission(
                self._permissions.for_ref(
                    ref_name,
                    value,
                    artifacts[value.revision],
                ),
                resource_kind="ref",
            )
            for value in history
        }
        authorized = self._authorization.filter_collection(
            principal=principal,
            candidates=history,
            collection_permission=collection_permission,
            permission_for=lambda value: permissions[value.revision],
            query_hash=query,
        )
        if authorized.binding != query_authorization.binding:
            raise IntegrityViolation("ref history authorization binding changed during the read")
        values = tuple(
            RefHistoryEntryV1(ref_name=ref_name, value=value) for value in authorized.items
        )
        return PageV1[RefHistoryEntryV1](
            read_snapshot_id=source_page.read_snapshot_id,
            items=values,
            next_cursor=source_page.next_cursor,
            expires_at=source_page.expires_at,
        )

    def get_bench_report(self, principal: Principal) -> dict[str, JsonValue]:
        artifact_id = self._capabilities.bench_reports.selected_artifact_id()
        if artifact_id is None:
            raise DependencyUnavailable(
                "selected BenchReport authority is unavailable",
                component="bench_report",
            )
        artifact, _ = self._load_authorized_artifact(
            principal,
            artifact_id,
            resource_kind="bench_report",
            projection="bench-report@2",
        )
        verified = self._read_verified(
            artifact,
            expected_kind="bench_report",
            expected_schema="bench-report@2",
        )
        if verified.payload.get("schema_version") != "bench-report@2":
            raise IntegrityViolation("selected BenchReport payload has the wrong schema")
        return dict(verified.payload)

    def list_execution_profiles(
        self,
        principal: Principal,
        *,
        profile_kind: ExecutionProfileKindV1 | None,
        run_kind: RunKindRef | None,
        domain_id: str | None,
        status: Literal["active", "replay_only", "disabled"] | None,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[ExecutionProfileReadViewV1]:
        limit = _page_limit(limit)
        catalog = self._catalog()
        domain_filter = None if domain_id is None else DomainScope(domain_ids=(domain_id,))
        query = _query_hash(
            resource_kind="execution_profiles",
            filters={
                "profile_kind": profile_kind,
                "run_kind": None if run_kind is None else run_kind.model_dump(mode="json"),
                "domain_id": domain_id,
                "status": status,
            },
            sort=("profile_id:asc", "version:asc"),
            projection="execution-profile-view@1",
            page_size=limit,
        )
        if cursor is None:
            values = self._profile_views(catalog)
            if profile_kind is not None:
                values = tuple(
                    value for value in values if value.profile.profile_kind == profile_kind
                )
            if run_kind is not None:
                values = tuple(
                    value for value in values if run_kind in value.profile.compatible_run_kinds
                )
            if domain_filter is not None:
                values = tuple(
                    value for value in values if domain_id in value.profile.domain_scope.domain_ids
                )
            if status is not None:
                values = tuple(value for value in values if value.profile.status == status)
            values = _bounded(
                values,
                label="execution profile",
                max_items=self._max_items,
            )
            collection_scope: DomainScopeValue = domain_filter or "all"
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=values,
                collection_permission=_collection_permission("execution_profile", collection_scope),
                permission_for=lambda value: _collection_permission(
                    "execution_profile", value.profile.domain_scope
                ),
                query_hash=query,
            )
            items = tuple(authorized.items)
        else:
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=(),
                collection_permission=_collection_permission(
                    "execution_profile", domain_filter or "all"
                ),
                permission_for=lambda _: _collection_permission(
                    "execution_profile", domain_filter or "all"
                ),
                query_hash=query,
            )
            items = ()
        return self._materialized_page(
            items,
            binding=authorized.binding,
            query_hash=query,
            definition=_ListDefinition(
                "execution_profiles",
                "execution_profile",
                "execution-profile-ref@1",
                "execution-profile-read-view@1",
                "execution-profile-view@1",
            ),
            cursor=cursor,
            limit=limit,
            model=ExecutionProfileReadViewV1,
            identity=lambda value: (
                f"{value.profile.profile.profile_id}:version:{value.profile.profile.version}",
                value.catalog_version,
            ),
        )

    def get_execution_profile(
        self,
        principal: Principal,
        *,
        profile_id: str,
        version: int,
    ) -> ExecutionProfileReadViewV1:
        catalog = self._catalog()
        values = tuple(
            value
            for value in self._profile_views(catalog)
            if value.profile.profile.profile_id == profile_id
            and value.profile.profile.version == version
        )
        if len(values) != 1:
            raise NotFound(
                "execution profile version does not exist in the current catalog",
                profile_id=profile_id,
                version=version,
            )
        value = values[0]
        query = _query_hash(
            resource_kind="execution_profile",
            filters={"profile_id": profile_id, "version": version},
            sort=(),
            projection="execution-profile-view@1",
        )
        self._authorization.require_singular(
            principal=principal,
            permission=_collection_permission("execution_profile", value.profile.domain_scope),
            query_hash=query,
        )
        return value

    def _get_artifact(self, artifact_id: str) -> ArtifactWire:
        value = self._repository.get_artifact(artifact_id)
        if value is None:
            raise NotFound("Artifact does not exist", artifact_id=artifact_id)
        return _parse_artifact_wire(value, expected_id=artifact_id)

    def _load_authorized_artifact(
        self,
        principal: Principal,
        artifact_id: str,
        *,
        resource_kind: str,
        projection: str,
    ) -> tuple[ArtifactWire, Permission]:
        artifact = self._get_artifact(artifact_id)
        permission = _permission(
            self._permissions.for_artifact(artifact, resource_kind=resource_kind),
            resource_kind=resource_kind,
        )
        query = _query_hash(
            resource_kind=resource_kind,
            filters={"artifact_id": artifact_id},
            sort=(),
            projection=projection,
        )
        self._authorization.require_singular(
            principal=principal,
            permission=permission,
            query_hash=query,
        )
        if permission is None:  # require_singular is fail-closed; narrows the type.
            raise IntegrityViolation("Artifact domain scope is not proved")
        return artifact, permission

    def _read_verified(
        self,
        artifact: ArtifactWire,
        *,
        expected_kind: ArtifactKind | None = None,
        expected_schema: str | None = None,
    ) -> VerifiedArtifactPayload:
        value = self._reader.read(artifact.artifact_id)
        if type(value) is not VerifiedArtifactPayload:
            raise IntegrityViolation("Artifact payload reader returned an invalid result")
        exact = _parse_artifact_wire(value.artifact, expected_id=artifact.artifact_id)
        if type(exact) is not ArtifactV2 or type(artifact) is not ArtifactV2:
            raise IntegrityViolation("verified payload must resolve the exact ArtifactV2")
        if typed_canonical_json(exact.model_dump(mode="json")) != typed_canonical_json(
            artifact.model_dump(mode="json")
        ):
            raise IntegrityViolation("Artifact changed between authorization and payload read")
        if expected_kind is not None and value.kind != expected_kind:
            raise IntegrityViolation("Artifact kind differs from the endpoint contract")
        if expected_schema is not None and value.payload_schema_id != expected_schema:
            raise IntegrityViolation("Artifact payload schema differs from the endpoint contract")
        return value

    def _summary_schema(self, artifact: ArtifactWire) -> str | None:
        if type(artifact) is ArtifactV1:
            return None
        binding = _optional_payload_binding(self._bindings, artifact)
        return None if binding is None else binding.payload_schema_id

    def _spec_binding(self, artifact: ArtifactWire) -> SpecReadBinding:
        value = self._capabilities.specs.resolve(artifact.artifact_id)
        if type(value) is not SpecReadBinding or value.artifact_id != artifact.artifact_id:
            raise IntegrityViolation("registered spec binding is unavailable or inconsistent")
        return value

    def _spec_view(
        self,
        artifact: ArtifactWire,
        permission: Permission,
        verified: VerifiedArtifactPayload,
        binding: SpecReadBinding,
    ) -> SpecViewV1:
        return SpecViewV1(
            artifact=_artifact_summary(
                artifact,
                permission=permission,
                payload_schema_id=verified.payload_schema_id,
            ),
            snapshot_id=binding.snapshot_id,
            schema_registry_version=binding.schema_registry_version,
            ref_name=binding.ref_name,
            ref_value=binding.ref_value,
        )

    def _project_spec(
        self,
        artifact: ArtifactWire,
        permission: Permission,
        verified: VerifiedArtifactPayload,
    ) -> SpecViewV1:
        binding = self._spec_binding(artifact)
        self._snapshot_graph(verified, binding=binding)
        return self._spec_view(artifact, permission, verified, binding)

    @staticmethod
    def _snapshot_graph(
        verified: VerifiedArtifactPayload,
        *,
        binding: SpecReadBinding,
    ) -> tuple[GraphItemV1, ...]:
        payload = verified.payload
        if set(payload) != {"meta_schema_version", "entities", "relations"}:
            raise IntegrityViolation("ir_snapshot payload has the wrong top-level shape")
        if (
            not isinstance(payload["meta_schema_version"], str)
            or not payload["meta_schema_version"]
        ):
            raise IntegrityViolation("ir_snapshot meta_schema_version is invalid")
        if not isinstance(payload["entities"], dict) or not isinstance(payload["relations"], dict):
            raise IntegrityViolation("ir_snapshot graph collections are invalid")
        if compute_snapshot_id(payload) != binding.snapshot_id:
            raise IntegrityViolation("registered snapshot_id differs from canonical IR content")
        if verified.artifact.version_tuple.ir_snapshot_id != binding.snapshot_id:
            raise IntegrityViolation("Artifact VersionTuple differs from registered snapshot_id")

        items: list[GraphItemV1] = []
        for identifier, raw in payload["entities"].items():
            if not isinstance(identifier, str) or not identifier or not isinstance(raw, dict):
                raise IntegrityViolation("ir_snapshot entity entry is invalid")
            if "id" in raw or set(raw) - (set(Entity.model_fields) - {"id"}):
                raise IntegrityViolation("ir_snapshot entity entry has unknown fields")
            try:
                entity = Entity.model_validate({"id": identifier, **raw})
            except (TypeError, ValueError, ValidationError) as exc:
                raise IntegrityViolation("ir_snapshot entity violates the IR contract") from exc
            items.append(
                GraphItemV1(
                    item_kind="entity",
                    item_id=identifier,
                    entity=entity,
                )
            )
        for identifier, raw in payload["relations"].items():
            if not isinstance(identifier, str) or not identifier or not isinstance(raw, dict):
                raise IntegrityViolation("ir_snapshot relation entry is invalid")
            if "id" in raw or set(raw) - (set(Relation.model_fields) - {"id"}):
                raise IntegrityViolation("ir_snapshot relation entry has unknown fields")
            try:
                relation = Relation.model_validate({"id": identifier, **raw})
            except (TypeError, ValueError, ValidationError) as exc:
                raise IntegrityViolation("ir_snapshot relation violates the IR contract") from exc
            items.append(
                GraphItemV1(
                    item_kind="relation",
                    item_id=identifier,
                    relation=relation,
                )
            )
        return tuple(sorted(items, key=lambda item: (item.item_kind, item.item_id)))

    def _constraint_view(
        self,
        artifact: ArtifactWire,
        permission: Permission,
        verified: VerifiedArtifactPayload,
    ) -> ConstraintSnapshotViewV1:
        payload = verified.payload
        if set(payload) != {"dsl_grammar_version", "constraints"}:
            raise IntegrityViolation("constraint snapshot payload has the wrong shape")
        grammar = payload["dsl_grammar_version"]
        constraints = payload["constraints"]
        if not isinstance(grammar, str) or not grammar or not isinstance(constraints, list):
            raise IntegrityViolation("constraint snapshot payload is invalid")
        for value in constraints:
            if not isinstance(value, dict):
                raise IntegrityViolation("constraint snapshot contains a non-object constraint")
            _model_payload(value, Constraint, label="constraint")
        return ConstraintSnapshotViewV1(
            artifact=_artifact_summary(
                artifact,
                permission=permission,
                payload_schema_id=verified.payload_schema_id,
            ),
            dsl_grammar_version=grammar,
            constraints=tuple(constraints),
        )

    def _constraint_proposal_view(
        self,
        artifact: ArtifactWire,
        permission: Permission,
        verified: VerifiedArtifactPayload,
    ) -> ConstraintProposalReadViewV1:
        proposal = _model_payload(
            verified.payload,
            ConstraintProposalV1,
            label="constraint proposal",
        )
        workflow = self._capabilities.proposal_workflows.resolve(artifact.artifact_id)
        if type(workflow) is not ConstraintProposalWorkflowBinding:
            raise IntegrityViolation("constraint proposal workflow binding is unavailable")
        return ConstraintProposalReadViewV1(
            artifact=_artifact_summary(
                artifact,
                permission=permission,
                payload_schema_id=verified.payload_schema_id,
            ),
            proposal=proposal,
            workflow_revision=workflow.workflow_revision,
            approval_status=workflow.approval_status,
        )

    def _patch_view(
        self,
        artifact: ArtifactWire,
        permission: Permission,
        verified: VerifiedArtifactPayload,
    ) -> PatchArtifactReadViewV1:
        patch = _model_payload(verified.payload, PatchV2, label="Patch")
        workflow = self._patch_workflow(artifact.artifact_id)
        if workflow is None:
            raise IntegrityViolation("Patch workflow binding is unavailable")
        return PatchArtifactReadViewV1(
            artifact=_artifact_summary(
                artifact,
                permission=permission,
                payload_schema_id=verified.payload_schema_id,
            ),
            patch=patch,
            validation_status=workflow.validation_status,
            regression_status=workflow.regression_status,
            approval_status=workflow.approval_status,
            workflow_revision=workflow.workflow_revision,
        )

    def _patch_workflow(self, artifact_id: str) -> PatchWorkflowReadBinding | None:
        workflow = self._capabilities.subject_workflows.resolve_patch(artifact_id)
        if workflow is not None and type(workflow) is not PatchWorkflowReadBinding:
            raise IntegrityViolation("Patch workflow authority returned an invalid binding")
        return workflow

    def _rollback_view(
        self,
        artifact: ArtifactWire,
        permission: Permission,
        verified: VerifiedArtifactPayload,
    ) -> RollbackRequestReadViewV1:
        request = _model_payload(
            verified.payload,
            RollbackRequestV1,
            label="rollback request",
        )
        workflow = self._capabilities.subject_workflows.resolve_rollback(artifact.artifact_id)
        if type(workflow) is not RollbackWorkflowReadBinding:
            raise IntegrityViolation("rollback workflow binding is unavailable")
        return RollbackRequestReadViewV1(
            artifact=_artifact_summary(
                artifact,
                permission=permission,
                payload_schema_id=verified.payload_schema_id,
            ),
            request=request,
            workflow_revision=workflow.workflow_revision,
            approval_status=workflow.approval_status,
        )

    def _review_view(
        self,
        artifact: ArtifactWire,
        permission: Permission,
        verified: VerifiedArtifactPayload,
    ) -> ReviewArtifactViewV1:
        report = _model_payload(verified.payload, ReviewReport, label="review report")
        if report.snapshot_id != artifact.version_tuple.ir_snapshot_id:
            raise IntegrityViolation("ReviewReport snapshot differs from Artifact VersionTuple")
        return ReviewArtifactViewV1(
            artifact=_artifact_summary(
                artifact,
                permission=permission,
                payload_schema_id=verified.payload_schema_id,
            ),
            report=report,
        )

    def _task_suite_view(
        self,
        artifact: ArtifactWire,
        permission: Permission,
        verified: VerifiedArtifactPayload,
    ) -> TaskSuiteArtifactViewV1:
        suite = _model_payload(verified.payload, TaskSuiteV1, label="task suite")
        return TaskSuiteArtifactViewV1(
            artifact=_artifact_summary(
                artifact,
                permission=permission,
                payload_schema_id=verified.payload_schema_id,
            ),
            task_suite=suite,
        )

    def _artifact_page(
        self,
        principal: Principal,
        *,
        definition: _ListDefinition,
        filters: Mapping[str, JsonValue],
        cursor: PageCursorV1 | None,
        limit: int,
        index_kind: Literal[
            "specs",
            "constraints",
            "constraint_proposals",
            "patches",
            "rollback_requests",
            "reviews",
            "task_suites",
        ],
        expected_kind: ArtifactKind,
        projector: Callable[
            [ArtifactWire, Permission, VerifiedArtifactPayload],
            _T,
        ],
        projection_model: type[_T] | None = None,
        materialize_projection: bool = False,
        eligible: Callable[[ArtifactWire], bool] | None = None,
        identity: Callable[[_T], tuple[str, int]],
    ) -> PageV1[_T]:
        limit = _page_limit(limit)
        query = _query_hash(
            resource_kind=definition.resource_kind,
            filters=filters,
            sort=("artifact_id:asc",),
            projection=definition.projection,
            page_size=limit,
        )
        collection_permission = _collection_permission(definition.item_resource_kind)
        if cursor is None:
            authorization_binding = self._authorization.filter_collection(
                principal=principal,
                candidates=(),
                collection_permission=collection_permission,
                permission_for=lambda _: collection_permission,
                query_hash=query,
            ).binding
        else:
            authorization_binding = self._authorization.require_collection_continuation(
                principal=principal,
                collection_permission=collection_permission,
                query_hash=query,
            )
        read_binding = ReadPageBinding(
            resource_kind=definition.resource_kind,
            query_hash=query,
            authz_fingerprint=authorization_binding.authz_fingerprint,
            stable_sort_schema_id=definition.stable_sort_schema_id,
            view_schema_id=definition.view_schema_id,
            principal_binding=authorization_binding.principal_binding,
        )
        source_cursor = None if materialize_projection else cursor
        source_page_size = (
            min(MAX_PAGE_ITEMS, self._max_items + 1) if materialize_projection else limit
        )
        artifacts: list[ArtifactWire] = []
        source_page: PageV1[ArtifactWire] | None = None
        if cursor is None or not materialize_projection:
            while True:
                source_page = self._capabilities.immutable_artifact_pages.page(
                    index_kind=index_kind,
                    expected_artifact_kind=expected_kind,
                    filters=filters,
                    cursor=source_cursor,
                    binding=read_binding,
                    page_size=source_page_size,
                )
                if not isinstance(source_page, PageV1) or len(source_page.items) > source_page_size:
                    raise IntegrityViolation(
                        "immutable Artifact page authority returned an invalid page"
                    )
                artifacts.extend(_parse_artifact_wire(value) for value in source_page.items)
                if len(artifacts) > self._max_items:
                    raise QueryTooBroad(
                        "content query exceeds the configured materialization bound",
                        max_items=self._max_items,
                    )
                source_cursor = source_page.next_cursor
                if source_cursor is None or not materialize_projection:
                    break
        artifact_ids = tuple(value.artifact_id for value in artifacts)
        if artifact_ids != tuple(sorted(set(artifact_ids))) or any(
            value.kind != expected_kind for value in artifacts
        ):
            raise IntegrityViolation(
                "immutable Artifact page is unsorted, duplicate, or wrong-kind"
            )
        if eligible is not None:
            artifacts = [artifact for artifact in artifacts if eligible(artifact)]
        permissions = {
            value.artifact_id: _permission(
                self._permissions.for_artifact(
                    value,
                    resource_kind=definition.item_resource_kind,
                ),
                resource_kind=definition.item_resource_kind,
            )
            for value in artifacts
        }
        authorized = self._authorization.filter_collection(
            principal=principal,
            candidates=artifacts,
            collection_permission=collection_permission,
            permission_for=lambda value: permissions[value.artifact_id],
            query_hash=query,
        )
        if authorized.binding != authorization_binding:
            raise IntegrityViolation("Artifact page authorization binding changed during the read")
        values = tuple(
            projector(
                artifact,
                permissions[artifact.artifact_id],  # type: ignore[arg-type]
                self._read_verified(
                    artifact,
                    expected_kind=expected_kind,
                    expected_schema={
                        "ir_snapshot": "ir-core@1",
                        "constraint_snapshot": "constraint-snapshot@1",
                        "constraint_proposal": "constraint-proposal@1",
                        "review_report": "review@1",
                        "task_suite": "task-suite@1",
                        "patch": "patch@2",
                        "rollback_request": "rollback-request@1",
                    }[expected_kind],
                ),
            )
            for artifact in authorized.items
        )
        for value in values:
            resource_id, revision = identity(value)
            if not resource_id or revision < 1:
                raise IntegrityViolation("immutable content view identity is invalid")
        if materialize_projection:
            if projection_model is None:
                raise IntegrityViolation("mutable content projection model is unavailable")
            return self._materialized_page(
                values,
                binding=authorization_binding,
                query_hash=query,
                definition=definition,
                cursor=cursor,
                limit=limit,
                model=projection_model,
                identity=identity,
            )
        if source_page is None:  # pragma: no cover - immutable reads always load one page
            raise IntegrityViolation("immutable Artifact page authority returned no page")
        return PageV1[_T](
            read_snapshot_id=source_page.read_snapshot_id,
            items=values,
            next_cursor=source_page.next_cursor,
            expires_at=source_page.expires_at,
        )

    def _materialized_page(
        self,
        values: Sequence[_T],
        *,
        binding: Any,
        query_hash: str,
        definition: _ListDefinition,
        cursor: PageCursorV1 | None,
        limit: int,
        model: type[_T],
        identity: Callable[[_T], tuple[str, int]],
    ) -> PageV1[_T]:
        page_repository = self._pages(_page_limit(limit))
        read_binding = ReadPageBinding(
            resource_kind=definition.resource_kind,
            query_hash=query_hash,
            authz_fingerprint=binding.authz_fingerprint,
            stable_sort_schema_id=definition.stable_sort_schema_id,
            view_schema_id=definition.view_schema_id,
            principal_binding=binding.principal_binding,
        )
        if cursor is None:
            candidates = tuple(
                ReadPageCandidate(
                    resource_id=identity(value)[0],
                    observed_revision=identity(value)[1],
                    canonical_view=value.model_dump(mode="json"),
                )
                for value in values
            )
            internal = page_repository.create(candidates, binding=read_binding)
        else:
            internal = page_repository.page(cursor, binding=read_binding)
        parsed: list[_T] = []
        for item in internal.items:
            try:
                value = model.model_validate(item.canonical_view)
            except (TypeError, ValueError, ValidationError) as exc:
                raise IntegrityViolation("materialized content read view is invalid") from exc
            resource_id, revision = identity(value)
            if item.resource_id != resource_id or item.observed_revision != revision:
                raise IntegrityViolation("materialized content read identity is invalid")
            parsed.append(value)
        return PageV1[_T](
            read_snapshot_id=internal.read_snapshot_id,
            items=tuple(parsed),
            next_cursor=internal.next_cursor,
            expires_at=internal.expires_at,
        )

    def _catalog(self) -> ExecutionProfileCatalogSnapshotV1:
        catalog = self._capabilities.execution_profiles.current_catalog()
        if type(catalog) is not ExecutionProfileCatalogSnapshotV1:
            raise DependencyUnavailable(
                "current execution profile catalog is unavailable",
                component="execution_profile_catalog",
            )
        return catalog

    @staticmethod
    def _profile_views(
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> tuple[ExecutionProfileReadViewV1, ...]:
        lifecycle = {item.profile: item for item in catalog.lifecycle}
        values = tuple(
            ExecutionProfileReadViewV1(
                profile=_profile_view(definition, lifecycle[definition.profile]),
                catalog_version=catalog.catalog_version,
                catalog_digest=catalog.catalog_digest,
            )
            for definition in catalog.definitions
        )
        return tuple(
            sorted(
                values,
                key=lambda value: (
                    value.profile.profile.profile_id,
                    value.profile.profile.version,
                ),
            )
        )


class ContentReadService:
    """Long-lived facade that opens one short authority/UoW scope per read."""

    def __init__(
        self,
        *,
        uow_factory: ContentReadUnitOfWorkFactory,
        max_materialized_items: int,
    ) -> None:
        if not callable(uow_factory):
            raise TypeError("uow_factory must be callable")
        if (
            isinstance(max_materialized_items, bool)
            or not isinstance(max_materialized_items, int)
            or max_materialized_items < 1
        ):
            raise ValueError("max_materialized_items must be positive")
        self._uow_factory = uow_factory
        self._max_items = max_materialized_items

    def _operations(self, capabilities: ContentReadCapabilities) -> _ContentReadOperations:
        if type(capabilities) is not ContentReadCapabilities:
            raise IntegrityViolation("content read UoW returned invalid capabilities")
        return _ContentReadOperations(
            capabilities=capabilities,
            max_materialized_items=self._max_items,
        )

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self._uow_factory() as capabilities:
            operation = getattr(self._operations(capabilities), method)
            return operation(*args, **kwargs)

    def get_artifact(self, principal: Principal, artifact_id: str) -> ArtifactPayloadViewV1:
        return self._call("get_artifact", principal, artifact_id)

    def get_spec(self, principal: Principal, artifact_id: str) -> SpecViewV1:
        return self._call("get_spec", principal, artifact_id)

    def list_specs(self, principal: Principal, **kwargs: Any) -> PageV1[SpecViewV1]:
        return self._call("list_specs", principal, **kwargs)

    def list_graph(
        self, principal: Principal, artifact_id: str, **kwargs: Any
    ) -> PageV1[GraphItemV1]:
        return self._call("list_graph", principal, artifact_id, **kwargs)

    def get_schema_registry(self, principal: Principal, version: str) -> SchemaRegistryDocumentV1:
        return self._call("get_schema_registry", principal, version)

    def get_constraint(self, principal: Principal, artifact_id: str) -> ConstraintSnapshotViewV1:
        return self._call("get_constraint", principal, artifact_id)

    def list_constraints(
        self, principal: Principal, **kwargs: Any
    ) -> PageV1[ConstraintSnapshotViewV1]:
        return self._call("list_constraints", principal, **kwargs)

    def get_constraint_proposal(
        self, principal: Principal, artifact_id: str
    ) -> ConstraintProposalReadViewV1:
        return self._call("get_constraint_proposal", principal, artifact_id)

    def list_constraint_proposals(
        self, principal: Principal, **kwargs: Any
    ) -> PageV1[ConstraintProposalReadViewV1]:
        return self._call("list_constraint_proposals", principal, **kwargs)

    def get_patch(self, principal: Principal, artifact_id: str) -> PatchArtifactReadViewV1:
        return self._call("get_patch", principal, artifact_id)

    def list_patches(self, principal: Principal, **kwargs: Any) -> PageV1[PatchArtifactReadViewV1]:
        return self._call("list_patches", principal, **kwargs)

    def get_rollback_request(
        self, principal: Principal, artifact_id: str
    ) -> RollbackRequestReadViewV1:
        return self._call("get_rollback_request", principal, artifact_id)

    def list_rollback_requests(
        self, principal: Principal, **kwargs: Any
    ) -> PageV1[RollbackRequestReadViewV1]:
        return self._call("list_rollback_requests", principal, **kwargs)

    def get_review(self, principal: Principal, artifact_id: str) -> ReviewArtifactViewV1:
        return self._call("get_review", principal, artifact_id)

    def list_reviews(self, principal: Principal, **kwargs: Any) -> PageV1[ReviewArtifactViewV1]:
        return self._call("list_reviews", principal, **kwargs)

    def get_task_suite(self, principal: Principal, artifact_id: str) -> TaskSuiteArtifactViewV1:
        return self._call("get_task_suite", principal, artifact_id)

    def get_playtest_result(self, principal: Principal, run_id: str) -> ArtifactPayloadViewV1:
        return self._call("get_playtest_result", principal, run_id)

    def list_task_suites(
        self, principal: Principal, **kwargs: Any
    ) -> PageV1[TaskSuiteArtifactViewV1]:
        return self._call("list_task_suites", principal, **kwargs)

    def diff(
        self, principal: Principal, **kwargs: Any
    ) -> tuple[SnapshotDiff, PageV1[SnapshotDiffEntry]]:
        return self._call("diff", principal, **kwargs)

    def lineage(
        self, principal: Principal, artifact_id: str, **kwargs: Any
    ) -> PageV1[LineageEntryV1]:
        return self._call("lineage", principal, artifact_id, **kwargs)

    def ref_history(
        self, principal: Principal, ref_name: str, **kwargs: Any
    ) -> PageV1[RefHistoryEntryV1]:
        return self._call("ref_history", principal, ref_name, **kwargs)

    def get_bench_report(self, principal: Principal) -> dict[str, JsonValue]:
        return self._call("get_bench_report", principal)

    def list_execution_profiles(
        self, principal: Principal, **kwargs: Any
    ) -> PageV1[ExecutionProfileReadViewV1]:
        return self._call("list_execution_profiles", principal, **kwargs)

    def get_execution_profile(
        self, principal: Principal, **kwargs: Any
    ) -> ExecutionProfileReadViewV1:
        return self._call("get_execution_profile", principal, **kwargs)


__all__ = [
    "BenchReportSelectionProvider",
    "ConstraintProposalWorkflowBinding",
    "ConstraintProposalWorkflowProvider",
    "ContentDomainPermissionResolver",
    "ContentReadCapabilities",
    "ContentReadRepository",
    "ContentReadService",
    "ContentReadUnitOfWorkFactory",
    "ExecutionProfileCatalogProvider",
    "PatchWorkflowReadBinding",
    "PlaytestResultReadProvider",
    "RefHistoryReadProvider",
    "RollbackWorkflowReadBinding",
    "SchemaRegistryProvider",
    "SnapshotDiffRead",
    "SnapshotDiffReadProvider",
    "SpecBindingProvider",
    "SpecReadBinding",
    "SubjectWorkflowReadProvider",
]
