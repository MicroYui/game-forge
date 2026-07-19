"""Alembic forward/rollback migration tests (contract §5, §12A.3)."""

from __future__ import annotations

import importlib
import json
import sqlite3
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Inspector, MetaData, Table, inspect, text
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import compute_snapshot_id
from gameforge.contracts.cost import CacheHitObservationV1, MonetaryObservationV1
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence import migrations_api as m
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base
from tests.runtime.cost.ledger_testkit import (
    amount,
    budget,
    budget_set,
    hold,
    seed_current_attempt,
    step_group,
    uow,
    usage,
)


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
_HOLD_BALANCE_TABLES = {"run_hold_balances"}
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
_MODEL_CALL_TABLES = {
    "run_model_route_links",
    "run_model_response_consumptions",
}
_TOOL_CONTEXT_TABLES = {"run_tool_intermediate_links"}
_M4_TABLES = (
    _STORAGE_TABLES
    | _IDENTITY_WORKFLOW_TABLES
    | _RUN_TABLES
    | _COST_ROUTING_TABLES
    | _SLO_ALERT_TABLES
    | _AUTH_TABLES
    | _MODEL_CALL_TABLES
    | _TOOL_CONTEXT_TABLES
    | _HOLD_BALANCE_TABLES
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


def _insert_duplicate_run_command_ids(url: str) -> None:
    engine = get_engine(url)
    try:
        metadata = MetaData()
        runs = Table("runs", metadata, autoload_with=engine)
        commands = Table("run_commands", metadata, autoload_with=engine)
        with engine.begin() as connection:
            for ordinal in (1, 2):
                run_id = f"run:duplicate-command:{ordinal}"
                connection.execute(
                    runs.insert().values(
                        run_id=run_id,
                        run_schema_version="run@1",
                        kind="checker.run",
                        kind_version=1,
                        status="queued",
                        revision=1,
                        idempotency_scope="migration:test",
                        idempotency_key=f"run:{ordinal}",
                        request_hash=f"{ordinal}" * 64,
                        payload={},
                        payload_hash="a" * 64,
                        run_kind_definition_digest="b" * 64,
                        outcome_policy_set_digest="c" * 64,
                        failure_classifier={},
                        initiated_by={},
                        queue_deadline_utc="2026-07-17T01:00:00Z",
                        attempt_timeout_ns=1_000_000_000,
                        overall_deadline_utc="2026-07-17T02:00:00Z",
                        next_attempt_no=1,
                        next_fencing_token=1,
                        next_event_seq=1,
                        budget_set_snapshot_id="budget-set:migration",
                        run_budget_hold_group_id=f"hold:{ordinal}",
                        retry_policy={},
                        max_attempts=1,
                        created_at="2026-07-17T00:00:00Z",
                        updated_at="2026-07-17T00:00:00Z",
                    )
                )
                connection.execute(
                    commands.insert().values(
                        run_id=run_id,
                        command_id="command:duplicate",
                        record_schema_version="run-command-record@1",
                        command_schema_version="run-command@1",
                        client_id=f"browser:{ordinal}",
                        client_seq=1,
                        idempotency_key=f"command:{ordinal}",
                        expected_run_revision=1,
                        type="provide_input",
                        payload_schema_id="playtest-provide-input@1",
                        payload={
                            "schema_version": "playtest-provide-input@1",
                            "interaction_id": f"interaction:{ordinal}",
                            "expected_state_hash": "a" * 64,
                            "choice_id": "choice:a",
                        },
                        request_hash=f"{ordinal + 2}" * 64,
                        actor={"principal_id": "human:a", "principal_kind": "human"},
                        status="pending",
                        revision=1,
                        created_at="2026-07-17T00:00:00Z",
                    )
                )
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


def _insert_0008_prompt_link(url: str) -> None:
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        artifact_id, lineage_schema_version, kind, version_tuple,
                        lineage, payload_hash, created_at, meta, object_ref
                    ) VALUES (
                        'artifact:prompt:migration', 'lineage@1', 'source_rendered', '{}',
                        '[]', :hash, '2026-07-16T00:00:00Z', '{}', NULL
                    )
                    """
                ),
                {"hash": "a" * 64},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO idempotency_records (
                        scope, operation, key, request_hash, resource_kind,
                        resource_id, created_at, updated_at, response
                    ) VALUES (
                        'run:migration/attempt:1', 'worker.prompt-rendered@1',
                        'prompt:1', :hash, 'source_rendered',
                        'artifact:prompt:migration', '2026-07-16T00:00:01Z',
                        '2026-07-16T00:00:01Z', :response
                    )
                    """
                ),
                {
                    "hash": "a" * 64,
                    "response": json.dumps(
                        {
                            "artifact_id": "artifact:prompt:migration",
                            "link": {
                                "link_schema_version": "run-intermediate-link@1",
                                "run_id": "run:migration",
                                "attempt_no": 1,
                                "call_ordinal": 1,
                                "artifact_id": "artifact:prompt:migration",
                                "role": "prompt_rendered",
                                "request_hash": "a" * 64,
                                "fencing_token": 1,
                                "published_at": "2026-07-16T00:00:01Z",
                            },
                        },
                        separators=(",", ":"),
                    ),
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO runs (
                        run_id, run_schema_version, kind, kind_version, status, revision,
                        idempotency_scope, idempotency_key, request_hash, payload,
                        payload_hash, run_kind_definition_digest, outcome_policy_set_digest,
                        migration_capability_matrix, failure_classifier, dispatch_trace_carrier,
                        initiated_by, queue_deadline_utc, attempt_timeout_ns,
                        overall_deadline_utc, cancel_requested_at, cancel_requested_by,
                        current_attempt_no, next_attempt_no, next_fencing_token, next_event_seq,
                        budget_set_snapshot_id, run_budget_hold_group_id,
                        concurrency_permit_group_id, retry_policy, max_attempts,
                        retry_not_before_utc, result_artifact_id, failure_artifact_id,
                        terminal_cassette_artifact_id, created_at, updated_at
                    ) VALUES (
                        'run:migration', 'run@1', 'review.run', 1, 'failed', 1,
                        'migration', 'prompt-route', :hash, '{}', :hash, :hash, :hash,
                        NULL, '{}', NULL, '{}', '2026-07-16T00:01:00Z', 1000000000,
                        '2026-07-16T00:10:00Z', NULL, NULL, 1, 2, 2, 1,
                        'budget-set:migration', 'reservation:migration', NULL, '{}', 1,
                        NULL, NULL, NULL, NULL,
                        '2026-07-16T00:00:00Z', '2026-07-16T00:00:00Z'
                    )
                    """
                ),
                {"hash": "b" * 64},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO run_attempts (
                        run_id, attempt_no, status, fencing_token, worker_principal_id,
                        trace_id, next_call_ordinal, started_at, attempt_deadline_utc,
                        ended_at, failure_class, retryable, failure_artifact_id,
                        cassette_bundle_artifact_id
                    ) VALUES (
                        'run:migration', 1, 'failed', 1, 'service:migration', NULL, 2,
                        '2026-07-16T00:00:00Z', '2026-07-16T00:01:00Z',
                        '2026-07-16T00:00:30Z', 'provider', 0, NULL, NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO run_intermediate_artifact_links (
                        run_id, attempt_no, call_ordinal, link_schema_version,
                        artifact_id, role, request_hash, fencing_token, published_at
                    ) VALUES (
                        'run:migration', 1, 1, 'run-intermediate-link@1',
                        'artifact:prompt:migration', 'prompt_rendered', :hash, 1,
                        '2026-07-16T00:00:01Z'
                    )
                    """
                ),
                {"hash": "a" * 64},
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

    m.upgrade(url, "0009")
    assert _current_revision(url) == "0009"
    assert _MODEL_CALL_TABLES <= _table_names(url)
    assert "resource_domain_scope" in _column_names(url, "runs")
    assert "route_ordinal" in _column_names(url, "run_intermediate_artifact_links")
    assert _primary_key(url, "run_intermediate_artifact_links") == (
        "run_id",
        "attempt_no",
        "call_ordinal",
        "route_ordinal",
    )
    assert _primary_key(url, "run_model_route_links") == (
        "run_id",
        "attempt_no",
        "call_ordinal",
        "route_ordinal",
    )
    assert _primary_key(url, "run_model_response_consumptions") == (
        "run_id",
        "attempt_no",
        "call_ordinal",
        "route_ordinal",
    )
    assert ("run_id", "attempt_no", "call_ordinal") in _unique_keys(
        url, "run_model_response_consumptions"
    )
    assert "route_ordinal >= 1" in _sqlite_schema_sql(
        url, "table", "run_intermediate_artifact_links"
    )
    m.upgrade(url, "0010")
    assert _current_revision(url) == "0010"
    assert _TOOL_CONTEXT_TABLES <= _table_names(url)
    assert _primary_key(url, "run_tool_intermediate_links") == (
        "run_id",
        "attempt_no",
        "target_call_ordinal",
    )
    assert ("run_id", "attempt_no", "artifact_id") in _unique_keys(
        url, "run_tool_intermediate_links"
    )
    m.upgrade(url, "0011")
    assert _current_revision(url) == "0011"
    assert ("command_id",) in _unique_keys(url, "run_commands")
    m.upgrade(url, "0012")
    assert _current_revision(url) == "0012"
    assert _primary_key(url, "run_hold_balances") == (
        "hold_group_id",
        "budget_id",
    )
    assert (
        ("hold_group_id", "budget_id"),
        "budget_reservations",
        ("reservation_group_id", "budget_id"),
    ) in _foreign_keys(url, "run_hold_balances")
    m.upgrade(url, "0013")
    assert _current_revision(url) == "0013"
    assert _indexes(url, "run_commands")["ix_run_commands_result_event"] == (
        "run_id",
        "result_event_seq",
    )
    m.upgrade(url, "0014")
    assert _current_revision(url) == "0014"
    assert "approval_evidence_bindings" in _table_names(url)
    assert _primary_key(url, "approval_evidence_bindings") == ("artifact_id",)
    m.downgrade(url, "0013")
    assert "approval_evidence_bindings" not in _table_names(url)
    m.downgrade(url, "0012")
    assert "ix_run_commands_result_event" not in _indexes(url, "run_commands")
    m.downgrade(url, "0011")
    assert "run_hold_balances" not in _table_names(url)
    m.downgrade(url, "0010")
    assert ("command_id",) not in _unique_keys(url, "run_commands")
    m.downgrade(url, "0009")
    assert not (_TOOL_CONTEXT_TABLES & _table_names(url))
    m.downgrade(url, "0008")
    assert not (_MODEL_CALL_TABLES & _table_names(url))
    assert "resource_domain_scope" not in _column_names(url, "runs")
    assert "route_ordinal" not in _column_names(url, "run_intermediate_artifact_links")

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
    assert _current_revision(url) == "0014"
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
    assert _current_revision(url) == "0014"
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


