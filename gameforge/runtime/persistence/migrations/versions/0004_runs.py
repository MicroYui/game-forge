"""persistent run, lease, event, command, and link foundation

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("run_schema_version", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("kind_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_scope", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("run_kind_definition_digest", sa.String(), nullable=False),
        sa.Column("outcome_policy_set_digest", sa.String(), nullable=False),
        sa.Column("migration_capability_matrix", sa.JSON(), nullable=True),
        sa.Column("failure_classifier", sa.JSON(), nullable=False),
        sa.Column("dispatch_trace_carrier", sa.JSON(), nullable=True),
        sa.Column("initiated_by", sa.JSON(), nullable=False),
        sa.Column("queue_deadline_utc", sa.String(), nullable=False),
        sa.Column("attempt_timeout_ns", sa.BigInteger(), nullable=False),
        sa.Column("overall_deadline_utc", sa.String(), nullable=False),
        sa.Column("cancel_requested_at", sa.String(), nullable=True),
        sa.Column("cancel_requested_by", sa.JSON(), nullable=True),
        sa.Column("current_attempt_no", sa.Integer(), nullable=True),
        sa.Column("next_attempt_no", sa.Integer(), nullable=False),
        sa.Column("next_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("next_event_seq", sa.BigInteger(), nullable=False),
        sa.Column("budget_set_snapshot_id", sa.String(), nullable=False),
        sa.Column("run_budget_hold_group_id", sa.String(), nullable=False),
        sa.Column("concurrency_permit_group_id", sa.String(), nullable=True),
        sa.Column("retry_policy", sa.JSON(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("retry_not_before_utc", sa.String(), nullable=True),
        sa.Column("result_artifact_id", sa.String(), nullable=True),
        sa.Column("failure_artifact_id", sa.String(), nullable=True),
        sa.Column("terminal_cassette_artifact_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["result_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["failure_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["terminal_cassette_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "idempotency_scope",
            "idempotency_key",
            name="uq_runs_idempotency",
        ),
    )
    op.create_index(
        "ix_runs_claim_order",
        "runs",
        ["status", "retry_not_before_utc", "created_at", "run_id"],
        unique=False,
    )
    op.create_index(
        "ix_runs_deadlines",
        "runs",
        ["status", "queue_deadline_utc", "overall_deadline_utc"],
        unique=False,
    )
    op.create_table(
        "run_attempts",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("worker_principal_id", sa.String(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=True),
        sa.Column("next_call_ordinal", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("attempt_deadline_utc", sa.String(), nullable=True),
        sa.Column("ended_at", sa.String(), nullable=True),
        sa.Column("failure_class", sa.String(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("failure_artifact_id", sa.String(), nullable=True),
        sa.Column("cassette_bundle_artifact_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["failure_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cassette_bundle_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "attempt_no"),
        sa.UniqueConstraint("run_id", "fencing_token", name="uq_run_attempt_fencing"),
    )
    op.create_index(
        "ix_run_attempts_status",
        "run_attempts",
        ["run_id", "status", "attempt_no"],
        unique=False,
    )
    op.create_table(
        "run_leases",
        sa.Column("lease_id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_version", sa.Integer(), nullable=False),
        sa.Column("owner_principal_id", sa.String(), nullable=False),
        sa.Column("acquired_at", sa.String(), nullable=False),
        sa.Column("heartbeat_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("released_at", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "attempt_no", name="uq_run_lease_attempt"),
    )
    op.create_index(
        "uq_run_active_lease",
        "run_leases",
        ["run_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_run_leases_expiry",
        "run_leases",
        ["status", "expires_at", "run_id"],
        unique=False,
    )
    op.create_table(
        "run_events",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_schema_version", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.Column("data_schema_version", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq"),
    )
    op.create_table(
        "run_commands",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("command_id", sa.String(), nullable=False),
        sa.Column("record_schema_version", sa.String(), nullable=False),
        sa.Column("command_schema_version", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_seq", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("expected_run_revision", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload_schema_id", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("actor", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("claimed_at", sa.String(), nullable=True),
        sa.Column("claimed_attempt_no", sa.Integer(), nullable=True),
        sa.Column("claimed_fencing_token", sa.BigInteger(), nullable=True),
        sa.Column("applied_at", sa.String(), nullable=True),
        sa.Column("result_event_seq", sa.BigInteger(), nullable=True),
        sa.Column("rejection_code", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["run_id", "result_event_seq"],
            ["run_events.run_id", "run_events.seq"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "command_id"),
        sa.UniqueConstraint(
            "run_id",
            "client_id",
            "client_seq",
            name="uq_run_command_client",
        ),
        sa.UniqueConstraint(
            "run_id",
            "idempotency_key",
            name="uq_run_command_idempotency",
        ),
    )
    op.create_index(
        "ix_run_commands_pending",
        "run_commands",
        ["run_id", "status", "created_at", "command_id"],
        unique=False,
    )
    op.create_table(
        "run_intermediate_artifact_links",
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
    op.create_table(
        "run_finding_links",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("link_schema_version", sa.String(), nullable=False),
        sa.Column("finding_id", sa.String(), nullable=False),
        sa.Column("finding_revision", sa.Integer(), nullable=False),
        sa.Column("finding_digest", sa.String(), nullable=False),
        sa.Column("evidence_artifact_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id", "finding_revision"],
            ["finding_revisions.finding_id", "finding_revisions.revision"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "attempt_no", "ordinal"),
        sa.UniqueConstraint(
            "run_id",
            "finding_id",
            "finding_revision",
            name="uq_run_finding_revision",
        ),
    )


def downgrade() -> None:
    op.drop_table("run_finding_links")
    op.drop_table("run_intermediate_artifact_links")
    op.drop_table("run_commands")
    op.drop_table("run_events")
    op.drop_table("run_leases")
    op.drop_table("run_attempts")
    op.drop_table("runs")
