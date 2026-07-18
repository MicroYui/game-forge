from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, event, select, text
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
from gameforge.runtime.persistence.models import RunHoldBalanceRow
from tests.runtime.cost.ledger_testkit import (
    NOW,
    amount,
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

    def conservative_usage_many(
        self,
        *,
        groups: tuple[
            tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]],
            ...,
        ],
        recorded_at: datetime,
    ) -> tuple[UsageEntryV1, ...]:
        return tuple(
            self.conservative_usage(
                group=group,
                reservations=reservations,
                recorded_at=recorded_at,
            )
            for group, reservations in groups
        )


class _AdvancingClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def now_utc(self) -> datetime:
        return next(self._values)


@pytest.fixture
def engine(tmp_path) -> Engine:
    selected = get_engine(f"sqlite:///{tmp_path / 'run-cost.db'}")
    migrations_api.upgrade(str(selected.url), "head")
    yield selected
    selected.dispose()


def test_terminal_cost_attempt_selector_uses_covering_index_without_sort(
    engine: Engine,
) -> None:
    statement = """
        EXPLAIN QUERY PLAN
        SELECT reservation_group_id
        FROM reservation_groups
        WHERE run_id = :run_id AND attempt_no = :attempt_no
        ORDER BY created_at, reservation_group_id
        LIMIT 32769
        """
    parameters = {"run_id": "run:1", "attempt_no": 1}
    index_name = "ix_reservation_groups_run_attempt"
    with engine.connect() as connection:
        details = tuple(
            str(row[3]) for row in connection.execute(text(statement), parameters).all()
        )
        assert any(f"USING COVERING INDEX {index_name}" in detail for detail in details)
        assert not any("SCAN reservation_groups" in detail for detail in details)
        assert not any("USE TEMP B-TREE" in detail for detail in details)


def test_next_reserve_authority_lookups_use_exact_indexes_without_scans(
    engine: Engine,
) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(
        selected_set,
        parent,
        suffix="indexed-a",
        input_tokens=1,
    )
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)

    selected_statements: list[tuple[str, tuple[object, ...] | dict[str, object]]] = []

    def record_select(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        executemany: bool,
    ) -> None:
        if statement.lstrip().split(None, 1)[0].upper() != "SELECT":
            return
        assert not executemany
        if isinstance(parameters, dict):
            retained_parameters: tuple[object, ...] | dict[str, object] = dict(parameters)
        else:
            assert isinstance(parameters, tuple)
            retained_parameters = tuple(parameters)
        selected_statements.append((statement, retained_parameters))

    event.listen(engine, "before_cursor_execute", record_select)
    try:
        with uow(engine).begin() as transaction:
            transaction.cost.reserve_many(child, child_members)
    finally:
        event.remove(engine, "before_cursor_execute", record_select)

    expected_indexes = (
        "sqlite_autoindex_reservation_groups_1",
        "sqlite_autoindex_reservation_groups_2",
        "sqlite_autoindex_runs_1",
        "sqlite_autoindex_run_attempts_1",
        "uq_run_active_lease",
        "sqlite_autoindex_reservation_groups_1",
        "sqlite_autoindex_budget_reservations_2",
        "sqlite_autoindex_run_hold_balances_1",
        "sqlite_autoindex_budgets_1",
    )
    assert len(selected_statements) == len(expected_indexes) == 9
    with engine.connect() as connection:
        for (statement, parameters), expected_index in zip(
            selected_statements,
            expected_indexes,
            strict=True,
        ):
            details = tuple(
                str(row[3])
                for row in connection.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {statement}",
                    parameters,
                ).all()
            )
            assert any("SEARCH " in detail and expected_index in detail for detail in details), (
                statement,
                details,
            )
            assert not any("SCAN " in detail for detail in details), (statement, details)
            assert not any("USE TEMP B-TREE" in detail for detail in details), (
                statement,
                details,
            )


def _assert_no_reservation_history_scan(
    statements: tuple[tuple[str, str, bool, int], ...],
) -> None:
    for operation, statement, _executemany, _parameter_count in statements:
        if operation != "SELECT":
            continue
        assert "usage_entries" not in statement
        assert re.search(r"\bparent_hold_group_id\s*(?:=|in\b)", statement) is None


