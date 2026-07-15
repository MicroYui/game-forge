"""The per-call M4b execution bridge the worker hands to each executor.

One :class:`WorkerModelBridge` is built per fenced attempt (the runner's
``model_bridge_factory``) and drives every LLM invocation through the M4b
router/cassette/cost/trace seams in the exact §7.A order:

  publish ``source_rendered`` (BEFORE the call, fenced + idempotent) ->
  record the routing decision -> reserve cost -> execute the one persisted
  decision on the ``M4ModelRouter`` (recording the cassette shard in RECORD;
  failing closed on a REPLAY miss) -> reconcile the typed usage.

The bridge never touches the DB or ObjectStore directly: prompt publication,
routing persistence, and cost reservation/reconciliation all go through injected
transaction-bound ports. A stale worker whose prompt publication is fenced out
raises before any reserve or provider call, so it can neither start a new reserve
nor publish a business result; its already-persisted reservation still settles
via the attempt-close / reaper path. REPLAY misses propagate ``CassetteReplayMiss``
so the executor / terminal policy fails the attempt closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from gameforge.contracts.jobs import RunIntermediateArtifactLinkV1
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.model_router import ModelRequestV2, request_hash
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.contracts.storage import UtcClock
from gameforge.platform.runs.commands import (
    PromptRenderPublicationRequest,
    PromptRenderPublicationResult,
)
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.runtime.cassette.store import CassetteRouteKey
from gameforge.runtime.model_router.m4_router import M4ModelRouter, M4RouterResultV1
from gameforge.runtime.observability.trace import Tracer


ExecutionSource = Literal["online", "full_response_cache", "cassette_replay"]


class PromptRenderPublisher(Protocol):
    """Fenced ``source_rendered`` publication (``RunCommandService`` surface)."""

    def publish_prompt_rendered(
        self, request: PromptRenderPublicationRequest
    ) -> PromptRenderPublicationResult: ...


class RoutingDecider(Protocol):
    """Select + persist exactly one native routing decision for a rendered call."""

    def decide_and_record(
        self,
        model_request: ModelRequestV2,
        *,
        execution_source: ExecutionSource,
        decided_at: datetime,
    ) -> RoutingDecisionV1: ...


class CallCostGateway(Protocol):
    """Reserve-before-use + typed-usage reconciliation for one model call."""

    def reserve_call(self, *, decision: RoutingDecisionV1) -> None: ...

    def reconcile_usage(self, *, decision: RoutingDecisionV1, result: M4RouterResultV1) -> None: ...


@dataclass(frozen=True, slots=True)
class ModelCallRequest:
    """One rendered LLM call the executor asks the bridge to run."""

    model_request: ModelRequestV2
    source_artifact_id: str
    idempotency_scope: str
    idempotency_key: str
    route_ordinal: int = 1
    deadline_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class ModelCallResult:
    response: M4RouterResultV1
    decision: RoutingDecisionV1
    link: RunIntermediateArtifactLinkV1
    replayed: bool


class WorkerModelBridge:
    def __init__(
        self,
        *,
        fence: AttemptWriteFence,
        execution_source: ExecutionSource,
        prompt_publisher: PromptRenderPublisher,
        decider: RoutingDecider,
        router: M4ModelRouter,
        cost: CallCostGateway,
        tracer: Tracer,
        clock: UtcClock,
        worker_actor: AuditActor,
    ) -> None:
        self._fence = fence
        self._execution_source = execution_source
        self._prompt_publisher = prompt_publisher
        self._decider = decider
        self._router = router
        self._cost = cost
        self._tracer = tracer
        self._clock = clock
        self._worker_actor = worker_actor

    def call_model(self, request: ModelCallRequest) -> ModelCallResult:
        with self._tracer.span(
            "worker.model.call",
            attributes={
                "run_id": self._fence.run_id,
                "attempt_no": self._fence.attempt_no,
                "route_ordinal": request.route_ordinal,
            },
        ):
            # 1) Publish the canonical source_rendered artifact BEFORE the call.
            #    This CAS-claims the call ordinal and is idempotent across crashes;
            #    a fenced-out (stale) worker raises here and never reserves/calls.
            #    The intermediate-link request_hash is the bare 64-hex digest.
            rendered_hash = request_hash(request.model_request).removeprefix("sha256:")
            publication = self._prompt_publisher.publish_prompt_rendered(
                PromptRenderPublicationRequest(
                    fence=self._fence,
                    artifact_id=request.source_artifact_id,
                    request_hash=rendered_hash,
                    idempotency_scope=request.idempotency_scope,
                    idempotency_key=request.idempotency_key,
                    actor=self._worker_actor,
                )
            )
            link = publication.link
            # 2) Record the routing decision for this exact rendered request.
            decision = self._decider.decide_and_record(
                request.model_request,
                execution_source=self._execution_source,
                decided_at=self._now(),
            )
            # 3) Reserve cost BEFORE incurring any provider usage.
            self._cost.reserve_call(decision=decision)
            # 4) Execute the one persisted decision. RECORD records the shard;
            #    a REPLAY miss raises CassetteReplayMiss and fails closed.
            route_key = CassetteRouteKey(
                run_id=self._fence.run_id,
                attempt_no=self._fence.attempt_no,
                call_ordinal=link.call_ordinal,
                route_ordinal=request.route_ordinal,
                routing_decision_id=decision.decision_id,
            )
            result = self._router.call(
                request.model_request,
                decision=decision,
                deadline_utc=request.deadline_utc or self._now(),
                recorded_at=self._now(),
                cassette_route_key=route_key,
            )
            # 5) Reconcile the typed usage the call actually incurred.
            self._cost.reconcile_usage(decision=decision, result=result)
            return ModelCallResult(
                response=result,
                decision=decision,
                link=link,
                replayed=publication.replayed,
            )

    def _now(self) -> datetime:
        value = self._clock.now_utc()
        if value.tzinfo is None:
            raise ValueError("worker model bridge clock must return timezone-aware UTC")
        return value.astimezone(UTC)


__all__ = [
    "CallCostGateway",
    "ExecutionSource",
    "ModelCallRequest",
    "ModelCallResult",
    "PromptRenderPublisher",
    "RoutingDecider",
    "WorkerModelBridge",
]
