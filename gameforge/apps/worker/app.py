"""Local composition for the persistent worker process.

Mirrors ``apps/api/local.py``: this module owns environment reads and concrete
adapters for the SECOND long-running process of the modular-monolith artifact.
It shares the SQLite / ObjectStore / telemetry authority with ``apps/api`` but
runs in its own process, discovering committed Runs through the DB queue
authority and executing them under lease fencing.

What is composed here today:
  * the shared worker runtime (engine, ObjectStore, telemetry, tracer, the
    injected bounded blocking-executor pool, the built-in registry, and the
    service/system actors),
  * the bounded expired-lease reaper scan over ``SqlRunRepository`` (Task 10
    seam #1), and
  * the GENERIC ``executor_key -> RunExecutor`` resolver over
    ``TrustedComponentMaps.executors`` (Task 10 seam #3), which adapts the two
    still-deferred executors and, once Tasks 11-13 register the eleven real
    executors, resolves them without any per-kind branching.

The fully-wired dispatch loop additionally needs the platform Run command /
lifecycle services with their cost-accounting plan+settlement providers and the
transaction-bound terminal ``ManifestLedger`` (including the RECORD/REPLAY
cassette-bundle suppliers). Those cross-task providers are injected into
``build_dispatcher`` by the composition root rather than fabricated here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import base64
import binascii
from hashlib import sha256
import hmac
import os
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.apps.worker.executor import RunExecutor, deferred_executor_adapter
from gameforge.apps.worker.pool import ControlPlanePool, ThreadedBlockingExecutorPool
from gameforge.contracts.jobs import RunRecord
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.registry import (
    PlatformReadinessValidator,
    TrustedComponentMaps,
    build_builtin_registry,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.run_handlers.deferred import DEFERRED_EXECUTORS
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.observability import AlwaysOnSampler, Tracer
from gameforge.runtime.observability.local_store import LocalTelemetryStore
from gameforge.runtime.persistence.engine import DATABASE_URL_ENV, DEFAULT_URL, get_engine
from gameforge.runtime.persistence.runs import SqlRunRepository


OBJECT_STORE_ROOT_ENV = "GAMEFORGE_OBJECT_STORE_ROOT"
OBJECT_STORE_ID_ENV = "GAMEFORGE_OBJECT_STORE_ID"
TELEMETRY_DB_PATH_ENV = "GAMEFORGE_TELEMETRY_DB_PATH"
WORKER_PRINCIPAL_ID_ENV = "GAMEFORGE_WORKER_PRINCIPAL_ID"
REAPER_PRINCIPAL_ID_ENV = "GAMEFORGE_WORKER_REAPER_PRINCIPAL_ID"
WORKER_LEASE_DURATION_NS_ENV = "GAMEFORGE_WORKER_LEASE_DURATION_NS"
WORKER_HEARTBEAT_INTERVAL_S_ENV = "GAMEFORGE_WORKER_HEARTBEAT_INTERVAL_S"
WORKER_POLL_INTERVAL_S_ENV = "GAMEFORGE_WORKER_POLL_INTERVAL_S"
WORKER_MAX_WORKERS_ENV = "GAMEFORGE_WORKER_MAX_WORKERS"
WORKER_MAX_CONCURRENCY_ENV = "GAMEFORGE_WORKER_MAX_CONCURRENCY"
WORKER_REAPER_LIMIT_ENV = "GAMEFORGE_WORKER_REAPER_LIMIT"
LOCAL_ROOT_SECRET_ENV = "GAMEFORGE_LOCAL_SECRET_BASE64"


class WorkerConfigurationError(ValueError):
    """The trusted local worker composition is incomplete or unsafe."""


def _root_secret(source: Mapping[str, str]) -> bytes:
    encoded = source.get(LOCAL_ROOT_SECRET_ENV)
    if not encoded:
        raise WorkerConfigurationError(f"{LOCAL_ROOT_SECRET_ENV} is required")
    try:
        value = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise WorkerConfigurationError(f"{LOCAL_ROOT_SECRET_ENV} must be valid base64") from None
    if len(value) < 32:
        raise WorkerConfigurationError(f"{LOCAL_ROOT_SECRET_ENV} must decode to at least 32 bytes")
    return value


def _derive_key(root_secret: bytes, purpose: str) -> bytes:
    return hmac.new(
        root_secret,
        b"gameforge-local-worker@1\x00" + purpose.encode("ascii"),
        sha256,
    ).digest()


def _positive_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise WorkerConfigurationError(f"{name} must be a positive integer") from None
    if value < 1:
        raise WorkerConfigurationError(f"{name} must be a positive integer")
    return value


def _positive_float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = source.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise WorkerConfigurationError(f"{name} must be a positive number") from None
    if value <= 0:
        raise WorkerConfigurationError(f"{name} must be a positive number")
    return value


@dataclass(frozen=True, slots=True)
class LocalWorkerConfig:
    database_url: str
    object_store_root: Path
    object_store_id: str
    telemetry_db_path: Path
    worker_principal_id: str
    reaper_principal_id: str
    lease_duration_ns: int = 30_000_000_000
    heartbeat_interval_s: float = 5.0
    poll_interval_s: float = 1.0
    reaper_limit: int = 32
    max_workers: int = 4
    max_concurrency: int | None = None
    root_secret: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        for name in (
            "database_url",
            "object_store_id",
            "worker_principal_id",
            "reaper_principal_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise WorkerConfigurationError(f"{name} must be a non-empty bounded string")
        if not isinstance(self.root_secret, bytes) or len(self.root_secret) < 32:
            raise WorkerConfigurationError("root_secret must contain at least 32 bytes")
        if self.max_concurrency is not None and self.max_concurrency > self.max_workers:
            raise WorkerConfigurationError("max_concurrency cannot exceed max_workers")
        # The heartbeat must renew comfortably before the lease expires; a renewal
        # interval at/above the lease duration self-expires the lease. Require the
        # interval to be at most half the lease so at least one beat lands in time.
        lease_duration_s = self.lease_duration_ns / 1_000_000_000
        if self.heartbeat_interval_s > lease_duration_s / 2:
            raise WorkerConfigurationError(
                "heartbeat_interval_s must be at most half the lease duration"
            )
        object.__setattr__(self, "object_store_root", Path(self.object_store_root))
        object.__setattr__(self, "telemetry_db_path", Path(self.telemetry_db_path))

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "LocalWorkerConfig":
        source = os.environ if environment is None else environment
        worker_principal_id = source.get(WORKER_PRINCIPAL_ID_ENV)
        if not worker_principal_id:
            raise WorkerConfigurationError(f"{WORKER_PRINCIPAL_ID_ENV} is required")
        reaper_principal_id = source.get(REAPER_PRINCIPAL_ID_ENV)
        if not reaper_principal_id:
            raise WorkerConfigurationError(f"{REAPER_PRINCIPAL_ID_ENV} is required")
        max_concurrency_raw = source.get(WORKER_MAX_CONCURRENCY_ENV)
        max_concurrency = (
            _positive_int(source, WORKER_MAX_CONCURRENCY_ENV, 0) if max_concurrency_raw else None
        )
        return cls(
            database_url=source.get(DATABASE_URL_ENV, DEFAULT_URL),
            object_store_root=Path(source.get(OBJECT_STORE_ROOT_ENV, ".gameforge/objects")),
            object_store_id=source.get(OBJECT_STORE_ID_ENV, "local:default"),
            telemetry_db_path=Path(
                source.get(TELEMETRY_DB_PATH_ENV, ".gameforge/telemetry.sqlite3")
            ),
            worker_principal_id=worker_principal_id,
            reaper_principal_id=reaper_principal_id,
            lease_duration_ns=_positive_int(source, WORKER_LEASE_DURATION_NS_ENV, 30_000_000_000),
            heartbeat_interval_s=_positive_float(source, WORKER_HEARTBEAT_INTERVAL_S_ENV, 5.0),
            poll_interval_s=_positive_float(source, WORKER_POLL_INTERVAL_S_ENV, 1.0),
            reaper_limit=_positive_int(source, WORKER_REAPER_LIMIT_ENV, 32),
            max_workers=_positive_int(source, WORKER_MAX_WORKERS_ENV, 4),
            max_concurrency=max_concurrency,
            root_secret=_root_secret(source),
        )


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    config: LocalWorkerConfig
    engine: Engine
    object_store: LocalObjectStore
    telemetry_store: LocalTelemetryStore
    tracer: Tracer
    executor_pool: ThreadedBlockingExecutorPool
    control_pool: ControlPlanePool
    registry: ImmutablePlatformRegistry
    components: TrustedComponentMaps
    worker_actor: AuditActor
    reaper_actor: AuditActor

    def close(self) -> None:
        self.executor_pool.close()
        self.control_pool.close()
        self.telemetry_store.close()
        self.engine.dispose()


def build_worker_runtime(
    config: LocalWorkerConfig,
    *,
    trusted_components: TrustedComponentMaps | None = None,
) -> WorkerRuntime:
    if not isinstance(config, LocalWorkerConfig):
        raise WorkerConfigurationError("local worker requires an exact LocalWorkerConfig")
    components = trusted_components or TrustedComponentMaps()
    if not isinstance(components, TrustedComponentMaps):
        raise WorkerConfigurationError("trusted_components must be an exact TrustedComponentMaps")

    clock = SystemUtcClock()
    engine = get_engine(config.database_url)
    if engine.dialect.name != "sqlite":
        engine.dispose()
        raise WorkerConfigurationError("local worker composition requires SQLite")
    object_store = LocalObjectStore(
        config.object_store_root,
        store_id=config.object_store_id,
        clock=clock,
        cursor_signing_key=_derive_key(config.root_secret, "object-store-cursor"),
    )
    telemetry_store = LocalTelemetryStore(
        config.telemetry_db_path,
        clock=clock,
        signing_key=_derive_key(config.root_secret, "telemetry-cursor"),
    )
    tracer = Tracer(
        exporter=_TelemetryExporter(telemetry_store),
        sampler=AlwaysOnSampler(),
        resource={"service.name": "gameforge-worker"},
    )
    executor_pool = ThreadedBlockingExecutorPool(
        max_workers=config.max_workers,
        max_concurrency=config.max_concurrency,
    )
    # Control-plane DB ops (heartbeat/claim/reap/terminal) run on a separate,
    # ungated lane so a saturated executor pool never stalls a lease heartbeat.
    control_pool = ControlPlanePool(max_workers=max(2, config.max_workers))
    return WorkerRuntime(
        config=config,
        engine=engine,
        object_store=object_store,
        telemetry_store=telemetry_store,
        tracer=tracer,
        executor_pool=executor_pool,
        control_pool=control_pool,
        registry=build_builtin_registry(),
        components=components,
        worker_actor=AuditActor(principal_id=config.worker_principal_id, principal_kind="service"),
        reaper_actor=AuditActor(principal_id=config.reaper_principal_id, principal_kind="system"),
    )


class _TelemetryExporter:
    def __init__(self, store: LocalTelemetryStore) -> None:
        self._store = store

    def export(self, spans) -> None:
        for span in spans:
            self._store.put(span)


def build_reaper_scan(engine: Engine) -> Callable[..., tuple[RunRecord, ...]]:
    """Bounded expired-lease discovery over the shared SQLite authority."""

    def scan(*, now_utc: str, limit: int) -> tuple[RunRecord, ...]:
        with Session(engine) as session:
            return SqlRunRepository(session).list_expired_leases(now_utc=now_utc, limit=limit)

    return scan


def build_executor_resolver(
    registry: ImmutablePlatformRegistry,
    components: TrustedComponentMaps,
) -> Callable[[RunRecord], RunExecutor]:
    """Generic ``run -> executor_key -> RunExecutor`` resolution.

    Never branches on Run kind: the kind's frozen ``executor_key`` indexes the
    trusted executor allowlist. The two still-deferred executors expose the narrow
    ``Callable[[DeferredExecutionRequest], PreparedRunFailure]`` signature, so they
    are wrapped by :func:`deferred_executor_adapter` into the generic
    :class:`RunExecutor` shape; once Tasks 11-13 register the eleven real executors
    (already ``RunExecutor``-shaped) they resolve unwrapped. A missing executor
    raises ``KeyError``, which the runner converts into a redacted, fenced failure
    through the terminal policy.
    """

    executors: Mapping[str, object] = components.executors

    def resolve(run: RunRecord) -> RunExecutor:
        definition = registry.get_run_kind(run.kind)
        if definition is None:
            raise KeyError(f"unknown run kind {run.kind.kind}@{run.kind.version}")
        executor = executors[definition.executor_key]
        if definition.executor_key in DEFERRED_EXECUTORS:
            return deferred_executor_adapter(executor)  # type: ignore[arg-type]
        return executor  # type: ignore[return-value]

    return resolve


def validate_worker_readiness(runtime: WorkerRuntime) -> None:
    """Fail closed unless every active Run kind's executor is provisioned."""

    PlatformReadinessValidator(
        registry=runtime.registry,
        components=runtime.components,
    ).validate()


__all__ = [
    "LocalWorkerConfig",
    "WorkerConfigurationError",
    "WorkerRuntime",
    "build_executor_resolver",
    "build_reaper_scan",
    "build_worker_runtime",
    "validate_worker_readiness",
]
