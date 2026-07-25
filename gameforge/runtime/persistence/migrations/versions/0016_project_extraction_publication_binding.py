"""bind each human publication draft to its exact source extraction

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-25
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PATCH_FK = "fk_project_extractions_publication_patch_artifact"
_APPROVAL_FK = "fk_project_extractions_publication_approval"


def _payload(value: object) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError("0016 cannot decode a project extraction payload") from exc
    if not isinstance(value, dict):
        raise RuntimeError("0016 found a non-object project extraction payload")
    return dict(value)


def upgrade() -> None:
    with op.batch_alter_table("project_extractions", recreate="always") as batch:
        batch.add_column(sa.Column("publication_patch_artifact_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("publication_approval_id", sa.String(), nullable=True))
        batch.create_foreign_key(
            _PATCH_FK,
            "artifacts",
            ["publication_patch_artifact_id"],
            ["artifact_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            _APPROVAL_FK,
            "approval_items",
            ["publication_approval_id"],
            ["approval_id"],
            ondelete="RESTRICT",
        )

    extractions = sa.table(
        "project_extractions",
        sa.column("extraction_id", sa.String()),
        sa.column("publication_patch_artifact_id", sa.String()),
        sa.column("publication_approval_id", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(extractions.c.extraction_id, extractions.c.payload)
    ).mappings()
    for row in rows:
        payload = _payload(row["payload"])
        retained_patch = payload.get("publication_patch_artifact_id")
        retained_approval = payload.get("publication_approval_id")
        if retained_patch is not None or retained_approval is not None:
            raise RuntimeError("0016 found an unproven pre-existing publication binding")
        payload["publication_patch_artifact_id"] = None
        payload["publication_approval_id"] = None
        connection.execute(
            sa.update(extractions)
            .where(extractions.c.extraction_id == row["extraction_id"])
            .values(
                publication_patch_artifact_id=None,
                publication_approval_id=None,
                payload=payload,
            )
        )


def downgrade() -> None:
    extractions = sa.table(
        "project_extractions",
        sa.column("extraction_id", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(extractions.c.extraction_id, extractions.c.payload)
    ).mappings()
    for row in rows:
        payload = _payload(row["payload"])
        payload.pop("publication_patch_artifact_id", None)
        payload.pop("publication_approval_id", None)
        connection.execute(
            sa.update(extractions)
            .where(extractions.c.extraction_id == row["extraction_id"])
            .values(payload=payload)
        )

    with op.batch_alter_table("project_extractions", recreate="always") as batch:
        batch.drop_constraint(_APPROVAL_FK, type_="foreignkey")
        batch.drop_constraint(_PATCH_FK, type_="foreignkey")
        batch.drop_column("publication_approval_id")
        batch.drop_column("publication_patch_artifact_id")
