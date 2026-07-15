"""Task 12a — the concrete Aureus ``ConfigExporter`` (platform-level exporter).

``tests/contracts/m4c/test_config_export.py`` already covers the codec framing;
this exercises the injected apps/worker exporter that serializes
``AureusCsvAdapter.from_ir(preview)`` per-sheet rows into a
``ConfigExportPackageV1`` bound to the resolved ``config_export`` profile details.
The concrete exporter lives in ``apps/worker`` (composition boundary) because the
platform contract must never hardcode Aureus.
"""

from __future__ import annotations

from gameforge.apps.worker.config_export import AureusConfigExporter
from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.config_export import (
    ConfigExportPackageV1,
    canonical_config_export_bytes,
)
from gameforge.contracts.execution_profiles import (
    ConfigExportProfileDetailsV1,
    ProfileRefV1,
)
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter

_ENV_PROFILE = ProfileRefV1(profile_id="builtin.environment", version=1)
_EXPORT_PROFILE = ProfileRefV1(profile_id="builtin.config_export", version=1)
_OTHER_EXPORT_PROFILE = ProfileRefV1(profile_id="builtin.config_export", version=2)

_DETAILS = ConfigExportProfileDetailsV1(
    target_environment_profile=_ENV_PROFILE,
    env_contract_version="generic-agent-env@1",
    format_schema_id="config-export-files@1",
)


def _workbook() -> dict[str, list[dict]]:
    return {
        "npcs": [{"npc_id": "guide", "name": "Guide"}],
        "items": [{"item_id": "sword", "name": "Sword"}],
        "quests": [{"quest_id": "q1", "giver": "guide", "reward": {"item": "sword", "gold": 30}}],
    }


def _preview_payload() -> dict:
    return AureusCsvAdapter().to_ir(_workbook(), "preview.csv").content_payload


def _exporter(details: ConfigExportProfileDetailsV1 = _DETAILS) -> AureusConfigExporter:
    return AureusConfigExporter(details_resolver=lambda ref: details)


def _export(profile: ProfileRefV1 = _EXPORT_PROFILE) -> ConfigExportPackageV1:
    return _exporter().export(
        export_profile=profile,
        preview_snapshot_id="snapshot:preview",
        preview_payload=_preview_payload(),
        constraint_snapshot_artifact_id="artifact:constraint",
        constraints=(),
    )


def test_export_binds_profile_environment_constraint_and_preview() -> None:
    package = _export()

    assert package.export_profile == _EXPORT_PROFILE
    assert package.target_environment_profile == _DETAILS.target_environment_profile
    assert package.env_contract_version == _DETAILS.env_contract_version
    assert package.format_schema_id == _DETAILS.format_schema_id
    assert package.source_preview_artifact_id == "snapshot:preview"
    assert package.constraint_snapshot_artifact_id == "artifact:constraint"
    assert package.package_schema_version == "config-export-package@1"


def test_export_serializes_every_workbook_sheet_with_safe_paths() -> None:
    package = _export()
    workbook = AureusCsvAdapter().from_ir(AureusCsvAdapter().to_ir(_workbook(), "preview.csv"))

    # one file per emitted sheet.
    assert {file.relative_path for file in package.files} == {f"{sheet}.json" for sheet in workbook}
    for file in package.files:
        # NFC-safe relative path: not absolute, no traversal, no backslash.
        assert not file.relative_path.startswith("/")
        assert ".." not in file.relative_path.split("/")
        assert "\\" not in file.relative_path
        # per-file hash/size bind exactly to the raw bytes.
        assert file.content_sha256 == sha256_lowerhex(file.content_bytes)
        assert file.size_bytes == len(file.content_bytes)
        assert file.media_type == "application/json"


def test_export_file_contents_match_from_ir_rows() -> None:
    package = _export()
    workbook = AureusCsvAdapter().from_ir(AureusCsvAdapter().to_ir(_workbook(), "preview.csv"))
    by_path = {file.relative_path: file for file in package.files}
    for sheet, rows in workbook.items():
        expected = canonical_json(rows).encode("utf-8")
        assert by_path[f"{sheet}.json"].content_bytes == expected


def test_export_framing_is_byte_identical_across_runs() -> None:
    first = canonical_config_export_bytes(_export())
    second = canonical_config_export_bytes(_export())
    assert first == second


def test_one_package_per_requested_profile() -> None:
    package_a = _export(_EXPORT_PROFILE)
    package_b = _export(_OTHER_EXPORT_PROFILE)
    assert package_a.export_profile == _EXPORT_PROFILE
    assert package_b.export_profile == _OTHER_EXPORT_PROFILE
    # differing only by export profile ⇒ distinct framings, same file bodies.
    assert canonical_config_export_bytes(package_a) != canonical_config_export_bytes(package_b)
    assert [f.content_sha256 for f in package_a.files] == [
        f.content_sha256 for f in package_b.files
    ]
