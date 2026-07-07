import pytest
from gameforge.contracts.model_router import Message, ModelRequest, ModelResponse, ModelSnapshot, request_hash
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import (
    CassetteReplayMiss, ModelRouter, QuotaExceeded, RouterMode,
)
from gameforge.runtime.model_router.transport import StubTransport


def _req(content="hi"):
    return ModelRequest(
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        messages=[Message(role="user", content=content)],
        agent_node_id="triage", prompt_version="triage@1",
    )


def test_record_then_replay_reproduces(tmp_path):
    req = _req()
    stub = StubTransport({request_hash(req): ModelResponse(response_normalized="live-answer")})
    store = CassetteStore(tmp_path)
    rec_router = ModelRouter(stub, store, mode=RouterMode.RECORD)
    assert rec_router.call(req).response_normalized == "live-answer"

    # REPLAY with a transport that would blow up if called → proves no live call
    class _Boom:
        def complete(self, r): raise AssertionError("REPLAY must not hit transport")
    rep_router = ModelRouter(_Boom(), store, mode=RouterMode.REPLAY)
    assert rep_router.call(req).response_normalized == "live-answer"


def test_replay_miss_raises(tmp_path):
    router = ModelRouter(StubTransport({}), CassetteStore(tmp_path), mode=RouterMode.REPLAY)
    with pytest.raises(CassetteReplayMiss):
        router.call(_req())


def test_session_cache_dedups_live_calls(tmp_path):
    req = _req()
    stub = StubTransport({request_hash(req): ModelResponse(response_normalized="x")})
    router = ModelRouter(stub, CassetteStore(tmp_path), mode=RouterMode.RECORD)
    router.call(req)
    router.call(req)
    assert len(stub.calls) == 1  # second call served from session cache


def test_quota_enforced(tmp_path):
    req_a, req_b = _req("a"), _req("b")
    stub = StubTransport({
        request_hash(req_a): ModelResponse(response_normalized="a"),
        request_hash(req_b): ModelResponse(response_normalized="b"),
    })
    router = ModelRouter(stub, CassetteStore(tmp_path), mode=RouterMode.RECORD, max_calls=1)
    router.call(req_a)
    with pytest.raises(QuotaExceeded):
        router.call(req_b)
