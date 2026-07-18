from __future__ import annotations

from decimal import Decimal, localcontext

import pytest
from hypothesis import given, strategies as st
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cost import (
    BudgetReservationV1,
    BudgetV1,
    MonetaryObservationV1,
    ReservationGroupV1,
)
from gameforge.contracts.errors import IntegrityViolation, QuotaExceeded
from gameforge.runtime.cost.ledger import (
    SqlCostLedger,
    _MAX_EXACT_COST_DECIMAL_DIGITS,
    _exact_decimal_add,
    _exact_decimal_subtract,
)
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import RunHoldBalanceRow
from tests.runtime.cost.ledger_testkit import (
    NOW,
    REQUEST_HASH,
    amount,
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


def _decimal_from_parts(coefficient: int, exponent: int) -> Decimal:
    if coefficient == 0:
        return Decimal((0, (0,), exponent))
    digits = Decimal(abs(coefficient)).as_tuple().digits
    return Decimal((1 if coefficient < 0 else 0, digits, exponent))


def _extreme_monetary_settlement_case(
    engine: Engine,
) -> tuple[
    BudgetV1,
    ReservationGroupV1,
    ReservationGroupV1,
    tuple[BudgetReservationV1, ...],
]:
    selected_budget = budget("run", "run:decimal-settlement").model_copy(
        update={
            "limits": (
                *budget("run", "run:decimal-settlement").limits,
                amount("monetary", 10),
            )
        }
    )
    selected_set = budget_set("run:decimal-settlement", (selected_budget,))
    parent, raw_parent_members = hold(selected_set)
    parent_members = tuple(
        member.model_copy(update={"reserved": (*member.reserved, amount("monetary", 1))})
        for member in raw_parent_members
    )
    child, raw_child_members = step_group(
        selected_set,
        parent,
        suffix="decimal-settlement-a",
        input_tokens=1,
    )
    child_members = tuple(
        member.model_copy(update={"reserved": (*member.reserved, amount("monetary", 1))})
        for member in raw_child_members
    )
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
    return selected_budget, parent, child, child_members


def _cost_settlement_authority_snapshot(
    engine: Engine,
    *,
    budget_id: str,
    parent_group_id: str,
    child_group_id: str,
    rejected_usage_id: str,
) -> tuple[object, ...]:
    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        balance = session.get(RunHoldBalanceRow, (parent_group_id, budget_id))
        assert balance is not None
        return (
            ledger.get_budget(budget_id),
            ledger.get_reservation_group(parent_group_id),
            ledger.get_reservation_group(child_group_id),
            ledger.get_usage(rejected_usage_id),
            balance.status,
            balance.revision,
            balance.active_child_count,
            balance.balance_digest,
            canonical_sha256(balance.payload),
        )


@given(
    left_coefficient=st.integers(min_value=-(10**80), max_value=10**80),
    left_exponent=st.integers(min_value=-30, max_value=30),
    middle_coefficient=st.integers(min_value=-(10**80), max_value=10**80),
    middle_exponent=st.integers(min_value=-30, max_value=30),
    right_coefficient=st.integers(min_value=-(10**80), max_value=10**80),
    right_exponent=st.integers(min_value=-30, max_value=30),
)
def test_exact_decimal_arithmetic_is_context_independent_and_associative(
    left_coefficient: int,
    left_exponent: int,
    middle_coefficient: int,
    middle_exponent: int,
    right_coefficient: int,
    right_exponent: int,
) -> None:
    left = _decimal_from_parts(left_coefficient, left_exponent)
    middle = _decimal_from_parts(middle_coefficient, middle_exponent)
    right = _decimal_from_parts(right_coefficient, right_exponent)

    with localcontext() as context:
        context.prec = 1
        low_precision_left_fold = _exact_decimal_add(
            _exact_decimal_add(left, middle),
            right,
        )
        low_precision_right_fold = _exact_decimal_add(
            left,
            _exact_decimal_add(middle, right),
        )
        low_precision_inverse = _exact_decimal_subtract(
            _exact_decimal_add(left, middle),
            middle,
        )
    with localcontext() as context:
        context.prec = 100
        high_precision_left_fold = _exact_decimal_add(
            _exact_decimal_add(left, middle),
            right,
        )

    assert low_precision_left_fold.as_tuple() == low_precision_right_fold.as_tuple()
    assert low_precision_left_fold.as_tuple() == high_precision_left_fold.as_tuple()
    assert low_precision_inverse == left


def test_exact_decimal_span_bound_rejects_before_first_ledger_write(engine: Engine) -> None:
    assert _MAX_EXACT_COST_DECIMAL_DIGITS == 4096
    selected_budget = budget("run", "run:decimal-span").model_copy(
        update={
            "limits": (amount("monetary", Decimal("1E+1000000001")),),
            "reserved": (),
            "consumed": (),
        }
    )
    selected_set = budget_set("run:decimal-span", (selected_budget,))
    parent, raw_parent_members = hold(selected_set)
    parent_members = tuple(
        member.model_copy(update={"reserved": (amount("monetary", Decimal("1E+1000000000")),)})
        for member in raw_parent_members
    )
    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)

    operations: list[str] = []

    def capture_operation(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        operations.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(engine, "before_cursor_execute", capture_operation)
    try:
        with pytest.raises(
            IntegrityViolation,
            match="exact arithmetic exceeds its decimal digit-span bound",
        ) as captured:
            with uow(engine).begin() as transaction:
                transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    finally:
        event.remove(engine, "before_cursor_execute", capture_operation)

    assert captured.value.context == {"max_decimal_digits": 4096}
    assert operations
    assert not {"INSERT", "UPDATE", "DELETE"}.intersection(operations)
    with Session(engine) as session:
        ledger = SqlCostLedger(session)
        assert ledger.get_budget_set(selected_set.budget_set_snapshot_id) is None
        assert ledger.get_reservation_group(parent.reservation_group_id) is None
        assert ledger.get_budget(selected_budget.budget_id) == selected_budget


@pytest.mark.parametrize(
    "transition",
    ("reconcile", "settle_unknown", "late_reconcile"),
)
def test_exact_decimal_span_bound_rejects_usage_settlement_before_first_dml(
    engine: Engine,
    transition: str,
) -> None:
    selected_budget, parent, child, _child_members = _extreme_monetary_settlement_case(engine)
    conservative = usage(
        child,
        usage_id=f"usage:{transition}:conservative",
        input_tokens=1,
    ).model_copy(
        update={
            "monetary": MonetaryObservationV1(
                status="reported",
                amount=Decimal("0.1"),
                currency="USD",
                price_book_version="price-book@1",
                quote_effective_at=NOW,
            )
        }
    )
    if transition in {"settle_unknown", "late_reconcile"}:
        with uow(engine).begin() as transaction:
            transaction.cost.hold_unknown_group(child.reservation_group_id)
            if transition == "late_reconcile":
                transaction.cost.settle_unknown_group(
                    child.reservation_group_id,
                    conservative,
                )

    rejected_usage = usage(
        child,
        usage_id=f"usage:{transition}:extreme",
        input_tokens=1,
        adjustment_of_usage_id=(conservative.usage_id if transition == "late_reconcile" else None),
    ).model_copy(
        update={
            "monetary": MonetaryObservationV1(
                status="reported",
                amount=Decimal("1E+1000000000"),
                currency="USD",
                price_book_version="price-book@1",
                quote_effective_at=NOW,
            )
        }
    )
    before = _cost_settlement_authority_snapshot(
        engine,
        budget_id=selected_budget.budget_id,
        parent_group_id=parent.reservation_group_id,
        child_group_id=child.reservation_group_id,
        rejected_usage_id=rejected_usage.usage_id,
    )
    operations: list[str] = []

    def capture_operation(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        operations.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(engine, "before_cursor_execute", capture_operation)
    try:
        with pytest.raises(
            IntegrityViolation,
            match="exact arithmetic exceeds its decimal digit-span bound",
        ):
            with uow(engine).begin() as transaction:
                if transition == "reconcile":
                    transaction.cost.reconcile_group(rejected_usage)
                elif transition == "settle_unknown":
                    transaction.cost.settle_unknown_group(
                        child.reservation_group_id,
                        rejected_usage,
                    )
                else:
                    transaction.cost.late_reconcile_group(rejected_usage)
    finally:
        event.remove(engine, "before_cursor_execute", capture_operation)

    assert operations
    assert not {"INSERT", "UPDATE", "DELETE"}.intersection(operations)
    assert (
        _cost_settlement_authority_snapshot(
            engine,
            budget_id=selected_budget.budget_id,
            parent_group_id=parent.reservation_group_id,
            child_group_id=child.reservation_group_id,
            rejected_usage_id=rejected_usage.usage_id,
        )
        == before
    )


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


def test_heterogeneous_scope_children_cover_exactly_the_budgets_governing_their_vector(
    engine: Engine,
) -> None:
    def scoped_budget(scope_kind: str, scope_id: str, dimensions: tuple[str, ...]) -> BudgetV1:
        base = budget(scope_kind, scope_id)
        values = {
            "input_token": amount("input_token", 100),
            "agent_step": amount("agent_step", 10),
        }
        return BudgetV1.model_validate(
            {
                **base.model_dump(mode="python"),
                "limits": tuple(values[dimension] for dimension in dimensions),
                "reserved": (),
                "consumed": (),
            }
        )

    budgets = (
        scoped_budget("run", "run:1", ("input_token", "agent_step")),
        scoped_budget("principal", "principal:1", ("agent_step",)),
        scoped_budget("system", "system:1", ("input_token",)),
    )
    selected_set = budget_set("run:1", budgets)
    parent_id = "hold:run:1:heterogeneous"
    parent_members = tuple(
        BudgetReservationV1(
            reservation_id=f"reservation:{parent_id}:{item.budget_id}",
            reservation_group_id=parent_id,
            budget_id=item.budget_id,
            reserved=tuple(
                value
                for value in (amount("input_token", 80), amount("agent_step", 8))
                if value.dimension in {limit.dimension for limit in item.limits}
            ),
            status="reserved",
            revision=1,
        )
        for item in budgets
    )
    parent = ReservationGroupV1(
        reservation_group_id=parent_id,
        scope="run_budget_hold",
        run_id=selected_set.run_id,
        budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
        request_hash=REQUEST_HASH,
        idempotency_key="hold-idempotency:heterogeneous",
        budget_reservation_ids=tuple(item.reservation_id for item in parent_members),
        status="reserved",
        revision=1,
        created_at=NOW,
    )

    def child(
        suffix: str,
        dimension: str,
        selected_budget_ids: tuple[str, ...],
    ) -> tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]]:
        group_id = f"step:run:1:{suffix}"
        value = amount(dimension, 1 if dimension == "agent_step" else 10)
        members = tuple(
            BudgetReservationV1(
                reservation_id=f"reservation:{group_id}:{budget_id}",
                reservation_group_id=group_id,
                budget_id=budget_id,
                reserved=(value,),
                status="reserved",
                revision=1,
            )
            for budget_id in selected_budget_ids
        )
        return (
            ReservationGroupV1(
                reservation_group_id=group_id,
                scope="agent_step",
                run_id=selected_set.run_id,
                budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
                parent_hold_group_id=parent_id,
                attempt_no=1,
                request_hash=f"sha256:{suffix[-1] * 64}",
                fencing_token=1,
                idempotency_key=f"step-idempotency:{suffix}",
                budget_reservation_ids=tuple(item.reservation_id for item in members),
                status="reserved",
                revision=1,
                created_at=NOW,
                expires_at=budgets[0].deadline_utc,
            ),
            members,
        )

    step = child(
        "agent1",
        "agent_step",
        (budgets[0].budget_id, budgets[1].budget_id),
    )
    call = child(
        "input2",
        "input_token",
        (budgets[0].budget_id, budgets[2].budget_id),
    )
    omitted_scope = child("agent3", "agent_step", (budgets[0].budget_id,))

    with uow(engine).begin() as transaction:
        for item in budgets:
            transaction.cost.put_budget(item)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)

    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(*step)
        transaction.cost.reserve_many(*call)
        with pytest.raises(IntegrityViolation, match="every parent budget"):
            transaction.cost.reserve_many(*omitted_scope)
        assert transaction.cost.get_reservation_group(omitted_scope[0].reservation_group_id) is None
        assert {
            item.budget_id
            for item in transaction.cost.list_budget_reservations(step[0].reservation_group_id)
        } == {budgets[0].budget_id, budgets[1].budget_id}
        assert {
            item.budget_id
            for item in transaction.cost.list_budget_reservations(call[0].reservation_group_id)
        } == {budgets[0].budget_id, budgets[2].budget_id}


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


