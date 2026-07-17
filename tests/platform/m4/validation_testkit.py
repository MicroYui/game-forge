from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import Conflict
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
)
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRoutePolicyRefV1,
    DomainScope,
    Permission,
)
from gameforge.contracts.jobs import (
    ConstraintValidationPayloadV1,
    PatchValidationPayloadV1,
    RefReadBindingV1,
    RollbackValidationPayloadV1,
    SolverEngineRefV1,
    ValidationSubjectBindingV1,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    ObjectBinding,
    ObjectLocation,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalRequirement,
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyProofV1,
    AutoApplyValidationProfileBindingV1,
    ConstraintCompileEvidenceV1,
    ConstraintCompileStageV1,
    ConstraintTargetBindingV1,
    EvidenceRequirement,
    EvidenceSet,
    PatchTargetBindingV1,
    RollbackRequestV1,
    RollbackTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.approvals.commands import (
    ApprovalCommandContext,
    DraftSubjectFacts,
    PreparedObjectBinding,
)
from gameforge.platform.approvals.validation import (
    PreparedValidationCompletion,
    ResolvedValidationProfiles,
    ValidationCompletionCapabilities,
    ValidationCompletionService,
    ValidationRunBinding,
    ValidationRunTerminalResult,
)


NOW = "2026-07-14T12:00:00Z"


def replace_item(item: ApprovalItem, **updates: object) -> ApprovalItem:
    return ApprovalItem.model_validate({**item.model_dump(mode="json"), **updates})


def artifact(
    kind: str,
    label: str,
    *,
    lineage: tuple[str, ...] = (),
    ir_snapshot_id: str | None = None,
    constraint_snapshot_id: str | None = None,
    payload: object | None = None,
) -> ArtifactV2:
    raw = (
        canonical_json(payload.model_dump(mode="json")).encode()
        if hasattr(payload, "model_dump")
        else label.encode()
    )
    ref = object_ref_for_bytes(raw)
    return build_artifact_v2(
        kind=kind,  # type: ignore[arg-type]
        version_tuple=VersionTuple(
            ir_snapshot_id=ir_snapshot_id,
            constraint_snapshot_id=constraint_snapshot_id,
            tool_version="validation-test@1",
        ),
        lineage=lineage,
        payload_hash=ref.sha256,
        object_ref=ref,
    )


def object_binding(value: ArtifactV2) -> PreparedObjectBinding:
    return PreparedObjectBinding(
        object_ref=value.object_ref,
        location=ObjectLocation(
            store_id="test-store",
            key=value.object_ref.key,
            backend_generation=f"generation:{value.artifact_id}",
        ),
        expected_revision=None,
    )


def requirement() -> ApprovalRequirement:
    scope = DomainScope(domain_ids=("narrative",))
    return ApprovalRequirement(
        requirement_id="requirement:narrative",
        domain_scope=scope,
        required_permission=Permission(
            action="approval.decide",
            resource_kind="approval",
            domain_scope=scope,
        ),
        route_role="content_designer",
        min_approvals=1,
        assignee_principal_ids=("human:reviewer",),
        distinct_from_requirement_ids=(),
    )


def approval_item(
    *,
    subject: ArtifactV2,
    target: ArtifactV2 | None,
    kind: str,
    series_id: str = "series:1",
    approval_id: str = "approval:1",
    subject_revision: int = 1,
    workflow_revision: int = 2,
    run_id: str = "run:validation:1",
    rollback_profile_binding: ResolvedExecutionProfileBindingV1 | None = None,
) -> ApprovalItem:
    domain_ref = DomainRegistryRefV1(
        registry_version="domains@1",
        registry_digest="4" * 64,
    )
    target_binding = None
    if kind == "patch":
        assert target is not None
        target_binding = PatchTargetBindingV1(
            target_artifact_id=target.artifact_id,
            target_snapshot_id=target.version_tuple.ir_snapshot_id or "",
            target_digest=target.payload_hash,
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=4),
        )
    elif kind == "rollback_request":
        assert target is not None and rollback_profile_binding is not None
        target_binding = RollbackTargetBindingV1(
            target_artifact_kind=target.kind,
            target_artifact_id=target.artifact_id,
            target_snapshot_id=target.version_tuple.ir_snapshot_id,
            target_digest=target.payload_hash,
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:current", revision=8),
            rollback_profile_binding=rollback_profile_binding,
        )
    return ApprovalItem(
        approval_id=approval_id,
        subject_series_id=series_id,
        subject_revision=subject_revision,
        subject_kind=kind,  # type: ignore[arg-type]
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        status="validating",
        workflow_revision=workflow_revision,
        proposer=AuditActor(principal_id="human:author", principal_kind="human"),
        domain_scope=DomainScope(domain_ids=("narrative",)),
        domain_registry_ref=domain_ref,
        route_policy=DomainRoutePolicyRefV1(
            route_version="routes@1",
            route_digest="5" * 64,
            domain_registry_ref=domain_ref,
        ),
        role_policy_version="roles@1",
        role_policy_digest="6" * 64,
        approval_policy=ApprovalPolicyRefV1(
            policy_version="approval-policy@1",
            policy_digest="7" * 64,
        ),
        requirements=(requirement(),),
        decisions=(),
        active_validation_run_id=run_id,
        regression_evidence_artifact_ids=(),
        target_binding=target_binding,
        created_at=NOW,
    )


