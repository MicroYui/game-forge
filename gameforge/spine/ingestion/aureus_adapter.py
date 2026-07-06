"""AureusCsvAdapter — to_ir/from_ir between a typed CSV workbook and Spec-IR (contract §12A.1).

This is the M0b headline feature: a LOSSLESS round trip between Aureus's
four-system config workbook (quest + combat + economy + gacha) and the
Spec-IR graph —

    from_ir(to_ir(x)) == x     (field level, contract §2 anchor)

`to_ir` is a two-pass build:

  1. Table -> entity: every row of every known sheet becomes exactly one
     `Entity` whose `attrs` is the FULL row minus its primary key, verbatim
     (no normalization, no dropped columns). This is the only pass `from_ir`
     reads back from.
  2. Derived relations: a second pass walks the same rows to add graph edges
     (STARTS_AT, REWARDS, GRANTS, SPAWNS, HAS_STEP/PRECEDES, DROPS_FROM, SELLS,
     TALKS_TO/REQUIRES, TRIGGERED_BY, USES_SKILL, APPLIES_EFFECT) purely for
     graph queries/checkers (M1). These edges are REDUNDANT with pass 1's
     attrs — e.g. a DROPS_FROM edge just restates what `monsters.drop_table_id`
     + `drop_tables.entries` already say — so `from_ir` never looks at them.

`from_ir` is therefore a pure projection: for each sheet, collect its
entities, sort by `source_ref.row` (the only thing recording original row
order — entity id order and dict iteration order are NOT guaranteed to match
the source), and re-emit `{pk: entity.id, **entity.attrs}`. Nothing is
invented and nothing is lost, so the round trip is lossless BY CONSTRUCTION
rather than by careful case-by-case symmetry between to_ir/from_ir.
"""

from __future__ import annotations

from typing import Any

from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation, SourceRef
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph

_ADAPTER_ID = "aureus-csv"

# Every entity-sheet in the Aureus four-system workbook -> its Spec-IR NodeType
# (contract §12A.1). Dict order doubles as `from_ir`'s sheet emission order.
SHEET_NODE_TYPE: dict[str, NodeType] = {
    "items": NodeType.ITEM,
    "npcs": NodeType.NPC,
    "monsters": NodeType.MONSTER,
    "skills": NodeType.SKILL,
    "effects": NodeType.EFFECT,
    "status_effects": NodeType.STATUS_EFFECT,
    "formulas": NodeType.FORMULA,
    "equipment": NodeType.EQUIPMENT,
    "drop_tables": NodeType.DROP_TABLE,
    "encounters": NodeType.BATTLE_ENCOUNTER,
    "shops": NodeType.SHOP,
    "gacha_pools": NodeType.GACHA_POOL,
    "currencies": NodeType.CURRENCY,
    "regions": NodeType.REGION,
    "spawn_points": NodeType.SPAWN_POINT,
    "interactables": NodeType.INTERACTABLE,
    "quests": NodeType.QUEST,
    "quest_steps": NodeType.QUEST_STEP,
}

# Primary-key column name per sheet (removed from the row before it becomes
# `entity.attrs`; restored as `entity.id` when reconstructed in `from_ir`).
PK_BY_SHEET: dict[str, str] = {
    "items": "item_id",
    "npcs": "npc_id",
    "monsters": "monster_id",
    "skills": "skill_id",
    "effects": "effect_id",
    "status_effects": "status_effect_id",
    "formulas": "formula_id",
    "equipment": "equipment_id",
    "drop_tables": "drop_table_id",
    "encounters": "encounter_id",
    "shops": "shop_id",
    "gacha_pools": "gacha_pool_id",
    "currencies": "currency_id",
    "regions": "region_id",
    "spawn_points": "spawn_point_id",
    "interactables": "interactable_id",
    "quests": "quest_id",
    "quest_steps": "step_id",
}


class _RelIds:
    """Deterministic `rel:<TYPE>:<src>-><dst>:<n>` ids (same scheme as the M0a loader)."""

    def __init__(self) -> None:
        self._n = 0

    def next(self, etype: EdgeType, src: str, dst: str) -> str:
        rid = f"rel:{etype.value}:{src}->{dst}:{self._n}"
        self._n += 1
        return rid


