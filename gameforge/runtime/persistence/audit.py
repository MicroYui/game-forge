"""Transaction-bound SQLite persistence for the ``audit@2`` hash chain."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.lineage import AuditRecordV2
from gameforge.contracts.versions import AUDIT_SCHEMA_VERSION_V2
from gameforge.runtime.persistence.models import AuditHeadRow, AuditRow


@dataclass(frozen=True, slots=True)
class AuditChainHead:
    chain_id: str
    seq: int
    content_hash: str | None
    revision: int


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
    return AuditRow(
        audit_schema_version=record.audit_schema_version,
        actor=record.actor.principal_id,
        action=record.action,
        artifact_id=record.subject.artifact_id,
        ts=record.ts,
        content_hash=record.content_hash,
        prev_hash=record.prev_hash,
        chain_id=record.chain_id,
        chain_seq=record.seq,
        actor_v2=record.actor.model_dump(mode="json"),
        initiated_by=(
            None if record.initiated_by is None else record.initiated_by.model_dump(mode="json")
        ),
        subject=record.subject.model_dump(mode="json"),
        correlation=record.correlation.model_dump(mode="json"),
    )


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
