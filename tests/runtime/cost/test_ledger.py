from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.errors import QuotaExceeded
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from tests.runtime.cost.ledger_testkit import (
    amounts_by_dimension,
    budget,
    budget_set,
    hold,
    permit_group,
    seed_current_attempt,
    step_group,
    uow,
    usage,
)


@pytest.fixture
def engine(tmp_path) -> Engine:
    selected = get_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    migrations_api.upgrade(str(selected.url), "head")
    yield selected
    selected.dispose()


def test_freeze_and_hold_are_atomic_across_run_principal_and_system(engine: Engine) -> None:
    budgets = (
        budget("run", "run:1"),
        budget("principal", "principal:1"),
        budget("system", "system:1"),
    )
    selected_set = budget_set("run:1", budgets)
    parent, reservations = hold(selected_set)

    with uow(engine).begin() as transaction:
        for item in budgets:
            transaction.cost.put_budget(item)
        assert transaction.cost.freeze_budget_set(selected_set, parent, reservations) == parent

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        assert ledger.get_budget_set(selected_set.budget_set_snapshot_id) == selected_set
        assert ledger.get_reservation_group(parent.reservation_group_id) == parent
        for item in budgets:
            stored = ledger.get_budget(item.budget_id)
            assert stored is not None
            assert amounts_by_dimension(stored.reserved) == {
                "input_token": Decimal(80),
                "agent_step": Decimal(8),
            }


def test_any_scope_rejection_leaves_no_snapshot_hold_or_partial_reserve(engine: Engine) -> None:
    budgets = (
        budget("run", "run:1"),
        budget("principal", "principal:1"),
        budget("system", "system:1", reserved_input=30),
    )
    selected_set = budget_set("run:1", budgets)
    parent, reservations = hold(selected_set)
    with uow(engine).begin() as transaction:
        for item in budgets:
            transaction.cost.put_budget(item)

    with pytest.raises(QuotaExceeded):
        with uow(engine).begin() as transaction:
            transaction.cost.freeze_budget_set(selected_set, parent, reservations)

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        assert ledger.get_budget_set(selected_set.budget_set_snapshot_id) is None
        assert ledger.get_reservation_group(parent.reservation_group_id) is None
        assert ledger.get_budget(budgets[0].budget_id) == budgets[0]
        assert ledger.get_budget(budgets[1].budget_id) == budgets[1]
        assert ledger.get_budget(budgets[2].budget_id) == budgets[2]


def test_child_reserve_does_not_increment_budget_reserved_and_usage_counts_once_per_scope(
    engine: Engine,
) -> None:
    budgets = (
        budget("run", "run:1"),
        budget("principal", "principal:1"),
        budget("system", "system:1"),
    )
    selected_set = budget_set("run:1", budgets)
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1")
    observed = usage(child, usage_id="usage:1", input_tokens=10)

    with uow(engine).begin() as transaction:
        for item in budgets:
            transaction.cost.put_budget(item)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
        for item in budgets:
            stored = transaction.cost.get_budget(item.budget_id)
            assert stored is not None
            assert amounts_by_dimension(stored.reserved)["input_token"] == 80
        settled = transaction.cost.reconcile_group(observed)
        assert settled.status == "reconciled"
        assert transaction.cost.reconcile_group(observed) == settled

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        assert ledger.list_usage(run_id=selected_set.run_id) == (observed,)
        for item in budgets:
            stored = ledger.get_budget(item.budget_id)
            assert stored is not None
            assert amounts_by_dimension(stored.reserved) == {
                "input_token": Decimal(70),
                "agent_step": Decimal(7),
            }
            assert amounts_by_dimension(stored.consumed) == {
                "input_token": Decimal(10),
                "agent_step": Decimal(1),
            }


def test_exact_reservation_replay_returns_retained_terminal_state(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1")
    observed = usage(child, usage_id="usage:1", input_tokens=10)

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
        terminal = transaction.cost.reconcile_group(observed)
        assert terminal.status == "reconciled"

    with uow(engine).begin() as transaction:
        replay = transaction.cost.reserve_many(child, child_members)

    assert replay == terminal
    assert replay.status == "reconciled"
    assert replay.revision > child.revision


def test_close_hold_releases_only_unallocated_balance_and_has_no_active_orphan(
    engine: Engine,
) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1")
    observed = usage(child, usage_id="usage:1", input_tokens=10)

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
        with pytest.raises(Exception, match="active child"):
            transaction.cost.close_hold_group(parent.reservation_group_id)
        transaction.cost.reconcile_group(observed)
        closed = transaction.cost.close_hold_group(parent.reservation_group_id)
        assert closed.status == "released"

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        stored = ledger.get_budget(selected_budget.budget_id)
        assert stored is not None
        assert stored.reserved == ()
        assert amounts_by_dimension(stored.consumed)["input_token"] == 10
        assert ledger.get_reservation_group(parent.reservation_group_id).status == "released"  # type: ignore[union-attr]


def test_budget_set_snapshot_captures_exact_current_amounts_not_only_revision(
    engine: Engine,
) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    changed_snapshot = selected_set.snapshots[0].model_copy(
        update={"budget_revision_at_freeze": 2, "reserved": ()}
    )
    changed_set = selected_set.model_copy(update={"snapshots": (changed_snapshot,)})
    parent, members = hold(changed_set)

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.replace_budget(
            selected_budget,
            selected_budget.model_copy(
                update={
                    "reserved": (
                        selected_budget.limits[0].model_copy(update={"value": Decimal(10)}),
                    ),
                    "revision": selected_budget.revision + 1,
                }
            ),
        )

    with pytest.raises(Exception):
        with uow(engine).begin() as transaction:
            transaction.cost.freeze_budget_set(changed_set, parent, members)


def test_exhausted_budget_rejects_new_reserve_retry_and_permit(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    first, first_members = step_group(selected_set, parent, suffix="1", input_tokens=30)
    second, second_members = step_group(selected_set, parent, suffix="2", input_tokens=10)
    overage = usage(first, usage_id="usage:overage", input_tokens=110)
    candidate_permit, candidate_permits = permit_group(
        selected_set,
        lease_id="lease:1",
        fencing_token=1,
        suffix="after-exhaustion",
    )

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(first, first_members)
        transaction.cost.reconcile_group(overage)

    with uow(engine).begin() as transaction:
        assert (
            transaction.cost.retry_budget_available(
                run_id=selected_set.run_id,
                budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
                hold_group_id=parent.reservation_group_id,
            )
            is False
        )
    with pytest.raises(QuotaExceeded, match="unavailable"):
        with uow(engine).begin() as transaction:
            transaction.cost.reserve_many(second, second_members)
    with pytest.raises(QuotaExceeded, match="unavailable"):
        with uow(engine).begin() as transaction:
            transaction.cost.acquire_permit_group(candidate_permit, candidate_permits)

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        assert ledger.get_budget(selected_budget.budget_id).status == "exhausted"  # type: ignore[union-attr]
        assert ledger.get_reservation_group(second.reservation_group_id) is None
        assert ledger.get_permit_group(candidate_permit.permit_group_id) is None