def resolved_profile(
    *,
    field_path: str,
    profile: ProfileRefV1,
    kind: str,
) -> ResolvedExecutionProfileBindingV1:
    return ResolvedExecutionProfileBindingV1(
        field_path=field_path,
        profile=profile,
        expected_profile_kind=kind,  # type: ignore[arg-type]
        profile_payload_hash="8" * 64,
        catalog_version=1,
        catalog_digest="9" * 64,
    )


def patch_prepared(
    outcome: str = "passed",
) -> tuple[
    PreparedValidationCompletion,
    ApprovalItem,
    SubjectHead,
    tuple[ArtifactV2, ...],
    ResolvedValidationProfiles,
]:
    subject = artifact("patch", "patch-subject")
    target = artifact(
        "ir_snapshot",
        "patch-preview",
        lineage=(subject.artifact_id,),
        ir_snapshot_id="snapshot:preview:1",
    )
    support = artifact(
        "review_report",
        "review-support",
        lineage=(target.artifact_id,),
        ir_snapshot_id="snapshot:preview:1",
    )
    item = approval_item(subject=subject, target=target, kind="patch")
    head = SubjectHead(
        subject_series_id=item.subject_series_id,
        current_subject_artifact_id=item.subject_artifact_id,
        current_approval_id=item.approval_id,
        revision=item.subject_revision,
    )
    profile = ProfileRefV1(profile_id="validation:patch", version=1)
    profile_binding = resolved_profile(
        field_path="/params/validation_policy",
        profile=profile,
        kind="validation",
    )
    payload = PatchValidationPayloadV1(
        subject=ValidationSubjectBindingV1(
            approval_id=item.approval_id,
            expected_workflow_revision=item.workflow_revision,
            subject_head_revision=head.revision,
            subject_artifact_id=item.subject_artifact_id,
            subject_digest=item.subject_digest,
            active_validation_run_id=item.active_validation_run_id or "",
        ),
        base_snapshot_artifact_id="artifact:base",
        preview_snapshot_artifact_id=target.artifact_id,
        candidate_config_export_artifact_ids=(),
        target=RefReadBindingV1(
            ref_name=item.target_binding.ref_name,  # type: ignore[union-attr]
            expected_ref=item.target_binding.expected_ref,  # type: ignore[union-attr]
        ),
        validation_policy=profile,
        checker_profiles=(),
        simulation_profiles=(),
        findings=(),
        review_artifact_ids=(support.artifact_id,),
        playtest_trace_artifact_ids=(),
        regression_suite_artifact_ids=(),
    )
    execution = ValidationRunBinding(
        run_id=item.active_validation_run_id or "",
        expected_run_revision=3,
        attempt_no=1,
        lease_id="lease:1",
        fencing_token=7,
        payload=payload,
        resolved_profiles=(profile_binding,),
    )
    outcome_code = {
        "passed": "patch_validation_passed",
        "failed": "patch_validation_failed",
        "unproven": "patch_validation_unproven",
        "execution_failed": "execution_failed",
        "cancelled": "cancelled",
        "timed_out": "timed_out",
    }[outcome]
    if outcome in {"execution_failed", "cancelled", "timed_out"}:
        prepared = PreparedValidationCompletion(
            execution=execution,
            outcome=outcome,
            outcome_code=outcome_code,
        )
    else:
        evidence_requirement = EvidenceRequirement(
            requirement_id="oracle:structure",
            kind="deterministic_oracle",
            applicability="required",
            status=outcome,  # type: ignore[arg-type]
            evidence_artifact_id=(support.artifact_id if outcome != "unproven" else None),
            reason_code=("oracle_unknown" if outcome == "unproven" else None),
            tool_version="checker@1",
        )
        evidence = EvidenceSet(
            subject_artifact_id=item.subject_artifact_id,
            subject_digest=item.subject_digest,
            policy_version="validation-policy@1",
            validation_run_id=execution.run_id,
            target_binding=item.target_binding,
            supporting_artifact_ids=(support.artifact_id,),
            finding_bindings=(),
            requirements=(evidence_requirement,),
            overall_status=outcome,  # type: ignore[arg-type]
        )
        evidence_artifact = artifact(
            "validation_evidence",
            "evidence-set",
            payload=evidence,
            lineage=(item.subject_artifact_id, target.artifact_id, support.artifact_id),
            ir_snapshot_id=target.version_tuple.ir_snapshot_id,
        )
        prepared = PreparedValidationCompletion(
            execution=execution,
            outcome=outcome,  # type: ignore[arg-type]
            outcome_code=outcome_code,
            evidence_set=evidence,
            evidence_set_artifact=evidence_artifact,
            object_bindings=(object_binding(evidence_artifact),),
        )
    resolution = ResolvedValidationProfiles(
        evidence_policy_version="validation-policy@1",
        primary=profile_binding,
    )
    return prepared, item, head, (subject, target, support), resolution


