from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gameforge.contracts.ir import Entity, NodeType
from tests.apps.api.workflow_command_testkit import (
    build_harness,
    headers,
    maker_actor,
    publish_base,
)


def _client(harness) -> TestClient:
    return TestClient(harness.app, base_url="https://gameforge.test")


def _spec_payload(**overrides) -> dict:
    payload = {
        "request_schema_version": "human-spec-upload-request@1",
        "ref_name": "spec/head",
        "expected_ref": None,
        "schema_registry_version": "registry@1",
        "meta_schema_version": "meta@1",
        "domain_scope": {"domain_ids": ["economy"]},
        "content_payload": {"kind": "spec", "reward_gold": 120},
    }
    payload.update(overrides)
    return payload


def _base_entities() -> list[Entity]:
    return [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})]


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


def test_human_spec_upload_publishes_ref_and_returns_spec_view(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        response = client.post(
            "/api/v1/specs",
            json=_spec_payload(),
            headers=headers(key="spec:1"),
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["view_schema_version"] == "spec-view@1"
    assert body["ref_name"] == "spec/head"
    assert body["ref_value"]["revision"] == 1
    assert body["artifact"]["kind"] == "ir_snapshot"
    assert response.headers["X-Resource-Revision"] == "1"
    assert response.headers["ETag"].startswith('"')
    assert response.headers["Cache-Control"] == "private, no-cache"


def test_spec_upload_duplicate_exact_request_replays_committed_result(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        first = client.post("/api/v1/specs", json=_spec_payload(), headers=headers(key="spec:2"))
        second = client.post("/api/v1/specs", json=_spec_payload(), headers=headers(key="spec:2"))
    assert first.status_code == 201 and second.status_code == 201
    assert first.json() == second.json()
    assert first.headers["ETag"] == second.headers["ETag"]


def test_spec_upload_same_key_different_payload_is_idempotency_conflict(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        first = client.post("/api/v1/specs", json=_spec_payload(), headers=headers(key="spec:3"))
        second = client.post(
            "/api/v1/specs",
            json=_spec_payload(content_payload={"kind": "spec", "reward_gold": 99}),
            headers=headers(key="spec:3"),
        )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "idempotency_conflict"


def test_spec_upload_stale_expected_ref_is_conflict(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        first = client.post("/api/v1/specs", json=_spec_payload(), headers=headers(key="spec:4"))
        stale = client.post(
            "/api/v1/specs",
            json=_spec_payload(
                content_payload={"kind": "spec", "reward_gold": 50},
                expected_ref=None,
            ),
            headers=headers(key="spec:5"),
        )
    assert first.status_code == 201
    assert stale.status_code == 409
    assert stale.json()["code"] == "revision_conflict"


def test_human_patch_draft_creates_draft_approval(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        response = client.post(
            "/api/v1/patches",
            json=_patch_payload(harness),
            headers=headers(key="patch:draft:1"),
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["view_schema_version"] == "patch-artifact-read-view@1"
    assert body["approval_status"] == "draft"
    assert body["workflow_revision"] == 1
    assert body["artifact"]["kind"] == "patch"
    assert response.headers["ETag"].startswith('"')


def test_patch_draft_duplicate_request_replays(tmp_path: Path) -> None:
    # Advance the clock between the two identical requests: the second request
    # re-assembles a draft whose fresh created_at differs from the committed one, so
    # a broken replay path would raise IntegrityViolation (500) instead of replaying.
    from datetime import timedelta

    from tests.apps.api.workflow_command_testkit import NOW_DT, AdvancingUtcClock

    harness = build_harness(tmp_path, clock=AdvancingUtcClock(NOW_DT, step=timedelta(seconds=1)))
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        first = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="patch:draft:2")
        )
        second = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="patch:draft:2")
        )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    # Duplicate exact request replays the committed result byte-for-byte.
    assert first.json() == second.json()
    assert first.headers["ETag"] == second.headers["ETag"]
    assert first.json()["artifact"]["created_at"] == second.json()["artifact"]["created_at"]


def test_patch_draft_same_key_different_payload_is_idempotency_conflict(tmp_path: Path) -> None:
    from datetime import timedelta

    from tests.apps.api.workflow_command_testkit import NOW_DT, AdvancingUtcClock

    harness = build_harness(tmp_path, clock=AdvancingUtcClock(NOW_DT, step=timedelta(seconds=1)))
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        first = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="patch:draft:3")
        )
        conflict = client.post(
            "/api/v1/patches",
            json={**_patch_payload(harness), "rationale": "A different rationale entirely."},
            headers=headers(key="patch:draft:3"),
        )
    assert first.status_code == 201, first.text
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_patch_validate_without_admission_fails_closed(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, admission=False)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    payload = {
        "request_schema_version": "patch-validation-admission-request@1",
        "approval_id": "approval:patch:x",
        "expected_subject_head_revision": 1,
        "expected_workflow_revision": 1,
        "subject_digest": "1" * 64,
        "base_snapshot_artifact_id": harness.base_artifact_id,
        "preview_snapshot_artifact_id": harness.base_artifact_id,
        "candidate_config_export_artifact_ids": [],
        "target": {
            "ref_name": "content/head",
            "expected_ref": harness.base_ref.model_dump(mode="json"),
        },
        "validation_policy": {"profile_id": "validation.patch", "version": 1},
        "checker_profiles": [],
        "simulation_profiles": [],
        "findings": [],
        "review_artifact_ids": [],
        "playtest_trace_artifact_ids": [],
        "regression_suite_artifact_ids": [],
    }
    with _client(harness) as client:
        response = client.post(
            "/api/v1/patches/artifact-patch:validate",
            json=payload,
            headers=headers(key="patch:validate:1"),
        )
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "dependency_unavailable"
    assert body["errors"] == [{"component": "run_admission"}]


def test_patch_validate_with_admission_returns_accepted_run(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    payload = {
        "request_schema_version": "patch-validation-admission-request@1",
        "approval_id": "approval:patch:x",
        "expected_subject_head_revision": 1,
        "expected_workflow_revision": 1,
        "subject_digest": "1" * 64,
        "base_snapshot_artifact_id": harness.base_artifact_id,
        "preview_snapshot_artifact_id": harness.base_artifact_id,
        "candidate_config_export_artifact_ids": [],
        "target": {
            "ref_name": "content/head",
            "expected_ref": harness.base_ref.model_dump(mode="json"),
        },
        "validation_policy": {"profile_id": "validation.patch", "version": 1},
        "checker_profiles": [],
        "simulation_profiles": [],
        "findings": [],
        "review_artifact_ids": [],
        "playtest_trace_artifact_ids": [],
        "regression_suite_artifact_ids": [],
    }
    with _client(harness) as client:
        response = client.post(
            "/api/v1/patches/artifact-patch:validate",
            json=payload,
            headers=headers(key="patch:validate:2"),
        )
    assert response.status_code == 202, response.text
    assert response.json()["run_id"].startswith("run:patch.validate:")
    assert harness.admission.calls[-1]["idempotency_key"] == "patch:validate:2"


def _draft_and_validate(harness, client) -> tuple[str, str]:
    draft = client.post(
        "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="patch:draft")
    )
    assert draft.status_code == 201, draft.text
    artifact_id = draft.json()["artifact"]["artifact_id"]
    approval_id = f"approval:patch:{artifact_id}"
    from tests.apps.api.workflow_command_testkit import drive_to_validated

    drive_to_validated(harness, approval_id, run_id="run:patch-validation:1")
    return artifact_id, approval_id


def test_patch_full_lifecycle_submit_approve_apply(tmp_path: Path) -> None:
    from tests.apps.api.workflow_command_testkit import operator_actor, reviewer_actor

    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        artifact_id, approval_id = _draft_and_validate(harness, client)

        validated = harness.load_item(approval_id)
        submit = client.post(
            f"/api/v1/patches/{artifact_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": validated.workflow_revision,
            },
            headers=headers(key="patch:submit"),
        )
        assert submit.status_code == 200, submit.text
        assert submit.json()["approval"]["status"] == "pending_approval"

        pending = harness.load_item(approval_id)
        requirement_ids = [r.requirement_id for r in pending.requirements]
        harness.use_actor(reviewer_actor(harness))
        approve = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": requirement_ids,
                "expected_workflow_revision": pending.workflow_revision,
                "reason_code": "independent_review_passed",
            },
            headers=headers(key="patch:approve"),
        )
        assert approve.status_code == 200, approve.text
        assert approve.json()["approval"]["status"] == "approved"

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
            headers=headers(key="patch:apply"),
        )
    assert apply.status_code == 200, apply.text
    result = apply.json()
    assert result["result_schema_version"] == "workflow-apply-result@1"
    assert result["approval"]["approval"]["status"] == "applied"
    assert result["ref_value"]["artifact_id"] == binding.target_artifact_id
    assert result["ref_transition_id"] is None


