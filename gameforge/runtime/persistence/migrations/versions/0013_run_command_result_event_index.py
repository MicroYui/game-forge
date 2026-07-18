"""index Run command result-event retention lookups

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_run_commands_result_event",
        "run_commands",
        ["run_id", "result_event_seq"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_run_commands_result_event", table_name="run_commands")
