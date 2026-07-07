from gameforge.contracts.model_router import Message, ModelRequest, ModelSnapshot, request_hash
from gameforge.runtime.model_router.transport import OpenAITransport, StubTransport


def _req(content="hi"):
    return ModelRequest(
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        messages=[Message(role="user", content=content)],
        agent_node_id="triage", prompt_version="triage@1",
    )


class _FakeChatCompletions:
    def create(self, **kw):
        class _Msg:  # minimal openai-response shape
            content = "hello from model"
            tool_calls = None
        class _Choice:
            message = _Msg()
            finish_reason = "stop"
        class _Usage:
            def model_dump(self): return {"prompt_tokens": 3, "completion_tokens": 4}
        class _Resp:
            choices = [_Choice()]
            usage = _Usage()
            def model_dump(self): return {"id": "x", "choices": []}
        return _Resp()


class _FakeClient:
    def __init__(self): self.chat = type("C", (), {"completions": _FakeChatCompletions()})()


def test_openai_transport_maps_response():
    t = OpenAITransport(base_url="http://localhost:4141", api_key="sk-x", client=_FakeClient())
    resp = t.complete(_req())
    assert resp.response_normalized == "hello from model"
    assert resp.finish_reason == "stop"
    assert resp.token_usage == {"prompt_tokens": 3, "completion_tokens": 4}


def test_openai_transport_constructs_default_client_when_not_injected(monkeypatch):
    # When no client is injected, OpenAITransport must build a real openai.OpenAI
    # client from base_url/api_key. Monkeypatch the SDK constructor itself so this
    # stays a pure unit test — no network call, no real key required.
    created = {}

    class _FakeOpenAI:
        def __init__(self, base_url, api_key):
            created["base_url"] = base_url
            created["api_key"] = api_key

    monkeypatch.setattr(
        "gameforge.runtime.model_router.transport.openai.OpenAI", _FakeOpenAI
    )
    t = OpenAITransport(base_url="http://localhost:4141", api_key="sk-x")
    assert created == {"base_url": "http://localhost:4141", "api_key": "sk-x"}
    assert isinstance(t._client, _FakeOpenAI)


def test_stub_transport_returns_by_request_hash():
    from gameforge.contracts.model_router import ModelResponse
    r = _req()
    stub = StubTransport({request_hash(r): ModelResponse(response_normalized="canned")})
    assert stub.complete(r).response_normalized == "canned"
    assert stub.calls == [r]
