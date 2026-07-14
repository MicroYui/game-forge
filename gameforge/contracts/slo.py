"""Versioned SLO evaluation and alert wire contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.observability import (
    MetricDescriptorRefV1,
    MetricDescriptorRegistryRefV1,
    MetricLabelMatcherV1,
    MetricUnit,
)


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def _json_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {key: _json_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_data(item) for item in value]
    return value


class MetricPredicateV1(_FrozenModel):
    predicate_schema_version: Literal["metric-predicate@1"] = "metric-predicate@1"
    descriptor: MetricDescriptorRefV1
    allowed_label_matchers: tuple[MetricLabelMatcherV1, ...]
    comparator: Literal["lt", "lte", "eq", "gte", "gt"]
    threshold: float
    unit: MetricUnit

    @field_validator("threshold")
    @classmethod
    def _finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric predicate threshold must be finite")
        return value

    @field_validator("allowed_label_matchers")
    @classmethod
    def _canonical_matchers(
        cls, value: tuple[MetricLabelMatcherV1, ...]
    ) -> tuple[MetricLabelMatcherV1, ...]:
        keys = [item.key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("metric predicate label matchers must have unique keys")
        return tuple(sorted(value, key=lambda item: item.key))


class SLIDefinitionV1(_FrozenModel):
    sli_schema_version: Literal["sli-definition@1"] = "sli-definition@1"
    metric_registry: MetricDescriptorRegistryRefV1
    eligible: MetricPredicateV1
    good: MetricPredicateV1
    total_aggregation: Literal["count", "sum"]
    workload_profile_id: NonEmptyStr
    missing_data: Literal["exclude", "bad", "hold"]
    late_data_grace_s: NonNegativeInt
    policy_version: NonEmptyStr


class WorkloadProfileV1(_FrozenModel):
    profile_schema_version: Literal["workload-profile@1"] = "workload-profile@1"
    profile_id: NonEmptyStr
    dataset_artifact_id: NonEmptyStr
    entity_count: NonNegativeInt
    relation_count: NonNegativeInt
    constraint_count: NonNegativeInt
    task_count: NonNegativeInt | None = None
    concurrency: PositiveInt
    environment_fingerprint: Sha256Hex


class SLODefinitionV1(_FrozenModel):
    slo_schema_version: Literal["slo-definition@1"] = "slo-definition@1"
    slo_id: NonEmptyStr
    name: NonEmptyStr
    sli: SLIDefinitionV1
    objective: float = Field(gt=0, le=1, allow_inf_nan=False)
    rolling_window_s: PositiveInt
    minimum_samples: PositiveInt
    evaluation_interval_s: PositiveInt
    effective_from: datetime
    policy_version: NonEmptyStr

    @field_validator("effective_from")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _evaluation_fits_window(self) -> SLODefinitionV1:
        if self.evaluation_interval_s > self.rolling_window_s:
            raise ValueError("SLO evaluation interval cannot exceed rolling window")
        return self


class SLOEvaluationV1(_FrozenModel):
    evaluation_schema_version: Literal["slo-evaluation@1"] = "slo-evaluation@1"
    evaluation_id: NonEmptyStr
    slo_id: NonEmptyStr
    window_start: datetime
    window_end: datetime
    eligible_count: NonNegativeInt
    good_count: NonNegativeInt
    total_value: float = Field(ge=0, allow_inf_nan=False)
    ratio: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    missing_count: NonNegativeInt
    late_count: NonNegativeInt
    status: Literal["met", "breached", "insufficient_data"]

    @field_validator("window_start", "window_end")
    @classmethod
    def _utc_timestamps(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _closed_evaluation(self) -> SLOEvaluationV1:
        if self.window_start >= self.window_end:
            raise ValueError("SLO evaluation window must be non-empty")
        if self.good_count > self.eligible_count:
            raise ValueError("good count cannot exceed eligible count")
        if (self.status == "insufficient_data") != (self.ratio is None):
            raise ValueError("only insufficient-data evaluation omits ratio")
        if self.ratio is not None:
            if self.eligible_count == 0:
                raise ValueError("reported SLO ratio requires eligible observations")
            if self.ratio != self.good_count / self.eligible_count:
                raise ValueError("SLO ratio must equal good_count / eligible_count")
        expected = "slo-evaluation:sha256:" + canonical_sha256(
            self.model_dump(mode="json", exclude={"evaluation_id"})
        )
        if self.evaluation_id != expected:
            raise ValueError("evaluation_id does not match canonical evaluation")
        return self

    @classmethod
    def create(cls, **values: Any) -> SLOEvaluationV1:
        normalized = dict(values)
        normalized["total_value"] = float(normalized["total_value"])
        if normalized.get("ratio") is not None:
            normalized["ratio"] = float(normalized["ratio"])
        payload = {"evaluation_schema_version": "slo-evaluation@1", **normalized}
        evaluation_id = "slo-evaluation:sha256:" + canonical_sha256(_json_data(payload))
        return cls(evaluation_id=evaluation_id, **normalized)


class AlertRuleV1(_FrozenModel):
    alert_schema_version: Literal["alert-rule@1"] = "alert-rule@1"
    alert_rule_id: NonEmptyStr
    slo_id: NonEmptyStr
    breach_threshold: float = Field(gt=0, allow_inf_nan=False)
    for_duration_s: NonNegativeInt
    severity: Literal["info", "warning", "critical"]
    dedup_key_template: NonEmptyStr
    cooldown_s: NonNegativeInt
    insufficient_data_action: Literal["hold", "resolve", "fire"]
    policy_version: NonEmptyStr


class AlertInstanceV1(_FrozenModel):
    instance_schema_version: Literal["alert-instance@1"] = "alert-instance@1"
    alert_instance_id: NonEmptyStr
    alert_rule_id: NonEmptyStr
    dedup_key: NonEmptyStr
    state: Literal["pending", "firing", "resolved"]
    pending_since: datetime | None = None
    fired_at: datetime | None = None
    resolved_at: datetime | None = None
    last_evaluation_id: NonEmptyStr
    last_delivery_at: datetime | None = None
    revision: PositiveInt

    @field_validator("pending_since", "fired_at", "resolved_at", "last_delivery_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def _state_times(self) -> AlertInstanceV1:
        if self.state == "pending":
            if (
                self.pending_since is None
                or self.fired_at is not None
                or self.resolved_at is not None
            ):
                raise ValueError("pending alert requires only pending_since")
        elif self.state == "firing":
            if self.pending_since is None or self.fired_at is None or self.resolved_at is not None:
                raise ValueError("firing alert requires pending and fired timestamps")
        elif self.resolved_at is None:
            raise ValueError("resolved alert requires resolved_at")
        return self


class AlertDeliveryResultV1(_FrozenModel):
    delivery_schema_version: Literal["alert-delivery@1"] = "alert-delivery@1"
    status: Literal["delivered", "duplicate", "failed"]
    idempotency_key: NonEmptyStr
    detail: str | None = None


class AlertSink(Protocol):
    def deliver(
        self,
        alert: AlertInstanceV1,
        evaluation: SLOEvaluationV1,
        idempotency_key: str,
    ) -> AlertDeliveryResultV1: ...


__all__ = [
    "AlertDeliveryResultV1",
    "AlertInstanceV1",
    "AlertRuleV1",
    "AlertSink",
    "MetricPredicateV1",
    "SLIDefinitionV1",
    "SLODefinitionV1",
    "SLOEvaluationV1",
    "WorkloadProfileV1",
]
