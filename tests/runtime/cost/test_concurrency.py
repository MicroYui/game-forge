from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.errors import QuotaExceeded
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import RunAttemptRow, RunLeaseRow
from tests.runtime.cost.ledger_testkit import (
    NOW,
    budget,
    budget_set,
    hold,
    seed_current_attempt,
    step_group,
    uow,
)


@pytest.fixture
def engine(tmp_path) -> Engine:
    selected = get_engine(f"sqlite:///{tmp_path / 'concurrency.db'}")
    migrations_api.upgrade(str(selected.url), "head")
    yield selected
    selected.dispose()


def test_concurrent_child_reservations_cannot_overallocate_the_parent_hold(
    engine: Engine,
) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set, input_tokens=80)
    first = step_group(selected_set, parent, suffix="1", input_tokens=60)
    second = step_group(selected_set, parent, suffix="2", input_tokens=60)
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)

    def reserve(candidate):
        try:
            with uow(engine).begin() as transaction:
                return transaction.cost.reserve_many(*candidate).reservation_group_id
        except QuotaExceeded:
            return "quota"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(reserve, (first, second)))
    assert sorted(value == "quota" for value in results) == [False, True]

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        retained = tuple(
            group_id
            for group_id in (first[0].reservation_group_id, second[0].reservation_group_id)
            if ledger.get_reservation_group(group_id) is not None
        )
        assert len(retained) == 1


def test_stale_fencing_and_expired_deadline_reject_only_new_reservation(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    stale, stale_members = step_group(
        selected_set,
        parent,
        suffix="2",
        fencing_token=2,
    )
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(
            session,
            selected_set=selected_set,
            parent=parent,
            fencing_token=1,
        )

    with pytest.raises(QuotaExceeded, match="fencing"):
        with uow(engine).begin() as transaction:
            transaction.cost.reserve_many(stale, stale_members)


def test_exact_reservation_replay_rechecks_current_fence_and_deadline(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1")
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        assert transaction.cost.reserve_many(child, child_members) == child

    expired_clock = FrozenUtcClock(NOW + timedelta(hours=3))
    with pytest.raises(QuotaExceeded, match="deadline|fencing"):
        with uow(engine, clock=expired_clock).begin() as transaction:
            transaction.cost.reserve_many(child, child_members)


def test_exact_reservation_replay_rejects_stale_fencing_token(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1")
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        assert transaction.cost.reserve_many(child, child_members) == child

    with Session(engine) as session, session.begin():
        attempt = session.get(RunAttemptRow, (selected_set.run_id, 1))
        lease = session.get(RunLeaseRow, "lease:1")
        assert attempt is not None
        assert lease is not None
        attempt.fencing_token = 2
        lease.fencing_token = 2

    with pytest.raises(QuotaExceeded, match="fencing"):
        with uow(engine).begin() as transaction:
            transaction.cost.reserve_many(child, child_members)
