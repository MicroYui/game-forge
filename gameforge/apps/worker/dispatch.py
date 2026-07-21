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
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC
from email.utils import parsedate_to_datetime
import math

import httpx

from sqlalchemy.orm import Session

from gameforge.apps.operational_metrics import install_builtin_operational_metrics
from gameforge.apps.worker.app import (
    LocalWorkerConfig,
    WORKER_RUN_AUDIT_CHAIN_ID,
    WorkerConfigurationError,
    WorkerRuntime,
    _derive_key,
    _note_cleanup_failure,
    build_executor_resolver,
    build_reaper_scan,
    build_timeout_scan,
    build_worker_registry,
    build_worker_runtime,
)
from gameforge.apps.worker.agent_drafts import (
    WorkerAgentDraftGovernanceRefs,
    build_agent_draft_capabilities,
    build_agent_draft_workflow_port,
)
from gameforge.apps.worker.agent_prompt_context import (
    bind_production_agent_prompt_context_authority,
)
from gameforge.apps.worker.auto_apply import (
    build_transaction_auto_apply_validation_port,
)
from gameforge.apps.worker.components import (
    WorkerArtifactBlobReader,
    WorkerPreparedArtifactStore,
    build_rollback_ports,
    build_trusted_components,
)
from gameforge.apps.worker.config_export import build_aureus_config_exporter
from gameforge.bench.payload_codec import BENCH_PAYLOAD_DECODERS
from gameforge.apps.worker.dispatcher import RunDispatcher
from gameforge.apps.worker.executor import WorkerModelBridgePort
from gameforge.apps.worker.heartbeat import LeaseHeartbeat
from gameforge.apps.worker.artifact_replay_bridge import (
    ArtifactReplayModelBridge,
    WorkerReplayRoutePublisher,
)
from gameforge.apps.worker.cost_bridge import (
    WorkerAgentStepCostGateway,
    WorkerCallCostGateway,
    WorkerConservativeAttemptUsageProvider,
)
from gameforge.apps.worker.model_authority import (
    WorkerModelExecutionAuthorities,
    WorkerModelSnapshotResolver,
)
from gameforge.apps.worker.model_bridge import WorkerModelBridge
from gameforge.apps.worker.prompt_rendering import CanonicalPromptRendererAuthority
from gameforge.apps.worker.publication import (
    AgentPromptContextMaterialRegistry,
    FencedToolPromptSourceAuthority,
    PromptRenderMaterialRegistry,
    WorkerArtifactPort,
    WorkerAgentPromptContextPublisher,
    WorkerAuditPort,
    WorkerBlobStager,
    WorkerBlobStore,
    WorkerCommandPublicationGateway,
    WorkerCommandTerminalPublicationGateway,
    WorkerManifestLedger,
    WorkerPromptRenderPublisher,
)
from gameforge.apps.worker.replay import ArtifactReplayLoader, LegacyArtifactReplaySource
from gameforge.apps.worker.response_publication import WorkerResponseConsumptionPublisher
from gameforge.apps.worker.routing_bridge import (
    PersistedArtifactResourceDomainResolver,
    PreparedWorkerRoute,
    WorkerRoutingDecider,
)
from gameforge.apps.worker.terminal import WorkerTerminalPublisher
from gameforge.apps.worker.task_suite import build_scenario_shaper_resolver
from gameforge.contracts.canonical import sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.cost import PriceBook
from gameforge.contracts.jobs import RunAttempt, RunLease, RunRecord
from gameforge.contracts.lineage import ArtifactV2
from gameforge.contracts.model_router import ModelRequestV2, ModelSnapshot, request_hash
from gameforge.contracts.reliability import (
    FailureClassificationV1,
    RetryPolicyV1,
)
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.contracts.storage import UtcClock
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.platform.runs.replay import MAX_REPLAY_ARTIFACT_BYTES
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.approvals.commands import ApprovalCommandService
from gameforge.platform.cost_policy.run_accounting import SqlRunCostAccounting
from gameforge.platform.publication import TerminalPublisher
from gameforge.platform.playtest_payload_schemas import (
    PlaytestPayloadValidationService,
    build_builtin_playtest_payload_validators,
)
from gameforge.platform.registry import TrustedComponentMaps
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.provenance import build_source_kind_registry
from gameforge.platform.runs.admission import (
    DefaultRunBudgetPlanProvider,
)
from gameforge.platform.runs.commands import RunCommandCapabilities, RunCommandService
from gameforge.platform.runs.lifecycle import (
    RunLifecycleCapabilities,
    RunLifecycleService,
)
from gameforge.apps.worker.runner import AttemptRunner
from gameforge.runtime.cassette.legacy_import import LegacyImportAuthority
from gameforge.runtime.clock import SystemMonotonicClock, SystemUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.cost.price_book import UnavailablePriceBook
from gameforge.runtime.model_router.cache import ExactResponseCache, ResponseCacheBinding
from gameforge.runtime.model_router.prefix_cache import CatalogPrefixCacheAdmission
from gameforge.runtime.model_router.m4_router import M4ModelRouter
from gameforge.runtime.model_router.router import RouterMode
from gameforge.runtime.model_router.typed_transport import TypedLlmTransport
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.findings import SqlFindingRepository
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from gameforge.runtime.reliability.breaker import CircuitBreaker
from gameforge.runtime.reliability.retry import RetryExecutor, SystemSleeper


