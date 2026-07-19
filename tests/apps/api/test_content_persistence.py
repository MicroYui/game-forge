from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
)
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryV1,
    DomainRegistryRefV1,
    DomainRoutePolicyRefV1,
    DomainScope,
    Permission,
    compute_domain_registry_digest,
)
from gameforge.contracts.lineage import (
    ArtifactV1,
    ArtifactV2,
    AuditActor,
    ObjectBinding,
    VersionTuple,
    build_artifact_v2,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalRequirement,
    EvidenceRequirement,
    EvidenceSet,
    PatchTargetBindingV1,
    RollbackTargetBindingV1,
    SubjectHead,
    ConstraintProposalV1,
)
from gameforge.platform.read_models.artifacts import VerifiedArtifactPayload
from gameforge.platform.read_models.paging import ReadPageBinding
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store.local import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.apps.api.content_persistence import (
    ApprovalEvidenceStateProjector,
    SqlApprovalContentAuthority,
    SqlApprovalPayloadBindingProvider,
    SqlImmutableArtifactPageProvider,
    SqlRefHistoryReadProvider,
)
from gameforge.apps.api.local_reads import _ArtifactDomainAuthority, _ArtifactDomainPayloadReader
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base, ReadSnapshotRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.refs import SqlRefStore


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
SIGNING_KEY = b"content-read-adapter-test-signing-key"
SNAPSHOT_TTL = timedelta(minutes=5)


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'content-reads.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _clock() -> FrozenUtcClock:
    return FrozenUtcClock(NOW)


def _signer() -> CursorSigner:
    return CursorSigner(signing_key=SIGNING_KEY, clock=_clock())


def _artifacts(session: Session) -> SqlArtifactRepository:
    return SqlArtifactRepository(
        session,
        binding_repository=None,
        cursor_signer=_signer(),
        clock=_clock(),
        page_size=100,
        snapshot_ttl=SNAPSHOT_TTL,
    )


def _artifact_pages(session: Session) -> SqlImmutableArtifactPageProvider:
    return SqlImmutableArtifactPageProvider(
        session,
        artifacts=_artifacts(session),
        cursor_signer=_signer(),
        clock=_clock(),
        snapshot_ttl=SNAPSHOT_TTL,
    )


def _refs(session: Session) -> SqlRefStore:
    return SqlRefStore(
        session,
        cursor_signer=_signer(),
        clock=_clock(),
        page_size=100,
        snapshot_ttl=SNAPSHOT_TTL,
    )


def _ref_history(session: Session) -> SqlRefHistoryReadProvider:
    return SqlRefHistoryReadProvider(
        session,
        refs=_refs(session),
        cursor_signer=_signer(),
        clock=_clock(),
        snapshot_ttl=SNAPSHOT_TTL,
    )


def _binding(resource_kind: str) -> ReadPageBinding:
    return ReadPageBinding(
        resource_kind=resource_kind,
        query_hash=canonical_sha256({"query": resource_kind, "version": 1}),
        authz_fingerprint=canonical_sha256({"authz": "human:a", "revision": 1}),
        stable_sort_schema_id=f"{resource_kind}-stable-sort@1",
        view_schema_id=f"{resource_kind}-view@1",
        principal_binding=canonical_sha256({"principal_id": "human:a", "principal_kind": "human"}),
    )


def _artifact(
    artifact_id: str,
    *,
    kind: str = "ir_snapshot",
    lineage: tuple[str, ...] = (),
    payload_hash: str | None = None,
) -> ArtifactV1:
    return ArtifactV1(
        artifact_id=artifact_id,
        kind=kind,
        version_tuple=VersionTuple(tool_version="content-read-test@1"),
        lineage=list(lineage),
        payload_hash=payload_hash,
        meta={},
    )


def _patch_approval(artifact_id: str, payload_hash: str) -> ApprovalItem:
    domain_ref = DomainRegistryRefV1(
        registry_version="domains@1",
        registry_digest="4" * 64,
    )
    scope = DomainScope(domain_ids=("content",))
    return ApprovalItem(
        approval_id=f"approval:{artifact_id}",
        subject_series_id=f"series:{artifact_id}",
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id=artifact_id,
        subject_digest=payload_hash,
        status="draft",
        workflow_revision=1,
        proposer=AuditActor(principal_id="human:author", principal_kind="human"),
        domain_scope=scope,
        domain_registry_ref=domain_ref,
        route_policy=DomainRoutePolicyRefV1(
            route_version="routes@1",
            route_digest="5" * 64,
            domain_registry_ref=domain_ref,
        ),
        role_policy_version="roles@1",
        role_policy_digest="6" * 64,
        approval_policy=ApprovalPolicyRefV1(
            policy_version="approval-policy@1",
            policy_digest="7" * 64,
        ),
        requirements=(
            ApprovalRequirement(
                requirement_id="content-review",
                domain_scope=scope,
                required_permission=Permission(
                    action="approval.decide",
                    resource_kind="approval",
                    domain_scope=scope,
                ),
                route_role="content_designer",
                min_approvals=1,
                assignee_principal_ids=(),
                distinct_from_requirement_ids=(),
            ),
        ),
        decisions=(),
        regression_evidence_artifact_ids=(),
        target_binding=PatchTargetBindingV1(
            target_artifact_id="artifact:preview",
            target_snapshot_id="snapshot:preview",
            target_digest="8" * 64,
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=1),
        ),
        created_at="2026-07-14T12:00:00Z",
    )


