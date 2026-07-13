"""Alembic forward/rollback migration tests (contract §5, §12A.3)."""

from __future__ import annotations

import json

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Inspector, inspect, text

from gameforge.contracts.canonical import compute_snapshot_id
from gameforge.runtime.persistence import migrations_api as m
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base


_LEGACY_TABLES = {"artifacts", "refs", "ref_history", "audit"}
_STORAGE_TABLES = {
    "object_bindings",
    "read_snapshots",
    "materialized_read_items",
    "audit_heads",
}
_IDENTITY_WORKFLOW_TABLES = {
    "principals",
    "role_assignments",
    "policy_snapshots",
    "subject_heads",
    "approval_items",
    "approval_decisions",
    "finding_revisions",
    "finding_heads",
    "conflict_sets",
    "merge_conflicts",
    "ref_transitions",
    "idempotency_records",
}
_RUN_TABLES = {
    "runs",
    "run_attempts",
    "run_leases",
    "run_events",
    "run_commands",
    "run_intermediate_artifact_links",
    "run_finding_links",
}
_M4_TABLES = _STORAGE_TABLES | _IDENTITY_WORKFLOW_TABLES | _RUN_TABLES


def _inspect(url: str) -> tuple[Inspector, object]:
    engine = get_engine(url)
    return inspect(engine), engine


def _table_names(url: str) -> set[str]:
    inspector, engine = _inspect(url)
    try:
        return set(inspector.get_table_names())
    finally:
        engine.dispose()


def _column_names(url: str, table: str) -> set[str]:
    inspector, engine = _inspect(url)
    try:
        return {column["name"] for column in inspector.get_columns(table)}
    finally:
        engine.dispose()


def _primary_key(url: str, table: str) -> tuple[str, ...]:
    inspector, engine = _inspect(url)
    try:
        return tuple(inspector.get_pk_constraint(table)["constrained_columns"])
    finally:
        engine.dispose()


def _unique_keys(url: str, table: str) -> set[tuple[str, ...]]:
    inspector, engine = _inspect(url)
    try:
        keys = {
            tuple(index["column_names"])
            for index in inspector.get_indexes(table)
            if index["unique"]
        }
        keys.update(
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table)
        )
        primary_key = tuple(inspector.get_pk_constraint(table)["constrained_columns"])
        if primary_key:
            keys.add(primary_key)
        return keys
    finally:
        engine.dispose()


def _indexes(url: str, table: str) -> dict[str, tuple[str, ...]]:
    inspector, engine = _inspect(url)
    try:
        return {
            str(index["name"]): tuple(index["column_names"])
            for index in inspector.get_indexes(table)
        }
    finally:
        engine.dispose()


def _column_type(url: str, table: str, column_name: str) -> str:
    inspector, engine = _inspect(url)
    try:
        columns = {
            str(column["name"]): str(column["type"]).upper()
            for column in inspector.get_columns(table)
        }
        return columns[column_name]
    finally:
        engine.dispose()


def _sqlite_schema_sql(url: str, object_type: str, name: str) -> str:
    engine = get_engine(url)
    try:
        with engine.connect() as connection:
            value = connection.execute(
                text("SELECT sql FROM sqlite_master WHERE type = :object_type AND name = :name"),
                {"object_type": object_type, "name": name},
            ).scalar_one()
            return str(value)
    finally:
        engine.dispose()


def _current_revision(url: str) -> str:
    engine = get_engine(url)
    try:
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            return str(revision)
    finally:
        engine.dispose()


def _fetch_one(url: str, statement: str) -> tuple[object, ...]:
    engine = get_engine(url)
    try:
        with engine.connect() as connection:
            return tuple(connection.execute(text(statement)).one())
    finally:
        engine.dispose()


