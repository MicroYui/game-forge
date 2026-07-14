from __future__ import annotations

from pydantic import ValidationError
import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.playtest import (
    MAX_PLAYTEST_JSON_BYTES,
    CompletionOracleDefinitionV1,
    CompletionOracleRefV1,
    CompletionOracleRegistryRefV1,
    CompletionOracleRegistryV1,
    ScenarioResetBindingV1,
    ScenarioSpecV1,
    TaskEpisodeV1,
    TaskSuiteV1,
    compute_completion_oracle_registry_digest,
    resolve_completion_oracle,
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
