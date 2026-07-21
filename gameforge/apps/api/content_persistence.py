"""SQLite bridges for append-only content reads at the API composition boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import timedelta

from pydantic import JsonValue, ValidationError
from sqlalchemy import BigInteger, func, literal_column, select
from sqlalchemy.orm import Session

from gameforge.contracts.api import SchemaRegistryDocumentV1, SubjectApprovalBindingViewV1
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.diff import SnapshotDiff
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation, QueryTooBroad
from gameforge.contracts.identity import DomainScope, Permission
from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.contracts.lineage import ArtifactKind, ArtifactV1, ArtifactV2
from gameforge.contracts.storage import PageCursorV1, PageV1, RefValue, UtcClock
from gameforge.contracts.workflow import (
    ApprovalItem,
    EvidenceSet,
    SubjectHead,
    regression_companion_evidence_ids,
)
from gameforge.platform.approvals.commands import EvidenceStateProjection
from gameforge.platform.registry import ARTIFACT_PAYLOAD_SCHEMAS
from gameforge.platform.read_models.artifacts import (
    ArtifactPayloadReader,
    TrustedArtifactPayloadBinding,
    VerifiedArtifactPayload,
)
from gameforge.platform.read_models.content import (
    ConstraintProposalWorkflowBinding,
    ContentReadRepository,
    ImmutableArtifactPageProvider,
    LineageSourceEntry,
    PatchWorkflowReadBinding,
    RefHistoryReadProvider,
    RollbackWorkflowReadBinding,
    SnapshotDiffRead,
    SpecReadBinding,
)
from gameforge.platform.read_models.paging import ReadPageBinding
from gameforge.platform.diff.engine import iter_snapshot_diff_entries
from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.contracts.versions import IR_SCHEMA_VERSION, META_SCHEMA_VERSION
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.models import (
    ApprovalEvidenceBindingRow,
    ApprovalItemRow,
    ArtifactRow,
    RefRow,
    RefHistoryRow,
)
from gameforge.runtime.persistence.read_views import (
    ImmutableReadBinding,
    ImmutableReadCandidate,
    SqlImmutableReadViewRepository,
)
from gameforge.runtime.persistence.refs import SqlRefStore


_ARTIFACT_ROWID = literal_column("artifacts.rowid", type_=BigInteger())
_MAX_LINEAGE_ITEMS = 1_000
_BUILTIN_IR_SCHEMA_REGISTRY_VERSION = "registry@1"
_INDEX_KINDS: dict[str, ArtifactKind] = {
    "specs": "ir_snapshot",
    "constraints": "constraint_snapshot",
    "constraint_proposals": "constraint_proposal",
    "patches": "patch",
    "rollback_requests": "rollback_request",
    "reviews": "review_report",
    "task_suites": "task_suite",
}
_SUBJECT_PAYLOAD_SCHEMAS: dict[str, str] = {
    "patch": "patch@2",
    "constraint_proposal": "constraint-proposal@1",
    "rollback_request": "rollback-request@1",
}


def builtin_ir_schema_registry() -> SchemaRegistryDocumentV1:
    """Return the frozen canonical-IR schema document used by local composition."""

    source_ref = {
        "additionalProperties": False,
        "properties": {
            "adapter": {"type": "string"},
            "column": {"type": ["string", "null"]},
            "file": {"type": "string"},
            "row": {"type": ["integer", "null"]},
            "sheet": {"type": ["string", "null"]},
        },
        "required": ["adapter", "file"],
        "type": "object",
    }
    entity = {
        "additionalProperties": False,
        "properties": {
            "attrs": {"additionalProperties": True, "type": "object"},
            "schema_version": {"const": IR_SCHEMA_VERSION},
            "source_ref": source_ref,
            "tags": {"items": {"type": "string"}, "type": "array"},
            "type": {"enum": [item.value for item in NodeType], "type": "string"},
        },
        "required": ["attrs", "schema_version", "type"],
        "type": "object",
    }
    relation = {
        "additionalProperties": False,
        "properties": {
            "attrs": {"additionalProperties": True, "type": "object"},
            "dst_id": {"type": "string"},
            "schema_version": {"const": IR_SCHEMA_VERSION},
            "source_ref": source_ref,
            "src_id": {"type": "string"},
            "type": {"enum": [item.value for item in EdgeType], "type": "string"},
        },
        "required": ["dst_id", "schema_version", "src_id", "type"],
        "type": "object",
    }
    schemas: dict[str, JsonValue] = {
        IR_SCHEMA_VERSION: {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "additionalProperties": False,
            "properties": {
                "entities": {"additionalProperties": entity, "type": "object"},
                "meta_schema_version": {"const": META_SCHEMA_VERSION},
                "relations": {"additionalProperties": relation, "type": "object"},
            },
            "required": ["entities", "meta_schema_version", "relations"],
            "type": "object",
        }
    }
    payload = {
        "registry_schema_version": "schema-registry-document@1",
        "registry_version": _BUILTIN_IR_SCHEMA_REGISTRY_VERSION,
        "schemas": schemas,
    }
    return SchemaRegistryDocumentV1(
        **payload,
        registry_digest=canonical_sha256(payload),
    )


class BuiltinSchemaRegistryProvider:
    """One immutable local registry document; unknown versions stay unavailable."""

    def __init__(self) -> None:
        self._document = builtin_ir_schema_registry()

    def get(self, version: str) -> SchemaRegistryDocumentV1 | None:
        return self._document if version == self._document.registry_version else None


def _immutable_binding(value: ReadPageBinding) -> ImmutableReadBinding:
    if not isinstance(value, ReadPageBinding):
        raise TypeError("binding must be ReadPageBinding")
    return ImmutableReadBinding(
        resource_kind=value.resource_kind,
        query_hash=value.query_hash,
        authz_fingerprint=value.authz_fingerprint,
        stable_sort_schema_id=value.stable_sort_schema_id,
        principal_binding=value.principal_binding,
    )


class SqlContentReadRepository(ContentReadRepository):
    """Expose the existing immutable Artifact authority under the read-model port."""

    def __init__(self, artifacts: SqlArtifactRepository) -> None:
        self._artifacts = artifacts

    def get_artifact(self, artifact_id: str) -> ArtifactV1 | ArtifactV2 | None:
        return self._artifacts.get(artifact_id)


class SqlSpecSnapshotReadAuthority:
    """Derive exact Spec bindings and deterministic diffs from retained Artifacts."""

    def __init__(
        self,
        session: Session,
        *,
        artifacts: SqlArtifactRepository,
        payload_reader: ArtifactPayloadReader,
        refs: SqlRefStore,
    ) -> None:
        if getattr(artifacts, "_session", None) is not session:
            raise ValueError("spec snapshot authority must share the Artifact Session")
        if getattr(refs, "_session", None) is not session:
            raise ValueError("spec snapshot authority must share the Ref Session")
        self._session = session
        self._artifacts = artifacts
        self._payload_reader = payload_reader
        self._refs = refs

    def resolve(self, artifact_id: str) -> SpecReadBinding | None:
        artifact = self._artifacts.get(artifact_id)
        if type(artifact) is not ArtifactV2 or artifact.kind != "ir_snapshot":
            return None
        return self._binding(artifact)

    def resolve_snapshot_id(self, snapshot_id: str) -> SpecReadBinding | None:
        if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 512:
            raise ValueError("snapshot_id must be a non-empty bounded string")
        identifiers = tuple(
            self._session.scalars(
                select(ArtifactRow.artifact_id)
                .where(
                    ArtifactRow.kind == "ir_snapshot",
                    ArtifactRow.version_tuple["ir_snapshot_id"].as_string() == snapshot_id,
                )
                .order_by(ArtifactRow.artifact_id)
                .limit(_MAX_LINEAGE_ITEMS + 1)
            ).all()
        )
        if len(identifiers) > _MAX_LINEAGE_ITEMS:
            raise QueryTooBroad(
                "snapshot identity resolves to too many retained Artifacts",
                max_items=_MAX_LINEAGE_ITEMS,
            )
        candidates: list[tuple[SpecReadBinding, ArtifactV2, DomainScope]] = []
        for identifier in identifiers:
            artifact = self._artifacts.get(identifier)
            if type(artifact) is not ArtifactV2 or artifact.kind != "ir_snapshot":
                raise IntegrityViolation(
                    "snapshot identity index resolved a non-snapshot Artifact",
                    artifact_id=identifier,
                )
            binding = self._binding(artifact)
            snapshot = self._load_snapshot(artifact)
            if snapshot.snapshot_id != snapshot_id:
                raise IntegrityViolation(
                    "snapshot identity differs from verified canonical content",
                    artifact_id=identifier,
                )
            candidates.append((binding, artifact, self._domain_scope(artifact)))
        if not candidates:
            return None

        first_binding, first_artifact, first_scope = candidates[0]
        for binding, artifact, scope in candidates[1:]:
            if (
                artifact.payload_hash != first_artifact.payload_hash
                or binding.schema_registry_version != first_binding.schema_registry_version
                or scope != first_scope
            ):
                raise IntegrityViolation(
                    "duplicate snapshot identity has inconsistent retained authority",
                    snapshot_id=snapshot_id,
                )
        current = [candidate for candidate in candidates if candidate[0].ref_name is not None]
        selected = min(current or candidates, key=lambda candidate: candidate[0].artifact_id)
        return selected[0]

    def read(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
        *,
        max_items: int,
    ) -> SnapshotDiffRead:
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
            raise ValueError("max_items must be positive")
        base = self._snapshot_for_id(base_snapshot_id)
        target = self._snapshot_for_id(target_snapshot_id)
        entries = []
        for entry in iter_snapshot_diff_entries(base.content_payload, target.content_payload):
            if len(entries) == max_items:
                raise QueryTooBroad(
                    "snapshot diff exceeds the configured item bound",
                    max_items=max_items,
                )
            entries.append(entry)
        return SnapshotDiffRead(
            diff=SnapshotDiff(
                base_snapshot_id=base_snapshot_id,
                target_snapshot_id=target_snapshot_id,
                entry_count=len(entries),
            ),
            entries=tuple(entries),
        )

    def _snapshot_for_id(self, snapshot_id: str):
        binding = self.resolve_snapshot_id(snapshot_id)
        if binding is None:
            raise DependencyUnavailable(
                "snapshot Artifact authority is unavailable",
                component="spec_snapshot",
                snapshot_id=snapshot_id,
            )
        artifact = self._artifacts.get(binding.artifact_id)
        if type(artifact) is not ArtifactV2 or artifact.kind != "ir_snapshot":
            raise IntegrityViolation("resolved snapshot Artifact is unavailable")
        return self._load_snapshot(artifact)

    def _binding(self, artifact: ArtifactV2) -> SpecReadBinding:
        snapshot_id = artifact.version_tuple.ir_snapshot_id
        if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 512:
            raise IntegrityViolation(
                "ir_snapshot Artifact has no exact snapshot identity",
                artifact_id=artifact.artifact_id,
            )
        registry_version = self._schema_registry_version(artifact)
        ref_names = tuple(
            self._session.scalars(
                select(RefRow.name)
                .where(RefRow.artifact_id == artifact.artifact_id)
                .order_by(RefRow.name)
                .limit(2)
            ).all()
        )
        if len(ref_names) > 1:
            raise IntegrityViolation(
                "one Spec Artifact is current under multiple refs",
                artifact_id=artifact.artifact_id,
            )
        if not ref_names:
            return SpecReadBinding(
                artifact_id=artifact.artifact_id,
                snapshot_id=snapshot_id,
                schema_registry_version=registry_version,
            )
        ref_name = ref_names[0]
        ref_value = self._refs.get(ref_name)
        if ref_value is None or ref_value.artifact_id != artifact.artifact_id:
            raise IntegrityViolation(
                "Spec current-ref index differs from Ref authority",
                artifact_id=artifact.artifact_id,
            )
        return SpecReadBinding(
            artifact_id=artifact.artifact_id,
            snapshot_id=snapshot_id,
            schema_registry_version=registry_version,
            ref_name=ref_name,
            ref_value=ref_value,
        )

    def _schema_registry_version(self, artifact: ArtifactV2) -> str:
        direct = self._direct_schema_registry_version(artifact)
        if direct is not None:
            return direct
        versions: set[str] = set()
        visited = {artifact.artifact_id}
        stack = list(reversed(tuple(artifact.lineage)))
        while stack:
            parent_id = stack.pop()
            if parent_id in visited:
                continue
            if len(visited) >= _MAX_LINEAGE_ITEMS:
                raise QueryTooBroad(
                    "Spec schema-registry lineage exceeds the configured bound",
                    max_items=_MAX_LINEAGE_ITEMS,
                )
            visited.add(parent_id)
            parent = self._artifacts.get(parent_id)
            if not isinstance(parent, (ArtifactV1, ArtifactV2)):
                raise IntegrityViolation(
                    "Spec schema-registry lineage parent is unavailable",
                    artifact_id=artifact.artifact_id,
                    parent_artifact_id=parent_id,
                )
            if type(parent) is ArtifactV2 and parent.kind == "ir_snapshot":
                inherited = self._direct_schema_registry_version(parent)
                if inherited is not None:
                    versions.add(inherited)
                    if len(versions) > 1:
                        raise IntegrityViolation(
                            "Spec lineage carries conflicting schema-registry versions",
                            artifact_id=artifact.artifact_id,
                        )
            stack.extend(reversed(tuple(parent.lineage)))
        if not versions:
            raise DependencyUnavailable(
                "Spec schema-registry binding is unavailable",
                component="schema_registry",
                artifact_id=artifact.artifact_id,
            )
        return next(iter(versions))

    @staticmethod
    def _direct_schema_registry_version(artifact: ArtifactV2) -> str | None:
        value = artifact.meta.get("schema_registry_version")
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 512:
            raise IntegrityViolation(
                "Spec schema-registry metadata is invalid",
                artifact_id=artifact.artifact_id,
            )
        return value

    @staticmethod
    def _domain_scope(artifact: ArtifactV2) -> DomainScope:
        raw = artifact.meta.get("domain_scope")
        try:
            scope = DomainScope.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "Spec Artifact domain authority is invalid",
                artifact_id=artifact.artifact_id,
            ) from exc
        if raw != scope.model_dump(mode="json"):
            raise IntegrityViolation(
                "Spec Artifact domain authority is noncanonical",
                artifact_id=artifact.artifact_id,
            )
        return scope

    def _load_snapshot(self, artifact: ArtifactV2):
        verified = self._payload_reader.read(artifact.artifact_id)
        if (
            type(verified) is not VerifiedArtifactPayload
            or verified.artifact != artifact
            or verified.kind != "ir_snapshot"
            or verified.payload_schema_id != IR_SCHEMA_VERSION
        ):
            raise IntegrityViolation(
                "Spec Artifact payload binding is invalid",
                artifact_id=artifact.artifact_id,
            )
        snapshot = snapshot_from_canonical_view(verified.payload)
        if snapshot.snapshot_id != artifact.version_tuple.ir_snapshot_id:
            raise IntegrityViolation(
                "Spec Artifact VersionTuple differs from canonical content",
                artifact_id=artifact.artifact_id,
            )
        return snapshot


class SqlApprovalPayloadBindingProvider:
    """Resolve schemas from workflow authority or the frozen kind/schema registry.

    Approval-bound subjects and EvidenceSets retain their stronger cross-check. Other
    publisher-validated Artifacts (for example RunResult and preview/config outputs)
    are readable only when their exact metadata schema is in the canonical registry;
    arbitrary or cross-kind schema claims remain fail-closed.
    """

    def __init__(
        self,
        session: Session,
        *,
        approvals: SqlApprovalRepository,
        artifacts: SqlArtifactRepository,
    ) -> None:
        if getattr(approvals, "_session", None) is not session:
            raise ValueError("approval payload bindings must share the read Session")
        if getattr(artifacts, "_session", None) is not session:
            raise ValueError("artifact payload bindings must share the read Session")
        self._session = session
        self._approvals = approvals
        self._artifacts = artifacts

    def resolve(self, artifact_id: str) -> TrustedArtifactPayloadBinding | None:
        artifact = self._artifacts.get(artifact_id)
        if type(artifact) is not ArtifactV2:
            return None
        if artifact.kind in _SUBJECT_PAYLOAD_SCHEMAS:
            approval_id = self._session.scalar(
                select(ApprovalItemRow.approval_id)
                .where(ApprovalItemRow.subject_artifact_id == artifact_id)
                .order_by(ApprovalItemRow.approval_id)
                .limit(1)
            )
            if approval_id is None:
                return self._canonical_registry_binding(artifact)
            item = self._approvals.get(approval_id)
            if (
                item is None
                or item.subject_artifact_id != artifact_id
                or item.subject_kind != artifact.kind
            ):
                raise IntegrityViolation(
                    "approval subject payload binding is not retained",
                    artifact_id=artifact_id,
                )
            payload_schema_id = _SUBJECT_PAYLOAD_SCHEMAS[artifact.kind]
        elif artifact.kind in {"validation_evidence", "regression_evidence"}:
            retained = self._session.get(ApprovalEvidenceBindingRow, artifact_id)
            if retained is None:
                return self._canonical_registry_binding(artifact)
            expected_kind = "validation" if artifact.kind == "validation_evidence" else "regression"
            if retained.binding_kind != expected_kind:
                raise IntegrityViolation(
                    "approval Evidence Artifact binding kind differs from its Artifact",
                    artifact_id=artifact_id,
                )
            item = self._approvals.get(retained.approval_id)
            expected_binding = item is not None and (
                item.evidence_set_artifact_id == artifact_id
                if expected_kind == "validation"
                else artifact_id in item.regression_evidence_artifact_ids
            )
            if not expected_binding:
                raise IntegrityViolation(
                    "approval Evidence Artifact binding is not retained",
                    artifact_id=artifact_id,
                )
            payload_schema_id = (
                "evidence-set@1" if expected_kind == "validation" else "regression-evidence@1"
            )
        else:
            return self._canonical_registry_binding(artifact)
        metadata_schema = artifact.meta.get("payload_schema_id")
        if metadata_schema is not None and metadata_schema != payload_schema_id:
            raise IntegrityViolation(
                "workflow payload binding differs from the retained Artifact metadata",
                artifact_id=artifact_id,
            )
        return TrustedArtifactPayloadBinding.for_artifact(
            artifact,
            payload_schema_id=payload_schema_id,
        )

    @staticmethod
    def _canonical_registry_binding(
        artifact: ArtifactV2,
    ) -> TrustedArtifactPayloadBinding | None:
        payload_schema_id = artifact.meta.get("payload_schema_id")
        if payload_schema_id is None:
            return None
        allowed = ARTIFACT_PAYLOAD_SCHEMAS[artifact.kind]
        if not isinstance(payload_schema_id, str) or payload_schema_id not in allowed:
            raise IntegrityViolation(
                "Artifact payload schema is outside the canonical kind registry",
                artifact_id=artifact.artifact_id,
            )
        return TrustedArtifactPayloadBinding.for_artifact(
            artifact,
            payload_schema_id=payload_schema_id,
        )


class ApprovalEvidenceStateProjector:
    """Project read-only validation state from exact immutable evidence."""

    def __init__(
        self,
        *,
        artifacts: SqlArtifactRepository,
        payload_reader: ArtifactPayloadReader,
    ) -> None:
        self._artifacts = artifacts
        self._payload_reader = payload_reader

    def project(self, item: ApprovalItem) -> EvidenceStateProjection:
        if type(item) is not ApprovalItem:
            raise TypeError("item must be an exact ApprovalItem")
        if item.evidence_set_artifact_id is None:
            if item.active_validation_run_id is not None:
                validation_status = "running"
            elif item.last_validation_failure_artifact_id is not None:
                failure = self._artifacts.get(item.last_validation_failure_artifact_id)
                if type(failure) is not ArtifactV2 or failure.kind != "run_failure":
                    raise IntegrityViolation(
                        "approval validation failure Artifact is unavailable",
                        approval_id=item.approval_id,
                    )
                validation_status = "execution_failed"
            else:
                validation_status = "not_started"
            return EvidenceStateProjection(
                validation_status=validation_status,
                regression_status="not_started",
            )

        verified = self._payload_reader.read(item.evidence_set_artifact_id)
        if (
            type(verified) is not VerifiedArtifactPayload
            or verified.artifact.kind != "validation_evidence"
            or verified.payload_schema_id != "evidence-set@1"
        ):
            raise IntegrityViolation(
                "approval EvidenceSet Artifact has the wrong kind or schema",
                approval_id=item.approval_id,
            )
        try:
            evidence = EvidenceSet.model_validate(verified.payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation(
                "approval EvidenceSet payload is invalid",
                approval_id=item.approval_id,
            ) from exc
        if (
            evidence.subject_artifact_id != item.subject_artifact_id
            or evidence.subject_digest != item.subject_digest
            or evidence.target_binding != item.target_binding
        ):
            raise IntegrityViolation(
                "approval EvidenceSet differs from its ApprovalItem",
                approval_id=item.approval_id,
            )

        regression_requirements = tuple(
            requirement for requirement in evidence.requirements if requirement.kind == "regression"
        )
        evidence_ids = regression_companion_evidence_ids(evidence)
        if evidence_ids != item.regression_evidence_artifact_ids:
            raise IntegrityViolation(
                "ApprovalItem regression evidence differs from its EvidenceSet",
                approval_id=item.approval_id,
            )
        for artifact_id in evidence_ids:
            artifact = self._artifacts.get(artifact_id)
            if type(artifact) is not ArtifactV2 or artifact.kind != "regression_evidence":
                raise IntegrityViolation(
                    "approval regression evidence Artifact is unavailable",
                    approval_id=item.approval_id,
                    artifact_id=artifact_id,
                )

        required_statuses = tuple(
            requirement.status
            for requirement in regression_requirements
            if requirement.applicability == "required"
        )
        if "failed" in required_statuses:
            regression_status = "failed"
        elif "unproven" in required_statuses:
            regression_status = "unproven"
        elif required_statuses:
            regression_status = "passed"
        else:
            regression_status = "not_applicable"

        if item.status == "validation_failed":
            if evidence.overall_status == "passed":
                raise IntegrityViolation(
                    "failed approval carries passed validation evidence",
                    approval_id=item.approval_id,
                )
        elif item.status != "superseded" and evidence.overall_status != "passed":
            raise IntegrityViolation(
                "non-failed approval carries failed or unproven validation evidence",
                approval_id=item.approval_id,
            )
        return EvidenceStateProjection(
            validation_status=evidence.overall_status,
            regression_status=regression_status,
        )


class SqlApprovalContentAuthority:
    """Expose retained approval workflow/domain authority to bounded content reads."""

    def __init__(
        self,
        session: Session,
        *,
        approvals: SqlApprovalRepository,
        evidence: ApprovalEvidenceStateProjector,
    ) -> None:
        if getattr(approvals, "_session", None) is not session:
            raise ValueError("approval content authority must share the read Session")
        self._session = session
        self._approvals = approvals
        self._evidence = evidence

    def resolve(self, artifact_id: str) -> ConstraintProposalWorkflowBinding | None:
        item = self._retained_item(artifact_id)
        if item is None or item.subject_kind != "constraint_proposal":
            return None
        return ConstraintProposalWorkflowBinding(
            workflow_revision=item.workflow_revision,
            approval_status=item.status,
        )

    def resolve_patch(self, artifact_id: str) -> PatchWorkflowReadBinding | None:
        item = self._retained_item(artifact_id)
        if item is None or item.subject_kind != "patch":
            return None
        state = self._evidence.project(item)
        return PatchWorkflowReadBinding(
            workflow_revision=item.workflow_revision,
            validation_status=state.validation_status,
            regression_status=state.regression_status,
            approval_status=item.status,
        )

    def resolve_rollback(self, artifact_id: str) -> RollbackWorkflowReadBinding | None:
        item = self._retained_item(artifact_id)
        if item is None or item.subject_kind != "rollback_request":
            return None
        return RollbackWorkflowReadBinding(
            workflow_revision=item.workflow_revision,
            approval_status=item.status,
        )

    def resolve_approval_binding(
        self,
        artifact_id: str,
    ) -> SubjectApprovalBindingViewV1 | None:
        item = self._item_for_artifact(artifact_id)
        if item is None:
            return None
        head, _current_item = self._require_retained_series(item, artifact_id=artifact_id)
        return SubjectApprovalBindingViewV1(
            subject_artifact_id=item.subject_artifact_id,
            subject_digest=item.subject_digest,
            subject_kind=item.subject_kind,
            subject_series_id=item.subject_series_id,
            subject_revision=item.subject_revision,
            subject_head_revision=head.revision,
            is_current_head=head.current_approval_id == item.approval_id,
            approval_id=item.approval_id,
            workflow_revision=item.workflow_revision,
            approval_status=item.status,
        )

    def for_artifact(
        self,
        artifact: ArtifactV1 | ArtifactV2,
        *,
        resource_kind: str,
    ) -> Permission:
        if artifact.kind in {"patch", "constraint_proposal", "rollback_request"}:
            item = self._retained_item(artifact.artifact_id)
            if item is not None and item.subject_kind != artifact.kind:
                raise IntegrityViolation(
                    "ApprovalItem subject kind differs from its Artifact",
                    artifact_id=artifact.artifact_id,
                )
        elif artifact.kind in {"validation_evidence", "regression_evidence"}:
            item = self._retained_evidence_item(
                artifact.artifact_id,
                binding_kind=(
                    "validation" if artifact.kind == "validation_evidence" else "regression"
                ),
            )
        else:
            item = None
        if item is None:
            raise DependencyUnavailable(
                "content domain authority is unavailable",
                component="content_producer_binding",
                artifact_id=artifact.artifact_id,
            )
        return Permission(
            action="read",
            resource_kind=resource_kind,
            domain_scope=item.domain_scope,
        )

    def for_ref(
        self,
        ref_name: str,
        value: RefValue,
        artifact: ArtifactV1 | ArtifactV2,
    ) -> None:
        del ref_name, value, artifact
        raise DependencyUnavailable(
            "ref domain authority is unavailable",
            component="content_producer_binding",
        )

    def _retained_item(self, artifact_id: str) -> ApprovalItem | None:
        item = self._item_for_artifact(artifact_id)
        if item is None:
            return None
        self._require_retained_series(item, artifact_id=artifact_id)
        return item

    def _retained_evidence_item(
        self,
        artifact_id: str,
        *,
        binding_kind: str,
    ) -> ApprovalItem | None:
        retained = self._session.get(ApprovalEvidenceBindingRow, artifact_id)
        if retained is None:
            return None
        if retained.binding_kind != binding_kind:
            raise IntegrityViolation(
                "approval Evidence Artifact binding kind differs from its Artifact",
                artifact_id=artifact_id,
            )
        item = self._approvals.get(retained.approval_id)
        bound = item is not None and (
            item.evidence_set_artifact_id == artifact_id
            if binding_kind == "validation"
            else artifact_id in item.regression_evidence_artifact_ids
        )
        if not bound:
            raise IntegrityViolation(
                "ApprovalItem Evidence Artifact binding is unavailable",
                artifact_id=artifact_id,
            )
        self._require_retained_series(item, artifact_id=artifact_id)
        return item

    def _require_retained_series(
        self,
        item: ApprovalItem,
        *,
        artifact_id: str,
    ) -> tuple[SubjectHead, ApprovalItem]:
        current = self._approvals.current(item.subject_series_id)
        if current is None:
            raise IntegrityViolation(
                "ApprovalItem subject series has no retained SubjectHead",
                artifact_id=artifact_id,
            )
        head, current_item = current
        if head.current_approval_id == item.approval_id:
            if current_item != item:
                raise IntegrityViolation(
                    "SubjectHead current ApprovalItem differs from its retained subject",
                    artifact_id=artifact_id,
                )
            return head, current_item
        if (
            item.status != "superseded"
            or item.subject_kind != current_item.subject_kind
            or item.subject_revision >= current_item.subject_revision
        ):
            raise IntegrityViolation(
                "historical ApprovalItem is not a retained superseded subject revision",
                artifact_id=artifact_id,
            )
        return head, current_item

    def _item_for_artifact(self, artifact_id: str) -> ApprovalItem | None:
        approval_ids = self._session.scalars(
            select(ApprovalItemRow.approval_id)
            .where(ApprovalItemRow.subject_artifact_id == artifact_id)
            .order_by(ApprovalItemRow.approval_id)
            .limit(2)
        ).all()
        if not approval_ids:
            return None
        if len(approval_ids) != 1:
            raise IntegrityViolation(
                "one subject Artifact is bound to multiple ApprovalItems",
                artifact_id=artifact_id,
            )
        item = self._approvals.get(approval_ids[0])
        if item is None or item.subject_artifact_id != artifact_id:
            raise IntegrityViolation(
                "ApprovalItem subject Artifact binding is unavailable",
                artifact_id=artifact_id,
            )
        return item


class SqlImmutableArtifactPageProvider(ImmutableArtifactPageProvider):
    """High-watermark pages over immutable Artifact identities."""

    def __init__(
        self,
        session: Session,
        *,
        artifacts: SqlArtifactRepository,
        cursor_signer: CursorSigner,
        clock: UtcClock,
        snapshot_ttl: timedelta,
        snapshot_session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    ) -> None:
        self._session = session
        self._artifacts = artifacts
        self._cursor_signer = cursor_signer
        self._clock = clock
        self._snapshot_ttl = snapshot_ttl
        self._snapshot_session_factory = snapshot_session_factory

    def page(
        self,
        *,
        index_kind: str,
        expected_artifact_kind: ArtifactKind,
        filters: Mapping[str, JsonValue],
        cursor: PageCursorV1 | None,
        binding: ReadPageBinding,
        page_size: int,
    ) -> PageV1[ArtifactV1 | ArtifactV2]:
        registered_kind = _INDEX_KINDS.get(index_kind)
        if registered_kind is None or registered_kind != expected_artifact_kind:
            raise IntegrityViolation("Artifact read index kind is not an immutable M4c index")
        if any(value is not None for value in filters.values()):
            raise DependencyUnavailable(
                "filtered immutable Artifact index is not available before its producer index",
                component="artifact_filter_index",
            )

        def high_watermark() -> int:
            value = self._session.scalar(
                select(func.coalesce(func.max(_ARTIFACT_ROWID), 0)).select_from(ArtifactRow)
            )
            return int(value or 0)

        def load_candidates(
            after_position: str | None,
            retained_high_watermark: int,
            limit: int,
        ) -> tuple[ImmutableReadCandidate[ArtifactV1 | ArtifactV2], ...]:
            statement = select(ArtifactRow.artifact_id, _ARTIFACT_ROWID).where(
                ArtifactRow.kind == expected_artifact_kind,
                _ARTIFACT_ROWID <= retained_high_watermark,
            )
            if index_kind == "patches":
                statement = statement.where(
                    select(ApprovalItemRow.approval_id)
                    .where(
                        ApprovalItemRow.subject_kind == "patch",
                        ApprovalItemRow.subject_artifact_id == ArtifactRow.artifact_id,
                    )
                    .exists()
                )
            if after_position is not None:
                statement = statement.where(ArtifactRow.artifact_id > after_position)
            rows = self._session.execute(
                statement.order_by(ArtifactRow.artifact_id).limit(limit)
            ).all()
            result: list[ImmutableReadCandidate[ArtifactV1 | ArtifactV2]] = []
            for artifact_id, sequence in rows:
                artifact = self._artifacts.get(artifact_id)
                if artifact is None or artifact.kind != expected_artifact_kind:
                    raise IntegrityViolation(
                        "Artifact read index points to missing or wrong-kind content",
                        artifact_id=artifact_id,
                    )
                result.append(
                    ImmutableReadCandidate[ArtifactV1 | ArtifactV2](
                        resource_id=artifact.artifact_id,
                        source_position=artifact.artifact_id,
                        observed_sequence=sequence,
                        observed_revision=1,
                        item=artifact,
                    )
                )
            return tuple(result)

        def retained_page(snapshot_session: Session) -> PageV1[ArtifactV1 | ArtifactV2]:
            return SqlImmutableReadViewRepository[ArtifactV1 | ArtifactV2](
                snapshot_session,
                cursor_signer=self._cursor_signer,
                clock=self._clock,
                page_size=page_size,
                snapshot_ttl=self._snapshot_ttl,
            ).page(
                binding=_immutable_binding(binding),
                cursor=cursor,
                high_watermark=high_watermark,
                load_candidates=load_candidates,
            )

        if cursor is None and self._snapshot_session_factory is not None:
            with self._snapshot_session_factory() as snapshot_session:
                return retained_page(snapshot_session)
        return retained_page(self._session)

    def page_lineage(
        self,
        *,
        root_artifact_id: str,
        cursor: PageCursorV1 | None,
        binding: ReadPageBinding,
        page_size: int,
    ) -> PageV1[LineageSourceEntry]:
        def high_watermark() -> int:
            value = self._session.scalar(
                select(func.coalesce(func.max(_ARTIFACT_ROWID), 0)).select_from(ArtifactRow)
            )
            return int(value or 0)

        def load_candidates(
            after_position: str | None,
            retained_high_watermark: int,
            limit: int,
        ) -> tuple[ImmutableReadCandidate[LineageSourceEntry], ...]:
            entries = self._lineage_entries(
                root_artifact_id,
                retained_high_watermark=retained_high_watermark,
            )
            positions = tuple(self._lineage_position(entry) for entry, _ in entries)
            start = 0
            if after_position is not None:
                try:
                    start = positions.index(after_position) + 1
                except ValueError as exc:
                    raise IntegrityViolation(
                        "retained lineage cursor anchor is not in the immutable traversal"
                    ) from exc
            return tuple(
                ImmutableReadCandidate[LineageSourceEntry](
                    resource_id=(
                        "lineage-entry:"
                        + canonical_sha256(
                            {
                                "root_artifact_id": root_artifact_id,
                                "artifact_id": entry.artifact_id,
                            }
                        )
                    ),
                    source_position=self._lineage_position(entry),
                    observed_sequence=sequence,
                    observed_revision=1,
                    item=entry,
                )
                for entry, sequence in entries[start : start + limit]
            )

        def retained_page(snapshot_session: Session) -> PageV1[LineageSourceEntry]:
            return SqlImmutableReadViewRepository[LineageSourceEntry](
                snapshot_session,
                cursor_signer=self._cursor_signer,
                clock=self._clock,
                page_size=page_size,
                snapshot_ttl=self._snapshot_ttl,
            ).page(
                binding=_immutable_binding(binding),
                cursor=cursor,
                high_watermark=high_watermark,
                load_candidates=load_candidates,
            )

        if cursor is None and self._snapshot_session_factory is not None:
            with self._snapshot_session_factory() as snapshot_session:
                return retained_page(snapshot_session)
        return retained_page(self._session)

    def _lineage_entries(
        self,
        root_artifact_id: str,
        *,
        retained_high_watermark: int,
    ) -> tuple[tuple[LineageSourceEntry, int], ...]:
        root = self._artifacts.get(root_artifact_id)
        if root is None:
            raise IntegrityViolation(
                "lineage root Artifact is missing",
                artifact_id=root_artifact_id,
            )
        seen = {root_artifact_id}
        frontier = tuple(sorted(set(root.lineage)))
        depth = 1
        result: list[tuple[LineageSourceEntry, int]] = []
        while frontier:
            if len(result) + len(frontier) > _MAX_LINEAGE_ITEMS:
                raise QueryTooBroad(
                    "Artifact lineage exceeds the configured traversal bound",
                    max_items=_MAX_LINEAGE_ITEMS,
                )
            seen.update(frontier)
            next_frontier: set[str] = set()
            for artifact_id in frontier:
                if not artifact_id:
                    raise IntegrityViolation("Artifact lineage contains an empty parent id")
                sequence = self._session.scalar(
                    select(_ARTIFACT_ROWID)
                    .select_from(ArtifactRow)
                    .where(
                        ArtifactRow.artifact_id == artifact_id,
                        _ARTIFACT_ROWID <= retained_high_watermark,
                    )
                )
                artifact = self._artifacts.get(artifact_id)
                if sequence is None or artifact is None:
                    raise IntegrityViolation(
                        "Artifact lineage references a missing retained parent",
                        root_artifact_id=root_artifact_id,
                        artifact_id=artifact_id,
                    )
                result.append(
                    (
                        LineageSourceEntry(artifact_id=artifact_id, depth=depth),
                        int(sequence),
                    )
                )
                next_frontier.update(parent for parent in artifact.lineage if parent not in seen)
            frontier = tuple(sorted(next_frontier))
            depth += 1
        return tuple(result)

    @staticmethod
    def _lineage_position(entry: LineageSourceEntry) -> str:
        return f"{entry.depth:020d}:{entry.artifact_id}"


class SqlRefHistoryReadProvider(RefHistoryReadProvider):
    """High-watermark pages over the existing append-only ref history."""

    def __init__(
        self,
        session: Session,
        *,
        refs: SqlRefStore,
        cursor_signer: CursorSigner,
        clock: UtcClock,
        snapshot_ttl: timedelta,
        snapshot_session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    ) -> None:
        self._session = session
        self._refs = refs
        self._cursor_signer = cursor_signer
        self._clock = clock
        self._snapshot_ttl = snapshot_ttl
        self._snapshot_session_factory = snapshot_session_factory

    def get_current(self, ref_name: str) -> RefValue | None:
        return self._refs.get(ref_name)

    def page_history(
        self,
        ref_name: str,
        *,
        cursor: PageCursorV1 | None,
        binding: ReadPageBinding,
        page_size: int,
    ) -> PageV1[RefValue]:
        current = self._refs.get(ref_name)
        if current is None:
            raise IntegrityViolation("ref history requested for a missing ref", ref_name=ref_name)

        def high_watermark() -> int:
            value = self._session.scalar(
                select(func.coalesce(func.max(RefHistoryRow.id), 0)).where(
                    RefHistoryRow.name == ref_name
                )
            )
            return int(value or 0)

        def load_candidates(
            after_position: str | None,
            retained_high_watermark: int,
            limit: int,
        ) -> tuple[ImmutableReadCandidate[RefValue], ...]:
            after_revision = 0
            if after_position is not None:
                if len(after_position) != 20 or not after_position.isdecimal():
                    raise IntegrityViolation("retained ref-history position is invalid")
                after_revision = int(after_position)
            rows = self._session.scalars(
                select(RefHistoryRow)
                .where(
                    RefHistoryRow.name == ref_name,
                    RefHistoryRow.id <= retained_high_watermark,
                    RefHistoryRow.seq > after_revision,
                )
                .order_by(RefHistoryRow.seq)
                .limit(limit)
            ).all()
            expected = after_revision + 1
            result: list[ImmutableReadCandidate[RefValue]] = []
            for row in rows:
                if row.seq != expected or row.seq > current.revision:
                    raise IntegrityViolation("retained ref history is noncontiguous")
                value = RefValue(artifact_id=row.artifact_id, revision=row.seq)
                result.append(
                    ImmutableReadCandidate[RefValue](
                        resource_id=(
                            "ref-history:" + canonical_sha256({"name": ref_name, "seq": row.seq})
                        ),
                        source_position=f"{row.seq:020d}",
                        observed_sequence=row.id,
                        observed_revision=row.seq,
                        item=value,
                    )
                )
                expected += 1
            return tuple(result)

        def retained_page(snapshot_session: Session) -> PageV1[RefValue]:
            return SqlImmutableReadViewRepository[RefValue](
                snapshot_session,
                cursor_signer=self._cursor_signer,
                clock=self._clock,
                page_size=page_size,
                snapshot_ttl=self._snapshot_ttl,
            ).page(
                binding=_immutable_binding(binding),
                cursor=cursor,
                high_watermark=high_watermark,
                load_candidates=load_candidates,
            )

        if cursor is None and self._snapshot_session_factory is not None:
            with self._snapshot_session_factory() as snapshot_session:
                return retained_page(snapshot_session)
        return retained_page(self._session)


__all__ = [
    "ApprovalEvidenceStateProjector",
    "BuiltinSchemaRegistryProvider",
    "SqlApprovalContentAuthority",
    "SqlApprovalPayloadBindingProvider",
    "SqlContentReadRepository",
    "SqlImmutableArtifactPageProvider",
    "SqlRefHistoryReadProvider",
    "SqlSpecSnapshotReadAuthority",
    "builtin_ir_schema_registry",
]
