"""Permanent audit@1 compatibility facade with a tamper-evident hash chain.

`AuditLog.append` is the ONLY mutating operation this module exposes — there
is no `update`/`delete`, by omission rather than by a guarded method, so
there is nothing to accidentally call. Each row's `content_hash` is a
content-addressed digest (`contracts.canonical.compute_snapshot_id`) over
its own fields plus the previous row's `content_hash` (`prev_hash`),
forming a hash chain. This application interface exposes no mutator for an
existing row; database-enforced WORM and external tail anchoring belong to
M4e, so this module claims detection rather than prevention.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session

from gameforge.contracts.canonical import compute_snapshot_id
from gameforge.contracts.lineage import AuditRecord
from gameforge.contracts.versions import AUDIT_SCHEMA_VERSION
from gameforge.runtime.persistence.models import AuditRow

SessionFactory = Callable[[], Session]


def _content_hash(
    *, actor: str, action: str, artifact_id: str | None, ts: str, prev_hash: str | None
) -> str:
    return compute_snapshot_id(
        {
            "actor": actor,
            "action": action,
            "artifact_id": artifact_id,
            "ts": ts,
            "prev_hash": prev_hash,
        }
    )


def _row_to_record(row: AuditRow) -> AuditRecord:
    return AuditRecord(
        audit_schema_version=row.audit_schema_version,
        seq=row.seq,
        actor=row.actor,
        action=row.action,
        artifact_id=row.artifact_id,
        ts=row.ts,
        content_hash=row.content_hash,
        prev_hash=row.prev_hash,
    )


class AuditLog:
    """`audit` table persistence: INSERT-only hash-chained log.

    No `update`/`delete` method exists on this class. That is an application
    boundary, not a claim that the underlying database prevents tampering.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def append(self, actor: str, action: str, artifact_id: str | None, ts: str) -> AuditRecord:
        """Insert a new audit row, chaining `prev_hash` from the last row's
        `content_hash` (or `None` for the first entry)."""
        with self._session_factory() as session:
            last = (
                session.query(AuditRow)
                .filter(AuditRow.audit_schema_version == AUDIT_SCHEMA_VERSION)
                .order_by(AuditRow.seq.desc())
                .first()
            )
            prev_hash = last.content_hash if last is not None else None
            content_hash = _content_hash(
                actor=actor, action=action, artifact_id=artifact_id, ts=ts, prev_hash=prev_hash
            )
            row = AuditRow(
                audit_schema_version=AUDIT_SCHEMA_VERSION,
                actor=actor,
                action=action,
                artifact_id=artifact_id,
                ts=ts,
                content_hash=content_hash,
                prev_hash=prev_hash,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _row_to_record(row)

    def verify_chain(self) -> bool:
        """Walk rows in `seq` order, recomputing each `content_hash` from its
        stored fields plus the running `prev_hash`. Returns `False` the
        moment a row's stored `prev_hash` or `content_hash` disagrees with
        what recomputation produces — which any direct-DB tamper of a
        field, a hash, or the chain linkage triggers."""
        with self._session_factory() as session:
            rows = (
                session.query(AuditRow)
                .filter(AuditRow.audit_schema_version == AUDIT_SCHEMA_VERSION)
                .order_by(AuditRow.seq)
                .all()
            )
            prev_hash: str | None = None
            for row in rows:
                if row.prev_hash != prev_hash:
                    return False
                expected = _content_hash(
                    actor=row.actor,
                    action=row.action,
                    artifact_id=row.artifact_id,
                    ts=row.ts,
                    prev_hash=prev_hash,
                )
                if row.content_hash != expected:
                    return False
                prev_hash = row.content_hash
        return True
