from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from gameforge.bench.external_corpus.contracts import (
    DiscoveryLedger,
    SourceProfile,
    canonical_bytes,
)
from gameforge.bench.external_corpus.profiles import get_profile_binding
from gameforge.bench.taxonomy import DefectClass


ROOT = Path(__file__).resolve().parents[3]
REGISTRATION_DIR = ROOT / "scenarios/external_corpus/endless_sky"
PROFILE_PATH = REGISTRATION_DIR / "source-profile.json"
DISCOVERY_PATH = REGISTRATION_DIR / "candidate-ledger.discovered.json"
PINNED_HEAD = "b10b7d6c24496e2f67a230a2553b344e200ba289"
UPSTREAM_ARTIFACT_SHA256 = {
    "LICENSE.endless-sky.txt": "589ed823e9a84c56feb95ac58e7cf384626b9cbf4fda2a907bc36e103de1bad2",
    "COPYRIGHT.endless-sky": "533a3ea9aaba5dbb0dcb2279944866fd70a625c18309fbec2d09463aec6f1b19",
}


def _registration_commit() -> str:
    if DISCOVERY_PATH.exists():
        raw = DISCOVERY_PATH.read_bytes()
        ledger = DiscoveryLedger.model_validate_json(raw)
        assert raw == canonical_bytes(ledger)
        return ledger.search_registration.project_commit_oid
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def test_registered_endless_sky_profile_is_canonical_and_exact() -> None:
    raw = PROFILE_PATH.read_bytes()
    profile = SourceProfile.model_validate_json(raw)

    assert raw == canonical_bytes(profile)
    assert profile.source_id == "endless_sky"
    assert profile.profile_version == "endless-sky-b0a@1"
    assert profile.repository_url == "https://github.com/endless-sky/endless-sky.git"
    assert profile.pinned_head == PINNED_HEAD
    assert profile.history_range.committed_at_gte == 1672531200
    assert profile.history_range.expected_commit_count == 2508
    assert profile.config_include_globs == ("data/**/*.txt",)
    assert profile.config_exclude_globs == ()
    assert profile.license_id == "GPL-3.0-or-later"
    assert profile.notice_files == ("copyright", "license.txt", "credits.txt")
    assert profile.b0a_protocol.expected_matched_candidate_count == 610
    assert profile.b0a_protocol.expected_config_only_candidate_count == 562
    assert profile.b0a_protocol.candidate_limit == 80
    assert get_profile_binding("endless_sky").validate_source_profile(profile) == profile


def test_registered_profile_has_complete_preregistered_taxonomy_applicability() -> None:
    profile = SourceProfile.model_validate_json(PROFILE_PATH.read_bytes())
    rows = {row.defect_class: row for row in profile.taxonomy_applicability}
    not_applicable = {
        DefectClass.economy_collapse,
        DefectClass.gacha_expectation_violation,
        DefectClass.non_monotonic_curve,
        DefectClass.prob_sum_ne_1,
    }

    assert set(rows) == set(DefectClass)
    assert {
        defect_class
        for defect_class, row in rows.items()
        if row.domain_applicability == "not_applicable"
    } == not_applicable
    assert all(row.implementation_support == "planned" for row in rows.values())


def test_registration_freezes_exact_pinned_upstream_notice_bytes() -> None:
    for name, expected_sha256 in UPSTREAM_ARTIFACT_SHA256.items():
        assert hashlib.sha256((REGISTRATION_DIR / name).read_bytes()).hexdigest() == (
            expected_sha256
        )

    notice = (REGISTRATION_DIR / "NOTICE").read_text(encoding="utf-8")
    for required in (
        "https://github.com/endless-sky/endless-sky.git",
        PINNED_HEAD,
        "license.txt",
        "copyright",
        "credits.txt",
        "GPL-3.0-or-later",
    ):
        assert required in notice


def test_registration_commit_contains_no_discovery_or_adjudication_result() -> None:
    registration_commit = _registration_commit()
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-tree",
            "--name-only",
            f"{registration_commit}:scenarios/external_corpus/endless_sky",
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    names = set(completed.stdout.splitlines())

    assert "source-profile.json" in names
    assert "candidate-ledger.discovered.json" not in names
    assert "review-package.json" not in names
    assert "adjudication-evidence.json" not in names
    assert "candidate-ledger.json" not in names
    assert "b0a-decision.json" not in names
    assert "blobs" not in names

    profile_at_registration = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "show",
            f"{registration_commit}:scenarios/external_corpus/endless_sky/source-profile.json",
        ],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    assert profile_at_registration == PROFILE_PATH.read_bytes()
