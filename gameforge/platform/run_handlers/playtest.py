"""``playtest_runner@1`` — the environment-profile-selected playtest handler.

Drives the UNMODIFIED M2b ``gameforge.agents.playtest.agent.PlaytestAgent`` — a
Planner PROPOSES a subgoal, an Executor PROPOSES an atomic action, a Reflector
gives advisory hints, and the DETERMINISTIC game engine is the SOLE authority on
every ``done``/``completed`` verdict. ``platform`` cannot import ``gameforge.game``
or ``gameforge.agents``, so the agent-env drive (build the env, run the agent,
evaluate the completion oracle) is an INJECTED :class:`PlaytestEnvRunner` port whose
concrete Aureus impl lives in ``apps/worker`` (mirroring 11b's agent-runner and 12a's
scenario shaper). The four agent nodes route through the ordered multi-node bridge
router (:class:`MultiNodeBridgeRouter`), so an unmodified agent issues its LLM calls
on ONE ordered run-scoped cassette with the correct per-node frozen model snapshot.

The env verdict is DETERMINISTIC (the completion oracle / env terminal signal); the
LLM only plans/acts/reflects and NEVER decides completion. The handler returns ONLY a
valid ``PreparedRunOutcome``: ONE primary ``playtest_trace[playtest-trace@1]`` (frozen
``playtest-completed`` policy, defaults.py) + playtest-findings (source ``playtest``).
An UNKNOWN ``environment_profile`` returns a typed UNAVAILABLE ``PreparedRunFailure``
(``game_environment`` permanent dependency) — NOT a game-specific branch, NOT a crash.

Stale bindings fail closed: the suite / config / constraint / environment / profile /
seed the payload references must resolve to the exact input artifacts, and each
selected ``{episode_id, scenario_spec_artifact_id}`` binding must EXACTLY match a suite
episode. Only the SELECTED non-empty episode subset is run, and only allowed bounded
interaction commands are accepted.

Lineage: the trace declares ONLY the run_input parents (``config`` + ``constraint`` +
``task_suite``); the SELECTED-SCENARIO sibling parents are content-addressed and
injected by the Task-18 publisher enhancement (the same ownership split as 11b/12a).
The LLM ``source_rendered`` runtime parents are publisher-projected. ``seed`` is
producer-local; ir/constraint/env are inherited from the run inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import (
    DependencyFailureV1,
    PlaytestEpisodeBindingV1,
    PlaytestRunPayloadV1,
    PreparedRunFailure,
    PreparedRunOutcome,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.playtest import (
    CompletionOracleRefV1,
    CompletionOracleRegistryRefV1,
    PlaytestActionRecordV1,
    PlaytestEpisodeTraceV1,
    PlaytestTraceV1,
    ScenarioSpecV1,
    TaskEpisodeV1,
    TaskSuiteV1,
)
from gameforge.spine.ir.snapshot import Snapshot

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    FindingEvidence,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    load_json_blob,
    resolved_profile,
    store_prepared_artifact,
)
from gameforge.platform.run_handlers.model_routing import (
    MultiNodeBridgeRouter,
    build_multinode_bridge_router,
)
from gameforge.platform.run_handlers.readers import SnapshotLoader, load_snapshot

PLAYTEST_TOOL_VERSION = "playtest@1"
PLAYTEST_TRACE_SCHEMA_ID = "playtest-trace@1"
PLAYTEST_OUTCOME_CODE = "playtest_completed"

ENVIRONMENT_PROFILE_FIELD = "/params/environment_profile"
PLANNER_POLICY_FIELD = "/params/planner_policy"

# The four distinct M2b PlaytestAgent LLM nodes (agent.py / planner.py / executor.py /
# reflect.py). planner/executor/reflect are always potentially issued; the memory node
# is issued only when the planner_policy selects memory-on (default memory=None keeps
# the M2b byte-identical regression lock).
PLAYTEST_PLANNER_NODE = "playtest.planner"
PLAYTEST_EXECUTOR_NODE = "playtest.executor"
PLAYTEST_REFLECT_NODE = "playtest.reflect"
PLAYTEST_MEMORY_NODE = "playtest.memory"
PLAYTEST_CORE_NODE_IDS = (
    PLAYTEST_PLANNER_NODE,
    PLAYTEST_EXECUTOR_NODE,
    PLAYTEST_REFLECT_NODE,
)

# The bounded env-command vocabulary. ``bounded_choice`` restricts the trace to the
# interactive subset; ``autonomous`` accepts the full env action vocabulary. Any action
# outside the mode's allowlist is rejected fail-closed.
_ENV_ACTION_KINDS = frozenset(
    {
        "observe",
        "wait",
        "navigate_to",
        "interact",
        "pickup",
        "choose",
        "attack",
        "cast_skill",
        "use",
        "equip",
        "buy",
        "sell",
    }
)
_BOUNDED_CHOICE_ACTION_KINDS = frozenset(
    {"observe", "wait", "navigate_to", "interact", "pickup", "choose"}
)


def allowed_action_kinds(interaction_mode: str) -> frozenset[str]:
    """The allowed bounded interaction commands for an interaction mode."""

    if interaction_mode == "bounded_choice":
        return _BOUNDED_CHOICE_ACTION_KINDS
    return _ENV_ACTION_KINDS


def derive_episode_seed(base_seed: int, episode_id: str) -> int:
    """Deterministic per-episode subseed from the run seed + episode id (``subseed@1``).

    ``AureusEnv.reset`` varies only by seed (the world is fixed by the preview at
    construction), so a distinct per-episode subseed makes each selected episode a
    distinct, deterministic seeded playthrough.
    """

    digest = canonical_sha256({"base_seed": int(base_seed), "episode_id": episode_id})
    return int(digest[:16], 16)


@dataclass(frozen=True, slots=True)
class PlaytestEpisodeRunRequest:
    """Fully-resolved deterministic inputs for ONE selected episode's agent-env drive."""

    preview_snapshot: Snapshot
    environment_profile: ProfileRefV1
    scenario_id: str
    seed: int
    router: MultiNodeBridgeRouter
    use_planner: bool
    memory_enabled: bool
    max_steps: int
    completion_oracle: CompletionOracleRefV1
    completion_oracle_registry_ref: CompletionOracleRegistryRefV1


