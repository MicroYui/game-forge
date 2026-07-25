"""add project-first authoring resources

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "game_projects",
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("project_key", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("domain_scope", sa.JSON(), nullable=False),
        sa.Column("bootstrap_snapshot_artifact_id", sa.String(), nullable=False),
        sa.Column("content_ref_name", sa.String(), nullable=False),
        sa.Column("constraint_ref_name", sa.String(), nullable=False),
        sa.Column("latest_extraction_id", sa.String(), nullable=True),
        sa.Column("latest_patch_artifact_id", sa.String(), nullable=True),
        sa.Column("latest_approval_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bootstrap_snapshot_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["latest_patch_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["latest_approval_id"],
            ["approval_items.approval_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("project_id"),
        sa.UniqueConstraint("project_key", name="uq_game_projects_key"),
    )
    op.create_index(
        "ix_game_projects_status_updated",
        "game_projects",
        ["status", "updated_at", "project_id"],
        unique=False,
    )

    op.create_table(
        "project_materials",
        sa.Column("material_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("source_format", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("original_source_artifact_id", sa.String(), nullable=False),
        sa.Column("rendered_source_artifact_id", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["game_projects.project_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["original_source_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rendered_source_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("material_id"),
    )
    op.create_index(
        "ix_project_materials_project_status_created",
        "project_materials",
        ["project_id", "status", "created_at", "material_id"],
        unique=False,
    )

    op.create_table(
        "project_extractions",
        sa.Column("extraction_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("patch_artifact_id", sa.String(), nullable=True),
        sa.Column("preview_snapshot_artifact_id", sa.String(), nullable=True),
        sa.Column("approval_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["game_projects.project_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["patch_artifact_id"], ["artifacts.artifact_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["preview_snapshot_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"], ["approval_items.approval_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("extraction_id"),
    )
    op.create_index(
        "ix_project_extractions_project_created",
        "project_extractions",
        ["project_id", "created_at", "extraction_id"],
        unique=False,
    )
    op.create_index(
        "ix_project_extractions_run",
        "project_extractions",
        ["run_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_project_extractions_run", table_name="project_extractions")
    op.drop_index("ix_project_extractions_project_created", table_name="project_extractions")
    op.drop_table("project_extractions")
    op.drop_index("ix_project_materials_project_status_created", table_name="project_materials")
    op.drop_table("project_materials")
    op.drop_index("ix_game_projects_status_updated", table_name="game_projects")
    op.drop_table("game_projects")
