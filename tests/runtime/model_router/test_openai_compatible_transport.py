from __future__ import annotations

from gameforge.contracts.model_router import Message, ModelRequest, ModelSnapshot
from gameforge.runtime.model_router.openai_compatible_transport import (
    OpenAICompatibleTransport,
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


def _request() -> ModelRequest:
    return ModelRequest(
        model_snapshot=ModelSnapshot(
            provider="openai", model="gpt5.6sol", snapshot_tag="pre-m4@1"
        ),
        messages=[
            Message(role="system", content="Return JSON."),
            Message(role="user", content="Repair this."),
        ],
        params={"max_tokens": 256, "temperature": 0},
        agent_node_id="repair",
        prompt_version="repair@4",
    )


def test_openai_compatible_transport_maps_gateway_request_and_response():
    payload = {
        "id": "response-1",
        "choices": [
            {
                "message": {
                    "content": '[{"op":"delete_relation"}]',
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "patch", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "ignored": "x"},
    }
    client = _FakeClient(payload)
    transport = OpenAICompatibleTransport(
        base_url="http://localhost:4141/", api_key="secret", client=client
    )

    response = transport.complete(_request())

    url, body, headers = client.calls[0]
    assert url == "http://localhost:4141/v1/chat/completions"
    assert body == {
        "model": "gpt5.6sol",
        "messages": [
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "Repair this."},
        ],
        "max_tokens": 256,
        "temperature": 0,
    }
    assert headers["Authorization"] == "Bearer secret"
    assert headers["Content-Type"] == "application/json"
    assert response.response_normalized == '[{"op":"delete_relation"}]'
    assert response.finish_reason == "tool_calls"
    assert response.token_usage == {"prompt_tokens": 10, "completion_tokens": 4}
    assert response.tool_calls == payload["choices"][0]["message"]["tool_calls"]
    assert response.raw_response == payload


def test_openai_compatible_transport_handles_missing_optional_response_fields():
    client = _FakeClient({"choices": [{"message": {}, "finish_reason": None}]})
    response = OpenAICompatibleTransport(
        base_url="http://localhost:4141", api_key="secret", client=client
    ).complete(_request())

    assert response.response_normalized == ""
    assert response.finish_reason == ""
    assert response.token_usage == {}
    assert response.tool_calls == []
