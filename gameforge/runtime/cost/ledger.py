"""Authoritative SQLite CostLedger with all-scope atomic accounting."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Callable, Literal
from weakref import WeakKeyDictionary, WeakSet

from sqlalchemy import bindparam, func, insert, or_, select, update
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_sha256, typed_canonical_json
from gameforge.contracts.cost import (
    BudgetReservationV1,
    BudgetSetSnapshotV1,
    BudgetSnapshotV1,
    BudgetV1,
    ConcurrencyPermitV1,
    CostAmountV1,
    PermitGroupV1,
    ReservationGroupV1,
    UsageEntryV1,
)
from gameforge.contracts.errors import (
    Conflict,
    IntegrityViolation,
    InvalidStateTransition,
    QuotaExceeded,
)
from gameforge.contracts.lineage import MAX_RUNTIME_AUTHORITY_BINDINGS
from gameforge.contracts.storage import UtcClock
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.persistence.cost import SqlCostRepository
from gameforge.runtime.persistence.models import (
    BudgetReservationRow,
    BudgetRow,
    ConcurrencyPermitRow,
    PermitGroupRow,
    ReservationGroupRow,
    RunHoldBalanceRow,
    RunAttemptRow,
    RunLeaseRow,
    RunRow,
    UsageEntryRow,
)


_ReservationTerminal = Literal[
    "reconciled",
    "conservatively_settled",
    "late_reconciled",
]


_TERMINAL_COST_SEAL = object()


class _FrozenParameterDict(dict[str, object]):
    """JSON/SQLAlchemy-compatible immutable terminal parameter mapping."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("terminal cost closure parameters are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class _FrozenParameterList(list[object]):
    """JSON-compatible immutable terminal parameter list."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("terminal cost closure parameters are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    __ior__ = _immutable


def _freeze_terminal_parameter(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenParameterDict(
            {str(key): _freeze_terminal_parameter(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return _FrozenParameterList(_freeze_terminal_parameter(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_terminal_parameter(item) for item in value)
    return value


def _freeze_parameter_mapping(value: Mapping[str, object]) -> _FrozenParameterDict:
    frozen = _freeze_terminal_parameter(value)
    if not isinstance(frozen, _FrozenParameterDict):  # pragma: no cover - defensive
        raise IntegrityViolation("terminal cost parameter freeze returned another type")
    return frozen


@dataclass(frozen=True, slots=True)
class _RowTransition[ModelT]:
    current: ModelT
    updated: ModelT


@dataclass(frozen=True, slots=True)
class _HoldBalance:
    hold_group_id: str
    budget_id: str
    status: str
    revision: int
    active_child_count: int
    active_allocated: tuple[CostAmountV1, ...]
    settled_impact: tuple[CostAmountV1, ...]


@dataclass(frozen=True, slots=True)
class _PreparedCostMutation:
    """Fully materialized CostLedger rows ready for DML-only application."""

    result_group: ReservationGroupV1
    budget_parameters: tuple[_FrozenParameterDict, ...]
    group_parameters: tuple[_FrozenParameterDict, ...]
    member_parameters: tuple[_FrozenParameterDict, ...]
    hold_balance_parameters: tuple[_FrozenParameterDict, ...]


@dataclass(frozen=True, slots=True)
class _TerminalCostClosureState:
    session: Session
    transaction: object
    usage_rows: tuple[_FrozenParameterDict, ...]
    budget_parameters: tuple[_FrozenParameterDict, ...]
    group_parameters: tuple[_FrozenParameterDict, ...]
    hold_balance_parameters: tuple[_FrozenParameterDict, ...]
    member_parameters: tuple[_FrozenParameterDict, ...]
    permit_expires_at: datetime | None
    permit_force_expired: bool
    permit_group_released_parameters: _FrozenParameterDict | None
    permit_group_expired_parameters: _FrozenParameterDict | None
    permit_member_released_parameters: tuple[_FrozenParameterDict, ...]
    permit_member_expired_parameters: tuple[_FrozenParameterDict, ...]


class PreflightedTerminalCostClosure:
    """Opaque, transaction-bound, one-shot terminal CostLedger write plan."""

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        *,
        seal: object,
        session: Session,
        transaction: object,
        usage_rows: tuple[dict[str, object], ...],
        budget_parameters: tuple[dict[str, object], ...],
        group_parameters: tuple[dict[str, object], ...],
        hold_balance_parameters: tuple[dict[str, object], ...],
        member_parameters: tuple[dict[str, object], ...],
        permit_expires_at: datetime | None,
        permit_force_expired: bool,
        permit_group_released_parameters: dict[str, object] | None,
        permit_group_expired_parameters: dict[str, object] | None,
        permit_member_released_parameters: tuple[dict[str, object], ...],
        permit_member_expired_parameters: tuple[dict[str, object], ...],
    ) -> None:
        if seal is not _TERMINAL_COST_SEAL:
            raise IntegrityViolation("terminal cost closure seal is private")
        state = _TerminalCostClosureState(
            session=session,
            transaction=transaction,
            usage_rows=tuple(_freeze_parameter_mapping(item) for item in usage_rows),
            budget_parameters=tuple(_freeze_parameter_mapping(item) for item in budget_parameters),
            group_parameters=tuple(_freeze_parameter_mapping(item) for item in group_parameters),
            hold_balance_parameters=tuple(
                _freeze_parameter_mapping(item) for item in hold_balance_parameters
            ),
            member_parameters=tuple(_freeze_parameter_mapping(item) for item in member_parameters),
            permit_expires_at=permit_expires_at,
            permit_force_expired=permit_force_expired,
            permit_group_released_parameters=(
                None
                if permit_group_released_parameters is None
                else _freeze_parameter_mapping(permit_group_released_parameters)
            ),
            permit_group_expired_parameters=(
                None
                if permit_group_expired_parameters is None
                else _freeze_parameter_mapping(permit_group_expired_parameters)
            ),
            permit_member_released_parameters=tuple(
                _freeze_parameter_mapping(item) for item in permit_member_released_parameters
            ),
            permit_member_expired_parameters=tuple(
                _freeze_parameter_mapping(item) for item in permit_member_expired_parameters
            ),
        )
        with _TERMINAL_COST_CLOSURE_LOCK:
            _TERMINAL_COST_CLOSURES[self] = state

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("terminal cost closure is immutable")


_TERMINAL_COST_CLOSURE_LOCK = Lock()
_TERMINAL_COST_CLOSURES: WeakKeyDictionary[
    PreflightedTerminalCostClosure,
    _TerminalCostClosureState,
] = WeakKeyDictionary()
_CONSUMED_TERMINAL_COST_CLOSURES: WeakSet[PreflightedTerminalCostClosure] = WeakSet()


def _now_utc(clock: UtcClock) -> datetime:
    value = clock.now_utc()
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise IntegrityViolation("CostLedger clock must return UTC")
    return value


def _amount_map(values: Sequence[CostAmountV1]) -> dict[str, CostAmountV1]:
    return {item.dimension: item for item in values}


def _value_map(values: Sequence[CostAmountV1]) -> dict[str, Decimal]:
    return {item.dimension: item.value for item in values}


def _amounts_from_values(
    values: dict[str, Decimal],
    identities: dict[str, CostAmountV1],
) -> tuple[CostAmountV1, ...]:
    unknown = set(values) - set(identities)
    if unknown:
        raise IntegrityViolation(
            "CostLedger amount has no budget limit identity",
            dimension=sorted(unknown)[0],
        )
    result: list[CostAmountV1] = []
    for dimension, identity in identities.items():
        value = values.get(dimension, Decimal(0))
        if value < 0:
            raise IntegrityViolation(
                "CostLedger amount became negative",
                dimension=dimension,
            )
        if value == 0:
            continue
        result.append(identity.model_copy(update={"value": value}))
    return tuple(result)


def _same_amount_identity(left: CostAmountV1, right: CostAmountV1) -> bool:
    return (
        left.dimension == right.dimension
        and left.unit == right.unit
        and left.currency == right.currency
    )


_HOLD_BALANCE_SCHEMA_VERSION = "run-hold-balance@1"
_MAX_EXACT_COST_DECIMAL_DIGITS = 4096


def _hold_balance_wire(balance: _HoldBalance) -> dict[str, object]:
    return {
        "balance_schema_version": _HOLD_BALANCE_SCHEMA_VERSION,
        "hold_group_id": balance.hold_group_id,
        "budget_id": balance.budget_id,
        "status": balance.status,
        "revision": balance.revision,
        "active_child_count": balance.active_child_count,
        "active_allocated": [
            item.model_copy(update={"value": _canonical_balance_decimal(item.value)}).model_dump(
                mode="json"
            )
            for item in sorted(balance.active_allocated, key=lambda value: value.dimension)
        ],
        "settled_impact": [
            item.model_copy(update={"value": _canonical_balance_decimal(item.value)}).model_dump(
                mode="json"
            )
            for item in sorted(balance.settled_impact, key=lambda value: value.dimension)
        ],
    }


def _hold_balance_digest(balance: _HoldBalance) -> str:
    return canonical_sha256(_hold_balance_wire(balance))


def _zero_balance(
    parent: ReservationGroupV1,
    member: BudgetReservationV1,
) -> _HoldBalance:
    identities = tuple(
        item.model_copy(update={"value": Decimal(0)})
        for item in sorted(member.reserved, key=lambda value: value.dimension)
    )
    return _HoldBalance(
        hold_group_id=parent.reservation_group_id,
        budget_id=member.budget_id,
        status=parent.status,
        revision=parent.revision,
        active_child_count=0,
        active_allocated=identities,
        settled_impact=identities,
    )


def _balance_values(values: Sequence[CostAmountV1]) -> dict[str, Decimal]:
    return {item.dimension: item.value for item in values}


def _canonical_balance_decimal(value: Decimal) -> Decimal:
    """Canonicalize internal balance decimals without changing CostAmountV1 wire."""
    if not value.is_finite():
        raise IntegrityViolation("Run hold balance amount must be finite")
    if value.is_zero():
        return Decimal(0)
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise IntegrityViolation("Run hold balance amount must be finite")
    canonical_digits = list(digits)
    while exponent < 0 and canonical_digits[-1] == 0:
        canonical_digits.pop()
        exponent += 1
    return Decimal((sign, tuple(canonical_digits), exponent))


def _decimal_coefficient_and_exponent(value: Decimal) -> tuple[int, int]:
    if not value.is_finite():
        raise IntegrityViolation("CostLedger exact arithmetic requires finite amounts")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise IntegrityViolation("CostLedger exact arithmetic requires finite amounts")
    if len(digits) > _MAX_EXACT_COST_DECIMAL_DIGITS:
        raise IntegrityViolation(
            "CostLedger exact arithmetic exceeds its decimal digit-span bound",
            max_decimal_digits=_MAX_EXACT_COST_DECIMAL_DIGITS,
        )
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    return (-coefficient if sign else coefficient), exponent


def _decimal_from_coefficient(coefficient: int, exponent: int) -> Decimal:
    if coefficient == 0:
        return Decimal((0, (0,), exponent))
    digits = Decimal(abs(coefficient)).as_tuple().digits
    if len(digits) > _MAX_EXACT_COST_DECIMAL_DIGITS:
        raise IntegrityViolation(
            "CostLedger exact arithmetic exceeds its decimal digit-span bound",
            max_decimal_digits=_MAX_EXACT_COST_DECIMAL_DIGITS,
        )
    return Decimal((1 if coefficient < 0 else 0, digits, exponent))


def _exact_decimal_common_exponent(left: Decimal, right: Decimal) -> int:
    tuples = (left.as_tuple(), right.as_tuple())
    if (
        not left.is_finite()
        or not right.is_finite()
        or any(not isinstance(value.exponent, int) for value in tuples)
    ):
        raise IntegrityViolation("CostLedger exact arithmetic requires finite amounts")
    if any(len(value.digits) > _MAX_EXACT_COST_DECIMAL_DIGITS for value in tuples):
        raise IntegrityViolation(
            "CostLedger exact arithmetic exceeds its decimal digit-span bound",
            max_decimal_digits=_MAX_EXACT_COST_DECIMAL_DIGITS,
        )
    exponents = tuple(int(value.exponent) for value in tuples)
    common_exponent = min(exponents)
    aligned_spans = tuple(
        len(value.digits) + exponent - common_exponent
        for value, exponent in zip(tuples, exponents, strict=True)
        if any(value.digits)
    )
    if aligned_spans and max(aligned_spans) > _MAX_EXACT_COST_DECIMAL_DIGITS:
        raise IntegrityViolation(
            "CostLedger exact arithmetic exceeds its decimal digit-span bound",
            max_decimal_digits=_MAX_EXACT_COST_DECIMAL_DIGITS,
        )
    return common_exponent


def _exact_decimal_add(left: Decimal, right: Decimal) -> Decimal:
    """Add finite decimals exactly, independently of the ambient Decimal context."""
    common_exponent = _exact_decimal_common_exponent(left, right)
    left_coefficient, left_exponent = _decimal_coefficient_and_exponent(left)
    right_coefficient, right_exponent = _decimal_coefficient_and_exponent(right)
    coefficient = (
        0 if left_coefficient == 0 else left_coefficient * (10 ** (left_exponent - common_exponent))
    )
    coefficient += (
        0
        if right_coefficient == 0
        else right_coefficient * (10 ** (right_exponent - common_exponent))
    )
    return _decimal_from_coefficient(coefficient, common_exponent)


def _exact_decimal_subtract(left: Decimal, right: Decimal) -> Decimal:
    """Subtract finite decimals exactly, independently of the ambient Decimal context."""
    common_exponent = _exact_decimal_common_exponent(left, right)
    left_coefficient, left_exponent = _decimal_coefficient_and_exponent(left)
    right_coefficient, right_exponent = _decimal_coefficient_and_exponent(right)
    coefficient = (
        0 if left_coefficient == 0 else left_coefficient * (10 ** (left_exponent - common_exponent))
    )
    coefficient -= (
        0
        if right_coefficient == 0
        else right_coefficient * (10 ** (right_exponent - common_exponent))
    )
    return _decimal_from_coefficient(coefficient, common_exponent)


def _balance_amounts(
    values: Mapping[str, Decimal],
    member: BudgetReservationV1,
) -> tuple[CostAmountV1, ...]:
    identities = _amount_map(member.reserved)
    if set(values) != set(identities):
        raise IntegrityViolation("Run hold balance dimensions differ from parent authority")
    result: list[CostAmountV1] = []
    for dimension in sorted(identities):
        value = values[dimension]
        if value < 0:
            raise IntegrityViolation(
                "Run hold balance became negative",
                budget_id=member.budget_id,
                dimension=dimension,
            )
        result.append(
            identities[dimension].model_copy(update={"value": _canonical_balance_decimal(value)})
        )
    return tuple(result)


def _available_values(
    member: BudgetReservationV1,
    balance: _HoldBalance,
) -> dict[str, Decimal]:
    held = _value_map(member.reserved)
    active = _balance_values(balance.active_allocated)
    settled = _balance_values(balance.settled_impact)
    return {
        dimension: max(
            _exact_decimal_subtract(
                _exact_decimal_subtract(
                    value,
                    active.get(dimension, Decimal(0)),
                ),
                settled.get(dimension, Decimal(0)),
            ),
            Decimal(0),
        )
        for dimension, value in held.items()
    }


def _hold_contribution_values(
    member: BudgetReservationV1,
    balance: _HoldBalance,
) -> dict[str, Decimal]:
    if balance.status == "released":
        return {dimension: Decimal(0) for dimension in _value_map(member.reserved)}
    active = _balance_values(balance.active_allocated)
    available = _available_values(member, balance)
    return {
        dimension: _exact_decimal_add(
            active.get(dimension, Decimal(0)),
            available[dimension],
        )
        for dimension in available
    }


def _require_complete_child_projection(
    *,
    parent_members: dict[str, BudgetReservationV1],
    child_members: dict[str, BudgetReservationV1],
) -> None:
    """Require every parent budget that governs the child's declared vector."""

    declared_dimensions = {
        amount.dimension for member in child_members.values() for amount in member.reserved
    }
    expected_dimensions: dict[str, set[str]] = {}
    for budget_id, parent in parent_members.items():
        parent_dimensions = {amount.dimension for amount in parent.reserved}
        governed = parent_dimensions & declared_dimensions
        if governed:
            expected_dimensions[budget_id] = governed
    if set(child_members) != set(expected_dimensions):
        raise IntegrityViolation(
            "child reservation does not cover every parent budget governing its dimensions"
        )
    for budget_id, child in child_members.items():
        actual_dimensions = {amount.dimension for amount in child.reserved}
        if actual_dimensions != expected_dimensions[budget_id]:
            raise IntegrityViolation(
                "child reservation dimension projection differs from its parent hold",
                budget_id=budget_id,
            )


