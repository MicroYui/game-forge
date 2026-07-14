"""Immutable SQLite persistence for three-way merge conflict sets."""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import (
    canonical_sha256,
    sha256_lowerhex,
    typed_canonical_json,
)
from gameforge.contracts.diff import ConflictSet, ConflictSetContextV1, MergeConflict
from gameforge.contracts.errors import CursorInvalid, IntegrityViolation
from gameforge.contracts.storage import (
    MAX_PAGE_ITEMS,
    PageCursorV1,
    PageV1,
    ReadSnapshotV1,
    UtcClock,
    compute_page_query_hash,
)
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.models import ArtifactRow, ConflictSetRow, MergeConflictRow


_RESOURCE_KIND = "merge_conflicts"
_STABLE_SORT_SCHEMA_ID = "merge-conflict-json-pointer-asc@1"
_AUTHZ_FINGERPRINT = canonical_sha256({"scope": "conflict-set-repository-internal"})
_SNAPSHOT_PREFIX = "conflict-set-read"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ContractModel = TypeVar("_ContractModel", bound=BaseModel)


def _query_hash(conflict_set_id: str) -> str:
    return compute_page_query_hash(
        api_version="diff@1",
        resource_kind=_RESOURCE_KIND,
        filters={"conflict_set_id": conflict_set_id},
        stable_sort=("path:asc",),
        page_projection=(
            "id",
            "path",
            "kind",
            "base",
            "current",
            "proposed",
            "allowed_resolutions",
        ),
    )


def _require_identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _same_wire(left: BaseModel, right: BaseModel) -> bool:
    return typed_canonical_json(left.model_dump(mode="python")) == typed_canonical_json(
        right.model_dump(mode="python")
    )


def _content_digest(value: MergeConflict) -> str:
    payload = {
        "digest_schema_version": "merge-conflict-content@1",
        "conflict": value.model_dump(mode="json"),
    }
    return sha256_lowerhex(typed_canonical_json(payload).encode("utf-8"))


def _set_content_digest(
    conflict_set: ConflictSet,
    context: ConflictSetContextV1,
    conflicts: tuple[MergeConflict, ...],
) -> str:
    payload = {
        "digest_schema_version": "conflict-set-content@1",
        "conflict_set": conflict_set.model_dump(mode="json"),
        "context": context.model_dump(mode="json"),
        "conflict_digests": [_content_digest(item) for item in conflicts],
    }
    return sha256_lowerhex(typed_canonical_json(payload).encode("utf-8"))


def _require_digest(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise IntegrityViolation(f"{label} must be a lowercase SHA-256 digest")
    return value


def _revalidate(
    value: _ContractModel,
    model_type: type[_ContractModel],
    *,
    label: str,
) -> _ContractModel:
    if type(value) is not model_type or set(value.__dict__) != set(model_type.model_fields):
        raise IntegrityViolation(f"{label} must be a canonical {model_type.__name__}")
    wire = value.model_dump(mode="python")
    try:
        parsed = model_type.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"{label} wire is invalid") from exc
    if typed_canonical_json(parsed.model_dump(mode="python")) != typed_canonical_json(wire):
        raise IntegrityViolation(f"{label} wire is not canonical")
    return parsed


