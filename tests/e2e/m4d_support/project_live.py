"""Loopback-only real API/worker launcher for the project-first browser journey."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

import uvicorn
from sqlalchemy.orm import Session

from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.apps.worker.model_authority import WorkerModelExecutionAuthorities
from gameforge.contracts.identity import DomainRegistryV1, RolePolicy
from gameforge.platform.identity.role_policy import builtin_role_policy
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from tests.e2e.m4c.test_agent_draft_terminal_audit import _model_authorities
from tests.e2e.m4c.test_journey_a import _Harness, _JourneyTransport, _seed_model_authority
from tests.e2e.m4c.test_journey_b import _shared_budget
from tests.e2e.m4d_support.journey_a_live import _retained_harness
from tests.e2e.m4d_support.journey_b_live import (
    _install_loopback_egress_guard,
    _is_loopback_host,
)


ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "admin-password-1"
_DATABASE_NAME = "journey-b.db"
_MANIFEST_SCHEMA = "project-live-fixture@1"
_ROLE_POLICY_VERSION = "project-live-roles@1"
_EFFECTIVE_FROM = "2026-07-24T00:00:00Z"
_PROJECT_GENERATION_OPS = json.dumps(
    [
        {
            "op_id": "project:add-weather-keeper",
            "op": "add_entity",
            "target": "weather.keeper",
            "new_value": {
                "type": "NPC",
                "attrs": {
                    "display_name": "天气管理员",
                    "pos": [2, 1],
                    "region": "region:sky_harbor",
                    "role": "维护天空港气候",
                },
            },
        },
        {
            "op_id": "project:add-sky-harbor",
            "op": "add_entity",
            "target": "sky.harbor",
            "new_value": {
                "type": "REGION",
                "attrs": {
                    "display_name": "天空港",
                    "grid": {"blocked": [], "height": 8, "width": 12},
                    "scenario_id": "sky_harbor",
                    "start_pos": [0, 0],
                },
            },
        },
        {
            "op_id": "project:add-air-quality-effect",
            "op": "add_entity",
            "target": "effect:atmosphere_quality_modifier",
            "new_value": {
                "type": "EFFECT",
                "attrs": {
                    "display_name": "空气质量影响",
                    "duration": 1,
                    "kind": "buff",
                    "magnitude": 1,
                    "stat": "air_quality",
                },
            },
        },
        {
            "op_id": "project:add-air-quality-dot",
            "op": "add_entity",
            "target": "air.quality",
            "new_value": {
                "type": "STATUS_EFFECT",
                "attrs": {
                    "display_name": "空气质量",
                    "duration": 1,
                    "effect_id": "effect:atmosphere_quality_modifier",
                    "value": "clean",
                },
            },
        },
        {
            "op_id": "project:add-air-quality-underscore",
            "op": "add_entity",
            "target": "air_quality",
            "new_value": {
                "type": "STATUS_EFFECT",
                "attrs": {
                    "display_name": "空气质量",
                    "duration": 1,
                    "effect_id": "effect:atmosphere_quality_modifier",
                    "value": "clean",
                },
            },
        },
        {
            "op_id": "project:locate-weather-keeper",
            "op": "add_relation",
            "target": "weather.keeper.location",
            "new_value": {
                "type": "LOCATED_IN",
                "src_id": "weather.keeper",
                "dst_id": "sky.harbor",
                "attrs": {},
            },
        },
        {
            "op_id": "project:bind-air-quality-effect",
            "op": "add_relation",
            "target": "rel:air_quality_effect",
            "new_value": {
                "type": "APPLIES_EFFECT",
                "src_id": "air.quality",
                "dst_id": "effect:atmosphere_quality_modifier",
                "attrs": {},
            },
        },
    ],
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
_PROJECT_CONSTRAINT_PROPOSALS = json.dumps(
    [
        {
            "proposed_id": "C_sky_harbor_quest_acyclic",
            "kind": "structural",
            "assert_expr": "quest_step_dependency_graph_is_acyclic",
            "rationale": "quest-step dependency graph must remain acyclic",
        }
    ],
    sort_keys=True,
    separators=(",", ":"),
)
_PROJECT_CONTINUATION_OPS = json.dumps(
    [
        {
            "op_id": "project:add-storm-observer",
            "op": "add_entity",
            "target": "npc:storm_observer",
            "new_value": {
                "type": "NPC",
                "attrs": {
                    "display_name": "风暴观测员",
                    "pos": [3, 1],
                    "region": "region:sky_harbor",
                    "role": "记录天空港周边风暴",
                },
            },
        },
        {
            "op_id": "project:locate-storm-observer",
            "op": "add_relation",
            "target": "rel:storm_observer_location",
            "new_value": {
                "type": "LOCATED_IN",
                "src_id": "npc:storm_observer",
                "dst_id": "region:sky_harbor",
                "attrs": {},
            },
        },
    ],
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)


class _ProjectTransport(_JourneyTransport):
    """Hermetic model transport for browser-created project requests."""

    def __init__(self, log_path: Path) -> None:
        super().__init__()
        self._log_path = log_path

    def complete_with_timeout(self, request, *, timeout_s: float):
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{request.agent_node_id}\n")
        return super().complete_with_timeout(request, timeout_s=timeout_s)

    def _response(self, request) -> str:
        if request.agent_node_id == "generation":
            prompt = "\n".join(message.content for message in request.messages)
            if "风暴观测员" in prompt:
                return _PROJECT_CONTINUATION_OPS
            return _PROJECT_GENERATION_OPS
        if request.agent_node_id == "extraction":
            return _PROJECT_CONSTRAINT_PROPOSALS
        return super()._response(request)


def _platform_role_policy(registry: DomainRegistryV1, base: RolePolicy) -> RolePolicy:
    """Install the product default policy, keeping this workspace's business roles."""

    return builtin_role_policy(
        registry,
        policy_version=_ROLE_POLICY_VERSION,
        effective_from=_EFFECTIVE_FROM,
        extra_grants=base.grants,
    )


