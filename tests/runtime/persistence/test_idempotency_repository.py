from __future__ import annotations
from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.models import Base, IdempotencyRecordRow


NOW = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
REQUEST_HASH = "a" * 64


@pytest.fixture
def engine(tmp_path) -> Engine:
    database = get_engine(f"sqlite:///{tmp_path / 'idempotency.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _repository(session: Session) -> SqlIdempotencyRepository:
    return SqlIdempotencyRepository(session, clock=FrozenUtcClock(NOW))


def _put(repository: SqlIdempotencyRepository, *, request_hash: str = REQUEST_HASH):
    return repository.put_result(
        scope="principal:human:alice",
        operation="approval.decide",
        key="request:1",
        request_hash=request_hash,
        resource_kind="approval",
        resource_id="approval:1",
        response={"approval_id": "approval:1", "revision": 2, "optional": None},
    )


def test_same_scoped_request_replays_the_stored_response(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        first = _put(repository)
        replay = repository.put_result(
            scope="principal:human:alice",
            operation="approval.decide",
            key="request:1",
            request_hash=REQUEST_HASH,
            resource_kind="ignored-on-replay",
            resource_id="ignored-on-replay",
            response={"would_have_been": "different"},
        )

        assert replay == first
        replay["revision"] = 999
        assert repository.get_result(
            scope="principal:human:alice",
            operation="approval.decide",
            key="request:1",
            request_hash=REQUEST_HASH,
        ) == {"approval_id": "approval:1", "revision": 2, "optional": None}

    with Session(engine) as session:
        rows = session.scalars(select(IdempotencyRecordRow)).all()
        assert len(rows) == 1


def test_same_scoped_key_with_a_different_request_hash_conflicts(engine: Engine) -> None:
    with Session(engine) as session:
        repository = _repository(session)
        _put(repository)
        session.commit()

        with pytest.raises(Conflict, match="idempotency"):
            repository.get_result(
                scope="principal:human:alice",
                operation="approval.decide",
                key="request:1",
                request_hash="b" * 64,
            )
        with pytest.raises(Conflict, match="idempotency"):
            _put(repository, request_hash="b" * 64)


def test_scope_and_operation_are_part_of_the_idempotency_identity(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        _put(repository)
        second = repository.put_result(
            scope="principal:human:bob",
            operation="approval.decide",
            key="request:1",
            request_hash="b" * 64,
            resource_kind="approval",
            resource_id="approval:2",
            response={"approval_id": "approval:2"},
        )
        third = repository.put_result(
            scope="principal:human:alice",
            operation="approval.submit",
            key="request:1",
            request_hash="c" * 64,
            resource_kind="approval",
            resource_id="approval:1",
            response={"approval_id": "approval:1", "revision": 3},
        )

    assert second == {"approval_id": "approval:2"}
    assert third == {"approval_id": "approval:1", "revision": 3}
    with Session(engine) as session:
        assert len(session.scalars(select(IdempotencyRecordRow)).all()) == 3


def test_put_result_never_commits_its_own_transaction(engine: Engine) -> None:
    with Session(engine) as session:
        _put(_repository(session))
        session.rollback()

    with Session(engine) as session:
        assert session.scalar(select(IdempotencyRecordRow)) is None


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("request_hash", "not-a-hash", "request_hash"),
        ("resource_kind", "", "resource"),
        ("response", None, "response"),
        ("created_at", "not-a-timestamp", "timestamp"),
    ],
)
def test_corrupt_stored_result_fails_closed(
    engine: Engine,
    field: str,
    value: object,
    match: str,
) -> None:
    with Session(engine) as session:
        _put(_repository(session))
        session.commit()
        row = session.scalar(select(IdempotencyRecordRow))
        assert row is not None
        setattr(row, field, value)
        session.commit()

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match=match):
            _repository(session).get_result(
                scope="principal:human:alice",
                operation="approval.decide",
                key="request:1",
                request_hash=REQUEST_HASH,
            )


@pytest.mark.parametrize("field", ["scope", "operation", "key", "resource_kind", "resource_id"])
def test_empty_identifiers_are_rejected_before_storage(engine: Engine, field: str) -> None:
    values = {
        "scope": "principal:human:alice",
        "operation": "approval.decide",
        "key": "request:1",
        "request_hash": REQUEST_HASH,
        "resource_kind": "approval",
        "resource_id": "approval:1",
        "response": {"approval_id": "approval:1"},
    }
    values[field] = ""
    with Session(engine) as session:
        with pytest.raises(ValueError, match=field):
            _repository(session).put_result(**values)
