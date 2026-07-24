from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_graphs import (
    AgentExecutionGraphV1,
    AgentExecutionProfileSelectorV1,
    agent_execution_graph_digest,
)
from gameforge.contracts.execution_profiles import (
    ExecutionProfileCatalogSnapshotV1,
    MigrationEdgeV1,
    MigrationProfileDetailsV1,
    ProfileRefV1,
    RunKindRef,
    execution_profile_catalog_digest,
)
from gameforge.contracts.jobs import IntermediateCountBindingV1
from gameforge.platform.registry import (
    ImmutablePlatformRegistry,
    PlatformReadinessValidator,
    TrustedComponentMaps,
    build_builtin_registry,
)
from gameforge.platform.run_handlers.deferred import DEFERRED_EXECUTORS


_NON_DEFERRED_EXECUTOR_KEYS = {
    "generation_proposer@1",
    "repair_search@1",
    "constraint_proposer@1",
    "review_runner@1",
    "checker_runner@1",
    "simulation_runner@1",
    "task_suite_deriver@1",
    "playtest_runner@1",
    "patch_validator@1",
    "constraint_validator@1",
    "rollback_validator@1",
    "bench_runner@1",
}

_TERMINAL_HOOK_KEYS = {
    "publish_gated_patch_preview@1",
    "publish_patch_revision_preview@1",
    "publish_constraint_proposal_draft@1",
    "publish_review@1",
    "publish_checker@1",
    "publish_simulation@1",
    "publish_task_suite@1",
    "publish_playtest@1",
    "publish_validation_completion@1",
    "publish_validation_success@1",
    "publish_bench@1",
    "publish_migration@1",
    "publish_operational_evidence@1",
    "publish_validation_non_success@1",
    "publish_run_failure@1",
    "publish_run_cancel@1",
    "publish_run_timeout@1",
}

_WORKFLOW_EFFECT_KEYS = {
    "create_patch_subject_head_and_draft@1",
    "no_workflow_subject@1",
    "close_attempt_for_terminal@1",
    "supersede_patch_head_create_draft@1",
    "leave_patch_head_unchanged@1",
    "create_constraint_subject_head_and_draft@1",
    "no_workflow_change@1",
    "set_patch_validated@1",
    "set_patch_validated_with_auto_proof@1",
    "set_patch_validation_failed@1",
    "set_exact_binding_and_validated@1",
    "set_exact_binding_and_validation_failed@1",
    "leave_binding_null_and_validation_failed@1",
    "set_rollback_validated@1",
    "set_rollback_validation_failed@1",
    "close_attempt_for_retry@1",
    "terminal_only@1",
    "restore_current_draft@1",
}

_DEFERRED_EXECUTOR_KEYS = ("artifact_migrator@1", "dr_drill_runner@1")


def _trusted_component(*args: Any, **kwargs: Any) -> None:
    del args, kwargs


def _profile_handler_keys(registry: Any) -> set[str]:
    keys: set[str] = set()
    for catalog in registry.list_execution_profile_catalogs():
        states = {
            (item.profile.profile_id, item.profile.version): item.state
            for item in catalog.lifecycle
        }
        for definition in catalog.definitions:
            ref = (definition.profile.profile_id, definition.profile.version)
            if states[ref] in {"active", "replay_only"}:
                keys.add(definition.handler_key)
    return keys


def _completion_oracle_keys(registry: Any) -> set[str]:
    return {
        definition.executor_key
        for oracle_registry in registry.completion_oracle_registries
        for definition in oracle_registry.definitions
    }


def _playtest_payload_validator_keys(registry: Any) -> set[str]:
    return {
        definition.validator_key
        for schema_registry in registry.playtest_payload_schema_registries
        for definition in schema_registry.definitions
    }


def _permission_resolver_keys(registry: Any) -> set[str]:
    return {
        resolver_key
        for definition in registry.list_run_kinds()
        if (
            resolver_key := registry.get_permission_resolver_key(
                RunKindRef(kind=definition.kind, version=definition.version)
            )
        )
        is not None
    }


