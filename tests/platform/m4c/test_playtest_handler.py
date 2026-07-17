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
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import pytest

from gameforge.apps.worker.completion_oracles import (
    ALL_QUESTS_COMPLETED_ORACLE,
    build_completion_oracle_executors,
)
from gameforge.apps.worker.playtest import (
    AureusPlaytestRunner,
    build_playtest_planner_config_resolver,
)
from gameforge.apps.worker.components import _playtest_supported_profiles
from gameforge.contracts.canonical import canonical_json, canonical_sha256, sha256_lowerhex
from gameforge.contracts.config_export import (
    ConfigExportFileV1,
    ConfigExportPackageV1,
    canonical_config_export_bytes,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    EnvironmentContractDescriptorV1,
    PlaytestPlannerProfileConfigV2,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    execution_profile_payload_hash,
)
from gameforge.contracts.findings import Finding
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    PlaytestEpisodeBindingV1,
    PlaytestRunPayloadV1,
    PreparedRunFailure,
    PreparedRunResult,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.model_router import Message, ModelRequest
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
from gameforge.platform.playtest_payload_schemas import (
    PlaytestPayloadValidationService,
    build_builtin_playtest_payload_validators,
)
from gameforge.platform.registry.defaults import _failure_classifier, build_builtin_registry
from gameforge.platform.run_handlers.playtest import (
    PlaytestEpisodeOutcomeV1,
    PlaytestEpisodeRunRequest,
    PlaytestRunHandler,
    derive_episode_seed,
)
from gameforge.platform.runs.lifecycle import validate_prepared_failure
from gameforge.spine.ir.loader import load_scenario
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
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
ENV_CONTRACT_VERSION = "generic-agent-env@1"

_ENV_CONTRACT = EnvironmentContractDescriptorV1(
    env_contract_version=ENV_CONTRACT_VERSION,
    reset_schema_id="generic-env-reset@1",
    action_schema_id="generic-env-action@1",
    observation_schema_id="generic-env-observation@1",
    max_navigation_grid_cells=65_536,
)

_HEX = "a" * 64
_STATE_0 = f"sha256:{'0' * 64}"
_STATE_1 = f"sha256:{'1' * 64}"
_STATE_2 = f"sha256:{'2' * 64}"


def _planner_config(*, memory_mode: str = "off", **updates: int) -> PlaytestPlannerProfileConfigV2:
    values = {
        "memory_mode": memory_mode,
        "max_episode_count": 32,
        "max_steps_per_episode": 512,
        "max_total_steps": 512,
        "max_total_model_calls": 3_072,
        "max_total_trace_bytes": 64 * 1024 * 1024,
        **updates,
    }
    return PlaytestPlannerProfileConfigV2.model_validate(values)


def _planner_config_resolver(_binding) -> PlaytestPlannerProfileConfigV2:
    return _planner_config()


# --------------------------------------------------------------------------- data
def _preview_bytes() -> bytes:
    snapshot = load_scenario("scenarios/caravan.yaml")
    return canonical_json(snapshot.content_payload).encode("utf-8")


def _config_bytes(
    *,
    source_preview_artifact_id: str = PREVIEW_ID,
    constraint_snapshot_artifact_id: str = CONSTRAINT_ID,
    environment_profile: ProfileRefV1 = ENV_PROFILE,
    grid_size: tuple[int, int] | None = None,
    add_extra_quest: bool = False,
) -> bytes:
    snapshot = load_scenario("scenarios/caravan.yaml")
    workbook = AureusCsvAdapter().from_ir(snapshot)
    if grid_size is not None:
        region = workbook["regions"][0]
        region["grid"] = {
            **region["grid"],
            "width": grid_size[0],
            "height": grid_size[1],
            "blocked": [],
        }
    if add_extra_quest:
        workbook.setdefault("npcs", []).append(
            {"npc_id": "npc:extra", "name": "Extra", "pos": [1, 1]}
        )
        workbook.setdefault("quests", []).append(
            {"quest_id": "quest:extra", "giver": "npc:extra", "reward": {}}
        )
        workbook.setdefault("quest_steps", []).append(
            {
                "step_id": "step:extra",
                "quest_id": "quest:extra",
                "kind": "talk",
                "target": "npc:extra",
                "order": 0,
            }
        )
    files = []
    for sheet, rows in workbook.items():
        content = canonical_json(rows).encode("utf-8")
        files.append(
            ConfigExportFileV1(
                relative_path=f"{sheet}.json",
                media_type="application/json",
                content_sha256=sha256_lowerhex(content),
                size_bytes=len(content),
                content_bytes=content,
            )
        )
    return canonical_config_export_bytes(
        ConfigExportPackageV1(
            export_profile=ProfileRefV1(profile_id="export:aureus", version=1),
            target_environment_profile=environment_profile,
            env_contract_version=ENV_CONTRACT_VERSION,
            source_preview_artifact_id=source_preview_artifact_id,
            constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
            format_schema_id="config-export-files@1",
            files=tuple(files),
        )
    )


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


