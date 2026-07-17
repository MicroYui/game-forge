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
production implementation yet (the remaining rollback analyzer) are bound
to interface-complete, fail-closed *deferred* ports — the executor is still the real
handler, its game body is deferred (project rule: define the contract now, defer the
implementation). Bench case loading, real oracle evaluation, exact aggregate reads,
and strict BenchReport composition are production-bound here.

The other five maps (``terminal_hooks`` / ``workflow_effects`` / ``profile_handlers`` /
``permission_domain_resolvers`` / ``completion_oracles``) are readiness-closure
allowlists derived from the exact registry; ``completion_oracles`` binds the real
Task-12a oracle executors, and ``workflow_effects`` binds every active key to the
exact callable in ``publication/effects.py``. Mutating handlers still require their
transaction-bound authority ports at execution time and fail closed when absent.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping, Sequence

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import sha256_lowerhex
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    CheckerProfileConfigV1,
    ConstraintExtractionProfileConfigV1,
    EnvironmentProfileDetailsV1,
    ExecutionProfileDefinitionV1,
    GenerationProfileConfigV1,
    PatchRepairProfileConfigV1,
    ProfileRefV1,
    ReviewProfileConfigV1,
    SimulationProfileConfigV1,
    WorkloadProfileConfigV1,
)
from gameforge.contracts.findings import Finding, FindingRevisionV1, finding_revision_digest
from gameforge.contracts.jobs import (
    MAX_PREPARED_ARTIFACT_BYTES,
    GenerationProposePayloadV1,
    PatchRepairPayloadV1,
)
from gameforge.contracts.lineage import ArtifactV2, ObjectLocation, ObjectRef
from gameforge.contracts.versions import IR_SCHEMA_VERSION
from gameforge.platform.registry import (
    TrustedComponentMaps,
    build_readiness_component_maps,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.playtest_payload_schemas import (
    ExactModelPayloadValidator,
    build_builtin_playtest_payload_validators,
)
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
from gameforge.platform.run_handlers.checker import (
    CheckerExecutionPolicy,
    validate_checker_execution_policy,
)
from gameforge.platform.run_handlers.constraint_validation import ConstraintValidationHandler
from gameforge.platform.run_handlers.constraint_proposal import (
    ConstraintExtractionExecutionConfig,
)
from gameforge.platform.run_handlers.generation import ConfigExporter, GenerationExecutionConfig
from gameforge.platform.run_handlers.patch_validation import PatchValidationHandler
from gameforge.platform.run_handlers.repair import RepairExecutionConfig
from gameforge.platform.run_handlers.review import ReviewExecutionConfig, ReviewSimConfig
from gameforge.platform.run_handlers.simulation import SimulationExecutionBudget
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
from gameforge.runtime.persistence.findings import SqlFindingRepository
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.apps.worker.agent_runners import (
    M2ConstraintProposalAgentRunner,
    M2GenerationAgentRunner,
    M2RepairAgentRunner,
)
from gameforge.apps.worker.bench import build_bench_ports
from gameforge.apps.worker.completion_oracles import build_completion_oracle_executors
from gameforge.apps.worker.config_export import build_aureus_config_exporter
from gameforge.apps.worker.playtest import build_playtest_handler
from gameforge.apps.worker.regression import (
    ProfileBoundEnvironment,
    RegressionEnvironmentPlanV1,
    build_worker_regression_runner,
)
from gameforge.apps.worker.task_suite import (
    build_scenario_shaper_resolver,
    build_task_suite_handler,
)
from gameforge.platform.run_handlers.task_suite import ScenarioShaperResolver
from gameforge.apps.worker.validation import build_differential_engines
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.checkers.base import Checker
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider


class _WorkerProfileHandlerImplementation:
    """Opaque capability proving a handler key is implemented by this worker build."""

    __slots__ = ("handler_key",)

    def __init__(self, handler_key: str) -> None:
        self.handler_key = handler_key


# This allowlist is intentionally independent from persisted catalog rows. A catalog
# may select one of these process-shipped implementations, but cannot register a new
# trusted implementation by merely naming its own handler key.
_WORKER_PROFILE_HANDLER_KEYS = (
    "builtin_artifact_migrator_profile@1",
    "builtin_bench_evaluator_profile@1",
    "builtin_checker_profile@1",
    "builtin_config_export_profile@1",
    "builtin_constraint_compiler_profile@1",
    "builtin_constraint_extraction_profile@1",
    "builtin_dr_plan_profile@1",
    "builtin_dr_verifier_profile@1",
    "builtin_environment_profile@1",
    "builtin_generation_profile@1",
    "builtin_impact_analysis_profile@1",
    "builtin_llm_triage_profile@1",
    "builtin_patch_repair_profile@1",
    "builtin_playtest_planner_profile@1",
    "builtin_playtest_planner_profile@2",
    "builtin_restore_target_profile@1",
    "builtin_review_profile@1",
    "builtin_rollback_profile@1",
    "builtin_schema_compatibility_profile@1",
    "builtin_simulation_profile@1",
    "builtin_task_suite_derivation_profile@1",
    "builtin_task_suite_derivation_profile@2",
    "builtin_validation_profile@1",
    "builtin_workload_profile@1",
)
_WORKER_PROFILE_HANDLER_IMPLEMENTATIONS = {
    key: _WorkerProfileHandlerImplementation(key) for key in _WORKER_PROFILE_HANDLER_KEYS
}


# ── executor Finding-authority ports ─────────────────────────────────────
class WorkerExactFindingRevisionLoader:
    """Materialise Run finding bindings from the immutable SQL authority.

    Admission verifies these bindings transactionally when the Run is created. The
    executor deliberately verifies them again against the retained immutable revision:
    execution must never substitute an evidence-artifact projection, a current head, or
    an unverified legacy ``Finding`` for the exact admitted ``(id, revision, digest)``.
    """

    def __init__(self, *, engine: Engine, cursor_signing_key: bytes, clock: UtcClock) -> None:
        self._engine = engine
        self._cursor_signing_key = cursor_signing_key
        self._clock = clock

    def load_exact(
        self, *, finding_id: str, finding_revision: int, finding_digest: str
    ) -> FindingRevisionV1:
        with Session(self._engine) as session:
            repository = self._repository(session)
            retained = repository.get(finding_id, finding_revision)
        if retained is None:
            raise IntegrityViolation(
                "bound Finding revision is unavailable at execution",
                finding_id=finding_id,
                finding_revision=finding_revision,
            )
        retained_digest = finding_revision_digest(retained)
        if not hmac.compare_digest(retained_digest, finding_digest):
            raise IntegrityViolation(
                "bound Finding digest differs from the retained revision",
                finding_id=finding_id,
                finding_revision=finding_revision,
            )
        return retained

    def __call__(
        self,
        blobs: ArtifactBlobReader,
        payload: GenerationProposePayloadV1 | PatchRepairPayloadV1,
    ) -> tuple[Finding, ...]:
        del blobs
        revisions = tuple(
            self.load_exact(
                finding_id=binding.finding_id,
                finding_revision=binding.finding_revision,
                finding_digest=binding.finding_digest,
            )
            for binding in payload.findings
        )
        return tuple(_materialise_finding(revision) for revision in revisions)

    def _repository(self, session: Session) -> SqlFindingRepository:
        return SqlFindingRepository(
            session,
            cursor_signer=CursorSigner(signing_key=self._cursor_signing_key, clock=self._clock),
            clock=self._clock,
        )


class WorkerFindingHeadRevisionResolver:
    """Resolve the exact current Finding-series head used by terminal CAS."""

    def __init__(self, *, engine: Engine, cursor_signing_key: bytes, clock: UtcClock) -> None:
        self._engine = engine
        self._cursor_signing_key = cursor_signing_key
        self._clock = clock

    def __call__(self, finding_id: str) -> int | None:
        with Session(self._engine) as session:
            repository = SqlFindingRepository(
                session,
                cursor_signer=CursorSigner(signing_key=self._cursor_signing_key, clock=self._clock),
                clock=self._clock,
            )
            current = repository.current(finding_id)
        return None if current is None else current.revision


def _materialise_finding(revision: FindingRevisionV1) -> Finding:
    """Project only digest-bound semantic fields into the legacy spine shape."""

    payload = revision.payload.model_dump(mode="python", exclude={"payload_schema_version"})
    return Finding.model_validate({"id": revision.finding_id, **payload})


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
        self.finding_revision_loader = WorkerExactFindingRevisionLoader(
            engine=engine,
            cursor_signing_key=cursor_signing_key,
            clock=clock,
        )
        self.finding_head_revision = WorkerFindingHeadRevisionResolver(
            engine=engine,
            cursor_signing_key=cursor_signing_key,
            clock=clock,
        )

    def read_bytes(self, artifact_id: str) -> bytes:
        """Read through the platform-wide hard cap; never issue an unbounded read."""

        return self.read_bytes_bounded(
            artifact_id,
            max_bytes=MAX_PREPARED_ARTIFACT_BYTES,
        )

    def load_artifact(self, artifact_id: str) -> ArtifactV2:
        """Return the exact retained Artifact envelope for identity-aware ports."""

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
            raise IntegrityViolation("run input Artifact is unavailable", artifact_id=artifact_id)
        return artifact

    def read_bytes_bounded(self, artifact_id: str, *, max_bytes: int) -> bytes:
        """Read and re-hash the exact retained object within the caller's cap."""

        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("Artifact read byte bound must be a positive integer")
        artifact = self.load_artifact(artifact_id)
        if artifact.object_ref.size_bytes > max_bytes:
            raise IntegrityViolation(
                "run input Artifact exceeds the consumer byte bound",
                artifact_id=artifact_id,
                max_bytes=max_bytes,
            )
        with Session(self._engine) as session:
            bindings = SqlObjectBindingRepository(
                session, self._object_store, self._object_store_id
            )
            binding = bindings.resolve(artifact.object_ref)
            with self._object_store.open(binding.location) as stream:
                # The retained size is already within ``max_bytes``.  Read at most
                # one byte beyond that exact size so a post-verification object
                # replacement cannot turn a small Artifact into a large allocation.
                remaining = artifact.object_ref.size_bytes + 1
                chunks: list[bytes] = []
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if chunk == b"":
                        break
                    if not isinstance(chunk, bytes):
                        raise IntegrityViolation(
                            "run input Artifact stream returned non-bytes",
                            artifact_id=artifact_id,
                        )
                    if len(chunk) > remaining:
                        raise IntegrityViolation(
                            "run input Artifact stream exceeded the bounded read",
                            artifact_id=artifact_id,
                        )
                    chunks.append(chunk)
                    remaining -= len(chunk)
                blob = b"".join(chunks)
        if len(blob) != artifact.object_ref.size_bytes or len(blob) > max_bytes:
            raise IntegrityViolation(
                "run input Artifact bytes differ from the retained ObjectRef",
                artifact_id=artifact_id,
            )
        digest = sha256_lowerhex(blob)
        if not hmac.compare_digest(digest, artifact.object_ref.sha256):
            raise IntegrityViolation(
                "run input Artifact hash differs from the retained ObjectRef",
                artifact_id=artifact_id,
            )
        return blob


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


class _CheckerGroup:
    def __init__(
        self,
        *,
        profile: ProfileRefV1,
        checkers: tuple[Checker, ...],
        policy: CheckerExecutionPolicy,
        constraint_count: int,
    ) -> None:
        self.id = f"profile:{profile.profile_id}@{profile.version}"
        self._checkers = checkers
        self._policy = policy
        self._constraint_count = constraint_count

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]:
        validate_checker_execution_policy(
            checker_ids=("graph",),
            defect_classes=(),
            constraint_count=self._constraint_count,
            snapshot=snapshot,
            policy=self._policy,
        )
        return [
            finding for checker in self._checkers for finding in checker.check(snapshot, nav=nav)
        ]


