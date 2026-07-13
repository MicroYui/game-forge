from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.workflow import (
    ConstraintCompileEvidenceV1,
    ConstraintTargetBindingV1,
    EvidenceSet,
    RollbackTargetBindingV1,
)
from gameforge.platform.approvals.validation import PreparedValidationCompletion
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import ArtifactRow, Base
from tests.platform.m4.test_approval_repository import (
    EVIDENCE_ARTIFACT_ID,
    _item as repository_item,
    _replace as repository_replace,
    _seed_artifacts,
)
from tests.platform.m4.validation_testkit import (
    artifact,
    auto_patch_prepared,
    constraint_prepared,
    context,
    harness,
    object_binding,
    patch_prepared,
    replace_item,
    rollback_prepared,
)


def _patch_with_regression_and_companion_publications():
    prepared, item, head, retained, resolution = patch_prepared("passed")
    evidence = prepared.evidence_set
    assert evidence is not None and item.target_binding is not None
    regression = artifact(
        "regression_evidence",
        "regression-output",
        lineage=(item.subject_artifact_id,),
        ir_snapshot_id=item.target_binding.target_snapshot_id,
    )
    companion = artifact(
        "playtest_trace",
        "playtest-output",
        lineage=(item.subject_artifact_id,),
        ir_snapshot_id=item.target_binding.target_snapshot_id,
    )
    updated_evidence = evidence.__class__.model_validate(
        {
            **evidence.model_dump(mode="python"),
            "supporting_artifact_ids": (
                *evidence.supporting_artifact_ids,
                regression.artifact_id,
                companion.artifact_id,
            ),
        }
    )
    evidence_artifact = artifact(
        "validation_evidence",
        "evidence-with-regression-and-playtest",
        payload=updated_evidence,
        lineage=(
            item.subject_artifact_id,
            item.target_binding.target_artifact_id,
            *updated_evidence.supporting_artifact_ids,
        ),
        ir_snapshot_id=item.target_binding.target_snapshot_id,
    )
    updated = PreparedValidationCompletion.model_validate(
        {
            **prepared.model_dump(mode="python"),
            "evidence_set": updated_evidence,
            "evidence_set_artifact": evidence_artifact,
            "regression_artifacts": (regression,),
            "companion_artifacts": (companion,),
            "object_bindings": tuple(
                object_binding(value)
                for value in (regression, companion, evidence_artifact)
            ),
        }
    )
    return (
        (updated, item, head, retained, resolution),
        regression,
        companion,
        evidence_artifact,
    )


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_code"),
    [
        ("passed", "validated", "patch_validation_passed"),
        ("failed", "validation_failed", "patch_validation_failed"),
        ("unproven", "validation_failed", "patch_validation_unproven"),
    ],
)
def test_patch_deterministic_outcomes_publish_exact_evidence(
    outcome: str,
    expected_status: str,
    expected_code: str,
) -> None:
    fixture = patch_prepared(outcome)
    prepared = fixture[0]
    test = harness(fixture)

    result = test.service.complete(prepared=prepared, context=context())

    assert result.disposition == "completed"
    assert result.outcome_code == expected_code
    assert result.approval_item.status == expected_status
    assert result.approval_item.workflow_revision == 3
    assert result.approval_item.active_validation_run_id is None
    assert result.approval_item.evidence_set_artifact_id == (
        prepared.evidence_set_artifact.artifact_id  # type: ignore[union-attr]
    )
    assert result.approval_item.target_binding == fixture[1].target_binding
    assert test.state.terminals == [(expected_code, result.published_artifact_ids)]
    assert test.state.audits == ["approval.validation_completed"]


@pytest.mark.parametrize("outcome", ["execution_failed", "cancelled", "timed_out"])
def test_execution_terminals_restore_current_item_to_draft(outcome: str) -> None:
    fixture = patch_prepared(outcome)
    prepared = fixture[0]
    test = harness(fixture)

    result = test.service.complete(prepared=prepared, context=context())

    assert result.outcome_code == outcome
    assert result.approval_item.status == "draft"
    assert result.approval_item.workflow_revision == 3
    assert result.approval_item.active_validation_run_id is None
    assert result.approval_item.evidence_set_artifact_id is None
    assert result.approval_item.last_validation_failure_artifact_id == (
        f"artifact:run-failure:{outcome}"
    )
    assert result.published_artifact_ids == ()