def _run_hold_budget_dimensions(
    budget_set: BudgetSetSnapshotV1,
) -> dict[str, set[str]]:
    """Return exactly the budgets/dimensions governed by the durable Run hold.

    ``concurrent_run`` belongs only to lease-scoped permits. A budget containing
    no other dimension remains an immutable member of the BudgetSetSnapshot but
    deliberately has no BudgetReservation in the run-level hold.
    """

    result: dict[str, set[str]] = {}
    for snapshot in budget_set.snapshots:
        dimensions = {
            amount.dimension for amount in snapshot.limits if amount.dimension != "concurrent_run"
        }
        if dimensions:
            result[snapshot.budget_id] = dimensions
    return result


def _without_mutable_reservation_fields(value: ReservationGroupV1) -> dict[str, object]:
    payload = value.model_dump(mode="json")
    payload.pop("status")
    payload.pop("revision")
    return payload


def _without_mutable_reservation_member_fields(
    value: BudgetReservationV1,
) -> dict[str, object]:
    payload = value.model_dump(mode="json")
    payload.pop("status")
    payload.pop("revision")
    return payload


def _without_mutable_permit_fields(value: PermitGroupV1) -> dict[str, object]:
    payload = value.model_dump(mode="json")
    payload.pop("status")
    payload.pop("revision")
    payload.pop("expires_at")
    return payload


def _without_mutable_permit_member_fields(
    value: ConcurrencyPermitV1,
) -> dict[str, object]:
    payload = value.model_dump(mode="json")
    payload.pop("status")
    payload.pop("revision")
    payload.pop("expires_at")
    return payload


def _usage_amounts(usage: UsageEntryV1) -> tuple[CostAmountV1, ...]:
    values: list[CostAmountV1] = []
    token_usage = usage.token_usage
    if token_usage.status == "reported":
        for dimension, value in (
            ("input_token", token_usage.input_tokens),
            ("output_token", token_usage.output_tokens),
            ("cache_read_token", token_usage.cache_read_tokens),
            ("cache_write_token", token_usage.cache_write_tokens),
        ):
            if value is not None:
                values.append(
                    CostAmountV1(
                        dimension=dimension,
                        value=Decimal(value),
                        unit="token",
                    )
                )
    if usage.scope == "attempt_call":
        values.append(CostAmountV1(dimension="request", value=1, unit="request"))
    else:
        values.append(CostAmountV1(dimension="agent_step", value=1, unit="step"))
    values.append(
        CostAmountV1(
            dimension="wall_time_ns",
            value=usage.wall_time_ns,
            unit="ns",
        )
    )
    monetary = usage.monetary
    if monetary.status == "reported":
        values.append(
            CostAmountV1(
                dimension="monetary",
                value=monetary.amount or Decimal(0),
                unit="currency",
                currency=monetary.currency,
            )
        )
    return tuple(values)