def _constraint_approval(artifact_id: str, payload_hash: str) -> ApprovalItem:
    return ApprovalItem.model_validate(
        {
            **_patch_approval(artifact_id, payload_hash).model_dump(mode="json"),
            "subject_kind": "constraint_proposal",
            "target_binding": None,
        }
    )


def _rollback_approval(artifact_id: str, payload_hash: str) -> ApprovalItem:
    profile = ResolvedExecutionProfileBindingV1(
        field_path="/params/rollback_profile",
        profile=ProfileRefV1(profile_id="rollback.default", version=1),
        expected_profile_kind="rollback",
        profile_payload_hash="9" * 64,
        catalog_version=1,
        catalog_digest="a" * 64,
    )
    return ApprovalItem.model_validate(
        {
            **_patch_approval(artifact_id, payload_hash).model_dump(mode="json"),
            "subject_kind": "rollback_request",
            "target_binding": RollbackTargetBindingV1(
                target_artifact_kind="ir_snapshot",
                target_artifact_id="artifact:rollback-target",
                target_snapshot_id="snapshot:rollback-target",
                target_digest="b" * 64,
                ref_name="content/head",
                expected_ref=RefValue(artifact_id="artifact:current", revision=2),
                rollback_profile_binding=profile,
            ).model_dump(mode="json"),
        }
    )


def _replace_approval(item: ApprovalItem, **changes: object) -> ApprovalItem:
    return ApprovalItem.model_validate({**item.model_dump(mode="json"), **changes})


def _publish_superseding_subjects(
    session: Session,
    *,
    first_artifact: ArtifactV1,
    second_artifact: ArtifactV1,
    first_item: ApprovalItem,
    second_item: ApprovalItem,
) -> tuple[SqlApprovalRepository, SqlArtifactRepository]:
    artifacts = _artifacts(session)
    artifacts.put(first_artifact)
    artifacts.put(second_artifact)
    approvals = SqlApprovalRepository(session)
    approvals.insert_draft(first_item)
    first_head = approvals.compare_and_set_subject_head(
        first_item.subject_series_id,
        None,
        SubjectHead(
            subject_series_id=first_item.subject_series_id,
            current_subject_artifact_id=first_item.subject_artifact_id,
            current_approval_id=first_item.approval_id,
            revision=1,
        ),
    )
    approvals.compare_and_set(
        first_item.approval_id,
        first_item.workflow_revision,
        _replace_approval(
            first_item,
            status="superseded",
            workflow_revision=first_item.workflow_revision + 1,
        ),
    )
    approvals.insert_draft(second_item)
    approvals.compare_and_set_subject_head(
        second_item.subject_series_id,
        first_head,
        SubjectHead(
            subject_series_id=second_item.subject_series_id,
            current_subject_artifact_id=second_item.subject_artifact_id,
            current_approval_id=second_item.approval_id,
            revision=2,
        ),
    )
    return approvals, artifacts


def _approval_authority(
    session: Session,
    *,
    approvals: SqlApprovalRepository,
    artifacts: SqlArtifactRepository,
) -> SqlApprovalContentAuthority:
    return SqlApprovalContentAuthority(
        session,
        approvals=approvals,
        evidence=ApprovalEvidenceStateProjector(
            artifacts=artifacts,
            payload_reader=_PayloadMap(()),  # type: ignore[arg-type]
        ),
    )


