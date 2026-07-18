from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import datetime, timezone
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import gameforge.contracts.lineage as lineage_module
import gameforge.runtime.persistence.audit as audit_persistence
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.lineage import (
    AuditActor,
    AuditCorrelation,
    AuditRecordV2,
    AuditSubject,
    build_audit_record_v2,
)
from gameforge.contracts.storage import AuditSink
from gameforge.platform.audit.gate import AuditAppendIntent, AuditGate, AuditGateStore
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import AuditHeadRow, AuditRow, Base, RefRow
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


_NOW = datetime(2026, 7, 14, 1, 2, 3, tzinfo=timezone.utc)


class _UnusedCapability:
    pass


class _InvalidHeadSink:
    def lock_head(self, chain_id: str) -> object:
        return type(
            "InvalidHead",
            (),
            {
                "chain_id": chain_id,
                "seq": 1,
                "content_hash": 7,
                "revision": 1,
            },
        )()

    def append(self, record: AuditRecordV2) -> AuditRecordV2:
        raise AssertionError("invalid head must fail before append")

    def verify_chain(self, chain_id: str) -> bool:
        del chain_id
        return True


class _BusinessProbe:
    def __init__(self, session: Session) -> None:
        self._session = session

    def write(self, name: str) -> None:
        self._session.add(
            RefRow(
                name=name,
                artifact_id="artifact:business",
                revision=1,
            )
        )
        self._session.flush()


def _capabilities(session: Session) -> TransactionCapabilities:
    unused = _UnusedCapability()
    return TransactionCapabilities(
        refs=_BusinessProbe(session),
        audit=SqlAuditSink(session),
        approvals=unused,
        lineage=unused,
        object_bindings=unused,
        runs=unused,
        cost=unused,
    )


