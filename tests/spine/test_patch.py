"""Tests for the deterministic typed-patch apply/reject engine (M1 Task 9).

Contract §6 anchor: old_value optimistic concurrency — rebase-or-reject, never
blindly apply.
"""

from __future__ import annotations

import pytest

from gameforge.contracts.findings import Patch, PatchV2, TypedOp
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch, dry_run


def _patch(snap: Snapshot, ops, preconditions=None, patch_id="p1") -> Patch:
    return Patch(
        id=patch_id,
        base_snapshot_id=snap.snapshot_id,
        target_snapshot_id="",
        side_effect_risk="low",
        ops=ops,
        preconditions=preconditions or [],
        produced_by="agent",
        producer_run_id="r1",
        rationale="test",
    )


def _patch_v2(snap: Snapshot, ops) -> PatchV2:
    return PatchV2(
        revision=1,
        base_snapshot_id=snap.snapshot_id,
        target_snapshot_id="sha256:declared-target",
        side_effect_risk="low",
        ops=ops,
        produced_by="agent",
        producer_run_id="run:1",
        rationale="test M4 revision",
    )


def _base_snapshot() -> Snapshot:
    entities = [
        Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120}),
        Entity(id="npc:1", type=NodeType.NPC, attrs={"name": "Bob"}),
    ]
    relations = [
        Relation(id="rel:1", type=EdgeType.STARTS_AT, src_id="q:1", dst_id="npc:1",
                 attrs={"note": "start"}),
    ]
    return Snapshot.from_entities_relations(entities, relations)


def test_set_entity_attr_with_correct_old_value_applies():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="o1", op="set_entity_attr", target="q:1.reward_gold",
                old_value=120, new_value=80),
    ])
    snap2 = apply_patch(snap, patch)
    assert snap2.to_graph().get_node("q:1").attrs["reward_gold"] == 80
    assert snap2.snapshot_id != snap.snapshot_id
    # never mutate input: the original snapshot is untouched
    assert snap.to_graph().get_node("q:1").attrs["reward_gold"] == 120


def test_patch_v2_uses_the_same_pure_exact_base_engine():
    snap = _base_snapshot()
    patch = _patch_v2(
        snap,
        [
            TypedOp(
                op_id="o1",
                op="set_entity_attr",
                target="q:1.reward_gold",
                old_value=120,
                new_value=80,
            )
        ],
    )

    updated = apply_patch(snap, patch)
    assert updated.to_graph().get_node("q:1").attrs["reward_gold"] == 80
    assert "q:1" in dry_run(snap, patch).changed_entities
    assert snap.to_graph().get_node("q:1").attrs["reward_gold"] == 120

    stale = patch.model_copy(update={"base_snapshot_id": "sha256:stale"})
    with pytest.raises(PatchRejected, match="base snapshot mismatch"):
        apply_patch(snap, stale)
    assert snap.to_graph().get_node("q:1").attrs["reward_gold"] == 120


def test_old_value_mismatch_is_rejected():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="o1", op="set_entity_attr", target="q:1.reward_gold",
                old_value=999, new_value=80),
    ])
    with pytest.raises(PatchRejected) as exc_info:
        apply_patch(snap, patch)
    assert exc_info.value.op_id == "o1"


def test_add_entity_applies():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="o1", op="add_entity", target="item:1",
                new_value={"type": "ITEM", "attrs": {"name": "Sword"}}),
    ])
    snap2 = apply_patch(snap, patch)
    node = snap2.to_graph().get_node("item:1")
    assert node is not None
    assert node.type == NodeType.ITEM
    assert node.attrs["name"] == "Sword"


def test_delete_entity_applies():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="o1", op="delete_entity", target="npc:1"),
    ])
    snap2 = apply_patch(snap, patch)
    assert snap2.to_graph().get_node("npc:1") is None


def test_add_relation_applies():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="o1", op="add_relation", target="rel:2",
                new_value={"type": "TALKS_TO", "src_id": "q:1", "dst_id": "npc:1"}),
    ])
    snap2 = apply_patch(snap, patch)
    rel = snap2.to_graph().get_relation("rel:2")
    assert rel is not None
    assert rel.type == EdgeType.TALKS_TO
    assert rel.src_id == "q:1" and rel.dst_id == "npc:1"


def test_delete_relation_applies():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="o1", op="delete_relation", target="rel:1"),
    ])
    snap2 = apply_patch(snap, patch)
    assert snap2.to_graph().get_relation("rel:1") is None


