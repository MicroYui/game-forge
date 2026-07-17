from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Literal

import pytest

from gameforge.contracts.auto_apply_ownership import (
    AUTO_APPLY_IR_ALL_TAG_V1,
    auto_apply_ir_classifier_binding,
)
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    ProfileRefV1,
    RunKindRef,
    ValidationProfileDetailsV1,
    canonical_config_hash,
    execution_profile_payload_hash,
)
from gameforge.contracts.findings import PatchV2, TypedOp
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicyRefV1,
    DomainScope,
    compute_domain_registry_digest,
)
from gameforge.contracts.jobs import (
    ResolvedArtifactRequirementV1,
    ResolvedPolicySnapshotV1,
    resolved_policy_snapshot_digest,
)
from gameforge.contracts.lineage import (
    ArtifactKind,
    AuditActor,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    AutoApplyOracleEvidenceBindingV1,
    AutoApplyOutcomeEvidenceBindingV1,
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyPolicyRegistryV1,
    AutoApplyPolicyV1,
    AutoApplyProofBindingV1,
    AutoApplyProofV1,
    AutoApplyValidationProfileBindingV1,
    DeterministicOracleDefinitionV1,
    DeterministicOracleRefV1,
    DeterministicOracleRegistryRefV1,
    DeterministicOracleRegistryV1,
    EvidenceRequirement,
    EvidenceSet,
    PatchTargetBindingV1,
    QualifiedOutcomeRuleRefV1,
    compute_auto_apply_policy_digest,
    compute_auto_apply_policy_registry_digest,
    compute_deterministic_oracle_digest,
    compute_deterministic_oracle_registry_digest,
)
from gameforge.platform.approvals.auto_apply import (
    AutoApplyChangeAssessment,
    AutoApplyGuardError,
    OracleEvidenceClaims,
    QualifiedOutcomeEvidenceClaims,
    ResolvedArtifactPayload,
    is_auto_apply_candidate_eligible,
    validate_auto_apply,
)


_BASE_ARTIFACT_ID = "artifact:base"
_BASE_SNAPSHOT_ID = "sha256:base"
_TARGET_SNAPSHOT_ID = "sha256:target"
_CURRENT_REF = RefValue(artifact_id=_BASE_ARTIFACT_ID, revision=7)
LiteralLineage = Literal["subject_target", "subject_only"]


def _json_bytes(payload: Any) -> bytes:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _artifact_payload(
    *,
    kind: ArtifactKind,
    payload_schema_id: str,
    payload: Any,
    lineage: tuple[str, ...],
    version_tuple: VersionTuple | None = None,
) -> ResolvedArtifactPayload:
    payload_bytes = _json_bytes(payload)
    object_ref = object_ref_for_bytes(payload_bytes)
    artifact = build_artifact_v2(
        kind=kind,
        version_tuple=version_tuple or VersionTuple(tool_version="m4a-test@1"),
        lineage=lineage,
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={},
        created_at="2026-07-14T00:00:00Z",
    )
    return ResolvedArtifactPayload(
        artifact=artifact,
        payload_schema_id=payload_schema_id,
        payload_bytes=payload_bytes,
    )


def _domain_registry(
    domain_ids: tuple[str, ...], *, ownership_complete: bool = True
) -> DomainRegistryV1:
    definitions = tuple(
        DomainDefinitionV1(
            domain_id=domain_id,
            display_name=domain_id,
            tags=((AUTO_APPLY_IR_ALL_TAG_V1,) if ownership_complete and index == 0 else ()),
            status="active",
        )
        for index, domain_id in enumerate(domain_ids)
    )
    version = "domains@1"
    return DomainRegistryV1(
        registry_version=version,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(version, definitions),
    )


def _domain_ref(registry: DomainRegistryV1) -> DomainRegistryRefV1:
    return DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _oracle_registry(
    *,
    domain_registry: DomainRegistryV1,
    supported_scope: DomainScope | str,
    artifact_kinds: tuple[ArtifactKind, ...] = ("checker_run",),
    payload_schema_ids: tuple[str, ...] = ("checker-evidence@1",),
    include_unrelated_definition: bool = False,
) -> DeterministicOracleRegistryV1:
    fields = {
        "oracle_id": "graph.structural",
        "oracle_version": "1",
        "engine_kind": "graph",
        "tool_version": "checker@1",
        "domain_registry": _domain_ref(domain_registry),
        "supported_domain_scope": supported_scope,
        "evidence_artifact_kinds": artifact_kinds,
        "evidence_payload_schema_ids": payload_schema_ids,
        "predicate_schema_id": "structural-predicate@1",
    }
    definition = DeterministicOracleDefinitionV1(
        **fields,
        oracle_digest=compute_deterministic_oracle_digest(**fields),
    )
    definitions = [definition]
    if include_unrelated_definition:
        unrelated_fields = {
            "oracle_id": "other.unrelated",
            "oracle_version": "1",
            "engine_kind": "graph",
            "tool_version": "other-checker@1",
            "domain_registry": DomainRegistryRefV1(
                registry_version="other-domains@1",
                registry_digest="9" * 64,
            ),
            "supported_domain_scope": DomainScope(domain_ids=("external-domain",)),
            "evidence_artifact_kinds": ("checker_run",),
            "evidence_payload_schema_ids": ("other-checker-evidence@1",),
            "predicate_schema_id": "other-predicate@1",
        }
        definitions.append(
            DeterministicOracleDefinitionV1(
                **unrelated_fields,
                oracle_digest=compute_deterministic_oracle_digest(**unrelated_fields),
            )
        )
    canonical_definitions = tuple(definitions)
    version = "oracles@1"
    return DeterministicOracleRegistryV1(
        registry_version=version,
        definitions=canonical_definitions,
        registry_digest=compute_deterministic_oracle_registry_digest(
            version, canonical_definitions
        ),
    )