def _profile_definition(
    registry: ImmutablePlatformRegistry,
    profile: ProfileRefV1,
    *,
    expected_kind: str,
) -> ExecutionProfileDefinitionV1:
    candidates = [
        definition
        for catalog in registry.list_execution_profile_catalogs()
        for definition in catalog.definitions
        if definition.profile == profile and definition.profile_kind == expected_kind
    ]
    definitions: list[ExecutionProfileDefinitionV1] = []
    for definition in candidates:
        if definition not in definitions:
            definitions.append(definition)
    if len(definitions) != 1:
        raise IntegrityViolation(
            "execution profile does not resolve one immutable definition",
            profile=profile.model_dump(mode="json"),
            expected_kind=expected_kind,
        )
    return definitions[0]


def _build_checker_resolver(registry: ImmutablePlatformRegistry):
    policy_resolver = _build_checker_execution_policy_resolver(registry)

    def resolve(profile: ProfileRefV1, constraints: list[Constraint]) -> Checker:
        definition = _profile_definition(registry, profile, expected_kind="checker")
        if getattr(definition, "handler_key", None) != "builtin_checker_profile@1":
            raise IntegrityViolation("checker profile handler is unavailable")
        policy = policy_resolver(profile)
        # Compilation constructs solver-backed checker objects. Reject the exact
        # retained profile's constraint cap before ``compile_all`` so a repair
        # request cannot multiply an oversized set across many profiles before
        # the first candidate verification call.
        if len(constraints) > policy.max_constraint_count:
            raise IntegrityViolation(
                "checker constraint set exceeds the exact profile count budget"
            )
        return _CheckerGroup(
            profile=profile,
            checkers=(GraphChecker(), *compile_all(constraints)),
            policy=policy,
            constraint_count=len(constraints),
        )

    return resolve


