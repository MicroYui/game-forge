"""Exact regular-file trees used by external-case fixtures."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from gameforge.bench.external_cases.contracts import TreeArtifact, TreeFile, content_sha256


def _regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            if entry.is_symlink():
                raise ValueError(f"fixture tree contains symlink: {path}")
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                stack.append(path)
            elif stat.S_ISREG(mode):
                files.append(path)
            else:
                raise ValueError(f"fixture tree contains non-regular file: {path}")
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def tree_artifact(root: str | Path) -> TreeArtifact:
    base = Path(root)
    if not base.is_dir():
        raise ValueError(f"fixture tree root is not a directory: {base}")
    files: list[TreeFile] = []
    for path in _regular_files(base):
        raw = path.read_bytes()
        files.append(
            TreeFile(
                path=path.relative_to(base).as_posix(),
                sha256=hashlib.sha256(raw).hexdigest(),
                size=len(raw),
            )
        )
    descriptors = tuple(files)
    return TreeArtifact(files=descriptors, tree_sha256=content_sha256(descriptors))


def read_tree(root: str | Path, artifact: TreeArtifact) -> dict[str, bytes]:
    base = Path(root).resolve(strict=True)
    actual = tree_artifact(base)
    if actual.tree_sha256 != artifact.tree_sha256:
        raise ValueError("tree_sha256 does not match fixture tree")
    if actual.files != artifact.files:
        raise ValueError("fixture tree file descriptors do not match")

    result: dict[str, bytes] = {}
    for descriptor in artifact.files:
        path = base.joinpath(*descriptor.path.split("/"))
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"fixture path is not a regular file: {descriptor.path}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(base):
            raise ValueError(f"fixture path escapes root: {descriptor.path}")
        raw = resolved.read_bytes()
        if len(raw) != descriptor.size:
            raise ValueError(f"size mismatch for {descriptor.path}")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != descriptor.sha256:
            raise ValueError(f"sha256 mismatch for {descriptor.path}")
        result[descriptor.path] = raw
    return result


__all__ = ["read_tree", "tree_artifact"]
