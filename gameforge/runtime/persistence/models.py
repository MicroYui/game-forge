"""SQLAlchemy models for the additive local persistence schema."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base shared by runtime repositories and Alembic."""


class ArtifactRow(Base):
    """Legacy lineage@1 columns plus the nullable lineage@2 ObjectRef."""

    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    lineage_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    version_tuple: Mapped[dict] = mapped_column(JSON, nullable=False)
    lineage: Mapped[list] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    object_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class RefRow(Base):
    """Current named pointer with a monotonic CAS revision."""

    __tablename__ = "refs"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )


class RefHistoryRow(Base):
    """Append-only ref history; ``seq`` equals the committed ref revision."""

    __tablename__ = "ref_history"
    __table_args__ = (Index("uq_ref_history_name_seq", "name", "seq", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)


class AuditRow(Base):
    """Shared audit table retaining audit@1 bytes and nullable audit@2 fields."""

    __tablename__ = "audit"
    __table_args__ = (Index("uq_audit_chain_seq", "chain_id", "chain_seq", unique=True),)

    # This remains the audit@1 wire seq and the physical row id. Audit@2 uses
    # chain_seq so independent chains can each begin at one.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audit_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ts: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    prev_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    chain_id: Mapped[str | None] = mapped_column(String, nullable=True)
    chain_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_v2: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    initiated_by: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    subject: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ObjectBindingRow(Base):
    __tablename__ = "object_bindings"
    __table_args__ = (
        Index(
            "ix_object_bindings_gc_order",
            "status",
            "verified_at",
            "object_key",
            "store_id",
        ),
    )

    object_key: Mapped[str] = mapped_column(String, primary_key=True)
    store_id: Mapped[str] = mapped_column(String, primary_key=True)
    binding_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    object_ref_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    location_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    object_sha256: Mapped[str] = mapped_column(String, nullable=False)
    object_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    backend_generation: Mapped[str] = mapped_column(String, nullable=False)
    etag: Mapped[str | None] = mapped_column(String, nullable=True)
    storage_class: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    verified_at: Mapped[str] = mapped_column(String, nullable=False)


class ReadSnapshotRow(Base):
    __tablename__ = "read_snapshots"
    __table_args__ = (Index("ix_read_snapshots_expiry", "expires_at", "snapshot_id"),)

    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    snapshot_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    resource_kind: Mapped[str] = mapped_column(String, nullable=False)
    query_hash: Mapped[str] = mapped_column(String, nullable=False)
    authz_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    stable_sort_schema_id: Mapped[str] = mapped_column(String, nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    high_watermark: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    materialized_item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)


class MaterializedReadItemRow(Base):
    __tablename__ = "materialized_read_items"
    __table_args__ = (UniqueConstraint("snapshot_id", "resource_id", name="uq_snapshot_resource"),)

    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("read_snapshots.snapshot_id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    observed_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    view_schema_id: Mapped[str] = mapped_column(String, nullable=False)
    canonical_view: Mapped[dict] = mapped_column(JSON, nullable=False)
    view_hash: Mapped[str] = mapped_column(String, nullable=False)


class AuditHeadRow(Base):
    __tablename__ = "audit_heads"

    chain_id: Mapped[str] = mapped_column(String, primary_key=True)
    head_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    head_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)


class PrincipalRow(Base):
    __tablename__ = "principals"
    __table_args__ = (Index("ix_principals_status_id", "status", "principal_id"),)

    principal_id: Mapped[str] = mapped_column(String, primary_key=True)
    principal_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    credential_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    authz_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    disabled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class RoleAssignmentRow(Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        Index(
            "uq_role_assignments_active_identity",
            "principal_id",
            "role",
            "scope_key",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_role_assignments_principal_status", "principal_id", "status"),
    )

    assignment_id: Mapped[str] = mapped_column(String, primary_key=True)
    assignment_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    scope_key: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[dict | str | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_at: Mapped[str] = mapped_column(String, nullable=False)
    granted_by: Mapped[dict] = mapped_column(JSON, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_by: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class PasswordCredentialRow(Base):
    __tablename__ = "password_credentials"
    __table_args__ = (
        UniqueConstraint(
            "normalized_login_name",
            name="uq_password_credentials_normalized_login",
        ),
        Index(
            "ix_password_credentials_principal_status",
            "principal_id",
            "status",
            "credential_id",
        ),
    )

    credential_id: Mapped[str] = mapped_column(String, primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="RESTRICT"),
        nullable=False,
    )
    normalized_login_name: Mapped[str] = mapped_column(String, nullable=False)
    normalization_policy_version: Mapped[str] = mapped_column(String, nullable=False)
    normalization_policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    hash_policy_version: Mapped[str] = mapped_column(String, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    changed_at: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class ApiKeyRow(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("key_digest", name="uq_api_keys_digest"),
        Index(
            "ix_api_keys_principal_status",
            "principal_id",
            "status",
            "api_key_id",
        ),
    )

    api_key_id: Mapped[str] = mapped_column(String, primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="RESTRICT"),
        nullable=False,
    )
    key_prefix: Mapped[str] = mapped_column(String, nullable=False)
    key_digest: Mapped[str] = mapped_column(String, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class SessionRow(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_sessions_token_digest"),
        Index(
            "ix_sessions_principal_expiry",
            "principal_id",
            "absolute_expires_at",
            "session_id",
        ),
        Index(
            "ix_sessions_source_credential",
            "source_credential_id",
            "credential_version",
            "session_id",
        ),
    )

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.principal_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_credential_id: Mapped[str] = mapped_column(String, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    token_digest: Mapped[str] = mapped_column(String, nullable=False)
    csrf_secret_digest: Mapped[str] = mapped_column(String, nullable=False)
    signing_key_id: Mapped[str] = mapped_column(String, nullable=False)
    issued_at: Mapped[str] = mapped_column(String, nullable=False)
    absolute_expires_at: Mapped[str] = mapped_column(String, nullable=False)
    idle_expires_at: Mapped[str] = mapped_column(String, nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class PolicySnapshotRow(Base):
    __tablename__ = "policy_snapshots"
    __table_args__ = (
        Index(
            "ix_policy_snapshots_digest",
            "document_kind",
            "document_id",
            "document_digest",
        ),
    )

    document_kind: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(String, primary_key=True)
    document_version: Mapped[str] = mapped_column(String, primary_key=True)
    document_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class SubjectHeadRow(Base):
    __tablename__ = "subject_heads"

    subject_series_id: Mapped[str] = mapped_column(String, primary_key=True)
    current_subject_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    current_subject_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    current_subject_digest: Mapped[str] = mapped_column(String, nullable=False)
    current_approval_id: Mapped[str] = mapped_column(
        ForeignKey("approval_items.approval_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class ApprovalItemRow(Base):
    __tablename__ = "approval_items"
    __table_args__ = (
        UniqueConstraint(
            "subject_series_id",
            "subject_revision",
            name="uq_approval_subject_revision",
        ),
        Index("ix_approval_queue", "status", "created_at", "approval_id"),
        Index("ix_approval_subject_artifact", "subject_artifact_id"),
        Index(
            "uq_approval_active_validation_run",
            "active_validation_run_id",
            unique=True,
            sqlite_where=text("active_validation_run_id IS NOT NULL"),
        ),
    )

    approval_id: Mapped[str] = mapped_column(String, primary_key=True)
    approval_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    subject_series_id: Mapped[str] = mapped_column(String, nullable=False)
    subject_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_kind: Mapped[str] = mapped_column(String, nullable=False)
    subject_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    subject_digest: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_approval_id: Mapped[str | None] = mapped_column(
        ForeignKey("approval_items.approval_id", ondelete="RESTRICT"),
        nullable=True,
    )
    proposer: Mapped[dict] = mapped_column(JSON, nullable=False)
    domain_scope: Mapped[dict] = mapped_column(JSON, nullable=False)
    domain_registry_ref: Mapped[dict] = mapped_column(JSON, nullable=False)
    route_policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    role_policy_version: Mapped[str] = mapped_column(String, nullable=False)
    role_policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    approval_policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    requirements: Mapped[list] = mapped_column(JSON, nullable=False)
    active_validation_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_validation_failure_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=True,
    )
    evidence_set_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=True,
    )
    regression_evidence_artifact_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    target_binding: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    auto_apply_proof: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    submitted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)
    applied_at: Mapped[str | None] = mapped_column(String, nullable=True)


class ApprovalDecisionRow(Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        Index("ix_approval_decisions_order", "approval_id", "occurred_at", "decision_id"),
    )

    decision_id: Mapped[str] = mapped_column(String, primary_key=True)
    approval_id: Mapped[str] = mapped_column(
        ForeignKey("approval_items.approval_id", ondelete="RESTRICT"),
        nullable=False,
    )
    requirement_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[dict] = mapped_column(JSON, nullable=False)
    expected_workflow_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String, nullable=False)
    comment: Mapped[str | None] = mapped_column(String, nullable=True)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)


class FindingRevisionRow(Base):
    __tablename__ = "finding_revisions"
    __table_args__ = (UniqueConstraint("finding_digest", name="uq_finding_revision_digest"),)

    finding_id: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    supersedes_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finding_digest: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class FindingHeadRow(Base):
    __tablename__ = "finding_heads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["finding_id", "current_revision"],
            ["finding_revisions.finding_id", "finding_revisions.revision"],
            ondelete="RESTRICT",
        ),
    )

    finding_id: Mapped[str] = mapped_column(String, primary_key=True)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    current_digest: Mapped[str] = mapped_column(String, nullable=False)
    row_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class RefTransitionRow(Base):
    __tablename__ = "ref_transitions"
    __table_args__ = (Index("ix_ref_transitions_ref_time", "ref_name", "occurred_at"),)

    transition_id: Mapped[str] = mapped_column(String, primary_key=True)
    transition_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    ref_name: Mapped[str] = mapped_column(String, nullable=False)
    from_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    to_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    to_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_item_id: Mapped[str] = mapped_column(
        ForeignKey("approval_items.approval_id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor: Mapped[dict] = mapped_column(JSON, nullable=False)
    initiated_by: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)


class ConflictSetRow(Base):
    __tablename__ = "conflict_sets"
    __table_args__ = (Index("ix_conflict_sets_created", "created_at", "conflict_set_id"),)

    conflict_set_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[str] = mapped_column(String, nullable=False)
    base_snapshot_id: Mapped[str] = mapped_column(String, nullable=False)
    current_snapshot_id: Mapped[str] = mapped_column(String, nullable=False)
    proposed_patch_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    expected_ref_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False)
    non_conflicting_ops_digest: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_digest: Mapped[str] = mapped_column(String, nullable=False)


class MergeConflictRow(Base):
    __tablename__ = "merge_conflicts"
    __table_args__ = (
        UniqueConstraint("conflict_set_id", "conflict_id", name="uq_conflict_set_id"),
        UniqueConstraint("conflict_set_id", "path", name="uq_conflict_set_path"),
        Index("ix_merge_conflicts_path", "conflict_set_id", "path", "ordinal"),
    )

    conflict_set_id: Mapped[str] = mapped_column(
        ForeignKey("conflict_sets.conflict_set_id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    conflict_id: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    base: Mapped[dict] = mapped_column(JSON, nullable=False)
    current: Mapped[dict] = mapped_column(JSON, nullable=False)
    proposed: Mapped[dict] = mapped_column(JSON, nullable=False)
    allowed_resolutions: Mapped[list] = mapped_column(JSON, nullable=False)
    content_digest: Mapped[str] = mapped_column(String, nullable=False)


class IdempotencyRecordRow(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (Index("ix_idempotency_resource", "resource_kind", "resource_id"),)

    scope: Mapped[str] = mapped_column(String, primary_key=True)
    operation: Mapped[str] = mapped_column(String, primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    resource_kind: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_scope",
            "idempotency_key",
            name="uq_runs_idempotency",
        ),
        Index("ix_runs_claim_order", "status", "retry_not_before_utc", "created_at", "run_id"),
        Index("ix_runs_deadlines", "status", "queue_deadline_utc", "overall_deadline_utc"),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    kind_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_scope: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    run_kind_definition_digest: Mapped[str] = mapped_column(String, nullable=False)
    outcome_policy_set_digest: Mapped[str] = mapped_column(String, nullable=False)
    migration_capability_matrix: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    failure_classifier: Mapped[dict] = mapped_column(JSON, nullable=False)
    dispatch_trace_carrier: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    initiated_by: Mapped[dict] = mapped_column(JSON, nullable=False)
    resource_domain_scope: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    queue_deadline_utc: Mapped[str] = mapped_column(String, nullable=False)
    attempt_timeout_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    overall_deadline_utc: Mapped[str] = mapped_column(String, nullable=False)
    cancel_requested_at: Mapped[str | None] = mapped_column(String, nullable=True)
    cancel_requested_by: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    current_attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    next_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    next_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    budget_set_snapshot_id: Mapped[str] = mapped_column(String, nullable=False)
    run_budget_hold_group_id: Mapped[str] = mapped_column(String, nullable=False)
    concurrency_permit_group_id: Mapped[str | None] = mapped_column(String, nullable=True)
    retry_policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_not_before_utc: Mapped[str | None] = mapped_column(String, nullable=True)
    result_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=True,
    )
    failure_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=True,
    )
    terminal_cassette_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class RunAttemptRow(Base):
    __tablename__ = "run_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "fencing_token", name="uq_run_attempt_fencing"),
        Index("ix_run_attempts_status", "run_id", "status", "attempt_no"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    worker_principal_id: Mapped[str] = mapped_column(String, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    next_call_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_deadline_utc: Mapped[str | None] = mapped_column(String, nullable=True)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String, nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    failure_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=True,
    )
    cassette_bundle_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=True,
    )


class RunLeaseRow(Base):
    __tablename__ = "run_leases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "attempt_no", name="uq_run_lease_attempt"),
        Index(
            "uq_run_active_lease",
            "run_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_run_leases_expiry", "status", "expires_at", "run_id"),
    )

    lease_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_principal_id: Mapped[str] = mapped_column(String, nullable=False)
    acquired_at: Mapped[str] = mapped_column(String, nullable=False)
    heartbeat_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    released_at: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)


