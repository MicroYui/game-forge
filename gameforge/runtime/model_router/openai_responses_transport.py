"""HTTP transport for the OpenAI Responses API exposed by the local gateway."""

from __future__ import annotations

import time
from typing import Any

import httpx

from gameforge.contracts.model_router import ModelRequest, ModelRequestV2, ModelResponse


class OpenAIResponsesTransport:
    """Map Model Router contracts to the gateway's Responses API."""

    def __init__(self, base_url: str, api_key: str, client: Any | None = None) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/responses"
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
        if any(message.tool_calls for message in req.messages):
            raise ValueError("message tool_calls require explicit Responses item mapping")
        if any(message.role == "tool" for message in req.messages):
            raise ValueError("tool-role messages require explicit Responses item mapping")

        params = dict(req.params)
        directive = req.prefix_cache_directive if isinstance(req, ModelRequestV2) else None
        if isinstance(req, ModelRequestV2) and "prompt_cache_key" in params:
            raise ValueError("prompt_cache_key requires the typed prefix cache directive")
        if "max_tokens" in params and "max_output_tokens" in params:
            raise ValueError("max_tokens and max_output_tokens are mutually exclusive")
        if "max_tokens" in params:
            params["max_output_tokens"] = params.pop("max_tokens")

        body = {
            "model": req.model_snapshot.model,
            "input": [
                {"role": message.role, "content": message.content} for message in req.messages
            ],
            **params,
        }
        if directive is not None:
            body["prompt_cache_key"] = directive.prefix_hash.removeprefix("sha256:")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        started = time.monotonic()
        kwargs = {"json": body, "headers": headers}
        if timeout_s is not None:
            kwargs["timeout"] = timeout_s
        response = self._client.post(self._url, **kwargs)
        response.raise_for_status()
        payload = response.json()
        latency_ms = int((time.monotonic() - started) * 1000)

        output = payload.get("output") or []
        text = "".join(
            content.get("text") or ""
            for item in output
            if item.get("type") == "message"
            for content in item.get("content") or []
            if content.get("type") == "output_text"
        )
        status = payload.get("status") or ""
        incomplete_details = payload.get("incomplete_details") or {}
        finish_reason = (
            incomplete_details.get("reason") or status if status == "incomplete" else status
        )
        usage = payload.get("usage") or {}
        token_usage = {key: value for key, value in usage.items() if type(value) is int}
        input_details = usage.get("input_tokens_details") or {}
        cached_tokens = input_details.get("cached_tokens")
        if type(cached_tokens) is int and cached_tokens >= 0:
            token_usage["cache_read_tokens"] = cached_tokens
        tool_calls = [item for item in output if item.get("type") == "function_call"]
        return ModelResponse(
            response_normalized=text,
            raw_response=payload,
            latency_ms=latency_ms,
            token_usage=token_usage,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )
