from gameforge.contracts.ir import NodeType
from gameforge.contracts.world import (
    GridSpec, Placement, QuestSpec, QuestStepSpec, ScenarioConfig, ShopEntry, WorldConfig,
)


def _wc():
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s1", start_pos=(0, 0)),
        grid=GridSpec(width=5, height=5, blocked=[]),
        placements=[Placement(entity_id="npc:a", type=NodeType.NPC, pos=(1, 1), attrs={})],
        quests=[
            QuestSpec(
                quest_id="q1", giver="npc:a",
                steps=[QuestStepSpec(step_id="s1", kind="talk", target="npc:a")],
                reward={"gold": 50},
            )
        ],
    )


def test_worldconfig_env_contract_version_default():
    assert _wc().env_contract_version == "env@1"


def test_worldconfig_roundtrips_via_pydantic():
    wc = _wc()
    again = WorldConfig.model_validate(wc.model_dump())
    assert again.quests[0].steps[0].kind == "talk"
    assert again.grid.width == 5


def test_quest_step_kinds_restricted():
    import pytest
    from pydantic import ValidationError

    for kind in ["talk", "collect", "turn_in", "fight"]:  # fight added in M0b
        QuestStepSpec(step_id="s", kind=kind)
    with pytest.raises(ValidationError):
        QuestStepSpec(step_id="s", kind="bogus")  # kind remains a restricted literal set


def test_shop_entry_buy_prob_defaults_to_none():
    e = ShopEntry(item="item:x", price=50)
    assert e.buy_prob is None
    assert e.currency == "gold"


def test_shop_entry_accepts_optional_buy_prob():
    e = ShopEntry(item="item:x", price=50, currency="gold", buy_prob=0.5)
    assert e.buy_prob == 0.5


def test_shop_entry_construction_from_entry_dict_with_buy_prob():
    # This is the EXACT call snapshot_to_world makes (ir_to_world.py:170):
    # ShopEntry(**entry). A buy_prob key in the entries JSON must not raise.
    entry = {"item": "item:x", "price": 50, "currency": "gold", "buy_prob": 0.5}
    e = ShopEntry(**entry)
    assert e.buy_prob == 0.5 and e.price == 50
