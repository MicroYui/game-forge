"""SQLite revisioned named refs with immutable high-watermark history pages."""

from __future__ import annotations

import uuid
from datetime import timedelta, timezone

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import (
    Conflict,
    CursorExpired,
    CursorInvalid,
    IntegrityViolation,
)
from gameforge.contracts.storage import (
    MAX_PAGE_ITEMS,
    PageCursorV1,
    PageV1,
    ReadSnapshotV1,
    RefValue,
    UtcClock,
    compute_page_query_hash,
)
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.models import ReadSnapshotRow, RefHistoryRow, RefRow


_RESOURCE_KIND = "ref_history"
_STABLE_SORT_SCHEMA_ID = "ref-history-revision-asc@1"
_AUTHZ_FINGERPRINT = canonical_sha256({"scope": "revisioned-ref-store-internal"})


def _query_hash(name: str) -> str:
    return compute_page_query_hash(
        api_version="storage@1",
        resource_kind=_RESOURCE_KIND,
        filters={"ref_name": name},
        stable_sort=("revision:asc",),
        page_projection=("artifact_id", "revision"),
    )


def _require_nonempty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityViolation(f"{field_name} must be a non-empty string")
    return value


def _require_revision(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise IntegrityViolation(f"{field_name} must be a positive integer")
    return value


def _utc_text(clock: UtcClock) -> str:
    now = clock.now_utc()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise IntegrityViolation("ref repository clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_current_row(row: RefRow, *, expected_name: str) -> RefValue:
    try:
        if row.name != expected_name:
            raise ValueError("row key differs from requested ref")
        return RefValue(artifact_id=row.artifact_id, revision=row.revision)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored current ref is invalid",
            ref_name=expected_name,
        ) from exc


def _parse_history_row(row: RefHistoryRow, *, expected_name: str) -> RefValue:
    try:
        if row.name != expected_name:
            raise ValueError("row key differs from requested ref")
        return RefValue(artifact_id=row.artifact_id, revision=row.seq)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored ref history entry is invalid",
            ref_name=expected_name,
            sequence=getattr(row, "seq", None),
        ) from exc


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
            "stored ref history read snapshot is invalid",
            snapshot_id=row.snapshot_id,
        ) from exc
    if (
        snapshot.resource_kind != _RESOURCE_KIND
        or snapshot.authz_fingerprint != _AUTHZ_FINGERPRINT
        or snapshot.stable_sort_schema_id != _STABLE_SORT_SCHEMA_ID
        or snapshot.strategy != "immutable_high_watermark"
    ):
        raise IntegrityViolation(
            "stored ref history read snapshot metadata is invalid",
            snapshot_id=snapshot.snapshot_id,
        )
    return snapshot


