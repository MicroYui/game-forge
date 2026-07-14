"""local password, API-key, and session authority

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "password_credentials",
        sa.Column("credential_id", sa.String(), primary_key=True),
        sa.Column("principal_id", sa.String(), nullable=False),
        sa.Column("normalized_login_name", sa.String(), nullable=False),
        sa.Column("normalization_policy_version", sa.String(), nullable=False),
        sa.Column("normalization_policy_digest", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("hash_policy_version", sa.String(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("changed_at", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.principal_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "normalized_login_name",
            name="uq_password_credentials_normalized_login",
        ),
    )
    op.create_index(
        "ix_password_credentials_principal_status",
        "password_credentials",
        ["principal_id", "status", "credential_id"],
        unique=False,
    )

    op.create_table(
        "api_keys",
        sa.Column("api_key_id", sa.String(), primary_key=True),
        sa.Column("principal_id", sa.String(), nullable=False),
        sa.Column("key_prefix", sa.String(), nullable=False),
        sa.Column("key_digest", sa.String(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.principal_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("key_digest", name="uq_api_keys_digest"),
    )
    op.create_index(
        "ix_api_keys_principal_status",
        "api_keys",
        ["principal_id", "status", "api_key_id"],
        unique=False,
    )

    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("principal_id", sa.String(), nullable=False),
        sa.Column("source_credential_id", sa.String(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("token_digest", sa.String(), nullable=False),
        sa.Column("csrf_secret_digest", sa.String(), nullable=False),
        sa.Column("signing_key_id", sa.String(), nullable=False),
        sa.Column("issued_at", sa.String(), nullable=False),
        sa.Column("absolute_expires_at", sa.String(), nullable=False),
        sa.Column("idle_expires_at", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True),
        sa.Column("revoke_reason", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.principal_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_digest", name="uq_sessions_token_digest"),
    )
    op.create_index(
        "ix_sessions_principal_expiry",
        "sessions",
        ["principal_id", "absolute_expires_at", "session_id"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_source_credential",
        "sessions",
        ["source_credential_id", "credential_version", "session_id"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    retained_tables = ("sessions", "api_keys", "password_credentials")
    if any(
        connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None
        for table_name in retained_tables
    ):
        raise RuntimeError("cannot remove authoritative auth schema while retained rows exist")
    op.drop_table("sessions")
    op.drop_table("api_keys")
    op.drop_table("password_credentials")
