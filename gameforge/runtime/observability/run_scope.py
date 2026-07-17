"""Storage-neutral authorization scope for telemetry Run correlations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from gameforge.contracts.observability import (
    MAX_QUERY_FILTER_ITEMS,
    LogPageV1,
    LogRecordV1,
    SpanDataV1,
)


TelemetryRunScopeMode = Literal["domainless_only", "run_allowlist"]


@dataclass(frozen=True, slots=True)
class RetainedTraceRunScope:
    trace_id: str
    run_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetainedLogPage:
    page: LogPageV1
    trace_scopes: tuple[RetainedTraceRunScope, ...]


def validate_telemetry_run_scope(
    mode: TelemetryRunScopeMode | None,
    allowed_run_ids: Sequence[str],
) -> tuple[TelemetryRunScopeMode | None, tuple[str, ...]]:
    """Return one unambiguous scope identity or reject a malformed selector."""

    canonical = tuple(sorted(set(allowed_run_ids)))
    if canonical != tuple(allowed_run_ids) or len(canonical) > MAX_QUERY_FILTER_ITEMS:
        raise ValueError("allowed_run_ids must be canonical and bounded")
    if any(not isinstance(value, str) or not 1 <= len(value) <= 512 for value in canonical):
        raise ValueError("allowed_run_ids must contain bounded strings")
    if mode is None:
        if canonical:
            raise ValueError("unscoped telemetry reads cannot include allowed Run ids")
        return None, ()
    if mode == "domainless_only":
        if canonical:
            raise ValueError("domainless telemetry scope cannot include allowed Run ids")
        return mode, ()
    if mode == "run_allowlist":
        if not canonical:
            raise ValueError("Run allowlist scope must include allowed Run ids")
        return mode, canonical
    raise ValueError("unknown telemetry Run scope mode")


def run_ids_are_in_scope(
    item_run_ids: Sequence[str],
    *,
    mode: TelemetryRunScopeMode | None,
    allowed_run_ids: tuple[str, ...],
) -> bool:
    """Test a complete item's Run membership without trimming the item."""

    if mode is None:
        return True
    exact = set(item_run_ids)
    if mode == "domainless_only":
        return not exact
    return exact <= set(allowed_run_ids)


def log_record_is_in_run_scope(
    record: LogRecordV1,
    *,
    mode: TelemetryRunScopeMode | None,
    allowed_run_ids: tuple[str, ...],
) -> bool:
    item_run_ids = tuple(
        sorted({value for value in (record.run_id, record.producer_run_id) if value is not None})
    )
    return run_ids_are_in_scope(
        item_run_ids,
        mode=mode,
        allowed_run_ids=allowed_run_ids,
    )


def span_is_in_run_scope(
    span: SpanDataV1,
    *,
    mode: TelemetryRunScopeMode | None,
    allowed_run_ids: tuple[str, ...],
) -> bool:
    from gameforge.contracts.observability import span_run_ids

    return run_ids_are_in_scope(
        span_run_ids(span),
        mode=mode,
        allowed_run_ids=allowed_run_ids,
    )