def test_patch_evidence_must_equal_draft_time_target_binding() -> None:
    fixture = patch_prepared("passed")
    prepared, item = fixture[:2]
    test = harness(fixture)
    assert item.target_binding is not None
    changed_target = item.target_binding.model_copy(update={"target_digest": "f" * 64})
    test.state.approvals[item.approval_id] = replace_item(
        item,
        target_binding=changed_target.model_dump(mode="json"),
    )

    with pytest.raises(IntegrityViolation, match="target differs"):
        test.service.complete(prepared=prepared, context=context())

    assert test.state.terminals == []
    assert prepared.evidence_set_artifact.artifact_id not in test.state.artifacts  # type: ignore[union-attr]


@pytest.mark.parametrize("outcome", ["passed", "failed", "unproven"])
def test_constraint_candidate_is_bound_once_for_every_completed_compile(
    outcome: str,
) -> None:
    fixture = constraint_prepared(outcome=outcome, candidate_exists=True)
    prepared, item = fixture[:2]
    test = harness(fixture)

    result = test.service.complete(prepared=prepared, context=context())

    assert result.approval_item.status == (
        "validated" if outcome == "passed" else "validation_failed"
    )
    assert result.approval_item.target_binding == prepared.evidence_set.target_binding  # type: ignore[union-attr]
    assert result.approval_item.target_binding is not None
    assert prepared.constraint_candidate_artifact.artifact_id in test.state.artifacts  # type: ignore[union-attr]
    assert prepared.constraint_compile_artifact.artifact_id in test.state.artifacts  # type: ignore[union-attr]


def test_constraint_failure_without_candidate_keeps_binding_null() -> None:
    fixture = constraint_prepared(outcome="failed", candidate_exists=False)
    prepared = fixture[0]
    test = harness(fixture)

    result = test.service.complete(prepared=prepared, context=context())

    assert result.outcome_code == "constraint_validation_failed_without_candidate"
    assert result.approval_item.status == "validation_failed"
    assert result.approval_item.target_binding is None
    assert prepared.constraint_compile_artifact.artifact_id in test.state.artifacts  # type: ignore[union-attr]


def test_constraint_pass_without_candidate_is_rejected_before_publication() -> None:
    prepared, item, head, retained, resolution = constraint_prepared(
        outcome="failed",
        candidate_exists=False,
    )
    evidence = prepared.evidence_set.model_copy(  # type: ignore[union-attr]
        update={
            "overall_status": "passed",
            "requirements": tuple(
                requirement.model_copy(
                    update={
                        "status": "passed",
                        "evidence_artifact_id": prepared.constraint_compile_artifact.artifact_id,
                        "reason_code": None,
                    }
                )
                for requirement in prepared.evidence_set.requirements  # type: ignore[union-attr]
            ),
        }
    )
    invalid = prepared.model_copy(
        update={
            "outcome": "passed",
            "outcome_code": "constraint_validated",
            "evidence_set": evidence,
        }
    )
    test = harness((invalid, item, head, retained, resolution))

    with pytest.raises(IntegrityViolation, match="without candidate cannot bind/pass"):
        test.service.complete(prepared=invalid, context=context())


def test_constraint_compile_engines_must_equal_frozen_run_payload() -> None:
    fixture = constraint_prepared(outcome="passed", candidate_exists=True)
    prepared = fixture[0]
    compile_evidence = prepared.constraint_compile_evidence
    assert compile_evidence is not None
    stages = tuple(
        stage.model_copy(update={"engine_version": "99"})
        if stage.stage == "differential" and stage.engine_id == "z3"
        else stage
        for stage in compile_evidence.stages
    )
    changed_compile = ConstraintCompileEvidenceV1.model_validate(
        {**compile_evidence.model_dump(mode="python"), "stages": stages}
    )
    changed = prepared.model_copy(
        update={"constraint_compile_evidence": changed_compile}
    )
    test = harness((changed, *fixture[1:]))

    with pytest.raises(IntegrityViolation, match="differential engines"):
        test.service.complete(prepared=changed, context=context())

    assert test.state.terminals == []


