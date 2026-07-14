from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gameforge.contracts.observability import SpanDataV1, SpanEventV1
from gameforge.runtime.observability.exporters import FileExporter, InMemoryExporter


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def _span(*, span_id: str = "2" * 16, attributes: dict | None = None) -> SpanDataV1:
    return SpanDataV1(
        trace_id="1" * 32,
        span_id=span_id,
        parent_span_id=None,
        name="checker",
        attributes=attributes or {"run_id": "run-1"},
        links=(),
        events=(),
        status="ok",
        error=None,
        resource={"service.name": "gameforge-test"},
        started_at=NOW,
        ended_at=NOW,
        duration_ns=7,
    )


def test_in_memory_exporter_is_bounded_and_does_not_leak_mutable_state() -> None:
    exporter = InMemoryExporter(capacity=2, max_batch_size=2)
    first = _span()
    second = _span(span_id="3" * 16)

    exporter.export((first, second))
    observed = exporter.spans
    observed[0].attributes["run_id"] = "mutated"

    assert [span.span_id for span in exporter.spans] == [first.span_id, second.span_id]
    assert exporter.spans[0].attributes["run_id"] == "run-1"
    with pytest.raises(BufferError, match="capacity"):
        exporter.export((_span(span_id="4" * 16),))
    with pytest.raises(BufferError, match="batch"):
        InMemoryExporter(capacity=4, max_batch_size=1).export((first, second))


def test_file_exporter_writes_deterministic_canonical_ndjson_without_partial_overflow(
    tmp_path,
) -> None:
    path = tmp_path / "spans.ndjson"
    first = _span()
    second = _span(span_id="3" * 16)
    exporter = FileExporter(path, max_file_bytes=100_000, max_batch_size=2)

    exporter.export((first, second))

    expected = "".join(
        json.dumps(
            span.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
        for span in (first, second)
    )
    assert path.read_text(encoding="utf-8") == expected

    before = path.read_bytes()
    with pytest.raises(BufferError, match="capacity"):
        FileExporter(
            path,
            max_file_bytes=len(before),
            max_batch_size=2,
        ).export((first,))
    assert path.read_bytes() == before


def test_file_exporter_defensively_rejects_sensitive_fields(tmp_path) -> None:
    path = tmp_path / "spans.ndjson"
    exporter = FileExporter(path, max_file_bytes=100_000)

    with pytest.raises(ValueError, match="sensitive"):
        exporter.export((_span(attributes={"raw_prompt": "private prompt"}),))

    assert not path.exists()


def test_file_exporter_redacts_sensitive_text_under_ordinary_keys(tmp_path) -> None:
    path = tmp_path / "spans.ndjson"
    exporter = FileExporter(path, max_file_bytes=100_000)
    span = _span(
        attributes={
            "detail": "Authorization: Bearer sk-secret",
            "note": "raw_response: private",
            "observations": [
                "retrying",
                "Authorization: Bearer array-secret",
                "raw_response: array-private",
            ],
        }
    )

    exporter.export((span,))

    wire = path.read_text(encoding="utf-8")
    for secret in ("sk-secret", "private", "array-secret", "array-private"):
        assert secret not in wire
    stored = SpanDataV1.model_validate_json(wire)
    assert stored.attributes == {
        "detail": "Authorization: [REDACTED]",
        "note": "[REDACTED]",
        "observations": [
            "retrying",
            "Authorization: [REDACTED]",
            "[REDACTED]",
        ],
    }


def test_exporters_redact_sensitive_span_and_event_names(tmp_path) -> None:
    path = tmp_path / "spans.ndjson"
    span = _span().model_copy(
        update={
            "name": "Authorization: Bearer span-secret",
            "events": (
                SpanEventV1(
                    name="Authorization: Bearer event-secret",
                    occurred_at=NOW,
                ),
            ),
        }
    )

    FileExporter(path, max_file_bytes=100_000).export((span,))
    in_memory = InMemoryExporter()
    in_memory.export((span,))

    wire = path.read_text(encoding="utf-8")
    assert "span-secret" not in wire
    assert "event-secret" not in wire
    stored = SpanDataV1.model_validate_json(wire)
    assert stored.name == "Authorization: [REDACTED]"
    assert stored.events[0].name == "Authorization: [REDACTED]"
    assert in_memory.spans == (stored,)
