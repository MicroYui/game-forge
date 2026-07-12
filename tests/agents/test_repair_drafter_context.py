"""The repair drafter's IR context must give the model what structural repairs
need: real relation ids/types/endpoints (to delete/modify), an available-entity
catalog (to reference as add_relation src/dst), and the edge-type vocabulary.
Without these the model invents relation ids that apply_patch rejects."""
import hashlib
import json

import pytest

from gameforge.agents.repair.drafter import RepairDrafter
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch


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


def test_build_ops_drops_old_value_on_add_and_delete_ops():
    # A model-summarized old_value on an add/delete op can't match apply_patch's
    # full-object current value, so its optimistic-concurrency pre-check
    # (spine/patch.py) would spuriously reject the whole patch. old_value is an
    # assertion about a PRE-EXISTING value — meaningful only for in-place updates
    # (set_*/replace_subgraph); add_* have no prior value and delete_* are
    # identified by id alone, so the drafter drops old_value there.
    raw = [
        {"op": "delete_relation", "target": "r_pre",
         "old_value": {"type": "PRECEDES", "src_id": "s1", "dst_id": "s2"}},
        {"op": "delete_entity", "target": "e1", "old_value": {"type": "QUEST"}},
        {"op": "add_relation", "target": "rel_fix_1", "old_value": {"stale": True},
         "new_value": {"type": "PRECEDES", "src_id": "s3", "dst_id": "s4"}},
        {"op": "add_entity", "target": "e_new", "old_value": {"stale": True},
         "new_value": {"type": "NPC", "attrs": {}}},
        {"op": "set_relation_attr", "target": "r_x.price", "old_value": 50, "new_value": 60},
        {"op": "set_entity_attr", "target": "e2.gold", "old_value": 5, "new_value": 3},
        {"op": "replace_subgraph", "target": "sg", "old_value": {"v": 1}, "new_value": {"v": 2}},
    ]
    by_op = {o.op: o for o in RepairDrafter()._build_ops(raw)}
    # add_* / delete_* drop old_value (spurious-rejection class)
    assert by_op["delete_relation"].old_value is None
    assert by_op["delete_entity"].old_value is None
    assert by_op["add_relation"].old_value is None
    assert by_op["add_entity"].old_value is None
    # in-place updates keep old_value (optimistic concurrency preserved)
    assert by_op["set_relation_attr"].old_value == 50
    assert by_op["set_entity_attr"].old_value == 5
    assert by_op["replace_subgraph"].old_value == {"v": 1}


def test_ir_context_still_includes_focus_node_attrs():
    ents = [Entity(id="q", type=NodeType.QUEST, attrs={"reward_gold": 120})]
    snap = Snapshot.from_entities_relations(ents, [])
    ctx = json.loads(RepairDrafter()._ir_context(_finding(snap, ["q"]), snap))
    focus = {n["id"]: n for n in ctx["focus_nodes"]}
    assert focus["q"]["attrs"]["reward_gold"] == 120


class _FixedOpsTransport:
    def __init__(self):
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(
            response_normalized=json.dumps(
                [
                    {
                        "op": "set_entity_attr",
                        "target": "q.reward_gold",
                        "old_value": 120,
                        "new_value": 80,
                    }
                ]
            )
        )


def _stable_context_snapshot(unrelated_name: str) -> Snapshot:
    return Snapshot.from_entities_relations(
        [
            Entity(id="q", type=NodeType.QUEST, attrs={"reward_gold": 120}),
            Entity(
                id="npc:unrelated",
                type=NodeType.NPC,
                attrs={"name": unrelated_name},
            ),
        ],
        [],
    )


def _expected_patch_id(patch) -> str:
    payload = {
        "request_hash": patch.producer_run_id,
        "base_snapshot_id": patch.base_snapshot_id,
        "ops": [op.model_dump(mode="json") for op in patch.ops],
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def test_semantic_request_reuses_model_run_but_patch_identity_binds_base(tmp_path):
    snap_a = _stable_context_snapshot("before")
    snap_b = _stable_context_snapshot("after")
    transport = _FixedOpsTransport()
    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
    )
    drafter = RepairDrafter()

    patch_a = drafter.draft(_finding(snap_a, ["q"]), snap_a, router)
    patch_b = drafter.draft(_finding(snap_b, ["q"]), snap_b, router)

    assert patch_a is not None and patch_b is not None
    assert snap_a.snapshot_id != snap_b.snapshot_id
    assert len(transport.calls) == 1
    assert patch_a.producer_run_id == patch_b.producer_run_id
    assert patch_a.id != patch_b.id
    assert patch_a.id == _expected_patch_id(patch_a)
    assert patch_b.id == _expected_patch_id(patch_b)
    assert patch_a.base_snapshot_id == snap_a.snapshot_id
    assert patch_b.base_snapshot_id == snap_b.snapshot_id
    with pytest.raises(PatchRejected, match="base snapshot mismatch"):
        apply_patch(snap_a, patch_b)


def test_user_prompt_omits_base_snapshot_identity():
    snap = _stable_context_snapshot("irrelevant")
    prompt = RepairDrafter()._build_user_prompt(_finding(snap, ["q"]), snap)

    assert snap.snapshot_id not in prompt
    assert "base_snapshot_id" not in prompt
