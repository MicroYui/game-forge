"""Versioned, bounded observability and run-cost read endpoints."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Annotated, Literal, TypeVar

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, StringConstraints, ValidationError, WithJsonSchema

from gameforge.apps.api.dependencies import require_actor
from gameforge.contracts.cost import MAX_COST_USAGE_PAGE_SIZE, RunCostViewV1
from gameforge.contracts.errors import QueryTooBroad, RequestSchemaInvalid
from gameforge.contracts.identity import ActorContext
from gameforge.contracts.observability import (
    MAX_QUERY_DESCRIPTOR_REFS,
    MAX_QUERY_FILTER_ITEMS,
    MAX_QUERY_PAGE_SIZE,
    MAX_QUERY_POINTS,
    MAX_QUERY_RESOLUTION_S,
    MAX_QUERY_SERIES,
    LogPageV1,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryV1,
    MetricLabelMatcherV1,
    MetricPageV1,
    SpanPageV1,
    SpanId,
    NonEmptyStr,
    TraceId,
    TraceSummaryPageV1,
    TraceSummaryV1,
)
from gameforge.platform.read_models.observability import (
    ObservabilityReadService,
)


_MAX_STRUCTURED_QUERY_BYTES = 32 * 1024


def _bounded_string_schema(maximum: int) -> WithJsonSchema:
    return WithJsonSchema({"type": "string", "minLength": 1, "maxLength": maximum})


def _bounded_integer_schema(maximum: int) -> WithJsonSchema:
    return WithJsonSchema({"type": "integer", "minimum": 1, "maximum": maximum})


def _optional_array_schema(items: dict[str, object], maximum: int) -> WithJsonSchema:
    return WithJsonSchema(
        {"anyOf": [{"type": "array", "items": items, "maxItems": maximum}, {"type": "null"}]}
    )


_BoundedCursor = Annotated[str, _bounded_string_schema(4096)]
_TraceId = Annotated[
    TraceId,
    StringConstraints(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"),
]
_SpanId = Annotated[
    SpanId,
    StringConstraints(min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$"),
]
_PageLimit = Annotated[int, _bounded_integer_schema(MAX_QUERY_PAGE_SIZE)]
_CostPageLimit = Annotated[int, _bounded_integer_schema(MAX_COST_USAGE_PAGE_SIZE)]
_StructuredQuery = Annotated[str, _bounded_string_schema(_MAX_STRUCTURED_QUERY_BYTES)]
_LogFilters = Annotated[
    list[NonEmptyStr] | None,
    Query(),
    _optional_array_schema(
        {"type": "string", "minLength": 1, "maxLength": 512}, MAX_QUERY_FILTER_ITEMS
    ),
]
_LogLevels = Annotated[
    list[Literal["debug", "info", "warning", "error", "critical"]] | None,
    Query(),
    _optional_array_schema(
        {"type": "string", "enum": ["debug", "info", "warning", "error", "critical"]}, 5
    ),
]
_MetricResolution = Annotated[int, _bounded_integer_schema(MAX_QUERY_RESOLUTION_S)]
_MetricPointLimit = Annotated[int, _bounded_integer_schema(MAX_QUERY_POINTS)]
_MetricSeriesLimit = Annotated[int, _bounded_integer_schema(MAX_QUERY_SERIES)]
T = TypeVar("T", bound=BaseModel)


def _structured_models(
    raw: str,
    *,
    model_type: type[T],
    maximum: int,
    label: str,
    non_empty: bool = False,
) -> tuple[T, ...]:
    if not isinstance(raw, str):
        raise RequestSchemaInvalid(f"{label} must be canonical JSON")
    if len(raw.encode("utf-8")) > _MAX_STRUCTURED_QUERY_BYTES:
        raise QueryTooBroad(f"{label} exceed the query byte cap")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RequestSchemaInvalid(f"{label} must be a JSON array") from exc
    if not isinstance(payload, list):
        raise RequestSchemaInvalid(f"{label} must be a JSON array")
    if non_empty and not payload:
        raise RequestSchemaInvalid(f"{label} must be non-empty")
    if len(payload) > maximum:
        raise QueryTooBroad(f"{label} exceed the service cap")
    try:
        return tuple(model_type.model_validate(item) for item in payload)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequestSchemaInvalid(f"{label} contain an invalid item") from exc


def observability_router(service: ObservabilityReadService) -> APIRouter:
    """Create the router with an explicit read service; composition remains in apps."""

    if not isinstance(service, ObservabilityReadService):
        raise TypeError("service must be an ObservabilityReadService")
    router = APIRouter(prefix="/api/v1", tags=["observability"])

    @router.get("/traces/{trace_id}", response_model=TraceSummaryV1)
    def get_trace(
        trace_id: _TraceId,
        actor: ActorContext = Depends(require_actor),
    ) -> TraceSummaryV1:
        return service.get_trace(principal=actor.principal, trace_id=trace_id)

    @router.get("/traces/{trace_id}/spans", response_model=SpanPageV1)
    def get_trace_spans(
        trace_id: _TraceId,
        cursor: _BoundedCursor | None = None,
        limit: _PageLimit = 100,
        actor: ActorContext = Depends(require_actor),
    ) -> SpanPageV1:
        return service.get_trace_spans(
            principal=actor.principal,
            trace_id=trace_id,
            cursor=cursor,
            limit=limit,
        )

    @router.get("/runs/{run_id}/traces", response_model=TraceSummaryPageV1)
    def list_run_traces(
        run_id: NonEmptyStr,
        cursor: _BoundedCursor | None = None,
        limit: _PageLimit = 100,
        actor: ActorContext = Depends(require_actor),
    ) -> TraceSummaryPageV1:
        return service.list_run_traces(
            principal=actor.principal,
            run_id=run_id,
            cursor=cursor,
            limit=limit,
        )

    @router.get("/logs/query", response_model=LogPageV1)
    def query_logs(
        start_utc: datetime,
        end_utc: datetime,
        services: _LogFilters = None,
        levels: _LogLevels = None,
        event_names: _LogFilters = None,
        run_id: NonEmptyStr | None = None,
        trace_id: _TraceId | None = None,
        span_id: _SpanId | None = None,
        producer_run_id: NonEmptyStr | None = None,
        cursor: _BoundedCursor | None = None,
        limit: _PageLimit = 100,
        actor: ActorContext = Depends(require_actor),
    ) -> LogPageV1:
        return service.query_logs(
            principal=actor.principal,
            start_utc=start_utc,
            end_utc=end_utc,
            services=tuple(services or ()),
            levels=tuple(levels or ()),
            event_names=tuple(event_names or ()),
            run_id=run_id,
            trace_id=trace_id,
            span_id=span_id,
            producer_run_id=producer_run_id,
            cursor=cursor,
            limit=limit,
        )

    @router.get(
        "/metrics/descriptors",
        response_model=MetricDescriptorRegistryV1,
    )
    def get_metric_descriptors(
        actor: ActorContext = Depends(require_actor),
    ) -> MetricDescriptorRegistryV1:
        return service.get_metric_descriptors(principal=actor.principal)

    @router.get("/metrics/query", response_model=MetricPageV1)
    def query_metrics(
        descriptor_refs: _StructuredQuery,
        start_utc: datetime,
        end_utc: datetime,
        resolution_s: _MetricResolution,
        max_points: _MetricPointLimit,
        series_limit: _MetricSeriesLimit,
        label_matchers: _StructuredQuery = "[]",
        cursor: _BoundedCursor | None = None,
        actor: ActorContext = Depends(require_actor),
    ) -> MetricPageV1:
        exact_refs = _structured_models(
            descriptor_refs,
            model_type=MetricDescriptorRefV1,
            maximum=MAX_QUERY_DESCRIPTOR_REFS,
            label="descriptor_refs",
            non_empty=True,
        )
        exact_matchers = _structured_models(
            label_matchers,
            model_type=MetricLabelMatcherV1,
            maximum=MAX_QUERY_FILTER_ITEMS,
            label="label_matchers",
        )
        return service.query_metrics(
            principal=actor.principal,
            descriptor_refs=exact_refs,
            start_utc=start_utc,
            end_utc=end_utc,
            resolution_s=resolution_s,
            label_matchers=exact_matchers,
            max_points=max_points,
            cursor=cursor,
            series_limit=series_limit,
        )

    @router.get("/cost/{run_id}", response_model=RunCostViewV1)
    def get_run_cost(
        run_id: NonEmptyStr,
        cursor: _BoundedCursor | None = None,
        limit: _CostPageLimit = 100,
        actor: ActorContext = Depends(require_actor),
    ) -> RunCostViewV1:
        return service.get_run_cost(
            principal=actor.principal,
            run_id=run_id,
            cursor=cursor,
            limit=limit,
        )

    return router


__all__ = ["observability_router"]