def _publish_validated_patch(
    session: Session,
    *,
    approvals: SqlApprovalRepository,
    subject: ArtifactV1,
    evidence: ArtifactV1,
    series_id: str,
    regression_evidence_artifact_ids: tuple[str, ...] = (),
) -> ApprovalItem:
    artifacts = _artifacts(session)
    artifacts.put(subject)
    artifacts.put(evidence)
    draft = _replace_approval(
        _patch_approval(subject.artifact_id, subject.payload_hash or ""),
        subject_series_id=series_id,
    )
    approvals.insert_draft(draft)
    approvals.compare_and_set_subject_head(
        series_id,
        None,
        SubjectHead(
            subject_series_id=series_id,
            current_subject_artifact_id=subject.artifact_id,
            current_approval_id=draft.approval_id,
            revision=1,
        ),
    )
    validating = _replace_approval(
        draft,
        status="validating",
        workflow_revision=2,
        active_validation_run_id=f"run:validation:{subject.artifact_id}",
    )
    approvals.compare_and_set(draft.approval_id, draft.workflow_revision, validating)
    validated = _replace_approval(
        validating,
        status="validated",
        workflow_revision=3,
        active_validation_run_id=None,
        evidence_set_artifact_id=evidence.artifact_id,
        regression_evidence_artifact_ids=regression_evidence_artifact_ids,
    )
    approvals.compare_and_set_validation_completion(
        validating.approval_id,
        validating.workflow_revision,
        validated,
    )
    return validated


class _ArtifactMap:
    def __init__(self, values: tuple[ArtifactV2, ...]) -> None:
        self._values = {value.artifact_id: value for value in values}

    def get(self, artifact_id: str):
        return self._values.get(artifact_id)


class _PayloadMap:
    def __init__(self, values: tuple[VerifiedArtifactPayload, ...]) -> None:
        self._values = {value.artifact.artifact_id: value for value in values}

    def read(self, artifact_id: str) -> VerifiedArtifactPayload:
        return self._values[artifact_id]


def _verified_payload(
    *,
    kind: str,
    payload_schema_id: str,
    payload: dict,
) -> VerifiedArtifactPayload:
    payload_bytes = canonical_json(payload).encode("utf-8")
    digest = canonical_sha256(payload)
    from gameforge.contracts.lineage import ObjectLocation, ObjectRef

    ref = ObjectRef(
        key=f"objects/v1/sha256/{digest[:2]}/{digest}",
        sha256=digest,
        size_bytes=len(payload_bytes),
    )
    location = ObjectLocation(
        store_id="local:test",
        key=ref.key,
        backend_generation=f"test-{digest[:16]}",
    )
    artifact = build_artifact_v2(
        kind=kind,  # type: ignore[arg-type]
        version_tuple=VersionTuple(tool_version="content-read-test@1"),
        lineage=(),
        payload_hash=digest,
        object_ref=ref,
    )
    return VerifiedArtifactPayload(
        artifact=artifact,
        object_binding=ObjectBinding(
            object_ref=ref,
            location=location,
            status="active",
            revision=1,
            verified_at="2026-07-14T12:00:00Z",
        ),
        payload_schema_id=payload_schema_id,
        kind=artifact.kind,
        metadata={},
        payload_bytes=payload_bytes,
        payload=payload,
    )


def test_artifact_kind_pages_use_one_retained_immutable_high_watermark(
    engine: Engine,
) -> None:
    with Session(engine) as setup, setup.begin():
        repository = _artifacts(setup)
        for item in (
            _artifact("spec-e"),
            _artifact("constraint-b", kind="constraint_snapshot"),
            _artifact("spec-a"),
            _artifact("review-d", kind="review_report"),
            _artifact("spec-c"),
        ):
            repository.put(item)

    binding = _binding("specs")
    with Session(engine) as first_session, first_session.begin():
        first = _artifact_pages(first_session).page(
            index_kind="specs",
            expected_artifact_kind="ir_snapshot",
            filters={},
            cursor=None,
            binding=binding,
            page_size=2,
        )

    assert [item.artifact_id for item in first.items] == ["spec-a", "spec-c"]
    assert first.next_cursor is not None
    with Session(engine) as verification:
        snapshot = verification.get(ReadSnapshotRow, first.read_snapshot_id)
        assert snapshot is not None
        assert snapshot.strategy == "immutable_high_watermark"
        assert snapshot.high_watermark is not None

    with Session(engine) as concurrent, concurrent.begin():
        _artifacts(concurrent).put(_artifact("spec-d"))

    with Session(engine) as second_session, second_session.begin():
        second = _artifact_pages(second_session).page(
            index_kind="specs",
            expected_artifact_kind="ir_snapshot",
            filters={},
            cursor=first.next_cursor,
            binding=binding,
            page_size=2,
        )

    assert second.read_snapshot_id == first.read_snapshot_id
    assert [item.artifact_id for item in second.items] == ["spec-e"]
    assert second.next_cursor is None

    with Session(engine) as fresh_session, fresh_session.begin():
        fresh = _artifact_pages(fresh_session).page(
            index_kind="specs",
            expected_artifact_kind="ir_snapshot",
            filters={},
            cursor=None,
            binding=binding,
            page_size=10,
        )
    assert [item.artifact_id for item in fresh.items] == [
        "spec-a",
        "spec-c",
        "spec-d",
        "spec-e",
    ]