def test_0009_upgrades_old_prompt_links_to_route_one_and_round_trips(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'prompt-route-upgrade.db'}"
    m.upgrade(url, "0008")
    _insert_0008_prompt_link(url)

    m.upgrade(url, "0009")
    assert _fetch_one(
        url,
        "SELECT resource_domain_scope FROM runs WHERE run_id = 'run:migration'",
    ) == (None,)
    assert _fetch_one(
        url,
        "SELECT call_ordinal, route_ordinal, artifact_id "
        "FROM run_intermediate_artifact_links WHERE run_id = 'run:migration'",
    ) == (1, 1, "artifact:prompt:migration")
    assert _fetch_one(
        url,
        "SELECT json_extract(response, '$.link.route_ordinal') "
        "FROM idempotency_records WHERE operation = 'worker.prompt-rendered@1'",
    ) == (1,)

    m.downgrade(url, "0008")
    assert _fetch_one(
        url,
        "SELECT call_ordinal, artifact_id FROM run_intermediate_artifact_links "
        "WHERE run_id = 'run:migration'",
    ) == (1, "artifact:prompt:migration")
    assert _fetch_one(
        url,
        "SELECT json_extract(response, '$.link.route_ordinal') "
        "FROM idempotency_records WHERE operation = 'worker.prompt-rendered@1'",
    ) == (None,)