def _reset(
    scenario_id: str,
    *,
    start_seed: int = 0,
    quest_ids: tuple[str, ...] = (),
) -> ScenarioResetBindingV1:
    value = {
        "scenario_id": scenario_id,
        "config_export_artifact_id": CONFIG_ID,
        "quest_ids": list(quest_ids),
        "start_seed": start_seed,
    }
    return ScenarioResetBindingV1(
        reset_schema_id=_ENV_CONTRACT.reset_schema_id,
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
    store.register(CONFIG_ID, _config_bytes())
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
        version_tuple=VersionTuple(
            ir_snapshot_id=load_scenario("scenarios/caravan.yaml").snapshot_id,
            constraint_snapshot_id=CONSTRAINT_ID,
            env_contract_version=ENV_CONTRACT_VERSION,
            seed=11,
        ),
    )


def _test_environment_contract(profile: ProfileRefV1) -> EnvironmentContractDescriptorV1:
    if profile != ENV_PROFILE:
        raise KeyError(profile)
    return _ENV_CONTRACT


def _real_runner(*, supported: tuple[ProfileRefV1, ...] = (ENV_PROFILE,)) -> AureusPlaytestRunner:
    registry = build_builtin_registry()
    return AureusPlaytestRunner(
        oracle_registry=_oracle_registry(),
        oracle_executors=build_completion_oracle_executors(),
        supported_profiles=frozenset(supported),
        environment_contract_resolver=_test_environment_contract,
        payload_validator=PlaytestPayloadValidationService(
            registry=registry,
            validators=build_builtin_playtest_payload_validators(),
        ),
    )


def _real_handler(
    store: FakeArtifactStore, runner: AureusPlaytestRunner | None = None
) -> PlaytestRunHandler:
    return PlaytestRunHandler(
        blobs=store,
        store=store,
        env_runner=runner or _real_runner(),
        planner_config_resolver=_planner_config_resolver,
    )


def _handler(store: FakeArtifactStore, runner) -> PlaytestRunHandler:
    return PlaytestRunHandler(
        blobs=store,
        store=store,
        env_runner=runner,
        planner_config_resolver=_planner_config_resolver,
    )


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


@dataclass
class _TwoCallPerEpisodeRunner:
    """Exercise the shared router without carrying Agent state across episodes."""

    calls: list[PlaytestEpisodeRunRequest] = field(default_factory=list)

    def supports(self, environment_profile: ProfileRefV1) -> bool:
        del environment_profile
        return True

    def run_episode(self, request: PlaytestEpisodeRunRequest) -> PlaytestEpisodeOutcomeV1:
        self.calls.append(request)
        for node_id in ("playtest.planner", "playtest.executor"):
            request.router.call(
                ModelRequest(
                    model_snapshot=request.router.default_model_snapshot,
                    messages=[Message(role="user", content=f"{request.scenario_id}:{node_id}")],
                    params={},
                    agent_node_id=node_id,
                    prompt_version="p@1",
                )
            )
        return _observe_outcome()