def test_ref_history_pages_are_contiguous_and_exclude_later_appends(
    engine: Engine,
) -> None:
    with Session(engine) as setup, setup.begin():
        refs = _refs(setup)
        current = refs.compare_and_set("release", None, "artifact-1")
        for revision in range(2, 6):
            current = refs.compare_and_set(
                "release",
                current,
                f"artifact-{revision}",
            )

    binding = _binding("ref_history")
    with Session(engine) as first_session, first_session.begin():
        first = _ref_history(first_session).page_history(
            "release",
            cursor=None,
            binding=binding,
            page_size=2,
        )
    assert first.next_cursor is not None

    with Session(engine) as concurrent, concurrent.begin():
        refs = _refs(concurrent)
        current = refs.get("release")
        assert current == RefValue(artifact_id="artifact-5", revision=5)
        refs.compare_and_set("release", current, "artifact-6")

    retained = list(first.items)
    cursor = first.next_cursor
    while cursor is not None:
        with Session(engine) as continued_session, continued_session.begin():
            page = _ref_history(continued_session).page_history(
                "release",
                cursor=cursor,
                binding=binding,
                page_size=2,
            )
        assert page.read_snapshot_id == first.read_snapshot_id
        retained.extend(page.items)
        cursor = page.next_cursor

    assert [item.revision for item in retained] == [1, 2, 3, 4, 5]
    assert [item.artifact_id for item in retained] == [
        "artifact-1",
        "artifact-2",
        "artifact-3",
        "artifact-4",
        "artifact-5",
    ]
    assert len({(item.artifact_id, item.revision) for item in retained}) == len(retained)