def _parse_utc(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise IntegrityViolation(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrityViolation(f"{label} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset() != timedelta(0):
        raise IntegrityViolation(f"{label} must be a UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _utc_now(clock: UtcClock) -> datetime:
    try:
        now = clock.now_utc()
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("conflict repository clock must return UTC") from exc
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("conflict repository clock must return UTC")
    return now.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _conflict_set_from_row(row: ConflictSetRow) -> ConflictSet:
    wire = {
        "schema_version": row.schema_version,
        "id": row.conflict_set_id,
        "base_snapshot_id": row.base_snapshot_id,
        "current_snapshot_id": row.current_snapshot_id,
        "proposed_patch_artifact_id": row.proposed_patch_artifact_id,
        "expected_ref_revision": row.expected_ref_revision,
        "conflict_count": row.conflict_count,
        "non_conflicting_ops_digest": row.non_conflicting_ops_digest,
        "created_at": row.created_at,
    }
    try:
        parsed = ConflictSet.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored ConflictSet is invalid",
            conflict_set_id=row.conflict_set_id,
        ) from exc
    if typed_canonical_json(parsed.model_dump(mode="python")) != typed_canonical_json(wire):
        raise IntegrityViolation(
            "stored ConflictSet is noncanonical",
            conflict_set_id=row.conflict_set_id,
        )
    _parse_utc(parsed.created_at, label="ConflictSet.created_at")
    return parsed


def _context_from_row(row: ConflictSetRow) -> ConflictSetContextV1:
    try:
        parsed = ConflictSetContextV1.model_validate(row.context)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored ConflictSet context is invalid",
            conflict_set_id=row.conflict_set_id,
        ) from exc
    if typed_canonical_json(parsed.model_dump(mode="python")) != typed_canonical_json(row.context):
        raise IntegrityViolation(
            "stored ConflictSet context is noncanonical",
            conflict_set_id=row.conflict_set_id,
        )
    return parsed


def _conflict_from_row(row: MergeConflictRow) -> MergeConflict:
    wire = {
        "id": row.conflict_id,
        "path": row.path,
        "kind": row.kind,
        "base": row.base,
        "current": row.current,
        "proposed": row.proposed,
        "allowed_resolutions": row.allowed_resolutions,
    }
    try:
        parsed = MergeConflict.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored MergeConflict is invalid",
            conflict_set_id=row.conflict_set_id,
            ordinal=row.ordinal,
        ) from exc
    if typed_canonical_json(parsed.model_dump(mode="python")) != typed_canonical_json(wire):
        raise IntegrityViolation(
            "stored MergeConflict is noncanonical",
            conflict_set_id=row.conflict_set_id,
            ordinal=row.ordinal,
        )
    retained_digest = _require_digest(
        row.content_digest,
        label="stored MergeConflict content digest",
    )
    if retained_digest != _content_digest(parsed):
        raise IntegrityViolation(
            "stored MergeConflict content digest does not match its payload",
            conflict_set_id=row.conflict_set_id,
            ordinal=row.ordinal,
        )
    return parsed


class SqlConflictSetRepository:
    """Persist immutable ConflictSets and expose bounded signed conflict pages."""

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
            raise ValueError("SqlConflictSetRepository requires a SQLite session")
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
        conflict_set: ConflictSet,
        context: ConflictSetContextV1,
        conflicts: tuple[MergeConflict, ...],
    ) -> ConflictSet:
        candidate = _revalidate(conflict_set, ConflictSet, label="ConflictSet")
        candidate_context = _revalidate(
            context,
            ConflictSetContextV1,
            label="ConflictSet context",
        )
        candidate_conflicts = self._validate_conflicts(candidate, conflicts)
        self._validate_context_closure(candidate, candidate_context)
        self._require_patch_artifact(candidate)
        candidate_content_digest = _set_content_digest(
            candidate,
            candidate_context,
            candidate_conflicts,
        )

        retained = self._load_validated(candidate.id)
        if retained is not None:
            self._require_exact_replay(
                candidate,
                candidate_context,
                candidate_conflicts,
                retained,
            )
            return retained[0]

        result = self._session.execute(
            sqlite_insert(ConflictSetRow)
            .values(
                conflict_set_id=candidate.id,
                schema_version=candidate.schema_version,
                base_snapshot_id=candidate.base_snapshot_id,
                current_snapshot_id=candidate.current_snapshot_id,
                proposed_patch_artifact_id=candidate.proposed_patch_artifact_id,
                expected_ref_revision=candidate.expected_ref_revision,
                conflict_count=candidate.conflict_count,
                non_conflicting_ops_digest=candidate.non_conflicting_ops_digest,
                created_at=candidate.created_at,
                context=candidate_context.model_dump(mode="json"),
                content_digest=candidate_content_digest,
            )
            .on_conflict_do_nothing(index_elements=[ConflictSetRow.conflict_set_id])
        )
        if result.rowcount != 1:
            self._session.expire_all()
            retained = self._load_validated(candidate.id)
            if retained is None:
                raise IntegrityViolation(
                    "ConflictSet insert conflicted without a retained row",
                    conflict_set_id=candidate.id,
                )
            self._require_exact_replay(
                candidate,
                candidate_context,
                candidate_conflicts,
                retained,
            )
            return retained[0]

        self._session.add_all(
            [
                MergeConflictRow(
                    conflict_set_id=candidate.id,
                    ordinal=ordinal,
                    conflict_id=conflict.id,
                    path=conflict.path,
                    kind=conflict.kind,
                    base=conflict.base.model_dump(mode="json"),
                    current=conflict.current.model_dump(mode="json"),
                    proposed=conflict.proposed.model_dump(mode="json"),
                    allowed_resolutions=list(conflict.allowed_resolutions),
                    content_digest=_content_digest(conflict),
                )
                for ordinal, conflict in enumerate(candidate_conflicts, start=1)
            ]
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise IntegrityViolation(
                "ConflictSet publication violated storage integrity",
                conflict_set_id=candidate.id,
            ) from exc
        return candidate

    def get(self, conflict_set_id: str) -> ConflictSet | None:
        retained = self._load_validated(conflict_set_id)
        return None if retained is None else retained[0]

    def get_context(self, conflict_set_id: str) -> ConflictSetContextV1 | None:
        retained = self._load_validated(conflict_set_id)
        return None if retained is None else retained[1]

    def load_bounded(
        self,
        conflict_set_id: str,
    ) -> tuple[ConflictSet, ConflictSetContextV1, tuple[MergeConflict, ...]] | None:
        """Load one contract-bounded immutable set with a single row scan."""

        return self._load_validated(conflict_set_id)

    def page_conflicts(
        self,
        conflict_set_id: str,
        cursor: PageCursorV1 | None = None,
    ) -> PageV1[MergeConflict]:
        identifier = _require_identifier(conflict_set_id, field_name="conflict_set_id")
        validated = self._load_validated(identifier)
        if validated is None:
            raise KeyError(identifier)
        conflict_set, context, conflicts = validated
        content_digest = _set_content_digest(conflict_set, context, conflicts)
        expected_query_hash = _query_hash(identifier)

        if cursor is None:
            snapshot = self._new_snapshot(
                conflict_set,
                expected_query_hash,
                content_digest,
            )
            position = 0
        else:
            self._cursor_signer.verify_signature(cursor)
            snapshot = self._snapshot_from_id(
                conflict_set,
                expected_query_hash,
                cursor.snapshot_id,
                content_digest,
            )
            self._cursor_signer.verify(
                cursor,
                expected_snapshot=snapshot,
                expected_query_hash=expected_query_hash,
                requested_page_size=self._page_size,
                snapshot_is_retained=lambda snapshot_id: (
                    snapshot_id == snapshot.snapshot_id
                    and self._session.get(ConflictSetRow, identifier) is not None
                ),
            )
            if not cursor.position.isascii() or not cursor.position.isdecimal():
                raise CursorInvalid("conflict cursor position is invalid")
            position = int(cursor.position)

        high_watermark = snapshot.high_watermark
        if high_watermark is None:  # pragma: no cover - contract constructor guards this
            raise AssertionError("immutable ConflictSet snapshot lacks a high watermark")
        if position < 0 or position > high_watermark:
            raise CursorInvalid("conflict cursor position is out of range")

        rows = self._session.scalars(
            select(MergeConflictRow)
            .where(
                MergeConflictRow.conflict_set_id == identifier,
                MergeConflictRow.ordinal > position,
                MergeConflictRow.ordinal <= high_watermark,
            )
            .order_by(MergeConflictRow.ordinal)
            .limit(self._page_size + 1)
        ).all()
        parsed: list[MergeConflict] = []
        expected_ordinal = position + 1
        previous_path: str | None = None
        if position > 0:
            predecessor = self._session.get(MergeConflictRow, (identifier, position))
            if predecessor is None:
                raise IntegrityViolation(
                    "ConflictSet ordinal sequence is missing",
                    conflict_set_id=identifier,
                    ordinal=position,
                )
            previous_path = predecessor.path
        for row in rows:
            if row.ordinal != expected_ordinal:
                raise IntegrityViolation(
                    "ConflictSet ordinal sequence is missing",
                    conflict_set_id=identifier,
                    expected_ordinal=expected_ordinal,
                    actual_ordinal=row.ordinal,
                )
            conflict = _conflict_from_row(row)
            if previous_path is not None and conflict.path <= previous_path:
                raise IntegrityViolation(
                    "ConflictSet path order is invalid",
                    conflict_set_id=identifier,
                    ordinal=row.ordinal,
                )
            parsed.append(conflict)
            previous_path = conflict.path
            expected_ordinal += 1

        page_items = tuple(parsed[: self._page_size])
        end_position = position + len(page_items)
        if end_position < high_watermark and len(parsed) <= self._page_size:
            raise IntegrityViolation(
                "ConflictSet rows end before the immutable high watermark",
                conflict_set_id=identifier,
                high_watermark=high_watermark,
            )
        next_cursor = None
        if end_position < high_watermark:
            next_cursor = self._cursor_signer.issue(
                snapshot=snapshot,
                position=str(end_position),
                page_size=self._page_size,
            )
        return PageV1[MergeConflict](
            read_snapshot_id=snapshot.snapshot_id,
            items=page_items,
            next_cursor=next_cursor,
            expires_at=snapshot.expires_at,
        )

    def _load_validated(
        self,
        conflict_set_id: str,
    ) -> tuple[ConflictSet, ConflictSetContextV1, tuple[MergeConflict, ...]] | None:
        retained = self._load_metadata(conflict_set_id)
        if retained is None:
            return None
        conflict_set, context, retained_digest = retained
        conflicts = self._load_conflicts(conflict_set)
        if retained_digest != _set_content_digest(conflict_set, context, conflicts):
            raise IntegrityViolation(
                "stored ConflictSet content digest does not match its payload",
                conflict_set_id=conflict_set.id,
            )
        return conflict_set, context, conflicts

    def _load_metadata(
        self,
        conflict_set_id: str,
    ) -> tuple[ConflictSet, ConflictSetContextV1, str] | None:
        identifier = _require_identifier(conflict_set_id, field_name="conflict_set_id")
        row = self._session.get(ConflictSetRow, identifier)
        if row is None:
            return None
        conflict_set = _conflict_set_from_row(row)
        context = _context_from_row(row)
        self._validate_context_closure(conflict_set, context)
        self._require_patch_artifact(conflict_set)
        content_digest = _require_digest(
            row.content_digest,
            label="stored ConflictSet content digest",
        )
        return conflict_set, context, content_digest

    def _load_conflicts(self, conflict_set: ConflictSet) -> tuple[MergeConflict, ...]:
        rows = self._session.scalars(
            select(MergeConflictRow)
            .where(MergeConflictRow.conflict_set_id == conflict_set.id)
            .order_by(MergeConflictRow.ordinal)
            .limit(conflict_set.conflict_count + 1)
        ).all()
        if len(rows) != conflict_set.conflict_count:
            raise IntegrityViolation(
                "ConflictSet row count differs from conflict_count",
                conflict_set_id=conflict_set.id,
                expected_count=conflict_set.conflict_count,
                actual_count=len(rows),
            )
        conflicts: list[MergeConflict] = []
        identifiers: set[str] = set()
        previous_path: str | None = None
        for expected_ordinal, row in enumerate(rows, start=1):
            if row.ordinal != expected_ordinal:
                raise IntegrityViolation(
                    "ConflictSet ordinals are not contiguous",
                    conflict_set_id=conflict_set.id,
                    expected_ordinal=expected_ordinal,
                    actual_ordinal=row.ordinal,
                )
            conflict = _conflict_from_row(row)
            if conflict.id in identifiers:
                raise IntegrityViolation(
                    "ConflictSet conflict IDs must be unique",
                    conflict_set_id=conflict_set.id,
                )
            if previous_path is not None and conflict.path <= previous_path:
                raise IntegrityViolation(
                    "ConflictSet path order is invalid",
                    conflict_set_id=conflict_set.id,
                    ordinal=row.ordinal,
                )
            identifiers.add(conflict.id)
            previous_path = conflict.path
            conflicts.append(conflict)
        return tuple(conflicts)

    @staticmethod
    def _validate_conflicts(
        conflict_set: ConflictSet,
        conflicts: tuple[MergeConflict, ...],
    ) -> tuple[MergeConflict, ...]:
        if not isinstance(conflicts, tuple):
            raise IntegrityViolation("ConflictSet conflicts must be an immutable tuple")
        canonical = tuple(
            _revalidate(conflict, MergeConflict, label="MergeConflict") for conflict in conflicts
        )
        if len(canonical) != conflict_set.conflict_count:
            raise IntegrityViolation("ConflictSet conflict_count differs from supplied conflicts")
        paths = tuple(conflict.path for conflict in canonical)
        if paths != tuple(sorted(paths)):
            raise IntegrityViolation("ConflictSet conflicts must be sorted by JSON Pointer")
        identifiers = tuple(conflict.id for conflict in canonical)
        if len(identifiers) != len(set(identifiers)):
            raise IntegrityViolation("ConflictSet conflict IDs must be unique")
        if len(paths) != len(set(paths)):
            raise IntegrityViolation("ConflictSet conflict paths must be unique")
        return canonical

    @staticmethod
    def _validate_context_closure(
        conflict_set: ConflictSet,
        context: ConflictSetContextV1,
    ) -> None:
        if context.expected_subject_artifact_id != conflict_set.proposed_patch_artifact_id:
            raise IntegrityViolation(
                "ConflictSet context subject artifact differs from proposed Patch"
            )
        if context.expected_ref.revision != conflict_set.expected_ref_revision:
            raise IntegrityViolation(
                "ConflictSet context ref revision differs from expected_ref_revision"
            )

    def _require_patch_artifact(self, conflict_set: ConflictSet) -> None:
        artifact = self._session.get(ArtifactRow, conflict_set.proposed_patch_artifact_id)
        if artifact is None or artifact.kind != "patch":
            raise IntegrityViolation(
                "ConflictSet proposed Patch Artifact is unavailable",
                conflict_set_id=conflict_set.id,
                artifact_id=conflict_set.proposed_patch_artifact_id,
            )

    def _require_exact_replay(
        self,
        candidate: ConflictSet,
        context: ConflictSetContextV1,
        conflicts: tuple[MergeConflict, ...],
        retained: tuple[
            ConflictSet,
            ConflictSetContextV1,
            tuple[MergeConflict, ...],
        ],
    ) -> None:
        actual_set, actual_context, actual_conflicts = retained
        if (
            not _same_wire(actual_set, candidate)
            or not _same_wire(actual_context, context)
            or len(actual_conflicts) != len(conflicts)
            or any(
                not _same_wire(actual, expected)
                for actual, expected in zip(
                    actual_conflicts,
                    conflicts,
                    strict=True,
                )
            )
        ):
            raise IntegrityViolation(
                "ConflictSet id is already bound to different immutable content",
                conflict_set_id=candidate.id,
            )

    def _new_snapshot(
        self,
        conflict_set: ConflictSet,
        query_hash: str,
        content_digest: str,
    ) -> ReadSnapshotV1:
        created_at = _utc_now(self._clock)
        expires_at = created_at + self._snapshot_ttl
        micros = self._microseconds_since_epoch(created_at)
        snapshot_id = self._snapshot_id(
            conflict_set=conflict_set,
            query_hash=query_hash,
            content_digest=content_digest,
            created_at=created_at,
            micros=micros,
        )
        return ReadSnapshotV1(
            snapshot_id=snapshot_id,
            resource_kind=_RESOURCE_KIND,
            query_hash=query_hash,
            authz_fingerprint=_AUTHZ_FINGERPRINT,
            stable_sort_schema_id=_STABLE_SORT_SCHEMA_ID,
            strategy="immutable_high_watermark",
            high_watermark=conflict_set.conflict_count,
            created_at=_utc_text(created_at),
            expires_at=_utc_text(expires_at),
        )

    def _snapshot_from_id(
        self,
        conflict_set: ConflictSet,
        query_hash: str,
        snapshot_id: str,
        content_digest: str,
    ) -> ReadSnapshotV1:
        parts = snapshot_id.split(":")
        if (
            len(parts) != 3
            or parts[0] != _SNAPSHOT_PREFIX
            or not parts[1].isascii()
            or not parts[1].isdecimal()
            or len(parts[1]) > 20
        ):
            raise CursorInvalid("conflict cursor read snapshot identity is invalid")
        try:
            micros = int(parts[1])
        except ValueError as exc:  # pragma: no cover - bounded decimal guard above
            raise CursorInvalid("conflict cursor read snapshot time is invalid") from exc
        try:
            created_at = _EPOCH + timedelta(microseconds=micros)
        except OverflowError as exc:
            raise CursorInvalid("conflict cursor read snapshot time is invalid") from exc
        expected_id = self._snapshot_id(
            conflict_set=conflict_set,
            query_hash=query_hash,
            content_digest=content_digest,
            created_at=created_at,
            micros=micros,
        )
        if not hmac.compare_digest(snapshot_id, expected_id):
            raise CursorInvalid("conflict cursor belongs to another immutable snapshot")
        return ReadSnapshotV1(
            snapshot_id=snapshot_id,
            resource_kind=_RESOURCE_KIND,
            query_hash=query_hash,
            authz_fingerprint=_AUTHZ_FINGERPRINT,
            stable_sort_schema_id=_STABLE_SORT_SCHEMA_ID,
            strategy="immutable_high_watermark",
            high_watermark=conflict_set.conflict_count,
            created_at=_utc_text(created_at),
            expires_at=_utc_text(created_at + self._snapshot_ttl),
        )

    @staticmethod
    def _microseconds_since_epoch(value: datetime) -> int:
        delta = value - _EPOCH
        return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds

    @staticmethod
    def _snapshot_id(
        *,
        conflict_set: ConflictSet,
        query_hash: str,
        content_digest: str,
        created_at: datetime,
        micros: int,
    ) -> str:
        digest = canonical_sha256(
            {
                "resource_kind": _RESOURCE_KIND,
                "conflict_set_id": conflict_set.id,
                "query_hash": query_hash,
                "high_watermark": conflict_set.conflict_count,
                "non_conflicting_ops_digest": conflict_set.non_conflicting_ops_digest,
                "content_digest": content_digest,
                "created_at": _utc_text(created_at),
            }
        )
        return f"{_SNAPSHOT_PREFIX}:{micros}:{digest}"


__all__ = ["SqlConflictSetRepository"]