def test_0009_preflights_corrupt_prompt_json_before_nontransactional_ddl(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'prompt-route-corrupt.db'}"
    m.upgrade(url, "0008")
    _insert_0008_prompt_link(url)
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE idempotency_records SET response = '{malformed' "
                    "WHERE operation = 'worker.prompt-rendered@1'"
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="malformed prompt idempotency JSON"):
        m.upgrade(url, "0009")
    assert _current_revision(url) == "0008"
    assert "resource_domain_scope" not in _column_names(url, "runs")
    assert "route_ordinal" not in _column_names(url, "run_intermediate_artifact_links")

    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE idempotency_records SET response = :response "
                    "WHERE operation = 'worker.prompt-rendered@1'"
                ),
                {"response": json.dumps({"artifact_id": "artifact:prompt:migration"})},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="malformed prompt idempotency response"):
        m.upgrade(url, "0009")
    assert _current_revision(url) == "0008"
    assert "resource_domain_scope" not in _column_names(url, "runs")
    assert "route_ordinal" not in _column_names(url, "run_intermediate_artifact_links")

    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE idempotency_records SET response = :response "
                    "WHERE operation = 'worker.prompt-rendered@1'"
                ),
                {
                    "response": json.dumps(
                        {
                            "artifact_id": "artifact:prompt:migration",
                            "link": {
                                "run_id": "run:migration",
                                "attempt_no": 1,
                                "call_ordinal": 1,
                                "artifact_id": "artifact:prompt:migration",
                            },
                        }
                    )
                },
            )
    finally:
        engine.dispose()

    m.upgrade(url, "0009")
    assert _current_revision(url) == "0009"


def test_0009_downgrade_refuses_to_collapse_fallback_prompt_route(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'prompt-route-downgrade.db'}"
    m.upgrade(url, "0008")
    _insert_0008_prompt_link(url)
    m.upgrade(url, "0009")
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE run_intermediate_artifact_links SET route_ordinal = 2 "
                    "WHERE run_id = 'run:migration'"
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="fallback prompt routes"):
        m.downgrade(url, "0008")
    assert _current_revision(url) == "0009"


def test_0009_downgrade_refuses_to_drop_resolved_run_domain_authority(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'run-domain-downgrade.db'}"
    m.upgrade(url, "0008")
    _insert_0008_prompt_link(url)
    m.upgrade(url, "0009")
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE runs SET resource_domain_scope = :scope WHERE run_id = 'run:migration'"
                ),
                {"scope": json.dumps({"domain_ids": ["aureus"]})},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="resource-domain authority"):
        m.downgrade(url, "0008")
    assert _current_revision(url) == "0009"


def test_0011_rejects_ambiguous_retained_command_ids_before_schema_change(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'command-id-preflight.db'}"
    m.upgrade(url, "0010")
    _insert_duplicate_run_command_ids(url)

    with pytest.raises(RuntimeError, match="duplicate Run command ids"):
        m.upgrade(url, "0011")

    assert _current_revision(url) == "0010"
    assert ("command_id",) not in _unique_keys(url, "run_commands")


def test_0011_preserves_retained_command_through_upgrade_and_downgrade(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'command-id-round-trip.db'}"
    m.upgrade(url, "0010")
    _insert_duplicate_run_command_ids(url)
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM run_commands WHERE run_id = 'run:duplicate-command:2'")
            )
    finally:
        engine.dispose()
    statement = (
        "SELECT run_id, command_id, client_id, client_seq, idempotency_key, "
        "request_hash, status, revision FROM run_commands"
    )
    expected = _fetch_one(url, statement)

    m.upgrade(url, "0011")
    assert _fetch_one(url, statement) == expected
    assert ("command_id",) in _unique_keys(url, "run_commands")

    m.downgrade(url, "0010")
    assert _fetch_one(url, statement) == expected
    assert ("command_id",) not in _unique_keys(url, "run_commands")


def test_0004_empty_conflict_store_upgrades_to_required_context_and_downgrades(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'empty-conflicts.db'}"
    m.upgrade(url, "0004")
    assert "context" not in _column_names(url, "conflict_sets")

    m.upgrade(url, "head")
    assert _current_revision(url) == "0014"
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


