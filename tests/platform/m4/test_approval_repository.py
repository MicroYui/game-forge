from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRoutePolicyRefV1,
    DomainScope,
    Permission,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalRequirement,
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyProofBindingV1,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    ApprovalDecisionRow,
    ApprovalItemRow,
    ArtifactRow,
    Base,
    SubjectHeadRow,
)


SUBJECT_DIGEST_1 = "1" * 64
SUBJECT_DIGEST_2 = "2" * 64
TARGET_DIGEST = "3" * 64
EVIDENCE_ARTIFACT_ID = "artifact:evidence:1"
REGRESSION_EVIDENCE_ARTIFACT_ID = "artifact:regression:1"
AUTO_APPLY_PROOF_ARTIFACT_ID = "artifact:auto-apply-proof:1"


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'approvals.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _artifact(artifact_id: str, *, payload_hash: str) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=artifact_id,
        lineage_schema_version="lineage@2",
        kind="patch",
        version_tuple={},
        lineage=[],
        payload_hash=payload_hash,
        created_at="2026-07-14T10:00:00Z",
        meta={},
        object_ref={},
    )


def _seed_artifacts(session: Session) -> None:
    session.add_all(
        [
            _artifact("artifact:patch:1", payload_hash=SUBJECT_DIGEST_1),
            _artifact("artifact:patch:2", payload_hash=SUBJECT_DIGEST_2),
            _artifact(EVIDENCE_ARTIFACT_ID, payload_hash="8" * 64),
            _artifact(REGRESSION_EVIDENCE_ARTIFACT_ID, payload_hash="9" * 64),
        ]
    )
    session.flush()


def _domain_ref() -> DomainRegistryRefV1:
    return DomainRegistryRefV1(
        registry_version="domains@1",
        registry_digest="4" * 64,
    )


def _requirement() -> ApprovalRequirement:
    scope = DomainScope(domain_ids=("narrative",))
    return ApprovalRequirement(
        requirement_id="requirement:narrative",
        domain_scope=scope,
        required_permission=Permission(
            action="approval.decide",
            resource_kind="approval",
            domain_scope=scope,
        ),
        route_role="content_designer",
        min_approvals=1,
        assignee_principal_ids=("human:bob",),
        distinct_from_requirement_ids=(),
    )


def _item(
    *,
    approval_id: str = "approval:1",
    subject_revision: int = 1,
    subject_artifact_id: str = "artifact:patch:1",
    subject_digest: str = SUBJECT_DIGEST_1,
    supersedes_approval_id: str | None = None,
) -> ApprovalItem:
    domain_ref = _domain_ref()
    return ApprovalItem(
        approval_id=approval_id,
        subject_series_id="patch-series:1",
        subject_revision=subject_revision,
        subject_kind="patch",
        subject_artifact_id=subject_artifact_id,
        subject_digest=subject_digest,
        status="draft",
        workflow_revision=1,
        supersedes_approval_id=supersedes_approval_id,
        proposer=AuditActor(
            principal_id="human:alice",
            principal_kind="human",
        ),
        domain_scope=DomainScope(domain_ids=("narrative",)),
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
        requirements=(_requirement(),),
        decisions=(),
        regression_evidence_artifact_ids=(),
        target_binding=PatchTargetBindingV1(
            target_artifact_id="artifact:preview:1",
            target_snapshot_id="snapshot:preview:1",
            target_digest=TARGET_DIGEST,
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=4),
        ),
        created_at="2026-07-14T10:00:00Z",
    )


def _replace(item: ApprovalItem, **changes: object) -> ApprovalItem:
    return ApprovalItem.model_validate(
        {
            **item.model_dump(mode="json"),
            **changes,
        }
    )


