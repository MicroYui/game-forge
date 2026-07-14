"""Typed, deadline-aware retry execution with authoritative attempt admission."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Generic, Protocol, TypeVar

from gameforge.contracts.reliability import (
    FailureClassificationV1,
    FailureClassifier,
    RetryPolicyV1,
)
from gameforge.contracts.storage import MonotonicClock, UtcClock


T = TypeVar("T")


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class SystemSleeper:
    __slots__ = ()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True, slots=True)
class RetryAttemptResult:
    attempt_no: int
    started_at: datetime
    duration_ns: int
    succeeded: bool
    classification: FailureClassificationV1 | None


class RetryExecutor(Generic[T]):
    """Run explicitly classified retries without owning cost or trace policy.

    ``reserve_attempt`` is the authoritative pre-call admission hook. It runs for
    every transport attempt and may fail closed. ``observe_attempt`` receives one
    completion for every operation that actually started, which lets composition
    attach cost reconciliation and a span without hiding either concern here.
    """

    def __init__(
        self,
        *,
        policy: RetryPolicyV1,
        classifier: FailureClassifier,
        utc_clock: UtcClock,
        monotonic_clock: MonotonicClock,
        sleeper: Sleeper,
        jitter: Callable[[], float],
    ) -> None:
        if classifier.version != policy.failure_classifier_version:
            raise ValueError("retry policy and failure classifier versions differ")
        self._policy = policy
        self._classifier = classifier
        self._utc_clock = utc_clock
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._jitter = jitter

    def run(
        self,
        operation: Callable[[int], T],
        *,
        idempotent: bool,
        deadline_utc: datetime,
        reserve_attempt: Callable[[int], None] | None = None,
        cancel_attempt: Callable[[int], None] | None = None,
        observe_attempt: Callable[[RetryAttemptResult], None] | None = None,
    ) -> T:
        deadline = _require_utc(deadline_utc, field_name="deadline_utc")
        reserve = reserve_attempt or _noop_reserve
        cancel = cancel_attempt or _noop_reserve
        observe = observe_attempt or _noop_observe

        for attempt_no in range(1, self._policy.max_attempts + 1):
            if self._now_utc() >= deadline:
                raise TimeoutError("retry deadline has elapsed")
            reserve(attempt_no)
            if self._now_utc() >= deadline:
                cancel(attempt_no)
                raise TimeoutError("retry deadline elapsed during attempt reservation")
            started_at = self._now_utc()
            started_ns = self._monotonic_clock.now_ns()
            try:
                result = operation(attempt_no)
            except BaseException as error:
                classification = self._classifier.classify(error)
                observe(
                    RetryAttemptResult(
                        attempt_no=attempt_no,
                        started_at=started_at,
                        duration_ns=_duration_ns(started_ns, self._monotonic_clock.now_ns()),
                        succeeded=False,
                        classification=classification,
                    )
                )
                if not self._may_retry(
                    classification,
                    idempotent=idempotent,
                    attempt_no=attempt_no,
                ):
                    raise
                delay_s = self._retry_delay_s(
                    attempt_no=attempt_no,
                    classification=classification,
                )
                if self._now_utc() + timedelta(seconds=delay_s) >= deadline:
                    raise
                self._sleeper.sleep(delay_s)
            else:
                ended_ns = self._monotonic_clock.now_ns()
                completed_after_deadline = self._now_utc() >= deadline
                observe(
                    RetryAttemptResult(
                        attempt_no=attempt_no,
                        started_at=started_at,
                        duration_ns=_duration_ns(started_ns, ended_ns),
                        succeeded=True,
                        classification=None,
                    )
                )
                if completed_after_deadline:
                    raise TimeoutError("retry deadline elapsed during transport attempt")
                return result

        raise AssertionError("retry loop exhausted without returning or raising")

    def remaining_deadline_s(self, deadline_utc: datetime) -> float:
        """Return the current positive UTC budget for one external attempt."""

        deadline = _require_utc(deadline_utc, field_name="deadline_utc")
        remaining = (deadline - self._now_utc()).total_seconds()
        if remaining <= 0:
            raise TimeoutError("retry deadline has elapsed")
        return remaining

    def _may_retry(
        self,
        classification: FailureClassificationV1,
        *,
        idempotent: bool,
        attempt_no: int,
    ) -> bool:
        return (
            classification.retryable
            and (idempotent or not classification.idempotency_required)
            and attempt_no < self._policy.max_attempts
        )

    def _retry_delay_s(
        self,
        *,
        attempt_no: int,
        classification: FailureClassificationV1,
    ) -> float:
        raw_ms = self._policy.initial_backoff_ms * (self._policy.multiplier ** (attempt_no - 1))
        bounded_ms = min(float(self._policy.max_backoff_ms), float(raw_ms))
        jitter_sample = self._jitter()
        if not isinstance(jitter_sample, (int, float)) or isinstance(jitter_sample, bool):
            raise TypeError("jitter source must return a number")
        if not -1 <= jitter_sample <= 1:
            raise ValueError("jitter source must return a value in [-1, 1]")
        jittered_s = bounded_ms / 1000 * (1 + self._policy.jitter_ratio * float(jitter_sample))
        retry_after_s = float(classification.retry_after_s or 0)
        return max(jittered_s, retry_after_s)

    def _now_utc(self) -> datetime:
        return _require_utc(self._utc_clock.now_utc(), field_name="utc_clock")


def _duration_ns(started_ns: int, ended_ns: int) -> int:
    if ended_ns < started_ns:
        raise ValueError("monotonic clock moved backwards")
    return ended_ns - started_ns


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _noop_reserve(_: int) -> None:
    return None


def _noop_observe(_: RetryAttemptResult) -> None:
    return None


__all__ = ["RetryAttemptResult", "RetryExecutor", "Sleeper", "SystemSleeper"]
