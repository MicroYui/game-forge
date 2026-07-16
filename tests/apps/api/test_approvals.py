from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.lineage import AuditActor
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from tests.apps.api.workflow_command_testkit import (
    build_harness,
    drive_to_validated,
    headers,
    maker_actor,
    resource_etag,
    reviewer_actor,
)


def _client(harness) -> TestClient:
    return TestClient(harness.app, base_url="https://gameforge.test")


def _patch_payload(harness) -> dict:
    return {
        "request_schema_version": "human-patch-draft-request@1",
        "base_snapshot_artifact_id": harness.base_artifact_id,
        "constraint_snapshot_artifact_id": None,
        "ref_name": "content/head",
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
        "rationale": "Keep quest rewards within the approved economy envelope.",
        "candidate_export_profiles": [],
    }


def _base(harness) -> None:
    from tests.apps.api.workflow_command_testkit import publish_base

    publish_base(
        harness, entities=[Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})]
    )


def _submit_to_pending(harness, client) -> tuple[str, list[str], int]:
    harness.use_actor(maker_actor(harness))
    draft = client.post(
        "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="draft")
    )
    assert draft.status_code == 201, draft.text
    artifact_id = draft.json()["artifact"]["artifact_id"]
    approval_id = f"approval:patch:{artifact_id}"
    drive_to_validated(harness, approval_id, run_id="run:patch-validation:1")
    validated = harness.load_item(approval_id)
    submit = client.post(
        f"/api/v1/patches/{artifact_id}:submit-for-approval",
        json={
            "request_schema_version": "submit-for-approval-request@1",
            "approval_id": approval_id,
            "expected_workflow_revision": validated.workflow_revision,
        },
        headers=headers(
            key="submit",
            if_match=resource_etag(
                resource_kind="patch",
                resource_id=artifact_id,
                revision=validated.workflow_revision,
            ),
        ),
    )
    assert submit.status_code == 200, submit.text
    pending = harness.load_item(approval_id)
    return approval_id, [r.requirement_id for r in pending.requirements], pending.workflow_revision


def test_submit_derives_server_metadata_and_projects_view(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    _base(harness)
    with _client(harness) as client:
        approval_id, requirement_ids, revision = _submit_to_pending(harness, client)
    item = harness.load_item(approval_id)
    assert item.status == "pending_approval"
    assert item.submitted_at is not None


def test_decision_derives_actor_time_and_id_and_supports_requirement_ids(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    _base(harness)
    with _client(harness) as client:
        approval_id, requirement_ids, revision = _submit_to_pending(harness, client)
        harness.use_actor(reviewer_actor(harness))
        approve = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": requirement_ids,
                "expected_workflow_revision": revision,
                "reason_code": "independent_review_passed",
            },
            headers=headers(
                key="approve",
                if_match=resource_etag(
                    resource_kind="approval",
                    resource_id=approval_id,
                    revision=revision,
                ),
            ),
        )
    assert approve.status_code == 200, approve.text
    decisions = approve.json()["approval"]["decisions"]
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["actor"]["principal_id"] == "human:reviewer"
    assert decision["decision"] == "approve"
    assert decision["requirement_ids"] == sorted(requirement_ids)
    assert decision["decision_id"]  # server-derived
    assert decision["occurred_at"]  # server clock


def test_proposer_self_approval_is_rejected(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    _base(harness)
    with _client(harness) as client:
        approval_id, requirement_ids, revision = _submit_to_pending(harness, client)
        # the maker (proposer) attempts to approve their own subject
        harness.use_actor(maker_actor(harness))
        response = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": requirement_ids,
                "expected_workflow_revision": revision,
                "reason_code": "self_approval_attempt",
            },
            headers=headers(
                key="self-approve",
                if_match=resource_etag(
                    resource_kind="approval",
                    resource_id=approval_id,
                    revision=revision,
                ),
            ),
        )
    assert response.status_code in {403, 409}
    assert harness.load_item(approval_id).status == "pending_approval"


def test_role_revoked_between_submit_and_decide_takes_effect(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    _base(harness)
    with _client(harness) as client:
        approval_id, requirement_ids, revision = _submit_to_pending(harness, client)
        # revoke the reviewer's economy role AFTER submission, BEFORE the decision
        with Session(harness.engine) as session, session.begin():
            identities = SqlIdentityRepository(session, clock=harness.clock)
            reviewer = identities.project("human:reviewer")
            assignment = identities.get_assignment("assignment:human:reviewer:economy")
            identities.revoke(
                assignment_id="assignment:human:reviewer:economy",
                revoked_by=AuditActor(principal_id="human:admin", principal_kind="human"),
                revoke_reason="rotation",
                expected_principal_revision=reviewer.revision,
                expected_assignment_revision=assignment.revision,
            )
        harness.use_actor(reviewer_actor(harness))
        response = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": requirement_ids,
                "expected_workflow_revision": revision,
                "reason_code": "independent_review_passed",
            },
            headers=headers(
                key="approve-after-revoke",
                if_match=resource_etag(
                    resource_kind="approval",
                    resource_id=approval_id,
                    revision=revision,
                ),
            ),
        )
    assert response.status_code in {403, 409}
    assert harness.load_item(approval_id).status == "pending_approval"
