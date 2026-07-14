from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gameforge.contracts.errors import Conflict, DependencyUnavailable
from gameforge.contracts.reliability import (
    CircuitBreakerConfigV1,
    FailureClassificationV1,
)
from gameforge.runtime.reliability.breaker import CircuitBreaker


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 14, tzinfo=UTC)

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _config(**changes: object) -> CircuitBreakerConfigV1:
    values: dict[str, object] = {
        "config_version": "breaker@1",
        "rolling_window_s": 60,
        "minimum_samples": 2,
        "failure_threshold": 0.5,
        "open_cooldown_s": 10,
        "half_open_max_concurrent_probes": 1,
        "half_open_success_threshold": 2,
    }
    values.update(changes)
    return CircuitBreakerConfigV1(**values)


def _infra() -> FailureClassificationV1:
    return FailureClassificationV1(
        failure_kind="transient_infrastructure",
        retryable=True,
        counts_for_breaker=True,
        idempotency_required=True,
        reason_code="gateway_unavailable",
    )


def _unproven() -> FailureClassificationV1:
    return FailureClassificationV1(
        failure_kind="solver_unproven",
        retryable=False,
        counts_for_breaker=False,
        idempotency_required=False,
        reason_code="solver_unknown",
    )


def test_closed_threshold_opens_and_cooldown_allows_bounded_probes() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(
        dependency_id="model-gateway",
        config=_config(),
        clock=clock,
    )

    first = breaker.before_call()
    breaker.record_success(first)
    second = breaker.before_call()
    breaker.record_failure(second, _infra())
    assert breaker.snapshot().state == "open"

    with pytest.raises(DependencyUnavailable) as blocked:
        breaker.before_call()
    assert blocked.value.context["dependency_id"] == "model-gateway"

    clock.advance(10)
    probe_one = breaker.before_call()
    assert probe_one.is_half_open_probe is True
    with pytest.raises(DependencyUnavailable, match="probe capacity"):
        breaker.before_call()
    breaker.record_success(probe_one)
    assert breaker.snapshot().state == "half_open"

    probe_two = breaker.before_call()
    breaker.record_success(probe_two)
    assert breaker.snapshot().state == "closed"
    assert breaker.snapshot().samples == ()


def test_half_open_infrastructure_failure_reopens() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(
        dependency_id="model-gateway",
        config=_config(minimum_samples=1, failure_threshold=1),
        clock=clock,
    )
    permit = breaker.before_call()
    breaker.record_failure(permit, _infra())
    opened_at = breaker.snapshot().opened_at
    clock.advance(10)
    probe = breaker.before_call()
    breaker.record_failure(probe, _infra())

    state = breaker.snapshot()
    assert state.state == "open"
    assert state.opened_at is not None
    assert state.opened_at > opened_at


def test_unproven_and_other_product_outcomes_never_increment_breaker() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(
        dependency_id="solver",
        config=_config(minimum_samples=1, failure_threshold=1),
        clock=clock,
    )
    for _ in range(5):
        permit = breaker.before_call()
        breaker.record_failure(permit, _unproven())

    state = breaker.snapshot()
    assert state.state == "closed"
    assert state.samples == ()


def test_rolling_window_prunes_old_health_samples() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(
        dependency_id="model-gateway",
        config=_config(rolling_window_s=5, minimum_samples=2, failure_threshold=1),
        clock=clock,
    )
    permit = breaker.before_call()
    breaker.record_failure(permit, _infra())
    clock.advance(6)
    permit = breaker.before_call()
    breaker.record_success(permit)

    state = breaker.snapshot()
    assert state.state == "closed"
    assert len(state.samples) == 1
    assert state.samples[0].infrastructure_failure is False


def test_permits_are_single_completion_tokens() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(
        dependency_id="model-gateway",
        config=_config(),
        clock=clock,
    )
    permit = breaker.before_call()
    breaker.record_success(permit)
    with pytest.raises(Conflict, match="already completed"):
        breaker.record_success(permit)


def test_half_open_waits_for_all_admitted_probes_and_any_failure_reopens() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(
        dependency_id="model-gateway",
        config=_config(
            minimum_samples=1,
            failure_threshold=1,
            half_open_max_concurrent_probes=2,
            half_open_success_threshold=1,
        ),
        clock=clock,
    )
    permit = breaker.before_call()
    breaker.record_failure(permit, _infra())
    clock.advance(10)
    first = breaker.before_call()
    second = breaker.before_call()

    breaker.record_success(first)
    assert breaker.snapshot().state == "half_open"
    with pytest.raises(DependencyUnavailable, match="awaiting"):
        breaker.before_call()

    breaker.record_failure(second, _infra())
    assert breaker.snapshot().state == "open"


@pytest.mark.parametrize("completion", ["cancel", "non_breaker"])
def test_half_open_closes_when_last_outstanding_probe_finishes_neutrally(
    completion: str,
) -> None:
    clock = _Clock()
    breaker = CircuitBreaker(
        dependency_id="model-gateway",
        config=_config(
            minimum_samples=1,
            failure_threshold=1,
            half_open_max_concurrent_probes=2,
            half_open_success_threshold=1,
        ),
        clock=clock,
    )
    failed = breaker.before_call()
    breaker.record_failure(failed, _infra())
    clock.advance(10)
    success = breaker.before_call()
    neutral = breaker.before_call()
    breaker.record_success(success)
    if completion == "cancel":
        breaker.cancel(neutral)
    else:
        breaker.record_failure(neutral, _unproven())
    assert breaker.snapshot().state == "closed"


def test_late_closed_failure_reopens_a_current_half_open_breaker() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(
        dependency_id="model-gateway",
        config=_config(
            minimum_samples=1,
            failure_threshold=1,
            half_open_max_concurrent_probes=1,
            half_open_success_threshold=1,
        ),
        clock=clock,
    )
    late = breaker.before_call()
    opener = breaker.before_call()
    breaker.record_failure(opener, _infra())
    clock.advance(10)
    probe = breaker.before_call()

    breaker.record_failure(late, _infra())
    assert breaker.snapshot().state == "open"
    breaker.record_success(probe)
    assert breaker.snapshot().state == "open"


def test_stale_half_open_success_cannot_complete_a_new_probe_cycle() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(
        dependency_id="model-gateway",
        config=_config(
            minimum_samples=1,
            failure_threshold=1,
            half_open_max_concurrent_probes=2,
            half_open_success_threshold=1,
        ),
        clock=clock,
    )
    opener = breaker.before_call()
    breaker.record_failure(opener, _infra())

    clock.advance(10)
    stale_probe = breaker.before_call()
    failed_probe = breaker.before_call()
    breaker.record_failure(failed_probe, _infra())

    clock.advance(10)
    current_probe = breaker.before_call()
    breaker.record_success(stale_probe)

    state = breaker.snapshot()
    assert state.state == "half_open"
    assert state.half_open_active_probes == 1
    assert state.half_open_successes == 0

    breaker.record_success(current_probe)
    assert breaker.snapshot().state == "closed"
