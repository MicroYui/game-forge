"""Immutable transaction-bound persistence for ref pointer transitions."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import typed_canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.storage import RefTransitionV1
from gameforge.runtime.persistence.models import RefTransitionRow


def _require_nonempty(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityViolation(f"{field_name} must be a non-empty string")
    return value


def _row_wire(row: RefTransitionRow) -> dict[str, Any]:
    return {
        "transition_schema_version": row.transition_schema_version,
        "transition_id": row.transition_id,
        "kind": row.kind,
        "ref_name": row.ref_name,
        "from_ref": {
            "artifact_id": row.from_artifact_id,
            "revision": row.from_revision,
        },
        "to_ref": {
            "artifact_id": row.to_artifact_id,
            "revision": row.to_revision,
        },
        "approval_item_id": row.approval_item_id,
        "actor": row.actor,
        "initiated_by": row.initiated_by,
        "request_id": row.request_id,
        "occurred_at": row.occurred_at,
    }


def _parse_stored_row(
    row: RefTransitionRow,
    *,
    expected_transition_id: str,
) -> RefTransitionV1:
    wire = _row_wire(row)
    try:
        if row.transition_id != expected_transition_id:
            raise ValueError("RefTransition storage key differs from its row")
        parsed = RefTransitionV1.model_validate(wire)
        if typed_canonical_json(parsed.model_dump(mode="python")) != typed_canonical_json(
            wire
        ):
            raise ValueError("RefTransition row is not canonical")
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored RefTransition is invalid",
            transition_id=expected_transition_id,
        ) from exc
    return parsed


def _revalidate_for_put(item: RefTransitionV1) -> RefTransitionV1:
    if type(item) is not RefTransitionV1 or set(item.__dict__) != set(
        RefTransitionV1.model_fields
    ):
        raise IntegrityViolation("ref transition put requires canonical RefTransitionV1 wire")
    wire = item.model_dump(mode="python")
    try:
        parsed = RefTransitionV1.model_validate(wire)
        if typed_canonical_json(parsed.model_dump(mode="python")) != typed_canonical_json(
            wire
        ):
            raise ValueError("RefTransition wire is not canonical")
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "ref transition wire is invalid",
            transition_id=getattr(item, "transition_id", None),
        ) from exc
    return parsed


def _row_values(item: RefTransitionV1) -> dict[str, Any]:
    wire = item.model_dump(mode="json")
    return {
        "transition_schema_version": wire["transition_schema_version"],
        "transition_id": wire["transition_id"],
        "kind": wire["kind"],
        "ref_name": wire["ref_name"],
        "from_artifact_id": wire["from_ref"]["artifact_id"],
        "from_revision": wire["from_ref"]["revision"],
        "to_artifact_id": wire["to_ref"]["artifact_id"],
        "to_revision": wire["to_ref"]["revision"],
        "approval_item_id": wire["approval_item_id"],
        "actor": wire["actor"],
        "initiated_by": wire["initiated_by"],
        "request_id": wire["request_id"],
        "occurred_at": wire["occurred_at"],
    }


def _same_wire(left: RefTransitionV1, right: RefTransitionV1) -> bool:
    return typed_canonical_json(left.model_dump(mode="python")) == typed_canonical_json(
        right.model_dump(mode="python")
    )


class SqlRefTransitionRepository:
    """Persist append-only ``RefTransitionV1`` records in an owned SQLite UoW."""

    def __init__(self, session: Session) -> None:
        if session.get_bind().dialect.name != "sqlite":
            raise ValueError("SqlRefTransitionRepository requires a SQLite session")
        self._session = session

    def get(self, transition_id: str) -> RefTransitionV1 | None:
        identifier = _require_nonempty(transition_id, field_name="transition_id")
        row = self._session.get(RefTransitionRow, identifier)
        if row is None:
            return None
        return _parse_stored_row(row, expected_transition_id=identifier)

    def put(self, item: RefTransitionV1) -> RefTransitionV1:
        candidate = _revalidate_for_put(item)
        existing = self.get(candidate.transition_id)
        if existing is not None:
            if _same_wire(existing, candidate):
                return existing
            raise IntegrityViolation(
                "ref transition id is already bound to different immutable content",
                transition_id=candidate.transition_id,
            )

        try:
            result = self._session.execute(
                sqlite_insert(RefTransitionRow)
                .values(**_row_values(candidate))
                .on_conflict_do_nothing(index_elements=[RefTransitionRow.transition_id])
            )
        except IntegrityError as exc:
            raise IntegrityViolation(
                "ref transition violates persisted references",
                transition_id=candidate.transition_id,
            ) from exc
        if result.rowcount == 1:
            return candidate

        self._session.expire_all()
        actual = self.get(candidate.transition_id)
        if actual is None:
            raise IntegrityViolation(
                "ref transition insert did not publish a row",
                transition_id=candidate.transition_id,
            )
        if not _same_wire(actual, candidate):
            raise IntegrityViolation(
                "ref transition id is already bound to different immutable content",
                transition_id=candidate.transition_id,
            )
        return actual


__all__ = ["SqlRefTransitionRepository"]
