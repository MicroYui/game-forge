"""Process-shipped constraint-compiler authority keyed by versioned handler identity."""

from __future__ import annotations

from dataclasses import dataclass

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    ExecutionProfileLifecycleV1,
    ProfileRefV1,
    RunKindRef,
    execution_profile_payload_hash,
)
from gameforge.contracts.jobs import SolverEngineRefV1


CONSTRAINT_VALIDATION_RUN_KIND_V1 = RunKindRef(
    kind="constraint_proposal.validate",
    version=1,
)

BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1 = (
    SolverEngineRefV1(engine_id="clingo", version=1),
    SolverEngineRefV1(engine_id="graph-reference", version=1),
    SolverEngineRefV1(engine_id="numeric-reference", version=1),
    SolverEngineRefV1(engine_id="z3", version=1),
)


@dataclass(frozen=True, slots=True)
class BuiltinConstraintCompilerAdapterContractV1:
    """Exact retained profile shape implemented by the built-in worker adapter."""

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
            and definition.profile_kind == "constraint_compiler"
            and definition.handler_key == self.handler_key
            and definition.config_schema_id == self.config_schema_id
            and definition.compatible_run_kinds == self.compatible_run_kinds
            and definition.input_schema_ids == self.input_schema_ids
            and definition.output_schema_ids == self.output_schema_ids
            and definition.stochastic is self.stochastic
            and definition.required_capabilities == self.required_capabilities
        )


BUILTIN_CONSTRAINT_COMPILER_ADAPTER_CONTRACT_V1 = BuiltinConstraintCompilerAdapterContractV1(
    handler_key="builtin_constraint_compiler_profile@1",
    config_schema_id="constraint_compiler-profile-config@1",
    compatible_run_kinds=(CONSTRAINT_VALIDATION_RUN_KIND_V1,),
    input_schema_ids=("constraint-validation@1",),
    output_schema_ids=(
        "constraint-compile-evidence@1",
        "constraint-snapshot@1",
    ),
    stochastic=False,
    required_capabilities=(),
)


def builtin_constraint_compiler_adapter_matches(
    definition: ExecutionProfileDefinitionV1,
) -> bool:
    """Return whether one exact profile can run on the shipped compiler adapter."""

    return BUILTIN_CONSTRAINT_COMPILER_ADAPTER_CONTRACT_V1.matches(definition)


@dataclass(frozen=True, slots=True)
class ConstraintValidationCompilerAuthority:
    compiler_profile: ProfileRefV1
    profile_payload_hash: str
    run_kind: RunKindRef
    differential_engines: tuple[SolverEngineRefV1, ...]


def resolve_constraint_validation_compiler_authority(
    definition: ExecutionProfileDefinitionV1,
    lifecycle: ExecutionProfileLifecycleV1,
) -> ConstraintValidationCompilerAuthority:
    """Resolve the one complete engine tuple accepted for an exact active compiler."""

    if (
        type(definition) is not ExecutionProfileDefinitionV1
        or type(lifecycle) is not ExecutionProfileLifecycleV1
    ):
        raise IntegrityViolation("constraint compiler authority received invalid profile data")
    if lifecycle.profile != definition.profile:
        raise IntegrityViolation("constraint compiler lifecycle differs from its definition")
    if definition.profile_kind != "constraint_compiler":
        raise Conflict(
            "execution profile is not a constraint_compiler",
            profile_id=definition.profile.profile_id,
            profile_version=definition.profile.version,
        )
    if lifecycle.state != "active":
        raise Conflict(
            "constraint compiler profile is not active",
            profile_id=definition.profile.profile_id,
            profile_version=definition.profile.version,
            profile_status=lifecycle.state,
        )
    if not builtin_constraint_compiler_adapter_matches(definition):
        raise Conflict(
            "constraint compiler profile does not match the supported builtin adapter contract",
            profile_id=definition.profile.profile_id,
            profile_version=definition.profile.version,
        )
    return ConstraintValidationCompilerAuthority(
        compiler_profile=definition.profile,
        profile_payload_hash=execution_profile_payload_hash(definition),
        run_kind=CONSTRAINT_VALIDATION_RUN_KIND_V1,
        differential_engines=BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1,
    )


__all__ = [
    "BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1",
    "BUILTIN_CONSTRAINT_COMPILER_ADAPTER_CONTRACT_V1",
    "CONSTRAINT_VALIDATION_RUN_KIND_V1",
    "BuiltinConstraintCompilerAdapterContractV1",
    "ConstraintValidationCompilerAuthority",
    "builtin_constraint_compiler_adapter_matches",
    "resolve_constraint_validation_compiler_authority",
]