def test_generation_v2_graph_is_active_while_v1_remains_replayable() -> None:
    registry = build_builtin_registry()

    graphs = registry.list_agent_execution_graphs_for_run_kind(
        RunKindRef(kind="generation.propose", version=1)
    )

    assert [
        (
            graph.agent_graph_version,
            graph.status,
            graph.nodes[0].prompt_version,
            graph.nodes[0].tool_version,
        )
        for graph in graphs
    ] == [
        ("generation-graph@1", "replay_only", "generation@1", "generation@1"),
        ("generation-graph@2", "active", "generation@2", "generation@1"),
    ]


def _components(registry: Any) -> TrustedComponentMaps:
    assert set(DEFERRED_EXECUTORS) == set(_DEFERRED_EXECUTOR_KEYS)
    assert _NON_DEFERRED_EXECUTOR_KEYS.isdisjoint(DEFERRED_EXECUTORS)
    executors = {key: _trusted_component for key in _NON_DEFERRED_EXECUTOR_KEYS}
    executors.update(DEFERRED_EXECUTORS)
    return TrustedComponentMaps(
        executors=executors,
        terminal_hooks={key: _trusted_component for key in _TERMINAL_HOOK_KEYS},
        workflow_effects={key: _trusted_component for key in _WORKFLOW_EFFECT_KEYS},
        completion_oracles={key: _trusted_component for key in _completion_oracle_keys(registry)},
        playtest_payload_validators={
            key: _trusted_component for key in _playtest_payload_validator_keys(registry)
        },
        profile_handlers={key: _trusted_component for key in _profile_handler_keys(registry)},
        permission_domain_resolvers={
            key: _trusted_component for key in _permission_resolver_keys(registry)
        },
    )


def test_builtin_registry_and_exact_trusted_maps_are_ready() -> None:
    registry = build_builtin_registry()
    report = PlatformReadinessValidator(
        registry=registry,
        components=_components(registry),
    ).validate()

    assert report.ready is True
    assert report.checked_run_kind_count == 14
    assert report.reference_checks > 0
    assert report.deferred_executor_keys == _DEFERRED_EXECUTOR_KEYS


def test_task11_execution_profile_catalog_remains_byte_identical() -> None:
    catalog = build_builtin_registry().list_execution_profile_catalogs()[0]

    assert catalog.catalog_version == 1
    assert catalog.catalog_digest == (
        "6473a8a4fe0d92133c97f57005e668881743b69206481d7dedec854f248647e4"
    )
    assert len(canonical_json(catalog.model_dump(mode="json")).encode("utf-8")) == 23_745


def test_readiness_rejects_record_shards_counted_from_prompt_links() -> None:
    registry = build_builtin_registry()
    definition = registry.get_run_kind(RunKindRef(kind="review.run", version=1))
    assert definition is not None
    rule_set = registry.get_runtime_parent_rule_set(definition.runtime_parent_rule_set)
    assert rule_set is not None
    malformed_rules = tuple(
        rule.model_copy(
            update={
                "count_binding": IntermediateCountBindingV1(
                    link_role="prompt_rendered",
                    scope=(
                        "current_attempt" if rule.manifest_scope == "attempt" else "all_attempts"
                    ),
                )
            }
        )
        if rule.source == "record_shard"
        else rule
        for rule in rule_set.rules
    )
    malformed = rule_set.model_copy(update={"rules": malformed_rules})

    with pytest.raises(IntegrityViolation, match="consumed execution-identity calls"):
        PlatformReadinessValidator(
            registry=_RuntimeRulesOverrideRegistry(registry, malformed),
            components=_components(registry),
        ).validate()


def test_builtin_playtest_graph_selectors_cover_exact_versioned_memory_config() -> None:
    registry = build_builtin_registry()
    playtest = RunKindRef(kind="playtest.run", version=1)
    graphs = tuple(
        graph
        for graph in registry.list_agent_execution_graphs()
        if graph.run_kind == playtest and graph.status == "active"
    )

    assert {
        (
            graph.profile_selector.profile_field_path,
            graph.profile_selector.config_pointer,
            graph.profile_selector.expected_value,
        )
        for graph in graphs
        if graph.profile_selector is not None
    } == {
        ("/params/planner_policy", "/memory_mode", "off"),
        ("/params/planner_policy", "/memory_mode", "llm_compaction"),
    }
    catalog = registry.list_execution_profile_catalogs()[0]
    planner = next(
        definition
        for definition in catalog.definitions
        if definition.profile_kind == "playtest_planner"
    )
    assert planner.config["memory_mode"] == "off"


