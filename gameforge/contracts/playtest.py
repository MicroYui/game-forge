"""Game-neutral scenario, task-suite, and completion-oracle contracts."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
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
from gameforge.contracts.execution_profiles import (
    MAX_PLAYTEST_EPISODES_V1,
    MAX_PLAYTEST_TOTAL_MODEL_CALLS_V2,
    MAX_PLAYTEST_TOTAL_STEPS_V1,
    MAX_PLAYTEST_TOTAL_TRACE_BYTES_V1,
    PlaytestPlannerProfileConfigV2,
    ProfileRefV1,
    RunKindRef,
)
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
# Conservative canonical bound used for admission-time aggregate preflight.  The
# action itself is limited to 64 KiB canonical JSON; this additionally covers a
# worst-case UTF-8 result string, state hash, field names, and framing.
MAX_PLAYTEST_ACTION_RECORD_CANONICAL_BYTES = 96 * 1024
MAX_PLAYTEST_TRACE_MARKERS = 3
MAX_PLAYTEST_EPISODE_METADATA_CANONICAL_BYTES = 128 * 1024
MAX_PLAYTEST_TRACE_ROOT_METADATA_CANONICAL_BYTES = 128 * 1024
PLAYTEST_MODEL_CALLS_PER_STEP_OFF = 3
PLAYTEST_MODEL_CALLS_PER_STEP_WITH_MEMORY = 6

BoundedId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_PLAYTEST_ID_LENGTH),
]
BoundedResult = Annotated[str, StringConstraints(max_length=MAX_PLAYTEST_STRING_LENGTH)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StateHash = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]
PositiveInt = Annotated[int, Field(ge=1)]
UInt64 = Annotated[int, Field(ge=0, le=(1 << 64) - 1)]
PlaytestPayloadSchemaPurposeV1 = Literal[
    "scenario_reset",
    "completion_oracle_params",
]
PlaytestTerminalReasonV1 = Literal[
    "completion_oracle_satisfied",
    "step_limit_exhausted",
    "deterministic_abort",
    "agent_stopped",
]
PlaytestTraceMarkerKindV1 = Literal[
    "completion",
    "failure",
    "step_limit",
    "stuck",
    "loop",
]


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


def playtest_resource_upper_bounds(
    config: PlaytestPlannerProfileConfigV2,
    *,
    episode_count: int,
    max_steps_per_episode: int,
) -> tuple[int, int, int]:
    """Return and validate the accepted Run's complete worst-case work envelope."""

    if (
        isinstance(episode_count, bool)
        or isinstance(max_steps_per_episode, bool)
        or not isinstance(episode_count, int)
        or not isinstance(max_steps_per_episode, int)
        or episode_count < 1
        or max_steps_per_episode < 1
    ):
        raise ValueError("playtest resource request must contain positive integer bounds")
    if episode_count > config.max_episode_count:
        raise ValueError("playtest episode count exceeds planner profile authority")
    if max_steps_per_episode > config.max_steps_per_episode:
        raise ValueError("playtest per-episode steps exceed planner profile authority")

    total_step_limit = episode_count * max_steps_per_episode
    if total_step_limit > config.max_total_steps:
        raise ValueError("playtest total steps exceed planner profile authority")
    calls_per_step = (
        PLAYTEST_MODEL_CALLS_PER_STEP_WITH_MEMORY
        if config.memory_mode == "llm_compaction"
        else PLAYTEST_MODEL_CALLS_PER_STEP_OFF
    )
    model_call_upper_bound = total_step_limit * calls_per_step
    if model_call_upper_bound > config.max_total_model_calls:
        raise ValueError("playtest model calls exceed planner profile authority")

    per_episode_action_upper_bound = min(
        MAX_PLAYTEST_TRACE_JSON_BYTES,
        2 + max_steps_per_episode * (MAX_PLAYTEST_ACTION_RECORD_CANONICAL_BYTES + 1),
    )
    total_trace_byte_upper_bound = (
        MAX_PLAYTEST_TRACE_ROOT_METADATA_CANONICAL_BYTES
        + episode_count
        * (per_episode_action_upper_bound + MAX_PLAYTEST_EPISODE_METADATA_CANONICAL_BYTES)
    )
    if total_trace_byte_upper_bound > config.max_total_trace_bytes:
        raise ValueError("playtest trace bytes exceed planner profile authority")
    return total_step_limit, model_call_upper_bound, total_trace_byte_upper_bound


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


