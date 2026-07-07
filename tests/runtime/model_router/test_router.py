import pytest
from gameforge.contracts.cassette import CASSETTE_MISS
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


class _FlakyTransport:
    """Raises on its first N calls, then succeeds — exercises the retry path."""

    def __init__(self, fail_times: int, response: ModelResponse) -> None:
        self._fail_times = fail_times
        self._response = response
        self.calls = 0

    def complete(self, req: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient gateway error")
        return self._response


def test_retry_recovers_from_transient_failure(tmp_path):
    req = _req()
    flaky = _FlakyTransport(fail_times=2, response=ModelResponse(response_normalized="ok-after-retry"))
    router = ModelRouter(flaky, CassetteStore(tmp_path), mode=RouterMode.RECORD, max_retries=2)
    resp = router.call(req)
    assert resp.response_normalized == "ok-after-retry"
    assert flaky.calls == 3  # 1 initial + 2 retries


def test_retry_exhausted_raises(tmp_path):
    req = _req()
    flaky = _FlakyTransport(fail_times=99, response=ModelResponse(response_normalized="never"))
    router = ModelRouter(flaky, CassetteStore(tmp_path), mode=RouterMode.RECORD, max_retries=2)
    with pytest.raises(RuntimeError):
        router.call(req)
    assert flaky.calls == 3  # 1 initial + 2 retries, then gives up


def test_passthrough_calls_transport_but_never_writes_cassette(tmp_path):
    req = _req()
    stub = StubTransport({request_hash(req): ModelResponse(response_normalized="live-only")})
    store = CassetteStore(tmp_path)
    router = ModelRouter(stub, store, mode=RouterMode.PASSTHROUGH)
    resp = router.call(req)
    assert resp.response_normalized == "live-only"
    assert len(stub.calls) == 1
    assert store.replay(request_hash(req)) is CASSETTE_MISS


def test_quota_counts_every_transport_attempt(tmp_path):
    # Under sustained failure the quota must trip on transport ATTEMPTS, not only
    # successful calls — otherwise the circuit-breaker never fires during an outage.
    class _AlwaysFails:
        def __init__(self):
            self.attempts = 0

        def complete(self, r):
            self.attempts += 1
            raise RuntimeError("gateway down")

    transport = _AlwaysFails()
    router = ModelRouter(transport, CassetteStore(tmp_path),
                         mode=RouterMode.RECORD, max_retries=5, max_calls=2)
    with pytest.raises(QuotaExceeded):
        router.call(_req())
    assert transport.attempts == 2  # exactly max_calls attempts, then quota trips
