"""M4 immutable approval, validation, and auto-apply wire contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal, TypeAlias, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import (
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
)
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRoutePolicyRefV1,
    DomainScope,
    Permission,
    Role,
    SubjectKind,
)
from gameforge.contracts.ir import SourceRef
from gameforge.contracts.lineage import ArtifactKind, AuditActor
from gameforge.contracts.storage import RefValue


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
LowerHexSha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(gt=0)]

_SUBJECT_ORDER: dict[str, int] = {
    "patch": 0,
    "constraint_proposal": 1,
    "rollback_request": 2,
}
_STAGE_ORDER: dict[str, int] = {
    "parse": 0,
    "typecheck": 1,
    "compile": 2,
    "differential": 3,
    "golden": 4,
}
_ARTIFACT_KIND_ORDER = {value: index for index, value in enumerate(get_args(ArtifactKind))}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


def _stable_unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _canonical_model_key(value: BaseModel) -> str:
    return canonical_json(value.model_dump(mode="json"))


def _stable_unique_models(values: Sequence[Any]) -> tuple[Any, ...]:
    by_payload = {_canonical_model_key(value): value for value in values}
    return tuple(by_payload[key] for key in sorted(by_payload))


def _canonical_subject_kinds(
    values: Sequence[SubjectKind],
) -> tuple[SubjectKind, ...]:
    return tuple(sorted(set(values), key=_SUBJECT_ORDER.__getitem__))


class ConstraintSourceBinding(_FrozenModel):
    source_artifact_id: NonEmptyStr
    source_ref: SourceRef | None = None
    provenance_hash: LowerHexSha256


class ConstraintProposalV1(_FrozenModel):
    proposal_schema_version: Literal["constraint-proposal@1"] = "constraint-proposal@1"
    revision: PositiveInt
    supersedes_artifact_id: NonEmptyStr | None = None
    base_constraint_snapshot_id: NonEmptyStr | None = None
    dsl_grammar_version: NonEmptyStr
    domain_scope: DomainScope
    constraints: tuple[Constraint, ...]
    source_bindings: tuple[ConstraintSourceBinding, ...]
    produced_by: Literal["agent", "human"]
    producer_run_id: NonEmptyStr | None = None
    rationale: NonEmptyStr

    @field_validator("source_bindings")
    @classmethod
    def _canonical_source_bindings(
        cls, value: tuple[ConstraintSourceBinding, ...]
    ) -> tuple[ConstraintSourceBinding, ...]:
        ids = [binding.source_artifact_id for binding in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate ConstraintSourceBinding source_artifact_id")
        return tuple(sorted(value, key=lambda item: item.source_artifact_id))

    @field_validator("constraints")
    @classmethod
    def _canonical_constraints(cls, value: tuple[Constraint, ...]) -> tuple[Constraint, ...]:
        identities = [constraint.id for constraint in value]
        if any(not identity for identity in identities):
            raise ValueError("constraint id must be non-empty")
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate constraint id")
        return tuple(sorted(value, key=lambda constraint: constraint.id))

    @model_validator(mode="after")
    def _validate_revision_and_producer(self) -> ConstraintProposalV1:
        if self.revision == 1 and self.supersedes_artifact_id is not None:
            raise ValueError("revision 1 cannot set supersedes_artifact_id")
        if self.revision > 1 and self.supersedes_artifact_id is None:
            raise ValueError("revision > 1 requires supersedes_artifact_id")
        if self.produced_by == "agent" and self.producer_run_id is None:
            raise ValueError("agent proposal requires producer_run_id")
        if self.produced_by == "human" and self.producer_run_id is not None:
            raise ValueError("human proposal must not set producer_run_id")
        if any(
            constraint.dsl_grammar_version != self.dsl_grammar_version
            for constraint in self.constraints
        ):
            raise ValueError("constraint dsl_grammar_version must match proposal")
        return self


def _validate_rollback_profile(
    binding: ResolvedExecutionProfileBindingV1,
) -> None:
    if binding.field_path != "/params/rollback_profile":
        raise ValueError("rollback_profile_binding must use /params/rollback_profile")
    if binding.expected_profile_kind != "rollback":
        raise ValueError("rollback_profile_binding must resolve profile_kind rollback")


class RollbackRequestV1(_FrozenModel):
    rollback_schema_version: Literal["rollback-request@1"] = "rollback-request@1"
    ref_name: NonEmptyStr
    expected_current_ref: RefValue
    target_artifact_id: NonEmptyStr
    target_history_revision: PositiveInt
    rollback_profile_binding: ResolvedExecutionProfileBindingV1
    reason: NonEmptyStr
    reverses_approval_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _validate_profile(self) -> RollbackRequestV1:
        _validate_rollback_profile(self.rollback_profile_binding)
        return self


ApprovalStatus = Literal[
    "draft",
    "validating",
    "validation_failed",
    "validated",
    "pending_approval",
    "auto_apply_eligible",
    "approved",
    "changes_requested",
    "rejected",
    "applied",
    "rolled_back",
    "superseded",
]


class EvidenceRequirement(_FrozenModel):
    requirement_id: NonEmptyStr
    kind: NonEmptyStr
    applicability: Literal["required", "not_applicable"]
    status: Literal["passed", "failed", "unproven", "not_applicable"]
    evidence_artifact_id: NonEmptyStr | None = None
    reason_code: NonEmptyStr | None = None
    tool_version: NonEmptyStr

    @model_validator(mode="after")
    def _validate_disposition(self) -> EvidenceRequirement:
        if self.applicability == "not_applicable":
            if self.status != "not_applicable":
                raise ValueError("not_applicable requirement must have matching status")
            if self.evidence_artifact_id is not None or self.reason_code is None:
                raise ValueError("not_applicable requirement needs reason_code and no evidence")
            return self
        if self.status == "not_applicable":
            raise ValueError("required evidence cannot have not_applicable status")
        if self.status in {"passed", "failed"} and self.evidence_artifact_id is None:
            raise ValueError(f"{self.status} evidence requires evidence_artifact_id")
        if self.status == "unproven" and self.reason_code is None:
            raise ValueError("unproven evidence requires reason_code")
        return self


class PatchTargetBindingV1(_FrozenModel):
    binding_schema_version: Literal["approval-target-binding@1"] = "approval-target-binding@1"
    subject_kind: Literal["patch"] = "patch"
    target_artifact_kind: Literal["ir_snapshot"] = "ir_snapshot"
    target_artifact_id: NonEmptyStr
    target_snapshot_id: NonEmptyStr
    target_digest: LowerHexSha256
    ref_name: NonEmptyStr
    expected_ref: RefValue | None


class ConstraintTargetBindingV1(_FrozenModel):
    binding_schema_version: Literal["approval-target-binding@1"] = "approval-target-binding@1"
    subject_kind: Literal["constraint_proposal"] = "constraint_proposal"
    target_artifact_kind: Literal["constraint_snapshot"] = "constraint_snapshot"
    target_artifact_id: NonEmptyStr
    target_snapshot_id: NonEmptyStr
    target_digest: LowerHexSha256
    ref_name: NonEmptyStr
    expected_ref: RefValue | None


class RollbackTargetBindingV1(_FrozenModel):
    binding_schema_version: Literal["approval-target-binding@1"] = "approval-target-binding@1"
    subject_kind: Literal["rollback_request"] = "rollback_request"
    target_artifact_kind: ArtifactKind
    target_artifact_id: NonEmptyStr
    target_snapshot_id: NonEmptyStr | None = None
    target_digest: LowerHexSha256
    ref_name: NonEmptyStr
    expected_ref: RefValue
    rollback_profile_binding: ResolvedExecutionProfileBindingV1

    @model_validator(mode="after")
    def _validate_profile(self) -> RollbackTargetBindingV1:
        _validate_rollback_profile(self.rollback_profile_binding)
        return self


ApprovalTargetBinding: TypeAlias = Annotated[
    PatchTargetBindingV1 | ConstraintTargetBindingV1 | RollbackTargetBindingV1,
    Field(discriminator="subject_kind"),
]


class FindingEvidenceBindingV1(_FrozenModel):
    finding_id: NonEmptyStr
    finding_revision: PositiveInt
    evidence_artifact_id: NonEmptyStr
    finding_digest: LowerHexSha256


class EvidenceSet(_FrozenModel):
    evidence_schema_version: Literal["evidence-set@1"] = "evidence-set@1"
    subject_artifact_id: NonEmptyStr
    subject_digest: LowerHexSha256
    policy_version: NonEmptyStr
    validation_run_id: NonEmptyStr
    target_binding: ApprovalTargetBinding | None = None
    supporting_artifact_ids: tuple[NonEmptyStr, ...]
    finding_bindings: tuple[FindingEvidenceBindingV1, ...]
    requirements: tuple[EvidenceRequirement, ...]
    overall_status: Literal["passed", "failed", "unproven"]

    @field_validator("supporting_artifact_ids")
    @classmethod
    def _canonical_supporting_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value)

    @field_validator("finding_bindings")
    @classmethod
    def _canonical_findings(
        cls, value: tuple[FindingEvidenceBindingV1, ...]
    ) -> tuple[FindingEvidenceBindingV1, ...]:
        identities = [(item.finding_id, item.finding_revision) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate finding revision binding")
        return tuple(sorted(value, key=lambda item: (item.finding_id, item.finding_revision)))

    @field_validator("requirements")
    @classmethod
    def _canonical_requirements(
        cls, value: tuple[EvidenceRequirement, ...]
    ) -> tuple[EvidenceRequirement, ...]:
        ids = [item.requirement_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate evidence requirement_id")
        return tuple(sorted(value, key=lambda item: item.requirement_id))

    @model_validator(mode="after")
    def _validate_overall_status(self) -> EvidenceSet:
        required_statuses = [
            item.status for item in self.requirements if item.applicability == "required"
        ]
        if any(status == "failed" for status in required_statuses):
            expected = "failed"
        elif any(status == "unproven" for status in required_statuses):
            expected = "unproven"
        else:
            expected = "passed"
        if self.overall_status != expected:
            raise ValueError(f"overall_status must be {expected} for requirement dispositions")
        return self


class ConstraintCompileStageV1(_FrozenModel):
    stage_id: NonEmptyStr
    stage: Literal["parse", "typecheck", "compile", "differential", "golden"]
    status: Literal["passed", "failed", "unproven", "not_applicable"]
    engine_id: NonEmptyStr | None = None
    engine_version: NonEmptyStr | None = None
    reason_code: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _validate_execution_details(self) -> ConstraintCompileStageV1:
        has_engine = self.engine_id is not None and self.engine_version is not None
        if (self.engine_id is None) != (self.engine_version is None):
            raise ValueError("engine_id and engine_version must be supplied together")
        if self.stage == "differential":
            if not has_engine:
                raise ValueError("differential stage requires engine_id and engine_version")
        elif has_engine:
            raise ValueError("only differential stages may carry engine details")

        if self.status == "passed":
            if self.reason_code is not None:
                raise ValueError("passed compile stage cannot carry reason_code")
        elif self.reason_code is None:
            raise ValueError(f"{self.status} compile stage requires reason_code")

        if self.status == "not_applicable" and self.stage != "golden":
            raise ValueError("only golden stage may be not_applicable")
        return self


class ConstraintCompileEvidenceV1(_FrozenModel):
    evidence_schema_version: Literal["constraint-compile-evidence@1"] = (
        "constraint-compile-evidence@1"
    )
    proposal_artifact_id: NonEmptyStr
    base_constraint_snapshot_artifact_id: NonEmptyStr | None = None
    candidate_constraint_snapshot_artifact_id: NonEmptyStr | None = None
    dsl_grammar_version: NonEmptyStr
    compiler_profile: ProfileRefV1
    stages: tuple[ConstraintCompileStageV1, ...]
    overall_status: Literal["passed", "failed", "unproven"]

    @field_validator("stages")
    @classmethod
    def _canonical_stages(
        cls, value: tuple[ConstraintCompileStageV1, ...]
    ) -> tuple[ConstraintCompileStageV1, ...]:
        ids = [item.stage_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate constraint compile stage_id")
        return tuple(sorted(value, key=lambda item: (_STAGE_ORDER[item.stage], item.stage_id)))

    @model_validator(mode="after")
    def _validate_stages_and_outcome(self) -> ConstraintCompileEvidenceV1:
        for core_stage in ("parse", "typecheck", "compile"):
            if sum(stage.stage == core_stage for stage in self.stages) != 1:
                raise ValueError(f"compile evidence requires exactly one {core_stage} stage")
        if sum(stage.stage == "golden" for stage in self.stages) != 1:
            raise ValueError("compile evidence requires exactly one golden stage")
        differential_stages = [stage for stage in self.stages if stage.stage == "differential"]
        if len(differential_stages) < 2:
            raise ValueError("compile evidence requires at least two differential engines")
        differential_engines = [
            (stage.engine_id, stage.engine_version) for stage in differential_stages
        ]
        if len(differential_engines) != len(set(differential_engines)):
            raise ValueError("compile evidence contains a duplicate differential engine")

        statuses = [stage.status for stage in self.stages]
        if "failed" in statuses:
            expected = "failed"
        elif "unproven" in statuses:
            expected = "unproven"
        else:
            expected = "passed"
        if self.overall_status != expected:
            raise ValueError(f"overall_status must be {expected} for compile stages")
        compile_stage = next(stage for stage in self.stages if stage.stage == "compile")
        candidate_exists = self.candidate_constraint_snapshot_artifact_id is not None
        if candidate_exists != (compile_stage.status == "passed"):
            raise ValueError(
                "candidate_constraint_snapshot_artifact_id must exist exactly when compile passes"
            )
        return self


class ApprovalRequirement(_FrozenModel):
    requirement_id: NonEmptyStr
    domain_scope: DomainScope
    required_permission: Permission
    route_role: Role
    min_approvals: PositiveInt
    assignee_principal_ids: tuple[NonEmptyStr, ...]
    distinct_from_requirement_ids: tuple[NonEmptyStr, ...]

    @field_validator("assignee_principal_ids", "distinct_from_requirement_ids")
    @classmethod
    def _canonical_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value)

    @model_validator(mode="after")
    def _validate_permission_scope(self) -> ApprovalRequirement:
        if self.required_permission.domain_scope != self.domain_scope:
            raise ValueError("required_permission domain_scope must match requirement")
        return self


class ApprovalDecision(_FrozenModel):
    decision_id: NonEmptyStr
    requirement_ids: tuple[NonEmptyStr, ...]
    decision: Literal["approve", "reject", "request_changes"]
    actor: AuditActor
    expected_workflow_revision: PositiveInt
    reason_code: NonEmptyStr
    comment: NonEmptyStr | None = None
    occurred_at: NonEmptyStr

    @field_validator("requirement_ids")
    @classmethod
    def _canonical_requirement_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = _stable_unique_strings(value)
        if not canonical:
            raise ValueError("decision requirement_ids must be non-empty")
        return canonical

    @model_validator(mode="after")
    def _human_only(self) -> ApprovalDecision:
        if self.actor.principal_kind != "human":
            raise ValueError("approval decision actor must be human")
        return self


class ApprovalPolicyRefV1(_FrozenModel):
    policy_version: NonEmptyStr
    policy_digest: LowerHexSha256


def compute_approval_policy_digest(
    *,
    policy_version: str,
    subject_kinds: Sequence[SubjectKind],
    maker_checker_required: bool,
    human_approver_required: bool,
    reauthorize_on_decision: bool,
    reauthorize_on_apply: bool,
    rollback_requires_approval: bool,
    terminal_revision_immutable: bool,
) -> str:
    return canonical_sha256(
        {
            "policy_schema_version": "approval-policy@1",
            "policy_version": policy_version,
            "subject_kinds": list(_canonical_subject_kinds(subject_kinds)),
            "maker_checker_required": maker_checker_required,
            "human_approver_required": human_approver_required,
            "reauthorize_on_decision": reauthorize_on_decision,
            "reauthorize_on_apply": reauthorize_on_apply,
            "rollback_requires_approval": rollback_requires_approval,
            "terminal_revision_immutable": terminal_revision_immutable,
        }
    )


class ApprovalPolicyV1(_FrozenModel):
    policy_schema_version: Literal["approval-policy@1"] = "approval-policy@1"
    policy_version: NonEmptyStr
    subject_kinds: tuple[SubjectKind, ...]
    maker_checker_required: Literal[True]
    human_approver_required: Literal[True]
    reauthorize_on_decision: Literal[True]
    reauthorize_on_apply: Literal[True]
    rollback_requires_approval: Literal[True]
    terminal_revision_immutable: Literal[True]
    policy_digest: LowerHexSha256

    @field_validator("subject_kinds")
    @classmethod
    def _canonical_kinds(cls, value: tuple[SubjectKind, ...]) -> tuple[SubjectKind, ...]:
        canonical = _canonical_subject_kinds(value)
        if not canonical:
            raise ValueError("approval policy subject_kinds must be non-empty")
        return canonical

    @model_validator(mode="after")
    def _validate_digest(self) -> ApprovalPolicyV1:
        expected = compute_approval_policy_digest(
            policy_version=self.policy_version,
            subject_kinds=self.subject_kinds,
            maker_checker_required=self.maker_checker_required,
            human_approver_required=self.human_approver_required,
            reauthorize_on_decision=self.reauthorize_on_decision,
            reauthorize_on_apply=self.reauthorize_on_apply,
            rollback_requires_approval=self.rollback_requires_approval,
            terminal_revision_immutable=self.terminal_revision_immutable,
        )
        if self.policy_digest != expected:
            raise ValueError("policy_digest does not match approval policy payload")
        return self


def compute_approval_policy_registry_digest(
    policies: Sequence[ApprovalPolicyV1],
) -> str:
    ordered = sorted(policies, key=lambda item: item.policy_version)
    return canonical_sha256(
        {
            "registry_schema_version": "approval-policy-registry@1",
            "policies": [item.model_dump(mode="json") for item in ordered],
        }
    )


class ApprovalPolicyRegistryV1(_FrozenModel):
    registry_schema_version: Literal["approval-policy-registry@1"] = "approval-policy-registry@1"
    policies: tuple[ApprovalPolicyV1, ...]
    registry_digest: LowerHexSha256

    @field_validator("policies")
    @classmethod
    def _canonical_policies(
        cls, value: tuple[ApprovalPolicyV1, ...]
    ) -> tuple[ApprovalPolicyV1, ...]:
        versions = [item.policy_version for item in value]
        if len(versions) != len(set(versions)):
            raise ValueError("duplicate approval policy_version")
        return tuple(sorted(value, key=lambda item: item.policy_version))

    @model_validator(mode="after")
    def _validate_digest(self) -> ApprovalPolicyRegistryV1:
        expected = compute_approval_policy_registry_digest(self.policies)
        if self.registry_digest != expected:
            raise ValueError("registry_digest does not match approval policies")
        return self


class QualifiedOutcomeRuleRefV1(_FrozenModel):
    resolved_policy_id: NonEmptyStr
    outcome_rule_id: NonEmptyStr


class DeterministicOracleRefV1(_FrozenModel):
    oracle_id: NonEmptyStr
    oracle_version: NonEmptyStr
    oracle_digest: LowerHexSha256


def _canonical_artifact_kinds(
    values: Sequence[ArtifactKind],
) -> tuple[ArtifactKind, ...]:
    return tuple(sorted(set(values), key=_ARTIFACT_KIND_ORDER.__getitem__))


def compute_deterministic_oracle_digest(
    *,
    oracle_id: str,
    oracle_version: str,
    engine_kind: str,
    tool_version: str,
    domain_registry: DomainRegistryRefV1,
    supported_domain_scope: DomainScope | Literal["all"],
    evidence_artifact_kinds: Sequence[ArtifactKind],
    evidence_payload_schema_ids: Sequence[str],
    predicate_schema_id: str,
) -> str:
    scope: Any = supported_domain_scope
    if isinstance(scope, DomainScope):
        scope = scope.model_dump(mode="json")
    return canonical_sha256(
        {
            "oracle_schema_version": "deterministic-oracle@1",
            "oracle_id": oracle_id,
            "oracle_version": oracle_version,
            "engine_kind": engine_kind,
            "tool_version": tool_version,
            "domain_registry": domain_registry.model_dump(mode="json"),
            "supported_domain_scope": scope,
            "evidence_artifact_kinds": list(_canonical_artifact_kinds(evidence_artifact_kinds)),
            "evidence_payload_schema_ids": list(
                _stable_unique_strings(evidence_payload_schema_ids)
            ),
            "predicate_schema_id": predicate_schema_id,
        }
    )


class DeterministicOracleDefinitionV1(_FrozenModel):
    oracle_schema_version: Literal["deterministic-oracle@1"] = "deterministic-oracle@1"
    oracle_id: NonEmptyStr
    oracle_version: NonEmptyStr
    engine_kind: Literal["graph", "asp", "smt", "simulation", "playtest_completion"]
    tool_version: NonEmptyStr
    domain_registry: DomainRegistryRefV1
    supported_domain_scope: DomainScope | Literal["all"]
    evidence_artifact_kinds: tuple[ArtifactKind, ...]
    evidence_payload_schema_ids: tuple[NonEmptyStr, ...]
    predicate_schema_id: NonEmptyStr
    oracle_digest: LowerHexSha256

    @field_validator("evidence_artifact_kinds")
    @classmethod
    def _canonical_kinds(cls, value: tuple[ArtifactKind, ...]) -> tuple[ArtifactKind, ...]:
        canonical = _canonical_artifact_kinds(value)
        if not canonical:
            raise ValueError("evidence_artifact_kinds must be non-empty")
        return canonical

    @field_validator("evidence_payload_schema_ids")
    @classmethod
    def _canonical_schemas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = _stable_unique_strings(value)
        if not canonical:
            raise ValueError("evidence_payload_schema_ids must be non-empty")
        return canonical

    @model_validator(mode="after")
    def _validate_digest(self) -> DeterministicOracleDefinitionV1:
        expected = compute_deterministic_oracle_digest(
            oracle_id=self.oracle_id,
            oracle_version=self.oracle_version,
            engine_kind=self.engine_kind,
            tool_version=self.tool_version,
            domain_registry=self.domain_registry,
            supported_domain_scope=self.supported_domain_scope,
            evidence_artifact_kinds=self.evidence_artifact_kinds,
            evidence_payload_schema_ids=self.evidence_payload_schema_ids,
            predicate_schema_id=self.predicate_schema_id,
        )
        if self.oracle_digest != expected:
            raise ValueError("oracle_digest does not match oracle definition")
        return self


class DeterministicOracleRegistryRefV1(_FrozenModel):
    registry_version: NonEmptyStr
    registry_digest: LowerHexSha256


def compute_deterministic_oracle_registry_digest(
    registry_version: str,
    definitions: Sequence[DeterministicOracleDefinitionV1],
) -> str:
    ordered = sorted(definitions, key=lambda item: (item.oracle_id, item.oracle_version))
    return canonical_sha256(
        {
            "registry_schema_version": "deterministic-oracle-registry@1",
            "registry_version": registry_version,
            "definitions": [item.model_dump(mode="json") for item in ordered],
        }
    )


class DeterministicOracleRegistryV1(_FrozenModel):
    registry_schema_version: Literal["deterministic-oracle-registry@1"] = (
        "deterministic-oracle-registry@1"
    )
    registry_version: NonEmptyStr
    definitions: tuple[DeterministicOracleDefinitionV1, ...]
    registry_digest: LowerHexSha256

    @field_validator("definitions")
    @classmethod
    def _canonical_definitions(
        cls, value: tuple[DeterministicOracleDefinitionV1, ...]
    ) -> tuple[DeterministicOracleDefinitionV1, ...]:
        identities = [(item.oracle_id, item.oracle_version) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate deterministic oracle identity")
        return tuple(sorted(value, key=lambda item: (item.oracle_id, item.oracle_version)))

    @model_validator(mode="after")
    def _validate_digest(self) -> DeterministicOracleRegistryV1:
        expected = compute_deterministic_oracle_registry_digest(
            self.registry_version, self.definitions
        )
        if self.registry_digest != expected:
            raise ValueError("registry_digest does not match oracle definitions")
        return self


def _canonical_oracle_refs(
    values: Sequence[DeterministicOracleRefV1],
) -> tuple[DeterministicOracleRefV1, ...]:
    identities = [(item.oracle_id, item.oracle_version) for item in values]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate deterministic oracle ref identity")
    return tuple(sorted(values, key=lambda item: (item.oracle_id, item.oracle_version)))


def _canonical_outcome_refs(
    values: Sequence[QualifiedOutcomeRuleRefV1],
) -> tuple[QualifiedOutcomeRuleRefV1, ...]:
    identities = [(item.resolved_policy_id, item.outcome_rule_id) for item in values]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate qualified outcome rule identity")
    return tuple(sorted(values, key=lambda item: (item.resolved_policy_id, item.outcome_rule_id)))


def _canonical_domain_scopes(values: Sequence[DomainScope]) -> tuple[DomainScope, ...]:
    by_ids = {scope.domain_ids: scope for scope in values}
    return tuple(by_ids[key] for key in sorted(by_ids))


class AutoApplyPolicyV1(_FrozenModel):
    policy_schema_version: Literal["auto-apply-policy@1"] = "auto-apply-policy@1"
    policy_id: NonEmptyStr
    policy_version: NonEmptyStr
    subject_kind: Literal["patch"] = "patch"
    allowed_operation_kinds: tuple[NonEmptyStr, ...]
    maximum_operation_count: PositiveInt
    domain_registry: DomainRegistryRefV1
    deterministic_oracle_registry: DeterministicOracleRegistryRefV1
    required_deterministic_oracles: tuple[DeterministicOracleRefV1, ...]
    required_outcome_rules: tuple[QualifiedOutcomeRuleRefV1, ...]
    allowed_domain_scopes: tuple[DomainScope, ...]
    forbidden_domain_scopes: tuple[DomainScope, ...]
    require_no_numeric_value_change: bool
    require_no_narrative_text_change: bool
    allowed_ref_names: tuple[NonEmptyStr, ...]

    @field_validator("allowed_operation_kinds", "allowed_ref_names")
    @classmethod
    def _canonical_strings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = _stable_unique_strings(value)
        if not canonical:
            raise ValueError("auto-apply allowlist must be non-empty")
        return canonical

    @field_validator("required_deterministic_oracles")
    @classmethod
    def _canonical_oracles(
        cls, value: tuple[DeterministicOracleRefV1, ...]
    ) -> tuple[DeterministicOracleRefV1, ...]:
        canonical = _canonical_oracle_refs(value)
        if not canonical:
            raise ValueError("required_deterministic_oracles must be non-empty")
        return canonical

    @field_validator("required_outcome_rules")
    @classmethod
    def _canonical_outcomes(
        cls, value: tuple[QualifiedOutcomeRuleRefV1, ...]
    ) -> tuple[QualifiedOutcomeRuleRefV1, ...]:
        canonical = _canonical_outcome_refs(value)
        if not canonical:
            raise ValueError("required_outcome_rules must be non-empty")
        return canonical

    @field_validator("allowed_domain_scopes", "forbidden_domain_scopes")
    @classmethod
    def _canonical_scopes(cls, value: tuple[DomainScope, ...]) -> tuple[DomainScope, ...]:
        return _canonical_domain_scopes(value)

    @model_validator(mode="after")
    def _validate_scope_sets(self) -> AutoApplyPolicyV1:
        if not self.allowed_domain_scopes:
            raise ValueError("allowed_domain_scopes must be non-empty")
        allowed_ids = {
            domain_id for scope in self.allowed_domain_scopes for domain_id in scope.domain_ids
        }
        forbidden_ids = {
            domain_id for scope in self.forbidden_domain_scopes for domain_id in scope.domain_ids
        }
        if allowed_ids & forbidden_ids:
            raise ValueError("allowed and forbidden domain scopes overlap")
        return self


def compute_auto_apply_policy_digest(policy: AutoApplyPolicyV1) -> str:
    return canonical_sha256(policy.model_dump(mode="json"))


def compute_auto_apply_policy_registry_digest(
    registry_version: str,
    policies: Sequence[AutoApplyPolicyV1],
) -> str:
    ordered = sorted(policies, key=lambda item: (item.policy_id, item.policy_version))
    return canonical_sha256(
        {
            "registry_schema_version": "auto-apply-policy-registry@1",
            "registry_version": registry_version,
            "policies": [item.model_dump(mode="json") for item in ordered],
        }
    )


class AutoApplyPolicyRegistryV1(_FrozenModel):
    registry_schema_version: Literal["auto-apply-policy-registry@1"] = (
        "auto-apply-policy-registry@1"
    )
    registry_version: NonEmptyStr
    policies: tuple[AutoApplyPolicyV1, ...]
    registry_digest: LowerHexSha256

    @field_validator("policies")
    @classmethod
    def _canonical_policies(
        cls, value: tuple[AutoApplyPolicyV1, ...]
    ) -> tuple[AutoApplyPolicyV1, ...]:
        identities = [(item.policy_id, item.policy_version) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate auto-apply policy identity")
        return tuple(sorted(value, key=lambda item: (item.policy_id, item.policy_version)))

    @model_validator(mode="after")
    def _validate_digest(self) -> AutoApplyPolicyRegistryV1:
        expected = compute_auto_apply_policy_registry_digest(self.registry_version, self.policies)
        if self.registry_digest != expected:
            raise ValueError("registry_digest does not match auto-apply policies")
        return self


class AutoApplyValidationProfileBindingV1(_FrozenModel):
    validation_profile: ProfileRefV1
    validation_profile_payload_hash: LowerHexSha256
    policy: AutoApplyPolicyRefV1


class AutoApplyOracleEvidenceBindingV1(_FrozenModel):
    oracle: DeterministicOracleRefV1
    evaluated_domain_scope: DomainScope
    evidence_artifact_id: NonEmptyStr
    evidence_payload_hash: LowerHexSha256


class AutoApplyOutcomeEvidenceBindingV1(_FrozenModel):
    rule: QualifiedOutcomeRuleRefV1
    requirement_id: NonEmptyStr
    evidence_artifact_id: NonEmptyStr
    evidence_payload_hash: LowerHexSha256


class AutoApplyProofV1(_FrozenModel):
    proof_schema_version: Literal["auto-apply-proof@1"] = "auto-apply-proof@1"
    subject_artifact_id: NonEmptyStr
    subject_digest: LowerHexSha256
    target_binding: PatchTargetBindingV1
    affected_domain_scope: DomainScope
    validation_evidence_artifact_id: NonEmptyStr
    regression_evidence_artifact_ids: tuple[NonEmptyStr, ...]
    validation_profile_binding: AutoApplyValidationProfileBindingV1
    deterministic_oracle_evidence: tuple[AutoApplyOracleEvidenceBindingV1, ...]
    required_outcome_evidence: tuple[AutoApplyOutcomeEvidenceBindingV1, ...]
    policy: AutoApplyPolicyRefV1

    @field_validator("regression_evidence_artifact_ids")
    @classmethod
    def _canonical_regression_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value)

    @field_validator("deterministic_oracle_evidence")
    @classmethod
    def _canonical_oracle_evidence(
        cls, value: tuple[AutoApplyOracleEvidenceBindingV1, ...]
    ) -> tuple[AutoApplyOracleEvidenceBindingV1, ...]:
        identities = [(item.oracle.oracle_id, item.oracle.oracle_version) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate auto-apply oracle evidence identity")
        return tuple(
            sorted(
                value,
                key=lambda item: (item.oracle.oracle_id, item.oracle.oracle_version),
            )
        )

    @field_validator("required_outcome_evidence")
    @classmethod
    def _canonical_outcome_evidence(
        cls, value: tuple[AutoApplyOutcomeEvidenceBindingV1, ...]
    ) -> tuple[AutoApplyOutcomeEvidenceBindingV1, ...]:
        identities = [
            (
                item.rule.resolved_policy_id,
                item.rule.outcome_rule_id,
                item.requirement_id,
            )
            for item in value
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate auto-apply outcome evidence identity")
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.rule.resolved_policy_id,
                    item.rule.outcome_rule_id,
                    item.requirement_id,
                ),
            )
        )

    @model_validator(mode="after")
    def _validate_scope_and_policy(self) -> AutoApplyProofV1:
        if self.validation_profile_binding.policy != self.policy:
            raise ValueError("validation profile and proof policy refs differ")
        for evidence in self.deterministic_oracle_evidence:
            if evidence.evaluated_domain_scope != self.affected_domain_scope:
                raise ValueError("oracle evaluated_domain_scope must equal affected_domain_scope")
        return self


class AutoApplyProofBindingV1(_FrozenModel):
    proof_artifact_id: NonEmptyStr
    policy: AutoApplyPolicyRefV1
    subject_digest: LowerHexSha256
    target_digest: LowerHexSha256
    expected_ref: RefValue | None
    validation_evidence_artifact_id: NonEmptyStr


class ApprovalItem(_FrozenModel):
    approval_schema_version: Literal["approval@1"] = "approval@1"
    approval_id: NonEmptyStr
    subject_series_id: NonEmptyStr
    subject_revision: PositiveInt
    subject_kind: SubjectKind
    subject_artifact_id: NonEmptyStr
    subject_digest: LowerHexSha256
    status: ApprovalStatus
    workflow_revision: PositiveInt
    supersedes_approval_id: NonEmptyStr | None = None
    proposer: AuditActor
    domain_scope: DomainScope
    domain_registry_ref: DomainRegistryRefV1
    route_policy: DomainRoutePolicyRefV1
    role_policy_version: NonEmptyStr
    role_policy_digest: LowerHexSha256
    approval_policy: ApprovalPolicyRefV1
    requirements: tuple[ApprovalRequirement, ...]
    decisions: tuple[ApprovalDecision, ...]
    active_validation_run_id: NonEmptyStr | None = None
    last_validation_failure_artifact_id: NonEmptyStr | None = None
    evidence_set_artifact_id: NonEmptyStr | None = None
    regression_evidence_artifact_ids: tuple[NonEmptyStr, ...]
    target_binding: ApprovalTargetBinding | None = None
    auto_apply_proof: AutoApplyProofBindingV1 | None = None
    created_at: NonEmptyStr
    submitted_at: NonEmptyStr | None = None
    decided_at: NonEmptyStr | None = None
    applied_at: NonEmptyStr | None = None

    @field_validator("requirements")
    @classmethod
    def _canonical_requirements(
        cls, value: tuple[ApprovalRequirement, ...]
    ) -> tuple[ApprovalRequirement, ...]:
        ids = [item.requirement_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate approval requirement_id")
        return tuple(sorted(value, key=lambda item: item.requirement_id))

    @field_validator("decisions")
    @classmethod
    def _canonical_decisions(
        cls, value: tuple[ApprovalDecision, ...]
    ) -> tuple[ApprovalDecision, ...]:
        ids = [item.decision_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate approval decision_id")
        return tuple(sorted(value, key=lambda item: item.decision_id))

    @field_validator("regression_evidence_artifact_ids")
    @classmethod
    def _canonical_regression_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value)

    @model_validator(mode="after")
    def _validate_static_workflow_invariants(self) -> ApprovalItem:
        if self.route_policy.domain_registry_ref != self.domain_registry_ref:
            raise ValueError("route policy and ApprovalItem domain registry refs differ")

        requirement_ids = {item.requirement_id for item in self.requirements}
        covered_domains: set[str] = set()
        for requirement in self.requirements:
            covered_domains.update(requirement.domain_scope.domain_ids)
            if not set(requirement.domain_scope.domain_ids) <= set(self.domain_scope.domain_ids):
                raise ValueError("approval requirement scope exceeds subject domain_scope")
            for distinct_id in requirement.distinct_from_requirement_ids:
                if distinct_id not in requirement_ids:
                    raise ValueError(f"unknown distinct_from_requirement_id: {distinct_id}")
                if distinct_id == requirement.requirement_id:
                    raise ValueError("approval requirement cannot be distinct from itself")
        if self.requirements and covered_domains != set(self.domain_scope.domain_ids):
            raise ValueError("approval requirements must cover the full domain_scope")

        for decision in self.decisions:
            if decision.actor.principal_id == self.proposer.principal_id:
                raise ValueError("proposer cannot approve or decide their own revision")
            unknown = set(decision.requirement_ids) - requirement_ids
            if unknown:
                raise ValueError(f"decision references unknown requirements: {sorted(unknown)}")
            if decision.expected_workflow_revision >= self.workflow_revision:
                raise ValueError("decision expected_workflow_revision must precede item revision")

        if self.target_binding is not None:
            if self.target_binding.subject_kind != self.subject_kind:
                raise ValueError("target_binding subject_kind does not match ApprovalItem")
        elif self.subject_kind != "constraint_proposal":
            raise ValueError("patch and rollback ApprovalItems require target_binding")
        elif self.status in {
            "validated",
            "pending_approval",
            "approved",
            "applied",
            "rolled_back",
        }:
            raise ValueError("validated constraint proposal requires target_binding")

        if self.status == "validating":
            if self.active_validation_run_id is None:
                raise ValueError("validating ApprovalItem requires active_validation_run_id")
        elif self.active_validation_run_id is not None:
            raise ValueError("only validating ApprovalItem may have active validation Run")

        if self.auto_apply_proof is not None:
            if self.subject_kind != "patch":
                raise ValueError("auto_apply_proof is valid only for patch subjects")
            if self.status not in {
                "validated",
                "auto_apply_eligible",
                "applied",
                "rolled_back",
                "superseded",
            }:
                raise ValueError(
                    "auto_apply_proof requires validated, eligible, applied, "
                    "rolled_back, or superseded status"
                )
            if self.target_binding is None:
                raise ValueError("auto_apply_proof requires target_binding")
            if self.auto_apply_proof.subject_digest != self.subject_digest:
                raise ValueError("auto_apply_proof subject_digest mismatch")
            if self.auto_apply_proof.target_digest != self.target_binding.target_digest:
                raise ValueError("auto_apply_proof target_digest mismatch")
            if self.auto_apply_proof.expected_ref != self.target_binding.expected_ref:
                raise ValueError("auto_apply_proof expected_ref mismatch")
            if (
                self.auto_apply_proof.validation_evidence_artifact_id
                != self.evidence_set_artifact_id
            ):
                raise ValueError("auto_apply_proof validation evidence mismatch")
        elif self.status == "auto_apply_eligible":
            raise ValueError("auto_apply_eligible requires auto_apply_proof")

        if self.status == "rolled_back" and self.subject_kind == "rollback_request":
            raise ValueError("rollback_request ApprovalItem cannot enter rolled_back")

        evidence_required = {
            "validation_failed",
            "validated",
            "pending_approval",
            "auto_apply_eligible",
            "approved",
            "applied",
            "rolled_back",
        }
        if self.status in evidence_required and self.evidence_set_artifact_id is None:
            raise ValueError(f"{self.status} ApprovalItem requires validation evidence")
        if (
            self.status
            in {
                "pending_approval",
                "approved",
                "changes_requested",
                "rejected",
            }
            and not self.requirements
        ):
            raise ValueError(f"{self.status} ApprovalItem requires approval requirements")

        if self.status in {"pending_approval", "approved", "rejected", "changes_requested"}:
            if self.submitted_at is None:
                raise ValueError(f"{self.status} ApprovalItem requires submitted_at")
        if self.status in {"approved", "rejected", "changes_requested"}:
            if self.decided_at is None:
                raise ValueError(f"{self.status} ApprovalItem requires decided_at")
        if self.status in {"applied", "rolled_back"} and self.applied_at is None:
            raise ValueError(f"{self.status} ApprovalItem requires applied_at")
        return self


class SubjectHead(_FrozenModel):
    subject_series_id: NonEmptyStr
    current_subject_artifact_id: NonEmptyStr
    current_approval_id: NonEmptyStr
    revision: PositiveInt


__all__ = [
    "ApprovalDecision",
    "ApprovalItem",
    "ApprovalPolicyRefV1",
    "ApprovalPolicyRegistryV1",
    "ApprovalPolicyV1",
    "ApprovalRequirement",
    "ApprovalStatus",
    "ApprovalTargetBinding",
    "AutoApplyOracleEvidenceBindingV1",
    "AutoApplyOutcomeEvidenceBindingV1",
    "AutoApplyPolicyRefV1",
    "AutoApplyPolicyRegistryRefV1",
    "AutoApplyPolicyRegistryV1",
    "AutoApplyPolicyV1",
    "AutoApplyProofBindingV1",
    "AutoApplyProofV1",
    "AutoApplyValidationProfileBindingV1",
    "ConstraintCompileEvidenceV1",
    "ConstraintCompileStageV1",
    "ConstraintProposalV1",
    "ConstraintSourceBinding",
    "ConstraintTargetBindingV1",
    "DeterministicOracleDefinitionV1",
    "DeterministicOracleRefV1",
    "DeterministicOracleRegistryRefV1",
    "DeterministicOracleRegistryV1",
    "EvidenceRequirement",
    "EvidenceSet",
    "FindingEvidenceBindingV1",
    "PatchTargetBindingV1",
    "QualifiedOutcomeRuleRefV1",
    "RollbackRequestV1",
    "RollbackTargetBindingV1",
    "SubjectHead",
    "compute_approval_policy_digest",
    "compute_approval_policy_registry_digest",
    "compute_auto_apply_policy_digest",
    "compute_auto_apply_policy_registry_digest",
    "compute_deterministic_oracle_digest",
    "compute_deterministic_oracle_registry_digest",
]
