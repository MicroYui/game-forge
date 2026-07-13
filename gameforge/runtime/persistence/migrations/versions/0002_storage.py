"""storage and object-binding foundation

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("object_ref", sa.JSON(), nullable=True))
    op.add_column(
        "refs",
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.execute(
        """
        UPDATE refs
        SET revision = COALESCE(
            (SELECT MAX(ref_history.seq) FROM ref_history WHERE ref_history.name = refs.name),
            1
        )
        """
    )
    op.create_index(
        "uq_ref_history_name_seq",
        "ref_history",
        ["name", "seq"],
        unique=True,
    )

    op.add_column("audit", sa.Column("chain_id", sa.String(), nullable=True))
    op.add_column("audit", sa.Column("chain_seq", sa.Integer(), nullable=True))
    op.add_column("audit", sa.Column("actor_v2", sa.JSON(), nullable=True))
    op.add_column("audit", sa.Column("initiated_by", sa.JSON(), nullable=True))
    op.add_column("audit", sa.Column("subject", sa.JSON(), nullable=True))
    op.add_column("audit", sa.Column("correlation", sa.JSON(), nullable=True))
    op.create_index(
        "uq_audit_chain_seq",
        "audit",
        ["chain_id", "chain_seq"],
        unique=True,
    )

    op.create_table(
        "object_bindings",
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("store_id", sa.String(), nullable=False),
        sa.Column("binding_schema_version", sa.String(), nullable=False),
        sa.Column("object_ref_schema_version", sa.String(), nullable=False),
        sa.Column("location_schema_version", sa.String(), nullable=False),
        sa.Column("object_sha256", sa.String(), nullable=False),
        sa.Column("object_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("backend_generation", sa.String(), nullable=False),
        sa.Column("etag", sa.String(), nullable=True),
        sa.Column("storage_class", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("verified_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("object_key", "store_id"),
    )
    op.create_index(
        "ix_object_bindings_gc_order",
        "object_bindings",
        ["status", "verified_at", "object_key", "store_id"],
        unique=False,
    )

    op.create_table(
        "read_snapshots",
        sa.Column("snapshot_id", sa.String(), primary_key=True),
        sa.Column("snapshot_schema_version", sa.String(), nullable=False),
        sa.Column("resource_kind", sa.String(), nullable=False),
        sa.Column("query_hash", sa.String(), nullable=False),
        sa.Column("authz_fingerprint", sa.String(), nullable=False),
        sa.Column("stable_sort_schema_id", sa.String(), nullable=False),
        sa.Column("strategy", sa.String(), nullable=False),
        sa.Column("high_watermark", sa.BigInteger(), nullable=True),
        sa.Column("materialized_item_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_read_snapshots_expiry",
        "read_snapshots",
        ["expires_at", "snapshot_id"],
        unique=False,
    )
    op.create_table(
        "materialized_read_items",
        sa.Column("snapshot_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("observed_revision", sa.Integer(), nullable=False),
        sa.Column("view_schema_id", sa.String(), nullable=False),
        sa.Column("canonical_view", sa.JSON(), nullable=False),
        sa.Column("view_hash", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["read_snapshots.snapshot_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "ordinal"),
        sa.UniqueConstraint("snapshot_id", "resource_id", name="uq_snapshot_resource"),
    )
    op.create_table(
        "audit_heads",
        sa.Column("chain_id", sa.String(), primary_key=True),
        sa.Column("head_seq", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("audit_heads")
    op.drop_table("materialized_read_items")
    op.drop_index("ix_read_snapshots_expiry", table_name="read_snapshots")
    op.drop_table("read_snapshots")
    op.drop_index("ix_object_bindings_gc_order", table_name="object_bindings")
    op.drop_table("object_bindings")

    op.drop_index("uq_audit_chain_seq", table_name="audit")
    op.drop_column("audit", "correlation")
    op.drop_column("audit", "subject")
    op.drop_column("audit", "initiated_by")
    op.drop_column("audit", "actor_v2")
    op.drop_column("audit", "chain_seq")
    op.drop_column("audit", "chain_id")

    op.drop_index("uq_ref_history_name_seq", table_name="ref_history")
    op.drop_column("refs", "revision")
    op.drop_column("artifacts", "object_ref")
