"""HTTP transport for the OpenAI Responses API exposed by the local gateway."""

from __future__ import annotations

import time
from typing import Any

import httpx

from gameforge.contracts.model_router import ModelRequest, ModelResponse


class OpenAIResponsesTransport:
    """Map Model Router contracts to the gateway's Responses API."""

    def __init__(self, base_url: str, api_key: str, client: Any | None = None) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/responses"
        self._api_key = api_key
        self._client = client if client is not None else httpx.Client(timeout=60.0)

    def complete(self, req: ModelRequest) -> ModelResponse:
        if any(message.tool_calls for message in req.messages):
            raise ValueError(
                "message tool_calls require explicit Responses item mapping"
            )
        if any(message.role == "tool" for message in req.messages):
            raise ValueError(
                "tool-role messages require explicit Responses item mapping"
            )

        params = dict(req.params)
        if "max_tokens" in params and "max_output_tokens" in params:
            raise ValueError("max_tokens and max_output_tokens are mutually exclusive")
        if "max_tokens" in params:
            params["max_output_tokens"] = params.pop("max_tokens")

        body = {
            "model": req.model_snapshot.model,
            "input": [
                {"role": message.role, "content": message.content}
                for message in req.messages
            ],
            **params,
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
            incomplete_details.get("reason") or status
            if status == "incomplete"
            else status
        )
        usage = payload.get("usage") or {}
        tool_calls = [item for item in output if item.get("type") == "function_call"]
        return ModelResponse(
            response_normalized=text,
            raw_response=payload,
            latency_ms=latency_ms,
            token_usage={key: value for key, value in usage.items() if type(value) is int},
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )
