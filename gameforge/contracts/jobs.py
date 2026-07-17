"""Persistent Run, attempt, outcome, and publication-policy wire contracts."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Mapping, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.execution_profiles import (
    ArtifactLineagePolicyRefV1,
    MigrationCapabilityMatrixRefV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    VersionTransitionPolicyRefV1,
)
from gameforge.contracts.findings import FindingPayloadV1, OracleType, FindingSource
from gameforge.contracts.identity import DomainScope, Permission
from gameforge.contracts.lineage import (
    ArtifactKind,
    AuditActor,
    MAX_RUNTIME_AUTHORITY_BINDINGS,
    ObjectLocation,
    ObjectRef,
    VersionTuple,
)
from gameforge.contracts.migration import (  # noqa: F401
    CanonicalRoundTripCheckResultV1,
    GoldenReplayCheckResultV1,
    MigrationCheckResultV1,
    MigrationCheckV1,
    MigrationPathResolvedCheckResultV1,
    MigrationReportV1,
    PublishBindingCheckResultV1,
    SemanticInvariantsCheckResultV1,
    SourceReadableCheckResultV1,
    TargetPayloadValidCheckResultV1,
    TargetReaderResolvedCheckResultV1,
)
from gameforge.contracts.playtest import CompletionOracleRegistryRefV1
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import FindingEvidenceBindingV1


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
BoundedNonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
BoundedJsonKey = Annotated[str, StringConstraints(max_length=4096)]
BoundedId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
JsonPointer = Annotated[
    str,
    StringConstraints(
        max_length=2048,
        pattern=r"^(?:|(?:/(?:[^~/]|~[01])*)+)$",
    ),
]
PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
Uint64 = Annotated[int, Field(ge=0, le=(1 << 64) - 1)]

MAX_COLLECTION_ITEMS = 1024
# TaskSuite derives one primary suite plus up to 1,024 ScenarioSpec siblings.
MAX_PREPARED_DOMAIN_ARTIFACTS = MAX_COLLECTION_ITEMS + 1
# A maximum-size Playtest binds one ConfigExport, one ConstraintSnapshot, one
# TaskSuite, and every selected ScenarioSpec.  REPLAY adds exactly one cassette
# root to the Run envelope; it is not prompt source content.
MAX_PLAYTEST_DIRECT_INPUT_ARTIFACTS = MAX_COLLECTION_ITEMS + 3
MAX_PLAYTEST_RUN_INPUT_ARTIFACTS = MAX_PLAYTEST_DIRECT_INPUT_ARTIFACTS + 1
# A subsequent RECORD/REPLAY prompt context may additionally bind the immediately
# preceding rendered prompt and its exact cassette shard/root.  Keep this separate
# from the frozen Run-input and first-prompt source authorities.
MAX_PLAYTEST_PROMPT_SOURCE_ARTIFACTS = MAX_PLAYTEST_DIRECT_INPUT_ARTIFACTS
MAX_PLAYTEST_PROMPT_UPSTREAM_ARTIFACTS = MAX_PLAYTEST_PROMPT_SOURCE_ARTIFACTS + 2
# Prepared domain lineage never includes the replay cassette root.  The largest
# legal direct-parent set is therefore the Playtest trace's exact input closure.
MAX_PLAYTEST_TRACE_LINEAGE_PARENTS = MAX_PLAYTEST_DIRECT_INPUT_ARTIFACTS
# A terminal manifest must represent the full frozen Task 11 execution envelope:
# up to three attempts, 1,000 repair calls per attempt, four routed prompts per
# call, one governed prompt context, one RECORD shard, plus bounded inputs and
# domain outputs.  Keep this authority-specific ceiling separate from ordinary
# request/result collection bounds.
MAX_RUN_MANIFEST_PARENT_BINDINGS = MAX_RUNTIME_AUTHORITY_BINDINGS
MAX_BENCHMARK_AGGREGATE_RESULT_ARTIFACTS = MAX_COLLECTION_ITEMS - 3
MAX_PREPARED_FINDINGS = 10_000
MAX_PREPARED_ARTIFACT_BYTES = 96 * 1024 * 1024
MAX_PREPARED_OUTCOME_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32
MAX_AGENT_PROMPT_CONTEXT_BYTES = 128 * 1024 * 1024
MAX_AGENT_PROMPT_CONTEXT_MESSAGES = 64
MAX_AGENT_PROMPT_CONTEXT_MESSAGE_BYTES = 17 * 1024 * 1024


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _json_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _json_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_data(item) for item in value]
    return value


def _validate_bounded_json(value: JsonValue, *, field_name: str) -> JsonValue:
    if len(canonical_json(value).encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError(f"{field_name} exceeds the canonical JSON byte limit")

    stack: list[tuple[JsonValue, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"{field_name} exceeds the JSON depth limit")
        if isinstance(item, str):
            if len(item) > 4096:
                raise ValueError(f"{field_name} contains an oversized string")
        elif isinstance(item, dict):
            if len(item) > MAX_COLLECTION_ITEMS:
                raise ValueError(f"{field_name} contains an oversized object")
            for key, child in item.items():
                if len(key) > 4096:
                    raise ValueError(f"{field_name} contains an oversized object key")
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            if len(item) > MAX_COLLECTION_ITEMS:
                raise ValueError(f"{field_name} contains an oversized array")
            stack.extend((child, depth + 1) for child in item)
    return value


def _canonical_unique_strings(
    values: tuple[str, ...], *, allow_empty: bool = True
) -> tuple[str, ...]:
    canonical = tuple(sorted(set(values)))
    if not allow_empty and not canonical:
        raise ValueError("collection must be non-empty")
    return canonical


def _canonical_unique_models[T: BaseModel](
    values: tuple[T, ...], *, allow_empty: bool = True
) -> tuple[T, ...]:
    by_payload = {canonical_json(value.model_dump(mode="json")): value for value in values}
    canonical = tuple(by_payload[key] for key in sorted(by_payload))
    if not allow_empty and not canonical:
        raise ValueError("collection must be non-empty")
    return canonical


def _canonical_finding_bindings(
    values: tuple[FindingEvidenceBindingV1, ...],
) -> tuple[FindingEvidenceBindingV1, ...]:
    finding_ids = [value.finding_id for value in values]
    if len(finding_ids) != len(set(finding_ids)):
        raise ValueError("each finding series may be bound only once")
    return tuple(sorted(values, key=lambda value: value.finding_id))


def canonical_payload_hash(payload: BaseModel | Mapping[str, Any]) -> str:
    return canonical_sha256(_json_data(payload))


FailureClassV1 = Literal[
    "business_rule",
    "validation",
    "transient_dependency",
    "permanent_dependency",
    "quota",
    "execution",
    "cancelled",
    "timeout",
    "lease",
    "subject_superseded",
    "integrity",
]
DependencyKind = Literal[
    "model_provider",
    "database",
    "object_store",
    "cost_ledger",
    "solver_executor",
    "simulation_backend",
    "game_environment",
    "identity_provider",
]


class DependencyFailureV1(_FrozenModel):
    dependency_schema_version: Literal["dependency-failure@1"] = "dependency-failure@1"
    dependency_kind: DependencyKind
    dependency_id: BoundedNonEmptyStr
    operation_code: BoundedNonEmptyStr
    classifier_code: BoundedNonEmptyStr
    upstream_status_code: int | None = Field(default=None, ge=100, le=599)
    retry_after_ms: int | None = Field(default=None, ge=0)


class FailureClassifierRefV1(_FrozenModel):
    classifier_version: PositiveInt
    classifier_digest: Sha256Hex


class FailureClassificationRuleV1(_FrozenModel):
    cause_code: BoundedNonEmptyStr
    failure_class: FailureClassV1
    intrinsic_retry_eligible: bool
    dependency_required: bool
    allowed_dependency_kinds: tuple[DependencyKind, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @field_validator("allowed_dependency_kinds")
    @classmethod
    def _canonical_dependencies(
        cls, value: tuple[DependencyKind, ...]
    ) -> tuple[DependencyKind, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _dependency_shape(self) -> "FailureClassificationRuleV1":
        if self.dependency_required != bool(self.allowed_dependency_kinds):
            raise ValueError("dependency_required must match a non-empty dependency allowlist")
        if self.intrinsic_retry_eligible and self.failure_class not in {
            "transient_dependency",
            "lease",
        }:
            raise ValueError("only transient dependency or lease failures are retryable")
        return self


def failure_classifier_digest(payload: Mapping[str, Any] | BaseModel) -> str:
    raw = _json_data(payload)
    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if key != "classifier_digest"}
        raw.setdefault("classifier_schema_version", "failure-classifier@1")
        raw["rules"] = sorted(raw.get("rules", []), key=lambda item: item["cause_code"])
    return canonical_sha256(raw)


class FailureClassifierV1(_FrozenModel):
    classifier_schema_version: Literal["failure-classifier@1"] = "failure-classifier@1"
    classifier_version: PositiveInt
    rules: tuple[FailureClassificationRuleV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    classifier_digest: Sha256Hex

    @field_validator("rules")
    @classmethod
    def _canonical_rules(
        cls, value: tuple[FailureClassificationRuleV1, ...]
    ) -> tuple[FailureClassificationRuleV1, ...]:
        causes = [rule.cause_code for rule in value]
        if len(causes) != len(set(causes)):
            raise ValueError("failure classifier cause codes must be unique")
        return tuple(sorted(value, key=lambda rule: rule.cause_code))

    @model_validator(mode="after")
    def _digest(self) -> "FailureClassifierV1":
        if self.classifier_digest != failure_classifier_digest(self):
            raise ValueError("classifier_digest does not match classifier payload")
        return self


class RetryPolicyRefV1(_FrozenModel):
    retry_policy_id: BoundedNonEmptyStr
    retry_policy_version: PositiveInt
    retry_policy_digest: Sha256Hex


def retry_policy_digest(payload: Mapping[str, Any] | BaseModel) -> str:
    raw = _json_data(payload)
    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if key != "retry_policy_digest"}
        raw.setdefault("retry_schema_version", "retry-policy@1")
        if "retryable_failure_classes" in raw:
            raw["retryable_failure_classes"] = sorted(set(raw["retryable_failure_classes"]))
    return canonical_sha256(raw)


class RetryPolicySnapshot(_FrozenModel):
    retry_schema_version: Literal["retry-policy@1"] = "retry-policy@1"
    retry_policy_id: BoundedNonEmptyStr
    retry_policy_version: PositiveInt
    max_attempts: PositiveInt
    retryable_failure_classes: tuple[FailureClassV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    backoff: Literal["fixed", "exponential"]
    base_delay_ms: NonNegativeInt
    max_delay_ms: NonNegativeInt
    jitter_policy: BoundedNonEmptyStr
    honor_retry_after: bool
    retry_policy_digest: Sha256Hex

    @field_validator("retryable_failure_classes")
    @classmethod
    def _canonical_classes(cls, value: tuple[FailureClassV1, ...]) -> tuple[FailureClassV1, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _closed_policy(self) -> "RetryPolicySnapshot":
        if self.base_delay_ms > self.max_delay_ms:
            raise ValueError("base delay cannot exceed max delay")
        if self.retry_policy_digest != retry_policy_digest(self):
            raise ValueError("retry_policy_digest does not match policy payload")
        return self


class RetryDecisionV1(_FrozenModel):
    decision_schema_version: Literal["retry-decision@1"] = "retry-decision@1"
    cause_code: BoundedNonEmptyStr
    failure_class: FailureClassV1
    intrinsic_retry_eligible: bool
    decision: Literal["retry", "terminal"]
    reason_code: Literal[
        "transient_eligible",
        "retry_after",
        "max_attempts_exhausted",
        "queue_deadline_exhausted",
        "attempt_deadline_exhausted",
        "overall_deadline_exhausted",
        "budget_exhausted",
        "policy_forbidden",
        "not_retry_eligible",
    ]
    retry_not_before_utc: BoundedNonEmptyStr | None = None
    classifier: FailureClassifierRefV1
    retry_policy: RetryPolicyRefV1
    evaluated_at_utc: BoundedNonEmptyStr

    @model_validator(mode="after")
    def _retry_shape(self) -> "RetryDecisionV1":
        retry_reasons = {"transient_eligible", "retry_after"}
        ineligible_reason = self.reason_code == "not_retry_eligible"
        if self.decision == "retry":
            if not self.intrinsic_retry_eligible or self.retry_not_before_utc is None:
                raise ValueError("retry requires eligibility and a not-before timestamp")
            if self.reason_code not in retry_reasons:
                raise ValueError("retry decision has a terminal reason")
        else:
            if self.retry_not_before_utc is not None:
                raise ValueError("terminal decision cannot schedule another attempt")
            if self.reason_code in retry_reasons:
                raise ValueError("terminal decision has a retry reason")
            deadline_reasons = {
                "queue_deadline_exhausted",
                "attempt_deadline_exhausted",
                "overall_deadline_exhausted",
            }
            if self.intrinsic_retry_eligible and ineligible_reason:
                raise ValueError("eligible failures cannot use not_retry_eligible")
            if (
                not self.intrinsic_retry_eligible
                and not ineligible_reason
                and self.reason_code not in deadline_reasons
            ):
                raise ValueError(
                    "ineligible failures require not_retry_eligible unless a deadline is authoritative"
                )
        return self


class PlannedAgentNodeVersionV1(_FrozenModel):
    agent_node_id: BoundedNonEmptyStr
    prompt_version: BoundedNonEmptyStr
    tool_version: BoundedNonEmptyStr
    allowed_model_snapshots: tuple[BoundedNonEmptyStr, ...] = Field(
        min_length=1, max_length=MAX_COLLECTION_ITEMS
    )

    @field_validator("allowed_model_snapshots")
    @classmethod
    def _canonical_models(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, allow_empty=False)


def execution_version_plan_digest(payload: Mapping[str, Any] | BaseModel) -> str:
    raw = _json_data(payload)
    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if key != "plan_digest"}
        raw.setdefault("plan_schema_version", "execution-version-plan@1")
        raw["nodes"] = sorted(raw.get("nodes", []), key=lambda item: item["agent_node_id"])
    return canonical_sha256(raw)


class ExecutionVersionPlanV1(_FrozenModel):
    plan_schema_version: Literal["execution-version-plan@1"] = "execution-version-plan@1"
    agent_graph_version: BoundedNonEmptyStr
    nodes: tuple[PlannedAgentNodeVersionV1, ...] = Field(
        min_length=1, max_length=MAX_COLLECTION_ITEMS
    )
    model_catalog_version: PositiveInt
    model_catalog_digest: Sha256Hex
    routing_policy_version: PositiveInt
    routing_policy_digest: Sha256Hex
    plan_digest: Sha256Hex

    @field_validator("nodes")
    @classmethod
    def _canonical_nodes(
        cls, value: tuple[PlannedAgentNodeVersionV1, ...]
    ) -> tuple[PlannedAgentNodeVersionV1, ...]:
        ids = [node.agent_node_id for node in value]
        if len(ids) != len(set(ids)):
            raise ValueError("execution plan node ids must be unique")
        return tuple(sorted(value, key=lambda node: node.agent_node_id))

    @model_validator(mode="after")
    def _digest(self) -> "ExecutionVersionPlanV1":
        if self.plan_digest != execution_version_plan_digest(self):
            raise ValueError("plan_digest does not match execution plan")
        return self


class ResolvedArtifactRequirementV1(_FrozenModel):
    requirement_id: BoundedNonEmptyStr
    outcome_rule_id: BoundedNonEmptyStr
    artifact_kind: ArtifactKind
    payload_schema_id: BoundedNonEmptyStr
    producer_profile_field_path: JsonPointer | None = None
    ordinal: PositiveInt


def resolved_policy_snapshot_digest(payload: Mapping[str, Any] | BaseModel) -> str:
    raw = _json_data(payload)
    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if key != "digest"}
        raw.setdefault("snapshot_schema_version", "resolved-policy@1")
        raw["requirements"] = sorted(
            raw.get("requirements", []),
            key=lambda item: (
                item["outcome_rule_id"],
                item["ordinal"],
                item["requirement_id"],
            ),
        )
    return canonical_sha256(raw)


class ResolvedPolicySnapshotV1(_FrozenModel):
    snapshot_schema_version: Literal["resolved-policy@1"] = "resolved-policy@1"
    resolved_policy_id: BoundedNonEmptyStr
    source_profile_field_path: JsonPointer
    source_profile_payload_hash: Sha256Hex
    requirements: tuple[ResolvedArtifactRequirementV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    digest: Sha256Hex

    @field_validator("requirements")
    @classmethod
    def _canonical_requirements(
        cls, value: tuple[ResolvedArtifactRequirementV1, ...]
    ) -> tuple[ResolvedArtifactRequirementV1, ...]:
        def key(
            item: ResolvedArtifactRequirementV1,
        ) -> tuple[str, int, str]:
            return (item.outcome_rule_id, item.ordinal, item.requirement_id)

        keys = [key(item) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("resolved requirements must have unique identities")
        return tuple(sorted(value, key=key))

    @model_validator(mode="after")
    def _digest(self) -> "ResolvedPolicySnapshotV1":
        if self.digest != resolved_policy_snapshot_digest(self):
            raise ValueError("resolved policy digest does not match its payload")
        return self


class RunPolicyBindingV1(_FrozenModel):
    binding_key: BoundedNonEmptyStr
    policy_kind: BoundedNonEmptyStr
    policy_id: BoundedNonEmptyStr
    policy_version: PositiveInt
    policy_digest: Sha256Hex


class RunSchemaBindingV1(_FrozenModel):
    binding_key: BoundedNonEmptyStr
    schema_id: BoundedNonEmptyStr


_TRACEPARENT = re.compile(
    r"^(?!ff)[0-9a-f]{2}-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}$"
)


class RunDispatchTraceCarrierV1(_FrozenModel):
    carrier_schema_version: Literal["run-dispatch-trace@1"] = "run-dispatch-trace@1"
    traceparent: BoundedNonEmptyStr
    tracestate: Annotated[str, StringConstraints(max_length=512)] | None = None

    @field_validator("traceparent")
    @classmethod
    def _valid_traceparent(cls, value: str) -> str:
        if _TRACEPARENT.fullmatch(value) is None:
            raise ValueError("traceparent must be a bounded W3C trace parent")
        return value


class RefReadBindingV1(_FrozenModel):
    ref_name: BoundedId
    expected_ref: RefValue | None = None


class GraphSelectionV1(_FrozenModel):
    mode: Literal["full", "ids"]
    entity_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    relation_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @field_validator("entity_ids", "relation_ids")
    @classmethod
    def _canonical_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)

    @model_validator(mode="after")
    def _selection_shape(self) -> "GraphSelectionV1":
        selected = bool(self.entity_ids or self.relation_ids)
        if self.mode == "full" and selected:
            raise ValueError("full graph selection forbids explicit ids")
        if self.mode == "ids" and not selected:
            raise ValueError("ids graph selection requires at least one id")
        return self


class PromptGoalBindingV1(_FrozenModel):
    source_artifact_id: BoundedId
    expected_payload_hash: Sha256Hex


class ValidationSubjectBindingV1(_FrozenModel):
    approval_id: BoundedId
    expected_workflow_revision: PositiveInt
    subject_head_revision: PositiveInt
    subject_artifact_id: BoundedId
    subject_digest: Sha256Hex
    active_validation_run_id: BoundedId


class PlaytestEpisodeBindingV1(_FrozenModel):
    episode_id: BoundedId
    scenario_spec_artifact_id: BoundedId


class SolverEngineRefV1(_FrozenModel):
    engine_id: BoundedId
    version: PositiveInt


class GenerationProposePayloadV1(_FrozenModel):
    schema_version: Literal["generation-propose@1"] = "generation-propose@1"
    base_snapshot_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId | None = None
    findings: tuple[FindingEvidenceBindingV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    objective_goal: PromptGoalBindingV1
    domain_scope: DomainScope
    target: RefReadBindingV1
    generation_policy: ProfileRefV1
    candidate_export_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @field_validator("findings")
    @classmethod
    def _findings(
        cls, value: tuple[FindingEvidenceBindingV1, ...]
    ) -> tuple[FindingEvidenceBindingV1, ...]:
        return _canonical_finding_bindings(value)

    @field_validator("candidate_export_profiles")
    @classmethod
    def _profiles(cls, value: tuple[ProfileRefV1, ...]) -> tuple[ProfileRefV1, ...]:
        return _canonical_unique_models(value)

    @model_validator(mode="after")
    def _export_constraint(self) -> "GenerationProposePayloadV1":
        if self.candidate_export_profiles and self.constraint_snapshot_artifact_id is None:
            raise ValueError("candidate_export_profiles require constraint_snapshot_artifact_id")
        return self


class PatchRepairPayloadV1(_FrozenModel):
    schema_version: Literal["patch-repair@1"] = "patch-repair@1"
    subject_patch_artifact_id: BoundedId
    expected_subject_head_revision: PositiveInt
    expected_workflow_revision: PositiveInt
    base_snapshot_artifact_id: BoundedId
    preview_snapshot_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId | None = None
    validation_evidence_artifact_id: BoundedId
    findings: tuple[FindingEvidenceBindingV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    target: RefReadBindingV1
    repair_policy: ProfileRefV1
    checker_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    simulation_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    regression_suite_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    candidate_export_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @field_validator("findings")
    @classmethod
    def _findings(
        cls, value: tuple[FindingEvidenceBindingV1, ...]
    ) -> tuple[FindingEvidenceBindingV1, ...]:
        return _canonical_finding_bindings(value)

    @field_validator("checker_profiles", "simulation_profiles", "candidate_export_profiles")
    @classmethod
    def _profiles(cls, value: tuple[ProfileRefV1, ...]) -> tuple[ProfileRefV1, ...]:
        return _canonical_unique_models(value)

    @field_validator("regression_suite_artifact_ids")
    @classmethod
    def _regression_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)

    @model_validator(mode="after")
    def _export_constraint(self) -> "PatchRepairPayloadV1":
        if self.candidate_export_profiles and self.constraint_snapshot_artifact_id is None:
            raise ValueError("candidate_export_profiles require constraint_snapshot_artifact_id")
        return self


class ConstraintProposalProposePayloadV1(_FrozenModel):
    schema_version: Literal["constraint-proposal-propose@1"] = "constraint-proposal-propose@1"
    source_artifact_ids: tuple[BoundedId, ...] = Field(
        min_length=1, max_length=MAX_COLLECTION_ITEMS
    )
    base_constraint_snapshot_artifact_id: BoundedId | None = None
    domain_scope: DomainScope
    authoring_goal: PromptGoalBindingV1
    dsl_grammar_version: BoundedNonEmptyStr
    extraction_policy: ProfileRefV1

    @field_validator("source_artifact_ids")
    @classmethod
    def _source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, allow_empty=False)


class ReviewRunPayloadV1(_FrozenModel):
    schema_version: Literal["review-run@1"] = "review-run@1"
    snapshot_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId | None = None
    selection: GraphSelectionV1
    review_profile: ProfileRefV1
    checker_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    simulation_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    llm_triage_policy: ProfileRefV1 | None = None

    @field_validator("checker_profiles", "simulation_profiles")
    @classmethod
    def _profiles(cls, value: tuple[ProfileRefV1, ...]) -> tuple[ProfileRefV1, ...]:
        return _canonical_unique_models(value)


class CheckerRunPayloadV1(_FrozenModel):
    schema_version: Literal["checker-run@1"] = "checker-run@1"
    snapshot_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId | None = None
    selection: GraphSelectionV1
    checker_profile: ProfileRefV1
    checker_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    defect_classes: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @field_validator("checker_ids", "defect_classes")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)


class SimulationRunPayloadV1(_FrozenModel):
    schema_version: Literal["simulation-run@1"] = "simulation-run@1"
    snapshot_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId | None = None
    scenario_artifact_id: BoundedId | None = None
    simulation_profile: ProfileRefV1
    workload_profile: ProfileRefV1
    replication_count: int = Field(ge=1, le=100_000)
    horizon_steps: int = Field(ge=1, le=100_000_000)


class PlaytestRunPayloadV1(_FrozenModel):
    schema_version: Literal["playtest-run@1"] = "playtest-run@1"
    config_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId
    task_suite_artifact_id: BoundedId
    episodes: tuple[PlaytestEpisodeBindingV1, ...] = Field(
        min_length=1, max_length=MAX_COLLECTION_ITEMS
    )
    environment_profile: ProfileRefV1
    planner_policy: ProfileRefV1
    max_steps_per_episode: int = Field(ge=1, le=10_000_000)
    interaction_mode: Literal["autonomous", "bounded_choice"]

    @field_validator("episodes")
    @classmethod
    def _episodes(
        cls, value: tuple[PlaytestEpisodeBindingV1, ...]
    ) -> tuple[PlaytestEpisodeBindingV1, ...]:
        episode_ids = [item.episode_id for item in value]
        scenario_ids = [item.scenario_spec_artifact_id for item in value]
        if len(episode_ids) != len(set(episode_ids)):
            raise ValueError("playtest episode ids must be unique")
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("playtest scenario bindings must be unique")
        return tuple(sorted(value, key=lambda item: item.episode_id))


class TaskSuiteDerivePayloadV1(_FrozenModel):
    schema_version: Literal["task-suite-derive@1"] = "task-suite-derive@1"
    source_preview_artifact_id: BoundedId
    config_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId
    derivation_profile: ProfileRefV1
    environment_profile: ProfileRefV1
    completion_oracle_registry_ref: CompletionOracleRegistryRefV1


class PatchValidationPayloadV1(_FrozenModel):
    schema_version: Literal["patch-validation@1"] = "patch-validation@1"
    subject: ValidationSubjectBindingV1
    base_snapshot_artifact_id: BoundedId
    preview_snapshot_artifact_id: BoundedId
    candidate_config_export_artifact_ids: tuple[BoundedId, ...] = Field(
        max_length=MAX_COLLECTION_ITEMS
    )
    target: RefReadBindingV1
    validation_policy: ProfileRefV1
    checker_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    simulation_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    findings: tuple[FindingEvidenceBindingV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    review_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    playtest_trace_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    regression_suite_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @field_validator(
        "candidate_config_export_artifact_ids",
        "review_artifact_ids",
        "playtest_trace_artifact_ids",
        "regression_suite_artifact_ids",
    )
    @classmethod
    def _artifact_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)

    @field_validator("checker_profiles", "simulation_profiles")
    @classmethod
    def _profiles(cls, value: tuple[ProfileRefV1, ...]) -> tuple[ProfileRefV1, ...]:
        return _canonical_unique_models(value)

    @field_validator("findings")
    @classmethod
    def _findings(
        cls, value: tuple[FindingEvidenceBindingV1, ...]
    ) -> tuple[FindingEvidenceBindingV1, ...]:
        return _canonical_finding_bindings(value)


class ConstraintValidationPayloadV1(_FrozenModel):
    schema_version: Literal["constraint-validation@1"] = "constraint-validation@1"
    subject: ValidationSubjectBindingV1
    base_constraint_snapshot_artifact_id: BoundedId | None = None
    target: RefReadBindingV1
    dsl_grammar_version: BoundedNonEmptyStr
    compiler_profile: ProfileRefV1
    differential_engines: tuple[SolverEngineRefV1, ...] = Field(
        min_length=2, max_length=MAX_COLLECTION_ITEMS
    )
    golden_suite_artifact_id: BoundedId | None = None
    regression_suite_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    validation_policy: ProfileRefV1

    @field_validator("differential_engines")
    @classmethod
    def _engines(cls, value: tuple[SolverEngineRefV1, ...]) -> tuple[SolverEngineRefV1, ...]:
        keys = [(item.engine_id, item.version) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("differential engine refs must be unique")
        return tuple(sorted(value, key=lambda item: (item.engine_id, item.version)))

    @field_validator("regression_suite_artifact_ids")
    @classmethod
    def _regression_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)


class RollbackValidationPayloadV1(_FrozenModel):
    schema_version: Literal["rollback-validation@1"] = "rollback-validation@1"
    subject: ValidationSubjectBindingV1
    ref_name: BoundedId
    expected_current_ref: RefValue
    target_artifact_id: BoundedId
    target_history_revision: PositiveInt
    rollback_profile: ProfileRefV1
    schema_compatibility_policy: ProfileRefV1
    impact_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    regression_suite_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @field_validator("impact_profiles")
    @classmethod
    def _profiles(cls, value: tuple[ProfileRefV1, ...]) -> tuple[ProfileRefV1, ...]:
        return _canonical_unique_models(value)

    @field_validator("regression_suite_artifact_ids")
    @classmethod
    def _regression_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)


class BenchRunPayloadV1(_FrozenModel):
    schema_version: Literal["bench-run@1"] = "bench-run@1"
    dataset_artifact_id: BoundedId
    benchmark_spec_artifact_id: BoundedId
    partition_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    evaluator_profile: ProfileRefV1
    repetition_count: int = Field(ge=1, le=100_000)
    execution_scope: Literal["execute_cases", "aggregate_results"]
    # RunPayloadEnvelope and RunManifest both cap their complete input/parent
    # closure at MAX_COLLECTION_ITEMS. Dataset + spec occupy two mandatory input
    # slots and the final BenchReport occupies one terminal-manifest parent slot.
    case_result_artifact_ids: tuple[BoundedId, ...] = Field(
        max_length=MAX_BENCHMARK_AGGREGATE_RESULT_ARTIFACTS
    )

    @field_validator("partition_ids", "case_result_artifact_ids")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)

    @model_validator(mode="after")
    def _execution_scope_shape(self) -> "BenchRunPayloadV1":
        if self.execution_scope == "execute_cases" and self.case_result_artifact_ids:
            raise ValueError("execute_cases forbids precomputed case results")
        if self.execution_scope == "aggregate_results" and not self.case_result_artifact_ids:
            raise ValueError("aggregate_results requires case result artifacts")
        return self


class ArtifactMigrationPayloadV1(_FrozenModel):
    schema_version: Literal["artifact-migration@1"] = "artifact-migration@1"
    source_artifact_id: BoundedId
    target_payload_schema_id: BoundedId
    target_meta_schema_version: BoundedNonEmptyStr
    target_dsl_grammar_version: BoundedNonEmptyStr | None = None
    migrator: ProfileRefV1
    publish_mode: Literal["report_only", "publish_migrated_artifact"]


class DrDrillPayloadV1(_FrozenModel):
    schema_version: Literal["dr-drill@1"] = "dr-drill@1"
    dr_plan: ProfileRefV1
    recovery_catalog_entry_id: BoundedId
    expected_checkpoint_id: BoundedId
    restore_target_profile: ProfileRefV1
    verification_profile: ProfileRefV1
    destroy_restored_target_after_verification: bool


RunKindPayload: TypeAlias = Annotated[
    GenerationProposePayloadV1
    | PatchRepairPayloadV1
    | ConstraintProposalProposePayloadV1
    | ReviewRunPayloadV1
    | CheckerRunPayloadV1
    | SimulationRunPayloadV1
    | PlaytestRunPayloadV1
    | TaskSuiteDerivePayloadV1
    | PatchValidationPayloadV1
    | ConstraintValidationPayloadV1
    | RollbackValidationPayloadV1
    | BenchRunPayloadV1
    | ArtifactMigrationPayloadV1
    | DrDrillPayloadV1,
    Field(discriminator="schema_version"),
]


def patch_repair_requires_root_seed(params: RunKindPayload) -> bool:
    """Whether exact repair regression replay adds seeded execution authority.

    Regression suites seed every case through ``subseed@1`` even when the direct
    checker/repair profiles are deterministic.  Admission and retained-payload
    validation share this predicate so a regression-only repair cannot be admitted
    without a root seed, nor have that required seed rejected as profile drift.
    """

    return isinstance(params, PatchRepairPayloadV1) and bool(params.regression_suite_artifact_ids)


def _target_artifact_id(target: RefReadBindingV1) -> str | None:
    return target.expected_ref.artifact_id if target.expected_ref is not None else None


def _referenced_input_artifact_ids(params: RunKindPayload) -> tuple[str, ...]:
    ids: list[str | None]
    if isinstance(params, GenerationProposePayloadV1):
        ids = [
            params.base_snapshot_artifact_id,
            params.constraint_snapshot_artifact_id,
            params.objective_goal.source_artifact_id,
            _target_artifact_id(params.target),
            *(binding.evidence_artifact_id for binding in params.findings),
        ]
    elif isinstance(params, PatchRepairPayloadV1):
        ids = [
            params.subject_patch_artifact_id,
            params.base_snapshot_artifact_id,
            params.preview_snapshot_artifact_id,
            params.constraint_snapshot_artifact_id,
            params.validation_evidence_artifact_id,
            _target_artifact_id(params.target),
            *(binding.evidence_artifact_id for binding in params.findings),
            *params.regression_suite_artifact_ids,
        ]
    elif isinstance(params, ConstraintProposalProposePayloadV1):
        ids = [
            *params.source_artifact_ids,
            params.base_constraint_snapshot_artifact_id,
            params.authoring_goal.source_artifact_id,
        ]
    elif isinstance(params, ReviewRunPayloadV1):
        ids = [params.snapshot_artifact_id, params.constraint_snapshot_artifact_id]
    elif isinstance(params, CheckerRunPayloadV1):
        ids = [params.snapshot_artifact_id, params.constraint_snapshot_artifact_id]
    elif isinstance(params, SimulationRunPayloadV1):
        ids = [
            params.snapshot_artifact_id,
            params.constraint_snapshot_artifact_id,
            params.scenario_artifact_id,
        ]
    elif isinstance(params, PlaytestRunPayloadV1):
        ids = [
            params.config_artifact_id,
            params.constraint_snapshot_artifact_id,
            params.task_suite_artifact_id,
            *(binding.scenario_spec_artifact_id for binding in params.episodes),
        ]
    elif isinstance(params, TaskSuiteDerivePayloadV1):
        ids = [
            params.source_preview_artifact_id,
            params.config_artifact_id,
            params.constraint_snapshot_artifact_id,
        ]
    elif isinstance(params, PatchValidationPayloadV1):
        ids = [
            params.subject.subject_artifact_id,
            params.base_snapshot_artifact_id,
            params.preview_snapshot_artifact_id,
            *params.candidate_config_export_artifact_ids,
            _target_artifact_id(params.target),
            *(binding.evidence_artifact_id for binding in params.findings),
            *params.review_artifact_ids,
            *params.playtest_trace_artifact_ids,
            *params.regression_suite_artifact_ids,
        ]
    elif isinstance(params, ConstraintValidationPayloadV1):
        ids = [
            params.subject.subject_artifact_id,
            params.base_constraint_snapshot_artifact_id,
            _target_artifact_id(params.target),
            params.golden_suite_artifact_id,
            *params.regression_suite_artifact_ids,
        ]
    elif isinstance(params, RollbackValidationPayloadV1):
        ids = [
            params.subject.subject_artifact_id,
            params.expected_current_ref.artifact_id,
            params.target_artifact_id,
            *params.regression_suite_artifact_ids,
        ]
    elif isinstance(params, BenchRunPayloadV1):
        ids = [
            params.dataset_artifact_id,
            params.benchmark_spec_artifact_id,
            *params.case_result_artifact_ids,
        ]
    elif isinstance(params, ArtifactMigrationPayloadV1):
        ids = [params.source_artifact_id]
    else:
        ids = []
    return _canonical_unique_strings(tuple(value for value in ids if value is not None))


def referenced_input_artifact_ids(params: RunKindPayload) -> tuple[str, ...]:
    """Public view of the exact Artifact input ids referenced by ``params``.

    Run admission uses this to fill ``RunPayloadEnvelope.input_artifact_ids`` from a
    single source of truth; the envelope validator recomputes the same set and
    rejects any hidden extra input.
    """

    return _referenced_input_artifact_ids(params)


_RUN_KIND_PAYLOAD_SCHEMAS: dict[tuple[str, int], str] = {
    ("generation.propose", 1): "generation-propose@1",
    ("patch.repair", 1): "patch-repair@1",
    ("constraint_proposal.propose", 1): "constraint-proposal-propose@1",
    ("review.run", 1): "review-run@1",
    ("checker.run", 1): "checker-run@1",
    ("simulation.run", 1): "simulation-run@1",
    ("task_suite.derive", 1): "task-suite-derive@1",
    ("playtest.run", 1): "playtest-run@1",
    ("patch.validate", 1): "patch-validation@1",
    ("constraint_proposal.validate", 1): "constraint-validation@1",
    ("rollback.validate", 1): "rollback-validation@1",
    ("bench.run", 1): "bench-run@1",
    ("artifact.migrate", 1): "artifact-migration@1",
    ("dr.drill", 1): "dr-drill@1",
}


class RunPayloadEnvelope(_FrozenModel):
    payload_schema_version: BoundedNonEmptyStr
    input_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_PLAYTEST_RUN_INPUT_ARTIFACTS)
    version_tuple: VersionTuple
    execution_version_plan: ExecutionVersionPlanV1 | None = None
    policy_bindings: tuple[RunPolicyBindingV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    schema_bindings: tuple[RunSchemaBindingV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    execution_profile_catalog_version: PositiveInt
    execution_profile_catalog_digest: Sha256Hex
    resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...] = Field(
        max_length=MAX_COLLECTION_ITEMS
    )
    resolved_policy_snapshots: tuple[ResolvedPolicySnapshotV1, ...] = Field(
        max_length=MAX_COLLECTION_ITEMS
    )
    budget_set_snapshot_id: BoundedId
    seed: Uint64 | None = None
    llm_execution_mode: Literal["not_applicable", "live", "record", "replay"]
    cassette_artifact_id: BoundedId | None = None
    params: RunKindPayload

    @field_validator("input_artifact_ids")
    @classmethod
    def _canonical_inputs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)

    @field_validator("policy_bindings")
    @classmethod
    def _canonical_policies(
        cls, value: tuple[RunPolicyBindingV1, ...]
    ) -> tuple[RunPolicyBindingV1, ...]:
        keys = [item.binding_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("policy binding keys must be unique")
        return tuple(sorted(value, key=lambda item: item.binding_key))

    @field_validator("schema_bindings")
    @classmethod
    def _canonical_schemas(
        cls, value: tuple[RunSchemaBindingV1, ...]
    ) -> tuple[RunSchemaBindingV1, ...]:
        keys = [item.binding_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("schema binding keys must be unique")
        return tuple(sorted(value, key=lambda item: item.binding_key))

    @field_validator("resolved_profiles")
    @classmethod
    def _canonical_profiles(
        cls, value: tuple[ResolvedExecutionProfileBindingV1, ...]
    ) -> tuple[ResolvedExecutionProfileBindingV1, ...]:
        keys = [item.field_path for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("resolved profile field paths must be unique")
        return tuple(sorted(value, key=lambda item: item.field_path))

    @field_validator("resolved_policy_snapshots")
    @classmethod
    def _canonical_resolved_policies(
        cls, value: tuple[ResolvedPolicySnapshotV1, ...]
    ) -> tuple[ResolvedPolicySnapshotV1, ...]:
        keys = [item.resolved_policy_id for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("resolved policy ids must be unique")
        return tuple(sorted(value, key=lambda item: item.resolved_policy_id))

    @model_validator(mode="after")
    def _execution_mode_shape(self) -> "RunPayloadEnvelope":
        if self.payload_schema_version != self.params.schema_version:
            raise ValueError("payload schema id differs from typed Run kind payload")
        if self.llm_execution_mode == "not_applicable":
            if self.execution_version_plan is not None or self.cassette_artifact_id is not None:
                raise ValueError("not_applicable mode forbids plan and cassette")
        elif self.llm_execution_mode in {"live", "record"}:
            if self.execution_version_plan is None or self.cassette_artifact_id is not None:
                raise ValueError("live/record mode requires only an execution plan")
        elif self.execution_version_plan is None or self.cassette_artifact_id is None:
            raise ValueError("replay mode requires a plan and exact cassette artifact")
        referenced_inputs = list(_referenced_input_artifact_ids(self.params))
        if self.cassette_artifact_id is not None:
            referenced_inputs.append(self.cassette_artifact_id)
        expected_inputs = _canonical_unique_strings(tuple(referenced_inputs))
        if isinstance(self.params, DrDrillPayloadV1):
            dynamic_inputs = set(self.input_artifact_ids) - set(expected_inputs)
            if len(dynamic_inputs) != 1 or not set(expected_inputs).issubset(
                self.input_artifact_ids
            ):
                raise ValueError(
                    "input_artifact_ids must add exactly the verified recovery manifest"
                )
        elif self.input_artifact_ids != expected_inputs:
            raise ValueError(
                "input_artifact_ids must exactly match artifacts referenced by the Run payload"
            )
        return self


RunStatus = Literal[
    "queued",
    "leased",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
]


class RunRecord(_FrozenModel):
    run_schema_version: Literal["run@1"] = "run@1"
    run_id: NonEmptyStr
    kind: RunKindRef
    status: RunStatus
    revision: PositiveInt
    idempotency_scope: NonEmptyStr
    idempotency_key: NonEmptyStr
    request_hash: Sha256Hex
    payload: RunPayloadEnvelope
    payload_hash: Sha256Hex
    run_kind_definition_digest: Sha256Hex
    outcome_policy_set_digest: Sha256Hex
    migration_capability_matrix: MigrationCapabilityMatrixRefV1 | None = None
    failure_classifier: FailureClassifierRefV1
    dispatch_trace_carrier: RunDispatchTraceCarrierV1 | None = None
    initiated_by: AuditActor
    # Admission-resolved authority.  ``None`` is retained only for historical
    # compatibility and domain-independent Runs; the broad ``all`` selector is
    # never a resolved resource scope.
    resource_domain_scope: DomainScope | None = None
    queue_deadline_utc: NonEmptyStr
    attempt_timeout_ns: PositiveInt
    overall_deadline_utc: NonEmptyStr
    cancel_requested_at: NonEmptyStr | None = None
    cancel_requested_by: AuditActor | None = None
    current_attempt_no: PositiveInt | None = None
    next_attempt_no: PositiveInt
    next_fencing_token: PositiveInt
    next_event_seq: PositiveInt
    budget_set_snapshot_id: NonEmptyStr
    run_budget_hold_group_id: NonEmptyStr
    concurrency_permit_group_id: NonEmptyStr | None = None
    retry_policy: RetryPolicyRefV1
    max_attempts: PositiveInt
    retry_not_before_utc: NonEmptyStr | None = None
    result_artifact_id: NonEmptyStr | None = None
    failure_artifact_id: NonEmptyStr | None = None
    terminal_cassette_artifact_id: NonEmptyStr | None = None
    created_at: NonEmptyStr
    updated_at: NonEmptyStr

    @model_validator(mode="after")
    def _record_projection(self) -> "RunRecord":
        expected_payload_schema = _RUN_KIND_PAYLOAD_SCHEMAS.get((self.kind.kind, self.kind.version))
        if expected_payload_schema is None:
            raise ValueError("Run kind/version is not part of the frozen Run payload union")
        if self.payload.payload_schema_version != expected_payload_schema:
            raise ValueError("Run kind/version and payload schema differ")
        if self.payload_hash != canonical_payload_hash(self.payload):
            raise ValueError("payload_hash does not match the immutable Run payload")
        if self.budget_set_snapshot_id != self.payload.budget_set_snapshot_id:
            raise ValueError("Run budget projection differs from payload binding")
        if (self.cancel_requested_at is None) != (self.cancel_requested_by is None):
            raise ValueError("cancel requester and timestamp are all-or-none")
        terminal = self.status in {"succeeded", "failed", "cancelled", "timed_out"}
        if self.status == "succeeded":
            if self.result_artifact_id is None or self.failure_artifact_id is not None:
                raise ValueError("successful Run requires only a result manifest")
        elif self.status in {"failed", "cancelled", "timed_out"}:
            if self.failure_artifact_id is None or self.result_artifact_id is not None:
                raise ValueError("non-success terminal Run requires only a failure manifest")
        elif self.result_artifact_id is not None or self.failure_artifact_id is not None:
            raise ValueError("non-terminal Run cannot publish a terminal manifest")
        if not terminal:
            if self.terminal_cassette_artifact_id is not None:
                raise ValueError("non-terminal Run cannot publish a cassette")
        elif self.payload.llm_execution_mode == "record":
            if self.terminal_cassette_artifact_id is None:
                raise ValueError("record Run requires a terminal cassette")
        elif self.payload.llm_execution_mode == "replay":
            if self.terminal_cassette_artifact_id != self.payload.cassette_artifact_id:
                raise ValueError("replay Run requires its exact input cassette")
        elif self.terminal_cassette_artifact_id is not None:
            raise ValueError(
                f"{self.payload.llm_execution_mode} Run does not publish a terminal cassette"
            )
        if self.status == "retry_wait" and self.retry_not_before_utc is None:
            raise ValueError("retry_wait requires a retry_not_before timestamp")
        if self.status != "retry_wait" and self.retry_not_before_utc is not None:
            raise ValueError("only retry_wait may carry retry_not_before")
        return self


RunAttemptStatus = Literal[
    "leased", "running", "succeeded", "failed", "cancelled", "timed_out", "lease_expired"
]


class RunAttempt(_FrozenModel):
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    status: RunAttemptStatus
    fencing_token: PositiveInt
    worker_principal_id: NonEmptyStr
    trace_id: NonEmptyStr | None = None
    next_call_ordinal: PositiveInt
    started_at: NonEmptyStr | None = None
    attempt_deadline_utc: NonEmptyStr | None = None
    ended_at: NonEmptyStr | None = None
    failure_class: FailureClassV1 | None = None
    retryable: bool | None = None
    failure_artifact_id: NonEmptyStr | None = None
    cassette_bundle_artifact_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _terminal_projection(self) -> "RunAttempt":
        has_started_at = self.started_at is not None
        has_attempt_deadline = self.attempt_deadline_utc is not None
        if has_started_at != has_attempt_deadline:
            raise ValueError("attempt start timing fields are all-or-none")
        if self.status == "leased" and has_started_at:
            raise ValueError("leased attempt cannot contain start timing")
        if self.status in {"running", "succeeded"} and not has_started_at:
            raise ValueError(f"{self.status} attempt requires start timing")
        failure = (self.failure_class, self.retryable, self.failure_artifact_id)
        if self.status in {"leased", "running"}:
            if self.ended_at is not None or any(value is not None for value in failure):
                raise ValueError("active attempt cannot contain terminal projections")
            if self.cassette_bundle_artifact_id is not None:
                raise ValueError("active attempt cannot publish a cassette bundle")
        elif self.status == "succeeded":
            if self.ended_at is None or any(value is not None for value in failure):
                raise ValueError("successful attempt has no failure projection")
        elif self.ended_at is None or any(value is None for value in failure):
            raise ValueError("non-success terminal attempt requires its failure projection")
        return self


class RunLease(_FrozenModel):
    lease_id: NonEmptyStr
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    fencing_token: PositiveInt
    lease_version: PositiveInt
    owner_principal_id: NonEmptyStr
    acquired_at: NonEmptyStr
    heartbeat_at: NonEmptyStr
    expires_at: NonEmptyStr
    status: Literal["active", "closed", "expired"]


class RunQueuedDataV1(_FrozenModel):
    data_schema_version: Literal["run-queued@1"] = "run-queued@1"
    run_kind: RunKindRef
    queue_deadline_utc: BoundedNonEmptyStr
    overall_deadline_utc: BoundedNonEmptyStr


class CancelRequestedDataV1(_FrozenModel):
    data_schema_version: Literal["cancel-requested@1"] = "cancel-requested@1"
    command_id: BoundedId
    reason_code: BoundedId


class CommandAcceptedDataV1(_FrozenModel):
    data_schema_version: Literal["command-accepted@1"] = "command-accepted@1"
    command_id: BoundedId
    command_type: Literal["cancel", "provide_input"]
    command_revision: PositiveInt


class AttemptLeasedDataV1(_FrozenModel):
    data_schema_version: Literal["attempt-leased@1"] = "attempt-leased@1"
    attempt_no: PositiveInt
    lease_expires_at: BoundedNonEmptyStr


class AttemptStartedDataV1(_FrozenModel):
    data_schema_version: Literal["attempt-started@1"] = "attempt-started@1"
    attempt_no: PositiveInt
    started_at: BoundedNonEmptyStr
    attempt_deadline_utc: BoundedNonEmptyStr


class AttemptProgressDataV1(_FrozenModel):
    data_schema_version: Literal["attempt-progress@1"] = "attempt-progress@1"
    attempt_no: PositiveInt
    phase_code: BoundedId
    completed_units: NonNegativeInt
    total_units: NonNegativeInt | None = None
    detail_artifact_id: BoundedId | None = None

    @model_validator(mode="after")
    def _progress_range(self) -> "AttemptProgressDataV1":
        if self.total_units is not None and self.completed_units > self.total_units:
            raise ValueError("completed units cannot exceed total units")
        return self


class LeaseExpiredDataV1(_FrozenModel):
    data_schema_version: Literal["lease-expired@1"] = "lease-expired@1"
    attempt_no: PositiveInt
    failure_artifact_id: BoundedId
    will_retry: bool


class RetryScheduledDataV1(_FrozenModel):
    data_schema_version: Literal["retry-scheduled@1"] = "retry-scheduled@1"
    attempt_no: PositiveInt
    failure_artifact_id: BoundedId
    cause_code: BoundedId
    failure_class: FailureClassV1
    retry_decision: RetryDecisionV1
    retry_not_before_utc: BoundedNonEmptyStr

    @model_validator(mode="after")
    def _decision_projection(self) -> "RetryScheduledDataV1":
        decision = self.retry_decision
        if decision.decision != "retry":
            raise ValueError("retry-scheduled event requires a retry decision")
        if (
            self.cause_code != decision.cause_code
            or self.failure_class != decision.failure_class
            or self.retry_not_before_utc != decision.retry_not_before_utc
        ):
            raise ValueError("retry schedule differs from its retry decision")
        return self


class CommandOutcomeDataV1(_FrozenModel):
    data_schema_version: Literal["command-outcome@1"] = "command-outcome@1"
    command_id: BoundedId
    command_type: Literal["cancel", "provide_input"]
    command_revision: PositiveInt
    outcome_code: BoundedId


class RunSucceededDataV1(_FrozenModel):
    data_schema_version: Literal["run-succeeded@1"] = "run-succeeded@1"
    attempt_no: PositiveInt
    result_artifact_id: BoundedId


class RunTerminatedDataV1(_FrozenModel):
    data_schema_version: Literal["run-terminated@1"] = "run-terminated@1"
    attempt_no: PositiveInt | None = None
    failure_artifact_id: BoundedId
    cause_code: BoundedId


RunEventData: TypeAlias = Annotated[
    RunQueuedDataV1
    | CancelRequestedDataV1
    | CommandAcceptedDataV1
    | AttemptLeasedDataV1
    | AttemptStartedDataV1
    | AttemptProgressDataV1
    | LeaseExpiredDataV1
    | RetryScheduledDataV1
    | CommandOutcomeDataV1
    | RunSucceededDataV1
    | RunTerminatedDataV1,
    Field(discriminator="data_schema_version"),
]

RunEventType = Literal[
    "run.queued",
    "run.cancel_requested",
    "run.command_accepted",
    "attempt.leased",
    "attempt.started",
    "attempt.progress",
    "attempt.lease_expired",
    "attempt.retry_scheduled",
    "run.command_applied",
    "run.command_rejected",
    "run.succeeded",
    "run.failed",
    "run.cancelled",
    "run.timed_out",
]
RunEventSourceStatus = Literal[
    "create",
    "queued",
    "leased",
    "running",
    "retry_wait",
]

_RUN_EVENT_STATUS_ORDER = {
    "create": 0,
    "queued": 1,
    "leased": 2,
    "running": 3,
    "retry_wait": 4,
}
_RUN_EVENT_DEFINITIONS: dict[
    str, tuple[str, Literal["run", "attempt", "either"], bool, tuple[str, ...]]
] = {
    "run.queued": ("run-queued@1", "run", False, ("create",)),
    "run.cancel_requested": (
        "cancel-requested@1",
        "run",
        False,
        ("queued", "leased", "running", "retry_wait"),
    ),
    "run.command_accepted": (
        "command-accepted@1",
        "run",
        False,
        ("leased", "running"),
    ),
    "attempt.leased": (
        "attempt-leased@1",
        "attempt",
        False,
        ("queued", "retry_wait"),
    ),
    "attempt.started": ("attempt-started@1", "attempt", False, ("leased",)),
    "attempt.progress": ("attempt-progress@1", "attempt", False, ("running",)),
    "attempt.lease_expired": (
        "lease-expired@1",
        "attempt",
        False,
        ("leased", "running"),
    ),
    "attempt.retry_scheduled": (
        "retry-scheduled@1",
        "attempt",
        False,
        ("leased", "running"),
    ),
    "run.command_applied": (
        "command-outcome@1",
        "either",
        False,
        ("queued", "leased", "running", "retry_wait"),
    ),
    "run.command_rejected": (
        "command-outcome@1",
        "either",
        False,
        ("queued", "leased", "running", "retry_wait"),
    ),
    "run.succeeded": ("run-succeeded@1", "attempt", True, ("running",)),
    "run.failed": (
        "run-terminated@1",
        "either",
        True,
        ("queued", "leased", "running", "retry_wait"),
    ),
    "run.cancelled": (
        "run-terminated@1",
        "either",
        True,
        ("queued", "leased", "running", "retry_wait"),
    ),
    "run.timed_out": (
        "run-terminated@1",
        "either",
        True,
        ("queued", "leased", "running", "retry_wait"),
    ),
}


class RunEventDefinitionV1(_FrozenModel):
    event_type: RunEventType
    data_schema_id: BoundedId
    attempt_scope: Literal["run", "attempt", "either"]
    terminal: bool
    allowed_from_statuses: tuple[RunEventSourceStatus, ...] = Field(min_length=1, max_length=5)

    @field_validator("allowed_from_statuses")
    @classmethod
    def _statuses(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("event source statuses must be unique")
        return tuple(sorted(value, key=_RUN_EVENT_STATUS_ORDER.__getitem__))

    @model_validator(mode="after")
    def _matches_frozen_definition(self) -> "RunEventDefinitionV1":
        expected = _RUN_EVENT_DEFINITIONS[self.event_type]
        actual = (
            self.data_schema_id,
            self.attempt_scope,
            self.terminal,
            self.allowed_from_statuses,
        )
        if actual != expected:
            raise ValueError("event definition differs from the frozen state-machine table")
        return self


def run_event_registry_digest(payload: Mapping[str, Any] | BaseModel) -> str:
    raw = _json_data(payload)
    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if key != "registry_digest"}
        raw.setdefault("registry_schema_version", "run-event-registry@1")
        raw["definitions"] = sorted(raw.get("definitions", []), key=lambda item: item["event_type"])
    return canonical_sha256(raw)


class RunEventRegistryV1(_FrozenModel):
    registry_schema_version: Literal["run-event-registry@1"] = "run-event-registry@1"
    registry_version: PositiveInt
    definitions: tuple[RunEventDefinitionV1, ...] = Field(
        min_length=len(_RUN_EVENT_DEFINITIONS),
        max_length=len(_RUN_EVENT_DEFINITIONS),
    )
    registry_digest: Sha256Hex

    @field_validator("definitions")
    @classmethod
    def _definitions(
        cls, value: tuple[RunEventDefinitionV1, ...]
    ) -> tuple[RunEventDefinitionV1, ...]:
        event_types = [item.event_type for item in value]
        if len(event_types) != len(set(event_types)):
            raise ValueError("Run event types must be unique")
        if set(event_types) != set(_RUN_EVENT_DEFINITIONS):
            raise ValueError("Run event registry must cover the frozen event table")
        return tuple(sorted(value, key=lambda item: item.event_type))

    @model_validator(mode="after")
    def _digest(self) -> "RunEventRegistryV1":
        if self.registry_digest != run_event_registry_digest(self):
            raise ValueError("Run event registry digest does not match its payload")
        return self


class RunEvent(_FrozenModel):
    event_schema_version: Literal["run-event@1"] = "run-event@1"
    run_id: BoundedId
    seq: PositiveInt
    event_type: RunEventType
    attempt_no: PositiveInt | None = None
    occurred_at: BoundedNonEmptyStr
    data_schema_version: BoundedId
    data: RunEventData
    trace_id: BoundedId | None = None

    @model_validator(mode="after")
    def _typed_event_projection(self) -> "RunEvent":
        expected_schema, scope, _, _ = _RUN_EVENT_DEFINITIONS[self.event_type]
        if (
            self.data_schema_version != expected_schema
            or self.data.data_schema_version != expected_schema
        ):
            raise ValueError("event type and data schema do not match")
        if scope == "run" and self.attempt_no is not None:
            raise ValueError("run-scoped event cannot carry an attempt number")
        if scope == "attempt" and self.attempt_no is None:
            raise ValueError("attempt-scoped event requires an attempt number")
        data_attempt_no = getattr(self.data, "attempt_no", None)
        if scope == "attempt" and data_attempt_no != self.attempt_no:
            raise ValueError("event and data attempt numbers differ")
        if scope == "either" and self.attempt_no is not None and hasattr(self.data, "attempt_no"):
            if data_attempt_no != self.attempt_no:
                raise ValueError("attempt-scoped event and data attempt numbers differ")
        return self


RunEventEnvelope = RunEvent


class CancelRunPayloadV1(_FrozenModel):
    schema_version: Literal["run-cancel@1"] = "run-cancel@1"
    reason_code: BoundedId
    comment: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None


class PlaytestProvideInputPayloadV1(_FrozenModel):
    schema_version: Literal["playtest-provide-input@1"] = "playtest-provide-input@1"
    interaction_id: BoundedId
    expected_state_hash: Sha256Hex
    choice_id: BoundedId


RunCommandPayload: TypeAlias = Annotated[
    CancelRunPayloadV1 | PlaytestProvideInputPayloadV1,
    Field(discriminator="schema_version"),
]

_COMMAND_PAYLOAD_SCHEMAS = {
    "cancel": "run-cancel@1",
    "provide_input": "playtest-provide-input@1",
}


class RunCommandV1(_FrozenModel):
    command_schema_version: Literal["run-command@1"] = "run-command@1"
    command_id: BoundedId
    client_id: BoundedId
    client_seq: PositiveInt
    idempotency_key: BoundedId
    expected_run_revision: PositiveInt
    type: Literal["cancel", "provide_input"]
    payload_schema_id: Literal["run-cancel@1", "playtest-provide-input@1"]
    payload: RunCommandPayload

    @model_validator(mode="after")
    def _payload_projection(self) -> "RunCommandV1":
        expected = _COMMAND_PAYLOAD_SCHEMAS[self.type]
        if self.payload_schema_id != expected or self.payload.schema_version != expected:
            raise ValueError("command type and payload schema do not match")
        return self


RunCommandStatus = Literal["pending", "claimed", "applied", "rejected"]


class RunCommandRecordV1(_FrozenModel):
    record_schema_version: Literal["run-command-record@1"] = "run-command-record@1"
    run_id: BoundedId
    command: RunCommandV1
    request_hash: Sha256Hex
    actor: AuditActor
    status: RunCommandStatus
    revision: PositiveInt
    created_at: BoundedNonEmptyStr
    claimed_at: BoundedNonEmptyStr | None = None
    claimed_attempt_no: PositiveInt | None = None
    claimed_fencing_token: PositiveInt | None = None
    applied_at: BoundedNonEmptyStr | None = None
    result_event_seq: PositiveInt | None = None
    rejection_code: BoundedId | None = None

    @model_validator(mode="after")
    def _record_shape(self) -> "RunCommandRecordV1":
        if self.request_hash != canonical_payload_hash(self.command):
            raise ValueError("command request hash does not match its canonical payload")
        claim = (self.claimed_at, self.claimed_attempt_no, self.claimed_fencing_token)
        if any(value is None for value in claim) and any(value is not None for value in claim):
            raise ValueError("command claim fields are all-or-none")
        claimed = all(value is not None for value in claim)
        if self.status == "pending":
            if claimed or any(
                value is not None
                for value in (self.applied_at, self.result_event_seq, self.rejection_code)
            ):
                raise ValueError("pending command cannot contain outcome fields")
        elif self.status == "claimed":
            if not claimed or any(
                value is not None
                for value in (self.applied_at, self.result_event_seq, self.rejection_code)
            ):
                raise ValueError("claimed command requires only claim fields")
        else:
            if self.applied_at is None or self.result_event_seq is None:
                raise ValueError("terminal command requires an outcome event and timestamp")
            if (self.status == "rejected") != (self.rejection_code is not None):
                raise ValueError("rejection code belongs only to rejected commands")
        if self.command.type == "cancel":
            if self.status != "applied" or claimed:
                raise ValueError("cancel command is atomically applied without a worker claim")
        elif self.status == "applied" and not claimed:
            raise ValueError("provide_input apply requires a current worker claim")
        elif self.status == "rejected" and not claimed and self.rejection_code != "run_terminal":
            raise ValueError(
                "unclaimed provide_input may only be rejected when the Run is terminal"
            )
        return self


class RunCommandViewV1(_FrozenModel):
    run_id: BoundedId
    command_id: BoundedId
    client_id: BoundedId
    client_seq: PositiveInt
    type: Literal["cancel", "provide_input"]
    payload_schema_id: Literal["run-cancel@1", "playtest-provide-input@1"]
    status: RunCommandStatus
    revision: PositiveInt
    created_at: BoundedNonEmptyStr
    applied_at: BoundedNonEmptyStr | None = None
    result_event_seq: PositiveInt | None = None
    rejection_code: BoundedId | None = None

    @model_validator(mode="after")
    def _outcome_shape(self) -> "RunCommandViewV1":
        if self.payload_schema_id != _COMMAND_PAYLOAD_SCHEMAS[self.type]:
            raise ValueError("command type and payload schema do not match")
        terminal = self.status in {"applied", "rejected"}
        if terminal != (self.applied_at is not None) or terminal != (
            self.result_event_seq is not None
        ):
            raise ValueError("command view terminal projection is incomplete")
        if (self.status == "rejected") != (self.rejection_code is not None):
            raise ValueError("command view rejection projection is inconsistent")
        return self


class RunCommandAckV1(_FrozenModel):
    ack_schema_version: Literal["run-command-ack@1"] = "run-command-ack@1"
    command_id: BoundedId
    client_id: BoundedId
    client_seq: PositiveInt
    status: Literal["accepted", "duplicate"]
    persisted_status: RunCommandStatus
    command_revision: PositiveInt
    run_revision: PositiveInt


class Problem(_FrozenModel):
    type: BoundedNonEmptyStr
    title: BoundedNonEmptyStr
    status: int = Field(ge=100, le=599)
    detail: BoundedNonEmptyStr
    instance: BoundedNonEmptyStr
    code: BoundedId
    request_id: BoundedId
    run_id: BoundedId | None = None
    trace_id: BoundedId | None = None
    errors: tuple[dict[BoundedJsonKey, JsonValue], ...] | None = Field(
        default=None, max_length=MAX_COLLECTION_ITEMS
    )
    retry_after_s: float | None = Field(default=None, ge=0)
    earliest_cursor: BoundedNonEmptyStr | None = None
    conflict_set_id: BoundedId | None = None

    @field_validator("errors")
    @classmethod
    def _errors(
        cls,
        value: tuple[dict[str, JsonValue], ...] | None,
    ) -> tuple[dict[str, JsonValue], ...] | None:
        if value is not None:
            _validate_bounded_json(list(value), field_name="errors")
        return value


class RunCommandProblemV1(_FrozenModel):
    problem_schema_version: Literal["run-command-problem@1"] = "run-command-problem@1"
    command_id: BoundedId | None = None
    client_seq: PositiveInt | None = None
    problem: Problem


class RunIntermediateArtifactLinkV1(_FrozenModel):
    link_schema_version: Literal["run-intermediate-link@1"] = "run-intermediate-link@1"
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    call_ordinal: PositiveInt
    # Added compatibly to the permanent @1 wire: retained pre-M4 rows are route 1.
    route_ordinal: PositiveInt = 1
    artifact_id: NonEmptyStr
    role: Literal["prompt_rendered"]
    request_hash: Sha256Hex
    fencing_token: PositiveInt
    published_at: NonEmptyStr


AgentPromptContextKind = Literal[
    "generation",
    "repair_initial",
    "repair_refine",
    "review_triage",
    "bench_agent_case",
    "constraint_extraction",
    "playtest",
]


class AgentPromptSourceMessageV1(_FrozenModel):
    """One exact non-system message carried by a governed prompt context."""

    message_schema_version: Literal["agent-prompt-source-message@1"] = (
        "agent-prompt-source-message@1"
    )
    role: Literal["user", "assistant", "tool"]
    content: Annotated[
        str,
        StringConstraints(min_length=1, max_length=MAX_AGENT_PROMPT_CONTEXT_MESSAGE_BYTES),
    ]
    tool_calls: tuple[dict[BoundedJsonKey, JsonValue], ...] = Field(
        default=(), max_length=MAX_COLLECTION_ITEMS
    )
    purpose: Literal["context", "tool_output"]

    @field_validator("tool_calls")
    @classmethod
    def _bounded_tool_calls(
        cls,
        value: tuple[dict[str, JsonValue], ...],
    ) -> tuple[dict[str, JsonValue], ...]:
        _validate_bounded_json(list(value), field_name="agent prompt message tool_calls")
        return value

    @model_validator(mode="after")
    def _purpose_matches_role(self) -> "AgentPromptSourceMessageV1":
        expected = "context" if self.role == "user" else "tool_output"
        if self.purpose != expected:
            raise ValueError("agent prompt message purpose does not match its role")
        if self.role == "user" and self.tool_calls:
            raise ValueError("user agent prompt messages cannot carry tool calls")
        if len(self.content.encode("utf-8")) > MAX_AGENT_PROMPT_CONTEXT_MESSAGE_BYTES:
            raise ValueError("agent prompt message exceeds its UTF-8 byte limit")
        return self


class AgentPromptSemanticBindingV1(_FrozenModel):
    """Opaque-but-typed exact identity used by a node-specific context assembler."""

    binding_key: BoundedId
    subject_id: BoundedId
    subject_digest: Sha256Hex
    subject_revision: PositiveInt | None = None


class AgentPromptArtifactBindingV1(_FrozenModel):
    binding_key: BoundedId
    artifact_id: BoundedId
    artifact_kind: ArtifactKind
    payload_schema_id: BoundedId
    payload_hash: Sha256Hex


class AgentPromptPriorConsumptionV1(_FrozenModel):
    """Exact prior prompt/route/response-consumption identity for a refine call."""

    attempt_no: PositiveInt
    call_ordinal: PositiveInt
    route_ordinal: PositiveInt
    prompt_artifact_id: BoundedId
    request_hash: Sha256Hex
    routing_decision_kind: Literal["native", "legacy_import"]
    routing_decision_id: BoundedId
    execution_source: Literal["online", "full_response_cache", "cassette_replay"]
    reservation_group_id: BoundedId
    transport_attempt: PositiveInt | None = None
    cassette_shard_artifact_id: BoundedId | None = None
    cassette_source_artifact_id: BoundedId | None = None
    response_digest: Sha256Hex

    @model_validator(mode="after")
    def _execution_shape(self) -> "AgentPromptPriorConsumptionV1":
        if self.execution_source == "online" and self.transport_attempt is None:
            raise ValueError("online prior consumption requires transport_attempt")
        if self.execution_source != "online" and self.transport_attempt is not None:
            raise ValueError("cache/replay prior consumption has no transport_attempt")
        if self.execution_source == "cassette_replay" and self.cassette_source_artifact_id is None:
            raise ValueError("replay prior consumption requires its cassette source")
        return self


def validate_agent_prompt_context_kind(
    *,
    agent_node_id: str,
    context_kind: AgentPromptContextKind,
    target_call_ordinal: int,
    prior_consumption: AgentPromptPriorConsumptionV1 | None,
) -> None:
    """Validate the immutable node/kind/causal mapping for a prompt context."""

    if target_call_ordinal < 1:
        raise ValueError("Agent prompt context target call ordinal must be positive")
    fixed_kinds: dict[str, AgentPromptContextKind] = {
        "generation": "generation",
        "review-triage": "review_triage",
        "bench-agent-case": "bench_agent_case",
        "extraction": "constraint_extraction",
    }
    if agent_node_id == "repair":
        if context_kind not in {"repair_initial", "repair_refine"}:
            raise ValueError("repair Agent prompt context has another context kind")
        if context_kind == "repair_refine" and target_call_ordinal == 1:
            raise ValueError("repair refine context cannot be the first logical call")
        if target_call_ordinal > 1 and prior_consumption is None:
            raise ValueError("later repair context requires prior response consumption")
        if prior_consumption is not None and (
            prior_consumption.call_ordinal != target_call_ordinal - 1
        ):
            raise ValueError("repair context prior consumption is not immediate")
        return
    expected = (
        "playtest" if agent_node_id.startswith("playtest.") else fixed_kinds.get(agent_node_id)
    )
    if expected is None or context_kind != expected:
        raise ValueError("Agent node and prompt context kind do not match")
    if agent_node_id in {"generation", "review-triage", "extraction"} and (
        target_call_ordinal != 1 or prior_consumption is not None
    ):
        raise ValueError("single-call Agent prompt context must be the first call without prior")


class AgentPromptContextDraftV1(_FrozenModel):
    """Non-authoritative per-call draft supplied by a node-specific assembler."""

    draft_schema_version: Literal["agent-prompt-context-draft@1"] = "agent-prompt-context-draft@1"
    context_kind: AgentPromptContextKind
    messages: tuple[AgentPromptSourceMessageV1, ...] = Field(
        min_length=1, max_length=MAX_AGENT_PROMPT_CONTEXT_MESSAGES
    )
    source_artifact_ids: tuple[BoundedId, ...] = Field(
        min_length=1, max_length=MAX_PLAYTEST_PROMPT_SOURCE_ARTIFACTS
    )
    semantic_bindings: tuple[AgentPromptSemanticBindingV1, ...] = Field(
        default=(), max_length=MAX_COLLECTION_ITEMS
    )
    include_previous_consumption: bool = False

    @field_validator("source_artifact_ids")
    @classmethod
    def _canonical_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, allow_empty=False)

    @field_validator("semantic_bindings")
    @classmethod
    def _canonical_semantics(
        cls,
        value: tuple[AgentPromptSemanticBindingV1, ...],
    ) -> tuple[AgentPromptSemanticBindingV1, ...]:
        keys = [item.binding_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("agent prompt semantic binding keys must be unique")
        return tuple(sorted(value, key=lambda item: item.binding_key))

    @model_validator(mode="after")
    def _refine_shape(self) -> "AgentPromptContextDraftV1":
        if self.context_kind == "repair_refine" and not self.include_previous_consumption:
            raise ValueError("repair refine context requires prior response consumption")
        return self


class AgentPromptContextV1(_FrozenModel):
    """Canonical bounded tool-output source for one exact logical model call."""

    context_schema_version: Literal["agent-prompt-context@1"] = "agent-prompt-context@1"
    context_kind: AgentPromptContextKind
    run_id: BoundedId
    attempt_no: PositiveInt
    target_call_ordinal: PositiveInt
    agent_node_id: BoundedId
    prompt_version: BoundedId
    messages: tuple[AgentPromptSourceMessageV1, ...] = Field(
        min_length=1, max_length=MAX_AGENT_PROMPT_CONTEXT_MESSAGES
    )
    upstream_artifacts: tuple[AgentPromptArtifactBindingV1, ...] = Field(
        min_length=1, max_length=MAX_PLAYTEST_PROMPT_UPSTREAM_ARTIFACTS
    )
    semantic_bindings: tuple[AgentPromptSemanticBindingV1, ...] = Field(
        default=(), max_length=MAX_COLLECTION_ITEMS
    )
    prior_consumption: AgentPromptPriorConsumptionV1 | None = None

    @field_validator("upstream_artifacts")
    @classmethod
    def _canonical_upstream(
        cls,
        value: tuple[AgentPromptArtifactBindingV1, ...],
    ) -> tuple[AgentPromptArtifactBindingV1, ...]:
        keys = [item.binding_key for item in value]
        ids = [item.artifact_id for item in value]
        if len(keys) != len(set(keys)) or len(ids) != len(set(ids)):
            raise ValueError("agent prompt upstream bindings must have unique keys and artifacts")
        return tuple(sorted(value, key=lambda item: item.binding_key))

    @field_validator("semantic_bindings")
    @classmethod
    def _canonical_context_semantics(
        cls,
        value: tuple[AgentPromptSemanticBindingV1, ...],
    ) -> tuple[AgentPromptSemanticBindingV1, ...]:
        keys = [item.binding_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("agent prompt semantic binding keys must be unique")
        return tuple(sorted(value, key=lambda item: item.binding_key))

    @model_validator(mode="after")
    def _closed_shape(self) -> "AgentPromptContextV1":
        has_prior = self.prior_consumption is not None
        validate_agent_prompt_context_kind(
            agent_node_id=self.agent_node_id,
            context_kind=self.context_kind,
            target_call_ordinal=self.target_call_ordinal,
            prior_consumption=self.prior_consumption,
        )
        if self.context_kind == "repair_refine" and not has_prior:
            raise ValueError("repair refine context requires prior response consumption")
        if self.prior_consumption is not None:
            prior = self.prior_consumption
            if (
                prior.attempt_no != self.attempt_no
                or prior.call_ordinal != self.target_call_ordinal - 1
            ):
                raise ValueError("prior consumption is not the immediately previous call")
            by_key = {item.binding_key: item for item in self.upstream_artifacts}
            expected_prior_keys = {"prior.prompt"}
            if prior.cassette_source_artifact_id is not None:
                expected_prior_keys.add("prior.cassette_source")
            actual_prior_keys = {key for key in by_key if not key.startswith("source:")}
            if (
                actual_prior_keys != expected_prior_keys
                or by_key["prior.prompt"].artifact_id != prior.prompt_artifact_id
                or (
                    prior.cassette_source_artifact_id is not None
                    and by_key["prior.cassette_source"].artifact_id
                    != prior.cassette_source_artifact_id
                )
            ):
                raise ValueError("prior consumption direct Artifact parents are not exact")
        elif any(not item.binding_key.startswith("source:") for item in self.upstream_artifacts):
            raise ValueError("context without prior consumption has prior Artifact parents")
        if (
            len(canonical_json(self.model_dump(mode="json")).encode("utf-8"))
            > MAX_AGENT_PROMPT_CONTEXT_BYTES
        ):
            raise ValueError("agent prompt context exceeds its canonical byte limit")
        return self


class RunToolIntermediateLinkV1(_FrozenModel):
    """Independent fenced link for one exact Agent prompt-context source."""

    link_schema_version: Literal["run-tool-intermediate-link@1"] = "run-tool-intermediate-link@1"
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    target_call_ordinal: PositiveInt
    artifact_id: NonEmptyStr
    role: Literal["agent_prompt_context"] = "agent_prompt_context"
    agent_node_id: BoundedId
    prompt_version: BoundedId
    payload_hash: Sha256Hex
    fencing_token: PositiveInt
    published_at: NonEmptyStr


class RunModelRouteLinkV1(_FrozenModel):
    """Immutable authority for one attempted route of one logical model call."""

    link_schema_version: Literal["run-model-route-link@1"] = "run-model-route-link@1"
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    call_ordinal: PositiveInt
    route_ordinal: PositiveInt
    prompt_artifact_id: NonEmptyStr
    request_hash: Sha256Hex
    routing_decision_kind: Literal["native", "legacy_import"]
    routing_decision_id: NonEmptyStr
    fencing_token: PositiveInt
    published_at: NonEmptyStr


class RunModelResponseConsumptionV1(_FrozenModel):
    """Committed response-consumption authority for one exact attempted route."""

    consumption_schema_version: Literal["run-model-response-consumption@1"] = (
        "run-model-response-consumption@1"
    )
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    call_ordinal: PositiveInt
    route_ordinal: PositiveInt
    execution_source: Literal["online", "full_response_cache", "cassette_replay"]
    reservation_group_id: NonEmptyStr
    transport_attempt: PositiveInt | None = None
    cassette_shard_artifact_id: NonEmptyStr | None = None
    # Additive @1 compatibility: pre-0010 rows cannot prove response bytes.
    response_digest: Sha256Hex | None = None
    consumed_at: NonEmptyStr

    @model_validator(mode="after")
    def _execution_shape(self) -> "RunModelResponseConsumptionV1":
        if self.execution_source == "online" and self.transport_attempt is None:
            raise ValueError("online response consumption requires transport_attempt")
        if self.execution_source != "online" and self.transport_attempt is not None:
            raise ValueError("cache/replay response consumption has no transport_attempt")
        return self


class RunFindingLinkV1(_FrozenModel):
    link_schema_version: Literal["run-finding-link@1"] = "run-finding-link@1"
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    ordinal: PositiveInt
    finding_id: NonEmptyStr
    finding_revision: PositiveInt
    finding_digest: Sha256Hex
    evidence_artifact_id: NonEmptyStr


class RunManifestParentBindingV1(_FrozenModel):
    artifact_id: BoundedNonEmptyStr
    role: Literal["input", "intermediate", "output", "evidence"]
    publication: Literal["existing", "run_published"]
    attempt_no: PositiveInt | None = None
    ordinal: PositiveInt | None = None
    cassette_scope: (
        Literal["record_shard", "attempt_bundle", "run_bundle", "replay_input"] | None
    ) = None

    @model_validator(mode="after")
    def _cassette_role(self) -> "RunManifestParentBindingV1":
        if self.cassette_scope == "replay_input" and (
            self.role != "input" or self.publication != "existing"
        ):
            raise ValueError("replay cassette must be an existing input")
        if self.cassette_scope in {"record_shard", "attempt_bundle", "run_bundle"} and (
            self.role != "intermediate" or self.publication != "run_published"
        ):
            raise ValueError("recorded cassette must be a published intermediate")
        return self


def _parent_key(
    parent: RunManifestParentBindingV1,
) -> tuple[str, int, int, str, str]:
    return (
        parent.role,
        parent.attempt_no or 0,
        parent.ordinal or 0,
        parent.cassette_scope or "",
        parent.artifact_id,
    )


class RunManifestVersionProjectionV1(_FrozenModel):
    projection_schema_version: Literal["run-manifest-version-projection@1"] = (
        "run-manifest-version-projection@1"
    )
    manifest_scope: Literal["attempt", "run"]
    attempt_no: PositiveInt | None = None
    run_kind: RunKindRef
    run_payload_hash: Sha256Hex
    frozen_input_version_tuple: VersionTuple
    terminal_version_tuple: VersionTuple
    version_transition_policy_ref: VersionTransitionPolicyRefV1
    parents: tuple[RunManifestParentBindingV1, ...] = Field(
        max_length=MAX_RUN_MANIFEST_PARENT_BINDINGS
    )

    @field_validator("parents")
    @classmethod
    def _canonical_parents(
        cls, value: tuple[RunManifestParentBindingV1, ...]
    ) -> tuple[RunManifestParentBindingV1, ...]:
        ids = [parent.artifact_id for parent in value]
        if len(ids) != len(set(ids)):
            raise ValueError("manifest parent artifact ids must be unique")
        return tuple(sorted(value, key=_parent_key))

    @model_validator(mode="after")
    def _scope(self) -> "RunManifestVersionProjectionV1":
        if self.manifest_scope == "attempt" and self.attempt_no is None:
            raise ValueError("attempt manifest requires attempt_no")
        return self


class RequirementDispositionV1(_FrozenModel):
    resolved_policy_id: BoundedNonEmptyStr
    outcome_rule_id: BoundedNonEmptyStr
    requirement_id: BoundedNonEmptyStr
    status: Literal["produced", "not_executed"]
    reason_code: BoundedNonEmptyStr | None = None

    @model_validator(mode="after")
    def _reason(self) -> "RequirementDispositionV1":
        if self.status == "produced" and self.reason_code is not None:
            raise ValueError("produced requirement cannot have a reason")
        if self.status == "not_executed" and self.reason_code is None:
            raise ValueError("not-executed requirement requires a reason")
        return self


class PreparedArtifact(_FrozenModel):
    kind: ArtifactKind
    payload_schema_id: BoundedNonEmptyStr
    version_tuple: VersionTuple
    lineage: tuple[BoundedNonEmptyStr, ...] = Field(max_length=MAX_PLAYTEST_TRACE_LINEAGE_PARENTS)
    payload_hash: Sha256Hex
    meta: dict[BoundedJsonKey, JsonValue] = Field(max_length=MAX_COLLECTION_ITEMS)
    object_ref: ObjectRef
    location: ObjectLocation

    @field_validator("lineage")
    @classmethod
    def _canonical_lineage(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)

    @field_validator("meta")
    @classmethod
    def _meta(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _validate_bounded_json(value, field_name="meta")
        return value

    @model_validator(mode="after")
    def _object_binding(self) -> "PreparedArtifact":
        if self.object_ref.key != self.location.key:
            raise ValueError("prepared object ref and location keys differ")
        return self


class PreparedFindingV1(_FrozenModel):
    finding_id: BoundedNonEmptyStr
    expected_previous_revision: PositiveInt | None
    evidence_artifact_index: NonNegativeInt
    payload: FindingPayloadV1


class PreparedRunResultSummaryV1(_FrozenModel):
    summary_schema_version: Literal["prepared-run-result-summary@1"] = (
        "prepared-run-result-summary@1"
    )
    outcome_code: BoundedNonEmptyStr
    primary_artifact_kind: ArtifactKind
    prepared_domain_artifact_count: NonNegativeInt
    prepared_finding_count: NonNegativeInt


class RunResultSummaryV1(_FrozenModel):
    summary_schema_version: Literal["run-result-summary@1"] = "run-result-summary@1"
    outcome_code: BoundedNonEmptyStr
    primary_artifact_kind: ArtifactKind
    produced_artifact_count: PositiveInt
    finding_count: NonNegativeInt


def _canonical_dispositions(
    value: tuple[RequirementDispositionV1, ...],
) -> tuple[RequirementDispositionV1, ...]:
    def key(item: RequirementDispositionV1) -> tuple[str, str, str]:
        return (
            item.resolved_policy_id,
            item.outcome_rule_id,
            item.requirement_id,
        )

    keys = [key(item) for item in value]
    if len(keys) != len(set(keys)):
        raise ValueError("requirement dispositions must have unique identities")
    return tuple(sorted(value, key=key))


class PreparedRunResult(_FrozenModel):
    prepared_schema_version: Literal["prepared-run-result@1"] = "prepared-run-result@1"
    run_id: BoundedNonEmptyStr
    attempt_no: PositiveInt
    run_kind: RunKindRef
    primary_index: NonNegativeInt
    artifacts: tuple[PreparedArtifact, ...] = Field(
        min_length=1, max_length=MAX_PREPARED_DOMAIN_ARTIFACTS
    )
    findings: tuple[PreparedFindingV1, ...] = Field(max_length=MAX_PREPARED_FINDINGS)
    requirement_dispositions: tuple[RequirementDispositionV1, ...] = Field(
        max_length=MAX_COLLECTION_ITEMS
    )
    summary: PreparedRunResultSummaryV1

    @field_validator("requirement_dispositions")
    @classmethod
    def _dispositions(
        cls, value: tuple[RequirementDispositionV1, ...]
    ) -> tuple[RequirementDispositionV1, ...]:
        return _canonical_dispositions(value)

    @model_validator(mode="after")
    def _summary_projection(self) -> "PreparedRunResult":
        if self.primary_index >= len(self.artifacts):
            raise ValueError("primary_index is outside prepared artifacts")
        if self.summary.prepared_domain_artifact_count != len(self.artifacts):
            raise ValueError("prepared artifact count does not match")
        if self.summary.prepared_finding_count != len(self.findings):
            raise ValueError("prepared finding count does not match")
        if self.summary.primary_artifact_kind != self.artifacts[self.primary_index].kind:
            raise ValueError("prepared primary kind does not match primary artifact")
        if sum(artifact.object_ref.size_bytes for artifact in self.artifacts) > (
            MAX_PREPARED_OUTCOME_BYTES
        ):
            raise ValueError("prepared outcome exceeds the frozen aggregate byte bound")
        for finding in self.findings:
            if finding.evidence_artifact_index >= len(self.artifacts):
                raise ValueError("finding evidence index is outside prepared artifacts")
            if finding.payload.producer_run_id != self.run_id:
                raise ValueError("prepared finding producer_run_id differs from Run")
        return self


class PreparedRunFailure(_FrozenModel):
    prepared_schema_version: Literal["prepared-run-failure@1"] = "prepared-run-failure@1"
    run_id: BoundedNonEmptyStr
    attempt_no: PositiveInt | None = None
    run_kind: RunKindRef
    artifacts: tuple[PreparedArtifact, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    requirement_dispositions: tuple[RequirementDispositionV1, ...] = Field(
        max_length=MAX_COLLECTION_ITEMS
    )
    cause_code: BoundedNonEmptyStr
    failure_class: FailureClassV1
    intrinsic_retry_eligible: bool
    classifier: FailureClassifierRefV1
    dependency: DependencyFailureV1 | None = None
    redacted_message: BoundedNonEmptyStr

    @field_validator("requirement_dispositions")
    @classmethod
    def _dispositions(
        cls, value: tuple[RequirementDispositionV1, ...]
    ) -> tuple[RequirementDispositionV1, ...]:
        return _canonical_dispositions(value)

    @model_validator(mode="after")
    def _dependency_shape(self) -> "PreparedRunFailure":
        requires_dependency = self.failure_class in {
            "transient_dependency",
            "permanent_dependency",
        }
        if requires_dependency != (self.dependency is not None):
            raise ValueError("dependency failure projection does not match failure class")
        if sum(artifact.object_ref.size_bytes for artifact in self.artifacts) > (
            MAX_PREPARED_OUTCOME_BYTES
        ):
            raise ValueError("prepared failure exceeds the frozen aggregate byte bound")
        return self


PreparedRunOutcome = Annotated[
    PreparedRunResult | PreparedRunFailure,
    Field(discriminator="prepared_schema_version"),
]


class RunResultV1(_FrozenModel):
    result_schema_version: Literal["run-result@1"] = "run-result@1"
    run_id: BoundedNonEmptyStr
    attempt_no: PositiveInt
    run_kind: RunKindRef
    primary_artifact_id: BoundedNonEmptyStr
    produced_artifact_ids: tuple[BoundedNonEmptyStr, ...] = Field(
        min_length=1, max_length=MAX_RUN_MANIFEST_PARENT_BINDINGS
    )
    finding_count: NonNegativeInt
    outcome_code: BoundedNonEmptyStr
    summary: RunResultSummaryV1
    requirement_dispositions: tuple[RequirementDispositionV1, ...] = Field(
        max_length=MAX_COLLECTION_ITEMS
    )
    version_projection: RunManifestVersionProjectionV1

    @field_validator("produced_artifact_ids")
    @classmethod
    def _canonical_outputs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, allow_empty=False)

    @field_validator("requirement_dispositions")
    @classmethod
    def _dispositions(
        cls, value: tuple[RequirementDispositionV1, ...]
    ) -> tuple[RequirementDispositionV1, ...]:
        return _canonical_dispositions(value)

    @model_validator(mode="after")
    def _result_projection(self) -> "RunResultV1":
        if self.primary_artifact_id not in self.produced_artifact_ids:
            raise ValueError("primary artifact must be a produced artifact")
        if self.summary.outcome_code != self.outcome_code:
            raise ValueError("result outcome differs from summary")
        if self.summary.produced_artifact_count != len(self.produced_artifact_ids):
            raise ValueError("produced artifact count differs from summary")
        if self.summary.finding_count != self.finding_count:
            raise ValueError("finding count differs from summary")
        if self.version_projection.manifest_scope != "run":
            raise ValueError("Run result requires a run-scope version projection")
        if self.version_projection.attempt_no != self.attempt_no:
            raise ValueError("result attempt differs from version projection")
        if self.version_projection.run_kind != self.run_kind:
            raise ValueError("result Run kind differs from version projection")
        return self


class RunFailureV1(_FrozenModel):
    failure_schema_version: Literal["run-failure@1"] = "run-failure@1"
    run_id: BoundedNonEmptyStr
    attempt_no: PositiveInt | None = None
    run_kind: RunKindRef
    cause_code: BoundedNonEmptyStr
    failure_class: FailureClassV1
    retryable: bool
    retry_decision: RetryDecisionV1
    dependency: DependencyFailureV1 | None = None
    redacted_message: BoundedNonEmptyStr
    evidence_artifact_ids: tuple[BoundedNonEmptyStr, ...] = Field(
        max_length=MAX_RUN_MANIFEST_PARENT_BINDINGS
    )
    requirement_dispositions: tuple[RequirementDispositionV1, ...] = Field(
        max_length=MAX_COLLECTION_ITEMS
    )
    occurred_at: BoundedNonEmptyStr
    version_projection: RunManifestVersionProjectionV1

    @field_validator("evidence_artifact_ids")
    @classmethod
    def _canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)

    @field_validator("requirement_dispositions")
    @classmethod
    def _dispositions(
        cls, value: tuple[RequirementDispositionV1, ...]
    ) -> tuple[RequirementDispositionV1, ...]:
        return _canonical_dispositions(value)

    @model_validator(mode="after")
    def _failure_projection(self) -> "RunFailureV1":
        decision = self.retry_decision
        if (
            self.cause_code != decision.cause_code
            or self.failure_class != decision.failure_class
            or self.retryable != (decision.decision == "retry")
        ):
            raise ValueError("failure and retry-decision projections differ")
        requires_dependency = self.failure_class in {
            "transient_dependency",
            "permanent_dependency",
        }
        if requires_dependency != (self.dependency is not None):
            raise ValueError("dependency projection differs from failure class")
        if self.version_projection.attempt_no != self.attempt_no:
            raise ValueError("failure attempt differs from version projection")
        if self.version_projection.run_kind != self.run_kind:
            raise ValueError("failure Run kind differs from version projection")
        return self


class TerminalPublisherHooks(_FrozenModel):
    on_success: NonEmptyStr
    on_failure: NonEmptyStr
    on_cancel: NonEmptyStr
    on_timeout: NonEmptyStr


class ArtifactIdentityBindingV1(_FrozenModel):
    collection_item_pointer: JsonPointer | None = None
    artifact_value_source: Literal["artifact_id", "payload"]
    artifact_payload_pointer: JsonPointer | None = None

    @model_validator(mode="after")
    def _source_pointer(self) -> "ArtifactIdentityBindingV1":
        if self.artifact_value_source == "payload":
            if self.artifact_payload_pointer is None:
                raise ValueError("payload identity requires artifact_payload_pointer")
        elif self.artifact_payload_pointer is not None:
            raise ValueError("artifact-id identity forbids a payload pointer")
        return self


class JsonCollectionCountBindingV1(_FrozenModel):
    source: Literal["run_payload", "prepared_primary_payload"]
    collection_pointer: JsonPointer
    identity_binding: ArtifactIdentityBindingV1 | None = None


class ResolvedPolicyCountBindingV1(_FrozenModel):
    source: Literal["resolved_policy_snapshot"] = "resolved_policy_snapshot"
    resolved_policy_id: NonEmptyStr
    outcome_rule_id: NonEmptyStr
    identity_binding: ArtifactIdentityBindingV1


class ResolvedPolicySubsetCountBindingV1(_FrozenModel):
    source: Literal["resolved_policy_subset"] = "resolved_policy_subset"
    resolved_policy_id: NonEmptyStr
    outcome_rule_id: NonEmptyStr
    allowed_not_executed_reason_codes: tuple[NonEmptyStr, ...]
    identity_binding: ArtifactIdentityBindingV1

    @field_validator("allowed_not_executed_reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)


class IntermediateCountBindingV1(_FrozenModel):
    source: Literal["published_intermediate_links"] = "published_intermediate_links"
    link_role: Literal["prompt_rendered", "agent_prompt_context"]
    scope: Literal["current_attempt", "all_attempts"]


class ExecutionIdentityCountBindingV1(_FrozenModel):
    """Count only logical calls whose provider response entered Agent state.

    Prompt-render links are intentionally a superset: the prompt is committed
    before reserve/provider work, so a crash or exhausted provider call can leave
    a durable ordinal without a response shard.  RECORD shard cardinality must
    therefore close against the transaction-bound terminal ExecutionIdentity,
    never against prompt-link count.
    """

    source: Literal["execution_identity"] = "execution_identity"
    response_consumed: Literal[True] = True
    scope: Literal["current_attempt", "all_attempts"]


class ExecutionModeCountsV1(_FrozenModel):
    not_applicable: NonNegativeInt
    live: NonNegativeInt
    record: NonNegativeInt
    replay: NonNegativeInt


class ExecutionModeCountBindingV1(_FrozenModel):
    source: Literal["execution_mode"] = "execution_mode"
    exact_count_by_mode: ExecutionModeCountsV1


ArtifactCountBindingV1 = Annotated[
    JsonCollectionCountBindingV1
    | ResolvedPolicyCountBindingV1
    | ResolvedPolicySubsetCountBindingV1
    | IntermediateCountBindingV1
    | ExecutionIdentityCountBindingV1
    | ExecutionModeCountBindingV1,
    Field(discriminator="source"),
]


class ArtifactParentRuleV1(_FrozenModel):
    parent_role: NonEmptyStr
    source: Literal["run_input", "run_intermediate", "prepared_rule", "child_payload_reference"]
    source_rule_id: NonEmptyStr | None = None
    child_payload_pointer: JsonPointer | None = None
    artifact_kinds: tuple[ArtifactKind, ...] = Field(min_length=1)
    payload_schema_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    min_count: NonNegativeInt
    max_count: NonNegativeInt | None = None
    direct_parent: Literal[True] = True

    @field_validator("artifact_kinds", "payload_schema_ids")
    @classmethod
    def _stable_allowlists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, allow_empty=False)

    @model_validator(mode="after")
    def _source_fields(self) -> "ArtifactParentRuleV1":
        binding_source = self.source
        if (binding_source == "prepared_rule") != (self.source_rule_id is not None):
            raise ValueError("source_rule_id belongs only to prepared_rule")
        if (binding_source == "child_payload_reference") != (
            self.child_payload_pointer is not None
        ):
            raise ValueError("child_payload_pointer belongs only to child_payload_reference")
        if self.max_count is not None and self.max_count < self.min_count:
            raise ValueError("max_count cannot be below min_count")
        return self


VersionTupleField = Literal[
    "doc_version",
    "ir_snapshot_id",
    "constraint_snapshot_id",
    "prompt_version",
    "model_snapshot",
    "agent_graph_version",
    "tool_version",
    "env_contract_version",
    "seed",
    "cassette_id",
]


class VersionFieldProjectionRuleV1(_FrozenModel):
    field: VersionTupleField
    source: Literal["producer_value", "parent_role", "constant_null"]
    parent_role: NonEmptyStr | None = None
    equality_parent_roles: tuple[NonEmptyStr, ...]

    @field_validator("equality_parent_roles")
    @classmethod
    def _roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)

    @model_validator(mode="after")
    def _parent_source(self) -> "VersionFieldProjectionRuleV1":
        projection_source = self.source
        if (projection_source == "parent_role") != (self.parent_role is not None):
            raise ValueError("parent_role field belongs only to parent_role source")
        return self


def artifact_lineage_policy_digest(payload: Mapping[str, Any] | BaseModel) -> str:
    raw = _json_data(payload)
    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if key != "digest"}
        raw["parent_rules"] = sorted(
            raw.get("parent_rules", []), key=lambda item: item["parent_role"]
        )
        raw["version_projection"] = sorted(
            raw.get("version_projection", []), key=lambda item: item["field"]
        )
    return canonical_sha256(raw)


class ArtifactLineagePolicyV1(_FrozenModel):
    policy_schema_version: NonEmptyStr
    policy_id: NonEmptyStr
    policy_version: PositiveInt
    child_kind: ArtifactKind
    child_payload_schema_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    parent_rules: tuple[ArtifactParentRuleV1, ...]
    version_projection: tuple[VersionFieldProjectionRuleV1, ...]
    allow_unmatched_parents: Literal[False] = False

    @field_validator("child_payload_schema_ids")
    @classmethod
    def _child_schemas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, allow_empty=False)

    @field_validator("parent_rules")
    @classmethod
    def _parent_rules(
        cls, value: tuple[ArtifactParentRuleV1, ...]
    ) -> tuple[ArtifactParentRuleV1, ...]:
        roles = [item.parent_role for item in value]
        if len(roles) != len(set(roles)):
            raise ValueError("lineage parent roles must be unique")
        return tuple(sorted(value, key=lambda item: item.parent_role))

    @field_validator("version_projection")
    @classmethod
    def _complete_projection(
        cls, value: tuple[VersionFieldProjectionRuleV1, ...]
    ) -> tuple[VersionFieldProjectionRuleV1, ...]:
        field_order = list(VersionTuple.model_fields)
        by_field = {item.field: item for item in value}
        if len(by_field) != len(value) or set(by_field) != set(field_order):
            raise ValueError("version projection must cover every VersionTuple field once")
        return tuple(by_field[field] for field in field_order)


class OutcomeArtifactRuleV1(_FrozenModel):
    rule_id: NonEmptyStr
    role: Literal["primary", "output", "evidence"]
    artifact_kind: ArtifactKind
    payload_schema_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    min_count: NonNegativeInt
    max_count: NonNegativeInt | None = None
    count_binding: ArtifactCountBindingV1 | None = None
    lineage_policy_ref: ArtifactLineagePolicyRefV1

    @field_validator("payload_schema_ids")
    @classmethod
    def _schemas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, allow_empty=False)

    @model_validator(mode="after")
    def _count_range(self) -> "OutcomeArtifactRuleV1":
        if self.max_count is not None and self.max_count < self.min_count:
            raise ValueError("max_count cannot be below min_count")
        if self.role == "primary" and (self.min_count != 1 or self.max_count != 1):
            raise ValueError("primary artifact rule cardinality must be exactly one")
        return self


TransitionOperation = Literal[
    "copy_frozen",
    "set_null_no_invocation",
    "set_from_execution_identity",
    "set_from_exact_cassette_parent",
]


class VersionTransitionFieldRuleV1(_FrozenModel):
    field: VersionTupleField
    operation: TransitionOperation
    cassette_scope: (
        Literal["record_shard", "attempt_bundle", "run_bundle", "replay_input"] | None
    ) = None

    @model_validator(mode="after")
    def _cassette_operation(self) -> "VersionTransitionFieldRuleV1":
        required = self.operation == "set_from_exact_cassette_parent"
        if required != (self.cassette_scope is not None):
            raise ValueError("cassette_scope belongs only to exact cassette transitions")
        return self


class VersionTransitionModeRuleV1(_FrozenModel):
    llm_execution_mode: Literal["not_applicable", "live", "record", "replay"]
    field_rules: tuple[VersionTransitionFieldRuleV1, ...]

    @field_validator("field_rules")
    @classmethod
    def _complete_fields(
        cls, value: tuple[VersionTransitionFieldRuleV1, ...]
    ) -> tuple[VersionTransitionFieldRuleV1, ...]:
        field_order = list(VersionTuple.model_fields)
        by_field = {item.field: item for item in value}
        if len(by_field) != len(value) or set(by_field) != set(field_order):
            raise ValueError("transition rule must cover every VersionTuple field once")
        return tuple(by_field[field] for field in field_order)


class VersionTransitionPolicyV1(_FrozenModel):
    policy_schema_version: NonEmptyStr
    policy_id: NonEmptyStr
    policy_version: PositiveInt
    manifest_scope: Literal["attempt", "run"]
    mode_rules: tuple[VersionTransitionModeRuleV1, ...]

    @field_validator("mode_rules")
    @classmethod
    def _complete_modes(
        cls, value: tuple[VersionTransitionModeRuleV1, ...]
    ) -> tuple[VersionTransitionModeRuleV1, ...]:
        order = ("not_applicable", "live", "record", "replay")
        by_mode = {item.llm_execution_mode: item for item in value}
        if len(by_mode) != len(value) or set(by_mode) != set(order):
            raise ValueError("transition policy must cover every execution mode once")
        return tuple(by_mode[mode] for mode in order)


class OutcomeArtifactPolicyV1(_FrozenModel):
    policy_schema_version: NonEmptyStr
    policy_id: NonEmptyStr
    policy_version: PositiveInt
    outcome_code: NonEmptyStr
    prepared_outcome: Literal["success", "failure"]
    publication_scope: Literal["attempt", "run"]
    attempt_terminal_status: Literal["failed", "cancelled", "timed_out", "lease_expired"] | None = (
        None
    )
    run_status_after_publication: Literal[
        "retry_wait", "succeeded", "failed", "cancelled", "timed_out"
    ]
    failure_class: FailureClassV1 | None = None
    retry_disposition: Literal["retry", "terminal"] | None = None
    artifact_rules: tuple[OutcomeArtifactRuleV1, ...]
    workflow_effect_key: NonEmptyStr
    version_transition_policy_ref: VersionTransitionPolicyRefV1

    @field_validator("artifact_rules")
    @classmethod
    def _canonical_artifact_rules(
        cls, value: tuple[OutcomeArtifactRuleV1, ...]
    ) -> tuple[OutcomeArtifactRuleV1, ...]:
        ids = [item.rule_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("outcome artifact rule ids must be unique")
        return tuple(sorted(value, key=lambda item: item.rule_id))

    @model_validator(mode="after")
    def _selector_shape(self) -> "OutcomeArtifactPolicyV1":
        failure = self.prepared_outcome == "failure"
        if failure != (self.failure_class is not None) or failure != (
            self.retry_disposition is not None
        ):
            raise ValueError("failure selector fields are all-or-none")
        if self.publication_scope == "attempt":
            if not failure or self.attempt_terminal_status is None:
                raise ValueError("attempt publication is a typed failure close")
        elif self.prepared_outcome == "success":
            if self.run_status_after_publication != "succeeded":
                raise ValueError("success policy must publish a succeeded Run")
            if self.attempt_terminal_status is not None:
                raise ValueError("success policy cannot close a failed attempt")
        return self


class FindingOutputPolicyRefV1(_FrozenModel):
    policy_id: NonEmptyStr
    policy_version: PositiveInt
    digest: Sha256Hex


class FindingOutputPolicyV1(_FrozenModel):
    policy_schema_version: Literal["finding-output-policy@1"] = "finding-output-policy@1"
    policy_id: NonEmptyStr
    policy_version: PositiveInt
    max_findings: NonNegativeInt
    allowed_evidence_outcome_rule_ids: tuple[NonEmptyStr, ...]
    allowed_oracle_types: tuple[OracleType, ...]
    allowed_sources: tuple[FindingSource, ...]

    @field_validator("allowed_evidence_outcome_rule_ids", "allowed_oracle_types", "allowed_sources")
    @classmethod
    def _stable_allowlists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)


class RuntimeParentRuleSetRef(_FrozenModel):
    rule_set_id: NonEmptyStr
    version: PositiveInt
    digest: Sha256Hex


class RuntimeParentRuleV1(_FrozenModel):
    rule_id: NonEmptyStr
    manifest_scope: Literal["attempt", "run", "both"]
    source: Literal[
        "run_input",
        "published_intermediate",
        "record_shard",
        "attempt_bundle",
        "run_bundle",
        "closed_attempt_failure",
    ]
    parent_role: Literal["input", "intermediate"]
    artifact_kind: ArtifactKind
    payload_schema_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    attempt_selector: Literal["none", "current", "all_closed"]
    enabled_execution_modes: tuple[Literal["not_applicable", "live", "record", "replay"], ...] = (
        "not_applicable",
        "live",
        "record",
        "replay",
    )
    min_count: NonNegativeInt
    max_count: NonNegativeInt | None = None
    count_binding: ArtifactCountBindingV1 | None = None

    @field_validator("payload_schema_ids")
    @classmethod
    def _schemas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, allow_empty=False)

    @field_validator("enabled_execution_modes")
    @classmethod
    def _execution_modes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        order = {"not_applicable": 0, "live": 1, "record": 2, "replay": 3}
        if not value or len(value) != len(set(value)):
            raise ValueError("runtime-parent execution modes must be non-empty and unique")
        return tuple(sorted(value, key=order.__getitem__))

    @model_validator(mode="after")
    def _range(self) -> "RuntimeParentRuleV1":
        if self.max_count is not None and self.max_count < self.min_count:
            raise ValueError("max_count cannot be below min_count")
        return self


class RuntimeParentRuleSetV1(_FrozenModel):
    rule_set_schema_version: Literal["runtime-parent-rules@1"] = "runtime-parent-rules@1"
    rule_set_id: NonEmptyStr
    version: PositiveInt
    rules: tuple[RuntimeParentRuleV1, ...]

    @field_validator("rules")
    @classmethod
    def _canonical_rules(
        cls, value: tuple[RuntimeParentRuleV1, ...]
    ) -> tuple[RuntimeParentRuleV1, ...]:
        ids = [item.rule_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("runtime parent rule ids must be unique")
        return tuple(sorted(value, key=lambda item: item.rule_id))


class RunKindDefinition(_FrozenModel):
    definition_schema_version: Literal["run-kind-definition@1"] = "run-kind-definition@1"
    kind: NonEmptyStr
    version: PositiveInt
    status: Literal["active", "disabled"]
    payload_schema_id: NonEmptyStr
    prepared_result_schema_id: Literal["prepared-run-result@1"]
    prepared_failure_schema_id: Literal["prepared-run-failure@1"]
    result_schema_id: Literal["run-result@1"]
    failure_schema_id: Literal["run-failure@1"]
    outcome_policies: tuple[OutcomeArtifactPolicyV1, ...]
    runtime_parent_rule_set: RuntimeParentRuleSetRef
    finding_output_policy_ref: FindingOutputPolicyRefV1 | None = None
    allowed_command_schema_ids: tuple[NonEmptyStr, ...]
    creation_mode: Literal["generic_runs_endpoint", "resource_endpoint_only", "internal_only"]
    allowed_llm_execution_modes: tuple[Literal["not_applicable", "live", "record", "replay"], ...]
    seed_policy: Literal["required", "forbidden", "profile_dependent"]
    seed_derivation_version: NonEmptyStr | None = None
    required_permission: Permission
    executor_key: NonEmptyStr
    terminal_hooks: TerminalPublisherHooks
    failure_classifier: FailureClassifierRefV1
    retry_policy: RetryPolicyRefV1
    migration_capability_matrix: MigrationCapabilityMatrixRefV1 | None = None

    @field_validator("allowed_command_schema_ids")
    @classmethod
    def _commands(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value)

    @field_validator("allowed_llm_execution_modes")
    @classmethod
    def _modes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        order = {"not_applicable": 0, "live": 1, "record": 2, "replay": 3}
        if len(value) != len(set(value)):
            raise ValueError("allowed execution modes must be unique")
        return tuple(sorted(value, key=order.__getitem__))

    @field_validator("outcome_policies")
    @classmethod
    def _policies(
        cls, value: tuple[OutcomeArtifactPolicyV1, ...]
    ) -> tuple[OutcomeArtifactPolicyV1, ...]:
        ids = [(item.policy_id, item.policy_version) for item in value]
        selectors = [
            (
                item.outcome_code,
                item.prepared_outcome,
                item.publication_scope,
                item.attempt_terminal_status,
                item.run_status_after_publication,
                item.failure_class,
                item.retry_disposition,
            )
            for item in value
        ]
        if len(ids) != len(set(ids)) or len(selectors) != len(set(selectors)):
            raise ValueError("outcome policies need unique refs and selectors")
        return tuple(sorted(value, key=lambda item: (item.policy_id, item.policy_version)))

    @model_validator(mode="after")
    def _seed_and_migration(self) -> "RunKindDefinition":
        if self.seed_policy in {"required", "profile_dependent"}:
            if self.seed_derivation_version is None:
                raise ValueError("seeded Run kinds need a derivation version")
        elif self.seed_derivation_version is not None:
            raise ValueError("seed-forbidden Run kinds cannot carry a derivation version")
        if (self.kind == "artifact.migrate") != (self.migration_capability_matrix is not None):
            raise ValueError("only artifact.migrate binds a capability matrix")
        return self


def run_kind_definition_digest(definition: RunKindDefinition) -> str:
    return canonical_sha256(definition.model_dump(mode="json"))


def outcome_policy_set_digest(
    run_kind: RunKindRef, policies: tuple[OutcomeArtifactPolicyV1, ...]
) -> str:
    ordered = sorted(policies, key=lambda item: (item.policy_id, item.policy_version))
    return canonical_sha256(
        {
            "policy_set_schema_version": "outcome-policy-set@1",
            "run_kind": run_kind.model_dump(mode="json"),
            "policies": [item.model_dump(mode="json") for item in ordered],
        }
    )
