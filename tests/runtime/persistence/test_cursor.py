from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from gameforge.contracts.errors import CursorExpired, CursorInvalid, IntegrityViolation
from gameforge.contracts.storage import (
    PageCursorV1,
    ReadSnapshotV1,
    compute_page_query_hash,
)
from gameforge.runtime.persistence.cursor import CursorSigner


QUERY_HASH = compute_page_query_hash(
    api_version="v1",
    resource_kind="runs",
    filters={"status": ["queued", "running"]},
    stable_sort=("created_at", "run_id"),
    page_projection=("run_id", "status", "revision"),
)
OTHER_QUERY_HASH = compute_page_query_hash(
    api_version="v1",
    resource_kind="runs",
    filters={"status": ["completed"]},
    stable_sort=("created_at", "run_id"),
    page_projection=("run_id", "status", "revision"),
)


@dataclass(frozen=True)
class FakeUtcClock:
    now: datetime

    def now_utc(self) -> datetime:
        return self.now


def _snapshot(
    *,
    snapshot_id: str = "read-snapshot:runs:1",
    query_hash: str = QUERY_HASH,
    created_at: str = "2026-07-13T12:00:00Z",
    expires_at: str = "2026-07-13T12:10:00Z",
) -> ReadSnapshotV1:
    return ReadSnapshotV1(
        snapshot_id=snapshot_id,
        resource_kind="runs",
        query_hash=query_hash,
        authz_fingerprint="a" * 64,
        stable_sort_schema_id="runs-created-at-id@1",
        strategy="immutable_high_watermark",
        high_watermark=17,
        created_at=created_at,
        expires_at=expires_at,
    )


def _signer(*, now: str = "2026-07-13T12:05:00+00:00") -> CursorSigner:
    return CursorSigner(
        signing_key=b"test-only-cursor-signing-key",
        clock=FakeUtcClock(datetime.fromisoformat(now)),
    )


def _issue(
    signer: CursorSigner,
    snapshot: ReadSnapshotV1,
    *,
    position: str = "created=2026-07-13T12:01:00Z;run=run-7",
    page_size: int = 25,
) -> PageCursorV1:
    return signer.issue(
        snapshot=snapshot,
        position=position,
        page_size=page_size,
    )


def _verify(
    signer: CursorSigner,
    cursor: PageCursorV1,
    snapshot: ReadSnapshotV1,
    *,
    expected_query_hash: str = QUERY_HASH,
    requested_page_size: int = 25,
    retained: bool = True,
) -> PageCursorV1:
    return signer.verify(
        cursor,
        expected_snapshot=snapshot,
        expected_query_hash=expected_query_hash,
        requested_page_size=requested_page_size,
        snapshot_is_retained=lambda snapshot_id: retained,
    )


@pytest.mark.parametrize("empty_key", [b"", bytearray(), memoryview(b"")])
def test_signer_rejects_empty_signing_key(empty_key: bytes) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        CursorSigner(
            signing_key=empty_key,
            clock=FakeUtcClock(datetime(2026, 7, 13, tzinfo=timezone.utc)),
        )


def test_issue_and_verify_bind_the_read_snapshot_query_and_page_size() -> None:
    signer = _signer()
    snapshot = _snapshot()
    cursor = _issue(signer, snapshot)
    retention_checks: list[str] = []

    verified = signer.verify(
        cursor,
        expected_snapshot=snapshot,
        expected_query_hash=QUERY_HASH,
        requested_page_size=25,
        snapshot_is_retained=lambda snapshot_id: retention_checks.append(snapshot_id) or True,
    )

    assert verified is cursor
    assert cursor.cursor_schema_version == "page-cursor@1"
    assert cursor.snapshot_id == snapshot.snapshot_id
    assert cursor.query_hash == QUERY_HASH
    assert cursor.page_size == 25
    assert len(cursor.opaque_signature) == 64
    assert retention_checks == [snapshot.snapshot_id]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("cursor_schema_version", "page-cursor@2"),
        ("snapshot_id", "read-snapshot:runs:other"),
        ("position", "created=2026-07-13T12:02:00Z;run=run-8"),
        ("page_size", 26),
        ("query_hash", "f" * 64),
    ],
)
def test_verify_rejects_tampering_with_every_signed_field(
    field: str,
    replacement: str | int,
) -> None:
    signer = _signer()
    snapshot = _snapshot()
    cursor = _issue(signer, snapshot)
    tampered = cursor.model_copy(update={field: replacement})

    with pytest.raises(CursorInvalid, match="signature"):
        _verify(signer, tampered, snapshot)


