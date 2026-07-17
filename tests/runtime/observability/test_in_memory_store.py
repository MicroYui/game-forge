from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gameforge.contracts.errors import CursorInvalid
from gameforge.contracts.observability import (
    LogQueryV1,
    LogRecordV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    SpanDataV1,
    SpanEventV1,
    TimeRangeV1,
    TraceQueryV1,
    compute_metric_descriptor_digest,
    compute_metric_registry_digest,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.observability.in_memory import (
    InMemoryTelemetryStore,
    TelemetryStoreLimits,
)


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def _span(index: int, *, run_id: str) -> SpanDataV1:
    return SpanDataV1(
        trace_id=f"{index:032x}",
        span_id=f"{index:016x}",
        parent_span_id=None,
        name="checker",
        attributes={"run_id": run_id},
        links=(),
        events=(),
        status="ok",
        error=None,
        resource={"service.name": "gameforge-test"},
        started_at=NOW + timedelta(seconds=index),
        ended_at=NOW + timedelta(seconds=index + 1),
        duration_ns=1_000,
    )


def test_trace_cursor_is_stable_tamper_evident_and_excludes_later_writes() -> None:
    clock = FrozenUtcClock(NOW)
    store = InMemoryTelemetryStore(clock=clock, signing_key=b"test-key")
    store.put(_span(1, run_id="run-1"))
    store.put(_span(2, run_id="run-2"))
    query = TraceQueryV1(
        time_range=TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=1)),
        limit=1,
        authz_fingerprint="a" * 64,
    )
    first = store.query_traces(query)
    assert len(first.items) == 1
    assert first.next_cursor is not None

    store.put(_span(3, run_id="run-3"))
    second = store.query_traces(query.model_copy(update={"cursor": first.next_cursor}))
    assert [item.run_ids for item in first.items + second.items] == [("run-1",), ("run-2",)]

    tampered = first.next_cursor[:-1] + ("A" if first.next_cursor[-1] != "A" else "B")
    with pytest.raises(CursorInvalid):
        store.query_traces(query.model_copy(update={"cursor": tampered}))


def test_log_queries_are_bounded_stable_and_authorization_bound() -> None:
    clock = FrozenUtcClock(NOW)
    store = InMemoryTelemetryStore(clock=clock, signing_key=b"test-key")
    for index in range(2):
        store.append(
            LogRecordV1(
                log_id=f"log-{index}",
                ts_utc=NOW + timedelta(seconds=index),
                level="info",
                message="done",
                service="api",
                event_name="run.done",
                run_id=f"run-{index}",
            )
        )
    query = LogQueryV1(
        time_range=TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=1)),
        services=("api",),
        limit=1,
        authz_fingerprint="b" * 64,
    )
    first = store.query_logs(query)
    assert first.next_cursor

    with pytest.raises(CursorInvalid):
        store.query_logs(
            query.model_copy(update={"cursor": first.next_cursor, "authz_fingerprint": "c" * 64})
        )


def test_in_memory_log_run_scope_filters_before_paging_and_binds_cursor() -> None:
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    records = (
        LogRecordV1(
            log_id="log-runless",
            ts_utc=NOW,
            level="info",
            message="done",
            service="api",
            event_name="run.done",
        ),
        LogRecordV1(
            log_id="log-a",
            ts_utc=NOW + timedelta(seconds=1),
            level="info",
            message="done",
            service="api",
            event_name="run.done",
            run_id="run:A",
        ),
        LogRecordV1(
            log_id="log-b",
            ts_utc=NOW + timedelta(seconds=2),
            level="info",
            message="done",
            service="api",
            event_name="run.done",
            producer_run_id="run:B",
        ),
    )
    for record in records:
        store.append(record)
    query = LogQueryV1(
        time_range=TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=1)),
        limit=1,
        authz_fingerprint="b" * 64,
    )
    principal = "1" * 64

    domainless = store.query_logs(
        query,
        principal_binding=principal,
        run_scope_mode="domainless_only",
    )
    assert tuple(item.record for item in domainless.items) == (records[0],)
    assert domainless.next_cursor is None

    first = store.query_logs(
        query,
        principal_binding=principal,
        run_scope_mode="run_allowlist",
        allowed_run_ids=("run:A",),
    )
    assert tuple(item.record for item in first.items) == (records[0],)
    assert first.next_cursor is not None
    second = store.query_logs(
        query.model_copy(update={"cursor": first.next_cursor}),
        principal_binding=principal,
        run_scope_mode="run_allowlist",
        allowed_run_ids=("run:A",),
    )
    assert tuple(item.record for item in second.items) == (records[1],)

    with pytest.raises(CursorInvalid):
        store.query_logs(
            query.model_copy(update={"cursor": first.next_cursor}),
            principal_binding=principal,
            run_scope_mode="run_allowlist",
            allowed_run_ids=("run:B",),
        )