def _require_failure_classifier(
    registry: ImmutablePlatformRegistry,
    run: RunRecord,
):
    classifier = registry.get_failure_classifier(run.failure_classifier)
    if classifier is None:
        raise IntegrityViolation("Run failure classifier is absent from exact worker authority")
    return classifier


class _NoModelBridge:
    """Explicit non-model capability for ``not_applicable`` Runs."""

    def call_model(self, request: object) -> object:
        del request
        raise IntegrityViolation("not_applicable Run cannot invoke a model")

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ) -> ModelSnapshot:
        del catalog_version, catalog_digest, model_snapshot_id
        raise IntegrityViolation("not_applicable Run has no model snapshot authority")


class _NoNetworkTransport:
    def complete(self, request: ModelRequestV2) -> object:
        del request
        raise IntegrityViolation("REPLAY cannot access a provider transport")


class _DisabledLooseCassetteStore:
    """Production RECORD capture is Artifact/UoW-owned, never a loose file store."""

    def replay_native(self, key: object) -> object:
        del key
        raise IntegrityViolation("loose cassette replay is disabled")

    def record_native(self, key: object, record: object) -> None:
        del key, record
        raise IntegrityViolation("loose cassette recording is disabled")


class _ProviderFailureClassifier:
    """Closed provider-transport classifier bound to the retained policy version."""

    def __init__(
        self,
        *,
        version: str,
        honor_retry_after: bool,
        clock: UtcClock,
    ) -> None:
        if not version:
            raise IntegrityViolation("routing policy failure classifier version is empty")
        self.version = version
        self._honor_retry_after = honor_retry_after
        self._clock = clock

    def classify(self, error: BaseException) -> FailureClassificationV1:
        retry_after = None
        status: int | None = None
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            if self._honor_retry_after:
                raw = error.response.headers.get("retry-after")
                if raw is not None:
                    retry_after = _parse_retry_after_s(raw, clock=self._clock)
        if status == 429:
            return FailureClassificationV1(
                failure_kind="quota",
                retryable=False,
                counts_for_breaker=False,
                idempotency_required=False,
                reason_code="provider_quota_rejected",
            )
        if isinstance(error, (TimeoutError, httpx.TimeoutException, httpx.TransportError)) or (
            status == 408 or (status is not None and status >= 500)
        ):
            return FailureClassificationV1(
                failure_kind="transient_infrastructure",
                retryable=True,
                counts_for_breaker=True,
                idempotency_required=True,
                reason_code="provider_transport_transient",
                retry_after_s=retry_after,
            )
        if status in {401, 403}:
            return FailureClassificationV1(
                failure_kind="authentication",
                retryable=False,
                counts_for_breaker=False,
                idempotency_required=False,
                reason_code="provider_authentication_rejected",
            )
        if isinstance(error, IntegrityViolation):
            return FailureClassificationV1(
                failure_kind="validation",
                retryable=False,
                counts_for_breaker=False,
                idempotency_required=False,
                reason_code="local_transport_integrity",
            )
        if isinstance(error, (ValueError, TypeError)):
            return FailureClassificationV1(
                failure_kind="validation",
                retryable=False,
                counts_for_breaker=False,
                idempotency_required=False,
                reason_code="local_transport_request_invalid",
            )
        if status is not None and 400 <= status < 500:
            return FailureClassificationV1(
                failure_kind="validation",
                retryable=False,
                counts_for_breaker=False,
                idempotency_required=False,
                reason_code="provider_request_rejected",
            )
        return FailureClassificationV1(
            failure_kind="permanent_infrastructure",
            retryable=False,
            counts_for_breaker=True,
            idempotency_required=False,
            reason_code="provider_transport_unclassified",
        )


_MAX_RETRY_AFTER_S = 315_576_000_000


def _parse_retry_after_s(raw: str, *, clock: UtcClock) -> int | None:
    """Parse RFC Retry-After without retaining or exposing the provider header."""

    value = raw.strip()
    if value.isascii() and value.isdigit():
        if len(value) > len(str(_MAX_RETRY_AFTER_S)):
            return _MAX_RETRY_AFTER_S
        return min(int(value), _MAX_RETRY_AFTER_S)
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        return None
    now = clock.now_utc()
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        raise IntegrityViolation("provider failure classifier clock must return UTC")
    seconds = max(0, math.ceil((target.astimezone(UTC) - now.astimezone(UTC)).total_seconds()))
    return min(seconds, _MAX_RETRY_AFTER_S)


class _MissingModelSnapshotAuthority:
    def get_model_snapshot(self, model_snapshot_id: str) -> None:
        del model_snapshot_id
        return None


class _SqlRoutingAuthority:
    def __init__(self, *, engine: object, clock: UtcClock, unit_of_work: object) -> None:
        self._engine = engine
        self._clock = clock
        self._unit_of_work = unit_of_work

    def get_routing_decision(self, decision_id: str) -> object | None:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            return SqlCostLedger(session, clock=self._clock).get_routing_decision(decision_id)

    def get_model_catalog(self, catalog_version: int, catalog_digest: str) -> object | None:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            return SqlCostLedger(session, clock=self._clock).get_model_catalog(
                catalog_version,
                catalog_digest,
            )

    def get_legacy_import_routing_decision(self, decision_id: str) -> object | None:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            return SqlCostLedger(session, clock=self._clock).get_legacy_import_routing_decision(
                decision_id
            )

    def put_legacy_import_routing_decision(self, decision: object) -> object:
        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            return transaction.cost.put_legacy_import_routing_decision(decision)


