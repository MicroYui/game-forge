from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from gameforge.apps.api.dependencies import require_actor
from gameforge.apps.api.errors import install_error_handlers
from gameforge.apps.api.routers.observability import observability_router
from gameforge.contracts.cost import (
    BudgetSetSnapshotV1,
    BudgetSnapshotV1,
    CacheHitObservationV1,
    CostAmountV1,
    LatencyObservationV1,
    MAX_COST_USAGE_PAGE_SIZE,
    MonetaryObservationV1,
    TokenUsageObservationV1,
    UsageEntryV1,
)
from gameforge.contracts.errors import Forbidden, IntegrityViolation, QueryTooBroad
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
    Permission,
    Principal,
    RoleAssignmentV1,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.observability import (
    LogErrorV1,
    LogPageV1,
    LogQueryV1,
    LogRecordV1,
    LogRecordViewV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricPageV1,
    MetricQueryV1,
    MetricSeriesV1,
    ScalarMetricSampleV1,
    SpanDataV1,
    SpanPageV1,
    SpanViewV1,
    TraceSummaryPageV1,
    TraceSummaryV1,
    compute_metric_descriptor_digest,
    compute_metric_registry_digest,
)
from gameforge.platform.read_models.authorization import ReadAuthorizationService
from gameforge.platform.read_models.observability import (
    AuthorizedLogReadPage,
    AuthorizedTelemetryRunScope,
    LogTraceScopeProof,
    ObservabilityReadCapabilities,
    ObservabilityReadService,
    RunCostReadPage,
    RunObservabilityScope,
)
from gameforge.runtime.observability._fields import (
    is_sensitive_key,
    redact_sensitive_text,
    redact_span_values,
    sanitize_telemetry_value,
)


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
TRACE_ID = "1" * 32
SPAN_ID = "2" * 16
DOMAIN = DomainScope(domain_ids=("numeric",))
DOMAIN_B = DomainScope(domain_ids=("narrative",))
DOMAIN_AB = DomainScope(domain_ids=("narrative", "numeric"))


def _registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(
            domain_id="narrative",
            display_name="Narrative",
            status="active",
        ),
        DomainDefinitionV1(
            domain_id="numeric",
            display_name="Numeric",
            status="active",
        ),
    )
    return DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )


def _assignment(
    assignment_id: str,
    role: str,
    scope: DomainScope | None,
) -> RoleAssignmentV1:
    return RoleAssignmentV1(
        assignment_id=assignment_id,
        principal_id="human:reader",
        role=role,
        scope=scope,
        status="active",
        revision=1,
        granted_at="2026-07-14T08:00:00Z",
        granted_by=AuditActor(
            principal_id="human:admin",
            principal_kind="human",
        ),
    )


def _principal(
    *,
    include_global: bool = True,
    domain_scope: DomainScope = DOMAIN,
) -> Principal:
    roles = [_assignment("assignment:domain", "content_designer", domain_scope)]
    if include_global:
        roles.append(_assignment("assignment:global", "tooling", None))
    return Principal(
        id="human:reader",
        kind="human",
        display_name="Reader",
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=2,
        roles=tuple(roles),
    )


def _actor(*, include_global: bool = True) -> ActorContext:
    return ActorContext(
        principal=_principal(include_global=include_global),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:reader",
        ),
        session_id="session:reader",
        request_id="request:test",
    )


class _Policies:
    def __init__(self) -> None:
        self.registry = _registry()
        ref = DomainRegistryRefV1(
            registry_version=self.registry.registry_version,
            registry_digest=self.registry.registry_digest,
        )
        grants = {
            "content_designer": tuple(
                Permission(action="read", resource_kind=kind, domain_scope="all")
                for kind in ("trace", "log", "cost")
            ),
            "tooling": tuple(
                Permission(action="read", resource_kind=kind, domain_scope=None)
                for kind in ("trace", "log", "metric", "cost")
            ),
        }
        self.policy = RolePolicy(
            policy_version="role-policy@1",
            domain_registry_ref=ref,
            grants=grants,
            effective_from="2026-07-14T08:00:00Z",
            policy_digest=compute_role_policy_digest(
                "role-policy@1",
                ref,
                grants,
                "2026-07-14T08:00:00Z",
            ),
        )

    def get_role_policy(self, version: str, digest: str) -> RolePolicy | None:
        if (version, digest) == (
            self.policy.policy_version,
            self.policy.policy_digest,
        ):
            return self.policy
        return None

    def get_domain_registry(
        self,
        ref: DomainRegistryRefV1,
    ) -> DomainRegistryV1 | None:
        if ref == self.policy.domain_registry_ref:
            return self.registry
        return None


def _descriptor() -> MetricDescriptorV1:
    payload = {
        "metric_name": "gameforge.run.completed",
        "descriptor_version": 1,
        "metric_type": "counter",
        "unit": "count",
        "label_keys": ("outcome",),
        "histogram_bucket_bounds": (),
        "series_limit": 8,
    }
    return MetricDescriptorV1(
        **payload,
        descriptor_digest=compute_metric_descriptor_digest(payload),
    )


def _metric_registry() -> MetricDescriptorRegistryV1:
    payload = {
        "registry_version": 1,
        "descriptors": (_descriptor(),),
        "global_series_limit": 16,
    }
    return MetricDescriptorRegistryV1(
        **payload,
        registry_digest=compute_metric_registry_digest(payload),
    )


def _trace_summary() -> TraceSummaryV1:
    return TraceSummaryV1(
        trace_id=TRACE_ID,
        root_span_id=SPAN_ID,
        run_ids=("run:1",),
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
        duration_ns=1_000_000_000,
        status="ok",
        span_count=1,
        service_names=("worker",),
        truncated=False,
    )


def _span_page() -> SpanPageV1:
    span = SpanDataV1(
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        parent_span_id=None,
        name="Authorization: Bearer span-secret",
        attributes={"run_id": "run:1", "raw_prompt": "private prompt"},
        links=(),
        events=(),
        status="ok",
        error=None,
        resource={"service.name": "worker"},
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
        duration_ns=1_000_000_000,
    )
    return SpanPageV1(
        trace_id=TRACE_ID,
        items=(SpanViewV1(span=span),),
        next_cursor=None,
        truncated=False,
    )


def _log_page(query: LogQueryV1) -> LogPageV1:
    record = LogRecordV1(
        log_id="log:1",
        ts_utc=NOW,
        level="error",
        message="Authorization: Bearer log-secret",
        service="worker",
        event_name="model.finished",
        run_id="run:1",
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        error=LogErrorV1(
            error_type="ProviderError",
            message="api_key=private-key",
        ),
        fields={"raw_response": "private response", "attempt": 1},
    )
    return LogPageV1(
        items=(LogRecordViewV1(record=record),),
        next_cursor=None,
        coverage_start=query.time_range.start_utc,
        coverage_end=query.time_range.end_utc,
        truncated=False,
    )


def _budget_set() -> BudgetSetSnapshotV1:
    amount = CostAmountV1(dimension="request", value=10, unit="request")
    snapshot = BudgetSnapshotV1(
        snapshot_id="budget-snapshot:1",
        budget_id="budget:run:1",
        scope_kind="run",
        scope_id="run:1",
        policy_version="budget-policy@1",
        budget_revision_at_freeze=1,
        limits=(amount,),
        reserved=(),
        consumed=(),
        captured_at=NOW,
    )
    return BudgetSetSnapshotV1(
        budget_set_snapshot_id="budget-set:run:1",
        run_id="run:1",
        selection_policy_version="selection@1",
        snapshots=(snapshot,),
        captured_at=NOW,
    )


def _usage() -> UsageEntryV1:
    return UsageEntryV1(
        usage_id="usage:1",
        reservation_group_id="reservation-group:1",
        budget_reservation_ids=("reservation:1",),
        scope="attempt_call",
        run_id="run:1",
        attempt_no=1,
        request_hash="sha256:" + "a" * 64,
        transport_attempt=1,
        execution_source="online",
        provider_prefix_cache=CacheHitObservationV1(status="reported", hit=False),
        retry_index=0,
        token_usage=TokenUsageObservationV1(
            status="reported",
            input_tokens=8,
            output_tokens=2,
            total_tokens=10,
        ),
        latency=LatencyObservationV1(status="reported", provider_latency_ms=25),
        wall_time_ns=30_000_000,
        monetary=MonetaryObservationV1(status="unavailable"),
        routing_decision_kind="native",
        routing_decision_id="routing:private",
        fencing_token_at_reserve=99,
        recorded_at=NOW,
    )