def _policy_registry(
    *,
    domain_registry: DomainRegistryV1,
    oracle_registry: DeterministicOracleRegistryV1,
    allowed_operation_kinds: tuple[str, ...],
    maximum_operation_count: int,
    allowed_domain_scopes: tuple[DomainScope, ...],
    forbidden_domain_scopes: tuple[DomainScope, ...],
    allowed_ref_names: tuple[str, ...] = ("content/head",),
    require_no_numeric_value_change: bool = True,
    require_no_narrative_text_change: bool = True,
) -> AutoApplyPolicyRegistryV1:
    oracle = oracle_registry.definitions[0]
    policy = AutoApplyPolicyV1(
        policy_id="structural-safe",
        policy_version="1",
        allowed_operation_kinds=allowed_operation_kinds,
        maximum_operation_count=maximum_operation_count,
        domain_registry=_domain_ref(domain_registry),
        deterministic_oracle_registry=DeterministicOracleRegistryRefV1(
            registry_version=oracle_registry.registry_version,
            registry_digest=oracle_registry.registry_digest,
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
                resolved_policy_id="patch-validation",
                outcome_rule_id="regression-passed",
            ),
        ),
        allowed_domain_scopes=allowed_domain_scopes,
        forbidden_domain_scopes=forbidden_domain_scopes,
        require_no_numeric_value_change=require_no_numeric_value_change,
        require_no_narrative_text_change=require_no_narrative_text_change,
        allowed_ref_names=allowed_ref_names,
    )
    version = "auto@1"
    return AutoApplyPolicyRegistryV1(
        registry_version=version,
        policies=(policy,),
        registry_digest=compute_auto_apply_policy_registry_digest(version, (policy,)),
    )


def _policy_ref(registry: AutoApplyPolicyRegistryV1) -> AutoApplyPolicyRefV1:
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


def _validation_profile(
    *,
    policy_ref: AutoApplyPolicyRefV1,
    domain_scope: DomainScope,
) -> ExecutionProfileDefinitionV1:
    config: dict[str, Any] = {}
    return ExecutionProfileDefinitionV1(
        profile=ProfileRefV1(profile_id="patch.validate", version=1),
        profile_kind="validation",
        compatible_run_kinds=(RunKindRef(kind="patch.validate", version=1),),
        domain_scope=domain_scope,
        stochastic=False,
        input_schema_ids=("patch@2", "ir-snapshot@1"),
        output_schema_ids=(
            "auto-apply-proof@1",
            "evidence-set@1",
            "regression-evidence@1",
        ),
        required_capabilities=("graph-checker",),
        display_name="Patch validation",
        handler_key="patch.validate",
        config_schema_id="patch-validation-config@1",
        config=config,
        config_hash=canonical_config_hash(config),
        details=ValidationProfileDetailsV1(
            subject_kinds=("patch",),
            auto_apply_policy=policy_ref,
        ),
    )


def _resolved_outcome_policy(
    *,
    profile_hash: str,
    artifact_kind: ArtifactKind,
    payload_schema_id: str,
) -> ResolvedPolicySnapshotV1:
    requirement = ResolvedArtifactRequirementV1(
        requirement_id="regression",
        outcome_rule_id="regression-passed",
        artifact_kind=artifact_kind,
        payload_schema_id=payload_schema_id,
        ordinal=1,
    )
    fields = {
        "resolved_policy_id": "patch-validation",
        "source_profile_field_path": "/params/validation_policy",
        "source_profile_payload_hash": profile_hash,
        "requirements": (requirement,),
    }
    return ResolvedPolicySnapshotV1(
        **fields,
        digest=resolved_policy_snapshot_digest(fields),
    )


def _decode_oracle_evidence(
    payload_schema_id: str, payload: dict[str, Any]
) -> OracleEvidenceClaims:
    assert payload_schema_id
    return OracleEvidenceClaims.model_validate(payload)


def _decode_outcome_evidence(
    payload_schema_id: str, payload: dict[str, Any]
) -> QualifiedOutcomeEvidenceClaims:
    assert payload_schema_id
    return QualifiedOutcomeEvidenceClaims.model_validate(payload)


def _build_case(
    *,
    patch_operation_kinds: tuple[str, ...] = ("add_relation",),
    allowed_operation_kinds: tuple[str, ...] = ("add_relation",),
    maximum_operation_count: int = 1,
    affected_domain_ids: tuple[str, ...] = ("structural",),
    known_domain_ids: tuple[str, ...] = ("structural", "numeric", "narrative"),
    allowed_domain_scope_ids: tuple[tuple[str, ...], ...] = (("structural",),),
    forbidden_domain_scope_ids: tuple[tuple[str, ...], ...] = (),
    allowed_ref_names: tuple[str, ...] = ("content/head",),
    expected_ref: RefValue | None = _CURRENT_REF,
    numeric_value_changed: bool = False,
    narrative_text_changed: bool = False,
    field_classification_complete: bool = True,
    oracle_supported_scope: DomainScope | str = "all",
    oracle_artifact_kind: ArtifactKind = "checker_run",
    oracle_payload_schema_id: str = "checker-evidence@1",
    allowed_oracle_artifact_kinds: tuple[ArtifactKind, ...] = ("checker_run",),
    allowed_oracle_payload_schema_ids: tuple[str, ...] = ("checker-evidence@1",),
    include_unrelated_oracle: bool = False,
    oracle_claim_overrides: dict[str, Any] | None = None,
    oracle_lineage: LiteralLineage = "subject_target",
    outcome_artifact_kind: ArtifactKind = "regression_evidence",
    outcome_payload_schema_id: str = "regression-evidence@1",
    required_outcome_artifact_kind: ArtifactKind = "regression_evidence",
    required_outcome_payload_schema_id: str = "regression-evidence@1",
    include_oracle_binding: bool = True,
    include_outcome_binding: bool = True,
    ownership_complete: bool = True,
    outcome_claim_overrides: dict[str, Any] | None = None,
    extra_evidence_set_parent: str | None = None,
    extra_proof_parent: str | None = None,
) -> dict[str, Any]:
    domain_registry = _domain_registry(
        known_domain_ids,
        ownership_complete=ownership_complete,
    )
    affected_scope = DomainScope(domain_ids=affected_domain_ids)
    oracle_registry = _oracle_registry(
        domain_registry=domain_registry,
        supported_scope=oracle_supported_scope,
        artifact_kinds=allowed_oracle_artifact_kinds,
        payload_schema_ids=allowed_oracle_payload_schema_ids,
        include_unrelated_definition=include_unrelated_oracle,
    )
    policy_registry = _policy_registry(
        domain_registry=domain_registry,
        oracle_registry=oracle_registry,
        allowed_operation_kinds=allowed_operation_kinds,
        maximum_operation_count=maximum_operation_count,
        allowed_domain_scopes=tuple(
            DomainScope(domain_ids=ids) for ids in allowed_domain_scope_ids
        ),
        forbidden_domain_scopes=tuple(
            DomainScope(domain_ids=ids) for ids in forbidden_domain_scope_ids
        ),
        allowed_ref_names=allowed_ref_names,
    )
    policy_ref = _policy_ref(policy_registry)
    validation_profile = _validation_profile(
        policy_ref=policy_ref,
        domain_scope=affected_scope,
    )
    validation_profile_hash = execution_profile_payload_hash(validation_profile)
    outcome_policy = _resolved_outcome_policy(
        profile_hash=validation_profile_hash,
        artifact_kind=required_outcome_artifact_kind,
        payload_schema_id=required_outcome_payload_schema_id,
    )

    patch = PatchV2(
        revision=1,
        base_snapshot_id=_BASE_SNAPSHOT_ID,
        target_snapshot_id=_TARGET_SNAPSHOT_ID,
        expected_to_fix=["finding:1"],
        preconditions=[],
        side_effect_risk="low",
        ops=[
            TypedOp(
                op_id=f"op:{index}",
                op=operation_kind,
                target=f"relation:{index}",
                new_value={"kind": "unblocks"},
            )
            for index, operation_kind in enumerate(patch_operation_kinds, start=1)
        ],
        produced_by="human",
        producer_run_id=None,
        rationale="deterministic structural repair",
    )
    subject = _artifact_payload(
        kind="patch",
        payload_schema_id="patch@2",
        payload=patch,
        lineage=(_BASE_ARTIFACT_ID,),
        version_tuple=VersionTuple(ir_snapshot_id=_BASE_SNAPSHOT_ID, tool_version="patch@2"),
    )
    target = _artifact_payload(
        kind="ir_snapshot",
        payload_schema_id="ir-snapshot@1",
        payload={"snapshot_schema_version": "ir-snapshot@1", "id": _TARGET_SNAPSHOT_ID},
        lineage=(_BASE_ARTIFACT_ID, subject.artifact.artifact_id),
        version_tuple=VersionTuple(
            ir_snapshot_id=_TARGET_SNAPSHOT_ID,
            tool_version="preview@1",
        ),
    )
    target_binding = PatchTargetBindingV1(
        target_artifact_id=target.artifact.artifact_id,
        target_snapshot_id=_TARGET_SNAPSHOT_ID,
        target_digest=target.artifact.payload_hash,
        ref_name="content/head",
        expected_ref=expected_ref,
    )

    oracle_ref = policy_registry.policies[0].required_deterministic_oracles[0]
    oracle_parent_ids = {
        "subject_target": (subject.artifact.artifact_id, target.artifact.artifact_id),
        "subject_only": (subject.artifact.artifact_id,),
    }[oracle_lineage]
    oracle_claims: dict[str, Any] = {
        "oracle": oracle_ref.model_dump(mode="json"),
        "subject_artifact_id": subject.artifact.artifact_id,
        "subject_digest": subject.artifact.payload_hash,
        "target_binding": target_binding.model_dump(mode="json"),
        "evaluated_domain_scope": affected_scope.model_dump(mode="json"),
        "predicate_schema_id": "structural-predicate@1",
        "predicate": {"kind": "no_dangling_reference"},
        "verdict": "passed",
        "verdict_authority": "deterministic",
        "direct_parent_artifact_ids": oracle_parent_ids,
    }
    oracle_claims.update(oracle_claim_overrides or {})
    oracle_evidence = _artifact_payload(
        kind=oracle_artifact_kind,
        payload_schema_id=oracle_payload_schema_id,
        payload=oracle_claims,
        lineage=oracle_parent_ids,
    )
    outcome_claims: dict[str, Any] = {
        "rule": policy_registry.policies[0].required_outcome_rules[0].model_dump(mode="json"),
        "requirement_id": "regression",
        "subject_artifact_id": subject.artifact.artifact_id,
        "subject_digest": subject.artifact.payload_hash,
        "target_binding": target_binding.model_dump(mode="json"),
        "evaluated_domain_scope": affected_scope.model_dump(mode="json"),
        "verdict": "passed",
        "verdict_authority": "deterministic",
        "direct_parent_artifact_ids": (
            subject.artifact.artifact_id,
            target.artifact.artifact_id,
        ),
    }
    outcome_claims.update(outcome_claim_overrides or {})
    outcome_evidence = _artifact_payload(
        kind=outcome_artifact_kind,
        payload_schema_id=outcome_payload_schema_id,
        payload=outcome_claims,
        lineage=(subject.artifact.artifact_id, target.artifact.artifact_id),
    )

    evidence_set = EvidenceSet(
        subject_artifact_id=subject.artifact.artifact_id,
        subject_digest=subject.artifact.payload_hash,
        policy_version="patch-validation@1",
        validation_run_id="run:validation",
        target_binding=target_binding,
        supporting_artifact_ids=(),
        finding_bindings=(),
        requirements=(
            EvidenceRequirement(
                requirement_id="oracle.graph",
                kind="deterministic_checker",
                applicability="required",
                status="passed",
                evidence_artifact_id=oracle_evidence.artifact.artifact_id,
                tool_version="checker@1",
            ),
            EvidenceRequirement(
                requirement_id="regression",
                kind="regression",
                applicability="required",
                status="passed",
                evidence_artifact_id=outcome_evidence.artifact.artifact_id,
                tool_version="regression@1",
            ),
        ),
        overall_status="passed",
    )
    evidence_set_lineage = (
        subject.artifact.artifact_id,
        target.artifact.artifact_id,
        oracle_evidence.artifact.artifact_id,
        outcome_evidence.artifact.artifact_id,
    )
    if extra_evidence_set_parent is not None:
        evidence_set_lineage += (extra_evidence_set_parent,)
    evidence_set_artifact = _artifact_payload(
        kind="validation_evidence",
        payload_schema_id="evidence-set@1",
        payload=evidence_set,
        lineage=evidence_set_lineage,
    )

    oracle_bindings = (
        (
            AutoApplyOracleEvidenceBindingV1(
                oracle=oracle_ref,
                evaluated_domain_scope=affected_scope,
                evidence_artifact_id=oracle_evidence.artifact.artifact_id,
                evidence_payload_hash=oracle_evidence.artifact.payload_hash,
            ),
        )
        if include_oracle_binding
        else ()
    )
    outcome_bindings = (
        (
            AutoApplyOutcomeEvidenceBindingV1(
                rule=policy_registry.policies[0].required_outcome_rules[0],
                requirement_id="regression",
                evidence_artifact_id=outcome_evidence.artifact.artifact_id,
                evidence_payload_hash=outcome_evidence.artifact.payload_hash,
            ),
        )
        if include_outcome_binding
        else ()
    )
    proof_payload = AutoApplyProofV1(
        subject_artifact_id=subject.artifact.artifact_id,
        subject_digest=subject.artifact.payload_hash,
        target_binding=target_binding,
        affected_domain_scope=affected_scope,
        validation_evidence_artifact_id=evidence_set_artifact.artifact.artifact_id,
        regression_evidence_artifact_ids=(outcome_evidence.artifact.artifact_id,),
        validation_profile_binding=AutoApplyValidationProfileBindingV1(
            validation_profile=validation_profile.profile,
            validation_profile_payload_hash=validation_profile_hash,
            policy=policy_ref,
        ),
        deterministic_oracle_evidence=oracle_bindings,
        required_outcome_evidence=outcome_bindings,
        policy=policy_ref,
    )
    proof_lineage = (
        subject.artifact.artifact_id,
        target.artifact.artifact_id,
        evidence_set_artifact.artifact.artifact_id,
        oracle_evidence.artifact.artifact_id,
        outcome_evidence.artifact.artifact_id,
    )
    if extra_proof_parent is not None:
        proof_lineage += (extra_proof_parent,)
    proof_artifact = _artifact_payload(
        kind="validation_evidence",
        payload_schema_id="auto-apply-proof@1",
        payload=proof_payload,
        lineage=proof_lineage,
    )
    proof_binding = AutoApplyProofBindingV1(
        proof_artifact_id=proof_artifact.artifact.artifact_id,
        policy=policy_ref,
        subject_digest=subject.artifact.payload_hash,
        target_digest=target.artifact.payload_hash,
        expected_ref=expected_ref,
        validation_evidence_artifact_id=evidence_set_artifact.artifact.artifact_id,
    )
    item = ApprovalItem(
        approval_id="approval:patch:1",
        subject_series_id="patch-series:1",
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id=subject.artifact.artifact_id,
        subject_digest=subject.artifact.payload_hash,
        status="validated",
        workflow_revision=3,
        proposer=AuditActor(principal_id="human:alice", principal_kind="human"),
        domain_scope=affected_scope,
        domain_registry_ref=_domain_ref(domain_registry),
        route_policy=DomainRoutePolicyRefV1(
            route_version="routes@1",
            route_digest="2" * 64,
            domain_registry_ref=_domain_ref(domain_registry),
        ),
        role_policy_version="roles@1",
        role_policy_digest="3" * 64,
        approval_policy=ApprovalPolicyRefV1(
            policy_version="approval@1",
            policy_digest="4" * 64,
        ),
        requirements=(),
        decisions=(),
        evidence_set_artifact_id=evidence_set_artifact.artifact.artifact_id,
        regression_evidence_artifact_ids=(outcome_evidence.artifact.artifact_id,),
        target_binding=target_binding,
        auto_apply_proof=proof_binding,
        created_at="2026-07-14T00:00:00Z",
    )
    classifier = auto_apply_ir_classifier_binding(domain_registry)
    assessment = AutoApplyChangeAssessment(
        base_artifact_id=_BASE_ARTIFACT_ID,
        base_snapshot_id=_BASE_SNAPSHOT_ID,
        subject_artifact_id=subject.artifact.artifact_id,
        subject_digest=subject.artifact.payload_hash,
        target_artifact_id=target.artifact.artifact_id,
        target_snapshot_id=_TARGET_SNAPSHOT_ID,
        target_digest=target.artifact.payload_hash,
        target_payload_schema_id="ir-snapshot@1",
        schema_id=classifier.classifier_schema_id,
        schema_digest=classifier.classifier_schema_digest,
        affected_domain_scope=affected_scope,
        field_classification_complete=field_classification_complete,
        numeric_value_changed=numeric_value_changed,
        narrative_text_changed=narrative_text_changed,
    )
    return {
        "outcome_code": "patch_validation_auto_eligible",
        "item": item,
        "subject": subject,
        "target": target,
        "proof": proof_artifact,
        "evidence_set": evidence_set_artifact,
        "evidence_artifacts": (oracle_evidence, outcome_evidence),
        "domain_registry": domain_registry,
        "policy_registry": policy_registry,
        "oracle_registry": oracle_registry,
        "validation_profile": validation_profile,
        "resolved_outcome_policies": (outcome_policy,),
        "change_assessment": assessment,
        "current_ref": expected_ref,
        "oracle_evidence_decoder": _decode_oracle_evidence,
        "outcome_evidence_decoder": _decode_outcome_evidence,
    }


