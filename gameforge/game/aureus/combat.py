"""Aureus combat system (M0b): attacks, skills, monster AI, status ticks, drops.

`CombatSystem` is bound to a single `CountingRandom` instance (contract §4.4:
rng draws are part of authoritative state, so the kernel owns one rng and
threads it through every combat call in deterministic order). All damage
math flows through `formula.safe_eval` — never raw python eval — so
content-authored formulas stay sandboxed.

`FormulaError` is re-exported here (imported from `formula.py`) since combat
callers need to catch it without importing the formula module directly.
"""

from __future__ import annotations

from gameforge.contracts.world import (
    DropTableSpec, EffectSpec, FormulaSpec, MonsterSpec, SkillSpec,
    StatusEffectSpec,
)
from gameforge.game.aureus.formula import FormulaError, safe_eval
from gameforge.game.aureus.rng import CountingRandom

__all__ = ["CombatSystem", "FormulaError"]

_BASE_HIT_CHANCE = 0.9
_BASE_ATTACK_POWER = 100


class CombatSystem:
    def __init__(self, rng: CountingRandom) -> None:
        self.rng = rng

    # --- basic attack ---
    def resolve_attack(
        self,
        attacker_stats: dict,
        target_state: dict,
        formula: FormulaSpec | None = None,
    ) -> dict:
        """Resolve a plain attack. Hit roll always consumes one rng draw
        (even on formula errors we still want the caller to see them), then
        damage is computed via `formula` if given, else `max(1, atk - defense)`.
        Mutates `target_state["hp"]` only on hit.
        """
        hit = self.rng.roll(_BASE_HIT_CHANCE)
        if formula is not None:
            names = {**attacker_stats, "power": _BASE_ATTACK_POWER}
            damage = safe_eval(formula.expr, names)
        else:
            atk = int(attacker_stats.get("atk", 0))
            defense = int(attacker_stats.get("defense", 0))
            damage = max(1, atk - defense)
        damage = max(0, int(damage))

        if hit:
            target_state["hp"] = target_state.get("hp", 0) - damage
        else:
            damage = 0
        return {"damage": damage, "hit": hit}

    # --- skills ---
    def resolve_skill(
        self,
        skill: SkillSpec,
        caster_stats: dict,
        target_state: dict,
        formulas: dict[str, FormulaSpec],
        effects: dict[str, EffectSpec],
        status_effects: dict[str, StatusEffectSpec],
    ) -> dict:
        """Apply a skill's formula-scaled damage/heal to `target_state`, then
        queue any status effect it applies. `target` on the SkillSpec decides
        polarity: "self"/"ally" heals, "enemy" damages. `formulas`/`effects`/
        `status_effects` are id-keyed indices (built once by the kernel from
        the WorldConfig lists) so lookups here are O(1) and draw order stays
        independent of index construction order.
        """
        formula = formulas.get(skill.formula_id) if skill.formula_id else None
        names = {**caster_stats, "power": skill.power}
        if formula is not None:
            amount = safe_eval(formula.expr, names)
        else:
            amount = int(caster_stats.get("atk", 0)) * skill.power // _BASE_ATTACK_POWER
        amount = max(0, int(amount))

        is_heal = skill.target in ("self", "ally")
        if is_heal:
            target_state["hp"] = target_state.get("hp", 0) + amount
        else:
            target_state["hp"] = target_state.get("hp", 0) - amount

        status_applied = None
        if skill.applies_status:
            status_effect = status_effects.get(skill.applies_status)
            if status_effect is not None:
                target_state.setdefault("status_effects", []).append(
                    {"effect_id": status_effect.effect_id, "remaining": status_effect.duration}
                )
                status_applied = status_effect.status_effect_id

        return {"amount": amount, "kind": "heal" if is_heal else "damage", "status_applied": status_applied}

    # --- monster AI ---
    def monster_ai_action(self, monster_state: dict, monster: MonsterSpec) -> str:
        """Deterministic policy, no rng consumed: aggressive monsters always
        attack; passive monsters always wait."""
        return "attack" if monster.ai == "aggressive" else "wait"

    # --- status effects ---
    def tick_status_effects(self, entity_state: dict, effects_index: dict[str, EffectSpec]) -> None:
        """Advance one tick for every queued status effect on `entity_state`
        (list of `{"effect_id", "remaining"}` dicts under the "status_effects"
        key, as queued by `resolve_skill`). Applies dot/heal magnitude for the
        tick, decrements `remaining`, and drops entries that expire. Mutates
        in place; no rng involved (status magnitudes are fixed content data)."""
        active = entity_state.get("status_effects", [])
        surviving = []
        for status in active:
            effect = effects_index.get(status["effect_id"])
            if effect is not None:
                if effect.kind == "dot":
                    entity_state["hp"] = entity_state.get("hp", 0) - effect.magnitude
                elif effect.kind == "heal":
                    entity_state["hp"] = entity_state.get("hp", 0) + effect.magnitude
                # buff/debuff magnitudes are applied once at queue time by the
                # caller; ticking here only tracks remaining duration for them.
            status["remaining"] = status.get("remaining", 1) - 1
            if status["remaining"] > 0:
                surviving.append(status)
        entity_state["status_effects"] = surviving

    # --- drops ---
    def roll_drops(self, drop_table: DropTableSpec | None) -> list[str]:
        """Roll every entry in `drop_table` in list order (deterministic draw
        order), returning the items that hit."""
        if drop_table is None:
            return []
        drops = []
        for entry in drop_table.entries:
            if self.rng.roll(entry.probability):
                drops.append(entry.item)
        return drops