def _observe_outcome(
    *, completed: bool = False, findings: tuple[Finding, ...] = ()
) -> PlaytestEpisodeOutcomeV1:
    return PlaytestEpisodeOutcomeV1(
        action_trace=(
            {
                "action": {"kind": "observe"},
                "last_action_result": "observed",
                "tick": 0,
                "state_hash": _STATE_1,
            },
        ),
        defect_findings=findings,
        completed=completed,
        initial_state_hash=_STATE_0,
        final_state_hash=_STATE_1,
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
        snapshot_id=_STATE_1,
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
    expected_seed = derive_episode_seed(
        root_seed=11,
        run_kind=PLAYTEST_KIND,
        environment_profile=ENV_PROFILE,
        task_suite_artifact_id=SUITE_ID,
        episode_id="episode:01",
    )
    assert episode.seed == expected_seed
    assert episode.seed_binding.model_dump(mode="json") == {
        "seed_derivation_version": "subseed@1",
        "root_seed": 11,
        "run_kind": PLAYTEST_KIND.model_dump(mode="json"),
        "profile": ENV_PROFILE.model_dump(mode="json"),
        "case_id": f"{SUITE_ID}:episode:01",
        "replication_index": 0,
        "seed": expected_seed,
    }
    # DETERMINISTIC verdict (caravan quest never completed under the empty replay).
    assert episode.completed is False
    assert episode.execution_step_limit == 3
    assert episode.terminal_reason == "step_limit_exhausted"
    assert episode.initial_state_hash.startswith("sha256:")
    assert episode.final_state_hash == episode.action_trace[-1].state_hash
    assert any(marker.kind == "step_limit" for marker in episode.markers)
    assert 1 <= len(episode.action_trace) <= 3
    assert all(rec.action["kind"] == "observe" for rec in episode.action_trace)
    assert trace.requested_max_steps_per_episode == 3
    assert trace.planner_memory_mode == "off"
    assert trace.execution_envelope.total_action_count == len(episode.action_trace)
    assert trace.execution_envelope.actual_model_calls <= (
        trace.execution_envelope.model_call_upper_bound
    )
    assert outcome.summary.prepared_finding_count == 1
    assert outcome.findings[0].payload.defect_class == "playtest_incomplete"


def test_playtest_real_agent_replay_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _real_handler(store_a)(_context(FakeModelBridge(responses=())))
    out_b = _real_handler(store_b)(_context(FakeModelBridge(responses=())))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]


def test_playtest_resolves_the_suite_exact_oracle_registry_not_process_default() -> None:
    exact = _oracle_registry()
    wrong = exact.model_copy(
        update={"registry_version": exact.registry_version + 1, "registry_digest": "f" * 64}
    )
    runner = replace(
        _real_runner(),
        oracle_registry=wrong,
        oracle_registry_resolver=lambda ref: exact if ref == _registry_ref() else None,
    )

    outcome = _real_handler(_store(), runner)(_context(FakeModelBridge(responses=())))

    assert isinstance(outcome, PreparedRunResult)


def test_playtest_rejects_non_boolean_completion_oracle_result() -> None:
    runner = replace(
        _real_runner(),
        oracle_executors={
            "state_predicate_oracle@1": SimpleNamespace(evaluate=lambda _env, _params: "false"),
            "bounded_progress_oracle@1": build_completion_oracle_executors()[
                "bounded_progress_oracle@1"
            ],
        },
    )

    with pytest.raises(IntegrityViolation, match="non-boolean verdict"):
        _real_handler(_store(), runner)(_context(FakeModelBridge(responses=())))


# ============================================================ fake-runner behaviours
def test_playtest_projects_findings_under_playtest_policy() -> None:
    store = _store()
    runner = _FakeRunner(_observe_outcome(completed=True, findings=(_unreachable_finding(),)))
    outcome = _handler(store, runner)(_context(FakeModelBridge(responses=())))
    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.prepared_finding_count == 1
    finding = outcome.findings[0]
    assert finding.payload.source == "playtest"
    assert finding.payload.oracle_type == "deterministic"
    assert finding.payload.producer_run_id == "run:1"
    assert (
        finding.payload.snapshot_id
        == _context(FakeModelBridge(responses=())).payload.version_tuple.ir_snapshot_id
    )
    assert finding.payload.evidence["occurrences"][0]["runtime_state_hash"] == _STATE_1
    assert finding.evidence_artifact_index == 0  # evidences the trace


def test_playtest_finding_uses_the_exact_current_series_head() -> None:
    store = _store()
    runner = _FakeRunner(_observe_outcome(completed=True, findings=(_unreachable_finding(),)))
    handler = replace(
        _handler(store, runner),
        finding_head_revision=lambda _finding_id: 7,
    )

    outcome = handler(_context(FakeModelBridge(responses=())))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.findings[0].expected_previous_revision == 7


