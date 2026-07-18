"""Run lifecycle adapters for the persistent CostLedger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cost import (
    BudgetReservationV1,
    BudgetSetSnapshotV1,
    ConcurrencyPermitV1,
    PermitGroupV1,
    ReservationGroupV1,
    UsageEntryV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import RetryDecisionV1, RunAttempt, RunLease, RunRecord
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import UtcClock
from gameforge.platform.runs.lifecycle import PermitGroupBinding
from gameforge.runtime.cost.ledger import PreflightedTerminalCostClosure, SqlCostLedger


@dataclass(frozen=True, slots=True)
class RunBudgetPlan:
    budget_set: BudgetSetSnapshotV1
    hold_group: ReservationGroupV1
    reservations: tuple[BudgetReservationV1, ...]


class RunBudgetPlanProvider(Protocol):
    """Resolve versioned business policy without moving policy into the ledger."""

    def resolve_run_budget(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        request_hash: str,
        initiated_by: AuditActor,
    ) -> RunBudgetPlan: ...


class AttemptConservativeUsageProvider(Protocol):
    """Build an auditable upper-bound observation for one stranded group."""

    def conservative_usage(
        self,
        *,
        group: ReservationGroupV1,
        reservations: tuple[BudgetReservationV1, ...],
        recorded_at: datetime,
    ) -> UsageEntryV1: ...

    def conservative_usage_many(
        self,
        *,
        groups: tuple[
            tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]],
            ...,
        ],
        recorded_at: datetime,
    ) -> tuple[UsageEntryV1, ...]: ...


def _utc(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrityViolation(f"{field} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise IntegrityViolation(f"{field} must be a UTC timestamp")
    return parsed


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    return f"{prefix}:sha256:{canonical_sha256(payload)}"


class SqlRunCostAccounting:
    """One transaction-bound implementation of both M4a Run cost seams."""

    def __init__(
        self,
        *,
        ledger: SqlCostLedger,
        plan_provider: RunBudgetPlanProvider,
        settlement_provider: AttemptConservativeUsageProvider,
        clock: UtcClock,
    ) -> None:
        self._ledger = ledger
        self._plan_provider = plan_provider
        self._settlement_provider = settlement_provider
        self._clock = clock

    def reserve_run_budget(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        request_hash: str,
        initiated_by: AuditActor,
    ) -> str:
        plan = self._plan_provider.resolve_run_budget(
            run_id=run_id,
            budget_set_snapshot_id=budget_set_snapshot_id,
            request_hash=request_hash,
            initiated_by=initiated_by,
        )
        if (
            plan.budget_set.run_id != run_id
            or plan.budget_set.budget_set_snapshot_id != budget_set_snapshot_id
            or plan.hold_group.run_id != run_id
            or plan.hold_group.budget_set_snapshot_id != budget_set_snapshot_id
            or plan.hold_group.request_hash.removeprefix("sha256:") != request_hash
        ):
            raise IntegrityViolation("Run budget plan differs from the admission request")
        retained = self._ledger.freeze_budget_set(
            plan.budget_set,
            plan.hold_group,
            plan.reservations,
        )
        return retained.reservation_group_id

    def acquire_execution_permits(
        self,
        *,
        run: RunRecord,
        attempt_no: int,
        fencing_token: int,
        worker_principal_id: str,
        lease_id: str,
        expires_at: str,
    ) -> str:
        if (
            run.payload.budget_set_snapshot_id != run.budget_set_snapshot_id
            or run.next_attempt_no != attempt_no
            or run.next_fencing_token != fencing_token
            or run.current_attempt_no is not None
            or run.concurrency_permit_group_id is not None
            or run.status not in {"queued", "retry_wait"}
        ):
            raise IntegrityViolation("Run is not at the exact permit-acquisition boundary")
        budget_set = self._ledger.get_budget_set(run.budget_set_snapshot_id)
        hold = self._ledger.get_reservation_group(run.run_budget_hold_group_id)
        if (
            budget_set is None
            or hold is None
            or budget_set.run_id != run.run_id
            or hold.run_id != run.run_id
            or hold.budget_set_snapshot_id != run.budget_set_snapshot_id
            or hold.status != "reserved"
        ):
            raise IntegrityViolation("Run permit acquisition differs from CostLedger authority")
        acquired_at = self._clock.now_utc()
        expiry = _utc(expires_at, field="permit expires_at")
        identity = {
            "run_id": run.run_id,
            "attempt_no": attempt_no,
            "lease_id": lease_id,
            "fencing_token": fencing_token,
            "worker_principal_id": worker_principal_id,
        }
        group_id = _stable_id("permit-group", identity)
        budget_ids = tuple(
            snapshot.budget_id
            for snapshot in budget_set.snapshots
            if any(item.dimension == "concurrent_run" for item in snapshot.limits)
        )
        permit_ids = tuple(
            _stable_id("permit", {**identity, "budget_id": budget_id}) for budget_id in budget_ids
        )
        group = PermitGroupV1(
            permit_group_id=group_id,
            budget_set_snapshot_id=budget_set.budget_set_snapshot_id,
            run_id=run.run_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
            permit_ids=permit_ids,
            status="active",
            revision=1,
            acquired_at=acquired_at,
            expires_at=expiry,
        )
        permits = tuple(
            ConcurrencyPermitV1(
                permit_id=permit_id,
                permit_group_id=group_id,
                budget_id=budget_id,
                run_id=run.run_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                status="active",
                revision=1,
                acquired_at=acquired_at,
                expires_at=expiry,
            )
            for budget_id, permit_id in zip(budget_ids, permit_ids, strict=True)
        )
        return self._ledger.acquire_permit_group(group, permits).permit_group_id

    def renew_execution_permits(
        self,
        *,
        permit_group_id: str,
        expected_revision: int,
        lease_id: str,
        fencing_token: int,
        expires_at: str,
    ) -> PermitGroupBinding:
        current = self._ledger.get_permit_group(permit_group_id)
        if (
            current is None
            or current.revision != expected_revision
            or current.lease_id != lease_id
            or current.fencing_token != fencing_token
        ):
            raise IntegrityViolation("permit renewal fence differs from CostLedger authority")
        desired = current.model_copy(
            update={
                "revision": current.revision + 1,
                "expires_at": _utc(expires_at, field="permit renewal expires_at"),
            }
        )
        renewed = self._ledger.renew_permit_group(desired)
        return PermitGroupBinding(
            permit_group_id=renewed.permit_group_id,
            revision=renewed.revision,
        )

    def retry_budget_available(self, *, run: RunRecord) -> bool:
        self._validate_run_cost_binding(run)
        return self._ledger.retry_budget_available(
            run_id=run.run_id,
            budget_set_snapshot_id=run.budget_set_snapshot_id,
            hold_group_id=run.run_budget_hold_group_id,
        )

    def release_attempt(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        retry_decision: RetryDecisionV1 | None,
    ) -> None:
        closure = self.preflight_terminal_closure(
            run=run,
            attempt=attempt,
            lease=lease,
            retry_decision=retry_decision,
            terminal_status=None,
        )
        self.apply_preflighted_terminal_closure(closure)

    def close_run(
        self,
        *,
        run: RunRecord,
        terminal_status: Literal["succeeded", "failed", "cancelled", "timed_out"],
    ) -> None:
        closure = self.preflight_terminal_closure(
            run=run,
            attempt=None,
            lease=None,
            retry_decision=None,
            terminal_status=terminal_status,
        )
        self.apply_preflighted_terminal_closure(closure)

    def preflight_terminal_closure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        lease: RunLease | None,
        retry_decision: RetryDecisionV1 | None,
        terminal_status: Literal["succeeded", "failed", "cancelled", "timed_out"] | None,
    ) -> PreflightedTerminalCostClosure:
        """Preflight attempt release plus optional Run closure as one write plan."""

        del retry_decision
        self._validate_run_cost_binding_shape(run)
        if (attempt is None) != (lease is None):
            raise IntegrityViolation("terminal attempt cost identity is incomplete")
        if attempt is None and terminal_status is None:
            raise IntegrityViolation("terminal cost closure has no requested transition")
        if attempt is not None and lease is not None:
            if (
                run.concurrency_permit_group_id is None
                or getattr(attempt, "run_id", run.run_id) != run.run_id
                or getattr(lease, "run_id", run.run_id) != run.run_id
                or lease.fencing_token != attempt.fencing_token
                or lease.attempt_no != attempt.attempt_no
            ):
                raise IntegrityViolation("attempt release differs from its Run/lease authority")

        def conservative_usage_factory(
            groups: tuple[
                tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]],
                ...,
            ],
            recorded_at: datetime,
        ) -> tuple[UsageEntryV1, ...]:
            return self._settlement_provider.conservative_usage_many(
                groups=groups,
                recorded_at=recorded_at,
            )

        return self._ledger.preflight_terminal_closure(
            run_id=run.run_id,
            budget_set_snapshot_id=run.budget_set_snapshot_id,
            hold_group_id=run.run_budget_hold_group_id,
            attempt_no=None if attempt is None else attempt.attempt_no,
            permit_group_id=(None if attempt is None else run.concurrency_permit_group_id),
            lease_id=None if lease is None else lease.lease_id,
            fencing_token=None if attempt is None else attempt.fencing_token,
            lease_status=None if lease is None else lease.status,
            close_hold=terminal_status is not None,
            recorded_at=self._clock.now_utc(),
            conservative_usage_factory=conservative_usage_factory,
        )

    def apply_preflighted_terminal_closure(
        self,
        closure: PreflightedTerminalCostClosure,
    ) -> None:
        self._ledger.apply_preflighted_terminal_closure(closure)

    def _validate_run_cost_binding(self, run: RunRecord) -> None:
        self._validate_run_cost_binding_shape(run)
        budget_set = self._ledger.get_budget_set(run.budget_set_snapshot_id)
        hold = self._ledger.get_reservation_group(run.run_budget_hold_group_id)
        if (
            budget_set is None
            or hold is None
            or budget_set.run_id != run.run_id
            or hold.run_id != run.run_id
            or hold.budget_set_snapshot_id != run.budget_set_snapshot_id
        ):
            raise IntegrityViolation("Run and CostLedger budget bindings differ")

    @staticmethod
    def _validate_run_cost_binding_shape(run: RunRecord) -> None:
        if run.payload.budget_set_snapshot_id != run.budget_set_snapshot_id:
            raise IntegrityViolation("Run payload and record budget-set bindings differ")


__all__ = [
    "AttemptConservativeUsageProvider",
    "RunBudgetPlan",
    "RunBudgetPlanProvider",
    "SqlRunCostAccounting",
]
