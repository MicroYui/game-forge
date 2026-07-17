from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

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
from gameforge.contracts.ir import SourceRef
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalPolicyRegistryV1,
    ApprovalPolicyV1,
    ApprovalRequirement,
    ApprovalTargetBinding,
    AutoApplyOracleEvidenceBindingV1,
    AutoApplyOutcomeEvidenceBindingV1,
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyPolicyRegistryV1,
    AutoApplyPolicyV1,
    AutoApplyProofBindingV1,
    AutoApplyProofV1,
    ConstraintCompileEvidenceV1,
    ConstraintCompileStageV1,
    CONSTRAINT_COMPILE_REQUIREMENT_KIND,
    ConstraintProposalV1,
    ConstraintSourceBinding,
    ConstraintTargetBindingV1,
    DeterministicOracleDefinitionV1,
    DeterministicOracleRefV1,
    DeterministicOracleRegistryRefV1,
    DeterministicOracleRegistryV1,
    EvidenceRequirement,
    EvidenceSet,
    FindingEvidenceBindingV1,
    PatchTargetBindingV1,
    QualifiedOutcomeRuleRefV1,
    RollbackRequestV1,
    RollbackTargetBindingV1,
    SubjectHead,
    compute_approval_policy_digest,
    compute_approval_policy_registry_digest,
    compute_auto_apply_policy_digest,
    compute_auto_apply_policy_registry_digest,
    compute_deterministic_oracle_digest,
    compute_deterministic_oracle_registry_digest,
    regression_companion_evidence_ids,
)


def _actor(principal_id: str = "human:alice", kind: str = "human") -> AuditActor:
    return AuditActor(principal_id=principal_id, principal_kind=kind)


def _domain_ref() -> DomainRegistryRefV1:
    return DomainRegistryRefV1(registry_version="domains@1", registry_digest="1" * 64)


def _route_ref() -> DomainRoutePolicyRefV1:
    return DomainRoutePolicyRefV1(
        route_version="routes@1",
        route_digest="2" * 64,
        domain_registry_ref=_domain_ref(),
    )


def _profile_binding() -> ResolvedExecutionProfileBindingV1:
    return ResolvedExecutionProfileBindingV1(
        field_path="/params/rollback_profile",
        profile=ProfileRefV1(profile_id="rollback.default", version=1),
        expected_profile_kind="rollback",
        profile_payload_hash="3" * 64,
        catalog_version=1,
        catalog_digest="4" * 64,
    )


def _patch_binding() -> PatchTargetBindingV1:
    return PatchTargetBindingV1(
        target_artifact_id="artifact:preview",
        target_snapshot_id="sha256:preview",
        target_digest="5" * 64,
        ref_name="content/head",
        expected_ref=RefValue(artifact_id="artifact:base", revision=7),
    )


def _requirement(status: str = "passed") -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="checker",
        kind="deterministic_checker",
        applicability="required",
        status=status,
        evidence_artifact_id="artifact:checker" if status != "unproven" else None,
        reason_code=None if status != "unproven" else "solver_unknown",
        tool_version="checker@1",
    )


def _approval_policy() -> ApprovalPolicyV1:
    fields = {
        "policy_version": "approval@policy-1",
        "subject_kinds": ("patch", "constraint_proposal", "rollback_request"),
        "maker_checker_required": True,
        "human_approver_required": True,
        "reauthorize_on_decision": True,
        "reauthorize_on_apply": True,
        "rollback_requires_approval": True,
        "terminal_revision_immutable": True,
    }
    return ApprovalPolicyV1(
        **fields,
        policy_digest=compute_approval_policy_digest(**fields),
    )


