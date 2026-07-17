from __future__ import annotations

from pydantic import ValidationError
import pytest

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.playtest import (
    MAX_PLAYTEST_ACTION_RECORD_CANONICAL_BYTES,
    MAX_PLAYTEST_EPISODE_METADATA_CANONICAL_BYTES,
    MAX_PLAYTEST_JSON_BYTES,
    MAX_PLAYTEST_STRING_LENGTH,
    MAX_PLAYTEST_TRACE_JSON_BYTES,
    MAX_PLAYTEST_TRACE_ROOT_METADATA_CANONICAL_BYTES,
    CompletionOracleDefinitionV1,
    CompletionOracleRefV1,
    CompletionOracleRegistryRefV1,
    CompletionOracleRegistryV1,
    BoundedProgressCompletionParamsV1,
    GenericEnvironmentResetPayloadV1,
    PlaytestActionRecordV1,
    PlaytestExecutionEnvelopeV1,
    PlaytestEpisodeSeedBindingV1,
    PlaytestEpisodeTraceV1,
    PlaytestTraceMarkerV1,
    PlaytestTraceV1,
    PlaytestPayloadSchemaDefinitionV1,
    PlaytestPayloadSchemaRegistryV1,
    ScenarioResetBindingV1,
    ScenarioSpecV1,
    StatePredicateCompletionParamsV1,
    TaskEpisodeV1,
    TaskSuiteV1,
    bind_exact_playtest_trace_bytes,
    compute_completion_oracle_registry_digest,
    compute_playtest_payload_schema_registry_digest,
    resolve_completion_oracle,
    resolve_playtest_payload_schema,
)


def _definition(
    oracle_id: str = "quest-complete", version: int = 1
) -> CompletionOracleDefinitionV1:
    return CompletionOracleDefinitionV1(
        oracle_id=oracle_id,
        version=version,
        params_schema_id="quest-completion-params@1",
        result_schema_id="completion-result@1",
        executor_key="quest_completion_v1",
    )


def _registry(*definitions: CompletionOracleDefinitionV1) -> CompletionOracleRegistryV1:
    payload = {
        "registry_schema_version": "completion-oracle-registry@1",
        "registry_version": 4,
        "definitions": definitions,
    }
    return CompletionOracleRegistryV1(
        **payload,
        registry_digest=compute_completion_oracle_registry_digest(payload),
    )


def _reset(payload: object | None = None) -> ScenarioResetBindingV1:
    value = {"spawn": "outpost"} if payload is None else payload
    return ScenarioResetBindingV1(
        reset_schema_id="fixture-reset@1",
        payload_hash=canonical_sha256(value),
        payload=value,
    )


def _episode(
    episode_id: str,
    scenario_artifact_id: str,
    *,
    reset: ScenarioResetBindingV1 | None = None,
) -> TaskEpisodeV1:
    return TaskEpisodeV1(
        episode_id=episode_id,
        scenario_spec_artifact_id=scenario_artifact_id,
        completion_oracle=CompletionOracleRefV1(
            oracle_id="quest-complete",
            version=1,
            params_schema_id="quest-completion-params@1",
            params={"quest_id": "main"},
        ),
        domain_scope=DomainScope(domain_ids=("quests",)),
        reset_binding=reset or _reset(),
        step_budget=250,
    )


def _suite(*episodes: TaskEpisodeV1) -> TaskSuiteV1:
    return TaskSuiteV1(
        suite_profile=ProfileRefV1(profile_id="suite:quest-regression", version=2),
        source_preview_artifact_id="artifact:preview",
        config_export_artifact_id="artifact:config",
        constraint_snapshot_artifact_id="artifact:constraints",
        environment_profile=ProfileRefV1(profile_id="environment:fixture", version=2),
        env_contract_version="agent-env@2",
        completion_oracle_registry_ref=CompletionOracleRegistryRefV1(
            registry_version=4,
            digest="a" * 64,
        ),
        episodes=episodes,
    )


