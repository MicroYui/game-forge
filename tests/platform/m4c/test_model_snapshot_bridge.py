from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from gameforge.agents.base import call_model
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    execution_version_plan_digest,
)
from gameforge.contracts.model_router import (
    Message,
    ModelRequest,
    ModelResponse,
    ModelSnapshot,
    request_hash,
)
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
)
from gameforge.platform.run_handlers.model_routing import (
    ExactModelCatalogSnapshotResolver,
    MultiNodeBridgeRouter,
    build_bridge_router,
)


NOW = datetime(2026, 7, 16, tzinfo=UTC)


class _CatalogAuthority:
    def __init__(self, catalog: ModelCatalogSnapshotV1) -> None:
        self.catalog = catalog
        self.calls: list[tuple[int, str]] = []

    def get_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None:
        self.calls.append((catalog_version, catalog_digest))
        if (
            catalog_version == self.catalog.catalog_version
            and catalog_digest == self.catalog.catalog_digest
        ):
            return self.catalog
        return None


class _SnapshotAuthority:
    def __init__(self, bindings: dict[str, ModelSnapshot]) -> None:
        self.bindings = dict(bindings)

    def get_model_snapshot(self, model_snapshot_id: str) -> ModelSnapshot | None:
        return self.bindings.get(model_snapshot_id)


class _Bridge:
    def __init__(self, resolver: ExactModelCatalogSnapshotResolver) -> None:
        self.resolver = resolver

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ) -> ModelSnapshot:
        return self.resolver.resolve_model_snapshot(
            catalog_version=catalog_version,
            catalog_digest=catalog_digest,
            model_snapshot_id=model_snapshot_id,
        )

    def call_model(self, request: object) -> object:  # pragma: no cover - construction only
        raise AssertionError(f"unexpected model call: {request!r}")


def _catalog(snapshot: ModelSnapshot) -> tuple[ModelCatalogSnapshotV1, str]:
    model_snapshot_id = canonical_model_snapshot_id(snapshot)
    descriptor = ModelDescriptorV1(
        provider=snapshot.provider,
        model_snapshot=model_snapshot_id,
        tier="reasoning",
        capabilities=("reasoning",),
        context_limit=200_000,
        max_output_tokens=32_000,
        prompt_cache_support=True,
        status="active",
    )
    payload = {
        "catalog_version": 7,
        "models": (descriptor,),
        "created_at": NOW,
    }
    return (
        ModelCatalogSnapshotV1(
            **payload,
            catalog_digest=compute_model_catalog_digest(payload),
        ),
        model_snapshot_id,
    )


def _plan(catalog: ModelCatalogSnapshotV1, model_snapshot_id: str) -> ExecutionVersionPlanV1:
    node = PlannedAgentNodeVersionV1(
        agent_node_id="generation",
        prompt_version="generation-prompt@1",
        tool_version="generation-tool@1",
        allowed_model_snapshots=(model_snapshot_id,),
    )
    payload = {
        "agent_graph_version": "generation-graph@1",
        "nodes": (node,),
        "model_catalog_version": catalog.catalog_version,
        "model_catalog_digest": catalog.catalog_digest,
        "routing_policy_version": 11,
        "routing_policy_digest": "b" * 64,
    }
    return ExecutionVersionPlanV1(
        **payload,
        plan_digest=execution_version_plan_digest(payload),
    )


def test_plan_to_handler_resolves_opaque_id_through_exact_catalog_authority() -> None:
    snapshot = ModelSnapshot(
        provider="anthropic",
        model="claude-opus-4-8",
        snapshot_tag="m2a@1",
    )
    catalog, model_snapshot_id = _catalog(snapshot)
    catalog_authority = _CatalogAuthority(catalog)
    resolver = ExactModelCatalogSnapshotResolver(
        catalogs=catalog_authority,
        snapshots=_SnapshotAuthority({model_snapshot_id: snapshot}),
    )
    bridge = _Bridge(resolver)
    plan = _plan(catalog, model_snapshot_id)
    context = SimpleNamespace(
        payload=SimpleNamespace(
            execution_version_plan=plan,
            input_artifact_ids=("artifact:source",),
            cassette_artifact_id=None,
        ),
        run=SimpleNamespace(run_id="run:1", idempotency_scope="principal:human:a"),
        attempt=SimpleNamespace(attempt_no=1),
        deadline_utc=NOW,
        model_bridge=bridge,
    )

    router = build_bridge_router(
        context=context,
        agent_node_id="generation",
        max_prompt_message_bytes=16 * 1024 * 1024,
    )

    assert "/" not in model_snapshot_id
    assert ":sha256:" in model_snapshot_id
    assert router.default_model_snapshot == snapshot
    assert canonical_model_snapshot_id(router.default_model_snapshot) == model_snapshot_id
    assert catalog_authority.calls == [(catalog.catalog_version, catalog.catalog_digest)]


