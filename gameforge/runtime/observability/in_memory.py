"""Bounded in-memory trace, log, and metric query store."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import (
    CursorExpired,
    IntegrityViolation,
    QueryTooBroad,
)
from gameforge.contracts.observability import (
    LogPageV1,
    LogQueryV1,
    LogRecordV1,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryRefV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricPageV1,
    MetricPointV1,
    MetricQueryV1,
    MetricSeriesV1,
    SpanDataV1,
    SpanPageV1,
    SpanViewV1,
    TraceQueryV1,
    TraceSummaryPageV1,
    TraceSummaryV1,
)
from gameforge.contracts.storage import UtcClock
from gameforge.runtime.observability._fields import redact_span_values
from gameforge.runtime.observability.cursor import OpaqueCursorCodec
from gameforge.runtime.observability.metrics import (
    aggregate_metric_points,
    validate_metric_query_shape,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


def _detached(value: ModelT) -> ModelT:
    return cast(ModelT, type(value).model_validate_json(value.model_dump_json()))


@dataclass(frozen=True, slots=True)
class TelemetryStoreLimits:
    max_time_range_s: int = 7 * 24 * 60 * 60
    max_trace_page_size: int = 500
    max_span_page_size: int = 1000
    max_log_page_size: int = 1000
    max_metric_series: int = 500
    max_metric_points: int = 10_000
    max_response_bytes: int = 4 * 1024 * 1024
    cursor_ttl_s: int = 15 * 60
    max_stored_spans: int = 10_000
    max_stored_logs: int = 50_000
    max_stored_metric_points: int = 100_000
    max_stored_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for item in fields(self):
            field_name = item.name
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class _QuerySnapshot:
    kind: str
    query_hash: str
    authz_fingerprint: str
    principal_binding: str | None
    items: tuple[Any, ...]
    expires_at: datetime


def _model_wire(value: Any) -> str:
    return canonical_json(value.model_dump(mode="json"))


def _ref_key(ref: MetricDescriptorRefV1) -> tuple[str, int, str]:
    return (ref.metric_name, ref.descriptor_version, ref.descriptor_digest)


def _labels_key(labels: dict[str, str]) -> str:
    return canonical_json(labels)


class InMemoryTelemetryStore:
    """Deterministic test adapter implementing all three telemetry read contracts."""

    def __init__(
        self,
        *,
        clock: UtcClock,
        signing_key: bytes,
        limits: TelemetryStoreLimits | None = None,
    ) -> None:
        self._clock = clock
        self._limits = limits or TelemetryStoreLimits()
        self._cursor_codec = OpaqueCursorCodec(signing_key=signing_key, clock=clock)
        self._spans: dict[tuple[str, str], SpanDataV1] = {}
        self._logs: dict[str, LogRecordV1] = {}
        self._points: dict[str, MetricPointV1] = {}
        self._registries: dict[int, MetricDescriptorRegistryV1] = {}
        self._descriptors: dict[tuple[str, int, str], MetricDescriptorV1] = {}
        self._descriptor_versions: dict[tuple[str, int], MetricDescriptorV1] = {}
        self._descriptor_registries: dict[tuple[str, int, str], set[int]] = defaultdict(set)
        self._series_by_descriptor: dict[tuple[str, int, str], set[str]] = defaultdict(set)
        self._snapshots: dict[str, _QuerySnapshot] = {}
        self._snapshot_counter = 0
        self._metric_registry_ref: MetricDescriptorRegistryRefV1 | None = None
        self._stored_bytes = 0

    @property
    def metric_point_count(self) -> int:
        return len(self._points)

    @property
    def metric_registry_ref(self) -> MetricDescriptorRegistryRefV1:
        if self._metric_registry_ref is None:
            raise IntegrityViolation("no metric descriptor registry is frozen")
        return self._metric_registry_ref

    def get_metric_descriptor_registry(self) -> MetricDescriptorRegistryV1 | None:
        if self._metric_registry_ref is None:
            return None
        registry = self._registries.get(self._metric_registry_ref.registry_version)
        if (
            registry is None
            or registry.registry_digest != self._metric_registry_ref.registry_digest
        ):
            raise IntegrityViolation("active metric registry payload is unavailable")
        return _detached(registry)

    def register_metric_registry(self, registry: MetricDescriptorRegistryV1) -> None:
        registry = _detached(registry)
        key = registry.registry_version
        existing = self._registries.get(key)
        if existing is not None:
            if existing != registry:
                raise IntegrityViolation("metric registry version has conflicting payload")
            self._metric_registry_ref = registry.ref
            return
        for descriptor in registry.descriptors:
            descriptor_key = _ref_key(descriptor.ref)
            version_key = (descriptor.metric_name, descriptor.descriptor_version)
            versioned = self._descriptor_versions.get(version_key)
            if versioned is not None and versioned != descriptor:
                raise IntegrityViolation("metric descriptor version has conflicting payload")
            known = self._descriptors.get(descriptor_key)
            if known is not None and known != descriptor:
                raise IntegrityViolation("metric descriptor identity has conflicting payload")
        self._registries[key] = registry
        for descriptor in registry.descriptors:
            descriptor_key = _ref_key(descriptor.ref)
            self._descriptors[descriptor_key] = descriptor
            self._descriptor_versions[(descriptor.metric_name, descriptor.descriptor_version)] = (
                descriptor
            )
            self._descriptor_registries[descriptor_key].add(key)
        self._metric_registry_ref = registry.ref

    def put(self, span: SpanDataV1) -> None:
        span = redact_span_values(_detached(span))
        key = (span.trace_id, span.span_id)
        existing = self._spans.get(key)
        if existing is not None:
            if existing != span:
                raise IntegrityViolation("span identity has conflicting immutable payload")
            return
        size = self._reserve_record_capacity(
            kind="span",
            current_count=len(self._spans),
            max_count=self._limits.max_stored_spans,
            value=span,
        )
        self._spans[key] = span
        self._stored_bytes += size

    def get(self, trace_id: str, span_id: str) -> SpanDataV1 | None:
        span = self._spans.get((trace_id, span_id))
        return None if span is None else redact_span_values(_detached(span))

    def get_trace_summary(self, trace_id: str) -> TraceSummaryV1 | None:
        spans = [
            redact_span_values(_detached(span))
            for span in self._spans.values()
            if span.trace_id == trace_id
        ]
        return None if not spans else self._summarize_trace(trace_id, spans)

    def append(self, record: LogRecordV1) -> None:
        record = _detached(record)
        existing = self._logs.get(record.log_id)
        if existing is not None:
            if existing != record:
                raise IntegrityViolation("log identity has conflicting immutable payload")
            return
        size = self._reserve_record_capacity(
            kind="log",
            current_count=len(self._logs),
            max_count=self._limits.max_stored_logs,
            value=record,
        )
        self._logs[record.log_id] = record
        self._stored_bytes += size

    def record(self, point: MetricPointV1) -> None:
        point = _detached(point)
        existing = self._points.get(point.point_id)
        if existing is not None:
            if existing != point:
                raise IntegrityViolation("metric point identity has conflicting payload")
            return
        descriptor_key = _ref_key(point.descriptor)
        descriptor = self._descriptors.get(descriptor_key)
        if descriptor is None:
            raise IntegrityViolation("metric point references an unknown exact descriptor")
        if descriptor.metric_type != point.metric_type:
            raise IntegrityViolation("metric point type differs from exact descriptor")
        if set(point.labels) != set(descriptor.label_keys):
            raise IntegrityViolation("metric point label keys differ from exact descriptor")
        series_key = _labels_key(point.labels)
        known_series = self._series_by_descriptor[descriptor_key]
        is_new_series = series_key not in known_series
        if is_new_series and len(known_series) >= descriptor.series_limit:
            raise BufferError("metric descriptor series limit exceeded")
        if is_new_series:
            for registry_key in self._descriptor_registries[descriptor_key]:
                registry = self._registries[registry_key]
                total_series = sum(
                    len(self._series_by_descriptor[_ref_key(item.ref)])
                    for item in registry.descriptors
                )
                if total_series >= registry.global_series_limit:
                    raise BufferError("metric registry global series limit exceeded")
        size = self._reserve_record_capacity(
            kind="metric point",
            current_count=len(self._points),
            max_count=self._limits.max_stored_metric_points,
            value=point,
        )
        self._points[point.point_id] = point
        known_series.add(series_key)
        self._stored_bytes += size

    def _reserve_record_capacity(
        self,
        *,
        kind: str,
        current_count: int,
        max_count: int,
        value: BaseModel,
    ) -> int:
        if current_count >= max_count:
            raise BufferError(f"in-memory telemetry {kind} record capacity is exhausted")
        size = len(_model_wire(value).encode("utf-8"))
        if self._stored_bytes + size > self._limits.max_stored_bytes:
            raise BufferError("in-memory telemetry byte capacity is exhausted")
        return size

    def query_traces(
        self,
        query: TraceQueryV1,
        *,
        principal_binding: str | None = None,
    ) -> TraceSummaryPageV1:
        self._validate_range(query.time_range.start_utc, query.time_range.end_utc)
        if query.limit > self._limits.max_trace_page_size:
            raise QueryTooBroad("trace page limit exceeds service cap")
        query_hash, legacy_query_hash = self._query_hashes(query, principal_binding)
        authz = query.authz_fingerprint
        if query.cursor is None:
            grouped: dict[str, list[SpanDataV1]] = defaultdict(list)
            for stored_span in self._spans.values():
                span = redact_span_values(stored_span)
                if not (query.time_range.start_utc <= span.started_at < query.time_range.end_utc):
                    continue
                grouped[span.trace_id].append(span)
            summaries: list[TraceSummaryV1] = []
            for trace_id, spans in grouped.items():
                summary = self._summarize_trace(trace_id, spans)
                if query.run_id is not None and query.run_id not in summary.run_ids:
                    continue
                if query.service is not None and query.service not in summary.service_names:
                    continue
                if query.status is not None and query.status != summary.status:
                    continue
                summaries.append(summary)
            summaries.sort(key=lambda item: (item.started_at, item.trace_id))
            snapshot_id, offset = self._create_snapshot(
                "traces", query_hash, authz, principal_binding, tuple(summaries)
            )
        else:
            snapshot_id, offset = self._resume_snapshot(
                query.cursor,
                kind="traces",
                query_hash=query_hash,
                authz=authz,
                page_limit=query.limit,
                principal_binding=principal_binding,
                legacy_query_hash=legacy_query_hash,
            )
        snapshot = self._require_snapshot(snapshot_id, "traces", query_hash, authz)
        page_items = snapshot.items[offset : offset + query.limit]
        next_offset = offset + len(page_items)
        next_cursor = self._next_cursor(
            snapshot_id=snapshot_id,
            snapshot=snapshot,
            offset=next_offset,
            page_limit=query.limit,
        )
        page = TraceSummaryPageV1(
            items=page_items,
            next_cursor=next_cursor,
            coverage_start=query.time_range.start_utc,
            coverage_end=query.time_range.end_utc,
            truncated=next_cursor is not None,
        )
        self._check_response_size(page)
        return _detached(page)

    def page_run_traces(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authz_fingerprint: str,
        principal_binding: str | None = None,
    ) -> TraceSummaryPageV1:
        if limit <= 0 or limit > self._limits.max_trace_page_size:
            raise QueryTooBroad("trace page limit exceeds service cap")
        query_hash = canonical_sha256({"run_id": run_id})
        if cursor is None:
            grouped: dict[str, list[SpanDataV1]] = defaultdict(list)
            trace_ids = {
                span.trace_id
                for span in self._spans.values()
                if span.attributes.get("run_id") == run_id
            }
            for stored_span in self._spans.values():
                span = redact_span_values(_detached(stored_span))
                if span.trace_id in trace_ids:
                    grouped[span.trace_id].append(span)
            summaries = tuple(
                sorted(
                    (self._summarize_trace(trace_id, spans) for trace_id, spans in grouped.items()),
                    key=lambda item: (item.started_at, item.trace_id),
                )
            )
            snapshot_id, offset = self._create_snapshot(
                "run_traces",
                query_hash,
                authz_fingerprint,
                principal_binding,
                summaries,
            )
        else:
            snapshot_id, offset = self._resume_snapshot(
                cursor,
                kind="run_traces",
                query_hash=query_hash,
                authz=authz_fingerprint,
                page_limit=limit,
                principal_binding=principal_binding,
            )
        snapshot = self._require_snapshot(
            snapshot_id,
            "run_traces",
            query_hash,
            authz_fingerprint,
        )
        items = snapshot.items[offset : offset + limit]
        next_cursor = self._next_cursor(
            snapshot_id=snapshot_id,
            snapshot=snapshot,
            offset=offset + len(items),
            page_limit=limit,
        )
        start = min((item.started_at for item in snapshot.items), default=self._clock.now_utc())
        end = max(
            (item.ended_at or item.started_at for item in snapshot.items),
            default=start,
        )
        page = TraceSummaryPageV1(
            items=items,
            next_cursor=next_cursor,
            coverage_start=start,
            coverage_end=end,
            truncated=next_cursor is not None,
        )
        self._check_response_size(page)
        return _detached(page)

    def page_spans(
        self,
        trace_id: str,
        *,
        cursor: str | None,
        limit: int,
        authz_fingerprint: str,
        principal_binding: str | None = None,
    ) -> SpanPageV1:
        if limit <= 0 or limit > self._limits.max_span_page_size:
            raise QueryTooBroad("span page limit exceeds service cap")
        query_hash = canonical_sha256({"trace_id": trace_id})
        if cursor is None:
            spans = sorted(
                (
                    redact_span_values(span)
                    for span in self._spans.values()
                    if span.trace_id == trace_id
                ),
                key=lambda item: (item.started_at, item.span_id),
            )
            views = tuple(SpanViewV1(span=span) for span in spans)
            snapshot_id, offset = self._create_snapshot(
                "spans", query_hash, authz_fingerprint, principal_binding, views
            )
        else:
            snapshot_id, offset = self._resume_snapshot(
                cursor,
                kind="spans",
                query_hash=query_hash,
                authz=authz_fingerprint,
                page_limit=limit,
                principal_binding=principal_binding,
            )
        snapshot = self._require_snapshot(snapshot_id, "spans", query_hash, authz_fingerprint)
        items = snapshot.items[offset : offset + limit]
        next_offset = offset + len(items)
        next_cursor = self._next_cursor(
            snapshot_id=snapshot_id,
            snapshot=snapshot,
            offset=next_offset,
            page_limit=limit,
        )
        page = SpanPageV1(
            trace_id=trace_id,
            items=items,
            next_cursor=next_cursor,
            truncated=next_cursor is not None,
        )
        self._check_response_size(page)
        return _detached(page)

    def query_logs(
        self,
        query: LogQueryV1,
        *,
        principal_binding: str | None = None,
    ) -> LogPageV1:
        self._validate_range(query.time_range.start_utc, query.time_range.end_utc)
        if query.limit > self._limits.max_log_page_size:
            raise QueryTooBroad("log page limit exceeds service cap")
        query_hash, legacy_query_hash = self._query_hashes(query, principal_binding)
        authz = query.authz_fingerprint
        if query.cursor is None:
            items = tuple(
                sorted(
                    (
                        record
                        for record in self._logs.values()
                        if query.time_range.start_utc <= record.ts_utc < query.time_range.end_utc
                        and (not query.services or record.service in query.services)
                        and (not query.levels or record.level in query.levels)
                        and (not query.event_names or record.event_name in query.event_names)
                        and (query.run_id is None or record.run_id == query.run_id)
                        and (query.trace_id is None or record.trace_id == query.trace_id)
                        and (query.span_id is None or record.span_id == query.span_id)
                        and (
                            query.producer_run_id is None
                            or record.producer_run_id == query.producer_run_id
                        )
                    ),
                    key=lambda item: (item.ts_utc, item.log_id),
                )
            )
            snapshot_id, offset = self._create_snapshot(
                "logs", query_hash, authz, principal_binding, items
            )
        else:
            snapshot_id, offset = self._resume_snapshot(
                query.cursor,
                kind="logs",
                query_hash=query_hash,
                authz=authz,
                page_limit=query.limit,
                principal_binding=principal_binding,
                legacy_query_hash=legacy_query_hash,
            )
        snapshot = self._require_snapshot(snapshot_id, "logs", query_hash, authz)
        page_items = snapshot.items[offset : offset + query.limit]
        next_offset = offset + len(page_items)
        next_cursor = self._next_cursor(
            snapshot_id=snapshot_id,
            snapshot=snapshot,
            offset=next_offset,
            page_limit=query.limit,
        )
        page = LogPageV1(
            items=page_items,
            next_cursor=next_cursor,
            coverage_start=query.time_range.start_utc,
            coverage_end=query.time_range.end_utc,
            truncated=next_cursor is not None,
        )
        self._check_response_size(page)
        return _detached(page)

    def query_metrics(
        self,
        query: MetricQueryV1,
        *,
        principal_binding: str | None = None,
    ) -> MetricPageV1:
        self._validate_metric_query(query)
        query_hash, legacy_query_hash = self._query_hashes(query, principal_binding)
        authz = query.authz_fingerprint
        if query.cursor is None:
            series = self._aggregate_metrics(query)
            snapshot_id, offset = self._create_snapshot(
                "metrics", query_hash, authz, principal_binding, tuple(series)
            )
        else:
            snapshot_id, offset = self._resume_snapshot(
                query.cursor,
                kind="metrics",
                query_hash=query_hash,
                authz=authz,
                page_limit=query.series_limit,
                principal_binding=principal_binding,
                legacy_query_hash=legacy_query_hash,
            )
        snapshot = self._require_snapshot(snapshot_id, "metrics", query_hash, authz)
        selected: list[MetricSeriesV1] = []
        point_count = 0
        index = offset
        while index < len(snapshot.items) and len(selected) < query.series_limit:
            candidate = snapshot.items[index]
            candidate_points = self._series_point_count(candidate)
            if candidate_points > query.max_points:
                raise QueryTooBroad("one metric series exceeds max_points")
            if selected and point_count + candidate_points > query.max_points:
                break
            selected.append(candidate)
            point_count += candidate_points
            index += 1
        next_cursor = self._next_cursor(
            snapshot_id=snapshot_id,
            snapshot=snapshot,
            offset=index,
            page_limit=query.series_limit,
        )
        page = MetricPageV1(
            series=tuple(selected),
            next_cursor=next_cursor,
            coverage_start=query.time_range.start_utc,
            coverage_end=query.time_range.end_utc,
            effective_resolution_s=query.resolution_s,
            truncated=next_cursor is not None,
        )
        self._check_response_size(page)
        return _detached(page)

    def query(self, query: MetricQueryV1) -> MetricPageV1:
        """Backward-compatible alias; new consumers use the typed method name."""

        return self.query_metrics(query)

    def resolve_metric_descriptors(
        self,
        refs: tuple[MetricDescriptorRefV1, ...] | list[MetricDescriptorRefV1],
    ) -> tuple[MetricDescriptorV1, ...]:
        result: list[MetricDescriptorV1] = []
        for ref in refs:
            descriptor = self._descriptors.get(_ref_key(ref))
            if descriptor is None:
                raise IntegrityViolation("metric query references an unknown descriptor")
            result.append(_detached(descriptor))
        return tuple(result)

    def _aggregate_metrics(self, query: MetricQueryV1) -> list[MetricSeriesV1]:
        return list(aggregate_metric_points(self._points.values(), self._descriptors, query))

    def _validate_metric_query(self, query: MetricQueryV1) -> None:
        self._validate_range(query.time_range.start_utc, query.time_range.end_utc)
        if query.max_points > self._limits.max_metric_points:
            raise QueryTooBroad("metric max_points exceeds service cap")
        if query.series_limit > self._limits.max_metric_series:
            raise QueryTooBroad("metric series_limit exceeds service cap")
        validate_metric_query_shape(query, self._descriptors)

    @staticmethod
    def _series_point_count(series: MetricSeriesV1) -> int:
        return len(series.scalar_points or ()) + len(series.histogram_points or ())

    @staticmethod
    def _summarize_trace(trace_id: str, spans: list[SpanDataV1]) -> TraceSummaryV1:
        ordered = sorted(spans, key=lambda item: (item.started_at, item.span_id))
        roots = [item for item in ordered if item.parent_span_id is None]
        root = roots[0] if len(roots) == 1 else None
        run_ids = tuple(
            sorted(
                {
                    value
                    for span in ordered
                    if isinstance((value := span.attributes.get("run_id")), str) and value
                }
            )
        )
        services = tuple(
            sorted(
                {
                    value
                    for span in ordered
                    if isinstance((value := span.resource.get("service.name")), str) and value
                }
            )
        )
        status = (
            "error"
            if any(item.status == "error" for item in ordered)
            else "ok"
            if all(item.status == "ok" for item in ordered)
            else "unset"
        )
        return TraceSummaryV1(
            trace_id=trace_id,
            root_span_id=root.span_id if root else None,
            run_ids=run_ids,
            started_at=min(item.started_at for item in ordered),
            ended_at=max(item.ended_at for item in ordered),
            duration_ns=root.duration_ns if root else None,
            status=status,
            span_count=len(ordered),
            service_names=services,
            truncated=False,
        )

    def _validate_range(self, start: datetime, end: datetime) -> None:
        if (end - start).total_seconds() > self._limits.max_time_range_s:
            raise QueryTooBroad("telemetry time range exceeds service cap")

    @staticmethod
    def _query_hash(query: Any, *, exclude: set[str]) -> str:
        return canonical_sha256(query.model_dump(mode="json", exclude=exclude))

    def _query_hashes(
        self,
        query: Any,
        principal_binding: str | None,
    ) -> tuple[str, str | None]:
        legacy = self._query_hash(query, exclude={"cursor"})
        if principal_binding is None:
            return legacy, None
        return self._query_hash(query, exclude={"cursor", "authz_fingerprint"}), legacy

    def _create_snapshot(
        self,
        kind: str,
        query_hash: str,
        authz: str,
        principal_binding: str | None,
        items: tuple[Any, ...],
    ) -> tuple[str, int]:
        self._cleanup_snapshots()
        self._snapshot_counter += 1
        snapshot_id = f"telemetry-snapshot-{self._snapshot_counter}"
        expires_at = self._clock.now_utc() + timedelta(seconds=self._limits.cursor_ttl_s)
        self._snapshots[snapshot_id] = _QuerySnapshot(
            kind=kind,
            query_hash=query_hash,
            authz_fingerprint=authz,
            principal_binding=principal_binding,
            items=items,
            expires_at=expires_at,
        )
        return snapshot_id, 0

    def _resume_snapshot(
        self,
        token: str,
        *,
        kind: str,
        query_hash: str,
        authz: str,
        page_limit: int,
        principal_binding: str | None,
        legacy_query_hash: str | None = None,
    ) -> tuple[str, int]:
        state = self._cursor_codec.verify(
            token,
            expected_kind=kind,
            expected_query_hash=query_hash,
            expected_authz_fingerprint=authz,
            expected_page_limit=page_limit,
            expected_principal_binding=principal_binding,
            expected_legacy_query_hash=legacy_query_hash,
        )
        self._require_snapshot(
            state.snapshot_id,
            kind,
            state.query_hash,
            state.authz_fingerprint,
        )
        return state.snapshot_id, state.offset

    def _require_snapshot(
        self,
        snapshot_id: str,
        kind: str,
        query_hash: str,
        authz: str,
        alternate_query_hash: str | None = None,
    ) -> _QuerySnapshot:
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None or self._clock.now_utc() >= snapshot.expires_at:
            self._snapshots.pop(snapshot_id, None)
            raise CursorExpired("telemetry query snapshot is no longer retained")
        if (
            snapshot.kind != kind
            or snapshot.query_hash not in {query_hash, alternate_query_hash}
            or snapshot.authz_fingerprint != authz
        ):
            raise IntegrityViolation("telemetry snapshot binding is inconsistent")
        return snapshot

    def _next_cursor(
        self,
        *,
        snapshot_id: str,
        snapshot: _QuerySnapshot,
        offset: int,
        page_limit: int,
    ) -> str | None:
        if offset >= len(snapshot.items):
            return None
        return self._cursor_codec.issue(
            kind=snapshot.kind,
            snapshot_id=snapshot_id,
            query_hash=snapshot.query_hash,
            authz_fingerprint=snapshot.authz_fingerprint,
            principal_binding=snapshot.principal_binding,
            offset=offset,
            page_limit=page_limit,
            expires_at=snapshot.expires_at,
        )

    def _cleanup_snapshots(self) -> None:
        now = self._clock.now_utc()
        expired = [key for key, value in self._snapshots.items() if now >= value.expires_at]
        for key in expired:
            del self._snapshots[key]

    def _check_response_size(self, page: Any) -> None:
        size = len(page.model_dump_json().encode("utf-8"))
        if size > self._limits.max_response_bytes:
            raise QueryTooBroad("telemetry response exceeds service byte cap")


__all__ = ["InMemoryTelemetryStore", "TelemetryStoreLimits"]
