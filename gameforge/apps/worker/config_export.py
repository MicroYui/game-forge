"""Concrete Aureus ``ConfigExporter`` for the 11b/12a run handlers.

The platform ``ConfigExporter`` Protocol (``run_handlers/generation.py``) has NO
concrete impl — the platform contract must never hardcode Aureus/Flare. This
module supplies the versioned Aureus exporter and wires it (composition boundary:
``apps`` may import ``spine`` + ``platform``) so the generation / repair /
task-suite handlers can be injected with a real serializer.

The exporter serializes ``AureusCsvAdapter.from_ir(preview)`` per-sheet rows into
``ConfigExportFileV1``s and frames a ``ConfigExportPackageV1`` whose
``target_environment_profile`` / ``env_contract_version`` / ``format_schema_id``
come from the resolved ``config_export`` profile's
:class:`ConfigExportProfileDetailsV1`. Framing is deterministic (canonical JSON
per sheet + the codec's canonical package bytes), so the same preview + profile
yields byte-identical package bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Mapping, Protocol

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.config_export import (
    ConfigExportFileV1,
    ConfigExportPackageV1,
)
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import (
    ConfigExportProfileDetailsV1,
    EnvironmentProfileDetailsV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    execution_profile_payload_hash,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter, SHEET_NODE_TYPE


# Resolve a config-export profile ref to its frozen details (format_schema_id,
# target environment profile, env_contract_version).
class ConfigExportDetailsResolver(Protocol):
    def __call__(
        self,
        binding: ResolvedExecutionProfileBindingV1,
        *,
        run_kind: RunKindRef | None,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
    ) -> ConfigExportProfileDetailsV1: ...


_SHEET_MEDIA_TYPE = "application/json"


@dataclass(frozen=True, slots=True)
class AureusConfigExporter:
    """Serialize an Aureus preview into a versioned ``ConfigExportPackageV1``."""

    details_resolver: ConfigExportDetailsResolver
    adapter: AureusCsvAdapter = field(default_factory=AureusCsvAdapter)

    def export(
        self,
        *,
        export_profile: ProfileRefV1,
        export_profile_binding: ResolvedExecutionProfileBindingV1,
        run_kind: RunKindRef | None,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        preview_snapshot_id: str,
        preview_payload: Mapping[str, object],
        constraint_snapshot_artifact_id: str,
        constraints: tuple[Constraint, ...],
    ) -> ConfigExportPackageV1:
        if export_profile_binding.profile != export_profile:
            raise IntegrityViolation("config export profile differs from its frozen Run binding")
        details = self.details_resolver(
            export_profile_binding,
            run_kind=run_kind,
            llm_execution_mode=llm_execution_mode,
        )
        snapshot = snapshot_from_canonical_view(dict(preview_payload))
        workbook = self.adapter.from_ir(snapshot)
        if not workbook:
            raise ValueError("preview snapshot has no exportable Aureus content")
        try:
            rebuilt = self.adapter.to_ir(
                workbook,
                file_ref=f"config-export-validation:{preview_snapshot_id}",
            )
            if snapshot_to_world(rebuilt) != snapshot_to_world(snapshot):
                raise IntegrityViolation(
                    "Aureus config export does not preserve the executable preview"
                )
        except IntegrityViolation:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "Aureus config export cannot reconstruct an executable preview"
            ) from exc
        files = tuple(self._file(sheet, rows) for sheet, rows in workbook.items())
        return ConfigExportPackageV1(
            export_profile=export_profile,
            target_environment_profile=details.target_environment_profile,
            env_contract_version=details.env_contract_version,
            source_preview_artifact_id=preview_snapshot_id,
            constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
            format_schema_id=details.format_schema_id,
            files=files,
        )

    @staticmethod
    def _file(sheet: str, rows: list[dict]) -> ConfigExportFileV1:
        content = canonical_json(rows).encode("utf-8")
        return ConfigExportFileV1(
            relative_path=f"{sheet}.json",
            media_type=_SHEET_MEDIA_TYPE,
            content_sha256=sha256_lowerhex(content),
            size_bytes=len(content),
            content_bytes=content,
        )


def build_config_export_details_resolver(
    registry: ImmutablePlatformRegistry,
) -> ConfigExportDetailsResolver:
    """Resolve one export adapter from the Run's exact retained catalog authority."""

    def resolve(
        binding: ResolvedExecutionProfileBindingV1,
        *,
        run_kind: RunKindRef | None,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
    ) -> ConfigExportProfileDetailsV1:
        if binding.expected_profile_kind != "config_export":
            raise IntegrityViolation("config export profile differs from its frozen Run binding")
        catalog = registry.get_execution_profile_catalog(
            binding.catalog_version,
            binding.catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation("config export exact profile catalog is unavailable")
        definitions = tuple(
            definition
            for definition in catalog.definitions
            if definition.profile == binding.profile
        )
        lifecycle = tuple(item for item in catalog.lifecycle if item.profile == binding.profile)
        if len(definitions) != 1 or len(lifecycle) != 1:
            raise IntegrityViolation("config export profile is not unique in its exact catalog")
        definition = definitions[0]
        allowed_states = {"active", "replay_only"} if llm_execution_mode == "replay" else {"active"}
        if (
            definition.profile_kind != "config_export"
            or not isinstance(definition.details, ConfigExportProfileDetailsV1)
            or definition.handler_key != "builtin_config_export_profile@1"
            or definition.details.format_schema_id != "config-export-files@1"
            or definition.details.package_schema_version != "config-export-package@1"
            or execution_profile_payload_hash(definition) != binding.profile_payload_hash
            or lifecycle[0].state not in allowed_states
            or "config-export-package@1" not in definition.output_schema_ids
            or (run_kind is not None and run_kind not in definition.compatible_run_kinds)
        ):
            raise IntegrityViolation("config export profile differs from its frozen Run binding")

        environment_definitions = tuple(
            candidate
            for candidate in catalog.definitions
            if candidate.profile == definition.details.target_environment_profile
        )
        environment_lifecycle = tuple(
            item
            for item in catalog.lifecycle
            if item.profile == definition.details.target_environment_profile
        )
        if len(environment_definitions) != 1 or len(environment_lifecycle) != 1:
            raise IntegrityViolation("config export target environment is not unique")
        environment = environment_definitions[0]
        if (
            environment.profile_kind != "environment"
            or not isinstance(environment.details, EnvironmentProfileDetailsV1)
            or environment.handler_key != "builtin_environment_profile@1"
            or environment.details.contract.env_contract_version
            != definition.details.env_contract_version
            or environment_lifecycle[0].state not in allowed_states
        ):
            raise IntegrityViolation("config export target environment authority is unavailable")
        return definition.details

    return resolve


def decode_aureus_config_workbook(package: ConfigExportPackageV1) -> dict[str, list[dict]]:
    """Decode the built-in Aureus package files without ignoring unknown content."""

    workbook: dict[str, list[dict]] = {}
    for file in package.files:
        if file.media_type != _SHEET_MEDIA_TYPE or "/" in file.relative_path:
            raise IntegrityViolation("Aureus config package contains an unsupported file")
        if not file.relative_path.endswith(".json"):
            raise IntegrityViolation("Aureus config package file has an unsupported extension")
        sheet = file.relative_path[: -len(".json")]
        if sheet not in SHEET_NODE_TYPE or sheet in workbook:
            raise IntegrityViolation("Aureus config package contains an unknown or repeated sheet")
        try:
            decoded = json.loads(file.content_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise IntegrityViolation("Aureus config package sheet is not UTF-8 JSON") from exc
        try:
            canonical = canonical_json(decoded).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise IntegrityViolation(
                "Aureus config package sheet is not canonical row JSON"
            ) from exc
        if (
            not isinstance(decoded, list)
            or any(not isinstance(row, dict) for row in decoded)
            or canonical != file.content_bytes
        ):
            raise IntegrityViolation("Aureus config package sheet is not canonical row JSON")
        workbook[sheet] = decoded
    if not workbook:
        raise IntegrityViolation("Aureus config package contains no workbook sheets")
    return workbook


def build_aureus_config_exporter(
    registry: ImmutablePlatformRegistry,
) -> AureusConfigExporter:
    """Compose the Aureus exporter over the frozen platform profile catalog."""

    return AureusConfigExporter(details_resolver=build_config_export_details_resolver(registry))


__all__ = [
    "AureusConfigExporter",
    "ConfigExportDetailsResolver",
    "build_aureus_config_exporter",
    "build_config_export_details_resolver",
    "decode_aureus_config_workbook",
]
