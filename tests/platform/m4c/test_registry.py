from __future__ import annotations

from dataclasses import dataclass

import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileCatalogSnapshotV1,
    ExecutionProfileDefinitionV1,
    GenerationProfileConfigV1,
    PatchRepairProfileConfigV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    execution_profile_catalog_digest,
    execution_profile_payload_hash,
)
from gameforge.contracts.jobs import (
    ArtifactIdentityBindingV1,
    ExecutionIdentityCountBindingV1,
    GraphSelectionV1,
    JsonCollectionCountBindingV1,
    ReviewRunPayloadV1,
    RunPayloadEnvelope,
    RunSchemaBindingV1,
    artifact_lineage_policy_digest,
    outcome_policy_set_digest,
    run_event_registry_digest,
    run_kind_definition_digest,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.playtest import CompletionOracleRegistryRefV1
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.registry.model import (
    FROZEN_RUN_KIND_IDENTITIES_BY_PAYLOAD_SCHEMA,
    FROZEN_RUN_KIND_SHAPES,
)


@dataclass(frozen=True, slots=True)
class _ExpectedRunKind:
    payload_schema_id: str
    creation_mode: str
    command_schema_ids: tuple[str, ...]
    permission_action: str
    permission_resource: str
    executor_key: str
    success_hook: str
    retry_policy_id: str
    llm_modes: tuple[str, ...]
    seed_policy: str
    seed_derivation_version: str | None
    finding_policy_id: str | None
    has_migration_matrix: bool = False


_CANCEL_ONLY = ("run-cancel@1",)
_NOT_APPLICABLE = ("not_applicable",)
_LLM = ("live", "record", "replay")
_ALL_MODES = ("not_applicable", "live", "record", "replay")

_EXPECTED_RUN_KINDS: dict[tuple[str, int], _ExpectedRunKind] = {
    ("generation.propose", 1): _ExpectedRunKind(
        "generation-propose@1",
        "resource_endpoint_only",
        _CANCEL_ONLY,
        "propose",
        "patch",
        "generation_proposer@1",
        "publish_gated_patch_preview@1",
        "llm_transient",
        _LLM,
        "forbidden",
        None,
        None,
    ),
    ("patch.repair", 1): _ExpectedRunKind(
        "patch-repair@1",
        "resource_endpoint_only",
        _CANCEL_ONLY,
        "propose",
        "patch",
        "repair_search@1",
        "publish_patch_revision_preview@1",
        "llm_transient",
        _LLM,
        "profile_dependent",
        "subseed@1",
        None,
    ),
    ("constraint_proposal.propose", 1): _ExpectedRunKind(
        "constraint-proposal-propose@1",
        "resource_endpoint_only",
        _CANCEL_ONLY,
        "propose",
        "constraint_proposal",
        "constraint_proposer@1",
        "publish_constraint_proposal_draft@1",
        "llm_transient",
        _LLM,
        "forbidden",
        None,
        None,
    ),
    ("review.run", 1): _ExpectedRunKind(
        "review-run@1",
        "generic_runs_endpoint",
        _CANCEL_ONLY,
        "run",
        "review",
        "review_runner@1",
        "publish_review@1",
        "composite_transient",
        _ALL_MODES,
        "profile_dependent",
        "subseed@1",
        "review-findings",
    ),
    ("checker.run", 1): _ExpectedRunKind(
        "checker-run@1",
        "generic_runs_endpoint",
        _CANCEL_ONLY,
        "run",
        "checker",
        "checker_runner@1",
        "publish_checker@1",
        "deterministic_job",
        _NOT_APPLICABLE,
        "forbidden",
        None,
        "checker-findings",
    ),
    ("simulation.run", 1): _ExpectedRunKind(
        "simulation-run@1",
        "generic_runs_endpoint",
        _CANCEL_ONLY,
        "run",
        "simulation",
        "simulation_runner@1",
        "publish_simulation@1",
        "deterministic_job",
        _NOT_APPLICABLE,
        "required",
        "subseed@1",
        "simulation-findings",
    ),
    ("task_suite.derive", 1): _ExpectedRunKind(
        "task-suite-derive@1",
        "resource_endpoint_only",
        _CANCEL_ONLY,
        "derive",
        "task_suite",
        "task_suite_deriver@1",
        "publish_task_suite@1",
        "deterministic_job",
        _NOT_APPLICABLE,
        "forbidden",
        None,
        None,
    ),
    ("playtest.run", 1): _ExpectedRunKind(
        "playtest-run@1",
        "resource_endpoint_only",
        ("playtest-provide-input@1", "run-cancel@1"),
        "run",
        "playtest",
        "playtest_runner@1",
        "publish_playtest@1",
        "agent_environment",
        _LLM,
        "required",
        "subseed@1",
        "playtest-findings",
    ),
    ("patch.validate", 1): _ExpectedRunKind(
        "patch-validation@1",
        "resource_endpoint_only",
        _CANCEL_ONLY,
        "validate",
        "patch",
        "patch_validator@1",
        "publish_validation_completion@1",
        "validation_job",
        _NOT_APPLICABLE,
        "profile_dependent",
        "subseed@1",
        "validation-findings",
    ),
    ("constraint_proposal.validate", 1): _ExpectedRunKind(
        "constraint-validation@1",
        "resource_endpoint_only",
        _CANCEL_ONLY,
        "validate",
        "constraint_proposal",
        "constraint_validator@1",
        "publish_validation_completion@1",
        "validation_job",
        _NOT_APPLICABLE,
        "profile_dependent",
        "subseed@1",
        "validation-findings",
    ),
    ("rollback.validate", 1): _ExpectedRunKind(
        "rollback-validation@1",
        "resource_endpoint_only",
        _CANCEL_ONLY,
        "validate",
        "rollback_request",
        "rollback_validator@1",
        "publish_validation_success@1",
        "validation_job",
        _NOT_APPLICABLE,
        "profile_dependent",
        "subseed@1",
        "validation-findings",
    ),
    ("bench.run", 1): _ExpectedRunKind(
        "bench-run@1",
        "generic_runs_endpoint",
        _CANCEL_ONLY,
        "run",
        "bench",
        "bench_runner@1",
        "publish_bench@1",
        "deterministic_job",
        _ALL_MODES,
        "profile_dependent",
        "subseed@1",
        None,
    ),
    ("artifact.migrate", 1): _ExpectedRunKind(
        "artifact-migration@1",
        "internal_only",
        _CANCEL_ONLY,
        "migrate",
        "artifact",
        "artifact_migrator@1",
        "publish_migration@1",
        "migration_job",
        _NOT_APPLICABLE,
        "forbidden",
        None,
        None,
        True,
    ),
    ("dr.drill", 1): _ExpectedRunKind(
        "dr-drill@1",
        "internal_only",
        _CANCEL_ONLY,
        "drill",
        "operations",
        "dr_drill_runner@1",
        "publish_operational_evidence@1",
        "operational_job",
        _NOT_APPLICABLE,
        "forbidden",
        None,
        None,
    ),
}


def _ref(key: tuple[str, int]) -> RunKindRef:
    return RunKindRef(kind=key[0], version=key[1])


def test_builtin_registry_materializes_exact_frozen_run_kind_projection() -> None:
    registry = build_builtin_registry()
    definitions = registry.list_run_kinds()

    assert {(item.kind, item.version) for item in definitions} == set(_EXPECTED_RUN_KINDS)
    assert len(definitions) == 14

    for key, expected in _EXPECTED_RUN_KINDS.items():
        definition = registry.get_run_kind(_ref(key))
        assert definition is not None
        assert definition.status == "active"
        assert definition.payload_schema_id == expected.payload_schema_id
        assert definition.prepared_result_schema_id == "prepared-run-result@1"
        assert definition.prepared_failure_schema_id == "prepared-run-failure@1"
        assert definition.result_schema_id == "run-result@1"
        assert definition.failure_schema_id == "run-failure@1"
        assert definition.creation_mode == expected.creation_mode
        assert definition.allowed_command_schema_ids == expected.command_schema_ids
        assert definition.required_permission.action == expected.permission_action
        assert definition.required_permission.resource_kind == expected.permission_resource
        assert definition.executor_key == expected.executor_key
        assert definition.terminal_hooks.on_success == expected.success_hook
        assert definition.retry_policy.retry_policy_id == expected.retry_policy_id
        assert definition.retry_policy.retry_policy_version == 1
        assert definition.allowed_llm_execution_modes == expected.llm_modes
        assert definition.seed_policy == expected.seed_policy
        assert definition.seed_derivation_version == expected.seed_derivation_version
        finding_ref = definition.finding_output_policy_ref
        assert (finding_ref.policy_id if finding_ref is not None else None) == (
            expected.finding_policy_id
        )
        assert (definition.migration_capability_matrix is not None) is (
            expected.has_migration_matrix
        )


def test_payload_schema_reverse_index_is_derived_from_the_frozen_run_kinds() -> None:
    expected: dict[str, list[tuple[str, int]]] = {}
    for identity, shape in FROZEN_RUN_KIND_SHAPES.items():
        expected.setdefault(shape.payload_schema_id, []).append(identity)

    assert FROZEN_RUN_KIND_IDENTITIES_BY_PAYLOAD_SCHEMA == {
        schema: tuple(sorted(identities)) for schema, identities in expected.items()
    }
    assert all(len(identities) == 1 for identities in expected.values())


def test_agent_execution_budgets_are_inside_hashed_profile_configs() -> None:
    catalog = build_builtin_registry().list_execution_profile_catalogs()[0]
    generation_definition = next(
        item for item in catalog.definitions if item.profile_kind == "generation"
    )
    generation = GenerationProfileConfigV1.model_validate(generation_definition.config)
    assert (
        generation.gate_simulation_seed,
        generation.gate_simulation_population,
        generation.gate_simulation_horizon_steps,
    ) == (0, 30, 120)
    assert generation.max_checker_constraint_count == 256
    assert generation.max_checker_work_units == 2_000_000

    repair_definition = next(
        item for item in catalog.definitions if item.profile_kind == "patch_repair"
    )
    repair = PatchRepairProfileConfigV1.model_validate(repair_definition.config)
    assert repair.max_search_steps == 4


def test_permission_templates_are_closed_by_trusted_domain_resolvers() -> None:
    registry = build_builtin_registry()

    for key in _EXPECTED_RUN_KINDS:
        definition = registry.get_run_kind(_ref(key))
        assert definition is not None
        resolver_key = registry.get_permission_resolver_key(_ref(key))
        if key == ("dr.drill", 1):
            assert definition.required_permission.domain_scope is None
            assert resolver_key is None
        else:
            # `all` is a registry-only marker. Readiness requires a trusted resolver
            # to replace it with the concrete resource-derived scope before authz.
            assert definition.required_permission.domain_scope == "all"
            assert resolver_key is not None


def test_validation_and_common_terminal_hooks_match_the_frozen_split() -> None:
    registry = build_builtin_registry()
    validation_kinds = {
        ("patch.validate", 1),
        ("constraint_proposal.validate", 1),
        ("rollback.validate", 1),
    }

    for key in _EXPECTED_RUN_KINDS:
        definition = registry.get_run_kind(_ref(key))
        assert definition is not None
        hooks = definition.terminal_hooks
        if key in validation_kinds:
            assert hooks.on_failure == "publish_validation_non_success@1"
            assert hooks.on_cancel == "publish_validation_non_success@1"
            assert hooks.on_timeout == "publish_validation_non_success@1"
        else:
            assert hooks.on_failure == "publish_run_failure@1"
            assert hooks.on_cancel == "publish_run_cancel@1"
            assert hooks.on_timeout == "publish_run_timeout@1"


def test_every_run_kind_exact_policy_reference_resolves_and_rehashes() -> None:
    registry = build_builtin_registry()

    for definition in registry.list_run_kinds():
        run_kind = RunKindRef(kind=definition.kind, version=definition.version)
        assert run_kind_definition_digest(definition) == canonical_sha256(
            definition.model_dump(mode="json")
        )
        assert outcome_policy_set_digest(run_kind, definition.outcome_policies)

        runtime_parents = registry.get_runtime_parent_rule_set(definition.runtime_parent_rule_set)
        assert runtime_parents is not None
        assert definition.runtime_parent_rule_set.digest == canonical_sha256(
            runtime_parents.model_dump(mode="json")
        )

        classifier = registry.get_failure_classifier(definition.failure_classifier)
        retry = registry.get_retry_policy(definition.retry_policy)
        assert classifier is not None
        assert retry is not None

        if definition.finding_output_policy_ref is not None:
            finding = registry.get_finding_output_policy(definition.finding_output_policy_ref)
            assert finding is not None
            assert definition.finding_output_policy_ref.digest == canonical_sha256(
                finding.model_dump(mode="json")
            )

        if definition.migration_capability_matrix is not None:
            matrix = registry.get_migration_capability_matrix(
                definition.migration_capability_matrix
            )
            assert matrix is not None
            assert matrix.matrix_digest == definition.migration_capability_matrix.matrix_digest

        for outcome_policy in definition.outcome_policies:
            transition = registry.get_version_transition_policy(
                outcome_policy.version_transition_policy_ref
            )
            assert transition is not None
            assert outcome_policy.version_transition_policy_ref.digest == canonical_sha256(
                transition.model_dump(mode="json")
            )
            for rule in outcome_policy.artifact_rules:
                lineage = registry.get_lineage_policy(rule.lineage_policy_ref)
                assert lineage is not None
                assert rule.lineage_policy_ref.digest == artifact_lineage_policy_digest(lineage)


def test_frozen_lineage_projects_existing_canonical_facts_from_typed_parents() -> None:
    registry = build_builtin_registry()
    expected = {
        ("generation.propose", "generation-gate-pass", "primary"): {
            "doc_version": "snapshot",
            "ir_snapshot_id": "snapshot",
            "constraint_snapshot_id": "constraint",
        },
        ("patch.repair", "repair-verified", "primary"): {
            "doc_version": "base",
            "ir_snapshot_id": "base",
            "constraint_snapshot_id": "constraint",
        },
        ("review.run", "review-completed", "primary"): {
            "doc_version": "snapshot",
            "ir_snapshot_id": "snapshot",
            "constraint_snapshot_id": "constraint",
        },
        ("playtest.run", "playtest-completed", "primary"): {
            "doc_version": "config",
            "ir_snapshot_id": "config",
            "constraint_snapshot_id": "constraint",
            "env_contract_version": "config",
        },
        ("checker.run", "checker-completed", "primary"): {
            "ir_snapshot_id": "snapshot",
            "constraint_snapshot_id": "constraint",
        },
        ("simulation.run", "simulation-completed", "primary"): {
            "ir_snapshot_id": "snapshot",
            "constraint_snapshot_id": "constraint",
            "env_contract_version": "scenario",
        },
        ("patch.validate", "patch-validation-passed", "primary"): {
            "doc_version": "target",
            "ir_snapshot_id": "target",
            "constraint_snapshot_id": "constraint",
            "env_contract_version": "candidate_config",
        },
        (
            "constraint_proposal.validate",
            "constraint-validated-with-candidate",
            "primary",
        ): {
            "doc_version": "candidate",
            "ir_snapshot_id": "candidate",
            "constraint_snapshot_id": "candidate",
        },
        (
            "constraint_proposal.validate",
            "constraint-validation-failed-without-candidate",
            "primary",
        ): {
            "doc_version": "proposal",
            "ir_snapshot_id": "proposal",
            "constraint_snapshot_id": "proposal",
        },
        ("rollback.validate", "rollback-validation-passed", "primary"): {
            "doc_version": "target",
            "ir_snapshot_id": "target",
            "constraint_snapshot_id": "target",
            "env_contract_version": "target",
        },
        ("bench.run", "bench-completed", "primary"): {
            "doc_version": "dataset",
            "ir_snapshot_id": "dataset",
            "constraint_snapshot_id": "dataset",
            "env_contract_version": "dataset",
        },
        ("dr.drill", "dr-drill-completed", "primary"): {
            "doc_version": "recovery_manifest",
            "ir_snapshot_id": "recovery_manifest",
            "constraint_snapshot_id": "recovery_manifest",
            "env_contract_version": "recovery_manifest",
        },
    }
    definitions = {item.kind: item for item in registry.list_run_kinds()}

    for (run_kind, policy_id, rule_id), fields in expected.items():
        policy = next(
            item for item in definitions[run_kind].outcome_policies if item.policy_id == policy_id
        )
        artifact_rule = next(item for item in policy.artifact_rules if item.rule_id == rule_id)
        lineage = registry.get_lineage_policy(artifact_rule.lineage_policy_ref)
        assert lineage is not None
        projections = {item.field: item for item in lineage.version_projection}
        for field, parent_role in fields.items():
            assert projections[field].source == "parent_role"
            assert projections[field].parent_role == parent_role

    migration = definitions["artifact.migrate"]

    review = definitions["review.run"]
    review_policy = next(
        item for item in review.outcome_policies if item.policy_id == "review-completed"
    )
    review_rule = next(item for item in review_policy.artifact_rules if item.rule_id == "primary")
    review_lineage = registry.get_lineage_policy(review_rule.lineage_policy_ref)
    assert review_lineage is not None
    review_projections = {item.field: item for item in review_lineage.version_projection}
    assert review_projections["seed"].source == "producer_value"

    migration_policy = next(
        item
        for item in migration.outcome_policies
        if item.policy_id == "artifact-migration-reported"
    )
    migration_rule = next(
        item for item in migration_policy.artifact_rules if item.rule_id == "primary"
    )
    migration_lineage = registry.get_lineage_policy(migration_rule.lineage_policy_ref)
    assert migration_lineage is not None
    for projection in migration_lineage.version_projection:
        expected_source = "producer_value" if projection.field == "tool_version" else "parent_role"
        assert projection.source == expected_source
        if expected_source == "parent_role":
            assert projection.parent_role == "source"

    allowed_local_canonical_fields = {
        "doc_version": set(),
        "ir_snapshot_id": {"ir_snapshot"},
        "constraint_snapshot_id": {"constraint_snapshot"},
        "env_contract_version": {
            "config_export",
            "regression_evidence",
            "simulation_run",
        },
    }
    for definition in definitions.values():
        for policy in definition.outcome_policies:
            for artifact_rule in policy.artifact_rules:
                lineage = registry.get_lineage_policy(artifact_rule.lineage_policy_ref)
                assert lineage is not None
                projections = {item.field: item for item in lineage.version_projection}
                for field, allowed_kinds in allowed_local_canonical_fields.items():
                    if projections[field].source == "producer_value":
                        assert lineage.child_kind in allowed_kinds


def test_repair_regression_evidence_selects_one_exact_suite_from_payload() -> None:
    registry = build_builtin_registry()
    definition = registry.get_run_kind(_ref(("patch.repair", 1)))
    assert definition is not None
    policy = next(
        item for item in definition.outcome_policies if item.policy_id == "repair-verified"
    )
    rule = next(item for item in policy.artifact_rules if item.rule_id == "regression")
    lineage = registry.get_lineage_policy(rule.lineage_policy_ref)
    assert lineage is not None

    suite = next(item for item in lineage.parent_rules if item.parent_role == "regression_suite")
    assert suite.source == "child_payload_reference"
    assert suite.child_payload_pointer == "/suite_artifact_id"
    assert suite.artifact_kinds == ("regression_suite",)
    assert suite.payload_schema_ids == ("regression-suite@1",)
    assert suite.min_count == 1
    assert suite.max_count == 1
    assert rule.lineage_policy_ref.digest == artifact_lineage_policy_digest(lineage)


@pytest.mark.parametrize(
    ("run_kind", "policy_id", "suite_pointer", "suite_min_count"),
    (
        (
            "patch.validate",
            "patch-validation-passed",
            "/lineage_suite_artifact_ids",
            0,
        ),
        (
            "constraint_proposal.validate",
            "constraint-validated-with-candidate",
            "/suite_artifact_id",
            1,
        ),
        (
            "rollback.validate",
            "rollback-validation-passed",
            "/lineage_suite_artifact_ids",
            0,
        ),
    ),
)
def test_validation_regression_environment_is_terminally_derived_per_suite(
    run_kind: str,
    policy_id: str,
    suite_pointer: str,
    suite_min_count: int,
) -> None:
    registry = build_builtin_registry()
    definition = registry.get_run_kind(RunKindRef(kind=run_kind, version=1))
    assert definition is not None
    policy = next(item for item in definition.outcome_policies if item.policy_id == policy_id)
    regression_rule = next(item for item in policy.artifact_rules if item.rule_id == "regression")
    regression_lineage = registry.get_lineage_policy(regression_rule.lineage_policy_ref)
    assert regression_lineage is not None

    suite = next(
        item for item in regression_lineage.parent_rules if item.parent_role == "regression_suite"
    )
    assert suite.source == "child_payload_reference"
    assert suite.child_payload_pointer == suite_pointer
    assert suite.min_count == suite_min_count
    assert suite.max_count == 1
    env_projection = next(
        item
        for item in regression_lineage.version_projection
        if item.field == "env_contract_version"
    )
    assert env_projection.source == "producer_value"

    primary_rule = next(item for item in policy.artifact_rules if item.rule_id == "primary")
    primary_lineage = registry.get_lineage_policy(primary_rule.lineage_policy_ref)
    assert primary_lineage is not None
    primary_env = next(
        item for item in primary_lineage.version_projection if item.field == "env_contract_version"
    )
    assert "regression" not in primary_env.equality_parent_roles


def test_runtime_parent_rules_close_record_and_replay_cassette_scopes() -> None:
    registry = build_builtin_registry()
    definition = registry.get_run_kind(_ref(("review.run", 1)))
    assert definition is not None
    rule_set = registry.get_runtime_parent_rule_set(definition.runtime_parent_rule_set)
    assert rule_set is not None
    rules = {rule.rule_id: rule for rule in rule_set.rules}

    attempt_shards = rules["attempt-record-shards"]
    assert attempt_shards.enabled_execution_modes == ("record",)
    assert attempt_shards.manifest_scope == "attempt"
    assert attempt_shards.attempt_selector == "current"
    assert attempt_shards.count_binding == ExecutionIdentityCountBindingV1(
        scope="current_attempt",
    )

    run_shards = rules["run-record-shards"]
    assert run_shards.enabled_execution_modes == ("record",)
    assert run_shards.manifest_scope == "run"
    assert run_shards.attempt_selector == "all_closed"
    assert run_shards.count_binding == ExecutionIdentityCountBindingV1(
        scope="all_attempts",
    )

    for rule_id, scope, selector, source in (
        ("attempt-cassette-bundle", "attempt", "current", "attempt_bundle"),
        ("run-cassette-bundle", "run", "all_closed", "run_bundle"),
    ):
        rule = rules[rule_id]
        assert rule.enabled_execution_modes == ("record",)
        assert (rule.manifest_scope, rule.attempt_selector, rule.source) == (
            scope,
            selector,
            source,
        )

    replay = rules["replay-input-cassette-bundle"]
    assert replay.enabled_execution_modes == ("replay",)
    assert replay.manifest_scope == "both"
    assert replay.source == "run_input"
    assert replay.min_count == replay.max_count == 1


def test_review_outputs_bind_one_to_one_to_exact_profile_refs() -> None:
    registry = build_builtin_registry()
    definition = registry.get_run_kind(_ref(("review.run", 1)))
    assert definition is not None
    policy = next(
        item for item in definition.outcome_policies if item.policy_id == "review-completed"
    )
    rules = {rule.rule_id: rule for rule in policy.artifact_rules}

    for rule_id, pointer in (
        ("checker", "/params/checker_profiles"),
        ("simulation", "/params/simulation_profiles"),
    ):
        binding = rules[rule_id].count_binding
        assert isinstance(binding, JsonCollectionCountBindingV1)
        assert binding.collection_pointer == pointer
        assert binding.identity_binding == ArtifactIdentityBindingV1(
            collection_item_pointer="",
            artifact_value_source="payload",
            artifact_payload_pointer="/profile",
        )


def test_builtin_simulation_profile_is_the_only_stochastic_profile() -> None:
    catalog = build_builtin_registry().list_execution_profile_catalogs()[0]
    stochastic = {
        definition.profile_kind for definition in catalog.definitions if definition.stochastic
    }
    assert stochastic == {"simulation"}


def test_exact_history_getters_never_substitute_current_aliases() -> None:
    registry = build_builtin_registry()
    definition = registry.get_run_kind(RunKindRef(kind="generation.propose", version=1))
    assert definition is not None

    assert registry.get_run_kind(RunKindRef(kind=definition.kind, version=2)) is None
    assert (
        registry.get_retry_policy(
            definition.retry_policy.model_copy(
                update={"retry_policy_version": definition.retry_policy.retry_policy_version + 1}
            )
        )
        is None
    )
    assert (
        registry.get_runtime_parent_rule_set(
            definition.runtime_parent_rule_set.model_copy(update={"digest": "0" * 64})
        )
        is None
    )

    policy = definition.outcome_policies[0]
    assert (
        registry.get_version_transition_policy(
            policy.version_transition_policy_ref.model_copy(update={"digest": "0" * 64})
        )
        is None
    )
    if policy.artifact_rules:
        lineage_ref = policy.artifact_rules[0].lineage_policy_ref
        assert (
            registry.get_lineage_policy(lineage_ref.model_copy(update={"digest": "0" * 64})) is None
        )


def test_profile_oracle_and_event_metadata_are_exactly_addressable() -> None:
    registry = build_builtin_registry()

    catalogs = registry.list_execution_profile_catalogs()
    assert catalogs
    for catalog in catalogs:
        assert (
            registry.get_execution_profile_catalog(
                catalog.catalog_version,
                catalog.catalog_digest,
            )
            == catalog
        )
        assert (
            registry.get_execution_profile_catalog(
                catalog.catalog_version,
                "0" * 64,
            )
            is None
        )
    for definition in registry.list_run_kinds():
        requirements = registry.get_profile_requirements(
            RunKindRef(kind=definition.kind, version=definition.version)
        )
        assert requirements is not None
        assert len({item.field_path for item in requirements}) == len(requirements)

    oracle_registries = registry.completion_oracle_registries
    assert oracle_registries
    for oracle_registry in oracle_registries:
        ref = CompletionOracleRegistryRefV1(
            registry_version=oracle_registry.registry_version,
            digest=oracle_registry.registry_digest,
        )
        assert registry.get_completion_oracle_registry(ref) == oracle_registry
        assert (
            registry.get_completion_oracle_registry(ref.model_copy(update={"digest": "0" * 64}))
            is None
        )

    event_registries = registry.run_event_registries
    assert event_registries
    for event_registry in event_registries:
        assert event_registry.registry_digest == run_event_registry_digest(event_registry)
        assert len(event_registry.definitions) == 14
        assert (
            registry.get_run_event_registry(
                event_registry.registry_version,
                event_registry.registry_digest,
            )
            == event_registry
        )
        assert (
            registry.get_run_event_registry(
                event_registry.registry_version,
                "0" * 64,
            )
            is None
        )


def test_profile_definition_cannot_change_across_retained_catalogs() -> None:
    registry = build_builtin_registry()
    latest = registry.list_execution_profile_catalogs()[-1]
    definitions = list(latest.definitions)
    definitions[0] = definitions[0].model_copy(
        update={"display_name": f"{definitions[0].display_name} conflicting"}
    )
    payload = {
        "catalog_schema_version": latest.catalog_schema_version,
        "catalog_version": latest.catalog_version + 1,
        "definitions": definitions,
        "lifecycle": latest.lifecycle,
    }
    conflicting = ExecutionProfileCatalogSnapshotV1(
        **payload,
        catalog_digest=execution_profile_catalog_digest(payload),
    )

    with pytest.raises(IntegrityViolation, match="conflicting retained history"):
        registry.with_execution_profile_catalogs((conflicting,), replace=False)


@dataclass(frozen=True, slots=True)
class _ReviewProfileSelection:
    catalog: ExecutionProfileCatalogSnapshotV1
    review: ExecutionProfileDefinitionV1
    checker: ExecutionProfileDefinitionV1


def _active_review_profile_selection(registry: object) -> _ReviewProfileSelection:
    run_kind = RunKindRef(kind="review.run", version=1)
    candidates: list[_ReviewProfileSelection] = []
    for catalog in registry.list_execution_profile_catalogs():
        lifecycle = {
            (item.profile.profile_id, item.profile.version): item.state
            for item in catalog.lifecycle
        }
        compatible = [
            definition
            for definition in catalog.definitions
            if run_kind in definition.compatible_run_kinds
            and lifecycle[(definition.profile.profile_id, definition.profile.version)] == "active"
        ]
        review_profiles = [
            definition for definition in compatible if definition.profile_kind == "review"
        ]
        checker_profiles = [
            definition for definition in compatible if definition.profile_kind == "checker"
        ]
        if review_profiles and checker_profiles:
            candidates.append(
                _ReviewProfileSelection(
                    catalog=catalog,
                    review=review_profiles[0],
                    checker=checker_profiles[0],
                )
            )

    assert candidates, (
        "builtin registry must retain an active catalog with compatible review and "
        "checker profiles for review.run@1"
    )
    return max(candidates, key=lambda item: item.catalog.catalog_version)


def _compatible_profile(
    selection: _ReviewProfileSelection,
    profile_kind: str,
) -> ExecutionProfileDefinitionV1:
    run_kind = RunKindRef(kind="review.run", version=1)
    lifecycle = {
        (item.profile.profile_id, item.profile.version): item.state
        for item in selection.catalog.lifecycle
    }
    matches = [
        definition
        for definition in selection.catalog.definitions
        if definition.profile_kind == profile_kind
        and run_kind in definition.compatible_run_kinds
        and lifecycle[(definition.profile.profile_id, definition.profile.version)] == "active"
    ]
    assert matches, f"builtin catalog lacks an active {profile_kind} profile for review.run@1"
    return matches[0]


def _profile_binding(
    *,
    field_path: str,
    definition: ExecutionProfileDefinitionV1,
    catalog: ExecutionProfileCatalogSnapshotV1,
) -> ResolvedExecutionProfileBindingV1:
    return ResolvedExecutionProfileBindingV1(
        field_path=field_path,
        profile=definition.profile,
        expected_profile_kind=definition.profile_kind,
        profile_payload_hash=execution_profile_payload_hash(definition),
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    )


def _review_envelope(
    selection: _ReviewProfileSelection,
    bindings: tuple[ResolvedExecutionProfileBindingV1, ...] | None = None,
) -> RunPayloadEnvelope:
    params = ReviewRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=selection.review.profile,
        checker_profiles=(selection.checker.profile,),
        simulation_profiles=(),
        llm_triage_policy=None,
    )
    resolved_profiles = _valid_review_bindings(selection) if bindings is None else bindings
    return RunPayloadEnvelope(
        payload_schema_version="review-run@1",
        input_artifact_ids=("artifact:snapshot",),
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:input",
            tool_version="review@1",
        ),
        policy_bindings=(),
        schema_bindings=(
            RunSchemaBindingV1(
                binding_key="run_payload",
                schema_id="review-run@1",
            ),
        ),
        execution_profile_catalog_version=selection.catalog.catalog_version,
        execution_profile_catalog_digest=selection.catalog.catalog_digest,
        resolved_profiles=resolved_profiles,
        resolved_policy_snapshots=(),
        budget_set_snapshot_id="budget-set:1",
        llm_execution_mode="not_applicable",
        params=params,
    )