def test_filtered_task_suite_index_fails_closed_until_its_producer_index_exists(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        with pytest.raises(DependencyUnavailable, match="filtered immutable Artifact index"):
            _artifact_pages(session).page(
                index_kind="task_suites",
                expected_artifact_kind="task_suite",
                filters={"config_artifact_id": "config:one"},
                cursor=None,
                binding=_binding("task_suites"),
                page_size=10,
            )


def test_patch_index_excludes_evidence_only_artifacts_without_workflow(
    engine: Engine,
) -> None:
    workflow_hash = "1" * 64
    with Session(engine) as setup, setup.begin():
        artifacts = _artifacts(setup)
        artifacts.put(
            _artifact(
                "patch:workflow",
                kind="patch",
                payload_hash=workflow_hash,
            )
        )
        artifacts.put(
            _artifact(
                "patch:evidence-only",
                kind="patch",
                payload_hash="2" * 64,
            )
        )
        SqlApprovalRepository(setup).insert_draft(_patch_approval("patch:workflow", workflow_hash))

    with Session(engine) as session, session.begin():
        page = _artifact_pages(session).page(
            index_kind="patches",
            expected_artifact_kind="patch",
            filters={},
            cursor=None,
            binding=_binding("patches"),
            page_size=10,
        )

    assert [item.artifact_id for item in page.items] == ["patch:workflow"]


def test_sql_approval_content_authority_uses_current_subject_head(engine: Engine) -> None:
    payload_hash = "1" * 64
    artifact = _artifact("patch:workflow", kind="patch", payload_hash=payload_hash)
    item = _patch_approval(artifact.artifact_id, payload_hash)
    with Session(engine) as session, session.begin():
        artifacts = _artifacts(session)
        artifacts.put(artifact)
        approvals = SqlApprovalRepository(session)
        approvals.insert_draft(item)
        approvals.compare_and_set_subject_head(
            item.subject_series_id,
            None,
            SubjectHead(
                subject_series_id=item.subject_series_id,
                current_subject_artifact_id=item.subject_artifact_id,
                current_approval_id=item.approval_id,
                revision=1,
            ),
        )
        authority = SqlApprovalContentAuthority(
            session,
            approvals=approvals,
            evidence=ApprovalEvidenceStateProjector(
                artifacts=artifacts,
                payload_reader=_PayloadMap(()),  # type: ignore[arg-type]
            ),
        )

        workflow = authority.resolve_patch(artifact.artifact_id)
        permission = authority.for_artifact(artifact, resource_kind="patch")

    assert workflow is not None
    assert workflow.workflow_revision == 1
    assert workflow.validation_status == "not_started"
    assert workflow.regression_status == "not_started"
    assert workflow.approval_status == "draft"
    assert permission == Permission(
        action="read",
        resource_kind="patch",
        domain_scope=item.domain_scope,
    )


def test_sql_approval_content_authority_uses_indexed_validation_owner(
    engine: Engine,
) -> None:
    subject = _artifact("patch:evidence-domain", kind="patch", payload_hash="c" * 64)
    evidence = _artifact(
        "validation-evidence:domain",
        kind="validation_evidence",
        payload_hash="d" * 64,
    )
    with Session(engine) as session, session.begin():
        approvals = SqlApprovalRepository(session)
        item = _publish_validated_patch(
            session,
            approvals=approvals,
            subject=subject,
            evidence=evidence,
            series_id="series:evidence-domain",
        )
        authority = _approval_authority(
            session,
            approvals=approvals,
            artifacts=_artifacts(session),
        )
        statements: list[str] = []

        def capture_statement(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ) -> None:
            del connection, cursor, parameters, context, executemany
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            permission = authority.for_artifact(evidence, resource_kind="artifact")
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

    assert permission == Permission(
        action="read",
        resource_kind="artifact",
        domain_scope=item.domain_scope,
    )
    assert any("approval_evidence_bindings" in statement for statement in statements)
    assert not any("json_each" in statement.lower() for statement in statements)


def test_approval_evidence_index_rejects_a_second_owner(engine: Engine) -> None:
    evidence = _artifact(
        "validation-evidence:shared",
        kind="validation_evidence",
        payload_hash="e" * 64,
    )
    with Session(engine) as session, session.begin():
        _publish_validated_patch(
            session,
            approvals=SqlApprovalRepository(session),
            subject=_artifact("patch:one", kind="patch", payload_hash="1" * 64),
            evidence=evidence,
            series_id="series:one",
        )

    with pytest.raises(IntegrityViolation, match="already bound"):
        with Session(engine) as session, session.begin():
            _publish_validated_patch(
                session,
                approvals=SqlApprovalRepository(session),
                subject=_artifact("patch:two", kind="patch", payload_hash="2" * 64),
                evidence=evidence,
                series_id="series:two",
            )


def test_sql_approval_content_authority_uses_indexed_regression_owner(
    engine: Engine,
) -> None:
    subject = _artifact("patch:regression-owner", kind="patch", payload_hash="1" * 64)
    evidence = _artifact(
        "evidence:regression-owner",
        kind="validation_evidence",
        payload_hash="2" * 64,
    )
    regression = _artifact(
        "regression:owned",
        kind="regression_evidence",
        payload_hash="3" * 64,
    )
    with Session(engine) as session, session.begin():
        artifacts = _artifacts(session)
        artifacts.put(regression)
        approvals = SqlApprovalRepository(session)
        item = _publish_validated_patch(
            session,
            approvals=approvals,
            subject=subject,
            evidence=evidence,
            series_id="series:regression-owner",
            regression_evidence_artifact_ids=(regression.artifact_id,),
        )
        authority = _approval_authority(
            session,
            approvals=approvals,
            artifacts=artifacts,
        )
        statements: list[str] = []

        def capture_statement(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ) -> None:
            del connection, cursor, parameters, context, executemany
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            permission = authority.for_artifact(regression, resource_kind="artifact")
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

    assert permission == Permission(
        action="read",
        resource_kind="artifact",
        domain_scope=item.domain_scope,
    )
    assert any("approval_evidence_bindings" in statement for statement in statements)
    assert not any("json_each" in statement.lower() for statement in statements)


def test_sql_approval_content_authority_retains_superseded_patch_projection(
    engine: Engine,
) -> None:
    first_artifact = _artifact("patch:1", kind="patch", payload_hash="1" * 64)
    second_artifact = _artifact("patch:2", kind="patch", payload_hash="2" * 64)
    first_item = _replace_approval(
        _patch_approval(first_artifact.artifact_id, first_artifact.payload_hash or ""),
        subject_series_id="series:patch",
    )
    second_item = _replace_approval(
        _patch_approval(second_artifact.artifact_id, second_artifact.payload_hash or ""),
        subject_series_id=first_item.subject_series_id,
        subject_revision=2,
        supersedes_approval_id=first_item.approval_id,
    )
    with Session(engine) as session, session.begin():
        approvals, artifacts = _publish_superseding_subjects(
            session,
            first_artifact=first_artifact,
            second_artifact=second_artifact,
            first_item=first_item,
            second_item=second_item,
        )

        historical = _approval_authority(
            session,
            approvals=approvals,
            artifacts=artifacts,
        ).resolve_patch(first_artifact.artifact_id)

    assert historical is not None
    assert historical.workflow_revision == 2
    assert historical.approval_status == "superseded"
    assert historical.validation_status == "not_started"
    assert historical.regression_status == "not_started"


def test_sql_approval_content_authority_retains_superseded_constraint_projection(
    engine: Engine,
) -> None:
    first_artifact = _artifact(
        "constraint-proposal:1",
        kind="constraint_proposal",
        payload_hash="3" * 64,
    )
    second_artifact = _artifact(
        "constraint-proposal:2",
        kind="constraint_proposal",
        payload_hash="4" * 64,
    )
    first_item = _replace_approval(
        _constraint_approval(first_artifact.artifact_id, first_artifact.payload_hash or ""),
        subject_series_id="series:constraint-proposal",
    )
    second_item = _replace_approval(
        _constraint_approval(second_artifact.artifact_id, second_artifact.payload_hash or ""),
        subject_series_id=first_item.subject_series_id,
        subject_revision=2,
        supersedes_approval_id=first_item.approval_id,
    )
    with Session(engine) as session, session.begin():
        approvals, artifacts = _publish_superseding_subjects(
            session,
            first_artifact=first_artifact,
            second_artifact=second_artifact,
            first_item=first_item,
            second_item=second_item,
        )

        historical = _approval_authority(
            session,
            approvals=approvals,
            artifacts=artifacts,
        ).resolve(first_artifact.artifact_id)

    assert historical is not None
    assert historical.workflow_revision == 2
    assert historical.approval_status == "superseded"


def test_sql_approval_content_authority_retains_superseded_rollback_projection(
    engine: Engine,
) -> None:
    first_artifact = _artifact(
        "rollback-request:1",
        kind="rollback_request",
        payload_hash="5" * 64,
    )
    second_artifact = _artifact(
        "rollback-request:2",
        kind="rollback_request",
        payload_hash="6" * 64,
    )
    first_item = _replace_approval(
        _rollback_approval(first_artifact.artifact_id, first_artifact.payload_hash or ""),
        subject_series_id="series:rollback-request",
    )
    second_item = _replace_approval(
        _rollback_approval(second_artifact.artifact_id, second_artifact.payload_hash or ""),
        subject_series_id=first_item.subject_series_id,
        subject_revision=2,
        supersedes_approval_id=first_item.approval_id,
    )
    with Session(engine) as session, session.begin():
        approvals, artifacts = _publish_superseding_subjects(
            session,
            first_artifact=first_artifact,
            second_artifact=second_artifact,
            first_item=first_item,
            second_item=second_item,
        )

        historical = _approval_authority(
            session,
            approvals=approvals,
            artifacts=artifacts,
        ).resolve_rollback(first_artifact.artifact_id)

    assert historical is not None
    assert historical.workflow_revision == 2
    assert historical.approval_status == "superseded"


@pytest.mark.parametrize(
    ("outcome", "regression_status"),
    (("passed", "not_applicable"), ("failed", "failed"), ("unproven", "unproven")),
)
def test_evidence_projector_derives_exact_validation_and_regression_state(
    outcome: str,
    regression_status: str,
) -> None:
    subject = _verified_payload(
        kind="patch",
        payload_schema_id="patch@2",
        payload={"patch_schema_version": "patch@2"},
    ).artifact
    item = _patch_approval(subject.artifact_id, subject.payload_hash)
    regression = None
    requirements: tuple[EvidenceRequirement, ...] = ()
    regression_ids: tuple[str, ...] = ()
    if outcome != "passed":
        regression = _verified_payload(
            kind="regression_evidence",
            payload_schema_id="regression-evidence@1",
            payload={"verdict": outcome},
        ).artifact
        requirements = (
            EvidenceRequirement(
                requirement_id="regression",
                kind="regression",
                applicability="required",
                status=outcome,  # type: ignore[arg-type]
                evidence_artifact_id=regression.artifact_id,
                reason_code="regression_unproven" if outcome == "unproven" else None,
                tool_version="regression@1",
            ),
        )
        regression_ids = (regression.artifact_id,)
    evidence = EvidenceSet(
        subject_artifact_id=item.subject_artifact_id,
        subject_digest=item.subject_digest,
        policy_version="validation@1",
        validation_run_id="run:validation:1",
        target_binding=item.target_binding,
        supporting_artifact_ids=regression_ids,
        finding_bindings=(),
        requirements=requirements,
        overall_status=outcome,  # type: ignore[arg-type]
    )
    verified = _verified_payload(
        kind="validation_evidence",
        payload_schema_id="evidence-set@1",
        payload=evidence.model_dump(mode="json"),
    )
    projected_item = _replace_approval(
        item,
        status="validated" if outcome == "passed" else "validation_failed",
        workflow_revision=2,
        evidence_set_artifact_id=verified.artifact.artifact_id,
        regression_evidence_artifact_ids=regression_ids,
    )
    artifacts = (verified.artifact,) if regression is None else (verified.artifact, regression)
    projector = ApprovalEvidenceStateProjector(
        artifacts=_ArtifactMap(artifacts),  # type: ignore[arg-type]
        payload_reader=_PayloadMap((verified,)),  # type: ignore[arg-type]
    )

    state = projector.project(projected_item)

    assert state.validation_status == outcome
    assert state.regression_status == regression_status


def test_evidence_projector_distinguishes_running_and_execution_failure() -> None:
    subject = _verified_payload(
        kind="patch",
        payload_schema_id="patch@2",
        payload={"patch_schema_version": "patch@2"},
    ).artifact
    item = _patch_approval(subject.artifact_id, subject.payload_hash)
    failure = _verified_payload(
        kind="run_failure",
        payload_schema_id="run-failure@1",
        payload={"failure_schema_version": "run-failure@1"},
    ).artifact
    projector = ApprovalEvidenceStateProjector(
        artifacts=_ArtifactMap((failure,)),  # type: ignore[arg-type]
        payload_reader=_PayloadMap(()),  # type: ignore[arg-type]
    )

    running = projector.project(
        _replace_approval(
            item,
            status="validating",
            workflow_revision=2,
            active_validation_run_id="run:validation:1",
        )
    )
    execution_failed = projector.project(
        _replace_approval(
            item,
            workflow_revision=2,
            last_validation_failure_artifact_id=failure.artifact_id,
        )
    )

    assert (running.validation_status, running.regression_status) == (
        "running",
        "not_started",
    )
    assert (execution_failed.validation_status, execution_failed.regression_status) == (
        "execution_failed",
        "not_started",
    )


def test_approval_payload_binding_uses_subject_kind_without_client_schema(
    engine: Engine,
    tmp_path,
) -> None:
    store = LocalObjectStore(
        tmp_path / "objects",
        store_id="local:test",
        clock=_clock(),
        cursor_signing_key=SIGNING_KEY,
    )
    payload = canonical_json({"patch_schema_version": "patch@2"}).encode("utf-8")
    stored = store.put_verified(payload)
    artifact = build_artifact_v2(
        kind="patch",
        version_tuple=VersionTuple(tool_version="content-read-test@1"),
        lineage=(),
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
    )
    item = _patch_approval(artifact.artifact_id, artifact.payload_hash)

    with Session(engine) as session, session.begin():
        object_bindings = SqlObjectBindingRepository(session, store, "local:test")
        object_bindings.bind_verified(stored.ref, stored.location, None)
        artifacts = SqlArtifactRepository(
            session,
            binding_repository=object_bindings,
            cursor_signer=_signer(),
            clock=_clock(),
        )
        artifacts.put(artifact)
        approvals = SqlApprovalRepository(session)
        approvals.insert_draft(item)
        binding = SqlApprovalPayloadBindingProvider(
            session,
            approvals=approvals,
            artifacts=artifacts,
        ).resolve(artifact.artifact_id)

    assert binding is not None
    assert binding.artifact_id == artifact.artifact_id
    assert binding.artifact_kind == "patch"
    assert binding.payload_schema_id == "patch@2"


@pytest.mark.parametrize(
    ("artifact_kind", "payload_schema_id", "accepted"),
    (
        ("regression_evidence", "regression-evidence@1", True),
        ("regression_evidence", "evidence-set@1", False),
        ("validation_evidence", "evidence-set@1", True),
        ("validation_evidence", "regression-evidence@1", False),
    ),
)
def test_payload_binding_uses_canonical_kind_schema_allowlist(
    engine: Engine,
    tmp_path,
    artifact_kind: str,
    payload_schema_id: str,
    accepted: bool,
) -> None:
    store = LocalObjectStore(
        tmp_path / "unbound-objects",
        store_id="local:test",
        clock=_clock(),
        cursor_signing_key=SIGNING_KEY,
    )
    payload = canonical_json({"payload_schema_version": payload_schema_id}).encode("utf-8")
    stored = store.put_verified(payload)
    artifact = build_artifact_v2(
        kind=artifact_kind,
        version_tuple=VersionTuple(tool_version="content-read-test@1"),
        lineage=(),
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        meta={"payload_schema_id": payload_schema_id},
    )

    with Session(engine) as session, session.begin():
        object_bindings = SqlObjectBindingRepository(session, store, "local:test")
        object_bindings.bind_verified(stored.ref, stored.location, None)
        artifacts = SqlArtifactRepository(
            session,
            binding_repository=object_bindings,
            cursor_signer=_signer(),
            clock=_clock(),
        )
        artifacts.put(artifact)
        approvals = SqlApprovalRepository(session)
        provider = SqlApprovalPayloadBindingProvider(
            session, approvals=approvals, artifacts=artifacts
        )
        statements: list[str] = []

        def capture_statement(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ) -> None:
            del connection, cursor, parameters, context, executemany
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            if accepted:
                binding = provider.resolve(artifact.artifact_id)
            else:
                with pytest.raises(IntegrityViolation, match="canonical kind registry"):
                    provider.resolve(artifact.artifact_id)
                binding = None
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

    assert not any("approval_items" in statement.lower() for statement in statements)
    if not accepted:
        return

    assert binding is not None
    assert binding.artifact_id == artifact.artifact_id
    assert binding.artifact_kind == artifact_kind
    assert binding.payload_schema_id == payload_schema_id


@pytest.mark.parametrize(
    ("payload_domain", "mismatch"),
    (("content", False), ("other", True)),
)
def test_trusted_workflow_schema_binds_constraint_payload_domain_without_metadata(
    engine: Engine,
    tmp_path,
    payload_domain: str,
    mismatch: bool,
) -> None:
    store = LocalObjectStore(
        tmp_path / f"constraint-domain-{payload_domain}",
        store_id="local:test",
        clock=_clock(),
        cursor_signing_key=SIGNING_KEY,
    )
    proposal = ConstraintProposalV1(
        revision=1,
        dsl_grammar_version="dsl@1",
        domain_scope=DomainScope(domain_ids=(payload_domain,)),
        constraints=(),
        source_bindings=(),
        produced_by="human",
        producer_run_id=None,
        rationale="domain binding test",
    )
    payload = canonical_json(proposal.model_dump(mode="json")).encode("utf-8")
    stored = store.put_verified(payload)
    artifact = build_artifact_v2(
        kind="constraint_proposal",
        version_tuple=VersionTuple(tool_version="content-read-test@1"),
        lineage=(),
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        meta={"domain_scope": DomainScope(domain_ids=("content",)).model_dump(mode="json")},
    )
    definitions = (
        DomainDefinitionV1(domain_id="content", display_name="Content", status="active"),
        DomainDefinitionV1(domain_id="other", display_name="Other", status="active"),
    )
    registry = DomainRegistryV1(
        registry_version="constraint-domain-test@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(
            "constraint-domain-test@1",
            definitions,
        ),
    )

    with Session(engine) as session, session.begin():
        object_bindings = SqlObjectBindingRepository(session, store, "local:test")
        object_bindings.bind_verified(stored.ref, stored.location, None)
        artifacts = SqlArtifactRepository(
            session,
            binding_repository=object_bindings,
            cursor_signer=_signer(),
            clock=_clock(),
        )
        artifacts.put(artifact)
        approvals = SqlApprovalRepository(session)
        approvals.insert_draft(_constraint_approval(artifact.artifact_id, artifact.payload_hash))
        payload_bindings = SqlApprovalPayloadBindingProvider(
            session,
            approvals=approvals,
            artifacts=artifacts,
        )
        authority = _ArtifactDomainAuthority(
            artifacts=artifacts,
            registry=registry,
            payloads=_ArtifactDomainPayloadReader(
                object_bindings=object_bindings,
                object_store=store,
            ),
            payload_bindings=payload_bindings,
        )
        if mismatch:
            with pytest.raises(IntegrityViolation, match="typed payload domains disagree"):
                authority.resolve(artifact)
        else:
            assert authority.resolve(artifact) == DomainScope(domain_ids=("content",))


