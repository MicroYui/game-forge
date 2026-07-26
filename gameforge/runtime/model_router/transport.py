"""HTTP transports for the OpenAI-compatible `/chat/completions` surface.

`OpenAITransport` is the sibling of `OpenAIResponsesTransport` and
`AnthropicMessagesTransport`: same package, same injectable `httpx` client, one
surface each. Gemini models on this gateway serve only `/chat/completions`, so a
worker that can reach every catalogued model needs all three.

`StubTransport` serves canned responses keyed by request_hash for deterministic
router/agent tests.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx

from gameforge.contracts.model_router import ModelRequest, ModelResponse, request_hash


class LlmTransport(Protocol):
    def complete(self, req: ModelRequest) -> ModelResponse: ...


class OpenAITransport:
    def __init__(self, base_url: str, api_key: str, client: Any | None = None) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._client = client if client is not None else httpx.Client(timeout=60.0)

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

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _complete(self, req: ModelRequest, *, timeout_s: float | None) -> ModelResponse:
        body = {
            "model": req.model_snapshot.model,
            "messages": [message.model_dump(exclude_none=True) for message in req.messages],
            **req.params,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        started = time.monotonic()
        kwargs: dict[str, Any] = {"json": body, "headers": headers}
        if timeout_s is not None:
            kwargs["timeout"] = timeout_s
        response = self._client.post(self._url, **kwargs)
        response.raise_for_status()
        payload = response.json()
        latency_ms = int((time.monotonic() - started) * 1000)

        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = payload.get("usage") or {}
        return ModelResponse(
            response_normalized=message.get("content") or "",
            raw_response=payload,
            latency_ms=latency_ms,
            token_usage={key: value for key, value in usage.items() if type(value) is int},
            finish_reason=choice.get("finish_reason") or "",
            tool_calls=list(message.get("tool_calls") or []),
        )


class StubTransport:
    """Deterministic transport for tests: returns canned responses by request_hash."""

    def __init__(self, responses: dict[str, ModelResponse]) -> None:
        self._responses = responses
        self.calls: list[ModelRequest] = []

    def complete(self, req: ModelRequest) -> ModelResponse:
        self.calls.append(req)
        return self._responses[request_hash(req)]

    def complete_with_timeout(
        self,
        req: ModelRequest,
        *,
        timeout_s: float,
    ) -> ModelResponse:
        if timeout_s <= 0:
            raise TimeoutError("transport deadline has elapsed")
        return self.complete(req)
