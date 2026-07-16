from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from gameforge.contracts.execution_profiles import (
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    CheckerProfileConfigV1,
    ConstraintExtractionProfileConfigV1,
    EnvironmentContractDescriptorV1,
    ExecutionProfileCatalogSnapshotV1,
    ExecutionProfileDefinitionV1,
    ExecutionProfileLifecycleV1,
    FixedResolvedPolicyRequirementConfigV1,
    GenericProfileDetailsV1,
    GenerationProfileConfigV1,
    MAX_AGENT_PROMPT_MESSAGE_BYTES_V1,
    MAX_CHECKER_CONSTRAINT_COUNT_V1,
    MAX_CHECKER_DIRECT_COUNT_V1,
    MAX_CHECKER_WORK_UNITS_V1,
    MAX_ENVIRONMENT_NAVIGATION_GRID_CELLS_V1,
    MAX_PROFILE_ALLOWLIST_ITEMS_V1,
    MAX_SIMULATION_HORIZON_STEPS_V1,
    MAX_SIMULATION_POPULATION_V1,
    MAX_SIMULATION_WORK_UNITS_V1,
    MAX_WORKLOAD_REPLICATION_COUNT_V1,
    MAX_WORKLOAD_REPLICATION_TICKS_V1,
    MAX_WORKLOAD_WORK_UNITS_V1,
    MigrationCapabilityMatrixV1,
    MigrationEdgeV1,
    MigrationEdgeCapabilityV1,
    MigrationKindDefaultV1,
    PatchRepairProfileConfigV1,
    MigrationProfileDetailsV1,
    ProfileRefV1,
    ResolvedPolicyProfileConfigV1,
    ReviewProfileConfigV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    SimulationProfileConfigV1,
    ValidationProfileDetailsV1,
    WorkloadProfileConfigV1,
    canonical_config_hash,
    execution_profile_catalog_digest,
    migration_capability_matrix_digest,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.lineage import ArtifactKind


_HASH_A = "a" * 64


@pytest.mark.parametrize(
    "config_model",
    (
        GenerationProfileConfigV1,
        PatchRepairProfileConfigV1,
        ReviewProfileConfigV1,
        ConstraintExtractionProfileConfigV1,
    ),
)
def test_agent_profile_prompt_message_cap_is_required(config_model) -> None:
    assert config_model.model_fields["max_prompt_message_bytes"].is_required()


def _definition() -> ExecutionProfileDefinitionV1:
    config = CheckerProfileConfigV1(
        allowed_checker_ids=("graph",),
        allowed_defect_classes=("dangling_reference",),
        max_direct_checker_count=1,
        max_constraint_count=1,
        max_work_units=1_000,
    ).model_dump(mode="json")
    return ExecutionProfileDefinitionV1(
        definition_schema_version="execution-profile@1",
        profile=ProfileRefV1(profile_id="checker.default", version=1),
        profile_kind="checker",
        compatible_run_kinds=(RunKindRef(kind="checker.run", version=1),),
        domain_scope=DomainScope(domain_ids=("structural",)),
        stochastic=False,
        input_schema_ids=("ir-snapshot@1",),
        output_schema_ids=("checker-run@1",),
        required_capabilities=(),
        display_name="Default checker",
        handler_key="checker.default",
        config_schema_id="checker-profile-config@1",
        config=config,
        config_hash=canonical_config_hash(config),
        details=GenericProfileDetailsV1(details_kind="generic"),
    )


def test_profile_refs_and_resolved_binding_are_exact_and_frozen() -> None:
    ref = ProfileRefV1(profile_id="rollback.default", version=1)
    binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/rollback_profile",
        profile=ref,
        expected_profile_kind="rollback",
        profile_payload_hash=_HASH_A,
        catalog_version=3,
        catalog_digest="b" * 64,
    )
    assert binding.profile == ref
    with pytest.raises(ValidationError):
        ProfileRefV1(profile_id="rollback.default", version=0)
    with pytest.raises(ValidationError):
        binding.catalog_version = 4


