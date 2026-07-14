"""Real local adapters for the bounded API read services."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.apps.api.content_persistence import (
    ApprovalEvidenceStateProjector,
    SqlApprovalContentAuthority,
    SqlApprovalPayloadBindingProvider,
    SqlContentReadRepository,
    SqlImmutableArtifactPageProvider,
    SqlRefHistoryReadProvider,
)
from gameforge.apps.api.observability_paging import SqlCostUsagePageAdapter
from gameforge.apps.api.pagination import OpaquePageCursorCodec
from gameforge.apps.api.read_paging import SqlMaterializedPageAdapter
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.execution_profiles import ExecutionProfileCatalogSnapshotV1
from gameforge.contracts.observability import (
    LogErrorV1,
    LogPageV1,
    LogQueryV1,
    LogRecordViewV1,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricPageV1,
    MetricQueryV1,
    SpanPageV1,
    SpanViewV1,
    TraceSummaryPageV1,
    TraceSummaryV1,
)
from gameforge.contracts.storage import ObjectStore, UtcClock
from gameforge.platform.read_models.artifacts import ArtifactPayloadReader
from gameforge.platform.read_models.authorization import (
    ReadAuthorizationBinding,
    ReadAuthorizationService,
)
from gameforge.platform.read_models.content import (
    ContentReadCapabilities,
    ContentReadService,
)
from gameforge.platform.read_models.observability import (
    ObservabilityReadCapabilities,
    ObservabilityReadService,
    RunCostReadPage,
    RunObservabilityScope,
)
from gameforge.platform.read_models.workflows import (
    CurrentApprovalProgressProjector,
    WorkflowReadCapabilities,
    WorkflowReadService,
)
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.observability._fields import (
    is_sensitive_key,
    redact_sensitive_text,
    redact_span_values,
    sanitize_telemetry_value,
)
from gameforge.runtime.observability.local_store import LocalTelemetryStore
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.conflicts import SqlConflictSetRepository
from gameforge.runtime.persistence.cost import SqlCostRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.findings import SqlFindingRepository
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.workflow_reads import SqlWorkflowReadRepository


_SNAPSHOT_TTL = timedelta(minutes=5)
_MAX_MATERIALIZED_ITEMS = 1_000
_MAX_ARTIFACT_PAYLOAD_BYTES = 4 * 1024 * 1024


class _UnavailableAuthority:
    """Fail closed for producer-owned bindings that M4c does not yet persist."""

    def __init__(self, component: str) -> None:
        self._component = component

    def __getattr__(self, method: str):
        def unavailable(*args: object, **kwargs: object) -> Any:
            del args, kwargs
            raise DependencyUnavailable(
                "read authority is unavailable until its producer publishes a binding",
                component=self._component,
                authority_method=method,
            )

        return unavailable


@dataclass(frozen=True, slots=True)
class _PinnedExecutionProfileCatalog:
    catalog: ExecutionProfileCatalogSnapshotV1

    def __post_init__(self) -> None:
        if type(self.catalog) is not ExecutionProfileCatalogSnapshotV1:
            raise TypeError("local execution-profile catalog must be exact v1")

    def current_catalog(self) -> ExecutionProfileCatalogSnapshotV1:
        return self.catalog


class _LocalTelemetryRedactor:
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


class _LocalObservabilityReadPort:
    def __init__(
        self,
        *,
        telemetry: LocalTelemetryStore,
        runs: SqlRunRepository,
        costs: SqlCostRepository,
        cost_pages: SqlCostUsagePageAdapter,
    ) -> None:
        self._telemetry = telemetry
        self._runs = runs
        self._costs = costs
        self._cost_pages = cost_pages

    def get_run_scope(self, run_id: str) -> RunObservabilityScope | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        raise DependencyUnavailable(
            "Run domain scope binding is unavailable",
            component="run_domain_binding",
            run_id=run_id,
        )

    def get_trace_summary(self, trace_id: str) -> TraceSummaryV1 | None:
        return self._telemetry.get_trace_summary(trace_id)

    def page_trace_spans(
        self,
        trace_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: ReadAuthorizationBinding,
    ) -> SpanPageV1:
        return self._telemetry.page_spans(
            trace_id,
            cursor=cursor,
            limit=limit,
            authz_fingerprint=authorization.authz_fingerprint,
            principal_binding=authorization.principal_binding,
        )

    def page_run_traces(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: ReadAuthorizationBinding,
    ) -> TraceSummaryPageV1:
        return self._telemetry.page_run_traces(
            run_id,
            cursor=cursor,
            limit=limit,
            authz_fingerprint=authorization.authz_fingerprint,
            principal_binding=authorization.principal_binding,
        )

    def query_logs(
        self,
        query: LogQueryV1,
        *,
        authorization: ReadAuthorizationBinding,
    ) -> LogPageV1:
        return self._telemetry.query_logs(
            query,
            principal_binding=authorization.principal_binding,
        )

    def get_metric_descriptor_registry(self) -> MetricDescriptorRegistryV1 | None:
        return self._telemetry.get_metric_descriptor_registry()

    def resolve_metric_descriptors(
        self,
        refs: tuple[MetricDescriptorRefV1, ...],
    ) -> tuple[MetricDescriptorV1, ...]:
        return self._telemetry.resolve_metric_descriptors(refs)

    def query_metrics(
        self,
        query: MetricQueryV1,
        *,
        authorization: ReadAuthorizationBinding,
    ) -> MetricPageV1:
        return self._telemetry.query_metrics(
            query,
            principal_binding=authorization.principal_binding,
        )

    def get_run_cost(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authorization: ReadAuthorizationBinding,
        query_hash: str,
    ) -> RunCostReadPage | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        budget_set = self._costs.get_budget_set(run.budget_set_snapshot_id)
        if budget_set is None:
            raise IntegrityViolation(
                "Run budget-set snapshot is unavailable",
                run_id=run_id,
            )
        return self._cost_pages.page(
            run_id=run_id,
            budget_set=budget_set,
            cursor=cursor,
            limit=limit,
            authorization=authorization,
            query_hash=query_hash,
        )


@dataclass(frozen=True, slots=True)
class LocalReadServices:
    content: ContentReadService
    workflows: WorkflowReadService
    observability: ObservabilityReadService


def build_local_read_services(
    *,
    engine: Engine,
    object_store: ObjectStore,
    object_store_id: str,
    telemetry_store: LocalTelemetryStore,
    role_policy_version: str,
    role_policy_digest: str,
    execution_profile_catalog: ExecutionProfileCatalogSnapshotV1,
    cursor_signing_key: bytes,
    clock: UtcClock | None = None,
) -> LocalReadServices:
    """Bind each public read to one short SQLite transaction/request scope."""

    selected_clock = clock or SystemUtcClock()
    cursor_signer = CursorSigner(
        signing_key=cursor_signing_key,
        clock=selected_clock,
    )
    cursor_codec = OpaquePageCursorCodec()

    @contextmanager
    def session_scope():
        session = Session(engine)
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def authorization(session: Session) -> ReadAuthorizationService:
        return ReadAuthorizationService(
            policy_repository=SqlPolicySnapshotRepository(session, clock=selected_clock),
            role_policy_version=role_policy_version,
            role_policy_digest=role_policy_digest,
        )

    def page_factory(session: Session):
        return lambda page_size: SqlMaterializedPageAdapter(
            session,
            cursor_signer=cursor_signer,
            clock=selected_clock,
            page_size=page_size,
            snapshot_ttl=_SNAPSHOT_TTL,
            max_materialized_items=_MAX_MATERIALIZED_ITEMS,
        )

    @contextmanager
    def content_uow():
        with session_scope() as session:
            unavailable = _UnavailableAuthority("content_producer_binding")
            approvals = SqlApprovalRepository(session)
            object_bindings = SqlObjectBindingRepository(
                session,
                object_store,
                object_store_id,
            )
            artifacts = SqlArtifactRepository(
                session,
                binding_repository=object_bindings,
                cursor_signer=cursor_signer,
                clock=selected_clock,
                snapshot_ttl=_SNAPSHOT_TTL,
            )
            refs = SqlRefStore(
                session,
                cursor_signer=cursor_signer,
                clock=selected_clock,
                snapshot_ttl=_SNAPSHOT_TTL,
            )
            payload_bindings = SqlApprovalPayloadBindingProvider(
                session,
                approvals=approvals,
                artifacts=artifacts,
            )
            payload_reader = ArtifactPayloadReader(
                artifacts=artifacts,
                trusted_bindings=payload_bindings,
                object_bindings=object_bindings,
                object_store=object_store,
                max_payload_bytes=_MAX_ARTIFACT_PAYLOAD_BYTES,
            )
            approval_authority = SqlApprovalContentAuthority(
                session,
                approvals=approvals,
                evidence=ApprovalEvidenceStateProjector(
                    artifacts=artifacts,
                    payload_reader=payload_reader,
                ),
            )
            yield ContentReadCapabilities(
                repository=SqlContentReadRepository(artifacts),
                immutable_artifact_pages=SqlImmutableArtifactPageProvider(
                    session,
                    artifacts=artifacts,
                    cursor_signer=cursor_signer,
                    clock=selected_clock,
                    snapshot_ttl=_SNAPSHOT_TTL,
                ),
                payload_reader=payload_reader,
                payload_bindings=payload_bindings,
                authorization=authorization(session),
                permission_resolver=approval_authority,
                specs=unavailable,
                schema_registry=unavailable,
                proposal_workflows=approval_authority,
                subject_workflows=approval_authority,
                playtest_results=unavailable,
                refs=SqlRefHistoryReadProvider(
                    session,
                    refs=refs,
                    cursor_signer=cursor_signer,
                    clock=selected_clock,
                    snapshot_ttl=_SNAPSHOT_TTL,
                ),
                diffs=unavailable,
                bench_reports=unavailable,
                execution_profiles=_PinnedExecutionProfileCatalog(execution_profile_catalog),
                page_factory=page_factory(session),
            )

    @contextmanager
    def workflow_uow():
        with session_scope() as session:
            policies = SqlPolicySnapshotRepository(session, clock=selected_clock)
            identities = SqlIdentityRepository(session, clock=selected_clock)
            runs = SqlRunRepository(session)
            findings = SqlFindingRepository(
                session,
                cursor_signer=cursor_signer,
                clock=selected_clock,
                snapshot_ttl=_SNAPSHOT_TTL,
            )
            conflicts = SqlConflictSetRepository(
                session,
                cursor_signer=cursor_signer,
                clock=selected_clock,
                snapshot_ttl=_SNAPSHOT_TTL,
            )
            approvals = SqlApprovalRepository(session)
            yield WorkflowReadCapabilities(
                repository=SqlWorkflowReadRepository(
                    session,
                    approvals=approvals,
                    runs=runs,
                    findings=findings,
                    conflicts=conflicts,
                ),
                authorization=ReadAuthorizationService(
                    policy_repository=policies,
                    role_policy_version=role_policy_version,
                    role_policy_digest=role_policy_digest,
                ),
                permission_resolver=_UnavailableAuthority("workflow_domain_binding"),
                approval_projector=CurrentApprovalProgressProjector(
                    policy_repository=policies,
                    role_policy_version=role_policy_version,
                    role_policy_digest=role_policy_digest,
                    principal_resolver=identities.project,
                ),
                page_factory=page_factory(session),
            )

    @contextmanager
    def observability_uow():
        with session_scope() as session:
            runs = SqlRunRepository(session)
            costs = SqlCostRepository(session)
            cost_pages = SqlCostUsagePageAdapter(
                repository=costs,
                page_factory=page_factory(session),
                cursor_codec=cursor_codec,
                max_materialized_items=_MAX_MATERIALIZED_ITEMS,
            )
            yield ObservabilityReadCapabilities(
                port=_LocalObservabilityReadPort(
                    telemetry=telemetry_store,
                    runs=runs,
                    costs=costs,
                    cost_pages=cost_pages,
                ),
                authorization=authorization(session),
                redactor=_LocalTelemetryRedactor(),
            )

    return LocalReadServices(
        content=ContentReadService(
            uow_factory=content_uow,
            max_materialized_items=_MAX_MATERIALIZED_ITEMS,
        ),
        workflows=WorkflowReadService(
            unit_of_work=workflow_uow,
            max_materialized_items=_MAX_MATERIALIZED_ITEMS,
        ),
        observability=ObservabilityReadService(unit_of_work=observability_uow),
    )


__all__ = ["LocalReadServices", "build_local_read_services"]