def test_completion_oracle_registry_is_canonical_digest_bound_and_resolvable() -> None:
    second = _definition("inventory-contains", 2)
    first = _definition()
    registry = _registry(second, first)

    assert tuple((item.oracle_id, item.version) for item in registry.definitions) == (
        ("inventory-contains", 2),
        ("quest-complete", 1),
    )
    ref = CompletionOracleRegistryRefV1(
        registry_version=registry.registry_version,
        digest=registry.registry_digest,
    )
    oracle = CompletionOracleRefV1(
        oracle_id="quest-complete",
        version=1,
        params_schema_id="quest-completion-params@1",
        params={"quest_id": "main"},
    )

    assert resolve_completion_oracle(registry, ref, oracle) == first
    with pytest.raises(ValueError, match="params_schema_id"):
        resolve_completion_oracle(
            registry,
            ref,
            oracle.model_copy(update={"params_schema_id": "wrong@1"}),
        )
    with pytest.raises(ValueError, match="registry digest"):
        resolve_completion_oracle(
            registry,
            ref.model_copy(update={"digest": "b" * 64}),
            oracle,
        )

    with pytest.raises(ValidationError, match="registry_digest"):
        CompletionOracleRegistryV1(
            **{
                **registry.model_dump(mode="python"),
                "registry_digest": "b" * 64,
            }
        )


def test_completion_oracle_registry_rejects_duplicate_exact_refs() -> None:
    definition = _definition()
    payload = {
        "registry_schema_version": "completion-oracle-registry@1",
        "registry_version": 1,
        "definitions": (definition, definition),
    }
    with pytest.raises(ValidationError, match="unique"):
        CompletionOracleRegistryV1(
            **payload,
            registry_digest=compute_completion_oracle_registry_digest(payload),
        )


def test_playtest_payload_schema_registry_is_digest_bound_and_exactly_resolvable() -> None:
    reset = PlaytestPayloadSchemaDefinitionV1(
        schema_id="generic-env-reset@1",
        purpose="scenario_reset",
        validator_key="generic_env_reset_payload@1",
    )
    params = PlaytestPayloadSchemaDefinitionV1(
        schema_id="state-predicate-params@1",
        purpose="completion_oracle_params",
        validator_key="state_predicate_params@1",
    )
    payload = {
        "registry_version": 1,
        "definitions": (params, reset),
    }
    registry = PlaytestPayloadSchemaRegistryV1(
        **payload,
        registry_digest=compute_playtest_payload_schema_registry_digest(payload),
    )

    assert tuple(item.schema_id for item in registry.definitions) == (
        "generic-env-reset@1",
        "state-predicate-params@1",
    )
    assert (
        resolve_playtest_payload_schema(
            registry,
            schema_id="generic-env-reset@1",
            purpose="scenario_reset",
        )
        == reset
    )
    with pytest.raises(ValueError, match="purpose"):
        resolve_playtest_payload_schema(
            registry,
            schema_id="generic-env-reset@1",
            purpose="completion_oracle_params",
        )
    with pytest.raises(ValidationError, match="registry_digest"):
        PlaytestPayloadSchemaRegistryV1(
            **{
                **registry.model_dump(mode="python"),
                "registry_digest": "b" * 64,
            }
        )


def test_builtin_playtest_payload_models_reject_noncanonical_or_out_of_range_authority() -> None:
    reset = GenericEnvironmentResetPayloadV1(
        scenario_id="scenario:quest-main",
        config_export_artifact_id="artifact:config",
        quest_ids=("quest:b", "quest:a"),
        start_seed=0,
    )
    assert reset.quest_ids == ("quest:a", "quest:b")
    with pytest.raises(ValidationError, match="unique"):
        GenericEnvironmentResetPayloadV1(
            scenario_id="scenario:quest-main",
            config_export_artifact_id="artifact:config",
            quest_ids=("quest:a", "quest:a"),
            start_seed=0,
        )
    with pytest.raises(ValidationError):
        StatePredicateCompletionParamsV1(predicate="model_says_complete")
    with pytest.raises(ValidationError):
        BoundedProgressCompletionParamsV1(min_completed_quest_fraction=1.01)


