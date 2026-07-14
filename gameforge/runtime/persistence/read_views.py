"""Transaction-bound materialized read snapshots for mutable authorized lists."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from itertools import islice
import json
import re
import uuid
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import (
    CursorExpired,
    CursorInvalid,
    Forbidden,
    IntegrityViolation,
    QueryTooBroad,
)
from gameforge.contracts.storage import (
    MAX_PAGE_ITEMS,
    MaterializedReadItemV1,
    PageCursorV1,
    PageV1,
    ReadSnapshotV1,
    UtcClock,
)
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.models import MaterializedReadItemRow, ReadSnapshotRow


_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_POSITION_SCHEMA_VERSION = "materialized-position@1"
_BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=512)]
_Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
T = TypeVar("T")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class MaterializedReadBinding(_FrozenModel):
    """Exact query, projection, principal, and authorization binding for one list."""

    resource_kind: _BoundedText
    query_hash: _Sha256Hex
    authz_fingerprint: _Sha256Hex
    stable_sort_schema_id: _BoundedText
    view_schema_id: _BoundedText
    principal_binding: _Sha256Hex


class MaterializedReadCandidate(_FrozenModel):
    """One already-authorized canonical list projection in stable source order."""

    resource_id: _BoundedText
    observed_revision: Annotated[int, Field(gt=0)]
    canonical_view: dict[str, JsonValue]


class ImmutableReadBinding(_FrozenModel):
    """Exact query and authorization binding for an append-only source."""

    resource_kind: _BoundedText
    query_hash: _Sha256Hex
    authz_fingerprint: _Sha256Hex
    stable_sort_schema_id: _BoundedText
    principal_binding: _Sha256Hex


class ImmutableReadCandidate(_FrozenModel, Generic[T]):
    """One append-only source row observed below a retained high watermark."""

    resource_id: _BoundedText
    source_position: _BoundedText
    observed_sequence: Annotated[int, Field(gt=0)]
    observed_revision: Annotated[int, Field(gt=0)]
    item: T


def _utc_now(clock: UtcClock) -> datetime:
    value = clock.now_utc()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise IntegrityViolation("materialized read-view clock must return UTC")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _snapshot_from_row(row: ReadSnapshotRow) -> ReadSnapshotV1:
    try:
        return ReadSnapshotV1(
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
            "stored materialized read snapshot is invalid",
            snapshot_id=getattr(row, "snapshot_id", None),
        ) from exc


def _position(*, ordinal: int, principal_binding: str) -> str:
    return canonical_json(
        {
            "position_schema_version": _POSITION_SCHEMA_VERSION,
            "ordinal": ordinal,
            "principal_binding": principal_binding,
        }
    )


def _decode_position(value: str) -> tuple[int, str]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CursorInvalid("materialized cursor position is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"position_schema_version", "ordinal", "principal_binding"}
        or payload.get("position_schema_version") != _POSITION_SCHEMA_VERSION
        or isinstance(payload.get("ordinal"), bool)
        or not isinstance(payload.get("ordinal"), int)
        or payload["ordinal"] < 1
        or not isinstance(payload.get("principal_binding"), str)
        or _LOWER_SHA256.fullmatch(payload["principal_binding"]) is None
        or canonical_json(payload) != value
    ):
        raise CursorInvalid("materialized cursor position is invalid")
    return payload["ordinal"], payload["principal_binding"]


def _immutable_position(*, source_position: str, principal_binding: str) -> str:
    return canonical_json(
        {
            "position_schema_version": "immutable-position@1",
            "principal_binding": principal_binding,
            "source_position": source_position,
        }
    )


def _decode_immutable_position(value: str) -> tuple[str, str]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CursorInvalid("immutable cursor position is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"position_schema_version", "principal_binding", "source_position"}
        or payload.get("position_schema_version") != "immutable-position@1"
        or not isinstance(payload.get("source_position"), str)
        or not payload["source_position"]
        or len(payload["source_position"]) > 512
        or not isinstance(payload.get("principal_binding"), str)
        or _LOWER_SHA256.fullmatch(payload["principal_binding"]) is None
        or canonical_json(payload) != value
    ):
        raise CursorInvalid("immutable cursor position is invalid")
    return payload["source_position"], payload["principal_binding"]


def _item_from_row(
    row: MaterializedReadItemRow,
    *,
    snapshot_id: str,
    expected_view_schema_id: str,
) -> MaterializedReadItemV1:
    try:
        item = MaterializedReadItemV1(
            snapshot_id=row.snapshot_id,
            ordinal=row.ordinal,
            resource_id=row.resource_id,
            observed_revision=row.observed_revision,
            view_schema_id=row.view_schema_id,
            canonical_view=row.canonical_view,
            view_hash=row.view_hash,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored materialized read item is invalid",
            snapshot_id=snapshot_id,
            ordinal=getattr(row, "ordinal", None),
        ) from exc
    if item.snapshot_id != snapshot_id:
        raise IntegrityViolation(
            "materialized read item belongs to another snapshot",
            snapshot_id=snapshot_id,
            ordinal=item.ordinal,
        )
    if item.view_schema_id != expected_view_schema_id:
        raise IntegrityViolation(
            "stored materialized read item view schema is invalid",
            snapshot_id=snapshot_id,
            ordinal=item.ordinal,
        )
    return item


class SqlImmutableReadViewRepository(Generic[T]):
    """Retain an authorization-bound high watermark for append-only list reads.

    Source-specific code owns the SQL query and stable position encoding. This
    coordinator owns the retained snapshot, signed cursor, bounds, and invariant
    checks, without retaining duplicate immutable rows.
    """

    def __init__(
        self,
        session: Session,
        *,
        cursor_signer: CursorSigner,
        clock: UtcClock,
        page_size: int = 100,
        snapshot_ttl: timedelta = timedelta(minutes=5),
        snapshot_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_ITEMS:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_ITEMS}")
        if snapshot_ttl <= timedelta(0):
            raise ValueError("snapshot_ttl must be positive")
        if snapshot_id_factory is not None and not callable(snapshot_id_factory):
            raise TypeError("snapshot_id_factory must be callable")
        self._session = session
        self._cursor_signer = cursor_signer
        self._clock = clock
        self._page_size = page_size
        self._snapshot_ttl = snapshot_ttl
        self._snapshot_id_factory = snapshot_id_factory or (
            lambda: f"read-snapshot:immutable:{uuid.uuid4().hex}"
        )

    def page(
        self,
        *,
        binding: ImmutableReadBinding,
        cursor: PageCursorV1 | None,
        high_watermark: Callable[[], int],
        load_candidates: Callable[
            [str | None, int, int],
            Iterable[ImmutableReadCandidate[T]],
        ],
    ) -> PageV1[T]:
        exact_binding = self._binding(binding)
        if not callable(high_watermark) or not callable(load_candidates):
            raise TypeError("immutable source callbacks must be callable")
        if cursor is None:
            snapshot = self._create_snapshot(
                binding=exact_binding,
                high_watermark=high_watermark(),
            )
            after_position = None
        else:
            if not isinstance(cursor, PageCursorV1):
                raise CursorInvalid("immutable page cursor has an invalid schema")
            self._cursor_signer.verify_signature(cursor)
            row = self._session.get(ReadSnapshotRow, cursor.snapshot_id)
            if row is None:
                raise CursorExpired("immutable read snapshot is no longer retained")
            snapshot = _snapshot_from_row(row)
            self._cursor_signer.verify(
                cursor,
                expected_snapshot=snapshot,
                expected_query_hash=exact_binding.query_hash,
                requested_page_size=self._page_size,
                snapshot_is_retained=lambda snapshot_id: (
                    self._session.get(ReadSnapshotRow, snapshot_id) is not None
                ),
            )
            after_position, retained_principal = _decode_immutable_position(cursor.position)
            if retained_principal != exact_binding.principal_binding:
                raise Forbidden("immutable cursor belongs to another principal")
            self._require_snapshot_metadata(snapshot, exact_binding)
            if snapshot.authz_fingerprint != exact_binding.authz_fingerprint:
                raise CursorExpired("immutable cursor authorization has changed")

        retained_high_watermark = snapshot.high_watermark
        if retained_high_watermark is None:
            raise IntegrityViolation(
                "immutable read snapshot has no high watermark",
                snapshot_id=snapshot.snapshot_id,
            )
        selected = tuple(
            islice(
                iter(
                    load_candidates(
                        after_position,
                        retained_high_watermark,
                        self._page_size + 1,
                    )
                ),
                self._page_size + 2,
            )
        )
        if len(selected) > self._page_size + 1:
            raise QueryTooBroad(
                "immutable source returned more than the requested bounded page",
                requested_limit=self._page_size + 1,
            )
        candidates = tuple(self._candidate(candidate) for candidate in selected)
        positions = tuple(candidate.source_position for candidate in candidates)
        resource_ids = tuple(candidate.resource_id for candidate in candidates)
        if positions != tuple(sorted(positions)) or len(positions) != len(set(positions)):
            raise IntegrityViolation("immutable read candidates are not in stable order")
        if len(resource_ids) != len(set(resource_ids)):
            raise IntegrityViolation("immutable read candidate resource ids are not unique")
        if after_position is not None and positions and positions[0] <= after_position:
            raise IntegrityViolation(
                "immutable read source repeated or reordered its cursor anchor"
            )
        if any(candidate.observed_sequence > retained_high_watermark for candidate in candidates):
            raise IntegrityViolation(
                "immutable read source returned a row above the retained high watermark"
            )

        page_candidates = candidates[: self._page_size]
        next_cursor = None
        if len(candidates) > self._page_size:
            if not page_candidates:  # pragma: no cover - page_size is positive
                raise IntegrityViolation("immutable read source could not advance its cursor")
            next_cursor = self._cursor_signer.issue(
                snapshot=snapshot,
                position=_immutable_position(
                    source_position=page_candidates[-1].source_position,
                    principal_binding=exact_binding.principal_binding,
                ),
                page_size=self._page_size,
            )
        return PageV1[T](
            read_snapshot_id=snapshot.snapshot_id,
            items=tuple(candidate.item for candidate in page_candidates),
            next_cursor=next_cursor,
            expires_at=snapshot.expires_at,
        )

    @staticmethod
    def _binding(binding: ImmutableReadBinding) -> ImmutableReadBinding:
        if not isinstance(binding, ImmutableReadBinding):
            raise TypeError("binding must be ImmutableReadBinding")
        return ImmutableReadBinding.model_validate(binding.model_dump(mode="json"))

    @staticmethod
    def _candidate(candidate: ImmutableReadCandidate[T]) -> ImmutableReadCandidate[T]:
        if not isinstance(candidate, ImmutableReadCandidate):
            raise IntegrityViolation("immutable read candidate has an invalid type")
        try:
            # Revalidate through the concrete parametrized model so typed payloads
            # such as ArtifactV2 and RefValue do not degrade into plain mappings.
            return type(candidate).model_validate(candidate.model_dump(mode="python"))
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("immutable read candidate is invalid") from exc

    def _create_snapshot(
        self,
        *,
        binding: ImmutableReadBinding,
        high_watermark: int,
    ) -> ReadSnapshotV1:
        if (
            isinstance(high_watermark, bool)
            or not isinstance(high_watermark, int)
            or high_watermark < 0
        ):
            raise IntegrityViolation("immutable source high watermark must be non-negative")
        snapshot_id = self._snapshot_id_factory()
        if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 512:
            raise IntegrityViolation("immutable read snapshot id factory returned an invalid id")
        created_at = _utc_now(self._clock)
        snapshot = ReadSnapshotV1(
            snapshot_id=snapshot_id,
            resource_kind=binding.resource_kind,
            query_hash=binding.query_hash,
            authz_fingerprint=binding.authz_fingerprint,
            stable_sort_schema_id=binding.stable_sort_schema_id,
            strategy="immutable_high_watermark",
            high_watermark=high_watermark,
            created_at=_utc_text(created_at),
            expires_at=_utc_text(created_at + self._snapshot_ttl),
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
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise IntegrityViolation(
                "immutable read snapshot could not be persisted",
                snapshot_id=snapshot.snapshot_id,
            ) from exc
        return snapshot

    @staticmethod
    def _require_snapshot_metadata(
        snapshot: ReadSnapshotV1,
        binding: ImmutableReadBinding,
    ) -> None:
        if (
            snapshot.resource_kind != binding.resource_kind
            or snapshot.stable_sort_schema_id != binding.stable_sort_schema_id
            or snapshot.strategy != "immutable_high_watermark"
        ):
            raise IntegrityViolation(
                "stored immutable read snapshot metadata is invalid",
                snapshot_id=snapshot.snapshot_id,
            )


class SqlMaterializedReadViewRepository:
    """Persist complete mutable list projections without owning commit or rollback.

    Candidates must already be filtered, authorized, projected, and stably ordered by
    the resource-specific query service. This adapter preserves that exact canonical
    sequence and never reloads mutable source rows while a cursor is being resumed.
    """

    def __init__(
        self,
        session: Session,
        *,
        cursor_signer: CursorSigner,
        clock: UtcClock,
        page_size: int = 100,
        snapshot_ttl: timedelta = timedelta(minutes=5),
        max_materialized_snapshot_items: int,
        snapshot_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_ITEMS:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_ITEMS}")
        if snapshot_ttl <= timedelta(0):
            raise ValueError("snapshot_ttl must be positive")
        if (
            isinstance(max_materialized_snapshot_items, bool)
            or not isinstance(max_materialized_snapshot_items, int)
            or max_materialized_snapshot_items < 1
        ):
            raise ValueError("max_materialized_snapshot_items must be positive")
        if snapshot_id_factory is not None and not callable(snapshot_id_factory):
            raise TypeError("snapshot_id_factory must be callable")
        self._session = session
        self._cursor_signer = cursor_signer
        self._clock = clock
        self._page_size = page_size
        self._snapshot_ttl = snapshot_ttl
        self._max_items = max_materialized_snapshot_items
        self._snapshot_id_factory = snapshot_id_factory or (
            lambda: f"read-snapshot:materialized:{uuid.uuid4().hex}"
        )

    def create(
        self,
        candidates: Iterable[MaterializedReadCandidate],
        *,
        binding: MaterializedReadBinding,
    ) -> PageV1[MaterializedReadItemV1]:
        exact_binding = self._binding(binding)
        selected = tuple(islice(iter(candidates), self._max_items + 1))
        if len(selected) > self._max_items:
            raise QueryTooBroad(
                "materialized snapshot exceeds the configured item limit",
                max_materialized_snapshot_items=self._max_items,
            )
        exact_candidates = tuple(self._candidate(item) for item in selected)
        resource_ids = tuple(item.resource_id for item in exact_candidates)
        if len(resource_ids) != len(set(resource_ids)):
            raise IntegrityViolation(
                "materialized read candidate resource_id values must be unique"
            )

        snapshot_id = self._snapshot_id_factory()
        if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 512:
            raise IntegrityViolation("materialized read snapshot id factory returned an invalid id")
        created_at = _utc_now(self._clock)
        snapshot = ReadSnapshotV1(
            snapshot_id=snapshot_id,
            resource_kind=exact_binding.resource_kind,
            query_hash=exact_binding.query_hash,
            authz_fingerprint=exact_binding.authz_fingerprint,
            stable_sort_schema_id=exact_binding.stable_sort_schema_id,
            strategy="materialized_view",
            materialized_item_count=len(exact_candidates),
            created_at=_utc_text(created_at),
            expires_at=_utc_text(created_at + self._snapshot_ttl),
        )
        items = tuple(
            MaterializedReadItemV1(
                snapshot_id=snapshot.snapshot_id,
                ordinal=ordinal,
                resource_id=candidate.resource_id,
                observed_revision=candidate.observed_revision,
                view_schema_id=exact_binding.view_schema_id,
                canonical_view=candidate.canonical_view,
                view_hash=canonical_sha256(candidate.canonical_view),
            )
            for ordinal, candidate in enumerate(exact_candidates, start=1)
        )

        try:
            # There is no ORM relationship between the generic snapshot and item
            # rows, so establish the FK parent explicitly before batching items.
            # Both flushes still belong to the caller's single transaction.
            self._session.add(self._snapshot_row(snapshot))
            self._session.flush()
            self._session.add_all(self._item_row(item) for item in items)
            self._session.flush()
        except IntegrityError as exc:
            raise IntegrityViolation(
                "materialized read snapshot could not be persisted",
                snapshot_id=snapshot.snapshot_id,
            ) from exc
        return self._page_from_snapshot(snapshot, position=0, binding=exact_binding)

    def page(
        self,
        cursor: PageCursorV1,
        *,
        binding: MaterializedReadBinding,
    ) -> PageV1[MaterializedReadItemV1]:
        exact_binding = self._binding(binding)
        if not isinstance(cursor, PageCursorV1):
            raise CursorInvalid("materialized page cursor has an invalid schema")
        self._cursor_signer.verify_signature(cursor)
        row = self._session.get(ReadSnapshotRow, cursor.snapshot_id)
        if row is None:
            raise CursorExpired("materialized read snapshot is no longer retained")
        snapshot = _snapshot_from_row(row)
        self._cursor_signer.verify(
            cursor,
            expected_snapshot=snapshot,
            expected_query_hash=exact_binding.query_hash,
            requested_page_size=self._page_size,
            snapshot_is_retained=lambda snapshot_id: (
                self._session.get(ReadSnapshotRow, snapshot_id) is not None
            ),
        )
        position, retained_principal_binding = _decode_position(cursor.position)
        if retained_principal_binding != exact_binding.principal_binding:
            raise Forbidden("materialized cursor belongs to another principal")
        self._require_snapshot_metadata(snapshot, exact_binding)
        if snapshot.authz_fingerprint != exact_binding.authz_fingerprint:
            raise CursorExpired("materialized cursor authorization has changed")
        count = snapshot.materialized_item_count
        if count is None:  # guarded by ReadSnapshotV1, retained for type narrowing
            raise IntegrityViolation("materialized read snapshot has no item count")
        if position >= count:
            raise CursorInvalid("materialized cursor position is out of range")
        return self._page_from_snapshot(snapshot, position=position, binding=exact_binding)

    @staticmethod
    def _binding(binding: MaterializedReadBinding) -> MaterializedReadBinding:
        if not isinstance(binding, MaterializedReadBinding):
            raise TypeError("binding must be MaterializedReadBinding")
        return MaterializedReadBinding.model_validate(binding.model_dump(mode="json"))

    @staticmethod
    def _candidate(candidate: MaterializedReadCandidate) -> MaterializedReadCandidate:
        if not isinstance(candidate, MaterializedReadCandidate):
            raise IntegrityViolation("materialized read candidate has an invalid type")
        try:
            return MaterializedReadCandidate.model_validate(candidate.model_dump(mode="json"))
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("materialized read candidate is invalid") from exc

    @staticmethod
    def _snapshot_row(snapshot: ReadSnapshotV1) -> ReadSnapshotRow:
        return ReadSnapshotRow(
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

    @staticmethod
    def _item_row(item: MaterializedReadItemV1) -> MaterializedReadItemRow:
        wire = item.model_dump(mode="json")
        return MaterializedReadItemRow(
            snapshot_id=wire["snapshot_id"],
            ordinal=wire["ordinal"],
            resource_id=wire["resource_id"],
            observed_revision=wire["observed_revision"],
            view_schema_id=wire["view_schema_id"],
            canonical_view=wire["canonical_view"],
            view_hash=wire["view_hash"],
        )

    @staticmethod
    def _require_snapshot_metadata(
        snapshot: ReadSnapshotV1,
        binding: MaterializedReadBinding,
    ) -> None:
        if (
            snapshot.resource_kind != binding.resource_kind
            or snapshot.stable_sort_schema_id != binding.stable_sort_schema_id
            or snapshot.strategy != "materialized_view"
        ):
            raise IntegrityViolation(
                "stored materialized read snapshot metadata is invalid",
                snapshot_id=snapshot.snapshot_id,
            )

    def _page_from_snapshot(
        self,
        snapshot: ReadSnapshotV1,
        *,
        position: int,
        binding: MaterializedReadBinding,
    ) -> PageV1[MaterializedReadItemV1]:
        count = snapshot.materialized_item_count
        if count is None:
            raise IntegrityViolation(
                "materialized read snapshot has no item count",
                snapshot_id=snapshot.snapshot_id,
            )
        if position < 0 or position > count:
            raise CursorInvalid("materialized cursor position is out of range")
        if position:
            anchor = self._session.get(
                MaterializedReadItemRow,
                (snapshot.snapshot_id, position),
            )
            if anchor is None:
                raise CursorExpired("retained materialized view is incomplete")
            _item_from_row(
                anchor,
                snapshot_id=snapshot.snapshot_id,
                expected_view_schema_id=binding.view_schema_id,
            )

        rows = self._session.scalars(
            select(MaterializedReadItemRow)
            .where(
                MaterializedReadItemRow.snapshot_id == snapshot.snapshot_id,
                MaterializedReadItemRow.ordinal > position,
            )
            .order_by(MaterializedReadItemRow.ordinal)
            .limit(self._page_size + 1)
        ).all()
        expected_remaining = count - position
        required_rows = min(expected_remaining, self._page_size + 1)
        if len(rows) < required_rows:
            raise CursorExpired("retained materialized view is incomplete")
        if expected_remaining <= self._page_size and len(rows) > expected_remaining:
            raise IntegrityViolation(
                "materialized read rows exceed the retained snapshot item count",
                snapshot_id=snapshot.snapshot_id,
            )

        parsed: list[MaterializedReadItemV1] = []
        expected_ordinal = position + 1
        for row in rows:
            if row.ordinal != expected_ordinal:
                raise CursorExpired("retained materialized view has an ordinal gap")
            parsed.append(
                _item_from_row(
                    row,
                    snapshot_id=snapshot.snapshot_id,
                    expected_view_schema_id=binding.view_schema_id,
                )
            )
            expected_ordinal += 1

        page_items = tuple(parsed[: self._page_size])
        end_position = position + len(page_items)
        next_cursor = None
        if end_position < count:
            next_cursor = self._cursor_signer.issue(
                snapshot=snapshot,
                position=_position(
                    ordinal=end_position,
                    principal_binding=binding.principal_binding,
                ),
                page_size=self._page_size,
            )
        return PageV1[MaterializedReadItemV1](
            read_snapshot_id=snapshot.snapshot_id,
            items=page_items,
            next_cursor=next_cursor,
            expires_at=snapshot.expires_at,
        )


__all__ = [
    "ImmutableReadBinding",
    "ImmutableReadCandidate",
    "MaterializedReadBinding",
    "MaterializedReadCandidate",
    "SqlImmutableReadViewRepository",
    "SqlMaterializedReadViewRepository",
]
