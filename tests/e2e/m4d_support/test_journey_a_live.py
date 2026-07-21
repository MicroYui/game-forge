"""Focused tests for the Journey-A browser launcher fixture."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import ArtifactRow, RunRow
from gameforge.contracts.findings import FindingPayloadV1, FindingRevisionV1
from tests.e2e.m4c.test_journey_b import (
    MAKER_LOGIN,
    MAKER_PASSWORD,
    REF_NAME,
    _approval,
    _login,
    _ref_history,
    _run,
    _start_api,
    _stop_api,
)
from tests.e2e.m4d_support.journey_a_live import (
    _LoggedJourneyTransport,
    _prepare_workspace,
    _validated_web_origin,
)


def _run_count(database_url: str) -> int:
    engine = get_engine(database_url)
    try:
        with Session(engine) as session:
            return int(session.scalar(select(func.count()).select_from(RunRow)) or 0)
    finally:
        engine.dispose()


def test_fresh_workspace_bootstraps_exact_sources_and_reuses_them(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest_path = tmp_path / "journey-a-live-manifest.json"

    first, manifest = _prepare_workspace(workspace, manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    assert json.loads(manifest_bytes) == manifest
    initial_run_count = _run_count(first.database_url)
    assert initial_run_count > 0

    api = _start_api(first.api_config())
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        for field in ("generation_source_run_id", "gate_rejected_source_run_id"):
            source = _run(maker, str(manifest[field]))
            assert source.terminal_cassette_artifact_id is not None
        assert _run(maker, str(manifest["generation_source_run_id"])).status == "succeeded"
        assert _run(maker, str(manifest["gate_rejected_source_run_id"])).status == "failed"
        record_patch_id = str(manifest["record_patch_artifact_id"])
        record_item = _approval(maker, f"approval:patch:{record_patch_id}")
        assert record_item.status == "draft"
        assert _ref_history(maker) == (manifest["expected_ref"],)
    finally:
        _stop_api(api)

    second, retained = _prepare_workspace(workspace, manifest_path)
    assert retained == manifest
    assert manifest_path.read_bytes() == manifest_bytes
    assert _run_count(second.database_url) == initial_run_count
    assert second.database_url == first.database_url
    assert second.object_root == first.object_root


def test_web_origin_accepts_only_loopback_https_origin() -> None:
    assert _validated_web_origin("https://127.0.0.1:49152/") == "https://127.0.0.1:49152"


def test_repair_record_resets_the_next_playtest_fixture_script(tmp_path: Path) -> None:
    transport = _LoggedJourneyTransport(tmp_path / "transport.log")
    transport._playtest_target = 3

    repair = transport.complete_with_timeout(
        SimpleNamespace(agent_node_id="repair"),
        timeout_s=30,
    )
    first_executor = transport.complete_with_timeout(
        SimpleNamespace(
            agent_node_id="playtest.executor",
            messages=[SimpleNamespace(content="available_interactions=")],
        ),
        timeout_s=30,
    )

    assert json.loads(repair.response_normalized)[0] == {
        "op_id": "repair:emblem-count",
        "op": "set_entity_attr",
        "target": "step:collect_emblem.count",
        "old_value": 4,
        "new_value": 3,
    }
    assert json.loads(first_executor.response_normalized) == {
        "kind": "navigate_to",
        "target": "npc:lincheng",
    }


def test_manifest_initial_ref_is_the_real_content_head(tmp_path: Path) -> None:
    harness, manifest = _prepare_workspace(
        tmp_path / "workspace",
        tmp_path / "journey-a-live-manifest.json",
    )
    api = _start_api(harness.api_config())
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        history = maker.client.get(f"/api/v1/refs/{REF_NAME}/history", params={"limit": 100})
        assert history.status_code == 200, history.text
        assert history.json()["items"][0]["value"]["artifact_id"] == manifest["base_artifact_id"]
    finally:
        _stop_api(api)


def test_regression_fixture_cli_publishes_suite_from_exact_finding_revision(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    harness, manifest = _prepare_workspace(
        workspace,
        tmp_path / "journey-a-live-manifest.json",
    )
    finding = FindingRevisionV1(
        finding_id="finding:journey-a:dynamic-playtest",
        revision=4,
        supersedes_revision=3,
        created_at="2026-07-20T10:00:00Z",
        payload=FindingPayloadV1(
            source="playtest",
            producer_id="playtest.completion_oracle",
            producer_run_id="run:journey-a:dynamic-playtest",
            oracle_type="deterministic",
            defect_class="playtest_incomplete",
            severity="major",
            snapshot_id="snapshot:journey-a:failed-preview",
            evidence={
                "episode_id": "episode:quest:missing_caravan",
                "scenario_spec_artifact_id": "artifact:scenario:browser-exact",
                "terminal_reason": "step_limit_exhausted",
            },
            minimal_repro={
                "episode_id": "episode:quest:missing_caravan",
                "scenario_spec_artifact_id": "artifact:scenario:browser-exact",
                "seed_binding": {
                    "root_seed": 1,
                    "case_id": "artifact:suite:browser-exact:episode:quest:missing_caravan",
                    "seed": 5_762_605_406_822_806_921,
                },
            },
            status="confirmed",
            message="The exact browser Playtest did not complete.",
        ),
    )
    finding_path = workspace / "exact-finding.json"
    finding_path.write_text(finding.model_dump_json(), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.e2e.m4d_support.journey_a_regression_fixture",
            "--workspace",
            str(workspace),
            "--base-artifact-id",
            str(manifest["base_artifact_id"]),
            "--finding",
            str(finding_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    suite_id = completed.stdout.strip()
    assert suite_id.startswith("sha256:")
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session:
            suite_row = session.get(ArtifactRow, suite_id)
            assert suite_row is not None
            assert suite_row.lineage == [manifest["base_artifact_id"]]
    finally:
        engine.dispose()

    api = _start_api(harness.api_config())
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        response = maker.client.get(f"/api/v1/artifacts/{suite_id}")
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["artifact"]["kind"] == "regression_suite"
        case = view["payload"]["adapter_payload"]["cases"][0]
        assert case["failure_finding"]["evidence"] == finding.payload.evidence
        assert case["failure_finding"]["minimal_repro"] == finding.payload.minimal_repro
        assert (
            case["failure_finding"]["minimal_repro"]["seed_binding"]["seed"]
            == 5_762_605_406_822_806_921
        )
        assert [step["action"] for step in case["steps"]] == [
            {"kind": "navigate_to", "target": "npc:lincheng"},
            {"kind": "interact", "target": "npc:lincheng"},
            {"kind": "navigate_to", "target": "interact:emblem_pile"},
            {"kind": "interact", "target": "interact:emblem_pile"},
            {"kind": "navigate_to", "target": "npc:lincheng"},
            {"kind": "interact", "target": "npc:lincheng"},
        ]
    finally:
        _stop_api(api)
