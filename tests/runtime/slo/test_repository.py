from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, IntegrityViolation, QueryTooBroad
from gameforge.contracts.observability import (
    MetricDescriptorRefV1,
    MetricDescriptorRegistryRefV1,
)
from gameforge.contracts.slo import (
    AlertInstanceV1,
    AlertRuleV1,
    MetricPredicateV1,
    SLIDefinitionV1,
    SLODefinitionV1,
    SLOEvaluationV1,
    WorkloadProfileV1,
)
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.slo import SqlSloRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def _profile() -> WorkloadProfileV1:
    return WorkloadProfileV1(
        profile_id="checker-baseline",
        dataset_artifact_id="artifact-checker-baseline",
        entity_count=100,
        relation_count=200,
        constraint_count=10,
        task_count=50,
        concurrency=2,
        environment_fingerprint="a" * 64,
    )


def _definition() -> SLODefinitionV1:
    descriptor = MetricDescriptorRefV1(
        metric_name="gameforge.checker.duration",
        descriptor_version=1,
        descriptor_digest="b" * 64,
    )
    predicate = MetricPredicateV1(
        descriptor=descriptor,
        allowed_label_matchers=(),
        comparator="lte",
        threshold=100,
        unit="ms",
    )
    return SLODefinitionV1(
        slo_id="checker-latency",
        name="Checker latency",
        sli=SLIDefinitionV1(
            metric_registry=MetricDescriptorRegistryRefV1(
                registry_version=1,
                registry_digest="c" * 64,
            ),
            eligible=predicate,
            good=predicate,
            total_aggregation="count",
            workload_profile_id="checker-baseline",
            missing_data="hold",
            late_data_grace_s=60,
            policy_version="sli@1",
        ),
        objective=0.99,
        rolling_window_s=3600,
        minimum_samples=50,
        evaluation_interval_s=60,
        effective_from=NOW - timedelta(days=1),
        policy_version="slo@1",
    )


def _rule() -> AlertRuleV1:
    return AlertRuleV1(
        alert_rule_id="checker-latency-page",
        slo_id="checker-latency",
        breach_threshold=1,
        for_duration_s=300,
        severity="critical",
        dedup_key_template="{slo_id}:{severity}",
        cooldown_s=900,
        insufficient_data_action="hold",
        policy_version="alert@1",
    )


def _evaluation(*, offset: int = 0, ratio: float = 0.95) -> SLOEvaluationV1:
    return SLOEvaluationV1.create(
        slo_id="checker-latency",
        window_start=NOW - timedelta(hours=1) + timedelta(seconds=offset),
        window_end=NOW + timedelta(seconds=offset),
        eligible_count=100,
        good_count=round(100 * ratio),
        total_value=100,
        ratio=ratio,
        missing_count=0,
        late_count=0,
        status="met" if ratio >= 0.99 else "breached",
    )


def _instance(evaluation: SLOEvaluationV1) -> AlertInstanceV1:
    return AlertInstanceV1(
        alert_instance_id="alert-instance-1",
        alert_rule_id="checker-latency-page",
        dedup_key="checker-latency:critical",
        state="pending",
        pending_since=NOW,
        last_evaluation_id=evaluation.evaluation_id,
        revision=1,
    )


@pytest.fixture
def engine(tmp_path) -> Engine:
    url = f"sqlite:///{tmp_path / 'slo.db'}"
    migrations_api.upgrade(url, "head")
    selected = get_engine(url)
    yield selected
    selected.dispose()


def _capabilities(session: Session) -> TransactionCapabilities:
    repository = SqlSloRepository(session)
    return TransactionCapabilities(
        refs=repository,
        audit=repository,
        approvals=repository,
        lineage=repository,
        object_bindings=repository,
        runs=repository,
        cost=repository,
        slo=repository,
    )


def _seed_authority(
    transaction,
) -> tuple[
    WorkloadProfileV1,
    SLODefinitionV1,
    AlertRuleV1,
    SLOEvaluationV1,
]:
    profile = _profile()
    definition = _definition()
    rule = _rule()
    evaluation = _evaluation()
    transaction.slo.put_workload_profile(profile)
    transaction.slo.put_definition(definition)
    transaction.slo.put_rule(rule)
    transaction.slo.put_evaluation(evaluation)
    return profile, definition, rule, evaluation


