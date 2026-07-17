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
from typing import Mapping

from gameforge.apps.worker.completion_oracles import ALL_QUESTS_COMPLETED_ORACLE
from gameforge.contracts.execution_profiles import (
    EnvironmentContractDescriptorV1,
    EnvironmentProfileDetailsV1,
    ProfileRefV1,
    TaskSuiteDerivationProfileConfigV2,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.playtest_payload_schemas import (
    ExactModelPayloadValidator,
    PlaytestPayloadValidationService,
    build_builtin_playtest_payload_validators,
)
from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    PreparedArtifactStore,
    trust_typed_profile_binding,
)
from gameforge.platform.run_handlers.task_suite import (
    EnvironmentContractResolver,
    ScenarioDerivationRequest,
    ScenarioDraftV1,
    ScenarioShaper,
    ScenarioShaperResolver,
    TaskSuiteDeriveHandler,
)

_STEPS_PER_QUEST_STEP = 200
_MIN_STEP_BUDGET = 1
_MAX_STEP_BUDGET = 10_000_000
_AUREUS_DERIVATION_HANDLER_KEY = "builtin_task_suite_derivation_profile@1"
_AUREUS_DERIVATION_HANDLER_KEY_V2 = "builtin_task_suite_derivation_profile@2"
_AUREUS_ENVIRONMENT_HANDLER_KEY = "builtin_environment_profile@1"


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
                    domain_scope=request.domain_scope,
                    reset_payload=reset_payload,
                    completion_oracle=ALL_QUESTS_COMPLETED_ORACLE,
                    step_budget=self._step_budget(step_counts.get(quest.id, 0)),
                )
            )
        return tuple(drafts)

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
            domain_scope=request.domain_scope,
            reset_payload=reset_payload,
            completion_oracle=ALL_QUESTS_COMPLETED_ORACLE,
            step_budget=self._step_budget(0),
        )


def build_environment_contract_resolver(
    registry: ImmutablePlatformRegistry,
) -> EnvironmentContractResolver:
    """Index exact retained contracts for the trusted Aureus environment handler."""

    retained_authority: dict[ProfileRefV1, tuple[str, EnvironmentContractDescriptorV1]] = {}
    for catalog in registry.list_execution_profile_catalogs():
        for profile in catalog.definitions:
            if profile.profile_kind != "environment":
                continue
            if not isinstance(profile.details, EnvironmentProfileDetailsV1):
                raise IntegrityViolation(
                    "environment profile has an incompatible details variant",
                    profile_id=profile.profile.profile_id,
                    profile_version=profile.profile.version,
                )
            authority = (profile.handler_key, profile.details.contract)
            previous = retained_authority.get(profile.profile)
            if previous is not None and previous != authority:
                raise IntegrityViolation(
                    "environment profile ref has conflicting retained authority",
                    profile_id=profile.profile.profile_id,
                    profile_version=profile.profile.version,
                )
            retained_authority[profile.profile] = authority

    def resolve(environment_profile: ProfileRefV1) -> EnvironmentContractDescriptorV1:
        try:
            handler_key, contract = retained_authority[environment_profile]
        except KeyError:
            raise IntegrityViolation(
                "environment profile contract is unavailable",
                profile_id=environment_profile.profile_id,
                profile_version=environment_profile.version,
            ) from None
        if handler_key != _AUREUS_ENVIRONMENT_HANDLER_KEY:
            raise IntegrityViolation(
                "environment profile has no trusted Aureus handler",
                profile_id=environment_profile.profile_id,
                profile_version=environment_profile.version,
                handler_key=handler_key,
            )
        return contract

    return resolve


