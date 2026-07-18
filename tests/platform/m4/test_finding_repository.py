from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import Engine, delete, event
from sqlalchemy.orm import Session

from gameforge.contracts.errors import (
    Conflict,
    CursorInvalid,
    IntegrityViolation,
)
from gameforge.contracts.findings import (
    FindingPayloadV1,
    FindingRevisionV1,
    finding_revision_digest,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence import findings as finding_persistence
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.findings import SqlFindingRepository
from gameforge.runtime.persistence.models import (
    Base,
    FindingHeadRow,
    FindingRevisionRow,
    ReadSnapshotRow,
)
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
SIGNING_KEY = b"finding-repository-test-signing-key"


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'findings.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _finding(
    revision: int,
    *,
    finding_id: str = "finding-series-a",
    supersedes_revision: int | None = None,
    created_at: str | None = None,
    status: str = "confirmed",
    message: str | None = None,
) -> FindingRevisionV1:
    return FindingRevisionV1(
        finding_id=finding_id,
        revision=revision,
        supersedes_revision=supersedes_revision,
        created_at=created_at or f"2026-07-14T09:00:0{revision}Z",
        payload=FindingPayloadV1(
            source="checker",
            producer_id="graph-checker@1",
            producer_run_id="run-a",
            oracle_type="deterministic",
            defect_class="dangling_reference",
            severity="major",
            snapshot_id="snapshot-a",
            entities=["quest-a"],
            relations=["requires-a"],
            evidence={"missing_entity_id": "item-missing"},
            minimal_repro={"entity_id": "quest-a"},
            status=status,
            confidence=1.0,
            message=message or f"finding revision {revision}",
        ),
    )


def _repository(
    session: Session,
    *,
    now: datetime = NOW,
    page_size: int = 2,
    signing_key: bytes = SIGNING_KEY,
) -> SqlFindingRepository:
    clock = FrozenUtcClock(now)
    return SqlFindingRepository(
        session,
        cursor_signer=CursorSigner(signing_key=signing_key, clock=clock),
        clock=clock,
        page_size=page_size,
        snapshot_ttl=timedelta(minutes=5),
    )


def _unit_of_work(
    engine: Engine,
    *,
    page_size: int = 2,
) -> SqliteUnitOfWork:
    def capabilities(session: Session) -> TransactionCapabilities:
        unavailable = object()
        return TransactionCapabilities(
            refs=unavailable,
            audit=unavailable,
            approvals=unavailable,
            lineage=_repository(session, page_size=page_size),
            object_bindings=unavailable,
            runs=unavailable,
            cost=unavailable,
        )

    return SqliteUnitOfWork(engine, capabilities)


def _put(
    engine: Engine,
    item: FindingRevisionV1,
    expected_current_revision: int | None,
) -> FindingRevisionV1:
    with _unit_of_work(engine).begin() as transaction:
        return transaction.lineage.put(
            item,
            expected_current_revision=expected_current_revision,
        )


def _collect_revisions(
    engine: Engine,
    finding_id: str,
    *,
    page_size: int = 2,
) -> list[FindingRevisionV1]:
    revisions: list[FindingRevisionV1] = []
    cursor = None
    while True:
        with Session(engine) as session, session.begin():
            page = _repository(session, page_size=page_size).revisions(
                finding_id,
                cursor,
            )
        revisions.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return revisions


def test_put_requires_explicit_expected_revision_and_creates_revision_one(
    engine: Engine,
) -> None:
    first = _finding(1)

    with Session(engine) as session, session.begin():
        repository = _repository(session)
        with pytest.raises(TypeError, match="expected_current_revision"):
            repository.put(first)  # type: ignore[call-arg]
        stored = repository.put(first, expected_current_revision=None)

    assert stored == first
    with Session(engine) as session:
        row = session.get(FindingRevisionRow, (first.finding_id, 1))
        head = session.get(FindingHeadRow, first.finding_id)
        assert row is not None
        assert row.finding_digest == finding_revision_digest(first)
        assert head is not None
        assert head.current_revision == 1
        assert head.current_digest == finding_revision_digest(first)
        assert head.row_revision == 1


