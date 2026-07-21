"""Focused tests for the constraint-publication browser launcher fixture."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import ArtifactRow, RunRow
from tests.e2e.m4c.test_agent_draft_terminal_audit import _execution_plan, _model_authorities
from tests.e2e.m4c.test_constraint_publication import (
    _proposal_for_run,
)
from tests.e2e.m4c.test_journey_b import (
    MAKER_LOGIN,
    MAKER_PASSWORD,
    _approval,
    _login,
    _run,
    _start_api,
    _stop_api,
)
from tests.e2e.m4d_support.constraint_live import (
    _ConstraintPublicationTransport,
    _prepare_workspace,
    _record_request,
    _run_worker,
    _validated_web_origin,
)


def _run_count(database_url: str) -> int:
    engine = get_engine(database_url)
    try:
        with Session(engine) as session:
            return int(session.scalar(select(func.count()).select_from(RunRow)) or 0)
    finally:
        engine.dispose()


def _artifact_kind(database_url: str, artifact_id: str) -> str | None:
    engine = get_engine(database_url)
    try:
        with Session(engine) as session:
            return session.scalar(
                select(ArtifactRow.kind).where(ArtifactRow.artifact_id == artifact_id)
            )
    finally:
        engine.dispose()


def test_fresh_workspace_records_once_then_reuses_persisted_authority(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest_path = tmp_path / "constraint-live-manifest.json"
    transport_log = tmp_path / "constraint-live-transport.log"

    first, manifest, _ = _prepare_workspace(
        workspace,
        manifest_path,
        transport_log=transport_log,
    )
    manifest_bytes = manifest_path.read_bytes()
    transport_bytes = transport_log.read_bytes()
    assert transport_bytes == b"extraction\n"
    assert json.loads(manifest_bytes) == manifest
    assert set(manifest) == {
        "source_artifact_id",
        "record_source_run_id",
        "record_proposal_artifact_id",
    }
    assert _run_count(first.database_url) == 1

    api = _start_api(first.api_config())
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        assert _artifact_kind(first.database_url, manifest["source_artifact_id"]) == "source_raw"
        record = _run(maker, manifest["record_source_run_id"])
        assert record.status == "succeeded"
        assert record.terminal_cassette_artifact_id is not None
        proposal = _proposal_for_run(maker, manifest["record_source_run_id"])
        assert proposal["artifact"]["artifact_id"] == manifest["record_proposal_artifact_id"]
        assert proposal["proposal"]["constraints"] == [
            {
                "id": "C_quest_acyclic",
                "dsl_grammar_version": "dsl@1",
                "kind": "structural",
                "oracle": "deterministic",
                "predicates": [],
                "scope": None,
                "forall": None,
                "assert": "quest_step_dependency_graph_is_acyclic",
                "severity": "major",
                "note": "quest-step dependency graph must remain acyclic",
            }
        ]
        approval = _approval(
            maker,
            f"approval:constraint_proposal:{manifest['record_proposal_artifact_id']}",
        )
        assert approval.status == "draft"
        assert approval.subject_revision == 1
    finally:
        _stop_api(api)

    second, retained_manifest, _ = _prepare_workspace(
        workspace,
        manifest_path,
        transport_log=transport_log,
    )
    assert retained_manifest == manifest
    assert manifest_path.read_bytes() == manifest_bytes
    assert transport_log.read_bytes() == transport_bytes
    assert _run_count(second.database_url) == 1
    assert second.database_url == first.database_url
    assert second.object_root == first.object_root


def test_retained_worker_replays_with_bootstrap_authorities_without_provider_call(
    tmp_path: Path,
) -> None:
    transport_log = tmp_path / "constraint-live-transport.log"
    harness, manifest, authorities = _prepare_workspace(
        tmp_path / "workspace",
        tmp_path / "constraint-live-manifest.json",
        transport_log=transport_log,
    )
    transport_bytes = transport_log.read_bytes()
    assert transport_bytes == b"extraction\n"
    expected, catalog, routing = _model_authorities()
    assert authorities.snapshots.manifests == expected.snapshots.manifests
    assert tuple(
        (binding.binding_id, binding.agent_node_id, binding.prompt_version)
        for binding in authorities.prompt_renderer.retained_bindings
    ) == tuple(
        (binding.binding_id, binding.agent_node_id, binding.prompt_version)
        for binding in expected.prompt_renderer.retained_bindings
    )
    assert (
        authorities.circuit_breaker_resolver.model_snapshot_ids
        == expected.circuit_breaker_resolver.model_snapshot_ids
    )
    assert isinstance(authorities.transport, _ConstraintPublicationTransport)
    assert authorities.transport.calls == 0

    api = _start_api(harness.api_config())
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(_run_worker, harness, stop, authorities)
        try:
            maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
            record = _run(maker, manifest["record_source_run_id"])
            assert record.terminal_cassette_artifact_id is not None
            body = _record_request(
                manifest["source_artifact_id"],
                _execution_plan(catalog, routing).model_dump(mode="json"),
            )
            response = maker.client.post(
                "/api/v1/constraint-proposals:propose",
                json={
                    **body,
                    "llm_execution_mode": "replay",
                    "cassette_artifact_id": record.terminal_cassette_artifact_id,
                },
                headers={
                    "Idempotency-Key": "constraint-live:retained-replay",
                    "X-CSRF-Token": maker.csrf,
                },
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                terminal = _run(maker, run_id)
                if terminal.status in {"succeeded", "failed", "cancelled", "timed_out"}:
                    break
                time.sleep(0.02)
            assert terminal.status == "succeeded"
            assert authorities.transport.calls == 0
            assert transport_log.read_bytes() == transport_bytes
        finally:
            stop.set()
            worker.result(timeout=10)
            _stop_api(api)


@pytest.mark.parametrize(
    "value",
    (
        "http://127.0.0.1:4173",
        "https://example.com:4173",
        "https://127.0.0.1",
        "https://user@127.0.0.1:4173",
        "https://127.0.0.1:0",
        "https://127.0.0.1:65536",
        "https://127.0.0.1:4173/console",
        "https://127.0.0.1:4173?debug=true",
    ),
)
def test_web_origin_rejects_non_loopback_or_non_origin_values(value: str) -> None:
    with pytest.raises(SystemExit):
        _validated_web_origin(value)


def test_web_origin_accepts_dynamic_loopback_https_port() -> None:
    assert _validated_web_origin("https://127.0.0.1:49152/") == "https://127.0.0.1:49152"


def test_partial_persisted_fixture_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "journey-b.db").touch()

    with pytest.raises(RuntimeError, match="database and manifest to exist together"):
        _prepare_workspace(workspace, tmp_path / "missing-manifest.json")
