"""Synchronous workflow-command composition service (M4c Task 7).

The service maps each versioned HTTP mutation to the exact existing platform
authority — ``ApprovalCommandService`` (draft/submit/decide),
``ApprovedApplyService`` (apply/publish/rollback apply), ``RebaseWorkflowService``
(rebase/resolve) and a narrow spec-upload composition — while keeping every
authority transition and audit append inside one UnitOfWork. CPU-heavy patch and
diff assembly happens OUTSIDE the write transaction, after the object bytes have
been ``put_verified`` blob-first; a rolled-back publication therefore leaves only
verified, GC-eligible orphans.

The three ``*.validate`` operations are Run admissions owned by Task 8. They are
routed through an injected :class:`ValidationAdmissionPort` that stays ``None``
until Task 8 wires the real engine; while absent they fail closed with
``DependencyUnavailable`` and never fabricate a Run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from gameforge.contracts.api import (
    ApprovalDecisionRequestV1,
    ApprovalViewV1,
    ArtifactSummaryV1,
    ConstraintProposalReadViewV1,
    ConstraintValidationAdmissionRequestV1,
    HumanConstraintDraftRequestV1,
    HumanConstraintRevisionRequestV1,
    HumanPatchDraftRequestV1,
    HumanSpecUploadRequestV1,
    PatchArtifactReadViewV1,
    PatchRebaseRequestV1,
    PatchValidationAdmissionRequestV1,
    ResolveConflictsRequestV1,
    RollbackDraftRequestV1,
    RollbackRequestReadViewV1,
    RollbackValidationAdmissionRequestV1,
    RunAcceptedV1,
    SubmitForApprovalRequestV1,
    WorkflowApplyRequestV1,
    WorkflowApplyResultV1,
    WorkflowCommandResponseV1,
    compute_resource_etag,
)
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.config_export import (
    ConfigExportPackageV1,
    canonical_config_export_bytes,
)
from gameforge.contracts.diff import (
    RebaseResult,
    ThreeWayMergePolicyV1,
    compute_merge_policy_digest,
)
from gameforge.contracts.errors import (
    Conflict,
    DependencyUnavailable,
    IntegrityViolation,
    RequestSchemaInvalid,
)
from gameforge.contracts.execution_profiles import (
    ConfigExportProfileDetailsV1,
    ExecutionProfileCatalogSnapshotV1,
    ProfileRefV1,
)
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import (
    ActorContext,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRoutePolicyRefV1,
    DomainScope,
    Principal,
    RolePolicy,
)
from gameforge.contracts.jobs import RunDispatchTraceCarrierV1
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    VersionTuple,
    build_artifact_v2,
)
from gameforge.contracts.storage import ObjectStore, UtcClock
from gameforge.contracts.versions import META_SCHEMA_VERSION
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalPolicyV1,
    ConstraintProposalV1,
    ConstraintSourceBinding,
    PatchTargetBindingV1,
    RollbackRequestV1,
    RollbackTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.approvals import build_approval_requirements
from gameforge.platform.approvals.apply import ApprovedApplyRequest, ApprovedApplyService
from gameforge.platform.approvals.commands import (
    ApprovalCommandContext,
    ApprovalCommandService,
    ApprovalDecisionRequest,
    PreparedDraft,
    PreparedObjectBinding,
)
from gameforge.platform.diff.ir_rebase import (
    REBASE_TOOL_VERSION,
    compile_rebased_patch,
    snapshot_from_canonical_view,
)
from gameforge.platform.diff.rebase import RebaseMaterial, RebaseWorkflowService
from gameforge.platform.diff.three_way import compute_three_way_merge
from gameforge.platform.read_models.workflows import CurrentApprovalProgressProjector
from gameforge.platform.workflow.readers import (
    WorkflowTypedReaders,
    workflow_target_snapshot_id,
)
from gameforge.platform.workflow.spec import SpecPublicationPlan, SpecUploadService
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch


_SPEC_TOOL_VERSION = "human-spec-upload@1"
_PATCH_TOOL_VERSION = "human-patch-draft@1"
_CONSTRAINT_TOOL_VERSION = "human-constraint-draft@1"
_ROLLBACK_TOOL_VERSION = "human-rollback-draft@1"
# Canonical payload schema ids stamped into each produced Artifact's immutable meta.
# Every producer declares the schema of its payload so the terminal publisher's
# input-parent lineage resolution (publisher._parent_info) can read it; the M4c
# read models resolve the same value from the artifact kind. Omitting it fails the
# publisher closed when one of these drafts is later a validation-Run input parent.
_PATCH_SCHEMA_ID = "patch@2"
_IR_SNAPSHOT_SCHEMA_ID = "ir-core@1"
_CONSTRAINT_PROPOSAL_SCHEMA_ID = "constraint-proposal@1"
_ROLLBACK_REQUEST_SCHEMA_ID = "rollback-request@1"
_WORKFLOW_MERGE_POLICY = ThreeWayMergePolicyV1(
    policy_version="workflow-three-way@1",
    collection_identities=(),
    policy_digest=compute_merge_policy_digest("workflow-three-way@1", ()),
)


# ── injected server metadata + returned outcome ──────────────────────────────
@dataclass(frozen=True, slots=True)
class WorkflowServerContext:
    """Server-owned request metadata forwarded from the transport."""

    actor: ActorContext
    request_id: str
    trace_id: str | None
    idempotency_key: str
    request_hash: str
    if_match: str
    resource_id: str
    dispatch_trace_carrier: RunDispatchTraceCarrierV1 | None = None


@dataclass(frozen=True, slots=True)
class WorkflowCommandOutcome:
    value: WorkflowCommandResponseV1
    resource_kind: str
    resource_id: str
    revision: int


# ── deferred/injected collaborators ──────────────────────────────────────────
class ValidationAdmissionPort(Protocol):
    """Task 8 Run-admission seam. ``None`` until Task 8 injects the real engine."""

    def admit(
        self,
        *,
        operation: str,
        resource_id: str,
        request: PatchValidationAdmissionRequestV1
        | ConstraintValidationAdmissionRequestV1
        | RollbackValidationAdmissionRequestV1,
        actor: ActorContext,
        server: WorkflowServerContext,
    ) -> RunAcceptedV1: ...


class WorkflowConfigExporter(Protocol):
    """Game-specific deterministic adapter used only for requested Patch exports."""

    def export(
        self,
        *,
        export_profile: ProfileRefV1,
        preview_snapshot_id: str,
        preview_payload: Mapping[str, object],
        constraint_snapshot_artifact_id: str,
        constraints: tuple[Constraint, ...],
    ) -> ConfigExportPackageV1: ...


class WorkflowScopeResolver(Protocol):
    """Resolve the exact affected DomainScope for subjects lacking a declared one."""

    def resolve_patch_scope(
        self,
        *,
        base_artifact: ArtifactV2,
        patch: PatchV2,
    ) -> DomainScope: ...

    def resolve_rollback_scope(
        self,
        *,
        target_artifact: ArtifactV2,
        request: RollbackRequestV1,
    ) -> DomainScope: ...


class WorkflowGovernanceProvider(Protocol):
    """Resolve the exact current governance snapshot at request time.

    The production composition resolves governance from the authoritative
    ``SqlPolicySnapshotRepository`` per request (governance is immutable per exact
    ref, so a fresh read is cheap and never touches an unmigrated database at build
    time). Tests may inject a concrete :class:`WorkflowGovernance` directly.
    """

    def current(self) -> WorkflowGovernance: ...


@dataclass(frozen=True, slots=True)
class WorkflowGovernance:
    """The exact current governance snapshot stamped onto new draft ApprovalItems."""

    registry: DomainRegistryV1
    route: DomainRoutePolicy
    roles: RolePolicy
    approval: ApprovalPolicyV1

    def domain_registry_ref(self) -> DomainRegistryRefV1:
        return DomainRegistryRefV1(
            registry_version=self.registry.registry_version,
            registry_digest=self.registry.registry_digest,
        )

    def route_ref(self) -> DomainRoutePolicyRefV1:
        return DomainRoutePolicyRefV1(
            route_version=self.route.route_version,
            route_digest=self.route.route_digest,
            domain_registry_ref=self.route.domain_registry_ref,
        )

    def approval_ref(self) -> ApprovalPolicyRefV1:
        return ApprovalPolicyRefV1(
            policy_version=self.approval.policy_version,
            policy_digest=self.approval.policy_digest,
        )


@dataclass(frozen=True, slots=True)
class WorkflowReadPort:
    """One short read transaction's exact authorities for assembly and projection."""

    artifacts: Any
    refs: Any
    approvals: Any
    policies: Any
    readers: WorkflowTypedReaders
    progress_projector: CurrentApprovalProgressProjector


