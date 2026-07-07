"""Consistency Assistant (PRD §7.5): dialogue/narrative -> llm-assisted hints.

The LLM only SUGGESTS suspected narrative inconsistencies/spoilers; it is never
authoritative — every hint is `is_suggestion=True` and downstream is confirmed
by a human or dropped. The deterministic oracle here is quorum voting (P2-D5):
the model is sampled 3 times under 3 DISTINCT `prompt_version` variants derived
from the base `consistency.system` version (`f"{base}#s{i}"`), each variant
producing its own `request_hash` and therefore its own cassette entry. A hint
(`span`, `issue`) pair) survives only if it is reported by a majority (>= 2 of
3) of the samples — a single hallucinated sample can never surface a hint on
its own. Per-sample parse failures are treated as empty samples rather than
raised; only if EVERY sample fails to parse does the run fall back to empty
hints.
"""
from __future__ import annotations

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

_QUORUM_SAMPLES = 3
_QUORUM_THRESHOLD = 2  # majority of 3


class ConsistencyAssistant:
    node_id = "consistency"

    def run(self, input: object, router: ModelRouter) -> AgentNodeResult:
        dn_input = (
            input
            if isinstance(input, DialogueNarrativeInput)
            else DialogueNarrativeInput(**input)  # type: ignore[arg-type]
        )
        base_version, system = get_prompt("consistency.system")
        constraints = ", ".join(dn_input.narrative_constraint_ids) or "(none)"
        user = f"Narrative constraints: {constraints}\n\nDialogue:\n{dn_input.dialogue}"

        request_hashes: list[str] = []
        samples: list[list[tuple[str, str]]] = []
        parse_failures = 0
        for i in range(_QUORUM_SAMPLES):
            variant_version = f"{base_version}#s{i}"
            resp, h = call_model(router, self.node_id, user, variant_version, system=system)
            request_hashes.append(h)
            try:
                raw = parse_json_block(resp.response_normalized)
            except AgentParseError:
                parse_failures += 1
                samples.append([])
                continue
            pairs: list[tuple[str, str]] = [
                (str(item["span"]), str(item["issue"]))
                for item in (raw if isinstance(raw, list) else [])
                if isinstance(item, dict) and "span" in item and "issue" in item
            ]
            samples.append(pairs)

        # Count each distinct pair at most once per sample — quorum is over
        # "reported in a sample", not raw occurrence count within one sample.
        counts: Counter[tuple[str, str]] = Counter()
        first_seen_order: list[tuple[str, str]] = []
        for pairs in samples:
            for pair in dict.fromkeys(pairs):  # dedupe within-sample, keep order
                if counts[pair] == 0:
                    first_seen_order.append(pair)
                counts[pair] += 1

        kept = [
            ConsistencyHint(span=span, issue=issue)
            for span, issue in first_seen_order
            if counts[(span, issue)] >= _QUORUM_THRESHOLD
        ]

        fallback_taken = parse_failures == _QUORUM_SAMPLES

        return AgentNodeResult(
            role="consistency",
            model_run_id=request_hashes[0] if request_hashes else "no-call",
            request_hashes=request_hashes,
            fallback_taken=fallback_taken,
            produced={"hints": [h.model_dump() for h in kept], "samples": _QUORUM_SAMPLES},
        )
