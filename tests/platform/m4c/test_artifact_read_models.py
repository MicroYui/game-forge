from __future__ import annotations

import io
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import BinaryIO

import pytest

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation, NotFound, PayloadTooLarge
from gameforge.contracts.lineage import (
    Artifact,
    ArtifactV1,
    ArtifactV2,
    ObjectBinding,
    ObjectLocation,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import ObjectStat
from gameforge.platform.read_models.artifacts import (
    ArtifactPayloadReader,
    TrustedArtifactPayloadBinding,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store import LocalObjectStore


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
STORE_ID = "local:artifact-read-model-test"
SCHEMA_ID = "checker-report@1"


class _Artifacts:
    def __init__(self, *items: ArtifactV1 | ArtifactV2) -> None:
        self.items = {item.artifact_id: item for item in items}

    def get(self, artifact_id: str) -> ArtifactV1 | ArtifactV2 | None:
        return self.items.get(artifact_id)


class _TrustedBindings:
    def __init__(self, *items: TrustedArtifactPayloadBinding) -> None:
        self.items = {item.artifact_id: item for item in items}
        self.lookups: list[str] = []

    def resolve(self, artifact_id: str) -> TrustedArtifactPayloadBinding | None:
        self.lookups.append(artifact_id)
        return self.items.get(artifact_id)


class _ObjectBindings:
    def __init__(self, binding: ObjectBinding | None) -> None:
        self.binding = binding
        self.lookups: list[ObjectRef] = []

    def resolve(
        self,
        ref: ObjectRef,
        store_id: str | None = None,
    ) -> ObjectBinding:
        assert store_id is None
        self.lookups.append(ref)
        if self.binding is None:
            raise FileNotFoundError(ref.key)
        return self.binding


@dataclass
class _ObjectStore:
    stat_result: ObjectStat
    payload: bytes
    short_read_size: int | None = None
    open_calls: int = 0

    def stat(self, location: ObjectLocation) -> ObjectStat:
        return self.stat_result

    def open(self, location: ObjectLocation) -> BinaryIO:
        self.open_calls += 1
        if self.short_read_size is None:
            return io.BytesIO(self.payload)
        return _ShortReadStream(self.payload, self.short_read_size)


class _ShortReadStream(io.BytesIO):
    def __init__(self, payload: bytes, short_read_size: int) -> None:
        super().__init__(payload)
        self._short_read_size = short_read_size

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError("artifact reader must use bounded reads")
        return super().read(min(size, self._short_read_size))


def _artifact(payload: bytes, *, meta: dict | None = None) -> ArtifactV2:
    ref = object_ref_for_bytes(payload)
    return build_artifact_v2(
        kind="checker_run",
        version_tuple=VersionTuple(
            ir_snapshot_id="sha256:fixture",
            tool_version="checker@1",
        ),
        lineage=(),
        payload_hash=ref.sha256,
        object_ref=ref,
        meta=meta or {"domain_scope": {"domain_ids": ["numeric"]}, "source": "server"},
    )


def _binding(ref: ObjectRef, location: ObjectLocation) -> ObjectBinding:
    return ObjectBinding(
        object_ref=ref,
        location=location,
        status="active",
        revision=3,
        verified_at="2026-07-14T12:00:00Z",
    )


def _stat(ref: ObjectRef, location: ObjectLocation) -> ObjectStat:
    return ObjectStat(
        ref=ref,
        location=location,
        verified_at="2026-07-14T12:00:00Z",
    )


def _reader(
    artifact: ArtifactV1 | ArtifactV2,
    trusted: TrustedArtifactPayloadBinding | None,
    binding: ObjectBinding | None,
    store: object,
    *,
    max_payload_bytes: int = 64 * 1024,
) -> ArtifactPayloadReader:
    trusted_items = () if trusted is None else (trusted,)
    return ArtifactPayloadReader(
        artifacts=_Artifacts(artifact),
        trusted_bindings=_TrustedBindings(*trusted_items),
        object_bindings=_ObjectBindings(binding),
        object_store=store,
        max_payload_bytes=max_payload_bytes,
    )


def test_reads_verified_payload_through_local_store_and_ignores_payload_claims(
    tmp_path,
) -> None:
    payload_value = {
        "kind": "patch",
        "meta": {"source": "payload"},
        "payload_schema_id": "untrusted@999",
        "result": "passed",
    }
    payload = canonical_json(payload_value).encode("utf-8")
    store = LocalObjectStore(
        tmp_path,
        store_id=STORE_ID,
        clock=FrozenUtcClock(NOW),
        cursor_signing_key=b"artifact-read-model-object-store-key",
        snapshot_ttl=timedelta(minutes=5),
    )
    stored = store.put_verified(payload)
    artifact = _artifact(payload)
    binding = _binding(stored.ref, stored.location)
    trusted = TrustedArtifactPayloadBinding.for_artifact(
        artifact,
        payload_schema_id=SCHEMA_ID,
    )

    resolved = _reader(artifact, trusted, binding, store).read(artifact.artifact_id)

    assert resolved.artifact == artifact
    assert resolved.object_binding == binding
    assert resolved.payload_schema_id == SCHEMA_ID
    assert resolved.kind == "checker_run"
    assert resolved.metadata == artifact.model_dump(mode="json")["meta"]
    assert resolved.payload == payload_value
    assert resolved.payload_bytes == payload


def test_short_reads_are_consumed_to_eof_without_an_unbounded_read() -> None:
    payload = canonical_json({"result": "passed", "values": list(range(30))}).encode()
    artifact = _artifact(payload)
    location = ObjectLocation(
        store_id=STORE_ID,
        key=artifact.object_ref.key,
        backend_generation="generation:1",
    )
    binding = _binding(artifact.object_ref, location)
    store = _ObjectStore(
        stat_result=_stat(artifact.object_ref, location),
        payload=payload,
        short_read_size=3,
    )

    result = _reader(
        artifact,
        TrustedArtifactPayloadBinding.for_artifact(artifact, payload_schema_id=SCHEMA_ID),
        binding,
        store,
    ).read(artifact.artifact_id)

    assert result.payload_bytes == payload
    assert store.open_calls == 1


def test_missing_artifact_is_typed_not_found() -> None:
    reader = ArtifactPayloadReader(
        artifacts=_Artifacts(),
        trusted_bindings=_TrustedBindings(),
        object_bindings=_ObjectBindings(None),
        object_store=object(),
        max_payload_bytes=1024,
    )

    with pytest.raises(NotFound, match="artifact does not exist"):
        reader.read("missing")


def test_lineage_v1_fails_without_fabricating_an_object_ref_or_schema_lookup() -> None:
    legacy = Artifact(
        artifact_id="legacy-artifact",
        kind="checker_run",
        version_tuple=VersionTuple(tool_version="legacy-checker@1"),
        lineage=[],
        payload_hash="sha256:legacy",
        meta={"payload_schema_id": SCHEMA_ID},
    )
    trusted = _TrustedBindings()
    reader = ArtifactPayloadReader(
        artifacts=_Artifacts(legacy),
        trusted_bindings=trusted,
        object_bindings=_ObjectBindings(None),
        object_store=object(),
        max_payload_bytes=1024,
    )

    with pytest.raises(NotFound, match="has no object-backed payload"):
        reader.read(legacy.artifact_id)

    assert trusted.lookups == []


def test_missing_server_trusted_payload_binding_fails_closed() -> None:
    payload = canonical_json({"result": "passed"}).encode()
    artifact = _artifact(payload)

    with pytest.raises(IntegrityViolation, match="trusted payload binding is unavailable"):
        _reader(artifact, None, None, object()).read(artifact.artifact_id)


@pytest.mark.parametrize(
    "tamper",
    [
        {"artifact_kind": "patch"},
        {"payload_hash": "0" * 64},
        {"metadata_digest": "1" * 64},
    ],
)
def test_server_trusted_binding_must_match_the_exact_artifact(
    tamper: dict[str, str],
) -> None:
    payload = canonical_json({"result": "passed"}).encode()
    artifact = _artifact(payload)
    trusted = TrustedArtifactPayloadBinding.for_artifact(
        artifact,
        payload_schema_id=SCHEMA_ID,
    )
    trusted = replace(trusted, **tamper)

    with pytest.raises(IntegrityViolation, match="trusted payload binding differs"):
        _reader(artifact, trusted, None, object()).read(artifact.artifact_id)


def test_missing_active_object_binding_is_an_integrity_failure() -> None:
    payload = canonical_json({"result": "passed"}).encode()
    artifact = _artifact(payload)

    with pytest.raises(IntegrityViolation, match="active ObjectBinding is unavailable"):
        _reader(
            artifact,
            TrustedArtifactPayloadBinding.for_artifact(
                artifact,
                payload_schema_id=SCHEMA_ID,
            ),
            None,
            object(),
        ).read(artifact.artifact_id)


def test_retired_or_different_object_binding_is_rejected() -> None:
    payload = canonical_json({"result": "passed"}).encode()
    artifact = _artifact(payload)
    location = ObjectLocation(
        store_id=STORE_ID,
        key=artifact.object_ref.key,
        backend_generation="generation:1",
    )
    retired = _binding(artifact.object_ref, location).model_copy(update={"status": "retired"})

    with pytest.raises(IntegrityViolation, match="ObjectBinding does not match"):
        _reader(
            artifact,
            TrustedArtifactPayloadBinding.for_artifact(
                artifact,
                payload_schema_id=SCHEMA_ID,
            ),
            retired,
            object(),
        ).read(artifact.artifact_id)


def test_object_stat_must_match_the_active_binding_and_artifact() -> None:
    payload = canonical_json({"result": "passed"}).encode()
    artifact = _artifact(payload)
    location = ObjectLocation(
        store_id=STORE_ID,
        key=artifact.object_ref.key,
        backend_generation="generation:1",
    )
    binding = _binding(artifact.object_ref, location)
    mismatched_ref = artifact.object_ref.model_copy(
        update={"size_bytes": artifact.object_ref.size_bytes + 1}
    )
    store = _ObjectStore(
        stat_result=_stat(mismatched_ref, location),
        payload=payload,
    )

    with pytest.raises(IntegrityViolation, match="ObjectStore stat does not match"):
        _reader(
            artifact,
            TrustedArtifactPayloadBinding.for_artifact(
                artifact,
                payload_schema_id=SCHEMA_ID,
            ),
            binding,
            store,
        ).read(artifact.artifact_id)


def test_payload_size_limit_is_checked_before_opening_the_object() -> None:
    payload = canonical_json({"result": "x" * 200}).encode()
    artifact = _artifact(payload)
    location = ObjectLocation(
        store_id=STORE_ID,
        key=artifact.object_ref.key,
        backend_generation="generation:1",
    )
    binding = _binding(artifact.object_ref, location)
    store = _ObjectStore(_stat(artifact.object_ref, location), payload)

    with pytest.raises(PayloadTooLarge, match="exceeds the configured byte limit"):
        _reader(
            artifact,
            TrustedArtifactPayloadBinding.for_artifact(
                artifact,
                payload_schema_id=SCHEMA_ID,
            ),
            binding,
            store,
            max_payload_bytes=len(payload) - 1,
        ).read(artifact.artifact_id)

    assert store.open_calls == 0


def test_same_size_corrupt_object_bytes_fail_sha256_verification() -> None:
    payload = canonical_json({"result": "passed"}).encode()
    corrupt = b"x" * len(payload)
    artifact = _artifact(payload)
    location = ObjectLocation(
        store_id=STORE_ID,
        key=artifact.object_ref.key,
        backend_generation="generation:1",
    )
    binding = _binding(artifact.object_ref, location)
    store = _ObjectStore(_stat(artifact.object_ref, location), corrupt)

    with pytest.raises(IntegrityViolation, match="content hash differs"):
        _reader(
            artifact,
            TrustedArtifactPayloadBinding.for_artifact(
                artifact,
                payload_schema_id=SCHEMA_ID,
            ),
            binding,
            store,
        ).read(artifact.artifact_id)


@pytest.mark.parametrize("stored_payload", [b"{}", b'{"result":"passed","extra":1}'])
def test_object_bytes_must_have_the_exact_declared_size(stored_payload: bytes) -> None:
    payload = canonical_json({"result": "passed"}).encode()
    assert len(stored_payload) != len(payload)
    artifact = _artifact(payload)
    location = ObjectLocation(
        store_id=STORE_ID,
        key=artifact.object_ref.key,
        backend_generation="generation:1",
    )
    binding = _binding(artifact.object_ref, location)

    with pytest.raises(IntegrityViolation, match="byte length differs"):
        _reader(
            artifact,
            TrustedArtifactPayloadBinding.for_artifact(
                artifact,
                payload_schema_id=SCHEMA_ID,
            ),
            binding,
            _ObjectStore(_stat(artifact.object_ref, location), stored_payload),
        ).read(artifact.artifact_id)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"b":2, "a":1}',
        b'{"duplicate":1,"duplicate":2}',
        b'{"value":NaN}',
        b'["not-an-object"]',
    ],
)
def test_payload_must_be_one_strict_canonical_json_object(payload: bytes) -> None:
    artifact = _artifact(payload)
    location = ObjectLocation(
        store_id=STORE_ID,
        key=artifact.object_ref.key,
        backend_generation="generation:1",
    )
    binding = _binding(artifact.object_ref, location)
    store = _ObjectStore(_stat(artifact.object_ref, location), payload)

    with pytest.raises(IntegrityViolation, match="strict canonical JSON object"):
        _reader(
            artifact,
            TrustedArtifactPayloadBinding.for_artifact(
                artifact,
                payload_schema_id=SCHEMA_ID,
            ),
            binding,
            store,
        ).read(artifact.artifact_id)


def test_payload_json_depth_is_bounded_even_within_the_byte_limit() -> None:
    value: dict[str, object] = {"leaf": True}
    for _ in range(40):
        value = {"nested": value}
    payload = canonical_json(value).encode()
    artifact = _artifact(payload)
    location = ObjectLocation(
        store_id=STORE_ID,
        key=artifact.object_ref.key,
        backend_generation="generation:1",
    )
    binding = _binding(artifact.object_ref, location)

    with pytest.raises(IntegrityViolation, match="JSON depth limit"):
        _reader(
            artifact,
            TrustedArtifactPayloadBinding.for_artifact(
                artifact,
                payload_schema_id=SCHEMA_ID,
            ),
            binding,
            _ObjectStore(_stat(artifact.object_ref, location), payload),
        ).read(artifact.artifact_id)
