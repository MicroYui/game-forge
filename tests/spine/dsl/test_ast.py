import pytest

from gameforge.spine.dsl.ast import parse_assert, select, DslError
from gameforge.spine.ir.store import IRGraph
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.dsl import Selector


def test_parse_numeric_assert_tree():
    node = parse_assert("reward_gold <= 80")
    assert node.__class__.__name__ == "Compare"
    assert "reward_gold" in __import__("gameforge.spine.dsl.ast", fromlist=["free_names"]).free_names(node)


def test_parse_rejects_non_whitelisted():
    with pytest.raises(DslError):
        parse_assert("__import__('os').system('x')")
    with pytest.raises(DslError):
        parse_assert("[x for x in y]")   # comprehension not allowed


def test_selector_filters_by_type_and_where():
    g = IRGraph()
    g.add_entity(Entity(id="q1", type=NodeType.QUEST, attrs={"region": "newbie_zone"}))
    g.add_entity(Entity(id="q2", type=NodeType.QUEST, attrs={"region": "boss_zone"}))
    got = select(g, Selector(var="q", node_type="QUEST", where={"region": "newbie_zone"}))
    assert [e.id for e in got] == ["q1"]


def test_selector_bad_type_raises():
    with pytest.raises(DslError):
        select(IRGraph(), Selector(var="x", node_type="NOT_A_TYPE"))
