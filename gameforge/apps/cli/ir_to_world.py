"""IR snapshot → WorldConfig (Aureus is driven by IR-exported config).

Reconstructs the runtime world from the typed IR: grid/start_pos from the primary
region, placements from positioned entities, quests from HAS_STEP + PRECEDES
ordered steps.
"""

from __future__ import annotations

from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.contracts.world import (
    CurrencySpec, DropEntry, DropTableSpec, EffectSpec, EquipmentSpec, FormulaSpec,
    GachaEntry, GachaPoolSpec, GridSpec, MonsterSpec, Placement, QuestSpec,
    QuestStepSpec, ScenarioConfig, ShopEntry, ShopSpec, SkillSpec, StatusEffectSpec,
    WorldConfig, BattleEncounterSpec,
)
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph

_PLACED_TYPES = {
    NodeType.NPC, NodeType.INTERACTABLE, NodeType.SPAWN_POINT,
    NodeType.BATTLE_ENCOUNTER, NodeType.SHOP, NodeType.GACHA_POOL,
}


def _ordered_step_ids(g: IRGraph, quest_id: str) -> list[str]:
    step_ids = [r.dst_id for r in g.neighbors(quest_id, EdgeType.HAS_STEP)]
    step_set = set(step_ids)
    succ: dict[str, str] = {}
    has_pred: set[str] = set()
    for sid in step_ids:
        for r in g.neighbors(sid, EdgeType.PRECEDES):
            if r.dst_id in step_set:
                succ[sid] = r.dst_id
                has_pred.add(r.dst_id)
    heads = sorted(sid for sid in step_ids if sid not in has_pred)
    ordered: list[str] = []
    if heads:
        cur: str | None = heads[0]
        seen: set[str] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            ordered.append(cur)
            cur = succ.get(cur)
    for sid in step_ids:  # safety: append any not linked by precedes
        if sid not in ordered:
            ordered.append(sid)
    return ordered


