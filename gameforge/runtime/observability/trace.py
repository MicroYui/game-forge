"""Injectable tracing runtime with best-effort bounded publication."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from itertools import islice
from typing import Any, Literal, Protocol

from pydantic import JsonValue

from gameforge.contracts.observability import (
    MAX_EVENT_COUNT,
    MAX_LINK_COUNT,
    SpanDataV1,
    SpanErrorV1,
    SpanEventV1,
    SpanExporter,
    SpanLinkV1,
    SpanStatus,
    TraceContextV1,
)
from gameforge.contracts.storage import MonotonicClock, UtcClock
from gameforge.runtime.clock import SystemMonotonicClock, SystemUtcClock
from gameforge.runtime.observability._fields import (
    add_field,
    redact_sensitive_text,
    redact_span_values,
    sanitize_fields,
)
from gameforge.runtime.observability.context import current_trace_context, use_trace_context


class IdGenerator(Protocol):
    def new_trace_id(self) -> str: ...

    def new_span_id(self) -> str: ...


class Sampler(Protocol):
    def should_sample(
        self,
        *,
        parent_context: TraceContextV1 | None,
        name: str,
        attributes: Mapping[str, JsonValue],
    ) -> bool: ...


class SpanProcessor(Protocol):
    def on_start(
        self,
        context: TraceContextV1,
        *,
        parent_context: TraceContextV1 | None,
        name: str,
    ) -> None: ...

    def on_end(self, span: SpanDataV1) -> None: ...


class RandomIdGenerator:
    def new_trace_id(self) -> str:
        return self._nonzero_hex(16)

    def new_span_id(self) -> str:
        return self._nonzero_hex(8)

    @staticmethod
    def _nonzero_hex(byte_count: int) -> str:
        while True:
            value = secrets.token_hex(byte_count)
            if set(value) != {"0"}:
                return value


class AlwaysOnSampler:
    def should_sample(
        self,
        *,
        parent_context: TraceContextV1 | None,
        name: str,
        attributes: Mapping[str, JsonValue],
    ) -> bool:
        del parent_context, name, attributes
        return True


class AlwaysOffSampler:
    def should_sample(
        self,
        *,
        parent_context: TraceContextV1 | None,
        name: str,
        attributes: Mapping[str, JsonValue],
    ) -> bool:
        del parent_context, name, attributes
        return False


class IdentitySpanProcessor:
    def on_start(
        self,
        context: TraceContextV1,
        *,
        parent_context: TraceContextV1 | None,
        name: str,
    ) -> None:
        del context, parent_context, name

    def on_end(self, span: SpanDataV1) -> None:
        del span


class BoundedDroppedTelemetryCounter:
    """Saturating counter that cannot recursively emit more telemetry."""

    def __init__(self, *, max_count: int = (2**63) - 1) -> None:
        if isinstance(max_count, bool) or not isinstance(max_count, int) or max_count < 1:
            raise ValueError("max_count must be a positive integer")
        self._max_count = max_count
        self._count = 0
        self._lock = threading.Lock()

    @property
    def value(self) -> int:
        with self._lock:
            return self._count

    def increment(self, amount: int = 1) -> None:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise ValueError("dropped telemetry increment must be a positive integer")
        with self._lock:
            self._count = min(self._max_count, self._count + amount)


def _clone_span(span: SpanDataV1) -> SpanDataV1:
    return SpanDataV1.model_validate(span.model_dump(mode="json"))


class _NonRecordingSpan(AbstractContextManager["_NonRecordingSpan"]):
    """No-op span used when telemetry dependencies fail before a span can start."""

    def __init__(self, *, parent_context: TraceContextV1 | None) -> None:
        self.context = parent_context

    @property
    def data(self) -> None:
        return None

    def __enter__(self) -> _NonRecordingSpan:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Literal[False]:
        del exc_type, exc, traceback
        return False

    def set_attribute(self, key: str, value: Any) -> bool:
        del key, value
        return False

    def add_event(self, name: str, *, attributes: Mapping[str, Any] | None = None) -> bool:
        del name, attributes
        return False

    def add_link(
        self,
        context: TraceContextV1,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> bool:
        del context, attributes
        return False

    def set_status(self, status: SpanStatus) -> None:
        del status

    def record_error(self, error: BaseException) -> None:
        del error

    def end(self) -> None:
        return None


class Span(AbstractContextManager["Span"]):
    def __init__(
        self,
        *,
        tracer: Tracer,
        context: TraceContextV1,
        parent_context: TraceContextV1 | None,
        name: str,
        attributes: dict[str, JsonValue],
        links: Sequence[SpanLinkV1],
        sampled: bool,
    ) -> None:
        self._tracer = tracer
        self.context = context
        self._parent_context = parent_context
        self._name = name
        self._attributes = dict(attributes)
        self._links: list[SpanLinkV1] = []
        self._events: list[SpanEventV1] = []
        self._status: SpanStatus = "unset"
        self._error: SpanErrorV1 | None = None
        self._sampled = sampled
        self._started_at = tracer._utc_clock.now_utc()
        self._started_ns = tracer._monotonic_clock.now_ns()
        self._completed: SpanDataV1 | None = None
        self._ended = False
        self._entered = False
        self._scope: AbstractContextManager[TraceContextV1] | None = None
        bounded_links = tuple(islice(iter(links), MAX_LINK_COUNT + 1))
        for link in bounded_links[:MAX_LINK_COUNT]:
            self.add_link(link.context, attributes=link.attributes)
        if len(bounded_links) > MAX_LINK_COUNT:
            tracer._drop()

    @property
    def data(self) -> SpanDataV1 | None:
        return None if self._completed is None else _clone_span(self._completed)

    def __enter__(self) -> Span:
        if self._entered or self._ended:
            raise RuntimeError("span context manager cannot be entered more than once")
        self._entered = True
        self._scope = use_trace_context(self.context)
        self._scope.__enter__()
        if self._sampled:
            try:
                self._tracer._processor.on_start(
                    self.context,
                    parent_context=self._parent_context,
                    name=self._name,
                )
            except Exception:
                self._tracer._drop()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Literal[False]:
        try:
            if exc is not None and not self._ended:
                self.record_error(exc)
            self.end()
        finally:
            if self._scope is not None:
                self._scope.__exit__(exc_type, exc, traceback)
                self._scope = None
        return False

    def _ensure_active(self) -> None:
        if self._ended:
            raise RuntimeError("completed spans cannot be changed")

    def set_attribute(self, key: str, value: Any) -> bool:
        self._ensure_active()
        return add_field(self._attributes, key, value, on_drop=self._tracer._drop)

    def add_event(self, name: str, *, attributes: Mapping[str, Any] | None = None) -> bool:
        self._ensure_active()
        if len(self._events) >= MAX_EVENT_COUNT:
            self._tracer._drop()
            return False
        try:
            sanitized_name, redacted = redact_sensitive_text(name)
        except TypeError:
            self._tracer._drop()
            return False
        if redacted:
            self._tracer._drop()
        event_attributes = sanitize_fields(attributes, on_drop=self._tracer._drop)
        try:
            event = SpanEventV1(
                name=sanitized_name,
                occurred_at=self._tracer._utc_clock.now_utc(),
                attributes=event_attributes,
            )
        except (TypeError, ValueError):
            self._tracer._drop()
            return False
        self._events.append(event)
        return True

    def add_link(
        self,
        context: TraceContextV1,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> bool:
        self._ensure_active()
        if len(self._links) >= MAX_LINK_COUNT:
            self._tracer._drop()
            return False
        link_attributes = sanitize_fields(attributes, on_drop=self._tracer._drop)
        try:
            link = SpanLinkV1(context=context, attributes=link_attributes)
        except (TypeError, ValueError):
            self._tracer._drop()
            return False
        self._links.append(link)
        return True

    def set_status(self, status: SpanStatus) -> None:
        self._ensure_active()
        if status not in {"unset", "ok", "error"}:
            raise ValueError("span status is invalid")
        self._status = status
        if status != "error":
            self._error = None

    def record_error(self, error: BaseException) -> None:
        self._ensure_active()
        error_type = type(error).__name__[:512] or "Exception"
        self._status = "error"
        self._error = SpanErrorV1(
            error_type=error_type,
            message="span body raised an exception",
        )

    def end(self) -> SpanDataV1 | None:
        if self._ended:
            return self.data
        self._ended = True
        if not self._sampled:
            return None
        try:
            ended_at = self._tracer._utc_clock.now_utc()
            ended_ns = self._tracer._monotonic_clock.now_ns()
            completed = redact_span_values(
                SpanDataV1(
                    trace_id=self.context.trace_id,
                    span_id=self.context.span_id,
                    parent_span_id=(
                        None if self._parent_context is None else self._parent_context.span_id
                    ),
                    name=self._name,
                    attributes=self._attributes,
                    links=tuple(self._links),
                    events=tuple(self._events),
                    status=self._status,
                    error=self._error,
                    resource=self._tracer._resource,
                    started_at=self._started_at,
                    ended_at=ended_at,
                    duration_ns=ended_ns - self._started_ns,
                )
            )
        except Exception:
            self._tracer._drop()
            return None
        self._completed = _clone_span(completed)
        try:
            self._tracer._processor.on_end(_clone_span(completed))
        except Exception:
            self._tracer._drop()
        try:
            self._tracer._exporter.export((_clone_span(completed),))
        except Exception:
            self._tracer._drop()
        return self.data


class Tracer:
    def __init__(
        self,
        *,
        exporter: SpanExporter,
        id_generator: IdGenerator | None = None,
        sampler: Sampler | None = None,
        processor: SpanProcessor | None = None,
        utc_clock: UtcClock | None = None,
        monotonic_clock: MonotonicClock | None = None,
        resource: Mapping[str, Any] | None = None,
        dropped_counter: BoundedDroppedTelemetryCounter | None = None,
    ) -> None:
        self._exporter = exporter
        self._id_generator = id_generator or RandomIdGenerator()
        self._sampler = sampler or AlwaysOnSampler()
        self._processor = processor or IdentitySpanProcessor()
        self._utc_clock = utc_clock or SystemUtcClock()
        self._monotonic_clock = monotonic_clock or SystemMonotonicClock()
        self._dropped_counter = dropped_counter or BoundedDroppedTelemetryCounter()
        self._resource = sanitize_fields(resource, on_drop=self._drop)

    @property
    def dropped_telemetry_count(self) -> int:
        return self._dropped_counter.value

    def _drop(self) -> None:
        self._dropped_counter.increment()

    def span(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        links: Sequence[SpanLinkV1] = (),
    ) -> Span | _NonRecordingSpan:
        sanitized_attributes = sanitize_fields(attributes, on_drop=self._drop)
        parent_context = current_trace_context()
        try:
            sanitized_name, redacted = redact_sensitive_text(name)
        except TypeError:
            self._drop()
            return _NonRecordingSpan(parent_context=parent_context)
        if redacted:
            self._drop()
        try:
            sampled = bool(
                self._sampler.should_sample(
                    parent_context=parent_context,
                    name=sanitized_name,
                    attributes=sanitized_attributes,
                )
            )
        except Exception:
            self._drop()
            sampled = False
        try:
            trace_id = (
                self._id_generator.new_trace_id()
                if parent_context is None
                else parent_context.trace_id
            )
            context = TraceContextV1(
                trace_id=trace_id,
                span_id=self._id_generator.new_span_id(),
                trace_flags="01" if sampled else "00",
                trace_state=(None if parent_context is None else parent_context.trace_state),
            )
            return Span(
                tracer=self,
                context=context,
                parent_context=parent_context,
                name=sanitized_name,
                attributes=sanitized_attributes,
                links=links,
                sampled=sampled,
            )
        except Exception:
            self._drop()
            return _NonRecordingSpan(parent_context=parent_context)
