from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

import gameforge.runtime.persistence.artifacts as artifact_repository_module

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import CursorInvalid, IntegrityViolation
from gameforge.contracts.lineage import (
    Artifact,
    ArtifactV1,
    ArtifactV2,
    ObjectLocation,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import MAX_PAGE_ITEMS, ObjectStat, ReadSnapshotV1
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    ArtifactRow,
    Base,
    MaterializedReadItemRow,
    ObjectBindingRow,
    ReadSnapshotRow,
)
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository


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


def test_put_many_preloads_existing_rows_and_flushes_once_for_the_batch(tmp_path) -> None:
    engine = _engine(tmp_path)

    def exercise(prefix: str, count: int) -> tuple[int, int]:
        selects: list[str] = []
        flushes = 0

        def observe_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        def observe_flush(
            _session: Session,
            _flush_context: object,
            _instances: object,
        ) -> None:
            nonlocal flushes
            flushes += 1

        items = tuple(_legacy(f"{prefix}-{ordinal}") for ordinal in range(count))
        with Session(engine, autoflush=False, expire_on_commit=False) as session:
            event.listen(engine, "before_cursor_execute", observe_statement)
            event.listen(session, "before_flush", observe_flush)
            try:
                with session.begin():
                    assert _repository(session).put_many(items) == items
            finally:
                event.remove(session, "before_flush", observe_flush)
                event.remove(engine, "before_cursor_execute", observe_statement)
        return len(selects), flushes

    assert exercise("single", 1) == (1, 1)
    assert exercise("batch", 8) == (1, 1)


def test_put_many_chunks_901_artifact_ids_and_still_flushes_once(tmp_path) -> None:
    engine = _engine(tmp_path)
    items = tuple(_legacy(f"chunked-{ordinal}") for ordinal in range(901))
    selects: list[str] = []
    flushes = 0

    def observe_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    def observe_flush(
        _session: Session,
        _flush_context: object,
        _instances: object,
    ) -> None:
        nonlocal flushes
        flushes += 1

    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        event.listen(engine, "before_cursor_execute", observe_statement)
        event.listen(session, "before_flush", observe_flush)
        try:
            with session.begin():
                assert _repository(session).put_many(items) == items
        finally:
            event.remove(session, "before_flush", observe_flush)
            event.remove(engine, "before_cursor_execute", observe_statement)

    assert len(selects) == 2
    assert flushes == 1


def test_artifact_preflight_seal_applies_without_reselect_or_reparse(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    items = tuple(_legacy(f"sealed-{ordinal}") for ordinal in range(8))
    statements: list[str] = []

    def observe_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lstrip().upper())

    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        repository = _repository(session)
        event.listen(engine, "before_cursor_execute", observe_statement)
        try:
            with session.begin():
                seal = repository.preflight_put_many(items)
                preflight_statement_count = len(statements)

                def fail_reparse(_item: object) -> object:
                    raise AssertionError("Artifact was reparsed after its preflight seal")

                def fail_canonical(*_args: object, **_kwargs: object) -> object:
                    raise AssertionError("Artifact canonicalization ran during sealed apply")

                monkeypatch.setattr(
                    artifact_repository_module,
                    "_revalidate_for_put",
                    fail_reparse,
                )
                monkeypatch.setattr(
                    artifact_repository_module,
                    "parse_artifact",
                    fail_canonical,
                )
                monkeypatch.setattr(
                    artifact_repository_module,
                    "canonical_json",
                    fail_canonical,
                )
                assert repository.put_preflighted_many(seal) == items
                apply_statements = statements[preflight_statement_count:]
                assert not any(statement.startswith("SELECT") for statement in apply_statements)
                with pytest.raises(IntegrityViolation, match="already consumed"):
                    repository.put_preflighted_many(seal)
        finally:
            event.remove(engine, "before_cursor_execute", observe_statement)


def test_artifact_preflight_seal_rejects_another_repository_and_transaction(tmp_path) -> None:
    engine = _engine(tmp_path)
    item = _legacy("transaction-bound-artifact")
    dml: list[str] = []

    def observe_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            dml.append(statement)

    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        first_transaction = session.begin()
        owner = _repository(session)
        seal = owner.preflight_put_many((item,))
        another_repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="another repository"):
            another_repository.put_preflighted_many(seal)
        first_transaction.rollback()

        event.listen(engine, "before_cursor_execute", observe_statement)
        try:
            with session.begin():
                with pytest.raises(IntegrityViolation, match="another transaction"):
                    owner.put_preflighted_many(seal)
        finally:
            event.remove(engine, "before_cursor_execute", observe_statement)

    assert dml == []


