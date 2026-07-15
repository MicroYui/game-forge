"""Persistent worker entry point.

Composes the full worker process from the environment (shared SQLite authority +
ObjectStore, trusted components, the fenced dispatch loop) and drives the discovery
+ dispatch loop until signalled. Building the process performs the real trusted
composition and closes readiness; an unprovisioned deployment fails closed at
:func:`validate_worker_readiness` rather than fabricating executor authority.
"""

from __future__ import annotations

import asyncio
import signal

from gameforge.apps.worker.app import (
    LocalWorkerConfig,
    validate_worker_readiness,
)
from gameforge.apps.worker.dispatch import WorkerProcess, build_worker_process


def build_process() -> WorkerProcess:
    """Compose the worker process and fail closed unless readiness genuinely closes."""

    process = build_worker_process(LocalWorkerConfig.from_environment())
    validate_worker_readiness(process.runtime)
    return process


async def _run(process: WorkerProcess, *, stop: asyncio.Event) -> None:
    await process.dispatcher.run_forever(stop=stop)


def main() -> None:
    process = build_process()
    stop = asyncio.Event()

    async def _drive() -> None:
        loop = asyncio.get_running_loop()
        for signal_name in ("SIGINT", "SIGTERM"):
            try:
                loop.add_signal_handler(getattr(signal, signal_name), stop.set)
            except (NotImplementedError, AttributeError, ValueError):
                # Signal handlers are unavailable on some platforms / non-main threads.
                pass
        await _run(process, stop=stop)

    try:
        asyncio.run(_drive())
    finally:
        process.close()


if __name__ == "__main__":
    main()
