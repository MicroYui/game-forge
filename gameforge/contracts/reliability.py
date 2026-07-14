"""Typed retry classification and circuit-breaker contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
FailureKind = Literal[
    "transient_infrastructure",
    "permanent_infrastructure",
    "authentication",
    "quota",
    "validation",
    "solver_unproven",
    "product_rejection",
    "cancelled",
    "deadline_exceeded",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


class FailureClassificationV1(_FrozenModel):
    classification_schema_version: Literal["failure-classification@1"] = "failure-classification@1"
    failure_kind: FailureKind
    retryable: bool
    counts_for_breaker: bool
    idempotency_required: bool
    reason_code: NonEmptyStr
    retry_after_s: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _closed_classification(self) -> FailureClassificationV1:
        infrastructure = self.failure_kind in {
            "transient_infrastructure",
            "permanent_infrastructure",
        }
        if self.counts_for_breaker and not infrastructure:
            raise ValueError("only classified infrastructure failures count for breaker")
        if self.retryable and self.failure_kind != "transient_infrastructure":
            raise ValueError("only transient infrastructure failures are retryable")
        if self.retryable and not self.idempotency_required:
            raise ValueError("retryable failures require an idempotent operation")
        if self.retry_after_s is not None and not self.retryable:
            raise ValueError("Retry-After belongs only to a retryable classification")
        return self


class RetryPolicyV1(_FrozenModel):
    retry_schema_version: Literal["retry-policy@1"] = "retry-policy@1"
    policy_version: NonEmptyStr
    failure_classifier_version: NonEmptyStr
    max_attempts: PositiveInt
    initial_backoff_ms: NonNegativeInt
    max_backoff_ms: NonNegativeInt
    multiplier: float = Field(ge=1)
    jitter_ratio: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _bounded_backoff(self) -> RetryPolicyV1:
        if self.max_backoff_ms < self.initial_backoff_ms:
            raise ValueError("max backoff must be at least initial backoff")
        return self


class CircuitBreakerConfigV1(_FrozenModel):
    config_schema_version: Literal["circuit-breaker-config@1"] = "circuit-breaker-config@1"
    config_version: NonEmptyStr
    rolling_window_s: PositiveInt
    minimum_samples: PositiveInt
    failure_threshold: float = Field(gt=0, le=1)
    open_cooldown_s: PositiveInt
    half_open_max_concurrent_probes: PositiveInt
    half_open_success_threshold: PositiveInt


class BreakerSampleV1(_FrozenModel):
    occurred_at: datetime
    infrastructure_failure: bool

    @field_validator("occurred_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)


class CircuitBreakerStateV1(_FrozenModel):
    state_schema_version: Literal["circuit-breaker-state@1"] = "circuit-breaker-state@1"
    dependency_id: NonEmptyStr
    config_version: NonEmptyStr
    state: Literal["closed", "open", "half_open"]
    samples: tuple[BreakerSampleV1, ...]
    opened_at: datetime | None = None
    half_open_active_probes: NonNegativeInt = 0
    half_open_successes: NonNegativeInt = 0
    revision: PositiveInt

    @field_validator("opened_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @field_validator("samples")
    @classmethod
    def _ordered_samples(cls, value: tuple[BreakerSampleV1, ...]) -> tuple[BreakerSampleV1, ...]:
        if tuple(sorted(value, key=lambda item: item.occurred_at)) != value:
            raise ValueError("breaker samples must be ordered by occurrence time")
        return value

    @model_validator(mode="after")
    def _state_shape(self) -> CircuitBreakerStateV1:
        if self.state == "closed":
            if (
                self.opened_at is not None
                or self.half_open_active_probes
                or self.half_open_successes
            ):
                raise ValueError("closed breaker excludes open/half-open fields")
        elif self.opened_at is None:
            raise ValueError("open and half-open breaker states require opened_at")
        if self.state != "half_open" and (self.half_open_active_probes or self.half_open_successes):
            raise ValueError("half-open counters belong only to half_open state")
        return self


class FailureClassifier(Protocol):
    @property
    def version(self) -> str: ...

    def classify(self, error: BaseException) -> FailureClassificationV1: ...


__all__ = [
    "BreakerSampleV1",
    "CircuitBreakerConfigV1",
    "CircuitBreakerStateV1",
    "FailureClassificationV1",
    "FailureClassifier",
    "FailureKind",
    "RetryPolicyV1",
]