def test_in_memory_log_scope_includes_trace_membership_and_freezes_snapshot() -> None:
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    trace_id = "a" * 32
    span_a = _span(10, run_id="run:A").model_copy(update={"trace_id": trace_id})
    store.put(span_a)
    logs = tuple(
        LogRecordV1(
            log_id=f"trace-log-{index}",
            ts_utc=NOW + timedelta(seconds=index),
            level="info",
            message="done",
            service="api",
            event_name="run.done",
            trace_id=trace_id,
            span_id=span_a.span_id,
        )
        for index in (1, 2)
    )
    for record in logs:
        store.append(record)
    query = LogQueryV1(
        time_range=TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=1)),
        limit=1,
        authz_fingerprint="b" * 64,
    )
    assert (
        store.query_logs(
            query,
            run_scope_mode="domainless_only",
        ).items
        == ()
    )
    first = store.query_logs(
        query,
        run_scope_mode="run_allowlist",
        allowed_run_ids=("run:A",),
    )
    assert first.next_cursor is not None

    store.put(
        _span(11, run_id="ignored").model_copy(
            update={"trace_id": trace_id, "attributes": {"producer_run_id": "run:B"}}
        )
    )
    second = store.query_logs(
        query.model_copy(update={"cursor": first.next_cursor}),
        run_scope_mode="run_allowlist",
        allowed_run_ids=("run:A",),
    )

    assert tuple(item.record for item in first.items + second.items) == logs
    assert (
        store.query_logs(
            query.model_copy(update={"limit": 10}),
            run_scope_mode="run_allowlist",
            allowed_run_ids=("run:A",),
        ).items
        == ()
    )


def test_in_memory_scoped_logs_exclude_orphan_trace_correlation() -> None:
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    store.append(
        LogRecordV1(
            log_id="orphan-log",
            ts_utc=NOW,
            level="info",
            message="done",
            service="api",
            event_name="run.done",
            trace_id="f" * 32,
            span_id="1" * 16,
        )
    )
    query = LogQueryV1(
        time_range=TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=1)),
        limit=10,
        authz_fingerprint="b" * 64,
    )

    assert store.query_logs(query, run_scope_mode="domainless_only").items == ()


def test_in_memory_trace_scope_includes_producer_run_correlations() -> None:
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    producer_only = _span(1, run_id="ignored").model_copy(
        update={"attributes": {"producer_run_id": "run:B"}}
    )
    cross_domain = _span(2, run_id="ignored").model_copy(
        update={"attributes": {"run_id": "run:A", "producer_run_id": "run:B"}}
    )
    store.put(producer_only)
    store.put(cross_domain)

    assert store.get_trace_summary(producer_only.trace_id).run_ids == ("run:B",)
    assert store.get_trace_summary(cross_domain.trace_id).run_ids == ("run:A", "run:B")
    page = store.page_run_traces(
        "run:B",
        cursor=None,
        limit=10,
        authz_fingerprint="a" * 64,
    )
    assert tuple(item.trace_id for item in page.items) == (
        producer_only.trace_id,
        cross_domain.trace_id,
    )


def test_in_memory_span_and_run_trace_scopes_precede_pagination() -> None:
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    trace_id = "d" * 32
    runless = _span(30, run_id="ignored").model_copy(
        update={"trace_id": trace_id, "attributes": {}}
    )
    run_a = _span(31, run_id="run:A").model_copy(update={"trace_id": trace_id})
    cross = _span(32, run_id="ignored").model_copy(
        update={
            "trace_id": "e" * 32,
            "attributes": {"run_id": "run:A", "producer_run_id": "run:B"},
        }
    )
    for span in (runless, run_a, cross):
        store.put(span)

    first = store.page_spans(
        trace_id,
        cursor=None,
        limit=1,
        authz_fingerprint="a" * 64,
        run_scope_mode="run_allowlist",
        allowed_run_ids=("run:A",),
    )
    assert first.next_cursor is not None
    with pytest.raises(CursorInvalid):
        store.page_spans(
            trace_id,
            cursor=first.next_cursor,
            limit=1,
            authz_fingerprint="a" * 64,
            run_scope_mode="run_allowlist",
            allowed_run_ids=("run:B",),
        )
    with pytest.raises(IntegrityViolation, match="scope changed"):
        store.page_spans(
            cross.trace_id,
            cursor=None,
            limit=10,
            authz_fingerprint="a" * 64,
            run_scope_mode="run_allowlist",
            allowed_run_ids=("run:A",),
        )

    assert store.get_run_trace_scope("run:A") == ("run:A", "run:B")
    a_only = store.page_run_traces(
        "run:A",
        cursor=None,
        limit=10,
        authz_fingerprint="a" * 64,
        run_scope_mode="run_allowlist",
        allowed_run_ids=("run:A",),
    )
    assert tuple(item.trace_id for item in a_only.items) == (trace_id,)
    both = store.page_run_traces(
        "run:A",
        cursor=None,
        limit=10,
        authz_fingerprint="a" * 64,
        run_scope_mode="run_allowlist",
        allowed_run_ids=("run:A", "run:B"),
    )
    assert tuple(item.trace_id for item in both.items) == (trace_id, cross.trace_id)

    query = TraceQueryV1(
        time_range=TimeRangeV1(start_utc=NOW, end_utc=NOW + timedelta(minutes=1)),
        limit=10,
        authz_fingerprint="a" * 64,
    )
    scoped = store.query_traces(
        query,
        run_scope_mode="run_allowlist",
        allowed_run_ids=("run:A",),
    )
    assert tuple(item.trace_id for item in scoped.items) == (trace_id,)


