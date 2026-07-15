from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gameforge.contracts.ir import Entity, NodeType
from tests.apps.api.workflow_command_testkit import (
    build_harness,
    drive_to_validated,
    headers,
    maker_actor,
    operator_actor,
    publish_base,
    reviewer_actor,
)

REF = "content-head"  # single path segment: the rollback-draft route is /refs/{ref_name}/...


def _client(harness) -> TestClient:
    return TestClient(harness.app, base_url="https://gameforge.test")


def _patch_payload(harness) -> dict:
    return {
        "request_schema_version": "human-patch-draft-request@1",
        "base_snapshot_artifact_id": harness.base_artifact_id,
        "constraint_snapshot_artifact_id": None,
        "ref_name": REF,
        "expected_ref": harness.base_ref.model_dump(mode="json"),
        "expected_to_fix": [],
        "preconditions": [],
        "side_effect_risk": "low",
        "ops": [
            {
                "op_id": "set-reward-gold",
                "op": "set_entity_attr",
                "target": "q:1.reward_gold",
                "old_value": 120,
                "new_value": 80,
            }
        ],
        "rationale": "Lower quest reward within the approved economy envelope.",
        "candidate_export_profiles": [],
    }


def _apply_patch(harness, client, key: str) -> tuple[str, dict]:
    """Draft→validate→submit→approve→apply a patch; return (approval_id, ref_value)."""

    harness.use_actor(maker_actor(harness))
    draft = client.post(
        "/api/v1/patches", json=_patch_payload(harness), headers=headers(key=f"{key}:draft")
    )
    assert draft.status_code == 201, draft.text
    artifact_id = draft.json()["artifact"]["artifact_id"]
    approval_id = f"approval:patch:{artifact_id}"
    drive_to_validated(harness, approval_id, run_id=f"run:patch-validation:{key}")
    item = harness.load_item(approval_id)
    client.post(
        f"/api/v1/patches/{artifact_id}:submit-for-approval",
        json={
            "request_schema_version": "submit-for-approval-request@1",
            "approval_id": approval_id,
            "expected_workflow_revision": item.workflow_revision,
        },
        headers=headers(key=f"{key}:submit"),
    )
    pending = harness.load_item(approval_id)
    harness.use_actor(reviewer_actor(harness))
    client.post(
        f"/api/v1/approvals/{approval_id}:approve",
        json={
            "request_schema_version": "approval-decision-request@1",
            "decision": "approve",
            "requirement_ids": [r.requirement_id for r in pending.requirements],
            "expected_workflow_revision": pending.workflow_revision,
            "reason_code": "independent_review_passed",
        },
        headers=headers(key=f"{key}:approve"),
    )
    approved = harness.load_item(approval_id)
    binding = approved.target_binding
    harness.use_actor(operator_actor(harness))
    apply = client.post(
        f"/api/v1/patches/{approved.subject_artifact_id}:apply",
        json={
            "request_schema_version": "workflow-apply-request@1",
            "approval_id": approval_id,
            "expected_workflow_revision": approved.workflow_revision,
            "subject_digest": approved.subject_digest,
            "target_artifact_id": binding.target_artifact_id,
            "target_digest": binding.target_digest,
            "ref_name": binding.ref_name,
            "expected_ref": binding.expected_ref.model_dump(mode="json"),
        },
        headers=headers(key=f"{key}:apply"),
    )
    assert apply.status_code == 200, apply.text
    return approval_id, apply.json()["ref_value"]


def _rollback_draft_payload(harness, *, ref_value, reverses) -> dict:
    return {
        "request_schema_version": "rollback-draft-request@1",
        "expected_current_ref": ref_value,
        "target_artifact_id": harness.base_artifact_id,
        "target_history_revision": 1,
        "rollback_profile": {"profile_id": "rollback.content", "version": 1},
        "reason": "Restore the independently approved historical reward snapshot.",
        "reverses_approval_id": reverses,
    }


def _draft_rollback(harness, client, *, ref_value, reverses, key: str) -> tuple[str, str]:
    harness.use_actor(maker_actor(harness))
    draft = client.post(
        f"/api/v1/refs/{REF}/rollback-requests",
        json=_rollback_draft_payload(harness, ref_value=ref_value, reverses=reverses),
        headers=headers(key=key),
    )
    assert draft.status_code == 201, draft.text
    artifact_id = draft.json()["artifact"]["artifact_id"]
    return artifact_id, f"approval:rollback_request:{artifact_id}"