def _oracle_registry() -> DeterministicOracleRegistryV1:
    definition_fields = {
        "oracle_id": "graph.structural",
        "oracle_version": "1",
        "engine_kind": "graph",
        "tool_version": "checker@1",
        "domain_registry": _domain_ref(),
        "supported_domain_scope": "all",
        "evidence_artifact_kinds": ("checker_run",),
        "evidence_payload_schema_ids": ("checker-evidence@1",),
        "predicate_schema_id": "structural-predicate@1",
    }
    definition = DeterministicOracleDefinitionV1(
        **definition_fields,
        oracle_digest=compute_deterministic_oracle_digest(**definition_fields),
    )
    digest = compute_deterministic_oracle_registry_digest("oracles@1", (definition,))
    return DeterministicOracleRegistryV1(
        registry_version="oracles@1", definitions=(definition,), registry_digest=digest
    )


def _auto_policy_registry() -> AutoApplyPolicyRegistryV1:
    oracle = _oracle_registry().definitions[0]
    policy = AutoApplyPolicyV1(
        policy_id="structural-safe",
        policy_version="1",
        allowed_operation_kinds=("add_relation",),
        maximum_operation_count=1,
        domain_registry=_domain_ref(),
        deterministic_oracle_registry=DeterministicOracleRegistryRefV1(
            registry_version="oracles@1",
            registry_digest=_oracle_registry().registry_digest,
        ),
        required_deterministic_oracles=(
            DeterministicOracleRefV1(
                oracle_id=oracle.oracle_id,
                oracle_version=oracle.oracle_version,
                oracle_digest=oracle.oracle_digest,
            ),
        ),
        required_outcome_rules=(
            QualifiedOutcomeRuleRefV1(
                resolved_policy_id="patch-validation", outcome_rule_id="passed"
            ),
        ),
        allowed_domain_scopes=(DomainScope(domain_ids=("narrative",)),),
        forbidden_domain_scopes=(),
        require_no_numeric_value_change=True,
        require_no_narrative_text_change=True,
        allowed_ref_names=("content/head",),
    )
    digest = compute_auto_apply_policy_registry_digest("auto@1", (policy,))
    return AutoApplyPolicyRegistryV1(
        registry_version="auto@1", policies=(policy,), registry_digest=digest
    )


def _auto_policy_ref() -> AutoApplyPolicyRefV1:
    registry = _auto_policy_registry()
    policy = registry.policies[0]
    return AutoApplyPolicyRefV1(
        registry=AutoApplyPolicyRegistryRefV1(
            registry_version=registry.registry_version,
            registry_digest=registry.registry_digest,
        ),
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_digest=compute_auto_apply_policy_digest(policy),
    )


def test_approval_target_binding_is_a_real_discriminated_union() -> None:
    adapter = TypeAdapter(ApprovalTargetBinding)
    patch = adapter.validate_python(_patch_binding().model_dump(mode="json"))
    assert isinstance(patch, PatchTargetBindingV1)

    constraint = adapter.validate_python(
        {
            "binding_schema_version": "approval-target-binding@1",
            "subject_kind": "constraint_proposal",
            "target_artifact_kind": "constraint_snapshot",
            "target_artifact_id": "artifact:constraint",
            "target_snapshot_id": "sha256:constraint",
            "target_digest": "6" * 64,
            "ref_name": "constraints/head",
            "expected_ref": None,
        }
    )
    assert isinstance(constraint, ConstraintTargetBindingV1)

    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "binding_schema_version": "approval-target-binding@1",
                "subject_kind": "patch",
                "target_artifact_kind": "constraint_snapshot",
                "target_artifact_id": "artifact:x",
                "target_snapshot_id": "sha256:x",
                "target_digest": "7" * 64,
                "ref_name": "content/head",
                "expected_ref": None,
            }
        )
    schema = adapter.json_schema()
    assert "oneOf" in schema and schema["discriminator"]["propertyName"] == "subject_kind"


