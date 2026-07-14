from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.cost import (
    BudgetReservationV1,
    BudgetSetSnapshotV1,
    BudgetSnapshotV1,
    BudgetV1,
    CacheHitObservationV1,
    ConcurrencyPermitV1,
    CostAmountV1,
    LatencyObservationV1,
    MonetaryObservationV1,
    PermitGroupV1,
    ReservationGroupV1,
    TokenUsageObservationV1,
    UsageEntryV1,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence.models import RunAttemptRow, RunLeaseRow, RunRow
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
REQUEST_HASH = "sha256:" + "a" * 64


def amount(dimension: str, value: int | Decimal) -> CostAmountV1:
    units = {
        "input_token": "token",
        "output_token": "token",
        "cache_read_token": "token",
        "cache_write_token": "token",
        "request": "request",
        "agent_step": "step",
        "wall_time_ns": "ns",
        "concurrent_run": "count",
        "monetary": "currency",
    }
    kwargs: dict[str, object] = {
        "dimension": dimension,
        "value": Decimal(value),
        "unit": units[dimension],
    }
    if dimension == "monetary":
        kwargs["currency"] = "USD"
    return CostAmountV1(**kwargs)


def amounts_by_dimension(values: Sequence[CostAmountV1]) -> dict[str, Decimal]:
    return {item.dimension: item.value for item in values}


def budget(
    scope_kind: str,
    scope_id: str,
    *,
    suffix: str | None = None,
    reserved_input: int = 0,
    concurrent_limit: int = 1,
) -> BudgetV1:
    selected_suffix = suffix or scope_kind
    reserved = () if reserved_input == 0 else (amount("input_token", reserved_input),)
    return BudgetV1(
        budget_id=f"budget:{selected_suffix}",
        scope_kind=scope_kind,
        scope_id=scope_id,
        policy_version="budget-policy@1",
        limits=(
            amount("input_token", 100),
            amount("agent_step", 10),
            amount("concurrent_run", concurrent_limit),
        ),
        reserved=reserved,
        consumed=(),
        status="active",
        revision=1,
        deadline_utc=NOW + timedelta(hours=2),
        created_at=NOW - timedelta(minutes=1),
    )


def budget_set(
    run_id: str,
    budgets: Sequence[BudgetV1],
    *,
    suffix: str = "1",
) -> BudgetSetSnapshotV1:
    snapshots = tuple(
        BudgetSnapshotV1(
            snapshot_id=f"snapshot:{run_id}:{item.budget_id}",
            budget_id=item.budget_id,
            scope_kind=item.scope_kind,
            scope_id=item.scope_id,
            policy_version=item.policy_version,
            budget_revision_at_freeze=item.revision,
            limits=item.limits,
            reserved=item.reserved,
            consumed=item.consumed,
            captured_at=NOW,
        )
        for item in budgets
    )
    return BudgetSetSnapshotV1(
        budget_set_snapshot_id=f"budget-set:{run_id}:{suffix}",
        run_id=run_id,
        selection_policy_version="selection-policy@1",
        snapshots=snapshots,
        captured_at=NOW,
    )


def hold(
    selected_set: BudgetSetSnapshotV1,
    *,
    input_tokens: int = 80,
    agent_steps: int = 8,
    suffix: str = "1",
) -> tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]]:
    group_id = f"hold:{selected_set.run_id}:{suffix}"
    reservations = tuple(
        BudgetReservationV1(
            reservation_id=f"reservation:{group_id}:{snapshot.budget_id}",
            reservation_group_id=group_id,
            budget_id=snapshot.budget_id,
            reserved=(
                amount("input_token", input_tokens),
                amount("agent_step", agent_steps),
            ),
            status="reserved",
            revision=1,
        )
        for snapshot in selected_set.snapshots
    )
    group = ReservationGroupV1(
        reservation_group_id=group_id,
        scope="run_budget_hold",
        run_id=selected_set.run_id,
        budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
        request_hash=REQUEST_HASH,
        idempotency_key=f"hold-idempotency:{selected_set.run_id}:{suffix}",
        budget_reservation_ids=tuple(item.reservation_id for item in reservations),
        status="reserved",
        revision=1,
        created_at=NOW,
    )
    return group, reservations


