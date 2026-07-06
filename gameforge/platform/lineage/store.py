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

from typing import Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from gameforge.contracts.lineage import Artifact, VersionTuple
from gameforge.runtime.persistence.models import ArtifactRow, RefHistoryRow, RefRow
from gameforge.spine.versioning.store import InMemoryArtifactStore, LineageGraph

SessionFactory = Callable[[], Session]


def _row_to_artifact(row: ArtifactRow) -> Artifact:
    """Reconstruct a full `Artifact` (all lineage/version-tuple fields) from
    its persisted row — the Task 12 columns cover every `Artifact` field, so
    nothing is lost on the round trip."""
    return Artifact(
        artifact_id=row.artifact_id,
        lineage_schema_version=row.lineage_schema_version,
        kind=row.kind,
        version_tuple=VersionTuple(**row.version_tuple),
        lineage=list(row.lineage),
        payload_hash=row.payload_hash,
        created_at=row.created_at,
        meta=dict(row.meta) if row.meta is not None else {},
    )


def _artifact_to_row(artifact: Artifact) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=artifact.artifact_id,
        lineage_schema_version=artifact.lineage_schema_version,
        kind=artifact.kind,
        version_tuple=artifact.version_tuple.model_dump(),
        lineage=list(artifact.lineage),
        payload_hash=artifact.payload_hash,
        created_at=artifact.created_at,
        meta=dict(artifact.meta),
    )


class SqlArtifactStore:
    """`artifacts` table persistence, interface-compatible with
    `spine.versioning.store.InMemoryArtifactStore` (`put`/`get`/`all`), plus
    `ancestors` (which `spine`'s in-memory store leaves to a separate
    `LineageGraph`)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def put(self, artifact: Artifact) -> str:
        """Persist `artifact`. Idempotent: artifacts are immutable and
        content-addressed by `artifact_id`, so re-putting the same id is a
        no-op overwrite of identical content (a `merge`, not an insert that
        would raise on a duplicate primary key)."""
        with self._session_factory() as session:
            session.merge(_artifact_to_row(artifact))
            session.commit()
        return artifact.artifact_id

    def get(self, artifact_id: str) -> Artifact | None:
        with self._session_factory() as session:
            row = session.get(ArtifactRow, artifact_id)
            return None if row is None else _row_to_artifact(row)

    def all(self) -> list[Artifact]:
        with self._session_factory() as session:
            rows = session.query(ArtifactRow).all()
            return [_row_to_artifact(row) for row in rows]

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
            next_seq = session.query(func.max(RefHistoryRow.seq)).filter(
                RefHistoryRow.name == name
            ).scalar()
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
