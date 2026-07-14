from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine, String, delete, func, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import (
    CursorExpired,
    CursorInvalid,
    Forbidden,
    IntegrityViolation,
    QueryTooBroad,
)
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    Base,
    MaterializedReadItemRow,
    ReadSnapshotRow,
)
from gameforge.runtime.persistence.read_views import (
    ImmutableReadBinding,
    ImmutableReadCandidate,
    MaterializedReadBinding,
    MaterializedReadCandidate,
    SqlImmutableReadViewRepository,
    SqlMaterializedReadViewRepository,
)


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
QUERY_HASH = canonical_sha256(
    {
        "api_version": "v1",
        "resource_kind": "runs",
        "filters": {"status": "queued"},
        "stable_sort": ["created_at:asc", "run_id:asc"],
        "page_projection": ["run_id", "status", "revision", "summary"],
    }
)
AUTHZ_A = canonical_sha256({"principal": "human:a", "authz_revision": 1})
AUTHZ_A_CHANGED = canonical_sha256({"principal": "human:a", "authz_revision": 2})
AUTHZ_B = canonical_sha256({"principal": "human:b", "authz_revision": 1})
PRINCIPAL_A = canonical_sha256({"principal_id": "human:a", "kind": "human"})
PRINCIPAL_B = canonical_sha256({"principal_id": "human:b", "kind": "human"})


@dataclass
class _MutableUtcClock:
    current: datetime = NOW

    def now_utc(self) -> datetime:
        return self.current


class _SourceBase(DeclarativeBase):
    pass


class _MutableRunRow(_SourceBase):
    __tablename__ = "test_mutable_runs"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'read-views.db'}")
    Base.metadata.create_all(database)
    _SourceBase.metadata.create_all(database)
    yield database
    database.dispose()


@pytest.fixture
def clock() -> _MutableUtcClock:
    return _MutableUtcClock()


def _binding(
    *,
    principal_binding: str = PRINCIPAL_A,
    authz_fingerprint: str = AUTHZ_A,
    query_hash: str = QUERY_HASH,
) -> MaterializedReadBinding:
    return MaterializedReadBinding(
        resource_kind="runs",
        query_hash=query_hash,
        authz_fingerprint=authz_fingerprint,
        stable_sort_schema_id="runs-created-at-id@1",
        view_schema_id="run-list-view@1",
        principal_binding=principal_binding,
    )


def _repository(
    session: Session,
    clock: _MutableUtcClock,
    *,
    page_size: int = 2,
    max_items: int = 10,
) -> SqlMaterializedReadViewRepository:
    return SqlMaterializedReadViewRepository(
        session,
        cursor_signer=CursorSigner(
            signing_key=b"materialized-read-view-test-signing-key",
            clock=clock,
        ),
        clock=clock,
        page_size=page_size,
        snapshot_ttl=timedelta(minutes=5),
        max_materialized_snapshot_items=max_items,
        snapshot_id_factory=lambda: "read-snapshot:runs:test",
    )


def _candidate(run_id: str, *, status: str = "queued", summary: str | None = None):
    view = {
        "run_id": run_id,
        "status": status,
        "revision": 1,
        "summary": summary or f"summary-{run_id}",
    }
    return MaterializedReadCandidate(
        resource_id=run_id,
        observed_revision=1,
        canonical_view=view,
    )


def _immutable_repository(
    session: Session,
    clock: _MutableUtcClock,
    *,
    page_size: int = 2,
) -> SqlImmutableReadViewRepository[dict[str, object]]:
    return SqlImmutableReadViewRepository(
        session,
        cursor_signer=CursorSigner(
            signing_key=b"immutable-read-view-test-signing-key",
            clock=clock,
        ),
        clock=clock,
        page_size=page_size,
        snapshot_ttl=timedelta(minutes=5),
        snapshot_id_factory=lambda: "read-snapshot:immutable:test",
    )


def _immutable_binding(
    *,
    principal_binding: str = PRINCIPAL_A,
    authz_fingerprint: str = AUTHZ_A,
) -> ImmutableReadBinding:
    return ImmutableReadBinding(
        resource_kind="artifacts",
        query_hash=QUERY_HASH,
        authz_fingerprint=authz_fingerprint,
        stable_sort_schema_id="artifact-id-asc@1",
        principal_binding=principal_binding,
    )


