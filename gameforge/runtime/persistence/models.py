"""SQLAlchemy models for the additive local persistence schema."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_at: Mapped[str] = mapped_column(String, nullable=False)


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
