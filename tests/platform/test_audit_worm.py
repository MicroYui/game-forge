"""Append-only WORM audit log tests (contract §5, §12A.3, Task 13).

`AuditLog` chains each row's `content_hash` from the previous row's hash
(`prev_hash`), so `verify_chain()` can detect any direct tamper of a stored
row — the audit trail's whole reason for existing (PRD: enterprise-grade
audit, not best-effort logging).
"""

from gameforge.contracts.lineage import AuditRecord
from gameforge.platform.audit.log import AuditLog
from gameforge.runtime.persistence.engine import get_engine, get_sessionmaker
from gameforge.runtime.persistence.models import AuditRow, Base


def _sf(tmp_path):
    url = f"sqlite:///{tmp_path / 'a.db'}"
    Base.metadata.create_all(get_engine(url))
    return get_sessionmaker(get_engine(url))


def test_audit_append_only_hash_chain_and_tamper_detection(tmp_path):
    sf = _sf(tmp_path)
    log = AuditLog(sf)
    log.append("cli", "record_artifact", "a1", ts="2026-07-06T00:00:00Z")
    log.append("cli", "record_artifact", "a2", ts="2026-07-06T00:00:01Z")
    assert log.verify_chain() is True
    # tamper directly in the DB -> chain verification fails
    with sf() as s:
        row = s.get(AuditRow, 1)
        row.action = "TAMPERED"
        s.commit()
    assert log.verify_chain() is False


def test_audit_append_returns_audit_record(tmp_path):
    log = AuditLog(_sf(tmp_path))
    record = log.append("cli", "record_artifact", "a1", ts="2026-07-06T00:00:00Z")
    assert isinstance(record, AuditRecord)
    assert record.seq == 1
    assert record.actor == "cli"
    assert record.action == "record_artifact"
    assert record.artifact_id == "a1"
    assert record.prev_hash is None
    assert record.content_hash.startswith("sha256:")


def test_audit_chains_prev_hash_from_previous_row(tmp_path):
    log = AuditLog(_sf(tmp_path))
    first = log.append("cli", "record_artifact", "a1", ts="2026-07-06T00:00:00Z")
    second = log.append("cli", "record_artifact", "a2", ts="2026-07-06T00:00:01Z")
    assert second.prev_hash == first.content_hash
    assert second.content_hash != first.content_hash


def test_audit_empty_chain_verifies_true(tmp_path):
    log = AuditLog(_sf(tmp_path))
    assert log.verify_chain() is True


def test_audit_has_no_update_or_delete_methods(tmp_path):
    log = AuditLog(_sf(tmp_path))
    assert not hasattr(log, "update")
    assert not hasattr(log, "delete")


def test_audit_tamper_of_prev_hash_detected(tmp_path):
    sf = _sf(tmp_path)
    log = AuditLog(sf)
    log.append("cli", "record_artifact", "a1", ts="2026-07-06T00:00:00Z")
    log.append("cli", "record_artifact", "a2", ts="2026-07-06T00:00:01Z")
    with sf() as s:
        row = s.get(AuditRow, 2)
        row.prev_hash = "sha256:" + "0" * 64
        s.commit()
    assert log.verify_chain() is False
