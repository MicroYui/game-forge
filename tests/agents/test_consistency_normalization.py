from __future__ import annotations

import pytest

from gameforge.agents.consistency.normalization import (
    HintKey,
    NormalizedHint,
    SourceSpan,
    normalize_hint,
    tally_normalized_hints,
)
from gameforge.contracts.agent_io import ConsistencyHint


_DIALOGUE = "Qi lowered her voice. The sealed envoy is Mara. The bells continued."
_ALLOWED_ENTITIES = {"npc:qi", "npc:other", "secret:mara"}
_ALLOWED_CONSTRAINTS = {"C-gate", "C-identity", "C-other"}


def _hint(**changes: object) -> ConsistencyHint:
    value: dict[str, object] = {
        "defect_class": "spoiler",
        "entity_ids": ["npc:qi", "secret:mara"],
        "constraint_ids": ["C-gate", "C-identity"],
        "span": "The sealed envoy is Mara.",
        "rationale": "The line names the sealed envoy before the reveal point.",
    }
    value.update(changes)
    return ConsistencyHint.model_validate(value)


def _normalize(**changes: object) -> NormalizedHint:
    result = normalize_hint(
        _DIALOGUE,
        _hint(**changes),
        _ALLOWED_ENTITIES,
        _ALLOWED_CONSTRAINTS,
    )
    assert result is not None
    return result


def test_same_grounded_hint_with_different_rationale_and_id_order_shares_one_key():
    first = _normalize(rationale="names the envoy")
    second = _normalize(
        entity_ids=["secret:mara", "npc:qi"],
        constraint_ids=["C-identity", "C-gate"],
        span="sealed envoy is Mara",
        rationale="reveals a gated identity",
    )

    assert first.key == second.key
    assert first.key.span == SourceSpan(start=22, end=47)
    assert first.key.entity_ids == ("npc:qi", "secret:mara")
    assert first.key.constraint_ids == ("C-gate", "C-identity")
    assert first.hint.rationale != second.hint.rationale
    assert first.hint.span == "The sealed envoy is Mara."
    assert second.hint.span == "The sealed envoy is Mara."


def test_class_entity_constraint_or_sentence_difference_prevents_false_quorum():
    values = [
        _normalize(),
        _normalize(defect_class="character_violation"),
        _normalize(entity_ids=["npc:other"]),
        _normalize(constraint_ids=["C-other"]),
        _normalize(span="The bells continued."),
    ]

    counts, first_seen = tally_normalized_hints([[item] for item in values])

    assert max(counts.values()) == 1
    assert first_seen == values


def test_quote_lookup_normalizes_nfkc_case_and_whitespace_but_keeps_source_text():
    dialogue = "The  Ｗarden\tkeeps the seal intact."
    normalized = normalize_hint(
        dialogue,
        _hint(
            entity_ids=["npc:qi"],
            constraint_ids=["C-gate"],
            span="the warden keeps THE seal intact",
        ),
        {"npc:qi"},
        {"C-gate"},
    )

    assert normalized is not None
    assert normalized.key.span == SourceSpan(0, len(dialogue))
    assert normalized.hint.span == dialogue


@pytest.mark.parametrize(
    "span",
    [
        "words absent from the dialogue",
        "The gate is closed.",
    ],
)
def test_absent_or_ambiguous_quote_is_rejected(span):
    dialogue = (
        _DIALOGUE
        if span.startswith("words")
        else "The gate is closed. Later, the gate is closed."
    )

    assert (
        normalize_hint(
            dialogue,
            _hint(span=span),
            _ALLOWED_ENTITIES,
            _ALLOWED_CONSTRAINTS,
        )
        is None
    )


@pytest.mark.parametrize(
    ("entities", "constraints"),
    [
        (["npc:invented"], ["C-gate"]),
        (["npc:qi"], ["C-invented"]),
    ],
)
def test_invented_grounding_ids_are_rejected(entities, constraints):
    assert (
        normalize_hint(
            _DIALOGUE,
            _hint(entity_ids=entities, constraint_ids=constraints),
            _ALLOWED_ENTITIES,
            _ALLOWED_CONSTRAINTS,
        )
        is None
    )


def test_sentence_expansion_honors_ascii_unicode_and_newline_boundaries():
    dialogue = "First claim! 第二句泄露了真相。\nFinal claim?"
    second = normalize_hint(
        dialogue,
        _hint(
            entity_ids=["npc:qi"],
            constraint_ids=["C-gate"],
            span="泄露了真相",
        ),
        {"npc:qi"},
        {"C-gate"},
    )
    final = normalize_hint(
        dialogue,
        _hint(
            entity_ids=["npc:qi"],
            constraint_ids=["C-gate"],
            span="Final claim",
        ),
        {"npc:qi"},
        {"C-gate"},
    )

    assert second is not None and second.hint.span == "第二句泄露了真相。"
    assert final is not None and final.hint.span == "Final claim?"


def test_tally_counts_one_vote_per_perspective_and_retains_first_rationale():
    first = _normalize(rationale="first rationale")
    duplicate = _normalize(span="sealed envoy is Mara", rationale="duplicate rationale")
    second_vote = _normalize(rationale="second perspective rationale")

    counts, first_seen = tally_normalized_hints(
        [[first, duplicate], [second_vote]]
    )

    assert counts[first.key] == 2
    assert first_seen == [first]
    assert first_seen[0].hint.rationale == "first rationale"


def test_source_span_rejects_empty_or_negative_ranges():
    with pytest.raises(ValueError):
        SourceSpan(-1, 2)
    with pytest.raises(ValueError):
        SourceSpan(2, 2)


def test_hint_key_is_orderable_for_deterministic_serialization():
    a = HintKey("spoiler", ("npc:a",), ("C-a",), SourceSpan(0, 2))
    b = HintKey("spoiler", ("npc:b",), ("C-b",), SourceSpan(3, 5))
    assert sorted([b, a]) == [a, b]
