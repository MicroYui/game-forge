from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation, QueryTooBroad
from gameforge.contracts.observability import (
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricLabelMatcherV1,
    MetricPointV1,
    compute_metric_descriptor_digest,
    compute_metric_registry_digest,
)
from gameforge.contracts.slo import (
    MetricPredicateV1,
    SLIDefinitionV1,
    SLODefinitionV1,
    WorkloadProfileV1,
)
from gameforge.platform.slo.evaluator import SLOEvaluator
from gameforge.platform.slo.service import (
    SLODefinitionCapabilities,
    SLODefinitionService,
)
from gameforge.runtime.observability.in_memory import InMemoryTelemetryStore
from gameforge.runtime.observability.local_store import (
    LocalTelemetryRetention,
    LocalTelemetryStore,
)


WINDOW_END = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


@dataclass
class _Clock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current


def _descriptor(*, metric_type: str = "gauge", version: int = 1) -> MetricDescriptorV1:
    payload = {
        "metric_name": "gameforge.provider.request.duration",
        "descriptor_version": version,
        "metric_type": metric_type,
        "unit": "ms",
        "label_keys": ("execution_source", "workload_profile_id"),
        "histogram_bucket_bounds": (100.0, 500.0, 1000.0) if metric_type == "histogram" else (),
        "series_limit": 16,
    }
    return MetricDescriptorV1(
        **payload,
        descriptor_digest=compute_metric_descriptor_digest(payload),
    )


def _registry(
    *descriptors: MetricDescriptorV1,
    version: int = 1,
) -> MetricDescriptorRegistryV1:
    payload = {
        "registry_version": version,
        "descriptors": descriptors,
        "global_series_limit": 32,
    }
    return MetricDescriptorRegistryV1(
        **payload,
        registry_digest=compute_metric_registry_digest(payload),
    )


def _profile(*, task_count: int = 4) -> WorkloadProfileV1:
    return WorkloadProfileV1(
        profile_id="provider-online",
        dataset_artifact_id="artifact-baseline-dataset",
        entity_count=100,
        relation_count=200,
        constraint_count=10,
        task_count=task_count,
        concurrency=1,
        environment_fingerprint="f" * 64,
    )


def _matcher(key: str, value: str) -> MetricLabelMatcherV1:
    return MetricLabelMatcherV1(key=key, operation="eq", values=(value,))


def _definition(
    registry: MetricDescriptorRegistryV1,
    descriptor: MetricDescriptorV1,
    *,
    missing_data: str = "exclude",
    objective: float = 0.75,
    minimum_samples: int = 2,
    late_data_grace_s: int = 60,
) -> SLODefinitionV1:
    labels = (
        _matcher("execution_source", "online"),
        _matcher("workload_profile_id", "provider-online"),
    )
    return SLODefinitionV1(
        slo_id="provider-latency",
        name="Provider latency",
        sli=SLIDefinitionV1(
            metric_registry=registry.ref,
            eligible=MetricPredicateV1(
                descriptor=descriptor.ref,
                allowed_label_matchers=labels,
                comparator="gt",
                threshold=-1,
                unit="ms",
            ),
            good=MetricPredicateV1(
                descriptor=descriptor.ref,
                allowed_label_matchers=labels,
                comparator="lte",
                threshold=500,
                unit="ms",
            ),
            total_aggregation="count",
            workload_profile_id="provider-online",
            missing_data=missing_data,
            late_data_grace_s=late_data_grace_s,
            policy_version="sli@1",
        ),
        objective=objective,
        rolling_window_s=3600,
        minimum_samples=minimum_samples,
        evaluation_interval_s=60,
        effective_from=WINDOW_END - timedelta(days=1),
        policy_version="slo@1",
    )