def step_group(
    selected_set: BudgetSetSnapshotV1,
    parent: ReservationGroupV1,
    *,
    suffix: str,
    input_tokens: int = 30,
    attempt_no: int = 1,
    fencing_token: int = 1,
) -> tuple[ReservationGroupV1, tuple[BudgetReservationV1, ...]]:
    group_id = f"step:{selected_set.run_id}:{suffix}"
    reservations = tuple(
        BudgetReservationV1(
            reservation_id=f"reservation:{group_id}:{snapshot.budget_id}",
            reservation_group_id=group_id,
            budget_id=snapshot.budget_id,
            reserved=(amount("input_token", input_tokens), amount("agent_step", 1)),
            status="reserved",
            revision=1,
        )
        for snapshot in selected_set.snapshots
    )
    group = ReservationGroupV1(
        reservation_group_id=group_id,
        scope="agent_step",
        run_id=selected_set.run_id,
        budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
        parent_hold_group_id=parent.reservation_group_id,
        attempt_no=attempt_no,
        request_hash="sha256:" + suffix[-1] * 64,
        fencing_token=fencing_token,
        idempotency_key=f"step-idempotency:{selected_set.run_id}:{suffix}",
        budget_reservation_ids=tuple(item.reservation_id for item in reservations),
        status="reserved",
        revision=1,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )
    return group, reservations


def usage(
    group: ReservationGroupV1,
    *,
    usage_id: str,
    input_tokens: int | None,
    adjustment_of_usage_id: str | None = None,
    recorded_at: datetime = NOW + timedelta(seconds=1),
) -> UsageEntryV1:
    token_usage = (
        TokenUsageObservationV1(status="unavailable")
        if input_tokens is None
        else TokenUsageObservationV1(
            status="reported",
            input_tokens=input_tokens,
            total_tokens=input_tokens,
        )
    )
    return UsageEntryV1(
        usage_id=usage_id,
        reservation_group_id=group.reservation_group_id,
        budget_reservation_ids=group.budget_reservation_ids,
        scope="agent_step",
        run_id=group.run_id,
        attempt_no=group.attempt_no or 1,
        request_hash=group.request_hash,
        execution_source="online",
        provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
        retry_index=0,
        token_usage=token_usage,
        latency=LatencyObservationV1(status="unavailable"),
        wall_time_ns=1_000,
        monetary=MonetaryObservationV1(status="unavailable"),
        fencing_token_at_reserve=group.fencing_token or 1,
        adjustment_of_usage_id=adjustment_of_usage_id,
        recorded_at=recorded_at,
    )


def permit_group(
    selected_set: BudgetSetSnapshotV1,
    *,
    lease_id: str,
    fencing_token: int,
    suffix: str,
    acquired_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(minutes=5),
) -> tuple[PermitGroupV1, tuple[ConcurrencyPermitV1, ...]]:
    group_id = f"permit-group:{selected_set.run_id}:{suffix}"
    budget_ids = tuple(
        snapshot.budget_id
        for snapshot in selected_set.snapshots
        if any(item.dimension == "concurrent_run" for item in snapshot.limits)
    )
    permits = tuple(
        ConcurrencyPermitV1(
            permit_id=f"permit:{group_id}:{budget_id}",
            permit_group_id=group_id,
            budget_id=budget_id,
            run_id=selected_set.run_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
            status="active",
            revision=1,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )
        for budget_id in budget_ids
    )
    group = PermitGroupV1(
        permit_group_id=group_id,
        budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
        run_id=selected_set.run_id,
        lease_id=lease_id,
        fencing_token=fencing_token,
        permit_ids=tuple(item.permit_id for item in permits),
        status="active",
        revision=1,
        acquired_at=acquired_at,
        expires_at=expires_at,
    )
    return group, permits


