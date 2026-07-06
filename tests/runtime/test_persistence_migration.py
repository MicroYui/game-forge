"""Alembic forward/rollback migration test (contract §5, §12A.3).

Drives the SAME code path CI/CLI uses (`migrations_api.upgrade`/`downgrade`),
against a throwaway sqlite file, and asserts the four version/lineage/audit
tables appear on upgrade and disappear on downgrade.
"""

from sqlalchemy import inspect

from gameforge.runtime.persistence import migrations_api as m
from gameforge.runtime.persistence.engine import get_engine


def test_migration_forward_creates_tables_then_rollback_drops(tmp_path):
    url = f"sqlite:///{tmp_path / 't.db'}"
    m.upgrade(url, "head")
    insp = inspect(get_engine(url))
    tables = set(insp.get_table_names())
    assert {"artifacts", "refs", "ref_history", "audit"} <= tables
    m.downgrade(url, "base")
    insp2 = inspect(get_engine(url))
    assert not ({"artifacts", "refs", "ref_history", "audit"} & set(insp2.get_table_names()))