def _valid_review_bindings(
    selection: _ReviewProfileSelection,
) -> tuple[ResolvedExecutionProfileBindingV1, ...]:
    return (
        _profile_binding(
            field_path="/params/review_profile",
            definition=selection.review,
            catalog=selection.catalog,
        ),
        _profile_binding(
            field_path="/params/checker_profiles/0",
            definition=selection.checker,
            catalog=selection.catalog,
        ),
    )


def _review_envelope_with_profile(
    selection: _ReviewProfileSelection,
    *,
    definition: ExecutionProfileDefinitionV1,
    field_path: str,
    params_field: str,
    seed: int | None = None,
) -> RunPayloadEnvelope:
    envelope = _review_envelope(selection)
    wire = envelope.model_dump(mode="python")
    params = dict(wire["params"])
    params[params_field] = (
        [definition.profile.model_dump(mode="python")]
        if field_path.endswith("/0")
        else definition.profile.model_dump(mode="python")
    )
    wire["params"] = params
    wire["seed"] = seed
    wire["resolved_profiles"] = [
        *wire["resolved_profiles"],
        _profile_binding(
            field_path=field_path,
            definition=definition,
            catalog=selection.catalog,
        ).model_dump(mode="python"),
    ]
    return RunPayloadEnvelope.model_validate(wire)