def capabilities(
    session: Session,
    *,
    clock: FrozenUtcClock | None = None,
) -> TransactionCapabilities:
    ledger = SqlCostLedger(session, clock=clock or FrozenUtcClock(NOW))
    return TransactionCapabilities(
        refs=ledger,
        audit=ledger,
        approvals=ledger,
        lineage=ledger,
        object_bindings=ledger,
        runs=ledger,
        cost=ledger,
    )


def uow(
    engine: Engine,
    *,
    clock: FrozenUtcClock | None = None,
) -> SqliteUnitOfWork:
    return SqliteUnitOfWork(
        engine,
        lambda session: capabilities(session, clock=clock),
    )


def seed_current_attempt(
    session: Session,
    *,
    selected_set: BudgetSetSnapshotV1,
    parent: ReservationGroupV1,
    lease_id: str = "lease:1",
    fencing_token: int = 1,
    expires_at: datetime = NOW + timedelta(hours=1),
) -> None:
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    expiry = expires_at.isoformat().replace("+00:00", "Z")
    run = RunRow(
        run_id=selected_set.run_id,
        run_schema_version="run@1",
        kind="test-run",
        kind_version=1,
        status="running",
        revision=2,
        idempotency_scope="test",
        idempotency_key=f"run:{selected_set.run_id}",
        request_hash=REQUEST_HASH,
        payload={"budget_set_snapshot_id": selected_set.budget_set_snapshot_id},
        payload_hash="sha256:" + "b" * 64,
        run_kind_definition_digest="sha256:" + "c" * 64,
        outcome_policy_set_digest="sha256:" + "d" * 64,
        migration_capability_matrix=None,
        failure_classifier={"version": 1},
        dispatch_trace_carrier=None,
        initiated_by={"principal_id": "human:1", "principal_kind": "human"},
        queue_deadline_utc=expiry,
        attempt_timeout_ns=3_600_000_000_000,
        overall_deadline_utc=expiry,
        cancel_requested_at=None,
        cancel_requested_by=None,
        current_attempt_no=1,
        next_attempt_no=2,
        next_fencing_token=fencing_token + 1,
        next_event_seq=2,
        budget_set_snapshot_id=selected_set.budget_set_snapshot_id,
        run_budget_hold_group_id=parent.reservation_group_id,
        concurrency_permit_group_id=None,
        retry_policy={"version": 1},
        max_attempts=3,
        retry_not_before_utc=None,
        result_artifact_id=None,
        failure_artifact_id=None,
        terminal_cassette_artifact_id=None,
        created_at=timestamp,
        updated_at=timestamp,
    )
    attempt = RunAttemptRow(
        run_id=selected_set.run_id,
        attempt_no=1,
        status="running",
        fencing_token=fencing_token,
        worker_principal_id="service:worker",
        trace_id=None,
        next_call_ordinal=1,
        started_at=timestamp,
        attempt_deadline_utc=expiry,
        ended_at=None,
        failure_class=None,
        retryable=None,
        failure_artifact_id=None,
        cassette_bundle_artifact_id=None,
    )
    lease = RunLeaseRow(
        lease_id=lease_id,
        run_id=selected_set.run_id,
        attempt_no=1,
        fencing_token=fencing_token,
        lease_version=1,
        owner_principal_id="service:worker",
        acquired_at=timestamp,
        heartbeat_at=timestamp,
        expires_at=expiry,
        released_at=None,
        status="active",
    )
    session.add(run)
    session.flush()
    session.add(attempt)
    session.flush()
    session.add(lease)
    session.flush()


__all__ = [
    "NOW",
    "amount",
    "amounts_by_dimension",
    "budget",
    "budget_set",
    "capabilities",
    "hold",
    "permit_group",
    "seed_current_attempt",
    "step_group",
    "uow",
    "usage",
]
