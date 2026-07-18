"""Platform gate for constructing and verifying authoritative audit records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Protocol, runtime_checkable
from weakref import WeakKeyDictionary, WeakSet

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

    def append_preflighted(
        self,
        record: AuditRecordV2,
        expected_head: "PreparedAuditHead",
    ) -> AuditRecordV2: ...

    def register_before_commit_guard(self, guard: Callable[[], None]) -> None: ...

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


@dataclass(frozen=True, slots=True)
class PreparedAuditHead:
    chain_id: str
    seq: int
    content_hash: str | None
    revision: int


def _validated_prepared_head(
    raw_head: AuditChainHeadView,
    *,
    chain_id: str,
) -> PreparedAuditHead:
    raw_chain_id = getattr(raw_head, "chain_id", None)
    seq = getattr(raw_head, "seq", None)
    content_hash = getattr(raw_head, "content_hash", None)
    revision = getattr(raw_head, "revision", None)
    if (
        raw_chain_id != chain_id
        or isinstance(seq, bool)
        or not isinstance(seq, int)
        or seq < 0
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision != seq
        or (seq == 0 and content_hash is not None)
        or (seq > 0 and content_hash is None)
        or (content_hash is not None and not _is_lower_hex_sha256(content_hash))
    ):
        raise IntegrityViolation(
            "audit sink returned an invalid locked head",
            chain_id=chain_id,
        )
    return PreparedAuditHead(
        chain_id=raw_chain_id,
        seq=seq,
        content_hash=content_hash,
        revision=revision,
    )


@dataclass(frozen=True, slots=True)
class AuditAppendIntent:
    actor: AuditActor
    initiated_by: AuditActor | None
    action: str
    subject: AuditSubject
    correlation: AuditCorrelation


_AUDIT_BATCH_SEAL = object()


@dataclass(frozen=True, slots=True)
class _AuditBatchState:
    sink: AuditGateStore
    transaction_identity: tuple[object, object] | None
    records: tuple[AuditRecordV2, ...]
    heads: tuple[PreparedAuditHead, ...]
    sink_batch: object | None
    require_batch: bool


def _audit_transaction_identity(
    sink: AuditGateStore,
) -> tuple[object, object] | None:
    """Read an optional SQL transaction capability without importing runtime."""

    try:
        session = object.__getattribute__(sink, "_session")
    except AttributeError:
        return None
    get_nested = getattr(session, "get_nested_transaction", None)
    get_transaction = getattr(session, "get_transaction", None)
    transaction = (get_nested() if callable(get_nested) else None) or (
        get_transaction() if callable(get_transaction) else None
    )
    if transaction is None or not getattr(transaction, "is_active", False):
        raise IntegrityViolation("audit batch requires an active transaction")
    return session, transaction


class PreflightedAuditBatch:
    """Data-free handle for one transaction-bound prepared audit batch."""

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        *,
        sink: AuditGateStore,
        records: tuple[AuditRecordV2, ...],
        heads: tuple[PreparedAuditHead, ...],
        sink_batch: object | None,
        require_batch: bool,
        _seal: object,
    ) -> None:
        if _seal is not _AUDIT_BATCH_SEAL:
            raise TypeError("audit batch is authority-issued only")
        state = _AuditBatchState(
            sink=sink,
            transaction_identity=_audit_transaction_identity(sink),
            records=records,
            heads=heads,
            sink_batch=sink_batch,
            require_batch=require_batch,
        )
        with _AUDIT_BATCH_STATE_LOCK:
            _AUDIT_BATCH_STATES[self] = state

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("audit batch is immutable")

    def consume(
        self, sink: AuditGateStore
    ) -> tuple[
        tuple[AuditRecordV2, ...],
        tuple[PreparedAuditHead, ...],
        object | None,
        bool,
    ]:
        with _AUDIT_BATCH_STATE_LOCK:
            state = _AUDIT_BATCH_STATES.get(self)
            current_identity = _audit_transaction_identity(sink)
            same_transaction = (
                state is not None
                and (state.transaction_identity is None) == (current_identity is None)
                and (
                    state.transaction_identity is None
                    or current_identity is not None
                    and all(
                        retained is current
                        for retained, current in zip(
                            state.transaction_identity,
                            current_identity,
                            strict=True,
                        )
                    )
                )
            )
            if (
                state is None
                or state.sink is not sink
                or not same_transaction
                or self in _CONSUMED_AUDIT_BATCHES
            ):
                raise IntegrityViolation("audit batch is invalid, cross-transaction, or reused")
            _CONSUMED_AUDIT_BATCHES.add(self)
        return state.records, state.heads, state.sink_batch, state.require_batch


_AUDIT_BATCH_STATE_LOCK = Lock()
_AUDIT_BATCH_STATES: WeakKeyDictionary[PreflightedAuditBatch, _AuditBatchState] = (
    WeakKeyDictionary()
)
_CONSUMED_AUDIT_BATCHES: WeakSet[PreflightedAuditBatch] = WeakSet()


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
        head = _validated_prepared_head(
            self._sink.lock_head(chain_id),
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

    def prepare_batch(
        self,
        *,
        chain_id: str,
        intents: tuple[AuditAppendIntent, ...],
        require_batch: bool = False,
    ) -> PreflightedAuditBatch:
        current = _validated_prepared_head(
            self._sink.lock_head(chain_id),
            chain_id=chain_id,
        )
        records: list[AuditRecordV2] = []
        heads: list[PreparedAuditHead] = []
        for intent in intents:
            heads.append(current)
            record = build_audit_record_v2(
                chain_id=chain_id,
                seq=current.seq + 1,
                actor=intent.actor,
                initiated_by=intent.initiated_by,
                action=intent.action,
                subject=intent.subject,
                correlation=intent.correlation,
                ts=_utc_text(self._clock),
                prev_hash=current.content_hash,
            )
            records.append(record)
            current = PreparedAuditHead(
                chain_id=chain_id,
                seq=record.seq,
                content_hash=record.content_hash,
                revision=current.revision + 1,
            )
        sealed_records = tuple(record.model_copy(deep=True) for record in records)
        sealed_heads = tuple(
            PreparedAuditHead(
                chain_id=head.chain_id,
                seq=head.seq,
                content_hash=head.content_hash,
                revision=head.revision,
            )
            for head in heads
        )
        prepare_sink_batch = getattr(self._sink, "prepare_preflighted_batch", None)
        append_sink_batch = getattr(self._sink, "append_preflighted_batch", None)
        if callable(prepare_sink_batch) != callable(append_sink_batch):
            raise IntegrityViolation("audit sink batch capability is partial")
        if require_batch and not callable(prepare_sink_batch):
            raise IntegrityViolation("terminal Audit requires batch sink authority")
        sink_batch = (
            prepare_sink_batch(sealed_records, sealed_heads)
            if callable(prepare_sink_batch)
            else None
        )
        return PreflightedAuditBatch(
            sink=self._sink,
            records=sealed_records,
            heads=sealed_heads,
            sink_batch=sink_batch,
            require_batch=require_batch,
            _seal=_AUDIT_BATCH_SEAL,
        )

    def apply_prepared_batch(self, prepared: PreflightedAuditBatch) -> None:
        records, heads, sink_batch, require_batch = prepared.consume(self._sink)
        append_batch = getattr(self._sink, "append_preflighted_batch", None)
        if callable(append_batch):
            written = append_batch(records, heads, sink_batch)
            if written is not records:
                raise IntegrityViolation("audit sink applied another prepared batch")
            return
        if require_batch:
            raise IntegrityViolation("terminal Audit lost its batch sink authority")
        for record, head in zip(records, heads, strict=True):
            if self._sink.append_preflighted(record, head) != record:
                raise IntegrityViolation("audit sink applied another prepared record")

    def register_before_commit_guard(self, guard: Callable[[], None]) -> None:
        """Require a transaction-bound invariant immediately before commit."""

        if not callable(guard):
            raise TypeError("audit before-commit guard must be callable")
        self._sink.register_before_commit_guard(guard)

    def verify_chain(self, chain_id: str) -> bool:
        verified = self._sink.verify_chain(chain_id)
        if verified is not True:
            raise IntegrityViolation(
                "audit sink did not verify the chain",
                chain_id=chain_id,
            )
        return True


__all__ = [
    "AuditAppendIntent",
    "AuditChainHeadView",
    "AuditGate",
    "AuditGateStore",
    "PreflightedAuditBatch",
    "PreparedAuditHead",
]
