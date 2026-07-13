"""SQLite write UnitOfWork with one transaction-bound Session."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.runtime.persistence.transaction import (
    TransactionCapabilities,
    TransactionHandle,
    TransactionHandleFactory,
    ensure_transaction_context_available,
)


CapabilityFactory = Callable[[Session], TransactionCapabilities]


class SqliteUnitOfWork:
    """Own physical commit/rollback for all capabilities in one SQLite write tx."""

    def __init__(self, engine: Engine, capability_factory: CapabilityFactory) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("SqliteUnitOfWork requires a SQLite engine")
        self._engine = engine
        self._capability_factory = capability_factory

    def begin(self) -> TransactionHandle:
        ensure_transaction_context_available()
        connection = self._engine.connect()
        session: Session | None = None
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            session = Session(
                bind=connection,
                autoflush=False,
                expire_on_commit=False,
                join_transaction_mode="control_fully",
            )
            capabilities = self._capability_factory(session)
            factory = TransactionHandleFactory()

            def finish_transaction(
                terminal_state: Literal["committed", "rolled_back"],
            ) -> None:
                try:
                    if terminal_state == "committed":
                        session.flush()
                        if session.in_transaction():
                            session.commit()
                        if connection.in_transaction():
                            connection.commit()
                    else:
                        if session.in_transaction():
                            session.rollback()
                        if connection.in_transaction():
                            connection.rollback()
                except BaseException:
                    if session.in_transaction():
                        session.rollback()
                    if connection.in_transaction():
                        connection.rollback()
                    raise
                finally:
                    session.close()
                    connection.close()

            return factory.begin(
                capabilities,
                finish_transaction=finish_transaction,
            )
        except BaseException:
            if session is not None:
                session.close()
            if connection.in_transaction():
                connection.rollback()
            connection.close()
            raise


__all__ = ["CapabilityFactory", "SqliteUnitOfWork"]
