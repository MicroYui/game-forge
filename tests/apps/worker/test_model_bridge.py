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
from gameforge.contracts.errors import (
    Conflict,
    DependencyUnavailable,
    IntegrityViolation,
    PermanentDependencyFailure,
    QuotaExceeded,
)
from gameforge.contracts.jobs import (
    AgentPromptContextDraftV1,
    AgentPromptSourceMessageV1,
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    RunAttempt,
    RunModelResponseConsumptionV1,
    RunToolIntermediateLinkV1,
    execution_version_plan_digest,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.model_router import ModelSnapshot, request_hash
from gameforge.contracts.observability import SpanDataV1
from gameforge.contracts.reliability import FailureClassificationV1, RetryPolicyV1
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.platform.runs.commands import (
    AgentPromptContextPublicationResult,
    PromptRenderPublicationResult,
)
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.contracts.jobs import RunIntermediateArtifactLinkV1
from gameforge.runtime.cassette.store import CassetteRouteKey, CassetteStore
from gameforge.runtime.model_router.cache import ExactResponseCache
from gameforge.runtime.model_router.m4_router import M4ModelRouter, ProviderRouteFailure
from gameforge.runtime.model_router.router import CassetteReplayMiss, RouterMode
from gameforge.runtime.observability import AlwaysOnSampler, Tracer
from gameforge.runtime.reliability.retry import RetryExecutor
from gameforge.runtime.clock import ManualMonotonicClock
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
from tests.platform.m4c.test_terminal_publisher import _registry_and_definition, _run_record


WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")


def _bridged_request():
    return _request().model_copy(update={"params": {"max_output_tokens": 4096}})


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

    def publish_prompt_rendered(
        self,
        request,
        *,
        model_request,
        source_artifact_ids,
    ) -> PromptRenderPublicationResult:
        assert source_artifact_ids
        self._order.append("publish_prompt")
        self.requests.append((request, model_request))
        if self._raises is not None:
            raise self._raises
        link = RunIntermediateArtifactLinkV1(
            run_id=request.fence.run_id,
            attempt_no=request.fence.attempt_no,
            call_ordinal=request.call_ordinal or len(self.requests),
            route_ordinal=request.route_ordinal,
            artifact_id=request.artifact_id,
            role="prompt_rendered",
            request_hash=request.request_hash,
            fencing_token=request.fence.fencing_token,
            published_at="2026-07-14T12:00:00Z",
        )
        return PromptRenderPublicationResult(link=link, replayed=False)


class _RecordingContextPublisher:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.requests: list[object] = []

    def publish_agent_prompt_context(self, **values) -> AgentPromptContextPublicationResult:
        self._order.append("publish_context")
        self.requests.append(values)
        return AgentPromptContextPublicationResult(
            link=RunToolIntermediateLinkV1(
                run_id="run-1",
                attempt_no=1,
                target_call_ordinal=values["target_call_ordinal"],
                artifact_id="artifact:context:1",
                agent_node_id=values["model_request"].agent_node_id,
                prompt_version=values["model_request"].prompt_version,
                payload_hash="d" * 64,
                fencing_token=1,
                published_at="2026-07-14T12:00:00Z",
            ),
            replayed=False,
        )


class _RecordingDecider:
    def __init__(self, order: list[str], authority: _DecisionAuthority, decision) -> None:
        self._order = order
        self._authority = authority
        self._decision = decision
        self.model_requests: list[object] = []

    class _Prepared:
        def __init__(self, decision, request) -> None:
            self.model_snapshot_id = decision.model_snapshot
            self.max_output_tokens = request.params["max_output_tokens"]

    def prepare(self, model_request):
        self._order.append("prepare")
        return self._Prepared(self._decision, model_request)

    def next_fallback(self, previous):
        del previous
        raise DependencyUnavailable("fallback chain exhausted")

    def decide_and_record(
        self,
        model_request,
        *,
        prepared,
        link,
        execution_source,
        decided_at,
    ):
        del prepared, link, execution_source, decided_at
        self.model_requests.append(model_request)
        self._order.append("decide")
        self._authority.decisions[self._decision.decision_id] = self._decision
        return self._decision


class _RecordingCost:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.reserved: list[object] = []
        self.reconciled: list[object] = []
        self.failed: list[object] = []
        self.cancelled: list[object] = []

    def reserve_call(
        self,
        *,
        decision,
        model_request,
        deadline_utc,
        call_ordinal,
        route_ordinal,
        transport_attempt,
    ) -> object:
        del model_request, deadline_utc
        self._order.append("reserve")
        reservation = (decision, call_ordinal, route_ordinal, transport_attempt)
        self.reserved.append(reservation)
        return reservation

    def reconcile_usage(self, *, reservation, decision, result, wall_time_ns) -> None:
        self._order.append("reconcile")
        self.reconciled.append((reservation, result, wall_time_ns))

    def settle_failed_transport(self, *, reservation, decision, wall_time_ns) -> None:
        self._order.append("settle_failed")
        self.failed.append((reservation, decision, wall_time_ns))

    def cancel_reservation(self, *, reservation) -> None:
        self._order.append("cancel_reservation")
        self.cancelled.append(reservation)


class _RecordingStepCost:
    def __init__(
        self,
        order: list[str],
        *,
        reserve_error: BaseException | None = None,
        reconcile_error: BaseException | None = None,
    ) -> None:
        self._order = order
        self._reserve_error = reserve_error
        self._reconcile_error = reconcile_error
        self.reserved: list[object] = []
        self.reconciled: list[object] = []
        self.in_transaction: list[object] = []
        self.standalone: list[object] = []

    def reserve_step(self, **values) -> object:
        self._order.append("reserve_step")
        if self._reserve_error is not None:
            raise self._reserve_error
        token = dict(values)
        self.reserved.append(token)
        return token

    def reconcile_step(self, *, reservation: object) -> None:
        self._order.append("reconcile_step")
        self.reconciled.append(reservation)
        self.standalone.append(reservation)
        if self._reconcile_error is not None:
            raise self._reconcile_error

    def reconcile_step_in_transaction(self, *, transaction, reservation: object) -> None:
        del transaction
        self._order.append("reconcile_step")
        self.reconciled.append(reservation)
        self.in_transaction.append(reservation)
        if self._reconcile_error is not None:
            raise self._reconcile_error


class _RecordingResponsePublisher:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.records: list[object] = []

    def publish_response_consumption(self, **values) -> RunModelResponseConsumptionV1:
        self._order.append("publish_record")
        self.records.append(values)
        values["step_cost"].reconcile_step_in_transaction(
            transaction=self,
            reservation=values["step_reservation"],
        )
        return RunModelResponseConsumptionV1(
            run_id=values["link"].run_id,
            attempt_no=values["link"].attempt_no,
            call_ordinal=values["link"].call_ordinal,
            route_ordinal=values["link"].route_ordinal,
            execution_source=values["result"].execution_source,
            reservation_group_id="reservation:recording",
            transport_attempt=(
                values["result"].transport_attempt_count
                if values["result"].execution_source == "online"
                else None
            ),
            cassette_shard_artifact_id="artifact:record-shard:1",
            response_digest="e" * 64,
            consumed_at="2026-07-14T12:00:00Z",
        )


class _SnapshotResolver:
    def __init__(self, snapshots=None) -> None:
        self._snapshots = dict(snapshots or {})

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ):
        del catalog_version, catalog_digest
        return self._snapshots.get(model_snapshot_id, _bridged_request().model_snapshot)