def test_hold_balance_keeps_remaining_and_next_reserve_statement_shape_constant(
    tmp_path,
) -> None:
    profiles: list[tuple[int, tuple[str, ...], int, tuple[str, ...]]] = []
    for history_count in (0, 1, 8, 32):
        selected_engine = get_engine(f"sqlite:///{tmp_path / f'hold-balance-{history_count}.db'}")
        migrations_api.upgrade(str(selected_engine.url), "head")
        try:
            selected_budget = budget("run", "run:1").model_copy(
                update={
                    "limits": (
                        amount("input_token", 10_000),
                        amount("agent_step", 10_000),
                        amount("concurrent_run", 1),
                    )
                }
            )
            selected_set = budget_set("run:1", (selected_budget,))
            parent, parent_members = hold(
                selected_set,
                input_tokens=10_000,
                agent_steps=10_000,
            )
            with uow(selected_engine).begin() as transaction:
                transaction.cost.put_budget(selected_budget)
                transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
            with Session(selected_engine) as session, session.begin():
                seed_current_attempt(session, selected_set=selected_set, parent=parent)
            with uow(selected_engine).begin() as transaction:
                for ordinal in range(history_count):
                    transaction.cost.reserve_many(
                        *step_group(
                            selected_set,
                            parent,
                            suffix=f"history-{ordinal:x}-a",
                            input_tokens=1,
                        )
                    )

            statements: list[tuple[str, str, bool, int]] = []

            def record_statement(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                executemany: bool,
            ) -> None:
                operation = statement.lstrip().split(None, 1)[0].upper()
                if operation in {"SELECT", "INSERT", "UPDATE", "DELETE"}:
                    statements.append(
                        (
                            operation,
                            " ".join(statement.lower().split()),
                            executemany,
                            len(_parameters) if executemany else 1,  # type: ignore[arg-type]
                        )
                    )

            event.listen(selected_engine, "before_cursor_execute", record_statement)
            try:
                with uow(selected_engine).begin() as transaction:
                    transaction.cost.remaining_hold_amounts(parent.reservation_group_id)
                remaining = tuple(statements)
                _assert_no_reservation_history_scan(remaining)
                statements.clear()
                with uow(selected_engine).begin() as transaction:
                    transaction.cost.reserve_many(
                        *step_group(
                            selected_set,
                            parent,
                            suffix="target-f",
                            input_tokens=1,
                        )
                    )
                reserve = tuple(statements)
                _assert_no_reservation_history_scan(reserve)
            finally:
                event.remove(selected_engine, "before_cursor_execute", record_statement)
            profiles.append(
                (
                    sum(item[0] == "SELECT" for item in remaining),
                    tuple(item[0] for item in remaining if item[0] != "SELECT"),
                    sum(item[0] == "SELECT" for item in reserve),
                    tuple(item[0] for item in reserve if item[0] != "SELECT"),
                )
            )
        finally:
            selected_engine.dispose()

    assert len(set(profiles)) == 1
    remaining_selects, remaining_writes, reserve_selects, reserve_writes = profiles[0]
    assert remaining_selects <= 3
    assert remaining_writes == ()
    assert reserve_selects <= 9
    assert len(reserve_writes) <= 5


