from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.execution_profiles import (
    ExecutionProfileCatalogSnapshotV1,
    MigrationEdgeV1,
    MigrationProfileDetailsV1,
    ProfileRefV1,
    execution_profile_catalog_digest,
)
from gameforge.contracts.jobs import PreparedRunFailure
from gameforge.platform.registry import (
    PlatformReadinessValidator,
    TrustedComponentMaps,
    build_builtin_registry,
)
from gameforge.platform.run_handlers.deferred import (
    DEFERRED_EXECUTORS,
    DeferredExecutionRequest,
)


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

        prepared = DEFERRED_EXECUTORS[executor_key](
            DeferredExecutionRequest(
                run_id=f"run:{kind}",
                attempt_no=1,
                run_kind=RunKindRef(kind=kind, version=1),
                classifier=definition.failure_classifier,
            )
        )
        assert isinstance(prepared, PreparedRunFailure)
        assert prepared.prepared_schema_version == "prepared-run-failure@1"
        assert prepared.run_kind == RunKindRef(kind=kind, version=1)
        assert prepared.artifacts == ()
        assert prepared.requirement_dispositions == ()
        assert prepared.cause_code == "execution_failed"
        assert prepared.failure_class == "execution"
        assert prepared.intrinsic_retry_eligible is False

    assert report.ready is True
    assert report.deferred_executor_keys == _DEFERRED_EXECUTOR_KEYS
    assert set(_DEFERRED_EXECUTOR_KEYS) <= set(components.executors)