class _RetryableClassifier:
    version = "classifier@retry"

    def classify(self, error: BaseException) -> FailureClassificationV1:
        assert isinstance(error, RuntimeError)
        return FailureClassificationV1(
            failure_kind="transient_infrastructure",
            retryable=True,
            counts_for_breaker=True,
            idempotency_required=True,
            reason_code="retryable_transport",
        )


class _NoopSleeper:
    def sleep(self, seconds: float) -> None:
        assert seconds == 0


class _FailOnceTransport:
    def __init__(self) -> None:
        self.calls = 0

    def complete_with_timeout(self, request, *, timeout_s):
        del request, timeout_s
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient provider failure")
        return _response()


class _AlwaysFailTransport:
    def complete_with_timeout(self, request, *, timeout_s):
        del request, timeout_s
        raise RuntimeError("provider URL/body/credential must remain redacted")


class _FailSelectedModelTransport:
    def __init__(self, failing: ModelSnapshot) -> None:
        self._failing = failing
        self.calls = []

    def complete_with_timeout(self, request, *, timeout_s):
        del timeout_s
        self.calls.append(request)
        if request.model_snapshot == self._failing:
            raise RuntimeError("selected provider route failed")
        return _response()


class _SequenceDecider:
    class _Prepared:
        def __init__(self, decision, request) -> None:
            self.model_snapshot_id = decision.model_snapshot
            self.max_output_tokens = request.params["max_output_tokens"]

    def __init__(self, order, authority, routes) -> None:
        self._order = order
        self._authority = authority
        self._routes = routes
        self._index = 0
        self.model_requests = []

    def prepare(self, model_request):
        self._order.append("prepare")
        return self._Prepared(self._routes[0], model_request)

    def next_fallback(self, previous):
        del previous
        self._order.append("fallback")
        self._index += 1
        if self._index >= len(self._routes):
            raise DependencyUnavailable("fallback chain exhausted")
        return self._Prepared(self._routes[self._index], _bridged_request())

    def decide_and_record(
        self,
        model_request,
        *,
        prepared,
        link,
        execution_source,
        decided_at,
    ):
        del prepared, link, execution_source, decided_at
        decision = self._routes[self._index]
        self._authority.decisions[decision.decision_id] = decision
        self.model_requests.append(model_request)
        self._order.append("decide")
        return decision


