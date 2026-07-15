"""The persistent worker's discovery + dispatch loop.

The DB ``RunStore`` is the queue authority; an in-process ``notify`` signal is
only a latency hint that wakes the loop early — every iteration still discovers
work by scanning the database, so lost hints and process restarts never lose a
committed Run. Each iteration:

  1. reaps expired leases discovered by the bounded scan (a stale worker's lease
     is fenced and its Run recovered to ``retry_wait`` or a typed terminal),
  2. claims at most one queued/retry_wait Run under lease fencing,
  3. starts the attempt, extracts the dispatch trace carrier for the attempt's
     consumer spans, and runs the executor with a live lease heartbeat.

Claim/start/reap and the terminal publication are authoritative DB transactions
run OFF the event loop on the injected bounded pool, so the heartbeat coroutine
keeps the lease alive while blocking executor work is in flight. Execution is
at-least-once; fencing gives exactly-once publication + idempotent accounting.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime

from gameforge.apps.worker.heartbeat import LeaseHeartbeat
from gameforge.apps.worker.pool import BlockingExecutorPool
from gameforge.apps.worker.runner import AttemptRunner
from gameforge.contracts.jobs import RunRecord
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import UtcClock
from gameforge.platform.runs.commands import RunClaimRequest, RunClaimResult, RunCommandService
from gameforge.platform.runs.lifecycle import (
    AttemptWriteFence,
    ReapExpiredLeaseRequest,
    RunLifecycleService,
    StartAttemptRequest,
)
from gameforge.runtime.observability.context import TraceCarrier, use_trace_context


ReaperScan = Callable[..., Sequence[RunRecord]]
HeartbeatFactory = Callable[..., LeaseHeartbeat]


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class RunDispatcher:
    def __init__(
        self,
        *,
        claim_service: RunCommandService,
        lifecycle: RunLifecycleService,
        reaper_scan: ReaperScan,
        runner: AttemptRunner,
        heartbeat_factory: HeartbeatFactory,
        pool: BlockingExecutorPool,
        clock: UtcClock,
        worker_actor: AuditActor,
        reaper_actor: AuditActor,
        lease_duration_ns: int,
        heartbeat_interval_s: float = 5.0,
        reaper_limit: int = 32,
        poll_interval_s: float = 1.0,
        lease_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if reaper_actor.principal_kind != "system":
            raise ValueError("the lease reaper requires a system actor")
        if worker_actor.principal_kind not in {"service", "system"}:
            raise ValueError("the worker requires a service or system actor")
        self._claim_service = claim_service
        self._lifecycle = lifecycle
        self._reaper_scan = reaper_scan
        self._runner = runner
        self._heartbeat_factory = heartbeat_factory
        self._pool = pool
        self._clock = clock
        self._worker_actor = worker_actor
        self._reaper_actor = reaper_actor
        self._lease_duration_ns = lease_duration_ns
        self._heartbeat_interval_s = heartbeat_interval_s
        self._reaper_limit = reaper_limit
        self._poll_interval_s = poll_interval_s
        self._lease_id_factory = lease_id_factory or (lambda: f"lease:{secrets.token_hex(16)}")

    async def dispatch_once(self) -> bool:
        """Reap expired leases, then claim + execute at most one Run.

        Returns ``True`` if a Run was claimed and executed this iteration.
        """

        await self._reap_expired()
        claim = await self._pool.run(self._claim)
        if claim is None:
            return False
        await self._execute(claim)
        return True

    async def run_forever(
        self,
        *,
        stop: asyncio.Event,
        notify: asyncio.Event | None = None,
    ) -> None:
        while not stop.is_set():
            worked = await self.dispatch_once()
            if worked:
                continue  # drain eagerly while there is work
            if notify is not None:
                # The notify hint only wakes us early; the next iteration still
                # rediscovers work by scanning the DB, so a lost hint loses nothing.
                waiters = (asyncio.ensure_future(stop.wait()), asyncio.ensure_future(notify.wait()))
                try:
                    await asyncio.wait(
                        waiters,
                        timeout=self._poll_interval_s,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for waiter in waiters:
                        waiter.cancel()
                notify.clear()
            else:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_s)
                except asyncio.TimeoutError:
                    pass

    # ------------------------------------------------------------------ steps
    async def _reap_expired(self) -> None:
        now = _utc_text(self._clock.now_utc())
        expired = await self._pool.run(
            lambda: tuple(self._reaper_scan(now_utc=now, limit=self._reaper_limit))
        )
        for run in expired:
            await self._pool.run(lambda run=run: self._reap_one(run))

    def _reap_one(self, run: RunRecord) -> None:
        self._lifecycle.reap_expired_lease(
            ReapExpiredLeaseRequest(
                run_id=run.run_id,
                expected_run_revision=run.revision,
                actor=self._reaper_actor,
            )
        )

    def _claim(self) -> RunClaimResult | None:
        return self._claim_service.claim_next(
            RunClaimRequest(
                worker=self._worker_actor,
                lease_id=self._lease_id_factory(),
                lease_duration_ns=self._lease_duration_ns,
                trace_id=None,
            )
        )

    async def _execute(self, claim: RunClaimResult) -> None:
        started = await self._pool.run(lambda: self._start(claim))
        run, attempt, lease = started.run, started.attempt, started.lease
        parent = (
            TraceCarrier.extract(run.dispatch_trace_carrier) if run.dispatch_trace_carrier else None
        )
        scope = use_trace_context(parent) if parent is not None else nullcontext()
        heartbeat = self._heartbeat_factory(run=run, attempt=attempt, lease=lease)
        stop = asyncio.Event()
        heartbeat_task = asyncio.ensure_future(heartbeat.run(stop))
        try:
            with scope:
                await self._runner.run_attempt(
                    run=run,
                    attempt=attempt,
                    lease=lease,
                    deadline_utc=_parse_utc(attempt.attempt_deadline_utc),
                )
        finally:
            stop.set()
            await heartbeat_task

    def _start(self, claim: RunClaimResult):
        return self._lifecycle.start_attempt(
            StartAttemptRequest(
                fence=AttemptWriteFence(
                    run_id=claim.run.run_id,
                    attempt_no=claim.attempt.attempt_no,
                    expected_run_revision=claim.run.revision,
                    lease_id=claim.lease.lease_id,
                    fencing_token=claim.attempt.fencing_token,
                ),
                actor=self._worker_actor,
            )
        )


__all__ = ["HeartbeatFactory", "ReaperScan", "RunDispatcher"]
