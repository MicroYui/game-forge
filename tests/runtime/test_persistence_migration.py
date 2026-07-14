"""Alembic forward/rollback migration tests (contract §5, §12A.3)."""

from __future__ import annotations

import json
import sqlite3

import pytest
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
_COST_ROUTING_TABLES = {
    "budgets",
    "budget_set_snapshots",
    "budget_snapshots",
    "reservation_groups",
    "budget_reservations",
    "usage_entries",
    "permit_groups",
    "concurrency_permits",
    "model_catalog_snapshots",
    "routing_policies",
    "routing_decisions",
    "legacy_import_routing_decisions",
}
_SLO_ALERT_TABLES = {
    "workload_profiles",
    "slo_definitions",
    "alert_rules",
    "slo_evaluations",
    "alert_instances",
}
_AUTH_TABLES = {
    "password_credentials",
    "api_keys",
    "sessions",
}
_M4_TABLES = (
    _STORAGE_TABLES
    | _IDENTITY_WORKFLOW_TABLES
    | _RUN_TABLES
    | _COST_ROUTING_TABLES
    | _SLO_ALERT_TABLES
    | _AUTH_TABLES
)


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


def _foreign_keys(url: str, table: str) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    inspector, engine = _inspect(url)
    try:
        return {
            (
                tuple(foreign_key["constrained_columns"]),
                str(foreign_key["referred_table"]),
                tuple(foreign_key["referred_columns"]),
            )
            for foreign_key in inspector.get_foreign_keys(table)
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


def _column_is_nullable(url: str, table: str, column_name: str) -> bool:
    inspector, engine = _inspect(url)
    try:
        columns = {
            str(column["name"]): bool(column["nullable"]) for column in inspector.get_columns(table)
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


def _insert_pre_context_conflict_set(url: str) -> None:
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        artifact_id, lineage_schema_version, kind, version_tuple, lineage
                    ) VALUES (
                        'legacy-patch', 'lineage@1', 'patch', '{}', '[]'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO conflict_sets (
                        conflict_set_id, schema_version, base_snapshot_id,
                        current_snapshot_id, proposed_patch_artifact_id,
                        expected_ref_revision, conflict_count,
                        non_conflicting_ops_digest, created_at
                    ) VALUES (
                        'legacy-conflict-set', 'conflict-set@1', 'base-snapshot',
                        'current-snapshot', 'legacy-patch', 3, 0,
                        'sha256:legacy-non-conflicting-ops', '2026-07-13T00:00:00Z'
                    )
                    """
                )
            )
    finally:
        engine.dispose()


def _insert_pre_context_orphan_merge_conflict(database_path: str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO merge_conflicts (
                conflict_set_id, ordinal, conflict_id, path, kind,
                base, current, proposed, allowed_resolutions
            ) VALUES (
                'orphan-set', 1, 'orphan-conflict', '/value', 'concurrent_change',
                '{"presence":"present","value":1}',
                '{"presence":"present","value":2}',
                '{"presence":"present","value":3}',
                '["keep_current","take_proposed"]'
            )
            """
        )


def _insert_current_conflict_set(url: str) -> None:
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        artifact_id, lineage_schema_version, kind, version_tuple, lineage
                    ) VALUES (
                        'current-patch', 'lineage@1', 'patch', '{}', '[]'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO conflict_sets (
                        conflict_set_id, schema_version, base_snapshot_id,
                        current_snapshot_id, proposed_patch_artifact_id,
                        expected_ref_revision, conflict_count,
                        non_conflicting_ops_digest, created_at, context, content_digest
                    ) VALUES (
                        'current-conflict-set', 'conflict-set@1', 'base-snapshot',
                        'current-snapshot', 'current-patch', 3, 1,
                        :digest, '2026-07-13T00:00:00Z', '{}', :digest
                    )
                    """
                ),
                {"digest": "a" * 64},
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


def _insert_populated_0007_fixture(url: str) -> None:
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO object_bindings (
                        object_key, store_id, binding_schema_version,
                        object_ref_schema_version, location_schema_version,
                        object_sha256, object_size_bytes, backend_generation,
                        etag, storage_class, status, revision, verified_at
                    ) VALUES (
                        'objects/populated', 'local', 'object-binding@1',
                        'object-ref@1', 'object-location@1', :digest, 7, 'generation-1',
                        'etag-1', 'standard', 'active', 1, '2026-07-14T00:00:00Z'
                    )
                    """
                ),
                {"digest": "a" * 64},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO principals (
                        principal_id, principal_schema_version, kind, display_name,
                        status, credential_epoch, authz_revision, revision,
                        created_at, updated_at, disabled_at, disabled_reason
                    ) VALUES (
                        'human-populated', 'principal@1', 'human', 'Populated Human',
                        'active', 2, 3, 4,
                        '2026-07-14T00:00:00Z', '2026-07-14T00:01:00Z', NULL, NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO runs (
                        run_id, run_schema_version, kind, kind_version, status, revision,
                        idempotency_scope, idempotency_key, request_hash, payload, payload_hash,
                        run_kind_definition_digest, outcome_policy_set_digest,
                        migration_capability_matrix, failure_classifier, dispatch_trace_carrier,
                        initiated_by, queue_deadline_utc, attempt_timeout_ns,
                        overall_deadline_utc, cancel_requested_at, cancel_requested_by,
                        current_attempt_no, next_attempt_no, next_fencing_token, next_event_seq,
                        budget_set_snapshot_id, run_budget_hold_group_id,
                        concurrency_permit_group_id, retry_policy, max_attempts,
                        retry_not_before_utc, result_artifact_id, failure_artifact_id,
                        terminal_cassette_artifact_id, created_at, updated_at
                    ) VALUES (
                        'run-populated', 'run@1', 'checker.run', 1, 'queued', 1,
                        'test', 'request-1', :request_hash, '{}', :payload_hash,
                        :definition_digest, :outcome_digest,
                        NULL, '{}', NULL, '{}',
                        '2026-07-14T01:00:00Z', 1000000000,
                        '2026-07-14T02:00:00Z', NULL, NULL,
                        NULL, 1, 1, 1,
                        'budget-set-populated', 'budget-hold-populated',
                        NULL, '{}', 1,
                        NULL, NULL, NULL, NULL,
                        '2026-07-14T00:00:00Z', '2026-07-14T00:00:00Z'
                    )
                    """
                ),
                {
                    "request_hash": "b" * 64,
                    "payload_hash": "c" * 64,
                    "definition_digest": "d" * 64,
                    "outcome_digest": "e" * 64,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO budgets (
                        budget_id, scope_kind, scope_id, policy_version, status,
                        revision, deadline_utc, created_at, payload
                    ) VALUES (
                        'budget-populated', 'run', 'run-populated', 'budget-policy@1',
                        'active', 2, '2026-07-14T02:00:00Z',
                        '2026-07-14T00:00:00Z', :payload
                    )
                    """
                ),
                {"payload": '{"token_limit":100}'},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO workload_profiles (
                        profile_id, dataset_artifact_id, entity_count, relation_count,
                        constraint_count, task_count, concurrency,
                        environment_fingerprint, payload
                    ) VALUES (
                        'workload-populated', 'artifact-dataset', 10, 20, 3, 4, 2,
                        :fingerprint, '{"profile":"populated"}'
                    )
                    """
                ),
                {"fingerprint": "f" * 64},
            )
    finally:
        engine.dispose()
    _insert_current_conflict_set(url)


def _populated_0007_projection(url: str) -> dict[str, list[tuple[object, ...]]]:
    queries = {
        "object_bindings": "SELECT * FROM object_bindings ORDER BY object_key, store_id",
        "principals": "SELECT * FROM principals ORDER BY principal_id",
        "runs": "SELECT * FROM runs ORDER BY run_id",
        "conflict_sets": "SELECT * FROM conflict_sets ORDER BY conflict_set_id",
        "budgets": "SELECT * FROM budgets ORDER BY budget_id",
        "workload_profiles": "SELECT * FROM workload_profiles ORDER BY profile_id",
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
    assert "context" not in _column_names(url, "conflict_sets")
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

    m.upgrade(url, "0005")
    assert _current_revision(url) == "0005"
    assert "context" in _column_names(url, "conflict_sets")
    assert "content_digest" in _column_names(url, "conflict_sets")
    assert "content_digest" in _column_names(url, "merge_conflicts")
    assert not _column_is_nullable(url, "conflict_sets", "context")
    assert not _column_is_nullable(url, "conflict_sets", "content_digest")
    assert not _column_is_nullable(url, "merge_conflicts", "content_digest")

    m.upgrade(url, "0006")
    assert _current_revision(url) == "0006"
    assert _COST_ROUTING_TABLES <= _table_names(url)
    assert not (_SLO_ALERT_TABLES & _table_names(url))
    assert _primary_key(url, "budgets") == ("budget_id",)
    assert _primary_key(url, "budget_snapshots") == ("snapshot_id",)
    assert ("run_id",) in _unique_keys(url, "budget_set_snapshots")
    assert ("run_id", "scope", "idempotency_key") in _unique_keys(
        url,
        "reservation_groups",
    )
    assert ("catalog_version", "catalog_digest") in _unique_keys(
        url,
        "model_catalog_snapshots",
    )
    assert _primary_key(url, "routing_decisions") == ("decision_id",)
    assert (
        "run_id",
        "attempt_no",
        "request_hash",
        "fallback_index",
    ) not in _unique_keys(url, "routing_decisions")
    assert ("usage_identity",) in _unique_keys(url, "usage_entries")
    assert ("run_id", "lease_id", "fencing_token") in _unique_keys(
        url,
        "permit_groups",
    )

    m.upgrade(url, "0007")
    assert _current_revision(url) == "0007"
    assert _SLO_ALERT_TABLES <= _table_names(url)
    assert not (_AUTH_TABLES & _table_names(url))
    assert _primary_key(url, "workload_profiles") == ("profile_id",)
    assert _primary_key(url, "slo_definitions") == ("slo_id",)
    assert _primary_key(url, "alert_rules") == ("alert_rule_id",)
    assert _primary_key(url, "slo_evaluations") == ("evaluation_id",)
    assert _primary_key(url, "alert_instances") == ("alert_instance_id",)
    assert ("alert_rule_id", "dedup_key") in _unique_keys(url, "alert_instances")
    assert _indexes(url, "slo_evaluations")["ix_slo_evaluations_order"] == (
        "slo_id",
        "window_start",
        "window_end",
        "evaluation_id",
    )
    assert _indexes(url, "alert_instances")["ix_alert_instances_state"] == (
        "state",
        "alert_rule_id",
        "alert_instance_id",
    )

    tables_before_auth = _table_names(url)
    m.upgrade(url, "0008")
    assert _current_revision(url) == "0008"
    tables_after_auth = _table_names(url)
    assert tables_after_auth - tables_before_auth == _AUTH_TABLES
    assert not any(name.startswith("oidc") for name in tables_after_auth)
    assert _column_names(url, "password_credentials") == {
        "credential_id",
        "principal_id",
        "normalized_login_name",
        "normalization_policy_version",
        "normalization_policy_digest",
        "password_hash",
        "hash_policy_version",
        "credential_version",
        "status",
        "changed_at",
        "revision",
    }
    assert _column_names(url, "api_keys") == {
        "api_key_id",
        "principal_id",
        "key_prefix",
        "key_digest",
        "credential_version",
        "status",
        "created_at",
        "expires_at",
        "revoked_at",
        "revision",
    }
    assert _column_names(url, "sessions") == {
        "session_id",
        "principal_id",
        "source_credential_id",
        "credential_version",
        "token_digest",
        "csrf_secret_digest",
        "signing_key_id",
        "issued_at",
        "absolute_expires_at",
        "idle_expires_at",
        "last_seen_at",
        "revoked_at",
        "revoke_reason",
        "revision",
    }
    assert ("normalized_login_name",) in _unique_keys(url, "password_credentials")
    assert ("key_digest",) in _unique_keys(url, "api_keys")
    assert ("token_digest",) in _unique_keys(url, "sessions")
    assert _indexes(url, "password_credentials")["ix_password_credentials_principal_status"] == (
        "principal_id",
        "status",
        "credential_id",
    )
    assert _indexes(url, "api_keys")["ix_api_keys_principal_status"] == (
        "principal_id",
        "status",
        "api_key_id",
    )
    assert _indexes(url, "sessions")["ix_sessions_principal_expiry"] == (
        "principal_id",
        "absolute_expires_at",
        "session_id",
    )
    assert _indexes(url, "sessions")["ix_sessions_source_credential"] == (
        "source_credential_id",
        "credential_version",
        "session_id",
    )
    assert (("principal_id",), "principals", ("principal_id",)) in _foreign_keys(
        url, "password_credentials"
    )
    assert (("principal_id",), "principals", ("principal_id",)) in _foreign_keys(url, "api_keys")
    assert (("principal_id",), "principals", ("principal_id",)) in _foreign_keys(url, "sessions")
    assert all(
        foreign_key[0] != ("source_credential_id",)
        for foreign_key in _foreign_keys(url, "sessions")
    )

    m.downgrade(url, "0007")
    assert not (_AUTH_TABLES & _table_names(url))

    m.downgrade(url, "0006")
    assert not (_SLO_ALERT_TABLES & _table_names(url))

    m.downgrade(url, "0005")
    assert not (_COST_ROUTING_TABLES & _table_names(url))

    m.downgrade(url, "0004")
    assert "context" not in _column_names(url, "conflict_sets")
    assert "content_digest" not in _column_names(url, "conflict_sets")
    assert "content_digest" not in _column_names(url, "merge_conflicts")
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
    assert _current_revision(url) == "0008"
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
    assert _current_revision(url) == "0008"
    assert _legacy_projection(url) == expected


def test_populated_0007_survives_0008_downgrade_and_reupgrade(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'populated-0007.db'}"
    m.upgrade(url, "0007")
    _insert_populated_0007_fixture(url)
    expected = _populated_0007_projection(url)

    m.upgrade(url, "0008")
    assert _current_revision(url) == "0008"
    assert _populated_0007_projection(url) == expected
    assert not any(_fetch_one(url, f"SELECT COUNT(*) FROM {table}")[0] for table in _AUTH_TABLES)

    m.downgrade(url, "0007")
    assert _current_revision(url) == "0007"
    assert _populated_0007_projection(url) == expected

    m.upgrade(url, "0008")
    assert _current_revision(url) == "0008"
    assert _populated_0007_projection(url) == expected
    assert not any(_fetch_one(url, f"SELECT COUNT(*) FROM {table}")[0] for table in _AUTH_TABLES)


def test_0004_empty_conflict_store_upgrades_to_required_context_and_downgrades(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'empty-conflicts.db'}"
    m.upgrade(url, "0004")
    assert "context" not in _column_names(url, "conflict_sets")

    m.upgrade(url, "head")
    assert _current_revision(url) == "0008"
    assert "context" in _column_names(url, "conflict_sets")
    assert "content_digest" in _column_names(url, "conflict_sets")
    assert "content_digest" in _column_names(url, "merge_conflicts")
    assert not _column_is_nullable(url, "conflict_sets", "context")

    m.downgrade(url, "0004")
    assert _current_revision(url) == "0004"
    assert "context" not in _column_names(url, "conflict_sets")
    assert "content_digest" not in _column_names(url, "conflict_sets")
    assert "content_digest" not in _column_names(url, "merge_conflicts")


def test_0004_nonempty_conflict_store_refuses_to_invent_context(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'nonempty-conflicts.db'}"
    m.upgrade(url, "0004")
    _insert_pre_context_conflict_set(url)

    with pytest.raises(
        RuntimeError,
        match="cannot add required conflict_sets.context while legacy rows exist",
    ):
        m.upgrade(url, "head")

    assert _current_revision(url) == "0004"
    assert "context" not in _column_names(url, "conflict_sets")
    assert _fetch_one(
        url,
        "SELECT conflict_set_id FROM conflict_sets",
    ) == ("legacy-conflict-set",)


def test_0004_orphan_merge_conflict_refuses_upgrade_before_any_ddl(tmp_path) -> None:
    database_path = tmp_path / "orphan-conflict.db"
    url = f"sqlite:///{database_path}"
    m.upgrade(url, "0004")
    _insert_pre_context_orphan_merge_conflict(str(database_path))

    with pytest.raises(RuntimeError, match="legacy conflict rows exist"):
        m.upgrade(url, "head")

    assert _current_revision(url) == "0004"
    assert "context" not in _column_names(url, "conflict_sets")
    assert "content_digest" not in _column_names(url, "conflict_sets")
    assert "content_digest" not in _column_names(url, "merge_conflicts")
    assert _fetch_one(
        url,
        "SELECT conflict_id FROM merge_conflicts",
    ) == ("orphan-conflict",)


def test_0005_downgrade_refuses_to_discard_retained_conflict_authority(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'retained-current-conflict.db'}"
    m.upgrade(url, "head")
    _insert_current_conflict_set(url)

    with pytest.raises(RuntimeError, match="cannot remove immutable conflict-set"):
        m.downgrade(url, "0004")

    assert _current_revision(url) == "0005"
    assert "context" in _column_names(url, "conflict_sets")
    assert "content_digest" in _column_names(url, "conflict_sets")
    assert _fetch_one(
        url,
        "SELECT conflict_set_id FROM conflict_sets",
    ) == ("current-conflict-set",)


def test_0006_downgrade_refuses_to_discard_retained_cost_authority(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'retained-cost.db'}"
    m.upgrade(url, "head")
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO budgets (
                        budget_id, scope_kind, scope_id, policy_version, status,
                        revision, deadline_utc, created_at, payload
                    ) VALUES (
                        'retained-budget', 'system', 'system', 'policy@1',
                        'active', 1, NULL, '2026-07-14T00:00:00Z', '{}'
                    )
                    """
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="cannot remove authoritative cost/routing"):
        m.downgrade(url, "0005")

    assert _current_revision(url) == "0006"
    assert _fetch_one(url, "SELECT budget_id FROM budgets") == ("retained-budget",)


