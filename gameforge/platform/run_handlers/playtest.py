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

Lineage: the trace declares the exact run_input parents (``config`` + ``constraint`` +
``task_suite`` + every selected ScenarioSpec); all ids already exist at admission.
The LLM ``source_rendered`` runtime parents are publisher-projected. ``seed`` is
producer-local; ir/constraint/env are inherited from the run inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from pydantic import JsonValue

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.config_export import ConfigExportPackageV1, decode_config_export_bytes
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    EnvironmentContractDescriptorV1,
    PlaytestPlannerProfileConfigV2,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import (
    DependencyFailureV1,
    PlaytestEpisodeBindingV1,
    PlaytestRunPayloadV1,
    PreparedRunFailure,
    PreparedRunOutcome,
)
from gameforge.contracts.playtest import (
    CompletionOracleRefV1,
    CompletionOracleRegistryRefV1,
    PlaytestActionRecordV1,
    PlaytestExecutionEnvelopeV1,
    PlaytestEpisodeSeedBindingV1,
    PlaytestEpisodeTraceV1,
    PlaytestPayloadSchemaPurposeV1,
    PlaytestTerminalReasonV1,
    PlaytestTraceV1,
    ScenarioResetBindingV1,
    ScenarioSpecV1,
    TaskEpisodeV1,
    TaskSuiteV1,
    bind_exact_playtest_trace_bytes,
    derive_playtest_trace_markers,
    playtest_resource_upper_bounds,
)
from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    ExecutorContextLike,
    FindingEvidence,
    FindingHeadRevisionResolver,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    load_json_blob,
    prepared_version_tuple,
    require_exact_profile_bindings,
    scoped_finding_series_id,
    store_prepared_artifact,
    trust_typed_profile_binding,
)
from gameforge.platform.run_handlers.model_routing import (
    MultiNodeBridgeRouter,
    build_multinode_bridge_router,
)
from gameforge.platform.run_handlers.validation_common import derive_validation_subseed

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


def derive_episode_seed(
    *,
    root_seed: int,
    run_kind: RunKindRef,
    environment_profile: ProfileRefV1,
    task_suite_artifact_id: str,
    episode_id: str,
) -> int:
    """Derive one episode through the frozen complete ``subseed@1`` tuple."""

    return derive_validation_subseed(
        root_seed=root_seed,
        run_kind=run_kind,
        profile=environment_profile,
        case_id=f"{task_suite_artifact_id}:{episode_id}",
        replication_index=0,
    )


@dataclass(frozen=True, slots=True)
class PlaytestEpisodeRunRequest:
    """Fully-resolved deterministic inputs for ONE selected episode's agent-env drive."""

    config_artifact_id: str
    config_export: ConfigExportPackageV1
    environment_profile: ProfileRefV1
    action_schema_id: str
    allowed_action_kinds: frozenset[str]
    scenario_id: str
    reset_binding: ScenarioResetBindingV1
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
    initial_state_hash: str
    final_state_hash: str


class PlaytestEnvRunner(Protocol):
    """Drive the M2b PlaytestAgent against a profile-selected env (game-specific port)."""

    def supports(self, environment_profile: ProfileRefV1) -> bool: ...

    def run_episode(self, request: PlaytestEpisodeRunRequest) -> PlaytestEpisodeOutcomeV1: ...


# Resolve the complete retained profile config through the Run-frozen catalog/hash
# binding.  The worker composition owns this authority; the handler has no process
# default and therefore cannot silently invent memory/resource behavior.
PlannerConfigResolver = Callable[
    [ResolvedExecutionProfileBindingV1], PlaytestPlannerProfileConfigV2
]
EnvironmentContractResolver = Callable[[ProfileRefV1], EnvironmentContractDescriptorV1]


class PlaytestActionPayloadValidator(Protocol):
    """Validate an action through the exact retained payload-schema authority."""

    def validate_exact(
        self,
        *,
        schema_id: str,
        purpose: PlaytestPayloadSchemaPurposeV1,
        payload: JsonValue,
    ) -> JsonValue: ...


