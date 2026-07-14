from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import CursorInvalid, IntegrityViolation
from gameforge.contracts.lineage import (
    Artifact,
    ArtifactV1,
    ArtifactV2,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import MAX_PAGE_ITEMS, ReadSnapshotV1
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    ArtifactRow,
    Base,
    MaterializedReadItemRow,
    ReadSnapshotRow,
)


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
PAGE_SIZE = 2


@dataclass
class _BindingProbe:
    active_refs: set[ObjectRef] = field(default_factory=set)
    checked_refs: list[ObjectRef] = field(default_factory=list)

    def has_active_binding(self, ref: ObjectRef) -> bool:
        self.checked_refs.append(ref)
        return ref in self.active_refs


def _engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'artifacts.db'}")
    Base.metadata.create_all(engine)
    return engine


def _repository(
    session: Session,
    bindings: _BindingProbe | None = None,
    *,
    now: datetime = NOW,
) -> SqlArtifactRepository:
    clock = FrozenUtcClock(now)
    return SqlArtifactRepository(
        session,
        binding_repository=bindings,
        cursor_signer=CursorSigner(
            signing_key=b"artifact-repository-test-key",
            clock=clock,
        ),
        clock=clock,
        page_size=PAGE_SIZE,
        snapshot_ttl=timedelta(minutes=5),
    )


def _legacy(
    artifact_id: str,
    *,
    created_at: str | None = None,
    meta: dict | None = None,
) -> ArtifactV1:
    return Artifact(
        artifact_id=artifact_id,
        kind="ir_snapshot",
        version_tuple=VersionTuple(ir_snapshot_id="sha256:legacy"),
        lineage=[],
        payload_hash="sha256:legacy-payload",
        created_at=created_at,
        meta=meta or {},
    )


def _current(
    payload: bytes = b"current artifact",
    *,
    created_at: str | None = None,
) -> ArtifactV2:
    ref = object_ref_for_bytes(payload)
    return build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(ir_snapshot_id=f"sha256:{ref.sha256}"),
        lineage=(),
        payload_hash=ref.sha256,
        object_ref=ref,
        meta={"source": "test"},
        created_at=created_at,
    )


def test_put_is_transaction_bound_and_rollback_removes_the_insert(tmp_path) -> None:
    engine = _engine(tmp_path)
    session = Session(engine, autoflush=False, expire_on_commit=False)
    transaction = session.begin()

    stored = _repository(session).put(_legacy("legacy-1"))

    assert stored.artifact_id == "legacy-1"
    assert session.get(ArtifactRow, "legacy-1") is not None
    transaction.rollback()
    session.close()

    with Session(engine) as verification:
        assert verification.get(ArtifactRow, "legacy-1") is None


def test_v1_and_v2_round_trip_without_fabricating_a_legacy_object_ref(tmp_path) -> None:
    engine = _engine(tmp_path)
    current = _current()
    bindings = _BindingProbe({current.object_ref})
    with Session(engine) as session, session.begin():
        repository = _repository(session, bindings)
        repository.put(_legacy("legacy-1"))
        repository.put(current)

    with Session(engine) as session:
        repository = _repository(session, bindings)
        legacy = repository.get("legacy-1")
        loaded_current = repository.get(current.artifact_id)

        assert isinstance(legacy, ArtifactV1)
        assert not hasattr(legacy, "object_ref")
        assert loaded_current == current
        assert isinstance(loaded_current, ArtifactV2)


def test_v2_put_and_idempotent_retry_each_require_the_exact_active_binding(tmp_path) -> None:
    engine = _engine(tmp_path)
    artifact = _current()
    bindings = _BindingProbe({artifact.object_ref})
    with Session(engine) as session, session.begin():
        repository = _repository(session, bindings)
        assert repository.put(artifact) == artifact
        assert repository.put(artifact) == artifact

    assert bindings.checked_refs == [artifact.object_ref, artifact.object_ref]

    bindings.active_refs.clear()
    with Session(engine) as session, session.begin():
        with pytest.raises(IntegrityViolation, match="active ObjectBinding"):
            _repository(session, bindings).put(artifact)


def test_v2_put_fails_closed_without_a_binding_repository(tmp_path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session, session.begin():
        with pytest.raises(IntegrityViolation, match="active ObjectBinding"):
            _repository(session).put(_current())


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_duplicate_put_keeps_the_first_created_at(version: str, tmp_path) -> None:
    engine = _engine(tmp_path)
    bindings = _BindingProbe()
    if version == "v1":
        first = _legacy("same-id", created_at="2026-07-13T12:00:00Z")
        retry = first.model_copy(update={"created_at": "2026-07-13T12:01:00Z"})
    else:
        first = _current(created_at="2026-07-13T12:00:00Z")
        retry = first.model_copy(update={"created_at": "2026-07-13T12:01:00Z"})
        bindings.active_refs.add(first.object_ref)

    with Session(engine) as session, session.begin():
        repository = _repository(session, bindings)
        repository.put(first)
        stored = repository.put(retry)

        assert stored.created_at == first.created_at

    with Session(engine) as session:
        assert _repository(session, bindings).get(first.artifact_id) == first


def test_same_v1_id_with_changed_immutable_content_is_rejected_without_overwrite(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    original = _legacy("same-id", meta={"value": 1})
    changed = _legacy("same-id", meta={"value": 2})
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        repository.put(original)
        with pytest.raises(IntegrityViolation, match="immutable content"):
            repository.put(changed)

    with Session(engine) as session:
        assert _repository(session).get("same-id") == original


def test_same_v2_id_with_changed_object_ref_size_is_rejected_without_overwrite(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    original = _current()
    changed_ref = original.object_ref.model_copy(
        update={"size_bytes": original.object_ref.size_bytes + 1}
    )
    changed = original.model_copy(update={"object_ref": changed_ref})
    bindings = _BindingProbe({original.object_ref, changed_ref})
    with Session(engine) as session, session.begin():
        repository = _repository(session, bindings)
        repository.put(original)
        with pytest.raises(IntegrityViolation, match="immutable content"):
            repository.put(changed)

    with Session(engine) as session:
        assert _repository(session, bindings).get(original.artifact_id) == original


def test_put_revalidates_v2_identity_before_writing(tmp_path) -> None:
    engine = _engine(tmp_path)
    artifact = _current()
    tampered = artifact.model_copy(update={"meta": {"source": "tampered"}})
    bindings = _BindingProbe({artifact.object_ref})

    with Session(engine) as session, session.begin():
        with pytest.raises(IntegrityViolation, match="invalid artifact wire"):
            _repository(session, bindings).put(tampered)
        assert session.get(ArtifactRow, artifact.artifact_id) is None


@pytest.mark.parametrize(
    ("schema_version", "object_ref_mode"),
    [
        ("lineage@999", "none"),
        ("lineage@1", "present"),
        ("lineage@2", "none"),
    ],
)
def test_get_fails_closed_for_unknown_or_cross_version_object_ref_shape(
    schema_version: str,
    object_ref_mode: str,
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    current = _current()
    wire = current.model_dump(mode="json")
    with Session(engine) as session, session.begin():
        session.add(
            ArtifactRow(
                artifact_id=wire["artifact_id"],
                lineage_schema_version=schema_version,
                kind=wire["kind"],
                version_tuple=wire["version_tuple"],
                lineage=wire["lineage"],
                payload_hash=wire["payload_hash"],
                created_at=wire["created_at"],
                meta=wire["meta"],
                object_ref=(wire["object_ref"] if object_ref_mode == "present" else None),
            )
        )

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match=r"stored .*artifact"):
            _repository(session).get(current.artifact_id)


def test_get_revalidates_stored_v2_hash_and_identity(tmp_path) -> None:
    engine = _engine(tmp_path)
    artifact = _current()
    wire = artifact.model_dump(mode="json")
    with Session(engine) as session, session.begin():
        session.add(
            ArtifactRow(
                artifact_id=wire["artifact_id"],
                lineage_schema_version=wire["lineage_schema_version"],
                kind=wire["kind"],
                version_tuple=wire["version_tuple"],
                lineage=wire["lineage"],
                payload_hash="0" * 64,
                created_at=wire["created_at"],
                meta=wire["meta"],
                object_ref=wire["object_ref"],
            )
        )

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="stored artifact"):
            _repository(session).get(artifact.artifact_id)


def test_get_rejects_v2_nested_fields_that_the_parser_would_silently_drop(tmp_path) -> None:
    engine = _engine(tmp_path)
    artifact = _current()
    wire = artifact.model_dump(mode="json")
    with Session(engine) as session, session.begin():
        session.add(
            ArtifactRow(
                artifact_id=wire["artifact_id"],
                lineage_schema_version=wire["lineage_schema_version"],
                kind=wire["kind"],
                version_tuple={**wire["version_tuple"], "unknown_future_field": "value"},
                lineage=wire["lineage"],
                payload_hash=wire["payload_hash"],
                created_at=wire["created_at"],
                meta=wire["meta"],
                object_ref=wire["object_ref"],
            )
        )

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="wire is not canonical"):
            _repository(session).get(artifact.artifact_id)


def test_page_uses_bounded_keyset_at_an_immutable_high_watermark(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    source_reads: list[tuple[str, object]] = []

    def capture_source_reads(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, context, executemany
        normalized = " ".join(statement.upper().split())
        if (
            "FROM ARTIFACTS" in normalized
            and "SELECT ARTIFACTS." in normalized
            and "ORDER BY ARTIFACTS.ARTIFACT_ID" in normalized
        ):
            source_reads.append((normalized, parameters))

    original = tuple(
        _legacy(artifact_id, meta={"state": f"original-{artifact_id}"})
        for artifact_id in (f"artifact-{index:04d}" for index in range(MAX_PAGE_ITEMS + 1))
    )

    with engine.connect() as first_connection:
        with Session(
            first_connection,
            autoflush=False,
            expire_on_commit=False,
        ) as first_session:
            event.listen(engine, "before_cursor_execute", capture_source_reads)
            try:
                with first_session.begin():
                    repository = _repository(first_session)
                    for artifact in reversed(original):
                        repository.put(artifact)
                    first_page = repository.page()
            finally:
                event.remove(engine, "before_cursor_execute", capture_source_reads)

            assert first_page.items == original[:PAGE_SIZE]
            assert first_page.next_cursor is not None
            assert len(source_reads) == 1
            assert " LIMIT " in source_reads[0][0]
            limit_parameters = source_reads[0][1]
            assert isinstance(limit_parameters, tuple)
            assert limit_parameters[-2] == PAGE_SIZE + 1

            with first_session.begin():
                snapshot = first_session.get(ReadSnapshotRow, first_page.read_snapshot_id)
                assert snapshot is not None
                assert snapshot.strategy == "immutable_high_watermark"
                assert snapshot.high_watermark == len(original)
                assert snapshot.materialized_item_count is None
                assert (
                    first_session.scalar(select(func.count()).select_from(MaterializedReadItemRow))
                    == 0
                )

            with engine.connect() as second_connection:
                with Session(second_connection) as second_session, second_session.begin():
                    _repository(second_session).put(
                        _legacy(
                            "artifact-0001a",
                            meta={"state": "inserted-after-page-one"},
                        )
                    )

            with first_session.begin():
                continued = _repository(first_session).page(first_page.next_cursor)

    assert continued.items == original[PAGE_SIZE : PAGE_SIZE * 2]
    assert all(item.artifact_id != "artifact-0001a" for item in continued.items)
    assert continued.next_cursor is not None

    with Session(engine) as fresh_session, fresh_session.begin():
        repository = _repository(fresh_session)
        fresh = repository.page()
        assert fresh.next_cursor is not None
        fresh_continued = repository.page(fresh.next_cursor)

    assert fresh.items == original[:PAGE_SIZE]
    assert [item.artifact_id for item in fresh_continued.items] == [
        "artifact-0001a",
        "artifact-0002",
    ]


def test_page_rejects_a_signed_empty_artifact_keyset_position(tmp_path) -> None:
    engine = _engine(tmp_path)
    clock = FrozenUtcClock(NOW)
    signer = CursorSigner(
        signing_key=b"artifact-repository-test-key",
        clock=clock,
    )
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        for artifact_id in ("artifact-a", "artifact-b", "artifact-c"):
            repository.put(_legacy(artifact_id))
        first_page = repository.page()
        row = session.get(ReadSnapshotRow, first_page.read_snapshot_id)
        assert row is not None
        snapshot = ReadSnapshotV1(
            snapshot_schema_version=row.snapshot_schema_version,
            snapshot_id=row.snapshot_id,
            resource_kind=row.resource_kind,
            query_hash=row.query_hash,
            authz_fingerprint=row.authz_fingerprint,
            stable_sort_schema_id=row.stable_sort_schema_id,
            strategy=row.strategy,
            high_watermark=row.high_watermark,
            materialized_item_count=row.materialized_item_count,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
        cursor = signer.issue(
            snapshot=snapshot,
            position=canonical_json({"artifact_id": ""}),
            page_size=PAGE_SIZE,
        )

    with Session(engine) as session:
        with pytest.raises(CursorInvalid, match="cursor position"):
            _repository(session).page(cursor)


def test_page_fails_closed_when_a_stored_wire_is_corrupt(tmp_path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        for artifact_id in ("artifact-a", "artifact-b", "artifact-c"):
            repository.put(_legacy(artifact_id))
        first_page = repository.page()

    assert first_page.next_cursor is not None
    with Session(engine) as session, session.begin():
        row = session.get(ArtifactRow, "artifact-c")
        assert row is not None
        row.kind = "not-an-artifact-kind"

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="stored artifact row"):
            _repository(session).page(first_page.next_cursor)


def test_page_rejects_nested_v2_fields_that_the_parser_would_silently_drop(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    current = _current()
    bindings = _BindingProbe({current.object_ref})
    with Session(engine) as session, session.begin():
        repository = _repository(session, bindings)
        repository.put(_legacy("artifact-a"))
        repository.put(_legacy("artifact-b"))
        repository.put(current)
        first_page = repository.page()

    assert first_page.next_cursor is not None
    with Session(engine) as session, session.begin():
        row = session.get(ArtifactRow, current.artifact_id)
        assert row is not None
        row.version_tuple = {
            **row.version_tuple,
            "unknown_future_field": "value",
        }

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="wire is not canonical"):
            _repository(session, bindings).page(first_page.next_cursor)
