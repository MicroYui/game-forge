from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.cost import (
    BudgetReservationV1,
    BudgetSetSnapshotV1,
    BudgetSnapshotV1,
    BudgetV1,
    CacheHitObservationV1,
    ConcurrencyPermitV1,
    CostAmountV1,
    LatencyObservationV1,
    MonetaryObservationV1,
    PermitGroupV1,
    ReservationGroupV1,
    TokenUsageObservationV1,
    UsageEntryV1,
)
from gameforge.contracts.errors import IntegrityViolation, QueryTooBroad
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingDecisionV1,
    RoutingPolicyV1,
    RoutingRuleV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
    compute_routing_policy_digest,
)
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.cost import SqlCostRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
REQUEST_HASH = "sha256:" + "1" * 64


def _amount(dimension: str, value: int) -> CostAmountV1:
    units = {
        "input_token": "token",
        "output_token": "token",
        "request": "request",
        "concurrent_run": "count",
    }
    return CostAmountV1(dimension=dimension, value=Decimal(value), unit=units[dimension])


def _budget() -> BudgetV1:
    return BudgetV1(
        budget_id="budget-run-1",
        scope_kind="run",
        scope_id="run-1",
        policy_version="budget-policy@1",
        limits=(_amount("input_token", 100), _amount("concurrent_run", 1)),
        reserved=(_amount("input_token", 50),),
        consumed=(),
        status="active",
        revision=1,
        deadline_utc=NOW + timedelta(hours=1),
        created_at=NOW,
    )


def _budget_set(budget: BudgetV1) -> BudgetSetSnapshotV1:
    snapshot = BudgetSnapshotV1(
        snapshot_id="budget-snapshot-1",
        budget_id=budget.budget_id,
        scope_kind=budget.scope_kind,
        scope_id=budget.scope_id,
        policy_version=budget.policy_version,
        budget_revision_at_freeze=budget.revision,
        limits=budget.limits,
        reserved=(),
        consumed=(),
        captured_at=NOW,
    )
    return BudgetSetSnapshotV1(
        budget_set_snapshot_id="budget-set-1",
        run_id="run-1",
        selection_policy_version="selection@1",
        snapshots=(snapshot,),
        captured_at=NOW,
    )


def _hold(budget: BudgetV1) -> tuple[ReservationGroupV1, BudgetReservationV1]:
    group = ReservationGroupV1(
        reservation_group_id="hold-1",
        scope="run_budget_hold",
        run_id="run-1",
        budget_set_snapshot_id="budget-set-1",
        request_hash=REQUEST_HASH,
        idempotency_key="hold-idempotency",
        budget_reservation_ids=("reservation-hold-1",),
        status="reserved",
        revision=1,
        created_at=NOW,
    )
    reservation = BudgetReservationV1(
        reservation_id="reservation-hold-1",
        reservation_group_id=group.reservation_group_id,
        budget_id=budget.budget_id,
        reserved=(_amount("input_token", 50),),
        status="reserved",
        revision=1,
    )
    return group, reservation


def _call(
    budget: BudgetV1,
    hold: ReservationGroupV1,
) -> tuple[ReservationGroupV1, BudgetReservationV1]:
    group = ReservationGroupV1(
        reservation_group_id="call-1",
        scope="attempt_call",
        run_id=hold.run_id,
        budget_set_snapshot_id=hold.budget_set_snapshot_id,
        parent_hold_group_id=hold.reservation_group_id,
        attempt_no=1,
        request_hash=REQUEST_HASH,
        transport_attempt=1,
        fencing_token=1,
        idempotency_key="call-idempotency",
        budget_reservation_ids=("reservation-call-1",),
        status="reserved",
        revision=1,
        created_at=NOW,
    )
    reservation = BudgetReservationV1(
        reservation_id="reservation-call-1",
        reservation_group_id=group.reservation_group_id,
        budget_id=budget.budget_id,
        reserved=(_amount("input_token", 20),),
        status="reserved",
        revision=1,
    )
    return group, reservation


