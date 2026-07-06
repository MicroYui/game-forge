"""In-memory artifact store + lineage DAG + ref/rollback (contract §5).

Pure, deterministic, in-memory core. Artifacts are immutable and
content-addressed (their `artifact_id` is a hash of their content) so
`put` is naturally idempotent: re-putting the same artifact_id is a no-op
overwrite with identical content. Nothing is ever deleted — `RefStore.rollback`
only repoints a named pointer at a historical artifact_id, and every prior
artifact remains retrievable from the store and from `history`.

A DB-backed persistence layer (SQLAlchemy) is a separate later task
(platform/lineage); this module must never import it.
"""

from __future__ import annotations

from gameforge.contracts.lineage import Artifact, VersionTuple


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}

    def put(self, artifact: Artifact) -> str:
        self._artifacts[artifact.artifact_id] = artifact
        return artifact.artifact_id

    def get(self, artifact_id: str) -> Artifact | None:
        return self._artifacts.get(artifact_id)

    def all(self) -> list[Artifact]:
        return list(self._artifacts.values())


class LineageGraph:
    """Transitive-parent traversal + provenance lookup over an artifact store."""

    def __init__(self, store: InMemoryArtifactStore) -> None:
        self._store = store

    def _parents(self, artifact_id: str) -> list[str]:
        artifact = self._store.get(artifact_id)
        if artifact is None:
            return []
        return list(artifact.lineage)

    def ancestors(self, artifact_id: str) -> list[str]:
        """Transitive parents via `Artifact.lineage`; deterministic (sorted) order."""
        seen: set[str] = set()
        stack = self._parents(artifact_id)
        while stack:
            parent_id = stack.pop()
            if parent_id in seen:
                continue
            seen.add(parent_id)
            stack.extend(self._parents(parent_id))
        return sorted(seen)

    def provenance(self, artifact_id: str) -> VersionTuple:
        artifact = self._store.get(artifact_id)
        if artifact is None:
            raise KeyError(f"unknown artifact_id: {artifact_id}")
        return artifact.version_tuple


class RefStore:
    """Named pointers to artifact ids, with full pointer history for rollback."""

    def __init__(self) -> None:
        self._current: dict[str, str] = {}
        self._history: dict[str, list[str]] = {}

    def set(self, name: str, artifact_id: str) -> None:
        self._current[name] = artifact_id
        self._history.setdefault(name, []).append(artifact_id)

    def get(self, name: str) -> str | None:
        return self._current.get(name)

    def rollback(self, name: str, artifact_id: str) -> None:
        """Repoint `name` at a historical artifact_id. The artifact itself is
        never deleted — only the pointer moves; the prior value stays in
        `history`.
        """
        self.set(name, artifact_id)

    def history(self, name: str) -> list[str]:
        return list(self._history.get(name, []))
