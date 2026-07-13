"""Engine/session factory for the version/lineage/audit store (contract §5,
§12A.3).

`resolve_url()` / `get_engine()` / `get_sessionmaker()` are the single choke
point through which every consumer (this package's own Alembic `env.py`, the
CLI, `platform.lineage`/`platform.audit` in Task 13, tests) obtains a
DB connection — so swapping sqlite for Postgres in production is a
`DATABASE_URL` change, not a code change (PRD §12 Postgres-ready).

Default: unless `DATABASE_URL` is set, resolve to a local sqlite file
(`sqlite:///gameforge.db`) — this is also what the Alembic CLI path
(`uv run alembic upgrade head`) exercises. Tests and the programmatic
migration path (`migrations_api.upgrade/downgrade`) instead pass an explicit
`url` (e.g. a `tmp_path` sqlite file), bypassing this default entirely, so
its exact value never affects test determinism.
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL_ENV = "DATABASE_URL"
DEFAULT_URL = "sqlite:///gameforge.db"
SQLITE_BUSY_TIMEOUT_MS = 5_000


def _configure_sqlite_connection(
    dbapi_connection: Any,
    connection_record: Any,
) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        journal_mode_row = cursor.fetchone()
        journal_mode = "" if journal_mode_row is None else str(journal_mode_row[0]).lower()
        if journal_mode != "wal":
            raise RuntimeError(
                f"SQLite connection requires WAL journal mode; got {journal_mode or 'no result'}"
            )
    finally:
        cursor.close()


def configure_sqlite_engine(engine: Engine) -> Engine:
    """Attach the local SQLite invariants before the engine opens a connection."""

    if engine.dialect.name == "sqlite" and not event.contains(
        engine,
        "connect",
        _configure_sqlite_connection,
    ):
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def resolve_url() -> str:
    """`DATABASE_URL` env var if set, else the local sqlite default."""
    return os.environ.get(DATABASE_URL_ENV, DEFAULT_URL)


def get_engine(url: str | None = None) -> Engine:
    """Create a SQLAlchemy `Engine` for `url` (or `resolve_url()` if omitted).

    A fresh `Engine` per call is intentional and safe here: all urls used
    across this codebase are file-backed (sqlite file or a real RDBMS), so a
    new `Engine` instance still sees data written via a previous one. (An
    in-memory sqlite url would NOT have this property — each `Engine` gets
    its own private database — so callers must not rely on `:memory:` across
    multiple `get_engine()` calls.)
    """
    return configure_sqlite_engine(create_engine(url or resolve_url()))


def get_sessionmaker(engine: Engine | None = None) -> sessionmaker[Session]:
    """`sessionmaker` bound to `engine` (or a fresh default-url engine)."""
    return sessionmaker(bind=engine or get_engine())


def run_migrations(url: str | None = None) -> None:
    """Convenience helper: run Alembic `upgrade("head")` programmatically
    against `url` (or `resolve_url()`), via the single `migrations_api` code
    path shared with the test suite and the CLI.
    """
    from gameforge.runtime.persistence import migrations_api

    migrations_api.upgrade(url or resolve_url(), "head")
