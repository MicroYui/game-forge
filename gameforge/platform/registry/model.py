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
        ("artifact.migrate", 1): "6a31f1cfd0d51848d9e7177de9bb4c0dc759be4db52504dfcb5cc9c568ac0378",
        ("bench.run", 1): "0bbc561039a535bc6cc2a23478b76df5c1ee5dc18ffcb09bf3d25de23ad8a762",
        ("checker.run", 1): "b5e73e19e62c3c9872642c9c9028f18d84915c42404d29ea16f3e33a59e24c97",
        (
            "constraint_proposal.propose",
            1,
        ): "bbb57637366e6d07c690c4dcd1214128cc4aeb2329b7f37683f98fd6c69c95f8",
        (
            "constraint_proposal.validate",
            1,
        ): "b501011350f43c4eb5a976b8163b9f819aeccf91d7dce3d9a6b4c02a27aa8078",
        ("dr.drill", 1): "5df0f659ff48ef5c065df4510c43c4054774ff3bc2b0af3dd8ca9f82481e46b6",
        (
            "generation.propose",
            1,
        ): "82a4eddf14e44185c11aab82c5a0bb8bf4c88e020a7c5edc030fd0f37b6d2d2c",
        ("patch.repair", 1): "9385f0d6c61deaf73f68b71571827c17e3c6a72ac40bd8eb04ea94ef1bd1966b",
        ("patch.validate", 1): "b1ce2b894a812bd83fa6b8c2e63527cbb8cd9f468c5c9ba33dba1f0fd9351e90",
        ("playtest.run", 1): "cfd93a982589bab661e51c981802e1002b8912f102e449673d88fb3fe33b63f9",
        ("review.run", 1): "a16ff361c2bb0ab09befe3e9b2f7a126a1832920b1f4006e881829408871aaa6",
        (
            "rollback.validate",
            1,
        ): "ebc48068649a8f46aa19677dce559a075959dc056072503089433d7f03a84923",
        ("simulation.run", 1): "d90bde6b162c65524e3ab3d53a4c54661d83ba9258b5cabe1fc669b8a07e0a84",
        (
            "task_suite.derive",
            1,
        ): "7eebc7a7450a64fe4569d2ebbd0e639a264d735a627fd84ddbdcfbdbe2b5064c",
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