def _validated_with_auto_apply_proof(validating: ApprovalItem) -> ApprovalItem:
    target = validating.target_binding
    assert isinstance(target, PatchTargetBindingV1)
    policy = AutoApplyPolicyRefV1(
        registry=AutoApplyPolicyRegistryRefV1(
            registry_version="auto-apply@1",
            registry_digest="8" * 64,
        ),
        policy_id="safe-structural-patch",
        policy_version="1",
        policy_digest="9" * 64,
    )
    return _replace(
        validating,
        status="validated",
        workflow_revision=validating.workflow_revision + 1,
        active_validation_run_id=None,
        evidence_set_artifact_id=EVIDENCE_ARTIFACT_ID,
        regression_evidence_artifact_ids=(REGRESSION_EVIDENCE_ARTIFACT_ID,),
        auto_apply_proof=AutoApplyProofBindingV1(
            proof_artifact_id=AUTO_APPLY_PROOF_ARTIFACT_ID,
            policy=policy,
            subject_digest=validating.subject_digest,
            target_digest=target.target_digest,
            expected_ref=target.expected_ref,
            validation_evidence_artifact_id=EVIDENCE_ARTIFACT_ID,
        ),
    )


def _decision(
    *,
    decision_id: str = "decision:1",
    expected_workflow_revision: int = 4,
    reason_code: str = "reviewed",
) -> ApprovalDecision:
    return ApprovalDecision(
        decision_id=decision_id,
        requirement_ids=("requirement:narrative",),
        decision="approve",
        actor=AuditActor(principal_id="human:bob", principal_kind="human"),
        expected_workflow_revision=expected_workflow_revision,
        reason_code=reason_code,
        occurred_at="2026-07-14T10:05:00Z",
    )


def _advance_to_pending(
    repository: SqlApprovalRepository,
    item: ApprovalItem,
) -> ApprovalItem:
    validating = _replace(
        item,
        status="validating",
        workflow_revision=2,
        active_validation_run_id="run:validation:1",
    )
    validated = _replace(
        validating,
        status="validated",
        workflow_revision=3,
        active_validation_run_id=None,
        evidence_set_artifact_id=EVIDENCE_ARTIFACT_ID,
    )
    pending = _replace(
        validated,
        status="pending_approval",
        workflow_revision=4,
        submitted_at="2026-07-14T10:04:00Z",
    )
    repository.compare_and_set(item.approval_id, 1, validating)
    repository.compare_and_set_validation_completion(item.approval_id, 2, validated)
    repository.compare_and_set(item.approval_id, 3, pending)
    return pending


def test_insert_draft_is_exactly_idempotent_and_hydrates_current_wire(
    engine: Engine,
) -> None:
    item = _item()
    with Session(engine) as session, session.begin():
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        assert repository.insert_draft(item) == item
        assert repository.insert_draft(item) == item
        assert repository.get(item.approval_id) == item

    with Session(engine) as session:
        repository = SqlApprovalRepository(session)
        assert repository.get(item.approval_id) == item


def test_insert_draft_rejects_same_id_or_subject_revision_with_different_wire(
    engine: Engine,
) -> None:
    item = _item()
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        session.commit()

        changed = _replace(item, created_at="2026-07-14T10:00:01Z")
        with pytest.raises(IntegrityViolation, match="different immutable content"):
            repository.insert_draft(changed)
        session.rollback()

        collision = _replace(item, approval_id="approval:other")
        with pytest.raises(Conflict, match="subject revision"):
            repository.insert_draft(collision)
        session.rollback()