def test_set_relation_attr_applies():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="o1", op="set_relation_attr", target="rel:1.note",
                old_value="start", new_value="updated"),
    ])
    snap2 = apply_patch(snap, patch)
    rel = snap2.to_graph().get_relation("rel:1")
    assert rel.attrs["note"] == "updated"


def test_replace_subgraph_applies():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="o1", op="replace_subgraph", target="subgraph:1", new_value={
            "entities": [{"id": "q:1", "type": "QUEST", "attrs": {"reward_gold": 50}}],
            "relations": [],
        }),
    ])
    snap2 = apply_patch(snap, patch)
    node = snap2.to_graph().get_node("q:1")
    assert node.attrs["reward_gold"] == 50


def test_precondition_failure_is_rejected():
    snap = _base_snapshot()
    patch = _patch(
        snap,
        [TypedOp(op_id="o1", op="set_entity_attr", target="q:1.reward_gold", new_value=1)],
        preconditions=[{"kind": "entity_exists", "id": "does_not_exist"}],
    )
    with pytest.raises(PatchRejected):
        apply_patch(snap, patch)


def test_dry_run_returns_diff_without_mutating_base():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="o1", op="set_entity_attr", target="q:1.reward_gold",
                old_value=120, new_value=80),
    ])
    d = dry_run(snap, patch)
    assert "q:1" in d.changed_entities
    assert snap.to_graph().get_node("q:1").attrs["reward_gold"] == 120


def test_dry_run_lets_rejection_propagate():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="o1", op="set_entity_attr", target="q:1.reward_gold",
                old_value=999, new_value=80),
    ])
    with pytest.raises(PatchRejected):
        dry_run(snap, patch)


@pytest.mark.parametrize(
    "ops",
    [
        [],
        [
            TypedOp(
                op_id="add",
                op="add_entity",
                target="item:new",
                new_value={"type": "ITEM", "attrs": {}},
            )
        ],
        [TypedOp(op_id="delete", op="delete_relation", target="rel:1")],
        [
            TypedOp(
                op_id="set",
                op="set_entity_attr",
                target="q:1.reward_gold",
                old_value=120,
                new_value=80,
            )
        ],
    ],
)
def test_stale_base_rejects_every_patch_shape_before_work(ops):
    snap = _base_snapshot()
    patch = _patch(snap, ops).model_copy(
        update={"base_snapshot_id": "sha256:stale"}
    )

    with pytest.raises(PatchRejected, match="base snapshot mismatch"):
        apply_patch(snap, patch)


def test_stale_base_rejection_precedes_malformed_precondition():
    snap = _base_snapshot()
    patch = _patch(snap, [], preconditions=[{"kind": "attr_equals"}]).model_copy(
        update={"base_snapshot_id": "sha256:stale"}
    )

    with pytest.raises(PatchRejected, match="base snapshot mismatch"):
        apply_patch(snap, patch)


@pytest.mark.parametrize(
    "condition",
    [
        {},
        {"kind": "entity_exists"},
        {"kind": "entity_exists", "id": ""},
        {"kind": "entity_exists", "id": 7},
        {"kind": "attr_equals"},
        {"kind": "attr_equals", "target": "q:1.reward_gold"},
        {"kind": "attr_equals", "target": "q:1", "value": 120},
        {"kind": "attr_equals", "target": 7, "value": 120},
    ],
)
def test_malformed_precondition_is_patch_rejected(condition):
    snap = _base_snapshot()

    with pytest.raises(PatchRejected, match="malformed precondition|unknown kind"):
        apply_patch(snap, _patch(snap, [], preconditions=[condition]))


def test_late_op_failure_never_mutates_input_snapshot():
    snap = _base_snapshot()
    patch = _patch(
        snap,
        [
            TypedOp(
                op_id="first",
                op="set_entity_attr",
                target="q:1.reward_gold",
                old_value=120,
                new_value=80,
            ),
            TypedOp(op_id="second", op="delete_entity", target="missing"),
        ],
    )

    with pytest.raises(PatchRejected):
        apply_patch(snap, patch)
    assert snap.to_graph().get_node("q:1").attrs["reward_gold"] == 120


def test_dry_run_rejects_stale_base():
    snap = _base_snapshot()
    patch = _patch(snap, []).model_copy(
        update={"base_snapshot_id": "sha256:stale"}
    )

    with pytest.raises(PatchRejected, match="base snapshot mismatch"):
        dry_run(snap, patch)
