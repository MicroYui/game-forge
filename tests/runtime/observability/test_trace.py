from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

import pytest
from pydantic import ValidationError

from gameforge.contracts.observability import (
    MAX_ATTRIBUTE_COUNT,
    MAX_EVENT_COUNT,
    MAX_LINK_COUNT,
    SpanDataV1,
    SpanLinkV1,
    TraceContextV1,
)
from gameforge.runtime.clock import FrozenUtcClock, ManualMonotonicClock
from gameforge.runtime.observability.context import (
    TraceCarrier,
    current_trace_context,
    use_trace_context,
)
from gameforge.runtime.observability.exporters import InMemoryExporter
from gameforge.runtime.observability.trace import (
    AlwaysOffSampler,
    AlwaysOnSampler,
    BoundedDroppedTelemetryCounter,
    IdentitySpanProcessor,
    Tracer,
)


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


@dataclass
class _DeterministicIds:
    trace_ids: list[str]
    span_ids: list[str]

    def new_trace_id(self) -> str:
        return self.trace_ids.pop(0)

    def new_span_id(self) -> str:
        return self.span_ids.pop(0)


@dataclass
class _SequenceUtcClock:
    values: list[datetime]

    def now_utc(self) -> datetime:
        return self.values.pop(0)


class _FailingIds:
    def new_trace_id(self) -> str:
        raise OSError("id source unavailable")

    def new_span_id(self) -> str:
        raise OSError("id source unavailable")


class _FailingUtcClock:
    def now_utc(self) -> datetime:
        raise OSError("UTC clock unavailable")


class _FailingMonotonicClock:
    def now_ns(self) -> int:
        raise OSError("monotonic clock unavailable")


@dataclass
class _RecordingProcessor:
    starts: list[tuple[TraceContextV1, TraceContextV1 | None, str]] = field(default_factory=list)
    ends: list[SpanDataV1] = field(default_factory=list)

    def on_start(
        self,
        context: TraceContextV1,
        *,
        parent_context: TraceContextV1 | None,
        name: str,
    ) -> None:
        self.starts.append((context, parent_context, name))

    def on_end(self, span: SpanDataV1) -> None:
        self.ends.append(span)


@dataclass
class _RecordingSampler:
    names: list[str] = field(default_factory=list)

    def should_sample(
        self,
        *,
        parent_context: TraceContextV1 | None,
        name: str,
        attributes: Mapping[str, Any],
    ) -> bool:
        del parent_context, attributes
        self.names.append(name)
        return True


def _tracer(
    *,
    exporter: Any,
    ids: _DeterministicIds | None = None,
    sampler: Any | None = None,
    processor: Any | None = None,
    utc_clock: Any | None = None,
    monotonic_clock: ManualMonotonicClock | None = None,
    resource: Mapping[str, Any] | None = None,
    dropped_counter: BoundedDroppedTelemetryCounter | None = None,
) -> Tracer:
    return Tracer(
        id_generator=ids
        or _DeterministicIds(
            trace_ids=[f"{index:032x}" for index in range(1, 17)],
            span_ids=[f"{index:016x}" for index in range(1, 17)],
        ),
        sampler=sampler or AlwaysOnSampler(),
        processor=processor or IdentitySpanProcessor(),
        exporter=exporter,
        utc_clock=utc_clock or FrozenUtcClock(NOW),
        monotonic_clock=monotonic_clock or ManualMonotonicClock(),
        resource=resource or {"service.name": "gameforge-test"},
        dropped_counter=dropped_counter,
    )