@dataclass(frozen=True, slots=True)
class PlaytestRunHandler:
    """A ``RunExecutor`` for ``playtest_runner@1`` (LLM, seeded, replayable)."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    env_runner: PlaytestEnvRunner
    planner_config_resolver: PlannerConfigResolver
    environment_contract_resolver: EnvironmentContractResolver
    finding_head_revision: FindingHeadRevisionResolver | None = None
    profile_binding_validator: ExactProfileBindingValidator = trust_typed_profile_binding
    use_planner: bool = True

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, PlaytestRunPayloadV1):
            raise TypeError("playtest_runner@1 requires a playtest-run@1 payload")
        envelope = context.payload

        profile_bindings = require_exact_profile_bindings(
            context,
            expected={
                ENVIRONMENT_PROFILE_FIELD: (
                    payload.environment_profile,
                    "environment",
                ),
                PLANNER_POLICY_FIELD: (
                    payload.planner_policy,
                    "playtest_planner",
                ),
            },
            validator=self.profile_binding_validator,
        )
        environment_profile = profile_bindings[ENVIRONMENT_PROFILE_FIELD].profile
        planner_binding = profile_bindings[PLANNER_POLICY_FIELD]

        if envelope.seed is None:
            raise ValueError("playtest_runner@1 requires a seeded Run payload (subseed@1)")
        base_seed = int(envelope.seed)

        # UNKNOWN environment → typed unavailable, BEFORE any suite load or LLM call.
        if not self.env_runner.supports(environment_profile):
            return self._unavailable_failure(context, environment_profile)

        environment_contract = self.environment_contract_resolver(environment_profile)
        if (
            context.payload.version_tuple.env_contract_version
            != environment_contract.env_contract_version
        ):
            raise IntegrityViolation(
                "playtest environment contract differs from the frozen Run authority"
            )
        suite = self._load_suite(payload)
        self._validate_suite_bindings(suite, payload)
        config_export = self._load_config_export(payload.config_artifact_id)
        self._validate_config_bindings(config_export, suite, payload)
        selected = self._resolve_selected_episodes(suite, payload)

        try:
            planner_config = PlaytestPlannerProfileConfigV2.model_validate(
                self.planner_config_resolver(planner_binding)
            )
            (
                total_step_limit,
                model_call_upper_bound,
                total_trace_byte_upper_bound,
            ) = playtest_resource_upper_bounds(
                planner_config,
                episode_count=len(selected),
                max_steps_per_episode=int(payload.max_steps_per_episode),
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "playtest request exceeds its exact planner profile authority"
            ) from exc

        use_memory = planner_config.memory_mode == "llm_compaction"
        router = self._build_router(
            context,
            use_memory,
            max_calls=model_call_upper_bound,
        )
        allowed_kinds = allowed_action_kinds(payload.interaction_mode)
        ir_snapshot_id = context.payload.version_tuple.ir_snapshot_id
        if not isinstance(ir_snapshot_id, str) or not ir_snapshot_id:
            raise IntegrityViolation("playtest Run lacks an exact IR snapshot authority")

        episode_traces: list[PlaytestEpisodeTraceV1] = []
        findings_by_id: dict[str, FindingEvidence] = {}
        for binding, episode in selected:
            # Each episode creates a fresh environment, Agent, and MemTrace. Keep
            # one run-scoped adapter/cassette, but do not invent a direct causal
            # edge from the preceding episode's final model response.
            router.begin_causal_scope()
            scenario = self._load_scenario(binding.scenario_spec_artifact_id)
            self._validate_scenario_bindings(scenario, suite, payload, episode)
            seed_case_id = f"{payload.task_suite_artifact_id}:{episode.episode_id}"
            episode_seed = derive_episode_seed(
                root_seed=base_seed,
                run_kind=context.run.kind,
                environment_profile=environment_profile,
                task_suite_artifact_id=payload.task_suite_artifact_id,
                episode_id=episode.episode_id,
            )
            max_steps = int(payload.max_steps_per_episode)
            outcome = self.env_runner.run_episode(
                PlaytestEpisodeRunRequest(
                    config_artifact_id=payload.config_artifact_id,
                    config_export=config_export,
                    environment_profile=environment_profile,
                    action_schema_id=environment_contract.action_schema_id,
                    allowed_action_kinds=allowed_kinds,
                    scenario_id=scenario.scenario_id,
                    reset_binding=scenario.reset_binding,
                    seed=episode_seed,
                    router=router,
                    use_planner=self.use_planner,
                    memory_enabled=use_memory,
                    max_steps=max_steps,
                    completion_oracle=episode.completion_oracle,
                    completion_oracle_registry_ref=suite.completion_oracle_registry_ref,
                )
            )
            if type(outcome.completed) is not bool:
                raise ValueError("playtest env runner must return a boolean completion verdict")
            records = _records(
                outcome.action_trace,
                max_steps=max_steps,
            )
            terminal_reason = _terminal_reason(
                completed=outcome.completed,
                action_count=len(records),
                execution_step_limit=max_steps,
                has_deterministic_findings=bool(outcome.defect_findings),
            )
            seed_binding = PlaytestEpisodeSeedBindingV1(
                root_seed=base_seed,
                run_kind=context.run.kind,
                profile=environment_profile,
                case_id=seed_case_id,
                replication_index=0,
                seed=episode_seed,
            )
            episode_trace = PlaytestEpisodeTraceV1(
                episode_id=episode.episode_id,
                scenario_spec_artifact_id=binding.scenario_spec_artifact_id,
                seed=episode_seed,
                seed_binding=seed_binding,
                step_budget=int(episode.step_budget),
                execution_step_limit=max_steps,
                completion_oracle=episode.completion_oracle,
                completed=outcome.completed,
                terminal_reason=terminal_reason,
                initial_state_hash=outcome.initial_state_hash,
                final_state_hash=outcome.final_state_hash,
                action_trace=records,
                markers=derive_playtest_trace_markers(
                    records,
                    initial_state_hash=outcome.initial_state_hash,
                    final_state_hash=outcome.final_state_hash,
                    terminal_reason=terminal_reason,
                ),
            )
            episode_traces.append(episode_trace)

            for finding in outcome.defect_findings:
                rebound = _rebind_runtime_finding(
                    finding,
                    ir_snapshot_id=ir_snapshot_id,
                    episode_id=episode.episode_id,
                    scenario_spec_artifact_id=binding.scenario_spec_artifact_id,
                )
                _accumulate_finding(
                    findings_by_id,
                    finding=rebound,
                    episode_id=episode.episode_id,
                )
            if not outcome.completed:
                _accumulate_finding(
                    findings_by_id,
                    finding=_incomplete_episode_finding(
                        run_id=context.run.run_id,
                        ir_snapshot_id=ir_snapshot_id,
                        episode=episode,
                        scenario_spec_artifact_id=binding.scenario_spec_artifact_id,
                        terminal_reason=terminal_reason,
                        episode_trace=episode_trace,
                    ),
                    episode_id=episode.episode_id,
                )

        total_action_count = sum(len(episode.action_trace) for episode in episode_traces)
        total_action_trace_bytes = sum(
            len(
                canonical_json(
                    [record.model_dump(mode="json") for record in episode.action_trace]
                ).encode("utf-8")
            )
            for episode in episode_traces
        )
        if router.call_count > planner_config.max_total_model_calls:
            raise IntegrityViolation("playtest exceeded its exact model-call authority")
        if total_action_trace_bytes > planner_config.max_total_trace_bytes:
            raise IntegrityViolation("playtest exceeded its exact trace-byte authority")

        trace_payload: dict[str, object] = {
            "config_artifact_id": payload.config_artifact_id,
            "constraint_snapshot_artifact_id": payload.constraint_snapshot_artifact_id,
            "task_suite_artifact_id": payload.task_suite_artifact_id,
            "environment_profile": payload.environment_profile.model_dump(mode="json"),
            "planner_policy": payload.planner_policy.model_dump(mode="json"),
            "env_contract_version": suite.env_contract_version,
            "interaction_mode": payload.interaction_mode,
            "seed": base_seed,
            "requested_max_steps_per_episode": int(payload.max_steps_per_episode),
            "planner_memory_mode": planner_config.memory_mode,
            "execution_envelope": PlaytestExecutionEnvelopeV1(
                planner_profile_payload_hash=planner_binding.profile_payload_hash,
                selected_episode_count=len(episode_traces),
                total_step_limit=total_step_limit,
                model_call_upper_bound=model_call_upper_bound,
                total_trace_byte_upper_bound=total_trace_byte_upper_bound,
                actual_model_calls=router.call_count,
                total_action_count=total_action_count,
                total_action_trace_bytes=total_action_trace_bytes,
                actual_trace_bytes=1,
            ).model_dump(mode="json"),
            "episodes": [episode.model_dump(mode="json") for episode in episode_traces],
        }
        trace = PlaytestTraceV1.model_validate(bind_exact_playtest_trace_bytes(trace_payload))
        if trace.execution_envelope.actual_trace_bytes > planner_config.max_total_trace_bytes:
            raise IntegrityViolation("playtest exceeded its exact complete trace authority")
        primary = store_prepared_artifact(
            self.store,
            kind="playtest_trace",
            payload_schema_id=PLAYTEST_TRACE_SCHEMA_ID,
            version_tuple=prepared_version_tuple(
                context,
                tool_version=PLAYTEST_TOOL_VERSION,
                projected_fields=(
                    "ir_snapshot_id",
                    "constraint_snapshot_id",
                    "env_contract_version",
                    "seed",
                ),
            ),
            lineage=self._run_input_lineage(payload),
            payload=trace.model_dump(mode="json"),
        )
        prepared_findings = build_prepared_findings(
            tuple(findings_by_id[key] for key in sorted(findings_by_id)),
            run_id=context.run.run_id,
            head_revision_resolver=self.finding_head_revision,
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
        self,
        context: ExecutorContextLike,
        use_memory: bool,
        *,
        max_calls: int,
    ) -> MultiNodeBridgeRouter:
        node_ids = list(PLAYTEST_CORE_NODE_IDS)
        if use_memory:
            node_ids.append(PLAYTEST_MEMORY_NODE)
        return build_multinode_bridge_router(
            context=context,
            agent_node_ids=tuple(node_ids),
            default_node_id=PLAYTEST_PLANNER_NODE,
            max_calls=max_calls,
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

    def _load_config_export(self, artifact_id: str) -> ConfigExportPackageV1:
        try:
            return decode_config_export_bytes(self.blobs.read_bytes(artifact_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("playtest config export is not a canonical package") from exc

    @staticmethod
    def _validate_suite_bindings(suite: TaskSuiteV1, payload: PlaytestRunPayloadV1) -> None:
        if suite.config_export_artifact_id != payload.config_artifact_id:
            raise ValueError("stale task-suite config binding")
        if suite.constraint_snapshot_artifact_id != payload.constraint_snapshot_artifact_id:
            raise ValueError("stale task-suite constraint binding")
        if suite.environment_profile != payload.environment_profile:
            raise ValueError("stale task-suite environment binding")

    @staticmethod
    def _validate_config_bindings(
        package: ConfigExportPackageV1,
        suite: TaskSuiteV1,
        payload: PlaytestRunPayloadV1,
    ) -> None:
        if package.source_preview_artifact_id != suite.source_preview_artifact_id:
            raise ValueError("stale config preview binding")
        if package.constraint_snapshot_artifact_id != payload.constraint_snapshot_artifact_id:
            raise ValueError("stale config constraint binding")
        if package.target_environment_profile != payload.environment_profile:
            raise ValueError("stale config environment binding")
        if package.env_contract_version != suite.env_contract_version:
            raise ValueError("stale config env-contract binding")

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
            if payload.max_steps_per_episode > episode.step_budget:
                raise ValueError(f"selected episode {binding.episode_id!r} step budget is stale")
            selected.append((binding, episode))
        if not selected:
            raise ValueError("playtest requires at least one selected episode")
        return tuple(selected)

    @staticmethod
    def _validate_scenario_bindings(
        scenario: ScenarioSpecV1,
        suite: TaskSuiteV1,
        payload: PlaytestRunPayloadV1,
        episode: TaskEpisodeV1,
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
        if scenario.reset_binding != episode.reset_binding:
            raise ValueError("stale scenario reset binding")
        if scenario.domain_scope != episode.domain_scope:
            raise ValueError("stale scenario domain binding")

    @staticmethod
    def _run_input_lineage(payload: PlaytestRunPayloadV1) -> tuple[str, ...]:
        # Every direct role is an immutable Run input.  Unlike prepared siblings,
        # selected ScenarioSpecs already have final Artifact ids at admission and
        # therefore must be declared by the handler for typed-lineage projection.
        return (
            payload.config_artifact_id,
            payload.constraint_snapshot_artifact_id,
            payload.task_suite_artifact_id,
            *(item.scenario_spec_artifact_id for item in payload.episodes),
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


def _terminal_reason(
    *,
    completed: bool,
    action_count: int,
    execution_step_limit: int,
    has_deterministic_findings: bool,
) -> PlaytestTerminalReasonV1:
    if completed:
        return "completion_oracle_satisfied"
    if action_count == execution_step_limit:
        return "step_limit_exhausted"
    if has_deterministic_findings:
        return "deterministic_abort"
    return "agent_stopped"


def _rebind_runtime_finding(
    finding: Finding,
    *,
    ir_snapshot_id: str,
    episode_id: str,
    scenario_spec_artifact_id: str,
) -> Finding:
    """Bind a runtime Finding to publishable IR authority without losing state evidence."""

    occurrence: dict[str, object] = {
        "evidence": dict(finding.evidence),
        "minimal_repro": dict(finding.minimal_repro),
    }
    if finding.snapshot_id != ir_snapshot_id:
        occurrence["runtime_state_hash"] = finding.snapshot_id
    return finding.model_copy(
        update={
            "snapshot_id": ir_snapshot_id,
            "evidence": {
                "episode_id": episode_id,
                "scenario_spec_artifact_id": scenario_spec_artifact_id,
                "occurrences": [occurrence],
            },
            "minimal_repro": {
                "episode_id": episode_id,
                "scenario_spec_artifact_id": scenario_spec_artifact_id,
            },
            "created_at": None,
        }
    )


def _accumulate_finding(
    findings_by_id: dict[str, FindingEvidence],
    *,
    finding: Finding,
    episode_id: str,
) -> None:
    """Deduplicate one episode-scoped series before the publisher's head CAS."""

    finding_id = scoped_finding_series_id(
        namespace="playtest",
        scope_id=episode_id,
        finding_id=finding.id,
    )
    existing = findings_by_id.get(finding_id)
    if existing is None:
        findings_by_id[finding_id] = FindingEvidence(
            finding=finding,
            evidence_artifact_index=0,
            finding_id=finding_id,
        )
        return

    stable_excludes = {"evidence", "minimal_repro", "created_at"}
    if canonical_json(
        existing.finding.model_dump(mode="json", exclude=stable_excludes)
    ) != canonical_json(finding.model_dump(mode="json", exclude=stable_excludes)):
        raise IntegrityViolation("duplicate playtest Finding series has conflicting semantics")
    previous_occurrences = existing.finding.evidence.get("occurrences")
    new_occurrences = finding.evidence.get("occurrences")
    if not isinstance(previous_occurrences, list) or not isinstance(new_occurrences, list):
        if canonical_json(existing.finding.evidence) != canonical_json(finding.evidence):
            raise IntegrityViolation("duplicate playtest Finding series has conflicting evidence")
        return
    by_payload = {
        canonical_json(occurrence): occurrence
        for occurrence in (*previous_occurrences, *new_occurrences)
    }
    merged_evidence = {
        **existing.finding.evidence,
        "occurrences": [by_payload[key] for key in sorted(by_payload)],
    }
    findings_by_id[finding_id] = FindingEvidence(
        finding=existing.finding.model_copy(update={"evidence": merged_evidence}),
        evidence_artifact_index=0,
        finding_id=finding_id,
    )


