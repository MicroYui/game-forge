from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gameforge.contracts.model_router import (
    Message,
    ModelRequest,
    ModelResponse,
    ModelSnapshot,
    request_hash,
)
from gameforge.contracts.reliability import (
    CircuitBreakerConfigV1,
    FailureClassificationV1,
    RetryPolicyV1,
)
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.clock import ManualMonotonicClock
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.runtime.reliability.breaker import CircuitBreaker
from gameforge.runtime.reliability.retry import RetryAttemptResult, RetryExecutor


class _Transient(Exception):
    pass


class _Invalid(Exception):
    pass


class _Unproven(Exception):
    pass


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 14, tzinfo=UTC)

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class _Sleeper:
    def __init__(self, utc: _Clock, monotonic: ManualMonotonicClock) -> None:
        self._utc = utc
        self._monotonic = monotonic
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._utc.advance(seconds)
        self._monotonic.advance_ns(round(seconds * 1_000_000_000))


class _Classifier:
    version = "classifier@1"

    def classify(self, error: BaseException) -> FailureClassificationV1:
        if isinstance(error, _Transient):
            return FailureClassificationV1(
                failure_kind="transient_infrastructure",
                retryable=True,
                counts_for_breaker=True,
                idempotency_required=True,
                reason_code="gateway_transient",
            )
        if isinstance(error, _Unproven):
            return FailureClassificationV1(
                failure_kind="solver_unproven",
                retryable=False,
                counts_for_breaker=False,
                idempotency_required=False,
                reason_code="unproven",
            )
        return FailureClassificationV1(
            failure_kind="validation",
            retryable=False,
            counts_for_breaker=False,
            idempotency_required=False,
            reason_code="invalid",
        )


class _Transport:
    def __init__(self, outcomes: list[ModelResponse | BaseException]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def complete(self, _: ModelRequest) -> ModelResponse:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _SlowSuccessTransport:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.calls = 0

    def complete(self, _: ModelRequest) -> ModelResponse:
        self.calls += 1
        self._clock.advance(2)
        return ModelResponse(response_normalized="too late")


def _request() -> ModelRequest:
    return ModelRequest(
        model_snapshot=ModelSnapshot(
            provider="openai", model="gpt-5.6-sol", snapshot_tag="2026-07-14"
        ),
        messages=[Message(role="user", content="repair")],
        agent_node_id="repair",
        prompt_version="repair@2",
    )


def _controls():
    utc = _Clock()
    monotonic = ManualMonotonicClock()
    sleeper = _Sleeper(utc, monotonic)
    classifier = _Classifier()
    retry = RetryExecutor(
        policy=RetryPolicyV1(
            policy_version="retry@1",
            failure_classifier_version=classifier.version,
            max_attempts=3,
            initial_backoff_ms=100,
            max_backoff_ms=100,
            multiplier=1,
            jitter_ratio=0,
        ),
        classifier=classifier,
        utc_clock=utc,
        monotonic_clock=monotonic,
        sleeper=sleeper,
        jitter=lambda: 0,
    )
    breaker = CircuitBreaker(
        dependency_id="model-gateway",
        config=CircuitBreakerConfigV1(
            config_version="breaker@1",
            rolling_window_s=60,
            minimum_samples=2,
            failure_threshold=1,
            open_cooldown_s=10,
            half_open_max_concurrent_probes=1,
            half_open_success_threshold=1,
        ),
        clock=utc,
    )
    return utc, retry, breaker, sleeper


def test_router_typed_retry_accounts_and_observes_every_transport_attempt(tmp_path) -> None:
    utc, retry, breaker, sleeper = _controls()
    transport = _Transport([_Transient("one"), ModelResponse(response_normalized="ok")])
    admitted: list[int] = []
    observed: list[RetryAttemptResult] = []
    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        mode=RouterMode.RECORD,
        retry_executor=retry,
        circuit_breaker=breaker,
        attempt_admission=admitted.append,
        attempt_observer=observed.append,
    )

    response = router.call(_request(), deadline_utc=utc.current + timedelta(seconds=5))

    assert response.response_normalized == "ok"
    assert transport.calls == 2
    assert admitted == [1, 2]
    assert [item.succeeded for item in observed] == [False, True]
    assert sleeper.calls == [0.1]
    assert len(breaker.snapshot().samples) == 2
    record = CassetteStore(tmp_path).replay(request_hash(_request()))
    assert record.transport_attempts == 2


@pytest.mark.parametrize("error", [_Invalid("bad"), _Unproven("unknown")])
def test_router_never_retries_or_breaks_on_non_infrastructure_outcome(
    tmp_path, error: BaseException
) -> None:
    utc, retry, breaker, sleeper = _controls()
    transport = _Transport([error])
    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
        retry_executor=retry,
        circuit_breaker=breaker,
    )

    with pytest.raises(type(error), match=str(error)):
        router.call(_request(), deadline_utc=utc.current + timedelta(seconds=5))

    assert transport.calls == 1
    assert sleeper.calls == []
    assert breaker.snapshot().state == "closed"
    assert breaker.snapshot().samples == ()


def test_typed_router_requires_total_deadline_before_any_attempt(tmp_path) -> None:
    _, retry, breaker, _ = _controls()
    transport = _Transport([ModelResponse(response_normalized="never")])
    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        retry_executor=retry,
        circuit_breaker=breaker,
    )

    with pytest.raises(ValueError, match="deadline"):
        router.call(_request())
    assert transport.calls == 0


def test_deadline_crossing_after_admission_cancels_budget_and_half_open_permit(
    tmp_path,
) -> None:
    utc, retry, breaker, _ = _controls()
    for _ in range(2):
        permit = breaker.before_call()
        breaker.record_failure(
            permit,
            FailureClassificationV1(
                failure_kind="transient_infrastructure",
                retryable=True,
                counts_for_breaker=True,
                idempotency_required=True,
                reason_code="gateway_transient",
            ),
        )
    assert breaker.snapshot().state == "open"
    utc.advance(10)
    cancelled: list[int] = []
    transport = _Transport([ModelResponse(response_normalized="never")])

    def admission(_: int) -> None:
        utc.advance(2)

    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
        retry_executor=retry,
        circuit_breaker=breaker,
        attempt_admission=admission,
        attempt_cancellation=cancelled.append,
    )
    with pytest.raises(TimeoutError, match="deadline"):
        router.call(_request(), deadline_utc=utc.current + timedelta(seconds=1))

    assert transport.calls == 0
    assert cancelled == [1]
    assert breaker.snapshot().state == "half_open"
    assert breaker.snapshot().half_open_active_probes == 0


def test_deadline_crossing_during_transport_settles_attempt_and_breaker_permit(
    tmp_path,
) -> None:
    utc, retry, breaker, _ = _controls()
    for _ in range(2):
        permit = breaker.before_call()
        breaker.record_failure(permit, _Classifier().classify(_Transient("down")))
    utc.advance(10)

    transport = _SlowSuccessTransport(utc)
    cancelled: list[int] = []
    observed: list[RetryAttemptResult] = []
    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
        retry_executor=retry,
        circuit_breaker=breaker,
        attempt_cancellation=cancelled.append,
        attempt_observer=observed.append,
    )

    with pytest.raises(TimeoutError, match="deadline"):
        router.call(_request(), deadline_utc=utc.current + timedelta(seconds=1))

    assert transport.calls == 1
    assert cancelled == []
    assert len(observed) == 1
    assert observed[0].succeeded is True
    assert breaker.snapshot().state == "closed"
