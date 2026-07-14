from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from gameforge.contracts.observability import (
    HistogramMetricSampleV1,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricLabelMatcherV1,
    MetricPointV1,
    MetricQueryV1,
    MetricSeriesV1,
    RunCorrelationV1,
    SpanDataV1,
    TimeRangeV1,
    TraceContextV1,
    compute_metric_descriptor_digest,
    compute_metric_registry_digest,
)


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def _descriptor(
    *,
    name: str = "gameforge.run.completed",
    metric_type: str = "counter",
    label_keys: tuple[str, ...] = ("outcome",),
    bounds: tuple[float, ...] = (),
) -> MetricDescriptorV1:
    payload = {
        "descriptor_schema_version": "metric-descriptor@1",
        "metric_name": name,
        "descriptor_version": 1,
        "metric_type": metric_type,
        "unit_schema_version": "metric-units@1",
        "unit": "count",
        "label_keys": tuple(sorted(label_keys)),
        "histogram_bucket_bounds": bounds,
        "series_limit": 16,
    }
    return MetricDescriptorV1(
        **payload,
        descriptor_digest=compute_metric_descriptor_digest(payload),
    )


def test_run_correlation_and_trace_context_are_distinct_strict_contracts() -> None:
    correlation = RunCorrelationV1(run_id="run-1", attempt_no=2)
    trace = TraceContextV1(
        trace_id="1" * 32,
        span_id="2" * 16,
        trace_flags="01",
        trace_state="vendor=value",
    )

    assert correlation.model_dump(mode="json") == {
        "correlation_schema_version": "run-correlation@1",
        "run_id": "run-1",
        "attempt_no": 2,
    }
    assert "run_id" not in trace.model_dump(mode="json")

    with pytest.raises(ValidationError):
        TraceContextV1(
            trace_id="0" * 32,
            span_id="2" * 16,
            trace_flags="01",
        )


def test_span_keeps_utc_timestamps_and_monotonic_duration_separate() -> None:
    span = SpanDataV1(
        trace_id="1" * 32,
        span_id="2" * 16,
        parent_span_id=None,
        name="checker",
        attributes={"run_id": "run-1", "replay": True},
        links=(),
        events=(),
        status="ok",
        error=None,
        resource={"service.name": "gameforge-test"},
        started_at=NOW,
        ended_at=NOW - timedelta(seconds=2),
        duration_ns=17,
    )

    assert span.ended_at < span.started_at
    assert span.duration_ns == 17


@pytest.mark.parametrize("label", ["run_id", "span_id", "artifact_id", "principal_id"])
def test_metric_descriptors_reject_forbidden_high_cardinality_labels(label: str) -> None:
    with pytest.raises(ValidationError, match="high-cardinality"):
        _descriptor(label_keys=(label,))


def test_metric_descriptor_and_registry_are_canonical_and_digest_bound() -> None:
    left = _descriptor(label_keys=("outcome", "domain"))
    right = _descriptor(
        name="gameforge.run.duration",
        metric_type="histogram",
        label_keys=("outcome",),
        bounds=(1.0, 10.0),
    )
    registry_payload = {
        "registry_schema_version": "metric-descriptor-registry@1",
        "registry_version": 3,
        "descriptors": (right, left),
        "global_series_limit": 32,
    }
    registry = MetricDescriptorRegistryV1(
        **registry_payload,
        registry_digest=compute_metric_registry_digest(registry_payload),
    )

    assert [item.metric_name for item in registry.descriptors] == sorted(
        [left.metric_name, right.metric_name]
    )

    with pytest.raises(ValidationError, match="registry_digest"):
        registry.model_copy(update={"registry_digest": "f" * 64}).__class__.model_validate(
            registry.model_copy(update={"registry_digest": "f" * 64}).model_dump()
        )


def test_metric_point_and_query_validate_exact_shapes() -> None:
    descriptor = _descriptor()
    ref = MetricDescriptorRefV1(
        metric_name=descriptor.metric_name,
        descriptor_version=descriptor.descriptor_version,
        descriptor_digest=descriptor.descriptor_digest,
    )
    point = MetricPointV1(
        point_id="point-1",
        descriptor=ref,
        metric_type="counter",
        ts_utc=NOW,
        value=1.0,
        labels={"outcome": "ok"},
    )
    query = MetricQueryV1(
        descriptor_refs=(ref,),
        time_range=TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=5)),
        resolution_s=60,
        label_matchers=(MetricLabelMatcherV1(key="outcome", operation="eq", values=("ok",)),),
        max_points=100,
        series_limit=10,
        authz_fingerprint="a" * 64,
    )

    assert point.value == 1.0
    assert query.time_range.start_utc == NOW

    with pytest.raises(ValidationError, match="non-negative"):
        MetricPointV1(
            point_id="point-2",
            descriptor=ref,
            metric_type="counter",
            ts_utc=NOW,
            value=-1,
            labels={"outcome": "failed"},
        )

    with pytest.raises(ValidationError, match="exactly one"):
        MetricLabelMatcherV1(key="outcome", operation="eq", values=("ok", "failed"))


def test_histogram_series_requires_cumulative_plus_infinity_bucket() -> None:
    descriptor = _descriptor(
        metric_type="histogram",
        label_keys=("outcome",),
        bounds=(1.0, 10.0),
    )
    ref = MetricDescriptorRefV1(
        metric_name=descriptor.metric_name,
        descriptor_version=descriptor.descriptor_version,
        descriptor_digest=descriptor.descriptor_digest,
    )
    sample = HistogramMetricSampleV1(
        ts_utc=NOW,
        count=3,
        sum=12.0,
        cumulative_bucket_counts=(1, 2, 3),
    )
    series = MetricSeriesV1(
        descriptor=ref,
        metric_name=descriptor.metric_name,
        metric_type="histogram",
        unit=descriptor.unit,
        labels={"outcome": "ok"},
        bucket_bounds=descriptor.histogram_bucket_bounds,
        histogram_points=(sample,),
    )
    assert series.histogram_points == (sample,)

    with pytest.raises(ValidationError, match="bucket"):
        MetricSeriesV1(
            descriptor=ref,
            metric_name=descriptor.metric_name,
            metric_type="histogram",
            unit=descriptor.unit,
            labels={"outcome": "ok"},
            bucket_bounds=descriptor.histogram_bucket_bounds,
            histogram_points=(sample.model_copy(update={"cumulative_bucket_counts": (1, 3)}),),
        )
