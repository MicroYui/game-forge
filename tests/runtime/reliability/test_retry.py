from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gameforge.contracts.errors import QuotaExceeded
from gameforge.contracts.reliability import FailureClassificationV1, RetryPolicyV1
from gameforge.runtime.clock import ManualMonotonicClock
from gameforge.runtime.reliability.retry import RetryAttemptResult, RetryExecutor


class _Transient(Exception):
    pass


class _Validation(Exception):
    pass


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 14, tzinfo=UTC)

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class _Classifier:
    version = "classifier@1"

    def classify(self, error: BaseException) -> FailureClassificationV1:
        if isinstance(error, _Transient):
            return FailureClassificationV1(
                failure_kind="transient_infrastructure",
                retryable=True,
                counts_for_breaker=True,
                idempotency_required=True,
                reason_code="gateway_unavailable",
                retry_after_s=getattr(error, "retry_after_s", None),
            )
        return FailureClassificationV1(
            failure_kind="validation",
            retryable=False,
            counts_for_breaker=False,
            idempotency_required=False,
            reason_code="invalid_request",
        )


class _Sleeper:
    def __init__(self, utc: _Clock, monotonic: ManualMonotonicClock) -> None:
        self.utc = utc
        self.monotonic = monotonic
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.utc.advance(seconds)
        self.monotonic.advance_ns(round(seconds * 1_000_000_000))


def _policy(**changes: object) -> RetryPolicyV1:
    values: dict[str, object] = {
        "policy_version": "retry@1",
        "failure_classifier_version": "classifier@1",
        "max_attempts": 3,
        "initial_backoff_ms": 100,
        "max_backoff_ms": 1_000,
        "multiplier": 2,
        "jitter_ratio": 0,
    }
    values.update(changes)
    return RetryPolicyV1(**values)


def _executor(
    *,
    clock: _Clock,
    monotonic: ManualMonotonicClock,
    sleeper: _Sleeper,
    policy: RetryPolicyV1 | None = None,
    jitter: float = 0,
) -> RetryExecutor:
    return RetryExecutor(
        policy=policy or _policy(),
        classifier=_Classifier(),
        utc_clock=clock,
        monotonic_clock=monotonic,
        sleeper=sleeper,
        jitter=lambda: jitter,
    )


def test_retry_is_typed_idempotent_budgeted_and_attempt_observable() -> None:
    utc = _Clock()
    monotonic = ManualMonotonicClock()
    sleeper = _Sleeper(utc, monotonic)
    executor = _executor(clock=utc, monotonic=monotonic, sleeper=sleeper)
    calls: list[int] = []
    reservations: list[int] = []
    observed: list[RetryAttemptResult] = []

    def operation(attempt_no: int) -> str:
        calls.append(attempt_no)
        monotonic.advance_ns(5)
        if attempt_no < 3:
            raise _Transient("temporary")
        return "ok"

    result = executor.run(
        operation,
        idempotent=True,
        deadline_utc=utc.current + timedelta(seconds=10),
        reserve_attempt=reservations.append,
        observe_attempt=observed.append,
    )

    assert result == "ok"
    assert calls == [1, 2, 3]
    assert reservations == [1, 2, 3]
    assert sleeper.calls == [0.1, 0.2]
    assert [item.duration_ns for item in observed] == [5, 5, 5]
    assert [item.succeeded for item in observed] == [False, False, True]
    assert observed[0].classification is not None
    assert observed[0].classification.reason_code == "gateway_unavailable"


@pytest.mark.parametrize("error", [_Validation("bad"), _Transient("temporary")])
def test_non_retryable_or_non_idempotent_failure_is_never_retried(error: Exception) -> None:
    utc = _Clock()
    monotonic = ManualMonotonicClock()
    sleeper = _Sleeper(utc, monotonic)
    executor = _executor(clock=utc, monotonic=monotonic, sleeper=sleeper)
    calls = 0

    def operation(_: int) -> None:
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(type(error), match=str(error)):
        executor.run(
            operation,
            idempotent=not isinstance(error, _Transient),
            deadline_utc=utc.current + timedelta(seconds=10),
        )

    assert calls == 1
    assert sleeper.calls == []


