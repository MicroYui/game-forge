import pytest
from pydantic import ValidationError

from gameforge.contracts.env_types import HIGH_LEVEL_MACROS, Observation, StepResult, parse_action


def test_action_discriminated_union():
    a = parse_action({"kind": "navigate_to", "target": "npc:lincheng"})
    assert a.kind == "navigate_to" and a.target == "npc:lincheng"


def test_combat_actions_defined_now():
    # combat/economy atomic actions are declared in M0a (impl@M0b), not cut
    a = parse_action({"kind": "attack", "target_id": "mob:1"})
    assert a.kind == "attack"
    b = parse_action({"kind": "buy", "shop_id": "shop:1", "item_id": "item:x", "count": 2})
    assert b.kind == "buy" and b.count == 2


@pytest.mark.parametrize(
    "payload",
    (
        {"kind": "observe", "forged": True},
        {"kind": "wait", "ticks": True},
        {"kind": "wait", "ticks": "1"},
        {
            "kind": "buy",
            "shop_id": "shop:1",
            "item_id": "item:x",
            "count": False,
        },
        {
            "kind": "buy",
            "shop_id": "shop:1",
            "item_id": "item:x",
            "count": "1",
        },
        {
            "kind": "sell",
            "shop_id": "shop:1",
            "item_id": "item:x",
            "count": True,
        },
        {
            "kind": "sell",
            "shop_id": "shop:1",
            "item_id": "item:x",
            "count": "1",
        },
    ),
)
def test_parse_action_rejects_extra_and_non_strict_integer_inputs(payload):
    with pytest.raises(ValidationError):
        parse_action(payload)


def test_observation_has_all_contract_fields():
    fields = set(Observation.model_fields.keys())
    for f in [
        "tick",
        "player_pos",
        "player_stats",
        "equipped_items",
        "active_effects",
        "active_quests",
        "completed_quests",
        "known_quests",
        "quest_state",
        "inventory",
        "hp",
        "nearby_entities",
        "reachable_targets",
        "available_interactions",
        "visible_map",
        "dialogue_options",
        "last_action_result",
        "logs",
    ]:
        assert f in fields, f


def test_step_result_shape():
    obs = Observation(tick=0, player_pos=(0, 0), hp=100)
    sr = StepResult(observation=obs, reward=0.0, done=False, info={})
    assert sr.done is False and sr.observation.tick == 0


def test_macros_are_planner_layer():
    assert HIGH_LEVEL_MACROS == ("accept_quest", "turn_in", "talk")
