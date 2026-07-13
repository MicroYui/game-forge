from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import Engine, delete, event, select
from sqlalchemy.orm import Session

from gameforge.contracts.errors import (
    Conflict,
    CursorExpired,
    CursorInvalid,
    IntegrityViolation,
)
from gameforge.contracts.storage import RefValue
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    Base,
    ReadSnapshotRow,
    RefHistoryRow,
    RefRow,
)
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
SIGNING_KEY = b"revisioned-ref-store-test-signing-key"


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'refs.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _repository(
    session: Session,
    *,
    now: datetime = NOW,
    page_size: int = 2,
    signing_key: bytes = SIGNING_KEY,
) -> SqlRefStore:
    clock = FrozenUtcClock(now)
    return SqlRefStore(
        session,
        cursor_signer=CursorSigner(signing_key=signing_key, clock=clock),
        clock=clock,
        page_size=page_size,
        snapshot_ttl=timedelta(minutes=5),
    )


def _unit_of_work(
    engine: Engine,
    *,
    now: datetime = NOW,
    page_size: int = 2,
) -> SqliteUnitOfWork:
    def capabilities(session: Session) -> TransactionCapabilities:
        unavailable = object()
        return TransactionCapabilities(
            refs=_repository(session, now=now, page_size=page_size),
            audit=unavailable,
            approvals=unavailable,
            lineage=unavailable,
            object_bindings=unavailable,
            runs=unavailable,
            cost=unavailable,
        )

    return SqliteUnitOfWork(engine, capabilities)


def _collect_history(
    engine: Engine,
    name: str,
    *,
    page_size: int = 2,
) -> list[RefValue]:
    items: list[RefValue] = []
    cursor = None
    while True:
        with Session(engine) as session, session.begin():
            page = _repository(session, page_size=page_size).history(name, cursor)
        items.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return items


def test_absent_get_and_history_are_distinct_from_create(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)

        assert repository.get("head") is None
        assert repository.get_history_entry("head", 1) is None
        page = repository.history("head")

    assert page.items == ()
    assert page.next_cursor is None


def test_create_requires_expected_null_and_appends_revision_one(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)

        created = repository.compare_and_set("head", None, "artifact-a")
        assert created == RefValue(artifact_id="artifact-a", revision=1)
        assert repository.get("head") == created

        with pytest.raises(Conflict, match="expected absent"):
            repository.compare_and_set("head", None, "artifact-b")

    assert _collect_history(engine, "head") == [created]


