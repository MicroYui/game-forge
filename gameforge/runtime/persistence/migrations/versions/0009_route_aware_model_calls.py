"""route-aware prompt, model-route, and response-consumption authority

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-16
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PROMPT_TABLE = "run_intermediate_artifact_links"
_OLD_PROMPT_TABLE = "_0009_run_intermediate_artifact_links"


def _create_route_aware_prompt_table() -> None:
    op.create_table(
        _PROMPT_TABLE,
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("call_ordinal", sa.Integer(), nullable=False),
        sa.Column("route_ordinal", sa.Integer(), nullable=False),
        sa.Column("link_schema_version", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "route_ordinal >= 1",
            name="ck_run_intermediate_route_ordinal_positive",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "attempt_no",
            "call_ordinal",
            "route_ordinal",
        ),
        sa.UniqueConstraint(
            "run_id",
            "attempt_no",
            "call_ordinal",
            "route_ordinal",
            "artifact_id",
            name="uq_run_intermediate_route_artifact",
        ),
    )


def _create_legacy_prompt_table() -> None:
    op.create_table(
        _PROMPT_TABLE,
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("call_ordinal", sa.Integer(), nullable=False),
        sa.Column("link_schema_version", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "attempt_no", "call_ordinal"),
    )


def _preflight_prompt_replay_json() -> None:
    """Reject corrupt retained JSON before SQLite's non-transactional DDL starts."""

    connection = op.get_bind()
    invalid_json = connection.execute(
        sa.text(
            """
            SELECT scope, key FROM idempotency_records
            WHERE operation = 'worker.prompt-rendered@1'
              AND response IS NOT NULL
              AND json_valid(response) != 1
            LIMIT 1
            """
        )
    ).first()
    if invalid_json is not None:
        raise RuntimeError("cannot migrate malformed prompt idempotency JSON")
    invalid_shape = connection.execute(
        sa.text(
            """
            SELECT scope, key FROM idempotency_records
            WHERE operation = 'worker.prompt-rendered@1'
              AND response IS NOT NULL
              AND (
                  json_type(response) IS NOT 'object'
                  OR json_type(response, '$.link') IS NOT 'object'
              )
            LIMIT 1
            """
        )
    ).first()
    if invalid_shape is not None:
        raise RuntimeError("cannot migrate malformed prompt idempotency response")


def upgrade() -> None:
    _preflight_prompt_replay_json()
    op.add_column(
        "runs",
        sa.Column("resource_domain_scope", sa.JSON(), nullable=True),
    )

    op.rename_table(_PROMPT_TABLE, _OLD_PROMPT_TABLE)
    _create_route_aware_prompt_table()
    op.execute(
        sa.text(
            f"""
            INSERT INTO {_PROMPT_TABLE} (
                run_id, attempt_no, call_ordinal, route_ordinal,
                link_schema_version, artifact_id, role, request_hash,
                fencing_token, published_at
            )
            SELECT
                run_id, attempt_no, call_ordinal, 1,
                link_schema_version, artifact_id, role, request_hash,
                fencing_token, published_at
            FROM {_OLD_PROMPT_TABLE}
            """
        )
    )
    op.drop_table(_OLD_PROMPT_TABLE)
    op.execute(
        sa.text(
            """
            UPDATE idempotency_records
            SET response = json_set(response, '$.link.route_ordinal', 1)
            WHERE operation = 'worker.prompt-rendered@1'
              AND response IS NOT NULL
              AND json_type(response, '$.link') = 'object'
              AND json_extract(response, '$.link.route_ordinal') IS NULL
            """
        )
    )

    op.create_table(
        "run_model_route_links",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("call_ordinal", sa.Integer(), nullable=False),
        sa.Column("route_ordinal", sa.Integer(), nullable=False),
        sa.Column("link_schema_version", sa.String(), nullable=False),
        sa.Column("prompt_artifact_id", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("routing_decision_kind", sa.String(), nullable=False),
        sa.Column("routing_decision_id", sa.String(), nullable=False),
        sa.Column("native_routing_decision_id", sa.String(), nullable=True),
        sa.Column("legacy_routing_decision_id", sa.String(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "route_ordinal >= 1",
            name="ck_run_model_route_ordinal_positive",
        ),
        sa.CheckConstraint(
            "(routing_decision_kind = 'native' "
            "AND native_routing_decision_id = routing_decision_id "
            "AND legacy_routing_decision_id IS NULL) OR "
            "(routing_decision_kind = 'legacy_import' "
            "AND legacy_routing_decision_id = routing_decision_id "
            "AND native_routing_decision_id IS NULL)",
            name="ck_run_model_route_decision_authority",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            [
                "run_id",
                "attempt_no",
                "call_ordinal",
                "route_ordinal",
                "prompt_artifact_id",
            ],
            [
                "run_intermediate_artifact_links.run_id",
                "run_intermediate_artifact_links.attempt_no",
                "run_intermediate_artifact_links.call_ordinal",
                "run_intermediate_artifact_links.route_ordinal",
                "run_intermediate_artifact_links.artifact_id",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["native_routing_decision_id"],
            ["routing_decisions.decision_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["legacy_routing_decision_id"],
            ["legacy_import_routing_decisions.decision_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "attempt_no", "call_ordinal", "route_ordinal"),
        sa.UniqueConstraint(
            "native_routing_decision_id",
            name="uq_run_model_route_native_decision",
        ),
    )
    op.create_index(
        "ix_run_model_route_links_run",
        "run_model_route_links",
        ["run_id", "attempt_no", "call_ordinal", "route_ordinal"],
        unique=False,
    )

    op.create_table(
        "run_model_response_consumptions",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("call_ordinal", sa.Integer(), nullable=False),
        sa.Column("route_ordinal", sa.Integer(), nullable=False),
        sa.Column("consumption_schema_version", sa.String(), nullable=False),
        sa.Column("execution_source", sa.String(), nullable=False),
        sa.Column("reservation_group_id", sa.String(), nullable=False),
        sa.Column("transport_attempt", sa.Integer(), nullable=True),
        sa.Column("cassette_shard_artifact_id", sa.String(), nullable=True),
        sa.Column("consumed_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "route_ordinal >= 1",
            name="ck_run_model_consumption_route_ordinal_positive",
        ),
        sa.CheckConstraint(
            "(execution_source = 'online' AND transport_attempt IS NOT NULL "
            "AND transport_attempt >= 1) OR "
            "(execution_source IN ('full_response_cache', 'cassette_replay') "
            "AND transport_attempt IS NULL)",
            name="ck_run_model_consumption_execution_shape",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "attempt_no", "call_ordinal", "route_ordinal"],
            [
                "run_model_route_links.run_id",
                "run_model_route_links.attempt_no",
                "run_model_route_links.call_ordinal",
                "run_model_route_links.route_ordinal",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_group_id"],
            ["reservation_groups.reservation_group_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cassette_shard_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "attempt_no", "call_ordinal", "route_ordinal"),
        sa.UniqueConstraint(
            "reservation_group_id",
            name="uq_run_model_consumption_reservation_group",
        ),
        sa.UniqueConstraint(
            "run_id",
            "attempt_no",
            "call_ordinal",
            name="uq_run_model_consumption_logical_call",
        ),
        sa.UniqueConstraint(
            "cassette_shard_artifact_id",
            name="uq_run_model_consumption_cassette_shard",
        ),
    )
    op.create_index(
        "ix_run_model_response_consumptions_run",
        "run_model_response_consumptions",
        ["run_id", "attempt_no", "call_ordinal", "route_ordinal"],
        unique=False,
    )


def downgrade() -> None:
    _preflight_prompt_replay_json()
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT 1 FROM runs WHERE resource_domain_scope IS NOT NULL LIMIT 1")
    ).first():
        raise RuntimeError("cannot remove retained Run resource-domain authority")
    if connection.execute(sa.text("SELECT 1 FROM run_model_route_links LIMIT 1")).first():
        raise RuntimeError("cannot remove retained model-route authority")
    if connection.execute(
        sa.text("SELECT 1 FROM run_intermediate_artifact_links WHERE route_ordinal != 1 LIMIT 1")
    ).first():
        raise RuntimeError("cannot collapse retained fallback prompt routes")
    if connection.execute(
        sa.text(
            """
            SELECT 1 FROM idempotency_records
            WHERE operation = 'worker.prompt-rendered@1'
              AND response IS NOT NULL
              AND json_extract(response, '$.link.route_ordinal') != 1
            LIMIT 1
            """
        )
    ).first():
        raise RuntimeError("cannot collapse retained fallback prompt replay authority")

    op.drop_table("run_model_response_consumptions")
    op.drop_table("run_model_route_links")

    op.drop_column("runs", "resource_domain_scope")
    op.rename_table(_PROMPT_TABLE, _OLD_PROMPT_TABLE)
    _create_legacy_prompt_table()
    op.execute(
        sa.text(
            f"""
            INSERT INTO {_PROMPT_TABLE} (
                run_id, attempt_no, call_ordinal, link_schema_version,
                artifact_id, role, request_hash, fencing_token, published_at
            )
            SELECT
                run_id, attempt_no, call_ordinal, link_schema_version,
                artifact_id, role, request_hash, fencing_token, published_at
            FROM {_OLD_PROMPT_TABLE}
            """
        )
    )
    op.drop_table(_OLD_PROMPT_TABLE)
    op.execute(
        sa.text(
            """
            UPDATE idempotency_records
            SET response = json_remove(response, '$.link.route_ordinal')
            WHERE operation = 'worker.prompt-rendered@1'
              AND response IS NOT NULL
              AND json_type(response, '$.link') = 'object'
            """
        )
    )