@pytest.mark.parametrize("stage_status", ["failed", "unproven"])
def test_constraint_failed_or_unproven_stage_cannot_publish_passed_evidence(
    stage_status: str,
) -> None:
    fixture = constraint_prepared(outcome="passed", candidate_exists=True)
    prepared, item, head, retained, resolution = fixture
    compile_evidence = prepared.constraint_compile_evidence
    assert compile_evidence is not None
    changed_compile = ConstraintCompileEvidenceV1.model_validate(
        {
            **compile_evidence.model_dump(mode="python"),
            "stages": tuple(
                stage.model_copy(
                    update={"status": stage_status, "reason_code": "engine_disagreed"}
                )
                if stage.stage == "differential" and stage.engine_id == "z3"
                else stage
                for stage in compile_evidence.stages
            ),
            "overall_status": stage_status,
        }
    )
    compile_artifact = artifact(
        "validation_evidence",
        f"compile-{stage_status}",
        payload=changed_compile,
        lineage=prepared.constraint_compile_artifact.lineage,  # type: ignore[union-attr]
    )
    evidence = prepared.evidence_set
    assert evidence is not None
    changed_evidence = EvidenceSet.model_validate(
        {
            **evidence.model_dump(mode="python"),
            "supporting_artifact_ids": (compile_artifact.artifact_id,),
            "requirements": tuple(
                requirement.model_copy(
                    update={"evidence_artifact_id": compile_artifact.artifact_id}
                )
                for requirement in evidence.requirements
            ),
        }
    )
    evidence_artifact = artifact(
        "validation_evidence",
        f"passed-evidence-over-{stage_status}-compile",
        payload=changed_evidence,
        lineage=(
            item.subject_artifact_id,
            prepared.constraint_candidate_artifact.artifact_id,  # type: ignore[union-attr]
            compile_artifact.artifact_id,
        ),
    )
    changed = PreparedValidationCompletion.model_validate(
        {
            **prepared.model_dump(mode="python"),
            "constraint_compile_evidence": changed_compile,
            "constraint_compile_artifact": compile_artifact,
            "evidence_set": changed_evidence,
            "evidence_set_artifact": evidence_artifact,
            "object_bindings": tuple(
                object_binding(value)
                for value in (
                    prepared.constraint_candidate_artifact,
                    compile_artifact,
                    evidence_artifact,
                )
            ),
        }
    )
    test = harness((changed, item, head, retained, resolution))

    with pytest.raises(IntegrityViolation, match="compile evidence and EvidenceSet"):
        test.service.complete(prepared=changed, context=context())

    assert test.state.terminals == []


def test_rollback_validation_requires_full_resolved_profile_binding() -> None:
    fixture = rollback_prepared()
    prepared, item, head, retained, resolution = fixture
    accepted = harness(fixture).service.complete(prepared=prepared, context=context())
    assert accepted.approval_item.status == "validated"

    target = item.target_binding
    assert isinstance(target, RollbackTargetBindingV1)
    stale_target = target.model_copy(
        update={
            "rollback_profile_binding": target.rollback_profile_binding.model_copy(
                update={"profile_payload_hash": "f" * 64}
            )
        }
    )
    stale_item = replace_item(item, target_binding=stale_target.model_dump(mode="json"))
    evidence = prepared.evidence_set
    assert evidence is not None
    stale_evidence = evidence.model_copy(update={"target_binding": stale_target})
    evidence_artifact = artifact(
        "validation_evidence",
        "rollback-stale-profile-evidence",
        payload=stale_evidence,
        lineage=prepared.evidence_set_artifact.lineage,  # type: ignore[union-attr]
        ir_snapshot_id=stale_target.target_snapshot_id,
    )
    stale_prepared = PreparedValidationCompletion.model_validate(
        {
            **prepared.model_dump(mode="python"),
            "evidence_set": stale_evidence,
            "evidence_set_artifact": evidence_artifact,
            "object_bindings": (object_binding(evidence_artifact),),
        }
    )
    test = harness((stale_prepared, stale_item, head, retained, resolution))

    with pytest.raises(IntegrityViolation, match="exact target"):
        test.service.complete(prepared=stale_prepared, context=context())

    assert test.state.terminals == []


