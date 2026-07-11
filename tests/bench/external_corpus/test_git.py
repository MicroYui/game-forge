from __future__ import annotations

import os
import subprocess

import pytest

import gameforge.bench.external_corpus.git as git_module
from gameforge.bench.external_corpus.git import GitEvidenceError, ReadOnlyGitRepo
from tests.bench.external_corpus.git_fixture import build_generic_git_repo


@pytest.fixture
def generic_git_repo(tmp_path):
    return build_generic_git_repo(tmp_path / "upstream")


def test_read_only_repo_exposes_binary_safe_commit_facts(generic_git_repo):
    repo = ReadOnlyGitRepo(generic_git_repo.path)

    metadata = repo.commit_metadata(generic_git_repo.data_fix)
    paths = repo.changed_paths(metadata.commit.diff_base_oid, generic_git_repo.data_fix)
    patch = repo.patch_bytes(metadata.commit.diff_base_oid, generic_git_repo.data_fix)

    assert metadata.commit.commit_oid == generic_git_repo.data_fix
    assert metadata.full_message.startswith("Fix mission reference")
    assert paths == ["data/missions/alpha.txt"]
    assert b"requires_status = alpha_ready" in patch
    assert repo.stable_patch_id(patch)
    assert repo.git_version().startswith("git version ")


def test_read_only_repo_exposes_current_head_and_registered_blob(generic_git_repo):
    repo = ReadOnlyGitRepo(generic_git_repo.path)

    assert repo.head_commit() == generic_git_repo.head
    assert repo.blob_bytes_at(
        generic_git_repo.data_fix,
        "data/missions/alpha.txt",
    ) == b"requires_status = alpha_ready\n"


def test_tracked_worktree_gate_ignores_untracked_files_but_rejects_tracked_changes(
    generic_git_repo,
):
    repo = ReadOnlyGitRepo(generic_git_repo.path)
    (generic_git_repo.path / "untracked-output.json").write_text("{}\n", encoding="utf-8")

    repo.assert_tracked_worktree_clean()

    (generic_git_repo.path / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(GitEvidenceError, match="tracked worktree"):
        repo.assert_tracked_worktree_clean()


def test_preflight_rejects_repository_local_attributes(generic_git_repo):
    attributes = generic_git_repo.git_dir / "info" / "attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text("*.txt -diff\n", encoding="utf-8")

    with pytest.raises(GitEvidenceError, match="attributes"):
        ReadOnlyGitRepo(generic_git_repo.path).preflight()


def test_preflight_rejects_promisor_configuration(generic_git_repo):
    subprocess.run(
        ["git", "-C", str(generic_git_repo.path), "config", "remote.origin.promisor", "true"],
        check=True,
        env={"PATH": os.environ["PATH"]},
    )

    with pytest.raises(GitEvidenceError, match="promisor"):
        ReadOnlyGitRepo(generic_git_repo.path).preflight()


def test_preflight_rejects_promisor_object_marker(generic_git_repo):
    pack_dir = generic_git_repo.git_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "fixture.promisor").write_bytes(b"")

    with pytest.raises(GitEvidenceError, match="promisor"):
        ReadOnlyGitRepo(generic_git_repo.path).preflight()


def test_public_revision_arguments_reject_option_shaped_values(generic_git_repo):
    repo = ReadOnlyGitRepo(generic_git_repo.path)

    with pytest.raises(GitEvidenceError, match="OID"):
        repo.resolve("-" + "a" * 39)


def test_history_count_must_match_the_registered_range(generic_git_repo):
    repo = ReadOnlyGitRepo(generic_git_repo.path)

    with pytest.raises(GitEvidenceError, match="reachable revision count"):
        repo._reachable_commits(
            pinned_head=generic_git_repo.head,
            after_exclusive_oid=None,
            committed_at_gte=None,
            expected_commit_count=generic_git_repo.revision_count - 1,
        )


def test_changed_paths_rejects_duplicate_and_rename_statuses(generic_git_repo, monkeypatch):
    repo = ReadOnlyGitRepo(generic_git_repo.path)
    monkeypatch.setattr(
        repo,
        "_run",
        lambda *args, **kwargs: b"M\x00data/a.txt\x00M\x00data/a.txt\x00",
    )
    with pytest.raises(GitEvidenceError, match="duplicate"):
        repo.changed_paths("a" * 40, "b" * 40)

    monkeypatch.setattr(repo, "_run", lambda *args, **kwargs: b"R100\x00data/a.txt\x00")
    with pytest.raises(GitEvidenceError, match="unsupported status"):
        repo.changed_paths("a" * 40, "b" * 40)


def test_stable_patch_id_rejects_malformed_git_output(generic_git_repo, monkeypatch):
    repo = ReadOnlyGitRepo(generic_git_repo.path)
    monkeypatch.setattr(repo, "_run", lambda *args, **kwargs: b"not-an-oid\n")

    with pytest.raises(GitEvidenceError, match="patch ID"):
        repo.stable_patch_id(b"GIT binary patch\nmalformed\n")


def test_all_git_subprocesses_are_argument_arrays_with_shell_disabled(
    generic_git_repo, monkeypatch
):
    real_run = subprocess.run
    calls = []

    def recording_run(args, **kwargs):
        calls.append((args, kwargs))
        return real_run(args, **kwargs)

    monkeypatch.setattr(git_module.subprocess, "run", recording_run)
    repo = ReadOnlyGitRepo(generic_git_repo.path)
    assert repo.resolve(generic_git_repo.head) == generic_git_repo.head

    assert calls
    assert all(isinstance(args, list) for args, _kwargs in calls)
    assert all(kwargs.get("shell") is False for _args, kwargs in calls)