def _seed_overage_hold_then_downgrade_to_0011(url: str) -> str:
    m.upgrade(url, "head")
    engine = get_engine(url)
    selected_budget = budget("run", "run:migration-balance").model_copy(
        update={"limits": (amount("input_token", 200), amount("agent_step", 20))}
    )
    selected_set = budget_set("run:migration-balance", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, child_members = step_group(selected_set, parent, suffix="1", input_tokens=30)
    observed = usage(child, usage_id="usage:migration:overage", input_tokens=110)
    try:
        with uow(engine).begin() as transaction:
            transaction.cost.put_budget(selected_budget)
            transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
        with Session(engine) as session, session.begin():
            seed_current_attempt(session, selected_set=selected_set, parent=parent)
        with uow(engine).begin() as transaction:
            transaction.cost.reserve_many(child, child_members)
            transaction.cost.reconcile_group(observed)
    finally:
        engine.dispose()
    m.downgrade(url, "0011")
    return parent.reservation_group_id


def test_0012_backfills_capped_impact_and_replays_after_downgrade(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'hold-balance-backfill.db'}"
    hold_group_id = _seed_overage_hold_then_downgrade_to_0011(url)
    assert _current_revision(url) == "0011"
    assert "run_hold_balances" not in _table_names(url)

    m.upgrade(url, "head")
    assert _current_revision(url) == "0014"
    raw = _fetch_one(
        url,
        f"SELECT payload FROM run_hold_balances WHERE hold_group_id = '{hold_group_id}'",
    )[0]
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(payload, dict)
    assert payload["active_child_count"] == 0
    assert {item["dimension"]: item["value"] for item in payload["settled_impact"]} == {
        "agent_step": "1",
        "input_token": "30",
    }

    engine = get_engine(url)
    try:
        with Session(engine) as session:
            SqlCostLedger(session).audit_hold_balance(hold_group_id)
    finally:
        engine.dispose()

    m.downgrade(url, "0011")
    m.upgrade(url, "head")
    assert _current_revision(url) == "0014"


def test_0012_round_trip_preserves_reused_active_plus_settled_overage(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'hold-balance-reused-overage.db'}"
    m.upgrade(url, "head")
    engine = get_engine(url)
    selected_budget = budget("run", "run:migration-reuse").model_copy(
        update={"limits": (amount("input_token", 300), amount("agent_step", 30))}
    )
    selected_set = budget_set("run:migration-reuse", (selected_budget,))
    parent, parent_members = hold(selected_set)
    first, first_members = step_group(selected_set, parent, suffix="a1", input_tokens=30)
    active, active_members = step_group(selected_set, parent, suffix="b2", input_tokens=70)
    conservative = usage(first, usage_id="usage:migration:conservative", input_tokens=10)
    actual = usage(
        first,
        usage_id="usage:migration:actual",
        input_tokens=50,
        adjustment_of_usage_id=conservative.usage_id,
    )
    try:
        with uow(engine).begin() as transaction:
            transaction.cost.put_budget(selected_budget)
            transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
        with Session(engine) as session, session.begin():
            seed_current_attempt(session, selected_set=selected_set, parent=parent)
        with uow(engine).begin() as transaction:
            transaction.cost.reserve_many(first, first_members)
            transaction.cost.hold_unknown_group(first.reservation_group_id)
            transaction.cost.settle_unknown_group(first.reservation_group_id, conservative)
            transaction.cost.reserve_many(active, active_members)
            transaction.cost.late_reconcile_group(actual)
        with engine.connect() as connection:
            balance_before = connection.execute(
                text("SELECT payload FROM run_hold_balances WHERE hold_group_id = :hold_group_id"),
                {"hold_group_id": parent.reservation_group_id},
            ).scalar_one()
            budget_before = connection.execute(
                text("SELECT payload FROM budgets WHERE budget_id = :budget_id"),
                {"budget_id": selected_budget.budget_id},
            ).scalar_one()
    finally:
        engine.dispose()
    balance = json.loads(balance_before) if isinstance(balance_before, str) else balance_before
    assert balance["active_child_count"] == 1
    active_values = {item["dimension"]: int(item["value"]) for item in balance["active_allocated"]}
    settled_values = {item["dimension"]: int(item["value"]) for item in balance["settled_impact"]}
    assert active_values["input_token"] == 70
    assert settled_values["input_token"] == 30
    assert active_values["input_token"] + settled_values["input_token"] > 80

    m.downgrade(url, "0011")
    m.upgrade(url, "head")
    engine = get_engine(url)
    try:
        with engine.connect() as connection:
            balance_after = connection.execute(
                text("SELECT payload FROM run_hold_balances WHERE hold_group_id = :hold_group_id"),
                {"hold_group_id": parent.reservation_group_id},
            ).scalar_one()
            budget_after = connection.execute(
                text("SELECT payload FROM budgets WHERE budget_id = :budget_id"),
                {"budget_id": selected_budget.budget_id},
            ).scalar_one()
        assert balance_after == balance_before
        assert budget_after == budget_before
        with Session(engine) as session:
            SqlCostLedger(session).audit_hold_balance(parent.reservation_group_id)
    finally:
        engine.dispose()


def test_0012_round_trip_preserves_zero_allocation_active_child_count(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'hold-balance-zero-active.db'}"
    m.upgrade(url, "head")
    engine = get_engine(url)
    selected_budget = budget("run", "run:migration-zero")
    selected_set = budget_set("run:migration-zero", (selected_budget,))
    parent, parent_members = hold(selected_set)
    child, raw_members = step_group(selected_set, parent, suffix="0")
    child_members = tuple(
        member.model_copy(
            update={
                "reserved": tuple(
                    item.model_copy(update={"value": Decimal(0)}) for item in member.reserved
                )
            }
        )
        for member in raw_members
    )
    try:
        with uow(engine).begin() as transaction:
            transaction.cost.put_budget(selected_budget)
            transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
        with Session(engine) as session, session.begin():
            seed_current_attempt(session, selected_set=selected_set, parent=parent)
        with uow(engine).begin() as transaction:
            transaction.cost.reserve_many(child, child_members)
        with engine.connect() as connection:
            before = connection.execute(
                text("SELECT payload FROM run_hold_balances WHERE hold_group_id = :hold_group_id"),
                {"hold_group_id": parent.reservation_group_id},
            ).scalar_one()
    finally:
        engine.dispose()
    parsed = json.loads(before) if isinstance(before, str) else before
    assert parsed["active_child_count"] == 1
    assert all(item["value"] == "0" for item in parsed["active_allocated"])

    m.downgrade(url, "0011")
    m.upgrade(url, "head")
    engine = get_engine(url)
    try:
        with engine.connect() as connection:
            after = connection.execute(
                text("SELECT payload FROM run_hold_balances WHERE hold_group_id = :hold_group_id"),
                {"hold_group_id": parent.reservation_group_id},
            ).scalar_one()
        assert after == before
        with Session(engine) as session:
            SqlCostLedger(session).audit_hold_balance(parent.reservation_group_id)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    (
        "limit_value",
        "parent_value",
        "child_value",
        "conservative_value",
        "actual_value",
        "expected_actual_wire",
        "expected_budget_reserved_wire",
        "expected_budget_consumed_wire",
    ),
    (
        pytest.param(
            Decimal("100.00"),
            Decimal("80.00"),
            Decimal("30.00"),
            Decimal("0.10"),
            Decimal("0.2"),
            "0.2",
            "79.80",
            "0.20",
            id="fractional-trailing-zero-canonicalization",
        ),
        pytest.param(
            Decimal("700000000000000000000000.00000000"),
            Decimal("600000000000000000000000.00000000"),
            Decimal("500000000000000000000000.00000000"),
            Decimal("398309499403506836990859.91625105"),
            Decimal("398309499403506836990459.91621105"),
            "398309499403506836990459.91621105",
            "201690500596493163009540.08378895",
            "398309499403506836990459.91621105",
            id="beyond-default-decimal-context",
        ),
    ),
)
def test_0012_round_trip_preserves_fractional_late_reconcile_balance_digest(
    tmp_path,
    limit_value: Decimal,
    parent_value: Decimal,
    child_value: Decimal,
    conservative_value: Decimal,
    actual_value: Decimal,
    expected_actual_wire: str,
    expected_budget_reserved_wire: str,
    expected_budget_consumed_wire: str,
) -> None:
    url = f"sqlite:///{tmp_path / 'hold-balance-fractional-late.db'}"
    m.upgrade(url, "head")
    engine = get_engine(url)
    selected_budget = budget("run", "run:migration-fractional").model_copy(
        update={
            "limits": (amount("monetary", limit_value),),
            "reserved": (),
            "consumed": (),
        }
    )
    selected_set = budget_set("run:migration-fractional", (selected_budget,))
    parent, raw_parent_members = hold(selected_set)
    parent_members = tuple(
        member.model_copy(update={"reserved": (amount("monetary", parent_value),)})
        for member in raw_parent_members
    )
    child, raw_child_members = step_group(selected_set, parent, suffix="1")
    child_members = tuple(
        member.model_copy(update={"reserved": (amount("monetary", child_value),)})
        for member in raw_child_members
    )
    conservative = usage(
        child,
        usage_id="usage:migration:fractional-conservative",
        input_tokens=None,
    ).model_copy(
        update={
            "monetary": MonetaryObservationV1(
                status="reported",
                amount=conservative_value,
                currency="USD",
                price_book_version="price-book@1",
                quote_effective_at=selected_budget.created_at,
            )
        }
    )
    actual = usage(
        child,
        usage_id="usage:migration:fractional-actual",
        input_tokens=None,
        adjustment_of_usage_id=conservative.usage_id,
    ).model_copy(
        update={
            "monetary": MonetaryObservationV1(
                status="reported",
                amount=actual_value,
                currency="USD",
                price_book_version="price-book@1",
                quote_effective_at=selected_budget.created_at,
            ),
            "provider_prefix_cache": CacheHitObservationV1(status="reported", hit=True),
        }
    )
    try:
        with uow(engine).begin() as transaction:
            transaction.cost.put_budget(selected_budget)
            transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
        with Session(engine) as session, session.begin():
            seed_current_attempt(session, selected_set=selected_set, parent=parent)
        with uow(engine).begin() as transaction:
            transaction.cost.reserve_many(child, child_members)
            transaction.cost.hold_unknown_group(child.reservation_group_id)
            transaction.cost.settle_unknown_group(
                child.reservation_group_id,
                conservative,
            )
            transaction.cost.late_reconcile_group(actual)
        with engine.connect() as connection:
            before = connection.execute(
                text(
                    "SELECT payload, balance_digest FROM run_hold_balances "
                    "WHERE hold_group_id = :hold_group_id"
                ),
                {"hold_group_id": parent.reservation_group_id},
            ).one()
            before_budget = connection.execute(
                text("SELECT payload FROM budgets WHERE budget_id = :budget_id"),
                {"budget_id": selected_budget.budget_id},
            ).scalar_one()
    finally:
        engine.dispose()

    m.downgrade(url, "0011")
    m.upgrade(url, "head")
    engine = get_engine(url)
    try:
        with engine.connect() as connection:
            after = connection.execute(
                text(
                    "SELECT payload, balance_digest FROM run_hold_balances "
                    "WHERE hold_group_id = :hold_group_id"
                ),
                {"hold_group_id": parent.reservation_group_id},
            ).one()
            after_budget = connection.execute(
                text("SELECT payload FROM budgets WHERE budget_id = :budget_id"),
                {"budget_id": selected_budget.budget_id},
            ).scalar_one()
        assert after == before
        assert after_budget == before_budget
        payload = json.loads(after.payload) if isinstance(after.payload, str) else after.payload
        assert {item["dimension"]: item["value"] for item in payload["active_allocated"]} == {
            "monetary": "0"
        }
        assert {item["dimension"]: item["value"] for item in payload["settled_impact"]} == {
            "monetary": expected_actual_wire
        }
        budget_payload = json.loads(after_budget) if isinstance(after_budget, str) else after_budget
        assert {item["dimension"]: item["value"] for item in budget_payload["reserved"]} == {
            "monetary": expected_budget_reserved_wire
        }
        assert {item["dimension"]: item["value"] for item in budget_payload["consumed"]} == {
            "monetary": expected_budget_consumed_wire
        }
        with Session(engine) as session:
            SqlCostLedger(session).audit_hold_balance(parent.reservation_group_id)
    finally:
        engine.dispose()


