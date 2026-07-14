from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.cost import (
    BudgetReservationV1,
    ReservationGroupV1,
    UsageEntryV1,
)
from gameforge.contracts.errors import IntegrityViolation, QuotaExceeded
from gameforge.contracts.jobs import RunAttempt, RunLease, RunPayloadEnvelope, RunRecord
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.cost_policy.run_accounting import RunBudgetPlan, SqlRunCostAccounting
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from tests.runtime.cost.ledger_testkit import (
    NOW,
    budget,
    budget_set,
    hold,
    seed_current_attempt,
    step_group,
    uow,
    usage,
)


@dataclass(frozen=True)
class _PlanProvider:
    plan: RunBudgetPlan

    def resolve_run_budget(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        request_hash: str,
        initiated_by: AuditActor,
    ) -> RunBudgetPlan:
        assert run_id and budget_set_snapshot_id and request_hash
        assert initiated_by.principal_kind == "human"
        return self.plan


@dataclass(frozen=True)
class _SettlementProvider:
    conservative_input_tokens: dict[str, int]

    def conservative_usage(
        self,
        *,
        group: ReservationGroupV1,
        reservations: tuple[BudgetReservationV1, ...],
        recorded_at: datetime,
    ) -> UsageEntryV1:
        assert tuple(item.reservation_id for item in reservations) == (group.budget_reservation_ids)
        input_tokens = self.conservative_input_tokens[group.reservation_group_id]
        return usage(
            group,
            usage_id=f"usage:conservative:{group.reservation_group_id}",
            input_tokens=input_tokens,
            recorded_at=recorded_at,
        )


@pytest.fixture
def engine(tmp_path) -> Engine:
    selected = get_engine(f"sqlite:///{tmp_path / 'run-cost.db'}")
    migrations_api.upgrade(str(selected.url), "head")
    yield selected
    selected.dispose()


def _queued_run(*, budget_set_snapshot_id: str, hold_group_id: str) -> RunRecord:
    payload = RunPayloadEnvelope.model_construct(budget_set_snapshot_id=budget_set_snapshot_id)
    return RunRecord.model_construct(
        run_id="run:1",
        payload=payload,
        status="queued",
        next_attempt_no=1,
        next_fencing_token=1,
        current_attempt_no=None,
        concurrency_permit_group_id=None,
        budget_set_snapshot_id=budget_set_snapshot_id,
        run_budget_hold_group_id=hold_group_id,
    )


@pytest.mark.parametrize(
    ("release_clock", "lease_status", "expected_permit_status"),
    (
        (NOW, "active", "released"),
        (NOW + timedelta(minutes=6), "expired", "expired"),
    ),
)
def test_real_run_cost_adapter_binds_hold_permit_lease_and_terminal_transition(
    engine: Engine,
    release_clock: datetime,
    lease_status: str,
    expected_permit_status: str,
) -> None:
    budgets = (
        budget("run", "run:1"),
        budget("principal", "principal:1"),
        budget("system", "system:1"),
    )
    selected_set = budget_set("run:1", budgets)
    parent, members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="stranded")
    provider = _PlanProvider(RunBudgetPlan(selected_set, parent, members))
    settlement_provider = _SettlementProvider({child.reservation_group_id: 30})
    actor = AuditActor(principal_id="human:1", principal_kind="human")

    with uow(engine).begin() as transaction:
        for item in budgets:
            transaction.cost.put_budget(item)
        accounting = SqlRunCostAccounting(
            ledger=transaction.cost,
            plan_provider=provider,
            settlement_provider=settlement_provider,
            clock=FrozenUtcClock(NOW),
        )
        assert (
            accounting.reserve_run_budget(
                run_id="run:1",
                budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
                request_hash="a" * 64,
                initiated_by=actor,
            )
            == parent.reservation_group_id
        )

    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)

    queued = _queued_run(
        budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
        hold_group_id=parent.reservation_group_id,
    )
    with uow(engine).begin() as transaction:
        accounting = SqlRunCostAccounting(
            ledger=transaction.cost,
            plan_provider=provider,
            settlement_provider=settlement_provider,
            clock=FrozenUtcClock(NOW),
        )
        permit_group_id = accounting.acquire_execution_permits(
            run=queued,
            attempt_no=1,
            fencing_token=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            expires_at=(NOW + timedelta(minutes=5)).isoformat(),
        )
        permit_group = transaction.cost.get_permit_group(permit_group_id)
        assert permit_group is not None
        assert permit_group.lease_id == "lease:1"
        assert permit_group.fencing_token == 1
        assert len(permit_group.permit_ids) == 3
        transaction.cost.reserve_many(child, child_members)

    running = queued.model_copy(
        update={
            "status": "running",
            "current_attempt_no": 1,
            "concurrency_permit_group_id": permit_group_id,
        }
    )
    attempt = RunAttempt.model_construct(attempt_no=1, fencing_token=1)
    lease = RunLease.model_construct(
        lease_id="lease:1",
        attempt_no=1,
        fencing_token=1,
        status=lease_status,
    )
    with uow(engine, clock=FrozenUtcClock(release_clock)).begin() as transaction:
        accounting = SqlRunCostAccounting(
            ledger=transaction.cost,
            plan_provider=provider,
            settlement_provider=settlement_provider,
            clock=FrozenUtcClock(release_clock),
        )
        assert accounting.retry_budget_available(run=running) is True
        accounting.release_attempt(
            run=running,
            attempt=attempt,
            lease=lease,
            retry_decision=None,
        )
        accounting.close_run(run=running, terminal_status="succeeded")

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        assert ledger.get_permit_group(permit_group_id).status == expected_permit_status  # type: ignore[union-attr]
        assert ledger.get_reservation_group(parent.reservation_group_id).status == "released"  # type: ignore[union-attr]
        assert (
            ledger.get_reservation_group(child.reservation_group_id).status
            == "conservatively_settled"
        )  # type: ignore[union-attr]
        assert [item.usage_id for item in ledger.list_usage(run_id="run:1")] == [
            f"usage:conservative:{child.reservation_group_id}"
        ]
        assert all(
            item.status not in {"reserved", "held_unknown"}
            for item in ledger.list_attempt_reservation_groups(
                run_id="run:1",
                attempt_no=1,
            )
        )
        for item in budgets:
            stored = ledger.get_budget(item.budget_id)
            assert stored is not None
            assert stored.reserved == ()


