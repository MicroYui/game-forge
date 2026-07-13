"""add immutable conflict-set publication context and content digests

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    legacy_set_row = connection.execute(
        sa.text("SELECT 1 FROM conflict_sets LIMIT 1")
    ).first()
    legacy_conflict_row = connection.execute(
        sa.text("SELECT 1 FROM merge_conflicts LIMIT 1")
    ).first()
    if legacy_set_row is not None or legacy_conflict_row is not None:
        raise RuntimeError(
            "cannot add required conflict_sets.context while legacy rows exist; "
            "legacy conflict rows exist and authoritative publication context "
            "cannot be reconstructed"
        )

    op.add_column(
        "conflict_sets",
        sa.Column("context", sa.JSON(), nullable=False),
    )
    op.add_column(
        "conflict_sets",
        sa.Column("content_digest", sa.String(), nullable=False),
    )
    op.add_column(
        "merge_conflicts",
        sa.Column("content_digest", sa.String(), nullable=False),
    )


def downgrade() -> None:
    connection = op.get_bind()
    retained_set_row = connection.execute(
        sa.text("SELECT 1 FROM conflict_sets LIMIT 1")
    ).first()
    retained_conflict_row = connection.execute(
        sa.text("SELECT 1 FROM merge_conflicts LIMIT 1")
    ).first()
    if retained_set_row is not None or retained_conflict_row is not None:
        raise RuntimeError(
            "cannot remove immutable conflict-set authority while retained "
            "conflict rows exist"
        )

    op.drop_column("merge_conflicts", "content_digest")
    op.drop_column("conflict_sets", "content_digest")
    op.drop_column("conflict_sets", "context")