def _incomplete_episode_finding(
    *,
    run_id: str,
    ir_snapshot_id: str,
    episode: TaskEpisodeV1,
    scenario_spec_artifact_id: str,
    terminal_reason: PlaytestTerminalReasonV1,
    episode_trace: PlaytestEpisodeTraceV1,
) -> Finding:
    return Finding(
        id=f"playtest-incomplete:{terminal_reason}",
        source="playtest",
        producer_id="playtest.completion_oracle",
        producer_run_id=run_id,
        oracle_type="deterministic",
        defect_class="playtest_incomplete",
        severity="major",
        snapshot_id=ir_snapshot_id,
        evidence={
            "episode_id": episode.episode_id,
            "scenario_spec_artifact_id": scenario_spec_artifact_id,
            "terminal_reason": terminal_reason,
            "completion_oracle": episode.completion_oracle.model_dump(mode="json"),
            "execution_step_limit": episode_trace.execution_step_limit,
            "executed_steps": len(episode_trace.action_trace),
            "initial_state_hash": episode_trace.initial_state_hash,
            "final_state_hash": episode_trace.final_state_hash,
        },
        minimal_repro={
            "episode_id": episode.episode_id,
            "scenario_spec_artifact_id": scenario_spec_artifact_id,
            "seed_binding": episode_trace.seed_binding.model_dump(mode="json"),
            "execution_step_limit": episode_trace.execution_step_limit,
        },
        status="confirmed",
        message="The deterministic completion oracle was not satisfied in the bounded episode.",
    )