def test_rollback_validation_rejects_history_revision_drift_from_request() -> None:
    fixture = rollback_prepared()
    test = harness(fixture)
    prepared = fixture[0]
    payload = prepared.execution.payload
    stale_payload = payload.model_copy(
        update={"target_history_revision": payload.target_history_revision + 1}
    )
    changed = prepared.model_copy(
        update={
            "execution": prepared.execution.model_copy(
                update={"payload": stale_payload}
            )
        }
    )
    test.runs.expected = changed.execution

    with pytest.raises(IntegrityViolation, match="history revision"):
        test.service.complete(prepared=changed, context=context())

    assert test.state.terminals == []


@pytest.mark.parametrize(
    "stale",
    [
        "workflow",
        "head",
        "active_run",
        "run_id",
        "expected_run_revision",
        "lease_id",
        "fencing_token",
    ],
)
def test_stale_subject_run_lease_or_fencing_binding_fails_closed(stale: str) -> None:
    fixture = patch_prepared("passed")
    prepared, item, head = fixture[:3]
    test = harness(fixture)
    call_context = context()

    if stale == "workflow":
        test.state.approvals[item.approval_id] = replace_item(
            item,
            workflow_revision=3,
        )
    elif stale == "head":
        test.state.heads[item.subject_series_id] = head.model_copy(update={"revision": 2})
    elif stale == "active_run":
        test.state.approvals[item.approval_id] = replace_item(
            item,
            active_validation_run_id="run:replacement",
        )
    else:
        changes: dict[str, object] = {
            "run_id": "run:replacement",
            "expected_run_revision": 99,
            "lease_id": "lease:replacement",
            "fencing_token": 99,
        }
        expected = prepared.execution.model_copy(update={stale: changes[stale]})
        if stale == "run_id":
            expected = expected.model_copy(
                update={
                    "payload": expected.payload.model_copy(
                        update={
                            "subject": expected.payload.subject.model_copy(
                                update={"active_validation_run_id": "run:replacement"}
                            )
                        }
                    )
                }
            )
        test.runs.expected = expected

    with pytest.raises((Conflict, IntegrityViolation)):
        test.service.complete(prepared=prepared, context=call_context)

    assert test.state.terminals == []
    assert prepared.evidence_set_artifact.artifact_id not in test.state.artifacts  # type: ignore[union-attr]


def test_terminal_failure_rolls_back_artifacts_workflow_and_terminal_side_effects() -> None:
    fixture = patch_prepared("passed")
    prepared, item = fixture[:2]
    test = harness(fixture)
    test.runs.fail = True

    with pytest.raises(Conflict, match="terminal CAS failed"):
        test.service.complete(prepared=prepared, context=context())

    assert test.state.approvals[item.approval_id] == item
    assert test.state.terminals == []
    assert test.state.audits == []
    assert test.state.bindings == {}
    assert prepared.evidence_set_artifact.artifact_id not in test.state.artifacts  # type: ignore[union-attr]


def test_validation_completion_replay_is_idempotent() -> None:
    fixture = patch_prepared("passed")
    prepared = fixture[0]
    test = harness(fixture)
    command_context = context()

    first = test.service.complete(prepared=prepared, context=command_context)
    second = test.service.complete(prepared=prepared, context=command_context)

    assert second == first
    assert test.state.terminals == [
        ("patch_validation_passed", first.published_artifact_ids)
    ]
    assert test.state.audits == ["approval.validation_completed"]
    assert len(test.state.idempotency) == 1


def test_validation_completion_idempotency_key_rejects_another_request_hash() -> None:
    fixture = patch_prepared("passed")
    prepared = fixture[0]
    test = harness(fixture)
    command_context = context()
    first = test.service.complete(prepared=prepared, context=command_context)

    with pytest.raises(Conflict, match="idempotency key"):
        test.service.complete(
            prepared=prepared,
            context=command_context.model_copy(update={"request_hash": "b" * 64}),
        )

    assert test.state.terminals == [
        ("patch_validation_passed", first.published_artifact_ids)
    ]
    assert test.state.audits == ["approval.validation_completed"]