def test_next_reserve_statement_shape_is_constant_across_budget_scopes(tmp_path) -> None:
    profiles: list[tuple[int, tuple[str, ...]]] = []
    for scope_count in (1, 8):
        selected_engine = get_engine(f"sqlite:///{tmp_path / f'hold-scopes-{scope_count}.db'}")
        migrations_api.upgrade(str(selected_engine.url), "head")
        try:
            budgets = tuple(
                budget(
                    "system",
                    f"system:{index}",
                    suffix=f"system-{index}",
                )
                for index in range(scope_count)
            )
            selected_set = budget_set("run:1", budgets)
            parent, parent_members = hold(selected_set)
            with uow(selected_engine).begin() as transaction:
                for selected_budget in budgets:
                    transaction.cost.put_budget(selected_budget)
                transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
            with Session(selected_engine) as session, session.begin():
                seed_current_attempt(session, selected_set=selected_set, parent=parent)

            statements: list[tuple[str, str, bool, int, tuple[dict[str, object], ...]]] = []

            def record_statement(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                context: object,
                executemany: bool,
            ) -> None:
                operation = statement.lstrip().split(None, 1)[0].upper()
                if operation in {"SELECT", "INSERT", "UPDATE", "DELETE"}:
                    compiled_parameters = tuple(
                        dict(item) for item in (getattr(context, "compiled_parameters", ()) or ())
                    )
                    statements.append(
                        (
                            operation,
                            " ".join(statement.lower().split()),
                            executemany,
                            len(_parameters) if executemany else 1,  # type: ignore[arg-type]
                            compiled_parameters,
                        )
                    )

            event.listen(selected_engine, "before_cursor_execute", record_statement)
            try:
                target_group, target_members = step_group(
                    selected_set,
                    parent,
                    suffix="target-a",
                    input_tokens=1,
                )
                with uow(selected_engine).begin() as transaction:
                    transaction.cost.reserve_many(target_group, target_members)
            finally:
                event.remove(selected_engine, "before_cursor_execute", record_statement)
            _assert_no_reservation_history_scan(tuple(item[:4] for item in statements))
            profiles.append(
                (
                    sum(item[0] == "SELECT" for item in statements),
                    tuple(item[0] for item in statements if item[0] != "SELECT"),
                )
            )
            if scope_count == 8:
                expected_budget_ids = tuple(sorted(item.budget_id for item in budgets))
                expected_parent_reservation_ids = tuple(
                    item.reservation_id
                    for item in sorted(parent_members, key=lambda item: item.budget_id)
                )
                child_insert = tuple(
                    item
                    for item in statements
                    if item[0] == "INSERT" and "into budget_reservations" in item[1]
                )
                parent_member_update = tuple(
                    item
                    for item in statements
                    if item[0] == "UPDATE" and "update budget_reservations" in item[1]
                )
                balance_update = tuple(
                    item
                    for item in statements
                    if item[0] == "UPDATE" and "update run_hold_balances" in item[1]
                )
                assert len(child_insert) == len(parent_member_update) == len(balance_update) == 1
                assert child_insert[0][2:4] == (True, 8)
                assert parent_member_update[0][2:4] == (True, 8)
                assert balance_update[0][2:4] == (True, 8)
                assert tuple(str(item["reservation_id"]) for item in child_insert[0][4]) == tuple(
                    sorted(item.reservation_id for item in target_members)
                )
                assert (
                    tuple(
                        str(item["expected_reservation_id"]) for item in parent_member_update[0][4]
                    )
                    == expected_parent_reservation_ids
                )
                assert (
                    tuple(str(item["expected_hold_budget_id"]) for item in balance_update[0][4])
                    == expected_budget_ids
                )

                with Session(selected_engine) as session:
                    ledger = SqlCostLedger(session)
                    parent_aggregate = ledger.get_reservation_group_with_members(
                        parent.reservation_group_id
                    )
                    assert parent_aggregate is not None
                    retained_parent, retained_parent_members = parent_aggregate
                    assert retained_parent.revision == 2
                    assert len(retained_parent_members) == 8
                    assert all(item.revision == 2 for item in retained_parent_members)
                    child_aggregate = ledger.get_reservation_group_with_members(
                        target_group.reservation_group_id
                    )
                    assert child_aggregate is not None
                    retained_child, retained_child_members = child_aggregate
                    assert retained_child.revision == 1
                    assert len(retained_child_members) == 8
                    assert all(item.revision == 1 for item in retained_child_members)
                    assert all(
                        {amount.dimension: amount.value for amount in item.reserved}
                        == {"agent_step": 1, "input_token": 1}
                        for item in retained_child_members
                    )
                    balances = tuple(
                        session.scalars(
                            select(RunHoldBalanceRow)
                            .where(RunHoldBalanceRow.hold_group_id == parent.reservation_group_id)
                            .order_by(RunHoldBalanceRow.budget_id)
                        ).all()
                    )
                    assert tuple(item.budget_id for item in balances) == expected_budget_ids
                    assert all(item.revision == 2 for item in balances)
                    assert all(item.active_child_count == 1 for item in balances)
                    for balance in balances:
                        active = {
                            item["dimension"]: item["value"]
                            for item in balance.payload["active_allocated"]
                        }
                        settled = {
                            item["dimension"]: item["value"]
                            for item in balance.payload["settled_impact"]
                        }
                        assert active == {"agent_step": "1", "input_token": "1"}
                        assert settled == {"agent_step": "0", "input_token": "0"}
                    retained_budgets = ledger.get_budgets_many(expected_budget_ids)
                    assert all(item is not None for item in retained_budgets.values())
                    for retained_budget in retained_budgets.values():
                        assert retained_budget is not None
                        assert retained_budget.revision == 2
                        assert {
                            item.dimension: item.value for item in retained_budget.reserved
                        } == {"agent_step": 8, "input_token": 80}
        finally:
            selected_engine.dispose()

    assert profiles[0] == profiles[1]
    assert profiles[0][0] <= 9
    assert len(profiles[0][1]) <= 5


