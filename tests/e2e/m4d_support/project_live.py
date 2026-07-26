"""Loopback-only real API/worker launcher for the project-first browser journey."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import replace
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit

import uvicorn
from sqlalchemy.orm import Session

from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.apps.worker.model_authority import (
    DEFAULT_MODEL_GATEWAY_BASE_URL,
    MODEL_GATEWAY_API_KEY_ENV,
    MODEL_GATEWAY_BASE_URL_ENV,
    MODEL_SNAPSHOT_MANIFEST_PATH_ENV,
    WorkerModelExecutionAuthorities,
    gateway_snapshot_manifest,
    load_local_model_execution_authorities,
)
from gameforge.contracts.identity import DomainRegistryV1, RolePolicy
from gameforge.contracts.routing import (
    GatewayModelV1,
    RoutingPolicyV1,
    canonical_model_snapshot_id,
)
from gameforge.platform.identity.role_policy import builtin_role_policy
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.routing.gateway_catalog import (
    GatewayModelAuthoritySeed,
    gateway_model_snapshot,
    plan_gateway_model_authority,
)
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.model_router.gateway_models import fetch_gateway_models
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from tests.e2e.m4c.test_journey_a import _Harness, _JourneyTransport
from tests.e2e.m4c.test_journey_b import _shared_budget
from tests.e2e.m4d_support.journey_a_live import _retained_harness
from tests.e2e.m4d_support.stub_models import STUB_MODELS
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
_MODEL_AUTHORITY_VERSION = "gateway-models@1"
_SNAPSHOT_MANIFEST_NAME = "model-snapshots.json"
# The model every launch card starts on. A planner may pick any other catalogued
# model; that choice selects the routing policy whose rules name it.
DEFAULT_MODEL = "gpt-5.6-sol"

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
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"schema_version": _MANIFEST_SCHEMA}, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    return harness


def _agent_nodes() -> dict[str, tuple[str, ...]]:
    """Every Agent node a run can reach, with the capabilities it declares."""

    nodes: dict[str, tuple[str, ...]] = {}
    for graph in build_builtin_registry().list_agent_execution_graphs():
        if graph.status not in {"active", "replay_only"}:
            continue
        for node in graph.nodes:
            declared = set(nodes.get(node.agent_node_id, ())) | set(node.required_capabilities)
            nodes[node.agent_node_id] = tuple(sorted(declared))
    return nodes


def _seed_routing_authority(
    harness: _Harness,
    models: Sequence[GatewayModelV1],
) -> GatewayModelAuthoritySeed:
    """Retain the catalog and the per-model policies this workspace can route to."""

    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            costs = SqlCostLedger(session, clock=harness.clock)
            seed = plan_gateway_model_authority(
                models,
                agent_nodes=_agent_nodes(),
                retained_catalogs=costs.list_model_catalogs(),
                created_at=harness.clock.now_utc(),
            )
            costs.put_model_catalog(seed.catalog)
            for policy in seed.policies:
                costs.put_routing_policy(policy)
    finally:
        engine.dispose()
    return seed


def _default_routing_policy(
    seed: GatewayModelAuthoritySeed,
    models: Sequence[GatewayModelV1],
) -> RoutingPolicyV1:
    default = next((model for model in models if model.model == DEFAULT_MODEL), None)
    if default is None:
        raise SystemExit(f"model gateway does not serve the default model {DEFAULT_MODEL}")
    wanted = canonical_model_snapshot_id(gateway_model_snapshot(default))
    return next(
        policy
        for policy in seed.policies
        if all(rule.primary_model_snapshot == wanted for rule in policy.rules)
    )


def _deployment_authorities(
    workspace: Path,
    models: Sequence[GatewayModelV1],
    *,
    base_url: str,
    api_key: str,
) -> WorkerModelExecutionAuthorities:
    """Compose the worker exactly as the production loader does.

    One gateway reading produces both the retained catalog and this deployment's
    snapshot manifest, so the identity a plan names and the request the worker
    builds always describe the same model on the same surface.
    """

    manifest_path = workspace / _SNAPSHOT_MANIFEST_NAME
    manifest_path.write_text(
        gateway_snapshot_manifest(
            models, authority_version=_MODEL_AUTHORITY_VERSION
        ).model_dump_json(),
        encoding="utf-8",
    )
    return load_local_model_execution_authorities(
        environment={
            MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(manifest_path),
            MODEL_GATEWAY_BASE_URL_ENV: base_url,
            MODEL_GATEWAY_API_KEY_ENV: api_key,
        }
    )


def _stub_authorities(
    workspace: Path,
    *,
    transport_log: Path,
) -> WorkerModelExecutionAuthorities:
    """The browser journey's stand-in for the gateway.

    Same composition as a real deployment — the seeded catalog, policies and
    manifest are identical in shape — with a fixed responder in place of the model,
    so the journey can assert exact authored content with no network at all.
    """

    authorities = _deployment_authorities(
        workspace,
        STUB_MODELS,
        base_url=DEFAULT_MODEL_GATEWAY_BASE_URL,
        api_key="stub-gateway-key",
    )
    authorities.transport.close()  # type: ignore[attr-defined]
    return replace(authorities, transport=_ProjectTransport(transport_log))


def _prepare_workspace(
    workspace: Path,
    manifest_path: Path,
    models: Sequence[GatewayModelV1],
) -> tuple[_Harness, GatewayModelAuthoritySeed]:
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
    return harness, _seed_routing_authority(harness, models)


def _project_api_config(harness: _Harness, policy: RoutingPolicyV1):
    """Bind native RECORD resolution to the exact retained routing authority."""

    return replace(
        harness.api_config(),
        execution_routing_policy_version=policy.policy_version,
        execution_routing_policy_digest=policy.routing_policy_digest,
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


def _gateway_credentials() -> tuple[str, str]:
    api_key = os.environ.get(MODEL_GATEWAY_API_KEY_ENV)
    if not api_key:
        raise SystemExit(f"{MODEL_GATEWAY_API_KEY_ENV} must hold the model gateway key")
    return os.environ.get(MODEL_GATEWAY_BASE_URL_ENV, DEFAULT_MODEL_GATEWAY_BASE_URL), api_key


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--worker", choices=("disabled", "enabled"), default="enabled")
    parser.add_argument("--web-origin", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--transport-log", required=True, type=Path)
    # The browser journey asserts exact authored content, so it routes to a fixed
    # stand-in instead of a model. A hands-on stack routes to the real gateway.
    parser.add_argument("--models", choices=("stub", "gateway"), default="stub")
    args = parser.parse_args()
    if not _is_loopback_host(args.host):
        raise SystemExit("project launcher accepts only a loopback host")
    if not 1 <= args.port <= 65_535:
        raise SystemExit("project launcher port must be between 1 and 65535")
    web_origin = _validated_web_origin(args.web_origin)

    _install_loopback_egress_guard()
    workspace = args.workspace.resolve()
    manifest_path = args.manifest.resolve()
    if args.models == "gateway":
        base_url, api_key = _gateway_credentials()
        models = fetch_gateway_models(base_url=base_url, api_key=api_key)
        harness, seed = _prepare_workspace(workspace, manifest_path, models)
        authorities = _deployment_authorities(
            workspace,
            models,
            base_url=base_url,
            api_key=api_key,
        )
    else:
        models = STUB_MODELS
        harness, seed = _prepare_workspace(workspace, manifest_path, models)
        authorities = _stub_authorities(workspace, transport_log=args.transport_log.resolve())
    api_config = replace(
        _project_api_config(harness, _default_routing_policy(seed, models)),
        allowed_websocket_origins=frozenset({web_origin}),
    )
    from gameforge.apps.api.local import create_readiness_closed_local_app

    # The picker reads the same models this process routes to: the gateway when it
    # is real, the fixed stand-in when the browser journey needs a fixed answer.
    app = create_readiness_closed_local_app(
        api_config,
        gateway_models=None if args.models == "gateway" else (lambda: STUB_MODELS),
    )
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
