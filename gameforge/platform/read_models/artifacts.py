"""Verified, bounded reads for immutable object-backed Artifact payloads."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol, get_args

from pydantic import ValidationError

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex, typed_canonical_json
from gameforge.contracts.errors import (
    DependencyUnavailable,
    IntegrityViolation,
    NotFound,
    PayloadTooLarge,
)
from gameforge.contracts.jobs import MAX_COLLECTION_ITEMS, MAX_JSON_DEPTH
from gameforge.contracts.lineage import (
    ArtifactKind,
    ArtifactV1,
    ArtifactV2,
    ObjectBinding,
    parse_artifact,
)
from gameforge.contracts.storage import ObjectBindingRepository, ObjectStat, ObjectStore


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_SCHEMA_ID_LENGTH = 512
_MAX_JSON_STRING_LENGTH = 4096
_READ_CHUNK_BYTES = 64 * 1024
_ARTIFACT_KINDS = frozenset(get_args(ArtifactKind))


class ArtifactReadRepository(Protocol):
    """Exact immutable Artifact authority used by the read path."""

    def get(self, artifact_id: str) -> ArtifactV1 | ArtifactV2 | None: ...


class ArtifactPayloadBindingProvider(Protocol):
    """Server-trusted publication schema binding; never supplied by a client."""

    def resolve(self, artifact_id: str) -> TrustedArtifactPayloadBinding | None: ...


def _metadata_digest(artifact: ArtifactV2) -> str:
    metadata = artifact.model_dump(mode="json")["meta"]
    return sha256_lowerhex(typed_canonical_json(metadata).encode("utf-8"))


@dataclass(frozen=True, slots=True)
class TrustedArtifactPayloadBinding:
    """Immutable server-side binding for schema and Artifact envelope authority.

    Payload bytes are deliberately not allowed to select their own kind, schema,
    or metadata.  A publication/read authority builds this record from the exact
    retained Artifact and its exact publication schema.
    """

    artifact_id: str
    artifact_kind: ArtifactKind
    payload_hash: str
    payload_schema_id: str
    metadata_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, str) or not self.artifact_id:
            raise ValueError("artifact_id must be a non-empty string")
        if self.artifact_kind not in _ARTIFACT_KINDS:
            raise ValueError("artifact_kind is not a frozen ArtifactKind")
        if not isinstance(self.payload_hash, str) or _SHA256.fullmatch(self.payload_hash) is None:
            raise ValueError("payload_hash must be a lowercase SHA-256 digest")
        if (
            not isinstance(self.payload_schema_id, str)
            or not self.payload_schema_id
            or len(self.payload_schema_id) > _MAX_SCHEMA_ID_LENGTH
        ):
            raise ValueError("payload_schema_id must be a non-empty bounded string")
        if (
            not isinstance(self.metadata_digest, str)
            or _SHA256.fullmatch(self.metadata_digest) is None
        ):
            raise ValueError("metadata_digest must be a lowercase SHA-256 digest")

    @classmethod
    def for_artifact(
        cls,
        artifact: ArtifactV2,
        *,
        payload_schema_id: str,
    ) -> TrustedArtifactPayloadBinding:
        exact = _exact_artifact_v2(artifact)
        return cls(
            artifact_id=exact.artifact_id,
            artifact_kind=exact.kind,
            payload_hash=exact.payload_hash,
            payload_schema_id=payload_schema_id,
            metadata_digest=_metadata_digest(exact),
        )


@dataclass(frozen=True, slots=True)
class VerifiedArtifactPayload:
    """Verified payload plus server-authoritative envelope projection."""

    artifact: ArtifactV2
    object_binding: ObjectBinding
    payload_schema_id: str
    kind: ArtifactKind
    metadata: dict[str, Any]
    payload_bytes: bytes
    payload: dict[str, Any]


def _exact_artifact_v2(value: Any) -> ArtifactV2:
    if type(value) is not ArtifactV2:
        raise TypeError("artifact must be an exact ArtifactV2")
    wire = value.model_dump(mode="json")
    try:
        parsed = parse_artifact(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored ArtifactV2 violates its immutable contract",
            artifact_id=getattr(value, "artifact_id", None),
        ) from exc
    if type(parsed) is not ArtifactV2:
        raise IntegrityViolation("stored artifact discriminator changed during validation")
    if typed_canonical_json(parsed.model_dump(mode="json")) != typed_canonical_json(wire):
        raise IntegrityViolation(
            "stored ArtifactV2 wire differs after exact validation",
            artifact_id=parsed.artifact_id,
        )
    return parsed


def _validate_trusted_binding(
    binding: Any,
    artifact: ArtifactV2,
) -> TrustedArtifactPayloadBinding:
    if type(binding) is not TrustedArtifactPayloadBinding:
        raise IntegrityViolation(
            "trusted payload binding has an invalid type",
            artifact_id=artifact.artifact_id,
        )
    expected = (
        artifact.artifact_id,
        artifact.kind,
        artifact.payload_hash,
        _metadata_digest(artifact),
    )
    actual = (
        binding.artifact_id,
        binding.artifact_kind,
        binding.payload_hash,
        binding.metadata_digest,
    )
    if actual != expected:
        raise IntegrityViolation(
            "trusted payload binding differs from the exact ArtifactV2",
            artifact_id=artifact.artifact_id,
        )
    return binding


def _validate_object_binding(value: Any, artifact: ArtifactV2) -> ObjectBinding:
    if type(value) is not ObjectBinding:
        raise IntegrityViolation(
            "ObjectBinding has an invalid type",
            artifact_id=artifact.artifact_id,
        )
    if (
        value.status != "active"
        or value.object_ref != artifact.object_ref
        or value.location.key != artifact.object_ref.key
    ):
        raise IntegrityViolation(
            "active ObjectBinding does not match the exact ArtifactV2",
            artifact_id=artifact.artifact_id,
        )
    return value


def _validate_object_stat(
    value: Any,
    *,
    artifact: ArtifactV2,
    binding: ObjectBinding,
) -> ObjectStat:
    if type(value) is not ObjectStat:
        raise IntegrityViolation(
            "ObjectStore stat has an invalid type",
            artifact_id=artifact.artifact_id,
        )
    if value.ref != artifact.object_ref or value.location != binding.location:
        raise IntegrityViolation(
            "ObjectStore stat does not match the active binding and ArtifactV2",
            artifact_id=artifact.artifact_id,
        )
    return value


def _read_exact_bytes(
    store: ObjectStore,
    binding: ObjectBinding,
    *,
    artifact_id: str,
    expected_size: int,
) -> bytes:
    try:
        stream = store.open(binding.location)
    except FileNotFoundError as exc:
        raise IntegrityViolation(
            "bound object payload is missing",
            artifact_id=artifact_id,
        ) from exc
    except OSError as exc:
        raise DependencyUnavailable(
            "object payload cannot be opened",
            component="object_store",
            artifact_id=artifact_id,
        ) from exc
    if not callable(getattr(stream, "read", None)) or not callable(getattr(stream, "close", None)):
        raise IntegrityViolation(
            "ObjectStore.open did not return a binary stream",
            artifact_id=artifact_id,
        )

    chunks: list[bytes] = []
    observed_size = 0
    digest = hashlib.sha256()
    try:
        while observed_size <= expected_size:
            request_size = min(_READ_CHUNK_BYTES, expected_size + 1 - observed_size)
            chunk = stream.read(request_size)
            if not isinstance(chunk, bytes):
                raise IntegrityViolation(
                    "ObjectStore payload stream returned a non-bytes chunk",
                    artifact_id=artifact_id,
                )
            if not chunk:
                break
            observed_size += len(chunk)
            if observed_size > expected_size:
                raise IntegrityViolation(
                    "object payload byte length differs from ObjectRef",
                    artifact_id=artifact_id,
                    expected_size=expected_size,
                    observed_size=observed_size,
                )
            digest.update(chunk)
            chunks.append(chunk)
    except OSError as exc:
        raise DependencyUnavailable(
            "object payload cannot be read",
            component="object_store",
            artifact_id=artifact_id,
        ) from exc
    finally:
        try:
            stream.close()
        except OSError as exc:
            raise DependencyUnavailable(
                "object payload stream cannot be closed",
                component="object_store",
                artifact_id=artifact_id,
            ) from exc

    if observed_size != expected_size:
        raise IntegrityViolation(
            "object payload byte length differs from ObjectRef",
            artifact_id=artifact_id,
            expected_size=expected_size,
            observed_size=observed_size,
        )
    payload = b"".join(chunks)
    if digest.hexdigest() != binding.object_ref.sha256:
        raise IntegrityViolation(
            "object payload content hash differs from ObjectRef",
            artifact_id=artifact_id,
        )
    return payload


def _strict_canonical_json_object(
    payload: bytes,
    *,
    artifact_id: str,
) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise IntegrityViolation(
            "artifact payload is not one strict canonical JSON object",
            artifact_id=artifact_id,
        ) from exc
    if not isinstance(value, dict):
        raise IntegrityViolation(
            "artifact payload is not one strict canonical JSON object",
            artifact_id=artifact_id,
        )

    _validate_json_shape(value, artifact_id=artifact_id)
    try:
        canonical = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise IntegrityViolation(
            "artifact payload is not one strict canonical JSON object",
            artifact_id=artifact_id,
        ) from exc
    if canonical != payload:
        raise IntegrityViolation(
            "artifact payload is not one strict canonical JSON object",
            artifact_id=artifact_id,
        )
    return value


def _validate_json_shape(value: dict[str, Any], *, artifact_id: str) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise IntegrityViolation(
                "artifact payload exceeds the JSON depth limit",
                artifact_id=artifact_id,
            )
        if isinstance(item, str):
            if len(item) > _MAX_JSON_STRING_LENGTH:
                raise IntegrityViolation(
                    "artifact payload contains an oversized JSON string",
                    artifact_id=artifact_id,
                )
        elif isinstance(item, Mapping):
            if len(item) > MAX_COLLECTION_ITEMS:
                raise IntegrityViolation(
                    "artifact payload contains an oversized JSON object",
                    artifact_id=artifact_id,
                )
            for key, child in item.items():
                if len(key) > _MAX_JSON_STRING_LENGTH:
                    raise IntegrityViolation(
                        "artifact payload contains an oversized JSON object key",
                        artifact_id=artifact_id,
                    )
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            if len(item) > MAX_COLLECTION_ITEMS:
                raise IntegrityViolation(
                    "artifact payload contains an oversized JSON array",
                    artifact_id=artifact_id,
                )
            stack.extend((child, depth + 1) for child in item)


class ArtifactPayloadReader:
    """Resolve and verify one immutable ArtifactV2 payload without network access."""

    def __init__(
        self,
        *,
        artifacts: ArtifactReadRepository,
        trusted_bindings: ArtifactPayloadBindingProvider,
        object_bindings: ObjectBindingRepository,
        object_store: ObjectStore,
        max_payload_bytes: int,
    ) -> None:
        if (
            isinstance(max_payload_bytes, bool)
            or not isinstance(max_payload_bytes, int)
            or max_payload_bytes < 1
        ):
            raise ValueError("max_payload_bytes must be a positive integer")
        self._artifacts = artifacts
        self._trusted_bindings = trusted_bindings
        self._object_bindings = object_bindings
        self._object_store = object_store
        self._max_payload_bytes = max_payload_bytes

    def read(self, artifact_id: str) -> VerifiedArtifactPayload:
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("artifact_id must be a non-empty string")
        stored = self._artifacts.get(artifact_id)
        if stored is None:
            raise NotFound("artifact does not exist", artifact_id=artifact_id)
        if type(stored) is ArtifactV1:
            raise NotFound(
                "lineage@1 artifact has no object-backed payload",
                artifact_id=artifact_id,
            )
        artifact = _exact_artifact_v2(stored)
        if artifact.artifact_id != artifact_id:
            raise IntegrityViolation(
                "artifact repository returned a different identity",
                artifact_id=artifact_id,
            )

        trusted = self._trusted_bindings.resolve(artifact_id)
        if trusted is None:
            raise IntegrityViolation(
                "trusted payload binding is unavailable",
                artifact_id=artifact_id,
            )
        trusted = _validate_trusted_binding(trusted, artifact)

        try:
            binding = self._object_bindings.resolve(artifact.object_ref)
        except FileNotFoundError as exc:
            raise IntegrityViolation(
                "active ObjectBinding is unavailable",
                artifact_id=artifact_id,
            ) from exc
        binding = _validate_object_binding(binding, artifact)

        try:
            stat = self._object_store.stat(binding.location)
        except FileNotFoundError as exc:
            raise IntegrityViolation(
                "bound object payload is missing",
                artifact_id=artifact_id,
            ) from exc
        except OSError as exc:
            raise DependencyUnavailable(
                "object payload metadata is unavailable",
                component="object_store",
                artifact_id=artifact_id,
            ) from exc
        _validate_object_stat(stat, artifact=artifact, binding=binding)

        if artifact.object_ref.size_bytes > self._max_payload_bytes:
            raise PayloadTooLarge(
                "artifact payload exceeds the configured byte limit",
                artifact_id=artifact_id,
                size_bytes=artifact.object_ref.size_bytes,
                max_payload_bytes=self._max_payload_bytes,
            )
        payload_bytes = _read_exact_bytes(
            self._object_store,
            binding,
            artifact_id=artifact_id,
            expected_size=artifact.object_ref.size_bytes,
        )
        if sha256_lowerhex(payload_bytes) != artifact.payload_hash:
            raise IntegrityViolation(
                "object payload content hash differs from ArtifactV2",
                artifact_id=artifact_id,
            )
        payload = _strict_canonical_json_object(payload_bytes, artifact_id=artifact_id)
        return VerifiedArtifactPayload(
            artifact=artifact,
            object_binding=binding,
            payload_schema_id=trusted.payload_schema_id,
            kind=trusted.artifact_kind,
            metadata=deepcopy(artifact.model_dump(mode="json")["meta"]),
            payload_bytes=payload_bytes,
            payload=deepcopy(payload),
        )


__all__ = [
    "ArtifactPayloadBindingProvider",
    "ArtifactPayloadReader",
    "ArtifactReadRepository",
    "TrustedArtifactPayloadBinding",
    "VerifiedArtifactPayload",
]
