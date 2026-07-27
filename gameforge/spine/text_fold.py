"""Fold text to a comparable form while keeping a map back to the original.

Two callers need the same fold for different reasons, so it lives here rather
than twice:

* ``agents.consistency.normalization`` grounds a model's claim in one exact
  sentence of a source document, which needs the offsets back.
* ``spine.ir.grounding`` matches an entity's surface forms against planning
  material, which needs only the folded string.

The fold is NFKC + casefold + whitespace collapse, applied per base-plus-
combining cluster so a decomposed character folds as one unit and still reports
the bounds of the cluster it came from.  It is deliberately NOT
``canonical_identity_token``: that one is for identifiers and replaces every
separator with ``_``, which no prose ever contains.
"""

from __future__ import annotations

import unicodedata
from typing import Iterable


def clusters(text: str) -> Iterable[tuple[str, int, int]]:
    """Yield base-plus-combining clusters with original source bounds."""

    start = 0
    for index in range(1, len(text)):
        if not unicodedata.combining(text[index]):
            yield text[start:index], start, index
            start = index
    if text:
        yield text[start:], start, len(text)


def fold_with_ranges(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Return the folded text and, per folded character, its source bounds."""

    folded: list[str] = []
    ranges: list[tuple[int, int]] = []
    for cluster, start, end in clusters(text):
        for character in unicodedata.normalize("NFKC", cluster).casefold():
            if character.isspace():
                if folded and folded[-1] != " ":
                    folded.append(" ")
                    ranges.append((start, end))
                continue
            folded.append(character)
            ranges.append((start, end))
    return "".join(folded), ranges


def fold_for_match(text: str) -> str:
    """Return the folded text alone, stripped — the form two strings compare in."""

    return fold_with_ranges(text)[0].strip()


__all__ = ["clusters", "fold_for_match", "fold_with_ranges"]
