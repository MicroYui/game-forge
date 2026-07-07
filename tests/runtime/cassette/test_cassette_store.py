from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord
from gameforge.contracts.model_router import ModelResponse, ModelSnapshot
from gameforge.runtime.cassette.store import CassetteStore


def _rec(h="sha256:abc"):
    return CassetteRecord(
        request_hash=h, agent_node_id="triage",
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        response=ModelResponse(response_normalized="ok"),
    )


def test_record_then_replay_round_trips(tmp_path):
    store = CassetteStore(tmp_path)
    store.record(_rec())
    got = store.replay("sha256:abc")
    assert got.response.response_normalized == "ok"
    assert got == _rec()


def test_replay_miss_returns_sentinel(tmp_path):
    assert CassetteStore(tmp_path).replay("sha256:nope") is CASSETTE_MISS


def test_record_is_stable_on_disk(tmp_path):
    store = CassetteStore(tmp_path)
    store.record(_rec())
    files = sorted(p.name for p in tmp_path.glob("*.json"))
    assert files == ["abc.json"]
