from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation, QueryTooBroad
from gameforge.contracts.observability import (
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricQueryV1,
    TimeRangeV1,
    compute_metric_descriptor_digest,
    compute_metric_registry_digest,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.observability.in_memory import (
    InMemoryTelemetryStore,
    TelemetryStoreLimits,
)
from gameforge.runtime.observability.metrics import MetricRegistrySink


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


class _FailingClock:
    def now_utc(self) -> datetime:
        raise OSError("UTC clock unavailable")


def _descriptor(
    name: str,
    metric_type: str,
    *,
    labels: tuple[str, ...] = ("outcome",),
    bounds: tuple[float, ...] = (),
    series_limit: int = 8,
) -> MetricDescriptorV1:
    payload = {
        "metric_name": name,
        "descriptor_version": 1,
        "metric_type": metric_type,
        "unit": "count" if metric_type == "counter" else "ms",
        "label_keys": labels,
        "histogram_bucket_bounds": bounds,
        "series_limit": series_limit,
    }
    return MetricDescriptorV1(
        **payload,
        descriptor_digest=compute_metric_descriptor_digest(payload),
    )


def _registry(
    *descriptors: MetricDescriptorV1,
    global_series_limit: int = 16,
) -> MetricDescriptorRegistryV1:
    payload = {
        "registry_version": 1,
        "descriptors": descriptors,
        "global_series_limit": global_series_limit,
    }
    return MetricDescriptorRegistryV1(
        **payload,
        registry_digest=compute_metric_registry_digest(payload),
    )


def test_metric_sink_emits_exact_descriptor_points_and_rejects_wrong_labels() -> None:
    descriptor = _descriptor("gameforge.run.completed", "counter")
    registry = _registry(descriptor)
    clock = FrozenUtcClock(NOW)
    store = InMemoryTelemetryStore(clock=clock, signing_key=b"test-key")
    store.register_metric_registry(registry)
    ids = iter(("point-1", "point-2"))
    sink = MetricRegistrySink(
        registry=registry, store=store, clock=clock, id_generator=ids.__next__
    )

    sink.counter(descriptor.ref).add(1, labels={"outcome": "ok"})
    assert store.metric_point_count == 1

    with pytest.raises(IntegrityViolation, match="label"):
        sink.counter(descriptor.ref).add(1, labels={"unexpected": "ok"})


def test_metric_query_aggregates_counter_and_histogram_without_ratio_averaging() -> None:
    counter = _descriptor("gameforge.finding.detected", "counter")
    histogram = _descriptor(
        "gameforge.provider.latency",
        "histogram",
        bounds=(10.0, 100.0),
    )
    registry = _registry(counter, histogram)
    clock = FrozenUtcClock(NOW)
    store = InMemoryTelemetryStore(clock=clock, signing_key=b"test-key")
    store.register_metric_registry(registry)
    ids = iter(f"point-{index}" for index in range(1, 10))
    sink = MetricRegistrySink(
        registry=registry, store=store, clock=clock, id_generator=ids.__next__
    )

    sink.counter(counter.ref).add(2, labels={"outcome": "true_positive"})
    sink.counter(counter.ref).add(3, labels={"outcome": "true_positive"})
    sink.histogram(histogram.ref).record(5, labels={"outcome": "ok"})
    sink.histogram(histogram.ref).record(50, labels={"outcome": "ok"})
    sink.histogram(histogram.ref).record(500, labels={"outcome": "ok"})

    query = MetricQueryV1(
        descriptor_refs=(counter.ref, histogram.ref),
        time_range=TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=1)),
        resolution_s=60,
        label_matchers=(),
        max_points=10,
        series_limit=10,
        authz_fingerprint="a" * 64,
    )
    page = store.query(query)
    by_name = {series.metric_name: series for series in page.series}

    assert by_name[counter.metric_name].scalar_points[0].value == 5
    histogram_sample = by_name[histogram.metric_name].histogram_points[0]
    assert histogram_sample.count == 3
    assert histogram_sample.cumulative_bucket_counts == (1, 2, 3)
    assert histogram_sample.sum == 555


def test_metric_point_id_is_idempotent_but_conflicting_payload_fails() -> None:
    descriptor = _descriptor("gameforge.run.completed", "counter")
    registry = _registry(descriptor)
    clock = FrozenUtcClock(NOW)
    store = InMemoryTelemetryStore(clock=clock, signing_key=b"test-key")
    store.register_metric_registry(registry)
    ids = iter(("same-id", "same-id", "same-id"))
    sink = MetricRegistrySink(
        registry=registry, store=store, clock=clock, id_generator=ids.__next__
    )
    handle = sink.counter(descriptor.ref)

    handle.add(1, labels={"outcome": "ok"})
    handle.add(1, labels={"outcome": "ok"})
    assert store.metric_point_count == 1

    with pytest.raises(IntegrityViolation):
        handle.add(2, labels={"outcome": "ok"})


