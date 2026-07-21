"""Structured, trace-correlated logging with default content redaction."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from gameforge.contracts.observability import (
    LogErrorV1,
    LogLevel,
    LogQueryStore,
    LogRecordV1,
)
from gameforge.contracts.storage import UtcClock
from gameforge.runtime.observability.context import current_trace_context
from gameforge.runtime.observability._fields import (
    is_sensitive_key,
    redact_sensitive_text,
    sanitize_telemetry_value,
)


class StructuredLogger:
    __slots__ = (
        "_clock",
        "_id_generator",
        "_max_field_bytes",
        "_service",
        "_store",
        "_dropped_count",
    )

    def __init__(
        self,
        *,
        service: str,
        store: LogQueryStore,
        clock: UtcClock,
        id_generator: Callable[[], str],
        max_field_bytes: int = 4096,
    ) -> None:
        if not service or max_field_bytes <= 0:
            raise ValueError("structured logger requires service and a positive field byte limit")
        self._service = service
        self._store = store
        self._clock = clock
        self._id_generator = id_generator
        self._max_field_bytes = max_field_bytes
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def log(
        self,
        *,
        level: LogLevel,
        event_name: str,
        message: str,
        request_id: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        producer_run_id: str | None = None,
        fields: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> LogRecordV1 | None:
        current = current_trace_context()
        if current is not None:
            trace_id = trace_id or current.trace_id
            span_id = span_id or current.span_id
        sanitized, redacted = self._sanitize_fields(fields or {})
        if redacted:
            sanitized["redacted_fields"] = list(redacted)
        error_record = self._sanitize_error(error) if error is not None else None
        try:
            record = LogRecordV1(
                log_id=self._id_generator(),
                ts_utc=self._clock.now_utc(),
                level=level,
                message=self._bounded_text(message),
                service=self._service,
                event_name=event_name,
                request_id=request_id,
                run_id=run_id,
                trace_id=trace_id,
                span_id=span_id,
                producer_run_id=producer_run_id,
                error=error_record,
                fields=sanitized,
            )
            self._store.append(record)
        except Exception:
            self._dropped_count = min(self._dropped_count + 1, (1 << 63) - 1)
            return None
        return record

    def _sanitize_fields(self, fields: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
        result: dict[str, Any] = {}
        redacted: list[str] = []
        for key in sorted(fields):
            if is_sensitive_key(key):
                result[key] = "[REDACTED]"
                redacted.append(key)
                continue
            value = fields[key]
            sanitized = self._sanitize_value(value)
            if sanitized is _OMIT:
                result[key] = "[OMITTED]"
                redacted.append(key)
            else:
                sanitized_value, value_redacted = sanitized
                result[key] = sanitized_value
                if value_redacted:
                    redacted.append(key)
        return result, tuple(redacted)

    def _sanitize_value(self, value: Any) -> tuple[Any, bool] | _Omit:
        try:
            return sanitize_telemetry_value(
                value,
                max_string_bytes=self._max_field_bytes,
            )
        except (TypeError, ValueError):
            return _OMIT

    def _sanitize_error(self, error: BaseException) -> LogErrorV1:
        error_type = type(error).__qualname__
        raw_message = str(error)
        message = self._bounded_text(raw_message)
        fingerprint = hashlib.sha256(f"{error_type}:{message}".encode("utf-8")).hexdigest()
        return LogErrorV1(
            error_type=error_type,
            message=message,
            stack_fingerprint=fingerprint,
        )

    def _bounded_text(self, value: str) -> str:
        redacted = redact_sensitive_text(value)[0]
        encoded = redacted.encode("utf-8")
        if len(encoded) <= min(self._max_field_bytes, 2048):
            return redacted
        return "[OMITTED]"


class _Omit:
    __slots__ = ()


_OMIT = _Omit()


__all__ = ["StructuredLogger"]
