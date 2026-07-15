"""The persistent worker's dispatch composition root (M4c Task 17a).

Wires the production dispatch loop over ONE shared SQLite authority + ObjectStore:
the ``RunCommandService`` (claim), ``RunLifecycleService`` (start/heartbeat/publish/
reap), the fenced ``AttemptRunner`` + ``LeaseHeartbeat`` + bounded pools, the generic
``executor_key -> RunExecutor`` resolver, and the REAL Task-9 ``TerminalPublisher``
bound through the concrete ``apps/worker/publication.py`` adapters (BlobStore /
ArtifactPort / ManifestLedger / AuditPort). The DB RunStore is the queue authority;
``RunDispatcher.dispatch_once`` / ``run_forever`` drive discovery + fenced execution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from gameforge.apps.worker.app import (
    LocalWorkerConfig,
    WorkerConfigurationError,
    WorkerRuntime,
    _derive_key,
    build_executor_resolver,
    build_reaper_scan,
    build_worker_runtime,
)
from gameforge.apps.worker.components import (
    WorkerArtifactBlobReader,
    WorkerPreparedArtifactStore,
    build_trusted_components,
)
from gameforge.apps.worker.dispatcher import RunDispatcher
from gameforge.apps.worker.executor import WorkerModelBridgePort
from gameforge.apps.worker.heartbeat import LeaseHeartbeat
from gameforge.apps.worker.publication import (
    BlobLocationRegistry,
    WorkerArtifactPort,
    WorkerAuditPort,
    WorkerBlobStore,
    WorkerCommandPublicationGateway,
    WorkerManifestLedger,
)
from gameforge.apps.worker.terminal import WorkerTerminalPublisher
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import RunAttempt, RunLease, RunRecord
from gameforge.contracts.storage import UtcClock
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.cost_policy.run_accounting import SqlRunCostAccounting
from gameforge.platform.publication import TerminalPublisher
from gameforge.platform.registry import TrustedComponentMaps, build_builtin_registry
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.runs.admission import (
    ConservativeAttemptUsageProvider,
    DefaultRunBudgetPlanProvider,
)
from gameforge.platform.runs.commands import RunCommandCapabilities, RunCommandService
from gameforge.platform.runs.lifecycle import (
    RunLifecycleCapabilities,
    RunLifecycleService,
)
from gameforge.apps.worker.runner import AttemptRunner
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.findings import SqlFindingRepository
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


WORKER_RUN_AUDIT_CHAIN_ID = "runs"


class _DeferredModelBridge:
    """A fenced attempt's model bridge for a deterministic / not_applicable Run.

    A ``not_applicable`` executor (checker/simulation/task_suite) never calls the
    model. The RECORD/REPLAY LLM bridge (prompt render -> route -> cost -> router) is
    Task 18; until then an LLM executor that reaches for the model fails closed and the
    runner classifies it into a redacted attempt failure rather than escaping the loop.
    """

    def call_model(self, request: object) -> object:
        raise IntegrityViolation("worker model bridge is deferred to Task 18 (RECORD/REPLAY)")


@dataclass(frozen=True, slots=True)
class WorkerProcess:
    """The composed worker process: shared authority + the driven dispatch loop."""

    runtime: WorkerRuntime
    dispatcher: RunDispatcher
    components: TrustedComponentMaps
    blob_registry: BlobLocationRegistry

    def close(self) -> None:
        self.runtime.close()


def build_worker_dispatch(
    *,
    runtime: WorkerRuntime,
    registry: ImmutablePlatformRegistry,
    blob_registry: BlobLocationRegistry,
    terminal_cursor_signing_key: bytes,
    run_audit_chain_id: str = WORKER_RUN_AUDIT_CHAIN_ID,
    notify: Callable[[str], None] | None = None,
) -> RunDispatcher:
    """Assemble the fenced dispatch loop over the worker runtime's shared authority."""

    clock: UtcClock = SystemUtcClock()
    engine = runtime.engine
    object_store = runtime.object_store
    object_store_id = runtime.config.object_store_id
    config = runtime.config

    def _cursor_signer() -> CursorSigner:
        return CursorSigner(signing_key=terminal_cursor_signing_key, clock=clock)

    def capability_factory(session: Session) -> TransactionCapabilities:
        cursor_signer = _cursor_signer()
        object_bindings = SqlObjectBindingRepository(session, object_store, object_store_id)
        return TransactionCapabilities(
            refs=SqlRefStore(session, cursor_signer=cursor_signer, clock=clock),
            audit=SqlAuditSink(session),
            approvals=None,
            lineage=None,
            object_bindings=object_bindings,
            runs=SqlRunRepository(session),
            cost=SqlCostLedger(session, clock=clock),
            artifacts=SqlArtifactRepository(
                session,
                binding_repository=object_bindings,
                cursor_signer=cursor_signer,
                clock=clock,
            ),
            findings=SqlFindingRepository(session, cursor_signer=cursor_signer, clock=clock),
        )

    unit_of_work = SqliteUnitOfWork(engine, capability_factory)

    def _accounting(transaction: object) -> SqlRunCostAccounting:
        return SqlRunCostAccounting(
            ledger=transaction.cost,  # type: ignore[attr-defined]
            plan_provider=DefaultRunBudgetPlanProvider(
                ledger=transaction.cost,  # type: ignore[attr-defined]
                clock=clock,
            ),
            settlement_provider=ConservativeAttemptUsageProvider(),
            clock=clock,
        )

    def bind_commands(transaction: object) -> RunCommandCapabilities:
        accounting = _accounting(transaction)
        return RunCommandCapabilities(
            runs=transaction.runs,  # type: ignore[attr-defined]
            registry=registry,
            admission=accounting,
            publication=WorkerCommandPublicationGateway(
                audit_gate=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[attr-defined]
                chain_id=run_audit_chain_id,
            ),
            accounting=accounting,
        )

    def bind_lifecycle(transaction: object) -> RunLifecycleCapabilities:
        audit_gate = AuditGate(sink=transaction.audit, clock=clock)  # type: ignore[attr-defined]
        publication = TerminalPublisher(
            registry=registry,
            artifacts=WorkerArtifactPort(
                artifacts=transaction.artifacts,  # type: ignore[attr-defined]
                object_bindings=transaction.object_bindings,  # type: ignore[attr-defined]
                registry=blob_registry,
            ),
            blobs=WorkerBlobStore(object_store, blob_registry),
            findings=transaction.findings,  # type: ignore[attr-defined]
            ledger=WorkerManifestLedger(transaction.runs),  # type: ignore[attr-defined]
            audit=WorkerAuditPort(audit_gate=audit_gate, chain_id=run_audit_chain_id),
        )
        return RunLifecycleCapabilities(
            runs=transaction.runs,  # type: ignore[attr-defined]
            registry=registry,
            accounting=_accounting(transaction),
            publication=publication,
        )

    claim_service = RunCommandService(
        unit_of_work=unit_of_work,
        bind_capabilities=bind_commands,
        clock=clock,
    )
    lifecycle = RunLifecycleService(
        unit_of_work=unit_of_work,
        bind_capabilities=bind_lifecycle,
        clock=clock,
    )
    terminal = WorkerTerminalPublisher(lifecycle, notify=notify)

    def _read_run_revision(run_id: str) -> int:
        with Session(engine) as session:
            run = SqlRunRepository(session).get(run_id)
            if run is None:
                raise IntegrityViolation("attempt terminal fence Run disappeared", run_id=run_id)
            return run.revision

    def _model_bridge_factory(
        *, run: RunRecord, attempt: RunAttempt, lease: RunLease
    ) -> WorkerModelBridgePort:
        del run, attempt, lease
        return _DeferredModelBridge()

    runner = AttemptRunner(
        executor_pool=runtime.executor_pool,
        control_pool=runtime.control_pool,
        resolve_executor=build_executor_resolver(registry, runtime.components),
        model_bridge_factory=_model_bridge_factory,
        terminal=terminal,
        read_run_revision=_read_run_revision,
        worker_actor=runtime.worker_actor,
    )

    def _heartbeat_factory(
        *, run: RunRecord, attempt: RunAttempt, lease: RunLease
    ) -> LeaseHeartbeat:
        return LeaseHeartbeat(
            lifecycle=lifecycle,
            pool=runtime.control_pool,
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            lease_id=lease.lease_id,
            fencing_token=attempt.fencing_token,
            lease_duration_ns=config.lease_duration_ns,
            interval_s=config.heartbeat_interval_s,
            initial_lease_version=lease.lease_version,
            # The permit group is minted at claim time at revision 1; a fast
            # deterministic attempt completes before the first heartbeat interval
            # elapses, so no renewal fires and the initial revision is unused.
            initial_permit_revision=1,
            worker_actor=runtime.worker_actor,
        )

    def _on_contention(op: str, exc: BaseException) -> None:
        with runtime.tracer.span(
            "worker.dispatch.contention",
            attributes={"op": op, "exception": type(exc).__name__},
        ):
            pass

    return RunDispatcher(
        claim_service=claim_service,
        lifecycle=lifecycle,
        reaper_scan=build_reaper_scan(engine),
        runner=runner,
        heartbeat_factory=_heartbeat_factory,
        control_pool=runtime.control_pool,
        clock=clock,
        worker_actor=runtime.worker_actor,
        reaper_actor=runtime.reaper_actor,
        lease_duration_ns=config.lease_duration_ns,
        heartbeat_interval_s=config.heartbeat_interval_s,
        reaper_limit=config.reaper_limit,
        poll_interval_s=config.poll_interval_s,
        on_contention=_on_contention,
    )


