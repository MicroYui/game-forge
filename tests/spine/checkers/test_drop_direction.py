"""Graph and ASP must agree on the producer-to-product drop direction."""

import pytest

from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.checkers.asp import ASPChecker
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ir.snapshot import Snapshot


def _snapshot_with_drop(src_id: str, dst_id: str) -> Snapshot:
    entities = [
        Entity(
            id="step",
            type=NodeType.QUEST_STEP,
            attrs={"kind": "collect", "item": "item"},
        ),
        Entity(id="item", type=NodeType.ITEM),
        Entity(id="monster", type=NodeType.MONSTER),
    ]
    relations = [
        Relation(
            id="drop",
            type=EdgeType.DROPS_FROM,
            src_id=src_id,
            dst_id=dst_id,
        )
    ]
    return Snapshot.from_entities_relations(entities, relations)


def _missing_source(checker, snapshot: Snapshot) -> list:
    return [
        finding
        for finding in checker.check(snapshot)
        if finding.defect_class == "missing_drop_source"
    ]


@pytest.mark.parametrize(
    "checker",
    [GraphChecker(), ASPChecker()],
    ids=["graph", "asp"],
)
def test_forward_drop_source_clears_missing_source(checker):
    snapshot = _snapshot_with_drop("monster", "item")

    assert not _missing_source(checker, snapshot)


@pytest.mark.parametrize(
    "checker",
    [GraphChecker(), ASPChecker()],
    ids=["graph", "asp"],
)
def test_reverse_drop_edge_cannot_masquerade_as_source(checker):
    snapshot = _snapshot_with_drop("item", "monster")

    assert _missing_source(checker, snapshot)