def test_many_profile_requirement_binds_each_payload_array_item_exactly() -> None:
    registry = build_builtin_registry()
    selection = _active_review_profile_selection(registry)
    run_kind = RunKindRef(kind="review.run", version=1)
    definition = registry.get_run_kind(run_kind)
    assert definition is not None
    requirements = registry.get_profile_requirements(run_kind)
    assert requirements is not None
    requirements_by_path = {item.field_path: item for item in requirements}
    assert requirements_by_path["/params/review_profile"].cardinality == "one"
    assert requirements_by_path["/params/checker_profiles"].cardinality == "many"
    assert requirements_by_path["/params/simulation_profiles"].cardinality == "many"
    assert requirements_by_path["/params/llm_triage_policy"].cardinality == "optional"

    registry.validate_payload_bindings(
        payload=_review_envelope(selection),
        definition=definition,
    )


@pytest.mark.parametrize(
    "case",
    ["missing", "extra", "gap", "wrong_kind", "wrong_profile"],
)
def test_many_profile_requirement_rejects_non_exact_array_bindings(case: str) -> None:
    registry = build_builtin_registry()
    selection = _active_review_profile_selection(registry)
    definition = registry.get_run_kind(RunKindRef(kind="review.run", version=1))
    assert definition is not None
    bindings = list(_valid_review_bindings(selection))

    if case == "missing":
        bindings.pop()
    elif case == "extra":
        bindings.append(
            _profile_binding(
                field_path="/params/checker_profiles/1",
                definition=selection.checker,
                catalog=selection.catalog,
            )
        )
    elif case == "gap":
        bindings[-1] = bindings[-1].model_copy(update={"field_path": "/params/checker_profiles/1"})
    elif case == "wrong_kind":
        bindings[-1] = bindings[-1].model_copy(update={"expected_profile_kind": "simulation"})
    else:
        bindings[-1] = bindings[-1].model_copy(update={"profile": selection.review.profile})

    with pytest.raises(IntegrityViolation):
        registry.validate_payload_bindings(
            payload=_review_envelope(selection, tuple(bindings)),
            definition=definition,
        )


