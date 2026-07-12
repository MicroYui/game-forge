from __future__ import annotations

from gameforge.agents.base import DEFAULT_SNAPSHOT
from gameforge.bench.narrative.contracts import NARRATIVE_CLASSES
from gameforge.bench.narrative.corpus import load_cases, load_manifest
from gameforge.bench.narrative.evidence import validate_evidence_manifest
from gameforge.bench.narrative.harness import load_evidence
from gameforge.bench.narrative.protocol import seal_protocol

_ROOT = "scenarios/narrative_bench"


def test_development_evidence_is_complete_replayable_and_ready_to_freeze():
    cases = load_cases(f"{_ROOT}/development.jsonl")
    corpus_manifest = load_manifest(f"{_ROOT}/corpus-manifest.json")
    protocol = seal_protocol(corpus_manifest)
    evidence = load_evidence(f"{_ROOT}/development-evidence.json")

    validate_evidence_manifest(
        evidence,
        cases,
        corpus_manifest_sha256=corpus_manifest.manifest_sha256,
        protocol_sha256=protocol.protocol_sha256,
        protocol_model_snapshot=protocol.model_snapshot,
    )
    assert evidence.model_snapshot == DEFAULT_SNAPSHOT
    assert len(evidence.outcomes) == 160
    assert {item.case_id for item in evidence.outcomes} == {
        item.case_id for item in cases
    }
    assert {metric.defect_class: metric.n for metric in evidence.by_class} == {
        defect_class: 20 for defect_class in NARRATIVE_CLASSES
    }
    assert all(metric.rate >= 0.80 for metric in evidence.by_class)
    assert evidence.clean_fp.n == 80
    assert evidence.clean_fp.rate <= 0.05
    assert all(
        outcome.status not in {"cassette_miss", "runner_error"}
        for outcome in evidence.outcomes
    )
    assert sum(len(outcome.request_hashes) for outcome in evidence.outcomes) == 480