def rollback_prepared() -> tuple[
    PreparedValidationCompletion,
    ApprovalItem,
    SubjectHead,
    tuple[ArtifactV2, ...],
    ResolvedValidationProfiles,
]:
    subject = artifact("rollback_request", "rollback-subject")
    target = artifact(
        "ir_snapshot",
        "rollback-target",
        ir_snapshot_id="snapshot:rollback:1",
    )
    profile = ProfileRefV1(profile_id="rollback:content", version=3)
    profile_binding = resolved_profile(
        field_path="/params/rollback_profile",
        profile=profile,
        kind="rollback",
    )
    item = approval_item(
        subject=subject,
        target=target,
        kind="rollback_request",
        rollback_profile_binding=profile_binding,
    )
    head = SubjectHead(
        subject_series_id=item.subject_series_id,
        current_subject_artifact_id=item.subject_artifact_id,
        current_approval_id=item.approval_id,
        revision=item.subject_revision,
    )
    compatibility_profile = ProfileRefV1(
        profile_id="schema-compatibility:content",
        version=2,
    )
    compatibility_binding = resolved_profile(
        field_path="/params/schema_compatibility_policy",
        profile=compatibility_profile,
        kind="schema_compatibility",
    )
    binding = item.target_binding
    assert isinstance(binding, RollbackTargetBindingV1)
    payload = RollbackValidationPayloadV1(
        subject=ValidationSubjectBindingV1(
            approval_id=item.approval_id,
            expected_workflow_revision=item.workflow_revision,
            subject_head_revision=head.revision,
            subject_artifact_id=item.subject_artifact_id,
            subject_digest=item.subject_digest,
            active_validation_run_id=item.active_validation_run_id or "",
        ),
        ref_name=binding.ref_name,
        expected_current_ref=binding.expected_ref,
        target_artifact_id=binding.target_artifact_id,
        target_history_revision=5,
        rollback_profile=profile,
        schema_compatibility_policy=compatibility_profile,
        impact_profiles=(),
        regression_suite_artifact_ids=(),
    )
    execution = ValidationRunBinding(
        run_id=item.active_validation_run_id or "",
        expected_run_revision=4,
        attempt_no=1,
        lease_id="lease:rollback:1",
        fencing_token=11,
        payload=payload,
        resolved_profiles=tuple(
            sorted(
                (profile_binding, compatibility_binding),
                key=lambda value: value.field_path,
            )
        ),
    )
    support = artifact(
        "checker_run",
        "rollback-compatibility",
        lineage=(target.artifact_id,),
        ir_snapshot_id=target.version_tuple.ir_snapshot_id,
    )
    requirement = EvidenceRequirement(
        requirement_id="rollback:compatibility",
        kind="schema_compatibility",
        applicability="required",
        status="passed",
        evidence_artifact_id=support.artifact_id,
        tool_version="rollback-checker@1",
    )
    evidence = EvidenceSet(
        subject_artifact_id=item.subject_artifact_id,
        subject_digest=item.subject_digest,
        policy_version="rollback-validation-policy@1",
        validation_run_id=execution.run_id,
        target_binding=binding,
        supporting_artifact_ids=(support.artifact_id,),
        finding_bindings=(),
        requirements=(requirement,),
        overall_status="passed",
    )
    evidence_artifact = artifact(
        "validation_evidence",
        "rollback-evidence-set",
        payload=evidence,
        lineage=(subject.artifact_id, target.artifact_id, support.artifact_id),
        ir_snapshot_id=target.version_tuple.ir_snapshot_id,
    )
    prepared = PreparedValidationCompletion(
        execution=execution,
        outcome="passed",
        outcome_code="rollback_validation_passed",
        evidence_set=evidence,
        evidence_set_artifact=evidence_artifact,
        object_bindings=(object_binding(evidence_artifact),),
    )
    resolution = ResolvedValidationProfiles(
        evidence_policy_version="rollback-validation-policy@1",
        primary=profile_binding,
    )
    return prepared, item, head, (subject, target, support), resolution


