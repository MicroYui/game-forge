"""``test_heartbeat`` — the lease heartbeat stays live under executor saturation.

The core Task-10 invariant: the heartbeat renews the lease while blocking
executor work runs off the event loop. Because the heartbeat runs on the
ungated control-plane lane (not the bounded executor semaphore), a saturated
executor pool (``max_concurrency=1`` with a long job in flight) must NOT starve
lease renewal — otherwise the lease expires and the worker reaps itself.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from gameforge.apps.worker.heartbeat import LeaseHeartbeat
from gameforge.apps.worker.pool import ControlPlanePool, ThreadedBlockingExecutorPool
from gameforge.contracts.errors import Conflict, IntegrityViolation, QuotaExceeded
from gameforge.contracts.jobs import RunLease
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.runs.lifecycle import PermitGroupBinding, RenewLeaseResult


WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")


class _FakeLifecycle:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.renews = 0
        self._fail_after = fail_after

    def renew_lease(self, request) -> RenewLeaseResult:
        self.renews += 1
        if self._fail_after is not None and self.renews > self._fail_after:
            raise Conflict("Run lease version differs")
        lease = RunLease(
            lease_id=request.lease_id,
            run_id=request.run_id,
            attempt_no=request.attempt_no,
            fencing_token=request.fencing_token,
            lease_version=request.expected_lease_version + 1,
            owner_principal_id=WORKER.principal_id,
            acquired_at="2026-07-14T12:00:10Z",
            heartbeat_at="2026-07-14T12:00:20Z",
            expires_at="2026-07-14T12:00:50Z",
            status="active",
        )
        permit = PermitGroupBinding(
            permit_group_id="permit:1",
            revision=request.expected_permit_revision + 1,
        )
        return RenewLeaseResult(lease=lease, permit=permit)


def _heartbeat(lifecycle, control_pool, *, continue_lease=None) -> LeaseHeartbeat:
    return LeaseHeartbeat(
        lifecycle=lifecycle,
        pool=control_pool,
        run_id="run:1",
        attempt_no=1,
        lease_id="lease:1",
        fencing_token=1,
        lease_duration_ns=30_000_000_000,
        interval_s=0.01,
        initial_lease_version=1,
        initial_permit_revision=1,
        worker_actor=WORKER,
        continue_lease=continue_lease,
    )


def test_heartbeat_renews_while_the_executor_lane_is_saturated() -> None:
    executor_pool = ThreadedBlockingExecutorPool(max_workers=1, max_concurrency=1)
    control_pool = ControlPlanePool(max_workers=2)
    lifecycle = _FakeLifecycle()
    heartbeat = _heartbeat(lifecycle, control_pool)
    release = threading.Event()

    def blocking() -> None:
        assert release.wait(timeout=5.0)

    async def scenario() -> None:
        # Occupy the single executor permit for the whole run.
        blocked = asyncio.ensure_future(executor_pool.run(blocking))
        for _ in range(20):
            await asyncio.sleep(0.01)
            if executor_pool.in_flight >= 1:
                break
        stop = asyncio.Event()
        hb = asyncio.ensure_future(heartbeat.run(stop))
        # The heartbeat renews several times even though the executor lane is full.
        for _ in range(30):
            await asyncio.sleep(0.01)
            if lifecycle.renews >= 2:
                break
        assert lifecycle.renews >= 2
        assert heartbeat.beats >= 2
        assert heartbeat.lease_version > 1  # renewal advanced the fence version
        stop.set()
        release.set()
        await hb
        await blocked

    try:
        asyncio.run(scenario())
    finally:
        executor_pool.close()
        control_pool.close()

    assert heartbeat.fenced is False


def test_heartbeat_lane_is_not_starved_by_saturated_terminal_control_threads() -> None:
    control_pool = ControlPlanePool(max_workers=2)
    heartbeat_pool = ControlPlanePool(
        max_workers=1,
        thread_name_prefix="test-heartbeat",
    )
    lifecycle = _FakeLifecycle()
    heartbeat = _heartbeat(lifecycle, heartbeat_pool)
    release = threading.Event()
    started = threading.Barrier(3)

    def blocking_control() -> None:
        started.wait(timeout=5)
        assert release.wait(timeout=5)

    async def scenario() -> None:
        blocked = [asyncio.create_task(control_pool.run(blocking_control)) for _ in range(2)]
        await asyncio.to_thread(started.wait, 5)
        stop = asyncio.Event()
        beats = asyncio.create_task(heartbeat.run(stop))
        for _ in range(30):
            await asyncio.sleep(0.01)
            if lifecycle.renews >= 2:
                break
        assert lifecycle.renews >= 2
        stop.set()
        release.set()
        await beats
        await asyncio.gather(*blocked)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        heartbeat_pool.close()
        control_pool.close()

    assert heartbeat.fenced is False


def test_heartbeat_marks_fenced_and_stops_when_renewal_is_conflicted() -> None:
    control_pool = ControlPlanePool(max_workers=1)
    lifecycle = _FakeLifecycle(fail_after=1)  # second renewal raises Conflict
    heartbeat = _heartbeat(lifecycle, control_pool)

    async def scenario() -> None:
        stop = asyncio.Event()
        await heartbeat.run(stop)  # returns on its own once fenced

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))
    finally:
        control_pool.close()

    assert heartbeat.fenced is True
    assert lifecycle.renews == 2  # one success, then the conflicted renewal


def test_heartbeat_stops_extending_lease_after_authoritative_cancel_observation() -> None:
    control_pool = ControlPlanePool(max_workers=1)
    lifecycle = _FakeLifecycle()
    heartbeat = _heartbeat(lifecycle, control_pool, continue_lease=lambda: False)

    try:
        asyncio.run(asyncio.wait_for(heartbeat.run(asyncio.Event()), timeout=5.0))
    finally:
        control_pool.close()

    assert heartbeat.stopped_by_authority is True
    assert heartbeat.fenced is False
    assert lifecycle.renews == 0


def test_heartbeat_stops_only_this_attempt_when_permit_renewal_hits_quota() -> None:
    class ExhaustedLifecycle:
        def __init__(self) -> None:
            self.renews = 0

        def renew_lease(self, request):
            del request
            self.renews += 1
            raise QuotaExceeded("concurrency budget deadline was reached")

    control_pool = ControlPlanePool(max_workers=1)
    lifecycle = ExhaustedLifecycle()
    heartbeat = _heartbeat(lifecycle, control_pool)

    try:
        asyncio.run(asyncio.wait_for(heartbeat.run(asyncio.Event()), timeout=5.0))
    finally:
        control_pool.close()

    assert heartbeat.stopped_by_authority is True
    assert heartbeat.fenced is False
    assert lifecycle.renews == 1


def test_heartbeat_propagates_integrity_corruption_instead_of_calling_it_a_fence() -> None:
    class CorruptLifecycle:
        def renew_lease(self, request):
            del request
            raise IntegrityViolation("permit projection is corrupt")

    control_pool = ControlPlanePool(max_workers=1)
    heartbeat = _heartbeat(CorruptLifecycle(), control_pool)
    try:
        with pytest.raises(IntegrityViolation, match="permit projection"):
            asyncio.run(asyncio.wait_for(heartbeat.run(asyncio.Event()), timeout=5.0))
    finally:
        control_pool.close()

    assert heartbeat.fenced is False
