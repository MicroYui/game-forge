"""Content-bound deployment authority for verified ``cassette@1`` imports.

The API and worker are separate processes, so an in-memory authority assembled by
one composition root is not a production authority for the other. This module
defines bounded, content-addressed shards that both processes can merge into one
immutable resolver. It contains no credentials and does not change historical
cassette bytes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from enum import Enum
import json
import os
from pathlib import Path
import stat
from typing import Annotated, Any, Literal, TypeVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cassette_import import (
    LegacyCassetteInputBindingV1,
    LegacyCassettePolicyBindingV1,
    LegacyCassetteProfileBindingV1,
    LegacyCassetteSchemaBindingV1,
    LegacyImportVerificationPolicyRegistryV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.model_router import ModelRequestV1
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    canonical_model_snapshot_id,
)
from gameforge.runtime.cassette.legacy_import import InMemoryLegacyImportAuthority

NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
PositiveInt = Annotated[int, Field(ge=1)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

# The manifest is a trusted startup input, but it still needs hard resource bounds.
# The per-collection cap matches the retained-authority bound already used by the
# lineage contracts; the total UTF-8 cap prevents their maxima from multiplying.
MAX_LEGACY_IMPORT_AUTHORITY_BINDINGS = 32_768
MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_CHARS = 4096
MAX_LEGACY_RENDERED_REQUEST_BYTES = 2 * 1024 * 1024
MAX_LEGACY_RENDERED_REQUEST_MESSAGES = 4096
MAX_LEGACY_RENDERED_REQUEST_TOOLS = 4096
LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV = "GAMEFORGE_LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class LegacyRenderedRequestBindingV1(_FrozenModel):
    artifact_id: NonEmptyStr
    request: ModelRequestV1

    @field_validator("request", mode="before")
    @classmethod
    def _strict_bounded_request(cls, value: object) -> object:
        payload = value.model_dump(mode="python") if isinstance(value, BaseModel) else value
        if not isinstance(payload, Mapping):
            raise ValueError("legacy rendered request must be an object")
        allowed = {
            "model_router_schema_version",
            "model_snapshot",
            "messages",
            "params",
            "tool_schemas",
            "agent_node_id",
            "prompt_version",
            "cache_key",
        }
        if set(payload) - allowed:
            raise ValueError("legacy rendered request contains unknown fields")
        snapshot = payload.get("model_snapshot")
        if isinstance(snapshot, BaseModel):
            snapshot = snapshot.model_dump(mode="python")
        if not isinstance(snapshot, Mapping) or set(snapshot) - {
            "provider",
            "model",
            "snapshot_tag",
        }:
            raise ValueError("legacy rendered request model snapshot is invalid")
        messages = payload.get("messages", ())
        tools = payload.get("tool_schemas", ())
        if (
            not isinstance(messages, (list, tuple))
            or len(messages) > MAX_LEGACY_RENDERED_REQUEST_MESSAGES
        ):
            raise ValueError("legacy rendered request message count exceeds its bound")
        if not isinstance(tools, (list, tuple)) or len(tools) > MAX_LEGACY_RENDERED_REQUEST_TOOLS:
            raise ValueError("legacy rendered request tool count exceeds its bound")
        for message in messages:
            if isinstance(message, BaseModel):
                message = message.model_dump(mode="python")
            if not isinstance(message, Mapping) or set(message) - {
                "role",
                "content",
                "tool_calls",
            }:
                raise ValueError("legacy rendered request message is invalid")
        for tool in tools:
            if isinstance(tool, BaseModel):
                tool = tool.model_dump(mode="python")
            if not isinstance(tool, Mapping) or set(tool) - {"name", "version"}:
                raise ValueError("legacy rendered request tool schema is invalid")
        try:
            encoded = json.dumps(
                _json_data(payload),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ValueError("legacy rendered request must be bounded JSON") from exc
        if len(encoded) > MAX_LEGACY_RENDERED_REQUEST_BYTES:
            raise ValueError("legacy rendered request exceeds its byte bound")
        return payload

    @model_validator(mode="after")
    def _v1_request(self) -> LegacyRenderedRequestBindingV1:
        if self.request.model_router_schema_version != "model-router@1":
            raise ValueError("legacy rendered authority only accepts model-router@1")
        return self


class LegacyFrozenVersionTupleBindingV1(_FrozenModel):
    source_suite_id: NonEmptyStr
    source_case_id: NonEmptyStr
    version_tuple: VersionTuple


class LegacyCallToolVersionBindingV1(_FrozenModel):
    source_suite_id: NonEmptyStr
    source_case_id: NonEmptyStr
    source_call_ordinal: PositiveInt
    tool_version: NonEmptyStr


def _json_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _json_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_data(item) for item in value]
    return value


_MANIFEST_SORT_KEYS = {
    "model_catalogs": lambda item: (item["catalog_version"], item["catalog_digest"]),
    "input_bindings": lambda item: (item["binding_key"], item["artifact_id"]),
    "profile_bindings": lambda item: (
        item["field_path"],
        item["profile_id"],
        item["profile_version"],
        item["catalog_version"],
        item["catalog_digest"],
    ),
    "policy_bindings": lambda item: (
        item["binding_key"],
        item["policy_kind"],
        item["policy_id"],
        item["policy_version"],
    ),
    "schema_bindings": lambda item: (item["binding_key"], item["schema_id"]),
    "rendered_requests": lambda item: item["artifact_id"],
    "frozen_version_tuples": lambda item: (
        item["source_suite_id"],
        item["source_case_id"],
    ),
    "call_tool_versions": lambda item: (
        item["source_suite_id"],
        item["source_case_id"],
        item["source_call_ordinal"],
    ),
}


def _canonical_manifest_payload(value: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    payload = dict(_json_data(value))
    payload.pop("manifest_digest", None)
    payload.setdefault(
        "manifest_schema_version",
        "legacy-import-authority-manifest@1",
    )
    for field_name, key in _MANIFEST_SORT_KEYS.items():
        payload[field_name] = sorted(payload.get(field_name, ()), key=key)
    return payload


def compute_legacy_import_authority_manifest_digest(
    value: Mapping[str, Any] | BaseModel,
) -> str:
    return canonical_sha256(_canonical_manifest_payload(value))


class LegacyImportAuthorityManifestV1(_FrozenModel):
    """One independently bounded shard of retained legacy replay preimages."""

    manifest_schema_version: Literal["legacy-import-authority-manifest@1"] = (
        "legacy-import-authority-manifest@1"
    )
    authority_version: NonEmptyStr
    verification_policy_registry: LegacyImportVerificationPolicyRegistryV1
    model_catalogs: tuple[ModelCatalogSnapshotV1, ...] = Field(
        max_length=MAX_LEGACY_IMPORT_AUTHORITY_BINDINGS,
    )
    input_bindings: tuple[LegacyCassetteInputBindingV1, ...] = Field(
        max_length=MAX_LEGACY_IMPORT_AUTHORITY_BINDINGS
    )
    profile_bindings: tuple[LegacyCassetteProfileBindingV1, ...] = Field(
        max_length=MAX_LEGACY_IMPORT_AUTHORITY_BINDINGS
    )
    policy_bindings: tuple[LegacyCassettePolicyBindingV1, ...] = Field(
        max_length=MAX_LEGACY_IMPORT_AUTHORITY_BINDINGS
    )
    schema_bindings: tuple[LegacyCassetteSchemaBindingV1, ...] = Field(
        max_length=MAX_LEGACY_IMPORT_AUTHORITY_BINDINGS
    )
    rendered_requests: tuple[LegacyRenderedRequestBindingV1, ...] = Field(
        max_length=MAX_LEGACY_IMPORT_AUTHORITY_BINDINGS
    )
    frozen_version_tuples: tuple[LegacyFrozenVersionTupleBindingV1, ...] = Field(
        max_length=MAX_LEGACY_IMPORT_AUTHORITY_BINDINGS
    )
    call_tool_versions: tuple[LegacyCallToolVersionBindingV1, ...] = Field(
        max_length=MAX_LEGACY_IMPORT_AUTHORITY_BINDINGS
    )
    manifest_digest: Sha256Hex

    @field_validator("model_catalogs")
    @classmethod
    def _canonical_catalogs(
        cls,
        value: tuple[ModelCatalogSnapshotV1, ...],
    ) -> tuple[ModelCatalogSnapshotV1, ...]:
        versions = [item.catalog_version for item in value]
        if len(versions) != len(set(versions)):
            raise ValueError("legacy authority model catalog versions must be unique")
        return tuple(sorted(value, key=lambda item: (item.catalog_version, item.catalog_digest)))

    @field_validator("input_bindings")
    @classmethod
    def _canonical_inputs(
        cls,
        value: tuple[LegacyCassetteInputBindingV1, ...],
    ) -> tuple[LegacyCassetteInputBindingV1, ...]:
        return _stable_unique(
            value,
            key=lambda item: (item.binding_key, item.artifact_id),
            label="input binding resolver identities",
        )

    @field_validator("profile_bindings")
    @classmethod
    def _canonical_profiles(
        cls,
        value: tuple[LegacyCassetteProfileBindingV1, ...],
    ) -> tuple[LegacyCassetteProfileBindingV1, ...]:
        return _stable_unique(
            value,
            key=lambda item: (
                item.field_path,
                item.profile_id,
                item.profile_version,
                item.catalog_version,
                item.catalog_digest,
            ),
            label="profile binding resolver identities",
        )

    @field_validator("policy_bindings")
    @classmethod
    def _canonical_policies(
        cls,
        value: tuple[LegacyCassettePolicyBindingV1, ...],
    ) -> tuple[LegacyCassettePolicyBindingV1, ...]:
        return _stable_unique(
            value,
            key=lambda item: (
                item.binding_key,
                item.policy_kind,
                item.policy_id,
                item.policy_version,
            ),
            label="policy binding resolver identities",
        )

    @field_validator("schema_bindings")
    @classmethod
    def _canonical_schemas(
        cls,
        value: tuple[LegacyCassetteSchemaBindingV1, ...],
    ) -> tuple[LegacyCassetteSchemaBindingV1, ...]:
        return _stable_unique(
            value,
            key=lambda item: (item.binding_key, item.schema_id),
            label="schema binding resolver identities",
        )

    @field_validator("rendered_requests")
    @classmethod
    def _canonical_rendered_requests(
        cls,
        value: tuple[LegacyRenderedRequestBindingV1, ...],
    ) -> tuple[LegacyRenderedRequestBindingV1, ...]:
        return _stable_unique(
            value,
            key=lambda item: item.artifact_id,
            label="rendered request artifact identities",
        )

    @field_validator("frozen_version_tuples")
    @classmethod
    def _canonical_frozen_tuples(
        cls,
        value: tuple[LegacyFrozenVersionTupleBindingV1, ...],
    ) -> tuple[LegacyFrozenVersionTupleBindingV1, ...]:
        return _stable_unique(
            value,
            key=lambda item: (item.source_suite_id, item.source_case_id),
            label="frozen version tuple resolver identities",
        )

    @field_validator("call_tool_versions")
    @classmethod
    def _canonical_call_tools(
        cls,
        value: tuple[LegacyCallToolVersionBindingV1, ...],
    ) -> tuple[LegacyCallToolVersionBindingV1, ...]:
        return _stable_unique(
            value,
            key=lambda item: (
                item.source_suite_id,
                item.source_case_id,
                item.source_call_ordinal,
            ),
            label="call tool version resolver identities",
        )

    @model_validator(mode="after")
    def _content_bound_shard(self) -> LegacyImportAuthorityManifestV1:
        if not self.verification_policy_registry.policies:
            raise ValueError("legacy import authority must retain a verification policy")
        if self.manifest_digest != compute_legacy_import_authority_manifest_digest(self):
            raise ValueError("legacy import authority manifest digest differs")
        return self

    @classmethod
    def create(cls, **values: Any) -> LegacyImportAuthorityManifestV1:
        payload = {
            "manifest_schema_version": "legacy-import-authority-manifest@1",
            **values,
        }
        payload.pop("manifest_digest", None)
        return cls(
            **payload,
            manifest_digest=compute_legacy_import_authority_manifest_digest(payload),
        )

    def build_authority(self) -> ContentBoundLegacyImportAuthority:
        """Build a defensive in-memory resolver from the verified manifest."""

        return build_legacy_import_authority((self,))


def _stable_unique(value: tuple[Any, ...], *, key: Any, label: str) -> tuple[Any, ...]:
    identities = [key(item) for item in value]
    if len(identities) != len(set(identities)):
        raise ValueError(f"legacy authority {label} must be unique")
    return tuple(sorted(value, key=key))


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _defensive_model_copy(value: _ModelT) -> _ModelT:
    return cast(_ModelT, type(value).model_validate(value.model_dump(mode="python")))


class ContentBoundLegacyImportAuthority:
    """Immutable production facade over the mutable offline/test authority.

    The delegate is name-mangled and never exposed. Every model that crosses the
    facade is revalidated into caller-owned memory, so readiness cannot be made
    stale through a public resolver or map mutation after the manifest is loaded.
    """

    __slots__ = ("__delegate",)

    def __init__(self, delegate: InMemoryLegacyImportAuthority) -> None:
        self.__delegate = delegate

    @property
    def verification_policy_registry(
        self,
    ) -> LegacyImportVerificationPolicyRegistryV1:
        return _defensive_model_copy(self.__delegate.verification_policy_registry)

    def resolve_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None:
        return self.__delegate.resolve_model_catalog(catalog_version, catalog_digest)

    def resolve_input_binding(
        self,
        binding_key: str,
        artifact_id: str,
    ) -> LegacyCassetteInputBindingV1 | None:
        return self.__delegate.resolve_input_binding(binding_key, artifact_id)

    def resolve_profile_binding(
        self,
        field_path: str,
        profile_id: str,
        profile_version: int,
        catalog_version: int,
        catalog_digest: str,
    ) -> LegacyCassetteProfileBindingV1 | None:
        return self.__delegate.resolve_profile_binding(
            field_path,
            profile_id,
            profile_version,
            catalog_version,
            catalog_digest,
        )

    def resolve_policy_binding(
        self,
        binding_key: str,
        policy_kind: str,
        policy_id: str,
        policy_version: int,
    ) -> LegacyCassettePolicyBindingV1 | None:
        return self.__delegate.resolve_policy_binding(
            binding_key,
            policy_kind,
            policy_id,
            policy_version,
        )

    def resolve_schema_binding(
        self,
        binding_key: str,
        schema_id: str,
    ) -> LegacyCassetteSchemaBindingV1 | None:
        return self.__delegate.resolve_schema_binding(binding_key, schema_id)

    def resolve_rendered_request(self, artifact_id: str) -> ModelRequestV1 | None:
        return self.__delegate.resolve_rendered_request(artifact_id)

    def resolve_frozen_version_tuple(
        self,
        source_suite_id: str,
        source_case_id: str,
    ) -> VersionTuple | None:
        return self.__delegate.resolve_frozen_version_tuple(
            source_suite_id,
            source_case_id,
        )

    def resolve_call_tool_version(
        self,
        source_suite_id: str,
        source_case_id: str,
        source_call_ordinal: int,
    ) -> str | None:
        return self.__delegate.resolve_call_tool_version(
            source_suite_id,
            source_case_id,
            source_call_ordinal,
        )


def _same_retained_value(left: object, right: object) -> bool:
    if isinstance(left, BaseModel) and isinstance(right, BaseModel):
        return type(left) is type(right) and left.model_dump(mode="json") == right.model_dump(
            mode="json"
        )
    return left == right


def _merge_exact(
    target: dict[Any, Any],
    key: Any,
    value: Any,
    *,
    label: str,
) -> None:
    if key not in target:
        target[key] = value
        return
    if not _same_retained_value(target[key], value):
        raise IntegrityViolation(
            f"legacy import authority shards conflict on {label}",
            resolver_identity=str(key),
        )


def build_legacy_import_authority(
    manifests: Sequence[LegacyImportAuthorityManifestV1],
) -> ContentBoundLegacyImportAuthority:
    """Merge bounded shards into one exact resolver without a history-wide cap."""

    try:
        retained = tuple(
            LegacyImportAuthorityManifestV1.model_validate(item.model_dump(mode="python"))
            for item in manifests
        )
    except (AttributeError, RecursionError, TypeError, ValueError) as exc:
        raise IntegrityViolation("legacy import authority shard is invalid") from exc
    if not retained:
        raise IntegrityViolation("legacy import authority manifest set is empty")
    authority_versions = {item.authority_version for item in retained}
    if len(authority_versions) != 1:
        raise IntegrityViolation("legacy import authority shards have different authority versions")
    registry = retained[0].verification_policy_registry
    registry_wire = registry.model_dump(mode="json")
    if any(
        item.verification_policy_registry.model_dump(mode="json") != registry_wire
        for item in retained[1:]
    ):
        raise IntegrityViolation(
            "legacy import authority shards have different verification registries"
        )

    catalogs_by_version: dict[int, ModelCatalogSnapshotV1] = {}
    input_bindings: dict[tuple[str, str], LegacyCassetteInputBindingV1] = {}
    profile_bindings: dict[
        tuple[str, str, int, int, str],
        LegacyCassetteProfileBindingV1,
    ] = {}
    policy_bindings: dict[
        tuple[str, str, str, int],
        LegacyCassettePolicyBindingV1,
    ] = {}
    schema_bindings: dict[tuple[str, str], LegacyCassetteSchemaBindingV1] = {}
    rendered_requests: dict[str, ModelRequestV1] = {}
    frozen_version_tuples: dict[tuple[str, str], VersionTuple] = {}
    call_tool_versions: dict[tuple[str, str, int], str] = {}

    for shard in retained:
        for item in shard.model_catalogs:
            _merge_exact(
                catalogs_by_version,
                item.catalog_version,
                item,
                label="model catalog version",
            )
        for item in shard.input_bindings:
            _merge_exact(
                input_bindings,
                (item.binding_key, item.artifact_id),
                item,
                label="input binding",
            )
        for item in shard.profile_bindings:
            _merge_exact(
                profile_bindings,
                (
                    item.field_path,
                    item.profile_id,
                    item.profile_version,
                    item.catalog_version,
                    item.catalog_digest,
                ),
                item,
                label="profile binding",
            )
        for item in shard.policy_bindings:
            _merge_exact(
                policy_bindings,
                (
                    item.binding_key,
                    item.policy_kind,
                    item.policy_id,
                    item.policy_version,
                ),
                item,
                label="policy binding",
            )
        for item in shard.schema_bindings:
            _merge_exact(
                schema_bindings,
                (item.binding_key, item.schema_id),
                item,
                label="schema binding",
            )
        for item in shard.rendered_requests:
            _merge_exact(
                rendered_requests,
                item.artifact_id,
                item.request,
                label="rendered request",
            )
        for item in shard.frozen_version_tuples:
            _merge_exact(
                frozen_version_tuples,
                (item.source_suite_id, item.source_case_id),
                item.version_tuple,
                label="frozen version tuple",
            )
        for item in shard.call_tool_versions:
            _merge_exact(
                call_tool_versions,
                (
                    item.source_suite_id,
                    item.source_case_id,
                    item.source_call_ordinal,
                ),
                item.tool_version,
                label="call tool version",
            )

    input_artifact_content: dict[str, object] = {}
    for item in input_bindings.values():
        _merge_exact(
            input_artifact_content,
            item.artifact_id,
            (
                item.payload_hash,
                item.version_tuple.model_dump(mode="json"),
            ),
            label="cross-key input artifact identity",
        )
    profile_content: dict[tuple[str, int], object] = {}
    profile_catalog_digests: dict[int, str] = {}
    for item in profile_bindings.values():
        _merge_exact(
            profile_content,
            (item.profile_id, item.profile_version),
            item.profile_payload_hash,
            label="cross-key profile identity",
        )
        _merge_exact(
            profile_catalog_digests,
            item.catalog_version,
            item.catalog_digest,
            label="execution profile catalog version",
        )
    policy_content: dict[tuple[str, str, int], str] = {}
    for item in policy_bindings.values():
        _merge_exact(
            policy_content,
            (item.policy_kind, item.policy_id, item.policy_version),
            item.policy_digest,
            label="cross-key policy identity",
        )

    catalogs = {
        (item.catalog_version, item.catalog_digest): item for item in catalogs_by_version.values()
    }
    if not catalogs:
        raise IntegrityViolation("legacy import authority has no model catalog")
    model_snapshot_ids = {
        descriptor.model_snapshot for catalog in catalogs.values() for descriptor in catalog.models
    }
    for request in rendered_requests.values():
        try:
            model_snapshot_id = canonical_model_snapshot_id(request.model_snapshot)
        except ValueError as exc:
            raise IntegrityViolation("legacy rendered request model preimage is invalid") from exc
        if model_snapshot_id not in model_snapshot_ids:
            raise IntegrityViolation(
                "legacy rendered request model is absent from retained catalogs"
            )
    ordinals_by_source: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    for source_suite_id, source_case_id, ordinal in call_tool_versions:
        source = (source_suite_id, source_case_id)
        if source not in frozen_version_tuples:
            raise IntegrityViolation("legacy call tool version has no frozen version tuple")
        if (
            call_tool_versions[(source_suite_id, source_case_id, ordinal)]
            != frozen_version_tuples[source].tool_version
        ):
            raise IntegrityViolation(
                "legacy call tool version differs from its frozen version tuple"
            )
        ordinals_by_source[source].append(ordinal)
    for ordinals in ordinals_by_source.values():
        ordered = tuple(sorted(ordinals))
        if ordered != tuple(range(1, len(ordered) + 1)):
            raise IntegrityViolation("legacy call tool ordinals must start at 1 and be contiguous")

    return ContentBoundLegacyImportAuthority(
        InMemoryLegacyImportAuthority(
            verification_policy_registry=registry,
            model_catalogs=catalogs,
            input_bindings=input_bindings,
            profile_bindings=profile_bindings,
            policy_bindings=policy_bindings,
            schema_bindings=schema_bindings,
            rendered_requests=rendered_requests,
            frozen_version_tuples=frozen_version_tuples,
            call_tool_versions=call_tool_versions,
        )
    )


def parse_legacy_import_authority_manifest(raw: str) -> LegacyImportAuthorityManifestV1:
    try:
        payload = json.loads(raw)
        return LegacyImportAuthorityManifestV1.model_validate(payload)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise IntegrityViolation("legacy import authority manifest is invalid") from exc


def _validated_manifest_path(path: str | Path) -> Path:
    try:
        raw_path = os.fspath(path)
    except TypeError as exc:
        raise IntegrityViolation("legacy import authority manifest path is invalid") from exc
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or len(raw_path) > MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_path)
    ):
        raise IntegrityViolation("legacy import authority manifest path is invalid")
    try:
        return Path(raw_path).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        raise IntegrityViolation("legacy import authority manifest path is invalid") from exc


def load_legacy_import_authority_manifest(
    path: str | Path,
) -> LegacyImportAuthorityManifestV1:
    manifest_path = _validated_manifest_path(path)
    try:
        expected = manifest_path.lstat()
        if not stat.S_ISREG(expected.st_mode):
            raise IntegrityViolation("legacy import authority manifest must be a regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(manifest_path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            actual = os.fstat(handle.fileno())
            if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                raise IntegrityViolation("legacy import authority manifest changed while opening")
            payload = handle.read(MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_BYTES + 1)
    except IntegrityViolation:
        raise
    except (OSError, ValueError) as exc:
        raise IntegrityViolation("legacy import authority manifest is unreadable") from exc
    if len(payload) > MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_BYTES:
        raise IntegrityViolation("legacy import authority manifest exceeds its byte bound")
    try:
        raw = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityViolation("legacy import authority manifest is not UTF-8") from exc
    return parse_legacy_import_authority_manifest(raw)


def _legacy_import_authority_manifest_paths(path: Path) -> tuple[Path, ...]:
    try:
        root = path.lstat()
    except (OSError, ValueError) as exc:
        raise IntegrityViolation("legacy import authority manifest path is unreadable") from exc
    if stat.S_ISLNK(root.st_mode):
        raise IntegrityViolation("legacy import authority manifest path cannot be a symbolic link")
    if stat.S_ISREG(root.st_mode):
        return (path,)
    if not stat.S_ISDIR(root.st_mode):
        raise IntegrityViolation(
            "legacy import authority manifest path must be a regular file or directory"
        )
    try:
        entries = tuple(sorted(path.iterdir(), key=lambda entry: entry.name))
    except (OSError, ValueError) as exc:
        raise IntegrityViolation(
            "legacy import authority manifest directory is unreadable"
        ) from exc
    if not entries:
        raise IntegrityViolation("legacy import authority manifest directory is empty")
    for entry in entries:
        try:
            entry_stat = entry.lstat()
        except (OSError, ValueError) as exc:
            raise IntegrityViolation(
                "legacy import authority manifest directory is unreadable"
            ) from exc
        if not stat.S_ISREG(entry_stat.st_mode):
            raise IntegrityViolation(
                "legacy import authority directory must contain only regular manifest files"
            )
    return entries


def load_legacy_import_authority(
    path: str | Path,
) -> ContentBoundLegacyImportAuthority:
    """Load one manifest file or a flat directory of independently bounded shards."""

    manifest_paths = _legacy_import_authority_manifest_paths(_validated_manifest_path(path))
    manifests = tuple(
        load_legacy_import_authority_manifest(manifest_path) for manifest_path in manifest_paths
    )
    return build_legacy_import_authority(manifests)


__all__ = [
    "ContentBoundLegacyImportAuthority",
    "LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV",
    "LegacyCallToolVersionBindingV1",
    "LegacyFrozenVersionTupleBindingV1",
    "LegacyImportAuthorityManifestV1",
    "LegacyRenderedRequestBindingV1",
    "MAX_LEGACY_IMPORT_AUTHORITY_BINDINGS",
    "MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_BYTES",
    "MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_CHARS",
    "MAX_LEGACY_RENDERED_REQUEST_BYTES",
    "build_legacy_import_authority",
    "compute_legacy_import_authority_manifest_digest",
    "load_legacy_import_authority",
    "load_legacy_import_authority_manifest",
    "parse_legacy_import_authority_manifest",
]