def test_validation_completion_replay_rejects_tampered_run_binding() -> None:
    fixture = patch_prepared("passed")
    prepared = fixture[0]
    test = harness(fixture)
    command_context = context()
    test.service.complete(prepared=prepared, context=command_context)
    identity = (
        command_context.idempotency_scope,
        "approval.complete_validation",
        command_context.idempotency_key,
    )
    request_hash, response = test.state.idempotency[identity]
    response["run_id"] = "run:other"
    test.state.idempotency[identity] = (request_hash, response)

    with pytest.raises(IntegrityViolation, match="differs from the command"):
        test.service.complete(prepared=prepared, context=command_context)

    assert len(test.state.terminals) == 1
    assert len(test.state.audits) == 1


def test_regression_and_companion_artifacts_publish_with_exact_approval_binding() -> None:
    fixture, regression, companion, evidence_artifact = (
        _patch_with_regression_and_companion_publications()
    )
    prepared, item = fixture[:2]
    test = harness(fixture)

    result = test.service.complete(prepared=prepared, context=context())

    expected_artifact_ids = tuple(
        value.artifact_id for value in (regression, companion, evidence_artifact)
    )
    assert result.published_artifact_ids == expected_artifact_ids
    assert result.approval_item.regression_evidence_artifact_ids == (
        regression.artifact_id,
    )
    assert test.state.approvals[item.approval_id] == result.approval_item
    assert all(
        test.state.artifacts[value.artifact_id] == value
        for value in (regression, companion, evidence_artifact)
    )
    assert set(test.state.bindings) == {
        value.object_ref.key for value in (regression, companion, evidence_artifact)
    }
    assert test.state.terminals == [
        ("patch_validation_passed", expected_artifact_ids)
    ]


def test_terminal_failure_rolls_back_regression_and_companion_publications() -> None:
    fixture, regression, companion, evidence_artifact = (
        _patch_with_regression_and_companion_publications()
    )
    prepared, item = fixture[:2]
    test = harness(fixture)
    test.runs.fail = True

    with pytest.raises(Conflict, match="terminal CAS failed"):
        test.service.complete(prepared=prepared, context=context())

    assert test.state.approvals[item.approval_id] == item
    assert test.state.terminals == []
    assert test.state.audits == []
    assert test.state.bindings == {}
    assert all(
        value.artifact_id not in test.state.artifacts
        for value in (regression, companion, evidence_artifact)
    )


def test_context_run_must_equal_validation_run() -> None:
    fixture = patch_prepared("passed")
    with pytest.raises(IntegrityViolation, match="audit Run"):
        harness(fixture).service.complete(
            prepared=fixture[0],
            context=context("run:other"),
        )


def test_dedicated_auto_eligible_outcome_attaches_proof_in_same_terminal_uow() -> None:
    fixture = auto_patch_prepared()
    prepared = fixture[0]
    test = harness(fixture)

    result = test.service.complete(prepared=prepared, context=context())

    assert result.outcome_code == "patch_validation_auto_eligible"
    assert result.approval_item.status == "validated"
    assert result.approval_item.auto_apply_proof is not None
    assert result.approval_item.auto_apply_proof.proof_artifact_id == (
        prepared.auto_apply_proof_artifact.artifact_id  # type: ignore[union-attr]
    )
    assert test.auto_apply.calls == [result.approval_item]
    assert prepared.auto_apply_proof_artifact.artifact_id in test.state.artifacts  # type: ignore[union-attr]
    assert test.state.terminals == [
        ("patch_validation_auto_eligible", result.published_artifact_ids)
    ]


def test_ordinary_passed_outcome_cannot_smuggle_auto_apply_proof() -> None:
    prepared = auto_patch_prepared()[0]
    with pytest.raises(ValueError, match="only auto-eligible"):
        PreparedValidationCompletion.model_validate(
            {
                **prepared.model_dump(mode="python"),
                "outcome_code": "patch_validation_passed",
            }
        )


