"""``rollback_validator@1`` — the deterministic rollback-validation handler.

Gathers the SIX rollback-validation dimensions — history / artifact / schema /
profile / impact / regression — into ONE primary
``validation_evidence[evidence-set@1]`` plus one
``regression_evidence[regression-evidence@1]`` per dimension
(``resolved(rollback-validation, regression)``; the frozen rollback policy admits
only those two output kinds). Each dimension's deterministic verdict feeds an
``EvidenceRequirement``; ``EvidenceSet.overall_status`` derives from them and IS the
outcome code: ``rollback_validation_passed`` / ``rollback_validation_failed`` /
``rollback_validation_unproven``. ALL THREE are run,succeeded BUSINESS results
(``PreparedRunResult``) — a failed/unproven rollback validation is NOT a
``RunFailure``.

The subject's ``RollbackRequestV1`` is parsed off the artifact through the injected
:class:`RollbackSubjectInspector`; the ``RollbackTargetBindingV1`` is built from the
payload plus the injected schema analyzer's target inspection (kind / snapshot /
digest). The verdict is DETERMINISTIC — no LLM (``llm_modes=NA``). A missing
execution (an unavailable analyzer / unreadable target) is ``unproven`` or
``failed``, NEVER a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Mapping, Protocol

from gameforge.contracts.execution_profiles import (
    MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import (
    PreparedArtifact,
    PreparedRunOutcome,
    RollbackValidationPayloadV1,
)
from gameforge.contracts.lineage import ArtifactKind, VersionTuple
from gameforge.contracts.workflow import (
    EvidenceRequirement,
    EvidenceSet,
    RollbackRequestV1,
    RollbackTargetBindingV1,
)
from gameforge.spine.ir.snapshot import Snapshot

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    FindingEvidence,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    load_json_blob,
    resolved_profile,
    rebind_finding_producers,
    store_prepared_artifact,
)
from gameforge.platform.run_handlers.validation_common import (
    EVIDENCE_SET_SCHEMA_ID,
    REGRESSION_EVIDENCE_KIND,
    REGRESSION_EVIDENCE_SCHEMA_ID,
    VALIDATION_EVIDENCE_KIND,
    DimensionResult,
    RegressionRunner,
    RegressionRunRequest,
    RegressionSuiteResultV1,
    content_addressed_artifact_id,
    deterministic_finding_status,
    evidence_requirement,
    evidence_version_tuple,
    overall_status_of,
    regression_evidence_version_tuple,
    require_exists,
    validation_child_execution_seed,
    validate_authoritative_regression_findings,
    with_validation_child_seed_evidence,
)
from gameforge.platform.run_handlers.readers import load_snapshot

ROLLBACK_PROFILE_FIELD = "/params/rollback_profile"
SCHEMA_PROFILE_FIELD = "/params/schema_compatibility_policy"
IMPACT_PROFILE_FIELD = "/params/impact_profiles"
VALIDATION_TOOL_VERSION = "rollback-validation@1"
REGRESSION_TOOL_VERSION = "regression@1"

_OUTCOME_CODE = {
    "passed": "rollback_validation_passed",
    "failed": "rollback_validation_failed",
    "unproven": "rollback_validation_unproven",
}

CheckStatus = Literal["passed", "failed", "unproven"]


@dataclass(frozen=True, slots=True)
class DimensionCheckV1:
    """One deterministic rollback-validation dimension verdict + its wire detail."""

    status: CheckStatus
    reason_code: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)
    profile_binding: ResolvedExecutionProfileBindingV1 | None = None
    rollback_profile_binding: ResolvedExecutionProfileBindingV1 | None = None


@dataclass(frozen=True, slots=True)
class RollbackTargetInspectionV1:
    """The schema analyzer's target inspection + schema-compatibility verdict."""

    status: CheckStatus
    target_artifact_kind: ArtifactKind
    target_digest: str
    target_snapshot_id: str | None = None
    target_version_tuple: VersionTuple | None = None
    reason_code: str | None = None
    schema_profile_binding: ResolvedExecutionProfileBindingV1 | None = None
    rollback_profile_binding: ResolvedExecutionProfileBindingV1 | None = None


