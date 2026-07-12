"""Frozen M2 consistency flow for byte-identical Opus cassette replay."""

from __future__ import annotations

import json
from collections import Counter

from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.contracts.agent_io import (
    AgentNodeResult,
    DialogueNarrativeInput,
    M2_AGENT_IO_SCHEMA_VERSION,
)
from gameforge.runtime.model_router.router import ModelRouter

register_all_prompts()

_PERSPECTIVES: tuple[str, ...] = ("temporal", "identity", "spoiler")
_THRESHOLD = 2
LegacyHintPair = tuple[str, str]


def _parse_pairs(response_text: str) -> tuple[list[LegacyHintPair], bool]:
    try:
        raw = parse_json_block(response_text)
    except AgentParseError:
        return [], False
    pairs = [
        (str(item["span"]), str(item["issue"]))
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict) and "span" in item and "issue" in item
    ]
    return pairs, True


def _tally(
    samples: list[list[LegacyHintPair]],
) -> tuple[Counter[LegacyHintPair], list[LegacyHintPair]]:
    counts: Counter[LegacyHintPair] = Counter()
    first_seen_order: list[LegacyHintPair] = []
    for pairs in samples:
        for pair in dict.fromkeys(pairs):
            if counts[pair] == 0:
                first_seen_order.append(pair)
            counts[pair] += 1
    return counts, first_seen_order


def run_legacy_m2(
    input: object,
    router: ModelRouter,
    *,
    perspectives: tuple[str, ...] = _PERSPECTIVES,
    threshold: int = _THRESHOLD,
    rebut: bool = True,
) -> AgentNodeResult:
    dn_input = (
        input
        if isinstance(input, DialogueNarrativeInput)
        else DialogueNarrativeInput(**input)  # type: ignore[arg-type]
    )
    base_version, _ = get_prompt("consistency.legacy.system")
    constraints = ", ".join(dn_input.narrative_constraint_ids) or "(none)"
    user = f"Narrative constraints: {constraints}\n\nDialogue:\n{dn_input.dialogue}"
    request_hashes: list[str] = []
    samples: list[list[LegacyHintPair]] = []
    parse_failures = 0

    for name in perspectives:
        variant_version = f"{base_version}#p_{name}"
        _, lens_system = get_prompt(f"consistency.legacy.perspective.{name}")
        response, request = call_model(
            router,
            "consistency",
            user,
            variant_version,
            system=lens_system,
        )
        request_hashes.append(request)
        pairs, ok = _parse_pairs(response.response_normalized)
        if not ok:
            parse_failures += 1
        samples.append(pairs)

    counts, first_seen_order = _tally(samples)
    survived = {pair for pair in first_seen_order if counts[pair] >= threshold}
    disputed = [pair for pair in first_seen_order if 1 <= counts[pair] < threshold]
    if disputed and rebut:
        confirmations = _rebuttal_round(
            disputed,
            dn_input,
            base_version,
            router,
            perspectives,
            request_hashes,
        )
        for pair in disputed:
            if confirmations[pair] >= threshold:
                survived.add(pair)

    hints = [
        {"span": span, "issue": issue, "is_suggestion": True}
        for span, issue in first_seen_order
        if (span, issue) in survived
    ]
    return AgentNodeResult(
        agent_io_schema_version=M2_AGENT_IO_SCHEMA_VERSION,
        role="consistency",
        model_run_id=request_hashes[0] if request_hashes else "no-call",
        request_hashes=request_hashes,
        fallback_taken=parse_failures == len(perspectives),
        produced={"hints": hints, "samples": len(perspectives)},
    )


def _rebuttal_round(
    disputed: list[LegacyHintPair],
    dn_input: DialogueNarrativeInput,
    base_version: str,
    router: ModelRouter,
    perspectives: tuple[str, ...],
    request_hashes: list[str],
) -> Counter[LegacyHintPair]:
    constraints = ", ".join(dn_input.narrative_constraint_ids) or "(none)"
    disputed_json = json.dumps(
        [{"span": span, "issue": issue} for span, issue in disputed]
    )
    user = (
        f"Narrative constraints: {constraints}\n\nDialogue:\n{dn_input.dialogue}\n\n"
        "Disputed hints from the first round (each was reported by fewer than "
        f"the required quorum of independent perspectives):\n{disputed_json}\n\n"
        "For EACH disputed hint, decide from your assigned perspective whether "
        "you CONFIRM it is a genuine issue."
    )
    disputed_set = set(disputed)
    samples: list[list[LegacyHintPair]] = []
    for name in perspectives:
        variant_version = f"{base_version}#r_{name}"
        _, lens_system = get_prompt(f"consistency.legacy.rebuttal.{name}")
        response, request = call_model(
            router,
            "consistency",
            user,
            variant_version,
            system=lens_system,
        )
        request_hashes.append(request)
        pairs, _ = _parse_pairs(response.response_normalized)
        samples.append([pair for pair in pairs if pair in disputed_set])
    counts, _ = _tally(samples)
    return counts


__all__ = ["run_legacy_m2"]