def test_environment_contract_requires_a_bounded_navigation_search_space() -> None:
    base = {
        "env_contract_version": "generic-agent-env@1",
        "reset_schema_id": "generic-env-reset@1",
        "action_schema_id": "generic-env-action@1",
        "observation_schema_id": "generic-env-observation@1",
    }

    with pytest.raises(ValidationError):
        EnvironmentContractDescriptorV1.model_validate(base)
    with pytest.raises(ValidationError):
        EnvironmentContractDescriptorV1.model_validate(
            {
                **base,
                "max_navigation_grid_cells": (MAX_ENVIRONMENT_NAVIGATION_GRID_CELLS_V1 + 1),
            }
        )


def test_profile_ref_and_direct_profile_identifiers_are_bounded() -> None:
    boundary_id = "p" * 512
    assert ProfileRefV1(profile_id=boundary_id, version=1).profile_id == boundary_id

    with pytest.raises(ValidationError):
        ProfileRefV1(profile_id="p" * 513, version=1)
    with pytest.raises(ValidationError):
        RunKindRef(kind="r" * 513, version=1)
    with pytest.raises(ValidationError):
        ResolvedExecutionProfileBindingV1(
            field_path="/" + "f" * 4096,
            profile=ProfileRefV1(profile_id="checker.default", version=1),
            expected_profile_kind="checker",
            profile_payload_hash="a" * 64,
            catalog_version=1,
            catalog_digest="b" * 64,
        )


def test_definition_rejects_bad_config_hash_or_wrong_detail_variant() -> None:
    definition = _definition()
    with pytest.raises(ValidationError):
        ExecutionProfileDefinitionV1.model_validate(
            {**definition.model_dump(), "config_hash": "b" * 64}
        )
    with pytest.raises(ValidationError):
        ExecutionProfileDefinitionV1.model_validate(
            {
                **definition.model_dump(),
                "profile_kind": "environment",
                "details": {"details_kind": "generic"},
            }
        )


@pytest.mark.parametrize(
    ("profile_kind", "config_schema_id", "config"),
    (
        ("checker", "checker-profile-config@1", {"garbage": "accepted"}),
        ("simulation", "simulation-profile-config@1", {"garbage": "accepted"}),
        ("workload", "workload-profile-config@1", {"garbage": "accepted"}),
    ),
)
def test_authoritative_runtime_profile_configs_are_closed(
    profile_kind: str,
    config_schema_id: str,
    config: dict[str, object],
) -> None:
    definition = _definition().model_dump(mode="json")
    config_hash = canonical_config_hash(config)
    with pytest.raises(ValidationError):
        ExecutionProfileDefinitionV1.model_validate(
            {
                **definition,
                "profile_kind": profile_kind,
                "config_schema_id": config_schema_id,
                "config": config,
                "config_hash": config_hash,
            }
        )


