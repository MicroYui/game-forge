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

from dataclasses import dataclass, field
from typing import Callable, Mapping

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.config_export import (
    ConfigExportFileV1,
    ConfigExportPackageV1,
)
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import (
    ConfigExportProfileDetailsV1,
    ProfileRefV1,
)
from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter

# Resolve a config-export profile ref to its frozen details (format_schema_id,
# target environment profile, env_contract_version).
ConfigExportDetailsResolver = Callable[[ProfileRefV1], ConfigExportProfileDetailsV1]

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
        preview_snapshot_id: str,
        preview_payload: Mapping[str, object],
        constraint_snapshot_artifact_id: str,
        constraints: tuple[Constraint, ...],
    ) -> ConfigExportPackageV1:
        details = self.details_resolver(export_profile)
        snapshot = snapshot_from_canonical_view(dict(preview_payload))
        workbook = self.adapter.from_ir(snapshot)
        if not workbook:
            raise ValueError("preview snapshot has no exportable Aureus content")
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
    """Index the frozen ``config_export`` profile details from the profile catalog."""

    details: dict[ProfileRefV1, ConfigExportProfileDetailsV1] = {}
    for catalog in registry.list_execution_profile_catalogs():
        for profile in catalog.definitions:
            if isinstance(profile.details, ConfigExportProfileDetailsV1):
                details[profile.profile] = profile.details

    def resolve(export_profile: ProfileRefV1) -> ConfigExportProfileDetailsV1:
        try:
            return details[export_profile]
        except KeyError:
            raise KeyError(f"no config-export profile details for {export_profile!r}") from None

    return resolve


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
]