def _build_checker_execution_policy_resolver(registry: ImmutablePlatformRegistry):
    def resolve(profile: ProfileRefV1) -> CheckerExecutionPolicy:
        definition = _profile_definition(registry, profile, expected_kind="checker")
        if definition.handler_key != "builtin_checker_profile@1":
            raise IntegrityViolation("checker profile handler is unavailable")
        config = CheckerProfileConfigV1.model_validate(definition.config)
        return CheckerExecutionPolicy(
            allowed_checker_ids=config.allowed_checker_ids,
            allowed_defect_classes=config.allowed_defect_classes,
            max_direct_checker_count=config.max_direct_checker_count,
            max_constraint_count=config.max_constraint_count,
            max_work_units=config.max_work_units,
        )

    return resolve


def _build_simulation_execution_budget_resolver(registry: ImmutablePlatformRegistry):
    def resolve(
        simulation_profile: ProfileRefV1,
        workload_profile: ProfileRefV1,
    ) -> SimulationExecutionBudget:
        simulation_definition = _profile_definition(
            registry, simulation_profile, expected_kind="simulation"
        )
        workload_definition = _profile_definition(
            registry, workload_profile, expected_kind="workload"
        )
        if (
            simulation_definition.handler_key != "builtin_simulation_profile@1"
            or workload_definition.handler_key != "builtin_workload_profile@1"
        ):
            raise IntegrityViolation("simulation profile handler is unavailable")
        simulation = SimulationProfileConfigV1.model_validate(simulation_definition.config)
        workload = WorkloadProfileConfigV1.model_validate(workload_definition.config)
        return SimulationExecutionBudget(
            max_replication_count=workload.max_replication_count,
            max_horizon_steps=simulation.max_horizon_steps,
            max_output_ticks=simulation.max_output_ticks,
            max_total_replication_ticks=workload.max_total_replication_ticks,
            max_total_work_units=min(simulation.max_work_units, workload.max_total_work_units),
        )

    return resolve