class PlaytestPayloadSchemaDefinitionV1(_FrozenModel):
    """One immutable, trusted validator binding for a playtest JSON payload."""

    schema_id: BoundedId
    purpose: PlaytestPayloadSchemaPurposeV1
    validator_key: BoundedId


def compute_playtest_payload_schema_registry_digest(payload: Mapping[str, Any]) -> str:
    raw = _json_data(payload)
    definitions = sorted(raw.get("definitions", []), key=lambda item: item["schema_id"])
    return canonical_sha256(
        {
            "registry_schema_version": raw.get(
                "registry_schema_version", "playtest-payload-schema-registry@1"
            ),
            "registry_version": raw["registry_version"],
            "definitions": definitions,
        }
    )


class PlaytestPayloadSchemaRegistryV1(_FrozenModel):
    registry_schema_version: Literal["playtest-payload-schema-registry@1"] = (
        "playtest-payload-schema-registry@1"
    )
    registry_version: PositiveInt
    definitions: tuple[PlaytestPayloadSchemaDefinitionV1, ...] = Field(
        min_length=1,
        max_length=MAX_PLAYTEST_COLLECTION_ITEMS,
    )
    registry_digest: Sha256Hex

    @field_validator("definitions")
    @classmethod
    def _definitions(
        cls, value: tuple[PlaytestPayloadSchemaDefinitionV1, ...]
    ) -> tuple[PlaytestPayloadSchemaDefinitionV1, ...]:
        schema_ids = [item.schema_id for item in value]
        if len(schema_ids) != len(set(schema_ids)):
            raise ValueError("playtest payload schema ids must be unique")
        return tuple(sorted(value, key=lambda item: item.schema_id))

    @model_validator(mode="after")
    def _digest(self) -> PlaytestPayloadSchemaRegistryV1:
        expected = compute_playtest_payload_schema_registry_digest(
            self.model_dump(mode="json", exclude={"registry_digest"})
        )
        if self.registry_digest != expected:
            raise ValueError("registry_digest does not match registry content")
        return self


def resolve_playtest_payload_schema(
    registry: PlaytestPayloadSchemaRegistryV1,
    *,
    schema_id: str,
    purpose: PlaytestPayloadSchemaPurposeV1,
) -> PlaytestPayloadSchemaDefinitionV1:
    for definition in registry.definitions:
        if definition.schema_id != schema_id:
            continue
        if definition.purpose != purpose:
            raise ValueError("playtest payload schema purpose does not match")
        return definition
    raise ValueError("playtest payload schema does not resolve in the retained registry")


class GenericEnvironmentResetPayloadV1(_FrozenModel):
    """Built-in ``generic-env-reset@1`` deterministic reset payload."""

    scenario_id: BoundedId
    config_export_artifact_id: BoundedId
    quest_ids: tuple[BoundedId, ...] = Field(max_length=MAX_PLAYTEST_COLLECTION_ITEMS)
    # The Run root/subseed is the sole execution seed authority.  A future reset
    # seed-composition algorithm requires a new schema version and trace binding.
    start_seed: Literal[0]

    @field_validator("quest_ids")
    @classmethod
    def _quest_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("quest_ids must be unique")
        return tuple(sorted(value))


class StatePredicateCompletionParamsV1(_FrozenModel):
    """Built-in ``state-predicate-params@1`` authority."""

    predicate: Literal["all_quests_completed"]


class BoundedProgressCompletionParamsV1(_FrozenModel):
    """Built-in ``bounded-progress-params@1`` authority."""

    min_completed_quest_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    @field_validator("min_completed_quest_fraction", mode="before")
    @classmethod
    def _canonical_float_wire_value(cls, value: object) -> object:
        # Immutable Artifact payloads use the repository's historical
        # ``canonical_json`` encoding, where floats are represented as ``f:<decimal>``.
        # This schema-aware decoder restores only this numeric field; arbitrary
        # strings remain invalid.
        if isinstance(value, str) and value.startswith("f:"):
            try:
                return float(Decimal(value[2:]))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("invalid canonical float wire value") from exc
        return value


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
    state_hash: StateHash

    @field_validator("action")
    @classmethod
    def _action(cls, value: JsonValue) -> JsonValue:
        return _validate_bounded_json(value, field_name="action")

    @model_validator(mode="after")
    def _canonical_size(self) -> "PlaytestActionRecordV1":
        if len(canonical_json(self.model_dump(mode="json")).encode("utf-8")) > (
            MAX_PLAYTEST_ACTION_RECORD_CANONICAL_BYTES
        ):
            raise ValueError("playtest action record exceeds its canonical byte limit")
        return self