def test_playtest_scopes_same_finding_series_per_episode() -> None:
    suite = _suite(
        _episode("episode:01", SCENARIO_01, "caravan"),
        _episode("episode:02", SCENARIO_02, "caravan-2"),
    )
    store = _store(suite, scenarios=(SCENARIO_01, SCENARIO_02))
    payload = _payload(
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id="episode:01", scenario_spec_artifact_id=SCENARIO_01
            ),
            PlaytestEpisodeBindingV1(
                episode_id="episode:02", scenario_spec_artifact_id=SCENARIO_02
            ),
        )
    )
    runner = _FakeRunner(_observe_outcome(completed=True, findings=(_unreachable_finding(),)))

    outcome = _handler(store, runner)(_context(FakeModelBridge(responses=()), payload))

    assert isinstance(outcome, PreparedRunResult)
    assert len(outcome.findings) == 2
    assert len({finding.finding_id for finding in outcome.findings}) == 2
    assert all(finding.evidence_artifact_index == 0 for finding in outcome.findings)


def test_playtest_merges_duplicate_finding_series_before_publication_cas() -> None:
    store = _store()
    first = _unreachable_finding()
    second = first.model_copy(update={"snapshot_id": _STATE_2})
    runner = _FakeRunner(_observe_outcome(completed=True, findings=(first, second)))

    outcome = _handler(store, runner)(_context(FakeModelBridge(responses=())))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.prepared_finding_count == 1
    occurrences = outcome.findings[0].payload.evidence["occurrences"]
    assert {item["runtime_state_hash"] for item in occurrences} == {_STATE_1, _STATE_2}


def test_playtest_rejects_aggregate_request_above_exact_planner_profile() -> None:
    suite = _suite(
        _episode("episode:01", SCENARIO_01, "caravan"),
        _episode("episode:02", SCENARIO_02, "caravan-2"),
    )
    store = _store(suite, scenarios=(SCENARIO_01, SCENARIO_02))
    payload = _payload(
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id="episode:01", scenario_spec_artifact_id=SCENARIO_01
            ),
            PlaytestEpisodeBindingV1(
                episode_id="episode:02", scenario_spec_artifact_id=SCENARIO_02
            ),
        )
    )
    runner = _FakeRunner(_observe_outcome())
    handler = PlaytestRunHandler(
        blobs=store,
        store=store,
        env_runner=runner,
        planner_config_resolver=lambda _binding: _planner_config(max_episode_count=1),
    )

    with pytest.raises(IntegrityViolation, match="planner profile authority"):
        handler(_context(FakeModelBridge(responses=()), payload))
    assert runner.calls == []


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
    outcome = _handler(store, runner)(_context(FakeModelBridge(responses=()), payload))
    assert len(runner.calls) == 1  # only the selected episode ran
    trace = _trace_of(store, outcome)
    assert [e.episode_id for e in trace.episodes] == ["episode:02"]
    assert [e.scenario_spec_artifact_id for e in trace.episodes] == [SCENARIO_02]


def test_playtest_executes_exact_config_and_reset_without_hidden_preview_read() -> None:
    store = _store()
    runner = _FakeRunner(_observe_outcome())

    outcome = _handler(store, runner)(_context(FakeModelBridge(responses=())))

    assert isinstance(outcome, PreparedRunResult)
    assert len(runner.calls) == 1
    request = runner.calls[0]
    assert request.config_export.source_preview_artifact_id == PREVIEW_ID
    assert request.config_export.constraint_snapshot_artifact_id == CONSTRAINT_ID
    assert request.config_export.target_environment_profile == ENV_PROFILE
    assert request.reset_binding == _scenario("caravan").reset_binding


def test_aureus_runner_applies_reset_quest_subset_to_exported_world() -> None:
    reset = _reset("caravan", quest_ids=("quest:missing_caravan",))
    episode = _episode("episode:01", SCENARIO_01, "caravan").model_copy(
        update={"reset_binding": reset}
    )
    suite = _suite(episode)
    scenario = _scenario("caravan").model_copy(update={"reset_binding": reset})
    store = _store(suite)
    store.register(CONFIG_ID, _config_bytes(add_extra_quest=True))
    store.register(SCENARIO_01, scenario.model_dump(mode="json"))
    fake = _FakeRunner(_observe_outcome())
    _handler(store, fake)(_context(FakeModelBridge(responses=())))

    world = _real_runner()._world_from_request(fake.calls[0])

    assert [quest.quest_id for quest in world.quests] == ["quest:missing_caravan"]


