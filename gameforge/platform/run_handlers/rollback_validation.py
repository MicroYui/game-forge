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

from dataclasses import dataclass, field
from typing import Literal, Mapping, Protocol

from gameforge.contracts.execution_profiles import ResolvedExecutionProfileBindingV1
from gameforge.contracts.jobs import (
    PreparedArtifact,
    PreparedRunOutcome,
    RollbackValidationPayloadV1,
)
from gameforge.contracts.lineage import ArtifactKind
from gameforge.contracts.workflow import (
    EvidenceRequirement,
    EvidenceSet,
    RollbackRequestV1,
    RollbackTargetBindingV1,
)

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    PreparedArtifactStore,
    build_success_result,
    load_json_blob,
    resolved_profile,
    store_prepared_artifact,
)
from gameforge.platform.run_handlers.validation_common import (
    DEFAULT_REGRESSION_RUNNER,
    EVIDENCE_SET_SCHEMA_ID,
    REGRESSION_EVIDENCE_KIND,
    REGRESSION_EVIDENCE_SCHEMA_ID,
    VALIDATION_EVIDENCE_KIND,
    DimensionResult,
    RegressionRunner,
    RegressionRunRequest,
    content_addressed_artifact_id,
    evidence_requirement,
    evidence_version_tuple,
    overall_status_of,
    require_exists,
)

ROLLBACK_PROFILE_FIELD = "/params/rollback_profile"
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


@dataclass(frozen=True, slots=True)
class RollbackTargetInspectionV1:
    """The schema analyzer's target inspection + schema-compatibility verdict."""

    status: CheckStatus
    target_artifact_kind: ArtifactKind
    target_digest: str
    target_snapshot_id: str | None = None
    reason_code: str | None = None


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
    schema_compatibility_policy_id: str
    schema_compatibility_policy_version: int


