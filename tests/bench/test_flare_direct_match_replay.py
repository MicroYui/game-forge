from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gameforge.bench.flare_evidence import (
    DiscoveryLedger,
    canonical_bytes,
    extract_eligible_patch_bytes,
    sha256_hex,
    verify_discovery_direct_matches,
)
from gameforge.bench.flare_git import ReadOnlyGitRepo


def _payload(discovery: DiscoveryLedger) -> dict:
    return discovery.model_dump(mode="json", exclude_none=True)


def _reason_sort_key(reason: dict) -> tuple[int, str, str, tuple[str, ...]]:
    return (
        {"direct_match": 0, "adjacent_context": 1, "lineage_context": 2}[
            reason["kind"]
        ],
        reason.get("anchor_oid", ""),
        reason.get("lineage_link_id", ""),
        tuple(reason.get("rule_ids", ())),
    )


def _rebind_candidate_universe(payload: dict) -> dict:
    universe = {
        "schema_version": payload["schema_version"],
        "search_spec_sha256": payload["search_spec_sha256"],
        "search_round": payload["search_round"],
        "discovered_candidates": payload["discovered_candidates"],
        "objective_lineage_links": payload["objective_lineage_links"],
    }
    payload["candidate_universe_sha256"] = sha256_hex(canonical_bytes(universe))
    return payload


def _candidate(payload: dict, oid: str) -> dict:
    return next(
        item for item in payload["discovered_candidates"] if item["commit"]["commit_oid"] == oid
    )


def _diff_direct_candidate(payload: dict) -> dict:
    return next(
        candidate
        for candidate in payload["discovered_candidates"]
        if candidate["commit"]["parent_oids"]
        and any(
            reason["kind"] == "direct_match"
            and "expanded.diff_behavior_key" in reason["rule_ids"]
            for reason in candidate["selection_reasons"]
        )
    )


def test_discovery_model_rejects_nonmatching_message_direct_reason(expanded_discovery):
    payload = _payload(expanded_discovery)
    candidate = _diff_direct_candidate(payload)
    candidate["commit"]["subject"] = "Routine content update"

    with pytest.raises(ValueError, match="direct.*message|message.*direct"):
        DiscoveryLedger.model_validate(_rebind_candidate_universe(payload))


def test_discovery_model_rejects_direct_reason_without_eligible_path(expanded_discovery):
    payload = _payload(expanded_discovery)
    referenced_anchors = {
        reason["anchor_oid"]
        for candidate in payload["discovered_candidates"]
        for reason in candidate["selection_reasons"]
        if reason["kind"] == "adjacent_context"
    }
    candidate = next(
        item
        for item in payload["discovered_candidates"]
        if item["commit"]["commit_oid"] not in referenced_anchors
        and any(reason["kind"] == "direct_match" for reason in item["selection_reasons"])
        and all(reason["kind"] == "direct_match" for reason in item["selection_reasons"])
    )
    candidate["changed_paths"] = ["engine/runtime.py"]
    candidate["eligible_paths"] = []
    candidate["config_only"] = False

    with pytest.raises(ValueError, match="direct.*eligible|eligible.*direct"):
        DiscoveryLedger.model_validate(_rebind_candidate_universe(payload))


def test_discovery_model_rejects_diff_direct_reason_for_merge(expanded_discovery):
    payload = _payload(expanded_discovery)
    candidate = _diff_direct_candidate(payload)
    second_parent = next(
        item["commit"]["commit_oid"]
        for item in payload["discovered_candidates"]
        if item["commit"]["commit_oid"] not in candidate["commit"]["parent_oids"]
        and item["commit"]["commit_oid"] != candidate["commit"]["commit_oid"]
    )
    candidate["commit"]["parent_oids"].append(second_parent)

    with pytest.raises(ValueError, match="merge.*diff|diff.*merge"):
        DiscoveryLedger.model_validate(_rebind_candidate_universe(payload))