def test_playtest_resets_prior_response_causality_between_ordered_episodes() -> None:
    suite = _suite(
        _episode("episode:01", SCENARIO_01, "caravan"),
        _episode("episode:02", SCENARIO_02, "caravan-2"),
    )
    store = _store(suite, scenarios=(SCENARIO_01, SCENARIO_02))
    payload = _payload(
        episodes=(
            PlaytestEpisodeBindingV1(
                episode_id="episode:01", scenario_spec_artifact_id=SCENARIO_01
            ),
            PlaytestEpisodeBindingV1(
                episode_id="episode:02", scenario_spec_artifact_id=SCENARIO_02
            ),
        )
    )
    bridge = FakeModelBridge(responses=("{}", "{}", "{}", "{}"))
    runner = _TwoCallPerEpisodeRunner()

    outcome = _handler(store, runner)(_context(bridge, payload))

    assert isinstance(outcome, PreparedRunResult)
    assert len(runner.calls) == 2
    assert [request.idempotency_key for request in bridge.requests] == [
        "model:1",
        "model:2",
        "model:3",
        "model:4",
    ]
    assert [request.prompt_context.include_previous_consumption for request in bridge.requests] == [
        False,
        True,
        False,
        True,
    ]


def test_playtest_unknown_environment_is_typed_unavailable() -> None:
    store = _store()
    runner = _FakeRunner(_observe_outcome(), supported=False)
    context = _context(FakeModelBridge(responses=()))
    outcome = _handler(store, runner)(context)
    assert isinstance(outcome, PreparedRunFailure)
    # The cause MUST be a frozen classifier cause (permanent_dependency family),
    # NOT a non-frozen domain code — otherwise the run boundary rejects it.
    assert outcome.cause_code == "permanent_dependency_failed"
    assert outcome.failure_class == "permanent_dependency"
    assert outcome.intrinsic_retry_eligible is False
    assert outcome.artifacts == ()
    assert outcome.dependency is not None
    assert outcome.dependency.dependency_kind == "game_environment"
    assert outcome.dependency.classifier_code == "permanent_dependency_failed"
    assert runner.calls == []  # no episode / LLM ran

    # The failure the handler emits is genuinely PUBLISHABLE: it survives the run
    # boundary's exact-classifier validation against the frozen classifier (this is
    # what catches a "constructed-but-unpublishable" cause code, unlike DTO checks).
    classifier = _failure_classifier()
    assert any(rule.cause_code == outcome.cause_code for rule in classifier.rules)
    validate_prepared_failure(
        run=context.run,
        attempt=context.attempt,
        prepared=outcome,
        classifier=classifier,
    )


def test_aureus_composition_advertises_only_environment_profiles() -> None:
    supported = _playtest_supported_profiles(build_builtin_registry())

    assert ProfileRefV1(profile_id="builtin.environment", version=1) in supported
    assert ProfileRefV1(profile_id="builtin.playtest_planner", version=1) not in supported


def test_playtest_config_is_resolved_from_exact_planner_profile_binding() -> None:
    registry = build_builtin_registry()
    catalog = max(
        registry.list_execution_profile_catalogs(),
        key=lambda item: item.catalog_version,
    )
    retained = next(
        item
        for item in catalog.definitions
        if item.profile_kind == "playtest_planner" and item.profile.version == 2
    )
    binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/planner_policy",
        profile=retained.profile,
        expected_profile_kind="playtest_planner",
        profile_payload_hash=execution_profile_payload_hash(retained),
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    )
    resolver = build_playtest_planner_config_resolver(registry)

    config = resolver(binding)
    assert config.memory_mode == "off"
    assert config.max_total_steps == 1_024
    with pytest.raises(IntegrityViolation, match="exact profile binding"):
        resolver(binding.model_copy(update={"profile_payload_hash": "f" * 64}))