def test_hold_close_statement_shape_is_constant_across_settled_history(tmp_path) -> None:
    profiles: list[tuple[int, tuple[str, ...]]] = []
    for history_count in (0, 1, 8, 32):
        selected_engine = get_engine(f"sqlite:///{tmp_path / f'hold-close-{history_count}.db'}")
        migrations_api.upgrade(str(selected_engine.url), "head")
        try:
            selected_budget = budget("run", "run:1").model_copy(
                update={
                    "limits": (
                        amount("input_token", 10_000),
                        amount("agent_step", 10_000),
                        amount("concurrent_run", 1),
                    )
                }
            )
            selected_set = budget_set("run:1", (selected_budget,))
            parent, parent_members = hold(
                selected_set,
                input_tokens=10_000,
                agent_steps=10_000,
            )
            with uow(selected_engine).begin() as transaction:
                transaction.cost.put_budget(selected_budget)
                transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
            with Session(selected_engine) as session, session.begin():
                seed_current_attempt(session, selected_set=selected_set, parent=parent)
            with uow(selected_engine).begin() as transaction:
                for ordinal in range(history_count):
                    child, members = step_group(
                        selected_set,
                        parent,
                        suffix=f"history-{ordinal:x}-a",
                        input_tokens=1,
                    )
                    transaction.cost.reserve_many(child, members)
                    transaction.cost.reconcile_group(
                        usage(
                            child,
                            usage_id=f"usage:history:{ordinal}",
                            input_tokens=1,
                        )
                    )

            statements: list[str] = []

            def record_statement(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                operation = statement.lstrip().split(None, 1)[0].upper()
                if operation in {"SELECT", "INSERT", "UPDATE", "DELETE"}:
                    statements.append(operation)

            event.listen(selected_engine, "before_cursor_execute", record_statement)
            try:
                with uow(selected_engine).begin() as transaction:
                    transaction.cost.close_hold_group(parent.reservation_group_id)
            finally:
                event.remove(selected_engine, "before_cursor_execute", record_statement)
            profiles.append(
                (
                    statements.count("SELECT"),
                    tuple(item for item in statements if item != "SELECT"),
                )
            )
        finally:
            selected_engine.dispose()

    assert len(set(profiles)) == 1
    close_selects, close_writes = profiles[0]
    assert close_selects <= 4
    assert len(close_writes) <= 4


def test_terminal_preflight_rejects_hold_omitting_a_budget_set_scope(engine: Engine) -> None:
    budgets = (
        budget("run", "run:1"),
        budget("principal", "principal:1"),
    )
    selected_set = budget_set("run:1", budgets)
    parent, parent_members = hold(selected_set)
    omitted = next(item for item in parent_members if item.budget_id == "budget:principal")
    with uow(engine).begin() as transaction:
        for selected_budget in budgets:
            transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM run_hold_balances WHERE budget_id = :budget_id"),
            {"budget_id": omitted.budget_id},
        )
        connection.execute(
            text("DELETE FROM budget_reservations WHERE reservation_id = :reservation_id"),
            {"reservation_id": omitted.reservation_id},
        )
        raw = connection.execute(
            text("SELECT payload FROM reservation_groups WHERE reservation_group_id = :group_id"),
            {"group_id": parent.reservation_group_id},
        ).scalar_one()
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        payload["budget_reservation_ids"] = [
            value for value in payload["budget_reservation_ids"] if value != omitted.reservation_id
        ]
        connection.execute(
            text(
                "UPDATE reservation_groups SET payload = :payload "
                "WHERE reservation_group_id = :group_id"
            ),
            {
                "group_id": parent.reservation_group_id,
                "payload": json.dumps(payload, separators=(",", ":")),
            },
        )

    with uow(engine).begin() as transaction:
        with pytest.raises(IntegrityViolation, match="members differ from budget-set authority"):
            transaction.cost.preflight_terminal_closure(
                run_id=selected_set.run_id,
                budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
                hold_group_id=parent.reservation_group_id,
                attempt_no=None,
                permit_group_id=None,
                lease_id=None,
                fencing_token=None,
                lease_status=None,
                close_hold=True,
                recorded_at=NOW,
                conservative_usage_factory=lambda _groups, _recorded_at: (),
            )


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


