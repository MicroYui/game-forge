import copy
import os
import platform
import shutil
import subprocess
import unicodedata

import pytest

import gameforge.bench.flare_git as flare_git
from gameforge.bench.flare_evidence import (
    DiscoveryLedger,
    GIT_COMMON_PREFIX,
    GIT_FIXED_ENVIRONMENT,
    SearchRegistration,
    canonical_bytes,
    posix_glob_matches,
    sha256_hex,
)
from gameforge.bench.flare_git import (
    GitEvidenceError,
    ReadOnlyGitRepo,
    discover_candidates,
)


def test_provenance_git_commands_stay_out_of_production_surface():
    assert not hasattr(flare_git, "verify_search_registration")


def _run_provenance_git(repo_path, *args, allowed_returncodes=(0,)):
    prefix = [str(repo_path) if token == "{repo}" else token for token in GIT_COMMON_PREFIX]
    completed = subprocess.run(
        [*prefix, *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.environ["PATH"], **GIT_FIXED_ENVIRONMENT},
        shell=False,
    )
    if completed.returncode not in allowed_returncodes:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise AssertionError(f"provenance fixture Git command failed: {stderr}")
    return completed


def _verify_registered_search_provenance(repo_path, spec, registration, result_commit):
    registration_commit = registration.project_commit_oid
    assert registration_commit != result_commit, (
        "search registration commit must predate and be an ancestor of the result commit"
    )
    ancestry = _run_provenance_git(
        repo_path,
        "merge-base",
        "--is-ancestor",
        registration_commit,
        result_commit,
        allowed_returncodes=(0, 1),
    )
    assert ancestry.returncode == 0, (
        "search registration commit must predate and be an ancestor of the result commit"
    )
    registered = _run_provenance_git(
        repo_path,
        "cat-file",
        "blob",
        f"{registration_commit}:{registration.repo_relative_path}",
    )
    assert registered.stdout == canonical_bytes(spec), (
        "registered canonical search spec bytes differ from the supplied search spec"
    )


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
        (item.commit.committed_at, item.commit.commit_oid) for item in first.discovered_candidates
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


def _blob_bytes(blob_dir):
    return {path.name: path.read_bytes() for path in sorted(blob_dir.iterdir())}


def _discover_with_blobs(repo, search_spec, search_registration, root):
    blob_dir = root / "blobs"
    ledger = discover_candidates(
        repo,
        search_spec,
        search_registration,
        "expanded",
        blob_dir,
    )
    return canonical_bytes(ledger), _blob_bytes(blob_dir)


