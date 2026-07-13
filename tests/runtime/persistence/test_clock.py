from datetime import datetime, timedelta, timezone

import pytest

from gameforge.contracts.storage import MonotonicClock, UtcClock
from gameforge.runtime.clock import (
    FrozenUtcClock,
    ManualMonotonicClock,
    SystemMonotonicClock,
    SystemUtcClock,
)


def test_system_utc_clock_returns_timezone_aware_utc() -> None:
    observed = SystemUtcClock().now_utc()

    assert observed.tzinfo is not None
    assert observed.utcoffset() == timedelta(0)
    assert isinstance(SystemUtcClock(), UtcClock)
    assert not isinstance(SystemUtcClock(), MonotonicClock)


def test_frozen_utc_clock_returns_the_same_instant() -> None:
    frozen_at = datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc)
    clock = FrozenUtcClock(frozen_at)

    assert clock.now_utc() == frozen_at
    assert clock.now_utc() == frozen_at


@pytest.mark.parametrize(
    "invalid",
    [
        datetime(2026, 7, 13, 9, 30),
        datetime(2026, 7, 13, 9, 30, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_frozen_utc_clock_rejects_naive_or_non_utc_datetime(invalid: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        FrozenUtcClock(invalid)


def test_system_monotonic_clock_returns_nonnegative_integer_nanoseconds() -> None:
    observed = SystemMonotonicClock().now_ns()

    assert type(observed) is int
    assert observed >= 0
    assert isinstance(SystemMonotonicClock(), MonotonicClock)
    assert not isinstance(SystemMonotonicClock(), UtcClock)


def test_manual_monotonic_clock_advances_only_explicitly() -> None:
    clock = ManualMonotonicClock(initial_ns=11)

    assert clock.now_ns() == 11
    assert clock.advance_ns(7) == 18
    assert clock.now_ns() == 18
    assert clock.advance_ns(0) == 18


def test_manual_monotonic_clock_rejects_negative_initial_value() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        ManualMonotonicClock(initial_ns=-1)


def test_manual_monotonic_clock_rejects_backward_advance_without_mutation() -> None:
    clock = ManualMonotonicClock(initial_ns=20)

    with pytest.raises(ValueError, match="nonnegative"):
        clock.advance_ns(-1)

    assert clock.now_ns() == 20


def test_utc_and_monotonic_clock_apis_are_distinct() -> None:
    utc_clock = FrozenUtcClock(datetime(2026, 7, 13, tzinfo=timezone.utc))
    monotonic_clock = ManualMonotonicClock()

    assert not hasattr(utc_clock, "now_ns")
    assert not hasattr(monotonic_clock, "now_utc")