class _SqlReplayReader:
    """Session-per-read replay authority safe across executor restarts/threads."""

    def __init__(
        self,
        *,
        engine: object,
        object_store: object,
        object_store_id: str,
        cursor_signing_key: bytes,
        clock: UtcClock,
    ) -> None:
        self._engine = engine
        self._object_store = object_store
        self._object_store_id = object_store_id
        self._cursor_signing_key = cursor_signing_key
        self._clock = clock

    def _repositories(self, session: Session) -> tuple[object, object, object, object]:
        bindings = SqlObjectBindingRepository(
            session,
            self._object_store,  # type: ignore[arg-type]
            self._object_store_id,
        )
        artifacts = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(
                signing_key=self._cursor_signing_key,
                clock=self._clock,
            ),
            clock=self._clock,
        )
        return (
            artifacts,
            bindings,
            SqlRunRepository(session),
            SqlCostLedger(
                session,
                clock=self._clock,
            ),
        )

    def get_artifact(self, artifact_id: str) -> ArtifactV2 | None:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            artifacts, _, _, _ = self._repositories(session)
            value = artifacts.get(artifact_id)  # type: ignore[attr-defined]
            return value if isinstance(value, ArtifactV2) else None

    def read_artifact_bytes(self, artifact_id: str) -> bytes:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            artifacts, bindings, _, _ = self._repositories(session)
            artifact = artifacts.get(artifact_id)  # type: ignore[attr-defined]
            if not isinstance(artifact, ArtifactV2):
                raise FileNotFoundError(artifact_id)
            binding = bindings.resolve(artifact.object_ref)  # type: ignore[attr-defined]
            with self._object_store.open(binding.location) as stream:  # type: ignore[attr-defined]
                payload = stream.read(MAX_REPLAY_ARTIFACT_BYTES + 1)
            if len(payload) > MAX_REPLAY_ARTIFACT_BYTES:
                raise IntegrityViolation("replay Artifact exceeds the worker byte limit")
            return payload

    def read_prompt_source_bytes(self, expected: ArtifactV2) -> bytes:
        """Read no more than the preflighted ObjectRef size for one prompt source."""

        with Session(self._engine) as session:  # type: ignore[arg-type]
            artifacts, bindings, _, _ = self._repositories(session)
            artifact = artifacts.get(expected.artifact_id)  # type: ignore[attr-defined]
            if not isinstance(artifact, ArtifactV2) or artifact != expected:
                raise IntegrityViolation(
                    "prompt source Artifact changed after metadata preflight",
                    artifact_id=expected.artifact_id,
                )
            binding = bindings.resolve(artifact.object_ref)  # type: ignore[attr-defined]
            with self._object_store.open(binding.location) as stream:  # type: ignore[attr-defined]
                payload = stream.read(artifact.object_ref.size_bytes + 1)
            if (
                len(payload) != artifact.object_ref.size_bytes
                or sha256_lowerhex(payload) != artifact.payload_hash
            ):
                raise IntegrityViolation(
                    "prompt source bytes differ from preflighted immutable ObjectRef",
                    artifact_id=artifact.artifact_id,
                )
            return payload

    def get_run(self, run_id: str) -> object | None:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            _, _, runs, _ = self._repositories(session)
            return runs.get(run_id)  # type: ignore[attr-defined]

    def get_attempt(self, run_id: str, attempt_no: int) -> object | None:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            _, _, runs, _ = self._repositories(session)
            return runs.get_attempt(run_id, attempt_no)  # type: ignore[attr-defined]

    def get_prompt_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> object | None:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            _, _, runs, _ = self._repositories(session)
            return runs.get_intermediate_link(  # type: ignore[attr-defined]
                run_id, attempt_no, call_ordinal, route_ordinal
            )

    def get_routing_decision(self, decision_id: str) -> object | None:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            _, _, _, cost = self._repositories(session)
            return cost.get_routing_decision(decision_id)  # type: ignore[attr-defined]

    def get_model_route_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> object | None:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            _, _, runs, _ = self._repositories(session)
            return runs.get_model_route_link(  # type: ignore[attr-defined]
                run_id, attempt_no, call_ordinal, route_ordinal
            )

    def get_model_response_consumption(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> object | None:
        with Session(self._engine) as session:  # type: ignore[arg-type]
            _, _, runs, _ = self._repositories(session)
            return runs.get_model_response_consumption(  # type: ignore[attr-defined]
                run_id, attempt_no, call_ordinal, route_ordinal
            )


@dataclass(frozen=True, slots=True)
class WorkerProcess:
    """The composed worker process: shared authority + the driven dispatch loop."""

    runtime: WorkerRuntime
    dispatcher: RunDispatcher
    components: TrustedComponentMaps

    def close(self) -> None:
        self.runtime.close()


def build_worker_dispatch(
    *,
    runtime: WorkerRuntime,
    registry: ImmutablePlatformRegistry,
    terminal_cursor_signing_key: bytes,
    run_audit_chain_id: str = WORKER_RUN_AUDIT_CHAIN_ID,
    notify: Callable[[str], None] | None = None,
    model_transport: TypedLlmTransport | None = None,
    model_snapshot_authority: object | None = None,
    prompt_renderer_authority: CanonicalPromptRendererAuthority | None = None,
    price_book: PriceBook | None = None,
    legacy_import_authority: LegacyImportAuthority | None = None,
    model_circuit_breaker_resolver: (Callable[[RoutingDecisionV1], CircuitBreaker] | None) = None,
) -> RunDispatcher:
    """Assemble the fenced dispatch loop over the worker runtime's shared authority."""

    clock: UtcClock = SystemUtcClock()
    engine = runtime.engine
    object_store = runtime.object_store
    object_store_id = runtime.config.object_store_id
    config = runtime.config
    call_price_book = price_book or UnavailablePriceBook()

    def _cursor_signer() -> CursorSigner:
        return CursorSigner(signing_key=terminal_cursor_signing_key, clock=clock)

    def capability_factory(session: Session) -> TransactionCapabilities:
        cursor_signer = _cursor_signer()
        object_bindings = SqlObjectBindingRepository(session, object_store, object_store_id)
        return TransactionCapabilities(
            refs=SqlRefStore(session, cursor_signer=cursor_signer, clock=clock),
            audit=SqlAuditSink(session),
            approvals=SqlApprovalRepository(session),
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
            idempotency=SqlIdempotencyRepository(session, clock=clock),
            policies=SqlPolicySnapshotRepository(session, clock=clock),
        )

    unit_of_work = SqliteUnitOfWork(engine, capability_factory)

    governance_values = (
        config.role_policy_version,
        config.role_policy_digest,
        config.workflow_route_policy_version,
        config.workflow_route_policy_digest,
        config.workflow_approval_policy_version,
        config.workflow_approval_policy_digest,
    )
    if all(value is None for value in governance_values):
        agent_draft_governance = None
    else:
        # LocalWorkerConfig enforces all-or-none and validates the exact digests.
        assert all(isinstance(value, str) for value in governance_values)
        agent_draft_governance = WorkerAgentDraftGovernanceRefs(
            role_policy_version=config.role_policy_version,  # type: ignore[arg-type]
            role_policy_digest=config.role_policy_digest,  # type: ignore[arg-type]
            route_policy_version=config.workflow_route_policy_version,  # type: ignore[arg-type]
            route_policy_digest=config.workflow_route_policy_digest,  # type: ignore[arg-type]
            approval_policy_version=config.workflow_approval_policy_version,  # type: ignore[arg-type]
            approval_policy_digest=config.workflow_approval_policy_digest,  # type: ignore[arg-type]
        )

    agent_draft_commands = ApprovalCommandService(
        unit_of_work=unit_of_work,
        bind_capabilities=lambda transaction: build_agent_draft_capabilities(
            transaction=transaction,
            object_store=object_store,
            clock=clock,
        ),
        clock=clock,
        audit_chain_id=run_audit_chain_id,
    )

    def _accounting(transaction: object) -> SqlRunCostAccounting:
        return SqlRunCostAccounting(
            ledger=transaction.cost,  # type: ignore[attr-defined]
            plan_provider=DefaultRunBudgetPlanProvider(
                ledger=transaction.cost,  # type: ignore[attr-defined]
                clock=clock,
            ),
            settlement_provider=WorkerConservativeAttemptUsageProvider(
                ledger=transaction.cost,  # type: ignore[attr-defined]
                price_book=call_price_book,
            ),
            clock=clock,
        )

    def _terminal_publisher(transaction: object) -> TerminalPublisher:
        audit_gate = AuditGate(sink=transaction.audit, clock=clock)  # type: ignore[attr-defined]
        return TerminalPublisher(
            registry=registry,
            artifacts=WorkerArtifactPort(
                artifacts=transaction.artifacts,  # type: ignore[attr-defined]
                object_bindings=transaction.object_bindings,  # type: ignore[attr-defined]
                object_store=object_store,
            ),
            blobs=WorkerBlobStore(object_store),
            findings=transaction.findings,  # type: ignore[attr-defined]
            ledger=WorkerManifestLedger(
                transaction.runs,  # type: ignore[attr-defined]
                transaction.cost,  # type: ignore[attr-defined]
                artifacts=transaction.artifacts,  # type: ignore[attr-defined]
                object_bindings=transaction.object_bindings,  # type: ignore[attr-defined]
                object_store=object_store,
            ),
            audit=WorkerAuditPort(audit_gate=audit_gate, chain_id=run_audit_chain_id),
            approvals=transaction.approvals,  # type: ignore[attr-defined]
            payload_decoders=BENCH_PAYLOAD_DECODERS,
            playtest_payload_validator=PlaytestPayloadValidationService(
                registry=registry,
                validators=runtime.components.playtest_payload_validators,
            ),
            agent_drafts=build_agent_draft_workflow_port(
                transaction=transaction,
                object_store=object_store,
                clock=clock,
                commands=agent_draft_commands,
                governance_refs=agent_draft_governance,
            ),
            auto_apply=build_transaction_auto_apply_validation_port(
                transaction=transaction,
                object_store=object_store,
                registry=registry,
            ),
        )

    @contextmanager
    def planning_scope():
        """Short read-only authority scope; never ``BEGIN IMMEDIATE``."""

        with Session(engine) as session:
            connection = session.connection()
            connection.exec_driver_sql("PRAGMA query_only = ON")
            try:
                yield capability_factory(session)
            finally:
                try:
                    connection.exec_driver_sql("PRAGMA query_only = OFF")
                finally:
                    session.rollback()

    prompt_materials = PromptRenderMaterialRegistry(
        max_entries=config.max_concurrency or config.max_workers
    )
    context_materials = AgentPromptContextMaterialRegistry(
        max_entries=config.max_concurrency or config.max_workers
    )
    # Prompt bindings are explicit retained production authority.  Task-specific
    # handler composition registers them; an absent binding must fail closed and
    # may never fall back to trusting handler-supplied messages.
    prompt_renderer = prompt_renderer_authority or CanonicalPromptRendererAuthority(
        source_kind_registries=(build_source_kind_registry(),), bindings=()
    )
    required_prompt_plan_keys = tuple(
        sorted(
            {
                (node.agent_node_id, node.prompt_version, node.tool_version)
                for graph in registry.list_agent_execution_graphs()
                if graph.status in {"active", "replay_only"}
                for node in graph.nodes
            }
        )
    )
    prompt_renderer = bind_production_agent_prompt_context_authority(
        prompt_renderer,
        required_plan_keys=required_prompt_plan_keys,
    )

    def bind_commands(transaction: object) -> RunCommandCapabilities:
        accounting = _accounting(transaction)
        command_audit = WorkerCommandPublicationGateway(
            audit_gate=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[attr-defined]
            chain_id=run_audit_chain_id,
            runs=transaction.runs,  # type: ignore[attr-defined]
            artifacts=transaction.artifacts,  # type: ignore[attr-defined]
            object_bindings=transaction.object_bindings,  # type: ignore[attr-defined]
            object_store=object_store,
            idempotency=transaction.idempotency,  # type: ignore[attr-defined]
            prompt_materials=prompt_materials,
            context_materials=context_materials,
        )
        return RunCommandCapabilities(
            runs=transaction.runs,  # type: ignore[attr-defined]
            registry=registry,
            admission=accounting,
            publication=WorkerCommandTerminalPublicationGateway(
                commands=command_audit,
                terminal=_terminal_publisher(transaction),
            ),
            accounting=accounting,
        )

    def bind_lifecycle(transaction: object) -> RunLifecycleCapabilities:
        return RunLifecycleCapabilities(
            runs=transaction.runs,  # type: ignore[attr-defined]
            registry=registry,
            accounting=_accounting(transaction),
            publication=_terminal_publisher(transaction),
        )

    blob_stager = WorkerBlobStager(object_store)

    claim_service = RunCommandService(
        unit_of_work=unit_of_work,
        bind_capabilities=bind_commands,
        clock=clock,
        planning_scope=planning_scope,
        bind_planning_capabilities=bind_commands,
        stage_publications=blob_stager,
    )
    lifecycle = RunLifecycleService(
        unit_of_work=unit_of_work,
        bind_capabilities=bind_lifecycle,
        clock=clock,
        planning_scope=planning_scope,
        bind_planning_capabilities=bind_lifecycle,
        stage_publications=blob_stager,
    )
    terminal = WorkerTerminalPublisher(lifecycle, notify=notify)

    routing_authority = _SqlRoutingAuthority(
        engine=engine,
        clock=clock,
        unit_of_work=unit_of_work,
    )
    replay_reader = _SqlReplayReader(
        engine=engine,
        object_store=object_store,
        object_store_id=object_store_id,
        cursor_signing_key=terminal_cursor_signing_key,
        clock=clock,
    )
    snapshot_resolver = WorkerModelSnapshotResolver(
        unit_of_work=unit_of_work,
        snapshots=model_snapshot_authority or _MissingModelSnapshotAuthority(),
    )

    def _source_artifact_loader(artifact_id: str) -> ArtifactV2:
        artifact = replay_reader.get_artifact(artifact_id)
        if not isinstance(artifact, ArtifactV2):
            raise IntegrityViolation(
                "prompt source Artifact authority is unavailable",
                artifact_id=artifact_id,
            )
        return artifact

    def _source_payload_loader(artifact: ArtifactV2) -> bytes:
        return replay_reader.read_prompt_source_bytes(artifact)

    def _tool_intermediate_for_call(
        run_id: str,
        attempt_no: int,
        target_call_ordinal: int,
    ):
        with Session(engine) as session:
            return SqlRunRepository(session).get_tool_intermediate_for_call(
                run_id,
                attempt_no,
                target_call_ordinal,
            )

    def _model_call_projection(
        fence: AttemptWriteFence,
        call_ordinal: int,
        route_ordinal: int,
    ):
        with Session(engine) as session:
            authority = SqlRunRepository(session).get_model_call_write_authority(
                fence,
                call_ordinal=call_ordinal,
                route_ordinal=route_ordinal,
            )
            if authority is None:
                return None
            return (
                authority.route_links[-1],
                authority.consumption,
            )

    prompt_source_authority = FencedToolPromptSourceAuthority(
        tool_link_loader=_tool_intermediate_for_call,
        artifact_loader=_source_artifact_loader,
        payload_loader=_source_payload_loader,
        call_projection_loader=_model_call_projection,
    )

    def _retry_executor(run: RunRecord) -> RetryExecutor:
        plan = run.payload.execution_version_plan
        retry = registry.get_retry_policy(run.retry_policy)
        if plan is None or retry is None:
            raise IntegrityViolation("model Run lacks exact retry/route authority")
        with Session(engine) as session:
            policy = SqlCostLedger(session, clock=clock).get_routing_policy(
                plan.routing_policy_version,
                plan.routing_policy_digest,
            )
        if policy is None:
            raise IntegrityViolation("model Run routing policy history is unavailable")
        if retry.jitter_policy not in {
            "none@1",
            "deterministic-request-hash@1",
        }:
            raise IntegrityViolation("model retry jitter policy is unsupported")
        classifier = _ProviderFailureClassifier(
            version=policy.failure_classifier_version,
            honor_retry_after=retry.honor_retry_after,
            clock=clock,
        )
        return RetryExecutor(
            policy=RetryPolicyV1(
                policy_version=(
                    f"{retry.retry_policy_id}@{retry.retry_policy_version}:"
                    f"{retry.retry_policy_digest}"
                ),
                failure_classifier_version=classifier.version,
                max_attempts=retry.max_attempts,
                initial_backoff_ms=retry.base_delay_ms,
                max_backoff_ms=retry.max_delay_ms,
                multiplier=2 if retry.backoff == "exponential" else 1,
                # The retained lifecycle policy freezes the deterministic jitter
                # algorithm but no amplitude. Zero is the only non-invented value.
                jitter_ratio=0,
            ),
            classifier=classifier,
            utc_clock=clock,
            monotonic_clock=SystemMonotonicClock(),
            sleeper=SystemSleeper(),
            jitter=lambda: 0.0,
        )

    def _read_run_revision(run_id: str) -> int:
        with Session(engine) as session:
            authority = SqlRunRepository(session).get_run_write_authority(run_id)
            if authority is None:
                raise IntegrityViolation("attempt terminal fence Run disappeared", run_id=run_id)
            return authority[0].revision

    def _model_bridge_factory(
        *, run: RunRecord, attempt: RunAttempt, lease: RunLease
    ) -> WorkerModelBridgePort:
        mode = run.payload.llm_execution_mode
        if mode == "not_applicable":
            return _NoModelBridge()
        fence = AttemptWriteFence(
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            expected_run_revision=run.revision,
            lease_id=lease.lease_id,
            fencing_token=attempt.fencing_token,
        )
        # Full-response cache entries are attempt-local replay optimizations.  Never
        # accept an injected/shared instance: doing so would let one Run consume
        # another Run's response authority despite an otherwise exact binding.
        response_cache = ExactResponseCache()
        prompt_publisher = WorkerPromptRenderPublisher(
            run=run,
            fence=fence,
            commands=claim_service,
            object_store=object_store,
            registry=prompt_materials,
            clock=clock,
            source_artifact_loader=_source_artifact_loader,
            source_payload_loader=_source_payload_loader,
            prompt_renderer=prompt_renderer,
            source_authority=prompt_source_authority,
        )
        context_publisher = WorkerAgentPromptContextPublisher(
            run=run,
            fence=fence,
            commands=claim_service,
            object_store=object_store,
            registry=context_materials,
            clock=clock,
            source_artifact_loader=_source_artifact_loader,
        )
        cost = WorkerCallCostGateway(
            unit_of_work=unit_of_work,
            run=run,
            attempt=attempt,
            fence=fence,
            actor=runtime.worker_actor,
            clock=clock,
            price_book=call_price_book,
        )
        step_cost = WorkerAgentStepCostGateway(
            unit_of_work=unit_of_work,
            run=run,
            attempt=attempt,
            fence=fence,
            actor=runtime.worker_actor,
            clock=clock,
        )
        response_publisher = WorkerResponseConsumptionPublisher(
            unit_of_work=unit_of_work,
            run=run,
            cost=cost,
            object_store=object_store,
            clock=clock,
            audit_chain_id=run_audit_chain_id,
        )
        if mode in {"live", "record"}:
            if model_circuit_breaker_resolver is None:
                raise IntegrityViolation(
                    "online model execution lacks dependency-scoped circuit-breaker authority"
                )
            decider = WorkerRoutingDecider(
                unit_of_work=unit_of_work,
                run=run,
                attempt=attempt,
                fence=fence,
                actor=runtime.worker_actor,
                clock=clock,
                audit_chain_id=run_audit_chain_id,
                domain_resolver=PersistedArtifactResourceDomainResolver(),
            )
            router = M4ModelRouter(
                transport=model_transport or _NoNetworkTransport(),  # type: ignore[arg-type]
                store=_DisabledLooseCassetteStore(),  # type: ignore[arg-type]
                cache=response_cache,
                # RECORD shards are published atomically by response_publisher;
                # RouterMode.RECORD would create a second loose-file authority.
                mode=RouterMode.PASSTHROUGH,
                retry_executor=_retry_executor(run),
                decision_authority=routing_authority,  # type: ignore[arg-type]
                circuit_breaker_resolver=model_circuit_breaker_resolver,
                prefix_cache_admission=CatalogPrefixCacheAdmission(
                    catalog_authority=routing_authority,  # type: ignore[arg-type]
                    allowed_policy_versions=(prompt_renderer.allowed_prefix_policy_versions),
                ),
            )

            def select_execution_source(
                request: ModelRequestV2,
                prepared: object,
            ) -> str:
                if not isinstance(prepared, PreparedWorkerRoute):
                    raise IntegrityViolation("cache selection lacks an exact prepared route")
                binding = ResponseCacheBinding(
                    request_hash=request_hash(request),
                    model_snapshot=prepared.model_snapshot_id,
                    catalog_version=prepared.catalog_version,
                    catalog_digest=prepared.catalog_digest,
                    policy_version=prepared.policy_version,
                    routing_policy_digest=prepared.routing_policy_digest,
                )
                return (
                    "full_response_cache" if response_cache.get(binding) is not None else "online"
                )

            return WorkerModelBridge(
                run=run,
                attempt=attempt,
                fence=fence,
                execution_source="online",
                execution_source_selector=select_execution_source,  # type: ignore[arg-type]
                prompt_publisher=prompt_publisher,
                context_publisher=context_publisher,
                decider=decider,
                router=router,
                cost=cost,
                step_cost=step_cost,
                model_snapshot_resolver=snapshot_resolver,
                tracer=runtime.tracer,
                clock=clock,
                worker_actor=runtime.worker_actor,
                response_publisher=response_publisher,
            )
        if mode != "replay":
            raise IntegrityViolation("Run has an unsupported LLM execution mode", mode=mode)
        source = ArtifactReplayLoader(
            replay_reader,  # type: ignore[arg-type]
            current_decision_resolver=routing_authority.get_routing_decision,  # type: ignore[arg-type]
            legacy_authority=legacy_import_authority,
            legacy_decisions=routing_authority,  # type: ignore[arg-type]
        ).load(run)
        native_router = None
        if not isinstance(source, LegacyArtifactReplaySource):
            native_router = M4ModelRouter(
                transport=_NoNetworkTransport(),  # type: ignore[arg-type]
                store=source,  # type: ignore[arg-type]
                cache=response_cache,
                mode=RouterMode.REPLAY,
                retry_executor=_retry_executor(run),
                decision_authority=routing_authority,  # type: ignore[arg-type]
            )
        return ArtifactReplayModelBridge(
            run=run,
            attempt=attempt,
            fence=fence,
            source=source,
            prompt_publisher=prompt_publisher,
            context_publisher=context_publisher,
            route_publisher=WorkerReplayRoutePublisher(
                unit_of_work=unit_of_work,
                fence=fence,
                clock=clock,
                audit_chain_id=run_audit_chain_id,
            ),
            native_router=native_router,
            cost=cost,
            step_cost=step_cost,
            response_publisher=response_publisher,
            model_snapshot_resolver=snapshot_resolver,
            tracer=runtime.tracer,
            clock=clock,
            worker_actor=runtime.worker_actor,
        )

    runner = AttemptRunner(
        executor_pool=runtime.executor_pool,
        control_pool=runtime.control_pool,
        resolve_executor=build_executor_resolver(registry, runtime.components),
        model_bridge_factory=_model_bridge_factory,
        terminal=terminal,
        progress=lifecycle,
        read_run_revision=_read_run_revision,
        resolve_failure_classifier=lambda run: _require_failure_classifier(
            registry,
            run,
        ),
        worker_actor=runtime.worker_actor,
    )

    def _heartbeat_factory(
        *, run: RunRecord, attempt: RunAttempt, lease: RunLease
    ) -> LeaseHeartbeat:
        return LeaseHeartbeat(
            lifecycle=lifecycle,
            pool=runtime.heartbeat_pool,
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
        timeout_scan=build_timeout_scan(engine),
        runner=runner,
        heartbeat_factory=_heartbeat_factory,
        control_pool=runtime.control_pool,
        clock=clock,
        worker_actor=runtime.worker_actor,
        reaper_actor=runtime.reaper_actor,
        lease_duration_ns=config.lease_duration_ns,
        tracer=runtime.tracer,
        logger=runtime.logger,
        operational_metrics=install_builtin_operational_metrics(
            store=runtime.telemetry_store,
            clock=clock,
        ),
        heartbeat_interval_s=config.heartbeat_interval_s,
        reaper_limit=config.reaper_limit,
        poll_interval_s=config.poll_interval_s,
        max_in_flight=config.max_concurrency or config.max_workers,
        on_contention=_on_contention,
    )


def build_worker_process(
    config: LocalWorkerConfig,
    *,
    notify: Callable[[str], None] | None = None,
    model_execution_authorities: WorkerModelExecutionAuthorities | None = None,
    model_transport: TypedLlmTransport | None = None,
    model_snapshot_authority: object | None = None,
    prompt_renderer_authority: CanonicalPromptRendererAuthority | None = None,
    price_book: PriceBook | None = None,
    legacy_import_authority: LegacyImportAuthority | None = None,
    model_circuit_breaker_resolver: (Callable[[RoutingDecisionV1], CircuitBreaker] | None) = None,
) -> WorkerProcess:
    """Build the whole worker process: shared authority + trusted components + loop."""

    if not isinstance(config, LocalWorkerConfig):
        raise WorkerConfigurationError("local worker requires an exact LocalWorkerConfig")
    legacy_authority_args = (
        model_transport,
        model_snapshot_authority,
        prompt_renderer_authority,
        price_book,
        legacy_import_authority,
        model_circuit_breaker_resolver,
    )
    if model_execution_authorities is not None:
        if not isinstance(model_execution_authorities, WorkerModelExecutionAuthorities):
            raise WorkerConfigurationError(
                "model_execution_authorities must be an exact authority closure"
            )
        if any(value is not None for value in legacy_authority_args):
            raise WorkerConfigurationError(
                "exact model authority closure cannot be mixed with individual authorities"
            )
        model_transport = model_execution_authorities.transport
        model_snapshot_authority = model_execution_authorities.snapshots
        prompt_renderer_authority = model_execution_authorities.prompt_renderer
        price_book = model_execution_authorities.price_book
        legacy_import_authority = model_execution_authorities.legacy_imports
        model_circuit_breaker_resolver = model_execution_authorities.circuit_breaker_resolver
    clock = SystemUtcClock()
    engine = None
    runtime: WorkerRuntime | None = None
    try:
        engine = get_engine(config.database_url)
        if engine.dialect.name != "sqlite":
            raise WorkerConfigurationError("local worker composition requires SQLite")
        object_store = LocalObjectStore(
            config.object_store_root,
            store_id=config.object_store_id,
            clock=clock,
            cursor_signing_key=_derive_key(config.root_secret, "object-store-cursor"),
        )
        terminal_cursor_key = _derive_key(config.root_secret, "worker-terminal-cursor")
        registry = build_worker_registry(engine, clock=clock)
        playtest_payload_validators = build_builtin_playtest_payload_validators()
        config_exporter = build_aureus_config_exporter(registry)
        task_suite_scenario_shaper_resolver = build_scenario_shaper_resolver(registry)
        required_prompt_plan_keys = tuple(
            sorted(
                {
                    (node.agent_node_id, node.prompt_version, node.tool_version)
                    for graph in registry.list_agent_execution_graphs()
                    if graph.status in {"active", "replay_only"}
                    for node in graph.nodes
                }
            )
        )
        if prompt_renderer_authority is not None:
            prompt_renderer_authority = bind_production_agent_prompt_context_authority(
                prompt_renderer_authority,
                required_plan_keys=required_prompt_plan_keys,
            )
            if model_execution_authorities is not None:
                model_execution_authorities = replace(
                    model_execution_authorities,
                    prompt_renderer=prompt_renderer_authority,
                )
        blobs = WorkerArtifactBlobReader(
            engine=engine,
            object_store=object_store,
            object_store_id=config.object_store_id,
            cursor_signing_key=terminal_cursor_key,
            clock=clock,
        )
        store = WorkerPreparedArtifactStore(object_store)
        rollback_history_verifier, rollback_schema_analyzer = build_rollback_ports(
            engine=engine,
            object_store=object_store,
            object_store_id=config.object_store_id,
            cursor_signing_key=terminal_cursor_key,
            clock=clock,
        )
        components = build_trusted_components(
            registry=registry,
            blobs=blobs,
            store=store,
            playtest_payload_validators=playtest_payload_validators,
            config_exporter=config_exporter,
            task_suite_scenario_shaper_resolver=task_suite_scenario_shaper_resolver,
            rollback_history_verifier=rollback_history_verifier,
            rollback_schema_analyzer=rollback_schema_analyzer,
        )
        runtime = build_worker_runtime(
            config,
            trusted_components=components,
            engine=engine,
            object_store=object_store,
            model_execution_authorities=model_execution_authorities,
            registry=registry,
        )
        dispatcher = build_worker_dispatch(
            runtime=runtime,
            registry=runtime.registry,
            terminal_cursor_signing_key=terminal_cursor_key,
            notify=notify,
            model_transport=model_transport,
            model_snapshot_authority=model_snapshot_authority,
            prompt_renderer_authority=prompt_renderer_authority,
            price_book=price_book,
            legacy_import_authority=legacy_import_authority,
            model_circuit_breaker_resolver=model_circuit_breaker_resolver,
        )
        return WorkerProcess(
            runtime=runtime,
            dispatcher=dispatcher,
            components=components,
        )
    except BaseException as original:
        if runtime is not None:
            try:
                runtime.close()
            except BaseException as cleanup_error:
                _note_cleanup_failure(
                    original,
                    label="worker runtime",
                    error=cleanup_error,
                )
        else:
            if model_execution_authorities is not None:
                transport_close = getattr(
                    model_execution_authorities.transport,
                    "close",
                    None,
                )
                if callable(transport_close):
                    try:
                        transport_close()
                    except BaseException as cleanup_error:
                        _note_cleanup_failure(
                            original,
                            label="model transport",
                            error=cleanup_error,
                        )
            if engine is not None:
                try:
                    engine.dispose()
                except BaseException as cleanup_error:
                    _note_cleanup_failure(
                        original,
                        label="business engine",
                        error=cleanup_error,
                    )
        # Engine.dispose() is intentionally idempotent: build_worker_runtime also
        # cleans an injected engine after partial construction, while this outer
        # owner must still cover failures in its preflight/type-validation window.
        raise


__all__ = [
    "WORKER_RUN_AUDIT_CHAIN_ID",
    "WorkerProcess",
    "build_worker_dispatch",
    "build_worker_process",
]
