"""index exact Approval evidence Artifact ownership

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-19
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _regression_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError("0014 cannot decode regression evidence bindings") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise RuntimeError("0014 found invalid regression evidence bindings")
    if len(value) != len(set(value)):
        raise RuntimeError("0014 found duplicate regression evidence bindings")
    return tuple(value)


def upgrade() -> None:
    op.create_table(
        "approval_evidence_bindings",
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("approval_id", sa.String(), nullable=False),
        sa.Column("binding_kind", sa.String(), nullable=False),
        sa.CheckConstraint(
            "binding_kind IN ('validation', 'regression')",
            name="ck_approval_evidence_binding_kind",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approval_items.approval_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("artifact_id"),
    )

    approvals = sa.table(
        "approval_items",
        sa.column("approval_id", sa.String()),
        sa.column("evidence_set_artifact_id", sa.String()),
        sa.column("regression_evidence_artifact_ids", sa.JSON()),
    )
    bindings = sa.table(
        "approval_evidence_bindings",
        sa.column("artifact_id", sa.String()),
        sa.column("approval_id", sa.String()),
        sa.column("binding_kind", sa.String()),
    )
    rows = op.get_bind().execute(
        sa.select(
            approvals.c.approval_id,
            approvals.c.evidence_set_artifact_id,
            approvals.c.regression_evidence_artifact_ids,
        )
    )
    by_artifact: dict[str, tuple[str, str]] = {}
    for row in rows.mappings():
        approval_id = row["approval_id"]
        evidence_id = row["evidence_set_artifact_id"]
        candidates: list[tuple[str, str]] = []
        if evidence_id is not None:
            candidates.append((evidence_id, "validation"))
        candidates.extend(
            (artifact_id, "regression")
            for artifact_id in _regression_ids(row["regression_evidence_artifact_ids"])
        )
        for artifact_id, binding_kind in candidates:
            retained = by_artifact.get(artifact_id)
            candidate = (approval_id, binding_kind)
            if retained is not None and retained != candidate:
                raise RuntimeError("0014 found evidence bound to multiple ApprovalItems")
            by_artifact[artifact_id] = candidate
    if by_artifact:
        op.bulk_insert(
            bindings,
            [
                {
                    "artifact_id": artifact_id,
                    "approval_id": owner[0],
                    "binding_kind": owner[1],
                }
                for artifact_id, owner in sorted(by_artifact.items())
            ],
        )


def downgrade() -> None:
    op.drop_table("approval_evidence_bindings")