def test_insert_draft_requires_exact_draft_shape_and_bound_subject_artifact(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        noninitial = _replace(_item(), workflow_revision=2)
        with pytest.raises(IntegrityViolation, match="draft.*workflow_revision=1"):
            repository.insert_draft(noninitial)

        missing_artifact = _replace(
            _item(),
            approval_id="approval:missing",
            subject_artifact_id="artifact:missing",
        )
        with pytest.raises(IntegrityViolation, match="subject Artifact"):
            repository.insert_draft(missing_artifact)

        wrong_digest = _replace(_item(), subject_digest="f" * 64)
        with pytest.raises(IntegrityViolation, match="subject digest"):
            repository.insert_draft(wrong_digest)


def test_compare_and_set_persists_service_prevalidated_state_and_is_idempotent(
    engine: Engine,
) -> None:
    item = _item()
    validating = _replace(
        item,
        status="validating",
        workflow_revision=2,
        active_validation_run_id="run:validation:1",
    )
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        assert repository.compare_and_set(item.approval_id, 1, validating) == validating
        assert repository.compare_and_set(item.approval_id, 1, validating) == validating
        session.commit()

    with Session(engine) as session:
        assert SqlApprovalRepository(session).get(item.approval_id) == validating


def test_compare_and_set_rejects_stale_revision_and_immutable_field_change(
    engine: Engine,
) -> None:
    item = _item()
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        session.commit()

        replacement = _replace(item, workflow_revision=3)
        with pytest.raises(Conflict, match="workflow revision"):
            repository.compare_and_set(item.approval_id, 2, replacement)
        session.rollback()

        changed_proposer = _replace(
            item,
            workflow_revision=2,
            proposer={"principal_id": "human:carol", "principal_kind": "human"},
        )
        with pytest.raises(IntegrityViolation, match="immutable approval fields"):
            repository.compare_and_set(item.approval_id, 1, changed_proposer)
        session.rollback()


def test_decision_and_workflow_cas_are_one_repository_primitive_and_retry_exactly(
    engine: Engine,
) -> None:
    item = _item()
    decision = _decision()
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        pending = _advance_to_pending(repository, item)
        replacement = _replace(
            pending,
            status="approved",
            workflow_revision=5,
            decisions=(decision.model_dump(mode="json"),),
            decided_at=decision.occurred_at,
        )

        assert (
            repository.append_decision_and_compare_and_set(
                item.approval_id,
                4,
                decision,
                replacement,
            )
            == replacement
        )
        assert (
            repository.append_decision_and_compare_and_set(
                item.approval_id,
                4,
                decision,
                replacement,
            )
            == replacement
        )
        assert repository.get_decision(decision.decision_id) == decision
        session.commit()

    with Session(engine) as session:
        assert SqlApprovalRepository(session).get(item.approval_id) == replacement


def test_repository_rejects_replacements_that_it_could_not_read_back(
    engine: Engine,
) -> None:
    item = _item()
    decision = _decision()
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        pending = _advance_to_pending(repository, item)

        approved_without_decision = _replace(
            pending,
            status="approved",
            workflow_revision=5,
            decided_at=decision.occurred_at,
        )
        with pytest.raises(IntegrityViolation, match="decision history"):
            repository.compare_and_set(
                item.approval_id,
                4,
                approved_without_decision,
            )

        decision_in_draft = _replace(
            pending,
            status="draft",
            workflow_revision=5,
            decisions=(decision.model_dump(mode="json"),),
        )
        with pytest.raises(IntegrityViolation, match="decision history"):
            repository.append_decision_and_compare_and_set(
                item.approval_id,
                4,
                decision,
                decision_in_draft,
            )

        assert repository.get(item.approval_id) == pending
        assert repository.get_decision(decision.decision_id) is None


def test_decision_id_collision_with_different_payload_fails_closed(
    engine: Engine,
) -> None:
    item = _item()
    decision = _decision()
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        pending = _advance_to_pending(repository, item)
        replacement = _replace(
            pending,
            status="approved",
            workflow_revision=5,
            decisions=(decision.model_dump(mode="json"),),
            decided_at=decision.occurred_at,
        )
        repository.append_decision_and_compare_and_set(
            item.approval_id,
            4,
            decision,
            replacement,
        )
        session.commit()

        changed = _decision(
            expected_workflow_revision=5,
            reason_code="different",
        )
        changed_replacement = _replace(
            replacement,
            workflow_revision=6,
            decisions=(changed.model_dump(mode="json"),),
        )
        with pytest.raises(Conflict, match="decision id"):
            repository.append_decision_and_compare_and_set(
                item.approval_id,
                5,
                changed,
                changed_replacement,
            )
        session.rollback()
        assert repository.get(item.approval_id) == replacement


def test_repository_never_commits_decision_or_workflow_state_outside_owning_uow(
    engine: Engine,
) -> None:
    item = _item()
    decision = _decision()
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        pending = _advance_to_pending(repository, item)
        session.commit()

        replacement = _replace(
            pending,
            status="approved",
            workflow_revision=5,
            decisions=(decision.model_dump(mode="json"),),
            decided_at=decision.occurred_at,
        )

        repository.append_decision_and_compare_and_set(
            item.approval_id,
            4,
            decision,
            replacement,
        )
        session.rollback()

        assert repository.get(item.approval_id) == pending
        assert repository.get_decision(decision.decision_id) is None


def test_subject_head_create_update_current_and_aba_protection(engine: Engine) -> None:
    first_item = _item()
    second_item = _item(
        approval_id="approval:2",
        subject_revision=2,
        subject_artifact_id="artifact:patch:2",
        subject_digest=SUBJECT_DIGEST_2,
        supersedes_approval_id=first_item.approval_id,
    )
    third_item = _item(
        approval_id="approval:3",
        subject_revision=3,
        subject_artifact_id="artifact:patch:1",
        subject_digest=SUBJECT_DIGEST_1,
        supersedes_approval_id=second_item.approval_id,
    )
    first_head = SubjectHead(
        subject_series_id=first_item.subject_series_id,
        current_subject_artifact_id=first_item.subject_artifact_id,
        current_approval_id=first_item.approval_id,
        revision=1,
    )
    second_head = SubjectHead(
        subject_series_id=second_item.subject_series_id,
        current_subject_artifact_id=second_item.subject_artifact_id,
        current_approval_id=second_item.approval_id,
        revision=2,
    )
    third_head = SubjectHead(
        subject_series_id=third_item.subject_series_id,
        current_subject_artifact_id=third_item.subject_artifact_id,
        current_approval_id=third_item.approval_id,
        revision=3,
    )

    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(first_item)
        repository.insert_draft(second_item)
        repository.insert_draft(third_item)
        assert (
            repository.compare_and_set_subject_head(
                first_head.subject_series_id,
                None,
                first_head,
            )
            == first_head
        )
        assert (
            repository.compare_and_set_subject_head(
                first_head.subject_series_id,
                None,
                first_head,
            )
            == first_head
        )
        assert repository.current(first_head.subject_series_id) == (first_head, first_item)

        assert (
            repository.compare_and_set_subject_head(
                first_head.subject_series_id,
                first_head,
                second_head,
            )
            == second_head
        )
        assert (
            repository.compare_and_set_subject_head(
                first_head.subject_series_id,
                first_head,
                second_head,
            )
            == second_head
        )
        assert (
            repository.compare_and_set_subject_head(
                first_head.subject_series_id,
                second_head,
                third_head,
            )
            == third_head
        )
        with pytest.raises(Conflict, match="SubjectHead compare-and-set"):
            repository.compare_and_set_subject_head(
                first_head.subject_series_id,
                first_head,
                second_head,
            )
        session.commit()

    with Session(engine) as session:
        repository = SqlApprovalRepository(session)
        assert repository.get_subject_head(first_head.subject_series_id) == third_head
        assert repository.current(first_head.subject_series_id) == (third_head, third_item)


@pytest.mark.parametrize(
    "serialization",
    ["validation_first", "supersede_first"],
)
def test_validation_completion_and_supersede_serialize_across_two_connections(
    engine: Engine,
    serialization: str,
) -> None:
    old_item = _item()
    new_item = _item(
        approval_id="approval:2",
        subject_revision=2,
        subject_artifact_id="artifact:patch:2",
        subject_digest=SUBJECT_DIGEST_2,
        supersedes_approval_id=old_item.approval_id,
    )
    old_head = SubjectHead(
        subject_series_id=old_item.subject_series_id,
        current_subject_artifact_id=old_item.subject_artifact_id,
        current_approval_id=old_item.approval_id,
        revision=1,
    )
    new_head = SubjectHead(
        subject_series_id=new_item.subject_series_id,
        current_subject_artifact_id=new_item.subject_artifact_id,
        current_approval_id=new_item.approval_id,
        revision=2,
    )
    validating = _replace(
        old_item,
        status="validating",
        workflow_revision=2,
        active_validation_run_id="run:validation:1",
    )
    validated = _validated_with_auto_apply_proof(validating)

    with Session(engine) as setup, setup.begin():
        _seed_artifacts(setup)
        repository = SqlApprovalRepository(setup)
        repository.insert_draft(old_item)
        repository.compare_and_set(old_item.approval_id, 1, validating)
        repository.compare_and_set_subject_head(old_item.subject_series_id, None, old_head)

    with engine.connect() as validation_connection, engine.connect() as supersede_connection:
        with (
            Session(validation_connection, expire_on_commit=False) as validation_session,
            Session(supersede_connection, expire_on_commit=False) as supersede_session,
        ):
            if serialization == "validation_first":
                with validation_session.begin():
                    validation_repository = SqlApprovalRepository(validation_session)
                    validation_repository.compare_and_set_validation_completion(
                        old_item.approval_id,
                        2,
                        validated,
                    )
                supersede_source = validated
            else:
                supersede_source = validating

            superseded = _replace(
                supersede_source,
                status="superseded",
                workflow_revision=supersede_source.workflow_revision + 1,
                active_validation_run_id=None,
            )
            with supersede_session.begin():
                supersede_repository = SqlApprovalRepository(supersede_session)
                supersede_repository.compare_and_set(
                    old_item.approval_id,
                    supersede_source.workflow_revision,
                    superseded,
                )
                supersede_repository.insert_draft(new_item)
                supersede_repository.compare_and_set_subject_head(
                    old_item.subject_series_id,
                    old_head,
                    new_head,
                )

            if serialization == "supersede_first":
                with pytest.raises(Conflict, match="workflow revision"):
                    with validation_session.begin():
                        SqlApprovalRepository(
                            validation_session
                        ).compare_and_set_validation_completion(
                            old_item.approval_id,
                            2,
                            validated,
                        )

    with Session(engine) as verification:
        repository = SqlApprovalRepository(verification)
        retained_old = repository.get(old_item.approval_id)
        retained_new = repository.get(new_item.approval_id)

        assert retained_old == superseded
        assert retained_new == new_item
        assert retained_new.evidence_set_artifact_id is None
        assert retained_new.regression_evidence_artifact_ids == ()
        assert retained_new.auto_apply_proof is None
        assert repository.current(old_item.subject_series_id) == (new_head, retained_new)
        if serialization == "validation_first":
            assert retained_old.evidence_set_artifact_id == EVIDENCE_ARTIFACT_ID
            assert retained_old.auto_apply_proof == validated.auto_apply_proof
        else:
            assert retained_old.evidence_set_artifact_id is None
            assert retained_old.auto_apply_proof is None


def test_subject_head_rejects_approval_that_does_not_bind_replacement(engine: Engine) -> None:
    item = _item()
    mismatched = SubjectHead(
        subject_series_id=item.subject_series_id,
        current_subject_artifact_id="artifact:patch:2",
        current_approval_id=item.approval_id,
        revision=1,
    )
    with Session(engine) as session, session.begin():
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        with pytest.raises(IntegrityViolation, match="does not bind"):
            repository.compare_and_set_subject_head(
                item.subject_series_id,
                None,
                mismatched,
            )


def test_subject_head_rejects_a_revision_that_differs_from_the_subject_revision(
    engine: Engine,
) -> None:
    item = _item()
    forged = SubjectHead(
        subject_series_id=item.subject_series_id,
        current_subject_artifact_id=item.subject_artifact_id,
        current_approval_id=item.approval_id,
        revision=2,
    )
    with Session(engine) as session, session.begin():
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        with pytest.raises(IntegrityViolation, match="subject revision"):
            repository.compare_and_set_subject_head(
                item.subject_series_id,
                None,
                forged,
            )


def test_new_subject_head_rejects_a_noninitial_subject_revision(
    engine: Engine,
) -> None:
    second_revision = _item(
        approval_id="approval:2",
        subject_revision=2,
    )
    head = SubjectHead(
        subject_series_id=second_revision.subject_series_id,
        current_subject_artifact_id=second_revision.subject_artifact_id,
        current_approval_id=second_revision.approval_id,
        revision=1,
    )
    with Session(engine) as session, session.begin():
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(second_revision)
        with pytest.raises(IntegrityViolation, match="subject revision"):
            repository.compare_and_set_subject_head(
                second_revision.subject_series_id,
                None,
                head,
            )


def test_subject_head_exact_retry_still_validates_the_expected_predecessor(
    engine: Engine,
) -> None:
    first_item = _item()
    second_item = _item(
        approval_id="approval:2",
        subject_revision=2,
        subject_artifact_id="artifact:patch:2",
        subject_digest=SUBJECT_DIGEST_2,
        supersedes_approval_id=first_item.approval_id,
    )
    first_head = SubjectHead(
        subject_series_id=first_item.subject_series_id,
        current_subject_artifact_id=first_item.subject_artifact_id,
        current_approval_id=first_item.approval_id,
        revision=1,
    )
    second_head = SubjectHead(
        subject_series_id=second_item.subject_series_id,
        current_subject_artifact_id=second_item.subject_artifact_id,
        current_approval_id=second_item.approval_id,
        revision=2,
    )
    forged_expected = SubjectHead(
        subject_series_id=first_item.subject_series_id,
        current_subject_artifact_id="artifact:missing",
        current_approval_id="approval:missing",
        revision=1,
    )

    with Session(engine) as session, session.begin():
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(first_item)
        repository.insert_draft(second_item)
        repository.compare_and_set_subject_head(first_head.subject_series_id, None, first_head)
        repository.compare_and_set_subject_head(
            first_head.subject_series_id,
            first_head,
            second_head,
        )

        with pytest.raises(IntegrityViolation, match="missing ApprovalItem"):
            repository.compare_and_set_subject_head(
                first_head.subject_series_id,
                forged_expected,
                second_head,
            )


def test_subject_head_cannot_change_subject_kind_within_a_revision_series(
    engine: Engine,
) -> None:
    first_item = _item()
    second_item = _replace(
        _item(
            approval_id="approval:2",
            subject_revision=2,
            subject_artifact_id="artifact:patch:2",
            subject_digest=SUBJECT_DIGEST_2,
            supersedes_approval_id=first_item.approval_id,
        ),
        subject_kind="constraint_proposal",
        target_binding=None,
    )
    first_head = SubjectHead(
        subject_series_id=first_item.subject_series_id,
        current_subject_artifact_id=first_item.subject_artifact_id,
        current_approval_id=first_item.approval_id,
        revision=1,
    )
    second_head = SubjectHead(
        subject_series_id=second_item.subject_series_id,
        current_subject_artifact_id=second_item.subject_artifact_id,
        current_approval_id=second_item.approval_id,
        revision=2,
    )
    with Session(engine) as session, session.begin():
        _seed_artifacts(session)
        artifact = session.get(ArtifactRow, "artifact:patch:2")
        assert artifact is not None
        artifact.kind = "constraint_proposal"
        repository = SqlApprovalRepository(session)
        repository.insert_draft(first_item)
        repository.insert_draft(second_item)
        repository.compare_and_set_subject_head(first_head.subject_series_id, None, first_head)
        with pytest.raises(IntegrityViolation, match="does not supersede"):
            repository.compare_and_set_subject_head(
                first_head.subject_series_id,
                first_head,
                second_head,
            )


def test_get_subject_head_rejects_a_persisted_revision_chain_mismatch(
    engine: Engine,
) -> None:
    item = _item()
    head = SubjectHead(
        subject_series_id=item.subject_series_id,
        current_subject_artifact_id=item.subject_artifact_id,
        current_approval_id=item.approval_id,
        revision=1,
    )
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        repository.compare_and_set_subject_head(item.subject_series_id, None, head)
        session.commit()

        session.execute(
            update(SubjectHeadRow)
            .where(SubjectHeadRow.subject_series_id == item.subject_series_id)
            .values(revision=2)
        )
        session.commit()
        session.expire_all()

        with pytest.raises(IntegrityViolation, match="subject revision"):
            repository.get_subject_head(item.subject_series_id)


def test_get_decision_rejects_a_row_not_absorbed_by_workflow_cas(engine: Engine) -> None:
    item = _item()
    decision = _decision(expected_workflow_revision=1)
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        session.add(
            ApprovalDecisionRow(
                approval_id=item.approval_id,
                **decision.model_dump(mode="json"),
            )
        )
        session.commit()

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="stored ApprovalItem is invalid"):
            SqlApprovalRepository(session).get_decision(decision.decision_id)


