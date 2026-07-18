"""persist current Run-hold balances independently of reservation history

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-18
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence, Union

import sqlalchemy as sa
from alembic import op
from pydantic import BaseModel, ValidationError

from gameforge.contracts.canonical import typed_canonical_json
from gameforge.contracts.cost import (
    BudgetReservationV1,
    BudgetSetSnapshotV1,
    BudgetSnapshotV1,
    BudgetV1,
    ReservationGroupV1,
    UsageEntryV1,
)


revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SCHEMA_VERSION = "run-hold-balance@1"
_MAX_EXACT_COST_DECIMAL_DIGITS = 4096


def _canonical_projection(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _canonical_projection(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_canonical_projection(item) for item in value]
    return value


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        _canonical_projection(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _payload(value: object, *, label: str) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"0012 cannot decode {label} payload") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"0012 found a non-object {label} payload")
    return value


def _model_payload(
    value: object,
    model_type: type[BaseModel],
    *,
    label: str,
) -> BaseModel:
    raw = _payload(value, label=label)
    try:
        parsed = model_type.model_validate(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RuntimeError(f"0012 found an invalid {label} payload") from exc
    if typed_canonical_json(parsed.model_dump(mode="json")) != typed_canonical_json(raw):
        raise RuntimeError(f"0012 found a noncanonical {label} payload")
    return parsed


def _require_projection(
    row: object,
    model: BaseModel,
    fields: Sequence[str],
    *,
    label: str,
) -> None:
    wire = model.model_dump(mode="json")
    if any(getattr(row, field) != wire[field] for field in fields):
        raise RuntimeError(f"0012 found a {label} payload/projection mismatch")


def _amounts(value: object, *, label: str) -> dict[str, tuple[dict[str, object], Decimal]]:
    if not isinstance(value, list):
        raise RuntimeError(f"0012 found a non-array {label} amount vector")
    result: dict[str, tuple[dict[str, object], Decimal]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise RuntimeError(f"0012 found a non-object {label} amount")
        try:
            dimension = raw["dimension"]
            amount_value = Decimal(str(raw["value"]))
            unit = raw["unit"]
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise RuntimeError(f"0012 found an invalid {label} amount") from exc
        if (
            not isinstance(dimension, str)
            or not dimension
            or not isinstance(unit, str)
            or not unit
            or amount_value < 0
            or dimension in result
        ):
            raise RuntimeError(f"0012 found an invalid {label} amount identity")
        result[dimension] = (dict(raw), amount_value)
    return result


def _usage_values(
    payload: dict[str, object],
) -> dict[str, tuple[str, str | None, Decimal]]:
    result: dict[str, tuple[str, str | None, Decimal]] = {}
    token_usage = payload.get("token_usage")
    if not isinstance(token_usage, dict):
        raise RuntimeError("0012 found usage without token observation")
    if token_usage.get("status") == "reported":
        for dimension, field in (
            ("input_token", "input_tokens"),
            ("output_token", "output_tokens"),
            ("cache_read_token", "cache_read_tokens"),
            ("cache_write_token", "cache_write_tokens"),
        ):
            raw = token_usage.get(field)
            if raw is not None:
                try:
                    result[dimension] = ("token", None, Decimal(str(raw)))
                except InvalidOperation as exc:
                    raise RuntimeError("0012 found invalid token usage") from exc
    scope = payload.get("scope")
    if scope == "attempt_call":
        result["request"] = ("request", None, Decimal(1))
    elif scope == "agent_step":
        result["agent_step"] = ("step", None, Decimal(1))
    else:
        raise RuntimeError("0012 found usage with an unknown reservation scope")
    try:
        result["wall_time_ns"] = ("ns", None, Decimal(str(payload["wall_time_ns"])))
    except (KeyError, InvalidOperation) as exc:
        raise RuntimeError("0012 found invalid wall-time usage") from exc
    monetary = payload.get("monetary")
    if not isinstance(monetary, dict):
        raise RuntimeError("0012 found usage without monetary observation")
    if monetary.get("status") == "reported":
        raw = monetary.get("amount")
        currency = monetary.get("currency")
        if not isinstance(currency, str) or not currency:
            raise RuntimeError("0012 found monetary usage without a currency")
        try:
            result["monetary"] = (
                "currency",
                currency,
                Decimal(0 if raw is None else str(raw)),
            )
        except InvalidOperation as exc:
            raise RuntimeError("0012 found invalid monetary usage") from exc
    if any(value < 0 for _unit, _currency, value in result.values()):
        raise RuntimeError("0012 found negative usage")
    return result


def _canonical_balance_decimal(value: Decimal) -> Decimal:
    """Freeze run-hold-balance@1 decimal spelling for historical replay."""
    if not value.is_finite():
        raise RuntimeError("0012 found a non-finite balance amount")
    if value.is_zero():
        return Decimal(0)
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise RuntimeError("0012 found a non-finite balance amount")
    canonical_digits = list(digits)
    while exponent < 0 and canonical_digits[-1] == 0:
        canonical_digits.pop()
        exponent += 1
    return Decimal((sign, tuple(canonical_digits), exponent))


def _decimal_coefficient_and_exponent(value: Decimal) -> tuple[int, int]:
    if not value.is_finite():
        raise RuntimeError("0012 exact arithmetic requires finite balance amounts")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise RuntimeError("0012 exact arithmetic requires finite balance amounts")
    if len(digits) > _MAX_EXACT_COST_DECIMAL_DIGITS:
        raise RuntimeError("0012 exact arithmetic exceeds its 4096-decimal-digit operational bound")
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    return (-coefficient if sign else coefficient), exponent


def _decimal_from_coefficient(coefficient: int, exponent: int) -> Decimal:
    if coefficient == 0:
        return Decimal((0, (0,), exponent))
    digits = Decimal(abs(coefficient)).as_tuple().digits
    if len(digits) > _MAX_EXACT_COST_DECIMAL_DIGITS:
        raise RuntimeError("0012 exact arithmetic exceeds its 4096-decimal-digit operational bound")
    return Decimal((1 if coefficient < 0 else 0, digits, exponent))


def _exact_decimal_common_exponent(left: Decimal, right: Decimal) -> int:
    tuples = (left.as_tuple(), right.as_tuple())
    if (
        not left.is_finite()
        or not right.is_finite()
        or any(not isinstance(value.exponent, int) for value in tuples)
    ):
        raise RuntimeError("0012 exact arithmetic requires finite balance amounts")
    if any(len(value.digits) > _MAX_EXACT_COST_DECIMAL_DIGITS for value in tuples):
        raise RuntimeError("0012 exact arithmetic exceeds its 4096-decimal-digit operational bound")
    exponents = tuple(int(value.exponent) for value in tuples)
    common_exponent = min(exponents)
    aligned_spans = tuple(
        len(value.digits) + exponent - common_exponent
        for value, exponent in zip(tuples, exponents, strict=True)
        if any(value.digits)
    )
    if aligned_spans and max(aligned_spans) > _MAX_EXACT_COST_DECIMAL_DIGITS:
        raise RuntimeError("0012 exact arithmetic exceeds its 4096-decimal-digit operational bound")
    return common_exponent


def _exact_decimal_add(left: Decimal, right: Decimal) -> Decimal:
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


def _amount_wire(identity: dict[str, object], value: Decimal) -> dict[str, object]:
    wire = dict(identity)
    wire["value"] = str(_canonical_balance_decimal(value))
    return wire


def _reconstruct(connection: object) -> list[dict[str, object]]:
    groups_table = sa.table(
        "reservation_groups",
        sa.column("reservation_group_id", sa.String()),
        sa.column("scope", sa.String()),
        sa.column("run_id", sa.String()),
        sa.column("budget_set_snapshot_id", sa.String()),
        sa.column("parent_hold_group_id", sa.String()),
        sa.column("attempt_no", sa.Integer()),
        sa.column("request_hash", sa.String()),
        sa.column("transport_attempt", sa.Integer()),
        sa.column("fencing_token", sa.BigInteger()),
        sa.column("idempotency_key", sa.String()),
        sa.column("status", sa.String()),
        sa.column("revision", sa.Integer()),
        sa.column("created_at", sa.String()),
        sa.column("expires_at", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    members_table = sa.table(
        "budget_reservations",
        sa.column("reservation_id", sa.String()),
        sa.column("reservation_group_id", sa.String()),
        sa.column("budget_id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("revision", sa.Integer()),
        sa.column("payload", sa.JSON()),
    )
    usage_table = sa.table(
        "usage_entries",
        sa.column("usage_id", sa.String()),
        sa.column("usage_identity", sa.String()),
        sa.column("reservation_group_id", sa.String()),
        sa.column("scope", sa.String()),
        sa.column("run_id", sa.String()),
        sa.column("attempt_no", sa.Integer()),
        sa.column("request_hash", sa.String()),
        sa.column("transport_attempt", sa.Integer()),
        sa.column("execution_source", sa.String()),
        sa.column("retry_index", sa.Integer()),
        sa.column("routing_decision_kind", sa.String()),
        sa.column("routing_decision_id", sa.String()),
        sa.column("native_routing_decision_id", sa.String()),
        sa.column("legacy_routing_decision_id", sa.String()),
        sa.column("adjustment_of_usage_id", sa.String()),
        sa.column("fencing_token_at_reserve", sa.BigInteger()),
        sa.column("recorded_at", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    budgets_table = sa.table(
        "budgets",
        sa.column("budget_id", sa.String()),
        sa.column("scope_kind", sa.String()),
        sa.column("scope_id", sa.String()),
        sa.column("policy_version", sa.String()),
        sa.column("status", sa.String()),
        sa.column("revision", sa.Integer()),
        sa.column("deadline_utc", sa.String()),
        sa.column("created_at", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    budget_sets_table = sa.table(
        "budget_set_snapshots",
        sa.column("budget_set_snapshot_id", sa.String()),
        sa.column("run_id", sa.String()),
        sa.column("selection_policy_version", sa.String()),
        sa.column("captured_at", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    budget_snapshots_table = sa.table(
        "budget_snapshots",
        sa.column("snapshot_id", sa.String()),
        sa.column("budget_set_snapshot_id", sa.String()),
        sa.column("ordinal", sa.Integer()),
        sa.column("budget_id", sa.String()),
        sa.column("scope_kind", sa.String()),
        sa.column("scope_id", sa.String()),
        sa.column("budget_revision_at_freeze", sa.Integer()),
        sa.column("payload", sa.JSON()),
    )
    budget_models: dict[str, BudgetV1] = {}
    for row in connection.execute(sa.select(budgets_table)).all():  # type: ignore[attr-defined]
        budget_id = str(row.budget_id)
        parsed_budget = _model_payload(row.payload, BudgetV1, label="budget")
        if not isinstance(parsed_budget, BudgetV1):  # pragma: no cover - defensive
            raise RuntimeError("0012 parsed an unexpected budget type")
        _require_projection(
            row,
            parsed_budget,
            (
                "budget_id",
                "scope_kind",
                "scope_id",
                "policy_version",
                "status",
                "revision",
                "deadline_utc",
                "created_at",
            ),
            label="budget",
        )
        if parsed_budget.budget_id != budget_id or budget_id in budget_models:
            raise RuntimeError("0012 found a budget identity mismatch")
        budget_models[budget_id] = parsed_budget
    budget_set_models: dict[str, BudgetSetSnapshotV1] = {}
    for row in connection.execute(sa.select(budget_sets_table)).all():  # type: ignore[attr-defined]
        snapshot_id = str(row.budget_set_snapshot_id)
        parsed_set = _model_payload(
            row.payload,
            BudgetSetSnapshotV1,
            label="budget-set snapshot",
        )
        if not isinstance(parsed_set, BudgetSetSnapshotV1):  # pragma: no cover - defensive
            raise RuntimeError("0012 parsed an unexpected budget-set snapshot type")
        _require_projection(
            row,
            parsed_set,
            (
                "budget_set_snapshot_id",
                "run_id",
                "selection_policy_version",
                "captured_at",
            ),
            label="budget-set snapshot",
        )
        if parsed_set.budget_set_snapshot_id != snapshot_id or snapshot_id in budget_set_models:
            raise RuntimeError("0012 found a budget-set snapshot identity mismatch")
        for snapshot in parsed_set.snapshots:
            budget = budget_models.get(snapshot.budget_id)
            if (
                budget is None
                or snapshot.scope_kind != budget.scope_kind
                or snapshot.scope_id != budget.scope_id
                or snapshot.policy_version != budget.policy_version
                or snapshot.limits != budget.limits
                or snapshot.budget_revision_at_freeze > budget.revision
            ):
                raise RuntimeError("0012 budget-set snapshot differs from retained budget identity")
        budget_set_models[snapshot_id] = parsed_set
    snapshots_by_set: dict[str, list[tuple[int, BudgetSnapshotV1]]] = defaultdict(list)
    for row in connection.execute(  # type: ignore[attr-defined]
        sa.select(budget_snapshots_table).order_by(
            budget_snapshots_table.c.budget_set_snapshot_id,
            budget_snapshots_table.c.ordinal,
        )
    ).all():
        selected_set_id = str(row.budget_set_snapshot_id)
        if selected_set_id not in budget_set_models:
            raise RuntimeError("0012 found a budget snapshot without its budget set")
        parsed_snapshot = _model_payload(
            row.payload,
            BudgetSnapshotV1,
            label="budget snapshot",
        )
        if not isinstance(parsed_snapshot, BudgetSnapshotV1):  # pragma: no cover - defensive
            raise RuntimeError("0012 parsed an unexpected budget snapshot type")
        _require_projection(
            row,
            parsed_snapshot,
            (
                "snapshot_id",
                "budget_id",
                "scope_kind",
                "scope_id",
                "budget_revision_at_freeze",
            ),
            label="budget snapshot",
        )
        snapshots_by_set[selected_set_id].append((int(row.ordinal), parsed_snapshot))
    for selected_set_id, parsed_set in budget_set_models.items():
        retained = snapshots_by_set.get(selected_set_id, [])
        if (
            tuple(ordinal for ordinal, _snapshot in retained)
            != tuple(range(1, len(parsed_set.snapshots) + 1))
            or tuple(snapshot for _ordinal, snapshot in retained) != parsed_set.snapshots
        ):
            raise RuntimeError("0012 budget snapshot members differ from budget-set payload")
    groups = {
        str(row.reservation_group_id): row
        for row in connection.execute(sa.select(groups_table)).all()  # type: ignore[attr-defined]
    }
    group_models: dict[str, ReservationGroupV1] = {}
    for group_id, row in groups.items():
        parsed = _model_payload(
            row.payload,
            ReservationGroupV1,
            label="reservation group",
        )
        if not isinstance(parsed, ReservationGroupV1):  # pragma: no cover - defensive
            raise RuntimeError("0012 parsed an unexpected reservation group type")
        _require_projection(
            row,
            parsed,
            (
                "reservation_group_id",
                "scope",
                "run_id",
                "budget_set_snapshot_id",
                "parent_hold_group_id",
                "attempt_no",
                "request_hash",
                "transport_attempt",
                "fencing_token",
                "idempotency_key",
                "status",
                "revision",
                "created_at",
                "expires_at",
            ),
            label="reservation group",
        )
        if parsed.reservation_group_id != group_id:
            raise RuntimeError("0012 found a reservation group identity mismatch")
        selected_set = budget_set_models.get(parsed.budget_set_snapshot_id)
        if selected_set is None or selected_set.run_id != parsed.run_id:
            raise RuntimeError("0012 reservation group differs from budget-set authority")
        group_models[group_id] = parsed
    members_by_group: dict[str, list[object]] = defaultdict(list)
    member_models_by_group: dict[str, list[BudgetReservationV1]] = defaultdict(list)
    for row in connection.execute(  # type: ignore[attr-defined]
        sa.select(members_table).order_by(
            members_table.c.reservation_group_id,
            members_table.c.reservation_id,
        )
    ).all():
        group_id = str(row.reservation_group_id)
        parsed = _model_payload(
            row.payload,
            BudgetReservationV1,
            label="budget reservation",
        )
        if not isinstance(parsed, BudgetReservationV1):  # pragma: no cover - defensive
            raise RuntimeError("0012 parsed an unexpected budget reservation type")
        _require_projection(
            row,
            parsed,
            ("reservation_id", "reservation_group_id", "budget_id", "status", "revision"),
            label="budget reservation",
        )
        members_by_group[group_id].append(row)
        member_models_by_group[group_id].append(parsed)
    if set(members_by_group) - set(group_models):
        raise RuntimeError("0012 found a budget reservation without its group")
    usages_by_group: dict[str, list[object]] = defaultdict(list)
    usage_models_by_group: dict[str, list[UsageEntryV1]] = defaultdict(list)
    for row in connection.execute(  # type: ignore[attr-defined]
        sa.select(usage_table).order_by(
            usage_table.c.reservation_group_id,
            usage_table.c.usage_id,
        )
    ).all():
        group_id = str(row.reservation_group_id)
        parsed = _model_payload(row.payload, UsageEntryV1, label="usage entry")
        if not isinstance(parsed, UsageEntryV1):  # pragma: no cover - defensive
            raise RuntimeError("0012 parsed an unexpected usage entry type")
        _require_projection(
            row,
            parsed,
            (
                "usage_id",
                "reservation_group_id",
                "scope",
                "run_id",
                "attempt_no",
                "request_hash",
                "transport_attempt",
                "execution_source",
                "retry_index",
                "routing_decision_kind",
                "routing_decision_id",
                "adjustment_of_usage_id",
                "fencing_token_at_reserve",
                "recorded_at",
            ),
            label="usage entry",
        )
        expected_native = (
            parsed.routing_decision_id if parsed.routing_decision_kind == "native" else None
        )
        expected_legacy = (
            parsed.routing_decision_id if parsed.routing_decision_kind == "legacy_import" else None
        )
        usage_identity = "usage-identity:sha256:" + _canonical_sha256(
            {
                "reservation_group_id": parsed.reservation_group_id,
                "scope": parsed.scope,
                "run_id": parsed.run_id,
                "attempt_no": parsed.attempt_no,
                "request_hash": parsed.request_hash,
                "transport_attempt": parsed.transport_attempt,
                "retry_index": parsed.retry_index,
                "adjustment_of_usage_id": parsed.adjustment_of_usage_id,
            }
        )
        if (
            row.native_routing_decision_id != expected_native
            or row.legacy_routing_decision_id != expected_legacy
            or row.usage_identity != usage_identity
        ):
            raise RuntimeError("0012 found a usage routing/identity projection mismatch")
        usages_by_group[group_id].append(row)
        usage_models_by_group[group_id].append(parsed)

    for group_id, group in group_models.items():
        members = member_models_by_group.get(group_id, [])
        if tuple(item.reservation_id for item in members) != group.budget_reservation_ids:
            raise RuntimeError("0012 found reservation members differing from group payload")
        if any(
            item.reservation_group_id != group_id
            or item.status != group.status
            or item.revision != group.revision
            for item in members
        ):
            raise RuntimeError("0012 found reservation member/head authority mismatch")
    usage_by_id = {
        item.usage_id: item for values in usage_models_by_group.values() for item in values
    }
    late_identity_fields = (
        "reservation_group_id",
        "budget_reservation_ids",
        "scope",
        "run_id",
        "attempt_no",
        "request_hash",
        "transport_attempt",
        "execution_source",
        "retry_index",
        "routing_decision_kind",
        "routing_decision_id",
        "fencing_token_at_reserve",
    )
    for group_id, usages in usage_models_by_group.items():
        group = group_models.get(group_id)
        if group is None or group.scope == "run_budget_hold":
            raise RuntimeError("0012 found usage without a child reservation group")
        for usage in usages:
            if (
                usage.budget_reservation_ids != group.budget_reservation_ids
                or usage.scope != group.scope
                or usage.run_id != group.run_id
                or usage.attempt_no != group.attempt_no
                or usage.request_hash != group.request_hash
                or usage.transport_attempt != group.transport_attempt
                or usage.fencing_token_at_reserve != group.fencing_token
            ):
                raise RuntimeError("0012 found usage differing from reservation authority")
            if usage.adjustment_of_usage_id is not None:
                original = usage_by_id.get(usage.adjustment_of_usage_id)
                if (
                    original is None
                    or original.adjustment_of_usage_id is not None
                    or any(
                        getattr(original, field) != getattr(usage, field)
                        for field in late_identity_fields
                    )
                ):
                    raise RuntimeError("0012 found invalid usage adjustment lineage")

    children_by_parent: dict[str, list[object]] = defaultdict(list)
    holds: list[object] = []
    for row in groups.values():
        if row.scope == "run_budget_hold":
            holds.append(row)
        elif row.parent_hold_group_id is not None:
            children_by_parent[str(row.parent_hold_group_id)].append(row)
        else:
            raise RuntimeError("0012 found a child reservation without a Run hold")
    hold_ids = {str(row.reservation_group_id) for row in holds}
    if set(children_by_parent) - hold_ids:
        raise RuntimeError("0012 found a child whose parent is not a Run hold")

    inserts: list[dict[str, object]] = []
    open_hold_contributions: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for hold in sorted(holds, key=lambda item: str(item.reservation_group_id)):
        hold_id = str(hold.reservation_group_id)
        hold_model = group_models[hold_id]
        if hold.status not in {"reserved", "released"} or int(hold.revision) < 1:
            raise RuntimeError("0012 found an invalid Run hold head")
        parent_rows = members_by_group.get(hold_id, [])
        parent: dict[str, dict[str, tuple[dict[str, object], Decimal]]] = {}
        for row in parent_rows:
            if row.status != hold.status or int(row.revision) != int(hold.revision):
                raise RuntimeError("0012 found a Run hold member/head mismatch")
            member_payload = _payload(row.payload, label="reservation")
            if (
                member_payload.get("reservation_group_id") != hold_id
                or member_payload.get("budget_id") != row.budget_id
            ):
                raise RuntimeError("0012 found a Run hold member projection mismatch")
            budget_id = str(row.budget_id)
            if budget_id in parent:
                raise RuntimeError("0012 found a repeated Run hold budget")
            parent[budget_id] = _amounts(
                member_payload.get("reserved"),
                label="Run hold",
            )
        if not parent:
            raise RuntimeError("0012 found a Run hold without members")
        selected_set = budget_set_models[hold_model.budget_set_snapshot_id]
        expected_hold_dimensions = {
            snapshot.budget_id: {
                item.dimension for item in snapshot.limits if item.dimension != "concurrent_run"
            }
            for snapshot in selected_set.snapshots
            if any(item.dimension != "concurrent_run" for item in snapshot.limits)
        }
        if set(parent) != set(expected_hold_dimensions) or any(
            set(parent[budget_id]) != dimensions
            for budget_id, dimensions in expected_hold_dimensions.items()
        ):
            raise RuntimeError("0012 Run hold members differ from budget-set authority")
        snapshots_by_budget = {snapshot.budget_id: snapshot for snapshot in selected_set.snapshots}
        for budget_id, identities in parent.items():
            budget = budget_models.get(budget_id)
            if budget is None:
                raise RuntimeError("0012 found a Run hold for an unknown budget")
            limits = _amounts(
                budget.model_dump(mode="json").get("limits"),
                label="budget limit",
            )
            hold_dimensions = set(identities)
            expected_dimensions = set(limits) - {"concurrent_run"}
            if hold_dimensions != expected_dimensions:
                raise RuntimeError("0012 Run hold dimensions differ from budget authority")
            for dimension, (identity, held) in identities.items():
                limit_identity, limit = limits[dimension]
                snapshot_identity = _amounts(
                    snapshots_by_budget[budget_id].model_dump(mode="json").get("limits"),
                    label="budget snapshot limit",
                )[dimension][0]
                if (
                    identity.get("unit") != limit_identity.get("unit")
                    or identity.get("currency") != limit_identity.get("currency")
                    or identity.get("unit") != snapshot_identity.get("unit")
                    or identity.get("currency") != snapshot_identity.get("currency")
                    or held > limit
                ):
                    raise RuntimeError("0012 Run hold amount exceeds budget authority")

        active = {
            budget_id: {dimension: Decimal(0) for dimension in identities}
            for budget_id, identities in parent.items()
        }
        settled = {
            budget_id: {dimension: Decimal(0) for dimension in identities}
            for budget_id, identities in parent.items()
        }
        active_counts = {budget_id: 0 for budget_id in parent}

        for child in sorted(
            children_by_parent.get(hold_id, []),
            key=lambda item: str(item.reservation_group_id),
        ):
            child_id = str(child.reservation_group_id)
            child_model = group_models[child_id]
            if (
                child_model.parent_hold_group_id != hold_id
                or child_model.run_id != hold_model.run_id
                or child_model.budget_set_snapshot_id != hold_model.budget_set_snapshot_id
                or child_model.scope == "run_budget_hold"
            ):
                raise RuntimeError("0012 found a child differing from its Run hold")
            child_rows = members_by_group.get(child_id, [])
            child_members: dict[str, dict[str, tuple[dict[str, object], Decimal]]] = {}
            declared_dimensions: set[str] = set()
            for row in child_rows:
                if row.status != child.status or int(row.revision) != int(child.revision):
                    raise RuntimeError("0012 found a child reservation member/head mismatch")
                member_payload = _payload(row.payload, label="reservation")
                budget_id = str(row.budget_id)
                if (
                    member_payload.get("reservation_group_id") != child_id
                    or member_payload.get("budget_id") != budget_id
                    or budget_id in child_members
                ):
                    raise RuntimeError("0012 found an invalid child reservation projection")
                child_members[budget_id] = _amounts(
                    member_payload.get("reserved"),
                    label="child reservation",
                )
                declared_dimensions.update(child_members[budget_id])
            expected = {
                budget_id: set(identities) & declared_dimensions
                for budget_id, identities in parent.items()
                if set(identities) & declared_dimensions
            }
            if set(expected) != set(child_members) or any(
                set(child_members[budget_id]) != dimensions
                for budget_id, dimensions in expected.items()
            ):
                raise RuntimeError("0012 found an incomplete child reservation projection")

            final_usage: dict[str, tuple[str, str | None, Decimal]] | None = None
            usage_rows = usages_by_group.get(child_id, [])
            originals = [row for row in usage_rows if row.adjustment_of_usage_id is None]
            adjustments = [row for row in usage_rows if row.adjustment_of_usage_id is not None]
            if child.status in {"reserved", "held_unknown"}:
                if usage_rows:
                    raise RuntimeError("0012 found usage on an active child reservation")
            elif child.status == "released":
                if usage_rows:
                    raise RuntimeError("0012 found usage on a released child reservation")
            elif child.status in {
                "reconciled",
                "conservatively_settled",
                "late_reconciled",
            }:
                if len(originals) != 1:
                    raise RuntimeError("0012 found settled child without one original usage")
                if child.status == "late_reconciled":
                    if (
                        len(adjustments) != 1
                        or adjustments[0].adjustment_of_usage_id != originals[0].usage_id
                    ):
                        raise RuntimeError("0012 found invalid late usage lineage")
                    final_usage = _usage_values(_payload(adjustments[0].payload, label="usage"))
                else:
                    if adjustments:
                        raise RuntimeError("0012 found adjustment on a non-late child")
                    final_usage = _usage_values(_payload(originals[0].payload, label="usage"))
            else:
                raise RuntimeError("0012 found an unknown child reservation status")

            for budget_id, child_amounts in child_members.items():
                for dimension, (child_identity, allocated) in child_amounts.items():
                    parent_identity = parent[budget_id][dimension][0]
                    if child_identity.get("unit") != parent_identity.get(
                        "unit"
                    ) or child_identity.get("currency") != parent_identity.get("currency"):
                        raise RuntimeError("0012 found a child/parent amount identity mismatch")
                    if allocated > parent[budget_id][dimension][1]:
                        raise RuntimeError("0012 child allocation exceeds parent authority")
                    if child.status in {"reserved", "held_unknown"}:
                        active[budget_id][dimension] = _exact_decimal_add(
                            active[budget_id][dimension],
                            allocated,
                        )
                    elif final_usage is not None:
                        if dimension not in final_usage:
                            raise RuntimeError("0012 found settled usage missing a held dimension")
                        observed_unit, observed_currency, observed_value = final_usage[dimension]
                        if observed_unit != child_identity.get(
                            "unit"
                        ) or observed_currency != child_identity.get("currency"):
                            raise RuntimeError(
                                "0012 settled usage amount identity differs from reservation"
                            )
                        settled[budget_id][dimension] = _exact_decimal_add(
                            settled[budget_id][dimension],
                            min(allocated, observed_value),
                        )
                if child.status in {"reserved", "held_unknown"}:
                    active_counts[budget_id] += 1

        if hold.status == "released" and any(active_counts.values()):
            raise RuntimeError("0012 found a released Run hold with active children")
        for budget_id in sorted(parent):
            identities = parent[budget_id]
            if any(
                active[budget_id][dimension] > held
                for dimension, (_identity, held) in identities.items()
            ):
                raise RuntimeError("0012 active Run hold balance exceeds parent authority")
            if hold.status == "reserved":
                for dimension, (_identity, held) in identities.items():
                    available = max(
                        _exact_decimal_subtract(
                            _exact_decimal_subtract(
                                held,
                                active[budget_id][dimension],
                            ),
                            settled[budget_id][dimension],
                        ),
                        Decimal(0),
                    )
                    contribution = _exact_decimal_add(
                        active[budget_id][dimension],
                        available,
                    )
                    open_hold_contributions[budget_id][dimension] = _exact_decimal_add(
                        open_hold_contributions[budget_id][dimension],
                        contribution,
                    )
            balance_payload = {
                "balance_schema_version": _SCHEMA_VERSION,
                "hold_group_id": hold_id,
                "budget_id": budget_id,
                "status": str(hold.status),
                "revision": int(hold.revision),
                "active_child_count": active_counts[budget_id],
                "active_allocated": [
                    _amount_wire(identities[dimension][0], active[budget_id][dimension])
                    for dimension in sorted(identities)
                ],
                "settled_impact": [
                    _amount_wire(identities[dimension][0], settled[budget_id][dimension])
                    for dimension in sorted(identities)
                ],
            }
            inserts.append(
                {
                    "hold_group_id": hold_id,
                    "budget_id": budget_id,
                    "status": str(hold.status),
                    "revision": int(hold.revision),
                    "active_child_count": active_counts[budget_id],
                    "balance_digest": _canonical_sha256(balance_payload),
                    "payload": balance_payload,
                }
            )
    unknown_contribution_budgets = set(open_hold_contributions) - set(budget_models)
    if unknown_contribution_budgets:
        raise RuntimeError("0012 reconstructed a contribution for an unknown budget")
    for budget_id, parsed_budget in budget_models.items():
        budget_payload = parsed_budget.model_dump(mode="json")
        limits = _amounts(budget_payload.get("limits"), label="budget limit")
        reserved = _amounts(budget_payload.get("reserved"), label="budget reserve")
        if set(reserved) - set(limits):
            raise RuntimeError("0012 found a budget reserve without a limit")
        expected = open_hold_contributions.get(budget_id, {})
        if set(expected) - set(limits):
            raise RuntimeError("0012 reconstructed a contribution for an unknown dimension")
        for dimension, (limit_identity, _limit_value) in limits.items():
            if dimension == "concurrent_run":
                continue
            actual_identity, actual = reserved.get(
                dimension,
                (limit_identity, Decimal(0)),
            )
            # Revision 0011 retains current heads, but no trustworthy global order for
            # reserve/release/settlement transitions.  A context-rounded aggregate
            # therefore cannot be redistributed into per-hold balances losslessly.
            if (
                actual_identity.get("unit") != limit_identity.get("unit")
                or actual_identity.get("currency") != limit_identity.get("currency")
                or actual != expected.get(dimension, Decimal(0))
            ):
                raise RuntimeError(
                    "0012 budget reserve differs from reconstructed open Run holds; "
                    "pre-0012 transition ordering cannot recover context-rounded authority "
                    "losslessly"
                )
    return inserts


def upgrade() -> None:
    connection = op.get_bind()
    inserts = _reconstruct(connection)
    op.create_table(
        "run_hold_balances",
        sa.Column("hold_group_id", sa.String(), primary_key=True),
        sa.Column("budget_id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active_child_count", sa.Integer(), nullable=False),
        sa.Column("balance_digest", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["hold_group_id", "budget_id"],
            [
                "budget_reservations.reservation_group_id",
                "budget_reservations.budget_id",
            ],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'released')",
            name="ck_run_hold_balance_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_run_hold_balance_revision"),
        sa.CheckConstraint(
            "active_child_count >= 0",
            name="ck_run_hold_balance_active_child_count",
        ),
    )
    if inserts:
        balances_table = sa.table(
            "run_hold_balances",
            sa.column("hold_group_id", sa.String()),
            sa.column("budget_id", sa.String()),
            sa.column("status", sa.String()),
            sa.column("revision", sa.Integer()),
            sa.column("active_child_count", sa.Integer()),
            sa.column("balance_digest", sa.String()),
            sa.column("payload", sa.JSON()),
        )
        connection.execute(balances_table.insert(), inserts)


def downgrade() -> None:
    op.drop_table("run_hold_balances")
