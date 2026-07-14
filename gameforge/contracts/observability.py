"""M4b observability wire contracts and adapter Protocols.

The DTOs in this module are operational records. They are intentionally kept
separate from deterministic spine payloads and artifact identity inputs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_json, canonical_sha256


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
OpaqueCursor = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
TelemetryKey = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"),
]
TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
SpanId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
TraceFlags = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{2}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]

MAX_ATTRIBUTE_COUNT = 64
MAX_EVENT_COUNT = 128
MAX_LINK_COUNT = 32
MAX_TELEMETRY_STRING_BYTES = 4096
MAX_TELEMETRY_ARRAY_ITEMS = 32
MAX_TELEMETRY_PAYLOAD_BYTES = 32 * 1024
MAX_QUERY_TIME_RANGE = timedelta(days=7)
MAX_QUERY_PAGE_SIZE = 1000
MAX_QUERY_FILTER_ITEMS = 64
MAX_QUERY_DESCRIPTOR_REFS = 64
MAX_QUERY_MATCHER_VALUES = 64
MAX_QUERY_RESOLUTION_S = 24 * 60 * 60
MAX_QUERY_POINTS = 10_000
MAX_QUERY_SERIES = 500
FORBIDDEN_METRIC_LABEL_KEYS = frozenset(
    {"run_id", "span_id", "trace_id", "artifact_id", "principal_id"}
)
METRIC_UNITS = frozenset(
    {"1", "count", "ratio", "token", "request", "step", "ns", "ms", "s", "byte"}
)

MetricType = Literal["counter", "histogram", "gauge"]
MetricUnit = Literal["1", "count", "ratio", "token", "request", "step", "ns", "ms", "s", "byte"]
SpanStatus = Literal["unset", "ok", "error"]
LogLevel = Literal["debug", "info", "warning", "error", "critical"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _json_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _json_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_data(item) for item in value]
    return value


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def _stable_unique_strings(values: Sequence[str], *, non_empty: bool = False) -> tuple[str, ...]:
    canonical = tuple(sorted(set(values)))
    if len(canonical) != len(values):
        raise ValueError("collection must contain unique values")
    if non_empty and not canonical:
        raise ValueError("collection must be non-empty")
    return canonical


def _validate_telemetry_value(value: JsonValue) -> None:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_TELEMETRY_STRING_BYTES:
            raise ValueError("telemetry string exceeds the byte limit")
        return
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("telemetry numbers must be finite")
        return
    if isinstance(value, list):
        if len(value) > MAX_TELEMETRY_ARRAY_ITEMS:
            raise ValueError("telemetry array exceeds the item limit")
        for item in value:
            if isinstance(item, (list, dict)):
                raise ValueError("telemetry arrays may contain primitives only")
            _validate_telemetry_value(item)
        return
    raise ValueError("telemetry values may contain only primitives or bounded primitive arrays")


def _validate_telemetry_fields(
    value: Mapping[str, JsonValue], *, max_items: int
) -> dict[str, JsonValue]:
    if len(value) > max_items:
        raise ValueError("telemetry field count exceeds the limit")
    canonical = {key: value[key] for key in sorted(value)}
    for item in canonical.values():
        _validate_telemetry_value(item)
    if len(canonical_json(canonical).encode("utf-8")) > MAX_TELEMETRY_PAYLOAD_BYTES:
        raise ValueError("telemetry payload exceeds the byte limit")
    return canonical


class RunCorrelationV1(_FrozenModel):
    correlation_schema_version: Literal["run-correlation@1"] = "run-correlation@1"
    run_id: NonEmptyStr
    attempt_no: PositiveInt | None = None


class TraceContextV1(_FrozenModel):
    context_schema_version: Literal["trace-context@1"] = "trace-context@1"
    trace_id: TraceId
    span_id: SpanId
    trace_flags: TraceFlags
    trace_state: Annotated[str, StringConstraints(max_length=512)] | None = None

    @model_validator(mode="after")
    def _nonzero_ids(self) -> TraceContextV1:
        if set(self.trace_id) == {"0"} or set(self.span_id) == {"0"}:
            raise ValueError("trace and span ids cannot be all zero")
        return self


class SpanLinkV1(_FrozenModel):
    context: TraceContextV1
    attributes: dict[TelemetryKey, JsonValue] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def _bounded_attributes(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_telemetry_fields(value, max_items=MAX_ATTRIBUTE_COUNT)


class SpanEventV1(_FrozenModel):
    name: NonEmptyStr
    occurred_at: datetime
    attributes: dict[TelemetryKey, JsonValue] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("attributes")
    @classmethod
    def _bounded_attributes(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_telemetry_fields(value, max_items=MAX_ATTRIBUTE_COUNT)


class SpanErrorV1(_FrozenModel):
    error_type: NonEmptyStr
    message: Annotated[str, StringConstraints(max_length=2048)]
    stack_fingerprint: Sha256Hex | None = None


class SpanDataV1(_FrozenModel):
    span_schema_version: Literal["span-data@1"] = "span-data@1"
    trace_id: TraceId
    span_id: SpanId
    parent_span_id: SpanId | None
    name: NonEmptyStr
    attributes: dict[TelemetryKey, JsonValue]
    links: tuple[SpanLinkV1, ...]
    events: tuple[SpanEventV1, ...]
    status: SpanStatus
    error: SpanErrorV1 | None
    resource: dict[TelemetryKey, JsonValue]
    started_at: datetime
    ended_at: datetime
    duration_ns: NonNegativeInt

    @field_validator("started_at", "ended_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("attributes", "resource")
    @classmethod
    def _bounded_fields(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_telemetry_fields(value, max_items=MAX_ATTRIBUTE_COUNT)

    @field_validator("links")
    @classmethod
    def _bounded_links(cls, value: tuple[SpanLinkV1, ...]) -> tuple[SpanLinkV1, ...]:
        if len(value) > MAX_LINK_COUNT:
            raise ValueError("span links exceed the limit")
        return value

    @field_validator("events")
    @classmethod
    def _bounded_events(cls, value: tuple[SpanEventV1, ...]) -> tuple[SpanEventV1, ...]:
        if len(value) > MAX_EVENT_COUNT:
            raise ValueError("span events exceed the limit")
        return value

    @model_validator(mode="after")
    def _status_error_pair(self) -> SpanDataV1:
        if self.error is not None and self.status != "error":
            raise ValueError("span error details require error status")
        return self


class TimeRangeV1(_FrozenModel):
    start_utc: datetime
    end_utc: datetime

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _utc_timestamps(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _nonempty_range(self) -> TimeRangeV1:
        if self.start_utc >= self.end_utc:
            raise ValueError("time range uses non-empty [start,end) bounds")
        if self.end_utc - self.start_utc > MAX_QUERY_TIME_RANGE:
            raise ValueError("time range exceeds the query limit")
        return self


class TraceQueryV1(_FrozenModel):
    query_schema_version: Literal["trace-query@1"] = "trace-query@1"
    run_id: NonEmptyStr | None = None
    service: NonEmptyStr | None = None
    status: SpanStatus | None = None
    time_range: TimeRangeV1
    cursor: OpaqueCursor | None = None
    limit: int = Field(gt=0, le=MAX_QUERY_PAGE_SIZE)
    authz_fingerprint: Sha256Hex


class TraceSummaryV1(_FrozenModel):
    trace_schema_version: Literal["trace-summary@1"] = "trace-summary@1"
    trace_id: TraceId
    root_span_id: SpanId | None = None
    run_ids: tuple[NonEmptyStr, ...]
    started_at: datetime
    ended_at: datetime | None = None
    duration_ns: NonNegativeInt | None = None
    status: SpanStatus
    span_count: NonNegativeInt
    service_names: tuple[NonEmptyStr, ...]
    truncated: bool

    @field_validator("run_ids", "service_names")
    @classmethod
    def _stable_sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


class TraceSummaryPageV1(_FrozenModel):
    page_schema_version: Literal["trace-summary-page@1"] = "trace-summary-page@1"
    items: tuple[TraceSummaryV1, ...] = Field(max_length=MAX_QUERY_PAGE_SIZE)
    next_cursor: OpaqueCursor | None = None
    coverage_start: datetime
    coverage_end: datetime
    truncated: bool

    @field_validator("coverage_start", "coverage_end")
    @classmethod
    def _utc_timestamps(cls, value: datetime) -> datetime:
        return _require_utc(value)


class SpanViewV1(_FrozenModel):
    span: SpanDataV1
    redacted_attribute_keys: tuple[TelemetryKey, ...] = ()
    redacted_event_fields: tuple[TelemetryKey, ...] = ()

    @field_validator("redacted_attribute_keys", "redacted_event_fields")
    @classmethod
    def _stable_sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value)


class SpanPageV1(_FrozenModel):
    page_schema_version: Literal["span-page@1"] = "span-page@1"
    trace_id: TraceId
    items: tuple[SpanViewV1, ...] = Field(max_length=MAX_QUERY_PAGE_SIZE)
    next_cursor: OpaqueCursor | None = None
    truncated: bool


class MetricDescriptorRegistryRefV1(_FrozenModel):
    registry_version: PositiveInt
    registry_digest: Sha256Hex


class MetricDescriptorRefV1(_FrozenModel):
    metric_name: NonEmptyStr
    descriptor_version: PositiveInt
    descriptor_digest: Sha256Hex


def compute_metric_descriptor_digest(payload: Mapping[str, Any] | BaseModel) -> str:
    raw = _json_data(payload)
    raw.pop("descriptor_digest", None)
    raw["descriptor_schema_version"] = raw.get("descriptor_schema_version", "metric-descriptor@1")
    raw["unit_schema_version"] = raw.get("unit_schema_version", "metric-units@1")
    raw["label_keys"] = sorted(raw.get("label_keys", ()))
    raw["histogram_bucket_bounds"] = list(raw.get("histogram_bucket_bounds", ()))
    return canonical_sha256(raw)


class MetricDescriptorV1(_FrozenModel):
    descriptor_schema_version: Literal["metric-descriptor@1"] = "metric-descriptor@1"
    metric_name: NonEmptyStr
    descriptor_version: PositiveInt
    metric_type: MetricType
    unit_schema_version: Literal["metric-units@1"] = "metric-units@1"
    unit: MetricUnit
    label_keys: tuple[TelemetryKey, ...]
    histogram_bucket_bounds: tuple[float, ...]
    series_limit: PositiveInt
    descriptor_digest: Sha256Hex

    @field_validator("label_keys")
    @classmethod
    def _canonical_label_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        forbidden = FORBIDDEN_METRIC_LABEL_KEYS.intersection(value)
        if forbidden:
            raise ValueError(
                "high-cardinality metric labels are forbidden: " + ", ".join(sorted(forbidden))
            )
        return _stable_unique_strings(value)

    @field_validator("histogram_bucket_bounds")
    @classmethod
    def _finite_bounds(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("histogram bucket bounds must be finite")
        if any(left >= right for left, right in zip(value, value[1:])):
            raise ValueError("histogram bucket bounds must be strictly increasing")
        return value

    @model_validator(mode="after")
    def _shape_and_digest(self) -> MetricDescriptorV1:
        if self.metric_type == "histogram" and not self.histogram_bucket_bounds:
            raise ValueError("histogram descriptors require bucket bounds")
        if self.metric_type != "histogram" and self.histogram_bucket_bounds:
            raise ValueError("only histogram descriptors accept bucket bounds")
        if self.unit not in METRIC_UNITS:
            raise ValueError("unknown metric unit")
        if self.descriptor_digest != compute_metric_descriptor_digest(self):
            raise ValueError("descriptor_digest does not match canonical descriptor payload")
        return self

    @property
    def ref(self) -> MetricDescriptorRefV1:
        return MetricDescriptorRefV1(
            metric_name=self.metric_name,
            descriptor_version=self.descriptor_version,
            descriptor_digest=self.descriptor_digest,
        )


def compute_metric_registry_digest(payload: Mapping[str, Any] | BaseModel) -> str:
    raw = _json_data(payload)
    raw.pop("registry_digest", None)
    raw["registry_schema_version"] = raw.get(
        "registry_schema_version", "metric-descriptor-registry@1"
    )
    raw["descriptors"] = sorted(
        raw.get("descriptors", ()),
        key=lambda item: (item["metric_name"], item["descriptor_version"]),
    )
    return canonical_sha256(raw)


class MetricDescriptorRegistryV1(_FrozenModel):
    registry_schema_version: Literal["metric-descriptor-registry@1"] = (
        "metric-descriptor-registry@1"
    )
    registry_version: PositiveInt
    descriptors: tuple[MetricDescriptorV1, ...]
    global_series_limit: PositiveInt
    registry_digest: Sha256Hex

    @field_validator("descriptors")
    @classmethod
    def _canonical_descriptors(
        cls, value: tuple[MetricDescriptorV1, ...]
    ) -> tuple[MetricDescriptorV1, ...]:
        identities = [(item.metric_name, item.descriptor_version) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("metric descriptor identities must be unique")
        return tuple(sorted(value, key=lambda item: (item.metric_name, item.descriptor_version)))

    @model_validator(mode="after")
    def _digest_and_limits(self) -> MetricDescriptorRegistryV1:
        if any(item.series_limit > self.global_series_limit for item in self.descriptors):
            raise ValueError("descriptor series limit cannot exceed registry global limit")
        if self.registry_digest != compute_metric_registry_digest(self):
            raise ValueError("registry_digest does not match canonical registry payload")
        return self

    @property
    def ref(self) -> MetricDescriptorRegistryRefV1:
        return MetricDescriptorRegistryRefV1(
            registry_version=self.registry_version,
            registry_digest=self.registry_digest,
        )


class MetricLabelMatcherV1(_FrozenModel):
    key: TelemetryKey
    operation: Literal["eq", "in"]
    values: tuple[NonEmptyStr, ...] = Field(
        min_length=1,
        max_length=MAX_QUERY_MATCHER_VALUES,
    )

    @field_validator("values")
    @classmethod
    def _canonical_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value, non_empty=True)

    @model_validator(mode="after")
    def _operation_shape(self) -> MetricLabelMatcherV1:
        if self.operation == "eq" and len(self.values) != 1:
            raise ValueError("eq matcher requires exactly one value")
        return self


class MetricPointV1(_FrozenModel):
    point_schema_version: Literal["metric-point@1"] = "metric-point@1"
    point_id: NonEmptyStr
    descriptor: MetricDescriptorRefV1
    metric_type: MetricType
    ts_utc: datetime
    value: float
    labels: dict[TelemetryKey, NonEmptyStr]

    @field_validator("ts_utc")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("labels")
    @classmethod
    def _canonical_labels(cls, value: dict[str, str]) -> dict[str, str]:
        forbidden = FORBIDDEN_METRIC_LABEL_KEYS.intersection(value)
        if forbidden:
            raise ValueError("high-cardinality metric labels are forbidden")
        return {key: value[key] for key in sorted(value)}

    @model_validator(mode="after")
    def _finite_value(self) -> MetricPointV1:
        if not math.isfinite(self.value):
            raise ValueError("metric value must be finite")
        if self.metric_type == "counter" and self.value < 0:
            raise ValueError("counter delta must be non-negative")
        return self


class MetricQueryV1(_FrozenModel):
    query_schema_version: Literal["metric-query@1"] = "metric-query@1"
    descriptor_refs: tuple[MetricDescriptorRefV1, ...] = Field(
        min_length=1,
        max_length=MAX_QUERY_DESCRIPTOR_REFS,
    )
    time_range: TimeRangeV1
    resolution_s: int = Field(gt=0, le=MAX_QUERY_RESOLUTION_S)
    label_matchers: tuple[MetricLabelMatcherV1, ...] = Field(
        max_length=MAX_QUERY_FILTER_ITEMS,
    )
    max_points: int = Field(gt=0, le=MAX_QUERY_POINTS)
    cursor: OpaqueCursor | None = None
    series_limit: int = Field(gt=0, le=MAX_QUERY_SERIES)
    authz_fingerprint: Sha256Hex

    @field_validator("descriptor_refs")
    @classmethod
    def _canonical_refs(
        cls, value: tuple[MetricDescriptorRefV1, ...]
    ) -> tuple[MetricDescriptorRefV1, ...]:
        identities = [
            (item.metric_name, item.descriptor_version, item.descriptor_digest) for item in value
        ]
        if not identities or len(identities) != len(set(identities)):
            raise ValueError("descriptor refs must be non-empty and unique")
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.metric_name,
                    item.descriptor_version,
                    item.descriptor_digest,
                ),
            )
        )

    @field_validator("label_matchers")
    @classmethod
    def _canonical_matchers(
        cls, value: tuple[MetricLabelMatcherV1, ...]
    ) -> tuple[MetricLabelMatcherV1, ...]:
        keys = [item.key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("each metric label may have at most one matcher")
        return tuple(sorted(value, key=lambda item: item.key))


class ScalarMetricSampleV1(_FrozenModel):
    ts_utc: datetime
    value: float

    @field_validator("ts_utc")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("value")
    @classmethod
    def _finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric sample value must be finite")
        return value


class HistogramMetricSampleV1(_FrozenModel):
    ts_utc: datetime
    count: NonNegativeInt
    sum: float | None = None
    cumulative_bucket_counts: tuple[NonNegativeInt, ...]

    @field_validator("ts_utc")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("sum")
    @classmethod
    def _finite_sum(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("histogram sum must be finite when reported")
        return value

    @model_validator(mode="after")
    def _cumulative_counts(self) -> HistogramMetricSampleV1:
        counts = self.cumulative_bucket_counts
        if not counts or any(left > right for left, right in zip(counts, counts[1:])):
            raise ValueError("histogram bucket counts must be non-empty and cumulative")
        if counts[-1] != self.count:
            raise ValueError("histogram +Inf bucket must equal count")
        return self


class MetricSeriesV1(_FrozenModel):
    descriptor: MetricDescriptorRefV1
    metric_name: NonEmptyStr
    metric_type: MetricType
    unit: MetricUnit
    labels: dict[TelemetryKey, NonEmptyStr]
    bucket_bounds: tuple[float, ...] | None = None
    scalar_points: tuple[ScalarMetricSampleV1, ...] | None = None
    histogram_points: tuple[HistogramMetricSampleV1, ...] | None = None

    @field_validator("labels")
    @classmethod
    def _canonical_labels(cls, value: dict[str, str]) -> dict[str, str]:
        return {key: value[key] for key in sorted(value)}

    @model_validator(mode="after")
    def _closed_series_union(self) -> MetricSeriesV1:
        if self.metric_name != self.descriptor.metric_name:
            raise ValueError("metric series name differs from exact descriptor ref")
        if self.metric_type == "histogram":
            if (
                not self.bucket_bounds
                or self.histogram_points is None
                or self.scalar_points is not None
            ):
                raise ValueError("histogram series requires only bucket and histogram points")
            expected_count = len(self.bucket_bounds) + 1
            if any(
                len(point.cumulative_bucket_counts) != expected_count
                for point in self.histogram_points
            ):
                raise ValueError("histogram sample bucket count differs from series bounds")
        elif (
            self.bucket_bounds is not None
            or self.scalar_points is None
            or self.histogram_points is not None
        ):
            raise ValueError("counter/gauge series requires only scalar points")
        return self


class MetricPageV1(_FrozenModel):
    page_schema_version: Literal["metric-page@1"] = "metric-page@1"
    series: tuple[MetricSeriesV1, ...] = Field(max_length=MAX_QUERY_SERIES)
    next_cursor: OpaqueCursor | None = None
    coverage_start: datetime
    coverage_end: datetime
    effective_resolution_s: PositiveInt
    truncated: bool

    @field_validator("coverage_start", "coverage_end")
    @classmethod
    def _utc_timestamps(cls, value: datetime) -> datetime:
        return _require_utc(value)


class LogErrorV1(_FrozenModel):
    error_type: NonEmptyStr
    message: Annotated[str, StringConstraints(max_length=2048)]
    stack_fingerprint: Sha256Hex | None = None


class LogRecordV1(_FrozenModel):
    log_schema_version: Literal["log-record@1"] = "log-record@1"
    log_id: NonEmptyStr
    ts_utc: datetime
    level: LogLevel
    message: Annotated[str, StringConstraints(max_length=2048)]
    service: NonEmptyStr
    event_name: NonEmptyStr
    request_id: NonEmptyStr | None = None
    run_id: NonEmptyStr | None = None
    trace_id: TraceId | None = None
    span_id: SpanId | None = None
    producer_run_id: NonEmptyStr | None = None
    error: LogErrorV1 | None = None
    fields: dict[TelemetryKey, JsonValue] = Field(default_factory=dict)

    @field_validator("ts_utc")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("fields")
    @classmethod
    def _bounded_fields(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_telemetry_fields(value, max_items=MAX_ATTRIBUTE_COUNT)

    @model_validator(mode="after")
    def _trace_pair(self) -> LogRecordV1:
        if self.span_id is not None and self.trace_id is None:
            raise ValueError("span_id requires trace_id")
        return self


class LogRecordViewV1(_FrozenModel):
    record: LogRecordV1
    redacted_fields: tuple[TelemetryKey, ...] = Field(
        default=(),
        max_length=MAX_ATTRIBUTE_COUNT,
    )

    @model_validator(mode="before")
    @classmethod
    def _wrap_internal_record(cls, value: Any) -> Any:
        if isinstance(value, LogRecordV1):
            return {"record": value, "redacted_fields": ()}
        return value

    @field_validator("redacted_fields")
    @classmethod
    def _stable_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value)


class LogQueryV1(_FrozenModel):
    query_schema_version: Literal["log-query@1"] = "log-query@1"
    time_range: TimeRangeV1
    services: tuple[NonEmptyStr, ...] = Field(
        default=(),
        max_length=MAX_QUERY_FILTER_ITEMS,
    )
    levels: tuple[LogLevel, ...] = Field(default=(), max_length=5)
    event_names: tuple[NonEmptyStr, ...] = Field(
        default=(),
        max_length=MAX_QUERY_FILTER_ITEMS,
    )
    run_id: NonEmptyStr | None = None
    trace_id: TraceId | None = None
    span_id: SpanId | None = None
    producer_run_id: NonEmptyStr | None = None
    cursor: OpaqueCursor | None = None
    limit: int = Field(gt=0, le=MAX_QUERY_PAGE_SIZE)
    authz_fingerprint: Sha256Hex

    @field_validator("services", "levels", "event_names")
    @classmethod
    def _stable_sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value)

    @model_validator(mode="after")
    def _trace_pair(self) -> LogQueryV1:
        if self.span_id is not None and self.trace_id is None:
            raise ValueError("span_id filter requires trace_id")
        return self


class LogPageV1(_FrozenModel):
    page_schema_version: Literal["log-page@1"] = "log-page@1"
    items: tuple[LogRecordViewV1, ...] = Field(max_length=MAX_QUERY_PAGE_SIZE)
    next_cursor: OpaqueCursor | None = None
    coverage_start: datetime
    coverage_end: datetime
    truncated: bool

    @field_validator("coverage_start", "coverage_end")
    @classmethod
    def _utc_timestamps(cls, value: datetime) -> datetime:
        return _require_utc(value)


class SpanExporter(Protocol):
    def export(self, spans: Sequence[SpanDataV1]) -> None: ...


class TraceQueryStore(Protocol):
    def put(self, span: SpanDataV1) -> None: ...

    def get(self, trace_id: str, span_id: str) -> SpanDataV1 | None: ...

    def query_traces(self, query: TraceQueryV1) -> TraceSummaryPageV1: ...

    def page_spans(
        self, trace_id: str, *, cursor: str | None, limit: int, authz_fingerprint: str
    ) -> SpanPageV1: ...


class LogQueryStore(Protocol):
    def append(self, record: LogRecordV1) -> None: ...

    def query_logs(self, query: LogQueryV1) -> LogPageV1: ...


class MetricQueryStore(Protocol):
    def record(self, point: MetricPointV1) -> None: ...

    def query_metrics(self, query: MetricQueryV1) -> MetricPageV1: ...


class Counter(Protocol):
    def add(self, value: float, *, labels: Mapping[str, str]) -> None: ...


class Histogram(Protocol):
    def record(self, value: float, *, labels: Mapping[str, str]) -> None: ...


class Gauge(Protocol):
    def set(self, value: float, *, labels: Mapping[str, str]) -> None: ...


class MetricSink(Protocol):
    @property
    def registry_ref(self) -> MetricDescriptorRegistryRefV1: ...

    def counter(self, descriptor: MetricDescriptorRefV1) -> Counter: ...

    def histogram(self, descriptor: MetricDescriptorRefV1) -> Histogram: ...

    def gauge(self, descriptor: MetricDescriptorRefV1) -> Gauge: ...


__all__ = [
    "Counter",
    "FORBIDDEN_METRIC_LABEL_KEYS",
    "Gauge",
    "Histogram",
    "HistogramMetricSampleV1",
    "LogPageV1",
    "LogQueryStore",
    "LogQueryV1",
    "LogRecordV1",
    "LogRecordViewV1",
    "MetricDescriptorRefV1",
    "MetricDescriptorRegistryRefV1",
    "MetricDescriptorRegistryV1",
    "MetricDescriptorV1",
    "MetricLabelMatcherV1",
    "MetricPageV1",
    "MetricPointV1",
    "MetricQueryStore",
    "MetricQueryV1",
    "MetricSeriesV1",
    "MetricSink",
    "RunCorrelationV1",
    "ScalarMetricSampleV1",
    "SpanDataV1",
    "SpanEventV1",
    "SpanExporter",
    "SpanLinkV1",
    "SpanPageV1",
    "SpanViewV1",
    "TimeRangeV1",
    "TraceContextV1",
    "TraceQueryStore",
    "TraceQueryV1",
    "TraceSummaryPageV1",
    "TraceSummaryV1",
    "compute_metric_descriptor_digest",
    "compute_metric_registry_digest",
]
