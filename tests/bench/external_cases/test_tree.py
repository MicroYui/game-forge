from __future__ import annotations

import os

import pytest

from gameforge.bench.external_cases.tree import read_tree, tree_artifact


def test_tree_artifact_partitions_exact_regular_files(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data/a.txt").write_bytes(b"alpha\n")
    (tmp_path / "data/b.txt").write_bytes(b"beta\r\n")

    artifact = tree_artifact(tmp_path)

    assert [item.path for item in artifact.files] == ["data/a.txt", "data/b.txt"]
    assert read_tree(tmp_path, artifact) == {
        "data/a.txt": b"alpha\n",
        "data/b.txt": b"beta\r\n",
    }


def test_read_tree_rejects_tampered_file(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    path = tmp_path / "data/a.txt"
    path.write_bytes(b"alpha\n")
    artifact = tree_artifact(tmp_path)
    path.write_bytes(b"changed\n")

    with pytest.raises(ValueError, match="size|sha256"):
        read_tree(tmp_path, artifact)


def test_tree_artifact_rejects_symlinks(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(b"outside\n")
    os.symlink(target, tmp_path / "data/link.txt")

    with pytest.raises(ValueError, match="symlink"):
        tree_artifact(tmp_path / "data")


def test_read_tree_rejects_aggregate_descriptor_tampering(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data/a.txt").write_bytes(b"alpha\n")
    artifact = tree_artifact(tmp_path)
    tampered = artifact.model_copy(update={"tree_sha256": "f" * 64})

    with pytest.raises(ValueError, match="tree_sha256"):
        read_tree(tmp_path, tampered)
