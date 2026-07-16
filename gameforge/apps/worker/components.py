"""The canonical M4c trusted-component composition for API readiness + worker dispatch.

``PlatformReadinessValidator`` requires ``TrustedComponentMaps`` to close EXACTLY
against the 14 active RunKind definitions across all six component maps. This module
builds that single canonical map once, so both the API readiness probe
(``apps/api/local.py``) and the persistent worker (``apps/worker``) share identical,
genuinely-closed authority.

The ``executors`` values are the REAL Task-11/12/13 platform handlers (never fakes):
the deterministic ``checker``/``simulation`` handlers, the ``task_suite``/``playtest``
game-composed handlers, the agent-backed generation/repair/constraint handlers, and
the validation handlers — plus the two Task-14 ``DEFERRED_EXECUTORS`` (auto-wrapped by
``build_executor_resolver``). Handler game ports that already have production impls or
production defaults are wired as-is; the small number of game ports without a
production implementation yet (bench case/report ports, rollback analyzers) are bound
to interface-complete, fail-closed *deferred* ports — the executor is still the real
handler, its game body is deferred (project rule: define the contract now, defer the
implementation). Only ``checker.run`` is exercised end-to-end in Task 17a.

The other five maps (``terminal_hooks`` / ``workflow_effects`` / ``profile_handlers`` /
``permission_domain_resolvers`` / ``completion_oracles``) are readiness-closure
allowlists derived from the exact registry; ``completion_oracles`` binds the real
Task-12a oracle executors, and ``workflow_effects`` binds every active key to the
exact callable in ``publication/effects.py``. Mutating handlers still require their
transaction-bound authority ports at execution time and fail closed when absent.
"""

from __future__ import annotations