def test_reset_binding_is_canonical_hash_bound_and_size_limited() -> None:
    reset = _reset({"inventory": ["key"], "position": {"x": 1, "y": 2}})
    assert reset.payload_hash == canonical_sha256(reset.payload)

    with pytest.raises(ValidationError, match="payload_hash"):
        ScenarioResetBindingV1(
            reset_schema_id=reset.reset_schema_id,
            payload_hash="0" * 64,
            payload=reset.payload,
        )
    with pytest.raises(ValidationError, match="payload"):
        _reset("x" * (MAX_PLAYTEST_JSON_BYTES + 1))


def test_scenario_keeps_environment_and_reset_schema_bindings_distinct() -> None:
    scenario = ScenarioSpecV1(
        scenario_id="scenario:quest-main",
        source_preview_artifact_id="artifact:preview",
        config_export_artifact_id="artifact:config",
        constraint_snapshot_artifact_id="artifact:constraints",
        environment_profile=ProfileRefV1(profile_id="environment:fixture", version=2),
        env_contract_version="agent-env@2",
        domain_scope=DomainScope(domain_ids=("quests",)),
        reset_binding=_reset(),
    )

    assert scenario.scenario_spec_schema_version == "scenario-spec@1"
    assert scenario.env_contract_version == "agent-env@2"
    assert scenario.reset_binding.reset_schema_id == "fixture-reset@1"


def test_task_suite_is_nonempty_stably_sorted_and_exactly_bound() -> None:
    second = _episode("episode:02", "artifact:scenario-02")
    first = _episode("episode:01", "artifact:scenario-01")
    suite = _suite(second, first)

    assert tuple(item.episode_id for item in suite.episodes) == ("episode:01", "episode:02")
    assert suite.task_suite_schema_version == "task-suite@1"

    with pytest.raises(ValidationError):
        _suite()
    with pytest.raises(ValidationError, match="episode_id"):
        _suite(first, first.model_copy(update={"scenario_spec_artifact_id": "artifact:other"}))
    with pytest.raises(ValidationError, match="scenario_spec_artifact_id"):
        _suite(
            first,
            second.model_copy(
                update={"scenario_spec_artifact_id": first.scenario_spec_artifact_id}
            ),
        )
    assert suite.env_contract_version == "agent-env@2"
    assert {item.reset_binding.reset_schema_id for item in suite.episodes} == {"fixture-reset@1"}


def test_jobs_keeps_completion_registry_ref_compatibility_export() -> None:
    from gameforge.contracts.jobs import CompletionOracleRegistryRefV1 as JobsRegistryRef

    assert JobsRegistryRef is CompletionOracleRegistryRefV1


def _oracle_ref() -> CompletionOracleRefV1:
    return CompletionOracleRefV1(
        oracle_id="state-predicate",
        version=1,
        params_schema_id="state-predicate-params@1",
        params={"predicate": "all_quests_completed"},
    )


