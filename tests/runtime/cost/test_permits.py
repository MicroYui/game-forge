from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, QuotaExceeded
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from tests.runtime.cost.ledger_testkit import (
    NOW,
    amounts_by_dimension,
    budget,
    budget_set,
    hold,
    permit_group,
    uow,
)


@pytest.fixture
def engine(tmp_path) -> Engine:
    selected = get_engine(f"sqlite:///{tmp_path / 'permits.db'}")
    migrations_api.upgrade(str(selected.url), "head")
    yield selected
    selected.dispose()


def test_permit_acquire_renew_release_and_retry_group_do_not_touch_usage_amounts(
    engine: Engine,
) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    first, first_members = permit_group(
        selected_set,
        lease_id="lease:1",
        fencing_token=1,
        suffix="1",
    )
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
        assert transaction.cost.acquire_permit_group(first, first_members) == first

    renewed_request = first.model_copy(
        update={
            "revision": 2,
            "expires_at": first.expires_at + timedelta(minutes=1),
        }
    )
    with uow(engine).begin() as transaction:
        renewed = transaction.cost.renew_permit_group(renewed_request)
        assert renewed == renewed_request

    released_request = renewed_request.model_copy(update={"revision": 3, "status": "released"})
    with uow(engine).begin() as transaction:
        released = transaction.cost.release_permit_group(released_request)
        assert released == released_request

    retry, retry_members = permit_group(
        selected_set,
        lease_id="lease:2",
        fencing_token=2,
        suffix="2",
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=6),
    )
    with uow(engine).begin() as transaction:
        transaction.cost.acquire_permit_group(retry, retry_members)

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        stored_budget = ledger.get_budget(selected_budget.budget_id)
        assert stored_budget is not None
        assert "concurrent_run" not in amounts_by_dimension(stored_budget.reserved)
        assert "concurrent_run" not in amounts_by_dimension(stored_budget.consumed)
        assert ledger.list_concurrency_permits(first.permit_group_id)[0].status == "released"
        assert ledger.list_concurrency_permits(retry.permit_group_id)[0].status == "active"


def test_capacity_is_all_scope_fail_closed_and_stale_revision_cannot_renew(engine: Engine) -> None:
    principal = budget("principal", "principal:1", suffix="shared")
    first_set = budget_set("run:1", (principal,), suffix="1")
    first_hold, first_hold_members = hold(first_set, input_tokens=10, agent_steps=1)
    first, first_members = permit_group(
        first_set,
        lease_id="lease:1",
        fencing_token=1,
        suffix="1",
    )
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(principal)
        transaction.cost.freeze_budget_set(first_set, first_hold, first_hold_members)
        transaction.cost.acquire_permit_group(first, first_members)

    with Session(engine) as session:
        current_principal = SqlCostLedger(session).get_budget(principal.budget_id)
    assert current_principal is not None
    second_set = budget_set("run:2", (current_principal,), suffix="2")
    second_hold, second_hold_members = hold(second_set, input_tokens=10, agent_steps=1)
    second, second_members = permit_group(
        second_set,
        lease_id="lease:2",
        fencing_token=1,
        suffix="2",
    )
    with uow(engine).begin() as transaction:
        transaction.cost.freeze_budget_set(second_set, second_hold, second_hold_members)

    with pytest.raises(QuotaExceeded, match="concurrent"):
        with uow(engine).begin() as transaction:
            transaction.cost.acquire_permit_group(second, second_members)

    stale = first.model_copy(
        update={
            "revision": 3,
            "expires_at": first.expires_at + timedelta(minutes=1),
        }
    )
    with pytest.raises(Conflict):
        with uow(engine).begin() as transaction:
            transaction.cost.renew_permit_group(stale)


def test_expire_reclaims_capacity_only_after_the_current_group_is_fenced(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    first, first_members = permit_group(
        selected_set,
        lease_id="lease:1",
        fencing_token=1,
        suffix="1",
    )
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
        transaction.cost.acquire_permit_group(first, first_members)

    expired_request = first.model_copy(update={"revision": 2, "status": "expired"})
    with pytest.raises(QuotaExceeded, match="not yet expired"):
        with uow(engine).begin() as transaction:
            transaction.cost.expire_permit_group(expired_request)

    later = NOW + timedelta(minutes=6)
    with Session(engine) as session, session.begin():
        ledger = SqlCostLedger(session, clock=FrozenUtcClock(later))
        assert ledger.expire_permit_group(expired_request) == expired_request