def test_immutable_high_watermark_excludes_cross_connection_inserts(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as setup, setup.begin():
        setup.add_all(
            _MutableRunRow(
                run_id=f"artifact:{suffix}",
                status="queued",
                summary=f"summary-{suffix}",
                revision=1,
            )
            for suffix in ("a", "b", "c")
        )

    def source(session: Session):
        def high_watermark() -> int:
            return int(session.scalar(select(func.count()).select_from(_MutableRunRow)) or 0)

        def load(after: str | None, high: int, limit: int):
            statement = select(_MutableRunRow).where(
                _MutableRunRow.run_id <= f"artifact:{chr(96 + high)}"
            )
            if after is not None:
                statement = statement.where(_MutableRunRow.run_id > after)
            rows = session.scalars(statement.order_by(_MutableRunRow.run_id).limit(limit)).all()
            return tuple(
                ImmutableReadCandidate(
                    resource_id=row.run_id,
                    source_position=row.run_id,
                    observed_sequence=ord(row.run_id[-1]) - 96,
                    observed_revision=row.revision,
                    item={"artifact_id": row.run_id, "summary": row.summary},
                )
                for row in rows
            )

        return high_watermark, load

    with Session(engine) as first_session, first_session.begin():
        high_watermark, load = source(first_session)
        first = _immutable_repository(first_session, clock).page(
            binding=_immutable_binding(),
            cursor=None,
            high_watermark=high_watermark,
            load_candidates=load,
        )
    assert [item["artifact_id"] for item in first.items] == ["artifact:a", "artifact:b"]
    assert first.next_cursor is not None

    with Session(engine) as mutation, mutation.begin():
        mutation.add(
            _MutableRunRow(
                run_id="artifact:d",
                status="queued",
                summary="inserted-later",
                revision=1,
            )
        )

    with Session(engine) as second_session, second_session.begin():
        high_watermark, load = source(second_session)
        second = _immutable_repository(second_session, clock).page(
            binding=_immutable_binding(),
            cursor=first.next_cursor,
            high_watermark=high_watermark,
            load_candidates=load,
        )
    assert [item["artifact_id"] for item in second.items] == ["artifact:c"]
    assert second.next_cursor is None


def test_immutable_cursor_distinguishes_principal_and_authz_changes(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    candidate = ImmutableReadCandidate(
        resource_id="artifact:a",
        source_position="artifact:a",
        observed_sequence=1,
        observed_revision=1,
        item={"artifact_id": "artifact:a"},
    )

    def loader(after: str | None, high: int, limit: int):
        del high, limit
        return (candidate,) if after is None else ()

    with Session(engine) as session, session.begin():
        first = _immutable_repository(session, clock, page_size=1).page(
            binding=_immutable_binding(),
            cursor=None,
            high_watermark=lambda: 2,
            load_candidates=lambda after, high, limit: (
                candidate,
                ImmutableReadCandidate(
                    resource_id="artifact:b",
                    source_position="artifact:b",
                    observed_sequence=2,
                    observed_revision=1,
                    item={"artifact_id": "artifact:b"},
                ),
            ),
        )
        assert first.next_cursor is not None
        with pytest.raises(Forbidden):
            _immutable_repository(session, clock, page_size=1).page(
                binding=_immutable_binding(
                    principal_binding=PRINCIPAL_B, authz_fingerprint=AUTHZ_B
                ),
                cursor=first.next_cursor,
                high_watermark=lambda: 2,
                load_candidates=loader,
            )
        with pytest.raises(CursorExpired):
            _immutable_repository(session, clock, page_size=1).page(
                binding=_immutable_binding(authz_fingerprint=AUTHZ_A_CHANGED),
                cursor=first.next_cursor,
                high_watermark=lambda: 2,
                load_candidates=loader,
            )


def test_immutable_page_rejects_unbounded_or_unstable_source_rows(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    def item(resource_id: str, sequence: int) -> ImmutableReadCandidate[dict[str, object]]:
        return ImmutableReadCandidate(
            resource_id=resource_id,
            source_position=resource_id,
            observed_sequence=sequence,
            observed_revision=1,
            item={"artifact_id": resource_id},
        )

    with Session(engine) as session:
        repository = _immutable_repository(session, clock, page_size=2)
        with pytest.raises(QueryTooBroad):
            repository.page(
                binding=_immutable_binding(),
                cursor=None,
                high_watermark=lambda: 4,
                load_candidates=lambda after, high, limit: tuple(
                    item(f"artifact:{index}", index) for index in range(1, 5)
                ),
            )
        session.rollback()

        with session.begin():
            with pytest.raises(IntegrityViolation, match="stable order"):
                repository.page(
                    binding=_immutable_binding(),
                    cursor=None,
                    high_watermark=lambda: 2,
                    load_candidates=lambda after, high, limit: (
                        item("artifact:b", 2),
                        item("artifact:a", 1),
                    ),
                )


def _source_candidates(session: Session) -> tuple[MaterializedReadCandidate, ...]:
    rows = session.scalars(
        select(_MutableRunRow)
        .where(_MutableRunRow.status == "queued")
        .order_by(_MutableRunRow.run_id)
        .limit(11)
    ).all()
    return tuple(
        MaterializedReadCandidate(
            resource_id=row.run_id,
            observed_revision=row.revision,
            canonical_view={
                "run_id": row.run_id,
                "status": row.status,
                "revision": row.revision,
                "summary": row.summary,
            },
        )
        for row in rows
    )


def test_materialized_pages_remain_exact_after_cross_connection_mutation(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        session.add_all(
            _MutableRunRow(
                run_id=f"run:{suffix}",
                status="queued",
                summary=f"original-{suffix}",
                revision=1,
            )
            for suffix in ("a", "b", "c", "d")
        )
        first = _repository(session, clock).create(
            _source_candidates(session),
            binding=_binding(),
        )

    assert [item.resource_id for item in first.items] == ["run:a", "run:b"]
    assert first.next_cursor is not None

    with Session(engine) as other, other.begin():
        other.add(
            _MutableRunRow(
                run_id="run:aa",
                status="queued",
                summary="inserted-after-snapshot",
                revision=1,
            )
        )
        other.execute(
            update(_MutableRunRow)
            .where(_MutableRunRow.run_id == "run:d")
            .values(status="failed", summary="changed-after-snapshot", revision=2)
        )
        other.execute(delete(_MutableRunRow).where(_MutableRunRow.run_id == "run:c"))

    with Session(engine) as reopened, reopened.begin():
        second = _repository(reopened, clock).page(first.next_cursor, binding=_binding())

    assert [item.resource_id for item in second.items] == ["run:c", "run:d"]
    assert [item.canonical_view["summary"] for item in second.items] == [
        "original-c",
        "original-d",
    ]
    assert second.next_cursor is None


def test_create_materializes_snapshot_and_items_atomically(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with Session(engine) as session, session.begin():
            _repository(session, clock).create(
                (_candidate("run:a"), _candidate("run:b")),
                binding=_binding(),
            )
            raise RuntimeError("rollback sentinel")

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ReadSnapshotRow)) == 0
        assert session.scalar(select(func.count()).select_from(MaterializedReadItemRow)) == 0


def test_max_plus_one_fails_without_persisting_a_partial_snapshot(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        with pytest.raises(QueryTooBroad, match="materialized snapshot"):
            _repository(session, clock, max_items=3).create(
                (_candidate(f"run:{index}") for index in range(4)),
                binding=_binding(),
            )
        assert session.scalar(select(func.count()).select_from(ReadSnapshotRow)) == 0
        assert session.scalar(select(func.count()).select_from(MaterializedReadItemRow)) == 0


def test_empty_materialized_snapshot_returns_one_terminal_empty_page(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        page = _repository(session, clock).create((), binding=_binding())

    assert page.items == ()
    assert page.next_cursor is None
    with Session(engine) as session:
        snapshot = session.get(ReadSnapshotRow, page.read_snapshot_id)
        assert snapshot is not None
        assert snapshot.strategy == "materialized_view"
        assert snapshot.materialized_item_count == 0
        assert snapshot.high_watermark is None


def test_cursor_distinguishes_cross_principal_from_same_principal_authz_change(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        first = _repository(session, clock, page_size=1).create(
            (_candidate("run:a"), _candidate("run:b")),
            binding=_binding(),
        )
    assert first.next_cursor is not None

    with Session(engine) as session:
        with pytest.raises(Forbidden, match="principal"):
            _repository(session, clock, page_size=1).page(
                first.next_cursor,
                binding=_binding(
                    principal_binding=PRINCIPAL_B,
                    authz_fingerprint=AUTHZ_B,
                ),
            )

    with Session(engine) as session:
        with pytest.raises(CursorExpired, match="authorization"):
            _repository(session, clock, page_size=1).page(
                first.next_cursor,
                binding=_binding(authz_fingerprint=AUTHZ_A_CHANGED),
            )


def test_cursor_rejects_query_reuse_page_size_change_and_expiry(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        first = _repository(session, clock, page_size=1).create(
            (_candidate("run:a"), _candidate("run:b")),
            binding=_binding(),
        )
    assert first.next_cursor is not None

    with Session(engine) as session:
        with pytest.raises(CursorInvalid, match="query"):
            _repository(session, clock, page_size=1).page(
                first.next_cursor,
                binding=_binding(query_hash="f" * 64),
            )
        with pytest.raises(CursorInvalid, match="page size"):
            _repository(session, clock, page_size=2).page(
                first.next_cursor,
                binding=_binding(),
            )

    clock.current = NOW + timedelta(minutes=5)
    with Session(engine) as session:
        with pytest.raises(CursorExpired, match="expired"):
            _repository(session, clock, page_size=1).page(
                first.next_cursor,
                binding=_binding(),
            )


def test_tampered_missing_snapshot_id_is_invalid_before_lookup_for_both_strategies(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    immutable_items = (
        ImmutableReadCandidate(
            resource_id="artifact:a",
            source_position="artifact:a",
            observed_sequence=1,
            observed_revision=1,
            item={"artifact_id": "artifact:a"},
        ),
        ImmutableReadCandidate(
            resource_id="artifact:b",
            source_position="artifact:b",
            observed_sequence=2,
            observed_revision=1,
            item={"artifact_id": "artifact:b"},
        ),
    )
    with Session(engine) as session, session.begin():
        immutable = _immutable_repository(session, clock, page_size=1).page(
            binding=_immutable_binding(),
            cursor=None,
            high_watermark=lambda: 2,
            load_candidates=lambda after, high, limit: immutable_items,
        )
        materialized = _repository(session, clock, page_size=1).create(
            (_candidate("run:a"), _candidate("run:b")),
            binding=_binding(),
        )
    assert immutable.next_cursor is not None
    assert materialized.next_cursor is not None

    for cursor, resume in (
        (
            immutable.next_cursor,
            lambda session, value: _immutable_repository(session, clock, page_size=1).page(
                binding=_immutable_binding(),
                cursor=value,
                high_watermark=lambda: 2,
                load_candidates=lambda after, high, limit: immutable_items,
            ),
        ),
        (
            materialized.next_cursor,
            lambda session, value: _repository(session, clock, page_size=1).page(
                value,
                binding=_binding(),
            ),
        ),
    ):
        tampered = cursor.model_copy(update={"snapshot_id": "read-snapshot:missing"})
        with Session(engine) as session, pytest.raises(CursorInvalid, match="signature"):
            resume(session, tampered)


def test_authentic_cursor_with_deleted_snapshot_is_expired(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        first = _repository(session, clock, page_size=1).create(
            (_candidate("run:a"), _candidate("run:b")),
            binding=_binding(),
        )
    assert first.next_cursor is not None
    with Session(engine) as session, session.begin():
        session.execute(
            delete(ReadSnapshotRow).where(ReadSnapshotRow.snapshot_id == first.read_snapshot_id)
        )

    with Session(engine) as session, pytest.raises(CursorExpired, match="no longer retained"):
        _repository(session, clock, page_size=1).page(
            first.next_cursor,
            binding=_binding(),
        )


def test_missing_materialized_row_expires_instead_of_returning_a_partial_page(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        first = _repository(session, clock, page_size=1).create(
            (_candidate("run:a"), _candidate("run:b"), _candidate("run:c")),
            binding=_binding(),
        )
    assert first.next_cursor is not None

    with Session(engine) as session, session.begin():
        session.execute(
            delete(MaterializedReadItemRow).where(
                MaterializedReadItemRow.snapshot_id == first.read_snapshot_id,
                MaterializedReadItemRow.ordinal == 2,
            )
        )

    with Session(engine) as session:
        with pytest.raises(CursorExpired, match="materialized view"):
            _repository(session, clock, page_size=1).page(
                first.next_cursor,
                binding=_binding(),
            )


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("view_hash", "f" * 64, "materialized read item"),
        ("view_schema_id", "wrong-view@1", "view schema"),
        ("observed_revision", 0, "materialized read item"),
    ],
)
def test_corrupt_materialized_item_fails_closed(
    engine: Engine,
    clock: _MutableUtcClock,
    column: str,
    value: object,
    message: str,
) -> None:
    with Session(engine) as session, session.begin():
        first = _repository(session, clock, page_size=1).create(
            (_candidate("run:a"), _candidate("run:b")),
            binding=_binding(),
        )
    assert first.next_cursor is not None

    with Session(engine) as session, session.begin():
        session.execute(
            update(MaterializedReadItemRow)
            .where(
                MaterializedReadItemRow.snapshot_id == first.read_snapshot_id,
                MaterializedReadItemRow.ordinal == 2,
            )
            .values({column: value})
        )

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match=message):
            _repository(session, clock, page_size=1).page(
                first.next_cursor,
                binding=_binding(),
            )


def test_corrupt_snapshot_metadata_fails_closed(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        first = _repository(session, clock, page_size=1).create(
            (_candidate("run:a"), _candidate("run:b")),
            binding=_binding(),
        )
    assert first.next_cursor is not None

    with Session(engine) as session, session.begin():
        snapshot = session.get(ReadSnapshotRow, first.read_snapshot_id)
        assert snapshot is not None
        snapshot.stable_sort_schema_id = "wrong-sort@1"

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="snapshot metadata"):
            _repository(session, clock, page_size=1).page(
                first.next_cursor,
                binding=_binding(),
            )


def test_duplicate_resource_id_is_rejected_before_any_snapshot_is_written(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        with pytest.raises(IntegrityViolation, match="resource_id"):
            _repository(session, clock).create(
                (_candidate("run:a"), _candidate("run:a")),
                binding=_binding(),
            )
        assert session.scalar(select(func.count()).select_from(ReadSnapshotRow)) == 0
