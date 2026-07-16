"""Immutable metadata used by the platform registry composition root."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping

from gameforge.contracts.execution_profiles import ExecutionProfileKindV1, RunKindRef


RunKindIdentity = tuple[str, int]


def run_kind_identity(value: RunKindRef | RunKindIdentity) -> RunKindIdentity:
    """Return the exact, versioned identity used by every registry index."""

    if isinstance(value, RunKindRef):
        return (value.kind, value.version)
    kind, version = value
    return (kind, version)


@dataclass(frozen=True, slots=True)
class ProfileRequirement:
    """One exact execution-profile binding required by a Run payload."""

    field_path: str
    expected_profile_kind: ExecutionProfileKindV1
    required: bool | None = None
    cardinality: Literal["one", "optional", "many"] | None = None

    def __post_init__(self) -> None:
        if not self.field_path.startswith("/"):
            raise ValueError("profile requirement field_path must be an RFC 6901 pointer")
        if "*" in self.field_path:
            raise ValueError("profile requirement field_path cannot contain a wildcard")
        if self.cardinality is None:
            object.__setattr__(
                self,
                "cardinality",
                "one" if self.required is not False else "optional",
            )
        elif self.required is not None:
            compatible = self.cardinality == ("one" if self.required else "optional")
            if not compatible:
                raise ValueError("profile requirement required/cardinality values conflict")


@dataclass(frozen=True, slots=True)
class FrozenRunKindShape:
    """The non-policy projection frozen by the M4c RunKind tables."""

    payload_schema_id: str
    creation_mode: str
    command_schema_ids: tuple[str, ...]
    permission_action: str
    permission_resource_kind: str
    dynamic_domain_permission: bool
    executor_key: str
    success_hook: str
    retry_policy_id: str
    llm_modes: tuple[str, ...]
    seed_policy: str
    seed_derivation_version: str | None
    finding_policy_id: str | None
    migration_matrix_required: bool = False


@dataclass(frozen=True, slots=True)
class TrustedComponentMaps:
    """Explicit in-process component allowlists supplied by the composition root."""

    executors: Mapping[str, object] = field(default_factory=dict)
    terminal_hooks: Mapping[str, object] = field(default_factory=dict)
    workflow_effects: Mapping[str, object] = field(default_factory=dict)
    completion_oracles: Mapping[str, object] = field(default_factory=dict)
    profile_handlers: Mapping[str, object] = field(default_factory=dict)
    permission_domain_resolvers: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "executors",
            "terminal_hooks",
            "workflow_effects",
            "completion_oracles",
            "profile_handlers",
            "permission_domain_resolvers",
        ):
            value = getattr(self, field_name)
            frozen = MappingProxyType(dict(value))
            if any(not key for key in frozen):
                raise ValueError(f"{field_name} cannot contain an empty component key")
            object.__setattr__(self, field_name, frozen)


@dataclass(frozen=True, slots=True)
class PlatformReadinessReport:
    """Read-only evidence that the complete platform registry closed successfully."""

    ready: bool
    active_run_kinds: tuple[RunKindRef, ...]
    checked_run_kind_count: int
    deferred_executor_keys: tuple[str, ...]
    reference_checks: int
    component_key_counts: tuple[tuple[str, int], ...]


_CANCEL_ONLY = ("run-cancel@1",)
_LLM = ("live", "record", "replay")
_ALL_MODES = ("not_applicable", "live", "record", "replay")
_NA = ("not_applicable",)


FROZEN_RUN_KIND_SHAPES: Mapping[RunKindIdentity, FrozenRunKindShape] = MappingProxyType(
    {
        ("generation.propose", 1): FrozenRunKindShape(
            "generation-propose@1",
            "resource_endpoint_only",
            _CANCEL_ONLY,
            "propose",
            "patch",
            True,
            "generation_proposer@1",
            "publish_gated_patch_preview@1",
            "llm_transient",
            _LLM,
            "forbidden",
            None,
            None,
        ),
        ("patch.repair", 1): FrozenRunKindShape(
            "patch-repair@1",
            "resource_endpoint_only",
            _CANCEL_ONLY,
            "propose",
            "patch",
            True,
            "repair_search@1",
            "publish_patch_revision_preview@1",
            "llm_transient",
            _LLM,
            "profile_dependent",
            "subseed@1",
            None,
        ),
        ("constraint_proposal.propose", 1): FrozenRunKindShape(
            "constraint-proposal-propose@1",
            "resource_endpoint_only",
            _CANCEL_ONLY,
            "propose",
            "constraint_proposal",
            True,
            "constraint_proposer@1",
            "publish_constraint_proposal_draft@1",
            "llm_transient",
            _LLM,
            "forbidden",
            None,
            None,
        ),
        ("review.run", 1): FrozenRunKindShape(
            "review-run@1",
            "generic_runs_endpoint",
            _CANCEL_ONLY,
            "run",
            "review",
            True,
            "review_runner@1",
            "publish_review@1",
            "composite_transient",
            _ALL_MODES,
            "profile_dependent",
            "subseed@1",
            "review-findings",
        ),
        ("checker.run", 1): FrozenRunKindShape(
            "checker-run@1",
            "generic_runs_endpoint",
            _CANCEL_ONLY,
            "run",
            "checker",
            True,
            "checker_runner@1",
            "publish_checker@1",
            "deterministic_job",
            _NA,
            "forbidden",
            None,
            "checker-findings",
        ),
        ("simulation.run", 1): FrozenRunKindShape(
            "simulation-run@1",
            "generic_runs_endpoint",
            _CANCEL_ONLY,
            "run",
            "simulation",
            True,
            "simulation_runner@1",
            "publish_simulation@1",
            "deterministic_job",
            _NA,
            "required",
            "subseed@1",
            "simulation-findings",
        ),
        ("task_suite.derive", 1): FrozenRunKindShape(
            "task-suite-derive@1",
            "resource_endpoint_only",
            _CANCEL_ONLY,
            "derive",
            "task_suite",
            True,
            "task_suite_deriver@1",
            "publish_task_suite@1",
            "deterministic_job",
            _NA,
            "forbidden",
            None,
            None,
        ),
        ("playtest.run", 1): FrozenRunKindShape(
            "playtest-run@1",
            "resource_endpoint_only",
            ("playtest-provide-input@1", "run-cancel@1"),
            "run",
            "playtest",
            True,
            "playtest_runner@1",
            "publish_playtest@1",
            "agent_environment",
            _LLM,
            "required",
            "subseed@1",
            "playtest-findings",
        ),
        ("patch.validate", 1): FrozenRunKindShape(
            "patch-validation@1",
            "resource_endpoint_only",
            _CANCEL_ONLY,
            "validate",
            "patch",
            True,
            "patch_validator@1",
            "publish_validation_completion@1",
            "validation_job",
            _NA,
            "profile_dependent",
            "subseed@1",
            "validation-findings",
        ),
        ("constraint_proposal.validate", 1): FrozenRunKindShape(
            "constraint-validation@1",
            "resource_endpoint_only",
            _CANCEL_ONLY,
            "validate",
            "constraint_proposal",
            True,
            "constraint_validator@1",
            "publish_validation_completion@1",
            "validation_job",
            _NA,
            "profile_dependent",
            "subseed@1",
            "validation-findings",
        ),
        ("rollback.validate", 1): FrozenRunKindShape(
            "rollback-validation@1",
            "resource_endpoint_only",
            _CANCEL_ONLY,
            "validate",
            "rollback_request",
            True,
            "rollback_validator@1",
            "publish_validation_success@1",
            "validation_job",
            _NA,
            "profile_dependent",
            "subseed@1",
            "validation-findings",
        ),
        ("bench.run", 1): FrozenRunKindShape(
            "bench-run@1",
            "generic_runs_endpoint",
            _CANCEL_ONLY,
            "run",
            "bench",
            True,
            "bench_runner@1",
            "publish_bench@1",
            "deterministic_job",
            _ALL_MODES,
            "profile_dependent",
            "subseed@1",
            None,
        ),
        ("artifact.migrate", 1): FrozenRunKindShape(
            "artifact-migration@1",
            "internal_only",
            _CANCEL_ONLY,
            "migrate",
            "artifact",
            True,
            "artifact_migrator@1",
            "publish_migration@1",
            "migration_job",
            _NA,
            "forbidden",
            None,
            None,
            True,
        ),
        ("dr.drill", 1): FrozenRunKindShape(
            "dr-drill@1",
            "internal_only",
            _CANCEL_ONLY,
            "drill",
            "operations",
            False,
            "dr_drill_runner@1",
            "publish_operational_evidence@1",
            "operational_job",
            _NA,
            "forbidden",
            None,
            None,
        ),
    }
)


FROZEN_PROFILE_REQUIREMENT_SHAPES: Mapping[RunKindIdentity, tuple[tuple[str, str, str], ...]] = (
    MappingProxyType(
        {
            ("generation.propose", 1): (
                ("/params/generation_policy", "generation", "one"),
                ("/params/candidate_export_profiles", "config_export", "many"),
            ),
            ("patch.repair", 1): (
                ("/params/repair_policy", "patch_repair", "one"),
                ("/params/checker_profiles", "checker", "many"),
                ("/params/simulation_profiles", "simulation", "many"),
                ("/params/candidate_export_profiles", "config_export", "many"),
            ),
            ("constraint_proposal.propose", 1): (
                ("/params/extraction_policy", "constraint_extraction", "one"),
            ),
            ("review.run", 1): (
                ("/params/review_profile", "review", "one"),
                ("/params/checker_profiles", "checker", "many"),
                ("/params/simulation_profiles", "simulation", "many"),
                ("/params/llm_triage_policy", "llm_triage", "optional"),
            ),
            ("checker.run", 1): (("/params/checker_profile", "checker", "one"),),
            ("simulation.run", 1): (
                ("/params/simulation_profile", "simulation", "one"),
                ("/params/workload_profile", "workload", "one"),
            ),
            ("task_suite.derive", 1): (
                ("/params/derivation_profile", "task_suite_derivation", "one"),
                ("/params/environment_profile", "environment", "one"),
            ),
            ("playtest.run", 1): (
                ("/params/environment_profile", "environment", "one"),
                ("/params/planner_policy", "playtest_planner", "one"),
            ),
            ("patch.validate", 1): (
                ("/params/validation_policy", "validation", "one"),
                ("/params/checker_profiles", "checker", "many"),
                ("/params/simulation_profiles", "simulation", "many"),
            ),
            ("constraint_proposal.validate", 1): (
                ("/params/compiler_profile", "constraint_compiler", "one"),
                ("/params/validation_policy", "validation", "one"),
            ),
            ("rollback.validate", 1): (
                ("/params/rollback_profile", "rollback", "one"),
                ("/params/schema_compatibility_policy", "schema_compatibility", "one"),
                ("/params/impact_profiles", "impact_analysis", "many"),
            ),
            ("bench.run", 1): (("/params/evaluator_profile", "bench_evaluator", "one"),),
            ("artifact.migrate", 1): (("/params/migrator", "artifact_migrator", "one"),),
            ("dr.drill", 1): (
                ("/params/dr_plan", "dr_plan", "one"),
                ("/params/restore_target_profile", "restore_target", "one"),
                ("/params/verification_profile", "dr_verifier", "one"),
            ),
        }
    )
)

FROZEN_ACTIVE_RUN_KIND_IDENTITIES: frozenset[RunKindIdentity] = frozenset(FROZEN_RUN_KIND_SHAPES)
FROZEN_RUN_KIND_DEFINITION_DIGESTS: Mapping[RunKindIdentity, str] = MappingProxyType(
    {
        ("artifact.migrate", 1): "3334acbeabf862501c15d5d8075dc5071dbfe83277caeacd6b27b90560631a8c",
        ("bench.run", 1): "6cb5a9d4e3f6c342ab2026da5a281327bef09232a58adc0e4665138634ec0703",
        ("checker.run", 1): "8ff7af32dc28be33d749a16bfe891ed58add7dcf535caaef828c4ab4dbe35e88",
        (
            "constraint_proposal.propose",
            1,
        ): "057de1a9295a10326082afa84916bb5281ba20c150723018438221dbe2777b54",
        (
            "constraint_proposal.validate",
            1,
        ): "7f075e8af79c6f658ffdbe1dd3789ab0190e3827e57f06ba9be253a9da096ca4",
        ("dr.drill", 1): "1fb1692545a847a4e50359d52aa5e6dbf66de79a21b708ade38443fbeae409a4",
        (
            "generation.propose",
            1,
        ): "40a53d73458e62c7f34ca5b34dc5127c8d6dc0f9390ff1f18705f56aa5faff06",
        ("patch.repair", 1): "1696866833d2e4391286056b46d72862abbbd553165ee94004c18f2cc5013b23",
        ("patch.validate", 1): "1d1d8e9b74fbda8cec13c8f04985d3cded6347b3b28e5d4b23d81e77d17fc130",
        ("playtest.run", 1): "38db6dee893a82b4e0bab0e115b2889e156a949660302247741ef9a64da91428",
        ("review.run", 1): "b073921ca5175d83f7975ce97fd1c7d2aaa68c5e26ff833b8bc6ee46709ca77a",
        (
            "rollback.validate",
            1,
        ): "89975a23bc81ed236b3c48d24b9c5f9c6c6bcb8b31dd6457d6338beb1b46d7cd",
        ("simulation.run", 1): "597da63b949d63e22a8d8838b836858885e7f8290072ff53e8006cf55f014374",
        (
            "task_suite.derive",
            1,
        ): "e92ab82897e282c09f1e5fbe7b9b5881dcf34fda7ab176e8ee9d9d7470bb6058",
    }
)
if set(FROZEN_PROFILE_REQUIREMENT_SHAPES) != set(FROZEN_RUN_KIND_SHAPES):
    raise RuntimeError("frozen Run kind and profile requirement tables differ")
if set(FROZEN_RUN_KIND_DEFINITION_DIGESTS) != set(FROZEN_RUN_KIND_SHAPES):
    raise RuntimeError("frozen Run kind shape and definition-digest tables differ")


__all__ = [
    "FROZEN_ACTIVE_RUN_KIND_IDENTITIES",
    "FROZEN_PROFILE_REQUIREMENT_SHAPES",
    "FROZEN_RUN_KIND_DEFINITION_DIGESTS",
    "FROZEN_RUN_KIND_SHAPES",
    "FrozenRunKindShape",
    "PlatformReadinessReport",
    "ProfileRequirement",
    "RunKindIdentity",
    "TrustedComponentMaps",
    "run_kind_identity",
]