def test_in_memory_store_detaches_nested_payloads_on_write_and_read() -> None:
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    source = _span(1, run_id="run-1")
    store.put(source)
    source.attributes["run_id"] = "mutated-source"

    first = store.get(source.trace_id, source.span_id)
    assert first is not None
    assert first.attributes["run_id"] == "run-1"
    first.attributes["run_id"] = "mutated-read"
    second = store.get(source.trace_id, source.span_id)
    assert second is not None
    assert second.attributes["run_id"] == "run-1"


def test_in_memory_store_defensively_sanitizes_spans_on_write_and_read() -> None:
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    source = _span(1, run_id="run-1").model_copy(
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
    observed = store.get(source.trace_id, source.span_id)

    assert observed is not None
    wire = observed.model_dump_json()
    for secret in (
        "span-secret",
        "attribute-secret",
        "value-secret",
        "event-secret",
        "event-attribute-secret",
    ):
        assert secret not in wire
    assert observed.attributes["authorization"] == "[REDACTED]"
    assert observed.events[0].attributes["api_key"] == "[REDACTED]"

    # Query/read paths also sanitize a valid legacy or externally injected DTO.
    store._spans[(source.trace_id, source.span_id)] = source
    page = store.page_spans(
        source.trace_id,
        cursor=None,
        limit=10,
        authz_fingerprint="a" * 64,
    )
    assert "span-secret" not in page.model_dump_json()
    assert "event-secret" not in page.model_dump_json()


def test_registry_version_and_descriptor_version_are_immutable_identities() -> None:
    def descriptor(series_limit: int) -> MetricDescriptorV1:
        payload = {
            "metric_name": "gameforge.run.completed",
            "descriptor_version": 1,
            "metric_type": "counter",
            "unit": "count",
            "label_keys": ("outcome",),
            "histogram_bucket_bounds": (),
            "series_limit": series_limit,
        }
        return MetricDescriptorV1(
            **payload,
            descriptor_digest=compute_metric_descriptor_digest(payload),
        )

    def registry(item: MetricDescriptorV1) -> MetricDescriptorRegistryV1:
        payload = {
            "registry_version": 1,
            "descriptors": (item,),
            "global_series_limit": 10,
        }
        return MetricDescriptorRegistryV1(
            **payload,
            registry_digest=compute_metric_registry_digest(payload),
        )

    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    store.register_metric_registry(registry(descriptor(2)))
    with pytest.raises(IntegrityViolation, match="version"):
        store.register_metric_registry(registry(descriptor(3)))


def test_typed_log_and_metric_query_method_names_do_not_collide() -> None:
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    assert callable(store.query_logs)
    assert callable(store.query_metrics)


def test_in_memory_store_enforces_record_and_total_byte_capacity() -> None:
    store = InMemoryTelemetryStore(
        clock=FrozenUtcClock(NOW),
        signing_key=b"test-key",
        limits=TelemetryStoreLimits(
            max_stored_spans=1,
            max_stored_logs=1,
            max_stored_metric_points=1,
            max_stored_bytes=1_000_000,
        ),
    )
    store.put(_span(1, run_id="run-1"))
    with pytest.raises(BufferError, match="span record capacity"):
        store.put(_span(2, run_id="run-2"))

    first_log = LogRecordV1(
        log_id="log-1",
        ts_utc=NOW,
        level="info",
        message="done",
        service="api",
        event_name="run.done",
    )
    store.append(first_log)
    with pytest.raises(BufferError, match="log record capacity"):
        store.append(first_log.model_copy(update={"log_id": "log-2"}))

    tiny = InMemoryTelemetryStore(
        clock=FrozenUtcClock(NOW),
        signing_key=b"test-key",
        limits=TelemetryStoreLimits(max_stored_bytes=1),
    )
    with pytest.raises(BufferError, match="byte capacity"):
        tiny.put(_span(1, run_id="run-1"))