class PlaytestTraceMarkerV1(_FrozenModel):
    """A deterministic timeline marker consumed by the generic TracePlayer."""

    kind: PlaytestTraceMarkerKindV1
    step_index: int | None = Field(default=None, ge=0)
    state_hash: StateHash
    detail: BoundedResult = ""


def derive_playtest_trace_markers(
    records: tuple[PlaytestActionRecordV1, ...],
    *,
    initial_state_hash: str,
    final_state_hash: str,
    terminal_reason: PlaytestTerminalReasonV1,
) -> tuple[PlaytestTraceMarkerV1, ...]:
    """Derive the one canonical marker sequence from authoritative trace state."""

    markers: list[PlaytestTraceMarkerV1] = []
    state_hashes = [record.state_hash for record in records]
    for index in range(2, len(state_hashes)):
        if len(set(state_hashes[index - 2 : index + 1])) == 1:
            markers.append(
                PlaytestTraceMarkerV1(
                    kind="stuck",
                    step_index=index,
                    state_hash=state_hashes[index],
                    detail="three consecutive steps produced the same authoritative state",
                )
            )
            break

    seen: dict[str, int] = {initial_state_hash: -1}
    for index, state_hash in enumerate(state_hashes):
        previous = seen.get(state_hash)
        if previous is not None and index - previous > 1:
            markers.append(
                PlaytestTraceMarkerV1(
                    kind="loop",
                    step_index=index,
                    state_hash=state_hash,
                    detail="authoritative state recurred after an intervening step",
                )
            )
            break
        seen[state_hash] = index

    terminal_kind = {
        "completion_oracle_satisfied": "completion",
        "step_limit_exhausted": "step_limit",
        "deterministic_abort": "failure",
        "agent_stopped": "failure",
    }[terminal_reason]
    markers.append(
        PlaytestTraceMarkerV1(
            kind=terminal_kind,
            step_index=len(records) - 1 if records else None,
            state_hash=final_state_hash,
            detail=terminal_reason,
        )
    )
    return tuple(markers)


class PlaytestExecutionEnvelopeV1(_FrozenModel):
    """Self-contained aggregate resource/usage evidence for one playtest trace."""

    planner_profile_payload_hash: Sha256Hex
    selected_episode_count: int = Field(ge=1, le=MAX_PLAYTEST_EPISODES_V1)
    total_step_limit: int = Field(ge=1, le=MAX_PLAYTEST_TOTAL_STEPS_V1)
    model_call_upper_bound: int = Field(ge=1, le=MAX_PLAYTEST_TOTAL_MODEL_CALLS_V2)
    total_trace_byte_upper_bound: int = Field(
        ge=1,
        le=MAX_PLAYTEST_TOTAL_TRACE_BYTES_V1,
    )
    actual_model_calls: int = Field(ge=0, le=MAX_PLAYTEST_TOTAL_MODEL_CALLS_V2)
    total_action_count: int = Field(ge=0, le=MAX_PLAYTEST_TOTAL_STEPS_V1)
    total_action_trace_bytes: int = Field(
        ge=0,
        le=MAX_PLAYTEST_TOTAL_TRACE_BYTES_V1,
    )
    actual_trace_bytes: int = Field(ge=1, le=MAX_PLAYTEST_TOTAL_TRACE_BYTES_V1)

    @model_validator(mode="after")
    def _usage_within_envelope(self) -> "PlaytestExecutionEnvelopeV1":
        if self.actual_model_calls > self.model_call_upper_bound:
            raise ValueError("actual playtest model calls exceed the preflight upper bound")
        if self.total_action_count > self.total_step_limit:
            raise ValueError("actual playtest steps exceed the preflight upper bound")
        if self.total_action_trace_bytes > self.total_trace_byte_upper_bound:
            raise ValueError("actual playtest trace bytes exceed the preflight upper bound")
        if self.actual_trace_bytes > self.total_trace_byte_upper_bound:
            raise ValueError("complete playtest trace exceeds the preflight upper bound")
        return self