def snapshot_to_world(snapshot: Snapshot) -> WorldConfig:
    g = snapshot.to_graph()

    # --- scenario metadata carried on the primary region ---
    region = next((r for r in g.nodes_of_type(NodeType.REGION) if "grid" in r.attrs), None)
    if region is None:
        raise ValueError("scenario has no region carrying grid metadata")
    grid_attr = region.attrs["grid"]
    grid = GridSpec(
        width=int(grid_attr["width"]),
        height=int(grid_attr["height"]),
        blocked=[(int(x), int(y)) for x, y in grid_attr.get("blocked", [])],
    )
    start = region.attrs.get("start_pos", [0, 0])
    scenario = ScenarioConfig(
        scenario_id=region.attrs.get("scenario_id", "scenario"),
        start_pos=(int(start[0]), int(start[1])),
    )

    # --- placements ---
    placements: list[Placement] = []
    for e in g.all_entities():
        if e.type in _PLACED_TYPES and e.attrs.get("pos") is not None:
            pos = e.attrs["pos"]
            placements.append(
                Placement(entity_id=e.id, type=e.type,
                          pos=(int(pos[0]), int(pos[1])), attrs=dict(e.attrs))
            )
    placements.sort(key=lambda p: p.entity_id)

    # --- quests ---
    quests: list[QuestSpec] = []
    for q in sorted(g.nodes_of_type(NodeType.QUEST), key=lambda e: e.id):
        steps: list[QuestStepSpec] = []
        for sid in _ordered_step_ids(g, q.id):
            step = g.get_node(sid)
            if step is None:
                continue
            steps.append(
                QuestStepSpec(
                    step_id=step.id,
                    kind=step.attrs.get("kind"),
                    target=step.attrs.get("target"),
                    item=step.attrs.get("item"),
                    count=int(step.attrs.get("count", 1)),
                    encounter=step.attrs.get("encounter"),
                )
            )
        quests.append(
            QuestSpec(
                quest_id=q.id,
                giver=q.attrs.get("giver"),
                steps=steps,
                reward=dict(q.attrs.get("reward", {})),
            )
        )

    # --- combat-economy content (M0b): id-keyed specs read straight from attrs ---
    currencies = [
        CurrencySpec(currency_id=e.id, name=e.attrs.get("name"))
        for e in sorted(g.nodes_of_type(NodeType.CURRENCY), key=lambda e: e.id)
    ]
    formulas = [
        FormulaSpec(formula_id=e.id, expr=e.attrs["expr"], kind=e.attrs.get("kind", "damage"))
        for e in sorted(g.nodes_of_type(NodeType.FORMULA), key=lambda e: e.id)
    ]
    effects = [
        EffectSpec(
            effect_id=e.id, kind=e.attrs["kind"], stat=e.attrs.get("stat"),
            magnitude=int(e.attrs.get("magnitude", 0)), duration=int(e.attrs.get("duration", 0)),
        )
        for e in sorted(g.nodes_of_type(NodeType.EFFECT), key=lambda e: e.id)
    ]
    status_effects = [
        StatusEffectSpec(
            status_effect_id=e.id, effect_id=e.attrs["effect_id"],
            duration=int(e.attrs.get("duration", 1)),
        )
        for e in sorted(g.nodes_of_type(NodeType.STATUS_EFFECT), key=lambda e: e.id)
    ]
    skills = [
        SkillSpec(
            skill_id=e.id, name=e.attrs.get("name"), cost=int(e.attrs.get("cost", 0)),
            power=int(e.attrs.get("power", 100)), formula_id=e.attrs.get("formula_id"),
            target=e.attrs.get("target", "enemy"), applies_status=e.attrs.get("applies_status"),
        )
        for e in sorted(g.nodes_of_type(NodeType.SKILL), key=lambda e: e.id)
    ]
    equipment = [
        EquipmentSpec(
            equipment_id=e.id, slot=e.attrs["slot"], stat_mods=dict(e.attrs.get("stat_mods", {})),
        )
        for e in sorted(g.nodes_of_type(NodeType.EQUIPMENT), key=lambda e: e.id)
    ]
    monsters = [
        MonsterSpec(
            monster_id=e.id, name=e.attrs.get("name"), stats=dict(e.attrs.get("stats", {})),
            skills=list(e.attrs.get("skills") or []), drop_table_id=e.attrs.get("drop_table_id"),
            ai=e.attrs.get("ai", "aggressive"),
        )
        for e in sorted(g.nodes_of_type(NodeType.MONSTER), key=lambda e: e.id)
    ]
    drop_tables = [
        DropTableSpec(
            drop_table_id=e.id,
            entries=[DropEntry(**entry) for entry in e.attrs.get("entries", [])],
        )
        for e in sorted(g.nodes_of_type(NodeType.DROP_TABLE), key=lambda e: e.id)
    ]
    encounters = [
        BattleEncounterSpec(
            encounter_id=e.id, monsters=list(e.attrs.get("monsters") or []),
            reward=dict(e.attrs.get("reward", {})),
            pos=(int(e.attrs["pos"][0]), int(e.attrs["pos"][1])) if e.attrs.get("pos") else None,
        )
        for e in sorted(g.nodes_of_type(NodeType.BATTLE_ENCOUNTER), key=lambda e: e.id)
    ]
    shops = [
        ShopSpec(
            shop_id=e.id, entries=[ShopEntry(**entry) for entry in e.attrs.get("entries", [])],
        )
        for e in sorted(g.nodes_of_type(NodeType.SHOP), key=lambda e: e.id)
    ]
    gacha_pools = [
        GachaPoolSpec(
            gacha_pool_id=e.id, cost=int(e.attrs.get("cost", 100)),
            currency=e.attrs.get("currency", "gold"),
            entries=[GachaEntry(**entry) for entry in e.attrs.get("entries", [])],
            pity_threshold=int(e.attrs.get("pity_threshold", 0)),
            pity_item=e.attrs.get("pity_item"),
        )
        for e in sorted(g.nodes_of_type(NodeType.GACHA_POOL), key=lambda e: e.id)
    ]

    return WorldConfig(
        scenario=scenario, grid=grid, placements=placements, quests=quests,
        currencies=currencies, formulas=formulas, effects=effects,
        status_effects=status_effects, skills=skills, equipment=equipment,
        monsters=monsters, drop_tables=drop_tables, encounters=encounters,
        shops=shops, gacha_pools=gacha_pools,
    )
