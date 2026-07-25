from __future__ import annotations

import json
import sqlite3

from sqlalchemy import inspect, text

from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine


def test_0015_project_authoring_upgrade_and_downgrade(tmp_path) -> None:
    database = tmp_path / "projects.db"
    url = f"sqlite+pysqlite:///{database}"

    migrations_api.upgrade(url, "0015")
    engine = get_engine(url)
    try:
        inspector = inspect(engine)
        assert {"game_projects", "project_materials", "project_extractions"} <= set(
            inspector.get_table_names()
        )
        assert {column["name"] for column in inspector.get_columns("game_projects")} >= {
            "project_id",
            "project_key",
            "status",
            "revision",
            "payload",
        }
        project_indexes = {index["name"] for index in inspector.get_indexes("game_projects")}
        assert "ix_game_projects_status_updated" in project_indexes
    finally:
        engine.dispose()

    migrations_api.downgrade(url, "0014")
    engine = get_engine(url)
    try:
        assert not {
            "game_projects",
            "project_materials",
            "project_extractions",
        }.intersection(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_0016_adds_fail_closed_proposal_publication_bindings_to_existing_rows(tmp_path) -> None:
    database = tmp_path / "project-publications.db"
    url = f"sqlite+pysqlite:///{database}"
    migrations_api.upgrade(url, "0015")
    payload = {
        "extraction_schema_version": "project-extraction@1",
        "extraction_id": "extraction:existing",
        "project_id": "project:existing",
        "planning_scope": "auto",
        "material_ids": [],
        "source_artifact_ids": [],
        "base_snapshot_artifact_id": "artifact:base",
        "run_id": "run:existing",
        "status": "queued",
        "patch_artifact_id": None,
        "preview_snapshot_artifact_id": None,
        "approval_id": None,
        "failure_cause_code": None,
        "failure_message": None,
        "failure_retryable": None,
        "normalization_summary": None,
        "alias_groups": [],
        "identity_conflicts": [],
        "validation_issues": [],
        "disposition": None,
        "discarded_by": None,
        "discarded_at": None,
        "discard_reason": None,
        "created_by": "human:maker",
        "created_at": "2026-07-24T00:00:00Z",
        "updated_at": "2026-07-24T00:00:00Z",
        "revision": 1,
    }
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO artifacts (
                artifact_id, lineage_schema_version, kind, version_tuple, lineage,
                payload_hash, created_at, meta, object_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "artifact:base",
                "lineage@2",
                "ir_snapshot",
                "{}",
                "[]",
                "0" * 64,
                "2026-07-24T00:00:00Z",
                "{}",
                None,
            ),
        )
        connection.execute(
            """
            INSERT INTO game_projects (
                project_id, project_key, display_name, status, domain_scope,
                bootstrap_snapshot_artifact_id, content_ref_name, constraint_ref_name,
                latest_extraction_id, latest_patch_artifact_id, latest_approval_id,
                created_by, created_at, updated_at, revision, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "project:existing",
                "existing",
                "Existing",
                "draft",
                json.dumps({"domain_ids": ["builtin"]}),
                "artifact:base",
                "projects/project:existing/content/head",
                "projects/project:existing/constraints/head",
                None,
                None,
                None,
                "human:maker",
                "2026-07-24T00:00:00Z",
                "2026-07-24T00:00:00Z",
                1,
                "{}",
            ),
        )
        connection.execute(
            """
            INSERT INTO runs (
                run_id, run_schema_version, kind, kind_version, status, revision,
                idempotency_scope, idempotency_key, request_hash, payload, payload_hash,
                run_kind_definition_digest, outcome_policy_set_digest,
                migration_capability_matrix, failure_classifier, dispatch_trace_carrier,
                initiated_by, resource_domain_scope, queue_deadline_utc,
                attempt_timeout_ns, overall_deadline_utc, cancel_requested_at,
                cancel_requested_by, current_attempt_no, next_attempt_no,
                next_fencing_token, next_event_seq, budget_set_snapshot_id,
                run_budget_hold_group_id, concurrency_permit_group_id, retry_policy,
                max_attempts, retry_not_before_utc, result_artifact_id,
                failure_artifact_id, terminal_cassette_artifact_id, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "run:existing",
                "run@1",
                "generation.propose",
                1,
                "queued",
                1,
                "principal:human:maker",
                "existing",
                "1" * 64,
                "{}",
                "2" * 64,
                "3" * 64,
                "4" * 64,
                None,
                "{}",
                None,
                "{}",
                json.dumps({"domain_ids": ["builtin"]}),
                "2026-07-24T00:10:00Z",
                1_000_000_000,
                "2026-07-24T01:00:00Z",
                None,
                None,
                None,
                1,
                1,
                1,
                "budget:existing",
                "hold:existing",
                None,
                "{}",
                1,
                None,
                None,
                None,
                None,
                "2026-07-24T00:00:00Z",
                "2026-07-24T00:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO project_extractions (
                extraction_id, project_id, run_id, status, patch_artifact_id,
                preview_snapshot_artifact_id, approval_id, created_by, created_at,
                updated_at, revision, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "extraction:existing",
                "project:existing",
                "run:existing",
                "queued",
                None,
                None,
                None,
                "human:maker",
                "2026-07-24T00:00:00Z",
                "2026-07-24T00:00:00Z",
                1,
                json.dumps(payload),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    migrations_api.upgrade(url, "0016")
    engine = get_engine(url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("project_extractions")}
        assert {
            "publication_patch_artifact_id",
            "publication_approval_id",
        } <= columns
        with engine.connect() as sql:
            migrated = sql.execute(
                text(
                    "SELECT publication_patch_artifact_id, publication_approval_id, payload "
                    "FROM project_extractions WHERE extraction_id = 'extraction:existing'"
                )
            ).one()
        assert migrated[0] is None
        assert migrated[1] is None
        migrated_payload = json.loads(migrated[2]) if isinstance(migrated[2], str) else migrated[2]
        assert migrated_payload["publication_patch_artifact_id"] is None
        assert migrated_payload["publication_approval_id"] is None
    finally:
        engine.dispose()

    migrations_api.downgrade(url, "0015")
    engine = get_engine(url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("project_extractions")}
        assert "publication_patch_artifact_id" not in columns
        with engine.connect() as sql:
            retained = sql.execute(
                text(
                    "SELECT payload FROM project_extractions "
                    "WHERE extraction_id = 'extraction:existing'"
                )
            ).scalar_one()
        retained_payload = json.loads(retained) if isinstance(retained, str) else retained
        assert "publication_patch_artifact_id" not in retained_payload
        assert "publication_approval_id" not in retained_payload
    finally:
        engine.dispose()
