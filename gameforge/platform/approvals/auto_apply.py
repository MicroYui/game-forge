"""Pure fail-closed validation for the deterministic Patch auto-apply gate."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from gameforge.contracts.canonical import sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    RunKindRef,
    ValidationProfileDetailsV1,
    execution_profile_payload_hash,
)
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
)
from gameforge.contracts.jobs import ResolvedArtifactRequirementV1, ResolvedPolicySnapshotV1
from gameforge.contracts.lineage import ArtifactV2
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyPolicyRegistryV1,
    AutoApplyPolicyV1,
    AutoApplyProofV1,
    DeterministicOracleDefinitionV1,
    DeterministicOracleRefV1,
    DeterministicOracleRegistryRefV1,
    DeterministicOracleRegistryV1,
    EvidenceRequirement,
    EvidenceSet,
    PatchTargetBindingV1,
    QualifiedOutcomeRuleRefV1,
    compute_auto_apply_policy_digest,
)


AUTO_APPLY_OUTCOME_CODE = "patch_validation_auto_eligible"


class AutoApplyGuardError(IntegrityViolation):
    """Stable typed failure for every closed auto-apply gate."""

    code = "auto_apply_guard_rejected"


@dataclass(frozen=True, slots=True)
class ResolvedArtifactPayload:
    """An immutable Artifact plus exact publication schema and stored bytes."""

    artifact: ArtifactV2
    payload_schema_id: str
    payload_bytes: bytes


@dataclass(frozen=True, slots=True)
class AutoApplyChangeAssessment:
    """Schema-aware facts recomputed from the exact base-to-target canonical diff."""

    base_artifact_id: str
    base_snapshot_id: str
    subject_artifact_id: str
    subject_digest: str
    target_artifact_id: str
    target_snapshot_id: str
    target_digest: str
    target_payload_schema_id: str
    schema_id: str
    schema_digest: str
    affected_domain_scope: DomainScope
    field_classification_complete: bool
    numeric_value_changed: bool
    narrative_text_changed: bool


class OracleEvidenceClaims(BaseModel):
    """Schema-reader projection required from every qualified oracle evidence payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    oracle: DeterministicOracleRefV1
    subject_artifact_id: str = Field(min_length=1)
    subject_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_binding: PatchTargetBindingV1
    evaluated_domain_scope: DomainScope
    predicate_schema_id: str = Field(min_length=1)
    predicate: dict[str, JsonValue] = Field(min_length=1)
    verdict: Literal["passed", "failed", "unproven"]
    verdict_authority: Literal["deterministic", "llm", "mixed", "human"]
    direct_parent_artifact_ids: tuple[str, ...]

    @field_validator("direct_parent_artifact_ids")
    @classmethod
    def _canonical_parents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not parent for parent in value):
            raise ValueError("oracle evidence parent IDs must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("oracle evidence parent IDs must be unique")
        return tuple(sorted(value))


OracleEvidenceDecoder = Callable[[str, dict[str, Any]], OracleEvidenceClaims]


class QualifiedOutcomeEvidenceClaims(BaseModel):
    """Schema-reader projection for one qualified deterministic outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule: QualifiedOutcomeRuleRefV1
    requirement_id: str = Field(min_length=1)
    subject_artifact_id: str = Field(min_length=1)
    subject_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_binding: PatchTargetBindingV1
    evaluated_domain_scope: DomainScope
    verdict: Literal["passed", "failed", "unproven"]
    verdict_authority: Literal["deterministic", "llm", "mixed", "human"]
    direct_parent_artifact_ids: tuple[str, ...]

    @field_validator("direct_parent_artifact_ids")
    @classmethod
    def _canonical_parents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not parent for parent in value):
            raise ValueError("outcome evidence parent IDs must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("outcome evidence parent IDs must be unique")
        return tuple(sorted(value))


OutcomeEvidenceDecoder = Callable[[str, dict[str, Any]], QualifiedOutcomeEvidenceClaims]


def _reject(reason_code: str, detail: str, **context: Any) -> None:
    raise AutoApplyGuardError(detail, reason_code=reason_code, **context)


def _contract_value[T: BaseModel](value: T, contract: type[T], label: str) -> T:
    try:
        return contract.model_validate(value.model_dump(mode="python"))
    except (TypeError, ValueError, ValidationError) as exc:
        _reject("invalid_guard_input", f"{label} violates its frozen contract")
        raise AssertionError("unreachable") from exc


def _strict_json_object(payload_bytes: bytes, *, artifact_id: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload_bytes,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        _reject(
            "artifact_payload_invalid",
            "Artifact payload is not one strict JSON object",
            artifact_id=artifact_id,
        )
        raise AssertionError("unreachable") from exc
    if not isinstance(decoded, dict):
        _reject(
            "artifact_payload_invalid",
            "Artifact payload must be a JSON object",
            artifact_id=artifact_id,
        )
    return decoded


def _load_payload(record: ResolvedArtifactPayload) -> dict[str, Any]:
    if not isinstance(record.payload_schema_id, str) or not record.payload_schema_id:
        _reject("artifact_schema_missing", "Artifact payload schema ID is missing")
    if not isinstance(record.payload_bytes, bytes):
        _reject("artifact_payload_invalid", "Artifact payload must be immutable bytes")
    artifact = _contract_value(record.artifact, ArtifactV2, "ArtifactV2")
    if len(record.payload_bytes) != artifact.object_ref.size_bytes:
        _reject(
            "artifact_payload_hash_mismatch",
            "Artifact payload byte length differs from ObjectRef",
            artifact_id=artifact.artifact_id,
        )
    payload_hash = sha256_lowerhex(record.payload_bytes)
    if payload_hash != artifact.payload_hash or payload_hash != artifact.object_ref.sha256:
        _reject(
            "artifact_payload_hash_mismatch",
            "Artifact payload bytes do not match the content-addressed hash",
            artifact_id=artifact.artifact_id,
        )
    return _strict_json_object(record.payload_bytes, artifact_id=artifact.artifact_id)


def _parse_payload[T: BaseModel](
    record: ResolvedArtifactPayload,
    contract: type[T],
    *,
    reason_code: str,
) -> T:
    payload = _load_payload(record)
    try:
        return contract.model_validate(payload)
    except (TypeError, ValueError, ValidationError) as exc:
        _reject(
            reason_code,
            f"{record.payload_schema_id} payload violates its frozen contract",
            artifact_id=record.artifact.artifact_id,
        )
        raise AssertionError("unreachable") from exc


def _artifact_records(
    records: Sequence[ResolvedArtifactPayload],
) -> tuple[dict[str, ResolvedArtifactPayload], dict[str, dict[str, Any]]]:
    by_id: dict[str, ResolvedArtifactPayload] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for record in records:
        artifact_id = record.artifact.artifact_id
        if artifact_id in by_id:
            _reject(
                "duplicate_evidence_artifact",
                "Evidence Artifact IDs must be unique",
                artifact_id=artifact_id,
            )
        by_id[artifact_id] = record
        payloads[artifact_id] = _load_payload(record)
    return by_id, payloads


def _domain_registry_ref(registry: DomainRegistryV1) -> DomainRegistryRefV1:
    return DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _policy_registry_ref(
    registry: AutoApplyPolicyRegistryV1,
) -> AutoApplyPolicyRegistryRefV1:
    return AutoApplyPolicyRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _oracle_registry_ref(
    registry: DeterministicOracleRegistryV1,
) -> DeterministicOracleRegistryRefV1:
    return DeterministicOracleRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _resolve_policy(
    *,
    policy_ref: AutoApplyPolicyRefV1,
    registry: AutoApplyPolicyRegistryV1,
) -> AutoApplyPolicyV1:
    if policy_ref.registry != _policy_registry_ref(registry):
        _reject(
            "policy_registry_mismatch",
            "Auto-apply proof does not bind the supplied historical policy registry",
        )
    candidates = tuple(
        policy
        for policy in registry.policies
        if (policy.policy_id, policy.policy_version)
        == (policy_ref.policy_id, policy_ref.policy_version)
    )
    if len(candidates) != 1:
        _reject(
            "policy_resolution_failed",
            "Auto-apply policy ref must resolve exactly once",
        )
    policy = candidates[0]
    if compute_auto_apply_policy_digest(policy) != policy_ref.policy_digest:
        _reject(
            "policy_digest_mismatch",
            "Auto-apply policy digest differs from its frozen ref",
        )
    return policy


def _scope_ids(scope: DomainScope) -> set[str]:
    return set(scope.domain_ids)


def _validate_domains(
    *,
    item: ApprovalItem,
    proof: AutoApplyProofV1,
    assessment: AutoApplyChangeAssessment,
    domain_registry: DomainRegistryV1,
    policy: AutoApplyPolicyV1,
    oracle_registry: DeterministicOracleRegistryV1,
) -> None:
    registry_ref = _domain_registry_ref(domain_registry)
    if (
        item.domain_registry_ref != registry_ref
        or policy.domain_registry != registry_ref
    ):
        _reject(
            "domain_registry_mismatch",
            "ApprovalItem and auto-apply policy must bind the supplied domain registry",
        )
    if proof.affected_domain_scope != item.domain_scope:
        _reject(
            "affected_domain_scope_mismatch",
            "Proof affected scope must equal ApprovalItem domain scope",
        )
    if assessment.affected_domain_scope != proof.affected_domain_scope:
        _reject(
            "affected_domain_scope_mismatch",
            "Recomputed affected scope must equal proof scope",
        )

    known_ids = {definition.domain_id for definition in domain_registry.definitions}
    referenced_ids = _scope_ids(proof.affected_domain_scope)
    for scope in (*policy.allowed_domain_scopes, *policy.forbidden_domain_scopes):
        referenced_ids.update(scope.domain_ids)
    required_oracle_ids = {
        (oracle.oracle_id, oracle.oracle_version)
        for oracle in policy.required_deterministic_oracles
    }
    for definition in oracle_registry.definitions:
        if (definition.oracle_id, definition.oracle_version) not in required_oracle_ids:
            continue
        if isinstance(definition.supported_domain_scope, DomainScope):
            referenced_ids.update(definition.supported_domain_scope.domain_ids)
    unknown = referenced_ids - known_ids
    if unknown:
        _reject(
            "unknown_domain",
            "Auto-apply scope references an unknown domain",
            domain_ids=sorted(unknown),
        )

    affected = _scope_ids(proof.affected_domain_scope)
    forbidden = {
        domain_id
        for scope in policy.forbidden_domain_scopes
        for domain_id in scope.domain_ids
    }
    if affected & forbidden:
        _reject(
            "forbidden_domain_affected",
            "Affected scope intersects a forbidden auto-apply scope",
            domain_ids=sorted(affected & forbidden),
        )
    allowed = {
        domain_id
        for scope in policy.allowed_domain_scopes
        for domain_id in scope.domain_ids
    }
    uncovered = affected - allowed
    if uncovered:
        _reject(
            "affected_domain_not_allowed",
            "Allowed auto-apply scopes do not cover every affected domain",
            domain_ids=sorted(uncovered),
        )


def _validate_profile(
    *,
    proof: AutoApplyProofV1,
    profile: ExecutionProfileDefinitionV1,
) -> None:
    binding = proof.validation_profile_binding
    profile_hash = execution_profile_payload_hash(profile)
    if (
        profile.profile != binding.validation_profile
        or profile_hash != binding.validation_profile_payload_hash
    ):
        _reject(
            "validation_profile_mismatch",
            "Resolved validation profile differs from the proof binding",
        )
    if profile.profile_kind != "validation" or not isinstance(
        profile.details, ValidationProfileDetailsV1
    ):
        _reject(
            "validation_profile_mismatch",
            "Auto-apply requires a validation execution profile",
        )
    if "patch" not in profile.details.subject_kinds:
        _reject(
            "validation_profile_mismatch",
            "Validation profile does not support Patch subjects",
        )
    if profile.details.auto_apply_policy != proof.policy:
        _reject(
            "validation_profile_mismatch",
            "Validation profile does not bind the proof auto-apply policy",
        )
    if RunKindRef(kind="patch.validate", version=1) not in profile.compatible_run_kinds:
        _reject(
            "validation_profile_mismatch",
            "Validation profile is not compatible with patch.validate@1",
        )
    required_outputs = {"evidence-set@1", "auto-apply-proof@1"}
    if not required_outputs <= set(profile.output_schema_ids):
        _reject(
            "validation_profile_mismatch",
            "Validation profile does not declare both auto-apply outputs",
        )
    if not _scope_ids(proof.affected_domain_scope) <= _scope_ids(profile.domain_scope):
        _reject(
            "validation_profile_mismatch",
            "Validation profile domain scope does not cover the affected scope",
        )


def _required_evidence_by_id(evidence_set: EvidenceSet) -> dict[str, EvidenceRequirement]:
    return {requirement.requirement_id: requirement for requirement in evidence_set.requirements}


def _validate_oracles(
    *,
    proof: AutoApplyProofV1,
    policy: AutoApplyPolicyV1,
    oracle_registry: DeterministicOracleRegistryV1,
    records: Mapping[str, ResolvedArtifactPayload],
    payloads: Mapping[str, dict[str, Any]],
    evidence_set: EvidenceSet,
    subject: ResolvedArtifactPayload,
    target_binding: PatchTargetBindingV1,
    decoder: OracleEvidenceDecoder,
) -> set[str]:
    if policy.deterministic_oracle_registry != _oracle_registry_ref(oracle_registry):
        _reject(
            "oracle_registry_mismatch",
            "Auto-apply policy does not bind the supplied oracle registry",
        )
    required_refs = tuple(policy.required_deterministic_oracles)
    bound_refs = tuple(binding.oracle for binding in proof.deterministic_oracle_evidence)
    if bound_refs != required_refs:
        _reject(
            "required_oracle_set_mismatch",
            "Proof oracle bindings must equal the policy required oracle set",
        )

    evidence_ids: set[str] = set()
    for binding in proof.deterministic_oracle_evidence:
        definitions = tuple(
            definition
            for definition in oracle_registry.definitions
            if (definition.oracle_id, definition.oracle_version)
            == (binding.oracle.oracle_id, binding.oracle.oracle_version)
        )
        if len(definitions) != 1:
            _reject(
                "oracle_resolution_failed",
                "Required deterministic oracle must resolve exactly once",
            )
        definition: DeterministicOracleDefinitionV1 = definitions[0]
        if definition.oracle_digest != binding.oracle.oracle_digest:
            _reject(
                "oracle_digest_mismatch",
                "Oracle evidence ref digest differs from the resolved definition",
            )
        if definition.domain_registry != policy.domain_registry:
            _reject(
                "oracle_domain_registry_mismatch",
                "Oracle definition and auto-apply policy use different domain registries",
            )
        affected = _scope_ids(proof.affected_domain_scope)
        if definition.supported_domain_scope != "all" and not affected <= _scope_ids(
            definition.supported_domain_scope
        ):
            _reject(
                "oracle_scope_not_covered",
                "Oracle supported scope does not cover every affected domain",
            )

        record = records.get(binding.evidence_artifact_id)
        if record is None:
            _reject(
                "oracle_evidence_missing",
                "Oracle evidence Artifact is missing",
                artifact_id=binding.evidence_artifact_id,
            )
        artifact = record.artifact
        if artifact.kind not in definition.evidence_artifact_kinds:
            _reject(
                "oracle_artifact_kind_forbidden",
                "Oracle evidence Artifact kind is not allowlisted",
                artifact_id=artifact.artifact_id,
            )
        if record.payload_schema_id not in definition.evidence_payload_schema_ids:
            _reject(
                "oracle_evidence_schema_forbidden",
                "Oracle evidence payload schema is not allowlisted",
                artifact_id=artifact.artifact_id,
            )
        if artifact.payload_hash != binding.evidence_payload_hash:
            _reject(
                "oracle_evidence_hash_mismatch",
                "Oracle evidence binding hash differs from its Artifact",
                artifact_id=artifact.artifact_id,
            )
        try:
            claims = decoder(record.payload_schema_id, payloads[artifact.artifact_id])
            claims = OracleEvidenceClaims.model_validate(claims.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError, ValidationError) as exc:
            _reject(
                "oracle_evidence_invalid",
                "Oracle evidence schema decoder rejected the payload",
                artifact_id=artifact.artifact_id,
            )
            raise AssertionError("unreachable") from exc
        if claims.oracle != binding.oracle:
            _reject(
                "oracle_evidence_binding_mismatch",
                "Oracle evidence payload binds a different oracle",
                artifact_id=artifact.artifact_id,
            )
        if (
            claims.subject_artifact_id != subject.artifact.artifact_id
            or claims.subject_digest != subject.artifact.payload_hash
            or claims.target_binding != target_binding
            or claims.evaluated_domain_scope != proof.affected_domain_scope
            or binding.evaluated_domain_scope != proof.affected_domain_scope
        ):
            _reject(
                "oracle_evidence_binding_mismatch",
                "Oracle evidence does not bind the exact subject, target, ref, and scope",
                artifact_id=artifact.artifact_id,
            )
        if claims.predicate_schema_id != definition.predicate_schema_id:
            _reject(
                "oracle_predicate_mismatch",
                "Oracle evidence predicate is not the registered predicate schema",
                artifact_id=artifact.artifact_id,
            )
        if claims.verdict != "passed":
            _reject(
                "oracle_verdict_not_passed",
                "Only a passed oracle verdict can support auto-apply",
                artifact_id=artifact.artifact_id,
            )
        if claims.verdict_authority != "deterministic":
            _reject(
                "oracle_verdict_not_deterministic",
                "Only deterministic oracle authority can support auto-apply",
                artifact_id=artifact.artifact_id,
            )
        required_parents = {
            subject.artifact.artifact_id,
            target_binding.target_artifact_id,
        }
        if (
            claims.direct_parent_artifact_ids != artifact.lineage
            or not required_parents <= set(artifact.lineage)
        ):
            _reject(
                "oracle_lineage_mismatch",
                "Oracle evidence lineage does not exactly match its payload projection",
                artifact_id=artifact.artifact_id,
            )
        evidence_requirements = tuple(
            requirement
            for requirement in evidence_set.requirements
            if requirement.evidence_artifact_id == artifact.artifact_id
        )
        if (
            len(evidence_requirements) != 1
            or evidence_requirements[0].applicability != "required"
            or evidence_requirements[0].status != "passed"
            or evidence_requirements[0].tool_version != definition.tool_version
        ):
            _reject(
                "oracle_evidence_requirement_mismatch",
                "EvidenceSet must contain one passed requirement for each oracle evidence",
                artifact_id=artifact.artifact_id,
            )
        evidence_ids.add(artifact.artifact_id)
    return evidence_ids


def _outcome_requirements(
    snapshots: Sequence[ResolvedPolicySnapshotV1],
    rule: QualifiedOutcomeRuleRefV1,
) -> tuple[tuple[ResolvedPolicySnapshotV1, ResolvedArtifactRequirementV1], ...]:
    matches: list[tuple[ResolvedPolicySnapshotV1, ResolvedArtifactRequirementV1]] = []
    for snapshot in snapshots:
        if snapshot.resolved_policy_id != rule.resolved_policy_id:
            continue
        matches.extend(
            (snapshot, requirement)
            for requirement in snapshot.requirements
            if requirement.outcome_rule_id == rule.outcome_rule_id
        )
    return tuple(matches)


def _validate_outcomes(
    *,
    proof: AutoApplyProofV1,
    policy: AutoApplyPolicyV1,
    snapshots: Sequence[ResolvedPolicySnapshotV1],
    records: Mapping[str, ResolvedArtifactPayload],
    payloads: Mapping[str, dict[str, Any]],
    evidence_set: EvidenceSet,
    subject_artifact_id: str,
    subject_digest: str,
    target_binding: PatchTargetBindingV1,
    affected_domain_scope: DomainScope,
    validation_profile_payload_hash: str,
    decoder: OutcomeEvidenceDecoder,
) -> tuple[set[str], set[str]]:
    snapshot_ids = [snapshot.resolved_policy_id for snapshot in snapshots]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        _reject(
            "outcome_policy_resolution_failed",
            "Resolved outcome policy IDs must be unique",
        )
    required_rules = tuple(policy.required_outcome_rules)
    bound_rules = tuple(binding.rule for binding in proof.required_outcome_evidence)
    if bound_rules != required_rules:
        _reject(
            "required_outcome_set_mismatch",
            "Proof outcome bindings must equal the policy required outcome set",
        )

    evidence_by_requirement = _required_evidence_by_id(evidence_set)
    evidence_ids: set[str] = set()
    regression_ids: set[str] = set()
    for binding in proof.required_outcome_evidence:
        resolved = _outcome_requirements(snapshots, binding.rule)
        if len(resolved) != 1:
            _reject(
                "outcome_policy_resolution_failed",
                "Required outcome rule must resolve to exactly one Artifact requirement",
            )
        snapshot, requirement = resolved[0]
        if (
            snapshot.source_profile_field_path != "/params/validation_policy"
            or snapshot.source_profile_payload_hash != validation_profile_payload_hash
        ):
            _reject(
                "outcome_policy_profile_mismatch",
                "Resolved outcome policy does not bind the exact validation profile",
            )
        if requirement.requirement_id != binding.requirement_id:
            _reject(
                "outcome_requirement_mismatch",
                "Outcome evidence requirement ID differs from the resolved policy",
            )
        record = records.get(binding.evidence_artifact_id)
        if record is None:
            _reject(
                "outcome_evidence_missing",
                "Required outcome evidence Artifact is missing",
                artifact_id=binding.evidence_artifact_id,
            )
        artifact = record.artifact
        if artifact.kind != requirement.artifact_kind:
            _reject(
                "outcome_artifact_kind_mismatch",
                "Outcome evidence kind differs from the resolved requirement",
                artifact_id=artifact.artifact_id,
            )
        if record.payload_schema_id != requirement.payload_schema_id:
            _reject(
                "outcome_artifact_schema_mismatch",
                "Outcome evidence schema differs from the resolved requirement",
                artifact_id=artifact.artifact_id,
            )
        if artifact.payload_hash != binding.evidence_payload_hash:
            _reject(
                "outcome_evidence_hash_mismatch",
                "Outcome evidence binding hash differs from its Artifact",
                artifact_id=artifact.artifact_id,
            )
        try:
            claims = decoder(record.payload_schema_id, payloads[artifact.artifact_id])
            claims = QualifiedOutcomeEvidenceClaims.model_validate(
                claims.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError, ValidationError) as exc:
            _reject(
                "outcome_evidence_invalid",
                "Qualified outcome evidence schema decoder rejected the payload",
                artifact_id=artifact.artifact_id,
            )
            raise AssertionError("unreachable") from exc
        if claims.rule != binding.rule or claims.requirement_id != binding.requirement_id:
            _reject(
                "outcome_evidence_binding_mismatch",
                "Outcome payload binds another qualified rule or requirement",
                artifact_id=artifact.artifact_id,
            )
        if (
            claims.subject_artifact_id != subject_artifact_id
            or claims.subject_digest != subject_digest
            or claims.target_binding != target_binding
            or claims.evaluated_domain_scope != affected_domain_scope
        ):
            _reject(
                "outcome_evidence_binding_mismatch",
                "Outcome payload does not bind the exact subject, target, ref, and scope",
                artifact_id=artifact.artifact_id,
            )
        if claims.verdict != "passed":
            _reject(
                "outcome_verdict_not_passed",
                "Only a passed qualified outcome can support auto-apply",
                artifact_id=artifact.artifact_id,
            )
        if claims.verdict_authority != "deterministic":
            _reject(
                "outcome_verdict_not_deterministic",
                "Only deterministic outcome authority can support auto-apply",
                artifact_id=artifact.artifact_id,
            )
        evidence_requirement = evidence_by_requirement.get(binding.requirement_id)
        if (
            evidence_requirement is None
            or evidence_requirement.applicability != "required"
            or evidence_requirement.status != "passed"
            or evidence_requirement.evidence_artifact_id != artifact.artifact_id
        ):
            _reject(
                "outcome_requirement_mismatch",
                "EvidenceSet does not contain the exact passed outcome requirement",
                artifact_id=artifact.artifact_id,
            )
        required_parents = {subject_artifact_id, target_binding.target_artifact_id}
        if (
            claims.direct_parent_artifact_ids != artifact.lineage
            or not required_parents <= set(artifact.lineage)
        ):
            _reject(
                "outcome_lineage_mismatch",
                "Outcome evidence lineage does not exactly match its payload projection",
                artifact_id=artifact.artifact_id,
            )
        evidence_ids.add(artifact.artifact_id)
        if artifact.kind == "regression_evidence":
            regression_ids.add(artifact.artifact_id)
    return evidence_ids, regression_ids


def _validate_evidence_set(
    *,
    record: ResolvedArtifactPayload,
    evidence_set: EvidenceSet,
    item: ApprovalItem,
    target_binding: PatchTargetBindingV1,
    evidence_records: Mapping[str, ResolvedArtifactPayload],
) -> None:
    if record.artifact.kind != "validation_evidence" or record.payload_schema_id != "evidence-set@1":
        _reject(
            "evidence_set_artifact_mismatch",
            "EvidenceSet must be a validation_evidence/evidence-set@1 Artifact",
        )
    if (
        evidence_set.subject_artifact_id != item.subject_artifact_id
        or evidence_set.subject_digest != item.subject_digest
        or evidence_set.target_binding != target_binding
        or evidence_set.overall_status != "passed"
        or item.evidence_set_artifact_id != record.artifact.artifact_id
    ):
        _reject(
            "evidence_set_binding_mismatch",
            "EvidenceSet does not bind the exact passed ApprovalItem target",
        )
    expected_lineage = {
        item.subject_artifact_id,
        target_binding.target_artifact_id,
        *evidence_set.supporting_artifact_ids,
        *(binding.evidence_artifact_id for binding in evidence_set.finding_bindings),
        *(
            requirement.evidence_artifact_id
            for requirement in evidence_set.requirements
            if requirement.evidence_artifact_id is not None
        ),
    }
    if set(record.artifact.lineage) != expected_lineage:
        _reject(
            "evidence_set_lineage_mismatch",
            "EvidenceSet lineage must equal its complete direct evidence closure",
        )
    unresolved = expected_lineage - {
        item.subject_artifact_id,
        target_binding.target_artifact_id,
        *evidence_records,
    }
    if unresolved:
        _reject(
            "evidence_set_evidence_missing",
            "EvidenceSet lineage references unresolved supporting evidence",
            artifact_ids=sorted(unresolved),
        )


def _validate_exact_subject_target(
    *,
    item: ApprovalItem,
    subject_record: ResolvedArtifactPayload,
    subject_patch: PatchV2,
    target_record: ResolvedArtifactPayload,
    assessment: AutoApplyChangeAssessment,
) -> PatchTargetBindingV1:
    if item.subject_kind != "patch" or item.status not in {
        "validated",
        "auto_apply_eligible",
    }:
        _reject(
            "subject_not_auto_apply_patch",
            "Auto-apply is valid only for validated or eligible Patch subjects",
        )
    if item.auto_apply_proof is None or not isinstance(
        item.target_binding, PatchTargetBindingV1
    ):
        _reject(
            "subject_not_auto_apply_patch",
            "Patch auto-apply requires immutable target and proof bindings",
        )
    target_binding = item.target_binding
    if (
        target_binding.expected_ref is not None
        and target_binding.expected_ref.artifact_id != assessment.base_artifact_id
    ):
        _reject(
            "ref_base_mismatch",
            "Patch base Artifact does not match the frozen target ref value",
        )
    if (
        subject_record.artifact.kind != "patch"
        or subject_record.payload_schema_id != "patch@2"
        or item.subject_artifact_id != subject_record.artifact.artifact_id
        or item.subject_digest != subject_record.artifact.payload_hash
    ):
        _reject(
            "subject_binding_mismatch",
            "ApprovalItem does not bind the exact Patch Artifact and payload",
        )
    if subject_record.artifact.version_tuple.ir_snapshot_id != subject_patch.base_snapshot_id:
        _reject(
            "subject_binding_mismatch",
            "Patch Artifact VersionTuple does not bind the Patch base snapshot",
        )
    if assessment.base_artifact_id not in subject_record.artifact.lineage:
        _reject(
            "subject_lineage_mismatch",
            "Patch Artifact does not directly bind the assessed base Artifact",
        )
    if (
        target_record.artifact.kind != "ir_snapshot"
        or target_record.artifact.artifact_id != target_binding.target_artifact_id
        or target_record.artifact.payload_hash != target_binding.target_digest
    ):
        _reject(
            "target_binding_mismatch",
            "Approval target binding does not match the exact preview Artifact",
        )
    if target_record.artifact.version_tuple.ir_snapshot_id != target_binding.target_snapshot_id:
        _reject(
            "target_binding_mismatch",
            "Preview Artifact VersionTuple does not bind the target snapshot",
        )
    expected_target_lineage = tuple(
        sorted({assessment.base_artifact_id, subject_record.artifact.artifact_id})
    )
    if target_record.artifact.lineage != expected_target_lineage:
        _reject(
            "target_lineage_mismatch",
            "Preview Artifact lineage must be exactly base plus Patch",
        )
    if target_record.payload_schema_id != assessment.target_payload_schema_id:
        _reject(
            "target_schema_mismatch",
            "Preview payload schema differs from the diff assessment",
        )
    if (
        subject_patch.base_snapshot_id != assessment.base_snapshot_id
        or subject_patch.target_snapshot_id != assessment.target_snapshot_id
        or target_binding.target_snapshot_id != assessment.target_snapshot_id
        or assessment.subject_artifact_id != subject_record.artifact.artifact_id
        or assessment.subject_digest != subject_record.artifact.payload_hash
        or assessment.target_artifact_id != target_record.artifact.artifact_id
        or assessment.target_digest != target_record.artifact.payload_hash
    ):
        _reject(
            "change_assessment_mismatch",
            "Canonical diff assessment does not bind the exact base, Patch, and target",
        )
    if (
        not assessment.schema_id
        or len(assessment.schema_digest) != 64
        or any(character not in "0123456789abcdef" for character in assessment.schema_digest)
    ):
        _reject(
            "change_assessment_mismatch",
            "Canonical diff assessment lacks an exact schema binding",
        )
    return target_binding


def _validate_auto_apply(
    *,
    outcome_code: str,
    item: ApprovalItem,
    subject: ResolvedArtifactPayload,
    target: ResolvedArtifactPayload,
    proof: ResolvedArtifactPayload,
    evidence_set: ResolvedArtifactPayload,
    evidence_artifacts: Sequence[ResolvedArtifactPayload],
    domain_registry: DomainRegistryV1,
    policy_registry: AutoApplyPolicyRegistryV1,
    oracle_registry: DeterministicOracleRegistryV1,
    validation_profile: ExecutionProfileDefinitionV1,
    resolved_outcome_policies: Sequence[ResolvedPolicySnapshotV1],
    change_assessment: AutoApplyChangeAssessment,
    current_ref: RefValue | None,
    oracle_evidence_decoder: OracleEvidenceDecoder,
    outcome_evidence_decoder: OutcomeEvidenceDecoder,
) -> None:
    """Re-run the complete deterministic auto-apply guard without side effects."""

    if outcome_code != AUTO_APPLY_OUTCOME_CODE:
        _reject(
            "outcome_not_auto_eligible",
            "Only patch_validation_auto_eligible may produce or consume auto proof",
        )

    item = _contract_value(item, ApprovalItem, "ApprovalItem")
    domain_registry = _contract_value(domain_registry, DomainRegistryV1, "DomainRegistryV1")
    policy_registry = _contract_value(
        policy_registry, AutoApplyPolicyRegistryV1, "AutoApplyPolicyRegistryV1"
    )
    oracle_registry = _contract_value(
        oracle_registry,
        DeterministicOracleRegistryV1,
        "DeterministicOracleRegistryV1",
    )
    validation_profile = _contract_value(
        validation_profile,
        ExecutionProfileDefinitionV1,
        "ExecutionProfileDefinitionV1",
    )
    resolved_outcome_policies = tuple(
        _contract_value(snapshot, ResolvedPolicySnapshotV1, "ResolvedPolicySnapshotV1")
        for snapshot in resolved_outcome_policies
    )

    subject_patch = _parse_payload(
        subject,
        PatchV2,
        reason_code="subject_payload_invalid",
    )
    _load_payload(target)
    proof_payload = _parse_payload(
        proof,
        AutoApplyProofV1,
        reason_code="proof_payload_invalid",
    )
    evidence_set_payload = _parse_payload(
        evidence_set,
        EvidenceSet,
        reason_code="evidence_set_payload_invalid",
    )
    records, payloads = _artifact_records(evidence_artifacts)

    target_binding = _validate_exact_subject_target(
        item=item,
        subject_record=subject,
        subject_patch=subject_patch,
        target_record=target,
        assessment=change_assessment,
    )
    if current_ref != target_binding.expected_ref:
        _reject(
            "ref_binding_mismatch",
            "Current ref does not equal the immutable auto-apply CAS precondition",
        )

    proof_binding = item.auto_apply_proof
    assert proof_binding is not None
    if proof.artifact.kind != "validation_evidence" or proof.payload_schema_id != "auto-apply-proof@1":
        _reject(
            "proof_artifact_mismatch",
            "Auto proof must be a validation_evidence/auto-apply-proof@1 Artifact",
        )
    if (
        proof_binding.proof_artifact_id != proof.artifact.artifact_id
        or proof_binding.subject_digest != item.subject_digest
        or proof_binding.target_digest != target_binding.target_digest
        or proof_binding.expected_ref != target_binding.expected_ref
        or proof_binding.validation_evidence_artifact_id
        != evidence_set.artifact.artifact_id
        or proof_payload.subject_artifact_id != item.subject_artifact_id
        or proof_payload.subject_digest != item.subject_digest
        or proof_payload.target_binding != target_binding
        or proof_payload.validation_evidence_artifact_id
        != evidence_set.artifact.artifact_id
        or proof_payload.regression_evidence_artifact_ids
        != item.regression_evidence_artifact_ids
    ):
        _reject(
            "proof_binding_mismatch",
            "Auto proof payload, binding, subject, target, ref, or evidence differs",
        )
    if (
        proof_payload.policy != proof_binding.policy
        or proof_payload.validation_profile_binding.policy != proof_binding.policy
    ):
        _reject(
            "proof_policy_mismatch",
            "Auto proof policy refs are not identical",
        )

    policy = _resolve_policy(policy_ref=proof_payload.policy, registry=policy_registry)
    if target_binding.ref_name not in policy.allowed_ref_names:
        _reject(
            "ref_name_forbidden",
            "Patch target ref is not allowlisted for auto-apply",
            ref_name=target_binding.ref_name,
        )
    if len(subject_patch.ops) > policy.maximum_operation_count:
        _reject(
            "operation_count_exceeded",
            "Patch operation count exceeds the auto-apply policy",
        )
    forbidden_ops = sorted(
        {operation.op for operation in subject_patch.ops}
        - set(policy.allowed_operation_kinds)
    )
    if forbidden_ops:
        _reject(
            "operation_kind_forbidden",
            "Patch contains an operation kind outside the auto-apply allowlist",
            operation_kinds=forbidden_ops,
        )
    if not change_assessment.field_classification_complete:
        _reject(
            "field_classification_incomplete",
            "Unknown field classification makes auto-apply ineligible",
        )
    if policy.require_no_numeric_value_change and change_assessment.numeric_value_changed:
        _reject(
            "numeric_change_forbidden",
            "Auto-apply policy forbids numeric value changes",
        )
    if (
        policy.require_no_narrative_text_change
        and change_assessment.narrative_text_changed
    ):
        _reject(
            "narrative_change_forbidden",
            "Auto-apply policy forbids narrative text changes",
        )

    _validate_domains(
        item=item,
        proof=proof_payload,
        assessment=change_assessment,
        domain_registry=domain_registry,
        policy=policy,
        oracle_registry=oracle_registry,
    )
    _validate_profile(
        proof=proof_payload,
        profile=validation_profile,
    )
    _validate_evidence_set(
        record=evidence_set,
        evidence_set=evidence_set_payload,
        item=item,
        target_binding=target_binding,
        evidence_records=records,
    )
    oracle_ids = _validate_oracles(
        proof=proof_payload,
        policy=policy,
        oracle_registry=oracle_registry,
        records=records,
        payloads=payloads,
        evidence_set=evidence_set_payload,
        subject=subject,
        target_binding=target_binding,
        decoder=oracle_evidence_decoder,
    )
    outcome_ids, regression_ids = _validate_outcomes(
        proof=proof_payload,
        policy=policy,
        snapshots=resolved_outcome_policies,
        records=records,
        payloads=payloads,
        evidence_set=evidence_set_payload,
        subject_artifact_id=item.subject_artifact_id,
        subject_digest=item.subject_digest,
        target_binding=target_binding,
        affected_domain_scope=proof_payload.affected_domain_scope,
        validation_profile_payload_hash=(
            proof_payload.validation_profile_binding.validation_profile_payload_hash
        ),
        decoder=outcome_evidence_decoder,
    )
    if (
        set(proof_payload.regression_evidence_artifact_ids) != regression_ids
        or set(item.regression_evidence_artifact_ids) != regression_ids
    ):
        _reject(
            "regression_evidence_mismatch",
            "Regression evidence must equal resolved regression outcome evidence",
        )

    expected_proof_lineage = {
        item.subject_artifact_id,
        target_binding.target_artifact_id,
        evidence_set.artifact.artifact_id,
        *oracle_ids,
        *outcome_ids,
        *regression_ids,
    }
    if set(proof.artifact.lineage) != expected_proof_lineage:
        _reject(
            "proof_lineage_mismatch",
            "Auto proof lineage must equal its subject, target, EvidenceSet, and evidence",
        )


def validate_auto_apply(
    *,
    outcome_code: str,
    item: ApprovalItem,
    subject: ResolvedArtifactPayload,
    target: ResolvedArtifactPayload,
    proof: ResolvedArtifactPayload,
    evidence_set: ResolvedArtifactPayload,
    evidence_artifacts: Sequence[ResolvedArtifactPayload],
    domain_registry: DomainRegistryV1,
    policy_registry: AutoApplyPolicyRegistryV1,
    oracle_registry: DeterministicOracleRegistryV1,
    validation_profile: ExecutionProfileDefinitionV1,
    resolved_outcome_policies: Sequence[ResolvedPolicySnapshotV1],
    change_assessment: AutoApplyChangeAssessment,
    current_ref: RefValue | None,
    oracle_evidence_decoder: OracleEvidenceDecoder,
    outcome_evidence_decoder: OutcomeEvidenceDecoder,
) -> None:
    """Re-run the complete guard and normalize every malformed input to one typed error."""

    try:
        _validate_auto_apply(
            outcome_code=outcome_code,
            item=item,
            subject=subject,
            target=target,
            proof=proof,
            evidence_set=evidence_set,
            evidence_artifacts=evidence_artifacts,
            domain_registry=domain_registry,
            policy_registry=policy_registry,
            oracle_registry=oracle_registry,
            validation_profile=validation_profile,
            resolved_outcome_policies=resolved_outcome_policies,
            change_assessment=change_assessment,
            current_ref=current_ref,
            oracle_evidence_decoder=oracle_evidence_decoder,
            outcome_evidence_decoder=outcome_evidence_decoder,
        )
    except AutoApplyGuardError:
        raise
    except Exception as exc:
        raise AutoApplyGuardError(
            "Auto-apply guard input could not be validated",
            reason_code="invalid_guard_input",
        ) from exc


__all__ = [
    "AUTO_APPLY_OUTCOME_CODE",
    "AutoApplyChangeAssessment",
    "AutoApplyGuardError",
    "OracleEvidenceClaims",
    "QualifiedOutcomeEvidenceClaims",
    "ResolvedArtifactPayload",
    "validate_auto_apply",
]
