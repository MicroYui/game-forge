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


def test_build_ops_drops_old_value_on_delete_ops():
    # A model-summarized old_value on a delete op can't match apply_patch's
    # full-object current value, so its optimistic-concurrency pre-check
    # (spine/patch.py) would spuriously reject the whole patch. Deletion is
    # identified by target id alone, so the drafter drops old_value for
    # delete_relation/delete_entity — while keeping it authoritative for set_*
    # ops, where it genuinely guards against stale writes.
    raw = [
        {"op": "delete_relation", "target": "r_pre",
         "old_value": {"type": "PRECEDES", "src_id": "s1", "dst_id": "s2"}},
        {"op": "delete_entity", "target": "e1", "old_value": {"type": "QUEST"}},
        {"op": "set_relation_attr", "target": "r_x.price", "old_value": 50, "new_value": 60},
        {"op": "set_entity_attr", "target": "e2.gold", "old_value": 5, "new_value": 3},
    ]
    by_op = {o.op: o for o in RepairDrafter()._build_ops(raw)}
    assert by_op["delete_relation"].old_value is None
    assert by_op["delete_entity"].old_value is None
    # set_* ops keep old_value (optimistic concurrency preserved)
    assert by_op["set_relation_attr"].old_value == 50
    assert by_op["set_entity_attr"].old_value == 5


def test_ir_context_still_includes_focus_node_attrs():
    ents = [Entity(id="q", type=NodeType.QUEST, attrs={"reward_gold": 120})]
    snap = Snapshot.from_entities_relations(ents, [])
    ctx = json.loads(RepairDrafter()._ir_context(_finding(snap, ["q"]), snap))
    focus = {n["id"]: n for n in ctx["focus_nodes"]}
    assert focus["q"]["attrs"]["reward_gold"] == 120
