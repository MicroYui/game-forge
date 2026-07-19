"""SQLite bridges for append-only content reads at the API composition boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import timedelta

from pydantic import JsonValue, ValidationError
from sqlalchemy import BigInteger, func, literal_column, select, true
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation, QueryTooBroad
from gameforge.contracts.identity import Permission
from gameforge.contracts.lineage import ArtifactKind, ArtifactV1, ArtifactV2
from gameforge.contracts.storage import PageCursorV1, PageV1, RefValue, UtcClock
from gameforge.contracts.workflow import (
    ApprovalItem,
    EvidenceSet,
    regression_companion_evidence_ids,
)
from gameforge.platform.approvals.commands import EvidenceStateProjection
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
)
from gameforge.platform.read_models.paging import ReadPageBinding
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.models import (
    ApprovalItemRow,
    ArtifactRow,
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


class SqlApprovalPayloadBindingProvider:
    """Resolve frozen publication schemas, cross-checking workflow bindings."""

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
                return None
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
        elif artifact.kind == "validation_evidence":
            approval_id = self._session.scalar(
                select(ApprovalItemRow.approval_id)
                .where(ApprovalItemRow.evidence_set_artifact_id == artifact_id)
                .order_by(ApprovalItemRow.approval_id)
                .limit(1)
            )
            if approval_id is None:
                return None
            item = self._approvals.get(approval_id)
            if item is None or item.evidence_set_artifact_id != artifact_id:
                raise IntegrityViolation(
                    "approval EvidenceSet payload binding is not retained",
                    artifact_id=artifact_id,
                )
            payload_schema_id = "evidence-set@1"
        elif artifact.kind == "regression_evidence":
            regression_values = func.json_each(
                ApprovalItemRow.regression_evidence_artifact_ids
            ).table_valued("key", "value")
            approval_ids = self._session.scalars(
                select(ApprovalItemRow.approval_id)
                .select_from(ApprovalItemRow)
                .join(regression_values, true())
                .where(regression_values.c.value == artifact_id)
                .order_by(ApprovalItemRow.approval_id)
                .limit(2)
            ).all()
            if not approval_ids:
                return None
            if len(approval_ids) != 1:
                raise IntegrityViolation(
                    "one regression Evidence Artifact is bound to multiple ApprovalItems",
                    artifact_id=artifact_id,
                )
            item = self._approvals.get(approval_ids[0])
            if item is None or artifact_id not in item.regression_evidence_artifact_ids:
                raise IntegrityViolation(
                    "approval regression Evidence payload binding is not retained",
                    artifact_id=artifact_id,
                )
            payload_schema_id = "regression-evidence@1"
        else:
            return None
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
        elif artifact.kind == "validation_evidence":
            item = self._retained_evidence_item(artifact.artifact_id)
        elif artifact.kind == "regression_evidence":
            item = self._retained_regression_item(artifact.artifact_id)
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

    def _retained_evidence_item(self, artifact_id: str) -> ApprovalItem | None:
        approval_ids = self._session.scalars(
            select(ApprovalItemRow.approval_id)
            .where(ApprovalItemRow.evidence_set_artifact_id == artifact_id)
            .order_by(ApprovalItemRow.approval_id)
            .limit(2)
        ).all()
        if not approval_ids:
            return None
        if len(approval_ids) != 1:
            raise IntegrityViolation(
                "one EvidenceSet Artifact is bound to multiple ApprovalItems",
                artifact_id=artifact_id,
            )
        item = self._approvals.get(approval_ids[0])
        if item is None or item.evidence_set_artifact_id != artifact_id:
            raise IntegrityViolation(
                "ApprovalItem EvidenceSet Artifact binding is unavailable",
                artifact_id=artifact_id,
            )
        self._require_retained_series(item, artifact_id=artifact_id)
        return item

    def _retained_regression_item(self, artifact_id: str) -> ApprovalItem | None:
        regression_values = func.json_each(
            ApprovalItemRow.regression_evidence_artifact_ids
        ).table_valued("key", "value")
        approval_ids = self._session.scalars(
            select(ApprovalItemRow.approval_id)
            .select_from(ApprovalItemRow)
            .join(regression_values, true())
            .where(regression_values.c.value == artifact_id)
            .order_by(ApprovalItemRow.approval_id)
            .limit(2)
        ).all()
        if not approval_ids:
            return None
        if len(approval_ids) != 1:
            raise IntegrityViolation(
                "one regression Evidence Artifact is bound to multiple ApprovalItems",
                artifact_id=artifact_id,
            )
        item = self._approvals.get(approval_ids[0])
        if item is None or artifact_id not in item.regression_evidence_artifact_ids:
            raise IntegrityViolation(
                "ApprovalItem regression Evidence Artifact binding is unavailable",
                artifact_id=artifact_id,
            )
        self._require_retained_series(item, artifact_id=artifact_id)
        return item

    def _require_retained_series(self, item: ApprovalItem, *, artifact_id: str) -> None:
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
            return
        if (
            item.status != "superseded"
            or item.subject_kind != current_item.subject_kind
            or item.subject_revision >= current_item.subject_revision
        ):
            raise IntegrityViolation(
                "historical ApprovalItem is not a retained superseded subject revision",
                artifact_id=artifact_id,
            )

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
    "SqlApprovalContentAuthority",
    "SqlApprovalPayloadBindingProvider",
    "SqlContentReadRepository",
    "SqlImmutableArtifactPageProvider",
    "SqlRefHistoryReadProvider",
]
