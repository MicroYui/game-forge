"""record which game an Artifact and a Run belong to

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-28

A planner working on one game saw every game's content in one list, because
nothing recorded which game anything belonged to. Only `game_projects`,
`project_materials`, `project_extractions` and `project_identity_aliases` carried
a project id; `artifacts`, `runs` and `refs` carried none, and the immutable read
index refuses any filter it has no producer index for.

`project_artifacts` is many-to-many because Artifact storage is content-addressed
and deduplicating: two projects publishing byte-identical content resolve to one
row, and a single-valued column would keep whichever wrote first and call that
the owner. Runs are the opposite — a run id is minted per request and belongs to
exactly one project — so a column is the honest shape there.

Rows retained from before this revision get their membership from the backfill below.
Content no project owns — the seeded catalog, bench
artifacts, DR drills — keeps no binding and a NULL project, which is a real answer
rather than a missing one.
"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0019"
down_revision: Union[str, Sequence[str], None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_artifacts",
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("bound_at", sa.String(), nullable=False),
        sa.Column("bound_by", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "artifact_id"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["game_projects.project_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
    )
    # The composite primary key already serves the page order the immutable index
    # reads; this covers the other direction, "which projects own this Artifact".
    op.create_index(
        "ix_project_artifacts_artifact_project",
        "project_artifacts",
        ["artifact_id", "project_id"],
        unique=False,
    )

    op.add_column("runs", sa.Column("project_id", sa.String(), nullable=True))
    op.create_index(
        "ix_runs_project_created",
        "runs",
        ["project_id", "created_at", "run_id"],
        unique=False,
    )
    op.create_index(
        "ix_run_finding_links_finding",
        "run_finding_links",
        ["finding_id", "run_id"],
        unique=False,
    )

    _backfill(op.get_bind())


_BOUND_BY = "migration:0019"
# A DAG walk with a cap rather than a `while True`: a lineage cycle is impossible by
# content addressing, but a migration that could not terminate is worse than one that
# refuses to.
_MAX_CLOSURE_ROUNDS = 64


def _seed_bindings(connection) -> None:
    """Phase A — every project membership some table already records, verbatim.

    No inference: each statement copies a fact a project authority wrote down. The
    ref-name statements matter most, because they cover a project's whole published
    content and constraint history exactly.
    """

    statements = (
        # The project head's own Artifacts.
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT project_id, bootstrap_snapshot_artifact_id, updated_at, :bound_by
        FROM game_projects
        """,
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT project_id, latest_patch_artifact_id, updated_at, :bound_by
        FROM game_projects WHERE latest_patch_artifact_id IS NOT NULL
        """,
        # Planning material, original and parsed.
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT project_id, original_source_artifact_id, created_at, :bound_by
        FROM project_materials
        """,
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT project_id, rendered_source_artifact_id, created_at, :bound_by
        FROM project_materials
        """,
        # Extraction products.
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT project_id, patch_artifact_id, created_at, :bound_by
        FROM project_extractions WHERE patch_artifact_id IS NOT NULL
        """,
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT project_id, preview_snapshot_artifact_id, created_at, :bound_by
        FROM project_extractions WHERE preview_snapshot_artifact_id IS NOT NULL
        """,
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT project_id, publication_patch_artifact_id, created_at, :bound_by
        FROM project_extractions WHERE publication_patch_artifact_id IS NOT NULL
        """,
        # What the Artifact itself recorded at publication.
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT json_extract(meta, '$.project_id'), artifact_id, created_at, :bound_by
        FROM artifacts
        WHERE json_extract(meta, '$.project_id') IN (SELECT project_id FROM game_projects)
        """,
        # Every published head, and every revision it ever had. `GameProjectV1` pins
        # the name, so the name identifies the owner exactly.
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT p.project_id, r.artifact_id, p.updated_at, :bound_by
        FROM refs r JOIN game_projects p
          ON r.name IN (p.content_ref_name, p.constraint_ref_name)
        """,
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT p.project_id, h.artifact_id, p.updated_at, :bound_by
        FROM ref_history h JOIN game_projects p
          ON h.name IN (p.content_ref_name, p.constraint_ref_name)
        """,
        # Approval subjects aimed at a project's namespace.
        """
        INSERT OR IGNORE INTO project_artifacts (project_id, artifact_id, bound_at, bound_by)
        SELECT p.project_id, a.subject_artifact_id, a.created_at, :bound_by
        FROM approval_items a JOIN game_projects p
          ON json_extract(a.target_binding, '$.ref_name')
             IN (p.content_ref_name, p.constraint_ref_name)
        """,
        # The extraction Run itself.
        """
        UPDATE runs SET project_id = (
            SELECT e.project_id FROM project_extractions e WHERE e.run_id = runs.run_id
        )
        WHERE project_id IS NULL
          AND run_id IN (SELECT run_id FROM project_extractions)
        """,
    )
    for statement in statements:
        connection.execute(sa.text(statement), {"bound_by": _BOUND_BY})


def _close_over_lineage(connection) -> None:
    """Phase B — an Artifact belongs to a project when one of its ancestors does.

    Phase A cannot reach review reports, finding evidence, playtest traces or config
    exports, and those are most of what the review and version pages show. `lineage`
    is immutable and names exact parents, so this is a derivation from retained
    authority rather than a guess — and it is one-time: after this revision every
    write records the membership directly, so nothing runs a closure again.
    """

    lineage_rows = connection.execute(
        sa.text("SELECT artifact_id, lineage, created_at FROM artifacts")
    ).fetchall()
    parents = {
        artifact_id: tuple(json.loads(lineage or "[]"))
        for artifact_id, lineage, _ in lineage_rows
    }
    created = {artifact_id: created_at for artifact_id, _, created_at in lineage_rows}

    owners: dict[str, set[str]] = {}
    for project_id, artifact_id in connection.execute(
        sa.text("SELECT project_id, artifact_id FROM project_artifacts")
    ).fetchall():
        owners.setdefault(artifact_id, set()).add(project_id)

    for _ in range(_MAX_CLOSURE_ROUNDS):
        grew = False
        for artifact_id, parent_ids in parents.items():
            inherited = {owner for parent in parent_ids for owner in owners.get(parent, ())}
            if inherited - owners.get(artifact_id, set()):
                owners.setdefault(artifact_id, set()).update(inherited)
                grew = True
        if not grew:
            break
    else:
        raise RuntimeError("project lineage closure did not settle within its round budget")

    rows = [
        {
            "project_id": project_id,
            "artifact_id": artifact_id,
            "bound_at": created.get(artifact_id, ""),
            "bound_by": _BOUND_BY,
        }
        for artifact_id, project_ids in sorted(owners.items())
        for project_id in sorted(project_ids)
    ]
    if rows:
        connection.execute(
            sa.text(
                "INSERT OR IGNORE INTO project_artifacts"
                " (project_id, artifact_id, bound_at, bound_by)"
                " VALUES (:project_id, :artifact_id, :bound_at, :bound_by)"
            ),
            rows,
        )


def _attribute_runs(connection) -> None:
    """A retained Run belongs to the one project covering every Artifact it consumed.

    Runs whose inputs span more than one project keep NULL. Historically that can only
    arise from the ref-ownership defect fixed in `d959cac2`, and guessing an owner
    would be worse than saying nothing.
    """

    owners: dict[str, set[str]] = {}
    for project_id, artifact_id in connection.execute(
        sa.text("SELECT project_id, artifact_id FROM project_artifacts")
    ).fetchall():
        owners.setdefault(artifact_id, set()).add(project_id)

    updates = []
    for run_id, payload in connection.execute(
        sa.text("SELECT run_id, payload FROM runs WHERE project_id IS NULL")
    ).fetchall():
        try:
            inputs = json.loads(payload or "{}").get("input_artifact_ids") or []
        except (TypeError, ValueError):
            continue
        candidates = {owner for item in inputs for owner in owners.get(item, ())}
        if len(candidates) == 1:
            updates.append({"run_id": run_id, "project_id": next(iter(candidates))})
    if updates:
        connection.execute(
            sa.text("UPDATE runs SET project_id = :project_id WHERE run_id = :run_id"),
            updates,
        )


def _backfill(connection) -> None:
    _seed_bindings(connection)
    _close_over_lineage(connection)
    _attribute_runs(connection)


def downgrade() -> None:
    connection = op.get_bind()
    # Project membership is retained authority: a downgrade that silently dropped it
    # would leave a workspace whose lists cannot say which game anything belongs to.
    if connection.execute(sa.text("SELECT 1 FROM project_artifacts LIMIT 1")).first():
        raise RuntimeError("cannot remove retained project Artifact membership")
    if connection.execute(
        sa.text("SELECT 1 FROM runs WHERE project_id IS NOT NULL LIMIT 1")
    ).first():
        raise RuntimeError("cannot remove retained Run project membership")

    op.drop_index("ix_run_finding_links_finding", table_name="run_finding_links")
    op.drop_index("ix_runs_project_created", table_name="runs")
    op.drop_column("runs", "project_id")
    op.drop_index("ix_project_artifacts_artifact_project", table_name="project_artifacts")
    op.drop_table("project_artifacts")
