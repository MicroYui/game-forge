"""Dependency-scoped in-process circuit breaker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Literal

from gameforge.contracts.errors import Conflict, DependencyUnavailable
from gameforge.contracts.reliability import (
    BreakerSampleV1,
    CircuitBreakerConfigV1,
    CircuitBreakerStateV1,
    FailureClassificationV1,
)
from gameforge.contracts.storage import UtcClock


@dataclass(frozen=True, slots=True)
class BreakerPermit:
    dependency_id: str
    token: int
    is_half_open_probe: bool
    half_open_generation: int | None = None


@dataclass(frozen=True, slots=True)
class _IssuedPermit:
    permit: BreakerPermit
    admitted_state: Literal["closed", "half_open"]
    half_open_generation: int | None


class CircuitBreaker:
    """Replayable breaker that counts classified infrastructure health only."""

    def __init__(
        self,
        *,
        dependency_id: str,
        config: CircuitBreakerConfigV1,
        clock: UtcClock,
        initial_state: CircuitBreakerStateV1 | None = None,
    ) -> None:
        if not dependency_id:
            raise ValueError("dependency_id must be non-empty")
        self._dependency_id = dependency_id
        self._config = config
        self._clock = clock
        self._lock = RLock()
        self._next_token = 1
        self._issued: dict[int, _IssuedPermit] = {}
        if initial_state is None:
            self._state = CircuitBreakerStateV1(
                dependency_id=dependency_id,
                config_version=config.config_version,
                state="closed",
                samples=(),
                revision=1,
            )
        else:
            if initial_state.dependency_id != dependency_id:
                raise ValueError("initial breaker dependency differs")
            if initial_state.config_version != config.config_version:
                raise ValueError("initial breaker config version differs")
            self._state = initial_state
        self._half_open_generation = 1 if self._state.state == "half_open" else 0

    def snapshot(self) -> CircuitBreakerStateV1:
        with self._lock:
            self._prune_samples(self._now())
            return self._state

    def before_call(self) -> BreakerPermit:
        with self._lock:
            now = self._now()
            self._prune_samples(now)
            if self._state.state == "open":
                assert self._state.opened_at is not None
                if now < self._state.opened_at + timedelta(seconds=self._config.open_cooldown_s):
                    self._raise_unavailable("breaker is open")
                self._half_open_generation += 1
                self._state = self._state.model_copy(
                    update={
                        "state": "half_open",
                        "half_open_active_probes": 0,
                        "half_open_successes": 0,
                        "revision": self._state.revision + 1,
                    }
                )
            if (
                self._state.state == "half_open"
                and self._state.half_open_successes >= self._config.half_open_success_threshold
            ):
                self._raise_unavailable("half-open breaker is awaiting admitted probes")
            if (
                self._state.state == "half_open"
                and self._state.half_open_active_probes
                >= self._config.half_open_max_concurrent_probes
            ):
                self._raise_unavailable("half-open probe capacity is exhausted")

            probe = self._state.state == "half_open"
            token = self._next_token
            self._next_token += 1
            permit = BreakerPermit(
                dependency_id=self._dependency_id,
                token=token,
                is_half_open_probe=probe,
                half_open_generation=self._half_open_generation if probe else None,
            )
            self._issued[token] = _IssuedPermit(
                permit=permit,
                admitted_state="half_open" if probe else "closed",
                half_open_generation=permit.half_open_generation,
            )
            if probe:
                self._state = self._state.model_copy(
                    update={
                        "half_open_active_probes": (self._state.half_open_active_probes + 1),
                        "revision": self._state.revision + 1,
                    }
                )
            return permit

    def record_success(self, permit: BreakerPermit) -> CircuitBreakerStateV1:
        with self._lock:
            issued = self._consume(permit)
            now = self._now()
            if issued.admitted_state == "half_open":
                if not self._is_current_half_open_generation(issued):
                    return self._state
                if self._state.state != "half_open":
                    return self._state
                active = max(0, self._state.half_open_active_probes - 1)
                successes = self._state.half_open_successes + 1
                if successes >= self._config.half_open_success_threshold and active == 0:
                    self._close()
                else:
                    self._state = self._state.model_copy(
                        update={
                            "half_open_active_probes": active,
                            "half_open_successes": successes,
                            "revision": self._state.revision + 1,
                        }
                    )
                return self._state

            if self._state.state != "closed":
                return self._state
            self._append_closed_sample(now, infrastructure_failure=False)
            return self._state

    def record_failure(
        self,
        permit: BreakerPermit,
        classification: FailureClassificationV1,
    ) -> CircuitBreakerStateV1:
        with self._lock:
            issued = self._consume(permit)
            now = self._now()
            if issued.admitted_state == "half_open" and not self._is_current_half_open_generation(
                issued
            ):
                return self._state
            if not classification.counts_for_breaker:
                if issued.admitted_state == "half_open" and self._state.state == "half_open":
                    self._complete_neutral_probe()
                return self._state

            if self._state.state in {"open", "half_open"}:
                self._open(now)
                return self._state

            self._append_closed_sample(now, infrastructure_failure=True)
            return self._state

    def cancel(self, permit: BreakerPermit) -> CircuitBreakerStateV1:
        """Release an admitted call that did not reach the dependency."""

        with self._lock:
            issued = self._consume(permit)
            if (
                issued.admitted_state == "half_open"
                and self._is_current_half_open_generation(issued)
                and self._state.state == "half_open"
            ):
                self._complete_neutral_probe()
            return self._state

    def _is_current_half_open_generation(self, issued: _IssuedPermit) -> bool:
        return issued.half_open_generation == self._half_open_generation

    def _complete_neutral_probe(self) -> None:
        active = max(0, self._state.half_open_active_probes - 1)
        if (
            active == 0
            and self._state.half_open_successes >= self._config.half_open_success_threshold
        ):
            self._close()
            return
        self._state = self._state.model_copy(
            update={
                "half_open_active_probes": active,
                "revision": self._state.revision + 1,
            }
        )

    def _close(self) -> None:
        self._state = CircuitBreakerStateV1(
            dependency_id=self._dependency_id,
            config_version=self._config.config_version,
            state="closed",
            samples=(),
            revision=self._state.revision + 1,
        )

    def _append_closed_sample(
        self,
        now: datetime,
        *,
        infrastructure_failure: bool,
    ) -> None:
        samples = (
            *self._state.samples,
            BreakerSampleV1(
                occurred_at=now,
                infrastructure_failure=infrastructure_failure,
            ),
        )
        self._state = self._state.model_copy(
            update={"samples": samples, "revision": self._state.revision + 1}
        )
        self._prune_samples(now)
        if self._state.state != "closed":
            return
        if len(self._state.samples) < self._config.minimum_samples:
            return
        failures = sum(item.infrastructure_failure for item in self._state.samples)
        if failures / len(self._state.samples) >= self._config.failure_threshold:
            self._open(now)

    def _open(self, now: datetime) -> None:
        self._state = CircuitBreakerStateV1(
            dependency_id=self._dependency_id,
            config_version=self._config.config_version,
            state="open",
            samples=self._state.samples,
            opened_at=now,
            revision=self._state.revision + 1,
        )

    def _prune_samples(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._config.rolling_window_s)
        retained = tuple(item for item in self._state.samples if item.occurred_at >= cutoff)
        if retained != self._state.samples:
            self._state = self._state.model_copy(
                update={"samples": retained, "revision": self._state.revision + 1}
            )

    def _consume(self, permit: BreakerPermit) -> _IssuedPermit:
        if permit.dependency_id != self._dependency_id:
            raise Conflict("breaker permit belongs to another dependency")
        issued = self._issued.pop(permit.token, None)
        if issued is None:
            raise Conflict("breaker permit was already completed or is unknown")
        if issued.permit != permit:
            raise Conflict("breaker permit payload differs from issued token")
        return issued

    def _now(self) -> datetime:
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("breaker clock must return timezone-aware UTC")
        return now.astimezone(UTC)

    def _raise_unavailable(self, detail: str) -> None:
        raise DependencyUnavailable(
            detail,
            dependency_id=self._dependency_id,
            breaker_state=self._state.state,
            config_version=self._config.config_version,
        )


__all__ = ["BreakerPermit", "CircuitBreaker"]
