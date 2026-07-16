"""Versioned, game-neutral dispatch contract for injected regression suites.

``regression-suite@1`` deliberately remains adapter-owned in the platform payload
schema registry.  This module freezes only the authority envelope needed to select
one trusted adapter and prove its inputs; the opaque ``adapter_payload`` is parsed by
that exact adapter.  The first adapter shipped by the worker replays bounded atomic
Agent-Env actions and evaluates a deterministic completion oracle.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.findings import Severity
from gameforge.contracts.playtest import (
    CompletionOracleRefV1,
    CompletionOracleRegistryRefV1,
)


MAX_REGRESSION_CASES = 256
MAX_REGRESSION_STEPS_PER_CASE = 4_096
MAX_REGRESSION_TOTAL_STEPS = 65_536
MAX_REGRESSION_JSON_BYTES = 16 * 1024 * 1024
MAX_REGRESSION_ID_LENGTH = 512
MAX_REGRESSION_RESULT_LENGTH = 4_096
REGRESSION_CASE_SEED_DERIVATION_VERSION = "subseed@1"

BoundedId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_REGRESSION_ID_LENGTH),
]
BoundedResult = Annotated[str, StringConstraints(max_length=MAX_REGRESSION_RESULT_LENGTH)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _validate_json_shape(value: JsonValue, *, label: str, max_bytes: int) -> JsonValue:
    if len(canonical_json(value).encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds its byte limit")
    stack: list[tuple[JsonValue, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > 32:
            raise ValueError(f"{label} exceeds its depth limit")
        if isinstance(item, str):
            if len(item) > MAX_REGRESSION_RESULT_LENGTH:
                raise ValueError(f"{label} contains an oversized string")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{label} contains a non-finite float")
        elif isinstance(item, dict):
            if len(item) > 1_024:
                raise ValueError(f"{label} contains an oversized object")
            for key, child in item.items():
                if len(key) > MAX_REGRESSION_RESULT_LENGTH:
                    raise ValueError(f"{label} contains an oversized object key")
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            if len(item) > MAX_REGRESSION_TOTAL_STEPS:
                raise ValueError(f"{label} contains an oversized array")
            stack.extend((child, depth + 1) for child in item)
    return value


class RegressionSuiteAdapterRefV1(_FrozenModel):
    adapter_id: BoundedId
    version: int = Field(ge=1)


class RegressionSuiteDispatchV1(_FrozenModel):
    """Authority envelope understood before adapter-specific suite parsing."""

    regression_suite_schema_version: Literal["regression-suite@1"] = "regression-suite@1"
    adapter: RegressionSuiteAdapterRefV1
    environment_profile: ResolvedExecutionProfileBindingV1
    env_contract_version: BoundedId
    adapter_payload: JsonValue

    @field_validator("adapter_payload")
    @classmethod
    def _bounded_adapter_payload(cls, value: JsonValue) -> JsonValue:
        return _validate_json_shape(
            value,
            label="regression suite adapter payload",
            max_bytes=MAX_REGRESSION_JSON_BYTES,
        )

    @model_validator(mode="after")
    def _profile_binding(self) -> RegressionSuiteDispatchV1:
        if (
            self.environment_profile.field_path != "/environment_profile"
            or self.environment_profile.expected_profile_kind != "environment"
        ):
            raise ValueError("regression suite environment profile binding is not exact")
        return self


class AgentEnvRegressionFindingTemplateV1(_FrozenModel):
    """Exact target-predicate identity emitted when a deterministic assertion fails."""

    defect_class: BoundedId
    severity: Severity
    entities: tuple[BoundedId, ...] = Field(default=(), max_length=1_024)
    relations: tuple[BoundedId, ...] = Field(default=(), max_length=1_024)
    constraint_id: BoundedId | None = None
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
    minimal_repro: dict[str, JsonValue] = Field(default_factory=dict)
    message: BoundedResult

    @field_validator("entities", "relations")
    @classmethod
    def _canonical_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("regression Finding locator ids must be unique")
        return tuple(sorted(value))

    @field_validator("evidence", "minimal_repro")
    @classmethod
    def _bounded_evidence(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _validate_json_shape(
            value,
            label="regression Finding identity evidence",
            max_bytes=64 * 1024,
        )
        return value


class AgentEnvRegressionStepV1(_FrozenModel):
    """One ordered atomic action plus optional deterministic step assertions."""

    action: JsonValue
    expected_last_action_result: BoundedResult | None = None
    expected_done: bool | None = None
    expected_state_hash: BoundedId | None = None
    failure_finding: AgentEnvRegressionFindingTemplateV1 | None = None

    @field_validator("action")
    @classmethod
    def _bounded_action(cls, value: JsonValue) -> JsonValue:
        if not isinstance(value, dict):
            raise ValueError("regression action must be an object")
        return _validate_json_shape(
            value,
            label="regression action",
            max_bytes=64 * 1024,
        )


class AgentEnvRegressionCaseV1(_FrozenModel):
    case_id: BoundedId
    scenario_id: BoundedId
    steps: tuple[AgentEnvRegressionStepV1, ...] = Field(max_length=MAX_REGRESSION_STEPS_PER_CASE)
    completion_oracle: CompletionOracleRefV1
    expected_completed: bool
    expected_initial_state_hash: BoundedId | None = None
    expected_final_state_hash: BoundedId | None = None
    failure_finding: AgentEnvRegressionFindingTemplateV1 | None = None


class AgentEnvRegressionPayloadV1(_FrozenModel):
    """Adapter payload for deterministic Agent-Env action replay."""

    adapter_payload_schema_version: Literal["agent-env-regression@1"] = "agent-env-regression@1"
    completion_oracle_registry_ref: CompletionOracleRegistryRefV1
    cases: tuple[AgentEnvRegressionCaseV1, ...] = Field(
        min_length=1,
        max_length=MAX_REGRESSION_CASES,
    )

    @field_validator("cases")
    @classmethod
    def _canonical_cases(
        cls, value: tuple[AgentEnvRegressionCaseV1, ...]
    ) -> tuple[AgentEnvRegressionCaseV1, ...]:
        case_ids = [item.case_id for item in value]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("regression case ids must be unique")
        if sum(len(item.steps) for item in value) > MAX_REGRESSION_TOTAL_STEPS:
            raise ValueError("regression suite exceeds its total step budget")
        return tuple(sorted(value, key=lambda item: item.case_id))


class RegressionCaseSeedV1(_FrozenModel):
    case_id: BoundedId
    derivation_case_id: Annotated[
        str,
        StringConstraints(min_length=1, max_length=2 * MAX_REGRESSION_ID_LENGTH + 1),
    ]
    replication_index: Literal[0] = 0
    seed: int = Field(ge=0, le=(1 << 64) - 1)


class RegressionCaseSeedManifestV1(_FrozenModel):
    manifest_schema_version: Literal["regression-case-seeds@1"] = "regression-case-seeds@1"
    suite_artifact_id: BoundedId
    root_seed: int = Field(ge=0, le=(1 << 64) - 1)
    run_kind: RunKindRef
    profile: ProfileRefV1
    seed_derivation_version: Literal["subseed@1"] = REGRESSION_CASE_SEED_DERIVATION_VERSION
    cases: tuple[RegressionCaseSeedV1, ...] = Field(
        min_length=1,
        max_length=MAX_REGRESSION_CASES,
    )

    @field_validator("cases")
    @classmethod
    def _canonical_case_seeds(
        cls, value: tuple[RegressionCaseSeedV1, ...]
    ) -> tuple[RegressionCaseSeedV1, ...]:
        case_ids = [item.case_id for item in value]
        derivation_ids = [item.derivation_case_id for item in value]
        if len(case_ids) != len(set(case_ids)) or len(derivation_ids) != len(set(derivation_ids)):
            raise ValueError("regression case seed identities must be unique")
        return tuple(sorted(value, key=lambda item: item.case_id))

    @model_validator(mode="after")
    def _derived_seeds(self) -> RegressionCaseSeedManifestV1:
        for item in self.cases:
            if item.derivation_case_id != f"{self.suite_artifact_id}:{item.case_id}":
                raise ValueError("regression case seed identity differs from suite/case")
            digest = canonical_sha256(
                {
                    "root_seed": self.root_seed,
                    "run_kind": self.run_kind.model_dump(mode="json"),
                    "profile_id": self.profile.profile_id,
                    "profile_version": self.profile.version,
                    "case_id": item.derivation_case_id,
                    "replication_index": item.replication_index,
                }
            )
            if item.seed != int(digest[:16], 16):
                raise ValueError("regression case seed differs from subseed@1")
        return self


__all__ = [
    "AgentEnvRegressionCaseV1",
    "AgentEnvRegressionFindingTemplateV1",
    "AgentEnvRegressionPayloadV1",
    "AgentEnvRegressionStepV1",
    "MAX_REGRESSION_CASES",
    "MAX_REGRESSION_STEPS_PER_CASE",
    "MAX_REGRESSION_TOTAL_STEPS",
    "REGRESSION_CASE_SEED_DERIVATION_VERSION",
    "RegressionCaseSeedManifestV1",
    "RegressionCaseSeedV1",
    "RegressionSuiteAdapterRefV1",
    "RegressionSuiteDispatchV1",
]