def _build_generation_execution_config_resolver(registry: ImmutablePlatformRegistry):
    def resolve(profile: ProfileRefV1) -> GenerationExecutionConfig:
        definition = _profile_definition(registry, profile, expected_kind="generation")
        if definition.handler_key != "builtin_generation_profile@1":
            raise IntegrityViolation("generation profile handler is unavailable")
        config = GenerationProfileConfigV1.model_validate(definition.config)
        return GenerationExecutionConfig(
            max_prompt_message_bytes=config.max_prompt_message_bytes,
            max_constraint_count=config.max_checker_constraint_count,
            max_work_units=config.max_checker_work_units,
            gate_simulation_seed=config.gate_simulation_seed,
            gate_simulation_population=config.gate_simulation_population,
            gate_simulation_horizon_steps=config.gate_simulation_horizon_steps,
            max_simulation_work_units=config.max_simulation_work_units,
            max_candidate_export_profiles=config.max_candidate_export_profiles,
            max_total_prepared_artifact_bytes=config.max_total_prepared_artifact_bytes,
        )

    return resolve


def _build_repair_execution_config_resolver(registry: ImmutablePlatformRegistry):
    def resolve(profile: ProfileRefV1) -> RepairExecutionConfig:
        definition = _profile_definition(registry, profile, expected_kind="patch_repair")
        if definition.handler_key != "builtin_patch_repair_profile@1":
            raise IntegrityViolation("repair profile handler is unavailable")
        config = PatchRepairProfileConfigV1.model_validate(definition.config)
        return RepairExecutionConfig(
            max_search_steps=config.max_search_steps,
            max_prompt_message_bytes=config.max_prompt_message_bytes,
            max_total_checker_work_units=config.max_total_checker_work_units,
            max_total_simulation_work_units=config.max_total_simulation_work_units,
            max_checker_profile_count=config.max_checker_profile_count,
            max_simulation_profile_count=config.max_simulation_profile_count,
            max_regression_suite_count=config.max_regression_suite_count,
            max_total_regression_work_units=config.max_total_regression_work_units,
            max_regression_suite_bytes=config.max_regression_suite_bytes,
            max_total_regression_suite_bytes=config.max_total_regression_suite_bytes,
            max_candidate_export_profiles=config.max_candidate_export_profiles,
            max_total_prepared_artifact_bytes=config.max_total_prepared_artifact_bytes,
        )

    return resolve


