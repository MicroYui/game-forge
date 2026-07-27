"""What the model is shown about the game it is extending.

The whole point is that a retrieved slice can be wrong in two directions, and
only one of them is visible. Missing the entity a document is about produces a
duplicate — which the alias work exists to prevent. Including everything anyway
produces a prompt that cannot be sent at all. So these tests assert positively on
both sides: the right thing IS there, and unrelated content is NOT.
"""

from __future__ import annotations

import json

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.grounding import (
    GroundingBudget,
    GroundingProjectionBudget,
    GroundingRetriever,
)
from gameforge.spine.ir.snapshot import Snapshot


def _budget(
    *,
    focus: int = 8,
    relations: int = 64,
    neighbors: int = 32,
    catalog: int = 50,
    max_bytes: int = 256 * 1024,
) -> GroundingBudget:
    return GroundingBudget(
        projection=GroundingProjectionBudget(
            max_focus_entities=focus,
            max_incident_relations=relations,
            max_neighbor_entities=neighbors,
            max_catalog_ids_per_type=catalog,
        ),
        max_grounding_bytes=max_bytes,
    )


def _liyue() -> Snapshot:
    return Snapshot.from_entities_relations(
        [
            Entity(id="npc:zhongli", type=NodeType.NPC, attrs={"name": "钟离", "role": "客卿"}),
            Entity(id="region:liyue", type=NodeType.REGION, attrs={"name": "璃月港"}),
            Entity(id="npc:venti", type=NodeType.NPC, attrs={"name": "温迪"}),
            Entity(id="region:mondstadt", type=NodeType.REGION, attrs={"name": "蒙德"}),
        ],
        [
            Relation(
                id="rel:zhongli_in_liyue",
                type=EdgeType.LOCATED_IN,
                src_id="npc:zhongli",
                dst_id="region:liyue",
            ),
            Relation(
                id="rel:venti_in_mondstadt",
                type=EdgeType.LOCATED_IN,
                src_id="npc:venti",
                dst_id="region:mondstadt",
            ),
        ],
    )


def test_a_declared_alias_retrieves_the_entity_no_lexical_rule_could_reach() -> None:
    """岩王帝君 and 钟离 share no characters, so nothing derives this but a person.

    Once said, the retrieval is exact. This is the whole reason the semantic step
    needs no model.
    """

    retriever = GroundingRetriever(
        _liyue(),
        declared_aliases={"岩王帝君": "npc:zhongli"},
    )

    result = retriever.retrieve("岩王帝君退隐之后，璃月的契约由谁执行？", budget=_budget())

    assert "npc:zhongli" in result.matched_entity_ids
    focus_ids = [node["id"] for node in result.focus_nodes]
    assert "npc:zhongli" in focus_ids
    assert dict(result.focus_nodes[focus_ids.index("npc:zhongli")])["attrs"] == {
        "name": "钟离",
        "role": "客卿",
    }


def test_an_undeclared_name_retrieves_nothing_rather_than_guessing() -> None:
    """Without a declaration the system must not invent the equivalence itself."""

    retriever = GroundingRetriever(_liyue())

    result = retriever.retrieve("岩王帝君退隐之后由谁执行契约？", budget=_budget())

    assert result.matched_entity_ids == ()


def test_unrelated_material_pulls_in_no_unrelated_content() -> None:
    """A document about Mondstadt must not drag Liyue into the prompt."""

    retriever = GroundingRetriever(_liyue())

    result = retriever.retrieve("温迪在蒙德组织了一场风花节。", budget=_budget())

    assert set(result.matched_entity_ids) == {"npc:venti", "region:mondstadt"}
    rendered = result.to_prompt_json()
    assert "npc:zhongli" not in json.dumps(
        [dict(node) for node in result.focus_nodes], ensure_ascii=False
    )
    # The catalog still names everything that exists, so the model can see that
    # 钟离 is already there and must not add a second one.
    assert "npc:zhongli" in rendered


def test_a_seed_carries_its_one_hop_neighbourhood_and_stops_there() -> None:
    snapshot = Snapshot.from_entities_relations(
        [
            Entity(id="a", type=NodeType.NPC, attrs={"name": "alpha"}),
            Entity(id="b", type=NodeType.REGION, attrs={"name": "beta", "secret": "hidden"}),
            Entity(id="c", type=NodeType.ITEM, attrs={"name": "gamma"}),
        ],
        [
            Relation(id="r:ab", type=EdgeType.LOCATED_IN, src_id="a", dst_id="b"),
            Relation(id="r:bc", type=EdgeType.CONTAINS, src_id="b", dst_id="c"),
        ],
    )
    retriever = GroundingRetriever(snapshot)

    result = retriever.retrieve("alpha does something new", budget=_budget())

    assert [node["id"] for node in result.focus_nodes] == ["a"]
    assert [relation["id"] for relation in result.incident_relations] == ["r:ab"]
    # A neighbour is named, not copied: no attrs beyond its name.
    assert [dict(node) for node in result.neighbor_nodes] == [
        {"id": "b", "type": "REGION", "name": "beta"}
    ]
    assert "gamma" not in json.dumps(
        [dict(node) for node in result.neighbor_nodes], ensure_ascii=False
    )


def test_an_empty_bootstrap_graph_degrades_without_raising() -> None:
    """Every project starts here, so this path runs before any other."""

    retriever = GroundingRetriever(Snapshot(entities={}, relations={}))

    result = retriever.retrieve("新增一名风暴观测员。", budget=_budget())

    assert result.focus_nodes == ()
    assert result.neighbor_nodes == ()
    assert result.entity_catalog == {}
    assert result.edge_types  # the closed vocabulary is always available
    assert result.complete is True


