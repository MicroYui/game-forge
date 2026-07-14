"""Bounded local span exporters for tests and deterministic CI artifacts."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path

from gameforge.contracts.observability import SpanDataV1
from gameforge.runtime.observability._fields import (
    redact_span_values,
    span_contains_sensitive_fields,
)


DEFAULT_MAX_BATCH_SIZE = 256
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_LINE_BYTES = 4 * 1024 * 1024


def _positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _safe_span_bytes(span: SpanDataV1) -> bytes:
    if not isinstance(span, SpanDataV1):
        raise TypeError("span exporter requires SpanDataV1 values")
    parsed = SpanDataV1.model_validate(span.model_dump(mode="json"))
    if span_contains_sensitive_fields(parsed):
        raise ValueError("span contains a sensitive telemetry field")
    parsed = redact_span_values(parsed)
    return json.dumps(
        parsed.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class InMemoryExporter:
    """Retain canonical span bytes so callers cannot mutate exporter state."""

    def __init__(
        self,
        *,
        capacity: int = 10_000,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        max_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self._capacity = _positive_int(capacity, field_name="capacity")
        self._max_batch_size = _positive_int(max_batch_size, field_name="max_batch_size")
        self._max_bytes = _positive_int(max_bytes, field_name="max_bytes")
        self._byte_size = 0
        self._records: list[bytes] = []
        self._lock = threading.Lock()

    @property
    def spans(self) -> tuple[SpanDataV1, ...]:
        with self._lock:
            records = tuple(self._records)
        return tuple(SpanDataV1.model_validate_json(record) for record in records)

    def export(self, spans: Sequence[SpanDataV1]) -> None:
        if len(spans) > self._max_batch_size:
            raise BufferError("span export batch exceeds its bounded batch size")
        values = tuple(spans)
        records = tuple(_safe_span_bytes(span) for span in values)
        new_bytes = sum(len(record) for record in records)
        with self._lock:
            if (
                len(self._records) + len(records) > self._capacity
                or self._byte_size + new_bytes > self._max_bytes
            ):
                raise BufferError("in-memory span exporter capacity is exhausted")
            self._records.extend(records)
            self._byte_size += new_bytes


class FileExporter:
    """Append canonical SpanDataV1 records as deterministic UTF-8 NDJSON."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    ) -> None:
        self._path = Path(path)
        self._max_file_bytes = _positive_int(max_file_bytes, field_name="max_file_bytes")
        self._max_batch_size = _positive_int(max_batch_size, field_name="max_batch_size")
        self._max_line_bytes = _positive_int(max_line_bytes, field_name="max_line_bytes")
        self._lock = threading.Lock()

    def export(self, spans: Sequence[SpanDataV1]) -> None:
        if len(spans) > self._max_batch_size:
            raise BufferError("span export batch exceeds its bounded batch size")
        values = tuple(spans)
        lines: list[bytes] = []
        payload_size = 0
        for span in values:
            line = _safe_span_bytes(span) + b"\n"
            if len(line) > self._max_line_bytes:
                raise BufferError("span NDJSON record exceeds its bounded line size")
            payload_size += len(line)
            if payload_size > self._max_file_bytes:
                raise BufferError("span file exporter capacity is exhausted")
            lines.append(line)
        payload = b"".join(lines)
        if not payload:
            return

        with self._lock:
            current_size = self._path.stat().st_size if self._path.exists() else 0
            if current_size + len(payload) > self._max_file_bytes:
                raise BufferError("span file exporter capacity is exhausted")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("ab") as output:
                written = output.write(payload)
                if written != len(payload):
                    raise OSError("span file exporter performed a partial write")
