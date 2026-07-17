"""Aureus composition for ``playtest_runner@1`` (Task 12b).

The platform :class:`PlaytestRunHandler` is game-agnostic; this module (the ``apps``
composition boundary, which may import ``spine`` + ``game`` + ``agents``) supplies the
concrete :class:`PlaytestEnvRunner` port injected into it. The runner:

* resolves whether the requested ``environment_profile`` is one this build serves
  (``supports`` — the platform never branches on Aureus itself);
* builds the DETERMINISTIC Aureus env for one episode from the preview IR
  (``snapshot_to_world`` → ``AureusEnv`` → ``env.reset``);
* drives the UNMODIFIED M2b ``PlaytestAgent`` through the injected multi-node bridge
  router (LLM proposes; the engine is the sole authority on outcomes);
* evaluates the episode's completion oracle against the FINAL env via the frozen
  completion-oracle registry + the trusted executor map — a DETERMINISTIC verdict, never
  an LLM claim.

``AureusEnv.reset(scenario, seed)`` fixes the world at construction and varies only by
seed, so each episode is a distinct seeded playthrough of the preview world; the
per-episode subseed comes from the platform handler (``derive_episode_seed``). The LLM
still flows ONLY through the injected router (over the M4b model bridge); no LLM SDK is
imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from gameforge.agents.playtest.agent import PlaytestAgent
from gameforge.agents.playtest.memory import LLMCompactor, MemTrace
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.apps.worker.config_export import decode_aureus_config_workbook
from gameforge.apps.worker.completion_oracles import build_completion_oracle_executors
from gameforge.contracts.agent_io import PlaytestInput
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    EnvironmentContractDescriptorV1,
    PlaytestPlannerProfileConfigV2,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    execution_profile_payload_hash,
)
from gameforge.contracts.playtest import (
    CompletionOracleRegistryRefV1,
    CompletionOracleRegistryV1,
    resolve_completion_oracle,
)
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.playtest_payload_schemas import (
    ExactModelPayloadValidator,
    PlaytestPayloadValidationService,
    build_builtin_playtest_payload_validators,
)
from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    FindingHeadRevisionResolver,
    PreparedArtifactStore,
)
from gameforge.platform.run_handlers.playtest import (
    PlaytestEpisodeOutcomeV1,
    PlaytestEpisodeRunRequest,
    PlaytestRunHandler,
)
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.apps.worker.task_suite import (
    EnvironmentContractResolver,
    build_environment_contract_resolver,
)


_AUREUS_CONFIG_FORMAT_SCHEMA_ID = "config-export-files@1"
_AUREUS_RESET_SCHEMA_IDS = frozenset({"generic-env-reset@1", "aureus-env-reset@1"})
_AUREUS_ACTION_SCHEMA_ID = "generic-env-action@1"
_AUREUS_OBSERVATION_SCHEMA_ID = "generic-env-observation@1"

PlaytestPlannerConfigResolver = Callable[
    [ResolvedExecutionProfileBindingV1], PlaytestPlannerProfileConfigV2
]


class _StateHashRecordingEnv:
    """Observe post-step state hashes without changing the M2 Agent trace/prompts."""

    def __init__(self, env: AureusEnv) -> None:
        self._env = env
        self.post_step_hashes: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._env, name)

    def step(self, action: Any) -> Any:
        result = self._env.step(action)
        self.post_step_hashes.append(self._env.state_hash())
        return result


@dataclass(frozen=True, slots=True)
class AureusPlaytestRunner:
    """Drive the M2b ``PlaytestAgent`` against a seeded Aureus env + eval the oracle."""

    oracle_registry: CompletionOracleRegistryV1
    oracle_executors: Mapping[str, object]
    supported_profiles: frozenset[ProfileRefV1]
    oracle_registry_resolver: (
        Callable[[CompletionOracleRegistryRefV1], CompletionOracleRegistryV1 | None] | None
    ) = None
    environment_contract_resolver: EnvironmentContractResolver | None = None
    payload_validator: PlaytestPayloadValidationService | None = None

    def supports(self, environment_profile: ProfileRefV1) -> bool:
        return environment_profile in self.supported_profiles

    def run_episode(self, request: PlaytestEpisodeRunRequest) -> PlaytestEpisodeOutcomeV1:
        if not self.supports(request.environment_profile):
            # Defense-in-depth: the platform already gated on ``supports``.
            raise ValueError(f"unsupported environment profile {request.environment_profile!r}")

        world = self._world_from_request(request)
        env = AureusEnv(world)
        env.reset(request.scenario_id, request.seed)
        initial_state_hash = env.state_hash()
        recording_env = _StateHashRecordingEnv(env)

        memory = MemTrace(compactor=LLMCompactor(strict=True)) if request.memory_enabled else None
        report = PlaytestAgent().run(
            PlaytestInput(scenario=request.scenario_id, seed=request.seed),
            recording_env,
            request.router,
            use_planner=request.use_planner,
            memory=memory,
            max_steps=request.max_steps,
        )

        # DETERMINISTIC completion verdict via the frozen registry + trusted executor.
        oracle_registry = (
            self.oracle_registry_resolver(request.completion_oracle_registry_ref)
            if self.oracle_registry_resolver is not None
            else self.oracle_registry
        )
        if oracle_registry is None:
            raise ValueError("completion-oracle registry authority is unavailable")
        definition = resolve_completion_oracle(
            oracle_registry,
            request.completion_oracle_registry_ref,
            request.completion_oracle,
        )
        oracle_params = request.completion_oracle.params
        if self.payload_validator is not None:
            oracle_params = self.payload_validator.validate_exact(
                schema_id=definition.params_schema_id,
                purpose="completion_oracle_params",
                payload=oracle_params,
            )
        executor = self.oracle_executors[definition.executor_key]
        verdict = executor.evaluate(env, oracle_params)
        if type(verdict) is not bool:
            raise IntegrityViolation("completion-oracle executor returned a non-boolean verdict")
        completed = verdict

        if len(recording_env.post_step_hashes) != len(report.action_trace):
            raise IntegrityViolation("playtest state-hash recorder differs from the action trace")
        action_trace = tuple(
            {**dict(record), "state_hash": state_hash}
            for record, state_hash in zip(
                report.action_trace,
                recording_env.post_step_hashes,
                strict=True,
            )
        )

        return PlaytestEpisodeOutcomeV1(
            action_trace=action_trace,
            defect_findings=tuple(report.defect_findings),
            completed=completed,
            initial_state_hash=initial_state_hash,
            final_state_hash=env.state_hash(),
        )

    def _world_from_request(self, request: PlaytestEpisodeRunRequest):
        """Build the exact bounded world selected by config + reset authority."""

        contract = self._resolve_environment_contract(request)
        snapshot = self._snapshot_from_config_export(request)
        world = snapshot_to_world(snapshot)
        if contract is not None:
            grid_cells = int(world.grid.width) * int(world.grid.height)
            if (
                world.grid.width <= 0
                or world.grid.height <= 0
                or grid_cells > contract.max_navigation_grid_cells
            ):
                raise IntegrityViolation(
                    "playtest world exceeds the environment navigation-grid authority"
                )
        quest_ids = self._reset_quest_ids(request, contract=contract)
        if not quest_ids:
            return world
        quests = {quest.quest_id: quest for quest in world.quests}
        unknown = tuple(sorted(set(quest_ids) - set(quests)))
        if unknown:
            raise ValueError("playtest reset payload references unknown quest ids")
        return world.model_copy(update={"quests": [quests[quest_id] for quest_id in quest_ids]})

    @staticmethod
    def _snapshot_from_config_export(request: PlaytestEpisodeRunRequest):
        package = request.config_export
        if package.target_environment_profile != request.environment_profile:
            raise ValueError("config export targets another playtest environment")
        if package.format_schema_id != _AUREUS_CONFIG_FORMAT_SCHEMA_ID:
            raise ValueError("Aureus runner does not support this config export format")
        workbook = decode_aureus_config_workbook(package)
        return AureusCsvAdapter().to_ir(
            workbook,
            file_ref=f"config-export:{request.config_artifact_id}",
        )

    def _resolve_environment_contract(
        self,
        request: PlaytestEpisodeRunRequest,
    ) -> EnvironmentContractDescriptorV1 | None:
        if self.environment_contract_resolver is None:
            return None
        contract = self.environment_contract_resolver(request.environment_profile)
        package = request.config_export
        if (
            package.env_contract_version != contract.env_contract_version
            or contract.action_schema_id != _AUREUS_ACTION_SCHEMA_ID
            or contract.observation_schema_id != _AUREUS_OBSERVATION_SCHEMA_ID
        ):
            raise IntegrityViolation(
                "playtest environment contract is unsupported by the Aureus adapter"
            )
        return contract

    def _reset_quest_ids(
        self,
        request: PlaytestEpisodeRunRequest,
        *,
        contract: EnvironmentContractDescriptorV1 | None,
    ) -> tuple[str, ...]:
        binding = request.reset_binding
        if contract is not None and binding.reset_schema_id != contract.reset_schema_id:
            raise IntegrityViolation("playtest reset schema differs from the environment contract")
        if self.payload_validator is not None:
            payload = self.payload_validator.validate_exact(
                schema_id=binding.reset_schema_id,
                purpose="scenario_reset",
                payload=binding.payload,
            )
        else:
            payload = binding.payload
        if binding.reset_schema_id not in _AUREUS_RESET_SCHEMA_IDS or not isinstance(payload, dict):
            raise ValueError("Aureus runner does not support this reset schema")
        if set(payload) != {
            "scenario_id",
            "config_export_artifact_id",
            "quest_ids",
            "start_seed",
        }:
            raise ValueError("Aureus reset payload has an invalid exact shape")
        scenario_id = payload["scenario_id"]
        config_artifact_id = payload["config_export_artifact_id"]
        quest_ids = payload["quest_ids"]
        start_seed = payload["start_seed"]
        if (
            not isinstance(scenario_id, str)
            or not scenario_id
            or scenario_id != request.scenario_id
            or config_artifact_id != request.config_artifact_id
            or not isinstance(quest_ids, list)
            or any(not isinstance(quest_id, str) or not quest_id for quest_id in quest_ids)
            or len(quest_ids) != len(set(quest_ids))
            or quest_ids != sorted(quest_ids)
            or not isinstance(start_seed, int)
            or isinstance(start_seed, bool)
            or start_seed != 0
        ):
            raise ValueError("Aureus reset payload differs from exact episode authority")
        return tuple(quest_ids)


def build_aureus_playtest_runner(
    *,
    registry: ImmutablePlatformRegistry,
    oracle_registry: CompletionOracleRegistryV1,
    supported_profiles: frozenset[ProfileRefV1],
    playtest_payload_validators: Mapping[str, ExactModelPayloadValidator] | None = None,
) -> AureusPlaytestRunner:
    """Compose the Aureus playtest runner over the frozen oracle registry + executors."""

    validators = (
        playtest_payload_validators
        if playtest_payload_validators is not None
        else build_builtin_playtest_payload_validators()
    )

    return AureusPlaytestRunner(
        oracle_registry=oracle_registry,
        oracle_executors=build_completion_oracle_executors(),
        supported_profiles=supported_profiles,
        oracle_registry_resolver=registry.get_completion_oracle_registry,
        environment_contract_resolver=build_environment_contract_resolver(registry),
        payload_validator=PlaytestPayloadValidationService(
            registry=registry,
            validators=validators,
        ),
    )


def build_playtest_planner_config_resolver(
    registry: ImmutablePlatformRegistry,
) -> PlaytestPlannerConfigResolver:
    """Resolve the complete planner config through the Run-frozen catalog binding."""

    def resolve(
        binding: ResolvedExecutionProfileBindingV1,
    ) -> PlaytestPlannerProfileConfigV2:
        catalog = registry.get_execution_profile_catalog(
            binding.catalog_version,
            binding.catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation("playtest planner exact catalog is unavailable")
        definition = next(
            (item for item in catalog.definitions if item.profile == binding.profile),
            None,
        )
        lifecycle = next(
            (item for item in catalog.lifecycle if item.profile == binding.profile),
            None,
        )
        if (
            binding.expected_profile_kind != "playtest_planner"
            or definition is None
            or lifecycle is None
            or lifecycle.state not in {"active", "replay_only"}
            or definition.profile_kind != "playtest_planner"
            or definition.handler_key != "builtin_playtest_planner_profile@2"
            or definition.config_schema_id != "playtest_planner-profile-config@2"
            or execution_profile_payload_hash(definition) != binding.profile_payload_hash
        ):
            raise IntegrityViolation("playtest planner exact profile binding is invalid")
        try:
            return PlaytestPlannerProfileConfigV2.model_validate(definition.config)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("playtest planner profile config is invalid") from exc

    return resolve


def build_playtest_handler(
    *,
    registry: ImmutablePlatformRegistry,
    blobs: ArtifactBlobReader,
    store: PreparedArtifactStore,
    oracle_registry: CompletionOracleRegistryV1,
    supported_profiles: frozenset[ProfileRefV1],
    finding_head_revision: FindingHeadRevisionResolver | None = None,
    playtest_payload_validators: Mapping[str, ExactModelPayloadValidator] | None = None,
) -> PlaytestRunHandler:
    """Compose the playtest handler with the Aureus env-runner port."""

    return PlaytestRunHandler(
        blobs=blobs,
        store=store,
        env_runner=build_aureus_playtest_runner(
            registry=registry,
            oracle_registry=oracle_registry,
            supported_profiles=supported_profiles,
            playtest_payload_validators=playtest_payload_validators,
        ),
        planner_config_resolver=build_playtest_planner_config_resolver(registry),
        finding_head_revision=finding_head_revision,
    )


__all__ = [
    "AureusPlaytestRunner",
    "build_aureus_playtest_runner",
    "build_playtest_planner_config_resolver",
    "build_playtest_handler",
]
