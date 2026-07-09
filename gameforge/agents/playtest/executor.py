"""Playtest Executor (M2b-1): subgoal + abstracted game state → next atomic
env action.

The LLM only PROPOSES an atomic action; it never decides whether the action
succeeds — that verdict is always the deterministic game engine's (AureusEnv).
Parse or validation failure degrades fail-closed to a harmless `observe`
action rather than crashing the playtest loop upstream.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from gameforge.agents.base import DEFAULT_SNAPSHOT, AgentParseError, call_model, parse_json_block
from gameforge.agents.playtest.planner import Subgoal
from gameforge.agents.playtest.prompts import register_playtest_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.contracts.env_types import Action, parse_action
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.runtime.model_router.router import ModelRouter

register_playtest_prompts()

_FALLBACK_ACTION: Action = parse_action({"kind": "observe"})


class Executor:
    node_id = "playtest.executor"

    def __init__(self, snapshot: ModelSnapshot = DEFAULT_SNAPSHOT) -> None:
        self.snapshot = snapshot

    def act(
        self,
        subgoal: Subgoal,
        state: str,
        router: ModelRouter,
        *,
        recall: str | None = None,
    ) -> tuple[Action, str]:
        version, system = get_prompt("playtest.executor")
        user = f"Subgoal: {json.dumps(subgoal, sort_keys=True)}\n\nState:\n{state}"
        if recall:
            user = f"{user}\n\nRelevant past experience:\n{recall}"

        h: str | None = None
        try:
            resp, h = call_model(
                router, self.node_id, user, version, system=system, snapshot=self.snapshot
            )
            raw = parse_json_block(resp.response_normalized)
            action = parse_action(raw)
        except (AgentParseError, ValidationError):
            return _FALLBACK_ACTION, h or "no-call"

        return action, h
