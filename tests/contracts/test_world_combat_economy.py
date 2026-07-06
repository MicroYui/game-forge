from gameforge.contracts.world import (
    WorldConfig, ScenarioConfig, GridSpec, QuestStepSpec,
    MonsterSpec, DropTableSpec, DropEntry, BattleEncounterSpec, ShopSpec,
    ShopEntry, GachaPoolSpec, GachaEntry, SkillSpec, FormulaSpec, EquipmentSpec,
)

def test_fight_step_kind_and_encounter():
    s = QuestStepSpec(step_id="f", kind="fight", encounter="enc:bandits")
    assert s.kind == "fight" and s.encounter == "enc:bandits"

def test_worldconfig_carries_combat_economy_and_defaults_empty():
    wc = WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=3, height=3, blocked=[]),
    )
    assert wc.monsters == [] and wc.gacha_pools == [] and wc.env_contract_version == "env@1"

def test_full_combat_economy_config_validates():
    wc = WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=3, height=3),
        formulas=[FormulaSpec(formula_id="fx:atk", expr="max(1, atk*power//100 - defense)")],
        skills=[SkillSpec(skill_id="sk:slash", power=120, formula_id="fx:atk")],
        equipment=[EquipmentSpec(equipment_id="eq:blade", slot="weapon", stat_mods={"atk": 5})],
        monsters=[MonsterSpec(monster_id="m:bandit", stats={"hp": 20, "atk": 6, "def": 1},
                              skills=["sk:slash"], drop_table_id="dt:bandit")],
        drop_tables=[DropTableSpec(drop_table_id="dt:bandit",
                                   entries=[DropEntry(item="item:coin", probability=0.5)])],
        encounters=[BattleEncounterSpec(encounter_id="enc:bandits", monsters=["m:bandit"],
                                        reward={"gold": 30}, pos=(2, 2))],
        shops=[ShopSpec(shop_id="shop:general",
                        entries=[ShopEntry(item="item:potion", price=10)])],
        gacha_pools=[GachaPoolSpec(gacha_pool_id="gp:std", cost=100,
                                   entries=[GachaEntry(item="item:rare", weight=1),
                                            GachaEntry(item="item:common", weight=9)],
                                   pity_threshold=10, pity_item="item:rare")],
    )
    rt = WorldConfig.model_validate(wc.model_dump())
    assert rt.gacha_pools[0].pity_threshold == 10
    assert rt.drop_tables[0].entries[0].probability == 0.5
