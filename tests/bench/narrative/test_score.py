from __future__ import annotations

import pytest

from gameforge.bench.narrative.generator import generate_case
from gameforge.bench.narrative.score import score_case, score_outcomes, span_overlaps
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.agent_io import ConsistencyHint

_PROTOCOL_SHA = "a" * 64


def _positive():
    return generate_case(
        split="verification",
        defect_class=DefectClass.character_violation,
        is_clean=False,
        seed=31,
        case_id="score-positive",
    )


def _clean():
    return generate_case(
        split="verification",
        defect_class=DefectClass.character_violation,
        is_clean=True,
        seed=32,
        case_id="score-clean",
    )


def _hint(case, **changes) -> ConsistencyHint:
    assert case.target_span is not None
    values = {
        "defect_class": case.defect_class.value,
        "entity_ids": list(case.target_entities),
        "constraint_ids": list(case.target_constraint_ids),
        "span": case.target_span.text,
        "rationale": "The event conflicts with the supplied narrative rule.",
    }
    values.update(changes)
    return ConsistencyHint(**values)


def _score(case, hints, **changes):
    return score_case(
        case,
        hints,
        protocol_sha256=_PROTOCOL_SHA,
        **changes,
    )


def test_positive_tp_requires_class_exact_entity_set_and_span_overlap():
    case = _positive()

    assert _score(case, [_hint(case)]).detected is True
    assert _score(
        case,
        [_hint(case, defect_class="faction_violation")],
    ).detected is False
    assert _score(case, [_hint(case, entity_ids=["npc:other"])]).detected is False
    assert _score(case, [_hint(case, span="An unrelated sentence.")]).detected is False


def test_extra_wrong_hint_does_not_erase_a_correct_positive_match():
    case = _positive()
    outcome = _score(
        case,
        [
            _hint(case, entity_ids=["npc:other"]),
            _hint(case),
        ],
    )

    assert outcome.detected is True
    assert outcome.matched_hint_indexes == (1,)


def test_constraint_match_is_diagnostic_and_not_a_tp_condition():
    case = _positive()
    outcome = _score(case, [_hint(case, constraint_ids=["C-other"])])

    assert outcome.detected is True
    assert outcome.constraint_match_indexes == ()


@pytest.mark.parametrize("status", ["fallback", "cassette_miss", "runner_error"])
def test_execution_failures_remain_positive_misses(status):
    case = _positive()
    outcome = _score(case, [], status=status, failure_reason=f"forced {status}")
    score = score_outcomes([outcome], [case])

    assert score.by_class[0].n == 1
    assert score.by_class[0].k == 0


def test_partial_parse_failure_remains_evaluated_and_can_detect():
    case = _positive()
    outcome = _score(
        case,
        [_hint(case)],
        status="partial_parse_failure",
        parse_failures=1,
        invalid_hint_items=2,
    )

    assert outcome.detected is True
    assert score_outcomes([outcome], [case]).by_class[0].n == 1


def test_any_number_of_surviving_hints_on_clean_is_one_false_positive_case():
    case = _clean()
    first_constraint = case.constraints[0]
    first_sentence = case.dialogue.splitlines()[0].split(". ", maxsplit=1)[0] + "."
    hint = ConsistencyHint(
        defect_class="character_violation",
        entity_ids=[first_constraint.entity_ids[0]],
        constraint_ids=[first_constraint.constraint_id],
        span=first_sentence,
        rationale="A deliberately wrong but structured hint.",
    )
    outcome = _score(case, [hint, hint])
    score = score_outcomes([outcome], [case])

    assert outcome.false_positive is True
    assert score.clean_fp.n == 1
    assert score.clean_fp.count == 1


def test_clean_parse_failure_stays_in_fp_denominator_without_becoming_a_hint():
    case = _clean()
    outcome = _score(
        case,
        [],
        status="partial_parse_failure",
        parse_failures=1,
    )
    score = score_outcomes([outcome], [case])

    assert score.clean_fp.n == 1
    assert score.clean_fp.count == 0


def test_span_overlap_uses_exact_unambiguous_half_open_source_ranges():
    case = _positive()
    target = case.target_span
    assert target is not None

    assert span_overlaps(case.dialogue, target.text, target) is True
    assert span_overlaps(case.dialogue, "text that is absent", target) is False
    assert span_overlaps(case.dialogue, target.text[3:], target) is False
    duplicated = f"{target.text} {target.text}"
    assert span_overlaps(duplicated, target.text, target) is False


def test_score_outcomes_rejects_missing_duplicate_or_mismatched_case_bindings():
    positive = _positive()
    clean = _clean()
    positive_outcome = _score(positive, [])
    clean_outcome = _score(clean, [])

    with pytest.raises(ValueError, match="denominator"):
        score_outcomes([positive_outcome], [positive, clean])
    with pytest.raises(ValueError, match="duplicate"):
        score_outcomes([positive_outcome, positive_outcome], [positive, clean])
    with pytest.raises(ValueError, match="case_sha256"):
        score_outcomes(
            [
                positive_outcome.model_copy(
                    update={"case_sha256": "b" * 64},
                ),
                clean_outcome,
            ],
            [positive, clean],
        )
