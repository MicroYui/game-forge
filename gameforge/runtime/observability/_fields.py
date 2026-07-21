"""Shared bounded-field handling for runtime telemetry ingestion and export."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import JsonValue

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.observability import (
    MAX_ATTRIBUTE_COUNT,
    MAX_TELEMETRY_ARRAY_ITEMS,
    MAX_TELEMETRY_PAYLOAD_BYTES,
    MAX_TELEMETRY_STRING_BYTES,
    SpanDataV1,
    SpanErrorV1,
    SpanEventV1,
    SpanLinkV1,
)


DropCallback = Callable[[], None]
_TELEMETRY_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_KEY_SEPARATOR_TRANSLATION = str.maketrans("", "", "-._")
_SENSITIVE_KEY_MARKERS = (
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "credential",
    "id_token",
    "password",
    "prompt",
    "prompt_text",
    "raw_prompt",
    "raw_response",
    "refresh_token",
    "rendered_prompt",
    "response_body",
    "secret",
    "session_token",
    "system_prompt",
    "user_prompt",
)
_COMPACT_SENSITIVE_KEY_MARKERS = tuple(
    marker.casefold().translate(_KEY_SEPARATOR_TRANSLATION) for marker in _SENSITIVE_KEY_MARKERS
)
_AUTHORIZATION_VALUE = re.compile(r"(?i)(authorization\s*[:=]\s*)?(?:bearer|basic)\s+[^\s,;]+")
_NAMED_SECRET_MARKER = re.compile(
    r"(?i)\b(?:access[-._ ]?token|api[-._ ]?key|client[-._ ]?secret|"
    r"credential|id[-._ ]?token|password|refresh[-._ ]?token|secret|session[-._ ]?token)"
    r"\s*[\"']?\s*[:=]"
)
_GENERIC_AUTHORIZATION_MARKER = re.compile(
    r"(?i)\bauthorization\s*[\"']?\s*[:=](?!\s*\[REDACTED\])"
)
_CONTENT_BLOB_MARKER = re.compile(
    r"(?i)\b(?:prompt|prompt[-._ ]?text|raw[-._ ]?prompt|raw[-._ ]?response|"
    r"rendered[-._ ]?prompt|response[-._ ]?body|system[-._ ]?prompt|user[-._ ]?prompt)"
    r"\s*[\"']?\s*[:=]"
)


def is_sensitive_key(key: str) -> bool:
    compact = key.casefold().translate(_KEY_SEPARATOR_TRANSLATION)
    return compact in {"prompt", "response", "secret"} or any(
        marker in compact for marker in _COMPACT_SENSITIVE_KEY_MARKERS
    )


def redact_sensitive_text(value: str) -> tuple[str, bool]:
    redacted = _AUTHORIZATION_VALUE.sub("Authorization: [REDACTED]", value)
    if (
        _CONTENT_BLOB_MARKER.search(redacted)
        or _NAMED_SECRET_MARKER.search(redacted)
        or _GENERIC_AUTHORIZATION_MARKER.search(redacted)
    ):
        return "[REDACTED]", True
    return redacted, redacted != value


def sanitize_telemetry_value(
    value: Any,
    *,
    max_string_bytes: int = MAX_TELEMETRY_STRING_BYTES,
    max_array_items: int = MAX_TELEMETRY_ARRAY_ITEMS,
) -> tuple[JsonValue, bool]:
    if isinstance(max_string_bytes, bool) or not isinstance(max_string_bytes, int):
        raise ValueError("telemetry string byte limit must be a positive integer")
    if max_string_bytes < 1:
        raise ValueError("telemetry string byte limit must be a positive integer")
    if isinstance(max_array_items, bool) or not isinstance(max_array_items, int):
        raise ValueError("telemetry array item limit must be a positive integer")
    if max_array_items < 1:
        raise ValueError("telemetry array item limit must be a positive integer")
    if value is None or isinstance(value, (bool, int)):
        return value, False
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("telemetry numbers must be finite")
        return value, False
    if isinstance(value, str):
        if len(value.encode("utf-8")) > max_string_bytes:
            raise ValueError("telemetry string exceeds the byte limit")
        redacted, changed = redact_sensitive_text(value)
        if len(redacted.encode("utf-8")) > max_string_bytes:
            raise ValueError("redacted telemetry string exceeds the byte limit")
        return redacted, changed
    if isinstance(value, (tuple, list)):
        if len(value) > max_array_items:
            raise ValueError("telemetry array exceeds the item limit")
        normalized: list[JsonValue] = []
        redacted = False
        for item in value:
            if isinstance(item, (tuple, list, Mapping)):
                raise ValueError("telemetry arrays may contain primitives only")
            normalized_item, item_redacted = sanitize_telemetry_value(
                item,
                max_string_bytes=max_string_bytes,
                max_array_items=max_array_items,
            )
            normalized.append(normalized_item)
            redacted = redacted or item_redacted
        return normalized, redacted
    raise ValueError("telemetry values may contain only primitives or bounded primitive arrays")


def _normalize_value(value: Any, *, on_redact: DropCallback) -> JsonValue:
    normalized, redacted = sanitize_telemetry_value(value)
    if redacted:
        on_redact()
    return normalized


def add_field(
    target: dict[str, JsonValue],
    key: Any,
    value: Any,
    *,
    on_drop: DropCallback,
    max_items: int = MAX_ATTRIBUTE_COUNT,
) -> bool:
    try:
        if not isinstance(key, str) or _TELEMETRY_KEY.fullmatch(key) is None:
            raise ValueError("telemetry key is invalid")
        if is_sensitive_key(key):
            raise ValueError("telemetry key is sensitive")
        if key not in target and len(target) >= max_items:
            raise ValueError("telemetry field count exceeds the limit")
        candidate = {**target, key: _normalize_value(value, on_redact=on_drop)}
        candidate = {field: candidate[field] for field in sorted(candidate)}
        if len(canonical_json(candidate).encode("utf-8")) > MAX_TELEMETRY_PAYLOAD_BYTES:
            raise ValueError("telemetry payload exceeds the byte limit")
    except (TypeError, ValueError):
        on_drop()
        return False
    target.clear()
    target.update(candidate)
    return True


def sanitize_fields(
    fields: Mapping[Any, Any] | None,
    *,
    on_drop: DropCallback,
    max_items: int = MAX_ATTRIBUTE_COUNT,
) -> dict[str, JsonValue]:
    if fields is None:
        return {}
    if not isinstance(fields, Mapping) or len(fields) > max_items:
        on_drop()
        return {}
    string_keys = sorted(key for key in fields if isinstance(key, str))
    for key in fields:
        if not isinstance(key, str):
            on_drop()
    sanitized: dict[str, JsonValue] = {}
    for key in string_keys:
        add_field(sanitized, key, fields[key], on_drop=on_drop, max_items=max_items)
    return sanitized


def span_contains_sensitive_fields(span: SpanDataV1) -> bool:
    field_sets = [span.attributes, span.resource]
    field_sets.extend(link.attributes for link in span.links)
    field_sets.extend(event.attributes for event in span.events)
    return any(is_sensitive_key(key) for fields in field_sets for key in fields)


def redact_span_values(span: SpanDataV1) -> SpanDataV1:
    def redact_fields(fields: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            key: "[REDACTED]" if is_sensitive_key(key) else sanitize_telemetry_value(value)[0]
            for key, value in sorted(fields.items())
        }

    error = span.error
    if error is not None:
        error = SpanErrorV1(
            error_type=error.error_type,
            message=redact_sensitive_text(error.message)[0],
            stack_fingerprint=error.stack_fingerprint,
        )
    return SpanDataV1(
        trace_id=span.trace_id,
        span_id=span.span_id,
        parent_span_id=span.parent_span_id,
        name=redact_sensitive_text(span.name)[0],
        attributes=redact_fields(span.attributes),
        links=tuple(
            SpanLinkV1(context=link.context, attributes=redact_fields(link.attributes))
            for link in span.links
        ),
        events=tuple(
            SpanEventV1(
                name=redact_sensitive_text(event.name)[0],
                occurred_at=event.occurred_at,
                attributes=redact_fields(event.attributes),
            )
            for event in span.events
        ),
        status=span.status,
        error=error,
        resource=redact_fields(span.resource),
        started_at=span.started_at,
        ended_at=span.ended_at,
        duration_ns=span.duration_ns,
    )