def _store(
    registry: MetricDescriptorRegistryV1,
    descriptor: MetricDescriptorV1,
    values: tuple[tuple[float, str], ...],
) -> InMemoryTelemetryStore:
    store = InMemoryTelemetryStore(
        clock=_Clock(WINDOW_END + timedelta(minutes=5)),
        signing_key=b"slo-metric-query-key",
    )
    store.register_metric_registry(registry)
    for index, (value, execution_source) in enumerate(values, start=1):
        store.record(
            MetricPointV1(
                point_id=f"point-{index}",
                descriptor=descriptor.ref,
                metric_type=descriptor.metric_type,
                ts_utc=WINDOW_END - timedelta(minutes=index),
                value=value,
                labels={
                    "execution_source": execution_source,
                    "workload_profile_id": "provider-online",
                },
            )
        )
    return store


@dataclass
class _DefinitionRepository:
    events: list[str]
    definitions: dict[str, SLODefinitionV1] = field(default_factory=dict)
    fail_put: bool = False

    def put_definition(self, definition: SLODefinitionV1) -> SLODefinitionV1:
        self.events.append("put")
        if self.fail_put:
            raise RuntimeError("injected authority failure")
        self.definitions[definition.slo_id] = definition
        return definition

    def list_live_definitions(self, *, limit: int) -> tuple[SLODefinitionV1, ...]:
        self.events.append(f"list:{limit}")
        definitions = tuple(self.definitions[key] for key in sorted(self.definitions))
        if len(definitions) > limit:
            raise QueryTooBroad("injected SLO definition reconciliation overflow")
        return definitions


@dataclass
class _DefinitionUnitOfWork:
    events: list[str]

    @contextmanager
    def begin(self):
        self.events.append("begin")
        try:
            yield object()
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")


@dataclass
class _RecordingRetainer:
    store: LocalTelemetryStore
    events: list[str]

    def retain_metric_descriptors(self, **kwargs) -> None:
        self.events.append("pin")
        self.store.retain_metric_descriptors(**kwargs)


@dataclass
class _CapturingRetainer:
    events: list[str]
    calls: list[dict]
    fail: bool = False

    def retain_metric_descriptors(self, **kwargs) -> None:
        self.events.append("pin")
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("injected retention reconciliation failure")


def test_evaluator_uses_exact_registry_and_excludes_replay_duration() -> None:
    descriptor = _descriptor()
    registry = _registry(descriptor)
    store = _store(
        registry,
        descriptor,
        (
            (100, "online"),
            (200, "online"),
            (600, "online"),
            (400, "online"),
            (1, "cassette_replay"),
        ),
    )
    evaluator = SLOEvaluator(
        metric_store=store,
        metric_registry=registry,
        clock=_Clock(WINDOW_END + timedelta(minutes=2)),
    )

    result = evaluator.evaluate(
        definition=_definition(registry, descriptor),
        workload_profile=_profile(),
        window_end=WINDOW_END,
    )

    assert result.eligible_count == 4
    assert result.good_count == 3
    assert result.total_value == 4
    assert result.ratio == 0.75
    assert result.status == "met"
    assert result.missing_count == 0
    assert result.evaluation_id.startswith("slo-evaluation:sha256:")


def test_slo_registration_pins_exact_descriptors_before_authoritative_publish(
    tmp_path,
) -> None:
    clock = _Clock(WINDOW_END)
    retention = LocalTelemetryRetention(metric_descriptors=timedelta(seconds=1))
    telemetry = LocalTelemetryStore(
        tmp_path / "telemetry.sqlite3",
        clock=clock,
        signing_key=b"slo-registration-test-key",
        retention=retention,
    )
    historical = _descriptor(version=1)
    historical_registry = _registry(historical, version=1)
    telemetry.register_metric_registry(historical_registry)
    definition = _definition(historical_registry, historical)
    events: list[str] = []
    repository = _DefinitionRepository(events)
    service = SLODefinitionService(
        descriptor_retainer=_RecordingRetainer(telemetry, events),
        unit_of_work=_DefinitionUnitOfWork(events),
        bind_capabilities=lambda _: SLODefinitionCapabilities(definitions=repository),
    )

    assert service.register(definition) == definition
    assert events == ["pin", "begin", "put", "commit"]

    current = _descriptor(version=2)
    telemetry.register_metric_registry(_registry(current, version=2))
    clock.current += timedelta(seconds=2)
    telemetry.purge_expired()

    assert repository.definitions[definition.slo_id] == definition
    assert telemetry.get_metric_descriptor(historical.ref) == historical


