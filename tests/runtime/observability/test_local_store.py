from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from gameforge.contracts.errors import (
    CursorExpired,
    CursorInvalid,
    IntegrityViolation,
    QueryTooBroad,
)
from gameforge.contracts.observability import (
    LogQueryV1,
    LogRecordV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricPointV1,
    MetricQueryV1,
    SpanDataV1,
    SpanEventV1,
    TimeRangeV1,
    TraceQueryV1,
    compute_metric_descriptor_digest,
    compute_metric_registry_digest,
)
from gameforge.runtime.observability.local_store import (
    LocalTelemetryLimits,
    LocalTelemetryRetention,
    LocalTelemetryStore,
)
from gameforge.runtime.observability.metrics import MetricRegistrySink


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
SIGNING_KEY = b"local-telemetry-store-test-key"


@dataclass
class _Clock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _descriptor(
    name: str = "gameforge.run.completed",
    *,
    version: int = 1,
    metric_type: str = "counter",
    bounds: tuple[float, ...] = (),
    series_limit: int = 8,
) -> MetricDescriptorV1:
    payload = {
        "metric_name": name,
        "descriptor_version": version,
        "metric_type": metric_type,
        "unit": "ms" if metric_type in {"gauge", "histogram"} else "count",
        "label_keys": ("outcome",),
        "histogram_bucket_bounds": bounds,
        "series_limit": series_limit,
    }
    return MetricDescriptorV1(
        **payload,
        descriptor_digest=compute_metric_descriptor_digest(payload),
    )


def _registry(
    *descriptors: MetricDescriptorV1,
    version: int = 1,
) -> MetricDescriptorRegistryV1:
    payload = {
        "registry_version": version,
        "descriptors": descriptors,
        "global_series_limit": 32,
    }
    return MetricDescriptorRegistryV1(
        **payload,
        registry_digest=compute_metric_registry_digest(payload),
    )


def _span(
    index: int,
    *,
    trace_id: str | None = None,
    started_at: datetime | None = None,
) -> SpanDataV1:
    started = started_at or NOW + timedelta(seconds=index)
    return SpanDataV1(
        trace_id=trace_id or f"{index:032x}",
        span_id=f"{index:016x}",
        parent_span_id=None,
        name="checker",
        attributes={"run_id": f"run-{index}"},
        links=(),
        events=(),
        status="ok",
        error=None,
        resource={"service.name": "worker"},
        started_at=started,
        ended_at=started + timedelta(seconds=1),
        duration_ns=1_000_000_000,
    )


def _log(index: int, *, ts_utc: datetime | None = None) -> LogRecordV1:
    return LogRecordV1(
        log_id=f"log-{index}",
        ts_utc=ts_utc or NOW + timedelta(seconds=index),
        level="info",
        message="completed",
        service="api",
        event_name="run.completed",
        run_id=f"run-{index}",
    )


def _point(
    descriptor: MetricDescriptorV1,
    point_id: str,
    *,
    value: float = 1,
    outcome: str = "ok",
    ts_utc: datetime = NOW,
) -> MetricPointV1:
    return MetricPointV1(
        point_id=point_id,
        descriptor=descriptor.ref,
        metric_type=descriptor.metric_type,
        ts_utc=ts_utc,
        value=value,
        labels={"outcome": outcome},
    )


def _trace_query(*, limit: int = 10, authz: str = "a" * 64) -> TraceQueryV1:
    return TraceQueryV1(
        time_range=TimeRangeV1(
            start_utc=NOW - timedelta(days=1),
            end_utc=NOW + timedelta(days=1),
        ),
        limit=limit,
        authz_fingerprint=authz,
    )


def _log_query(*, limit: int = 10, authz: str = "b" * 64) -> LogQueryV1:
    return LogQueryV1(
        time_range=TimeRangeV1(
            start_utc=NOW - timedelta(days=1),
            end_utc=NOW + timedelta(days=1),
        ),
        limit=limit,
        authz_fingerprint=authz,
    )


def _metric_query(
    descriptor: MetricDescriptorV1,
    *,
    series_limit: int = 10,
    authz: str = "c" * 64,
) -> MetricQueryV1:
    return MetricQueryV1(
        descriptor_refs=(descriptor.ref,),
        time_range=TimeRangeV1(
            start_utc=NOW - timedelta(days=1),
            end_utc=NOW + timedelta(days=1),
        ),
        resolution_s=60,
        label_matchers=(),
        max_points=100,
        series_limit=series_limit,
        authz_fingerprint=authz,
    )


