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
from gameforge.apps.worker.model_authority import WorkerModelExecutionAuthorities


def build_process(
    *,
    model_execution_authorities: WorkerModelExecutionAuthorities | None = None,
) -> WorkerProcess:
    """Compose the worker process and fail closed unless readiness genuinely closes."""

    config = LocalWorkerConfig.from_environment()
    process = (
        build_worker_process(config)
        if model_execution_authorities is None
        else build_worker_process(
            config,
            model_execution_authorities=model_execution_authorities,
        )
    )
    try:
        validate_worker_readiness(process.runtime)
    except BaseException as original:
        # Readiness runs after threads, telemetry and the shared engine exist. A
        # failed startup must join/close all of them just like a driven process.
        try:
            process.close()
        except BaseException as cleanup_error:
            original.add_note(
                "worker cleanup after readiness failure also failed "
                f"({type(cleanup_error).__name__})"
            )
        raise
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
    except BaseException as original:
        try:
            process.close()
        except BaseException as cleanup_error:
            original.add_note(
                f"worker cleanup after drive failure also failed ({type(cleanup_error).__name__})"
            )
        raise
    else:
        process.close()


if __name__ == "__main__":
    main()
