"""Versioned, bounded observability and run-cost read endpoints."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ValidationError

from gameforge.apps.api.dependencies import require_actor
from gameforge.contracts.cost import RunCostViewV1
from gameforge.contracts.errors import QueryTooBroad, RequestSchemaInvalid
from gameforge.contracts.identity import ActorContext
from gameforge.contracts.observability import (
    MAX_QUERY_DESCRIPTOR_REFS,
    MAX_QUERY_FILTER_ITEMS,
    LogPageV1,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryV1,
    MetricLabelMatcherV1,
    MetricPageV1,
    SpanPageV1,
    TraceSummaryPageV1,
    TraceSummaryV1,
)
from gameforge.platform.read_models.observability import (
    ObservabilityReadService,
)


_MAX_STRUCTURED_QUERY_BYTES = 32 * 1024
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
        trace_id: str,
        actor: ActorContext = Depends(require_actor),
    ) -> TraceSummaryV1:
        return service.get_trace(principal=actor.principal, trace_id=trace_id)

    @router.get("/traces/{trace_id}/spans", response_model=SpanPageV1)
    def get_trace_spans(
        trace_id: str,
        cursor: str | None = None,
        limit: int = 100,
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
        run_id: str,
        cursor: str | None = None,
        limit: int = 100,
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
        services: Annotated[list[str] | None, Query()] = None,
        levels: Annotated[list[str] | None, Query()] = None,
        event_names: Annotated[list[str] | None, Query()] = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        producer_run_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
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
        descriptor_refs: str,
        start_utc: datetime,
        end_utc: datetime,
        resolution_s: int,
        max_points: int,
        series_limit: int,
        label_matchers: str = "[]",
        cursor: str | None = None,
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
        run_id: str,
        cursor: str | None = None,
        limit: int = 100,
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