def test_new_ref_target_binding_defaults_expected_ref_to_null_and_round_trips() -> None:
    # A first-write (new-ref) target binding leaves ``expected_ref`` unset; it now
    # defaults to null and canonicalises identically to an explicit ``null`` (both
    # drop the key under ``_canon``), so the binding round-trips without error.
    payload = _patch_binding().model_dump(mode="json")
    payload.pop("expected_ref")
    reparsed = TypeAdapter(ApprovalTargetBinding).validate_python(payload)
    assert isinstance(reparsed, PatchTargetBindingV1)
    assert reparsed.expected_ref is None

    new_ref_binding = PatchTargetBindingV1(
        target_artifact_id="artifact:new",
        target_snapshot_id="sha256:new",
        target_digest="9" * 64,
        ref_name="content/head",
    )
    assert new_ref_binding.expected_ref is None
    from gameforge.contracts.canonical import canonical_json

    round_tripped = TypeAdapter(ApprovalTargetBinding).validate_json(
        canonical_json(new_ref_binding.model_dump(mode="json"))
    )
    assert round_tripped == new_ref_binding
    # Explicit-null and defaulted-null canonicalise to the same bytes.
    assert canonical_json(new_ref_binding.model_dump(mode="json")) == canonical_json(
        PatchTargetBindingV1(
            target_artifact_id="artifact:new",
            target_snapshot_id="sha256:new",
            target_digest="9" * 64,
            ref_name="content/head",
            expected_ref=None,
        ).model_dump(mode="json")
    )


def test_rollback_binding_requires_non_null_ref_and_exact_profile_slot() -> None:
    binding = RollbackTargetBindingV1(
        target_artifact_kind="ir_snapshot",
        target_artifact_id="artifact:old",
        target_snapshot_id="sha256:old",
        target_digest="8" * 64,
        ref_name="content/head",
        expected_ref=RefValue(artifact_id="artifact:current", revision=9),
        rollback_profile_binding=_profile_binding(),
    )
    assert binding.subject_kind == "rollback_request"
    assert (
        TypeAdapter(ApprovalTargetBinding).validate_python(binding.model_dump(mode="json"))
        == binding
    )
    with pytest.raises(ValidationError, match="rollback_profile"):
        RollbackTargetBindingV1(
            **{
                **binding.model_dump(),
                "rollback_profile_binding": _profile_binding().model_copy(
                    update={"field_path": "/wrong"}
                ),
            }
        )


def test_constraint_proposal_enforces_revision_and_producer_identity() -> None:
    source = ConstraintSourceBinding(
        source_artifact_id="artifact:source",
        source_ref=SourceRef(adapter="aureus", file="quests.csv", row=2),
        provenance_hash="1" * 64,
    )
    human = ConstraintProposalV1(
        revision=1,
        dsl_grammar_version="dsl@1",
        domain_scope=DomainScope(domain_ids=("narrative",)),
        constraints=(),
        source_bindings=(source,),
        produced_by="human",
        producer_run_id=None,
        rationale="typed authoring",
    )
    assert human.producer_run_id is None
    assert ConstraintProposalV1.model_validate(human.model_dump(mode="json")) == human
    with pytest.raises(ValidationError, match="producer_run_id"):
        ConstraintProposalV1(
            **{**human.model_dump(), "produced_by": "agent", "producer_run_id": None}
        )
    with pytest.raises(ValidationError, match="supersedes_artifact_id"):
        ConstraintProposalV1(**{**human.model_dump(), "revision": 2})


def test_rollback_request_freezes_exact_current_ref_and_profile() -> None:
    request = RollbackRequestV1(
        ref_name="content/head",
        expected_current_ref=RefValue(artifact_id="artifact:current", revision=10),
        target_artifact_id="artifact:old",
        target_history_revision=3,
        rollback_profile_binding=_profile_binding(),
        reason="regression",
    )
    assert request.rollback_profile_binding.profile.profile_id == "rollback.default"


def test_evidence_requirement_and_set_cannot_claim_false_pass() -> None:
    with pytest.raises(ValidationError, match="not_applicable"):
        EvidenceRequirement(
            requirement_id="playtest",
            kind="playtest",
            applicability="required",
            status="not_applicable",
            reason_code="skipped",
            tool_version="playtest@1",
        )
    with pytest.raises(ValidationError, match="overall_status"):
        EvidenceSet(
            subject_artifact_id="artifact:patch",
            subject_digest="9" * 64,
            policy_version="validation@1",
            validation_run_id="run:1",
            target_binding=_patch_binding(),
            supporting_artifact_ids=("artifact:checker",),
            finding_bindings=(),
            requirements=(_requirement("unproven"),),
            overall_status="passed",
        )