def test_v1_runtime_profile_absolute_caps_fail_closed() -> None:
    checker = {
        "allowed_checker_ids": ("graph",),
        "allowed_defect_classes": ("dangling_reference",),
        "max_direct_checker_count": 1,
        "max_constraint_count": 1,
        "max_work_units": 1,
    }
    for field, value in (
        ("max_direct_checker_count", MAX_CHECKER_DIRECT_COUNT_V1 + 1),
        ("max_constraint_count", MAX_CHECKER_CONSTRAINT_COUNT_V1 + 1),
        ("max_work_units", MAX_CHECKER_WORK_UNITS_V1 + 1),
    ):
        with pytest.raises(ValidationError):
            CheckerProfileConfigV1.model_validate({**checker, field: value})
    with pytest.raises(ValidationError):
        CheckerProfileConfigV1.model_validate(
            {
                **checker,
                "allowed_checker_ids": tuple(
                    f"checker:{index}" for index in range(MAX_PROFILE_ALLOWLIST_ITEMS_V1 + 1)
                ),
            }
        )

    generation_policy = ResolvedPolicyProfileConfigV1(
        resolved_policy_id="generation-gate",
        requirement_sources=(
            FixedResolvedPolicyRequirementConfigV1(
                outcome_rule_id="checker",
                requirement_id="checker",
                artifact_kind="checker_run",
                payload_schema_id="checker-report@1",
                producer_profile_field_path="/params/generation_policy",
                ordinal=1,
            ),
        ),
    )
    generation = {
        "resolved_policy": generation_policy.model_dump(mode="json"),
        "max_prompt_message_bytes": 16 * 1024 * 1024,
        "max_checker_constraint_count": 1,
        "max_checker_work_units": 1,
        "gate_simulation_seed": 0,
        "gate_simulation_population": 1,
        "gate_simulation_horizon_steps": 1,
        "max_simulation_work_units": 1,
    }
    with pytest.raises(ValidationError):
        GenerationProfileConfigV1.model_validate(
            {
                **generation,
                "gate_simulation_population": MAX_SIMULATION_POPULATION_V1 + 1,
            }
        )
    with pytest.raises(ValidationError):
        GenerationProfileConfigV1.model_validate(
            {
                **generation,
                "max_prompt_message_bytes": MAX_AGENT_PROMPT_MESSAGE_BYTES_V1 + 1,
            }
        )
    with pytest.raises(ValidationError):
        GenerationProfileConfigV1.model_validate(
            {
                **generation,
                "gate_simulation_population": 1_001,
                "gate_simulation_horizon_steps": 2_000,
                "max_simulation_work_units": 2_000_000,
            }
        )

    simulation = {
        "default_population": 1,
        "default_horizon_steps": 1,
        "max_horizon_steps": 1,
        "max_output_ticks": 1,
        "max_work_units": 1,
    }
    for field, value in (
        ("default_population", MAX_SIMULATION_POPULATION_V1 + 1),
        ("max_horizon_steps", MAX_SIMULATION_HORIZON_STEPS_V1 + 1),
        ("max_work_units", MAX_SIMULATION_WORK_UNITS_V1 + 1),
    ):
        with pytest.raises(ValidationError):
            SimulationProfileConfigV1.model_validate({**simulation, field: value})

    workload = {
        "max_replication_count": 1,
        "max_total_replication_ticks": 1,
        "max_total_work_units": 1,
    }
    for field, value in (
        ("max_replication_count", MAX_WORKLOAD_REPLICATION_COUNT_V1 + 1),
        ("max_total_replication_ticks", MAX_WORKLOAD_REPLICATION_TICKS_V1 + 1),
        ("max_total_work_units", MAX_WORKLOAD_WORK_UNITS_V1 + 1),
    ):
        with pytest.raises(ValidationError):
            WorkloadProfileConfigV1.model_validate({**workload, field: value})


def test_catalog_requires_exact_definition_lifecycle_set_and_digest() -> None:
    definition = _definition()
    lifecycle = ExecutionProfileLifecycleV1(
        profile=definition.profile,
        state="active",
        revision=1,
        changed_at="2026-07-13T12:00:00Z",
    )
    payload = {
        "catalog_schema_version": "execution-profile-catalog@1",
        "catalog_version": 1,
        "definitions": (definition,),
        "lifecycle": (lifecycle,),
    }
    catalog = ExecutionProfileCatalogSnapshotV1(
        **payload,
        catalog_digest=execution_profile_catalog_digest(payload),
    )
    assert catalog.definitions[0].profile == catalog.lifecycle[0].profile
    assert (
        ExecutionProfileCatalogSnapshotV1.model_validate(catalog.model_dump(mode="json")) == catalog
    )

    with pytest.raises(ValidationError):
        ExecutionProfileCatalogSnapshotV1(
            **{**payload, "lifecycle": ()},
            catalog_digest=execution_profile_catalog_digest({**payload, "lifecycle": ()}),
        )