def _build_review_execution_config_resolver(registry: ImmutablePlatformRegistry):
    def resolve(profile: ProfileRefV1) -> ReviewExecutionConfig:
        definition = _profile_definition(registry, profile, expected_kind="review")
        if definition.handler_key != "builtin_review_profile@1":
            raise IntegrityViolation("review profile handler is unavailable")
        config = ReviewProfileConfigV1.model_validate(definition.config)
        return ReviewExecutionConfig(
            max_prompt_message_bytes=config.max_prompt_message_bytes,
            max_checker_profile_count=config.max_checker_profile_count,
            max_simulation_profile_count=config.max_simulation_profile_count,
            max_total_checker_work_units=config.max_total_checker_work_units,
            max_total_simulation_work_units=config.max_total_simulation_work_units,
            max_total_prepared_artifact_bytes=config.max_total_prepared_artifact_bytes,
        )

    return resolve


def _build_constraint_extraction_execution_config_resolver(
    registry: ImmutablePlatformRegistry,
):
    def resolve(profile: ProfileRefV1) -> ConstraintExtractionExecutionConfig:
        definition = _profile_definition(registry, profile, expected_kind="constraint_extraction")
        if definition.handler_key != "builtin_constraint_extraction_profile@1":
            raise IntegrityViolation("constraint extraction profile handler is unavailable")
        config = ConstraintExtractionProfileConfigV1.model_validate(definition.config)
        return ConstraintExtractionExecutionConfig(
            max_prompt_message_bytes=config.max_prompt_message_bytes,
            max_source_artifact_count=config.max_source_artifact_count,
            max_source_artifact_bytes=config.max_source_artifact_bytes,
            max_total_input_bytes=config.max_total_input_bytes,
            max_proposal_count=config.max_proposal_count,
            max_output_bytes=config.max_output_bytes,
        )

    return resolve


