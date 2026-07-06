"""AureusWorld — runtime world built from a WorldConfig (Aureus M0a/M0b).

Holds the grid, entity positions, gather sources, quest definitions, and
(M0b) id-keyed indices for combat/economy/gacha content specs. Pure data +
lookups; the tick logic lives in the kernel.
"""

from __future__ import annotations

from gameforge.contracts.world import (
    BattleEncounterSpec, DropTableSpec, EffectSpec, EquipmentSpec, FormulaSpec,
    GachaPoolSpec, MonsterSpec, QuestSpec, ShopSpec, SkillSpec, StatusEffectSpec,
    WorldConfig,
)
from gameforge.game.aureus.grid import Grid

Pos = tuple[int, int]


class AureusWorld:
    def __init__(self, config: WorldConfig) -> None:
        self.config = config
        self.grid = Grid(config.grid)
        self.start_pos: Pos = (int(config.scenario.start_pos[0]), int(config.scenario.start_pos[1]))
        self.positions: dict[str, Pos] = {}
        self.npc_ids: set[str] = set()
        self.interactables: dict[str, dict] = {}
        for p in config.placements:
            pos = (int(p.pos[0]), int(p.pos[1]))
            self.positions[p.entity_id] = pos
            if p.type.value == "NPC":
                self.npc_ids.add(p.entity_id)
            if p.type.value == "INTERACTABLE":
                self.interactables[p.entity_id] = {
                    "kind": p.attrs.get("kind"),
                    "yields_item": p.attrs.get("yields_item"),
                    "yields_count": int(p.attrs.get("yields_count", 1)),
                    "pos": pos,
                }
        self.quests: dict[str, QuestSpec] = {q.quest_id: q for q in config.quests}

        # --- M0b: id-keyed indices for combat/economy/gacha content ---
        self.formulas: dict[str, FormulaSpec] = {f.formula_id: f for f in config.formulas}
        self.effects: dict[str, EffectSpec] = {e.effect_id: e for e in config.effects}
        self.status_effects: dict[str, StatusEffectSpec] = {
            s.status_effect_id: s for s in config.status_effects
        }
        self.skills: dict[str, SkillSpec] = {s.skill_id: s for s in config.skills}
        self.equipment: dict[str, EquipmentSpec] = {e.equipment_id: e for e in config.equipment}
        self.monsters: dict[str, MonsterSpec] = {m.monster_id: m for m in config.monsters}
        self.drop_tables: dict[str, DropTableSpec] = {
            d.drop_table_id: d for d in config.drop_tables
        }
        self.encounters: dict[str, BattleEncounterSpec] = {
            e.encounter_id: e for e in config.encounters
        }
        self.shops: dict[str, ShopSpec] = {s.shop_id: s for s in config.shops}
        self.gacha_pools: dict[str, GachaPoolSpec] = {
            g.gacha_pool_id: g for g in config.gacha_pools
        }

    def pos_of(self, entity_id: str) -> Pos | None:
        return self.positions.get(entity_id)

    def entities_at(self, pos: Pos) -> list[str]:
        return sorted(eid for eid, p in self.positions.items() if p == pos)

    def grants_item(self, interactable_id: str) -> tuple[str | None, int]:
        it = self.interactables.get(interactable_id)
        if not it:
            return None, 0
        return it["yields_item"], it["yields_count"]

    # --- M0b: combat/economy/gacha lookups ---
    def encounter_at(self, pos: Pos) -> BattleEncounterSpec | None:
        """First encounter (config order) placed at `pos`, or None. Encounters
        with `pos=None` are never location-triggered (e.g. scripted-only)."""
        for enc in self.encounters.values():
            if enc.pos is not None and (int(enc.pos[0]), int(enc.pos[1])) == pos:
                return enc
        return None

    def shop(self, shop_id: str) -> ShopSpec | None:
        return self.shops.get(shop_id)

    def gacha(self, pool_id: str) -> GachaPoolSpec | None:
        return self.gacha_pools.get(pool_id)
