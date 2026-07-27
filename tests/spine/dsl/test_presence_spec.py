"""What counts as "this attribute must be here" — and what deliberately does not.

The recogniser sits in front of the existing structural routing, so its refusals
matter as much as its acceptances: anything it declines must fall through
unchanged rather than be half-understood.
"""

from __future__ import annotations

from gameforge.contracts.dsl import Constraint, Selector
from gameforge.spine.dsl.presence import (
    PresenceAtom,
    parse_presence_spec,
    presence_conflicts,
)


def _constraint(
    assert_expr: str,
    *,
    kind: str = "structural",
    oracle: str = "deterministic",
    selector: Selector | None = Selector(var="n", node_type="NPC"),
    scope: Selector | None = None,
    predicates: list | None = None,
) -> Constraint:
    return Constraint.model_validate(
        {
            "id": "C-presence",
            "kind": kind,
            "oracle": oracle,
            "forall": selector.model_dump() if selector else None,
            "scope": scope.model_dump() if scope else None,
            "assert": assert_expr,
            "severity": "major",
            "predicates": predicates or [],
        }
    )


def test_a_planner_can_say_every_npc_needs_a_faction() -> None:
    spec = parse_presence_spec(_constraint("is_text(faction)"))

    assert spec is not None
    assert spec.selector.node_type == "NPC"
    assert spec.atoms == (PresenceAtom(path="faction", kind="text"),)


def test_presence_alone_does_not_demand_a_type() -> None:
    spec = parse_presence_spec(_constraint("has(reward)"))

    assert spec is not None
    assert spec.atoms == (PresenceAtom(path="reward", kind="present"),)


def test_a_conjunction_over_a_nested_path_is_one_spec() -> None:
    spec = parse_presence_spec(_constraint("is_object(schedule) and is_text(schedule.timezone)"))

    assert spec is not None
    assert spec.atoms == (
        PresenceAtom(path="schedule", kind="object"),
        PresenceAtom(path="schedule.timezone", kind="text"),
    )


def test_atoms_are_ordered_and_deduplicated_so_the_spec_is_stable() -> None:
    """Two spellings of one requirement are one requirement, in a fixed order."""

    first = parse_presence_spec(_constraint("is_text(b) and has(a) and is_text(b)"))
    second = parse_presence_spec(_constraint("has(a) and is_text(b)"))

    assert first is not None and second is not None
    assert first.atoms == second.atoms


def test_an_or_is_declined_because_compliance_could_not_be_exhibited() -> None:
    """Publication requires a witness the constraint accepts.

    Building one for an arbitrary boolean formula is a satisfiability problem;
    for a conjunction it is "supply every atom". So the grammar stops at
    conjunction rather than pretending it can validate more.
    """

    assert parse_presence_spec(_constraint("has(a) or has(b)")) is None
    assert parse_presence_spec(_constraint("not has(a)")) is None


def test_the_existing_structural_vocabulary_is_untouched() -> None:
    """A cycle constraint must still fall through to the keyword routing."""

    assert parse_presence_spec(_constraint("acyclic(quest_steps)")) is None
    assert parse_presence_spec(_constraint("reward.gold <= 150")) is None


def test_a_constraint_without_a_selector_has_nothing_to_quantify_over() -> None:
    assert parse_presence_spec(_constraint("has(faction)", selector=None)) is None


def test_a_scope_selector_works_like_forall() -> None:
    spec = parse_presence_spec(
        _constraint("has(faction)", selector=None, scope=Selector(var="n", node_type="NPC"))
    )

    assert spec is not None and spec.selector.node_type == "NPC"


def test_a_numeric_constraint_is_not_a_presence_constraint() -> None:
    assert parse_presence_spec(_constraint("has(faction)", kind="numeric")) is None


def test_an_unparseable_assert_is_declined_rather_than_raised() -> None:
    """An assert this cannot read belongs to some other backend, not to an error."""

    assert parse_presence_spec(_constraint("faction ===")) is None


def test_a_requirement_that_contradicts_itself_is_reported() -> None:
    """A rule nothing can satisfy is a trap, not authority: it would reject every
    candidate forever while looking like an ordinary published constraint."""

    spec = parse_presence_spec(_constraint("is_text(faction) and is_object(faction)"))

    assert spec is not None
    reasons = presence_conflicts(spec)
    assert any("both text and an object" in reason for reason in reasons)


def test_requiring_text_and_nesting_under_it_is_reported() -> None:
    spec = parse_presence_spec(_constraint("is_text(schedule) and has(schedule.timezone)"))

    assert spec is not None
    assert any("requires it to nest" in reason for reason in presence_conflicts(spec))


def test_a_requirement_the_selector_already_fixes_is_reported() -> None:
    """`where` already pins the attribute, so the rule holds for everything it
    selects — it looks like a rule and decides nothing."""

    spec = parse_presence_spec(
        _constraint(
            "has(lifecycle)",
            selector=Selector(var="e", node_type="EVENT", where={"lifecycle": "limited"}),
        )
    )

    assert spec is not None
    assert any("already fixed by the selector" in reason for reason in presence_conflicts(spec))


def test_an_ordinary_requirement_has_no_conflicts() -> None:
    spec = parse_presence_spec(_constraint("is_text(faction)"))

    assert spec is not None and presence_conflicts(spec) == ()