def test_unused_child_release_restores_full_hold_capacity_without_usage(engine: Engine) -> None:
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
        transaction.cost.reserve_many(child, child_members)
        before = amounts_by_dimension(
            transaction.cost.remaining_hold_amounts(parent.reservation_group_id)
        )
        assert before["input_token"] == 50
        released = transaction.cost.release_unused_group(child.reservation_group_id)
        assert released.status == "released"
        assert transaction.cost.release_unused_group(child.reservation_group_id) == released
        after = amounts_by_dimension(
            transaction.cost.remaining_hold_amounts(parent.reservation_group_id)
        )
        assert after["input_token"] == 80
        assert transaction.cost.list_usage(run_id=selected_set.run_id) == ()


def test_zero_allocation_child_still_blocks_close_until_released(engine: Engine) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, raw_members = step_group(selected_set, parent, suffix="0")
    child_members = tuple(
        member.model_copy(
            update={
                "reserved": tuple(
                    item.model_copy(update={"value": Decimal(0)}) for item in member.reserved
                )
            }
        )
        for member in raw_members
    )

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
        assert (
            amounts_by_dimension(
                transaction.cost.remaining_hold_amounts(parent.reservation_group_id)
            )["input_token"]
            == 80
        )
        with pytest.raises(Exception, match="active child"):
            transaction.cost.close_hold_group(parent.reservation_group_id)
        transaction.cost.release_unused_group(child.reservation_group_id)
        assert transaction.cost.close_hold_group(parent.reservation_group_id).status == "released"
        transaction.cost.audit_hold_balance(parent.reservation_group_id)


