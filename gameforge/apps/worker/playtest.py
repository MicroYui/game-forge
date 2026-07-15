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
from typing import Mapping

from gameforge.agents.playtest.agent import PlaytestAgent
from gameforge.agents.playtest.memory import MemTrace
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.apps.worker.completion_oracles import build_completion_oracle_executors
from gameforge.contracts.agent_io import PlaytestInput
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.playtest import (
    CompletionOracleRegistryV1,
    resolve_completion_oracle,
)
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    PreparedArtifactStore,
)
from gameforge.platform.run_handlers.playtest import (
    PlaytestEpisodeOutcomeV1,
    PlaytestEpisodeRunRequest,
    PlaytestRunHandler,
)


@dataclass(frozen=True, slots=True)
class AureusPlaytestRunner:
    """Drive the M2b ``PlaytestAgent`` against a seeded Aureus env + eval the oracle."""

    oracle_registry: CompletionOracleRegistryV1
    oracle_executors: Mapping[str, object]
    supported_profiles: frozenset[ProfileRefV1]

    def supports(self, environment_profile: ProfileRefV1) -> bool:
        return environment_profile in self.supported_profiles

    def run_episode(self, request: PlaytestEpisodeRunRequest) -> PlaytestEpisodeOutcomeV1:
        if not self.supports(request.environment_profile):
            # Defense-in-depth: the platform already gated on ``supports``.
            raise ValueError(f"unsupported environment profile {request.environment_profile!r}")

        world = snapshot_to_world(request.preview_snapshot)
        env = AureusEnv(world)
        env.reset(request.scenario_id, request.seed)

        memory = MemTrace() if request.memory_enabled else None
        report = PlaytestAgent().run(
            PlaytestInput(scenario=request.scenario_id, seed=request.seed),
            env,
            request.router,
            use_planner=request.use_planner,
            memory=memory,
            max_steps=request.max_steps,
        )

        # DETERMINISTIC completion verdict via the frozen registry + trusted executor.
        definition = resolve_completion_oracle(
            self.oracle_registry,
            request.completion_oracle_registry_ref,
            request.completion_oracle,
        )
        executor = self.oracle_executors[definition.executor_key]
        completed = bool(executor.evaluate(env, request.completion_oracle.params))

        return PlaytestEpisodeOutcomeV1(
            action_trace=tuple(report.action_trace),
            defect_findings=tuple(report.defect_findings),
            completed=completed,
        )


def build_aureus_playtest_runner(
    *,
    registry: ImmutablePlatformRegistry,
    oracle_registry: CompletionOracleRegistryV1,
    supported_profiles: frozenset[ProfileRefV1],
) -> AureusPlaytestRunner:
    """Compose the Aureus playtest runner over the frozen oracle registry + executors."""

    return AureusPlaytestRunner(
        oracle_registry=oracle_registry,
        oracle_executors=build_completion_oracle_executors(),
        supported_profiles=supported_profiles,
    )


def build_playtest_handler(
    *,
    registry: ImmutablePlatformRegistry,
    blobs: ArtifactBlobReader,
    store: PreparedArtifactStore,
    oracle_registry: CompletionOracleRegistryV1,
    supported_profiles: frozenset[ProfileRefV1],
) -> PlaytestRunHandler:
    """Compose the playtest handler with the Aureus env-runner port."""

    return PlaytestRunHandler(
        blobs=blobs,
        store=store,
        env_runner=build_aureus_playtest_runner(
            registry=registry,
            oracle_registry=oracle_registry,
            supported_profiles=supported_profiles,
        ),
    )


__all__ = [
    "AureusPlaytestRunner",
    "build_aureus_playtest_runner",
    "build_playtest_handler",
]
