"""ASPChecker (M1 Task 5): fact generation + built-in Clingo rules + grounding
budget degradation.
"""

from __future__ import annotations

import clingo

from gameforge.contracts.ir import Entity, EdgeType, NodeType, Relation
from gameforge.spine.checkers.asp import ASPChecker, ir_to_asp_facts
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph


def _snap(entities, relations):
    return Snapshot.from_entities_relations(entities, relations)


def _findings(entities, relations, defect_class, **kwargs):
    return [
        f for f in ASPChecker(**kwargs).check(_snap(entities, relations))
        if f.defect_class == defect_class
    ]


# --- ir_to_asp_facts: pure, independently unit-testable ---

def test_ir_to_asp_facts_generates_node_and_edge_atoms():
    g = IRGraph()
    g.add_entity(Entity(id="q:1", type=NodeType.QUEST, attrs={"region": "newbie"}))
    g.add_entity(Entity(id="s:1", type=NodeType.QUEST_STEP, attrs={"kind": "collect", "item": "i:1"}))
    g.add_relation(
        Relation(
            id="r:1",
            type=EdgeType.HAS_STEP,
            src_id="q:1",
            dst_id="s:1",
            attrs={"repeatability": "once"},
        )
    )

    facts = ir_to_asp_facts(g)

    assert 'node("q:1", "QUEST").' in facts
    assert 'node("s:1", "QUEST_STEP").' in facts
    assert 'edge("r:1", "HAS_STEP", "q:1", "s:1").' in facts
    assert 'edge_attr("r:1", "repeatability", "once").' in facts
    assert 'attr("s:1", "kind", "collect").' in facts
    assert 'attr("s:1", "item", "i:1").' in facts


def test_ir_to_asp_facts_is_valid_clingo_program_and_deterministic():
    g = IRGraph()
    g.add_entity(Entity(id="q1", type=NodeType.QUEST))
    facts = ir_to_asp_facts(g)
    facts2 = ir_to_asp_facts(g)
    assert facts == facts2  # deterministic ordering

    ctl = clingo.Control()
    ctl.add("base", [], facts)
    ctl.ground([("base", [])])  # must not raise


# --- ASPChecker.check: cyclic_dependency ---

def test_cyclic_dependency_detected_by_asp():
    ents = [Entity(id=f"s{i}", type=NodeType.QUEST_STEP) for i in (1, 2, 3)]
    rels = [
        Relation(id="a", type=EdgeType.PRECEDES, src_id="s1", dst_id="s2"),
        Relation(id="b", type=EdgeType.PRECEDES, src_id="s2", dst_id="s3"),
        Relation(id="c", type=EdgeType.PRECEDES, src_id="s3", dst_id="s1"),
    ]
    fs = _findings(ents, rels, "cyclic_dependency")
    assert len(fs) == 1
    assert set(fs[0].entities) == {"s1", "s2", "s3"}
    assert fs[0].oracle_type == "deterministic"
    assert fs[0].status == "confirmed"
    assert fs[0].source == "checker"
    assert fs[0].producer_id == "asp"
    assert fs[0].evidence


def test_clean_graph_has_no_violations():
    ents = [Entity(id=f"s{i}", type=NodeType.QUEST_STEP) for i in (1, 2, 3)]
    rels = [
        Relation(id="a", type=EdgeType.PRECEDES, src_id="s1", dst_id="s2"),
        Relation(id="b", type=EdgeType.PRECEDES, src_id="s2", dst_id="s3"),
    ]
    fs = ASPChecker().check(_snap(ents, rels))
    assert fs == []


def test_self_requirement_is_detected_by_asp():
    quest = Entity(id="quest:q", type=NodeType.QUEST)
    requirement = Relation(
        id="requires-self",
        type=EdgeType.REQUIRES,
        src_id=quest.id,
        dst_id=quest.id,
    )

    findings = _findings([quest], [requirement], "cyclic_dependency")

    assert len(findings) == 1
    assert findings[0].entities == [quest.id]


def test_once_only_edge_is_excluded_from_asp_dependency_cycles():
    entities = [
        Entity(id="dialogue:a", type=NodeType.DIALOGUE_NODE),
        Entity(id="dialogue:b", type=NodeType.DIALOGUE_NODE),
    ]
    relations = [
        Relation(
            id="forward",
            type=EdgeType.PRECEDES,
            src_id="dialogue:a",
            dst_id="dialogue:b",
        ),
        Relation(
            id="bounded-return",
            type=EdgeType.PRECEDES,
            src_id="dialogue:b",
            dst_id="dialogue:a",
            attrs={"repeatability": "once"},
        ),
    ]

    assert _findings(entities, relations, "cyclic_dependency") == []


# --- ASPChecker.check: missing_drop_source ---

def test_missing_drop_source_detected_by_asp():
    ents = [
        Entity(id="s1", type=NodeType.QUEST_STEP, attrs={"kind": "collect", "item": "i1"}),
        Entity(id="i1", type=NodeType.ITEM),
    ]
    fs = _findings(ents, [], "missing_drop_source")
    assert len(fs) == 1
    assert set(fs[0].entities) == {"s1", "i1"}


def test_drop_source_present_is_silent():
    ents = [
        Entity(id="s1", type=NodeType.QUEST_STEP, attrs={"kind": "collect", "item": "i1"}),
        Entity(id="i1", type=NodeType.ITEM),
        Entity(id="m1", type=NodeType.MONSTER),
    ]
    rels = [Relation(id="d1", type=EdgeType.DROPS_FROM, src_id="m1", dst_id="i1")]
    fs = _findings(ents, rels, "missing_drop_source")
    assert fs == []


# --- Grounding budget / degradation (M1-D7) ---

def test_zero_budget_forces_unproven_never_silently_passes():
    ents = [Entity(id=f"s{i}", type=NodeType.QUEST_STEP) for i in (1, 2, 3)]
    rels = [
        Relation(id="a", type=EdgeType.PRECEDES, src_id="s1", dst_id="s2"),
        Relation(id="b", type=EdgeType.PRECEDES, src_id="s2", dst_id="s3"),
        Relation(id="c", type=EdgeType.PRECEDES, src_id="s3", dst_id="s1"),
    ]
    fs = ASPChecker(grounding_budget_atoms=0).check(_snap(ents, rels))
    assert fs, "budget exceeded must never silently pass -> must emit findings"
    assert all(f.status == "unproven" for f in fs)
    assert all(f.oracle_type == "deterministic" for f in fs)
    defect_classes = {f.defect_class for f in fs}
    assert "cyclic_dependency" in defect_classes
    assert "missing_drop_source" in defect_classes


def test_normal_small_graph_does_not_trigger_budget_degradation():
    ents = [Entity(id="q1", type=NodeType.QUEST)]
    fs = ASPChecker().check(_snap(ents, []))
    assert all(f.status != "unproven" for f in fs)
