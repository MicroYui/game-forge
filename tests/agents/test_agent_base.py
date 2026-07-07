import pytest
from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.contracts.model_router import Message, ModelRequest, ModelResponse, request_hash
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.runtime.model_router.transport import StubTransport


def test_parse_json_block_handles_fences_and_prose():
    assert parse_json_block('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_block('Sure! [1, 2, 3] done.') == [1, 2, 3]
    with pytest.raises(AgentParseError):
        parse_json_block("no json here")


def test_call_model_builds_request_and_returns_hash(tmp_path):
    from gameforge.agents.base import DEFAULT_SNAPSHOT
    probe = ModelRequest(
        model_snapshot=DEFAULT_SNAPSHOT,
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        params={"max_tokens": 2048, "temperature": 0},
        agent_node_id="probe", prompt_version="p@1",
    )
    stub = StubTransport({request_hash(probe): ModelResponse(response_normalized='{"ok": true}')})
    router = ModelRouter(stub, CassetteStore(tmp_path), mode=RouterMode.RECORD)
    resp, h = call_model(router, "probe", "hi", "p@1", system="sys")
    assert h == request_hash(probe)
    assert parse_json_block(resp.response_normalized) == {"ok": True}
