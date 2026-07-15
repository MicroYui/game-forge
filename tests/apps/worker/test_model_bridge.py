"""``test_model_bridge`` — the per-call M4b execution bridge (M4c Task 10).

Every LLM invocation the worker drives must, in order:
  1. publish the canonical ``source_rendered`` prompt BEFORE the provider call,
  2. record the routing decision,
  3. reserve cost BEFORE the call,
  4. execute exactly that one persisted decision through the real ``M4ModelRouter``
     (recording the cassette shard in RECORD; failing closed on a REPLAY miss),
  5. reconcile the typed usage AFTER the call.
A stale worker (whose prompt publication is fenced out) never reserves or calls
the provider. NO live network: RECORD uses a fake transport, REPLAY a real store.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from gameforge.apps.worker.model_bridge import ModelCallRequest, WorkerModelBridge
from gameforge.contracts.cassette import CassetteRecordV2
from gameforge.contracts.errors import Conflict
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.observability import SpanDataV1
from gameforge.platform.runs.commands import PromptRenderPublicationResult
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.contracts.jobs import RunIntermediateArtifactLinkV1
from gameforge.runtime.cassette.store import CassetteRouteKey, CassetteStore
from gameforge.runtime.model_router.cache import ExactResponseCache
from gameforge.runtime.model_router.m4_router import M4ModelRouter
from gameforge.runtime.model_router.router import CassetteReplayMiss, RouterMode
from gameforge.runtime.observability import AlwaysOnSampler, Tracer
from tests.runtime.model_router.test_routing_v2 import (
    NOW,
    _Clock,
    _DecisionAuthority,
    _TypedTransport,
    _decision,
    _request,
    _response,
    _retry,
)


WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")


def _fence() -> AttemptWriteFence:
    return AttemptWriteFence(
        run_id="run-1",
        attempt_no=1,
        expected_run_revision=4,
        lease_id="lease:1",
        fencing_token=1,
    )


class _ListExporter:
    def __init__(self) -> None:
        self.spans: list[SpanDataV1] = []

    def export(self, spans) -> None:
        self.spans.extend(spans)


class _RecordingPromptPublisher:
    def __init__(self, order: list[str], *, raises: BaseException | None = None) -> None:
        self._order = order
        self._raises = raises
        self.requests: list[object] = []

    def publish_prompt_rendered(self, request) -> PromptRenderPublicationResult:
        self._order.append("publish_prompt")
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        link = RunIntermediateArtifactLinkV1(
            run_id=request.fence.run_id,
            attempt_no=request.fence.attempt_no,
            call_ordinal=1,
            artifact_id=request.artifact_id,
            role="prompt_rendered",
            request_hash=request.request_hash,
            fencing_token=request.fence.fencing_token,
            published_at="2026-07-14T12:00:00Z",
        )
        return PromptRenderPublicationResult(link=link, replayed=False)


class _RecordingDecider:
    def __init__(self, order: list[str], authority: _DecisionAuthority, decision) -> None:
        self._order = order
        self._authority = authority
        self._decision = decision

    def decide_and_record(self, model_request, *, execution_source, decided_at):
        self._order.append("decide")
        self._authority.decisions[self._decision.decision_id] = self._decision
        return self._decision


class _RecordingCost:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.reserved: list[object] = []
        self.reconciled: list[object] = []

    def reserve_call(self, *, decision) -> None:
        self._order.append("reserve")
        self.reserved.append(decision)

    def reconcile_usage(self, *, decision, result) -> None:
        self._order.append("reconcile")
        self.reconciled.append(result)


def _bridge(*, order, publisher, decider, cost, router, execution_source):
    return WorkerModelBridge(
        fence=_fence(),
        execution_source=execution_source,
        prompt_publisher=publisher,
        decider=decider,
        router=router,
        cost=cost,
        tracer=Tracer(exporter=_ListExporter(), sampler=AlwaysOnSampler()),
        clock=_Clock(),
        worker_actor=WORKER,
    )


def _call_request() -> ModelCallRequest:
    return ModelCallRequest(
        model_request=_request(),
        source_artifact_id="artifact:source-rendered:1",
        idempotency_scope="run-1:attempt:1",
        idempotency_key="call:1",
        route_ordinal=1,
        deadline_utc=NOW + timedelta(seconds=10),
    )


def test_record_call_orders_render_decision_reserve_call_reconcile(tmp_path) -> None:
    order: list[str] = []
    transport = _TypedTransport(_response())
    store = CassetteStore(tmp_path)
    decision = _decision(_request(), source="online")
    authority = _DecisionAuthority()
    router = M4ModelRouter(
        transport=transport,
        store=store,
        cache=ExactResponseCache(),
        mode=RouterMode.RECORD,
        retry_executor=_retry(_Clock()),
        decision_authority=authority,
    )
    publisher = _RecordingPromptPublisher(order)
    decider = _RecordingDecider(order, authority, decision)
    cost = _RecordingCost(order)
    bridge = _bridge(
        order=order,
        publisher=publisher,
        decider=decider,
        cost=cost,
        router=router,
        execution_source="online",
    )

    result = bridge.call_model(_call_request())

    assert order == ["publish_prompt", "decide", "reserve", "reconcile"]
    # The prompt was published before the provider was ever called.
    assert publisher.requests[0].request_hash  # source_rendered published first
    assert transport.calls  # the provider ran exactly once
    # RECORD wrote a cassette shard through the router.
    shard = store.replay_native(
        CassetteRouteKey(
            run_id="run-1",
            attempt_no=1,
            call_ordinal=1,
            route_ordinal=1,
            routing_decision_id=decision.decision_id,
        )
    )
    assert isinstance(shard, CassetteRecordV2)
    # Typed usage was reconciled from the real router result.
    assert cost.reconciled == [result.response]
    assert result.response.execution_source == "online"
    assert result.link.call_ordinal == 1


def test_replay_miss_fails_closed_without_reconciling(tmp_path) -> None:
    order: list[str] = []
    store = CassetteStore(tmp_path)  # empty -> replay miss
    decision = _decision(_request(), source="cassette_replay")
    authority = _DecisionAuthority()
    router = M4ModelRouter(
        transport=_TypedTransport(_response()),
        store=store,
        cache=ExactResponseCache(),
        mode=RouterMode.REPLAY,
        retry_executor=_retry(_Clock()),
        decision_authority=authority,
    )
    cost = _RecordingCost(order)
    bridge = _bridge(
        order=order,
        publisher=_RecordingPromptPublisher(order),
        decider=_RecordingDecider(order, authority, decision),
        cost=cost,
        router=router,
        execution_source="cassette_replay",
    )

    with pytest.raises(CassetteReplayMiss):
        bridge.call_model(_call_request())

    # Reserve happened before the (failed) call; usage was NOT reconciled.
    assert order == ["publish_prompt", "decide", "reserve"]
    assert cost.reconciled == []


def test_stale_worker_never_reserves_or_calls_provider(tmp_path) -> None:
    order: list[str] = []
    transport = _TypedTransport(_response())
    store = CassetteStore(tmp_path)
    decision = _decision(_request(), source="online")
    authority = _DecisionAuthority()
    router = M4ModelRouter(
        transport=transport,
        store=store,
        cache=ExactResponseCache(),
        mode=RouterMode.RECORD,
        retry_executor=_retry(_Clock()),
        decision_authority=authority,
    )
    cost = _RecordingCost(order)
    publisher = _RecordingPromptPublisher(order, raises=Conflict("attempt write fence differs"))
    bridge = _bridge(
        order=order,
        publisher=publisher,
        decider=_RecordingDecider(order, authority, decision),
        cost=cost,
        router=router,
        execution_source="online",
    )

    with pytest.raises(Conflict):
        bridge.call_model(_call_request())

    # A fenced-out worker publishes nothing downstream: no decision, reserve, or call.
    assert order == ["publish_prompt"]
    assert cost.reserved == []
    assert transport.calls == []
