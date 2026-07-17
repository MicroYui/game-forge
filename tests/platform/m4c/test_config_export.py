"""Task 12a — the concrete Aureus ``ConfigExporter`` (platform-level exporter).

``tests/contracts/m4c/test_config_export.py`` already covers the codec framing;
this exercises the injected apps/worker exporter that serializes
``AureusCsvAdapter.from_ir(preview)`` per-sheet rows into a
``ConfigExportPackageV1`` bound to the resolved ``config_export`` profile details.
The concrete exporter lives in ``apps/worker`` (composition boundary) because the
platform contract must never hardcode Aureus.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gameforge.apps.worker.config_export import (
    AureusConfigExporter,
    build_aureus_config_exporter,
    build_config_export_details_resolver,
    decode_aureus_config_workbook,
)
from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.config_export import (
    ConfigExportFileV1,
    ConfigExportPackageV1,
    canonical_config_export_bytes,
    decode_config_export_bytes,
)
from gameforge.contracts.execution_profiles import (
    ConfigExportProfileDetailsV1,
    ExecutionProfileCatalogSnapshotV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    execution_profile_catalog_digest,
    execution_profile_payload_hash,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.publication.publisher import TerminalPublisher
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.spine.ir.loader import load_scenario
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
        "regions": [
            {
                "region_id": "region:start",
                "name": "Start",
                "grid": {"width": 4, "height": 4, "blocked": []},
                "start_pos": [0, 0],
                "scenario_id": "config-export",
            }
        ],
        "npcs": [
            {
                "npc_id": "guide",
                "name": "Guide",
                "region": "region:start",
                "pos": [1, 0],
            }
        ],
        "items": [{"item_id": "sword", "name": "Sword"}],
        "quests": [{"quest_id": "q1", "giver": "guide", "reward": {"item": "sword", "gold": 30}}],
        "quest_steps": [
            {
                "step_id": "step:turn-in",
                "quest_id": "q1",
                "order": 0,
                "kind": "turn_in",
                "target": "guide",
                "item": None,
                "count": 1,
                "encounter": None,
            }
        ],
    }


def _preview_payload() -> dict:
    return AureusCsvAdapter().to_ir(_workbook(), "preview.csv").content_payload


def _exporter(details: ConfigExportProfileDetailsV1 = _DETAILS) -> AureusConfigExporter:
    return AureusConfigExporter(details_resolver=lambda binding, **kwargs: details)


def _binding(profile: ProfileRefV1) -> ResolvedExecutionProfileBindingV1:
    return ResolvedExecutionProfileBindingV1(
        field_path="/params/candidate_export_profiles/0",
        profile=profile,
        expected_profile_kind="config_export",
        profile_payload_hash="a" * 64,
        catalog_version=1,
        catalog_digest="b" * 64,
    )


def _export(profile: ProfileRefV1 = _EXPORT_PROFILE) -> ConfigExportPackageV1:
    return _exporter().export(
        export_profile=profile,
        export_profile_binding=_binding(profile),
        run_kind=RunKindRef(kind="generation.propose", version=1),
        llm_execution_mode="replay",
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


def test_terminal_rederives_config_export_from_authoritative_preview() -> None:
    registry = build_builtin_registry()
    catalog = max(
        registry.list_execution_profile_catalogs(),
        key=lambda item: item.catalog_version,
    )
    definition = next(item for item in catalog.definitions if item.profile == _EXPORT_PROFILE)
    binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/candidate_export_profiles/0",
        profile=_EXPORT_PROFILE,
        expected_profile_kind="config_export",
        profile_payload_hash=execution_profile_payload_hash(definition),
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    )
    exporter = build_aureus_config_exporter(registry)
    authoritative = _preview_payload()
    altered_workbook = _workbook()
    altered_workbook["npcs"][0]["name"] = "Forged Guide"
    forged_preview = (
        AureusCsvAdapter()
        .to_ir(
            altered_workbook,
            "forged-preview.csv",
        )
        .content_payload
    )
    forged = exporter.export(
        export_profile=_EXPORT_PROFILE,
        export_profile_binding=binding,
        run_kind=RunKindRef(kind="generation.propose", version=1),
        llm_execution_mode="replay",
        preview_snapshot_id="artifact:preview-final",
        preview_payload=forged_preview,
        constraint_snapshot_artifact_id="artifact:constraint",
        constraints=(),
    )
    constraint_blob = canonical_json(
        {"dsl_grammar_version": "constraint-dsl@1", "constraints": []}
    ).encode("utf-8")
    publisher = object.__new__(TerminalPublisher)
    publisher._config_exporter = exporter  # noqa: SLF001
    publisher._artifacts = SimpleNamespace(  # noqa: SLF001
        read_bytes=lambda artifact_id: constraint_blob
    )
    run = SimpleNamespace(
        kind=RunKindRef(kind="generation.propose", version=1),
        payload=SimpleNamespace(
            params=SimpleNamespace(candidate_export_profiles=(_EXPORT_PROFILE,)),
            resolved_profiles=(binding,),
            llm_execution_mode="replay",
        ),
    )

    with pytest.raises(IntegrityViolation, match="deterministic terminal derivation"):
        publisher._validate_config_export_content_authority(  # noqa: SLF001
            run=run,
            payload_schema_id="config-export-package@1",
            payload=forged.model_dump(mode="json"),
            preview_payloads=(authoritative,),
        )


def test_export_rejects_a_package_the_target_environment_cannot_execute() -> None:
    preview = AureusCsvAdapter().to_ir(
        {"items": [{"item_id": "item:orphan", "name": "Orphan"}]},
        "incomplete-preview.csv",
    )

    with pytest.raises(IntegrityViolation, match="executable preview"):
        _exporter().export(
            export_profile=_EXPORT_PROFILE,
            export_profile_binding=_binding(_EXPORT_PROFILE),
            run_kind=RunKindRef(kind="generation.propose", version=1),
            llm_execution_mode="replay",
            preview_snapshot_id=preview.snapshot_id,
            preview_payload=preview.content_payload,
            constraint_snapshot_artifact_id="artifact:constraint",
            constraints=(),
        )


def test_loader_preview_export_decodes_to_an_executable_round_trip() -> None:
    source = load_scenario(
        {
            "scenario_id": "export-loader-preview",
            "grid": {"width": 4, "height": 4, "blocked": []},
            "start_pos": [0, 0],
            "regions": [{"id": "region:r", "name": "R"}],
            "npcs": [{"id": "npc:a", "name": "A", "region": "region:r", "pos": [1, 0]}],
            "items": [{"id": "item:x", "name": "X"}],
            "interactables": [
                {
                    "id": "gather:x",
                    "kind": "gather",
                    "pos": [2, 0],
                    "yields_item": "item:x",
                    "yields_count": 1,
                }
            ],
            "quests": [
                {
                    "id": "quest:q",
                    "title": "Q",
                    "region": "region:r",
                    "giver": "npc:a",
                    "reward": {"gold": 10},
                    "steps": [
                        {"id": "step:talk", "kind": "talk", "target": "npc:a"},
                        {"id": "step:collect", "kind": "collect", "item": "item:x"},
                        {"id": "step:turn-in", "kind": "turn_in", "target": "npc:a"},
                    ],
                }
            ],
        }
    )
    registry = build_builtin_registry()
    catalog = registry.list_execution_profile_catalogs()[0]
    definition = next(item for item in catalog.definitions if item.profile == _EXPORT_PROFILE)
    binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/candidate_export_profiles/0",
        profile=_EXPORT_PROFILE,
        expected_profile_kind="config_export",
        profile_payload_hash=execution_profile_payload_hash(definition),
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    )
    package = build_aureus_config_exporter(registry).export(
        export_profile=_EXPORT_PROFILE,
        export_profile_binding=binding,
        run_kind=RunKindRef(kind="generation.propose", version=1),
        llm_execution_mode="replay",
        preview_snapshot_id=source.snapshot_id,
        preview_payload=source.content_payload,
        constraint_snapshot_artifact_id="artifact:constraint",
        constraints=(),
    )

    decoded = decode_config_export_bytes(canonical_config_export_bytes(package))
    workbook = decode_aureus_config_workbook(decoded)
    rebuilt = AureusCsvAdapter().to_ir(
        workbook,
        file_ref="config-export://export-loader-preview",
    )

    assert snapshot_to_world(rebuilt) == snapshot_to_world(source)


@pytest.mark.parametrize(
    ("path", "content"),
    (("unknown.json", b"[]"), ("quests.json", b"[ ]")),
)
def test_aureus_workbook_decoder_rejects_unknown_or_noncanonical_sheets(
    path: str,
    content: bytes,
) -> None:
    file = ConfigExportFileV1(
        relative_path=path,
        media_type="application/json",
        content_sha256=sha256_lowerhex(content),
        size_bytes=len(content),
        content_bytes=content,
    )
    package = ConfigExportPackageV1(
        export_profile=_EXPORT_PROFILE,
        target_environment_profile=_ENV_PROFILE,
        env_contract_version="generic-agent-env@1",
        source_preview_artifact_id="artifact:preview",
        constraint_snapshot_artifact_id="artifact:constraint",
        format_schema_id="config-export-files@1",
        files=(file,),
    )

    with pytest.raises(IntegrityViolation, match="unknown|canonical"):
        decode_aureus_config_workbook(package)


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


def test_details_resolver_uses_the_exact_frozen_catalog_binding() -> None:
    builtin = build_builtin_registry()
    first = builtin.list_execution_profile_catalogs()[0]
    original = next(
        definition for definition in first.definitions if definition.profile_kind == "config_export"
    )
    changed = original.model_copy(
        update={
            "details": original.details.model_copy(
                update={"format_schema_id": "another-config-format@1"}
            )
        }
    )
    second_payload = {
        "catalog_version": first.catalog_version + 1,
        "definitions": tuple(
            changed if definition.profile == original.profile else definition
            for definition in first.definitions
        ),
        "lifecycle": first.lifecycle,
    }
    second = ExecutionProfileCatalogSnapshotV1(
        **second_payload,
        catalog_digest=execution_profile_catalog_digest(second_payload),
    )

    class _CatalogRegistry:
        def list_execution_profile_catalogs(self):
            # Put the conflicting later catalog last: a ProfileRef-only lookup
            # would silently select it instead of the Run's frozen authority.
            return (first, second)

        def get_execution_profile_catalog(self, catalog_version, catalog_digest):
            for catalog in (first, second):
                if (
                    catalog.catalog_version == catalog_version
                    and catalog.catalog_digest == catalog_digest
                ):
                    return catalog
            return None

    resolver = build_config_export_details_resolver(_CatalogRegistry())  # type: ignore[arg-type]
    binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/candidate_export_profiles/0",
        profile=original.profile,
        expected_profile_kind="config_export",
        profile_payload_hash=execution_profile_payload_hash(original),
        catalog_version=first.catalog_version,
        catalog_digest=first.catalog_digest,
    )

    details = resolver(
        binding,
        run_kind=RunKindRef(kind="generation.propose", version=1),
        llm_execution_mode="replay",
    )

    assert details == original.details
    assert details.format_schema_id != changed.details.format_schema_id


def test_details_resolver_rejects_a_forged_profile_payload_hash() -> None:
    registry = build_builtin_registry()
    catalog = registry.list_execution_profile_catalogs()[0]
    definition = next(item for item in catalog.definitions if item.profile_kind == "config_export")
    resolver = build_config_export_details_resolver(registry)
    binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/candidate_export_profiles/0",
        profile=definition.profile,
        expected_profile_kind="config_export",
        profile_payload_hash="0" * 64,
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    )

    with pytest.raises(IntegrityViolation, match="frozen Run binding"):
        resolver(
            binding,
            run_kind=RunKindRef(kind="generation.propose", version=1),
            llm_execution_mode="replay",
        )
