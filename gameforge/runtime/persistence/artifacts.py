"""Immutable transaction-bound SQL Artifact repository."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta, timezone
from typing import Any, Sequence

from pydantic import ValidationError
from sqlalchemy import BigInteger, func, literal_column, select
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import CursorExpired, CursorInvalid, IntegrityViolation
from gameforge.contracts.lineage import ArtifactV1, ArtifactV2, parse_artifact
from gameforge.contracts.storage import (
    MAX_PAGE_ITEMS,
    PageCursorV1,
    PageV1,
    ReadSnapshotV1,
    UtcClock,
    compute_page_query_hash,
)
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.models import (
    ArtifactRow,
    ReadSnapshotRow,
)
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository


ArtifactWire = ArtifactV1 | ArtifactV2
_MAX_SQL_IN_ITEMS = 900
_ARTIFACT_QUERY_HASH = compute_page_query_hash(
    api_version="storage@1",
    resource_kind="artifacts",
    filters={},
    stable_sort=("artifact_id:asc",),
    page_projection=(
        "lineage_schema_version",
        "kind",
        "version_tuple",
        "lineage",
        "payload_hash",
        "object_ref",
        "created_at",
        "meta",
    ),
)
_ARTIFACT_AUTHZ_FINGERPRINT = canonical_sha256(
    {"scope": "artifact-repository-internal", "resource_kind": "artifacts"}
)
_STABLE_SORT_SCHEMA_ID = "artifact-id-asc@1"
# Artifact rows are append-only: DELETE/VACUUM must not run while a read snapshot is retained.
_ARTIFACT_ROWID = literal_column("artifacts.rowid", type_=BigInteger())


def _row_wire(row: ArtifactRow) -> dict[str, Any]:
    base = {
        "artifact_id": row.artifact_id,
        "lineage_schema_version": row.lineage_schema_version,
        "kind": row.kind,
        "version_tuple": row.version_tuple,
        "lineage": row.lineage,
        "payload_hash": row.payload_hash,
        "created_at": row.created_at,
        "meta": row.meta,
    }
    if row.lineage_schema_version == "lineage@1":
        if row.object_ref is not None:
            raise IntegrityViolation(
                "stored lineage@1 artifact must not contain an ObjectRef",
                artifact_id=row.artifact_id,
            )
        # Historical M0b rows allowed null in this JSON column and were read as {}.
        if base["meta"] is None:
            base["meta"] = {}
    else:
        base["object_ref"] = row.object_ref
    return base


def _parse_stored_wire(value: Any, *, artifact_id: str, source: str) -> ArtifactWire:
    try:
        parsed = parse_artifact(value)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            f"{source} contains an invalid stored artifact",
            artifact_id=artifact_id,
        ) from exc
    if parsed.artifact_id != artifact_id:
        raise IntegrityViolation(
            f"{source} artifact identity differs from its storage key",
            artifact_id=artifact_id,
            parsed_artifact_id=parsed.artifact_id,
        )
    if isinstance(parsed, ArtifactV2) and canonical_json(
        parsed.model_dump(mode="json")
    ) != canonical_json(value):
        raise IntegrityViolation(
            f"{source} ArtifactV2 wire is not canonical",
            artifact_id=artifact_id,
        )
    return parsed


def _revalidate_for_put(item: ArtifactWire) -> ArtifactWire:
    if not isinstance(item, (ArtifactV1, ArtifactV2)):
        raise IntegrityViolation("artifact put requires an ArtifactV1 or ArtifactV2")
    if isinstance(item, ArtifactV1):
        unexpected_fields = set(item.__dict__) - set(type(item).model_fields)
        if unexpected_fields:
            raise IntegrityViolation(
                "invalid artifact wire contains fields outside lineage@1",
                artifact_id=item.artifact_id,
                fields=sorted(unexpected_fields),
            )
    wire = item.model_dump(mode="json")
    try:
        parsed = parse_artifact(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "invalid artifact wire or content identity",
            artifact_id=getattr(item, "artifact_id", None),
        ) from exc
    if canonical_json(parsed.model_dump(mode="json")) != canonical_json(wire):
        raise IntegrityViolation(
            "invalid artifact wire is not canonical",
            artifact_id=item.artifact_id,
        )
    return parsed


def _immutable_identity(item: ArtifactWire) -> str:
    return canonical_json(item.model_dump(mode="json", exclude={"created_at"}))


def _artifact_row(item: ArtifactWire) -> ArtifactRow:
    wire = item.model_dump(mode="json")
    return ArtifactRow(
        artifact_id=wire["artifact_id"],
        lineage_schema_version=wire["lineage_schema_version"],
        kind=wire["kind"],
        version_tuple=wire["version_tuple"],
        lineage=wire["lineage"],
        payload_hash=wire["payload_hash"],
        created_at=wire["created_at"],
        meta=wire["meta"],
        object_ref=wire.get("object_ref"),
    )


def _snapshot_from_row(row: ReadSnapshotRow) -> ReadSnapshotV1:
    try:
        snapshot = ReadSnapshotV1(
            snapshot_schema_version=row.snapshot_schema_version,
            snapshot_id=row.snapshot_id,
            resource_kind=row.resource_kind,
            query_hash=row.query_hash,
            authz_fingerprint=row.authz_fingerprint,
            stable_sort_schema_id=row.stable_sort_schema_id,
            strategy=row.strategy,
            high_watermark=row.high_watermark,
            materialized_item_count=row.materialized_item_count,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored artifact read snapshot is invalid",
            snapshot_id=row.snapshot_id,
        ) from exc
    if (
        snapshot.resource_kind != "artifacts"
        or snapshot.authz_fingerprint != _ARTIFACT_AUTHZ_FINGERPRINT
        or snapshot.stable_sort_schema_id != _STABLE_SORT_SCHEMA_ID
        or snapshot.strategy != "immutable_high_watermark"
    ):
        raise IntegrityViolation(
            "stored artifact read snapshot metadata is invalid",
            snapshot_id=row.snapshot_id,
        )
    return snapshot


def _encode_position(artifact_id: str) -> str:
    return canonical_json({"artifact_id": artifact_id})


def _decode_position(position: str) -> str:
    try:
        value = json.loads(position)
    except (TypeError, ValueError) as exc:
        raise CursorInvalid("artifact cursor position is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"artifact_id"}
        or not isinstance(value["artifact_id"], str)
        or not value["artifact_id"]
    ):
        raise CursorInvalid("artifact cursor position is invalid")
    return value["artifact_id"]


class SqlArtifactRepository:
    """Persist ArtifactV1/V2 without owning the surrounding transaction."""

    def __init__(
        self,
        session: Session,
        *,
        binding_repository: SqlObjectBindingRepository | None,
        cursor_signer: CursorSigner,
        clock: UtcClock,
        page_size: int = 100,
        snapshot_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_ITEMS:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_ITEMS}")
        if snapshot_ttl <= timedelta(0):
            raise ValueError("snapshot_ttl must be positive")
        self._session = session
        self._binding_repository = binding_repository
        self._cursor_signer = cursor_signer
        self._clock = clock
        self._page_size = page_size
        self._snapshot_ttl = snapshot_ttl

    def get(self, identifier: str) -> ArtifactWire | None:
        row = self._session.get(ArtifactRow, identifier)
        if row is None:
            return None
        try:
            wire = _row_wire(row)
        except IntegrityViolation:
            raise
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "stored artifact row is invalid",
                artifact_id=identifier,
            ) from exc
        return _parse_stored_wire(wire, artifact_id=identifier, source="stored artifact row")

    def get_many(self, identifiers: Sequence[str]) -> dict[str, ArtifactWire | None]:
        """Read an exact Artifact set with bounded SQL statements."""

        selected = tuple(dict.fromkeys(identifiers))
        if any(not isinstance(identifier, str) or not identifier for identifier in selected):
            raise ValueError("artifact identifiers must be non-empty strings")
        retained: dict[str, ArtifactWire | None] = dict.fromkeys(selected)
        for offset in range(0, len(selected), _MAX_SQL_IN_ITEMS):
            rows = self._session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.artifact_id.in_(selected[offset : offset + _MAX_SQL_IN_ITEMS])
                )
            ).all()
            for row in rows:
                identifier = row.artifact_id
                try:
                    wire = _row_wire(row)
                    retained[identifier] = _parse_stored_wire(
                        wire,
                        artifact_id=identifier,
                        source="stored artifact row",
                    )
                except IntegrityViolation:
                    raise
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "stored artifact row is invalid",
                        artifact_id=identifier,
                    ) from exc
        return retained

    def put(self, item: ArtifactWire) -> ArtifactWire:
        parsed = _revalidate_for_put(item)
        if isinstance(parsed, ArtifactV2):
            if self._binding_repository is None or not self._binding_repository.has_active_binding(
                parsed.object_ref
            ):
                raise IntegrityViolation(
                    "ArtifactV2 publication requires an active ObjectBinding",
                    artifact_id=parsed.artifact_id,
                    object_key=parsed.object_ref.key,
                )

        existing = self.get(parsed.artifact_id)
        if existing is None:
            self._session.add(_artifact_row(parsed))
            self._session.flush()
            return parsed
        if _immutable_identity(existing) != _immutable_identity(parsed):
            raise IntegrityViolation(
                "artifact id is already bound to different immutable content",
                artifact_id=parsed.artifact_id,
            )
        return existing

    def page(self, cursor: PageCursorV1 | None = None) -> PageV1[ArtifactWire]:
        if cursor is None:
            snapshot = self._create_snapshot()
            position = None
        else:
            self._cursor_signer.verify_signature(cursor)
            row = self._session.get(ReadSnapshotRow, cursor.snapshot_id)
            if row is None:
                raise CursorExpired("artifact read snapshot is no longer retained")
            snapshot = _snapshot_from_row(row)
            self._cursor_signer.verify(
                cursor,
                expected_snapshot=snapshot,
                expected_query_hash=_ARTIFACT_QUERY_HASH,
                requested_page_size=self._page_size,
                snapshot_is_retained=lambda snapshot_id: (
                    self._session.get(ReadSnapshotRow, snapshot_id) is not None
                ),
            )
            position = _decode_position(cursor.position)

        high_watermark = snapshot.high_watermark
        if high_watermark is None:
            raise IntegrityViolation(
                "artifact read snapshot has no immutable high watermark",
                snapshot_id=snapshot.snapshot_id,
            )
        statement = select(ArtifactRow).where(_ARTIFACT_ROWID <= high_watermark)
        if position is not None:
            anchor = self._session.scalar(
                select(_ARTIFACT_ROWID)
                .select_from(ArtifactRow)
                .where(
                    ArtifactRow.artifact_id == position,
                    _ARTIFACT_ROWID <= high_watermark,
                )
            )
            if anchor is None:
                raise IntegrityViolation(
                    "artifact cursor anchor is missing from its read snapshot",
                    snapshot_id=snapshot.snapshot_id,
                    artifact_id=position,
                )
            statement = statement.where(ArtifactRow.artifact_id > position)
        rows = self._session.scalars(
            statement.order_by(ArtifactRow.artifact_id).limit(self._page_size + 1)
        ).all()

        values = tuple(
            _parse_stored_wire(
                _row_wire(row),
                artifact_id=row.artifact_id,
                source="stored artifact row",
            )
            for row in rows
        )
        items = values[: self._page_size]
        next_cursor = None
        if len(values) > self._page_size:
            next_cursor = self._cursor_signer.issue(
                snapshot=snapshot,
                position=_encode_position(items[-1].artifact_id),
                page_size=self._page_size,
            )
        return PageV1[ArtifactWire](
            read_snapshot_id=snapshot.snapshot_id,
            items=items,
            next_cursor=next_cursor,
            expires_at=snapshot.expires_at,
        )

    def _create_snapshot(self) -> ReadSnapshotV1:
        high_watermark = self._session.scalar(
            select(func.coalesce(func.max(_ARTIFACT_ROWID), 0)).select_from(ArtifactRow)
        )
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise IntegrityViolation("artifact repository clock must return UTC")
        created_at = now.astimezone(timezone.utc)
        expires_at = created_at + self._snapshot_ttl
        snapshot = ReadSnapshotV1(
            snapshot_id=f"artifact-read-snapshot:{uuid.uuid4().hex}",
            resource_kind="artifacts",
            query_hash=_ARTIFACT_QUERY_HASH,
            authz_fingerprint=_ARTIFACT_AUTHZ_FINGERPRINT,
            stable_sort_schema_id=_STABLE_SORT_SCHEMA_ID,
            strategy="immutable_high_watermark",
            high_watermark=high_watermark,
            created_at=created_at.isoformat().replace("+00:00", "Z"),
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        )
        self._session.add(
            ReadSnapshotRow(
                snapshot_id=snapshot.snapshot_id,
                snapshot_schema_version=snapshot.snapshot_schema_version,
                resource_kind=snapshot.resource_kind,
                query_hash=snapshot.query_hash,
                authz_fingerprint=snapshot.authz_fingerprint,
                stable_sort_schema_id=snapshot.stable_sort_schema_id,
                strategy=snapshot.strategy,
                high_watermark=snapshot.high_watermark,
                materialized_item_count=snapshot.materialized_item_count,
                created_at=snapshot.created_at,
                expires_at=snapshot.expires_at,
            )
        )
        self._session.flush()
        return snapshot


__all__ = ["SqlArtifactRepository"]
