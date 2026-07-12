import pytest
from pydantic import ValidationError

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


def test_historical_cassette_without_attempt_fields_remains_valid():
    record = CassetteRecord.model_validate(
        {
            "cassette_schema_version": "cassette@1",
            "request_hash": "sha256:historical",
            "agent_node_id": "triage",
            "model_snapshot": {
                "provider": "anthropic",
                "model": "claude-opus-4-8",
                "snapshot_tag": "m2a@1",
            },
            "response": {"response_normalized": "ok"},
        }
    )

    assert record.transport_attempts is None
    assert record.transport_retries is None


@pytest.mark.parametrize(
    ("attempts", "retries"),
    [(None, 0), (1, None), (0, 0), (2, 0)],
)
def test_cassette_attempt_fields_are_complete_and_consistent(attempts, retries):
    with pytest.raises(ValidationError):
        CassetteRecord(
            request_hash="sha256:invalid-attempts",
            agent_node_id="triage",
            model_snapshot=ModelSnapshot(
                provider="anthropic",
                model="claude-opus-4-8",
                snapshot_tag="m2a@1",
            ),
            response=ModelResponse(response_normalized="ok"),
            transport_attempts=attempts,
            transport_retries=retries,
        )