def _assert_rejected(case: dict[str, Any], reason_code: str) -> None:
    with pytest.raises(AutoApplyGuardError) as raised:
        validate_auto_apply(**case)
    assert raised.value.code == "auto_apply_guard_rejected"
    assert raised.value.context["reason_code"] == reason_code


def _candidate_args(case: dict[str, Any]) -> dict[str, Any]:
    target_binding = case["item"].target_binding
    proof_binding = case["item"].auto_apply_proof
    assert isinstance(target_binding, PatchTargetBindingV1)
    assert proof_binding is not None
    return {
        "subject": case["subject"],
        "target": case["target"],
        "target_binding": target_binding,
        "policy_ref": proof_binding.policy,
        "domain_registry": case["domain_registry"],
        "policy_registry": case["policy_registry"],
        "oracle_registry": case["oracle_registry"],
        "validation_profile": case["validation_profile"],
        "change_assessment": case["change_assessment"],
        "current_ref": case["current_ref"],
    }


def _with_additional_outcome_requirement(
    case: dict[str, Any],
    *,
    requirement_id: str,
    include_proof_binding: bool = True,
) -> dict[str, Any]:
    subject = case["subject"]
    target = case["target"]
    target_binding = case["item"].target_binding
    assert isinstance(target_binding, PatchTargetBindingV1)
    rule = case["policy_registry"].policies[0].required_outcome_rules[0]

    retained_snapshot = case["resolved_outcome_policies"][0]
    additional_requirement = ResolvedArtifactRequirementV1(
        requirement_id=requirement_id,
        outcome_rule_id=rule.outcome_rule_id,
        artifact_kind="regression_evidence",
        payload_schema_id="regression-evidence@1",
        ordinal=2,
    )
    snapshot_fields = {
        "resolved_policy_id": retained_snapshot.resolved_policy_id,
        "source_profile_field_path": retained_snapshot.source_profile_field_path,
        "source_profile_payload_hash": retained_snapshot.source_profile_payload_hash,
        "requirements": (*retained_snapshot.requirements, additional_requirement),
    }
    expanded_snapshot = ResolvedPolicySnapshotV1(
        **snapshot_fields,
        digest=resolved_policy_snapshot_digest(snapshot_fields),
    )

    claims = QualifiedOutcomeEvidenceClaims(
        rule=rule,
        requirement_id=requirement_id,
        subject_artifact_id=subject.artifact.artifact_id,
        subject_digest=subject.artifact.payload_hash,
        target_binding=target_binding,
        evaluated_domain_scope=case["change_assessment"].affected_domain_scope,
        verdict="passed",
        verdict_authority="deterministic",
        direct_parent_artifact_ids=(
            subject.artifact.artifact_id,
            target.artifact.artifact_id,
        ),
    )
    additional_evidence = _artifact_payload(
        kind="regression_evidence",
        payload_schema_id="regression-evidence@1",
        payload=claims,
        lineage=(subject.artifact.artifact_id, target.artifact.artifact_id),
    )

    evidence_set_payload = EvidenceSet.model_validate(
        json.loads(case["evidence_set"].payload_bytes)
    )
    evidence_set_payload = EvidenceSet.model_validate(
        {
            **evidence_set_payload.model_dump(mode="python"),
            "requirements": (
                *evidence_set_payload.requirements,
                EvidenceRequirement(
                    requirement_id=requirement_id,
                    kind="regression",
                    applicability="required",
                    status="passed",
                    evidence_artifact_id=additional_evidence.artifact.artifact_id,
                    tool_version="regression@1",
                ),
            ),
        }
    )
    evidence_records = (*case["evidence_artifacts"], additional_evidence)
    expanded_evidence_set = _artifact_payload(
        kind="validation_evidence",
        payload_schema_id="evidence-set@1",
        payload=evidence_set_payload,
        lineage=(
            subject.artifact.artifact_id,
            target.artifact.artifact_id,
            *(record.artifact.artifact_id for record in evidence_records),
        ),
    )

    proof_payload = AutoApplyProofV1.model_validate(json.loads(case["proof"].payload_bytes))
    additional_binding = AutoApplyOutcomeEvidenceBindingV1(
        rule=rule,
        requirement_id=requirement_id,
        evidence_artifact_id=additional_evidence.artifact.artifact_id,
        evidence_payload_hash=additional_evidence.artifact.payload_hash,
    )
    outcome_bindings = proof_payload.required_outcome_evidence
    if include_proof_binding:
        outcome_bindings = (*outcome_bindings, additional_binding)
    proof_payload = AutoApplyProofV1.model_validate(
        {
            **proof_payload.model_dump(mode="python"),
            "validation_evidence_artifact_id": expanded_evidence_set.artifact.artifact_id,
            "regression_evidence_artifact_ids": (
                *proof_payload.regression_evidence_artifact_ids,
                additional_evidence.artifact.artifact_id,
            ),
            "required_outcome_evidence": outcome_bindings,
        }
    )
    expanded_proof = _artifact_payload(
        kind="validation_evidence",
        payload_schema_id="auto-apply-proof@1",
        payload=proof_payload,
        lineage=(
            subject.artifact.artifact_id,
            target.artifact.artifact_id,
            expanded_evidence_set.artifact.artifact_id,
            *(record.artifact.artifact_id for record in evidence_records),
        ),
    )

    old_proof_binding = case["item"].auto_apply_proof
    assert old_proof_binding is not None
    expanded_proof_binding = AutoApplyProofBindingV1(
        proof_artifact_id=expanded_proof.artifact.artifact_id,
        policy=old_proof_binding.policy,
        subject_digest=old_proof_binding.subject_digest,
        target_digest=old_proof_binding.target_digest,
        expected_ref=old_proof_binding.expected_ref,
        validation_evidence_artifact_id=expanded_evidence_set.artifact.artifact_id,
    )
    expanded_item = ApprovalItem.model_validate(
        {
            **case["item"].model_dump(mode="python"),
            "evidence_set_artifact_id": expanded_evidence_set.artifact.artifact_id,
            "regression_evidence_artifact_ids": proof_payload.regression_evidence_artifact_ids,
            "auto_apply_proof": expanded_proof_binding,
        }
    )
    return {
        **case,
        "item": expanded_item,
        "proof": expanded_proof,
        "evidence_set": expanded_evidence_set,
        "evidence_artifacts": evidence_records,
        "resolved_outcome_policies": (expanded_snapshot,),
    }


