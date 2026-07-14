"""persistent cost ledger and model routing history

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("budget_id", sa.String(), primary_key=True),
        sa.Column("scope_kind", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("deadline_utc", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_budgets_scope_status",
        "budgets",
        ["scope_kind", "scope_id", "status", "budget_id"],
        unique=False,
    )
    op.create_index(
        "ix_budgets_deadline",
        "budgets",
        ["status", "deadline_utc", "budget_id"],
        unique=False,
    )

    op.create_table(
        "budget_set_snapshots",
        sa.Column("budget_set_snapshot_id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("selection_policy_version", sa.String(), nullable=False),
        sa.Column("captured_at", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("run_id", name="uq_budget_set_run"),
    )
    op.create_index(
        "ix_budget_sets_captured",
        "budget_set_snapshots",
        ["captured_at", "budget_set_snapshot_id"],
        unique=False,
    )

    op.create_table(
        "budget_snapshots",
        sa.Column("snapshot_id", sa.String(), primary_key=True),
        sa.Column("budget_set_snapshot_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("budget_id", sa.String(), nullable=False),
        sa.Column("scope_kind", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("budget_revision_at_freeze", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["budget_set_snapshot_id"],
            ["budget_set_snapshots.budget_set_snapshot_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["budget_id"], ["budgets.budget_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "budget_set_snapshot_id",
            "ordinal",
            name="uq_budget_snapshot_ordinal",
        ),
        sa.UniqueConstraint(
            "budget_set_snapshot_id",
            "budget_id",
            name="uq_budget_snapshot_budget",
        ),
    )

    op.create_table(
        "model_catalog_snapshots",
        sa.Column("catalog_version", sa.Integer(), primary_key=True),
        sa.Column("catalog_digest", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "catalog_version",
            "catalog_digest",
            name="uq_model_catalog_exact_ref",
        ),
        sa.UniqueConstraint("catalog_digest", name="uq_model_catalog_digest"),
    )
    op.create_index(
        "ix_model_catalog_created",
        "model_catalog_snapshots",
        ["created_at", "catalog_version"],
        unique=False,
    )

    op.create_table(
        "routing_policies",
        sa.Column("policy_version", sa.Integer(), primary_key=True),
        sa.Column("routing_policy_digest", sa.String(), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False),
        sa.Column("catalog_digest", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_version", "catalog_digest"],
            [
                "model_catalog_snapshots.catalog_version",
                "model_catalog_snapshots.catalog_digest",
            ],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "policy_version",
            "routing_policy_digest",
            name="uq_routing_policy_exact_ref",
        ),
        sa.UniqueConstraint(
            "routing_policy_digest",
            name="uq_routing_policy_digest",
        ),
    )

    op.create_table(
        "reservation_groups",
        sa.Column("reservation_group_id", sa.String(), primary_key=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("budget_set_snapshot_id", sa.String(), nullable=False),
        sa.Column("parent_hold_group_id", sa.String(), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=True),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("transport_attempt", sa.Integer(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["budget_set_snapshot_id"],
            ["budget_set_snapshots.budget_set_snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_hold_group_id"],
            ["reservation_groups.reservation_group_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "run_id",
            "scope",
            "idempotency_key",
            name="uq_reservation_group_idempotency",
        ),
    )
    op.create_index(
        "ix_reservation_groups_run_attempt",
        "reservation_groups",
        ["run_id", "attempt_no", "created_at", "reservation_group_id"],
        unique=False,
    )
    op.create_index(
        "ix_reservation_groups_status_expiry",
        "reservation_groups",
        ["status", "expires_at", "reservation_group_id"],
        unique=False,
    )

    op.create_table(
        "budget_reservations",
        sa.Column("reservation_id", sa.String(), primary_key=True),
        sa.Column("reservation_group_id", sa.String(), nullable=False),
        sa.Column("budget_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["reservation_group_id"],
            ["reservation_groups.reservation_group_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["budget_id"], ["budgets.budget_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "reservation_group_id",
            "budget_id",
            name="uq_budget_reservation_member",
        ),
    )
    op.create_index(
        "ix_budget_reservations_budget_status",
        "budget_reservations",
        ["budget_id", "status", "reservation_id"],
        unique=False,
    )

    op.create_table(
        "routing_decisions",
        sa.Column("decision_id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("model_snapshot", sa.String(), nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("budget_set_snapshot_id", sa.String(), nullable=False),
        sa.Column("fallback_index", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("routing_policy_digest", sa.String(), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False),
        sa.Column("catalog_digest", sa.String(), nullable=False),
        sa.Column("execution_source", sa.String(), nullable=False),
        sa.Column("decided_at", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["budget_set_snapshot_id"],
            ["budget_set_snapshots.budget_set_snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_version", "routing_policy_digest"],
            ["routing_policies.policy_version", "routing_policies.routing_policy_digest"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version", "catalog_digest"],
            [
                "model_catalog_snapshots.catalog_version",
                "model_catalog_snapshots.catalog_digest",
            ],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_routing_decisions_run",
        "routing_decisions",
        ["run_id", "attempt_no", "decided_at", "decision_id"],
        unique=False,
    )

    op.create_table(
        "legacy_import_routing_decisions",
        sa.Column("decision_id", sa.String(), primary_key=True),
        sa.Column("source_wire_sha256", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("model_snapshot", sa.String(), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False),
        sa.Column("catalog_digest", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_version", "catalog_digest"],
            [
                "model_catalog_snapshots.catalog_version",
                "model_catalog_snapshots.catalog_digest",
            ],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_legacy_route_model",
        "legacy_import_routing_decisions",
        ["model_snapshot", "decision_id"],
        unique=False,
    )

    op.create_table(
        "usage_entries",
        sa.Column("usage_id", sa.String(), primary_key=True),
        sa.Column("usage_identity", sa.String(), nullable=False),
        sa.Column("reservation_group_id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("transport_attempt", sa.Integer(), nullable=True),
        sa.Column("execution_source", sa.String(), nullable=False),
        sa.Column("retry_index", sa.Integer(), nullable=False),
        sa.Column("routing_decision_kind", sa.String(), nullable=True),
        sa.Column("routing_decision_id", sa.String(), nullable=True),
        sa.Column("native_routing_decision_id", sa.String(), nullable=True),
        sa.Column("legacy_routing_decision_id", sa.String(), nullable=True),
        sa.Column("adjustment_of_usage_id", sa.String(), nullable=True),
        sa.Column("fencing_token_at_reserve", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["reservation_group_id"],
            ["reservation_groups.reservation_group_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["native_routing_decision_id"],
            ["routing_decisions.decision_id"],
            name="fk_usage_native_routing_decision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["legacy_routing_decision_id"],
            ["legacy_import_routing_decisions.decision_id"],
            name="fk_usage_legacy_routing_decision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adjustment_of_usage_id"],
            ["usage_entries.usage_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("usage_identity", name="uq_usage_identity"),
    )
    op.create_index(
        "ix_usage_run_attempt",
        "usage_entries",
        ["run_id", "attempt_no", "recorded_at", "usage_id"],
        unique=False,
    )
    op.create_index(
        "ix_usage_reservation_group",
        "usage_entries",
        ["reservation_group_id", "recorded_at", "usage_id"],
        unique=False,
    )

    op.create_table(
        "permit_groups",
        sa.Column("permit_group_id", sa.String(), primary_key=True),
        sa.Column("budget_set_snapshot_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["budget_set_snapshot_id"],
            ["budget_set_snapshots.budget_set_snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "run_id",
            "lease_id",
            "fencing_token",
            name="uq_permit_group_lease",
        ),
    )
    op.create_index(
        "ix_permit_groups_status_expiry",
        "permit_groups",
        ["status", "expires_at", "permit_group_id"],
        unique=False,
    )

    op.create_table(
        "concurrency_permits",
        sa.Column("permit_id", sa.String(), primary_key=True),
        sa.Column("permit_group_id", sa.String(), nullable=False),
        sa.Column("budget_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["permit_group_id"],
            ["permit_groups.permit_group_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["budget_id"], ["budgets.budget_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "permit_group_id",
            "budget_id",
            name="uq_concurrency_permit_member",
        ),
    )
    op.create_index(
        "ix_concurrency_permits_budget_status",
        "concurrency_permits",
        ["budget_id", "status", "expires_at", "permit_id"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    retained_tables = (
        "usage_entries",
        "concurrency_permits",
        "permit_groups",
        "routing_decisions",
        "legacy_import_routing_decisions",
        "budget_reservations",
        "reservation_groups",
        "routing_policies",
        "model_catalog_snapshots",
        "budget_snapshots",
        "budget_set_snapshots",
        "budgets",
    )
    if any(
        connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None
        for table_name in retained_tables
    ):
        raise RuntimeError(
            "cannot remove authoritative cost/routing schema while retained rows exist"
        )
    op.drop_table("concurrency_permits")
    op.drop_table("permit_groups")
    op.drop_table("usage_entries")
    op.drop_table("legacy_import_routing_decisions")
    op.drop_table("routing_decisions")
    op.drop_table("budget_reservations")
    op.drop_table("reservation_groups")
    op.drop_table("routing_policies")
    op.drop_table("model_catalog_snapshots")
    op.drop_table("budget_snapshots")
    op.drop_table("budget_set_snapshots")
    op.drop_table("budgets")
