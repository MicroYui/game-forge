"""Authoritative SQLite CostLedger with all-scope atomic accounting."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

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
from gameforge.contracts.storage import UtcClock
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.persistence.cost import SqlCostRepository
from gameforge.runtime.persistence.models import (
    BudgetReservationRow,
    BudgetRow,
    ConcurrencyPermitRow,
    PermitGroupRow,
    ReservationGroupRow,
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
    result: list[CostAmountV1] = []
    for dimension, value in values.items():
        if value < 0:
            raise IntegrityViolation(
                "CostLedger amount became negative",
                dimension=dimension,
            )
        if value == 0:
            continue
        identity = identities.get(dimension)
        if identity is None:
            raise IntegrityViolation(
                "CostLedger amount has no budget limit identity",
                dimension=dimension,
            )
        result.append(identity.model_copy(update={"value": value}))
    return tuple(result)


def _same_amount_identity(left: CostAmountV1, right: CostAmountV1) -> bool:
    return (
        left.dimension == right.dimension
        and left.unit == right.unit
        and left.currency == right.currency
    )


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

    def freeze_budget_set(
        self,
        budget_set: BudgetSetSnapshotV1,
        hold_group: ReservationGroupV1,
        reservations: Sequence[BudgetReservationV1],
    ) -> ReservationGroupV1:
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
            return replay

        members = {item.budget_id: item for item in reservations}
        if set(members) != {item.budget_id for item in budget_set.snapshots}:
            raise IntegrityViolation("run hold must reserve every budget-set member")
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
            member = members[budget.budget_id]
            self._validate_reservation_capacity(budget, member.reserved)
            current.append((snapshot, budget, member))

        self.put_budget_set(budget_set)
        self.put_reservation_group(hold_group, reservations)
        for _, budget, member in current:
            reserved = _value_map(budget.reserved)
            for requested in member.reserved:
                reserved[requested.dimension] = reserved.get(requested.dimension, Decimal(0)) + (
                    requested.value
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

        parent = self.get_reservation_group(group.parent_hold_group_id or "")
        if (
            parent is None
            or parent.scope != "run_budget_hold"
            or parent.status != "reserved"
            or parent.run_id != group.run_id
            or parent.budget_set_snapshot_id != group.budget_set_snapshot_id
        ):
            raise IntegrityViolation("child reservation has no active matching run hold")
        parent_members = {
            item.budget_id: item
            for item in self.list_budget_reservations(parent.reservation_group_id)
        }
        requested_members = {item.budget_id: item for item in reservations}
        if set(parent_members) != set(requested_members):
            raise IntegrityViolation("child reservation must include every parent budget member")
        now = _now_utc(self._clock)
        for budget_id in sorted(parent_members):
            budget = self.get_budget(budget_id)
            if budget is None or budget.status != "active":
                raise QuotaExceeded("parent hold budget is unavailable", budget_id=budget_id)
            if budget.deadline_utc is not None and now >= budget.deadline_utc:
                raise QuotaExceeded("parent hold budget deadline has expired", budget_id=budget_id)
        available = self._hold_available(parent, parent_members)
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

        self.put_reservation_group(group, reservations)
        self._bump_parent(parent)
        return group

    def retry_budget_available(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        hold_group_id: str,
    ) -> bool:
        hold = self.get_reservation_group(hold_group_id)
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
        members = {
            item.budget_id: item
            for item in self.list_budget_reservations(hold.reservation_group_id)
        }
        if set(members) != {item.budget_id for item in budget_set.snapshots}:
            raise IntegrityViolation("retry budget hold members differ from budget set")
        now = _now_utc(self._clock)
        for budget_id in sorted(members):
            budget = self.get_budget(budget_id)
            if budget is None or budget.status != "active":
                return False
            if budget.deadline_utc is not None and now >= budget.deadline_utc:
                return False
        available = self._hold_available(hold, members)
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

        if not _usage_is_known_for(usage, members):
            transitioned = self._transition_reservation(group, members, "held_unknown")
            self._bump_parent_for_child(group)
            return transitioned

        self.put_usage(usage)
        self._apply_initial_settlement(members, usage)
        transitioned = self._transition_reservation(group, members, "reconciled")
        self._bump_parent_for_child(group)
        return transitioned

    def hold_unknown_group(self, reservation_group_id: str) -> ReservationGroupV1:
        group = self.get_reservation_group(reservation_group_id)
        if group is None or group.scope == "run_budget_hold":
            raise IntegrityViolation("held_unknown requires a retained child reservation")
        members = self.list_budget_reservations(group.reservation_group_id)
        if group.status == "held_unknown":
            return group
        if group.status != "reserved":
            raise InvalidStateTransition("reservation cannot enter held_unknown from this state")
        transitioned = self._transition_reservation(group, members, "held_unknown")
        self._bump_parent_for_child(group)
        return transitioned

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

        self.put_usage(conservative_usage)
        self._apply_initial_settlement(members, conservative_usage)
        transitioned = self._transition_reservation(
            group,
            members,
            "conservatively_settled",
        )
        self._bump_parent_for_child(group)
        return transitioned

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

        self.put_usage(adjustment)
        self._apply_late_adjustment(members, conservative, adjustment)
        transitioned = self._transition_reservation(group, members, "late_reconciled")
        self._bump_parent_for_child(group)
        return transitioned

    def close_hold_group(self, reservation_group_id: str) -> ReservationGroupV1:
        group = self.get_reservation_group(reservation_group_id)
        if group is None or group.scope != "run_budget_hold":
            raise IntegrityViolation("close_hold_group requires a retained run hold")
        members = self.list_budget_reservations(reservation_group_id)
        if group.status == "released":
            return group
        if group.status != "reserved":
            raise InvalidStateTransition("run hold cannot close from its current state")
        children = self._child_groups(group.reservation_group_id)
        if any(item.status in {"reserved", "held_unknown"} for item in children):
            raise InvalidStateTransition("run hold has an active child reservation")

        parent_members = {item.budget_id: item for item in members}
        remaining = self._hold_available(group, parent_members)
        for budget_id in sorted(parent_members):
            budget = self.get_budget(budget_id)
            if budget is None:
                raise IntegrityViolation("run hold budget disappeared")
            reserved = _value_map(budget.reserved)
            for dimension, value in remaining[budget_id].items():
                current = reserved.get(dimension, Decimal(0))
                if value > current:
                    raise IntegrityViolation("run hold remaining balance exceeds budget reserve")
                reserved[dimension] = current - value
            updated = self._with_budget_amounts(budget, reserved=reserved)
            self.replace_budget(budget, updated)
        return self._transition_reservation(group, members, "released")

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
        current = self.get_reservation_group(requested.reservation_group_id)
        if current is None:
            return None
        current_members = self.list_budget_reservations(requested.reservation_group_id)
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
            projected = (
                reserved.get(item.dimension, Decimal(0))
                + consumed.get(item.dimension, Decimal(0))
                + item.value
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

    def _child_groups(self, parent_hold_group_id: str) -> tuple[ReservationGroupV1, ...]:
        rows = self._session.scalars(
            select(ReservationGroupRow)
            .where(ReservationGroupRow.parent_hold_group_id == parent_hold_group_id)
            .order_by(ReservationGroupRow.reservation_group_id)
        ).all()
        result: list[ReservationGroupV1] = []
        for row in rows:
            group = self.get_reservation_group(row.reservation_group_id)
            if group is None:
                raise IntegrityViolation("child reservation row disappeared")
            result.append(group)
        return tuple(result)

    def _hold_available(
        self,
        parent: ReservationGroupV1,
        parent_members: dict[str, BudgetReservationV1],
    ) -> dict[str, dict[str, Decimal]]:
        available = {
            budget_id: _value_map(member.reserved) for budget_id, member in parent_members.items()
        }
        for child in self._child_groups(parent.reservation_group_id):
            child_members = {
                item.budget_id: item
                for item in self.list_budget_reservations(child.reservation_group_id)
            }
            if set(child_members) != set(parent_members):
                raise IntegrityViolation("child reservation members differ from parent hold")
            for budget_id, child_member in child_members.items():
                impact = self._child_impact(child, child_member)
                for dimension, value in impact.items():
                    current = available[budget_id].get(dimension, Decimal(0))
                    if value > current:
                        raise IntegrityViolation("child reservations exceed parent hold authority")
                    available[budget_id][dimension] = current - value
        return available

    def _child_impact(
        self,
        group: ReservationGroupV1,
        member: BudgetReservationV1,
    ) -> dict[str, Decimal]:
        allocated = _value_map(member.reserved)
        if group.status in {"reserved", "held_unknown"}:
            return allocated
        if group.status == "released":
            return {dimension: Decimal(0) for dimension in allocated}
        usages = self._group_usage(group.reservation_group_id)
        originals = tuple(item for item in usages if item.adjustment_of_usage_id is None)
        adjustments = tuple(item for item in usages if item.adjustment_of_usage_id is not None)
        if len(originals) != 1:
            raise IntegrityViolation("settled reservation has no unique original usage")
        original = _value_map(_usage_amounts(originals[0]))
        final = original
        if group.status == "late_reconciled":
            if len(adjustments) != 1:
                raise IntegrityViolation("late settlement has no unique adjustment")
            adjusted = _value_map(_usage_amounts(adjustments[0]))
            final = {dimension: adjusted.get(dimension, Decimal(0)) for dimension in allocated}
        elif adjustments:
            raise IntegrityViolation("non-late settlement contains an adjustment")
        return {
            dimension: min(value, final.get(dimension, Decimal(0)))
            for dimension, value in allocated.items()
        }

    def _group_usage(self, reservation_group_id: str) -> tuple[UsageEntryV1, ...]:
        rows = self._session.scalars(
            select(UsageEntryRow)
            .where(UsageEntryRow.reservation_group_id == reservation_group_id)
            .order_by(UsageEntryRow.recorded_at, UsageEntryRow.usage_id)
        ).all()
        return tuple(self._parse_usage_row(row) for row in rows)

    def _bump_parent(self, parent: ReservationGroupV1) -> ReservationGroupV1:
        members = self.list_budget_reservations(parent.reservation_group_id)
        return self._transition_reservation(parent, members, "reserved")

    def _bump_parent_for_child(self, child: ReservationGroupV1) -> None:
        parent = self.get_reservation_group(child.parent_hold_group_id or "")
        if parent is None:
            raise IntegrityViolation("child settlement has no open parent hold")
        if parent.status == "released" and child.status == "conservatively_settled":
            return
        if parent.status != "reserved":
            raise IntegrityViolation("child settlement has no open parent hold")
        self._bump_parent(parent)

    def _transition_reservation(
        self,
        current: ReservationGroupV1,
        current_members: Sequence[BudgetReservationV1],
        status: str,
    ) -> ReservationGroupV1:
        updated = current.model_copy(update={"status": status, "revision": current.revision + 1})
        updated_members = tuple(
            item.model_copy(update={"status": status, "revision": item.revision + 1})
            for item in current_members
        )
        wire = updated.model_dump(mode="json")
        result = self._session.execute(
            update(ReservationGroupRow)
            .where(
                ReservationGroupRow.reservation_group_id == current.reservation_group_id,
                ReservationGroupRow.revision == current.revision,
            )
            .values(status=status, revision=updated.revision, payload=wire)
        )
        if result.rowcount != 1:
            raise Conflict("reservation group compare-and-set did not match")
        for old_member, new_member in zip(current_members, updated_members, strict=True):
            member_result = self._session.execute(
                update(BudgetReservationRow)
                .where(
                    BudgetReservationRow.reservation_id == old_member.reservation_id,
                    BudgetReservationRow.revision == old_member.revision,
                )
                .values(
                    status=status,
                    revision=new_member.revision,
                    payload=new_member.model_dump(mode="json"),
                )
            )
            if member_result.rowcount != 1:
                raise Conflict("budget reservation compare-and-set did not match")
        self._session.expire_all()
        return updated

    def _load_usage_group(
        self,
        usage: UsageEntryV1,
    ) -> tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]]:
        group = self.get_reservation_group(usage.reservation_group_id)
        if group is None or group.scope == "run_budget_hold":
            raise IntegrityViolation("usage references an unavailable child reservation")
        members = self.list_budget_reservations(group.reservation_group_id)
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

    def _apply_initial_settlement(
        self,
        members: Sequence[BudgetReservationV1],
        usage: UsageEntryV1,
    ) -> None:
        actual = _amount_map(_usage_amounts(usage))
        for member in sorted(members, key=lambda item: item.budget_id):
            budget = self.get_budget(member.budget_id)
            if budget is None:
                raise IntegrityViolation("settlement budget disappeared")
            limits = _amount_map(budget.limits)
            allocated = _value_map(member.reserved)
            reserved = _value_map(budget.reserved)
            consumed = _value_map(budget.consumed)
            for dimension, observed in actual.items():
                limit = limits.get(dimension)
                if limit is None:
                    continue
                if not _same_amount_identity(limit, observed):
                    raise IntegrityViolation("usage amount identity differs from budget limit")
                released = min(allocated.get(dimension, Decimal(0)), observed.value)
                current_reserved = reserved.get(dimension, Decimal(0))
                if released > current_reserved:
                    raise IntegrityViolation("settlement releases more than budget reserved")
                reserved[dimension] = current_reserved - released
                consumed[dimension] = consumed.get(dimension, Decimal(0)) + observed.value
            updated = self._with_budget_amounts(
                budget,
                reserved=reserved,
                consumed=consumed,
            )
            self.replace_budget(budget, updated)

    def _apply_late_adjustment(
        self,
        members: Sequence[BudgetReservationV1],
        conservative: UsageEntryV1,
        actual: UsageEntryV1,
    ) -> None:
        conservative_amounts = _amount_map(_usage_amounts(conservative))
        actual_amounts = _amount_map(_usage_amounts(actual))
        group = self.get_reservation_group(actual.reservation_group_id)
        if group is None:
            raise IntegrityViolation("late settlement reservation disappeared")
        parent = self.get_reservation_group(group.parent_hold_group_id or "")
        restore_to_open_hold = parent is not None and parent.status == "reserved"
        for member in sorted(members, key=lambda item: item.budget_id):
            budget = self.get_budget(member.budget_id)
            if budget is None:
                raise IntegrityViolation("late settlement budget disappeared")
            limits = _amount_map(budget.limits)
            allocated = _value_map(member.reserved)
            reserved = _value_map(budget.reserved)
            consumed = _value_map(budget.consumed)
            dimensions = set(conservative_amounts) | set(actual_amounts)
            for dimension in dimensions:
                limit = limits.get(dimension)
                if limit is None:
                    continue
                before = conservative_amounts.get(dimension)
                after = actual_amounts.get(dimension)
                if before is not None and not _same_amount_identity(limit, before):
                    raise IntegrityViolation("conservative amount identity differs from budget")
                if after is not None and not _same_amount_identity(limit, after):
                    raise IntegrityViolation("late amount identity differs from budget")
                before_value = Decimal(0) if before is None else before.value
                after_value = Decimal(0) if after is None else after.value
                delta = after_value - before_value
                current_consumed = consumed.get(dimension, Decimal(0))
                if current_consumed + delta < 0:
                    raise IntegrityViolation("late adjustment would make usage negative")
                consumed[dimension] = current_consumed + delta
                before_from_hold = min(allocated.get(dimension, Decimal(0)), before_value)
                after_from_hold = min(allocated.get(dimension, Decimal(0)), after_value)
                reserved_delta = before_from_hold - after_from_hold
                if restore_to_open_hold:
                    current_reserved = reserved.get(dimension, Decimal(0))
                    if reserved_delta < 0 and -reserved_delta > current_reserved:
                        raise IntegrityViolation("late adjustment releases more than reserved")
                    reserved[dimension] = current_reserved + reserved_delta
            updated = self._with_budget_amounts(
                budget,
                reserved=reserved,
                consumed=consumed,
            )
            self.replace_budget(budget, updated)

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


__all__ = ["SqlCostLedger"]
