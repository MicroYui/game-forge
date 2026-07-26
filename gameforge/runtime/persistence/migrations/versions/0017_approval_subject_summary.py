"""record what an approval subject does, in its author's own words

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-26

An approval queue titled by subject kind and revision reads the same on every
row. The subject's own rationale/reason is immutable, so it can be recorded on
the ApprovalItem at draft time and never go stale. Approvals retained from
before this column carry none.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("approval_items", sa.Column("subject_summary", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("approval_items", "subject_summary")
