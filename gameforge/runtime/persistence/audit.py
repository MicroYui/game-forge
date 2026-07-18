"""Transaction-bound SQLite persistence for the ``audit@2`` hash chain."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from threading import Lock
from weakref import WeakKeyDictionary, WeakSet

from pydantic import BaseModel, ValidationError
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.lineage import AuditRecordV2
from gameforge.contracts.versions import AUDIT_SCHEMA_VERSION_V2
from gameforge.runtime.persistence.models import AuditHeadRow, AuditRow


_INSERT_AUDIT_BATCH_SQL = """
INSERT INTO audit (
    audit_schema_version,
    actor,
    action,
    artifact_id,
    ts,
    content_hash,
    prev_hash,
    chain_id,
    chain_seq,
    actor_v2,
    initiated_by,
    subject,
    correlation
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_AUDIT_HEAD_SQL = """
INSERT INTO audit_heads (
    chain_id,
    head_seq,
    head_hash,
    revision,
    updated_at
) VALUES (?, ?, ?, ?, ?)
"""
_UPDATE_AUDIT_HEAD_SQL = """
UPDATE audit_heads
SET head_seq = ?,
    head_hash = ?,
    revision = ?,
    updated_at = ?
WHERE chain_id = ?
  AND head_seq = ?
  AND head_hash = ?
  AND revision = ?
"""


@dataclass(frozen=True, slots=True)
class AuditChainHead:
    chain_id: str
    seq: int
    content_hash: str | None
    revision: int


class _PreparedExecutemanyParameters(list[tuple[object, ...]]):
    """Driver-compatible list whose preflighted rows cannot be changed."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("prepared Audit executemany parameters are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


@dataclass(frozen=True, slots=True)
class _SqlAuditBatchState:
    """Exact SQLite parameters retained outside an opaque one-shot handle."""

    owner: SqlAuditSink
    session: Session
    transaction: object
    records: tuple[AuditRecordV2, ...]
    expected_heads: tuple[object, ...]
    chain_id: str | None
    first_seq: int
    first_hash: str | None
    first_revision: int
    final_seq: int
    final_hash: str | None
    final_revision: int
    final_ts: str | None
    row_parameters: _PreparedExecutemanyParameters


class _PreparedSqlAuditBatch:
    """Data-free transaction-local capability for one audit executemany."""

    __slots__ = ("__weakref__",)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("SQL Audit batch seal is immutable")


_SQL_AUDIT_BATCH_LOCK = Lock()
_SQL_AUDIT_BATCH_STATES: WeakKeyDictionary[
    _PreparedSqlAuditBatch,
    _SqlAuditBatchState,
] = WeakKeyDictionary()
_CONSUMED_SQL_AUDIT_BATCHES: WeakSet[_PreparedSqlAuditBatch] = WeakSet()


def _require_chain_id(chain_id: str) -> str:
    if not isinstance(chain_id, str) or not chain_id:
        raise ValueError("chain_id must be a non-empty string")
    return chain_id


def _record_from_row(row: AuditRow) -> AuditRecordV2:
    if row.audit_schema_version != AUDIT_SCHEMA_VERSION_V2:
        raise IntegrityViolation(
            "audit chain stored row is not audit@2",
            physical_seq=row.seq,
            audit_schema_version=row.audit_schema_version,
        )
    if row.chain_id is None or row.chain_seq is None or row.actor_v2 is None:
        raise IntegrityViolation(
            "audit chain stored row is missing required audit@2 columns",
            physical_seq=row.seq,
        )

    wire = {
        "audit_schema_version": row.audit_schema_version,
        "chain_id": row.chain_id,
        "seq": row.chain_seq,
        "actor": row.actor_v2,
        "initiated_by": row.initiated_by,
        "action": row.action,
        "subject": row.subject,
        "correlation": row.correlation,
        "ts": row.ts,
        "prev_hash": row.prev_hash,
        "content_hash": row.content_hash,
    }
    try:
        record = AuditRecordV2.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "audit chain stored audit@2 row is invalid",
            chain_id=row.chain_id,
            chain_seq=row.chain_seq,
            physical_seq=row.seq,
        ) from exc

    canonical_actor = record.actor.model_dump(mode="json")
    canonical_initiator = (
        None if record.initiated_by is None else record.initiated_by.model_dump(mode="json")
    )
    canonical_subject = record.subject.model_dump(mode="json")
    canonical_correlation = record.correlation.model_dump(mode="json")
    if (
        row.actor_v2 != canonical_actor
        or row.initiated_by != canonical_initiator
        or row.subject != canonical_subject
        or row.correlation != canonical_correlation
        or row.actor != record.actor.principal_id
        or row.artifact_id != record.subject.artifact_id
    ):
        raise IntegrityViolation(
            "audit chain stored audit@2 projections are noncanonical",
            chain_id=record.chain_id,
            chain_seq=record.seq,
            physical_seq=row.seq,
        )
    return record


def _row_for(record: AuditRecordV2) -> AuditRow:
    return AuditRow(**_row_values_for(record))


def _row_values_for(record: AuditRecordV2) -> dict[str, object]:
    return {
        "audit_schema_version": record.audit_schema_version,
        "actor": record.actor.principal_id,
        "action": record.action,
        "artifact_id": record.subject.artifact_id,
        "ts": record.ts,
        "content_hash": record.content_hash,
        "prev_hash": record.prev_hash,
        "chain_id": record.chain_id,
        "chain_seq": record.seq,
        "actor_v2": record.actor.model_dump(mode="json"),
        "initiated_by": (
            None if record.initiated_by is None else record.initiated_by.model_dump(mode="json")
        ),
        "subject": record.subject.model_dump(mode="json"),
        "correlation": record.correlation.model_dump(mode="json"),
    }


def _sqlite_json(value: object, *, label: str) -> str:
    """Serialize one JSON bind before the sealed DML-only apply phase."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise IntegrityViolation(f"audit {label} cannot be serialized for storage") from exc


def _audit_insert_parameters(record: AuditRecordV2) -> tuple[object, ...]:
    return (
        record.audit_schema_version,
        record.actor.principal_id,
        record.action,
        record.subject.artifact_id,
        record.ts,
        record.content_hash,
        record.prev_hash,
        record.chain_id,
        record.seq,
        _sqlite_json(record.actor.model_dump(mode="json"), label="actor"),
        (
            None
            if record.initiated_by is None
            else _sqlite_json(
                record.initiated_by.model_dump(mode="json"),
                label="initiated_by",
            )
        ),
        _sqlite_json(record.subject.model_dump(mode="json"), label="subject"),
        _sqlite_json(record.correlation.model_dump(mode="json"), label="correlation"),
    )


def _current_audit_transaction(session: Session) -> object:
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None or not transaction.is_active:
        raise IntegrityViolation("SQL Audit batch requires an active transaction")
    return transaction


def _issue_sql_audit_batch(state: _SqlAuditBatchState) -> _PreparedSqlAuditBatch:
    handle = _PreparedSqlAuditBatch()
    with _SQL_AUDIT_BATCH_LOCK:
        _SQL_AUDIT_BATCH_STATES[handle] = state
    return handle


def _consume_sql_audit_batch(
    handle: object,
    *,
    owner: SqlAuditSink,
    records: tuple[AuditRecordV2, ...],
    expected_heads: tuple[object, ...],
) -> _SqlAuditBatchState:
    state = None
    consumed = False
    if type(handle) is _PreparedSqlAuditBatch:
        with _SQL_AUDIT_BATCH_LOCK:
            state = _SQL_AUDIT_BATCH_STATES.get(handle)
            consumed = handle in _CONSUMED_SQL_AUDIT_BATCHES
    if state is None:
        raise IntegrityViolation("SQL Audit batch lacks a trusted preflight seal")
    if consumed:
        raise IntegrityViolation("SQL Audit batch preflight seal was already consumed")
    if (
        state.owner is not owner
        or state.session is not owner._session
        or state.transaction is not _current_audit_transaction(owner._session)
        or state.records is not records
        or state.expected_heads is not expected_heads
    ):
        raise IntegrityViolation(
            "SQL Audit batch belongs to another repository, transaction, or projection"
        )
    with _SQL_AUDIT_BATCH_LOCK:
        if (
            _SQL_AUDIT_BATCH_STATES.get(handle) is not state
            or handle in _CONSUMED_SQL_AUDIT_BATCHES
        ):
            raise IntegrityViolation("SQL Audit batch preflight seal was already consumed")
        _CONSUMED_SQL_AUDIT_BATCHES.add(handle)
    return state


def _exact_runtime_value(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, BaseModel) and isinstance(right, BaseModel):
        if set(left.__dict__) != set(right.__dict__):
            return False
        return all(
            _exact_runtime_value(left.__dict__[field], right.__dict__[field])
            for field in left.__dict__
        )
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(_exact_runtime_value(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _exact_runtime_value(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _revalidate_for_append(record: AuditRecordV2) -> AuditRecordV2:
    nested_models = (
        record,
        record.actor,
        record.initiated_by,
        record.subject,
        record.correlation,
    )
    if any(
        value is not None
        and (
            not isinstance(value, BaseModel) or set(value.__dict__) != set(type(value).model_fields)
        )
        for value in nested_models
    ):
        raise IntegrityViolation("append requires a canonical AuditRecordV2")
    wire = record.model_dump(mode="json")
    try:
        canonical = AuditRecordV2.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation("append requires a canonical AuditRecordV2") from exc
    canonical_wire = canonical.model_dump(mode="json")
    if not _exact_runtime_value(record, canonical) or canonical_json(
        canonical_wire
    ) != canonical_json(wire):
        raise IntegrityViolation("append requires a canonical AuditRecordV2")
    return canonical


class SqlAuditSink:
    """Append ``audit@2`` records without committing their owning UnitOfWork.

    ``lock_head`` and ``append`` inspect only the chain-head row and its direct
    predecessor. The SQLite UnitOfWork's ``BEGIN IMMEDIATE`` supplies the write
    lock; a PostgreSQL adapter can implement the same boundary with row locks.
    Full-chain verification is an explicit, separately invoked operation.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_head(self, chain_id: str) -> AuditChainHead:
        """Return a validated head in the current write transaction."""

        selected_chain = _require_chain_id(chain_id)
        row = self._load_head(selected_chain)
        if row is None:
            existing = self._session.scalar(
                select(AuditRow.seq)
                .where(
                    AuditRow.audit_schema_version == AUDIT_SCHEMA_VERSION_V2,
                    AuditRow.chain_id == selected_chain,
                )
                .limit(1)
            )
            if existing is not None:
                raise IntegrityViolation(
                    "audit chain has records but no chain head",
                    chain_id=selected_chain,
                )
            return AuditChainHead(
                chain_id=selected_chain,
                seq=0,
                content_hash=None,
                revision=0,
            )
        return self._validated_head(row)

    def append(self, record: AuditRecordV2) -> AuditRecordV2:
        if not isinstance(record, AuditRecordV2):
            raise TypeError("audit sink requires an AuditRecordV2")
        record = _revalidate_for_append(record)

        current = self.lock_head(record.chain_id)
        if record.seq != current.seq + 1 or record.prev_hash != current.content_hash:
            raise Conflict(
                "audit chain head changed",
                chain_id=record.chain_id,
                expected_seq=record.seq - 1,
                actual_seq=current.seq,
                expected_hash=record.prev_hash,
                actual_hash=current.content_hash,
                actual_revision=current.revision,
            )

        if current.seq == 0:
            self._session.add(
                AuditHeadRow(
                    chain_id=record.chain_id,
                    head_seq=record.seq,
                    head_hash=record.content_hash,
                    revision=1,
                    updated_at=record.ts,
                )
            )
        else:
            result = self._session.execute(
                update(AuditHeadRow)
                .where(
                    AuditHeadRow.chain_id == current.chain_id,
                    AuditHeadRow.head_seq == current.seq,
                    AuditHeadRow.head_hash == current.content_hash,
                    AuditHeadRow.revision == current.revision,
                )
                .values(
                    head_seq=record.seq,
                    head_hash=record.content_hash,
                    revision=current.revision + 1,
                    updated_at=record.ts,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise Conflict(
                    "audit chain head CAS failed",
                    chain_id=record.chain_id,
                    expected_seq=current.seq,
                    expected_hash=current.content_hash,
                    expected_revision=current.revision,
                )

        self._session.add(_row_for(record))
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise IntegrityViolation(
                "audit chain append violated storage integrity",
                chain_id=record.chain_id,
                chain_seq=record.seq,
            ) from exc
        return record

    def append_preflighted(
        self,
        record: AuditRecordV2,
        expected_head: object,
    ) -> AuditRecordV2:
        """Append against a same-transaction head snapshot without re-SELECT."""

        chain_id = getattr(expected_head, "chain_id", None)
        seq = getattr(expected_head, "seq", None)
        content_hash = getattr(expected_head, "content_hash", None)
        revision = getattr(expected_head, "revision", None)
        if (
            chain_id != record.chain_id
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or record.seq != seq + 1
            or record.prev_hash != content_hash
        ):
            raise IntegrityViolation("preflighted audit head/record binding is invalid")
        if seq == 0:
            self._session.add(
                AuditHeadRow(
                    chain_id=record.chain_id,
                    head_seq=record.seq,
                    head_hash=record.content_hash,
                    revision=1,
                    updated_at=record.ts,
                )
            )
        else:
            result = self._session.execute(
                update(AuditHeadRow)
                .where(
                    AuditHeadRow.chain_id == chain_id,
                    AuditHeadRow.head_seq == seq,
                    AuditHeadRow.head_hash == content_hash,
                    AuditHeadRow.revision == revision,
                )
                .values(
                    head_seq=record.seq,
                    head_hash=record.content_hash,
                    revision=revision + 1,
                    updated_at=record.ts,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise Conflict(
                    "preflighted audit chain head CAS failed",
                    chain_id=record.chain_id,
                    expected_seq=seq,
                )
        self._session.add(_row_for(record))
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise IntegrityViolation(
                "preflighted audit append violated storage integrity",
                chain_id=record.chain_id,
                chain_seq=record.seq,
            ) from exc
        return record

    def prepare_preflighted_batch(
        self,
        records: tuple[AuditRecordV2, ...],
        expected_heads: tuple[object, ...],
    ) -> _PreparedSqlAuditBatch:
        """Validate and project a contiguous batch before terminal DML begins."""

        if type(records) is not tuple or type(expected_heads) is not tuple:
            raise IntegrityViolation("preflighted audit batch must use exact tuples")
        if len(records) != len(expected_heads):
            raise IntegrityViolation("preflighted audit batch cardinality differs")
        if not records:
            return _issue_sql_audit_batch(
                _SqlAuditBatchState(
                    owner=self,
                    session=self._session,
                    transaction=_current_audit_transaction(self._session),
                    records=records,
                    expected_heads=expected_heads,
                    chain_id=None,
                    first_seq=0,
                    first_hash=None,
                    first_revision=0,
                    final_seq=0,
                    final_hash=None,
                    final_revision=0,
                    final_ts=None,
                    row_parameters=_PreparedExecutemanyParameters(),
                )
            )
        if any(not isinstance(record, AuditRecordV2) for record in records):
            raise IntegrityViolation("preflighted audit batch contains an invalid record")
        first_head = expected_heads[0]
        first_chain_id = getattr(first_head, "chain_id", None)
        first_seq = getattr(first_head, "seq", None)
        first_hash = getattr(first_head, "content_hash", None)
        first_revision = getattr(first_head, "revision", None)
        if (
            not isinstance(first_chain_id, str)
            or not first_chain_id
            or isinstance(first_seq, bool)
            or not isinstance(first_seq, int)
            or first_seq < 0
            or isinstance(first_revision, bool)
            or not isinstance(first_revision, int)
            or first_revision != first_seq
            or (first_seq == 0 and first_hash is not None)
            or (
                first_seq > 0
                and (
                    not isinstance(first_hash, str)
                    or len(first_hash) != 64
                    or any(character not in "0123456789abcdef" for character in first_hash)
                )
            )
        ):
            raise IntegrityViolation("preflighted audit batch initial head is invalid")

        previous_record: AuditRecordV2 | None = None
        previous_revision = first_revision - 1
        row_parameters: list[tuple[object, ...]] = []
        for record, expected_head in zip(records, expected_heads, strict=True):
            chain_id = getattr(expected_head, "chain_id", None)
            seq = getattr(expected_head, "seq", None)
            content_hash = getattr(expected_head, "content_hash", None)
            revision = getattr(expected_head, "revision", None)
            if (
                chain_id != first_chain_id
                or record.chain_id != first_chain_id
                or isinstance(seq, bool)
                or not isinstance(seq, int)
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision != seq
                or revision != previous_revision + 1
                or record.seq != seq + 1
                or record.prev_hash != content_hash
                or (
                    previous_record is not None
                    and (seq != previous_record.seq or content_hash != previous_record.content_hash)
                )
            ):
                raise IntegrityViolation("preflighted audit batch links are not contiguous")
            row_parameters.append(_audit_insert_parameters(record))
            previous_record = record
            previous_revision = revision

        final_record = records[-1]
        final_revision = first_revision + len(records)
        return _issue_sql_audit_batch(
            _SqlAuditBatchState(
                owner=self,
                session=self._session,
                transaction=_current_audit_transaction(self._session),
                records=records,
                expected_heads=expected_heads,
                chain_id=first_chain_id,
                first_seq=first_seq,
                first_hash=first_hash,
                first_revision=first_revision,
                final_seq=final_record.seq,
                final_hash=final_record.content_hash,
                final_revision=final_revision,
                final_ts=final_record.ts,
                row_parameters=_PreparedExecutemanyParameters(row_parameters),
            )
        )

    def append_preflighted_batch(
        self,
        records: tuple[AuditRecordV2, ...],
        expected_heads: tuple[object, ...],
        prepared: object,
    ) -> tuple[AuditRecordV2, ...]:
        """Apply a sealed batch with one head DML and one audit executemany.

        All validation, model projection, and JSON encoding happened in
        ``prepare_preflighted_batch``.  This boundary performs no SELECT, no Python
        work proportional to batch size, and no ORM flush.
        """

        state = _consume_sql_audit_batch(
            prepared,
            owner=self,
            records=records,
            expected_heads=expected_heads,
        )
        if not state.row_parameters:
            return records
        chain_id = state.chain_id
        if chain_id is None:  # pragma: no cover - nonempty rows always bind a chain
            raise IntegrityViolation("SQL Audit batch lost its chain identity")
        try:
            with self._session.no_autoflush:
                connection = self._session.connection()
                if state.first_seq == 0:
                    head_result = connection.exec_driver_sql(
                        _INSERT_AUDIT_HEAD_SQL,
                        (
                            chain_id,
                            state.final_seq,
                            state.final_hash,
                            state.final_revision,
                            state.final_ts,
                        ),
                    )
                    if head_result.rowcount != 1:
                        raise Conflict("preflighted audit batch could not create its head")
                else:
                    head_result = connection.exec_driver_sql(
                        _UPDATE_AUDIT_HEAD_SQL,
                        (
                            state.final_seq,
                            state.final_hash,
                            state.final_revision,
                            state.final_ts,
                            chain_id,
                            state.first_seq,
                            state.first_hash,
                            state.first_revision,
                        ),
                    )
                    if head_result.rowcount != 1:
                        raise Conflict(
                            "preflighted audit batch head CAS failed",
                            chain_id=chain_id,
                            expected_seq=state.first_seq,
                        )
                row_result = connection.exec_driver_sql(
                    _INSERT_AUDIT_BATCH_SQL,
                    state.row_parameters,
                )
                if row_result.rowcount != len(state.row_parameters):
                    raise IntegrityViolation(
                        "preflighted audit batch did not insert every sealed record",
                        chain_id=chain_id,
                    )
        except IntegrityError as exc:
            raise IntegrityViolation(
                "preflighted audit batch violated storage integrity",
                chain_id=chain_id,
            ) from exc
        return records

    def register_before_commit_guard(self, guard: Callable[[], None]) -> None:
        """Run one authority invariant before the owning Session may commit."""

        if not callable(guard):
            raise TypeError("audit before-commit guard must be callable")
        session = self._session

        def validate_before_commit(committing: Session) -> None:
            if committing is not session:
                raise IntegrityViolation("audit commit guard ran for another transaction")
            guard()

        event.listen(session, "before_commit", validate_before_commit, once=True)

    def get(self, chain_id: str, seq: int) -> AuditRecordV2 | None:
        selected_chain = _require_chain_id(chain_id)
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise ValueError("seq must be a positive integer")
        row = self._session.scalar(
            select(AuditRow)
            .where(
                AuditRow.audit_schema_version == AUDIT_SCHEMA_VERSION_V2,
                AuditRow.chain_id == selected_chain,
                AuditRow.chain_seq == seq,
            )
            .execution_options(populate_existing=True)
        )
        return None if row is None else _record_from_row(row)

    def verify_chain(self, chain_id: str) -> bool:
        """Explicitly scan and validate one whole local chain.

        A successful result proves consistency with the locally stored head.
        Without an independently trusted head it cannot prove that a tail and
        the local head were not truncated together.
        """

        selected_chain = _require_chain_id(chain_id)
        rows = self._session.scalars(
            select(AuditRow)
            .where(
                AuditRow.audit_schema_version == AUDIT_SCHEMA_VERSION_V2,
                AuditRow.chain_id == selected_chain,
            )
            .order_by(AuditRow.chain_seq)
            .execution_options(populate_existing=True)
        ).all()
        head_row = self._load_head(selected_chain)

        if head_row is None:
            if rows:
                raise IntegrityViolation(
                    "audit chain has records but no chain head",
                    chain_id=selected_chain,
                )
            return True

        head = self._validated_head(head_row)
        previous_hash: str | None = None
        for expected_seq, row in enumerate(rows, start=1):
            record = _record_from_row(row)
            if record.seq != expected_seq or record.prev_hash != previous_hash:
                raise IntegrityViolation(
                    "audit chain sequence or predecessor is broken",
                    chain_id=selected_chain,
                    expected_seq=expected_seq,
                    actual_seq=record.seq,
                    expected_prev_hash=previous_hash,
                    actual_prev_hash=record.prev_hash,
                )
            previous_hash = record.content_hash

        if len(rows) != head.seq or previous_hash != head.content_hash:
            raise IntegrityViolation(
                "audit chain head does not match the verified records",
                chain_id=selected_chain,
                verified_seq=len(rows),
                head_seq=head.seq,
                verified_hash=previous_hash,
                head_hash=head.content_hash,
            )
        return True

    def _load_head(self, chain_id: str) -> AuditHeadRow | None:
        return self._session.scalar(
            select(AuditHeadRow)
            .where(AuditHeadRow.chain_id == chain_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def _validated_head(self, row: AuditHeadRow) -> AuditChainHead:
        if (
            isinstance(row.head_seq, bool)
            or not isinstance(row.head_seq, int)
            or row.head_seq < 1
            or isinstance(row.revision, bool)
            or not isinstance(row.revision, int)
            or row.revision != row.head_seq
            or not isinstance(row.head_hash, str)
            or len(row.head_hash) != 64
            or any(character not in "0123456789abcdef" for character in row.head_hash)
        ):
            raise IntegrityViolation(
                "audit chain head is invalid",
                chain_id=row.chain_id,
            )

        predecessor = self._session.scalar(
            select(AuditRow)
            .where(
                AuditRow.audit_schema_version == AUDIT_SCHEMA_VERSION_V2,
                AuditRow.chain_id == row.chain_id,
                AuditRow.chain_seq == row.head_seq,
            )
            .execution_options(populate_existing=True)
        )
        if predecessor is None:
            raise IntegrityViolation(
                "audit chain head has no direct predecessor record",
                chain_id=row.chain_id,
                head_seq=row.head_seq,
            )
        record = _record_from_row(predecessor)
        if record.content_hash != row.head_hash:
            raise IntegrityViolation(
                "audit chain head hash differs from its direct predecessor",
                chain_id=row.chain_id,
                head_seq=row.head_seq,
            )
        return AuditChainHead(
            chain_id=row.chain_id,
            seq=row.head_seq,
            content_hash=row.head_hash,
            revision=row.revision,
        )


__all__ = ["AuditChainHead", "SqlAuditSink"]