WorkflowReadScope = Callable[[], AbstractContextManager[WorkflowReadPort]]


def _utc_text(clock: UtcClock) -> str:
    now = clock.now_utc()
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("workflow command clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_artifact(read: WorkflowReadPort, artifact_id: str) -> ArtifactV2:
    artifact = read.artifacts.get(artifact_id)
    if not isinstance(artifact, ArtifactV2):
        raise Conflict("required Artifact is unavailable", artifact_id=artifact_id)
    return artifact


def _artifact_summary(
    artifact: ArtifactV2,
    *,
    domain_scope: Any,
    payload_schema_id: str | None,
) -> ArtifactSummaryV1:
    return ArtifactSummaryV1(
        artifact_id=artifact.artifact_id,
        lineage_schema_version="lineage@2",
        kind=artifact.kind,
        version_tuple=artifact.version_tuple,
        parent_artifact_ids=tuple(sorted(set(artifact.lineage))),
        payload_hash=artifact.payload_hash,
        payload_schema_id=payload_schema_id,
        domain_scope=domain_scope,
        created_at=artifact.created_at,
    )


@dataclass(frozen=True, slots=True)
class _AssembledDraft:
    prepared: PreparedDraft
    view: WorkflowCommandResponseV1


class WorkflowCommandService:
    """Route one synchronous workflow command to its exact platform authority."""

    def __init__(
        self,
        *,
        clock: UtcClock,
        object_store: ObjectStore,
        read_scope: WorkflowReadScope,
        approval_commands: ApprovalCommandService,
        apply_service: ApprovedApplyService,
        rebase_service: RebaseWorkflowService,
        spec_service: SpecUploadService,
        governance: WorkflowGovernance | WorkflowGovernanceProvider | None,
        scope_resolver: WorkflowScopeResolver | None,
        admission: ValidationAdmissionPort | None,
        execution_profile_catalog: ExecutionProfileCatalogSnapshotV1 | None = None,
        config_exporter: WorkflowConfigExporter | None = None,
    ) -> None:
        self._clock = clock
        self._objects = object_store
        self._read_scope = read_scope
        self._approvals = approval_commands
        self._apply = apply_service
        self._rebase = rebase_service
        self._spec = spec_service
        self._governance = governance
        self._scope_resolver = scope_resolver
        self._admission = admission
        self._catalog = execution_profile_catalog
        self._config_exporter = config_exporter

    # ── dispatch ─────────────────────────────────────────────────────────────
    def execute(
        self,
        *,
        operation: str,
        payload: Any,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        handler = _HANDLERS.get(operation)
        if handler is None:
            raise IntegrityViolation("unknown workflow operation", operation=operation)
        return handler(self, payload, server)

    # ── shared context ───────────────────────────────────────────────────────
    def _context(self, server: WorkflowServerContext) -> ApprovalCommandContext:
        principal = server.actor.principal
        return ApprovalCommandContext(
            actor=AuditActor(principal_id=principal.id, principal_kind=principal.kind),
            initiated_by=None,
            request_id=server.request_id,
            run_id=None,
            trace_id=server.trace_id,
            idempotency_scope=f"principal:{principal.id}",
            idempotency_key=server.idempotency_key,
            request_hash=server.request_hash,
            if_match=server.if_match,
        )

    def _require_governance(self) -> WorkflowGovernance:
        governance = self._governance
        if governance is None:
            raise DependencyUnavailable(
                "workflow governance is unavailable",
                component="workflow_governance",
            )
        if isinstance(governance, WorkflowGovernance):
            return governance
        resolved = governance.current()
        if not isinstance(resolved, WorkflowGovernance):
            raise IntegrityViolation("workflow governance provider returned an invalid snapshot")
        return resolved

    def _require_scope_resolver(self) -> WorkflowScopeResolver:
        if self._scope_resolver is None:
            raise DependencyUnavailable(
                "workflow domain-scope resolution is unavailable",
                component="workflow_domain_scope",
            )
        return self._scope_resolver

    def _proposer(self, server: WorkflowServerContext) -> AuditActor:
        principal = server.actor.principal
        return AuditActor(principal_id=principal.id, principal_kind=principal.kind)

    # ── spec.upload ──────────────────────────────────────────────────────────
    def _spec_upload(
        self,
        payload: HumanSpecUploadRequestV1,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        try:
            snapshot = snapshot_from_canonical_view(payload.content_payload)
        except IntegrityViolation as exc:
            raise RequestSchemaInvalid(
                "spec content_payload is not a canonical IR snapshot"
            ) from exc
        if snapshot.meta_schema_version != payload.meta_schema_version:
            raise Conflict(
                "spec payload meta schema differs from the declared version",
                declared_meta_schema_version=payload.meta_schema_version,
                payload_meta_schema_version=snapshot.meta_schema_version,
            )
        if snapshot.meta_schema_version != META_SCHEMA_VERSION:
            raise Conflict(
                "spec payload uses an unsupported meta schema version",
                meta_schema_version=snapshot.meta_schema_version,
            )
        content = snapshot.content_payload
        snapshot_id = snapshot.snapshot_id
        stored = self._objects.put_verified(canonical_json(content).encode("utf-8"))
        artifact = build_artifact_v2(
            kind="ir_snapshot",
            version_tuple=VersionTuple(
                ir_snapshot_id=snapshot_id,
                tool_version=_SPEC_TOOL_VERSION,
            ),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": _IR_SNAPSHOT_SCHEMA_ID,
                "schema_registry_version": payload.schema_registry_version,
                "meta_schema_version": payload.meta_schema_version,
                "domain_scope": payload.domain_scope.model_dump(mode="json"),
            },
            created_at=_utc_text(self._clock),
        )
        plan = SpecPublicationPlan(
            artifact=artifact,
            binding=PreparedObjectBinding(
                object_ref=stored.ref,
                location=stored.location,
                expected_revision=None,
            ),
            ref_name=payload.ref_name,
            expected_ref=payload.expected_ref,
            snapshot_id=snapshot_id,
            schema_registry_version=payload.schema_registry_version,
            domain_scope=payload.domain_scope,
        )
        view = self._spec.upload(plan=plan, context=self._context(server))
        return WorkflowCommandOutcome(
            value=view,
            resource_kind="spec_ref",
            resource_id=view.ref_name or payload.ref_name,
            revision=(view.ref_value.revision if view.ref_value is not None else 1),
        )

    # ── draft assembly + publication ─────────────────────────────────────────
    def _publish_assembled(
        self,
        assembled: _AssembledDraft,
        server: WorkflowServerContext,
        *,
        expected_subject_head: SubjectHead | None,
    ) -> WorkflowCommandOutcome:
        result = self._approvals.publish_draft(
            prepared=assembled.prepared,
            context=self._context(server),
        )
        committed = result.approval_item
        # An idempotent replay returns the RETAINED committed item, whose server-owned
        # ``created_at`` is authoritative. The freshly assembled item carries a new
        # request-time ``created_at`` under a real advancing clock, so verify the
        # assembled item matches the committed one modulo that server timestamp, then
        # project the response from the committed creation time. A duplicate exact
        # request therefore replays identical response bytes instead of raising.
        normalized = assembled.prepared.approval_item.model_copy(
            update={"created_at": committed.created_at}
        )
        if committed != normalized:
            raise IntegrityViolation("published draft differs from the assembled item")
        view = _reproject_view_created_at(assembled.view, committed.created_at)
        return WorkflowCommandOutcome(
            value=view,
            resource_kind=committed.subject_kind,
            resource_id=committed.subject_artifact_id,
            revision=committed.workflow_revision,
        )

    def _patch_draft(
        self,
        payload: HumanPatchDraftRequestV1,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        governance = self._require_governance()
        resolver = self._require_scope_resolver()
        with self._read_scope() as read:
            base_artifact = _require_artifact(read, payload.base_snapshot_artifact_id)
            if base_artifact.kind != "ir_snapshot":
                raise Conflict("patch base Artifact is not an ir_snapshot")
            if (
                payload.expected_ref is not None
                and payload.expected_ref.artifact_id != base_artifact.artifact_id
            ):
                raise Conflict(
                    "patch expected ref does not bind the exact base Artifact",
                    expected_ref_artifact_id=payload.expected_ref.artifact_id,
                    base_artifact_id=base_artifact.artifact_id,
                )
            base_snapshot = read.readers.load_snapshot(base_artifact)
            patch = self._compile_patch(payload, base_snapshot)
            preview = apply_patch(base_snapshot, patch)
            scope = resolver.resolve_patch_scope(base_artifact=base_artifact, patch=patch)
            constraint_artifact, constraints = self._resolve_patch_export_inputs(
                payload=payload,
                read=read,
            )
        created_at = _utc_text(self._clock)
        patch_stored = self._objects.put_verified(
            canonical_json(patch.model_dump(mode="json")).encode("utf-8")
        )
        patch_artifact = build_artifact_v2(
            kind="patch",
            version_tuple=VersionTuple(
                ir_snapshot_id=base_snapshot.snapshot_id,
                tool_version=_PATCH_TOOL_VERSION,
            ),
            lineage=(base_artifact.artifact_id,),
            payload_hash=patch_stored.ref.sha256,
            object_ref=patch_stored.ref,
            meta={
                "payload_schema_id": _PATCH_SCHEMA_ID,
                "domain_scope": scope.model_dump(mode="json"),
            },
            created_at=created_at,
        )
        preview_stored = self._objects.put_verified(
            canonical_json(preview.content_payload).encode("utf-8")
        )
        preview_artifact = build_artifact_v2(
            kind="ir_snapshot",
            version_tuple=VersionTuple(
                ir_snapshot_id=preview.snapshot_id,
                tool_version=_PATCH_TOOL_VERSION,
            ),
            lineage=(base_artifact.artifact_id, patch_artifact.artifact_id),
            payload_hash=preview_stored.ref.sha256,
            object_ref=preview_stored.ref,
            meta={
                "payload_schema_id": _IR_SNAPSHOT_SCHEMA_ID,
                "domain_scope": scope.model_dump(mode="json"),
            },
            created_at=created_at,
        )
        config_artifacts, config_bindings = self._assemble_patch_config_exports(
            payload=payload,
            preview=preview,
            preview_artifact=preview_artifact,
            constraint_artifact=constraint_artifact,
            constraints=constraints,
            domain_scope=scope,
            created_at=created_at,
        )
        binding = PatchTargetBindingV1(
            target_artifact_id=preview_artifact.artifact_id,
            target_snapshot_id=preview.snapshot_id,
            target_digest=preview_artifact.payload_hash,
            ref_name=payload.ref_name,
            expected_ref=payload.expected_ref,
        )
        item = self._new_draft_item(
            governance=governance,
            subject_kind="patch",
            subject_artifact=patch_artifact,
            subject_revision=1,
            domain_scope=scope,
            target_binding=binding,
            proposer=self._proposer(server),
            created_at=created_at,
        )
        prepared = PreparedDraft(
            subject_artifact=patch_artifact,
            companion_artifacts=(preview_artifact, *config_artifacts),
            object_bindings=(
                PreparedObjectBinding(
                    object_ref=patch_stored.ref,
                    location=patch_stored.location,
                    expected_revision=None,
                ),
                PreparedObjectBinding(
                    object_ref=preview_stored.ref,
                    location=preview_stored.location,
                    expected_revision=None,
                ),
                *config_bindings,
            ),
            approval_item=item,
            expected_subject_head=None,
        )
        view = PatchArtifactReadViewV1(
            artifact=_artifact_summary(
                patch_artifact,
                domain_scope=scope,
                payload_schema_id="patch@2",
            ),
            patch=patch,
            validation_status="not_started",
            regression_status="not_started",
            approval_status=item.status,
            workflow_revision=item.workflow_revision,
        )
        return self._publish_assembled(
            _AssembledDraft(prepared=prepared, view=view),
            server,
            expected_subject_head=None,
        )

    def _resolve_patch_export_inputs(
        self,
        *,
        payload: HumanPatchDraftRequestV1,
        read: WorkflowReadPort,
    ) -> tuple[ArtifactV2 | None, tuple[Constraint, ...]]:
        if not payload.candidate_export_profiles:
            return None, ()
        if self._catalog is None:
            raise DependencyUnavailable(
                "Patch config-export profile catalog is unavailable",
                component="workflow_execution_profiles",
            )
        if self._config_exporter is None:
            raise DependencyUnavailable(
                "Patch config exporter is unavailable",
                component="workflow_config_exporter",
            )
        constraint_id = payload.constraint_snapshot_artifact_id
        if constraint_id is None:  # guarded by HumanPatchDraftRequestV1
            raise IntegrityViolation("Patch export profiles require a constraint snapshot")
        constraint_artifact = _require_artifact(read, constraint_id)
        if (
            constraint_artifact.kind != "constraint_snapshot"
            or constraint_artifact.version_tuple.constraint_snapshot_id is None
        ):
            raise Conflict("Patch export constraint is not a constraint_snapshot Artifact")
        constraints = tuple(read.readers.load_constraints(constraint_artifact))
        for index, profile in enumerate(payload.candidate_export_profiles):
            definitions = tuple(
                definition
                for definition in self._catalog.definitions
                if definition.profile == profile
            )
            lifecycle = tuple(item for item in self._catalog.lifecycle if item.profile == profile)
            if (
                len(definitions) != 1
                or len(lifecycle) != 1
                or definitions[0].profile_kind != "config_export"
                or lifecycle[0].state != "active"
            ):
                raise Conflict(
                    "Patch export profile is not an active config_export profile",
                    profile_id=profile.profile_id,
                    profile_version=profile.version,
                )
            read.policies.resolve_execution_profile(
                catalog_version=self._catalog.catalog_version,
                catalog_digest=self._catalog.catalog_digest,
                field_path=f"/candidate_export_profiles/{index}",
                profile=profile,
                expected_profile_kind="config_export",
            )
        return constraint_artifact, constraints

    def _assemble_patch_config_exports(
        self,
        *,
        payload: HumanPatchDraftRequestV1,
        preview: Snapshot,
        preview_artifact: ArtifactV2,
        constraint_artifact: ArtifactV2 | None,
        constraints: tuple[Constraint, ...],
        domain_scope: DomainScope,
        created_at: str,
    ) -> tuple[tuple[ArtifactV2, ...], tuple[PreparedObjectBinding, ...]]:
        if not payload.candidate_export_profiles:
            return (), ()
        if self._config_exporter is None or constraint_artifact is None:
            raise IntegrityViolation("resolved Patch export dependencies are unavailable")
        artifacts: list[ArtifactV2] = []
        bindings: list[PreparedObjectBinding] = []
        for profile in payload.candidate_export_profiles:
            if self._catalog is None:  # guarded during exact input resolution
                raise IntegrityViolation("config-export profile catalog is unavailable")
            definitions = tuple(
                definition
                for definition in self._catalog.definitions
                if definition.profile == profile
            )
            if len(definitions) != 1 or not isinstance(
                definitions[0].details, ConfigExportProfileDetailsV1
            ):
                raise IntegrityViolation("resolved config-export profile details are unavailable")
            details = definitions[0].details
            package = self._config_exporter.export(
                export_profile=profile,
                # The package field is an Artifact ID despite the historical port
                # parameter name; bind it to the exact prepared preview candidate.
                preview_snapshot_id=preview_artifact.artifact_id,
                preview_payload=preview.content_payload,
                constraint_snapshot_artifact_id=constraint_artifact.artifact_id,
                constraints=constraints,
            )
            if (
                package.export_profile != profile
                or package.source_preview_artifact_id != preview_artifact.artifact_id
                or package.constraint_snapshot_artifact_id != constraint_artifact.artifact_id
                or package.target_environment_profile != details.target_environment_profile
                or package.env_contract_version != details.env_contract_version
                or package.format_schema_id != details.format_schema_id
                or package.package_schema_version != details.package_schema_version
            ):
                raise IntegrityViolation(
                    "config exporter returned a package bound to different Patch inputs"
                )
            stored = self._objects.put_verified(canonical_config_export_bytes(package))
            artifact = build_artifact_v2(
                kind="config_export",
                version_tuple=VersionTuple(
                    ir_snapshot_id=preview.snapshot_id,
                    constraint_snapshot_id=(
                        constraint_artifact.version_tuple.constraint_snapshot_id
                    ),
                    tool_version="config-export@1",
                    env_contract_version=package.env_contract_version,
                ),
                lineage=(preview_artifact.artifact_id, constraint_artifact.artifact_id),
                payload_hash=stored.ref.sha256,
                object_ref=stored.ref,
                meta={
                    "payload_schema_id": "config-export-package@1",
                    "export_profile": profile.model_dump(mode="json"),
                    "domain_scope": domain_scope.model_dump(mode="json"),
                },
                created_at=created_at,
            )
            artifacts.append(artifact)
            bindings.append(
                PreparedObjectBinding(
                    object_ref=stored.ref,
                    location=stored.location,
                    expected_revision=None,
                )
            )
        return tuple(artifacts), tuple(bindings)

    def _compile_patch(
        self,
        payload: HumanPatchDraftRequestV1,
        base_snapshot: Snapshot,
    ) -> PatchV2:
        provisional = PatchV2(
            revision=1,
            base_snapshot_id=base_snapshot.snapshot_id,
            target_snapshot_id=base_snapshot.snapshot_id,
            expected_to_fix=list(payload.expected_to_fix),
            preconditions=[dict(value) for value in payload.preconditions],
            side_effect_risk=payload.side_effect_risk,
            ops=list(payload.ops),
            produced_by="human",
            producer_run_id=None,
            rationale=payload.rationale,
        )
        try:
            preview = apply_patch(base_snapshot, provisional)
        except PatchRejected as exc:
            raise Conflict(
                "patch ops do not apply to the exact base snapshot",
                reason=exc.reason,
                op_id=exc.op_id,
            ) from exc
        return PatchV2(
            revision=1,
            base_snapshot_id=base_snapshot.snapshot_id,
            target_snapshot_id=preview.snapshot_id,
            expected_to_fix=list(payload.expected_to_fix),
            preconditions=[dict(value) for value in payload.preconditions],
            side_effect_risk=payload.side_effect_risk,
            ops=list(payload.ops),
            produced_by="human",
            producer_run_id=None,
            rationale=payload.rationale,
        )

    def _constraint_draft(
        self,
        payload: HumanConstraintDraftRequestV1,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        return self._publish_constraint(payload, server, revision=1, expected_head=None)

    def _constraint_revise(
        self,
        payload: HumanConstraintRevisionRequestV1,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        with self._read_scope() as read:
            old_item = read.approvals.get(payload.approval_id)
            if not isinstance(old_item, ApprovalItem):
                raise Conflict(
                    "constraint proposal does not exist", approval_id=payload.approval_id
                )
            current = read.approvals.current(old_item.subject_series_id)
        if current is None:
            raise Conflict(
                "constraint proposal has no current head", approval_id=payload.approval_id
            )
        head, current_item = current
        if current_item.subject_artifact_id != server.resource_id:
            raise Conflict("constraint revision path does not bind the current subject Artifact")
        if current_item.approval_id != payload.approval_id:
            raise Conflict("constraint revise target is not the current head")
        if head.revision != payload.expected_subject_head_revision:
            raise Conflict("constraint revise subject head is stale")
        if current_item.workflow_revision != payload.expected_workflow_revision:
            raise Conflict(
                "constraint revise workflow revision is stale",
                expected_workflow_revision=payload.expected_workflow_revision,
                actual_workflow_revision=current_item.workflow_revision,
            )
        return self._publish_constraint(
            payload,
            server,
            revision=current_item.subject_revision + 1,
            expected_head=head,
            supersedes=current_item,
        )

    def _publish_constraint(
        self,
        payload: HumanConstraintDraftRequestV1,
        server: WorkflowServerContext,
        *,
        revision: int,
        expected_head: SubjectHead | None,
        supersedes: ApprovalItem | None = None,
    ) -> WorkflowCommandOutcome:
        governance = self._require_governance()
        with self._read_scope() as read:
            base_snapshot_id: str | None = None
            base_artifact_id: str | None = None
            source_bindings: list[ConstraintSourceBinding] = []
            if payload.base_constraint_snapshot_artifact_id is not None:
                base_artifact = _require_artifact(
                    read, payload.base_constraint_snapshot_artifact_id
                )
                if (
                    base_artifact.kind != "constraint_snapshot"
                    or base_artifact.version_tuple.constraint_snapshot_id is None
                ):
                    raise Conflict("constraint proposal base is not a constraint_snapshot")
                base_artifact_id = base_artifact.artifact_id
                base_snapshot_id = base_artifact.version_tuple.constraint_snapshot_id
            for source_id in payload.source_artifact_ids:
                source = _require_artifact(read, source_id)
                source_bindings.append(
                    ConstraintSourceBinding(
                        source_artifact_id=source_id,
                        provenance_hash=source.payload_hash,
                    )
                )
        proposal = ConstraintProposalV1(
            revision=revision,
            supersedes_artifact_id=(None if supersedes is None else supersedes.subject_artifact_id),
            base_constraint_snapshot_id=base_snapshot_id,
            dsl_grammar_version=payload.dsl_grammar_version,
            domain_scope=payload.domain_scope,
            constraints=payload.constraints,
            source_bindings=tuple(source_bindings),
            produced_by="human",
            producer_run_id=None,
            rationale=payload.rationale,
        )
        created_at = _utc_text(self._clock)
        stored = self._objects.put_verified(
            canonical_json(proposal.model_dump(mode="json")).encode("utf-8")
        )
        lineage = tuple(
            sorted(
                {
                    *(() if supersedes is None else (supersedes.subject_artifact_id,)),
                    *(() if base_artifact_id is None else (base_artifact_id,)),
                    *payload.source_artifact_ids,
                }
            )
        )
        artifact = build_artifact_v2(
            kind="constraint_proposal",
            version_tuple=VersionTuple(
                # A human typed proposal with no base Snapshot still needs an exact
                # producer input revision.  Use the complete immutable proposal bytes;
                # the DSL grammar is schema metadata, never a document revision.
                doc_version=(None if base_snapshot_id is not None else stored.ref.sha256),
                constraint_snapshot_id=base_snapshot_id,
                tool_version=_CONSTRAINT_TOOL_VERSION,
            ),
            lineage=lineage,
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": _CONSTRAINT_PROPOSAL_SCHEMA_ID,
                "domain_scope": payload.domain_scope.model_dump(mode="json"),
            },
            created_at=created_at,
        )
        series_id = (
            expected_head.subject_series_id
            if expected_head is not None
            else f"series:constraint_proposal:{artifact.artifact_id}"
        )
        item = self._new_draft_item(
            governance=governance,
            subject_kind="constraint_proposal",
            subject_artifact=artifact,
            subject_revision=revision,
            domain_scope=payload.domain_scope,
            target_binding=None,
            proposer=self._proposer(server),
            created_at=created_at,
            series_id=series_id,
            supersedes_approval_id=None if supersedes is None else supersedes.approval_id,
        )
        prepared = PreparedDraft(
            subject_artifact=artifact,
            companion_artifacts=(),
            object_bindings=(
                PreparedObjectBinding(
                    object_ref=stored.ref,
                    location=stored.location,
                    expected_revision=None,
                ),
            ),
            approval_item=item,
            expected_subject_head=expected_head,
            expected_previous_workflow_revision=(
                None
                if not isinstance(payload, HumanConstraintRevisionRequestV1)
                else payload.expected_workflow_revision
            ),
        )
        view = ConstraintProposalReadViewV1(
            artifact=_artifact_summary(
                artifact,
                domain_scope=payload.domain_scope,
                payload_schema_id="constraint-proposal@1",
            ),
            proposal=proposal,
            workflow_revision=item.workflow_revision,
            approval_status=item.status,
        )
        return self._publish_assembled(
            _AssembledDraft(prepared=prepared, view=view),
            server,
            expected_subject_head=expected_head,
        )

    def _rollback_draft(
        self,
        payload: RollbackDraftRequestV1,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        governance = self._require_governance()
        resolver = self._require_scope_resolver()
        if self._catalog is None:
            raise DependencyUnavailable(
                "rollback profile catalog is unavailable",
                component="workflow_execution_profiles",
            )
        ref_name = server.resource_id
        with self._read_scope() as read:
            # Load the immutable parents named by the request here; the mutable
            # current-ref and history membership are re-read only after the draft
            # idempotency check, inside the publication UoW.
            current_artifact = _require_artifact(
                read,
                payload.expected_current_ref.artifact_id,
            )
            target_artifact = _require_artifact(read, payload.target_artifact_id)
            profile_binding = read.policies.resolve_execution_profile(
                catalog_version=self._catalog.catalog_version,
                catalog_digest=self._catalog.catalog_digest,
                field_path="/params/rollback_profile",
                profile=payload.rollback_profile,
                expected_profile_kind="rollback",
            )
            request = RollbackRequestV1(
                ref_name=ref_name,
                expected_current_ref=payload.expected_current_ref,
                target_artifact_id=payload.target_artifact_id,
                target_history_revision=payload.target_history_revision,
                rollback_profile_binding=profile_binding,
                reason=payload.reason,
                reverses_approval_id=payload.reverses_approval_id,
            )
            scope = resolver.resolve_rollback_scope(
                target_artifact=target_artifact, request=request
            )
        created_at = _utc_text(self._clock)
        stored = self._objects.put_verified(
            canonical_json(request.model_dump(mode="json")).encode("utf-8")
        )
        artifact = build_artifact_v2(
            kind="rollback_request",
            version_tuple=target_artifact.version_tuple.model_copy(
                update={"tool_version": _ROLLBACK_TOOL_VERSION}
            ),
            lineage=(current_artifact.artifact_id, target_artifact.artifact_id),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": _ROLLBACK_REQUEST_SCHEMA_ID,
                "domain_scope": scope.model_dump(mode="json"),
            },
            created_at=created_at,
        )
        binding = RollbackTargetBindingV1(
            target_artifact_kind=target_artifact.kind,
            target_artifact_id=target_artifact.artifact_id,
            target_snapshot_id=workflow_target_snapshot_id(target_artifact),
            target_digest=target_artifact.payload_hash,
            ref_name=ref_name,
            expected_ref=payload.expected_current_ref,
            rollback_profile_binding=request.rollback_profile_binding,
        )
        item = self._new_draft_item(
            governance=governance,
            subject_kind="rollback_request",
            subject_artifact=artifact,
            subject_revision=1,
            domain_scope=scope,
            target_binding=binding,
            proposer=self._proposer(server),
            created_at=created_at,
        )
        prepared = PreparedDraft(
            subject_artifact=artifact,
            companion_artifacts=(),
            object_bindings=(
                PreparedObjectBinding(
                    object_ref=stored.ref,
                    location=stored.location,
                    expected_revision=None,
                ),
            ),
            approval_item=item,
            expected_subject_head=None,
        )
        view = RollbackRequestReadViewV1(
            artifact=_artifact_summary(
                artifact,
                domain_scope=scope,
                payload_schema_id="rollback-request@1",
            ),
            request=request,
            workflow_revision=item.workflow_revision,
            approval_status=item.status,
        )
        return self._publish_assembled(
            _AssembledDraft(prepared=prepared, view=view),
            server,
            expected_subject_head=None,
        )

    def _new_draft_item(
        self,
        *,
        governance: WorkflowGovernance,
        subject_kind: str,
        subject_artifact: ArtifactV2,
        subject_revision: int,
        domain_scope: DomainScope,
        target_binding: Any,
        proposer: AuditActor,
        created_at: str,
        series_id: str | None = None,
        supersedes_approval_id: str | None = None,
    ) -> ApprovalItem:
        requirements = build_approval_requirements(
            registry=governance.registry,
            policy=governance.route,
            subject_kind=subject_kind,  # type: ignore[arg-type]
            domain_scope=domain_scope,
        )
        return ApprovalItem(
            approval_id=f"approval:{subject_kind}:{subject_artifact.artifact_id}",
            subject_series_id=series_id or f"series:{subject_kind}:{subject_artifact.artifact_id}",
            subject_revision=subject_revision,
            subject_kind=subject_kind,  # type: ignore[arg-type]
            subject_artifact_id=subject_artifact.artifact_id,
            subject_digest=subject_artifact.payload_hash,
            status="draft",
            workflow_revision=1,
            supersedes_approval_id=supersedes_approval_id,
            proposer=proposer,
            domain_scope=domain_scope,
            domain_registry_ref=governance.domain_registry_ref(),
            route_policy=governance.route_ref(),
            role_policy_version=governance.roles.policy_version,
            role_policy_digest=governance.roles.policy_digest,
            approval_policy=governance.approval_ref(),
            requirements=requirements,
            decisions=(),
            regression_evidence_artifact_ids=(),
            target_binding=target_binding,
            created_at=created_at,
        )

    # ── submit / decide ──────────────────────────────────────────────────────
    def _submit(
        self,
        payload: SubmitForApprovalRequestV1,
        server: WorkflowServerContext,
        *,
        subject_kind: str,
    ) -> WorkflowCommandOutcome:
        item = self._approvals.submit_for_approval(
            approval_id=payload.approval_id,
            expected_workflow_revision=payload.expected_workflow_revision,
            context=self._context(server),
            expected_subject_artifact_id=server.resource_id,
            expected_subject_kind=subject_kind,
        )
        return self._approval_outcome(item, server)

    def _decide(
        self,
        payload: ApprovalDecisionRequestV1,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        item = self._approvals.decide_current(
            approval_id=server.resource_id,
            request=ApprovalDecisionRequest(
                requirement_ids=payload.requirement_ids,
                decision=payload.decision,
                expected_workflow_revision=payload.expected_workflow_revision,
                reason_code=payload.reason_code,
                comment=payload.comment,
            ),
            context=self._context(server),
        )
        return self._approval_outcome(item, server)

    def _approval_outcome(
        self,
        item: ApprovalItem,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        view = self._project_approval(item, server.actor.principal)
        return WorkflowCommandOutcome(
            value=view,
            resource_kind="approval",
            resource_id=item.approval_id,
            revision=item.workflow_revision,
        )

    def _project_approval(self, item: ApprovalItem, actor: Principal) -> ApprovalViewV1:
        with self._read_scope() as read:
            return read.progress_projector.project(item, actor)

    # ── apply / publish / rollback apply ─────────────────────────────────────
    def _apply_op(
        self,
        payload: WorkflowApplyRequestV1,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        result = self._apply.apply(
            ApprovedApplyRequest(
                approval_id=payload.approval_id,
                expected_workflow_revision=payload.expected_workflow_revision,
                subject_artifact_id=server.resource_id,
                subject_digest=payload.subject_digest,
                target_artifact_id=payload.target_artifact_id,
                target_digest=payload.target_digest,
                ref_name=payload.ref_name,
                expected_ref=payload.expected_ref,
                context=self._context(server),
            )
        )
        item = result.approval_item
        approval_view = self._project_approval(item, server.actor.principal)
        value = WorkflowApplyResultV1(
            approval=approval_view,
            ref_name=payload.ref_name,
            ref_value=result.ref_value,
            ref_transition_id=(
                None if result.ref_transition is None else result.ref_transition.transition_id
            ),
            reversed_approval_id=(
                None
                if result.reversed_approval_item is None
                else result.reversed_approval_item.approval_id
            ),
        )
        return WorkflowCommandOutcome(
            value=value,
            resource_kind="approval",
            resource_id=item.approval_id,
            revision=item.workflow_revision,
        )

    # ── rebase / resolve ─────────────────────────────────────────────────────
    def _rebase_op(
        self,
        payload: PatchRebaseRequestV1,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        context = self._context(server)
        replay = self._rebase.replay_command(
            context=context,
            resolve_conflicts=False,
        )
        if replay is not None:
            return self._rebase_outcome(replay, server)
        material, prepared = self._assemble_rebase(payload, server)
        result = self._rebase.rebase(
            material=material,
            prepared_draft=prepared,
            context=context,
        )
        return self._rebase_outcome(result, server)

    def _resolve_op(
        self,
        payload: ResolveConflictsRequestV1,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        context = self._context(server)
        replay = self._rebase.replay_command(
            context=context,
            resolve_conflicts=True,
        )
        if replay is not None:
            return self._rebase_outcome(replay, server)
        material, prepared = self._assemble_rebase(payload, server, require_clean=True)
        assert prepared is not None
        result = self._rebase.resolve_conflicts(
            material=material,
            conflict_set_id=payload.conflict_set_id,
            resolutions=payload.resolutions,
            prepared_draft=prepared,
            context=context,
        )
        return self._rebase_outcome(result, server)

    def _rebase_outcome(
        self,
        result: RebaseResult,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        if result.status == "clean":
            assert result.new_patch_artifact_id is not None
            return WorkflowCommandOutcome(
                value=result,
                resource_kind="patch",
                resource_id=result.new_patch_artifact_id,
                revision=1,
            )
        assert result.conflict_set_id is not None
        return WorkflowCommandOutcome(
            value=result,
            resource_kind="conflict_set",
            resource_id=result.conflict_set_id,
            revision=1,
        )

    def _assemble_rebase(
        self,
        payload: PatchRebaseRequestV1,
        server: WorkflowServerContext,
        *,
        require_clean: bool = False,
    ) -> tuple[RebaseMaterial, PreparedDraft | None]:
        governance = self._require_governance()
        with self._read_scope() as read:
            source_item = read.approvals.get(payload.approval_id)
            if not isinstance(source_item, ApprovalItem) or source_item.subject_kind != "patch":
                raise Conflict("rebase source is not a patch ApprovalItem")
            if source_item.subject_artifact_id != server.resource_id:
                raise Conflict("rebase path does not bind the source Patch Artifact")
            if source_item.workflow_revision != payload.expected_workflow_revision:
                raise Conflict(
                    "rebase source workflow revision is stale",
                    expected_workflow_revision=payload.expected_workflow_revision,
                    actual_workflow_revision=source_item.workflow_revision,
                )
            binding = source_item.target_binding
            if not isinstance(binding, PatchTargetBindingV1) or binding.expected_ref is None:
                raise Conflict("rebase source patch has no base ref binding")
            head = read.approvals.get_subject_head(source_item.subject_series_id)
            if head is None:
                raise Conflict("rebase source has no current head")
            if head.revision != payload.expected_subject_head_revision:
                raise Conflict(
                    "rebase subject head revision is stale",
                    expected_subject_head_revision=payload.expected_subject_head_revision,
                    actual_subject_head_revision=head.revision,
                )
            source_patch_artifact = _require_artifact(read, source_item.subject_artifact_id)
            source_patch = read.readers.load_patch(source_patch_artifact)
            base_artifact = _require_artifact(read, binding.expected_ref.artifact_id)
            proposed_artifact = _require_artifact(read, binding.target_artifact_id)
            current_ref = payload.expected_ref
            current_artifact = _require_artifact(read, current_ref.artifact_id)
            base_snapshot = read.readers.load_snapshot(base_artifact)
            current_snapshot = read.readers.load_snapshot(current_artifact)
            proposed_snapshot = read.readers.load_snapshot(proposed_artifact)
        material = RebaseMaterial(
            source_item=source_item,
            source_head=head,
            source_patch_artifact=source_patch_artifact,
            source_patch=source_patch,
            base_artifact=base_artifact,
            base_snapshot=base_snapshot,
            current_artifact=current_artifact,
            current_snapshot=current_snapshot,
            proposed_artifact=proposed_artifact,
            proposed_snapshot=proposed_snapshot,
            ref_name=payload.ref_name,
            expected_ref=payload.expected_ref,
            merge_policy=_WORKFLOW_MERGE_POLICY,
        )
        plan = compute_three_way_merge(
            base_snapshot.content_payload,
            current_snapshot.content_payload,
            proposed_snapshot.content_payload,
            _WORKFLOW_MERGE_POLICY,
        )
        if plan.conflicts and not require_clean:
            return material, None
        compiled = compile_rebased_patch(
            source_patch_artifact_id=source_patch_artifact.artifact_id,
            source_patch=source_patch,
            current=current_snapshot,
            resolved_view=(_resolve_view(material, payload) if require_clean else plan.merged),
        )
        prepared = self._assemble_rebased_draft(
            material=material,
            compiled=compiled,
            governance=governance,
            server=server,
        )
        return material, prepared

    def _assemble_rebased_draft(
        self,
        *,
        material: RebaseMaterial,
        compiled: Any,
        governance: WorkflowGovernance,
        server: WorkflowServerContext,
    ) -> PreparedDraft:
        created_at = _utc_text(self._clock)
        patch = compiled.patch
        preview = compiled.preview
        patch_stored = self._objects.put_verified(
            canonical_json(patch.model_dump(mode="json")).encode("utf-8")
        )
        patch_artifact = build_artifact_v2(
            kind="patch",
            version_tuple=VersionTuple(
                ir_snapshot_id=material.current_snapshot.snapshot_id,
                tool_version=REBASE_TOOL_VERSION,
            ),
            lineage=(
                material.source_patch_artifact.artifact_id,
                material.current_artifact.artifact_id,
            ),
            payload_hash=patch_stored.ref.sha256,
            object_ref=patch_stored.ref,
            meta={
                "payload_schema_id": _PATCH_SCHEMA_ID,
                "domain_scope": material.source_item.domain_scope.model_dump(mode="json"),
            },
            created_at=created_at,
        )
        preview_stored = self._objects.put_verified(
            canonical_json(preview.content_payload).encode("utf-8")
        )
        preview_artifact = build_artifact_v2(
            kind="ir_snapshot",
            version_tuple=VersionTuple(
                ir_snapshot_id=preview.snapshot_id,
                tool_version=REBASE_TOOL_VERSION,
            ),
            lineage=(material.current_artifact.artifact_id, patch_artifact.artifact_id),
            payload_hash=preview_stored.ref.sha256,
            object_ref=preview_stored.ref,
            meta={
                "payload_schema_id": _IR_SNAPSHOT_SCHEMA_ID,
                "domain_scope": material.source_item.domain_scope.model_dump(mode="json"),
            },
            created_at=created_at,
        )
        binding = PatchTargetBindingV1(
            target_artifact_id=preview_artifact.artifact_id,
            target_snapshot_id=preview.snapshot_id,
            target_digest=preview_artifact.payload_hash,
            ref_name=material.ref_name,
            expected_ref=material.expected_ref,
        )
        item = self._new_draft_item(
            governance=governance,
            subject_kind="patch",
            subject_artifact=patch_artifact,
            subject_revision=material.source_item.subject_revision + 1,
            domain_scope=material.source_item.domain_scope,
            target_binding=binding,
            proposer=self._proposer(server),
            created_at=created_at,
            series_id=material.source_item.subject_series_id,
            supersedes_approval_id=material.source_item.approval_id,
        )
        return PreparedDraft(
            subject_artifact=patch_artifact,
            companion_artifacts=(preview_artifact,),
            object_bindings=(
                PreparedObjectBinding(
                    object_ref=patch_stored.ref,
                    location=patch_stored.location,
                    expected_revision=None,
                ),
                PreparedObjectBinding(
                    object_ref=preview_stored.ref,
                    location=preview_stored.location,
                    expected_revision=None,
                ),
            ),
            approval_item=item,
            expected_subject_head=material.source_head,
            expected_previous_workflow_revision=material.source_item.workflow_revision,
        )

    # ── validate (Task 8 admission) ──────────────────────────────────────────
    def _validate(
        self,
        operation: str,
        payload: Any,
        server: WorkflowServerContext,
    ) -> WorkflowCommandOutcome:
        if self._admission is None:
            raise DependencyUnavailable(
                "Run admission is unavailable",
                component="run_admission",
            )
        resource_kind = {
            "patch.validate": "patch",
            "constraint.validate": "constraint_proposal",
            "rollback.validate": "rollback_request",
        }.get(operation)
        if resource_kind is None:
            raise IntegrityViolation("unknown validation operation", operation=operation)
        expected_etag = compute_resource_etag(
            resource_kind=resource_kind,
            resource_id=server.resource_id,
            revision=payload.expected_workflow_revision,
        )
        if server.if_match != expected_etag:
            raise Conflict(
                "If-Match does not bind the exact validation subject revision",
                resource_kind=resource_kind,
                resource_id=server.resource_id,
                revision=payload.expected_workflow_revision,
            )
        accepted = self._admission.admit(
            operation=operation,
            resource_id=server.resource_id,
            request=payload,
            actor=server.actor,
            server=server,
        )
        if not isinstance(accepted, RunAcceptedV1):
            raise IntegrityViolation("admission port returned a non-RunAccepted value")
        return WorkflowCommandOutcome(
            value=accepted,
            resource_kind="run",
            resource_id=accepted.run_id,
            revision=1,
        )


def _reproject_view_created_at(
    view: WorkflowCommandResponseV1,
    created_at: str | None,
) -> WorkflowCommandResponseV1:
    """Restamp a draft view's Artifact summary with the committed creation time.

    Each draft read view carries an :class:`ArtifactSummaryV1` whose ``created_at``
    is the server-owned draft creation time. On idempotent replay the response must
    reproduce the FIRST creation's bytes, so the re-assembled view's timestamp is
    replaced with the retained committed value.
    """

    artifact = getattr(view, "artifact", None)
    if not isinstance(artifact, ArtifactSummaryV1):
        raise IntegrityViolation("draft view is missing its Artifact summary")
    return view.model_copy(
        update={"artifact": artifact.model_copy(update={"created_at": created_at})}
    )


def _resolve_view(material: RebaseMaterial, payload: ResolveConflictsRequestV1) -> Any:
    from gameforge.platform.diff.three_way import resolve_three_way_merge

    return resolve_three_way_merge(
        material.base_snapshot.content_payload,
        material.current_snapshot.content_payload,
        material.proposed_snapshot.content_payload,
        material.merge_policy,
        payload.resolutions,
    )


_HANDLERS: Mapping[
    str, Callable[[WorkflowCommandService, Any, WorkflowServerContext], WorkflowCommandOutcome]
] = {
    "spec.upload": lambda s, p, c: s._spec_upload(p, c),
    "patch.draft": lambda s, p, c: s._patch_draft(p, c),
    "constraint.draft": lambda s, p, c: s._constraint_draft(p, c),
    "constraint.revise": lambda s, p, c: s._constraint_revise(p, c),
    "rollback.draft": lambda s, p, c: s._rollback_draft(p, c),
    "patch.submit": lambda s, p, c: s._submit(p, c, subject_kind="patch"),
    "constraint.submit": lambda s, p, c: s._submit(p, c, subject_kind="constraint_proposal"),
    "rollback.submit": lambda s, p, c: s._submit(p, c, subject_kind="rollback_request"),
    "approval.approve": lambda s, p, c: s._decide(p, c),
    "approval.reject": lambda s, p, c: s._decide(p, c),
    "approval.request_changes": lambda s, p, c: s._decide(p, c),
    "patch.apply": lambda s, p, c: s._apply_op(p, c),
    "constraint.publish": lambda s, p, c: s._apply_op(p, c),
    "rollback.apply": lambda s, p, c: s._apply_op(p, c),
    "patch.rebase": lambda s, p, c: s._rebase_op(p, c),
    "patch.resolve_conflicts": lambda s, p, c: s._resolve_op(p, c),
    "patch.validate": lambda s, p, c: s._validate("patch.validate", p, c),
    "constraint.validate": lambda s, p, c: s._validate("constraint.validate", p, c),
    "rollback.validate": lambda s, p, c: s._validate("rollback.validate", p, c),
}


__all__ = [
    "ValidationAdmissionPort",
    "WorkflowCommandOutcome",
    "WorkflowCommandService",
    "WorkflowGovernance",
    "WorkflowGovernanceProvider",
    "WorkflowReadPort",
    "WorkflowReadScope",
    "WorkflowScopeResolver",
    "WorkflowServerContext",
]
