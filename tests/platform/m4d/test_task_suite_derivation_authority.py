from __future__ import annotations

import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.registry.task_suite_derivation import (
    TASK_SUITE_DERIVATION_RUN_KIND_V1,
    resolve_task_suite_derivation_authority,
)


def _catalog():
    return build_builtin_registry().list_execution_profile_catalogs()[-1]


def _profile(profile_id: str, version: int = 1):
    catalog = _catalog()
    definition = next(
        item
        for item in catalog.definitions
        if item.profile.profile_id == profile_id and item.profile.version == version
    )
    lifecycle = next(item for item in catalog.lifecycle if item.profile == definition.profile)
    return definition, lifecycle


def test_builtin_task_suite_derivation_authority_is_complete_and_profile_bound() -> None:
    definition, lifecycle = _profile("builtin.task_suite_derivation", 2)

    authority = resolve_task_suite_derivation_authority(definition, lifecycle)

    assert authority.derivation_profile == definition.profile
    assert authority.profile_payload_hash
    assert authority.run_kind == TASK_SUITE_DERIVATION_RUN_KIND_V1
    assert authority.target_environment_profile.profile_id == "builtin.environment"
    assert authority.completion_oracle_registry_ref.registry_version == 1
    assert authority.max_scenarios == 1024
    assert authority.max_total_prepared_artifact_bytes == 256 * 1024 * 1024


def test_task_suite_derivation_authority_projects_alternate_valid_profile_limits() -> None:
    definition, lifecycle = _profile("builtin.task_suite_derivation", 2)
    config = {
        **definition.config,
        "max_scenarios": 17,
        "max_total_prepared_artifact_bytes": 8 * 1024 * 1024,
    }
    definition = definition.model_copy(
        update={
            "config": config,
            "config_hash": canonical_sha256(config),
        }
    )

    authority = resolve_task_suite_derivation_authority(definition, lifecycle)

    assert authority.max_scenarios == 17
    assert authority.max_total_prepared_artifact_bytes == 8 * 1024 * 1024


@pytest.mark.parametrize("state", ["replay_only", "disabled"])
def test_task_suite_derivation_authority_rejects_non_active_lifecycle(state: str) -> None:
    definition, lifecycle = _profile("builtin.task_suite_derivation", 2)

    with pytest.raises(Conflict, match="active"):
        resolve_task_suite_derivation_authority(
            definition,
            lifecycle.model_copy(update={"state": state}),
        )


def test_task_suite_derivation_authority_rejects_wrong_kind_or_lifecycle_mismatch() -> None:
    environment_definition, environment_lifecycle = _profile("builtin.environment")
    with pytest.raises(Conflict, match="task_suite_derivation"):
        resolve_task_suite_derivation_authority(
            environment_definition,
            environment_lifecycle,
        )

    definition, _lifecycle = _profile("builtin.task_suite_derivation", 2)
    with pytest.raises(IntegrityViolation, match="lifecycle"):
        resolve_task_suite_derivation_authority(definition, environment_lifecycle)


@pytest.mark.parametrize(
    "update",
    [
        {"handler_key": "builtin_task_suite_derivation_profile@999"},
        {"config_schema_id": "task_suite_derivation-profile-config@1"},
        {"compatible_run_kinds": (RunKindRef(kind="playtest.run", version=1),)},
        {"input_schema_ids": ("playtest-run@1",)},
        {"output_schema_ids": ("playtest-trace@1",)},
        {"stochastic": True},
        {"required_capabilities": ("spatial_2d",)},
    ],
    ids=(
        "handler",
        "config-schema",
        "compatible-run-kinds",
        "input-schemas",
        "output-schemas",
        "stochastic",
        "required-capabilities",
    ),
)
def test_task_suite_derivation_authority_rejects_worker_adapter_contract_drift(
    update: dict[str, object],
) -> None:
    definition, lifecycle = _profile("builtin.task_suite_derivation", 2)

    with pytest.raises(Conflict, match="adapter contract"):
        resolve_task_suite_derivation_authority(
            definition.model_copy(update=update),
            lifecycle,
        )


def test_task_suite_derivation_authority_rejects_corrupt_config_hash() -> None:
    definition, lifecycle = _profile("builtin.task_suite_derivation", 2)

    with pytest.raises(IntegrityViolation, match="config hash"):
        resolve_task_suite_derivation_authority(
            definition.model_copy(update={"config_hash": "f" * 64}),
            lifecycle,
        )
