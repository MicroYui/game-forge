"""``test_dispatcher`` — worker discovery + dispatch loop (M4c Task 10).

The DB RunStore is the queue authority; the dispatcher rediscovers work by
scanning it every iteration, so lost in-process hints and restarts never lose a
committed Run. Each iteration reaps expired leases (system actor), then claims +
starts + runs one Run with a live lease heartbeat, all fenced.
"""

from __future__ import annotations

import asyncio

from gameforge.apps.worker.dispatcher import RunDispatcher
from gameforge.apps.worker.pool import ThreadedBlockingExecutorPool
from gameforge.contracts.jobs import (
    AttemptLeasedDataV1,
    AttemptStartedDataV1,
    RunAttempt,
    RunEvent,
    RunLease,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.runs.commands import RunClaimResult
from gameforge.platform.runs.lifecycle import StartAttemptResult
from tests.platform.m4c.test_terminal_publisher import (
    _attempt,
    _registry_and_definition,
    _run_record,
)


WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")
REAPER = AuditActor(principal_id="system:lease-reaper", principal_kind="system")


def _leased_attempt() -> RunAttempt:
    return RunAttempt(
        run_id="run:1",
        attempt_no=1,
        status="leased",
        fencing_token=1,
        worker_principal_id=WORKER.principal_id,
        next_call_ordinal=1,
    )


def _lease() -> RunLease:
    return RunLease(
        lease_id="lease:1",
        run_id="run:1",
        attempt_no=1,
        fencing_token=1,
        lease_version=1,
        owner_principal_id=WORKER.principal_id,
        acquired_at="2026-07-14T12:00:10Z",
        heartbeat_at="2026-07-14T12:00:10Z",
        expires_at="2026-07-14T12:00:40Z",
        status="active",
    )


def _leased_event() -> RunEvent:
    return RunEvent(
        run_id="run:1",
        seq=2,
        event_type="attempt.leased",
        attempt_no=1,
        occurred_at="2026-07-14T12:00:10Z",
        data_schema_version="attempt-leased@1",
        data=AttemptLeasedDataV1(attempt_no=1, lease_expires_at="2026-07-14T12:00:40Z"),
    )


def _started_event() -> RunEvent:
    return RunEvent(
        run_id="run:1",
        seq=3,
        event_type="attempt.started",
        attempt_no=1,
        occurred_at="2026-07-14T12:00:11Z",
        data_schema_version="attempt-started@1",
        data=AttemptStartedDataV1(
            attempt_no=1,
            started_at="2026-07-14T12:00:11Z",
            attempt_deadline_utc="2026-07-14T12:30:00Z",
        ),
    )


class _Clock:
    def now_utc(self):
        from datetime import UTC, datetime

        return datetime(2026, 7, 14, 12, 0, 20, tzinfo=UTC)


class _FakeClaim:
    def __init__(self, results) -> None:
        self._results = list(results)
        self.requests = []

    def claim_next(self, request):
        self.requests.append(request)
        return self._results.pop(0) if self._results else None


class _FakeLifecycle:
    def __init__(self, run, attempt, lease) -> None:
        self._run = run
        self._attempt = attempt
        self._lease = lease
        self.reaped: list[tuple[str, int, str]] = []
        self.started = 0

    def start_attempt(self, request):
        self.started += 1
        running_run = self._run.model_copy(
            update={"status": "running", "revision": self._run.revision + 1}
        )
        running_attempt = self._attempt.model_copy(update={"status": "running"})
        return StartAttemptResult(
            previous=self._run,
            run=running_run,
            attempt=running_attempt,
            lease=self._lease,
            event=_started_event(),
        )

    def reap_expired_lease(self, request):
        self.reaped.append(
            (request.run_id, request.expected_run_revision, request.actor.principal_kind)
        )
        return None


class _FakeHeartbeat:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def run(self, stop: asyncio.Event) -> None:
        self.started = True
        await stop.wait()
        self.stopped = True


class _FakeRunner:
    def __init__(self, heartbeat: _FakeHeartbeat) -> None:
        self.calls = []
        self._heartbeat = heartbeat

    async def run_attempt(self, *, run, attempt, lease, deadline_utc):
        # The heartbeat coroutine is live while the (blocking) attempt runs.
        for _ in range(3):
            await asyncio.sleep(0)
        assert self._heartbeat.started is True
        self.calls.append((run.run_id, attempt.attempt_no, deadline_utc))
        return "published"


def _dispatcher(*, claim_results, expired, heartbeat, runner, lifecycle, pool):
    return RunDispatcher(
        claim_service=_FakeClaim(claim_results),
        lifecycle=lifecycle,
        reaper_scan=lambda *, now_utc, limit: expired,
        runner=runner,
        heartbeat_factory=lambda **_: heartbeat,
        pool=pool,
        clock=_Clock(),
        worker_actor=WORKER,
        reaper_actor=REAPER,
        lease_duration_ns=30_000_000_000,
        lease_id_factory=lambda: "lease:1",
    )


def _claim_result(run, attempt, lease) -> RunClaimResult:
    return RunClaimResult(
        previous=run,
        run=run,
        attempt=attempt,
        lease=lease,
        event=_leased_event(),
    )


def test_dispatch_once_reaps_claims_starts_and_runs_with_live_heartbeat() -> None:
    _, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(update={"status": "leased"})
    leased_attempt = _leased_attempt()
    lease = _lease()
    expired_run = run.model_copy(update={"run_id": "run:expired", "revision": 7})
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, _attempt(), lease)

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            claim_results=[_claim_result(run, leased_attempt, lease)],
            expired=(expired_run,),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
        )
        worked = asyncio.run(dispatcher.dispatch_once())

    assert worked is True
    # The expired lease was reaped first, with the system actor + its own revision.
    assert lifecycle.reaped == [("run:expired", 7, "system")]
    assert lifecycle.started == 1
    # The runner executed the started attempt and the heartbeat was stopped after.
    assert runner.calls and runner.calls[0][0] == run.run_id
    assert heartbeat.started is True and heartbeat.stopped is True


def test_idle_iteration_returns_false_and_a_missed_hint_loses_nothing() -> None:
    _, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(update={"status": "leased"})
    leased_attempt = _leased_attempt()
    lease = _lease()
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, _attempt(), lease)

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        # No claim candidate the first pass; the second pass (a re-scan of the DB,
        # not a hint) discovers the same queued Run.
        claim = _FakeClaim([None, _claim_result(run, leased_attempt, lease)])
        dispatcher = RunDispatcher(
            claim_service=claim,
            lifecycle=lifecycle,
            reaper_scan=lambda *, now_utc, limit: (),
            runner=runner,
            heartbeat_factory=lambda **_: heartbeat,
            pool=pool,
            clock=_Clock(),
            worker_actor=WORKER,
            reaper_actor=REAPER,
            lease_duration_ns=30_000_000_000,
            lease_id_factory=lambda: "lease:1",
        )
        first = asyncio.run(dispatcher.dispatch_once())
        second = asyncio.run(dispatcher.dispatch_once())

    assert first is False  # nothing to do this iteration
    assert second is True  # rediscovered by DB scan, not by a hint
    assert runner.calls and runner.calls[0][0] == run.run_id
