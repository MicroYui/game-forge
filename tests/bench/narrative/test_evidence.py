from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.bench.narrative.evidence import (
    NarrativeClassMetric,
    NarrativeEvidenceManifest,
    canonical_evidence_bytes,
    seal_evidence_manifest,
    validate_evidence_manifest,
)
from gameforge.bench.narrative.generator import generate_case
from gameforge.bench.narrative.score import score_case, score_outcomes
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.agent_io import ConsistencyHint
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.spine.stats import wilson_ci

_PROTOCOL_SHA = "a" * 64
_CORPUS_SHA = "b" * 64
_SNAPSHOT = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="pre-m4@1",
)


def _cases():
    positive = generate_case(
        split="verification",
        defect_class=DefectClass.spoiler,
        is_clean=False,
        seed=41,
        case_id="evidence-positive",
    )
    clean = generate_case(
        split="verification",
        defect_class=DefectClass.spoiler,
        is_clean=True,
        seed=42,
        case_id="evidence-clean",
    )
    return tuple(sorted((positive, clean), key=lambda item: item.case_id))


def _correct_hint(case) -> ConsistencyHint:
    assert case.target_span is not None and case.defect_class is not None
    return ConsistencyHint(
        defect_class=case.defect_class.value,
        entity_ids=list(case.target_entities),
        constraint_ids=list(case.target_constraint_ids),
        span=case.target_span.text,
        rationale="The identity appears before its permitted story stage.",
    )


def _manifest():
    cases = _cases()
    outcomes = tuple(
        score_case(
            case,
            [] if case.is_clean else [_correct_hint(case)],
            protocol_sha256=_PROTOCOL_SHA,
            request_hashes=(f"sha256:{index:064x}",),
        )
        for index, case in enumerate(cases, start=1)
    )
    score = score_outcomes(outcomes, cases)
    manifest = seal_evidence_manifest(
        split="verification",
        protocol_sha256=_PROTOCOL_SHA,
        corpus_manifest_sha256=_CORPUS_SHA,
        model_snapshot=_SNAPSHOT,
        outcomes=outcomes,
        by_class=score.by_class,
        clean_fp=score.clean_fp,
    )
    return cases, manifest


def test_evidence_manifest_round_trips_and_recomputes_every_metric():
    cases, manifest = _manifest()
    restored = NarrativeEvidenceManifest.model_validate_json(
        canonical_evidence_bytes(manifest)
    )

    assert restored == manifest
    validate_evidence_manifest(
        restored,
        cases,
        corpus_manifest_sha256=_CORPUS_SHA,
        protocol_sha256=_PROTOCOL_SHA,
        protocol_model_snapshot=_SNAPSHOT,
    )


def test_manifest_rejects_missing_or_duplicate_frozen_cases():
    cases, manifest = _manifest()
    missing = manifest.model_copy(update={"outcomes": manifest.outcomes[:-1]})
    duplicate = manifest.model_copy(
        update={"outcomes": (manifest.outcomes[0], manifest.outcomes[0])}
    )

    with pytest.raises(ValueError, match="denominator"):
        validate_evidence_manifest(
            missing,
            cases,
            corpus_manifest_sha256=_CORPUS_SHA,
            protocol_sha256=_PROTOCOL_SHA,
            protocol_model_snapshot=_SNAPSHOT,
        )
    with pytest.raises(ValueError, match="duplicate"):
        validate_evidence_manifest(
            duplicate,
            cases,
            corpus_manifest_sha256=_CORPUS_SHA,
            protocol_sha256=_PROTOCOL_SHA,
            protocol_model_snapshot=_SNAPSHOT,
        )


def test_manifest_rejects_tampered_derived_metrics():
    cases, manifest = _manifest()
    metric = manifest.by_class[0]
    low, high = wilson_ci(0, metric.n)
    tampered_metric = NarrativeClassMetric(
        defect_class=metric.defect_class,
        split=metric.split,
        n=metric.n,
        k=0,
        rate=0.0,
        ci_low=low,
        ci_high=high,
    )
    tampered = manifest.model_copy(update={"by_class": (tampered_metric,)})

    with pytest.raises(ValueError, match="derived metrics"):
        validate_evidence_manifest(
            tampered,
            cases,
            corpus_manifest_sha256=_CORPUS_SHA,
            protocol_sha256=_PROTOCOL_SHA,
            protocol_model_snapshot=_SNAPSHOT,
        )


def test_manifest_rejects_model_protocol_or_corpus_mismatch():
    cases, manifest = _manifest()
    wrong_snapshot = ModelSnapshot(
        provider="anthropic",
        model="claude-opus-4-8",
        snapshot_tag="m2a@1",
    )

    with pytest.raises(ValueError, match="model snapshot"):
        validate_evidence_manifest(
            manifest,
            cases,
            corpus_manifest_sha256=_CORPUS_SHA,
            protocol_sha256=_PROTOCOL_SHA,
            protocol_model_snapshot=wrong_snapshot,
        )
    with pytest.raises(ValueError, match="protocol_sha256"):
        validate_evidence_manifest(
            manifest,
            cases,
            corpus_manifest_sha256=_CORPUS_SHA,
            protocol_sha256="c" * 64,
            protocol_model_snapshot=_SNAPSHOT,
        )
    with pytest.raises(ValueError, match="corpus_manifest_sha256"):
        validate_evidence_manifest(
            manifest,
            cases,
            corpus_manifest_sha256="d" * 64,
            protocol_sha256=_PROTOCOL_SHA,
            protocol_model_snapshot=_SNAPSHOT,
        )


def test_outcome_and_manifest_self_hashes_reject_field_tampering():
    _, manifest = _manifest()
    payload = manifest.model_dump(mode="json")
    payload["outcomes"][0]["detected"] = not payload["outcomes"][0]["detected"]

    with pytest.raises(ValidationError, match="outcome_sha256|evidence_sha256"):
        NarrativeEvidenceManifest.model_validate(payload)
