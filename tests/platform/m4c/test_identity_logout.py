from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gameforge.contracts.auth import (
    PasswordCredentialRecordV1,
    SecretText,
    SessionPolicyV1,
    SessionRecordV1,
    SessionToken,
)
from gameforge.contracts.errors import CsrfFailed, SessionRevoked
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.identity.logout import (
    LogoutCapabilities,
    LogoutCommandService,
)
from gameforge.runtime.auth.local import LocalSessionRuntime
from gameforge.runtime.auth.tokens import (
    SessionSigningKey,
    SessionSigningKeySet,
    SessionTokenRuntime,
)
from gameforge.runtime.persistence.audit import AuditChainHead, SqlAuditSink
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import Base, IdempotencyRecordRow
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


T0 = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)


@dataclass
class _Clock:
    current: datetime = T0

    def now_utc(self) -> datetime:
        return self.current


class _Entropy:
    def __init__(self) -> None:
        self._ordinal = 0

    def __call__(self, size: int) -> bytes:
        self._ordinal += 1
        block = hashlib.sha512(f"logout:{self._ordinal}".encode()).digest()
        return (block * ((size // len(block)) + 1))[:size]


class _FailingAudit:
    def lock_head(self, chain_id: str) -> AuditChainHead:
        return AuditChainHead(chain_id=chain_id, seq=0, content_hash=None, revision=0)

    def append(self, record: object) -> None:
        raise RuntimeError("audit unavailable")

    def verify_chain(self, chain_id: str) -> bool:
        return True


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'logout.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _policy() -> SessionPolicyV1:
    return SessionPolicyV1(
        policy_version="session@1",
        absolute_ttl_s=3600,
        idle_ttl_s=600,
        touch_interval_s=60,
        signing_key_set_version="keys@1",
        csrf_mode="synchronizer_token",
        same_site="strict",
        secure_cookie_required=True,
    )


def _token_runtime() -> SessionTokenRuntime:
    keys = SessionSigningKeySet(
        key_set_version="keys@1",
        keys=(
            SessionSigningKey(
                key_id="session-key-1",
                secret=b"s" * 32,
                status="active",
            ),
        ),
    )
    return SessionTokenRuntime(
        key_set_resolver=lambda version: keys if version == keys.key_set_version else None,
        token_digest_key=b"t" * 32,
        csrf_digest_key=b"c" * 32,
        random_bytes=_Entropy(),
    )


def _seed_session(
    engine: Engine,
    *,
    clock: _Clock,
    token_runtime: SessionTokenRuntime,
) -> tuple[SessionToken, SecretText]:
    policy = _policy()
    issued = token_runtime.issue(
        session_id="session:alice:1",
        credential_version=1,
        policy=policy,
    )
    with Session(engine) as session:
        identities = SqlIdentityRepository(session, clock=clock)
        auth = SqlAuthRepository(session, clock=clock)
        identities.create(
            principal_id="human:alice",
            kind="human",
            display_name="Alice",
        )
        auth.create_password(
            PasswordCredentialRecordV1(
                credential_id="password:alice",
                principal_id="human:alice",
                normalized_login_name="alice",
                normalization_policy_version="normalization@1",
                normalization_policy_digest="a" * 64,
                password_hash="not-used-by-session-resolution",
                hash_policy_version="argon2@1",
                credential_version=1,
                status="active",
                changed_at="2026-07-14T08:00:00Z",
                revision=1,
            )
        )
        auth.create_session(
            SessionRecordV1(
                session_id="session:alice:1",
                principal_id="human:alice",
                source_credential_id="password:alice",
                credential_version=1,
                token_digest=issued.token_digest,
                csrf_secret_digest=issued.csrf_secret_digest,
                signing_key_id=issued.signing_key_id,
                issued_at="2026-07-14T08:00:00Z",
                absolute_expires_at="2026-07-14T09:00:00Z",
                idle_expires_at="2026-07-14T08:10:00Z",
                last_seen_at="2026-07-14T08:00:00Z",
                revision=1,
            )
        )
        session.commit()
    return issued.session_token, issued.csrf_token


def _service(
    engine: Engine,
    *,
    clock: _Clock,
    token_runtime: SessionTokenRuntime,
    audit_factory: Callable[[Session], object] | None = None,
) -> LogoutCommandService:
    policy = _policy()

    def capabilities(session: Session) -> TransactionCapabilities:
        return TransactionCapabilities(
            refs=None,
            audit=(audit_factory(session) if audit_factory is not None else SqlAuditSink(session)),
            approvals=None,
            lineage=None,
            object_bindings=None,
            runs=None,
            cost=None,
            identity=SqlIdentityRepository(session, clock=clock),
            auth=SqlAuthRepository(session, clock=clock),
            idempotency=SqlIdempotencyRepository(session, clock=clock),
        )

    unit_of_work = SqliteUnitOfWork(engine, capabilities)

    def bind(transaction: object) -> LogoutCapabilities:
        runtime = LocalSessionRuntime(
            auth_repository=transaction.auth,  # type: ignore[attr-defined]
            identity_repository=transaction.identity,  # type: ignore[attr-defined]
            session_policy_resolver=lambda version: (
                policy if version == policy.policy_version else None
            ),
            token_runtime=token_runtime,
            clock=clock,
            session_id_generator=lambda: "unused",
        )
        return LogoutCapabilities(
            session_runtime=runtime,
            session_records=transaction.auth,  # type: ignore[attr-defined]
            identities=transaction.identity,  # type: ignore[attr-defined]
            idempotency=transaction.idempotency,  # type: ignore[attr-defined]
            audit=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[attr-defined]
        )

    return LogoutCommandService(
        unit_of_work=unit_of_work,
        bind_capabilities=bind,
        audit_chain_id="platform-authority",
    )


def test_logout_cas_revoke_audit_and_idempotency_commit_atomically(engine: Engine) -> None:
    clock = _Clock()
    tokens = _token_runtime()
    token, csrf = _seed_session(engine, clock=clock, token_runtime=tokens)
    service = _service(engine, clock=clock, token_runtime=tokens)

    first = service.logout(
        token,
        csrf_token=csrf,
        idempotency_key="logout:alice:1",
        request_id="request:logout:1",
    )
    replay = service.logout(
        token,
        csrf_token=csrf,
        idempotency_key="logout:alice:1",
        request_id="request:logout:2",
    )

    assert first.session_id == replay.session_id == "session:alice:1"
    assert first.revoked_revision == replay.revoked_revision == 2
    assert first.replayed is False
    assert replay.replayed is True
    with Session(engine) as session:
        retained = SqlAuthRepository(session, clock=clock).get_session("session:alice:1")
        assert retained is not None
        assert retained.revision == 2
        assert retained.revoked_at == "2026-07-14T08:00:00Z"
        assert retained.revoke_reason == "logout"
        assert SqlAuditSink(session).get("platform-authority", 1) is not None
        assert SqlAuditSink(session).get("platform-authority", 2) is None
        assert session.scalar(select(func.count()).select_from(IdempotencyRecordRow)) == 1


def test_logout_wrong_csrf_and_unretained_revoked_request_fail_closed(engine: Engine) -> None:
    clock = _Clock()
    tokens = _token_runtime()
    token, csrf = _seed_session(engine, clock=clock, token_runtime=tokens)
    service = _service(engine, clock=clock, token_runtime=tokens)

    with pytest.raises(CsrfFailed):
        service.logout(
            token,
            csrf_token=SecretText("wrong"),
            idempotency_key="logout:wrong-csrf",
            request_id="request:logout:csrf",
        )
    service.logout(
        token,
        csrf_token=csrf,
        idempotency_key="logout:retained",
        request_id="request:logout:retained",
    )
    with pytest.raises(SessionRevoked):
        service.logout(
            token,
            csrf_token=csrf,
            idempotency_key="logout:not-retained",
            request_id="request:logout:not-retained",
        )


def test_logout_audit_failure_rolls_back_revoke_and_idempotency(engine: Engine) -> None:
    clock = _Clock()
    tokens = _token_runtime()
    token, csrf = _seed_session(engine, clock=clock, token_runtime=tokens)
    service = _service(
        engine,
        clock=clock,
        token_runtime=tokens,
        audit_factory=lambda session: _FailingAudit(),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.logout(
            token,
            csrf_token=csrf,
            idempotency_key="logout:audit-fails",
            request_id="request:logout:audit-fails",
        )

    with Session(engine) as session:
        retained = SqlAuthRepository(session, clock=clock).get_session("session:alice:1")
        assert retained is not None
        assert retained.revision == 1
        assert retained.revoked_at is None
        assert session.scalar(select(func.count()).select_from(IdempotencyRecordRow)) == 0