def _terminal_closure_statement_profile(
    engine: Engine,
    *,
    group_count: int,
) -> tuple[int, tuple[str, ...]]:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    children = tuple(
        step_group(
            selected_set,
            parent,
            suffix=f"group{index:x}",
            input_tokens=1,
        )
        for index in range(1, group_count + 1)
    )
    plan_provider = _PlanProvider(RunBudgetPlan(selected_set, parent, parent_members))
    settlement_provider = _SettlementProvider(
        {group.reservation_group_id: 1 for group, _ in children}
    )
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
        for child in children:
            transaction.cost.reserve_many(*child)

    running = queued.model_copy(
        update={
            "status": "running",
            "current_attempt_no": 1,
            "concurrency_permit_group_id": permit_group_id,
        }
    )
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        with uow(engine).begin() as transaction:
            accounting = SqlRunCostAccounting(
                ledger=transaction.cost,
                plan_provider=plan_provider,
                settlement_provider=settlement_provider,
                clock=FrozenUtcClock(NOW),
            )
            preflight_start = len(statements)
            closure = accounting.preflight_terminal_closure(
                run=running,
                attempt=RunAttempt.model_construct(attempt_no=1, fencing_token=1),
                lease=RunLease.model_construct(
                    lease_id="lease:1",
                    attempt_no=1,
                    fencing_token=1,
                    status="active",
                ),
                retry_decision=None,
                terminal_status="succeeded",
            )
            preflight_statements = tuple(statements[preflight_start:])
            tamper_start = len(statements)
            with pytest.raises(TypeError, match="immutable"):
                closure._group_parameters = ()  # noqa: SLF001
            with pytest.raises((AttributeError, TypeError)):
                object.__setattr__(closure, "_consumed", False)
            with pytest.raises((AttributeError, TypeError)):
                object.__setattr__(closure, "_group_parameters", ())
            assert statements[tamper_start:] == []
            apply_start = len(statements)
            accounting.apply_preflighted_terminal_closure(closure)
            apply_statements = tuple(statements[apply_start:])
            with pytest.raises(IntegrityViolation, match="already consumed"):
                accounting.apply_preflighted_terminal_closure(closure)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert preflight_statements
    assert set(preflight_statements) == {"SELECT"}
    assert apply_statements
    assert set(apply_statements) <= {"INSERT", "UPDATE"}
    return preflight_statements.count("SELECT"), apply_statements


def test_terminal_cost_preflight_and_apply_query_counts_do_not_scale_per_group(
    tmp_path,
) -> None:
    profiles: list[tuple[int, tuple[str, ...]]] = []
    for group_count in (1, 8):
        selected_engine = get_engine(f"sqlite:///{tmp_path / f'terminal-cost-{group_count}.db'}")
        migrations_api.upgrade(str(selected_engine.url), "head")
        try:
            profiles.append(
                _terminal_closure_statement_profile(
                    selected_engine,
                    group_count=group_count,
                )
            )
        finally:
            selected_engine.dispose()

    assert profiles[0][0] == profiles[1][0]
    assert (
        profiles[0][1]
        == profiles[1][1]
        == (
            "INSERT",
            "UPDATE",
            "UPDATE",
            "UPDATE",
            "UPDATE",
            "UPDATE",
            "UPDATE",
        )
    )