def _episode_trace(
    episode_id: str,
    scenario_artifact_id: str,
    *,
    steps: int = 2,
    completed: bool = False,
) -> PlaytestEpisodeTraceV1:
    environment_profile = ProfileRefV1(profile_id="environment:fixture", version=2)
    case_id = f"artifact:suite:{episode_id}"
    digest = canonical_sha256(
        {
            "root_seed": 7,
            "run_kind": {"kind": "playtest.run", "version": 1},
            "profile_id": environment_profile.profile_id,
            "profile_version": environment_profile.version,
            "case_id": case_id,
            "replication_index": 0,
        }
    )
    seed = int(digest[:16], 16)
    state_hashes = tuple(f"sha256:{index:064x}" for index in range(1, steps + 1))
    initial_state_hash = f"sha256:{0:064x}"
    final_state_hash = state_hashes[-1] if state_hashes else initial_state_hash
    execution_step_limit = max(3, steps + (0 if completed else 1))
    terminal_reason = "completion_oracle_satisfied" if completed else "agent_stopped"
    terminal_marker = "completion" if completed else "failure"
    return PlaytestEpisodeTraceV1(
        episode_id=episode_id,
        scenario_spec_artifact_id=scenario_artifact_id,
        seed=seed,
        seed_binding=PlaytestEpisodeSeedBindingV1(
            root_seed=7,
            run_kind=RunKindRef(kind="playtest.run", version=1),
            profile=environment_profile,
            case_id=case_id,
            replication_index=0,
            seed=seed,
        ),
        step_budget=250,
        execution_step_limit=execution_step_limit,
        completion_oracle=_oracle_ref(),
        completed=completed,
        terminal_reason=terminal_reason,
        initial_state_hash=initial_state_hash,
        final_state_hash=final_state_hash,
        action_trace=tuple(
            PlaytestActionRecordV1(
                action={"kind": "observe"},
                last_action_result="observed",
                tick=i,
                state_hash=state_hashes[i],
            )
            for i in range(steps)
        ),
        markers=(
            PlaytestTraceMarkerV1(
                kind=terminal_marker,
                step_index=steps - 1 if steps else None,
                state_hash=final_state_hash,
                detail=terminal_reason,
            ),
        ),
    )


def _trace(*episodes: PlaytestEpisodeTraceV1) -> PlaytestTraceV1:
    requested_steps = 3
    episode_count = len(episodes)
    total_action_count = sum(len(episode.action_trace) for episode in episodes)
    total_action_trace_bytes = sum(
        len(
            canonical_json(
                [record.model_dump(mode="json") for record in episode.action_trace]
            ).encode("utf-8")
        )
        for episode in episodes
    )
    per_episode_upper = min(
        MAX_PLAYTEST_TRACE_JSON_BYTES,
        2 + requested_steps * (MAX_PLAYTEST_ACTION_RECORD_CANONICAL_BYTES + 1),
    )
    bounded_episode_count = max(1, episode_count)
    payload = {
        "config_artifact_id": "artifact:config",
        "constraint_snapshot_artifact_id": "artifact:constraints",
        "task_suite_artifact_id": "artifact:suite",
        "environment_profile": ProfileRefV1(profile_id="environment:fixture", version=2).model_dump(
            mode="json"
        ),
        "planner_policy": ProfileRefV1(profile_id="planner:layered", version=1).model_dump(
            mode="json"
        ),
        "env_contract_version": "agent-env@2",
        "interaction_mode": "autonomous",
        "seed": 7,
        "requested_max_steps_per_episode": requested_steps,
        "planner_memory_mode": "off",
        "execution_envelope": PlaytestExecutionEnvelopeV1(
            planner_profile_payload_hash="a" * 64,
            selected_episode_count=bounded_episode_count,
            total_step_limit=bounded_episode_count * requested_steps,
            model_call_upper_bound=bounded_episode_count * requested_steps * 3,
            total_trace_byte_upper_bound=(
                MAX_PLAYTEST_TRACE_ROOT_METADATA_CANONICAL_BYTES
                + bounded_episode_count
                * (per_episode_upper + MAX_PLAYTEST_EPISODE_METADATA_CANONICAL_BYTES)
            ),
            actual_model_calls=0,
            total_action_count=total_action_count,
            total_action_trace_bytes=total_action_trace_bytes,
            actual_trace_bytes=1,
        ).model_dump(mode="json"),
        "episodes": [episode.model_dump(mode="json") for episode in episodes],
    }
    return PlaytestTraceV1.model_validate(bind_exact_playtest_trace_bytes(payload))


