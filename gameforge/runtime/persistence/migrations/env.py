"""Alembic environment script (contract §5, §12A.3).

Targets `gameforge.runtime.persistence.models.Base.metadata` for
autogenerate, and reads `sqlalchemy.url` from the `Config` it is handed
rather than any hardcoded value — `migrations_api.py` sets that url per
call (to a tmp sqlite file in tests, to `DATABASE_URL`/the local default
elsewhere), and this file must honor whatever it was given.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from gameforge.runtime.persistence.models import Base

# Alembic Config object; provides access to values within the .ini in use
# (as overridden by migrations_api.py's `set_main_option` calls).
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for 'autogenerate' support.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode: emit SQL against a URL only, no
    live DBAPI connection required."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode: create an Engine from the config
    (whose `sqlalchemy.url` reflects whatever `migrations_api.py` set) and
    associate a connection with the migration context."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