class RunEventRow(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    data_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True)


class RunCommandRow(Base):
    __tablename__ = "run_commands"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_run_command_id"),
        UniqueConstraint("run_id", "client_id", "client_seq", name="uq_run_command_client"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_run_command_idempotency"),
        ForeignKeyConstraint(
            ["run_id", "result_event_seq"],
            ["run_events.run_id", "run_events.seq"],
            ondelete="RESTRICT",
        ),
        Index("ix_run_commands_pending", "run_id", "status", "created_at", "command_id"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    command_id: Mapped[str] = mapped_column(String, primary_key=True)
    record_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    command_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    client_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    expected_run_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    payload_schema_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    claimed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claimed_fencing_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    applied_at: Mapped[str | None] = mapped_column(String, nullable=True)
    result_event_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rejection_code: Mapped[str | None] = mapped_column(String, nullable=True)


class RunIntermediateArtifactLinkRow(Base):
    __tablename__ = "run_intermediate_artifact_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "route_ordinal >= 1",
            name="ck_run_intermediate_route_ordinal_positive",
        ),
        UniqueConstraint(
            "run_id",
            "attempt_no",
            "call_ordinal",
            "route_ordinal",
            "artifact_id",
            name="uq_run_intermediate_route_artifact",
        ),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_at: Mapped[str] = mapped_column(String, nullable=False)


class RunToolIntermediateLinkRow(Base):
    __tablename__ = "run_tool_intermediate_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "target_call_ordinal >= 1",
            name="ck_run_tool_intermediate_target_call_positive",
        ),
        UniqueConstraint(
            "run_id",
            "attempt_no",
            "artifact_id",
            name="uq_run_tool_intermediate_artifact",
        ),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_call_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    agent_node_id: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_at: Mapped[str] = mapped_column(String, nullable=False)