@dataclass
class _Port:
    missing_scope: bool = False

    def __post_init__(self) -> None:
        self.registry = _metric_registry()
        self.trace_summary = _trace_summary()
        self.authorization_bindings: list[object] = []
        self.log_scopes: list[AuthorizedTelemetryRunScope] = []
        self.span_scopes: list[AuthorizedTelemetryRunScope] = []
        self.run_trace_scopes: list[AuthorizedTelemetryRunScope] = []

    def get_run_scope(self, run_id: str) -> RunObservabilityScope | None:
        if self.missing_scope or run_id != "run:1":
            return None
        return RunObservabilityScope(
            run_id=run_id,
            domain_scope=DOMAIN,
            run_revision=3,
        )

    def get_trace_summary(self, trace_id: str) -> TraceSummaryV1 | None:
        return self.trace_summary if trace_id == TRACE_ID else None

    def page_trace_spans(
        self,
        trace_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: object,
        scope: AuthorizedTelemetryRunScope,
    ) -> SpanPageV1:
        del cursor, limit
        self.authorization_bindings.append(authorization)
        self.span_scopes.append(scope)
        return _span_page()

    def get_run_trace_scope(self, run_id: str) -> tuple[str, ...]:
        return tuple(sorted(set(self.trace_summary.run_ids) | {run_id}))

    def page_run_traces(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: object,
        scope: AuthorizedTelemetryRunScope,
    ) -> TraceSummaryPageV1:
        del cursor, limit
        self.authorization_bindings.append(authorization)
        self.run_trace_scopes.append(scope)
        if run_id != "run:1":
            raise AssertionError("unexpected run")
        return TraceSummaryPageV1(
            items=(self.trace_summary,),
            next_cursor=None,
            coverage_start=NOW,
            coverage_end=NOW + timedelta(seconds=1),
            truncated=False,
        )

    def query_logs(
        self,
        query: LogQueryV1,
        *,
        authorization: object,
        scope: AuthorizedTelemetryRunScope,
    ) -> AuthorizedLogReadPage:
        self.authorization_bindings.append(authorization)
        self.log_scopes.append(scope)
        return AuthorizedLogReadPage(
            page=_log_page(query),
            trace_scopes=(LogTraceScopeProof(trace_id=TRACE_ID, run_ids=("run:1",)),),
        )

    def get_metric_descriptor_registry(self) -> MetricDescriptorRegistryV1 | None:
        return self.registry

    def resolve_metric_descriptors(self, refs: object) -> tuple[MetricDescriptorV1, ...]:
        assert tuple(refs) == (self.registry.descriptors[0].ref,)
        return self.registry.descriptors

    def query_metrics(self, query: MetricQueryV1, *, authorization: object) -> MetricPageV1:
        self.authorization_bindings.append(authorization)
        return MetricPageV1(
            series=(),
            next_cursor=None,
            coverage_start=query.time_range.start_utc,
            coverage_end=query.time_range.end_utc,
            effective_resolution_s=query.resolution_s,
            truncated=False,
        )

    def get_run_cost(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: object,
        query_hash: str,
    ) -> RunCostReadPage | None:
        del cursor, limit, query_hash
        self.authorization_bindings.append(authorization)
        if run_id != "run:1":
            return None
        return RunCostReadPage(
            budget_set=_budget_set(),
            usage_entries=(_usage(),),
            next_cursor=None,
        )


class _CrossRunPort(_Port):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.trace_summary = self.trace_summary.model_copy(update={"run_ids": ("run:1", "run:2")})

    def get_run_scope(self, run_id: str) -> RunObservabilityScope | None:
        scopes = {"run:1": DOMAIN, "run:2": DOMAIN_B}
        scope = scopes.get(run_id)
        if scope is None:
            return None
        return RunObservabilityScope(run_id=run_id, domain_scope=scope, run_revision=3)

    def page_run_traces(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: object,
        scope: AuthorizedTelemetryRunScope,
    ) -> TraceSummaryPageV1:
        del cursor, limit
        self.authorization_bindings.append(authorization)
        self.run_trace_scopes.append(scope)
        assert run_id in self.trace_summary.run_ids
        items = (
            (self.trace_summary,)
            if set(self.trace_summary.run_ids) <= set(scope.allowed_run_ids)
            else ()
        )
        return TraceSummaryPageV1(
            items=items,
            next_cursor=None,
            coverage_start=NOW,
            coverage_end=NOW + timedelta(seconds=1),
            truncated=False,
        )


