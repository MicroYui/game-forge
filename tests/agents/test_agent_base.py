import pytest
from gameforge.agents.base import (
    DEFAULT_SNAPSHOT,
    M2_REPLAY_SNAPSHOT,
    AgentParseError,
    call_model,
    parse_json_block,
)
from gameforge.contracts.model_router import (
    Message,
    ModelRequest,
    ModelResponse,
    ModelSnapshot,
    request_hash,
)
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.runtime.model_router.transport import StubTransport


def test_parse_json_block_handles_fences_and_prose():
    assert parse_json_block('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_block('Sure! [1, 2, 3] done.') == [1, 2, 3]
    with pytest.raises(AgentParseError):
        parse_json_block("no json here")


def test_call_model_builds_request_and_returns_hash(tmp_path):
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


def test_future_agent_default_uses_gpt56sol_without_rewriting_m2_snapshot():
    assert DEFAULT_SNAPSHOT == ModelSnapshot(
        provider="openai",
        model="gpt5.6sol",
        snapshot_tag="pre-m4@1",
    )
    assert M2_REPLAY_SNAPSHOT == ModelSnapshot(
        provider="anthropic",
        model="claude-opus-4-8",
        snapshot_tag="m2a@1",
    )


def test_router_snapshot_policy_selects_replay_model_and_explicit_node_wins(tmp_path):
    replay_probe = ModelRequest(
        model_snapshot=M2_REPLAY_SNAPSHOT,
        messages=[Message(role="user", content="legacy")],
        params={"max_tokens": 2048, "temperature": 0},
        agent_node_id="probe",
        prompt_version="p@1",
    )
    explicit_probe = replay_probe.model_copy(
        update={
            "model_snapshot": DEFAULT_SNAPSHOT,
            "messages": [Message(role="user", content="explicit")],
        }
    )
    responses = {
        request_hash(replay_probe): ModelResponse(response_normalized='{"mode": "replay"}'),
        request_hash(explicit_probe): ModelResponse(response_normalized='{"mode": "explicit"}'),
    }
    router = ModelRouter(
        StubTransport(responses),
        CassetteStore(tmp_path),
        mode=RouterMode.RECORD,
        default_model_snapshot=M2_REPLAY_SNAPSHOT,
    )

    replay_response, replay_hash = call_model(router, "probe", "legacy", "p@1")
    explicit_response, explicit_hash = call_model(
        router,
        "probe",
        "explicit",
        "p@1",
        snapshot=DEFAULT_SNAPSHOT,
    )

    assert replay_hash == request_hash(replay_probe)
    assert parse_json_block(replay_response.response_normalized) == {"mode": "replay"}
    assert explicit_hash == request_hash(explicit_probe)
    assert parse_json_block(explicit_response.response_normalized) == {"mode": "explicit"}
