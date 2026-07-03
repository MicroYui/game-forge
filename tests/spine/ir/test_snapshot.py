from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph


def _g():
    g = IRGraph()
    g.add_entity(Entity(id="npc:a", type=NodeType.NPC, attrs={"z": 1, "a": 2}))
    g.add_entity(Entity(id="item:x", type=NodeType.ITEM))
    g.add_relation(Relation(id="r1", type=EdgeType.DROPS_FROM, src_id="item:x", dst_id="npc:a"))
    return g


def test_snapshot_roundtrip_diff_empty():
    g = _g()
    snap = Snapshot.from_graph(g)
    assert snap.to_graph().diff(g).is_empty()  # contract §2.5 anchor: diff(import(export(x)),x)==∅


def test_snapshot_id_order_independent():
    snap1 = Snapshot.from_graph(_g())
    g2 = IRGraph()
    g2.add_entity(Entity(id="item:x", type=NodeType.ITEM))
    g2.add_entity(Entity(id="npc:a", type=NodeType.NPC, attrs={"a": 2, "z": 1}))
    g2.add_relation(Relation(id="r1", type=EdgeType.DROPS_FROM, src_id="item:x", dst_id="npc:a"))
    assert snap1.snapshot_id == Snapshot.from_graph(g2).snapshot_id


def test_diff_detects_attr_change():
    g = _g()
    base = Snapshot.from_graph(g)
    g.get_node("npc:a").attrs["z"] = 999
    d = Snapshot.from_graph(g).to_graph().diff(base.to_graph())
    assert not d.is_empty()
    assert "npc:a" in d.changed_entities


def test_diff_detects_add_remove():
    g = _g()
    base = Snapshot.from_graph(g).to_graph()
    g.add_entity(Entity(id="item:y", type=NodeType.ITEM))
    g.remove_relation("r1")
    d = g.diff(base)
    assert "item:y" in d.added_entities
    assert "r1" in d.removed_relations


def test_snapshot_content_excludes_non_content_fields():
    snap = Snapshot.from_graph(_g(), parent_id="sha256:parent")
    # snapshot_id must not depend on parent_id / created_at / author
    snap2 = Snapshot.from_graph(_g(), parent_id="sha256:DIFFERENT")
    assert snap.snapshot_id == snap2.snapshot_id
