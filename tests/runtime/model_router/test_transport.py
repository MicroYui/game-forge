"""The OpenAI-compatible chat-completions surface, over the same injectable client
its two sibling transports use — no LLM SDK, no network in these tests.
"""

from typing import Any

from gameforge.contracts.model_router import Message, ModelRequest, ModelSnapshot, request_hash
from gameforge.runtime.model_router.transport import OpenAITransport, StubTransport


def _req(content="hi"):
    return ModelRequest(
        model_snapshot=ModelSnapshot(
            provider="google", model="gemini-3.6-flash", snapshot_tag="s1"
        ),
        messages=[Message(role="user", content=content)],
        agent_node_id="triage",
        prompt_version="triage@1",
    )


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._payload = payload or {
            "choices": [
                {
                    "message": {"content": "hello from model", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        return _Response(self._payload)


def test_openai_transport_maps_response():
    client = _FakeClient()
    transport = OpenAITransport(
        base_url="http://localhost:4141",
        api_key="sk-x",
        client=client,
    )

    resp = transport.complete(_req())

    assert resp.response_normalized == "hello from model"
    assert resp.finish_reason == "stop"
    assert resp.token_usage == {"prompt_tokens": 3, "completion_tokens": 4}
    url, kwargs = client.calls[0]
    assert url == "http://localhost:4141/chat/completions"
    assert kwargs["json"]["model"] == "gemini-3.6-flash"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-x"


def test_openai_transport_applies_remaining_attempt_timeout():
    client = _FakeClient()
    transport = OpenAITransport(
        base_url="http://localhost:4141",
        api_key="sk-x",
        client=client,
    )

    transport.complete_with_timeout(_req(), timeout_s=4.5)

    assert client.calls[0][1]["timeout"] == 4.5


def test_openai_transport_refuses_an_elapsed_deadline():
    transport = OpenAITransport(
        base_url="http://localhost:4141",
        api_key="sk-x",
        client=_FakeClient(),
    )

    try:
        transport.complete_with_timeout(_req(), timeout_s=0)
    except TimeoutError as error:
        assert "deadline" in str(error)
    else:  # pragma: no cover - the transport must not call a dead deadline
        raise AssertionError("an elapsed deadline must not reach the gateway")


def test_stub_transport_returns_by_request_hash():
    from gameforge.contracts.model_router import ModelResponse

    r = _req()
    stub = StubTransport({request_hash(r): ModelResponse(response_normalized="canned")})
    assert stub.complete(r).response_normalized == "canned"
    assert stub.calls == [r]


def test_openai_transport_maps_tool_calls_and_raw_response():
    payload = {
        "id": "resp-1",
        "choices": [
            {
                "message": {
                    "content": "with tools",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "patch"}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    transport = OpenAITransport(
        base_url="http://localhost:4141",
        api_key="sk-x",
        client=_FakeClient(payload),
    )

    resp = transport.complete(_req())

    assert resp.tool_calls == [{"id": "call_1", "type": "function", "function": {"name": "patch"}}]
    assert resp.raw_response == payload
    assert resp.finish_reason == "tool_calls"
    assert resp.token_usage == {}  # no usage reported → empty dict