def test_span_uses_utc_endpoints_and_monotonic_duration_with_safe_bounded_fields() -> None:
    exporter = InMemoryExporter(capacity=4)
    processor = _RecordingProcessor()
    monotonic = ManualMonotonicClock(initial_ns=100)
    dropped = BoundedDroppedTelemetryCounter(max_count=10)
    tracer = _tracer(
        exporter=exporter,
        processor=processor,
        utc_clock=_SequenceUtcClock([NOW, NOW + timedelta(seconds=1), NOW - timedelta(minutes=5)]),
        monotonic_clock=monotonic,
        resource={"service.name": "gameforge-test", "api_key": "resource-secret"},
        dropped_counter=dropped,
    )
    link_context = TraceContextV1(
        trace_id="a" * 32,
        span_id="b" * 16,
        trace_flags="00",
    )

    with tracer.span(
        "provider.call",
        attributes={
            "recorded_provider_latency_ms": 900,
            "raw_prompt": "do not export this prompt",
            "detail": "Authorization: Bearer sk-secret",
            "note": "raw_response: private",
            "observations": [
                "retrying",
                "Authorization: Bearer array-secret",
                "raw_response: array-private",
            ],
        },
        links=(SpanLinkV1(context=link_context, attributes={"kind": "retry"}),),
    ) as span:
        assert current_trace_context() == span.context
        assert span.set_attribute("producer_run_id", "producer-1")
        assert span.add_event(
            "provider.response",
            attributes={"outcome": "ok", "authorization": "Bearer secret"},
        )
        span.set_status("ok")
        monotonic.advance_ns(37)

    completed = exporter.spans[0]
    wire = completed.model_dump_json()
    for secret in ("sk-secret", "private", "array-secret", "array-private"):
        assert secret not in wire
    assert completed.started_at == NOW
    assert completed.ended_at == NOW - timedelta(minutes=5)
    assert completed.duration_ns == 37
    assert completed.attributes == {
        "detail": "Authorization: [REDACTED]",
        "note": "[REDACTED]",
        "observations": [
            "retrying",
            "Authorization: [REDACTED]",
            "[REDACTED]",
        ],
        "producer_run_id": "producer-1",
        "recorded_provider_latency_ms": 900,
    }
    assert completed.events[0].attributes == {"outcome": "ok"}
    assert completed.resource == {"service.name": "gameforge-test"}
    assert completed.links[0].context == link_context
    assert processor.starts == [(span.context, None, "provider.call")]
    assert processor.ends[0] == completed
    assert tracer.dropped_telemetry_count == 6

    with pytest.raises(ValidationError):
        completed.name = "mutated"  # type: ignore[misc]
    completed.attributes["producer_run_id"] = "mutated"
    assert exporter.spans[0].attributes["producer_run_id"] == "producer-1"


def test_nested_sync_and_async_spans_keep_one_trace_and_restore_parent_context() -> None:
    exporter = InMemoryExporter(capacity=8)
    tracer = _tracer(
        exporter=exporter,
        ids=_DeterministicIds(
            trace_ids=["1" * 32],
            span_ids=["2" * 16, "3" * 16, "4" * 16],
        ),
    )

    async def async_child() -> TraceContextV1:
        await asyncio.sleep(0)
        with tracer.span("async-child") as child:
            return child.context

    with tracer.span("root") as root:
        with tracer.span("sync-child") as sync_child:
            assert sync_child.context.trace_id == root.context.trace_id
            assert sync_child.context.span_id != root.context.span_id
        assert current_trace_context() == root.context
        async_context = asyncio.run(async_child())
        assert async_context.trace_id == root.context.trace_id
        assert current_trace_context() == root.context

    assert current_trace_context() is None
    by_name = {span.name: span for span in exporter.spans}
    assert by_name["root"].parent_span_id is None
    assert by_name["sync-child"].parent_span_id == root.context.span_id
    assert by_name["async-child"].parent_span_id == root.context.span_id


def test_unsampled_span_propagates_context_but_is_not_exported() -> None:
    exporter = InMemoryExporter(capacity=2)
    tracer = _tracer(exporter=exporter, sampler=AlwaysOffSampler())

    with tracer.span("not-recorded") as span:
        assert span.context.trace_flags == "00"
        assert current_trace_context() == span.context

    assert exporter.spans == ()
    assert tracer.dropped_telemetry_count == 0


def test_extracted_carrier_context_becomes_the_span_parent_without_becoming_content() -> None:
    exporter = InMemoryExporter(capacity=2)
    tracer = _tracer(exporter=exporter)
    remote = TraceContextV1(
        trace_id="a" * 32,
        span_id="b" * 16,
        trace_flags="01",
        trace_state="vendor=value",
    )
    extracted = TraceCarrier.extract(TraceCarrier.inject(remote))
    assert extracted is not None

    with use_trace_context(extracted):
        with tracer.span("worker-attempt") as child:
            assert child.context.trace_id == remote.trace_id
            assert child.context.trace_state == remote.trace_state

    completed = exporter.spans[0]
    assert completed.parent_span_id == remote.span_id
    assert "trace_id" not in completed.attributes


