"""enforce globally unique durable Run command identity

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "run_commands"
_CONSTRAINT = "uq_run_command_id"


def _preflight_global_identity() -> None:
    """Fail before SQLite's table rebuild if retained command identity is ambiguous."""

    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT command_id
            FROM run_commands
            GROUP BY command_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
            )
        )
        .first()
    )
    if duplicate is not None:
        raise RuntimeError("cannot migrate duplicate Run command ids")


def upgrade() -> None:
    _preflight_global_identity()
    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        batch.create_unique_constraint(_CONSTRAINT, ["command_id"])


def downgrade() -> None:
    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        batch.drop_constraint(_CONSTRAINT, type_="unique")