def build_worker_process(
    config: LocalWorkerConfig,
    *,
    notify: Callable[[str], None] | None = None,
) -> WorkerProcess:
    """Build the whole worker process: shared authority + trusted components + loop."""

    if not isinstance(config, LocalWorkerConfig):
        raise WorkerConfigurationError("local worker requires an exact LocalWorkerConfig")
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
    blob_registry = BlobLocationRegistry()
    terminal_cursor_key = _derive_key(config.root_secret, "worker-terminal-cursor")
    registry = build_builtin_registry()
    blobs = WorkerArtifactBlobReader(
        engine=engine,
        object_store=object_store,
        object_store_id=config.object_store_id,
        cursor_signing_key=terminal_cursor_key,
        clock=clock,
    )
    store = WorkerPreparedArtifactStore(object_store, blob_registry)
    components = build_trusted_components(registry=registry, blobs=blobs, store=store)
    runtime = build_worker_runtime(
        config,
        trusted_components=components,
        engine=engine,
        object_store=object_store,
    )
    dispatcher = build_worker_dispatch(
        runtime=runtime,
        registry=runtime.registry,
        blob_registry=blob_registry,
        terminal_cursor_signing_key=terminal_cursor_key,
        notify=notify,
    )
    return WorkerProcess(
        runtime=runtime,
        dispatcher=dispatcher,
        components=components,
        blob_registry=blob_registry,
    )


__all__ = [
    "WORKER_RUN_AUDIT_CHAIN_ID",
    "WorkerProcess",
    "build_worker_dispatch",
    "build_worker_process",
]