@pytest.fixture
def audit_engine(tmp_path) -> Engine:
    engine = get_engine(f"sqlite:///{tmp_path / 'audit-v2.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _gate(sink: Any) -> AuditGate:
    return AuditGate(sink=sink, clock=FrozenUtcClock(_NOW))


def _append(
    gate: AuditGate,
    *,
    action: str = "artifact.publish",
    resource_id: str = "artifact:1",
) -> AuditRecordV2:
    return gate.append(
        chain_id="platform-authority",
        actor=AuditActor(principal_id="worker:1", principal_kind="service"),
        initiated_by=AuditActor(principal_id="human:maker", principal_kind="human"),
        action=action,
        subject=AuditSubject(
            resource_kind="artifact",
            resource_id=resource_id,
            artifact_id=resource_id,
        ),
        correlation=AuditCorrelation(
            request_id="request:1",
            run_id="run:1",
            trace_id="trace:1",
        ),
    )


def _uow(engine: Engine) -> SqliteUnitOfWork:
    return SqliteUnitOfWork(engine, _capabilities)


def test_sql_sink_satisfies_append_and_gate_storage_protocols(
    audit_engine: Engine,
) -> None:
    with Session(audit_engine) as session:
        sink = SqlAuditSink(session)
        assert isinstance(sink, AuditSink)
        assert isinstance(sink, AuditGateStore)
        assert not isinstance(_UnusedCapability(), AuditGateStore)


def test_gate_maps_a_malformed_adapter_head_to_typed_integrity_failure() -> None:
    with pytest.raises(IntegrityViolation, match="invalid locked head"):
        _append(_gate(_InvalidHeadSink()))


def test_batch_preflight_maps_a_malformed_adapter_head_before_any_append() -> None:
    intent = AuditAppendIntent(
        actor=AuditActor(principal_id="worker:1", principal_kind="service"),
        initiated_by=None,
        action="artifact.publish",
        subject=AuditSubject(resource_kind="artifact", resource_id="artifact:1"),
        correlation=AuditCorrelation(request_id="request:1"),
    )
    with pytest.raises(IntegrityViolation, match="invalid locked head"):
        _gate(_InvalidHeadSink()).prepare_batch(
            chain_id="platform-authority",
            intents=(intent,),
        )


def test_gate_builds_and_round_trips_every_audit_v2_semantic_field(
    audit_engine: Engine,
) -> None:
    with _uow(audit_engine).begin() as transaction:
        written = _append(_gate(transaction.audit))
        loaded = transaction.audit.get(written.chain_id, written.seq)

    assert loaded == written
    assert written.seq == 1
    assert written.prev_hash is None
    assert written.ts == "2026-07-14T01:02:03Z"
    assert written.actor == AuditActor(
        principal_id="worker:1",
        principal_kind="service",
    )
    assert written.initiated_by == AuditActor(
        principal_id="human:maker",
        principal_kind="human",
    )
    assert written.subject == AuditSubject(
        resource_kind="artifact",
        resource_id="artifact:1",
        artifact_id="artifact:1",
    )
    assert written.correlation == AuditCorrelation(
        request_id="request:1",
        run_id="run:1",
        trace_id="trace:1",
    )


def test_prepared_audit_batch_is_data_free_transaction_bound_and_one_shot(
    audit_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hash_calls = 0
    original_hash = lineage_module.audit_content_hash_v2

    def counted_hash(**kwargs: object) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return original_hash(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lineage_module, "audit_content_hash_v2", counted_hash)
    intent = AuditAppendIntent(
        actor=AuditActor(principal_id="worker:1", principal_kind="service"),
        initiated_by=None,
        action="artifact.publish",
        subject=AuditSubject(resource_kind="artifact", resource_id="artifact:1"),
        correlation=AuditCorrelation(request_id="request:1"),
    )
    with _uow(audit_engine).begin() as transaction:
        gate = _gate(transaction.audit)
        prepared = gate.prepare_batch(
            chain_id="platform-authority",
            intents=(intent,),
        )
        # build_audit_record_v2 computes once and AuditRecordV2 validates once;
        # retaining the preflight state must not serialize/hash it a third time.
        assert hash_calls == 2
        with pytest.raises((AttributeError, TypeError)):
            object.__setattr__(prepared, "_records", ())
        with pytest.raises(IntegrityViolation, match="invalid"):
            gate.apply_prepared_batch(copy(prepared))
        with pytest.raises(IntegrityViolation, match="invalid"):
            gate.apply_prepared_batch(deepcopy(prepared))
        forged = object.__new__(type(prepared))
        with pytest.raises(IntegrityViolation, match="invalid"):
            gate.apply_prepared_batch(forged)
        object.__setattr__(intent.actor, "principal_id", "worker:changed-after-prepare")
        gate.apply_prepared_batch(prepared)
        assert hash_calls == 2
        with pytest.raises(IntegrityViolation, match="reused"):
            gate.apply_prepared_batch(prepared)

    with Session(audit_engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditHeadRow)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditRow)
                .where(AuditRow.audit_schema_version == "audit@2")
            )
            == 1
        )
        retained = session.scalar(
            select(AuditRow).where(AuditRow.audit_schema_version == "audit@2")
        )
        assert retained is not None
        assert retained.actor_v2["principal_id"] == "worker:1"