def test_submit_stale_workflow_revision_is_conflict(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        artifact_id, approval_id = _draft_and_validate(harness, client)
        submit = client.post(
            f"/api/v1/patches/{artifact_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": 1,
            },
            headers=headers(key="patch:submit:stale"),
        )
    assert submit.status_code == 409
    assert submit.json()["code"] == "revision_conflict"


def _constraint(id_: str, assert_expr: str) -> dict:
    return {
        "id": id_,
        "dsl_grammar_version": "dsl@1",
        "kind": "numeric",
        "oracle": "deterministic",
        "predicates": [],
        "assert": assert_expr,
        "severity": "major",
    }


def _constraint_payload(**overrides) -> dict:
    payload = {
        "request_schema_version": "human-constraint-draft-request@1",
        "base_constraint_snapshot_artifact_id": None,
        "ref_name": "constraints/head",
        "expected_ref": None,
        "dsl_grammar_version": "dsl@1",
        "domain_scope": {"domain_ids": ["economy"]},
        "constraints": [_constraint("c:reward-cap", "reward_gold <= 100")],
        "source_artifact_ids": [],
        "rationale": "Cap economy reward payouts.",
    }
    payload.update(overrides)
    return payload


def test_human_constraint_draft_and_revision(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        draft = client.post(
            "/api/v1/constraint-proposals",
            json=_constraint_payload(),
            headers=headers(key="constraint:draft"),
        )
        assert draft.status_code == 201, draft.text
        body = draft.json()
        assert body["view_schema_version"] == "constraint-proposal-read-view@1"
        assert body["proposal"]["revision"] == 1
        assert body["workflow_revision"] == 1
        artifact_id = body["artifact"]["artifact_id"]
        approval_id = f"approval:constraint_proposal:{artifact_id}"

        revise = client.post(
            f"/api/v1/constraint-proposals/{artifact_id}:revise",
            json=_constraint_payload(
                request_schema_version="human-constraint-revision-request@1",
                constraints=[_constraint("c:reward-cap", "reward_gold <= 90")],
                approval_id=approval_id,
                expected_subject_head_revision=1,
                expected_workflow_revision=1,
            ),
            headers=headers(key="constraint:revise"),
        )
    assert revise.status_code == 201, revise.text
    revised = revise.json()
    assert revised["proposal"]["revision"] == 2
    assert revised["proposal"]["supersedes_artifact_id"] == artifact_id


def test_patch_rebase_conflict_persists_conflict_set(tmp_path: Path) -> None:
    from tests.apps.api.workflow_command_testkit import apply_full_patch

    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    with _client(harness) as client:
        # an intervening approved apply advances the ref away from the draft's base
        apply_full_patch(harness, client, ref_name="content/head", new_value=100, key="intervening")
        # draft a competing patch on the ORIGINAL base
        harness.use_actor(maker_actor(harness))
        draft = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="rebase:draft")
        )
        assert draft.status_code == 201, draft.text
        patch_artifact = draft.json()["artifact"]["artifact_id"]
        approval_id = f"approval:patch:{patch_artifact}"
        live_ref = _live_ref(harness, "content/head")
        rebase = client.post(
            f"/api/v1/patches/{patch_artifact}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
            },
            headers=headers(key="rebase:1"),
        )
    assert rebase.status_code == 200, rebase.text
    body = rebase.json()
    assert body["status"] == "conflicted"
    assert body["conflict_set_id"].startswith("conflict-set:")


