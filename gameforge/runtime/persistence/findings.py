"""SQLite Finding series with immutable revisions and CAS-protected heads."""

from __future__ import annotations

import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone

from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_sha256, typed_canonical_json
from gameforge.contracts.errors import (
    Conflict,
    CursorExpired,
    CursorInvalid,
    IntegrityViolation,
)
from gameforge.contracts.findings import (
    FindingRevisionV1,
    finding_revision_digest,
)
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
    FindingHeadRow,
    FindingRevisionRow,
    ReadSnapshotRow,
)


_RESOURCE_KIND = "finding_revisions"
_STABLE_SORT_SCHEMA_ID = "finding-revision-asc@1"
_AUTHZ_FINGERPRINT = canonical_sha256(
    {"scope": "finding-repository-internal", "resource_kind": _RESOURCE_KIND}
)
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class _FindingHead:
    finding_id: str
    current_revision: int
    current_digest: str
    row_revision: int


def _query_hash(finding_id: str) -> str:
    return compute_page_query_hash(
        api_version="storage@1",
        resource_kind=_RESOURCE_KIND,
        filters={"finding_id": finding_id},
        stable_sort=("revision:asc",),
        page_projection=(
            "revision_schema_version",
            "finding_id",
            "revision",
            "supersedes_revision",
            "created_at",
            "payload",
        ),
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
        raise IntegrityViolation("finding repository clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _revalidate_for_put(item: FindingRevisionV1) -> FindingRevisionV1:
    if not isinstance(item, FindingRevisionV1):
        raise IntegrityViolation("finding put requires FindingRevisionV1")
    wire = item.model_dump(mode="python")
    try:
        parsed = FindingRevisionV1.model_validate(wire)
        parsed_wire = typed_canonical_json(parsed.model_dump(mode="python"))
        input_wire = typed_canonical_json(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation("finding revision wire is invalid") from exc
    if parsed_wire != input_wire:
        raise IntegrityViolation(
            "finding revision wire is not canonical",
            finding_id=parsed.finding_id,
            revision=parsed.revision,
        )
    return parsed


def _parse_revision_row(
    row: FindingRevisionRow,
    *,
    expected_finding_id: str,
    expected_revision: int,
) -> FindingRevisionV1:
    try:
        if row.finding_id != expected_finding_id or row.revision != expected_revision:
            raise ValueError("finding revision storage key does not match the row")
        parsed = FindingRevisionV1(
            revision_schema_version=row.revision_schema_version,
            finding_id=row.finding_id,
            revision=row.revision,
            supersedes_revision=row.supersedes_revision,
            created_at=row.created_at,
            payload=row.payload,
        )
        if parsed.revision > 1 and parsed.supersedes_revision != parsed.revision - 1:
            raise ValueError("stored finding revision does not supersede its direct predecessor")
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored finding revision is invalid",
            finding_id=expected_finding_id,
            revision=expected_revision,
        ) from exc

    try:
        expected_digest = finding_revision_digest(parsed)
    except (TypeError, ValueError) as exc:
        raise IntegrityViolation(
            "stored finding revision contains a non-canonical JSON value",
            finding_id=expected_finding_id,
            revision=expected_revision,
        ) from exc
    if not isinstance(row.finding_digest, str) or not _LOWER_SHA256.fullmatch(row.finding_digest):
        raise IntegrityViolation(
            "stored finding revision digest is invalid",
            finding_id=expected_finding_id,
            revision=expected_revision,
        )
    if not hmac.compare_digest(row.finding_digest, expected_digest):
        raise IntegrityViolation(
            "stored finding revision digest does not match its content",
            finding_id=expected_finding_id,
            revision=expected_revision,
        )
    return parsed


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
            "stored finding revision read snapshot is invalid",
            snapshot_id=row.snapshot_id,
        ) from exc
    if (
        snapshot.resource_kind != _RESOURCE_KIND
        or snapshot.authz_fingerprint != _AUTHZ_FINGERPRINT
        or snapshot.stable_sort_schema_id != _STABLE_SORT_SCHEMA_ID
        or snapshot.strategy != "immutable_high_watermark"
    ):
        raise IntegrityViolation(
            "stored finding revision read snapshot metadata is invalid",
            snapshot_id=snapshot.snapshot_id,
        )
    return snapshot


class SqlFindingRepository:
    """Transaction-bound store for stable Finding series and immutable revisions."""

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
            raise ValueError("SqlFindingRepository requires a SQLite session")
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_ITEMS:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_ITEMS}")
        if snapshot_ttl <= timedelta(0):
            raise ValueError("snapshot_ttl must be positive")
        self._session = session
        self._cursor_signer = cursor_signer
        self._clock = clock
        self._page_size = page_size
        self._snapshot_ttl = snapshot_ttl

    def put(
        self,
        item: FindingRevisionV1,
        *,
        expected_current_revision: int | None,
    ) -> FindingRevisionV1:
        parsed = _revalidate_for_put(item)
        expected = self._validate_expected_revision(expected_current_revision)

        existing = self._load_revision(parsed.finding_id, parsed.revision)
        if existing is not None:
            if typed_canonical_json(existing.model_dump(mode="python")) != typed_canonical_json(
                parsed.model_dump(mode="python")
            ):
                head = self._load_and_verify_head(parsed.finding_id)
                if (
                    head is not None
                    and parsed.revision > 1
                    and head.current_revision >= parsed.revision
                    and expected == parsed.revision - 1
                    and parsed.supersedes_revision == expected
                ):
                    raise Conflict(
                        "finding put lost the head revision compare-and-set",
                        finding_id=parsed.finding_id,
                        expected_current_revision=expected,
                        actual_current_revision=head.current_revision,
                    )
                raise IntegrityViolation(
                    "finding revision key is already bound to different immutable content",
                    finding_id=parsed.finding_id,
                    revision=parsed.revision,
                )
            original_expected = None if parsed.revision == 1 else parsed.supersedes_revision
            if expected != original_expected:
                raise Conflict(
                    "finding idempotent retry uses a different expected current revision",
                    finding_id=parsed.finding_id,
                    revision=parsed.revision,
                    original_expected_current_revision=original_expected,
                    actual_expected_current_revision=expected,
                )
            head = self._load_and_verify_head(parsed.finding_id)
            if head is None:
                raise IntegrityViolation(
                    "finding revision exists without a head",
                    finding_id=parsed.finding_id,
                )
            if head.current_revision < parsed.revision:
                raise IntegrityViolation(
                    "finding revision is newer than its series head",
                    finding_id=parsed.finding_id,
                    revision=parsed.revision,
                    head_revision=head.current_revision,
                )
            return existing

        head = self._load_and_verify_head(parsed.finding_id)
        if head is None:
            self._require_no_orphan_revisions(parsed.finding_id)
            if expected is not None:
                raise Conflict(
                    "finding series expected current revision but does not exist",
                    finding_id=parsed.finding_id,
                    expected_current_revision=expected,
                )
            if parsed.revision != 1 or parsed.supersedes_revision is not None:
                raise Conflict(
                    "new finding series must begin at revision 1 without a predecessor",
                    finding_id=parsed.finding_id,
                    revision=parsed.revision,
                    supersedes_revision=parsed.supersedes_revision,
                )
            self._insert_revision(parsed)
            result = self._session.execute(
                sqlite_insert(FindingHeadRow)
                .values(
                    finding_id=parsed.finding_id,
                    current_revision=1,
                    current_digest=finding_revision_digest(parsed),
                    row_revision=1,
                    updated_at=_utc_text(self._clock),
                )
                .on_conflict_do_nothing(index_elements=[FindingHeadRow.finding_id])
            )
            if result.rowcount != 1:
                self._remove_inserted_revision(parsed)
                self._session.expire_all()
                actual = self._load_and_verify_head(parsed.finding_id)
                raise Conflict(
                    "finding series create expected absent but a head exists",
                    finding_id=parsed.finding_id,
                    actual_current_revision=(None if actual is None else actual.current_revision),
                )
            return parsed

        if expected != head.current_revision:
            raise Conflict(
                "finding put expected current revision does not match the series head",
                finding_id=parsed.finding_id,
                expected_current_revision=expected,
                actual_current_revision=head.current_revision,
            )
        if parsed.revision != head.current_revision + 1:
            raise Conflict(
                "finding put must publish exactly the next revision",
                finding_id=parsed.finding_id,
                expected_revision=head.current_revision + 1,
                actual_revision=parsed.revision,
            )
        if parsed.supersedes_revision != head.current_revision:
            raise Conflict(
                "finding put must supersede current revision",
                finding_id=parsed.finding_id,
                expected_supersedes_revision=head.current_revision,
                actual_supersedes_revision=parsed.supersedes_revision,
            )

        digest = finding_revision_digest(parsed)
        self._insert_revision(parsed)
        result = self._session.execute(
            update(FindingHeadRow)
            .where(
                FindingHeadRow.finding_id == parsed.finding_id,
                FindingHeadRow.current_revision == head.current_revision,
                FindingHeadRow.current_digest == head.current_digest,
                FindingHeadRow.row_revision == head.row_revision,
            )
            .values(
                current_revision=parsed.revision,
                current_digest=digest,
                row_revision=FindingHeadRow.row_revision + 1,
                updated_at=_utc_text(self._clock),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._remove_inserted_revision(parsed)
            self._session.expire_all()
            actual = self._load_and_verify_head(parsed.finding_id)
            raise Conflict(
                "finding head compare-and-set precondition did not match",
                finding_id=parsed.finding_id,
                expected_current_revision=head.current_revision,
                actual_current_revision=(None if actual is None else actual.current_revision),
            )
        return parsed

    def get(self, finding_id: str, revision: int) -> FindingRevisionV1 | None:
        series_id = _require_nonempty_string(finding_id, field_name="finding id")
        revision_value = _require_revision(revision, field_name="finding revision")
        head = self._load_and_verify_head(series_id)
        if head is None:
            self._require_no_orphan_revisions(series_id)
            return None
        if revision_value > head.current_revision:
            return None
        stored = self._load_revision(series_id, revision_value)
        if stored is None:
            raise IntegrityViolation(
                "finding series is missing a committed revision",
                finding_id=series_id,
                revision=revision_value,
            )
        return stored

    def current(self, finding_id: str) -> FindingRevisionV1 | None:
        series_id = _require_nonempty_string(finding_id, field_name="finding id")
        head = self._load_and_verify_head(series_id)
        if head is None:
            self._require_no_orphan_revisions(series_id)
            return None
        current = self._load_revision(series_id, head.current_revision)
        if current is None:
            raise IntegrityViolation(
                "finding head points to a missing revision",
                finding_id=series_id,
                revision=head.current_revision,
            )
        return current

    def revisions(
        self,
        finding_id: str,
        cursor: PageCursorV1 | None = None,
    ) -> PageV1[FindingRevisionV1]:
        series_id = _require_nonempty_string(finding_id, field_name="finding id")
        expected_query_hash = _query_hash(series_id)
        if cursor is None:
            snapshot = self._create_revision_snapshot(series_id, expected_query_hash)
            position = 0
        else:
            self._cursor_signer.verify_signature(cursor)
            row = self._session.get(ReadSnapshotRow, cursor.snapshot_id)
            if row is None:
                raise CursorExpired("finding revision read snapshot is no longer retained")
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
                raise CursorInvalid("finding revision cursor position is invalid")
            position = int(cursor.position)

        high_watermark = snapshot.high_watermark
        if high_watermark is None:
            raise IntegrityViolation(
                "finding revision snapshot has no immutable high watermark",
                snapshot_id=snapshot.snapshot_id,
            )
        if position < 0 or position > high_watermark:
            raise CursorInvalid("finding revision cursor position is out of range")
        self._verify_snapshot_series_state(series_id, high_watermark)

        rows = self._session.scalars(
            select(FindingRevisionRow)
            .where(
                FindingRevisionRow.finding_id == series_id,
                FindingRevisionRow.revision > position,
                FindingRevisionRow.revision <= high_watermark,
            )
            .order_by(FindingRevisionRow.revision)
            .limit(self._page_size + 1)
        ).all()
        values: list[FindingRevisionV1] = []
        expected_revision = position + 1
        for row in rows:
            if row.revision != expected_revision:
                raise IntegrityViolation(
                    "finding revision sequence has a missing or reordered entry",
                    finding_id=series_id,
                    expected_revision=expected_revision,
                    actual_revision=row.revision,
                )
            values.append(
                _parse_revision_row(
                    row,
                    expected_finding_id=series_id,
                    expected_revision=expected_revision,
                )
            )
            expected_revision += 1

        page_items = tuple(values[: self._page_size])
        end_position = position + len(page_items)
        if end_position < high_watermark and len(values) <= self._page_size:
            raise IntegrityViolation(
                "finding revision sequence ends before its snapshot high watermark",
                finding_id=series_id,
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
        return PageV1[FindingRevisionV1](
            read_snapshot_id=snapshot.snapshot_id,
            items=page_items,
            next_cursor=next_cursor,
            expires_at=snapshot.expires_at,
        )

    @staticmethod
    def _validate_expected_revision(value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise IntegrityViolation(
                "expected current finding revision must be a positive integer or null"
            )
        return value

    def _load_revision(
        self,
        finding_id: str,
        revision: int,
    ) -> FindingRevisionV1 | None:
        row = self._session.get(FindingRevisionRow, (finding_id, revision))
        if row is None:
            return None
        return _parse_revision_row(
            row,
            expected_finding_id=finding_id,
            expected_revision=revision,
        )

    def _insert_revision(self, item: FindingRevisionV1) -> None:
        digest = finding_revision_digest(item)
        result = self._session.execute(
            sqlite_insert(FindingRevisionRow)
            .values(
                finding_id=item.finding_id,
                revision=item.revision,
                revision_schema_version=item.revision_schema_version,
                supersedes_revision=item.supersedes_revision,
                finding_digest=digest,
                created_at=item.created_at,
                payload=item.payload.model_dump(mode="json"),
            )
            .on_conflict_do_nothing()
        )
        if result.rowcount == 1:
            return
        self._session.expire_all()
        existing = self._load_revision(item.finding_id, item.revision)
        if existing is not None and typed_canonical_json(
            existing.model_dump(mode="python")
        ) == typed_canonical_json(item.model_dump(mode="python")):
            return
        raise IntegrityViolation(
            "finding revision could not be inserted without an immutable conflict",
            finding_id=item.finding_id,
            revision=item.revision,
            finding_digest=digest,
        )

    def _remove_inserted_revision(self, item: FindingRevisionV1) -> None:
        result = self._session.execute(
            delete(FindingRevisionRow).where(
                FindingRevisionRow.finding_id == item.finding_id,
                FindingRevisionRow.revision == item.revision,
                FindingRevisionRow.finding_digest == finding_revision_digest(item),
            )
        )
        if result.rowcount != 1:
            raise IntegrityViolation(
                "failed finding head CAS could not remove its unpublished revision",
                finding_id=item.finding_id,
                revision=item.revision,
            )

    def _load_and_verify_head(self, finding_id: str) -> _FindingHead | None:
        row = self._session.get(FindingHeadRow, finding_id)
        if row is None:
            return None
        try:
            if row.finding_id != finding_id:
                raise ValueError("finding head key differs from requested series")
            current_revision = _require_revision(
                row.current_revision,
                field_name="finding head current revision",
            )
            row_revision = _require_revision(
                row.row_revision,
                field_name="finding head row revision",
            )
            if row_revision != current_revision:
                raise ValueError("finding head row revision is not monotonic with content")
            if not isinstance(row.current_digest, str) or not _LOWER_SHA256.fullmatch(
                row.current_digest
            ):
                raise ValueError("finding head digest is not lower-hex SHA-256")
            if not isinstance(row.updated_at, str) or not row.updated_at:
                raise ValueError("finding head update time is invalid")
        except (TypeError, ValueError, IntegrityViolation) as exc:
            raise IntegrityViolation(
                "stored finding head is invalid",
                finding_id=finding_id,
            ) from exc

        current = self._load_revision(finding_id, current_revision)
        if current is None:
            raise IntegrityViolation(
                "finding head points to a missing revision",
                finding_id=finding_id,
                revision=current_revision,
            )
        if (
            current_revision > 1
            and self._load_revision(
                finding_id,
                current_revision - 1,
            )
            is None
        ):
            raise IntegrityViolation(
                "finding head current revision has no direct predecessor",
                finding_id=finding_id,
                revision=current_revision,
                expected_predecessor_revision=current_revision - 1,
            )
        digest = finding_revision_digest(current)
        if not hmac.compare_digest(row.current_digest, digest):
            raise IntegrityViolation(
                "finding head digest disagrees with its current revision",
                finding_id=finding_id,
                revision=current_revision,
            )
        future = self._session.scalar(
            select(FindingRevisionRow.revision)
            .where(
                FindingRevisionRow.finding_id == finding_id,
                FindingRevisionRow.revision > current_revision,
            )
            .order_by(FindingRevisionRow.revision)
            .limit(1)
        )
        if future is not None:
            raise IntegrityViolation(
                "finding series contains a revision newer than its head",
                finding_id=finding_id,
                head_revision=current_revision,
                future_revision=future,
            )
        return _FindingHead(
            finding_id=finding_id,
            current_revision=current_revision,
            current_digest=row.current_digest,
            row_revision=row_revision,
        )

    def _series_has_revisions(self, finding_id: str) -> bool:
        return (
            self._session.scalar(
                select(FindingRevisionRow.revision)
                .where(FindingRevisionRow.finding_id == finding_id)
                .order_by(FindingRevisionRow.revision)
                .limit(1)
            )
            is not None
        )

    def _require_no_orphan_revisions(self, finding_id: str) -> None:
        if self._series_has_revisions(finding_id):
            raise IntegrityViolation(
                "finding revisions exist without a head",
                finding_id=finding_id,
            )

    def _create_revision_snapshot(
        self,
        finding_id: str,
        query_hash: str,
    ) -> ReadSnapshotV1:
        head = self._load_and_verify_head(finding_id)
        if head is None:
            self._require_no_orphan_revisions(finding_id)
            high_watermark = 0
        else:
            high_watermark = head.current_revision

        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise IntegrityViolation("finding repository clock must return UTC")
        created_at = now.astimezone(timezone.utc)
        expires_at = created_at + self._snapshot_ttl
        snapshot = ReadSnapshotV1(
            snapshot_id=f"finding-revisions-snapshot:{uuid.uuid4().hex}",
            resource_kind=_RESOURCE_KIND,
            query_hash=query_hash,
            authz_fingerprint=_AUTHZ_FINGERPRINT,
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

    def _verify_snapshot_series_state(
        self,
        finding_id: str,
        high_watermark: int,
    ) -> None:
        head = self._load_and_verify_head(finding_id)
        if head is None:
            if high_watermark != 0:
                raise IntegrityViolation(
                    "finding series was removed below its revision snapshot high watermark",
                    finding_id=finding_id,
                    high_watermark=high_watermark,
                )
            self._require_no_orphan_revisions(finding_id)
            return
        if head.current_revision < high_watermark:
            raise IntegrityViolation(
                "finding head is below its revision snapshot high watermark",
                finding_id=finding_id,
                current_revision=head.current_revision,
                high_watermark=high_watermark,
            )


__all__ = ["SqlFindingRepository"]