def test_0007_downgrade_refuses_to_discard_retained_slo_authority(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'retained-slo.db'}"
    m.upgrade(url, "head")
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO workload_profiles (
                        profile_id, dataset_artifact_id, entity_count, relation_count,
                        constraint_count, task_count, concurrency,
                        environment_fingerprint, payload
                    ) VALUES (
                        'retained-profile', 'artifact-profile', 1, 1,
                        1, NULL, 1, :fingerprint, '{}'
                    )
                    """
                ),
                {"fingerprint": "a" * 64},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="cannot remove authoritative SLO/alert"):
        m.downgrade(url, "0006")

    assert _current_revision(url) == "0007"
    assert _fetch_one(url, "SELECT profile_id FROM workload_profiles") == ("retained-profile",)


def test_0008_downgrade_refuses_to_discard_retained_auth_authority(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'retained-auth.db'}"
    m.upgrade(url, "head")
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO principals (
                        principal_id, principal_schema_version, kind, display_name,
                        status, credential_epoch, authz_revision, revision,
                        created_at, updated_at, disabled_at, disabled_reason
                    ) VALUES (
                        'human-retained', 'principal@1', 'human', 'Retained Human',
                        'active', 0, 0, 1,
                        '2026-07-14T00:00:00Z', '2026-07-14T00:00:00Z', NULL, NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO password_credentials (
                        credential_id, principal_id, normalized_login_name,
                        normalization_policy_version, normalization_policy_digest,
                        password_hash, hash_policy_version, credential_version,
                        status, changed_at, revision
                    ) VALUES (
                        'password-retained', 'human-retained', 'retained',
                        'normalization@1', :digest, 'argon2-hash', 'argon2@1', 1,
                        'active', '2026-07-14T00:00:00Z', 1
                    )
                    """
                ),
                {"digest": "a" * 64},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="cannot remove authoritative auth"):
        m.downgrade(url, "0007")

    assert _current_revision(url) == "0008"
    assert _fetch_one(url, "SELECT credential_id FROM password_credentials") == (
        "password-retained",
    )


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
