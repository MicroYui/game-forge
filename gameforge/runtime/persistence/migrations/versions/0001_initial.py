"""initial: artifacts, refs, ref_history, audit (contract §5, §12A.3)

Revision ID: 0001
Revises:
Create Date: 2026-07-06

Creates the four version/lineage/audit tables from
`gameforge.runtime.persistence.models` explicitly (rather than via
autogenerate) for a deterministic, reviewable migration. `downgrade()` drops
them in reverse order.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(), primary_key=True),
        sa.Column("lineage_schema_version", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("version_tuple", sa.JSON(), nullable=False),
        sa.Column("lineage", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
    )
    op.create_table(
        "refs",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=True),
    )
    op.create_table(
        "ref_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
    )
    op.create_table(
        "audit",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("audit_schema_version", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=True),
        sa.Column("ts", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("prev_hash", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("audit")
    op.drop_table("ref_history")
    op.drop_table("refs")
    op.drop_table("artifacts")
