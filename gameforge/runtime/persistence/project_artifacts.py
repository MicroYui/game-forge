"""Which project an Artifact belongs to, recorded at the moment it is published.

This is the producer index the project filter reads. It is deliberately a sibling
write rather than a column on `artifacts`: Artifact storage is content-addressed and
deduplicating, so two projects publishing byte-identical content resolve to one row,
and a single-valued column would keep whichever wrote first and call that the owner.

`bind` is idempotent because publication is contractually re-drivable — a retried
publish must record the same membership rather than fail.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from gameforge.runtime.persistence.models import ProjectArtifactRow


class SqlProjectArtifactBindingRepository:
    """Record and read project membership for Artifacts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def bind(
        self,
        project_id: str,
        artifact_ids: Iterable[str],
        *,
        bound_by: str,
        bound_at: str,
    ) -> None:
        rows = [
            {
                "project_id": project_id,
                "artifact_id": artifact_id,
                "bound_at": bound_at,
                "bound_by": bound_by,
            }
            for artifact_id in sorted(set(artifact_ids))
        ]
        if not rows:
            return
        self._session.execute(
            sqlite_insert(ProjectArtifactRow).on_conflict_do_nothing(
                index_elements=[
                    ProjectArtifactRow.project_id,
                    ProjectArtifactRow.artifact_id,
                ]
            ),
            rows,
        )

    def projects_for(self, artifact_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                self._session.scalars(
                    select(ProjectArtifactRow.project_id).where(
                        ProjectArtifactRow.artifact_id == artifact_id
                    )
                ).all()
            )
        )

    def projects_for_many(self, artifact_ids: Sequence[str]) -> Mapping[str, tuple[str, ...]]:
        if not artifact_ids:
            return {}
        owners: dict[str, set[str]] = {}
        for artifact_id, project_id in self._session.execute(
            select(ProjectArtifactRow.artifact_id, ProjectArtifactRow.project_id).where(
                ProjectArtifactRow.artifact_id.in_(sorted(set(artifact_ids)))
            )
        ):
            owners.setdefault(artifact_id, set()).add(project_id)
        return {artifact_id: tuple(sorted(values)) for artifact_id, values in owners.items()}


__all__ = ["SqlProjectArtifactBindingRepository"]