def test_get_decision_rejects_a_side_insert_after_an_unrelated_workflow_cas(
    engine: Engine,
) -> None:
    item = _item()
    validating = _replace(
        item,
        status="validating",
        workflow_revision=2,
        active_validation_run_id="run:validation:1",
    )
    decision = _decision(expected_workflow_revision=1)
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        repository.compare_and_set(item.approval_id, 1, validating)
        session.add(
            ApprovalDecisionRow(
                approval_id=item.approval_id,
                **decision.model_dump(mode="json"),
            )
        )
        session.commit()

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="decision history"):
            SqlApprovalRepository(session).get_decision(decision.decision_id)


@pytest.mark.parametrize("corruption", ["approval", "decision", "head"])
def test_reads_fail_closed_on_corrupt_persisted_workflow_rows(
    engine: Engine,
    corruption: str,
) -> None:
    item = _item()
    decision = _decision()
    head = SubjectHead(
        subject_series_id=item.subject_series_id,
        current_subject_artifact_id=item.subject_artifact_id,
        current_approval_id=item.approval_id,
        revision=1,
    )
    with Session(engine) as session:
        _seed_artifacts(session)
        repository = SqlApprovalRepository(session)
        repository.insert_draft(item)
        pending = _advance_to_pending(repository, item)
        replacement = _replace(
            pending,
            status="approved",
            workflow_revision=5,
            decisions=(decision.model_dump(mode="json"),),
            decided_at=decision.occurred_at,
        )
        repository.append_decision_and_compare_and_set(
            item.approval_id,
            4,
            decision,
            replacement,
        )
        repository.compare_and_set_subject_head(item.subject_series_id, None, head)
        session.commit()

        if corruption == "approval":
            session.execute(
                update(ApprovalItemRow)
                .where(ApprovalItemRow.approval_id == item.approval_id)
                .values(role_policy_digest="not-a-digest")
            )
        elif corruption == "decision":
            session.execute(
                update(ApprovalDecisionRow)
                .where(ApprovalDecisionRow.decision_id == decision.decision_id)
                .values(actor={"principal_id": "service:worker", "principal_kind": "service"})
            )
        else:
            session.execute(
                update(SubjectHeadRow)
                .where(SubjectHeadRow.subject_series_id == item.subject_series_id)
                .values(current_subject_digest="f" * 64)
            )
        session.commit()
        session.expire_all()

        with pytest.raises(IntegrityViolation):
            if corruption == "head":
                repository.get_subject_head(item.subject_series_id)
            else:
                repository.get(item.approval_id)
