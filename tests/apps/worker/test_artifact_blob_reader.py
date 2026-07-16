"""Worker input blobs are bounded and re-verified at the final read boundary."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from io import BytesIO

import pytest
from sqlalchemy.orm import Session

from gameforge.apps.worker.components import WorkerArtifactBlobReader
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import MAX_PREPARED_ARTIFACT_BYTES
from gameforge.contracts.lineage import ArtifactV2, VersionTuple, build_artifact_v2
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
SIGNING_KEY = b"worker-artifact-blob-reader-signing-key"
STORE_ID = "local:test"


@pytest.fixture
def committed_blob(
    tmp_path,
) -> Iterator[tuple[WorkerArtifactBlobReader, LocalObjectStore, ArtifactV2, bytes]]:
    engine = get_engine(f"sqlite:///{tmp_path / 'worker-artifact-reader.db'}")
    Base.metadata.create_all(engine)
    clock = FrozenUtcClock(NOW)
    objects = LocalObjectStore(
        tmp_path / "objects",
        store_id=STORE_ID,
        clock=clock,
        cursor_signing_key=SIGNING_KEY,
    )
    payload = b'{"exact":"committed-input"}'
    stored = objects.put_verified(payload)
    artifact = build_artifact_v2(
        kind="source_raw",
        version_tuple=VersionTuple(doc_version="doc@1", tool_version="source@1"),
        lineage=(),
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        meta={"payload_schema_id": "source-raw@1"},
        created_at="2026-07-17T12:00:00Z",
    )
    with Session(engine) as session, session.begin():
        bindings = SqlObjectBindingRepository(session, objects, STORE_ID)
        bindings.bind_verified(stored.ref, stored.location, None)
        SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(signing_key=SIGNING_KEY, clock=clock),
            clock=clock,
        ).put(artifact)
    reader = WorkerArtifactBlobReader(
        engine=engine,
        object_store=objects,
        object_store_id=STORE_ID,
        cursor_signing_key=SIGNING_KEY,
        clock=clock,
    )
    yield reader, objects, artifact, payload
    engine.dispose()


def test_read_bytes_delegates_to_the_platform_hard_cap(committed_blob, monkeypatch) -> None:
    reader, _objects, artifact, payload = committed_blob
    calls: list[tuple[str, int]] = []

    def bounded(artifact_id: str, *, max_bytes: int) -> bytes:
        calls.append((artifact_id, max_bytes))
        return payload

    monkeypatch.setattr(reader, "read_bytes_bounded", bounded)

    assert reader.read_bytes(artifact.artifact_id) == payload
    assert calls == [(artifact.artifact_id, MAX_PREPARED_ARTIFACT_BYTES)]


def test_bounded_read_rejects_declared_oversize_before_open(committed_blob, monkeypatch) -> None:
    reader, objects, artifact, payload = committed_blob

    def forbidden_open(_location):
        raise AssertionError("oversized ObjectRef must be rejected before object I/O")

    monkeypatch.setattr(objects, "open", forbidden_open)

    with pytest.raises(IntegrityViolation, match="exceeds the consumer byte bound"):
        reader.read_bytes_bounded(artifact.artifact_id, max_bytes=len(payload) - 1)


def test_bounded_read_rejects_same_size_object_replacement(committed_blob, monkeypatch) -> None:
    reader, objects, artifact, payload = committed_blob
    replacement = bytes(byte ^ 1 for byte in payload)
    assert len(replacement) == len(payload)
    monkeypatch.setattr(objects, "open", lambda _location: BytesIO(replacement))

    with pytest.raises(IntegrityViolation, match="hash differs"):
        reader.read_bytes_bounded(artifact.artifact_id, max_bytes=len(payload))


def test_bounded_read_rejects_post_binding_oversize_replacement(
    committed_blob, monkeypatch
) -> None:
    reader, objects, artifact, payload = committed_blob
    monkeypatch.setattr(objects, "open", lambda _location: BytesIO(payload + b"!"))

    with pytest.raises(IntegrityViolation, match="bytes differ"):
        reader.read_bytes_bounded(artifact.artifact_id, max_bytes=len(payload))
