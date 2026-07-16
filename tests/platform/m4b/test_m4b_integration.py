from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from gameforge.contracts.cassette import CassetteRecordV2
from gameforge.contracts.cost import (
    BudgetReservationV1,
    CacheHitObservationV1,
    LatencyObservationV1,
    MonetaryObservationV1,
    ReservationGroupV1,
    TokenUsageObservationV1,
    UsageEntryV1,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.model_router import ModelRequestV2, request_hash
from gameforge.contracts.observability import (
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricQueryV1,
    TimeRangeV1,
    TraceContextV1,
    compute_metric_descriptor_digest,
    compute_metric_registry_digest,
)
from gameforge.contracts.reliability import (
    CircuitBreakerConfigV1,
    FailureClassificationV1,
    RetryPolicyV1,
)
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.contracts.slo import AlertRuleV1
from gameforge.platform.cost_policy.routing import RouteRequest, RoutingPolicyService
from gameforge.platform.slo.alerts import AlertStateMachine
from gameforge.platform.slo.evaluator import SLOEvaluator
from gameforge.platform.slo.repository import InMemoryAlertStateRepository
from gameforge.runtime.cassette.legacy_import import (
    InMemoryLegacyImportDecisionRepository,
    LegacyCassetteRuntimeImporter,
)
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.clock import FrozenUtcClock, ManualMonotonicClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.model_router.cache import ExactResponseCache
from gameforge.runtime.model_router.m4_router import M4ModelRouter, VerifiedLegacyReplayRouter
from gameforge.runtime.model_router.router import RouterMode
from gameforge.runtime.model_router.typed_transport import TransportResponseV2
from gameforge.runtime.observability.context import TraceCarrier, use_trace_context
from gameforge.runtime.observability.exporters import InMemoryExporter
from gameforge.runtime.observability.in_memory import InMemoryTelemetryStore
from gameforge.runtime.observability.metrics import MetricRegistrySink
from gameforge.runtime.observability.trace import Tracer
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from gameforge.runtime.reliability.breaker import CircuitBreaker
from gameforge.runtime.reliability.retry import RetryAttemptResult, RetryExecutor
from gameforge.runtime.slo.sinks import InMemoryAlertSink
from tests.platform.m4b.test_routing_policy import (
    MODEL_A,
    MODEL_B,
    SNAPSHOT_A,
    SNAPSHOT_B,
    _catalog,
    _descriptor,
    _model_request,
    _policy,
    _rule,
)
from tests.platform.m4b.test_slo import _definition as _slo_definition
from tests.platform.m4b.test_slo import _profile as _workload_profile
from tests.runtime.cassette.test_legacy_import import (
    _authority as _legacy_authority,
)
from tests.runtime.cassette.test_legacy_import import _candidate as _legacy_candidate
from tests.runtime.cassette.test_legacy_import import _finalize as _finalize_legacy
from tests.runtime.cassette.test_legacy_import import _request as _legacy_request
from tests.runtime.cost.ledger_testkit import (
    NOW,
    amount,
    budget,
    budget_set,
    seed_current_attempt,
    uow,
)
from tests.runtime.model_router.test_routing_v2 import (
    _DecisionAuthority,
    _route_key,
)
from tests.runtime.observability.test_trace import _DeterministicIds
from tests.runtime.persistence.test_run_repository import (
    _capabilities as _run_capabilities,
)
from tests.runtime.persistence.test_run_repository import _queued_event, _run


class _Transient(Exception):
    pass


@dataclass
class _Clock:
    current: datetime = NOW

    def now_utc(self) -> datetime:
        return self.current


class _Sleeper:
    def __init__(self, clock: _Clock, monotonic: ManualMonotonicClock) -> None:
        self._clock = clock
        self._monotonic = monotonic

    def sleep(self, seconds: float) -> None:
        self._clock.current += timedelta(seconds=seconds)
        self._monotonic.advance_ns(round(seconds * 1_000_000_000))


class _Classifier:
    version = "classifier@1"

    def classify(self, error: BaseException) -> FailureClassificationV1:
        return FailureClassificationV1(
            failure_kind="transient_infrastructure"
            if isinstance(error, _Transient)
            else "validation",
            retryable=isinstance(error, _Transient),
            counts_for_breaker=isinstance(error, _Transient),
            idempotency_required=isinstance(error, _Transient),
            reason_code="gateway_transient" if isinstance(error, _Transient) else "invalid",
        )


class _Transport:
    def __init__(
        self,
        outcomes: list[TransportResponseV2 | BaseException],
        monotonic: ManualMonotonicClock,
    ) -> None:
        self._outcomes = outcomes
        self._monotonic = monotonic
        self.calls = 0

    def complete(self, request: ModelRequestV2) -> TransportResponseV2:
        del request
        outcome = self._outcomes[self.calls]
        self.calls += 1
        self._monotonic.advance_ns(11)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _tracer(
    exporter: InMemoryExporter,
    monotonic: ManualMonotonicClock,
    *,
    span_ids: list[str] | None = None,
) -> Tracer:
    return Tracer(
        exporter=exporter,
        id_generator=_DeterministicIds(
            trace_ids=["1" * 32],
            span_ids=span_ids or ["2" * 16, "3" * 16],
        ),
        utc_clock=FrozenUtcClock(NOW),
        monotonic_clock=monotonic,
        resource={"service.name": "gameforge-m4b-integration"},
    )


def _metric_descriptor(
    name: str,
    metric_type: str,
    unit: str,
    labels: tuple[str, ...],
) -> MetricDescriptorV1:
    payload = {
        "metric_name": name,
        "descriptor_version": 1,
        "metric_type": metric_type,
        "unit": unit,
        "label_keys": labels,
        "histogram_bucket_bounds": (),
        "series_limit": 32,
    }
    return MetricDescriptorV1(
        **payload,
        descriptor_digest=compute_metric_descriptor_digest(payload),
    )


def _metric_registry(*descriptors: MetricDescriptorV1) -> MetricDescriptorRegistryV1:
    payload = {
        "registry_version": 1,
        "descriptors": descriptors,
        "global_series_limit": 64,
    }
    return MetricDescriptorRegistryV1(
        **payload,
        registry_digest=compute_metric_registry_digest(payload),
    )


def _hold(selected_set) -> tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]]:
    group_id = f"hold:{selected_set.run_id}:integration"
    reserved = (
        amount("input_token", 80),
        amount("output_token", 40),
        amount("request", 5),
        amount("wall_time_ns", 500_000_000),
    )
    members = tuple(
        BudgetReservationV1(
            reservation_id=f"reservation:{group_id}:{snapshot.budget_id}",
            reservation_group_id=group_id,
            budget_id=snapshot.budget_id,
            reserved=reserved,
            status="reserved",
            revision=1,
        )
        for snapshot in selected_set.snapshots
    )
    return (
        ReservationGroupV1(
            reservation_group_id=group_id,
            scope="run_budget_hold",
            run_id=selected_set.run_id,
            budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
            request_hash="sha256:" + "a" * 64,
            idempotency_key=f"hold:{selected_set.run_id}",
            budget_reservation_ids=tuple(item.reservation_id for item in members),
            status="reserved",
            revision=1,
            created_at=NOW,
        ),
        members,
    )