def _insert_legacy_fixture(url: str) -> None:
    version_tuple = json.dumps(
        {
            "doc_version": "doc@legacy",
            "ir_snapshot_id": "sha256:legacy-snapshot",
            "constraint_snapshot_id": None,
            "prompt_version": None,
            "model_snapshot": None,
            "agent_graph_version": None,
            "tool_version": "legacy-tool@1",
            "env_contract_version": None,
            "seed": 7,
            "cassette_id": None,
        },
        separators=(",", ":"),
    )
    lineage = '["parent-z","parent-a"]'
    meta = '{"legacy":true,"nested":{"value":1}}'

    first_audit_hash = compute_snapshot_id(
        {
            "actor": "legacy-human",
            "action": "legacy.create",
            "artifact_id": "legacy-artifact",
            "ts": "2026-07-06T00:00:00Z",
            "prev_hash": None,
        }
    )
    second_audit_hash = compute_snapshot_id(
        {
            "actor": "legacy-worker",
            "action": "legacy.move-ref",
            "artifact_id": None,
            "ts": "2026-07-06T00:00:01Z",
            "prev_hash": first_audit_hash,
        }
    )

    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        artifact_id, lineage_schema_version, kind, version_tuple,
                        lineage, payload_hash, created_at, meta
                    ) VALUES (
                        :artifact_id, :schema, :kind, :version_tuple,
                        :lineage, :payload_hash, :created_at, :meta
                    )
                    """
                ),
                {
                    "artifact_id": "legacy-artifact",
                    "schema": "lineage@1",
                    "kind": "ir_snapshot",
                    "version_tuple": version_tuple,
                    "lineage": lineage,
                    "payload_hash": "sha256:legacy-payload",
                    "created_at": "2026-07-06T00:00:00Z",
                    "meta": meta,
                },
            )
            # Legacy refs were not required to point at a persisted Artifact.
            # M4 migrations must not retroactively reject or rewrite this row.
            connection.execute(
                text(
                    "INSERT INTO refs (name, artifact_id, updated_at) "
                    "VALUES ('legacy-head', 'legacy-target-v2', '2026-07-06T00:00:01Z')"
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO ref_history (id, name, artifact_id, seq) VALUES
                        (11, 'legacy-head', 'legacy-target-v1', 1),
                        (12, 'legacy-head', 'legacy-target-v2', 2)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO audit (
                        seq, audit_schema_version, actor, action, artifact_id,
                        ts, content_hash, prev_hash
                    ) VALUES (
                        21, 'audit@1', 'legacy-human', 'legacy.create',
                        'legacy-artifact', '2026-07-06T00:00:00Z', :first_hash, NULL
                    ), (
                        22, 'audit@1', 'legacy-worker', 'legacy.move-ref',
                        NULL, '2026-07-06T00:00:01Z', :second_hash, :first_hash
                    )
                    """
                ),
                {"first_hash": first_audit_hash, "second_hash": second_audit_hash},
            )
    finally:
        engine.dispose()


def _legacy_projection(url: str) -> dict[str, list[tuple[object, ...]]]:
    queries = {
        "artifacts": """
            SELECT artifact_id, lineage_schema_version, kind, version_tuple,
                   lineage, payload_hash, created_at, meta
            FROM artifacts ORDER BY artifact_id
        """,
        "refs": "SELECT name, artifact_id, updated_at FROM refs ORDER BY name",
        "ref_history": """
            SELECT id, name, artifact_id, seq FROM ref_history ORDER BY id
        """,
        "audit": """
            SELECT seq, audit_schema_version, actor, action, artifact_id,
                   ts, content_hash, prev_hash
            FROM audit ORDER BY seq
        """,
    }
    engine = get_engine(url)
    try:
        with engine.connect() as connection:
            return {
                table: [tuple(row) for row in connection.execute(text(statement)).all()]
                for table, statement in queries.items()
            }
    finally:
        engine.dispose()


def test_migration_forward_creates_tables_then_rollback_drops(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 't.db'}"
    m.upgrade(url, "head")
    assert _LEGACY_TABLES | _M4_TABLES <= _table_names(url)

    m.downgrade(url, "base")
    assert not ((_LEGACY_TABLES | _M4_TABLES) & _table_names(url))


def test_migration_creates_lineage_and_audit_schema_version_columns(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 't2.db'}"
    m.upgrade(url, "head")
    assert "lineage_schema_version" in _column_names(url, "artifacts")
    assert "audit_schema_version" in _column_names(url, "audit")


