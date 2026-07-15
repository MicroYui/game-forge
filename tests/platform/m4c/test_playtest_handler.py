"""Task 12b — ``playtest_runner@1`` (environment-profile-selected playtest).

Drives the REAL M2b ``PlaytestAgent`` through the ordered multi-node bridge router
against the REAL seeded ``AureusEnv`` (built from the ``caravan`` preview) with the
``handler_support`` ``FakeModelBridge`` serving REPLAY responses, and — for the
targeted edge cases (stale bindings, episode subset, unknown environment, bounded
interaction commands, findings projection) — an injected fake env-runner double.

The env completion verdict is DETERMINISTIC (the completion oracle / env terminal
signal); the LLM only plans/acts/reflects. The handler returns ONLY a valid
``PreparedRunOutcome``: ONE primary ``playtest_trace[playtest-trace@1]`` +
playtest-findings (source ``playtest``); an unknown environment yields a typed
UNAVAILABLE ``PreparedRunFailure``; deterministic + REPLAY ⇒ byte-identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from gameforge.apps.worker.completion_oracles import (
    ALL_QUESTS_COMPLETED_ORACLE,
    build_completion_oracle_executors,
)
from gameforge.apps.worker.playtest import AureusPlaytestRunner
from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    PlaytestEpisodeBindingV1,
    PlaytestRunPayloadV1,
    PreparedRunFailure,
    PreparedRunResult,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.playtest import (
    CompletionOracleDefinitionV1,
    CompletionOracleRegistryRefV1,
    CompletionOracleRegistryV1,
    PlaytestTraceV1,
    ScenarioResetBindingV1,
    ScenarioSpecV1,
    TaskEpisodeV1,
    TaskSuiteV1,
    compute_completion_oracle_registry_digest,
)
from gameforge.platform.publication.lineage import (
    LineageParentSources,
    ParentInfo,
    _candidate_for_rule,
)
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.run_handlers.playtest import (
    PlaytestEpisodeOutcomeV1,
    PlaytestEpisodeRunRequest,
    PlaytestRunHandler,
    derive_episode_seed,
)
from gameforge.spine.ir.loader import load_scenario
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    FakeModelBridge,
    build_context,
    execution_plan,
    resolved_binding,
)

PLAYTEST_KIND = RunKindRef(kind="playtest.run", version=1)
MODEL_REF = "anthropic/claude-opus-4-8/m2a@1"

CONFIG_ID = "artifact:config"
CONSTRAINT_ID = "artifact:constraint"
SUITE_ID = "artifact:suite"
PREVIEW_ID = "artifact:preview"
SCENARIO_01 = "artifact:scenario-01"
SCENARIO_02 = "artifact:scenario-02"

ENV_PROFILE = ProfileRefV1(profile_id="environment:aureus", version=1)
PLANNER_POLICY = ProfileRefV1(profile_id="planner:layered", version=1)
SUITE_PROFILE = ProfileRefV1(profile_id="suite:caravan", version=1)
ENV_CONTRACT_VERSION = "aureus-agent-env@1"

_HEX = "a" * 64


# --------------------------------------------------------------------------- data
def _preview_bytes() -> bytes:
    snapshot = load_scenario("scenarios/caravan.yaml")
    return canonical_json(snapshot.content_payload).encode("utf-8")


def _oracle_registry() -> CompletionOracleRegistryV1:
    definitions = (
        CompletionOracleDefinitionV1(
            oracle_id="state-predicate",
            version=1,
            params_schema_id="state-predicate-params@1",
            result_schema_id="completion-oracle-result@1",
            executor_key="state_predicate_oracle@1",
        ),
        CompletionOracleDefinitionV1(
            oracle_id="bounded-progress",
            version=1,
            params_schema_id="bounded-progress-params@1",
            result_schema_id="completion-oracle-result@1",
            executor_key="bounded_progress_oracle@1",
        ),
    )
    payload = {"registry_version": 1, "definitions": definitions}
    return CompletionOracleRegistryV1(
        **payload,
        registry_digest=compute_completion_oracle_registry_digest(payload),
    )


def _registry_ref() -> CompletionOracleRegistryRefV1:
    registry = _oracle_registry()
    return CompletionOracleRegistryRefV1(
        registry_version=registry.registry_version,
        digest=registry.registry_digest,
    )


def _reset(scenario_id: str) -> ScenarioResetBindingV1:
    value = {"scenario_id": scenario_id, "quest_ids": [], "start_seed": 0}
    return ScenarioResetBindingV1(
        reset_schema_id="aureus-env-reset@1",
        payload_hash=canonical_sha256(value),
        payload=value,
    )


def _scenario(scenario_id: str) -> ScenarioSpecV1:
    return ScenarioSpecV1(
        scenario_id=scenario_id,
        source_preview_artifact_id=PREVIEW_ID,
        config_export_artifact_id=CONFIG_ID,
        constraint_snapshot_artifact_id=CONSTRAINT_ID,
        environment_profile=ENV_PROFILE,
        env_contract_version=ENV_CONTRACT_VERSION,
        domain_scope=DomainScope(domain_ids=("aureus",)),
        reset_binding=_reset(scenario_id),
    )


def _episode(episode_id: str, scenario_artifact_id: str, scenario_id: str) -> TaskEpisodeV1:
    return TaskEpisodeV1(
        episode_id=episode_id,
        scenario_spec_artifact_id=scenario_artifact_id,
        completion_oracle=ALL_QUESTS_COMPLETED_ORACLE,
        domain_scope=DomainScope(domain_ids=("aureus",)),
        reset_binding=_reset(scenario_id),
        step_budget=250,
    )


def _suite(*episodes: TaskEpisodeV1) -> TaskSuiteV1:
    return TaskSuiteV1(
        suite_profile=SUITE_PROFILE,
        source_preview_artifact_id=PREVIEW_ID,
        config_export_artifact_id=CONFIG_ID,
        constraint_snapshot_artifact_id=CONSTRAINT_ID,
        environment_profile=ENV_PROFILE,
        env_contract_version=ENV_CONTRACT_VERSION,
        completion_oracle_registry_ref=_registry_ref(),
        episodes=episodes,
    )


def _default_suite() -> TaskSuiteV1:
    return _suite(_episode("episode:01", SCENARIO_01, "caravan"))


def _payload(
    *,
    episodes: tuple[PlaytestEpisodeBindingV1, ...] | None = None,
    interaction_mode: str = "autonomous",
    max_steps: int = 3,
) -> PlaytestRunPayloadV1:
    if episodes is None:
        episodes = (
            PlaytestEpisodeBindingV1(
                episode_id="episode:01", scenario_spec_artifact_id=SCENARIO_01
            ),
        )
    return PlaytestRunPayloadV1(
        config_artifact_id=CONFIG_ID,
        constraint_snapshot_artifact_id=CONSTRAINT_ID,
        task_suite_artifact_id=SUITE_ID,
        episodes=episodes,
        environment_profile=ENV_PROFILE,
        planner_policy=PLANNER_POLICY,
        max_steps_per_episode=max_steps,
        interaction_mode=interaction_mode,
    )


def _store(
    suite: TaskSuiteV1 | None = None, *, scenarios: tuple[str, ...] = (SCENARIO_01,)
) -> FakeArtifactStore:
    store = FakeArtifactStore()
    store.register(PREVIEW_ID, _preview_bytes())
    store.register(SUITE_ID, (suite or _default_suite()).model_dump(mode="json"))
    for scenario_artifact_id in scenarios:
        store.register(
            scenario_artifact_id,
            _scenario(_scenario_id_for(scenario_artifact_id)).model_dump(mode="json"),
        )
    return store


def _scenario_id_for(scenario_artifact_id: str) -> str:
    return "caravan" if scenario_artifact_id == SCENARIO_01 else "caravan-2"


def _context(bridge: FakeModelBridge, payload: PlaytestRunPayloadV1 | None = None):
    return build_context(
        params=payload or _payload(),
        kind=PLAYTEST_KIND,
        seed=11,
        resolved_profiles=(
            resolved_binding(
                "/params/environment_profile",
                profile_id="environment:aureus",
                version=1,
                kind="environment",
            ),
            resolved_binding(
                "/params/planner_policy",
                profile_id="planner:layered",
                version=1,
                kind="playtest_planner",
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan(
            {
                "playtest.planner": MODEL_REF,
                "playtest.executor": MODEL_REF,
                "playtest.reflect": MODEL_REF,
                "playtest.memory": MODEL_REF,
            }
        ),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )


def _real_runner(*, supported: tuple[ProfileRefV1, ...] = (ENV_PROFILE,)) -> AureusPlaytestRunner:
    return AureusPlaytestRunner(
        oracle_registry=_oracle_registry(),
        oracle_executors=build_completion_oracle_executors(),
        supported_profiles=frozenset(supported),
    )


def _real_handler(
    store: FakeArtifactStore, runner: AureusPlaytestRunner | None = None
) -> PlaytestRunHandler:
    return PlaytestRunHandler(blobs=store, store=store, env_runner=runner or _real_runner())


# ------------------------------------------------------------------ fake runner
@dataclass
class _FakeRunner:
    outcome: PlaytestEpisodeOutcomeV1
    supported: bool = True
    calls: list = field(default_factory=list)

    def supports(self, environment_profile: ProfileRefV1) -> bool:
        return self.supported

    def run_episode(self, request: PlaytestEpisodeRunRequest) -> PlaytestEpisodeOutcomeV1:
        self.calls.append(request)
        return self.outcome


def _observe_outcome(
    *, completed: bool = False, findings: tuple[Finding, ...] = ()
) -> PlaytestEpisodeOutcomeV1:
    return PlaytestEpisodeOutcomeV1(
        action_trace=(
            {"action": {"kind": "observe"}, "last_action_result": "observed", "tick": 0},
        ),
        defect_findings=findings,
        completed=completed,
    )


def _unreachable_finding() -> Finding:
    return Finding(
        id="playtest-unreachable:npc:lincheng",
        source="playtest",
        producer_id="playtest.grounding",
        producer_run_id="playtest.grounding",
        oracle_type="deterministic",
        defect_class="unreachable_target",
        severity="major",
        snapshot_id=_HEX,
        entities=["npc:lincheng"],
        status="confirmed",
        message="target is unreachable per the deterministic nav oracle",
    )


def _trace_of(store: FakeArtifactStore, outcome) -> PlaytestTraceV1:
    primary = outcome.artifacts[outcome.primary_index]
    return PlaytestTraceV1.model_validate(json.loads(store.read_prepared(primary.object_ref)))


# ============================================================ real-agent integration
def test_playtest_publishes_trace_driving_the_real_agent() -> None:
    store = _store()
    outcome = _real_handler(store)(_context(FakeModelBridge(responses=())))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "playtest_completed"
    assert outcome.summary.prepared_domain_artifact_count == 1
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "playtest_trace"
    assert primary.payload_schema_id == "playtest-trace@1"

    # version tuple: producer-local seed + inherited ir/constraint/env.
    assert primary.version_tuple.seed == 11
    assert primary.version_tuple.constraint_snapshot_id == CONSTRAINT_ID
    assert primary.version_tuple.env_contract_version == ENV_CONTRACT_VERSION
    assert primary.version_tuple.tool_version == "playtest@1"

    trace = _trace_of(store, outcome)
    assert trace.config_artifact_id == CONFIG_ID
    assert trace.constraint_snapshot_artifact_id == CONSTRAINT_ID
    assert trace.task_suite_artifact_id == SUITE_ID
    assert trace.environment_profile == ENV_PROFILE
    assert trace.planner_policy == PLANNER_POLICY
    assert trace.env_contract_version == ENV_CONTRACT_VERSION
    assert trace.interaction_mode == "autonomous"
    assert trace.seed == 11
    assert len(trace.episodes) == 1
    episode = trace.episodes[0]
    assert episode.episode_id == "episode:01"
    assert episode.scenario_spec_artifact_id == SCENARIO_01
    assert episode.seed == derive_episode_seed(11, "episode:01")
    # DETERMINISTIC verdict (caravan quest never completed under the empty replay).
    assert episode.completed is False
    assert 1 <= len(episode.action_trace) <= 3
    assert all(rec.action["kind"] == "observe" for rec in episode.action_trace)


def test_playtest_real_agent_replay_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _real_handler(store_a)(_context(FakeModelBridge(responses=())))
    out_b = _real_handler(store_b)(_context(FakeModelBridge(responses=())))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]


# ============================================================ fake-runner behaviours
def test_playtest_projects_findings_under_playtest_policy() -> None:
    store = _store()
    runner = _FakeRunner(_observe_outcome(findings=(_unreachable_finding(),)))
    outcome = PlaytestRunHandler(blobs=store, store=store, env_runner=runner)(
        _context(FakeModelBridge(responses=()))
    )
    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.prepared_finding_count == 1
    finding = outcome.findings[0]
    assert finding.payload.source == "playtest"
    assert finding.payload.oracle_type == "deterministic"
    assert finding.payload.producer_run_id == "run:1"
    assert finding.evidence_artifact_index == 0  # evidences the trace


def test_playtest_runs_only_selected_episode_subset() -> None:
    suite = _suite(
        _episode("episode:01", SCENARIO_01, "caravan"),
        _episode("episode:02", SCENARIO_02, "caravan-2"),
    )
    store = _store(suite, scenarios=(SCENARIO_01, SCENARIO_02))
    # payload selects ONLY episode:02.
    payload = _payload(
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id="episode:02", scenario_spec_artifact_id=SCENARIO_02
            ),
        ),
    )
    runner = _FakeRunner(_observe_outcome())
    outcome = PlaytestRunHandler(blobs=store, store=store, env_runner=runner)(
        _context(FakeModelBridge(responses=()), payload)
    )
    assert len(runner.calls) == 1  # only the selected episode ran
    trace = _trace_of(store, outcome)
    assert [e.episode_id for e in trace.episodes] == ["episode:02"]
    assert [e.scenario_spec_artifact_id for e in trace.episodes] == [SCENARIO_02]


def test_playtest_unknown_environment_is_typed_unavailable() -> None:
    store = _store()
    runner = _FakeRunner(_observe_outcome(), supported=False)
    outcome = PlaytestRunHandler(blobs=store, store=store, env_runner=runner)(
        _context(FakeModelBridge(responses=()))
    )
    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "playtest_environment_unavailable"
    assert outcome.failure_class == "permanent_dependency"
    assert outcome.intrinsic_retry_eligible is False
    assert outcome.artifacts == ()
    assert outcome.dependency is not None
    assert outcome.dependency.dependency_kind == "game_environment"
    assert runner.calls == []  # no episode / LLM ran


@pytest.mark.parametrize(
    "mutation, match",
    [
        ("suite_config", "task-suite config"),
        ("suite_constraint", "task-suite constraint"),
        ("suite_env", "task-suite environment"),
        ("scenario_config", "scenario config"),
        ("scenario_env_contract", "scenario env-contract"),
        ("episode_scenario", "scenario binding is stale"),
        ("episode_missing", "not in the task suite"),
    ],
)
def test_playtest_rejects_stale_bindings(mutation: str, match: str) -> None:
    suite = _default_suite()
    payload = _payload()
    store = FakeArtifactStore()
    store.register(PREVIEW_ID, _preview_bytes())

    scenario = _scenario("caravan")
    if mutation == "suite_config":
        suite = suite.model_copy(update={"config_export_artifact_id": "artifact:other"})
    elif mutation == "suite_constraint":
        suite = suite.model_copy(update={"constraint_snapshot_artifact_id": "artifact:other"})
    elif mutation == "suite_env":
        suite = suite.model_copy(
            update={"environment_profile": ProfileRefV1(profile_id="env:x", version=1)}
        )
    elif mutation == "scenario_config":
        scenario = scenario.model_copy(update={"config_export_artifact_id": "artifact:other"})
    elif mutation == "scenario_env_contract":
        scenario = scenario.model_copy(update={"env_contract_version": "other-env@9"})
    elif mutation == "episode_scenario":
        payload = _payload(
            episodes=(
                PlaytestEpisodeBindingV1(
                    episode_id="episode:01", scenario_spec_artifact_id="artifact:wrong"
                ),
            ),
        )
    elif mutation == "episode_missing":
        payload = _payload(
            episodes=(
                PlaytestEpisodeBindingV1(
                    episode_id="episode:99", scenario_spec_artifact_id=SCENARIO_01
                ),
            ),
        )
    store.register(SUITE_ID, suite.model_dump(mode="json"))
    store.register(SCENARIO_01, scenario.model_dump(mode="json"))

    runner = _FakeRunner(_observe_outcome())
    handler = PlaytestRunHandler(blobs=store, store=store, env_runner=runner)
    with pytest.raises(ValueError, match=match):
        handler(_context(FakeModelBridge(responses=()), payload))


def test_playtest_rejects_stale_profile_binding() -> None:
    store = _store()
    runner = _FakeRunner(_observe_outcome())
    handler = PlaytestRunHandler(blobs=store, store=store, env_runner=runner)
    context = build_context(
        params=_payload(),
        kind=PLAYTEST_KIND,
        seed=11,
        resolved_profiles=(
            resolved_binding(
                "/params/environment_profile",
                profile_id="environment:WRONG",
                version=1,
                kind="environment",
            ),
            resolved_binding(
                "/params/planner_policy",
                profile_id="planner:layered",
                version=1,
                kind="playtest_planner",
            ),
        ),
        llm_execution_mode="not_applicable",
        model_bridge=FakeModelBridge(responses=()),
    )
    with pytest.raises(ValueError, match="stale environment profile binding"):
        handler(context)


def test_playtest_requires_a_seeded_payload() -> None:
    store = _store()
    runner = _FakeRunner(_observe_outcome())
    handler = PlaytestRunHandler(blobs=store, store=store, env_runner=runner)
    context = build_context(
        params=_payload(),
        kind=PLAYTEST_KIND,
        seed=None,
        resolved_profiles=(
            resolved_binding(
                "/params/environment_profile",
                profile_id="environment:aureus",
                version=1,
                kind="environment",
            ),
            resolved_binding(
                "/params/planner_policy",
                profile_id="planner:layered",
                version=1,
                kind="playtest_planner",
            ),
        ),
        llm_execution_mode="not_applicable",
        model_bridge=FakeModelBridge(responses=()),
    )
    with pytest.raises(ValueError, match="seeded"):
        handler(context)


def test_playtest_rejects_disallowed_bounded_interaction_command() -> None:
    store = _store()
    # A combat action is outside the bounded_choice interactive command set.
    attack_outcome = PlaytestEpisodeOutcomeV1(
        action_trace=(
            {
                "action": {"kind": "attack", "target_id": "m"},
                "last_action_result": "hit",
                "tick": 1,
            },
        ),
        defect_findings=(),
        completed=False,
    )
    runner = _FakeRunner(attack_outcome)
    handler = PlaytestRunHandler(blobs=store, store=store, env_runner=runner)
    with pytest.raises(ValueError, match="not an allowed bounded interaction command"):
        handler(
            _context(FakeModelBridge(responses=()), _payload(interaction_mode="bounded_choice"))
        )

    # The same combat action IS allowed in autonomous mode.
    outcome = PlaytestRunHandler(blobs=store, store=store, env_runner=_FakeRunner(attack_outcome))(
        _context(FakeModelBridge(responses=()), _payload(interaction_mode="autonomous"))
    )
    assert isinstance(outcome, PreparedRunResult)


# ============================================================ lineage conformance
def test_playtest_run_input_lineage_is_dangling_free() -> None:
    store = _store()
    outcome = PlaytestRunHandler(
        blobs=store, store=store, env_runner=_FakeRunner(_observe_outcome())
    )(_context(FakeModelBridge(responses=())))
    registry = build_builtin_registry()
    definition = registry.get_run_kind(PLAYTEST_KIND)
    assert definition is not None
    policy = next(p for p in definition.outcome_policies if p.outcome_code == "playtest_completed")
    rule = next(r for r in policy.artifact_rules if r.artifact_kind == "playtest_trace")
    lineage_policy = registry.get_lineage_policy(rule.lineage_policy_ref)
    assert lineage_policy is not None

    run_inputs = {
        CONFIG_ID: ParentInfo(
            artifact_id=CONFIG_ID,
            kind="config_export",
            payload_schema_id="config-export-package@1",
            version_tuple=VersionTuple(),
        ),
        CONSTRAINT_ID: ParentInfo(
            artifact_id=CONSTRAINT_ID,
            kind="constraint_snapshot",
            payload_schema_id="constraint-snapshot@1",
            version_tuple=VersionTuple(),
        ),
        SUITE_ID: ParentInfo(
            artifact_id=SUITE_ID,
            kind="task_suite",
            payload_schema_id="task-suite@1",
            version_tuple=VersionTuple(),
        ),
    }
    sources = LineageParentSources(
        run_inputs=run_inputs, run_intermediates={}, prepared_siblings={}
    )

    primary = outcome.artifacts[outcome.primary_index]
    # ONLY config/constraint/task_suite are handler-declared; the selected_scenarios
    # siblings are the Task-18 publisher-injection carry.
    assert set(primary.lineage) == {CONFIG_ID, CONSTRAINT_ID, SUITE_ID}
    for parent_id in primary.lineage:
        matched = [
            r.parent_role
            for r in lineage_policy.parent_rules
            if _candidate_for_rule(parent_id, rule=r, sources=sources) is not None
        ]
        assert matched, f"lineage parent {parent_id!r} matches no typed role"