def test_slo_registration_fails_before_authority_when_descriptor_pin_fails(
    tmp_path,
) -> None:
    clock = _Clock(WINDOW_END)
    telemetry = LocalTelemetryStore(
        tmp_path / "telemetry.sqlite3",
        clock=clock,
        signing_key=b"slo-registration-test-key",
    )
    descriptor = _descriptor()
    definition = _definition(_registry(descriptor), descriptor)
    events: list[str] = []
    repository = _DefinitionRepository(events)
    service = SLODefinitionService(
        descriptor_retainer=_RecordingRetainer(telemetry, events),
        unit_of_work=_DefinitionUnitOfWork(events),
        bind_capabilities=lambda _: SLODefinitionCapabilities(definitions=repository),
    )

    with pytest.raises(IntegrityViolation, match="unknown descriptor"):
        service.register(definition)

    assert events == ["pin"]
    assert repository.definitions == {}


def test_slo_registration_keeps_conservative_pin_when_authority_rolls_back(
    tmp_path,
) -> None:
    clock = _Clock(WINDOW_END)
    telemetry = LocalTelemetryStore(
        tmp_path / "telemetry.sqlite3",
        clock=clock,
        signing_key=b"slo-registration-test-key",
        retention=LocalTelemetryRetention(metric_descriptors=timedelta(seconds=1)),
    )
    historical = _descriptor(version=1)
    registry = _registry(historical, version=1)
    telemetry.register_metric_registry(registry)
    events: list[str] = []
    repository = _DefinitionRepository(events, fail_put=True)
    service = SLODefinitionService(
        descriptor_retainer=_RecordingRetainer(telemetry, events),
        unit_of_work=_DefinitionUnitOfWork(events),
        bind_capabilities=lambda _: SLODefinitionCapabilities(definitions=repository),
    )

    with pytest.raises(RuntimeError, match="authority failure"):
        service.register(_definition(registry, historical))

    telemetry.register_metric_registry(_registry(_descriptor(version=2), version=2))
    clock.current += timedelta(seconds=2)
    telemetry.purge_expired()
    assert events == ["pin", "begin", "put", "rollback"]
    assert telemetry.get_metric_descriptor(historical.ref) == historical


def test_slo_retention_reconciliation_reads_bounded_authority_and_pins_once() -> None:
    first = _descriptor(version=1)
    second = _descriptor(version=2)
    first_definition = _definition(_registry(first, version=1), first)
    second_definition = _definition(_registry(second, version=2), second).model_copy(
        update={"slo_id": "provider-throughput", "name": "Provider throughput"}
    )
    events: list[str] = []
    calls: list[dict] = []
    repository = _DefinitionRepository(
        events,
        definitions={
            second_definition.slo_id: second_definition,
            first_definition.slo_id: first_definition,
        },
    )
    service = SLODefinitionService(
        descriptor_retainer=_CapturingRetainer(events, calls),
        unit_of_work=_DefinitionUnitOfWork(events),
        bind_capabilities=lambda _: SLODefinitionCapabilities(definitions=repository),
    )

    assert service.reconcile_retention(max_definitions=2) == 2
    assert events == ["begin", "list:2", "commit", "pin"]
    assert len(calls) == 1
    assert calls[0]["owner_kind"] == "slo"
    assert calls[0]["owner_id"] == "slo-authority-reconciliation@1"
    assert calls[0]["expires_at"] is None
    assert calls[0]["descriptor_refs"] == (first.ref, second.ref)


def test_slo_retention_reconciliation_failure_is_fail_closed() -> None:
    descriptor = _descriptor()
    definition = _definition(_registry(descriptor), descriptor)
    events: list[str] = []
    repository = _DefinitionRepository(
        events,
        definitions={definition.slo_id: definition},
    )
    service = SLODefinitionService(
        descriptor_retainer=_CapturingRetainer(events, [], fail=True),
        unit_of_work=_DefinitionUnitOfWork(events),
        bind_capabilities=lambda _: SLODefinitionCapabilities(definitions=repository),
    )

    with pytest.raises(RuntimeError, match="retention reconciliation"):
        service.reconcile_retention(max_definitions=1)

    assert events == ["begin", "list:1", "commit", "pin"]