@dataclass(frozen=True, slots=True)
class PlaytestEpisodeOutcomeV1:
    """The deterministic result of ONE episode's agent-env drive.

    ``completed`` is the completion-oracle verdict (env terminal signal), never an LLM
    claim. ``action_trace`` is the raw M2b ``PlaytestReport.action_trace`` (a list of
    ``{action, last_action_result, tick}`` dicts); ``defect_findings`` are the spine
    Findings the deterministic verifier-grounding recorded.
    """

    action_trace: tuple[dict, ...]
    defect_findings: tuple[Finding, ...]
    completed: bool


class PlaytestEnvRunner(Protocol):
    """Drive the M2b PlaytestAgent against a profile-selected env (game-specific port)."""

    def supports(self, environment_profile: ProfileRefV1) -> bool: ...

    def run_episode(self, request: PlaytestEpisodeRunRequest) -> PlaytestEpisodeOutcomeV1: ...


# Decide (deterministically) whether a planner_policy selects the M2b MemTrace memory
# layer. Default OFF keeps the ``memory=None`` byte-identical regression lock.
MemorySelector = Callable[[ProfileRefV1], bool]


def _no_memory(_: ProfileRefV1) -> bool:
    return False


@dataclass(frozen=True, slots=True)
class PlaytestRunHandler:
    """A ``RunExecutor`` for ``playtest_runner@1`` (LLM, seeded, replayable)."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    env_runner: PlaytestEnvRunner
    snapshot_loader: SnapshotLoader = load_snapshot
    memory_selector: MemorySelector = field(default=_no_memory)
    use_planner: bool = True

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, PlaytestRunPayloadV1):
            raise TypeError("playtest_runner@1 requires a playtest-run@1 payload")
        envelope = context.payload
        if envelope.seed is None:
            raise ValueError("playtest_runner@1 requires a seeded Run payload (subseed@1)")
        base_seed = int(envelope.seed)

        environment_profile = resolved_profile(envelope, ENVIRONMENT_PROFILE_FIELD).profile
        planner_policy = resolved_profile(envelope, PLANNER_POLICY_FIELD).profile
        if environment_profile != payload.environment_profile:
            raise ValueError("stale environment profile binding")
        if planner_policy != payload.planner_policy:
            raise ValueError("stale planner policy binding")

        # UNKNOWN environment → typed unavailable, BEFORE any suite load or LLM call.
        if not self.env_runner.supports(environment_profile):
            return self._unavailable_failure(context, environment_profile)

        suite = self._load_suite(payload)
        self._validate_suite_bindings(suite, payload)
        selected = self._resolve_selected_episodes(suite, payload)

        use_memory = bool(self.memory_selector(planner_policy))
        router = self._build_router(context, use_memory)
        preview = self.snapshot_loader(self.blobs, suite.source_preview_artifact_id)
        allowed_kinds = allowed_action_kinds(payload.interaction_mode)

        episode_traces: list[PlaytestEpisodeTraceV1] = []
        findings_evidence: list[FindingEvidence] = []
        for binding, episode in selected:
            scenario = self._load_scenario(binding.scenario_spec_artifact_id)
            self._validate_scenario_bindings(scenario, suite, payload)
            episode_seed = derive_episode_seed(base_seed, episode.episode_id)
            max_steps = min(int(payload.max_steps_per_episode), int(episode.step_budget))
            outcome = self.env_runner.run_episode(
                PlaytestEpisodeRunRequest(
                    preview_snapshot=preview,
                    environment_profile=environment_profile,
                    scenario_id=scenario.scenario_id,
                    seed=episode_seed,
                    router=router,
                    use_planner=self.use_planner,
                    memory_enabled=use_memory,
                    max_steps=max_steps,
                    completion_oracle=episode.completion_oracle,
                    completion_oracle_registry_ref=suite.completion_oracle_registry_ref,
                )
            )
            episode_traces.append(
                PlaytestEpisodeTraceV1(
                    episode_id=episode.episode_id,
                    scenario_spec_artifact_id=binding.scenario_spec_artifact_id,
                    seed=episode_seed,
                    step_budget=int(episode.step_budget),
                    completion_oracle=episode.completion_oracle,
                    completed=bool(outcome.completed),
                    action_trace=_records(outcome.action_trace, allowed_kinds),
                )
            )
            findings_evidence.extend(
                FindingEvidence(finding=finding, evidence_artifact_index=0)
                for finding in outcome.defect_findings
            )

        trace = PlaytestTraceV1(
            config_artifact_id=payload.config_artifact_id,
            constraint_snapshot_artifact_id=payload.constraint_snapshot_artifact_id,
            task_suite_artifact_id=payload.task_suite_artifact_id,
            environment_profile=payload.environment_profile,
            planner_policy=payload.planner_policy,
            env_contract_version=suite.env_contract_version,
            interaction_mode=payload.interaction_mode,
            seed=base_seed,
            episodes=tuple(episode_traces),
        )
        primary = store_prepared_artifact(
            self.store,
            kind="playtest_trace",
            payload_schema_id=PLAYTEST_TRACE_SCHEMA_ID,
            version_tuple=VersionTuple(
                ir_snapshot_id=preview.snapshot_id,
                constraint_snapshot_id=payload.constraint_snapshot_artifact_id,
                env_contract_version=suite.env_contract_version,
                tool_version=PLAYTEST_TOOL_VERSION,
                seed=base_seed,
            ),
            lineage=self._run_input_lineage(payload),
            payload=trace.model_dump(mode="json"),
        )
        prepared_findings = build_prepared_findings(
            tuple(findings_evidence), run_id=context.run.run_id
        )
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code=PLAYTEST_OUTCOME_CODE,
            primary_index=0,
            artifacts=(primary,),
            findings=prepared_findings,
        )

    # ------------------------------------------------------------------ router
    def _build_router(
        self, context: ExecutorContextLike, use_memory: bool
    ) -> MultiNodeBridgeRouter:
        node_ids = list(PLAYTEST_CORE_NODE_IDS)
        if use_memory:
            node_ids.append(PLAYTEST_MEMORY_NODE)
        return build_multinode_bridge_router(
            context=context,
            agent_node_ids=tuple(node_ids),
            default_node_id=PLAYTEST_PLANNER_NODE,
        )

    # ------------------------------------------------------------------ inputs
    def _load_suite(self, payload: PlaytestRunPayloadV1) -> TaskSuiteV1:
        raw = load_json_blob(self.blobs, payload.task_suite_artifact_id)
        if not isinstance(raw, dict):
            raise ValueError("task_suite artifact payload must be a JSON object")
        return TaskSuiteV1.model_validate(raw)

    def _load_scenario(self, artifact_id: str) -> ScenarioSpecV1:
        raw = load_json_blob(self.blobs, artifact_id)
        if not isinstance(raw, dict):
            raise ValueError("scenario_spec artifact payload must be a JSON object")
        return ScenarioSpecV1.model_validate(raw)

    @staticmethod
    def _validate_suite_bindings(suite: TaskSuiteV1, payload: PlaytestRunPayloadV1) -> None:
        if suite.config_export_artifact_id != payload.config_artifact_id:
            raise ValueError("stale task-suite config binding")
        if suite.constraint_snapshot_artifact_id != payload.constraint_snapshot_artifact_id:
            raise ValueError("stale task-suite constraint binding")
        if suite.environment_profile != payload.environment_profile:
            raise ValueError("stale task-suite environment binding")

    @staticmethod
    def _resolve_selected_episodes(
        suite: TaskSuiteV1, payload: PlaytestRunPayloadV1
    ) -> tuple[tuple[PlaytestEpisodeBindingV1, TaskEpisodeV1], ...]:
        by_id = {episode.episode_id: episode for episode in suite.episodes}
        selected: list[tuple[PlaytestEpisodeBindingV1, TaskEpisodeV1]] = []
        for binding in payload.episodes:
            episode = by_id.get(binding.episode_id)
            if episode is None:
                raise ValueError(
                    f"selected episode {binding.episode_id!r} is not in the task suite"
                )
            if episode.scenario_spec_artifact_id != binding.scenario_spec_artifact_id:
                raise ValueError(
                    f"selected episode {binding.episode_id!r} scenario binding is stale"
                )
            selected.append((binding, episode))
        if not selected:
            raise ValueError("playtest requires at least one selected episode")
        return tuple(selected)

    @staticmethod
    def _validate_scenario_bindings(
        scenario: ScenarioSpecV1, suite: TaskSuiteV1, payload: PlaytestRunPayloadV1
    ) -> None:
        if scenario.config_export_artifact_id != payload.config_artifact_id:
            raise ValueError("stale scenario config binding")
        if scenario.constraint_snapshot_artifact_id != payload.constraint_snapshot_artifact_id:
            raise ValueError("stale scenario constraint binding")
        if scenario.environment_profile != payload.environment_profile:
            raise ValueError("stale scenario environment binding")
        if scenario.env_contract_version != suite.env_contract_version:
            raise ValueError("stale scenario env-contract binding")
        if scenario.source_preview_artifact_id != suite.source_preview_artifact_id:
            raise ValueError("stale scenario preview binding")

    @staticmethod
    def _run_input_lineage(payload: PlaytestRunPayloadV1) -> tuple[str, ...]:
        # config + constraint + task_suite run_input roles; the selected_scenarios
        # sibling parents on the trace are publisher-injected (Task-18 carry).
        return (
            payload.config_artifact_id,
            payload.constraint_snapshot_artifact_id,
            payload.task_suite_artifact_id,
        )

    def _unavailable_failure(
        self, context: ExecutorContextLike, environment_profile: ProfileRefV1
    ) -> PreparedRunFailure:
        # The cause_code MUST be a cause the frozen failure classifier knows, and the
        # dependency's classifier_code MUST equal it, or the run boundary's exact
        # classifier validation (validate_prepared_failure) rejects the failure with an
        # IntegrityViolation. ``permanent_dependency_failed`` is the frozen
        # ``permanent_dependency`` cause whose allowlist includes ``game_environment``;
        # the "unknown environment profile" specificity is carried in the dependency's
        # operation_code / dependency_id + the redacted message, not in the cause code.
        return PreparedRunFailure(
            run_id=context.run.run_id,
            attempt_no=context.attempt.attempt_no,
            run_kind=context.run.kind,
            artifacts=(),
            requirement_dispositions=(),
            cause_code="permanent_dependency_failed",
            failure_class="permanent_dependency",
            intrinsic_retry_eligible=False,
            classifier=context.run.failure_classifier,
            dependency=DependencyFailureV1(
                dependency_kind="game_environment",
                dependency_id=(
                    f"unknown_environment_profile:"
                    f"{environment_profile.profile_id}@{environment_profile.version}"
                ),
                operation_code="resolve_environment_profile",
                classifier_code="permanent_dependency_failed",
            ),
            redacted_message="requested playtest environment profile is not available",
        )


def _records(
    action_trace: tuple[dict, ...], allowed_kinds: frozenset[str]
) -> tuple[PlaytestActionRecordV1, ...]:
    """Project the raw agent action trace onto bounded records (fail-closed on kind)."""

    records: list[PlaytestActionRecordV1] = []
    for step in action_trace:
        action = step.get("action", {}) if isinstance(step, dict) else {}
        kind = action.get("kind") if isinstance(action, dict) else None
        if kind not in allowed_kinds:
            raise ValueError(
                f"playtest action kind {kind!r} is not an allowed bounded interaction command"
            )
        result = step.get("last_action_result") if isinstance(step, dict) else None
        tick = step.get("tick", 0) if isinstance(step, dict) else 0
        records.append(
            PlaytestActionRecordV1(
                action=action,
                last_action_result=result if isinstance(result, str) else "",
                tick=int(tick) if isinstance(tick, int) else 0,
            )
        )
    return tuple(records)


__all__ = [
    "ENVIRONMENT_PROFILE_FIELD",
    "PLANNER_POLICY_FIELD",
    "PLAYTEST_CORE_NODE_IDS",
    "PLAYTEST_EXECUTOR_NODE",
    "PLAYTEST_MEMORY_NODE",
    "PLAYTEST_OUTCOME_CODE",
    "PLAYTEST_PLANNER_NODE",
    "PLAYTEST_REFLECT_NODE",
    "PLAYTEST_TOOL_VERSION",
    "PLAYTEST_TRACE_SCHEMA_ID",
    "MemorySelector",
    "PlaytestEnvRunner",
    "PlaytestEpisodeOutcomeV1",
    "PlaytestEpisodeRunRequest",
    "PlaytestRunHandler",
    "allowed_action_kinds",
    "derive_episode_seed",
]