from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.lineage import ObjectLocation, ObjectRef
from gameforge.contracts.versions import IR_SCHEMA_VERSION
from gameforge.platform.registry import (
    TrustedComponentMaps,
    build_readiness_component_maps,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.run_handlers import (
    BenchRunHandler,
    CheckerRunHandler,
    ConstraintProposalHandler,
    DEFERRED_EXECUTORS,
    DefaultCheckerFactory,
    GenerationProposalHandler,
    RepairSearchHandler,
    ReviewRunHandler,
    SimulationRunHandler,
)
from gameforge.platform.run_handlers.base import ArtifactBlobReader, PreparedArtifactStore
from gameforge.platform.run_handlers.constraint_validation import ConstraintValidationHandler
from gameforge.platform.run_handlers.patch_validation import PatchValidationHandler
from gameforge.platform.run_handlers.review import ReviewSimConfig
from gameforge.platform.run_handlers.rollback_validation import (
    DimensionCheckV1,
    RollbackHistoryRequest,
    RollbackSchemaRequest,
    RollbackTargetInspectionV1,
    RollbackValidationHandler,
)
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.contracts.storage import UtcClock
from gameforge.platform.publication.effects import WORKFLOW_EFFECTS
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.apps.worker.agent_runners import (
    M2ConstraintProposalAgentRunner,
    M2GenerationAgentRunner,
    M2RepairAgentRunner,
)
from gameforge.apps.worker.completion_oracles import build_completion_oracle_executors
from gameforge.apps.worker.config_export import build_aureus_config_exporter
from gameforge.apps.worker.playtest import build_playtest_handler
from gameforge.apps.worker.task_suite import build_task_suite_handler
from gameforge.apps.worker.validation import build_differential_engines
from gameforge.spine.checkers.graph import GraphChecker


# ── executor object-store ports (read committed inputs / write prepared blobs) ──
class WorkerArtifactBlobReader:
    """The handlers' ``ArtifactBlobReader`` over committed input Artifacts.

    Resolves ``artifact_id -> ArtifactV2.object_ref -> ObjectBinding.location`` in a
    fresh short read transaction (the executor runs off-loop on the blocking pool) and
    reads the exact stored bytes. Fails closed if the input Artifact or its binding is
    unavailable, so a missing input becomes a classified attempt failure.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        object_store: LocalObjectStore,
        object_store_id: str,
        cursor_signing_key: bytes,
        clock: UtcClock,
    ) -> None:
        self._engine = engine
        self._object_store = object_store
        self._object_store_id = object_store_id
        self._cursor_signing_key = cursor_signing_key
        self._clock = clock

    def read_bytes(self, artifact_id: str) -> bytes:
        with Session(self._engine) as session:
            bindings = SqlObjectBindingRepository(
                session, self._object_store, self._object_store_id
            )
            artifacts = SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=CursorSigner(signing_key=self._cursor_signing_key, clock=self._clock),
                clock=self._clock,
            )
            artifact = artifacts.get(artifact_id)
            if artifact is None:
                raise IntegrityViolation(
                    "run input Artifact is unavailable", artifact_id=artifact_id
                )
            object_ref: ObjectRef | None = getattr(artifact, "object_ref", None)
            if object_ref is None:
                raise IntegrityViolation(
                    "run input Artifact has no object payload", artifact_id=artifact_id
                )
            binding = bindings.resolve(object_ref)
            with self._object_store.open(binding.location) as stream:
                return stream.read()


class WorkerPreparedArtifactStore:
    """The handlers' ``PreparedArtifactStore`` over the content-addressed ObjectStore.

    Publishes the prepared blob and returns its exact ``ObjectLocation``. The sealed
    outcome carries that location to the terminal publisher, which repeats ``stat``
    against the content-addressed ``ObjectRef`` before reading or binding it.
    """

    def __init__(self, object_store: LocalObjectStore) -> None:
        self._object_store = object_store

    def put_prepared(self, payload: bytes) -> tuple[ObjectRef, ObjectLocation]:
        stored = self._object_store.put_verified(payload)
        return stored.ref, stored.location


# ── review/repair/patch-validation shared game-port composition ────────────────
# A checker profile resolves to the production spine graph checker (the graph oracle
# needs no constraints). The bounded review-simulation population/horizon is a fixed
# composition policy (execution-profile details do not yet carry population/horizon).
_REVIEW_SIM_POPULATION = 16
_REVIEW_SIM_HORIZON = 64


def _checker_resolver(profile: ProfileRefV1, constraints: list[Constraint]) -> GraphChecker:
    del profile, constraints
    return GraphChecker()


def _sim_config_resolver(profile: ProfileRefV1) -> ReviewSimConfig:
    del profile
    return ReviewSimConfig(n_agents=_REVIEW_SIM_POPULATION, n_ticks=_REVIEW_SIM_HORIZON)


def _generation_checker_factory(snapshot: object, constraints: object) -> list[GraphChecker]:
    del snapshot, constraints
    return [GraphChecker()]


# ── deferred game ports (interface-complete, fail-closed; Task 11-13 follow-ups) ──
class _DeferredGamePort:
    """A registered-but-deferred game port: constructible now, body deferred.

    Registering the REAL platform handler keeps readiness genuinely closed; the game
    algorithm behind a not-yet-implemented port is deferred, so invoking it fails
    closed (the runner converts it to a classified attempt failure) exactly like the
    Task-14 deferred executors. Never invoked by the Task-17a ``checker.run`` path.
    """

    def __init__(self, port_name: str) -> None:
        self._port_name = port_name

    def _fail(self) -> None:
        raise IntegrityViolation(
            "game port implementation is deferred to its Task 11-13 follow-up",
            port=self._port_name,
        )

    def load_cases(self, **_: object) -> object:
        self._fail()

    def evaluate(self, *_: object, **__: object) -> object:
        self._fail()

    def compose_execute(self, **_: object) -> object:
        self._fail()

    def compose_aggregate(self, **_: object) -> object:
        self._fail()

    def verify(self, *_: object, **__: object) -> object:
        self._fail()

    def analyze(self, *_: object, **__: object) -> object:
        self._fail()


class _WorkerReadinessBlockedExecutor:
    """Callable executor whose mandatory nested production port is not yet closed."""

    def __init__(self, executor: object, *, blocker: str) -> None:
        if not callable(executor) or not blocker:
            raise ValueError("readiness-blocked executor requires a callable and reason")
        self._executor = executor
        self.worker_readiness_blocker = blocker

    def __call__(self, context: object) -> object:
        return self._executor(context)  # type: ignore[operator]


# ── real deterministic rollback ports (platform-generic ref/artifact reads) ──────
class _SqlRollbackHistoryVerifier:
    """Deterministic ``RollbackHistoryVerifier`` over the authoritative ref store.

    A rollback's history dimension is a platform-generic, deterministic ref lookup — no
    game algorithm: the current ref must equal the exact expected head, and the target
    Artifact must be the exact member at the claimed history revision. Implemented as a
    real port (not a deferred stub) so a rollback to a valid prior revision genuinely
    passes; a mismatch fails closed (never a spurious pass).
    """

    def __init__(self, *, engine: Engine, cursor_signing_key: bytes, clock: UtcClock) -> None:
        self._engine = engine
        self._cursor_signing_key = cursor_signing_key
        self._clock = clock

    def verify(self, request: RollbackHistoryRequest) -> DimensionCheckV1:
        with Session(self._engine) as session:
            refs = SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=self._cursor_signing_key, clock=self._clock),
                clock=self._clock,
            )
            current = refs.get(request.ref_name)
            if current is None:
                return DimensionCheckV1(status="failed", reason_code="rollback_ref_absent")
            if (
                current.artifact_id != request.expected_current_ref_artifact_id
                or current.revision != request.expected_current_ref_revision
            ):
                return DimensionCheckV1(
                    status="failed", reason_code="rollback_current_ref_mismatch"
                )
            history = refs.get_history_entry(request.ref_name, request.target_history_revision)
            if history is None or history.artifact_id != request.target_artifact_id:
                return DimensionCheckV1(
                    status="failed", reason_code="rollback_target_not_in_history"
                )
            return DimensionCheckV1(
                status="passed",
                detail={
                    "ref_name": request.ref_name,
                    "target_history_revision": request.target_history_revision,
                    "target_artifact_id": request.target_artifact_id,
                },
            )


class _SqlRollbackSchemaAnalyzer:
    """Deterministic ``RollbackSchemaAnalyzer``: target inspection + same-schema compat.

    Target inspection (kind / snapshot id / digest) is a plain committed-Artifact read.
    The schema-compatibility verdict is deliberately conservative: restoring a
    previously-published ``ir_snapshot`` whose declared payload schema is the CURRENT IR
    schema is a compatible rollback (``passed``); anything whose forward-compatibility
    cannot be proven here is ``unproven`` (never a fabricated pass). Cross-schema /
    migration-shaped compatibility remains a deferred game-shaped follow-up.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        object_store: LocalObjectStore,
        object_store_id: str,
        cursor_signing_key: bytes,
        clock: UtcClock,
    ) -> None:
        self._engine = engine
        self._object_store = object_store
        self._object_store_id = object_store_id
        self._cursor_signing_key = cursor_signing_key
        self._clock = clock

    def analyze(self, request: RollbackSchemaRequest) -> RollbackTargetInspectionV1:
        with Session(self._engine) as session:
            bindings = SqlObjectBindingRepository(
                session, self._object_store, self._object_store_id
            )
            artifacts = SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=CursorSigner(signing_key=self._cursor_signing_key, clock=self._clock),
                clock=self._clock,
            )
            target = artifacts.get(request.target_artifact_id)
            if target is None:
                return RollbackTargetInspectionV1(
                    status="failed",
                    target_artifact_kind="ir_snapshot",
                    target_digest="0" * 64,
                    reason_code="rollback_target_unreadable",
                )
            kind = target.kind
            declared_schema = (getattr(target, "meta", {}) or {}).get("payload_schema_id")
            snapshot_id = None
            if kind == "ir_snapshot":
                snapshot_id = target.version_tuple.ir_snapshot_id
            elif kind == "constraint_snapshot":
                snapshot_id = target.version_tuple.constraint_snapshot_id
            if kind == "ir_snapshot" and declared_schema == IR_SCHEMA_VERSION:
                return RollbackTargetInspectionV1(
                    status="passed",
                    target_artifact_kind=kind,
                    target_digest=target.payload_hash,
                    target_snapshot_id=snapshot_id,
                    target_version_tuple=target.version_tuple,
                )
            return RollbackTargetInspectionV1(
                status="unproven",
                target_artifact_kind=kind,
                target_digest=target.payload_hash,
                target_snapshot_id=snapshot_id,
                target_version_tuple=target.version_tuple,
                reason_code="rollback_schema_compat_unproven",
            )


