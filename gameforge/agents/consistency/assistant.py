"""Consistency Assistant (PRD §7.5): dialogue/narrative -> llm-assisted hints.

The LLM only SUGGESTS suspected narrative inconsistencies/spoilers; it is never
authoritative — every hint is `is_suggestion=True` and downstream is confirmed
by a human or dropped. The deterministic oracle is a perspective-diverse
debate (M2b-2 Part C): instead of 3 identically-prompted samples, the model is
queried once per PERSPECTIVE — distinct lenses over the SAME dialogue
(`temporal` = timeline/ordering contradictions, `identity` = who-knows/who-is
contradictions, `spoiler` = premature reveals) — each under its own
`prompt_version` variant `f"{base_version}#p_{name}"`, so each perspective is
its own request_hash/cassette entry.

A hint (`span`, `issue`) pair reported by `>= threshold` perspectives passes
directly. A DISPUTED hint (reported by `1 <= count < threshold` perspectives)
triggers ONE rebuttal round (when `rebut=True`): every perspective is
re-queried under `f"{base_version}#r_{name}"`, shown the disputed hints, and
asked to confirm/refute them from its own lens; confirmations are re-tallied
and a disputed hint survives only if confirmations reach `threshold`. The
tally is fully deterministic (fixed perspective order, dedupe within each
sample). Per-sample parse failures are treated as empty samples rather than
raised; only if EVERY perspective in the first round fails to parse does the
run fall back to empty hints (`fallback_taken=True`).

Default `threshold=2` over the default 3 perspectives reproduces the old
majority-of-3 quorum's pass bar, so `ConsistencyChecker` (which calls `run`
with no extra kwargs) is unaffected.
"""
from __future__ import annotations

import json
from collections import Counter

from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.contracts.agent_io import (
    AgentNodeResult,
    ConsistencyHint,
    DialogueNarrativeInput,
)
from gameforge.runtime.model_router.router import ModelRouter

register_all_prompts()

_DEFAULT_PERSPECTIVES: tuple[str, ...] = ("temporal", "identity", "spoiler")
_DEFAULT_THRESHOLD = 2  # majority of 3, preserved as the backward-compatible default

HintPair = tuple[str, str]


def _parse_pairs(response_text: str) -> tuple[list[HintPair], bool]:
    """Parses a model response into (span, issue) pairs. Returns (pairs, ok);
    ok=False means the response didn't parse and must count as a parse failure,
    never a crash upstream."""
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


def _tally(samples: list[list[HintPair]]) -> tuple[Counter[HintPair], list[HintPair]]:
    """Counts each distinct pair at most once per sample — quorum is over
    "reported in a sample", not raw occurrence count within one sample.
    Deterministic: samples are consumed in the (fixed) order given, first-seen
    order is preserved."""
    counts: Counter[HintPair] = Counter()
    first_seen_order: list[HintPair] = []
    for pairs in samples:
        for pair in dict.fromkeys(pairs):  # dedupe within-sample, keep order
            if counts[pair] == 0:
                first_seen_order.append(pair)
            counts[pair] += 1
    return counts, first_seen_order


class ConsistencyAssistant:
    node_id = "consistency"

    def run(
        self,
        input: object,
        router: ModelRouter,
        *,
        perspectives: tuple[str, ...] = _DEFAULT_PERSPECTIVES,
        threshold: int = _DEFAULT_THRESHOLD,
        rebut: bool = True,
    ) -> AgentNodeResult:
        dn_input = (
            input
            if isinstance(input, DialogueNarrativeInput)
            else DialogueNarrativeInput(**input)  # type: ignore[arg-type]
        )
        base_version, _ = get_prompt("consistency.system")
        constraints = ", ".join(dn_input.narrative_constraint_ids) or "(none)"
        user = f"Narrative constraints: {constraints}\n\nDialogue:\n{dn_input.dialogue}"

        request_hashes: list[str] = []
        samples: list[list[HintPair]] = []
        parse_failures = 0

        for name in perspectives:
            variant_version = f"{base_version}#p_{name}"
            _, lens_system = get_prompt(f"consistency.perspective.{name}")
            resp, h = call_model(router, self.node_id, user, variant_version, system=lens_system)
            request_hashes.append(h)
            pairs, ok = _parse_pairs(resp.response_normalized)
            if not ok:
                parse_failures += 1
            samples.append(pairs)

        counts, first_seen_order = _tally(samples)

        survived: set[HintPair] = {pair for pair in first_seen_order if counts[pair] >= threshold}
        disputed = [pair for pair in first_seen_order if 1 <= counts[pair] < threshold]

        if disputed and rebut:
            confirmations = self._rebuttal_round(
                disputed, dn_input, base_version, router, perspectives, request_hashes
            )
            for pair in disputed:
                if confirmations[pair] >= threshold:
                    survived.add(pair)

        kept = [
            ConsistencyHint(span=span, issue=issue)
            for span, issue in first_seen_order
            if (span, issue) in survived
        ]

        fallback_taken = parse_failures == len(perspectives)

        return AgentNodeResult(
            role="consistency",
            model_run_id=request_hashes[0] if request_hashes else "no-call",
            request_hashes=request_hashes,
            fallback_taken=fallback_taken,
            produced={"hints": [h.model_dump() for h in kept], "samples": len(perspectives)},
        )

    def _rebuttal_round(
        self,
        disputed: list[HintPair],
        dn_input: DialogueNarrativeInput,
        base_version: str,
        router: ModelRouter,
        perspectives: tuple[str, ...],
        request_hashes: list[str],
    ) -> Counter[HintPair]:
        """One rebuttal round: every perspective is re-queried, shown the
        disputed hints, and asked which it confirms. Returns the re-tallied
        confirmation counts (dedupe within each perspective's response, fixed
        perspective order — deterministic). A perspective "confirming" a hint
        that was never in the disputed list is ignored — the rebuttal round
        can only affirm/deny hints already on the table, never introduce new
        ones outside the tally."""
        constraints = ", ".join(dn_input.narrative_constraint_ids) or "(none)"
        disputed_json = json.dumps([{"span": span, "issue": issue} for span, issue in disputed])
        user = (
            f"Narrative constraints: {constraints}\n\nDialogue:\n{dn_input.dialogue}\n\n"
            "Disputed hints from the first round (each was reported by fewer than "
            f"the required quorum of independent perspectives):\n{disputed_json}\n\n"
            "For EACH disputed hint, decide from your assigned perspective whether "
            "you CONFIRM it is a genuine issue."
        )
        disputed_set = set(disputed)
        confirm_samples: list[list[HintPair]] = []
        for name in perspectives:
            variant_version = f"{base_version}#r_{name}"
            _, lens_system = get_prompt(f"consistency.rebuttal.{name}")
            resp, h = call_model(router, self.node_id, user, variant_version, system=lens_system)
            request_hashes.append(h)
            pairs, _ok = _parse_pairs(resp.response_normalized)
            confirm_samples.append([p for p in pairs if p in disputed_set])

        counts, _ = _tally(confirm_samples)
        return counts
