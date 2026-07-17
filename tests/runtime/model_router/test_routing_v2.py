from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gameforge.contracts.cassette import CassetteRecordV1, CassetteRecordV2
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import (
    Message,
    ModelRequestV2,
    ModelResponse,
    ModelSnapshot,
    PrefixCacheDirectiveV1,
    compute_prefix_hash,
    request_hash,
)
from gameforge.contracts.reliability import FailureClassificationV1, RetryPolicyV1
from gameforge.contracts.reliability import CircuitBreakerConfigV1
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.runtime.cassette.store import CassetteRouteKey, CassetteStore
from gameforge.runtime.clock import ManualMonotonicClock
from gameforge.runtime.model_router.cache import ExactResponseCache
from gameforge.runtime.model_router.m4_router import M4ModelRouter
from gameforge.runtime.model_router.router import CassetteReplayMiss, RouterMode
from gameforge.runtime.model_router.typed_transport import (
    LegacyTypedTransportAdapter,
    TransportResponseV2,
)
from gameforge.runtime.reliability.retry import RetryExecutor
from gameforge.runtime.reliability.breaker import CircuitBreaker


NOW = datetime(2026, 7, 14, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.current = NOW

    def now_utc(self) -> datetime:
        return self.current


class _NoSleep:
    def sleep(self, seconds: float) -> None:
        raise AssertionError(f"unexpected sleep {seconds}")


class _Classifier:
    version = "classifier@1"

    def classify(self, error: BaseException) -> FailureClassificationV1:
        return FailureClassificationV1(
            failure_kind="validation",
            retryable=False,
            counts_for_breaker=False,
            idempotency_required=False,
            reason_code="transport_failure",
        )


class _TypedTransport:
    def __init__(self, response: TransportResponseV2) -> None:
        self.response = response
        self.calls: list[ModelRequestV2] = []
        self.timeouts: list[float] = []

    def complete(self, request: ModelRequestV2) -> TransportResponseV2:
        self.calls.append(request)
        return self.response

    def complete_with_timeout(
        self,
        request: ModelRequestV2,
        *,
        timeout_s: float,
    ) -> TransportResponseV2:
        self.timeouts.append(timeout_s)
        return self.complete(request)


class _DecisionAuthority:
    def __init__(self, *decisions: RoutingDecisionV1) -> None:
        self.decisions = {item.decision_id: item for item in decisions}

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None:
        return self.decisions.get(decision_id)


def _route_key(
    decision: RoutingDecisionV1,
    *,
    call_ordinal: int = 1,
    route_ordinal: int = 1,
) -> CassetteRouteKey:
    return CassetteRouteKey(
        run_id=decision.run_id,
        attempt_no=decision.attempt_no,
        call_ordinal=call_ordinal,
        route_ordinal=route_ordinal,
        routing_decision_id=decision.decision_id,
    )


def _snapshot() -> ModelSnapshot:
    return ModelSnapshot(
        provider="openai",
        model="gpt-5.6-sol",
        snapshot_tag="2026-07-14",
    )


def _request(content: str = "repair") -> ModelRequestV2:
    return ModelRequestV2(
        model_snapshot=_snapshot(),
        messages=[Message(role="user", content=content)],
        agent_node_id="repair",
        prompt_version="repair@2",
    )


def _decision(
    request: ModelRequestV2,
    *,
    source: str,
    run_id: str = "run-1",
    catalog_digest: str = "2" * 64,
) -> RoutingDecisionV1:
    return RoutingDecisionV1.create(
        run_id=run_id,
        attempt_no=1,
        request_hash=request_hash(request),
        rule_id="repair",
        model_snapshot=canonical_model_snapshot_id(request.model_snapshot),
        tier="best",
        reason_code="primary_rule" if source == "online" else "recorded_replay",
        budget_set_snapshot_id=f"budget-{run_id}",
        fallback_from=None,
        fallback_index=0,
        policy_version=1,
        routing_policy_digest="1" * 64,
        catalog_version=1,
        catalog_digest=catalog_digest,
        execution_source=source,
        decided_at=NOW,
    )


def _response() -> TransportResponseV2:
    return TransportResponseV2(
        response_normalized="fixed",
        raw_response={"id": "response-1"},
        finish_reason="stop",
        tool_calls=(),
        latency=LatencyObservationV1(status="reported", provider_latency_ms=120),
        token_usage=TokenUsageObservationV1(
            status="reported", input_tokens=10, output_tokens=2, total_tokens=12
        ),
        provider_prefix_cache=CacheHitObservationV1(status="reported", hit=False),
    )


def _retry(clock: _Clock) -> RetryExecutor:
    return RetryExecutor(
        policy=RetryPolicyV1(
            policy_version="retry@1",
            failure_classifier_version="classifier@1",
            max_attempts=1,
            initial_backoff_ms=0,
            max_backoff_ms=0,
            multiplier=1,
            jitter_ratio=0,
        ),
        classifier=_Classifier(),
        utc_clock=clock,
        monotonic_clock=ManualMonotonicClock(),
        sleeper=_NoSleep(),
        jitter=lambda: 0,
    )


def _breaker(snapshot_id: str, clock: _Clock) -> CircuitBreaker:
    return CircuitBreaker(
        dependency_id=f"model-provider:{snapshot_id}",
        config=CircuitBreakerConfigV1(
            config_version="breaker@1",
            rolling_window_s=60,
            minimum_samples=2,
            failure_threshold=1,
            open_cooldown_s=10,
            half_open_max_concurrent_probes=1,
            half_open_success_threshold=1,
        ),
        clock=clock,
    )


def test_online_route_breaker_resolver_fails_closed_without_exact_authority(tmp_path) -> None:
    clock = _Clock()
    request = _request()
    decision = _decision(request, source="online")
    router = M4ModelRouter(
        transport=_TypedTransport(_response()),
        store=CassetteStore(tmp_path),
        cache=ExactResponseCache(),
        mode=RouterMode.PASSTHROUGH,
        retry_executor=_retry(clock),
        decision_authority=_DecisionAuthority(decision),
        circuit_breaker_resolver=lambda _: None,  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(IntegrityViolation, match="no exact dependency authority"):
        router.call(
            request,
            decision=decision,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )


def test_online_route_breaker_resolver_rejects_another_model_dependency(tmp_path) -> None:
    clock = _Clock()
    request = _request()
    decision = _decision(request, source="online")
    wrong = CircuitBreaker(
        dependency_id="model-provider:another-snapshot",
        config=CircuitBreakerConfigV1(
            config_version="breaker@1",
            rolling_window_s=60,
            minimum_samples=2,
            failure_threshold=1,
            open_cooldown_s=10,
            half_open_max_concurrent_probes=1,
            half_open_success_threshold=1,
        ),
        clock=clock,
    )
    router = M4ModelRouter(
        transport=_TypedTransport(_response()),
        store=CassetteStore(tmp_path),
        cache=ExactResponseCache(),
        mode=RouterMode.PASSTHROUGH,
        retry_executor=_retry(clock),
        decision_authority=_DecisionAuthority(decision),
        circuit_breaker_resolver=lambda _: wrong,
    )

    with pytest.raises(IntegrityViolation, match="exact model dependency"):
        router.call(
            request,
            decision=decision,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )


def test_online_primary_and_fallback_resolve_distinct_exact_breakers(tmp_path) -> None:
    clock = _Clock()
    primary_request = _request("primary")
    fallback_request = primary_request.model_copy(
        update={
            "model_snapshot": ModelSnapshot(
                provider="anthropic",
                model="claude-opus-4-8",
                snapshot_tag="2026-07-14",
            ),
            "messages": [Message(role="user", content="fallback")],
        }
    )
    primary = _decision(primary_request, source="online")
    fallback = _decision(fallback_request, source="online")
    breakers = {
        primary.model_snapshot: _breaker(primary.model_snapshot, clock),
        fallback.model_snapshot: _breaker(fallback.model_snapshot, clock),
    }
    resolved: list[str] = []

    def resolve(decision: RoutingDecisionV1) -> CircuitBreaker:
        resolved.append(decision.model_snapshot)
        return breakers[decision.model_snapshot]

    router = M4ModelRouter(
        transport=_TypedTransport(_response()),
        store=CassetteStore(tmp_path),
        cache=ExactResponseCache(),
        mode=RouterMode.PASSTHROUGH,
        retry_executor=_retry(clock),
        decision_authority=_DecisionAuthority(primary, fallback),
        circuit_breaker_resolver=resolve,
    )

    for request, decision in ((primary_request, primary), (fallback_request, fallback)):
        router.call(
            request,
            decision=decision,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )

    assert resolved == [primary.model_snapshot, fallback.model_snapshot]
    assert all(len(breaker.snapshot().samples) == 1 for breaker in breakers.values())


def test_record_writes_only_cassette_v2_and_preserves_typed_observations(tmp_path) -> None:
    clock = _Clock()
    transport = _TypedTransport(_response())
    store = CassetteStore(tmp_path)
    request = _request()
    decision = _decision(request, source="online")
    route_key = _route_key(decision)
    router = M4ModelRouter(
        transport=transport,
        store=store,
        cache=ExactResponseCache(),
        mode=RouterMode.RECORD,
        retry_executor=_retry(clock),
        decision_authority=_DecisionAuthority(decision),
    )

    result = router.call(
        request,
        decision=decision,
        cassette_route_key=route_key,
        deadline_utc=NOW + timedelta(seconds=10),
        recorded_at=NOW,
    )

    record = store.replay_native(route_key)
    assert isinstance(record, CassetteRecordV2)
    assert record.token_usage == result.token_usage
    assert record.latency.provider_latency_ms == 120
    assert record.transport_attempt_count == 1
    assert transport.calls == [request]
    assert transport.timeouts == [10.0]


def test_full_response_cache_is_an_explicit_route_and_never_uses_prefix_only(tmp_path) -> None:
    clock = _Clock()
    transport = _TypedTransport(_response())
    cache = ExactResponseCache()
    store = CassetteStore(tmp_path)
    first = _request("suffix A")
    online_decision = _decision(first, source="online")
    cache_decision = _decision(first, source="full_response_cache", run_id="run-2")
    second = _request("suffix B")
    miss_decision = _decision(second, source="full_response_cache", run_id="run-3")
    router = M4ModelRouter(
        transport=transport,
        store=store,
        cache=cache,
        mode=RouterMode.PASSTHROUGH,
        retry_executor=_retry(clock),
        decision_authority=_DecisionAuthority(
            online_decision,
            cache_decision,
            miss_decision,
        ),
    )
    online = router.call(
        first,
        decision=online_decision,
        deadline_utc=NOW + timedelta(seconds=10),
        recorded_at=NOW,
    )
    router.commit_response_cache(
        first,
        decision=online_decision,
        result=online,
        recorded_at=NOW,
    )
    cached = router.call(
        first,
        decision=cache_decision,
        deadline_utc=NOW + timedelta(seconds=10),
        recorded_at=NOW,
    )
    assert cached.execution_source == "full_response_cache"
    assert cached.token_usage.total_tokens == 0
    assert cached.recorded_transport_attempt_count == 1
    assert cached.recorded_transport_retry_count == 0
    assert len(transport.calls) == 1

    with pytest.raises(IntegrityViolation, match="miss"):
        router.call(
            second,
            decision=miss_decision,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )


def test_replay_reproduces_recorded_choice_and_rejects_policy_drift(tmp_path) -> None:
    clock = _Clock()
    store = CassetteStore(tmp_path)
    request = _request()
    recorded_decision = _decision(request, source="online")
    route_key = _route_key(recorded_decision)
    recorder = M4ModelRouter(
        transport=_TypedTransport(_response()),
        store=store,
        cache=ExactResponseCache(),
        mode=RouterMode.RECORD,
        retry_executor=_retry(clock),
        decision_authority=_DecisionAuthority(recorded_decision),
    )
    recorder.call(
        request,
        decision=recorded_decision,
        cassette_route_key=route_key,
        deadline_utc=NOW + timedelta(seconds=10),
        recorded_at=NOW,
    )
    replay_transport = _TypedTransport(_response())
    replay_decision = _decision(request, source="cassette_replay", run_id="run-replay")
    drift_decision = _decision(
        request,
        source="cassette_replay",
        run_id="run-drift",
        catalog_digest="f" * 64,
    )
    replay = M4ModelRouter(
        transport=replay_transport,
        store=store,
        cache=ExactResponseCache(),
        mode=RouterMode.REPLAY,
        retry_executor=_retry(clock),
        decision_authority=_DecisionAuthority(replay_decision, drift_decision),
    )
    result = replay.call(
        request,
        decision=replay_decision,
        cassette_route_key=route_key,
        deadline_utc=NOW + timedelta(seconds=10),
        recorded_at=NOW,
    )
    assert result.execution_source == "cassette_replay"
    assert result.latency.provider_latency_ms == 120
    assert result.transport_attempt_count == 0
    assert result.transport_retry_count == 0
    assert result.recorded_transport_attempt_count == 1
    assert result.recorded_transport_retry_count == 0
    assert replay_transport.calls == []

    with pytest.raises(IntegrityViolation, match="routing evidence"):
        replay.call(
            request,
            decision=drift_decision,
            cassette_route_key=route_key,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )


def test_native_replay_requires_exact_route_key_and_never_uses_flat_v2(tmp_path) -> None:
    clock = _Clock()
    store = CassetteStore(tmp_path)
    request = _request()
    recorded_decision = _decision(request, source="online", run_id="source-run")
    response = _response()
    store.record(
        CassetteRecordV2(
            request_hash=request_hash(request),
            agent_node_id=request.agent_node_id,
            model_snapshot=request.model_snapshot,
            routing_decision=recorded_decision,
            response_normalized=response.response_normalized,
            raw_response=response.raw_response,
            latency=response.latency,
            token_usage=response.token_usage,
            provider_prefix_cache=response.provider_prefix_cache,
            finish_reason=response.finish_reason,
            tool_calls=response.tool_calls,
            transport_attempt_count=1,
            transport_retry_count=0,
            recorded_at=NOW,
        )
    )
    replay_decision = _decision(request, source="cassette_replay", run_id="replay-run")
    router = M4ModelRouter(
        transport=_TypedTransport(response),
        store=store,
        cache=ExactResponseCache(),
        mode=RouterMode.REPLAY,
        retry_executor=_retry(clock),
        decision_authority=_DecisionAuthority(replay_decision),
    )

    with pytest.raises(IntegrityViolation, match="exact cassette route key"):
        router.call(
            request,
            decision=replay_decision,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )


def test_unverified_legacy_record_is_not_executable_in_m4_replay(tmp_path) -> None:
    request = _request()
    store = CassetteStore(tmp_path)
    store.record(
        CassetteRecordV1(
            request_hash=request_hash(request),
            agent_node_id=request.agent_node_id,
            model_snapshot=request.model_snapshot,
            response=ModelResponse(response_normalized="legacy"),
        )
    )
    decision = _decision(request, source="cassette_replay")
    router = M4ModelRouter(
        transport=_TypedTransport(_response()),
        store=store,
        cache=ExactResponseCache(),
        mode=RouterMode.REPLAY,
        retry_executor=_retry(_Clock()),
        decision_authority=_DecisionAuthority(decision),
    )
    with pytest.raises(IntegrityViolation, match="exact cassette route key"):
        router.call(
            request,
            decision=decision,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )


def test_replay_miss_remains_fail_closed(tmp_path) -> None:
    request = _request()
    decision = _decision(request, source="cassette_replay")
    missing_route = CassetteRouteKey(
        run_id="source-run",
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        routing_decision_id="routing-decision:sha256:" + "f" * 64,
    )
    router = M4ModelRouter(
        transport=_TypedTransport(_response()),
        store=CassetteStore(tmp_path),
        cache=ExactResponseCache(),
        mode=RouterMode.REPLAY,
        retry_executor=_retry(_Clock()),
        decision_authority=_DecisionAuthority(decision),
    )
    with pytest.raises(CassetteReplayMiss):
        router.call(
            request,
            decision=decision,
            cassette_route_key=missing_route,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )


def test_router_rejects_uncommitted_decision_before_any_external_effect(tmp_path) -> None:
    request = _request()
    decision = _decision(request, source="online")
    transport = _TypedTransport(_response())
    router = M4ModelRouter(
        transport=transport,
        store=CassetteStore(tmp_path),
        cache=ExactResponseCache(),
        mode=RouterMode.PASSTHROUGH,
        retry_executor=_retry(_Clock()),
        decision_authority=_DecisionAuthority(),
    )

    with pytest.raises(IntegrityViolation, match="not committed"):
        router.call(
            request,
            decision=decision,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )

    assert transport.calls == []


def test_router_rejects_decision_content_drift_before_any_external_effect(tmp_path) -> None:
    request = _request()
    decision = _decision(request, source="online")
    corrupted_authority_value = decision.model_copy(update={"tier": "tampered"})
    transport = _TypedTransport(_response())
    router = M4ModelRouter(
        transport=transport,
        store=CassetteStore(tmp_path),
        cache=ExactResponseCache(),
        mode=RouterMode.PASSTHROUGH,
        retry_executor=_retry(_Clock()),
        decision_authority=_DecisionAuthority(corrupted_authority_value),
    )

    with pytest.raises(IntegrityViolation, match="differs from committed"):
        router.call(
            request,
            decision=decision,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )

    assert transport.calls == []


def test_router_requires_explicit_prefix_cache_admission_before_transport(tmp_path) -> None:
    messages = [
        Message(role="system", content="stable prefix"),
        Message(role="user", content="suffix"),
    ]
    request = ModelRequestV2(
        model_snapshot=_snapshot(),
        messages=messages,
        agent_node_id="repair",
        prompt_version="repair@2",
        prefix_cache_directive=PrefixCacheDirectiveV1(
            prefix_message_count=1,
            prefix_hash=compute_prefix_hash(messages[:1]),
            provider_scope="openai",
            policy_version="prefix-policy@1",
        ),
    )
    decision = _decision(request, source="online")
    transport = _TypedTransport(_response())
    router = M4ModelRouter(
        transport=transport,
        store=CassetteStore(tmp_path),
        cache=ExactResponseCache(),
        mode=RouterMode.PASSTHROUGH,
        retry_executor=_retry(_Clock()),
        decision_authority=_DecisionAuthority(decision),
    )

    with pytest.raises(IntegrityViolation, match="explicit catalog/policy admission"):
        router.call(
            request,
            decision=decision,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )

    assert transport.calls == []


def test_legacy_transport_adapter_keeps_zero_and_empty_observations_unavailable() -> None:
    class _Legacy:
        def complete(self, request):
            return ModelResponse(
                response_normalized="legacy",
                latency_ms=0,
                token_usage={},
            )

    result = LegacyTypedTransportAdapter(_Legacy()).complete(_request())
    assert result.latency.status == "unavailable"
    assert result.token_usage.status == "unavailable"
    assert result.provider_prefix_cache.status == "unavailable"


def test_legacy_transport_adapter_closes_its_owned_transport() -> None:
    class _Legacy:
        def __init__(self) -> None:
            self.closed = False

        def complete(self, request):
            del request
            return ModelResponse(response_normalized="legacy")

        def close(self) -> None:
            self.closed = True

    legacy = _Legacy()
    adapter = LegacyTypedTransportAdapter(legacy)

    adapter.close()

    assert legacy.closed is True


@pytest.mark.parametrize(
    ("raw_response", "expected_hit"),
    [
        ({"usage": {"input_tokens_details": {"cached_tokens": 0}}}, False),
        ({"usage": {"cache_read_input_tokens": 7}}, True),
        (
            {
                "copilot_usage": {
                    "token_details": [
                        {"token_type": "cache_read", "token_count": 3},
                    ]
                }
            },
            True,
        ),
    ],
)
def test_legacy_transport_adapter_reports_provider_prefix_cache_observation(
    raw_response,
    expected_hit,
) -> None:
    class _Legacy:
        def complete(self, request):
            return ModelResponse(
                response_normalized="legacy",
                raw_response=raw_response,
            )

    result = LegacyTypedTransportAdapter(_Legacy()).complete(_request())
    assert result.provider_prefix_cache == CacheHitObservationV1(
        status="reported",
        hit=expected_hit,
    )
