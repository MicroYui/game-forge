"""HTTP transport for the local OpenAI-compatible model gateway.

This transport deliberately uses ``httpx`` instead of an LLM SDK. Agent
harnesses may therefore construct it without acquiring a transitive dependency
on the SDK-only transport module.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from gameforge.contracts.model_router import ModelRequest, ModelResponse


class OpenAICompatibleTransport:
    """Map Model Router contracts to the gateway's chat-completions API."""

    def __init__(self, base_url: str, api_key: str, client: Any | None = None) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/chat/completions"
        self._api_key = api_key
        self._client = client if client is not None else httpx.Client(timeout=60.0)

    def complete(self, req: ModelRequest) -> ModelResponse:
        messages: list[dict[str, Any]] = []
        for item in req.messages:
            message = item.model_dump(exclude_none=True)
            if not message.get("tool_calls"):
                message.pop("tool_calls", None)
            messages.append(message)

        body = {
            "model": req.model_snapshot.model,
            "messages": messages,
            **req.params,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        started = time.monotonic()
        response = self._client.post(self._url, json=body, headers=headers)
        response.raise_for_status()
        payload = response.json()
        latency_ms = int((time.monotonic() - started) * 1000)

        choice = payload["choices"][0]
        message = choice.get("message") or {}
        usage = payload.get("usage") or {}
        return ModelResponse(
            response_normalized=message.get("content") or "",
            raw_response=payload,
            latency_ms=latency_ms,
            token_usage={key: value for key, value in usage.items() if type(value) is int},
            finish_reason=choice.get("finish_reason") or "",
            tool_calls=message.get("tool_calls") or [],
        )
