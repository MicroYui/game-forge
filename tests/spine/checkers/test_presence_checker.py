"""Two implementations, one verdict — and a property test whose job is to break that.

The pair exists because a rule may only be published once two distinct exact
engines have positively decided it. Two copies of one algorithm would satisfy the
letter of that and none of its purpose: they agree about their shared bugs. So
the interesting test here is not that either is right on a fixture, it is that
they cannot be made to disagree on generated attribute shapes.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from gameforge.contracts.dsl import Constraint, Selector
from gameforge.contracts.ir import Entity, NodeType
from gameforge.spine.checkers.presence import (
    MISSING_REQUIRED_ATTRIBUTE,
    AttributePresenceChecker,
    EagerPathIndexPresenceReference,
)
from gameforge.spine.ir.snapshot import Snapshot


def _constraint(assert_expr: str, *, where: dict[str, Any] | None = None) -> Constraint:
    return Constraint.model_validate(
        {
            "id": "C-npc-faction",
            "kind": "structural",
            "oracle": "deterministic",
            "forall": Selector(var="n", node_type="NPC", where=where or {}).model_dump(),
            "assert": assert_expr,
            "severity": "critical",
        }
    )


def _snapshot(*npcs: tuple[str, dict[str, Any]]) -> Snapshot:
    return Snapshot.from_entities_relations(
        [Entity(id=name, type=NodeType.NPC, attrs=attrs) for name, attrs in npcs], []
    )


def test_the_rule_a_planner_wanted_finds_the_npc_that_breaks_it() -> None:
    snapshot = _snapshot(
        ("npc:zhongli", {"faction": "liyue"}),
        ("npc:nameless", {}),
    )

    findings = AttributePresenceChecker(_constraint("is_text(faction)")).check(snapshot)

    assert [finding.entities[0] for finding in findings] == ["npc:nameless"]
    assert findings[0].defect_class == MISSING_REQUIRED_ATTRIBUTE
    assert findings[0].status == "confirmed"
    assert findings[0].constraint_id == "C-npc-faction"


def test_a_present_but_wrongly_typed_attribute_is_not_compliance() -> None:
    """The distinction SMT could not draw: absent and wrong-typed both raised
    there, and both looked identical."""

    snapshot = _snapshot(("npc:odd", {"faction": {"name": "liyue"}}))

    findings = AttributePresenceChecker(_constraint("is_text(faction)")).check(snapshot)

    assert len(findings) == 1
    assert findings[0].evidence["observed"] == "object"
    assert "not text" in findings[0].message


def test_a_boolean_is_not_text() -> None:
    """`bool` is a subclass of `int` and reads as truthy; neither makes it a name."""

    findings = AttributePresenceChecker(_constraint("is_text(faction)")).check(
        _snapshot(("npc:flag", {"faction": True}))
    )

    assert len(findings) == 1


def test_presence_alone_accepts_any_type() -> None:
    findings = AttributePresenceChecker(_constraint("has(faction)")).check(
        _snapshot(("npc:a", {"faction": 7}), ("npc:b", {"faction": {"x": 1}}))
    )

    assert findings == []


def test_a_nested_requirement_reads_through_the_path() -> None:
    checker = AttributePresenceChecker(
        _constraint("is_object(schedule) and is_text(schedule.timezone)")
    )

    findings = checker.check(
        _snapshot(
            ("npc:ok", {"schedule": {"timezone": "Asia/Shanghai"}}),
            ("npc:shallow", {"schedule": {}}),
        )
    )

    assert [finding.evidence["path"] for finding in findings] == ["schedule.timezone"]


def test_a_selector_narrows_who_the_rule_applies_to() -> None:
    findings = AttributePresenceChecker(
        _constraint("is_text(faction)", where={"tier": "major"})
    ).check(_snapshot(("npc:major", {"tier": "major"}), ("npc:minor", {"tier": "minor"})))

    assert [finding.entities[0] for finding in findings] == ["npc:major"]


def test_no_entities_of_that_type_is_compliance_not_a_verdict_of_ignorance() -> None:
    """A game with no NPCs yet does not violate "every NPC needs a faction".

    This is forall's standard reading and matches dead_quest / isolated_node, but
    it does mean "nothing written yet" reads as "compliant".
    """

    assert AttributePresenceChecker(_constraint("is_text(faction)")).check(_snapshot()) == []


def test_a_rule_nothing_could_satisfy_is_undecidable_not_a_violation() -> None:
    findings = AttributePresenceChecker(
        _constraint("is_text(faction) and is_object(faction)")
    ).check(_snapshot(("npc:a", {"faction": "liyue"})))

    assert len(findings) == 1
    assert findings[0].status == "unproven"
    assert findings[0].entities == []


def test_findings_are_ordered_by_entity_so_the_verdict_is_stable() -> None:
    forward = AttributePresenceChecker(_constraint("has(faction)")).check(
        _snapshot(("npc:b", {}), ("npc:a", {}), ("npc:c", {}))
    )
    reverse = AttributePresenceChecker(_constraint("has(faction)")).check(
        _snapshot(("npc:c", {}), ("npc:a", {}), ("npc:b", {}))
    )

    assert [f.entities[0] for f in forward] == ["npc:a", "npc:b", "npc:c"]
    assert [f.message for f in forward] == [f.message for f in reverse]


_ATTR_VALUES = st.recursive(
    st.one_of(
        st.text(max_size=4),
        st.integers(min_value=-5, max_value=5),
        st.booleans(),
        st.none(),
    ),
    lambda children: st.dictionaries(
        st.sampled_from(["faction", "schedule", "timezone", "x"]), children, max_size=3
    ),
    max_leaves=6,
)


@settings(max_examples=250, deadline=None)
@given(
    attrs=st.dictionaries(
        st.sampled_from(["faction", "schedule", "tier"]), _ATTR_VALUES, max_size=3
    ),
    assert_expr=st.sampled_from(
        [
            "has(faction)",
            "is_text(faction)",
            "is_object(schedule)",
            "is_text(schedule.timezone)",
            "is_object(schedule) and is_text(schedule.timezone)",
        ]
    ),
)
def test_the_two_engines_cannot_be_made_to_disagree(attrs, assert_expr) -> None:
    """This is what makes the pair a differential rather than a formality.

    They resolve paths by opposite strategies — walk-and-stop versus
    index-everything — so any disagreement is real drift in path resolution, not
    a shared blind spot.
    """

    snapshot = _snapshot(("npc:subject", attrs))
    constraint = _constraint(assert_expr)

    production = AttributePresenceChecker(constraint).check(snapshot)
    reference = EagerPathIndexPresenceReference(constraint).check(snapshot)

    assert [(f.entities, f.evidence, f.status) for f in production] == [
        (f.entities, f.evidence, f.status) for f in reference
    ]