def build_rollback_ports(
    *,
    engine: Engine,
    object_store: LocalObjectStore,
    object_store_id: str,
    cursor_signing_key: bytes,
    clock: UtcClock,
) -> tuple[_SqlRollbackHistoryVerifier, _SqlRollbackSchemaAnalyzer]:
    """The real deterministic rollback history + schema ports for the worker."""

    return (
        _SqlRollbackHistoryVerifier(
            engine=engine, cursor_signing_key=cursor_signing_key, clock=clock
        ),
        _SqlRollbackSchemaAnalyzer(
            engine=engine,
            object_store=object_store,
            object_store_id=object_store_id,
            cursor_signing_key=cursor_signing_key,
            clock=clock,
        ),
    )


def _build_executor_handlers(
    *,
    registry: ImmutablePlatformRegistry,
    blobs: ArtifactBlobReader,
    store: PreparedArtifactStore,
    rollback_history_verifier: object | None = None,
    rollback_schema_analyzer: object | None = None,
) -> dict[str, object]:
    """Instantiate the 12 real handlers + the 2 Task-14 deferred executors."""

    config_exporter = build_aureus_config_exporter(registry)
    oracle_registries = registry.completion_oracle_registries
    if not oracle_registries:
        raise IntegrityViolation("builtin registry retains no completion-oracle registry")
    oracle_registry = oracle_registries[0]
    supported_profiles = _playtest_supported_profiles(registry)
    bench_port = _DeferredGamePort("bench")
    rollback_port = _DeferredGamePort("rollback")
    # The rollback history + schema ports are real deterministic platform reads when the
    # composition supplies them; the impact analyzer stays deferred (the happy path never
    # invokes it — it passes no impact profiles).
    history_verifier = rollback_history_verifier or rollback_port
    schema_analyzer = rollback_schema_analyzer or rollback_port

    handlers: dict[str, object] = {
        "checker_runner@1": CheckerRunHandler(
            blobs=blobs, store=store, checker_factory=DefaultCheckerFactory()
        ),
        "simulation_runner@1": SimulationRunHandler(blobs=blobs, store=store),
        "review_runner@1": ReviewRunHandler(
            blobs=blobs,
            store=store,
            checker_resolver=_checker_resolver,
            sim_config_resolver=_sim_config_resolver,
        ),
        "bench_runner@1": _WorkerReadinessBlockedExecutor(
            BenchRunHandler(
                blobs=blobs,
                store=store,
                case_loader=bench_port,
                evaluator=bench_port,
                composer=bench_port,
            ),
            blocker="bench production case/evaluator/composer ports are unavailable",
        ),
        "generation_proposer@1": GenerationProposalHandler(
            blobs=blobs,
            store=store,
            agent_runner=M2GenerationAgentRunner(checker_factory=_generation_checker_factory),
            config_exporter=config_exporter,
        ),
        "repair_search@1": RepairSearchHandler(
            blobs=blobs,
            store=store,
            agent_runner=M2RepairAgentRunner(),
            config_exporter=config_exporter,
            checker_resolver=_checker_resolver,
            sim_config_resolver=_sim_config_resolver,
        ),
        "constraint_proposer@1": ConstraintProposalHandler(
            blobs=blobs,
            store=store,
            agent_runner=M2ConstraintProposalAgentRunner(),
        ),
        "task_suite_deriver@1": build_task_suite_handler(
            registry=registry, blobs=blobs, store=store
        ),
        "playtest_runner@1": build_playtest_handler(
            registry=registry,
            blobs=blobs,
            store=store,
            oracle_registry=oracle_registry,
            supported_profiles=supported_profiles,
        ),
        "patch_validator@1": PatchValidationHandler(
            blobs=blobs,
            store=store,
            checker_resolver=_checker_resolver,
            sim_config_resolver=_sim_config_resolver,
        ),
        "constraint_validator@1": ConstraintValidationHandler(
            blobs=blobs,
            store=store,
            differential_engines=build_differential_engines(),
        ),
        "rollback_validator@1": _WorkerReadinessBlockedExecutor(
            RollbackValidationHandler(
                blobs=blobs,
                store=store,
                history_verifier=history_verifier,
                schema_analyzer=schema_analyzer,
                impact_analyzer=rollback_port,
            ),
            blocker="rollback impact-analysis production port is unavailable",
        ),
    }
    handlers.update(DEFERRED_EXECUTORS)
    return handlers


