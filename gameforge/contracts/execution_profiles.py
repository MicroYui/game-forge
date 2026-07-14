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


class GenericProfileDetailsV1(_FrozenModel):
    details_kind: Literal["generic"] = "generic"


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
