from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextvars import copy_context

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session

from gameforge.contracts.errors import InvalidStateTransition, TransactionClosed
from gameforge.runtime.persistence.engine import SQLITE_BUSY_TIMEOUT_MS, get_engine
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


class _ProbeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def session_identity(self) -> int:
        return id(self._session)

    def connection_identity(self) -> int:
        return id(self._session.connection())

    def write(self, key: str, value: str) -> None:
        self._session.execute(
            text("INSERT INTO uow_probe (key, value) VALUES (:key, :value)"),
            {"key": key, "value": value},
        )

    def read(self, key: str) -> str | None:
        return self._session.execute(
            text("SELECT value FROM uow_probe WHERE key = :key"),
            {"key": key},
        ).scalar_one_or_none()


def _capability_factory(session: Session) -> TransactionCapabilities:
    return TransactionCapabilities(
        refs=_ProbeRepository(session),
        audit=_ProbeRepository(session),
        approvals=_ProbeRepository(session),
        lineage=_ProbeRepository(session),
        object_bindings=_ProbeRepository(session),
        runs=_ProbeRepository(session),
        cost=_ProbeRepository(session),
    )


@pytest.fixture
def sqlite_engine(tmp_path) -> Iterator[Engine]:
    engine = get_engine(f"sqlite:///{tmp_path / 'uow.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE uow_probe (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
    yield engine
    engine.dispose()


def _read_outside_transaction(engine: Engine, key: str) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT value FROM uow_probe WHERE key = :key"),
            {"key": key},
        ).scalar_one_or_none()


def test_every_checked_out_sqlite_connection_has_required_pragmas(
    sqlite_engine: Engine,
) -> None:
    assert SQLITE_BUSY_TIMEOUT_MS > 0

    with sqlite_engine.connect() as first, sqlite_engine.connect() as second:
        for connection in (first, second):
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
            assert (
                connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
                == SQLITE_BUSY_TIMEOUT_MS
            )


def test_sqlite_engine_rejects_connection_when_wal_is_unavailable() -> None:
    engine = get_engine("sqlite:///:memory:")
    try:
        with pytest.raises(RuntimeError, match="WAL"):
            engine.connect()
    finally:
        engine.dispose()