def test_repository_round_trips_immutable_authority_and_alert_head(engine: Engine) -> None:
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        profile, definition, rule, evaluation = _seed_authority(transaction)
        alert = transaction.slo.compare_and_swap(
            _instance(evaluation),
            expected_revision=None,
        )

    with Session(engine) as session:
        repository = SqlSloRepository(session)
        assert repository.get_workload_profile(profile.profile_id) == profile
        assert repository.get_definition(definition.slo_id) == definition
        assert repository.get_rule(rule.alert_rule_id) == rule
        assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
        assert repository.list_evaluations(slo_id=definition.slo_id) == (evaluation,)
        assert repository.get(alert.alert_instance_id) == alert


def test_repository_lists_all_live_definitions_stably_and_refuses_partial_results(
    engine: Engine,
) -> None:
    profile = _profile()
    first = _definition()
    second = first.model_copy(update={"slo_id": "checker-throughput", "name": "Checker throughput"})
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.slo.put_workload_profile(profile)
        transaction.slo.put_definition(first)
        transaction.slo.put_definition(second)

    with Session(engine) as session:
        repository = SqlSloRepository(session)
        assert repository.list_live_definitions(limit=2) == (first, second)
        with pytest.raises(QueryTooBroad, match="reconciliation"):
            repository.list_live_definitions(limit=1)


def test_alert_head_uses_revision_cas_and_exact_evaluation_fk(engine: Engine) -> None:
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        _, _, _, first_evaluation = _seed_authority(transaction)
        first = transaction.slo.compare_and_swap(
            _instance(first_evaluation),
            expected_revision=None,
        )

    second_evaluation = _evaluation(offset=60, ratio=1.0)
    second = first.model_copy(
        update={
            "state": "resolved",
            "resolved_at": NOW + timedelta(seconds=60),
            "last_evaluation_id": second_evaluation.evaluation_id,
            "revision": 2,
        }
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.slo.put_evaluation(second_evaluation)
        assert transaction.slo.compare_and_swap(second, expected_revision=1) == second

    with pytest.raises(Conflict, match="revision"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.slo.compare_and_swap(
                second.model_copy(update={"revision": 3}),
                expected_revision=1,
            )

    missing = second.model_copy(
        update={"last_evaluation_id": "slo-evaluation:sha256:" + "f" * 64, "revision": 3}
    )
    with pytest.raises(IntegrityViolation, match="evaluation"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.slo.compare_and_swap(missing, expected_revision=2)


def test_immutable_ids_reject_conflicting_payloads_and_missing_parents(engine: Engine) -> None:
    profile = _profile()
    definition = _definition()
    with pytest.raises(IntegrityViolation, match="workload profile"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.slo.put_definition(definition)

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.slo.put_workload_profile(profile)
        transaction.slo.put_definition(definition)

    changed = definition.model_copy(update={"name": "Changed immutable name"})
    with pytest.raises(IntegrityViolation, match="different authoritative content"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.slo.put_definition(changed)


def test_repository_writes_roll_back_with_owning_uow(engine: Engine) -> None:
    with pytest.raises(RuntimeError, match="rollback"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.slo.put_workload_profile(_profile())
            raise RuntimeError("rollback")

    with Session(engine) as session:
        assert SqlSloRepository(session).get_workload_profile(_profile().profile_id) is None


def test_evaluation_queries_are_stable_and_bounded(engine: Engine) -> None:
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        _seed_authority(transaction)
        later = _evaluation(offset=60, ratio=1.0)
        transaction.slo.put_evaluation(later)

    with Session(engine) as session:
        repository = SqlSloRepository(session)
        assert repository.list_evaluations(slo_id="checker-latency", limit=1) == (_evaluation(),)
        with pytest.raises(QueryTooBroad):
            repository.list_evaluations(slo_id="checker-latency", limit=10_001)
