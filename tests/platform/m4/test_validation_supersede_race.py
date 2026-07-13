from __future__ import annotations

import pytest

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.workflow import SubjectHead
from tests.platform.m4.validation_testkit import (
    approval_item,
    artifact,
    context,
    harness,
    patch_prepared,
    replace_item,
)


def _supersede_first(outcome: str = "passed"):
    fixture = patch_prepared(outcome)
    prepared, old_item = fixture[:2]
    test = harness(fixture)
    superseded = replace_item(
        old_item,
        status="superseded",
        workflow_revision=old_item.workflow_revision + 1,
        active_validation_run_id=None,
    )
    new_subject = artifact("patch", "new-patch-revision")
    test.state.approvals[old_item.approval_id] = superseded
    test.state.heads[old_item.subject_series_id] = SubjectHead(
        subject_series_id=old_item.subject_series_id,
        current_subject_artifact_id=new_subject.artifact_id,
        current_approval_id="approval:2",
        revision=2,
    )
    test.state.artifacts[new_subject.artifact_id] = new_subject
    return prepared, old_item, superseded, test


@pytest.mark.parametrize("outcome", ["passed", "failed", "execution_failed"])
def test_supersede_first_terminates_old_run_without_publishing_evidence(
    outcome: str,
) -> None:
    prepared, old_item, superseded, test = _supersede_first(outcome)

    result = test.service.complete(prepared=prepared, context=context())

    assert result.disposition == "subject_superseded"
    assert result.outcome_code == "subject_superseded"
    assert result.approval_item == superseded
    assert test.state.approvals[old_item.approval_id] == superseded
    assert test.state.terminals == [("subject_superseded", ())]
    assert result.published_artifact_ids == ()
    assert test.state.bindings == {}
    if prepared.evidence_set_artifact is not None:
        assert prepared.evidence_set_artifact.artifact_id not in test.state.artifacts


def test_completion_first_keeps_evidence_only_on_old_superseded_revision() -> None:
    fixture = patch_prepared("passed")
    prepared, old_item = fixture[:2]
    test = harness(fixture)

    completed = test.service.complete(prepared=prepared, context=context()).approval_item
    assert completed.status == "validated"
    assert completed.evidence_set_artifact_id is not None

    old_superseded = replace_item(
        completed,
        status="superseded",
        workflow_revision=completed.workflow_revision + 1,
    )
    new_subject = artifact("patch", "new-patch-revision")
    new_target = artifact(
        "ir_snapshot",
        "new-preview",
        lineage=(new_subject.artifact_id,),
        ir_snapshot_id="snapshot:preview:2",
    )
    new_item = approval_item(
        subject=new_subject,
        target=new_target,
        kind="patch",
        series_id=old_item.subject_series_id,
        approval_id="approval:2",
        subject_revision=2,
        workflow_revision=1,
        run_id="run:unused",
    )
    new_item = replace_item(
        new_item,
        status="draft",
        active_validation_run_id=None,
        supersedes_approval_id=old_item.approval_id,
    )
    test.state.approvals[old_item.approval_id] = old_superseded
    test.state.approvals[new_item.approval_id] = new_item
    test.state.heads[old_item.subject_series_id] = SubjectHead(
        subject_series_id=old_item.subject_series_id,
        current_subject_artifact_id=new_item.subject_artifact_id,
        current_approval_id=new_item.approval_id,
        revision=2,
    )

    assert test.state.approvals[old_item.approval_id].evidence_set_artifact_id == (
        completed.evidence_set_artifact_id
    )
    assert new_item.evidence_set_artifact_id is None
    assert new_item.regression_evidence_artifact_ids == ()
    assert new_item.auto_apply_proof is None


def test_noncurrent_item_not_marked_superseded_fails_closed() -> None:
    fixture = patch_prepared("passed")
    prepared, item = fixture[:2]
    test = harness(fixture)
    test.state.heads[item.subject_series_id] = SubjectHead(
        subject_series_id=item.subject_series_id,
        current_subject_artifact_id="artifact:other",
        current_approval_id="approval:other",
        revision=2,
    )

    with pytest.raises(IntegrityViolation, match="not a superseded revision"):
        test.service.complete(prepared=prepared, context=context())

    assert test.state.terminals == []


def test_superseded_terminal_failure_is_atomic() -> None:
    prepared, old_item, superseded, test = _supersede_first()
    before_head = test.state.heads[old_item.subject_series_id]
    test.runs.fail = True

    with pytest.raises(Conflict, match="terminal CAS failed"):
        test.service.complete(prepared=prepared, context=context())

    assert test.state.approvals[old_item.approval_id] == superseded
    assert test.state.heads[old_item.subject_series_id] == before_head
    assert test.state.terminals == []
    assert test.state.audits == []


def test_superseded_completion_replay_does_not_duplicate_terminal_or_audit() -> None:
    prepared, _old_item, _superseded, test = _supersede_first()
    command_context = context()

    first = test.service.complete(prepared=prepared, context=command_context)
    second = test.service.complete(prepared=prepared, context=command_context)

    assert second == first
    assert test.state.terminals == [("subject_superseded", ())]
    assert test.state.audits == ["approval.validation_subject_superseded"]
    assert len(test.state.idempotency) == 1


@pytest.mark.parametrize("mismatch", ["old_head_revision", "old_workflow_revision"])
def test_superseded_path_requires_exact_old_validating_revision(mismatch: str) -> None:
    prepared, old_item, _superseded, test = _supersede_first()
    subject = prepared.execution.payload.subject
    changed_subject = subject.model_copy(
        update={
            (
                "subject_head_revision"
                if mismatch == "old_head_revision"
                else "expected_workflow_revision"
            ): 99
        }
    )
    changed_payload = prepared.execution.payload.model_copy(
        update={"subject": changed_subject}
    )
    changed_execution = prepared.execution.model_copy(update={"payload": changed_payload})
    changed = prepared.model_copy(update={"execution": changed_execution})
    test.runs.expected = changed_execution

    with pytest.raises(IntegrityViolation, match="not a superseded revision"):
        test.service.complete(prepared=changed, context=context())

    assert test.state.approvals[old_item.approval_id].status == "superseded"
    assert test.state.terminals == []