def test_0012_exact_decimal_span_bound_is_frozen_and_fail_closed() -> None:
    migration = importlib.import_module(
        "gameforge.runtime.persistence.migrations.versions.0012_run_hold_balances"
    )

    assert migration._MAX_EXACT_COST_DECIMAL_DIGITS == 4096
    with pytest.raises(
        RuntimeError,
        match="0012 exact arithmetic exceeds its 4096-decimal-digit operational bound",
    ):
        migration._exact_decimal_add(Decimal(0), Decimal("1E+1000000000"))


def test_0012_replay_is_independent_of_high_precision_child_transition_order(
    tmp_path,
) -> None:
    def execute(
        database_name: str,
        transition_order: tuple[str, ...],
    ) -> tuple[tuple[object, ...], object]:
        url = f"sqlite:///{tmp_path / database_name}"
        m.upgrade(url, "head")
        engine = get_engine(url)
        selected_budget = budget("run", "run:migration-order").model_copy(
            update={
                "limits": (amount("monetary", Decimal("40000000000000000000000000000")),),
                "reserved": (),
                "consumed": (),
            }
        )
        selected_set = budget_set("run:migration-order", (selected_budget,))
        parent, raw_parent_members = hold(selected_set)
        parent_members = tuple(
            member.model_copy(
                update={"reserved": (amount("monetary", Decimal("30000000000000000000000000000")),)}
            )
            for member in raw_parent_members
        )
        observed_values = {
            "a": Decimal("10000000000000000000000000000"),
            "b": Decimal(6),
            "c": Decimal(6),
        }
        children = {}
        for suffix, observed_value in observed_values.items():
            child, raw_child_members = step_group(selected_set, parent, suffix=suffix)
            child_members = tuple(
                member.model_copy(update={"reserved": (amount("monetary", observed_value),)})
                for member in raw_child_members
            )
            observed_usage = usage(
                child,
                usage_id=f"usage:migration:order:{suffix}",
                input_tokens=None,
            ).model_copy(
                update={
                    "monetary": MonetaryObservationV1(
                        status="reported",
                        amount=observed_value,
                        currency="USD",
                        price_book_version="price-book@1",
                        quote_effective_at=selected_budget.created_at,
                    )
                }
            )
            children[suffix] = (child, child_members, observed_usage)
        try:
            with uow(engine).begin() as transaction:
                transaction.cost.put_budget(selected_budget)
                transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
            with Session(engine) as session, session.begin():
                seed_current_attempt(session, selected_set=selected_set, parent=parent)
            for suffix in transition_order:
                child, child_members, observed_usage = children[suffix]
                with uow(engine).begin() as transaction:
                    transaction.cost.reserve_many(child, child_members)
                    transaction.cost.reconcile_group(observed_usage)
            with engine.connect() as connection:
                before_balance = connection.execute(
                    text(
                        "SELECT payload, balance_digest FROM run_hold_balances "
                        "WHERE hold_group_id = :hold_group_id"
                    ),
                    {"hold_group_id": parent.reservation_group_id},
                ).one()
                before_budget = connection.execute(
                    text("SELECT payload FROM budgets WHERE budget_id = :budget_id"),
                    {"budget_id": selected_budget.budget_id},
                ).scalar_one()
        finally:
            engine.dispose()

        m.downgrade(url, "0011")
        m.upgrade(url, "head")
        engine = get_engine(url)
        try:
            with engine.connect() as connection:
                after_balance = connection.execute(
                    text(
                        "SELECT payload, balance_digest FROM run_hold_balances "
                        "WHERE hold_group_id = :hold_group_id"
                    ),
                    {"hold_group_id": parent.reservation_group_id},
                ).one()
                after_budget = connection.execute(
                    text("SELECT payload FROM budgets WHERE budget_id = :budget_id"),
                    {"budget_id": selected_budget.budget_id},
                ).scalar_one()
            assert after_balance == before_balance
            assert after_budget == before_budget
            return tuple(after_balance), after_budget
        finally:
            engine.dispose()

    large_first = execute("hold-balance-large-first.db", ("a", "b", "c"))
    small_first = execute("hold-balance-small-first.db", ("b", "c", "a"))

    assert small_first == large_first


