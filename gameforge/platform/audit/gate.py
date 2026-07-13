"""Platform gate for constructing and verifying authoritative audit records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import (
    AuditActor,
    AuditCorrelation,
    AuditRecordV2,
    AuditSubject,
    build_audit_record_v2,
)
from gameforge.contracts.storage import UtcClock
from gameforge.contracts.storage import AuditSink as AuditAppendSink


@runtime_checkable
class AuditChainHeadView(Protocol):
    @property
    def chain_id(self) -> str: ...

    @property
    def seq(self) -> int: ...

    @property
    def content_hash(self) -> str | None: ...

    @property
    def revision(self) -> int: ...


@runtime_checkable
class AuditGateStore(AuditAppendSink, Protocol):
    """Storage capabilities required in addition to the narrow append sink."""

    def lock_head(self, chain_id: str) -> AuditChainHeadView: ...

    def append(self, record: AuditRecordV2) -> AuditRecordV2: ...

    def verify_chain(self, chain_id: str) -> bool: ...


def _is_lower_hex_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _utc_text(clock: UtcClock) -> str:
    now = clock.now_utc()
    if not isinstance(now, datetime):
        raise IntegrityViolation("audit clock did not return a datetime")
    if now.tzinfo is None or now.utcoffset() is None or now.utcoffset() != timedelta(0):
        raise IntegrityViolation("audit clock must return a timezone-aware UTC datetime")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AuditGate:
    """Build the next record from the locked local chain head.

    The sink rechecks the head while appending, so a stale head becomes a
    typed conflict rather than an incorrectly linked record.
    """

    def __init__(self, *, sink: AuditGateStore, clock: UtcClock) -> None:
        self._sink = sink
        self._clock = clock

    def append(
        self,
        *,
        chain_id: str,
        actor: AuditActor,
        initiated_by: AuditActor | None,
        action: str,
        subject: AuditSubject,
        correlation: AuditCorrelation,
    ) -> AuditRecordV2:
        head = self._sink.lock_head(chain_id)
        if (
            head.chain_id != chain_id
            or isinstance(head.seq, bool)
            or not isinstance(head.seq, int)
            or head.seq < 0
            or isinstance(head.revision, bool)
            or not isinstance(head.revision, int)
            or head.revision != head.seq
            or (head.seq == 0 and head.content_hash is not None)
            or (head.seq > 0 and head.content_hash is None)
            or (head.content_hash is not None and not _is_lower_hex_sha256(head.content_hash))
        ):
            raise IntegrityViolation(
                "audit sink returned an invalid locked head",
                chain_id=chain_id,
            )
        record = build_audit_record_v2(
            chain_id=chain_id,
            seq=head.seq + 1,
            actor=actor,
            initiated_by=initiated_by,
            action=action,
            subject=subject,
            correlation=correlation,
            ts=_utc_text(self._clock),
            prev_hash=head.content_hash,
        )
        written = self._sink.append(record)
        if written != record:
            raise IntegrityViolation(
                "audit sink returned a different record than it appended",
                chain_id=chain_id,
                chain_seq=record.seq,
            )
        return written

    def verify_chain(self, chain_id: str) -> bool:
        verified = self._sink.verify_chain(chain_id)
        if verified is not True:
            raise IntegrityViolation(
                "audit sink did not verify the chain",
                chain_id=chain_id,
            )
        return True


__all__ = ["AuditChainHeadView", "AuditGate", "AuditGateStore"]
