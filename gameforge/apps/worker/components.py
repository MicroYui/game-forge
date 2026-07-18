"""The canonical M4c trusted-component composition for API readiness + worker dispatch.

``PlatformReadinessValidator`` requires ``TrustedComponentMaps`` to close EXACTLY
against the 14 active RunKind definitions across all six component maps. This module
builds that single canonical map once, so both the API readiness probe
(``apps/api/local.py``) and the persistent worker (``apps/worker``) share identical,
genuinely-closed authority.

The ``executors`` values are the REAL Task-11/12/13 platform handlers (never fakes):
the deterministic ``checker``/``simulation`` handlers, the ``task_suite``/``playtest``
game-composed handlers, the agent-backed generation/repair/constraint handlers, and
the validation handlers — plus the two Task-14 ``DEFERRED_EXECUTORS`` on the same
full context signature used by implemented handlers. Rollback validation binds real SQL history, exact
catalog-backed schema, bounded current→target impact, and headless regression ports.
Bench case loading, real oracle evaluation, exact aggregate reads, and strict
BenchReport composition are production-bound here.

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

from sqlalchemy import Engine, select
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
    ResolvedExecutionProfileBindingV1,
    ReviewProfileConfigV1,
    RunKindRef,
    SimulationProfileConfigV1,
    WorkloadProfileConfigV1,
)
from gameforge.contracts.findings import Finding, FindingRevisionV1, finding_revision_digest
from gameforge.contracts.jobs import (
    MAX_COLLECTION_ITEMS,
    MAX_PREPARED_ARTIFACT_BYTES,
    GenerationProposePayloadV1,
    PatchRepairPayloadV1,
    RunRecord,
)
from gameforge.contracts.lineage import ArtifactV2, ObjectLocation, ObjectRef
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
from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    LlmExecutionMode,
    PreparedArtifactStore,
)
from gameforge.platform.run_handlers.checker import (
    CheckerExecutionPolicy,
    validate_checker_execution_policy,
    validate_checker_output_policy,
)
from gameforge.platform.run_handlers.constraint_validation import ConstraintValidationHandler
from gameforge.platform.run_handlers.constraint_proposal import (
    ConstraintExtractionExecutionConfig,
)
from gameforge.platform.run_handlers.generation import ConfigExporter, GenerationExecutionConfig
from gameforge.platform.run_handlers.patch_validation import (
    ExactLinkedFindingRevision,
    PatchValidationHandler,
)
from gameforge.platform.run_handlers.repair import RepairExecutionConfig
from gameforge.platform.run_handlers.review import ReviewExecutionConfig, ReviewSimConfig
from gameforge.platform.run_handlers.simulation import SimulationExecutionBudget
from gameforge.platform.run_handlers.rollback_validation import (
    DimensionCheckV1,
    RollbackHistoryRequest,
    RollbackValidationHandler,
)
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.contracts.storage import UtcClock
from gameforge.platform.publication.effects import WORKFLOW_EFFECTS
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.findings import SqlFindingRepository
from gameforge.runtime.persistence.models import FindingHeadRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.apps.worker.agent_runners import (
    M2ConstraintProposalAgentRunner,
    M2GenerationAgentRunner,
    M2RepairAgentRunner,
)
from gameforge.apps.worker.auto_apply import build_worker_auto_apply_evaluator
from gameforge.apps.worker.bench import build_bench_ports
from gameforge.apps.worker.completion_oracles import build_completion_oracle_executors
from gameforge.apps.worker.config_export import build_aureus_config_exporter
from gameforge.apps.worker.playtest import build_playtest_handler
from gameforge.apps.worker.regression import (
    ProfileBoundEnvironment,
    RegressionEnvironmentPlanV1,
    build_worker_regression_runner,
)
from gameforge.apps.worker.rollback_validation import (
    DeterministicRollbackImpactAnalyzer,
    ExactRollbackSchemaAnalyzer,
)
from gameforge.apps.worker.task_suite import (
    build_scenario_shaper_resolver,
    build_task_suite_handler,
)
from gameforge.platform.run_handlers.task_suite import ScenarioShaperResolver
from gameforge.apps.worker.validation import (
    RegistryConstraintValidationProfileResolver,
    build_differential_engines,
)
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.checkers.base import Checker, CheckerExecutionBinding
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

    def list_linked_exact(
        self,
        *,
        evidence_artifact_ids: tuple[str, ...],
    ) -> tuple[ExactLinkedFindingRevision, ...]:
        """Load the complete bounded RunFindingLink closure for evidence."""

        with Session(self._engine) as session:
            links = SqlRunRepository(session).list_finding_links_by_evidence_artifact_ids(
                evidence_artifact_ids,
                max_items=MAX_COLLECTION_ITEMS,
            )
            repository = self._repository(session)
            linked: list[ExactLinkedFindingRevision] = []
            for link in links:
                revision = repository.get(link.finding_id, link.finding_revision)
                if revision is None:
                    raise IntegrityViolation(
                        "evidence-linked Finding revision is unavailable at execution",
                        finding_id=link.finding_id,
                        finding_revision=link.finding_revision,
                    )
                digest = finding_revision_digest(revision)
                if not hmac.compare_digest(digest, link.finding_digest):
                    raise IntegrityViolation(
                        "evidence-linked Finding digest differs from its revision",
                        finding_id=link.finding_id,
                        finding_revision=link.finding_revision,
                    )
                linked.append(
                    ExactLinkedFindingRevision(
                        evidence_artifact_id=link.evidence_artifact_id,
                        revision=revision,
                    )
                )
        return tuple(linked)

    def _repository(self, session: Session) -> SqlFindingRepository:
        return SqlFindingRepository(
            session,
            cursor_signer=CursorSigner(signing_key=self._cursor_signing_key, clock=self._clock),
            clock=self._clock,
        )


class WorkerFindingHeadRevisionResolver:
    """Read current Finding revisions in one bounded projection for terminal CAS."""

    def __init__(self, *, engine: Engine) -> None:
        self._engine = engine

    def __call__(self, finding_ids: tuple[str, ...]) -> dict[str, int | None]:
        revisions = dict.fromkeys(finding_ids)
        with Session(self._engine) as session:
            for offset in range(0, len(finding_ids), 900):
                chunk = finding_ids[offset : offset + 900]
                rows = session.execute(
                    select(FindingHeadRow.finding_id, FindingHeadRow.current_revision).where(
                        FindingHeadRow.finding_id.in_(chunk)
                    )
                ).all()
                revisions.update(rows)
        return revisions


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

    def load_run(self, run_id: str) -> RunRecord:
        """Return the exact retained producer Run for aggregate provenance checks."""

        with Session(self._engine) as session:
            run = SqlRunRepository(session).get(run_id)
        if not isinstance(run, RunRecord):
            raise IntegrityViolation("producer Run is unavailable", run_id=run_id)
        return run

    def get_ref(self, ref_name: str):
        """Read the current exact CAS value used by auto-apply prequalification."""

        with Session(self._engine) as session:
            refs = SqlRefStore(
                session,
                cursor_signer=CursorSigner(
                    signing_key=self._cursor_signing_key,
                    clock=self._clock,
                ),
                clock=self._clock,
            )
            return refs.get(ref_name)

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
        # Patch validation must prove which native deterministic executors ran
        # even when their clean result contains no Finding carrying producer_id.
        bindings = []
        for checker in checkers:
            if not getattr(checker, "deterministic_execution", True):
                continue
            binding = getattr(checker, "execution_binding", None)
            if not isinstance(binding, CheckerExecutionBinding):
                binding = CheckerExecutionBinding(
                    wrapper_id=checker.id,
                    native_id=checker.id,
                    constraint_id=None,
                )
            bindings.append(binding)
        self.executed_checker_bindings = tuple(
            sorted(
                set(bindings),
                key=lambda item: (
                    item.native_id,
                    item.constraint_id is not None,
                    item.constraint_id or "",
                    item.wrapper_id,
                ),
            )
        )
        # Retain the legacy scalar view only for direct/global executors. A
        # compiled backend must never escape here as a naked global native id;
        # its authority exists only in the structured constraint-scoped view.
        self.executed_checker_ids = tuple(
            sorted(
                {
                    binding.native_id
                    for binding in self.executed_checker_bindings
                    if binding.constraint_id is None
                }
            )
        )
        self._checkers = checkers
        self._policy = policy
        self._constraint_count = constraint_count

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]:
        direct_checker_ids = tuple(
            sorted(
                {
                    binding.native_id
                    for binding in self.executed_checker_bindings
                    if binding.constraint_id is None
                }
            )
        )
        native_checker_ids = {binding.native_id for binding in self.executed_checker_bindings}
        disallowed_native_ids = tuple(
            sorted(native_checker_ids - set(self._policy.allowed_checker_ids))
        )
        if disallowed_native_ids:
            raise IntegrityViolation(
                "compiled checker route is outside the exact profile taxonomy",
                checker_ids=disallowed_native_ids,
            )
        validate_checker_execution_policy(
            # Constraint-scoped native routes consume work/count authority below,
            # but do not inflate the profile's direct-backend count.
            checker_ids=direct_checker_ids,
            defect_classes=(),
            constraint_count=self._constraint_count,
            snapshot=snapshot,
            policy=self._policy,
        )
        findings = [
            finding for checker in self._checkers for finding in checker.check(snapshot, nav=nav)
        ]
        validate_checker_output_policy(findings, policy=self._policy)
        return findings


_RUN_HANDLER_PROFILE_ADAPTER_CONTRACTS: dict[
    str,
    tuple[str, str, frozenset[str], frozenset[str], frozenset[bool]],
] = {
    "bench_evaluator": (
        "builtin_bench_evaluator_profile@1",
        "bench_evaluator-profile-config@1",
        frozenset(("bench-run@1",)),
        frozenset(("bench-report@2",)),
        frozenset((False, True)),
    ),
    "checker": (
        "builtin_checker_profile@1",
        "checker-profile-config@1",
        frozenset(("checker-run@1", "patch-repair@1", "patch-validation@1", "review-run@1")),
        frozenset(("checker-report@1", "regression-evidence@1")),
        frozenset((False,)),
    ),
    "config_export": (
        "builtin_config_export_profile@1",
        "config_export-profile-config@1",
        frozenset(("generation-propose@1", "patch-repair@1")),
        frozenset(("config-export-package@1",)),
        frozenset((False,)),
    ),
    "constraint_extraction": (
        "builtin_constraint_extraction_profile@1",
        "constraint_extraction-profile-config@1",
        frozenset(("constraint-proposal-propose@1",)),
        frozenset(("constraint-proposal@1",)),
        frozenset((False,)),
    ),
    "environment": (
        "builtin_environment_profile@1",
        "environment-profile-config@1",
        frozenset(("playtest-run@1", "task-suite-derive@1")),
        frozenset(("playtest-trace@1", "scenario-spec@1")),
        frozenset((False,)),
    ),
    "generation": (
        "builtin_generation_profile@1",
        "generation-profile-config@1",
        frozenset(("generation-propose@1",)),
        frozenset(
            (
                "checker-report@1",
                "config-export-package@1",
                "ir-core@1",
                "patch@2",
                "review@1",
                "simulation-result@1",
            )
        ),
        frozenset((False,)),
    ),
    "impact_analysis": (
        "builtin_impact_analysis_profile@1",
        "impact_analysis-profile-config@1",
        frozenset(("rollback-validation@1",)),
        frozenset(("regression-evidence@1",)),
        frozenset((False,)),
    ),
    "llm_triage": (
        "builtin_llm_triage_profile@1",
        "llm_triage-profile-config@1",
        frozenset(("review-run@1",)),
        frozenset(("review@1",)),
        frozenset((False,)),
    ),
    "patch_repair": (
        "builtin_patch_repair_profile@1",
        "patch_repair-profile-config@1",
        frozenset(("patch-repair@1",)),
        frozenset(
            (
                "checker-report@1",
                "config-export-package@1",
                "ir-core@1",
                "patch@2",
                "regression-evidence@1",
                "simulation-result@1",
            )
        ),
        frozenset((False,)),
    ),
    "playtest_planner": (
        "builtin_playtest_planner_profile@2",
        "playtest_planner-profile-config@2",
        frozenset(("playtest-run@1",)),
        frozenset(("playtest-trace@1",)),
        frozenset((False,)),
    ),
    "review": (
        "builtin_review_profile@1",
        "review-profile-config@1",
        frozenset(("review-run@1",)),
        frozenset(("review@1",)),
        frozenset((False,)),
    ),
    "rollback": (
        "builtin_rollback_profile@1",
        "rollback-profile-config@1",
        frozenset(("rollback-validation@1",)),
        frozenset(("evidence-set@1", "regression-evidence@1")),
        frozenset((False,)),
    ),
    "schema_compatibility": (
        "builtin_schema_compatibility_profile@1",
        "schema_compatibility-profile-config@1",
        frozenset(("rollback-validation@1",)),
        frozenset(("regression-evidence@1",)),
        frozenset((False,)),
    ),
    "simulation": (
        "builtin_simulation_profile@1",
        "simulation-profile-config@1",
        frozenset(("patch-repair@1", "patch-validation@1", "review-run@1", "simulation-run@1")),
        frozenset(("regression-evidence@1", "simulation-result@1")),
        frozenset((True,)),
    ),
    "task_suite_derivation": (
        "builtin_task_suite_derivation_profile@2",
        "task_suite_derivation-profile-config@2",
        frozenset(("task-suite-derive@1",)),
        frozenset(("scenario-spec@1", "task-suite@1")),
        frozenset((False,)),
    ),
    "validation": (
        "builtin_validation_profile@1",
        "validation-profile-config@1",
        frozenset(("constraint-validation@1", "patch-validation@1")),
        frozenset(("auto-apply-proof@1", "evidence-set@1")),
        frozenset((False,)),
    ),
    "workload": (
        "builtin_workload_profile@1",
        "workload-profile-config@1",
        frozenset(("simulation-run@1",)),
        frozenset(("simulation-result@1",)),
        frozenset((False,)),
    ),
}


def _build_exact_profile_binding_validator(
    registry: ImmutablePlatformRegistry,
) -> ExactProfileBindingValidator:
    """Re-resolve frozen handler bindings against retained catalog authority."""

    def validate(
        binding: ResolvedExecutionProfileBindingV1,
        *,
        llm_execution_mode: LlmExecutionMode,
        run_kind: RunKindRef,
    ) -> None:
        definition, lifecycle = registry.resolve_execution_profile_binding(binding)
        contract = _RUN_HANDLER_PROFILE_ADAPTER_CONTRACTS.get(binding.expected_profile_kind)
        if contract is None:
            raise IntegrityViolation(
                "execution profile kind has no registered handler adapter contract",
                field_path=binding.field_path,
            )
        handler_key, config_schema_id, inputs, outputs, stochastic_values = contract
        if (
            definition.profile != binding.profile
            or definition.profile_kind != binding.expected_profile_kind
            or run_kind not in definition.compatible_run_kinds
            or definition.handler_key != handler_key
            or definition.config_schema_id != config_schema_id
            or frozenset(definition.input_schema_ids) != inputs
            or frozenset(definition.output_schema_ids) != outputs
            or definition.stochastic not in stochastic_values
            or definition.required_capabilities
        ):
            raise IntegrityViolation(
                "execution profile definition is incompatible with the exact Run binding",
                field_path=binding.field_path,
            )
        if lifecycle.state == "disabled" or (
            lifecycle.state == "replay_only" and llm_execution_mode != "replay"
        ):
            raise IntegrityViolation(
                "execution profile lifecycle forbids this Run mode",
                field_path=binding.field_path,
            )

    return validate


def _resolve_handler_executor_definition(
    registry: ImmutablePlatformRegistry,
    binding: ResolvedExecutionProfileBindingV1,
    *,
    expected_kind: str,
    handler_key: str,
    config_schema_id: str,
) -> ExecutionProfileDefinitionV1:
    """Resolve one exact binding and close the built-in adapter/config contract."""

    definition, _lifecycle = registry.resolve_execution_profile_binding(binding)
    contract = _RUN_HANDLER_PROFILE_ADAPTER_CONTRACTS.get(expected_kind)
    if contract is None:
        raise IntegrityViolation(
            "execution profile kind has no registered handler adapter contract"
        )
    (
        expected_handler_key,
        expected_config_schema_id,
        inputs,
        outputs,
        stochastic_values,
    ) = contract
    if (
        definition.profile != binding.profile
        or definition.profile_kind != expected_kind
        or definition.handler_key != handler_key
        or definition.config_schema_id != config_schema_id
        or handler_key != expected_handler_key
        or config_schema_id != expected_config_schema_id
        or frozenset(definition.input_schema_ids) != inputs
        or frozenset(definition.output_schema_ids) != outputs
        or definition.stochastic not in stochastic_values
        or definition.required_capabilities
    ):
        raise IntegrityViolation(
            "execution profile does not authorize the built-in handler adapter",
            field_path=binding.field_path,
        )
    return definition


def _build_checker_resolver(registry: ImmutablePlatformRegistry):
    policy_resolver = _build_checker_execution_policy_resolver(registry)

    def resolve(
        binding: ResolvedExecutionProfileBindingV1,
        constraints: list[Constraint],
    ) -> Checker:
        definition = _resolve_handler_executor_definition(
            registry,
            binding,
            expected_kind="checker",
            handler_key="builtin_checker_profile@1",
            config_schema_id="checker-profile-config@1",
        )
        policy = policy_resolver(binding)
        # Compilation constructs solver-backed checker objects. Reject the exact
        # retained profile's constraint cap before ``compile_all`` so a repair
        # request cannot multiply an oversized set across many profiles before
        # the first candidate verification call.
        if len(constraints) > policy.max_constraint_count:
            raise IntegrityViolation(
                "checker constraint set exceeds the exact profile count budget"
            )
        return _CheckerGroup(
            profile=definition.profile,
            checkers=(GraphChecker(), *compile_all(constraints)),
            policy=policy,
            constraint_count=len(constraints),
        )

    return resolve


def _build_checker_execution_policy_resolver(registry: ImmutablePlatformRegistry):
    def resolve(binding: ResolvedExecutionProfileBindingV1) -> CheckerExecutionPolicy:
        definition = _resolve_handler_executor_definition(
            registry,
            binding,
            expected_kind="checker",
            handler_key="builtin_checker_profile@1",
            config_schema_id="checker-profile-config@1",
        )
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
        simulation_profile: ResolvedExecutionProfileBindingV1,
        workload_profile: ResolvedExecutionProfileBindingV1,
    ) -> SimulationExecutionBudget:
        simulation_definition = _resolve_handler_executor_definition(
            registry,
            simulation_profile,
            expected_kind="simulation",
            handler_key="builtin_simulation_profile@1",
            config_schema_id="simulation-profile-config@1",
        )
        workload_definition = _resolve_handler_executor_definition(
            registry,
            workload_profile,
            expected_kind="workload",
            handler_key="builtin_workload_profile@1",
            config_schema_id="workload-profile-config@1",
        )
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
    def resolve(binding: ResolvedExecutionProfileBindingV1) -> GenerationExecutionConfig:
        definition = _resolve_handler_executor_definition(
            registry,
            binding,
            expected_kind="generation",
            handler_key="builtin_generation_profile@1",
            config_schema_id="generation-profile-config@1",
        )
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
    def resolve(binding: ResolvedExecutionProfileBindingV1) -> RepairExecutionConfig:
        definition = _resolve_handler_executor_definition(
            registry,
            binding,
            expected_kind="patch_repair",
            handler_key="builtin_patch_repair_profile@1",
            config_schema_id="patch_repair-profile-config@1",
        )
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
    def resolve(binding: ResolvedExecutionProfileBindingV1) -> ReviewExecutionConfig:
        definition = _resolve_review_executor_definition(
            registry,
            binding,
            expected_kind="review",
        )
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


def _build_review_checker_policy_resolver(registry: ImmutablePlatformRegistry):
    def resolve(binding: ResolvedExecutionProfileBindingV1) -> CheckerExecutionPolicy:
        definition = _resolve_review_executor_definition(
            registry,
            binding,
            expected_kind="checker",
        )
        config = CheckerProfileConfigV1.model_validate(definition.config)
        return CheckerExecutionPolicy(
            allowed_checker_ids=config.allowed_checker_ids,
            allowed_defect_classes=config.allowed_defect_classes,
            max_direct_checker_count=config.max_direct_checker_count,
            max_constraint_count=config.max_constraint_count,
            max_work_units=config.max_work_units,
        )

    return resolve


def _build_constraint_extraction_execution_config_resolver(
    registry: ImmutablePlatformRegistry,
):
    def resolve(
        binding: ResolvedExecutionProfileBindingV1,
    ) -> ConstraintExtractionExecutionConfig:
        definition = _resolve_handler_executor_definition(
            registry,
            binding,
            expected_kind="constraint_extraction",
            handler_key="builtin_constraint_extraction_profile@1",
            config_schema_id="constraint_extraction-profile-config@1",
        )
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
    def resolve(binding: ResolvedExecutionProfileBindingV1) -> ReviewSimConfig:
        definition = _resolve_handler_executor_definition(
            registry,
            binding,
            expected_kind="simulation",
            handler_key="builtin_simulation_profile@1",
            config_schema_id="simulation-profile-config@1",
        )
        config = SimulationProfileConfigV1.model_validate(definition.config)
        return ReviewSimConfig(
            n_agents=config.default_population,
            n_ticks=config.default_horizon_steps,
            max_work_units=config.max_work_units,
        )

    return resolve


def _resolve_review_executor_definition(
    registry: ImmutablePlatformRegistry,
    binding: ResolvedExecutionProfileBindingV1,
    *,
    expected_kind: str,
) -> ExecutionProfileDefinitionV1:
    definition, _lifecycle = registry.resolve_execution_profile_binding(binding)
    contracts = {
        "review": (
            "builtin_review_profile@1",
            "review-profile-config@1",
            {"review-run@1"},
            {"review@1"},
            {RunKindRef(kind="review.run", version=1)},
            False,
        ),
        "checker": (
            "builtin_checker_profile@1",
            "checker-profile-config@1",
            {"checker-run@1", "patch-repair@1", "patch-validation@1", "review-run@1"},
            {"checker-report@1", "regression-evidence@1"},
            {
                RunKindRef(kind="checker.run", version=1),
                RunKindRef(kind="patch.repair", version=1),
                RunKindRef(kind="patch.validate", version=1),
                RunKindRef(kind="review.run", version=1),
            },
            False,
        ),
        "simulation": (
            "builtin_simulation_profile@1",
            "simulation-profile-config@1",
            {"patch-repair@1", "patch-validation@1", "review-run@1", "simulation-run@1"},
            {"regression-evidence@1", "simulation-result@1"},
            {
                RunKindRef(kind="patch.repair", version=1),
                RunKindRef(kind="patch.validate", version=1),
                RunKindRef(kind="review.run", version=1),
                RunKindRef(kind="simulation.run", version=1),
            },
            True,
        ),
        "llm_triage": (
            "builtin_llm_triage_profile@1",
            "llm_triage-profile-config@1",
            {"review-run@1"},
            {"review@1"},
            {RunKindRef(kind="review.run", version=1)},
            False,
        ),
    }
    handler_key, config_schema_id, inputs, outputs, compatible_kinds, stochastic = contracts[
        expected_kind
    ]
    if (
        definition.profile != binding.profile
        or definition.profile_kind != expected_kind
        or set(definition.compatible_run_kinds) != compatible_kinds
        or definition.handler_key != handler_key
        or definition.config_schema_id != config_schema_id
        or set(definition.input_schema_ids) != inputs
        or set(definition.output_schema_ids) != outputs
        or definition.stochastic is not stochastic
        or definition.required_capabilities
    ):
        raise IntegrityViolation(
            "review executor profile does not authorize the built-in adapter",
            field_path=binding.field_path,
        )
    return definition


def _build_review_triage_profile_authorizer(registry: ImmutablePlatformRegistry):
    def authorize(binding: ResolvedExecutionProfileBindingV1) -> None:
        _resolve_review_executor_definition(
            registry,
            binding,
            expected_kind="llm_triage",
        )

    return authorize


def _build_review_checker_resolver(registry: ImmutablePlatformRegistry):
    policy_resolver = _build_review_checker_policy_resolver(registry)

    def resolve(
        binding: ResolvedExecutionProfileBindingV1,
        constraints: list[Constraint],
    ) -> Checker:
        definition = _resolve_review_executor_definition(
            registry,
            binding,
            expected_kind="checker",
        )
        policy = policy_resolver(binding)
        if len(constraints) > policy.max_constraint_count:
            raise IntegrityViolation(
                "review checker constraint set exceeds the exact profile count budget"
            )
        return _CheckerGroup(
            profile=definition.profile,
            checkers=(GraphChecker(), *compile_all(constraints)),
            policy=policy,
            constraint_count=len(constraints),
        )

    return resolve


def _build_review_sim_config_resolver(registry: ImmutablePlatformRegistry):
    def resolve(binding: ResolvedExecutionProfileBindingV1) -> ReviewSimConfig:
        definition = _resolve_review_executor_definition(
            registry,
            binding,
            expected_kind="simulation",
        )
        config = SimulationProfileConfigV1.model_validate(definition.config)
        return ReviewSimConfig(
            n_agents=config.default_population,
            n_ticks=config.default_horizon_steps,
            max_work_units=config.max_work_units,
        )

    return resolve


def _resolve_patch_executor_definition(
    registry: ImmutablePlatformRegistry,
    binding: ResolvedExecutionProfileBindingV1,
    *,
    expected_kind: str,
) -> ExecutionProfileDefinitionV1:
    definition, lifecycle = registry.resolve_execution_profile_binding(binding)
    contracts = {
        "checker": (
            "builtin_checker_profile@1",
            "checker-profile-config@1",
            {"checker-run@1", "patch-repair@1", "patch-validation@1", "review-run@1"},
            {"checker-report@1", "regression-evidence@1"},
            {
                RunKindRef(kind="checker.run", version=1),
                RunKindRef(kind="patch.repair", version=1),
                RunKindRef(kind="patch.validate", version=1),
                RunKindRef(kind="review.run", version=1),
            },
            False,
        ),
        "simulation": (
            "builtin_simulation_profile@1",
            "simulation-profile-config@1",
            {"patch-repair@1", "patch-validation@1", "review-run@1", "simulation-run@1"},
            {"regression-evidence@1", "simulation-result@1"},
            {
                RunKindRef(kind="patch.repair", version=1),
                RunKindRef(kind="patch.validate", version=1),
                RunKindRef(kind="review.run", version=1),
                RunKindRef(kind="simulation.run", version=1),
            },
            True,
        ),
    }
    handler_key, config_schema_id, inputs, outputs, compatible_kinds, stochastic = contracts[
        expected_kind
    ]
    if (
        lifecycle.state != "active"
        or definition.profile != binding.profile
        or definition.profile_kind != expected_kind
        or set(definition.compatible_run_kinds) != compatible_kinds
        or definition.handler_key != handler_key
        or definition.config_schema_id != config_schema_id
        or set(definition.input_schema_ids) != inputs
        or set(definition.output_schema_ids) != outputs
        or definition.stochastic is not stochastic
        or definition.required_capabilities
    ):
        raise IntegrityViolation(
            "patch validation executor profile does not authorize the built-in adapter",
            field_path=binding.field_path,
        )
    return definition


def _build_patch_checker_resolver(registry: ImmutablePlatformRegistry):
    def resolve(
        binding: ResolvedExecutionProfileBindingV1,
        constraints: list[Constraint],
    ) -> Checker:
        definition = _resolve_patch_executor_definition(
            registry,
            binding,
            expected_kind="checker",
        )
        config = CheckerProfileConfigV1.model_validate(definition.config)
        if len(constraints) > config.max_constraint_count:
            raise IntegrityViolation(
                "checker constraint set exceeds the exact profile count budget"
            )
        policy = CheckerExecutionPolicy(
            allowed_checker_ids=config.allowed_checker_ids,
            allowed_defect_classes=config.allowed_defect_classes,
            max_direct_checker_count=config.max_direct_checker_count,
            max_constraint_count=config.max_constraint_count,
            max_work_units=config.max_work_units,
        )
        return _CheckerGroup(
            profile=definition.profile,
            checkers=(GraphChecker(), *compile_all(constraints)),
            policy=policy,
            constraint_count=len(constraints),
        )

    return resolve


def _build_patch_sim_config_resolver(registry: ImmutablePlatformRegistry):
    def resolve(binding: ResolvedExecutionProfileBindingV1) -> ReviewSimConfig:
        definition = _resolve_patch_executor_definition(
            registry,
            binding,
            expected_kind="simulation",
        )
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


def build_rollback_ports(
    *,
    engine: Engine,
    object_store: LocalObjectStore,
    object_store_id: str,
    cursor_signing_key: bytes,
    clock: UtcClock,
) -> tuple[_SqlRollbackHistoryVerifier, None]:
    """Build the SQL history port; schema/impact bind after registry construction.

    The retained call shape is used by ``dispatch.py``.  Exact schema/impact ports
    need both the immutable registry and the identity-aware Artifact reader, which
    are already available to :func:`build_trusted_components`.
    """

    del object_store, object_store_id

    return (
        _SqlRollbackHistoryVerifier(
            engine=engine, cursor_signing_key=cursor_signing_key, clock=clock
        ),
        None,
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
    exact_profile_binding_validator = _build_exact_profile_binding_validator(registry)
    patch_checker_resolver = _build_patch_checker_resolver(registry)
    checker_execution_policy_resolver = _build_checker_execution_policy_resolver(registry)
    review_checker_resolver = _build_review_checker_resolver(registry)
    review_checker_policy_resolver = _build_review_checker_policy_resolver(registry)
    review_triage_profile_authorizer = _build_review_triage_profile_authorizer(registry)
    simulation_execution_budget_resolver = _build_simulation_execution_budget_resolver(registry)
    generation_execution_config_resolver = _build_generation_execution_config_resolver(registry)
    repair_execution_config_resolver = _build_repair_execution_config_resolver(registry)
    review_execution_config_resolver = _build_review_execution_config_resolver(registry)
    constraint_extraction_execution_config_resolver = (
        _build_constraint_extraction_execution_config_resolver(registry)
    )
    sim_config_resolver = _build_sim_config_resolver(registry)
    patch_sim_config_resolver = _build_patch_sim_config_resolver(registry)
    review_sim_config_resolver = _build_review_sim_config_resolver(registry)
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
    history_verifier = rollback_history_verifier or _SqlRollbackHistoryVerifier(
        engine=blobs._engine,
        cursor_signing_key=blobs._cursor_signing_key,
        clock=blobs._clock,
    )
    schema_analyzer = rollback_schema_analyzer or ExactRollbackSchemaAnalyzer(
        artifacts=blobs,
        registry=registry,
    )
    impact_analyzer = DeterministicRollbackImpactAnalyzer(
        artifacts=blobs,
        registry=registry,
    )
    auto_apply_evaluator = build_worker_auto_apply_evaluator(
        registry=registry,
        engine=blobs._engine,
        clock=blobs._clock,
        artifacts=blobs,
    )

    handlers: dict[str, object] = {
        "checker_runner@1": CheckerRunHandler(
            blobs=blobs,
            store=store,
            checker_factory=DefaultCheckerFactory(),
            execution_policy_resolver=checker_execution_policy_resolver,
            finding_head_revision=finding_head_revision,
            profile_binding_validator=exact_profile_binding_validator,
        ),
        "simulation_runner@1": SimulationRunHandler(
            blobs=blobs,
            store=store,
            execution_budget_resolver=simulation_execution_budget_resolver,
            finding_head_revision=finding_head_revision,
            profile_binding_validator=exact_profile_binding_validator,
        ),
        "review_runner@1": ReviewRunHandler(
            blobs=blobs,
            store=store,
            checker_resolver=review_checker_resolver,
            sim_config_resolver=review_sim_config_resolver,
            checker_execution_policy_resolver=review_checker_policy_resolver,
            execution_config_resolver=review_execution_config_resolver,
            finding_head_revision=finding_head_revision,
            triage_profile_authorizer=review_triage_profile_authorizer,
            profile_binding_validator=exact_profile_binding_validator,
        ),
        "bench_runner@1": BenchRunHandler(
            blobs=blobs,
            store=store,
            case_loader=bench_port,
            evaluator=bench_port,
            composer=bench_port,
            aggregate_input_verifier=bench_port,
            profile_binding_validator=exact_profile_binding_validator,
        ),
        "generation_proposer@1": GenerationProposalHandler(
            blobs=blobs,
            store=store,
            agent_runner=M2GenerationAgentRunner(checker_factory=_generation_checker_factory),
            config_exporter=config_exporter,
            finding_loader=finding_revision_loader,
            execution_config_resolver=generation_execution_config_resolver,
            profile_binding_validator=exact_profile_binding_validator,
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
            profile_binding_validator=exact_profile_binding_validator,
        ),
        "constraint_proposer@1": ConstraintProposalHandler(
            blobs=blobs,
            store=store,
            agent_runner=M2ConstraintProposalAgentRunner(),
            execution_config_resolver=(constraint_extraction_execution_config_resolver),
            profile_binding_validator=exact_profile_binding_validator,
        ),
        "task_suite_deriver@1": build_task_suite_handler(
            registry=registry,
            blobs=blobs,
            store=store,
            playtest_payload_validators=playtest_payload_validators,
            scenario_shaper_resolver=task_suite_scenario_shaper_resolver,
            profile_binding_validator=exact_profile_binding_validator,
        ),
        "playtest_runner@1": build_playtest_handler(
            registry=registry,
            blobs=blobs,
            store=store,
            oracle_registry=oracle_registry,
            supported_profiles=supported_profiles,
            finding_head_revision=finding_head_revision,
            playtest_payload_validators=playtest_payload_validators,
            profile_binding_validator=exact_profile_binding_validator,
        ),
        "patch_validator@1": PatchValidationHandler(
            blobs=blobs,
            store=store,
            checker_resolver=patch_checker_resolver,
            sim_config_resolver=patch_sim_config_resolver,
            auto_apply_evaluator=auto_apply_evaluator,
            finding_revision_loader=finding_revision_loader,
            regression_runner=regression_runner,
            profile_binding_validator=exact_profile_binding_validator,
        ),
        "constraint_validator@1": ConstraintValidationHandler(
            blobs=blobs,
            store=store,
            differential_engines=build_differential_engines(),
            profile_resolver=RegistryConstraintValidationProfileResolver(registry),
            regression_runner=regression_runner,
        ),
        "rollback_validator@1": RollbackValidationHandler(
            blobs=blobs,
            store=store,
            history_verifier=history_verifier,
            schema_analyzer=schema_analyzer,
            impact_analyzer=impact_analyzer,
            regression_runner=regression_runner,
            profile_binding_validator=exact_profile_binding_validator,
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
    handler. String/deferred sentinels are forbidden in an executing process. Rollback
    history defaults to the same reader's SQL authority; schema and impact default to
    exact registry-bound Artifact adapters.
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