def test_write_uow_issues_begin_immediate_before_repository_sql(
    sqlite_engine: Engine,
) -> None:
    statements: list[str] = []

    def capture_statement(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        statements.append(" ".join(statement.upper().split()))

    event.listen(sqlite_engine, "before_cursor_execute", capture_statement)
    try:
        with SqliteUnitOfWork(sqlite_engine, _capability_factory).begin() as transaction:
            transaction.refs.write("ordered", "yes")
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", capture_statement)

    assert statements[0] == "BEGIN IMMEDIATE"
    assert statements.index("BEGIN IMMEDIATE") < next(
        index for index, statement in enumerate(statements) if statement.startswith("INSERT ")
    )


def test_capabilities_share_one_session_connection_and_transaction_view(
    sqlite_engine: Engine,
) -> None:
    outside = sqlite_engine.connect()
    try:
        with SqliteUnitOfWork(sqlite_engine, _capability_factory).begin() as transaction:
            assert transaction.refs.session_identity() == transaction.audit.session_identity()
            assert transaction.refs.connection_identity() == transaction.audit.connection_identity()

            transaction.refs.write("shared", "visible-inside")
            assert transaction.audit.read("shared") == "visible-inside"
            assert (
                outside.execute(
                    text("SELECT value FROM uow_probe WHERE key = 'shared'")
                ).scalar_one_or_none()
                is None
            )
    finally:
        outside.close()

    assert _read_outside_transaction(sqlite_engine, "shared") == "visible-inside"


def test_normal_context_exit_commits_all_capability_writes(
    sqlite_engine: Engine,
) -> None:
    with SqliteUnitOfWork(sqlite_engine, _capability_factory).begin() as transaction:
        transaction.refs.write("refs", "committed")
        transaction.audit.write("audit", "committed")
        assert _read_outside_transaction(sqlite_engine, "refs") is None
        assert _read_outside_transaction(sqlite_engine, "audit") is None

    assert _read_outside_transaction(sqlite_engine, "refs") == "committed"
    assert _read_outside_transaction(sqlite_engine, "audit") == "committed"


def test_exception_rolls_back_every_capability_and_releases_resources(
    sqlite_engine: Engine,
) -> None:
    unit_of_work = SqliteUnitOfWork(sqlite_engine, _capability_factory)

    with pytest.raises(RuntimeError, match="sentinel"):
        with unit_of_work.begin() as transaction:
            transaction.refs.write("refs", "rolled-back")
            transaction.audit.write("audit", "rolled-back")
            raise RuntimeError("sentinel")

    assert _read_outside_transaction(sqlite_engine, "refs") is None
    assert _read_outside_transaction(sqlite_engine, "audit") is None

    with unit_of_work.begin() as transaction:
        transaction.refs.write("after", "usable")
    assert _read_outside_transaction(sqlite_engine, "after") == "usable"


@pytest.mark.parametrize(
    ("terminal_action", "expected_value"),
    [("commit", "persisted"), ("rollback", None)],
)
def test_direct_terminal_action_controls_database_and_expires_proxies(
    sqlite_engine: Engine,
    terminal_action: str,
    expected_value: str | None,
) -> None:
    statements_after_close: list[str] = []

    with SqliteUnitOfWork(sqlite_engine, _capability_factory).begin() as transaction:
        escaped = transaction.refs
        captured_read: Callable[[str], str | None] = escaped.read
        escaped.write("direct", "persisted")
        getattr(transaction, terminal_action)()

        def capture_statement(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            del connection, cursor, parameters, context, executemany
            statements_after_close.append(statement)

        event.listen(sqlite_engine, "before_cursor_execute", capture_statement)
        try:
            with pytest.raises(TransactionClosed, match="closed"):
                escaped.read("direct")
            with pytest.raises(TransactionClosed, match="closed"):
                captured_read("direct")
        finally:
            event.remove(sqlite_engine, "before_cursor_execute", capture_statement)

    assert statements_after_close == []
    assert _read_outside_transaction(sqlite_engine, "direct") == expected_value


def test_nested_uows_fail_closed_without_poisoning_outer_or_next_transaction(
    sqlite_engine: Engine,
) -> None:
    outer_uow = SqliteUnitOfWork(sqlite_engine, _capability_factory)
    other_uow = SqliteUnitOfWork(sqlite_engine, _capability_factory)

    with outer_uow.begin() as outer:
        outer.refs.write("before-nested", "kept")
        with pytest.raises(InvalidStateTransition, match="nested"):
            with outer_uow.begin():
                pass
        with pytest.raises(InvalidStateTransition, match="nested"):
            with other_uow.begin():
                pass
        outer.audit.write("after-nested", "kept")

    with other_uow.begin() as next_transaction:
        assert next_transaction.refs.read("before-nested") == "kept"
        assert next_transaction.audit.read("after-nested") == "kept"
        next_transaction.refs.write("next", "usable")

    assert _read_outside_transaction(sqlite_engine, "next") == "usable"


@pytest.mark.parametrize("terminal_action", ["commit", "rollback"])
@pytest.mark.parametrize("child_context", ["copied", "async-task"])
def test_child_context_cannot_finish_owning_database_transaction(
    sqlite_engine: Engine,
    terminal_action: str,
    child_context: str,
) -> None:
    transaction = SqliteUnitOfWork(sqlite_engine, _capability_factory).begin()
    transaction.refs.write("context-bound", terminal_action)
    finish = getattr(transaction, terminal_action)

    if child_context == "copied":

        def invoke_in_child() -> None:
            copy_context().run(finish)

    else:

        async def invoke_async() -> None:
            finish()

        def invoke_in_child() -> None:
            asyncio.run(invoke_async())

    with pytest.raises(InvalidStateTransition, match="owning UnitOfWork context"):
        invoke_in_child()

    assert transaction.state == "active"
    assert _read_outside_transaction(sqlite_engine, "context-bound") is None

    finish()
    expected = terminal_action if terminal_action == "commit" else None
    assert _read_outside_transaction(sqlite_engine, "context-bound") == expected
