from __future__ import annotations

import asyncio
import json

from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.apps.worker.components import WorkerArtifactBlobReader
from tests.e2e.m4c.test_agent_draft_terminal_audit import _execution_plan, _model_authorities
from tests.e2e.m4c.test_journey_b import _headers, _login, _start_api, _stop_api
from tests.e2e.m4c.test_journey_b import _drive
from tests.e2e.m4d_support.project_live import (
    ADMIN_LOGIN,
    ADMIN_PASSWORD,
    _ProjectTransport,
    _prepare_workspace,
    _project_api_config,
)


def test_project_launcher_provisions_a_real_platform_admin_and_retained_model_authority(
    tmp_path,
) -> None:
    harness, authorities = _prepare_workspace(
        tmp_path,
        tmp_path / "project-live-manifest.json",
        tmp_path / "project-live-transport.log",
    )
    assert isinstance(authorities.transport, _ProjectTransport)

    api = _start_api(_project_api_config(harness))
    try:
        admin = _login(api, ADMIN_LOGIN, ADMIN_PASSWORD)
        response = admin.client.post(
            "/api/v1/projects",
            json={
                "request_schema_version": "project-create-request@1",
                "project_key": "launcher-smoke",
                "display_name": "Launcher Smoke",
                "description": "Real project authority.",
                "genre": "Test",
                "domain_scope": {"domain_ids": ["builtin"]},
            },
            headers=_headers(admin, idempotency_key="project-live:create"),
        )
        assert response.status_code == 201, response.text
        assert response.json()["project_id"].startswith("project:")
    finally:
        _stop_api(api)