def test_patch_rebase_clean_compiles_and_publishes_rebased_draft(tmp_path: Path) -> None:
    from gameforge.contracts.storage import RefValue
    from tests.apps.api.workflow_command_testkit import apply_full_patch

    harness = build_harness(tmp_path)
    # a base with two independent fields so the intervening change and the draft touch
    # disjoint JSON paths -> a conflict-free three-way merge (the clean rebase branch).
    publish_base(
        harness,
        entities=[
            Entity(
                id="q:1",
                type=NodeType.QUEST,
                attrs={"reward_gold": 120, "difficulty": "normal"},
            )
        ],
    )
    with _client(harness) as client:
        # intervening approved apply advances the ref by changing difficulty only
        apply_full_patch(
            harness,
            client,
            ref_name="content/head",
            key="intervening",
            ops=[
                {
                    "op_id": "set-difficulty",
                    "op": "set_entity_attr",
                    "target": "q:1.difficulty",
                    "old_value": "normal",
                    "new_value": "hard",
                }
            ],
        )
        # competing draft on the ORIGINAL base changes reward_gold only
        harness.use_actor(maker_actor(harness))
        draft = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="clean:draft")
        )
        assert draft.status_code == 201, draft.text
        patch_artifact = draft.json()["artifact"]["artifact_id"]
        source_approval_id = f"approval:patch:{patch_artifact}"
        live_ref = _live_ref(harness, "content/head")
        rebase = client.post(
            f"/api/v1/patches/{patch_artifact}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": source_approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
            },
            headers=headers(key="clean:rebase"),
        )
    assert rebase.status_code == 200, rebase.text
    body = rebase.json()
    assert body["status"] == "clean"
    assert body["conflict_set_id"] is None
    new_patch_artifact_id = body["new_patch_artifact_id"]
    assert new_patch_artifact_id is not None

    # the byte-exact rebased draft supersedes the source on the same subject series,
    # carrying its exact preview companion pinned to the live ref (supersession CAS).
    rebased = harness.load_item(f"approval:patch:{new_patch_artifact_id}")
    source = harness.load_item(source_approval_id)
    assert rebased.supersedes_approval_id == source_approval_id
    assert rebased.subject_series_id == source.subject_series_id
    assert rebased.subject_revision == source.subject_revision + 1
    assert rebased.status == "draft"
    assert rebased.target_binding.expected_ref == RefValue.model_validate(live_ref)


