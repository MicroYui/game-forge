"""Deterministic multi-source Git history for external-corpus tests."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


_OID_RE = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class GenericGitFixture:
    path: Path
    git_dir: Path
    empty_tree_oid: str
    root: str
    mods_fix: str
    data_fix: str
    data_adjacent: str
    data_missing: str
    mixed_fix: str
    binary_fix: str
    rename_source: str
    rename_fix: str
    backport: str
    duplicate_source: str
    duplicate_copy: str
    duplicate_revert: str
    merge_commit: str
    head: str
    revision_count: int


class _GitBuilder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._timestamp = 1_700_000_000
        self._base_env = {
            "PATH": os.environ["PATH"],
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_NAME": "External Corpus Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "External Corpus Fixture",
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
        environment = dict(self._base_env)
        if commit_time:
            timestamp = f"{self._timestamp} +0000"
            environment["GIT_AUTHOR_DATE"] = timestamp
            environment["GIT_COMMITTER_DATE"] = timestamp
            self._timestamp += 1
        completed = subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=False,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
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


def build_generic_git_repo(path: Path) -> GenericGitFixture:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        },
        shell=False,
    )
    git = _GitBuilder(path)
    git.run("config", "commit.gpgSign", "false")
    git.run("config", "core.hooksPath", "/dev/null")

    root = git.commit("Initialize fixture", {"README.md": "fixture\n"})
    mods_fix = git.commit(
        "Fix broken mod quest",
        {"mods/core/quest.txt": "requires_status = mod_ready\n"},
        body=f"Backport-of: {root}",
    )
    data_fix = git.commit(
        "Fix mission reference",
        {"data/missions/alpha.txt": "requires_status = alpha_ready\n"},
    )
    data_adjacent = git.commit(
        "Continue mission context",
        {
            "data/missions/alpha.txt": (
                "requires_status = alpha_ready\nnote = preserve adjacent context\n"
            )
        },
    )
    data_missing = git.commit(
        "Restore missing mission state",
        {
            "data/missions/alpha.txt": (
                "requires_status = alpha_complete\nnote = preserve adjacent context\n"
            )
        },
    )
    mixed_fix = git.commit(
        "Fix mixed mission",
        {
            "data/missions/mixed.txt": "requires_status = runtime_ready\n",
            "src/runtime.py": "RUNTIME_READY = True\n",
        },
    )
    binary_fix = git.commit(
        "Fix binary-backed mission",
        {
            "assets/raw.bin": b"\x00\xff\x80fixture\x00",
            "data/missions/binary.txt": "requires_status = binary_ready\n",
        },
    )
    rename_source = git.commit(
        "Add legacy mission",
        {"data/missions/old.txt": "status = legacy\n"},
    )
    git.run("mv", "data/missions/old.txt", "data/missions/new.txt")
    git.run("commit", "--no-gpg-sign", "-m", "Fix renamed mission", commit_time=True)
    rename_fix = git.oid()
    backport = git.commit(
        "Fix data backport",
        {"data/missions/backport.txt": "status = restored\n"},
        body=f"Backport-of: {mods_fix}",
    )

    git.run("branch", "duplicate-copy", backport)
    duplicate_source = git.commit(
        "Fix missing drop",
        {"data/loot/drop.txt": "item = relic\n"},
    )
    git.run("switch", "duplicate-copy")
    git.run("cherry-pick", "-x", duplicate_source, commit_time=True)
    duplicate_copy = git.oid()
    git.run("switch", "main")
    git.run("revert", "--no-edit", duplicate_source, commit_time=True)
    duplicate_revert = git.oid()
    git.run(
        "merge",
        "--no-ff",
        "--no-gpg-sign",
        "-m",
        "Merge duplicate branch",
        "duplicate-copy",
        commit_time=True,
    )
    merge_commit = git.oid()

    empty_tree_oid = git.run("hash-object", "-t", "tree", "--stdin", input_bytes=b"")
    empty_tree_oid_text = empty_tree_oid.decode("ascii").strip()
    assert _OID_RE.fullmatch(empty_tree_oid_text)
    revision_count = int(git.run("rev-list", "--count", merge_commit).decode().strip())
    assert revision_count == 14

    return GenericGitFixture(
        path=path,
        git_dir=path / ".git",
        empty_tree_oid=empty_tree_oid_text,
        root=root,
        mods_fix=mods_fix,
        data_fix=data_fix,
        data_adjacent=data_adjacent,
        data_missing=data_missing,
        mixed_fix=mixed_fix,
        binary_fix=binary_fix,
        rename_source=rename_source,
        rename_fix=rename_fix,
        backport=backport,
        duplicate_source=duplicate_source,
        duplicate_copy=duplicate_copy,
        duplicate_revert=duplicate_revert,
        merge_commit=merge_commit,
        head=merge_commit,
        revision_count=revision_count,
    )
