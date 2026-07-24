from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from gameforge.contracts.auth import (
    ApiKeyRecordV1,
    PasswordCredentialRecordV1,
    SessionRecordV1,
)
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import (
    ApiKeyRow,
    Base,
    PasswordCredentialRow,
    SessionRow,
)
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


T0 = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 14, 3, 0, tzinfo=timezone.utc)


@dataclass
class _MutableUtcClock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


@pytest.fixture
def clock() -> _MutableUtcClock:
    return _MutableUtcClock(T0)


def _repository(session: Session, clock: _MutableUtcClock) -> SqlAuthRepository:
    return SqlAuthRepository(session, clock=clock)


def _create_principals(session: Session, clock: _MutableUtcClock) -> None:
    identities = SqlIdentityRepository(session, clock=clock)
    identities.create(principal_id="human:alice", kind="human", display_name="Alice")
    identities.create(principal_id="service:worker", kind="service", display_name="Worker")


def _password(**updates: object) -> PasswordCredentialRecordV1:
    payload: dict[str, object] = {
        "credential_id": "password:alice:1",
        "principal_id": "human:alice",
        "normalized_login_name": "alice",
        "normalization_policy_version": "normalization@1",
        "normalization_policy_digest": "1" * 64,
        "password_hash": "$argon2id$v=19$fixture",
        "hash_policy_version": "argon2id@1",
        "credential_version": 1,
        "status": "active",
        "changed_at": "2026-07-14T01:00:00Z",
        "revision": 1,
    }
    payload.update(updates)
    return PasswordCredentialRecordV1.model_validate(payload)


def _api_key(**updates: object) -> ApiKeyRecordV1:
    payload: dict[str, object] = {
        "api_key_id": "api-key:worker:1",
        "principal_id": "service:worker",
        "key_prefix": "gfk_test",
        "key_digest": "2" * 64,
        "credential_version": 1,
        "status": "active",
        "created_at": "2026-07-14T01:00:00Z",
        "expires_at": "2026-07-15T01:00:00Z",
        "revoked_at": None,
        "revision": 1,
    }
    payload.update(updates)
    return ApiKeyRecordV1.model_validate(payload)


def _session(**updates: object) -> SessionRecordV1:
    payload: dict[str, object] = {
        "session_id": "session:alice:1",
        "principal_id": "human:alice",
        "source_credential_id": "password:alice:1",
        "credential_version": 1,
        "token_digest": "3" * 64,
        "csrf_secret_digest": "4" * 64,
        "signing_key_id": "session-signing@1",
        "issued_at": "2026-07-14T01:00:00Z",
        "absolute_expires_at": "2026-07-15T01:00:00Z",
        "idle_expires_at": "2026-07-14T03:00:00Z",
        "last_seen_at": "2026-07-14T01:00:00Z",
        "revoked_at": None,
        "revoke_reason": None,
        "revision": 1,
    }
    payload.update(updates)
    return SessionRecordV1.model_validate(payload)


