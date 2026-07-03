"""AureusWorld — runtime world built from a WorldConfig (Aureus M0a).

Holds the grid, entity positions, gather sources, and quest definitions. Pure
data + lookups; the tick logic lives in the kernel.
"""

from __future__ import annotations

from gameforge.contracts.world import QuestSpec, WorldConfig
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

    def pos_of(self, entity_id: str) -> Pos | None:
        return self.positions.get(entity_id)

    def entities_at(self, pos: Pos) -> list[str]:
        return sorted(eid for eid, p in self.positions.items() if p == pos)

    def grants_item(self, interactable_id: str) -> tuple[str | None, int]:
        it = self.interactables.get(interactable_id)
        if not it:
            return None, 0
        return it["yields_item"], it["yields_count"]