@pytest.mark.parametrize(
    "bad_signature",
    ["0" * 64, "not-hex", "A" * 64, "\u00e9" * 64],
)
def test_verify_rejects_tampered_signature(bad_signature: str) -> None:
    signer = _signer()
    snapshot = _snapshot()
    cursor = _issue(signer, snapshot)
    tampered = cursor.model_copy(update={"opaque_signature": bad_signature})

    with pytest.raises(CursorInvalid, match="signature"):
        _verify(signer, tampered, snapshot)


def test_verify_rejects_cursor_reuse_against_another_query() -> None:
    signer = _signer()
    cursor = _issue(signer, _snapshot())
    other_query_snapshot = _snapshot(query_hash=OTHER_QUERY_HASH)

    with pytest.raises(CursorInvalid, match="query"):
        _verify(
            signer,
            cursor,
            other_query_snapshot,
            expected_query_hash=OTHER_QUERY_HASH,
        )


def test_verify_rejects_when_snapshot_query_and_expected_query_differ() -> None:
    signer = _signer()
    snapshot = _snapshot()
    cursor = _issue(signer, snapshot)

    with pytest.raises(CursorInvalid, match="query"):
        _verify(
            signer,
            cursor,
            snapshot,
            expected_query_hash=OTHER_QUERY_HASH,
        )


def test_verify_treats_stored_snapshot_query_drift_as_integrity_failure() -> None:
    signer = _signer()
    snapshot = _snapshot()
    cursor = _issue(signer, snapshot)
    corrupted = _snapshot(query_hash=OTHER_QUERY_HASH)

    with pytest.raises(IntegrityViolation, match="stored read snapshot query"):
        _verify(signer, cursor, corrupted, expected_query_hash=QUERY_HASH)


def test_verify_rejects_cursor_reuse_against_another_snapshot() -> None:
    signer = _signer()
    snapshot = _snapshot()
    cursor = _issue(signer, snapshot)

    with pytest.raises(CursorInvalid, match="snapshot"):
        _verify(
            signer,
            cursor,
            _snapshot(snapshot_id="read-snapshot:runs:other"),
        )


def test_verify_rejects_a_different_requested_page_size() -> None:
    signer = _signer()
    snapshot = _snapshot()
    cursor = _issue(signer, snapshot)

    with pytest.raises(CursorInvalid, match="page size"):
        _verify(signer, cursor, snapshot, requested_page_size=50)


def test_verify_rejects_snapshot_at_its_expiry_boundary() -> None:
    signer = _signer(now="2026-07-13T12:10:00+00:00")
    snapshot = _snapshot()
    cursor = _issue(signer, snapshot)

    with pytest.raises(CursorExpired, match="expired"):
        _verify(signer, cursor, snapshot)


def test_verify_rejects_snapshot_missing_from_retention() -> None:
    signer = _signer()
    snapshot = _snapshot()
    cursor = _issue(signer, snapshot)

    with pytest.raises(CursorExpired, match="retention"):
        _verify(signer, cursor, snapshot, retained=False)


@pytest.mark.parametrize(
    ("created_at", "expires_at"),
    [
        ("not-a-timestamp", "2026-07-13T12:10:00Z"),
        ("2026-07-13T12:00:00Z", "not-a-timestamp"),
        ("2026-07-13T12:00:00", "2026-07-13T12:10:00Z"),
        ("2026-07-13T12:00:00Z", "2026-07-13T20:10:00+08:00"),
    ],
)
def test_verify_treats_unparseable_or_non_utc_snapshot_time_as_expired(
    created_at: str,
    expires_at: str,
) -> None:
    signer = _signer()
    snapshot = _snapshot(created_at=created_at, expires_at=expires_at)
    cursor = _issue(signer, snapshot)

    with pytest.raises(CursorExpired, match="UTC timestamp"):
        _verify(signer, cursor, snapshot)


def test_verify_treats_an_inverted_snapshot_lifetime_as_expired() -> None:
    signer = _signer()
    snapshot = _snapshot(
        created_at="2026-07-13T12:09:00Z",
        expires_at="2026-07-13T12:08:00Z",
    )
    cursor = _issue(signer, snapshot)

    with pytest.raises(CursorExpired, match="lifetime"):
        _verify(signer, cursor, snapshot)
