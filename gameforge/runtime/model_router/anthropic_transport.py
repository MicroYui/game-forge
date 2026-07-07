"""AnthropicMessagesTransport — reaches claude-opus-4-8 via the gateway's
Anthropic-native /v1/messages endpoint (the OpenAI-compatible /chat/completions
endpoint that OpenAITransport uses does not serve opus). Lives in
runtime.model_router alongside OpenAITransport: this package is the only place
allowed to make HTTP calls to the LLM gateway (import-linter contract). httpx is
a generic HTTP client, not an LLM SDK, but the transport itself belongs here.

The underlying HTTP client is injectable so unit tests exercise the Anthropic
Messages request/response mapping with a fake and never touch the network.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from gameforge.contracts.model_router import ModelRequest, ModelResponse

_DEFAULT_MAX_TOKENS = 4096
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicMessagesTransport:
    def __init__(self, base_url: str, api_key: str, client: Any = None) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=60)

    def complete(self, req: ModelRequest) -> ModelResponse:
        # Anthropic puts the system prompt in a top-level `system` string, not in
        # `messages` — concatenate every system-role Message's content into it.
        system = "\n\n".join(m.content for m in req.messages if m.role == "system")
        messages = [
            {"role": m.role, "content": m.content} for m in req.messages if m.role != "system"
        ]
        max_tokens = req.params.get("max_tokens", _DEFAULT_MAX_TOKENS)
        body: dict[str, Any] = {
            "model": req.model_snapshot.model,
            "max_tokens": max_tokens,
            "messages": messages,
            **({"system": system} if system else {}),
            **{k: v for k, v in req.params.items() if k != "max_tokens"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        started = time.monotonic()
        resp = self._client.post(f"{self._base_url}/v1/messages", json=body, headers=headers)
        resp.raise_for_status()
        latency_ms = int((time.monotonic() - started) * 1000)
        data = resp.json()

        content = data.get("content", [])
        response_normalized = "".join(
            block.get("text", "") for block in content if block.get("type") == "text"
        )
        tool_calls = [block for block in content if block.get("type") == "tool_use"]

        copilot_usage = data.get("copilot_usage") or {}
        token_details = copilot_usage.get("token_details")
        if token_details:
            token_usage = {td["token_type"]: int(td["token_count"]) for td in token_details}
        elif "usage" in data:
            usage = data.get("usage") or {}
            token_usage = {
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
            }
        else:
            token_usage = {}

        return ModelResponse(
            response_normalized=response_normalized,
            raw_response=data,
            latency_ms=latency_ms,
            token_usage=token_usage,
            finish_reason=data.get("stop_reason", "") or "",
            tool_calls=tool_calls,
        )