def test_span_caps_attributes_events_and_links_and_counts_each_drop() -> None:
    exporter = InMemoryExporter(capacity=2)
    dropped = BoundedDroppedTelemetryCounter(max_count=10_000)
    tracer = _tracer(exporter=exporter, dropped_counter=dropped)
    link_context = TraceContextV1(
        trace_id="a" * 32,
        span_id="b" * 16,
        trace_flags="01",
    )

    with tracer.span(
        "bounded",
        attributes={f"field_{index:03d}": index for index in range(MAX_ATTRIBUTE_COUNT)},
    ) as span:
        assert not span.set_attribute("field_overflow", "dropped")
        for index in range(MAX_EVENT_COUNT + 1):
            span.add_event(f"event-{index}")
        for index in range(MAX_LINK_COUNT + 1):
            span.add_link(link_context, attributes={"ordinal": index})

    completed = exporter.spans[0]
    assert len(completed.attributes) == MAX_ATTRIBUTE_COUNT
    assert len(completed.events) == MAX_EVENT_COUNT
    assert len(completed.links) == MAX_LINK_COUNT
    assert tracer.dropped_telemetry_count == 3


class _FailingExporter:
    def export(self, spans: tuple[SpanDataV1, ...]) -> None:
        del spans
        raise RuntimeError("telemetry backend unavailable")


def test_exporter_failure_is_isolated_and_dropped_counter_saturates() -> None:
    dropped = BoundedDroppedTelemetryCounter(max_count=1)
    tracer = _tracer(exporter=_FailingExporter(), dropped_counter=dropped)
    business_result: list[str] = []

    with tracer.span("business-operation"):
        business_result.append("completed")
    with tracer.span("another-operation"):
        business_result.append("also-completed")

    assert business_result == ["completed", "also-completed"]
    assert tracer.dropped_telemetry_count == 1


def test_completed_span_redacts_names_before_processor_and_return() -> None:
    exporter = InMemoryExporter(capacity=2)
    processor = _RecordingProcessor()
    sampler = _RecordingSampler()
    tracer = _tracer(exporter=exporter, processor=processor, sampler=sampler)

    with tracer.span("Authorization: Bearer span-secret") as span:
        assert span.add_event("Authorization: Bearer event-secret")
        assert sampler.names == ["Authorization: [REDACTED]"]
        assert processor.starts[0][2] == "Authorization: [REDACTED]"
        assert span._events[0].name == "Authorization: [REDACTED]"
        assert tracer.dropped_telemetry_count == 2

    completed = span.data
    assert completed is not None
    for observed in (completed, processor.ends[0], exporter.spans[0]):
        wire = observed.model_dump_json()
        assert "span-secret" not in wire
        assert "event-secret" not in wire
        assert observed.name == "Authorization: [REDACTED]"
        assert observed.events[0].name == "Authorization: [REDACTED]"


@pytest.mark.parametrize(
    "ids",
    [
        pytest.param(_FailingIds(), id="generator-error"),
        pytest.param(
            _DeterministicIds(trace_ids=["invalid"], span_ids=["invalid"]),
            id="invalid-context-dto",
        ),
    ],
)
def test_trace_identity_failures_use_a_non_recording_span(ids: Any) -> None:
    exporter = InMemoryExporter(capacity=2)
    tracer = _tracer(exporter=exporter, ids=ids)
    business_result: list[str] = []

    with tracer.span("business-operation") as span:
        assert span.data is None
        assert not span.set_attribute("outcome", "ok")
        assert not span.add_event("completed")
        span.set_status("ok")
        business_result.append("completed")

    assert business_result == ["completed"]
    assert exporter.spans == ()
    assert tracer.dropped_telemetry_count == 1


@pytest.mark.parametrize(
    ("utc_clock", "monotonic_clock"),
    [
        pytest.param(_FailingUtcClock(), ManualMonotonicClock(), id="utc-clock"),
        pytest.param(FrozenUtcClock(NOW), _FailingMonotonicClock(), id="monotonic-clock"),
    ],
)
def test_trace_start_clock_failures_use_a_non_recording_span(
    utc_clock: Any,
    monotonic_clock: Any,
) -> None:
    exporter = InMemoryExporter(capacity=2)
    tracer = _tracer(
        exporter=exporter,
        utc_clock=utc_clock,
        monotonic_clock=monotonic_clock,
    )
    business_result: list[str] = []

    with tracer.span("business-operation"):
        business_result.append("completed")

    assert business_result == ["completed"]
    assert exporter.spans == ()
    assert tracer.dropped_telemetry_count == 1


def test_business_exception_is_reraised_with_redacted_span_error() -> None:
    exporter = InMemoryExporter(capacity=2)
    tracer = _tracer(exporter=exporter)

    with pytest.raises(ValueError, match="Bearer private-token"):
        with tracer.span("failing-operation"):
            raise ValueError("Bearer private-token")

    completed = exporter.spans[0]
    assert completed.status == "error"
    assert completed.error is not None
    assert completed.error.error_type == "ValueError"
    assert "private-token" not in completed.error.message