def _usage_is_known_for(
    usage: UsageEntryV1,
    reservations: Sequence[BudgetReservationV1],
) -> bool:
    observed = _amount_map(_usage_amounts(usage))
    required = {item.dimension for reservation in reservations for item in reservation.reserved}
    return required.issubset(observed)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrityViolation("stored CostLedger deadline is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise IntegrityViolation("stored CostLedger deadline is not UTC")
    return parsed


class SqlCostLedger(SqlCostRepository):
    """SQLite CostLedger bound to the owning write UnitOfWork Session.

    SQLite's ``BEGIN IMMEDIATE`` serializes writers. Every mutable head still
    uses a revision predicate so the same implementation retains explicit OCC
    semantics when the adapter moves to PostgreSQL.
    """

    def __init__(self, session: Session, *, clock: UtcClock | None = None) -> None:
        super().__init__(session)
        self._clock = clock or SystemUtcClock()

    def put_reservation_group(
        self,
        group: ReservationGroupV1,
        reservations: Sequence[BudgetReservationV1],
    ) -> ReservationGroupV1:
        """Persist bootstrap authority while keeping the balance projection total."""

        existing = self.get_reservation_group(group.reservation_group_id)
        if existing is not None:
            retained = super().put_reservation_group(group, reservations)
            if retained.scope == "run_budget_hold":
                members = {
                    item.budget_id: item
                    for item in self.list_budget_reservations(retained.reservation_group_id)
                }
                self._load_hold_balances(retained, members)
            return retained

        if group.scope == "run_budget_hold":
            retained = super().put_reservation_group(group, reservations)
            self._insert_hold_balances(retained, reservations)
            return retained

        if (
            group.status != "reserved"
            or group.revision != 1
            or any(item.revision != 1 for item in reservations)
        ):
            raise IntegrityViolation("bootstrap child reservation must be newly reserved")
        self._validate_group_members(group, reservations)
        parent_aggregate = self.get_reservation_group_with_members(group.parent_hold_group_id or "")
        parent = None if parent_aggregate is None else parent_aggregate[0]
        if (
            parent is None
            or parent.scope != "run_budget_hold"
            or parent.status != "reserved"
            or parent.run_id != group.run_id
            or parent.budget_set_snapshot_id != group.budget_set_snapshot_id
        ):
            raise IntegrityViolation("child reservation has no active matching run hold")
        assert parent_aggregate is not None  # narrowed by the authority check above
        parent_members = {item.budget_id: item for item in parent_aggregate[1]}
        balances = self._load_hold_balances(parent, parent_members)
        requested_members = {item.budget_id: item for item in reservations}
        _require_complete_child_projection(
            parent_members=parent_members,
            child_members=requested_members,
        )
        available = self._hold_available(parent, parent_members, balances=balances)
        for budget_id, requested in requested_members.items():
            for item in requested.reserved:
                identity = _amount_map(parent_members[budget_id].reserved).get(item.dimension)
                if identity is None or not _same_amount_identity(identity, item):
                    raise IntegrityViolation(
                        "child reservation differs from parent hold dimensions"
                    )
                if item.value > available[budget_id].get(item.dimension, Decimal(0)):
                    raise QuotaExceeded(
                        "child reservation exceeds parent hold balance",
                        budget_id=budget_id,
                        dimension=item.dimension,
                    )

        retained = super().put_reservation_group(group, reservations)
        state_updates = dict(balances)
        for budget_id, requested in requested_members.items():
            state_updates[budget_id] = self._updated_hold_balance(
                balances[budget_id],
                parent_members[budget_id],
                active_delta=_value_map(requested.reserved),
                count_delta=1,
            )
        self._transition_parent_with_balances(
            parent,
            parent_members,
            balances,
            state_updates,
        )
        return retained

    def replace_budget(self, expected: BudgetV1, updated: BudgetV1) -> BudgetV1:
        if (
            expected.budget_id != updated.budget_id
            or expected.scope_kind != updated.scope_kind
            or expected.scope_id != updated.scope_id
            or expected.policy_version != updated.policy_version
            or expected.limits != updated.limits
            or expected.deadline_utc != updated.deadline_utc
            or expected.created_at != updated.created_at
            or updated.revision != expected.revision + 1
        ):
            raise IntegrityViolation("budget CAS attempted to change immutable identity")
        wire = updated.model_dump(mode="json")
        result = self._session.execute(
            update(BudgetRow)
            .where(
                BudgetRow.budget_id == expected.budget_id,
                BudgetRow.revision == expected.revision,
            )
            .values(
                status=updated.status,
                revision=updated.revision,
                payload=wire,
            )
        )
        if result.rowcount != 1:
            raise Conflict(
                "budget compare-and-set did not match",
                budget_id=expected.budget_id,
                expected_revision=expected.revision,
            )
        self._session.expire_all()
        return updated

    @staticmethod
    def _hold_balance_row_values(balance: _HoldBalance) -> dict[str, object]:
        wire = _hold_balance_wire(balance)
        return {
            "hold_group_id": balance.hold_group_id,
            "budget_id": balance.budget_id,
            "status": balance.status,
            "revision": balance.revision,
            "active_child_count": balance.active_child_count,
            "balance_digest": canonical_sha256(wire),
            "payload": wire,
        }

    def _insert_hold_balances(
        self,
        parent: ReservationGroupV1,
        members: Sequence[BudgetReservationV1],
    ) -> dict[str, _HoldBalance]:
        balances = {
            member.budget_id: _zero_balance(parent, member)
            for member in sorted(members, key=lambda item: item.budget_id)
        }
        if not balances:
            raise IntegrityViolation("Run hold balance requires at least one budget member")
        result = self._session.connection().execute(
            insert(RunHoldBalanceRow),
            tuple(self._hold_balance_row_values(item) for item in balances.values()),
        )
        if result.rowcount != len(balances):
            raise Conflict("Run hold balance insert count differs")
        self._session.expire_all()
        return balances

    @staticmethod
    def _parse_hold_balance_row(
        row: RunHoldBalanceRow,
        *,
        parent: ReservationGroupV1,
        member: BudgetReservationV1,
    ) -> _HoldBalance:
        payload = row.payload
        if not isinstance(payload, dict):
            raise IntegrityViolation("stored Run hold balance payload is not an object")
        expected_keys = {
            "balance_schema_version",
            "hold_group_id",
            "budget_id",
            "status",
            "revision",
            "active_child_count",
            "active_allocated",
            "settled_impact",
        }
        if set(payload) != expected_keys:
            raise IntegrityViolation("stored Run hold balance payload shape differs")

        def parse_amounts(field: str) -> tuple[CostAmountV1, ...]:
            raw = payload.get(field)
            if not isinstance(raw, list):
                raise IntegrityViolation("stored Run hold balance vector is not an array")
            try:
                parsed = tuple(CostAmountV1.model_validate(item) for item in raw)
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("stored Run hold balance amount is invalid") from exc
            if typed_canonical_json(
                [item.model_dump(mode="json") for item in parsed]
            ) != typed_canonical_json(raw):
                raise IntegrityViolation("stored Run hold balance amount is noncanonical")
            dimensions = tuple(item.dimension for item in parsed)
            if dimensions != tuple(sorted(dimensions)) or len(set(dimensions)) != len(dimensions):
                raise IntegrityViolation("stored Run hold balance dimensions are not canonical")
            identities = _amount_map(member.reserved)
            if set(dimensions) != set(identities) or any(
                not _same_amount_identity(item, identities[item.dimension]) for item in parsed
            ):
                raise IntegrityViolation(
                    "stored Run hold balance dimensions differ from parent authority"
                )
            return parsed

        count = payload.get("active_child_count")
        revision = payload.get("revision")
        if (
            payload.get("balance_schema_version") != _HOLD_BALANCE_SCHEMA_VERSION
            or payload.get("hold_group_id") != parent.reservation_group_id
            or payload.get("budget_id") != member.budget_id
            or payload.get("status") != parent.status
            or isinstance(revision, bool)
            or revision != parent.revision
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or row.hold_group_id != parent.reservation_group_id
            or row.budget_id != member.budget_id
            or row.status != parent.status
            or row.revision != parent.revision
            or row.active_child_count != count
        ):
            raise IntegrityViolation("stored Run hold balance projection differs from parent")
        balance = _HoldBalance(
            hold_group_id=parent.reservation_group_id,
            budget_id=member.budget_id,
            status=parent.status,
            revision=parent.revision,
            active_child_count=count,
            active_allocated=parse_amounts("active_allocated"),
            settled_impact=parse_amounts("settled_impact"),
        )
        if row.balance_digest != _hold_balance_digest(balance):
            raise IntegrityViolation("stored Run hold balance digest differs")
        held = _value_map(member.reserved)
        if any(item.value > held[item.dimension] for item in balance.active_allocated):
            raise IntegrityViolation("stored Run hold active balance exceeds parent authority")
        active_nonzero = any(item.value > 0 for item in balance.active_allocated)
        if count == 0 and active_nonzero:
            raise IntegrityViolation("stored Run hold balance active count differs from amounts")
        if parent.status == "released" and (count != 0 or active_nonzero):
            raise IntegrityViolation("released Run hold retains active balance")
        return balance

    def _load_hold_balances(
        self,
        parent: ReservationGroupV1,
        members: Mapping[str, BudgetReservationV1],
    ) -> dict[str, _HoldBalance]:
        if not members or any(
            item.reservation_group_id != parent.reservation_group_id
            or item.status != parent.status
            or item.revision != parent.revision
            for item in members.values()
        ):
            raise IntegrityViolation("Run hold members differ from parent authority")
        rows = self._session.scalars(
            select(RunHoldBalanceRow)
            .where(RunHoldBalanceRow.hold_group_id == parent.reservation_group_id)
            .order_by(RunHoldBalanceRow.budget_id)
        ).all()
        if {row.budget_id for row in rows} != set(members) or len(rows) != len(members):
            raise IntegrityViolation("Run hold balance set differs from parent members")
        return {
            row.budget_id: self._parse_hold_balance_row(
                row,
                parent=parent,
                member=members[row.budget_id],
            )
            for row in rows
        }

    @staticmethod
    def _updated_hold_balance(
        current: _HoldBalance,
        member: BudgetReservationV1,
        *,
        active_delta: Mapping[str, Decimal] | None = None,
        settled_delta: Mapping[str, Decimal] | None = None,
        count_delta: int = 0,
        status: str | None = None,
        revision: int | None = None,
    ) -> _HoldBalance:
        active = _balance_values(current.active_allocated)
        settled = _balance_values(current.settled_impact)
        for dimension, value in (active_delta or {}).items():
            if dimension not in active:
                raise IntegrityViolation("active Run hold delta has no parent dimension")
            active[dimension] = _exact_decimal_add(active[dimension], value)
        for dimension, value in (settled_delta or {}).items():
            if dimension not in settled:
                raise IntegrityViolation("settled Run hold delta has no parent dimension")
            settled[dimension] = _exact_decimal_add(settled[dimension], value)
        next_count = current.active_child_count + count_delta
        if next_count < 0:
            raise IntegrityViolation("Run hold active child count became negative")
        updated = _HoldBalance(
            hold_group_id=current.hold_group_id,
            budget_id=current.budget_id,
            status=current.status if status is None else status,
            revision=current.revision if revision is None else revision,
            active_child_count=next_count,
            active_allocated=_balance_amounts(active, member),
            settled_impact=_balance_amounts(settled, member),
        )
        held = _value_map(member.reserved)
        if any(item.value > held[item.dimension] for item in updated.active_allocated):
            raise IntegrityViolation("Run hold active balance exceeds parent authority")
        active_nonzero = any(item.value > 0 for item in updated.active_allocated)
        if next_count == 0 and active_nonzero:
            raise IntegrityViolation("Run hold active child delta differs from amounts")
        if updated.status == "released" and (next_count != 0 or active_nonzero):
            raise IntegrityViolation("released Run hold cannot retain active balance")
        return updated

    @staticmethod
    def _prepare_reservation_transition(
        current: ReservationGroupV1,
        current_members: Sequence[BudgetReservationV1],
        status: str,
    ) -> tuple[
        _RowTransition[ReservationGroupV1],
        tuple[_RowTransition[BudgetReservationV1], ...],
    ]:
        updated = current.model_copy(update={"status": status, "revision": current.revision + 1})
        updated_members = tuple(
            item.model_copy(update={"status": status, "revision": item.revision + 1})
            for item in current_members
        )
        return (
            _RowTransition(current=current, updated=updated),
            tuple(
                _RowTransition(current=old_member, updated=new_member)
                for old_member, new_member in zip(
                    current_members,
                    updated_members,
                    strict=True,
                )
            ),
        )

    def _prepare_parent_balance_transition(
        self,
        parent: ReservationGroupV1,
        parent_members: Mapping[str, BudgetReservationV1],
        balances: Mapping[str, _HoldBalance],
        state_updates: Mapping[str, _HoldBalance],
        *,
        status: str | None = None,
    ) -> tuple[
        _RowTransition[ReservationGroupV1],
        tuple[_RowTransition[BudgetReservationV1], ...],
        tuple[_RowTransition[_HoldBalance], ...],
    ]:
        selected_status = parent.status if status is None else status
        next_revision = parent.revision + 1
        if set(parent_members) != set(balances) or set(balances) != set(state_updates):
            raise IntegrityViolation("Run hold transition authority differs")
        updated_balances = {
            budget_id: self._updated_hold_balance(
                state_updates[budget_id],
                parent_members[budget_id],
                status=selected_status,
                revision=next_revision,
            )
            for budget_id in sorted(balances)
        }
        group_transition, member_transitions = self._prepare_reservation_transition(
            parent,
            tuple(parent_members[budget_id] for budget_id in sorted(parent_members)),
            selected_status,
        )
        if group_transition.updated.revision != next_revision:
            raise IntegrityViolation("Run hold transition revision differs")
        return (
            group_transition,
            member_transitions,
            tuple(
                _RowTransition(
                    current=balances[budget_id],
                    updated=updated_balances[budget_id],
                )
                for budget_id in sorted(balances)
            ),
        )

    def _transition_parent_with_balances(
        self,
        parent: ReservationGroupV1,
        parent_members: Mapping[str, BudgetReservationV1],
        balances: Mapping[str, _HoldBalance],
        state_updates: Mapping[str, _HoldBalance],
        *,
        status: str | None = None,
    ) -> ReservationGroupV1:
        group_transition, member_transitions, balance_transitions = (
            self._prepare_parent_balance_transition(
                parent,
                parent_members,
                balances,
                state_updates,
                status=status,
            )
        )
        connection = self._session.connection()
        self._execute_group_transitions(
            connection,
            (self._group_transition_parameters(group_transition),),
        )
        self._execute_member_transitions(
            connection,
            tuple(self._member_transition_parameters(item) for item in member_transitions),
        )
        self._execute_hold_balance_transitions(
            connection,
            tuple(self._hold_balance_transition_parameters(item) for item in balance_transitions),
        )
        self._session.expire_all()
        return group_transition.updated

    def freeze_budget_set(
        self,
        budget_set: BudgetSetSnapshotV1,
        hold_group: ReservationGroupV1,
        reservations: Sequence[BudgetReservationV1],
    ) -> ReservationGroupV1:
        hold_group, canonical_members = self._canonical_reservation_group_write(
            hold_group,
            reservations,
        )
        reservations = canonical_members
        if hold_group.scope != "run_budget_hold" or hold_group.status != "reserved":
            raise IntegrityViolation("budget freeze requires a new run_budget_hold")
        if hold_group.revision != 1 or any(item.revision != 1 for item in reservations):
            raise IntegrityViolation("budget freeze members must start at revision one")
        self._validate_group_members(hold_group, reservations)
        if (
            hold_group.run_id != budget_set.run_id
            or hold_group.budget_set_snapshot_id != budget_set.budget_set_snapshot_id
        ):
            raise IntegrityViolation("budget hold differs from its budget-set snapshot")

        replay = self._reservation_replay(hold_group, reservations)
        retained_set = self.get_budget_set(budget_set.budget_set_snapshot_id)
        if replay is not None or retained_set is not None:
            if retained_set != budget_set or replay is None:
                raise IntegrityViolation("budget freeze replay differs from retained authority")
            retained_members = {
                item.budget_id: item
                for item in self.list_budget_reservations(replay.reservation_group_id)
            }
            self._load_hold_balances(replay, retained_members)
            return replay

        members = {item.budget_id: item for item in reservations}
        hold_dimensions = _run_hold_budget_dimensions(budget_set)
        if set(members) != set(hold_dimensions):
            raise IntegrityViolation(
                "run hold must reserve every budget-set member with hold dimensions "
                "and exclude permit-only members"
            )
        current: list[tuple[BudgetSnapshotV1, BudgetV1, BudgetReservationV1]] = []
        now = _now_utc(self._clock)
        for snapshot in budget_set.snapshots:
            budget = self.get_budget(snapshot.budget_id)
            if budget is None:
                raise IntegrityViolation("budget freeze references an unavailable budget")
            self._validate_exact_snapshot(snapshot, budget)
            if budget.status != "active":
                raise QuotaExceeded("budget is not active", budget_id=budget.budget_id)
            if budget.deadline_utc is not None and now >= budget.deadline_utc:
                raise QuotaExceeded("budget deadline has expired", budget_id=budget.budget_id)
            member = members.get(budget.budget_id)
            if member is None:
                continue
            if {item.dimension for item in member.reserved} != hold_dimensions[budget.budget_id]:
                raise IntegrityViolation(
                    "run hold reservation dimensions differ from its budget snapshot",
                    budget_id=budget.budget_id,
                )
            self._validate_reservation_capacity(budget, member.reserved)
            current.append((snapshot, budget, member))

        self.put_budget_set(budget_set)
        self._insert_reservation_group_rows(hold_group, canonical_members)
        self._insert_hold_balances(hold_group, reservations)
        for _, budget, member in current:
            reserved = _value_map(budget.reserved)
            for requested in member.reserved:
                reserved[requested.dimension] = _exact_decimal_add(
                    reserved.get(requested.dimension, Decimal(0)),
                    requested.value,
                )
            updated = budget.model_copy(
                update={
                    "reserved": _amounts_from_values(reserved, _amount_map(budget.limits)),
                    "revision": budget.revision + 1,
                }
            )
            self.replace_budget(budget, updated)
        return hold_group

    def reserve_many(
        self,
        group: ReservationGroupV1,
        reservations: Sequence[BudgetReservationV1],
    ) -> ReservationGroupV1:
        group, canonical_members = self._canonical_reservation_group_write(group, reservations)
        reservations = canonical_members
        if group.scope == "run_budget_hold" or group.status != "reserved":
            raise IntegrityViolation("child reserve requires a new call or step group")
        if group.revision != 1 or any(item.revision != 1 for item in reservations):
            raise IntegrityViolation("child reservation members must start at revision one")
        self._validate_group_members(group, reservations)
        replay = self._reservation_replay(group, reservations)
        if replay is not None:
            self._validate_current_fence(group)
            return replay
        self._reject_idempotency_collision(group)
        self._validate_current_fence(group)

        parent_aggregate = self.get_reservation_group_with_members(group.parent_hold_group_id or "")
        parent = None if parent_aggregate is None else parent_aggregate[0]
        if (
            parent is None
            or parent.scope != "run_budget_hold"
            or parent.status != "reserved"
            or parent.run_id != group.run_id
            or parent.budget_set_snapshot_id != group.budget_set_snapshot_id
        ):
            raise IntegrityViolation("child reservation has no active matching run hold")
        assert parent_aggregate is not None  # narrowed by the authority check above
        parent_members = {item.budget_id: item for item in parent_aggregate[1]}
        balances = self._load_hold_balances(parent, parent_members)
        requested_members = {item.budget_id: item for item in reservations}
        _require_complete_child_projection(
            parent_members=parent_members,
            child_members=requested_members,
        )
        now = _now_utc(self._clock)
        budget_heads = self.get_budgets_many(tuple(sorted(parent_members)))
        for budget_id in sorted(parent_members):
            budget = budget_heads[budget_id]
            if budget is None or budget.status != "active":
                raise QuotaExceeded("parent hold budget is unavailable", budget_id=budget_id)
            if budget.deadline_utc is not None and now >= budget.deadline_utc:
                raise QuotaExceeded("parent hold budget deadline has expired", budget_id=budget_id)
        available = self._hold_available(parent, parent_members, balances=balances)
        for budget_id, requested in requested_members.items():
            available_values = available[budget_id]
            for item in requested.reserved:
                retained = parent_members[budget_id]
                identity = _amount_map(retained.reserved).get(item.dimension)
                if identity is None or not _same_amount_identity(identity, item):
                    raise IntegrityViolation(
                        "child reservation differs from parent hold dimensions"
                    )
                if item.value > available_values.get(item.dimension, Decimal(0)):
                    raise QuotaExceeded(
                        "child reservation exceeds parent hold balance",
                        budget_id=budget_id,
                        dimension=item.dimension,
                    )

        self._insert_reservation_group_rows(group, canonical_members)
        state_updates = dict(balances)
        for budget_id, requested in requested_members.items():
            allocated = _value_map(requested.reserved)
            state_updates[budget_id] = self._updated_hold_balance(
                balances[budget_id],
                parent_members[budget_id],
                active_delta=allocated,
                count_delta=1,
            )
        self._transition_parent_with_balances(
            parent,
            parent_members,
            balances,
            state_updates,
        )
        return group

    def retry_budget_available(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        hold_group_id: str,
    ) -> bool:
        hold_aggregate = self.get_reservation_group_with_members(hold_group_id)
        hold = None if hold_aggregate is None else hold_aggregate[0]
        budget_set = self.get_budget_set(budget_set_snapshot_id)
        if (
            hold is None
            or budget_set is None
            or hold.scope != "run_budget_hold"
            or hold.status != "reserved"
            or hold.run_id != run_id
            or budget_set.run_id != run_id
            or hold.budget_set_snapshot_id != budget_set_snapshot_id
        ):
            raise IntegrityViolation("retry budget binding differs from retained CostLedger state")
        assert hold_aggregate is not None  # narrowed by the authority check above
        members = {item.budget_id: item for item in hold_aggregate[1]}
        if set(members) != set(_run_hold_budget_dimensions(budget_set)):
            raise IntegrityViolation(
                "retry budget hold members differ from hold-bearing budget-set members"
            )
        now = _now_utc(self._clock)
        for snapshot in budget_set.snapshots:
            budget = self.get_budget(snapshot.budget_id)
            if budget is None:
                raise IntegrityViolation(
                    "retry budget-set member disappeared",
                    budget_id=snapshot.budget_id,
                )
            self._validate_retained_snapshot_identity(snapshot, budget)
            if budget.status != "active":
                return False
            if budget.deadline_utc is not None and now >= budget.deadline_utc:
                return False
        balances = self._load_hold_balances(hold, members)
        available = self._hold_available(hold, members, balances=balances)
        return all(any(value > 0 for value in values.values()) for values in available.values())

    def reconcile_group(self, usage: UsageEntryV1) -> ReservationGroupV1:
        group, members = self._load_usage_group(usage)
        retained = self._get_usage(usage.usage_id)
        if group.status == "reconciled":
            if retained == usage:
                return group
            raise IntegrityViolation("reconciled usage replay differs from retained entry")
        if group.status == "held_unknown":
            if not _usage_is_known_for(usage, members):
                return group
            raise InvalidStateTransition("known usage must settle held_unknown explicitly")
        if group.status != "reserved":
            raise InvalidStateTransition("reservation group cannot be reconciled from this state")
        if retained is not None:
            raise IntegrityViolation("usage was retained before its group settled")
        parent, parent_members, balances = self._load_parent_balance_for_child(group, members)

        if not _usage_is_known_for(usage, members):
            transitioned = self._transition_reservation(group, members, "held_unknown")
            self._transition_parent_with_balances(
                parent,
                parent_members,
                balances,
                balances,
            )
            return transitioned

        updated_balances = self._settled_child_balance_updates(
            parent_members=parent_members,
            balances=balances,
            child_members=members,
            usage=usage,
        )
        prepared = self._prepare_usage_settlement_mutation(
            group=group,
            members=members,
            group_status="reconciled",
            parent=parent,
            parent_members=parent_members,
            balances=balances,
            updated_balances=updated_balances,
            usage_after=usage,
        )
        self.put_usage(usage)
        self._apply_prepared_cost_mutation(prepared)
        return prepared.result_group

    def hold_unknown_group(self, reservation_group_id: str) -> ReservationGroupV1:
        aggregate = self.get_reservation_group_with_members(reservation_group_id)
        group = None if aggregate is None else aggregate[0]
        if group is None or group.scope == "run_budget_hold":
            raise IntegrityViolation("held_unknown requires a retained child reservation")
        assert aggregate is not None  # narrowed by the authority check above
        members = aggregate[1]
        if group.status == "held_unknown":
            return group
        if group.status != "reserved":
            raise InvalidStateTransition("reservation cannot enter held_unknown from this state")
        parent, parent_members, balances = self._load_parent_balance_for_child(group, members)
        transitioned = self._transition_reservation(group, members, "held_unknown")
        self._transition_parent_with_balances(
            parent,
            parent_members,
            balances,
            balances,
        )
        return transitioned

    def release_unused_group(self, reservation_group_id: str) -> ReservationGroupV1:
        """Release a reservation whose protected operation never started.

        This is deliberately distinct from zero usage: ``attempt_call`` usage
        always carries the fixed request charge, while a deadline/fence failure
        between reserve and transport start incurred no request at all.
        """

        aggregate = self.get_reservation_group_with_members(reservation_group_id)
        group = None if aggregate is None else aggregate[0]
        if group is None or group.scope == "run_budget_hold":
            raise IntegrityViolation("unused release requires a retained child reservation")
        assert aggregate is not None  # narrowed by the authority check above
        members = aggregate[1]
        if group.status == "released":
            return group
        if group.status != "reserved":
            raise InvalidStateTransition("only a reserved, unused child group may be released")
        parent, parent_members, balances = self._load_parent_balance_for_child(group, members)
        updated_balances = dict(balances)
        for member in members:
            allocated = _value_map(member.reserved)
            updated_balances[member.budget_id] = self._updated_hold_balance(
                balances[member.budget_id],
                parent_members[member.budget_id],
                active_delta={
                    dimension: _exact_decimal_subtract(Decimal(0), value)
                    for dimension, value in allocated.items()
                },
                count_delta=-1,
            )
        transitioned = self._transition_reservation(group, members, "released")
        self._transition_parent_with_balances(
            parent,
            parent_members,
            balances,
            updated_balances,
        )
        return transitioned

    def remaining_hold_amounts(
        self,
        reservation_group_id: str,
    ) -> tuple[CostAmountV1, ...]:
        """Return the most restrictive exact remaining amount across all scopes."""

        aggregate = self.get_reservation_group_with_members(reservation_group_id)
        hold = None if aggregate is None else aggregate[0]
        if hold is None or hold.scope != "run_budget_hold" or hold.status != "reserved":
            raise IntegrityViolation("remaining budget requires an active run hold")
        assert aggregate is not None  # narrowed by the authority check above
        members = {item.budget_id: item for item in aggregate[1]}
        balances = self._load_hold_balances(hold, members)
        available = self._hold_available(hold, members, balances=balances)
        identities: dict[str, CostAmountV1] = {}
        minima: dict[str, Decimal] = {}
        for budget_id in sorted(members):
            member_identities = _amount_map(members[budget_id].reserved)
            for dimension, value in available[budget_id].items():
                identity = member_identities[dimension]
                retained = identities.get(dimension)
                if retained is not None and not _same_amount_identity(retained, identity):
                    raise IntegrityViolation(
                        "applicable budget scopes disagree on a cost identity",
                        dimension=dimension,
                    )
                identities[dimension] = identity
                minima[dimension] = min(minima.get(dimension, value), value)
        return tuple(
            identities[dimension].model_copy(update={"value": value})
            for dimension, value in sorted(minima.items())
        )

    def list_attempt_reservation_groups(
        self,
        *,
        run_id: str,
        attempt_no: int,
    ) -> tuple[ReservationGroupV1, ...]:
        if attempt_no < 1:
            raise IntegrityViolation("attempt_no must be positive")
        rows = self._session.scalars(
            select(ReservationGroupRow)
            .where(
                ReservationGroupRow.run_id == run_id,
                ReservationGroupRow.attempt_no == attempt_no,
            )
            .order_by(ReservationGroupRow.created_at, ReservationGroupRow.reservation_group_id)
        ).all()
        groups: list[ReservationGroupV1] = []
        for row in rows:
            group = self.get_reservation_group(row.reservation_group_id)
            if group is None:
                raise IntegrityViolation("attempt reservation group disappeared")
            groups.append(group)
        return tuple(groups)

    def terminal_attempt_reservation_groups(
        self,
        *,
        run_id: str,
        attempt_nos: Sequence[int],
        limit: int,
    ) -> tuple[tuple[int, tuple[ReservationGroupV1, ...]], ...]:
        """Project all terminal-relevant attempt groups without per-row lookups."""

        selected_attempts = tuple(dict.fromkeys(attempt_nos))
        if (
            any(
                isinstance(item, bool) or not isinstance(item, int) or item < 1
                for item in selected_attempts
            )
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
        ):
            raise IntegrityViolation("terminal reservation projection is not bounded")
        if not selected_attempts:
            return ()
        rows: list[tuple[int, str]] = []
        for offset in range(0, len(selected_attempts), 900):
            chunk = selected_attempts[offset : offset + 900]
            remaining = limit - len(rows)
            selected_rows = self._session.execute(
                select(
                    ReservationGroupRow.attempt_no,
                    ReservationGroupRow.reservation_group_id,
                )
                .where(
                    ReservationGroupRow.run_id == run_id,
                    ReservationGroupRow.attempt_no.in_(chunk),
                )
                .order_by(
                    ReservationGroupRow.attempt_no,
                    ReservationGroupRow.created_at,
                    ReservationGroupRow.reservation_group_id,
                )
                .limit(remaining + 1)
            ).all()
            rows.extend((int(attempt_no), str(group_id)) for attempt_no, group_id in selected_rows)
            if len(rows) > limit:
                raise IntegrityViolation("terminal reservation authority exceeds its hard cap")
        group_ids = tuple(str(group_id) for _, group_id in rows)
        retained = self.get_reservation_groups_many(group_ids)
        grouped: dict[int, list[ReservationGroupV1]] = {
            attempt_no: [] for attempt_no in selected_attempts
        }
        for attempt_no, group_id in rows:
            group = retained.get(str(group_id))
            if group is None:
                raise IntegrityViolation("attempt reservation group disappeared")
            grouped[int(attempt_no)].append(group)
        return tuple((attempt_no, tuple(grouped[attempt_no])) for attempt_no in selected_attempts)

    def preflight_terminal_closure(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        hold_group_id: str,
        attempt_no: int | None,
        permit_group_id: str | None,
        lease_id: str | None,
        fencing_token: int | None,
        lease_status: str | None,
        close_hold: bool,
        recorded_at: datetime,
        conservative_usage_factory: Callable[
            [
                tuple[
                    tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]],
                    ...,
                ],
                datetime,
            ],
            tuple[UsageEntryV1, ...],
        ],
    ) -> PreflightedTerminalCostClosure:
        """Set-preflight complete terminal cost authority before any terminal DML.

        The returned seal is bound to this exact Session and may be consumed once.
        All group/member/usage/budget authority is loaded in bounded set statements;
        the seal contains final rows so apply performs no reads or recomputation.
        """

        if (
            not run_id
            or not budget_set_snapshot_id
            or not hold_group_id
            or (
                attempt_no is None
                and any(
                    value is not None
                    for value in (permit_group_id, lease_id, fencing_token, lease_status)
                )
            )
            or (
                attempt_no is not None
                and any(
                    value is None
                    for value in (permit_group_id, lease_id, fencing_token, lease_status)
                )
            )
        ):
            raise IntegrityViolation("terminal cost closure identity is incomplete")
        if recorded_at.tzinfo is None or recorded_at.utcoffset() != UTC.utcoffset(recorded_at):
            raise IntegrityViolation("terminal cost closure timestamp must be UTC")

        budget_set = self.get_budget_set(budget_set_snapshot_id)
        if budget_set is None or budget_set.run_id != run_id:
            raise IntegrityViolation("Run and CostLedger budget bindings differ")

        # The durable balance is current authority.  Terminal work reads only
        # this attempt plus the parent head; closing a long-lived Run must not
        # scan its historical child reservations while holding SQLite's writer
        # lock.
        selected_group_ids: dict[str, None] = {hold_group_id: None}
        if attempt_no is not None:
            attempt_group_ids = self._session.scalars(
                select(ReservationGroupRow.reservation_group_id)
                .where(
                    ReservationGroupRow.run_id == run_id,
                    ReservationGroupRow.attempt_no == attempt_no,
                )
                .order_by(
                    ReservationGroupRow.created_at,
                    ReservationGroupRow.reservation_group_id,
                )
                .limit(MAX_RUNTIME_AUTHORITY_BINDINGS + 1)
            ).all()
            for group_id in attempt_group_ids:
                selected_group_ids.setdefault(str(group_id), None)
            if len(attempt_group_ids) > MAX_RUNTIME_AUTHORITY_BINDINGS:
                raise IntegrityViolation("terminal cost authority exceeds its hard cap")
        if len(selected_group_ids) > MAX_RUNTIME_AUTHORITY_BINDINGS:
            raise IntegrityViolation("terminal cost authority exceeds its hard cap")
        group_ids = tuple(selected_group_ids)
        groups_by_id = self.get_reservation_groups_many(group_ids)
        members_by_group = self.get_budget_reservations_many(group_ids)
        groups: dict[str, ReservationGroupV1] = {}
        for group_id in group_ids:
            group = groups_by_id.get(group_id)
            if group is None:
                raise IntegrityViolation("terminal reservation authority disappeared")
            members = members_by_group[group_id]
            if tuple(item.reservation_id for item in members) != group.budget_reservation_ids:
                raise IntegrityViolation("reservation member rows differ from group authority")
            if any(
                item.reservation_group_id != group_id
                or item.status != group.status
                or item.revision != group.revision
                for item in members
            ):
                raise IntegrityViolation("reservation member heads differ from group authority")
            groups[group_id] = group

        hold = groups.get(hold_group_id)
        if (
            hold is None
            or hold.scope != "run_budget_hold"
            or hold.run_id != run_id
            or hold.budget_set_snapshot_id != budget_set_snapshot_id
        ):
            raise IntegrityViolation("Run and CostLedger budget bindings differ")
        parent_members = {item.budget_id: item for item in members_by_group[hold_group_id]}
        hold_dimensions = _run_hold_budget_dimensions(budget_set)
        if set(parent_members) != set(hold_dimensions) or any(
            {item.dimension for item in parent_members[budget_id].reserved} != dimensions
            for budget_id, dimensions in hold_dimensions.items()
        ):
            raise IntegrityViolation("Run hold members differ from budget-set authority")
        snapshots_by_budget = {snapshot.budget_id: snapshot for snapshot in budget_set.snapshots}
        if any(
            not _same_amount_identity(
                item,
                _amount_map(snapshots_by_budget[budget_id].limits)[item.dimension],
            )
            for budget_id, member in parent_members.items()
            for item in member.reserved
        ):
            raise IntegrityViolation("Run hold amount identity differs from budget-set authority")
        initial_balances = self._load_hold_balances(hold, parent_members)
        if hold.status == "released":
            if attempt_no is not None:
                raise InvalidStateTransition("attempt release cannot reuse a released Run hold")
            transaction = self._session.get_transaction()
            if transaction is None or not transaction.is_active:
                raise IntegrityViolation("terminal cost preflight requires an active transaction")
            return PreflightedTerminalCostClosure(
                seal=_TERMINAL_COST_SEAL,
                session=self._session,
                transaction=transaction,
                usage_rows=(),
                budget_parameters=(),
                group_parameters=(),
                hold_balance_parameters=(),
                member_parameters=(),
                permit_expires_at=None,
                permit_force_expired=False,
                permit_group_released_parameters=None,
                permit_group_expired_parameters=None,
                permit_member_released_parameters=(),
                permit_member_expired_parameters=(),
            )
        if hold.status != "reserved":
            raise InvalidStateTransition("run hold cannot close from its current state")

        if attempt_no is not None:
            attempt_groups = tuple(
                group for group in groups.values() if group.reservation_group_id != hold_group_id
            )
            if any(
                group.run_id != run_id
                or group.attempt_no != attempt_no
                or group.parent_hold_group_id != hold_group_id
                or group.scope == "run_budget_hold"
                or group.budget_set_snapshot_id != budget_set_snapshot_id
                for group in attempt_groups
            ):
                raise IntegrityViolation("attempt reservation has another Run hold")
        else:
            attempt_groups = ()

        all_budget_ids = tuple(snapshot.budget_id for snapshot in budget_set.snapshots)
        budgets_by_id = self.get_budgets_many(all_budget_ids)
        budgets: dict[str, BudgetV1] = {}
        for snapshot in budget_set.snapshots:
            budget = budgets_by_id.get(snapshot.budget_id)
            if budget is None:
                raise IntegrityViolation("run hold budget disappeared")
            self._validate_retained_snapshot_identity(snapshot, budget)
            budgets[budget.budget_id] = budget

        intermediate: list[tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]]] = []
        final_groups = dict(groups)
        final_members = dict(members_by_group)
        parent_transition_count = 0
        for group in attempt_groups:
            if group.status not in {"reserved", "held_unknown"}:
                continue
            members = members_by_group[group.reservation_group_id]
            _require_complete_child_projection(
                parent_members=parent_members,
                child_members={item.budget_id: item for item in members},
            )
            first_increment = 1 if group.status == "reserved" else 0
            held_group = group.model_copy(
                update={
                    "status": "held_unknown",
                    "revision": group.revision + first_increment,
                }
            )
            held_members = tuple(
                item.model_copy(
                    update={
                        "status": "held_unknown",
                        "revision": item.revision + first_increment,
                    }
                )
                for item in members
            )
            intermediate.append((held_group, held_members))
            transition_count = first_increment + 1
            parent_transition_count += transition_count
            final_groups[group.reservation_group_id] = held_group.model_copy(
                update={
                    "status": "conservatively_settled",
                    "revision": held_group.revision + 1,
                }
            )
            final_members[group.reservation_group_id] = tuple(
                item.model_copy(
                    update={
                        "status": "conservatively_settled",
                        "revision": item.revision + 1,
                    }
                )
                for item in held_members
            )

        new_usages = (
            () if not intermediate else conservative_usage_factory(tuple(intermediate), recorded_at)
        )
        if len(new_usages) != len(intermediate):
            raise IntegrityViolation("conservative settlement returned another usage count")
        native_ids: list[str] = []
        legacy_ids: list[str] = []
        usage_ids: list[str] = []
        usage_identities: list[str] = []
        for (group, members), usage in zip(intermediate, new_usages, strict=True):
            if (
                usage.reservation_group_id != group.reservation_group_id
                or usage.budget_reservation_ids != group.budget_reservation_ids
                or usage.scope != group.scope
                or usage.run_id != group.run_id
                or usage.attempt_no != group.attempt_no
                or usage.request_hash != group.request_hash
                or usage.transport_attempt != group.transport_attempt
                or usage.fencing_token_at_reserve != group.fencing_token
                or usage.adjustment_of_usage_id is not None
                or not _usage_is_known_for(usage, members)
            ):
                raise IntegrityViolation("conservative usage differs from reservation authority")
            if usage.routing_decision_kind == "native":
                native_ids.append(usage.routing_decision_id or "")
            elif usage.routing_decision_kind == "legacy_import":
                legacy_ids.append(usage.routing_decision_id or "")
            usage_ids.append(usage.usage_id)
            usage_identities.append(self.usage_identity(usage))
        if len(set(usage_ids)) != len(usage_ids) or len(set(usage_identities)) != len(
            usage_identities
        ):
            raise IntegrityViolation("terminal conservative usage identities collide")

        native = self.get_routing_decisions_many(tuple(dict.fromkeys(native_ids)))
        legacy = self.get_legacy_import_routing_decisions_many(tuple(dict.fromkeys(legacy_ids)))
        for usage in new_usages:
            if usage.routing_decision_kind == "native":
                decision = native.get(usage.routing_decision_id or "")
                if (
                    decision is None
                    or decision.run_id != usage.run_id
                    or decision.attempt_no != usage.attempt_no
                    or decision.request_hash != usage.request_hash
                    or decision.execution_source != usage.execution_source
                ):
                    raise IntegrityViolation("usage lost its exact native routing authority")
            elif usage.routing_decision_kind == "legacy_import":
                decision = legacy.get(usage.routing_decision_id or "")
                if (
                    decision is None
                    or decision.request_hash != usage.request_hash
                    or usage.execution_source != "cassette_replay"
                ):
                    raise IntegrityViolation("usage lost its exact legacy routing authority")

        collisions: list[UsageEntryRow] = []
        for offset in range(0, len(usage_ids), 900):
            id_chunk = usage_ids[offset : offset + 900]
            identity_chunk = usage_identities[offset : offset + 900]
            collisions.extend(
                self._session.scalars(
                    select(UsageEntryRow).where(
                        or_(
                            UsageEntryRow.usage_id.in_(id_chunk),
                            UsageEntryRow.usage_identity.in_(identity_chunk),
                        )
                    )
                ).all()
            )
        if collisions:
            raise IntegrityViolation("terminal conservative usage already exists")

        current_budgets = dict(budgets)
        current_balances = dict(initial_balances)
        for (_group, members), usage in zip(intermediate, new_usages, strict=True):
            next_balances = self._settled_child_balance_updates(
                parent_members=parent_members,
                balances=current_balances,
                child_members=members,
                usage=usage,
            )
            actual = _amount_map(_usage_amounts(usage))
            for member in sorted(members, key=lambda item: item.budget_id):
                budget = current_budgets.get(member.budget_id)
                if budget is None:
                    raise IntegrityViolation("settlement budget disappeared")
                limits = _amount_map(budget.limits)
                reserved = _value_map(budget.reserved)
                consumed = _value_map(budget.consumed)
                before_contribution = _hold_contribution_values(
                    parent_members[member.budget_id],
                    current_balances[member.budget_id],
                )
                after_contribution = _hold_contribution_values(
                    parent_members[member.budget_id],
                    next_balances[member.budget_id],
                )
                for dimension, previous in before_contribution.items():
                    delta = _exact_decimal_subtract(
                        after_contribution[dimension],
                        previous,
                    )
                    retained = reserved.get(dimension, Decimal(0))
                    next_reserved = _exact_decimal_add(retained, delta)
                    if next_reserved < 0:
                        raise IntegrityViolation(
                            "Run hold contribution exceeds shared budget reserve"
                        )
                    reserved[dimension] = next_reserved
                for dimension, observed in actual.items():
                    limit = limits.get(dimension)
                    if limit is None:
                        continue
                    if not _same_amount_identity(limit, observed):
                        raise IntegrityViolation("usage amount identity differs from budget limit")
                    consumed[dimension] = _exact_decimal_add(
                        consumed.get(dimension, Decimal(0)),
                        observed.value,
                    )
                current_budgets[budget.budget_id] = self._with_budget_amounts(
                    budget,
                    reserved=reserved,
                    consumed=consumed,
                )
            current_balances = next_balances

        if parent_transition_count:
            final_groups[hold_group_id] = hold.model_copy(
                update={"revision": hold.revision + parent_transition_count}
            )
            final_members[hold_group_id] = tuple(
                item.model_copy(update={"revision": item.revision + parent_transition_count})
                for item in members_by_group[hold_group_id]
            )

        if close_hold:
            if any(balance.active_child_count for balance in current_balances.values()):
                raise InvalidStateTransition("run hold has an active child reservation")
            released_balances = {
                budget_id: self._updated_hold_balance(
                    balance,
                    parent_members[budget_id],
                    status="released",
                )
                for budget_id, balance in current_balances.items()
            }
            for budget_id, parent_member in parent_members.items():
                budget = current_budgets.get(budget_id)
                if budget is None:
                    raise IntegrityViolation("run hold budget disappeared")
                reserved = _value_map(budget.reserved)
                before_contribution = _hold_contribution_values(
                    parent_member,
                    current_balances[budget_id],
                )
                after_contribution = _hold_contribution_values(
                    parent_member,
                    released_balances[budget_id],
                )
                for dimension, previous in before_contribution.items():
                    delta = _exact_decimal_subtract(
                        after_contribution[dimension],
                        previous,
                    )
                    retained = reserved.get(dimension, Decimal(0))
                    next_reserved = _exact_decimal_add(retained, delta)
                    if next_reserved < 0:
                        raise IntegrityViolation(
                            "Run hold contribution exceeds shared budget reserve"
                        )
                    reserved[dimension] = next_reserved
                current_budgets[budget_id] = self._with_budget_amounts(
                    budget,
                    reserved=reserved,
                )
            current_balances = released_balances
            current_hold = final_groups[hold_group_id]
            final_groups[hold_group_id] = current_hold.model_copy(
                update={"status": "released", "revision": current_hold.revision + 1}
            )
            final_members[hold_group_id] = tuple(
                item.model_copy(update={"status": "released", "revision": item.revision + 1})
                for item in final_members[hold_group_id]
            )

        final_hold = final_groups[hold_group_id]
        final_balances = {
            budget_id: self._updated_hold_balance(
                balance,
                parent_members[budget_id],
                status=final_hold.status,
                revision=final_hold.revision,
            )
            for budget_id, balance in current_balances.items()
        }

        permit_released_transition = None
        permit_expired_transition = None
        permit_released_member_transitions: tuple[_RowTransition[ConcurrencyPermitV1], ...] = ()
        permit_expired_member_transitions: tuple[_RowTransition[ConcurrencyPermitV1], ...] = ()
        permit_expires_at = None
        permit_force_expired = lease_status == "expired"
        if attempt_no is not None:
            permit = self.get_permit_group(permit_group_id or "")
            permit_members = self.list_concurrency_permits(permit_group_id or "")
            if (
                permit is None
                or permit.run_id != run_id
                or permit.lease_id != lease_id
                or permit.fencing_token != fencing_token
                or permit.status != "active"
            ):
                raise IntegrityViolation("attempt release differs from its PermitGroup")
            if tuple(item.permit_id for item in permit_members) != permit.permit_ids:
                raise IntegrityViolation("permit members differ from group authority")
            if any(
                item.permit_group_id != permit.permit_group_id
                or item.run_id != permit.run_id
                or item.lease_id != permit.lease_id
                or item.fencing_token != permit.fencing_token
                or item.status != permit.status
                or item.revision != permit.revision
                or item.acquired_at != permit.acquired_at
                or item.expires_at != permit.expires_at
                for item in permit_members
            ):
                raise IntegrityViolation("permit member heads differ from group authority")
            if permit_force_expired and recorded_at < permit.expires_at:
                raise QuotaExceeded("permit group is not yet expired")
            permit_expires_at = permit.expires_at
            released_permit = permit.model_copy(
                update={"status": "released", "revision": permit.revision + 1}
            )
            expired_permit = permit.model_copy(
                update={"status": "expired", "revision": permit.revision + 1}
            )
            permit_released_transition = _RowTransition(permit, released_permit)
            permit_expired_transition = _RowTransition(permit, expired_permit)
            permit_released_member_transitions = tuple(
                _RowTransition(
                    item,
                    item.model_copy(update={"status": "released", "revision": item.revision + 1}),
                )
                for item in permit_members
            )
            permit_expired_member_transitions = tuple(
                _RowTransition(
                    item,
                    item.model_copy(update={"status": "expired", "revision": item.revision + 1}),
                )
                for item in permit_members
            )

        budget_transitions = tuple(
            _RowTransition(budgets[budget_id], updated)
            for budget_id, updated in sorted(current_budgets.items())
            if updated != budgets[budget_id]
        )
        group_transitions = tuple(
            _RowTransition(groups[group_id], updated)
            for group_id, updated in sorted(final_groups.items())
            if updated != groups[group_id]
        )
        hold_balance_transitions = tuple(
            _RowTransition(initial_balances[budget_id], updated)
            for budget_id, updated in sorted(final_balances.items())
            if updated != initial_balances[budget_id]
        )
        member_transitions = tuple(
            _RowTransition(current, updated)
            for group_id in sorted(final_members)
            for current, updated in zip(
                members_by_group[group_id],
                final_members[group_id],
                strict=True,
            )
            if current != updated
        )
        transaction = self._session.get_transaction()
        if transaction is None or not transaction.is_active:
            raise IntegrityViolation("terminal cost preflight requires an active transaction")
        return PreflightedTerminalCostClosure(
            seal=_TERMINAL_COST_SEAL,
            session=self._session,
            transaction=transaction,
            usage_rows=tuple(self._usage_row_values(usage) for usage in new_usages),
            budget_parameters=tuple(
                self._budget_transition_parameters(item) for item in budget_transitions
            ),
            group_parameters=tuple(
                self._group_transition_parameters(item) for item in group_transitions
            ),
            hold_balance_parameters=tuple(
                self._hold_balance_transition_parameters(item) for item in hold_balance_transitions
            ),
            member_parameters=tuple(
                self._member_transition_parameters(item) for item in member_transitions
            ),
            permit_expires_at=permit_expires_at,
            permit_force_expired=permit_force_expired,
            permit_group_released_parameters=(
                None
                if permit_released_transition is None
                else self._permit_group_transition_parameters(permit_released_transition)
            ),
            permit_group_expired_parameters=(
                None
                if permit_expired_transition is None
                else self._permit_group_transition_parameters(permit_expired_transition)
            ),
            permit_member_released_parameters=tuple(
                self._permit_member_transition_parameters(item)
                for item in permit_released_member_transitions
            ),
            permit_member_expired_parameters=tuple(
                self._permit_member_transition_parameters(item)
                for item in permit_expired_member_transitions
            ),
        )

    def apply_preflighted_terminal_closure(
        self,
        closure: PreflightedTerminalCostClosure,
    ) -> None:
        """Consume a terminal closure using DML only (no reads or recomputation)."""

        state = None
        if type(closure) is PreflightedTerminalCostClosure:
            with _TERMINAL_COST_CLOSURE_LOCK:
                state = _TERMINAL_COST_CLOSURES.get(closure)
                if closure in _CONSUMED_TERMINAL_COST_CLOSURES:
                    state = None
        if (
            state is None
            or state.session is not self._session
            or state.transaction is not self._session.get_transaction()
            or not state.transaction.is_active  # type: ignore[attr-defined]
        ):
            raise IntegrityViolation("terminal cost closure is invalid or already consumed")

        permit_group_parameters = state.permit_group_released_parameters
        permit_member_parameters = state.permit_member_released_parameters
        if state.permit_expires_at is not None:
            apply_now = _now_utc(self._clock)
            if state.permit_force_expired and apply_now < state.permit_expires_at:
                raise QuotaExceeded("permit group is not yet expired")
            if state.permit_force_expired or apply_now >= state.permit_expires_at:
                permit_group_parameters = state.permit_group_expired_parameters
                permit_member_parameters = state.permit_member_expired_parameters
        with _TERMINAL_COST_CLOSURE_LOCK:
            if (
                _TERMINAL_COST_CLOSURES.get(closure) is not state
                or closure in _CONSUMED_TERMINAL_COST_CLOSURES
            ):
                raise IntegrityViolation("terminal cost closure is invalid or already consumed")
            _CONSUMED_TERMINAL_COST_CLOSURES.add(closure)
        connection = self._session.connection()

        if state.usage_rows:
            result = connection.execute(
                insert(UsageEntryRow),
                state.usage_rows,
            )
            if result.rowcount != len(state.usage_rows):
                raise Conflict("terminal usage insert count differs")
        self._execute_budget_transitions(connection, state.budget_parameters)
        self._execute_group_transitions(connection, state.group_parameters)
        self._execute_member_transitions(connection, state.member_parameters)
        self._execute_hold_balance_transitions(
            connection,
            state.hold_balance_parameters,
        )
        if permit_group_parameters is not None:
            self._execute_permit_group_transition(
                connection,
                permit_group_parameters,
            )
        self._execute_permit_member_transitions(
            connection,
            permit_member_parameters,
        )
        self._session.expire_all()

    def settle_unknown_group(
        self,
        reservation_group_id: str,
        conservative_usage: UsageEntryV1,
    ) -> ReservationGroupV1:
        group = self.get_reservation_group(reservation_group_id)
        if group is None:
            raise IntegrityViolation("unknown settlement references an unavailable group")
        if conservative_usage.reservation_group_id != reservation_group_id:
            raise IntegrityViolation("conservative usage references another group")
        group, members = self._load_usage_group(conservative_usage)
        retained = self._get_usage(conservative_usage.usage_id)
        if group.status in {"conservatively_settled", "late_reconciled"}:
            if retained == conservative_usage:
                return group
            raise IntegrityViolation("conservative settlement replay differs from retained entry")
        if group.status != "held_unknown":
            raise InvalidStateTransition("only held_unknown may settle conservatively")
        if conservative_usage.adjustment_of_usage_id is not None:
            raise IntegrityViolation("conservative settlement must be the original usage entry")
        if not _usage_is_known_for(conservative_usage, members):
            raise IntegrityViolation("conservative settlement must cover every reserved dimension")

        parent, parent_members, balances = self._load_parent_balance_for_child(group, members)
        updated_balances = self._settled_child_balance_updates(
            parent_members=parent_members,
            balances=balances,
            child_members=members,
            usage=conservative_usage,
        )
        prepared = self._prepare_usage_settlement_mutation(
            group=group,
            members=members,
            group_status="conservatively_settled",
            parent=parent,
            parent_members=parent_members,
            balances=balances,
            updated_balances=updated_balances,
            usage_after=conservative_usage,
        )
        self.put_usage(conservative_usage)
        self._apply_prepared_cost_mutation(prepared)
        return prepared.result_group

    def late_reconcile_group(self, adjustment: UsageEntryV1) -> ReservationGroupV1:
        group, members = self._load_usage_group(adjustment)
        retained_adjustment = self._get_usage(adjustment.usage_id)
        if group.status == "late_reconciled":
            if retained_adjustment == adjustment:
                return group
            raise IntegrityViolation("late usage replay differs from retained adjustment")
        if group.status != "conservatively_settled":
            raise InvalidStateTransition("late usage requires conservative settlement")
        if adjustment.adjustment_of_usage_id is None:
            raise IntegrityViolation("late usage must reference the conservative entry")
        conservative = self._get_usage(adjustment.adjustment_of_usage_id)
        if conservative is None or conservative.adjustment_of_usage_id is not None:
            raise IntegrityViolation("late usage has no retained conservative entry")
        self._validate_late_identity(conservative, adjustment)
        if not _usage_is_known_for(adjustment, members):
            raise IntegrityViolation("late usage must cover every reserved dimension")

        parent, parent_members, balances = self._load_parent_balance_for_child(
            group,
            members,
            allow_released=True,
        )
        conservative_amounts = _amount_map(_usage_amounts(conservative))
        actual_amounts = _amount_map(_usage_amounts(adjustment))
        updated_balances = dict(balances)
        for member in members:
            settled_delta: dict[str, Decimal] = {}
            for dimension in _value_map(member.reserved):
                before = conservative_amounts.get(dimension)
                after = actual_amounts.get(dimension)
                if before is None or after is None:
                    raise IntegrityViolation("late usage omits a held dimension")
                identity = _amount_map(parent_members[member.budget_id].reserved)[dimension]
                if not _same_amount_identity(identity, before) or not _same_amount_identity(
                    identity, after
                ):
                    raise IntegrityViolation("late usage amount identity differs from Run hold")
                allocated = _value_map(member.reserved)[dimension]
                settled_delta[dimension] = _exact_decimal_subtract(
                    min(allocated, after.value),
                    min(allocated, before.value),
                )
            updated_balances[member.budget_id] = self._updated_hold_balance(
                balances[member.budget_id],
                parent_members[member.budget_id],
                settled_delta=settled_delta,
            )
        prepared = self._prepare_usage_settlement_mutation(
            group=group,
            members=members,
            group_status="late_reconciled",
            parent=parent,
            parent_members=parent_members,
            balances=balances,
            updated_balances=updated_balances,
            usage_before=conservative,
            usage_after=adjustment,
        )
        self.put_usage(adjustment)
        self._apply_prepared_cost_mutation(prepared)
        return prepared.result_group

    def close_hold_group(self, reservation_group_id: str) -> ReservationGroupV1:
        aggregate = self.get_reservation_group_with_members(reservation_group_id)
        group = None if aggregate is None else aggregate[0]
        if group is None or group.scope != "run_budget_hold":
            raise IntegrityViolation("close_hold_group requires a retained run hold")
        assert aggregate is not None  # narrowed by the authority check above
        members = {item.budget_id: item for item in aggregate[1]}
        balances = self._load_hold_balances(group, members)
        if group.status == "released":
            return group
        if group.status != "reserved":
            raise InvalidStateTransition("run hold cannot close from its current state")
        if any(item.active_child_count for item in balances.values()):
            raise InvalidStateTransition("run hold has an active child reservation")

        released_balances = {
            budget_id: self._updated_hold_balance(
                balance,
                members[budget_id],
                status="released",
            )
            for budget_id, balance in balances.items()
        }
        budget_transitions = self._prepare_hold_accounting_transitions(
            parent_members=members,
            before=balances,
            after=released_balances,
            budget_ids=tuple(members),
        )
        group_transition, member_transitions, balance_transitions = (
            self._prepare_parent_balance_transition(
                group,
                members,
                balances,
                released_balances,
                status="released",
            )
        )
        prepared = self._prepare_cost_mutation(
            result_group=group_transition.updated,
            budget_transitions=budget_transitions,
            group_transitions=(group_transition,),
            member_transitions=member_transitions,
            hold_balance_transitions=balance_transitions,
        )
        self._apply_prepared_cost_mutation(prepared)
        return prepared.result_group

    def audit_hold_balance(self, reservation_group_id: str) -> None:
        """Offline full-history oracle for the durable Run-hold projection.

        This deliberately performs a parent table scan and must never be called
        from admission, worker, retry, or terminal paths holding a production
        writer lock.  It exists for migration verification and diagnostics.
        """

        parent = self.get_reservation_group(reservation_group_id)
        if parent is None or parent.scope != "run_budget_hold":
            raise IntegrityViolation("hold balance audit requires a retained Run hold")
        parent_members = {
            item.budget_id: item
            for item in self.list_budget_reservations(parent.reservation_group_id)
        }
        retained = self._load_hold_balances(parent, parent_members)
        child_ids = tuple(
            sorted(
                str(value)
                for value in self._session.scalars(
                    select(ReservationGroupRow.reservation_group_id).where(
                        ReservationGroupRow.parent_hold_group_id == parent.reservation_group_id
                    )
                ).all()
            )
        )
        groups = self.get_reservation_groups_many(child_ids)
        members_by_group = self.get_budget_reservations_many(child_ids)
        usage_by_group = self.get_usage_by_reservation_groups_many(child_ids)
        expected = {
            budget_id: _zero_balance(parent, member) for budget_id, member in parent_members.items()
        }
        for child_id in child_ids:
            child = groups.get(child_id)
            if (
                child is None
                or child.parent_hold_group_id != parent.reservation_group_id
                or child.scope == "run_budget_hold"
                or child.run_id != parent.run_id
                or child.budget_set_snapshot_id != parent.budget_set_snapshot_id
            ):
                raise IntegrityViolation("hold balance audit found a foreign child")
            child_members = members_by_group[child_id]
            selected_members = {item.budget_id: item for item in child_members}
            _require_complete_child_projection(
                parent_members=parent_members,
                child_members=selected_members,
            )
            usages = usage_by_group[child_id]
            final_usage: dict[str, CostAmountV1] | None = None
            if child.status in {"reserved", "held_unknown"}:
                if usages:
                    raise IntegrityViolation("active child has retained usage")
            elif child.status == "released":
                if usages:
                    raise IntegrityViolation("released child has retained usage")
            elif child.status in {
                "reconciled",
                "conservatively_settled",
                "late_reconciled",
            }:
                originals = tuple(item for item in usages if item.adjustment_of_usage_id is None)
                adjustments = tuple(
                    item for item in usages if item.adjustment_of_usage_id is not None
                )
                if len(originals) != 1:
                    raise IntegrityViolation("settled child has no unique original usage")
                final_usage = _amount_map(_usage_amounts(originals[0]))
                if child.status == "late_reconciled":
                    if (
                        len(adjustments) != 1
                        or adjustments[0].adjustment_of_usage_id != originals[0].usage_id
                    ):
                        raise IntegrityViolation("late child has invalid adjustment lineage")
                    final_usage = _amount_map(_usage_amounts(adjustments[0]))
                elif adjustments:
                    raise IntegrityViolation("non-late child has an adjustment")
            else:
                raise IntegrityViolation("hold balance audit found an unknown child status")

            for member in child_members:
                allocated = _value_map(member.reserved)
                if child.status in {"reserved", "held_unknown"}:
                    expected[member.budget_id] = self._updated_hold_balance(
                        expected[member.budget_id],
                        parent_members[member.budget_id],
                        active_delta=allocated,
                        count_delta=1,
                    )
                elif final_usage is not None:
                    impact: dict[str, Decimal] = {}
                    for dimension, value in allocated.items():
                        observed = final_usage.get(dimension)
                        if observed is None:
                            raise IntegrityViolation("settled child usage omits a held dimension")
                        identity = _amount_map(parent_members[member.budget_id].reserved)[dimension]
                        if not _same_amount_identity(identity, observed):
                            raise IntegrityViolation(
                                "settled child usage differs from hold identity"
                            )
                        impact[dimension] = min(value, observed.value)
                    expected[member.budget_id] = self._updated_hold_balance(
                        expected[member.budget_id],
                        parent_members[member.budget_id],
                        settled_delta=impact,
                    )
        if expected != retained:
            raise IntegrityViolation("durable Run hold balance differs from full history")

    def acquire_permit_group(
        self,
        group: PermitGroupV1,
        permits: Sequence[ConcurrencyPermitV1],
    ) -> PermitGroupV1:
        if group.status != "active" or group.revision != 1:
            raise IntegrityViolation("permit acquisition requires a new active group")
        if any(item.status != "active" or item.revision != 1 for item in permits):
            raise IntegrityViolation("permit acquisition members must start active at revision one")
        self._validate_permit_members(group, permits)
        replay = self._permit_replay(group, permits)
        if replay is not None:
            return replay
        budget_set = self.get_budget_set(group.budget_set_snapshot_id)
        if budget_set is None or budget_set.run_id != group.run_id:
            raise IntegrityViolation("permit group has no exact budget-set snapshot")
        expected_budget_ids = {
            snapshot.budget_id
            for snapshot in budget_set.snapshots
            if "concurrent_run" in _amount_map(snapshot.limits)
        }
        provided_budget_ids = {item.budget_id for item in permits}
        if not expected_budget_ids or provided_budget_ids != expected_budget_ids:
            raise IntegrityViolation("permit group must cover every concurrent budget scope")

        now = _now_utc(self._clock)
        if group.expires_at <= now:
            raise QuotaExceeded("permit group expiry is not in the future")
        if group.acquired_at > now:
            raise IntegrityViolation("permit acquisition time cannot be in the future")
        for budget_id in sorted(expected_budget_ids):
            budget = self.get_budget(budget_id)
            if budget is None or budget.status != "active":
                raise QuotaExceeded("concurrency budget is unavailable", budget_id=budget_id)
            if budget.deadline_utc is not None:
                if now >= budget.deadline_utc or group.expires_at > budget.deadline_utc:
                    raise QuotaExceeded("permit exceeds budget deadline", budget_id=budget_id)
            limit = _amount_map(budget.limits)["concurrent_run"].value
            if limit != limit.to_integral_value():
                raise IntegrityViolation("concurrent_run limit must be an integer")
            active = self._session.scalar(
                select(func.count())
                .select_from(ConcurrencyPermitRow)
                .where(
                    ConcurrencyPermitRow.budget_id == budget_id,
                    ConcurrencyPermitRow.status == "active",
                )
            )
            if Decimal(active or 0) >= limit:
                raise QuotaExceeded(
                    "concurrent_run capacity is exhausted",
                    budget_id=budget_id,
                )
        self.put_permit_group(group, permits)
        return group

    def renew_permit_group(self, group: PermitGroupV1) -> PermitGroupV1:
        current, members = self._load_permit_transition(group)
        if current == group:
            return current
        if (
            current.status != "active"
            or group.status != "active"
            or group.revision != current.revision + 1
            or group.acquired_at != current.acquired_at
            or group.expires_at <= current.expires_at
        ):
            raise Conflict("permit renewal compare-and-set did not match")
        now = _now_utc(self._clock)
        if group.expires_at <= now:
            raise QuotaExceeded("permit renewal expiry is not in the future")
        for member in members:
            budget = self.get_budget(member.budget_id)
            if budget is None or budget.status == "closed":
                raise QuotaExceeded("concurrency budget is unavailable")
            if budget.deadline_utc is not None and group.expires_at > budget.deadline_utc:
                raise QuotaExceeded("permit renewal exceeds budget deadline")
        return self._replace_permit_group(current, members, group)

    def release_permit_group(self, group: PermitGroupV1) -> PermitGroupV1:
        current, members = self._load_permit_transition(group)
        if current == group:
            return current
        if (
            current.status != "active"
            or group.status != "released"
            or group.revision != current.revision + 1
            or group.expires_at != current.expires_at
        ):
            raise Conflict("permit release compare-and-set did not match")
        return self._replace_permit_group(current, members, group)

    def expire_permit_group(self, group: PermitGroupV1) -> PermitGroupV1:
        current, members = self._load_permit_transition(group)
        if current == group:
            return current
        if (
            current.status != "active"
            or group.status != "expired"
            or group.revision != current.revision + 1
            or group.expires_at != current.expires_at
        ):
            raise Conflict("permit expiry compare-and-set did not match")
        if _now_utc(self._clock) < current.expires_at:
            raise QuotaExceeded("permit group is not yet expired")
        return self._replace_permit_group(current, members, group)

    def _validate_exact_snapshot(self, snapshot: BudgetSnapshotV1, budget: BudgetV1) -> None:
        if (
            snapshot.budget_id != budget.budget_id
            or snapshot.scope_kind != budget.scope_kind
            or snapshot.scope_id != budget.scope_id
            or snapshot.policy_version != budget.policy_version
            or snapshot.budget_revision_at_freeze != budget.revision
            or snapshot.limits != budget.limits
            or snapshot.reserved != budget.reserved
            or snapshot.consumed != budget.consumed
        ):
            raise Conflict(
                "budget snapshot does not match the current budget head",
                budget_id=budget.budget_id,
            )

    @staticmethod
    def _validate_retained_snapshot_identity(
        snapshot: BudgetSnapshotV1,
        budget: BudgetV1,
    ) -> None:
        if (
            snapshot.budget_id != budget.budget_id
            or snapshot.scope_kind != budget.scope_kind
            or snapshot.scope_id != budget.scope_id
            or snapshot.policy_version != budget.policy_version
            or snapshot.limits != budget.limits
            or budget.revision < snapshot.budget_revision_at_freeze
        ):
            raise IntegrityViolation(
                "current budget no longer descends from its frozen snapshot identity",
                budget_id=snapshot.budget_id,
            )

    @staticmethod
    def _validate_group_members(
        group: ReservationGroupV1,
        reservations: Sequence[BudgetReservationV1],
    ) -> None:
        members = tuple(sorted(reservations, key=lambda item: item.reservation_id))
        if tuple(item.reservation_id for item in members) != group.budget_reservation_ids:
            raise IntegrityViolation("reservation group member ids differ")
        if len({item.budget_id for item in members}) != len(members):
            raise IntegrityViolation("reservation group repeats a budget")
        for item in members:
            if item.reservation_group_id != group.reservation_group_id:
                raise IntegrityViolation("reservation member references another group")
            if item.status != group.status:
                raise IntegrityViolation("reservation member status differs from group")

    def _reservation_replay(
        self,
        requested: ReservationGroupV1,
        requested_members: Sequence[BudgetReservationV1],
    ) -> ReservationGroupV1 | None:
        aggregate = self.get_reservation_group_with_members(requested.reservation_group_id)
        if aggregate is None:
            return None
        current, current_members = aggregate
        if _without_mutable_reservation_fields(current) != _without_mutable_reservation_fields(
            requested
        ) or tuple(
            _without_mutable_reservation_member_fields(item) for item in current_members
        ) != tuple(
            _without_mutable_reservation_member_fields(item)
            for item in sorted(requested_members, key=lambda value: value.reservation_id)
        ):
            raise IntegrityViolation("reservation idempotency replay has different content")
        return current

    def _reject_idempotency_collision(self, group: ReservationGroupV1) -> None:
        collision = self._session.scalar(
            select(ReservationGroupRow).where(
                ReservationGroupRow.run_id == group.run_id,
                ReservationGroupRow.scope == group.scope,
                ReservationGroupRow.idempotency_key == group.idempotency_key,
            )
        )
        if collision is not None:
            raise IntegrityViolation("reservation idempotency key is already bound")

    @staticmethod
    def _validate_reservation_capacity(
        budget: BudgetV1,
        requested: Sequence[CostAmountV1],
    ) -> None:
        limits = _amount_map(budget.limits)
        reserved = _value_map(budget.reserved)
        consumed = _value_map(budget.consumed)
        for item in requested:
            limit = limits.get(item.dimension)
            if limit is None or not _same_amount_identity(limit, item):
                raise IntegrityViolation("reservation dimension differs from its budget limit")
            if item.dimension == "concurrent_run":
                raise IntegrityViolation("concurrent_run cannot enter a reservation")
            projected = _exact_decimal_add(
                _exact_decimal_add(
                    reserved.get(item.dimension, Decimal(0)),
                    consumed.get(item.dimension, Decimal(0)),
                ),
                item.value,
            )
            if projected > limit.value:
                raise QuotaExceeded(
                    "budget reservation exceeds a hard limit",
                    budget_id=budget.budget_id,
                    dimension=item.dimension,
                )

    def _validate_current_fence(self, group: ReservationGroupV1) -> None:
        now = _now_utc(self._clock)
        if group.expires_at is not None and now >= group.expires_at:
            raise QuotaExceeded("reservation deadline has expired")
        run = self._session.get(RunRow, group.run_id)
        attempt = self._session.get(RunAttemptRow, (group.run_id, group.attempt_no))
        lease = self._session.scalar(
            select(RunLeaseRow).where(
                RunLeaseRow.run_id == group.run_id,
                RunLeaseRow.status == "active",
            )
        )
        if (
            run is None
            or attempt is None
            or lease is None
            or run.status not in {"leased", "running"}
            or run.current_attempt_no != group.attempt_no
            or run.budget_set_snapshot_id != group.budget_set_snapshot_id
            or run.run_budget_hold_group_id != group.parent_hold_group_id
            or attempt.fencing_token != group.fencing_token
            or attempt.status not in {"leased", "running"}
            or lease.attempt_no != group.attempt_no
            or lease.fencing_token != group.fencing_token
            or _parse_utc(lease.expires_at) <= now
            or _parse_utc(run.overall_deadline_utc) <= now
            or (
                attempt.attempt_deadline_utc is not None
                and _parse_utc(attempt.attempt_deadline_utc) <= now
            )
        ):
            raise QuotaExceeded("reservation fencing or deadline is no longer current")

    def _hold_available(
        self,
        parent: ReservationGroupV1,
        parent_members: dict[str, BudgetReservationV1],
        *,
        balances: Mapping[str, _HoldBalance] | None = None,
    ) -> dict[str, dict[str, Decimal]]:
        retained = (
            self._load_hold_balances(parent, parent_members) if balances is None else dict(balances)
        )
        if set(retained) != set(parent_members):
            raise IntegrityViolation("Run hold balance set differs from parent members")
        return {
            budget_id: _available_values(parent_members[budget_id], retained[budget_id])
            for budget_id in sorted(parent_members)
        }

    def _transition_reservation(
        self,
        current: ReservationGroupV1,
        current_members: Sequence[BudgetReservationV1],
        status: str,
    ) -> ReservationGroupV1:
        group_transition, member_transitions = self._prepare_reservation_transition(
            current,
            current_members,
            status,
        )
        connection = self._session.connection()
        self._execute_group_transitions(
            connection,
            (self._group_transition_parameters(group_transition),),
        )
        self._execute_member_transitions(
            connection,
            tuple(self._member_transition_parameters(item) for item in member_transitions),
        )
        self._session.expire_all()
        return group_transition.updated

    def _load_usage_group(
        self,
        usage: UsageEntryV1,
    ) -> tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]]:
        aggregate = self.get_reservation_group_with_members(usage.reservation_group_id)
        group = None if aggregate is None else aggregate[0]
        if group is None or group.scope == "run_budget_hold":
            raise IntegrityViolation("usage references an unavailable child reservation")
        assert aggregate is not None  # narrowed by the authority check above
        members = aggregate[1]
        if (
            usage.budget_reservation_ids != group.budget_reservation_ids
            or usage.scope != group.scope
            or usage.run_id != group.run_id
            or usage.attempt_no != group.attempt_no
            or usage.request_hash != group.request_hash
            or usage.transport_attempt != group.transport_attempt
            or usage.fencing_token_at_reserve != group.fencing_token
        ):
            raise IntegrityViolation("usage identity differs from its reservation group")
        return group, members

    def _load_parent_balance_for_child(
        self,
        child: ReservationGroupV1,
        child_members: Sequence[BudgetReservationV1],
        *,
        allow_released: bool = False,
    ) -> tuple[
        ReservationGroupV1,
        dict[str, BudgetReservationV1],
        dict[str, _HoldBalance],
    ]:
        aggregate = self.get_reservation_group_with_members(child.parent_hold_group_id or "")
        parent = None if aggregate is None else aggregate[0]
        if (
            parent is None
            or parent.scope != "run_budget_hold"
            or parent.run_id != child.run_id
            or parent.budget_set_snapshot_id != child.budget_set_snapshot_id
            or parent.status not in ({"reserved", "released"} if allow_released else {"reserved"})
        ):
            raise IntegrityViolation("child settlement has no matching Run hold")
        assert aggregate is not None  # narrowed by the authority check above
        parent_members = {item.budget_id: item for item in aggregate[1]}
        selected_children = {item.budget_id: item for item in child_members}
        _require_complete_child_projection(
            parent_members=parent_members,
            child_members=selected_children,
        )
        balances = self._load_hold_balances(parent, parent_members)
        return parent, parent_members, balances

    def _settled_child_balance_updates(
        self,
        *,
        parent_members: Mapping[str, BudgetReservationV1],
        balances: Mapping[str, _HoldBalance],
        child_members: Sequence[BudgetReservationV1],
        usage: UsageEntryV1,
    ) -> dict[str, _HoldBalance]:
        actual = _amount_map(_usage_amounts(usage))
        updates = dict(balances)
        for child_member in child_members:
            allocated = _value_map(child_member.reserved)
            settled_delta: dict[str, Decimal] = {}
            for dimension in allocated:
                observed = actual.get(dimension)
                if observed is None:
                    raise IntegrityViolation("settlement usage omits a held dimension")
                identity = _amount_map(parent_members[child_member.budget_id].reserved)[dimension]
                if not _same_amount_identity(identity, observed):
                    raise IntegrityViolation("usage amount identity differs from Run hold")
                settled_delta[dimension] = min(allocated[dimension], observed.value)
            updates[child_member.budget_id] = self._updated_hold_balance(
                balances[child_member.budget_id],
                parent_members[child_member.budget_id],
                active_delta={
                    dimension: _exact_decimal_subtract(Decimal(0), value)
                    for dimension, value in allocated.items()
                },
                settled_delta=settled_delta,
                count_delta=-1,
            )
        return updates

    def _prepare_hold_accounting_transitions(
        self,
        *,
        parent_members: Mapping[str, BudgetReservationV1],
        before: Mapping[str, _HoldBalance],
        after: Mapping[str, _HoldBalance],
        budget_ids: Sequence[str],
        usage_before: UsageEntryV1 | None = None,
        usage_after: UsageEntryV1 | None = None,
    ) -> tuple[_RowTransition[BudgetV1], ...]:
        selected_budget_ids = tuple(dict.fromkeys(budget_ids))
        before_usage = {} if usage_before is None else _amount_map(_usage_amounts(usage_before))
        after_usage = {} if usage_after is None else _amount_map(_usage_amounts(usage_after))
        transitions: list[_RowTransition[BudgetV1]] = []
        for budget_id in sorted(selected_budget_ids):
            member = parent_members.get(budget_id)
            if member is None or budget_id not in before or budget_id not in after:
                raise IntegrityViolation("Run hold accounting budget authority differs")
            budget = self.get_budget(budget_id)
            if budget is None:
                raise IntegrityViolation("Run hold accounting budget disappeared")
            limits = _amount_map(budget.limits)
            reserved = _value_map(budget.reserved)
            consumed = _value_map(budget.consumed)
            before_contribution = _hold_contribution_values(member, before[budget_id])
            after_contribution = _hold_contribution_values(member, after[budget_id])
            changed = False
            for dimension in before_contribution:
                delta = _exact_decimal_subtract(
                    after_contribution[dimension],
                    before_contribution[dimension],
                )
                current = reserved.get(dimension, Decimal(0))
                next_reserved = _exact_decimal_add(current, delta)
                if next_reserved < 0:
                    raise IntegrityViolation(
                        "Run hold contribution exceeds shared budget reserve",
                        budget_id=budget_id,
                        dimension=dimension,
                    )
                if delta:
                    changed = True
                reserved[dimension] = next_reserved
            for dimension in set(before_usage) | set(after_usage):
                limit = limits.get(dimension)
                if limit is None:
                    continue
                previous = before_usage.get(dimension)
                current = after_usage.get(dimension)
                if previous is not None and not _same_amount_identity(limit, previous):
                    raise IntegrityViolation("prior usage amount identity differs from budget")
                if current is not None and not _same_amount_identity(limit, current):
                    raise IntegrityViolation("usage amount identity differs from budget limit")
                delta = _exact_decimal_subtract(
                    Decimal(0) if current is None else current.value,
                    Decimal(0) if previous is None else previous.value,
                )
                retained = consumed.get(dimension, Decimal(0))
                next_consumed = _exact_decimal_add(retained, delta)
                if next_consumed < 0:
                    raise IntegrityViolation("usage transition would make consumption negative")
                if delta:
                    changed = True
                consumed[dimension] = next_consumed
            if changed:
                transitions.append(
                    _RowTransition(
                        current=budget,
                        updated=self._with_budget_amounts(
                            budget,
                            reserved=reserved,
                            consumed=consumed,
                        ),
                    )
                )
        return tuple(transitions)

    def _prepare_cost_mutation(
        self,
        *,
        result_group: ReservationGroupV1,
        budget_transitions: Sequence[_RowTransition[BudgetV1]],
        group_transitions: Sequence[_RowTransition[ReservationGroupV1]],
        member_transitions: Sequence[_RowTransition[BudgetReservationV1]],
        hold_balance_transitions: Sequence[_RowTransition[_HoldBalance]],
    ) -> _PreparedCostMutation:
        return _PreparedCostMutation(
            result_group=result_group,
            budget_parameters=tuple(
                _freeze_parameter_mapping(self._budget_transition_parameters(item))
                for item in budget_transitions
            ),
            group_parameters=tuple(
                _freeze_parameter_mapping(self._group_transition_parameters(item))
                for item in group_transitions
            ),
            member_parameters=tuple(
                _freeze_parameter_mapping(self._member_transition_parameters(item))
                for item in member_transitions
            ),
            hold_balance_parameters=tuple(
                _freeze_parameter_mapping(self._hold_balance_transition_parameters(item))
                for item in hold_balance_transitions
            ),
        )

    def _apply_prepared_cost_mutation(self, prepared: _PreparedCostMutation) -> None:
        """Apply a fully validated cost mutation without reads or arithmetic."""

        connection = self._session.connection()
        self._execute_budget_transitions(connection, prepared.budget_parameters)
        self._execute_group_transitions(connection, prepared.group_parameters)
        self._execute_member_transitions(connection, prepared.member_parameters)
        self._execute_hold_balance_transitions(
            connection,
            prepared.hold_balance_parameters,
        )
        self._session.expire_all()

    def _prepare_usage_settlement_mutation(
        self,
        *,
        group: ReservationGroupV1,
        members: Sequence[BudgetReservationV1],
        group_status: _ReservationTerminal,
        parent: ReservationGroupV1,
        parent_members: Mapping[str, BudgetReservationV1],
        balances: Mapping[str, _HoldBalance],
        updated_balances: Mapping[str, _HoldBalance],
        usage_before: UsageEntryV1 | None = None,
        usage_after: UsageEntryV1,
    ) -> _PreparedCostMutation:
        budget_transitions = self._prepare_hold_accounting_transitions(
            parent_members=parent_members,
            before=balances,
            after=updated_balances,
            budget_ids=tuple(item.budget_id for item in members),
            usage_before=usage_before,
            usage_after=usage_after,
        )
        child_group_transition, child_member_transitions = self._prepare_reservation_transition(
            group, members, group_status
        )
        parent_group_transition, parent_member_transitions, balance_transitions = (
            self._prepare_parent_balance_transition(
                parent,
                parent_members,
                balances,
                updated_balances,
                status=parent.status,
            )
        )
        return self._prepare_cost_mutation(
            result_group=child_group_transition.updated,
            budget_transitions=budget_transitions,
            group_transitions=(child_group_transition, parent_group_transition),
            member_transitions=(*child_member_transitions, *parent_member_transitions),
            hold_balance_transitions=balance_transitions,
        )

    def _with_budget_amounts(
        self,
        budget: BudgetV1,
        *,
        reserved: dict[str, Decimal] | None = None,
        consumed: dict[str, Decimal] | None = None,
    ) -> BudgetV1:
        limits = _amount_map(budget.limits)
        next_reserved = reserved if reserved is not None else _value_map(budget.reserved)
        next_consumed = consumed if consumed is not None else _value_map(budget.consumed)
        exhausted = any(
            dimension != "concurrent_run"
            and next_consumed.get(dimension, Decimal(0)) >= limit.value
            for dimension, limit in limits.items()
        )
        status = budget.status
        if status != "closed":
            status = "exhausted" if exhausted else "active"
        return budget.model_copy(
            update={
                "reserved": _amounts_from_values(next_reserved, limits),
                "consumed": _amounts_from_values(next_consumed, limits),
                "status": status,
                "revision": budget.revision + 1,
            }
        )

    @staticmethod
    def _validate_late_identity(original: UsageEntryV1, adjustment: UsageEntryV1) -> None:
        fields = (
            "reservation_group_id",
            "budget_reservation_ids",
            "scope",
            "run_id",
            "attempt_no",
            "request_hash",
            "transport_attempt",
            "execution_source",
            "provider_prefix_cache",
            "retry_index",
            "routing_decision_kind",
            "routing_decision_id",
            "fencing_token_at_reserve",
        )
        if any(getattr(original, field) != getattr(adjustment, field) for field in fields):
            raise IntegrityViolation("late adjustment must inherit original execution identity")

    @staticmethod
    def _validate_permit_members(
        group: PermitGroupV1,
        permits: Sequence[ConcurrencyPermitV1],
    ) -> None:
        members = tuple(sorted(permits, key=lambda item: item.permit_id))
        if tuple(item.permit_id for item in members) != group.permit_ids:
            raise IntegrityViolation("permit group member ids differ")
        if len({item.budget_id for item in members}) != len(members):
            raise IntegrityViolation("permit group repeats a budget")
        for item in members:
            if (
                item.permit_group_id != group.permit_group_id
                or item.run_id != group.run_id
                or item.lease_id != group.lease_id
                or item.fencing_token != group.fencing_token
                or item.status != group.status
                or item.acquired_at != group.acquired_at
                or item.expires_at != group.expires_at
            ):
                raise IntegrityViolation("permit member differs from its group")

    def _permit_replay(
        self,
        requested: PermitGroupV1,
        requested_members: Sequence[ConcurrencyPermitV1],
    ) -> PermitGroupV1 | None:
        current = self.get_permit_group(requested.permit_group_id)
        if current is None:
            return None
        current_members = self.list_concurrency_permits(requested.permit_group_id)
        if _without_mutable_permit_fields(current) != _without_mutable_permit_fields(
            requested
        ) or tuple(
            _without_mutable_permit_member_fields(item) for item in current_members
        ) != tuple(
            _without_mutable_permit_member_fields(item)
            for item in sorted(requested_members, key=lambda value: value.permit_id)
        ):
            raise IntegrityViolation("permit replay has different lease identity")
        return current

    def _load_permit_transition(
        self,
        requested: PermitGroupV1,
    ) -> tuple[PermitGroupV1, tuple[ConcurrencyPermitV1, ...]]:
        current = self.get_permit_group(requested.permit_group_id)
        if current is None:
            raise IntegrityViolation("permit transition references an unavailable group")
        if _without_mutable_permit_fields(current) != _without_mutable_permit_fields(requested):
            raise IntegrityViolation("permit transition changed immutable lease identity")
        return current, self.list_concurrency_permits(current.permit_group_id)

    def _replace_permit_group(
        self,
        current: PermitGroupV1,
        current_members: Sequence[ConcurrencyPermitV1],
        requested: PermitGroupV1,
    ) -> PermitGroupV1:
        updated_members = tuple(
            item.model_copy(
                update={
                    "status": requested.status,
                    "revision": item.revision + 1,
                    "expires_at": requested.expires_at,
                }
            )
            for item in current_members
        )
        result = self._session.execute(
            update(PermitGroupRow)
            .where(
                PermitGroupRow.permit_group_id == current.permit_group_id,
                PermitGroupRow.revision == current.revision,
            )
            .values(
                status=requested.status,
                revision=requested.revision,
                expires_at=requested.model_dump(mode="json")["expires_at"],
                payload=requested.model_dump(mode="json"),
            )
        )
        if result.rowcount != 1:
            raise Conflict("permit group compare-and-set did not match")
        for old_member, new_member in zip(current_members, updated_members, strict=True):
            member_result = self._session.execute(
                update(ConcurrencyPermitRow)
                .where(
                    ConcurrencyPermitRow.permit_id == old_member.permit_id,
                    ConcurrencyPermitRow.revision == old_member.revision,
                )
                .values(
                    status=new_member.status,
                    revision=new_member.revision,
                    expires_at=new_member.model_dump(mode="json")["expires_at"],
                    payload=new_member.model_dump(mode="json"),
                )
            )
            if member_result.rowcount != 1:
                raise Conflict("concurrency permit compare-and-set did not match")
        self._session.expire_all()
        return requested

    def _usage_row_values(self, usage: UsageEntryV1) -> dict[str, object]:
        wire = usage.model_dump(mode="json")
        return {
            "usage_id": usage.usage_id,
            "usage_identity": self.usage_identity(usage),
            "reservation_group_id": usage.reservation_group_id,
            "scope": usage.scope,
            "run_id": usage.run_id,
            "attempt_no": usage.attempt_no,
            "request_hash": usage.request_hash,
            "transport_attempt": usage.transport_attempt,
            "execution_source": usage.execution_source,
            "retry_index": usage.retry_index,
            "routing_decision_kind": usage.routing_decision_kind,
            "routing_decision_id": usage.routing_decision_id,
            "native_routing_decision_id": (
                usage.routing_decision_id if usage.routing_decision_kind == "native" else None
            ),
            "legacy_routing_decision_id": (
                usage.routing_decision_id
                if usage.routing_decision_kind == "legacy_import"
                else None
            ),
            "adjustment_of_usage_id": usage.adjustment_of_usage_id,
            "fencing_token_at_reserve": usage.fencing_token_at_reserve,
            "recorded_at": wire["recorded_at"],
            "payload": wire,
        }

    @staticmethod
    def _budget_transition_parameters(
        transition: _RowTransition[BudgetV1],
    ) -> dict[str, object]:
        return {
            "expected_budget_id": transition.current.budget_id,
            "expected_budget_revision": transition.current.revision,
            "next_budget_status": transition.updated.status,
            "next_budget_revision": transition.updated.revision,
            "next_budget_payload": transition.updated.model_dump(mode="json"),
        }

    @staticmethod
    def _execute_budget_transitions(
        connection: object,
        parameters: tuple[dict[str, object], ...],
    ) -> None:
        if not parameters:
            return
        table = BudgetRow.__table__
        statement = (
            table.update()
            .where(
                table.c.budget_id == bindparam("expected_budget_id"),
                table.c.revision == bindparam("expected_budget_revision"),
            )
            .values(
                status=bindparam("next_budget_status"),
                revision=bindparam("next_budget_revision"),
                payload=bindparam("next_budget_payload"),
            )
        )
        result = connection.execute(  # type: ignore[attr-defined]
            statement,
            parameters,
        )
        if result.rowcount != len(parameters):
            raise Conflict("terminal budget compare-and-set did not match")

    @staticmethod
    def _hold_balance_transition_parameters(
        transition: _RowTransition[_HoldBalance],
    ) -> dict[str, object]:
        current = transition.current
        updated = transition.updated
        if (
            current.hold_group_id != updated.hold_group_id
            or current.budget_id != updated.budget_id
            or updated.revision <= current.revision
        ):
            raise IntegrityViolation("Run hold balance CAS changed immutable identity")
        wire = _hold_balance_wire(updated)
        return {
            "expected_hold_group_id": current.hold_group_id,
            "expected_hold_budget_id": current.budget_id,
            "expected_hold_revision": current.revision,
            "expected_hold_digest": _hold_balance_digest(current),
            "next_hold_status": updated.status,
            "next_hold_revision": updated.revision,
            "next_active_child_count": updated.active_child_count,
            "next_hold_digest": canonical_sha256(wire),
            "next_hold_payload": wire,
        }

    @staticmethod
    def _execute_hold_balance_transitions(
        connection: object,
        parameters: tuple[dict[str, object], ...],
    ) -> None:
        if not parameters:
            return
        table = RunHoldBalanceRow.__table__
        statement = (
            table.update()
            .where(
                table.c.hold_group_id == bindparam("expected_hold_group_id"),
                table.c.budget_id == bindparam("expected_hold_budget_id"),
                table.c.revision == bindparam("expected_hold_revision"),
                table.c.balance_digest == bindparam("expected_hold_digest"),
            )
            .values(
                status=bindparam("next_hold_status"),
                revision=bindparam("next_hold_revision"),
                active_child_count=bindparam("next_active_child_count"),
                balance_digest=bindparam("next_hold_digest"),
                payload=bindparam("next_hold_payload"),
            )
        )
        result = connection.execute(  # type: ignore[attr-defined]
            statement,
            parameters,
        )
        if result.rowcount != len(parameters):
            raise Conflict("Run hold balance compare-and-set did not match")

    @staticmethod
    def _group_transition_parameters(
        transition: _RowTransition[ReservationGroupV1],
    ) -> dict[str, object]:
        return {
            "expected_group_id": transition.current.reservation_group_id,
            "expected_group_revision": transition.current.revision,
            "next_group_status": transition.updated.status,
            "next_group_revision": transition.updated.revision,
            "next_group_payload": transition.updated.model_dump(mode="json"),
        }

    @staticmethod
    def _execute_group_transitions(
        connection: object,
        parameters: tuple[dict[str, object], ...],
    ) -> None:
        if not parameters:
            return
        table = ReservationGroupRow.__table__
        statement = (
            table.update()
            .where(
                table.c.reservation_group_id == bindparam("expected_group_id"),
                table.c.revision == bindparam("expected_group_revision"),
            )
            .values(
                status=bindparam("next_group_status"),
                revision=bindparam("next_group_revision"),
                payload=bindparam("next_group_payload"),
            )
        )
        result = connection.execute(  # type: ignore[attr-defined]
            statement,
            parameters,
        )
        if result.rowcount != len(parameters):
            raise Conflict("terminal reservation compare-and-set did not match")

    @staticmethod
    def _member_transition_parameters(
        transition: _RowTransition[BudgetReservationV1],
    ) -> dict[str, object]:
        return {
            "expected_reservation_id": transition.current.reservation_id,
            "expected_reservation_revision": transition.current.revision,
            "next_reservation_status": transition.updated.status,
            "next_reservation_revision": transition.updated.revision,
            "next_reservation_payload": transition.updated.model_dump(mode="json"),
        }

    @staticmethod
    def _execute_member_transitions(
        connection: object,
        parameters: tuple[dict[str, object], ...],
    ) -> None:
        if not parameters:
            return
        table = BudgetReservationRow.__table__
        statement = (
            table.update()
            .where(
                table.c.reservation_id == bindparam("expected_reservation_id"),
                table.c.revision == bindparam("expected_reservation_revision"),
            )
            .values(
                status=bindparam("next_reservation_status"),
                revision=bindparam("next_reservation_revision"),
                payload=bindparam("next_reservation_payload"),
            )
        )
        result = connection.execute(  # type: ignore[attr-defined]
            statement,
            parameters,
        )
        if result.rowcount != len(parameters):
            raise Conflict("terminal reservation member compare-and-set did not match")

    @staticmethod
    def _permit_group_transition_parameters(
        transition: _RowTransition[PermitGroupV1],
    ) -> dict[str, object]:
        wire = transition.updated.model_dump(mode="json")
        return {
            "expected_permit_group_id": transition.current.permit_group_id,
            "expected_permit_group_revision": transition.current.revision,
            "next_permit_group_status": transition.updated.status,
            "next_permit_group_revision": transition.updated.revision,
            "next_permit_group_expires_at": wire["expires_at"],
            "next_permit_group_payload": wire,
        }

    @staticmethod
    def _execute_permit_group_transition(
        connection: object,
        parameters: dict[str, object],
    ) -> None:
        table = PermitGroupRow.__table__
        statement = (
            table.update()
            .where(
                table.c.permit_group_id == bindparam("expected_permit_group_id"),
                table.c.revision == bindparam("expected_permit_group_revision"),
            )
            .values(
                status=bindparam("next_permit_group_status"),
                revision=bindparam("next_permit_group_revision"),
                expires_at=bindparam("next_permit_group_expires_at"),
                payload=bindparam("next_permit_group_payload"),
            )
        )
        result = connection.execute(  # type: ignore[attr-defined]
            statement,
            parameters,
        )
        if result.rowcount != 1:
            raise Conflict("terminal permit group compare-and-set did not match")

    @staticmethod
    def _permit_member_transition_parameters(
        transition: _RowTransition[ConcurrencyPermitV1],
    ) -> dict[str, object]:
        wire = transition.updated.model_dump(mode="json")
        return {
            "expected_permit_id": transition.current.permit_id,
            "expected_permit_revision": transition.current.revision,
            "next_permit_status": transition.updated.status,
            "next_permit_revision": transition.updated.revision,
            "next_permit_expires_at": wire["expires_at"],
            "next_permit_payload": wire,
        }

    @staticmethod
    def _execute_permit_member_transitions(
        connection: object,
        parameters: tuple[dict[str, object], ...],
    ) -> None:
        if not parameters:
            return
        table = ConcurrencyPermitRow.__table__
        statement = (
            table.update()
            .where(
                table.c.permit_id == bindparam("expected_permit_id"),
                table.c.revision == bindparam("expected_permit_revision"),
            )
            .values(
                status=bindparam("next_permit_status"),
                revision=bindparam("next_permit_revision"),
                expires_at=bindparam("next_permit_expires_at"),
                payload=bindparam("next_permit_payload"),
            )
        )
        result = connection.execute(  # type: ignore[attr-defined]
            statement,
            parameters,
        )
        if result.rowcount != len(parameters):
            raise Conflict("terminal permit member compare-and-set did not match")


__all__ = ["PreflightedTerminalCostClosure", "SqlCostLedger"]