def test_readiness_rejects_selector_graphs_that_miss_reachable_profile_config() -> None:
    registry = build_builtin_registry()
    graphs = tuple(
        graph
        for graph in registry.list_agent_execution_graphs()
        if graph.agent_graph_version != "playtest-core-graph@1"
    )

    with pytest.raises(IntegrityViolation, match="reachable profile config"):
        PlatformReadinessValidator(
            registry=_GraphsOverrideRegistry(registry, graphs),
            components=_components(registry),
        ).validate()


def test_readiness_rejects_overlapping_playtest_selector_branches() -> None:
    registry = build_builtin_registry()
    graphs = tuple(registry.list_agent_execution_graphs())
    memory = next(
        graph for graph in graphs if graph.agent_graph_version == "playtest-memory-graph@1"
    )
    duplicate = _replace_graph(
        memory,
        profile_selector=AgentExecutionProfileSelectorV1(
            profile_field_path="/params/planner_policy",
            config_pointer="/memory_mode",
            expected_value="off",
        ),
    )
    changed = tuple(duplicate if graph is memory else graph for graph in graphs)

    with pytest.raises(IntegrityViolation, match="branches overlap"):
        PlatformReadinessValidator(
            registry=_GraphsOverrideRegistry(registry, changed),
            components=_components(registry),
        ).validate()


def test_readiness_rejects_mixed_unconditional_and_selector_graph_branches() -> None:
    registry = build_builtin_registry()
    graphs = tuple(registry.list_agent_execution_graphs())
    core = next(graph for graph in graphs if graph.agent_graph_version == "playtest-core-graph@1")
    unconditional = _replace_graph(core, profile_selector=None)
    changed = tuple(unconditional if graph is core else graph for graph in graphs)

    with pytest.raises(IntegrityViolation, match="unconditional active Agent graph"):
        PlatformReadinessValidator(
            registry=_GraphsOverrideRegistry(registry, changed),
            components=_components(registry),
        ).validate()


def test_readiness_retains_historical_graph_for_disabled_run_kind_version() -> None:
    registry = build_builtin_registry()
    definitions, graphs = _with_historical_generation_graph(registry)
    historical_registry = _registry_with_run_kinds_and_graphs(
        registry,
        definitions=definitions,
        graphs=graphs,
    )

    report = PlatformReadinessValidator(
        registry=historical_registry,
        components=_components(registry),
    ).validate()

    assert report.ready is True
    historical = graphs[-1]
    assert historical.status == "replay_only"
    assert historical_registry.get_run_kind(historical.run_kind) == definitions[-1]
    assert (
        historical_registry.get_agent_execution_graph(
            historical.run_kind,
            historical.agent_graph_version,
        )
        == historical
    )


def test_readiness_retains_historical_selector_graph_without_active_profile_metadata() -> None:
    registry = build_builtin_registry()
    definitions = tuple(registry.list_run_kinds())
    playtest_definition = next(
        definition for definition in definitions if definition.kind == "playtest.run"
    )
    historical_definition = playtest_definition.model_copy(
        update={"version": 2, "status": "disabled"}
    )
    graphs = tuple(registry.list_agent_execution_graphs())
    memory_graph = next(
        graph for graph in graphs if graph.agent_graph_version == "playtest-memory-graph@1"
    )
    historical_graph = _replace_graph(
        memory_graph,
        agent_graph_version="playtest-memory-graph@historical-2",
        run_kind=RunKindRef(kind="playtest.run", version=2),
        status="replay_only",
    )
    historical_registry = _registry_with_run_kinds_and_graphs(
        registry,
        definitions=(*definitions, historical_definition),
        graphs=(*graphs, historical_graph),
    )

    assert historical_registry.get_profile_requirements(historical_graph.run_kind) is None
    assert (
        PlatformReadinessValidator(
            registry=historical_registry,
            components=_components(registry),
        )
        .validate()
        .ready
        is True
    )


