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
Task-12a oracle executors, and ``workflow_effects`` closure is deliberately SEPARATE
from ``publication/effects.py``'s resolver (which still fail-closes the mutating keys
until Task 17b).
"""

from __future__ import annotations


from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.lineage import ObjectLocation, ObjectRef
from gameforge.platform.registry import TrustedComponentMaps
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
from gameforge.platform.run_handlers.rollback_validation import RollbackValidationHandler
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
from gameforge.apps.worker.publication import BlobLocationRegistry
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

    Publishes the prepared blob and records its exact ``ObjectLocation`` into the shared
    :class:`BlobLocationRegistry` so the later terminal publish can re-read + bind it by
    ``ObjectRef`` alone.
    """

    def __init__(self, object_store: LocalObjectStore, registry: BlobLocationRegistry) -> None:
        self._object_store = object_store
        self._registry = registry

    def put_prepared(self, payload: bytes) -> tuple[ObjectRef, ObjectLocation]:
        stored = self._object_store.put_verified(payload)
        self._registry.record(stored.ref, stored.location)
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


def _build_executor_handlers(
    *,
    registry: ImmutablePlatformRegistry,
    blobs: ArtifactBlobReader,
    store: PreparedArtifactStore,
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
        "bench_runner@1": BenchRunHandler(
            blobs=blobs,
            store=store,
            case_loader=bench_port,
            evaluator=bench_port,
            composer=bench_port,
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
        "rollback_validator@1": RollbackValidationHandler(
            blobs=blobs,
            store=store,
            history_verifier=rollback_port,
            schema_analyzer=rollback_port,
            impact_analyzer=rollback_port,
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


def _derive_readiness_maps(
    registry: ImmutablePlatformRegistry,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    """Derive the four key-set-only readiness maps from the exact registry."""

    active = tuple(item for item in registry.list_run_kinds() if item.status == "active")
    terminal_hooks: dict[str, object] = {}
    workflow_effects: dict[str, object] = {}
    permission_resolvers: dict[str, object] = {}
    for definition in active:
        hooks = definition.terminal_hooks
        for hook in (hooks.on_success, hooks.on_failure, hooks.on_cancel, hooks.on_timeout):
            terminal_hooks[hook] = hook
        for policy in definition.outcome_policies:
            key = policy.workflow_effect_key
            # Real handler for the no-mutation keys; a documented deferred marker for
            # the mutating keys (effects.py's resolver stays the fail-closed authority).
            workflow_effects[key] = WORKFLOW_EFFECTS.get(key, key)
        if definition.required_permission.domain_scope == "all":
            resolver_key = registry.get_permission_resolver_key(
                RunKindRef(kind=definition.kind, version=definition.version)
            )
            if resolver_key is not None:
                permission_resolvers[resolver_key] = resolver_key
    profile_handlers: dict[str, object] = {}
    for catalog in registry.list_execution_profile_catalogs():
        states = {
            (item.profile.profile_id, item.profile.version): item.state
            for item in catalog.lifecycle
        }
        for definition in catalog.definitions:
            ref = (definition.profile.profile_id, definition.profile.version)
            if states[ref] in {"active", "replay_only"}:
                profile_handlers[definition.handler_key] = definition.handler_key
    return terminal_hooks, workflow_effects, profile_handlers, permission_resolvers


def build_trusted_components(
    *,
    registry: ImmutablePlatformRegistry,
    blobs: ArtifactBlobReader,
    store: PreparedArtifactStore,
) -> TrustedComponentMaps:
    """Build the single canonical ``TrustedComponentMaps`` closing all 6 maps exactly."""

    executors = _build_executor_handlers(registry=registry, blobs=blobs, store=store)
    terminal_hooks, workflow_effects, profile_handlers, permission_resolvers = (
        _derive_readiness_maps(registry)
    )
    return TrustedComponentMaps(
        executors=executors,
        terminal_hooks=terminal_hooks,
        workflow_effects=workflow_effects,
        completion_oracles=build_completion_oracle_executors(),
        profile_handlers=profile_handlers,
        permission_domain_resolvers=permission_resolvers,
    )


__all__ = [
    "WorkerArtifactBlobReader",
    "WorkerPreparedArtifactStore",
    "build_trusted_components",
]
