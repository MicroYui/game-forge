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

from sqlalchemy.orm import Session

from gameforge.platform.runs.commands import RunClaimRequest
from gameforge.platform.runs.lifecycle import AttemptWriteFence, StartAttemptRequest
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.runs import SqlRunRepository
from tests.platform.m4.test_run_create_claim import NOW_DT, _create_request
from tests.platform.m4.test_run_multi_connection_integration import (
    WORKER_1,
    _services,
    _utc,
)
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
                now_utc=_text(_utc(claim.lease.expires_at) + timedelta(milliseconds=10)),
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
