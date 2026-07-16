from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from gameforge.contracts.cost import (
    BudgetReservationV1,
    BudgetSetSnapshotV1,
    BudgetSnapshotV1,
    BudgetV1,
    CacheHitObservationV1,
    CostAmountV1,
    CostLedger,
    LatencyObservationV1,
    MonetaryObservationV1,
    PermitGroupV1,
    ReservationGroupV1,
    TokenUsageObservationV1,
    UsageEntryV1,
)


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def test_cost_ledger_contract_distinguishes_release_from_lease_expiry() -> None:
    assert "release_permit_group" in CostLedger.__dict__
    assert "expire_permit_group" in CostLedger.__dict__


def _amount(dimension: str, value: int) -> CostAmountV1:
    units = {
        "input_token": "token",
        "output_token": "token",
        "request": "request",
        "agent_step": "step",
        "wall_time_ns": "ns",
        "concurrent_run": "count",
    }
    return CostAmountV1(dimension=dimension, value=Decimal(value), unit=units[dimension])


def _budget(*, budget_id: str, scope_kind: str, scope_id: str) -> BudgetV1:
    return BudgetV1(
        budget_id=budget_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        policy_version="budget-policy@1",
        limits=(_amount("input_token", 100), _amount("concurrent_run", 2)),
        reserved=(_amount("input_token", 20),),
        consumed=(_amount("input_token", 10),),
        status="active",
        revision=3,
        created_at=NOW,
    )


def test_unknown_observations_are_not_reported_zero_or_false() -> None:
    unknown_usage = TokenUsageObservationV1(status="unavailable")
    zero_usage = TokenUsageObservationV1(
        status="reported",
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
    )
    unknown_latency = LatencyObservationV1(status="unavailable")
    zero_latency = LatencyObservationV1(status="reported", provider_latency_ms=0)
    unknown_hit = CacheHitObservationV1(status="unavailable")
    miss = CacheHitObservationV1(status="reported", hit=False)

    assert unknown_usage != zero_usage
    assert unknown_latency != zero_latency
    assert unknown_hit != miss

    with pytest.raises(ValidationError):
        TokenUsageObservationV1(status="unavailable", total_tokens=0)
    with pytest.raises(ValidationError):
        CacheHitObservationV1(status="reported")


def test_budget_keeps_concurrent_run_as_limit_only() -> None:
    budget = _budget(budget_id="budget-run", scope_kind="run", scope_id="run-1")
    assert {item.dimension for item in budget.limits} == {"concurrent_run", "input_token"}

    with pytest.raises(ValidationError, match="concurrent_run"):
        BudgetV1(
            **budget.model_dump(exclude={"reserved"}),
            reserved=(_amount("concurrent_run", 1),),
        )


def test_budget_set_is_stably_ordered_and_scope_complete() -> None:
    budgets = (
        _budget(budget_id="system", scope_kind="system", scope_id="global"),
        _budget(budget_id="run", scope_kind="run", scope_id="run-1"),
        _budget(budget_id="principal-daily", scope_kind="principal", scope_id="human-1"),
        _budget(budget_id="principal", scope_kind="principal", scope_id="human-1"),
    )
    snapshots = tuple(
        BudgetSnapshotV1(
            snapshot_id=f"snapshot-{budget.budget_id}",
            budget_id=budget.budget_id,
            scope_kind=budget.scope_kind,
            scope_id=budget.scope_id,
            policy_version=budget.policy_version,
            budget_revision_at_freeze=budget.revision,
            limits=budget.limits,
            reserved=budget.reserved,
            consumed=budget.consumed,
            captured_at=NOW,
        )
        for budget in budgets
    )
    budget_set = BudgetSetSnapshotV1(
        budget_set_snapshot_id="budget-set-1",
        run_id="run-1",
        selection_policy_version="selection@1",
        snapshots=snapshots,
        captured_at=NOW,
    )

    assert [(item.scope_kind, item.budget_id) for item in budget_set.snapshots] == [
        ("run", "run"),
        ("principal", "principal"),
        ("principal", "principal-daily"),
        ("system", "system"),
    ]


def test_reservation_scope_shapes_are_closed() -> None:
    hold = ReservationGroupV1(
        reservation_group_id="hold-1",
        scope="run_budget_hold",
        run_id="run-1",
        budget_set_snapshot_id="budget-set-1",
        request_hash="sha256:" + "1" * 64,
        idempotency_key="idem-hold",
        budget_reservation_ids=("reservation-1",),
        status="reserved",
        revision=1,
        created_at=NOW,
    )
    step = ReservationGroupV1(
        reservation_group_id="step-1",
        scope="agent_step",
        run_id="run-1",
        budget_set_snapshot_id="budget-set-1",
        parent_hold_group_id=hold.reservation_group_id,
        attempt_no=1,
        request_hash="sha256:" + "2" * 64,
        fencing_token=7,
        idempotency_key="idem-step",
        budget_reservation_ids=("reservation-step-1",),
        status="reserved",
        revision=1,
        created_at=NOW,
    )
    assert step.transport_attempt is None

    with pytest.raises(ValidationError, match="transport_attempt"):
        ReservationGroupV1(
            **step.model_dump(exclude={"transport_attempt"}),
            transport_attempt=1,
        )

    reservation = BudgetReservationV1(
        reservation_id="reservation-step-1",
        reservation_group_id=step.reservation_group_id,
        budget_id="run",
        reserved=(_amount("input_token", 10),),
        status="reserved",
        revision=1,
    )
    assert reservation.reserved[0].value == 10


def test_usage_routing_identity_and_unknown_money_are_strict() -> None:
    usage = UsageEntryV1(
        usage_id="usage-1",
        reservation_group_id="call-1",
        budget_reservation_ids=("reservation-1",),
        scope="attempt_call",
        run_id="run-1",
        attempt_no=1,
        request_hash="sha256:" + "3" * 64,
        transport_attempt=1,
        execution_source="cassette_replay",
        provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
        retry_index=0,
        token_usage=TokenUsageObservationV1(status="unavailable"),
        latency=LatencyObservationV1(status="unavailable"),
        wall_time_ns=5,
        monetary=MonetaryObservationV1(status="unavailable"),
        routing_decision_kind="legacy_import",
        routing_decision_id="legacy-route-1",
        fencing_token_at_reserve=7,
        recorded_at=NOW,
    )
    assert usage.monetary.amount is None

    with pytest.raises(ValidationError, match="routing"):
        UsageEntryV1(
            **usage.model_dump(exclude={"routing_decision_id"}),
            routing_decision_id=None,
        )


def test_permit_group_requires_positive_lease_fencing_and_expiry() -> None:
    permit = PermitGroupV1(
        permit_group_id="permit-group-1",
        budget_set_snapshot_id="budget-set-1",
        run_id="run-1",
        lease_id="lease-1",
        fencing_token=4,
        permit_ids=("permit-1",),
        status="active",
        revision=1,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    assert permit.expires_at > permit.acquired_at