def test_untracked_worktree_gitattributes_cannot_rebind_discovery_bytes(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    clean = _discover_with_blobs(repo, search_spec, search_registration, tmp_path / "clean")
    (flare_git_repo.path / ".gitattributes").write_text("*.txt -diff\n", encoding="utf-8")
    polluted = _discover_with_blobs(repo, search_spec, search_registration, tmp_path / "untracked")
    assert polluted == clean


def test_staged_worktree_gitattributes_cannot_rebind_discovery_bytes(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    clean = _discover_with_blobs(repo, search_spec, search_registration, tmp_path / "clean")
    (flare_git_repo.path / ".gitattributes").write_text("*.txt -diff\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "add", ".gitattributes"],
        check=True,
    )
    polluted = _discover_with_blobs(repo, search_spec, search_registration, tmp_path / "staged")
    assert polluted == clean


def test_git_evidence_commands_use_git_directory_and_bare_repo_is_unchanged(
    flare_git_repo, tmp_path, monkeypatch
):
    real_run = subprocess.run
    commands = []

    def recording_run(args, **kwargs):
        commands.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    assert repo.resolve(flare_git_repo.head) == flare_git_repo.head
    assert commands[-1][commands[-1].index("-C") + 1] == str(flare_git_repo.git_dir.resolve())

    bare_path = tmp_path / "flare.git"
    shutil.copytree(flare_git_repo.git_dir, bare_path)
    bare = ReadOnlyGitRepo(bare_path)
    assert bare.resolve(flare_git_repo.head) == flare_git_repo.head
    assert commands[-1][commands[-1].index("-C") + 1] == str(bare_path.resolve())


def test_discovery_records_regex_runtime_provenance(expanded_discovery):
    assert expanded_discovery.discovery_tool.python_implementation == (
        platform.python_implementation()
    )
    assert expanded_discovery.discovery_tool.python_version == platform.python_version()
    assert expanded_discovery.discovery_tool.python_build == platform.python_build()
    assert expanded_discovery.discovery_tool.unicode_version == unicodedata.unidata_version


def test_discovery_tool_commit_must_match_search_registration(expanded_discovery):
    payload = expanded_discovery.model_dump(mode="json", exclude_none=True)
    assert (
        payload["discovery_tool"]["project_commit_oid"]
        == payload["search_registration"]["project_commit_oid"]
    )
    payload["discovery_tool"]["project_commit_oid"] = "f" * 40

    with pytest.raises(ValueError, match="discovery tool commit.*search registration"):
        DiscoveryLedger.model_validate(payload)


def test_discovery_ledger_requires_registered_tool_version(expanded_discovery):
    payload = expanded_discovery.model_dump(mode="json", exclude_none=True)
    payload["discovery_tool"]["tool_version"] = "foreign-flare-discovery@1"

    with pytest.raises(ValueError, match="tool_version"):
        DiscoveryLedger.model_validate(payload)


def _rebind_candidate_universe(payload):
    universe = {
        "schema_version": payload["schema_version"],
        "search_spec_sha256": payload["search_spec_sha256"],
        "search_round": payload["search_round"],
        "discovered_candidates": payload["discovered_candidates"],
        "objective_lineage_links": payload["objective_lineage_links"],
    }
    payload["candidate_universe_sha256"] = sha256_hex(canonical_bytes(universe))
    return payload


def _payload(discovery):
    return discovery.model_dump(mode="json", exclude_none=True)


def _validate_rebound(payload):
    return DiscoveryLedger.model_validate(_rebind_candidate_universe(payload))


def _link_sort_key(link):
    return (
        link["link_type"],
        link["source_oid"],
        link["target_oid"],
        link.get("rule_id", ""),
        link.get("patch_id", ""),
        link["link_id"],
    )


def _semantic_link_id(link):
    fields = {
        "link_type": link["link_type"],
        "source_oid": link["source_oid"],
        "target_oid": link["target_oid"],
    }
    evidence_field = "patch_id" if link["link_type"] == "patch_id" else "rule_id"
    fields[evidence_field] = link[evidence_field]
    return sha256_hex(canonical_bytes(fields))


def _replace_link(payload, index, **updates):
    link = payload["objective_lineage_links"][index]
    old_id = link["link_id"]
    link.update(updates)
    link["link_id"] = _semantic_link_id(link)
    for candidate in payload["discovered_candidates"]:
        for reason in candidate["selection_reasons"]:
            if reason.get("lineage_link_id") == old_id:
                reason["lineage_link_id"] = link["link_id"]
    payload["objective_lineage_links"].sort(key=_link_sort_key)
    return link


def _trailer_text(link_type, source_oid):
    return {
        "backport": f"Backport-of: {source_oid}",
        "cherry_pick": f"(cherry picked from commit {source_oid})",
        "revert": f"This reverts commit {source_oid}.",
    }[link_type]


def _candidate_with_reason(payload, kind):
    return next(
        candidate
        for candidate in payload["discovered_candidates"]
        if any(reason["kind"] == kind for reason in candidate["selection_reasons"])
    )


def _reason_sort_key(reason):
    return (
        {"direct_match": 0, "adjacent_context": 1, "lineage_context": 2}[reason["kind"]],
        reason.get("anchor_oid", ""),
        reason.get("lineage_link_id", ""),
        tuple(reason.get("rule_ids", [])),
    )


def test_discovery_ledger_recomputes_exact_eligible_paths(expanded_discovery):
    payload = _payload(expanded_discovery)
    candidate = next(item for item in payload["discovered_candidates"] if item["config_only"])
    candidate["eligible_paths"] = []
    candidate["config_only"] = False
    with pytest.raises(ValueError, match="eligible_paths"):
        _validate_rebound(payload)


def test_discovery_ledger_derives_config_only(expanded_discovery):
    payload = _payload(expanded_discovery)
    candidate = payload["discovered_candidates"][0]
    candidate["config_only"] = not candidate["config_only"]
    with pytest.raises(ValueError, match="config_only"):
        _validate_rebound(payload)


def test_discovery_ledger_requires_commit_oids_to_be_unique(expanded_discovery):
    payload = _payload(expanded_discovery)
    duplicate = copy.deepcopy(payload["discovered_candidates"][0])
    duplicate["commit"]["committed_at"] += 1
    payload["discovered_candidates"].append(duplicate)
    payload["discovered_candidates"].sort(
        key=lambda item: (item["commit"]["committed_at"], item["commit"]["commit_oid"])
    )

    with pytest.raises(ValueError, match="commit OIDs.*unique"):
        _validate_rebound(payload)


def test_discovery_ledger_rejects_case_fold_duplicate_paths(expanded_discovery):
    payload = _payload(expanded_discovery)
    candidate = next(item for item in payload["discovered_candidates"] if item["config_only"])
    path = candidate["changed_paths"][0]
    duplicate = path.upper()
    assert duplicate != path and posix_glob_matches(
        duplicate.lower(), payload["search_frame"]["config_path_globs"][0]
    )
    candidate["changed_paths"] = sorted([*candidate["changed_paths"], duplicate])
    candidate["eligible_paths"] = sorted([*candidate["eligible_paths"], duplicate])
    with pytest.raises(ValueError, match="case-fold"):
        _validate_rebound(payload)


def test_discovery_ledger_recomputes_lineage_link_ids(expanded_discovery):
    payload = _payload(expanded_discovery)
    link = payload["objective_lineage_links"][0]
    old_id = link["link_id"]
    link["link_id"] = "f" * 64
    for candidate in payload["discovered_candidates"]:
        for reason in candidate["selection_reasons"]:
            if reason.get("lineage_link_id") == old_id:
                reason["lineage_link_id"] = link["link_id"]
    payload["objective_lineage_links"].sort(key=_link_sort_key)
    with pytest.raises(ValueError, match="link_id"):
        _validate_rebound(payload)


@pytest.mark.parametrize("mutation", ["duplicate", "reverse"])
def test_discovery_ledger_requires_unique_ordered_selection_reasons(mutation, expanded_discovery):
    payload = _payload(expanded_discovery)
    candidate = next(
        item for item in payload["discovered_candidates"] if len(item["selection_reasons"]) > 1
    )
    if mutation == "duplicate":
        candidate["selection_reasons"].append(candidate["selection_reasons"][0])
    else:
        candidate["selection_reasons"].reverse()
    with pytest.raises(ValueError, match="selection_reasons"):
        _validate_rebound(payload)


def test_initial_direct_reason_must_use_a_selected_round_rule(initial_discovery):
    payload = _payload(initial_discovery)
    candidate = _candidate_with_reason(payload, "direct_match")
    direct = next(
        reason for reason in candidate["selection_reasons"] if reason["kind"] == "direct_match"
    )
    direct["rule_ids"] = ["expanded.message_bug_language"]
    with pytest.raises(ValueError, match="direct.*rule"):
        _validate_rebound(payload)


def test_adjacent_reason_anchor_must_be_a_direct_first_parent_neighbor(
    expanded_discovery,
):
    payload = _payload(expanded_discovery)
    candidate = _candidate_with_reason(payload, "adjacent_context")
    adjacent = next(
        reason for reason in candidate["selection_reasons"] if reason["kind"] == "adjacent_context"
    )
    adjacent["anchor_oid"] = "f" * 40
    candidate["selection_reasons"].sort(key=_reason_sort_key)
    with pytest.raises(ValueError, match="adjacent.*anchor"):
        _validate_rebound(payload)


def test_adjacent_reason_anchor_must_be_on_the_exact_first_parent_edge(
    expanded_discovery,
):
    payload = _payload(expanded_discovery)
    candidate = _candidate_with_reason(payload, "adjacent_context")
    adjacent = next(
        reason for reason in candidate["selection_reasons"] if reason["kind"] == "adjacent_context"
    )
    candidate_oid = candidate["commit"]["commit_oid"]
    direct = next(
        item
        for item in payload["discovered_candidates"]
        if item["commit"]["commit_oid"] != candidate_oid
        and any(reason["kind"] == "direct_match" for reason in item["selection_reasons"])
        and candidate["commit"].get("selected_parent_oid") != item["commit"]["commit_oid"]
        and item["commit"].get("selected_parent_oid") != candidate_oid
    )
    adjacent["anchor_oid"] = direct["commit"]["commit_oid"]
    candidate["selection_reasons"].sort(key=_reason_sort_key)
    with pytest.raises(ValueError, match="first-parent"):
        _validate_rebound(payload)


def test_adjacent_reason_anchor_must_share_an_exact_eligible_path(
    expanded_discovery, flare_git_repo
):
    payload = _payload(expanded_discovery)
    by_oid = {item["commit"]["commit_oid"]: item for item in payload["discovered_candidates"]}
    predecessor = by_oid[flare_git_repo.quest_fix]
    child = by_oid[flare_git_repo.multicommit_a]
    assert child["commit"]["selected_parent_oid"] == flare_git_repo.quest_fix
    assert any(reason["kind"] == "direct_match" for reason in child["selection_reasons"])
    assert set(predecessor["eligible_paths"]).isdisjoint(child["eligible_paths"])
    predecessor["selection_reasons"].append(
        {"kind": "adjacent_context", "rule_ids": [], "anchor_oid": flare_git_repo.multicommit_a}
    )
    with pytest.raises(ValueError, match="adjacent.*eligible path"):
        _validate_rebound(payload)


def test_lineage_reason_must_resolve_to_a_link_sourced_by_candidate(
    expanded_discovery,
):
    payload = _payload(expanded_discovery)
    candidate = _candidate_with_reason(payload, "lineage_context")
    reason = next(
        item for item in candidate["selection_reasons"] if item["kind"] == "lineage_context"
    )
    oid = candidate["commit"]["commit_oid"]
    other = next(link for link in payload["objective_lineage_links"] if link["source_oid"] != oid)
    reason["lineage_link_id"] = other["link_id"]
    with pytest.raises(ValueError, match="lineage.*source"):
        _validate_rebound(payload)


def test_lineage_reason_cannot_use_a_patch_id_link(expanded_discovery):
    payload = _payload(expanded_discovery)
    link = next(
        item for item in payload["objective_lineage_links"] if item["link_type"] == "patch_id"
    )
    candidate = next(
        item
        for item in payload["discovered_candidates"]
        if item["commit"]["commit_oid"] == link["source_oid"]
    )
    candidate["selection_reasons"].append(
        {
            "kind": "lineage_context",
            "rule_ids": [],
            "lineage_link_id": link["link_id"],
        }
    )
    candidate["selection_reasons"].sort(key=_reason_sort_key)

    with pytest.raises(ValueError, match="lineage reason.*trailer"):
        _validate_rebound(payload)


def test_each_trailer_link_requires_its_source_lineage_reason(expanded_discovery):
    payload = _payload(expanded_discovery)
    by_oid = {item["commit"]["commit_oid"]: item for item in payload["discovered_candidates"]}
    link = next(
        item
        for item in payload["objective_lineage_links"]
        if item["link_type"] != "patch_id"
        and len(by_oid[item["source_oid"]]["selection_reasons"]) > 1
    )
    source = by_oid[link["source_oid"]]
    source["selection_reasons"] = [
        reason
        for reason in source["selection_reasons"]
        if reason.get("lineage_link_id") != link["link_id"]
    ]

    with pytest.raises(ValueError, match="trailer link.*lineage reason"):
        _validate_rebound(payload)


def test_each_frozen_trailer_match_requires_its_objective_link(expanded_discovery):
    payload = _payload(expanded_discovery)
    by_oid = {item["commit"]["commit_oid"]: item for item in payload["discovered_candidates"]}
    link = next(
        item
        for item in payload["objective_lineage_links"]
        if item["link_type"] != "patch_id"
        and len(by_oid[item["source_oid"]]["selection_reasons"]) > 1
    )
    payload["objective_lineage_links"] = [
        item for item in payload["objective_lineage_links"] if item["link_id"] != link["link_id"]
    ]
    source = by_oid[link["source_oid"]]
    source["selection_reasons"] = [
        reason
        for reason in source["selection_reasons"]
        if reason.get("lineage_link_id") != link["link_id"]
    ]

    with pytest.raises(ValueError, match="trailer match.*objective lineage link"):
        _validate_rebound(payload)


def test_trailer_link_must_match_source_in_target_full_message(expanded_discovery):
    payload = _payload(expanded_discovery)
    link = next(
        item for item in payload["objective_lineage_links"] if item["link_type"] != "patch_id"
    )
    target = next(
        item
        for item in payload["discovered_candidates"]
        if item["commit"]["commit_oid"] == link["target_oid"]
    )
    assert link["source_oid"] in target["diff_evidence"]["commit_message"]
    target["diff_evidence"]["commit_message"] = target["diff_evidence"]["commit_message"].replace(
        link["source_oid"], "f" * 40
    )

    with pytest.raises(ValueError, match="trailer link.*target commit message"):
        _validate_rebound(payload)


def test_lineage_only_candidate_components_require_a_rooted_selection_seed(
    expanded_discovery,
):
    payload = _payload(expanded_discovery)
    linked_oids = {
        oid
        for link in payload["objective_lineage_links"]
        for oid in (link["source_oid"], link["target_oid"])
    }
    anchor_oids = {
        reason["anchor_oid"]
        for candidate in payload["discovered_candidates"]
        for reason in candidate["selection_reasons"]
        if reason.get("anchor_oid") is not None
    }
    first, second = [
        candidate
        for candidate in payload["discovered_candidates"]
        if candidate["commit"]["commit_oid"] not in linked_oids | anchor_oids
    ][:2]
    rule = payload["search_frame"]["lineage_regexes"][0]

    links = []
    for source, target in ((first, second), (second, first)):
        link = {
            "link_type": rule["link_type"],
            "source_oid": source["commit"]["commit_oid"],
            "target_oid": target["commit"]["commit_oid"],
            "rule_id": rule["rule_id"],
        }
        link["link_id"] = _semantic_link_id(link)
        links.append(link)
        source["selection_reasons"] = [
            {
                "kind": "lineage_context",
                "rule_ids": [],
                "lineage_link_id": link["link_id"],
            }
        ]
        target["diff_evidence"]["commit_message"] += (
            "\n" + _trailer_text(link["link_type"], link["source_oid"]) + "\n"
        )

    payload["objective_lineage_links"].extend(links)
    payload["objective_lineage_links"].sort(key=_link_sort_key)

    with pytest.raises(ValueError, match="rooted.*selection seed"):
        _validate_rebound(payload)


def test_lineage_endpoints_must_belong_to_candidate_universe(expanded_discovery):
    payload = _payload(expanded_discovery)
    _replace_link(payload, 0, source_oid="f" * 40)
    with pytest.raises(ValueError, match="lineage endpoint"):
        _validate_rebound(payload)


@pytest.mark.parametrize("mutation", ["unknown_rule", "wrong_type"])
def test_trailer_links_must_match_frozen_lineage_rule_type(mutation, expanded_discovery):
    payload = _payload(expanded_discovery)
    index = next(
        index
        for index, link in enumerate(payload["objective_lineage_links"])
        if link["link_type"] == "backport"
    )
    updates = (
        {"rule_id": "trailer.not_registered"}
        if mutation == "unknown_rule"
        else {"link_type": "cherry_pick"}
    )
    _replace_link(payload, index, **updates)
    with pytest.raises(ValueError, match="lineage rule|link type"):
        _validate_rebound(payload)


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
    assert by_oid[flare_git_repo.multicommit_b].selection_reasons[0].kind == ("adjacent_context")
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


def test_expanded_discovery_preserves_initial_reasons_and_objective_links_byte_exactly(
    initial_discovery, expanded_discovery
):
    expanded_by_oid = {
        item.commit.commit_oid: item for item in expanded_discovery.discovered_candidates
    }
    for initial in initial_discovery.discovered_candidates:
        replayed = expanded_by_oid[initial.commit.commit_oid]
        initial_reasons = {canonical_bytes(item) for item in initial.selection_reasons}
        expanded_reasons = {canonical_bytes(item) for item in replayed.selection_reasons}
        assert initial_reasons <= expanded_reasons
    initial_links = {canonical_bytes(item) for item in initial_discovery.objective_lineage_links}
    expanded_links = {canonical_bytes(item) for item in expanded_discovery.objective_lineage_links}
    assert initial_links <= expanded_links


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


@pytest.mark.parametrize(
    ("method_name", "bad_argument_index"),
    [
        ("resolve", 0),
        ("commit_facts", 0),
        ("commit_message", 0),
        ("changed_paths", 0),
        ("changed_paths", 1),
        ("patch_bytes", 0),
        ("patch_bytes", 1),
        ("eligible_patch_bytes", 0),
        ("eligible_patch_bytes", 1),
    ],
)
def test_public_revision_arguments_reject_options_before_subprocess(
    flare_git_repo, monkeypatch, method_name, bad_argument_index
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    arguments = {
        "resolve": [flare_git_repo.head],
        "commit_facts": [flare_git_repo.head],
        "commit_message": [flare_git_repo.head],
        "changed_paths": [flare_git_repo.before_loot, flare_git_repo.loot_fix],
        "patch_bytes": [flare_git_repo.before_loot, flare_git_repo.loot_fix],
        "eligible_patch_bytes": [
            flare_git_repo.before_loot,
            flare_git_repo.loot_fix,
            ["mods/core/loot/table.txt"],
        ],
    }[method_name]
    arguments[bad_argument_index] = "--output=/tmp/flare-option-injection"
    calls = []

    def guarded_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            returncode=129,
            stdout=b"",
            stderr=b"blocked option-like revision",
        )

    monkeypatch.setattr(subprocess, "run", guarded_run)
    with pytest.raises(GitEvidenceError, match="lowercase full Git OID"):
        getattr(repo, method_name)(*arguments)
    assert calls == []


@pytest.mark.parametrize("field_name", ["pinned_head", "after_exclusive"])
def test_reachable_revision_range_rejects_options_before_subprocess(
    flare_git_repo, search_spec, monkeypatch, field_name
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    unsafe_spec = search_spec.model_copy(
        update={field_name: "--output=/tmp/flare-range-option-injection"}
    )
    calls = []

    def guarded_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            returncode=129,
            stdout=b"",
            stderr=b"blocked option-like revision range",
        )

    monkeypatch.setattr(subprocess, "run", guarded_run)
    with pytest.raises(GitEvidenceError, match="lowercase full Git OID"):
        repo.reachable_commits(unsafe_spec)
    assert calls == []


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


def test_linked_worktree_rejects_common_git_dir_info_attributes(flare_git_repo, tmp_path):
    linked_path = tmp_path / "linked-worktree"
    subprocess.run(
        [
            "git",
            "-C",
            str(flare_git_repo.path),
            "worktree",
            "add",
            "--detach",
            str(linked_path),
            flare_git_repo.head,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.environ["PATH"], **GIT_FIXED_ENVIRONMENT},
        shell=False,
    )
    common_attributes = flare_git_repo.git_dir / "info" / "attributes"
    common_attributes.write_text("*.txt -diff\n", encoding="utf-8")

    repo = ReadOnlyGitRepo(linked_path)
    assert repo.git_dir != flare_git_repo.git_dir
    assert (repo.git_dir / "commondir").is_file()
    assert not (repo.git_dir / "info" / "attributes").exists()
    with pytest.raises(GitEvidenceError, match="info/attributes"):
        repo.commit_facts(flare_git_repo.head)


def test_search_registration_provenance_reads_canonical_spec_from_registration_commit(
    search_registration_repo, search_spec
):
    registration = SearchRegistration(
        project_commit_oid=search_registration_repo.registration_commit,
        repo_relative_path=search_registration_repo.repo_relative_path,
    )
    _verify_registered_search_provenance(
        search_registration_repo.path,
        search_spec,
        registration,
        search_registration_repo.result_commit,
    )


def test_search_registration_provenance_rejects_spec_mismatch(
    search_registration_repo, search_spec
):
    registration = SearchRegistration(
        project_commit_oid=search_registration_repo.registration_commit,
        repo_relative_path=search_registration_repo.repo_relative_path,
    )
    mismatched_spec = search_spec.model_copy(
        update={"expected_revision_count": search_spec.expected_revision_count + 1}
    )
    with pytest.raises(AssertionError, match="canonical search spec"):
        _verify_registered_search_provenance(
            search_registration_repo.path,
            mismatched_spec,
            registration,
            search_registration_repo.result_commit,
        )


def test_search_registration_provenance_rejects_registration_after_result(
    search_registration_repo, search_spec
):
    late_registration = SearchRegistration(
        project_commit_oid=search_registration_repo.late_registration_commit,
        repo_relative_path=search_registration_repo.late_repo_relative_path,
    )
    with pytest.raises(AssertionError, match="must predate and be an ancestor"):
        _verify_registered_search_provenance(
            search_registration_repo.path,
            search_spec,
            late_registration,
            search_registration_repo.result_commit,
        )


def test_search_registration_provenance_requires_a_strictly_earlier_commit(
    search_registration_repo, search_spec
):
    registration = SearchRegistration(
        project_commit_oid=search_registration_repo.registration_commit,
        repo_relative_path=search_registration_repo.repo_relative_path,
    )
    with pytest.raises(AssertionError, match="must predate and be an ancestor"):
        _verify_registered_search_provenance(
            search_registration_repo.path,
            search_spec,
            registration,
            search_registration_repo.registration_commit,
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
    initial_oids = {item.commit.commit_oid for item in initial_discovery.discovered_candidates}
    expanded_oids = {item.commit.commit_oid for item in expanded_discovery.discovered_candidates}
    assert initial_oids < expanded_oids
    assert len(positive_evidence.group_decisions) == 8
    assert len(initial_insufficient_evidence.group_decisions) == 7
    assert initial_ledger.gate_summary.status == "expanded_round_required"
    assert initial_decision.candidate_ledger_sha256 == sha256_hex(canonical_bytes(initial_ledger))
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
        foreign, decision, rebound = foreign_initial_pair_factory(field, expanded_evidence)
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
        assert rebound.prior_candidate_ledger_sha256 == sha256_hex(canonical_bytes(foreign))

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
