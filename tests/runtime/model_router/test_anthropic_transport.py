"""AnthropicMessagesTransport — reaches claude-opus-4-8 via the gateway's
Anthropic-native /v1/messages endpoint (the OpenAI-compatible /chat/completions
endpoint the OpenAITransport uses does not serve opus). Unit tests inject a fake
httpx-shaped client (`.post(url, json=..., headers=...)` -> object with `.json()`
and `.raise_for_status()`) so no network call is ever made here. One live
integration test is gated on GAMEFORGE_LLM_LIVE=1 so CI stays zero-live.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from gameforge.contracts.model_router import Message, ModelRequest, ModelSnapshot
from gameforge.runtime.model_router.anthropic_transport import AnthropicMessagesTransport

_REAL_SHAPE = {
    "content": [{"text": "pong", "type": "text"}],
    "copilot_usage": {
        "token_details": [
            {"batch_size": 1000000, "cost_per_batch": 500000000000, "token_count": 18, "token_type": "input"},
            {"token_count": 0, "token_type": "cache_read"},
            {"token_count": 0, "token_type": "cache_write"},
            {"token_count": 4, "token_type": "output"},
        ],
        "total_nano_aiu": 19000000,
    },
    "id": "msg_011...",
    "model": "claude-opus-4-8",
    "role": "assistant",
    "stop_details": None,
    "stop_reason": "end_turn",
}


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.raise_for_status_called = False

    def raise_for_status(self) -> None:
        self.raise_for_status_called = True

    def json(self) -> dict:
        return self._data


class _FakeClient:
    """Captures every `.post(...)` call so tests can assert on the request body."""

    def __init__(self, data: dict) -> None:
        self._data = data
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None):  # noqa: A002 - mirrors httpx signature
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._data)


def _req(messages, params=None):
    return ModelRequest(
        model_snapshot=ModelSnapshot(provider="anthropic", model="claude-opus-4-8", snapshot_tag="s1"),
        messages=messages,
        params=params or {},
        agent_node_id="triage",
        prompt_version="triage@1",
    )


def test_maps_real_response_shape():
    fake = _FakeClient(_REAL_SHAPE)
    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x", client=fake)
    resp = t.complete(_req([Message(role="user", content="ping")]))

    assert resp.response_normalized == "pong"
    assert resp.finish_reason == "end_turn"
    assert resp.token_usage == {"input": 18, "cache_read": 0, "cache_write": 0, "output": 4}
    assert resp.raw_response == _REAL_SHAPE
    assert resp.latency_ms >= 0


def test_system_message_extracted_to_top_level():
    fake = _FakeClient(_REAL_SHAPE)
    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x", client=fake)
    t.complete(_req([
        Message(role="system", content="you are terse"),
        Message(role="user", content="hi"),
    ]))

    assert len(fake.calls) == 1
    body = fake.calls[0]["json"]
    assert body["system"] == "you are terse"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    # headers per spec
    headers = fake.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer sk-x"
    assert headers["x-api-key"] == "sk-x"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["content-type"] == "application/json"
    # url
    assert fake.calls[0]["url"] == "http://localhost:4141/v1/messages"


def test_multiple_system_messages_joined_with_blank_line():
    fake = _FakeClient(_REAL_SHAPE)
    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x", client=fake)
    t.complete(_req([
        Message(role="system", content="first"),
        Message(role="system", content="second"),
        Message(role="user", content="hi"),
    ]))
    assert fake.calls[0]["json"]["system"] == "first\n\nsecond"


def test_no_system_message_omits_system_key():
    fake = _FakeClient(_REAL_SHAPE)
    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x", client=fake)
    t.complete(_req([Message(role="user", content="hi")]))
    assert "system" not in fake.calls[0]["json"]


def test_max_tokens_defaults_to_4096_when_absent():
    fake = _FakeClient(_REAL_SHAPE)
    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x", client=fake)
    t.complete(_req([Message(role="user", content="hi")]))
    assert fake.calls[0]["json"]["max_tokens"] == 4096


def test_max_tokens_honored_when_present():
    fake = _FakeClient(_REAL_SHAPE)
    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x", client=fake)
    t.complete(_req([Message(role="user", content="hi")], params={"max_tokens": 16}))
    assert fake.calls[0]["json"]["max_tokens"] == 16
    # must not leak into the params-passthrough spread as a duplicate key error
    assert fake.calls[0]["json"]["model"] == "claude-opus-4-8"


def test_other_params_pass_through_except_max_tokens():
    fake = _FakeClient(_REAL_SHAPE)
    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x", client=fake)
    t.complete(_req([Message(role="user", content="hi")], params={"max_tokens": 16, "temperature": 0}))
    body = fake.calls[0]["json"]
    assert body["temperature"] == 0
    assert body["max_tokens"] == 16


def test_tool_use_content_block_surfaces_in_tool_calls():
    data = {
        "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "sf"}},
        ],
        "id": "msg_1",
        "model": "claude-opus-4-8",
        "role": "assistant",
        "stop_reason": "tool_use",
    }
    fake = _FakeClient(data)
    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x", client=fake)
    resp = t.complete(_req([Message(role="user", content="weather?")]))

    assert resp.response_normalized == "let me check"
    assert resp.tool_calls == [
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "sf"}}
    ]
    assert resp.finish_reason == "tool_use"


def test_usage_fallback_when_copilot_usage_absent():
    data = {
        "content": [{"type": "text", "text": "hi"}],
        "id": "msg_2",
        "model": "claude-opus-4-8",
        "role": "assistant",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }
    fake = _FakeClient(data)
    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x", client=fake)
    resp = t.complete(_req([Message(role="user", content="hi")]))
    assert resp.token_usage == {"input": 7, "output": 3}


def test_no_usage_at_all_yields_empty_token_usage():
    data = {
        "content": [{"type": "text", "text": "hi"}],
        "id": "msg_3",
        "model": "claude-opus-4-8",
        "role": "assistant",
        "stop_reason": "end_turn",
    }
    fake = _FakeClient(data)
    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x", client=fake)
    resp = t.complete(_req([Message(role="user", content="hi")]))
    assert resp.token_usage == {}


def test_default_client_is_httpx_when_not_injected():
    import httpx

    t = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key="sk-x")
    assert isinstance(t._client, httpx.Client)


@pytest.mark.skipif(os.environ.get("GAMEFORGE_LLM_LIVE") != "1", reason="live gateway call; set GAMEFORGE_LLM_LIVE=1")
def test_live_opus_call_via_messages():
    # Load GAMEFORGE_LLM_KEY from the gitignored .env if not already in the environment
    # (mirrors .superpowers/sdd/live_probe.py's approach — no python-dotenv dependency).
    if not os.environ.get("GAMEFORGE_LLM_KEY"):
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("GAMEFORGE_LLM_KEY="):
                    os.environ["GAMEFORGE_LLM_KEY"] = line.split("=", 1)[1].strip()

    from gameforge.runtime.secrets.env import get_llm_key

    transport = AnthropicMessagesTransport(base_url="http://localhost:4141", api_key=get_llm_key())
    req = _req(
        [Message(role="user", content="Reply with exactly one word: pong")],
        params={"max_tokens": 16, "temperature": 0},
    )
    resp = transport.complete(req)

    assert resp.response_normalized.strip() != ""
    assert resp.finish_reason
