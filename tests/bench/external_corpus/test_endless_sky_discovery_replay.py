from __future__ import annotations

import hashlib
import re
from pathlib import Path

from gameforge.bench.external_corpus.contracts import (
    DiscoveryLedger,
    ReviewPackage,
    SourceProfile,
    canonical_bytes,
)


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_corpus/endless_sky"
PROFILE_PATH = CORPUS / "source-profile.json"
LEDGER_PATH = CORPUS / "candidate-ledger.discovered.json"
REVIEW_PACKAGE_PATH = CORPUS / "review-package.json"
REGISTRATION_COMMIT = "b018283e52fe9bd879e14fb5039e601423d3f164"


def _load_canonical(path: Path, model_type):
    raw = path.read_bytes()
    model = model_type.model_validate_json(raw)
    assert raw == canonical_bytes(model)
    return model


def test_discovered_universe_replays_registered_counts_and_order() -> None:
    profile = _load_canonical(PROFILE_PATH, SourceProfile)
    ledger = _load_canonical(LEDGER_PATH, DiscoveryLedger)

    assert ledger.source_profile == profile
    assert ledger.search_registration.project_commit_oid == REGISTRATION_COMMIT
    assert (
        ledger.search_registration.profile_repo_relative_path
        == "scenarios/external_corpus/endless_sky/source-profile.json"
    )
    assert ledger.observed_history_count == 2508
    assert ledger.matched_candidate_count == 610
    assert ledger.config_only_candidate_count == 562
    assert len(ledger.discovered_candidates) == 80
    assert sum(candidate.config_only for candidate in ledger.discovered_candidates) == 75
    assert (
        ledger.discovered_candidates[0].commit.commit_oid
        == "c55df3918b9aa6052bda0aca7f6b6fe4d10a1d77"
    )


def test_discovered_universe_replays_patch_cas_and_message_matches_offline() -> None:
    ledger = _load_canonical(LEDGER_PATH, DiscoveryLedger)
    message_rules = ledger.source_profile.message_rules
    assert ledger.source_profile.diff_rules == ()

    for candidate in ledger.discovered_candidates:
        patch = (CORPUS / candidate.diff_evidence.patch_blob).read_bytes()
        assert hashlib.sha256(patch).hexdigest() == candidate.diff_evidence.patch_sha256

        expected_message_rules = {
            rule.rule_id
            for rule in message_rules
            if re.search(rule.pattern, candidate.commit.subject) is not None
        }
        observed_direct_rules = {
            rule_id
            for reason in candidate.selection_reasons
            if reason.kind == "direct_match"
            for rule_id in reason.rule_ids
        }
        assert observed_direct_rules == expected_message_rules


def test_review_package_is_complete_non_approving_and_bound_to_universe() -> None:
    ledger = _load_canonical(LEDGER_PATH, DiscoveryLedger)
    review_package = _load_canonical(REVIEW_PACKAGE_PATH, ReviewPackage)

    assert review_package.review_status == "awaiting_human"
    assert review_package.candidate_universe_sha256 == ledger.candidate_universe_sha256
    assert len(review_package.rows) == 80
    assert [row.commit.commit_oid for row in review_package.rows] == [
        candidate.commit.commit_oid for candidate in ledger.discovered_candidates
    ]