def test_put_many_preloads_series_and_uses_constant_raw_dml_for_the_batch(
    engine: Engine,
) -> None:
    def exercise(
        prefix: str,
        count: int,
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        selects: list[str] = []
        writes_seen: list[str] = []
        flushes = 0

        def observe_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            operation = statement.lstrip().upper().split(maxsplit=1)[0]
            if operation in {"SELECT", "WITH"}:
                selects.append(statement)
            elif operation in {"INSERT", "UPDATE", "DELETE"}:
                writes_seen.append(statement)

        def observe_flush(
            _session: Session,
            _flush_context: object,
            _instances: object,
        ) -> None:
            nonlocal flushes
            flushes += 1

        writes = tuple(
            (
                _finding(1, finding_id=f"{prefix}-{ordinal}"),
                None,
            )
            for ordinal in range(count)
        )
        with Session(engine, autoflush=False, expire_on_commit=False) as session:
            event.listen(engine, "before_cursor_execute", observe_statement)
            event.listen(session, "before_flush", observe_flush)
            try:
                with session.begin():
                    repository = _repository(session)
                    seal = repository.preflight_put_many(writes)
                    preflight_counts = (len(selects), len(writes_seen), flushes)
                    selects.clear()
                    writes_seen.clear()
                    assert repository.put_preflighted_many(seal) == tuple(
                        item for item, _expected in writes
                    )
                    write_counts = (len(selects), len(writes_seen), flushes)
            finally:
                event.remove(session, "before_flush", observe_flush)
                event.remove(engine, "before_cursor_execute", observe_statement)
        return preflight_counts, write_counts

    expected = ((4, 0, 0), (0, 2, 0))
    assert exercise("single", 1) == expected
    assert exercise("batch", 8) == expected


@pytest.mark.parametrize("history_depth", (0, 1, 8, 32))
def test_finding_preflight_uses_only_exact_head_and_revision_lookups(
    engine: Engine,
    history_depth: int,
) -> None:
    finding_id = f"finding:history-shape:{history_depth}"
    for revision in range(1, history_depth + 1):
        _put(
            engine,
            _finding(
                revision,
                finding_id=finding_id,
                supersedes_revision=None if revision == 1 else revision - 1,
                created_at=(NOW + timedelta(seconds=revision)).isoformat().replace("+00:00", "Z"),
            ),
            None if revision == 1 else revision - 1,
        )

    next_revision = history_depth + 1
    write = (
        _finding(
            next_revision,
            finding_id=finding_id,
            supersedes_revision=None if history_depth == 0 else history_depth,
            created_at=(NOW + timedelta(seconds=next_revision)).isoformat().replace("+00:00", "Z"),
        ),
        None if history_depth == 0 else history_depth,
    )
    selects: list[tuple[str, object]] = []

    def observe_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            selects.append((statement, parameters))

    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        event.listen(engine, "before_cursor_execute", observe_statement)
        try:
            with session.begin():
                _repository(session).preflight_put_many((write,))
        finally:
            event.remove(engine, "before_cursor_execute", observe_statement)

    assert len(selects) == 4
    normalized_sql = "\n".join(statement.upper() for statement, _parameters in selects)
    assert "MAX(" not in normalized_sql
    assert "GROUP BY" not in normalized_sql

    with engine.connect() as connection:
        plans = [
            connection.exec_driver_sql(
                f"EXPLAIN QUERY PLAN {statement}",
                parameters,
            ).all()
            for statement, parameters in selects
        ]
    revision_plans = "\n".join(
        str(row[-1]).upper()
        for (statement, _parameters), plan in zip(selects, plans, strict=True)
        if "FINDING_REVISIONS" in statement.upper()
        for row in plan
    )
    assert "SCAN FINDING_REVISIONS" not in revision_plans
    assert "SEARCH FINDING_REVISIONS" in revision_plans
    boundary_plan = next(
        plan
        for (statement, _parameters), plan in zip(selects, plans, strict=True)
        if statement.lstrip().upper().startswith("WITH REQUESTED")
    )
    boundary_details = "\n".join(str(row[-1]).upper() for row in boundary_plan)
    assert "SEARCH CANDIDATE" in boundary_details
    assert "(FINDING_ID=?)" in boundary_details
    assert "(FINDING_ID=? AND REVISION>?)" in boundary_details


def test_finding_preflight_seek_first_detects_a_hidden_headless_orphan(
    engine: Engine,
) -> None:
    orphan = _finding(
        7,
        finding_id="finding:hidden-headless-orphan",
        supersedes_revision=6,
    )
    with Session(engine) as session, session.begin():
        session.add(
            FindingRevisionRow(
                finding_id=orphan.finding_id,
                revision=orphan.revision,
                revision_schema_version=orphan.revision_schema_version,
                supersedes_revision=orphan.supersedes_revision,
                finding_digest=finding_revision_digest(orphan),
                created_at=orphan.created_at,
                payload=orphan.payload.model_dump(mode="json"),
            )
        )

    requested = _finding(1, finding_id=orphan.finding_id)
    with Session(engine, autoflush=False, expire_on_commit=False) as session, session.begin():
        with pytest.raises(IntegrityViolation, match="without a head"):
            _repository(session).preflight_put_many(((requested, None),))


def test_finding_preflight_seek_first_detects_a_hidden_future_revision(
    engine: Engine,
) -> None:
    first = _finding(1, finding_id="finding:hidden-future")
    _put(engine, first, None)
    future = _finding(
        3,
        finding_id=first.finding_id,
        supersedes_revision=2,
    )
    with Session(engine) as session, session.begin():
        session.add(
            FindingRevisionRow(
                finding_id=future.finding_id,
                revision=future.revision,
                revision_schema_version=future.revision_schema_version,
                supersedes_revision=future.supersedes_revision,
                finding_digest=finding_revision_digest(future),
                created_at=future.created_at,
                payload=future.payload.model_dump(mode="json"),
            )
        )

    requested = _finding(
        2,
        finding_id=first.finding_id,
        supersedes_revision=1,
    )
    with Session(engine, autoflush=False, expire_on_commit=False) as session, session.begin():
        with pytest.raises(IntegrityViolation, match="newer than its head"):
            _repository(session).preflight_put_many(((requested, 1),))


def test_put_many_chunks_901_findings_without_an_orm_flush(engine: Engine) -> None:
    writes = tuple((_finding(1, finding_id=f"chunked-{ordinal}"), None) for ordinal in range(901))
    selects: list[str] = []
    writes_seen: list[str] = []
    flushes = 0

    def observe_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        operation = statement.lstrip().upper().split(maxsplit=1)[0]
        if operation in {"SELECT", "WITH"}:
            selects.append(statement)
        elif operation in {"INSERT", "UPDATE", "DELETE"}:
            writes_seen.append(statement)

    def observe_flush(
        _session: Session,
        _flush_context: object,
        _instances: object,
    ) -> None:
        nonlocal flushes
        flushes += 1

    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        event.listen(engine, "before_cursor_execute", observe_statement)
        event.listen(session, "before_flush", observe_flush)
        try:
            with session.begin():
                repository = _repository(session)
                seal = repository.preflight_put_many(writes)
                assert len(selects) == 10
                assert writes_seen == []
                assert flushes == 0
                selects.clear()
                assert repository.put_preflighted_many(seal) == tuple(
                    item for item, _expected in writes
                )
        finally:
            event.remove(session, "before_flush", observe_flush)
            event.remove(engine, "before_cursor_execute", observe_statement)

    assert selects == []
    assert len(writes_seen) == 2
    assert flushes == 0


def test_finding_preflight_seal_rejects_cross_transaction_and_reuse_before_dml(
    engine: Engine,
) -> None:
    write = (_finding(1, finding_id="finding:sealed"), None)
    dml: list[str] = []

    def observe_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().split(maxsplit=1)[0] in {
            "INSERT",
            "UPDATE",
            "DELETE",
        }:
            dml.append(statement)

    event.listen(engine, "before_cursor_execute", observe_statement)
    try:
        with Session(engine, autoflush=False, expire_on_commit=False) as owner, owner.begin():
            repository = _repository(owner)
            seal = repository.preflight_put_many((write,))
            with pytest.raises((AttributeError, TypeError)):
                object.__setattr__(seal, "_revision_parameters", (("forged",),))
            with pytest.raises(TypeError):
                replace(seal)
            with pytest.raises(IntegrityViolation, match="trusted preflight seal"):
                repository.put_preflighted_many(copy(seal))
            assert dml == []
            with Session(engine, autoflush=False, expire_on_commit=False) as other, other.begin():
                with pytest.raises(IntegrityViolation, match="current transaction"):
                    _repository(other).put_preflighted_many(seal)
                assert dml == []

            assert repository.put_preflighted_many(seal) == (write[0],)
            dml.clear()
            with pytest.raises(IntegrityViolation, match="already been consumed"):
                repository.put_preflighted_many(seal)
            assert dml == []
    finally:
        event.remove(engine, "before_cursor_execute", observe_statement)


def test_finding_preflight_seal_applies_only_precomputed_database_rows(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = (_finding(1, finding_id="finding:precomputed-row"), None)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Finding payload work ran during sealed apply")

    with Session(engine, autoflush=False, expire_on_commit=False) as session, session.begin():
        repository = _repository(session)
        seal = repository.preflight_put_many((write,))
        with pytest.raises(TypeError, match="immutable"):
            seal._results = ()  # type: ignore[attr-defined]
        with pytest.raises((AttributeError, TypeError)):
            object.__setattr__(seal, "_results", ())

        with monkeypatch.context() as apply_phase:
            apply_phase.setattr(
                FindingRevisionV1,
                "model_validate",
                classmethod(forbidden),
            )
            apply_phase.setattr(FindingRevisionV1, "model_dump", forbidden)
            apply_phase.setattr(FindingPayloadV1, "model_dump", forbidden)
            apply_phase.setattr(finding_persistence, "finding_revision_digest", forbidden)
            apply_phase.setattr(finding_persistence, "typed_canonical_json", forbidden)
            apply_phase.setattr(json, "dumps", forbidden)

            assert repository.put_preflighted_many(seal) == (write[0],)

    with Session(engine) as session:
        row = session.get(FindingRevisionRow, (write[0].finding_id, 1))
        assert row is not None
        assert row.finding_digest == finding_revision_digest(write[0])
        assert row.payload == write[0].payload.model_dump(mode="json")


def test_put_many_preserves_series_cas_and_idempotency_in_input_order(
    engine: Engine,
) -> None:
    first = _finding(1)
    second = _finding(2, supersedes_revision=1)

    with Session(engine, autoflush=False, expire_on_commit=False) as session, session.begin():
        repository = _repository(session)
        assert repository.put_many(((first, None), (second, 1), (first, None))) == (
            first,
            second,
            first,
        )

    with Session(engine) as session:
        repository = _repository(session)
        assert repository.current(first.finding_id) == second
        head = session.get(FindingHeadRow, first.finding_id)
        assert head is not None
        assert head.current_revision == 2
        assert head.row_revision == 2


def test_put_many_rejects_an_in_batch_immutable_revision_conflict_without_writes(
    engine: Engine,
) -> None:
    first = _finding(1)
    changed = _finding(1, message="different immutable content")

    with Session(engine, autoflush=False, expire_on_commit=False) as session, session.begin():
        with pytest.raises(IntegrityViolation, match="different immutable content"):
            _repository(session).put_many(((first, None), (changed, None)))

    with Session(engine) as session:
        assert session.get(FindingRevisionRow, (first.finding_id, 1)) is None
        assert session.get(FindingHeadRow, first.finding_id) is None


def test_finding_preflight_detects_retained_drift_before_the_first_dml(
    engine: Engine,
) -> None:
    first = _finding(1)
    changed = _finding(1, message="different retained content")
    assert _put(engine, first, None) == first
    dml: list[str] = []

    def observe_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().split(maxsplit=1)[0] in {
            "INSERT",
            "UPDATE",
            "DELETE",
        }:
            dml.append(statement)

    event.listen(engine, "before_cursor_execute", observe_statement)
    try:
        with Session(engine, autoflush=False, expire_on_commit=False) as session, session.begin():
            with pytest.raises(IntegrityViolation, match="different immutable content"):
                _repository(session).preflight_put_many(((changed, None),))
    finally:
        event.remove(engine, "before_cursor_execute", observe_statement)

    assert dml == []


def test_put_many_advances_a_retained_head_with_one_aggregate_cas(engine: Engine) -> None:
    first = _finding(1)
    second = _finding(2, supersedes_revision=1)
    third = _finding(3, supersedes_revision=2)
    assert _put(engine, first, None) == first

    with Session(engine, autoflush=False, expire_on_commit=False) as session, session.begin():
        assert _repository(session).put_many(((second, 1), (third, 2))) == (
            second,
            third,
        )

    with Session(engine) as session:
        repository = _repository(session)
        assert repository.current(first.finding_id) == third
        head = session.get(FindingHeadRow, first.finding_id)
        assert head is not None
        assert head.current_revision == 3
        assert head.current_digest == finding_revision_digest(third)
        assert head.row_revision == 3


def test_put_is_idempotent_only_for_the_exact_immutable_wire(engine: Engine) -> None:
    first = _finding(1)
    assert _put(engine, first, None) == first
    assert _put(engine, first, None) == first
    with pytest.raises(Conflict, match="idempotent retry"):
        _put(engine, first, 1)

    same_digest_different_time = _finding(
        1,
        created_at="2026-07-14T10:00:00Z",
    )
    assert finding_revision_digest(same_digest_different_time) == (finding_revision_digest(first))
    with pytest.raises(IntegrityViolation, match="different immutable content"):
        _put(engine, same_digest_different_time, None)

    changed_payload = _finding(1, message="changed semantic content")
    with pytest.raises(IntegrityViolation, match="different immutable content"):
        _put(engine, changed_payload, None)

    with Session(engine) as session:
        stored = _repository(session).get(first.finding_id, 1)
    assert stored == first


@pytest.mark.parametrize(
    ("stored_evidence", "different_evidence"),
    [
        ({}, {"x": None}),
        ({"x": 1.0}, {"x": "f:1"}),
        ({"x": True}, {"x": 1}),
        ({"x": -0.0}, {"x": 0.0}),
    ],
)
def test_put_does_not_collapse_distinct_typed_json_wire_values(
    engine: Engine,
    stored_evidence: dict[str, object],
    different_evidence: dict[str, object],
) -> None:
    base = _finding(1)
    stored = base.model_copy(
        update={"payload": base.payload.model_copy(update={"evidence": stored_evidence})}
    )
    different = base.model_copy(
        update={"payload": base.payload.model_copy(update={"evidence": different_evidence})}
    )
    _put(engine, stored, None)

    with pytest.raises(IntegrityViolation, match="different immutable content"):
        _put(engine, different, None)


def test_put_exact_wire_equality_ignores_map_insertion_order(engine: Engine) -> None:
    base = _finding(1)
    first = base.model_copy(
        update={
            "payload": base.payload.model_copy(
                update={"evidence": {"alpha": 1, "beta": [None, False, 2.5]}}
            )
        }
    )
    reordered = base.model_copy(
        update={
            "payload": base.payload.model_copy(
                update={"evidence": {"beta": [None, False, 2.5], "alpha": 1}}
            )
        }
    )

    assert _put(engine, first, None) == first
    assert _put(engine, reordered, None) == first


@pytest.mark.parametrize("nonfinite", [float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("location", ["evidence", "confidence"])
def test_put_rejects_nonfinite_values_before_publishing_any_row(
    engine: Engine,
    nonfinite: float,
    location: str,
) -> None:
    base = _finding(1)
    if location == "evidence":
        payload = base.payload.model_copy(update={"evidence": {"value": nonfinite}})
    else:
        payload = base.payload.model_copy(update={"confidence": nonfinite})
    invalid = base.model_copy(update={"payload": payload})

    with pytest.raises(IntegrityViolation, match="finding revision wire is invalid"):
        _put(engine, invalid, None)

    with Session(engine) as session:
        assert session.get(FindingRevisionRow, (invalid.finding_id, 1)) is None
        assert session.get(FindingHeadRow, invalid.finding_id) is None


@pytest.mark.parametrize("nonfinite", [float("inf"), float("-inf"), float("nan")])
def test_stored_nonfinite_finding_values_fail_closed(
    engine: Engine,
    nonfinite: float,
) -> None:
    first = _finding(1)
    _put(engine, first, None)

    with Session(engine) as session, session.begin():
        row = session.get(FindingRevisionRow, (first.finding_id, 1))
        assert row is not None
        row.payload = {**row.payload, "evidence": {"value": nonfinite}}

    with Session(engine) as session:
        repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="stored finding revision"):
            repository.get(first.finding_id, 1)
        with pytest.raises(IntegrityViolation, match="stored finding revision"):
            repository.current(first.finding_id)


def test_later_revision_must_be_exactly_current_plus_one_and_supersede_current(
    engine: Engine,
) -> None:
    first = _finding(1)
    _put(engine, first, None)

    with pytest.raises(Conflict, match="expected current revision"):
        _put(engine, _finding(2, supersedes_revision=1), None)
    with pytest.raises(Conflict, match="next revision"):
        _put(engine, _finding(3, supersedes_revision=1), 1)
    with pytest.raises(Conflict, match="supersede current"):
        _put(engine, _finding(2, supersedes_revision=None), 1)

    second = _finding(2, supersedes_revision=1, status="fixed")
    assert _put(engine, second, 1) == second
    assert _put(engine, second, 1) == second

    with Session(engine) as session:
        repository = _repository(session)
        assert repository.get(first.finding_id, 1) == first
        assert repository.get(first.finding_id, 2) == second
        assert repository.get(first.finding_id, 3) is None
        assert repository.current(first.finding_id) == second


def test_same_uow_reads_the_head_advanced_by_its_own_put(engine: Engine) -> None:
    first = _finding(1)
    second = _finding(2, supersedes_revision=1)

    with Session(engine) as session, session.begin():
        repository = _repository(session)
        repository.put(first, expected_current_revision=None)
        repository.put(second, expected_current_revision=1)

        assert repository.current(first.finding_id) == second
        assert repository.get(first.finding_id, 2) == second


def test_absent_series_current_get_and_revision_page_are_empty(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        assert repository.current("missing-series") is None
        assert repository.get("missing-series", 1) is None
        page = repository.revisions("missing-series")

    assert page.items == ()
    assert page.next_cursor is None


def test_uow_rollback_removes_revision_and_head_together(engine: Engine) -> None:
    first = _finding(1)

    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with _unit_of_work(engine).begin() as transaction:
            transaction.lineage.put(first, expected_current_revision=None)
            raise RuntimeError("rollback sentinel")

    with Session(engine) as session:
        assert session.get(FindingRevisionRow, (first.finding_id, 1)) is None
        assert session.get(FindingHeadRow, first.finding_id) is None


def test_revision_pages_use_an_immutable_series_high_watermark(engine: Engine) -> None:
    current = _finding(1)
    _put(engine, current, None)
    for revision in range(2, 6):
        next_revision = _finding(revision, supersedes_revision=revision - 1)
        _put(engine, next_revision, revision - 1)
        current = next_revision

    with Session(engine) as session, session.begin():
        first_page = _repository(session, page_size=2).revisions(current.finding_id)

    assert [item.revision for item in first_page.items] == [1, 2]
    assert first_page.next_cursor is not None
    with Session(engine) as session:
        snapshot = session.get(ReadSnapshotRow, first_page.read_snapshot_id)
        assert snapshot is not None
        assert snapshot.strategy == "immutable_high_watermark"
        assert snapshot.high_watermark == 5
        assert snapshot.materialized_item_count is None

    sixth = _finding(6, supersedes_revision=5)
    _put(engine, sixth, 5)

    with Session(engine) as session, session.begin():
        second_page = _repository(session, page_size=2).revisions(
            current.finding_id,
            first_page.next_cursor,
        )
    assert [item.revision for item in second_page.items] == [3, 4]
    assert second_page.next_cursor is not None

    with Session(engine) as session, session.begin():
        final_page = _repository(session, page_size=2).revisions(
            current.finding_id,
            second_page.next_cursor,
        )
    assert [item.revision for item in final_page.items] == [5]
    assert final_page.next_cursor is None
    assert [item.revision for item in _collect_revisions(engine, current.finding_id)] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]


def test_revision_cursor_is_bound_to_finding_series(engine: Engine) -> None:
    for finding_id in ("finding-a", "finding-b"):
        _put(engine, _finding(1, finding_id=finding_id), None)
        _put(
            engine,
            _finding(2, finding_id=finding_id, supersedes_revision=1),
            1,
        )

    with Session(engine) as session, session.begin():
        first_page = _repository(session, page_size=1).revisions("finding-a")

    assert first_page.next_cursor is not None
    with Session(engine) as session:
        with pytest.raises(CursorInvalid, match="another query"):
            _repository(session, page_size=1).revisions(
                "finding-b",
                first_page.next_cursor,
            )


def test_stored_revision_digest_and_payload_corruption_fail_closed(
    engine: Engine,
) -> None:
    first = _finding(1)
    _put(engine, first, None)

    with Session(engine) as session, session.begin():
        row = session.get(FindingRevisionRow, (first.finding_id, 1))
        assert row is not None
        row.payload = {**row.payload, "message": "tampered"}

    with Session(engine) as session:
        repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="digest"):
            repository.get(first.finding_id, 1)
        with pytest.raises(IntegrityViolation, match="digest"):
            repository.current(first.finding_id)


def test_corrupt_or_orphan_head_state_fails_closed(engine: Engine) -> None:
    first = _finding(1)
    _put(engine, first, None)

    with Session(engine) as session, session.begin():
        head = session.get(FindingHeadRow, first.finding_id)
        assert head is not None
        head.current_digest = "0" * 64

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="head"):
            _repository(session).current(first.finding_id)

    orphan = _finding(1, finding_id="orphan-series")
    with Session(engine) as session, session.begin():
        session.add(
            FindingRevisionRow(
                finding_id=orphan.finding_id,
                revision=orphan.revision,
                revision_schema_version=orphan.revision_schema_version,
                supersedes_revision=orphan.supersedes_revision,
                finding_digest=finding_revision_digest(orphan),
                created_at=orphan.created_at,
                payload=orphan.payload.model_dump(mode="json"),
            )
        )

    with Session(engine) as session:
        repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="without a head"):
            repository.current(orphan.finding_id)
        with pytest.raises(IntegrityViolation, match="without a head"):
            repository.get(orphan.finding_id, 1)