@pytest.mark.parametrize(
    "mutation, match",
    [
        ("suite_config", "task-suite config"),
        ("suite_constraint", "task-suite constraint"),
        ("suite_env", "task-suite environment"),
        ("scenario_config", "scenario config"),
        ("scenario_env_contract", "scenario env-contract"),
        ("scenario_reset", "scenario reset"),
        ("episode_scenario", "scenario binding is stale"),
        ("episode_missing", "not in the task suite"),
    ],
)
def test_playtest_rejects_stale_bindings(mutation: str, match: str) -> None:
    suite = _default_suite()
    payload = _payload()
    store = FakeArtifactStore()
    store.register(PREVIEW_ID, _preview_bytes())
    store.register(CONFIG_ID, _config_bytes())

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
    elif mutation == "scenario_reset":
        scenario = scenario.model_copy(update={"reset_binding": _reset("scenario:other")})
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
    handler = _handler(store, runner)
    with pytest.raises(ValueError, match=match):
        handler(_context(FakeModelBridge(responses=()), payload))


def test_playtest_rejects_max_steps_above_selected_episode_budget() -> None:
    store = _store()
    handler = _handler(store, _FakeRunner(_observe_outcome()))

    with pytest.raises(ValueError, match="step budget"):
        handler(_context(FakeModelBridge(responses=()), _payload(max_steps=251)))


def test_playtest_rejects_stale_profile_binding() -> None:
    store = _store()
    runner = _FakeRunner(_observe_outcome())
    handler = _handler(store, runner)
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
    handler = _handler(store, runner)
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


@pytest.mark.parametrize(
    ("config_bytes", "match"),
    (
        (_config_bytes(source_preview_artifact_id="artifact:other"), "preview binding"),
        (_config_bytes(constraint_snapshot_artifact_id="artifact:other"), "constraint binding"),
        (
            _config_bytes(
                environment_profile=ProfileRefV1(profile_id="environment:other", version=1)
            ),
            "environment binding",
        ),
    ),
)
def test_playtest_rejects_stale_config_package(config_bytes: bytes, match: str) -> None:
    store = _store()
    store.register(CONFIG_ID, config_bytes)
    handler = _handler(store, _FakeRunner(_observe_outcome()))

    with pytest.raises(ValueError, match=match):
        handler(_context(FakeModelBridge(responses=())))


def test_playtest_rejects_world_above_environment_grid_authority() -> None:
    store = _store()
    store.register(CONFIG_ID, _config_bytes(grid_size=(257, 256)))

    with pytest.raises(IntegrityViolation, match="navigation-grid authority"):
        _real_handler(store)(_context(FakeModelBridge(responses=())))


def test_playtest_rejects_reset_seed_outside_root_subseed_authority() -> None:
    reset = _reset("caravan", start_seed=1)
    episode = _episode("episode:01", SCENARIO_01, "caravan").model_copy(
        update={"reset_binding": reset}
    )
    suite = _suite(episode)
    scenario = _scenario("caravan").model_copy(update={"reset_binding": reset})
    store = _store(suite)
    store.register(SCENARIO_01, scenario.model_dump(mode="json"))

    with pytest.raises(IntegrityViolation, match="exact schema"):
        _real_handler(store)(_context(FakeModelBridge(responses=())))


def test_playtest_rejects_env_trace_beyond_requested_step_limit() -> None:
    store = _store()
    outcome = PlaytestEpisodeOutcomeV1(
        action_trace=(
            {
                "action": {"kind": "observe"},
                "last_action_result": "ok",
                "tick": 0,
                "state_hash": _STATE_0,
            },
            {
                "action": {"kind": "observe"},
                "last_action_result": "ok",
                "tick": 1,
                "state_hash": _STATE_1,
            },
        ),
        defect_findings=(),
        completed=False,
        initial_state_hash=_STATE_0,
        final_state_hash=_STATE_1,
    )
    handler = _handler(store, _FakeRunner(outcome))

    with pytest.raises(ValueError, match="requested step limit"):
        handler(_context(FakeModelBridge(responses=()), _payload(max_steps=1)))


def test_playtest_trace_marks_stuck_authoritative_state() -> None:
    store = _store()
    steps = tuple(
        {
            "action": {"kind": "observe"},
            "last_action_result": "observed",
            "tick": index,
            "state_hash": _STATE_1,
        }
        for index in range(3)
    )
    outcome = _handler(
        store,
        _FakeRunner(
            PlaytestEpisodeOutcomeV1(
                action_trace=steps,
                defect_findings=(),
                completed=False,
                initial_state_hash=_STATE_0,
                final_state_hash=_STATE_1,
            )
        ),
    )(_context(FakeModelBridge(responses=()), _payload(max_steps=4)))

    episode = _trace_of(store, outcome).episodes[0]
    assert episode.terminal_reason == "agent_stopped"
    assert any(marker.kind == "stuck" and marker.step_index == 2 for marker in episode.markers)