@dataclass(frozen=True, slots=True)
class RollbackHistoryRequest:
    ref_name: str
    expected_current_ref_artifact_id: str
    expected_current_ref_revision: int
    target_artifact_id: str
    target_history_revision: int


@dataclass(frozen=True, slots=True)
class RollbackSchemaRequest:
    target_artifact_id: str
    ref_name: str
    schema_profile_binding: ResolvedExecutionProfileBindingV1
    rollback_profile_binding: ResolvedExecutionProfileBindingV1

    @property
    def schema_compatibility_policy_id(self) -> str:
        """Compatibility projection for existing concrete ports."""

        return self.schema_profile_binding.profile.profile_id

    @property
    def schema_compatibility_policy_version(self) -> int:
        return self.schema_profile_binding.profile.version


@dataclass(frozen=True, slots=True)
class RollbackImpactRequest:
    current_artifact_id: str
    current_ref_revision: int
    target_artifact_id: str
    ref_name: str
    impact_profile_binding: ResolvedExecutionProfileBindingV1
    rollback_profile_binding: ResolvedExecutionProfileBindingV1

    @property
    def impact_profile_id(self) -> str:
        """Compatibility projection for existing concrete ports."""

        return self.impact_profile_binding.profile.profile_id

    @property
    def impact_profile_version(self) -> int:
        return self.impact_profile_binding.profile.version


class RollbackSubjectInspector(Protocol):
    """Parse the immutable ``RollbackRequestV1`` off the subject artifact."""

    def inspect(self, subject_artifact_id: str) -> RollbackRequestV1: ...


class RollbackHistoryVerifier(Protocol):
    """Verify the target artifact exists at the claimed history revision on the ref."""

    def verify(self, request: RollbackHistoryRequest) -> DimensionCheckV1: ...


class RollbackSchemaAnalyzer(Protocol):
    """Inspect the target artifact + decide schema compatibility for the rollback."""

    def analyze(self, request: RollbackSchemaRequest) -> RollbackTargetInspectionV1: ...


class RollbackImpactAnalyzer(Protocol):
    """Analyze the blast radius of restoring the target under one impact profile."""

    def analyze(self, request: RollbackImpactRequest) -> DimensionCheckV1: ...


@dataclass(frozen=True, slots=True)
class _UnavailableRegressionRunner:
    """Fail-closed default: an unwired regression execution never passes."""

    def run(self, request: RegressionRunRequest) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="not_executed",
            reason_code="regression_runner_unavailable",
            payload={
                "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "seed": request.seed,
                "status": "not_executed",
                "reason_code": "regression_runner_unavailable",
            },
        )


UNAVAILABLE_REGRESSION_RUNNER: RegressionRunner = _UnavailableRegressionRunner()


def load_rollback_request(blobs: ArtifactBlobReader, subject_artifact_id: str) -> RollbackRequestV1:
    """Default subject inspector: parse the subject blob into its typed request."""

    return RollbackRequestV1.model_validate(load_json_blob(blobs, subject_artifact_id))


@dataclass(frozen=True, slots=True)
class _DefaultSubjectInspector:
    blobs: ArtifactBlobReader

    def inspect(self, subject_artifact_id: str) -> RollbackRequestV1:
        return load_rollback_request(self.blobs, subject_artifact_id)