def test_password_create_and_exact_normalized_lookup(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        _create_principals(session, clock)
        repository = _repository(session, clock)
        created = repository.create_password(_password())

        assert repository.get_password(created.credential_id) == created
        assert repository.get_password_by_normalized_login("alice") == created
        assert repository.get_password_by_normalized_login("Alice") is None


def test_password_id_content_and_normalized_login_collisions_fail_closed(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        _create_principals(session, clock)
        repository = _repository(session, clock)
        original = repository.create_password(_password())
        session.commit()

        with pytest.raises(Conflict, match="password credential already exists"):
            repository.create_password(original)
        session.rollback()

        with pytest.raises(IntegrityViolation, match="different password credential content"):
            repository.create_password(_password(password_hash="$argon2id$v=19$different"))
        session.rollback()

        with pytest.raises(Conflict, match="normalized login name"):
            repository.create_password(
                _password(
                    credential_id="password:alice:2",
                    password_hash="$argon2id$v=19$second",
                )
            )
        session.rollback()


def test_password_compare_and_set_then_disable(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        _create_principals(session, clock)
        repository = _repository(session, clock)
        original = repository.create_password(_password())
        session.commit()

        rehashed = _password(
            password_hash="$argon2id$v=19$rehash",
            hash_policy_version="argon2id@2",
            changed_at="2026-07-14T02:00:00Z",
            revision=2,
        )
        with pytest.raises(Conflict, match="password credential revision"):
            repository.compare_and_set_password(rehashed, expected_revision=2)
        session.rollback()
        assert repository.get_password(original.credential_id) == original

        assert repository.compare_and_set_password(rehashed, expected_revision=1) == rehashed
        session.commit()

        clock.current = T2
        disabled = repository.disable_password(
            original.credential_id,
            expected_revision=rehashed.revision,
        )
        session.commit()

    assert disabled.status == "disabled"
    assert disabled.revision == 3
    assert disabled.credential_version == rehashed.credential_version
    assert disabled.changed_at == "2026-07-14T03:00:00Z"


def test_api_key_digest_lookup_collision_and_revoke(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        _create_principals(session, clock)
        repository = _repository(session, clock)
        created = repository.create_api_key(_api_key())
        session.commit()

        assert repository.get_api_key(created.api_key_id) == created
        assert repository.get_api_key_by_digest(created.key_digest) == created
        assert repository.get_api_key_by_digest("f" * 64) is None

        with pytest.raises(IntegrityViolation, match="different API key content"):
            repository.create_api_key(_api_key(key_prefix="gfk_other"))
        session.rollback()

        with pytest.raises(Conflict, match="API key digest"):
            repository.create_api_key(
                _api_key(api_key_id="api-key:worker:2", key_prefix="gfk_second")
            )
        session.rollback()

        with pytest.raises(Conflict, match="API key revision"):
            repository.revoke_api_key(created.api_key_id, expected_revision=2)
        session.rollback()

        clock.current = T1
        revoked = repository.revoke_api_key(created.api_key_id, expected_revision=1)
        session.commit()

    assert revoked.status == "revoked"
    assert revoked.revoked_at == "2026-07-14T02:00:00Z"
    assert revoked.revision == 2


def test_session_token_lookup_touch_and_revoke(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        _create_principals(session, clock)
        repository = _repository(session, clock)
        repository.create_password(_password())
        created = repository.create_session(_session())
        session.commit()

        assert repository.get_session(created.session_id) == created
        assert repository.get_session_by_token_digest(created.token_digest) == created
        assert repository.get_session_by_token_digest("f" * 64) is None

        with pytest.raises(Conflict, match="session revision"):
            repository.touch_session(
                created.session_id,
                expected_revision=2,
                last_seen_at="2026-07-14T02:00:00Z",
                idle_expires_at="2026-07-14T04:00:00Z",
            )
        session.rollback()

        clock.current = T1
        touched = repository.touch_session(
            created.session_id,
            expected_revision=1,
            last_seen_at="2026-07-14T02:00:00Z",
            idle_expires_at="2026-07-14T04:00:00Z",
        )
        session.commit()
        assert touched.last_seen_at == "2026-07-14T02:00:00Z"
        assert touched.idle_expires_at == "2026-07-14T04:00:00Z"
        assert touched.revision == 2

        clock.current = T2
        revoked = repository.revoke_session(
            created.session_id,
            expected_revision=touched.revision,
            reason="logout",
        )
        session.commit()

        assert revoked.revoked_at == "2026-07-14T03:00:00Z"
        assert revoked.revoke_reason == "logout"
        assert revoked.revision == 3

        with pytest.raises(Conflict, match="session is revoked"):
            repository.touch_session(
                created.session_id,
                expected_revision=revoked.revision,
                last_seen_at="2026-07-14T03:00:00Z",
                idle_expires_at="2026-07-14T05:00:00Z",
            )


def test_session_token_digest_collision_fails_closed(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        _create_principals(session, clock)
        repository = _repository(session, clock)
        repository.create_password(_password())
        repository.create_session(_session())
        session.commit()

        with pytest.raises(IntegrityViolation, match="different session content"):
            repository.create_session(_session(csrf_secret_digest="5" * 64))
        session.rollback()

        with pytest.raises(Conflict, match="session token digest"):
            repository.create_session(
                _session(session_id="session:alice:2", csrf_secret_digest="6" * 64)
            )


def test_session_touch_cannot_revive_an_expired_session(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        _create_principals(session, clock)
        repository = _repository(session, clock)
        repository.create_password(_password())
        created = repository.create_session(_session())
        session.commit()

        clock.current = T2
        with pytest.raises(Conflict, match="session has expired"):
            repository.touch_session(
                created.session_id,
                expected_revision=created.revision,
                last_seen_at="2026-07-14T03:00:00Z",
                idle_expires_at="2026-07-14T05:00:00Z",
            )
        session.rollback()

        assert repository.get_session(created.session_id) == created


@pytest.mark.parametrize(
    ("last_seen_at", "message"),
    [
        ("2026-07-14T02:00:00+00:00", "canonical UTC timestamp"),
        ("2026-07-14T01:15:00Z", "clock moved backwards"),
        ("2026-07-14T02:00:00.000001Z", "later than repository time"),
    ],
)
def test_session_touch_rejects_invalid_caller_time_samples(
    engine: Engine,
    clock: _MutableUtcClock,
    last_seen_at: str,
    message: str,
) -> None:
    with Session(engine) as session:
        _create_principals(session, clock)
        repository = _repository(session, clock)
        repository.create_password(_password())
        created = repository.create_session(_session(last_seen_at="2026-07-14T01:30:00Z"))
        session.commit()

        clock.current = T1
        with pytest.raises(IntegrityViolation, match=message):
            repository.touch_session(
                created.session_id,
                expected_revision=created.revision,
                last_seen_at=last_seen_at,
                idle_expires_at="2026-07-14T04:00:00Z",
            )
        session.rollback()

        assert repository.get_session(created.session_id) == created


@pytest.mark.parametrize(
    ("row_type", "mutation", "reader", "message"),
    [
        (
            PasswordCredentialRow,
            lambda: (
                update(PasswordCredentialRow)
                .where(PasswordCredentialRow.credential_id == "password:alice:1")
                .values(revision=0)
            ),
            lambda repository: repository.get_password("password:alice:1"),
            "stored password credential row",
        ),
        (
            ApiKeyRow,
            lambda: (
                update(ApiKeyRow)
                .where(ApiKeyRow.api_key_id == "api-key:worker:1")
                .values(key_digest="not-a-digest")
            ),
            lambda repository: repository.get_api_key("api-key:worker:1"),
            "stored API key row",
        ),
        (
            SessionRow,
            lambda: (
                update(SessionRow)
                .where(SessionRow.session_id == "session:alice:1")
                .values(last_seen_at="not-a-timestamp")
            ),
            lambda repository: repository.get_session("session:alice:1"),
            "stored session",
        ),
    ],
)
def test_corrupt_rows_fail_closed(
    engine: Engine,
    clock: _MutableUtcClock,
    row_type,
    mutation,
    reader,
    message: str,
) -> None:
    del row_type
    with Session(engine) as session:
        _create_principals(session, clock)
        repository = _repository(session, clock)
        repository.create_password(_password())
        repository.create_api_key(_api_key())
        repository.create_session(_session())
        session.commit()
        session.execute(mutation())
        session.commit()
        session.expire_all()

        with pytest.raises(IntegrityViolation, match=message):
            reader(repository)


def test_repository_never_commits_or_rolls_back_its_session(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with Session(engine) as session, session.begin():
            _create_principals(session, clock)
            repository = _repository(session, clock)
            repository.create_password(_password())
            repository.create_api_key(_api_key())
            repository.create_session(_session())
            raise RuntimeError("rollback sentinel")

    with Session(engine) as session:
        assert session.scalars(select(PasswordCredentialRow)).all() == []
        assert session.scalars(select(ApiKeyRow)).all() == []
        assert session.scalars(select(SessionRow)).all() == []


def test_identity_and_auth_capabilities_share_one_uow_transaction(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    def capabilities(session: Session) -> TransactionCapabilities:
        return TransactionCapabilities(
            refs=None,
            audit=None,
            approvals=None,
            lineage=None,
            object_bindings=None,
            runs=None,
            cost=None,
            identity=SqlIdentityRepository(session, clock=clock),
            auth=SqlAuthRepository(session, clock=clock),
        )

    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with SqliteUnitOfWork(engine, capabilities).begin() as transaction:
            transaction.identity.create(
                principal_id="human:alice",
                kind="human",
                display_name="Alice",
            )
            transaction.auth.create_password(_password())
            assert transaction.auth.get_password("password:alice:1") == _password()
            raise RuntimeError("rollback sentinel")

    with Session(engine) as session:
        assert session.scalars(select(PasswordCredentialRow)).all() == []


def test_repository_clock_must_return_timezone_aware_utc(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    clock.current = datetime(2026, 7, 14, 1, 0)
    with Session(engine) as session:
        _create_principals(session, _MutableUtcClock(T0))
        repository = _repository(session, clock)
        repository.create_password(_password())
        with pytest.raises(IntegrityViolation, match="clock must return UTC"):
            repository.disable_password("password:alice:1", expected_revision=1)