def constraint_prepared(
    *,
    outcome: str = "passed",
    candidate_exists: bool = True,
) -> tuple[
    PreparedValidationCompletion,
    ApprovalItem,
    SubjectHead,
    tuple[ArtifactV2, ...],
    ResolvedValidationProfiles,
]:
    subject = artifact("constraint_proposal", "constraint-proposal")
    item = approval_item(subject=subject, target=None, kind="constraint_proposal")
    head = SubjectHead(
        subject_series_id=item.subject_series_id,
        current_subject_artifact_id=item.subject_artifact_id,
        current_approval_id=item.approval_id,
        revision=item.subject_revision,
    )
    validation_profile = ProfileRefV1(profile_id="validation:constraint", version=1)
    compiler_profile = ProfileRefV1(profile_id="compiler:constraint", version=2)
    validation_binding = resolved_profile(
        field_path="/params/validation_policy",
        profile=validation_profile,
        kind="validation",
    )
    compiler_binding = resolved_profile(
        field_path="/params/compiler_profile",
        profile=compiler_profile,
        kind="constraint_compiler",
    )
    payload = ConstraintValidationPayloadV1(
        subject=ValidationSubjectBindingV1(
            approval_id=item.approval_id,
            expected_workflow_revision=item.workflow_revision,
            subject_head_revision=head.revision,
            subject_artifact_id=item.subject_artifact_id,
            subject_digest=item.subject_digest,
            active_validation_run_id=item.active_validation_run_id or "",
        ),
        target=RefReadBindingV1(
            ref_name="constraints/head",
            expected_ref=RefValue(artifact_id="artifact:constraints:base", revision=2),
        ),
        dsl_grammar_version="constraint-dsl@1",
        compiler_profile=compiler_profile,
        differential_engines=(
            SolverEngineRefV1(engine_id="clingo", version=1),
            SolverEngineRefV1(engine_id="z3", version=2),
        ),
        golden_suite_artifact_id=None,
        regression_suite_artifact_ids=(),
        validation_policy=validation_profile,
    )
    execution = ValidationRunBinding(
        run_id=item.active_validation_run_id or "",
        expected_run_revision=3,
        attempt_no=1,
        lease_id="lease:constraint:1",
        fencing_token=9,
        payload=payload,
        resolved_profiles=tuple(
            sorted((compiler_binding, validation_binding), key=lambda v: v.field_path)
        ),
    )
    candidate = None
    compile_status = "failed"
    if candidate_exists:
        candidate = artifact(
            "constraint_snapshot",
            "constraint-candidate",
            lineage=(subject.artifact_id,),
            constraint_snapshot_id="constraint-snapshot:candidate:1",
        )
        compile_status = "passed"
    stages = (
        ConstraintCompileStageV1(stage_id="parse", stage="parse", status="passed"),
        ConstraintCompileStageV1(stage_id="typecheck", stage="typecheck", status="passed"),
        ConstraintCompileStageV1(
            stage_id="compile",
            stage="compile",
            status=compile_status,  # type: ignore[arg-type]
            reason_code=(None if candidate_exists else "compile_failed"),
        ),
        ConstraintCompileStageV1(
            stage_id="diff:clingo",
            stage="differential",
            status=("passed" if candidate_exists else "unproven"),
            engine_id="clingo",
            engine_version="1",
            reason_code=(None if candidate_exists else "compile_failed"),
        ),
        ConstraintCompileStageV1(
            stage_id="diff:z3",
            stage="differential",
            status=("passed" if candidate_exists else "unproven"),
            engine_id="z3",
            engine_version="2",
            reason_code=(None if candidate_exists else "compile_failed"),
        ),
        ConstraintCompileStageV1(
            stage_id="golden",
            stage="golden",
            status="not_applicable",
            reason_code="no_golden_suite",
        ),
    )
    compile_evidence = ConstraintCompileEvidenceV1(
        proposal_artifact_id=subject.artifact_id,
        candidate_constraint_snapshot_artifact_id=(
            None if candidate is None else candidate.artifact_id
        ),
        dsl_grammar_version=payload.dsl_grammar_version,
        compiler_profile=compiler_profile,
        stages=stages,
        overall_status=("passed" if candidate_exists else "failed"),
    )
    compile_parents = [subject.artifact_id]
    if candidate is not None:
        compile_parents.append(candidate.artifact_id)
    compile_artifact = artifact(
        "validation_evidence",
        "compile-evidence",
        payload=compile_evidence,
        lineage=tuple(compile_parents),
    )
    target_binding = None
    if candidate is not None:
        target_binding = ConstraintTargetBindingV1(
            target_artifact_id=candidate.artifact_id,
            target_snapshot_id=candidate.version_tuple.constraint_snapshot_id or "",
            target_digest=candidate.payload_hash,
            ref_name=payload.target.ref_name,
            expected_ref=payload.target.expected_ref,
        )
    compile_requirement = EvidenceRequirement(
        requirement_id="constraint:compile",
        kind="constraint_compile",
        applicability="required",
        status=compile_evidence.overall_status,
        evidence_artifact_id=compile_artifact.artifact_id,
        reason_code=(
            None
            if compile_evidence.overall_status == "passed"
            else f"compile_{compile_evidence.overall_status}"
        ),
        tool_version="compiler@2",
    )
    regression = None
    requirements = [compile_requirement]
    support_ids = [compile_artifact.artifact_id]
    if candidate_exists and outcome != "passed":
        regression = artifact(
            "regression_evidence",
            f"constraint-regression-{outcome}",
            lineage=(candidate.artifact_id,),  # type: ignore[union-attr]
            constraint_snapshot_id=candidate.version_tuple.constraint_snapshot_id,  # type: ignore[union-attr]
        )
        support_ids.append(regression.artifact_id)
        requirements.append(
            EvidenceRequirement(
                requirement_id="constraint:regression",
                kind="regression",
                applicability="required",
                status=outcome,  # type: ignore[arg-type]
                evidence_artifact_id=regression.artifact_id,
                reason_code=("regression_unproven" if outcome == "unproven" else None),
                tool_version="regression@1",
            )
        )
    evidence = EvidenceSet(
        subject_artifact_id=item.subject_artifact_id,
        subject_digest=item.subject_digest,
        policy_version="constraint-validation-policy@1",
        validation_run_id=execution.run_id,
        target_binding=target_binding,
        supporting_artifact_ids=tuple(support_ids),
        finding_bindings=(),
        requirements=tuple(requirements),
        overall_status=outcome,  # type: ignore[arg-type]
    )
    evidence_lineage = [subject.artifact_id, compile_artifact.artifact_id]
    if candidate is not None:
        evidence_lineage.append(candidate.artifact_id)
    if regression is not None:
        evidence_lineage.append(regression.artifact_id)
    evidence_artifact = artifact(
        "validation_evidence",
        "constraint-evidence-set",
        payload=evidence,
        lineage=tuple(evidence_lineage),
    )
    published = tuple(
        value
        for value in (candidate, compile_artifact, regression, evidence_artifact)
        if value is not None
    )
    prepared = PreparedValidationCompletion(
        execution=execution,
        outcome=outcome,  # type: ignore[arg-type]
        outcome_code=(
            "constraint_validated"
            if outcome == "passed"
            else "constraint_validation_failed_"
            + ("with_candidate" if candidate_exists else "without_candidate")
        ),
        evidence_set=evidence,
        evidence_set_artifact=evidence_artifact,
        constraint_compile_evidence=compile_evidence,
        constraint_compile_artifact=compile_artifact,
        constraint_candidate_artifact=candidate,
        regression_artifacts=(() if regression is None else (regression,)),
        object_bindings=tuple(object_binding(value) for value in published),
    )
    resolution = ResolvedValidationProfiles(
        evidence_policy_version="constraint-validation-policy@1",
        primary=validation_binding,
        compiler=compiler_binding,
    )
    return prepared, item, head, (subject,), resolution