def test_artifact_preflight_seal_rejects_field_and_nested_row_tampering_before_dml(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    item = _current(b"immutable-artifact-preflight")
    dml: list[str] = []

    def observe_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            dml.append(statement)

    class ExactObjectStore:
        def __init__(self, stat: ObjectStat) -> None:
            self._stat = stat

        def stat(self, location: ObjectLocation) -> ObjectStat:
            if location != self._stat.location:
                raise FileNotFoundError(location.backend_generation)
            return self._stat

    location = ObjectLocation(
        store_id="local",
        key=item.object_ref.key,
        backend_generation="immutable-artifact-generation",
    )
    stat = ObjectStat(
        ref=item.object_ref,
        location=location,
        verified_at="2026-07-13T12:00:00Z",
    )
    with Session(engine, autoflush=False, expire_on_commit=False) as session, session.begin():
        bindings = SqlObjectBindingRepository(
            session,
            object_store=ExactObjectStore(stat),  # type: ignore[arg-type]
            default_store_id="local",
        )
        artifacts = _repository(session, bindings)  # type: ignore[arg-type]
        binding_seal = bindings.preflight_terminal_preverified_many(((stat, None),))
        seal = artifacts.preflight_put_many((item,), binding_preflight=binding_seal)
        event.listen(engine, "before_cursor_execute", observe_statement)
        try:
            assert type(seal).__slots__ == ("__weakref__",)
            with pytest.raises(AttributeError):
                object.__setattr__(seal, "_rows", ())
            assert not hasattr(seal, "_rows")
            for unregistered in (replace(seal), copy(seal)):
                with pytest.raises(IntegrityViolation, match="trusted preflight seal"):
                    artifacts.put_preflighted_many(unregistered)
        finally:
            event.remove(engine, "before_cursor_execute", observe_statement)

    assert dml == []


def test_v2_artifact_preflight_consumes_the_same_binding_seal_without_reselect(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExactObjectStore:
        def __init__(self, stat: ObjectStat) -> None:
            self._stat = stat

        def stat(self, location: ObjectLocation) -> ObjectStat:
            if location != self._stat.location:
                raise FileNotFoundError(location.backend_generation)
            return self._stat

    engine = _engine(tmp_path)
    artifact = _current(b"sealed-v2-artifact")
    location = ObjectLocation(
        store_id="local",
        key=artifact.object_ref.key,
        backend_generation="sealed-v2-generation",
    )
    stat = ObjectStat(
        ref=artifact.object_ref,
        location=location,
        verified_at="2026-07-13T12:00:00Z",
    )
    statements: list[str] = []

    def observe_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lstrip().upper())

    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        bindings = SqlObjectBindingRepository(
            session,
            object_store=ExactObjectStore(stat),  # type: ignore[arg-type]
            default_store_id="local",
        )
        artifacts = _repository(session, bindings)  # type: ignore[arg-type]
        event.listen(engine, "before_cursor_execute", observe_statement)
        try:
            with session.begin():
                binding_seal = bindings.preflight_terminal_preverified_many(((stat, None),))
                artifact_seal = artifacts.preflight_put_many(
                    (artifact,),
                    binding_preflight=binding_seal,
                )
                assert sum(statement.startswith("SELECT") for statement in statements) == 2
                preflight_statement_count = len(statements)

                def fail_reparse(_item: object) -> object:
                    raise AssertionError("Artifact was reparsed after its preflight seal")

                monkeypatch.setattr(
                    artifact_repository_module,
                    "_revalidate_for_put",
                    fail_reparse,
                )
                bindings.apply_terminal_preverified_many(binding_seal)
                stored = artifacts.put_preflighted_many(artifact_seal)
                assert stored == (artifact,)
                with pytest.raises(TypeError, match="immutable"):
                    stored[0].meta["tampered"] = True
                with pytest.raises(ValidationError, match="frozen"):
                    stored[0].version_tuple.seed = 99
                apply_statements = statements[preflight_statement_count:]
                assert not any(statement.startswith("SELECT") for statement in apply_statements)
        finally:
            event.remove(engine, "before_cursor_execute", observe_statement)


def test_put_many_preserves_input_order_and_rejects_an_in_batch_immutable_conflict(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    first = _legacy("same-id", meta={"value": 1})
    retry_with_later_time = first.model_copy(update={"created_at": "2026-07-13T12:01:00Z"})
    changed = _legacy("same-id", meta={"value": 2})

    with Session(engine, autoflush=False, expire_on_commit=False) as session, session.begin():
        repository = _repository(session)
        assert repository.put_many((first, retry_with_later_time)) == (first, first)

    with Session(engine, autoflush=False, expire_on_commit=False) as session, session.begin():
        with pytest.raises(IntegrityViolation, match="immutable content"):
            _repository(session).put_many((first, changed))

    with Session(engine) as session:
        assert _repository(session).get(first.artifact_id) == first


def test_put_many_v2_accepts_an_active_binding_in_any_store_with_one_set_read(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    artifacts = (_current(b"first"), _current(b"second"))
    with Session(engine) as session, session.begin():
        session.add_all(
            ObjectBindingRow(
                object_key=artifact.object_ref.key,
                store_id="archive",
                binding_schema_version="object-binding@1",
                object_ref_schema_version=artifact.object_ref.object_ref_schema_version,
                location_schema_version="object-location@1",
                object_sha256=artifact.object_ref.sha256,
                object_size_bytes=artifact.object_ref.size_bytes,
                backend_generation=f"generation-{ordinal}",
                etag=None,
                storage_class=None,
                status="active",
                revision=1,
                verified_at="2026-07-13T12:00:00Z",
            )
            for ordinal, artifact in enumerate(artifacts, start=1)
        )

    binding_selects: list[str] = []

    def observe_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("SELECT") and "FROM OBJECT_BINDINGS" in normalized:
            binding_selects.append(statement)

    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        bindings = SqlObjectBindingRepository(
            session,
            object_store=object(),  # type: ignore[arg-type]
            default_store_id="primary",
        )
        event.listen(engine, "before_cursor_execute", observe_statement)
        try:
            with session.begin():
                assert _repository(session, bindings).put_many(artifacts) == artifacts  # type: ignore[arg-type]
        finally:
            event.remove(engine, "before_cursor_execute", observe_statement)

    assert len(binding_selects) == 1


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