def test_prepared_audit_batch_builds_one_contiguous_chain_and_apply_is_dml_only(
    audit_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hash_calls = 0
    original_hash = lineage_module.audit_content_hash_v2

    def counted_hash(**kwargs: object) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return original_hash(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lineage_module, "audit_content_hash_v2", counted_hash)
    intents = tuple(
        AuditAppendIntent(
            actor=AuditActor(principal_id="worker:1", principal_kind="service"),
            initiated_by=None,
            action=f"terminal.publish.{ordinal}",
            subject=AuditSubject(
                resource_kind="run",
                resource_id=f"run:{ordinal}",
            ),
            correlation=AuditCorrelation(run_id=f"run:{ordinal}"),
        )
        for ordinal in (1, 2)
    )
    capture_apply = False
    apply_statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if capture_apply:
            apply_statements.append(statement)

    event.listen(audit_engine, "before_cursor_execute", capture_statement)
    try:
        with _uow(audit_engine).begin() as transaction:
            gate = _gate(transaction.audit)
            prepared = gate.prepare_batch(
                chain_id="platform-authority",
                intents=intents,
            )
            hashes_after_preflight = hash_calls
            capture_apply = True
            gate.apply_prepared_batch(prepared)
            capture_apply = False
            assert hash_calls == hashes_after_preflight
    finally:
        event.remove(audit_engine, "before_cursor_execute", capture_statement)

    assert apply_statements
    assert all(
        not statement.lstrip().upper().startswith("SELECT") for statement in apply_statements
    )
    with _uow(audit_engine).begin() as transaction:
        first = transaction.audit.get("platform-authority", 1)
        second = transaction.audit.get("platform-authority", 2)
        assert first is not None and second is not None
        assert (first.action, second.action) == (
            "terminal.publish.1",
            "terminal.publish.2",
        )
        assert second.prev_hash == first.content_hash
        assert transaction.audit.verify_chain("platform-authority") is True


def test_sql_audit_batch_cost_is_linear_before_apply_and_constant_during_apply(
    audit_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_size = 128
    hash_calls = 0
    flush_calls = 0
    original_hash = lineage_module.audit_content_hash_v2

    def counted_hash(**kwargs: object) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return original_hash(**kwargs)  # type: ignore[arg-type]

    def counted_flush(*_args: object, **_kwargs: object) -> None:
        nonlocal flush_calls
        if capture_apply:
            flush_calls += 1

    monkeypatch.setattr(lineage_module, "audit_content_hash_v2", counted_hash)
    intents = tuple(
        AuditAppendIntent(
            actor=AuditActor(principal_id="worker:1", principal_kind="service"),
            initiated_by=None,
            action=f"terminal.publish.{ordinal}",
            subject=AuditSubject(resource_kind="run", resource_id=f"run:{ordinal}"),
            correlation=AuditCorrelation(run_id=f"run:{ordinal}"),
        )
        for ordinal in range(batch_size)
    )
    apply_statements: list[tuple[str, bool]] = []
    capture_apply = False

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        executemany: bool,
    ) -> None:
        if capture_apply:
            apply_statements.append((statement, executemany))

    event.listen(audit_engine, "before_cursor_execute", capture_statement)
    event.listen(Session, "before_flush", counted_flush)
    try:
        with _uow(audit_engine).begin() as transaction:
            gate = _gate(transaction.audit)
            prepared = gate.prepare_batch(
                chain_id="platform-authority",
                intents=intents,
                require_batch=True,
            )
            # Building and validating each immutable record hashes it exactly twice;
            # increasing the batch does not rescan any already-built chain prefix.
            assert hash_calls == batch_size * 2

            def forbidden_late_projection(*_args: object, **_kwargs: object) -> None:
                raise AssertionError("Audit row projection escaped its preflight phase")

            monkeypatch.setattr(
                audit_persistence,
                "_audit_insert_parameters",
                forbidden_late_projection,
            )
            monkeypatch.setattr(audit_persistence, "_sqlite_json", forbidden_late_projection)

            capture_apply = True
            gate.apply_prepared_batch(prepared)
            capture_apply = False
            assert hash_calls == batch_size * 2
            assert flush_calls == 0
    finally:
        event.remove(audit_engine, "before_cursor_execute", capture_statement)
        event.remove(Session, "before_flush", counted_flush)

    assert len(apply_statements) == 2
    assert apply_statements[0][0].lstrip().upper().startswith("INSERT INTO AUDIT_HEADS")
    assert apply_statements[1][0].lstrip().upper().startswith("INSERT INTO AUDIT")
    assert apply_statements[1][1] is True

    with Session(audit_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditRow)
                .where(AuditRow.audit_schema_version == "audit@2")
            )
            == batch_size
        )


def test_prepared_audit_batch_rejects_reuse_of_one_sink_in_a_later_transaction(
    audit_engine: Engine,
) -> None:
    intent = AuditAppendIntent(
        actor=AuditActor(principal_id="worker:1", principal_kind="service"),
        initiated_by=None,
        action="artifact.publish",
        subject=AuditSubject(resource_kind="artifact", resource_id="artifact:1"),
        correlation=AuditCorrelation(request_id="request:1"),
    )
    with Session(audit_engine) as session:
        sink = SqlAuditSink(session)
        gate = _gate(sink)
        with session.begin():
            prepared = gate.prepare_batch(
                chain_id="platform-authority",
                intents=(intent,),
            )
        with session.begin():
            with pytest.raises(IntegrityViolation, match="cross-transaction"):
                gate.apply_prepared_batch(prepared)

    with Session(audit_engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditHeadRow)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditRow)
                .where(AuditRow.audit_schema_version == "audit@2")
            )
            == 0
        )


