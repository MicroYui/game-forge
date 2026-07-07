"""The repair drafter's IR context must give the model what structural repairs
need: real relation ids/types/endpoints (to delete/modify), an available-entity
catalog (to reference as add_relation src/dst), and the edge-type vocabulary.
Without these the model invents relation ids that apply_patch rejects."""
import json

from gameforge.agents.repair.drafter import RepairDrafter
from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.snapshot import Snapshot


def _finding(snap, entities):
    return Finding(
        id="f1", source="checker", producer_id="p", producer_run_id="r",
        oracle_type="deterministic", defect_class="cyclic_dependency", severity="major",
        snapshot_id=snap.snapshot_id, status="confirmed", message="cycle", entities=entities,
    )


def test_ir_context_exposes_relations_catalog_and_edge_types():
    ents = [
        Entity(id="s1", type=NodeType.QUEST_STEP, attrs={"name": "first"}),
        Entity(id="s2", type=NodeType.QUEST_STEP, attrs={"name": "second"}),
        Entity(id="npc1", type=NodeType.NPC, attrs={"name": "elder"}),
    ]
    rels = [Relation(id="r_pre", type=EdgeType.PRECEDES, src_id="s1", dst_id="s2")]
    snap = Snapshot.from_entities_relations(ents, rels)

    ctx = json.loads(RepairDrafter()._ir_context(_finding(snap, ["s1", "s2"]), snap))

    # incident relations expose the REAL relation id + endpoints (so delete_relation
    # targets an id that actually exists, instead of an invented "s1->s2")
    assert any(
        r["id"] == "r_pre" and r["type"] == EdgeType.PRECEDES.value
        and r["src_id"] == "s1" and r["dst_id"] == "s2"
        for r in ctx["incident_relations"]
    )
    # entity catalog lists available ids by type (so add_relation can pick a valid src/dst)
    assert "npc1" in json.dumps(ctx["entity_catalog"])
    # edge-type vocabulary present (so add_relation type is a real EdgeType)
    assert EdgeType.PRECEDES.value in ctx["edge_types"]


def test_ir_context_still_includes_focus_node_attrs():
    ents = [Entity(id="q", type=NodeType.QUEST, attrs={"reward_gold": 120})]
    snap = Snapshot.from_entities_relations(ents, [])
    ctx = json.loads(RepairDrafter()._ir_context(_finding(snap, ["q"]), snap))
    focus = {n["id"]: n for n in ctx["focus_nodes"]}
    assert focus["q"]["attrs"]["reward_gold"] == 120
