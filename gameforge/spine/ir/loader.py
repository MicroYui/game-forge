"""Hand-written scenario YAML → Spec-IR loader (M0a ingestion).

Direct loader for the M0a vertical slice. The full Schema Registry round-trip
adapter is M0b; this is the minimal path that produces a typed IR snapshot with
`has_step`/`precedes` edges (contract §2.3) and `source_ref` provenance so
Findings can point at the originating row.
"""

from __future__ import annotations

import os
from typing import Any

import yaml

from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation, SourceRef
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph

_ADAPTER = "m0a-yaml"


class _RelIds:
    def __init__(self) -> None:
        self._n = 0

    def next(self, etype: EdgeType, src: str, dst: str) -> str:
        rid = f"rel:{etype.value}:{src}->{dst}:{self._n}"
        self._n += 1
        return rid


def load_scenario(path_or_dict: str | dict) -> Snapshot:
    if isinstance(path_or_dict, str):
        with open(path_or_dict, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        file_ref = path_or_dict
    else:
        data = path_or_dict
        file_ref = "inline"

    g = IRGraph()
    rid = _RelIds()

    def sref(sheet: str, row: int) -> SourceRef:
        return SourceRef(adapter=_ADAPTER, file=file_ref, sheet=sheet, row=row)

    # --- regions (carry M0a scenario-level metadata: grid/start_pos/scenario_id) ---
    grid = data.get("grid", {})
    start_pos = data.get("start_pos", [0, 0])
    scenario_id = data.get("scenario_id", "scenario")
    for i, region in enumerate(data.get("regions", [])):
        attrs: dict[str, Any] = {"name": region.get("name")}
        if i == 0:  # attach scenario metadata to the primary region for M0a
            attrs.update({"grid": grid, "start_pos": start_pos, "scenario_id": scenario_id})
        g.add_entity(Entity(id=region["id"], type=NodeType.REGION, attrs=attrs,
                            source_ref=sref("regions", i)))

    # --- items ---
    for i, item in enumerate(data.get("items", [])):
        g.add_entity(Entity(id=item["id"], type=NodeType.ITEM,
                            attrs={"name": item.get("name")}, source_ref=sref("items", i)))

    # --- npcs ---
    for i, npc in enumerate(data.get("npcs", [])):
        g.add_entity(Entity(id=npc["id"], type=NodeType.NPC,
                            attrs={"name": npc.get("name"), "pos": npc.get("pos"),
                                   "region": npc.get("region")},
                            source_ref=sref("npcs", i)))
        if npc.get("region"):
            g.add_relation(Relation(id=rid.next(EdgeType.LOCATED_IN, npc["id"], npc["region"]),
                                    type=EdgeType.LOCATED_IN, src_id=npc["id"],
                                    dst_id=npc["region"], source_ref=sref("npcs", i)))

    # --- spawn points ---
    for i, sp in enumerate(data.get("spawn_points", [])):
        g.add_entity(Entity(id=sp["id"], type=NodeType.SPAWN_POINT,
                            attrs={"pos": sp.get("pos"), "region": sp.get("region")},
                            source_ref=sref("spawn_points", i)))
        if sp.get("region"):
            g.add_relation(Relation(id=rid.next(EdgeType.LOCATED_IN, sp["id"], sp["region"]),
                                    type=EdgeType.LOCATED_IN, src_id=sp["id"],
                                    dst_id=sp["region"], source_ref=sref("spawn_points", i)))
        if sp.get("spawns"):
            g.add_relation(Relation(id=rid.next(EdgeType.SPAWNS, sp["id"], sp["spawns"]),
                                    type=EdgeType.SPAWNS, src_id=sp["id"], dst_id=sp["spawns"],
                                    source_ref=sref("spawn_points", i)))

    # --- interactables (gather sources GRANT items) ---
    for i, it in enumerate(data.get("interactables", [])):
        g.add_entity(Entity(id=it["id"], type=NodeType.INTERACTABLE,
                            attrs={"kind": it.get("kind"), "pos": it.get("pos"),
                                   "yields_item": it.get("yields_item"),
                                   "yields_count": it.get("yields_count", 1)},
                            source_ref=sref("interactables", i)))
        if it.get("yields_item"):
            g.add_relation(Relation(id=rid.next(EdgeType.GRANTS, it["id"], it["yields_item"]),
                                    type=EdgeType.GRANTS, src_id=it["id"],
                                    dst_id=it["yields_item"], source_ref=sref("interactables", i)))

    # --- quests + steps ---
    for qi, quest in enumerate(data.get("quests", [])):
        qid = quest["id"]
        g.add_entity(Entity(id=qid, type=NodeType.QUEST,
                            attrs={"title": quest.get("title"), "region": quest.get("region"),
                                   "giver": quest.get("giver"), "reward": quest.get("reward", {})},
                            source_ref=sref("quests", qi)))
        if quest.get("giver"):
            g.add_relation(Relation(id=rid.next(EdgeType.STARTS_AT, qid, quest["giver"]),
                                    type=EdgeType.STARTS_AT, src_id=qid, dst_id=quest["giver"],
                                    source_ref=sref("quests", qi)))
        reward = quest.get("reward", {})
        if reward.get("item"):
            g.add_relation(Relation(id=rid.next(EdgeType.REWARDS, qid, reward["item"]),
                                    type=EdgeType.REWARDS, src_id=qid, dst_id=reward["item"],
                                    source_ref=sref("quests", qi)))

        prev_step_id: str | None = None
        for si, step in enumerate(quest.get("steps", [])):
            sid = step["id"]
            g.add_entity(Entity(id=sid, type=NodeType.QUEST_STEP,
                                attrs={"kind": step.get("kind"), "target": step.get("target"),
                                       "item": step.get("item"), "count": step.get("count", 1)},
                                source_ref=sref(f"quests[{qi}].steps", si)))
            g.add_relation(Relation(id=rid.next(EdgeType.HAS_STEP, qid, sid),
                                    type=EdgeType.HAS_STEP, src_id=qid, dst_id=sid,
                                    source_ref=sref(f"quests[{qi}].steps", si)))
            if prev_step_id is not None:
                g.add_relation(Relation(id=rid.next(EdgeType.PRECEDES, prev_step_id, sid),
                                        type=EdgeType.PRECEDES, src_id=prev_step_id, dst_id=sid,
                                        source_ref=sref(f"quests[{qi}].steps", si)))
            prev_step_id = sid

            kind = step.get("kind")
            if kind in ("talk", "turn_in") and step.get("target"):
                g.add_relation(Relation(id=rid.next(EdgeType.TALKS_TO, sid, step["target"]),
                                        type=EdgeType.TALKS_TO, src_id=sid, dst_id=step["target"],
                                        source_ref=sref(f"quests[{qi}].steps", si)))
            if kind == "collect" and step.get("item"):
                g.add_relation(Relation(id=rid.next(EdgeType.REQUIRES, sid, step["item"]),
                                        type=EdgeType.REQUIRES, src_id=sid, dst_id=step["item"],
                                        source_ref=sref(f"quests[{qi}].steps", si)))

    return Snapshot.from_graph(g)


def basename(path: str) -> str:
    return os.path.basename(path)
