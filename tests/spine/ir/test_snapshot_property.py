"""Property tests: snapshot round-trip is lossless and snapshot_id is order-independent."""

from hypothesis import given, settings
from hypothesis import strategies as st

from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph

_attr_values = st.one_of(
    st.integers(min_value=-1000, max_value=1000),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=8),
    st.booleans(),
)
_attrs = st.dictionaries(st.text(min_size=1, max_size=6), _attr_values, max_size=4)


@st.composite
def _graphs(draw):
    ids = draw(st.lists(st.text(min_size=1, max_size=6), min_size=1, max_size=6, unique=True))
    entities = [
        Entity(id=eid, type=draw(st.sampled_from(list(NodeType))), attrs=draw(_attrs))
        for eid in ids
    ]
    n_rel = draw(st.integers(min_value=0, max_value=6))
    relations = []
    for i in range(n_rel):
        relations.append(
            Relation(
                id=f"rel{i}",
                type=draw(st.sampled_from(list(EdgeType))),
                src_id=draw(st.sampled_from(ids)),
                dst_id=draw(st.sampled_from(ids)),
            )
        )
    return entities, relations


def _build(entities, relations):
    g = IRGraph()
    for e in entities:
        g.add_entity(e)
    for r in relations:
        g.add_relation(r)
    return g


@settings(max_examples=150)
@given(_graphs())
def test_roundtrip_is_lossless(data):
    entities, relations = data
    g = _build(entities, relations)
    assert Snapshot.from_graph(g).to_graph().diff(g).is_empty()


@settings(max_examples=150)
@given(_graphs(), st.randoms(use_true_random=False))
def test_snapshot_id_independent_of_insertion_order(data, rnd):
    entities, relations = data
    e2, r2 = list(entities), list(relations)
    rnd.shuffle(e2)
    rnd.shuffle(r2)
    assert Snapshot.from_graph(_build(entities, relations)).snapshot_id == \
        Snapshot.from_graph(_build(e2, r2)).snapshot_id