def test_slo_retention_reconciliation_refuses_partial_authority_listing() -> None:
    descriptor = _descriptor()
    first = _definition(_registry(descriptor), descriptor)
    second = first.model_copy(update={"slo_id": "provider-throughput"})
    events: list[str] = []
    repository = _DefinitionRepository(
        events,
        definitions={first.slo_id: first, second.slo_id: second},
    )
    service = SLODefinitionService(
        descriptor_retainer=_CapturingRetainer(events, []),
        unit_of_work=_DefinitionUnitOfWork(events),
        bind_capabilities=lambda _: SLODefinitionCapabilities(definitions=repository),
    )

    with pytest.raises(QueryTooBroad, match="overflow"):
        service.reconcile_retention(max_definitions=1)

    assert events == ["begin", "list:1", "rollback"]


def test_evaluator_rejects_mutable_or_unscoped_metric_bindings() -> None:
    descriptor = _descriptor()
    registry = _registry(descriptor)
    store = _store(registry, descriptor, ((100, "online"),))
    evaluator = SLOEvaluator(
        metric_store=store,
        metric_registry=registry,
        clock=_Clock(WINDOW_END + timedelta(minutes=2)),
    )
    definition = _definition(registry, descriptor)
    wrong_registry = definition.model_copy(
        update={
            "sli": definition.sli.model_copy(
                update={
                    "metric_registry": definition.sli.metric_registry.model_copy(
                        update={"registry_digest": "0" * 64}
                    )
                }
            )
        }
    )
    with pytest.raises(IntegrityViolation, match="registry"):
        evaluator.evaluate(
            definition=wrong_registry,
            workload_profile=_profile(),
            window_end=WINDOW_END,
        )

    unscoped = definition.model_copy(
        update={
            "sli": definition.sli.model_copy(
                update={
                    "eligible": definition.sli.eligible.model_copy(
                        update={
                            "allowed_label_matchers": (
                                _matcher("workload_profile_id", "provider-online"),
                            )
                        }
                    )
                }
            )
        }
    )
    with pytest.raises(IntegrityViolation, match="online"):
        evaluator.evaluate(
            definition=unscoped,
            workload_profile=_profile(),
            window_end=WINDOW_END,
        )


@pytest.mark.parametrize(
    ("missing_data", "eligible", "good", "ratio", "status"),
    (
        ("exclude", 2, 2, 1.0, "met"),
        ("bad", 4, 2, 0.5, "breached"),
        ("hold", 2, 2, None, "insufficient_data"),
    ),
)
def test_missing_data_policy_is_explicit(
    missing_data: str,
    eligible: int,
    good: int,
    ratio: float | None,
    status: str,
) -> None:
    descriptor = _descriptor()
    registry = _registry(descriptor)
    store = _store(registry, descriptor, ((100, "online"), (200, "online")))
    evaluator = SLOEvaluator(
        metric_store=store,
        metric_registry=registry,
        clock=_Clock(WINDOW_END + timedelta(minutes=2)),
    )

    result = evaluator.evaluate(
        definition=_definition(registry, descriptor, missing_data=missing_data),
        workload_profile=_profile(task_count=4),
        window_end=WINDOW_END,
        late_count=1,
    )

    assert (result.eligible_count, result.good_count) == (eligible, good)
    assert result.ratio == ratio
    assert result.status == status
    assert result.missing_count == 2
    assert result.late_count == 1