def test_retry_after_and_deadline_prevent_late_attempt() -> None:
    utc = _Clock()
    monotonic = ManualMonotonicClock()
    sleeper = _Sleeper(utc, monotonic)
    executor = _executor(clock=utc, monotonic=monotonic, sleeper=sleeper)
    error = _Transient("retry later")
    error.retry_after_s = 2

    with pytest.raises(_Transient, match="retry later"):
        executor.run(
            lambda _: (_ for _ in ()).throw(error),
            idempotent=True,
            deadline_utc=utc.current + timedelta(seconds=1),
        )

    assert sleeper.calls == []


def test_budget_reservation_failure_stops_before_transport_attempt() -> None:
    utc = _Clock()
    monotonic = ManualMonotonicClock()
    sleeper = _Sleeper(utc, monotonic)
    executor = _executor(clock=utc, monotonic=monotonic, sleeper=sleeper)
    calls: list[int] = []

    def reserve(attempt_no: int) -> None:
        if attempt_no == 2:
            raise QuotaExceeded("attempt budget exhausted")

    def operation(attempt_no: int) -> None:
        calls.append(attempt_no)
        raise _Transient("temporary")

    with pytest.raises(QuotaExceeded, match="attempt budget exhausted"):
        executor.run(
            operation,
            idempotent=True,
            deadline_utc=utc.current + timedelta(seconds=10),
            reserve_attempt=reserve,
        )

    assert calls == [1]
    assert sleeper.calls == [0.1]


def test_deadline_is_rechecked_after_authoritative_attempt_reservation() -> None:
    utc = _Clock()
    monotonic = ManualMonotonicClock()
    sleeper = _Sleeper(utc, monotonic)
    executor = _executor(clock=utc, monotonic=monotonic, sleeper=sleeper)
    calls = 0
    cancelled: list[int] = []

    def reserve(_: int) -> None:
        utc.advance(2)

    def operation(_: int) -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(TimeoutError, match="deadline"):
        executor.run(
            operation,
            idempotent=True,
            deadline_utc=utc.current + timedelta(seconds=1),
            reserve_attempt=reserve,
            cancel_attempt=cancelled.append,
        )
    assert calls == 0
    assert cancelled == [1]


def test_late_success_is_observed_but_never_returned_after_total_deadline() -> None:
    utc = _Clock()
    monotonic = ManualMonotonicClock()
    sleeper = _Sleeper(utc, monotonic)
    executor = _executor(clock=utc, monotonic=monotonic, sleeper=sleeper)
    reservations: list[int] = []
    cancelled: list[int] = []
    observed: list[RetryAttemptResult] = []

    def operation(_: int) -> str:
        utc.advance(2)
        monotonic.advance_ns(2_000_000_000)
        return "too late"

    with pytest.raises(TimeoutError, match="deadline"):
        executor.run(
            operation,
            idempotent=True,
            deadline_utc=utc.current + timedelta(seconds=1),
            reserve_attempt=reservations.append,
            cancel_attempt=cancelled.append,
            observe_attempt=observed.append,
        )

    assert reservations == [1]
    assert cancelled == []
    assert len(observed) == 1
    assert observed[0].succeeded is True
    assert observed[0].duration_ns == 2_000_000_000


def test_jitter_is_injected_bounded_and_retry_after_is_a_floor() -> None:
    utc = _Clock()
    monotonic = ManualMonotonicClock()
    sleeper = _Sleeper(utc, monotonic)
    executor = _executor(
        clock=utc,
        monotonic=monotonic,
        sleeper=sleeper,
        policy=_policy(jitter_ratio=0.5, max_attempts=2),
        jitter=-1,
    )
    error = _Transient("retry later")
    error.retry_after_s = 1
    attempts = 0

    def operation(_: int) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error
        return "ok"

    assert (
        executor.run(
            operation,
            idempotent=True,
            deadline_utc=utc.current + timedelta(seconds=10),
        )
        == "ok"
    )
    assert sleeper.calls == [1.0]

    bad = _executor(
        clock=utc,
        monotonic=monotonic,
        sleeper=sleeper,
        policy=_policy(jitter_ratio=0.5),
        jitter=1.1,
    )
    with pytest.raises(ValueError, match="jitter"):
        bad.run(
            lambda _: (_ for _ in ()).throw(_Transient("temporary")),
            idempotent=True,
            deadline_utc=utc.current + timedelta(seconds=10),
        )
