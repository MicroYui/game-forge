from __future__ import annotations

import hashlib

from pydantic import ValidationError
import pytest

from gameforge.contracts.config_export import (
    MAX_CONFIG_EXPORT_FILE_BYTES,
    ConfigExportFileV1,
    ConfigExportPackageV1,
    canonical_config_export_bytes,
)
from gameforge.contracts.execution_profiles import ProfileRefV1


def _file(path: str, content: bytes = b"id,value\n1,2\n") -> ConfigExportFileV1:
    return ConfigExportFileV1(
        relative_path=path,
        media_type="text/csv",
        content_sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        content_bytes=content,
    )


def _package(*files: ConfigExportFileV1) -> ConfigExportPackageV1:
    return ConfigExportPackageV1(
        export_profile=ProfileRefV1(profile_id="export:aureus-csv", version=3),
        target_environment_profile=ProfileRefV1(profile_id="environment:fixture", version=2),
        env_contract_version="agent-env@2",
        source_preview_artifact_id="artifact:preview",
        constraint_snapshot_artifact_id="artifact:constraints",
        format_schema_id="aureus-csv@1",
        files=files,
    )


def test_file_binds_normalized_relative_path_size_hash_and_arbitrary_bytes() -> None:
    content = b"\x00\xff\n"
    exported = _file("tables/e\u0301conomy.csv", content)

    assert exported.relative_path == "tables/\u00e9conomy.csv"
    assert exported.size_bytes == 3
    assert exported.content_sha256 == hashlib.sha256(content).hexdigest()
    assert ConfigExportFileV1.model_validate_json(exported.model_dump_json()) == exported

    for change, match in (
        ({"size_bytes": 4}, "size_bytes"),
        ({"content_sha256": "0" * 64}, "content_sha256"),
    ):
        with pytest.raises(ValidationError, match=match):
            ConfigExportFileV1(**{**exported.model_dump(mode="python"), **change})


@pytest.mark.parametrize(
    "path",
    (
        "",
        "/absolute/file.csv",
        "C:/windows/file.csv",
        "../escape.csv",
        "tables/../escape.csv",
        "tables/./file.csv",
        "tables//file.csv",
        "tables/file.csv/",
        "tables\\file.csv",
        "tables/\x00file.csv",
    ),
)
def test_file_rejects_unsafe_or_ambiguous_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="relative_path"):
        _file(path)


def test_file_content_has_a_frozen_hard_limit() -> None:
    content = b"x" * (MAX_CONFIG_EXPORT_FILE_BYTES + 1)
    with pytest.raises(ValidationError):
        _file("oversized.bin", content)


def test_package_canonicalizes_files_and_rejects_normalized_path_collisions() -> None:
    economy = _file("tables/economy.csv")
    quests = _file("tables/quests.csv")

    package = _package(quests, economy)

    assert tuple(file.relative_path for file in package.files) == (
        "tables/economy.csv",
        "tables/quests.csv",
    )
    with pytest.raises(ValidationError, match="relative_path"):
        _package(_file("tables/e\u0301.csv"), _file("tables/\u00e9.csv"))


def test_package_framing_is_order_independent_and_binds_manifest_and_raw_bytes() -> None:
    economy = _file("tables/economy.csv", b"economy")
    quests = _file("tables/quests.csv", b"quests")
    first = _package(quests, economy)
    second = _package(economy, quests)

    framed = canonical_config_export_bytes(first)

    assert framed == canonical_config_export_bytes(second)
    assert b"config-export-package@1" in framed
    assert b"tables/economy.csv" in framed
    assert framed.endswith(b"economy" + len(b"quests").to_bytes(8, "big") + b"quests")

    changed_content = _package(_file("tables/economy.csv", b"changed"), quests)
    changed_profile = first.model_copy(
        update={"export_profile": ProfileRefV1(profile_id="export:other", version=1)}
    )
    assert canonical_config_export_bytes(changed_content) != framed
    assert canonical_config_export_bytes(changed_profile) != framed


def test_package_requires_at_least_one_file() -> None:
    with pytest.raises(ValidationError):
        _package()
