from gameforge.game.aureus.rng import CountingRandom
from gameforge.game.aureus.combat import CombatSystem
from gameforge.contracts.world import (FormulaSpec, MonsterSpec,
                                        DropTableSpec, DropEntry,
                                        SkillSpec, EffectSpec, StatusEffectSpec)


def _cs(seed=1):
    return CombatSystem(rng=CountingRandom(seed))


def test_attack_deals_deterministic_damage_and_counts_rng():
    cs = _cs(1)
    tgt = {"hp": 20}
    fx = FormulaSpec(formula_id="fx", expr="max(1, atk - defense)")
    r1 = cs.resolve_attack({"atk": 8, "defense": 2}, tgt, fx)
    assert cs.rng.draws >= 1  # hit roll consumed rng
    # replay from a fresh seed reproduces exactly
    cs2 = _cs(1)
    tgt2 = {"hp": 20}
    r2 = cs2.resolve_attack({"atk": 8, "defense": 2}, tgt2, fx)
    assert r1 == r2 and tgt["hp"] == tgt2["hp"]


def test_monster_ai_is_deterministic_policy():
    cs = _cs()
    m = MonsterSpec(monster_id="m", ai="aggressive")
    assert cs.monster_ai_action({"hp": 5}, m) == "attack"
    assert cs.monster_ai_action({"hp": 5}, MonsterSpec(monster_id="m2", ai="passive")) == "wait"


def test_roll_drops_seed_reproducible():
    dt = DropTableSpec(drop_table_id="dt", entries=[DropEntry(item="i", probability=1.0),
                                                    DropEntry(item="j", probability=0.0)])
    assert _cs(7).roll_drops(dt) == ["i"]  # p=1 always, p=0 never — deterministic
    assert _cs(7).roll_drops(dt) == _cs(7).roll_drops(dt)


# --- resolve_skill ---

def test_resolve_skill_heal_path_increases_target_hp():
    cs = _cs()
    formula = FormulaSpec(formula_id="heal_f", expr="power")
    skill = SkillSpec(skill_id="heal", power=50, formula_id="heal_f", target="self")
    target = {"hp": 50}
    result = cs.resolve_skill(
        skill, {"atk": 10}, target,
        formulas={"heal_f": formula}, effects={}, status_effects={},
    )
    assert result["kind"] == "heal"
    assert result["amount"] == 50
    assert target["hp"] == 100


def test_resolve_skill_damage_path_scales_by_formula():
    cs = _cs()
    formula = FormulaSpec(formula_id="dmg_f", expr="atk*power//100")
    skill = SkillSpec(skill_id="fireball", power=150, formula_id="dmg_f", target="enemy")
    target = {"hp": 100}
    result = cs.resolve_skill(
        skill, {"atk": 20}, target,
        formulas={"dmg_f": formula}, effects={}, status_effects={},
    )
    assert result["kind"] == "damage"
    assert result["amount"] == 30  # 20 * 150 // 100
    assert target["hp"] == 70


def test_resolve_skill_queues_applied_status_onto_target():
    cs = _cs()
    status_effect = StatusEffectSpec(status_effect_id="poison", effect_id="poison_dot", duration=3)
    skill = SkillSpec(skill_id="poison_strike", power=10, target="enemy", applies_status="poison")
    target = {"hp": 100}
    result = cs.resolve_skill(
        skill, {"atk": 5}, target,
        formulas={}, effects={}, status_effects={"poison": status_effect},
    )
    assert result["status_applied"] == "poison"
    assert target["status_effects"] == [{"effect_id": "poison_dot", "remaining": 3}]


# --- tick_status_effects ---

def test_tick_status_effects_dot_reduces_hp_and_decrements_duration():
    cs = _cs()
    effect = EffectSpec(effect_id="poison_dot", kind="dot", magnitude=5, duration=3)
    state = {"hp": 100, "status_effects": [{"effect_id": "poison_dot", "remaining": 2}]}
    cs.tick_status_effects(state, {"poison_dot": effect})
    assert state["hp"] == 95
    assert state["status_effects"] == [{"effect_id": "poison_dot", "remaining": 1}]


def test_tick_status_effects_heal_applies_per_tick_and_expires():
    cs = _cs()
    effect = EffectSpec(effect_id="regen", kind="heal", magnitude=10, duration=3)
    state = {"hp": 50, "status_effects": [{"effect_id": "regen", "remaining": 1}]}
    cs.tick_status_effects(state, {"regen": effect})
    assert state["hp"] == 60
    # duration hit 0 this tick -> removed from the entity's active-effects list
    assert state["status_effects"] == []


def test_tick_status_effects_buff_only_decrements_duration():
    cs = _cs()
    effect = EffectSpec(effect_id="atk_buff", kind="buff", stat="atk", magnitude=5, duration=3)
    state = {"hp": 100, "status_effects": [{"effect_id": "atk_buff", "remaining": 2}]}
    cs.tick_status_effects(state, {"atk_buff": effect})
    assert state["hp"] == 100  # buff magnitude applied once at queue time, not per tick
    assert state["status_effects"] == [{"effect_id": "atk_buff", "remaining": 1}]
