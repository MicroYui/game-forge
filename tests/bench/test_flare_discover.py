import os
import subprocess

import pytest

from gameforge.bench.flare_evidence import canonical_bytes, sha256_hex
from gameforge.bench.flare_git import GitEvidenceError, ReadOnlyGitRepo, discover_candidates


def test_discover_is_byte_stable_and_keeps_non_config_candidates_for_rejection(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    first = discover_candidates(
        repo, search_spec, search_registration, "expanded", tmp_path / "a" / "blobs"
    )
    second = discover_candidates(
        repo, search_spec, search_registration, "expanded", tmp_path / "b" / "blobs"
    )
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    candidate_keys = [
        (item.commit.committed_at, item.commit.commit_oid)
        for item in first.discovered_candidates
    ]
    assert candidate_keys == sorted(candidate_keys)
    by_oid = {item.commit.commit_oid: item for item in first.discovered_candidates}
    assert by_oid[flare_git_repo.quest_fix].config_only is True
    assert by_oid[flare_git_repo.mixed_fix].config_only is False
    assert by_oid[flare_git_repo.mixed_fix].changed_paths == [
        "engine/runtime.py",
        "mods/core/quests/test.txt",
    ]
    assert flare_git_repo.localization_only not in by_oid
    assert by_oid[flare_git_repo.behavior_and_localization].config_only is False
    assert by_oid[flare_git_repo.non_utf8_binary_sibling].config_only is False
    assert flare_git_repo.engine_key_only not in by_oid
    if flare_git_repo.merge_commit in by_oid:
        assert "direct_match" not in {
            reason.kind for reason in by_oid[flare_git_repo.merge_commit].selection_reasons
        }


def test_patch_evidence_and_objective_lineage_are_offline_replayable(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    ledger = discover_candidates(
        ReadOnlyGitRepo(flare_git_repo.path),
        search_spec,
        search_registration,
        "expanded",
        tmp_path / "blobs",
    )
    for item in ledger.discovered_candidates:
        blob = tmp_path / item.diff_evidence.patch_blob
        assert blob.read_bytes()
        assert item.diff_evidence.patch_sha256 == sha256_hex(blob.read_bytes())
    link_types = {link.link_type for link in ledger.objective_lineage_links}
    assert {"patch_id", "cherry_pick", "backport", "revert"} <= link_types
    links = {
        (link.link_type, link.source_oid, link.target_oid)
        for link in ledger.objective_lineage_links
    }
    assert ("cherry_pick", flare_git_repo.loot_fix, flare_git_repo.loot_cherry_pick) in links
    assert (
        "backport",
        flare_git_repo.remote_backport_source,
        flare_git_repo.backport,
    ) in links
    assert ("revert", flare_git_repo.loot_fix, flare_git_repo.loot_revert) in links
    assert any(
        link.link_type == "patch_id"
        and {link.source_oid, link.target_oid}
        == {flare_git_repo.loot_fix, flare_git_repo.loot_cherry_pick}
        for link in ledger.objective_lineage_links
    )
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    merge = repo.commit_facts(flare_git_repo.merge_commit)
    assert merge.selected_parent_oid == merge.parent_oids[0]
    root = repo.commit_facts(flare_git_repo.root)
    assert root.diff_base_oid == flare_git_repo.empty_tree_oid


def test_direct_matches_expand_one_first_parent_edge_for_complete_grouping(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    ledger = discover_candidates(
        ReadOnlyGitRepo(flare_git_repo.path),
        search_spec,
        search_registration,
        "expanded",
        tmp_path / "blobs",
    )
    by_oid = {item.commit.commit_oid: item for item in ledger.discovered_candidates}
    assert by_oid[flare_git_repo.multicommit_a].selection_reasons[0].kind == "direct_match"
    assert by_oid[flare_git_repo.multicommit_b].selection_reasons[0].kind == (
        "adjacent_context"
    )
    assert by_oid[flare_git_repo.multicommit_c].selection_reasons[0].kind == "direct_match"
    assert by_oid[flare_git_repo.remote_backport_source].selection_reasons[0].kind == (
        "lineage_context"
    )


def test_expanded_round_is_a_superset_of_initial_under_union_semantics(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    initial = discover_candidates(
        repo,
        search_spec,
        search_registration,
        "initial",
        tmp_path / "initial" / "blobs",
    )
    expanded = discover_candidates(
        repo,
        search_spec,
        search_registration,
        "expanded",
        tmp_path / "expanded" / "blobs",
    )
    initial_oids = {item.commit.commit_oid for item in initial.discovered_candidates}
    expanded_oids = {item.commit.commit_oid for item in expanded.discovered_candidates}
    assert initial_oids <= expanded_oids


def test_discover_rejects_wrong_head_and_never_invokes_a_shell(
    flare_git_repo, search_spec, search_registration, tmp_path, monkeypatch
):
    calls = []
    real_run = subprocess.run

    def guarded_run(args, **kwargs):
        assert isinstance(args, list)
        assert kwargs.get("shell", False) is False
        calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    with pytest.raises(GitEvidenceError, match="pinned head"):
        discover_candidates(
            ReadOnlyGitRepo(flare_git_repo.path),
            search_spec.model_copy(update={"pinned_head": "f" * 40}),
            search_registration,
            "initial",
            tmp_path / "blobs",
        )
    assert calls


def test_successful_discovery_uses_only_argument_arrays(
    flare_git_repo, search_spec, search_registration, tmp_path, monkeypatch
):
    calls = []
    real_run = subprocess.run

    def guarded_run(args, **kwargs):
        assert isinstance(args, list)
        assert kwargs.get("shell", False) is False
        calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    discover_candidates(
        ReadOnlyGitRepo(flare_git_repo.path),
        search_spec,
        search_registration,
        "expanded",
        tmp_path / "blobs",
    )
    assert calls


def test_repository_git_config_and_locale_cannot_change_patch_bytes(
    flare_git_repo, search_spec, search_registration, tmp_path, monkeypatch
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    clean = discover_candidates(
        repo,
        search_spec,
        search_registration,
        "expanded",
        tmp_path / "clean" / "blobs",
    )
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "config", "color.ui", "always"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "config", "diff.noprefix", "true"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "config", "diff.algorithm", "histogram"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(flare_git_repo.path),
            "config",
            "diff.interHunkContext",
            "100",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(flare_git_repo.path),
            "config",
            "diff.suppressBlankEmpty",
            "true",
        ],
        check=True,
    )
    order_file = tmp_path / "reverse.order"
    order_file.write_text("mods/core/quests/test.txt\n*\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(flare_git_repo.path),
            "config",
            "diff.orderFile",
            str(order_file),
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(flare_git_repo.path),
            "config",
            "i18n.logOutputEncoding",
            "ISO-8859-1",
        ],
        check=True,
    )
    attributes_file = tmp_path / "global.attributes"
    attributes_file.write_text("*.txt -diff\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(flare_git_repo.path),
            "config",
            "core.attributesFile",
            str(attributes_file),
        ],
        check=True,
    )
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.setenv("GIT_DIFF_OPTS", "--unified=99")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.noprefix")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    polluted = discover_candidates(
        repo,
        search_spec,
        search_registration,
        "expanded",
        tmp_path / "polluted" / "blobs",
    )
    assert canonical_bytes(clean) == canonical_bytes(polluted)


