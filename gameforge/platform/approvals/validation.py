"""Atomic validation completion over exact Run, subject, and evidence bindings."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
)
from gameforge.contracts.jobs import (
    ConstraintValidationPayloadV1,
    PatchValidationPayloadV1,
    RollbackValidationPayloadV1,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    AuditCorrelation,
    AuditSubject,
)
from gameforge.contracts.workflow import (
    CONSTRAINT_COMPILE_REQUIREMENT_KIND,
    ApprovalItem,
    AutoApplyProofBindingV1,
    AutoApplyProofV1,
    ConstraintCompileEvidenceV1,
    ConstraintTargetBindingV1,
    EvidenceSet,
    RollbackRequestV1,
    RollbackTargetBindingV1,
    SubjectHead,
    regression_companion_evidence_ids,
)
from gameforge.platform.approvals.commands import (
    ApprovalAuditWriter,
    ApprovalCommandContext,
    ArtifactRepository,
    BindingRepository,
    IdempotencyRepository,
    PreparedObjectBinding,
    SubjectPayloadGateway,
)
from gameforge.platform.approvals.state import validate_status_transition


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]

ValidationPayload: TypeAlias = (
    PatchValidationPayloadV1 | ConstraintValidationPayloadV1 | RollbackValidationPayloadV1
)
ValidationOutcome: TypeAlias = Literal[
    "passed",
    "failed",
    "unproven",
    "execution_failed",
    "cancelled",
    "timed_out",
]
DeterministicOutcome: TypeAlias = Literal["passed", "failed", "unproven"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ValidationRunBinding(_FrozenModel):
    """Exact active-attempt and immutable validation payload expected by completion."""

    run_id: NonEmptyStr
    expected_run_revision: PositiveInt
    attempt_no: PositiveInt
    lease_id: NonEmptyStr
    fencing_token: PositiveInt
    payload: ValidationPayload
    resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...]

    @model_validator(mode="after")
    def _run_and_profile_identity(self) -> ValidationRunBinding:
        if self.payload.subject.active_validation_run_id != self.run_id:
            raise ValueError("validation payload active Run differs from completion Run")
        paths = [binding.field_path for binding in self.resolved_profiles]
        if len(paths) != len(set(paths)):
            raise ValueError("resolved validation profile field paths must be unique")
        if tuple(sorted(paths)) != tuple(paths):
            raise ValueError("resolved validation profiles must be sorted by field path")
        return self


class ResolvedValidationProfiles(_FrozenModel):
    """Historical profile resolution used to interpret immutable evidence."""

    evidence_policy_version: NonEmptyStr
    primary: ResolvedExecutionProfileBindingV1
    compiler: ResolvedExecutionProfileBindingV1 | None = None


class PreparedValidationCompletion(_FrozenModel):
    """Preverified blobs and typed payloads prepared outside the write transaction."""

    execution: ValidationRunBinding
    outcome: ValidationOutcome
    outcome_code: NonEmptyStr
    evidence_set: EvidenceSet | None = None
    evidence_set_artifact: ArtifactV2 | None = None
    constraint_compile_evidence: ConstraintCompileEvidenceV1 | None = None
    constraint_compile_artifact: ArtifactV2 | None = None
    constraint_candidate_artifact: ArtifactV2 | None = None
    auto_apply_proof: AutoApplyProofV1 | None = None
    auto_apply_proof_artifact: ArtifactV2 | None = None
    regression_artifacts: tuple[ArtifactV2, ...] = ()
    companion_artifacts: tuple[ArtifactV2, ...] = ()
    object_bindings: tuple[PreparedObjectBinding, ...] = ()

    @model_validator(mode="after")
    def _publication_shape(self) -> PreparedValidationCompletion:
        deterministic = self.outcome in {"passed", "failed", "unproven"}
        if deterministic != (
            self.evidence_set is not None and self.evidence_set_artifact is not None
        ):
            raise ValueError(
                "deterministic validation completion requires EvidenceSet and its Artifact"
            )
        if not deterministic and any(
            (
                self.constraint_compile_evidence is not None,
                self.constraint_compile_artifact is not None,
                self.constraint_candidate_artifact is not None,
                self.auto_apply_proof is not None,
                self.auto_apply_proof_artifact is not None,
                bool(self.regression_artifacts),
                bool(self.companion_artifacts),
                bool(self.object_bindings),
            )
        ):
            raise ValueError("execution terminal completion cannot publish domain evidence")

        if deterministic:
            evidence = self.evidence_set
            evidence_artifact = self.evidence_set_artifact
            assert evidence is not None and evidence_artifact is not None
            if evidence_artifact.kind != "validation_evidence":
                raise ValueError("EvidenceSet Artifact must be validation_evidence")
            if evidence.validation_run_id != self.execution.run_id:
                raise ValueError("EvidenceSet validation_run_id differs from completion Run")
            if evidence.overall_status != self.outcome:
                raise ValueError("EvidenceSet status differs from deterministic outcome")

        expected_codes = self._expected_outcome_codes()
        if self.outcome_code not in expected_codes:
            raise ValueError("validation outcome_code does not match payload/outcome shape")
        auto_eligible = self.outcome_code == "patch_validation_auto_eligible"
        if auto_eligible != (
            self.auto_apply_proof is not None and self.auto_apply_proof_artifact is not None
        ):
            raise ValueError("only auto-eligible completion requires proof payload and Artifact")
        if auto_eligible:
            if not isinstance(self.execution.payload, PatchValidationPayloadV1):
                raise ValueError("auto-eligible completion is Patch-only")
            assert self.auto_apply_proof_artifact is not None
            if self.auto_apply_proof_artifact.kind != "validation_evidence":
                raise ValueError("auto-apply proof Artifact must be validation_evidence")

        payload = self.execution.payload
        is_constraint = isinstance(payload, ConstraintValidationPayloadV1)
        has_compile_payload = self.constraint_compile_evidence is not None
        has_compile_artifact = self.constraint_compile_artifact is not None
        if is_constraint and deterministic:
            if not has_compile_payload or not has_compile_artifact:
                raise ValueError("constraint completion requires compile evidence")
            if self.constraint_compile_artifact.kind != "validation_evidence":
                raise ValueError("constraint compile Artifact must be validation_evidence")
        elif has_compile_payload or has_compile_artifact or self.constraint_candidate_artifact:
            raise ValueError("only constraint completion may publish compile/candidate artifacts")

        if self.constraint_candidate_artifact is not None:
            if self.constraint_candidate_artifact.kind != "constraint_snapshot":
                raise ValueError("constraint candidate must be a constraint_snapshot Artifact")
        if any(artifact.kind != "regression_evidence" for artifact in self.regression_artifacts):
            raise ValueError("regression outputs must be regression_evidence Artifacts")
        allowed_companion_kinds = {
            "review_report",
            "checker_run",
            "simulation_run",
            "playtest_trace",
            "validation_evidence",
            "regression_evidence",
        }
        if any(
            artifact.kind not in allowed_companion_kinds for artifact in self.companion_artifacts
        ):
            raise ValueError("validation companion Artifact kind is not supporting evidence")

        artifacts = self.artifacts
        artifact_ids = [artifact.artifact_id for artifact in artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("prepared validation Artifact IDs must be unique")
        binding_refs = {binding.object_ref for binding in self.object_bindings}
        artifact_refs = {artifact.object_ref for artifact in artifacts}
        if binding_refs != artifact_refs:
            raise ValueError("prepared bindings must cover exactly every validation Artifact")
        binding_identities = [
            (binding.object_ref.key, binding.location.store_id) for binding in self.object_bindings
        ]
        if len(binding_identities) != len(set(binding_identities)):
            raise ValueError("prepared validation ObjectBinding identities must be unique")
        return self

    def _expected_outcome_codes(self) -> frozenset[str]:
        payload = self.execution.payload
        if self.outcome in {"execution_failed", "cancelled", "timed_out"}:
            return frozenset({self.outcome})
        if isinstance(payload, PatchValidationPayloadV1):
            if self.outcome == "passed":
                return frozenset({"patch_validation_passed", "patch_validation_auto_eligible"})
            return frozenset({f"patch_validation_{self.outcome}"})
        if isinstance(payload, RollbackValidationPayloadV1):
            return frozenset({f"rollback_validation_{self.outcome}"})
        if self.outcome == "passed":
            return frozenset({"constraint_validated"})
        suffix = (
            "with_candidate"
            if self.constraint_candidate_artifact is not None
            else "without_candidate"
        )
        return frozenset({f"constraint_validation_failed_{suffix}"})

    @property
    def artifacts(self) -> tuple[ArtifactV2, ...]:
        values: list[ArtifactV2] = []
        if self.constraint_candidate_artifact is not None:
            values.append(self.constraint_candidate_artifact)
        if self.constraint_compile_artifact is not None:
            values.append(self.constraint_compile_artifact)
        values.extend(self.regression_artifacts)
        values.extend(self.companion_artifacts)
        if self.evidence_set_artifact is not None:
            values.append(self.evidence_set_artifact)
        if self.auto_apply_proof_artifact is not None:
            values.append(self.auto_apply_proof_artifact)
        return tuple(values)


class ValidationRunTerminalResult(_FrozenModel):
    outcome_code: NonEmptyStr
    failure_artifact_id: NonEmptyStr | None = None


class ValidationCompletionResult(_FrozenModel):
    result_schema_version: Literal["validation-completion-result@1"] = (
        "validation-completion-result@1"
    )
    run_id: NonEmptyStr
    disposition: Literal["completed", "subject_superseded"]
    outcome_code: NonEmptyStr
    approval_item: ApprovalItem
    published_artifact_ids: tuple[NonEmptyStr, ...]


class ValidationCompletionApprovalRepository(Protocol):
    def get(self, approval_id: str) -> ApprovalItem | None: ...

    def get_subject_head(self, subject_series_id: str) -> SubjectHead | None: ...

    def compare_and_set(
        self,
        approval_id: str,
        expected_workflow_revision: int,
        replacement: ApprovalItem,
    ) -> ApprovalItem: ...

    def compare_and_set_validation_completion(
        self,
        approval_id: str,
        expected_workflow_revision: int,
        replacement: ApprovalItem,
    ) -> ApprovalItem: ...


class ValidationProfileResolver(Protocol):
    def resolve(self, execution: ValidationRunBinding) -> ResolvedValidationProfiles: ...


class ValidationPublicationVerifier(Protocol):
    def validate_publication(
        self,
        *,
        prepared: PreparedValidationCompletion,
        item: ApprovalItem,
        retained_parent_ids: tuple[str, ...],
    ) -> None: ...


class ValidationRunTerminalGateway(Protocol):
    def publish_terminal(
        self,
        *,
        execution: ValidationRunBinding,
        outcome_code: str,
        approval_id: str,
        published_artifact_ids: tuple[str, ...],
        actor: AuditActor,
        initiated_by: AuditActor | None,
    ) -> ValidationRunTerminalResult: ...


class ValidationAutoApplyGuard(Protocol):
    def validate_completion(
        self,
        *,
        prepared: PreparedValidationCompletion,
        current_item: ApprovalItem,
        projected_item: ApprovalItem,
        profiles: ResolvedValidationProfiles,
    ) -> None: ...


@dataclass(slots=True)
class ValidationCompletionCapabilities:
    approvals: ValidationCompletionApprovalRepository | None
    artifacts: ArtifactRepository | None
    object_bindings: BindingRepository | None
    idempotency: IdempotencyRepository | None
    profiles: ValidationProfileResolver | None
    verifier: ValidationPublicationVerifier | None
    runs: ValidationRunTerminalGateway | None
    audit: ApprovalAuditWriter | None
    subjects: SubjectPayloadGateway | None = None
    auto_apply: ValidationAutoApplyGuard | None = None


class ValidationCompletionUnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...


ValidationCompletionCapabilityBinder = Callable[[Any], ValidationCompletionCapabilities]


def _required[T](value: T | None, name: str) -> T:
    if value is None:
        raise IntegrityViolation(f"{name} validation completion capability is unavailable")
    return value


def _replace_item(item: ApprovalItem, **updates: object) -> ApprovalItem:
    payload = item.model_dump(mode="python")
    payload.update(updates)
    return ApprovalItem.model_validate(payload)


# ── shared validation-completion core (M4c Task 17b) ─────────────────────────
# The single source of truth for the subject/current-binding/evidence checks and
# the ApprovalItem replacement shape used by BOTH the standalone
# ``ValidationCompletionService.complete()`` (its own-UoW synchronous path) AND the
# ``publication/effects.py`` workflow effects that run inside ``TerminalPublisher``'s
# terminal UoW (decision (a): the ApprovalItem CAS happens in the same UoW that
# published the EvidenceSet, reusing these checks — never a duplicated copy).

_PAYLOAD_SUBJECT_KIND: dict[type, str] = {
    PatchValidationPayloadV1: "patch",
    ConstraintValidationPayloadV1: "constraint_proposal",
    RollbackValidationPayloadV1: "rollback_request",
}


def payload_subject_kind(payload: ValidationPayload) -> str:
    """The ApprovalItem ``subject_kind`` a validation payload type binds."""

    return _PAYLOAD_SUBJECT_KIND[type(payload)]


def validate_immutable_subject_binding(
    item: ApprovalItem,
    subject: object,
    subject_kind: str,
) -> None:
    """The immutable subject identity every validation completion re-verifies."""

    if (
        item.approval_id != subject.approval_id
        or item.subject_kind != subject_kind
        or item.subject_artifact_id != subject.subject_artifact_id
        or item.subject_digest != subject.subject_digest
    ):
        raise IntegrityViolation("validation Run subject differs from ApprovalItem")


def validate_current_subject_binding(
    item: ApprovalItem,
    head: SubjectHead,
    subject: object,
    run_id: str,
) -> None:
    """The current head / validating / active-run precondition for completion."""

    if (
        head.current_subject_artifact_id != item.subject_artifact_id
        or head.revision != subject.subject_head_revision
        or head.revision != item.subject_revision
        or item.status != "validating"
        or item.workflow_revision != subject.expected_workflow_revision
        or item.active_validation_run_id != run_id
        or subject.active_validation_run_id != item.active_validation_run_id
    ):
        raise Conflict(
            "validation completion subject/workflow/head binding changed",
            approval_id=item.approval_id,
        )


def validate_strict_superseded_subject_binding(
    item: ApprovalItem,
    head: SubjectHead,
    subject: object,
) -> None:
    """Prove that a non-current validation item was superseded exactly once."""

    if (
        head.current_approval_id == item.approval_id
        or item.status != "superseded"
        or item.active_validation_run_id is not None
        or head.subject_series_id != item.subject_series_id
        or head.revision <= item.subject_revision
        or subject.subject_head_revision != item.subject_revision
        or item.workflow_revision != subject.expected_workflow_revision + 1
    ):
        raise IntegrityViolation(
            "non-current validation item is not a superseded revision; strict superseded proof failed",
            approval_id=item.approval_id,
        )


def validate_evidence_subject(evidence: EvidenceSet, item: ApprovalItem) -> None:
    """The EvidenceSet subject binding every completion re-verifies."""

    if (
        evidence.subject_artifact_id != item.subject_artifact_id
        or evidence.subject_digest != item.subject_digest
    ):
        raise IntegrityViolation("EvidenceSet subject binding differs")


def validate_patch_evidence_binding(
    evidence: EvidenceSet,
    item: ApprovalItem,
    payload: PatchValidationPayloadV1,
) -> None:
    """Patch EvidenceSet target/expected_ref/finding binding re-verification."""

    if evidence.target_binding != item.target_binding:
        raise IntegrityViolation("Patch EvidenceSet target differs from draft binding")
    if item.target_binding is None:
        raise IntegrityViolation("Patch validation target binding is missing")
    if (
        item.target_binding.target_artifact_id != payload.preview_snapshot_artifact_id
        or item.target_binding.ref_name != payload.target.ref_name
        or item.target_binding.expected_ref != payload.target.expected_ref
        or evidence.finding_bindings
        != tuple(
            sorted(
                (*payload.expected_findings, *payload.findings),
                key=lambda binding: (binding.finding_id, binding.finding_revision),
            )
        )
    ):
        raise IntegrityViolation("Patch validation payload differs from exact evidence")


def validate_rollback_evidence_binding(
    evidence: EvidenceSet,
    item: ApprovalItem,
    payload: RollbackValidationPayloadV1,
    *,
    profile_binding: ResolvedExecutionProfileBindingV1,
) -> None:
    """Rollback EvidenceSet target binding re-verification.

    ``profile_binding`` is the exact resolved rollback profile frozen on the Run.
    Both completion paths must compare it; terminal publication may not skip the
    check merely because it already runs inside the write UnitOfWork.
    """

    binding = item.target_binding
    if not isinstance(binding, RollbackTargetBindingV1) or evidence.target_binding != binding:
        raise IntegrityViolation("Rollback EvidenceSet target differs from draft binding")
    if (
        binding.ref_name != payload.ref_name
        or binding.expected_ref != payload.expected_current_ref
        or binding.target_artifact_id != payload.target_artifact_id
        or binding.rollback_profile_binding != profile_binding
    ):
        raise IntegrityViolation("Rollback validation payload differs from exact target")


def validate_constraint_evidence_candidate_binding(
    evidence: EvidenceSet,
    payload: ConstraintValidationPayloadV1,
) -> None:
    """Constraint EvidenceSet candidate target binding re-verification.

    The deep compile/candidate-lineage checks stay in
    ``ValidationCompletionService`` (they need the prepared candidate/compile
    Artifacts); the in-transaction effect re-verifies the published candidate
    binding against the frozen Run target (ref_name/expected_ref) — the candidate
    Artifact and its lineage were already published + re-derived by the publisher.
    """

    target = evidence.target_binding
    if target is None:
        return
    if not isinstance(target, ConstraintTargetBindingV1):
        raise IntegrityViolation("constraint candidate requires an exact target binding")
    if (
        target.ref_name != payload.target.ref_name
        or target.expected_ref != payload.target.expected_ref
    ):
        raise IntegrityViolation("constraint candidate binding differs from frozen Run target")


def build_auto_apply_proof_binding(
    *,
    proof: AutoApplyProofV1,
    proof_artifact_id: str,
    evidence_artifact_id: str,
    regression_artifact_ids: tuple[str, ...],
    item: ApprovalItem,
) -> AutoApplyProofBindingV1:
    """Project the one exact immutable proof binding used by both completion paths.

    Task 9's generic terminal publisher and the standalone validation completion
    service must not maintain subtly different proof projections.  Deep oracle /
    policy eligibility remains the injected deterministic auto-apply guard; this
    pure helper closes the subject/target/EvidenceSet/regression identities before
    either path mutates ``ApprovalItem``.
    """

    target = item.target_binding
    if target is None:
        raise IntegrityViolation("auto-apply proof lacks the Approval target binding")
    expected_regression_ids = tuple(sorted(regression_artifact_ids))
    if (
        proof.subject_artifact_id != item.subject_artifact_id
        or proof.subject_digest != item.subject_digest
        or proof.target_binding != target
        or proof.validation_evidence_artifact_id != evidence_artifact_id
        or proof.regression_evidence_artifact_ids != expected_regression_ids
    ):
        raise IntegrityViolation("auto-apply proof payload differs from completion binding")
    return AutoApplyProofBindingV1(
        proof_artifact_id=proof_artifact_id,
        policy=proof.policy,
        subject_digest=item.subject_digest,
        target_digest=target.target_digest,
        expected_ref=target.expected_ref,
        validation_evidence_artifact_id=evidence_artifact_id,
    )


def build_validation_completion_replacement(
    item: ApprovalItem,
    *,
    target_status: str,
    evidence_set_artifact_id: str,
    regression_evidence_artifact_ids: tuple[str, ...],
    target_binding: object | None,
    auto_apply_proof: AutoApplyProofBindingV1 | None,
) -> ApprovalItem:
    """Build the ``validated``/``validation_failed`` replacement ApprovalItem."""

    return _replace_item(
        item,
        status=target_status,
        workflow_revision=item.workflow_revision + 1,
        active_validation_run_id=None,
        last_validation_failure_artifact_id=None,
        evidence_set_artifact_id=evidence_set_artifact_id,
        regression_evidence_artifact_ids=regression_evidence_artifact_ids,
        target_binding=target_binding,
        auto_apply_proof=auto_apply_proof,
    )


def build_validation_revert_replacement(
    item: ApprovalItem,
    *,
    failure_artifact_id: str | None,
) -> ApprovalItem:
    """Build the ``validating -> draft`` revert replacement ApprovalItem."""

    return _replace_item(
        item,
        status="draft",
        workflow_revision=item.workflow_revision + 1,
        active_validation_run_id=None,
        last_validation_failure_artifact_id=failure_artifact_id,
    )


def regression_evidence_ids_from_set(evidence: EvidenceSet) -> tuple[str, ...]:
    """Compatibility name for the exact ``regression`` outcome-rule closure."""

    return regression_companion_evidence_ids(evidence)


class ValidationCompletionService:
    """Publish one validation terminal and its Approval transition in one UoW."""

    _IDEMPOTENCY_OPERATION = "approval.complete_validation"

    def __init__(
        self,
        *,
        unit_of_work: ValidationCompletionUnitOfWork,
        bind_capabilities: ValidationCompletionCapabilityBinder,
        audit_chain_id: str,
    ) -> None:
        if not audit_chain_id:
            raise ValueError("audit_chain_id must be non-empty")
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._audit_chain_id = audit_chain_id

    def complete(
        self,
        *,
        prepared: PreparedValidationCompletion,
        context: ApprovalCommandContext,
    ) -> ValidationCompletionResult:
        self._validate_worker_context(prepared, context)
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            idempotency = _required(capabilities.idempotency, "idempotency")
            replay = idempotency.get_result(
                scope=context.idempotency_scope,
                operation=self._IDEMPOTENCY_OPERATION,
                key=context.idempotency_key,
                request_hash=context.request_hash,
            )
            if replay is not None:
                return self._replay_result(replay, prepared=prepared)

            approvals = _required(capabilities.approvals, "approvals")
            artifacts = _required(capabilities.artifacts, "artifacts")
            runs = _required(capabilities.runs, "runs")
            audit = _required(capabilities.audit, "audit")

            subject = prepared.execution.payload.subject
            item = approvals.get(subject.approval_id)
            if item is None:
                raise Conflict(
                    "validation ApprovalItem does not exist", approval_id=subject.approval_id
                )
            self._validate_immutable_subject(item, prepared)
            head = approvals.get_subject_head(item.subject_series_id)
            if head is None:
                raise IntegrityViolation("validation subject series has no SubjectHead")

            if head.current_approval_id != item.approval_id:
                result = self._complete_superseded(
                    prepared=prepared,
                    context=context,
                    item=item,
                    head=head,
                    runs=runs,
                    audit=audit,
                )
                self._put_idempotent(idempotency, context, result=result)
                return result

            self._validate_current_binding(item, head, prepared)
            if prepared.outcome in {"execution_failed", "cancelled", "timed_out"}:
                result = self._complete_execution_terminal(
                    prepared=prepared,
                    context=context,
                    item=item,
                    approvals=approvals,
                    runs=runs,
                    audit=audit,
                )
                self._put_idempotent(idempotency, context, result=result)
                return result

            bindings = _required(capabilities.object_bindings, "object_bindings")
            profiles = _required(capabilities.profiles, "profiles")
            verifier = _required(capabilities.verifier, "verifier")
            resolution = profiles.resolve(prepared.execution)
            self._validate_profile_resolution(prepared, resolution)
            self._validate_evidence(prepared, item, resolution)
            self._validate_bound_artifacts(prepared, item, artifacts)
            self._validate_rollback_request_payload(
                prepared,
                item,
                artifacts,
                capabilities.subjects,
            )

            evidence = prepared.evidence_set
            evidence_artifact = prepared.evidence_set_artifact
            assert evidence is not None and evidence_artifact is not None
            target_status = "validated" if prepared.outcome == "passed" else "validation_failed"
            validate_status_transition(
                current=item.status,
                target=target_status,
                subject_kind=item.subject_kind,
            )
            target_binding = item.target_binding
            if item.subject_kind == "constraint_proposal":
                target_binding = evidence.target_binding
            proof_binding = self._auto_apply_proof_binding(prepared, item)
            replacement = build_validation_completion_replacement(
                item,
                target_status=target_status,
                evidence_set_artifact_id=evidence_artifact.artifact_id,
                regression_evidence_artifact_ids=tuple(
                    sorted(artifact.artifact_id for artifact in prepared.regression_artifacts)
                ),
                target_binding=target_binding,
                auto_apply_proof=proof_binding,
            )
            if prepared.outcome_code == "patch_validation_auto_eligible":
                auto_apply = _required(capabilities.auto_apply, "auto_apply")
                auto_apply.validate_completion(
                    prepared=prepared,
                    current_item=item,
                    projected_item=replacement,
                    profiles=resolution,
                )
            retained_parents = self._retained_parents(prepared, artifacts)
            verifier.validate_publication(
                prepared=prepared,
                item=item,
                retained_parent_ids=retained_parents,
            )
            self._publish_artifacts(prepared, artifacts, bindings)

            outcome_code = prepared.outcome_code
            terminal = runs.publish_terminal(
                execution=prepared.execution,
                outcome_code=outcome_code,
                approval_id=item.approval_id,
                published_artifact_ids=tuple(
                    artifact.artifact_id for artifact in prepared.artifacts
                ),
                actor=context.actor,
                initiated_by=context.initiated_by,
            )
            if terminal.outcome_code != outcome_code or terminal.failure_artifact_id is not None:
                raise IntegrityViolation("validation terminal gateway returned another outcome")

            approvals.compare_and_set_validation_completion(
                item.approval_id,
                item.workflow_revision,
                replacement,
            )
            self._audit(
                audit,
                context,
                action="approval.validation_completed",
                item=replacement,
            )
            result = ValidationCompletionResult(
                run_id=prepared.execution.run_id,
                disposition="completed",
                outcome_code=outcome_code,
                approval_item=replacement,
                published_artifact_ids=tuple(
                    artifact.artifact_id for artifact in prepared.artifacts
                ),
            )
            self._put_idempotent(idempotency, context, result=result)
            return result

    @staticmethod
    def _validate_worker_context(
        prepared: PreparedValidationCompletion,
        context: ApprovalCommandContext,
    ) -> None:
        if context.actor.principal_kind not in {"service", "system"}:
            raise IntegrityViolation("validation completion requires a service/system worker")
        if context.run_id != prepared.execution.run_id:
            raise IntegrityViolation("validation completion audit Run differs from active Run")

    @staticmethod
    def _validate_immutable_subject(
        item: ApprovalItem,
        prepared: PreparedValidationCompletion,
    ) -> None:
        payload = prepared.execution.payload
        validate_immutable_subject_binding(item, payload.subject, payload_subject_kind(payload))

    @staticmethod
    def _validate_current_binding(
        item: ApprovalItem,
        head: SubjectHead,
        prepared: PreparedValidationCompletion,
    ) -> None:
        validate_current_subject_binding(
            item, head, prepared.execution.payload.subject, prepared.execution.run_id
        )

    def _complete_superseded(
        self,
        *,
        prepared: PreparedValidationCompletion,
        context: ApprovalCommandContext,
        item: ApprovalItem,
        head: SubjectHead,
        runs: ValidationRunTerminalGateway,
        audit: ApprovalAuditWriter,
    ) -> ValidationCompletionResult:
        validate_strict_superseded_subject_binding(
            item,
            head,
            prepared.execution.payload.subject,
        )
        terminal = runs.publish_terminal(
            execution=prepared.execution,
            outcome_code="subject_superseded",
            approval_id=item.approval_id,
            published_artifact_ids=(),
            actor=context.actor,
            initiated_by=context.initiated_by,
        )
        if terminal.outcome_code != "subject_superseded" or terminal.failure_artifact_id is None:
            raise IntegrityViolation("superseded validation terminal is not a typed failure")
        self._audit(
            audit,
            context,
            action="approval.validation_subject_superseded",
            item=item,
        )
        return ValidationCompletionResult(
            run_id=prepared.execution.run_id,
            disposition="subject_superseded",
            outcome_code="subject_superseded",
            approval_item=item,
            published_artifact_ids=(),
        )

    def _complete_execution_terminal(
        self,
        *,
        prepared: PreparedValidationCompletion,
        context: ApprovalCommandContext,
        item: ApprovalItem,
        approvals: ValidationCompletionApprovalRepository,
        runs: ValidationRunTerminalGateway,
        audit: ApprovalAuditWriter,
    ) -> ValidationCompletionResult:
        outcome_code = prepared.outcome_code
        terminal = runs.publish_terminal(
            execution=prepared.execution,
            outcome_code=outcome_code,
            approval_id=item.approval_id,
            published_artifact_ids=(),
            actor=context.actor,
            initiated_by=context.initiated_by,
        )
        if terminal.outcome_code != outcome_code or terminal.failure_artifact_id is None:
            raise IntegrityViolation("execution terminal gateway did not publish RunFailure")
        reset_reason = {
            "execution_failed": "execution_failed",
            "cancelled": "cancelled",
            "timed_out": "timed_out",
        }[prepared.outcome]
        validate_status_transition(
            current=item.status,
            target="draft",
            subject_kind=item.subject_kind,
            validation_reset_reason=reset_reason,
        )
        replacement = build_validation_revert_replacement(
            item, failure_artifact_id=terminal.failure_artifact_id
        )
        approvals.compare_and_set(item.approval_id, item.workflow_revision, replacement)
        self._audit(
            audit,
            context,
            action=f"approval.validation_{outcome_code}",
            item=replacement,
        )
        return ValidationCompletionResult(
            run_id=prepared.execution.run_id,
            disposition="completed",
            outcome_code=outcome_code,
            approval_item=replacement,
            published_artifact_ids=(),
        )

    @classmethod
    def _replay_result(
        cls,
        response: Mapping[str, Any],
        *,
        prepared: PreparedValidationCompletion,
    ) -> ValidationCompletionResult:
        try:
            result = ValidationCompletionResult.model_validate(response)
        except ValidationError as exc:
            raise IntegrityViolation(
                "validation completion idempotency response is malformed"
            ) from exc

        subject = prepared.execution.payload.subject
        item = result.approval_item
        cls._validate_immutable_subject(item, prepared)
        prepared_ids = tuple(artifact.artifact_id for artifact in prepared.artifacts)
        expected_codes = {prepared.outcome_code, "subject_superseded"}
        expected_ids = () if result.outcome_code == "subject_superseded" else prepared_ids
        if (
            result.run_id != prepared.execution.run_id
            or result.outcome_code not in expected_codes
            or item.approval_id != subject.approval_id
            or item.subject_artifact_id != subject.subject_artifact_id
            or item.subject_digest != subject.subject_digest
            or item.subject_revision != subject.subject_head_revision
            or item.workflow_revision != subject.expected_workflow_revision + 1
            or item.active_validation_run_id is not None
            or result.published_artifact_ids != expected_ids
        ):
            raise IntegrityViolation(
                "validation completion idempotency response differs from the command"
            )

        if result.outcome_code == "subject_superseded":
            if result.disposition != "subject_superseded" or item.status != "superseded":
                raise IntegrityViolation(
                    "validation completion idempotency response has invalid superseded state"
                )
            return result

        expected_status = {
            "passed": "validated",
            "failed": "validation_failed",
            "unproven": "validation_failed",
            "execution_failed": "draft",
            "cancelled": "draft",
            "timed_out": "draft",
        }[prepared.outcome]
        if result.disposition != "completed" or item.status != expected_status:
            raise IntegrityViolation(
                "validation completion idempotency response has invalid terminal state"
            )

        if prepared.outcome in {"execution_failed", "cancelled", "timed_out"}:
            if (
                item.evidence_set_artifact_id is not None
                or item.regression_evidence_artifact_ids
                or item.auto_apply_proof is not None
                or item.last_validation_failure_artifact_id is None
            ):
                raise IntegrityViolation(
                    "validation execution-terminal idempotency response is inconsistent"
                )
            return result

        evidence_artifact = prepared.evidence_set_artifact
        evidence = prepared.evidence_set
        assert evidence_artifact is not None and evidence is not None
        expected_target = evidence.target_binding
        if (
            item.evidence_set_artifact_id != evidence_artifact.artifact_id
            or item.regression_evidence_artifact_ids
            != tuple(sorted(artifact.artifact_id for artifact in prepared.regression_artifacts))
            or item.target_binding != expected_target
        ):
            raise IntegrityViolation(
                "validation completion idempotency response has different evidence bindings"
            )
        expected_proof = cls._auto_apply_proof_binding(prepared, item)
        if item.auto_apply_proof != expected_proof:
            raise IntegrityViolation(
                "validation completion idempotency response has different auto-apply proof"
            )
        return result

    def _put_idempotent(
        self,
        repository: IdempotencyRepository,
        context: ApprovalCommandContext,
        *,
        result: ValidationCompletionResult,
    ) -> None:
        response = result.model_dump(mode="json")
        stored = repository.put_result(
            scope=context.idempotency_scope,
            operation=self._IDEMPOTENCY_OPERATION,
            key=context.idempotency_key,
            request_hash=context.request_hash,
            resource_kind="approval",
            resource_id=result.approval_item.approval_id,
            response=response,
        )
        if dict(stored) != response:
            raise IntegrityViolation(
                "idempotency repository stored another validation completion response"
            )

    @staticmethod
    def _validate_profile_resolution(
        prepared: PreparedValidationCompletion,
        resolution: ResolvedValidationProfiles,
    ) -> None:
        payload = prepared.execution.payload
        expected_primary: tuple[str, str, ProfileRefV1]
        if isinstance(payload, (PatchValidationPayloadV1, ConstraintValidationPayloadV1)):
            expected_primary = (
                "/params/validation_policy",
                "validation",
                payload.validation_policy,
            )
        else:
            expected_primary = ("/params/rollback_profile", "rollback", payload.rollback_profile)
        path, kind, profile = expected_primary
        primary = resolution.primary
        if (
            primary not in prepared.execution.resolved_profiles
            or primary.field_path != path
            or primary.expected_profile_kind != kind
            or primary.profile != profile
        ):
            raise IntegrityViolation("validation policy profile resolution is not exact")

        if isinstance(payload, ConstraintValidationPayloadV1):
            compiler = resolution.compiler
            if (
                compiler is None
                or compiler not in prepared.execution.resolved_profiles
                or compiler.field_path != "/params/compiler_profile"
                or compiler.expected_profile_kind != "constraint_compiler"
                or compiler.profile != payload.compiler_profile
            ):
                raise IntegrityViolation("constraint compiler profile resolution is not exact")
        elif resolution.compiler is not None:
            raise IntegrityViolation("non-constraint validation cannot resolve a compiler profile")

    @staticmethod
    def _validate_evidence(
        prepared: PreparedValidationCompletion,
        item: ApprovalItem,
        resolution: ResolvedValidationProfiles,
    ) -> None:
        evidence = prepared.evidence_set
        assert evidence is not None
        payload = prepared.execution.payload
        validate_evidence_subject(evidence, item)
        if evidence.policy_version != resolution.evidence_policy_version:
            raise IntegrityViolation("EvidenceSet subject or policy binding differs")
        regression_ids = {artifact.artifact_id for artifact in prepared.regression_artifacts}
        if not regression_ids.issubset(evidence.supporting_artifact_ids):
            raise IntegrityViolation("EvidenceSet does not bind every regression Artifact")
        referenced_evidence_ids = {
            *evidence.supporting_artifact_ids,
            *(binding.evidence_artifact_id for binding in evidence.finding_bindings),
            *(
                requirement.evidence_artifact_id
                for requirement in evidence.requirements
                if requirement.evidence_artifact_id is not None
            ),
        }
        published_support_ids = {
            *(artifact.artifact_id for artifact in prepared.regression_artifacts),
            *(artifact.artifact_id for artifact in prepared.companion_artifacts),
        }
        if prepared.constraint_compile_artifact is not None:
            published_support_ids.add(prepared.constraint_compile_artifact.artifact_id)
        if not published_support_ids.issubset(referenced_evidence_ids):
            raise IntegrityViolation("EvidenceSet omits a published supporting Artifact")

        if isinstance(payload, PatchValidationPayloadV1):
            validate_patch_evidence_binding(evidence, item, payload)
            required_support = {
                *payload.candidate_config_export_artifact_ids,
                *payload.review_artifact_ids,
                *payload.playtest_trace_artifact_ids,
                *payload.regression_suite_artifact_ids,
                *(binding.evidence_artifact_id for binding in payload.expected_findings),
                *(binding.evidence_artifact_id for binding in payload.findings),
            }
            if payload.constraint_snapshot_artifact_id is not None:
                required_support.add(payload.constraint_snapshot_artifact_id)
            if not required_support.issubset(evidence.supporting_artifact_ids):
                raise IntegrityViolation("Patch EvidenceSet omits frozen supporting evidence")
            return

        if isinstance(payload, RollbackValidationPayloadV1):
            validate_rollback_evidence_binding(
                evidence, item, payload, profile_binding=resolution.primary
            )
            return

        compile_evidence = prepared.constraint_compile_evidence
        compile_artifact = prepared.constraint_compile_artifact
        assert compile_evidence is not None and compile_artifact is not None
        candidate = prepared.constraint_candidate_artifact
        candidate_id = None if candidate is None else candidate.artifact_id
        if (
            compile_evidence.proposal_artifact_id != item.subject_artifact_id
            or compile_evidence.base_constraint_snapshot_artifact_id
            != payload.base_constraint_snapshot_artifact_id
            or compile_evidence.candidate_constraint_snapshot_artifact_id != candidate_id
            or compile_evidence.dsl_grammar_version != payload.dsl_grammar_version
            or compile_evidence.compiler_profile != payload.compiler_profile
        ):
            raise IntegrityViolation("constraint compile evidence differs from frozen Run payload")
        expected_engines = {
            (engine.engine_id, str(engine.version)) for engine in payload.differential_engines
        }
        actual_engines = {
            (stage.engine_id, stage.engine_version)
            for stage in compile_evidence.stages
            if stage.stage == "differential"
        }
        if actual_engines != expected_engines:
            raise IntegrityViolation("constraint compile differential engines differ")
        golden = next(stage for stage in compile_evidence.stages if stage.stage == "golden")
        if (payload.golden_suite_artifact_id is None) != (golden.status == "not_applicable"):
            raise IntegrityViolation(
                "constraint compile golden disposition differs from Run payload"
            )
        if compile_artifact.artifact_id not in evidence.supporting_artifact_ids:
            raise IntegrityViolation("EvidenceSet omits constraint compile evidence")
        target = evidence.target_binding
        if candidate is None and (target is not None or prepared.outcome == "passed"):
            raise IntegrityViolation("constraint completion without candidate cannot bind/pass")
        compile_requirements = tuple(
            requirement
            for requirement in evidence.requirements
            if requirement.kind == CONSTRAINT_COMPILE_REQUIREMENT_KIND
        )
        if len(compile_requirements) != 1:
            raise IntegrityViolation(
                "EvidenceSet must contain one exact constraint compile requirement"
            )
        compile_requirement = compile_requirements[0]
        if (
            compile_requirement.applicability != "required"
            or compile_requirement.status != compile_evidence.overall_status
            or compile_requirement.evidence_artifact_id != compile_artifact.artifact_id
        ):
            raise IntegrityViolation(
                "constraint compile evidence and EvidenceSet requirement differ"
            )
        if prepared.outcome == "passed" and compile_evidence.overall_status != "passed":
            raise IntegrityViolation(
                "failed or unproven constraint compile cannot validate a candidate"
            )

        if candidate is None:
            return
        if not isinstance(target, ConstraintTargetBindingV1):
            raise IntegrityViolation("constraint candidate requires exact target binding")
        if (
            target.target_artifact_id != candidate.artifact_id
            or target.target_digest != candidate.payload_hash
            or target.ref_name != payload.target.ref_name
            or target.expected_ref != payload.target.expected_ref
            or candidate.version_tuple.constraint_snapshot_id != target.target_snapshot_id
        ):
            raise IntegrityViolation("constraint candidate and exact target binding differ")
        expected_candidate_parents = tuple(
            sorted(
                value
                for value in (
                    item.subject_artifact_id,
                    payload.base_constraint_snapshot_artifact_id,
                )
                if value is not None
            )
        )
        if candidate.lineage != expected_candidate_parents:
            raise IntegrityViolation("constraint candidate direct lineage is not exact")

    @staticmethod
    def _validate_bound_artifacts(
        prepared: PreparedValidationCompletion,
        item: ApprovalItem,
        artifacts: ArtifactRepository,
    ) -> None:
        subject = artifacts.get(item.subject_artifact_id)
        expected_subject_kind = {
            "patch": "patch",
            "constraint_proposal": "constraint_proposal",
            "rollback_request": "rollback_request",
        }[item.subject_kind]
        if (
            not isinstance(subject, ArtifactV2)
            or subject.kind != expected_subject_kind
            or subject.payload_hash != item.subject_digest
        ):
            raise IntegrityViolation("validation subject Artifact binding is unavailable")
        if item.subject_kind == "constraint_proposal":
            return
        binding = item.target_binding
        if binding is None:
            raise IntegrityViolation("validation target binding is missing")
        target = artifacts.get(binding.target_artifact_id)
        if (
            not isinstance(target, ArtifactV2)
            or target.kind != binding.target_artifact_kind
            or target.payload_hash != binding.target_digest
        ):
            raise IntegrityViolation("validation target Artifact binding is unavailable")
        if binding.target_snapshot_id is not None:
            actual_snapshot_id = {
                "ir_snapshot": target.version_tuple.ir_snapshot_id,
                "constraint_snapshot": target.version_tuple.constraint_snapshot_id,
            }.get(target.kind)
            if actual_snapshot_id != binding.target_snapshot_id:
                raise IntegrityViolation(
                    "validation target Artifact VersionTuple differs from binding"
                )

    @staticmethod
    def _validate_rollback_request_payload(
        prepared: PreparedValidationCompletion,
        item: ApprovalItem,
        artifacts: ArtifactRepository,
        subjects: SubjectPayloadGateway | None,
    ) -> None:
        payload = prepared.execution.payload
        if not isinstance(payload, RollbackValidationPayloadV1):
            return
        gateway = _required(subjects, "subjects")
        subject = artifacts.get(item.subject_artifact_id)
        if not isinstance(subject, ArtifactV2):
            raise IntegrityViolation("rollback request Artifact is unavailable")
        facts = gateway.inspect_draft_subject(subject)
        request = facts.rollback_request
        if facts.subject_kind != "rollback_request" or not isinstance(request, RollbackRequestV1):
            raise IntegrityViolation("rollback subject parser omitted RollbackRequest")
        binding = item.target_binding
        if not isinstance(binding, RollbackTargetBindingV1):
            raise IntegrityViolation("rollback request lacks its exact target binding")
        if (
            request.ref_name != payload.ref_name
            or request.expected_current_ref != payload.expected_current_ref
            or request.target_artifact_id != payload.target_artifact_id
            or request.target_history_revision != payload.target_history_revision
            or request.rollback_profile_binding != binding.rollback_profile_binding
            or request.rollback_profile_binding.profile != payload.rollback_profile
            or binding.ref_name != payload.ref_name
            or binding.expected_ref != payload.expected_current_ref
            or binding.target_artifact_id != payload.target_artifact_id
        ):
            raise IntegrityViolation(
                "rollback validation payload/history revision differs from the immutable "
                "RollbackRequest"
            )

    @staticmethod
    def _auto_apply_proof_binding(
        prepared: PreparedValidationCompletion,
        item: ApprovalItem,
    ) -> AutoApplyProofBindingV1 | None:
        proof = prepared.auto_apply_proof
        proof_artifact = prepared.auto_apply_proof_artifact
        if proof is None or proof_artifact is None:
            return None
        evidence_artifact = prepared.evidence_set_artifact
        if evidence_artifact is None:
            raise IntegrityViolation("auto-apply proof lacks its EvidenceSet")
        return build_auto_apply_proof_binding(
            proof=proof,
            proof_artifact_id=proof_artifact.artifact_id,
            evidence_artifact_id=evidence_artifact.artifact_id,
            regression_artifact_ids=tuple(
                artifact.artifact_id for artifact in prepared.regression_artifacts
            ),
            item=item,
        )

    @staticmethod
    def _retained_parents(
        prepared: PreparedValidationCompletion,
        artifacts: ArtifactRepository,
    ) -> tuple[str, ...]:
        prepared_ids = {artifact.artifact_id for artifact in prepared.artifacts}
        retained: set[str] = set()
        for artifact in prepared.artifacts:
            for parent_id in artifact.lineage:
                if parent_id in prepared_ids:
                    continue
                parent = artifacts.get(parent_id)
                if not isinstance(parent, ArtifactV2):
                    raise IntegrityViolation(
                        "validation Artifact lineage parent is unavailable",
                        artifact_id=artifact.artifact_id,
                        parent_artifact_id=parent_id,
                    )
                retained.add(parent_id)
        return tuple(sorted(retained))

    @staticmethod
    def _publish_artifacts(
        prepared: PreparedValidationCompletion,
        artifacts: ArtifactRepository,
        bindings: BindingRepository,
    ) -> None:
        for binding in sorted(
            prepared.object_bindings,
            key=lambda item: (item.object_ref.key, item.location.store_id),
        ):
            published = bindings.bind_verified(
                binding.object_ref,
                binding.location,
                binding.expected_revision,
            )
            if (
                published.object_ref != binding.object_ref
                or published.location != binding.location
                or published.status != "active"
            ):
                raise IntegrityViolation(
                    "validation ObjectBinding publisher returned another binding"
                )
        for artifact in ValidationCompletionService._topological_artifacts(prepared.artifacts):
            if artifacts.put(artifact) != artifact:
                raise IntegrityViolation("validation Artifact publisher returned another Artifact")

    @staticmethod
    def _topological_artifacts(artifacts: tuple[ArtifactV2, ...]) -> tuple[ArtifactV2, ...]:
        pending = {artifact.artifact_id: artifact for artifact in artifacts}
        ordered: list[ArtifactV2] = []
        while pending:
            ready = sorted(
                (
                    artifact
                    for artifact in pending.values()
                    if not set(artifact.lineage).intersection(pending)
                ),
                key=lambda artifact: artifact.artifact_id,
            )
            if not ready:
                raise IntegrityViolation("prepared validation Artifact lineage contains a cycle")
            for artifact in ready:
                ordered.append(artifact)
                del pending[artifact.artifact_id]
        return tuple(ordered)

    def _audit(
        self,
        audit: ApprovalAuditWriter,
        context: ApprovalCommandContext,
        *,
        action: str,
        item: ApprovalItem,
    ) -> None:
        audit.append(
            chain_id=self._audit_chain_id,
            actor=context.actor,
            initiated_by=context.initiated_by,
            action=action,
            subject=AuditSubject(
                resource_kind="approval",
                resource_id=item.approval_id,
                artifact_id=item.subject_artifact_id,
            ),
            correlation=AuditCorrelation(
                request_id=context.request_id,
                run_id=context.run_id,
                trace_id=context.trace_id,
            ),
        )


__all__ = [
    "PreparedValidationCompletion",
    "ResolvedValidationProfiles",
    "ValidationCompletionCapabilities",
    "ValidationCompletionResult",
    "ValidationCompletionService",
    "ValidationOutcome",
    "ValidationAutoApplyGuard",
    "ValidationProfileResolver",
    "ValidationPublicationVerifier",
    "ValidationRunBinding",
    "ValidationRunTerminalGateway",
    "ValidationRunTerminalResult",
    "build_auto_apply_proof_binding",
    "build_validation_completion_replacement",
    "build_validation_revert_replacement",
    "payload_subject_kind",
    "regression_evidence_ids_from_set",
    "validate_constraint_evidence_candidate_binding",
    "validate_current_subject_binding",
    "validate_evidence_subject",
    "validate_immutable_subject_binding",
    "validate_patch_evidence_binding",
    "validate_rollback_evidence_binding",
    "validate_strict_superseded_subject_binding",
]
