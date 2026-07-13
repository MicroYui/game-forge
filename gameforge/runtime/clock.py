"""Injectable UTC and monotonic clocks with intentionally distinct APIs."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def _require_nonnegative_ns(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer number of nanoseconds")
    if value < 0:
        raise ValueError(f"{field_name} must be nonnegative")
    return value


class SystemUtcClock:
    """UTC authority for persisted timestamps and deadlines."""

    __slots__ = ()

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class FrozenUtcClock:
    """UTC authority fixed to one instant for deterministic tests and replay."""

    frozen_at: datetime

    def __post_init__(self) -> None:
        if self.frozen_at.tzinfo is None or self.frozen_at.utcoffset() is None:
            raise ValueError("frozen_at must be timezone-aware UTC")
        if self.frozen_at.utcoffset() != timedelta(0):
            raise ValueError("frozen_at must use UTC")
        object.__setattr__(self, "frozen_at", self.frozen_at.astimezone(timezone.utc))

    def now_utc(self) -> datetime:
        return self.frozen_at


class SystemMonotonicClock:
    """Monotonic authority for elapsed durations, never persisted wall time."""

    __slots__ = ()

    def now_ns(self) -> int:
        return time.monotonic_ns()


@dataclass(slots=True, init=False)
class ManualMonotonicClock:
    """Explicitly advanced monotonic clock for deterministic tests."""

    _current_ns: int = field(repr=False)

    def __init__(self, initial_ns: int = 0) -> None:
        self._current_ns = _require_nonnegative_ns(initial_ns, field_name="initial_ns")

    def now_ns(self) -> int:
        return self._current_ns

    def advance_ns(self, delta_ns: int) -> int:
        delta = _require_nonnegative_ns(delta_ns, field_name="delta_ns")
        self._current_ns += delta
        return self._current_ns
