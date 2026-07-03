"""IR snapshot → WorldConfig (Aureus is driven by IR-exported config).

Reconstructs the runtime world from the typed IR: grid/start_pos from the primary
region, placements from positioned entities, quests from HAS_STEP + PRECEDES
ordered steps.
"""

from __future__ import annotations

from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.contracts.world import (
    GridSpec, Placement, QuestSpec, QuestStepSpec, ScenarioConfig, WorldConfig,
)
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph

_PLACED_TYPES = {NodeType.NPC, NodeType.INTERACTABLE, NodeType.SPAWN_POINT}


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

    return WorldConfig(scenario=scenario, grid=grid, placements=placements, quests=quests)
