"""Denominator-preserving scoring for structured narrative Agent hints."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from gameforge.bench.narrative.contracts import (
    NARRATIVE_CLASSES,
    NarrativeCase,
    TargetSpan,
)
from gameforge.bench.narrative.evidence import (
    NarrativeCaseOutcome,
    NarrativeClassMetric,
    NarrativeFpMetric,
    NarrativeScore,
    OutcomeStatus,
)
from gameforge.contracts.agent_io import ConsistencyHint
from gameforge.spine.stats import wilson_ci

_SENTENCE_BOUNDARIES = frozenset(".?!。！？\n")


def _sentence_offsets(dialogue: str, quote_start: int, quote_end: int) -> tuple[int, int]:
    left = -1
    for index in range(quote_start - 1, -1, -1):
        if dialogue[index] in _SENTENCE_BOUNDARIES:
            left = index
            break
    start = left + 1
    while start < len(dialogue) and dialogue[start].isspace():
        start += 1

    end = len(dialogue)
    for index in range(max(start, quote_end - 1), len(dialogue)):
        character = dialogue[index]
        if character in _SENTENCE_BOUNDARIES:
            end = index if character == "\n" else index + 1
            break
    while end > start and dialogue[end - 1].isspace():
        end -= 1
    return start, end


def span_overlaps(dialogue: str, hint_span: str, target_span: TargetSpan | None) -> bool:
    """Match one exact source occurrence and apply half-open interval overlap."""

    if target_span is None or not hint_span:
        return False
    start = dialogue.find(hint_span)
    if start < 0 or dialogue.find(hint_span, start + 1) >= 0:
        return False
    end = start + len(hint_span)
    if (start, end) != _sentence_offsets(dialogue, start, end):
        return False
    return max(start, target_span.start) < min(end, target_span.end)


def _matches_positive(case: NarrativeCase, hint: ConsistencyHint) -> bool:
    return bool(
        case.defect_class is not None
        and hint.defect_class == case.defect_class.value
        and set(hint.entity_ids) == set(case.target_entities)
        and span_overlaps(case.dialogue, hint.span, case.target_span)
    )


def score_case(
    case: NarrativeCase,
    hints: Sequence[ConsistencyHint],
    *,
    protocol_sha256: str,
    status: OutcomeStatus = "evaluated",
    request_hashes: Sequence[str] = (),
    parse_failures: int = 0,
    invalid_hint_items: int = 0,
    failure_reason: str | None = None,
) -> NarrativeCaseOutcome:
    """Score one case without consulting free-text rationale."""

    validated_hints = tuple(ConsistencyHint.model_validate(item) for item in hints)
    terminal = status in {"fallback", "cassette_miss", "runner_error"}
    if terminal and validated_hints:
        raise ValueError("terminal execution outcomes cannot contain hints")

    matched = ()
    constraint_matches = ()
    detected = False
    false_positive = False
    if not terminal:
        matched = tuple(
            index
            for index, hint in enumerate(validated_hints)
            if _matches_positive(case, hint)
        )
        if not case.is_clean:
            constraint_matches = tuple(
                index
                for index, hint in enumerate(validated_hints)
                if set(hint.constraint_ids) == set(case.target_constraint_ids)
            )
        detected = bool(matched) if not case.is_clean else False
        false_positive = bool(validated_hints) if case.is_clean else False

    return NarrativeCaseOutcome.seal(
        case_id=case.case_id,
        case_sha256=case.case_sha256,
        protocol_sha256=protocol_sha256,
        status=status,
        request_hashes=tuple(request_hashes),
        parse_failures=parse_failures,
        invalid_hint_items=invalid_hint_items,
        hints=validated_hints,
        detected=detected,
        false_positive=false_positive,
        matched_hint_indexes=matched,
        constraint_match_indexes=constraint_matches,
        failure_reason=failure_reason,
    )


def _validate_bindings(
    outcomes: Sequence[NarrativeCaseOutcome],
    cases: Sequence[NarrativeCase],
) -> tuple[dict[str, NarrativeCaseOutcome], tuple[NarrativeCase, ...]]:
    case_values = tuple(cases)
    outcome_values = tuple(outcomes)
    if not case_values:
        raise ValueError("narrative score requires a nonempty case denominator")
    case_ids = [case.case_id for case in case_values]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("narrative case denominator contains duplicate case IDs")
    outcome_ids = [outcome.case_id for outcome in outcome_values]
    if len(outcome_ids) != len(set(outcome_ids)):
        raise ValueError("narrative outcomes contain duplicate case IDs")
    if len(outcome_ids) != len(case_ids) or set(outcome_ids) != set(case_ids):
        raise ValueError("narrative outcome denominator does not match frozen cases")
    splits = {case.split for case in case_values}
    if len(splits) != 1:
        raise ValueError("narrative score cannot mix corpus splits")
    protocols = {outcome.protocol_sha256 for outcome in outcome_values}
    if len(protocols) != 1:
        raise ValueError("narrative score cannot mix protocol hashes")
    return {item.case_id: item for item in outcome_values}, case_values


def score_outcomes(
    outcomes: Sequence[NarrativeCaseOutcome],
    cases: Sequence[NarrativeCase],
) -> NarrativeScore:
    """Aggregate over every frozen case, including all execution failures."""

    outcomes_by_id, case_values = _validate_bindings(outcomes, cases)
    positive_n: Counter = Counter()
    positive_k: Counter = Counter()
    clean_n = 0
    clean_count = 0
    split = case_values[0].split

    for case in case_values:
        outcome = outcomes_by_id[case.case_id]
        if outcome.case_sha256 != case.case_sha256:
            raise ValueError(f"case_sha256 mismatch for {case.case_id}")
        rebuilt = score_case(
            case,
            outcome.hints,
            protocol_sha256=outcome.protocol_sha256,
            status=outcome.status,
            request_hashes=outcome.request_hashes,
            parse_failures=outcome.parse_failures,
            invalid_hint_items=outcome.invalid_hint_items,
            failure_reason=outcome.failure_reason,
        )
        if rebuilt != outcome:
            raise ValueError(f"stored outcome fields do not rescore for {case.case_id}")
        if case.is_clean:
            clean_n += 1
            clean_count += int(outcome.false_positive)
        else:
            assert case.defect_class is not None
            positive_n[case.defect_class] += 1
            positive_k[case.defect_class] += int(outcome.detected)

    by_class: list[NarrativeClassMetric] = []
    for defect_class in NARRATIVE_CLASSES:
        n = positive_n[defect_class]
        if not n:
            continue
        k = positive_k[defect_class]
        low, high = wilson_ci(k, n)
        by_class.append(
            NarrativeClassMetric(
                defect_class=defect_class,
                split=split,
                n=n,
                k=k,
                rate=k / n,
                ci_low=low,
                ci_high=high,
            )
        )

    fp_low, fp_high = wilson_ci(clean_count, clean_n)
    return NarrativeScore(
        by_class=tuple(by_class),
        clean_fp=NarrativeFpMetric(
            split=split,
            n=clean_n,
            count=clean_count,
            rate=clean_count / clean_n if clean_n else 0.0,
            ci_low=fp_low,
            ci_high=fp_high,
        ),
    )


__all__ = ["score_case", "score_outcomes", "span_overlaps"]
