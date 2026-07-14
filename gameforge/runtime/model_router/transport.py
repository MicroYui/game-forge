"""LLM transport — the ONLY module allowed to import an LLM SDK (import-linter).

OpenAITransport talks to the OpenAI-compatible gateway (localhost:4141). The
underlying client is injectable so unit tests exercise response-mapping with a
fake and never touch the network. StubTransport serves canned responses keyed by
request_hash for deterministic router/agent tests.
"""

from __future__ import annotations

import time
from typing import Protocol

import openai  # the one allowed SDK import (import-linter contract)

from gameforge.contracts.model_router import ModelRequest, ModelResponse, request_hash


class LlmTransport(Protocol):
    def complete(self, req: ModelRequest) -> ModelResponse: ...


class OpenAITransport:
    def __init__(self, base_url: str, api_key: str, client=None) -> None:
        self._client = client or openai.OpenAI(base_url=base_url, api_key=api_key)

    def complete(self, req: ModelRequest) -> ModelResponse:
        return self._complete(req, client=self._client)

    def complete_with_timeout(
        self,
        req: ModelRequest,
        *,
        timeout_s: float,
    ) -> ModelResponse:
        if timeout_s <= 0:
            raise TimeoutError("transport deadline has elapsed")
        client = self._client.with_options(timeout=timeout_s)
        return self._complete(req, client=client)

    @staticmethod
    def _complete(req: ModelRequest, *, client) -> ModelResponse:
        started = time.monotonic()
        resp = client.chat.completions.create(
            model=req.model_snapshot.model,
            messages=[m.model_dump(exclude_none=True) for m in req.messages],
            **req.params,
        )
        choice = resp.choices[0]
        usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
        tool_calls = getattr(choice.message, "tool_calls", None) or []
        return ModelResponse(
            response_normalized=choice.message.content or "",
            raw_response=resp.model_dump() if hasattr(resp, "model_dump") else {},
            latency_ms=int((time.monotonic() - started) * 1000),
            token_usage={k: int(v) for k, v in usage.items() if isinstance(v, int)},
            finish_reason=getattr(choice, "finish_reason", "") or "",
            tool_calls=[tc if isinstance(tc, dict) else tc.model_dump() for tc in tool_calls],
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
