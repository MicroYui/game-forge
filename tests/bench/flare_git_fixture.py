"""Deterministic local Git history for Flare discovery tests."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


_OID_RE = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class FlareGitRepoFixture:
    path: Path
    git_dir: Path
    empty_tree_oid: str
    root: str
    source_gap_a: str
    source_gap_b: str
    remote_backport_source: str
    source_gap_c: str
    source_gap_d: str
    quest_fix: str
    before_multicommit: str
    multicommit_a: str
    multicommit_b: str
    multicommit_c: str
    reference_fix: str
    spawn_fix: str
    status_fix: str
    chest_fix: str
    mixed_fix: str
    before_loot: str
    loot_fix: str
    loot_cherry_pick: str
    backport: str
    localization_only: str
    behavior_and_localization: str
    non_utf8_binary_sibling: str
    engine_key_only: str
    loot_revert: str
    merge_commit: str
    head: str
    revision_count: int


@dataclass(frozen=True)
class SearchRegistrationRepoFixture:
    path: Path
    git_dir: Path
    repo_relative_path: str
    registration_commit: str
    result_commit: str
    late_repo_relative_path: str
    late_registration_commit: str


class _GitBuilder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._timestamp = 1_600_000_000
        self._base_env = {
            "PATH": os.environ["PATH"],
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_NAME": "Flare Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Flare Fixture",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }

    def run(
        self,
        *args: str,
        input_bytes: bytes | None = None,
        commit_time: bool = False,
    ) -> bytes:
        env = dict(self._base_env)
        if commit_time:
            timestamp = f"{self._timestamp} +0000"
            env["GIT_AUTHOR_DATE"] = timestamp
            env["GIT_COMMITTER_DATE"] = timestamp
            self._timestamp += 1
        completed = subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=False,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
        return completed.stdout

    def oid(self, ref: str = "HEAD") -> str:
        oid = self.run("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
        assert _OID_RE.fullmatch(oid)
        return oid

    def commit(
        self,
        subject: str,
        changes: dict[str, str | bytes],
        *,
        body: str | None = None,
    ) -> str:
        for relative, content in changes.items():
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
        self.run("add", "--all")
        args = ["commit", "--no-gpg-sign", "-m", subject]
        if body is not None:
            args.extend(["-m", body])
        self.run(*args, commit_time=True)
        return self.oid()

    def message(self, oid: str) -> str:
        return self.run("show", "-s", "--format=%B", oid).decode("utf-8", errors="strict")


def build_flare_git_repo(path: Path) -> FlareGitRepoFixture:
    path.parent.mkdir(parents=True, exist_ok=True)
    init_env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=init_env,
        shell=False,
    )
    git = _GitBuilder(path)
    git.run("config", "commit.gpgSign", "false")
    git.run("config", "core.hooksPath", "/dev/null")
    git.run("config", "merge.autoStash", "false")

    root = git.commit(
        "Fix missing quest status",
        {"mods/core/quests/root.txt": "requires_status = root_ready\n"},
    )
    source_gap_a = git.commit(
        "Record engine fixture note one",
        {"engine/gap_a.py": "GAP_A = 1\n"},
    )
    source_gap_b = git.commit(
        "Record engine fixture note two",
        {"engine/gap_b.py": "GAP_B = 2\n"},
    )
    remote_backport_source = git.commit(
        "Document remote quest context",
        {"mods/core/quests/remote-source.txt": "title = remote context\n"},
    )
    source_gap_c = git.commit(
        "Record engine fixture note three",
        {"engine/gap_c.py": "GAP_C = 3\n"},
    )
    source_gap_d = git.commit(
        "Record engine fixture note four",
        {"engine/gap_d.py": "GAP_D = 4\n"},
    )

    quest_fix = git.commit(
        "Fix broken quest reference",
        {"mods/core/quests/quest-fix.txt": "requires_status = quest_intro\n"},
    )
    before_multicommit = quest_fix
    multicommit_a = git.commit(
        "Fix incorrect quest status setup",
        {
            "mods/core/quests/multicommit.txt": (
                "title = chained quest\nrequires_status = missing_stage\n"
            )
        },
    )
    multicommit_b = git.commit(
        "Continue quest data",
        {
            "mods/core/quests/multicommit.txt": (
                "title = chained quest\nrequires_status = missing_stage\n"
                "note = preserve adjacent context\n"
            )
        },
    )
    multicommit_c = git.commit(
        "Fix missing quest completion",
        {
            "mods/core/quests/multicommit.txt": (
                "title = chained quest\nrequires_status = ready_stage\n"
                "note = preserve adjacent context\n"
            )
        },
    )
    reference_fix = git.commit(
        "Fix broken quest references",
        {"mods/core/quests/reference-fix.txt": "requires_item = ancient_key\n"},
    )
    spawn_fix = git.commit(
        "Fix missing enemy spawn",
        {"mods/core/quests/spawn-fix.txt": "item = missing_spawn_token\n"},
    )
    status_fix = git.commit(
        "Fix stuck quest status",
        {"mods/core/quests/status-fix.txt": "set_status = quest_released\n"},
    )
    chest_fix = git.commit(
        "Fix missing chest item",
        {"mods/core/quests/chest-fix.txt": "requires_item = chest_key\n"},
    )
    mixed_fix = git.commit(
        "Fix broken quest runtime",
        {
            "engine/runtime.py": "RUNTIME_FIX = True\n",
            "mods/core/quests/test.txt": "set_status = runtime_ready\n",
        },
    )

    before_loot = mixed_fix
    loot_fix = git.commit(
        "Fix missing loot drop",
        {"mods/core/loot/table.txt": "loot = rare_sword\n"},
    )
    git.run("branch", "loot-backport", before_loot)
    git.run("switch", "loot-backport")
    git.run("cherry-pick", "-x", loot_fix, commit_time=True)
    loot_cherry_pick = git.oid()
    git.run("switch", "main")

    backport = git.commit(
        "Fix upstream configuration",
        {"mods/core/quests/backport.txt": "set_status = restored_remote_state\n"},
        body=f"Backport-of: {remote_backport_source}",
    )
    localization_only = git.commit(
        "Fix missing quest language item",
        {"mods/test/languages/readme.txt": "localized quest guidance\n"},
    )
    behavior_and_localization = git.commit(
        "Fix broken quest status text",
        {
            "mods/core/quests/behavior-localization.txt": "set_status = localized_ready\n",
            "mods/test/languages/readme.txt": "updated localized quest guidance\n",
        },
    )
    non_utf8_binary_sibling = git.commit(
        "Fix broken quest status binary",
        {
            "assets/raw/non_utf8.bin": b"\x00\xff\xfe\x80binary\x00",
            "mods/core/quests/binary-sibling.txt": "requires_status = binary_ready\n",
        },
    )
    engine_key_only = git.commit(
        "Routine content maintenance",
        {
            "engine/behavior.txt": "set_status = engine_only\n",
            "mods/core/quests/unique-neutral.txt": "title = neutral content\n",
        },
    )

    git.run("revert", "--no-edit", loot_fix, commit_time=True)
    loot_revert = git.oid()
    git.run(
        "merge",
        "--no-ff",
        "--no-gpg-sign",
        "-m",
        "Merge loot backport branch",
        "loot-backport",
        commit_time=True,
    )
    merge_commit = git.oid()

    empty_tree_oid = git.run("hash-object", "-t", "tree", "--stdin", input_bytes=b"")
    empty_tree_oid_text = empty_tree_oid.decode("ascii").strip()
    assert _OID_RE.fullmatch(empty_tree_oid_text)

    cherry_message = git.message(loot_cherry_pick)
    backport_message = git.message(backport)
    revert_message = git.message(loot_revert)
    assert re.search(
        rf"(?m)^\(cherry picked from commit {re.escape(loot_fix)}\)$",
        cherry_message,
    )
    assert re.search(
        rf"(?m)^Backport-of: {re.escape(remote_backport_source)}$",
        backport_message,
    )
    assert re.search(
        rf"(?m)^This reverts commit {re.escape(loot_fix)}\.$",
        revert_message,
    )

    merge_parents = git.run("show", "-s", "--format=%P", merge_commit).decode().split()
    assert merge_parents == [loot_revert, loot_cherry_pick]
    revision_count = int(git.run("rev-list", "--count", merge_commit).decode().strip())

    return FlareGitRepoFixture(
        path=path,
        git_dir=path / ".git",
        empty_tree_oid=empty_tree_oid_text,
        root=root,
        source_gap_a=source_gap_a,
        source_gap_b=source_gap_b,
        remote_backport_source=remote_backport_source,
        source_gap_c=source_gap_c,
        source_gap_d=source_gap_d,
        quest_fix=quest_fix,
        before_multicommit=before_multicommit,
        multicommit_a=multicommit_a,
        multicommit_b=multicommit_b,
        multicommit_c=multicommit_c,
        reference_fix=reference_fix,
        spawn_fix=spawn_fix,
        status_fix=status_fix,
        chest_fix=chest_fix,
        mixed_fix=mixed_fix,
        before_loot=before_loot,
        loot_fix=loot_fix,
        loot_cherry_pick=loot_cherry_pick,
        backport=backport,
        localization_only=localization_only,
        behavior_and_localization=behavior_and_localization,
        non_utf8_binary_sibling=non_utf8_binary_sibling,
        engine_key_only=engine_key_only,
        loot_revert=loot_revert,
        merge_commit=merge_commit,
        head=merge_commit,
        revision_count=revision_count,
    )


def build_search_registration_repo(
    path: Path, registered_search_spec_bytes: bytes
) -> SearchRegistrationRepoFixture:
    path.parent.mkdir(parents=True, exist_ok=True)
    init_env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=init_env,
        shell=False,
    )
    git = _GitBuilder(path)
    git.run("config", "commit.gpgSign", "false")
    git.run("config", "core.hooksPath", "/dev/null")
    git.commit("Initialize project provenance fixture", {"README.md": "fixture\n"})

    repo_relative_path = "scenarios/flare_corpus/search-spec.json"
    registration_commit = git.commit(
        "Register Flare search specification",
        {repo_relative_path: registered_search_spec_bytes},
    )
    result_commit = git.commit(
        "Record generated Flare discovery result",
        {"scenarios/flare_corpus/discovered-initial.json": b'{"result":"fixture"}\n'},
    )
    late_repo_relative_path = "scenarios/flare_corpus/late-search-spec.json"
    late_registration_commit = git.commit(
        "Register search specification after result",
        {late_repo_relative_path: registered_search_spec_bytes},
    )

    return SearchRegistrationRepoFixture(
        path=path,
        git_dir=path / ".git",
        repo_relative_path=repo_relative_path,
        registration_commit=registration_commit,
        result_commit=result_commit,
        late_repo_relative_path=late_repo_relative_path,
        late_registration_commit=late_registration_commit,
    )