def test_auto_apply_accepts_one_exactly_closed_deterministic_patch_proof() -> None:
    validate_auto_apply(**_build_case())


def test_auto_apply_expands_one_required_rule_to_all_resolved_requirements() -> None:
    case = _with_additional_outcome_requirement(
        _build_case(),
        requirement_id="alpha-regression",
    )

    proof = AutoApplyProofV1.model_validate(json.loads(case["proof"].payload_bytes))
    assert [
        (binding.rule.outcome_rule_id, binding.requirement_id)
        for binding in proof.required_outcome_evidence
    ] == [
        ("regression-passed", "alpha-regression"),
        ("regression-passed", "regression"),
    ]
    validate_auto_apply(**case)


def test_auto_apply_rejects_one_missing_requirement_from_a_required_rule() -> None:
    case = _with_additional_outcome_requirement(
        _build_case(),
        requirement_id="alpha-regression",
        include_proof_binding=False,
    )

    _assert_rejected(case, "required_outcome_set_mismatch")


def test_auto_apply_validates_each_artifact_from_an_expanded_required_rule() -> None:
    case = _with_additional_outcome_requirement(
        _build_case(),
        requirement_id="alpha-regression",
    )
    additional = case["evidence_artifacts"][-1]
    case["evidence_artifacts"] = (
        *case["evidence_artifacts"][:-1],
        replace(additional, payload_schema_id="wrong-regression@1"),
    )

    _assert_rejected(case, "outcome_artifact_schema_mismatch")


