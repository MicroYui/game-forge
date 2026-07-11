"""Playtest self-correction (M2b-1): a stuck trace → one corrective hint.

When the deterministic game state has not advanced for several steps, the loop
asks the Reflector (LLM, `playtest.reflect`) for ONE short natural-language hint
to try something different, which is then injected into the next Planner call.

The hint is advisory ONLY — it never decides that a quest is stuck or a target
unreachable (that verdict is always the deterministic engine + reachability
oracle's). Parse failure degrades fail-closed to an empty hint rather than
crashing the playtest loop upstream.
"""
from __future__ import annotations

import json

from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.agents.playtest.prompts import register_playtest_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.runtime.model_router.router import ModelRouter

register_playtest_prompts()

_NODE_ID = "playtest.reflect"


def _render_trace(trace: list[dict]) -> str:
    """Compact, deterministic rendering of the recent stuck steps."""
    lines = ["Recent steps that made no forward progress:"]
    for i, step in enumerate(trace):
        action = json.dumps(step.get("action", {}), sort_keys=True)
        lines.append(
            f"  [{i}] tick={step.get('tick')} action={action} "
            f"result={step.get('last_action_result')!r}"
        )
    return "\n".join(lines)


def reflect(
    trace: list[dict], router: ModelRouter, *, snapshot: ModelSnapshot | None = None
) -> tuple[str, str]:
    """Ask the Reflector for one corrective hint given a stuck `trace`.

    Returns `(hint, request_hash)`. On any parse failure the hint is `""` and the
    hash is the real request hash (or `"no-call"` if the call itself never
    produced one).
    """
    version, system = get_prompt("playtest.reflect")
    user = _render_trace(trace)

    h: str | None = None
    try:
        resp, h = call_model(router, _NODE_ID, user, version, system=system, snapshot=snapshot)
        raw = parse_json_block(resp.response_normalized)
    except AgentParseError:
        return "", h or "no-call"

    if not isinstance(raw, dict):
        return "", h or "no-call"
    hint = raw.get("hint")
    if not isinstance(hint, str):
        return "", h or "no-call"
    return hint, h