def _build_sim_config_resolver(registry: ImmutablePlatformRegistry):
    def resolve(profile: ProfileRefV1) -> ReviewSimConfig:
        definition = _profile_definition(registry, profile, expected_kind="simulation")
        if getattr(definition, "handler_key", None) != "builtin_simulation_profile@1":
            raise IntegrityViolation("simulation profile handler is unavailable")
        config = SimulationProfileConfigV1.model_validate(definition.config)
        return ReviewSimConfig(
            n_agents=config.default_population,
            n_ticks=config.default_horizon_steps,
            max_work_units=config.max_work_units,
        )

    return resolve


def _generation_checker_factory(
    snapshot: Snapshot, constraints: Sequence[Constraint]
) -> list[Checker]:
    del snapshot
    return [GraphChecker(), *compile_all(list(constraints))]


def _build_builtin_environment(
    snapshot: Snapshot, _definition: ExecutionProfileDefinitionV1
) -> RegressionEnvironmentPlanV1:
    """Profile-handler factory registered for the built-in environment adapter."""

    if not isinstance(_definition.details, EnvironmentProfileDetailsV1):
        raise IntegrityViolation("built-in environment profile has no contract")
    world = snapshot_to_world(snapshot)
    contract = _definition.details.contract
    grid_cells = world.grid.width * world.grid.height
    if (
        world.grid.width < 1
        or world.grid.height < 1
        or grid_cells > contract.max_navigation_grid_cells
    ):
        raise ValueError("candidate grid exceeds the frozen environment profile bound")
    # ``AureusEnv.observe`` runs one BFS per placed target plus at most one per
    # encounter that can become a pending fight.  Charge that conservative exact
    # upper bound for reset and for every step observation; navigation itself adds
    # one further grid traversal.  All arithmetic happens before an Env/BFS exists.
    observation_work_units = grid_cells * (len(world.placements) + len(world.encounters))
    contract_version = contract.env_contract_version

    def create() -> ProfileBoundEnvironment:
        # AureusEnv's historical class attribute names its older kernel interface.
        # The explicit wrapper is the profile-selected projection that implements
        # generic-agent-env@1; the runner validates its reset/action/observation wire.
        return ProfileBoundEnvironment(
            delegate=AureusEnv(world),
            env_contract_version=contract_version,
        )

    return RegressionEnvironmentPlanV1(
        factory=create,
        reset_work_units=observation_work_units,
        step_observation_work_units=observation_work_units,
        navigation_work_units=grid_cells,
    )


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
    playtest_payload_validators: Mapping[str, ExactModelPayloadValidator],
    config_exporter: ConfigExporter,
    task_suite_scenario_shaper_resolver: ScenarioShaperResolver,
    finding_revision_loader: WorkerExactFindingRevisionLoader | None = None,
    finding_head_revision: WorkerFindingHeadRevisionResolver | None = None,
    rollback_history_verifier: object | None = None,
    rollback_schema_analyzer: object | None = None,
) -> dict[str, object]:
    """Instantiate the 12 real handlers + the 2 Task-14 deferred executors."""

    if isinstance(blobs, WorkerArtifactBlobReader):
        finding_revision_loader = finding_revision_loader or blobs.finding_revision_loader
        finding_head_revision = finding_head_revision or blobs.finding_head_revision
    if finding_revision_loader is None or finding_head_revision is None:
        raise IntegrityViolation(
            "worker executor composition requires exact Finding read authority"
        )

    checker_resolver = _build_checker_resolver(registry)
    checker_execution_policy_resolver = _build_checker_execution_policy_resolver(registry)
    simulation_execution_budget_resolver = _build_simulation_execution_budget_resolver(registry)
    generation_execution_config_resolver = _build_generation_execution_config_resolver(registry)
    repair_execution_config_resolver = _build_repair_execution_config_resolver(registry)
    review_execution_config_resolver = _build_review_execution_config_resolver(registry)
    constraint_extraction_execution_config_resolver = (
        _build_constraint_extraction_execution_config_resolver(registry)
    )
    sim_config_resolver = _build_sim_config_resolver(registry)
    oracle_registries = registry.completion_oracle_registries
    if not oracle_registries:
        raise IntegrityViolation("builtin registry retains no completion-oracle registry")
    oracle_registry = oracle_registries[0]
    supported_profiles = _playtest_supported_profiles(registry)
    if not isinstance(blobs, WorkerArtifactBlobReader):
        raise IntegrityViolation(
            "worker executor composition requires identity-aware Artifact reads"
        )
    bench_port = build_bench_ports(registry=registry, artifacts=blobs)
    regression_runner = build_worker_regression_runner(
        artifacts=blobs,
        registry=registry,
        environment_builders={"builtin_environment_profile@1": _build_builtin_environment},
        oracle_executors=build_completion_oracle_executors(),
    )
    rollback_port = _DeferredGamePort("rollback")
    # The rollback history + schema ports are real deterministic platform reads when the
    # composition supplies them; the impact analyzer stays deferred (the happy path never
    # invokes it — it passes no impact profiles).
    history_verifier = rollback_history_verifier or rollback_port
    schema_analyzer = rollback_schema_analyzer or rollback_port

    handlers: dict[str, object] = {
        "checker_runner@1": CheckerRunHandler(
            blobs=blobs,
            store=store,
            checker_factory=DefaultCheckerFactory(),
            execution_policy_resolver=checker_execution_policy_resolver,
            finding_head_revision=finding_head_revision,
        ),
        "simulation_runner@1": SimulationRunHandler(
            blobs=blobs,
            store=store,
            execution_budget_resolver=simulation_execution_budget_resolver,
            finding_head_revision=finding_head_revision,
        ),
        "review_runner@1": ReviewRunHandler(
            blobs=blobs,
            store=store,
            checker_resolver=checker_resolver,
            sim_config_resolver=sim_config_resolver,
            checker_execution_policy_resolver=checker_execution_policy_resolver,
            execution_config_resolver=review_execution_config_resolver,
            finding_head_revision=finding_head_revision,
        ),
        "bench_runner@1": BenchRunHandler(
            blobs=blobs,
            store=store,
            case_loader=bench_port,
            evaluator=bench_port,
            composer=bench_port,
            aggregate_input_verifier=bench_port,
        ),
        "generation_proposer@1": GenerationProposalHandler(
            blobs=blobs,
            store=store,
            agent_runner=M2GenerationAgentRunner(checker_factory=_generation_checker_factory),
            config_exporter=config_exporter,
            finding_loader=finding_revision_loader,
            execution_config_resolver=generation_execution_config_resolver,
        ),
        "repair_search@1": RepairSearchHandler(
            blobs=blobs,
            store=store,
            agent_runner=M2RepairAgentRunner(regression_runner=regression_runner),
            config_exporter=config_exporter,
            checker_resolver=checker_resolver,
            sim_config_resolver=sim_config_resolver,
            execution_config_resolver=repair_execution_config_resolver,
            finding_loader=finding_revision_loader,
        ),
        "constraint_proposer@1": ConstraintProposalHandler(
            blobs=blobs,
            store=store,
            agent_runner=M2ConstraintProposalAgentRunner(),
            execution_config_resolver=(constraint_extraction_execution_config_resolver),
        ),
        "task_suite_deriver@1": build_task_suite_handler(
            registry=registry,
            blobs=blobs,
            store=store,
            playtest_payload_validators=playtest_payload_validators,
            scenario_shaper_resolver=task_suite_scenario_shaper_resolver,
        ),
        "playtest_runner@1": build_playtest_handler(
            registry=registry,
            blobs=blobs,
            store=store,
            oracle_registry=oracle_registry,
            supported_profiles=supported_profiles,
            finding_head_revision=finding_head_revision,
            playtest_payload_validators=playtest_payload_validators,
        ),
        "patch_validator@1": PatchValidationHandler(
            blobs=blobs,
            store=store,
            checker_resolver=checker_resolver,
            sim_config_resolver=sim_config_resolver,
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
            if (
                definition.profile_kind == "environment"
                and definition.handler_key == "builtin_environment_profile@1"
                and isinstance(definition.details, EnvironmentProfileDetailsV1)
                and definition.details.contract.reset_schema_id == "generic-env-reset@1"
                and definition.details.contract.action_schema_id == "generic-env-action@1"
                and definition.details.contract.observation_schema_id == "generic-env-observation@1"
            ):
                profiles.add(definition.profile)
    return frozenset(profiles)


def build_trusted_components(
    *,
    registry: ImmutablePlatformRegistry,
    blobs: ArtifactBlobReader,
    store: PreparedArtifactStore,
    playtest_payload_validators: Mapping[str, ExactModelPayloadValidator] | None = None,
    config_exporter: ConfigExporter | None = None,
    task_suite_scenario_shaper_resolver: ScenarioShaperResolver | None = None,
    finding_revision_loader: WorkerExactFindingRevisionLoader | None = None,
    finding_head_revision: WorkerFindingHeadRevisionResolver | None = None,
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
    validators = (
        playtest_payload_validators
        if playtest_payload_validators is not None
        else build_builtin_playtest_payload_validators()
    )
    exporter = config_exporter or build_aureus_config_exporter(registry)
    scenario_shaper_resolver = (
        task_suite_scenario_shaper_resolver
        if task_suite_scenario_shaper_resolver is not None
        else build_scenario_shaper_resolver(registry)
    )
    executors = _build_executor_handlers(
        registry=registry,
        blobs=blobs,
        store=store,
        playtest_payload_validators=validators,
        config_exporter=exporter,
        task_suite_scenario_shaper_resolver=scenario_shaper_resolver,
        finding_revision_loader=finding_revision_loader,
        finding_head_revision=finding_head_revision,
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
    expected_profile_handlers = set(base.profile_handlers)
    registered_profile_handlers = set(_WORKER_PROFILE_HANDLER_IMPLEMENTATIONS)
    if registered_profile_handlers != expected_profile_handlers:
        raise IntegrityViolation(
            "worker profile handlers do not close the retained catalog exactly",
            missing=tuple(sorted(expected_profile_handlers - registered_profile_handlers)),
            extra=tuple(sorted(registered_profile_handlers - expected_profile_handlers)),
        )
    profile_handlers = {
        key: _WORKER_PROFILE_HANDLER_IMPLEMENTATIONS[key]
        for key in sorted(expected_profile_handlers)
    }
    return TrustedComponentMaps(
        executors=executors,
        terminal_hooks=dict(base.terminal_hooks),
        workflow_effects=workflow_effects,
        completion_oracles=build_completion_oracle_executors(),
        playtest_payload_validators=validators,
        profile_handlers=profile_handlers,
        permission_domain_resolvers=dict(base.permission_domain_resolvers),
    )


__all__ = [
    "WorkerArtifactBlobReader",
    "WorkerExactFindingRevisionLoader",
    "WorkerFindingHeadRevisionResolver",
    "WorkerPreparedArtifactStore",
    "build_rollback_ports",
    "build_trusted_components",
]
