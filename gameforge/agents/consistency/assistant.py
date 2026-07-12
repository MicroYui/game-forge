"""Bounded narrative consistency suggestions with grounded perspective quorum."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from pydantic import ValidationError

from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.agents.consistency.normalization import (
    MATCHER_VERSION,
    HintKey,
    NormalizedHint,
    normalize_hint,
    tally_normalized_hints,
)
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.contracts.agent_io import (
    AgentNodeResult,
    ConsistencyHint,
    DialogueNarrativeInput,
)
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.runtime.model_router.router import ModelRouter

register_all_prompts()

CURRENT_PERSPECTIVES: tuple[str, ...] = (
    "constraint_matching",
    "causal_world_state",
    "adversarial_falsification",
)
DEFAULT_THRESHOLD = 2
_LEGACY_PERSPECTIVES: tuple[str, ...] = ("temporal", "identity", "spoiler")


@dataclass(frozen=True)
class _ParsedHints:
    hints: tuple[ConsistencyHint, ...]
    parse_ok: bool
    raw_items: int


def _parse_hints(response_text: str) -> _ParsedHints:
    try:
        raw = parse_json_block(response_text)
    except AgentParseError:
        return _ParsedHints((), False, 0)
    if not isinstance(raw, list):
        return _ParsedHints((), False, 0)
    hints: list[ConsistencyHint] = []
    for item in raw:
        try:
            hints.append(ConsistencyHint.model_validate(item))
        except (TypeError, ValidationError):
            continue
    return _ParsedHints(tuple(hints), True, len(raw))


def _build_user(input: DialogueNarrativeInput) -> str:
    constraints = [
        {
            "constraint_id": item.constraint_id,
            "entity_ids": item.entity_ids,
            "statement": item.statement,
        }
        for item in input.narrative_constraints
    ]
    if not constraints:
        constraints = [
            {
                "constraint_id": constraint_id,
                "entity_ids": [],
                "statement": "Statement unavailable in legacy input.",
            }
            for constraint_id in input.narrative_constraint_ids
        ]
    return (
        "Narrative constraints (copy IDs exactly):\n"
        f"{json.dumps(constraints, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Dialogue (quote exact source text):\n{input.dialogue}"
    )


def _allowed_ids(input: DialogueNarrativeInput) -> tuple[set[str], set[str]]:
    entities = {
        entity_id
        for constraint in input.narrative_constraints
        for entity_id in constraint.entity_ids
    }
    constraints = {item.constraint_id for item in input.narrative_constraints}
    constraints.update(input.narrative_constraint_ids)
    return entities, constraints


def _validate_quorum(perspectives: tuple[str, ...], threshold: int) -> None:
    if not perspectives:
        raise ValueError("at least one consistency perspective is required")
    if len(perspectives) != len(set(perspectives)):
        raise ValueError("consistency perspectives must be unique")
    if not set(perspectives) <= set(CURRENT_PERSPECTIVES):
        raise ValueError("unknown consistency perspective")
    if threshold < 1 or threshold > len(perspectives):
        raise ValueError("threshold must be between one and the perspective count")


class ConsistencyAssistant:
    node_id = "consistency"

    def run_legacy_m2(
        self,
        input: object,
        router: ModelRouter,
        *,
        perspectives: tuple[str, ...] = _LEGACY_PERSPECTIVES,
        threshold: int = DEFAULT_THRESHOLD,
        rebut: bool = True,
    ) -> AgentNodeResult:
        from gameforge.agents.consistency.legacy import run_legacy_m2

        return run_legacy_m2(
            input,
            router,
            perspectives=perspectives,
            threshold=threshold,
            rebut=rebut,
        )

    def run(
        self,
        input: object,
        router: ModelRouter,
        *,
        perspectives: tuple[str, ...] = CURRENT_PERSPECTIVES,
        threshold: int = DEFAULT_THRESHOLD,
        rebut: bool = True,
        model_snapshot: ModelSnapshot | None = None,
    ) -> AgentNodeResult:
        _validate_quorum(perspectives, threshold)
        narrative_input = (
            input
            if isinstance(input, DialogueNarrativeInput)
            else DialogueNarrativeInput(**input)  # type: ignore[arg-type]
        )
        base_version, _ = get_prompt("consistency.system")
        user = _build_user(narrative_input)
        allowed_entities, allowed_constraints = _allowed_ids(narrative_input)
        request_hashes: list[str] = []
        samples: list[list[NormalizedHint]] = []
        diagnostics: list[dict[str, object]] = []
        parse_failures = 0

        for name in perspectives:
            response, request = call_model(
                router,
                self.node_id,
                user,
                f"{base_version}#p_{name}",
                system=get_prompt(f"consistency.perspective.{name}")[1],
                snapshot=model_snapshot,
            )
            request_hashes.append(request)
            parsed = _parse_hints(response.response_normalized)
            if not parsed.parse_ok:
                parse_failures += 1
            normalized = [
                grounded
                for hint in parsed.hints
                if (
                    grounded := normalize_hint(
                        narrative_input.dialogue,
                        hint,
                        allowed_entities,
                        allowed_constraints,
                    )
                )
                is not None
            ]
            samples.append(normalized)
            diagnostics.append(
                {
                    "name": name,
                    "request_hash": request,
                    "parse_ok": parsed.parse_ok,
                    "raw_items": parsed.raw_items,
                    "accepted_items": len(normalized),
                }
            )

        counts, first_seen = tally_normalized_hints(samples)
        survived = {item.key for item in first_seen if counts[item.key] >= threshold}
        disputed = [item for item in first_seen if 1 <= counts[item.key] < threshold]
        if rebut and disputed:
            confirmations = self._rebuttal_round(
                disputed,
                narrative_input,
                router,
                perspectives,
                base_version,
                model_snapshot,
                request_hashes,
            )
            for item in disputed:
                if confirmations[item.key] >= threshold:
                    survived.add(item.key)

        kept = [item.hint for item in first_seen if item.key in survived]
        return AgentNodeResult(
            role="consistency",
            model_run_id=request_hashes[0] if request_hashes else "no-call",
            request_hashes=request_hashes,
            fallback_taken=parse_failures == len(perspectives),
            produced={
                "hints": [item.model_dump() for item in kept],
                "perspectives": diagnostics,
                "samples": len(perspectives),
                "threshold": threshold,
                "matcher_version": MATCHER_VERSION,
                "rebuttal_enabled": rebut,
            },
        )

    def _rebuttal_round(
        self,
        disputed: list[NormalizedHint],
        narrative_input: DialogueNarrativeInput,
        router: ModelRouter,
        perspectives: tuple[str, ...],
        base_version: str,
        model_snapshot: ModelSnapshot | None,
        request_hashes: list[str],
    ) -> Counter[HintKey]:
        disputed_keys = {item.key for item in disputed}
        disputed_json = json.dumps(
            [item.hint.model_dump() for item in disputed],
            ensure_ascii=False,
            sort_keys=True,
        )
        user = (
            f"{_build_user(narrative_input)}\n\n"
            f"Disputed structured hints:\n{disputed_json}"
        )
        allowed_entities, allowed_constraints = _allowed_ids(narrative_input)
        samples: list[list[NormalizedHint]] = []
        for name in perspectives:
            response, request = call_model(
                router,
                self.node_id,
                user,
                f"{base_version}#r_{name}",
                system=get_prompt(f"consistency.rebuttal.{name}")[1],
                snapshot=model_snapshot,
            )
            request_hashes.append(request)
            parsed = _parse_hints(response.response_normalized)
            confirmed: list[NormalizedHint] = []
            for hint in parsed.hints:
                item = normalize_hint(
                    narrative_input.dialogue,
                    hint,
                    allowed_entities,
                    allowed_constraints,
                )
                if item is not None and item.key in disputed_keys:
                    confirmed.append(item)
            samples.append(confirmed)
        counts, _ = tally_normalized_hints(samples)
        return counts


__all__ = [
    "CURRENT_PERSPECTIVES",
    "DEFAULT_THRESHOLD",
    "ConsistencyAssistant",
]