def test_readiness_rejects_historical_graph_without_exact_run_kind_definition() -> None:
    registry = build_builtin_registry()
    _, graphs = _with_historical_generation_graph(registry)

    with pytest.raises(IntegrityViolation, match="exact retained Run kind definition"):
        PlatformReadinessValidator(
            registry=_GraphsOverrideRegistry(registry, graphs),
            components=_components(registry),
        ).validate()


def test_readiness_rejects_active_graph_for_disabled_run_kind_version() -> None:
    registry = build_builtin_registry()
    definitions, graphs = _with_historical_generation_graph(registry)
    active_historical = _replace_graph(graphs[-1], status="active")
    historical_registry = _RunKindsOverrideRegistry(
        _GraphsOverrideRegistry(registry, (*graphs[:-1], active_historical)),
        definitions,
    )

    with pytest.raises(IntegrityViolation, match="requires an active LLM-capable Run kind"):
        PlatformReadinessValidator(
            registry=historical_registry,
            components=_components(registry),
        ).validate()


def test_readiness_rejects_replay_only_graph_for_non_replay_run_kind() -> None:
    registry = build_builtin_registry()
    definitions, graphs = _with_historical_generation_graph(registry)
    historical_definition = definitions[-1].model_copy(
        update={"allowed_llm_execution_modes": ("live",)}
    )
    historical_registry = _RunKindsOverrideRegistry(
        _GraphsOverrideRegistry(registry, graphs),
        (*definitions[:-1], historical_definition),
    )

    with pytest.raises(IntegrityViolation, match="requires a replay-capable Run kind"):
        PlatformReadinessValidator(
            registry=historical_registry,
            components=_components(registry),
        ).validate()


def test_optional_parent_projection_is_single_valued_and_multi_value_requires_equality() -> None:
    registry = build_builtin_registry()
    definition = next(item for item in registry.list_run_kinds() if item.kind == "checker.run")
    policy = next(
        item for item in definition.outcome_policies if item.policy_id == "checker-completed"
    )
    artifact_rule = next(item for item in policy.artifact_rules if item.rule_id == "primary")
    lineage = registry.get_lineage_policy(artifact_rule.lineage_policy_ref)
    assert lineage is not None
    constraint_parent = next(
        item for item in lineage.parent_rules if item.parent_role == "constraint"
    )
    constraint_projection = next(
        item for item in lineage.version_projection if item.field == "constraint_snapshot_id"
    )
    assert (constraint_parent.min_count, constraint_parent.max_count) == (0, 1)
    assert constraint_projection.source == "parent_role"
    assert constraint_projection.parent_role == "constraint"

    changed_parents = tuple(
        item.model_copy(update={"max_count": None}) if item.parent_role == "constraint" else item
        for item in lineage.parent_rules
    )

    with pytest.raises(IntegrityViolation, match="multi-valued lineage projection"):
        PlatformReadinessValidator._validate_lineage_policy(
            artifact_rule=artifact_rule,
            lineage_policy=lineage.model_copy(update={"parent_rules": changed_parents}),
            policy_rule_ids={item.rule_id for item in policy.artifact_rules},
        )


@pytest.mark.parametrize(
    ("rule_id", "expected_equality_roles"),
    [
        ("primary", {"config", "scenarios"}),
        ("scenario", {"config"}),
    ],
)
def test_task_suite_lineage_closes_document_version_across_exact_content_parents(
    rule_id: str,
    expected_equality_roles: set[str],
) -> None:
    registry = build_builtin_registry()
    definition = next(
        item for item in registry.list_run_kinds() if item.kind == "task_suite.derive"
    )
    policy = next(
        item for item in definition.outcome_policies if item.policy_id == "task-suite-derived"
    )
    artifact_rule = next(item for item in policy.artifact_rules if item.rule_id == rule_id)
    lineage = registry.get_lineage_policy(artifact_rule.lineage_policy_ref)
    assert lineage is not None
    doc_projection = next(
        item for item in lineage.version_projection if item.field == "doc_version"
    )
    assert doc_projection.source == "parent_role"
    assert doc_projection.parent_role == "preview"
    assert set(doc_projection.equality_parent_roles) == expected_equality_roles

    with pytest.raises(IntegrityViolation, match="TaskSuite lineage doc version"):
        PlatformReadinessValidator._validate_lineage_policy(
            artifact_rule=artifact_rule,
            lineage_policy=lineage.model_copy(
                update={
                    "version_projection": tuple(
                        item.model_copy(
                            update={
                                "equality_parent_roles": tuple(
                                    role for role in item.equality_parent_roles if role != "config"
                                )
                            }
                        )
                        if item.field == "doc_version"
                        else item
                        for item in lineage.version_projection
                    )
                }
            ),
            policy_rule_ids={item.rule_id for item in policy.artifact_rules},
        )