def test_no_match_on_a_populated_graph_still_shows_real_ids() -> None:
    """Material about all-new content must still see the taxonomy in use."""

    retriever = GroundingRetriever(_liyue())

    result = retriever.retrieve("新增一名风暴观测员，安排在一个新港口。", budget=_budget())

    assert result.matched_entity_ids == ()
    assert result.focus_nodes == ()
    assert {node["id"] for node in result.neighbor_nodes} == {
        "npc:venti",
        "npc:zhongli",
        "region:liyue",
        "region:mondstadt",
    }


def test_retrieval_is_byte_identical_for_equal_content_in_any_insertion_order() -> None:
    """Two snapshots holding the same graph must ground identically.

    A truncated catalog must be a function of the content, never of the order a
    dict happened to be filled in — otherwise the same game grounds differently
    depending on how it was loaded, and replay stops meaning anything.
    """

    entities = [
        Entity(id=f"quest:q{index:03d}", type=NodeType.QUEST, attrs={"name": f"q{index}"})
        for index in range(80)
    ]
    forward = Snapshot.from_entities_relations(entities, [])
    reversed_order = Snapshot.from_entities_relations(list(reversed(entities)), [])

    budget = _budget()
    first = GroundingRetriever(forward).retrieve("quest:q005 需要调整", budget=budget)
    second = GroundingRetriever(reversed_order).retrieve("quest:q005 需要调整", budget=budget)

    assert first.to_prompt_json() == second.to_prompt_json()


def test_a_declared_alias_outranks_a_longer_lexical_match() -> None:
    snapshot = Snapshot.from_entities_relations(
        [
            Entity(id="npc:zhongli", type=NodeType.NPC, attrs={"name": "钟离"}),
            Entity(id="npc:other", type=NodeType.NPC, attrs={"name": "岩王帝君的侍从"}),
        ],
        [],
    )
    retriever = GroundingRetriever(snapshot, declared_aliases={"岩王帝君": "npc:zhongli"})

    result = retriever.retrieve("岩王帝君的侍从来了", budget=_budget(focus=2))

    assert result.matched_entity_ids[0] == "npc:zhongli"


def test_the_byte_ceiling_drops_neighbours_then_relations_then_focus() -> None:
    """Count caps cannot bound bytes — one entity's attrs are unbounded."""

    snapshot = Snapshot.from_entities_relations(
        [
            Entity(id="a", type=NodeType.NPC, attrs={"name": "alpha", "lore": "x" * 4000}),
            Entity(id="b", type=NodeType.REGION, attrs={"name": "beta"}),
        ],
        [Relation(id="r:ab", type=EdgeType.LOCATED_IN, src_id="a", dst_id="b")],
    )
    retriever = GroundingRetriever(snapshot)

    result = retriever.retrieve("alpha", budget=_budget(max_bytes=1200))

    assert len(result.to_prompt_json().encode("utf-8")) <= 1200
    assert result.complete is False
    assert result.omitted["neighbor_nodes"] >= 1
    assert result.focus_nodes == ()
    assert result.omitted["focus_nodes"] >= 1


def test_a_single_character_name_is_not_a_seed() -> None:
    """One character matches almost any prose, which would make everything a seed."""

    snapshot = Snapshot.from_entities_relations(
        [Entity(id="x", type=NodeType.ITEM, attrs={"name": "刀"})],
        [],
    )
    retriever = GroundingRetriever(snapshot)

    assert retriever.retrieve("这把刀很锋利", budget=_budget()).matched_entity_ids == ()


def test_a_name_no_identifier_rule_accepts_does_not_break_retrieval() -> None:
    """A display name is prose, not an identifier.

    `canonical_identity_token("！！！")` raises — it strips punctuation and refuses
    an empty result. Names must therefore never go through it, and an entity a
    planner named in punctuation must still be indexable rather than crash the
    extraction of an unrelated document.
    """

    snapshot = Snapshot.from_entities_relations(
        [
            Entity(id="item:blade", type=NodeType.ITEM, attrs={"name": "！！！"}),
            Entity(id="npc:tao", type=NodeType.NPC, attrs={"name": "老陶"}),
        ],
        [],
    )
    retriever = GroundingRetriever(snapshot)

    assert retriever.retrieve("老陶来了", budget=_budget()).matched_entity_ids == ("npc:tao",)
    # And the odd name still matches itself — NFKC folds it to "!!!", which is a
    # form like any other.
    assert retriever.retrieve("！！！ 出现了", budget=_budget()).matched_entity_ids == (
        "item:blade",
    )


def test_a_name_that_folds_to_nothing_is_never_a_surface_form() -> None:
    snapshot = Snapshot.from_entities_relations(
        [Entity(id="item:blade", type=NodeType.ITEM, attrs={"name": "   "})],
        [],
    )

    result = GroundingRetriever(snapshot).retrieve("some unrelated prose", budget=_budget())

    assert result.matched_entity_ids == ()


def test_retrieval_never_mutates_the_snapshot_it_reads() -> None:
    """The index skips the usual deep copy, so this is what guards the caller."""

    snapshot = _liyue()
    before = json.dumps(snapshot.content_payload, sort_keys=True, default=str)

    GroundingRetriever(snapshot).retrieve("钟离在璃月港", budget=_budget())

    assert json.dumps(snapshot.content_payload, sort_keys=True, default=str) == before


def test_a_declaration_naming_no_entity_fails_closed_at_index_time() -> None:
    with pytest.raises(IntegrityViolation):
        GroundingRetriever(_liyue(), declared_aliases={"岩王帝君": "npc:morax"})
