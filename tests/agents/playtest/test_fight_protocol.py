"""TDD (deterministic, no LLM): the fight protocol contract.

Diagnosis (task #36, live record #1 = 0% completion in every mode): the
Executor spammed `attack` from wherever it stood, and the engine correctly
answered `not_in_combat` forever — combat can only start once the player
stands ON the monster's tile. The fix threads an explicit
`Observation.pending_fight_targets` field (contract §4.3 actionability) end to
end: kernel `observe()` populates it, `abstract_state` surfaces it plus a
deterministic FIGHT PROTOCOL hint, and the executor prompt (`playtest@2`)
teaches the same protocol. This test drives `AureusEnv` directly (chain #0 of
the generated corpus) — ScriptedDriver-style, zero LLM calls — to prove the
field appears exactly where expected and that a protocol-following caller can
actually complete the fight step.
"""
from __future__ import annotations

from gameforge.agents.playtest.state import abstract_state
from gameforge.agents.playtest_harness import default_chain_snapshots
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.contracts.env_types import Attack, Interact, NavigateTo
from gameforge.game.aureus.kernel import AureusEnv

_MONSTER = "mon:0_0_0"
_NPC = "npc:0_0_0"
_GATHER = "gather:0_0_0"


def _fresh_env() -> AureusEnv:
    snapshot = default_chain_snapshots()[0]
    world_config = snapshot_to_world(snapshot)
    env = AureusEnv(world_config)
    env.reset(world_config.scenario.scenario_id, 0)
    return env


def _drive_to_fight_step(env: AureusEnv):
    """talk -> collect -> (now at the `fight` step). Mirrors the manual
    diagnosis: nav npc (x2, until arrived), interact npc, nav gather,
    interact gather."""
    env.step(NavigateTo(target=_NPC))
    result = env.step(NavigateTo(target=_NPC))
    assert result.observation.last_action_result == "arrived"
    result = env.step(Interact(target=_NPC))
    assert result.observation.last_action_result == "quest_accepted"
    result = env.step(NavigateTo(target=_GATHER))
    assert result.observation.last_action_result == "arrived"
    result = env.step(Interact(target=_GATHER))
    assert result.observation.last_action_result == "gathered"
    return result.observation


def test_pending_fight_targets_populated_at_fight_step():
    env = _fresh_env()
    obs = _drive_to_fight_step(env)
    assert obs.quest_state["quest:0_0_0"]["step_kind"] == "fight"
    assert env.observe().pending_fight_targets == [_MONSTER]


def test_abstract_state_surfaces_fight_protocol_only_at_fight_step():
    env = _fresh_env()

    initial_obs = env.observe()
    initial_state = abstract_state(initial_obs)
    assert "FIGHT PROTOCOL" not in initial_state

    _drive_to_fight_step(env)
    fight_obs = env.observe()
    fight_state = abstract_state(fight_obs)
    assert "FIGHT PROTOCOL" in fight_state
    assert _MONSTER in fight_state


def test_observe_does_not_change_state_hash():
    env = _fresh_env()
    _drive_to_fight_step(env)
    before = env.state_hash()
    env.observe()  # calling observe() again must not mutate authoritative state
    after = env.state_hash()
    assert before == after


def test_navigate_then_attack_protocol_completes_the_fight_step():
    env = _fresh_env()
    _drive_to_fight_step(env)

    # navigate_to the pending fight target until 'arrived' — this is the step
    # the buggy agent skipped, attacking from off-tile instead.
    result = env.step(NavigateTo(target=_MONSTER))
    while result.observation.last_action_result == "moving":
        result = env.step(NavigateTo(target=_MONSTER))
    assert result.observation.last_action_result == "arrived"

    # THEN attack repeatedly until victory.
    outcomes = []
    for _ in range(20):
        result = env.step(Attack(target_id=_MONSTER))
        outcomes.append(result.observation.last_action_result)
        if result.observation.last_action_result == "victory":
            break
    assert "victory" in outcomes
    assert "not_in_combat" not in outcomes

    # The quest advances past `fight` to `turn_in`.
    assert result.observation.quest_state["quest:0_0_0"]["step_kind"] == "turn_in"

    # Finish the chain to be thorough: turn_in completes the quest.
    env.step(NavigateTo(target=_NPC))
    final = env.step(Interact(target=_NPC))
    assert final.observation.quest_state["quest:0_0_0"]["status"] == "completed"
    assert final.done is True