def test_stored_revision_chain_and_head_row_revision_corruption_fail_closed(
    engine: Engine,
) -> None:
    first = _finding(1)
    second = _finding(2, supersedes_revision=1)
    _put(engine, first, None)
    _put(engine, second, 1)

    with Session(engine) as session, session.begin():
        row = session.get(FindingRevisionRow, (second.finding_id, 2))
        assert row is not None
        row.supersedes_revision = None
        corrupted = FindingRevisionV1(
            finding_id=second.finding_id,
            revision=2,
            supersedes_revision=None,
            created_at=second.created_at,
            payload=second.payload,
        )
        row.finding_digest = finding_revision_digest(corrupted)

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="stored finding revision"):
            _repository(session).current(second.finding_id)

    other = _finding(1, finding_id="head-row-revision-corrupt")
    _put(engine, other, None)
    with Session(engine) as session, session.begin():
        head = session.get(FindingHeadRow, other.finding_id)
        assert head is not None
        head.row_revision = 2

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="stored finding head"):
            _repository(session).current(other.finding_id)


def test_revision_page_detects_a_missing_middle_revision(engine: Engine) -> None:
    _put(engine, _finding(1), None)
    _put(engine, _finding(2, supersedes_revision=1), 1)
    _put(engine, _finding(3, supersedes_revision=2), 2)
    _put(engine, _finding(4, supersedes_revision=3), 3)

    with Session(engine) as session, session.begin():
        session.execute(
            delete(FindingRevisionRow).where(
                FindingRevisionRow.finding_id == "finding-series-a",
                FindingRevisionRow.revision == 2,
            )
        )

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="missing or reordered"):
            _repository(session, page_size=10).revisions("finding-series-a")


def test_two_sqlite_writers_publish_one_next_revision(engine: Engine) -> None:
    _put(engine, _finding(1), None)
    ready = Barrier(2)

    def compete(message: str) -> tuple[str, FindingRevisionV1 | None]:
        ready.wait()
        candidate = _finding(2, supersedes_revision=1, message=message)
        try:
            return ("winner", _put(engine, candidate, 1))
        except Conflict:
            return ("conflict", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(compete, ("candidate-a", "candidate-b")))

    assert sorted(outcome for outcome, _ in outcomes) == ["conflict", "winner"]
    winner = next(item for outcome, item in outcomes if outcome == "winner")
    assert winner is not None
    with Session(engine) as session:
        repository = _repository(session)
        assert repository.current("finding-series-a") == winner
    assert [item.revision for item in _collect_revisions(engine, "finding-series-a")] == [
        1,
        2,
    ]
