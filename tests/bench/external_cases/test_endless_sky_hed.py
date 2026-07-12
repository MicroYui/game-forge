from __future__ import annotations

from gameforge.bench.external_cases.endless_sky_hed import (
    build_endless_sky_hed_evidence,
    load_endless_sky_hed_inputs,
    seal_endless_sky_hed_protocol,
)
from gameforge.bench.external_cases.qualify import load_manifest
from gameforge.bench.hed.contracts import canonical_evidence_bytes
from gameforge.bench.hed.harness import replay_router, run_hed_cases
from gameforge.bench.hed.protocol import load_protocol

_CORPUS = "scenarios/external_cases/endless_sky"
_MANIFEST = f"{_CORPUS}/external-corpus-manifest.json"


def test_source_composition_reconstructs_all_eight_protocol_bound_inputs():
    manifest = load_manifest(_MANIFEST)
    cases = load_endless_sky_hed_inputs(_CORPUS, manifest)

    assert len(cases) == 8
    assert tuple(item.case_id for item in cases) == tuple(
        sorted(case.spec.case_id for case in manifest.cases)
    )
    by_id = {case.spec.case_id: case for case in manifest.cases}
    for case in cases:
        assert case.external_case_evidence_sha256 == by_id[case.case_id].evidence_sha256
        assert case.target_finding.snapshot_id == case.before_snapshot.snapshot_id
        assert case.before_snapshot.snapshot_id != case.human_target_snapshot.snapshot_id


def test_protocol_seal_writes_canonical_gpt56_protocol(tmp_path):
    output = tmp_path / "hed-protocol.json"

    protocol = seal_endless_sky_hed_protocol(
        corpus_root=_CORPUS,
        manifest_path=_MANIFEST,
        output=output,
    )

    assert load_protocol(output) == protocol
    assert protocol.model_snapshot.model == "gpt-5.6-sol"
    assert protocol.external_case_count == 8


def test_empty_replay_still_builds_deterministic_eight_case_evidence(tmp_path):
    protocol_path = tmp_path / "hed-protocol.json"
    protocol = seal_endless_sky_hed_protocol(
        corpus_root=_CORPUS,
        manifest_path=_MANIFEST,
        output=protocol_path,
    )
    manifest = load_manifest(_MANIFEST)
    cases = load_endless_sky_hed_inputs(_CORPUS, manifest)
    outcomes = run_hed_cases(cases, replay_router(tmp_path / "empty"), protocol)

    first = build_endless_sky_hed_evidence(
        cases,
        outcomes,
        protocol,
        manifest,
    )
    second = build_endless_sky_hed_evidence(
        cases,
        outcomes,
        protocol,
        manifest,
    )

    assert first == second
    assert canonical_evidence_bytes(first) == canonical_evidence_bytes(second)
    assert first.metric.protocol_failure_count == 8
