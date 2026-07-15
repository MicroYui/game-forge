"""Task 12b — Aureus completion-oracle executor semantics.

The bounded-progress oracle has REAL bounded-progress semantics (completed-quest
fraction ≥ ``min_completed_quest_fraction``), genuinely distinct from the
state-predicate ``all_quests_completed`` oracle. Both verdicts are DETERMINISTIC
(read off the env's own quest state), never an LLM claim.
"""

from __future__ import annotations

from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.apps.worker.completion_oracles import (
    AureusBoundedProgressOracle,
    AureusStatePredicateOracle,
)
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.spine.ir.loader import load_scenario


def _caravan_env() -> AureusEnv:
    world = snapshot_to_world(load_scenario("scenarios/caravan.yaml"))
    env = AureusEnv(world)
    env.reset(world.scenario.scenario_id, 0)
    return env


def test_bounded_progress_oracle_respects_fraction_threshold() -> None:
    env = _caravan_env()  # 1 known quest, not completed → fraction 0/1.
    bounded = AureusBoundedProgressOracle()
    state = AureusStatePredicateOracle()

    # A 0.0 threshold accepts zero progress — DISTINCT from the state predicate,
    # which requires EVERY quest completed.
    assert bounded.evaluate(env, {"min_completed_quest_fraction": 0.0}) is True
    assert bounded.evaluate(env, {"min_completed_quest_fraction": 1.0}) is False
    assert state.evaluate(env, {"predicate": "all_quests_completed"}) is False

    # Complete the (single) quest → fraction 1/1; both verdicts now agree.
    for qid in env.quest_states:
        env.quest_states[qid]["status"] = "completed"
    assert bounded.evaluate(env, {"min_completed_quest_fraction": 1.0}) is True
    assert state.evaluate(env, {"predicate": "all_quests_completed"}) is True


def test_bounded_progress_oracle_defaults_to_full_completion() -> None:
    env = _caravan_env()
    bounded = AureusBoundedProgressOracle()
    # Default (no param) requires full completion, matching the state predicate.
    assert bounded.evaluate(env, {}) is False
    for qid in env.quest_states:
        env.quest_states[qid]["status"] = "completed"
    assert bounded.evaluate(env, {}) is True


def test_bounded_progress_oracle_reads_partial_fraction() -> None:
    env = _caravan_env()
    # Inject a synthetic 4-quest state to exercise a genuine partial fraction.
    env.quest_states = {
        "q1": {"status": "completed", "current_step": 0},
        "q2": {"status": "completed", "current_step": 0},
        "q3": {"status": "active", "current_step": 0},
        "q4": {"status": "known", "current_step": 0},
    }
    bounded = AureusBoundedProgressOracle()
    assert bounded.evaluate(env, {"min_completed_quest_fraction": 0.5}) is True  # 2/4
    assert bounded.evaluate(env, {"min_completed_quest_fraction": 0.75}) is False


def test_bounded_progress_oracle_no_quests_makes_no_progress() -> None:
    env = _caravan_env()
    env.quest_states = {}
    bounded = AureusBoundedProgressOracle()
    assert bounded.evaluate(env, {"min_completed_quest_fraction": 0.0}) is False
