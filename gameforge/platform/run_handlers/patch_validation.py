"""``patch_validator@1`` — the deterministic patch-validation handler.

Re-verifies a subject patch's PREVIEW snapshot against the selected
``checker_profiles`` checkers + ``simulation_profiles`` economy simulations,
its bound regression suites, and the exact target Finding / Review / Playtest
supporting evidence, then seals ONE primary
``validation_evidence[evidence-set@1]`` plus one
``regression_evidence[regression-evidence@1]`` per re-verification dimension
(``resolved(patch-validation, regression)``). The verdict is DETERMINISTIC — the
checkers / simulation / regression decide ``passed`` / ``failed`` / ``unproven``;
this kind runs NO LLM (``llm_modes=NA``).

``EvidenceSet.overall_status`` derives from the required dimension statuses (any
failed → failed; else any unproven → unproven; else passed) and IS the selected
outcome code: ``patch_validation_passed`` / ``patch_validation_failed`` /
``patch_validation_unproven``. When the resolved validation profile declares
deterministic auto-apply eligibility AND ``overall_status=passed`` AND the injected
:class:`AutoApplyEvaluator` confirms the required deterministic oracles + outcomes
are satisfied, the handler additionally seals ONE
``validation_evidence[auto-apply-proof@1]`` and picks
``patch_validation_auto_eligible``. Terminal publication and synchronous apply
both resolve the immutable evidence closure and re-run the full
``validate_auto_apply`` guard before workflow CAS.

The frozen ``patch-validation`` policy admits ONLY ``validation_evidence`` +
``regression_evidence`` outputs, so EVERY re-verification dimension (checker /
simulation / regression suite) is captured as a ``resolved(patch-validation,
regression)`` regression-evidence artifact whose id the EvidenceSet requirement
binds. The handler mutates no ApprovalItem / EvidenceSet / ref / audit — it returns
only the sealed ``PreparedRunResult``; the transaction-bound effect consumes and
revalidates the outcome code it picks.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol

from gameforge.contracts.canonical import canonical_sha256, sha256_lowerhex
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    MAX_CHECKER_WORK_UNITS_V1,
    MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
    MAX_SIMULATION_WORK_UNITS_V1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.findings import (
    Finding,
    FindingPayloadV1,
    FindingRevisionV1,
    finding_revision_digest,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    FindingEvidenceBindingV1,
    PatchValidationPayloadV1,
    PreparedArtifact,
    PreparedRunOutcome,
    RunRecord,
)
from gameforge.contracts.lineage import ArtifactV2, ExecutionIdentityV1, VersionTuple
from gameforge.contracts.playtest import PlaytestEpisodeTraceV1, PlaytestTraceV1
from gameforge.contracts.review import ReviewReport
from gameforge.contracts.workflow import (
    AutoApplyEvidenceContextV1,
    AutoApplyOracleAttestationV1,
    AutoApplyOutcomeAttestationV1,
    AutoApplyProofV1,
    AutoApplyPolicyRefV1,
    DeterministicOracleDefinitionV1,
    EvidenceRequirement,
    EvidenceSet,
    PatchTargetBindingV1,
    QualifiedOutcomeRuleRefV1,
)
from gameforge.spine.checkers.base import Checker, CheckerExecutionBinding
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    ExecutorContextLike,
    FindingEvidence,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    load_json_blob,
    prepared_version_tuple,
    rebind_embedded_finding_payload,
    rebind_finding_producers,
    require_exact_profile_bindings,
    resolved_profile,
    store_prepared_artifact,
    trust_typed_profile_binding,
)
from gameforge.platform.run_handlers.readers import (
    ConstraintLoader,
    NavLoader,
    SnapshotLoader,
    load_constraints,
    load_nav,
    load_snapshot,
)
from gameforge.platform.run_handlers.review import ReviewSimConfig
from gameforge.platform.run_handlers.simulation import (
    EconomySimulatorPort,
    unproven_input_application_findings,
    validate_economy_simulation_work_budget,
)
from gameforge.platform.publication.payload_schema import (
    decode_and_validate_artifact_payload,
)
from gameforge.platform.run_handlers.validation_common import (
    AUTO_APPLY_PROOF_SCHEMA_ID,
    DEFAULT_REGRESSION_RUNNER,
    EVIDENCE_SET_SCHEMA_ID,
    PATCH_SIMULATION_EXECUTION_MODE_V1,
    REGRESSION_EVIDENCE_KIND,
    REGRESSION_EVIDENCE_SCHEMA_ID,
    VALIDATION_SEED_DERIVATION_VERSION,
    VALIDATION_EVIDENCE_KIND,
    DimensionResult,
    DimensionStatus,
    RegressionRunner,
    RegressionRunRequest,
    content_addressed_artifact_id,
    derive_validation_subseed,
    deterministic_finding_status,
    digest_of,
    evidence_requirement,
    overall_status_of,
    regression_evidence_version_tuple,
    regression_suite_execution_coverage_binding,
    regression_suite_execution_coverage_marker,
    require_exists,
    validation_child_execution_seed,
    validation_child_seed_evidence,
    validate_authoritative_regression_findings,
    with_validation_child_seed_evidence,
)

VALIDATION_POLICY_FIELD = "/params/validation_policy"
CHECKER_TOOL_VERSION = "checker@1"
SIMULATION_TOOL_VERSION = "economy-sim@1"
REGRESSION_TOOL_VERSION = "regression@1"
EVIDENCE_TOOL_VERSION = "patch-validation@1"
FINDING_TOOL_VERSION = "finding@1"
REVIEW_TOOL_VERSION = "review@1"
PLAYTEST_TOOL_VERSION = "playtest@1"

_OUTCOME_CODE = {
    "passed": "patch_validation_passed",
    "failed": "patch_validation_failed",
    "unproven": "patch_validation_unproven",
}
_AUTO_ELIGIBLE_CODE = "patch_validation_auto_eligible"


def _exact_patch_validation_profile_bindings(
    context: ExecutorContextLike,
    payload: PatchValidationPayloadV1,
    *,
    validator: ExactProfileBindingValidator,
) -> dict[str, ResolvedExecutionProfileBindingV1]:
    expected = {
        VALIDATION_POLICY_FIELD: (payload.validation_policy, "validation"),
        **{
            f"/params/checker_profiles/{index}": (profile, "checker")
            for index, profile in enumerate(payload.checker_profiles)
        },
        **{
            f"/params/simulation_profiles/{index}": (profile, "simulation")
            for index, profile in enumerate(payload.simulation_profiles)
        },
    }
    return require_exact_profile_bindings(
        context,
        expected=expected,
        validator=validator,
    )


@dataclass(frozen=True, slots=True)
class AutoApplyEvaluationRequest:
    """The deterministic validation result an auto-apply evaluator judges."""

    run: RunRecord
    validation_profile: ProfileRefV1
    validation_profile_payload_hash: str
    subject_artifact_id: str
    subject_digest: str
    target_binding: PatchTargetBindingV1
    validation_evidence_artifact_id: str
    regression_evidence_artifact_ids: tuple[str, ...]
    requirements: tuple[EvidenceRequirement, ...]
    evidence_candidates: tuple[AutoApplyEvidenceCandidate, ...]


@dataclass(frozen=True, slots=True)
class AutoApplyEvidenceCandidate:
    """Exact prepared evidence identity available to proof construction.

    The final publisher may reseal sibling IDs, but payload hash, requirement,
    direct input lineage and deterministic oracle coverage are already fixed here.
    Task-9's semantic binder rewrites only the prepared-to-final IDs and verifies
    the hashes/requirement identities against final sibling authority.
    """

    requirement: EvidenceRequirement
    artifact_id: str
    payload_hash: str
    direct_parent_artifact_ids: tuple[str, ...]
    oracle_coverage: tuple[str, ...]
    oracle_attestations: tuple[AutoApplyOracleAttestationV1, ...] = ()
    outcome_attestations: tuple[AutoApplyOutcomeAttestationV1, ...] = ()


@dataclass(frozen=True, slots=True)
class AutoApplyPreparationRequest:
    run: RunRecord
    validation_profile: ProfileRefV1
    validation_profile_payload_hash: str
    subject_artifact_id: str
    subject_digest: str
    target_binding: PatchTargetBindingV1


@dataclass(frozen=True, slots=True)
class AutoApplyQualificationPlan:
    policy: AutoApplyPolicyRefV1
    affected_domain_scope: DomainScope
    deterministic_oracles: tuple[DeterministicOracleDefinitionV1, ...]
    outcome_rules_by_requirement: tuple[tuple[str, tuple[QualifiedOutcomeRuleRefV1, ...]], ...]
    evaluated_scopes_by_requirement: tuple[tuple[str, DomainScope], ...] = ()


@dataclass(frozen=True, slots=True)
class ExactLinkedFindingRevision:
    """One persisted producer link and the exact immutable revision it names."""

    evidence_artifact_id: str
    revision: FindingRevisionV1


class AutoApplyEvaluator(Protocol):
    """Decide deterministic auto-apply eligibility for a PASSED patch validation.

    Returns a fully-built ``AutoApplyProofV1`` when (and only when) the resolved
    validation profile declares auto-apply eligibility and the required
    deterministic oracles + qualified outcomes are satisfied by the result;
    otherwise ``None`` (the run stays ``patch_validation_passed``). The concrete
    impl resolves the frozen auto-apply policy from the registry — the platform
    never hardcodes a policy — and lives in ``apps/worker``.
    """

    def prepare(
        self, request: AutoApplyPreparationRequest
    ) -> AutoApplyQualificationPlan | None: ...

    def evaluate(self, request: AutoApplyEvaluationRequest) -> AutoApplyProofV1 | None: ...


class ExactFindingRevisionLoader(Protocol):
    """Load one immutable Finding revision by its complete admitted identity."""

    def load_many_exact(
        self,
        *,
        bindings: tuple[FindingEvidenceBindingV1, ...],
    ) -> tuple[FindingRevisionV1, ...]: ...

    def list_linked_exact(
        self,
        *,
        evidence_artifact_ids: tuple[str, ...],
    ) -> tuple[ExactLinkedFindingRevision, ...]: ...


class PatchCheckerResolver(Protocol):
    def __call__(
        self,
        binding: ResolvedExecutionProfileBindingV1,
        constraints: list[Constraint],
    ) -> Checker: ...


class PatchSimConfigResolver(Protocol):
    def __call__(self, binding: ResolvedExecutionProfileBindingV1) -> ReviewSimConfig: ...


class _NeverAutoApply:
    """Default evaluator: no profile declares auto-apply → always ``passed``."""

    def prepare(self, request: AutoApplyPreparationRequest) -> AutoApplyQualificationPlan | None:
        del request
        return None

    def evaluate(self, request: AutoApplyEvaluationRequest) -> AutoApplyProofV1 | None:
        return None


class _UnavailableFindingRevisionLoader:
    """Fail closed when a validation request binds Findings without authority."""

    def load_many_exact(
        self,
        *,
        bindings: tuple[FindingEvidenceBindingV1, ...],
    ) -> tuple[FindingRevisionV1, ...]:
        if not bindings:
            return ()
        raise IntegrityViolation(
            "patch_validator@1 requires exact Finding revision authority",
            finding_ids=tuple(binding.finding_id for binding in bindings),
        )

    def list_linked_exact(
        self,
        *,
        evidence_artifact_ids: tuple[str, ...],
    ) -> tuple[ExactLinkedFindingRevision, ...]:
        if not evidence_artifact_ids:
            return ()
        raise IntegrityViolation(
            "patch_validator@1 requires exact Finding revision authority",
            evidence_artifact_ids=evidence_artifact_ids,
        )


@dataclass(frozen=True, slots=True)
class _DimensionArtifact:
    """One dimension's verdict + its sealed regression-evidence + findings."""

    result: DimensionResult
    artifact: PreparedArtifact
    findings: tuple[Finding, ...]
    finding_id_prefix: str
    oracle_coverage: tuple[str, ...] = ()
    oracle_attestations: tuple[AutoApplyOracleAttestationV1, ...] = ()
    outcome_attestations: tuple[AutoApplyOutcomeAttestationV1, ...] = ()


_FindingBindingKey = tuple[str, str, int, str]
_DefectKey = tuple[str, tuple[str, ...], tuple[str, ...], str | None, str | None]