class _ScopeDiscoveryMustNotRunPort(_Port):
    def get_run_trace_scope(self, run_id: str) -> tuple[str, ...]:
        raise AssertionError(f"unauthorized scope discovery for {run_id}")


class _ProducerMismatchPort(_CrossRunPort):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.trace_summary = self.trace_summary.model_copy(update={"run_ids": ("run:1",)})

    def page_trace_spans(
        self,
        trace_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: object,
        scope: AuthorizedTelemetryRunScope,
    ) -> SpanPageV1:
        del trace_id, cursor, limit
        self.authorization_bindings.append(authorization)
        self.span_scopes.append(scope)
        page = _span_page()
        span = page.items[0].span.model_copy(
            update={
                "attributes": {
                    **page.items[0].span.attributes,
                    "producer_run_id": "run:2",
                }
            }
        )
        return page.model_copy(update={"items": (SpanViewV1(span=span),)})


class _LogTraceMismatchPort(_Port):
    def query_logs(
        self,
        query: LogQueryV1,
        *,
        authorization: object,
        scope: AuthorizedTelemetryRunScope,
    ) -> AuthorizedLogReadPage:
        self.authorization_bindings.append(authorization)
        self.log_scopes.append(scope)
        page = _log_page(query)
        record = page.items[0].record.model_copy(update={"run_id": None})
        return AuthorizedLogReadPage(
            page=page.model_copy(update={"items": (LogRecordViewV1(record=record),)}),
            trace_scopes=(LogTraceScopeProof(trace_id=TRACE_ID, run_ids=("run:1",)),),
        )


class _OrphanLogPort(_Port):
    def query_logs(
        self,
        query: LogQueryV1,
        *,
        authorization: object,
        scope: AuthorizedTelemetryRunScope,
    ) -> AuthorizedLogReadPage:
        self.authorization_bindings.append(authorization)
        self.log_scopes.append(scope)
        page = _log_page(query)
        record = page.items[0].record.model_copy(update={"run_id": None})
        return AuthorizedLogReadPage(
            page=page.model_copy(update={"items": (LogRecordViewV1(record=record),)}),
            trace_scopes=(),
        )


class _FrozenLogCursorPort(_Port):
    def query_logs(
        self,
        query: LogQueryV1,
        *,
        authorization: object,
        scope: AuthorizedTelemetryRunScope,
    ) -> AuthorizedLogReadPage:
        self.authorization_bindings.append(authorization)
        self.log_scopes.append(scope)
        page = _log_page(query)
        suffix = "first" if query.cursor is None else "second"
        record = page.items[0].record.model_copy(update={"log_id": f"log:{suffix}"})
        return AuthorizedLogReadPage(
            page=page.model_copy(
                update={
                    "items": (LogRecordViewV1(record=record),),
                    "next_cursor": "retained-cursor" if query.cursor is None else None,
                    "truncated": query.cursor is None,
                }
            ),
            trace_scopes=(LogTraceScopeProof(trace_id=TRACE_ID, run_ids=("run:1",)),),
        )


class _MixedRunTracePort(_CrossRunPort):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.a_only = self.trace_summary.model_copy(
            update={
                "trace_id": "3" * 32,
                "root_span_id": "4" * 16,
                "run_ids": ("run:1",),
                "started_at": NOW - timedelta(seconds=1),
                "ended_at": NOW,
            }
        )

    def page_run_traces(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: object,
        scope: AuthorizedTelemetryRunScope,
    ) -> TraceSummaryPageV1:
        del cursor, limit
        self.authorization_bindings.append(authorization)
        self.run_trace_scopes.append(scope)
        allowed = set(scope.allowed_run_ids)
        items = tuple(
            summary
            for summary in (self.a_only, self.trace_summary)
            if run_id in summary.run_ids and set(summary.run_ids) <= allowed
        )
        return TraceSummaryPageV1(
            items=items,
            next_cursor=None,
            coverage_start=NOW - timedelta(seconds=1),
            coverage_end=NOW + timedelta(seconds=1),
            truncated=False,
        )


