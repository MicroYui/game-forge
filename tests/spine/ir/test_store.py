from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.store import IRGraph


def _q():
    g = IRGraph()
    g.add_entity(Entity(id="q1", type=NodeType.QUEST))
    g.add_entity(Entity(id="s1", type=NodeType.QUEST_STEP, attrs={"kind": "talk"}))
    g.add_relation(Relation(id="r1", type=EdgeType.HAS_STEP, src_id="q1", dst_id="s1"))
    return g


def test_get_node_and_relation():
    g = _q()
    assert g.get_node("q1").type is NodeType.QUEST
    assert g.get_relation("r1").dst_id == "s1"
    assert g.get_node("missing") is None


def test_neighbors_by_edge_type_and_direction():
    g = _q()
    assert [r.dst_id for r in g.neighbors("q1", EdgeType.HAS_STEP)] == ["s1"]
    assert [r.src_id for r in g.neighbors("s1", EdgeType.HAS_STEP, direction="in")] == ["q1"]
    assert g.neighbors("q1", EdgeType.PRECEDES) == []


def test_nodes_of_type_and_subgraph():
    g = _q()
    assert {e.id for e in g.nodes_of_type(NodeType.QUEST_STEP)} == {"s1"}
    sub = g.subgraph({NodeType.QUEST})
    assert {e.id for e in sub.nodes_of_type(NodeType.QUEST)} == {"q1"}
    assert sub.nodes_of_type(NodeType.QUEST_STEP) == []


def test_path_exists_via_ir_edges():
    g = _q()
    assert g.path_exists("q1", "s1", via=EdgeType.HAS_STEP) is True
    assert g.path_exists("s1", "q1", via=EdgeType.HAS_STEP) is False


def test_remove_relation_and_entity():
    g = _q()
    g.remove_relation("r1")
    assert g.get_relation("r1") is None
    assert g.neighbors("q1", EdgeType.HAS_STEP) == []
    g.remove_entity("s1")
    assert g.get_node("s1") is None
