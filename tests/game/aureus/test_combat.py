from gameforge.game.aureus.rng import CountingRandom
from gameforge.game.aureus.combat import CombatSystem
from gameforge.contracts.world import (FormulaSpec, MonsterSpec,
                                        DropTableSpec, DropEntry)


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