class SqlRefStore:
    """Transaction-bound SQLite implementation of the M4 ``RefStore`` contract."""

    def __init__(
        self,
        session: Session,
        *,
        cursor_signer: CursorSigner,
        clock: UtcClock,
        page_size: int = 100,
        snapshot_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        if session.get_bind().dialect.name != "sqlite":
            raise ValueError("SqlRefStore requires a SQLite session")
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_ITEMS:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_ITEMS}")
        if snapshot_ttl <= timedelta(0):
            raise ValueError("snapshot_ttl must be positive")
        self._session = session
        self._cursor_signer = cursor_signer
        self._clock = clock
        self._page_size = page_size
        self._snapshot_ttl = snapshot_ttl

    def get(self, name: str) -> RefValue | None:
        ref_name = _require_nonempty_string(name, field_name="ref name")
        current = self._load_current(ref_name)
        if current is None:
            self._require_no_orphan_history(ref_name)
            return None
        self._verify_current_history(ref_name, current)
        return current

    def get_history_entry(self, name: str, revision: int) -> RefValue | None:
        """Read one retained ref revision in the caller's transaction snapshot."""

        ref_name = _require_nonempty_string(name, field_name="ref name")
        target_revision = _require_revision(revision, field_name="ref revision")
        current = self._load_current(ref_name)
        if current is None:
            self._require_no_orphan_history(ref_name)
            return None
        self._verify_current_history(ref_name, current)

        if target_revision > current.revision:
            return None
        target_row = self._session.scalar(
            select(RefHistoryRow).where(
                RefHistoryRow.name == ref_name,
                RefHistoryRow.seq == target_revision,
            )
        )
        if target_row is None:
            raise IntegrityViolation(
                "retained ref history entry is missing or noncontiguous",
                ref_name=ref_name,
                requested_revision=target_revision,
                current_revision=current.revision,
            )
        return _parse_history_row(target_row, expected_name=ref_name)

    def compare_and_set(
        self,
        name: str,
        expected: RefValue | None,
        new_artifact_id: str,
    ) -> RefValue:
        ref_name = _require_nonempty_string(name, field_name="ref name")
        artifact_id = _require_nonempty_string(
            new_artifact_id,
            field_name="new artifact id",
        )
        expected_value = self._validate_expected(expected)
        updated_at = _utc_text(self._clock)

        current = self._load_current(ref_name)
        if current is None:
            self._require_no_orphan_history(ref_name)
        else:
            self._verify_current_history(ref_name, current)

        if expected_value is None:
            if current is not None:
                raise Conflict(
                    "ref create expected absent but current ref exists",
                    ref_name=ref_name,
                    actual=current.model_dump(mode="json"),
                )
            result = self._session.execute(
                sqlite_insert(RefRow)
                .values(
                    name=ref_name,
                    artifact_id=artifact_id,
                    revision=1,
                    updated_at=updated_at,
                )
                .on_conflict_do_nothing(index_elements=[RefRow.name])
            )
            if result.rowcount != 1:
                self._session.expire_all()
                actual = self._load_current(ref_name)
                if actual is None:
                    raise IntegrityViolation(
                        "ref create did not publish a current row",
                        ref_name=ref_name,
                    )
                self._verify_current_history(ref_name, actual)
                raise Conflict(
                    "ref create expected absent but current ref exists",
                    ref_name=ref_name,
                    actual=actual.model_dump(mode="json"),
                )
            created = RefValue(artifact_id=artifact_id, revision=1)
            self._append_history(ref_name, created)
            return created

        if current is None:
            raise Conflict(
                "ref compare-and-set expected a current ref but none exists",
                ref_name=ref_name,
                expected=expected_value.model_dump(mode="json"),
            )

        statement = (
            update(RefRow)
            .where(
                RefRow.name == ref_name,
                RefRow.artifact_id == expected_value.artifact_id,
                RefRow.revision == expected_value.revision,
            )
            .values(
                artifact_id=artifact_id,
                revision=RefRow.revision + 1,
                updated_at=updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self._load_current(ref_name)
            if actual is not None:
                self._verify_current_history(ref_name, actual)
            raise Conflict(
                "ref compare-and-set precondition did not match",
                ref_name=ref_name,
                expected=expected_value.model_dump(mode="json"),
                actual=None if actual is None else actual.model_dump(mode="json"),
            )

        updated = RefValue(
            artifact_id=artifact_id,
            revision=expected_value.revision + 1,
        )
        self._append_history(ref_name, updated)
        return updated

    def history(
        self,
        name: str,
        cursor: PageCursorV1 | None = None,
    ) -> PageV1[RefValue]:
        ref_name = _require_nonempty_string(name, field_name="ref name")
        expected_query_hash = _query_hash(ref_name)
        if cursor is None:
            snapshot = self._create_history_snapshot(ref_name, expected_query_hash)
            position = 0
        else:
            self._cursor_signer.verify_signature(cursor)
            row = self._session.get(ReadSnapshotRow, cursor.snapshot_id)
            if row is None:
                raise CursorExpired("ref history read snapshot is no longer retained")
            snapshot = _snapshot_from_row(row)
            self._cursor_signer.verify(
                cursor,
                expected_snapshot=snapshot,
                expected_query_hash=expected_query_hash,
                requested_page_size=self._page_size,
                snapshot_is_retained=lambda snapshot_id: (
                    self._session.get(ReadSnapshotRow, snapshot_id) is not None
                ),
            )
            if not cursor.position.isascii() or not cursor.position.isdecimal():
                raise CursorInvalid("ref history cursor position is invalid")
            position = int(cursor.position)

        high_watermark = snapshot.high_watermark
        if high_watermark is None:
            raise IntegrityViolation(
                "ref history snapshot has no immutable high watermark",
                snapshot_id=snapshot.snapshot_id,
            )
        if position < 0 or position > high_watermark:
            raise CursorInvalid("ref history cursor position is out of range")
        self._verify_snapshot_ref_state(ref_name, high_watermark)

        rows = self._session.scalars(
            select(RefHistoryRow)
            .where(
                RefHistoryRow.name == ref_name,
                RefHistoryRow.seq > position,
                RefHistoryRow.seq <= high_watermark,
            )
            .order_by(RefHistoryRow.seq)
            .limit(self._page_size + 1)
        ).all()
        values: list[RefValue] = []
        expected_revision = position + 1
        for row in rows:
            value = _parse_history_row(row, expected_name=ref_name)
            if value.revision != expected_revision:
                raise IntegrityViolation(
                    "ref history sequence is missing or reordered",
                    ref_name=ref_name,
                    expected_revision=expected_revision,
                    actual_revision=value.revision,
                )
            values.append(value)
            expected_revision += 1

        page_items = tuple(values[: self._page_size])
        end_position = position + len(page_items)
        if end_position < high_watermark and len(values) <= self._page_size:
            raise IntegrityViolation(
                "ref history sequence ends before its snapshot high watermark",
                ref_name=ref_name,
                expected_revision=end_position + 1,
                high_watermark=high_watermark,
            )

        next_cursor = None
        if end_position < high_watermark:
            next_cursor = self._cursor_signer.issue(
                snapshot=snapshot,
                position=str(end_position),
                page_size=self._page_size,
            )
        return PageV1[RefValue](
            read_snapshot_id=snapshot.snapshot_id,
            items=page_items,
            next_cursor=next_cursor,
            expires_at=snapshot.expires_at,
        )

    @staticmethod
    def _validate_expected(expected: RefValue | None) -> RefValue | None:
        if expected is None:
            return None
        if not isinstance(expected, RefValue):
            raise IntegrityViolation("expected ref must be RefValue or null")
        try:
            return RefValue.model_validate(expected.model_dump(mode="json"))
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("expected ref is invalid") from exc

    def _load_current(self, name: str) -> RefValue | None:
        row = self._session.get(RefRow, name)
        return None if row is None else _parse_current_row(row, expected_name=name)

    def _history_exists(self, name: str) -> bool:
        return (
            self._session.scalar(
                select(RefHistoryRow.id).where(RefHistoryRow.name == name).limit(1)
            )
            is not None
        )

    def _require_no_orphan_history(self, name: str) -> None:
        if self._history_exists(name):
            raise IntegrityViolation(
                "ref history exists without a current ref",
                ref_name=name,
            )

    def _verify_current_history(self, name: str, current: RefValue) -> None:
        predecessor = self._session.scalar(
            select(RefHistoryRow).where(
                RefHistoryRow.name == name,
                RefHistoryRow.seq == current.revision,
            )
        )
        if predecessor is None:
            raise IntegrityViolation(
                "current ref has no matching ref history entry",
                ref_name=name,
                revision=current.revision,
            )
        history_value = _parse_history_row(predecessor, expected_name=name)
        if history_value != current:
            raise IntegrityViolation(
                "stored current/history ref values disagree",
                ref_name=name,
                revision=current.revision,
            )
        future_sequence = self._session.scalar(
            select(RefHistoryRow.seq)
            .where(
                RefHistoryRow.name == name,
                RefHistoryRow.seq > current.revision,
            )
            .order_by(RefHistoryRow.seq)
            .limit(1)
        )
        if future_sequence is not None:
            raise IntegrityViolation(
                "ref history contains entries newer than current ref",
                ref_name=name,
                current_revision=current.revision,
                future_revision=future_sequence,
            )

    def _append_history(self, name: str, value: RefValue) -> None:
        self._session.add(
            RefHistoryRow(
                name=name,
                artifact_id=value.artifact_id,
                seq=value.revision,
            )
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise IntegrityViolation(
                "ref history revision could not be appended",
                ref_name=name,
                revision=value.revision,
            ) from exc

    def _create_history_snapshot(
        self,
        name: str,
        query_hash: str,
    ) -> ReadSnapshotV1:
        current = self._load_current(name)
        if current is None:
            self._require_no_orphan_history(name)
            high_watermark = 0
        else:
            self._verify_current_history(name, current)
            high_watermark = current.revision

        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise IntegrityViolation("ref repository clock must return UTC")
        created_at_value = now.astimezone(timezone.utc)
        expires_at_value = created_at_value + self._snapshot_ttl
        snapshot = ReadSnapshotV1(
            snapshot_id=f"ref-history-snapshot:{uuid.uuid4().hex}",
            resource_kind=_RESOURCE_KIND,
            query_hash=query_hash,
            authz_fingerprint=_AUTHZ_FINGERPRINT,
            stable_sort_schema_id=_STABLE_SORT_SCHEMA_ID,
            strategy="immutable_high_watermark",
            high_watermark=high_watermark,
            created_at=created_at_value.isoformat().replace("+00:00", "Z"),
            expires_at=expires_at_value.isoformat().replace("+00:00", "Z"),
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

    def _verify_snapshot_ref_state(self, name: str, high_watermark: int) -> None:
        current = self._load_current(name)
        if current is None:
            if high_watermark != 0:
                raise IntegrityViolation(
                    "ref was removed below its history snapshot high watermark",
                    ref_name=name,
                    high_watermark=high_watermark,
                )
            self._require_no_orphan_history(name)
            return
        self._verify_current_history(name, current)
        if current.revision < high_watermark:
            raise IntegrityViolation(
                "current ref revision is below its history snapshot high watermark",
                ref_name=name,
                current_revision=current.revision,
                high_watermark=high_watermark,
            )


__all__ = ["SqlRefStore"]