def test_patch_resolve_conflicts_publishes_resolved_draft(tmp_path: Path) -> None:
    from gameforge.contracts.diff import (
        ThreeWayMergePolicyV1,
        compute_merge_policy_digest,
    )
    from gameforge.contracts.storage import RefValue
    from gameforge.platform.diff.three_way import compute_three_way_merge
    from gameforge.spine.ir.snapshot import Snapshot
    from tests.apps.api.workflow_command_testkit import apply_full_patch

    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    with _client(harness) as client:
        # intervening apply changes the SAME field the draft changes -> conflict
        apply_full_patch(harness, client, ref_name="content/head", new_value=100, key="intervening")
        harness.use_actor(maker_actor(harness))
        draft = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="resolve:draft")
        )
        assert draft.status_code == 201, draft.text
        patch_artifact = draft.json()["artifact"]["artifact_id"]
        source_approval_id = f"approval:patch:{patch_artifact}"
        live_ref = _live_ref(harness, "content/head")
        rebase = client.post(
            f"/api/v1/patches/{patch_artifact}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": source_approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
            },
            headers=headers(key="resolve:rebase"),
        )
        assert rebase.status_code == 200, rebase.text
        conflict_set_id = rebase.json()["conflict_set_id"]
        assert conflict_set_id is not None

        # Recompute the exact conflicts the service saw so resolutions cover them.
        policy = ThreeWayMergePolicyV1(
            policy_version="workflow-three-way@1",
            collection_identities=(),
            policy_digest=compute_merge_policy_digest("workflow-three-way@1", ()),
        )
        base_payload = Snapshot.from_entities_relations(
            [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})], []
        ).content_payload
        current_payload = Snapshot.from_entities_relations(
            [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 100})], []
        ).content_payload
        proposed_payload = Snapshot.from_entities_relations(
            [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 80})], []
        ).content_payload
        plan = compute_three_way_merge(base_payload, current_payload, proposed_payload, policy)
        assert plan.conflicts, "expected the intervening apply to force a conflict"
        resolutions = [
            {"conflict_id": conflict.id, "choice": "take_proposed"} for conflict in plan.conflicts
        ]

        resolve = client.post(
            f"/api/v1/patches/{patch_artifact}:resolve-conflicts",
            json={
                "request_schema_version": "resolve-conflicts-request@1",
                "approval_id": source_approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
                "conflict_set_id": conflict_set_id,
                "resolutions": resolutions,
            },
            headers=headers(key="resolve:resolve"),
        )
    assert resolve.status_code == 200, resolve.text
    body = resolve.json()
    assert body["status"] == "clean"
    new_patch_artifact_id = body["new_patch_artifact_id"]
    assert new_patch_artifact_id is not None

    resolved = harness.load_item(f"approval:patch:{new_patch_artifact_id}")
    source = harness.load_item(source_approval_id)
    assert resolved.supersedes_approval_id == source_approval_id
    assert resolved.subject_series_id == source.subject_series_id
    assert resolved.subject_revision == source.subject_revision + 1
    assert resolved.status == "draft"
    assert resolved.target_binding.expected_ref == RefValue.model_validate(live_ref)