_AUTO_RESPONSE_PUBLISHER = object()


def _bridge(
    *,
    order,
    publisher,
    decider,
    cost,
    step_cost=None,
    router,
    execution_source,
    context_publisher=None,
    response_publisher=_AUTO_RESPONSE_PUBLISHER,
    extra_model_snapshots=(),
    exporter=None,
):
    request = _bridged_request()
    model_id = canonical_model_snapshot_id(request.model_snapshot)
    node = PlannedAgentNodeVersionV1(
        agent_node_id=request.agent_node_id,
        prompt_version=request.prompt_version,
        tool_version="repair-tool@1",
        allowed_model_snapshots=(
            model_id,
            *(canonical_model_snapshot_id(item) for item in extra_model_snapshots),
        ),
    )
    body = {
        "agent_graph_version": "repair-graph@1",
        "nodes": (node,),
        "model_catalog_version": 1,
        "model_catalog_digest": "2" * 64,
        "routing_policy_version": 1,
        "routing_policy_digest": "1" * 64,
    }
    plan = ExecutionVersionPlanV1(
        **body,
        plan_digest=execution_version_plan_digest(body),
    )
    _, definition = _registry_and_definition()
    base = _run_record(definition)
    mode = "replay" if execution_source == "cassette_replay" else "record"
    payload = base.payload.model_copy(
        update={
            "llm_execution_mode": mode,
            "execution_version_plan": plan,
            "cassette_artifact_id": "artifact:replay" if mode == "replay" else None,
        }
    )
    run = base.model_copy(
        update={
            "run_id": "run-1",
            "payload": payload,
            "budget_set_snapshot_id": payload.budget_set_snapshot_id,
        }
    )
    attempt = RunAttempt(
        run_id="run-1",
        attempt_no=1,
        status="running",
        fencing_token=1,
        worker_principal_id=WORKER.principal_id,
        next_call_ordinal=1,
        started_at=NOW.isoformat().replace("+00:00", "Z"),
        attempt_deadline_utc=(NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
    )
    return WorkerModelBridge(
        run=run,
        attempt=attempt,
        fence=_fence(),
        execution_source=execution_source,
        prompt_publisher=publisher,
        context_publisher=context_publisher or _RecordingContextPublisher(order),
        decider=decider,
        router=router,
        cost=cost,
        step_cost=step_cost or _RecordingStepCost(order),
        model_snapshot_resolver=_SnapshotResolver(
            {
                model_id: request.model_snapshot,
                **{canonical_model_snapshot_id(item): item for item in extra_model_snapshots},
            }
        ),
        tracer=Tracer(exporter=exporter or _ListExporter(), sampler=AlwaysOnSampler()),
        clock=_Clock(),
        worker_actor=WORKER,
        response_publisher=(
            _RecordingResponsePublisher(order)
            if response_publisher is _AUTO_RESPONSE_PUBLISHER
            else response_publisher
        ),
    )


def _call_request() -> ModelCallRequest:
    source_artifact_ids = ("artifact:source-rendered:1",)
    return ModelCallRequest(
        model_request=_bridged_request(),
        source_artifact_ids=source_artifact_ids,
        prompt_context=AgentPromptContextDraftV1(
            context_kind="repair_initial",
            messages=(
                AgentPromptSourceMessageV1(
                    role="user",
                    content=_bridged_request().messages[-1].content,
                    purpose="context",
                ),
            ),
            source_artifact_ids=source_artifact_ids,
        ),
        idempotency_scope="run:run-1:attempt:1",
        idempotency_key="model:1",
        route_ordinal=1,
        deadline_utc=NOW + timedelta(seconds=10),
    )


def test_record_call_orders_render_decision_reserve_call_reconcile(tmp_path) -> None:
    order: list[str] = []
    transport = _TypedTransport(_response())
    store = CassetteStore(tmp_path)
    decision = _decision(_bridged_request(), source="online")
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

    assert order == [
        "publish_context",
        "prepare",
        "publish_prompt",
        "reserve_step",
        "decide",
        "reserve",
        "publish_record",
        "reconcile_step",
    ]
    assert decider.model_requests == [_call_request().model_request]
    assert decider.model_requests[0].agent_node_id == "repair"
    # The prompt was published before the provider was ever called.
    assert publisher.requests[0][0].request_hash  # source_rendered published first
    assert publisher.requests[0][1] == _call_request().model_request
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
    # RECORD usage is reconciled atomically by the response publisher, not here.
    assert cost.reconciled == []
    assert result.response.execution_source == "online"
    assert result.link.call_ordinal == 1


def test_each_transport_retry_has_its_own_reserve_and_immediate_settlement(
    tmp_path,
) -> None:
    order: list[str] = []
    clock = _Clock()
    transport = _FailOnceTransport()
    decision = _decision(_bridged_request(), source="online")
    authority = _DecisionAuthority()
    cost = _RecordingCost(order)
    step_cost = _RecordingStepCost(order)
    exporter = _ListExporter()
    router = M4ModelRouter(
        transport=transport,
        store=CassetteStore(tmp_path),
        cache=ExactResponseCache(),
        mode=RouterMode.RECORD,
        retry_executor=RetryExecutor(
            policy=RetryPolicyV1(
                policy_version="retry@transport",
                failure_classifier_version="classifier@retry",
                max_attempts=2,
                initial_backoff_ms=0,
                max_backoff_ms=0,
                multiplier=1,
                jitter_ratio=0,
            ),
            classifier=_RetryableClassifier(),
            utc_clock=clock,
            monotonic_clock=ManualMonotonicClock(),
            sleeper=_NoopSleeper(),
            jitter=lambda: 0,
        ),
        decision_authority=authority,
    )
    bridge = _bridge(
        order=order,
        publisher=_RecordingPromptPublisher(order),
        decider=_RecordingDecider(order, authority, decision),
        cost=cost,
        step_cost=step_cost,
        router=router,
        execution_source="online",
        exporter=exporter,
    )

    result = bridge.call_model(_call_request())

    assert order == [
        "publish_context",
        "prepare",
        "publish_prompt",
        "reserve_step",
        "decide",
        "reserve",
        "settle_failed",
        "reserve",
        "publish_record",
        "reconcile_step",
    ]
    assert [item[3] for item in cost.reserved] == [1, 2]
    assert cost.failed[0][0] == cost.reserved[0]
    assert result.response.transport_attempt_count == 2
    assert len(step_cost.reserved) == len(step_cost.reconciled) == 1
    assert len(step_cost.in_transaction) == 1
    assert step_cost.standalone == []
    transport_spans = [span for span in exporter.spans if span.name == "worker.model.transport"]
    assert [span.attributes["transport_attempt"] for span in transport_spans] == [1, 2]
    assert [span.attributes["succeeded"] for span in transport_spans] == [False, True]
    assert all("request" not in span.attributes for span in transport_spans)


def test_record_refine_context_binds_committed_record_shard_as_cassette_source(
    tmp_path,
) -> None:
    order: list[str] = []
    decision = _decision(_bridged_request(), source="online")
    authority = _DecisionAuthority()
    context_publisher = _RecordingContextPublisher(order)
    bridge = _bridge(
        order=order,
        publisher=_RecordingPromptPublisher(order),
        context_publisher=context_publisher,
        decider=_RecordingDecider(order, authority, decision),
        cost=_RecordingCost(order),
        router=M4ModelRouter(
            transport=_TypedTransport(_response()),
            store=CassetteStore(tmp_path),
            cache=ExactResponseCache(),
            mode=RouterMode.RECORD,
            retry_executor=_retry(_Clock()),
            decision_authority=authority,
        ),
        execution_source="online",
    )
    first = _call_request()
    bridge.call_model(first)
    bridge.call_model(
        ModelCallRequest(
            model_request=first.model_request,
            source_artifact_ids=first.source_artifact_ids,
            prompt_context=first.prompt_context.model_copy(
                update={
                    "context_kind": "repair_refine",
                    "include_previous_consumption": True,
                }
            ),
            idempotency_scope=first.idempotency_scope,
            idempotency_key="model:2",
            route_ordinal=1,
            deadline_utc=first.deadline_utc,
        )
    )

    prior = context_publisher.requests[1]["prior_consumption"]
    assert prior.cassette_shard_artifact_id == "artifact:record-shard:1"
    assert prior.cassette_source_artifact_id == "artifact:record-shard:1"


def test_record_full_response_cache_preserves_origin_transport_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    order: list[str] = []
    request = _bridged_request()
    online_decision = _decision(request, source="online")
    cache_decision = _decision(request, source="full_response_cache")
    authority = _DecisionAuthority(online_decision, cache_decision)
    cache = ExactResponseCache()
    seed_router = M4ModelRouter(
        transport=_TypedTransport(_response()),
        store=CassetteStore(tmp_path / "seed"),
        cache=cache,
        mode=RouterMode.PASSTHROUGH,
        retry_executor=_retry(_Clock()),
        decision_authority=authority,
    )
    online = seed_router.call(
        request,
        decision=online_decision,
        deadline_utc=NOW + timedelta(seconds=10),
        recorded_at=NOW,
    )
    seed_router.commit_response_cache(
        request,
        decision=online_decision,
        result=online,
        recorded_at=NOW,
    )
    response_publisher = _RecordingResponsePublisher(order)
    # outer logical-call span start, local cache lookup start/end, span end
    monotonic_samples = iter((0, 100, 137, 200))
    monkeypatch.setattr(
        "gameforge.apps.worker.model_bridge.time.monotonic_ns",
        lambda: next(monotonic_samples),
    )
    bridge = _bridge(
        order=order,
        publisher=_RecordingPromptPublisher(order),
        decider=_RecordingDecider(order, authority, cache_decision),
        cost=_RecordingCost(order),
        router=M4ModelRouter(
            transport=_TypedTransport(_response()),
            store=CassetteStore(tmp_path / "record"),
            cache=cache,
            mode=RouterMode.RECORD,
            retry_executor=_retry(_Clock()),
            decision_authority=authority,
        ),
        execution_source="full_response_cache",
        response_publisher=response_publisher,
    )

    result = bridge.call_model(_call_request())

    assert result.response.execution_source == "full_response_cache"
    assert result.response.transport_attempt_count == 0
    assert result.response.recorded_transport_attempt_count == 1
    record = response_publisher.records[0]["record"]
    assert record.token_usage.input_tokens == 0
    assert record.token_usage.output_tokens == 0
    assert record.token_usage.cache_read_tokens == 0
    assert record.token_usage.cache_write_tokens == 0
    assert record.token_usage.total_tokens == 0
    assert record.transport_attempt_count == 1
    assert record.transport_retry_count == 0
    assert response_publisher.records[0]["wall_time_ns"] == 37


def test_model_fallback_keeps_one_call_and_uses_contiguous_route_ordinals(
    tmp_path,
) -> None:
    order: list[str] = []
    initial = _bridged_request()
    selected_first = ModelSnapshot(
        provider="openai",
        model="selected-after-static-skip",
        snapshot_tag="v1",
    )
    selected_second = ModelSnapshot(
        provider="openai",
        model="selected-after-runtime-failure",
        snapshot_tag="v1",
    )
    first_request = initial.model_copy(update={"model_snapshot": selected_first})
    second_request = initial.model_copy(update={"model_snapshot": selected_second})
    first = RoutingDecisionV1.create(
        run_id="run-1",
        attempt_no=1,
        request_hash=request_hash(first_request),
        rule_id="repair",
        model_snapshot=canonical_model_snapshot_id(selected_first),
        tier="best",
        reason_code="fallback_model_unavailable",
        budget_set_snapshot_id="budget-run-1",
        fallback_from=canonical_model_snapshot_id(initial.model_snapshot),
        fallback_index=1,
        policy_version=1,
        routing_policy_digest="1" * 64,
        catalog_version=1,
        catalog_digest="2" * 64,
        execution_source="online",
        decided_at=NOW,
    )
    second = RoutingDecisionV1.create(
        run_id="run-1",
        attempt_no=1,
        request_hash=request_hash(second_request),
        rule_id="repair",
        model_snapshot=canonical_model_snapshot_id(selected_second),
        tier="best",
        reason_code="fallback_after_failure",
        budget_set_snapshot_id="budget-run-1",
        # Policy index 2 was skipped; actual route ordinal is still exactly 2.
        fallback_from="openai:sha256:" + "f" * 64,
        fallback_index=3,
        policy_version=1,
        routing_policy_digest="1" * 64,
        catalog_version=1,
        catalog_digest="2" * 64,
        execution_source="online",
        decided_at=NOW,
    )
    authority = _DecisionAuthority()
    transport = _FailSelectedModelTransport(selected_first)
    publisher = _RecordingPromptPublisher(order)
    step_cost = _RecordingStepCost(order)
    bridge = _bridge(
        order=order,
        publisher=publisher,
        decider=_SequenceDecider(order, authority, (first, second)),
        cost=_RecordingCost(order),
        step_cost=step_cost,
        router=M4ModelRouter(
            transport=transport,
            store=CassetteStore(tmp_path),
            cache=ExactResponseCache(),
            mode=RouterMode.RECORD,
            retry_executor=RetryExecutor(
                policy=RetryPolicyV1(
                    policy_version="retry@fallback",
                    failure_classifier_version="classifier@retry",
                    max_attempts=1,
                    initial_backoff_ms=0,
                    max_backoff_ms=0,
                    multiplier=1,
                    jitter_ratio=0,
                ),
                classifier=_RetryableClassifier(),
                utc_clock=_Clock(),
                monotonic_clock=ManualMonotonicClock(),
                sleeper=_NoopSleeper(),
                jitter=lambda: 0,
            ),
            decision_authority=authority,
        ),
        execution_source="online",
        extra_model_snapshots=(selected_first, selected_second),
    )

    result = bridge.call_model(_call_request())

    prompt_requests = [item[0] for item in publisher.requests]
    assert [(item.call_ordinal, item.route_ordinal) for item in prompt_requests] == [
        (None, 1),
        (1, 2),
    ]
    assert result.link.call_ordinal == 1
    assert result.link.route_ordinal == 2
    assert result.decision.fallback_index == 3
    assert [item.model_snapshot for item in transport.calls] == [
        selected_first,
        selected_second,
    ]


def test_exhausted_transient_provider_routes_surface_typed_retryable_dependency(
    tmp_path,
) -> None:
    order: list[str] = []
    request = _bridged_request()
    decision = _decision(request, source="online")
    authority = _DecisionAuthority()
    retry = RetryExecutor(
        policy=RetryPolicyV1(
            policy_version="retry@provider",
            failure_classifier_version="classifier@retry",
            max_attempts=1,
            initial_backoff_ms=0,
            max_backoff_ms=0,
            multiplier=1,
            jitter_ratio=0,
        ),
        classifier=_RetryableClassifier(),
        utc_clock=_Clock(),
        monotonic_clock=ManualMonotonicClock(),
        sleeper=_NoopSleeper(),
        jitter=lambda: 0,
    )
    step_cost = _RecordingStepCost(order)
    bridge = _bridge(
        order=order,
        publisher=_RecordingPromptPublisher(order),
        decider=_RecordingDecider(order, authority, decision),
        cost=_RecordingCost(order),
        step_cost=step_cost,
        router=M4ModelRouter(
            transport=_AlwaysFailTransport(),
            store=CassetteStore(tmp_path),
            cache=ExactResponseCache(),
            mode=RouterMode.PASSTHROUGH,
            retry_executor=retry,
            decision_authority=authority,
        ),
        execution_source="online",
    )

    with pytest.raises(DependencyUnavailable) as captured:
        bridge.call_model(_call_request())

    assert captured.value.context == {
        "dependency_kind": "model_provider",
        "dependency_id": f"model-provider:{decision.model_snapshot}",
        "operation_code": "model.route_fallback",
        "classifier_code": "routing_fallback_exhausted",
    }
    assert "provider URL" not in captured.value.detail
    assert len(step_cost.reconciled) == 1
    assert len(step_cost.standalone) == 1
    assert step_cost.in_transaction == []


@pytest.mark.parametrize(
    ("classification", "expected_type"),
    [
        (
            FailureClassificationV1(
                failure_kind="quota",
                retryable=False,
                counts_for_breaker=False,
                idempotency_required=False,
                reason_code="provider_quota_rejected",
            ),
            QuotaExceeded,
        ),
        (
            FailureClassificationV1(
                failure_kind="authentication",
                retryable=False,
                counts_for_breaker=False,
                idempotency_required=False,
                reason_code="provider_authentication_rejected",
            ),
            PermanentDependencyFailure,
        ),
    ],
)
def test_provider_classification_projects_to_frozen_worker_fault(
    classification,
    expected_type,
) -> None:
    decision = _decision(_bridged_request(), source="online")
    failure = ProviderRouteFailure(RuntimeError("raw provider secret"), classification)

    projected = WorkerModelBridge._project_provider_failure(failure, decision=decision)

    assert isinstance(projected, expected_type)
    assert "raw provider secret" not in getattr(projected, "detail", "")


def test_replay_miss_fails_closed_without_reconciling(tmp_path) -> None:
    order: list[str] = []
    store = CassetteStore(tmp_path)  # empty -> replay miss
    decision = _decision(_bridged_request(), source="cassette_replay")
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

    # Reserve happened before replay; a miss releases its zero-use admission.
    assert order == [
        "publish_context",
        "prepare",
        "publish_prompt",
        "reserve_step",
        "decide",
        "reserve",
        "cancel_reservation",
        "reconcile_step",
    ]
    assert cost.reconciled == []


def test_stale_worker_never_reserves_or_calls_provider(tmp_path) -> None:
    order: list[str] = []
    transport = _TypedTransport(_response())
    store = CassetteStore(tmp_path)
    decision = _decision(_bridged_request(), source="online")
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
    assert order == ["publish_context", "prepare", "publish_prompt"]
    assert cost.reserved == []
    assert transport.calls == []


def test_agent_step_rejection_after_prompt_prevents_route_reserve_and_provider(
    tmp_path,
) -> None:
    order: list[str] = []
    transport = _TypedTransport(_response())
    decision = _decision(_bridged_request(), source="online")
    authority = _DecisionAuthority()
    cost = _RecordingCost(order)
    step_cost = _RecordingStepCost(
        order,
        reserve_error=Conflict("agent-step fence differs"),
    )
    bridge = _bridge(
        order=order,
        publisher=_RecordingPromptPublisher(order),
        decider=_RecordingDecider(order, authority, decision),
        cost=cost,
        step_cost=step_cost,
        router=M4ModelRouter(
            transport=transport,
            store=CassetteStore(tmp_path),
            cache=ExactResponseCache(),
            mode=RouterMode.PASSTHROUGH,
            retry_executor=_retry(_Clock()),
            decision_authority=authority,
        ),
        execution_source="online",
    )

    with pytest.raises(Conflict, match="agent-step fence"):
        bridge.call_model(_call_request())

    assert order == ["publish_context", "prepare", "publish_prompt", "reserve_step"]
    assert cost.reserved == []
    assert transport.calls == []


def test_record_without_atomic_shard_publisher_fails_before_prompt_or_provider(
    tmp_path,
) -> None:
    order: list[str] = []
    transport = _TypedTransport(_response())
    decision = _decision(_bridged_request(), source="online")
    authority = _DecisionAuthority(decision)
    bridge = _bridge(
        order=order,
        publisher=_RecordingPromptPublisher(order),
        decider=_RecordingDecider(order, authority, decision),
        cost=_RecordingCost(order),
        router=M4ModelRouter(
            transport=transport,
            store=CassetteStore(tmp_path),
            cache=ExactResponseCache(),
            mode=RouterMode.RECORD,
            retry_executor=_retry(_Clock()),
            decision_authority=authority,
        ),
        execution_source="online",
        response_publisher=None,
    )

    with pytest.raises(IntegrityViolation, match="atomic response-consumption"):
        bridge.call_model(_call_request())

    assert order == []
    assert transport.calls == []


def test_response_publication_and_reconcile_double_failure_preserves_publication_error(
    tmp_path,
) -> None:
    order: list[str] = []
    decision = _decision(_bridged_request(), source="online")
    authority = _DecisionAuthority()

    class BrokenPublisher:
        def publish_response_consumption(self, **values) -> None:
            del values
            order.append("publish_record")
            raise Conflict("response publication lost its fence")

    class BrokenCost(_RecordingCost):
        def reconcile_usage(self, **values) -> None:
            del values
            order.append("reconcile")
            raise RuntimeError("accounting backend unavailable")

    bridge = _bridge(
        order=order,
        publisher=_RecordingPromptPublisher(order),
        decider=_RecordingDecider(order, authority, decision),
        cost=BrokenCost(order),
        router=M4ModelRouter(
            transport=_TypedTransport(_response()),
            store=CassetteStore(tmp_path),
            cache=ExactResponseCache(),
            mode=RouterMode.PASSTHROUGH,
            retry_executor=_retry(_Clock()),
            decision_authority=authority,
        ),
        execution_source="online",
        response_publisher=BrokenPublisher(),
    )

    with pytest.raises(Conflict, match="response publication lost its fence") as captured:
        bridge.call_model(_call_request())

    assert order[-3:] == ["publish_record", "reconcile", "reconcile_step"]
    assert any("reconciliation also failed" in note for note in captured.value.__notes__)


def test_response_and_step_settlement_double_failure_preserves_publication_error(
    tmp_path,
) -> None:
    order: list[str] = []
    decision = _decision(_bridged_request(), source="online")
    authority = _DecisionAuthority()

    class BrokenPublisher:
        def publish_response_consumption(self, **values) -> None:
            del values
            order.append("publish_record")
            raise Conflict("response publication lost its fence")

    step_cost = _RecordingStepCost(
        order,
        reconcile_error=RuntimeError("step ledger unavailable"),
    )
    bridge = _bridge(
        order=order,
        publisher=_RecordingPromptPublisher(order),
        decider=_RecordingDecider(order, authority, decision),
        cost=_RecordingCost(order),
        step_cost=step_cost,
        router=M4ModelRouter(
            transport=_TypedTransport(_response()),
            store=CassetteStore(tmp_path),
            cache=ExactResponseCache(),
            mode=RouterMode.PASSTHROUGH,
            retry_executor=_retry(_Clock()),
            decision_authority=authority,
        ),
        execution_source="online",
        response_publisher=BrokenPublisher(),
    )

    with pytest.raises(Conflict, match="response publication lost its fence") as captured:
        bridge.call_model(_call_request())

    assert len(step_cost.reserved) == len(step_cost.reconciled) == 1
    assert len(step_cost.standalone) == 1
    assert any("agent-step reconciliation also failed" in note for note in captured.value.__notes__)


@pytest.mark.parametrize("requested_deadline", [None, NOW + timedelta(hours=1)])
def test_model_deadline_defaults_to_and_is_capped_by_attempt_deadline(
    tmp_path, requested_deadline
) -> None:
    order: list[str] = []
    transport = _TypedTransport(_response())
    decision = _decision(_bridged_request(), source="online")
    authority = _DecisionAuthority()
    bridge = _bridge(
        order=order,
        publisher=_RecordingPromptPublisher(order),
        decider=_RecordingDecider(order, authority, decision),
        cost=_RecordingCost(order),
        router=M4ModelRouter(
            transport=transport,
            store=CassetteStore(tmp_path),
            cache=ExactResponseCache(),
            mode=RouterMode.RECORD,
            retry_executor=_retry(_Clock()),
            decision_authority=authority,
        ),
        execution_source="online",
    )
    call = _call_request()
    call = ModelCallRequest(
        model_request=call.model_request,
        source_artifact_ids=call.source_artifact_ids,
        prompt_context=call.prompt_context,
        idempotency_scope=call.idempotency_scope,
        idempotency_key=call.idempotency_key,
        route_ordinal=call.route_ordinal,
        deadline_utc=requested_deadline,
    )

    bridge.call_model(call)

    assert transport.timeouts == [pytest.approx(30 * 60)]


@pytest.mark.parametrize(
    "request_update",
    [
        {"agent_node_id": "escaped-node"},
        {"prompt_version": "escaped-prompt@1"},
        {
            "model_snapshot": _bridged_request().model_snapshot.model_copy(
                update={"snapshot_tag": "escaped"}
            )
        },
    ],
)
def test_execution_plan_escape_fails_before_prompt_reserve_or_transport(
    tmp_path, request_update
) -> None:
    order: list[str] = []
    transport = _TypedTransport(_response())
    decision = _decision(_bridged_request(), source="online")
    authority = _DecisionAuthority(decision)
    router = M4ModelRouter(
        transport=transport,
        store=CassetteStore(tmp_path),
        cache=ExactResponseCache(),
        mode=RouterMode.RECORD,
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
        execution_source="online",
    )
    escaped = _call_request()
    escaped = ModelCallRequest(
        model_request=escaped.model_request.model_copy(update=request_update),
        source_artifact_ids=escaped.source_artifact_ids,
        prompt_context=escaped.prompt_context,
        idempotency_scope=escaped.idempotency_scope,
        idempotency_key=escaped.idempotency_key,
        route_ordinal=escaped.route_ordinal,
        deadline_utc=escaped.deadline_utc,
    )

    with pytest.raises(IntegrityViolation, match="execution plan"):
        bridge.call_model(escaped)

    assert order == []
    assert cost.reserved == []
    assert transport.calls == []
