"""One project's pages never contain another project's content.

Every fixture in this repository creates ONE project, which is exactly why a planner
had to discover in a browser that the pages show every game at once. These tests exist
so a second project is present wherever the filter is decided.

They drive the page provider directly, because that is where the narrowing lives: the
filter is served by `project_artifacts`, the producer index written in the same
transaction as the Artifact. Content no project owns — the seeded catalog, bench
reports, DR drills — has no binding and so appears only in the unfiltered view, which
is the "all games" option the shell offers.
"""

from __future__ import annotations

from typing import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.identity import DomainScope
from gameforge.contracts.projects import GameProjectV1
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.project_artifacts import (
    SqlProjectArtifactBindingRepository,
)
from gameforge.runtime.persistence.projects import SqlProjectRepository
from tests.apps.api.test_content_persistence import (
    _artifact,
    _artifact_pages,
    _artifacts,
    _binding,
)

_ALPHA = "project:alpha"
_BETA = "project:beta"


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'project-scope.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _project(project_id: str, bootstrap_artifact_id: str) -> GameProjectV1:
    return GameProjectV1(
        project_id=project_id,
        project_key=project_id.removeprefix("project:"),
        display_name=project_id,
        status="draft",
        domain_scope=DomainScope(domain_ids=("builtin",)),
        bootstrap_snapshot_artifact_id=bootstrap_artifact_id,
        content_ref_name=f"projects/{project_id}/content/head",
        constraint_ref_name=f"projects/{project_id}/constraints/head",
        created_by="human:maker",
        created_at="2026-07-28T00:00:00Z",
        updated_at="2026-07-28T00:00:00Z",
        revision=1,
    )


def _seed_two_projects(engine: Engine) -> None:
    """Two games with their own content, plus one Artifact no project owns."""

    with Session(engine) as session, session.begin():
        repository = _artifacts(session)
        for artifact_id in ("ir:alpha", "ir:beta"):
            repository.put(_artifact(artifact_id, kind="ir_snapshot"))
        for artifact_id in ("config:alpha", "config:alpha2", "config:beta", "config:unowned"):
            repository.put(_artifact(artifact_id, kind="config_export"))
        projects = SqlProjectRepository(session)
        bindings = SqlProjectArtifactBindingRepository(session)
        for project_id, bootstrap, content in (
            (_ALPHA, "ir:alpha", ("config:alpha", "config:alpha2")),
            (_BETA, "ir:beta", ("config:beta",)),
        ):
            projects.create_project(_project(project_id, bootstrap))
            bindings.bind(
                project_id,
                (bootstrap, *content),
                bound_by="test",
                bound_at="2026-07-28T00:00:00Z",
            )


def _page(engine: Engine, project_id: str | None, *, page_size: int = 100):
    with Session(engine) as session, session.begin():
        return _artifact_pages(session).page(
            index_kind="artifacts",
            expected_artifact_kind="config_export",
            filters={"kind": "config_export", "project_id": project_id},
            cursor=None,
            binding=_binding(f"artifacts-config-export-{project_id or 'all'}"),
            page_size=page_size,
        )


def test_a_project_page_contains_only_that_project_s_content(engine: Engine) -> None:
    _seed_two_projects(engine)

    alpha = {item.artifact_id for item in _page(engine, _ALPHA).items}
    beta = {item.artifact_id for item in _page(engine, _BETA).items}
    everything = {item.artifact_id for item in _page(engine, None).items}

    assert alpha == {"config:alpha", "config:alpha2"}
    assert beta == {"config:beta"}
    assert alpha.isdisjoint(beta)
    # "All games" is a superset, and the only place unowned content is reachable.
    assert alpha | beta < everything
    assert "config:unowned" in everything - (alpha | beta)


def test_an_unknown_project_pages_empty_rather_than_failing(engine: Engine) -> None:
    _seed_two_projects(engine)

    page = _page(engine, "project:absent")

    assert page.items == ()
    assert page.next_cursor is None


def test_a_late_binding_stays_above_a_frozen_project_watermark(engine: Engine) -> None:
    """The detail that makes project pages append-only.

    Artifact storage deduplicates, so a second project binding content the first
    already published appends a binding row long after that Artifact's own rowid was
    frozen. Watermarking on the Artifact would surface it beneath a frozen watermark
    and break the page contract; watermarking on the binding excludes it.
    """

    _seed_two_projects(engine)
    first = _page(engine, _ALPHA, page_size=1)

    with Session(engine) as session, session.begin():
        SqlProjectArtifactBindingRepository(session).bind(
            _ALPHA,
            ("config:beta",),
            bound_by="test",
            bound_at="2026-07-28T01:00:00Z",
        )

    with Session(engine) as session, session.begin():
        continued = _artifact_pages(session).page(
            index_kind="artifacts",
            expected_artifact_kind="config_export",
            filters={"kind": "config_export", "project_id": _ALPHA},
            cursor=first.next_cursor,
            binding=_binding(f"artifacts-config-export-{_ALPHA}"),
            page_size=1,
        )

    assert [item.artifact_id for item in first.items] == ["config:alpha"]
    assert first.next_cursor is not None
    # `config:beta` sorts after `config:alpha2` and was bound after the page froze, so
    # the continuation must show only what the frozen watermark already covered.
    assert [item.artifact_id for item in continued.items] == ["config:alpha2"]


def test_other_filters_still_have_no_producer_index(engine: Engine) -> None:
    """`project_id` gained an index; nothing else did, and the refusal must hold."""

    from gameforge.contracts.errors import DependencyUnavailable

    _seed_two_projects(engine)
    with Session(engine) as session, session.begin():
        with pytest.raises(DependencyUnavailable, match="producer index"):
            _artifact_pages(session).page(
                index_kind="task_suites",
                expected_artifact_kind="task_suite",
                filters={"config_artifact_id": "config:alpha", "project_id": None},
                cursor=None,
                binding=_binding("task-suites"),
                page_size=10,
            )
