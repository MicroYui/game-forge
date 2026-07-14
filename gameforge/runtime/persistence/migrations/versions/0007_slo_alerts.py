"""persistent SLO definitions, evaluations, and alert state

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workload_profiles",
        sa.Column("profile_id", sa.String(), primary_key=True),
        sa.Column("dataset_artifact_id", sa.String(), nullable=False),
        sa.Column("entity_count", sa.Integer(), nullable=False),
        sa.Column("relation_count", sa.Integer(), nullable=False),
        sa.Column("constraint_count", sa.Integer(), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=True),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("environment_fingerprint", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )

    op.create_table(
        "slo_definitions",
        sa.Column("slo_id", sa.String(), primary_key=True),
        sa.Column("workload_profile_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("effective_from", sa.String(), nullable=False),
        sa.Column("rolling_window_s", sa.Integer(), nullable=False),
        sa.Column("evaluation_interval_s", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workload_profile_id"],
            ["workload_profiles.profile_id"],
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "alert_rules",
        sa.Column("alert_rule_id", sa.String(), primary_key=True),
        sa.Column("slo_id", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["slo_id"],
            ["slo_definitions.slo_id"],
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "slo_evaluations",
        sa.Column("evaluation_id", sa.String(), primary_key=True),
        sa.Column("slo_id", sa.String(), nullable=False),
        sa.Column("window_start", sa.String(), nullable=False),
        sa.Column("window_end", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["slo_id"],
            ["slo_definitions.slo_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_slo_evaluations_order",
        "slo_evaluations",
        ["slo_id", "window_start", "window_end", "evaluation_id"],
        unique=False,
    )

    op.create_table(
        "alert_instances",
        sa.Column("alert_instance_id", sa.String(), primary_key=True),
        sa.Column("alert_rule_id", sa.String(), nullable=False),
        sa.Column("dedup_key", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("last_evaluation_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["alert_rule_id"],
            ["alert_rules.alert_rule_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_evaluation_id"],
            ["slo_evaluations.evaluation_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "alert_rule_id",
            "dedup_key",
            name="uq_alert_instance_rule_dedup",
        ),
    )
    op.create_index(
        "ix_alert_instances_state",
        "alert_instances",
        ["state", "alert_rule_id", "alert_instance_id"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    retained_tables = (
        "alert_instances",
        "slo_evaluations",
        "alert_rules",
        "slo_definitions",
        "workload_profiles",
    )
    if any(
        connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None
        for table_name in retained_tables
    ):
        raise RuntimeError("cannot remove authoritative SLO/alert schema while retained rows exist")
    op.drop_table("alert_instances")
    op.drop_table("slo_evaluations")
    op.drop_table("alert_rules")
    op.drop_table("slo_definitions")
    op.drop_table("workload_profiles")
