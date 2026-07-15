"""Persistent worker entry point.

Composes the local worker runtime from the environment and drives the discovery
+ dispatch loop. Building the runtime performs the real trusted composition
(engine, ObjectStore, telemetry, tracer, bounded pool, registry, actors); an
unprovisioned deployment fails closed at :func:`validate_worker_readiness`
rather than fabricating executor authority.
"""

from __future__ import annotations

from gameforge.apps.worker.app import (
    LocalWorkerConfig,
    build_worker_runtime,
    validate_worker_readiness,
)


def main() -> None:
    config = LocalWorkerConfig.from_environment()
    runtime = build_worker_runtime(config)
    try:
        validate_worker_readiness(runtime)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
