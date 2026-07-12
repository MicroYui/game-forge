from __future__ import annotations

import json
from pathlib import Path

import pytest

from gameforge.bench.external_cases.contracts import TargetLocator
from gameforge.bench.external_cases.endless_sky_fixture import load_case_specs
from gameforge.bench.external_cases.endless_sky_predicates import evaluate_predicate
from gameforge.spine.ingestion.endless_sky_reader import read_source_tree


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_cases/endless_sky"
PATH = "data/synthetic.txt"


BAD_REFERENCE = b'effect "Pulse"\n\tsound "missing impact"\n'
GOOD_REFERENCE = b'effect "Pulse"\n\tsound "known impact"\n'

SELF_REQUIRE = (
    b'mission "Looping Work"\n'
    b"\tto offer\n"
    b'\t\thas "Looping Work: offered"\n'
)
PRIOR_REQUIRE = (
    b'mission "Prior Work"\n'
    b'\tsource "Harbor"\n'
    b'mission "Looping Work"\n'
    b"\tto offer\n"
    b'\t\thas "Prior Work: offered"\n'
)

MISSING_ACCESS = (
    b'mission "Restricted Courier"\n'
    b'\tsource "Harbor"\n'
    b'\tdestination "Citadel"\n'
)
ACCESS_PROVED = MISSING_ACCESS + b"\tclearance\n"

MISSING_SOURCE = (
    b'mission "Hidden Dispatch"\n'
    b'\tdestination "Citadel"\n'
)
SOURCE_PRESENT = (
    b'mission "Hidden Dispatch"\n'
    b'\tsource "Harbor"\n'
    b'\tdestination "Citadel"\n'
)


def _target(*, kind: str = "mission", name: str) -> TargetLocator:
    return TargetLocator(path=PATH, record_kind=kind, record_name=name)


def _evaluate(
    raw: bytes,
    predicate_id: str,
    *,
    target: TargetLocator,
    context: dict[str, object] | None = None,
):
    return evaluate_predicate(
        predicate_id,
        read_source_tree({PATH: raw}),
        (target,),
        context or {},
    )


@pytest.mark.parametrize(
    ("predicate_id", "before", "after", "target", "context"),
    [
        (
            "reference_resolves",
            BAD_REFERENCE,
            GOOD_REFERENCE,
            _target(kind="effect", name="Pulse"),
            {"resources": [{"kind": "sound", "name": "known impact"}]},
        ),
        (
            "dependency_acyclic",
            SELF_REQUIRE,
            PRIOR_REQUIRE,
            _target(name="Looping Work"),
            {},
        ),
        (
            "target_reachable",
            MISSING_ACCESS,
            ACCESS_PROVED,
            _target(name="Restricted Courier"),
            {"restricted_destinations": ["Citadel"]},
        ),
        (
            "mission_offerable",
            MISSING_SOURCE,
            SOURCE_PRESENT,
            _target(name="Hidden Dispatch"),
            {},
        ),
    ],
)
def test_predicate_transitions_violation_to_clear(
    predicate_id: str,
    before: bytes,
    after: bytes,
    target: TargetLocator,
    context: dict[str, object],
) -> None:
    before_result = _evaluate(
        before,
        predicate_id,
        target=target,
        context=context,
    )
    after_result = _evaluate(
        after,
        predicate_id,
        target=target,
        context=context,
    )

    assert before_result.status == "violation"
    assert before_result.evidence["violations"][0]["path"] == PATH
    assert before_result.evidence["violations"][0]["line"] >= 1
    assert after_result.status == "clear"
    assert after_result.evidence["violations"] == []


def test_reference_predicate_rejects_only_ambiguous_implicit_choice_merges() -> None:
    ambiguous = (
        b'mission "Branching Exercise"\n'
        b"\ton offer\n"
        b"\t\tconversation\n"
        b"\t\t\tchoice\n"
        b"\t\t\t\tlong-answer-one\n"
        b"\t\t\t\tlong-answer-two\n"
        b"\t\t\t\tshort-answer\n"
        b"\t\t\t\t\tgoto shared\n"
        b"\t\t\tfirst-detail\n"
        b"\t\t\tsecond-detail\n"
        b"\t\t\tlabel shared\n"
        b"\t\t\tcontinuation\n"
    )
    explicit_merge = ambiguous.replace(
        b"\t\t\tsecond-detail\n",
        b"\t\t\tsecond-detail\n\t\t\t\tgoto merged\n",
    ) + b"\t\t\tlabel merged\n\t\t\tending\n"
    target = _target(name="Branching Exercise")

    before = _evaluate(ambiguous, "reference_resolves", target=target)
    after = _evaluate(explicit_merge, "reference_resolves", target=target)

    assert before.status == "violation"
    assert before.evidence["violations"][0]["reason"] == "ambiguous_choice_merge"
    assert after.status == "clear"