def test_profile_dependent_seed_tracks_exact_stochastic_profile_bindings() -> None:
    registry = build_builtin_registry()
    selection = _active_review_profile_selection(registry)
    definition = registry.get_run_kind(RunKindRef(kind="review.run", version=1))
    assert definition is not None
    simulation = _compatible_profile(selection, "simulation")
    assert simulation.stochastic is True

    without_seed = _review_envelope_with_profile(
        selection,
        definition=simulation,
        field_path="/params/simulation_profiles/0",
        params_field="simulation_profiles",
    )
    with pytest.raises(IntegrityViolation):
        registry.validate_payload_bindings(payload=without_seed, definition=definition)

    registry.validate_payload_bindings(
        payload=without_seed.model_copy(update={"seed": 17}),
        definition=definition,
    )

    with pytest.raises(IntegrityViolation):
        registry.validate_payload_bindings(
            payload=_review_envelope(selection).model_copy(update={"seed": 17}),
            definition=definition,
        )


def test_review_not_applicable_mode_rejects_an_llm_triage_profile() -> None:
    registry = build_builtin_registry()
    selection = _active_review_profile_selection(registry)
    definition = registry.get_run_kind(RunKindRef(kind="review.run", version=1))
    assert definition is not None
    llm_triage = _compatible_profile(selection, "llm_triage")
    envelope = _review_envelope_with_profile(
        selection,
        definition=llm_triage,
        field_path="/params/llm_triage_policy",
        params_field="llm_triage_policy",
    )

    with pytest.raises(IntegrityViolation):
        registry.validate_payload_bindings(payload=envelope, definition=definition)
