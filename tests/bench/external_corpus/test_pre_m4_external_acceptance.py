from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from gameforge.bench.external_corpus.adjudication import adjudicate
from gameforge.bench.external_corpus.contracts import (
    AdjudicationEvidence,
    B0ADecision,
    CandidateLedger,
    DiscoveryLedger,
    EvidenceArtifact,
    ReviewPackage,
    canonical_bytes,
    read_regular_file,
    sha256_hex,
)
from gameforge.bench.external_corpus.discovery import verify_discovery_direct_matches
from gameforge.bench.external_corpus.profiles import get_profile_binding
from gameforge.bench.flare_adjudication import adjudicate as adjudicate_flare
from gameforge.bench.flare_mining import main as flare_mining_main
from tests.bench.external_corpus.adjudication_fixture import (
    discovery_ledger,
    reviewed_evidence,
    write_cas,
)


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_corpus/endless_sky"
REGISTRATION_PATH = CORPUS / "source-profile.json"
DISCOVERY_PATH = CORPUS / "candidate-ledger.discovered.json"
REVIEW_PATH = CORPUS / "review-package.json"
EVIDENCE_PATH = CORPUS / "adjudication-evidence.json"
CANDIDATE_LEDGER_PATH = CORPUS / "candidate-ledger.json"
DECISION_PATH = CORPUS / "b0a-decision.json"
FLARE_FREEZE_COMMIT = "755fe2e"
EXPECTED_UNIVERSE = "f22981b17b43e02caaa494193e6a4b8cd92bbc0c312f9d5f1db249da7365793f"
FINAL_ARTIFACTS = (EVIDENCE_PATH, CANDIDATE_LEDGER_PATH, DECISION_PATH)
STATUS_DOCS = (
    ROOT / "CLAUDE.md",
    ROOT / "README.md",
    ROOT / "docs/superpowers/plans/README.md",
)


def _load_canonical(path: Path, model_type):
    raw = path.read_bytes()
    model = model_type.model_validate_json(raw)
    assert raw == canonical_bytes(model)
    return model


def _last_changed_commit(path: Path) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "log",
            "--format=%H",
            "-1",
            "--",
            str(path.relative_to(ROOT)),
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    commit = completed.stdout.strip()
    assert len(commit) == 40
    return commit


def _verify_cas_blob(corpus: Path, blob_path: str, digest: str, label: str) -> None:
    blob = read_regular_file(corpus / blob_path)
    assert sha256_hex(blob) == digest, f"{label} CAS blob does not match {digest}"


def _derived_b0a_status(corpus: Path = CORPUS) -> str:
    evidence_path = corpus / "adjudication-evidence.json"
    candidate_ledger_path = corpus / "candidate-ledger.json"
    decision_path = corpus / "b0a-decision.json"
    final_artifacts = (evidence_path, candidate_ledger_path, decision_path)
    present = tuple(path.exists() for path in final_artifacts)
    if not any(present):
        return "awaiting_human_evidence"
    assert all(present), "reviewed B0A artifacts must publish as one complete set"

    discovery = _load_canonical(
        corpus / "candidate-ledger.discovered.json",
        DiscoveryLedger,
    )
    evidence = _load_canonical(evidence_path, AdjudicationEvidence)
    for candidate in discovery.discovered_candidates:
        diff = candidate.diff_evidence
        _verify_cas_blob(corpus, diff.patch_blob, diff.patch_sha256, "candidate")
        if diff.eligible_patch_sha256 is not None:
            assert diff.eligible_patch_blob is not None
            _verify_cas_blob(
                corpus,
                diff.eligible_patch_blob,
                diff.eligible_patch_sha256,
                "eligible",
            )
    for artifact in evidence.source_artifacts:
        _verify_cas_blob(corpus, artifact.blob_path, artifact.blob_sha256, "source")
    verify_discovery_direct_matches(corpus / "blobs", discovery)

    replayed_ledger, replayed_decision = adjudicate(discovery, evidence)
    candidate_ledger = _load_canonical(candidate_ledger_path, CandidateLedger)
    decision = _load_canonical(decision_path, B0ADecision)
    assert read_regular_file(candidate_ledger_path) == canonical_bytes(replayed_ledger)
    assert read_regular_file(decision_path) == canonical_bytes(replayed_decision)
    assert decision.source_id == discovery.source_id
    assert decision.gate == candidate_ledger.gate_summary
    assert decision.candidate_ledger_sha256 == sha256_hex(canonical_bytes(candidate_ledger))
    return decision.gate.status