def test_auto_apply_guard_is_required_and_failure_rolls_back_publication() -> None:
    fixture = auto_patch_prepared()
    prepared, item = fixture[:2]
    missing = harness(fixture)
    missing.capabilities.auto_apply = None
    with pytest.raises(IntegrityViolation, match="auto_apply"):
        missing.service.complete(prepared=prepared, context=context())
    assert missing.state.approvals[item.approval_id] == item
    assert missing.state.bindings == {}

    rejected = harness(fixture)
    rejected.auto_apply.fail = True
    with pytest.raises(Conflict, match="auto-apply guard rejected"):
        rejected.service.complete(prepared=prepared, context=context())
    assert rejected.state.approvals[item.approval_id] == item
    assert rejected.state.artifacts.keys() == missing.state.artifacts.keys()
    assert rejected.state.terminals == []


def test_sql_repository_reserves_evidence_append_for_completion_primitive(
    tmp_path,
) -> None:
    engine = get_engine(f"sqlite:///{tmp_path / 'completion-cas.db'}")
    Base.metadata.create_all(engine)
    item = repository_item()
    validating = repository_replace(
        item,
        status="validating",
        workflow_revision=2,
        active_validation_run_id="run:validation:1",
    )
    validated = repository_replace(
        validating,
        status="validated",
        workflow_revision=3,
        active_validation_run_id=None,
        evidence_set_artifact_id=EVIDENCE_ARTIFACT_ID,
    )
    try:
        with Session(engine) as session, session.begin():
            _seed_artifacts(session)
            repository = SqlApprovalRepository(session)
            repository.insert_draft(item)
            repository.compare_and_set(item.approval_id, 1, validating)
            with pytest.raises(IntegrityViolation, match="only be appended"):
                repository.compare_and_set(item.approval_id, 2, validated)
            assert repository.compare_and_set_validation_completion(
                item.approval_id,
                2,
                validated,
            ) == validated
    finally:
        engine.dispose()


def test_sql_constraint_target_null_to_exact_is_completion_only(tmp_path) -> None:
    engine = get_engine(f"sqlite:///{tmp_path / 'constraint-completion-cas.db'}")
    Base.metadata.create_all(engine)
    patch_item = repository_item()
    constraint = repository_replace(
        patch_item,
        subject_kind="constraint_proposal",
        target_binding=None,
    )
    validating = repository_replace(
        constraint,
        status="validating",
        workflow_revision=2,
        active_validation_run_id="run:validation:1",
    )
    target = ConstraintTargetBindingV1(
        target_artifact_id="artifact:constraint:candidate",
        target_snapshot_id="constraint-snapshot:1",
        target_digest="b" * 64,
        ref_name="constraints/head",
        expected_ref=None,
    )
    validated = repository_replace(
        validating,
        status="validated",
        workflow_revision=3,
        active_validation_run_id=None,
        evidence_set_artifact_id=EVIDENCE_ARTIFACT_ID,
        target_binding=target.model_dump(mode="json"),
    )
    try:
        with Session(engine) as session, session.begin():
            _seed_artifacts(session)
            subject_row = session.get(ArtifactRow, constraint.subject_artifact_id)
            assert subject_row is not None
            subject_row.kind = "constraint_proposal"
            session.add(
                ArtifactRow(
                    artifact_id=target.target_artifact_id,
                    lineage_schema_version="lineage@2",
                    kind="constraint_snapshot",
                    version_tuple={"constraint_snapshot_id": target.target_snapshot_id},
                    lineage=[constraint.subject_artifact_id],
                    payload_hash=target.target_digest,
                    created_at="2026-07-14T10:00:00Z",
                    meta={},
                    object_ref={},
                )
            )
            session.flush()
            repository = SqlApprovalRepository(session)
            repository.insert_draft(constraint)
            repository.compare_and_set(constraint.approval_id, 1, validating)
            with pytest.raises(IntegrityViolation, match="immutable target binding"):
                repository.compare_and_set(constraint.approval_id, 2, validated)
            assert repository.compare_and_set_validation_completion(
                constraint.approval_id,
                2,
                validated,
            ) == validated
    finally:
        engine.dispose()