class _Redactor:
    def redact_span(self, view: SpanViewV1) -> SpanViewV1:
        redacted = redact_span_values(view.span)
        keys = tuple(
            sorted(
                key
                for key, value in view.span.attributes.items()
                if redacted.attributes.get(key) != value
            )
        )
        return SpanViewV1(
            span=redacted,
            redacted_attribute_keys=keys,
            redacted_event_fields=view.redacted_event_fields,
        )

    def redact_log(self, view: LogRecordViewV1) -> LogRecordViewV1:
        record = view.record
        fields: dict[str, object] = {}
        redacted_fields = set(view.redacted_fields)
        for key, value in record.fields.items():
            if is_sensitive_key(key):
                fields[key] = "[REDACTED]"
                redacted_fields.add(key)
            else:
                fields[key] = sanitize_telemetry_value(value)[0]
        error = record.error
        if error is not None:
            error = LogErrorV1(
                error_type=error.error_type,
                message=redact_sensitive_text(error.message)[0],
                stack_fingerprint=error.stack_fingerprint,
            )
        safe = record.model_copy(
            update={
                "message": redact_sensitive_text(record.message)[0],
                "error": error,
                "fields": fields,
            }
        )
        return LogRecordViewV1(
            record=safe,
            redacted_fields=tuple(sorted(redacted_fields)),
        )


def _service(port: _Port | None = None) -> tuple[ObservabilityReadService, _Port]:
    policies = _Policies()
    selected_port = port or _Port()
    authorization = ReadAuthorizationService(
        policy_repository=policies,
        role_policy_version=policies.policy.policy_version,
        role_policy_digest=policies.policy.policy_digest,
    )

    @contextmanager
    def unit_of_work():
        yield ObservabilityReadCapabilities(
            port=selected_port,
            authorization=authorization,
            redactor=_Redactor(),
        )

    return ObservabilityReadService(unit_of_work=unit_of_work), selected_port


def test_service_authorizes_run_scope_and_redacts_trace_log_and_cost_views() -> None:
    service, port = _service()
    principal = _principal()

    assert service.get_trace(principal=principal, trace_id=TRACE_ID) == _trace_summary()
    spans = service.get_trace_spans(
        principal=principal,
        trace_id=TRACE_ID,
        cursor=None,
        limit=10,
    )
    logs = service.query_logs(
        principal=principal,
        start_utc=NOW - timedelta(minutes=1),
        end_utc=NOW + timedelta(minutes=1),
        services=("worker",),
        levels=("error",),
        event_names=("model.finished",),
        run_id="run:1",
        trace_id=None,
        span_id=None,
        producer_run_id=None,
        cursor=None,
        limit=10,
    )
    cost = service.get_run_cost(
        principal=principal,
        run_id="run:1",
        cursor=None,
        limit=10,
    )

    wire = json.dumps(
        {
            "spans": spans.model_dump(mode="json"),
            "logs": logs.model_dump(mode="json"),
            "cost": cost.model_dump(mode="json"),
        },
        sort_keys=True,
    )
    for secret in (
        "span-secret",
        "private prompt",
        "log-secret",
        "private-key",
        "private response",
        "routing:private",
        "sha256:" + "a" * 64,
    ):
        assert secret not in wire
    assert "fencing_token_at_reserve" not in wire
    assert spans.items[0].span.attributes["raw_prompt"] == "[REDACTED]"
    assert logs.items[0].record.fields["raw_response"] == "[REDACTED]"
    assert len(port.authorization_bindings) == 3
    assert port.log_scopes == [
        AuthorizedTelemetryRunScope(mode="run_allowlist", allowed_run_ids=("run:1",))
    ]
    assert all(
        len(item.principal_binding) == 64 and len(item.authz_fingerprint) == 64
        for item in port.authorization_bindings
    )


def test_trace_authorization_covers_run_and_producer_run_domains() -> None:
    service, port = _service(_CrossRunPort())

    with pytest.raises(Forbidden):
        service.get_trace(principal=_principal(), trace_id=TRACE_ID)

    principal = _principal(domain_scope=DOMAIN_AB)
    assert service.get_trace(principal=principal, trace_id=TRACE_ID).run_ids == (
        "run:1",
        "run:2",
    )
    page = service.list_run_traces(
        principal=principal,
        run_id="run:2",
        cursor=None,
        limit=10,
    )
    assert page.items == (port.trace_summary,)


