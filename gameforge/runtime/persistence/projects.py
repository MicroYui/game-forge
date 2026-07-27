"""Transaction-bound persistence for project-first authoring resources."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation, QueryTooBroad
from gameforge.contracts.projects import (
    GameProjectV1,
    ProjectExtractionV1,
    ProjectIdentityAliasV1,
    ProjectMaterialV1,
)
from gameforge.runtime.persistence.models import (
    ArtifactRow,
    GameProjectRow,
    ProjectExtractionRow,
    ProjectIdentityAliasRow,
    ProjectMaterialRow,
    RunRow,
)


_ModelT = TypeVar("_ModelT", bound=BaseModel)
MAX_PROJECT_QUERY_ITEMS = 1000


def _canonical(value: object, model_type: type[_ModelT], *, label: str) -> _ModelT:
    if type(value) is not model_type:
        raise IntegrityViolation(f"{label} requires an exact {model_type.__name__}")
    try:
        wire = value.model_dump(mode="json")  # type: ignore[union-attr]
        parsed = model_type.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"{label} payload is invalid") from exc
    if canonical_json(wire) != canonical_json(parsed.model_dump(mode="json")):
        raise IntegrityViolation(f"{label} payload is noncanonical")
    return parsed


def _parse(payload: object, model_type: type[_ModelT], *, label: str, identity: str) -> _ModelT:
    if not isinstance(payload, dict):
        raise IntegrityViolation(f"stored {label} payload is not an object", identity=identity)
    try:
        parsed = model_type.model_validate(payload)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"stored {label} payload is invalid", identity=identity) from exc
    if canonical_json(payload) != canonical_json(parsed.model_dump(mode="json")):
        raise IntegrityViolation(f"stored {label} payload is noncanonical", identity=identity)
    return parsed


def _limit(value: int) -> int:
    if isinstance(value, bool) or not 1 <= value <= MAX_PROJECT_QUERY_ITEMS:
        raise QueryTooBroad("project query limit is outside the supported range")
    return value


class SqlProjectRepository:
    """Persist only project mappings; immutable payloads stay in Artifact storage."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_project(self, project: GameProjectV1) -> GameProjectV1:
        canonical = _canonical(project, GameProjectV1, label="project")
        existing = self.get_project(canonical.project_id)
        if existing is not None:
            if existing == canonical:
                return existing
            raise Conflict("project id already exists", project_id=canonical.project_id)
        key_owner = self.get_project_by_key(canonical.project_key)
        if key_owner is not None:
            raise Conflict("project key already exists", project_key=canonical.project_key)
        artifact = self._session.get(ArtifactRow, canonical.bootstrap_snapshot_artifact_id)
        if artifact is None or artifact.kind != "ir_snapshot":
            raise IntegrityViolation(
                "project bootstrap Artifact is unavailable or not an ir_snapshot"
            )
        wire = canonical.model_dump(mode="json")
        self._session.add(GameProjectRow(**self._project_values(wire)))
        self._flush("project", project_id=canonical.project_id)
        return canonical

    def get_project(self, project_id: str) -> GameProjectV1 | None:
        row = self._session.get(GameProjectRow, project_id)
        return None if row is None else self._project_from_row(row)

    def get_project_by_key(self, project_key: str) -> GameProjectV1 | None:
        row = self._session.scalar(
            select(GameProjectRow).where(GameProjectRow.project_key == project_key)
        )
        return None if row is None else self._project_from_row(row)

    def list_projects(
        self,
        *,
        limit: int,
        status: str | None = None,
        after: tuple[str, str] | None = None,
    ) -> tuple[GameProjectV1, ...]:
        statement = select(GameProjectRow)
        if status is not None:
            statement = statement.where(GameProjectRow.status == status)
        if after is not None:
            updated_at, project_id = after
            statement = statement.where(
                or_(
                    GameProjectRow.updated_at < updated_at,
                    and_(
                        GameProjectRow.updated_at == updated_at,
                        GameProjectRow.project_id > project_id,
                    ),
                )
            )
        rows = self._session.scalars(
            statement.order_by(GameProjectRow.updated_at.desc(), GameProjectRow.project_id).limit(
                _limit(limit)
            )
        ).all()
        return tuple(self._project_from_row(row) for row in rows)

    def compare_and_set_project(
        self,
        project_id: str,
        expected_revision: int,
        replacement: GameProjectV1,
    ) -> GameProjectV1:
        canonical = _canonical(replacement, GameProjectV1, label="project replacement")
        if canonical.project_id != project_id or canonical.revision != expected_revision + 1:
            raise IntegrityViolation("project replacement identity or revision is invalid")
        wire = canonical.model_dump(mode="json")
        result = self._session.execute(
            update(GameProjectRow)
            .where(
                GameProjectRow.project_id == project_id,
                GameProjectRow.revision == expected_revision,
            )
            .values(**self._project_values(wire))
        )
        if result.rowcount != 1:
            raise Conflict("project revision differs", project_id=project_id)
        return canonical

    def create_material(self, material: ProjectMaterialV1) -> ProjectMaterialV1:
        canonical = _canonical(material, ProjectMaterialV1, label="project material")
        if self.get_project(canonical.project_id) is None:
            raise IntegrityViolation("project material references an unavailable project")
        for artifact_id, kind in (
            (canonical.original_source_artifact_id, "source_raw"),
            (canonical.rendered_source_artifact_id, "source_rendered"),
        ):
            artifact = self._session.get(ArtifactRow, artifact_id)
            if artifact is None or artifact.kind != kind:
                raise IntegrityViolation("project material source Artifact is unavailable or wrong")
        existing = self.get_material(canonical.material_id)
        if existing is not None:
            if existing == canonical:
                return existing
            raise Conflict("material id already exists", material_id=canonical.material_id)
        wire = canonical.model_dump(mode="json")
        self._session.add(ProjectMaterialRow(**self._material_values(wire)))
        self._flush("project material", material_id=canonical.material_id)
        return canonical

    def get_material(self, material_id: str) -> ProjectMaterialV1 | None:
        row = self._session.get(ProjectMaterialRow, material_id)
        return None if row is None else self._material_from_row(row)

    def list_materials(
        self,
        *,
        project_id: str,
        limit: int,
        status: str | None = None,
    ) -> tuple[ProjectMaterialV1, ...]:
        statement = select(ProjectMaterialRow).where(ProjectMaterialRow.project_id == project_id)
        if status is not None:
            statement = statement.where(ProjectMaterialRow.status == status)
        rows = self._session.scalars(
            statement.order_by(ProjectMaterialRow.created_at, ProjectMaterialRow.material_id).limit(
                _limit(limit)
            )
        ).all()
        return tuple(self._material_from_row(row) for row in rows)

    def compare_and_set_material(
        self,
        material_id: str,
        expected_revision: int,
        replacement: ProjectMaterialV1,
    ) -> ProjectMaterialV1:
        canonical = _canonical(replacement, ProjectMaterialV1, label="material replacement")
        if canonical.material_id != material_id or canonical.revision != expected_revision + 1:
            raise IntegrityViolation("material replacement identity or revision is invalid")
        wire = canonical.model_dump(mode="json")
        result = self._session.execute(
            update(ProjectMaterialRow)
            .where(
                ProjectMaterialRow.material_id == material_id,
                ProjectMaterialRow.revision == expected_revision,
            )
            .values(**self._material_values(wire))
        )
        if result.rowcount != 1:
            raise Conflict("material revision differs", material_id=material_id)
        return canonical

    def create_identity_alias(self, alias: ProjectIdentityAliasV1) -> ProjectIdentityAliasV1:
        canonical = _canonical(alias, ProjectIdentityAliasV1, label="project identity alias")
        if self.get_project(canonical.project_id) is None:
            raise IntegrityViolation("identity alias references an unavailable project")
        existing = self.get_identity_alias(canonical.alias_id)
        if existing is not None:
            if existing == canonical:
                return existing
            raise Conflict("identity alias already exists", alias_id=canonical.alias_id)
        self._session.add(
            ProjectIdentityAliasRow(
                alias_id=canonical.alias_id,
                project_id=canonical.project_id,
                alias=canonical.alias,
                canonical_alias=canonical.canonical_alias,
                canonical_entity_id=canonical.canonical_entity_id,
                declared_by=canonical.declared_by,
                declared_at=canonical.declared_at,
                status=canonical.status,
                revision=canonical.revision,
            )
        )
        self._flush("project identity alias", alias_id=canonical.alias_id)
        return canonical

    def get_identity_alias(self, alias_id: str) -> ProjectIdentityAliasV1 | None:
        row = self._session.get(ProjectIdentityAliasRow, alias_id)
        return None if row is None else self._identity_alias_from_row(row)

    def list_identity_aliases(
        self,
        *,
        project_id: str,
        limit: int,
        status: str | None = None,
    ) -> tuple[ProjectIdentityAliasV1, ...]:
        statement = select(ProjectIdentityAliasRow).where(
            ProjectIdentityAliasRow.project_id == project_id
        )
        if status is not None:
            statement = statement.where(ProjectIdentityAliasRow.status == status)
        rows = self._session.scalars(
            statement.order_by(ProjectIdentityAliasRow.alias_id).limit(_limit(limit))
        ).all()
        return tuple(self._identity_alias_from_row(row) for row in rows)

    def compare_and_set_identity_alias(
        self,
        alias_id: str,
        expected_revision: int,
        replacement: ProjectIdentityAliasV1,
    ) -> ProjectIdentityAliasV1:
        canonical = _canonical(replacement, ProjectIdentityAliasV1, label="alias replacement")
        if canonical.alias_id != alias_id or canonical.revision != expected_revision + 1:
            raise IntegrityViolation("identity alias replacement identity or revision is invalid")
        result = self._session.execute(
            update(ProjectIdentityAliasRow)
            .where(
                ProjectIdentityAliasRow.alias_id == alias_id,
                ProjectIdentityAliasRow.revision == expected_revision,
            )
            .values(
                alias=canonical.alias,
                canonical_alias=canonical.canonical_alias,
                canonical_entity_id=canonical.canonical_entity_id,
                status=canonical.status,
                revision=canonical.revision,
            )
        )
        if result.rowcount != 1:
            raise Conflict("identity alias revision differs", alias_id=alias_id)
        return canonical

    @staticmethod
    def _identity_alias_from_row(row: ProjectIdentityAliasRow) -> ProjectIdentityAliasV1:
        return ProjectIdentityAliasV1(
            alias_id=row.alias_id,
            project_id=row.project_id,
            alias=row.alias,
            canonical_alias=row.canonical_alias,
            canonical_entity_id=row.canonical_entity_id,
            declared_by=row.declared_by,
            declared_at=row.declared_at,
            status=row.status,  # type: ignore[arg-type]
            revision=row.revision,
        )

    def create_extraction(self, extraction: ProjectExtractionV1) -> ProjectExtractionV1:
        canonical = _canonical(extraction, ProjectExtractionV1, label="project extraction")
        if self.get_project(canonical.project_id) is None:
            raise IntegrityViolation("project extraction references an unavailable project")
        if self._session.get(RunRow, canonical.run_id) is None:
            raise IntegrityViolation("project extraction references an unavailable Run")
        existing = self.get_extraction(canonical.extraction_id)
        if existing is not None:
            if existing == canonical:
                return existing
            raise Conflict("extraction id already exists", extraction_id=canonical.extraction_id)
        wire = canonical.model_dump(mode="json")
        self._session.add(ProjectExtractionRow(**self._extraction_values(wire)))
        self._flush("project extraction", extraction_id=canonical.extraction_id)
        return canonical

    def get_extraction(self, extraction_id: str) -> ProjectExtractionV1 | None:
        row = self._session.get(ProjectExtractionRow, extraction_id)
        return None if row is None else self._extraction_from_row(row)

    def list_extractions(
        self,
        *,
        project_id: str,
        limit: int,
    ) -> tuple[ProjectExtractionV1, ...]:
        rows = self._session.scalars(
            select(ProjectExtractionRow)
            .where(ProjectExtractionRow.project_id == project_id)
            .order_by(
                ProjectExtractionRow.created_at.desc(),
                ProjectExtractionRow.extraction_id.desc(),
            )
            .limit(_limit(limit))
        ).all()
        return tuple(self._extraction_from_row(row) for row in rows)

    def compare_and_set_extraction(
        self,
        extraction_id: str,
        expected_revision: int,
        replacement: ProjectExtractionV1,
    ) -> ProjectExtractionV1:
        canonical = _canonical(replacement, ProjectExtractionV1, label="extraction replacement")
        if canonical.extraction_id != extraction_id or canonical.revision != expected_revision + 1:
            raise IntegrityViolation("extraction replacement identity or revision is invalid")
        wire = canonical.model_dump(mode="json")
        result = self._session.execute(
            update(ProjectExtractionRow)
            .where(
                ProjectExtractionRow.extraction_id == extraction_id,
                ProjectExtractionRow.revision == expected_revision,
            )
            .values(**self._extraction_values(wire))
        )
        if result.rowcount != 1:
            raise Conflict("extraction revision differs", extraction_id=extraction_id)
        return canonical

    @staticmethod
    def _project_values(wire: dict) -> dict:
        return {
            "project_id": wire["project_id"],
            "project_key": wire["project_key"],
            "display_name": wire["display_name"],
            "status": wire["status"],
            "domain_scope": wire["domain_scope"],
            "bootstrap_snapshot_artifact_id": wire["bootstrap_snapshot_artifact_id"],
            "content_ref_name": wire["content_ref_name"],
            "constraint_ref_name": wire["constraint_ref_name"],
            "latest_extraction_id": wire["latest_extraction_id"],
            "latest_patch_artifact_id": wire["latest_patch_artifact_id"],
            "latest_approval_id": wire["latest_approval_id"],
            "created_by": wire["created_by"],
            "created_at": wire["created_at"],
            "updated_at": wire["updated_at"],
            "revision": wire["revision"],
            "payload": wire,
        }

    @staticmethod
    def _material_values(wire: dict) -> dict:
        return {
            "material_id": wire["material_id"],
            "project_id": wire["project_id"],
            "display_name": wire["display_name"],
            "source_format": wire["source_format"],
            "status": wire["status"],
            "original_source_artifact_id": wire["original_source_artifact_id"],
            "rendered_source_artifact_id": wire["rendered_source_artifact_id"],
            "created_by": wire["created_by"],
            "created_at": wire["created_at"],
            "revision": wire["revision"],
            "payload": wire,
        }

    @staticmethod
    def _extraction_values(wire: dict) -> dict:
        return {
            "extraction_id": wire["extraction_id"],
            "project_id": wire["project_id"],
            "run_id": wire["run_id"],
            "status": wire["status"],
            "patch_artifact_id": wire["patch_artifact_id"],
            "preview_snapshot_artifact_id": wire["preview_snapshot_artifact_id"],
            "approval_id": wire["approval_id"],
            "publication_patch_artifact_id": wire["publication_patch_artifact_id"],
            "publication_approval_id": wire["publication_approval_id"],
            "created_by": wire["created_by"],
            "created_at": wire["created_at"],
            "updated_at": wire["updated_at"],
            "revision": wire["revision"],
            "payload": wire,
        }

    @staticmethod
    def _project_from_row(row: GameProjectRow) -> GameProjectV1:
        value = _parse(row.payload, GameProjectV1, label="project", identity=row.project_id)
        expected = SqlProjectRepository._project_values(value.model_dump(mode="json"))
        for field, projected in expected.items():
            if field != "payload" and getattr(row, field) != projected:
                raise IntegrityViolation("stored project projection differs", field=field)
        return value

    @staticmethod
    def _material_from_row(row: ProjectMaterialRow) -> ProjectMaterialV1:
        value = _parse(
            row.payload,
            ProjectMaterialV1,
            label="project material",
            identity=row.material_id,
        )
        expected = SqlProjectRepository._material_values(value.model_dump(mode="json"))
        for field, projected in expected.items():
            if field != "payload" and getattr(row, field) != projected:
                raise IntegrityViolation("stored material projection differs", field=field)
        return value

    @staticmethod
    def _extraction_from_row(row: ProjectExtractionRow) -> ProjectExtractionV1:
        value = _parse(
            row.payload,
            ProjectExtractionV1,
            label="project extraction",
            identity=row.extraction_id,
        )
        expected = SqlProjectRepository._extraction_values(value.model_dump(mode="json"))
        for field, projected in expected.items():
            if field != "payload" and getattr(row, field) != projected:
                raise IntegrityViolation("stored extraction projection differs", field=field)
        return value

    def _flush(self, label: str, **context: str) -> None:
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise Conflict(f"{label} write conflicts with retained authority", **context) from exc


__all__ = ["MAX_PROJECT_QUERY_ITEMS", "SqlProjectRepository"]