def test_terminal_permit_expiry_is_decided_at_apply_time(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(
        selected_set,
        parent,
        suffix="expiry-boundary1",
        input_tokens=1,
    )
    plan_provider = _PlanProvider(RunBudgetPlan(selected_set, parent, parent_members))
    settlement_provider = _SettlementProvider({child.reservation_group_id: 1})
    queued = _queued_run(
        budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
        hold_group_id=parent.reservation_group_id,
    )
    expires_at = NOW + timedelta(minutes=5)
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
            expires_at=expires_at.isoformat(),
        )
        transaction.cost.reserve_many(child, child_members)

    running = queued.model_copy(
        update={
            "status": "running",
            "current_attempt_no": 1,
            "concurrency_permit_group_id": permit_group_id,
        }
    )
    preflight_at = expires_at - timedelta(microseconds=1)
    apply_at = expires_at + timedelta(microseconds=1)
    terminal_clock = _AdvancingClock(preflight_at, apply_at.replace(tzinfo=None), apply_at)
    with uow(engine, clock=terminal_clock).begin() as transaction:  # type: ignore[arg-type]
        accounting = SqlRunCostAccounting(
            ledger=transaction.cost,
            plan_provider=plan_provider,
            settlement_provider=settlement_provider,
            clock=terminal_clock,  # type: ignore[arg-type]
        )
        closure = accounting.preflight_terminal_closure(
            run=running,
            attempt=RunAttempt.model_construct(attempt_no=1, fencing_token=1),
            lease=RunLease.model_construct(
                lease_id="lease:1",
                attempt_no=1,
                fencing_token=1,
                status="active",
            ),
            retry_decision=None,
            terminal_status="succeeded",
        )
        with pytest.raises(IntegrityViolation, match="clock must return UTC"):
            accounting.apply_preflighted_terminal_closure(closure)
        accounting.apply_preflighted_terminal_closure(closure)

    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        permit = ledger.get_permit_group(permit_group_id)
        assert permit is not None
        assert permit.status == "expired"
        retained_usage = ledger.list_usage(run_id="run:1")
        assert len(retained_usage) == 1
        assert retained_usage[0].recorded_at == preflight_at


def test_stage_to_commit_cost_drift_fails_before_the_first_dml(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(
        selected_set,
        parent,
        suffix="drift1",
        input_tokens=1,
    )
    plan_provider = _PlanProvider(RunBudgetPlan(selected_set, parent, parent_members))
    settlement_provider = _SettlementProvider({child.reservation_group_id: 1})
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

    # Blob staging/planning retained an active permit.  A separate committed
    # writer changes that exact cost authority before terminal publication.
    with uow(engine).begin() as transaction:
        current = transaction.cost.get_permit_group(permit_group_id)
        assert current is not None
        transaction.cost.release_permit_group(
            current.model_copy(update={"status": "released", "revision": current.revision + 1})
        )

    running = queued.model_copy(
        update={
            "status": "running",
            "current_attempt_no": 1,
            "concurrency_permit_group_id": permit_group_id,
        }
    )
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(IntegrityViolation, match="PermitGroup"):
            with uow(engine).begin() as transaction:
                SqlRunCostAccounting(
                    ledger=transaction.cost,
                    plan_provider=plan_provider,
                    settlement_provider=settlement_provider,
                    clock=FrozenUtcClock(NOW),
                ).preflight_terminal_closure(
                    run=running,
                    attempt=RunAttempt.model_construct(attempt_no=1, fencing_token=1),
                    lease=RunLease.model_construct(
                        lease_id="lease:1",
                        attempt_no=1,
                        fencing_token=1,
                        status="active",
                    ),
                    retry_decision=None,
                    terminal_status="succeeded",
                )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert statements
    assert set(statements) <= {"BEGIN", "SELECT"}
    assert not ({"INSERT", "UPDATE", "DELETE"} & set(statements))
