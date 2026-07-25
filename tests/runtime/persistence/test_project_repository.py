from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.projects import GameProjectV1, ProjectMaterialV1
from gameforge.runtime.persistence.models import ArtifactRow, Base
from gameforge.runtime.persistence.projects import SqlProjectRepository


NOW = "2026-07-24T00:00:00Z"


def _artifact(artifact_id: str, kind: str) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=artifact_id,
        lineage_schema_version="lineage@2",
        kind=kind,
        version_tuple={},
        lineage=[],
        payload_hash="0" * 64,
        created_at=NOW,
        meta={},
        object_ref={
            "object_ref_schema_version": "object-ref@1",
            "key": "objects/v1/sha256/00/" + "0" * 64,
            "sha256": "0" * 64,
            "size_bytes": 1,
        },
    )


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                _artifact("artifact:bootstrap", "ir_snapshot"),
                _artifact("artifact:raw", "source_raw"),
                _artifact("artifact:rendered", "source_rendered"),
            ]
        )
        session.flush()
        yield session
        session.rollback()
    engine.dispose()


def _project(**changes: object) -> GameProjectV1:
    values: dict[str, object] = {
        "project_id": "project:p1",
        "project_key": "sky-harbor",
        "display_name": "天空港",
        "description": "浮空城经营 RPG",
        "genre": "RPG",
        "status": "draft",
        "domain_scope": DomainScope(domain_ids=("domain:narrative",)),
        "bootstrap_snapshot_artifact_id": "artifact:bootstrap",
        "content_ref_name": "projects/project:p1/content/head",
        "constraint_ref_name": "projects/project:p1/constraints/head",
        "current_content_ref": None,
        "current_constraint_ref": None,
        "created_by": "human:maker",
        "created_at": NOW,
        "updated_at": NOW,
        "revision": 1,
    }
    values.update(changes)
    project_id = str(values["project_id"])
    if "content_ref_name" not in changes:
        values["content_ref_name"] = f"projects/{project_id}/content/head"
    if "constraint_ref_name" not in changes:
        values["constraint_ref_name"] = f"projects/{project_id}/constraints/head"
    return GameProjectV1.model_validate(values)


def _material(**changes: object) -> ProjectMaterialV1:
    values: dict[str, object] = {
        "material_id": "material:m1",
        "project_id": "project:p1",
        "display_name": "世界观",
        "media_type": "text/markdown",
        "source_format": "markdown",
        "original_source_artifact_id": "artifact:raw",
        "rendered_source_artifact_id": "artifact:rendered",
        "parser_id": "planning-material-markdown",
        "parser_version": "1",
        "parse_status": "ready",
        "parse_warnings": (),
        "byte_size": 12,
        "text_char_count": 6,
        "created_by": "human:maker",
        "created_at": NOW,
        "status": "active",
        "revision": 1,
    }
    values.update(changes)
    return ProjectMaterialV1.model_validate(values)


def test_project_repository_round_trips_and_enforces_key_identity(session: Session) -> None:
    repository = SqlProjectRepository(session)
    expected = _project()

    assert repository.create_project(expected) == expected
    assert repository.get_project(expected.project_id) == expected
    assert repository.get_project_by_key(expected.project_key) == expected
    assert repository.list_projects(limit=10) == (expected,)

    with pytest.raises(Conflict):
        repository.create_project(_project(project_id="project:p2"))


def test_project_repository_compare_and_set_is_monotonic(session: Session) -> None:
    repository = SqlProjectRepository(session)
    current = repository.create_project(_project())
    replacement = current.model_copy(
        update={"display_name": "天空港：重制", "revision": 2, "updated_at": "2026-07-24T00:00:01Z"}
    )

    assert repository.compare_and_set_project(current.project_id, 1, replacement) == replacement
    with pytest.raises(Conflict):
        repository.compare_and_set_project(current.project_id, 1, replacement)


def test_material_repository_round_trips_lists_and_archives_by_cas(session: Session) -> None:
    repository = SqlProjectRepository(session)
    repository.create_project(_project())
    material = _material()

    assert repository.create_material(material) == material
    assert repository.get_material(material.material_id) == material
    assert repository.list_materials(project_id="project:p1", limit=10) == (material,)

    archived = material.model_copy(update={"status": "archived", "revision": 2})
    assert repository.compare_and_set_material(material.material_id, 1, archived) == archived


def test_repository_rejects_noncanonical_or_cross_project_payload(session: Session) -> None:
    repository = SqlProjectRepository(session)
    repository.create_project(_project())

    with pytest.raises(IntegrityViolation):
        repository.create_material(_material(project_id="project:missing"))