def test_run_trace_collection_filters_mixed_domain_traces_before_paging() -> None:
    service, port = _service(_MixedRunTracePort())

    a_only = service.list_run_traces(
        principal=_principal(),
        run_id="run:1",
        cursor=None,
        limit=10,
    )
    assert a_only.items == (port.a_only,)
    assert port.run_trace_scopes[-1] == AuthorizedTelemetryRunScope(
        mode="run_allowlist",
        allowed_run_ids=("run:1",),
    )

    both = service.list_run_traces(
        principal=_principal(domain_scope=DOMAIN_AB),
        run_id="run:1",
        cursor=None,
        limit=10,
    )
    assert both.items == (port.a_only, port.trace_summary)
    assert port.run_trace_scopes[-1] == AuthorizedTelemetryRunScope(
        mode="run_allowlist",
        allowed_run_ids=("run:1", "run:2"),
    )


def test_denied_run_trace_list_stops_before_scope_discovery() -> None:
    service, _ = _service(_ScopeDiscoveryMustNotRunPort())

    with pytest.raises(Forbidden):
        service.list_run_traces(
            principal=_principal(domain_scope=DOMAIN_B),
            run_id="run:1",
            cursor=None,
            limit=10,
        )


def test_producer_only_trace_is_not_treated_as_non_domain() -> None:
    port = _CrossRunPort()
    port.trace_summary = port.trace_summary.model_copy(update={"run_ids": ("run:2",)})
    service, _ = _service(port)

    with pytest.raises(Forbidden):
        service.get_trace(principal=_principal(), trace_id=TRACE_ID)

    principal = _principal(domain_scope=DOMAIN_B)
    assert service.get_trace(principal=principal, trace_id=TRACE_ID).run_ids == ("run:2",)
    assert service.list_run_traces(
        principal=principal,
        run_id="run:2",
        cursor=None,
        limit=10,
    ).items == (port.trace_summary,)


def test_span_page_rejects_producer_scope_omitted_by_trace_summary() -> None:
    service, _ = _service(_ProducerMismatchPort())

    with pytest.raises(IntegrityViolation, match="unauthorized Run"):
        service.get_trace_spans(
            principal=_principal(domain_scope=DOMAIN_AB),
            trace_id=TRACE_ID,
            cursor=None,
            limit=10,
        )


def test_overbroad_trace_run_scope_stops_before_log_page_read() -> None:
    port = _Port()
    port.trace_summary = port.trace_summary.model_copy(
        update={"run_ids": tuple(f"run:{index:03d}" for index in range(65))}
    )
    service, _ = _service(port)

    with pytest.raises(QueryTooBroad, match="Run scope"):
        service.query_logs(
            principal=_principal(),
            start_utc=NOW - timedelta(minutes=1),
            end_utc=NOW + timedelta(minutes=1),
            services=(),
            levels=(),
            event_names=(),
            run_id=None,
            trace_id=TRACE_ID,
            span_id=None,
            producer_run_id=None,
            cursor=None,
            limit=10,
        )

    assert port.authorization_bindings == []
    assert port.log_scopes == []


def test_missing_authoritative_run_scope_fails_closed_before_data_read() -> None:
    service, port = _service(_Port(missing_scope=True))

    with pytest.raises(IntegrityViolation, match="scope"):
        service.get_trace(principal=_principal(), trace_id=TRACE_ID)

    assert port.authorization_bindings == []


def test_global_metric_and_log_reads_require_explicit_non_domain_permission() -> None:
    service, _ = _service()
    principal = _principal(include_global=False)

    with pytest.raises(Forbidden):
        service.get_metric_descriptors(principal=principal)
    with pytest.raises(Forbidden):
        service.query_logs(
            principal=principal,
            start_utc=NOW - timedelta(minutes=1),
            end_utc=NOW + timedelta(minutes=1),
            services=(),
            levels=(),
            event_names=(),
            run_id=None,
            trace_id=None,
            span_id=None,
            producer_run_id=None,
            cursor=None,
            limit=10,
        )