def test_run_budget_plan_cannot_redirect_the_payload_snapshot_binding(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, members = hold(selected_set)
    provider = _PlanProvider(RunBudgetPlan(selected_set, parent, members))
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        accounting = SqlRunCostAccounting(
            ledger=transaction.cost,
            plan_provider=provider,
            settlement_provider=_SettlementProvider({}),
            clock=FrozenUtcClock(NOW),
        )
        with pytest.raises(IntegrityViolation, match="differs"):
            accounting.reserve_run_budget(
                run_id="run:1",
                budget_set_snapshot_id="budget-set:redirected",
                request_hash="a" * 64,
                initiated_by=AuditActor(
                    principal_id="human:1",
                    principal_kind="human",
                ),
            )


@pytest.mark.parametrize(
    ("lease_id", "lease_status", "expected_error", "match"),
    (
        ("lease:wrong", "active", IntegrityViolation, "PermitGroup"),
        ("lease:1", "expired", QuotaExceeded, "not yet expired"),
    ),
)
def test_attempt_settlement_and_permit_transition_roll_back_together(
    engine: Engine,
    lease_id: str,
    lease_status: str,
    expected_error: type[Exception],
    match: str,
) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="stranded")
    plan_provider = _PlanProvider(RunBudgetPlan(selected_set, parent, parent_members))
    settlement_provider = _SettlementProvider({child.reservation_group_id: 30})
    queued = _queued_run(
        budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
        hold_group_id=parent.reservation_group_id,
    )

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        accounting = SqlRunCostAccounting(
            ledger=transaction.cost,
            plan_provider=plan_provider,
            settlement_provider=settlement_provider,
            clock=FrozenUtcClock(NOW),
        )
        permit_group_id = accounting.acquire_execution_permits(
            run=queued,
            attempt_no=1,
            fencing_token=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            expires_at=(NOW + timedelta(minutes=5)).isoformat(),
        )
        transaction.cost.reserve_many(child, child_members)

    running = queued.model_copy(
        update={
            "status": "running",
            "current_attempt_no": 1,
            "concurrency_permit_group_id": permit_group_id,
        }
    )
    with pytest.raises(expected_error, match=match):
        with uow(engine).begin() as transaction:
            SqlRunCostAccounting(
                ledger=transaction.cost,
                plan_provider=plan_provider,
                settlement_provider=settlement_provider,
                clock=FrozenUtcClock(NOW),
            ).release_attempt(
                run=running,
                attempt=RunAttempt.model_construct(attempt_no=1, fencing_token=1),
                lease=RunLease.model_construct(
                    lease_id=lease_id,
                    attempt_no=1,
                    fencing_token=1,
                    status=lease_status,
                ),
                retry_decision=None,
            )

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        assert ledger.get_reservation_group(child.reservation_group_id).status == "reserved"  # type: ignore[union-attr]
        assert ledger.list_usage(run_id="run:1") == ()
        assert ledger.get_permit_group(permit_group_id).status == "active"  # type: ignore[union-attr]