def test_linear_m4_revisions_own_only_their_schema_slice(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'linear.db'}"

    m.upgrade(url, "0001")
    assert _current_revision(url) == "0001"
    assert not (_M4_TABLES & _table_names(url))
    assert "object_ref" not in _column_names(url, "artifacts")
    assert "revision" not in _column_names(url, "refs")

    m.upgrade(url, "0002")
    assert _current_revision(url) == "0002"
    assert _STORAGE_TABLES <= _table_names(url)
    assert not ((_IDENTITY_WORKFLOW_TABLES | _RUN_TABLES) & _table_names(url))
    assert {"object_ref"} <= _column_names(url, "artifacts")
    assert {"revision"} <= _column_names(url, "refs")
    assert {
        "chain_id",
        "chain_seq",
        "actor_v2",
        "initiated_by",
        "subject",
        "correlation",
    } <= _column_names(url, "audit")
    assert ("name", "seq") in _unique_keys(url, "ref_history")
    assert {
        "object_key",
        "object_sha256",
        "object_size_bytes",
        "store_id",
        "backend_generation",
        "status",
        "revision",
        "verified_at",
    } <= _column_names(url, "object_bindings")
    assert ("object_key", "store_id") in _unique_keys(url, "object_bindings")
    assert _primary_key(url, "materialized_read_items") == ("snapshot_id", "ordinal")

    m.upgrade(url, "0003")
    assert _current_revision(url) == "0003"
    assert _IDENTITY_WORKFLOW_TABLES <= _table_names(url)
    assert not (_RUN_TABLES & _table_names(url))
    assert {
        "principal_id",
        "status",
        "credential_epoch",
        "authz_revision",
        "revision",
    } <= _column_names(url, "principals")
    assert {
        "approval_id",
        "subject_series_id",
        "subject_revision",
        "subject_artifact_id",
        "status",
        "workflow_revision",
    } <= _column_names(url, "approval_items")
    assert _primary_key(url, "policy_snapshots") == (
        "document_kind",
        "document_id",
        "document_version",
    )
    assert _primary_key(url, "approval_decisions") == ("decision_id",)
    assert _indexes(url, "approval_decisions")["ix_approval_decisions_order"] == (
        "approval_id",
        "occurred_at",
        "decision_id",
    )
    assert _primary_key(url, "finding_revisions") == ("finding_id", "revision")
    assert _primary_key(url, "finding_heads") == ("finding_id",)
    assert _primary_key(url, "merge_conflicts") == ("conflict_set_id", "ordinal")
    assert {("conflict_set_id", "conflict_id"), ("conflict_set_id", "path")} <= (
        _unique_keys(url, "merge_conflicts")
    )
    assert _indexes(url, "merge_conflicts")["ix_merge_conflicts_path"] == (
        "conflict_set_id",
        "path",
        "ordinal",
    )
    assert "WHERE active_validation_run_id IS NOT NULL" in _sqlite_schema_sql(
        url,
        "index",
        "uq_approval_active_validation_run",
    )
    assert _primary_key(url, "idempotency_records") == ("scope", "operation", "key")

    m.upgrade(url, "0004")
    assert _current_revision(url) == "0004"
    assert _RUN_TABLES <= _table_names(url)
    assert {
        "run_id",
        "status",
        "revision",
        "idempotency_scope",
        "idempotency_key",
        "request_hash",
        "next_attempt_no",
        "next_fencing_token",
        "next_event_seq",
    } <= _column_names(url, "runs")
    assert ("idempotency_scope", "idempotency_key") in _unique_keys(url, "runs")
    assert _column_type(url, "runs", "kind_version") == "INTEGER"
    assert _primary_key(url, "run_attempts") == ("run_id", "attempt_no")
    assert ("run_id", "fencing_token") in _unique_keys(url, "run_attempts")
    assert _primary_key(url, "run_events") == ("run_id", "seq")
    assert _primary_key(url, "run_intermediate_artifact_links") == (
        "run_id",
        "attempt_no",
        "call_ordinal",
    )
    assert _primary_key(url, "run_finding_links") == ("run_id", "attempt_no", "ordinal")
    assert ("run_id", "finding_id", "finding_revision") in _unique_keys(
        url,
        "run_finding_links",
    )
    assert ("run_id", "client_id", "client_seq") in _unique_keys(url, "run_commands")

    m.downgrade(url, "0003")
    assert not (_RUN_TABLES & _table_names(url))
    m.downgrade(url, "0002")
    assert not (_IDENTITY_WORKFLOW_TABLES & _table_names(url))
    m.downgrade(url, "0001")
    assert not (_STORAGE_TABLES & _table_names(url))
    assert "object_ref" not in _column_names(url, "artifacts")
    assert "revision" not in _column_names(url, "refs")


def test_legacy_projection_survives_0001_head_0001_head_round_trip(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    m.upgrade(url, "0001")
    _insert_legacy_fixture(url)
    expected = _legacy_projection(url)

    m.upgrade(url, "head")
    assert _current_revision(url) == "0004"
    assert _legacy_projection(url) == expected
    assert _fetch_one(
        url,
        "SELECT object_ref FROM artifacts WHERE artifact_id = 'legacy-artifact'",
    ) == (None,)
    assert _fetch_one(url, "SELECT revision FROM refs WHERE name = 'legacy-head'") == (2,)
    assert _fetch_one(
        url,
        """
        SELECT chain_id, chain_seq, actor_v2, initiated_by, subject, correlation
        FROM audit WHERE seq = 21
        """,
    ) == (None, None, None, None, None, None)

    m.downgrade(url, "0001")
    assert _current_revision(url) == "0001"
    assert _legacy_projection(url) == expected

    m.upgrade(url, "head")
    assert _current_revision(url) == "0004"
    assert _legacy_projection(url) == expected


def test_alembic_head_matches_runtime_metadata(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'metadata-parity.db'}"
    m.upgrade(url, "head")

    engine = get_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()
