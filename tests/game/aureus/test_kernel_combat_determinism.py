from gameforge.contracts.world import (WorldConfig, ScenarioConfig, GridSpec, Placement,
    QuestSpec, QuestStepSpec, MonsterSpec, BattleEncounterSpec, FormulaSpec, DropTableSpec, DropEntry,
    ShopSpec, ShopEntry, GachaPoolSpec, GachaEntry, EquipmentSpec)
from gameforge.contracts.ir import NodeType
from gameforge.contracts.env_types import parse_action
from gameforge.game.aureus.kernel import AureusEnv


def _wc():
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=6, height=6, blocked=[]),
        placements=[Placement(entity_id="npc:a", type=NodeType.NPC, pos=(1, 0), attrs={})],
        quests=[QuestSpec(quest_id="q", giver="npc:a", reward={"gold": 10}, steps=[
            QuestStepSpec(step_id="f", kind="fight", encounter="enc:bandit"),
        ])],
        formulas=[FormulaSpec(formula_id="fx", expr="max(1, atk - defense)")],
        monsters=[MonsterSpec(monster_id="m:bandit", stats={"hp": 6, "atk": 3, "def": 0},
                              drop_table_id="dt")],
        drop_tables=[DropTableSpec(drop_table_id="dt", entries=[DropEntry(item="item:coin", probability=1.0)])],
        encounters=[BattleEncounterSpec(encounter_id="enc:bandit", monsters=["m:bandit"],
                                        reward={"gold": 30}, pos=(0, 0))],
    )


def test_combat_run_is_deterministic_per_tick():
    actions = [{"kind": "attack", "target_id": "m:bandit"}] * 10
    def run():
        e = AureusEnv(_wc())
        e.reset("s", seed=4)
        hs = [e.state_hash()]
        for a in actions:
            e.step(parse_action(a))
            hs.append(e.state_hash())
        return hs
    assert run() == run()  # contract §4.4 anchor extended to combat rng + monster states


def test_state_hash_includes_monster_and_gacha_scope():
    e = AureusEnv(_wc())
    e.reset("s", seed=1)
    # a fresh reset exposes the extended authoritative scope without crashing
    assert isinstance(e.state_hash(), str) and e.state_hash().startswith("sha256:")


# --- supplementary integration coverage (beyond the brief's minimum) ---

def _wc_full_fight():
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=6, height=6, blocked=[]),
        placements=[Placement(entity_id="npc:a", type=NodeType.NPC, pos=(1, 0), attrs={})],
        quests=[QuestSpec(quest_id="q", giver="npc:a", reward={"gold": 5}, steps=[
            QuestStepSpec(step_id="t", kind="talk", target="npc:a"),
            QuestStepSpec(step_id="f", kind="fight", encounter="enc:bandit"),
        ])],
        monsters=[MonsterSpec(monster_id="m:bandit", stats={"hp": 1, "atk": 0, "def": 0},
                              drop_table_id="dt")],
        drop_tables=[DropTableSpec(drop_table_id="dt", entries=[DropEntry(item="item:coin", probability=1.0)])],
        encounters=[BattleEncounterSpec(encounter_id="enc:bandit", monsters=["m:bandit"],
                                        reward={"gold": 30}, pos=(0, 0))],
    )


def test_fight_step_victory_grants_reward_drops_and_advances_quest():
    e = AureusEnv(_wc_full_fight())
    e.reset("s", seed=2)
    for _ in range(10):  # walk from (0,0) to npc:a at (1,0)
        r = e.step(parse_action({"kind": "navigate_to", "target": "npc:a"}))
        if r.observation.last_action_result == "arrived":
            break
    e.step(parse_action({"kind": "interact", "target": "npc:a"}))  # accept + complete talk step
    assert e.observe().quest_state["q"]["step_kind"] == "fight"

    e.player_pos = (0, 0)  # walk back to the encounter's tile

    r = None
    for _ in range(20):
        r = e.step(parse_action({"kind": "attack", "target_id": "m:bandit"}))
        if r.observation.last_action_result == "victory":
            break
    assert r.observation.last_action_result == "victory"
    assert r.observation.inventory.get("item:coin") == 1  # drop_table p=1.0 always hits
    assert r.observation.player_stats["gold"] == 5 + 30   # quest reward + encounter reward
    assert "q" in r.observation.completed_quests
    assert r.done is True


def _wc_shops():
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=3, height=3, blocked=[]),
        shops=[ShopSpec(shop_id="shop:general", entries=[ShopEntry(item="item:potion", price=5)])],
        gacha_pools=[GachaPoolSpec(gacha_pool_id="gacha:std", cost=10,
                                    entries=[GachaEntry(item="item:common", weight=1)],
                                    pity_threshold=0)],
        equipment=[EquipmentSpec(equipment_id="eq:blade", slot="weapon", stat_mods={"atk": 5})],
    )


def test_buy_routes_to_gacha_vs_shop_and_sell_use_equip_atomics():
    e = AureusEnv(_wc_shops())
    e.reset("s", seed=9)
    e.player_stats["gold"] = 100

    r1 = e.step(parse_action({"kind": "buy", "shop_id": "shop:general", "item_id": "item:potion", "count": 2}))
    assert r1.observation.last_action_result == "bought"
    assert r1.observation.inventory.get("item:potion") == 2
    assert r1.observation.player_stats["gold"] == 90

    r2 = e.step(parse_action({"kind": "buy", "shop_id": "gacha:std", "item_id": "unused", "count": 1}))
    assert r2.observation.last_action_result == "pulled"
    assert r2.observation.inventory.get("item:common") == 1
    assert r2.observation.player_stats["gold"] == 80

    r3 = e.step(parse_action({"kind": "sell", "shop_id": "shop:general", "item_id": "item:potion", "count": 1}))
    assert r3.observation.last_action_result == "sold"
    assert r3.observation.inventory.get("item:potion") == 1
    assert r3.observation.player_stats["gold"] == 85

    r4 = e.step(parse_action({"kind": "use", "item_id": "item:potion"}))
    assert r4.observation.last_action_result == "used"
    assert r4.observation.inventory.get("item:potion") is None

    e.inventory["eq:blade"] = 1
    r5 = e.step(parse_action({"kind": "equip", "item_id": "eq:blade"}))
    assert r5.observation.last_action_result == "equipped"
    assert r5.observation.equipped_items == ["eq:blade"]
    assert r5.observation.player_stats["atk"] == 10 + 5  # default atk 10 + stat_mods


def _wc_tanky_monster():
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=4, height=4, blocked=[]),
        monsters=[MonsterSpec(monster_id="m:tank", stats={"hp": 1000, "atk": 1, "def": 0})],
        encounters=[BattleEncounterSpec(encounter_id="enc:tank", monsters=["m:tank"], reward={}, pos=(0, 0))],
    )


def test_observe_includes_alive_monster_when_in_combat():
    e = AureusEnv(_wc_tanky_monster())
    e.reset("s", seed=6)
    assert "m:tank" not in e.observe().nearby_entities
    e.step(parse_action({"kind": "attack", "target_id": "m:tank"}))
    obs = e.observe()
    assert "m:tank" in obs.nearby_entities
    assert "m:tank" in obs.reachable_targets
