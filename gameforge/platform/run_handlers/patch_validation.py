"""``patch_validator@1`` — the deterministic patch-validation handler.

Re-verifies a subject patch's PREVIEW snapshot against the selected
``checker_profiles`` checkers + ``simulation_profiles`` economy simulations and
its bound regression suites, then seals ONE primary
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
``patch_validation_auto_eligible`` (the FULL ``validate_auto_apply`` guard re-check
at completion is the Task-18 deferral).

The frozen ``patch-validation`` policy admits ONLY ``validation_evidence`` +
``regression_evidence`` outputs, so EVERY re-verification dimension (checker /
simulation / regression suite) is captured as a ``resolved(patch-validation,
regression)`` regression-evidence artifact whose id the EvidenceSet requirement
binds. The handler mutates no ApprovalItem / EvidenceSet / ref / audit — it returns
only the sealed ``PreparedRunResult``; the deferred effect side (Task 18) consumes
the outcome code it picks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import (
    PatchValidationPayloadV1,
    PreparedArtifact,
    PreparedRunOutcome,
)
from gameforge.contracts.workflow import (
    AutoApplyProofV1,
    EvidenceRequirement,
    EvidenceSet,
    PatchTargetBindingV1,
)
from gameforge.spine.checkers.base import Checker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    FindingEvidence,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    resolved_profile,
    store_prepared_artifact,
)
from gameforge.platform.run_handlers.readers import (
    NavLoader,
    SnapshotLoader,
    load_nav,
    load_snapshot,
)
from gameforge.platform.run_handlers.review import (
    CheckerResolver,
    ReviewSimConfig,
    SimConfigResolver,
)
from gameforge.platform.run_handlers.simulation import EconomySimulatorPort
from gameforge.platform.run_handlers.validation_common import (
    AUTO_APPLY_PROOF_SCHEMA_ID,
    DEFAULT_REGRESSION_RUNNER,
    EVIDENCE_SET_SCHEMA_ID,
    REGRESSION_EVIDENCE_KIND,
    REGRESSION_EVIDENCE_SCHEMA_ID,
    VALIDATION_EVIDENCE_KIND,
    DimensionResult,
    DimensionStatus,
    RegressionRunner,
    RegressionRunRequest,
    content_addressed_artifact_id,
    derive_validation_subseed,
    digest_of,
    evidence_requirement,
    evidence_version_tuple,
    overall_status_of,
    require_exists,
    validation_child_execution_seed,
    validation_child_seed_evidence,
    with_validation_child_seed_evidence,
)

VALIDATION_POLICY_FIELD = "/params/validation_policy"
CHECKER_TOOL_VERSION = "checker@1"
SIMULATION_TOOL_VERSION = "economy-sim@1"
REGRESSION_TOOL_VERSION = "regression@1"
EVIDENCE_TOOL_VERSION = "patch-validation@1"

_OUTCOME_CODE = {
    "passed": "patch_validation_passed",
    "failed": "patch_validation_failed",
    "unproven": "patch_validation_unproven",
}
_AUTO_ELIGIBLE_CODE = "patch_validation_auto_eligible"


@dataclass(frozen=True, slots=True)
class AutoApplyEvaluationRequest:
    """The deterministic validation result an auto-apply evaluator judges."""

    validation_profile: ProfileRefV1
    validation_profile_payload_hash: str
    subject_artifact_id: str
    subject_digest: str
    target_binding: PatchTargetBindingV1
    validation_evidence_artifact_id: str
    regression_evidence_artifact_ids: tuple[str, ...]
    requirements: tuple[EvidenceRequirement, ...]


class AutoApplyEvaluator(Protocol):
    """Decide deterministic auto-apply eligibility for a PASSED patch validation.

    Returns a fully-built ``AutoApplyProofV1`` when (and only when) the resolved
    validation profile declares auto-apply eligibility and the required
    deterministic oracles + qualified outcomes are satisfied by the result;
    otherwise ``None`` (the run stays ``patch_validation_passed``). The concrete
    impl resolves the frozen auto-apply policy from the registry — the platform
    never hardcodes a policy — and lives in ``apps/worker``.
    """

    def evaluate(self, request: AutoApplyEvaluationRequest) -> AutoApplyProofV1 | None: ...


class _NeverAutoApply:
    """Default evaluator: no profile declares auto-apply → always ``passed``."""

    def evaluate(self, request: AutoApplyEvaluationRequest) -> AutoApplyProofV1 | None:
        return None


@dataclass(frozen=True, slots=True)
class _DimensionArtifact:
    """One dimension's verdict + its sealed regression-evidence + findings."""

    result: DimensionResult
    artifact: PreparedArtifact
    findings: tuple[Finding, ...]
    finding_id_prefix: str


