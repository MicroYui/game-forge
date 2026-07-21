"""Transaction-bound SQLite persistence for M4b cost and routing authority."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_sha256, typed_canonical_json
from gameforge.contracts.cassette_import import LegacyImportRoutingDecisionV1
from gameforge.contracts.cost import (
    BudgetReservationV1,
    BudgetSetSnapshotV1,
    BudgetSnapshotV1,
    BudgetV1,
    ConcurrencyPermitV1,
    CostSettlementGroupCountV1,
    CostSettlementSummaryV1,
    PermitGroupV1,
    ReservationGroupV1,
    UsageEntryV1,
)
from gameforge.contracts.errors import IntegrityViolation, QueryTooBroad
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    RoutingDecisionV1,
    RoutingPolicyV1,
    validate_policy_catalog_closure,
)
from gameforge.runtime.persistence.models import (
    BudgetReservationRow,
    BudgetRow,
    BudgetSetSnapshotRow,
    BudgetSnapshotRow,
    ConcurrencyPermitRow,
    LegacyImportRoutingDecisionRow,
    ModelCatalogSnapshotRow,
    PermitGroupRow,
    ReservationGroupRow,
    RoutingDecisionRow,
    RoutingPolicyRow,
    UsageEntryRow,
)


_ModelT = TypeVar("_ModelT", bound=BaseModel)
MAX_LEDGER_QUERY_ITEMS = 1_000


def _canonical_model(value: object, model_type: type[_ModelT], *, label: str) -> _ModelT:
    if type(value) is not model_type:
        raise IntegrityViolation(f"{label} requires an exact {model_type.__name__}")
    try:
        wire = value.model_dump(mode="json")  # type: ignore[union-attr]
        parsed = model_type.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"{label} wire is invalid") from exc
    if typed_canonical_json(parsed.model_dump(mode="json")) != typed_canonical_json(wire):
        raise IntegrityViolation(f"{label} wire is noncanonical")
    return parsed


def _parse_payload(
    payload: object,
    model_type: type[_ModelT],
    *,
    label: str,
    identity: str,
) -> _ModelT:
    if not isinstance(payload, dict):
        raise IntegrityViolation(f"stored {label} payload is not an object", identity=identity)
    try:
        parsed = model_type.model_validate(payload)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"stored {label} is invalid", identity=identity) from exc
    if typed_canonical_json(parsed.model_dump(mode="json")) != typed_canonical_json(payload):
        raise IntegrityViolation(f"stored {label} payload is noncanonical", identity=identity)
    return parsed


def _require_projection(
    row: object,
    model: BaseModel,
    fields: Sequence[str],
    *,
    label: str,
    identity: str,
) -> None:
    wire = model.model_dump(mode="json")
    for field_name in fields:
        if getattr(row, field_name) != wire[field_name]:
            raise IntegrityViolation(
                f"stored {label} projection differs from its payload",
                identity=identity,
                field=field_name,
            )


def _same_model(left: BaseModel, right: BaseModel) -> bool:
    return typed_canonical_json(left.model_dump(mode="json")) == typed_canonical_json(
        right.model_dump(mode="json")
    )


def _usage_identity(usage: UsageEntryV1) -> str:
    payload = {
        "reservation_group_id": usage.reservation_group_id,
        "scope": usage.scope,
        "run_id": usage.run_id,
        "attempt_no": usage.attempt_no,
        "request_hash": usage.request_hash,
        "transport_attempt": usage.transport_attempt,
        "retry_index": usage.retry_index,
        "adjustment_of_usage_id": usage.adjustment_of_usage_id,
    }
    return "usage-identity:sha256:" + canonical_sha256(payload)


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_LEDGER_QUERY_ITEMS:
        raise QueryTooBroad(
            "ledger query limit is outside the supported range",
            max_limit=MAX_LEDGER_QUERY_ITEMS,
        )
    return limit


class SqlCostRepository:
    """Share the owning UoW Session and never commit independently."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def put_budget(self, budget: BudgetV1) -> BudgetV1:
        canonical = _canonical_model(budget, BudgetV1, label="budget")
        existing = self.get_budget(canonical.budget_id)
        if existing is not None:
            if not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "budget id has different authoritative content",
                    budget_id=canonical.budget_id,
                )
            return existing
        wire = canonical.model_dump(mode="json")
        self._session.add(
            BudgetRow(
                budget_id=canonical.budget_id,
                scope_kind=canonical.scope_kind,
                scope_id=canonical.scope_id,
                policy_version=canonical.policy_version,
                status=canonical.status,
                revision=canonical.revision,
                deadline_utc=wire["deadline_utc"],
                created_at=wire["created_at"],
                payload=wire,
            )
        )
        self._flush("budget", budget_id=canonical.budget_id)
        return canonical

    def get_budget(self, budget_id: str) -> BudgetV1 | None:
        row = self._session.get(BudgetRow, budget_id)
        if row is None:
            return None
        parsed = _parse_payload(row.payload, BudgetV1, label="budget", identity=budget_id)
        _require_projection(
            row,
            parsed,
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
            identity=budget_id,
        )
        return parsed

    def get_budgets_many(self, budget_ids: Sequence[str]) -> dict[str, BudgetV1 | None]:
        """Read exact current Budget heads with bounded set statements."""

        selected = tuple(dict.fromkeys(budget_ids))
        if any(not isinstance(budget_id, str) or not budget_id for budget_id in selected):
            raise ValueError("budget ids must be non-empty strings")
        retained: dict[str, BudgetV1 | None] = dict.fromkeys(selected)
        for offset in range(0, len(selected), 900):
            rows = self._session.scalars(
                select(BudgetRow).where(BudgetRow.budget_id.in_(selected[offset : offset + 900]))
            ).all()
            for row in rows:
                parsed = _parse_payload(
                    row.payload,
                    BudgetV1,
                    label="budget",
                    identity=row.budget_id,
                )
                _require_projection(
                    row,
                    parsed,
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
                    identity=row.budget_id,
                )
                retained[row.budget_id] = parsed
        return retained

    def list_budgets_by_scope_identity(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        limit: int,
    ) -> tuple[BudgetV1, ...]:
        """Return retained current Budget heads for one exact scope identity.

        Callers must provide an explicit bounded limit.  Ordering by the immutable
        budget id makes policy selection stable.  Multiple independently versioned
        policies may apply to the same identity; closed/exhausted heads remain in the
        result so admission cannot bypass a rejecting applicable budget.
        """

        limit = _validate_limit(limit)
        rows = self._session.scalars(
            select(BudgetRow)
            .where(
                BudgetRow.scope_kind == scope_kind,
                BudgetRow.scope_id == scope_id,
            )
            .order_by(BudgetRow.budget_id)
            .limit(limit)
        ).all()
        budgets: list[BudgetV1] = []
        for row in rows:
            budget = self.get_budget(row.budget_id)
            if budget is None:
                raise IntegrityViolation(
                    "budget scope query lost an authoritative row",
                    budget_id=row.budget_id,
                )
            budgets.append(budget)
        return tuple(budgets)

    def put_budget_set(self, budget_set: BudgetSetSnapshotV1) -> BudgetSetSnapshotV1:
        canonical = _canonical_model(
            budget_set,
            BudgetSetSnapshotV1,
            label="budget set snapshot",
        )
        existing = self.get_budget_set(canonical.budget_set_snapshot_id)
        if existing is not None:
            if not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "budget-set snapshot id has different immutable content",
                    budget_set_snapshot_id=canonical.budget_set_snapshot_id,
                )
            return existing
        run_collision = self._session.scalar(
            select(BudgetSetSnapshotRow).where(BudgetSetSnapshotRow.run_id == canonical.run_id)
        )
        if run_collision is not None:
            raise IntegrityViolation(
                "run already has a different budget-set snapshot",
                run_id=canonical.run_id,
            )
        for snapshot in canonical.snapshots:
            budget = self.get_budget(snapshot.budget_id)
            if budget is None:
                raise IntegrityViolation(
                    "budget-set snapshot references an unavailable budget",
                    budget_id=snapshot.budget_id,
                )
            if (
                snapshot.scope_kind != budget.scope_kind
                or snapshot.scope_id != budget.scope_id
                or snapshot.policy_version != budget.policy_version
                or snapshot.budget_revision_at_freeze != budget.revision
            ):
                raise IntegrityViolation(
                    "budget snapshot identity differs from current budget",
                    budget_id=snapshot.budget_id,
                )
        wire = canonical.model_dump(mode="json")
        self._session.add(
            BudgetSetSnapshotRow(
                budget_set_snapshot_id=canonical.budget_set_snapshot_id,
                run_id=canonical.run_id,
                selection_policy_version=canonical.selection_policy_version,
                captured_at=wire["captured_at"],
                payload=wire,
            )
        )
        for ordinal, snapshot in enumerate(canonical.snapshots, start=1):
            snapshot_wire = snapshot.model_dump(mode="json")
            self._session.add(
                BudgetSnapshotRow(
                    snapshot_id=snapshot.snapshot_id,
                    budget_set_snapshot_id=canonical.budget_set_snapshot_id,
                    ordinal=ordinal,
                    budget_id=snapshot.budget_id,
                    scope_kind=snapshot.scope_kind,
                    scope_id=snapshot.scope_id,
                    budget_revision_at_freeze=snapshot.budget_revision_at_freeze,
                    payload=snapshot_wire,
                )
            )
        self._flush(
            "budget set snapshot",
            budget_set_snapshot_id=canonical.budget_set_snapshot_id,
        )
        return canonical

    def get_budget_set(self, budget_set_snapshot_id: str) -> BudgetSetSnapshotV1 | None:
        row = self._session.get(BudgetSetSnapshotRow, budget_set_snapshot_id)
        if row is None:
            return None
        parsed = _parse_payload(
            row.payload,
            BudgetSetSnapshotV1,
            label="budget set snapshot",
            identity=budget_set_snapshot_id,
        )
        _require_projection(
            row,
            parsed,
            (
                "budget_set_snapshot_id",
                "run_id",
                "selection_policy_version",
                "captured_at",
            ),
            label="budget set snapshot",
            identity=budget_set_snapshot_id,
        )
        members = self._session.scalars(
            select(BudgetSnapshotRow)
            .where(BudgetSnapshotRow.budget_set_snapshot_id == budget_set_snapshot_id)
            .order_by(BudgetSnapshotRow.ordinal)
        ).all()
        stored = tuple(self._parse_budget_snapshot_row(member) for member in members)
        if stored != parsed.snapshots or tuple(member.ordinal for member in members) != tuple(
            range(1, len(parsed.snapshots) + 1)
        ):
            raise IntegrityViolation(
                "budget-set member rows differ from the immutable payload",
                budget_set_snapshot_id=budget_set_snapshot_id,
            )
        return parsed

    def put_reservation_group(
        self,
        group: ReservationGroupV1,
        reservations: Sequence[BudgetReservationV1],
    ) -> ReservationGroupV1:
        canonical, members = self._canonical_reservation_group_write(group, reservations)
        existing = self.get_reservation_group(canonical.reservation_group_id)
        if existing is not None:
            retained_members = self.list_budget_reservations(canonical.reservation_group_id)
            if not _same_model(existing, canonical) or retained_members != members:
                raise IntegrityViolation(
                    "reservation group id has different authoritative content",
                    reservation_group_id=canonical.reservation_group_id,
                )
            return existing
        collision = self._session.scalar(
            select(ReservationGroupRow).where(
                ReservationGroupRow.run_id == canonical.run_id,
                ReservationGroupRow.scope == canonical.scope,
                ReservationGroupRow.idempotency_key == canonical.idempotency_key,
            )
        )
        if collision is not None:
            raise IntegrityViolation(
                "reservation idempotency key is bound to a different group",
                run_id=canonical.run_id,
                scope=canonical.scope,
            )
        budget_set = self.get_budget_set(canonical.budget_set_snapshot_id)
        if budget_set is None or budget_set.run_id != canonical.run_id:
            raise IntegrityViolation(
                "reservation group has no matching budget-set snapshot",
                reservation_group_id=canonical.reservation_group_id,
            )
        if canonical.parent_hold_group_id is not None:
            parent = self.get_reservation_group(canonical.parent_hold_group_id)
            if (
                parent is None
                or parent.scope != "run_budget_hold"
                or parent.run_id != canonical.run_id
                or parent.budget_set_snapshot_id != canonical.budget_set_snapshot_id
            ):
                raise IntegrityViolation(
                    "child reservation has no matching run hold",
                    reservation_group_id=canonical.reservation_group_id,
                )
        if (
            tuple(sorted(item.reservation_id for item in members))
            != canonical.budget_reservation_ids
        ):
            raise IntegrityViolation(
                "reservation members differ from group ids",
                reservation_group_id=canonical.reservation_group_id,
            )
        budget_ids = {snapshot.budget_id for snapshot in budget_set.snapshots}
        if len({item.budget_id for item in members}) != len(members):
            raise IntegrityViolation("reservation group repeats a budget member")
        for member in members:
            if (
                member.reservation_group_id != canonical.reservation_group_id
                or member.budget_id not in budget_ids
                or member.status != canonical.status
            ):
                raise IntegrityViolation(
                    "budget reservation does not match its group",
                    reservation_id=member.reservation_id,
                )
        return self._insert_reservation_group_rows(canonical, members)

    @staticmethod
    def _canonical_reservation_group_write(
        group: ReservationGroupV1,
        reservations: Sequence[BudgetReservationV1],
    ) -> tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]]:
        """Clone one reservation aggregate into exact canonical contracts."""

        canonical = _canonical_model(group, ReservationGroupV1, label="reservation group")
        members = tuple(
            sorted(
                (
                    _canonical_model(item, BudgetReservationV1, label="budget reservation")
                    for item in reservations
                ),
                key=lambda item: item.reservation_id,
            )
        )
        return canonical, members

    def _insert_reservation_group_rows(
        self,
        canonical: ReservationGroupV1,
        members: tuple[BudgetReservationV1, ...],
    ) -> ReservationGroupV1:
        """Insert an aggregate whose immutable authority was already preflighted."""

        wire = canonical.model_dump(mode="json")
        self._session.add(
            ReservationGroupRow(
                reservation_group_id=canonical.reservation_group_id,
                scope=canonical.scope,
                run_id=canonical.run_id,
                budget_set_snapshot_id=canonical.budget_set_snapshot_id,
                parent_hold_group_id=canonical.parent_hold_group_id,
                attempt_no=canonical.attempt_no,
                request_hash=canonical.request_hash,
                transport_attempt=canonical.transport_attempt,
                fencing_token=canonical.fencing_token,
                idempotency_key=canonical.idempotency_key,
                status=canonical.status,
                revision=canonical.revision,
                created_at=wire["created_at"],
                expires_at=wire["expires_at"],
                payload=wire,
            )
        )
        self._flush(
            "reservation group head",
            reservation_group_id=canonical.reservation_group_id,
        )
        for member in members:
            self._session.add(
                BudgetReservationRow(
                    reservation_id=member.reservation_id,
                    reservation_group_id=member.reservation_group_id,
                    budget_id=member.budget_id,
                    status=member.status,
                    revision=member.revision,
                    payload=member.model_dump(mode="json"),
                )
            )
        self._flush(
            "reservation group",
            reservation_group_id=canonical.reservation_group_id,
        )
        return canonical

    def get_reservation_group(self, reservation_group_id: str) -> ReservationGroupV1 | None:
        row = self._session.get(ReservationGroupRow, reservation_group_id)
        if row is None:
            return None
        parsed = self._parse_reservation_group_row(row)
        ids = tuple(
            self._session.scalars(
                select(BudgetReservationRow.reservation_id)
                .where(BudgetReservationRow.reservation_group_id == reservation_group_id)
                .order_by(BudgetReservationRow.reservation_id)
            ).all()
        )
        if ids != parsed.budget_reservation_ids:
            raise IntegrityViolation(
                "reservation member rows differ from the group payload",
                reservation_group_id=reservation_group_id,
            )
        return parsed

    def get_reservation_group_with_members(
        self,
        reservation_group_id: str,
    ) -> tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]] | None:
        """Read and close one reservation aggregate in two indexed statements."""

        row = self._session.get(ReservationGroupRow, reservation_group_id)
        if row is None:
            return None
        parsed = self._parse_reservation_group_row(row)
        member_rows = self._session.scalars(
            select(BudgetReservationRow).where(
                BudgetReservationRow.reservation_group_id == reservation_group_id
            )
        ).all()
        members = tuple(
            sorted(
                (self._parse_budget_reservation_row(member) for member in member_rows),
                key=lambda member: member.reservation_id,
            )
        )
        if tuple(member.reservation_id for member in members) != parsed.budget_reservation_ids:
            raise IntegrityViolation(
                "reservation member rows differ from the group payload",
                reservation_group_id=reservation_group_id,
            )
        return parsed, members

    def get_reservation_groups_many(
        self,
        reservation_group_ids: Sequence[str],
    ) -> dict[str, ReservationGroupV1 | None]:
        """Read exact reservation groups and member closures in bounded statements."""

        selected = tuple(dict.fromkeys(reservation_group_ids))
        if any(not isinstance(group_id, str) or not group_id for group_id in selected):
            raise ValueError("reservation group ids must be non-empty strings")
        retained: dict[str, ReservationGroupV1 | None] = dict.fromkeys(selected)
        rows_by_id: dict[str, ReservationGroupRow] = {}
        member_ids: dict[str, list[str]] = {group_id: [] for group_id in selected}
        for offset in range(0, len(selected), 900):
            chunk = selected[offset : offset + 900]
            for row in self._session.scalars(
                select(ReservationGroupRow).where(
                    ReservationGroupRow.reservation_group_id.in_(chunk)
                )
            ).all():
                rows_by_id[row.reservation_group_id] = row
            for group_id, reservation_id in self._session.execute(
                select(
                    BudgetReservationRow.reservation_group_id,
                    BudgetReservationRow.reservation_id,
                )
                .where(BudgetReservationRow.reservation_group_id.in_(chunk))
                .order_by(
                    BudgetReservationRow.reservation_group_id,
                    BudgetReservationRow.reservation_id,
                )
            ).all():
                member_ids[str(group_id)].append(str(reservation_id))
        for group_id, row in rows_by_id.items():
            parsed = _parse_payload(
                row.payload,
                ReservationGroupV1,
                label="reservation group",
                identity=group_id,
            )
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
                identity=group_id,
            )
            if tuple(member_ids[group_id]) != parsed.budget_reservation_ids:
                raise IntegrityViolation(
                    "reservation member rows differ from the group payload",
                    reservation_group_id=group_id,
                )
            retained[group_id] = parsed
        return retained

    def list_budget_reservations(
        self,
        reservation_group_id: str,
    ) -> tuple[BudgetReservationV1, ...]:
        rows = self._session.scalars(
            select(BudgetReservationRow)
            .where(BudgetReservationRow.reservation_group_id == reservation_group_id)
            .order_by(BudgetReservationRow.reservation_id)
        ).all()
        return tuple(self._parse_budget_reservation_row(row) for row in rows)

    def get_budget_reservations_many(
        self,
        reservation_group_ids: Sequence[str],
    ) -> dict[str, tuple[BudgetReservationV1, ...]]:
        """Read reservation member payloads for a bounded group set."""

        selected = tuple(dict.fromkeys(reservation_group_ids))
        if any(not isinstance(group_id, str) or not group_id for group_id in selected):
            raise ValueError("reservation group ids must be non-empty strings")
        retained: dict[str, list[BudgetReservationV1]] = {group_id: [] for group_id in selected}
        for offset in range(0, len(selected), 900):
            rows = self._session.scalars(
                select(BudgetReservationRow)
                .where(
                    BudgetReservationRow.reservation_group_id.in_(selected[offset : offset + 900])
                )
                .order_by(
                    BudgetReservationRow.reservation_group_id,
                    BudgetReservationRow.reservation_id,
                )
            ).all()
            for row in rows:
                retained[row.reservation_group_id].append(self._parse_budget_reservation_row(row))
        return {group_id: tuple(values) for group_id, values in retained.items()}

    def get_usage_by_reservation_groups_many(
        self,
        reservation_group_ids: Sequence[str],
    ) -> dict[str, tuple[UsageEntryV1, ...]]:
        """Read complete usage history for a bounded reservation-group set."""

        selected = tuple(dict.fromkeys(reservation_group_ids))
        if any(not isinstance(group_id, str) or not group_id for group_id in selected):
            raise ValueError("reservation group ids must be non-empty strings")
        retained: dict[str, list[UsageEntryV1]] = {group_id: [] for group_id in selected}
        for offset in range(0, len(selected), 900):
            chunk = selected[offset : offset + 900]
            rows = self._session.scalars(
                select(UsageEntryRow)
                .where(UsageEntryRow.reservation_group_id.in_(chunk))
                .order_by(
                    UsageEntryRow.reservation_group_id,
                    UsageEntryRow.recorded_at,
                    UsageEntryRow.usage_id,
                )
                .limit(len(chunk) * 2 + 1)
            ).all()
            if len(rows) > len(chunk) * 2:
                raise IntegrityViolation("reservation usage authority exceeds its hard cap")
            for row in rows:
                retained[row.reservation_group_id].append(self._parse_usage_row(row))
        if any(len(values) > 2 for values in retained.values()):
            raise IntegrityViolation("reservation group has excess usage authority")
        return {group_id: tuple(values) for group_id, values in retained.items()}

    def put_usage(self, usage: UsageEntryV1) -> UsageEntryV1:
        canonical = _canonical_model(usage, UsageEntryV1, label="usage entry")
        existing = self._get_usage(canonical.usage_id)
        if existing is not None:
            if not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "usage id has different append-only content",
                    usage_id=canonical.usage_id,
                )
            return existing
        group = self.get_reservation_group(canonical.reservation_group_id)
        if group is None:
            raise IntegrityViolation(
                "usage references an unavailable reservation group",
                usage_id=canonical.usage_id,
            )
        expected = (
            canonical.scope == group.scope
            and canonical.run_id == group.run_id
            and canonical.attempt_no == group.attempt_no
            and canonical.request_hash == group.request_hash
            and canonical.transport_attempt == group.transport_attempt
            and canonical.fencing_token_at_reserve == group.fencing_token
            and canonical.budget_reservation_ids == group.budget_reservation_ids
        )
        if not expected:
            raise IntegrityViolation(
                "usage identity differs from its reservation group",
                usage_id=canonical.usage_id,
            )
        self._validate_usage_routing(canonical)
        if canonical.adjustment_of_usage_id is not None:
            original = self._get_usage(canonical.adjustment_of_usage_id)
            if original is None or original.adjustment_of_usage_id is not None:
                raise IntegrityViolation(
                    "usage adjustment does not reference an original entry",
                    usage_id=canonical.usage_id,
                )
        identity = _usage_identity(canonical)
        collision = self._session.scalar(
            select(UsageEntryRow).where(UsageEntryRow.usage_identity == identity)
        )
        if collision is not None:
            raise IntegrityViolation(
                "usage observation identity is bound to a different entry",
                usage_id=canonical.usage_id,
            )
        wire = canonical.model_dump(mode="json")
        self._session.add(
            UsageEntryRow(
                usage_id=canonical.usage_id,
                usage_identity=identity,
                reservation_group_id=canonical.reservation_group_id,
                scope=canonical.scope,
                run_id=canonical.run_id,
                attempt_no=canonical.attempt_no,
                request_hash=canonical.request_hash,
                transport_attempt=canonical.transport_attempt,
                execution_source=canonical.execution_source,
                retry_index=canonical.retry_index,
                routing_decision_kind=canonical.routing_decision_kind,
                routing_decision_id=canonical.routing_decision_id,
                native_routing_decision_id=(
                    canonical.routing_decision_id
                    if canonical.routing_decision_kind == "native"
                    else None
                ),
                legacy_routing_decision_id=(
                    canonical.routing_decision_id
                    if canonical.routing_decision_kind == "legacy_import"
                    else None
                ),
                adjustment_of_usage_id=canonical.adjustment_of_usage_id,
                fencing_token_at_reserve=canonical.fencing_token_at_reserve,
                recorded_at=wire["recorded_at"],
                payload=wire,
            )
        )
        self._flush("usage entry", usage_id=canonical.usage_id)
        return canonical

    def list_usage(
        self,
        *,
        run_id: str,
        attempt_no: int | None = None,
        limit: int = 100,
        after: tuple[str, str] | None = None,
    ) -> tuple[UsageEntryV1, ...]:
        limit = _validate_limit(limit)
        statement = select(UsageEntryRow).where(UsageEntryRow.run_id == run_id)
        if attempt_no is not None:
            statement = statement.where(UsageEntryRow.attempt_no == attempt_no)
        if after is not None:
            recorded_at, usage_id = after
            statement = statement.where(
                (UsageEntryRow.recorded_at > recorded_at)
                | ((UsageEntryRow.recorded_at == recorded_at) & (UsageEntryRow.usage_id > usage_id))
            )
        rows = self._session.scalars(
            statement.order_by(UsageEntryRow.recorded_at, UsageEntryRow.usage_id).limit(limit)
        ).all()
        return tuple(self._parse_usage_row(row) for row in rows)

    def summarize_run_settlement(self, *, run_id: str) -> CostSettlementSummaryV1:
        """Aggregate public settlement state without loading sensitive group identities."""

        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        group_rows = self._session.execute(
            select(
                ReservationGroupRow.scope,
                ReservationGroupRow.status,
                func.count(ReservationGroupRow.reservation_group_id),
            )
            .where(ReservationGroupRow.run_id == run_id)
            .group_by(ReservationGroupRow.scope, ReservationGroupRow.status)
        ).all()
        if not group_rows:
            raise IntegrityViolation(
                "Run has no reservation settlement authority",
                run_id=run_id,
            )
        group_counts = tuple(
            CostSettlementGroupCountV1(
                scope=str(scope),
                status=str(status),
                group_count=int(group_count),
            )
            for scope, status, group_count in group_rows
        )
        usage_entry_count, late_adjustment_usage_count = self._session.execute(
            select(
                func.count(UsageEntryRow.usage_id),
                func.count(UsageEntryRow.adjustment_of_usage_id),
            ).where(UsageEntryRow.run_id == run_id)
        ).one()
        exact_usage_count = int(usage_entry_count)
        return CostSettlementSummaryV1(
            group_counts=group_counts,
            total_group_count=sum(item.group_count for item in group_counts),
            held_unknown_group_count=sum(
                item.group_count for item in group_counts if item.status == "held_unknown"
            ),
            usage_entry_count=exact_usage_count,
            usage_evidence_status="recorded" if exact_usage_count else "not_recorded",
            late_adjustment_usage_count=int(late_adjustment_usage_count),
        )

    def put_permit_group(
        self,
        group: PermitGroupV1,
        permits: Sequence[ConcurrencyPermitV1],
    ) -> PermitGroupV1:
        canonical = _canonical_model(group, PermitGroupV1, label="permit group")
        members = tuple(
            sorted(
                (
                    _canonical_model(item, ConcurrencyPermitV1, label="concurrency permit")
                    for item in permits
                ),
                key=lambda item: item.permit_id,
            )
        )
        existing = self.get_permit_group(canonical.permit_group_id)
        if existing is not None:
            retained_members = self.list_concurrency_permits(canonical.permit_group_id)
            if not _same_model(existing, canonical) or retained_members != members:
                raise IntegrityViolation(
                    "permit group id has different authoritative content",
                    permit_group_id=canonical.permit_group_id,
                )
            return existing
        collision = self._session.scalar(
            select(PermitGroupRow).where(
                PermitGroupRow.run_id == canonical.run_id,
                PermitGroupRow.lease_id == canonical.lease_id,
                PermitGroupRow.fencing_token == canonical.fencing_token,
            )
        )
        if collision is not None:
            raise IntegrityViolation(
                "lease identity is bound to a different permit group",
                run_id=canonical.run_id,
                lease_id=canonical.lease_id,
            )
        budget_set = self.get_budget_set(canonical.budget_set_snapshot_id)
        if budget_set is None or budget_set.run_id != canonical.run_id:
            raise IntegrityViolation(
                "permit group has no matching budget-set snapshot",
                permit_group_id=canonical.permit_group_id,
            )
        if tuple(sorted(item.permit_id for item in members)) != canonical.permit_ids:
            raise IntegrityViolation(
                "permit members differ from group ids",
                permit_group_id=canonical.permit_group_id,
            )
        budget_ids = {snapshot.budget_id for snapshot in budget_set.snapshots}
        if len({item.budget_id for item in members}) != len(members):
            raise IntegrityViolation("permit group repeats a budget member")
        for member in members:
            if (
                member.permit_group_id != canonical.permit_group_id
                or member.budget_id not in budget_ids
                or member.run_id != canonical.run_id
                or member.lease_id != canonical.lease_id
                or member.fencing_token != canonical.fencing_token
                or member.status != canonical.status
                or member.acquired_at != canonical.acquired_at
                or member.expires_at != canonical.expires_at
            ):
                raise IntegrityViolation(
                    "concurrency permit does not match its group",
                    permit_id=member.permit_id,
                )
        wire = canonical.model_dump(mode="json")
        self._session.add(
            PermitGroupRow(
                permit_group_id=canonical.permit_group_id,
                budget_set_snapshot_id=canonical.budget_set_snapshot_id,
                run_id=canonical.run_id,
                lease_id=canonical.lease_id,
                fencing_token=canonical.fencing_token,
                status=canonical.status,
                revision=canonical.revision,
                acquired_at=wire["acquired_at"],
                expires_at=wire["expires_at"],
                payload=wire,
            )
        )
        self._flush("permit group head", permit_group_id=canonical.permit_group_id)
        for member in members:
            member_wire = member.model_dump(mode="json")
            self._session.add(
                ConcurrencyPermitRow(
                    permit_id=member.permit_id,
                    permit_group_id=member.permit_group_id,
                    budget_id=member.budget_id,
                    run_id=member.run_id,
                    lease_id=member.lease_id,
                    fencing_token=member.fencing_token,
                    status=member.status,
                    revision=member.revision,
                    acquired_at=member_wire["acquired_at"],
                    expires_at=member_wire["expires_at"],
                    payload=member_wire,
                )
            )
        self._flush("permit group", permit_group_id=canonical.permit_group_id)
        return canonical

    def get_permit_group(self, permit_group_id: str) -> PermitGroupV1 | None:
        row = self._session.get(PermitGroupRow, permit_group_id)
        if row is None:
            return None
        parsed = _parse_payload(
            row.payload,
            PermitGroupV1,
            label="permit group",
            identity=permit_group_id,
        )
        _require_projection(
            row,
            parsed,
            (
                "permit_group_id",
                "budget_set_snapshot_id",
                "run_id",
                "lease_id",
                "fencing_token",
                "status",
                "revision",
                "acquired_at",
                "expires_at",
            ),
            label="permit group",
            identity=permit_group_id,
        )
        ids = tuple(
            self._session.scalars(
                select(ConcurrencyPermitRow.permit_id)
                .where(ConcurrencyPermitRow.permit_group_id == permit_group_id)
                .order_by(ConcurrencyPermitRow.permit_id)
            ).all()
        )
        if ids != parsed.permit_ids:
            raise IntegrityViolation(
                "permit member rows differ from the group payload",
                permit_group_id=permit_group_id,
            )
        return parsed

    def list_concurrency_permits(
        self,
        permit_group_id: str,
    ) -> tuple[ConcurrencyPermitV1, ...]:
        rows = self._session.scalars(
            select(ConcurrencyPermitRow)
            .where(ConcurrencyPermitRow.permit_group_id == permit_group_id)
            .order_by(ConcurrencyPermitRow.permit_id)
        ).all()
        return tuple(self._parse_concurrency_permit_row(row) for row in rows)

    def put_model_catalog(self, catalog: ModelCatalogSnapshotV1) -> ModelCatalogSnapshotV1:
        canonical = _canonical_model(
            catalog,
            ModelCatalogSnapshotV1,
            label="model catalog",
        )
        row = self._session.get(ModelCatalogSnapshotRow, canonical.catalog_version)
        if row is not None:
            existing = self.get_model_catalog(canonical.catalog_version, row.catalog_digest)
            if existing is None or not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "model catalog version has different immutable content",
                    catalog_version=canonical.catalog_version,
                )
            return existing
        wire = canonical.model_dump(mode="json")
        self._session.add(
            ModelCatalogSnapshotRow(
                catalog_version=canonical.catalog_version,
                catalog_digest=canonical.catalog_digest,
                created_at=wire["created_at"],
                payload=wire,
            )
        )
        self._flush("model catalog", catalog_version=canonical.catalog_version)
        return canonical

    def get_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None:
        row = self._session.get(ModelCatalogSnapshotRow, catalog_version)
        if row is None:
            return None
        if row.catalog_digest != catalog_digest:
            raise IntegrityViolation(
                "retained model catalog digest differs from requested exact ref",
                catalog_version=catalog_version,
            )
        parsed = _parse_payload(
            row.payload,
            ModelCatalogSnapshotV1,
            label="model catalog",
            identity=str(catalog_version),
        )
        _require_projection(
            row,
            parsed,
            ("catalog_version", "catalog_digest", "created_at"),
            label="model catalog",
            identity=str(catalog_version),
        )
        return parsed

    def get_model_catalogs_many(
        self,
        exact_refs: Sequence[tuple[int, str]],
    ) -> dict[tuple[int, str], ModelCatalogSnapshotV1 | None]:
        """Read exact model-catalog refs with bounded set statements.

        Catalog versions are the storage primary key while the digest is part of
        the immutable exact ref.  Returning a mapping keyed by both fields lets a
        terminal cost preflight reject a same-version/different-digest drift
        without issuing one lookup per stranded model call.
        """

        selected = tuple(dict.fromkeys(exact_refs))
        if any(
            isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or not isinstance(digest, str)
            or not digest
            for version, digest in selected
        ):
            raise ValueError("model catalog exact refs are invalid")
        retained: dict[tuple[int, str], ModelCatalogSnapshotV1 | None] = dict.fromkeys(selected)
        expected_by_version: dict[int, str] = {}
        for version, digest in selected:
            previous = expected_by_version.setdefault(version, digest)
            if previous != digest:
                raise IntegrityViolation(
                    "model catalog version is requested with conflicting digests",
                    catalog_version=version,
                )
        versions = tuple(expected_by_version)
        for offset in range(0, len(versions), 900):
            rows = self._session.scalars(
                select(ModelCatalogSnapshotRow).where(
                    ModelCatalogSnapshotRow.catalog_version.in_(versions[offset : offset + 900])
                )
            ).all()
            for row in rows:
                digest = expected_by_version[row.catalog_version]
                if row.catalog_digest != digest:
                    raise IntegrityViolation(
                        "retained model catalog digest differs from requested exact ref",
                        catalog_version=row.catalog_version,
                    )
                parsed = _parse_payload(
                    row.payload,
                    ModelCatalogSnapshotV1,
                    label="model catalog snapshot",
                    identity=str(row.catalog_version),
                )
                _require_projection(
                    row,
                    parsed,
                    ("catalog_version", "catalog_digest", "created_at"),
                    label="model catalog snapshot",
                    identity=str(row.catalog_version),
                )
                retained[(row.catalog_version, row.catalog_digest)] = parsed
        return retained

    def put_routing_policy(self, policy: RoutingPolicyV1) -> RoutingPolicyV1:
        canonical = _canonical_model(policy, RoutingPolicyV1, label="routing policy")
        row = self._session.get(RoutingPolicyRow, canonical.policy_version)
        if row is not None:
            existing = self.get_routing_policy(canonical.policy_version, row.routing_policy_digest)
            if existing is None or not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "routing policy version has different immutable content",
                    policy_version=canonical.policy_version,
                )
            return existing
        catalog = self.get_model_catalog(canonical.catalog_version, canonical.catalog_digest)
        if catalog is None:
            raise IntegrityViolation(
                "routing policy references unavailable catalog history",
                policy_version=canonical.policy_version,
            )
        try:
            validate_policy_catalog_closure(canonical, catalog)
        except ValueError as exc:
            raise IntegrityViolation("routing policy catalog closure is invalid") from exc
        self._session.add(
            RoutingPolicyRow(
                policy_version=canonical.policy_version,
                routing_policy_digest=canonical.routing_policy_digest,
                catalog_version=canonical.catalog_version,
                catalog_digest=canonical.catalog_digest,
                payload=canonical.model_dump(mode="json"),
            )
        )
        self._flush("routing policy", policy_version=canonical.policy_version)
        return canonical

    def get_routing_policy(
        self,
        policy_version: int,
        routing_policy_digest: str,
    ) -> RoutingPolicyV1 | None:
        row = self._session.get(RoutingPolicyRow, policy_version)
        if row is None:
            return None
        if row.routing_policy_digest != routing_policy_digest:
            raise IntegrityViolation(
                "retained routing policy digest differs from requested exact ref",
                policy_version=policy_version,
            )
        parsed = _parse_payload(
            row.payload,
            RoutingPolicyV1,
            label="routing policy",
            identity=str(policy_version),
        )
        _require_projection(
            row,
            parsed,
            (
                "policy_version",
                "routing_policy_digest",
                "catalog_version",
                "catalog_digest",
            ),
            label="routing policy",
            identity=str(policy_version),
        )
        return parsed

    def put_routing_decision(self, decision: RoutingDecisionV1) -> RoutingDecisionV1:
        canonical = _canonical_model(decision, RoutingDecisionV1, label="routing decision")
        existing = self.get_routing_decision(canonical.decision_id)
        if existing is not None:
            if not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "routing decision id has different append-only content",
                    decision_id=canonical.decision_id,
                )
            return existing
        budget_set = self.get_budget_set(canonical.budget_set_snapshot_id)
        if budget_set is None or budget_set.run_id != canonical.run_id:
            raise IntegrityViolation(
                "routing decision has no matching budget-set snapshot",
                decision_id=canonical.decision_id,
            )
        policy = self.get_routing_policy(
            canonical.policy_version,
            canonical.routing_policy_digest,
        )
        catalog = self.get_model_catalog(canonical.catalog_version, canonical.catalog_digest)
        if policy is None or catalog is None:
            raise IntegrityViolation(
                "routing decision exact policy or catalog history is unavailable",
                decision_id=canonical.decision_id,
            )
        self._validate_decision_closure(canonical, policy, catalog)
        wire = canonical.model_dump(mode="json")
        self._session.add(
            RoutingDecisionRow(
                decision_id=canonical.decision_id,
                run_id=canonical.run_id,
                attempt_no=canonical.attempt_no,
                request_hash=canonical.request_hash,
                rule_id=canonical.rule_id,
                model_snapshot=canonical.model_snapshot,
                tier=canonical.tier,
                budget_set_snapshot_id=canonical.budget_set_snapshot_id,
                fallback_index=canonical.fallback_index,
                policy_version=canonical.policy_version,
                routing_policy_digest=canonical.routing_policy_digest,
                catalog_version=canonical.catalog_version,
                catalog_digest=canonical.catalog_digest,
                execution_source=canonical.execution_source,
                decided_at=wire["decided_at"],
                payload=wire,
            )
        )
        self._flush("routing decision", decision_id=canonical.decision_id)
        return canonical

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None:
        row = self._session.get(RoutingDecisionRow, decision_id)
        if row is None:
            return None
        parsed = _parse_payload(
            row.payload,
            RoutingDecisionV1,
            label="routing decision",
            identity=decision_id,
        )
        _require_projection(
            row,
            parsed,
            (
                "decision_id",
                "run_id",
                "attempt_no",
                "request_hash",
                "rule_id",
                "model_snapshot",
                "tier",
                "budget_set_snapshot_id",
                "fallback_index",
                "policy_version",
                "routing_policy_digest",
                "catalog_version",
                "catalog_digest",
                "execution_source",
                "decided_at",
            ),
            label="routing decision",
            identity=decision_id,
        )
        return parsed

    def get_routing_decisions_many(
        self,
        decision_ids: Sequence[str],
    ) -> dict[str, RoutingDecisionV1 | None]:
        """Read an exact native routing-decision set with bounded statements."""

        selected = tuple(dict.fromkeys(decision_ids))
        if any(not isinstance(decision_id, str) or not decision_id for decision_id in selected):
            raise ValueError("routing decision ids must be non-empty strings")
        retained: dict[str, RoutingDecisionV1 | None] = dict.fromkeys(selected)
        for offset in range(0, len(selected), 900):
            rows = self._session.scalars(
                select(RoutingDecisionRow).where(
                    RoutingDecisionRow.decision_id.in_(selected[offset : offset + 900])
                )
            ).all()
            for row in rows:
                parsed = _parse_payload(
                    row.payload,
                    RoutingDecisionV1,
                    label="routing decision",
                    identity=row.decision_id,
                )
                _require_projection(
                    row,
                    parsed,
                    (
                        "decision_id",
                        "run_id",
                        "attempt_no",
                        "request_hash",
                        "rule_id",
                        "model_snapshot",
                        "tier",
                        "budget_set_snapshot_id",
                        "fallback_index",
                        "policy_version",
                        "routing_policy_digest",
                        "catalog_version",
                        "catalog_digest",
                        "execution_source",
                        "decided_at",
                    ),
                    label="routing decision",
                    identity=row.decision_id,
                )
                retained[row.decision_id] = parsed
        return retained

    def put_legacy_import_routing_decision(
        self,
        decision: LegacyImportRoutingDecisionV1,
    ) -> LegacyImportRoutingDecisionV1:
        canonical = _canonical_model(
            decision,
            LegacyImportRoutingDecisionV1,
            label="legacy import routing decision",
        )
        existing = self.get_legacy_import_routing_decision(canonical.decision_id)
        if existing is not None:
            if not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "legacy route id has different append-only content",
                    decision_id=canonical.decision_id,
                )
            return existing
        catalog = self.get_model_catalog(
            canonical.model_catalog_version,
            canonical.model_catalog_digest,
        )
        if catalog is None or canonical.model_snapshot not in {
            item.model_snapshot for item in catalog.models
        }:
            raise IntegrityViolation(
                "legacy route exact catalog/model history is unavailable",
                decision_id=canonical.decision_id,
            )
        self._session.add(
            LegacyImportRoutingDecisionRow(
                decision_id=canonical.decision_id,
                source_wire_sha256=canonical.source_wire_sha256,
                request_hash=canonical.request_hash,
                model_snapshot=canonical.model_snapshot,
                catalog_version=canonical.model_catalog_version,
                catalog_digest=canonical.model_catalog_digest,
                payload=canonical.model_dump(mode="json"),
            )
        )
        self._flush("legacy import routing decision", decision_id=canonical.decision_id)
        return canonical

    def get_legacy_import_routing_decision(
        self,
        decision_id: str,
    ) -> LegacyImportRoutingDecisionV1 | None:
        row = self._session.get(LegacyImportRoutingDecisionRow, decision_id)
        if row is None:
            return None
        parsed = _parse_payload(
            row.payload,
            LegacyImportRoutingDecisionV1,
            label="legacy import routing decision",
            identity=decision_id,
        )
        projections = {
            "decision_id": parsed.decision_id,
            "source_wire_sha256": parsed.source_wire_sha256,
            "request_hash": parsed.request_hash,
            "model_snapshot": parsed.model_snapshot,
            "catalog_version": parsed.model_catalog_version,
            "catalog_digest": parsed.model_catalog_digest,
        }
        for field_name, expected in projections.items():
            if getattr(row, field_name) != expected:
                raise IntegrityViolation(
                    "stored legacy route projection differs from its payload",
                    decision_id=decision_id,
                    field=field_name,
                )
        return parsed

    def get_legacy_import_routing_decisions_many(
        self,
        decision_ids: Sequence[str],
    ) -> dict[str, LegacyImportRoutingDecisionV1 | None]:
        """Read an exact legacy routing-decision set with bounded statements."""

        selected = tuple(dict.fromkeys(decision_ids))
        if any(not isinstance(decision_id, str) or not decision_id for decision_id in selected):
            raise ValueError("legacy routing decision ids must be non-empty strings")
        retained: dict[str, LegacyImportRoutingDecisionV1 | None] = dict.fromkeys(selected)
        for offset in range(0, len(selected), 900):
            rows = self._session.scalars(
                select(LegacyImportRoutingDecisionRow).where(
                    LegacyImportRoutingDecisionRow.decision_id.in_(selected[offset : offset + 900])
                )
            ).all()
            for row in rows:
                parsed = _parse_payload(
                    row.payload,
                    LegacyImportRoutingDecisionV1,
                    label="legacy import routing decision",
                    identity=row.decision_id,
                )
                projections = {
                    "decision_id": parsed.decision_id,
                    "source_wire_sha256": parsed.source_wire_sha256,
                    "request_hash": parsed.request_hash,
                    "model_snapshot": parsed.model_snapshot,
                    "catalog_version": parsed.model_catalog_version,
                    "catalog_digest": parsed.model_catalog_digest,
                }
                for field_name, expected in projections.items():
                    if getattr(row, field_name) != expected:
                        raise IntegrityViolation(
                            "stored legacy route projection differs from its payload",
                            decision_id=row.decision_id,
                            field=field_name,
                        )
                retained[row.decision_id] = parsed
        return retained

    def list_routing_decisions(
        self,
        *,
        run_id: str,
        attempt_no: int | None = None,
        limit: int = 100,
        after: tuple[str, str] | None = None,
    ) -> tuple[RoutingDecisionV1, ...]:
        limit = _validate_limit(limit)
        statement = select(RoutingDecisionRow).where(RoutingDecisionRow.run_id == run_id)
        if attempt_no is not None:
            statement = statement.where(RoutingDecisionRow.attempt_no == attempt_no)
        if after is not None:
            decided_at, decision_id = after
            statement = statement.where(
                (RoutingDecisionRow.decided_at > decided_at)
                | (
                    (RoutingDecisionRow.decided_at == decided_at)
                    & (RoutingDecisionRow.decision_id > decision_id)
                )
            )
        rows = self._session.scalars(
            statement.order_by(
                RoutingDecisionRow.decided_at,
                RoutingDecisionRow.decision_id,
            ).limit(limit)
        ).all()
        return tuple(self.get_routing_decision(row.decision_id) for row in rows)  # type: ignore[return-value]

    def _validate_usage_routing(self, usage: UsageEntryV1) -> None:
        if usage.routing_decision_kind == "native":
            decision = self.get_routing_decision(usage.routing_decision_id or "")
            if (
                decision is None
                or decision.run_id != usage.run_id
                or decision.attempt_no != usage.attempt_no
                or decision.request_hash != usage.request_hash
                or decision.execution_source != usage.execution_source
            ):
                raise IntegrityViolation(
                    "usage does not resolve its exact native routing decision",
                    usage_id=usage.usage_id,
                )
        elif usage.routing_decision_kind == "legacy_import":
            decision = self.get_legacy_import_routing_decision(usage.routing_decision_id or "")
            if (
                decision is None
                or decision.request_hash != usage.request_hash
                or usage.execution_source != "cassette_replay"
            ):
                raise IntegrityViolation(
                    "usage does not resolve its exact legacy routing decision",
                    usage_id=usage.usage_id,
                )

    @staticmethod
    def _validate_decision_closure(
        decision: RoutingDecisionV1,
        policy: RoutingPolicyV1,
        catalog: ModelCatalogSnapshotV1,
    ) -> None:
        if (
            decision.catalog_version != policy.catalog_version
            or decision.catalog_digest != policy.catalog_digest
        ):
            raise IntegrityViolation("routing decision policy/catalog exact refs differ")
        rule = next((item for item in policy.rules if item.rule_id == decision.rule_id), None)
        if rule is None:
            raise IntegrityViolation("routing decision references an unknown policy rule")
        chain = (rule.primary_model_snapshot, *rule.allowed_fallback_chain)
        if decision.fallback_index >= len(chain):
            raise IntegrityViolation("routing decision fallback index is outside the policy chain")
        expected_model = chain[decision.fallback_index]
        expected_source = (
            None if decision.fallback_index == 0 else chain[decision.fallback_index - 1]
        )
        if decision.model_snapshot != expected_model or decision.fallback_from != expected_source:
            raise IntegrityViolation(
                "routing decision model is outside the explicit fallback chain"
            )
        descriptor = next(
            (item for item in catalog.models if item.model_snapshot == decision.model_snapshot),
            None,
        )
        if descriptor is None or descriptor.tier != decision.tier:
            raise IntegrityViolation("routing decision model/tier differs from exact catalog")

    def get_usage(self, usage_id: str) -> UsageEntryV1 | None:
        if not isinstance(usage_id, str) or not usage_id:
            raise IntegrityViolation("usage lookup identity must be non-empty")
        row = self._session.get(UsageEntryRow, usage_id)
        return None if row is None else self._parse_usage_row(row)

    def _get_usage(self, usage_id: str) -> UsageEntryV1 | None:
        return self.get_usage(usage_id)

    @staticmethod
    def usage_identity(usage: UsageEntryV1) -> str:
        """Return the authoritative idempotency identity for one usage row."""

        return _usage_identity(usage)

    @staticmethod
    def _parse_budget_snapshot_row(row: BudgetSnapshotRow) -> BudgetSnapshotV1:
        parsed = _parse_payload(
            row.payload,
            BudgetSnapshotV1,
            label="budget snapshot",
            identity=row.snapshot_id,
        )
        _require_projection(
            row,
            parsed,
            (
                "snapshot_id",
                "budget_id",
                "scope_kind",
                "scope_id",
                "budget_revision_at_freeze",
            ),
            label="budget snapshot",
            identity=row.snapshot_id,
        )
        return parsed

    @staticmethod
    def _parse_reservation_group_row(row: ReservationGroupRow) -> ReservationGroupV1:
        parsed = _parse_payload(
            row.payload,
            ReservationGroupV1,
            label="reservation group",
            identity=row.reservation_group_id,
        )
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
            identity=row.reservation_group_id,
        )
        return parsed

    @staticmethod
    def _parse_budget_reservation_row(row: BudgetReservationRow) -> BudgetReservationV1:
        parsed = _parse_payload(
            row.payload,
            BudgetReservationV1,
            label="budget reservation",
            identity=row.reservation_id,
        )
        _require_projection(
            row,
            parsed,
            ("reservation_id", "reservation_group_id", "budget_id", "status", "revision"),
            label="budget reservation",
            identity=row.reservation_id,
        )
        return parsed

    @staticmethod
    def _parse_concurrency_permit_row(row: ConcurrencyPermitRow) -> ConcurrencyPermitV1:
        parsed = _parse_payload(
            row.payload,
            ConcurrencyPermitV1,
            label="concurrency permit",
            identity=row.permit_id,
        )
        _require_projection(
            row,
            parsed,
            (
                "permit_id",
                "permit_group_id",
                "budget_id",
                "run_id",
                "lease_id",
                "fencing_token",
                "status",
                "revision",
                "acquired_at",
                "expires_at",
            ),
            label="concurrency permit",
            identity=row.permit_id,
        )
        return parsed

    @staticmethod
    def _parse_usage_row(row: UsageEntryRow) -> UsageEntryV1:
        parsed = _parse_payload(
            row.payload,
            UsageEntryV1,
            label="usage entry",
            identity=row.usage_id,
        )
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
            identity=row.usage_id,
        )
        expected_native = (
            parsed.routing_decision_id if parsed.routing_decision_kind == "native" else None
        )
        expected_legacy = (
            parsed.routing_decision_id if parsed.routing_decision_kind == "legacy_import" else None
        )
        if (
            row.native_routing_decision_id != expected_native
            or row.legacy_routing_decision_id != expected_legacy
            or row.usage_identity != _usage_identity(parsed)
        ):
            raise IntegrityViolation(
                "stored usage routing/idempotency projection differs from payload",
                usage_id=row.usage_id,
            )
        return parsed

    def _flush(self, label: str, **context: Any) -> None:
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise IntegrityViolation(f"{label} could not be persisted", **context) from exc


__all__ = ["MAX_LEDGER_QUERY_ITEMS", "SqlCostRepository"]
