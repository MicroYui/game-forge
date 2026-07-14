"""Signed cursor binding for retained read snapshots."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Callable

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import CursorExpired, CursorInvalid, IntegrityViolation
from gameforge.contracts.storage import PageCursorV1, ReadSnapshotV1, UtcClock


SnapshotRetentionCheck = Callable[[str], bool]


def _parse_utc_timestamp(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise CursorExpired("read snapshot has an invalid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CursorExpired("read snapshot has an invalid UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _require_utc_now(clock: UtcClock) -> datetime:
    now = clock.now_utc()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise CursorExpired("cursor clock did not provide a UTC timestamp")
    return now.astimezone(timezone.utc)


class CursorSigner:
    """Issue and verify HMAC-bound page cursors without transport encoding."""

    __slots__ = ("_clock", "_signing_key")

    def __init__(
        self,
        *,
        signing_key: bytes | bytearray | memoryview,
        clock: UtcClock,
    ) -> None:
        if not isinstance(signing_key, (bytes, bytearray, memoryview)):
            raise TypeError("signing_key must be bytes-like")
        immutable_key = bytes(signing_key)
        if not immutable_key:
            raise ValueError("signing_key must be non-empty")
        self._signing_key = immutable_key
        self._clock = clock

    @staticmethod
    def _signature_payload(
        *,
        cursor_schema_version: str,
        snapshot_id: str,
        position: str,
        page_size: int,
        query_hash: str,
    ) -> dict[str, str | int]:
        return {
            "cursor_schema_version": cursor_schema_version,
            "snapshot_id": snapshot_id,
            "position": position,
            "page_size": page_size,
            "query_hash": query_hash,
        }

    def _signature(self, payload: dict[str, str | int]) -> str:
        return hmac.new(
            self._signing_key,
            canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def issue(
        self,
        *,
        snapshot: ReadSnapshotV1,
        position: str,
        page_size: int,
    ) -> PageCursorV1:
        payload = self._signature_payload(
            cursor_schema_version="page-cursor@1",
            snapshot_id=snapshot.snapshot_id,
            position=position,
            page_size=page_size,
            query_hash=snapshot.query_hash,
        )
        return PageCursorV1(
            **payload,
            opaque_signature=self._signature(payload),
        )

    def verify(
        self,
        cursor: PageCursorV1,
        *,
        expected_snapshot: ReadSnapshotV1,
        expected_query_hash: str,
        requested_page_size: int,
        snapshot_is_retained: SnapshotRetentionCheck,
    ) -> PageCursorV1:
        self.verify_signature(cursor)
        if cursor.snapshot_id != expected_snapshot.snapshot_id:
            raise CursorInvalid("cursor belongs to another read snapshot")
        if cursor.query_hash != expected_query_hash:
            raise CursorInvalid("cursor belongs to another query")
        if expected_snapshot.query_hash != cursor.query_hash:
            raise IntegrityViolation(
                "stored read snapshot query binding differs from its signed cursor",
                snapshot_id=expected_snapshot.snapshot_id,
            )
        if cursor.page_size != requested_page_size:
            raise CursorInvalid("cursor page size differs from the request")

        created_at = _parse_utc_timestamp(expected_snapshot.created_at)
        expires_at = _parse_utc_timestamp(expected_snapshot.expires_at)
        if created_at > expires_at:
            raise CursorExpired("read snapshot has an invalid lifetime")
        if _require_utc_now(self._clock) >= expires_at:
            raise CursorExpired("read snapshot has expired")
        if not snapshot_is_retained(expected_snapshot.snapshot_id):
            raise CursorExpired("read snapshot retention is no longer available")
        return cursor

    def verify_signature(self, cursor: PageCursorV1) -> PageCursorV1:
        """Authenticate the complete cursor before using its snapshot identifier."""

        if not isinstance(cursor, PageCursorV1):
            raise CursorInvalid("page cursor has an invalid schema")
        payload = self._signature_payload(
            cursor_schema_version=cursor.cursor_schema_version,
            snapshot_id=cursor.snapshot_id,
            position=cursor.position,
            page_size=cursor.page_size,
            query_hash=cursor.query_hash,
        )
        expected_signature = self._signature(payload)
        if (
            len(cursor.opaque_signature) != 64
            or any(character not in "0123456789abcdef" for character in cursor.opaque_signature)
            or not hmac.compare_digest(cursor.opaque_signature, expected_signature)
        ):
            raise CursorInvalid("cursor signature is invalid")
        if cursor.cursor_schema_version != "page-cursor@1":
            raise CursorInvalid("cursor schema version is unsupported")
        return cursor