def test_lineage_traversal_is_stable_bounded_and_snapshot_retained(
    engine: Engine,
) -> None:
    with Session(engine) as setup, setup.begin():
        repository = _artifacts(setup)
        for item in (
            _artifact("artifact:a"),
            _artifact("artifact:b", lineage=("artifact:a",)),
            _artifact("artifact:c", lineage=("artifact:a",)),
            _artifact("artifact:root", lineage=("artifact:c", "artifact:b")),
        ):
            repository.put(item)

    binding = _binding("artifact_lineage")
    with Session(engine) as first_session, first_session.begin():
        first = _artifact_pages(first_session).page_lineage(
            root_artifact_id="artifact:root",
            cursor=None,
            binding=binding,
            page_size=2,
        )
    assert [(item.artifact_id, item.depth) for item in first.items] == [
        ("artifact:b", 1),
        ("artifact:c", 1),
    ]
    assert first.next_cursor is not None

    with Session(engine) as concurrent, concurrent.begin():
        _artifacts(concurrent).put(_artifact("artifact:later"))

    with Session(engine) as second_session, second_session.begin():
        second = _artifact_pages(second_session).page_lineage(
            root_artifact_id="artifact:root",
            cursor=first.next_cursor,
            binding=binding,
            page_size=2,
        )

    assert second.read_snapshot_id == first.read_snapshot_id
    assert [(item.artifact_id, item.depth) for item in second.items] == [("artifact:a", 2)]
    assert second.next_cursor is None
