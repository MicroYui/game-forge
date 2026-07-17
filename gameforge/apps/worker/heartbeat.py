"""Operational lease heartbeat for an in-flight worker attempt.

While the runner executes blocking checker/sim/Agent work off the event loop,
this coroutine periodically renews the ``RunLease`` and its execution
``PermitGroup`` so a healthy worker is never fenced by its own inactivity. Per
§3.2 the heartbeat is *operational renewal + telemetry only* — it emits NO
per-beat authoritative audit; ``renew_lease`` advances the lease/permit versions
with a CAS and never writes ``audit@2``. If a renewal is fenced out (the lease was
reaped/stolen or the deadline was reached) the heartbeat marks itself fenced and
stops so the runner's terminal publication will also fence-fail rather than a
stale worker publishing a business result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from gameforge.contracts.errors import Conflict, InvalidStateTransition, QuotaExceeded
from gameforge.contracts.lineage import AuditActor
from gameforge.apps.worker.pool import BlockingExecutorPool
from gameforge.platform.runs.lifecycle import RenewLeaseRequest, RunLifecycleService


class LeaseHeartbeat:
    def __init__(
        self,
        *,
        lifecycle: RunLifecycleService,
        pool: BlockingExecutorPool,
        run_id: str,
        attempt_no: int,
        lease_id: str,
        fencing_token: int,
        lease_duration_ns: int,
        interval_s: float,
        initial_lease_version: int,
        initial_permit_revision: int,
        worker_actor: AuditActor,
        continue_lease: Callable[[], bool] | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._lifecycle = lifecycle
        self._pool = pool
        self._run_id = run_id
        self._attempt_no = attempt_no
        self._lease_id = lease_id
        self._fencing_token = fencing_token
        self._lease_duration_ns = lease_duration_ns
        self._interval_s = interval_s
        self._lease_version = initial_lease_version
        self._permit_revision = initial_permit_revision
        self._worker_actor = worker_actor
        self._continue_lease = continue_lease
        self._fenced = False
        self._stopped_by_authority = False
        self._beats = 0

    @property
    def fenced(self) -> bool:
        return self._fenced

    @property
    def beats(self) -> int:
        return self._beats

    @property
    def stopped_by_authority(self) -> bool:
        return self._stopped_by_authority

    @property
    def lease_version(self) -> int:
        return self._lease_version

    @property
    def permit_revision(self) -> int:
        return self._permit_revision

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
                return  # a stop was requested before the next interval elapsed
            except asyncio.TimeoutError:
                pass  # the interval elapsed: renew now
            if self._continue_lease is not None:
                try:
                    should_continue = await self._pool.run(self._continue_lease)
                except (Conflict, InvalidStateTransition):
                    self._fenced = True
                    return
                if not should_continue:
                    # A committed cancel/terminal projection tells the worker to
                    # stop extending this lease. The executor may be uninterruptible
                    # in a thread, but no stale reserve/publication can pass its
                    # fence and the concurrent reaper will close authority at expiry.
                    self._stopped_by_authority = True
                    return
            try:
                result = await self._pool.run(self._renew)
            except (Conflict, InvalidStateTransition):
                # The lease was reaped/stolen or the deadline was reached: a healthy
                # worker cannot renew, so stop and let the terminal publish fence.
                self._fenced = True
                return
            except QuotaExceeded:
                # A budget may be closed or reach its deadline while this attempt
                # is running. That is authoritative per-Run control state, not
                # worker-process corruption: stop extending only this lease and
                # let expiry/reaping settle it without cancelling sibling Runs.
                self._stopped_by_authority = True
                return
            self._lease_version = result.lease.lease_version
            self._permit_revision = result.permit.revision
            self._beats += 1

    def _renew(self):
        return self._lifecycle.renew_lease(
            RenewLeaseRequest(
                run_id=self._run_id,
                attempt_no=self._attempt_no,
                lease_id=self._lease_id,
                fencing_token=self._fencing_token,
                expected_lease_version=self._lease_version,
                expected_permit_revision=self._permit_revision,
                lease_duration_ns=self._lease_duration_ns,
                actor=self._worker_actor,
            )
        )


__all__ = ["LeaseHeartbeat"]
