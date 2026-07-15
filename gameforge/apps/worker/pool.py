"""Injected bounded blocking-executor pool for the persistent worker.

The worker's control loop (dispatch, heartbeat, terminal hand-off) is asyncio;
executor work (checker/sim/Agent) is synchronous and CPU/IO blocking. This pool
runs that blocking work OFF the event loop on a bounded ``ThreadPoolExecutor``
while an ``asyncio.Semaphore`` bounds the number of concurrently admitted jobs,
so the heartbeat coroutine keeps renewing leases and the process never admits
more blocking work than its injected bound. It is a latency/concurrency
abstraction only: the DB RunStore remains the queue authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol, TypeVar

T = TypeVar("T")


class BlockingExecutorPool(Protocol):
    """Run one blocking callable off the event loop, bounded by the pool."""

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
            return await loop.run_in_executor(self._executor, fn)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "ThreadedBlockingExecutorPool":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["BlockingExecutorPool", "ThreadedBlockingExecutorPool"]