def _live_ref(harness, ref_name: str) -> dict:
    from sqlalchemy.orm import Session

    from gameforge.runtime.persistence.cursor import CursorSigner
    from gameforge.runtime.persistence.refs import SqlRefStore
    from tests.apps.api.workflow_command_testkit import CURSOR_KEY

    with Session(harness.engine) as session:
        value = SqlRefStore(
            session,
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=harness.clock),
            clock=harness.clock,
        ).get(ref_name)
    return value.model_dump(mode="json")


def _object_generation_count(harness) -> int:
    total = 0
    cursor = None
    while True:
        page = harness.objects.list_versions(cursor)
        total += len(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return total


def _artifact_row_count(harness) -> int:
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from gameforge.runtime.persistence.models import ArtifactRow

    with Session(harness.engine) as session:
        return session.execute(select(func.count(ArtifactRow.artifact_id))).scalar_one()


def test_failed_publication_leaves_only_a_verified_gc_eligible_orphan(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        ok = client.post("/api/v1/specs", json=_spec_payload(), headers=headers(key="orphan:1"))
        assert ok.status_code == 201
        committed_generations = _object_generation_count(harness)
        committed_artifacts = _artifact_row_count(harness)
        # a second upload whose ref CAS is stale: the blob is put_verified BEFORE the
        # failing write transaction, so it becomes a referenced-by-nothing orphan.
        failed = client.post(
            "/api/v1/specs",
            json=_spec_payload(content_payload={"kind": "spec", "reward_gold": 7}),
            headers=headers(key="orphan:2"),
        )
    assert failed.status_code == 409
    # the DB authority is unchanged: still exactly one committed spec Artifact and ref@1
    assert _artifact_row_count(harness) == committed_artifacts
    # the object store gained exactly one verified orphan generation with no Artifact row
    assert _object_generation_count(harness) == committed_generations + 1
