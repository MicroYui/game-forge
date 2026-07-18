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
    ``TrustedComponentMaps.executors`` (Task 10 seam #3), where deferred and
    implemented executors share one signature and resolve without per-kind branching.

The fully-wired dispatch loop additionally needs the platform Run command /
lifecycle services with their cost-accounting plan+settlement providers and the
transaction-bound terminal ``ManifestLedger`` (including the RECORD/REPLAY
cassette-bundle suppliers). Those cross-task providers are injected into
``build_dispatcher`` by the composition root rather than fabricated here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
import base64
import binascii
from hashlib import sha256
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
from urllib.parse import unquote

from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.util import asbool

from gameforge.apps.worker.agent_drafts import (
    TransactionWorkflowGovernanceProvider,
    WorkerAgentDraftGovernanceRefs,
)
from gameforge.apps.worker.agent_prompt_context import (
    agent_prompt_context_binding_plan_keys,
    build_builtin_agent_prompt_context_authority,
)
from gameforge.apps.worker.executor import RunExecutor
from gameforge.apps.worker.model_authority import WorkerModelExecutionAuthorities
from gameforge.apps.worker.prompt_rendering import CanonicalPromptRendererAuthority
from gameforge.apps.worker.pool import ControlPlanePool, ThreadedBlockingExecutorPool
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cassette_import import (
    CassetteBundleV1,
    LegacyImportRoutingDecisionV1,
)
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.jobs import RunRecord
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.registry import (
    PlatformReadinessValidator,
    TrustedComponentMaps,
    build_builtin_registry,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cassette.legacy_import import (
    LegacyCassetteRuntimeImporter,
    LegacyImportAuthority,
    LegacyImportDecisionRepository,
)
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.observability import AlwaysOnSampler, Tracer
from gameforge.runtime.observability.local_store import LocalTelemetryStore
from gameforge.runtime.observability.logs import StructuredLogger
from gameforge.runtime.persistence.engine import DATABASE_URL_ENV, DEFAULT_URL, get_engine
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.cost import SqlCostRepository
from gameforge.runtime.persistence.models import ModelCatalogSnapshotRow, RunRow
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
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
ROLE_POLICY_VERSION_ENV = "GAMEFORGE_IDENTITY_ROLE_POLICY_VERSION"
ROLE_POLICY_DIGEST_ENV = "GAMEFORGE_IDENTITY_ROLE_POLICY_DIGEST"
WORKFLOW_ROUTE_POLICY_VERSION_ENV = "GAMEFORGE_WORKFLOW_ROUTE_POLICY_VERSION"
WORKFLOW_ROUTE_POLICY_DIGEST_ENV = "GAMEFORGE_WORKFLOW_ROUTE_POLICY_DIGEST"
WORKFLOW_APPROVAL_POLICY_VERSION_ENV = "GAMEFORGE_WORKFLOW_APPROVAL_POLICY_VERSION"
WORKFLOW_APPROVAL_POLICY_DIGEST_ENV = "GAMEFORGE_WORKFLOW_APPROVAL_POLICY_DIGEST"
WORKER_RUN_AUDIT_CHAIN_ID = "runs"
_MAX_WORKER_THREADS = 1024
_MAX_LEASE_DURATION_NS = 86_400_000_000_000  # one day
_MAX_CONTROL_INTERVAL_S = 3_600.0
_MODEL_CATALOG_READINESS_PAGE_SIZE = 256
_RUN_REPLAY_READINESS_PAGE_SIZE = 256
_NONTERMINAL_RUN_STATUSES = ("queued", "leased", "running", "retry_wait")
_REQUIRED_WORKER_TABLES = frozenset(
    {
        "alembic_version",
        "approval_decisions",
        "approval_items",
        "artifacts",
        "audit",
        "audit_heads",
        "budget_reservations",
        "budget_snapshots",
        "object_bindings",
        "budgets",
        "runs",
        "run_attempts",
        "run_commands",
        "run_leases",
        "run_events",
        "run_finding_links",
        "run_intermediate_artifact_links",
        "run_model_response_consumptions",
        "run_model_route_links",
        "budget_set_snapshots",
        "concurrency_permits",
        "finding_heads",
        "finding_revisions",
        "idempotency_records",
        "legacy_import_routing_decisions",
        "model_catalog_snapshots",
        "reservation_groups",
        "permit_groups",
        "policy_snapshots",
        "ref_history",
        "ref_transitions",
        "refs",
        "routing_decisions",
        "routing_policies",
        "subject_heads",
        "usage_entries",
    }
)


class WorkerConfigurationError(ValueError):
    """The trusted local worker composition is incomplete or unsafe."""


def _sqlite_file_path(database_url: str) -> Path | None:
    """Return the physical SQLite file target, excluding in-memory databases."""

    try:
        url = make_url(database_url)
    except ArgumentError as exc:
        raise WorkerConfigurationError("database_url must be a valid SQLAlchemy URL") from exc
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if database in {None, "", ":memory:"}:
        return None
    try:
        is_sqlite_uri = asbool(url.query.get("uri", False))
    except ValueError as exc:
        raise WorkerConfigurationError("database_url SQLite uri flag must be boolean") from exc
    if database.startswith("file:") and is_sqlite_uri:
        database = unquote(database.removeprefix("file:"))
        if database == ":memory:" or url.query.get("mode") == "memory":
            return None
    return Path(database).expanduser().resolve(strict=False)


def _same_physical_file(left: Path, right: Path) -> bool:
    left = left.expanduser().resolve(strict=False)
    right = right.expanduser().resolve(strict=False)
    if left == right:
        return True
    try:
        return left.samefile(right)
    except (FileNotFoundError, OSError):
        return False


def _note_cleanup_failure(original: BaseException, *, label: str, error: BaseException) -> None:
    """Retain the primary failure without copying cleanup exception text."""

    original.add_note(f"{label} cleanup also failed ({type(error).__name__})")


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
    if not math.isfinite(value) or value <= 0:
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
    role_policy_version: str | None = None
    role_policy_digest: str | None = None
    workflow_route_policy_version: str | None = None
    workflow_route_policy_digest: str | None = None
    workflow_approval_policy_version: str | None = None
    workflow_approval_policy_digest: str | None = None

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
        for name in ("lease_duration_ns", "reaper_limit", "max_workers"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise WorkerConfigurationError(f"{name} must be a positive integer")
        if self.lease_duration_ns > _MAX_LEASE_DURATION_NS:
            raise WorkerConfigurationError("lease_duration_ns must be at most one day")
        if self.reaper_limit > 1024:
            raise WorkerConfigurationError("reaper_limit must be at most 1024")
        if self.max_workers > _MAX_WORKER_THREADS:
            raise WorkerConfigurationError("max_workers must be at most 1024")
        for name in ("heartbeat_interval_s", "poll_interval_s"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise WorkerConfigurationError(f"{name} must be a positive finite number")
            if value > _MAX_CONTROL_INTERVAL_S:
                raise WorkerConfigurationError(f"{name} must be at most one hour")
        if self.max_concurrency is not None and (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency < 1
        ):
            raise WorkerConfigurationError("max_concurrency must be a positive integer")
        if self.max_concurrency is not None and self.max_concurrency > _MAX_WORKER_THREADS:
            raise WorkerConfigurationError("max_concurrency must be at most 1024")
        if self.max_concurrency is not None and self.max_concurrency > self.max_workers:
            raise WorkerConfigurationError("max_concurrency cannot exceed max_workers")
        governance = (
            self.role_policy_version,
            self.role_policy_digest,
            self.workflow_route_policy_version,
            self.workflow_route_policy_digest,
            self.workflow_approval_policy_version,
            self.workflow_approval_policy_digest,
        )
        present = tuple(value for value in governance if value is not None)
        if present and len(present) != len(governance):
            raise WorkerConfigurationError(
                "worker workflow governance pointers must be provided together or not at all"
            )
        if present:
            for name in (
                "role_policy_version",
                "workflow_route_policy_version",
                "workflow_approval_policy_version",
            ):
                value = getattr(self, name)
                if not isinstance(value, str) or not value or len(value) > 4096:
                    raise WorkerConfigurationError(f"{name} must be a non-empty bounded string")
            for name in (
                "role_policy_digest",
                "workflow_route_policy_digest",
                "workflow_approval_policy_digest",
            ):
                value = getattr(self, name)
                if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    raise WorkerConfigurationError(f"{name} must be a lowercase SHA-256 digest")
        # The heartbeat must renew comfortably before the lease expires; a renewal
        # interval at/above the lease duration self-expires the lease. Require the
        # interval to be at most half the lease so at least one beat lands in time.
        lease_duration_s = self.lease_duration_ns / 1_000_000_000
        if self.heartbeat_interval_s >= lease_duration_s / 2:
            raise WorkerConfigurationError(
                "heartbeat_interval_s must be less than half the lease duration"
            )
        if self.poll_interval_s >= lease_duration_s / 2:
            raise WorkerConfigurationError(
                "poll_interval_s must be less than half the lease duration"
            )
        object_store_root = Path(self.object_store_root).expanduser()
        telemetry_db_path = Path(self.telemetry_db_path).expanduser()
        business_db_path = _sqlite_file_path(self.database_url)
        if business_db_path is not None and _same_physical_file(
            business_db_path,
            telemetry_db_path,
        ):
            raise WorkerConfigurationError(
                "telemetry SQLite must be physically separate from the business SQLite"
            )
        object.__setattr__(self, "object_store_root", object_store_root)
        object.__setattr__(self, "telemetry_db_path", telemetry_db_path)

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
            role_policy_version=source.get(ROLE_POLICY_VERSION_ENV),
            role_policy_digest=source.get(ROLE_POLICY_DIGEST_ENV),
            workflow_route_policy_version=source.get(WORKFLOW_ROUTE_POLICY_VERSION_ENV),
            workflow_route_policy_digest=source.get(WORKFLOW_ROUTE_POLICY_DIGEST_ENV),
            workflow_approval_policy_version=source.get(WORKFLOW_APPROVAL_POLICY_VERSION_ENV),
            workflow_approval_policy_digest=source.get(WORKFLOW_APPROVAL_POLICY_DIGEST_ENV),
        )


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    config: LocalWorkerConfig
    engine: Engine
    object_store: LocalObjectStore
    telemetry_store: LocalTelemetryStore
    tracer: Tracer
    logger: StructuredLogger
    executor_pool: ThreadedBlockingExecutorPool
    control_pool: ControlPlanePool
    heartbeat_pool: ControlPlanePool
    registry: ImmutablePlatformRegistry
    components: TrustedComponentMaps
    worker_actor: AuditActor
    reaper_actor: AuditActor
    model_execution_authorities: WorkerModelExecutionAuthorities | None = None

    def close(self) -> None:
        first_error: BaseException | None = None
        cleanup: list[tuple[str, Callable[[], None]]] = [
            ("executor pool", self.executor_pool.close),
            ("heartbeat pool", self.heartbeat_pool.close),
            ("control pool", self.control_pool.close),
        ]
        model_transport = (
            None
            if self.model_execution_authorities is None
            else self.model_execution_authorities.transport
        )
        model_transport_close = getattr(model_transport, "close", None)
        if callable(model_transport_close):
            cleanup.append(("model transport", model_transport_close))
        cleanup.extend(
            (
                ("telemetry store", self.telemetry_store.close),
                ("business engine", self.engine.dispose),
            )
        )
        for label, close in cleanup:
            try:
                close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
                else:
                    _note_cleanup_failure(first_error, label=label, error=error)
        if first_error is not None:
            raise first_error


def build_worker_registry(
    engine: Engine,
    *,
    clock: SystemUtcClock | None = None,
) -> ImmutablePlatformRegistry:
    """Compose built-in platform authority with exact DB-retained profile history.

    The API persists execution-profile catalogs in ``policy_snapshots`` before it
    admits a Run.  A separately started worker must resolve that same immutable
    ``{catalog_version,catalog_digest}`` from the shared database; falling back to
    process built-ins would strand otherwise valid queued Runs.  Non-profile
    registries remain the frozen built-in authority, while equal catalog versions
    must be byte-for-byte identical through ``with_execution_profile_catalogs``.

    An unmigrated local database remains constructible so the readiness check can
    report its migration-head/table failure without this composition path creating
    schema or inventing catalog state.
    """

    registry = build_builtin_registry()
    if not inspect(engine).has_table("policy_snapshots"):
        return registry
    with Session(engine) as session:
        persisted = SqlPolicySnapshotRepository(
            session,
            clock=clock or SystemUtcClock(),
        ).list_execution_profile_catalogs()
    if not persisted:
        return registry
    return registry.with_execution_profile_catalogs(persisted, replace=False)


def build_worker_runtime(
    config: LocalWorkerConfig,
    *,
    trusted_components: TrustedComponentMaps | None = None,
    engine: Engine | None = None,
    object_store: LocalObjectStore | None = None,
    model_execution_authorities: WorkerModelExecutionAuthorities | None = None,
    registry: ImmutablePlatformRegistry | None = None,
) -> WorkerRuntime:
    clock = SystemUtcClock()
    runtime_engine: Engine | None = engine
    telemetry_store: LocalTelemetryStore | None = None
    executor_pool: ThreadedBlockingExecutorPool | None = None
    control_pool: ControlPlanePool | None = None
    heartbeat_pool: ControlPlanePool | None = None
    try:
        if not isinstance(config, LocalWorkerConfig):
            raise WorkerConfigurationError("local worker requires an exact LocalWorkerConfig")
        components = trusted_components or TrustedComponentMaps()
        if not isinstance(components, TrustedComponentMaps):
            raise WorkerConfigurationError(
                "trusted_components must be an exact TrustedComponentMaps"
            )
        runtime_engine = runtime_engine or get_engine(config.database_url)
        if runtime_engine.dialect.name != "sqlite":
            raise WorkerConfigurationError("local worker composition requires SQLite")
        runtime_registry = (
            build_worker_registry(runtime_engine, clock=clock) if registry is None else registry
        )
        if not isinstance(runtime_registry, ImmutablePlatformRegistry):
            raise WorkerConfigurationError(
                "worker registry must be an exact ImmutablePlatformRegistry"
            )
        if object_store is None:
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
        logger = StructuredLogger(
            service="gameforge-worker",
            store=telemetry_store,
            clock=clock,
            id_generator=lambda: f"log:{secrets.token_hex(16)}",
        )
        executor_pool = ThreadedBlockingExecutorPool(
            max_workers=config.max_workers,
            max_concurrency=config.max_concurrency,
        )
        # Ordinary control work cannot occupy the dedicated heartbeat-renewal lane.
        control_pool = ControlPlanePool(max_workers=max(2, config.max_workers))
        heartbeat_pool = ControlPlanePool(
            max_workers=config.max_concurrency or config.max_workers,
            thread_name_prefix="gameforge-worker-heartbeat",
        )
        return WorkerRuntime(
            config=config,
            engine=runtime_engine,
            object_store=object_store,
            telemetry_store=telemetry_store,
            tracer=tracer,
            logger=logger,
            executor_pool=executor_pool,
            control_pool=control_pool,
            heartbeat_pool=heartbeat_pool,
            registry=runtime_registry,
            components=components,
            worker_actor=AuditActor(
                principal_id=config.worker_principal_id,
                principal_kind="service",
            ),
            reaper_actor=AuditActor(
                principal_id=config.reaper_principal_id,
                principal_kind="system",
            ),
            model_execution_authorities=model_execution_authorities,
        )
    except BaseException as original:
        for label, resource in (
            ("executor pool", executor_pool),
            ("heartbeat pool", heartbeat_pool),
            ("control pool", control_pool),
            ("telemetry store", telemetry_store),
        ):
            if resource is None:
                continue
            try:
                resource.close()
            except BaseException as cleanup_error:
                _note_cleanup_failure(original, label=label, error=cleanup_error)
        if runtime_engine is not None:
            try:
                runtime_engine.dispose()
            except BaseException as cleanup_error:
                _note_cleanup_failure(
                    original,
                    label="business engine",
                    error=cleanup_error,
                )
        raise


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


def build_timeout_scan(engine: Engine) -> Callable[..., tuple[RunRecord, ...]]:
    """Bounded inactive-deadline discovery over the shared SQLite authority."""

    def scan(*, now_utc: str, limit: int) -> tuple[RunRecord, ...]:
        with Session(engine) as session:
            return SqlRunRepository(session).list_timeout_candidates(
                now_utc=now_utc,
                limit=limit,
            )

    return scan


def build_executor_resolver(
    registry: ImmutablePlatformRegistry,
    components: TrustedComponentMaps,
) -> Callable[[RunRecord], RunExecutor]:
    """Generic ``run -> executor_key -> RunExecutor`` resolution.

    Never branches on Run kind: the kind's frozen ``executor_key`` indexes the
    trusted executor allowlist. Deferred and implemented handlers share the exact
    same :class:`RunExecutor` signature, so M4e can replace either deferred callable
    under its retained key without changing dispatch. A missing executor raises
    ``KeyError``, which the runner converts into a redacted, fenced failure through
    the terminal policy.
    """

    executors: Mapping[str, object] = components.executors

    def resolve(run: RunRecord) -> RunExecutor:
        definition = registry.get_run_kind(run.kind)
        if definition is None:
            raise KeyError(f"unknown run kind {run.kind.kind}@{run.kind.version}")
        return executors[definition.executor_key]  # type: ignore[return-value]

    return resolve


def _iter_nonterminal_replay_runs(engine: Engine) -> Iterator[RunRecord]:
    """Keyset-scan exact retained replay Runs without a total history cap."""

    after_run_id: str | None = None
    with Session(engine) as session:
        repository = SqlRunRepository(session)
        while True:
            statement = (
                select(RunRow.run_id)
                .where(RunRow.status.in_(_NONTERMINAL_RUN_STATUSES))
                .order_by(RunRow.run_id)
                .limit(_RUN_REPLAY_READINESS_PAGE_SIZE)
            )
            if after_run_id is not None:
                statement = statement.where(RunRow.run_id > after_run_id)
            try:
                rows = tuple(session.scalars(statement).all())
            except (
                IntegrityViolation,
                RecursionError,
                SQLAlchemyError,
                TypeError,
                ValueError,
            ):
                raise WorkerConfigurationError(
                    "worker retained nonterminal Run is unreadable"
                ) from None
            if not rows:
                return
            for run_id in rows:
                try:
                    run = repository.get(run_id)
                except (
                    IntegrityViolation,
                    RecursionError,
                    SQLAlchemyError,
                    TypeError,
                    ValueError,
                ):
                    raise WorkerConfigurationError(
                        "worker retained nonterminal Run is unreadable"
                    ) from None
                if run is None:
                    raise WorkerConfigurationError(
                        "worker retained nonterminal Run disappeared during readiness"
                    )
                if run.status not in _NONTERMINAL_RUN_STATUSES:
                    continue
                if run.payload.llm_execution_mode != "replay":
                    continue
                if run.payload.cassette_artifact_id is None:
                    raise WorkerConfigurationError(
                        "worker retained REPLAY Run has no cassette authority"
                    )
                yield run
            after_run_id = rows[-1]


def _read_readiness_cassette_bundle(
    runtime: WorkerRuntime,
    artifact_id: str,
) -> CassetteBundleV1:
    """Read and verify one immutable canonical cassette bundle for readiness."""

    # Kept local to avoid making the runtime dataclass retain a second blob-reader
    # authority solely for startup validation.
    from gameforge.apps.worker.components import WorkerArtifactBlobReader
    from gameforge.platform.runs.replay import MAX_REPLAY_ARTIFACT_BYTES

    reader = WorkerArtifactBlobReader(
        engine=runtime.engine,
        object_store=runtime.object_store,
        object_store_id=runtime.config.object_store_id,
        cursor_signing_key=_derive_key(
            runtime.config.root_secret,
            "worker-terminal-cursor",
        ),
        clock=SystemUtcClock(),
    )
    try:
        blob = reader.read_bytes_bounded(
            artifact_id,
            max_bytes=MAX_REPLAY_ARTIFACT_BYTES,
        )
        decoded = json.loads(blob.decode("utf-8"))
        bundle = CassetteBundleV1.model_validate(decoded)
        canonical_blob = canonical_json(bundle.model_dump(mode="json")).encode("utf-8")
    except (
        AttributeError,
        IntegrityViolation,
        OSError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise WorkerConfigurationError(
            "worker retained REPLAY cassette bundle is unreadable"
        ) from None
    if canonical_blob != blob:
        raise WorkerConfigurationError(
            "worker retained REPLAY cassette bundle is not canonical authority"
        )
    return bundle


def _read_readiness_cassette_tree(
    runtime: WorkerRuntime,
    root_artifact_id: str,
    root: CassetteBundleV1 | None = None,
) -> tuple[CassetteBundleV1, dict[str, CassetteBundleV1]]:
    """Read the exact bounded run/attempt/shard tree referenced by one Run."""

    root = root or _read_readiness_cassette_bundle(runtime, root_artifact_id)
    if root.scope != "run":
        raise WorkerConfigurationError("worker retained REPLAY cassette root is not run-scoped")
    if root.run_id is None and len(root.child_bundle_artifact_ids) != 1:
        raise WorkerConfigurationError(
            "worker retained legacy REPLAY cassette root has invalid attempt cardinality"
        )
    visited = {root_artifact_id}
    children: dict[str, CassetteBundleV1] = {}
    for attempt_id in root.child_bundle_artifact_ids:
        if attempt_id in visited:
            raise WorkerConfigurationError(
                "worker retained REPLAY cassette tree repeats an Artifact"
            )
        visited.add(attempt_id)
        attempt = _read_readiness_cassette_bundle(runtime, attempt_id)
        if root.run_id is None and attempt.scope != "attempt":
            raise WorkerConfigurationError(
                "worker retained legacy REPLAY cassette attempt has invalid scope"
            )
        children[attempt_id] = attempt
        for shard_id in attempt.child_bundle_artifact_ids:
            if shard_id in visited:
                raise WorkerConfigurationError(
                    "worker retained REPLAY cassette tree repeats an Artifact"
                )
            visited.add(shard_id)
            shard = _read_readiness_cassette_bundle(runtime, shard_id)
            if root.run_id is None and shard.scope != "record_shard":
                raise WorkerConfigurationError(
                    "worker retained legacy REPLAY cassette shard has invalid scope"
                )
            children[shard_id] = shard
    return root, children


class _ReadOnlyLegacyImportDecisionRepository:
    """Read retained import decisions without granting startup a write path."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_legacy_import_routing_decision(
        self,
        decision_id: str,
    ) -> LegacyImportRoutingDecisionV1 | None:
        with Session(self._engine) as session:
            return SqlCostRepository(session).get_legacy_import_routing_decision(decision_id)

    def put_legacy_import_routing_decision(
        self,
        decision: LegacyImportRoutingDecisionV1,
    ) -> LegacyImportRoutingDecisionV1:
        del decision
        raise IntegrityViolation("worker readiness legacy decision authority is read-only")


def _read_only_legacy_decision_repository(
    engine: Engine,
) -> LegacyImportDecisionRepository:
    return _ReadOnlyLegacyImportDecisionRepository(engine)


def _validate_worker_legacy_replay_authority(
    runtime: WorkerRuntime,
    *,
    legacy_authority: LegacyImportAuthority | None,
) -> None:
    """Verify every retained executable legacy Run against its exact preimages."""

    decisions: LegacyImportDecisionRepository | None = None
    for run in _iter_nonterminal_replay_runs(runtime.engine):
        artifact_id = run.payload.cassette_artifact_id
        if artifact_id is None:  # closed by the iterator; defensive at the boundary
            raise WorkerConfigurationError("worker retained REPLAY Run has no cassette authority")
        root = _read_readiness_cassette_bundle(runtime, artifact_id)
        if root.scope != "run":
            raise WorkerConfigurationError("worker retained REPLAY cassette root is not run-scoped")
        # Native M4 roots retain their source run_id. Verified legacy-import roots
        # contractually use run_id=null and carry import identity in their manifest.
        if root.run_id is not None:
            continue
        if legacy_authority is None:
            raise WorkerConfigurationError(
                "worker legacy import authority is required by a retained "
                f"nonterminal REPLAY Run: {run.run_id}"
            )
        plan = run.payload.execution_version_plan
        if plan is None:
            raise WorkerConfigurationError(
                "worker retained legacy REPLAY Run omitted its execution plan"
            )
        root, children = _read_readiness_cassette_tree(runtime, artifact_id, root)
        if decisions is None:
            decisions = _read_only_legacy_decision_repository(runtime.engine)
        try:
            LegacyCassetteRuntimeImporter(legacy_authority).read_verified(
                root=root,
                child_bundles_by_artifact_id=children,
                model_catalog_version=plan.model_catalog_version,
                model_catalog_digest=plan.model_catalog_digest,
                decision_repository=decisions,
            )
        except (
            AttributeError,
            IntegrityViolation,
            KeyError,
            RecursionError,
            SQLAlchemyError,
            TypeError,
            ValueError,
        ):
            raise WorkerConfigurationError(
                "worker legacy import authority does not close retained "
                f"nonterminal REPLAY Run: {run.run_id}"
            ) from None


def _validate_worker_model_authority_closure(
    engine: Engine,
    *,
    snapshot_ids: tuple[str, ...],
    breaker_ids: tuple[str, ...],
) -> None:
    """Close deployment preimages and online breakers over retained catalogs.

    Catalog history is contractually retained for as long as an exact Run or
    cassette can reference it, so readiness must traverse the complete history
    without imposing a process-local total-row cap.  Each query remains bounded
    and keyset-paged by the immutable catalog-version primary key.

    Disabled-only descriptors are immutable read history and cannot enter a new
    execution, including REPLAY.  Structured preimages and circuit breakers both
    cover descriptors that remain ``active`` in at least one exact retained
    catalog.  A breaker is never accepted without its structured preimage.
    """

    retained_snapshot_ids = set(snapshot_ids)
    retained_breaker_ids = set(breaker_ids)
    breakers_without_preimages = tuple(sorted(retained_breaker_ids - retained_snapshot_ids))
    if breakers_without_preimages:
        raise WorkerConfigurationError(
            "worker dependency-scoped breaker has no structured snapshot preimage: "
            + ", ".join(breakers_without_preimages)
        )

    executable_model_ids: set[str] = set()
    after_catalog_version: int | None = None
    saw_catalog = False
    with Session(engine) as session:
        catalogs = SqlCostRepository(session)
        while True:
            statement = select(ModelCatalogSnapshotRow).order_by(
                ModelCatalogSnapshotRow.catalog_version
            )
            if after_catalog_version is not None:
                statement = statement.where(
                    ModelCatalogSnapshotRow.catalog_version > after_catalog_version
                )
            rows = session.scalars(statement.limit(_MODEL_CATALOG_READINESS_PAGE_SIZE)).all()
            if not rows:
                break
            saw_catalog = True
            for row in rows:
                catalog = catalogs.get_model_catalog(row.catalog_version, row.catalog_digest)
                if catalog is None:
                    raise WorkerConfigurationError(
                        "worker retained model-catalog authority is unreadable"
                    )
                executable_model_ids.update(
                    item.model_snapshot for item in catalog.models if item.status == "active"
                )
            after_catalog_version = rows[-1].catalog_version

    if not saw_catalog:
        raise WorkerConfigurationError("worker retained model-catalog closure is empty")
    missing_snapshot_ids = tuple(sorted(executable_model_ids - retained_snapshot_ids))
    if missing_snapshot_ids:
        raise WorkerConfigurationError(
            "worker model authority misses retained catalog snapshots: "
            + ", ".join(missing_snapshot_ids)
        )
    missing_breaker_ids = tuple(sorted(executable_model_ids - retained_breaker_ids))
    if missing_breaker_ids:
        raise WorkerConfigurationError(
            "worker model authority misses active dependency-scoped breakers: "
            + ", ".join(missing_breaker_ids)
        )


def _validate_worker_prompt_authority_closure(
    registry: ImmutablePlatformRegistry,
    authority: CanonicalPromptRendererAuthority,
) -> None:
    required_prompt_keys = {
        (node.agent_node_id, node.prompt_version, node.tool_version)
        for graph in registry.list_agent_execution_graphs()
        if graph.status in {"active", "replay_only"}
        for node in graph.nodes
    }
    retained_prompt_keys = set(authority.binding_plan_keys)
    missing_prompt_keys = tuple(sorted(required_prompt_keys - retained_prompt_keys))
    if missing_prompt_keys:
        raise WorkerConfigurationError(
            "worker canonical prompt authority misses frozen Agent graph bindings: "
            + ", ".join("/".join(item) for item in missing_prompt_keys)
        )
    context_prompt_keys = set(agent_prompt_context_binding_plan_keys(authority))
    missing_context_prompt_keys = tuple(sorted(required_prompt_keys - context_prompt_keys))
    if missing_context_prompt_keys:
        raise WorkerConfigurationError(
            "worker canonical prompt authority has unfenced Agent context bindings: "
            + ", ".join("/".join(item) for item in missing_context_prompt_keys)
        )
    expected = build_builtin_agent_prompt_context_authority(
        required_plan_keys=tuple(sorted(required_prompt_keys))
    )
    expected_digests = {item[:3]: item[3] for item in expected.binding_plan_configuration_digests}
    retained_digests = {item[:3]: item[3] for item in authority.binding_plan_configuration_digests}
    mismatched_prompt_keys = tuple(
        sorted(
            key
            for key in required_prompt_keys
            if retained_digests.get(key) != expected_digests[key]
        )
    )
    if mismatched_prompt_keys:
        raise WorkerConfigurationError(
            "worker canonical prompt authority differs from frozen Agent request configuration: "
            + ", ".join("/".join(item) for item in mismatched_prompt_keys)
        )


def validate_worker_readiness(runtime: WorkerRuntime) -> None:
    """Fail closed unless schema, audit, storage and registry authority are ready."""

    expected_heads = migrations_api.expected_heads(runtime.config.database_url)
    with runtime.engine.connect() as connection:
        current_heads = tuple(sorted(MigrationContext.configure(connection).get_current_heads()))
    if current_heads != expected_heads:
        raise WorkerConfigurationError("database migration head does not match the worker")
    retained_tables = frozenset(inspect(runtime.engine).get_table_names())
    missing_tables = tuple(sorted(_REQUIRED_WORKER_TABLES - retained_tables))
    if missing_tables:
        raise WorkerConfigurationError(
            f"worker database is missing required tables: {', '.join(missing_tables)}"
        )
    runtime.object_store.check_ready()
    with Session(runtime.engine) as session:
        if SqlAuditSink(session).verify_chain(WORKER_RUN_AUDIT_CHAIN_ID) is not True:
            raise WorkerConfigurationError("worker Run audit chain verification failed")

    report = PlatformReadinessValidator(
        registry=runtime.registry,
        components=runtime.components,
    ).validate()
    if report.ready is not True:
        raise WorkerConfigurationError("worker registry closure is not ready")
    required_heartbeat_capacity = runtime.config.max_concurrency or runtime.config.max_workers
    if runtime.heartbeat_pool.max_workers < required_heartbeat_capacity:
        raise WorkerConfigurationError("worker heartbeat lane cannot cover every active attempt")
    authorities = runtime.model_execution_authorities
    if not isinstance(authorities, WorkerModelExecutionAuthorities):
        raise WorkerConfigurationError("worker model execution authority closure is not configured")
    _validate_worker_legacy_replay_authority(
        runtime,
        legacy_authority=authorities.legacy_imports,
    )
    _validate_worker_prompt_authority_closure(
        runtime.registry,
        authorities.prompt_renderer,
    )
    snapshot_ids = authorities.snapshots.model_snapshot_ids
    breaker_ids = authorities.circuit_breaker_resolver.model_snapshot_ids
    _validate_worker_model_authority_closure(
        runtime.engine,
        snapshot_ids=snapshot_ids,
        breaker_ids=breaker_ids,
    )
    _validate_worker_workflow_governance(runtime)


def _validate_worker_workflow_governance(runtime: WorkerRuntime) -> None:
    """Resolve the exact governance used by active terminal Agent workflows.

    A missing pointer/table/document cannot be deferred until after an Agent has
    consumed model/cost budget: generation, repair, and constraint-proposal success
    all require this authority in their terminal UoW.
    """

    config = runtime.config
    values = (
        config.role_policy_version,
        config.role_policy_digest,
        config.workflow_route_policy_version,
        config.workflow_route_policy_digest,
        config.workflow_approval_policy_version,
        config.workflow_approval_policy_digest,
    )
    if any(value is None for value in values):
        raise WorkerConfigurationError(
            "worker workflow governance pointers are required for active Agent workflows"
        )
    refs = WorkerAgentDraftGovernanceRefs(
        role_policy_version=config.role_policy_version,  # type: ignore[arg-type]
        role_policy_digest=config.role_policy_digest,  # type: ignore[arg-type]
        route_policy_version=config.workflow_route_policy_version,  # type: ignore[arg-type]
        route_policy_digest=config.workflow_route_policy_digest,  # type: ignore[arg-type]
        approval_policy_version=config.workflow_approval_policy_version,  # type: ignore[arg-type]
        approval_policy_digest=config.workflow_approval_policy_digest,  # type: ignore[arg-type]
    )
    with Session(runtime.engine) as session:
        provider = TransactionWorkflowGovernanceProvider(
            policies=SqlPolicySnapshotRepository(session, clock=SystemUtcClock()),
            refs=refs,
        )
        try:
            provider.current()
        except (DependencyUnavailable, IntegrityViolation, ValueError) as exc:
            # The provider raises only typed dependency/integrity/validation
            # failures, but readiness must expose one stable configuration error
            # and must not copy repository exception text into process output.
            raise WorkerConfigurationError(
                "worker workflow governance authority is unavailable or inconsistent"
            ) from exc


__all__ = [
    "LocalWorkerConfig",
    "WORKER_RUN_AUDIT_CHAIN_ID",
    "WorkerConfigurationError",
    "WorkerRuntime",
    "build_executor_resolver",
    "build_reaper_scan",
    "build_timeout_scan",
    "build_worker_registry",
    "build_worker_runtime",
    "validate_worker_readiness",
]