def test_readiness_rejects_run_kind_outcome_drift_from_frozen_digest() -> None:
    registry = build_builtin_registry()
    definitions = list(registry.list_run_kinds())
    index = next(
        index
        for index, definition in enumerate(definitions)
        if definition.kind == "generation.propose"
    )
    definition = definitions[index]
    definitions[index] = definition.model_copy(
        update={"outcome_policies": definition.outcome_policies[1:]}
    )

    with pytest.raises(IntegrityViolation, match="definition digest"):
        PlatformReadinessValidator(
            registry=_RunKindsOverrideRegistry(registry, tuple(definitions)),
            components=_components(registry),
        ).validate()


def test_readiness_rejects_unclosed_config_export_environment_ref() -> None:
    registry = build_builtin_registry()
    catalog = registry.list_execution_profile_catalogs()[0]
    config_export = next(
        profile for profile in catalog.definitions if profile.profile_kind == "config_export"
    )
    details = config_export.details.model_copy(
        update={"target_environment_profile": ProfileRefV1(profile_id="missing", version=1)}
    )
    changed = _replace_catalog_profile(
        catalog,
        profile_kind="config_export",
        details=details,
    )

    with pytest.raises(IntegrityViolation, match="environment contract"):
        PlatformReadinessValidator(
            registry=_CatalogOverrideRegistry(registry, changed),
            components=_components(registry),
        ).validate()


def test_readiness_rejects_migration_profile_edge_missing_from_matrix() -> None:
    registry = build_builtin_registry()
    catalog = registry.list_execution_profile_catalogs()[0]
    details = MigrationProfileDetailsV1(
        edges=(
            MigrationEdgeV1(
                edge_id="unretained-edge",
                source_kind="ir_snapshot",
                source_payload_schema_id="ir-core@1",
                target_payload_schema_id="ir-core@2",
                target_meta_schema_version="meta@2",
                golden_replay_policy="required",
                golden_fixture_set_digest="a" * 64,
            ),
        )
    )
    changed = _replace_catalog_profile(
        catalog,
        profile_kind="artifact_migrator",
        details=details,
    )

    with pytest.raises(IntegrityViolation, match="matrix capability"):
        PlatformReadinessValidator(
            registry=_CatalogOverrideRegistry(registry, changed),
            components=_components(registry),
        ).validate()


@pytest.mark.parametrize(
    "field_name",
    [
        "executors",
        "terminal_hooks",
        "workflow_effects",
        "completion_oracles",
        "playtest_payload_validators",
        "profile_handlers",
        "permission_domain_resolvers",
    ],
)
def test_readiness_rejects_every_missing_trusted_mapping(field_name: str) -> None:
    registry = build_builtin_registry()
    components = _components(registry)
    retained = dict(getattr(components, field_name))
    assert retained, f"builtin {field_name} registry must not be empty"
    retained.pop(sorted(retained)[0])

    with pytest.raises(IntegrityViolation):
        PlatformReadinessValidator(
            registry=registry,
            components=replace(components, **{field_name: retained}),
        ).validate()


@pytest.mark.parametrize(
    "field_name",
    [
        "executors",
        "terminal_hooks",
        "workflow_effects",
        "completion_oracles",
        "playtest_payload_validators",
        "profile_handlers",
        "permission_domain_resolvers",
    ],
)
def test_readiness_rejects_every_extra_trusted_mapping(field_name: str) -> None:
    registry = build_builtin_registry()
    components = _components(registry)
    expanded = dict(getattr(components, field_name))
    expanded["unexpected_component@1"] = _trusted_component

    with pytest.raises(IntegrityViolation):
        PlatformReadinessValidator(
            registry=registry,
            components=replace(components, **{field_name: expanded}),
        ).validate()