def auto_patch_prepared():
    prepared, item, head, retained, resolution = patch_prepared("passed")
    evidence_artifact = prepared.evidence_set_artifact
    assert evidence_artifact is not None and item.target_binding is not None
    policy = AutoApplyPolicyRefV1(
        registry=AutoApplyPolicyRegistryRefV1(
            registry_version="auto-policies@1",
            registry_digest="c" * 64,
        ),
        policy_id="structural-safe",
        policy_version="1",
        policy_digest="d" * 64,
    )
    proof = AutoApplyProofV1(
        subject_artifact_id=item.subject_artifact_id,
        subject_digest=item.subject_digest,
        target_binding=item.target_binding,
        affected_domain_scope=item.domain_scope,
        validation_evidence_artifact_id=evidence_artifact.artifact_id,
        regression_evidence_artifact_ids=(),
        validation_profile_binding=AutoApplyValidationProfileBindingV1(
            validation_profile=resolution.primary.profile,
            validation_profile_payload_hash=resolution.primary.profile_payload_hash,
            policy=policy,
        ),
        deterministic_oracle_evidence=(),
        required_outcome_evidence=(),
        policy=policy,
    )
    proof_artifact = artifact(
        "validation_evidence",
        "auto-apply-proof",
        payload=proof,
        lineage=(
            item.subject_artifact_id,
            item.target_binding.target_artifact_id,
            evidence_artifact.artifact_id,
        ),
        ir_snapshot_id=item.target_binding.target_snapshot_id,
    )
    auto = PreparedValidationCompletion.model_validate(
        {
            **prepared.model_dump(mode="python"),
            "outcome_code": "patch_validation_auto_eligible",
            "auto_apply_proof": proof,
            "auto_apply_proof_artifact": proof_artifact,
            "object_bindings": (*prepared.object_bindings, object_binding(proof_artifact)),
        }
    )
    return auto, item, head, retained, resolution