def test_project_launcher_drives_feishu_material_to_a_normalized_editable_draft(
    tmp_path,
) -> None:
    harness, authorities = _prepare_workspace(
        tmp_path,
        tmp_path / "project-live-manifest.json",
        tmp_path / "project-live-transport.log",
    )
    api = _start_api(_project_api_config(harness))
    worker = build_worker_process(
        harness.worker_config(),
        model_execution_authorities=authorities,
    )
    try:
        admin = _login(api, ADMIN_LOGIN, ADMIN_PASSWORD)
        project = admin.client.post(
            "/api/v1/projects",
            json={
                "request_schema_version": "project-create-request@1",
                "project_key": "sky-harbor",
                "display_name": "天空港计划",
                "description": "玩家经营一座漂浮在云海中的天空港。",
                "genre": "叙事经营",
                "domain_scope": {"domain_ids": ["builtin"]},
            },
            headers=_headers(admin, idempotency_key="project-live:create-sky-harbor"),
        )
        assert project.status_code == 201, project.text
        project_id = project.json()["project_id"]
        material = admin.client.post(
            f"/api/v1/projects/{project_id}/materials:text",
            json={
                "request_schema_version": "project-material-text-request@1",
                "display_name": "天空港核心创意",
                "source_format": "feishu_blocks_json",
                "text": (
                    '{"blocks":['
                    '{"block_type":3,"heading1":{"elements":['
                    '{"text_run":{"content":"世界观"}}]}},'
                    '{"block_type":2,"text":{"elements":['
                    '{"text_run":{"content":"天空港由天气管理员维护。"}}]}},'
                    '{"block_type":12,"bullet":{"elements":['
                    '{"text_run":{"content":"空气质量 air.quality 与 air_quality 是同一属性"}}]}}'
                    "]}"
                ),
            },
            headers=_headers(admin, idempotency_key="project-live:add-material"),
        )
        assert material.status_code == 201, material.text
        extraction = admin.client.post(
            f"/api/v1/projects/{project_id}/extractions",
            json={
                "request_schema_version": "project-extraction-create-request@1",
                "material_ids": [material.json()["material_id"]],
                "planning_scope": "auto",
                "objective_goal_text": "完整提取实体、属性和关系草案。",
                "llm_execution_mode": "record",
                "candidate_export_profiles": [],
                "cassette_artifact_id": None,
                "execution_version_plan": None,
                "generation_policy": None,
            },
            headers=_headers(admin, idempotency_key="project-live:extract"),
        )
        assert extraction.status_code == 202, extraction.text
        terminal = asyncio.run(_drive(worker.dispatcher, admin, extraction.json()["run_id"]))
        failure = None
        if terminal.failure_artifact_id is not None:
            failure_reader = WorkerArtifactBlobReader(
                engine=worker.runtime.engine,
                object_store=worker.runtime.object_store,
                object_store_id=worker.runtime.config.object_store_id,
                cursor_signing_key=b"p" * 32,
                clock=harness.clock,
            )
            failure = json.loads(
                failure_reader.read_bytes(terminal.failure_artifact_id).decode("utf-8")
            )
            failure["debug_evidence"] = [
                json.loads(failure_reader.read_bytes(artifact_id).decode("utf-8"))
                for artifact_id in failure["evidence_artifact_ids"]
            ]
        assert terminal.status == "succeeded", json.dumps(
            failure, ensure_ascii=False, sort_keys=True
        )

        loaded = admin.client.get(
            f"/api/v1/projects/{project_id}/extractions/{extraction.json()['extraction_id']}"
        )
        assert loaded.status_code == 200, loaded.text
        payload = loaded.json()
        assert payload["status"] == "ready"
        assert payload["preview_snapshot_artifact_id"] is not None
        assert payload["normalization_summary"]["auto_merge_count"] == 1
        assert payload["normalization_summary"]["blocking_conflict_count"] == 0
        air_alias = next(
            group
            for group in payload["alias_groups"]
            if {"air.quality", "air_quality"}.issubset(group["aliases"])
        )
        assert air_alias["canonical_identity"] == "status_effect:air_quality"

        # A platform administrator must be able to see the governed proposal that
        # the successful Agent run just materialized.  A missing read grant used to
        # make this collection silently look empty even though the Artifact and its
        # ApprovalItem were both retained correctly.
        _unused_authorities, catalog, routing = _model_authorities()
        constraint = admin.client.post(
            "/api/v1/constraint-proposals:propose",
            json={
                "request_schema_version": "constraint-propose-request@1",
                "source_artifact_ids": [material.json()["rendered_source_artifact_id"]],
                "base_constraint_snapshot_artifact_id": None,
                "authoring_goal_text": "提取天空港任务结构规则。",
                "domain_scope": {"domain_ids": ["builtin"]},
                "dsl_grammar_version": "dsl@1",
                "extraction_policy": {
                    "profile_id": "builtin.constraint_extraction",
                    "version": 1,
                },
                "llm_execution_mode": "record",
                "execution_version_plan": _execution_plan(catalog, routing).model_dump(mode="json"),
                "cassette_artifact_id": None,
            },
            headers=_headers(
                admin,
                idempotency_key="project-live:propose-constraint",
            ),
        )
        assert constraint.status_code == 202, constraint.text
        constraint_terminal = asyncio.run(
            _drive(worker.dispatcher, admin, constraint.json()["run_id"])
        )
        assert constraint_terminal.status == "succeeded"
        proposals = admin.client.get("/api/v1/constraint-proposals", params={"limit": 100})
        assert proposals.status_code == 200, proposals.text
        generated = [
            item
            for item in proposals.json()["items"]
            if item["proposal"]["producer_run_id"] == constraint.json()["run_id"]
        ]
        assert len(generated) == 1
        assert generated[0]["approval_status"] == "draft"

        # The Agent Patch remains an extraction candidate.  It must never be
        # projected as the planner-confirmed publication draft, because that would
        # let a failed graph save silently fall back to the unedited AI result.
        current_project = admin.client.get(f"/api/v1/projects/{project_id}")
        assert current_project.status_code == 200, current_project.text
        assert current_project.json()["latest_patch_artifact_id"] is None
        assert current_project.json()["latest_approval_id"] is None

        content_draft = admin.client.post(
            f"/api/v1/projects/{project_id}/content-drafts",
            json={
                "request_schema_version": "project-graph-draft-request@1",
                "source_extraction_id": payload["extraction_id"],
                "expected_source_extraction_revision": payload["revision"],
                "expected_project_revision": current_project.json()["revision"],
                "entities": [
                    {
                        "id": "npc:weather_keeper",
                        "type": "NPC",
                        "attrs": {
                            "display_name": "天气管理员",
                            "role": "维护天空港气候",
                        },
                    },
                    {
                        "id": "region:sky_harbor",
                        "type": "REGION",
                        "attrs": {"display_name": "天空港"},
                    },
                    {
                        "id": "status_effect:air_quality",
                        "type": "STATUS_EFFECT",
                        "attrs": {"display_name": "空气质量", "value": "clean"},
                    },
                    {
                        "id": "npc:new_1",
                        "type": "NPC",
                        "attrs": {"display_name": "云港向导"},
                    },
                ],
                "relations": [
                    {
                        "id": "rel:weather_keeper_location",
                        "type": "LOCATED_IN",
                        "src_id": "npc:weather_keeper",
                        "dst_id": "region:sky_harbor",
                        "attrs": {},
                    },
                    {
                        "id": "rel:guide_location",
                        "type": "LOCATED_IN",
                        "src_id": "npc:new_1",
                        "dst_id": "region:sky_harbor",
                        "attrs": {},
                    },
                ],
                "rationale": "策划已确认首个天空港内容版本。",
                "candidate_export_profiles": [],
                "side_effect_risk": "low",
            },
            headers={
                "Idempotency-Key": "project-live:create-confirmed-content-draft",
                "If-Match": current_project.headers["ETag"],
                "X-CSRF-Token": admin.csrf,
            },
        )
        assert content_draft.status_code == 201, content_draft.text
        confirmed = content_draft.json()
        assert confirmed["patch"]["produced_by"] == "human"
        assert "云港向导" in json.dumps(confirmed["patch"], ensure_ascii=False)
        assert confirmed["artifact"]["artifact_id"] != payload["patch_artifact_id"]

        refreshed_project = admin.client.get(f"/api/v1/projects/{project_id}")
        assert refreshed_project.status_code == 200, refreshed_project.text
        assert (
            refreshed_project.json()["latest_patch_artifact_id"]
            == (confirmed["artifact"]["artifact_id"])
        )
        assert refreshed_project.json()["latest_approval_id"] == (
            f"approval:patch:{confirmed['artifact']['artifact_id']}"
        )
        bound_extraction = admin.client.get(
            f"/api/v1/projects/{project_id}/extractions/{payload['extraction_id']}"
        )
        assert bound_extraction.status_code == 200, bound_extraction.text
        assert (
            bound_extraction.json()["publication_patch_artifact_id"]
            == (confirmed["artifact"]["artifact_id"])
        )
        assert bound_extraction.json()["publication_approval_id"] == (
            f"approval:patch:{confirmed['artifact']['artifact_id']}"
        )
        assert bound_extraction.json()["revision"] == payload["revision"] + 1

        approval_id = refreshed_project.json()["latest_approval_id"]
        approval = admin.client.get(f"/api/v1/approvals/{approval_id}")
        assert approval.status_code == 200, approval.text
        approval_item = approval.json()["approval"]
        validation = admin.client.post(
            f"/api/v1/patches/{confirmed['artifact']['artifact_id']}:validate",
            json={
                "request_schema_version": "patch-validation-admission-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": approval_item["workflow_revision"],
                "expected_subject_head_revision": approval_item["subject_revision"],
                "subject_digest": confirmed["artifact"]["payload_hash"],
                "base_snapshot_artifact_id": current_project.json()[
                    "bootstrap_snapshot_artifact_id"
                ],
                "preview_snapshot_artifact_id": approval_item["target_binding"][
                    "target_artifact_id"
                ],
                "constraint_snapshot_artifact_id": None,
                "candidate_config_export_artifact_ids": [],
                "target": {
                    "ref_name": approval_item["target_binding"]["ref_name"],
                    "expected_ref": None,
                },
                "validation_policy": {
                    "profile_id": "builtin.validation",
                    "version": 1,
                },
                "checker_profiles": [{"profile_id": "builtin.checker", "version": 1}],
                "simulation_profiles": [],
                "expected_findings": [],
                "findings": [],
                "review_artifact_ids": [],
                "playtest_trace_artifact_ids": [],
                "regression_suite_artifact_ids": [],
                "seed": None,
            },
            headers={
                "Idempotency-Key": "project-live:validate-confirmed-content-draft",
                "If-Match": content_draft.headers["ETag"],
                "X-CSRF-Token": admin.csrf,
            },
        )
        assert validation.status_code == 202, validation.text
        validation_terminal = asyncio.run(
            _drive(worker.dispatcher, admin, validation.json()["run_id"])
        )
        assert validation_terminal.status == "succeeded"

        validated_patch = admin.client.get(
            f"/api/v1/patches/{confirmed['artifact']['artifact_id']}"
        )
        assert validated_patch.status_code == 200, validated_patch.text
        validation_failure = None
        if validated_patch.json()["validation_status"] != "passed":
            failed_approval = admin.client.get(f"/api/v1/approvals/{approval_id}")
            evidence_id = failed_approval.json()["approval"]["evidence_set_artifact_id"]
            failure_reader = WorkerArtifactBlobReader(
                engine=worker.runtime.engine,
                object_store=worker.runtime.object_store,
                object_store_id=worker.runtime.config.object_store_id,
                cursor_signing_key=b"p" * 32,
                clock=harness.clock,
            )
            validation_failure = json.loads(failure_reader.read_bytes(evidence_id).decode("utf-8"))
            validation_failure["supporting_evidence"] = [
                json.loads(failure_reader.read_bytes(artifact_id).decode("utf-8"))
                for artifact_id in validation_failure["supporting_artifact_ids"]
            ]
        assert validated_patch.json()["validation_status"] == "passed", json.dumps(
            validation_failure,
            ensure_ascii=False,
            sort_keys=True,
        )
    finally:
        worker.close()
        _stop_api(api)
