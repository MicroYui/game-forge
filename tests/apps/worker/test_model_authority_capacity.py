from __future__ import annotations

import json
from pathlib import Path

import pytest

from gameforge.apps.worker.model_authority import (
    StaticStructuredModelSnapshotAuthority,
    load_structured_model_snapshot_authority,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.routing import canonical_model_snapshot_id


def _manifest(
    snapshots: list[ModelSnapshot],
    *,
    authority_version: str = "deployment-models@1",
) -> dict[str, object]:
    bindings = [
        {
            "model_snapshot_id": canonical_model_snapshot_id(snapshot),
            "snapshot": snapshot.model_dump(mode="json"),
        }
        for snapshot in snapshots
    ]
    bindings.sort(key=lambda item: item["model_snapshot_id"])
    payload: dict[str, object] = {
        "manifest_schema_version": "structured-model-snapshots@1",
        "authority_version": authority_version,
        "bindings": bindings,
    }
    return {**payload, "manifest_digest": canonical_sha256(payload)}


def _write_manifest(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def test_manifest_file_loader_remains_backward_compatible(tmp_path: Path) -> None:
    snapshot = ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")
    manifest_path = tmp_path / "model-snapshots.json"
    _write_manifest(manifest_path, _manifest([snapshot]))

    authority = load_structured_model_snapshot_authority(manifest_path)

    assert isinstance(authority, StaticStructuredModelSnapshotAuthority)
    assert authority.get_model_snapshot(canonical_model_snapshot_id(snapshot)) == snapshot


def test_manifest_directory_closes_1025_snapshots_across_two_bounded_shards(
    tmp_path: Path,
) -> None:
    snapshots = [
        ModelSnapshot(provider="openai", model=f"model-{index:04d}", snapshot_tag="v1")
        for index in range(1025)
    ]
    first = _manifest(snapshots[:1024])
    second = _manifest(snapshots[1024:])
    manifest_directory = tmp_path / "model-snapshots"
    manifest_directory.mkdir()
    _write_manifest(manifest_directory / "z-second.json", second)
    _write_manifest(manifest_directory / "a-first.json", first)

    authority = load_structured_model_snapshot_authority(manifest_directory)

    assert len(authority.model_snapshot_ids) == 1025
    assert authority.model_snapshot_ids == tuple(sorted(authority.model_snapshot_ids))
    assert tuple(shard.manifest_digest for shard in authority.manifests) == (
        first["manifest_digest"],
        second["manifest_digest"],
    )
    assert authority.get_model_snapshot(canonical_model_snapshot_id(snapshots[0])) == snapshots[0]
    assert authority.get_model_snapshot(canonical_model_snapshot_id(snapshots[-1])) == snapshots[-1]


def test_manifest_directory_requires_one_exact_authority_version(tmp_path: Path) -> None:
    manifest_directory = tmp_path / "model-snapshots"
    manifest_directory.mkdir()
    _write_manifest(
        manifest_directory / "one.json",
        _manifest(
            [ModelSnapshot(provider="openai", model="one", snapshot_tag="v1")],
            authority_version="deployment-models@1",
        ),
    )
    _write_manifest(
        manifest_directory / "two.json",
        _manifest(
            [ModelSnapshot(provider="openai", model="two", snapshot_tag="v1")],
            authority_version="deployment-models@2",
        ),
    )

    with pytest.raises(IntegrityViolation, match="authority version"):
        load_structured_model_snapshot_authority(manifest_directory)


def test_manifest_directory_rejects_duplicate_identity_across_shards(
    tmp_path: Path,
) -> None:
    snapshot = ModelSnapshot(provider="openai", model="same", snapshot_tag="v1")
    shard = _manifest([snapshot])
    manifest_directory = tmp_path / "model-snapshots"
    manifest_directory.mkdir()
    _write_manifest(manifest_directory / "one.json", shard)
    _write_manifest(manifest_directory / "two.json", shard)

    with pytest.raises(IntegrityViolation, match="more than one manifest"):
        load_structured_model_snapshot_authority(manifest_directory)


@pytest.mark.parametrize("unsafe_entry_kind", ("directory", "symlink"))
def test_manifest_directory_rejects_non_regular_entries(
    tmp_path: Path,
    unsafe_entry_kind: str,
) -> None:
    manifest_directory = tmp_path / "model-snapshots"
    manifest_directory.mkdir()
    valid = manifest_directory / "valid.json"
    _write_manifest(
        valid,
        _manifest([ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")]),
    )
    unsafe = manifest_directory / "unsafe"
    if unsafe_entry_kind == "directory":
        unsafe.mkdir()
    else:
        unsafe.symlink_to(valid)

    with pytest.raises(IntegrityViolation, match="regular manifest files"):
        load_structured_model_snapshot_authority(manifest_directory)


def test_manifest_directory_rejects_empty_closure(tmp_path: Path) -> None:
    manifest_directory = tmp_path / "model-snapshots"
    manifest_directory.mkdir()

    with pytest.raises(IntegrityViolation, match="empty"):
        load_structured_model_snapshot_authority(manifest_directory)
