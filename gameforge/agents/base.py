"""Shared agent-node plumbing: deterministic JSON parsing + router call helper.

Agents reach the LLM ONLY through ModelRouter. Every model output is parsed
deterministically; parse failure is a fallback signal, never a crash upstream.
"""
from __future__ import annotations

import json

from gameforge.agents.prompts.registry import render  # noqa: F401  (re-exported for agents)
from gameforge.contracts.model_router import (
    Message, ModelRequest, ModelResponse, ModelSnapshot, request_hash,
)
from gameforge.runtime.model_router.router import ModelRouter

DEFAULT_SNAPSHOT = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="pre-m4@1",
)
M2_REPLAY_SNAPSHOT = ModelSnapshot(
    provider="anthropic",
    model="claude-opus-4-8",
    snapshot_tag="m2a@1",
)


class AgentParseError(Exception):
    pass


def resolve_model_snapshot(
    router: ModelRouter,
    snapshot: ModelSnapshot | None = None,
) -> ModelSnapshot:
    """Resolve explicit node policy, then session policy, then product default."""
    return snapshot or getattr(router, "default_model_snapshot", None) or DEFAULT_SNAPSHOT


def parse_json_block(text: str):
    t = text.strip()
    if "```" in t:
        # take the content of the first fenced block
        parts = t.split("```")
        if len(parts) >= 3:
            body = parts[1]
            if body.lstrip().lower().startswith("json"):
                body = body.lstrip()[4:]
            t = body.strip()
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        raise AgentParseError(f"no JSON object/array in model output: {text[:120]!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(t[min(starts):])
    except json.JSONDecodeError as exc:
        raise AgentParseError(str(exc)) from exc
    return obj


def call_model(
    router: ModelRouter,
    agent_node_id: str,
    user_prompt: str,
    prompt_version: str,
    *,
    system: str | None = None,
    params: dict | None = None,
    snapshot: ModelSnapshot | None = None,
) -> tuple[ModelResponse, str]:
    model_snapshot = resolve_model_snapshot(router, snapshot)
    if params is None:
        params = {"max_tokens": 2048}
        if model_snapshot != DEFAULT_SNAPSHOT:
            params["temperature"] = 0
    messages: list[Message] = []
    if system is not None:
        messages.append(Message(role="system", content=system))
    messages.append(Message(role="user", content=user_prompt))
    req = ModelRequest(
        model_snapshot=model_snapshot,
        messages=messages,
        params=params,
        agent_node_id=agent_node_id,
        prompt_version=prompt_version,
    )
    return router.call(req), request_hash(req)
