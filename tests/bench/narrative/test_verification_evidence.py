from __future__ import annotations

from pathlib import Path

import pytest

from gameforge.bench.narrative.contracts import NARRATIVE_CLASSES
from gameforge.bench.narrative.corpus import load_cases, load_manifest
from gameforge.bench.narrative.evidence import validate_evidence_manifest
from gameforge.bench.narrative.harness import load_evidence
from gameforge.bench.narrative.protocol import load_protocol
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.spine.stats import wilson_ci

_ROOT = Path("scenarios/narrative_bench")
_CORPUS_MANIFEST_SHA256 = (
    "349d2d34d1c65c182c960fc116dea9d15bf84e644e29a5a7ac43d34f66140de2"
)
_VERIFICATION_CORPUS_SHA256 = (
    "503afa2f70ff660cb64a1110f7b1e63b5a633679334877bd709a00acef935872"
)
_PROTOCOL_SHA256 = (
    "144eb42d538a41c79405d32515fcd8aee0effb660872e44a465e0b177587b492"
)
_GPT56_SNAPSHOT = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="pre-m4@1",
)


def _frozen_inputs():
    cases = load_cases(_ROOT / "verification.jsonl")
    corpus_manifest = load_manifest(_ROOT / "corpus-manifest.json")
    protocol = load_protocol(_ROOT / "protocol.json")
    evidence = load_evidence(_ROOT / "verification-evidence.json")
    return cases, corpus_manifest, protocol, evidence


def test_verification_evidence_has_the_exact_power_complete_denominator():
    cases, corpus_manifest, protocol, evidence = _frozen_inputs()

    assert corpus_manifest.manifest_sha256 == _CORPUS_MANIFEST_SHA256
    verification_file = next(
        item for item in corpus_manifest.files if item.split == "verification"
    )
    assert verification_file.sha256 == _VERIFICATION_CORPUS_SHA256
    assert protocol.protocol_sha256 == _PROTOCOL_SHA256
    assert evidence.model_snapshot == _GPT56_SNAPSHOT
    assert evidence.split == "verification"
    assert len(evidence.outcomes) == 1_905
    assert {metric.defect_class: metric.n for metric in evidence.by_class} == {
        defect_class: 381 for defect_class in NARRATIVE_CLASSES
    }
    assert evidence.clean_fp.n == 381

    frozen_ids_and_hashes = {
        (case.case_id, case.case_sha256) for case in cases
    }
    measured_ids_and_hashes = {
        (outcome.case_id, outcome.case_sha256) for outcome in evidence.outcomes
    }
    assert measured_ids_and_hashes == frozen_ids_and_hashes

    validate_evidence_manifest(
        evidence,
        cases,
        corpus_manifest_sha256=corpus_manifest.manifest_sha256,
        protocol_sha256=protocol.protocol_sha256,
        protocol_model_snapshot=protocol.model_snapshot,
    )


def test_verification_metrics_and_wilson_intervals_rederive_from_all_outcomes():
    cases, _, _, evidence = _frozen_inputs()
    outcomes = {item.case_id: item for item in evidence.outcomes}
    metrics = {item.defect_class: item for item in evidence.by_class}

    for defect_class in NARRATIVE_CLASSES:
        denominator = tuple(
            case
            for case in cases
            if not case.is_clean and case.defect_class is defect_class
        )
        k = sum(outcomes[case.case_id].detected for case in denominator)
        low, high = wilson_ci(k, len(denominator))
        metric = metrics[defect_class]

        assert len(denominator) == 381
        assert metric.k == k
        assert metric.rate == pytest.approx(k / len(denominator), abs=1e-12)
        assert metric.ci_low == pytest.approx(low, abs=1e-12)
        assert metric.ci_high == pytest.approx(high, abs=1e-12)

    clean_cases = tuple(case for case in cases if case.is_clean)
    fp_count = sum(outcomes[case.case_id].false_positive for case in clean_cases)
    fp_low, fp_high = wilson_ci(fp_count, len(clean_cases))

    assert len(clean_cases) == 381
    assert evidence.clean_fp.count == fp_count
    assert evidence.clean_fp.rate == pytest.approx(
        fp_count / len(clean_cases),
        abs=1e-12,
    )
    assert evidence.clean_fp.ci_low == pytest.approx(fp_low, abs=1e-12)
    assert evidence.clean_fp.ci_high == pytest.approx(fp_high, abs=1e-12)