def test_0012_rejects_active_balance_above_parent_even_if_budget_matches(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'hold-balance-active-over-parent.db'}"
    m.upgrade(url, "head")
    engine = get_engine(url)
    selected_budget = budget("run", "run:migration-active")
    selected_set = budget_set("run:migration-active", (selected_budget,))
    parent, parent_members = hold(selected_set)
    children = (
        step_group(selected_set, parent, suffix="1", input_tokens=30),
        step_group(selected_set, parent, suffix="2", input_tokens=30),
    )
    try:
        with uow(engine).begin() as transaction:
            transaction.cost.put_budget(selected_budget)
            transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
        with Session(engine) as session, session.begin():
            seed_current_attempt(session, selected_set=selected_set, parent=parent)
        with uow(engine).begin() as transaction:
            for child, child_members in children:
                transaction.cost.reserve_many(child, child_members)
    finally:
        engine.dispose()
    m.downgrade(url, "0011")

    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            for child, _child_members in children:
                member_raw = connection.execute(
                    text(
                        "SELECT payload FROM budget_reservations "
                        "WHERE reservation_group_id = :group_id"
                    ),
                    {"group_id": child.reservation_group_id},
                ).scalar_one()
                member_payload = (
                    json.loads(member_raw) if isinstance(member_raw, str) else dict(member_raw)
                )
                for item in member_payload["reserved"]:
                    if item["dimension"] == "input_token":
                        item["value"] = "50"
                connection.execute(
                    text(
                        "UPDATE budget_reservations SET payload = :payload "
                        "WHERE reservation_group_id = :group_id"
                    ),
                    {
                        "group_id": child.reservation_group_id,
                        "payload": json.dumps(member_payload, separators=(",", ":")),
                    },
                )
            budget_raw = connection.execute(
                text("SELECT payload FROM budgets WHERE budget_id = :budget_id"),
                {"budget_id": selected_budget.budget_id},
            ).scalar_one()
            budget_payload = (
                json.loads(budget_raw) if isinstance(budget_raw, str) else dict(budget_raw)
            )
            for item in budget_payload["reserved"]:
                if item["dimension"] == "input_token":
                    item["value"] = "100"
            connection.execute(
                text("UPDATE budgets SET payload = :payload WHERE budget_id = :budget_id"),
                {
                    "budget_id": selected_budget.budget_id,
                    "payload": json.dumps(budget_payload, separators=(",", ":")),
                },
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="active Run hold balance exceeds parent"):
        m.upgrade(url, "head")
    assert _current_revision(url) == "0011"
    assert "run_hold_balances" not in _table_names(url)


def test_0012_rejects_parent_hold_above_budget_limit_even_if_reserve_matches(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'hold-above-budget-limit.db'}"
    hold_group_id = _seed_overage_hold_then_downgrade_to_0011(url)
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            member_raw = connection.execute(
                text(
                    "SELECT payload FROM budget_reservations WHERE reservation_group_id = :group_id"
                ),
                {"group_id": hold_group_id},
            ).scalar_one()
            member_payload = (
                json.loads(member_raw) if isinstance(member_raw, str) else dict(member_raw)
            )
            for item in member_payload["reserved"]:
                if item["dimension"] == "input_token":
                    item["value"] = "201"
            connection.execute(
                text(
                    "UPDATE budget_reservations SET payload = :payload "
                    "WHERE reservation_group_id = :group_id"
                ),
                {
                    "group_id": hold_group_id,
                    "payload": json.dumps(member_payload, separators=(",", ":")),
                },
            )
            budget_raw = connection.execute(
                text("SELECT payload FROM budgets WHERE budget_id = 'budget:run'")
            ).scalar_one()
            budget_payload = (
                json.loads(budget_raw) if isinstance(budget_raw, str) else dict(budget_raw)
            )
            for item in budget_payload["reserved"]:
                if item["dimension"] == "input_token":
                    item["value"] = "171"
            connection.execute(
                text("UPDATE budgets SET payload = :payload WHERE budget_id = 'budget:run'"),
                {"payload": json.dumps(budget_payload, separators=(",", ":"))},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="Run hold amount exceeds budget authority"):
        m.upgrade(url, "head")
    assert _current_revision(url) == "0011"
    assert "run_hold_balances" not in _table_names(url)


