from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.lineage import (
    AuditActor,
    AuditCorrelation,
    AuditRecord,
    AuditRecordV1,
    AuditRecordV2,
    AuditSubject,
    audit_content_hash_v2,
    build_audit_record_v2,
    parse_audit_record,
)


def _record(**overrides) -> AuditRecordV2:
    values = {
        "chain_id": "platform-authority",
        "seq": 1,
        "actor": AuditActor(principal_id="worker:1", principal_kind="service"),
        "initiated_by": AuditActor(principal_id="human:maker", principal_kind="human"),
        "action": "artifact.publish",
        "subject": AuditSubject(
            resource_kind="artifact",
            resource_id="artifact:1",
            artifact_id="artifact:1",
        ),
        "correlation": AuditCorrelation(
            request_id="request:1",
            run_id="run:1",
            trace_id="trace:1",
        ),
        "ts": "2026-07-13T00:00:00Z",
        "prev_hash": None,
    }
    values.update(overrides)
    return build_audit_record_v2(**values)


def test_legacy_audit_constructor_and_prefixed_hash_remain_valid() -> None:
    record = AuditRecord(
        seq=1,
        actor="cli",
        action="record_artifact",
        artifact_id="a1",
        ts="2026-07-06T00:00:00Z",
        content_hash="sha256:legacy-hash",
        prev_hash=None,
    )
    assert isinstance(record, AuditRecordV1)
    assert record.audit_schema_version == "audit@1"
    assert parse_audit_record(record.model_dump(mode="json")) == record


def test_audit_v2_hash_binds_every_authoritative_field() -> None:
    record = _record()
    assert record.audit_schema_version == "audit@2"
    assert len(record.content_hash) == 64
    assert record.content_hash == audit_content_hash_v2(
        chain_id=record.chain_id,
        seq=record.seq,
        actor=record.actor,
        initiated_by=record.initiated_by,
        action=record.action,
        subject=record.subject,
        correlation=record.correlation,
        ts=record.ts,
        prev_hash=record.prev_hash,
    )

    assert _record(seq=2).content_hash != record.content_hash
    assert _record(action="artifact.reject").content_hash != record.content_hash
    assert _record(initiated_by=None).content_hash != record.content_hash
    assert _record(
        correlation=AuditCorrelation(request_id="request:2", run_id="run:1", trace_id="trace:1")
    ).content_hash != record.content_hash


def test_audit_v2_parser_is_discriminator_driven() -> None:
    record = _record()
    assert parse_audit_record(record.model_dump(mode="json")) == record

    with pytest.raises(ValidationError):
        parse_audit_record({**record.model_dump(mode="json"), "audit_schema_version": "audit@999"})

    missing_subject = record.model_dump(mode="json")
    del missing_subject["subject"]
    with pytest.raises(ValidationError):
        parse_audit_record(missing_subject)


def test_audit_v2_rejects_hash_tampering_and_noncanonical_digests() -> None:
    record = _record()
    with pytest.raises(ValidationError, match="content_hash"):
        AuditRecordV2.model_validate(
            {**record.model_dump(mode="json"), "content_hash": "0" * 64}
        )

    for bad_prev in (
        "sha256:" + "0" * 64,
        "A" * 64,
        "0" * 63,
    ):
        with pytest.raises(ValidationError):
            _record(prev_hash=bad_prev)


def test_audit_nested_contracts_are_closed() -> None:
    with pytest.raises(ValidationError):
        AuditActor(principal_id="human:1", principal_kind="human", role="admin")
    with pytest.raises(ValidationError):
        AuditSubject(resource_kind="artifact", resource_id="a", arbitrary="value")
    with pytest.raises(ValidationError):
        AuditCorrelation(request_id="r", unknown="value")
