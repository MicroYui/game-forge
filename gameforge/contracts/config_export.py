"""Deterministic configuration-export package wire contracts."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.execution_profiles import ProfileRefV1


MAX_CONFIG_EXPORT_PATH_LENGTH = 1024
MAX_CONFIG_EXPORT_MEDIA_TYPE_LENGTH = 255
MAX_CONFIG_EXPORT_ID_LENGTH = 512
MAX_CONFIG_EXPORT_FILES = 1024
MAX_CONFIG_EXPORT_FILE_BYTES = 16 * 1024 * 1024
MAX_CONFIG_EXPORT_PACKAGE_BYTES = 64 * 1024 * 1024

RelativePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_CONFIG_EXPORT_PATH_LENGTH),
]
MediaType = Annotated[
    str,
    StringConstraints(min_length=3, max_length=MAX_CONFIG_EXPORT_MEDIA_TYPE_LENGTH),
]
BoundedId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_CONFIG_EXPORT_ID_LENGTH),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BoundedFileSize = Annotated[int, Field(ge=0, le=MAX_CONFIG_EXPORT_FILE_BYTES)]

_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:/")
_PACKAGE_MAGIC = b"GAMEFORGE-CONFIG-EXPORT\x00\x01"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )


def _canonical_relative_path(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or len(normalized) > MAX_CONFIG_EXPORT_PATH_LENGTH:
        raise ValueError("relative_path must be non-empty and bounded")
    if "\x00" in normalized or "\\" in normalized:
        raise ValueError("relative_path contains an ambiguous separator")
    if normalized.startswith("/") or _WINDOWS_DRIVE_PREFIX.match(normalized):
        raise ValueError("relative_path must not be absolute")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative_path contains an unsafe or ambiguous segment")
    return normalized


class ConfigExportFileV1(_FrozenModel):
    relative_path: RelativePath
    media_type: MediaType
    content_sha256: Sha256Hex
    size_bytes: BoundedFileSize
    content_bytes: bytes = Field(max_length=MAX_CONFIG_EXPORT_FILE_BYTES)

    @field_validator("relative_path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _canonical_relative_path(value)

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str) -> str:
        if value != value.strip() or value.count("/") != 1:
            raise ValueError("media_type must be a canonical type/subtype value")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("media_type must not contain control characters")
        return value

    @model_validator(mode="after")
    def _content_binding(self) -> ConfigExportFileV1:
        if self.size_bytes != len(self.content_bytes):
            raise ValueError("size_bytes does not match content_bytes")
        if self.content_sha256 != sha256_lowerhex(self.content_bytes):
            raise ValueError("content_sha256 does not match content_bytes")
        return self


class ConfigExportPackageV1(_FrozenModel):
    package_schema_version: Literal["config-export-package@1"] = "config-export-package@1"
    export_profile: ProfileRefV1
    target_environment_profile: ProfileRefV1
    env_contract_version: BoundedId
    source_preview_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId
    format_schema_id: BoundedId
    files: tuple[ConfigExportFileV1, ...] = Field(
        min_length=1,
        max_length=MAX_CONFIG_EXPORT_FILES,
    )

    @field_validator("files")
    @classmethod
    def _canonical_files(
        cls, value: tuple[ConfigExportFileV1, ...]
    ) -> tuple[ConfigExportFileV1, ...]:
        paths = [item.relative_path for item in value]
        if len(paths) != len(set(paths)):
            raise ValueError("files must have unique NFC relative_path values")
        return tuple(sorted(value, key=lambda item: item.relative_path))

    @model_validator(mode="after")
    def _package_size(self) -> ConfigExportPackageV1:
        if sum(item.size_bytes for item in self.files) > MAX_CONFIG_EXPORT_PACKAGE_BYTES:
            raise ValueError("config export package content exceeds the hard byte limit")
        return self


def _manifest(package: ConfigExportPackageV1) -> dict[str, object]:
    return {
        "package_schema_version": package.package_schema_version,
        "export_profile": package.export_profile.model_dump(mode="json"),
        "target_environment_profile": package.target_environment_profile.model_dump(mode="json"),
        "env_contract_version": package.env_contract_version,
        "source_preview_artifact_id": package.source_preview_artifact_id,
        "constraint_snapshot_artifact_id": package.constraint_snapshot_artifact_id,
        "format_schema_id": package.format_schema_id,
        "files": [
            {
                "relative_path": item.relative_path,
                "media_type": item.media_type,
                "content_sha256": item.content_sha256,
                "size_bytes": item.size_bytes,
            }
            for item in package.files
        ],
    }


def canonical_config_export_bytes(package: ConfigExportPackageV1) -> bytes:
    """Frame a package manifest and its raw file bytes without encoding ambiguity."""

    manifest = canonical_json(_manifest(package)).encode("utf-8")
    chunks = [_PACKAGE_MAGIC, len(manifest).to_bytes(8, "big"), manifest]
    for item in package.files:
        chunks.extend((item.size_bytes.to_bytes(8, "big"), item.content_bytes))
    return b"".join(chunks)
