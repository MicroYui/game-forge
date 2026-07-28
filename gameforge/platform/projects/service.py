"""Authoritative project and planning-material application service.

The service persists only the human-facing project mapping.  Project content,
source bytes, lineage, refs, authorization and audit remain owned by the existing
GameForge authorities.  Object bytes are staged before the database transaction;
failed transactions therefore leave only content-addressed GC candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Literal

from pydantic import ValidationError

from gameforge.contracts.api import (
    ExecutionOptionResolveRequestV1,
    HumanPatchDraftRequestV1,
    ProspectiveGenerationProposeRequestV1,
    RunAcceptedV1,
)
from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import (
    Conflict,
    DependencyUnavailable,
    Forbidden,
    IntegrityViolation,
    NotFound,
    PayloadTooLarge,
    RequestSchemaInvalid,
)
from gameforge.contracts.identity import (
    ActorContext,
    DomainRegistryV1,
    DomainScope,
    Permission,
    Principal,
    RolePolicy,
)
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.jobs import (
    GenerationProposePayloadV1,
    RefReadBindingV1,
    RunFailureV1,
    RunRecord,
    RunResultV1,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    AuditCorrelation,
    AuditSubject,
    VersionTuple,
    build_artifact_v2,
)
from gameforge.contracts.projects import (
    MAX_PROJECT_IDENTITY_ALIASES,
    MAX_PROJECT_MATERIAL_BYTES,
    MAX_PROJECT_MATERIAL_TEXT_CHARS,
    GameProjectV1,
    IdentityAliasGroupV1,
    IdentityConflictV1,
    IdentityNormalizationSummaryV1,
    MaterialSourceFormat,
    ProjectArchiveRequestV1,
    ProjectCreateRequestV1,
    ProjectExtractionCreateRequestV1,
    ProjectExtractionDiscardRequestV1,
    ProjectExtractionIssueV1,
    ProjectExtractionPageV1,
    ProjectExtractionStatus,
    ProjectExtractionV1,
    ProjectGraphDraftRequestV1,
    ProjectIdentityAliasDeclareRequestV1,
    ProjectIdentityAliasPageV1,
    ProjectIdentityAliasRetractRequestV1,
    ProjectIdentityAliasV1,
    ProjectMaterialPageV1,
    ProjectMaterialRenameRequestV1,
    ProjectMaterialTextRequestV1,
    ProjectMaterialV1,
    ProjectPageV1,
    ProjectUpdateRequestV1,
)
from gameforge.contracts.provenance import (
    OriginRefV1,
    ProvenanceTransformationV1,
    ProvenanceV1,
)
from gameforge.contracts.storage import ObjectStore, UtcClock
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.provenance.registry import (
    PLANNING_DOCUMENT,
    TOOL_OUTPUT,
    build_source_kind_registry,
)
from gameforge.platform.publication.payload_schema import (
    decode_and_validate_artifact_payload,
)
from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.platform.projects.graph_draft import compile_project_graph_draft
from gameforge.platform.rbac.authorization import AuthorizationDecision, authorize
from gameforge.platform.runs.admission import AdmissionRequestContext
from gameforge.spine.ingestion.planning_materials import (
    MaterialParseError,
    ParsedPlanningMaterial,
    parse_planning_material,
)
from gameforge.spine.identity_normalization import (
    canonical_identity_reference,
    canonical_identity_token,
)
from gameforge.spine.ir.snapshot import Snapshot


_PROJECT_BOOTSTRAP_TOOL = "project-bootstrap@1"
_PROJECT_MATERIAL_CONNECTOR = "project-material-upload@1"
_PROJECT_MATERIAL_CONNECTOR_VERSION = "1"
_IR_SCHEMA_REGISTRY_VERSION = "registry@1"


@dataclass(frozen=True, slots=True)
class ProjectCommandContext:
    actor: ActorContext
    idempotency_key: str
    request_hash: str
    request_id: str
    trace_id: str | None = None
    if_match: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("idempotency_key", "request_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ValueError(f"{field_name} must be a non-empty bounded string")
        if (
            not isinstance(self.request_hash, str)
            or len(self.request_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.request_hash)
        ):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ProjectContentDraftPreparation:
    workflow_request: HumanPatchDraftRequestV1
    alias_groups: tuple[IdentityAliasGroupV1, ...]
    normalization_summary: IdentityNormalizationSummaryV1
    expected_project_revision: int
    source_extraction_id: str
    expected_source_extraction_revision: int


def _required(value: Any, name: str) -> Any:
    if value is None:
        raise DependencyUnavailable(
            "project authoring authority is unavailable",
            component=name,
        )
    return value


def _utc_text(clock: UtcClock) -> str:
    now = clock.now_utc()
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("project authoring clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _actor(value: ActorContext) -> AuditActor:
    return AuditActor(
        principal_id=value.principal.id,
        principal_kind=value.principal.kind,
    )


def _deterministic_id(
    kind: Literal["project", "material", "extraction"],
    *,
    principal_id: str,
    idempotency_key: str,
    request_hash: str,
) -> str:
    digest = canonical_sha256(
        {
            "identity_schema_version": f"{kind}-request-identity@1",
            "principal_id": principal_id,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
        }
    )
    return f"{kind}:{digest[:32]}"


def _bind_project_artifacts(
    transaction: object,
    *,
    project_id: str,
    artifact_ids: tuple[str, ...],
    bound_by: str,
    bound_at: str,
) -> None:
    """Record which game these Artifacts belong to, in the same UoW that published them.

    Without this a freshly created game has content but no membership, so selecting it
    shows an empty workspace — worse than showing everything.
    """

    _required(transaction.project_artifacts, "project_artifacts").bind(
        project_id, artifact_ids, bound_by=bound_by, bound_at=bound_at
    )


class ProjectAuthoringService:
    """Create/read/archive project mappings and governed planning materials."""

    def __init__(
        self,
        *,
        unit_of_work: Any,
        object_store: ObjectStore,
        clock: UtcClock,
        role_policy_version: str,
        role_policy_digest: str,
        audit_chain_id: str,
        run_admission: Any | None = None,
        default_generation_policy: ProfileRefV1 = ProfileRefV1(
            profile_id="builtin.generation",
            version=3,
        ),
    ) -> None:
        for value in (role_policy_version, role_policy_digest, audit_chain_id):
            if not isinstance(value, str) or not value:
                raise ValueError("project authoring authority identifiers must be non-empty")
        self._unit_of_work = unit_of_work
        self._objects = object_store
        self._clock = clock
        self._role_policy_version = role_policy_version
        self._role_policy_digest = role_policy_digest
        self._audit_chain_id = audit_chain_id
        self._run_admission = run_admission
        self._default_generation_policy = default_generation_policy
        self._source_registry = build_source_kind_registry()

    # ── projects ──────────────────────────────────────────────────────────
    def create_project(
        self,
        request: ProjectCreateRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> GameProjectV1:
        project_id = _deterministic_id(
            "project",
            principal_id=context.actor.principal.id,
            idempotency_key=context.idempotency_key,
            request_hash=context.request_hash,
        )
        created_at = _utc_text(self._clock)
        snapshot = Snapshot(entities={}, relations={})
        stored = self._objects.put_verified(
            canonical_json(snapshot.content_payload).encode("utf-8")
        )
        bootstrap = build_artifact_v2(
            kind="ir_snapshot",
            version_tuple=VersionTuple(
                doc_version=f"{project_id}@bootstrap",
                ir_snapshot_id=snapshot.snapshot_id,
                tool_version=_PROJECT_BOOTSTRAP_TOOL,
            ),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": "ir-core@1",
                "schema_registry_version": _IR_SCHEMA_REGISTRY_VERSION,
                "meta_schema_version": snapshot.meta_schema_version,
                "domain_scope": request.domain_scope.model_dump(mode="json"),
                "project_id": project_id,
                "project_bootstrap": True,
            },
            created_at=created_at,
        )
        candidate = GameProjectV1(
            project_id=project_id,
            project_key=request.project_key,
            display_name=request.display_name,
            description=request.description,
            genre=request.genre,
            status="draft",
            domain_scope=request.domain_scope,
            bootstrap_snapshot_artifact_id=bootstrap.artifact_id,
            content_ref_name=f"projects/{project_id}/content/head",
            constraint_ref_name=f"projects/{project_id}/constraints/head",
            created_by=context.actor.principal.id,
            created_at=created_at,
            updated_at=created_at,
            revision=1,
        )
        with self._unit_of_work.begin() as transaction:
            projects = _required(transaction.projects, "projects")
            idempotency = _required(transaction.idempotency, "idempotency")
            self._authorize(
                transaction,
                actor=context.actor,
                action="create",
                resource_kind="project",
                scope=request.domain_scope,
                direct_human=True,
            )
            replay = idempotency.get_result(
                scope=self._idempotency_scope(context.actor),
                operation="project.create",
                key=context.idempotency_key,
                request_hash=context.request_hash,
            )
            if replay is not None:
                return self._replay_project(replay, expected_project_id=project_id)

            bindings = _required(transaction.object_bindings, "object_bindings")
            artifacts = _required(transaction.artifacts, "artifacts")
            binding = bindings.bind_verified(stored.ref, stored.location, None)
            if binding.object_ref != stored.ref or binding.status != "active":
                raise IntegrityViolation("bootstrap ObjectBinding changed during publication")
            if artifacts.put(bootstrap) != bootstrap:
                raise IntegrityViolation("bootstrap Artifact publisher returned another Artifact")
            created = projects.create_project(candidate)
            _bind_project_artifacts(
                transaction,
                project_id=created.project_id,
                artifact_ids=(bootstrap.artifact_id,),
                bound_by="command:project.create",
                bound_at=created.created_at,
            )
            self._append_audit(
                transaction,
                context=context,
                action="project.created",
                subject=AuditSubject(
                    resource_kind="project",
                    resource_id=created.project_id,
                    artifact_id=bootstrap.artifact_id,
                ),
            )
            stored_result = idempotency.put_result(
                scope=self._idempotency_scope(context.actor),
                operation="project.create",
                key=context.idempotency_key,
                request_hash=context.request_hash,
                resource_kind="project",
                resource_id=created.project_id,
                response=created.model_dump(mode="json"),
            )
            return self._replay_project(stored_result, expected_project_id=project_id)

    def get_project(self, project_id: str, *, actor: ActorContext) -> GameProjectV1:
        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=actor,
                action="read",
                resource_kind="project",
                scope=project.domain_scope,
            )
            return self._project_authority_projection(transaction, project)

    def list_projects(
        self,
        *,
        actor: ActorContext,
        limit: int = 100,
        status: Literal["draft", "active", "archived"] | None = None,
    ) -> ProjectPageV1:
        with self._unit_of_work.begin() as transaction:
            projects = _required(transaction.projects, "projects")
            # ``active`` is ref-derived, not a mutable table label. Project first,
            # then filter, otherwise a newly published row retained as ``draft``
            # would disappear from the active view.
            retained = projects.list_projects(limit=1000, status=None)
            visible: list[GameProjectV1] = []
            for project in retained:
                if self._allowed(
                    transaction,
                    actor=actor,
                    action="read",
                    resource_kind="project",
                    scope=project.domain_scope,
                ):
                    projected = self._project_authority_projection(transaction, project)
                    if status is None or projected.status == status:
                        visible.append(projected)
                    if len(visible) == limit:
                        break
            return ProjectPageV1(items=tuple(visible))

    def update_project(
        self,
        project_id: str,
        request: ProjectUpdateRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> GameProjectV1:
        with self._unit_of_work.begin() as transaction:
            projects = _required(transaction.projects, "projects")
            current = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="update",
                resource_kind="project",
                scope=current.domain_scope,
                direct_human=True,
            )
            self._authorize(
                transaction,
                actor=context.actor,
                action="update",
                resource_kind="project",
                scope=request.domain_scope,
                direct_human=True,
            )
            self._require_precondition(
                resource_kind="project",
                resource_id=project_id,
                revision=current.revision,
                expected_revision=request.expected_revision,
                if_match=context.if_match,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.update",
            )
            if replay is not None:
                return self._replay_project(replay, expected_project_id=project_id)
            if current.status == "archived":
                raise Conflict("archived project cannot be updated", project_id=project_id)
            if (
                request.domain_scope != current.domain_scope
                and transaction.refs.get(current.content_ref_name) is not None
            ):
                raise Conflict("published project domain scope cannot be rewritten")
            now = _utc_text(self._clock)
            replacement = GameProjectV1.model_validate(
                {
                    **current.model_dump(mode="json"),
                    "display_name": request.display_name,
                    "description": request.description,
                    "genre": request.genre,
                    "domain_scope": request.domain_scope.model_dump(mode="json"),
                    "updated_at": now,
                    "revision": current.revision + 1,
                }
            )
            updated = projects.compare_and_set_project(
                project_id,
                current.revision,
                replacement,
            )
            self._append_audit(
                transaction,
                context=context,
                action="project.updated",
                subject=AuditSubject(resource_kind="project", resource_id=project_id),
            )
            return self._store_project_result(
                transaction,
                context=context,
                operation="project.update",
                project=updated,
            )

    def archive_project(
        self,
        project_id: str,
        request: ProjectArchiveRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> GameProjectV1:
        with self._unit_of_work.begin() as transaction:
            projects = _required(transaction.projects, "projects")
            current = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="archive",
                resource_kind="project",
                scope=current.domain_scope,
                direct_human=True,
            )
            self._require_precondition(
                resource_kind="project",
                resource_id=project_id,
                revision=current.revision,
                expected_revision=request.expected_revision,
                if_match=context.if_match,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.archive",
            )
            if replay is not None:
                return self._replay_project(replay, expected_project_id=project_id)
            if current.status == "archived":
                raise Conflict("project is already archived", project_id=project_id)
            projected = self._project_authority_projection(transaction, current)
            replacement = GameProjectV1.model_validate(
                {
                    **projected.model_dump(mode="json"),
                    "status": "archived",
                    "updated_at": _utc_text(self._clock),
                    "revision": current.revision + 1,
                }
            )
            archived = projects.compare_and_set_project(
                project_id,
                current.revision,
                replacement,
            )
            self._append_audit(
                transaction,
                context=context,
                action="project.archived",
                subject=AuditSubject(resource_kind="project", resource_id=project_id),
            )
            return self._store_project_result(
                transaction,
                context=context,
                operation="project.archive",
                project=archived,
            )

    # ── materials ─────────────────────────────────────────────────────────
    def add_text_material(
        self,
        project_id: str,
        request: ProjectMaterialTextRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> ProjectMaterialV1:
        media_type = {
            "plain_text": "text/plain; charset=utf-8",
            "markdown": "text/markdown; charset=utf-8",
            "html": "text/html; charset=utf-8",
            "feishu_blocks_json": "application/json",
        }[request.source_format]
        return self.add_uploaded_material(
            project_id,
            payload=request.text.encode("utf-8"),
            display_name=request.display_name,
            media_type=media_type,
            source_format=request.source_format,
            context=context,
        )

    def add_uploaded_material(
        self,
        project_id: str,
        *,
        payload: bytes,
        display_name: str,
        media_type: str,
        source_format: MaterialSourceFormat,
        context: ProjectCommandContext,
    ) -> ProjectMaterialV1:
        if len(payload) > MAX_PROJECT_MATERIAL_BYTES:
            raise PayloadTooLarge(
                "project material exceeds the maximum upload size",
                max_bytes=MAX_PROJECT_MATERIAL_BYTES,
            )
        if not display_name or display_name != display_name.strip() or len(display_name) > 256:
            raise RequestSchemaInvalid("material display name is invalid")
        if (
            not media_type
            or media_type != media_type.strip()
            or len(media_type) > 256
            or any(ord(character) < 0x20 for character in media_type)
        ):
            raise RequestSchemaInvalid("material media type is invalid")
        try:
            parsed = parse_planning_material(payload, source_format)
        except MaterialParseError as exc:
            raise RequestSchemaInvalid(str(exc)) from exc
        if len(parsed.text) > MAX_PROJECT_MATERIAL_TEXT_CHARS:
            raise PayloadTooLarge(
                "rendered project material exceeds the maximum text size",
                max_characters=MAX_PROJECT_MATERIAL_TEXT_CHARS,
            )
        material_id = _deterministic_id(
            "material",
            principal_id=context.actor.principal.id,
            idempotency_key=context.idempotency_key,
            request_hash=context.request_hash,
        )
        created_at = _utc_text(self._clock)
        # First resolve current project authority without holding a write
        # transaction while object bytes are staged.
        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="create",
                resource_kind="material",
                scope=project.domain_scope,
                direct_human=True,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.material.create",
            )
            if replay is not None:
                return self._replay_material(replay, expected_material_id=material_id)
            if project.status == "archived":
                raise Conflict("archived project cannot accept materials", project_id=project_id)
            material_scope = project.domain_scope
        raw_stored = self._objects.put_verified(payload)
        rendered_bytes = parsed.text.encode("utf-8")
        rendered_stored = self._objects.put_verified(rendered_bytes)
        raw_artifact, rendered_artifact = self._material_artifacts(
            project_id=project_id,
            material_id=material_id,
            domain_scope=material_scope,
            parsed=parsed,
            raw_stored=raw_stored,
            rendered_stored=rendered_stored,
            created_at=created_at,
        )
        with self._unit_of_work.begin() as transaction:
            projects = _required(transaction.projects, "projects")
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="create",
                resource_kind="material",
                scope=project.domain_scope,
                direct_human=True,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.material.create",
            )
            if replay is not None:
                return self._replay_material(replay, expected_material_id=material_id)
            if project.status == "archived":
                raise Conflict("archived project cannot accept materials", project_id=project_id)
            if project.domain_scope != material_scope:
                raise Conflict("project domain scope changed while material was staged")

            bindings = _required(transaction.object_bindings, "object_bindings")
            artifacts = _required(transaction.artifacts, "artifacts")
            for stored, artifact in (
                (raw_stored, raw_artifact),
                (rendered_stored, rendered_artifact),
            ):
                binding = bindings.bind_verified(stored.ref, stored.location, None)
                if binding.object_ref != stored.ref or binding.status != "active":
                    raise IntegrityViolation("material ObjectBinding changed during publication")
                if artifacts.put(artifact) != artifact:
                    raise IntegrityViolation(
                        "material Artifact publisher returned another Artifact"
                    )
            material = ProjectMaterialV1(
                material_id=material_id,
                project_id=project_id,
                display_name=display_name,
                media_type=media_type,
                source_format=source_format,
                original_source_artifact_id=raw_artifact.artifact_id,
                rendered_source_artifact_id=rendered_artifact.artifact_id,
                parser_id=parsed.parser_id,
                parser_version=parsed.parser_version,
                parse_status="ready",
                parse_warnings=parsed.warnings,
                byte_size=len(payload),
                text_char_count=len(parsed.text),
                created_by=context.actor.principal.id,
                created_at=created_at,
                status="active",
                revision=1,
            )
            created = projects.create_material(material)
            _bind_project_artifacts(
                transaction,
                project_id=project_id,
                artifact_ids=(
                    created.original_source_artifact_id,
                    created.rendered_source_artifact_id,
                ),
                bound_by="command:project.material",
                bound_at=created_at,
            )
            # Material changes are visible on the project card and therefore advance
            # the project mapping revision without changing content authority.
            replacement = project.model_copy(
                update={
                    "updated_at": created_at,
                    "revision": project.revision + 1,
                }
            )
            projects.compare_and_set_project(project_id, project.revision, replacement)
            self._append_audit(
                transaction,
                context=context,
                action="project.material.created",
                subject=AuditSubject(
                    resource_kind="project_material",
                    resource_id=created.material_id,
                    artifact_id=rendered_artifact.artifact_id,
                ),
            )
            idempotency = _required(transaction.idempotency, "idempotency")
            stored_result = idempotency.put_result(
                scope=self._idempotency_scope(context.actor),
                operation="project.material.create",
                key=context.idempotency_key,
                request_hash=context.request_hash,
                resource_kind="project_material",
                resource_id=created.material_id,
                response=created.model_dump(mode="json"),
            )
            return self._replay_material(
                stored_result,
                expected_material_id=material_id,
            )

    def get_material(
        self,
        project_id: str,
        material_id: str,
        *,
        actor: ActorContext,
    ) -> ProjectMaterialV1:
        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=actor,
                action="read",
                resource_kind="material",
                scope=project.domain_scope,
            )
            material = _required(transaction.projects, "projects").get_material(material_id)
            if material is None or material.project_id != project_id:
                raise NotFound("project material does not exist", material_id=material_id)
            return material

    def list_materials(
        self,
        project_id: str,
        *,
        actor: ActorContext,
        limit: int = 100,
        status: Literal["active", "archived"] | None = None,
    ) -> ProjectMaterialPageV1:
        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=actor,
                action="read",
                resource_kind="material",
                scope=project.domain_scope,
            )
            items = _required(transaction.projects, "projects").list_materials(
                project_id=project_id,
                limit=limit,
                status=status,
            )
            return ProjectMaterialPageV1(items=items)

    def rename_material(
        self,
        project_id: str,
        material_id: str,
        request: ProjectMaterialRenameRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> ProjectMaterialV1:
        """Rename retained material without touching its Artifacts."""

        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="create",
                resource_kind="material",
                scope=project.domain_scope,
                direct_human=True,
            )
            projects = _required(transaction.projects, "projects")
            current = projects.get_material(material_id)
            if current is None or current.project_id != project_id:
                raise NotFound("project material does not exist", material_id=material_id)
            # A retry of the SAME request replays its result; the revision it moved
            # must not make the retry look like a stale write.
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.material.rename",
            )
            if replay is not None:
                return self._replay_material(replay, expected_material_id=material_id)
            self._require_precondition(
                resource_kind="project_material",
                resource_id=material_id,
                revision=current.revision,
                expected_revision=request.expected_revision,
                if_match=context.if_match,
            )
            renamed = current.model_copy(
                update={
                    "display_name": request.display_name,
                    "revision": current.revision + 1,
                }
            )
            result = projects.compare_and_set_material(
                material_id,
                current.revision,
                renamed,
            )
            self._append_audit(
                transaction,
                context=context,
                action="project.material.renamed",
                subject=AuditSubject(
                    resource_kind="project_material",
                    resource_id=material_id,
                    artifact_id=current.rendered_source_artifact_id,
                ),
            )
            idempotency = _required(transaction.idempotency, "idempotency")
            stored_result = idempotency.put_result(
                scope=self._idempotency_scope(context.actor),
                operation="project.material.rename",
                key=context.idempotency_key,
                request_hash=context.request_hash,
                resource_kind="project_material",
                resource_id=material_id,
                response=result.model_dump(mode="json"),
            )
            return self._replay_material(stored_result, expected_material_id=material_id)

    def _declared_aliases(self, transaction: Any, project_id: str) -> dict[str, str]:
        """Every name this project has decided refers to an entity it has.

        Read whole rather than paged: the normalizer needs all of them at once,
        and the set is bounded by contract.
        """

        projects = _required(transaction.projects, "projects")
        return {
            alias.canonical_alias: alias.canonical_entity_id
            for alias in projects.list_identity_aliases(
                project_id=project_id,
                limit=MAX_PROJECT_IDENTITY_ALIASES,
                status="active",
            )
        }

    def _current_content_snapshot(self, transaction: Any, project: GameProjectV1) -> Snapshot:
        """The game's content as it stands, which an alias must point into."""

        current = transaction.refs.get(project.content_ref_name)
        artifact_id = (
            project.bootstrap_snapshot_artifact_id if current is None else current.artifact_id
        )
        artifact, payload = self._artifact_payload(transaction, artifact_id)
        if artifact.kind != "ir_snapshot":
            raise IntegrityViolation("project content base is not an ir_snapshot")
        return snapshot_from_canonical_view(payload)

    @staticmethod
    def _replay_identity_alias(
        response: Any,
        *,
        expected_alias_id: str | None = None,
    ) -> ProjectIdentityAliasV1:
        try:
            alias = ProjectIdentityAliasV1.model_validate(response)
        except ValidationError as exc:
            raise IntegrityViolation("identity alias idempotency response is malformed") from exc
        if expected_alias_id is not None and alias.alias_id != expected_alias_id:
            raise IntegrityViolation("identity alias idempotency response has another identity")
        return alias

    def declare_identity_alias(
        self,
        project_id: str,
        request: ProjectIdentityAliasDeclareRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> ProjectIdentityAliasV1:
        """Record that a name refers to an entity this project's content already has.

        The entity has to exist in the current content: an alias pointing at
        nothing would invent an entity the next extraction silently creates.
        """

        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="create",
                resource_kind="material",
                scope=project.domain_scope,
                direct_human=True,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.identity_alias.declare",
            )
            if replay is not None:
                return self._replay_identity_alias(replay)
            self._require_precondition(
                resource_kind="project",
                resource_id=project_id,
                revision=project.revision,
                expected_revision=request.expected_project_revision,
                if_match=context.if_match,
            )
            canonical_alias = canonical_identity_token(request.alias)
            snapshot = self._current_content_snapshot(transaction, project)
            if request.canonical_entity_id not in snapshot.entities:
                raise Conflict(
                    "identity alias names an entity this game does not have",
                    canonical_entity_id=request.canonical_entity_id,
                )
            if canonical_alias in {
                canonical_identity_reference(entity_id) for entity_id in snapshot.entities
            }:
                raise Conflict(
                    "this name already belongs to an entity of its own",
                    alias=request.alias,
                )
            projects = _required(transaction.projects, "projects")
            alias_id = f"identity-alias:{canonical_sha256({'project_id': project_id, 'canonical_alias': canonical_alias})}"
            declared = ProjectIdentityAliasV1(
                alias_id=alias_id,
                project_id=project_id,
                alias=request.alias,
                canonical_alias=canonical_alias,
                canonical_entity_id=request.canonical_entity_id,
                declared_by=context.actor.principal.id,
                declared_at=self._clock.now_utc().isoformat().replace("+00:00", "Z"),
                status="active",
                revision=1,
            )
            existing = projects.get_identity_alias(alias_id)
            result = (
                projects.compare_and_set_identity_alias(
                    alias_id,
                    existing.revision,
                    declared.model_copy(
                        update={
                            "revision": existing.revision + 1,
                            "declared_by": existing.declared_by,
                            "declared_at": existing.declared_at,
                        }
                    ),
                )
                if existing is not None
                else projects.create_identity_alias(declared)
            )
            self._append_audit(
                transaction,
                context=context,
                action="project.identity_alias.declared",
                subject=AuditSubject(
                    resource_kind="project_identity_alias",
                    resource_id=alias_id,
                    artifact_id=None,
                ),
            )
            idempotency = _required(transaction.idempotency, "idempotency")
            stored = idempotency.put_result(
                scope=self._idempotency_scope(context.actor),
                operation="project.identity_alias.declare",
                key=context.idempotency_key,
                request_hash=context.request_hash,
                resource_kind="project_identity_alias",
                resource_id=alias_id,
                response=result.model_dump(mode="json"),
            )
            return self._replay_identity_alias(stored)

    def retract_identity_alias(
        self,
        project_id: str,
        alias_id: str,
        request: ProjectIdentityAliasRetractRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> ProjectIdentityAliasV1:
        """Stop applying a declaration. The record itself is retained."""

        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="archive",
                resource_kind="material",
                scope=project.domain_scope,
                direct_human=True,
            )
            projects = _required(transaction.projects, "projects")
            current = projects.get_identity_alias(alias_id)
            if current is None or current.project_id != project_id:
                raise NotFound("identity alias does not exist", alias_id=alias_id)
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.identity_alias.retract",
            )
            if replay is not None:
                return self._replay_identity_alias(replay, expected_alias_id=alias_id)
            self._require_precondition(
                resource_kind="project_identity_alias",
                resource_id=alias_id,
                revision=current.revision,
                expected_revision=request.expected_revision,
                if_match=context.if_match,
            )
            result = projects.compare_and_set_identity_alias(
                alias_id,
                current.revision,
                current.model_copy(
                    update={"status": "retracted", "revision": current.revision + 1}
                ),
            )
            self._append_audit(
                transaction,
                context=context,
                action="project.identity_alias.retracted",
                subject=AuditSubject(
                    resource_kind="project_identity_alias",
                    resource_id=alias_id,
                    artifact_id=None,
                ),
            )
            idempotency = _required(transaction.idempotency, "idempotency")
            stored = idempotency.put_result(
                scope=self._idempotency_scope(context.actor),
                operation="project.identity_alias.retract",
                key=context.idempotency_key,
                request_hash=context.request_hash,
                resource_kind="project_identity_alias",
                resource_id=alias_id,
                response=result.model_dump(mode="json"),
            )
            return self._replay_identity_alias(stored, expected_alias_id=alias_id)

    def list_identity_aliases(
        self,
        project_id: str,
        *,
        actor: ActorContext,
        limit: int,
    ) -> ProjectIdentityAliasPageV1:
        with self._unit_of_work.begin_read() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=actor,
                action="read",
                resource_kind="material",
                scope=project.domain_scope,
            )
            projects = _required(transaction.projects, "projects")
            return ProjectIdentityAliasPageV1(
                items=projects.list_identity_aliases(project_id=project_id, limit=limit),
            )

    def archive_material(
        self,
        project_id: str,
        material_id: str,
        request: ProjectArchiveRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> ProjectMaterialV1:
        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="archive",
                resource_kind="material",
                scope=project.domain_scope,
                direct_human=True,
            )
            projects = _required(transaction.projects, "projects")
            current = projects.get_material(material_id)
            if current is None or current.project_id != project_id:
                raise NotFound("project material does not exist", material_id=material_id)
            self._require_precondition(
                resource_kind="project_material",
                resource_id=material_id,
                revision=current.revision,
                expected_revision=request.expected_revision,
                if_match=context.if_match,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.material.archive",
            )
            if replay is not None:
                return self._replay_material(replay, expected_material_id=material_id)
            if current.status == "archived":
                raise Conflict("project material is already archived", material_id=material_id)
            archived = current.model_copy(
                update={"status": "archived", "revision": current.revision + 1}
            )
            result = projects.compare_and_set_material(
                material_id,
                current.revision,
                archived,
            )
            self._append_audit(
                transaction,
                context=context,
                action="project.material.archived",
                subject=AuditSubject(
                    resource_kind="project_material",
                    resource_id=material_id,
                    artifact_id=current.rendered_source_artifact_id,
                ),
            )
            idempotency = _required(transaction.idempotency, "idempotency")
            stored_result = idempotency.put_result(
                scope=self._idempotency_scope(context.actor),
                operation="project.material.archive",
                key=context.idempotency_key,
                request_hash=context.request_hash,
                resource_kind="project_material",
                resource_id=material_id,
                response=result.model_dump(mode="json"),
            )
            return self._replay_material(stored_result, expected_material_id=material_id)

    # ── AI extraction ─────────────────────────────────────────────────────
    def create_extraction(
        self,
        project_id: str,
        request: ProjectExtractionCreateRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> ProjectExtractionV1:
        """Admit one material-grounded generation Run and retain its project map."""

        admission = self._run_admission
        if admission is None:
            raise DependencyUnavailable(
                "project extraction admission is unavailable",
                component="project_extraction_admission",
            )
        objective_goal_text = self._scoped_objective_goal(request)
        extraction_id = _deterministic_id(
            "extraction",
            principal_id=context.actor.principal.id,
            idempotency_key=context.idempotency_key,
            request_hash=context.request_hash,
        )
        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="create",
                resource_kind="extraction",
                scope=project.domain_scope,
                direct_human=True,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.extraction.create",
            )
            if replay is not None:
                retained = self._replay_extraction(
                    replay,
                    expected_extraction_id=extraction_id,
                )
                return self._extraction_authority_projection(
                    transaction,
                    project,
                    retained,
                )
            if project.status == "archived":
                raise Conflict("archived project cannot start extraction", project_id=project_id)
            projects = _required(transaction.projects, "projects")
            materials = []
            for material_id in request.material_ids:
                material = projects.get_material(material_id)
                if material is None or material.project_id != project_id:
                    raise NotFound(
                        "project extraction material does not exist",
                        material_id=material_id,
                    )
                if material.status != "active" or material.parse_status != "ready":
                    raise Conflict(
                        "project extraction material is not active and ready",
                        material_id=material_id,
                    )
                materials.append(material)
            source_artifact_ids = tuple(
                sorted(material.rendered_source_artifact_id for material in materials)
            )
            current_content = transaction.refs.get(project.content_ref_name)
            current_constraints = transaction.refs.get(project.constraint_ref_name)
            base_snapshot_artifact_id = (
                project.bootstrap_snapshot_artifact_id
                if current_content is None
                else current_content.artifact_id
            )
            constraint_snapshot_artifact_id = (
                None if current_constraints is None else current_constraints.artifact_id
            )
            if request.candidate_export_profiles and constraint_snapshot_artifact_id is None:
                raise Conflict("candidate exports require a published project constraint snapshot")
            domain_scope = project.domain_scope
            target = RefReadBindingV1(
                ref_name=project.content_ref_name,
                expected_ref=current_content,
            )
            # Read inside the transaction that read the base snapshot: the aliases
            # bound to this extraction must be the ones that pointed into exactly
            # that content.
            declared_identity_aliases = tuple(
                sorted(self._declared_aliases(transaction, project_id).items())
            )

        generation_policy = request.generation_policy or self._default_generation_policy
        execution_version_plan = request.execution_version_plan
        if execution_version_plan is None:
            if request.llm_execution_mode == "replay":
                raise RequestSchemaInvalid(
                    "project extraction replay requires an exact execution version plan"
                )
            prospective = ProspectiveGenerationProposeRequestV1(
                base_snapshot_artifact_id=base_snapshot_artifact_id,
                source_artifact_ids=source_artifact_ids,
                constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
                findings=(),
                objective_goal_text=objective_goal_text,
                domain_scope=domain_scope,
                target=target,
                generation_policy=generation_policy,
                candidate_export_profiles=request.candidate_export_profiles,
                llm_execution_mode=request.llm_execution_mode,
                execution_version_plan=None,
                cassette_artifact_id=None,
            )
            option = admission.resolve_execution_option(
                request=ExecutionOptionResolveRequestV1(
                    resource_operation_id=("propose_generation_api_v1_generation_propose_post"),
                    run_kind=RunKindRef(kind="generation.propose", version=2),
                    llm_execution_mode=request.llm_execution_mode,
                    prospective_request=prospective,
                    routing_policy_version=request.routing_policy_version,
                    routing_policy_digest=request.routing_policy_digest,
                ),
                actor=context.actor,
            )
            execution_version_plan = option.execution_version_plan

        run_request_hash = canonical_sha256(
            {
                "request_hash_schema_version": "project-extraction-run-request@1",
                "project_id": project_id,
                "base_snapshot_artifact_id": base_snapshot_artifact_id,
                "source_artifact_ids": source_artifact_ids,
                "constraint_snapshot_artifact_id": constraint_snapshot_artifact_id,
                "objective_goal_text": objective_goal_text,
                "domain_scope": domain_scope.model_dump(mode="json"),
                "target": target.model_dump(mode="json"),
                "generation_policy": generation_policy.model_dump(mode="json"),
                "candidate_export_profiles": [
                    item.model_dump(mode="json") for item in request.candidate_export_profiles
                ],
                "llm_execution_mode": request.llm_execution_mode,
                "execution_version_plan": execution_version_plan.model_dump(mode="json"),
                "cassette_artifact_id": request.cassette_artifact_id,
                "declared_identity_aliases": [list(item) for item in declared_identity_aliases],
            }
        )
        run_key = "project-extraction:" + canonical_sha256(
            {
                "key_schema_version": "project-extraction-run-key@1",
                "project_id": project_id,
                "principal_id": context.actor.principal.id,
                "external_idempotency_key": context.idempotency_key,
            }
        )
        accepted = admission.admit_generation(
            base_snapshot_artifact_id=base_snapshot_artifact_id,
            source_artifact_ids=source_artifact_ids,
            constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
            findings=(),
            objective_goal_text=objective_goal_text,
            domain_scope=domain_scope,
            target=target,
            generation_policy=generation_policy,
            candidate_export_profiles=request.candidate_export_profiles,
            actor=context.actor,
            server=AdmissionRequestContext(
                idempotency_key=run_key,
                request_hash=run_request_hash,
                request_id=context.request_id,
                trace_id=context.trace_id,
            ),
            llm_execution_mode=request.llm_execution_mode,
            execution_version_plan=execution_version_plan,
            cassette_artifact_id=request.cassette_artifact_id,
            declared_identity_aliases=declared_identity_aliases,
        )
        if not isinstance(accepted, RunAcceptedV1):
            raise IntegrityViolation("project extraction admission returned another contract")

        created_at = _utc_text(self._clock)
        candidate = ProjectExtractionV1(
            extraction_id=extraction_id,
            project_id=project_id,
            planning_scope=request.planning_scope,
            material_ids=request.material_ids,
            source_artifact_ids=source_artifact_ids,
            base_snapshot_artifact_id=base_snapshot_artifact_id,
            run_id=accepted.run_id,
            status="queued",
            created_by=context.actor.principal.id,
            created_at=created_at,
            updated_at=created_at,
            revision=1,
        )
        with self._unit_of_work.begin() as transaction:
            projects = _required(transaction.projects, "projects")
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="create",
                resource_kind="extraction",
                scope=project.domain_scope,
                direct_human=True,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.extraction.create",
            )
            if replay is not None:
                retained = self._replay_extraction(
                    replay,
                    expected_extraction_id=extraction_id,
                )
                return self._extraction_authority_projection(
                    transaction,
                    project,
                    retained,
                )
            run = _required(transaction.runs, "runs").get(accepted.run_id)
            if not isinstance(run, RunRecord):
                raise IntegrityViolation("admitted project extraction Run is unavailable")
            created = projects.create_extraction(candidate)
            replacement = project.model_copy(
                update={
                    "latest_extraction_id": created.extraction_id,
                    "updated_at": created_at,
                    "revision": project.revision + 1,
                }
            )
            projects.compare_and_set_project(project_id, project.revision, replacement)
            self._append_audit(
                transaction,
                context=context,
                action="project.extraction.created",
                subject=AuditSubject(
                    resource_kind="project_extraction",
                    resource_id=created.extraction_id,
                ),
            )
            stored = _required(transaction.idempotency, "idempotency").put_result(
                scope=self._idempotency_scope(context.actor),
                operation="project.extraction.create",
                key=context.idempotency_key,
                request_hash=context.request_hash,
                resource_kind="project_extraction",
                resource_id=created.extraction_id,
                response=created.model_dump(mode="json"),
            )
            retained = self._replay_extraction(
                stored,
                expected_extraction_id=extraction_id,
            )
            return self._extraction_authority_projection(
                transaction,
                replacement,
                retained,
            )

    @staticmethod
    def _scoped_objective_goal(request: ProjectExtractionCreateRequestV1) -> str:
        scope_guidance = {
            "auto": (
                "Infer whether the material describes game-wide foundations, a permanent feature, "
                "a limited-time event, or a live update. Preserve that scope explicitly and do not "
                "merge event-owned content into permanent content."
            ),
            "game_foundation": (
                "This material is authoritative game-wide foundation design. Model reusable systems "
                "and permanent content; do not wrap the whole game in an EVENT."
            ),
            "permanent_feature": (
                "This material describes a permanent feature. Its content remains available after "
                "release and must not inherit a limited-event expiry."
            ),
            "limited_event": (
                "This material describes a limited-time event. Create one owning EVENT, retain its "
                "gameplay and reward-claim windows, scope event-only content to that EVENT, and keep "
                "shared permanent content as references."
            ),
            "live_update": (
                "This material describes a live update to existing content. Prefer exact changes to "
                "the grounded snapshot over duplicating permanent entities."
            ),
        }[request.planning_scope]
        return (
            f"{request.objective_goal_text.rstrip()}\n\n"
            f"Planning scope authority: {request.planning_scope}. {scope_guidance}"
        )

    def get_extraction(
        self,
        project_id: str,
        extraction_id: str,
        *,
        actor: ActorContext,
    ) -> ProjectExtractionV1:
        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=actor,
                action="read",
                resource_kind="extraction",
                scope=project.domain_scope,
            )
            extraction = _required(transaction.projects, "projects").get_extraction(extraction_id)
            if extraction is None or extraction.project_id != project_id:
                raise NotFound(
                    "project extraction does not exist",
                    extraction_id=extraction_id,
                )
            return self._extraction_authority_projection(
                transaction,
                project,
                extraction,
            )

    def list_extractions(
        self,
        project_id: str,
        *,
        actor: ActorContext,
        limit: int = 100,
    ) -> ProjectExtractionPageV1:
        """Return every retained proposal attempt, newest first."""

        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=actor,
                action="read",
                resource_kind="extraction",
                scope=project.domain_scope,
            )
            retained = _required(transaction.projects, "projects").list_extractions(
                project_id=project_id,
                limit=limit,
            )
            return ProjectExtractionPageV1(
                items=tuple(
                    self._extraction_authority_projection(
                        transaction,
                        project,
                        extraction,
                    )
                    for extraction in retained
                )
            )

    def discard_extraction(
        self,
        project_id: str,
        extraction_id: str,
        request: ProjectExtractionDiscardRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> ProjectExtractionV1:
        """Remove a terminal proposal from active planning without deleting evidence."""

        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            projects = _required(transaction.projects, "projects")
            current = projects.get_extraction(extraction_id)
            if current is None or current.project_id != project_id:
                raise NotFound(
                    "project extraction does not exist",
                    extraction_id=extraction_id,
                )
            principal = self._current_principal(transaction, context.actor)
            is_platform_admin = any(
                assignment.role == "platform_admin" for assignment in principal.roles
            )
            if current.created_by != principal.id and not is_platform_admin:
                raise Forbidden(
                    "only the proposal maker or a platform administrator can discard it"
                )
            self._authorize(
                transaction,
                actor=context.actor,
                action="create",
                resource_kind="extraction",
                scope=project.domain_scope,
                direct_human=True,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.extraction.discard",
            )
            if replay is not None:
                return self._replay_extraction(
                    replay,
                    expected_extraction_id=extraction_id,
                )
            self._require_precondition(
                resource_kind="project_extraction",
                resource_id=extraction_id,
                revision=current.revision,
                expected_revision=request.expected_revision,
                if_match=context.if_match,
            )
            if project.status == "archived":
                raise Conflict("archived project proposals cannot be changed")
            if current.disposition == "discarded":
                raise Conflict(
                    "project extraction is already discarded", extraction_id=extraction_id
                )

            projected = self._extraction_authority_projection(
                transaction,
                project,
                current,
            )
            if projected.status not in {"needs_resolution", "ready", "failed"}:
                raise Conflict(
                    "project extraction is still running and cannot be discarded",
                    extraction_id=extraction_id,
                )
            now = _utc_text(self._clock)
            replacement = projected.model_copy(
                update={
                    "disposition": "discarded",
                    "discarded_by": principal.id,
                    "discarded_at": now,
                    "discard_reason": request.reason,
                    "updated_at": max(projected.updated_at, now),
                    "revision": current.revision + 1,
                }
            )
            discarded = projects.compare_and_set_extraction(
                extraction_id,
                current.revision,
                replacement,
            )

            if project.latest_extraction_id == extraction_id:
                retained = projects.list_extractions(project_id=project_id, limit=1000)
                next_extraction_id = next(
                    (
                        item.extraction_id
                        for item in retained
                        if item.extraction_id != extraction_id and item.disposition != "discarded"
                    ),
                    None,
                )
                project_replacement = project.model_copy(
                    update={
                        "latest_extraction_id": next_extraction_id,
                        "updated_at": max(project.updated_at, now),
                        "revision": project.revision + 1,
                    }
                )
                projects.compare_and_set_project(
                    project_id,
                    project.revision,
                    project_replacement,
                )

            self._append_audit(
                transaction,
                context=context,
                action="project.extraction.discarded",
                subject=AuditSubject(
                    resource_kind="project_extraction",
                    resource_id=extraction_id,
                    artifact_id=discarded.preview_snapshot_artifact_id,
                ),
            )
            stored = _required(transaction.idempotency, "idempotency").put_result(
                scope=self._idempotency_scope(context.actor),
                operation="project.extraction.discard",
                key=context.idempotency_key,
                request_hash=context.request_hash,
                resource_kind="project_extraction",
                resource_id=extraction_id,
                response=discarded.model_dump(mode="json"),
            )
            return self._replay_extraction(
                stored,
                expected_extraction_id=extraction_id,
            )

    # ── full-graph editor → governed human Patch ──────────────────────────
    def prepare_content_draft(
        self,
        project_id: str,
        request: ProjectGraphDraftRequestV1,
        *,
        context: ProjectCommandContext,
    ) -> ProjectContentDraftPreparation:
        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="update",
                resource_kind="project",
                scope=project.domain_scope,
                direct_human=True,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.content_draft.bind",
            )
            if replay is not None:
                return self._replay_content_draft_preparation(replay)
            if project.status == "archived":
                raise Conflict("archived project cannot create a content draft")
            self._require_precondition(
                resource_kind="project",
                resource_id=project_id,
                revision=project.revision,
                expected_revision=request.expected_project_revision,
                if_match=context.if_match,
            )
            source_extraction = self._publication_source_extraction(
                transaction,
                project,
                extraction_id=request.source_extraction_id,
                expected_revision=request.expected_source_extraction_revision,
            )
            current_content = transaction.refs.get(project.content_ref_name)
            current_constraints = transaction.refs.get(project.constraint_ref_name)
            base_artifact_id = (
                project.bootstrap_snapshot_artifact_id
                if current_content is None
                else current_content.artifact_id
            )
            base_artifact, base_payload = self._artifact_payload(
                transaction,
                base_artifact_id,
            )
            if base_artifact.kind != "ir_snapshot":
                raise IntegrityViolation("project content base is not an ir_snapshot")
            base = snapshot_from_canonical_view(base_payload)
            if base.snapshot_id != base_artifact.version_tuple.ir_snapshot_id:
                raise IntegrityViolation("project content base payload differs from its Artifact")
            # The editor normalizes against the same declarations the extraction
            # did; otherwise a planner's own edit splits apart what this project
            # already decided is one thing.
            editor_aliases = self._declared_aliases(transaction, project_id)
            constraint_artifact_id = (
                None if current_constraints is None else current_constraints.artifact_id
            )
            if request.candidate_export_profiles and constraint_artifact_id is None:
                raise Conflict("candidate exports require a published project constraint snapshot")
            if source_extraction.base_snapshot_artifact_id != base_artifact_id:
                raise Conflict(
                    "source extraction is based on another project content version",
                    extraction_id=source_extraction.extraction_id,
                )

        compiled = compile_project_graph_draft(
            base=base,
            entities=request.entities,
            relations=request.relations,
            declared_aliases=editor_aliases,
        )
        if not compiled.ops:
            raise Conflict("project graph has no changes to publish")
        return ProjectContentDraftPreparation(
            workflow_request=HumanPatchDraftRequestV1(
                base_snapshot_artifact_id=base_artifact_id,
                constraint_snapshot_artifact_id=constraint_artifact_id,
                ref_name=project.content_ref_name,
                expected_ref=current_content,
                expected_to_fix=(),
                preconditions=(),
                side_effect_risk=request.side_effect_risk,
                ops=compiled.ops,
                rationale=request.rationale,
                candidate_export_profiles=request.candidate_export_profiles,
            ),
            alias_groups=compiled.alias_groups,
            normalization_summary=compiled.normalization_summary,
            expected_project_revision=request.expected_project_revision,
            source_extraction_id=source_extraction.extraction_id,
            expected_source_extraction_revision=source_extraction.revision,
        )

    def record_content_draft(
        self,
        project_id: str,
        *,
        preparation: ProjectContentDraftPreparation,
        patch_artifact_id: str,
        context: ProjectCommandContext,
    ) -> GameProjectV1:
        with self._unit_of_work.begin() as transaction:
            project = self._require_project(transaction, project_id)
            self._authorize(
                transaction,
                actor=context.actor,
                action="update",
                resource_kind="project",
                scope=project.domain_scope,
                direct_human=True,
            )
            replay = self._idempotency_replay(
                transaction,
                context=context,
                operation="project.content_draft.bind",
            )
            if replay is not None:
                retained_preparation = self._replay_content_draft_preparation(replay)
                if retained_preparation != preparation:
                    raise IntegrityViolation(
                        "content draft replay differs from its retained preparation"
                    )
                return self._replay_content_draft_project(replay, project_id=project_id)
            self._require_precondition(
                resource_kind="project",
                resource_id=project_id,
                revision=project.revision,
                expected_revision=preparation.expected_project_revision,
                if_match=context.if_match,
            )
            source_extraction = self._publication_source_extraction(
                transaction,
                project,
                extraction_id=preparation.source_extraction_id,
                expected_revision=preparation.expected_source_extraction_revision,
            )
            patch_artifact, patch_payload = self._artifact_payload(
                transaction,
                patch_artifact_id,
            )
            try:
                patch = PatchV2.model_validate(patch_payload)
            except ValidationError as exc:
                raise IntegrityViolation("project content draft Patch is malformed") from exc
            workflow = preparation.workflow_request
            if source_extraction.base_snapshot_artifact_id != workflow.base_snapshot_artifact_id:
                raise IntegrityViolation(
                    "content draft source extraction differs from its exact base"
                )
            base_artifact = _required(transaction.artifacts, "artifacts").get(
                workflow.base_snapshot_artifact_id
            )
            if (
                patch_artifact.kind != "patch"
                or patch.produced_by != "human"
                or not patch_artifact.lineage
                or patch_artifact.lineage[0] != workflow.base_snapshot_artifact_id
                or not isinstance(base_artifact, ArtifactV2)
                or patch.base_snapshot_id != base_artifact.version_tuple.ir_snapshot_id
                or canonical_json([operation.model_dump(mode="json") for operation in patch.ops])
                != canonical_json([operation.model_dump(mode="json") for operation in workflow.ops])
                or patch.rationale != workflow.rationale
            ):
                raise IntegrityViolation("project content draft differs from its exact request")
            approval_id = f"approval:patch:{patch_artifact_id}"
            approval = _required(transaction.approvals, "approvals").get(approval_id)
            target = None if approval is None else approval.target_binding
            if (
                approval is None
                or approval.subject_kind != "patch"
                or approval.subject_artifact_id != patch_artifact_id
                or approval.proposer.principal_id != context.actor.principal.id
                or target is None
                or getattr(target, "ref_name", None) != project.content_ref_name
                or getattr(target, "expected_ref", None) != workflow.expected_ref
                or not set(approval.domain_scope.domain_ids).issubset(
                    project.domain_scope.domain_ids
                )
            ):
                raise IntegrityViolation("project content draft ApprovalItem differs")
            now = _utc_text(self._clock)
            extraction_replacement = source_extraction.model_copy(
                update={
                    "publication_patch_artifact_id": patch_artifact_id,
                    "publication_approval_id": approval_id,
                    "updated_at": max(source_extraction.updated_at, now),
                    "revision": source_extraction.revision + 1,
                }
            )
            projects = _required(transaction.projects, "projects")
            updated_extraction = projects.compare_and_set_extraction(
                source_extraction.extraction_id,
                source_extraction.revision,
                extraction_replacement,
            )
            replacement = project.model_copy(
                update={
                    "latest_patch_artifact_id": patch_artifact_id,
                    "latest_approval_id": approval_id,
                    "updated_at": now,
                    "revision": project.revision + 1,
                }
            )
            updated = projects.compare_and_set_project(
                project_id,
                project.revision,
                replacement,
            )
            self._append_audit(
                transaction,
                context=context,
                action="project.content_draft.created",
                subject=AuditSubject(
                    resource_kind="project_extraction",
                    resource_id=source_extraction.extraction_id,
                    artifact_id=patch_artifact_id,
                ),
            )
            response = {
                "response_schema_version": "project-content-draft-binding@2",
                "project": updated.model_dump(mode="json"),
                "source_extraction": updated_extraction.model_dump(mode="json"),
                "workflow_request": workflow.model_dump(mode="json"),
                "alias_groups": [item.model_dump(mode="json") for item in preparation.alias_groups],
                "normalization_summary": preparation.normalization_summary.model_dump(mode="json"),
                "expected_project_revision": preparation.expected_project_revision,
                "source_extraction_id": preparation.source_extraction_id,
                "expected_source_extraction_revision": (
                    preparation.expected_source_extraction_revision
                ),
            }
            stored = _required(transaction.idempotency, "idempotency").put_result(
                scope=self._idempotency_scope(context.actor),
                operation="project.content_draft.bind",
                key=context.idempotency_key,
                request_hash=context.request_hash,
                resource_kind="project_extraction",
                resource_id=source_extraction.extraction_id,
                response=response,
            )
            return self._replay_content_draft_project(stored, project_id=project_id)

    def _publication_source_extraction(
        self,
        transaction: Any,
        project: GameProjectV1,
        *,
        extraction_id: str,
        expected_revision: int,
    ) -> ProjectExtractionV1:
        extraction = _required(transaction.projects, "projects").get_extraction(extraction_id)
        if extraction is None or extraction.project_id != project.project_id:
            raise NotFound(
                "source project extraction does not exist",
                extraction_id=extraction_id,
            )
        if extraction.revision != expected_revision:
            raise Conflict(
                "source extraction revision differs",
                extraction_id=extraction_id,
                expected_revision=expected_revision,
                actual_revision=extraction.revision,
            )
        projected = self._extraction_authority_projection(
            transaction,
            project,
            extraction,
        )
        if projected.disposition == "discarded":
            raise Conflict(
                "discarded source extraction cannot create a publication draft",
                extraction_id=extraction_id,
            )
        if projected.status not in {"ready", "needs_resolution"}:
            raise Conflict(
                "source extraction is not ready for planner publication",
                extraction_id=extraction_id,
                extraction_status=projected.status,
            )
        if projected.patch_artifact_id is None or projected.preview_snapshot_artifact_id is None:
            raise IntegrityViolation(
                "publication-ready source extraction lacks its candidate artifacts",
                extraction_id=extraction_id,
            )
        if (
            projected.publication_patch_artifact_id is not None
            or projected.publication_approval_id is not None
        ):
            raise Conflict(
                "source extraction already has a publication draft",
                extraction_id=extraction_id,
            )
        return extraction

    # ── authority helpers ─────────────────────────────────────────────────
    def _extraction_authority_projection(
        self,
        transaction: Any,
        project: GameProjectV1,
        extraction: ProjectExtractionV1,
    ) -> ProjectExtractionV1:
        run = _required(transaction.runs, "runs").get(extraction.run_id)
        if not isinstance(run, RunRecord):
            raise IntegrityViolation("project extraction Run authority is unavailable")
        params = run.payload.params
        if (
            not isinstance(params, GenerationProposePayloadV1)
            or run.kind.kind != "generation.propose"
            or run.resource_domain_scope != project.domain_scope
            or params.domain_scope != project.domain_scope
            or params.base_snapshot_artifact_id != extraction.base_snapshot_artifact_id
            or params.source_artifact_ids != extraction.source_artifact_ids
            or params.target.ref_name != project.content_ref_name
            or run.initiated_by.principal_id != extraction.created_by
        ):
            raise IntegrityViolation("project extraction mapping differs from its exact Run")

        if extraction.disposition == "discarded":
            return extraction

        failure: RunFailureV1 | None = None
        if run.status == "queued":
            status: ProjectExtractionStatus = "queued"
            artifact_ids: tuple[str, ...] = ()
        elif run.status in {"leased", "running", "retry_wait"}:
            status = "running"
            artifact_ids = ()
        elif run.status == "succeeded":
            if run.result_artifact_id is None:
                raise IntegrityViolation("successful extraction Run lacks its result manifest")
            _manifest, payload = self._artifact_payload(transaction, run.result_artifact_id)
            try:
                result = RunResultV1.model_validate(payload)
            except ValidationError as exc:
                raise IntegrityViolation("project extraction RunResult is malformed") from exc
            if result.run_id != run.run_id or result.run_kind != run.kind:
                raise IntegrityViolation("project extraction RunResult differs from its Run")
            status = "ready"
            artifact_ids = result.produced_artifact_ids
        else:
            if run.failure_artifact_id is None:
                raise IntegrityViolation("terminal extraction Run lacks its failure manifest")
            _manifest, payload = self._artifact_payload(transaction, run.failure_artifact_id)
            try:
                failure = RunFailureV1.model_validate(payload)
            except ValidationError as exc:
                raise IntegrityViolation("project extraction RunFailure is malformed") from exc
            if failure.run_id != run.run_id or failure.run_kind != run.kind:
                raise IntegrityViolation("project extraction RunFailure differs from its Run")
            status = "failed"
            artifact_ids = failure.evidence_artifact_ids

        artifacts: list[ArtifactV2] = []
        for artifact_id in artifact_ids:
            artifact = _required(transaction.artifacts, "artifacts").get(artifact_id)
            if not isinstance(artifact, ArtifactV2):
                raise IntegrityViolation(
                    "project extraction terminal manifest references a missing Artifact",
                    artifact_id=artifact_id,
                )
            artifacts.append(artifact)
        patches = [artifact for artifact in artifacts if artifact.kind == "patch"]
        previews = [artifact for artifact in artifacts if artifact.kind == "ir_snapshot"]
        if len(patches) > 1 or len(previews) > 1:
            raise IntegrityViolation("project extraction terminal outputs are ambiguous")
        patch_artifact_id = patches[0].artifact_id if patches else None
        preview_snapshot_artifact_id = previews[0].artifact_id if previews else None
        if status == "ready" and (
            patch_artifact_id is None or preview_snapshot_artifact_id is None
        ):
            raise IntegrityViolation("ready project extraction lacks patch or preview output")

        summary, alias_groups, identity_conflicts = self._normalization_evidence(
            transaction,
            artifacts,
        )
        rejection_reason = (
            self._generation_rejection_reason(transaction, artifacts)
            if failure is not None
            else None
        )
        validation_issues = (
            self._generation_validation_issues(transaction, artifacts)
            if failure is not None
            and failure.cause_code == "generation_gate_rejected"
            and rejection_reason is None
            else ()
        )
        if summary is not None and summary.blocking_conflict_count:
            if run.status == "succeeded":
                raise IntegrityViolation(
                    "successful project extraction contains blocking identity conflicts"
                )
            status = "needs_resolution"
        elif (
            failure is not None
            and failure.cause_code == "generation_gate_rejected"
            and rejection_reason is None
            and validation_issues
            and patch_artifact_id is not None
            and preview_snapshot_artifact_id is not None
        ):
            # The model call and typed proposal succeeded; deterministic oracles
            # found content that a planner must correct. Keep the Run failure as
            # immutable authority, but project it as editable resolution work.
            status = "needs_resolution"

        approval_id: str | None = None
        if patch_artifact_id is not None and transaction.approvals is not None:
            candidate_approval_id = f"approval:patch:{patch_artifact_id}"
            approval = transaction.approvals.get(candidate_approval_id)
            if approval is not None:
                if approval.subject_artifact_id != patch_artifact_id:
                    raise IntegrityViolation("project extraction ApprovalItem differs")
                approval_id = approval.approval_id
        failure_cause_code: str | None = None
        failure_message: str | None = None
        failure_retryable: bool | None = None
        if failure is not None:
            failure_cause_code, failure_message = self._project_extraction_failure_copy(
                failure,
                rejection_reason=rejection_reason,
                status=status,
                validation_issue_count=len(validation_issues),
                identity_conflict_count=len(identity_conflicts),
            )
            failure_retryable = failure.retryable
        return extraction.model_copy(
            update={
                "status": status,
                "patch_artifact_id": patch_artifact_id,
                "preview_snapshot_artifact_id": preview_snapshot_artifact_id,
                "approval_id": approval_id,
                "failure_cause_code": failure_cause_code,
                "failure_message": failure_message,
                "failure_retryable": failure_retryable,
                "normalization_summary": summary,
                "alias_groups": alias_groups,
                "identity_conflicts": identity_conflicts,
                "validation_issues": validation_issues,
                "updated_at": max(extraction.updated_at, run.updated_at),
            }
        )

    def _generation_validation_issues(
        self,
        transaction: Any,
        artifacts: list[ArtifactV2],
    ) -> tuple[ProjectExtractionIssueV1, ...]:
        entity_labels: dict[str, str] = {}
        for artifact in artifacts:
            if artifact.kind != "ir_snapshot":
                continue
            _manifest, payload = self._artifact_payload(transaction, artifact.artifact_id)
            raw_entities = payload.get("entities")
            if not isinstance(raw_entities, dict):
                continue
            for entity_id, raw_entity in raw_entities.items():
                if not isinstance(entity_id, str) or not isinstance(raw_entity, dict):
                    continue
                attrs = raw_entity.get("attrs")
                if not isinstance(attrs, dict):
                    continue
                for field_name in ("name", "display_name", "title"):
                    candidate = attrs.get(field_name)
                    if isinstance(candidate, str) and candidate.strip():
                        entity_labels[entity_id] = candidate.strip()[:256]
                        break

        issues: dict[str, ProjectExtractionIssueV1] = {}
        for artifact in artifacts:
            source: Literal["structure", "economy"]
            if artifact.kind == "checker_run":
                source = "structure"
            elif artifact.kind == "simulation_run":
                source = "economy"
            else:
                continue
            _manifest, payload = self._artifact_payload(transaction, artifact.artifact_id)
            findings = payload.get("findings")
            if not isinstance(findings, list):
                continue
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                issue = self._project_extraction_issue(
                    finding,
                    source=source,
                    entity_labels=entity_labels,
                )
                if issue is not None:
                    issues[issue.issue_id] = issue
        return tuple(issues[key] for key in sorted(issues))

    @staticmethod
    def _project_extraction_issue(
        finding: dict[str, object],
        *,
        source: Literal["structure", "economy"],
        entity_labels: dict[str, str] | None = None,
    ) -> ProjectExtractionIssueV1 | None:
        if finding.get("status") not in {"confirmed", "unproven"}:
            return None
        code = finding.get("defect_class")
        if not isinstance(code, str) or not code or len(code) > 512:
            return None
        if code in {"identity_normalization", "invalid_generation_proposal"}:
            return None
        evidence = finding.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}

        raw_entities = finding.get("entities")
        entity_ids = (
            [item for item in raw_entities if isinstance(item, str)]
            if isinstance(raw_entities, list)
            else []
        )
        if code == "drop_source_existence_and_yield_rate":
            currencies = evidence.get("currencies_without_source")
            if isinstance(currencies, list):
                entity_ids.extend(item for item in currencies if isinstance(item, str))

        def label(identity: str) -> str:
            resolved = entity_labels.get(identity) if entity_labels is not None else None
            if isinstance(resolved, str) and resolved:
                return resolved[:256]
            value = identity.split(":", 1)[-1].replace("_", " ").strip()
            return (value or "未命名内容")[:256]

        affected = tuple(sorted({label(item) for item in entity_ids}))[:64]
        subject = "、".join(f"“{item}”" for item in affected[:4]) or "这部分内容"
        title = "草案存在需要确认的问题"
        description = f"{subject}还没有通过确定性检查。"
        resolution_hint = "请根据原策划材料检查对应实体、属性和关系后再创建发布草案。"

        if code == "dead_quest":
            missing: list[str] = []
            if evidence.get("has_giver") is False:
                missing.append("发起方")
            if evidence.get("has_steps") is False:
                missing.append("任务步骤")
            missing_text = "和".join(missing) or "完整的起点或步骤"
            title = "任务缺少起点或步骤"
            description = f"{subject}缺少明确的{missing_text}。"
            resolution_hint = (
                "补充材料中明确的发起角色和任务步骤；若它其实是玩法模块，请改为活动或战斗内容。"
            )
        elif code == "isolated_node":
            title = "内容还没有与其他内容关联"
            description = f"{subject}已被提取，但没有连接到任务、奖励、地点或活动。"
            resolution_hint = (
                "补充材料中已有的归属或奖励关系；若它不应是独立实体，可把事实保留为属性。"
            )
        elif code == "drop_source_existence_and_yield_rate":
            title = "货币产出链不完整"
            description = f"{subject}还没有可验证的产出来源和产出速率。"
            resolution_hint = (
                "补充明确的产出来源与数值；若材料只有奖励金额，可先把金额保留在奖励表属性中。"
            )
        elif code == "currency_sink_source_balance":
            title = "货币产出与消耗不平衡"
            description = "当前货币产出与消耗的模拟比例超出安全范围。"
            resolution_hint = "检查产出来源、商店价格、购买概率和所用货币是否来自同一套明确规则。"
        elif code in {"economy_collapse", "inflation_rate"}:
            title = "经济模拟出现失衡"
            description = "当前数值会让货币余额持续异常增长或快速崩塌。"
            resolution_hint = "检查货币来源、消耗口和对应数值；系统不会自动替你猜测平衡值。"
        elif code == "dangling_reference":
            title = "关系引用的内容不存在"
            description = f"{subject}包含无法找到目标内容的关系。"
            resolution_hint = "补充缺失实体，或把关系端点改为草案中确实存在的内容。"
        elif code == "cyclic_dependency":
            title = "任务步骤形成循环"
            description = f"{subject}的前后顺序互相依赖，任务无法确定从哪里开始。"
            resolution_hint = "调整步骤先后关系，确保从开始步骤到结束步骤只有无环方向。"
        elif code == "unsatisfiable_completion":
            title = "任务无法到达完成步骤"
            description = f"{subject}的完成步骤无法从其他步骤到达。"
            resolution_hint = "补齐步骤顺序或前置关系，使完成步骤能从任务入口到达。"
        elif code == "missing_required_attribute":
            title = "内容缺少项目要求的字段"
            description = f"{subject}没有携带项目规则声明必须填写的字段，或该字段的类型不符合要求。"
            resolution_hint = (
                "按材料补齐该字段的真实取值；若这条要求本身不该适用于这类内容，请改约束而不是改内容。"
            )
        elif code == "missing_drop_source":
            title = "收集目标缺少来源"
            description = f"{subject}要求收集内容，但草案没有对应的产出来源。"
            resolution_hint = "把材料中明确的怪物、掉落表、交互物或奖励来源连接到该物品。"
        elif code in {"unreachable_target", "gated_destination"}:
            title = "目标当前不可到达"
            description = f"{subject}受位置或解锁条件阻断，当前流程无法抵达。"
            resolution_hint = "检查地点连接、进入条件和解锁关系是否完整且方向正确。"
        elif code == "unbound_event_schedule":
            title = "限时活动还没有确定档期"
            description = f"{subject}只写了持续时长，还没有可执行的开始和结束时间。"
            resolution_hint = "填写活动开始时间、玩法结束时间、奖励兑换截止时间和时区后再发布。"
        elif code == "invalid_event_lifecycle":
            title = "活动时间窗口不合法"
            description = f"{subject}的开始、玩法结束或奖励兑换时间缺失或顺序不正确。"
            resolution_hint = (
                "确保玩法结束晚于开始时间，奖励兑换截止不早于玩法结束，并使用明确时区。"
            )
        elif code in {"event_scope_owner_missing", "event_scope_membership_missing"}:
            title = "活动专属内容没有正确归属"
            description = f"{subject}被标记为活动内容，但没有连接到唯一的限时活动。"
            resolution_hint = "指定所属活动，并用包含关系把活动专属任务、玩法或商店连接到该活动。"
        elif code == "permanent_depends_on_limited_content":
            title = "永久内容依赖了会过期的内容"
            description = f"{subject}在活动下线后会留下无法满足的依赖。"
            resolution_hint = (
                "反转依赖方向、改为活动内容引用永久内容，或提供活动结束后的永久替代项。"
            )

        severity = finding.get("severity")
        if severity not in {"critical", "major", "minor", "info"}:
            severity = "major"
        issue_id = (
            "project-issue:"
            + source
            + ":"
            + canonical_sha256(
                {
                    "source": source,
                    "code": code,
                    "finding_id": finding.get("id"),
                    "entities": entity_ids,
                    "evidence": evidence,
                }
            )[:32]
        )
        return ProjectExtractionIssueV1(
            issue_id=issue_id,
            source=source,
            severity=severity,
            code=code,
            title=title,
            description=description,
            resolution_hint=resolution_hint,
            affected_content=affected,
        )

    def _generation_rejection_reason(
        self,
        transaction: Any,
        artifacts: list[ArtifactV2],
    ) -> str | None:
        reasons: set[str] = set()
        for artifact in artifacts:
            if artifact.kind != "checker_run":
                continue
            _manifest, payload = self._artifact_payload(transaction, artifact.artifact_id)
            findings = payload.get("findings")
            if not isinstance(findings, list):
                continue
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                if finding.get("defect_class") != "invalid_generation_proposal":
                    continue
                evidence = finding.get("evidence")
                reason = evidence.get("reason_code") if isinstance(evidence, dict) else None
                if isinstance(reason, str) and reason:
                    reasons.add(reason)
        return sorted(reasons)[0] if reasons else None

    @staticmethod
    def _project_extraction_failure_copy(
        failure: RunFailureV1,
        *,
        rejection_reason: str | None,
        status: Literal["queued", "running", "needs_resolution", "ready", "failed"],
        validation_issue_count: int,
        identity_conflict_count: int,
    ) -> tuple[str, str]:
        if status == "needs_resolution":
            issue_count = validation_issue_count + identity_conflict_count
            return (
                "generation_validation_needs_resolution",
                f"已生成可编辑草案，但确定性检查发现 {issue_count} 个需要策划确认的问题。",
            )
        messages = {
            "material_extraction_call_budget_exceeded": (
                "generation_material_call_budget_exceeded",
                "材料包含的结构化内容过多，本次提取已安全停止。",
            ),
            "model_output_truncated": (
                "generation_output_truncated",
                "AI 输出达到长度上限，系统已安全停止。",
            ),
            "unsupported_ir_type": (
                "generation_unsupported_ir_type",
                "AI 使用了系统无法安全识别的实体或关系类型，本次草案未被采用。",
            ),
        }
        if rejection_reason in messages:
            return messages[rejection_reason]
        if failure.cause_code == "generation_gate_rejected":
            return (
                failure.cause_code,
                "AI 草案没有通过结构与一致性检查，本次草案未被采用。",
            )
        return failure.cause_code, "AI 提取未完成，系统已安全停止。"

    def _artifact_payload(
        self,
        transaction: Any,
        artifact_id: str,
    ) -> tuple[ArtifactV2, dict[str, object]]:
        artifact = _required(transaction.artifacts, "artifacts").get(artifact_id)
        if not isinstance(artifact, ArtifactV2):
            raise IntegrityViolation("project authority Artifact is unavailable")
        try:
            location = (
                _required(
                    transaction.object_bindings,
                    "object_bindings",
                )
                .resolve(artifact.object_ref)
                .location
            )
        except FileNotFoundError as exc:
            raise IntegrityViolation("project authority ObjectBinding is unavailable") from exc
        with self._objects.open(location) as source:
            blob = source.read()
        if (
            len(blob) != artifact.object_ref.size_bytes
            or hashlib.sha256(blob).hexdigest() != artifact.object_ref.sha256
        ):
            raise IntegrityViolation("project authority ObjectRef verification failed")
        schema = artifact.meta.get("payload_schema_id")
        if not isinstance(schema, str):
            raise IntegrityViolation("project authority Artifact lacks a payload schema")
        return artifact, decode_and_validate_artifact_payload(
            payload_schema_id=schema,
            blob=blob,
        )

    def _normalization_evidence(
        self,
        transaction: Any,
        artifacts: list[ArtifactV2],
    ) -> tuple[
        IdentityNormalizationSummaryV1 | None,
        tuple[IdentityAliasGroupV1, ...],
        tuple[IdentityConflictV1, ...],
    ]:
        retained: (
            tuple[
                IdentityNormalizationSummaryV1,
                tuple[IdentityAliasGroupV1, ...],
                tuple[IdentityConflictV1, ...],
            ]
            | None
        ) = None
        for artifact in artifacts:
            if artifact.kind != "checker_run":
                continue
            _artifact, payload = self._artifact_payload(transaction, artifact.artifact_id)
            findings = payload.get("findings")
            if not isinstance(findings, list):
                raise IntegrityViolation("project extraction checker report is malformed")
            for finding in findings:
                if not isinstance(finding, dict) or finding.get("defect_class") != (
                    "identity_normalization"
                ):
                    continue
                evidence = finding.get("evidence")
                if not isinstance(evidence, dict):
                    raise IntegrityViolation("identity normalization evidence is malformed")
                try:
                    summary = IdentityNormalizationSummaryV1.model_validate(evidence.get("summary"))
                except ValidationError as exc:
                    raise IntegrityViolation("identity normalization summary is malformed") from exc
                aliases_payload = evidence.get("alias_groups")
                conflicts_payload = evidence.get("blocking_conflicts")
                if not isinstance(aliases_payload, list) or not isinstance(conflicts_payload, list):
                    raise IntegrityViolation("identity normalization evidence is malformed")
                try:
                    aliases = tuple(
                        sorted(
                            (IdentityAliasGroupV1.model_validate(item) for item in aliases_payload),
                            key=lambda item: item.canonical_identity,
                        )
                    )
                    conflicts = tuple(
                        sorted(
                            (IdentityConflictV1.model_validate(item) for item in conflicts_payload),
                            key=lambda item: item.conflict_id,
                        )
                    )
                except ValidationError as exc:
                    raise IntegrityViolation(
                        "identity normalization evidence is malformed"
                    ) from exc
                if (
                    len(aliases) != summary.alias_group_count
                    or len(conflicts) != summary.blocking_conflict_count
                ):
                    raise IntegrityViolation("identity normalization evidence counts differ")
                candidate = (summary, aliases, conflicts)
                if retained is not None and retained != candidate:
                    raise IntegrityViolation(
                        "project extraction has conflicting normalization evidence"
                    )
                retained = candidate
        if retained is None:
            return None, (), ()
        return retained

    def _material_artifacts(
        self,
        *,
        project_id: str,
        material_id: str,
        domain_scope: DomainScope,
        parsed: ParsedPlanningMaterial,
        raw_stored: Any,
        rendered_stored: Any,
        created_at: str,
    ) -> tuple[ArtifactV2, ArtifactV2]:
        planning = self._source_registry.get(PLANNING_DOCUMENT)
        output = self._source_registry.get(TOOL_OUTPUT)
        if (
            planning is None
            or "reviewed_external" not in planning.allowed_trust_levels
            or "context" not in planning.allowed_prompt_purposes
            or output is None
            or "reviewed_external" not in output.allowed_trust_levels
            or "context" not in output.allowed_prompt_purposes
        ):
            raise IntegrityViolation("project material source registry is incompatible")
        origin = OriginRefV1(
            opaque_source_id=f"{project_id}:{material_id}",
            source_revision=raw_stored.ref.sha256,
        )
        raw_provenance = ProvenanceV1(
            source_kind_registry_version=self._source_registry.registry_version,
            source_kind_id=PLANNING_DOCUMENT,
            origin_ref=origin,
            parent_source_artifact_ids=(),
            connector_id=_PROJECT_MATERIAL_CONNECTOR,
            connector_version=_PROJECT_MATERIAL_CONNECTOR_VERSION,
            trust="reviewed_external",
            source_hash=raw_stored.ref.sha256,
        )
        raw = build_artifact_v2(
            kind="source_raw",
            version_tuple=VersionTuple(
                doc_version=raw_stored.ref.sha256,
                tool_version=_PROJECT_MATERIAL_CONNECTOR,
            ),
            lineage=(),
            payload_hash=raw_stored.ref.sha256,
            object_ref=raw_stored.ref,
            meta={
                "payload_schema_id": "project-material-original@1",
                "domain_scope": domain_scope.model_dump(mode="json"),
                "project_id": project_id,
                "material_id": material_id,
                "provenance": raw_provenance.model_dump(mode="json"),
            },
            created_at=created_at,
        )
        rendered_origin = OriginRefV1(
            opaque_source_id=f"{project_id}:{material_id}:rendered",
            source_revision=rendered_stored.ref.sha256,
        )
        rendered_provenance = ProvenanceV1(
            source_kind_registry_version=self._source_registry.registry_version,
            source_kind_id=TOOL_OUTPUT,
            origin_ref=rendered_origin,
            parent_source_artifact_ids=(raw.artifact_id,),
            connector_id=parsed.parser_id,
            connector_version=parsed.parser_version,
            trust="reviewed_external",
            source_hash=rendered_stored.ref.sha256,
            transformations=(
                ProvenanceTransformationV1(
                    tool_version=f"{parsed.parser_id}@{parsed.parser_version}",
                    input_hash=raw_stored.ref.sha256,
                    output_hash=rendered_stored.ref.sha256,
                ),
            ),
        )
        rendered = build_artifact_v2(
            kind="source_rendered",
            version_tuple=VersionTuple(
                doc_version=rendered_stored.ref.sha256,
                tool_version=f"{parsed.parser_id}@{parsed.parser_version}",
            ),
            lineage=(raw.artifact_id,),
            payload_hash=rendered_stored.ref.sha256,
            object_ref=rendered_stored.ref,
            meta={
                "payload_schema_id": "project-material-rendered@1",
                "domain_scope": domain_scope.model_dump(mode="json"),
                "project_id": project_id,
                "material_id": material_id,
                "provenance": rendered_provenance.model_dump(mode="json"),
            },
            created_at=created_at,
        )
        return raw, rendered

    def _policy(self, transaction: Any) -> tuple[RolePolicy, DomainRegistryV1]:
        policies = _required(transaction.policies, "policies")
        role_policy = policies.get_role_policy(
            self._role_policy_version,
            self._role_policy_digest,
        )
        if not isinstance(role_policy, RolePolicy):
            raise DependencyUnavailable(
                "project authoring role policy is unavailable",
                component="project_authoring_authorization",
            )
        registry = policies.get_domain_registry(role_policy.domain_registry_ref)
        if not isinstance(registry, DomainRegistryV1):
            raise DependencyUnavailable(
                "project authoring domain registry is unavailable",
                component="project_authoring_authorization",
            )
        return role_policy, registry

    def _current_principal(self, transaction: Any, actor: ActorContext) -> Principal:
        principal = _required(transaction.identity, "identity").project(actor.principal.id)
        if not isinstance(principal, Principal) or principal.kind != actor.principal.kind:
            raise Forbidden("project authoring actor has no current principal")
        return principal

    def _allowed(
        self,
        transaction: Any,
        *,
        actor: ActorContext,
        action: str,
        resource_kind: str,
        scope: DomainScope,
    ) -> bool:
        role_policy, registry = self._policy(transaction)
        active = {
            definition.domain_id
            for definition in registry.definitions
            if definition.status == "active"
        }
        if not set(scope.domain_ids) <= active:
            return False
        return (
            authorize(
                principal=self._current_principal(transaction, actor),
                role_policy=role_policy,
                requested_permission=Permission(
                    action=action,
                    resource_kind=resource_kind,
                    domain_scope=scope,
                ),
                domain_registry=registry,
            )
            is AuthorizationDecision.ALLOW
        )

    def _authorize(
        self,
        transaction: Any,
        *,
        actor: ActorContext,
        action: str,
        resource_kind: str,
        scope: DomainScope,
        direct_human: bool = False,
    ) -> None:
        if direct_human and actor.principal.kind != "human":
            raise Forbidden("project mutation requires a direct human actor")
        if not self._allowed(
            transaction,
            actor=actor,
            action=action,
            resource_kind=resource_kind,
            scope=scope,
        ):
            raise Forbidden("actor lacks the current project authoring permission")

    @staticmethod
    def _idempotency_scope(actor: ActorContext) -> str:
        return f"principal:{actor.principal.id}"

    def _idempotency_replay(
        self,
        transaction: Any,
        *,
        context: ProjectCommandContext,
        operation: str,
    ) -> dict[str, Any] | None:
        return _required(transaction.idempotency, "idempotency").get_result(
            scope=self._idempotency_scope(context.actor),
            operation=operation,
            key=context.idempotency_key,
            request_hash=context.request_hash,
        )

    def _append_audit(
        self,
        transaction: Any,
        *,
        context: ProjectCommandContext,
        action: str,
        subject: AuditSubject,
    ) -> None:
        AuditGate(sink=_required(transaction.audit, "audit"), clock=self._clock).append(
            chain_id=self._audit_chain_id,
            actor=_actor(context.actor),
            initiated_by=None,
            action=action,
            subject=subject,
            correlation=AuditCorrelation(
                request_id=context.request_id,
                trace_id=context.trace_id,
            ),
        )

    @staticmethod
    def _require_project(transaction: Any, project_id: str) -> GameProjectV1:
        project = _required(transaction.projects, "projects").get_project(project_id)
        if project is None:
            raise NotFound("game project does not exist", project_id=project_id)
        return project

    def _project_authority_projection(
        self,
        transaction: Any,
        project: GameProjectV1,
    ) -> GameProjectV1:
        content = _required(transaction.refs, "refs").get(project.content_ref_name)
        constraints = transaction.refs.get(project.constraint_ref_name)
        status = project.status
        if status != "archived":
            status = "active" if content is not None else "draft"
        if project.latest_extraction_id is not None:
            extraction = _required(transaction.projects, "projects").get_extraction(
                project.latest_extraction_id
            )
            if extraction is None or extraction.project_id != project.project_id:
                raise IntegrityViolation("project latest extraction mapping is unavailable")
            # Validate the exact Run/Artifact authority without presenting its Agent
            # Patch as a planner-confirmed publication draft.  The extraction resource
            # owns that candidate until ``record_content_draft`` records a distinct
            # human Patch after graph review.
            self._extraction_authority_projection(
                transaction,
                project,
                extraction,
            )
        try:
            return GameProjectV1.model_validate(
                {
                    **project.model_dump(mode="json"),
                    "status": status,
                    "current_content_ref": (
                        None if content is None else content.model_dump(mode="json")
                    ),
                    "current_constraint_ref": (
                        None if constraints is None else constraints.model_dump(mode="json")
                    ),
                    "latest_patch_artifact_id": project.latest_patch_artifact_id,
                    "latest_approval_id": project.latest_approval_id,
                }
            )
        except ValidationError as exc:
            raise IntegrityViolation("project ref authority projection is invalid") from exc

    @staticmethod
    def _replay_project(
        response: Any,
        *,
        expected_project_id: str,
    ) -> GameProjectV1:
        try:
            project = GameProjectV1.model_validate(response)
        except ValidationError as exc:
            raise IntegrityViolation("project idempotency response is malformed") from exc
        if project.project_id != expected_project_id:
            raise IntegrityViolation("project idempotency response has another identity")
        return project

    @staticmethod
    def _replay_material(
        response: Any,
        *,
        expected_material_id: str,
    ) -> ProjectMaterialV1:
        try:
            material = ProjectMaterialV1.model_validate(response)
        except ValidationError as exc:
            raise IntegrityViolation("material idempotency response is malformed") from exc
        if material.material_id != expected_material_id:
            raise IntegrityViolation("material idempotency response has another identity")
        return material

    @staticmethod
    def _replay_extraction(
        response: Any,
        *,
        expected_extraction_id: str,
    ) -> ProjectExtractionV1:
        try:
            extraction = ProjectExtractionV1.model_validate(response)
        except ValidationError as exc:
            raise IntegrityViolation("extraction idempotency response is malformed") from exc
        if extraction.extraction_id != expected_extraction_id:
            raise IntegrityViolation("extraction idempotency response has another identity")
        return extraction

    @staticmethod
    def _replay_content_draft_preparation(
        response: Any,
    ) -> ProjectContentDraftPreparation:
        if not isinstance(response, dict) or response.get("response_schema_version") != (
            "project-content-draft-binding@2"
        ):
            raise IntegrityViolation("content draft idempotency response is malformed")
        try:
            workflow_request = HumanPatchDraftRequestV1.model_validate(
                response.get("workflow_request")
            )
            aliases = tuple(
                IdentityAliasGroupV1.model_validate(item)
                for item in response.get("alias_groups", ())
            )
            summary = IdentityNormalizationSummaryV1.model_validate(
                response.get("normalization_summary")
            )
            expected_revision = response.get("expected_project_revision")
            source_extraction_id = response.get("source_extraction_id")
            expected_source_extraction_revision = response.get(
                "expected_source_extraction_revision"
            )
            if (
                not isinstance(expected_revision, int)
                or isinstance(expected_revision, bool)
                or expected_revision < 1
            ):
                raise ValueError("invalid expected project revision")
            if not isinstance(source_extraction_id, str) or not source_extraction_id:
                raise ValueError("invalid source extraction id")
            if (
                not isinstance(expected_source_extraction_revision, int)
                or isinstance(expected_source_extraction_revision, bool)
                or expected_source_extraction_revision < 1
            ):
                raise ValueError("invalid expected source extraction revision")
            source_extraction = ProjectExtractionV1.model_validate(
                response.get("source_extraction")
            )
            if (
                source_extraction.extraction_id != source_extraction_id
                or source_extraction.revision != expected_source_extraction_revision + 1
                or source_extraction.publication_patch_artifact_id is None
                or source_extraction.publication_approval_id is None
            ):
                raise ValueError("invalid retained source extraction binding")
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("content draft replay preparation is malformed") from exc
        return ProjectContentDraftPreparation(
            workflow_request=workflow_request,
            alias_groups=aliases,
            normalization_summary=summary,
            expected_project_revision=expected_revision,
            source_extraction_id=source_extraction_id,
            expected_source_extraction_revision=expected_source_extraction_revision,
        )

    @staticmethod
    def _replay_content_draft_project(
        response: Any,
        *,
        project_id: str,
    ) -> GameProjectV1:
        if not isinstance(response, dict):
            raise IntegrityViolation("content draft idempotency response is malformed")
        try:
            project = GameProjectV1.model_validate(response.get("project"))
        except ValidationError as exc:
            raise IntegrityViolation("content draft replay project is malformed") from exc
        if project.project_id != project_id:
            raise IntegrityViolation("content draft replay project has another identity")
        return project

    def _store_project_result(
        self,
        transaction: Any,
        *,
        context: ProjectCommandContext,
        operation: str,
        project: GameProjectV1,
    ) -> GameProjectV1:
        stored = _required(transaction.idempotency, "idempotency").put_result(
            scope=self._idempotency_scope(context.actor),
            operation=operation,
            key=context.idempotency_key,
            request_hash=context.request_hash,
            resource_kind="project",
            resource_id=project.project_id,
            response=project.model_dump(mode="json"),
        )
        return self._replay_project(stored, expected_project_id=project.project_id)

    @staticmethod
    def _require_precondition(
        *,
        resource_kind: str,
        resource_id: str,
        revision: int,
        expected_revision: int,
        if_match: str | None,
    ) -> None:
        from gameforge.contracts.api import compute_resource_etag

        expected = compute_resource_etag(
            resource_kind=resource_kind,
            resource_id=resource_id,
            revision=revision,
        )
        if expected_revision != revision or if_match != expected:
            raise Conflict(
                "project resource precondition differs",
                resource_kind=resource_kind,
                resource_id=resource_id,
                expected_revision=expected_revision,
                actual_revision=revision,
                expected_etag=expected,
            )


__all__ = [
    "ProjectAuthoringService",
    "ProjectCommandContext",
    "ProjectContentDraftPreparation",
]