def test_single_implicit_choice_path_may_join_an_explicit_target() -> None:
    raw = (
        b'mission "Branching Exercise"\n'
        b"\ton offer\n"
        b"\t\tconversation\n"
        b"\t\t\tchoice\n"
        b"\t\t\t\tlong-answer\n"
        b"\t\t\t\tshort-answer\n"
        b"\t\t\t\t\tgoto shared\n"
        b"\t\t\tlong-detail\n"
        b"\t\t\tlabel shared\n"
        b"\t\t\tcontinuation\n"
    )

    result = _evaluate(
        raw,
        "reference_resolves",
        target=_target(name="Branching Exercise"),
    )

    assert result.status == "clear"


def test_conversation_cycle_is_cleared_only_by_matching_monotonic_guard() -> None:
    repeatable = (
        b'mission "Branching Exercise"\n'
        b"\ton offer\n"
        b"\t\tconversation\n"
        b"\t\t\tlabel alpha\n"
        b"\t\t\tchoice\n"
        b"\t\t\t\tvisit-beta\n"
        b"\t\t\tlabel beta\n"
        b"\t\t\taction\n"
        b'\t\t\t\tset "visited beta"\n'
        b"\t\t\tchoice\n"
        b"\t\t\t\treturn-alpha\n"
        b"\t\t\t\t\tgoto alpha\n"
    )
    guarded = repeatable.replace(
        b"\t\t\t\tvisit-beta\n",
        b"\t\t\t\tvisit-beta\n"
        b"\t\t\t\t\tto display\n"
        b'\t\t\t\t\t\tnot "visited beta"\n',
    )
    target = _target(name="Branching Exercise")

    before = _evaluate(repeatable, "dependency_acyclic", target=target)
    after = _evaluate(guarded, "dependency_acyclic", target=target)

    assert before.status == "violation"
    assert before.evidence["violations"][0]["reason"] == "dependency_cycle"
    assert after.status == "clear"


@pytest.mark.parametrize(
    "trigger",
    ["landing", "job", "assisting", "boarding", "entering", "spaceport"],
)
def test_registered_offer_triggers_make_a_mission_offerable(trigger: str) -> None:
    raw = f'mission "Triggered Work"\n\t{trigger}\n'.encode()

    result = _evaluate(
        raw,
        "mission_offerable",
        target=_target(name="Triggered Work"),
    )

    assert result.status == "clear"


def test_landing_access_condition_proves_restricted_destination_reachable() -> None:
    raw = (
        b'mission "Restricted Courier"\n'
        b"\tto offer\n"
        b'\t\thas "landing access: Citadel"\n'
        b'\tdestination "Citadel"\n'
    )

    result = _evaluate(
        raw,
        "target_reachable",
        target=_target(name="Restricted Courier"),
        context={"restricted_destinations": ["Citadel"]},
    )

    assert result.status == "clear"


def test_unknown_predicate_and_missing_target_fail_closed() -> None:
    target = _target(name="Hidden Dispatch")
    tree = read_source_tree({PATH: SOURCE_PRESENT})

    unknown = evaluate_predicate("not_registered", tree, (target,), {})
    missing = evaluate_predicate(
        "mission_offerable",
        tree,
        (_target(name="Absent Dispatch"),),
        {},
    )

    assert unknown.status == "unproven"
    assert unknown.evidence["unproven"][0]["reason"] == "unknown_predicate"
    assert missing.status == "unproven"
    assert missing.evidence["unproven"][0]["reason"] == "target_missing"


@pytest.mark.parametrize("side", ["before", "after"])
def test_all_frozen_case_predicates_have_expected_transition(side: str) -> None:
    registration = load_case_specs(CORPUS / "case-specs.json")
    expected = "violation" if side == "before" else "clear"

    for spec in registration.cases:
        case_root = CORPUS / "cases" / spec.case_id
        source_root = case_root / side
        tree = read_source_tree(
            {path: (source_root / path).read_bytes() for path in spec.changed_paths}
        )
        context = json.loads((case_root / "context.json").read_bytes())

        result = evaluate_predicate(
            spec.predicate_id,
            tree,
            spec.target_locators,
            context,
        )

        assert result.status == expected, (
            spec.case_id,
            side,
            result.model_dump(mode="json"),
        )
        assert result.target_locators == spec.target_locators
