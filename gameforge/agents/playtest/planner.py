"""Playtest Planner (M2b-1): abstracted game state → next high-level subgoal.

The LLM only PROPOSES a subgoal (quest/step_kind/need_item/target); it never
decides whether the subgoal is achieved — that verdict is always the
deterministic game engine's (AureusEnv). Parse failure degrades fail-closed to
a generic "advance" subgoal rather than crashing the playtest loop upstream.
"""
from __future__ import annotations

from typing import Any

from gameforge.agents.base import DEFAULT_SNAPSHOT, AgentParseError, call_model, parse_json_block
from gameforge.agents.playtest.prompts import register_playtest_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.runtime.model_router.router import ModelRouter

register_playtest_prompts()

Subgoal = dict[str, Any]

_SUBGOAL_KEYS = ("quest", "step_kind", "need_item", "target")

_FALLBACK_SUBGOAL: Subgoal = {"quest": None, "step_kind": "advance", "_fallback": True}


class Planner:
    node_id = "playtest.planner"

    def __init__(self, snapshot: ModelSnapshot = DEFAULT_SNAPSHOT) -> None:
        self.snapshot = snapshot

    def plan(
        self, state: str, router: ModelRouter, *, extra: str | None = None
    ) -> tuple[Subgoal, str]:
        version, system = get_prompt("playtest.planner")
        user = state
        if extra is not None:
            user = f"{user}\n\nCorrective hint: {extra}"

        h: str | None = None
        try:
            resp, h = call_model(
                router, self.node_id, user, version, system=system, snapshot=self.snapshot
            )
            raw = parse_json_block(resp.response_normalized)
        except AgentParseError:
            return dict(_FALLBACK_SUBGOAL), h or "no-call"

        if not isinstance(raw, dict):
            return dict(_FALLBACK_SUBGOAL), h or "no-call"

        subgoal: Subgoal = {k: raw[k] for k in _SUBGOAL_KEYS if k in raw}
        subgoal["_fallback"] = False
        return subgoal, h
