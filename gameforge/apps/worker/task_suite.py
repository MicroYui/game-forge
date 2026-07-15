"""Aureus composition for ``task_suite_deriver@1`` (Task 12a).

The platform :class:`TaskSuiteDeriveHandler` is game-agnostic; this module (the
``apps`` composition boundary, which may import ``spine`` + ``game``) supplies the
GAME-SPECIFIC scenario shaping and the registry-backed environment-contract
resolver injected into it.

``AureusScenarioShaper`` derives ONE completable scenario/episode per quest chain
in the preview (a fixed, deterministic function of the preview IR — no RNG, no
LLM), binding each episode to the deterministic "all quests completed" oracle.
When a preview carries no quests it degrades to a single whole-preview scenario so
the suite is always non-empty. The reset-schema is chosen by the handler from the
profile-selected env contract, so this shaper never hardcodes the schema id.
"""

from __future__ import annotations

from dataclasses import dataclass

from gameforge.apps.worker.completion_oracles import ALL_QUESTS_COMPLETED_ORACLE
from gameforge.contracts.execution_profiles import (
    EnvironmentContractDescriptorV1,
    EnvironmentProfileDetailsV1,
    ProfileRefV1,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    PreparedArtifactStore,
)
from gameforge.platform.run_handlers.task_suite import (
    EnvironmentContractResolver,
    ScenarioDerivationRequest,
    ScenarioDraftV1,
    TaskSuiteDeriveHandler,
)

_DEFAULT_DOMAIN = "aureus"
_STEPS_PER_QUEST_STEP = 200
_MIN_STEP_BUDGET = 1
_MAX_STEP_BUDGET = 10_000_000


@dataclass(frozen=True, slots=True)
class AureusScenarioShaper:
    """One completable scenario/episode per quest chain in the preview IR."""

    def shape(self, request: ScenarioDerivationRequest) -> tuple[ScenarioDraftV1, ...]:
        graph = request.preview_snapshot.to_graph()
        quests = sorted(graph.nodes_of_type(NodeType.QUEST), key=lambda entity: entity.id)
        if not quests:
            return (self._whole_preview_scenario(request),)

        step_counts: dict[str, int] = {}
        for relation in graph.all_relations():
            if relation.type == EdgeType.HAS_STEP:
                step_counts[relation.src_id] = step_counts.get(relation.src_id, 0) + 1

        drafts: list[ScenarioDraftV1] = []
        for quest in quests:
            scenario_id = f"scenario:{quest.id}"
            reset_payload = {
                "scenario_id": scenario_id,
                "config_export_artifact_id": request.config_export_artifact_id,
                "quest_ids": [quest.id],
                "start_seed": 0,
            }
            drafts.append(
                ScenarioDraftV1(
                    scenario_id=scenario_id,
                    episode_id=f"episode:{quest.id}",
                    domain_scope=DomainScope(domain_ids=(self._domain_for(quest),)),
                    reset_payload=reset_payload,
                    completion_oracle=ALL_QUESTS_COMPLETED_ORACLE,
                    step_budget=self._step_budget(step_counts.get(quest.id, 0)),
                )
            )
        return tuple(drafts)

    @staticmethod
    def _domain_for(quest: object) -> str:
        region = getattr(quest, "attrs", {}).get("region")
        return region if isinstance(region, str) and region else _DEFAULT_DOMAIN

    @staticmethod
    def _step_budget(step_count: int) -> int:
        budget = (step_count + 1) * _STEPS_PER_QUEST_STEP
        return max(_MIN_STEP_BUDGET, min(_MAX_STEP_BUDGET, budget))

    def _whole_preview_scenario(self, request: ScenarioDerivationRequest) -> ScenarioDraftV1:
        scenario_id = f"scenario:{request.preview_snapshot.snapshot_id}"
        reset_payload = {
            "scenario_id": scenario_id,
            "config_export_artifact_id": request.config_export_artifact_id,
            "quest_ids": [],
            "start_seed": 0,
        }
        return ScenarioDraftV1(
            scenario_id=scenario_id,
            episode_id=f"episode:{request.preview_snapshot.snapshot_id}",
            domain_scope=DomainScope(domain_ids=(_DEFAULT_DOMAIN,)),
            reset_payload=reset_payload,
            completion_oracle=ALL_QUESTS_COMPLETED_ORACLE,
            step_budget=self._step_budget(0),
        )


def build_environment_contract_resolver(
    registry: ImmutablePlatformRegistry,
) -> EnvironmentContractResolver:
    """Index the frozen environment-profile contracts from the profile catalog."""

    contracts: dict[ProfileRefV1, EnvironmentContractDescriptorV1] = {}
    for catalog in registry.list_execution_profile_catalogs():
        for profile in catalog.definitions:
            if isinstance(profile.details, EnvironmentProfileDetailsV1):
                contracts[profile.profile] = profile.details.contract

    def resolve(environment_profile: ProfileRefV1) -> EnvironmentContractDescriptorV1:
        try:
            return contracts[environment_profile]
        except KeyError:
            raise KeyError(f"no environment profile contract for {environment_profile!r}") from None

    return resolve


def build_task_suite_handler(
    *,
    registry: ImmutablePlatformRegistry,
    blobs: ArtifactBlobReader,
    store: PreparedArtifactStore,
) -> TaskSuiteDeriveHandler:
    """Compose the deterministic task-suite handler with the Aureus ports."""

    return TaskSuiteDeriveHandler(
        blobs=blobs,
        store=store,
        scenario_shaper=AureusScenarioShaper(),
        environment_contract_resolver=build_environment_contract_resolver(registry),
    )


__all__ = [
    "AureusScenarioShaper",
    "build_environment_contract_resolver",
    "build_task_suite_handler",
]
