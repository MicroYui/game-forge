from __future__ import annotations

from datetime import UTC, datetime

from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.observability.in_memory import InMemoryTelemetryStore
from gameforge.runtime.observability.context import use_trace_context
from gameforge.runtime.observability.logs import StructuredLogger
from gameforge.contracts.observability import TraceContextV1


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def test_structured_logger_redacts_sensitive_and_oversized_fields() -> None:
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    logger = StructuredLogger(
        service="worker",
        store=store,
        clock=FrozenUtcClock(NOW),
        id_generator=lambda: "log-1",
        max_field_bytes=64,
    )

    record = logger.log(
        level="info",
        event_name="model.finished",
        message="finished",
        run_id="run-1",
        fields={
            "api_key": "secret-value",
            "prompt": "do not log this",
            "raw_response": {"private": "body"},
            "detail": "Authorization: Bearer sk-secret",
            "note": "raw_response: private",
            "json_note": 'raw_response: {"a":"private","b":"more"}',
            "observations": [
                "retrying",
                "Authorization: Bearer array-secret",
                "raw_response: array-private",
            ],
            "result": "x" * 200,
            "attempt": 1,
        },
    )

    assert record is not None
    wire = record.model_dump_json()
    assert "secret-value" not in wire
    assert "do not log this" not in wire
    assert "private" not in wire
    assert "more" not in wire
    assert "sk-secret" not in wire
    assert "array-secret" not in wire
    assert "array-private" not in wire
    assert "x" * 200 not in wire
    assert record.fields["attempt"] == 1
    assert record.fields["observations"][0] == "retrying"


def test_log_store_failure_is_best_effort() -> None:
    class BrokenStore:
        def append(self, record):
            raise OSError("disk full")

    logger = StructuredLogger(
        service="worker",
        store=BrokenStore(),
        clock=FrozenUtcClock(NOW),
        id_generator=lambda: "log-1",
    )
    assert logger.log(level="info", event_name="run", message="ok") is None
    assert logger.dropped_count == 1


def test_logger_inherits_current_trace_and_redacts_sensitive_message_and_error() -> None:
    store = InMemoryTelemetryStore(clock=FrozenUtcClock(NOW), signing_key=b"test-key")
    logger = StructuredLogger(
        service="worker",
        store=store,
        clock=FrozenUtcClock(NOW),
        id_generator=lambda: "log-1",
    )
    context = TraceContextV1(
        trace_id="1" * 32,
        span_id="2" * 16,
        trace_flags="01",
    )

    with use_trace_context(context):
        record = logger.log(
            level="error",
            event_name="provider.failed",
            message="Authorization: Bearer top-secret-token prompt: dump-world-state",
            error=RuntimeError("api_key=sk-live-secret raw_response: private-body"),
        )

    assert record is not None
    assert record.trace_id == context.trace_id
    assert record.span_id == context.span_id
    wire = record.model_dump_json()
    for secret in ("top-secret-token", "dump-world-state", "sk-live-secret", "private-body"):
        assert secret not in wire
