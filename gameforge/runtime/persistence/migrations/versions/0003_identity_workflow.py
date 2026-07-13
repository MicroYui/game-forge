"""identity and workflow foundation

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column("principal_id", sa.String(), primary_key=True),
        sa.Column("principal_schema_version", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("credential_epoch", sa.Integer(), nullable=False),
        sa.Column("authz_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("disabled_at", sa.String(), nullable=True),
        sa.Column("disabled_reason", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_principals_status_id",
        "principals",
        ["status", "principal_id"],
        unique=False,
    )
    op.create_table(
        "role_assignments",
        sa.Column("assignment_id", sa.String(), primary_key=True),
        sa.Column("assignment_schema_version", sa.String(), nullable=False),
        sa.Column("principal_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("granted_at", sa.String(), nullable=False),
        sa.Column("granted_by", sa.JSON(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True),
        sa.Column("revoked_by", sa.JSON(), nullable=True),
        sa.Column("revoke_reason", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.principal_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_role_assignments_active_identity",
        "role_assignments",
        ["principal_id", "role", "scope_key"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_role_assignments_principal_status",
        "role_assignments",
        ["principal_id", "status"],
        unique=False,
    )
    op.create_table(
        "policy_snapshots",
        sa.Column("document_kind", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("document_version", sa.String(), nullable=False),
        sa.Column("document_digest", sa.String(), nullable=False),
        sa.Column("payload_schema_version", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("document_kind", "document_id", "document_version"),
    )
    op.create_index(
        "ix_policy_snapshots_digest",
        "policy_snapshots",
        ["document_kind", "document_id", "document_digest"],
        unique=False,
    )
    op.create_table(
        "approval_items",
        sa.Column("approval_id", sa.String(), primary_key=True),
        sa.Column("approval_schema_version", sa.String(), nullable=False),
        sa.Column("subject_series_id", sa.String(), nullable=False),
        sa.Column("subject_revision", sa.Integer(), nullable=False),
        sa.Column("subject_kind", sa.String(), nullable=False),
        sa.Column("subject_artifact_id", sa.String(), nullable=False),
        sa.Column("subject_digest", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_approval_id", sa.String(), nullable=True),
        sa.Column("proposer", sa.JSON(), nullable=False),
        sa.Column("domain_scope", sa.JSON(), nullable=False),
        sa.Column("domain_registry_ref", sa.JSON(), nullable=False),
        sa.Column("route_policy", sa.JSON(), nullable=False),
        sa.Column("role_policy_version", sa.String(), nullable=False),
        sa.Column("role_policy_digest", sa.String(), nullable=False),
        sa.Column("approval_policy", sa.JSON(), nullable=False),
        sa.Column("requirements", sa.JSON(), nullable=False),
        sa.Column("active_validation_run_id", sa.String(), nullable=True),
        sa.Column("last_validation_failure_artifact_id", sa.String(), nullable=True),
        sa.Column("evidence_set_artifact_id", sa.String(), nullable=True),
        sa.Column("regression_evidence_artifact_ids", sa.JSON(), nullable=False),
        sa.Column("target_binding", sa.JSON(), nullable=True),
        sa.Column("auto_apply_proof", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.String(), nullable=True),
        sa.Column("decided_at", sa.String(), nullable=True),
        sa.Column("applied_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["subject_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_approval_id"],
            ["approval_items.approval_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_validation_failure_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_set_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "subject_series_id",
            "subject_revision",
            name="uq_approval_subject_revision",
        ),
    )
    op.create_index(
        "ix_approval_queue",
        "approval_items",
        ["status", "created_at", "approval_id"],
        unique=False,
    )
    op.create_index(
        "ix_approval_subject_artifact",
        "approval_items",
        ["subject_artifact_id"],
        unique=False,
    )
    op.create_index(
        "uq_approval_active_validation_run",
        "approval_items",
        ["active_validation_run_id"],
        unique=True,
        sqlite_where=sa.text("active_validation_run_id IS NOT NULL"),
    )
    op.create_table(
        "approval_decisions",
        sa.Column("decision_id", sa.String(), primary_key=True),
        sa.Column("approval_id", sa.String(), nullable=False),
        sa.Column("requirement_ids", sa.JSON(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("actor", sa.JSON(), nullable=False),
        sa.Column("expected_workflow_revision", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approval_items.approval_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_approval_decisions_order",
        "approval_decisions",
        ["approval_id", "occurred_at", "decision_id"],
        unique=False,
    )
    op.create_table(
        "subject_heads",
        sa.Column("subject_series_id", sa.String(), primary_key=True),
        sa.Column("current_subject_artifact_id", sa.String(), nullable=False),
        sa.Column("current_subject_revision", sa.Integer(), nullable=False),
        sa.Column("current_subject_digest", sa.String(), nullable=False),
        sa.Column("current_approval_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["current_subject_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["current_approval_id"],
            ["approval_items.approval_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "finding_revisions",
        sa.Column("finding_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revision_schema_version", sa.String(), nullable=False),
        sa.Column("supersedes_revision", sa.Integer(), nullable=True),
        sa.Column("finding_digest", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("finding_id", "revision"),
        sa.UniqueConstraint("finding_digest", name="uq_finding_revision_digest"),
    )
    op.create_table(
        "finding_heads",
        sa.Column("finding_id", sa.String(), primary_key=True),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("current_digest", sa.String(), nullable=False),
        sa.Column("row_revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["finding_id", "current_revision"],
            ["finding_revisions.finding_id", "finding_revisions.revision"],
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "conflict_sets",
        sa.Column("conflict_set_id", sa.String(), primary_key=True),
        sa.Column("schema_version", sa.String(), nullable=False),
        sa.Column("base_snapshot_id", sa.String(), nullable=False),
        sa.Column("current_snapshot_id", sa.String(), nullable=False),
        sa.Column("proposed_patch_artifact_id", sa.String(), nullable=False),
        sa.Column("expected_ref_revision", sa.Integer(), nullable=False),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("non_conflicting_ops_digest", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposed_patch_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_conflict_sets_created",
        "conflict_sets",
        ["created_at", "conflict_set_id"],
        unique=False,
    )
    op.create_table(
        "merge_conflicts",
        sa.Column("conflict_set_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("conflict_id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("base", sa.JSON(), nullable=False),
        sa.Column("current", sa.JSON(), nullable=False),
        sa.Column("proposed", sa.JSON(), nullable=False),
        sa.Column("allowed_resolutions", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conflict_set_id"],
            ["conflict_sets.conflict_set_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conflict_set_id", "ordinal"),
        sa.UniqueConstraint("conflict_set_id", "conflict_id", name="uq_conflict_set_id"),
        sa.UniqueConstraint("conflict_set_id", "path", name="uq_conflict_set_path"),
    )
    op.create_index(
        "ix_merge_conflicts_path",
        "merge_conflicts",
        ["conflict_set_id", "path", "ordinal"],
        unique=False,
    )
    op.create_table(
        "ref_transitions",
        sa.Column("transition_id", sa.String(), primary_key=True),
        sa.Column("transition_schema_version", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ref_name", sa.String(), nullable=False),
        sa.Column("from_artifact_id", sa.String(), nullable=False),
        sa.Column("from_revision", sa.Integer(), nullable=False),
        sa.Column("to_artifact_id", sa.String(), nullable=False),
        sa.Column("to_revision", sa.Integer(), nullable=False),
        sa.Column("approval_item_id", sa.String(), nullable=False),
        sa.Column("actor", sa.JSON(), nullable=False),
        sa.Column("initiated_by", sa.JSON(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["from_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_item_id"],
            ["approval_items.approval_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_ref_transitions_ref_time",
        "ref_transitions",
        ["ref_name", "occurred_at"],
        unique=False,
    )
    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("resource_kind", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("scope", "operation", "key"),
    )
    op.create_index(
        "ix_idempotency_resource",
        "idempotency_records",
        ["resource_kind", "resource_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_table("ref_transitions")
    op.drop_table("merge_conflicts")
    op.drop_table("conflict_sets")
    op.drop_table("finding_heads")
    op.drop_table("finding_revisions")
    op.drop_table("subject_heads")
    op.drop_table("approval_decisions")
    op.drop_table("approval_items")
    op.drop_table("policy_snapshots")
    op.drop_table("role_assignments")
    op.drop_table("principals")