class RunModelRouteLinkRow(Base):
    __tablename__ = "run_model_route_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "run_id",
                "attempt_no",
                "call_ordinal",
                "route_ordinal",
                "prompt_artifact_id",
            ],
            [
                "run_intermediate_artifact_links.run_id",
                "run_intermediate_artifact_links.attempt_no",
                "run_intermediate_artifact_links.call_ordinal",
                "run_intermediate_artifact_links.route_ordinal",
                "run_intermediate_artifact_links.artifact_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["native_routing_decision_id"],
            ["routing_decisions.decision_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["legacy_routing_decision_id"],
            ["legacy_import_routing_decisions.decision_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "route_ordinal >= 1",
            name="ck_run_model_route_ordinal_positive",
        ),
        CheckConstraint(
            "(routing_decision_kind = 'native' "
            "AND native_routing_decision_id = routing_decision_id "
            "AND legacy_routing_decision_id IS NULL) OR "
            "(routing_decision_kind = 'legacy_import' "
            "AND legacy_routing_decision_id = routing_decision_id "
            "AND native_routing_decision_id IS NULL)",
            name="ck_run_model_route_decision_authority",
        ),
        UniqueConstraint(
            "native_routing_decision_id",
            name="uq_run_model_route_native_decision",
        ),
        Index(
            "ix_run_model_route_links_run",
            "run_id",
            "attempt_no",
            "call_ordinal",
            "route_ordinal",
        ),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    prompt_artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    routing_decision_kind: Mapped[str] = mapped_column(String, nullable=False)
    routing_decision_id: Mapped[str] = mapped_column(String, nullable=False)
    native_routing_decision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    legacy_routing_decision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_at: Mapped[str] = mapped_column(String, nullable=False)


class RunModelResponseConsumptionRow(Base):
    __tablename__ = "run_model_response_consumptions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "attempt_no", "call_ordinal", "route_ordinal"],
            [
                "run_model_route_links.run_id",
                "run_model_route_links.attempt_no",
                "run_model_route_links.call_ordinal",
                "run_model_route_links.route_ordinal",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "route_ordinal >= 1",
            name="ck_run_model_consumption_route_ordinal_positive",
        ),
        CheckConstraint(
            "(execution_source = 'online' AND transport_attempt IS NOT NULL "
            "AND transport_attempt >= 1) OR "
            "(execution_source IN ('full_response_cache', 'cassette_replay') "
            "AND transport_attempt IS NULL)",
            name="ck_run_model_consumption_execution_shape",
        ),
        UniqueConstraint(
            "reservation_group_id",
            name="uq_run_model_consumption_reservation_group",
        ),
        UniqueConstraint(
            "run_id",
            "attempt_no",
            "call_ordinal",
            name="uq_run_model_consumption_logical_call",
        ),
        UniqueConstraint(
            "cassette_shard_artifact_id",
            name="uq_run_model_consumption_cassette_shard",
        ),
        Index(
            "ix_run_model_response_consumptions_run",
            "run_id",
            "attempt_no",
            "call_ordinal",
            "route_ordinal",
        ),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumption_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    execution_source: Mapped[str] = mapped_column(String, nullable=False)
    reservation_group_id: Mapped[str] = mapped_column(
        ForeignKey("reservation_groups.reservation_group_id", ondelete="RESTRICT"),
        nullable=False,
    )
    transport_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cassette_shard_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=True,
    )
    response_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    consumed_at: Mapped[str] = mapped_column(String, nullable=False)


class RunFindingLinkRow(Base):
    __tablename__ = "run_finding_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "attempt_no"],
            ["run_attempts.run_id", "run_attempts.attempt_no"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["finding_id", "finding_revision"],
            ["finding_revisions.finding_id", "finding_revisions.revision"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "run_id",
            "finding_id",
            "finding_revision",
            name="uq_run_finding_revision",
        ),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    finding_id: Mapped[str] = mapped_column(String, nullable=False)
    finding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_digest: Mapped[str] = mapped_column(String, nullable=False)
    evidence_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )


class BudgetRow(Base):
    """Current authoritative budget head; updates use revision CAS in CostLedger."""

    __tablename__ = "budgets"
    __table_args__ = (
        Index("ix_budgets_scope_status", "scope_kind", "scope_id", "status", "budget_id"),
        Index("ix_budgets_deadline", "status", "deadline_utc", "budget_id"),
    )

    budget_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String, nullable=False)
    scope_id: Mapped[str] = mapped_column(String, nullable=False)
    policy_version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    deadline_utc: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class BudgetSetSnapshotRow(Base):
    """Immutable selection of every budget applicable to one Run."""

    __tablename__ = "budget_set_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_budget_set_run"),
        Index("ix_budget_sets_captured", "captured_at", "budget_set_snapshot_id"),
    )

    budget_set_snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    selection_policy_version: Mapped[str] = mapped_column(String, nullable=False)
    captured_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class BudgetSnapshotRow(Base):
    """One immutable member of a BudgetSetSnapshot."""

    __tablename__ = "budget_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "budget_set_snapshot_id",
            "ordinal",
            name="uq_budget_snapshot_ordinal",
        ),
        UniqueConstraint(
            "budget_set_snapshot_id",
            "budget_id",
            name="uq_budget_snapshot_budget",
        ),
    )

    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    budget_set_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("budget_set_snapshots.budget_set_snapshot_id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_id: Mapped[str] = mapped_column(
        ForeignKey("budgets.budget_id", ondelete="RESTRICT"),
        nullable=False,
    )
    scope_kind: Mapped[str] = mapped_column(String, nullable=False)
    scope_id: Mapped[str] = mapped_column(String, nullable=False)
    budget_revision_at_freeze: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ReservationGroupRow(Base):
    """Reservation lifecycle head with an immutable idempotency identity."""

    __tablename__ = "reservation_groups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parent_hold_group_id"],
            ["reservation_groups.reservation_group_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "run_id",
            "scope",
            "idempotency_key",
            name="uq_reservation_group_idempotency",
        ),
        Index(
            "ix_reservation_groups_run_attempt",
            "run_id",
            "attempt_no",
            "created_at",
            "reservation_group_id",
        ),
        Index(
            "ix_reservation_groups_status_expiry",
            "status",
            "expires_at",
            "reservation_group_id",
        ),
    )

    reservation_group_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    budget_set_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("budget_set_snapshots.budget_set_snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_hold_group_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    transport_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fencing_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class BudgetReservationRow(Base):
    """Per-budget member of a reservation group."""

    __tablename__ = "budget_reservations"
    __table_args__ = (
        UniqueConstraint(
            "reservation_group_id",
            "budget_id",
            name="uq_budget_reservation_member",
        ),
        Index(
            "ix_budget_reservations_budget_status",
            "budget_id",
            "status",
            "reservation_id",
        ),
    )

    reservation_id: Mapped[str] = mapped_column(String, primary_key=True)
    reservation_group_id: Mapped[str] = mapped_column(
        ForeignKey("reservation_groups.reservation_group_id", ondelete="CASCADE"),
        nullable=False,
    )
    budget_id: Mapped[str] = mapped_column(
        ForeignKey("budgets.budget_id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ModelCatalogSnapshotRow(Base):
    """Immutable content-addressed model catalog history."""

    __tablename__ = "model_catalog_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "catalog_version",
            "catalog_digest",
            name="uq_model_catalog_exact_ref",
        ),
        UniqueConstraint("catalog_digest", name="uq_model_catalog_digest"),
        Index("ix_model_catalog_created", "created_at", "catalog_version"),
    )

    catalog_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalog_digest: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class RoutingPolicyRow(Base):
    """Immutable routing policy bound to one exact catalog."""

    __tablename__ = "routing_policies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["catalog_version", "catalog_digest"],
            [
                "model_catalog_snapshots.catalog_version",
                "model_catalog_snapshots.catalog_digest",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "policy_version",
            "routing_policy_digest",
            name="uq_routing_policy_exact_ref",
        ),
        UniqueConstraint("routing_policy_digest", name="uq_routing_policy_digest"),
    )

    policy_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    routing_policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    catalog_version: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class RoutingDecisionRow(Base):
    """Append-only native route choice made before model execution."""

    __tablename__ = "routing_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["policy_version", "routing_policy_digest"],
            ["routing_policies.policy_version", "routing_policies.routing_policy_digest"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["catalog_version", "catalog_digest"],
            [
                "model_catalog_snapshots.catalog_version",
                "model_catalog_snapshots.catalog_digest",
            ],
            ondelete="RESTRICT",
        ),
        Index(
            "ix_routing_decisions_run",
            "run_id",
            "attempt_no",
            "decided_at",
            "decision_id",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    rule_id: Mapped[str] = mapped_column(String, nullable=False)
    model_snapshot: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False)
    budget_set_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("budget_set_snapshots.budget_set_snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    fallback_index: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    routing_policy_digest: Mapped[str] = mapped_column(String, nullable=False)
    catalog_version: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_digest: Mapped[str] = mapped_column(String, nullable=False)
    execution_source: Mapped[str] = mapped_column(String, nullable=False)
    decided_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class LegacyImportRoutingDecisionRow(Base):
    """Verified legacy replay route evidence; never a fabricated native route."""

    __tablename__ = "legacy_import_routing_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["catalog_version", "catalog_digest"],
            [
                "model_catalog_snapshots.catalog_version",
                "model_catalog_snapshots.catalog_digest",
            ],
            ondelete="RESTRICT",
        ),
        Index("ix_legacy_route_model", "model_snapshot", "decision_id"),
    )

    decision_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_wire_sha256: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    model_snapshot: Mapped[str] = mapped_column(String, nullable=False)
    catalog_version: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_digest: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class UsageEntryRow(Base):
    """Append-only one-observation ledger entry."""

    __tablename__ = "usage_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["native_routing_decision_id"],
            ["routing_decisions.decision_id"],
            name="fk_usage_native_routing_decision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["legacy_routing_decision_id"],
            ["legacy_import_routing_decisions.decision_id"],
            name="fk_usage_legacy_routing_decision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["adjustment_of_usage_id"],
            ["usage_entries.usage_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("usage_identity", name="uq_usage_identity"),
        Index(
            "ix_usage_run_attempt",
            "run_id",
            "attempt_no",
            "recorded_at",
            "usage_id",
        ),
        Index(
            "ix_usage_reservation_group",
            "reservation_group_id",
            "recorded_at",
            "usage_id",
        ),
    )

    usage_id: Mapped[str] = mapped_column(String, primary_key=True)
    usage_identity: Mapped[str] = mapped_column(String, nullable=False)
    reservation_group_id: Mapped[str] = mapped_column(
        ForeignKey("reservation_groups.reservation_group_id", ondelete="RESTRICT"),
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    transport_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_source: Mapped[str] = mapped_column(String, nullable=False)
    retry_index: Mapped[int] = mapped_column(Integer, nullable=False)
    routing_decision_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    routing_decision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    native_routing_decision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    legacy_routing_decision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    adjustment_of_usage_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fencing_token_at_reserve: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class PermitGroupRow(Base):
    """Current concurrency permit-group head for one exact worker lease."""

    __tablename__ = "permit_groups"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "lease_id",
            "fencing_token",
            name="uq_permit_group_lease",
        ),
        Index(
            "ix_permit_groups_status_expiry",
            "status",
            "expires_at",
            "permit_group_id",
        ),
    )

    permit_group_id: Mapped[str] = mapped_column(String, primary_key=True)
    budget_set_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("budget_set_snapshots.budget_set_snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    lease_id: Mapped[str] = mapped_column(String, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    acquired_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ConcurrencyPermitRow(Base):
    """Per-budget member of a PermitGroup."""

    __tablename__ = "concurrency_permits"
    __table_args__ = (
        UniqueConstraint(
            "permit_group_id",
            "budget_id",
            name="uq_concurrency_permit_member",
        ),
        Index(
            "ix_concurrency_permits_budget_status",
            "budget_id",
            "status",
            "expires_at",
            "permit_id",
        ),
    )

    permit_id: Mapped[str] = mapped_column(String, primary_key=True)
    permit_group_id: Mapped[str] = mapped_column(
        ForeignKey("permit_groups.permit_group_id", ondelete="CASCADE"),
        nullable=False,
    )
    budget_id: Mapped[str] = mapped_column(
        ForeignKey("budgets.budget_id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    lease_id: Mapped[str] = mapped_column(String, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    acquired_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class WorkloadProfileRow(Base):
    """Immutable measured workload identity used by SLO definitions."""

    __tablename__ = "workload_profiles"

    profile_id: Mapped[str] = mapped_column(String, primary_key=True)
    dataset_artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    relation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    constraint_count: Mapped[int] = mapped_column(Integer, nullable=False)
    task_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    environment_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class SLODefinitionRow(Base):
    """Immutable SLI/SLO policy bound to one measured workload profile."""

    __tablename__ = "slo_definitions"

    slo_id: Mapped[str] = mapped_column(String, primary_key=True)
    workload_profile_id: Mapped[str] = mapped_column(
        ForeignKey("workload_profiles.profile_id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    policy_version: Mapped[str] = mapped_column(String, nullable=False)
    effective_from: Mapped[str] = mapped_column(String, nullable=False)
    rolling_window_s: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluation_interval_s: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class AlertRuleRow(Base):
    """Immutable alert policy for one exact SLO definition."""

    __tablename__ = "alert_rules"

    alert_rule_id: Mapped[str] = mapped_column(String, primary_key=True)
    slo_id: Mapped[str] = mapped_column(
        ForeignKey("slo_definitions.slo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    severity: Mapped[str] = mapped_column(String, nullable=False)
    policy_version: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class SLOEvaluationRow(Base):
    """Content-addressed immutable result for one closed SLO window."""

    __tablename__ = "slo_evaluations"
    __table_args__ = (
        Index(
            "ix_slo_evaluations_order",
            "slo_id",
            "window_start",
            "window_end",
            "evaluation_id",
        ),
    )

    evaluation_id: Mapped[str] = mapped_column(String, primary_key=True)
    slo_id: Mapped[str] = mapped_column(
        ForeignKey("slo_definitions.slo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    window_start: Mapped[str] = mapped_column(String, nullable=False)
    window_end: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class AlertInstanceRow(Base):
    """Mutable alert state head guarded by monotonic revision CAS."""

    __tablename__ = "alert_instances"
    __table_args__ = (
        UniqueConstraint(
            "alert_rule_id",
            "dedup_key",
            name="uq_alert_instance_rule_dedup",
        ),
        Index(
            "ix_alert_instances_state",
            "state",
            "alert_rule_id",
            "alert_instance_id",
        ),
    )

    alert_instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    alert_rule_id: Mapped[str] = mapped_column(
        ForeignKey("alert_rules.alert_rule_id", ondelete="RESTRICT"),
        nullable=False,
    )
    dedup_key: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    last_evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("slo_evaluations.evaluation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