def test_audit_v2_chain_sequence_is_not_the_shared_physical_row_sequence(
    audit_engine: Engine,
) -> None:
    with Session(audit_engine) as session:
        session.add(
            AuditRow(
                audit_schema_version="audit@1",
                actor="legacy-cli",
                action="legacy.record",
                artifact_id="legacy-artifact",
                ts="2026-07-13T00:00:00Z",
                content_hash="sha256:legacy",
                prev_hash=None,
            )
        )
        session.commit()

    with _uow(audit_engine).begin() as transaction:
        written = _append(_gate(transaction.audit))

    with Session(audit_engine) as session:
        physical_seq = session.scalar(
            select(AuditRow.seq).where(
                AuditRow.audit_schema_version == "audit@2",
                AuditRow.chain_id == written.chain_id,
                AuditRow.chain_seq == written.seq,
            )
        )

    assert written.seq == 1
    assert physical_seq == 2


def test_append_rejects_a_record_built_against_a_stale_head(
    audit_engine: Engine,
) -> None:
    with _uow(audit_engine).begin() as transaction:
        first = _append(_gate(transaction.audit), resource_id="artifact:1")

    stale = build_audit_record_v2(
        chain_id=first.chain_id,
        seq=2,
        actor=first.actor,
        initiated_by=first.initiated_by,
        action="artifact.stale",
        subject=first.subject,
        correlation=first.correlation,
        ts="2026-07-14T01:02:04Z",
        prev_hash=first.content_hash,
    )

    with _uow(audit_engine).begin() as transaction:
        current = _append(_gate(transaction.audit), resource_id="artifact:2")
    assert current.seq == 2

    with pytest.raises(Conflict, match="head"):
        with _uow(audit_engine).begin() as transaction:
            transaction.audit.append(stale)

    with _uow(audit_engine).begin() as transaction:
        assert transaction.audit.verify_chain(first.chain_id) is True


def test_sink_revalidates_copied_records_before_writing_head_or_row(
    audit_engine: Engine,
) -> None:
    valid = build_audit_record_v2(
        chain_id="platform-authority",
        seq=1,
        actor=AuditActor(principal_id="worker:1", principal_kind="service"),
        initiated_by=None,
        action="artifact.publish",
        subject=AuditSubject(resource_kind="artifact", resource_id="artifact:1"),
        correlation=AuditCorrelation(request_id="request:1"),
        ts="2026-07-14T01:02:03Z",
        prev_hash=None,
    )
    invalid_records = (
        valid.model_copy(update={"action": "tampered-without-rehash"}),
        valid.model_copy(update={"unhashed_extra": "not-authoritative"}),
        valid.model_copy(
            update={"actor": {"principal_id": "worker:1", "principal_kind": "service"}}
        ),
        valid.model_copy(update={"seq": True}),
    )

    for invalid in invalid_records:
        with pytest.raises(IntegrityViolation, match="canonical AuditRecordV2"):
            with _uow(audit_engine).begin() as transaction:
                transaction.audit.append(invalid)

    with Session(audit_engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditHeadRow)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditRow)
                .where(AuditRow.audit_schema_version == "audit@2")
            )
            == 0
        )


def test_audit_and_business_writes_roll_back_with_the_same_unit_of_work(
    audit_engine: Engine,
) -> None:
    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with _uow(audit_engine).begin() as transaction:
            transaction.refs.write("business-rollback")
            _append(_gate(transaction.audit))
            raise RuntimeError("rollback sentinel")

    with Session(audit_engine) as session:
        assert session.get(RefRow, "business-rollback") is None
        assert session.scalar(select(func.count()).select_from(AuditHeadRow)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditRow)
                .where(AuditRow.audit_schema_version == "audit@2")
            )
            == 0
        )