def test_domainless_log_query_rejects_run_bound_adapter_output() -> None:
    service, port = _service()

    with pytest.raises(IntegrityViolation, match="unauthorized Run"):
        service.query_logs(
            principal=_principal(),
            start_utc=NOW - timedelta(minutes=1),
            end_utc=NOW + timedelta(minutes=1),
            services=(),
            levels=(),
            event_names=(),
            run_id=None,
            trace_id=None,
            span_id=None,
            producer_run_id=None,
            cursor=None,
            limit=10,
        )

    assert port.log_scopes == [AuthorizedTelemetryRunScope(mode="domainless_only")]


def test_domainless_log_query_rejects_domain_trace_correlation() -> None:
    service, port = _service(_LogTraceMismatchPort())

    with pytest.raises(IntegrityViolation, match="unauthorized Run"):
        service.query_logs(
            principal=_principal(),
            start_utc=NOW - timedelta(minutes=1),
            end_utc=NOW + timedelta(minutes=1),
            services=(),
            levels=(),
            event_names=(),
            run_id=None,
            trace_id=None,
            span_id=None,
            producer_run_id=None,
            cursor=None,
            limit=10,
        )

    assert port.log_scopes == [AuthorizedTelemetryRunScope(mode="domainless_only")]


def test_log_page_requires_complete_retained_trace_scope_proof() -> None:
    service, _ = _service(_OrphanLogPort())

    with pytest.raises(IntegrityViolation, match="proof"):
        service.query_logs(
            principal=_principal(),
            start_utc=NOW - timedelta(minutes=1),
            end_utc=NOW + timedelta(minutes=1),
            services=(),
            levels=(),
            event_names=(),
            run_id=None,
            trace_id=None,
            span_id=None,
            producer_run_id=None,
            cursor=None,
            limit=1,
        )


def test_log_continuation_uses_frozen_trace_scope_proof_not_current_summary() -> None:
    port = _FrozenLogCursorPort()
    service, _ = _service(port)
    arguments = {
        "principal": _principal(),
        "start_utc": NOW - timedelta(minutes=1),
        "end_utc": NOW + timedelta(minutes=1),
        "services": (),
        "levels": (),
        "event_names": (),
        "run_id": "run:1",
        "trace_id": None,
        "span_id": None,
        "producer_run_id": None,
        "limit": 1,
    }

    first = service.query_logs(**arguments, cursor=None)
    assert first.next_cursor == "retained-cursor"
    port.trace_summary = port.trace_summary.model_copy(update={"run_ids": ("run:1", "run:2")})
    second = service.query_logs(**arguments, cursor=first.next_cursor)

    assert tuple(item.record.log_id for item in first.items + second.items) == (
        "log:first",
        "log:second",
    )


def test_service_rejects_overbroad_queries_before_calling_port() -> None:
    service, port = _service()

    with pytest.raises(QueryTooBroad):
        service.get_trace_spans(
            principal=_principal(),
            trace_id=TRACE_ID,
            cursor=None,
            limit=1001,
        )
    with pytest.raises(QueryTooBroad):
        service.query_logs(
            principal=_principal(),
            start_utc=NOW - timedelta(days=8),
            end_utc=NOW,
            services=(),
            levels=(),
            event_names=(),
            run_id=None,
            trace_id=None,
            span_id=None,
            producer_run_id=None,
            cursor=None,
            limit=10,
        )
    with pytest.raises(QueryTooBroad):
        service.get_run_cost(
            principal=_principal(),
            run_id="run:1",
            cursor=None,
            limit=MAX_COST_USAGE_PAGE_SIZE + 1,
        )

    assert port.authorization_bindings == []


def _client() -> tuple[TestClient, _Port]:
    service, port = _service()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(observability_router(service))
    app.dependency_overrides[require_actor] = _actor
    return TestClient(app), port


