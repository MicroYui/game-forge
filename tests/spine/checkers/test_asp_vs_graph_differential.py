"""Differential test (M1 Task 5, contract §3 / §12A.1 anchor): ASPChecker (Clingo,
an independent solver-based engine) must agree with GraphChecker (hand-rolled
BFS/Tarjan) on the SET of (defect_class, sorted(entities)) findings for the
shared structural defect classes {cyclic_dependency, missing_drop_source}.

This is a genuine cross-check between two independently-implemented engines —
ASPChecker never calls GraphChecker (and vice versa); each derives its verdict
from its own encoding of the same IR graph.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from gameforge.contracts.ir import Entity, EdgeType, NodeType, Relation
from gameforge.spine.checkers.asp import ASPChecker
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ir.snapshot import Snapshot

SHARED_CLASSES = {"cyclic_dependency", "missing_drop_source"}


def _defect_set(findings):
    return {
        (f.defect_class, tuple(sorted(f.entities)))
        for f in findings
        if f.defect_class in SHARED_CLASSES
    }


@st.composite
def _quest_step_graph(draw):
    n_quests = draw(st.integers(min_value=1, max_value=3))
    n_steps = draw(st.integers(min_value=1, max_value=6))
    n_items = draw(st.integers(min_value=0, max_value=3))
    n_sources = draw(st.integers(min_value=0, max_value=2))

    quests = [f"q{i}" for i in range(n_quests)]
    steps = [f"s{i}" for i in range(n_steps)]
    items = [f"i{i}" for i in range(n_items)]
    sources = [f"src{i}" for i in range(n_sources)]

    entities = [Entity(id=q, type=NodeType.QUEST) for q in quests]
    entities += [Entity(id=src, type=NodeType.MONSTER) for src in sources]

    relations = []
    rid = 0

    # each step HAS_STEP-assigned to a random quest
    step_kinds = {}
    for s in steps:
        owner = draw(st.sampled_from(quests))
        relations.append(Relation(id=f"r{rid}", type=EdgeType.HAS_STEP, src_id=owner, dst_id=s))
        rid += 1
        is_collect = draw(st.booleans())
        if is_collect and items:
            item = draw(st.sampled_from(items))
            step_kinds[s] = item
            entities.append(Entity(id=s, type=NodeType.QUEST_STEP,
                                    attrs={"kind": "collect", "item": item}))
        else:
            entities.append(Entity(id=s, type=NodeType.QUEST_STEP))

    entities += [Entity(id=i, type=NodeType.ITEM) for i in items]

    # random PRECEDES edges among steps (may create cycles)
    precedes_pairs = draw(
        st.lists(
            st.tuples(st.sampled_from(steps), st.sampled_from(steps)),
            max_size=8,
        )
    )
    for a, b in precedes_pairs:
        relations.append(Relation(id=f"r{rid}", type=EdgeType.PRECEDES, src_id=a, dst_id=b))
        rid += 1

    # random GRANTS/DROPS_FROM edges from sources -> items (some items get no source)
    if sources and items:
        source_edges = draw(
            st.lists(
                st.tuples(st.sampled_from(sources), st.sampled_from(items),
                          st.sampled_from([EdgeType.GRANTS, EdgeType.DROPS_FROM])),
                max_size=6,
            )
        )
        for src, item, etype in source_edges:
            relations.append(Relation(id=f"r{rid}", type=etype, src_id=src, dst_id=item))
            rid += 1

    return entities, relations


@settings(max_examples=200)
@given(_quest_step_graph())
def test_asp_and_graph_agree_on_shared_defect_classes(graph):
    entities, relations = graph
    snap = Snapshot.from_entities_relations(entities, relations)

    asp_findings = ASPChecker().check(snap)
    graph_findings = GraphChecker().check(snap)

    assert _defect_set(asp_findings) == _defect_set(graph_findings)