def test_late_grace_minimum_samples_and_objective_are_enforced() -> None:
    descriptor = _descriptor()
    registry = _registry(descriptor)
    store = _store(registry, descriptor, ((100, "online"), (700, "online")))
    definition = _definition(
        registry,
        descriptor,
        objective=0.8,
        minimum_samples=3,
        late_data_grace_s=60,
    )

    during_grace = SLOEvaluator(
        metric_store=store,
        metric_registry=registry,
        clock=_Clock(WINDOW_END + timedelta(seconds=59)),
    ).evaluate(
        definition=definition,
        workload_profile=_profile(task_count=2),
        window_end=WINDOW_END,
    )
    assert during_grace.status == "insufficient_data"
    assert during_grace.ratio is None

    after_grace = SLOEvaluator(
        metric_store=store,
        metric_registry=registry,
        clock=_Clock(WINDOW_END + timedelta(seconds=60)),
    ).evaluate(
        definition=definition,
        workload_profile=_profile(task_count=2),
        window_end=WINDOW_END,
    )
    assert after_grace.status == "insufficient_data"
    assert after_grace.ratio is None

    enough_store = _store(
        registry,
        descriptor,
        ((100, "online"), (200, "online"), (700, "online")),
    )
    breached = SLOEvaluator(
        metric_store=enough_store,
        metric_registry=registry,
        clock=_Clock(WINDOW_END + timedelta(seconds=60)),
    ).evaluate(
        definition=definition,
        workload_profile=_profile(task_count=3),
        window_end=WINDOW_END,
    )
    assert breached.ratio == pytest.approx(2 / 3)
    assert breached.status == "breached"


def test_histogram_predicate_counts_observations_at_exact_bucket_boundary() -> None:
    descriptor = _descriptor(metric_type="histogram")
    registry = _registry(descriptor)
    store = _store(
        registry,
        descriptor,
        ((50, "online"), (250, "online"), (900, "online"), (1500, "online")),
    )
    definition = _definition(registry, descriptor).model_copy(
        update={
            "sli": _definition(registry, descriptor).sli.model_copy(
                update={
                    "eligible": _definition(registry, descriptor).sli.eligible.model_copy(
                        update={"comparator": "lte", "threshold": 1000}
                    )
                }
            )
        }
    )

    result = SLOEvaluator(
        metric_store=store,
        metric_registry=registry,
        clock=_Clock(WINDOW_END + timedelta(minutes=2)),
    ).evaluate(
        definition=definition,
        workload_profile=_profile(task_count=3),
        window_end=WINDOW_END,
    )

    assert result.eligible_count == 3
    assert result.good_count == 2
    assert result.ratio == pytest.approx(2 / 3)
    assert result.status == "breached"
    assert result.evaluation_id == "slo-evaluation:sha256:" + canonical_sha256(
        result.model_dump(mode="json", exclude={"evaluation_id"})
    )


def test_histogram_good_count_is_the_intersection_not_minimum_of_marginals() -> None:
    descriptor = _descriptor(metric_type="histogram")
    registry = _registry(descriptor)
    store = _store(
        registry,
        descriptor,
        ((50, "online"), (250, "online"), (900, "online"), (1500, "online")),
    )
    base = _definition(registry, descriptor)
    definition = base.model_copy(
        update={
            "sli": base.sli.model_copy(
                update={
                    "eligible": base.sli.eligible.model_copy(
                        update={"comparator": "lte", "threshold": 1000}
                    ),
                    "good": base.sli.good.model_copy(update={"comparator": "gt", "threshold": 500}),
                }
            )
        }
    )

    result = SLOEvaluator(
        metric_store=store,
        metric_registry=registry,
        clock=_Clock(WINDOW_END + timedelta(minutes=2)),
    ).evaluate(
        definition=definition,
        workload_profile=_profile(task_count=3),
        window_end=WINDOW_END,
    )

    assert result.eligible_count == 3
    assert result.good_count == 1
    assert result.ratio == pytest.approx(1 / 3)


def test_histogram_sum_uses_eligible_total_without_requiring_good_subset_sum() -> None:
    descriptor = _descriptor(metric_type="histogram")
    registry = _registry(descriptor)
    store = _store(
        registry,
        descriptor,
        ((50, "online"), (250, "online"), (900, "online")),
    )
    base = _definition(registry, descriptor)
    definition = base.model_copy(
        update={
            "sli": base.sli.model_copy(
                update={
                    "eligible": base.sli.eligible.model_copy(
                        update={"comparator": "lte", "threshold": 1000}
                    ),
                    "good": base.sli.good.model_copy(
                        update={"comparator": "lte", "threshold": 500}
                    ),
                    "total_aggregation": "sum",
                }
            )
        }
    )

    result = SLOEvaluator(
        metric_store=store,
        metric_registry=registry,
        clock=_Clock(WINDOW_END + timedelta(minutes=2)),
    ).evaluate(
        definition=definition,
        workload_profile=_profile(task_count=3),
        window_end=WINDOW_END,
    )

    assert result.eligible_count == 3
    assert result.good_count == 2
    assert result.total_value == 1200
    assert result.ratio == pytest.approx(2 / 3)