def build_derivation_config_resolver(
    registry: ImmutablePlatformRegistry,
):
    """Resolve immutable TaskSuite profile config by exact ProfileRef."""

    configs: dict[ProfileRefV1, TaskSuiteDerivationProfileConfigV2] = {}
    for catalog in registry.list_execution_profile_catalogs():
        for profile in catalog.definitions:
            if profile.profile_kind != "task_suite_derivation":
                continue
            if profile.config_schema_id != "task_suite_derivation-profile-config@2":
                continue
            config = TaskSuiteDerivationProfileConfigV2.model_validate(profile.config)
            retained = configs.get(profile.profile)
            if retained is not None and retained != config:
                raise IntegrityViolation(
                    "task-suite profile ref has conflicting retained config",
                    profile_id=profile.profile.profile_id,
                    profile_version=profile.profile.version,
                )
            configs[profile.profile] = config

    def resolve(profile: ProfileRefV1) -> TaskSuiteDerivationProfileConfigV2:
        try:
            return configs[profile]
        except KeyError:
            raise IntegrityViolation(
                "task-suite derivation profile config is unavailable",
                profile_id=profile.profile_id,
                profile_version=profile.version,
            ) from None

    return resolve


def build_scenario_shaper_resolver(
    registry: ImmutablePlatformRegistry,
):
    """Bind each retained derivation profile to an explicit trusted shaper key."""

    trusted_shapers: dict[str, ScenarioShaper] = {
        _AUREUS_DERIVATION_HANDLER_KEY: AureusScenarioShaper(),
        _AUREUS_DERIVATION_HANDLER_KEY_V2: AureusScenarioShaper(),
    }
    by_profile: dict[ProfileRefV1, ScenarioShaper] = {}
    for catalog in registry.list_execution_profile_catalogs():
        for profile in catalog.definitions:
            if profile.profile_kind != "task_suite_derivation":
                continue
            shaper = trusted_shapers.get(profile.handler_key)
            if shaper is None:
                raise IntegrityViolation(
                    "task-suite derivation profile has no trusted scenario shaper",
                    handler_key=profile.handler_key,
                )
            retained = by_profile.get(profile.profile)
            if retained is not None and type(retained) is not type(shaper):
                raise IntegrityViolation("task-suite profile ref has conflicting retained shapers")
            by_profile[profile.profile] = shaper

    def resolve(profile: ProfileRefV1) -> ScenarioShaper:
        try:
            return by_profile[profile]
        except KeyError:
            raise IntegrityViolation(
                "task-suite scenario shaper is unavailable",
                profile_id=profile.profile_id,
                profile_version=profile.version,
            ) from None

    return resolve


def build_task_suite_handler(
    *,
    registry: ImmutablePlatformRegistry,
    blobs: ArtifactBlobReader,
    store: PreparedArtifactStore,
    playtest_payload_validators: Mapping[str, ExactModelPayloadValidator] | None = None,
    scenario_shaper_resolver: ScenarioShaperResolver | None = None,
    profile_binding_validator: ExactProfileBindingValidator = trust_typed_profile_binding,
) -> TaskSuiteDeriveHandler:
    """Compose the deterministic task-suite handler with the Aureus ports."""

    validators = (
        playtest_payload_validators
        if playtest_payload_validators is not None
        else build_builtin_playtest_payload_validators()
    )

    return TaskSuiteDeriveHandler(
        blobs=blobs,
        store=store,
        scenario_shaper_resolver=(
            scenario_shaper_resolver
            if scenario_shaper_resolver is not None
            else build_scenario_shaper_resolver(registry)
        ),
        environment_contract_resolver=build_environment_contract_resolver(registry),
        derivation_config_resolver=build_derivation_config_resolver(registry),
        completion_oracle_registry_resolver=registry.get_completion_oracle_registry,
        payload_validator=PlaytestPayloadValidationService(
            registry=registry,
            validators=validators,
        ),
        profile_binding_validator=profile_binding_validator,
    )


__all__ = [
    "AureusScenarioShaper",
    "build_derivation_config_resolver",
    "build_environment_contract_resolver",
    "build_scenario_shaper_resolver",
    "build_task_suite_handler",
]
