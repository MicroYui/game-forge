from __future__ import annotations

import pytest

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.registry.constraint_compilers import (
    BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1,
    resolve_constraint_validation_compiler_authority,
)


def _catalog():
    return build_builtin_registry().list_execution_profile_catalogs()[-1]


def _profile(profile_id: str):
    catalog = _catalog()
    definition = next(item for item in catalog.definitions if item.profile.profile_id == profile_id)
    lifecycle = next(item for item in catalog.lifecycle if item.profile == definition.profile)
    return definition, lifecycle


def test_builtin_compiler_authority_is_complete_and_profile_bound() -> None:
    definition, lifecycle = _profile("builtin.constraint_compiler")

    authority = resolve_constraint_validation_compiler_authority(definition, lifecycle)

    assert authority.compiler_profile == definition.profile
    assert authority.run_kind.kind == "constraint_proposal.validate"
    assert authority.run_kind.version == 1
    assert authority.differential_engines == BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1
    assert authority.profile_payload_hash


def test_compiler_authority_rejects_wrong_kind_inactive_or_unsupported_profiles() -> None:
    validation_definition, validation_lifecycle = _profile("builtin.validation")
    with pytest.raises(Conflict, match="constraint_compiler"):
        resolve_constraint_validation_compiler_authority(
            validation_definition,
            validation_lifecycle,
        )

    compiler_definition, compiler_lifecycle = _profile("builtin.constraint_compiler")
    with pytest.raises(Conflict, match="active"):
        resolve_constraint_validation_compiler_authority(
            compiler_definition,
            compiler_lifecycle.model_copy(update={"state": "disabled"}),
        )

    with pytest.raises(Conflict, match="supported builtin"):
        resolve_constraint_validation_compiler_authority(
            compiler_definition.model_copy(
                update={"handler_key": "builtin_constraint_compiler_profile@999"}
            ),
            compiler_lifecycle,
        )


def test_compiler_authority_treats_definition_lifecycle_mismatch_as_integrity_failure() -> None:
    compiler_definition, _compiler_lifecycle = _profile("builtin.constraint_compiler")
    _validation_definition, validation_lifecycle = _profile("builtin.validation")

    with pytest.raises(IntegrityViolation, match="lifecycle"):
        resolve_constraint_validation_compiler_authority(
            compiler_definition,
            validation_lifecycle,
        )


@pytest.mark.parametrize(
    "update",
    [
        {"config_schema_id": "constraint_compiler-profile-config@999"},
        {"compatible_run_kinds": (RunKindRef(kind="checker.run", version=1),)},
        {"input_schema_ids": ("checker-run@1",)},
        {"output_schema_ids": ("checker-report@1",)},
        {"stochastic": True},
        {"required_capabilities": ("reasoning",)},
    ],
    ids=(
        "config-schema",
        "compatible-run-kinds",
        "input-schemas",
        "output-schemas",
        "stochastic",
        "required-capabilities",
    ),
)
def test_compiler_authority_rejects_builtin_worker_adapter_contract_drift(
    update: dict[str, object],
) -> None:
    definition, lifecycle = _profile("builtin.constraint_compiler")

    with pytest.raises(Conflict, match="adapter contract"):
        resolve_constraint_validation_compiler_authority(
            definition.model_copy(update=update),
            lifecycle,
        )