@dataclass(frozen=True, slots=True)
class _HistoricalEvidenceIndex:
    artifact: ArtifactV2
    schema_id: str
    payload: Mapping[str, object]
    finding_payload_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _ReverificationIndex:
    requirement_order: tuple[str, ...]
    requirement_ids_by_coverage: Mapping[str, frozenset[str]]
    findings_by_exact_key: Mapping[tuple[str, _DefectKey], tuple[Finding, ...]]
    findings_by_broad_key: Mapping[tuple[str, _DefectKey], tuple[Finding, ...]]

    def match(
        self,
        *,
        required_coverage: frozenset[str],
        expected: Finding,
    ) -> tuple[tuple[str, ...], tuple[Finding, ...]]:
        covered = frozenset(
            requirement_id
            for marker in required_coverage
            for requirement_id in self.requirement_ids_by_coverage.get(marker, ())
        )
        requirement_ids = tuple(
            requirement_id for requirement_id in self.requirement_order if requirement_id in covered
        )
        exact_episode = _finding_episode_id(expected) is not None
        key = _defect_key(expected, include_episode=exact_episode)
        index = self.findings_by_exact_key if exact_episode else self.findings_by_broad_key
        findings = tuple(
            finding
            for requirement_id in requirement_ids
            for finding in index.get((requirement_id, key), ())
        )
        return requirement_ids, findings