def test_sum_aggregation_fails_closed_on_floating_point_overflow() -> None:
    descriptor = _descriptor(metric_type="gauge")
    registry = _registry(descriptor)
    store = _store(
        registry,
        descriptor,
        ((1e308, "online"), (1e308, "online")),
    )
    base = _definition(registry, descriptor)
    definition = base.model_copy(
        update={
            "sli": base.sli.model_copy(update={"total_aggregation": "sum"}),
        }
    )

    with pytest.raises(IntegrityViolation, match="overflow|finite"):
        SLOEvaluator(
            metric_store=store,
            metric_registry=registry,
            clock=_Clock(WINDOW_END + timedelta(minutes=2)),
        ).evaluate(
            definition=definition,
            workload_profile=_profile(task_count=2),
            window_end=WINDOW_END,
        )


def test_sum_aggregation_preserves_raw_counter_numerator_and_denominator() -> None:
    def counter(name: str) -> MetricDescriptorV1:
        payload = {
            "metric_name": name,
            "descriptor_version": 1,
            "metric_type": "counter",
            "unit": "count",
            "label_keys": ("execution_source", "workload_profile_id"),
            "histogram_bucket_bounds": (),
            "series_limit": 8,
        }
        return MetricDescriptorV1(
            **payload,
            descriptor_digest=compute_metric_descriptor_digest(payload),
        )

    total = counter("gameforge.provider.request.total")
    good = counter("gameforge.provider.request.good")
    registry = _registry(total, good)
    store = InMemoryTelemetryStore(
        clock=_Clock(WINDOW_END + timedelta(minutes=2)),
        signing_key=b"slo-counter-query-key",
    )
    store.register_metric_registry(registry)
    labels = {
        "execution_source": "online",
        "workload_profile_id": "provider-online",
    }
    for point_id, descriptor, value in (
        ("total", total, 100),
        ("good", good, 95),
    ):
        store.record(
            MetricPointV1(
                point_id=point_id,
                descriptor=descriptor.ref,
                metric_type="counter",
                ts_utc=WINDOW_END - timedelta(minutes=1),
                value=value,
                labels=labels,
            )
        )
    matchers = (
        _matcher("execution_source", "online"),
        _matcher("workload_profile_id", "provider-online"),
    )
    predicate_args = {
        "allowed_label_matchers": matchers,
        "comparator": "gt",
        "threshold": 0,
        "unit": "count",
    }
    definition = SLODefinitionV1(
        slo_id="provider-success",
        name="Provider success ratio",
        sli=SLIDefinitionV1(
            metric_registry=registry.ref,
            eligible=MetricPredicateV1(descriptor=total.ref, **predicate_args),
            good=MetricPredicateV1(descriptor=good.ref, **predicate_args),
            total_aggregation="sum",
            workload_profile_id="provider-online",
            missing_data="exclude",
            late_data_grace_s=0,
            policy_version="sli@1",
        ),
        objective=0.99,
        rolling_window_s=3600,
        minimum_samples=100,
        evaluation_interval_s=60,
        effective_from=WINDOW_END - timedelta(days=1),
        policy_version="slo@1",
    )

    result = SLOEvaluator(
        metric_store=store,
        metric_registry=registry,
        clock=_Clock(WINDOW_END),
    ).evaluate(
        definition=definition,
        workload_profile=_profile(task_count=100),
        window_end=WINDOW_END,
    )

    assert result.eligible_count == 100
    assert result.good_count == 95
    assert result.total_value == 100
    assert result.ratio == 0.95
    assert result.status == "breached"