def test_migration_edge_freezes_golden_required_vs_not_applicable() -> None:
    required = MigrationEdgeV1(
        edge_id="ir@1-to-ir@2",
        source_kind="ir_snapshot",
        source_payload_schema_id="ir@1",
        target_payload_schema_id="ir@2",
        target_meta_schema_version="meta@2",
        golden_replay_policy="required",
        golden_fixture_set_digest=_HASH_A,
    )
    details = MigrationProfileDetailsV1(details_kind="artifact_migrator", edges=(required,))
    assert details.edges[0].golden_fixture_set_digest == _HASH_A
    with pytest.raises(ValidationError):
        MigrationEdgeV1(
            edge_id="ir@1-to-ir@2",
            source_kind="ir_snapshot",
            source_payload_schema_id="ir@1",
            target_payload_schema_id="ir@2",
            target_meta_schema_version="meta@2",
            golden_replay_policy="not_applicable",
        )


def test_validation_profile_keeps_the_exact_workflow_policy_ref() -> None:
    policy = AutoApplyPolicyRefV1(
        registry=AutoApplyPolicyRegistryRefV1(
            registry_version="auto-apply@1",
            registry_digest="b" * 64,
        ),
        policy_id="patch.safe",
        policy_version="1",
        policy_digest="c" * 64,
    )
    details = ValidationProfileDetailsV1(subject_kinds=("patch",), auto_apply_policy=policy)
    assert details.auto_apply_policy == policy


def test_migration_capability_digest_canonicalizes_semantic_collections() -> None:
    defaults = [
        {
            "source_kind": kind,
            "unsupported_edge_action": "reject_409",
        }
        for kind in get_args(ArtifactKind)
    ]
    edges = [
        {
            "source_kind": "ir_snapshot",
            "source_payload_schema_id": "ir@1",
            "target_payload_schema_id": "ir@2",
            "target_meta_schema_version": "meta@2",
            "target_dsl_grammar_version": None,
            "capability": "report_only",
            "publication_lineage_policy_ref": None,
        },
        {
            "source_kind": "constraint_snapshot",
            "source_payload_schema_id": "constraint@1",
            "target_payload_schema_id": "constraint@2",
            "target_meta_schema_version": "meta@2",
            "target_dsl_grammar_version": "dsl@2",
            "capability": "needs_re_compile",
            "publication_lineage_policy_ref": None,
        },
    ]
    forward = {
        "matrix_schema_version": "migration-capability-matrix@1",
        "matrix_version": 1,
        "kind_defaults": defaults,
        "edges": edges,
    }
    reversed_payload = {
        **forward,
        "kind_defaults": list(reversed(defaults)),
        "edges": list(reversed(edges)),
    }
    assert migration_capability_matrix_digest(forward) == (
        migration_capability_matrix_digest(reversed_payload)
    )

    matrix = MigrationCapabilityMatrixV1(
        matrix_version=1,
        kind_defaults=tuple(
            MigrationKindDefaultV1.model_validate(item) for item in reversed(defaults)
        ),
        edges=tuple(MigrationEdgeCapabilityV1.model_validate(item) for item in reversed(edges)),
        matrix_digest=migration_capability_matrix_digest(forward),
    )
    assert matrix.kind_defaults[0].source_kind == get_args(ArtifactKind)[0]


def test_execution_profile_json_pointers_reject_bad_rfc6901_escape() -> None:
    with pytest.raises(ValidationError):
        ResolvedExecutionProfileBindingV1(
            field_path="/bad~2",
            profile=ProfileRefV1(profile_id="checker.default", version=1),
            expected_profile_kind="checker",
            profile_payload_hash="a" * 64,
            catalog_version=1,
            catalog_digest="b" * 64,
        )