def _store(
    path: Path,
    clock: _Clock,
    *,
    limits: LocalTelemetryLimits | None = None,
    retention: LocalTelemetryRetention | None = None,
) -> LocalTelemetryStore:
    return LocalTelemetryStore(
        path,
        clock=clock,
        signing_key=SIGNING_KEY,
        limits=limits,
        retention=retention,
    )


def test_wal_store_is_restart_readable_and_returns_exact_dtos(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.sqlite3"
    clock = _Clock(NOW)
    descriptor = _descriptor()
    registry = _registry(descriptor)
    span = _span(1)
    log = _log(1)
    point = _point(descriptor, "point-1", value=3)

    first = _store(path, clock)
    assert first.journal_mode == "wal"
    first.register_metric_registry(registry)
    first.put(span)
    first.append(log)
    first.record(point)
    concurrent_reader = _store(path, clock)
    assert concurrent_reader.get(span.trace_id, span.span_id) == span
    assert concurrent_reader.metric_point_count == 1
    concurrent_reader.close()
    first.close()

    reopened = _store(path, clock)
    assert reopened.metric_registry_ref == registry.ref
    assert reopened.get(span.trace_id, span.span_id) == span
    trace_page = reopened.query_traces(_trace_query())
    span_page = reopened.page_spans(
        span.trace_id,
        cursor=None,
        limit=10,
        authz_fingerprint="a" * 64,
    )
    log_page = reopened.query_logs(_log_query())
    metric_page = reopened.query_metrics(_metric_query(descriptor))

    assert trace_page.items[0].trace_id == span.trace_id
    assert trace_page.items[0].run_ids == ("run-1",)
    assert span_page.items[0].span == span
    assert log_page.items == (log,)
    assert metric_page.series[0].descriptor == descriptor.ref
    assert metric_page.series[0].scalar_points[0].value == 3


def test_local_store_defensively_sanitizes_spans_on_write_and_read(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.sqlite3"
    store = _store(path, _Clock(NOW))
    source = _span(1).model_copy(
        update={
            "name": "Authorization: Bearer span-secret",
            "attributes": {
                "run_id": "run-1",
                "authorization": "attribute-secret",
                "detail": "Authorization: Bearer value-secret",
            },
            "events": (
                SpanEventV1(
                    name="Authorization: Bearer event-secret",
                    occurred_at=NOW,
                    attributes={"api_key": "event-attribute-secret"},
                ),
            ),
        }
    )

    store.put(source)
    with sqlite3.connect(path) as connection:
        persisted = connection.execute("SELECT payload FROM spans").fetchone()[0]
    for secret in (
        "span-secret",
        "attribute-secret",
        "value-secret",
        "event-secret",
        "event-attribute-secret",
    ):
        assert secret not in persisted

    # A valid legacy/tampered payload must still be sanitized at the read boundary.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE spans SET payload = ? WHERE trace_id = ? AND span_id = ?",
            (source.model_dump_json(), source.trace_id, source.span_id),
        )
    observed = store.get(source.trace_id, source.span_id)
    assert observed is not None
    assert "span-secret" not in observed.model_dump_json()
    page = store.page_spans(
        source.trace_id,
        cursor=None,
        limit=10,
        authz_fingerprint="a" * 64,
    )
    assert "event-secret" not in page.model_dump_json()


def test_read_snapshot_survives_reopen_and_excludes_later_sorted_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telemetry.sqlite3"
    clock = _Clock(NOW)
    first_store = _store(path, clock)
    first_store.append(_log(1))
    first_store.append(_log(2))
    query = _log_query(limit=1)
    first_page = first_store.query_logs(query)
    assert [item.log_id for item in first_page.items] == ["log-1"]
    assert first_page.next_cursor is not None

    # This committed row sorts before the first page but is beyond its high watermark.
    first_store.append(_log(0, ts_utc=NOW - timedelta(hours=1)))
    reopened = _store(path, clock)
    second_page = reopened.query_logs(query.model_copy(update={"cursor": first_page.next_cursor}))
    assert [item.log_id for item in second_page.items] == ["log-2"]
    assert second_page.next_cursor is None

    with pytest.raises(CursorInvalid):
        reopened.query_logs(
            query.model_copy(
                update={
                    "cursor": first_page.next_cursor,
                    "authz_fingerprint": "d" * 64,
                }
            )
        )
    different_retention = _store(
        path,
        clock,
        retention=LocalTelemetryRetention(logs=timedelta(days=8)),
    )
    with pytest.raises(CursorInvalid):
        different_retention.query_logs(query.model_copy(update={"cursor": first_page.next_cursor}))
    with pytest.raises(CursorInvalid):
        reopened.query_logs(
            query.model_copy(
                update={
                    "cursor": first_page.next_cursor,
                    "services": ("another-service",),
                }
            )
        )


def test_cursor_tamper_and_expiry_fail_closed(tmp_path: Path) -> None:
    clock = _Clock(NOW)
    retention = LocalTelemetryRetention(read_snapshot_ttl=timedelta(minutes=1))
    store = _store(tmp_path / "telemetry.sqlite3", clock, retention=retention)
    store.append(_log(1))
    store.append(_log(2))
    query = _log_query(limit=1)
    cursor = store.query_logs(query).next_cursor
    assert cursor is not None

    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    with pytest.raises(CursorInvalid):
        store.query_logs(query.model_copy(update={"cursor": tampered}))

    clock.advance(timedelta(minutes=2))
    with pytest.raises(CursorExpired):
        store.query_logs(query.model_copy(update={"cursor": cursor}))


def test_exact_identities_are_idempotent_and_conflicts_are_rejected(
    tmp_path: Path,
) -> None:
    clock = _Clock(NOW)
    store = _store(tmp_path / "telemetry.sqlite3", clock)
    descriptor = _descriptor()
    store.register_metric_registry(_registry(descriptor))
    point = _point(descriptor, "point-1")

    store.record(point)
    store.record(point)
    assert store.metric_point_count == 1

    with pytest.raises(IntegrityViolation, match="point_id"):
        store.record(point.model_copy(update={"value": 2}))
    with pytest.raises(IntegrityViolation, match="descriptor"):
        store.record(_point(_descriptor(version=2), "point-2"))

    span = _span(1)
    store.put(span)
    store.put(span)
    with pytest.raises(IntegrityViolation, match="span"):
        store.put(span.model_copy(update={"name": "changed"}))

    log = _log(1)
    store.append(log)
    store.append(log)
    with pytest.raises(IntegrityViolation, match="log_id"):
        store.append(log.model_copy(update={"message": "changed"}))


def test_local_metric_series_capacity_is_best_effort_through_metric_sink(
    tmp_path: Path,
) -> None:
    clock = _Clock(NOW)
    descriptor = _descriptor(series_limit=1)
    registry = _registry(descriptor)
    store = _store(tmp_path / "telemetry.sqlite3", clock)
    store.register_metric_registry(registry)
    point_ids = iter(("point-1", "point-2"))
    sink = MetricRegistrySink(
        registry=registry,
        store=store,
        clock=clock,
        id_generator=point_ids.__next__,
    )

    counter = sink.counter(descriptor.ref)
    counter.add(1, labels={"outcome": "first"})
    counter.add(1, labels={"outcome": "overflow"})

    assert store.metric_point_count == 1
    assert sink.dropped_count == 1


def test_server_side_query_caps_reject_before_returning_partial_data(
    tmp_path: Path,
) -> None:
    clock = _Clock(NOW)
    limits = LocalTelemetryLimits(
        max_time_range=timedelta(hours=1),
        max_page_size=1,
        max_series=1,
        max_points=1,
        max_points_per_series=1,
        max_span_count=1,
    )
    store = _store(tmp_path / "telemetry.sqlite3", clock, limits=limits)
    same_trace = "f" * 32
    store.put(_span(1, trace_id=same_trace))
    store.put(_span(2, trace_id=same_trace))

    with pytest.raises(QueryTooBroad, match="time range"):
        store.query_traces(_trace_query(limit=1))
    with pytest.raises(QueryTooBroad, match="page"):
        store.query_traces(
            TraceQueryV1(
                time_range=TimeRangeV1(
                    start_utc=NOW,
                    end_utc=NOW + timedelta(minutes=1),
                ),
                limit=2,
                authz_fingerprint="a" * 64,
            )
        )
    with pytest.raises(QueryTooBroad, match="span"):
        store.page_spans(
            same_trace,
            cursor=None,
            limit=1,
            authz_fingerprint="a" * 64,
        )

    tiny_response_store = _store(
        tmp_path / "tiny.sqlite3",
        clock,
        limits=LocalTelemetryLimits(max_response_bytes=64),
    )
    tiny_response_store.append(_log(1))
    with pytest.raises(QueryTooBroad, match="response"):
        tiny_response_store.query_logs(_log_query())

    metric_store = _store(
        tmp_path / "metric-caps.sqlite3",
        clock,
        limits=LocalTelemetryLimits(
            max_time_range=timedelta(hours=1),
            max_series=1,
            max_points=1,
            max_points_per_series=1,
        ),
    )
    descriptor = _descriptor()
    metric_store.register_metric_registry(_registry(descriptor))
    metric_store.record(_point(descriptor, "point-1", ts_utc=NOW))
    metric_store.record(_point(descriptor, "point-2", ts_utc=NOW + timedelta(minutes=1)))
    metric_query = MetricQueryV1(
        descriptor_refs=(descriptor.ref,),
        time_range=TimeRangeV1(
            start_utc=NOW - timedelta(minutes=1),
            end_utc=NOW + timedelta(minutes=2),
        ),
        resolution_s=60,
        label_matchers=(),
        max_points=1,
        series_limit=1,
        authz_fingerprint="c" * 64,
    )
    with pytest.raises(QueryTooBroad, match="series"):
        metric_store.query(metric_query.model_copy(update={"series_limit": 2}))
    with pytest.raises(QueryTooBroad, match="max_points"):
        metric_store.query(metric_query.model_copy(update={"max_points": 2}))
    with pytest.raises(QueryTooBroad, match="series.*point"):
        metric_store.query(metric_query)


@pytest.mark.parametrize("owner_kind", ["slo", "alert", "saved_query"])
def test_retention_preserves_points_and_descriptors_for_live_authority(
    tmp_path: Path,
    owner_kind: str,
) -> None:
    path = tmp_path / f"telemetry-{owner_kind}.sqlite3"
    clock = _Clock(NOW)
    retention = LocalTelemetryRetention(
        spans=timedelta(hours=1),
        logs=timedelta(hours=1),
        metric_points=timedelta(hours=1),
        metric_descriptors=timedelta(hours=1),
        read_snapshot_ttl=timedelta(hours=3),
    )
    store = _store(path, clock, retention=retention)
    historical = _descriptor(version=1)
    current = _descriptor(version=2)
    store.register_metric_registry(_registry(historical, version=1))
    old_time = NOW - timedelta(hours=2)
    store.put(_span(1, started_at=old_time))
    current_span = _span(2, started_at=NOW)
    store.put(current_span)
    store.append(_log(1, ts_utc=old_time))
    current_log = _log(2, ts_utc=NOW)
    store.append(current_log)
    store.record(_point(historical, "point-a", outcome="a", ts_utc=old_time))
    store.record(_point(historical, "point-b", outcome="b", ts_utc=old_time))
    store.retain_metric_descriptors(
        owner_kind=owner_kind,
        owner_id=f"{owner_kind}:1",
        descriptor_refs=(historical.ref,),
    )
    store.register_metric_registry(_registry(current, version=2))

    query = _metric_query(historical, series_limit=1)
    first = store.query(query)
    assert first.next_cursor is not None
    with pytest.raises(CursorInvalid):
        store.query_metrics(
            query.model_copy(
                update={
                    "cursor": first.next_cursor,
                    "authz_fingerprint": "d" * 64,
                }
            )
        )
    store.record(_point(historical, "point-c", outcome="0", ts_utc=old_time))

    initial = store.purge_expired()
    assert initial.deleted_spans == 1
    assert initial.deleted_logs == 1
    assert initial.deleted_metric_points == 0
    assert store.get(current_span.trace_id, current_span.span_id) == current_span
    assert store.query_logs(_log_query()).items == (current_log,)
    assert store.metric_point_count == 3
    second = _store(path, clock, retention=retention).query(
        query.model_copy(update={"cursor": first.next_cursor})
    )
    assert len(first.series + second.series) == 2

    clock.advance(timedelta(hours=4))
    expired = store.purge_expired()
    assert expired.deleted_metric_points == 3
    assert store.metric_point_count == 0
    store.record(_point(historical, "point-a", outcome="a", ts_utc=old_time))
    assert store.metric_point_count == 0
    with pytest.raises(IntegrityViolation, match="point_id"):
        store.record(
            _point(
                historical,
                "point-a",
                value=2,
                outcome="a",
                ts_utc=old_time,
            )
        )
    assert store.get_metric_descriptor(historical.ref) == historical

    store.release_metric_descriptors(
        owner_kind=owner_kind,
        owner_id=f"{owner_kind}:1",
    )
    released = store.purge_expired()
    assert released.deleted_metric_descriptors == 1
    assert store.get_metric_descriptor(historical.ref) is None
    assert store.get_metric_descriptor(current.ref) == current