def validate_environment_action(
    action: dict[str, object],
    *,
    action_schema_id: str,
    allowed_kinds: frozenset[str],
    payload_validator: PlaytestActionPayloadValidator,
) -> dict[str, object]:
    """Return the exact profile-schema-selected canonical environment action.

    The unchanged M2 agent serializes ``Use(target=None)`` with an explicit null in
    its report even though the action wire's canonical representation omits that
    optional field. Normalize only that known internal representation before exact
    schema validation; every other extra, missing, coerced, or out-of-bound value is
    rejected.
    """

    candidate = dict(action)
    if candidate.get("kind") == "use" and candidate.get("target") is None:
        candidate.pop("target", None)
    try:
        validated = payload_validator.validate_exact(
            schema_id=action_schema_id,
            purpose="environment_action",
            payload=candidate,
        )
    except IntegrityViolation as exc:
        raise IntegrityViolation(
            "playtest environment action violates its exact profile-selected schema",
            action_schema_id=action_schema_id,
        ) from exc
    if not isinstance(validated, dict):
        raise IntegrityViolation(
            "playtest environment action schema returned a non-object value",
            action_schema_id=action_schema_id,
        )
    canonical_action = dict(validated)
    kind = canonical_action.get("kind")
    if kind not in allowed_kinds:
        raise IntegrityViolation(
            "playtest environment action kind is not allowed by the interaction mode",
            action_schema_id=action_schema_id,
            action_kind=kind,
        )
    return canonical_action