class _MissingExactReferenceRegistry:
    def __init__(self, delegate: Any, missing_getter: str) -> None:
        self._delegate = delegate
        self._missing_getter = missing_getter

    def __getattr__(self, name: str) -> Any:
        if name == self._missing_getter:
            return lambda *args, **kwargs: None
        return getattr(self._delegate, name)


class _RunKindsOverrideRegistry:
    def __init__(self, delegate: Any, definitions: tuple[Any, ...]) -> None:
        self._delegate = delegate
        self._definitions = definitions

    def list_run_kinds(self) -> tuple[Any, ...]:
        return self._definitions

    def get_run_kind(self, kind: RunKindRef) -> Any | None:
        return next(
            (
                definition
                for definition in self._definitions
                if (definition.kind, definition.version) == (kind.kind, kind.version)
            ),
            None,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _RuntimeRulesOverrideRegistry:
    def __init__(self, delegate: Any, rule_set: Any) -> None:
        self._delegate = delegate
        self._rule_set = rule_set

    def get_runtime_parent_rule_set(self, ref: Any) -> Any:
        del ref
        return self._rule_set

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _GraphsOverrideRegistry:
    def __init__(self, delegate: Any, graphs: tuple[AgentExecutionGraphV1, ...]) -> None:
        self._delegate = delegate
        self._graphs = graphs

    def list_agent_execution_graphs(self) -> tuple[AgentExecutionGraphV1, ...]:
        return self._graphs

    def get_agent_execution_graph(
        self,
        run_kind: RunKindRef,
        agent_graph_version: str,
    ) -> AgentExecutionGraphV1 | None:
        return next(
            (
                graph
                for graph in self._graphs
                if graph.run_kind == run_kind and graph.agent_graph_version == agent_graph_version
            ),
            None,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _CatalogOverrideRegistry:
    def __init__(self, delegate: Any, catalog: ExecutionProfileCatalogSnapshotV1) -> None:
        self._delegate = delegate
        self._catalog = catalog

    def list_execution_profile_catalogs(self) -> tuple[ExecutionProfileCatalogSnapshotV1, ...]:
        return (self._catalog,)

    def get_execution_profile_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ExecutionProfileCatalogSnapshotV1 | None:
        if (
            catalog_version == self._catalog.catalog_version
            and catalog_digest == self._catalog.catalog_digest
        ):
            return self._catalog
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def _replace_graph(
    graph: AgentExecutionGraphV1,
    **updates: Any,
) -> AgentExecutionGraphV1:
    body = graph.model_dump(mode="python", exclude={"graph_digest"})
    body.update(updates)
    return AgentExecutionGraphV1(
        **body,
        graph_digest=agent_execution_graph_digest(body),
    )


def _with_historical_generation_graph(
    registry: Any,
) -> tuple[tuple[Any, ...], tuple[AgentExecutionGraphV1, ...]]:
    definitions = tuple(registry.list_run_kinds())
    generation_definition = next(
        definition for definition in definitions if definition.kind == "generation.propose"
    )
    historical_definition = generation_definition.model_copy(
        update={"version": 2, "status": "disabled"}
    )
    graphs = tuple(registry.list_agent_execution_graphs())
    generation_graph = next(
        graph
        for graph in graphs
        if graph.run_kind == RunKindRef(kind="generation.propose", version=1)
    )
    historical_graph = _replace_graph(
        generation_graph,
        agent_graph_version="generation-graph@historical-2",
        run_kind=RunKindRef(kind="generation.propose", version=2),
        status="replay_only",
    )
    return (*definitions, historical_definition), (*graphs, historical_graph)


def _registry_with_run_kinds_and_graphs(
    source: ImmutablePlatformRegistry,
    *,
    definitions: tuple[Any, ...],
    graphs: tuple[AgentExecutionGraphV1, ...],
) -> ImmutablePlatformRegistry:
    return ImmutablePlatformRegistry(
        run_kinds=definitions,
        retry_policies=source._retry_policies.values(),  # noqa: SLF001
        failure_classifiers=source._failure_classifiers.values(),  # noqa: SLF001
        lineage_policies=source._lineage_policies.values(),  # noqa: SLF001
        version_transition_policies=source._version_transition_policies.values(),  # noqa: SLF001
        runtime_parent_rule_sets=source._runtime_parent_rule_sets.values(),  # noqa: SLF001
        finding_output_policies=source._finding_output_policies.values(),  # noqa: SLF001
        run_event_registries=source._run_event_registries.values(),  # noqa: SLF001
        completion_oracle_registries=source._completion_oracle_registries.values(),  # noqa: SLF001
        playtest_payload_schema_registries=(
            source._playtest_payload_schema_registries.values()  # noqa: SLF001
        ),
        agent_execution_graphs=graphs,
        execution_profile_catalogs=source._execution_profile_catalogs.values(),  # noqa: SLF001
        migration_capability_matrices=source._migration_capability_matrices.values(),  # noqa: SLF001
        profile_requirements=source._profile_requirements,  # noqa: SLF001
        permission_resolver_keys=source._permission_resolver_keys,  # noqa: SLF001
    )


def _replace_catalog_profile(
    catalog: ExecutionProfileCatalogSnapshotV1,
    *,
    profile_kind: str,
    details: Any,
) -> ExecutionProfileCatalogSnapshotV1:
    definitions = tuple(
        profile.model_copy(update={"details": details})
        if profile.profile_kind == profile_kind
        else profile
        for profile in catalog.definitions
    )
    payload = {
        "catalog_version": catalog.catalog_version,
        "definitions": definitions,
        "lifecycle": catalog.lifecycle,
    }
    return ExecutionProfileCatalogSnapshotV1(
        **payload,
        catalog_digest=execution_profile_catalog_digest(payload),
    )


@pytest.mark.parametrize(
    "missing_getter",
    [
        "get_runtime_parent_rule_set",
        "get_retry_policy",
        "get_failure_classifier",
        "get_version_transition_policy",
        "get_lineage_policy",
        "get_finding_output_policy",
        "get_migration_capability_matrix",
        "get_execution_profile_catalog",
        "get_completion_oracle_registry",
        "get_run_event_registry",
    ],
)
def test_readiness_rejects_each_broken_exact_reference_closure(
    missing_getter: str,
) -> None:
    registry = build_builtin_registry()
    components = _components(registry)

    with pytest.raises(IntegrityViolation):
        PlatformReadinessValidator(
            registry=_MissingExactReferenceRegistry(registry, missing_getter),
            components=components,
        ).validate()


def test_permission_template_cannot_be_ready_without_its_trusted_resolver() -> None:
    registry = build_builtin_registry()
    components = _components(registry)
    resolvers = dict(components.permission_domain_resolvers)
    resolver_key = registry.get_permission_resolver_key(
        RunKindRef(kind="generation.propose", version=1)
    )
    assert resolver_key is not None
    resolvers.pop(resolver_key)

    with pytest.raises(IntegrityViolation):
        PlatformReadinessValidator(
            registry=registry,
            components=replace(components, permission_domain_resolvers=resolvers),
        ).validate()


def test_m4e_deferred_kinds_are_explicit_internal_seams_not_missing_handlers() -> None:
    registry = build_builtin_registry()
    components = _components(registry)
    report = PlatformReadinessValidator(
        registry=registry,
        components=components,
    ).validate()

    for kind, executor_key, success_hook in (
        ("artifact.migrate", "artifact_migrator@1", "publish_migration@1"),
        ("dr.drill", "dr_drill_runner@1", "publish_operational_evidence@1"),
    ):
        definition = registry.get_run_kind(RunKindRef(kind=kind, version=1))
        assert definition is not None
        assert definition.creation_mode == "internal_only"
        assert definition.allowed_llm_execution_modes == ("not_applicable",)
        assert definition.seed_policy == "forbidden"
        assert definition.executor_key == executor_key
        assert executor_key in components.executors
        assert definition.terminal_hooks.on_success == success_hook
        assert success_hook in components.terminal_hooks
        # M4c keeps the complete M4e success contract while the explicit executor
        # seam remains replaceable by the real Task 14/M4e implementation.
        assert any(policy.prepared_outcome == "success" for policy in definition.outcome_policies)

    assert report.ready is True
    assert report.deferred_executor_keys == _DEFERRED_EXECUTOR_KEYS
    assert set(_DEFERRED_EXECUTOR_KEYS) <= set(components.executors)