def test_router_exposes_the_seven_versioned_bounded_read_endpoints() -> None:
    client, port = _client()
    descriptor = _descriptor()
    start = (NOW - timedelta(minutes=1)).isoformat()
    end = (NOW + timedelta(minutes=1)).isoformat()

    assert client.get(f"/api/v1/traces/{TRACE_ID}").status_code == 200
    run_traces = client.get("/api/v1/runs/run:1/traces", params={"limit": 10})
    assert run_traces.status_code == 200
    spans = client.get(f"/api/v1/traces/{TRACE_ID}/spans", params={"limit": 10})
    assert spans.status_code == 200
    logs = client.get(
        "/api/v1/logs/query",
        params={
            "start_utc": start,
            "end_utc": end,
            "run_id": "run:1",
            "limit": 10,
        },
    )
    assert logs.status_code == 200
    assert client.get("/api/v1/metrics/descriptors").status_code == 200
    metrics = client.get(
        "/api/v1/metrics/query",
        params={
            "descriptor_refs": json.dumps(
                [descriptor.ref.model_dump(mode="json")],
                separators=(",", ":"),
            ),
            "label_matchers": "[]",
            "start_utc": start,
            "end_utc": end,
            "resolution_s": 60,
            "max_points": 100,
            "series_limit": 10,
        },
    )
    assert metrics.status_code == 200, metrics.text
    assert client.get("/api/v1/cost/run:1", params={"limit": 10}).status_code == 200

    assert len(port.authorization_bindings) == 5


def test_service_opens_one_short_read_uow_per_public_call() -> None:
    policies = _Policies()
    port = _Port()
    authorization = ReadAuthorizationService(
        policy_repository=policies,
        role_policy_version=policies.policy.policy_version,
        role_policy_digest=policies.policy.policy_digest,
    )
    entered = 0
    exited = 0

    @contextmanager
    def unit_of_work():
        nonlocal entered, exited
        entered += 1
        try:
            yield ObservabilityReadCapabilities(
                port=port,
                authorization=authorization,
                redactor=_Redactor(),
            )
        finally:
            exited += 1

    service = ObservabilityReadService(unit_of_work=unit_of_work)
    service.get_trace(principal=_principal(), trace_id=TRACE_ID)

    assert entered == exited == 1


def test_trace_span_page_rejects_an_item_from_another_trace() -> None:
    port = _Port()
    alien = _span_page().items[0].span.model_copy(update={"trace_id": "f" * 32})
    port.page_trace_spans = lambda *args, **kwargs: SpanPageV1(  # type: ignore[method-assign]
        trace_id=TRACE_ID,
        items=(SpanViewV1(span=alien),),
        next_cursor=None,
        truncated=False,
    )
    service, _ = _service(port)

    with pytest.raises(IntegrityViolation, match="trace"):
        service.get_trace_spans(
            principal=_principal(),
            trace_id=TRACE_ID,
            cursor=None,
            limit=10,
        )


def test_metric_page_is_verified_against_exact_descriptor_and_time_range() -> None:
    port = _Port()
    descriptor = _descriptor()
    query_start = NOW - timedelta(minutes=1)
    query_end = NOW + timedelta(minutes=1)
    mismatched = MetricSeriesV1(
        descriptor=descriptor.ref,
        metric_name=descriptor.metric_name,
        metric_type="counter",
        unit="token",
        labels={"unexpected": "value"},
        scalar_points=(ScalarMetricSampleV1(ts_utc=query_end, value=1),),
    )

    def invalid_page(query: MetricQueryV1, *, authorization: object) -> MetricPageV1:
        del authorization
        return MetricPageV1(
            series=(mismatched,),
            next_cursor=None,
            coverage_start=query.time_range.start_utc,
            coverage_end=query.time_range.end_utc,
            effective_resolution_s=query.resolution_s,
            truncated=False,
        )

    port.query_metrics = invalid_page  # type: ignore[method-assign]
    service, _ = _service(port)

    with pytest.raises(IntegrityViolation, match="descriptor|time range|label"):
        service.query_metrics(
            principal=_principal(),
            descriptor_refs=(descriptor.ref,),
            start_utc=query_start,
            end_utc=query_end,
            resolution_s=60,
            label_matchers=(),
            max_points=10,
            cursor=None,
            series_limit=10,
        )


def test_router_maps_overbroad_query_to_stable_problem() -> None:
    client, _ = _client()

    response = client.get(
        f"/api/v1/traces/{TRACE_ID}/spans",
        params={"limit": 1001},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "query_too_broad"


def test_platform_observability_read_model_does_not_import_runtime() -> None:
    path = Path(__file__).parents[3] / "gameforge" / "platform" / "read_models" / "observability.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(
        name == "gameforge.runtime" or name.startswith("gameforge.runtime.") for name in imported
    )