def test_0012_rejects_child_allocation_above_parent_even_after_settlement(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'child-above-parent-hold.db'}"
    _seed_overage_hold_then_downgrade_to_0011(url)
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            member_raw = connection.execute(
                text(
                    "SELECT payload FROM budget_reservations "
                    "WHERE reservation_group_id = 'step:run:migration-balance:1'"
                )
            ).scalar_one()
            member_payload = (
                json.loads(member_raw) if isinstance(member_raw, str) else dict(member_raw)
            )
            for item in member_payload["reserved"]:
                if item["dimension"] == "input_token":
                    item["value"] = "81"
            connection.execute(
                text(
                    "UPDATE budget_reservations SET payload = :payload "
                    "WHERE reservation_group_id = 'step:run:migration-balance:1'"
                ),
                {"payload": json.dumps(member_payload, separators=(",", ":"))},
            )
            budget_raw = connection.execute(
                text("SELECT payload FROM budgets WHERE budget_id = 'budget:run'")
            ).scalar_one()
            budget_payload = (
                json.loads(budget_raw) if isinstance(budget_raw, str) else dict(budget_raw)
            )
            budget_payload["reserved"] = [
                item for item in budget_payload["reserved"] if item["dimension"] != "input_token"
            ]
            connection.execute(
                text("UPDATE budgets SET payload = :payload WHERE budget_id = 'budget:run'"),
                {"payload": json.dumps(budget_payload, separators=(",", ":"))},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="child allocation exceeds parent authority"):
        m.upgrade(url, "head")
    assert _current_revision(url) == "0011"
    assert "run_hold_balances" not in _table_names(url)


def test_0012_rejects_monetary_usage_currency_differing_from_hold(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'usage-currency-mismatch.db'}"
    m.upgrade(url, "head")
    engine = get_engine(url)
    selected_budget = budget("run", "run:migration-money").model_copy(
        update={
            "limits": (amount("monetary", 100),),
            "reserved": (),
            "consumed": (),
        }
    )
    selected_set = budget_set("run:migration-money", (selected_budget,))
    parent, raw_parent_members = hold(selected_set)
    parent_members = tuple(
        member.model_copy(update={"reserved": (amount("monetary", 80),)})
        for member in raw_parent_members
    )
    child, raw_child_members = step_group(selected_set, parent, suffix="1")
    child_members = tuple(
        member.model_copy(update={"reserved": (amount("monetary", 30),)})
        for member in raw_child_members
    )
    observed = usage(child, usage_id="usage:migration:money", input_tokens=None).model_copy(
        update={
            "monetary": MonetaryObservationV1(
                status="reported",
                amount=Decimal(10),
                currency="USD",
                price_book_version="price-book@1",
                quote_effective_at=selected_budget.created_at,
            )
        }
    )
    try:
        with uow(engine).begin() as transaction:
            transaction.cost.put_budget(selected_budget)
            transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
        with Session(engine) as session, session.begin():
            seed_current_attempt(session, selected_set=selected_set, parent=parent)
        with uow(engine).begin() as transaction:
            transaction.cost.reserve_many(child, child_members)
            transaction.cost.reconcile_group(observed)
    finally:
        engine.dispose()
    m.downgrade(url, "0011")

    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            raw = connection.execute(
                text("SELECT payload FROM usage_entries WHERE usage_id = :usage_id"),
                {"usage_id": observed.usage_id},
            ).scalar_one()
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
            payload["monetary"]["currency"] = "EUR"
            connection.execute(
                text("UPDATE usage_entries SET payload = :payload WHERE usage_id = :usage_id"),
                {
                    "usage_id": observed.usage_id,
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )
    finally:
        engine.dispose()

    with pytest.raises(
        RuntimeError,
        match="settled usage amount identity differs from reservation",
    ):
        m.upgrade(url, "head")
    assert _current_revision(url) == "0011"
    assert "run_hold_balances" not in _table_names(url)


def test_0012_rejects_partial_hold_that_omits_a_budget_set_scope(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'partial-hold-scope.db'}"
    m.upgrade(url, "head")
    engine = get_engine(url)
    budgets = (
        budget("run", "run:migration-partial"),
        budget("principal", "principal:migration-partial"),
    )
    selected_set = budget_set("run:migration-partial", budgets)
    parent, parent_members = hold(selected_set)
    try:
        with uow(engine).begin() as transaction:
            for selected_budget in budgets:
                transaction.cost.put_budget(selected_budget)
            transaction.cost.freeze_budget_set(selected_set, parent, parent_members)
    finally:
        engine.dispose()
    m.downgrade(url, "0011")

    omitted = next(item for item in parent_members if item.budget_id == "budget:principal")
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM budget_reservations WHERE reservation_id = :reservation_id"),
                {"reservation_id": omitted.reservation_id},
            )
            raw_group = connection.execute(
                text(
                    "SELECT payload FROM reservation_groups WHERE reservation_group_id = :group_id"
                ),
                {"group_id": parent.reservation_group_id},
            ).scalar_one()
            group_payload = json.loads(raw_group) if isinstance(raw_group, str) else dict(raw_group)
            group_payload["budget_reservation_ids"] = [
                value
                for value in group_payload["budget_reservation_ids"]
                if value != omitted.reservation_id
            ]
            connection.execute(
                text(
                    "UPDATE reservation_groups SET payload = :payload "
                    "WHERE reservation_group_id = :group_id"
                ),
                {
                    "group_id": parent.reservation_group_id,
                    "payload": json.dumps(group_payload, separators=(",", ":")),
                },
            )
            raw_budget = connection.execute(
                text("SELECT payload FROM budgets WHERE budget_id = 'budget:principal'")
            ).scalar_one()
            budget_payload = (
                json.loads(raw_budget) if isinstance(raw_budget, str) else dict(raw_budget)
            )
            budget_payload["reserved"] = []
            connection.execute(
                text("UPDATE budgets SET payload = :payload WHERE budget_id = 'budget:principal'"),
                {"payload": json.dumps(budget_payload, separators=(",", ":"))},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="members differ from budget-set authority"):
        m.upgrade(url, "head")
    assert _current_revision(url) == "0011"
    assert "run_hold_balances" not in _table_names(url)