def test_evidence_set_canonicalizes_ids_and_finding_revisions() -> None:
    finding = FindingEvidenceBindingV1(
        finding_id="finding:1",
        finding_revision=2,
        evidence_artifact_id="artifact:finding-evidence",
        finding_digest="a" * 64,
    )
    evidence = EvidenceSet(
        subject_artifact_id="artifact:patch",
        subject_digest="9" * 64,
        policy_version="validation@1",
        validation_run_id="run:1",
        target_binding=_patch_binding(),
        supporting_artifact_ids=("artifact:z", "artifact:a", "artifact:z"),
        finding_bindings=(finding,),
        requirements=(_requirement(),),
        overall_status="passed",
    )
    assert evidence.supporting_artifact_ids == ("artifact:a", "artifact:z")


def test_regression_companion_ids_follow_artifact_rule_not_semantic_kind() -> None:
    requirements = tuple(
        EvidenceRequirement(
            requirement_id=requirement_id,
            kind=kind,
            applicability="required",
            status="passed",
            evidence_artifact_id=artifact_id,
            tool_version="validator@1",
        )
        for requirement_id, kind, artifact_id in (
            ("artifact", "artifact", "artifact:evidence:artifact"),
            ("compile", CONSTRAINT_COMPILE_REQUIREMENT_KIND, "artifact:compile"),
            ("history", "history", "artifact:evidence:history"),
            ("regression", "regression", "artifact:evidence:regression"),
        )
    )
    evidence = EvidenceSet(
        subject_artifact_id="artifact:subject",
        subject_digest="9" * 64,
        policy_version="validation@1",
        validation_run_id="run:1",
        target_binding=_patch_binding(),
        supporting_artifact_ids=(),
        finding_bindings=(),
        requirements=requirements,
        overall_status="passed",
    )

    assert regression_companion_evidence_ids(evidence) == (
        "artifact:evidence:artifact",
        "artifact:evidence:history",
        "artifact:evidence:regression",
    )


def test_constraint_compile_evidence_requires_core_stages_and_candidate_binding() -> None:
    stage_without_engine = ConstraintCompileStageV1(
        stage_id="parse", stage="parse", status="passed"
    )
    assert stage_without_engine.engine_id is None
    stages = tuple(
        ConstraintCompileStageV1(stage_id=stage, stage=stage, status="passed")
        for stage in ("parse", "typecheck", "compile")
    ) + (
        ConstraintCompileStageV1(
            stage_id="differential:clingo",
            stage="differential",
            status="passed",
            engine_id="clingo",
            engine_version="1",
        ),
        ConstraintCompileStageV1(
            stage_id="differential:z3",
            stage="differential",
            status="passed",
            engine_id="z3",
            engine_version="1",
        ),
        ConstraintCompileStageV1(
            stage_id="golden",
            stage="golden",
            status="not_applicable",
            reason_code="no_golden_suite",
        ),
    )
    evidence = ConstraintCompileEvidenceV1(
        proposal_artifact_id="artifact:proposal",
        candidate_constraint_snapshot_artifact_id="artifact:candidate",
        dsl_grammar_version="dsl@1",
        compiler_profile=ProfileRefV1(profile_id="compiler", version=1),
        stages=stages,
        overall_status="passed",
    )
    assert evidence.stages[0].stage == "parse"
    with pytest.raises(ValidationError, match="candidate_constraint_snapshot_artifact_id"):
        ConstraintCompileEvidenceV1(
            **{**evidence.model_dump(), "candidate_constraint_snapshot_artifact_id": None}
        )

    failed_after_compile = tuple(
        stage.model_copy(
            update={
                "status": "failed",
                "reason_code": "differential_mismatch",
            }
        )
        if stage.stage == "differential" and stage.engine_id == "z3"
        else stage
        for stage in evidence.stages
    )
    with pytest.raises(ValidationError, match="candidate_constraint_snapshot_artifact_id"):
        ConstraintCompileEvidenceV1(
            **{
                **evidence.model_dump(),
                "candidate_constraint_snapshot_artifact_id": None,
                "stages": failed_after_compile,
                "overall_status": "failed",
            }
        )