def test_runtime_rejects_active_balance_above_parent_even_with_valid_digest(
    engine: Engine,
) -> None:
    selected_budget = budget("run", "run:1")
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        row = session.get(
            RunHoldBalanceRow,
            (parent.reservation_group_id, selected_budget.budget_id),
        )
        assert row is not None
        payload = dict(row.payload)
        payload["active_child_count"] = 1
        payload["active_allocated"] = [
            {
                **item,
                "value": "81" if item["dimension"] == "input_token" else item["value"],
            }
            for item in payload["active_allocated"]
        ]
        row.active_child_count = 1
        row.payload = payload
        row.balance_digest = canonical_sha256(payload)

    with uow(engine).begin() as transaction:
        with pytest.raises(IntegrityViolation, match="active balance exceeds parent"):
            transaction.cost.remaining_hold_amounts(parent.reservation_group_id)


def test_single_child_overage_caps_hold_impact_but_consumes_full_usage(engine: Engine) -> None:
    selected_budget = budget("run", "run:1").model_copy(
        update={"limits": (amount("input_token", 200), amount("agent_step", 20))}
    )
    selected_set = budget_set("run:1", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1", input_tokens=30)
    overage = usage(child, usage_id="usage:overage:single", input_tokens=110)

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(selected_budget)
        transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(session, selected_set=selected_set, parent=parent)
    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(child, child_members)
        transaction.cost.reconcile_group(overage)
        assert (
            amounts_by_dimension(
                transaction.cost.remaining_hold_amounts(parent.reservation_group_id)
            )["input_token"]
            == 50
        )
        retained = transaction.cost.get_budget(selected_budget.budget_id)
        assert retained is not None
        assert amounts_by_dimension(retained.reserved)["input_token"] == 50
        assert amounts_by_dimension(retained.consumed)["input_token"] == 110
        transaction.cost.audit_hold_balance(parent.reservation_group_id)


def test_reused_children_may_exceed_hold_impact_without_stealing_another_run_reserve(
    engine: Engine,
) -> None:
    shared = budget("system", "system:shared").model_copy(
        update={
            "limits": (
                amount("input_token", 300),
                amount("agent_step", 30),
                amount("concurrent_run", 2),
            )
        }
    )
    first_set = budget_set("run:1", (shared,), suffix="1")
    first_hold, first_hold_members = hold(first_set, suffix="1")
    first, first_members = step_group(first_set, first_hold, suffix="a1", input_tokens=30)
    reused, reused_members = step_group(first_set, first_hold, suffix="b2", input_tokens=70)
    conservative = usage(first, usage_id="usage:conservative:1", input_tokens=10)
    actual = usage(
        first,
        usage_id="usage:actual:1",
        input_tokens=50,
        adjustment_of_usage_id=conservative.usage_id,
    )
    reused_usage = usage(reused, usage_id="usage:reused:2", input_tokens=70)

    with uow(engine).begin() as transaction:
        transaction.cost.put_budget(shared)
        transaction.cost.freeze_budget_set(first_set, first_hold, first_hold_members)
        after_first_freeze = transaction.cost.get_budget(shared.budget_id)
        assert after_first_freeze is not None
    second_set = budget_set("run:2", (after_first_freeze,), suffix="2")
    second_hold, second_hold_members = hold(second_set, suffix="2")
    with uow(engine).begin() as transaction:
        transaction.cost.freeze_budget_set(second_set, second_hold, second_hold_members)
    with Session(engine) as session, session.begin():
        seed_current_attempt(
            session,
            selected_set=first_set,
            parent=first_hold,
            lease_id="lease:run:1",
        )
        seed_current_attempt(
            session,
            selected_set=second_set,
            parent=second_hold,
            lease_id="lease:run:2",
        )

    with uow(engine).begin() as transaction:
        transaction.cost.reserve_many(first, first_members)
        transaction.cost.hold_unknown_group(first.reservation_group_id)
        transaction.cost.settle_unknown_group(first.reservation_group_id, conservative)
        transaction.cost.reserve_many(reused, reused_members)
        transaction.cost.reconcile_group(reused_usage)
        before_late = transaction.cost.get_budget(shared.budget_id)
        assert before_late is not None
        assert amounts_by_dimension(before_late.reserved)["input_token"] == 80

        transaction.cost.late_reconcile_group(actual)
        after_late = transaction.cost.get_budget(shared.budget_id)
        assert after_late is not None
        assert amounts_by_dimension(after_late.reserved)["input_token"] == 80
        assert amounts_by_dimension(after_late.consumed)["input_token"] == 120
        assert (
            amounts_by_dimension(
                transaction.cost.remaining_hold_amounts(second_hold.reservation_group_id)
            )["input_token"]
            == 80
        )
        transaction.cost.audit_hold_balance(first_hold.reservation_group_id)
        transaction.cost.audit_hold_balance(second_hold.reservation_group_id)


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
