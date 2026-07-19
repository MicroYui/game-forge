"""Seam #1 — bounded expired-lease scan on ``SqlRunRepository`` (M4c Task 10).

The queue authority only surfaces queued/retry_wait Runs through
``get_claim_candidate``; the persistent worker's reaper needs to *discover*
leased/running Runs whose current lease has already expired so it can drive
``reap_expired_lease``. This exercises the new bounded scan over the existing
``ix_run_leases_expiry`` index against a real SQLite database.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session

from gameforge.platform.runs.commands import RunClaimRequest
from gameforge.platform.runs.lifecycle import AttemptWriteFence, StartAttemptRequest
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import RunLeaseRow, RunRow
from gameforge.runtime.persistence.runs import SqlRunRepository
from tests.platform.m4.test_run_create_claim import (
    NOW_DT,
    OVERALL_DEADLINE,
    QUEUE_DEADLINE,
    _create_request,
)
from tests.platform.m4.test_run_multi_connection_integration import (
    REAPER,
    WORKER_1,
    _services,
    _utc,
)
from gameforge.platform.runs.lifecycle import ReapExpiredLeaseRequest
from tests.platform.m4.test_run_fencing import _Registry


def _text(value) -> str:
    return value.astimezone(value.tzinfo).isoformat().replace("+00:00", "Z")


def _create_claim_start(database_url: str, registry: _Registry):
    with _services(database_url, registry, NOW_DT) as (commands, _):
        created = commands.create_run(_create_request())
    claim_at = NOW_DT + timedelta(milliseconds=10)
    with _services(database_url, registry, claim_at) as (commands, _):
        claim = commands.claim_next(
            RunClaimRequest(
                worker=WORKER_1,
                lease_id="lease:attempt:1",
                lease_duration_ns=1_000_000_000,
                trace_id="trace:attempt:1",
            )
        )
    assert claim is not None
    fence = AttemptWriteFence(
        run_id=created.run.run_id,
        attempt_no=claim.attempt.attempt_no,
        expected_run_revision=claim.run.revision,
        lease_id=claim.lease.lease_id,
        fencing_token=claim.attempt.fencing_token,
    )
    with _services(database_url, registry, claim_at + timedelta(milliseconds=10)) as (_, lifecycle):
        lifecycle.start_attempt(StartAttemptRequest(fence=fence, actor=WORKER_1))
    return created, claim


def test_scan_excludes_live_lease_and_surfaces_expired_lease(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'reaper-scan.db'}"
    migrations_api.upgrade(database_url, "head")
    registry = _Registry()
    created, claim = _create_claim_start(database_url, registry)

    engine = get_engine(database_url)
    try:
        with Session(engine) as session:
            repository = SqlRunRepository(session)
            # Before the lease expires, the running Run is NOT a reaper candidate.
            before = repository.list_expired_leases(
                now_utc=_text(_utc(claim.lease.expires_at) - timedelta(seconds=1)),
                limit=10,
            )
            assert before == ()
            # After expiry the Run surfaces with its current revision for fenced reaping.
            after = repository.list_expired_leases(
                now_utc=claim.lease.expires_at,
                limit=10,
            )
            assert tuple(run.run_id for run in after) == (created.run.run_id,)
            assert after[0].status == "running"
    finally:
        engine.dispose()


def test_scan_is_bounded_by_limit(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'reaper-scan-bounded.db'}"
    migrations_api.upgrade(database_url, "head")
    registry = _Registry()
    created, claim = _create_claim_start(database_url, registry)

    engine = get_engine(database_url)
    try:
        with Session(engine) as session:
            repository = SqlRunRepository(session)
            reap_now = _text(_utc(claim.lease.expires_at) + timedelta(milliseconds=10))
            assert repository.list_expired_leases(now_utc=reap_now, limit=1) != ()
            assert len(repository.list_expired_leases(now_utc=reap_now, limit=1)) <= 1
    finally:
        engine.dispose()


def test_expired_scan_orders_integer_and_fractional_utc_chronologically(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'reaper-fractional-boundary.db'}"
    migrations_api.upgrade(database_url, "head")
    registry = _Registry()
    created, claim = _create_claim_start(database_url, registry)

    engine = get_engine(database_url)
    try:
        with Session(engine) as session:
            session.execute(
                update(RunLeaseRow)
                .where(RunLeaseRow.lease_id == claim.lease.lease_id)
                .values(expires_at="2026-07-14T12:00:01Z")
            )
            session.commit()
        with Session(engine) as session:
            candidates = SqlRunRepository(session).list_expired_leases(
                now_utc="2026-07-14T12:00:01.000001Z",
                limit=10,
            )
            assert tuple(run.run_id for run in candidates) == (created.run.run_id,)
    finally:
        engine.dispose()


def test_timeout_scan_surfaces_active_attempt_deadline_before_live_lease_expiry(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'active-timeout.db'}"
    migrations_api.upgrade(database_url, "head")
    registry = _Registry()
    created, claim = _create_claim_start(database_url, registry)

    engine = get_engine(database_url)
    try:
        with Session(engine) as session:
            repository = SqlRunRepository(session)
            attempt = repository.get_attempt(created.run.run_id, claim.attempt.attempt_no)
            assert attempt is not None
            assert attempt.attempt_deadline_utc is not None
            attempt_deadline = _utc(attempt.attempt_deadline_utc)
            # Model a healthy heartbeat whose lease extends beyond the immutable
            # attempt deadline. Timeout discovery, not lease expiry classification,
            # must take authority at the deadline.
            session.execute(
                update(RunLeaseRow)
                .where(RunLeaseRow.lease_id == claim.lease.lease_id)
                .values(expires_at=_text(attempt_deadline + timedelta(seconds=10)))
            )
            session.commit()

        with Session(engine) as session:
            repository = SqlRunRepository(session)
            before = repository.list_timeout_candidates(
                now_utc=_text(attempt_deadline - timedelta(microseconds=1)),
                limit=10,
            )
            assert before == ()
            assert (
                repository.list_expired_leases(
                    now_utc=_text(attempt_deadline),
                    limit=10,
                )
                == ()
            )
            due = repository.list_timeout_candidates(
                now_utc=_text(attempt_deadline),
                limit=10,
            )
            assert tuple(run.run_id for run in due) == (created.run.run_id,)
            assert due[0].status == "running"
    finally:
        engine.dispose()


def test_scan_surfaces_queued_and_retry_wait_deadlines_across_restart(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'inactive-timeouts.db'}"
    migrations_api.upgrade(database_url, "head")
    registry = _Registry()

    with _services(database_url, registry, NOW_DT) as (commands, _):
        queued = commands.create_run(
            _create_request(run_id="run:queued-timeout").model_copy(
                update={"idempotency_key": "request:queued-timeout"}
            )
        )

    _, retry_claim = _create_claim_start(database_url, registry)
    reap_at = _utc(retry_claim.lease.expires_at) + timedelta(milliseconds=10)
    with _services(database_url, registry, reap_at) as (_, lifecycle):
        retried = lifecycle.reap_expired_lease(
            ReapExpiredLeaseRequest(
                run_id=retry_claim.run.run_id,
                expected_run_revision=retry_claim.run.revision + 1,
                actor=REAPER,
            )
        )
    assert retried.run.status == "retry_wait"

    engine = get_engine(database_url)
    try:
        with Session(engine) as session:
            repository = SqlRunRepository(session)
            before = repository.list_inactive_timeout_candidates(
                now_utc=_text(_utc(QUEUE_DEADLINE) - timedelta(microseconds=1)),
                limit=10,
            )
            assert before == ()

            queued_due = repository.list_inactive_timeout_candidates(
                now_utc=QUEUE_DEADLINE,
                limit=10,
            )
            assert tuple(run.run_id for run in queued_due) == (queued.run.run_id,)
            queued_due_fractional = repository.list_inactive_timeout_candidates(
                now_utc="2026-07-14T12:10:00.000001Z",
                limit=10,
            )
            assert tuple(run.run_id for run in queued_due_fractional) == (queued.run.run_id,)

            all_due = repository.list_inactive_timeout_candidates(
                now_utc=OVERALL_DEADLINE,
                limit=10,
            )
            assert {run.run_id for run in all_due} == {
                queued.run.run_id,
                retry_claim.run.run_id,
            }
    finally:
        engine.dispose()


def test_scan_surfaces_cancel_requested_queued_run_before_deadline(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'inactive-cancel.db'}"
    migrations_api.upgrade(database_url, "head")
    registry = _Registry()

    with _services(database_url, registry, NOW_DT) as (commands, _):
        created = commands.create_run(
            _create_request(run_id="run:queued-cancel").model_copy(
                update={"idempotency_key": "request:queued-cancel"}
            )
        )

    engine = get_engine(database_url)
    try:
        with Session(engine) as session:
            session.execute(
                update(RunRow)
                .where(RunRow.run_id == created.run.run_id)
                .values(
                    cancel_requested_at="2026-07-14T12:00:01Z",
                    cancel_requested_by=REAPER.model_dump(mode="json"),
                )
            )
            session.commit()
        with Session(engine) as session:
            repository = SqlRunRepository(session)
            candidates = repository.list_inactive_timeout_candidates(
                now_utc="2026-07-14T12:00:02Z",
                limit=10,
            )
            assert tuple(run.run_id for run in candidates) == (created.run.run_id,)
            production_candidates = repository.list_timeout_candidates(
                now_utc="2026-07-14T12:00:02Z",
                limit=10,
            )
            assert tuple(run.run_id for run in production_candidates) == (created.run.run_id,)
    finally:
        engine.dispose()
