"""Loopback-only real API/worker launcher for M4d constraint publication.

The first launch creates one retained RECORD source Run as fixture bootstrap.  The
browser receives only that Run ID and exercises the product journey in REPLAY mode.
Subsequent launches rebuild the real API and worker over the same SQLite authority
and ObjectStore without recording again.
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

from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.apps.worker.model_authority import WorkerModelExecutionAuthorities
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.runtime.model_router.typed_transport import TransportResponseV2
from tests.e2e.m4c.test_agent_draft_terminal_audit import (
    _execution_plan,
    _model_authorities,
    _seed_source,
)
from tests.e2e.m4c.test_constraint_publication import (
    _install_constraint_role_policy,
    _proposal_for_run,
    _seed_model_authorities,
    _worker_config,
)
from tests.e2e.m4c.test_journey_b import (
    DOMAIN,
    MAKER_LOGIN,
    MAKER_PASSWORD,
    _Harness,
    _drive,
    _headers,
    _login,
    _start_api,
    _stop_api,
)
from tests.e2e.m4d_support.journey_b_live import (
    _install_loopback_egress_guard,
    _is_loopback_host,
    _retained_harness,
)


_DATABASE_NAME = "journey-b.db"
_AUTHORING_GOAL = "Extract a deterministic gold reward cap."
_PUBLICATION_PROPOSALS = json.dumps(
    [
        {
            "proposed_id": "C_quest_acyclic",
            "kind": "structural",
            "assert_expr": "quest_step_dependency_graph_is_acyclic",
            "rationale": "quest-step dependency graph must remain acyclic",
        }
    ],
    sort_keys=True,
    separators=(",", ":"),
)


class _ConstraintPublicationTransport:
    """One valid RECORD fixture; retained REPLAY must never call it."""

    def __init__(self, log_path: Path | None = None) -> None:
        self.calls = 0
        self._log_path = log_path

    def complete(self, request) -> TransportResponseV2:
        return self.complete_with_timeout(request, timeout_s=30)

    def complete_with_timeout(self, request, *, timeout_s: float) -> TransportResponseV2:
        assert timeout_s > 0
        assert request.agent_node_id == "extraction"
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as stream:
                stream.write(f"{request.agent_node_id}\n")
        self.calls += 1
        return TransportResponseV2(
            response_normalized=_PUBLICATION_PROPOSALS,
            raw_response={"id": "response:constraint-publication"},
            finish_reason="stop",
            tool_calls=(),
            latency=LatencyObservationV1(status="unavailable"),
            token_usage=TokenUsageObservationV1(status="unavailable"),
            provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
        )

    def close(self) -> None:
        return None


def _record_request(source_artifact_id: str, execution_version_plan: dict[str, object]) -> dict:
    return {
        "request_schema_version": "constraint-propose-request@1",
        "source_artifact_ids": [source_artifact_id],
        "base_constraint_snapshot_artifact_id": None,
        "authoring_goal_text": _AUTHORING_GOAL,
        "domain_scope": {"domain_ids": [DOMAIN]},
        "dsl_grammar_version": "dsl@1",
        "extraction_policy": {
            "profile_id": "builtin.constraint_extraction",
            "version": 1,
        },
        "llm_execution_mode": "record",
        "execution_version_plan": execution_version_plan,
        "cassette_artifact_id": None,
    }


def _write_manifest(path: Path, payload: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_manifest(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("constraint launcher manifest is unavailable or invalid") from exc
    expected = {
        "source_artifact_id",
        "record_source_run_id",
        "record_proposal_artifact_id",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise RuntimeError("constraint launcher manifest has an unexpected shape")
    if any(not isinstance(payload[key], str) or not payload[key] for key in expected):
        raise RuntimeError("constraint launcher manifest identifiers must be non-empty strings")
    return payload


def _bootstrap_record(
    workspace: Path,
    manifest_path: Path,
    *,
    transport_log: Path | None,
) -> None:
    harness = _Harness(workspace)
    _install_constraint_role_policy(harness)
    source_artifact_id = _seed_source(harness)
    seeded_authorities, catalog, routing = _seed_model_authorities(harness)
    authorities = replace(
        seeded_authorities,
        transport=_ConstraintPublicationTransport(transport_log),
    )
    execution_plan = _execution_plan(catalog, routing)

    api = _start_api(harness.api_config())
    process = build_worker_process(
        _worker_config(harness),
        model_execution_authorities=authorities,
    )
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        response = maker.client.post(
            "/api/v1/constraint-proposals:propose",
            json=_record_request(
                source_artifact_id,
                execution_plan.model_dump(mode="json"),
            ),
            headers=_headers(
                maker,
                idempotency_key="constraint-live:record-bootstrap",
            ),
        )
        if response.status_code != 202:
            raise RuntimeError(
                f"constraint RECORD bootstrap was rejected: {response.status_code} {response.text}"
            )
        record_source_run_id = response.json()["run_id"]
        terminal = asyncio.run(_drive(process.dispatcher, maker, record_source_run_id))
        if terminal.status != "succeeded" or terminal.terminal_cassette_artifact_id is None:
            raise RuntimeError("constraint RECORD bootstrap did not retain a successful cassette")
        proposal = _proposal_for_run(maker, record_source_run_id)
        record_proposal_artifact_id = proposal["artifact"]["artifact_id"]
        if not isinstance(authorities.transport, _ConstraintPublicationTransport):
            raise RuntimeError("constraint RECORD bootstrap used an unexpected model transport")
        if authorities.transport.calls != 1:
            raise RuntimeError("constraint RECORD bootstrap must call its fixture transport once")
    finally:
        process.close()
        _stop_api(api)

    _write_manifest(
        manifest_path,
        {
            "source_artifact_id": source_artifact_id,
            "record_source_run_id": record_source_run_id,
            "record_proposal_artifact_id": record_proposal_artifact_id,
        },
    )


def _prepare_workspace(
    workspace: Path,
    manifest_path: Path,
    *,
    transport_log: Path | None = None,
) -> tuple[_Harness, dict[str, str], WorkerModelExecutionAuthorities]:
    workspace.mkdir(parents=True, exist_ok=True)
    database_exists = (workspace / _DATABASE_NAME).exists()
    manifest_exists = manifest_path.exists()
    if database_exists != manifest_exists:
        raise RuntimeError(
            "constraint launcher requires its persisted database and manifest to exist together"
        )
    if not database_exists:
        _bootstrap_record(
            workspace,
            manifest_path,
            transport_log=transport_log,
        )

    manifest = _read_manifest(manifest_path)
    harness = _retained_harness(workspace)
    _install_constraint_role_policy(harness)
    authorities, _, _ = _model_authorities()
    authorities = replace(
        authorities,
        transport=_ConstraintPublicationTransport(transport_log),
    )
    return harness, manifest, authorities


def _run_worker(
    harness: _Harness,
    stop: threading.Event,
    model_execution_authorities: WorkerModelExecutionAuthorities,
) -> None:
    process = build_worker_process(
        _worker_config(harness),
        model_execution_authorities=model_execution_authorities,
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
        raise SystemExit("constraint launcher web origin has an invalid port") from exc
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
        raise SystemExit("constraint launcher accepts only one loopback HTTPS web origin")
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
        raise SystemExit("constraint launcher accepts only a loopback host")
    if not 1 <= args.port <= 65_535:
        raise SystemExit("constraint launcher port must be between 1 and 65535")
    web_origin = _validated_web_origin(args.web_origin)

    # This must precede migrations, fixture bootstrap, API lifespan, and worker startup.
    _install_loopback_egress_guard()
    harness, _, model_execution_authorities = _prepare_workspace(
        args.workspace.resolve(),
        args.manifest.resolve(),
        transport_log=args.transport_log.resolve(),
    )
    api_config = replace(
        harness.api_config(),
        allowed_websocket_origins=frozenset({web_origin}),
    )
    from gameforge.apps.api.local import create_readiness_closed_local_app

    app = create_readiness_closed_local_app(api_config)
    stop = threading.Event()
    worker = None
    if args.worker == "enabled":
        worker = threading.Thread(
            target=_run_worker,
            args=(harness, stop, model_execution_authorities),
            daemon=True,
            name="constraint-publication-worker",
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
                raise RuntimeError("constraint publication worker did not stop")


if __name__ == "__main__":
    main()