def _call_group(selected_set, parent, decision, transport_attempt: int):
    group_id = f"call:{selected_set.run_id}:{transport_attempt}"
    members = tuple(
        BudgetReservationV1(
            reservation_id=f"reservation:{group_id}:{snapshot.budget_id}",
            reservation_group_id=group_id,
            budget_id=snapshot.budget_id,
            reserved=(
                amount("input_token", 20),
                amount("output_token", 10),
                amount("request", 1),
                amount("wall_time_ns", 1_000),
            ),
            status="reserved",
            revision=1,
        )
        for snapshot in selected_set.snapshots
    )
    return (
        ReservationGroupV1(
            reservation_group_id=group_id,
            scope="attempt_call",
            run_id=selected_set.run_id,
            budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
            parent_hold_group_id=parent.reservation_group_id,
            attempt_no=1,
            request_hash=decision.request_hash,
            transport_attempt=transport_attempt,
            fencing_token=1,
            idempotency_key=group_id,
            budget_reservation_ids=tuple(item.reservation_id for item in members),
            status="reserved",
            revision=1,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        ),
        members,
    )


def _usage(
    group: ReservationGroupV1,
    decision: RoutingDecisionV1,
    *,
    usage_id: str,
    input_tokens: int,
    output_tokens: int,
    wall_time_ns: int,
) -> UsageEntryV1:
    return UsageEntryV1(
        usage_id=usage_id,
        reservation_group_id=group.reservation_group_id,
        budget_reservation_ids=group.budget_reservation_ids,
        scope="attempt_call",
        run_id=group.run_id,
        attempt_no=1,
        request_hash=group.request_hash,
        transport_attempt=group.transport_attempt,
        execution_source="online",
        provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
        retry_index=(group.transport_attempt or 1) - 1,
        token_usage=TokenUsageObservationV1(
            status="reported",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
        latency=LatencyObservationV1(status="unavailable"),
        wall_time_ns=wall_time_ns,
        monetary=MonetaryObservationV1(status="unavailable"),
        routing_decision_kind="native",
        routing_decision_id=decision.decision_id,
        fencing_token_at_reserve=1,
        recorded_at=NOW + timedelta(seconds=1),
    )


def test_db_carrier_reopen_preserves_parentage_without_entering_payload_hash(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'carrier.db'}"
    migrations_api.upgrade(url, "head")
    engine = get_engine(url)
    api_exporter = InMemoryExporter(capacity=4)
    incoming = TraceContextV1(
        trace_id="a" * 32,
        span_id="b" * 16,
        trace_flags="01",
        trace_state="vendor=state",
    )
    run = _run()
    original_payload_hash = run.payload_hash
    with use_trace_context(incoming):
        with _tracer(api_exporter, ManualMonotonicClock()).span("api.create") as api_span:
            persisted = run.model_copy(
                update={"dispatch_trace_carrier": TraceCarrier.inject(api_span.context)}
            )
            with SqliteUnitOfWork(engine, _run_capabilities).begin() as transaction:
                transaction.runs.create_queued(persisted, _queued_event(persisted))

    engine.dispose()
    reopened_engine = get_engine(url)
    with Session(reopened_engine) as session:
        reopened = SqlRunRepository(session).get(run.run_id)
    assert reopened is not None and reopened.dispatch_trace_carrier is not None
    assert reopened.payload == run.payload
    assert reopened.payload_hash == original_payload_hash
    assert reopened.request_hash == run.request_hash

    extracted = TraceCarrier.extract(reopened.dispatch_trace_carrier)
    assert extracted is not None
    worker_exporter = InMemoryExporter(capacity=2)
    worker_clock = ManualMonotonicClock()
    with use_trace_context(extracted):
        with _tracer(worker_exporter, worker_clock, span_ids=["4" * 16]).span(
            "worker.execute", attributes={"run_id": reopened.run_id}
        ) as worker_span:
            assert worker_span.context.trace_flags == "01"
            assert worker_span.context.trace_state == "vendor=state"
            worker_clock.advance_ns(17)
    child = worker_exporter.spans[0]
    assert child.trace_id == api_exporter.spans[0].trace_id
    assert child.parent_span_id == api_exporter.spans[0].span_id
    assert child.span_id != child.parent_span_id
    assert child.duration_ns == 17
    reopened_engine.dispose()


def test_native_route_retry_cost_replay_metrics_and_slo_are_one_correlatable_chain(
    tmp_path: Path,
) -> None:
    engine = get_engine(f"sqlite:///{tmp_path / 'integration.db'}")
    migrations_api.upgrade(str(engine.url), "head")
    selected_budget = budget("run", "run-1").model_copy(
        update={
            "limits": (
                amount("input_token", 100),
                amount("output_token", 50),
                amount("request", 10),
                amount("wall_time_ns", 1_000_000_000),
                amount("concurrent_run", 1),
            )
        }
    )
    selected_set = budget_set("run-1", (selected_budget,))
    parent, parent_members = _hold(selected_set)
    catalog = _catalog(_descriptor(MODEL_A), _descriptor(MODEL_B))
    policy = _policy(catalog, _rule())
    routing = RoutingPolicyService(catalog=catalog, policy=policy)
    intent = RouteRequest(
        run_id="run-1",
        attempt_no=1,
        task_kind="patch_repair",
        domain_scope=DomainScope(domain_ids=("default",)),
        budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
        remaining_budget=(amount("input_token", 80),),
        context_tokens=1_000,
        max_output_tokens=1_000,
    )
    model_request = _model_request(SNAPSHOT_A)
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
        transaction.cost.put_model_catalog(catalog)
        transaction.cost.put_routing_policy(policy)
        primary = routing.select(intent)
        decision = routing.decide_and_record(
            intent,
            model_request=model_request,
            repository=transaction.cost,
            execution_source="online",
            decided_at=NOW,
            selection=primary,
        )
        fallback = routing.next_fallback(primary, request=intent)
        fallback_decision = routing.decide_and_record(
            intent,
            model_request=_model_request(SNAPSHOT_B),
            repository=transaction.cost,
            execution_source="online",
            decided_at=NOW,
            selection=fallback,
        )
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)

    clock = _Clock()
    monotonic = ManualMonotonicClock()
    retry = RetryExecutor(
        policy=RetryPolicyV1(
            policy_version="retry@1",
            failure_classifier_version="classifier@1",
            max_attempts=2,
            initial_backoff_ms=1,
            max_backoff_ms=1,
            multiplier=1,
            jitter_ratio=0,
        ),
        classifier=_Classifier(),
        utc_clock=clock,
        monotonic_clock=monotonic,
        sleeper=_Sleeper(clock, monotonic),
        jitter=lambda: 0,
    )
    breaker = CircuitBreaker(
        dependency_id="model-gateway",
        config=CircuitBreakerConfigV1(
            config_version="breaker@1",
            rolling_window_s=60,
            minimum_samples=2,
            failure_threshold=1,
            open_cooldown_s=10,
            half_open_max_concurrent_probes=1,
            half_open_success_threshold=1,
        ),
        clock=clock,
    )
    response = TransportResponseV2(
        response_normalized="fixed",
        raw_response={"id": "response-1"},
        finish_reason="stop",
        tool_calls=(),
        latency=LatencyObservationV1(status="reported", provider_latency_ms=120),
        token_usage=TokenUsageObservationV1(
            status="reported", input_tokens=10, output_tokens=2, total_tokens=12
        ),
        provider_prefix_cache=CacheHitObservationV1(status="reported", hit=True),
    )
    groups: dict[int, ReservationGroupV1] = {}
    observed: list[RetryAttemptResult] = []

    def admit(transport_attempt: int) -> None:
        group, members = _call_group(selected_set, parent, decision, transport_attempt)
        with uow(engine).begin() as transaction:
            transaction.cost.reserve_many(group, members)
        groups[transport_attempt] = group

    def observe(result: RetryAttemptResult) -> None:
        observed.append(result)
        if not result.succeeded:
            group = groups[result.attempt_no]
            conservative = _usage(
                group,
                decision,
                usage_id=f"usage:{result.attempt_no}",
                input_tokens=20,
                output_tokens=10,
                wall_time_ns=result.duration_ns,
            )
            with uow(engine).begin() as transaction:
                transaction.cost.hold_unknown_group(group.reservation_group_id)
                transaction.cost.settle_unknown_group(group.reservation_group_id, conservative)

    cassette = CassetteStore(tmp_path / "cassettes")
    cache = ExactResponseCache()
    router = M4ModelRouter(
        transport=_Transport([_Transient("retry"), response], monotonic),
        store=cassette,
        cache=cache,
        mode=RouterMode.RECORD,
        retry_executor=retry,
        decision_authority=_DecisionAuthority(decision),
        circuit_breaker=breaker,
        attempt_admission=admit,
        attempt_observer=observe,
    )
    route_key = _route_key(decision)
    result = router.call(
        model_request,
        decision=decision,
        deadline_utc=NOW + timedelta(seconds=10),
        recorded_at=NOW,
        cassette_route_key=route_key,
    )
    with uow(engine).begin() as transaction:
        transaction.cost.reconcile_group(
            _usage(
                groups[2],
                decision,
                usage_id="usage:2",
                input_tokens=10,
                output_tokens=2,
                wall_time_ns=observed[1].duration_ns,
            )
        )

    recorded = cassette.replay_native(route_key)
    assert isinstance(recorded, CassetteRecordV2)
    replay_decision = RoutingDecisionV1.create(
        run_id="run-replay",
        attempt_no=1,
        request_hash=request_hash(model_request),
        rule_id=decision.rule_id,
        model_snapshot=decision.model_snapshot,
        tier=decision.tier,
        reason_code="recorded_replay",
        budget_set_snapshot_id="budget-set:replay",
        fallback_from=None,
        fallback_index=0,
        policy_version=decision.policy_version,
        routing_policy_digest=decision.routing_policy_digest,
        catalog_version=decision.catalog_version,
        catalog_digest=decision.catalog_digest,
        execution_source="cassette_replay",
        decided_at=NOW,
    )
    replay_router = M4ModelRouter(
        transport=_Transport([], monotonic),
        store=cassette,
        cache=ExactResponseCache(),
        mode=RouterMode.REPLAY,
        retry_executor=retry,
        decision_authority=_DecisionAuthority(replay_decision),
    )
    trace_exporter = InMemoryExporter(capacity=2)
    replay_monotonic = ManualMonotonicClock()
    with _tracer(trace_exporter, replay_monotonic).span(
        "model.replay", attributes={"run_id": "run-replay"}
    ) as span:
        replay_result = replay_router.call(
            model_request,
            decision=replay_decision,
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
            cassette_route_key=route_key,
        )
        span.set_attribute(
            "recorded_provider_latency_ms",
            replay_result.latency.provider_latency_ms,
        )
        replay_monotonic.advance_ns(7)

    accounting_metric = _metric_descriptor(
        "gameforge.model.accounting", "counter", "count", ("measure", "execution_source")
    )
    provider_metric = _metric_descriptor(
        "gameforge.provider.request.duration",
        "gauge",
        "ms",
        ("execution_source", "workload_profile_id"),
    )
    replay_metric = _metric_descriptor(
        "gameforge.replay.execution.duration",
        "gauge",
        "ns",
        ("execution_source", "workload_profile_id"),
    )
    registry = _metric_registry(accounting_metric, provider_metric, replay_metric)
    metric_clock = FrozenUtcClock(NOW - timedelta(minutes=1))
    metric_store = InMemoryTelemetryStore(clock=metric_clock, signing_key=b"integration")
    metric_store.register_metric_registry(registry)
    ids = iter(f"point-{index}" for index in range(20))
    sink = MetricRegistrySink(
        registry=registry,
        store=metric_store,
        clock=metric_clock,
        id_generator=ids.__next__,
    )
    counter = sink.counter(accounting_metric.ref)
    for measure, source, value in (
        ("logical_call", "online", 1),
        ("transport_attempt", "online", result.transport_attempt_count),
        ("transport_retry", "online", result.transport_retry_count),
        ("prefix_cache_hit", "online", 1),
        ("logical_call", "cassette_replay", 1),
        ("recorded_attempt", "cassette_replay", replay_result.recorded_transport_attempt_count),
        ("recorded_retry", "cassette_replay", replay_result.recorded_transport_retry_count),
    ):
        counter.add(value or 0, labels={"measure": measure, "execution_source": source})
    sink.gauge(provider_metric.ref).set(
        120,
        labels={"execution_source": "online", "workload_profile_id": "provider-online"},
    )
    sink.gauge(replay_metric.ref).set(
        trace_exporter.spans[0].duration_ns,
        labels={
            "execution_source": "cassette_replay",
            "workload_profile_id": "provider-online",
        },
    )

    evaluation = SLOEvaluator(
        metric_store=metric_store,
        metric_registry=registry,
        clock=FrozenUtcClock(NOW + timedelta(minutes=2)),
    ).evaluate(
        definition=_slo_definition(
            registry,
            provider_metric,
            missing_data="bad",
            objective=0.75,
            minimum_samples=2,
            late_data_grace_s=0,
        ),
        workload_profile=_workload_profile(task_count=2),
        window_end=NOW,
        late_count=1,
    )
    definition = _slo_definition(
        registry,
        provider_metric,
        missing_data="bad",
        objective=0.75,
        minimum_samples=2,
        late_data_grace_s=0,
    )
    rule = AlertRuleV1(
        alert_rule_id="provider-latency-alert",
        slo_id=definition.slo_id,
        breach_threshold=1,
        for_duration_s=0,
        severity="critical",
        dedup_key_template="{slo_id}:{severity}",
        cooldown_s=60,
        insufficient_data_action="hold",
        policy_version="alert@1",
    )
    alerts = InMemoryAlertStateRepository()
    machine = AlertStateMachine(
        repository=alerts,
        sink=InMemoryAlertSink(),
        clock=FrozenUtcClock(NOW + timedelta(minutes=2)),
    )
    firing = machine.process(
        definition=definition,
        rule=rule,
        evaluation=evaluation,
        expected_revision=None,
    )
    replayed_alert = machine.process(
        definition=definition,
        rule=rule,
        evaluation=evaluation,
        expected_revision=None,
    )

    query = MetricQueryV1(
        descriptor_refs=(accounting_metric.ref,),
        time_range=TimeRangeV1(start_utc=NOW - timedelta(minutes=2), end_utc=NOW),
        resolution_s=60,
        label_matchers=(),
        max_points=32,
        series_limit=32,
        authz_fingerprint="a" * 64,
    )
    metric_values = {
        (series.labels["measure"], series.labels["execution_source"]): (
            series.scalar_points[0].value
        )
        for series in metric_store.query_metrics(query).series
    }
    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        retained_decision = ledger.get_routing_decision(decision.decision_id)
        usages = ledger.list_usage(run_id="run-1")
    assert retained_decision == decision
    assert fallback_decision.fallback_from == MODEL_A and fallback_decision.fallback_index == 1
    assert [item.routing_decision_id for item in usages] == [decision.decision_id] * 2
    assert all(len(item.budget_reservation_ids) == 1 for item in usages)
    assert metric_values[("logical_call", "online")] == 1
    assert metric_values[("transport_attempt", "online")] == 2
    assert metric_values[("transport_retry", "online")] == 1
    assert trace_exporter.spans[0].duration_ns == 7
    assert trace_exporter.spans[0].attributes["recorded_provider_latency_ms"] == 120
    assert evaluation.status == "breached" and evaluation.late_count == 1
    assert firing.instance is not None and firing.instance.state == "firing"
    assert replayed_alert.instance == firing.instance and replayed_alert.delivery is None
    assert breaker.snapshot().state == "closed"
    engine.dispose()


def test_verified_legacy_replay_stays_explicitly_non_native() -> None:
    importer = LegacyCassetteRuntimeImporter(_legacy_authority())
    prepared = importer.prepare(_legacy_candidate())
    repository = InMemoryLegacyImportDecisionRepository()
    tree = _finalize_legacy(importer, prepared, repository)
    assert tree.replay_source is not None
    manifest = tree.root.legacy_run_import_manifest
    assert manifest is not None
    result = VerifiedLegacyReplayRouter(
        source=tree.replay_source,
        expected_import_id=manifest.import_id,
    ).call(_legacy_request(), call_ordinal=1)
    assert tree.root.run_id is None
    assert result.execution_source == "cassette_replay"
    assert result.routing_decision_kind == "legacy_import"
    assert result.transport_attempt_count == 0
    assert result.recorded_transport_attempt_count == 1
    assert result.latency.status == "unavailable"