@dataclass(frozen=True, slots=True)
class PatchValidationHandler:
    """A ``RunExecutor`` for ``patch_validator@1`` (deterministic, no LLM)."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    checker_resolver: CheckerResolver
    sim_config_resolver: SimConfigResolver
    auto_apply_evaluator: AutoApplyEvaluator = field(default_factory=_NeverAutoApply)
    regression_runner: RegressionRunner = DEFAULT_REGRESSION_RUNNER
    simulator: EconomySimulatorPort = field(default_factory=EconomySimulator)
    snapshot_loader: SnapshotLoader = load_snapshot
    nav_loader: NavLoader = load_nav

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, PatchValidationPayloadV1):
            raise TypeError("patch_validator@1 requires a patch-validation@1 payload")

        preview = self.snapshot_loader(self.blobs, payload.preview_snapshot_artifact_id)
        nav = self.nav_loader(self.blobs, payload.preview_snapshot_artifact_id)
        root_seed = context.payload.seed
        self._reverify_supporting(payload)

        lineage = self._artifact_lineage(payload)
        target_binding = self._target_binding(payload, preview)

        dimensions = (
            *self._checker_dimensions(payload, preview, nav, lineage, root_seed),
            *self._simulation_dimensions(payload, preview, lineage, context.run.kind, root_seed),
            *self._regression_dimensions(payload, preview, lineage, context.run.kind, root_seed),
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
            preview,
            target_binding,
            requirements,
            companion_ids,
            lineage,
            overall,
            root_seed,
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
            )
            if proof is not None:
                artifacts.append(self._seal_auto_proof(payload, preview, proof, lineage, root_seed))
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
        root_seed: int | None,
    ) -> tuple[_DimensionArtifact, ...]:
        dims: list[_DimensionArtifact] = []
        for profile in payload.checker_profiles:
            checker: Checker = self.checker_resolver(profile, [])
            findings = tuple(checker.check(preview, nav=nav))
            requirement_id = f"checker:{_profile_key(profile)}"
            dims.append(
                self._reverification_dimension(
                    payload,
                    preview,
                    lineage,
                    root_seed,
                    kind="checker",
                    tool_version=CHECKER_TOOL_VERSION,
                    requirement_id=requirement_id,
                    findings=findings,
                )
            )
        return tuple(dims)

    def _simulation_dimensions(
        self,
        payload: PatchValidationPayloadV1,
        preview: Snapshot,
        lineage: tuple[str, ...],
        run_kind: RunKindRef,
        root_seed: int | None,
    ) -> tuple[_DimensionArtifact, ...]:
        if not payload.simulation_profiles:
            return ()
        model = EconomyModel.from_snapshot(preview)
        dims: list[_DimensionArtifact] = []
        for profile in payload.simulation_profiles:
            if root_seed is None:
                raise ValueError("patch validation simulation requires the frozen root seed")
            config: ReviewSimConfig = self.sim_config_resolver(profile)
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
            findings = tuple(to_findings(result, preview.snapshot_id, model))
            dims.append(
                self._reverification_dimension(
                    payload,
                    preview,
                    lineage,
                    root_seed,
                    kind="simulation",
                    tool_version=SIMULATION_TOOL_VERSION,
                    requirement_id=requirement_id,
                    findings=findings,
                    execution_seed=execution_seed,
                    seed_run_kind=run_kind,
                    seed_profile=profile,
                )
            )
        return tuple(dims)

    def _regression_dimensions(
        self,
        payload: PatchValidationPayloadV1,
        preview: Snapshot,
        lineage: tuple[str, ...],
        run_kind: RunKindRef,
        root_seed: int | None,
    ) -> tuple[_DimensionArtifact, ...]:
        dims: list[_DimensionArtifact] = []
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
                )
            )
            status = "unproven" if outcome.status == "not_executed" else outcome.status
            reason_code = outcome.reason_code
            if status == "unproven" and reason_code is None:
                reason_code = "regression_not_executed"
            artifact = store_prepared_artifact(
                self.store,
                kind=REGRESSION_EVIDENCE_KIND,
                payload_schema_id=REGRESSION_EVIDENCE_SCHEMA_ID,
                version_tuple=evidence_version_tuple(
                    ir_snapshot_id=preview.snapshot_id,
                    constraint_snapshot_id=None,
                    # regression_evidence tool_version is producer-local (§3.3): the
                    # producing run is this patch.validate, so its version_tuple
                    # carries the RUN's producer tool, which the terminal publisher
                    # re-projects via ``producer_value``. The dimension tool
                    # (regression@1) is recorded on the EvidenceRequirement, not here.
                    tool_version=EVIDENCE_TOOL_VERSION,
                    seed=root_seed,
                ),
                lineage=lineage,
                payload=with_validation_child_seed_evidence(
                    {
                        **outcome.payload,
                        "reason_code": reason_code if status == "unproven" else None,
                    },
                    root_seed=root_seed,
                    execution_seed=execution_seed,
                    run_kind=run_kind,
                    profile=payload.validation_policy,
                    case_id=suite_id,
                ),
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
                    findings=(),
                    finding_id_prefix=f"regression:{suite_id}",
                )
            )
        return tuple(dims)

    def _reverification_dimension(
        self,
        payload: PatchValidationPayloadV1,
        preview: Snapshot,
        lineage: tuple[str, ...],
        root_seed: int | None,
        *,
        kind: str,
        tool_version: str,
        requirement_id: str,
        findings: tuple[Finding, ...],
        execution_seed: int | None = None,
        seed_run_kind: RunKindRef | None = None,
        seed_profile: ProfileRefV1 | None = None,
    ) -> _DimensionArtifact:
        status = _dimension_status(findings)
        reason_code = "checker_budget_unproven" if status == "unproven" else None
        artifact = store_prepared_artifact(
            self.store,
            kind=REGRESSION_EVIDENCE_KIND,
            payload_schema_id=REGRESSION_EVIDENCE_SCHEMA_ID,
            version_tuple=evidence_version_tuple(
                ir_snapshot_id=preview.snapshot_id,
                constraint_snapshot_id=None,
                # producer-local: the RUN's producer tool (see _regression_dimensions).
                tool_version=EVIDENCE_TOOL_VERSION,
                seed=root_seed,
            ),
            lineage=lineage,
            payload={
                "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                "requirement_id": requirement_id,
                "dimension": kind,
                "snapshot_id": preview.snapshot_id,
                "status": status,
                "findings": [finding.model_dump(mode="json") for finding in findings],
                **validation_child_seed_evidence(
                    root_seed=root_seed,
                    execution_seed=execution_seed,
                    run_kind=seed_run_kind,
                    profile=seed_profile,
                    case_id=requirement_id,
                ),
            },
            extra_meta={"requirement_id": requirement_id},
        )
        return _DimensionArtifact(
            result=DimensionResult(
                requirement_id=requirement_id,
                kind="regression",
                tool_version=tool_version,
                status=status,
                evidence_artifact_id=content_addressed_artifact_id(artifact),
                reason_code=reason_code,
            ),
            artifact=artifact,
            findings=findings,
            finding_id_prefix=requirement_id,
        )

    # --------------------------------------------------------------- evidence set
    def _seal_evidence_set(
        self,
        context: ExecutorContextLike,
        payload: PatchValidationPayloadV1,
        preview: Snapshot,
        target_binding: PatchTargetBindingV1,
        requirements: tuple[EvidenceRequirement, ...],
        companion_ids: tuple[str, ...],
        lineage: tuple[str, ...],
        overall: str,
        root_seed: int | None,
    ) -> PreparedArtifact:
        supporting = (
            *companion_ids,
            *payload.candidate_config_export_artifact_ids,
            *payload.review_artifact_ids,
            *payload.playtest_trace_artifact_ids,
            *payload.regression_suite_artifact_ids,
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
            finding_bindings=payload.findings,
            requirements=requirements,
            overall_status=overall,  # type: ignore[arg-type]
        )
        return store_prepared_artifact(
            self.store,
            kind=VALIDATION_EVIDENCE_KIND,
            payload_schema_id=EVIDENCE_SET_SCHEMA_ID,
            version_tuple=evidence_version_tuple(
                ir_snapshot_id=preview.snapshot_id,
                constraint_snapshot_id=None,
                tool_version=EVIDENCE_TOOL_VERSION,
                seed=root_seed,
            ),
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
    ) -> AutoApplyProofV1 | None:
        binding = resolved_profile(context.payload, VALIDATION_POLICY_FIELD)
        request = AutoApplyEvaluationRequest(
            validation_profile=binding.profile,
            validation_profile_payload_hash=binding.profile_payload_hash,
            subject_artifact_id=payload.subject.subject_artifact_id,
            subject_digest=payload.subject.subject_digest,
            target_binding=target_binding,
            validation_evidence_artifact_id=content_addressed_artifact_id(evidence_set),
            regression_evidence_artifact_ids=regression_ids,
            requirements=requirements,
        )
        return self.auto_apply_evaluator.evaluate(request)

    def _seal_auto_proof(
        self,
        payload: PatchValidationPayloadV1,
        preview: Snapshot,
        proof: AutoApplyProofV1,
        lineage: tuple[str, ...],
        root_seed: int | None,
    ) -> PreparedArtifact:
        return store_prepared_artifact(
            self.store,
            kind=VALIDATION_EVIDENCE_KIND,
            payload_schema_id=AUTO_APPLY_PROOF_SCHEMA_ID,
            version_tuple=evidence_version_tuple(
                ir_snapshot_id=preview.snapshot_id,
                constraint_snapshot_id=None,
                tool_version=EVIDENCE_TOOL_VERSION,
                seed=root_seed,
            ),
            lineage=lineage,
            payload=proof.model_dump(mode="json"),
        )

    # ------------------------------------------------------------------- inputs
    def _reverify_supporting(self, payload: PatchValidationPayloadV1) -> None:
        for artifact_id in (
            *payload.candidate_config_export_artifact_ids,
            *payload.review_artifact_ids,
            *payload.playtest_trace_artifact_ids,
            *(binding.evidence_artifact_id for binding in payload.findings),
        ):
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
        # sibling on the EvidenceSet/proof is the Task-18 publisher injection; the
        # regression SUITE ids are NOT a lineage role (they only feed the payload
        # supporting_artifact_ids / were re-verified above).
        return (
            payload.subject.subject_artifact_id,
            payload.preview_snapshot_artifact_id,
            *payload.candidate_config_export_artifact_ids,
            *payload.review_artifact_ids,
            *payload.playtest_trace_artifact_ids,
            *(binding.evidence_artifact_id for binding in payload.findings),
        )


def _profile_key(profile: ProfileRefV1) -> str:
    return f"{profile.profile_id}@{profile.version}"


def _dimension_status(findings: tuple[Finding, ...]) -> DimensionStatus:
    if any(finding.status == "confirmed" for finding in findings):
        return "failed"
    if any(finding.status == "unproven" for finding in findings):
        return "unproven"
    return "passed"


__all__ = [
    "AutoApplyEvaluationRequest",
    "AutoApplyEvaluator",
    "PatchValidationHandler",
    "VALIDATION_POLICY_FIELD",
]
