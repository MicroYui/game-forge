"""SQLAlchemy 2.0 declarative models for the version/lineage/audit store
(contract §5, §12A.3).

Mirrors `gameforge.contracts.lineage.Artifact` / `AuditRecord` and
`gameforge.spine.versioning.store` (`InMemoryArtifactStore` / `RefStore`) at
the persistence layer: artifacts are immutable + content-addressed, `refs`
are named pointers (rollback = repoint, never delete), `ref_history` is the
full pointer history per name, and `audit` is an append-only WORM log with a
content-hash chain (`prev_hash`).

All column types are DB-agnostic (`String`, `Integer`, `JSON`) so the same
schema works against sqlite (tests/local) and any production RDBMS via
`DATABASE_URL`, without a code change.
"""

from __future__ import annotations

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base shared by all persistence models and Alembic's
    `target_metadata`."""


class ArtifactRow(Base):
    """An immutable, content-addressed artifact (IR snapshot, config export,
    checker run, playtest trace, patch — contract §5's `Artifact`)."""

    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    version_tuple: Mapped[dict] = mapped_column(JSON, nullable=False)
    lineage: Mapped[list] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, nullable=True)


class RefRow(Base):
    """Current named pointer to an artifact_id. Rollback = repoint, never
    delete the underlying artifact."""

    __tablename__ = "refs"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=True)


class RefHistoryRow(Base):
    """Append-only history of every value a named ref has pointed at, in
    order (`seq`), so a rollback's prior value stays traceable."""

    __tablename__ = "ref_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)


class AuditRow(Base):
    """Append-only, WORM audit log entry with a content-hash chain
    (`prev_hash` links to the previous entry's `content_hash`)."""

    __tablename__ = "audit"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=True)
    ts: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String, nullable=True)
