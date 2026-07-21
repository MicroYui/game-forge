"""Loopback-only real API/worker launcher for the M4d Journey-A browser suite.

The launcher creates only the initial cassette sources that do not depend on
browser-produced Artifacts.  Later Review, Playtest, and Repair RECORD sources are
created step-by-step through the real public API by the test fixture; the product
journey continues only the corresponding distinct REPLAY Runs.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

import uvicorn
from sqlalchemy import select
from sqlalchemy.orm import Session

from gameforge.apps.worker.config_export import AureusConfigExporter
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.platform.registry import build_builtin_registry
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import ArtifactRow
from tests.e2e.m4c.test_agent_draft_terminal_audit import _model_authorities
from tests.e2e.m4c.test_journey_a import (
    _Harness,
    _JourneyTransport,
    _execution_plan,
    _generation_body,
    _journey_a_role_policy,
    _record_replay,
    _run_result,
    _seed_model_authority,
    _submit,
)
from tests.e2e.m4c.test_journey_b import (
    MAKER_LOGIN,
    MAKER_PASSWORD,
    _login,
    _registry,
    _route,
    _start_api,
    _stop_api,
)
from tests.e2e.m4d_support.journey_b_live import (
    _install_loopback_egress_guard,
    _is_loopback_host,
)
from tests.platform.m4 import apply_testkit


_DATABASE_NAME = "journey-b.db"
_MANIFEST_SCHEMA = "journey-a-live-fixture@1"
_GENERATION_GOAL = "Raise the caravan emblem requirement from three to four."
_REJECTED_GOAL = "Propose a deliberately dangling generation candidate."


class _LoggedJourneyTransport(_JourneyTransport):
    def __init__(self, log_path: Path) -> None:
        super().__init__()
        self.select("playtest_fail")
        self._log_path = log_path

    def complete_with_timeout(self, request, *, timeout_s: float):
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{request.agent_node_id}\n")
        response = super().complete_with_timeout(request, timeout_s=timeout_s)
        if request.agent_node_id == "repair":
            self.select("playtest_pass")
        return response


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Journey A launcher manifest is unavailable or invalid") from exc
    required = {
        "schema_version",
        "base_artifact_id",
        "constraint_artifact_id",
        "expected_ref",
        "generation_source_run_id",
        "gate_rejected_source_run_id",
        "record_patch_artifact_id",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise RuntimeError("Journey A launcher manifest has an unexpected shape")
    if payload.get("schema_version") != _MANIFEST_SCHEMA:
        raise RuntimeError("Journey A launcher manifest schema is unsupported")
    if any(
        not isinstance(payload.get(key), str) or not payload[key]
        for key in required - {"schema_version", "expected_ref"}
    ):
        raise RuntimeError("Journey A launcher manifest identifiers must be non-empty strings")
    expected_ref = payload.get("expected_ref")
    if (
        not isinstance(expected_ref, dict)
        or set(expected_ref) != {"artifact_id", "revision"}
        or expected_ref.get("artifact_id") != payload.get("base_artifact_id")
        or expected_ref.get("revision") != 1
    ):
        raise RuntimeError("Journey A launcher manifest has an invalid initial ref")
    return payload


def _bootstrap_workspace(
    workspace: Path, manifest_path: Path
) -> tuple[_Harness, dict[str, object]]:
    harness = _Harness(workspace)
    base_id, constraint_id, expected_ref = harness.seed_authoring_inputs()
    authorities, transport, catalog, routing = _seed_model_authority(harness)
    generation_plan = _execution_plan(
        kind=RunKindRef(kind="generation.propose", version=1),
        catalog=catalog,
        routing=routing,
    )
    api = _start_api(harness.api_config())
    worker = build_worker_process(
        harness.worker_config(),
        model_execution_authorities=authorities,
    )
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        transport.select("generation")
        generation_record, generation_replay = _record_replay(
            worker,
            maker,
            path="/api/v1/generation:propose",
            body=_generation_body(
                base_artifact_id=base_id,
                constraint_artifact_id=constraint_id,
                expected_ref=expected_ref,
                plan=generation_plan,
                mode="record",
                cassette_artifact_id=None,
            ),
            key="journey-a-live:generation-bootstrap",
        )
        record_patch_id = _run_result(maker, generation_record)["primary_artifact_id"]

        transport.select("generation_reject")
        rejected = _submit(
            worker,
            maker,
            path="/api/v1/generation:propose",
            body=_generation_body(
                base_artifact_id=base_id,
                constraint_artifact_id=constraint_id,
                expected_ref=expected_ref,
                plan=generation_plan,
                mode="record",
                cassette_artifact_id=None,
            )
            | {"objective_goal_text": _REJECTED_GOAL},
            key="journey-a-live:gate-rejected-bootstrap",
            expected_status="failed",
        )
        if rejected.terminal_cassette_artifact_id is None:
            raise RuntimeError("Journey A rejected bootstrap did not retain its cassette")
    finally:
        worker.close()
        _stop_api(api)

    payload: dict[str, object] = {
        "schema_version": _MANIFEST_SCHEMA,
        "base_artifact_id": base_id,
        "constraint_artifact_id": constraint_id,
        "expected_ref": expected_ref,
        "generation_source_run_id": generation_record.run_id,
        "gate_rejected_source_run_id": rejected.run_id,
        "record_patch_artifact_id": record_patch_id,
    }
    _write_manifest(manifest_path, payload)
    return harness, payload


def _retained_harness(workspace: Path) -> _Harness:
    harness = object.__new__(_Harness)
    harness.tmp_path = workspace
    harness.database_url = f"sqlite:///{workspace / _DATABASE_NAME}"
    harness.object_root = workspace / "objects"
    harness.telemetry_path = workspace / "telemetry.sqlite3"
    harness.clock = SystemUtcClock()
    harness.registry = _registry()
    harness.route = _route(harness.registry)
    harness.approval_policy = apply_testkit._approval_policy()
    harness.role_policy = _journey_a_role_policy(harness.registry)
    harness.catalog = build_builtin_registry().list_execution_profile_catalogs()[-1]
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session:
            bench_ids = tuple(
                session.scalars(
                    select(ArtifactRow.artifact_id)
                    .where(ArtifactRow.kind == "bench_report")
                    .order_by(ArtifactRow.artifact_id)
                )
            )
    finally:
        engine.dispose()
    if len(bench_ids) != 1:
        raise RuntimeError("Journey A retained workspace lacks one exact BenchReport")
    harness.bench_report_artifact_id = bench_ids[0]
    return harness


def _prepare_workspace(workspace: Path, manifest_path: Path) -> tuple[_Harness, dict[str, object]]:
    workspace.mkdir(parents=True, exist_ok=True)
    database_exists = (workspace / _DATABASE_NAME).exists()
    manifest_exists = manifest_path.exists()
    if database_exists != manifest_exists:
        raise RuntimeError("Journey A launcher requires its database and manifest together")
    if not database_exists:
        return _bootstrap_workspace(workspace, manifest_path)
    return _retained_harness(workspace), _read_manifest(manifest_path)


def _live_model_authorities(log_path: Path):
    authorities, _catalog, _routing = _model_authorities()
    return replace(authorities, transport=_LoggedJourneyTransport(log_path))


def _install_record_only_repair_fault() -> None:
    original = AureusConfigExporter.export

    def export(self, **kwargs):
        run_kind = kwargs.get("run_kind")
        if (
            kwargs.get("llm_execution_mode") == "record"
            and isinstance(run_kind, RunKindRef)
            and run_kind.kind == "patch.repair"
        ):
            raise RuntimeError("Journey A fixture-only repair RECORD terminal fault")
        return original(self, **kwargs)

    AureusConfigExporter.export = export


def _run_worker(harness: _Harness, stop: threading.Event, transport_log: Path) -> None:
    process = build_worker_process(
        harness.worker_config(),
        model_execution_authorities=_live_model_authorities(transport_log),
    )

    async def drive() -> None:
        while not stop.is_set():
            claimed = await process.dispatcher.dispatch_once()
            if not claimed:
                await asyncio.sleep(0.05)

    try:
        asyncio.run(drive())
    finally:
        process.close()


def _validated_web_origin(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SystemExit("Journey A launcher web origin has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not _is_loopback_host(parsed.hostname)
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("Journey A launcher accepts only one loopback HTTPS web origin")
    return value.rstrip("/")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--worker", choices=("disabled", "enabled"), default="enabled")
    parser.add_argument("--web-origin", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--transport-log", required=True, type=Path)
    args = parser.parse_args()
    if not _is_loopback_host(args.host):
        raise SystemExit("Journey A launcher accepts only a loopback host")
    if not 1 <= args.port <= 65_535:
        raise SystemExit("Journey A launcher port must be between 1 and 65535")
    web_origin = _validated_web_origin(args.web_origin)

    _install_loopback_egress_guard()
    harness, _ = _prepare_workspace(args.workspace.resolve(), args.manifest.resolve())
    _install_record_only_repair_fault()
    _, _, routing = _model_authorities()
    api_config = replace(
        harness.api_config(),
        allowed_websocket_origins=frozenset({web_origin}),
        execution_routing_policy_version=routing.policy_version,
        execution_routing_policy_digest=routing.routing_policy_digest,
    )
    from gameforge.apps.api.local import create_readiness_closed_local_app

    app = create_readiness_closed_local_app(api_config)
    stop = threading.Event()
    worker = None
    if args.worker == "enabled":
        worker = threading.Thread(
            target=_run_worker,
            args=(harness, stop, args.transport_log.resolve()),
            daemon=True,
            name="journey-a-worker",
        )
        worker.start()
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            access_log=False,
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
    finally:
        stop.set()
        if worker is not None:
            worker.join(timeout=30)
            if worker.is_alive():
                raise RuntimeError("Journey A worker did not stop")


if __name__ == "__main__":
    main()
