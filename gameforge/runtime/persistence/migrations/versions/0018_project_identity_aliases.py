"""record the names a project has decided refer to one entity

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-27

Lexical normalization reaches `air.quality` ≡ `air_quality` on its own; it can
never reach 岩王帝君 ≡ 钟离, which share no characters. A person has to say those
are one thing. This table records that they said it, so every later extraction
resolves the name deterministically with no model in the decision path.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0018"
down_revision: Union[str, Sequence[str], None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_identity_aliases",
        sa.Column("alias_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("canonical_alias", sa.String(), nullable=False),
        sa.Column("canonical_entity_id", sa.String(), nullable=False),
        sa.Column("declared_by", sa.String(), nullable=False),
        sa.Column("declared_at", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("alias_id"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["game_projects.project_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "project_id",
            "canonical_alias",
            name="uq_project_identity_alias_canonical",
        ),
    )
    op.create_index(
        "ix_project_identity_aliases_project_status",
        "project_identity_aliases",
        ["project_id", "status", "alias_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_identity_aliases_project_status",
        table_name="project_identity_aliases",
    )
    op.drop_table("project_identity_aliases")
