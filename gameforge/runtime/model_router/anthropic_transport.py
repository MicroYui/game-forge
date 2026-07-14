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

from gameforge.contracts.model_router import ModelRequest, ModelRequestV2, ModelResponse

_DEFAULT_MAX_TOKENS = 4096
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicMessagesTransport:
    def __init__(self, base_url: str, api_key: str, client: Any = None) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=60)

    def complete(self, req: ModelRequest) -> ModelResponse:
        return self._complete(req, timeout_s=None)

    def complete_with_timeout(
        self,
        req: ModelRequest,
        *,
        timeout_s: float,
    ) -> ModelResponse:
        if timeout_s <= 0:
            raise TimeoutError("transport deadline has elapsed")
        return self._complete(req, timeout_s=timeout_s)

    def _complete(self, req: ModelRequest, *, timeout_s: float | None) -> ModelResponse:
        # Anthropic puts the system prompt in a top-level `system` string, not in
        # `messages` — concatenate every system-role Message's content into it.
        directive = req.prefix_cache_directive if isinstance(req, ModelRequestV2) else None
        if directive is None:
            system: str | list[dict[str, Any]] = "\n\n".join(
                m.content for m in req.messages if m.role == "system"
            )
            messages = [
                {"role": m.role, "content": m.content} for m in req.messages if m.role != "system"
            ]
        else:
            system, messages = _map_prefix_cached_messages(
                req,
                prefix_message_count=directive.prefix_message_count,
            )
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
        kwargs = {"json": body, "headers": headers}
        if timeout_s is not None:
            kwargs["timeout"] = timeout_s
        resp = self._client.post(f"{self._base_url}/v1/messages", **kwargs)
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


def _map_prefix_cached_messages(
    req: ModelRequestV2,
    *,
    prefix_message_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system_count = 0
    for message in req.messages:
        if message.role != "system":
            break
        system_count += 1
    if any(message.role == "system" for message in req.messages[system_count:]):
        raise ValueError("Anthropic prefix caching requires leading system messages")

    boundary = prefix_message_count - 1
    system: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    for index, message in enumerate(req.messages):
        text = message.content
        if message.role == "system" and index + 1 < system_count:
            text += "\n\n"
        block: dict[str, Any] = {"type": "text", "text": text}
        if index == boundary:
            block["cache_control"] = {"type": "ephemeral"}
        if message.role == "system":
            system.append(block)
        else:
            messages.append({"role": message.role, "content": [block]})
    return system, messages
