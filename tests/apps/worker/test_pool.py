"""Seam #2 — the injected bounded blocking-executor pool (M4c Task 10).

Blocking checker/sim/Agent execution must run OFF the asyncio event loop so the
lease heartbeat coroutine keeps renewing while an attempt is mid-flight, and the
pool must bound in-flight concurrency (in-process signals are only latency hints;
the DB queue authority governs work).
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from gameforge.apps.worker.pool import ControlPlanePool, ThreadedBlockingExecutorPool


def test_blocking_work_runs_off_the_event_loop_while_a_heartbeat_keeps_ticking() -> None:
    pool = ThreadedBlockingExecutorPool(max_workers=2)
    released = threading.Event()
    beats: list[int] = []

    def blocking() -> str:
        # Block a worker thread until the event loop has advanced the heartbeat.
        assert released.wait(timeout=5.0)
        return "done"

    async def scenario() -> str:
        task = asyncio.ensure_future(pool.run(blocking))
        for _ in range(5):
            await asyncio.sleep(0)  # the loop stays responsive despite blocking work
            beats.append(len(beats))
        released.set()  # only now may the blocking thread finish
        return await task

    try:
        result = asyncio.run(scenario())
    finally:
        pool.close()

    assert result == "done"
    # The heartbeat ticked several times *before* the blocking call was allowed to end.
    assert len(beats) == 5


def test_pool_bounds_concurrent_blocking_work() -> None:
    pool = ThreadedBlockingExecutorPool(max_workers=8, max_concurrency=2)
    lock = threading.Lock()
    in_flight = 0
    peak = 0
    gate = threading.Event()

    def blocking() -> None:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        assert gate.wait(timeout=5.0)
        with lock:
            in_flight -= 1

    async def scenario() -> None:
        tasks = [asyncio.ensure_future(pool.run(blocking)) for _ in range(6)]
        # Give the pool a chance to admit as many as its bound permits.
        for _ in range(20):
            await asyncio.sleep(0.01)
            if peak >= 2:
                break
        gate.set()
        await asyncio.gather(*tasks)

    try:
        asyncio.run(scenario())
    finally:
        pool.close()

    assert peak == 2  # never exceeded the injected concurrency bound


def test_blocking_exception_propagates_to_the_awaiter() -> None:
    pool = ThreadedBlockingExecutorPool(max_workers=1)

    def blocking() -> None:
        raise RuntimeError("boom")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            await pool.run(blocking)

    try:
        asyncio.run(scenario())
    finally:
        pool.close()


def test_run_after_close_is_rejected() -> None:
    pool = ThreadedBlockingExecutorPool(max_workers=1)
    pool.close()

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="closed"):
            await pool.run(lambda: time.time())

    asyncio.run(scenario())


def test_control_plane_lane_is_not_starved_by_a_saturated_executor_lane() -> None:
    # The executor lane is bounded to a single permit and held for the whole test;
    # the control-plane lane (heartbeat/claim/reap/terminal) must still make progress.
    executor_pool = ThreadedBlockingExecutorPool(max_workers=1, max_concurrency=1)
    control_pool = ControlPlanePool(max_workers=2)
    release = threading.Event()
    control_ran: list[str] = []

    def blocking() -> str:
        assert release.wait(timeout=5.0)
        return "executor"

    async def scenario() -> None:
        blocked = asyncio.ensure_future(executor_pool.run(blocking))
        for _ in range(20):  # let the blocking job grab the only executor permit
            await asyncio.sleep(0.01)
            if executor_pool.in_flight >= 1:
                break
        # Control-plane ops proceed while the executor semaphore is fully held.
        for _ in range(3):
            control_ran.append(await control_pool.run(lambda: "renewed"))
        release.set()
        assert await blocked == "executor"

    try:
        asyncio.run(scenario())
    finally:
        executor_pool.close()
        control_pool.close()

    assert control_ran == ["renewed", "renewed", "renewed"]


def test_control_plane_run_after_close_is_rejected() -> None:
    pool = ControlPlanePool(max_workers=1)
    pool.close()

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="closed"):
            await pool.run(lambda: 1)

    asyncio.run(scenario())