@dataclass(frozen=True, slots=True)
class RollbackValidationHandler:
    """A ``RunExecutor`` for ``rollback_validator@1`` (deterministic, no LLM)."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    history_verifier: RollbackHistoryVerifier
    schema_analyzer: RollbackSchemaAnalyzer
    impact_analyzer: RollbackImpactAnalyzer
    subject_inspector: RollbackSubjectInspector | None = None
    regression_runner: RegressionRunner = UNAVAILABLE_REGRESSION_RUNNER

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, RollbackValidationPayloadV1):
            raise TypeError("rollback_validator@1 requires a rollback-validation@1 payload")

        inspector = self.subject_inspector or _DefaultSubjectInspector(self.blobs)
        request = inspector.inspect(payload.subject.subject_artifact_id)
        rollback_profile_binding = self._exact_profile_binding(
            context,
            field_path=ROLLBACK_PROFILE_FIELD,
            profile=payload.rollback_profile,
            expected_kind="rollback",
        )
        schema_profile_binding = self._exact_profile_binding(
            context,
            field_path=SCHEMA_PROFILE_FIELD,
            profile=payload.schema_compatibility_policy,
            expected_kind="schema_compatibility",
        )
        impact_profile_bindings = tuple(
            self._exact_profile_binding(
                context,
                field_path=f"{IMPACT_PROFILE_FIELD}/{index}",
                profile=profile,
                expected_kind="impact_analysis",
            )
            for index, profile in enumerate(payload.impact_profiles)
        )
        expected_profile_paths = {
            ROLLBACK_PROFILE_FIELD,
            SCHEMA_PROFILE_FIELD,
            *(f"{IMPACT_PROFILE_FIELD}/{index}" for index in range(len(payload.impact_profiles))),
        }
        actual_profile_paths = {item.field_path for item in context.payload.resolved_profiles}
        if actual_profile_paths != expected_profile_paths:
            raise IntegrityViolation(
                "rollback validation resolved profile closure is not exact",
                expected_paths=tuple(sorted(expected_profile_paths)),
                actual_paths=tuple(sorted(actual_profile_paths)),
            )
        root_seed = context.payload.seed
        # Every rollback verdict depends on the subject request, the observed
        # current head, and the selected historical target. Keep that complete
        # closure in prepared lineage as well as in terminal typed roles.
        lineage = (
            payload.subject.subject_artifact_id,
            payload.expected_current_ref.artifact_id,
            payload.target_artifact_id,
        )

        inspection = self.schema_analyzer.analyze(
            RollbackSchemaRequest(
                target_artifact_id=payload.target_artifact_id,
                ref_name=payload.ref_name,
                schema_profile_binding=schema_profile_binding,
                rollback_profile_binding=rollback_profile_binding,
            )
        )
        inspection = self._validate_schema_execution_binding(
            inspection,
            schema_profile_binding=schema_profile_binding,
            rollback_profile_binding=rollback_profile_binding,
        )
        target_binding = self._target_binding(payload, rollback_profile_binding, inspection)
        target_tuple = inspection.target_version_tuple
        if target_tuple is None:
            raise IntegrityViolation(
                "rollback target inspection omitted its exact VersionTuple",
                target_artifact_id=payload.target_artifact_id,
            )
        evidence_tuple = evidence_version_tuple(
            doc_version=target_tuple.doc_version,
            ir_snapshot_id=target_tuple.ir_snapshot_id,
            constraint_snapshot_id=target_tuple.constraint_snapshot_id,
            env_contract_version=target_tuple.env_contract_version,
            tool_version=VALIDATION_TOOL_VERSION,
            seed=root_seed,
        )

        dimensions: list[tuple[DimensionResult, PreparedArtifact]] = []
        dimensions.append(self._history_dimension(payload, lineage, evidence_tuple))
        dimensions.append(self._artifact_dimension(payload, inspection, lineage, evidence_tuple))
        dimensions.append(
            self._schema_dimension(
                inspection,
                payload.target_artifact_id,
                payload.expected_current_ref.artifact_id,
                lineage,
                evidence_tuple,
            )
        )
        dimensions.append(
            self._profile_dimension(
                payload,
                request,
                rollback_profile_binding,
                lineage,
                evidence_tuple,
            )
        )
        dimensions.extend(
            self._impact_dimensions(
                payload,
                impact_profile_bindings,
                rollback_profile_binding,
                lineage,
                evidence_tuple,
            )
        )
        regression_dimensions = self._regression_dimensions(
            payload,
            lineage,
            context.run.kind,
            context.run.run_id,
            root_seed,
            evidence_tuple,
            rollback_profile_binding,
            inspection,
        )
        dimensions.extend((result, artifact) for result, artifact, _ in regression_dimensions)

        requirements = tuple(evidence_requirement(result) for result, _ in dimensions)
        overall = overall_status_of(tuple(result.status for result, _ in dimensions))
        regression_artifacts = tuple(artifact for _, artifact in dimensions)

        supporting = (
            *(content_addressed_artifact_id(artifact) for artifact in regression_artifacts),
            payload.expected_current_ref.artifact_id,
            payload.target_artifact_id,
            *payload.regression_suite_artifact_ids,
        )
        evidence_set = self._seal_evidence_set(
            context,
            payload,
            target_binding,
            requirements,
            supporting,
            lineage,
            overall,
            evidence_tuple,
        )

        artifacts = (evidence_set, *regression_artifacts)
        findings_by_artifact_id = {
            content_addressed_artifact_id(artifact): findings
            for _, artifact, findings in regression_dimensions
            if findings
        }
        prepared_findings = build_prepared_findings(
            tuple(
                FindingEvidence(
                    finding=finding,
                    evidence_artifact_index=index + 1,
                    finding_id=f"rollback:{result.requirement_id}:{finding.id}",
                )
                for index, (result, artifact) in enumerate(dimensions)
                for finding in findings_by_artifact_id.get(
                    content_addressed_artifact_id(artifact), ()
                )
            ),
            run_id=context.run.run_id,
        )
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code=_OUTCOME_CODE[overall],
            primary_index=0,
            artifacts=artifacts,
            findings=prepared_findings,
        )

    @staticmethod
    def _exact_profile_binding(
        context: ExecutorContextLike,
        *,
        field_path: str,
        profile: ProfileRefV1,
        expected_kind: str,
    ) -> ResolvedExecutionProfileBindingV1:
        try:
            binding = resolved_profile(context.payload, field_path)
        except ValueError as exc:
            raise IntegrityViolation(
                "rollback validation execution profile binding is missing",
                field_path=field_path,
            ) from exc
        assert binding is not None
        if (
            binding.field_path != field_path
            or binding.profile != profile
            or binding.expected_profile_kind != expected_kind
        ):
            raise IntegrityViolation(
                "rollback validation execution profile binding is not exact",
                field_path=field_path,
                expected_profile=profile.model_dump(mode="json"),
                expected_kind=expected_kind,
            )
        return binding

    @staticmethod
    def _validate_schema_execution_binding(
        inspection: RollbackTargetInspectionV1,
        *,
        schema_profile_binding: ResolvedExecutionProfileBindingV1,
        rollback_profile_binding: ResolvedExecutionProfileBindingV1,
    ) -> RollbackTargetInspectionV1:
        if inspection.schema_profile_binding is None or inspection.rollback_profile_binding is None:
            return replace(
                inspection,
                status="unproven",
                reason_code="rollback_schema_execution_unavailable",
                schema_profile_binding=schema_profile_binding,
                rollback_profile_binding=rollback_profile_binding,
            )
        if inspection.schema_profile_binding != schema_profile_binding:
            raise IntegrityViolation("schema analyzer used another execution profile")
        if inspection.rollback_profile_binding != rollback_profile_binding:
            raise IntegrityViolation("schema analyzer used another rollback profile")
        return inspection

    # --------------------------------------------------------------- dimensions
    def _history_dimension(
        self,
        payload: RollbackValidationPayloadV1,
        lineage: tuple[str, ...],
        evidence_tuple: VersionTuple,
    ) -> tuple[DimensionResult, PreparedArtifact]:
        check = self.history_verifier.verify(
            RollbackHistoryRequest(
                ref_name=payload.ref_name,
                expected_current_ref_artifact_id=payload.expected_current_ref.artifact_id,
                expected_current_ref_revision=payload.expected_current_ref.revision,
                target_artifact_id=payload.target_artifact_id,
                target_history_revision=payload.target_history_revision,
            )
        )
        return self._seal_dimension(
            "history",
            "history",
            check,
            payload.target_artifact_id,
            payload.expected_current_ref.artifact_id,
            lineage,
            evidence_tuple,
        )

    def _artifact_dimension(
        self,
        payload: RollbackValidationPayloadV1,
        inspection: RollbackTargetInspectionV1,
        lineage: tuple[str, ...],
        evidence_tuple: VersionTuple,
    ) -> tuple[DimensionResult, PreparedArtifact]:
        # The target was frozen as a Run input. A read/integrity/dependency fault is
        # therefore an execution failure (and must restore the current draft), not a
        # deterministic business verdict that can strand the item as validation_failed.
        require_exists(self.blobs, payload.target_artifact_id)
        check = DimensionCheckV1(
            status="passed",
            detail={
                "target_artifact_id": payload.target_artifact_id,
                "target_artifact_kind": inspection.target_artifact_kind,
                "target_digest": inspection.target_digest,
            },
        )
        return self._seal_dimension(
            "artifact",
            "artifact",
            check,
            payload.target_artifact_id,
            payload.expected_current_ref.artifact_id,
            lineage,
            evidence_tuple,
        )

    def _schema_dimension(
        self,
        inspection: RollbackTargetInspectionV1,
        target_artifact_id: str,
        current_artifact_id: str,
        lineage: tuple[str, ...],
        evidence_tuple: VersionTuple,
    ) -> tuple[DimensionResult, PreparedArtifact]:
        check = DimensionCheckV1(
            status=inspection.status,
            reason_code=inspection.reason_code if inspection.status != "passed" else None,
            detail={
                "target_artifact_kind": inspection.target_artifact_kind,
                "target_snapshot_id": inspection.target_snapshot_id,
                "schema_profile_binding": (
                    None
                    if inspection.schema_profile_binding is None
                    else inspection.schema_profile_binding.model_dump(mode="json")
                ),
                "rollback_profile_binding": (
                    None
                    if inspection.rollback_profile_binding is None
                    else inspection.rollback_profile_binding.model_dump(mode="json")
                ),
            },
        )
        return self._seal_dimension(
            "schema",
            "schema",
            check,
            target_artifact_id,
            current_artifact_id,
            lineage,
            evidence_tuple,
        )

    def _profile_dimension(
        self,
        payload: RollbackValidationPayloadV1,
        request: RollbackRequestV1,
        rollback_profile_binding: ResolvedExecutionProfileBindingV1,
        lineage: tuple[str, ...],
        evidence_tuple: VersionTuple,
    ) -> tuple[DimensionResult, PreparedArtifact]:
        mismatch = (
            request.ref_name != payload.ref_name
            or request.target_artifact_id != payload.target_artifact_id
            or request.target_history_revision != payload.target_history_revision
            or request.expected_current_ref != payload.expected_current_ref
            or request.rollback_profile_binding != rollback_profile_binding
            or payload.rollback_profile != rollback_profile_binding.profile
        )
        if mismatch:
            check = DimensionCheckV1(status="failed", reason_code="subject_payload_mismatch")
        elif rollback_profile_binding.expected_profile_kind != "rollback":
            check = DimensionCheckV1(status="failed", reason_code="rollback_profile_kind_mismatch")
        else:
            check = DimensionCheckV1(
                status="passed",
                detail={
                    "rollback_profile_binding": rollback_profile_binding.model_dump(mode="json")
                },
            )
        return self._seal_dimension(
            "profile",
            "profile",
            check,
            payload.target_artifact_id,
            payload.expected_current_ref.artifact_id,
            lineage,
            evidence_tuple,
        )

    def _impact_dimensions(
        self,
        payload: RollbackValidationPayloadV1,
        impact_profile_bindings: tuple[ResolvedExecutionProfileBindingV1, ...],
        rollback_profile_binding: ResolvedExecutionProfileBindingV1,
        lineage: tuple[str, ...],
        evidence_tuple: VersionTuple,
    ) -> tuple[tuple[DimensionResult, PreparedArtifact], ...]:
        dims: list[tuple[DimensionResult, PreparedArtifact]] = []
        for profile, profile_binding in zip(
            payload.impact_profiles, impact_profile_bindings, strict=True
        ):
            try:
                check = self.impact_analyzer.analyze(
                    RollbackImpactRequest(
                        current_artifact_id=payload.expected_current_ref.artifact_id,
                        current_ref_revision=payload.expected_current_ref.revision,
                        target_artifact_id=payload.target_artifact_id,
                        ref_name=payload.ref_name,
                        impact_profile_binding=profile_binding,
                        rollback_profile_binding=rollback_profile_binding,
                    )
                )
            except (DependencyUnavailable, NotImplementedError):
                check = DimensionCheckV1(
                    status="unproven",
                    reason_code="rollback_impact_execution_unavailable",
                    profile_binding=profile_binding,
                    rollback_profile_binding=rollback_profile_binding,
                )
            if check.profile_binding is None or check.rollback_profile_binding is None:
                check = replace(
                    check,
                    status="unproven",
                    reason_code="rollback_impact_execution_unavailable",
                    profile_binding=profile_binding,
                    rollback_profile_binding=rollback_profile_binding,
                )
            elif check.profile_binding != profile_binding:
                raise IntegrityViolation("impact analyzer used another execution profile")
            elif check.rollback_profile_binding != rollback_profile_binding:
                raise IntegrityViolation("impact analyzer used another rollback profile")
            detail = {
                **dict(check.detail),
                "current_artifact_id": payload.expected_current_ref.artifact_id,
                "current_ref_revision": payload.expected_current_ref.revision,
                "impact_profile_binding": profile_binding.model_dump(mode="json"),
                "rollback_profile_binding": rollback_profile_binding.model_dump(mode="json"),
            }
            check = replace(check, detail=detail)
            requirement_id = f"impact:{profile.profile_id}@{profile.version}"
            dims.append(
                self._seal_dimension(
                    requirement_id,
                    "impact",
                    check,
                    payload.target_artifact_id,
                    payload.expected_current_ref.artifact_id,
                    lineage,
                    evidence_tuple,
                )
            )
        return tuple(dims)

    def _regression_dimensions(
        self,
        payload: RollbackValidationPayloadV1,
        lineage: tuple[str, ...],
        run_kind: RunKindRef,
        run_id: str,
        root_seed: int | None,
        evidence_tuple: VersionTuple,
        rollback_profile_binding: ResolvedExecutionProfileBindingV1,
        inspection: RollbackTargetInspectionV1,
    ) -> tuple[tuple[DimensionResult, PreparedArtifact, tuple[Finding, ...]], ...]:
        if not payload.regression_suite_artifact_ids:
            return ()
        dims: list[tuple[DimensionResult, PreparedArtifact, tuple[Finding, ...]]] = []
        snapshot = self._load_regression_snapshot(inspection, payload.target_artifact_id)
        remaining_work_units = MAX_REPAIR_REGRESSION_WORK_UNITS_V1
        for suite_id in payload.regression_suite_artifact_ids:
            require_exists(self.blobs, suite_id)
            execution_seed = validation_child_execution_seed(
                root_seed=root_seed,
                run_kind=run_kind,
                profile=payload.rollback_profile,
                case_id=suite_id,
            )
            outcome = self.regression_runner.run(
                RegressionRunRequest(
                    suite_artifact_id=suite_id,
                    snapshot_id=inspection.target_snapshot_id,
                    seed=execution_seed,
                    snapshot=snapshot,
                    root_seed=root_seed,
                    run_kind=run_kind,
                    profile=payload.rollback_profile,
                    max_action_work_units=remaining_work_units,
                )
            )
            if outcome.suite_artifact_id != suite_id:
                raise IntegrityViolation("regression runner returned another suite Artifact")
            payload_snapshot_id = outcome.payload.get("snapshot_id")
            if (
                payload_snapshot_id is not None
                and payload_snapshot_id != inspection.target_snapshot_id
            ):
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
                        "rollback validation regressions exceed the aggregate work budget",
                        suite_artifact_id=suite_id,
                        remaining_work_units=remaining_work_units,
                        measured_work_units=measured_work,
                    )
                remaining_work_units -= measured_work
            raw_findings = outcome.payload.get("findings")
            if raw_findings is None:
                suite_findings: tuple[Finding, ...] = ()
            else:
                if not isinstance(raw_findings, (list, tuple)):
                    raise IntegrityViolation("regression runner returned invalid Findings")
                try:
                    suite_findings = tuple(Finding.model_validate(value) for value in raw_findings)
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation("regression runner returned invalid Findings") from exc
                if any(
                    finding.snapshot_id != inspection.target_snapshot_id
                    for finding in suite_findings
                ):
                    raise IntegrityViolation("regression runner Finding targets another snapshot")
                validate_authoritative_regression_findings(
                    suite_findings,
                    snapshot_id=inspection.target_snapshot_id,
                )
                expected_status = deterministic_finding_status(suite_findings)
                if outcome.status != expected_status:
                    raise IntegrityViolation(
                        "regression runner status contradicts exact Finding verdicts"
                    )
            if outcome.status == "failed" and not suite_findings:
                raise IntegrityViolation("failed regression runner omitted exact Findings")

            status = "unproven" if outcome.status == "not_executed" else outcome.status
            reason = outcome.reason_code
            if snapshot is None and status in {"passed", "failed"}:
                status = "unproven"
                reason = "candidate_snapshot_unavailable"
            elif status in {"passed", "failed"} and outcome.env_contract_version is None:
                status = "unproven"
                reason = "regression_environment_binding_unavailable"
            if status == "unproven" and reason is None:
                reason = "regression_not_executed"
            if status != outcome.status:
                suite_findings = ()
            suite_findings = tuple(rebind_finding_producers(suite_findings, run_id=run_id))
            outcome_payload = {
                key: value for key, value in outcome.payload.items() if key != "findings"
            }
            if suite_findings:
                outcome_payload["findings"] = [
                    finding.model_dump(mode="json") for finding in suite_findings
                ]
            check = DimensionCheckV1(
                status=status,  # type: ignore[arg-type]
                reason_code=reason if status != "passed" else None,
                detail=with_validation_child_seed_evidence(
                    {
                        **outcome_payload,
                        "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                        "suite_artifact_id": suite_id,
                        "snapshot_id": inspection.target_snapshot_id,
                        "seed": execution_seed,
                        "status": status,
                        "rollback_profile_binding": rollback_profile_binding.model_dump(
                            mode="json"
                        ),
                        "reason_code": reason if status == "unproven" else None,
                    },
                    root_seed=root_seed,
                    execution_seed=execution_seed,
                    run_kind=run_kind,
                    profile=payload.rollback_profile,
                    case_id=suite_id,
                ),
            )
            result, artifact = self._seal_dimension(
                f"regression:{suite_id}",
                "regression",
                check,
                payload.target_artifact_id,
                payload.expected_current_ref.artifact_id,
                (*lineage, suite_id),
                regression_evidence_version_tuple(evidence_tuple, outcome),
                lineage_suite_artifact_ids=(suite_id,),
            )
            dims.append(
                (
                    result,
                    artifact,
                    suite_findings,
                )
            )
        return tuple(dims)

    def _load_regression_snapshot(
        self,
        inspection: RollbackTargetInspectionV1,
        target_artifact_id: str,
    ) -> Snapshot | None:
        if inspection.target_artifact_kind != "ir_snapshot":
            return None
        try:
            snapshot = load_snapshot(self.blobs, target_artifact_id)
        except (IntegrityViolation, KeyError, TypeError, ValueError):
            return None
        if inspection.target_snapshot_id is None:
            raise IntegrityViolation("IR rollback target inspection omitted snapshot id")
        if snapshot.snapshot_id != inspection.target_snapshot_id:
            raise IntegrityViolation("rollback target snapshot bytes differ from inspection")
        return snapshot

    # ----------------------------------------------------------------- sealing
    def _seal_dimension(
        self,
        requirement_id: str,
        kind: str,
        check: DimensionCheckV1,
        target_artifact_id: str,
        current_artifact_id: str,
        lineage: tuple[str, ...],
        evidence_tuple: VersionTuple,
        *,
        lineage_suite_artifact_ids: tuple[str, ...] = (),
    ) -> tuple[DimensionResult, PreparedArtifact]:
        if check.status == "passed":
            normalized_reason = None
        else:
            normalized_reason = check.reason_code or f"rollback_{kind}_{check.status}"
        detail = dict(check.detail)
        retained_target = detail.setdefault("target_artifact_id", target_artifact_id)
        if retained_target != target_artifact_id:
            raise IntegrityViolation(
                "rollback dimension detail names another target Artifact",
                requirement_id=requirement_id,
            )
        retained_current = detail.setdefault("current_artifact_id", current_artifact_id)
        if retained_current != current_artifact_id:
            raise IntegrityViolation(
                "rollback dimension detail names another current Artifact",
                requirement_id=requirement_id,
            )
        artifact = store_prepared_artifact(
            self.store,
            kind=REGRESSION_EVIDENCE_KIND,
            payload_schema_id=REGRESSION_EVIDENCE_SCHEMA_ID,
            # Target-derived fields are exact; tool/seed are producer-local (§3.3).
            # The dimension-specific tool is recorded on the EvidenceRequirement.
            version_tuple=evidence_tuple,
            lineage=lineage,
            payload={
                "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                "requirement_id": requirement_id,
                "dimension": kind,
                "lineage_suite_artifact_ids": list(lineage_suite_artifact_ids),
                "status": check.status,
                "reason_code": normalized_reason,
                "detail": detail,
            },
            extra_meta={"requirement_id": requirement_id},
        )
        result = DimensionResult(
            requirement_id=requirement_id,
            kind=kind,
            tool_version=VALIDATION_TOOL_VERSION,
            status=check.status,
            evidence_artifact_id=content_addressed_artifact_id(artifact),
            reason_code=normalized_reason if check.status == "unproven" else None,
        )
        return result, artifact

    def _seal_evidence_set(
        self,
        context: ExecutorContextLike,
        payload: RollbackValidationPayloadV1,
        target_binding: RollbackTargetBindingV1,
        requirements: tuple[EvidenceRequirement, ...],
        supporting: tuple[str, ...],
        lineage: tuple[str, ...],
        overall: str,
        evidence_tuple: VersionTuple,
    ) -> PreparedArtifact:
        evidence_set = EvidenceSet(
            subject_artifact_id=payload.subject.subject_artifact_id,
            subject_digest=payload.subject.subject_digest,
            policy_version=f"{payload.rollback_profile.profile_id}@{payload.rollback_profile.version}",
            validation_run_id=context.run.run_id,
            target_binding=target_binding,
            supporting_artifact_ids=supporting,
            finding_bindings=(),
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

    def _target_binding(
        self,
        payload: RollbackValidationPayloadV1,
        rollback_profile_binding: ResolvedExecutionProfileBindingV1,
        inspection: RollbackTargetInspectionV1,
    ) -> RollbackTargetBindingV1:
        return RollbackTargetBindingV1(
            target_artifact_kind=inspection.target_artifact_kind,
            target_artifact_id=payload.target_artifact_id,
            target_snapshot_id=inspection.target_snapshot_id,
            target_digest=inspection.target_digest,
            ref_name=payload.ref_name,
            expected_ref=payload.expected_current_ref,
            rollback_profile_binding=rollback_profile_binding,
        )


__all__ = [
    "ROLLBACK_PROFILE_FIELD",
    "DimensionCheckV1",
    "RollbackHistoryRequest",
    "RollbackHistoryVerifier",
    "RollbackImpactAnalyzer",
    "RollbackImpactRequest",
    "RollbackSchemaAnalyzer",
    "RollbackSchemaRequest",
    "RollbackSubjectInspector",
    "RollbackTargetInspectionV1",
    "RollbackValidationHandler",
    "load_rollback_request",
]
