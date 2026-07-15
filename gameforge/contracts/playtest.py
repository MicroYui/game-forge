"""Game-neutral scenario, task-suite, and completion-oracle contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

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
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import DomainScope


MAX_PLAYTEST_ID_LENGTH = 512
MAX_PLAYTEST_STRING_LENGTH = 4096
MAX_PLAYTEST_COLLECTION_ITEMS = 1024
MAX_PLAYTEST_JSON_BYTES = 64 * 1024
MAX_PLAYTEST_JSON_DEPTH = 32
MAX_PLAYTEST_STEPS_PER_EPISODE = 10_000_000
# Byte bound for a single episode's canonical action trace (a bounded playthrough
# record, distinct from the per-value 64 KiB JSON bound applied to each action).
MAX_PLAYTEST_TRACE_JSON_BYTES = 8 * 1024 * 1024

BoundedId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_PLAYTEST_ID_LENGTH),
]
BoundedResult = Annotated[str, StringConstraints(max_length=MAX_PLAYTEST_STRING_LENGTH)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(ge=1)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _json_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _json_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_data(item) for item in value]
    return value


def _validate_bounded_json(value: JsonValue, *, field_name: str) -> JsonValue:
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) > MAX_PLAYTEST_JSON_BYTES:
        raise ValueError(f"{field_name} exceeds the canonical JSON byte limit")

    stack: list[tuple[JsonValue, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_PLAYTEST_JSON_DEPTH:
            raise ValueError(f"{field_name} exceeds the JSON depth limit")
        if isinstance(item, str):
            if len(item) > MAX_PLAYTEST_STRING_LENGTH:
                raise ValueError(f"{field_name} contains an oversized string")
        elif isinstance(item, dict):
            if len(item) > MAX_PLAYTEST_COLLECTION_ITEMS:
                raise ValueError(f"{field_name} contains an oversized object")
            for key, child in item.items():
                if len(key) > MAX_PLAYTEST_STRING_LENGTH:
                    raise ValueError(f"{field_name} contains an oversized object key")
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            if len(item) > MAX_PLAYTEST_COLLECTION_ITEMS:
                raise ValueError(f"{field_name} contains an oversized array")
            stack.extend((child, depth + 1) for child in item)
    return value


def _validate_domain_scope(value: DomainScope) -> None:
    if len(value.domain_ids) > MAX_PLAYTEST_COLLECTION_ITEMS:
        raise ValueError("domain_scope exceeds the domain count limit")
    if any(len(domain_id) > MAX_PLAYTEST_ID_LENGTH for domain_id in value.domain_ids):
        raise ValueError("domain_scope contains an oversized domain id")


class CompletionOracleRefV1(_FrozenModel):
    oracle_id: BoundedId
    version: PositiveInt
    params_schema_id: BoundedId
    params: JsonValue

    @field_validator("params")
    @classmethod
    def _params(cls, value: JsonValue) -> JsonValue:
        return _validate_bounded_json(value, field_name="params")


class CompletionOracleDefinitionV1(_FrozenModel):
    oracle_id: BoundedId
    version: PositiveInt
    params_schema_id: BoundedId
    result_schema_id: BoundedId
    executor_key: BoundedId


class CompletionOracleRegistryRefV1(_FrozenModel):
    registry_version: PositiveInt
    digest: Sha256Hex


def compute_completion_oracle_registry_digest(payload: Mapping[str, Any]) -> str:
    raw = _json_data(payload)
    definitions = sorted(
        raw.get("definitions", []),
        key=lambda item: (item["oracle_id"], item["version"]),
    )
    return canonical_sha256(
        {
            "registry_schema_version": raw.get(
                "registry_schema_version", "completion-oracle-registry@1"
            ),
            "registry_version": raw["registry_version"],
            "definitions": definitions,
        }
    )


class CompletionOracleRegistryV1(_FrozenModel):
    registry_schema_version: Literal["completion-oracle-registry@1"] = (
        "completion-oracle-registry@1"
    )
    registry_version: PositiveInt
    definitions: tuple[CompletionOracleDefinitionV1, ...] = Field(
        min_length=1,
        max_length=MAX_PLAYTEST_COLLECTION_ITEMS,
    )
    registry_digest: Sha256Hex

    @field_validator("definitions")
    @classmethod
    def _definitions(
        cls, value: tuple[CompletionOracleDefinitionV1, ...]
    ) -> tuple[CompletionOracleDefinitionV1, ...]:
        refs = [(item.oracle_id, item.version) for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("completion oracle refs must be unique")
        return tuple(sorted(value, key=lambda item: (item.oracle_id, item.version)))

    @model_validator(mode="after")
    def _digest(self) -> CompletionOracleRegistryV1:
        expected = compute_completion_oracle_registry_digest(
            self.model_dump(mode="json", exclude={"registry_digest"})
        )
        if self.registry_digest != expected:
            raise ValueError("registry_digest does not match registry content")
        return self


def resolve_completion_oracle(
    registry: CompletionOracleRegistryV1,
    registry_ref: CompletionOracleRegistryRefV1,
    oracle_ref: CompletionOracleRefV1,
) -> CompletionOracleDefinitionV1:
    if registry_ref.registry_version != registry.registry_version:
        raise ValueError("completion oracle registry version does not match")
    if registry_ref.digest != registry.registry_digest:
        raise ValueError("completion oracle registry digest does not match")
    for definition in registry.definitions:
        if (definition.oracle_id, definition.version) != (
            oracle_ref.oracle_id,
            oracle_ref.version,
        ):
            continue
        if definition.params_schema_id != oracle_ref.params_schema_id:
            raise ValueError("completion oracle params_schema_id does not match")
        return definition
    raise ValueError("completion oracle ref does not resolve in the frozen registry")


class ScenarioResetBindingV1(_FrozenModel):
    reset_schema_id: BoundedId
    payload_hash: Sha256Hex
    payload: JsonValue

    @field_validator("payload")
    @classmethod
    def _payload(cls, value: JsonValue) -> JsonValue:
        return _validate_bounded_json(value, field_name="payload")

    @model_validator(mode="after")
    def _hash(self) -> ScenarioResetBindingV1:
        if self.payload_hash != canonical_sha256(self.payload):
            raise ValueError("payload_hash does not match reset payload")
        return self


class ScenarioSpecV1(_FrozenModel):
    scenario_spec_schema_version: Literal["scenario-spec@1"] = "scenario-spec@1"
    scenario_id: BoundedId
    source_preview_artifact_id: BoundedId
    config_export_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId
    environment_profile: ProfileRefV1
    env_contract_version: BoundedId
    domain_scope: DomainScope
    reset_binding: ScenarioResetBindingV1

    @model_validator(mode="after")
    def _environment_binding(self) -> ScenarioSpecV1:
        _validate_domain_scope(self.domain_scope)
        return self


class TaskEpisodeV1(_FrozenModel):
    episode_id: BoundedId
    scenario_spec_artifact_id: BoundedId
    completion_oracle: CompletionOracleRefV1
    domain_scope: DomainScope
    reset_binding: ScenarioResetBindingV1
    step_budget: int = Field(ge=1, le=MAX_PLAYTEST_STEPS_PER_EPISODE)

    @model_validator(mode="after")
    def _bounded_scope(self) -> TaskEpisodeV1:
        _validate_domain_scope(self.domain_scope)
        return self


class TaskSuiteV1(_FrozenModel):
    task_suite_schema_version: Literal["task-suite@1"] = "task-suite@1"
    suite_profile: ProfileRefV1
    source_preview_artifact_id: BoundedId
    config_export_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId
    environment_profile: ProfileRefV1
    env_contract_version: BoundedId
    completion_oracle_registry_ref: CompletionOracleRegistryRefV1
    episodes: tuple[TaskEpisodeV1, ...] = Field(
        min_length=1,
        max_length=MAX_PLAYTEST_COLLECTION_ITEMS,
    )

    @field_validator("episodes")
    @classmethod
    def _episodes(cls, value: tuple[TaskEpisodeV1, ...]) -> tuple[TaskEpisodeV1, ...]:
        episode_ids = [item.episode_id for item in value]
        scenario_ids = [item.scenario_spec_artifact_id for item in value]
        if len(episode_ids) != len(set(episode_ids)):
            raise ValueError("episode_id values must be unique")
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("scenario_spec_artifact_id values must be unique")
        return tuple(sorted(value, key=lambda item: item.episode_id))


class PlaytestActionRecordV1(_FrozenModel):
    """One bounded record of an atomic env step the agent proposed + its outcome.

    ``action`` is the deterministic ``Action.model_dump`` the executor proposed;
    ``last_action_result`` and ``tick`` are read back from the deterministic
    engine's post-step observation — the LLM never decides either.
    """

    action: JsonValue
    last_action_result: BoundedResult
    tick: int = Field(ge=0)

    @field_validator("action")
    @classmethod
    def _action(cls, value: JsonValue) -> JsonValue:
        return _validate_bounded_json(value, field_name="action")


class PlaytestEpisodeTraceV1(_FrozenModel):
    """The bounded trace of ONE selected playtest episode.

    ``completed`` is the DETERMINISTIC completion-oracle verdict (env terminal
    signal), never an LLM claim. ``action_trace`` is the bounded step-by-step
    record; ``seed`` is the per-episode subseed derived from the run seed.
    """

    episode_id: BoundedId
    scenario_spec_artifact_id: BoundedId
    seed: int
    step_budget: int = Field(ge=1, le=MAX_PLAYTEST_STEPS_PER_EPISODE)
    completion_oracle: CompletionOracleRefV1
    completed: bool
    action_trace: tuple[PlaytestActionRecordV1, ...] = Field(
        max_length=MAX_PLAYTEST_STEPS_PER_EPISODE,
    )

    @model_validator(mode="after")
    def _bounded_trace(self) -> PlaytestEpisodeTraceV1:
        if len(self.action_trace) > self.step_budget:
            raise ValueError("action_trace exceeds the episode step budget")
        encoded = canonical_json(
            [record.model_dump(mode="json") for record in self.action_trace]
        ).encode("utf-8")
        if len(encoded) > MAX_PLAYTEST_TRACE_JSON_BYTES:
            raise ValueError("action_trace exceeds the canonical trace byte limit")
        return self


class PlaytestTraceV1(_FrozenModel):
    """The primary ``playtest_trace[playtest-trace@1]`` artifact.

    Binds the EXACT run inputs (config / constraint / task-suite), the
    environment + planner profiles, the producer-local ``seed``, and the selected
    ``{episode_id, scenario_spec_artifact_id}`` episode bindings with their bounded
    per-episode action traces + deterministic completion verdicts (spec L1155).
    """

    playtest_trace_schema_version: Literal["playtest-trace@1"] = "playtest-trace@1"
    config_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId
    task_suite_artifact_id: BoundedId
    environment_profile: ProfileRefV1
    planner_policy: ProfileRefV1
    env_contract_version: BoundedId
    interaction_mode: Literal["autonomous", "bounded_choice"]
    seed: int
    episodes: tuple[PlaytestEpisodeTraceV1, ...] = Field(
        min_length=1,
        max_length=MAX_PLAYTEST_COLLECTION_ITEMS,
    )

    @field_validator("episodes")
    @classmethod
    def _episodes(
        cls, value: tuple[PlaytestEpisodeTraceV1, ...]
    ) -> tuple[PlaytestEpisodeTraceV1, ...]:
        episode_ids = [item.episode_id for item in value]
        scenario_ids = [item.scenario_spec_artifact_id for item in value]
        if len(episode_ids) != len(set(episode_ids)):
            raise ValueError("playtest trace episode ids must be unique")
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("playtest trace scenario bindings must be unique")
        return tuple(sorted(value, key=lambda item: item.episode_id))
