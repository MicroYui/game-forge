from __future__ import annotations

from hypothesis import given, strategies as st

from gameforge.bench.hed.delta import (
    AtomicDelta,
    semantic_delta,
    symmetric_difference_distance,
)
from gameforge.contracts.ir import (
    EdgeType,
    Entity,
    NodeType,
    Relation,
    SourceRef,
)
from gameforge.spine.ir.snapshot import Snapshot


HASH_A = "a" * 64
HASH_B = "b" * 64


def _entity(
    entity_id: str,
    node_type: NodeType = NodeType.QUEST,
    *,
    attrs: dict | None = None,
    row: int = 1,
) -> Entity:
    return Entity(
        id=entity_id,
        type=node_type,
        attrs=attrs or {},
        source_ref=SourceRef(
            adapter="fixture",
            file="data/content.txt",
            sheet="mission",
            row=row,
            column=f"line:{row + 1}",
        ),
    )


def _relation(
    relation_id: str,
    src_id: str,
    dst_id: str,
    *,
    attrs: dict | None = None,
    row: int = 1,
) -> Relation:
    return Relation(
        id=relation_id,
        type=EdgeType.REQUIRES,
        src_id=src_id,
        dst_id=dst_id,
        attrs=attrs,
        source_ref=SourceRef(
            adapter="fixture",
            file="data/content.txt",
            sheet="mission",
            row=row,
            column=f"line:{row + 1}",
        ),
    )


def _snapshot(
    entities: list[Entity],
    relations: list[Relation] | None = None,
) -> Snapshot:
    return Snapshot.from_entities_relations(entities, relations or [])


def test_source_envelope_source_refs_and_relation_ids_are_not_semantic() -> None:
    before = _snapshot(
        [
            _entity(
                "quest:q",
                attrs={
                    "kind": "story",
                    "source_chunk_b64": "YQ==",
                    "source_kind": "mission",
                    "source_name": "Q",
                    "source_order": 0,
                    "reader_version": "reader@1",
                },
                row=1,
            ),
            _entity("quest:required"),
        ],
        [_relation("relation:line-2", "quest:q", "quest:required", row=1)],
    )
    after = _snapshot(
        [
            _entity(
                "quest:q",
                attrs={
                    "kind": "story",
                    "source_chunk_b64": "Yg==",
                    "source_kind": "mission",
                    "source_name": "Renamed source label",
                    "source_order": 99,
                    "reader_version": "reader@1",
                },
                row=50,
            ),
            _entity("quest:required", row=50),
        ],
        [_relation("relation:line-99", "quest:q", "quest:required", row=50)],
    )

    assert semantic_delta(before, after) == ()


def test_opaque_content_addressed_ids_are_matched_by_semantics() -> None:
    before_opaque = f"dialogue:option:{HASH_A}"
    after_opaque = f"dialogue:option:{HASH_B}"
    before = _snapshot(
        [
            _entity("quest:q"),
            _entity(before_opaque, NodeType.DIALOGUE_NODE, attrs={"choice": "accept"}),
        ],
        [_relation("relation:before", "quest:q", before_opaque)],
    )
    after = _snapshot(
        [
            _entity("quest:q", row=20),
            _entity(
                after_opaque,
                NodeType.DIALOGUE_NODE,
                attrs={"choice": "accept"},
                row=20,
            ),
        ],
        [_relation("relation:after", "quest:q", after_opaque, row=20)],
    )

    assert semantic_delta(before, after) == ()


def test_stable_entity_attribute_change_is_one_atomic_delta() -> None:
    before = _snapshot([_entity("quest:q", attrs={"reward": 10, "kind": "story"})])
    after = _snapshot([_entity("quest:q", attrs={"reward": 20, "kind": "story"})])

    assert semantic_delta(before, after) == (
        AtomicDelta(
            kind="set_entity_attr",
            target="quest:q",
            field="attrs.reward",
            old_json="10",
            new_json="20",
        ),
    )


def test_relation_removal_is_one_atomic_semantic_delta() -> None:
    entities = [_entity("quest:q"), _entity("quest:required")]
    before = _snapshot(
        entities,
        [_relation("source-derived-id", "quest:q", "quest:required")],
    )
    after = _snapshot(entities)

    delta = semantic_delta(before, after)

    assert len(delta) == 1
    assert delta[0].kind == "delete_relation"
    assert delta[0].old_json is not None
    assert delta[0].new_json is None


def test_symmetric_difference_distance_is_exact_bounded_and_symmetric() -> None:
    shared = AtomicDelta("delete_relation", "relation:a", None, "{}", None)
    human_only = AtomicDelta("add_relation", "relation:b", None, None, "{}")
    agent_only = AtomicDelta("add_relation", "relation:c", None, None, "{}")
    human = (shared, human_only)
    agent = (shared, agent_only)

    assert symmetric_difference_distance(agent, human) == (2, 2 / 3)
    assert symmetric_difference_distance(human, agent) == (2, 2 / 3)
    assert symmetric_difference_distance((), ()) == (0, 0.0)


def test_semantic_delta_is_stable_under_snapshot_insertion_order() -> None:
    first = _entity("quest:first", attrs={"reward": 1})
    second = _entity("quest:second", attrs={"reward": 2})
    before_a = _snapshot([first, second])
    before_b = _snapshot([second, first])
    after_a = _snapshot(
        [
            first.model_copy(update={"attrs": {"reward": 3}}),
            second,
        ]
    )
    after_b = _snapshot(
        [
            second,
            first.model_copy(update={"attrs": {"reward": 3}}),
        ]
    )

    assert semantic_delta(before_a, after_a) == semantic_delta(before_b, after_b)


@given(st.dictionaries(st.text(min_size=1), st.integers(), max_size=8))
def test_identical_snapshots_have_no_semantic_delta(attrs: dict[str, int]) -> None:
    snapshot = _snapshot([_entity("quest:q", attrs=attrs)])

    assert semantic_delta(snapshot, snapshot) == ()


@given(
    st.sets(st.integers(min_value=0, max_value=20), max_size=12),
    st.sets(st.integers(min_value=0, max_value=20), max_size=12),
)
def test_jaccard_distance_is_symmetric_and_bounded(
    first_ids: set[int],
    second_ids: set[int],
) -> None:
    def deltas(values: set[int]) -> tuple[AtomicDelta, ...]:
        return tuple(
            AtomicDelta("add_entity", f"entity:{value}", None, None, "{}")
            for value in sorted(values)
        )

    forward = symmetric_difference_distance(deltas(first_ids), deltas(second_ids))
    reverse = symmetric_difference_distance(deltas(second_ids), deltas(first_ids))

    assert forward == reverse
    assert forward[0] >= 0
    assert 0.0 <= forward[1] <= 1.0