def test_offline_replay_rejects_fabricated_diff_direct_reason(
    expanded_discovery,
    flare_git_repo,
    blob_dir,
):
    payload = _payload(expanded_discovery)
    candidate = _candidate(payload, flare_git_repo.multicommit_b)
    assert all(reason["kind"] != "direct_match" for reason in candidate["selection_reasons"])
    candidate["selection_reasons"].append(
        {"kind": "direct_match", "rule_ids": ["expanded.diff_behavior_key"]}
    )
    candidate["selection_reasons"].sort(key=_reason_sort_key)
    forged = DiscoveryLedger.model_validate(_rebind_candidate_universe(payload))

    with pytest.raises(ValueError, match="direct-match.*replay|replay.*direct-match"):
        verify_discovery_direct_matches(blob_dir, forged)


def test_offline_replay_rejects_missing_expected_diff_direct_reason(
    expanded_discovery,
    flare_git_repo,
    blob_dir,
):
    payload = _payload(expanded_discovery)
    candidate = _candidate(payload, flare_git_repo.multicommit_b)
    assert all(reason["kind"] != "direct_match" for reason in candidate["selection_reasons"])
    original_digest = candidate["diff_evidence"]["patch_sha256"]
    matching_patch = (blob_dir / original_digest).read_bytes() + b"+loot = injected\n"
    matching_digest = sha256_hex(matching_patch)
    (blob_dir / matching_digest).write_bytes(matching_patch)
    candidate["diff_evidence"]["patch_sha256"] = matching_digest
    candidate["diff_evidence"]["patch_blob"] = f"blobs/{matching_digest}"
    missing_reason = DiscoveryLedger.model_validate(_rebind_candidate_universe(payload))

    with pytest.raises(ValueError, match="direct-match.*recorded|recorded.*direct-match"):
        verify_discovery_direct_matches(blob_dir, missing_reason)


def test_offline_replay_rejects_tampered_patch_blob(expanded_discovery, blob_dir):
    digest = expanded_discovery.discovered_candidates[0].diff_evidence.patch_sha256
    (blob_dir / digest).write_bytes(b"tampered patch\n")

    with pytest.raises(ValueError, match="CAS blob.*digest|digest.*CAS blob"):
        verify_discovery_direct_matches(blob_dir, expanded_discovery)


def test_offline_patch_slicing_matches_git_eligible_path_diff(
    expanded_discovery,
    flare_git_repo,
    blob_dir,
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    for candidate in expanded_discovery.discovered_candidates:
        full_patch = (blob_dir / candidate.diff_evidence.patch_sha256).read_bytes()
        replayed = extract_eligible_patch_bytes(
            full_patch,
            changed_paths=candidate.changed_paths,
            eligible_paths=candidate.eligible_paths,
        )
        expected = (
            repo.eligible_patch_bytes(
                candidate.commit.diff_base_oid,
                candidate.commit.commit_oid,
                candidate.eligible_paths,
            )
            if candidate.eligible_paths
            else b""
        )
        assert replayed == expected


def test_offline_patch_slicing_matches_git_for_quoted_paths(tmp_path: Path):
    worktree = tmp_path / "quoted-paths"
    worktree.mkdir()

    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=worktree,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    paths = [
        "mods/space name.txt",
        "mods/tab\tname.txt",
        "mods/newline\nname.txt",
        'mods/double"quote.txt',
        "mods/unicode-白鸢.txt",
        "src/ignored.py",
    ]
    for path in paths:
        target = worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("before\n", encoding="utf-8")
    git("add", "--all")
    git("commit", "-qm", "fixture parent")
    parent = git("rev-parse", "HEAD")

    for path in paths:
        (worktree / path).write_text("after\n", encoding="utf-8")
    git("commit", "-qam", "fixture child")
    child = git("rev-parse", "HEAD")

    repo = ReadOnlyGitRepo(worktree)
    changed_paths = repo.changed_paths(parent, child)
    eligible_paths = sorted(path for path in paths if path.startswith("mods/"))
    replayed = extract_eligible_patch_bytes(
        repo.patch_bytes(parent, child),
        changed_paths=changed_paths,
        eligible_paths=eligible_paths,
    )
    assert replayed == repo.eligible_patch_bytes(parent, child, eligible_paths)


def test_offline_replay_accepts_current_discovery_fixture(expanded_discovery, blob_dir):
    verify_discovery_direct_matches(blob_dir, expanded_discovery)