def test_0012_fails_when_budget_reserve_disagrees_with_reconstructed_holds(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'hold-balance-corrupt-reserve.db'}"
    _seed_overage_hold_then_downgrade_to_0011(url)
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            raw = connection.execute(
                text("SELECT payload FROM budgets WHERE budget_id = 'budget:run'")
            ).scalar_one()
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
            for item in payload["reserved"]:
                if item["dimension"] == "input_token":
                    item["value"] = "49"
            connection.execute(
                text("UPDATE budgets SET payload = :payload WHERE budget_id = 'budget:run'"),
                {"payload": json.dumps(payload, separators=(",", ":"))},
            )
    finally:
        engine.dispose()

    with pytest.raises(
        RuntimeError,
        match=(
            "budget reserve differs from reconstructed open Run holds; "
            "pre-0012 transition ordering cannot recover context-rounded authority losslessly"
        ),
    ):
        m.upgrade(url, "head")
    assert _current_revision(url) == "0011"
    assert "run_hold_balances" not in _table_names(url)

    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            raw = connection.execute(
                text("SELECT payload FROM budgets WHERE budget_id = 'budget:run'")
            ).scalar_one()
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
            for item in payload["reserved"]:
                if item["dimension"] == "input_token":
                    item["value"] = "50"
            connection.execute(
                text("UPDATE budgets SET payload = :payload WHERE budget_id = 'budget:run'"),
                {"payload": json.dumps(payload, separators=(",", ":"))},
            )
    finally:
        engine.dispose()
    m.upgrade(url, "head")
    assert _current_revision(url) == "0014"


def test_0012_rejects_noncanonical_group_source_before_ddl_and_can_retry(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'hold-balance-corrupt-group.db'}"
    hold_group_id = _seed_overage_hold_then_downgrade_to_0011(url)
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT revision, payload FROM reservation_groups "
                    "WHERE reservation_group_id = :group_id"
                ),
                {"group_id": hold_group_id},
            ).one()
            payload = json.loads(row.payload) if isinstance(row.payload, str) else dict(row.payload)
            payload["revision"] = 999
            connection.execute(
                text(
                    "UPDATE reservation_groups SET payload = :payload "
                    "WHERE reservation_group_id = :group_id"
                ),
                {
                    "group_id": hold_group_id,
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="reservation group payload/projection mismatch"):
        m.upgrade(url, "head")
    assert _current_revision(url) == "0011"
    assert "run_hold_balances" not in _table_names(url)

    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            raw = connection.execute(
                text(
                    "SELECT payload FROM reservation_groups WHERE reservation_group_id = :group_id"
                ),
                {"group_id": hold_group_id},
            ).scalar_one()
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
            payload["revision"] = int(row.revision)
            connection.execute(
                text(
                    "UPDATE reservation_groups SET payload = :payload "
                    "WHERE reservation_group_id = :group_id"
                ),
                {
                    "group_id": hold_group_id,
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )
    finally:
        engine.dispose()
    m.upgrade(url, "head")
    assert _current_revision(url) == "0014"


def test_0012_rejects_budget_snapshot_member_drift_before_ddl_and_can_retry(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'hold-balance-corrupt-budget-snapshot.db'}"
    _seed_overage_hold_then_downgrade_to_0011(url)
    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            row = connection.execute(
                text("SELECT snapshot_id, scope_id, payload FROM budget_snapshots LIMIT 1")
            ).one()
            payload = json.loads(row.payload) if isinstance(row.payload, str) else dict(row.payload)
            payload["scope_id"] = "run:tampered"
            connection.execute(
                text(
                    "UPDATE budget_snapshots SET scope_id = :scope_id, payload = :payload "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {
                    "snapshot_id": row.snapshot_id,
                    "scope_id": "run:tampered",
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="budget snapshot members differ"):
        m.upgrade(url, "head")
    assert _current_revision(url) == "0011"
    assert "run_hold_balances" not in _table_names(url)

    engine = get_engine(url)
    try:
        with engine.begin() as connection:
            raw = connection.execute(
                text("SELECT payload FROM budget_snapshots WHERE snapshot_id = :snapshot_id"),
                {"snapshot_id": row.snapshot_id},
            ).scalar_one()
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
            payload["scope_id"] = row.scope_id
            connection.execute(
                text(
                    "UPDATE budget_snapshots SET scope_id = :scope_id, payload = :payload "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {
                    "snapshot_id": row.snapshot_id,
                    "scope_id": row.scope_id,
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )
    finally:
        engine.dispose()
    m.upgrade(url, "head")
    assert _current_revision(url) == "0014"


def test_0012_rejects_orphan_budget_reservation_before_ddl_and_can_retry(tmp_path) -> None:
    database_path = tmp_path / "hold-balance-orphan-reservation.db"
    url = f"sqlite:///{database_path}"
    _seed_overage_hold_then_downgrade_to_0011(url)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT budget_id, status, revision, payload FROM budget_reservations LIMIT 1"
        ).fetchone()
        assert row is not None
        budget_id, status, revision, raw = row
        payload = json.loads(raw)
        payload["reservation_id"] = "reservation:orphan"
        payload["reservation_group_id"] = "group:missing"
        connection.execute(
            """
            INSERT INTO budget_reservations (
                reservation_id, reservation_group_id, budget_id, status, revision, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["reservation_id"],
                payload["reservation_group_id"],
                budget_id,
                status,
                revision,
                json.dumps(payload, separators=(",", ":")),
            ),
        )

    with pytest.raises(RuntimeError, match="budget reservation without its group"):
        m.upgrade(url, "head")
    assert _current_revision(url) == "0011"
    assert "run_hold_balances" not in _table_names(url)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM budget_reservations WHERE reservation_id = 'reservation:orphan'"
        )
    m.upgrade(url, "head")
    assert _current_revision(url) == "0014"


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
