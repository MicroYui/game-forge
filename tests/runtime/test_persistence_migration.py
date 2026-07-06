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


def test_migration_creates_lineage_and_audit_schema_version_columns(tmp_path):
    """`artifacts.lineage_schema_version` / `audit.audit_schema_version` must
    exist so `contracts.lineage.Artifact.lineage_schema_version` /
    `AuditRecord.audit_schema_version` round-trip through the SQL store
    (Task 13) instead of being silently dropped (PRD §12A.3)."""
    url = f"sqlite:///{tmp_path / 't2.db'}"
    m.upgrade(url, "head")
    insp = inspect(get_engine(url))
    artifact_cols = {c["name"] for c in insp.get_columns("artifacts")}
    audit_cols = {c["name"] for c in insp.get_columns("audit")}
    assert "lineage_schema_version" in artifact_cols
    assert "audit_schema_version" in audit_cols
