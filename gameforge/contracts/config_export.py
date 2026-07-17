"""Deterministic configuration-export package wire contracts."""

from __future__ import annotations

import json
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
MAX_CONFIG_EXPORT_MANIFEST_BYTES = 8 * 1024 * 1024

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

_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
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
    if "\\" in normalized:
        raise ValueError("relative_path contains an ambiguous separator")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
        raise ValueError("relative_path contains a control or surrogate character")
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
        manifest = canonical_json(_manifest(self)).encode("utf-8")
        if len(manifest) > MAX_CONFIG_EXPORT_MANIFEST_BYTES:
            raise ValueError("config export package exceeds the manifest byte limit")
        if _framed_package_size(self, manifest_size=len(manifest)) > (
            MAX_CONFIG_EXPORT_PACKAGE_BYTES
        ):
            raise ValueError("config export package exceeds the framed byte limit")
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


def _framed_package_size(
    package: ConfigExportPackageV1,
    *,
    manifest_size: int,
) -> int:
    return (
        len(_PACKAGE_MAGIC)
        + 8
        + manifest_size
        + 8 * len(package.files)
        + sum(item.size_bytes for item in package.files)
    )


def canonical_config_export_bytes(package: ConfigExportPackageV1) -> bytes:
    """Frame a package manifest and its raw file bytes without encoding ambiguity."""

    manifest = canonical_json(_manifest(package)).encode("utf-8")
    if len(manifest) > MAX_CONFIG_EXPORT_MANIFEST_BYTES:
        raise ValueError("config export package exceeds the manifest byte limit")
    if _framed_package_size(package, manifest_size=len(manifest)) > (
        MAX_CONFIG_EXPORT_PACKAGE_BYTES
    ):
        raise ValueError("config export package exceeds the framed byte limit")
    chunks = [_PACKAGE_MAGIC, len(manifest).to_bytes(8, "big"), manifest]
    for item in package.files:
        chunks.extend((item.size_bytes.to_bytes(8, "big"), item.content_bytes))
    return b"".join(chunks)


def decode_config_export_bytes(blob: bytes) -> ConfigExportPackageV1:
    """Decode and re-verify the canonical framed package representation.

    The manifest intentionally omits raw file bytes, so decoding walks its exact
    file order and consumes one length-prefixed byte string per manifest row.  A
    successful decode proves the manifest is canonical, every file hash/size is
    correct, and no trailing or unframed bytes were accepted.
    """

    if not isinstance(blob, bytes):
        raise TypeError("config export package must be bytes")
    if len(blob) > MAX_CONFIG_EXPORT_PACKAGE_BYTES:
        raise ValueError("config export package exceeds the framed byte limit")
    header_size = len(_PACKAGE_MAGIC) + 8
    if len(blob) < header_size or not blob.startswith(_PACKAGE_MAGIC):
        raise ValueError("config export package magic is invalid")
    manifest_size = int.from_bytes(blob[len(_PACKAGE_MAGIC) : header_size], "big")
    if not 1 <= manifest_size <= MAX_CONFIG_EXPORT_MANIFEST_BYTES:
        raise ValueError("config export manifest length is invalid")
    manifest_end = header_size + manifest_size
    if manifest_end > len(blob):
        raise ValueError("config export manifest is truncated")
    manifest_bytes = blob[header_size:manifest_end]
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("config export manifest is invalid JSON") from exc
    try:
        canonical_manifest = canonical_json(manifest).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("config export manifest is not canonical") from exc
    if not isinstance(manifest, dict) or canonical_manifest != manifest_bytes:
        raise ValueError("config export manifest is not canonical")
    file_manifests = manifest.get("files")
    if not isinstance(file_manifests, list):
        raise ValueError("config export manifest files must be an array")
    if not 1 <= len(file_manifests) <= MAX_CONFIG_EXPORT_FILES:
        raise ValueError("config export manifest file count is invalid")

    offset = manifest_end
    files: list[dict[str, object]] = []
    for item in file_manifests:
        if not isinstance(item, dict):
            raise ValueError("config export file manifest must be an object")
        if offset + 8 > len(blob):
            raise ValueError("config export file length is truncated")
        framed_size = int.from_bytes(blob[offset : offset + 8], "big")
        offset += 8
        if framed_size > MAX_CONFIG_EXPORT_FILE_BYTES or offset + framed_size > len(blob):
            raise ValueError("config export file content is truncated or oversized")
        content = blob[offset : offset + framed_size]
        offset += framed_size
        files.append({**item, "content_bytes": content})
    if offset != len(blob):
        raise ValueError("config export package contains trailing bytes")

    package = ConfigExportPackageV1.model_validate({**manifest, "files": files})
    if canonical_config_export_bytes(package) != blob:
        raise ValueError("config export package is not canonical")
    return package