def _install_platform_policy(harness: _Harness) -> None:
    policy = _platform_role_policy(harness.registry, harness.role_policy)
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            SqlPolicySnapshotRepository(session, clock=harness.clock).put_role_policy(policy)
    finally:
        engine.dispose()
    harness.role_policy = policy


def _bootstrap_workspace(workspace: Path, manifest_path: Path) -> _Harness:
    harness = _Harness(workspace)
    harness.seed_authoring_inputs()
    _install_platform_policy(harness)
    harness._provision_human(
        principal_id="human:admin",
        login=ADMIN_LOGIN,
        password=ADMIN_PASSWORD,
        display_name="Platform Admin",
        roles=("platform_admin", "tooling"),
    )
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            SqlCostLedger(session, clock=harness.clock).put_budget(
                _shared_budget(
                    budget_id="budget:principal:human:admin",
                    scope_kind="principal",
                    scope_id="human:admin",
                )
            )
    finally:
        engine.dispose()
    _seed_model_authority(harness)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"schema_version": _MANIFEST_SCHEMA}, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    return harness


def _prepare_workspace(
    workspace: Path,
    manifest_path: Path,
    transport_log: Path,
) -> tuple[_Harness, WorkerModelExecutionAuthorities]:
    workspace.mkdir(parents=True, exist_ok=True)
    database_exists = (workspace / _DATABASE_NAME).exists()
    manifest_exists = manifest_path.exists()
    if database_exists != manifest_exists:
        raise RuntimeError("project launcher database and manifest must exist together")
    if database_exists:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload != {"schema_version": _MANIFEST_SCHEMA}:
            raise RuntimeError("project launcher manifest is invalid")
        harness = _retained_harness(workspace)
        _install_platform_policy(harness)
    else:
        harness = _bootstrap_workspace(workspace, manifest_path)
    authorities, _, _ = _model_authorities()
    return harness, replace(authorities, transport=_ProjectTransport(transport_log))


def _project_api_config(harness: _Harness):
    """Bind native RECORD resolution to the exact retained routing authority."""

    _authorities, _catalog, routing = _model_authorities()
    return replace(
        harness.api_config(),
        execution_routing_policy_version=routing.policy_version,
        execution_routing_policy_digest=routing.routing_policy_digest,
    )


def _run_worker(
    harness: _Harness,
    stop: threading.Event,
    authorities: WorkerModelExecutionAuthorities,
) -> None:
    process = build_worker_process(
        harness.worker_config(),
        model_execution_authorities=authorities,
    )

    async def drive() -> None:
        while not stop.is_set():
            if not await process.dispatcher.dispatch_once():
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
        raise SystemExit("project launcher web origin has an invalid port") from exc
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
        raise SystemExit("project launcher accepts only one loopback HTTPS web origin")
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
        raise SystemExit("project launcher accepts only a loopback host")
    if not 1 <= args.port <= 65_535:
        raise SystemExit("project launcher port must be between 1 and 65535")
    web_origin = _validated_web_origin(args.web_origin)

    _install_loopback_egress_guard()
    harness, authorities = _prepare_workspace(
        args.workspace.resolve(),
        args.manifest.resolve(),
        args.transport_log.resolve(),
    )
    api_config = replace(
        _project_api_config(harness),
        allowed_websocket_origins=frozenset({web_origin}),
    )
    from gameforge.apps.api.local import create_readiness_closed_local_app

    app = create_readiness_closed_local_app(api_config)
    stop = threading.Event()
    worker = None
    if args.worker == "enabled":
        worker = threading.Thread(
            target=_run_worker,
            args=(harness, stop, authorities),
            daemon=True,
            name="project-first-worker",
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
                raise RuntimeError("project-first worker did not stop")


if __name__ == "__main__":
    main()