def _usage(
    group: ReservationGroupV1,
    reservation: BudgetReservationV1,
    *,
    routing_decision_id: str,
) -> UsageEntryV1:
    return UsageEntryV1(
        usage_id="usage-1",
        reservation_group_id=group.reservation_group_id,
        budget_reservation_ids=(reservation.reservation_id,),
        scope="attempt_call",
        run_id=group.run_id,
        attempt_no=1,
        request_hash=group.request_hash,
        transport_attempt=1,
        execution_source="online",
        provider_prefix_cache=CacheHitObservationV1(status="reported", hit=False),
        retry_index=0,
        token_usage=TokenUsageObservationV1(
            status="reported", input_tokens=10, output_tokens=2, total_tokens=12
        ),
        latency=LatencyObservationV1(status="reported", provider_latency_ms=100),
        wall_time_ns=120_000_000,
        monetary=MonetaryObservationV1(status="unavailable"),
        routing_decision_kind="native",
        routing_decision_id=routing_decision_id,
        fencing_token_at_reserve=1,
        recorded_at=NOW,
    )


def _permit() -> tuple[PermitGroupV1, ConcurrencyPermitV1]:
    group = PermitGroupV1(
        permit_group_id="permit-group-1",
        budget_set_snapshot_id="budget-set-1",
        run_id="run-1",
        lease_id="lease-1",
        fencing_token=1,
        permit_ids=("permit-1",),
        status="active",
        revision=1,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    permit = ConcurrencyPermitV1(
        permit_id="permit-1",
        permit_group_id=group.permit_group_id,
        budget_id="budget-run-1",
        run_id=group.run_id,
        lease_id=group.lease_id,
        fencing_token=group.fencing_token,
        status="active",
        revision=1,
        acquired_at=group.acquired_at,
        expires_at=group.expires_at,
    )
    return group, permit


def _catalog_policy_decision() -> tuple[
    ModelCatalogSnapshotV1,
    RoutingPolicyV1,
    RoutingDecisionV1,
]:
    structured = ModelSnapshot(provider="openai", model="gpt-5.6-sol", snapshot_tag="2026-07")
    descriptor = ModelDescriptorV1(
        provider="openai",
        model_snapshot=canonical_model_snapshot_id(structured),
        tier="best",
        capabilities=("reasoning",),
        context_limit=200_000,
        max_output_tokens=32_000,
        prompt_cache_support=True,
        status="active",
    )
    catalog_payload = {
        "catalog_version": 1,
        "models": (descriptor,),
        "created_at": NOW,
    }
    catalog = ModelCatalogSnapshotV1(
        **catalog_payload,
        catalog_digest=compute_model_catalog_digest(catalog_payload),
    )
    rule = RoutingRuleV1(
        rule_id="repair",
        task_kind="patch_repair",
        required_capabilities=("reasoning",),
        primary_model_snapshot=descriptor.model_snapshot,
        allowed_fallback_chain=(),
        budget_predicates=(),
    )
    policy_payload = {
        "policy_version": 1,
        "catalog_version": catalog.catalog_version,
        "catalog_digest": catalog.catalog_digest,
        "rules": (rule,),
        "failure_classifier_version": "failure-classifier@1",
    }
    policy = RoutingPolicyV1(
        **policy_payload,
        routing_policy_digest=compute_routing_policy_digest(policy_payload),
    )
    decision = RoutingDecisionV1.create(
        run_id="run-1",
        attempt_no=1,
        request_hash=REQUEST_HASH,
        rule_id=rule.rule_id,
        model_snapshot=descriptor.model_snapshot,
        tier=descriptor.tier,
        reason_code="primary_rule",
        budget_set_snapshot_id="budget-set-1",
        fallback_from=None,
        fallback_index=0,
        policy_version=policy.policy_version,
        routing_policy_digest=policy.routing_policy_digest,
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        execution_source="online",
        decided_at=NOW,
    )
    return catalog, policy, decision


@pytest.fixture
def engine(tmp_path) -> Engine:
    url = f"sqlite:///{tmp_path / 'cost.db'}"
    migrations_api.upgrade(url, "head")
    selected = get_engine(url)
    yield selected
    selected.dispose()


def _capabilities(session: Session) -> TransactionCapabilities:
    repository = SqlCostRepository(session)
    return TransactionCapabilities(
        refs=repository,
        audit=repository,
        approvals=repository,
        lineage=repository,
        object_bindings=repository,
        runs=repository,
        cost=repository,
    )


def test_cost_repository_round_trips_authoritative_records(engine: Engine) -> None:
    budget = _budget()
    budget_set = _budget_set(budget)
    hold, reservation = _hold(budget)
    permit_group, permit = _permit()
    catalog, policy, decision = _catalog_policy_decision()

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.cost.put_budget(budget)
        transaction.cost.put_budget_set(budget_set)
        transaction.cost.put_reservation_group(hold, (reservation,))
        transaction.cost.put_permit_group(permit_group, (permit,))
        transaction.cost.put_model_catalog(catalog)
        transaction.cost.put_routing_policy(policy)
        transaction.cost.put_routing_decision(decision)

    with Session(engine) as session:
        repository = SqlCostRepository(session)
        assert repository.get_budget(budget.budget_id) == budget
        assert repository.get_budget_set(budget_set.budget_set_snapshot_id) == budget_set
        assert repository.get_reservation_group(hold.reservation_group_id) == hold
        assert repository.list_budget_reservations(hold.reservation_group_id) == (reservation,)
        assert repository.get_permit_group(permit_group.permit_group_id) == permit_group
        assert repository.list_concurrency_permits(permit_group.permit_group_id) == (permit,)
        assert repository.get_model_catalog(1, catalog.catalog_digest) == catalog
        assert repository.get_routing_policy(1, policy.routing_policy_digest) == policy
        assert repository.get_routing_decision(decision.decision_id) == decision


def test_budget_scope_identity_query_is_exact_stable_and_bounded(engine: Engine) -> None:
    first = _budget().model_copy(
        update={
            "budget_id": "budget-principal-a",
            "scope_kind": "principal",
            "scope_id": "human:actor",
        }
    )
    second = first.model_copy(update={"budget_id": "budget-principal-b"})
    other = first.model_copy(
        update={
            "budget_id": "budget-principal-other",
            "scope_id": "human:other",
        }
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        for budget in (second, other, first):
            transaction.cost.put_budget(budget)

    with Session(engine) as session:
        repository = SqlCostRepository(session)
        assert repository.list_budgets_by_scope_identity(
            scope_kind="principal",
            scope_id="human:actor",
            limit=2,
        ) == (first, second)
        assert (
            repository.list_budgets_by_scope_identity(
                scope_kind="principal",
                scope_id="human:missing",
                limit=1,
            )
            == ()
        )
        with pytest.raises(QueryTooBroad):
            repository.list_budgets_by_scope_identity(
                scope_kind="principal",
                scope_id="human:actor",
                limit=0,
            )
        with pytest.raises(QueryTooBroad):
            repository.list_budgets_by_scope_identity(
                scope_kind="principal",
                scope_id="human:actor",
                limit=1_001,
            )


def test_routing_decisions_do_not_conflate_repeated_logical_calls(engine: Engine) -> None:
    budget = _budget()
    budget_set = _budget_set(budget)
    catalog, policy, first = _catalog_policy_decision()
    second = RoutingDecisionV1.create(
        **first.model_dump(exclude={"decision_id", "decision_schema_version", "decided_at"}),
        decided_at=NOW + timedelta(microseconds=1),
    )

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.cost.put_budget(budget)
        transaction.cost.put_budget_set(budget_set)
        transaction.cost.put_model_catalog(catalog)
        transaction.cost.put_routing_policy(policy)
        assert transaction.cost.put_routing_decision(first) == first
        assert transaction.cost.put_routing_decision(first) == first
        assert transaction.cost.put_routing_decision(second) == second

    with Session(engine) as session:
        repository = SqlCostRepository(session)
        retained = repository.list_routing_decisions(run_id="run-1", attempt_no=1)
        assert {item.decision_id: item for item in retained} == {
            first.decision_id: first,
            second.decision_id: second,
        }


def test_usage_is_append_only_idempotent_and_conflicting_identity_fails(engine: Engine) -> None:
    budget = _budget()
    budget_set = _budget_set(budget)
    hold, hold_reservation = _hold(budget)
    call, call_reservation = _call(budget, hold)
    catalog, policy, decision = _catalog_policy_decision()
    usage = _usage(
        call,
        call_reservation,
        routing_decision_id=decision.decision_id,
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.cost.put_budget(budget)
        transaction.cost.put_budget_set(budget_set)
        transaction.cost.put_reservation_group(hold, (hold_reservation,))
        transaction.cost.put_reservation_group(call, (call_reservation,))
        transaction.cost.put_model_catalog(catalog)
        transaction.cost.put_routing_policy(policy)
        transaction.cost.put_routing_decision(decision)
        transaction.cost.put_usage(usage)
        transaction.cost.put_usage(usage)

    with Session(engine) as session:
        assert SqlCostRepository(session).list_usage(run_id="run-1") == (usage,)

    changed = usage.model_copy(update={"wall_time_ns": usage.wall_time_ns + 1})
    with pytest.raises(IntegrityViolation):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.cost.put_usage(changed)


def test_usage_rejects_an_unresolved_routing_variant(engine: Engine) -> None:
    budget = _budget()
    budget_set = _budget_set(budget)
    hold, hold_reservation = _hold(budget)
    call, call_reservation = _call(budget, hold)
    catalog, policy, decision = _catalog_policy_decision()
    usage = _usage(
        call,
        call_reservation,
        routing_decision_id=decision.decision_id,
    )

    with pytest.raises(IntegrityViolation, match="exact native routing decision"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.cost.put_budget(budget)
            transaction.cost.put_budget_set(budget_set)
            transaction.cost.put_reservation_group(hold, (hold_reservation,))
            transaction.cost.put_reservation_group(call, (call_reservation,))
            transaction.cost.put_model_catalog(catalog)
            transaction.cost.put_routing_policy(policy)
            transaction.cost.put_usage(usage)


def test_repository_writes_roll_back_with_the_owning_uow(engine: Engine) -> None:
    budget = _budget()
    with pytest.raises(RuntimeError, match="rollback"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.cost.put_budget(budget)
            raise RuntimeError("rollback")

    with Session(engine) as session:
        assert SqlCostRepository(session).get_budget(budget.budget_id) is None


def test_exact_history_and_reservation_idempotency_are_fail_closed(engine: Engine) -> None:
    budget = _budget()
    budget_set = _budget_set(budget)
    hold, hold_reservation = _hold(budget)
    catalog, _, _ = _catalog_policy_decision()
    changed_descriptor = catalog.models[0].model_copy(update={"tier": "different-tier"})
    changed_payload = {
        "catalog_version": catalog.catalog_version,
        "models": (changed_descriptor,),
        "created_at": catalog.created_at,
    }
    changed_catalog = ModelCatalogSnapshotV1(
        **changed_payload,
        catalog_digest=compute_model_catalog_digest(changed_payload),
    )

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.cost.put_budget(budget)
        transaction.cost.put_budget_set(budget_set)
        transaction.cost.put_reservation_group(hold, (hold_reservation,))
        transaction.cost.put_model_catalog(catalog)

    with pytest.raises(IntegrityViolation, match="catalog version"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.cost.put_model_catalog(changed_catalog)

    conflicting_group = hold.model_copy(
        update={
            "reservation_group_id": "hold-2",
            "request_hash": "sha256:" + "2" * 64,
            "budget_reservation_ids": ("reservation-hold-2",),
        }
    )
    conflicting_reservation = hold_reservation.model_copy(
        update={
            "reservation_id": "reservation-hold-2",
            "reservation_group_id": "hold-2",
        }
    )
    with pytest.raises(IntegrityViolation, match="idempotency key"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.cost.put_reservation_group(
                conflicting_group,
                (conflicting_reservation,),
            )
