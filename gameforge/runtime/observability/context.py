"""W3C trace propagation and task-local current context."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic import ValidationError

from gameforge.contracts.jobs import RunDispatchTraceCarrierV1
from gameforge.contracts.observability import TraceContextV1


_TRACEPARENT = re.compile(
    r"^(?!ff-)[0-9a-f]{2}-(?!0{32})([0-9a-f]{32})-(?!0{16})([0-9a-f]{16})-([0-9a-f]{2})$"
)
_SIMPLE_STATE_KEY = re.compile(r"^[a-z][a-z0-9_\-*/]{0,255}$")
_TENANT_STATE_KEY = re.compile(r"^[a-z0-9][a-z0-9_\-*/]{0,240}@[a-z][a-z0-9_\-*/]{0,13}$")
_STATE_VALUE = re.compile(r"^[\x20-\x2b\x2d-\x3c\x3e-\x7e]+$")
_CURRENT_TRACE_CONTEXT: ContextVar[TraceContextV1 | None] = ContextVar(
    "gameforge_current_trace_context",
    default=None,
)


def _revalidate_context(context: TraceContextV1) -> TraceContextV1:
    return TraceContextV1.model_validate(context.model_dump(mode="json"))


def _valid_trace_state(value: str | None) -> bool:
    if value is None:
        return True
    if not value or len(value) > 512:
        return False
    members = value.split(",")
    if len(members) > 32:
        return False
    keys: set[str] = set()
    for raw_member in members:
        member = raw_member.strip(" \t")
        if not member or len(member) > 256 or "=" not in member:
            return False
        key, state_value = member.split("=", 1)
        if (
            (_SIMPLE_STATE_KEY.fullmatch(key) is None and _TENANT_STATE_KEY.fullmatch(key) is None)
            or _STATE_VALUE.fullmatch(state_value) is None
            or len(state_value) > 256
            or key in keys
        ):
            return False
        keys.add(key)
    return True


class TraceCarrier:
    """Translate the bounded persisted carrier to and from W3C trace context."""

    @staticmethod
    def inject(context: TraceContextV1) -> RunDispatchTraceCarrierV1:
        parsed = _revalidate_context(context)
        return RunDispatchTraceCarrierV1(
            traceparent=(f"00-{parsed.trace_id}-{parsed.span_id}-{parsed.trace_flags}"),
            tracestate=(parsed.trace_state if _valid_trace_state(parsed.trace_state) else None),
        )

    @staticmethod
    def extract(
        carrier: RunDispatchTraceCarrierV1 | Mapping[str, Any],
    ) -> TraceContextV1 | None:
        try:
            if isinstance(carrier, RunDispatchTraceCarrierV1):
                parsed_carrier = carrier
            elif isinstance(carrier, Mapping):
                normalized: dict[str, Any] = {}
                for key, value in carrier.items():
                    normalized_key = key.lower() if isinstance(key, str) else ""
                    if normalized_key not in {"traceparent", "tracestate"}:
                        continue
                    if normalized_key in normalized:
                        return None
                    normalized[normalized_key] = value
                parsed_carrier = RunDispatchTraceCarrierV1.model_validate(normalized)
            else:
                return None
            match = _TRACEPARENT.fullmatch(parsed_carrier.traceparent)
            if match is None or not _valid_trace_state(parsed_carrier.tracestate):
                return None
            return TraceContextV1(
                trace_id=match.group(1),
                span_id=match.group(2),
                trace_flags=match.group(3),
                trace_state=parsed_carrier.tracestate,
            )
        except (TypeError, ValueError, ValidationError):
            return None


def current_trace_context() -> TraceContextV1 | None:
    return _CURRENT_TRACE_CONTEXT.get()


@contextmanager
def use_trace_context(context: TraceContextV1) -> Iterator[TraceContextV1]:
    parsed = _revalidate_context(context)
    token = _CURRENT_TRACE_CONTEXT.set(parsed)
    try:
        yield parsed
    finally:
        _CURRENT_TRACE_CONTEXT.reset(token)
