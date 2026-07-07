from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord
from gameforge.contracts.model_router import ModelResponse, ModelSnapshot


def test_cassette_record_round_trips():
    r = CassetteRecord(
        request_hash="sha256:x", agent_node_id="triage",
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        response=ModelResponse(response_normalized="ok"),
    )
    assert CassetteRecord.model_validate(r.model_dump()) == r
    assert r.cassette_schema_version == "cassette@1"


def test_cassette_miss_is_a_distinct_sentinel():
    assert CASSETTE_MISS is not None
    assert CASSETTE_MISS != CassetteRecord.model_construct()
