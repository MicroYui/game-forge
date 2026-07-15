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
_MIN_COMPLETED_QUEST_FRACTION = "min_completed_quest_fraction"

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
    """Deterministic bounded-progress verdict: completed-quest fraction ≥ threshold.

    Unlike the state-predicate ``all_quests_completed`` oracle (which requires EVERY
    known quest to be completed), the bounded-progress oracle reads the fraction of
    known quests completed within the bounded playthrough (the agent already stopped
    at the step budget) and compares it to the ``min_completed_quest_fraction`` param
    (default ``1.0``). With a threshold below 1.0 this is a genuinely weaker, distinct
    verdict — e.g. ``0.5`` accepts a run that completed half the quest chains. The
    verdict is DETERMINISTIC (read straight off the env's quest state), never an LLM
    claim. An env with no quests can make no progress, so the verdict is ``False``.
    """

    def evaluate(self, env: AureusEnv, params: Mapping[str, object]) -> bool:
        quest_states = getattr(env, "quest_states", {})
        if not quest_states:
            return False
        threshold = params.get(_MIN_COMPLETED_QUEST_FRACTION, 1.0)
        completed = sum(1 for state in quest_states.values() if state.get("status") == "completed")
        fraction = completed / len(quest_states)
        return fraction >= float(threshold)


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
