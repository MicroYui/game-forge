"""Authorized, bounded read projections for telemetry and run cost data."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cost import (
    BudgetSetSnapshotV1,
    CostUsageViewV1,
    MAX_COST_USAGE_PAGE_SIZE,
    RunCostViewV1,
    UsageEntryV1,
)
from gameforge.contracts.errors import (
    CursorInvalid,
    DependencyUnavailable,
    IntegrityViolation,
    NotFound,
    QueryTooBroad,
    RequestSchemaInvalid,
)
from gameforge.contracts.identity import DomainScope, DomainScopeValue, Permission, Principal
from gameforge.contracts.observability import (
    MAX_QUERY_DESCRIPTOR_REFS,
    MAX_QUERY_FILTER_ITEMS,
    MAX_QUERY_PAGE_SIZE,
    MAX_QUERY_POINTS,
    MAX_QUERY_RESOLUTION_S,
    MAX_QUERY_SERIES,
    MAX_QUERY_TIME_RANGE,
    LogPageV1,
    LogQueryV1,
    LogRecordV1,
    LogRecordViewV1,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricLabelMatcherV1,
    MetricPageV1,
    MetricQueryV1,
    SpanPageV1,
    SpanViewV1,
    TimeRangeV1,
    TraceSummaryPageV1,
    TraceSummaryV1,
    span_run_ids,
)
from gameforge.platform.read_models.authorization import (
    ReadAuthorizationBinding,
    ReadAuthorizationService,
)

_MAX_CURSOR_CHARS = 4096
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_PROVISIONAL_AUTHZ_FINGERPRINT = "0" * 64


@dataclass(frozen=True, slots=True)
class RunObservabilityScope:
    """Authoritative current Run scope proof, distinct from a missing lookup."""

    run_id: str
    domain_scope: DomainScopeValue
    run_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not 1 <= len(self.run_id) <= 512:
            raise ValueError("run_id must be a non-empty bounded string")
        if (
            self.domain_scope is not None
            and self.domain_scope != "all"
            and not isinstance(self.domain_scope, DomainScope)
        ):
            raise TypeError("domain_scope must be an exact DomainScope, all, or None")
        if (
            isinstance(self.run_revision, bool)
            or not isinstance(self.run_revision, int)
            or self.run_revision < 1
        ):
            raise ValueError("run_revision must be a positive integer")


@dataclass(frozen=True, slots=True)
class AuthorizedTelemetryRunScope:
    """Exact Run boundary that a telemetry adapter applies before pagination."""

    mode: Literal["domainless_only", "run_allowlist"]
    allowed_run_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        canonical = tuple(sorted(set(self.allowed_run_ids)))
        if canonical != self.allowed_run_ids or any(
            not isinstance(value, str) or not 1 <= len(value) <= 512 for value in canonical
        ):
            raise ValueError("allowed_run_ids must be canonical bounded strings")
        if len(canonical) > MAX_QUERY_FILTER_ITEMS:
            raise ValueError("allowed_run_ids exceed the log scope limit")
        if self.mode == "domainless_only" and canonical:
            raise ValueError("domainless log scope cannot include Run ids")
        if self.mode == "run_allowlist" and not canonical:
            raise ValueError("run allowlist log scope must include at least one Run id")


@dataclass(frozen=True, slots=True)
class LogTraceScopeProof:
    """Trace Run membership frozen at the exact retained log snapshot cut."""

    trace_id: str
    run_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if _TRACE_ID.fullmatch(self.trace_id) is None:
            raise ValueError("trace_id must be a lowercase 128-bit hex id")
        if self.run_ids != tuple(sorted(set(self.run_ids))) or any(
            not isinstance(value, str) or not 1 <= len(value) <= 512 for value in self.run_ids
        ):
            raise ValueError("trace Run ids must be canonical bounded strings")
        if len(self.run_ids) > MAX_QUERY_FILTER_ITEMS:
            raise ValueError("trace Run ids exceed the scope limit")


@dataclass(frozen=True, slots=True)
class AuthorizedLogReadPage:
    page: LogPageV1
    trace_scopes: tuple[LogTraceScopeProof, ...]


def _telemetry_scope(
    scopes: Sequence[RunObservabilityScope],
) -> AuthorizedTelemetryRunScope:
    allowed_run_ids = tuple(sorted(item.run_id for item in scopes))
    return AuthorizedTelemetryRunScope(
        mode="run_allowlist" if allowed_run_ids else "domainless_only",
        allowed_run_ids=allowed_run_ids,
    )


@dataclass(frozen=True, slots=True)
class RunCostReadPage:
    """Internal exact ledger page; the service projects only safe fields."""

    budget_set: BudgetSetSnapshotV1
    usage_entries: tuple[UsageEntryV1, ...]
    next_cursor: str | None


class ObservabilityReadPort(Protocol):
    """Adapter surface for exact retained telemetry, Run scope, and cost reads.

    Cursor implementations must bind the current authorization fingerprint and
    preserve the M4c invalid/forbidden/expired distinction. Returned telemetry
    remains subject to the platform projection's final redaction checks.
    """

    def get_run_scope(self, run_id: str) -> RunObservabilityScope | None: ...

    def get_trace_summary(self, trace_id: str) -> TraceSummaryV1 | None: ...

    def page_trace_spans(
        self,
        trace_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: ReadAuthorizationBinding,
        scope: AuthorizedTelemetryRunScope,
    ) -> SpanPageV1: ...

    def page_run_traces(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: ReadAuthorizationBinding,
        scope: AuthorizedTelemetryRunScope,
    ) -> TraceSummaryPageV1: ...

    def get_run_trace_scope(self, run_id: str) -> tuple[str, ...]: ...

    def query_logs(
        self,
        query: LogQueryV1,
        *,
        authorization: ReadAuthorizationBinding,
        scope: AuthorizedTelemetryRunScope,
    ) -> AuthorizedLogReadPage: ...

    def get_metric_descriptor_registry(self) -> MetricDescriptorRegistryV1 | None: ...

    def resolve_metric_descriptors(
        self,
        refs: Sequence[MetricDescriptorRefV1],
    ) -> Sequence[MetricDescriptorV1]: ...

    def query_metrics(
        self,
        query: MetricQueryV1,
        *,
        authorization: ReadAuthorizationBinding,
    ) -> MetricPageV1: ...

    def get_run_cost(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: ReadAuthorizationBinding,
        query_hash: str,
    ) -> RunCostReadPage | None: ...


class TelemetryReadRedactor(Protocol):
    """Pure redaction boundary supplied by the apps composition root."""

    def redact_span(self, view: SpanViewV1) -> SpanViewV1: ...

    def redact_log(self, view: LogRecordViewV1) -> LogRecordViewV1: ...


@dataclass(frozen=True, slots=True)
class ObservabilityReadCapabilities:
    """All transaction/request-scoped authorities for one observability read."""

    port: ObservabilityReadPort
    authorization: ReadAuthorizationService
    redactor: TelemetryReadRedactor


ObservabilityReadUnitOfWorkFactory = Callable[
    [], AbstractContextManager[ObservabilityReadCapabilities]
]


class _ObservabilityReadOperations:
    """Load exact scope, authorize, and read within one short capability scope."""

    def __init__(
        self,
        *,
        capabilities: ObservabilityReadCapabilities,
    ) -> None:
        if type(capabilities) is not ObservabilityReadCapabilities:
            raise IntegrityViolation("observability read UoW returned invalid capabilities")
        self._port = capabilities.port
        self._authorization = capabilities.authorization
        self._redactor = capabilities.redactor

    def get_trace(self, *, principal: Principal, trace_id: str) -> TraceSummaryV1:
        trace = self._trace(trace_id)
        query_hash = _query_hash(
            resource_kind="trace",
            filters={"trace_id": trace_id},
            projection="trace-summary@1",
        )
        self._authorize_runs(
            principal=principal,
            run_ids=trace.run_ids,
            resource_kind="trace",
            query_hash=query_hash,
        )
        return trace

    def get_trace_spans(
        self,
        *,
        principal: Principal,
        trace_id: str,
        cursor: str | None,
        limit: int,
    ) -> SpanPageV1:
        exact_limit = _bounded_limit(limit, maximum=MAX_QUERY_PAGE_SIZE, label="span")
        exact_cursor = _bounded_cursor(cursor)
        trace = self._trace(trace_id)
        query_hash = _query_hash(
            resource_kind="trace_spans",
            filters={"trace_id": trace_id, "limit": exact_limit},
            projection="span-page@1",
        )
        binding, scopes = self._authorize_runs(
            principal=principal,
            run_ids=trace.run_ids,
            resource_kind="trace",
            query_hash=query_hash,
        )
        scope = _telemetry_scope(scopes)
        page = self._port.page_trace_spans(
            trace_id,
            cursor=exact_cursor,
            limit=exact_limit,
            authorization=binding,
            scope=scope,
        )
        if type(page) is not SpanPageV1:
            raise IntegrityViolation("trace span adapter returned an invalid page")
        if page.trace_id != trace_id or len(page.items) > exact_limit:
            raise IntegrityViolation("trace span page differs from its bounded query")
        _validate_cursor(page.next_cursor)
        allowed_run_ids = set(scope.allowed_run_ids)
        sanitized: list[SpanViewV1] = []
        for item in page.items:
            if item.span.trace_id != trace_id:
                raise IntegrityViolation("trace span belongs to a different trace")
            if not set(span_run_ids(item.span)) <= allowed_run_ids:
                raise IntegrityViolation("trace span contains an unauthorized Run scope")
            redacted = self._redactor.redact_span(item)
            if (
                type(redacted) is not SpanViewV1
                or redacted.span.trace_id != item.span.trace_id
                or redacted.span.span_id != item.span.span_id
            ):
                raise IntegrityViolation("telemetry redactor changed span identity")
            sanitized.append(redacted)
        return SpanPageV1(
            trace_id=page.trace_id,
            items=tuple(sanitized),
            next_cursor=page.next_cursor,
            truncated=page.truncated,
        )

    def list_run_traces(
        self,
        *,
        principal: Principal,
        run_id: str,
        cursor: str | None,
        limit: int,
    ) -> TraceSummaryPageV1:
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 512:
            raise RequestSchemaInvalid("run_id must be a non-empty bounded string")
        exact_limit = _bounded_limit(limit, maximum=MAX_QUERY_PAGE_SIZE, label="trace")
        exact_cursor = _bounded_cursor(cursor)
        query_hash = _query_hash(
            resource_kind="run_traces",
            filters={"run_id": run_id, "limit": exact_limit},
            projection="trace-summary-page@1",
        )
        related_run_ids = self._port.get_run_trace_scope(run_id)
        if (
            type(related_run_ids) is not tuple
            or related_run_ids != tuple(sorted(set(related_run_ids)))
            or run_id not in related_run_ids
        ):
            raise IntegrityViolation("Run trace scope authority returned an invalid scope")
        singular_binding, _ = self._authorize_runs(
            principal=principal,
            run_ids=(run_id,),
            resource_kind="trace",
            query_hash=query_hash,
        )
        related_scopes = self._load_run_scopes(related_run_ids)
        authorized = self._authorization.filter_collection(
            principal=principal,
            candidates=related_scopes,
            collection_permission=Permission(
                action="read",
                resource_kind="trace",
                domain_scope="all",
            ),
            permission_for=lambda value: Permission(
                action="read",
                resource_kind="trace",
                domain_scope=value.domain_scope,
            ),
            query_hash=canonical_sha256(
                {
                    "query_hash": query_hash,
                    "collection_binding": "run-trace-scopes@1",
                }
            ),
        )
        if authorized.binding.principal_binding != singular_binding.principal_binding:
            raise IntegrityViolation("Run trace authorization changed principal binding")
        scope = _telemetry_scope(authorized.items)
        if run_id not in scope.allowed_run_ids:
            raise IntegrityViolation("requested Run disappeared from authorized trace scope")
        page = self._port.page_run_traces(
            run_id,
            cursor=exact_cursor,
            limit=exact_limit,
            authorization=authorized.binding,
            scope=scope,
        )
        if type(page) is not TraceSummaryPageV1 or len(page.items) > exact_limit:
            raise IntegrityViolation("Run trace adapter returned an invalid bounded page")
        if page.coverage_start > page.coverage_end:
            raise IntegrityViolation("Run trace page returned invalid coverage")
        _validate_cursor(page.next_cursor)
        ordered_keys: list[tuple[datetime, str]] = []
        allowed_run_ids = set(scope.allowed_run_ids)
        seen_trace_ids: set[str] = set()
        for trace in page.items:
            if type(trace) is not TraceSummaryV1 or run_id not in trace.run_ids:
                raise IntegrityViolation("Run trace page contains an unrelated trace")
            if not set(trace.run_ids) <= allowed_run_ids:
                raise IntegrityViolation("Run trace page contains an unauthorized Run scope")
            if trace.trace_id in seen_trace_ids:
                raise IntegrityViolation("Run trace page contains a duplicate trace")
            seen_trace_ids.add(trace.trace_id)
            ordered_keys.append((trace.started_at, trace.trace_id))
        if ordered_keys != sorted(ordered_keys):
            raise IntegrityViolation("Run trace page is not in stable store order")
        return page

    def query_logs(
        self,
        *,
        principal: Principal,
        start_utc: datetime,
        end_utc: datetime,
        services: Sequence[str],
        levels: Sequence[str],
        event_names: Sequence[str],
        run_id: str | None,
        trace_id: str | None,
        span_id: str | None,
        producer_run_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> LogPageV1:
        time_range = _time_range(start_utc, end_utc)
        exact_limit = _bounded_limit(limit, maximum=MAX_QUERY_PAGE_SIZE, label="log")
        exact_cursor = _bounded_cursor(cursor)
        _bounded_collection(services, MAX_QUERY_FILTER_ITEMS, label="log service filters")
        _bounded_collection(event_names, MAX_QUERY_FILTER_ITEMS, label="log event filters")
        if len(levels) > 5:
            raise QueryTooBroad("log level filter exceeds the service cap")
        if span_id is not None and trace_id is None:
            raise RequestSchemaInvalid("span_id filter requires trace_id")

        provisional = _validated(
            LogQueryV1,
            time_range=time_range,
            services=tuple(services),
            levels=tuple(levels),
            event_names=tuple(event_names),
            run_id=run_id,
            trace_id=trace_id,
            span_id=span_id,
            producer_run_id=producer_run_id,
            cursor=exact_cursor,
            limit=exact_limit,
            authz_fingerprint=_PROVISIONAL_AUTHZ_FINGERPRINT,
        )
        run_ids = {
            value
            for value in (provisional.run_id, provisional.producer_run_id)
            if value is not None
        }
        if provisional.trace_id is not None:
            run_ids.update(self._trace(provisional.trace_id).run_ids)
        requested_run_ids = tuple(sorted(run_ids))
        query_hash = _query_hash(
            resource_kind="logs",
            filters={
                "query": provisional.model_dump(
                    mode="json",
                    exclude={"query_schema_version", "cursor", "authz_fingerprint"},
                ),
                "run_scope_mode": "allowlist" if requested_run_ids else "runless_only",
                "requested_run_ids": requested_run_ids,
            },
            projection="log-page@1",
        )
        binding, scopes = self._authorize_runs(
            principal=principal,
            run_ids=requested_run_ids,
            resource_kind="log",
            query_hash=query_hash,
        )
        query_values = provisional.model_dump(mode="python")
        query_values["authz_fingerprint"] = binding.authz_fingerprint
        query = _validated(LogQueryV1, **query_values)
        log_scope = _telemetry_scope(scopes)
        retained = self._port.query_logs(
            query,
            authorization=binding,
            scope=log_scope,
        )
        if type(retained) is not AuthorizedLogReadPage:
            raise IntegrityViolation("log adapter returned an invalid retained page")
        page = retained.page
        if type(page) is not LogPageV1:
            raise IntegrityViolation("log adapter returned an invalid page")
        if (
            len(page.items) > exact_limit
            or page.coverage_start != start_utc
            or page.coverage_end != end_utc
        ):
            raise IntegrityViolation("log page differs from its bounded query")
        _validate_cursor(page.next_cursor)
        allowed_run_id_set = set(log_scope.allowed_run_ids)
        expected_trace_ids = {
            item.record.trace_id for item in page.items if item.record.trace_id is not None
        }
        if any(type(proof) is not LogTraceScopeProof for proof in retained.trace_scopes):
            raise IntegrityViolation("log trace scope proof has an invalid type")
        trace_run_ids = {proof.trace_id: proof.run_ids for proof in retained.trace_scopes}
        if (
            len(trace_run_ids) != len(retained.trace_scopes)
            or set(trace_run_ids) != expected_trace_ids
        ):
            raise IntegrityViolation("log trace scope proof is incomplete or ambiguous")
        sanitized: list[LogRecordViewV1] = []
        for item in page.items:
            item_run_ids = {
                value
                for value in (item.record.run_id, item.record.producer_run_id)
                if value is not None
            }
            if item.record.trace_id is not None:
                item_run_ids.update(trace_run_ids[item.record.trace_id])
            _validate_log_result(
                item.record,
                query,
                allowed_run_id_set,
                item_run_ids=item_run_ids,
            )
            redacted = self._redactor.redact_log(item)
            if (
                type(redacted) is not LogRecordViewV1
                or redacted.record.log_id != item.record.log_id
            ):
                raise IntegrityViolation("telemetry redactor changed log identity")
            sanitized.append(redacted)
        return LogPageV1(
            items=tuple(sanitized),
            next_cursor=page.next_cursor,
            coverage_start=page.coverage_start,
            coverage_end=page.coverage_end,
            truncated=page.truncated,
        )

    def get_metric_descriptors(self, *, principal: Principal) -> MetricDescriptorRegistryV1:
        query_hash = _query_hash(
            resource_kind="metric_descriptors",
            filters={},
            projection="metric-descriptor-registry@1",
        )
        self._authorize_runs(
            principal=principal,
            run_ids=(),
            resource_kind="metric",
            query_hash=query_hash,
        )
        registry = self._port.get_metric_descriptor_registry()
        if registry is None:
            raise DependencyUnavailable(
                "metric descriptor registry is unavailable",
                component="metric_descriptor_registry",
            )
        if type(registry) is not MetricDescriptorRegistryV1:
            raise IntegrityViolation("metric adapter returned an invalid exact registry")
        return registry

    def query_metrics(
        self,
        *,
        principal: Principal,
        descriptor_refs: Sequence[MetricDescriptorRefV1],
        start_utc: datetime,
        end_utc: datetime,
        resolution_s: int,
        label_matchers: Sequence[MetricLabelMatcherV1],
        max_points: int,
        cursor: str | None,
        series_limit: int,
    ) -> MetricPageV1:
        time_range = _time_range(start_utc, end_utc)
        _bounded_collection(
            descriptor_refs,
            MAX_QUERY_DESCRIPTOR_REFS,
            label="metric descriptor refs",
            non_empty=True,
        )
        _bounded_collection(
            label_matchers,
            MAX_QUERY_FILTER_ITEMS,
            label="metric label matchers",
        )
        exact_resolution = _bounded_limit(
            resolution_s,
            maximum=MAX_QUERY_RESOLUTION_S,
            label="metric resolution",
        )
        exact_max_points = _bounded_limit(
            max_points,
            maximum=MAX_QUERY_POINTS,
            label="metric max_points",
        )
        exact_series_limit = _bounded_limit(
            series_limit,
            maximum=MAX_QUERY_SERIES,
            label="metric series",
        )
        exact_cursor = _bounded_cursor(cursor)
        provisional = _validated(
            MetricQueryV1,
            descriptor_refs=tuple(descriptor_refs),
            time_range=time_range,
            resolution_s=exact_resolution,
            label_matchers=tuple(label_matchers),
            max_points=exact_max_points,
            cursor=exact_cursor,
            series_limit=exact_series_limit,
            authz_fingerprint=_PROVISIONAL_AUTHZ_FINGERPRINT,
        )
        query_hash = _query_hash(
            resource_kind="metrics",
            filters=provisional.model_dump(
                mode="json",
                exclude={"query_schema_version", "cursor", "authz_fingerprint"},
            ),
            projection="metric-page@1",
        )
        binding, _ = self._authorize_runs(
            principal=principal,
            run_ids=(),
            resource_kind="metric",
            query_hash=query_hash,
        )
        query_values = provisional.model_dump(mode="python")
        query_values["authz_fingerprint"] = binding.authz_fingerprint
        query = _validated(MetricQueryV1, **query_values)
        descriptors = _exact_metric_descriptors(
            self._port.resolve_metric_descriptors(query.descriptor_refs),
            query.descriptor_refs,
        )
        page = self._port.query_metrics(query, authorization=binding)
        _validate_metric_page(page, query, descriptors)
        return page

    def get_run_cost(
        self,
        *,
        principal: Principal,
        run_id: str,
        cursor: str | None,
        limit: int,
    ) -> RunCostViewV1:
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 512:
            raise RequestSchemaInvalid("run_id must be a non-empty bounded string")
        exact_limit = _bounded_limit(
            limit,
            maximum=MAX_COST_USAGE_PAGE_SIZE,
            label="cost usage",
        )
        exact_cursor = _bounded_cursor(cursor)
        query_hash = _query_hash(
            resource_kind="cost",
            filters={"run_id": run_id, "limit": exact_limit},
            projection="run-cost-view@1",
        )
        binding, _ = self._authorize_runs(
            principal=principal,
            run_ids=(run_id,),
            resource_kind="cost",
            query_hash=query_hash,
        )
        page = self._port.get_run_cost(
            run_id,
            cursor=exact_cursor,
            limit=exact_limit,
            authorization=binding,
            query_hash=query_hash,
        )
        if page is None:
            raise NotFound("run cost data is unavailable", run_id=run_id)
        if type(page) is not RunCostReadPage:
            raise IntegrityViolation("cost adapter returned an invalid page")
        if type(page.budget_set) is not BudgetSetSnapshotV1:
            raise IntegrityViolation("cost adapter returned an invalid budget set")
        if page.budget_set.run_id != run_id or len(page.usage_entries) > exact_limit:
            raise IntegrityViolation("cost page differs from its bounded Run query")
        _validate_cursor(page.next_cursor)
        usage: list[CostUsageViewV1] = []
        for entry in page.usage_entries:
            if type(entry) is not UsageEntryV1 or entry.run_id != run_id:
                raise IntegrityViolation("cost usage entry differs from its Run query")
            usage.append(_cost_usage_view(entry))
        return RunCostViewV1(
            run_id=run_id,
            budget_set=page.budget_set,
            usage=tuple(usage),
            next_cursor=page.next_cursor,
        )

    def _trace(self, trace_id: str) -> TraceSummaryV1:
        if not isinstance(trace_id, str) or _TRACE_ID.fullmatch(trace_id) is None:
            raise RequestSchemaInvalid("trace_id must be a lowercase 128-bit hex id")
        trace = self._port.get_trace_summary(trace_id)
        if trace is None:
            raise NotFound("trace is unavailable", trace_id=trace_id)
        if type(trace) is not TraceSummaryV1 or trace.trace_id != trace_id:
            raise IntegrityViolation("trace adapter returned a different trace")
        return trace

    def _authorize_runs(
        self,
        *,
        principal: Principal,
        run_ids: Sequence[str],
        resource_kind: str,
        query_hash: str,
    ) -> tuple[ReadAuthorizationBinding, tuple[RunObservabilityScope, ...]]:
        scopes = list(self._load_run_scopes(run_ids))

        scope_evidence = [
            {
                "run_id": item.run_id,
                "run_revision": item.run_revision,
                "domain_scope": (
                    item.domain_scope.model_dump(mode="json")
                    if isinstance(item.domain_scope, DomainScope)
                    else item.domain_scope
                ),
            }
            for item in scopes
        ]
        if not scope_evidence:
            scope_evidence.append({"global_non_domain": True})
        scoped_query_hash = canonical_sha256(
            {
                "query_hash": query_hash,
                "scope_evidence": scope_evidence,
            }
        )
        permissions = [item.domain_scope for item in scopes] if scopes else [None]
        bindings = [
            self._authorization.require_singular(
                principal=principal,
                permission=Permission(
                    action="read",
                    resource_kind=resource_kind,
                    domain_scope=scope,
                ),
                query_hash=scoped_query_hash,
            )
            for scope in permissions
        ]
        principal_bindings = {item.principal_binding for item in bindings}
        if len(principal_bindings) != 1:
            raise IntegrityViolation("read authorization returned inconsistent principals")
        return (
            ReadAuthorizationBinding(
                principal_binding=bindings[0].principal_binding,
                authz_fingerprint=canonical_sha256(
                    {
                        "binding_schema_version": "observability-read-authz@1",
                        "members": sorted({item.authz_fingerprint for item in bindings}),
                        "scope_evidence": scope_evidence,
                    }
                ),
            ),
            tuple(scopes),
        )

    def _load_run_scopes(
        self,
        run_ids: Sequence[str],
    ) -> tuple[RunObservabilityScope, ...]:
        unique_run_ids = tuple(sorted(set(run_ids)))
        if len(unique_run_ids) > MAX_QUERY_FILTER_ITEMS:
            raise QueryTooBroad("observability Run scope exceeds the service cap")
        scopes: list[RunObservabilityScope] = []
        for run_id in unique_run_ids:
            scope = self._port.get_run_scope(run_id)
            if scope is None:
                raise IntegrityViolation(
                    "authoritative Run scope is unavailable",
                    run_id=run_id,
                )
            if type(scope) is not RunObservabilityScope or scope.run_id != run_id:
                raise IntegrityViolation("Run scope authority returned a different Run")
            scopes.append(scope)
        return tuple(scopes)


class ObservabilityReadService:
    """Long-lived facade that opens exactly one short UoW per public read."""

    def __init__(self, *, unit_of_work: ObservabilityReadUnitOfWorkFactory) -> None:
        if not callable(unit_of_work):
            raise TypeError("unit_of_work must be callable")
        self._unit_of_work = unit_of_work

    def _operations(
        self,
        capabilities: ObservabilityReadCapabilities,
    ) -> _ObservabilityReadOperations:
        return _ObservabilityReadOperations(capabilities=capabilities)

    def get_trace(self, *, principal: Principal, trace_id: str) -> TraceSummaryV1:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).get_trace(
                principal=principal,
                trace_id=trace_id,
            )

    def get_trace_spans(
        self,
        *,
        principal: Principal,
        trace_id: str,
        cursor: str | None,
        limit: int,
    ) -> SpanPageV1:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).get_trace_spans(
                principal=principal,
                trace_id=trace_id,
                cursor=cursor,
                limit=limit,
            )

    def list_run_traces(
        self,
        *,
        principal: Principal,
        run_id: str,
        cursor: str | None,
        limit: int,
    ) -> TraceSummaryPageV1:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).list_run_traces(
                principal=principal,
                run_id=run_id,
                cursor=cursor,
                limit=limit,
            )

    def query_logs(
        self,
        *,
        principal: Principal,
        start_utc: datetime,
        end_utc: datetime,
        services: Sequence[str],
        levels: Sequence[str],
        event_names: Sequence[str],
        run_id: str | None,
        trace_id: str | None,
        span_id: str | None,
        producer_run_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> LogPageV1:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).query_logs(
                principal=principal,
                start_utc=start_utc,
                end_utc=end_utc,
                services=services,
                levels=levels,
                event_names=event_names,
                run_id=run_id,
                trace_id=trace_id,
                span_id=span_id,
                producer_run_id=producer_run_id,
                cursor=cursor,
                limit=limit,
            )

    def get_metric_descriptors(self, *, principal: Principal) -> MetricDescriptorRegistryV1:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).get_metric_descriptors(principal=principal)

    def query_metrics(
        self,
        *,
        principal: Principal,
        descriptor_refs: Sequence[MetricDescriptorRefV1],
        start_utc: datetime,
        end_utc: datetime,
        resolution_s: int,
        label_matchers: Sequence[MetricLabelMatcherV1],
        max_points: int,
        cursor: str | None,
        series_limit: int,
    ) -> MetricPageV1:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).query_metrics(
                principal=principal,
                descriptor_refs=descriptor_refs,
                start_utc=start_utc,
                end_utc=end_utc,
                resolution_s=resolution_s,
                label_matchers=label_matchers,
                max_points=max_points,
                cursor=cursor,
                series_limit=series_limit,
            )

    def get_run_cost(
        self,
        *,
        principal: Principal,
        run_id: str,
        cursor: str | None,
        limit: int,
    ) -> RunCostViewV1:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).get_run_cost(
                principal=principal,
                run_id=run_id,
                cursor=cursor,
                limit=limit,
            )


def _query_hash(*, resource_kind: str, filters: object, projection: str) -> str:
    return canonical_sha256(
        {
            "api_version": "v1",
            "resource_kind": resource_kind,
            "filters": _wire_value(filters),
            "stable_sort": f"{resource_kind}-store-order@1",
            "page_projection": projection,
        }
    )


def _wire_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _wire_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_wire_value(item) for item in value]
    return value


def _bounded_limit(value: int, *, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RequestSchemaInvalid(f"{label} limit must be a positive integer")
    if value > maximum:
        raise QueryTooBroad(f"{label} query exceeds the service cap")
    return value


def _bounded_collection(
    values: Sequence[object],
    maximum: int,
    *,
    label: str,
    non_empty: bool = False,
) -> None:
    if non_empty and not values:
        raise RequestSchemaInvalid(f"{label} must be non-empty")
    if len(values) > maximum:
        raise QueryTooBroad(f"{label} exceed the service cap")


def _bounded_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_CHARS:
        raise CursorInvalid("observability cursor is malformed")
    return cursor


def _validate_cursor(cursor: str | None) -> None:
    if cursor is not None and (
        not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_CHARS
    ):
        raise IntegrityViolation("observability adapter returned an invalid cursor")


def _time_range(start_utc: datetime, end_utc: datetime) -> TimeRangeV1:
    for value in (start_utc, end_utc):
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise RequestSchemaInvalid("observability timestamps must be UTC")
    if start_utc >= end_utc:
        raise RequestSchemaInvalid("observability range must be non-empty")
    if end_utc - start_utc > MAX_QUERY_TIME_RANGE:
        raise QueryTooBroad("observability time range exceeds the service cap")
    return TimeRangeV1(start_utc=start_utc, end_utc=end_utc)


def _validated[T: BaseModel](model_type: type[T], **values: object) -> T:
    try:
        return model_type.model_validate(values)
    except ValidationError as exc:
        raise RequestSchemaInvalid("observability query is invalid") from exc


def _validate_log_result(
    record: LogRecordV1,
    query: LogQueryV1,
    allowed_run_ids: set[str],
    *,
    item_run_ids: set[str],
) -> None:
    if not query.time_range.start_utc <= record.ts_utc < query.time_range.end_utc:
        raise IntegrityViolation("log adapter returned an item outside the time range")
    checks = (
        (not query.services or record.service in query.services),
        (not query.levels or record.level in query.levels),
        (not query.event_names or record.event_name in query.event_names),
        (query.run_id is None or record.run_id == query.run_id),
        (query.trace_id is None or record.trace_id == query.trace_id),
        (query.span_id is None or record.span_id == query.span_id),
        (query.producer_run_id is None or record.producer_run_id == query.producer_run_id),
    )
    if not all(checks):
        raise IntegrityViolation("log adapter returned an item outside the exact filters")
    if not item_run_ids <= allowed_run_ids:
        raise IntegrityViolation("log adapter returned an unauthorized Run scope")


def _exact_metric_descriptors(
    values: Sequence[MetricDescriptorV1],
    requested_refs: Sequence[MetricDescriptorRefV1],
) -> dict[MetricDescriptorRefV1, MetricDescriptorV1]:
    descriptors = tuple(values)
    if any(type(item) is not MetricDescriptorV1 for item in descriptors):
        raise IntegrityViolation("metric descriptor resolver returned an invalid descriptor")
    by_ref = {item.ref: item for item in descriptors}
    if len(by_ref) != len(descriptors) or set(by_ref) != set(requested_refs):
        raise IntegrityViolation(
            "metric descriptor resolver did not return the exact requested set"
        )
    return by_ref


def _validate_metric_page(
    page: MetricPageV1,
    query: MetricQueryV1,
    descriptors: Mapping[MetricDescriptorRefV1, MetricDescriptorV1],
) -> None:
    if type(page) is not MetricPageV1:
        raise IntegrityViolation("metric adapter returned an invalid page")
    if (
        len(page.series) > query.series_limit
        or page.coverage_start != query.time_range.start_utc
        or page.coverage_end != query.time_range.end_utc
        or page.effective_resolution_s != query.resolution_s
    ):
        raise IntegrityViolation("metric page differs from its bounded query")
    _validate_cursor(page.next_cursor)
    requested = set(query.descriptor_refs)
    point_count = 0
    series_keys: list[tuple[str, int, str, tuple[tuple[str, str], ...]]] = []
    for series in page.series:
        if series.descriptor not in requested:
            raise IntegrityViolation("metric adapter returned an unrequested descriptor")
        descriptor = descriptors.get(series.descriptor)
        if descriptor is None:
            raise IntegrityViolation("metric adapter returned an unresolved descriptor")
        expected_bounds = (
            descriptor.histogram_bucket_bounds if descriptor.metric_type == "histogram" else None
        )
        if (
            series.metric_name != descriptor.metric_name
            or series.metric_type != descriptor.metric_type
            or series.unit != descriptor.unit
            or series.bucket_bounds != expected_bounds
            or set(series.labels) != set(descriptor.label_keys)
        ):
            raise IntegrityViolation("metric series differs from its exact descriptor")
        series_keys.append(
            (
                series.descriptor.metric_name,
                series.descriptor.descriptor_version,
                series.descriptor.descriptor_digest,
                tuple(sorted(series.labels.items())),
            )
        )
        points = series.scalar_points or series.histogram_points or ()
        point_count += len(points)
        timestamps = [point.ts_utc for point in points]
        if timestamps != sorted(set(timestamps)):
            raise IntegrityViolation("metric samples are not in stable unique timestamp order")
        if any(
            not query.time_range.start_utc <= timestamp < query.time_range.end_utc
            for timestamp in timestamps
        ):
            raise IntegrityViolation("metric sample falls outside the requested time range")
        for matcher in query.label_matchers:
            value = series.labels.get(matcher.key)
            if value is None or value not in matcher.values:
                raise IntegrityViolation("metric adapter returned a series outside label filters")
    if series_keys != sorted(series_keys) or len(series_keys) != len(set(series_keys)):
        raise IntegrityViolation("metric series are not in stable unique order")
    if point_count > query.max_points:
        raise IntegrityViolation("metric adapter exceeded the requested point bound")


def _cost_usage_view(entry: UsageEntryV1) -> CostUsageViewV1:
    return CostUsageViewV1(
        usage_id=entry.usage_id,
        scope=entry.scope,
        attempt_no=entry.attempt_no,
        transport_attempt=entry.transport_attempt,
        execution_source=entry.execution_source,
        provider_prefix_cache=entry.provider_prefix_cache,
        retry_index=entry.retry_index,
        token_usage=entry.token_usage,
        latency=entry.latency,
        wall_time_ns=entry.wall_time_ns,
        monetary=entry.monetary,
        adjustment_of_usage_id=entry.adjustment_of_usage_id,
        recorded_at=entry.recorded_at,
    )


__all__ = [
    "ObservabilityReadCapabilities",
    "ObservabilityReadPort",
    "ObservabilityReadService",
    "ObservabilityReadUnitOfWorkFactory",
    "RunCostReadPage",
    "RunObservabilityScope",
    "TelemetryReadRedactor",
]
