from __future__ import annotations

import pytest

from gameforge.contracts.model_router import Message, ModelRequest, ModelSnapshot
from gameforge.runtime.model_router.openai_responses_transport import (
    OpenAIResponsesTransport,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def post(self, url, *, json, headers):
        self.calls.append((url, json, headers))
        return _FakeResponse(self._payload)


def _request(**params) -> ModelRequest:
    return ModelRequest(
        model_snapshot=ModelSnapshot(
            provider="openai",
            model="gpt-5.6-sol",
            snapshot_tag="pre-m4@1",
        ),
        messages=[
            Message(role="system", content="Return JSON."),
            Message(role="user", content="Analyze this evidence."),
        ],
        params=params or {"max_tokens": 256},
        agent_node_id="external-evidence",
        prompt_version="external-evidence@1",
    )


def test_responses_transport_maps_request_and_normalizes_text_and_usage():
    payload = {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "reasoning_1", "summary": []},
            {
                "type": "message",
                "id": "message_1",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": '{"status":'},
                    {"type": "output_text", "text": '"ready"}'},
                ],
            },
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "input_tokens_details": {"cached_tokens": 0},
        },
    }
    client = _FakeClient(payload)
    transport = OpenAIResponsesTransport(
        base_url="http://localhost:4141/",
        api_key="secret",
        client=client,
    )

    response = transport.complete(_request())

    url, body, headers = client.calls[0]
    assert url == "http://localhost:4141/v1/responses"
    assert body == {
        "model": "gpt-5.6-sol",
        "input": [
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "Analyze this evidence."},
        ],
        "max_output_tokens": 256,
    }
    assert headers["Authorization"] == "Bearer secret"
    assert headers["Content-Type"] == "application/json"
    assert response.response_normalized == '{"status":"ready"}'
    assert response.finish_reason == "completed"
    assert response.token_usage == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
    }
    assert response.tool_calls == []
    assert response.raw_response == payload


def test_responses_transport_maps_incomplete_reason_and_function_calls():
    function_call = {
        "type": "function_call",
        "id": "call_1",
        "call_id": "call_1",
        "name": "classify",
        "arguments": "{}",
        "status": "completed",
    }
    client = _FakeClient(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [function_call],
        }
    )

    response = OpenAIResponsesTransport(
        base_url="http://localhost:4141",
        api_key="secret",
        client=client,
    ).complete(_request(max_output_tokens=64))

    assert client.calls[0][1]["max_output_tokens"] == 64
    assert response.response_normalized == ""
    assert response.finish_reason == "max_output_tokens"
    assert response.tool_calls == [function_call]


def test_responses_transport_rejects_ambiguous_token_params_and_message_tool_calls():
    client = _FakeClient({"status": "completed", "output": []})
    transport = OpenAIResponsesTransport(
        base_url="http://localhost:4141",
        api_key="secret",
        client=client,
    )

    with pytest.raises(ValueError, match="max_tokens"):
        transport.complete(_request(max_tokens=64, max_output_tokens=64))

    request = _request()
    request.messages[0].tool_calls = [{"id": "call_1"}]
    with pytest.raises(ValueError, match="tool_calls"):
        transport.complete(request)

    tool_request = _request()
    tool_request.messages = [Message(role="tool", content="{}")]
    with pytest.raises(ValueError, match="tool-role"):
        transport.complete(tool_request)

    assert client.calls == []