def test_candidate_eligibility_reuses_the_exact_prepublication_policy_guard() -> None:
    case = _build_case()
    assert is_auto_apply_candidate_eligible(**_candidate_args(case))

    forbidden = _build_case(allowed_operation_kinds=("delete_relation",))
    assert not is_auto_apply_candidate_eligible(**_candidate_args(forbidden))

    stale_ref = _candidate_args(_build_case())
    stale_ref["current_ref"] = RefValue(artifact_id="artifact:stale", revision=8)
    assert not is_auto_apply_candidate_eligible(**stale_ref)


def test_candidate_eligibility_rejects_forged_classifier_authority() -> None:
    case = _build_case()
    candidate = _candidate_args(case)
    candidate["change_assessment"] = replace(
        candidate["change_assessment"],
        schema_digest="0" * 64,
    )

    with pytest.raises(AutoApplyGuardError) as raised:
        is_auto_apply_candidate_eligible(**candidate)

    assert raised.value.context["reason_code"] == "change_assessment_mismatch"


def test_candidate_eligibility_rejects_incomplete_multi_domain_ownership() -> None:
    case = _build_case(ownership_complete=False)

    with pytest.raises(AutoApplyGuardError) as raised:
        is_auto_apply_candidate_eligible(**_candidate_args(case))

    assert raised.value.context["reason_code"] == "domain_ownership_incomplete"


@pytest.mark.parametrize("authority", ["policy", "profile", "assessment", "payload"])
def test_candidate_eligibility_fails_typed_on_malformed_authority(authority: str) -> None:
    case = _build_case()
    candidate = _candidate_args(case)
    expected_reason = {
        "policy": "policy_registry_mismatch",
        "profile": "validation_profile_mismatch",
        "assessment": "change_assessment_mismatch",
        "payload": "artifact_payload_hash_mismatch",
    }[authority]
    if authority == "policy":
        old = candidate["policy_registry"]
        candidate["policy_registry"] = AutoApplyPolicyRegistryV1(
            registry_version="auto@other",
            policies=old.policies,
            registry_digest=compute_auto_apply_policy_registry_digest("auto@other", old.policies),
        )
    elif authority == "profile":
        profile = candidate["validation_profile"]
        details = profile.details
        assert isinstance(details, ValidationProfileDetailsV1)
        candidate["validation_profile"] = profile.model_copy(
            update={"details": details.model_copy(update={"auto_apply_policy": None})}
        )
    elif authority == "assessment":
        candidate["change_assessment"] = replace(
            candidate["change_assessment"],
            target_snapshot_id="sha256:other-target",
        )
    else:
        subject = candidate["subject"]
        candidate["subject"] = replace(
            subject,
            payload_bytes=subject.payload_bytes + b" ",
        )

    with pytest.raises(AutoApplyGuardError) as raised:
        is_auto_apply_candidate_eligible(**candidate)
    assert raised.value.context["reason_code"] == expected_reason