def test_rollback_request_validate_submit_independent_approval_apply(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(
        harness,
        entities=[Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})],
        ref_name=REF,
    )
    with _client(harness) as client:
        patch_approval_id, ref_after_patch = _apply_patch(harness, client, key="patch")

        artifact_id, rb_approval = _draft_rollback(
            harness, client, ref_value=ref_after_patch, reverses=patch_approval_id, key="rb:draft"
        )
        drive_to_validated(harness, rb_approval, run_id="run:rollback-validation:1")

        validated = harness.load_item(rb_approval)
        harness.use_actor(maker_actor(harness))
        submit = client.post(
            f"/api/v1/rollback-requests/{artifact_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": rb_approval,
                "expected_workflow_revision": validated.workflow_revision,
            },
            headers=headers(key="rb:submit"),
        )
        assert submit.status_code == 200, submit.text

        pending = harness.load_item(rb_approval)
        harness.use_actor(reviewer_actor(harness))
        approve = client.post(
            f"/api/v1/approvals/{rb_approval}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": [r.requirement_id for r in pending.requirements],
                "expected_workflow_revision": pending.workflow_revision,
                "reason_code": "independent_review_passed",
            },
            headers=headers(key="rb:approve"),
        )
        assert approve.status_code == 200, approve.text

        approved = harness.load_item(rb_approval)
        binding = approved.target_binding
        harness.use_actor(operator_actor(harness))
        apply = client.post(
            f"/api/v1/rollback-requests/{approved.subject_artifact_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": rb_approval,
                "expected_workflow_revision": approved.workflow_revision,
                "subject_digest": approved.subject_digest,
                "target_artifact_id": binding.target_artifact_id,
                "target_digest": binding.target_digest,
                "ref_name": binding.ref_name,
                "expected_ref": binding.expected_ref.model_dump(mode="json"),
            },
            headers=headers(key="rb:apply"),
        )
    assert apply.status_code == 200, apply.text
    result = apply.json()
    assert result["approval"]["approval"]["status"] == "applied"
    assert result["ref_value"]["artifact_id"] == harness.base_artifact_id
    assert result["ref_transition_id"] is not None
    assert result["reversed_approval_id"] == patch_approval_id
    assert harness.load_item(patch_approval_id).status == "rolled_back"


def test_rollback_cannot_skip_validation(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(
        harness,
        entities=[Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})],
        ref_name=REF,
    )
    with _client(harness) as client:
        patch_approval_id, ref_after_patch = _apply_patch(harness, client, key="patch")
        artifact_id, rb_approval = _draft_rollback(
            harness, client, ref_value=ref_after_patch, reverses=patch_approval_id, key="rb:draft"
        )
        # submit straight from draft, bypassing validation
        draft_item = harness.load_item(rb_approval)
        harness.use_actor(maker_actor(harness))
        submit = client.post(
            f"/api/v1/rollback-requests/{artifact_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": rb_approval,
                "expected_workflow_revision": draft_item.workflow_revision,
            },
            headers=headers(key="rb:bypass-validate"),
        )
    # fails closed with a state-machine conflict (the draft is not validated); it never advances
    assert submit.status_code == 409
    assert harness.load_item(rb_approval).status == "draft"


def test_rollback_cannot_skip_independent_approval(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(
        harness,
        entities=[Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})],
        ref_name=REF,
    )
    with _client(harness) as client:
        patch_approval_id, ref_after_patch = _apply_patch(harness, client, key="patch")
        artifact_id, rb_approval = _draft_rollback(
            harness, client, ref_value=ref_after_patch, reverses=patch_approval_id, key="rb:draft"
        )
        drive_to_validated(harness, rb_approval, run_id="run:rollback-validation:1")
        validated = harness.load_item(rb_approval)
        harness.use_actor(maker_actor(harness))
        client.post(
            f"/api/v1/rollback-requests/{artifact_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": rb_approval,
                "expected_workflow_revision": validated.workflow_revision,
            },
            headers=headers(key="rb:submit"),
        )
        pending = harness.load_item(rb_approval)
        binding = pending.target_binding
        # apply straight from pending_approval, bypassing the independent approval
        harness.use_actor(operator_actor(harness))
        apply = client.post(
            f"/api/v1/rollback-requests/{pending.subject_artifact_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": rb_approval,
                "expected_workflow_revision": pending.workflow_revision,
                "subject_digest": pending.subject_digest,
                "target_artifact_id": binding.target_artifact_id,
                "target_digest": binding.target_digest,
                "ref_name": binding.ref_name,
                "expected_ref": binding.expected_ref.model_dump(mode="json"),
            },
            headers=headers(key="rb:bypass-approve"),
        )
    assert apply.status_code == 409
    assert harness.load_item(rb_approval).status == "pending_approval"


def test_rollback_apply_cannot_skip_request(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(
        harness,
        entities=[Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})],
        ref_name=REF,
    )
    with _client(harness) as client:
        _apply_patch(harness, client, key="patch")
        harness.use_actor(operator_actor(harness))
        apply = client.post(
            "/api/v1/rollback-requests/artifact:phantom:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": "approval:rollback_request:phantom",
                "expected_workflow_revision": 5,
                "subject_digest": "1" * 64,
                "target_digest": "2" * 64,
                "target_artifact_id": harness.base_artifact_id,
                "ref_name": REF,
                "expected_ref": harness.base_ref.model_dump(mode="json"),
            },
            headers=headers(key="rb:phantom-apply"),
        )
    assert apply.status_code == 409
