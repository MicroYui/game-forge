"""Exact task-suite derivation authority exposed to admission, worker, and reads."""

from __future__ import annotations

from dataclasses import dataclass
from hmac import compare_digest

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    ExecutionProfileLifecycleV1,
    ProfileRefV1,
    RunKindRef,
    TaskSuiteDerivationProfileConfigV2,
    canonical_config_hash,
    execution_profile_payload_hash,
)
from gameforge.contracts.playtest import CompletionOracleRegistryRefV1


TASK_SUITE_DERIVATION_RUN_KIND_V1 = RunKindRef(
    kind="task_suite.derive",
    version=1,
)


@dataclass(frozen=True, slots=True)
class BuiltinTaskSuiteDerivationAdapterContractV2:
    """Exact profile shape implemented by the shipped deterministic deriver."""

    handler_key: str
    config_schema_id: str
    compatible_run_kinds: tuple[RunKindRef, ...]
    input_schema_ids: tuple[str, ...]
    output_schema_ids: tuple[str, ...]
    stochastic: bool
    required_capabilities: tuple[str, ...]

    def matches(self, definition: ExecutionProfileDefinitionV1) -> bool:
        return (
            type(definition) is ExecutionProfileDefinitionV1
            and definition.profile_kind == "task_suite_derivation"
            and definition.handler_key == self.handler_key
            and definition.config_schema_id == self.config_schema_id
            and definition.compatible_run_kinds == self.compatible_run_kinds
            and definition.input_schema_ids == self.input_schema_ids
            and definition.output_schema_ids == self.output_schema_ids
            and definition.stochastic is self.stochastic
            and definition.required_capabilities == self.required_capabilities
        )


BUILTIN_TASK_SUITE_DERIVATION_ADAPTER_CONTRACT_V2 = BuiltinTaskSuiteDerivationAdapterContractV2(
    handler_key="builtin_task_suite_derivation_profile@2",
    config_schema_id="task_suite_derivation-profile-config@2",
    compatible_run_kinds=(TASK_SUITE_DERIVATION_RUN_KIND_V1,),
    input_schema_ids=("task-suite-derive@1",),
    output_schema_ids=("scenario-spec@1", "task-suite@1"),
    stochastic=False,
    required_capabilities=(),
)


def builtin_task_suite_derivation_adapter_matches(
    definition: ExecutionProfileDefinitionV1,
) -> bool:
    return BUILTIN_TASK_SUITE_DERIVATION_ADAPTER_CONTRACT_V2.matches(definition)


@dataclass(frozen=True, slots=True)
class TaskSuiteDerivationAuthority:
    derivation_profile: ProfileRefV1
    profile_payload_hash: str
    run_kind: RunKindRef
    target_environment_profile: ProfileRefV1
    completion_oracle_registry_ref: CompletionOracleRegistryRefV1
    max_scenarios: int
    max_total_prepared_artifact_bytes: int


def resolve_task_suite_derivation_authority(
    definition: ExecutionProfileDefinitionV1,
    lifecycle: ExecutionProfileLifecycleV1,
) -> TaskSuiteDerivationAuthority:
    """Resolve the complete authority accepted for a new deterministic derive Run."""

    if (
        type(definition) is not ExecutionProfileDefinitionV1
        or type(lifecycle) is not ExecutionProfileLifecycleV1
    ):
        raise IntegrityViolation("task-suite derivation authority received invalid profile data")
    if lifecycle.profile != definition.profile:
        raise IntegrityViolation("task-suite derivation lifecycle differs from its definition")
    if definition.profile_kind != "task_suite_derivation":
        raise Conflict(
            "execution profile is not a task_suite_derivation profile",
            profile_id=definition.profile.profile_id,
            profile_version=definition.profile.version,
        )
    # task_suite.derive@1 permits only not_applicable execution mode. Per the
    # frozen lifecycle contract, that mode may use active profiles only;
    # replay_only is not an executable fallback for this deterministic RunKind.
    if lifecycle.state != "active":
        raise Conflict(
            "task-suite derivation profile is not active",
            profile_id=definition.profile.profile_id,
            profile_version=definition.profile.version,
            profile_status=lifecycle.state,
        )
    if not builtin_task_suite_derivation_adapter_matches(definition):
        raise Conflict(
            "task-suite derivation profile does not match the supported builtin adapter contract",
            profile_id=definition.profile.profile_id,
            profile_version=definition.profile.version,
        )
    if not compare_digest(definition.config_hash, canonical_config_hash(definition.config)):
        raise IntegrityViolation("task-suite derivation profile config hash is invalid")
    try:
        config = TaskSuiteDerivationProfileConfigV2.model_validate(definition.config)
    except (TypeError, ValueError) as exc:
        raise IntegrityViolation("task-suite derivation profile config is invalid") from exc
    return TaskSuiteDerivationAuthority(
        derivation_profile=definition.profile,
        profile_payload_hash=execution_profile_payload_hash(definition),
        run_kind=TASK_SUITE_DERIVATION_RUN_KIND_V1,
        target_environment_profile=config.target_environment_profile,
        completion_oracle_registry_ref=CompletionOracleRegistryRefV1(
            registry_version=config.completion_oracle_registry_version,
            digest=config.completion_oracle_registry_digest,
        ),
        max_scenarios=config.max_scenarios,
        max_total_prepared_artifact_bytes=config.max_total_prepared_artifact_bytes,
    )


__all__ = [
    "BUILTIN_TASK_SUITE_DERIVATION_ADAPTER_CONTRACT_V2",
    "TASK_SUITE_DERIVATION_RUN_KIND_V1",
    "BuiltinTaskSuiteDerivationAdapterContractV2",
    "TaskSuiteDerivationAuthority",
    "builtin_task_suite_derivation_adapter_matches",
    "resolve_task_suite_derivation_authority",
]
