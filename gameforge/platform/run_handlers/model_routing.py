"""Model-routing adapter: run an agent-style LLM call through the M4b bridge.

M2 agents reach the LLM through the *legacy* ``gameforge.agents.base.call_model``
over ``gameforge.runtime.model_router.router.ModelRouter``. The M4c executor seam
instead hands each handler an ``ExecutorContext.model_bridge`` — a
``WorkerModelBridgePort.call_model(ModelCallRequest) -> ModelCallResult`` over the
*native* ``M4ModelRouter`` that fences prompt publication, records the routing
decision, reserves cost, executes exactly one persisted decision (recording a
cassette shard in RECORD, failing closed on a REPLAY miss), and reconciles usage.

This adapter is the single bridge between those two shapes. It lets the composite
review handler (11a) and, later, the agent handlers (11b) issue an agent-style
call — ``agent_node_id`` + rendered ``user_prompt`` + ``prompt_version`` (+
optional system/params/tools) — and get back the M2 ``ModelResponse`` the agent
code expects, while every side effect flows through the injected bridge.

Dependency direction: ``platform`` must not import ``gameforge.apps`` or any LLM
SDK, so both sides consume the shared contracts-layer request and the bridge result
is consumed by attribute access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import ExecutionVersionPlanV1
from gameforge.contracts.model_router import (
    Message,
    ModelBridgeCallRequestV1,
    ModelRequest,
    ModelRequestV2,
    ModelResponse,
    ModelSnapshot,
    ToolSchemaRef,
)
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    canonical_model_snapshot_id,
)

from gameforge.platform.run_handlers.base import ExecutorContextLike, ModelBridgePort


@dataclass(frozen=True, slots=True)
class AdapterModelResult:
    """The mapped agent-facing result of one bridged model call."""

    response: ModelResponse
    request_hash: str
    routing_decision_id: str
    call_ordinal: int
    route_ordinal: int
    replayed: bool
    execution_source: str


class ExactModelCatalogAuthority(Protocol):
    """Retained model-catalog history addressed by its exact version and digest."""

    def get_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None: ...


class StructuredModelSnapshotAuthority(Protocol):
    """Trusted preimages for the opaque model snapshot IDs used by M4 plans."""

    def get_model_snapshot(self, model_snapshot_id: str) -> ModelSnapshot | None: ...


class PlannedModelSnapshotResolver(Protocol):
    """Resolve one planned opaque ID without parsing meaning out of the ID string."""

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ) -> ModelSnapshot: ...


class ExactModelCatalogSnapshotResolver:
    """Close an opaque plan model ID over retained catalog + structured preimage.

    ``ExecutionVersionPlanV1`` intentionally carries only the canonical opaque
    model identity.  A provider request still needs the legacy structured
    :class:`ModelSnapshot`, so the worker composition supplies a trusted preimage
    authority.  This resolver first loads the *exact* retained catalog descriptor,
    then proves that the supplied structure hashes back to the descriptor's opaque
    ID.  No component reverse-parses provider/model/tag from that opaque string.
    """

    def __init__(
        self,
        *,
        catalogs: ExactModelCatalogAuthority,
        snapshots: StructuredModelSnapshotAuthority,
    ) -> None:
        self._catalogs = catalogs
        self._snapshots = snapshots

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ) -> ModelSnapshot:
        catalog = self._catalogs.get_model_catalog(catalog_version, catalog_digest)
        if catalog is None:
            raise IntegrityViolation("exact model catalog history is unavailable")
        if catalog.catalog_version != catalog_version or catalog.catalog_digest != catalog_digest:
            raise IntegrityViolation("model catalog authority returned a non-exact binding")

        descriptor = next(
            (item for item in catalog.models if item.model_snapshot == model_snapshot_id),
            None,
        )
        if descriptor is None:
            raise IntegrityViolation(
                "planned model snapshot is absent from the exact catalog",
                model_snapshot=model_snapshot_id,
            )
        if descriptor.status != "active":
            raise IntegrityViolation(
                "planned model snapshot is disabled in the exact catalog",
                model_snapshot=model_snapshot_id,
            )

        retained = self._snapshots.get_model_snapshot(model_snapshot_id)
        if retained is None:
            raise IntegrityViolation(
                "structured model snapshot binding is unavailable",
                model_snapshot=model_snapshot_id,
            )
        if not isinstance(retained, ModelSnapshot):
            raise IntegrityViolation("structured model snapshot authority returned an invalid type")
        snapshot = ModelSnapshot.model_validate(retained.model_dump(mode="python"))
        try:
            canonical_id = canonical_model_snapshot_id(snapshot)
        except ValueError as exc:
            raise IntegrityViolation("structured model snapshot binding is invalid") from exc
        if canonical_id != descriptor.model_snapshot or snapshot.provider != descriptor.provider:
            raise IntegrityViolation(
                "structured model snapshot binding differs from the exact catalog descriptor",
                model_snapshot=model_snapshot_id,
            )
        return snapshot


def _token_usage_dict(observation: object) -> dict[str, int]:
    if getattr(observation, "status", None) != "reported":
        return {}
    usage: dict[str, int] = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    ):
        value = getattr(observation, name, None)
        if value is not None:
            usage[name] = int(value)
    return usage


def _latency_ms(observation: object) -> int:
    if getattr(observation, "status", None) != "reported":
        return 0
    value = getattr(observation, "provider_latency_ms", None)
    return int(value) if value is not None else 0


def router_result_to_model_response(result: object) -> ModelResponse:
    """Map a native ``M4RouterResultV1`` back onto the M2 ``ModelResponse`` shape."""

    return ModelResponse(
        response_normalized=getattr(result, "response_normalized"),
        raw_response=dict(getattr(result, "raw_response", {}) or {}),
        latency_ms=_latency_ms(getattr(result, "latency", None)),
        token_usage=_token_usage_dict(getattr(result, "token_usage", None)),
        finish_reason=getattr(result, "finish_reason", "") or "",
        tool_calls=[dict(call) for call in getattr(result, "tool_calls", ()) or ()],
    )


def plan_node_snapshot(
    plan: ExecutionVersionPlanV1 | None,
    agent_node_id: str,
    resolver: PlannedModelSnapshotResolver,
) -> ModelSnapshot:
    """Resolve the frozen model snapshot for ``agent_node_id`` from the plan.

    Non-``not_applicable`` Runs always carry an execution plan; the single
    ``allowed_model_snapshots`` entry (or the first, canonically ordered) fixes the
    served model for a replay/record/live call. Fail-closed when the node or its
    plan is absent — the adapter never invents a model.
    """

    if plan is None:
        raise ValueError("an LLM call requires a frozen execution version plan")
    for node in plan.nodes:
        if node.agent_node_id == agent_node_id:
            return resolver.resolve_model_snapshot(
                catalog_version=plan.model_catalog_version,
                catalog_digest=plan.model_catalog_digest,
                model_snapshot_id=node.allowed_model_snapshots[0],
            )
    raise ValueError(f"agent node {agent_node_id!r} is not part of the execution plan")


class ModelBridgeAgentAdapter:
    """Drive ordered, run-scoped agent LLM calls through the executor bridge.

    One adapter is created per handler invocation; the internal call counter keeps
    every logical invocation on a single ordered sequence, which is exactly what
    produces ONE ordered run-scoped cassette in RECORD/REPLAY.
    """

    def __init__(
        self,
        *,
        model_bridge: ModelBridgePort,
        idempotency_scope: str,
        deadline_utc: datetime | None = None,
    ) -> None:
        self._bridge = model_bridge
        self._idempotency_scope = idempotency_scope
        self._deadline = deadline_utc
        self._call_index = 0

    @property
    def call_count(self) -> int:
        return self._call_index

    def call_model(
        self,
        *,
        agent_node_id: str,
        user_prompt: str,
        prompt_version: str,
        model_snapshot: ModelSnapshot,
        source_artifact_ids: tuple[str, ...],
        system: str | None = None,
        params: dict[str, object] | None = None,
        tool_schemas: tuple[ToolSchemaRef, ...] = (),
        route_ordinal: int = 1,
    ) -> AdapterModelResult:
        """Issue one ordered model call and map the bridge result back to M2 shape."""

        messages: list[Message] = []
        if system is not None:
            messages.append(Message(role="system", content=system))
        messages.append(Message(role="user", content=user_prompt))
        model_request = ModelRequestV2(
            model_snapshot=model_snapshot,
            messages=messages,
            params=dict(params or {}),
            tool_schemas=list(tool_schemas),
            agent_node_id=agent_node_id,
            prompt_version=prompt_version,
        )
        self._call_index += 1
        bridge_request = ModelBridgeCallRequestV1(
            model_request=model_request,
            source_artifact_ids=source_artifact_ids,
            idempotency_scope=self._idempotency_scope,
            idempotency_key=f"model:{self._call_index}",
            route_ordinal=route_ordinal,
            deadline_utc=self._deadline,
        )
        result = self._bridge.call_model(bridge_request)
        response = router_result_to_model_response(getattr(result, "response"))
        link = getattr(result, "link")
        return AdapterModelResult(
            response=response,
            request_hash=getattr(getattr(result, "decision"), "request_hash"),
            routing_decision_id=getattr(getattr(result, "decision"), "decision_id"),
            call_ordinal=getattr(link, "call_ordinal"),
            route_ordinal=getattr(link, "route_ordinal"),
            replayed=bool(getattr(result, "replayed")),
            execution_source=getattr(getattr(result, "response"), "execution_source", ""),
        )


class BridgeModelRouter:
    """A ``ModelRouter``-shaped shim that drives one M2 agent through the bridge.

    The M2 agents reach the LLM via ``gameforge.agents.base.call_model`` which
    reads ``router.default_model_snapshot`` and calls ``router.call(request)`` with
    a legacy :class:`ModelRequest`. This shim exposes exactly that surface but
    routes every call through the injected :class:`ModelBridgeAgentAdapter`, so an
    unmodified agent (``ContentGenerator.run`` / ``RepairDrafter.draft`` /
    ``ExtractionProposer.run``) issues its LLM calls on the SAME ordered,
    run-scoped cassette the executor bridge fences — without ``platform`` importing
    ``gameforge.agents`` or any LLM SDK.

    One shim wraps exactly one agent node (``generation`` / ``repair`` /
    ``extraction``); ``default_model_snapshot`` is the plan-frozen model for that
    node so ``resolve_model_snapshot`` pins it onto every request the agent builds.
    """

    def __init__(
        self,
        *,
        adapter: ModelBridgeAgentAdapter,
        default_model_snapshot: ModelSnapshot,
        source_artifact_ids: tuple[str, ...],
    ) -> None:
        self._adapter = adapter
        self.default_model_snapshot = default_model_snapshot
        self._source_artifact_ids = source_artifact_ids

    def call(self, request: ModelRequest) -> ModelResponse:
        system: str | None = None
        user = ""
        for message in request.messages:
            if message.role == "system":
                system = message.content
            elif message.role == "user":
                user = message.content
        result = self._adapter.call_model(
            agent_node_id=request.agent_node_id,
            user_prompt=user,
            prompt_version=request.prompt_version,
            model_snapshot=request.model_snapshot,
            source_artifact_ids=self._source_artifact_ids,
            system=system,
            params=dict(request.params),
            tool_schemas=tuple(
                ToolSchemaRef(name=ref.name, version=ref.version) for ref in request.tool_schemas
            ),
        )
        return result.response

    @property
    def call_count(self) -> int:
        return self._adapter.call_count


def build_bridge_router(
    *,
    context: ExecutorContextLike,
    agent_node_id: str,
) -> BridgeModelRouter:
    """Build the per-node :class:`BridgeModelRouter` for one handler invocation.

    Constructs the ordered run-scoped :class:`ModelBridgeAgentAdapter` over the
    context bridge and pins the plan-frozen model snapshot for ``agent_node_id``.
    """

    adapter = ModelBridgeAgentAdapter(
        model_bridge=context.model_bridge,
        idempotency_scope=(f"run:{context.run.run_id}:attempt:{context.attempt.attempt_no}"),
        deadline_utc=context.deadline_utc,
    )
    model_snapshot = plan_node_snapshot(
        context.payload.execution_version_plan,
        agent_node_id,
        context.model_bridge,
    )
    return BridgeModelRouter(
        adapter=adapter,
        default_model_snapshot=model_snapshot,
        source_artifact_ids=_prompt_sources(context),
    )


class MultiNodeBridgeRouter:
    """A ``ModelRouter``-shaped shim that drives a MULTI-NODE M2 agent.

    Unlike :class:`BridgeModelRouter` (one agent node → one frozen snapshot), the
    M2b ``PlaytestAgent`` reaches the LLM through FOUR distinct node ids
    (``playtest.planner`` / ``playtest.executor`` / ``playtest.reflect`` /
    ``playtest.memory``). Each node's frozen model snapshot is resolved PER node id
    from the run's ``ExecutionVersionPlanV1`` (:func:`plan_node_snapshot`); every
    node's calls land, in agent-issue order, on the SAME ordered run-scoped cassette
    the injected :class:`ModelBridgeAgentAdapter` fences — so one unmodified agent
    produces one ordered cassette with the correct snapshot per node, without
    ``platform`` importing ``gameforge.agents`` or any LLM SDK.

    ``default_model_snapshot`` is the pre-call snapshot the agent's
    ``resolve_model_snapshot`` reads (before the node id is known to that helper);
    :meth:`call` then OVERRIDES it with the exact per-node plan-frozen snapshot, so
    the routed model is always the node's own snapshot regardless of the pre-call
    default.
    """

    def __init__(
        self,
        *,
        adapter: ModelBridgeAgentAdapter,
        node_snapshots: dict[str, ModelSnapshot],
        default_node_id: str,
        source_artifact_ids: tuple[str, ...],
    ) -> None:
        if default_node_id not in node_snapshots:
            raise ValueError(f"default node {default_node_id!r} is not routed by the plan")
        self._adapter = adapter
        self._node_snapshots = dict(node_snapshots)
        self.default_model_snapshot = node_snapshots[default_node_id]
        self._source_artifact_ids = source_artifact_ids

    def call(self, request: ModelRequest) -> ModelResponse:
        node_id = request.agent_node_id
        snapshot = self._node_snapshots.get(node_id)
        if snapshot is None:
            raise ValueError(f"agent node {node_id!r} is not routed by the playtest execution plan")
        system: str | None = None
        user = ""
        for message in request.messages:
            if message.role == "system":
                system = message.content
            elif message.role == "user":
                user = message.content
        result = self._adapter.call_model(
            agent_node_id=node_id,
            user_prompt=user,
            prompt_version=request.prompt_version,
            model_snapshot=snapshot,
            source_artifact_ids=self._source_artifact_ids,
            system=system,
            params=dict(request.params),
            tool_schemas=tuple(
                ToolSchemaRef(name=ref.name, version=ref.version) for ref in request.tool_schemas
            ),
        )
        return result.response

    @property
    def call_count(self) -> int:
        return self._adapter.call_count


def build_multinode_bridge_router(
    *,
    context: ExecutorContextLike,
    agent_node_ids: tuple[str, ...],
    default_node_id: str,
) -> MultiNodeBridgeRouter:
    """Build the ordered multi-node router for one multi-node agent invocation.

    Resolves each node id's plan-frozen snapshot up front (fail-closed if any node
    is missing from the plan) and shares ONE ordered adapter across all nodes.
    """

    if not agent_node_ids:
        raise ValueError("a multi-node router requires at least one agent node id")
    adapter = ModelBridgeAgentAdapter(
        model_bridge=context.model_bridge,
        idempotency_scope=(f"run:{context.run.run_id}:attempt:{context.attempt.attempt_no}"),
        deadline_utc=context.deadline_utc,
    )
    node_snapshots = {
        node_id: plan_node_snapshot(
            context.payload.execution_version_plan,
            node_id,
            context.model_bridge,
        )
        for node_id in agent_node_ids
    }
    return MultiNodeBridgeRouter(
        adapter=adapter,
        node_snapshots=node_snapshots,
        default_node_id=default_node_id,
        source_artifact_ids=_prompt_sources(context),
    )


def _prompt_sources(context: ExecutorContextLike) -> tuple[str, ...]:
    """Return real frozen handler inputs, never the REPLAY cassette container.

    Task-specific retained prompt bindings decide which complete source set is
    renderable.  This adapter must not collapse that authority to the first input,
    and the cassette authenticates a prior request rather than serving as source
    content for the current render.
    """

    payload = context.payload
    source_ids = tuple(
        artifact_id
        for artifact_id in payload.input_artifact_ids
        if artifact_id != payload.cassette_artifact_id
    )
    if not source_ids:
        raise IntegrityViolation("model Run has no exact non-cassette prompt source Artifact")
    if source_ids != tuple(sorted(set(source_ids))):
        raise IntegrityViolation("model Run prompt sources are not stable-unique")
    return source_ids


__all__ = [
    "AdapterModelResult",
    "BridgeModelRouter",
    "ExactModelCatalogAuthority",
    "ExactModelCatalogSnapshotResolver",
    "ModelBridgeAgentAdapter",
    "ModelBridgeCallRequestV1",
    "MultiNodeBridgeRouter",
    "PlannedModelSnapshotResolver",
    "StructuredModelSnapshotAuthority",
    "build_bridge_router",
    "build_multinode_bridge_router",
    "plan_node_snapshot",
    "router_result_to_model_response",
]