@dataclass(frozen=True, slots=True)
class PatchValidationHandler:
    """A ``RunExecutor`` for ``patch_validator@1`` (deterministic, no LLM)."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    checker_resolver: PatchCheckerResolver
    sim_config_resolver: PatchSimConfigResolver
    auto_apply_evaluator: AutoApplyEvaluator = field(default_factory=_NeverAutoApply)
    finding_revision_loader: ExactFindingRevisionLoader = field(
        default_factory=_UnavailableFindingRevisionLoader
    )
    regression_runner: RegressionRunner = DEFAULT_REGRESSION_RUNNER
    simulator: EconomySimulatorPort = field(default_factory=EconomySimulator)
    snapshot_loader: SnapshotLoader = load_snapshot
    constraint_loader: ConstraintLoader = load_constraints
    nav_loader: NavLoader = load_nav
    profile_binding_validator: ExactProfileBindingValidator = trust_typed_profile_binding

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, PatchValidationPayloadV1):
            raise TypeError("patch_validator@1 requires a patch-validation@1 payload")
        profile_bindings = _exact_patch_validation_profile_bindings(
            context,
            payload,
            validator=self.profile_binding_validator,
        )

        preview = self.snapshot_loader(self.blobs, payload.preview_snapshot_artifact_id)
        nav = self.nav_loader(self.blobs, payload.preview_snapshot_artifact_id)
        root_seed = context.payload.seed
        evidence_tuple = self._evidence_tuple(context, preview)
        constraints = self._constraints(context, payload)
        self._reverify_supporting(payload)
        target_revisions = self._verify_target_finding_closure(payload)
        expected_revisions = self.finding_revision_loader.load_many_exact(
            bindings=payload.expected_findings
        )
        exact_findings = {
            _finding_binding_key(binding): _finding_from_revision(revision)
            for binding, revision in (
                *zip(payload.expected_findings, expected_revisions),
                *zip(payload.findings, target_revisions),
            )
        }
        historical_evidence = self._historical_evidence_indices(payload.expected_findings)
        deterministic_constraint_count = sum(
            not constraint.has_llm_predicate() for constraint in constraints
        )
        checker_work_units = (
            max(
                1,
                len(preview.entities) * len(preview.entities)
                + len(preview.entities)
                + len(preview.relations),
            )
            * (1 + deterministic_constraint_count)
            * len(payload.checker_profiles)
        )
        if checker_work_units > MAX_CHECKER_WORK_UNITS_V1:
            raise IntegrityViolation(
                "patch validation checker profiles exceed the aggregate work budget"
            )

        lineage = self._artifact_lineage(payload)
        target_binding = self._target_binding(payload, preview)
        validation_binding = profile_bindings[VALIDATION_POLICY_FIELD]
        prepare = getattr(self.auto_apply_evaluator, "prepare", None)
        qualification = (
            prepare(
                AutoApplyPreparationRequest(
                    run=context.run,
                    validation_profile=validation_binding.profile,
                    validation_profile_payload_hash=validation_binding.profile_payload_hash,
                    subject_artifact_id=payload.subject.subject_artifact_id,
                    subject_digest=payload.subject.subject_digest,
                    target_binding=target_binding,
                )
            )
            if callable(prepare)
            else None
        )
        auto_apply_context = (
            _auto_apply_evidence_context(
                context.run,
                payload,
                target_binding,
                lineage,
                affected_domain_scope=qualification.affected_domain_scope,
            )
            if qualification is not None
            else None
        )

        checker_dimensions = self._checker_dimensions(
            payload,
            preview,
            nav,
            lineage,
            context.run.run_id,
            root_seed,
            evidence_tuple,
            constraints,
            profile_bindings,
            auto_apply_context,
            qualification,
        )
        simulation_dimensions = self._simulation_dimensions(
            payload,
            preview,
            constraints,
            lineage,
            context.run.run_id,
            context.run.kind,
            root_seed,
            evidence_tuple,
            profile_bindings,
            auto_apply_context,
            qualification,
        )
        regression_dimensions = self._regression_dimensions(
            payload,
            preview,
            lineage,
            context.run.run_id,
            context.run.kind,
            root_seed,
            evidence_tuple,
            auto_apply_context,
            qualification,
        )
        core_dimensions = (
            *checker_dimensions,
            *simulation_dimensions,
            *regression_dimensions,
        )
        dimensions = (
            *core_dimensions,
            *self._supporting_dimensions(
                payload,
                preview,
                lineage,
                evidence_tuple,
                core_dimensions,
                auto_apply_context,
                qualification,
                exact_findings,
                historical_evidence,
            ),
        )
        if not dimensions:
            dimensions = (
                self._status_dimension(
                    lineage=lineage,
                    evidence_tuple=evidence_tuple,
                    requirement_id="validation:required-dimension",
                    dimension="validation_input",
                    tool_version=EVIDENCE_TOOL_VERSION,
                    status="unproven",
                    reason_code="no_validation_dimension_selected",
                    detail={"selected_dimension_count": 0},
                    auto_apply_context=auto_apply_context,
                    qualification=qualification,
                ),
            )
        overall = overall_status_of(tuple(dim.result.status for dim in dimensions))

        requirements = tuple(evidence_requirement(dim.result) for dim in dimensions)
        companion_ids = tuple(content_addressed_artifact_id(dim.artifact) for dim in dimensions)
        regression_ids = tuple(
            companion_ids[index]
            for index, dim in enumerate(dimensions)
            if dim.result.kind == "regression"
        )
        evidence_set = self._seal_evidence_set(
            context,
            payload,
            target_binding,
            requirements,
            companion_ids,
            tuple(sorted({*lineage, *payload.regression_suite_artifact_ids})),
            overall,
            evidence_tuple,
        )

        artifacts: list[PreparedArtifact] = [evidence_set, *(dim.artifact for dim in dimensions)]
        prepared_findings = build_prepared_findings(
            tuple(
                FindingEvidence(
                    finding=finding,
                    evidence_artifact_index=index + 1,
                    finding_id=f"{dim.finding_id_prefix}:{finding.id}",
                )
                for index, dim in enumerate(dimensions)
                for finding in dim.findings
                if _publishable_validation_finding(finding)
            ),
            run_id=context.run.run_id,
        )

        outcome_code = _OUTCOME_CODE[overall]
        if overall == "passed":
            proof = self._maybe_auto_proof(
                context,
                payload,
                target_binding,
                evidence_set,
                regression_ids,
                requirements,
                dimensions,
            )
            if proof is not None:
                # Only already-retained run_input parents belong in the prepared
                # bare lineage.  The publisher injects final EvidenceSet and
                # regression siblings from the typed prepared_rule roles; putting
                # predicted IDs here would create forbidden duplicate/unknown
                # direct parents after sibling reseal.
                proof_lineage = tuple(
                    sorted(
                        {
                            payload.subject.subject_artifact_id,
                            target_binding.target_artifact_id,
                        }
                    )
                )
                artifacts.append(self._seal_auto_proof(proof, proof_lineage, evidence_tuple))
                outcome_code = _AUTO_ELIGIBLE_CODE

        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code=outcome_code,
            primary_index=0,
            artifacts=tuple(artifacts),
            findings=prepared_findings,
        )

    # --------------------------------------------------------------- dimensions
    def _checker_dimensions(
        self,
        payload: PatchValidationPayloadV1,
        preview: Snapshot,
        nav: NavProvider | None,
        lineage: tuple[str, ...],
        run_id: str,
        root_seed: int | None,
        evidence_tuple: VersionTuple,
        constraints: tuple[Constraint, ...],
        profile_bindings: Mapping[str, ResolvedExecutionProfileBindingV1],
        auto_apply_context: AutoApplyEvidenceContextV1 | None,
        qualification: AutoApplyQualificationPlan | None,
    ) -> tuple[_DimensionArtifact, ...]:
        dims: list[_DimensionArtifact] = []
        for index, profile in enumerate(payload.checker_profiles):
            requirement_id = f"checker:{_profile_key(profile)}"
            binding = profile_bindings[f"/params/checker_profiles/{index}"]
            checker: Checker = self.checker_resolver(binding, list(constraints))
            findings = tuple(checker.check(preview, nav=nav))
            checker_id = getattr(checker, "id", None)
            execution_bindings = getattr(checker, "executed_checker_bindings", None)
            if execution_bindings is None:
                execution_bindings = (
                    (
                        CheckerExecutionBinding(
                            wrapper_id=checker_id,
                            native_id=checker_id,
                            constraint_id=None,
                        ),
                    )
                    if isinstance(checker_id, str) and checker_id
                    else ()
                )
            elif not isinstance(execution_bindings, tuple) or any(
                not isinstance(value, CheckerExecutionBinding) for value in execution_bindings
            ):
                raise IntegrityViolation("checker returned malformed execution bindings")
            constraint_ids = {constraint.id for constraint in constraints}
            unknown_constraint_ids = tuple(
                sorted(
                    {
                        binding.constraint_id
                        for binding in execution_bindings
                        if binding.constraint_id is not None
                        and binding.constraint_id not in constraint_ids
                    }
                )
            )
            if unknown_constraint_ids:
                raise IntegrityViolation(
                    "checker execution binding names a constraint outside the exact loaded set",
                    unknown_constraint_ids=unknown_constraint_ids,
                )
            _validate_patch_checker_findings(
                findings,
                snapshot_id=preview.snapshot_id,
                constraints=constraints,
                execution_bindings=execution_bindings,
            )
            coverage = tuple(
                sorted(
                    {
                        _checker_coverage_marker(
                            native_id=binding.native_id,
                            constraint_id=binding.constraint_id,
                            profile=profile,
                            constraint_snapshot_artifact_id=(
                                payload.constraint_snapshot_artifact_id
                            ),
                        )
                        for binding in execution_bindings
                    }
                )
            )
            dimension = self._reverification_dimension(
                preview,
                lineage,
                root_seed,
                evidence_tuple,
                kind="checker",
                tool_version=CHECKER_TOOL_VERSION,
                requirement_id=requirement_id,
                findings=findings,
                producer_run_id=run_id,
                oracle_coverage=coverage,
                auto_apply_context=auto_apply_context,
                qualification=qualification,
                engine_version=str(profile.version),
                executed_engine_ids=tuple(
                    sorted(
                        {
                            binding.native_id
                            for binding in execution_bindings
                            if binding.constraint_id is None
                        }
                    )
                ),
                checker_profile=profile,
                checker_execution_bindings=execution_bindings,
                checker_constraint_snapshot_artifact_id=(payload.constraint_snapshot_artifact_id),
            )
            dims.append(dimension)
        return tuple(dims)

    def _simulation_dimensions(
        self,
        payload: PatchValidationPayloadV1,
        preview: Snapshot,
        constraints: tuple[Constraint, ...],
        lineage: tuple[str, ...],
        run_id: str,
        run_kind: RunKindRef,
        root_seed: int | None,
        evidence_tuple: VersionTuple,
        profile_bindings: Mapping[str, ResolvedExecutionProfileBindingV1],
        auto_apply_context: AutoApplyEvidenceContextV1 | None,
        qualification: AutoApplyQualificationPlan | None,
    ) -> tuple[_DimensionArtifact, ...]:
        if not payload.simulation_profiles:
            return ()
        model = EconomyModel.from_snapshot(preview)
        dims: list[_DimensionArtifact] = []
        resolved_configs: list[tuple[ProfileRefV1, ReviewSimConfig]] = []
        total_work_units = 0
        for index, profile in enumerate(payload.simulation_profiles):
            binding = profile_bindings[f"/params/simulation_profiles/{index}"]
            config = self.sim_config_resolver(binding)
            total_work_units += validate_economy_simulation_work_budget(
                model,
                n_agents=config.n_agents,
                n_ticks=config.n_ticks,
                replication_count=1,
                max_work_units=config.max_work_units,
            )
            if total_work_units > MAX_SIMULATION_WORK_UNITS_V1:
                raise IntegrityViolation(
                    "patch validation simulations exceed the aggregate work budget"
                )
            resolved_configs.append((profile, config))
        for profile, config in resolved_configs:
            if root_seed is None:
                raise ValueError("patch validation simulation requires the frozen root seed")
            requirement_id = f"simulation:{_profile_key(profile)}"
            execution_seed = derive_validation_subseed(
                root_seed=root_seed,
                run_kind=run_kind,
                profile=profile,
                case_id=requirement_id,
                replication_index=0,
            )
            result = self.simulator.run(
                model,
                seed=execution_seed,
                n_agents=config.n_agents,
                n_ticks=config.n_ticks,
            )
            input_application_findings = unproven_input_application_findings(
                snapshot_id=preview.snapshot_id,
                constraints=constraints,
                scenario=None,
            )
            if payload.constraint_snapshot_artifact_id is not None and not constraints:
                input_application_findings.append(
                    _empty_constraint_snapshot_unproven_finding(
                        snapshot_id=preview.snapshot_id,
                        constraint_snapshot_artifact_id=(payload.constraint_snapshot_artifact_id),
                    )
                )
            findings = tuple(
                (
                    *to_findings(result, preview.snapshot_id, model),
                    *input_application_findings,
                )
            )
            _validate_patch_simulation_findings(
                findings,
                snapshot_id=preview.snapshot_id,
            )
            execution_binding = _simulation_companion_evidence(
                producer_id="economy_sim",
                profile=profile,
                constraint_snapshot_artifact_id=(payload.constraint_snapshot_artifact_id),
                constraints=constraints,
                n_agents=config.n_agents,
                n_ticks=config.n_ticks,
                root_seed=root_seed,
                run_kind=run_kind,
                case_id=requirement_id,
                execution_seed=execution_seed,
            )
            coverage = (
                ()
                if payload.constraint_snapshot_artifact_id is not None
                else (
                    _simulation_coverage_marker(
                        execution_binding=execution_binding,
                    ),
                )
            )
            dimension = self._reverification_dimension(
                preview,
                lineage,
                root_seed,
                evidence_tuple,
                kind="simulation",
                tool_version=SIMULATION_TOOL_VERSION,
                requirement_id=requirement_id,
                findings=findings,
                producer_run_id=run_id,
                execution_seed=execution_seed,
                seed_run_kind=run_kind,
                seed_profile=profile,
                oracle_coverage=coverage,
                auto_apply_context=auto_apply_context,
                qualification=qualification,
                engine_kind="simulation",
                engine_id="economy_sim",
                engine_version=str(profile.version),
                executed_engine_ids=("economy_sim",),
                simulation_execution_binding=execution_binding,
            )
            dims.append(dimension)
        return tuple(dims)

    def _regression_dimensions(
        self,
        payload: PatchValidationPayloadV1,
        preview: Snapshot,
        lineage: tuple[str, ...],
        run_id: str,
        run_kind: RunKindRef,
        root_seed: int | None,
        evidence_tuple: VersionTuple,
        auto_apply_context: AutoApplyEvidenceContextV1 | None,
        qualification: AutoApplyQualificationPlan | None,
    ) -> tuple[_DimensionArtifact, ...]:
        dims: list[_DimensionArtifact] = []
        remaining_work_units = MAX_REPAIR_REGRESSION_WORK_UNITS_V1
        for suite_id in payload.regression_suite_artifact_ids:
            require_exists(self.blobs, suite_id)
            execution_seed = validation_child_execution_seed(
                root_seed=root_seed,
                run_kind=run_kind,
                profile=payload.validation_policy,
                case_id=suite_id,
            )
            outcome = self.regression_runner.run(
                RegressionRunRequest(
                    suite_artifact_id=suite_id,
                    snapshot_id=preview.snapshot_id,
                    seed=execution_seed,
                    snapshot=preview,
                    root_seed=root_seed,
                    run_kind=run_kind,
                    profile=payload.validation_policy,
                    max_action_work_units=remaining_work_units,
                )
            )
            if outcome.suite_artifact_id != suite_id:
                raise IntegrityViolation("regression runner returned another suite Artifact")
            rebound_outcome_payload = rebind_embedded_finding_payload(
                outcome.payload,
                run_id=run_id,
            )
            returned_snapshot_id = rebound_outcome_payload.get("snapshot_id")
            if returned_snapshot_id is not None and returned_snapshot_id != preview.snapshot_id:
                raise IntegrityViolation("regression runner returned another target snapshot")
            measured_work = outcome.action_work_units
            if measured_work is not None and (
                isinstance(measured_work, bool)
                or not isinstance(measured_work, int)
                or measured_work < 0
            ):
                raise IntegrityViolation("regression runner returned invalid measured work")
            if outcome.status in {"passed", "failed"} and measured_work is None:
                raise IntegrityViolation(
                    "executed regression omitted measured action work",
                    suite_artifact_id=suite_id,
                )
            if measured_work is not None:
                if measured_work > remaining_work_units:
                    raise IntegrityViolation(
                        "patch validation regressions exceed the aggregate work budget",
                        suite_artifact_id=suite_id,
                        remaining_work_units=remaining_work_units,
                        measured_work_units=measured_work,
                    )
                remaining_work_units -= measured_work
            status = "unproven" if outcome.status == "not_executed" else outcome.status
            reason_code = outcome.reason_code
            raw_findings = rebound_outcome_payload.get("findings")
            suite_findings: tuple[Finding, ...] = ()
            if raw_findings is not None:
                if not isinstance(raw_findings, (list, tuple)):
                    raise IntegrityViolation("regression runner returned invalid Findings")
                try:
                    suite_findings = tuple(
                        Finding.model_validate(finding) for finding in raw_findings
                    )
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation("regression runner returned invalid Findings") from exc
                if any(finding.snapshot_id != preview.snapshot_id for finding in suite_findings):
                    raise IntegrityViolation("regression runner Finding targets another snapshot")
                validate_authoritative_regression_findings(
                    suite_findings,
                    snapshot_id=preview.snapshot_id,
                )
                if suite_findings and status != deterministic_finding_status(suite_findings):
                    raise IntegrityViolation(
                        "regression runner status contradicts exact Finding verdicts"
                    )
            if status == "failed" and not suite_findings:
                raise IntegrityViolation("failed regression runner omitted Findings")
            if status in {"passed", "failed"} and outcome.env_contract_version is None:
                status = "unproven"
                reason_code = "regression_environment_binding_unavailable"
            if status == "unproven" and reason_code is None:
                reason_code = "regression_not_executed"
            execution_coverage_binding = None
            execution_coverage_marker = None
            if (
                status in {"passed", "failed"}
                and outcome.env_contract_version is not None
                and root_seed is not None
            ):
                execution_coverage_binding = regression_suite_execution_coverage_binding(
                    suite_artifact_id=suite_id,
                    validation_profile=payload.validation_policy,
                    constraint_snapshot_artifact_id=(payload.constraint_snapshot_artifact_id),
                    env_contract_version=outcome.env_contract_version,
                    root_seed=root_seed,
                    run_kind=run_kind,
                    execution_seed=execution_seed,
                )
                execution_coverage_marker = regression_suite_execution_coverage_marker(
                    execution_coverage_binding
                )
            regression_tuple = regression_evidence_version_tuple(evidence_tuple, outcome)
            artifact_lineage = tuple(sorted({*lineage, suite_id}))
            sealed_payload = with_validation_child_seed_evidence(
                {
                    **rebound_outcome_payload,
                    "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                    "requirement_id": f"regression:{suite_id}",
                    "suite_artifact_id": suite_id,
                    "lineage_suite_artifact_ids": [suite_id],
                    "snapshot_id": preview.snapshot_id,
                    "seed": execution_seed,
                    "status": status,
                    "reason_code": reason_code if status == "unproven" else None,
                    **(
                        {}
                        if execution_coverage_binding is None
                        else {"execution_coverage_binding": execution_coverage_binding}
                    ),
                    **_auto_apply_payload_fields(
                        auto_apply_context,
                        qualification=qualification,
                        lineage=artifact_lineage,
                        requirement_id=f"regression:{suite_id}",
                        status=status,
                        tool_version=REGRESSION_TOOL_VERSION,
                        deterministic_outcome=True,
                    ),
                },
                root_seed=root_seed,
                execution_seed=execution_seed,
                run_kind=run_kind,
                profile=payload.validation_policy,
                case_id=suite_id,
            )
            artifact = store_prepared_artifact(
                self.store,
                kind=REGRESSION_EVIDENCE_KIND,
                payload_schema_id=REGRESSION_EVIDENCE_SCHEMA_ID,
                version_tuple=regression_tuple,
                lineage=artifact_lineage,
                payload=sealed_payload,
                extra_meta={"requirement_id": f"regression:{suite_id}"},
            )
            dims.append(
                _DimensionArtifact(
                    result=DimensionResult(
                        requirement_id=f"regression:{suite_id}",
                        kind="regression",
                        tool_version=REGRESSION_TOOL_VERSION,
                        status=status,  # type: ignore[arg-type]
                        evidence_artifact_id=content_addressed_artifact_id(artifact),
                        reason_code=reason_code if status == "unproven" else None,
                    ),
                    artifact=artifact,
                    findings=suite_findings,
                    finding_id_prefix=f"regression:{suite_id}",
                    oracle_coverage=(
                        () if execution_coverage_marker is None else (execution_coverage_marker,)
                    ),
                    **_dimension_attestations(sealed_payload),
                )
            )
        return tuple(dims)

    def _reverification_dimension(
        self,
        preview: Snapshot,
        lineage: tuple[str, ...],
        root_seed: int | None,
        evidence_tuple: VersionTuple,
        *,
        kind: str,
        tool_version: str,
        requirement_id: str,
        findings: tuple[Finding, ...],
        producer_run_id: str,
        execution_seed: int | None = None,
        seed_run_kind: RunKindRef | None = None,
        seed_profile: ProfileRefV1 | None = None,
        oracle_coverage: tuple[str, ...] = (),
        auto_apply_context: AutoApplyEvidenceContextV1 | None = None,
        qualification: AutoApplyQualificationPlan | None = None,
        engine_kind: str | None = None,
        engine_id: str | None = None,
        engine_version: str | None = None,
        executed_engine_ids: tuple[str, ...] = (),
        checker_profile: ProfileRefV1 | None = None,
        checker_execution_bindings: tuple[CheckerExecutionBinding, ...] = (),
        checker_constraint_snapshot_artifact_id: str | None = None,
        simulation_execution_binding: Mapping[str, object] | None = None,
        deterministic_outcome: bool = True,
    ) -> _DimensionArtifact:
        findings = tuple(rebind_finding_producers(findings, run_id=producer_run_id))
        status = _dimension_status(findings)
        reason_code = f"{kind}_reported_unproven" if status == "unproven" else None
        seed_evidence = validation_child_seed_evidence(
            root_seed=root_seed,
            execution_seed=execution_seed,
            run_kind=seed_run_kind,
            profile=seed_profile,
            case_id=requirement_id,
        )
        checker_evidence = _checker_companion_evidence(
            profile=checker_profile,
            execution_bindings=checker_execution_bindings,
            constraint_snapshot_artifact_id=checker_constraint_snapshot_artifact_id,
        )
        simulation_evidence = (
            {}
            if simulation_execution_binding is None
            else {"simulation_execution_binding": dict(simulation_execution_binding)}
        )
        artifact_payload: dict[str, object]
        if status == "unproven":
            artifact_payload = {
                "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                "requirement_id": requirement_id,
                "dimension": kind,
                "lineage_suite_artifact_ids": [],
                **checker_evidence,
                **simulation_evidence,
                "status": status,
                "reason_code": reason_code,
                "detail": {
                    "snapshot_id": preview.snapshot_id,
                    "findings": [finding.model_dump(mode="json") for finding in findings],
                    **seed_evidence,
                },
                **_auto_apply_payload_fields(
                    auto_apply_context,
                    qualification=qualification,
                    lineage=lineage,
                    requirement_id=requirement_id,
                    status=status,
                    tool_version=tool_version,
                    engine_kind=engine_kind,
                    engine_id=engine_id,
                    engine_version=engine_version,
                    executed_engine_ids=executed_engine_ids,
                    deterministic_outcome=deterministic_outcome,
                ),
            }
        else:
            artifact_payload = {
                "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                "requirement_id": requirement_id,
                "dimension": kind,
                "lineage_suite_artifact_ids": [],
                **checker_evidence,
                **simulation_evidence,
                "snapshot_id": preview.snapshot_id,
                "status": status,
                "findings": [finding.model_dump(mode="json") for finding in findings],
                **seed_evidence,
                **_auto_apply_payload_fields(
                    auto_apply_context,
                    qualification=qualification,
                    lineage=lineage,
                    requirement_id=requirement_id,
                    status=status,
                    tool_version=tool_version,
                    engine_kind=engine_kind,
                    engine_id=engine_id,
                    engine_version=engine_version,
                    executed_engine_ids=executed_engine_ids,
                    deterministic_outcome=deterministic_outcome,
                ),
            }
        artifact = store_prepared_artifact(
            self.store,
            kind=REGRESSION_EVIDENCE_KIND,
            payload_schema_id=REGRESSION_EVIDENCE_SCHEMA_ID,
            version_tuple=evidence_tuple,
            lineage=lineage,
            payload=artifact_payload,
            extra_meta={"requirement_id": requirement_id},
        )
        return _DimensionArtifact(
            result=DimensionResult(
                requirement_id=requirement_id,
                kind="regression",
                tool_version=tool_version,
                status=status,
                evidence_artifact_id=content_addressed_artifact_id(artifact),
                reason_code=reason_code if status == "unproven" else None,
            ),
            artifact=artifact,
            findings=findings,
            finding_id_prefix=requirement_id,
            oracle_coverage=oracle_coverage,
            **_dimension_attestations(artifact_payload),
        )

    def _supporting_dimensions(
        self,
        payload: PatchValidationPayloadV1,
        preview: Snapshot,
        lineage: tuple[str, ...],
        evidence_tuple: VersionTuple,
        executed_dimensions: tuple[_DimensionArtifact, ...],
        auto_apply_context: AutoApplyEvidenceContextV1 | None,
        qualification: AutoApplyQualificationPlan | None,
        exact_findings: Mapping[_FindingBindingKey, Finding],
        historical_evidence: Mapping[str, _HistoricalEvidenceIndex | None],
    ) -> tuple[_DimensionArtifact, ...]:
        """Turn exact target/supporting evidence into required dispositions.

        These are re-verification dimensions, not passive lineage decorations: a
        confirmed target Finding or an incomplete deterministic Playtest cannot be
        attached to a passed EvidenceSet.  Each disposition gets its own companion
        regression-evidence artifact so completion can cross-check it exactly.
        """

        target_findings_by_evidence: dict[str, list[Finding]] = {}
        for binding in payload.findings:
            target_findings_by_evidence.setdefault(binding.evidence_artifact_id, []).append(
                exact_findings[_finding_binding_key(binding)]
            )

        playtest_dimensions: list[_DimensionArtifact] = []
        for artifact_id in payload.playtest_trace_artifact_ids:
            status, reason_code, detail, trace_coverage = self._playtest_status(
                artifact_id,
                payload,
                evidence_tuple,
            )
            # The selected trace may be linked to suggestion-only Finding
            # revisions.  Only the trace's deterministic playtest oracle belongs
            # in this authoritative companion; every other linked Finding still
            # gets its independent fail-closed ``finding:*`` disposition below.
            findings = tuple(
                finding
                for finding in target_findings_by_evidence.get(artifact_id, ())
                if _authoritative_selected_playtest_finding(
                    finding,
                    snapshot_id=preview.snapshot_id,
                )
            )
            oracle_coverage = tuple(
                sorted(
                    {
                        *trace_coverage,
                        *(
                            finding.producer_id
                            for finding in findings
                            if finding.source == "playtest"
                            and finding.oracle_type == "deterministic"
                        ),
                    }
                )
            )
            playtest_dimensions.append(
                self._status_dimension(
                    lineage=lineage,
                    evidence_tuple=evidence_tuple,
                    requirement_id=f"playtest:{artifact_id}",
                    dimension="playtest",
                    tool_version=PLAYTEST_TOOL_VERSION,
                    status=status,
                    reason_code=reason_code,
                    detail=detail,
                    findings=findings,
                    oracle_coverage=oracle_coverage,
                    auto_apply_context=auto_apply_context,
                    qualification=qualification,
                    engine_kind="playtest_completion",
                    engine_id="playtest_completion",
                    engine_version="1",
                    executed_engine_ids=("playtest_completion",),
                    deterministic_outcome=True,
                )
            )

        reverification_index = _index_reverification_dimensions(
            (*executed_dimensions, *playtest_dimensions)
        )
        dimensions: list[_DimensionArtifact] = []
        for binding in payload.expected_findings:
            status, reason_code, detail = self._expected_finding_status(
                binding,
                reverification_index,
                exact_findings[_finding_binding_key(binding)],
                historical_evidence.get(binding.evidence_artifact_id),
            )
            dimensions.append(
                self._status_dimension(
                    lineage=lineage,
                    evidence_tuple=evidence_tuple,
                    requirement_id=(
                        f"expected-finding:{binding.finding_id}@{binding.finding_revision}"
                    ),
                    dimension="expected_finding_reverification",
                    tool_version=FINDING_TOOL_VERSION,
                    status=status,
                    reason_code=reason_code,
                    detail=detail,
                    auto_apply_context=auto_apply_context,
                    qualification=qualification,
                    deterministic_outcome=False,
                )
            )
        for binding in payload.findings:
            status, reason_code, detail = self._finding_binding_status(
                binding,
                preview,
                exact_findings[_finding_binding_key(binding)],
            )
            dimensions.append(
                self._status_dimension(
                    lineage=lineage,
                    evidence_tuple=evidence_tuple,
                    requirement_id=(f"finding:{binding.finding_id}@{binding.finding_revision}"),
                    dimension="finding",
                    tool_version=FINDING_TOOL_VERSION,
                    status=status,
                    reason_code=reason_code,
                    detail=detail,
                    auto_apply_context=auto_apply_context,
                    qualification=qualification,
                    deterministic_outcome=False,
                )
            )
        for artifact_id in payload.review_artifact_ids:
            status, reason_code, detail = self._review_status(artifact_id, preview)
            dimensions.append(
                self._status_dimension(
                    lineage=lineage,
                    evidence_tuple=evidence_tuple,
                    requirement_id=f"review:{artifact_id}",
                    dimension="review",
                    tool_version=REVIEW_TOOL_VERSION,
                    status=status,
                    reason_code=reason_code,
                    detail=detail,
                    auto_apply_context=auto_apply_context,
                    qualification=qualification,
                    deterministic_outcome=False,
                )
            )
        dimensions.extend(playtest_dimensions)
        return tuple(dimensions)

    def _expected_finding_status(
        self,
        binding: FindingEvidenceBindingV1,
        executed: _ReverificationIndex,
        expected: Finding,
        historical_evidence: _HistoricalEvidenceIndex | None,
    ) -> tuple[DimensionStatus, str | None, dict[str, object]]:
        """Decide whether the historical defect's exact oracle was re-executed.

        The old Finding disposition is evidence of what the Patch intended to fix,
        not a verdict on the target preview.  It therefore never directly fails the
        candidate. A pass requires positive coverage by the corresponding
        deterministic checker/simulator or the exact selected Playtest episode, and
        absence of the same semantic defect in that fresh output;
        missing/mismatched coverage is unproven.
        """

        detail: dict[str, object] = {
            "finding_id": binding.finding_id,
            "finding_revision": binding.finding_revision,
            "finding_digest": binding.finding_digest,
            "source_artifact_id": binding.evidence_artifact_id,
        }
        detail.update(
            {
                "historical_status": expected.status,
                "historical_snapshot_id": expected.snapshot_id,
                "producer_id": expected.producer_id,
                "source": expected.source,
            }
        )
        required_coverage = self._historical_expected_finding_coverage(
            expected,
            historical_evidence,
        )
        covered_requirement_ids, reproduced = executed.match(
            required_coverage=required_coverage,
            expected=expected,
        )
        if not required_coverage or not covered_requirement_ids:
            return "unproven", "expected_finding_oracle_not_reexecuted", detail
        detail["covered_requirement_ids"] = list(covered_requirement_ids)
        detail["reproduced_count"] = len(reproduced)
        status = _supporting_finding_status(reproduced)
        if status == "failed":
            return "failed", "expected_finding_reproduced", detail
        if status == "unproven":
            return "unproven", "expected_finding_reverification_unproven", detail
        return "passed", None, detail

    def _finding_binding_status(
        self,
        binding: FindingEvidenceBindingV1,
        preview: Snapshot,
        finding: Finding,
    ) -> tuple[DimensionStatus, str | None, dict[str, object]]:
        detail: dict[str, object] = {
            "finding_id": binding.finding_id,
            "finding_revision": binding.finding_revision,
            "finding_digest": binding.finding_digest,
            "source_artifact_id": binding.evidence_artifact_id,
        }
        detail["finding_status"] = finding.status
        detail["oracle_type"] = finding.oracle_type
        if finding.snapshot_id != preview.snapshot_id:
            return "unproven", "finding_target_snapshot_mismatch", detail
        status = _supporting_finding_status((finding,))
        return status, _finding_reason(status, source="finding"), detail

    def _historical_expected_finding_coverage(
        self,
        finding: Finding,
        evidence: _HistoricalEvidenceIndex | None,
    ) -> frozenset[str]:
        """Resolve the historical oracle only from its immutable evidence bytes.

        Finding payload metadata is not execution-profile authority.  A historical
        checker/simulation finding therefore proves coverage only when its bound
        evidence Artifact strictly decodes, targets the same snapshot, embeds the
        same producer/defect, and carries one exact profile binding.  Playtest
        coverage similarly comes only from an exact PlaytestTrace episode/scenario
        pair, never from a profile string self-reported by the Finding.
        """

        if evidence is None:
            return frozenset()
        if evidence.schema_id == "checker-report@1":
            return _historical_checker_coverage(
                evidence.payload,
                finding,
                finding_payload_counts=evidence.finding_payload_counts,
            )
        if evidence.schema_id == "simulation-result@1":
            return _historical_simulation_coverage(evidence.payload, finding)
        if evidence.schema_id == "regression-evidence@1":
            return _historical_regression_coverage(
                evidence.payload,
                finding,
                artifact=evidence.artifact,
                finding_payload_counts=evidence.finding_payload_counts,
            )
        if evidence.schema_id == "playtest-trace@1":
            return _historical_playtest_coverage(
                evidence.payload,
                finding,
                artifact=evidence.artifact,
            )
        return frozenset()

    def _historical_evidence_indices(
        self,
        bindings: tuple[FindingEvidenceBindingV1, ...],
    ) -> dict[str, _HistoricalEvidenceIndex | None]:
        indices: dict[str, _HistoricalEvidenceIndex | None] = {}
        for artifact_id in dict.fromkeys(binding.evidence_artifact_id for binding in bindings):
            try:
                blob = self.blobs.read_bytes(artifact_id)
                artifact = self._load_exact_artifact_envelope(artifact_id, blob=blob)
                if artifact is None:
                    indices[artifact_id] = None
                    continue
                schema_id = artifact.meta.get("payload_schema_id")
                expected_kind = {
                    "checker-report@1": "checker_run",
                    "simulation-result@1": "simulation_run",
                    "regression-evidence@1": "regression_evidence",
                    "playtest-trace@1": "playtest_trace",
                }.get(schema_id)
                if not isinstance(schema_id, str) or artifact.kind != expected_kind:
                    indices[artifact_id] = None
                    continue
                payload = decode_and_validate_artifact_payload(
                    payload_schema_id=schema_id,
                    blob=blob,
                )
                if not isinstance(payload, Mapping):
                    indices[artifact_id] = None
                    continue
                embedded = (
                    _embedded_regression_findings(payload)
                    if schema_id == "regression-evidence@1"
                    else _embedded_findings(payload)
                )
                counts = Counter(_finding_payload_key(finding) for finding in embedded)
                indices[artifact_id] = _HistoricalEvidenceIndex(
                    artifact=artifact,
                    schema_id=schema_id,
                    payload=payload,
                    finding_payload_counts=dict(counts),
                )
            except (IntegrityViolation, KeyError, TypeError, ValueError):
                indices[artifact_id] = None
        return indices

    def _load_exact_artifact_envelope(
        self,
        artifact_id: str,
        *,
        blob: bytes,
    ) -> ArtifactV2 | None:
        """Load one content-addressed envelope without trusting payload identity."""

        load_artifact = getattr(self.blobs, "load_artifact", None)
        if not callable(load_artifact):
            return None
        try:
            artifact = load_artifact(artifact_id)
        except (IntegrityViolation, KeyError, TypeError, ValueError):
            return None
        if not isinstance(artifact, ArtifactV2):
            return None
        if (
            artifact.artifact_id != artifact_id
            or artifact.payload_hash != sha256_lowerhex(blob)
            or artifact.object_ref.sha256 != artifact.payload_hash
            or artifact.object_ref.size_bytes != len(blob)
        ):
            return None
        return artifact

    def _verify_target_finding_closure(
        self, payload: PatchValidationPayloadV1
    ) -> tuple[FindingRevisionV1, ...]:
        """Require target bindings to exactly cover selected evidence links.

        Review payloads may repeat Findings, while PlaytestTrace intentionally does
        not. The persisted ``RunFindingLink`` set is therefore the only complete
        producer-side index. A caller cannot obtain a passing validation by omitting
        a confirmed Finding that was atomically published with a selected evidence
        Artifact.
        """

        evidence_artifact_ids = tuple(
            sorted(
                {
                    *payload.review_artifact_ids,
                    *payload.playtest_trace_artifact_ids,
                    *(binding.evidence_artifact_id for binding in payload.findings),
                }
            )
        )
        if not evidence_artifact_ids:
            return ()
        linked = self.finding_revision_loader.list_linked_exact(
            evidence_artifact_ids=evidence_artifact_ids
        )
        linked_bindings: list[FindingEvidenceBindingV1] = []
        revisions_by_binding: dict[_FindingBindingKey, FindingRevisionV1] = {}
        seen: set[tuple[str, int]] = set()
        for item in linked:
            revision = item.revision
            identity = (revision.finding_id, revision.revision)
            if identity in seen:
                raise IntegrityViolation(
                    "selected evidence repeats an immutable Finding revision",
                    finding_id=revision.finding_id,
                    finding_revision=revision.revision,
                )
            seen.add(identity)
            binding = FindingEvidenceBindingV1(
                finding_id=revision.finding_id,
                finding_revision=revision.revision,
                evidence_artifact_id=item.evidence_artifact_id,
                finding_digest=finding_revision_digest(revision),
            )
            linked_bindings.append(binding)
            revisions_by_binding[_finding_binding_key(binding)] = revision

        provided_keys = tuple(sorted(_finding_binding_key(item) for item in payload.findings))
        linked_keys = tuple(sorted(_finding_binding_key(item) for item in linked_bindings))
        if provided_keys != linked_keys:
            provided_set = set(provided_keys)
            linked_set = set(linked_keys)
            raise IntegrityViolation(
                "Patch target Finding bindings must exactly cover selected evidence links",
                missing_finding_revisions=tuple(
                    f"{item[1]}@{item[2]}" for item in sorted(linked_set - provided_set)
                ),
                extra_finding_revisions=tuple(
                    f"{item[1]}@{item[2]}" for item in sorted(provided_set - linked_set)
                ),
            )
        return tuple(
            revisions_by_binding[_finding_binding_key(binding)] for binding in payload.findings
        )

    def _review_status(
        self,
        artifact_id: str,
        preview: Snapshot,
    ) -> tuple[DimensionStatus, str | None, dict[str, object]]:
        detail: dict[str, object] = {"source_artifact_id": artifact_id}
        try:
            report = ReviewReport.model_validate(load_json_blob(self.blobs, artifact_id))
        except (KeyError, TypeError, ValueError):
            return "unproven", "review_evidence_unreadable", detail
        findings = _review_findings(report)
        detail.update(
            {
                "deterministic_finding_count": len(report.deterministic_findings),
                "simulation_finding_count": len(report.simulation_findings),
                "llm_assisted_finding_count": len(report.llm_assisted_findings),
                "unproven_finding_count": len(report.unproven_findings),
            }
        )
        if report.snapshot_id != preview.snapshot_id or any(
            item.snapshot_id != preview.snapshot_id for item in findings
        ):
            return "unproven", "review_target_snapshot_mismatch", detail
        status = _supporting_finding_status(findings)
        return status, _finding_reason(status, source="review"), detail

    def _playtest_status(
        self,
        artifact_id: str,
        payload: PatchValidationPayloadV1,
        evidence_tuple: VersionTuple,
    ) -> tuple[
        DimensionStatus,
        str | None,
        dict[str, object],
        tuple[str, ...],
    ]:
        detail: dict[str, object] = {"source_artifact_id": artifact_id}
        try:
            blob = self.blobs.read_bytes(artifact_id)
            trace = PlaytestTraceV1.model_validate(
                decode_and_validate_artifact_payload(
                    payload_schema_id="playtest-trace@1",
                    blob=blob,
                )
            )
        except (IntegrityViolation, KeyError, TypeError, ValueError):
            return "unproven", "playtest_evidence_unreadable", detail, ()
        completed = tuple(episode.episode_id for episode in trace.episodes if episode.completed)
        episode_ids = tuple(episode.episode_id for episode in trace.episodes)
        detail.update(
            {
                "config_artifact_id": trace.config_artifact_id,
                "constraint_snapshot_artifact_id": trace.constraint_snapshot_artifact_id,
                "env_contract_version": trace.env_contract_version,
                "episode_count": len(trace.episodes),
                "episode_ids": list(episode_ids),
                "completed_episode_ids": list(completed),
            }
        )
        if trace.config_artifact_id not in payload.candidate_config_export_artifact_ids:
            return "unproven", "playtest_candidate_config_mismatch", detail, ()
        if trace.constraint_snapshot_artifact_id != payload.constraint_snapshot_artifact_id:
            return "unproven", "playtest_constraint_binding_mismatch", detail, ()
        if (
            evidence_tuple.env_contract_version is not None
            and trace.env_contract_version != evidence_tuple.env_contract_version
        ):
            return "unproven", "playtest_environment_binding_mismatch", detail, ()
        artifact = self._load_exact_artifact_envelope(artifact_id, blob=blob)
        if (
            artifact is None
            or artifact.kind != "playtest_trace"
            or artifact.meta.get("payload_schema_id") != "playtest-trace@1"
        ):
            return "unproven", "playtest_execution_authority_unavailable", detail, ()
        bindings = tuple(
            _playtest_execution_binding(
                trace,
                episode,
                artifact=artifact,
                expected_ir_snapshot_id=evidence_tuple.ir_snapshot_id,
                expected_constraint_snapshot_id=evidence_tuple.constraint_snapshot_id,
            )
            for episode in trace.episodes
        )
        if any(binding is None for binding in bindings):
            return "unproven", "playtest_execution_authority_unavailable", detail, ()
        coverage = tuple(sorted(f"playtest:binding:{binding}" for binding in bindings))
        if len(completed) != len(trace.episodes):
            return "failed", "playtest_completion_oracle_failed", detail, coverage
        return "passed", None, detail, coverage

    def _status_dimension(
        self,
        *,
        lineage: tuple[str, ...],
        evidence_tuple: VersionTuple,
        requirement_id: str,
        dimension: str,
        tool_version: str,
        status: DimensionStatus,
        reason_code: str | None,
        detail: Mapping[str, object],
        findings: tuple[Finding, ...] = (),
        oracle_coverage: tuple[str, ...] = (),
        auto_apply_context: AutoApplyEvidenceContextV1 | None = None,
        qualification: AutoApplyQualificationPlan | None = None,
        engine_kind: str | None = None,
        engine_id: str | None = None,
        engine_version: str | None = None,
        executed_engine_ids: tuple[str, ...] = (),
        deterministic_outcome: bool = False,
    ) -> _DimensionArtifact:
        if status == "passed":
            reason_code = None
        elif reason_code is None:
            raise IntegrityViolation(
                "non-passing patch-validation dimension omitted its reason",
                requirement_id=requirement_id,
            )
        artifact_payload = {
            "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
            "requirement_id": requirement_id,
            "dimension": dimension,
            "lineage_suite_artifact_ids": [],
            "status": status,
            "reason_code": reason_code,
            "detail": dict(detail),
            **_auto_apply_payload_fields(
                auto_apply_context,
                qualification=qualification,
                lineage=lineage,
                requirement_id=requirement_id,
                status=status,
                tool_version=tool_version,
                engine_kind=engine_kind,
                engine_id=engine_id,
                engine_version=engine_version,
                executed_engine_ids=executed_engine_ids,
                deterministic_outcome=deterministic_outcome,
            ),
        }
        artifact = store_prepared_artifact(
            self.store,
            kind=REGRESSION_EVIDENCE_KIND,
            payload_schema_id=REGRESSION_EVIDENCE_SCHEMA_ID,
            version_tuple=evidence_tuple,
            lineage=lineage,
            payload=artifact_payload,
            extra_meta={"requirement_id": requirement_id},
        )
        return _DimensionArtifact(
            result=DimensionResult(
                requirement_id=requirement_id,
                kind="regression",
                tool_version=tool_version,
                status=status,
                evidence_artifact_id=content_addressed_artifact_id(artifact),
                reason_code=reason_code if status == "unproven" else None,
            ),
            artifact=artifact,
            findings=findings,
            finding_id_prefix=requirement_id,
            oracle_coverage=oracle_coverage,
            **_dimension_attestations(artifact_payload),
        )

    # --------------------------------------------------------------- evidence set
    def _seal_evidence_set(
        self,
        context: ExecutorContextLike,
        payload: PatchValidationPayloadV1,
        target_binding: PatchTargetBindingV1,
        requirements: tuple[EvidenceRequirement, ...],
        companion_ids: tuple[str, ...],
        lineage: tuple[str, ...],
        overall: str,
        evidence_tuple: VersionTuple,
    ) -> PreparedArtifact:
        supporting = (
            *companion_ids,
            *payload.regression_suite_artifact_ids,
            *(
                (payload.constraint_snapshot_artifact_id,)
                if payload.constraint_snapshot_artifact_id
                else ()
            ),
            *payload.candidate_config_export_artifact_ids,
            *payload.review_artifact_ids,
            *payload.playtest_trace_artifact_ids,
            *(binding.evidence_artifact_id for binding in payload.expected_findings),
            *(binding.evidence_artifact_id for binding in payload.findings),
        )
        evidence_set = EvidenceSet(
            subject_artifact_id=payload.subject.subject_artifact_id,
            subject_digest=payload.subject.subject_digest,
            policy_version=_profile_key(
                resolved_profile(context.payload, VALIDATION_POLICY_FIELD).profile
            ),
            validation_run_id=context.run.run_id,
            target_binding=target_binding,
            supporting_artifact_ids=supporting,
            finding_bindings=(*payload.expected_findings, *payload.findings),
            requirements=requirements,
            overall_status=overall,  # type: ignore[arg-type]
        )
        return store_prepared_artifact(
            self.store,
            kind=VALIDATION_EVIDENCE_KIND,
            payload_schema_id=EVIDENCE_SET_SCHEMA_ID,
            version_tuple=evidence_tuple,
            lineage=lineage,
            payload=evidence_set.model_dump(mode="json"),
        )

    # --------------------------------------------------------------- auto-apply
    def _maybe_auto_proof(
        self,
        context: ExecutorContextLike,
        payload: PatchValidationPayloadV1,
        target_binding: PatchTargetBindingV1,
        evidence_set: PreparedArtifact,
        regression_ids: tuple[str, ...],
        requirements: tuple[EvidenceRequirement, ...],
        dimensions: tuple[_DimensionArtifact, ...],
    ) -> AutoApplyProofV1 | None:
        binding = resolved_profile(context.payload, VALIDATION_POLICY_FIELD)
        candidates = tuple(
            AutoApplyEvidenceCandidate(
                requirement=requirements[index],
                artifact_id=content_addressed_artifact_id(dimension.artifact),
                payload_hash=dimension.artifact.payload_hash,
                direct_parent_artifact_ids=dimension.artifact.lineage,
                oracle_coverage=tuple(sorted(set(dimension.oracle_coverage))),
                oracle_attestations=dimension.oracle_attestations,
                outcome_attestations=dimension.outcome_attestations,
            )
            for index, dimension in enumerate(dimensions)
        )
        request = AutoApplyEvaluationRequest(
            run=context.run,
            validation_profile=binding.profile,
            validation_profile_payload_hash=binding.profile_payload_hash,
            subject_artifact_id=payload.subject.subject_artifact_id,
            subject_digest=payload.subject.subject_digest,
            target_binding=target_binding,
            validation_evidence_artifact_id=content_addressed_artifact_id(evidence_set),
            regression_evidence_artifact_ids=regression_ids,
            requirements=requirements,
            evidence_candidates=candidates,
        )
        return self.auto_apply_evaluator.evaluate(request)

    def _seal_auto_proof(
        self,
        proof: AutoApplyProofV1,
        lineage: tuple[str, ...],
        evidence_tuple: VersionTuple,
    ) -> PreparedArtifact:
        return store_prepared_artifact(
            self.store,
            kind=VALIDATION_EVIDENCE_KIND,
            payload_schema_id=AUTO_APPLY_PROOF_SCHEMA_ID,
            version_tuple=evidence_tuple,
            lineage=lineage,
            payload=proof.model_dump(mode="json"),
        )

    # ------------------------------------------------------------------- inputs
    def _evidence_tuple(
        self,
        context: ExecutorContextLike,
        preview: Snapshot,
    ) -> VersionTuple:
        frozen = context.payload.version_tuple
        if frozen.ir_snapshot_id != preview.snapshot_id:
            raise IntegrityViolation(
                "patch validation preview bytes differ from the frozen Run target",
                frozen_snapshot_id=frozen.ir_snapshot_id,
                loaded_snapshot_id=preview.snapshot_id,
            )
        if frozen.seed != context.payload.seed:
            raise IntegrityViolation("patch validation root seed differs from its VersionTuple")
        return prepared_version_tuple(
            context,
            tool_version=EVIDENCE_TOOL_VERSION,
            projected_fields=(
                "doc_version",
                "ir_snapshot_id",
                "constraint_snapshot_id",
                "env_contract_version",
                "seed",
            ),
        )

    def _constraints(
        self,
        context: ExecutorContextLike,
        payload: PatchValidationPayloadV1,
    ) -> tuple[Constraint, ...]:
        semantic_id = context.payload.version_tuple.constraint_snapshot_id
        artifact_id = payload.constraint_snapshot_artifact_id
        if artifact_id is None:
            if semantic_id is not None:
                raise IntegrityViolation(
                    "patch validation Run has an unbound constraint snapshot identity"
                )
            return ()
        if semantic_id is None:
            raise IntegrityViolation(
                "patch validation constraint Artifact lacks a frozen semantic identity"
            )
        constraints = tuple(self.constraint_loader(self.blobs, artifact_id))
        ids = tuple(item.id for item in constraints)
        if len(ids) != len(set(ids)):
            raise IntegrityViolation("constraint snapshot repeats a constraint id")
        return tuple(sorted(constraints, key=lambda item: item.id))

    def _reverify_supporting(self, payload: PatchValidationPayloadV1) -> None:
        consumed_later = {
            *payload.review_artifact_ids,
            *payload.playtest_trace_artifact_ids,
            *(binding.evidence_artifact_id for binding in payload.expected_findings),
        }
        for artifact_id in dict.fromkeys(
            (
                *payload.candidate_config_export_artifact_ids,
                *(binding.evidence_artifact_id for binding in payload.findings),
            )
        ):
            if artifact_id not in consumed_later:
                require_exists(self.blobs, artifact_id)

    def _target_binding(
        self, payload: PatchValidationPayloadV1, preview: Snapshot
    ) -> PatchTargetBindingV1:
        return PatchTargetBindingV1(
            target_artifact_id=payload.preview_snapshot_artifact_id,
            target_snapshot_id=preview.snapshot_id,
            target_digest=digest_of(self.blobs, payload.preview_snapshot_artifact_id),
            ref_name=payload.target.ref_name,
            expected_ref=payload.target.expected_ref,
        )

    def _artifact_lineage(self, payload: PatchValidationPayloadV1) -> tuple[str, ...]:
        # subject(patch) + target(preview ir_snapshot) + candidate_config* +
        # supporting*(review/playtest/finding-evidence). The regression prepared
        # sibling on the EvidenceSet/proof is injected by semantic publication; the
        # regression suite inputs are attached only to the primary EvidenceSet by
        # its dedicated lineage rule; they do not fan out onto every dimension.
        return (
            payload.subject.subject_artifact_id,
            payload.preview_snapshot_artifact_id,
            *(
                (payload.constraint_snapshot_artifact_id,)
                if payload.constraint_snapshot_artifact_id
                else ()
            ),
            *payload.candidate_config_export_artifact_ids,
            *payload.review_artifact_ids,
            *payload.playtest_trace_artifact_ids,
            *(binding.evidence_artifact_id for binding in payload.expected_findings),
            *(binding.evidence_artifact_id for binding in payload.findings),
        )


def _auto_apply_evidence_context(
    run: RunRecord,
    payload: PatchValidationPayloadV1,
    target_binding: PatchTargetBindingV1,
    lineage: tuple[str, ...],
    *,
    affected_domain_scope: DomainScope,
) -> AutoApplyEvidenceContextV1 | None:
    """Seal the Run-authorized exact context into every deterministic dimension.

    A historical/domain-independent Run has no scope and therefore cannot yield
    qualified auto-apply evidence.  The configured evaluator treats that as a
    fail-closed authority error; null-policy profiles remain byte-compatible.
    """

    scope = affected_domain_scope
    if run.payload.params != payload or run.kind != RunKindRef(kind="patch.validate", version=1):
        raise IntegrityViolation("auto-apply evidence context differs from its exact Run")
    return AutoApplyEvidenceContextV1(
        subject_artifact_id=payload.subject.subject_artifact_id,
        subject_digest=payload.subject.subject_digest,
        target_binding=target_binding,
        evaluated_domain_scope=scope,
        direct_parent_artifact_ids=tuple(sorted(set(lineage))),
    )


def _auto_apply_payload_fields(
    context: AutoApplyEvidenceContextV1 | None,
    *,
    qualification: AutoApplyQualificationPlan | None,
    lineage: tuple[str, ...],
    requirement_id: str,
    status: DimensionStatus,
    tool_version: str,
    engine_kind: str | None = None,
    engine_id: str | None = None,
    engine_version: str | None = None,
    executed_engine_ids: tuple[str, ...] = (),
    deterministic_outcome: bool = False,
) -> dict[str, object]:
    if context is None or qualification is None:
        return {}
    exact = replace(
        context,
        direct_parent_artifact_ids=tuple(sorted(set(lineage))),
    )
    actual_engine_ids = tuple(
        sorted(set(executed_engine_ids or ((engine_id,) if engine_id is not None else ())))
    )
    oracle_attestations: list[AutoApplyOracleAttestationV1] = []
    actual_scope = dict(qualification.evaluated_scopes_by_requirement).get(requirement_id)
    eligible_definitions = tuple(
        definition
        for definition in qualification.deterministic_oracles
        if _native_executor_id(definition.engine_kind) in actual_engine_ids
        and definition.tool_version == tool_version
        and definition.predicate_schema_id == "gameforge-dimension-status@1"
    )
    if actual_scope == exact.evaluated_domain_scope and engine_version is not None:
        # One evidence Artifact may never fan out across multiple oracle refs.
        # Ambiguous executor-to-definition mappings simply cannot qualify.
        if len(eligible_definitions) == 1:
            definition = eligible_definitions[0]
            native_engine_id = _native_executor_id(definition.engine_kind)
            oracle_attestations.append(
                AutoApplyOracleAttestationV1(
                    oracle={
                        "oracle_id": definition.oracle_id,
                        "oracle_version": definition.oracle_version,
                        "oracle_digest": definition.oracle_digest,
                    },
                    engine_kind=definition.engine_kind,
                    engine_id=native_engine_id,
                    engine_version=engine_version,
                    tool_version=tool_version,
                    predicate_schema_id=definition.predicate_schema_id,
                    predicate={
                        "kind": "dimension_status",
                        "requirement_id": requirement_id,
                        "engine_id": native_engine_id,
                        "engine_version": engine_version,
                        "status": status,
                    },
                    evaluated_domain_scope=exact.evaluated_domain_scope,
                    verdict=status,
                    direct_parent_artifact_ids=exact.direct_parent_artifact_ids,
                )
            )
    rules_by_requirement = dict(qualification.outcome_rules_by_requirement)
    outcome_attestations = tuple(
        AutoApplyOutcomeAttestationV1(
            rule=rule,
            requirement_id=requirement_id,
            evaluated_domain_scope=exact.evaluated_domain_scope,
            verdict=status,
            direct_parent_artifact_ids=exact.direct_parent_artifact_ids,
        )
        for rule in (
            rules_by_requirement.get(requirement_id, ())
            if deterministic_outcome and actual_scope == exact.evaluated_domain_scope
            else ()
        )
    )
    return {
        "auto_apply_context": exact.model_dump(mode="json"),
        "oracle_attestations": [item.model_dump(mode="json") for item in oracle_attestations],
        "outcome_attestations": [item.model_dump(mode="json") for item in outcome_attestations],
    }


def _native_executor_id(engine_kind: str) -> str:
    return {
        "graph": "graph",
        "asp": "asp",
        "smt": "smt",
        "simulation": "economy_sim",
        "playtest_completion": "playtest_completion",
    }[engine_kind]


def _dimension_attestations(
    payload: Mapping[str, object],
) -> dict[
    str,
    tuple[AutoApplyOracleAttestationV1, ...] | tuple[AutoApplyOutcomeAttestationV1, ...],
]:
    return {
        "oracle_attestations": tuple(
            AutoApplyOracleAttestationV1.model_validate(value)
            for value in payload.get("oracle_attestations", ())  # type: ignore[arg-type]
        ),
        "outcome_attestations": tuple(
            AutoApplyOutcomeAttestationV1.model_validate(value)
            for value in payload.get("outcome_attestations", ())  # type: ignore[arg-type]
        ),
    }


def _profile_key(profile: ProfileRefV1) -> str:
    return f"{profile.profile_id}@{profile.version}"


def _finding_binding_key(
    binding: FindingEvidenceBindingV1,
) -> _FindingBindingKey:
    return (
        binding.evidence_artifact_id,
        binding.finding_id,
        binding.finding_revision,
        binding.finding_digest,
    )


def _finding_from_revision(revision: FindingRevisionV1) -> Finding:
    semantic_payload = revision.payload.model_dump(
        mode="python", exclude={"payload_schema_version"}
    )
    return Finding.model_validate({"id": revision.finding_id, **semantic_payload})


def _defect_key(finding: Finding, *, include_episode: bool) -> _DefectKey:
    return (
        finding.defect_class,
        tuple(sorted(finding.entities)),
        tuple(sorted(finding.relations)),
        finding.constraint_id,
        _finding_episode_id(finding) if include_episode else None,
    )


def _index_reverification_dimensions(
    dimensions: tuple[_DimensionArtifact, ...],
) -> _ReverificationIndex:
    requirement_order: list[str] = []
    by_coverage: dict[str, set[str]] = {}
    exact: dict[tuple[str, _DefectKey], list[Finding]] = {}
    broad: dict[tuple[str, _DefectKey], list[Finding]] = {}
    for dimension in dimensions:
        if dimension.result.status == "unproven":
            continue
        requirement_id = dimension.result.requirement_id
        requirement_order.append(requirement_id)
        for marker in dimension.oracle_coverage:
            by_coverage.setdefault(marker, set()).add(requirement_id)
        for finding in dimension.findings:
            exact.setdefault(
                (requirement_id, _defect_key(finding, include_episode=True)), []
            ).append(finding)
            broad.setdefault(
                (requirement_id, _defect_key(finding, include_episode=False)), []
            ).append(finding)
    return _ReverificationIndex(
        requirement_order=tuple(requirement_order),
        requirement_ids_by_coverage={
            marker: frozenset(requirements) for marker, requirements in by_coverage.items()
        },
        findings_by_exact_key={key: tuple(value) for key, value in exact.items()},
        findings_by_broad_key={key: tuple(value) for key, value in broad.items()},
    )


def _dimension_status(findings: tuple[Finding, ...]) -> DimensionStatus:
    if any(finding.status == "confirmed" for finding in findings):
        return "failed"
    if any(finding.status == "unproven" for finding in findings):
        return "unproven"
    return "passed"


def _validate_patch_checker_findings(
    findings: tuple[Finding, ...],
    *,
    snapshot_id: str,
    constraints: tuple[Constraint, ...],
    execution_bindings: tuple[CheckerExecutionBinding, ...],
) -> None:
    execution_keys = {(binding.native_id, binding.constraint_id) for binding in execution_bindings}
    llm_constraint_ids = {
        constraint.id for constraint in constraints if constraint.has_llm_predicate()
    }
    for finding in findings:
        if not isinstance(finding, Finding) or finding.snapshot_id != snapshot_id:
            raise IntegrityViolation("patch checker Finding escaped its exact target")
        producer = finding.producer_id.removeprefix("checker:")
        deterministic = (
            finding.source == "checker"
            and finding.oracle_type == "deterministic"
            and finding.status in {"confirmed", "unproven"}
            and (producer, finding.constraint_id) in execution_keys
        )
        exact_llm_placeholder = (
            finding.source == "llm"
            and finding.oracle_type == "llm-assisted"
            and finding.status == "unproven"
            and finding.producer_id == "llm-routed"
            and finding.defect_class == "llm_assisted_predicate"
            and finding.constraint_id in llm_constraint_ids
        )
        if not deterministic and not exact_llm_placeholder:
            raise IntegrityViolation(
                "patch checker Finding differs from its exact execution authority",
                finding_id=finding.id,
            )


def _validate_patch_simulation_findings(
    findings: tuple[Finding, ...],
    *,
    snapshot_id: str,
) -> None:
    for finding in findings:
        if (
            not isinstance(finding, Finding)
            or finding.snapshot_id != snapshot_id
            or finding.source != "sim"
            or finding.oracle_type != "simulation"
            or finding.producer_id != "economy_sim"
            or finding.status not in {"confirmed", "unproven"}
        ):
            raise IntegrityViolation(
                "patch simulation Finding differs from its exact execution authority",
                finding_id=getattr(finding, "id", None),
            )


def _empty_constraint_snapshot_unproven_finding(
    *,
    snapshot_id: str,
    constraint_snapshot_artifact_id: str,
) -> Finding:
    run_id = f"simulation-inputs@{snapshot_id[:19]}"
    return Finding(
        id=f"{run_id}#constraints",
        source="sim",
        producer_id="economy_sim",
        producer_run_id=run_id,
        oracle_type="simulation",
        defect_class="simulation_constraint_unproven",
        severity="major",
        snapshot_id=snapshot_id,
        evidence={
            "reason": "constraint_profile_not_executable",
            "constraint_snapshot_artifact_id": constraint_snapshot_artifact_id,
            "constraint_ids": [],
        },
        status="unproven",
        message=(
            "the built-in economy simulation profile does not interpret the bound "
            "DSL constraint snapshot; its simulation verdict is unproven"
        ),
    )


def _publishable_validation_finding(finding: Finding) -> bool:
    return (
        (finding.source == "checker" and finding.oracle_type == "deterministic")
        or (finding.source == "sim" and finding.oracle_type == "simulation")
        or (
            finding.source == "playtest"
            and finding.oracle_type == "deterministic"
            and finding.producer_id in {"agent-env-action-replay@1", "playtest.completion_oracle"}
        )
    )


def _authoritative_selected_playtest_finding(
    finding: Finding,
    *,
    snapshot_id: str,
) -> bool:
    """Admit only the deterministic Finding producer owned by PlaytestHandler.

    Agent grounding suggestions use ``playtest.grounding`` with llm-assisted
    authority and remain independent fail-closed Finding dispositions.  Regression
    replay uses ``agent-env-action-replay@1`` and is admitted at the regression
    runner boundary, never by masquerading as a selected PlaytestTrace Finding.
    """

    return (
        finding.source == "playtest"
        and finding.oracle_type == "deterministic"
        and finding.producer_id == "playtest.completion_oracle"
        and finding.snapshot_id == snapshot_id
        and finding.status in {"confirmed", "unproven"}
    )


def _review_findings(report: ReviewReport) -> tuple[Finding, ...]:
    return (
        *report.deterministic_findings,
        *report.simulation_findings,
        *report.llm_assisted_findings,
        *report.unproven_findings,
    )


def _supporting_finding_status(findings: tuple[Finding, ...]) -> DimensionStatus:
    # LLM output is suggestion-only even if a forged/legacy payload labels it
    # confirmed.  It can make validation unproven, never deterministically failed
    # or passed.
    if any(item.status == "confirmed" and item.oracle_type != "llm-assisted" for item in findings):
        return "failed"
    if any(item.status == "unproven" or item.oracle_type == "llm-assisted" for item in findings):
        return "unproven"
    return "passed"


def _finding_reason(status: DimensionStatus, *, source: str) -> str | None:
    if status == "failed":
        return f"{source}_contains_confirmed_findings"
    if status == "unproven":
        return f"{source}_contains_unproven_findings"
    return None


def _checker_companion_evidence(
    *,
    profile: ProfileRefV1 | None,
    execution_bindings: tuple[CheckerExecutionBinding, ...],
    constraint_snapshot_artifact_id: str | None,
) -> dict[str, object]:
    if profile is None:
        if execution_bindings or constraint_snapshot_artifact_id is not None:
            raise IntegrityViolation("non-checker dimension carries checker execution authority")
        return {}
    if not execution_bindings:
        raise IntegrityViolation("checker dimension lacks trusted execution bindings")
    ordered = tuple(
        sorted(
            set(execution_bindings),
            key=lambda item: (
                item.native_id,
                item.constraint_id or "",
                item.wrapper_id,
            ),
        )
    )
    return {
        "checker_profile": profile.model_dump(mode="json"),
        "checker_execution_bindings": [
            {
                "wrapper_id": item.wrapper_id,
                "native_id": item.native_id,
                "constraint_id": item.constraint_id,
            }
            for item in ordered
        ],
        "constraint_snapshot_binding_status": (
            "not_applicable" if constraint_snapshot_artifact_id is None else "bound"
        ),
        "constraint_snapshot_artifact_id": constraint_snapshot_artifact_id,
    }


def _checker_coverage_marker(
    *,
    native_id: str,
    constraint_id: str | None,
    profile: ProfileRefV1,
    constraint_snapshot_artifact_id: str | None,
) -> str:
    return "checker-binding:" + canonical_sha256(
        {
            "binding_schema_version": "checker-expected-finding-binding@1",
            "native_id": native_id,
            "execution_context": (
                {"kind": "direct"}
                if constraint_id is None
                else {"kind": "constraint", "constraint_id": constraint_id}
            ),
            "profile": profile.model_dump(mode="json"),
            "constraint_snapshot_binding_status": (
                "not_applicable" if constraint_snapshot_artifact_id is None else "bound"
            ),
            "constraint_snapshot_artifact_id": constraint_snapshot_artifact_id,
        }
    )


def _simulation_companion_evidence(
    *,
    producer_id: str,
    profile: ProfileRefV1,
    constraint_snapshot_artifact_id: str | None,
    constraints: tuple[Constraint, ...],
    n_agents: int,
    n_ticks: int,
    root_seed: int,
    run_kind: RunKindRef,
    case_id: str,
    execution_seed: int,
) -> dict[str, object]:
    constraint_ids = tuple(sorted(constraint.id for constraint in constraints))
    if len(constraint_ids) != len(set(constraint_ids)):
        raise IntegrityViolation("patch simulation constraint set repeats an id")
    bound = constraint_snapshot_artifact_id is not None
    if bound != bool(constraint_ids):
        # A declared snapshot may validly be empty; keep that binding explicit.
        if not bound:
            raise IntegrityViolation("unbound patch simulation carries constraints")
    expected_seed = derive_validation_subseed(
        root_seed=root_seed,
        run_kind=run_kind,
        profile=profile,
        case_id=case_id,
        replication_index=0,
    )
    if execution_seed != expected_seed:
        raise IntegrityViolation("patch simulation child seed differs from subseed@1")
    seed_binding = validation_child_seed_evidence(
        root_seed=root_seed,
        execution_seed=execution_seed,
        run_kind=run_kind,
        profile=profile,
        case_id=case_id,
    )
    binding: dict[str, object] = {
        "binding_schema_version": "simulation-expected-finding-binding@1",
        "producer_id": producer_id,
        "simulation_profile": profile.model_dump(mode="json"),
        "execution_mode": PATCH_SIMULATION_EXECUTION_MODE_V1,
        "seed_binding": seed_binding,
        "constraint_snapshot_binding_status": "bound" if bound else "not_applicable",
        "constraint_ids": list(constraint_ids),
        "constraint_application": (
            {
                "status": "unproven",
                "reason_code": "constraint_profile_not_executable",
            }
            if bound
            else {"status": "not_applicable"}
        ),
        "n_agents": n_agents,
        "n_ticks": n_ticks,
    }
    if constraint_snapshot_artifact_id is not None:
        binding["constraint_snapshot_artifact_id"] = constraint_snapshot_artifact_id
    return binding


def _simulation_coverage_marker(
    *,
    execution_binding: Mapping[str, object],
) -> str:
    return "simulation-binding:" + canonical_sha256(execution_binding)


def _embedded_findings(payload: Mapping[str, object]) -> tuple[Finding, ...]:
    raw = payload.get("findings")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(Finding.model_validate(value) for value in raw)


def _finding_payload_key(finding: Finding) -> str:
    semantic_fields = set(FindingPayloadV1.model_fields) - {"payload_schema_version"}
    payload = FindingPayloadV1.model_validate(
        finding.model_dump(mode="python", include=semantic_fields)
    )
    return canonical_sha256(payload.model_dump(mode="json"))


def _historical_checker_coverage(
    payload: Mapping[str, object],
    finding: Finding,
    *,
    finding_payload_counts: Mapping[str, int],
) -> frozenset[str]:
    if (
        finding.source != "checker"
        or finding.oracle_type != "deterministic"
        or payload.get("snapshot_id") != finding.snapshot_id
        or finding_payload_counts.get(_finding_payload_key(finding)) != 1
    ):
        return frozenset()
    profile = ProfileRefV1.model_validate(payload.get("checker_profile"))
    constraint_status = payload.get("constraint_snapshot_binding_status")
    constraint_snapshot_artifact_id = payload.get("constraint_snapshot_artifact_id")
    if constraint_status == "not_applicable":
        if constraint_snapshot_artifact_id is not None:
            return frozenset()
    elif constraint_status == "bound":
        if (
            not isinstance(constraint_snapshot_artifact_id, str)
            or not constraint_snapshot_artifact_id
        ):
            return frozenset()
    else:
        return frozenset()

    producer_id = finding.producer_id.removeprefix("checker:")
    execution_bindings = payload.get("checker_execution_bindings")
    if isinstance(execution_bindings, (list, tuple)):
        selected = tuple(
            value
            for value in execution_bindings
            if isinstance(value, Mapping)
            and value.get("native_id") == producer_id
            and value.get("constraint_id") == finding.constraint_id
        )
        if len(selected) != 1:
            return frozenset()
    elif finding.constraint_id is None:
        checker_ids = payload.get("checker_ids")
        if (
            not isinstance(checker_ids, (list, tuple))
            or sum(value == producer_id for value in checker_ids) != 1
        ):
            return frozenset()
    else:
        applications = payload.get("constraint_application")
        if constraint_status != "bound" or not isinstance(applications, (list, tuple)):
            return frozenset()
        selected = tuple(
            value
            for value in applications
            if isinstance(value, Mapping)
            and value.get("constraint_id") == finding.constraint_id
            and value.get("checker_id") == producer_id
            and value.get("status") == "executed"
        )
        if len(selected) != 1:
            return frozenset()
    return frozenset(
        (
            _checker_coverage_marker(
                native_id=producer_id,
                constraint_id=finding.constraint_id,
                profile=profile,
                constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
            ),
        )
    )


def _historical_simulation_coverage(
    payload: Mapping[str, object],
    finding: Finding,
) -> frozenset[str]:
    del payload, finding
    # ``simulation-result@1`` has two producer-specific execution semantics:
    # review executes one population of ``n_agents`` while standalone simulation
    # executes ``replication_count`` independent one-agent trajectories under a
    # workload profile.  Neither wire is the exact Patch single-population child
    # binding (run kind/case/subseed included), so numeric coincidences must never
    # be promoted to expected-Finding re-verification authority.
    return frozenset()


def _historical_regression_coverage(
    payload: Mapping[str, object],
    finding: Finding,
    *,
    artifact: ArtifactV2,
    finding_payload_counts: Mapping[str, int],
) -> frozenset[str]:
    if finding_payload_counts.get(_finding_payload_key(finding)) != 1:
        return frozenset()
    snapshot_id = _regression_snapshot_id(payload)
    if snapshot_id != finding.snapshot_id:
        return frozenset()
    dimension = payload.get("dimension")
    if dimension == "checker":
        return _historical_checker_coverage(
            {**dict(payload), "snapshot_id": snapshot_id},
            finding,
            finding_payload_counts=finding_payload_counts,
        )
    if dimension == "simulation":
        return _historical_regression_simulation_coverage(
            payload,
            finding,
            artifact=artifact,
        )
    if payload.get("suite_artifact_id") is not None:
        return _historical_regression_suite_coverage(payload, artifact=artifact)
    return frozenset()


def _historical_regression_simulation_coverage(
    payload: Mapping[str, object],
    finding: Finding,
    *,
    artifact: ArtifactV2,
) -> frozenset[str]:
    """Decode the exact simulation companion added to regression-evidence@1."""

    if finding.source != "sim" or finding.oracle_type != "simulation":
        return frozenset()
    execution_binding = payload.get("simulation_execution_binding")
    if not isinstance(execution_binding, Mapping):
        return frozenset()
    seed_binding = execution_binding.get("seed_binding")
    if not isinstance(seed_binding, Mapping):
        return frozenset()
    if (
        execution_binding.get("binding_schema_version") != "simulation-expected-finding-binding@1"
        or execution_binding.get("producer_id") != finding.producer_id
        or execution_binding.get("execution_mode") != PATCH_SIMULATION_EXECUTION_MODE_V1
        or execution_binding.get("constraint_snapshot_binding_status") != "not_applicable"
        or execution_binding.get("constraint_snapshot_artifact_id") is not None
        or execution_binding.get("constraint_ids") != []
        or execution_binding.get("constraint_application") != {"status": "not_applicable"}
        or payload.get("requirement_id") != seed_binding.get("case_id")
        or payload.get("root_seed") != seed_binding.get("root_seed")
        or payload.get("run_kind") != seed_binding.get("run_kind")
        or payload.get("profile_id") != seed_binding.get("profile_id")
        or payload.get("profile_version") != seed_binding.get("profile_version")
        or payload.get("case_id") != seed_binding.get("case_id")
        or payload.get("replication_index") != seed_binding.get("replication_index")
        or payload.get("seed") != seed_binding.get("seed")
        or payload.get("seed_derivation_version") != seed_binding.get("seed_derivation_version")
        or artifact.version_tuple.tool_version != EVIDENCE_TOOL_VERSION
        or artifact.version_tuple.ir_snapshot_id != finding.snapshot_id
        or artifact.version_tuple.constraint_snapshot_id is not None
        or artifact.version_tuple.seed != seed_binding.get("root_seed")
    ):
        return frozenset()
    return frozenset((_simulation_coverage_marker(execution_binding=execution_binding),))


def _historical_regression_suite_coverage(
    payload: Mapping[str, object],
    *,
    artifact: ArtifactV2,
) -> frozenset[str]:
    execution = payload.get("execution_coverage_binding")
    suite_artifact_id = payload.get("suite_artifact_id")
    if (
        payload.get("status") not in {"passed", "failed"}
        or payload.get("reason_code") is not None
        or not isinstance(execution, Mapping)
        or not isinstance(suite_artifact_id, str)
    ):
        return frozenset()
    profile = ProfileRefV1.model_validate(execution.get("validation_profile"))
    run_kind = RunKindRef.model_validate(execution.get("run_kind"))
    constraint_artifact_id = execution.get("constraint_snapshot_artifact_id")
    env_contract_version = execution.get("env_contract_version")
    root_seed = execution.get("root_seed")
    execution_seed = execution.get("execution_seed")
    if (
        execution.get("binding_schema_version") != "regression-suite-expected-finding-binding@1"
        or execution.get("suite_artifact_id") != suite_artifact_id
        or execution.get("case_id") != suite_artifact_id
        or execution.get("replication_index") != 0
        or execution.get("seed_derivation_version") != VALIDATION_SEED_DERIVATION_VERSION
        or not isinstance(env_contract_version, str)
        or not env_contract_version
        or artifact.version_tuple.env_contract_version != env_contract_version
        or isinstance(root_seed, bool)
        or not isinstance(root_seed, int)
        or isinstance(execution_seed, bool)
        or not isinstance(execution_seed, int)
        or payload.get("root_seed") != root_seed
        or payload.get("run_kind") != run_kind.model_dump(mode="json")
        or payload.get("profile_id") != profile.profile_id
        or payload.get("profile_version") != profile.version
        or payload.get("case_id") != suite_artifact_id
        or payload.get("replication_index") != 0
        or payload.get("seed") != execution_seed
        or payload.get("seed_derivation_version") != VALIDATION_SEED_DERIVATION_VERSION
    ):
        return frozenset()
    if constraint_artifact_id is None:
        if artifact.version_tuple.constraint_snapshot_id is not None:
            return frozenset()
    elif (
        not isinstance(constraint_artifact_id, str)
        or not constraint_artifact_id
        or constraint_artifact_id not in artifact.lineage
        or artifact.version_tuple.constraint_snapshot_id is None
    ):
        return frozenset()
    expected_binding = regression_suite_execution_coverage_binding(
        suite_artifact_id=suite_artifact_id,
        validation_profile=profile,
        constraint_snapshot_artifact_id=constraint_artifact_id,
        env_contract_version=env_contract_version,
        root_seed=root_seed,
        run_kind=run_kind,
        execution_seed=execution_seed,
    )
    if canonical_sha256(execution) != canonical_sha256(expected_binding):
        return frozenset()
    marker = regression_suite_execution_coverage_marker(expected_binding)
    return frozenset((marker,))


def _embedded_regression_findings(payload: Mapping[str, object]) -> tuple[Finding, ...]:
    embedded = _embedded_findings(payload)
    if embedded:
        return embedded
    detail = payload.get("detail")
    return _embedded_findings(detail) if isinstance(detail, Mapping) else ()


def _regression_snapshot_id(payload: Mapping[str, object]) -> str | None:
    snapshot_id = payload.get("snapshot_id")
    if isinstance(snapshot_id, str):
        return snapshot_id
    detail = payload.get("detail")
    if isinstance(detail, Mapping):
        nested = detail.get("snapshot_id")
        return nested if isinstance(nested, str) else None
    return None


def _historical_playtest_coverage(
    payload: Mapping[str, object],
    finding: Finding,
    *,
    artifact: ArtifactV2,
) -> frozenset[str]:
    if finding.source != "playtest" or finding.oracle_type != "deterministic":
        return frozenset()
    trace = PlaytestTraceV1.model_validate(payload)
    episode_id = _finding_episode_id(finding)
    scenario_id = _finding_scenario_artifact_id(finding)
    if episode_id is None or scenario_id is None:
        return frozenset()
    episode = next(
        (
            item
            for item in trace.episodes
            if item.episode_id == episode_id and item.scenario_spec_artifact_id == scenario_id
        ),
        None,
    )
    if episode is None:
        return frozenset()
    binding = _playtest_execution_binding(
        trace,
        episode,
        artifact=artifact,
        expected_ir_snapshot_id=finding.snapshot_id,
    )
    if binding is None:
        return frozenset()
    return frozenset((f"playtest:binding:{binding}",))


def _playtest_execution_binding(
    trace: PlaytestTraceV1,
    episode: PlaytestEpisodeTraceV1,
    *,
    artifact: ArtifactV2,
    expected_ir_snapshot_id: str | None = None,
    expected_constraint_snapshot_id: str | None = None,
) -> str | None:
    """Fingerprint one stable expected-Finding execution variant.

    Candidate config, TaskSuite and ScenarioSpec IDs are content-addressed and
    therefore *must* change after a real repair.  The suite-derived case ID and
    child seed change with them too.  Those candidate-local identities are
    validated against the trace Artifact lineage, but deliberately excluded from
    the cross-candidate fingerprint.  Producer/profile/oracle/root-seed/bounds and
    normalized model execution authority remain exact.
    """

    envelope = trace.execution_envelope
    version_tuple = artifact.version_tuple
    identity = artifact.meta.get("execution_identity")
    replayability = artifact.meta.get("replayability")
    required_lineage = {
        trace.config_artifact_id,
        trace.constraint_snapshot_artifact_id,
        trace.task_suite_artifact_id,
        *(item.scenario_spec_artifact_id for item in trace.episodes),
    }
    if (
        artifact.kind != "playtest_trace"
        or artifact.meta.get("payload_schema_id") != "playtest-trace@1"
        or not required_lineage.issubset(artifact.lineage)
        or version_tuple.tool_version != PLAYTEST_TOOL_VERSION
        or version_tuple.ir_snapshot_id is None
        or version_tuple.constraint_snapshot_id is None
        or version_tuple.env_contract_version != trace.env_contract_version
        or version_tuple.seed != trace.seed
        or (
            expected_ir_snapshot_id is not None
            and version_tuple.ir_snapshot_id != expected_ir_snapshot_id
        )
        or (
            expected_constraint_snapshot_id is not None
            and version_tuple.constraint_snapshot_id != expected_constraint_snapshot_id
        )
        or not isinstance(identity, ExecutionIdentityV1)
        or identity.scope != "artifact"
        or not identity.bindings
        or not isinstance(identity.agent_graph_version, str)
        or not identity.agent_graph_version
        or version_tuple.prompt_version != identity.prompt_projection.tuple_value
        or version_tuple.model_snapshot != identity.model_projection.tuple_value
        or version_tuple.agent_graph_version != identity.agent_graph_version
        or any(not binding.response_consumed for binding in identity.bindings)
        or replayability not in {"online_only", "cassette_replay"}
    ):
        return None

    logical_call_count = len(
        {(binding.attempt_no, binding.call_ordinal) for binding in identity.bindings}
    )
    if logical_call_count != envelope.actual_model_calls:
        return None

    execution_sources = tuple(sorted({binding.execution_source for binding in identity.bindings}))
    routing_decision_kinds = tuple(
        sorted({binding.routing_decision_kind for binding in identity.bindings})
    )
    has_replay_source = "cassette_replay" in execution_sources
    has_non_replay_source = any(source != "cassette_replay" for source in execution_sources)
    cassette_id = version_tuple.cassette_id
    cassette_bound = cassette_id is not None
    if has_replay_source and has_non_replay_source:
        return None
    if replayability == "online_only":
        if cassette_bound or has_replay_source:
            return None
        execution_mode = "live"
    else:
        if (
            not cassette_bound
            or not isinstance(cassette_id, str)
            or not cassette_id.startswith("sha256:")
            or len(cassette_id) != 71
            or any(character not in "0123456789abcdef" for character in cassette_id[7:])
        ):
            return None
        execution_mode = "replay" if has_replay_source else "record"
    if execution_mode != "replay" and "legacy_import" in routing_decision_kinds:
        return None

    prompt_members = tuple(sorted({binding.prompt_version for binding in identity.bindings}))
    model_members = tuple(sorted({binding.model_snapshot for binding in identity.bindings}))
    invocation_variants = tuple(
        {
            "agent_node_id": agent_node_id,
            "prompt_version": prompt_version,
            "model_snapshot": model_snapshot,
            "tool_version": tool_version,
            "routing_decision_kind": routing_kind,
            "execution_source": execution_source,
        }
        for (
            agent_node_id,
            prompt_version,
            model_snapshot,
            tool_version,
            routing_kind,
            execution_source,
        ) in sorted(
            {
                (
                    binding.agent_node_id,
                    binding.prompt_version,
                    binding.model_snapshot,
                    binding.tool_version,
                    binding.routing_decision_kind,
                    binding.execution_source,
                )
                for binding in identity.bindings
            }
        )
    )
    return canonical_sha256(
        {
            "binding_schema_version": "playtest-expected-finding-binding@2",
            "producer_tool_version": version_tuple.tool_version,
            "producer_env_contract_version": version_tuple.env_contract_version,
            "producer_constraint_snapshot_id": version_tuple.constraint_snapshot_id,
            "constraint_snapshot_artifact_id": trace.constraint_snapshot_artifact_id,
            "episode_id": episode.episode_id,
            "environment_profile": trace.environment_profile.model_dump(mode="json"),
            "planner_policy": trace.planner_policy.model_dump(mode="json"),
            "planner_profile_payload_hash": envelope.planner_profile_payload_hash,
            "interaction_mode": trace.interaction_mode,
            "planner_memory_mode": trace.planner_memory_mode,
            "root_seed": trace.seed,
            "seed_derivation": {
                "seed_derivation_version": episode.seed_binding.seed_derivation_version,
                "run_kind": episode.seed_binding.run_kind.model_dump(mode="json"),
                "profile": episode.seed_binding.profile.model_dump(mode="json"),
                "replication_index": episode.seed_binding.replication_index,
            },
            "completion_oracle": episode.completion_oracle.model_dump(mode="json"),
            "requested_max_steps_per_episode": trace.requested_max_steps_per_episode,
            "episode_step_budget": episode.step_budget,
            "episode_execution_step_limit": episode.execution_step_limit,
            "selected_episode_count": envelope.selected_episode_count,
            "total_step_limit": envelope.total_step_limit,
            "model_call_upper_bound": envelope.model_call_upper_bound,
            "total_trace_byte_upper_bound": envelope.total_trace_byte_upper_bound,
            "execution_authority": {
                "identity_schema_version": identity.identity_schema_version,
                "identity_scope": identity.scope,
                "agent_graph_version": identity.agent_graph_version,
                "prompt_projection": {
                    "mode": (
                        "not_applicable"
                        if not prompt_members
                        else "single"
                        if len(prompt_members) == 1
                        else "set"
                    ),
                    "members": list(prompt_members),
                },
                "model_projection": {
                    "mode": (
                        "not_applicable"
                        if not model_members
                        else "single"
                        if len(model_members) == 1
                        else "set"
                    ),
                    "members": list(model_members),
                },
                "invocation_variants": list(invocation_variants),
                "routing_decision_kinds": list(routing_decision_kinds),
                "execution_sources": list(execution_sources),
                "execution_mode": execution_mode,
                "replayability": replayability,
                # The cassette's content address changes with repaired candidate
                # requests/responses.  Bind only the required cassette semantics.
                "cassette_bound": cassette_bound,
            },
        }
    )


def _finding_episode_id(finding: Finding) -> str | None:
    for source in (finding.minimal_repro, finding.evidence):
        value = source.get("episode_id")
        if isinstance(value, str) and value:
            return value
    return None


def _finding_scenario_artifact_id(finding: Finding) -> str | None:
    for source in (finding.minimal_repro, finding.evidence):
        value = source.get("scenario_spec_artifact_id")
        if isinstance(value, str) and value:
            return value
    return None


__all__ = [
    "AutoApplyEvidenceCandidate",
    "AutoApplyEvaluationRequest",
    "AutoApplyEvaluator",
    "AutoApplyPreparationRequest",
    "AutoApplyQualificationPlan",
    "ExactFindingRevisionLoader",
    "ExactLinkedFindingRevision",
    "PatchValidationHandler",
    "VALIDATION_POLICY_FIELD",
]