def test_git_child_environment_is_minimal_and_drops_inherited_git_overrides(
    flare_git_repo, search_spec, search_registration, tmp_path, monkeypatch
):
    monkeypatch.setenv("GIT_DIFF_OPTS", "--unified=99")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.noprefix")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    real_run = subprocess.run
    child_environments = []

    def guarded_run(args, **kwargs):
        child_environments.append(kwargs["env"])
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    discover_candidates(
        ReadOnlyGitRepo(flare_git_repo.path),
        search_spec,
        search_registration,
        "initial",
        tmp_path / "blobs",
    )
    expected_keys = {"PATH"} | set(search_spec.git_environment_policy.fixed)
    assert child_environments
    assert all(set(env) == expected_keys for env in child_environments)
    assert all(env["PATH"] == os.environ["PATH"] for env in child_environments)
    assert all("GIT_DIFF_OPTS" not in env for env in child_environments)
    assert all("GIT_CONFIG_COUNT" not in env for env in child_environments)
    assert all("GIT_CONFIG_KEY_0" not in env for env in child_environments)
    assert all("GIT_CONFIG_VALUE_0" not in env for env in child_environments)


def test_repo_local_info_attributes_are_rejected(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    info_attributes = flare_git_repo.git_dir / "info" / "attributes"
    info_attributes.write_text("*.txt -diff\n", encoding="utf-8")
    with pytest.raises(GitEvidenceError, match="info/attributes"):
        discover_candidates(
            ReadOnlyGitRepo(flare_git_repo.path),
            search_spec,
            search_registration,
            "initial",
            tmp_path / "blobs",
        )


def test_shared_round_models_and_paths_form_complete_canonical_fixture_pairs(
    initial_discovery,
    expanded_discovery,
    positive_evidence,
    initial_insufficient_evidence,
    expanded_evidence,
    expanded_insufficient_evidence,
    initial_ledger,
    initial_decision,
    foreign_initial_pair_factory,
    blob_paths,
    initial_discovered_path,
    expanded_discovered_path,
    initial_positive_evidence_path,
    initial_insufficient_evidence_path,
    expanded_evidence_path,
    expanded_insufficient_evidence_path,
    initial_ledger_path,
    initial_decision_path,
):
    initial_oids = {
        item.commit.commit_oid for item in initial_discovery.discovered_candidates
    }
    expanded_oids = {
        item.commit.commit_oid for item in expanded_discovery.discovered_candidates
    }
    assert initial_oids < expanded_oids
    assert len(positive_evidence.group_decisions) == 8
    assert len(initial_insufficient_evidence.group_decisions) == 7
    assert initial_ledger.gate_summary.status == "expanded_round_required"
    assert initial_decision.candidate_ledger_sha256 == sha256_hex(
        canonical_bytes(initial_ledger)
    )
    assert expanded_evidence.prior_candidate_ledger_sha256 == sha256_hex(
        canonical_bytes(initial_ledger)
    )
    assert expanded_insufficient_evidence.search_round == "expanded"

    for field in (
        "search_frame",
        "search_spec_sha256",
        "search_registration",
        "observed_revision_count",
        "discovery_tool",
    ):
        foreign, decision, rebound = foreign_initial_pair_factory(
            field, expanded_evidence
        )
        changed_fields = [
            name
            for name in (
                "search_frame",
                "search_spec_sha256",
                "search_registration",
                "observed_revision_count",
                "discovery_tool",
            )
            if getattr(foreign, name) != getattr(initial_ledger, name)
        ]
        assert changed_fields == [field]
        assert decision.candidate_ledger_sha256 == sha256_hex(canonical_bytes(foreign))
        assert rebound.prior_candidate_ledger_sha256 == sha256_hex(
            canonical_bytes(foreign)
        )

    pairs = [
        (initial_discovered_path, initial_discovery),
        (expanded_discovered_path, expanded_discovery),
        (initial_positive_evidence_path, positive_evidence),
        (initial_insufficient_evidence_path, initial_insufficient_evidence),
        (expanded_evidence_path, expanded_evidence),
        (expanded_insufficient_evidence_path, expanded_insufficient_evidence),
        (initial_ledger_path, initial_ledger),
        (initial_decision_path, initial_decision),
    ]
    assert blob_paths
    assert all(path.read_bytes() == canonical_bytes(model) for path, model in pairs)
