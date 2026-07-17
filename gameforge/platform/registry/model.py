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
    playtest_payload_validators: Mapping[str, object] = field(default_factory=dict)
    profile_handlers: Mapping[str, object] = field(default_factory=dict)
    permission_domain_resolvers: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "executors",
            "terminal_hooks",
            "workflow_effects",
            "completion_oracles",
            "playtest_payload_validators",
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
        ("artifact.migrate", 1): "64b10377ee297187af7277a25bb6df49498f0bfdb557e4bb2183a02b42ffb201",
        ("bench.run", 1): "39ab96fe4f25dae12a31601e7714adf944f684ab6871b8e22f16728e7a796f3f",
        ("checker.run", 1): "f4d08fbbe49b2986ee98de23e72ccf7336b3370934df037ebc2b9a0151ca4c80",
        (
            "constraint_proposal.propose",
            1,
        ): "ee9e63341b7869fedb02b360eba30b2f72638c4fe474660309d56af85dcf46e6",
        (
            "constraint_proposal.validate",
            1,
        ): "77970ec169483c790d3f52b77a64435b5921071525223ef793227a201dba82a6",
        ("dr.drill", 1): "f3bbbccc696d329dd0ac62f0819c50c9829429cc7402e443fbcb963c0fe6b16d",
        (
            "generation.propose",
            1,
        ): "9253e59f78e693f84ca3817c87bc5d9e78dd57377260d50c77fa1c4784b19878",
        ("patch.repair", 1): "7551bbf349d3e935c07e5cb51b50f8d3cd50aa60dc4394244bdd8aa2e4b35665",
        ("patch.validate", 1): "dea532d0bdbd3d9b2a1a71aa8f1181736040a5e528a70c61bc4231d4a38e930d",
        ("playtest.run", 1): "099288bd5fea318b35562251d647a9c9b8c9f8b9f6a10e72e8cf910b9e1e00d8",
        ("review.run", 1): "61cfe9782fe31ac183609808c817432e836ffb27e032340ede448c5030a2249b",
        (
            "rollback.validate",
            1,
        ): "a288a1e965cbfc23864a8f201e040293f6a0c33418478bcc5c42f7e333e07a47",
        ("simulation.run", 1): "54ff6e59819bebcd3dd434e58f0a9d2e3413771584b971aff28f8cc1882086ef",
        (
            "task_suite.derive",
            1,
        ): "e72ef147359a4a00d3cbcdb3915a6f0b97749ed30bbdb13d01754bf1a5191c39",
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
