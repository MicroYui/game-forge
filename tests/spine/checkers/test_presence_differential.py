"""Cross-check the three presence backends against each other.

Publication needs two engines that decided a rule independently, so what matters
here is not that any one backend is right but that three different resolution
strategies — walk, index, ASP — cannot be told apart by their verdicts.
"""

from __future__ import annotations

import pytest

from gameforge.contracts.dsl import Constraint, Selector
from gameforge.contracts.ir import Entity, NodeType
from gameforge.spine.checkers.presence import (
    AttributePresenceChecker,
    EagerPathIndexPresenceReference,
)
from gameforge.spine.checkers.presence_asp import ClingoPresenceReference
from gameforge.spine.dsl.presence import parse_presence_spec, presence_witnesses
from gameforge.spine.ir.snapshot import Snapshot

_BACKENDS = (
    AttributePresenceChecker,
    EagerPathIndexPresenceReference,
    ClingoPresenceReference,
)


def _constraint(expression: str, *, where: dict[str, object] | None = None) -> Constraint:
    return Constraint(
        id="constraint:presence",
        kind="structural",
        oracle="deterministic",
        severity="major",
        scope=Selector(var="n", node_type="NPC", where=where or {}),
        **{"assert": expression},
    )


def _verdicts(constraint: Constraint, snapshot: Snapshot) -> list[list[tuple[object, ...]]]:
    return [
        sorted(
            (finding.defect_class, tuple(finding.entities), finding.status, finding.message)
            for finding in backend(constraint).check(snapshot)
        )
        for backend in _BACKENDS
    ]


def _snapshot(*entities: Entity) -> Snapshot:
    return Snapshot.from_entities_relations(list(entities), [])


@pytest.mark.parametrize(
    "expression",
    [
        "has(faction)",
        "is_text(faction)",
        "is_object(profile)",
        "has(profile.home) and is_text(faction)",
        "is_text(profile.home.region)",
    ],
)
def test_every_backend_agrees_on_the_spec_s_own_witnesses(expression: str) -> None:
    constraint = _constraint(expression)
    spec = parse_presence_spec(constraint)
    assert spec is not None
    witnesses = presence_witnesses(spec)
    assert witnesses is not None
    dirty, clean = witnesses

    walk, index, asp = _verdicts(constraint, dirty)
    assert walk == index == asp
    assert walk, "the dirty witness must actually be rejected"
    assert all(item[2] == "confirmed" for item in walk)

    assert _verdicts(constraint, clean) == [[], [], []]


@pytest.mark.parametrize(
    "attrs",
    [
        {},
        {"faction": "liyue"},
        {"faction": True},
        {"faction": 7},
        {"faction": {}},
        {"faction": {"name": "liyue"}},
        {"profile": {"home": "liyue"}},
        {"profile": {"home": {"region": "liyue"}}},
        {"profile": "liyue"},
        {"profile.home": "forged"},
        {"faction": None},
        {"faction": ["liyue"]},
    ],
)
@pytest.mark.parametrize(
    "expression",
    ["has(faction)", "is_text(faction)", "is_object(profile)", "is_text(profile.home)"],
)
def test_three_strategies_cannot_be_told_apart(expression: str, attrs: dict) -> None:
    snapshot = _snapshot(Entity(id="npc:1", type=NodeType.NPC, attrs=attrs))
    walk, index, asp = _verdicts(_constraint(expression), snapshot)
    assert walk == index == asp


def test_a_forged_dotted_key_does_not_satisfy_a_nested_requirement() -> None:
    """``{"profile.home": …}`` is one key, not a path — all three must say so."""

    snapshot = _snapshot(
        Entity(id="npc:1", type=NodeType.NPC, attrs={"profile.home": "liyue"}),
        Entity(id="npc:2", type=NodeType.NPC, attrs={"profile": {"home": "liyue"}}),
    )
    walk, index, asp = _verdicts(_constraint("is_text(profile.home)"), snapshot)
    assert walk == index == asp
    assert [item[1] for item in walk] == [("npc:1",)]


def test_selection_is_re_derived_not_borrowed() -> None:
    """The ASP backend must reach the same governed set from its own facts."""

    snapshot = _snapshot(
        Entity(id="npc:1", type=NodeType.NPC, attrs={"tier": "elite"}),
        Entity(id="npc:2", type=NodeType.NPC, attrs={"tier": "common"}),
        Entity(id="item:1", type=NodeType.ITEM, attrs={"tier": "elite"}),
    )
    walk, index, asp = _verdicts(_constraint("has(faction)", where={"tier": "elite"}), snapshot)
    assert walk == index == asp
    assert [item[1] for item in walk] == [("npc:1",)]


def test_a_boolean_where_value_does_not_match_its_own_spelling() -> None:
    """``True`` and ``"true"`` stay distinct, or ASP would widen the governed set."""

    snapshot = _snapshot(
        Entity(id="npc:1", type=NodeType.NPC, attrs={"unique": True}),
        Entity(id="npc:2", type=NodeType.NPC, attrs={"unique": "true"}),
    )
    walk, index, asp = _verdicts(_constraint("has(faction)", where={"unique": True}), snapshot)
    assert walk == index == asp
    assert [item[1] for item in walk] == [("npc:1",)]


def test_an_empty_graph_decides_quietly_rather_than_erroring() -> None:
    """A project publishes rules before it has content — that must not be a fault."""

    walk, index, asp = _verdicts(_constraint("is_text(profile.home)"), Snapshot({}, {}))
    assert walk == index == asp == []


def test_an_exhausted_grounding_budget_reports_unproven_not_a_pass() -> None:
    snapshot = _snapshot(
        *(Entity(id=f"npc:{index}", type=NodeType.NPC) for index in range(20))
    )
    findings = ClingoPresenceReference(
        _constraint("has(faction)"), grounding_budget_atoms=1
    ).check(snapshot)
    assert [finding.status for finding in findings] == ["unproven"]
    assert "grounding_budget_exceeded" in findings[0].evidence["reason"]