class PlaytestEpisodeSeedBindingV1(_FrozenModel):
    """Complete, independently re-derivable ``subseed@1`` episode authority."""

    seed_derivation_version: Literal["subseed@1"] = "subseed@1"
    root_seed: UInt64
    run_kind: RunKindRef
    profile: ProfileRefV1
    case_id: BoundedId
    replication_index: int = Field(ge=0)
    seed: UInt64

    @model_validator(mode="after")
    def _derived_seed(self) -> PlaytestEpisodeSeedBindingV1:
        digest = canonical_sha256(
            {
                "root_seed": self.root_seed,
                "run_kind": self.run_kind.model_dump(mode="json"),
                "profile_id": self.profile.profile_id,
                "profile_version": self.profile.version,
                "case_id": self.case_id,
                "replication_index": self.replication_index,
            }
        )
        if self.seed != int(digest[:16], 16):
            raise ValueError("playtest episode seed differs from subseed@1")
        return self


class PlaytestEpisodeTraceV1(_FrozenModel):
    """The bounded trace of ONE selected playtest episode.

    ``completed`` is the DETERMINISTIC completion-oracle verdict (env terminal
    signal), never an LLM claim. ``action_trace`` is the bounded step-by-step
    record; ``seed`` is the per-episode subseed derived from the run seed.
    """

    episode_id: BoundedId
    scenario_spec_artifact_id: BoundedId
    seed: UInt64
    seed_binding: PlaytestEpisodeSeedBindingV1
    step_budget: int = Field(ge=1, le=MAX_PLAYTEST_STEPS_PER_EPISODE)
    execution_step_limit: int = Field(ge=1, le=MAX_PLAYTEST_STEPS_PER_EPISODE)
    completion_oracle: CompletionOracleRefV1
    completed: bool
    terminal_reason: PlaytestTerminalReasonV1
    initial_state_hash: StateHash
    final_state_hash: StateHash
    action_trace: tuple[PlaytestActionRecordV1, ...] = Field(
        max_length=MAX_PLAYTEST_STEPS_PER_EPISODE,
    )
    markers: tuple[PlaytestTraceMarkerV1, ...] = Field(
        min_length=1,
        max_length=MAX_PLAYTEST_TRACE_MARKERS,
    )

    @field_validator("markers")
    @classmethod
    def _markers(
        cls, value: tuple[PlaytestTraceMarkerV1, ...]
    ) -> tuple[PlaytestTraceMarkerV1, ...]:
        identities = [(item.kind, item.step_index, item.state_hash, item.detail) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("playtest trace markers must be unique")
        return value

    @model_validator(mode="after")
    def _bounded_trace(self) -> PlaytestEpisodeTraceV1:
        if self.seed_binding.seed != self.seed:
            raise ValueError("playtest episode seed differs from its complete binding")
        if self.execution_step_limit > self.step_budget:
            raise ValueError("execution step limit exceeds the episode step budget")
        if len(self.action_trace) > self.execution_step_limit:
            raise ValueError("action_trace exceeds the episode execution step limit")
        if bool(self.completed) != (self.terminal_reason == "completion_oracle_satisfied"):
            raise ValueError("completion verdict differs from the terminal reason")
        if self.terminal_reason == "step_limit_exhausted" and (
            len(self.action_trace) != self.execution_step_limit
        ):
            raise ValueError("step-limit terminal reason requires an exhausted execution limit")
        if self.terminal_reason in {"deterministic_abort", "agent_stopped"} and (
            len(self.action_trace) >= self.execution_step_limit
        ):
            raise ValueError("early terminal reason requires unused execution steps")
        if self.action_trace:
            if self.final_state_hash != self.action_trace[-1].state_hash:
                raise ValueError("final state hash differs from the last action record")
        elif self.final_state_hash != self.initial_state_hash:
            raise ValueError("empty action trace must preserve the initial state hash")
        ticks = [record.tick for record in self.action_trace]
        if any(right < left for left, right in zip(ticks, ticks[1:], strict=False)):
            raise ValueError("playtest action ticks must be non-decreasing")
        for marker in self.markers:
            if marker.step_index is None:
                if self.action_trace:
                    raise ValueError("non-empty trace marker requires a step index")
            elif marker.step_index >= len(self.action_trace):
                raise ValueError("playtest trace marker points outside the action trace")
            elif marker.state_hash != self.action_trace[marker.step_index].state_hash:
                raise ValueError("playtest trace marker differs from its action state")
        expected_markers = derive_playtest_trace_markers(
            self.action_trace,
            initial_state_hash=self.initial_state_hash,
            final_state_hash=self.final_state_hash,
            terminal_reason=self.terminal_reason,
        )
        if self.markers != expected_markers:
            raise ValueError("playtest trace markers are not the canonical state timeline")
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
    seed: UInt64
    requested_max_steps_per_episode: int = Field(
        ge=1,
        le=MAX_PLAYTEST_STEPS_PER_EPISODE,
    )
    planner_memory_mode: Literal["off", "llm_compaction"]
    execution_envelope: PlaytestExecutionEnvelopeV1
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

    @model_validator(mode="after")
    def _seed_bindings(self) -> PlaytestTraceV1:
        action_count = 0
        action_trace_bytes = 0
        for episode in self.episodes:
            binding = episode.seed_binding
            if (
                binding.root_seed != self.seed
                or binding.run_kind != RunKindRef(kind="playtest.run", version=1)
                or binding.profile != self.environment_profile
                or binding.case_id != f"{self.task_suite_artifact_id}:{episode.episode_id}"
                or binding.replication_index != 0
            ):
                raise ValueError("playtest episode seed binding differs from trace authority")
            if episode.execution_step_limit != self.requested_max_steps_per_episode:
                raise ValueError("episode execution limit differs from the Run request")
            action_count += len(episode.action_trace)
            action_trace_bytes += len(
                canonical_json(
                    [record.model_dump(mode="json") for record in episode.action_trace]
                ).encode("utf-8")
            )
        total_step_limit = len(self.episodes) * self.requested_max_steps_per_episode
        calls_per_step = (
            PLAYTEST_MODEL_CALLS_PER_STEP_WITH_MEMORY
            if self.planner_memory_mode == "llm_compaction"
            else PLAYTEST_MODEL_CALLS_PER_STEP_OFF
        )
        per_episode_action_upper_bound = min(
            MAX_PLAYTEST_TRACE_JSON_BYTES,
            2
            + self.requested_max_steps_per_episode
            * (MAX_PLAYTEST_ACTION_RECORD_CANONICAL_BYTES + 1),
        )
        expected = {
            "selected_episode_count": len(self.episodes),
            "total_step_limit": total_step_limit,
            "model_call_upper_bound": total_step_limit * calls_per_step,
            "total_trace_byte_upper_bound": (
                MAX_PLAYTEST_TRACE_ROOT_METADATA_CANONICAL_BYTES
                + len(self.episodes)
                * (per_episode_action_upper_bound + MAX_PLAYTEST_EPISODE_METADATA_CANONICAL_BYTES)
            ),
            "total_action_count": action_count,
            "total_action_trace_bytes": action_trace_bytes,
        }
        actual = self.execution_envelope.model_dump(mode="python")
        for field, value in expected.items():
            if actual[field] != value:
                raise ValueError(f"playtest execution envelope {field} is not exact")
        if self.execution_envelope.actual_trace_bytes != len(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ):
            raise ValueError("playtest execution envelope complete trace bytes are not exact")
        return self


def bind_exact_playtest_trace_bytes(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached trace payload with its self-inclusive byte count sealed."""

    raw = _json_data(payload)
    if not isinstance(raw, dict):
        raise ValueError("playtest trace payload must be a JSON object")
    raw.setdefault("playtest_trace_schema_version", "playtest-trace@1")
    envelope = raw.get("execution_envelope")
    if not isinstance(envelope, dict):
        raise ValueError("playtest trace execution envelope must be a JSON object")
    envelope["actual_trace_bytes"] = 1
    for _ in range(8):
        exact_size = len(canonical_json(raw).encode("utf-8"))
        if envelope["actual_trace_bytes"] == exact_size:
            return raw
        envelope["actual_trace_bytes"] = exact_size
    raise ValueError("playtest trace byte size did not converge")