def test_playtest_trace_binds_selected_episodes_and_sorts() -> None:
    trace = _trace(
        _episode_trace("episode:02", "artifact:scenario-02", completed=True),
        _episode_trace("episode:01", "artifact:scenario-01"),
    )

    assert trace.playtest_trace_schema_version == "playtest-trace@1"
    assert tuple(e.episode_id for e in trace.episodes) == ("episode:01", "episode:02")
    assert trace.interaction_mode == "autonomous"
    assert trace.env_contract_version == "agent-env@2"
    assert trace.seed == 7
    # The DETERMINISTIC completion verdict is carried per episode.
    assert {e.episode_id: e.completed for e in trace.episodes} == {
        "episode:01": False,
        "episode:02": True,
    }


def test_playtest_trace_rejects_duplicate_episode_or_scenario_bindings() -> None:
    first = _episode_trace("episode:01", "artifact:scenario-01")
    with pytest.raises(ValidationError, match="episode ids"):
        _trace(first, first.model_copy(update={"scenario_spec_artifact_id": "artifact:x"}))
    with pytest.raises(ValidationError, match="scenario bindings"):
        _trace(
            first,
            _episode_trace("episode:02", "artifact:scenario-01"),
        )
    with pytest.raises(ValidationError):
        _trace()  # min_length=1


def test_playtest_episode_trace_is_step_budget_and_byte_bounded() -> None:
    # action_trace longer than the step budget is rejected fail-closed.
    with pytest.raises(ValidationError, match="step budget"):
        PlaytestEpisodeTraceV1.model_validate(
            {
                **_episode_trace("episode:01", "artifact:scenario-01").model_dump(mode="json"),
                "step_budget": 1,
            }
        )


def test_playtest_episode_trace_accepts_equal_ticks_but_rejects_time_reversal() -> None:
    payload = _episode_trace("episode:01", "artifact:scenario-01", steps=3).model_dump(mode="json")
    payload["action_trace"][1]["tick"] = payload["action_trace"][0]["tick"]
    PlaytestEpisodeTraceV1.model_validate(payload)

    payload["action_trace"][0]["tick"] = 1
    payload["action_trace"][1]["tick"] = 1
    payload["action_trace"][2]["tick"] = 0
    with pytest.raises(ValidationError, match="non-decreasing"):
        PlaytestEpisodeTraceV1.model_validate(payload)


def test_playtest_episode_trace_rejects_forged_or_extra_markers() -> None:
    episode = _episode_trace("episode:01", "artifact:scenario-01", steps=3)
    payload = episode.model_dump(mode="json")
    payload["markers"][0]["state_hash"] = payload["initial_state_hash"]
    with pytest.raises(ValidationError, match="marker"):
        PlaytestEpisodeTraceV1.model_validate(payload)

    payload = episode.model_dump(mode="json")
    payload["markers"].insert(
        0,
        {
            "kind": "step_limit",
            "step_index": 0,
            "state_hash": payload["action_trace"][0]["state_hash"],
            "detail": "forged",
        },
    )
    with pytest.raises(ValidationError, match="canonical state timeline"):
        PlaytestEpisodeTraceV1.model_validate(payload)
    # a single action's JSON is bounded by the per-value string limit.
    with pytest.raises(ValidationError, match="oversized string"):
        PlaytestActionRecordV1(
            action={"kind": "x" * (MAX_PLAYTEST_STRING_LENGTH + 1)},
            last_action_result="ok",
            tick=0,
            state_hash=f"sha256:{0:064x}",
        )


def test_playtest_trace_rejects_forged_aggregate_resource_evidence() -> None:
    trace = _trace(_episode_trace("episode:01", "artifact:scenario-01"))
    forged = trace.model_dump(mode="json")
    forged["execution_envelope"]["actual_model_calls"] = (
        forged["execution_envelope"]["model_call_upper_bound"] + 1
    )

    with pytest.raises(ValidationError, match="model calls"):
        PlaytestTraceV1.model_validate(forged)


def test_playtest_trace_json_byte_bound_is_larger_than_single_value_bound() -> None:
    # The whole-trace byte bound is a distinct, larger bound than the per-value one.
    assert MAX_PLAYTEST_TRACE_JSON_BYTES > MAX_PLAYTEST_JSON_BYTES
