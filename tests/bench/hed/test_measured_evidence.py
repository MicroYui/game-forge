from __future__ import annotations

from pathlib import Path

from gameforge.bench.external_cases.endless_sky_hed import (
    load_endless_sky_hed_inputs,
)
from gameforge.bench.external_cases.qualify import load_manifest
from gameforge.bench.hed.contracts import (
    content_sha256,
    load_evidence,
    validate_evidence_manifest,
)
from gameforge.bench.hed.harness import validate_hed_evidence
from gameforge.bench.hed.protocol import assert_protocol_ready, load_protocol
from gameforge.contracts.cassette import CassetteRecord
from gameforge.contracts.model_router import ModelSnapshot

_ROOT = Path("scenarios/external_cases/endless_sky")
_EXTERNAL = _ROOT / "external-corpus-manifest.json"
_PROTOCOL = _ROOT / "hed-protocol.json"
_EVIDENCE = _ROOT / "hed-evidence.json"
_CASSETTES = Path("cassettes/hed/pre-m4-1")
_GPT56 = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="pre-m4@1",
)
_EXTERNAL_SHA256 = (
    "4eb8c8fbe872228f33919ff9ea92e8b45350dffa6b0afca5ec97dc258c59306c"
)
_PROTOCOL_SHA256 = (
    "9d5e90669f19fd4899dd8e16758a9f32cc02d77a46b84dbe228dedcb2e2f035c"
)
_EVIDENCE_SHA256 = (
    "15c6ae78b78fc098c5114e1916b24b9a524eaf5107dd5ad7e4e485c3e4287b62"
)


def _frozen_inputs():
    external = load_manifest(_EXTERNAL)
    protocol = load_protocol(_PROTOCOL)
    evidence = load_evidence(_EVIDENCE)
    cases = load_endless_sky_hed_inputs(_ROOT, external)
    return external, protocol, evidence, cases


def _cassette_records() -> dict[str, CassetteRecord]:
    return {
        record.request_hash: record
        for path in sorted(_CASSETTES.glob("*.json"))
        if (
            record := CassetteRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        )
    }


def test_measured_hed_evidence_is_complete_and_rederivable():
    external, protocol, evidence, cases = _frozen_inputs()

    assert protocol.model_snapshot == _GPT56
    assert evidence.model_snapshot == _GPT56
    assert external.manifest_sha256 == _EXTERNAL_SHA256
    assert protocol.protocol_sha256 == _PROTOCOL_SHA256
    assert evidence.evidence_sha256 == _EVIDENCE_SHA256
    assert len(evidence.outcomes) == 8
    assert {item.case_id for item in evidence.outcomes} == {
        item.spec.case_id for item in external.cases
    }
    assert evidence.metric.planned_n == 8
    assert evidence.metric.evaluated_n == 8
    assert evidence.metric.protocol_failure_count == 0
    assert all(item.human_delta for item in evidence.outcomes)
    assert evidence.metric.model_dump() == {
        "planned_n": 8,
        "evaluated_n": 8,
        "mean_normalized_distance": 0.9067708333333333,
        "median_normalized_distance": 0.96875,
        "primary_estimate": 0.9067708333333333,
        "ci_low": 0.8177083333333334,
        "ci_high": 0.9796875,
        "ci_method": "percentile-bootstrap95",
        "mean_raw_distance": 9.375,
        "median_raw_distance": 4.0,
        "unchanged_count": 0,
        "edited_count": 6,
        "unusable_count": 2,
        "protocol_failure_count": 0,
    }

    assert_protocol_ready(protocol, external, manifest_path=_EXTERNAL)
    validate_evidence_manifest(
        evidence,
        protocol=protocol,
        external_manifest=external,
    )
    validate_hed_evidence(evidence, cases, protocol, external)


def test_every_hash_bound_patch_outcome_and_manifest_recomputes():
    external, protocol, evidence, _ = _frozen_inputs()
    external_by_id = {item.spec.case_id: item for item in external.cases}

    assert evidence.protocol_sha256 == protocol.protocol_sha256
    assert evidence.external_manifest_sha256 == external.manifest_sha256
    assert evidence.evidence_sha256 == content_sha256(
        evidence,
        exclude={"evidence_sha256"},
    )
    for outcome in evidence.outcomes:
        assert (
            outcome.external_case_evidence_sha256
            == external_by_id[outcome.case_id].evidence_sha256
        )
        assert outcome.outcome_sha256 == content_sha256(
            outcome,
            exclude={"outcome_sha256"},
        )
        assert outcome.patch is not None
        assert outcome.patch_sha256 == content_sha256(outcome.patch)


def test_every_request_resolves_to_a_dedicated_gpt56_repair_cassette():
    _, _, evidence, _ = _frozen_inputs()
    referenced = {
        request_hash
        for outcome in evidence.outcomes
        for request_hash in outcome.request_hashes
    }
    records = _cassette_records()

    assert referenced
    assert len(referenced) <= 32
    assert len(referenced) == 10
    assert sum(len(item.request_hashes) for item in evidence.outcomes) == 14
    assert referenced == set(records)
    for request_hash in referenced:
        record = records[request_hash]
        assert record.agent_node_id == "repair"
        assert record.model_snapshot == _GPT56
