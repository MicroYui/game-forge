"""Deterministic Aureus completion-oracle executors (Task 12a).

The oracle DEFINITION / registry is platform contract (frozen in the registry
defaults: ``state-predicate`` / ``bounded-progress``). The EXECUTOR that turns a
scenario's :class:`CompletionOracleRefV1` into a verdict is the GAME-SPECIFIC
piece, injected at the composition root into
``TrustedComponentMaps.completion_oracles`` so platform readiness closes the
executor-key set exactly. The verdict is DETERMINISTIC — it is the Aureus env's
own terminal signal (``AureusEnv._all_quests_completed`` / ``env.done``), never an
LLM judgment. ``apps`` is the one layer permitted to bind ``platform`` contracts
to a concrete ``gameforge.game`` environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from gameforge.contracts.playtest import CompletionOracleRefV1
from gameforge.game.aureus.kernel import AureusEnv

STATE_PREDICATE_ORACLE_KEY = "state_predicate_oracle@1"
BOUNDED_PROGRESS_ORACLE_KEY = "bounded_progress_oracle@1"

_ALL_QUESTS_COMPLETED = "all_quests_completed"

# The scenario-derivation completion oracle: "all quests completed" expressed as a
# state predicate resolving to the frozen ``state-predicate@1`` registry
# definition. Its executor is bound to ``AureusEnv._all_quests_completed``.
ALL_QUESTS_COMPLETED_ORACLE = CompletionOracleRefV1(
    oracle_id="state-predicate",
    version=1,
    params_schema_id="state-predicate-params@1",
    params={"predicate": _ALL_QUESTS_COMPLETED},
)


@dataclass(frozen=True, slots=True)
class AureusStatePredicateOracle:
    """Evaluate a state predicate against the Aureus env's deterministic signal."""

    def evaluate(self, env: AureusEnv, params: Mapping[str, object]) -> bool:
        predicate = params.get("predicate")
        if predicate == _ALL_QUESTS_COMPLETED:
            # env.done / terminal signal — deterministic, never an LLM claim.
            return env._all_quests_completed()
        raise ValueError(f"unsupported completion state predicate {predicate!r}")


@dataclass(frozen=True, slots=True)
class AureusBoundedProgressOracle:
    """Deterministic bounded-progress verdict from the env's terminal signal."""

    def evaluate(self, env: AureusEnv, params: Mapping[str, object]) -> bool:
        return env._all_quests_completed()


def build_completion_oracle_executors() -> dict[str, object]:
    """The trusted ``executor_key -> oracle`` map for the composition root.

    Keys MUST cover exactly the frozen completion-oracle registry executor keys so
    ``PlatformReadinessValidator`` closes the ``completion_oracles`` set.
    """

    return {
        STATE_PREDICATE_ORACLE_KEY: AureusStatePredicateOracle(),
        BOUNDED_PROGRESS_ORACLE_KEY: AureusBoundedProgressOracle(),
    }


__all__ = [
    "ALL_QUESTS_COMPLETED_ORACLE",
    "BOUNDED_PROGRESS_ORACLE_KEY",
    "STATE_PREDICATE_ORACLE_KEY",
    "AureusBoundedProgressOracle",
    "AureusStatePredicateOracle",
    "build_completion_oracle_executors",
]