@pytest.mark.parametrize(
    "step",
    (
        {"action": {"kind": "navigate_to"}, "last_action_result": "ok", "tick": 1},
        {
            "action": {"kind": "observe", "forged": True},
            "last_action_result": "ok",
            "tick": 1,
        },
        {"action": {"kind": "observe"}, "last_action_result": 7, "tick": 1},
        {"action": {"kind": "observe"}, "last_action_result": "ok", "tick": "1"},
    ),
)
def test_playtest_rejects_malformed_env_action_records(step: dict) -> None:
    store = _store()
    handler = PlaytestRunHandler(
        blobs=store,
        store=store,
        env_runner=_FakeRunner(
            PlaytestEpisodeOutcomeV1(
                action_trace=(step,),
                defect_findings=(),
                completed=False,
                initial_state_hash=_STATE_0,
                final_state_hash=_STATE_1,
            )
        ),
        planner_config_resolver=_planner_config_resolver,
    )

    with pytest.raises(ValueError, match="action record"):
        handler(_context(FakeModelBridge(responses=())))


def test_playtest_rejects_non_boolean_completion_verdict() -> None:
    store = _store()
    handler = PlaytestRunHandler(
        blobs=store,
        store=store,
        env_runner=_FakeRunner(
            PlaytestEpisodeOutcomeV1(
                action_trace=(),
                defect_findings=(),
                completed="false",  # type: ignore[arg-type]
                initial_state_hash=_STATE_0,
                final_state_hash=_STATE_0,
            )
        ),
        planner_config_resolver=_planner_config_resolver,
    )

    with pytest.raises(ValueError, match="boolean completion"):
        handler(_context(FakeModelBridge(responses=())))


def test_playtest_rejects_disallowed_bounded_interaction_command() -> None:
    store = _store()
    # A combat action is outside the bounded_choice interactive command set.
    attack_outcome = PlaytestEpisodeOutcomeV1(
        action_trace=(
            {
                "action": {"kind": "attack", "target_id": "m"},
                "last_action_result": "hit",
                "tick": 1,
                "state_hash": _STATE_1,
            },
        ),
        defect_findings=(),
        completed=False,
        initial_state_hash=_STATE_0,
        final_state_hash=_STATE_1,
    )
    runner = _FakeRunner(attack_outcome)
    handler = _handler(store, runner)
    with pytest.raises(ValueError, match="not an allowed bounded interaction command"):
        handler(
            _context(FakeModelBridge(responses=()), _payload(interaction_mode="bounded_choice"))
        )

    # The same combat action IS allowed in autonomous mode.
    outcome = _handler(store, _FakeRunner(attack_outcome))(
        _context(FakeModelBridge(responses=()), _payload(interaction_mode="autonomous"))
    )
    assert isinstance(outcome, PreparedRunResult)


# ============================================================ lineage conformance
def test_playtest_run_input_lineage_is_dangling_free() -> None:
    store = _store()
    outcome = _handler(store, _FakeRunner(_observe_outcome()))(
        _context(FakeModelBridge(responses=()))
    )
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
        SCENARIO_01: ParentInfo(
            artifact_id=SCENARIO_01,
            kind="scenario_spec",
            payload_schema_id="scenario-spec@1",
            version_tuple=VersionTuple(),
        ),
    }
    sources = LineageParentSources(
        run_inputs=run_inputs, run_intermediates={}, prepared_siblings={}
    )

    primary = outcome.artifacts[outcome.primary_index]
    assert set(primary.lineage) == {CONFIG_ID, CONSTRAINT_ID, SUITE_ID, SCENARIO_01}
    for parent_id in primary.lineage:
        matched = [
            r.parent_role
            for r in lineage_policy.parent_rules
            if _candidate_for_rule(parent_id, rule=r, sources=sources) is not None
        ]
        assert matched, f"lineage parent {parent_id!r} matches no typed role"