@pytest.mark.parametrize(
    "outcome_code",
    [
        "patch_validation_passed",
        "patch_validation_failed",
        "patch_validation_unproven",
        "execution_failed",
    ],
)
def test_auto_apply_is_exclusive_to_the_dedicated_patch_outcome(
    outcome_code: str,
) -> None:
    case = _build_case()
    case["outcome_code"] = outcome_code
    _assert_rejected(case, "outcome_not_auto_eligible")


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        (
            {"maximum_operation_count": 1, "patch_operation_kinds": ("add_relation",) * 2},
            "operation_count_exceeded",
        ),
        ({"allowed_operation_kinds": ("delete_relation",)}, "operation_kind_forbidden"),
    ],
)
def test_auto_apply_enforces_operation_count_and_kind(
    updates: dict[str, Any], reason_code: str
) -> None:
    _assert_rejected(_build_case(**updates), reason_code)


def test_auto_apply_requires_the_current_ref_to_equal_the_frozen_cas_value() -> None:
    case = _build_case()
    case["current_ref"] = RefValue(artifact_id="artifact:other", revision=8)
    _assert_rejected(case, "ref_binding_mismatch")


def test_auto_apply_requires_the_frozen_ref_to_point_at_the_patch_base() -> None:
    mismatched_ref = RefValue(artifact_id="artifact:other-base", revision=7)
    _assert_rejected(
        _build_case(expected_ref=mismatched_ref),
        "ref_base_mismatch",
    )


def test_auto_apply_requires_the_target_ref_name_to_be_allowlisted() -> None:
    _assert_rejected(
        _build_case(allowed_ref_names=("content/staging",)),
        "ref_name_forbidden",
    )


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        (
            {
                "affected_domain_ids": ("structural", "numeric"),
                "allowed_domain_scope_ids": (("structural",),),
                "forbidden_domain_scope_ids": (("numeric",),),
            },
            "forbidden_domain_affected",
        ),
        (
            {
                "affected_domain_ids": ("unregistered",),
                "known_domain_ids": ("structural",),
                "allowed_domain_scope_ids": (("unregistered",),),
            },
            "unknown_domain",
        ),
    ],
)
def test_auto_apply_domain_scope_is_exact_registered_allowed_and_not_forbidden(
    updates: dict[str, Any], reason_code: str
) -> None:
    _assert_rejected(_build_case(**updates), reason_code)


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        ({"field_classification_complete": False}, "field_classification_incomplete"),
        ({"numeric_value_changed": True}, "numeric_change_forbidden"),
        ({"narrative_text_changed": True}, "narrative_change_forbidden"),
    ],
)
def test_auto_apply_fails_closed_on_unknown_numeric_or_narrative_changes(
    updates: dict[str, Any], reason_code: str
) -> None:
    _assert_rejected(_build_case(**updates), reason_code)


def test_auto_apply_binds_the_exact_historical_policy_registry() -> None:
    case = _build_case()
    old = case["policy_registry"]
    case["policy_registry"] = AutoApplyPolicyRegistryV1(
        registry_version="auto@other",
        policies=old.policies,
        registry_digest=compute_auto_apply_policy_registry_digest("auto@other", old.policies),
    )
    _assert_rejected(case, "policy_registry_mismatch")


def test_auto_apply_binds_the_exact_validation_profile_payload() -> None:
    case = _build_case()
    profile = case["validation_profile"]
    case["validation_profile"] = profile.model_copy(
        update={"profile": ProfileRefV1(profile_id="patch.validate.other", version=1)}
    )
    _assert_rejected(case, "validation_profile_mismatch")


@pytest.mark.parametrize(
    ("source_update", "reason_code"),
    [
        (
            {"source_profile_payload_hash": "f" * 64},
            "outcome_policy_profile_mismatch",
        ),
        (
            {"source_profile_field_path": "/params/other_validation_policy"},
            "outcome_policy_profile_mismatch",
        ),
    ],
)
def test_auto_apply_outcome_policy_binds_the_exact_validation_profile_source(
    source_update: dict[str, Any], reason_code: str
) -> None:
    case = _build_case()
    retained = case["resolved_outcome_policies"][0]
    fields = {
        "resolved_policy_id": retained.resolved_policy_id,
        "source_profile_field_path": retained.source_profile_field_path,
        "source_profile_payload_hash": retained.source_profile_payload_hash,
        "requirements": retained.requirements,
        **source_update,
    }
    case["resolved_outcome_policies"] = (
        ResolvedPolicySnapshotV1(
            **fields,
            digest=resolved_policy_snapshot_digest(fields),
        ),
    )
    _assert_rejected(case, reason_code)


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        ({"include_oracle_binding": False}, "required_oracle_set_mismatch"),
        ({"include_outcome_binding": False}, "required_outcome_set_mismatch"),
    ],
)
def test_auto_apply_requires_exact_oracle_and_outcome_sets(
    updates: dict[str, Any], reason_code: str
) -> None:
    _assert_rejected(_build_case(**updates), reason_code)


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        ({"oracle_artifact_kind": "review_report"}, "oracle_artifact_kind_forbidden"),
        ({"oracle_payload_schema_id": "other-evidence@1"}, "oracle_evidence_schema_forbidden"),
        (
            {"oracle_claim_overrides": {"predicate_schema_id": "other-predicate@1"}},
            "oracle_predicate_mismatch",
        ),
        (
            {"oracle_claim_overrides": {"verdict": "unproven"}},
            "oracle_verdict_not_passed",
        ),
        (
            {"oracle_claim_overrides": {"verdict_authority": "llm"}},
            "oracle_verdict_not_deterministic",
        ),
        ({"oracle_lineage": "subject_only"}, "oracle_lineage_mismatch"),
    ],
)
def test_auto_apply_closes_oracle_schema_hash_lineage_predicate_and_verdict(
    updates: dict[str, Any], reason_code: str
) -> None:
    _assert_rejected(_build_case(**updates), reason_code)


