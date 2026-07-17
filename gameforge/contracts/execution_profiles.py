"""Versioned execution-profile catalog and migration capability contracts."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.identity import DomainScope, SubjectKind
from gameforge.contracts.lineage import ArtifactKind

MAX_IDENTIFIER_LENGTH = 512
MAX_JSON_POINTER_LENGTH = 4096

# ``@1`` profile documents are retained authority, not trusted process-local
# configuration.  Keep an immutable platform envelope here so a corrupt or
# malicious catalog row cannot turn a self-declared profile maximum into an
# unbounded checker/simulator allocation.  A future, intentionally larger
# envelope requires a new config-schema version.
MAX_PROFILE_ALLOWLIST_ITEMS_V1 = 256
MAX_CHECKER_DIRECT_COUNT_V1 = 3
MAX_CHECKER_CONSTRAINT_COUNT_V1 = 256
MAX_CHECKER_WORK_UNITS_V1 = 2_000_000
MAX_SIMULATION_POPULATION_V1 = 10_000
MAX_SIMULATION_HORIZON_STEPS_V1 = 100_000
MAX_SIMULATION_OUTPUT_TICKS_V1 = 100_000
MAX_SIMULATION_WORK_UNITS_V1 = 20_000_000
MAX_WORKLOAD_REPLICATION_COUNT_V1 = 10_000
MAX_WORKLOAD_REPLICATION_TICKS_V1 = 2_000_000
MAX_WORKLOAD_WORK_UNITS_V1 = 20_000_000
MAX_EXECUTION_PROFILE_COLLECTION_V1 = 64
MAX_CANDIDATE_EXPORT_PROFILES_V1 = 16
MAX_REPAIR_REGRESSION_SUITES_V1 = 64
MAX_REPAIR_REGRESSION_WORK_UNITS_V1 = 20_000_000
MAX_REPAIR_REGRESSION_SUITE_BYTES_V1 = 17 * 1024 * 1024
MAX_REPAIR_TOTAL_REGRESSION_SUITE_BYTES_V1 = 64 * 1024 * 1024
MAX_PREPARED_OUTCOME_BYTES_V1 = 256 * 1024 * 1024
MAX_EXTRACTION_SOURCE_ARTIFACTS_V1 = 64
MAX_EXTRACTION_SOURCE_BYTES_V1 = 4 * 1024 * 1024
MAX_EXTRACTION_TOTAL_INPUT_BYTES_V1 = 16 * 1024 * 1024
MAX_EXTRACTION_PROPOSALS_V1 = 256
MAX_EXTRACTION_OUTPUT_BYTES_V1 = 8 * 1024 * 1024
MAX_AGENT_PROMPT_MESSAGE_BYTES_V1 = 17 * 1024 * 1024
MAX_ENVIRONMENT_NAVIGATION_GRID_CELLS_V1 = 1_000_000
MAX_TASK_SUITE_SCENARIOS_V1 = 1024
MAX_PLAYTEST_EPISODES_V1 = 1024
MAX_PLAYTEST_STEPS_PER_EPISODE_V1 = 1_000_000
MAX_PLAYTEST_TOTAL_STEPS_V1 = 1_000_000
MAX_PLAYTEST_TOTAL_MODEL_CALLS_V1 = 6_000_000
MAX_PLAYTEST_TOTAL_TRACE_BYTES_V1 = 256 * 1024 * 1024
# One RECORD attempt stores one cassette shard child per consumed logical call.
# ``CassetteBundleV1`` currently caps that exact child closure at 4096; a profile
# cannot authorize more calls until a versioned multi-level bundle exists.
MAX_PLAYTEST_TOTAL_MODEL_CALLS_V2 = 4096

NonEmptyStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_IDENTIFIER_LENGTH),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
JsonPointer = Annotated[
    str,
    StringConstraints(max_length=MAX_JSON_POINTER_LENGTH, pattern=r"^(?:|/.*)$"),
]


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


def _stable_strings(values: tuple[str, ...], *, allow_empty: bool = True) -> tuple[str, ...]:
    canonical = tuple(sorted(set(values)))
    if not allow_empty and not canonical:
        raise ValueError("collection must be non-empty")
    return canonical


def _is_json_pointer(value: str) -> bool:
    if value == "":
        return True
    if not value.startswith("/"):
        return False
    index = 0
    while index < len(value):
        if value[index] != "~":
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            return False
        index += 2
    return True


class RunKindRef(_FrozenModel):
    kind: NonEmptyStr
    version: int = Field(ge=1)


class ProfileRefV1(_FrozenModel):
    profile_id: NonEmptyStr
    version: int = Field(ge=1)


class AutoApplyPolicyRegistryRefV1(_FrozenModel):
    registry_version: NonEmptyStr
    registry_digest: Sha256Hex


class AutoApplyPolicyRefV1(_FrozenModel):
    registry: AutoApplyPolicyRegistryRefV1
    policy_id: NonEmptyStr
    policy_version: NonEmptyStr
    policy_digest: Sha256Hex


ExecutionProfileKindV1 = Literal[
    "generation",
    "patch_repair",
    "constraint_extraction",
    "review",
    "llm_triage",
    "checker",
    "simulation",
    "workload",
    "config_export",
    "task_suite_derivation",
    "environment",
    "playtest_planner",
    "validation",
    "constraint_compiler",
    "rollback",
    "schema_compatibility",
    "impact_analysis",
    "bench_evaluator",
    "artifact_migrator",
    "dr_plan",
    "restore_target",
    "dr_verifier",
]


class ResolvedExecutionProfileBindingV1(_FrozenModel):
    field_path: JsonPointer
    profile: ProfileRefV1
    expected_profile_kind: ExecutionProfileKindV1
    profile_payload_hash: Sha256Hex
    catalog_version: int = Field(ge=1)
    catalog_digest: Sha256Hex

    @field_validator("field_path")
    @classmethod
    def _valid_field_path(cls, value: str) -> str:
        if not _is_json_pointer(value):
            raise ValueError("field_path must be an RFC 6901 JSON Pointer")
        return value


class EnvironmentContractDescriptorV1(_FrozenModel):
    env_contract_version: NonEmptyStr
    reset_schema_id: NonEmptyStr
    action_schema_id: NonEmptyStr
    observation_schema_id: NonEmptyStr
    # A navigation-capable environment must freeze the largest search space one
    # atomic action may traverse.  Profile-selected adapters use this authority
    # both to reject an oversized candidate before constructing the environment
    # and to debit their exact static traversal work from the Run-wide ledger.
    max_navigation_grid_cells: int = Field(
        ge=1,
        le=MAX_ENVIRONMENT_NAVIGATION_GRID_CELLS_V1,
    )


class GenericProfileDetailsV1(_FrozenModel):
    details_kind: Literal["generic"] = "generic"


class FixedResolvedPolicyRequirementConfigV1(_FrozenModel):
    """One exact requirement declared by a versioned execution profile."""

    source: Literal["fixed"] = "fixed"
    outcome_rule_id: NonEmptyStr
    requirement_id: NonEmptyStr
    artifact_kind: ArtifactKind
    payload_schema_id: NonEmptyStr
    producer_profile_field_path: JsonPointer
    ordinal: int = Field(ge=1)


class ProfileCollectionResolvedPolicyRequirementConfigV1(_FrozenModel):
    """Derive one requirement per exact ProfileRef in a Run payload collection."""

    source: Literal["profile_collection"] = "profile_collection"
    outcome_rule_id: NonEmptyStr
    artifact_kind: ArtifactKind
    payload_schema_id: NonEmptyStr
    collection_field_path: JsonPointer


class ArtifactCollectionResolvedPolicyRequirementConfigV1(_FrozenModel):
    """Derive one requirement per exact Artifact id in a Run payload collection."""

    source: Literal["artifact_collection"] = "artifact_collection"
    outcome_rule_id: NonEmptyStr
    artifact_kind: ArtifactKind
    payload_schema_id: NonEmptyStr
    collection_field_path: JsonPointer


ResolvedPolicyRequirementConfigV1 = Annotated[
    FixedResolvedPolicyRequirementConfigV1
    | ProfileCollectionResolvedPolicyRequirementConfigV1
    | ArtifactCollectionResolvedPolicyRequirementConfigV1,
    Field(discriminator="source"),
]


class ResolvedPolicyProfileConfigV1(_FrozenModel):
    """Versioned authority for terminal resolved-policy cardinality requirements."""

    resolved_policy_id: NonEmptyStr
    requirement_sources: tuple[ResolvedPolicyRequirementConfigV1, ...] = Field(min_length=1)

    @field_validator("requirement_sources")
    @classmethod
    def _canonical_sources(
        cls, value: tuple[ResolvedPolicyRequirementConfigV1, ...]
    ) -> tuple[ResolvedPolicyRequirementConfigV1, ...]:
        def identity(item: ResolvedPolicyRequirementConfigV1) -> tuple[str, str, str]:
            if isinstance(item, FixedResolvedPolicyRequirementConfigV1):
                return (item.outcome_rule_id, item.source, item.requirement_id)
            return (item.outcome_rule_id, item.source, item.collection_field_path)

        identities = [identity(item) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("resolved-policy requirement sources must be unique")
        return tuple(sorted(value, key=identity))


class GenerationProfileConfigV1(_FrozenModel):
    config_schema_version: Literal["generation-profile-config@1"] = "generation-profile-config@1"
    resolved_policy: ResolvedPolicyProfileConfigV1
    max_prompt_message_bytes: int = Field(ge=1, le=MAX_AGENT_PROMPT_MESSAGE_BYTES_V1)
    max_checker_constraint_count: int = Field(ge=1, le=MAX_CHECKER_CONSTRAINT_COUNT_V1)
    max_checker_work_units: int = Field(ge=1, le=MAX_CHECKER_WORK_UNITS_V1)
    gate_simulation_seed: int = Field(ge=0, le=(1 << 64) - 1)
    gate_simulation_population: int = Field(ge=1, le=MAX_SIMULATION_POPULATION_V1)
    gate_simulation_horizon_steps: int = Field(ge=1, le=MAX_SIMULATION_HORIZON_STEPS_V1)
    max_simulation_work_units: int = Field(ge=1, le=MAX_SIMULATION_WORK_UNITS_V1)
    max_candidate_export_profiles: int = Field(ge=0, le=MAX_CANDIDATE_EXPORT_PROFILES_V1)
    max_total_prepared_artifact_bytes: int = Field(ge=1, le=MAX_PREPARED_OUTCOME_BYTES_V1)

    @model_validator(mode="after")
    def _simulation_defaults_within_work_envelope(self) -> "GenerationProfileConfigV1":
        if (
            self.gate_simulation_population * self.gate_simulation_horizon_steps
            > self.max_simulation_work_units
        ):
            raise ValueError("generation simulation defaults exceed the v1 work envelope")
        return self


class PatchRepairProfileConfigV1(_FrozenModel):
    config_schema_version: Literal["patch_repair-profile-config@1"] = (
        "patch_repair-profile-config@1"
    )
    resolved_policy: ResolvedPolicyProfileConfigV1
    max_prompt_message_bytes: int = Field(ge=1, le=MAX_AGENT_PROMPT_MESSAGE_BYTES_V1)
    max_search_steps: int = Field(ge=1, le=1_000)
    max_total_checker_work_units: int = Field(ge=1, le=MAX_CHECKER_WORK_UNITS_V1)
    max_total_simulation_work_units: int = Field(ge=1, le=MAX_SIMULATION_WORK_UNITS_V1)
    max_checker_profile_count: int = Field(ge=1, le=MAX_EXECUTION_PROFILE_COLLECTION_V1)
    max_simulation_profile_count: int = Field(ge=0, le=MAX_EXECUTION_PROFILE_COLLECTION_V1)
    max_regression_suite_count: int = Field(ge=0, le=MAX_REPAIR_REGRESSION_SUITES_V1)
    max_total_regression_work_units: int = Field(ge=1, le=MAX_REPAIR_REGRESSION_WORK_UNITS_V1)
    max_regression_suite_bytes: int = Field(ge=1, le=MAX_REPAIR_REGRESSION_SUITE_BYTES_V1)
    max_total_regression_suite_bytes: int = Field(
        ge=1, le=MAX_REPAIR_TOTAL_REGRESSION_SUITE_BYTES_V1
    )
    max_candidate_export_profiles: int = Field(ge=0, le=MAX_CANDIDATE_EXPORT_PROFILES_V1)
    max_total_prepared_artifact_bytes: int = Field(ge=1, le=MAX_PREPARED_OUTCOME_BYTES_V1)

    @model_validator(mode="after")
    def _regression_suite_envelope(self) -> "PatchRepairProfileConfigV1":
        if self.max_regression_suite_bytes > self.max_total_regression_suite_bytes:
            raise ValueError("repair per-suite bytes exceed total suite bytes")
        return self


class ReviewProfileConfigV1(_FrozenModel):
    """Run-wide profile-count and deterministic-work authority for review."""

    config_schema_version: Literal["review-profile-config@1"] = "review-profile-config@1"
    max_prompt_message_bytes: int = Field(ge=1, le=MAX_AGENT_PROMPT_MESSAGE_BYTES_V1)
    max_checker_profile_count: int = Field(ge=0, le=MAX_EXECUTION_PROFILE_COLLECTION_V1)
    max_simulation_profile_count: int = Field(ge=0, le=MAX_EXECUTION_PROFILE_COLLECTION_V1)
    max_total_checker_work_units: int = Field(ge=1, le=MAX_CHECKER_WORK_UNITS_V1)
    max_total_simulation_work_units: int = Field(ge=1, le=MAX_SIMULATION_WORK_UNITS_V1)
    max_total_prepared_artifact_bytes: int = Field(ge=1, le=MAX_PREPARED_OUTCOME_BYTES_V1)


class ConstraintExtractionProfileConfigV1(_FrozenModel):
    """Bounded authenticated inputs and deterministic draft output."""

    config_schema_version: Literal["constraint_extraction-profile-config@1"] = (
        "constraint_extraction-profile-config@1"
    )
    max_prompt_message_bytes: int = Field(ge=1, le=MAX_AGENT_PROMPT_MESSAGE_BYTES_V1)
    max_source_artifact_count: int = Field(ge=1, le=MAX_EXTRACTION_SOURCE_ARTIFACTS_V1)
    max_source_artifact_bytes: int = Field(ge=1, le=MAX_EXTRACTION_SOURCE_BYTES_V1)
    max_total_input_bytes: int = Field(ge=1, le=MAX_EXTRACTION_TOTAL_INPUT_BYTES_V1)
    max_proposal_count: int = Field(ge=1, le=MAX_EXTRACTION_PROPOSALS_V1)
    max_output_bytes: int = Field(ge=1, le=MAX_EXTRACTION_OUTPUT_BYTES_V1)

    @model_validator(mode="after")
    def _source_envelope(self) -> "ConstraintExtractionProfileConfigV1":
        if self.max_source_artifact_bytes > self.max_total_input_bytes:
            raise ValueError("extraction per-source bytes exceed total input bytes")
        return self


class CheckerProfileConfigV1(_FrozenModel):
    """Frozen checker taxonomy and executable work envelope."""

    config_schema_version: Literal["checker-profile-config@1"] = "checker-profile-config@1"
    allowed_checker_ids: tuple[NonEmptyStr, ...] = Field(
        min_length=1, max_length=MAX_PROFILE_ALLOWLIST_ITEMS_V1
    )
    allowed_defect_classes: tuple[NonEmptyStr, ...] = Field(
        min_length=1, max_length=MAX_PROFILE_ALLOWLIST_ITEMS_V1
    )
    max_direct_checker_count: int = Field(ge=1, le=MAX_CHECKER_DIRECT_COUNT_V1)
    max_constraint_count: int = Field(ge=1, le=MAX_CHECKER_CONSTRAINT_COUNT_V1)
    max_work_units: int = Field(ge=1, le=MAX_CHECKER_WORK_UNITS_V1)

    @field_validator("allowed_checker_ids", "allowed_defect_classes")
    @classmethod
    def _stable_allowlists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_strings(value, allow_empty=False)


class SimulationProfileConfigV1(_FrozenModel):
    """Frozen horizon/output bounds for one simulator implementation."""

    config_schema_version: Literal["simulation-profile-config@1"] = "simulation-profile-config@1"
    default_population: int = Field(ge=1, le=MAX_SIMULATION_POPULATION_V1)
    default_horizon_steps: int = Field(ge=1, le=MAX_SIMULATION_HORIZON_STEPS_V1)
    max_horizon_steps: int = Field(ge=1, le=MAX_SIMULATION_HORIZON_STEPS_V1)
    max_output_ticks: int = Field(ge=1, le=MAX_SIMULATION_OUTPUT_TICKS_V1)
    max_work_units: int = Field(ge=1, le=MAX_SIMULATION_WORK_UNITS_V1)

    @model_validator(mode="after")
    def _defaults_within_bounds(self) -> "SimulationProfileConfigV1":
        if (
            self.default_horizon_steps > self.max_horizon_steps
            or self.default_horizon_steps > self.max_output_ticks
        ):
            raise ValueError("simulation profile defaults exceed its execution bounds")
        if self.default_population * self.default_horizon_steps > self.max_work_units:
            raise ValueError("simulation profile defaults exceed its work envelope")
        return self


class WorkloadProfileConfigV1(_FrozenModel):
    """Frozen replication and aggregate-work bounds for simulation workloads."""

    config_schema_version: Literal["workload-profile-config@1"] = "workload-profile-config@1"
    max_replication_count: int = Field(ge=1, le=MAX_WORKLOAD_REPLICATION_COUNT_V1)
    max_total_replication_ticks: int = Field(ge=1, le=MAX_WORKLOAD_REPLICATION_TICKS_V1)
    max_total_work_units: int = Field(ge=1, le=MAX_WORKLOAD_WORK_UNITS_V1)


class PlaytestPlannerProfileConfigV1(_FrozenModel):
    """Versioned planner behavior that determines the executable Agent graph.

    ``memory_mode`` is deliberately explicit: admission and the worker must never
    infer whether the optional memory-compaction LLM node exists from an injected
    process default.
    """

    config_schema_version: Literal["playtest_planner-profile-config@1"] = (
        "playtest_planner-profile-config@1"
    )
    memory_mode: Literal["off", "llm_compaction"]


class PlaytestPlannerProfileConfigV2(_FrozenModel):
    """Planner behavior plus the complete aggregate resource authority.

    Version 1 shipped before Task 12 and contains only ``memory_mode``.  Its
    immutable shape remains readable for historical catalogs; resource-bounded
    Task 12 Runs must bind this version-2 config instead of manufacturing limits
    for an old ProfileRef.
    """

    config_schema_version: Literal["playtest_planner-profile-config@2"] = (
        "playtest_planner-profile-config@2"
    )
    memory_mode: Literal["off", "llm_compaction"]
    max_episode_count: int = Field(ge=1, le=MAX_PLAYTEST_EPISODES_V1)
    max_steps_per_episode: int = Field(
        ge=1,
        le=MAX_PLAYTEST_STEPS_PER_EPISODE_V1,
    )
    max_total_steps: int = Field(ge=1, le=MAX_PLAYTEST_TOTAL_STEPS_V1)
    max_total_model_calls: int = Field(
        ge=1,
        le=MAX_PLAYTEST_TOTAL_MODEL_CALLS_V2,
    )
    max_total_trace_bytes: int = Field(
        ge=1,
        le=MAX_PLAYTEST_TOTAL_TRACE_BYTES_V1,
    )

    @model_validator(mode="after")
    def _resource_envelope(self) -> "PlaytestPlannerProfileConfigV2":
        if self.max_steps_per_episode > self.max_total_steps:
            raise ValueError("playtest per-episode steps exceed total-step authority")
        if self.max_episode_count > self.max_total_steps:
            raise ValueError("playtest episodes exceed the minimum total-step authority")
        calls_per_step = 6 if self.memory_mode == "llm_compaction" else 3
        if self.max_total_steps * calls_per_step > self.max_total_model_calls:
            raise ValueError("playtest profile cannot close its model-call authority")
        return self


class TaskSuiteDerivationProfileConfigV1(_FrozenModel):
    """Historical Task 11 config shape (the exact empty object)."""


class TaskSuiteDerivationProfileConfigV2(_FrozenModel):
    """Exact deterministic authority for one task-suite derivation profile.

    The profile closes the environment and completion-oracle registry selected by
    a derive Run and bounds the complete ``scenario_spec* + task_suite`` prepared
    batch.  A process-local shaper cannot silently widen any of these authorities.
    """

    config_schema_version: Literal["task_suite_derivation-profile-config@2"] = (
        "task_suite_derivation-profile-config@2"
    )
    target_environment_profile: ProfileRefV1
    completion_oracle_registry_version: int = Field(ge=1)
    completion_oracle_registry_digest: Sha256Hex
    max_scenarios: int = Field(ge=1, le=MAX_TASK_SUITE_SCENARIOS_V1)
    max_total_prepared_artifact_bytes: int = Field(ge=1, le=MAX_PREPARED_OUTCOME_BYTES_V1)


class EnvironmentProfileDetailsV1(_FrozenModel):
    details_kind: Literal["environment"] = "environment"
    contract: EnvironmentContractDescriptorV1


class ConfigExportProfileDetailsV1(_FrozenModel):
    details_kind: Literal["config_export"] = "config_export"
    target_environment_profile: ProfileRefV1
    env_contract_version: NonEmptyStr
    format_schema_id: NonEmptyStr
    package_schema_version: Literal["config-export-package@1"] = "config-export-package@1"


class ValidationProfileDetailsV1(_FrozenModel):
    details_kind: Literal["validation"] = "validation"
    subject_kinds: tuple[SubjectKind, ...] = Field(min_length=1)
    auto_apply_policy: AutoApplyPolicyRefV1 | None = None

    @field_validator("subject_kinds")
    @classmethod
    def _stable_subject_kinds(cls, value: tuple[SubjectKind, ...]) -> tuple[SubjectKind, ...]:
        order = {"patch": 0, "constraint_proposal": 1, "rollback_request": 2}
        return tuple(sorted(set(value), key=order.__getitem__))

    @model_validator(mode="after")
    def _auto_apply_is_patch_only(self) -> "ValidationProfileDetailsV1":
        if self.auto_apply_policy is not None and "patch" not in self.subject_kinds:
            raise ValueError("auto-apply policy is only valid for patch validation")
        return self


class MigrationEdgeV1(_FrozenModel):
    edge_id: NonEmptyStr
    source_kind: ArtifactKind
    source_payload_schema_id: NonEmptyStr
    target_payload_schema_id: NonEmptyStr
    target_meta_schema_version: NonEmptyStr
    target_dsl_grammar_version: NonEmptyStr | None = None
    golden_replay_policy: Literal["required", "not_applicable"]
    golden_fixture_set_digest: Sha256Hex | None = None
    not_applicable_reason_code: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _golden_evidence(self) -> "MigrationEdgeV1":
        if self.golden_replay_policy == "required":
            if (
                self.golden_fixture_set_digest is None
                or self.not_applicable_reason_code is not None
            ):
                raise ValueError("required golden replay needs only a fixture digest")
        elif self.golden_fixture_set_digest is not None or self.not_applicable_reason_code is None:
            raise ValueError("not-applicable golden replay needs only a versioned reason code")
        return self


class MigrationProfileDetailsV1(_FrozenModel):
    details_kind: Literal["artifact_migrator"] = "artifact_migrator"
    edges: tuple[MigrationEdgeV1, ...]

    @field_validator("edges")
    @classmethod
    def _canonical_edges(cls, value: tuple[MigrationEdgeV1, ...]) -> tuple[MigrationEdgeV1, ...]:
        ids = [edge.edge_id for edge in value]
        semantic = [
            (
                edge.source_kind,
                edge.source_payload_schema_id,
                edge.target_payload_schema_id,
                edge.target_meta_schema_version,
                edge.target_dsl_grammar_version,
            )
            for edge in value
        ]
        if len(ids) != len(set(ids)) or len(semantic) != len(set(semantic)):
            raise ValueError("migration edges must have unique ids and source/target tuples")
        return tuple(sorted(value, key=lambda edge: edge.edge_id))


ExecutionProfileDetailsV1 = Annotated[
    GenericProfileDetailsV1
    | EnvironmentProfileDetailsV1
    | ConfigExportProfileDetailsV1
    | ValidationProfileDetailsV1
    | MigrationProfileDetailsV1,
    Field(discriminator="details_kind"),
]


def canonical_config_hash(config: Mapping[str, JsonValue]) -> str:
    return canonical_sha256(_json_data(config))


class ExecutionProfileDefinitionV1(_FrozenModel):
    definition_schema_version: Literal["execution-profile@1"] = "execution-profile@1"
    profile: ProfileRefV1
    profile_kind: ExecutionProfileKindV1
    compatible_run_kinds: tuple[RunKindRef, ...] = Field(min_length=1)
    domain_scope: DomainScope
    stochastic: bool
    input_schema_ids: tuple[NonEmptyStr, ...]
    output_schema_ids: tuple[NonEmptyStr, ...]
    required_capabilities: tuple[NonEmptyStr, ...]
    display_name: NonEmptyStr
    handler_key: NonEmptyStr
    config_schema_id: NonEmptyStr
    config: dict[str, JsonValue]
    config_hash: Sha256Hex
    details: ExecutionProfileDetailsV1

    @field_validator("compatible_run_kinds")
    @classmethod
    def _stable_run_kinds(cls, value: tuple[RunKindRef, ...]) -> tuple[RunKindRef, ...]:
        keys = [(item.kind, item.version) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("compatible run kinds must be unique")
        return tuple(sorted(value, key=lambda item: (item.kind, item.version)))

    @field_validator("input_schema_ids", "output_schema_ids", "required_capabilities")
    @classmethod
    def _stable_string_sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_strings(value)

    @model_validator(mode="after")
    def _definition_closure(self) -> "ExecutionProfileDefinitionV1":
        if self.config_hash != canonical_config_hash(self.config):
            raise ValueError("config_hash does not match canonical config")
        expected_details = {
            "environment": "environment",
            "config_export": "config_export",
            "validation": "validation",
            "artifact_migrator": "artifact_migrator",
        }.get(self.profile_kind, "generic")
        if self.details.details_kind != expected_details:
            raise ValueError("details variant does not match profile_kind")
        config_models = {
            "generation": ("generation-profile-config@1", GenerationProfileConfigV1),
            "patch_repair": ("patch_repair-profile-config@1", PatchRepairProfileConfigV1),
            "review": ("review-profile-config@1", ReviewProfileConfigV1),
            "constraint_extraction": (
                "constraint_extraction-profile-config@1",
                ConstraintExtractionProfileConfigV1,
            ),
            "checker": ("checker-profile-config@1", CheckerProfileConfigV1),
            "simulation": ("simulation-profile-config@1", SimulationProfileConfigV1),
            "workload": ("workload-profile-config@1", WorkloadProfileConfigV1),
        }
        if self.profile_kind == "bench_evaluator":
            # Local import avoids the contract-level ProfileRef/benchmark cycle while
            # still making the retained evaluator profile config fully typed.
            from gameforge.contracts.benchmark import BenchmarkEvaluatorProfileConfigV1

            config_models["bench_evaluator"] = (
                "bench_evaluator-profile-config@1",
                BenchmarkEvaluatorProfileConfigV1,
            )
        versioned_config_models = {
            "task_suite_derivation": {
                "task_suite_derivation-profile-config@1": TaskSuiteDerivationProfileConfigV1,
                "task_suite_derivation-profile-config@2": TaskSuiteDerivationProfileConfigV2,
            },
            "playtest_planner": {
                "playtest_planner-profile-config@1": PlaytestPlannerProfileConfigV1,
                "playtest_planner-profile-config@2": PlaytestPlannerProfileConfigV2,
            },
        }
        versioned_contracts = versioned_config_models.get(self.profile_kind)
        if versioned_contracts is not None:
            model = versioned_contracts.get(self.config_schema_id)
            if model is None:
                raise ValueError("profile config schema does not match profile kind")
            model.model_validate(self.config)
            return self

        config_contract = config_models.get(self.profile_kind)
        if config_contract is not None:
            expected_schema_id, model = config_contract
            if self.config_schema_id != expected_schema_id:
                raise ValueError("profile config schema does not match profile kind")
            model.model_validate(self.config)
        return self


def execution_profile_payload_hash(definition: ExecutionProfileDefinitionV1) -> str:
    return canonical_sha256(definition.model_dump(mode="json"))


class ExecutionProfileLifecycleV1(_FrozenModel):
    profile: ProfileRefV1
    state: Literal["active", "replay_only", "disabled"]
    revision: int = Field(ge=1)
    reason_code: NonEmptyStr | None = None
    changed_at: NonEmptyStr


def execution_profile_catalog_digest(payload: Mapping[str, Any]) -> str:
    raw = _json_data(payload)
    definitions = sorted(
        raw.get("definitions", []),
        key=lambda item: (item["profile"]["profile_id"], item["profile"]["version"]),
    )
    lifecycle = sorted(
        raw.get("lifecycle", []),
        key=lambda item: (item["profile"]["profile_id"], item["profile"]["version"]),
    )
    return canonical_sha256(
        {
            "catalog_schema_version": raw.get(
                "catalog_schema_version", "execution-profile-catalog@1"
            ),
            "catalog_version": raw["catalog_version"],
            "definitions": definitions,
            "lifecycle": lifecycle,
        }
    )


class ExecutionProfileCatalogSnapshotV1(_FrozenModel):
    catalog_schema_version: Literal["execution-profile-catalog@1"] = "execution-profile-catalog@1"
    catalog_version: int = Field(ge=1)
    definitions: tuple[ExecutionProfileDefinitionV1, ...]
    lifecycle: tuple[ExecutionProfileLifecycleV1, ...]
    catalog_digest: Sha256Hex

    @field_validator("definitions")
    @classmethod
    def _canonical_definitions(
        cls, value: tuple[ExecutionProfileDefinitionV1, ...]
    ) -> tuple[ExecutionProfileDefinitionV1, ...]:
        refs = [(item.profile.profile_id, item.profile.version) for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("catalog definitions must have unique ProfileRefs")
        return tuple(
            sorted(value, key=lambda item: (item.profile.profile_id, item.profile.version))
        )

    @field_validator("lifecycle")
    @classmethod
    def _canonical_lifecycle(
        cls, value: tuple[ExecutionProfileLifecycleV1, ...]
    ) -> tuple[ExecutionProfileLifecycleV1, ...]:
        refs = [(item.profile.profile_id, item.profile.version) for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("catalog lifecycle rows must have unique ProfileRefs")
        return tuple(
            sorted(value, key=lambda item: (item.profile.profile_id, item.profile.version))
        )

    @model_validator(mode="after")
    def _catalog_closure(self) -> "ExecutionProfileCatalogSnapshotV1":
        definitions = {item.profile for item in self.definitions}
        lifecycle = {item.profile for item in self.lifecycle}
        if definitions != lifecycle:
            raise ValueError("definitions and lifecycle must contain the exact same refs")
        if self.catalog_digest != execution_profile_catalog_digest(
            self.model_dump(mode="json", exclude={"catalog_digest"})
        ):
            raise ValueError("catalog_digest does not match canonical catalog payload")
        return self


class ExecutionProfileViewV1(_FrozenModel):
    profile: ProfileRefV1
    profile_payload_hash: Sha256Hex
    profile_kind: ExecutionProfileKindV1
    status: Literal["active", "replay_only", "disabled"]
    compatible_run_kinds: tuple[RunKindRef, ...]
    domain_scope: DomainScope
    stochastic: bool
    input_schema_ids: tuple[NonEmptyStr, ...]
    output_schema_ids: tuple[NonEmptyStr, ...]
    required_capabilities: tuple[NonEmptyStr, ...]
    display_name: NonEmptyStr
    env_contract_version: NonEmptyStr | None = None
    target_environment_profile: ProfileRefV1 | None = None


class ArtifactLineagePolicyRefV1(_FrozenModel):
    policy_id: NonEmptyStr
    policy_version: int = Field(ge=1)
    digest: Sha256Hex


class VersionTransitionPolicyRefV1(_FrozenModel):
    policy_id: NonEmptyStr
    policy_version: int = Field(ge=1)
    digest: Sha256Hex


class MigrationCapabilityMatrixRefV1(_FrozenModel):
    matrix_version: int = Field(ge=1)
    matrix_digest: Sha256Hex


MigrationCapability = Literal[
    "publish_same_kind", "report_only", "needs_re_extract", "needs_re_compile"
]


class MigrationEdgeCapabilityV1(_FrozenModel):
    source_kind: ArtifactKind
    source_payload_schema_id: NonEmptyStr
    target_payload_schema_id: NonEmptyStr
    target_meta_schema_version: NonEmptyStr
    target_dsl_grammar_version: NonEmptyStr | None = None
    capability: MigrationCapability
    publication_lineage_policy_ref: ArtifactLineagePolicyRefV1 | None = None

    @model_validator(mode="after")
    def _publication_policy(self) -> "MigrationEdgeCapabilityV1":
        required = self.capability == "publish_same_kind"
        if required != (self.publication_lineage_policy_ref is not None):
            raise ValueError("only publish_same_kind capabilities require a lineage policy")
        return self


class MigrationKindDefaultV1(_FrozenModel):
    source_kind: ArtifactKind
    unsupported_edge_action: Literal[
        "reject_409", "report_only", "needs_re_extract", "needs_re_compile"
    ]


def migration_capability_matrix_digest(payload: Mapping[str, Any]) -> str:
    raw = _json_data(payload)
    kind_rank = {kind: index for index, kind in enumerate(get_args(ArtifactKind))}
    kind_defaults = sorted(
        raw.get("kind_defaults", []),
        key=lambda item: kind_rank[item["source_kind"]],
    )
    edges = sorted(
        raw.get("edges", []),
        key=lambda item: (
            item["source_kind"],
            item["source_payload_schema_id"],
            item["target_payload_schema_id"],
            item["target_meta_schema_version"],
            item.get("target_dsl_grammar_version") or "",
        ),
    )
    return canonical_sha256(
        {
            "matrix_schema_version": raw.get(
                "matrix_schema_version", "migration-capability-matrix@1"
            ),
            "matrix_version": raw["matrix_version"],
            "kind_defaults": kind_defaults,
            "edges": edges,
        }
    )


class MigrationCapabilityMatrixV1(_FrozenModel):
    matrix_schema_version: Literal["migration-capability-matrix@1"] = (
        "migration-capability-matrix@1"
    )
    matrix_version: int = Field(ge=1)
    kind_defaults: tuple[MigrationKindDefaultV1, ...]
    edges: tuple[MigrationEdgeCapabilityV1, ...]
    matrix_digest: Sha256Hex

    @field_validator("kind_defaults")
    @classmethod
    def _complete_defaults(
        cls, value: tuple[MigrationKindDefaultV1, ...]
    ) -> tuple[MigrationKindDefaultV1, ...]:
        artifact_kinds = tuple(get_args(ArtifactKind))
        by_kind = {item.source_kind: item for item in value}
        if len(by_kind) != len(value) or set(by_kind) != set(artifact_kinds):
            raise ValueError("kind defaults must cover every ArtifactKind exactly once")
        return tuple(by_kind[kind] for kind in artifact_kinds)

    @field_validator("edges")
    @classmethod
    def _canonical_capabilities(
        cls, value: tuple[MigrationEdgeCapabilityV1, ...]
    ) -> tuple[MigrationEdgeCapabilityV1, ...]:
        def key(item: MigrationEdgeCapabilityV1) -> tuple[str, str, str, str, str]:
            return (
                item.source_kind,
                item.source_payload_schema_id,
                item.target_payload_schema_id,
                item.target_meta_schema_version,
                item.target_dsl_grammar_version or "",
            )

        keys = [key(item) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("migration capability edge tuples must be unique")
        return tuple(sorted(value, key=key))

    @model_validator(mode="after")
    def _digest(self) -> "MigrationCapabilityMatrixV1":
        if self.matrix_digest != migration_capability_matrix_digest(
            self.model_dump(mode="json", exclude={"matrix_digest"})
        ):
            raise ValueError("matrix_digest does not match canonical matrix payload")
        return self


def migration_capability_registry_digest(payload: Mapping[str, Any]) -> str:
    raw = _json_data(payload)
    matrices = sorted(raw.get("matrices", []), key=lambda item: item["matrix_version"])
    return canonical_sha256(
        {
            "registry_schema_version": raw.get(
                "registry_schema_version", "migration-capability-matrix-registry@1"
            ),
            "matrices": matrices,
        }
    )


class MigrationCapabilityMatrixRegistryV1(_FrozenModel):
    registry_schema_version: Literal["migration-capability-matrix-registry@1"] = (
        "migration-capability-matrix-registry@1"
    )
    matrices: tuple[MigrationCapabilityMatrixV1, ...]
    registry_digest: Sha256Hex

    @field_validator("matrices")
    @classmethod
    def _canonical_matrices(
        cls, value: tuple[MigrationCapabilityMatrixV1, ...]
    ) -> tuple[MigrationCapabilityMatrixV1, ...]:
        versions = [item.matrix_version for item in value]
        if len(versions) != len(set(versions)):
            raise ValueError("matrix registry versions must be unique")
        return tuple(sorted(value, key=lambda item: item.matrix_version))

    @model_validator(mode="after")
    def _digest(self) -> "MigrationCapabilityMatrixRegistryV1":
        if self.registry_digest != migration_capability_registry_digest(
            self.model_dump(mode="json", exclude={"registry_digest"})
        ):
            raise ValueError("registry_digest does not match canonical registry payload")
        return self
