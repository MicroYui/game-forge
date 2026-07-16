"""Injected off-loop execution lanes for the persistent worker.

The worker's control loop (dispatch, heartbeat, terminal hand-off) is asyncio;
executor work (checker/sim/Agent) is synchronous and CPU/IO blocking. Two lanes
keep them from interfering:

* :class:`ThreadedBlockingExecutorPool` runs the executor's blocking *domain*
  work on a bounded ``ThreadPoolExecutor`` fronted by an ``asyncio.Semaphore`` so
  the process never admits more blocking work than its injected bound.
* :class:`ControlPlanePool` supplies separate off-loop lanes for ordinary
  control work and heartbeat renewal. They are NOT gated by the executor
  semaphore — a
  saturated executor lane (e.g. ``max_concurrency=1`` with a minutes-long run in
  flight) must never stall a lease heartbeat, which would make the worker reap
  itself.

Both are latency/concurrency abstractions only: the DB RunStore remains the
queue authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Protocol, TypeVar

T = TypeVar("T")


class BlockingExecutorPool(Protocol):
    """Run one blocking callable off the event loop."""

    async def run(self, fn: Callable[[], T]) -> T: ...

    def close(self) -> None: ...


class ThreadedBlockingExecutorPool:
    """A bounded ``ThreadPoolExecutor`` fronted by an asyncio concurrency gate."""

    def __init__(self, *, max_workers: int, max_concurrency: int | None = None) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        bound = max_workers if max_concurrency is None else max_concurrency
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 1:
            raise ValueError("max_concurrency must be a positive integer")
        if bound > max_workers:
            raise ValueError("max_concurrency cannot exceed max_workers")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="gameforge-worker"
        )
        self._bound = bound
        self._semaphore: asyncio.Semaphore | None = None
        self._closed = False
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def run(self, fn: Callable[[], T]) -> T:
        if self._closed:
            raise RuntimeError("blocking executor pool is closed")
        if self._semaphore is None:
            # Lazily bind the semaphore to the running loop that first uses the pool.
            self._semaphore = asyncio.Semaphore(self._bound)
        loop = asyncio.get_running_loop()
        async with self._semaphore:
            if self._closed:
                raise RuntimeError("blocking executor pool is closed")
            self._in_flight += 1
            try:
                # ``run_in_executor`` does not propagate ContextVars. Capture the
                # attempt consumer context at submission so executor/model spans
                # remain children of the DB-carried worker span.
                context = copy_context()
                return await loop.run_in_executor(self._executor, context.run, fn)
            finally:
                self._in_flight -= 1

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # WorkerRuntime closes shared DB/ObjectStore/telemetry only after this
        # returns. Joining here prevents a cancelled/failing process from letting a
        # still-running executor thread touch already-disposed authority.
        self._executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> "ThreadedBlockingExecutorPool":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ControlPlanePool:
    """Ungated off-loop lane for latency-critical control-plane DB operations.

    Heartbeat renewals, claim/reap, and terminal publication run here so they are
    never blocked by the bounded executor lane. It has its own threads and NO
    concurrency semaphore, so a fully-saturated executor pool cannot stall a
    lease heartbeat.
    """

    def __init__(
        self,
        *,
        max_workers: int = 2,
        thread_name_prefix: str = "gameforge-worker-ctl",
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._max_workers = max_workers
        self._closed = False

    @property
    def max_workers(self) -> int:
        return self._max_workers

    async def run(self, fn: Callable[[], T]) -> T:
        if self._closed:
            raise RuntimeError("control-plane pool is closed")
        loop = asyncio.get_running_loop()
        context = copy_context()
        return await loop.run_in_executor(self._executor, context.run, fn)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> "ControlPlanePool":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["BlockingExecutorPool", "ControlPlanePool", "ThreadedBlockingExecutorPool"]