def test_auto_apply_requires_every_oracle_to_cover_the_full_affected_scope() -> None:
    _assert_rejected(
        _build_case(
            oracle_supported_scope=DomainScope(domain_ids=("numeric",)),
        ),
        "oracle_scope_not_covered",
    )


def test_unrequired_historical_oracle_from_another_registry_does_not_block() -> None:
    validate_auto_apply(**_build_case(include_unrelated_oracle=True))


def test_auto_apply_rejects_evidence_bytes_that_do_not_match_the_artifact_hash() -> None:
    case = _build_case()
    oracle, outcome = case["evidence_artifacts"]
    case["evidence_artifacts"] = (
        replace(oracle, payload_bytes=oracle.payload_bytes + b" "),
        outcome,
    )
    _assert_rejected(case, "artifact_payload_hash_mismatch")


@pytest.mark.parametrize(
    ("record_update", "reason_code"),
    [
        ({"payload_schema_id": "wrong-regression@1"}, "outcome_artifact_schema_mismatch"),
    ],
)
def test_auto_apply_outcome_artifact_matches_resolved_kind_schema_and_requirement(
    record_update: dict[str, Any], reason_code: str
) -> None:
    case = _build_case()
    oracle, outcome = case["evidence_artifacts"]
    case["evidence_artifacts"] = (oracle, replace(outcome, **record_update))
    _assert_rejected(case, reason_code)


def test_auto_apply_outcome_artifact_kind_is_exact() -> None:
    _assert_rejected(
        _build_case(outcome_artifact_kind="review_report"),
        "outcome_artifact_kind_mismatch",
    )


@pytest.mark.parametrize(
    ("outcome_claim_overrides", "reason_code"),
    [
        (
            {
                "rule": QualifiedOutcomeRuleRefV1(
                    resolved_policy_id="another-policy",
                    outcome_rule_id="regression-passed",
                ).model_dump(mode="json")
            },
            "outcome_evidence_binding_mismatch",
        ),
        ({"requirement_id": "another-requirement"}, "outcome_evidence_binding_mismatch"),
        ({"subject_digest": "f" * 64}, "outcome_evidence_binding_mismatch"),
        (
            {
                "target_binding": PatchTargetBindingV1(
                    target_artifact_id="artifact:other-target",
                    target_snapshot_id="sha256:other-target",
                    target_digest="e" * 64,
                    ref_name="content/head",
                    expected_ref=_CURRENT_REF,
                ).model_dump(mode="json")
            },
            "outcome_evidence_binding_mismatch",
        ),
        (
            {
                "evaluated_domain_scope": DomainScope(domain_ids=("numeric",)).model_dump(
                    mode="json"
                )
            },
            "outcome_evidence_binding_mismatch",
        ),
        ({"verdict": "failed"}, "outcome_verdict_not_passed"),
        ({"verdict_authority": "llm"}, "outcome_verdict_not_deterministic"),
        (
            {"direct_parent_artifact_ids": (_BASE_ARTIFACT_ID,)},
            "outcome_lineage_mismatch",
        ),
    ],
)
def test_auto_apply_closes_outcome_payload_binding_verdict_and_lineage(
    outcome_claim_overrides: dict[str, Any], reason_code: str
) -> None:
    _assert_rejected(
        _build_case(outcome_claim_overrides=outcome_claim_overrides),
        reason_code,
    )


@pytest.mark.parametrize(
    ("record_name", "schema_id", "reason_code"),
    [
        ("proof", "validation-proof-other@1", "proof_artifact_mismatch"),
        ("evidence_set", "evidence-set-other@1", "evidence_set_artifact_mismatch"),
    ],
)
def test_auto_apply_proof_and_evidence_set_schema_ids_are_exact(
    record_name: str,
    schema_id: str,
    reason_code: str,
) -> None:
    case = _build_case()
    case[record_name] = replace(case[record_name], payload_schema_id=schema_id)
    _assert_rejected(case, reason_code)


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        ({"extra_evidence_set_parent": "artifact:unbound"}, "evidence_set_lineage_mismatch"),
        ({"extra_proof_parent": "artifact:unbound"}, "proof_lineage_mismatch"),
    ],
)
def test_auto_apply_evidence_set_and_proof_have_exact_direct_lineage(
    updates: dict[str, Any], reason_code: str
) -> None:
    _assert_rejected(_build_case(**updates), reason_code)


def test_auto_apply_change_assessment_is_bound_to_the_exact_subject_and_target() -> None:
    case = _build_case()
    case["change_assessment"] = replace(
        case["change_assessment"],
        target_snapshot_id="sha256:other-target",
    )
    _assert_rejected(case, "change_assessment_mismatch")


def test_auto_apply_target_payload_schema_is_exact() -> None:
    case = _build_case()
    case["target"] = replace(case["target"], payload_schema_id="ir-snapshot@2")
    _assert_rejected(case, "target_schema_mismatch")
