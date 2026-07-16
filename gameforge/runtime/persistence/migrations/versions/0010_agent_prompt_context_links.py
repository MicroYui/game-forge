"""typed Agent prompt-context intermediate authority

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-16
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "run_model_response_consumptions",
        sa.Column("response_digest", sa.String(), nullable=True),
    )
    op.create_table(
        "run_tool_intermediate_links",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("target_call_ordinal", sa.Integer(), nullable=False),
        sa.Column("link_schema_version", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("agent_node_id", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "target_call_ordinal >= 1",
            name="ck_run_tool_intermediate_target_call_positive",
        ),
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
        sa.PrimaryKeyConstraint("run_id", "attempt_no", "target_call_ordinal"),
        sa.UniqueConstraint(
            "run_id",
            "attempt_no",
            "artifact_id",
            name="uq_run_tool_intermediate_artifact",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    retained_link = connection.execute(
        sa.text("SELECT 1 FROM run_tool_intermediate_links LIMIT 1")
    ).first()
    retained_digest = connection.execute(
        sa.text(
            "SELECT 1 FROM run_model_response_consumptions "
            "WHERE response_digest IS NOT NULL LIMIT 1"
        )
    ).first()
    if retained_link is not None or retained_digest is not None:
        raise RuntimeError(
            "cannot remove authoritative Agent prompt-context/response-digest evidence"
        )
    op.drop_table("run_tool_intermediate_links")
    op.drop_column("run_model_response_consumptions", "response_digest")