def _playtest_supported_profiles(
    registry: ImmutablePlatformRegistry,
) -> frozenset[ProfileRefV1]:
    profiles: set[ProfileRefV1] = set()
    for catalog in registry.list_execution_profile_catalogs():
        for definition in catalog.definitions:
            if definition.profile_kind in {"environment", "playtest_planner"}:
                profiles.add(definition.profile)
    return frozenset(profiles)


def build_trusted_components(
    *,
    registry: ImmutablePlatformRegistry,
    blobs: ArtifactBlobReader,
    store: PreparedArtifactStore,
    rollback_history_verifier: object | None = None,
    rollback_schema_analyzer: object | None = None,
) -> TrustedComponentMaps:
    """Build the single canonical ``TrustedComponentMaps`` closing all 6 maps exactly.

    The exact 6-map KEY-SET is derived once by
    :func:`gameforge.platform.registry.build_readiness_component_maps` (the single source
    of truth shared with the API's key-only readiness composition). The worker EXECUTES
    Runs, so it replaces the key-only sentinels with the REAL executor + completion-oracle
    instances and binds every ``workflow_effect_key`` to its callable ``effects.py``
    handler. String/deferred sentinels are forbidden in an executing process. When the composition supplies
    the deterministic rollback history/schema ports they back ``rollback_validator@1``.
    """

    base = build_readiness_component_maps(registry)
    executors = _build_executor_handlers(
        registry=registry,
        blobs=blobs,
        store=store,
        rollback_history_verifier=rollback_history_verifier,
        rollback_schema_analyzer=rollback_schema_analyzer,
    )
    expected_workflow_effects = set(base.workflow_effects)
    registered_workflow_effects = set(WORKFLOW_EFFECTS)
    if registered_workflow_effects != expected_workflow_effects:
        raise IntegrityViolation(
            "worker workflow effects do not close the active registry exactly",
            missing=tuple(sorted(expected_workflow_effects - registered_workflow_effects)),
            extra=tuple(sorted(registered_workflow_effects - expected_workflow_effects)),
        )
    workflow_effects = {key: WORKFLOW_EFFECTS[key] for key in sorted(expected_workflow_effects)}
    if any(not callable(effect) for effect in workflow_effects.values()):
        raise IntegrityViolation("worker workflow effect registry contains a non-callable")
    return TrustedComponentMaps(
        executors=executors,
        terminal_hooks=dict(base.terminal_hooks),
        workflow_effects=workflow_effects,
        completion_oracles=build_completion_oracle_executors(),
        profile_handlers=dict(base.profile_handlers),
        permission_domain_resolvers=dict(base.permission_domain_resolvers),
    )


__all__ = [
    "WorkerArtifactBlobReader",
    "WorkerPreparedArtifactStore",
    "build_rollback_ports",
    "build_trusted_components",
]