def test_update_matches_artifact_and_revision_and_blocks_aba(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        first = repository.compare_and_set("head", None, "artifact-a")
        second = repository.compare_and_set("head", first, "artifact-b")
        third = repository.compare_and_set("head", second, "artifact-a")

        with pytest.raises(Conflict, match="compare-and-set"):
            repository.compare_and_set("head", first, "artifact-c")

        assert repository.get("head") == third
        assert repository.get_history_entry("head", 1) == first
        assert repository.get_history_entry("head", 2) == second
        assert repository.get_history_entry("head", 3) == third
        assert repository.get_history_entry("head", 4) is None

    assert [item.artifact_id for item in _collect_history(engine, "head")] == [
        "artifact-a",
        "artifact-b",
        "artifact-a",
    ]
    assert [item.revision for item in _collect_history(engine, "head")] == [1, 2, 3]


def test_failed_cas_creates_no_revision_gap_and_never_queries_max(engine: Engine) -> None:
    statements: list[str] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        statements.append(" ".join(statement.upper().split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as session, session.begin():
            repository = _repository(session)
            first = repository.compare_and_set("head", None, "artifact-a")
            with pytest.raises(Conflict):
                repository.compare_and_set(
                    "head",
                    RefValue(artifact_id="artifact-wrong", revision=first.revision),
                    "artifact-b",
                )
            second = repository.compare_and_set("head", first, "artifact-b")
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert second.revision == 2
    assert [item.revision for item in _collect_history(engine, "head")] == [1, 2]
    assert not any("MAX(" in statement for statement in statements)


def test_uow_rollback_removes_current_and_history_together(engine: Engine) -> None:
    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with _unit_of_work(engine).begin() as transaction:
            transaction.refs.compare_and_set("head", None, "artifact-a")
            raise RuntimeError("rollback sentinel")

    with Session(engine) as session:
        assert session.get(RefRow, "head") is None
        assert session.scalars(select(RefHistoryRow)).all() == []


def test_history_is_keyset_paginated_at_an_immutable_high_watermark(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        current = repository.compare_and_set("head", None, "artifact-1")
        for revision in range(2, 6):
            current = repository.compare_and_set(
                "head",
                current,
                f"artifact-{revision}",
            )
        first_page = repository.history("head")

    assert [item.revision for item in first_page.items] == [1, 2]
    assert first_page.next_cursor is not None
    assert first_page.next_cursor.position == "2"

    with Session(engine) as session:
        snapshot = session.get(ReadSnapshotRow, first_page.read_snapshot_id)
        assert snapshot is not None
        assert snapshot.strategy == "immutable_high_watermark"
        assert snapshot.high_watermark == 5
        assert snapshot.materialized_item_count is None

    with Session(engine) as session, session.begin():
        repository = _repository(session)
        current = repository.get("head")
        assert current is not None
        repository.compare_and_set("head", current, "artifact-6")

    with Session(engine) as session, session.begin():
        repository = _repository(session)
        second_page = repository.history("head", first_page.next_cursor)
    assert [item.revision for item in second_page.items] == [3, 4]
    assert second_page.next_cursor is not None

    with Session(engine) as session, session.begin():
        final_page = _repository(session).history("head", second_page.next_cursor)
    assert [item.revision for item in final_page.items] == [5]
    assert final_page.next_cursor is None
    assert [item.revision for item in _collect_history(engine, "head")] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]


def test_history_cursor_is_bound_to_ref_name(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session, page_size=1)
        head = repository.compare_and_set("head", None, "head-1")
        repository.compare_and_set("head", head, "head-2")
        other = repository.compare_and_set("other", None, "other-1")
        repository.compare_and_set("other", other, "other-2")
        first_page = repository.history("head")

    assert first_page.next_cursor is not None
    with Session(engine) as session:
        with pytest.raises(CursorInvalid, match="another query"):
            _repository(session, page_size=1).history("other", first_page.next_cursor)


def test_history_cursor_survives_repository_and_connection_recreation(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session, page_size=1)
        first = repository.compare_and_set("head", None, "artifact-1")
        repository.compare_and_set("head", first, "artifact-2")
        first_page = repository.history("head")

    assert first_page.next_cursor is not None
    with Session(engine) as new_session, new_session.begin():
        continued = _repository(new_session, page_size=1).history(
            "head",
            first_page.next_cursor,
        )
    assert continued.items == (RefValue(artifact_id="artifact-2", revision=2),)

    with Session(engine) as session:
        with pytest.raises(CursorInvalid, match="signature"):
            _repository(
                session,
                page_size=1,
                signing_key=b"different-key",
            ).history("head", first_page.next_cursor)


def test_history_cursor_expires_against_injected_utc_clock(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session, page_size=1)
        first = repository.compare_and_set("head", None, "artifact-1")
        repository.compare_and_set("head", first, "artifact-2")
        first_page = repository.history("head")

    assert first_page.next_cursor is not None
    with Session(engine) as session:
        with pytest.raises(CursorExpired, match="expired"):
            _repository(
                session,
                now=NOW + timedelta(minutes=5),
                page_size=1,
            ).history("head", first_page.next_cursor)


@pytest.mark.parametrize(
    ("artifact_id", "revision"),
    [("", 1), ("artifact-a", 0)],
)
def test_get_fails_closed_on_corrupt_current_row(
    engine: Engine,
    artifact_id: str,
    revision: int,
) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            RefRow(
                name="head",
                artifact_id=artifact_id,
                revision=revision,
                updated_at="2026-07-14T08:00:00Z",
            )
        )

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="current ref"):
            _repository(session).get("head")


def test_get_and_cas_fail_closed_when_current_history_disagree(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        created = _repository(session).compare_and_set("head", None, "artifact-a")

    with Session(engine) as session, session.begin():
        row = session.scalar(
            select(RefHistoryRow).where(
                RefHistoryRow.name == "head",
                RefHistoryRow.seq == created.revision,
            )
        )
        assert row is not None
        row.artifact_id = "different-artifact"

    with Session(engine) as session:
        repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="current/history"):
            repository.get("head")
        with pytest.raises(IntegrityViolation, match="current/history"):
            repository.get_history_entry("head", 1)
        with pytest.raises(IntegrityViolation, match="current/history"):
            repository.compare_and_set("head", created, "artifact-b")


def test_get_history_entry_fails_closed_when_retained_revision_is_missing(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        first = repository.compare_and_set("head", None, "artifact-a")
        repository.compare_and_set("head", first, "artifact-b")

    with Session(engine) as session, session.begin():
        session.execute(
            delete(RefHistoryRow).where(
                RefHistoryRow.name == "head",
                RefHistoryRow.seq == 1,
            )
        )

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="history entry is missing"):
            _repository(session).get_history_entry("head", 1)


def test_get_history_entry_fails_closed_when_intermediate_revision_is_missing(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        first = repository.compare_and_set("head", None, "artifact-a")
        second = repository.compare_and_set("head", first, "artifact-b")
        repository.compare_and_set("head", second, "artifact-c")

    with Session(engine) as session, session.begin():
        session.execute(
            delete(RefHistoryRow).where(
                RefHistoryRow.name == "head",
                RefHistoryRow.seq == 2,
            )
        )

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="noncontiguous"):
            _repository(session).get_history_entry("head", 1)


@pytest.mark.parametrize(("name", "revision"), [("", 1), ("head", 0), ("head", True)])
def test_get_history_entry_rejects_noncanonical_lookup_keys(
    engine: Engine,
    name: str,
    revision: int,
) -> None:
    with Session(engine) as session:
        with pytest.raises(IntegrityViolation):
            _repository(session).get_history_entry(name, revision)


@pytest.mark.parametrize("corruption", ["missing-middle", "invalid-artifact", "ahead"])
def test_history_fails_closed_on_corrupt_sequence(
    engine: Engine,
    corruption: str,
) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session, page_size=10)
        current = repository.compare_and_set("head", None, "artifact-1")
        current = repository.compare_and_set("head", current, "artifact-2")
        repository.compare_and_set("head", current, "artifact-3")

    with Session(engine) as session, session.begin():
        if corruption == "missing-middle":
            session.execute(
                delete(RefHistoryRow).where(
                    RefHistoryRow.name == "head",
                    RefHistoryRow.seq == 2,
                )
            )
        elif corruption == "invalid-artifact":
            row = session.scalar(
                select(RefHistoryRow).where(
                    RefHistoryRow.name == "head",
                    RefHistoryRow.seq == 2,
                )
            )
            assert row is not None
            row.artifact_id = ""
        else:
            session.add(RefHistoryRow(name="head", artifact_id="future", seq=4))

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="ref history"):
            _repository(session, page_size=10).history("head")