def test_append_checks_only_the_locked_head_and_its_direct_predecessor(
    audit_engine: Engine,
) -> None:
    with _uow(audit_engine).begin() as transaction:
        _append(_gate(transaction.audit), resource_id="artifact:1")
        _append(_gate(transaction.audit), resource_id="artifact:2")
        _append(_gate(transaction.audit), resource_id="artifact:3")

    with Session(audit_engine) as session:
        session.execute(
            update(AuditRow)
            .where(
                AuditRow.chain_id == "platform-authority",
                AuditRow.chain_seq == 1,
            )
            .values(action="tampered-middle")
        )
        session.commit()

    # Per-append work is deliberately O(1): damage before the direct
    # predecessor is detected by the separate explicit full-chain check.
    with _uow(audit_engine).begin() as transaction:
        fourth = _append(_gate(transaction.audit), resource_id="artifact:4")
    assert fourth.seq == 4

    with pytest.raises(IntegrityViolation, match="audit chain"):
        with _uow(audit_engine).begin() as transaction:
            transaction.audit.verify_chain("platform-authority")


def test_append_fails_closed_when_the_direct_predecessor_is_corrupt(
    audit_engine: Engine,
) -> None:
    with _uow(audit_engine).begin() as transaction:
        _append(_gate(transaction.audit), resource_id="artifact:1")

    with Session(audit_engine) as session:
        session.execute(
            update(AuditRow)
            .where(
                AuditRow.chain_id == "platform-authority",
                AuditRow.chain_seq == 1,
            )
            .values(action="tampered-head")
        )
        session.commit()

    with pytest.raises(IntegrityViolation, match="audit chain"):
        with _uow(audit_engine).begin() as transaction:
            _append(_gate(transaction.audit), resource_id="artifact:2")


@pytest.mark.parametrize("damage", ["alter", "remove"])
def test_explicit_full_chain_verification_detects_middle_damage(
    audit_engine: Engine,
    damage: str,
) -> None:
    records: list[AuditRecordV2] = []
    with _uow(audit_engine).begin() as transaction:
        for index in range(1, 4):
            records.append(_append(_gate(transaction.audit), resource_id=f"artifact:{index}"))

    with Session(audit_engine) as session:
        predicate = (
            AuditRow.chain_id == "platform-authority",
            AuditRow.chain_seq == 2,
        )
        if damage == "alter":
            session.execute(update(AuditRow).where(*predicate).values(action="tampered"))
        else:
            session.execute(delete(AuditRow).where(*predicate))
        session.commit()

    with pytest.raises(IntegrityViolation, match="audit chain"):
        with _uow(audit_engine).begin() as transaction:
            transaction.audit.verify_chain("platform-authority")


def test_local_verification_cannot_prove_a_consistently_truncated_tail(
    audit_engine: Engine,
) -> None:
    records: list[AuditRecordV2] = []
    with _uow(audit_engine).begin() as transaction:
        for index in range(1, 4):
            records.append(_append(_gate(transaction.audit), resource_id=f"artifact:{index}"))

    second = records[1]
    with Session(audit_engine) as session:
        session.execute(
            delete(AuditRow).where(
                AuditRow.chain_id == second.chain_id,
                AuditRow.chain_seq > second.seq,
            )
        )
        session.execute(
            update(AuditHeadRow)
            .where(AuditHeadRow.chain_id == second.chain_id)
            .values(
                head_seq=second.seq,
                head_hash=second.content_hash,
                revision=second.seq,
                updated_at=second.ts,
            )
        )
        session.commit()

    # This is the documented threat-model boundary: without an external
    # trusted head, a locally self-consistent truncation is not provable.
    with _uow(audit_engine).begin() as transaction:
        assert transaction.audit.verify_chain(second.chain_id) is True


def test_multi_connection_writers_have_unique_contiguous_chain_sequences(
    audit_engine: Engine,
) -> None:
    barrier = Barrier(2)

    def write(index: int) -> AuditRecordV2:
        barrier.wait()
        with _uow(audit_engine).begin() as transaction:
            return _append(
                _gate(transaction.audit),
                action=f"artifact.publish.{index}",
                resource_id=f"artifact:{index}",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(write, (1, 2)))

    assert sorted(record.seq for record in records) == [1, 2]
    by_seq = sorted(records, key=lambda record: record.seq)
    assert by_seq[1].prev_hash == by_seq[0].content_hash
    with _uow(audit_engine).begin() as transaction:
        assert transaction.audit.verify_chain("platform-authority") is True