class AureusCsvAdapter:
    """Adapter for the Aureus reference-game CSV workbook format (contract §12A.1)."""

    format_id = "aureus-csv"

    def to_ir(self, workbook: dict[str, list[dict]], file_ref: str) -> Snapshot:
        g = IRGraph()
        rid = _RelIds()

        def sref(sheet: str, row: int) -> SourceRef:
            return SourceRef(adapter=_ADAPTER_ID, file=file_ref, sheet=sheet, row=row)

        # --- pass 1: every row -> one typed Entity, full row minus pk as attrs ---
        for sheet, node_type in SHEET_NODE_TYPE.items():
            pk = PK_BY_SHEET[sheet]
            for i, row in enumerate(workbook.get(sheet, [])):
                attrs: dict[str, Any] = {k: v for k, v in row.items() if k != pk}
                g.add_entity(Entity(id=row[pk], type=node_type, attrs=attrs,
                                    source_ref=sref(sheet, i)))

        # --- pass 2: derived relations for graph queries/checkers (M1) ---

        # GRANTS (interactable -> item) via interactables.yields_item — same
        # direction as the M0a loader (src=interactable, dst=item): a gather
        # source GRANTS the item it yields. This is a checker-gate input
        # (`StructuralChecker._collect_needs_source` looks for GRANTS/DROPS_FROM
        # edges whose dst_id is the collected item).
        for i, it in enumerate(workbook.get("interactables", [])):
            item = it.get("yields_item")
            if not item:
                continue
            g.add_relation(Relation(
                id=rid.next(EdgeType.GRANTS, it["interactable_id"], item),
                type=EdgeType.GRANTS, src_id=it["interactable_id"], dst_id=item,
                source_ref=sref("interactables", i),
            ))

        # SPAWNS (spawn_point -> item) via spawn_points.spawns — same direction
        # as the M0a loader (src=spawn_point, dst=item).
        for i, sp in enumerate(workbook.get("spawn_points", [])):
            item = sp.get("spawns")
            if not item:
                continue
            g.add_relation(Relation(
                id=rid.next(EdgeType.SPAWNS, sp["spawn_point_id"], item),
                type=EdgeType.SPAWNS, src_id=sp["spawn_point_id"], dst_id=item,
                source_ref=sref("spawn_points", i),
            ))

        # STARTS_AT (quest -> giver) + REWARDS (quest -> reward.item), same
        # direction as the M0a loader (`spine/ir/loader.py`). Without these a
        # CSV-only quest has neither edge, which makes GraphChecker's
        # dead_quest (no giver) and isolated_node (reward item unreferenced)
        # fire as false positives on an otherwise-clean quest.
        for i, quest in enumerate(workbook.get("quests", [])):
            giver = quest.get("giver")
            if giver:
                g.add_relation(Relation(
                    id=rid.next(EdgeType.STARTS_AT, quest["quest_id"], giver),
                    type=EdgeType.STARTS_AT, src_id=quest["quest_id"], dst_id=giver,
                    source_ref=sref("quests", i),
                ))
            reward = quest.get("reward") or {}
            reward_item = reward.get("item")
            if reward_item:
                g.add_relation(Relation(
                    id=rid.next(EdgeType.REWARDS, quest["quest_id"], reward_item),
                    type=EdgeType.REWARDS, src_id=quest["quest_id"], dst_id=reward_item,
                    source_ref=sref("quests", i),
                ))

        # HAS_STEP (quest -> step) + PRECEDES (chain, ordered by `order`, not row order).
        steps_by_quest: dict[str, list[tuple[Any, str, int]]] = {}
        for i, step in enumerate(workbook.get("quest_steps", [])):
            steps_by_quest.setdefault(step["quest_id"], []).append(
                (step.get("order", i), step["step_id"], i)
            )
        for quest_id, steps in steps_by_quest.items():
            prev_step_id: str | None = None
            for _order, step_id, row_i in sorted(steps, key=lambda t: t[0]):
                g.add_relation(Relation(
                    id=rid.next(EdgeType.HAS_STEP, quest_id, step_id),
                    type=EdgeType.HAS_STEP, src_id=quest_id, dst_id=step_id,
                    source_ref=sref("quest_steps", row_i),
                ))
                if prev_step_id is not None:
                    g.add_relation(Relation(
                        id=rid.next(EdgeType.PRECEDES, prev_step_id, step_id),
                        type=EdgeType.PRECEDES, src_id=prev_step_id, dst_id=step_id,
                        source_ref=sref("quest_steps", row_i),
                    ))
                prev_step_id = step_id

        # TALKS_TO / REQUIRES / TRIGGERED_BY, per step (same kinds as the M0a loader,
        # plus TRIGGERED_BY for the M0b `fight` step kind).
        for i, step in enumerate(workbook.get("quest_steps", [])):
            step_id = step["step_id"]
            kind = step.get("kind")
            if kind in ("talk", "turn_in") and step.get("target"):
                g.add_relation(Relation(
                    id=rid.next(EdgeType.TALKS_TO, step_id, step["target"]),
                    type=EdgeType.TALKS_TO, src_id=step_id, dst_id=step["target"],
                    source_ref=sref("quest_steps", i),
                ))
            if kind == "collect" and step.get("item"):
                g.add_relation(Relation(
                    id=rid.next(EdgeType.REQUIRES, step_id, step["item"]),
                    type=EdgeType.REQUIRES, src_id=step_id, dst_id=step["item"],
                    source_ref=sref("quest_steps", i),
                ))
            if kind == "fight" and step.get("encounter"):
                g.add_relation(Relation(
                    id=rid.next(EdgeType.TRIGGERED_BY, step["encounter"], step_id),
                    type=EdgeType.TRIGGERED_BY, src_id=step["encounter"], dst_id=step_id,
                    source_ref=sref("quest_steps", i),
                ))

        # DROPS_FROM (item -> monster) via monsters.drop_table_id + drop_tables.entries.
        drop_tables_by_id = {
            row["drop_table_id"]: row for row in workbook.get("drop_tables", [])
        }
        for i, monster in enumerate(workbook.get("monsters", [])):
            dt = drop_tables_by_id.get(monster.get("drop_table_id"))
            if dt is None:
                continue
            for entry in dt.get("entries", []):
                g.add_relation(Relation(
                    id=rid.next(EdgeType.DROPS_FROM, entry["item"], monster["monster_id"]),
                    type=EdgeType.DROPS_FROM, src_id=entry["item"], dst_id=monster["monster_id"],
                    source_ref=sref("monsters", i),
                ))

        # DROPS_FROM (monster -> currency) via monsters.gold_min/gold_max/
        # currency — all optional columns beyond the base monsters schema,
        # present only on scenarios that exercise the economy simulator
        # (Task 8 `EconomyModel.from_snapshot`). This is the OPPOSITE
        # direction from the item-drop DROPS_FROM edges above (src=item,
        # dst=monster there): DROPS_FROM is contract-wide overloaded for two
        # distinct "produces" relationships — item sourcing (checker-facing,
        # dst=item) and currency sourcing (economy-sim-facing, dst=currency)
        # — that never collide in practice since item ids and currency ids
        # are disjoint namespaces.
        for i, monster in enumerate(workbook.get("monsters", [])):
            currency = monster.get("currency")
            if not currency or monster.get("gold_min") is None:
                continue
            g.add_relation(Relation(
                id=rid.next(EdgeType.DROPS_FROM, monster["monster_id"], currency),
                type=EdgeType.DROPS_FROM, src_id=monster["monster_id"], dst_id=currency,
                source_ref=sref("monsters", i),
            ))

        # SELLS (shop -> item) via shops.entries.
        for i, shop in enumerate(workbook.get("shops", [])):
            for entry in shop.get("entries", []):
                g.add_relation(Relation(
                    id=rid.next(EdgeType.SELLS, shop["shop_id"], entry["item"]),
                    type=EdgeType.SELLS, src_id=shop["shop_id"], dst_id=entry["item"],
                    source_ref=sref("shops", i),
                ))

        # USES_SKILL (monster -> skill) via monsters.skills.
        for i, monster in enumerate(workbook.get("monsters", [])):
            for skill_id in monster.get("skills") or []:
                g.add_relation(Relation(
                    id=rid.next(EdgeType.USES_SKILL, monster["monster_id"], skill_id),
                    type=EdgeType.USES_SKILL, src_id=monster["monster_id"], dst_id=skill_id,
                    source_ref=sref("monsters", i),
                ))

        # APPLIES_EFFECT (skill -> status_effect -> effect) via skills.applies_status
        # and status_effects.effect_id.
        for i, skill in enumerate(workbook.get("skills", [])):
            status_id = skill.get("applies_status")
            if not status_id:
                continue
            g.add_relation(Relation(
                id=rid.next(EdgeType.APPLIES_EFFECT, skill["skill_id"], status_id),
                type=EdgeType.APPLIES_EFFECT, src_id=skill["skill_id"], dst_id=status_id,
                source_ref=sref("skills", i),
            ))
        for i, status_effect in enumerate(workbook.get("status_effects", [])):
            effect_id = status_effect.get("effect_id")
            if not effect_id:
                continue
            g.add_relation(Relation(
                id=rid.next(EdgeType.APPLIES_EFFECT, status_effect["status_effect_id"], effect_id),
                type=EdgeType.APPLIES_EFFECT, src_id=status_effect["status_effect_id"], dst_id=effect_id,
                source_ref=sref("status_effects", i),
            ))

        return Snapshot.from_graph(g)

    def from_ir(self, snapshot: Snapshot) -> dict[str, list[dict]]:
        g = snapshot.to_graph()
        workbook: dict[str, list[dict]] = {}
        for sheet, node_type in SHEET_NODE_TYPE.items():
            entities = g.nodes_of_type(node_type)
            if not entities:
                continue  # emit ONLY sheets that have entities
            pk = PK_BY_SHEET[sheet]
            entities.sort(
                key=lambda e: e.source_ref.row
                if e.source_ref is not None and e.source_ref.row is not None
                else 0
            )
            workbook[sheet] = [{pk: e.id, **e.attrs} for e in entities]
        return workbook