@dataclass
class State:
    approvals: dict[str, ApprovalItem]
    heads: dict[str, SubjectHead]
    artifacts: dict[str, ArtifactV2]
    bindings: dict[str, ObjectBinding] = field(default_factory=dict)
    terminals: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    audits: list[str] = field(default_factory=list)
    idempotency: dict[
        tuple[str, str, str],
        tuple[str, dict[str, object]],
    ] = field(default_factory=dict)


class Repositories:
    def __init__(self, state: State) -> None:
        self.state = state

    def get(self, key: str) -> ApprovalItem | ArtifactV2 | None:
        return self.state.approvals.get(key) or self.state.artifacts.get(key)

    def put(self, value: ArtifactV2) -> ArtifactV2:
        existing = self.state.artifacts.get(value.artifact_id)
        if existing is not None and existing != value:
            raise Conflict("artifact collision")
        self.state.artifacts[value.artifact_id] = value
        return value

    def get_subject_head(self, series_id: str) -> SubjectHead | None:
        return self.state.heads.get(series_id)

    def compare_and_set(
        self, approval_id: str, expected_workflow_revision: int, replacement: ApprovalItem
    ) -> ApprovalItem:
        current = self.state.approvals[approval_id]
        if current.workflow_revision != expected_workflow_revision:
            raise Conflict("workflow revision changed")
        self.state.approvals[approval_id] = replacement
        return replacement

    compare_and_set_validation_completion = compare_and_set

    def get_result(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        request_hash: str,
    ) -> dict[str, object] | None:
        retained = self.state.idempotency.get((scope, operation, key))
        if retained is None:
            return None
        retained_hash, response = retained
        if retained_hash != request_hash:
            raise Conflict("idempotency key is already bound to another request")
        return deepcopy(response)

    def put_result(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        request_hash: str,
        resource_kind: str,
        resource_id: str,
        response: Mapping[str, object],
    ) -> dict[str, object]:
        replay = self.get_result(
            scope=scope,
            operation=operation,
            key=key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        retained = deepcopy(dict(response))
        self.state.idempotency[(scope, operation, key)] = (request_hash, retained)
        return deepcopy(retained)

    def bind_verified(
        self,
        ref: object,
        location: ObjectLocation,
        expected_revision: int | None,
    ) -> ObjectBinding:
        binding = ObjectBinding(
            object_ref=ref,
            location=location,
            status="active",
            revision=1,
            verified_at=NOW,
        )
        self.state.bindings[location.key] = binding
        return binding


class Profiles:
    def __init__(self, resolution: ResolvedValidationProfiles) -> None:
        self.resolution = resolution

    def resolve(self, execution: ValidationRunBinding) -> ResolvedValidationProfiles:
        return self.resolution


class Subjects:
    def __init__(self, facts: dict[str, DraftSubjectFacts]) -> None:
        self.facts = facts

    def inspect_draft_subject(self, artifact: ArtifactV2) -> DraftSubjectFacts:
        return self.facts[artifact.artifact_id]


class Verifier:
    fail = False

    def validate_publication(self, **kwargs: object) -> None:
        if self.fail:
            raise Conflict("lineage policy rejected")


class Runs:
    def __init__(self, state: State, expected: ValidationRunBinding) -> None:
        self.state = state
        self.expected = expected
        self.fail = False

    def publish_terminal(
        self,
        *,
        execution: ValidationRunBinding,
        outcome_code: str,
        approval_id: str,
        published_artifact_ids: tuple[str, ...],
        actor: AuditActor,
        initiated_by: AuditActor | None,
    ) -> ValidationRunTerminalResult:
        if execution != self.expected:
            raise Conflict("run/lease/fencing binding changed")
        self.state.terminals.append((outcome_code, published_artifact_ids))
        if self.fail:
            raise Conflict("terminal CAS failed")
        failure = None
        if outcome_code in {
            "execution_failed",
            "cancelled",
            "timed_out",
            "subject_superseded",
        }:
            failure = f"artifact:run-failure:{outcome_code}"
            self.state.artifacts[failure] = artifact("run_failure", failure)
        return ValidationRunTerminalResult(
            outcome_code=outcome_code,
            failure_artifact_id=failure,
        )


class Audit:
    def __init__(self, state: State) -> None:
        self.state = state

    def append(self, **kwargs: object) -> object:
        self.state.audits.append(str(kwargs["action"]))
        return object()


class AutoGuard:
    def __init__(self) -> None:
        self.calls: list[ApprovalItem] = []
        self.fail = False

    def validate_completion(self, **kwargs: object) -> None:
        projected = kwargs["projected_item"]
        assert isinstance(projected, ApprovalItem)
        assert projected.status == "validated"
        assert projected.auto_apply_proof is not None
        self.calls.append(projected)
        if self.fail:
            raise Conflict("auto-apply guard rejected")


class UnitOfWork:
    def __init__(self, state: State) -> None:
        self.state = state

    @contextmanager
    def begin(self) -> Iterator[object]:
        before = deepcopy(self.state)
        try:
            yield object()
        except BaseException:
            self.state.approvals = before.approvals
            self.state.heads = before.heads
            self.state.artifacts = before.artifacts
            self.state.bindings = before.bindings
            self.state.terminals = before.terminals
            self.state.audits = before.audits
            self.state.idempotency = before.idempotency
            raise


@dataclass
class Harness:
    state: State
    service: ValidationCompletionService
    runs: Runs
    verifier: Verifier
    auto_apply: AutoGuard
    capabilities: ValidationCompletionCapabilities


def harness(
    fixture: tuple[
        PreparedValidationCompletion,
        ApprovalItem,
        SubjectHead,
        tuple[ArtifactV2, ...],
        ResolvedValidationProfiles,
    ],
) -> Harness:
    prepared, item, head, retained, resolution = fixture
    state = State(
        approvals={item.approval_id: item},
        heads={item.subject_series_id: head},
        artifacts={value.artifact_id: value for value in retained},
    )
    repositories = Repositories(state)
    runs = Runs(state, prepared.execution)
    verifier = Verifier()
    auto_apply = AutoGuard()
    subject_facts: dict[str, DraftSubjectFacts] = {}
    payload = prepared.execution.payload
    binding = item.target_binding
    if isinstance(payload, RollbackValidationPayloadV1):
        assert isinstance(binding, RollbackTargetBindingV1)
        subject_facts[item.subject_artifact_id] = DraftSubjectFacts(
            subject_kind="rollback_request",
            subject_revision=None,
            produced_by="human",
            producer_run_id=None,
            supersedes_artifact_id=None,
            target_artifact_id=binding.target_artifact_id,
            target_snapshot_id=binding.target_snapshot_id,
            rollback_request=RollbackRequestV1(
                ref_name=binding.ref_name,
                expected_current_ref=binding.expected_ref,
                target_artifact_id=binding.target_artifact_id,
                target_history_revision=payload.target_history_revision,
                rollback_profile_binding=binding.rollback_profile_binding,
                reason="restore retained content",
            ),
        )
    capabilities = ValidationCompletionCapabilities(
        approvals=repositories,
        artifacts=repositories,
        object_bindings=repositories,
        idempotency=repositories,
        profiles=Profiles(resolution),
        verifier=verifier,
        runs=runs,
        audit=Audit(state),
        subjects=Subjects(subject_facts),
        auto_apply=auto_apply,
    )
    service = ValidationCompletionService(
        unit_of_work=UnitOfWork(state),
        bind_capabilities=lambda transaction: capabilities,
        audit_chain_id="platform-authority",
    )
    return Harness(
        state=state,
        service=service,
        runs=runs,
        verifier=verifier,
        auto_apply=auto_apply,
        capabilities=capabilities,
    )


def context(run_id: str = "run:validation:1") -> ApprovalCommandContext:
    return ApprovalCommandContext(
        actor=AuditActor(principal_id="service:worker", principal_kind="service"),
        initiated_by=AuditActor(principal_id="human:author", principal_kind="human"),
        request_id="request:complete-validation",
        run_id=run_id,
        idempotency_scope=run_id,
        idempotency_key="terminal:1",
        request_hash="a" * 64,
    )
