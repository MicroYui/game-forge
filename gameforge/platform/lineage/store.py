"""SQLAlchemy-backed artifact/lineage/ref store (contract §5, §12A.3).

This is the production persistence counterpart to
`gameforge.spine.versioning.store` (`InMemoryArtifactStore` / `RefStore` /
`LineageGraph`): same behavioral interface (`put`/`get`/`all`,
`set`/`get`/`rollback`/`history`), but backed by the `artifacts` / `refs` /
`ref_history` tables (Task 12's `gameforge.runtime.persistence.models`)
instead of an in-process dict, so artifacts and named pointers survive
process restarts and are queryable via any `DATABASE_URL` (sqlite/Postgres).

`ancestors()` deliberately reuses `spine`'s `LineageGraph` rather than
re-implementing transitive-parent traversal: it materializes the DB's
artifacts into an in-memory `InMemoryArtifactStore` (a read-only view, never
written back) and delegates to `LineageGraph.ancestors`. `platform → spine`
is an allowed dependency direction (only `spine → platform` is forbidden),
so this reuse is intentional, not a layering violation.
"""

from __future__ import annotations

import secrets
from typing import Callable

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gameforge.contracts.lineage import ArtifactV1, ArtifactV2
from gameforge.contracts.storage import ObjectStore
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.models import ArtifactRow, RefHistoryRow, RefRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from gameforge.spine.versioning.store import InMemoryArtifactStore, LineageGraph

SessionFactory = Callable[[], Session]


class SqlArtifactStore:
    """`artifacts` table persistence, interface-compatible with
    `spine.versioning.store.InMemoryArtifactStore` (`put`/`get`/`all`), plus
    `ancestors` (which `spine`'s in-memory store leaves to a separate
    `LineageGraph`)."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        object_store: ObjectStore | None = None,
        default_store_id: str | None = None,
    ) -> None:
        if (object_store is None) != (default_store_id is None):
            raise ValueError("object_store and default_store_id must be provided together")
        self._session_factory = session_factory
        with session_factory() as session:
            bind = session.get_bind()
            self._engine = bind if isinstance(bind, Engine) else bind.engine
        self._object_store = object_store
        self._default_store_id = default_store_id
        self._clock = SystemUtcClock()
        self._cursor_signer = CursorSigner(
            signing_key=secrets.token_bytes(32),
            clock=self._clock,
        )
        self._unit_of_work = SqliteUnitOfWork(self._engine, self._capabilities)

    def _repository(self, session: Session) -> SqlArtifactRepository:
        binding_repository = None
        if self._object_store is not None and self._default_store_id is not None:
            binding_repository = SqlObjectBindingRepository(
                session,
                object_store=self._object_store,
                default_store_id=self._default_store_id,
            )
        return SqlArtifactRepository(
            session,
            binding_repository=binding_repository,
            cursor_signer=self._cursor_signer,
            clock=self._clock,
        )

    def _capabilities(self, session: Session) -> TransactionCapabilities:
        unavailable = object()
        return TransactionCapabilities(
            refs=unavailable,
            audit=unavailable,
            approvals=unavailable,
            lineage=self._repository(session),
            object_bindings=unavailable,
            runs=unavailable,
            cost=unavailable,
        )

    def put(self, artifact: ArtifactV1 | ArtifactV2) -> str:
        """Publish through the immutable repository and an owning SQLite UoW."""
        with self._unit_of_work.begin() as transaction:
            transaction.lineage.put(artifact)
        return artifact.artifact_id

    def get(self, artifact_id: str) -> ArtifactV1 | ArtifactV2 | None:
        with self._session_factory() as session:
            return self._repository(session).get(artifact_id)

    def all(self) -> list[ArtifactV1 | ArtifactV2]:
        with self._session_factory() as session:
            repository = self._repository(session)
            artifact_ids = session.scalars(
                select(ArtifactRow.artifact_id).order_by(ArtifactRow.artifact_id)
            ).all()
            artifacts = [repository.get(artifact_id) for artifact_id in artifact_ids]
            return [artifact for artifact in artifacts if artifact is not None]

    def ancestors(self, artifact_id: str) -> list[str]:
        """Transitive parents, computed by materializing every persisted
        artifact into an in-memory `LineageGraph` and delegating to it
        (contract-mandated reuse; see module docstring)."""
        view = InMemoryArtifactStore()
        for artifact in self.all():
            view.put(artifact)
        return LineageGraph(view).ancestors(artifact_id)


class SqlRefStore:
    """`refs` (current pointer) + `ref_history` (append-only pointer log)
    persistence, interface-compatible with `spine.versioning.store.RefStore`
    (`set`/`get`/`rollback`/`history`)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def set(self, name: str, artifact_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(RefRow, name)
            if row is None:
                session.add(RefRow(name=name, artifact_id=artifact_id))
            else:
                row.artifact_id = artifact_id
            next_seq = (
                session.query(func.max(RefHistoryRow.seq))
                .filter(RefHistoryRow.name == name)
                .scalar()
            )
            next_seq = 1 if next_seq is None else next_seq + 1
            session.add(RefHistoryRow(name=name, artifact_id=artifact_id, seq=next_seq))
            session.commit()

    def get(self, name: str) -> str | None:
        with self._session_factory() as session:
            row = session.get(RefRow, name)
            return None if row is None else row.artifact_id

    def rollback(self, name: str, artifact_id: str) -> None:
        """Repoint `name` at a historical `artifact_id`. Never deletes: the
        prior value stays in `history`, and `ref_history` gains a new entry
        rather than losing one."""
        self.set(name, artifact_id)

    def history(self, name: str) -> list[str]:
        with self._session_factory() as session:
            rows = (
                session.query(RefHistoryRow)
                .filter(RefHistoryRow.name == name)
                .order_by(RefHistoryRow.seq)
                .all()
            )
            return [row.artifact_id for row in rows]
