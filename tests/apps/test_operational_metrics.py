from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gameforge.apps.operational_metrics import (
    API_REQUEST_COUNT,
    BUILTIN_OPERATIONAL_METRIC_REGISTRY,
    BUILTIN_OPERATIONAL_METRIC_REGISTRY_DIGEST,
    WORKER_ATTEMPT_COUNT,
    install_builtin_operational_metrics,
)
from gameforge.contracts.observability import (
    FORBIDDEN_METRIC_LABEL_KEYS,
    MetricQueryV1,
    TimeRangeV1,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.observability.in_memory import InMemoryTelemetryStore


NOW = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)


def test_builtin_operational_registry_has_a_frozen_exact_low_cardinality_shape() -> None:
    registry = BUILTIN_OPERATIONAL_METRIC_REGISTRY

    assert registry.registry_version == 1
    assert registry.registry_digest == BUILTIN_OPERATIONAL_METRIC_REGISTRY_DIGEST
    assert registry.registry_digest == (
        "e73fa4e59df4d75e9582e999d9f8c4ba079a2920a79e4cc5b6a2eb0fa3bc1a64"
    )
    assert registry.descriptors == (API_REQUEST_COUNT, WORKER_ATTEMPT_COUNT)
    assert API_REQUEST_COUNT.label_keys == ("method", "status_class")
    assert WORKER_ATTEMPT_COUNT.label_keys == ("phase", "run_kind")
    assert all(
        FORBIDDEN_METRIC_LABEL_KEYS.isdisjoint(descriptor.label_keys)
        for descriptor in registry.descriptors
    )


def test_install_is_idempotent_and_emits_queryable_normalized_points() -> None:
    clock = FrozenUtcClock(NOW)
    store = InMemoryTelemetryStore(clock=clock, signing_key=b"operational-metrics-test")

    first = install_builtin_operational_metrics(store=store, clock=clock)
    second = install_builtin_operational_metrics(store=store, clock=clock)

    assert first.registry_ref == second.registry_ref == BUILTIN_OPERATIONAL_METRIC_REGISTRY.ref
    first.record_api_request(method="GET", status_code=200)
    first.record_api_request(method="custom-unbounded-method", status_code=599)
    second.record_worker_attempt(run_kind="checker.run", phase="started")
    second.record_worker_attempt(run_kind="checker.run", phase="terminal_published")
    second.record_worker_attempt(run_kind="run:unbounded-user-value", phase="unexpected")

    page = store.query_metrics(
        MetricQueryV1(
            descriptor_refs=(API_REQUEST_COUNT.ref, WORKER_ATTEMPT_COUNT.ref),
            time_range=TimeRangeV1(
                start_utc=NOW,
                end_utc=NOW + timedelta(minutes=1),
            ),
            resolution_s=60,
            label_matchers=(),
            max_points=100,
            series_limit=100,
            authz_fingerprint="a" * 64,
        )
    )

    labels = {series.metric_name: [] for series in page.series}
    for series in page.series:
        labels[series.metric_name].append(series.labels)
    assert labels[API_REQUEST_COUNT.metric_name] == [
        {"method": "GET", "status_class": "2xx"},
        {"method": "OTHER", "status_class": "5xx"},
    ]
    assert labels[WORKER_ATTEMPT_COUNT.metric_name] == [
        {"phase": "other", "run_kind": "other"},
        {"phase": "started", "run_kind": "checker.run"},
        {"phase": "terminal_published", "run_kind": "checker.run"},
    ]


def test_operational_metric_export_failure_never_changes_the_business_result() -> None:
    clock = FrozenUtcClock(NOW)

    class BrokenStore:
        def __init__(self) -> None:
            self.registry = None

        def register_metric_registry(self, registry) -> None:
            self.registry = registry

        @property
        def metric_registry_ref(self):
            return self.registry.ref

        def record(self, point) -> None:
            del point
            raise OSError("telemetry disk unavailable")

    metrics = install_builtin_operational_metrics(store=BrokenStore(), clock=clock)

    assert metrics.record_api_request(method="POST", status_code=503) is None
    assert (
        metrics.record_worker_attempt(run_kind="simulation.run", phase="terminal_published") is None
    )
    assert metrics.dropped_count == 2