def test_plan_to_handler_rejects_non_preimage_snapshot_binding() -> None:
    snapshot = ModelSnapshot(provider="openai", model="gpt-5.6-sol", snapshot_tag="2026-07")
    catalog, model_snapshot_id = _catalog(snapshot)
    resolver = ExactModelCatalogSnapshotResolver(
        catalogs=_CatalogAuthority(catalog),
        snapshots=_SnapshotAuthority(
            {
                model_snapshot_id: ModelSnapshot(
                    provider="openai",
                    model="gpt-5.6-mini",
                    snapshot_tag="2026-07",
                )
            }
        ),
    )

    with pytest.raises(IntegrityViolation, match="structured model snapshot binding differs"):
        resolver.resolve_model_snapshot(
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.catalog_digest,
            model_snapshot_id=model_snapshot_id,
        )


def test_plan_to_handler_rejects_unretained_exact_catalog() -> None:
    snapshot = ModelSnapshot(provider="openai", model="gpt-5.6-sol", snapshot_tag="2026-07")
    catalog, model_snapshot_id = _catalog(snapshot)
    resolver = ExactModelCatalogSnapshotResolver(
        catalogs=_CatalogAuthority(catalog),
        snapshots=_SnapshotAuthority({model_snapshot_id: snapshot}),
    )

    with pytest.raises(IntegrityViolation, match="exact model catalog history is unavailable"):
        resolver.resolve_model_snapshot(
            catalog_version=catalog.catalog_version + 1,
            catalog_digest=catalog.catalog_digest,
            model_snapshot_id=model_snapshot_id,
        )


def test_multinode_router_selects_snapshot_before_params_and_request_hash() -> None:
    planner = ModelSnapshot(
        provider="anthropic",
        model="claude-opus-4-8",
        snapshot_tag="planner@1",
    )
    executor = ModelSnapshot(
        provider="openai",
        model="gpt-5.6-sol",
        snapshot_tag="executor@1",
    )

    class _Adapter:
        def __init__(self) -> None:
            self.call_count = 0
            self.calls: list[dict[str, object]] = []

        def call_model(self, **kwargs):
            self.call_count += 1
            self.calls.append(kwargs)
            return SimpleNamespace(response=ModelResponse(response_normalized="{}"))

    adapter = _Adapter()
    router = MultiNodeBridgeRouter(
        adapter=adapter,  # type: ignore[arg-type]
        node_snapshots={"playtest.planner": planner, "playtest.executor": executor},
        default_node_id="playtest.planner",
        source_artifact_ids=("artifact:source",),
    )

    _, planner_hash = call_model(
        router,
        "playtest.planner",
        "plan",
        "planner-prompt@1",
    )
    _, executor_hash = call_model(
        router,
        "playtest.executor",
        "act",
        "executor-prompt@1",
    )

    planner_request = ModelRequest(
        model_snapshot=planner,
        messages=[Message(role="user", content="plan")],
        params={"max_tokens": 2048, "temperature": 0},
        agent_node_id="playtest.planner",
        prompt_version="planner-prompt@1",
    )
    executor_request = ModelRequest(
        model_snapshot=executor,
        messages=[Message(role="user", content="act")],
        params={"max_tokens": 2048, "temperature": 0},
        agent_node_id="playtest.executor",
        prompt_version="executor-prompt@1",
    )
    assert [call["model_snapshot"] for call in adapter.calls] == [planner, executor]
    assert [call["params"] for call in adapter.calls] == [
        planner_request.params,
        executor_request.params,
    ]
    assert planner_hash == request_hash(planner_request)
    assert executor_hash == request_hash(executor_request)