def test_registry_digest_is_exposed_as_readiness_binding() -> None:
    descriptor = _descriptor("gameforge.run.completed", "counter")
    registry = _registry(descriptor)
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    store.register_metric_registry(registry)

    assert store.metric_registry_ref == registry.ref
    assert canonical_sha256(registry.model_dump(mode="json", exclude={"registry_digest"}))


def test_metric_store_failure_is_best_effort_and_counted_without_recursion() -> None:
    descriptor = _descriptor("gameforge.run.completed", "counter")
    registry = _registry(descriptor)

    class _BrokenStore:
        def record(self, point) -> None:
            raise OSError("disk full")

    sink = MetricRegistrySink(
        registry=registry,
        store=_BrokenStore(),
        clock=FrozenUtcClock(NOW),
        id_generator=lambda: "point-1",
    )

    assert sink.counter(descriptor.ref).add(1, labels={"outcome": "ok"}) is None
    assert sink.dropped_count == 1


@pytest.mark.parametrize(
    ("clock", "id_generator"),
    [
        pytest.param(
            FrozenUtcClock(NOW),
            lambda: (_ for _ in ()).throw(OSError("id source unavailable")),
            id="id-generator",
        ),
        pytest.param(
            FrozenUtcClock(NOW),
            lambda: "",
            id="invalid-point-dto",
        ),
        pytest.param(
            _FailingClock(),
            lambda: "point-1",
            id="utc-clock",
        ),
    ],
)
def test_metric_point_construction_failures_are_best_effort(
    clock: Any,
    id_generator: Callable[[], str],
) -> None:
    descriptor = _descriptor("gameforge.run.completed", "counter")
    registry = _registry(descriptor)
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    store.register_metric_registry(registry)
    sink = MetricRegistrySink(
        registry=registry,
        store=store,
        clock=clock,
        id_generator=id_generator,
    )

    assert sink.counter(descriptor.ref).add(1, labels={"outcome": "ok"}) is None
    assert store.metric_point_count == 0
    assert sink.dropped_count == 1


def test_metric_store_record_and_descriptor_series_capacity_are_best_effort() -> None:
    descriptor = _descriptor(
        "gameforge.run.completed",
        "counter",
        series_limit=1,
    )
    registry = _registry(descriptor)
    store = InMemoryTelemetryStore(
        clock=FrozenUtcClock(NOW),
        signing_key=b"test-key",
        limits=TelemetryStoreLimits(max_stored_metric_points=1),
    )
    store.register_metric_registry(registry)
    ids = iter(("point-1", "point-2", "point-3"))
    sink = MetricRegistrySink(
        registry=registry,
        store=store,
        clock=FrozenUtcClock(NOW),
        id_generator=ids.__next__,
    )

    handle = sink.counter(descriptor.ref)
    handle.add(1, labels={"outcome": "ok"})
    handle.add(1, labels={"outcome": "ok"})
    handle.add(1, labels={"outcome": "different"})

    assert store.metric_point_count == 1
    assert sink.dropped_count == 2


def test_metric_registry_global_series_capacity_is_best_effort() -> None:
    first = _descriptor("gameforge.first", "counter", series_limit=1)
    second = _descriptor("gameforge.second", "counter", series_limit=1)
    registry = _registry(first, second, global_series_limit=1)
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    store.register_metric_registry(registry)
    ids = iter(("point-1", "point-2"))
    sink = MetricRegistrySink(
        registry=registry,
        store=store,
        clock=FrozenUtcClock(NOW),
        id_generator=ids.__next__,
    )

    sink.counter(first.ref).add(1, labels={"outcome": "ok"})
    sink.counter(second.ref).add(1, labels={"outcome": "ok"})

    assert store.metric_point_count == 1
    assert sink.dropped_count == 1


def test_non_finite_aggregate_is_explicit_not_a_raw_validation_failure() -> None:
    counter = _descriptor("gameforge.large.counter", "counter")
    histogram = _descriptor(
        "gameforge.large.histogram",
        "histogram",
        bounds=(1.0,),
    )
    registry = _registry(counter, histogram)
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    store.register_metric_registry(registry)
    ids = iter(f"large-{index}" for index in range(4))
    sink = MetricRegistrySink(
        registry=registry,
        store=store,
        clock=FrozenUtcClock(NOW),
        id_generator=ids.__next__,
    )
    for _ in range(2):
        sink.counter(counter.ref).add(1e308, labels={"outcome": "ok"})
        sink.histogram(histogram.ref).record(1e308, labels={"outcome": "ok"})

    histogram_query = MetricQueryV1(
        descriptor_refs=(histogram.ref,),
        time_range=TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=1)),
        resolution_s=60,
        label_matchers=(),
        max_points=10,
        series_limit=10,
        authz_fingerprint="a" * 64,
    )
    histogram_page = store.query_metrics(histogram_query)
    assert histogram_page.series[0].histogram_points[0].sum is None

    with pytest.raises(QueryTooBroad, match="counter aggregate"):
        store.query_metrics(histogram_query.model_copy(update={"descriptor_refs": (counter.ref,)}))
