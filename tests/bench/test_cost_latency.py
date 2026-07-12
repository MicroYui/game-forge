"""Source-neutral cassette token and record-time latency evidence."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from gameforge.bench.cost_latency import (
    AgentCostLatencyEvidence,
    SampleTrace,
    aggregate_sample,
    aggregate_workload,
    canonical_evidence_bytes,
    load_evidence,
    normalize_tokens,
    seal_agent_cost_evidence,
    validate_agent_cost_evidence,
    write_evidence,
)
from gameforge.bench.report_contracts import TokenTotals
from gameforge.contracts.cassette import CassetteRecord
from gameforge.contracts.model_router import ModelResponse, ModelSnapshot
from gameforge.runtime.cassette.store import CassetteStore


GPT_56 = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="pre-m4@1",
)
OPUS_M2 = ModelSnapshot(
    provider="anthropic",
    model="claude-opus-4-8",
    snapshot_tag="m2a@1",
)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _openai_record(
    request_hash: str = HASH_A,
    *,
    attempts: int | None = 1,
) -> CassetteRecord:
    return CassetteRecord(
        request_hash=request_hash,
        agent_node_id="repair",
        model_snapshot=GPT_56,
        response=ModelResponse(
            response_normalized="{}",
            raw_response={
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "input_tokens_details": {
                        "cached_tokens": 60,
                        "cache_write_tokens": 40,
                    },
                }
            },
            latency_ms=1200,
            token_usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        ),
        transport_attempts=attempts,
        transport_retries=(attempts - 1 if attempts is not None else None),
    )


def _anthropic_record(request_hash: str = HASH_B) -> CassetteRecord:
    return CassetteRecord(
        request_hash=request_hash,
        agent_node_id="playtest.executor",
        model_snapshot=OPUS_M2,
        response=ModelResponse(
            response_normalized="{}",
            latency_ms=800,
            token_usage={
                "input": 10,
                "output": 5,
                "cache_read": 70,
                "cache_write": 30,
            },
        ),
    )


def _record(root, record: CassetteRecord) -> None:
    CassetteStore(root).record(record)


def test_token_normalization_preserves_cache_components_without_double_counting():
    assert normalize_tokens(_openai_record()) == TokenTotals(
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=60,
        cache_write_tokens=40,
        reported_total_tokens=120,
    )
    assert normalize_tokens(_anthropic_record()) == TokenTotals(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=70,
        cache_write_tokens=30,
        reported_total_tokens=115,
    )


def test_legacy_openai_aliases_are_normalized():
    record = _openai_record().model_copy(
        update={
            "response": ModelResponse(
                response_normalized="{}",
                latency_ms=100,
                token_usage={"prompt_tokens": 7, "completion_tokens": 3},
            )
        }
    )
    assert normalize_tokens(record) == TokenTotals(
        input_tokens=7,
        output_tokens=3,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reported_total_tokens=10,
    )


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": -1, "output_tokens": 1, "total_tokens": 0},
        {"input_tokens": 2, "prompt_tokens": 2, "output_tokens": 1},
        {"input_tokens": 2, "output_tokens": 1, "total_tokens": 9},
        {"input_tokens": True, "output_tokens": 1, "total_tokens": 2},
    ],
)
def test_token_normalization_rejects_missing_ambiguous_or_inconsistent_usage(usage):
    record = _openai_record().model_copy(
        update={
            # Bypass Pydantic's int coercion so the normalizer sees malformed
            # provider evidence exactly as received.
            "response": ModelResponse.model_construct(
                response_normalized="{}",
                latency_ms=100,
                token_usage=usage,
            )
        }
    )
    with pytest.raises(ValueError):
        normalize_tokens(record)


def test_repeated_logical_hash_is_one_recorded_request_and_visible_cache_reuse(tmp_path):
    _record(tmp_path, _openai_record(HASH_A, attempts=3))
    _record(tmp_path, _openai_record(HASH_B, attempts=None))

    sample = aggregate_sample(
        SampleTrace(sample_id="case-1", request_hashes=(HASH_A, HASH_A, HASH_B)),
        tmp_path,
        expected_model_snapshot=GPT_56,
    )

    assert sample.logical_requests == 3
    assert sample.recorded_requests == 2
    assert sample.session_cache_reuses == 1
    assert sample.recorded_request_hashes == (HASH_A, HASH_B)
    assert sample.recorded_request_latencies_ms == (1200, 1200)
    assert sample.recorded_latency_ms == 2400
    assert sample.tokens.reported_total_tokens == 240
    assert sample.known_transport_attempts == 3
    assert sample.known_transport_retries == 2
    assert sample.unknown_transport_attempt_records == 1


def test_sample_fails_closed_on_missing_cassette_or_model_mismatch(tmp_path):
    with pytest.raises(ValueError, match="missing cassette"):
        aggregate_sample(
            SampleTrace(sample_id="missing", request_hashes=(HASH_A,)),
            tmp_path,
            expected_model_snapshot=GPT_56,
        )
    _record(tmp_path, _anthropic_record(HASH_A))
    with pytest.raises(ValueError, match="model snapshot"):
        aggregate_sample(
            SampleTrace(sample_id="wrong-model", request_hashes=(HASH_A,)),
            tmp_path,
            expected_model_snapshot=GPT_56,
        )


def test_sample_fails_closed_on_coercible_usage_and_missing_latency(tmp_path):
    _record(tmp_path, _openai_record())
    cassette_path = tmp_path / f"{HASH_A.removeprefix('sha256:')}.json"
    payload = json.loads(cassette_path.read_text(encoding="utf-8"))
    payload["response"]["token_usage"]["input_tokens"] = True
    cassette_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="token usage values must be integers"):
        aggregate_sample(
            SampleTrace(sample_id="bad-usage", request_hashes=(HASH_A,)),
            tmp_path,
            expected_model_snapshot=GPT_56,
        )

    zero_latency = _openai_record().model_copy(
        update={
            "response": _openai_record().response.model_copy(
                update={"latency_ms": 0}
            )
        }
    )
    _record(tmp_path, zero_latency)
    with pytest.raises(ValueError, match="positive record-time latency"):
        aggregate_sample(
            SampleTrace(sample_id="missing-latency", request_hashes=(HASH_A,)),
            tmp_path,
            expected_model_snapshot=GPT_56,
        )


def test_workload_uses_sample_tokens_and_record_latencies_with_frozen_statistics(tmp_path):
    _record(tmp_path, _openai_record(HASH_A, attempts=1))
    second = _openai_record(HASH_B, attempts=None).model_copy(
        update={
            "response": _openai_record(HASH_B).response.model_copy(
                update={
                    "latency_ms": 800,
                    "token_usage": {
                        "input_tokens": 50,
                        "output_tokens": 10,
                        "total_tokens": 60,
                    },
                    "raw_response": {
                        "usage": {
                            "input_tokens": 50,
                            "output_tokens": 10,
                            "total_tokens": 60,
                            "input_tokens_details": {
                                "cached_tokens": 0,
                                "cache_write_tokens": 0,
                            },
                        }
                    },
                }
            )
        }
    )
    _record(tmp_path, second)

    workload = aggregate_workload(
        workload_id="repair-search",
        model_snapshot=GPT_56,
        cassette_root=tmp_path,
        cassette_root_ref="cassettes/repair",
        protocol_id="repair-protocol@1",
        source_evidence_sha256="c" * 64,
        planned_n=2,
        traces=(
            SampleTrace(sample_id="case-b", request_hashes=(HASH_B,)),
            SampleTrace(sample_id="case-a", request_hashes=(HASH_A,)),
        ),
    )

    assert workload.evaluated_n == 2
    assert tuple(sample.sample_id for sample in workload.samples) == ("case-a", "case-b")
    assert workload.tokens.reported_total_tokens == 180
    assert workload.tokens_per_sample.mean == 90.0
    assert workload.tokens_per_sample.median == 90.0
    assert workload.tokens_per_sample.p95 == pytest.approx(117.0)
    assert workload.request_latency_ms.mean == 1000.0
    assert workload.request_latency_ms.median == 1000.0
    assert workload.known_transport_attempts == 1
    assert workload.unknown_transport_attempt_records == 1
    assert workload.monetary_status == "unavailable"
    assert workload.price_book_ref is None


def test_cost_evidence_round_trips_and_revalidates_against_cassette_bytes(tmp_path):
    cassette_root = tmp_path / "cassettes"
    _record(cassette_root, _openai_record())
    workload = aggregate_workload(
        workload_id="external-hed",
        model_snapshot=GPT_56,
        cassette_root=cassette_root,
        cassette_root_ref="cassettes/hed/pre-m4-1",
        protocol_id="hed-protocol@1",
        source_evidence_sha256="d" * 64,
        planned_n=1,
        traces=(SampleTrace(sample_id="case-a", request_hashes=(HASH_A,)),),
    )
    evidence = seal_agent_cost_evidence((workload,))
    path = tmp_path / "agent-cost.json"
    write_evidence(path, evidence)

    loaded = load_evidence(path)
    validate_agent_cost_evidence(
        loaded,
        repo_root=tmp_path,
        cassette_roots={"cassettes/hed/pre-m4-1": cassette_root},
    )

    assert loaded == evidence
    assert path.read_bytes() == canonical_evidence_bytes(evidence)


def test_cost_evidence_detects_metric_hash_and_cassette_tampering(tmp_path):
    cassette_root = tmp_path / "cassettes"
    _record(cassette_root, _openai_record())
    workload = aggregate_workload(
        workload_id="external-hed",
        model_snapshot=GPT_56,
        cassette_root=cassette_root,
        cassette_root_ref="cassettes/hed/pre-m4-1",
        protocol_id="hed-protocol@1",
        source_evidence_sha256="d" * 64,
        planned_n=1,
        traces=(SampleTrace(sample_id="case-a", request_hashes=(HASH_A,)),),
    )
    evidence = seal_agent_cost_evidence((workload,))
    payload = evidence.model_dump(mode="json")
    payload["workloads"][0]["tokens"]["reported_total_tokens"] += 1
    with pytest.raises(ValidationError):
        AgentCostLatencyEvidence.model_validate(payload)

    cassette_path = cassette_root / f"{HASH_A.removeprefix('sha256:')}.json"
    raw = json.loads(cassette_path.read_text(encoding="utf-8"))
    raw["response"]["latency_ms"] = 999
    cassette_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="cassette bytes"):
        validate_agent_cost_evidence(
            evidence,
            repo_root=tmp_path,
            cassette_roots={"cassettes/hed/pre-m4-1": cassette_root},
        )


def test_cost_evidence_rejects_path_traversal_and_unsorted_workloads():
    with pytest.raises(ValidationError):
        SampleTrace(sample_id="case", request_hashes=("not-a-hash",))

    with pytest.raises(ValueError, match="normalized"):
        aggregate_workload(
            workload_id="bad",
            model_snapshot=GPT_56,
            cassette_root=".",
            cassette_root_ref="../cassettes",
            protocol_id="protocol@1",
            source_evidence_sha256="f" * 64,
            planned_n=1,
            traces=(),
        )
