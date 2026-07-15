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
SDK, so the bridge request is a *structurally* identical local dataclass (the
concrete ``gameforge.apps.worker.model_bridge.ModelCallRequest`` reads the exact
same attribute names) and the bridge result is consumed by attribute access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from gameforge.contracts.jobs import ExecutionVersionPlanV1
from gameforge.contracts.model_router import (
    Message,
    ModelRequestV2,
    ModelResponse,
    ModelSnapshot,
    ToolSchemaRef,
    request_hash,
)

from gameforge.platform.run_handlers.base import ModelBridgePort


@dataclass(frozen=True, slots=True)
class ModelBridgeCallRequestV1:
    """A rendered model call for the bridge.

    Field-for-field structurally identical to
    ``gameforge.apps.worker.model_bridge.ModelCallRequest`` so the injected
    ``WorkerModelBridge`` consumes it verbatim without ``platform`` importing
    ``gameforge.apps``.
    """

    model_request: ModelRequestV2
    source_artifact_id: str
    idempotency_scope: str
    idempotency_key: str
    route_ordinal: int = 1
    deadline_utc: datetime | None = None


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
            return ModelSnapshot(
                provider=_provider_of(node.allowed_model_snapshots[0]),
                model=_model_of(node.allowed_model_snapshots[0]),
                snapshot_tag=_tag_of(node.allowed_model_snapshots[0]),
            )
    raise ValueError(f"agent node {agent_node_id!r} is not part of the execution plan")


def _split_snapshot(reference: str) -> tuple[str, str, str]:
    parts = reference.split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            "planned model snapshot must be provider/model/snapshot_tag",
        )
    return parts[0], parts[1], parts[2]


def _provider_of(reference: str) -> str:
    return _split_snapshot(reference)[0]


def _model_of(reference: str) -> str:
    return _split_snapshot(reference)[1]


def _tag_of(reference: str) -> str:
    return _split_snapshot(reference)[2]


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
        idempotency_prefix: str,
        deadline_utc: datetime | None = None,
    ) -> None:
        self._bridge = model_bridge
        self._idempotency_scope = idempotency_scope
        self._idempotency_prefix = idempotency_prefix
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
        source_artifact_id: str,
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
            source_artifact_id=source_artifact_id,
            idempotency_scope=self._idempotency_scope,
            idempotency_key=f"{self._idempotency_prefix}:model:{self._call_index}",
            route_ordinal=route_ordinal,
            deadline_utc=self._deadline,
        )
        result = self._bridge.call_model(bridge_request)
        response = router_result_to_model_response(getattr(result, "response"))
        link = getattr(result, "link")
        return AdapterModelResult(
            response=response,
            request_hash=request_hash(model_request),
            routing_decision_id=getattr(getattr(result, "decision"), "decision_id"),
            call_ordinal=getattr(link, "call_ordinal"),
            route_ordinal=route_ordinal,
            replayed=bool(getattr(result, "replayed")),
            execution_source=getattr(getattr(result, "response"), "execution_source", ""),
        )


__all__ = [
    "AdapterModelResult",
    "ModelBridgeAgentAdapter",
    "ModelBridgeCallRequestV1",
    "plan_node_snapshot",
    "router_result_to_model_response",
]