def _records(
    action_trace: tuple[dict, ...],
    *,
    max_steps: int,
) -> tuple[PlaytestActionRecordV1, ...]:
    """Project the already-executed action trace onto bounded records."""

    if not isinstance(action_trace, tuple):
        raise ValueError("playtest action record collection must be an exact tuple")
    if len(action_trace) > max_steps:
        raise ValueError("playtest action trace exceeds the requested step limit")
    records: list[PlaytestActionRecordV1] = []
    for step in action_trace:
        if not isinstance(step, dict) or set(step) != {
            "action",
            "last_action_result",
            "tick",
            "state_hash",
        }:
            raise ValueError("playtest action record has an invalid shape")
        action = step["action"]
        result = step["last_action_result"]
        tick = step["tick"]
        state_hash = step["state_hash"]
        if (
            not isinstance(action, dict)
            or not isinstance(result, str)
            or not isinstance(tick, int)
            or isinstance(tick, bool)
            or tick < 0
            or not isinstance(state_hash, str)
            or not state_hash
        ):
            raise ValueError("playtest action record has invalid field types")
        canonical_action = dict(action)
        if canonical_action.get("kind") == "use" and canonical_action.get("target") is None:
            canonical_action.pop("target", None)
        try:
            records.append(
                PlaytestActionRecordV1(
                    action=canonical_action,
                    last_action_result=result,
                    tick=tick,
                    state_hash=state_hash,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("playtest action record exceeds its bounded contract") from exc
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
    "EnvironmentContractResolver",
    "PlannerConfigResolver",
    "PlaytestActionPayloadValidator",
    "PlaytestEnvRunner",
    "PlaytestEpisodeOutcomeV1",
    "PlaytestEpisodeRunRequest",
    "PlaytestRunHandler",
    "allowed_action_kinds",
    "derive_episode_seed",
    "validate_environment_action",
]