def _write_complete_fixture(
    corpus: Path,
    *,
    forge_direct_match: bool = False,
) -> dict[str, str]:
    corpus.mkdir()
    blob_dir = corpus / "blobs"
    discovery = discovery_ledger()
    if forge_direct_match:
        payload = discovery.model_dump(mode="json")
        candidate = next(
            item
            for item in payload["discovered_candidates"]
            if item["diff_evidence"]["eligible_patch_sha256"] is not None
        )
        direct = next(
            reason
            for reason in candidate["selection_reasons"]
            if reason["kind"] == "direct_match"
        )
        direct["rule_ids"] = ["diff.eligible_marker"]
        discovery = DiscoveryLedger.model_validate(payload)
    write_cas(discovery, blob_dir)

    source_bytes = b'{"fixture":"issue"}\n'
    source_digest = sha256_hex(source_bytes)
    source_artifact = EvidenceArtifact(
        artifact_id="issue.fixture",
        artifact_type="issue",
        source_url="https://example.test/issues/1",
        retrieval_date=date(2026, 7, 11),
        blob_path=f"blobs/{source_digest}",
        blob_sha256=source_digest,
    )
    (blob_dir / source_digest).write_bytes(source_bytes)
    evidence = reviewed_evidence(discovery, source_artifacts=[source_artifact])
    ledger, decision = adjudicate(discovery, evidence)

    (corpus / "candidate-ledger.discovered.json").write_bytes(canonical_bytes(discovery))
    (corpus / "adjudication-evidence.json").write_bytes(canonical_bytes(evidence))
    (corpus / "candidate-ledger.json").write_bytes(canonical_bytes(ledger))
    (corpus / "b0a-decision.json").write_bytes(canonical_bytes(decision))
    eligible_digest = next(
        candidate.diff_evidence.eligible_patch_sha256
        for candidate in discovery.discovered_candidates
        if candidate.diff_evidence.eligible_patch_sha256 is not None
    )
    return {
        "candidate": discovery.discovered_candidates[1].diff_evidence.patch_sha256,
        "eligible": eligible_digest,
        "source": source_digest,
    }


def test_complete_final_artifacts_replay_to_the_published_gate(tmp_path) -> None:
    corpus = tmp_path / "corpus"
    _write_complete_fixture(corpus)

    assert _derived_b0a_status(corpus) == "pass"


def test_complete_final_artifacts_replay_registered_discovery_rules(tmp_path) -> None:
    corpus = tmp_path / "corpus"
    _write_complete_fixture(corpus, forge_direct_match=True)

    with pytest.raises(ValueError, match="direct-match replay"):
        _derived_b0a_status(corpus)


@pytest.mark.parametrize("blob_kind", ["candidate", "eligible", "source"])
def test_complete_final_artifacts_reject_every_tampered_cas_kind(
    tmp_path, blob_kind
) -> None:
    corpus = tmp_path / "corpus"
    digests = _write_complete_fixture(corpus)
    (corpus / "blobs" / digests[blob_kind]).write_bytes(b"tampered\n")

    with pytest.raises(AssertionError, match=f"{blob_kind} CAS"):
        _derived_b0a_status(corpus)


def test_pre_m4_external_evidence_engineering_boundary_is_accepted() -> None:
    discovery = _load_canonical(DISCOVERY_PATH, DiscoveryLedger)
    review = _load_canonical(REVIEW_PATH, ReviewPackage)

    assert get_profile_binding("flare").source_id == "flare"
    assert get_profile_binding("endless_sky").source_id == "endless_sky"
    assert callable(adjudicate_flare)
    assert callable(flare_mining_main)

    assert discovery.observed_history_count == 2508
    assert discovery.matched_candidate_count == 610
    assert discovery.config_only_candidate_count == 562
    assert len(discovery.discovered_candidates) == 80
    assert sum(candidate.config_only for candidate in discovery.discovered_candidates) == 75
    assert discovery.candidate_universe_sha256 == EXPECTED_UNIVERSE
    assert review.candidate_universe_sha256 == EXPECTED_UNIVERSE
    assert [row.commit.commit_oid for row in review.rows] == [
        candidate.commit.commit_oid for candidate in discovery.discovered_candidates
    ]

    registration_commit = discovery.search_registration.project_commit_oid
    discovery_commit = _last_changed_commit(DISCOVERY_PATH)
    assert registration_commit != discovery_commit
    subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "merge-base",
            "--is-ancestor",
            registration_commit,
            discovery_commit,
        ],
        check=True,
    )
    registered_profile = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "show",
            f"{registration_commit}:{REGISTRATION_PATH.relative_to(ROOT)}",
        ],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    assert registered_profile == REGISTRATION_PATH.read_bytes()
    subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "diff",
            "--quiet",
            registration_commit,
            discovery_commit,
            "--",
            "gameforge/bench/external_corpus",
            "gameforge/bench/flare_evidence.py",
            "gameforge/bench/flare_git.py",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "diff",
            "--quiet",
            FLARE_FREEZE_COMMIT,
            "--",
            "scenarios/flare_corpus",
        ],
        check=True,
    )


def test_pre_m4_status_anchor_is_derived_from_complete_artifacts() -> None:
    status = _derived_b0a_status()
    assert status in {"awaiting_human_evidence", "pass", "insufficient_evidence"}

    for path in STATUS_DOCS:
        text = path.read_text(encoding="utf-8")
        assert EXPECTED_UNIVERSE in text
        assert f"`{status}`" in text

    if status == "awaiting_human_evidence":
        assert not any(path.exists() for path in FINAL_ARTIFACTS)