@dataclass(frozen=True, slots=True)
class RollbackImpactRequest:
    target_artifact_id: str
    ref_name: str
    impact_profile_id: str
    impact_profile_version: int


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
    regression_runner: RegressionRunner = DEFAULT_REGRESSION_RUNNER

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, RollbackValidationPayloadV1):
            raise TypeError("rollback_validator@1 requires a rollback-validation@1 payload")

        inspector = self.subject_inspector or _DefaultSubjectInspector(self.blobs)
        request = inspector.inspect(payload.subject.subject_artifact_id)
        rollback_profile_binding = resolved_profile(context.payload, ROLLBACK_PROFILE_FIELD)
        seed = int(context.payload.seed) if context.payload.seed is not None else 0
        lineage = (payload.subject.subject_artifact_id,)

        inspection = self.schema_analyzer.analyze(
            RollbackSchemaRequest(
                target_artifact_id=payload.target_artifact_id,
                ref_name=payload.ref_name,
                schema_compatibility_policy_id=payload.schema_compatibility_policy.profile_id,
                schema_compatibility_policy_version=payload.schema_compatibility_policy.version,
            )
        )
        target_binding = self._target_binding(payload, rollback_profile_binding, inspection)

        dimensions: list[tuple[DimensionResult, PreparedArtifact]] = []
        dimensions.append(self._history_dimension(payload, lineage, seed))
        dimensions.append(self._artifact_dimension(payload, inspection, lineage, seed))
        dimensions.append(self._schema_dimension(inspection, lineage, seed))
        dimensions.append(
            self._profile_dimension(payload, request, rollback_profile_binding, lineage, seed)
        )
        dimensions.extend(self._impact_dimensions(payload, lineage, seed))
        dimensions.extend(self._regression_dimensions(payload, lineage, seed))

        requirements = tuple(evidence_requirement(result) for result, _ in dimensions)
        overall = overall_status_of(tuple(result.status for result, _ in dimensions))
        regression_artifacts = tuple(artifact for _, artifact in dimensions)

        supporting = (
            *(content_addressed_artifact_id(artifact) for artifact in regression_artifacts),
            payload.target_artifact_id,
            *payload.regression_suite_artifact_ids,
        )
        evidence_set = self._seal_evidence_set(
            context, payload, target_binding, requirements, supporting, lineage, overall, seed
        )

        artifacts = (evidence_set, *regression_artifacts)
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code=_OUTCOME_CODE[overall],
            primary_index=0,
            artifacts=artifacts,
            findings=(),
        )

    # --------------------------------------------------------------- dimensions
    def _history_dimension(
        self, payload: RollbackValidationPayloadV1, lineage: tuple[str, ...], seed: int
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
        return self._seal_dimension("history", "history", check, lineage, seed)

    def _artifact_dimension(
        self,
        payload: RollbackValidationPayloadV1,
        inspection: RollbackTargetInspectionV1,
        lineage: tuple[str, ...],
        seed: int,
    ) -> tuple[DimensionResult, PreparedArtifact]:
        try:
            require_exists(self.blobs, payload.target_artifact_id)
            check = DimensionCheckV1(
                status="passed",
                detail={
                    "target_artifact_id": payload.target_artifact_id,
                    "target_artifact_kind": inspection.target_artifact_kind,
                    "target_digest": inspection.target_digest,
                },
            )
        except Exception:  # noqa: BLE001 - an unreadable target is a definite failed dimension
            check = DimensionCheckV1(status="failed", reason_code="target_artifact_unreadable")
        return self._seal_dimension("artifact", "artifact", check, lineage, seed)

    def _schema_dimension(
        self, inspection: RollbackTargetInspectionV1, lineage: tuple[str, ...], seed: int
    ) -> tuple[DimensionResult, PreparedArtifact]:
        check = DimensionCheckV1(
            status=inspection.status,
            reason_code=inspection.reason_code if inspection.status != "passed" else None,
            detail={
                "target_artifact_kind": inspection.target_artifact_kind,
                "target_snapshot_id": inspection.target_snapshot_id,
            },
        )
        return self._seal_dimension("schema", "schema", check, lineage, seed)

    def _profile_dimension(
        self,
        payload: RollbackValidationPayloadV1,
        request: RollbackRequestV1,
        rollback_profile_binding: ResolvedExecutionProfileBindingV1,
        lineage: tuple[str, ...],
        seed: int,
    ) -> tuple[DimensionResult, PreparedArtifact]:
        mismatch = (
            request.ref_name != payload.ref_name
            or request.target_artifact_id != payload.target_artifact_id
            or request.target_history_revision != payload.target_history_revision
            or request.expected_current_ref != payload.expected_current_ref
        )
        if mismatch:
            check = DimensionCheckV1(status="failed", reason_code="subject_payload_mismatch")
        elif rollback_profile_binding.expected_profile_kind != "rollback":
            check = DimensionCheckV1(status="failed", reason_code="rollback_profile_kind_mismatch")
        else:
            check = DimensionCheckV1(
                status="passed",
                detail={
                    "rollback_profile": rollback_profile_binding.profile.model_dump(mode="json")
                },
            )
        return self._seal_dimension("profile", "profile", check, lineage, seed)

    def _impact_dimensions(
        self, payload: RollbackValidationPayloadV1, lineage: tuple[str, ...], seed: int
    ) -> tuple[tuple[DimensionResult, PreparedArtifact], ...]:
        dims: list[tuple[DimensionResult, PreparedArtifact]] = []
        for profile in payload.impact_profiles:
            check = self.impact_analyzer.analyze(
                RollbackImpactRequest(
                    target_artifact_id=payload.target_artifact_id,
                    ref_name=payload.ref_name,
                    impact_profile_id=profile.profile_id,
                    impact_profile_version=profile.version,
                )
            )
            requirement_id = f"impact:{profile.profile_id}@{profile.version}"
            dims.append(self._seal_dimension(requirement_id, "impact", check, lineage, seed))
        return tuple(dims)

    def _regression_dimensions(
        self, payload: RollbackValidationPayloadV1, lineage: tuple[str, ...], seed: int
    ) -> tuple[tuple[DimensionResult, PreparedArtifact], ...]:
        dims: list[tuple[DimensionResult, PreparedArtifact]] = []
        for suite_id in payload.regression_suite_artifact_ids:
            require_exists(self.blobs, suite_id)
            outcome = self.regression_runner.run(
                RegressionRunRequest(suite_artifact_id=suite_id, snapshot_id=None, seed=seed)
            )
            status = "unproven" if outcome.status == "not_executed" else outcome.status
            reason = outcome.reason_code
            if status == "unproven" and reason is None:
                reason = "regression_not_executed"
            check = DimensionCheckV1(
                status=status,  # type: ignore[arg-type]
                reason_code=reason if status != "passed" else None,
                detail=dict(outcome.payload),
            )
            dims.append(
                self._seal_dimension(f"regression:{suite_id}", "regression", check, lineage, seed)
            )
        return tuple(dims)

    # ----------------------------------------------------------------- sealing
    def _seal_dimension(
        self,
        requirement_id: str,
        kind: str,
        check: DimensionCheckV1,
        lineage: tuple[str, ...],
        seed: int,
    ) -> tuple[DimensionResult, PreparedArtifact]:
        artifact = store_prepared_artifact(
            self.store,
            kind=REGRESSION_EVIDENCE_KIND,
            payload_schema_id=REGRESSION_EVIDENCE_SCHEMA_ID,
            version_tuple=evidence_version_tuple(
                ir_snapshot_id=None,
                constraint_snapshot_id=None,
                tool_version=REGRESSION_TOOL_VERSION,
                seed=seed,
            ),
            lineage=lineage,
            payload={
                "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                "requirement_id": requirement_id,
                "dimension": kind,
                "status": check.status,
                "reason_code": check.reason_code,
                "detail": dict(check.detail),
            },
            extra_meta={"requirement_id": requirement_id},
        )
        result = DimensionResult(
            requirement_id=requirement_id,
            kind=kind,
            tool_version=VALIDATION_TOOL_VERSION,
            status=check.status,
            evidence_artifact_id=content_addressed_artifact_id(artifact),
            reason_code=check.reason_code if check.status == "unproven" else None,
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
        seed: int,
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
            version_tuple=evidence_version_tuple(
                ir_snapshot_id=None,
                constraint_snapshot_id=None,
                tool_version=VALIDATION_TOOL_VERSION,
                seed=seed,
            ),
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
