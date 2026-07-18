from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.cost import CacheHitObservationV1
from gameforge.contracts.errors import IntegrityViolation
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import RunLeaseRow
from tests.runtime.cost.ledger_testkit import (
    NOW,
    amounts_by_dimension,
    budget,
    budget_set,
    hold,
    seed_current_attempt,
    step_group,
    uow,
    usage,
)


@pytest.fixture
def engine(tmp_path) -> Engine:
    selected = get_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    migrations_api.upgrade(str(selected.url), "head")
    yield selected
    selected.dispose()


def test_unknown_survives_restart_then_conservative_and_late_actual_are_idempotent(
    engine: Engine,
) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1", input_tokens=30)
    unknown = usage(child, usage_id="usage:unknown", input_tokens=None)

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
        held = transaction.cost.reconcile_group(unknown)
        assert held.status == "held_unknown"
        assert transaction.cost.list_usage(run_id=selected_set.run_id) == ()

    with Session(engine) as session, session.begin():
        lease = session.get(RunLeaseRow, "lease:1")
        assert lease is not None
        lease.status = "expired"
        lease.released_at = NOW.isoformat().replace("+00:00", "Z")

    conservative = usage(
        child,
        usage_id="usage:conservative",
        input_tokens=30,
        recorded_at=NOW + timedelta(minutes=1),
    )
    with uow(engine).begin() as transaction:
        settled = transaction.cost.settle_unknown_group(
            child.reservation_group_id,
            conservative,
        )
        assert settled.status == "conservatively_settled"

    late = usage(
        child,
        usage_id="usage:late",
        input_tokens=12,
        adjustment_of_usage_id=conservative.usage_id,
        recorded_at=NOW + timedelta(minutes=2),
    ).model_copy(
        update={"provider_prefix_cache": CacheHitObservationV1(status="reported", hit=True)}
    )
    with uow(engine).begin() as transaction:
        reconciled = transaction.cost.late_reconcile_group(late)
        assert reconciled.status == "late_reconciled"
        assert transaction.cost.late_reconcile_group(late) == reconciled

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        entries = ledger.list_usage(run_id=selected_set.run_id)
        assert entries == (conservative, late)
        stored = ledger.get_budget(selected_budget.budget_id)
        assert stored is not None
        assert amounts_by_dimension(stored.consumed)["input_token"] == Decimal(12)
        assert amounts_by_dimension(stored.reserved)["input_token"] == Decimal(68)


def test_already_incurred_known_usage_reconciles_after_lease_expiry(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1", input_tokens=30)
    observed = usage(child, usage_id="usage:known-after-expiry", input_tokens=9)

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
    with Session(engine) as session, session.begin():
        lease = session.get(RunLeaseRow, "lease:1")
        assert lease is not None
        lease.status = "expired"
        lease.released_at = NOW.isoformat().replace("+00:00", "Z")

    with uow(engine).begin() as transaction:
        assert transaction.cost.reconcile_group(observed).status == "reconciled"

    with Session(engine) as session:
        stored = SqlCostLedger(session).get_budget(selected_budget.budget_id)
        assert stored is not None
        assert amounts_by_dimension(stored.consumed)["input_token"] == Decimal(9)


def test_late_actual_above_conservative_consumes_remaining_hold_then_records_overage(
    engine: Engine,
) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1", input_tokens=30)
    unknown = usage(child, usage_id="usage:unknown", input_tokens=None)
    conservative = usage(child, usage_id="usage:conservative", input_tokens=20)
    late = usage(
        child,
        usage_id="usage:late-overage",
        input_tokens=40,
        adjustment_of_usage_id=conservative.usage_id,
        recorded_at=NOW + timedelta(minutes=2),
    )

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
        transaction.cost.reconcile_group(unknown)
        transaction.cost.settle_unknown_group(child.reservation_group_id, conservative)
        transaction.cost.late_reconcile_group(late)

    with Session(engine) as session:
        stored = SqlCostLedger(session).get_budget(selected_budget.budget_id)
        assert stored is not None
        assert amounts_by_dimension(stored.reserved)["input_token"] == Decimal(50)
        assert amounts_by_dimension(stored.consumed)["input_token"] == Decimal(40)


@pytest.mark.parametrize(
    ("conservative_tokens", "actual_tokens"),
    ((30, 12), (20, 40)),
)
def test_late_actual_after_closed_hold_only_adjusts_consumed(
    engine: Engine,
    conservative_tokens: int,
    actual_tokens: int,
) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1", input_tokens=30)
    unknown = usage(child, usage_id="usage:unknown", input_tokens=None)
    conservative = usage(
        child,
        usage_id="usage:conservative",
        input_tokens=conservative_tokens,
    )
    late = usage(
        child,
        usage_id="usage:late-after-close",
        input_tokens=actual_tokens,
        adjustment_of_usage_id=conservative.usage_id,
        recorded_at=NOW + timedelta(minutes=2),
    )

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
        transaction.cost.reconcile_group(unknown)
        transaction.cost.settle_unknown_group(child.reservation_group_id, conservative)
        transaction.cost.close_hold_group(parent.reservation_group_id)

    with uow(engine).begin() as transaction:
        assert transaction.cost.late_reconcile_group(late).status == "late_reconciled"

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        stored = ledger.get_budget(selected_budget.budget_id)
        assert stored is not None
        assert stored.reserved == ()
        assert amounts_by_dimension(stored.consumed)["input_token"] == Decimal(actual_tokens)
        assert ledger.get_reservation_group(parent.reservation_group_id).status == "released"  # type: ignore[union-attr]


def test_late_adjustment_must_inherit_the_original_execution_identity(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1")
    unknown = usage(child, usage_id="usage:unknown", input_tokens=None)
    conservative = usage(child, usage_id="usage:conservative", input_tokens=30)

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
        transaction.cost.reconcile_group(unknown)
        transaction.cost.settle_unknown_group(child.reservation_group_id, conservative)

    invalid_late = usage(
        child,
        usage_id="usage:late",
        input_tokens=12,
        adjustment_of_usage_id=conservative.usage_id,
    ).model_copy(update={"retry_index": 1})
    with pytest.raises(IntegrityViolation, match="inherit"):
        with uow(engine).begin() as transaction:
            transaction.cost.late_reconcile_group(invalid_late)
