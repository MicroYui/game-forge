"""Deterministic identity for perspective-diverse narrative hints."""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from gameforge.contracts.agent_io import ConsistencyHint

MATCHER_VERSION = "narrative-span@1"
_SENTENCE_BOUNDARIES = frozenset(".?!。！？\n")


@dataclass(frozen=True, order=True)
class SourceSpan:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("source span must be a nonempty nonnegative range")


@dataclass(frozen=True, order=True)
class HintKey:
    defect_class: str
    entity_ids: tuple[str, ...]
    constraint_ids: tuple[str, ...]
    span: SourceSpan


@dataclass(frozen=True)
class NormalizedHint:
    key: HintKey
    hint: ConsistencyHint


def _clusters(text: str) -> Iterable[tuple[str, int, int]]:
    """Yield base-plus-combining clusters with original source bounds."""

    start = 0
    for index in range(1, len(text)):
        if not unicodedata.combining(text[index]):
            yield text[start:index], start, index
            start = index
    if text:
        yield text[start:], start, len(text)


def _fold_with_ranges(text: str) -> tuple[str, list[tuple[int, int]]]:
    folded: list[str] = []
    ranges: list[tuple[int, int]] = []
    for cluster, start, end in _clusters(text):
        for character in unicodedata.normalize("NFKC", cluster).casefold():
            if character.isspace():
                if folded and folded[-1] != " ":
                    folded.append(" ")
                    ranges.append((start, end))
                continue
            folded.append(character)
            ranges.append((start, end))
    return "".join(folded), ranges


def _fold(text: str) -> str:
    return _fold_with_ranges(text)[0].strip()


def _unique_occurrence(text: str, needle: str) -> int | None:
    positions: list[int] = []
    offset = 0
    while True:
        position = text.find(needle, offset)
        if position < 0:
            break
        positions.append(position)
        if len(positions) > 1:
            return None
        offset = position + 1
    return positions[0] if positions else None


def _sentence_span(dialogue: str, quote_start: int, quote_end: int) -> SourceSpan | None:
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
    if end <= start:
        return None
    return SourceSpan(start, end)


def normalize_hint(
    dialogue: str,
    hint: ConsistencyHint,
    allowed_entity_ids: Collection[str],
    allowed_constraint_ids: Collection[str],
) -> NormalizedHint | None:
    """Ground a model hint in the supplied IDs and one unambiguous source sentence."""

    if not set(hint.entity_ids) <= set(allowed_entity_ids):
        return None
    if not set(hint.constraint_ids) <= set(allowed_constraint_ids):
        return None

    folded_dialogue, source_ranges = _fold_with_ranges(dialogue)
    folded_quote = _fold(hint.span)
    if not folded_quote:
        return None
    occurrence = _unique_occurrence(folded_dialogue, folded_quote)
    if occurrence is None:
        return None
    final_index = occurrence + len(folded_quote) - 1
    if final_index >= len(source_ranges):
        return None
    quote_start = source_ranges[occurrence][0]
    quote_end = source_ranges[final_index][1]
    source_span = _sentence_span(dialogue, quote_start, quote_end)
    if source_span is None:
        return None

    entity_ids = tuple(sorted(set(hint.entity_ids)))
    constraint_ids = tuple(sorted(set(hint.constraint_ids)))
    normalized_hint = ConsistencyHint(
        defect_class=hint.defect_class,
        entity_ids=list(entity_ids),
        constraint_ids=list(constraint_ids),
        span=dialogue[source_span.start : source_span.end],
        rationale=hint.rationale,
        is_suggestion=True,
    )
    return NormalizedHint(
        key=HintKey(
            defect_class=hint.defect_class,
            entity_ids=entity_ids,
            constraint_ids=constraint_ids,
            span=source_span,
        ),
        hint=normalized_hint,
    )


def tally_normalized_hints(
    samples: Iterable[Iterable[NormalizedHint]],
) -> tuple[Counter[HintKey], list[NormalizedHint]]:
    """Count each grounded identity at most once per perspective."""

    counts: Counter[HintKey] = Counter()
    first_seen: list[NormalizedHint] = []
    known: set[HintKey] = set()
    for sample in samples:
        sample_seen: set[HintKey] = set()
        for item in sample:
            if item.key in sample_seen:
                continue
            sample_seen.add(item.key)
            counts[item.key] += 1
            if item.key not in known:
                known.add(item.key)
                first_seen.append(item)
    return counts, first_seen


__all__ = [
    "MATCHER_VERSION",
    "HintKey",
    "NormalizedHint",
    "SourceSpan",
    "normalize_hint",
    "tally_normalized_hints",
]
