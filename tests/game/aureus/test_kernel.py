from gameforge.contracts.env_types import parse_action
from gameforge.contracts.ir import NodeType
from gameforge.contracts.world import (
    GridSpec, Placement, QuestSpec, QuestStepSpec, ScenarioConfig, WorldConfig,
)
from gameforge.game.aureus.kernel import AureusEnv


def _wc():
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=6, height=6, blocked=[]),
        placements=[
            Placement(entity_id="npc:a", type=NodeType.NPC, pos=(1, 0), attrs={}),
            Placement(
                entity_id="interact:pile", type=NodeType.INTERACTABLE, pos=(4, 4),
                attrs={"kind": "gather", "yields_item": "item:x", "yields_count": 2},
            ),
        ],
        quests=[QuestSpec(quest_id="q", giver="npc:a", reward={"gold": 60}, steps=[
            QuestStepSpec(step_id="t", kind="talk", target="npc:a"),
            QuestStepSpec(step_id="c", kind="collect", item="item:x", count=2),
            QuestStepSpec(step_id="d", kind="turn_in", target="npc:a"),
        ])],
    )


def _navigate_until_arrived(env, target, limit=50):
    for _ in range(limit):
        r = env.step(parse_action({"kind": "navigate_to", "target": target}))
        if r.observation.last_action_result == "arrived":
            return r
    raise AssertionError(f"did not arrive at {target}")


def test_reset_is_deterministic():
    e1, e2 = AureusEnv(_wc()), AureusEnv(_wc())
    e1.reset("s", seed=7)
    e2.reset("s", seed=7)
    assert e1.state_hash() == e2.state_hash()


def test_navigate_advances_toward_target():
    e = AureusEnv(_wc())
    e.reset("s", seed=1)
    r = e.step(parse_action({"kind": "navigate_to", "target": "npc:a"}))
    assert r.observation.player_pos == (1, 0)
    assert r.observation.last_action_result == "arrived"


def test_unsupported_combat_action_is_declared_not_crashing():
    e = AureusEnv(_wc())
    e.reset("s", seed=1)
    r = e.step(parse_action({"kind": "attack", "target_id": "mob:1"}))
    assert r.observation.last_action_result == "unsupported_in_m0a"
    assert r.done is False


def test_replay_same_seed_same_per_tick_hash():
    actions = [
        {"kind": "observe"},
        {"kind": "navigate_to", "target": "npc:a"},
        {"kind": "wait", "ticks": 1},
    ]

    def run():
        e = AureusEnv(_wc())
        e.reset("s", seed=3)
        hs = [e.state_hash()]
        for a in actions:
            e.step(parse_action(a))
            hs.append(e.state_hash())
        return hs

    assert run() == run()  # contract §4.4 anchor


def test_full_quest_chain_completes():
    e = AureusEnv(_wc())
    obs = e.reset("s", seed=0)
    assert "q" in obs.known_quests

    # talk step: go to giver, interact -> accept + complete talk
    _navigate_until_arrived(e, "npc:a")
    e.step(parse_action({"kind": "interact", "target": "npc:a"}))
    assert e.observe().quest_state["q"]["step_kind"] == "collect"

    # collect step: gather 2x item:x
    _navigate_until_arrived(e, "interact:pile")
    e.step(parse_action({"kind": "interact", "target": "interact:pile"}))
    st = e.observe()
    assert st.inventory.get("item:x", 0) >= 2
    assert st.quest_state["q"]["step_kind"] == "turn_in"

    # turn_in: back to giver, interact -> complete quest
    _navigate_until_arrived(e, "npc:a")
    r = e.step(parse_action({"kind": "interact", "target": "npc:a"}))
    assert "q" in r.observation.completed_quests
    assert r.done is True