def test_orphan_history_without_current_ref_fails_closed(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(RefHistoryRow(name="head", artifact_id="orphan", seq=1))

    with Session(engine) as session:
        repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="without a current ref"):
            repository.get("head")
        with pytest.raises(IntegrityViolation, match="without a current ref"):
            repository.compare_and_set("head", None, "artifact-a")


def test_corrupt_persisted_read_snapshot_fails_closed(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session, page_size=1)
        first = repository.compare_and_set("head", None, "artifact-1")
        repository.compare_and_set("head", first, "artifact-2")
        first_page = repository.history("head")

    assert first_page.next_cursor is not None
    with Session(engine) as session, session.begin():
        snapshot = session.get(ReadSnapshotRow, first_page.read_snapshot_id)
        assert snapshot is not None
        snapshot.resource_kind = "artifacts"

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="read snapshot metadata"):
            _repository(session, page_size=1).history("head", first_page.next_cursor)


def test_two_sqlite_writers_have_one_cas_winner_and_one_typed_conflict(
    engine: Engine,
) -> None:
    with _unit_of_work(engine).begin() as transaction:
        expected = transaction.refs.compare_and_set("head", None, "artifact-a")

    ready = Barrier(2)

    def compete(artifact_id: str) -> tuple[str, RefValue | None]:
        ready.wait()
        try:
            with _unit_of_work(engine).begin() as transaction:
                value = transaction.refs.compare_and_set("head", expected, artifact_id)
            return ("winner", value)
        except Conflict:
            return ("conflict", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(compete, ("artifact-b", "artifact-c")))

    assert sorted(outcome for outcome, _ in outcomes) == ["conflict", "winner"]
    winner = next(value for outcome, value in outcomes if outcome == "winner")
    assert winner is not None
    assert winner.revision == 2

    with Session(engine) as session:
        current = _repository(session).get("head")
    assert current == winner
    history = _collect_history(engine, "head")
    assert [item.revision for item in history] == [1, 2]
    assert [item.artifact_id for item in history] == ["artifact-a", winner.artifact_id]
