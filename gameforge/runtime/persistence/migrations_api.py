"""Single Alembic entrypoint shared by tests, the CLI, and any future caller
(contract §5, §12A.3).

`upgrade`/`downgrade` build the Alembic `Config` and drive `alembic.command`
directly — this is the ONE code path exercised by both
`tests/runtime/test_persistence_migration.py` and
`uv run alembic -c alembic.ini upgrade head` / `downgrade base`, so there is
no divergence between what a test proves and what the CLI/CI does.

`script_location` and `sqlalchemy.url` are both pinned via absolute
overrides on the loaded `Config` (rather than trusting `alembic.ini`'s own
relative-path resolution), so this works regardless of the caller's current
working directory — in particular from a pytest run rooted anywhere.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

# gameforge/runtime/persistence/migrations_api.py -> repo root is 3 parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _config(url: str) -> Config:
    """Build an Alembic `Config` from `alembic.ini`, pinned to this
    package's `migrations/` dir and to `url`, independent of CWD."""
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url)
    return config


def upgrade(url: str, rev: str = "head") -> None:
    """Run Alembic upgrade to `rev` (default `"head"`) against `url`."""
    command.upgrade(_config(url), rev)


def downgrade(url: str, rev: str = "base") -> None:
    """Run Alembic downgrade to `rev` (default `"base"`) against `url`."""
    command.downgrade(_config(url), rev)


def expected_heads(url: str) -> tuple[str, ...]:
    """Return the stable exact Alembic script heads for readiness checks."""

    return tuple(sorted(ScriptDirectory.from_config(_config(url)).get_heads()))


__all__ = ["downgrade", "expected_heads", "upgrade"]