@pytest.mark.parametrize(
    "values",
    [
        {"stage_id": "diff", "stage": "differential", "status": "passed"},
        {
            "stage_id": "parse",
            "stage": "parse",
            "status": "passed",
            "engine_id": "parser",
            "engine_version": "1",
        },
        {
            "stage_id": "compile",
            "stage": "compile",
            "status": "passed",
            "reason_code": "unexpected",
        },
    ],
)
def test_constraint_compile_stage_rejects_invalid_engine_or_reason_shape(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ConstraintCompileStageV1.model_validate(values)


def test_constraint_compile_v1_reader_preserves_historical_reason_codes() -> None:
    stage = ConstraintCompileStageV1(
        stage_id="differential:legacy@1",
        stage="differential",
        status="unproven",
        engine_id="legacy",
        engine_version="1",
        reason_code="historical_adapter_reason",
    )

    assert stage.reason_code == "historical_adapter_reason"


def test_approval_policy_guards_are_literal_true_and_digest_bound() -> None:
    policy = _approval_policy()
    with pytest.raises(ValidationError):
        ApprovalPolicyV1(**{**policy.model_dump(), "maker_checker_required": False})
    registry_digest = compute_approval_policy_registry_digest((policy,))
    registry = ApprovalPolicyRegistryV1(policies=(policy,), registry_digest=registry_digest)
    assert registry.policies == (policy,)
    assert ApprovalPolicyRegistryV1.model_validate(registry.model_dump(mode="json")) == registry
    with pytest.raises(ValidationError, match="registry_digest"):
        ApprovalPolicyRegistryV1(policies=(policy,), registry_digest="f" * 64)


def test_deterministic_oracle_registry_is_digest_bound() -> None:
    registry = _oracle_registry()
    assert registry.definitions[0].engine_kind == "graph"
    assert (
        DeterministicOracleRegistryV1.model_validate(registry.model_dump(mode="json")) == registry
    )
    with pytest.raises(ValidationError, match="oracle_digest"):
        DeterministicOracleDefinitionV1(
            **{**registry.definitions[0].model_dump(), "oracle_digest": "f" * 64}
        )


def test_auto_apply_policy_rejects_allowed_forbidden_overlap() -> None:
    registry = _auto_policy_registry()
    policy = registry.policies[0]
    with pytest.raises(ValidationError, match="allowed.*forbidden"):
        AutoApplyPolicyV1(
            **{
                **policy.model_dump(),
                "forbidden_domain_scopes": policy.allowed_domain_scopes,
            }
        )


def test_auto_apply_proof_requires_exact_scope_for_every_oracle() -> None:
    policy_ref = _auto_policy_ref()
    policy = _auto_policy_registry().policies[0]
    oracle_ref = policy.required_deterministic_oracles[0]
    affected_scope = DomainScope(domain_ids=("narrative",))
    valid_oracle_evidence = AutoApplyOracleEvidenceBindingV1(
        oracle=oracle_ref,
        evaluated_domain_scope=affected_scope,
        evidence_artifact_id="artifact:oracle",
        evidence_payload_hash="b" * 64,
    )
    outcome_evidence = AutoApplyOutcomeEvidenceBindingV1(
        rule=policy.required_outcome_rules[0],
        requirement_id="checker",
        evidence_artifact_id="artifact:outcome",
        evidence_payload_hash="e" * 64,
    )
    proof = AutoApplyProofV1(
        subject_artifact_id="artifact:patch",
        subject_digest="c" * 64,
        target_binding=_patch_binding(),
        affected_domain_scope=affected_scope,
        validation_evidence_artifact_id="artifact:validation",
        regression_evidence_artifact_ids=(),
        validation_profile_binding={
            "validation_profile": {"profile_id": "patch.validate", "version": 1},
            "validation_profile_payload_hash": "d" * 64,
            "policy": policy_ref.model_dump(),
        },
        deterministic_oracle_evidence=(valid_oracle_evidence,),
        required_outcome_evidence=(outcome_evidence,),
        policy=policy_ref,
    )
    assert AutoApplyProofV1.model_validate(proof.model_dump(mode="json")) == proof

    oracle_evidence = AutoApplyOracleEvidenceBindingV1(
        oracle=oracle_ref,
        evaluated_domain_scope=DomainScope(domain_ids=("other",)),
        evidence_artifact_id="artifact:oracle",
        evidence_payload_hash="b" * 64,
    )
    with pytest.raises(ValidationError, match="evaluated_domain_scope"):
        AutoApplyProofV1(
            subject_artifact_id="artifact:patch",
            subject_digest="c" * 64,
            target_binding=_patch_binding(),
            affected_domain_scope=DomainScope(domain_ids=("narrative",)),
            validation_evidence_artifact_id="artifact:validation",
            regression_evidence_artifact_ids=(),
            validation_profile_binding={
                "validation_profile": {"profile_id": "patch.validate", "version": 1},
                "validation_profile_payload_hash": "d" * 64,
                "policy": policy_ref.model_dump(),
            },
            deterministic_oracle_evidence=(oracle_evidence,),
            required_outcome_evidence=(),
            policy=policy_ref,
        )


def test_approval_item_enforces_target_kind_auto_apply_and_maker_checker() -> None:
    policy = _approval_policy()
    policy_ref = ApprovalPolicyRefV1(
        policy_version=policy.policy_version, policy_digest=policy.policy_digest
    )
    requirement = ApprovalRequirement(
        requirement_id="narrative",
        domain_scope=DomainScope(domain_ids=("narrative",)),
        required_permission=Permission(
            action="approval.decide",
            resource_kind="approval",
            domain_scope=DomainScope(domain_ids=("narrative",)),
        ),
        route_role="content_designer",
        min_approvals=1,
        assignee_principal_ids=("human:bob",),
        distinct_from_requirement_ids=(),
    )
    decision = ApprovalDecision(
        decision_id="decision:1",
        requirement_ids=("narrative",),
        decision="approve",
        actor=_actor("human:bob"),
        expected_workflow_revision=2,
        reason_code="reviewed",
        occurred_at="2026-07-13T00:00:00Z",
    )
    item = ApprovalItem(
        approval_id="approval:1",
        subject_series_id="patch-series:1",
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id="artifact:patch",
        subject_digest="c" * 64,
        status="approved",
        workflow_revision=3,
        proposer=_actor(),
        domain_scope=DomainScope(domain_ids=("narrative",)),
        domain_registry_ref=_domain_ref(),
        route_policy=_route_ref(),
        role_policy_version="roles@1",
        role_policy_digest="e" * 64,
        approval_policy=policy_ref,
        requirements=(requirement,),
        decisions=(decision,),
        evidence_set_artifact_id="artifact:evidence-set",
        regression_evidence_artifact_ids=(),
        target_binding=_patch_binding(),
        created_at="2026-07-13T00:00:00Z",
        submitted_at="2026-07-13T00:00:00Z",
        decided_at="2026-07-13T00:00:00Z",
    )
    assert item.status == "approved"
    assert ApprovalItem.model_validate(item.model_dump(mode="json")) == item

    with pytest.raises(ValidationError, match="proposer"):
        ApprovalItem(
            **{
                **item.model_dump(),
                "decisions": (decision.model_copy(update={"actor": _actor("human:alice")}),),
            }
        )
    with pytest.raises(ValidationError, match="target_binding"):
        ApprovalItem(
            **{
                **item.model_dump(),
                "subject_kind": "rollback_request",
                "target_binding": _patch_binding(),
            }
        )


@pytest.mark.parametrize(
    "status",
    ["validated", "auto_apply_eligible", "applied", "superseded", "rolled_back"],
)
def test_patch_auto_apply_proof_survives_its_frozen_workflow_history(status: str) -> None:
    policy = _approval_policy()
    target = _patch_binding()
    proof = AutoApplyProofBindingV1(
        proof_artifact_id="artifact:auto-proof",
        policy=_auto_policy_ref(),
        subject_digest="c" * 64,
        target_digest=target.target_digest,
        expected_ref=target.expected_ref,
        validation_evidence_artifact_id="artifact:evidence-set",
    )
    item = ApprovalItem(
        approval_id="approval:auto",
        subject_series_id="patch-series:auto",
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id="artifact:patch",
        subject_digest="c" * 64,
        status=status,
        workflow_revision=3,
        proposer=_actor(),
        domain_scope=DomainScope(domain_ids=("narrative",)),
        domain_registry_ref=_domain_ref(),
        route_policy=_route_ref(),
        role_policy_version="roles@1",
        role_policy_digest="e" * 64,
        approval_policy=ApprovalPolicyRefV1(
            policy_version=policy.policy_version,
            policy_digest=policy.policy_digest,
        ),
        requirements=(),
        decisions=(),
        evidence_set_artifact_id="artifact:evidence-set",
        regression_evidence_artifact_ids=(),
        target_binding=target,
        auto_apply_proof=proof,
        created_at="2026-07-13T00:00:00Z",
        applied_at=("2026-07-13T00:01:00Z" if status in {"applied", "rolled_back"} else None),
    )

    assert item.auto_apply_proof == proof


def test_auto_apply_proof_remains_patch_only_and_mandatory_for_eligible_state() -> None:
    policy = _approval_policy()
    target = _patch_binding()
    proof = AutoApplyProofBindingV1(
        proof_artifact_id="artifact:auto-proof",
        policy=_auto_policy_ref(),
        subject_digest="c" * 64,
        target_digest=target.target_digest,
        expected_ref=target.expected_ref,
        validation_evidence_artifact_id="artifact:evidence-set",
    )
    common = {
        "approval_id": "approval:auto",
        "subject_series_id": "patch-series:auto",
        "subject_revision": 1,
        "subject_artifact_id": "artifact:patch",
        "subject_digest": "c" * 64,
        "workflow_revision": 3,
        "proposer": _actor(),
        "domain_scope": DomainScope(domain_ids=("narrative",)),
        "domain_registry_ref": _domain_ref(),
        "route_policy": _route_ref(),
        "role_policy_version": "roles@1",
        "role_policy_digest": "e" * 64,
        "approval_policy": ApprovalPolicyRefV1(
            policy_version=policy.policy_version,
            policy_digest=policy.policy_digest,
        ),
        "requirements": (),
        "decisions": (),
        "evidence_set_artifact_id": "artifact:evidence-set",
        "regression_evidence_artifact_ids": (),
        "created_at": "2026-07-13T00:00:00Z",
    }

    with pytest.raises(ValidationError, match="auto_apply_eligible"):
        ApprovalItem(
            **common,
            subject_kind="patch",
            status="auto_apply_eligible",
            target_binding=target,
        )

    constraint_target = ConstraintTargetBindingV1(
        target_artifact_id=target.target_artifact_id,
        target_snapshot_id=target.target_snapshot_id,
        target_digest=target.target_digest,
        ref_name=target.ref_name,
        expected_ref=target.expected_ref,
    )
    with pytest.raises(ValidationError, match="only for patch"):
        ApprovalItem(
            **common,
            subject_kind="constraint_proposal",
            status="superseded",
            target_binding=constraint_target,
            auto_apply_proof=proof,
        )


def test_subject_head_is_strict_frozen_and_revisioned() -> None:
    head = SubjectHead(
        subject_series_id="patch-series:1",
        current_subject_artifact_id="artifact:patch",
        current_approval_id="approval:1",
        revision=1,
    )
    with pytest.raises(ValidationError):
        head.revision = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SubjectHead(
            subject_series_id="patch-series:1",
            current_subject_artifact_id="artifact:patch",
            current_approval_id="approval:1",
            revision=0,
        )
