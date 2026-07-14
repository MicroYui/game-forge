from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from gameforge.contracts.observability import (
    HistogramMetricSampleV1,
    LogPageV1,
    LogQueryV1,
    LogRecordV1,
    LogRecordViewV1,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricLabelMatcherV1,
    MetricPageV1,
    MetricPointV1,
    MetricQueryV1,
    MetricSeriesV1,
    RunCorrelationV1,
    SpanDataV1,
    SpanPageV1,
    TimeRangeV1,
    TraceContextV1,
    TraceQueryV1,
    TraceSummaryPageV1,
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


def test_trace_query_freezes_time_page_and_string_bounds() -> None:
    base = {
        "time_range": TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(days=7)),
        "limit": 1000,
        "authz_fingerprint": "a" * 64,
    }
    assert TraceQueryV1(**base).limit == 1000
    assert TraceQueryV1.model_json_schema()["properties"]["limit"]["maximum"] == 1000

    with pytest.raises(ValidationError):
        TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(days=7, seconds=1))
    with pytest.raises(ValidationError):
        TraceQueryV1(**{**base, "limit": 1001})
    with pytest.raises(ValidationError):
        TraceQueryV1(**{**base, "service": "s" * 513})


def test_metric_query_freezes_all_collection_and_count_bounds() -> None:
    ref = _descriptor().ref
    base = {
        "descriptor_refs": (ref,),
        "time_range": TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=5)),
        "resolution_s": 60,
        "label_matchers": (),
        "max_points": 100,
        "series_limit": 10,
        "authz_fingerprint": "a" * 64,
    }

    schema = MetricQueryV1.model_json_schema()["properties"]
    assert schema["descriptor_refs"]["maxItems"] == 64
    assert schema["label_matchers"]["maxItems"] == 64
    assert MetricLabelMatcherV1.model_json_schema()["properties"]["values"]["maxItems"] == 64
    with pytest.raises(ValidationError):
        MetricQueryV1(
            **{
                **base,
                "descriptor_refs": tuple(
                    MetricDescriptorRefV1(
                        metric_name=f"metric.{index}",
                        descriptor_version=1,
                        descriptor_digest=f"{index:064x}",
                    )
                    for index in range(65)
                ),
            }
        )
    with pytest.raises(ValidationError):
        MetricQueryV1(
            **{
                **base,
                "label_matchers": tuple(
                    MetricLabelMatcherV1(key=f"key{index}", operation="eq", values=("value",))
                    for index in range(65)
                ),
            }
        )
    with pytest.raises(ValidationError):
        MetricLabelMatcherV1(
            key="outcome",
            operation="in",
            values=tuple(f"value-{index}" for index in range(65)),
        )
    for field, value in (
        ("resolution_s", 86_401),
        ("max_points", 10_001),
        ("series_limit", 501),
    ):
        with pytest.raises(ValidationError):
            MetricQueryV1(**{**base, field: value})


def test_log_query_freezes_filter_and_page_bounds() -> None:
    base = {
        "time_range": TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=5)),
        "limit": 1000,
        "authz_fingerprint": "a" * 64,
    }
    assert LogQueryV1(**base).limit == 1000
    schema = LogQueryV1.model_json_schema()["properties"]
    assert schema["services"]["maxItems"] == 64
    assert schema["levels"]["maxItems"] == 5
    assert schema["event_names"]["maxItems"] == 64
    assert schema["span_id"]["anyOf"][0]["pattern"] == "^[0-9a-f]{16}$"
    assert schema["producer_run_id"]["anyOf"][0]["maxLength"] == 512

    correlated = LogQueryV1(
        **base,
        trace_id="1" * 32,
        span_id="2" * 16,
        producer_run_id="producer-run-1",
    )
    assert correlated.span_id == "2" * 16
    assert correlated.producer_run_id == "producer-run-1"

    with pytest.raises(ValidationError):
        LogQueryV1(**{**base, "services": tuple(f"service-{index}" for index in range(65))})
    with pytest.raises(ValidationError):
        LogQueryV1(**{**base, "event_names": tuple(f"event-{index}" for index in range(65))})
    with pytest.raises(ValidationError):
        LogQueryV1(**{**base, "levels": ("debug", "info", "warning", "error", "critical", "info")})
    with pytest.raises(ValidationError):
        LogQueryV1(**{**base, "limit": 1001})
    with pytest.raises(ValidationError):
        LogQueryV1(**{**base, "span_id": "2" * 16})
    with pytest.raises(ValidationError):
        LogQueryV1(**{**base, "producer_run_id": "p" * 513})


def test_log_page_uses_an_explicit_redaction_view() -> None:
    record = LogRecordV1(
        log_id="log-1",
        ts_utc=NOW,
        level="info",
        message="completed",
        service="api",
        event_name="run.completed",
        fields={"redacted_fields": ["authorization"]},
    )
    explicit = LogRecordViewV1(
        record=record,
        redacted_fields=("authorization",),
    )
    page = LogPageV1(
        items=(explicit,),
        coverage_start=NOW,
        coverage_end=NOW + timedelta(minutes=1),
        truncated=False,
    )

    assert page.items[0].redacted_fields == ("authorization",)
    assert page.items[0].record.fields["redacted_fields"] == ["authorization"]

    compatibility_page = LogPageV1(
        items=(record,),
        coverage_start=NOW,
        coverage_end=NOW + timedelta(minutes=1),
        truncated=False,
    )
    assert compatibility_page.items[0].record == record
    assert compatibility_page.items[0].redacted_fields == ()
    with pytest.raises(ValidationError):
        LogRecordViewV1(
            record=record,
            redacted_fields=("authorization", "authorization"),
        )


def test_observability_pages_publish_exact_collection_bounds() -> None:
    assert TraceSummaryPageV1.model_json_schema()["properties"]["items"]["maxItems"] == 1000
    assert SpanPageV1.model_json_schema()["properties"]["items"]["maxItems"] == 1000
    assert MetricPageV1.model_json_schema()["properties"]["series"]["maxItems"] == 500
    log_items = LogPageV1.model_json_schema()["properties"]["items"]
    assert log_items["maxItems"] == 1000
    assert log_items["items"]["$ref"].endswith("/$defs/LogRecordViewV1")


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
