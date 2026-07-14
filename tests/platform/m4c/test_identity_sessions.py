from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.auth import (
    LoginNameNormalizationPolicyV1,
    PasswordAuthRequestV1,
    PasswordCredentialRecordV1,
    PasswordHashPolicyV1,
    SecretText,
    SessionContextV1,
    SessionIssueRequestV1,
    SessionIssueV1,
    SessionManager,
    SessionPolicyV1,
    SessionRecordV1,
    SessionToken,
    compute_login_name_normalization_policy_digest,
)
from gameforge.contracts.errors import (
    AuthFailed,
    CredentialDisabled,
    Forbidden,
    IntegrityViolation,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.identity.sessions import (
    SessionAuthenticationCapabilities,
    SessionAuthenticationService,
    SessionManagerCapabilities,
    TransactionalSessionManager,
)
from gameforge.runtime.auth.local import LocalPasswordAuthenticator, LocalSessionRuntime
from gameforge.runtime.auth.passwords import Argon2PasswordRuntime
from gameforge.runtime.auth.tokens import (
    SessionSigningKey,
    SessionSigningKeySet,
    SessionTokenRuntime,
)
from gameforge.runtime.persistence.audit import AuditChainHead, SqlAuditSink
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import Base
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
        block = hashlib.sha512(f"entropy:{self._ordinal}".encode()).digest()
        return (block * ((size // len(block)) + 1))[:size]


class _SessionIds:
    def __init__(self) -> None:
        self._ordinal = 0

    def __call__(self) -> str:
        self._ordinal += 1
        return f"session:alice:{self._ordinal}"


class _FailingAudit:
    def lock_head(self, chain_id: str) -> AuditChainHead:
        return AuditChainHead(chain_id=chain_id, seq=0, content_hash=None, revision=0)

    def append(self, record: object) -> None:
        raise RuntimeError("audit append failed")

    def verify_chain(self, chain_id: str) -> bool:
        return True


class _UnusedSessionRuntime:
    def issue(self, request: SessionIssueRequestV1) -> SessionIssueV1:
        raise AssertionError("session issue is not part of this revoke test")

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
    ) -> SessionContextV1:
        raise AssertionError("session resolve is not part of this revoke test")


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'identity-sessions.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _normalization_policy() -> LoginNameNormalizationPolicyV1:
    payload = {
        "policy_version": "login-normalization@1",
        "unicode_normalization": "NFKC",
        "trim_unicode_whitespace": True,
        "case_mapping": "unicode_casefold",
        "reject_categories": ("control", "surrogate", "private_use"),
        "minimum_codepoints": 3,
        "maximum_codepoints": 128,
    }
    return LoginNameNormalizationPolicyV1(
        **payload,
        policy_digest=compute_login_name_normalization_policy_digest(payload),
    )


def _hash_policy(version: str, *, iterations: int) -> PasswordHashPolicyV1:
    return PasswordHashPolicyV1(
        policy_version=version,
        algorithm="argon2id",
        memory_kib=8192,
        iterations=iterations,
        parallelism=1,
        salt_bytes=16,
        rehash_on_login=True,
        effective_from="2026-07-14T00:00:00Z",
    )


def _session_policy() -> SessionPolicyV1:
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


def _seed_human(
    engine: Engine,
    *,
    clock: _Clock,
    password_runtime: Argon2PasswordRuntime,
    hash_policy: PasswordHashPolicyV1,
) -> PasswordCredentialRecordV1:
    normalization = _normalization_policy()
    with Session(engine) as session:
        identities = SqlIdentityRepository(session, clock=clock)
        auth = SqlAuthRepository(session, clock=clock)
        identities.create(principal_id="human:alice", kind="human", display_name="Alice")
        credential = auth.create_password(
            PasswordCredentialRecordV1(
                credential_id="password:alice",
                principal_id="human:alice",
                normalized_login_name="alice",
                normalization_policy_version=normalization.policy_version,
                normalization_policy_digest=normalization.policy_digest,
                password_hash=password_runtime.hash_password(
                    SecretText("correct-password"), hash_policy
                ),
                hash_policy_version=hash_policy.policy_version,
                credential_version=1,
                status="active",
                changed_at="2026-07-14T08:00:00Z",
                revision=1,
            )
        )
        session.commit()
        return credential


def _service(
    engine: Engine,
    *,
    clock: _Clock,
    password_runtime: Argon2PasswordRuntime,
    retained_hash_policy: PasswordHashPolicyV1,
    current_hash_policy: PasswordHashPolicyV1,
    session_ids: Callable[[], str] | None = None,
    audit_factory: Callable[[Session], object] | None = None,
) -> SessionAuthenticationService:
    normalization = _normalization_policy()
    session_policy = _session_policy()
    key_set = SessionSigningKeySet(
        key_set_version="keys@1",
        keys=(
            SessionSigningKey(
                key_id="session-key-1",
                secret=b"s" * 32,
                status="active",
            ),
        ),
    )
    token_runtime = SessionTokenRuntime(
        key_set_resolver=lambda version: key_set if version == key_set.key_set_version else None,
        token_digest_key=b"t" * 32,
        csrf_digest_key=b"c" * 32,
        random_bytes=_Entropy(),
    )
    id_generator = session_ids or _SessionIds()

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
        )

    unit_of_work = SqliteUnitOfWork(engine, capabilities)

    def bind(transaction: object) -> SessionAuthenticationCapabilities:
        password_authenticator = LocalPasswordAuthenticator(
            auth_repository=transaction.auth,  # type: ignore[attr-defined]
            identity_repository=transaction.identity,  # type: ignore[attr-defined]
            normalization_policy_resolver=lambda version, digest: (
                normalization
                if (version, digest) == (normalization.policy_version, normalization.policy_digest)
                else None
            ),
            hash_policy_resolver=lambda version: {
                retained_hash_policy.policy_version: retained_hash_policy,
                current_hash_policy.policy_version: current_hash_policy,
            }.get(version),
            current_hash_policy=current_hash_policy,
            password_runtime=password_runtime,
            clock=clock,
        )
        sessions = LocalSessionRuntime(
            auth_repository=transaction.auth,  # type: ignore[attr-defined]
            identity_repository=transaction.identity,  # type: ignore[attr-defined]
            session_policy_resolver=lambda version: (
                session_policy if version == session_policy.policy_version else None
            ),
            token_runtime=token_runtime,
            clock=clock,
            session_id_generator=id_generator,
        )
        return SessionAuthenticationCapabilities(
            password_authenticator=password_authenticator,
            session_runtime=sessions,
            identities=transaction.identity,  # type: ignore[attr-defined]
            audit=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[attr-defined]
        )

    return SessionAuthenticationService(
        unit_of_work=unit_of_work,
        bind_capabilities=bind,
        session_policy_version=session_policy.policy_version,
        audit_chain_id="platform-authority",
    )


def _transactional_session_manager(
    engine: Engine,
    *,
    clock: _Clock,
    audit_factory: Callable[[Session], object] | None = None,
) -> TransactionalSessionManager:
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
        )

    unit_of_work = SqliteUnitOfWork(engine, capabilities)

    def bind(transaction: object) -> SessionManagerCapabilities:
        return SessionManagerCapabilities(
            session_runtime=_UnusedSessionRuntime(),
            session_records=transaction.auth,  # type: ignore[attr-defined]
            identities=transaction.identity,  # type: ignore[attr-defined]
            audit=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[attr-defined]
        )

    return TransactionalSessionManager(
        unit_of_work=unit_of_work,
        bind_capabilities=bind,
        audit_chain_id="platform-authority",
    )


def test_password_login_rehash_session_insert_and_audit_commit_in_one_uow(
    engine: Engine,
) -> None:
    clock = _Clock()
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    retained_policy = _hash_policy("argon2@1", iterations=1)
    current_policy = _hash_policy("argon2@2", iterations=2)
    original = _seed_human(
        engine,
        clock=clock,
        password_runtime=password_runtime,
        hash_policy=retained_policy,
    )
    service = _service(
        engine,
        clock=clock,
        password_runtime=password_runtime,
        retained_hash_policy=retained_policy,
        current_hash_policy=current_policy,
    )

    issued = service.login(
        PasswordAuthRequestV1(
            login_name="\u3000ＡＬＩＣＥ\u00a0",
            password=SecretText("correct-password"),
        ),
        request_id="request:login:1",
    )

    with Session(engine) as session:
        auth = SqlAuthRepository(session, clock=clock)
        retained_password = auth.get_password(original.credential_id)
        retained_session = auth.get_session(issued.session_id)
        audit = SqlAuditSink(session).get("platform-authority", 1)
        assert retained_password is not None
        assert retained_password.hash_policy_version == current_policy.policy_version
        assert retained_password.revision == original.revision + 1
        assert retained_session is not None
        assert retained_session.source_credential_id == original.credential_id
        assert audit is not None
        assert audit.action == "identity.session_issued"
        assert audit.actor == AuditActor(
            principal_id="human:alice",
            principal_kind="human",
        )
        assert audit.subject.resource_kind == "session"
        assert audit.subject.resource_id == issued.session_id
        assert audit.correlation.request_id == "request:login:1"


def test_audit_failure_rolls_back_password_rehash_and_session_insert(engine: Engine) -> None:
    clock = _Clock()
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    retained_policy = _hash_policy("argon2@1", iterations=1)
    current_policy = _hash_policy("argon2@2", iterations=2)
    original = _seed_human(
        engine,
        clock=clock,
        password_runtime=password_runtime,
        hash_policy=retained_policy,
    )
    service = _service(
        engine,
        clock=clock,
        password_runtime=password_runtime,
        retained_hash_policy=retained_policy,
        current_hash_policy=current_policy,
        audit_factory=lambda session: _FailingAudit(),
    )

    with pytest.raises(RuntimeError, match="audit append failed"):
        service.login(
            PasswordAuthRequestV1(
                login_name="alice",
                password=SecretText("correct-password"),
            ),
            request_id="request:login:rollback",
        )

    with Session(engine) as session:
        auth = SqlAuthRepository(session, clock=clock)
        assert auth.get_password(original.credential_id) == original
        assert auth.get_session("session:alice:1") is None


def test_password_failures_are_externally_indistinguishable(engine: Engine) -> None:
    clock = _Clock()
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    policy = _hash_policy("argon2@1", iterations=1)
    credential = _seed_human(
        engine,
        clock=clock,
        password_runtime=password_runtime,
        hash_policy=policy,
    )
    service = _service(
        engine,
        clock=clock,
        password_runtime=password_runtime,
        retained_hash_policy=policy,
        current_hash_policy=policy,
    )

    failures: list[AuthFailed] = []
    for login_name, password in (
        ("unknown", "wrong"),
        ("alice", "wrong"),
    ):
        with pytest.raises(AuthFailed) as caught:
            service.login(
                PasswordAuthRequestV1(
                    login_name=login_name,
                    password=SecretText(password),
                ),
                request_id=f"request:login:{login_name}",
            )
        failures.append(caught.value)

    with Session(engine) as session:
        SqlAuthRepository(session, clock=clock).disable_password(
            credential.credential_id,
            expected_revision=credential.revision,
        )
        session.commit()
    with pytest.raises(AuthFailed) as caught:
        service.login(
            PasswordAuthRequestV1(
                login_name="alice",
                password=SecretText("correct-password"),
            ),
            request_id="request:login:disabled",
        )
    failures.append(caught.value)

    assert {(type(item), item.code, item.detail) for item in failures} == {
        (AuthFailed, "auth_failed", "password authentication failed")
    }


def test_password_login_does_not_mask_authority_integrity_failures(engine: Engine) -> None:
    clock = _Clock()
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    stored_policy = _hash_policy("argon2@stored", iterations=1)
    unavailable_policy = _hash_policy("argon2@unavailable", iterations=1)
    _seed_human(
        engine,
        clock=clock,
        password_runtime=password_runtime,
        hash_policy=stored_policy,
    )
    service = _service(
        engine,
        clock=clock,
        password_runtime=password_runtime,
        retained_hash_policy=unavailable_policy,
        current_hash_policy=unavailable_policy,
    )

    with pytest.raises(IntegrityViolation, match="hash policy is unavailable"):
        service.login(
            PasswordAuthRequestV1(
                login_name="alice",
                password=SecretText("correct-password"),
            ),
            request_id="request:login:integrity",
        )


def test_session_resolve_rebuilds_current_roles_and_rejects_stale_credential(
    engine: Engine,
) -> None:
    clock = _Clock()
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    policy = _hash_policy("argon2@1", iterations=1)
    credential = _seed_human(
        engine,
        clock=clock,
        password_runtime=password_runtime,
        hash_policy=policy,
    )
    service = _service(
        engine,
        clock=clock,
        password_runtime=password_runtime,
        retained_hash_policy=policy,
        current_hash_policy=policy,
    )
    issued = service.login(
        PasswordAuthRequestV1(
            login_name="alice",
            password=SecretText("correct-password"),
        ),
        request_id="request:login:roles",
    )

    with Session(engine) as session:
        identities = SqlIdentityRepository(session, clock=clock)
        principal = identities.get("human:alice")
        assert principal is not None
        identities.grant(
            assignment_id="role:alice:tooling",
            principal_id=principal.principal_id,
            role="tooling",
            scope=None,
            granted_by=AuditActor(
                principal_id="system:bootstrap",
                principal_kind="system",
            ),
            expected_principal_revision=principal.revision,
        )
        session.commit()

    actor = service.resolve(
        issued.session_token,
        csrf_token=None,
        request_method="GET",
        request_id="request:me:1",
    )
    assert actor.authentication.mechanism == "session"
    assert actor.authentication.credential_id == credential.credential_id
    assert actor.session_id == issued.session_id
    assert [assignment.role for assignment in actor.principal.roles] == ["tooling"]

    with Session(engine) as session:
        identities = SqlIdentityRepository(session, clock=clock)
        assignment = identities.get_assignment("role:alice:tooling")
        principal = identities.get("human:alice")
        assert assignment is not None and principal is not None
        identities.revoke(
            assignment_id=assignment.assignment_id,
            revoked_by=AuditActor(
                principal_id="system:bootstrap",
                principal_kind="system",
            ),
            revoke_reason="least_privilege",
            expected_principal_revision=principal.revision,
            expected_assignment_revision=assignment.revision,
        )
        session.commit()

    refreshed = service.resolve(
        issued.session_token,
        csrf_token=None,
        request_method="GET",
        request_id="request:me:2",
    )
    assert refreshed.principal.roles == ()
    assert refreshed.principal.authz_revision > actor.principal.authz_revision

    with Session(engine) as session:
        auth = SqlAuthRepository(session, clock=clock)
        current = auth.get_password(credential.credential_id)
        assert current is not None
        auth.compare_and_set_password(
            current.model_copy(
                update={
                    "credential_version": current.credential_version + 1,
                    "revision": current.revision + 1,
                }
            ),
            expected_revision=current.revision,
        )
        session.commit()

    with pytest.raises(CredentialDisabled, match="version"):
        service.resolve(
            issued.session_token,
            csrf_token=None,
            request_method="GET",
            request_id="request:me:stale",
        )


def test_transactional_session_manager_conforms_and_revoke_audit_is_atomic(
    engine: Engine,
) -> None:
    clock = _Clock()
    session_record = SessionRecordV1(
        session_id="session:alice:managed",
        principal_id="human:alice",
        source_credential_id="password:alice",
        credential_version=1,
        token_digest="1" * 64,
        csrf_secret_digest="2" * 64,
        signing_key_id="session-key-1",
        issued_at="2026-07-14T08:00:00Z",
        absolute_expires_at="2026-07-14T09:00:00Z",
        idle_expires_at="2026-07-14T08:10:00Z",
        last_seen_at="2026-07-14T08:00:00Z",
        revision=1,
    )
    actor = AuditActor(principal_id="human:alice", principal_kind="human")
    with Session(engine) as session:
        SqlIdentityRepository(session, clock=clock).create(
            principal_id=actor.principal_id,
            kind=actor.principal_kind,
            display_name="Alice",
        )
        SqlAuthRepository(session, clock=clock).create_session(session_record)
        session.commit()

    failing = _transactional_session_manager(
        engine,
        clock=clock,
        audit_factory=lambda session: _FailingAudit(),
    )
    assert isinstance(failing, SessionManager)
    with pytest.raises(RuntimeError, match="audit append failed"):
        failing.revoke(
            session_record.session_id,
            expected_revision=session_record.revision,
            reason="logout",
            actor=actor,
        )

    with Session(engine) as session:
        assert (
            SqlAuthRepository(session, clock=clock).get_session(session_record.session_id)
            == session_record
        )

    manager = _transactional_session_manager(engine, clock=clock)
    revoked = manager.revoke(
        session_record.session_id,
        expected_revision=session_record.revision,
        reason="logout",
        actor=actor,
    )
    assert revoked.revision == session_record.revision + 1
    assert revoked.revoke_reason == "logout"
    with Session(engine) as session:
        assert (
            SqlAuthRepository(session, clock=clock).get_session(session_record.session_id)
            == revoked
        )
        audit = SqlAuditSink(session).get("platform-authority", 1)
        assert audit is not None
        assert audit.actor == actor
        assert audit.action == "identity.session_revoked"
        assert audit.subject.resource_kind == "session"
        assert audit.subject.resource_id == session_record.session_id


def test_transactional_session_manager_rejects_cross_principal_revoke(
    engine: Engine,
) -> None:
    clock = _Clock()
    bob_session = SessionRecordV1(
        session_id="session:bob:managed",
        principal_id="human:bob",
        source_credential_id="password:bob",
        credential_version=1,
        token_digest="3" * 64,
        csrf_secret_digest="4" * 64,
        signing_key_id="session-key-1",
        issued_at="2026-07-14T08:00:00Z",
        absolute_expires_at="2026-07-14T09:00:00Z",
        idle_expires_at="2026-07-14T08:10:00Z",
        last_seen_at="2026-07-14T08:00:00Z",
        revision=1,
    )
    alice = AuditActor(principal_id="human:alice", principal_kind="human")
    with Session(engine) as session:
        identities = SqlIdentityRepository(session, clock=clock)
        identities.create(
            principal_id=alice.principal_id,
            kind=alice.principal_kind,
            display_name="Alice",
        )
        identities.create(
            principal_id="human:bob",
            kind="human",
            display_name="Bob",
        )
        SqlAuthRepository(session, clock=clock).create_session(bob_session)
        session.commit()

    manager = _transactional_session_manager(engine, clock=clock)
    with pytest.raises(Forbidden, match="owning human"):
        manager.revoke(
            bob_session.session_id,
            expected_revision=bob_session.revision,
            reason="logout",
            actor=alice,
        )

    with Session(engine) as session:
        assert (
            SqlAuthRepository(session, clock=clock).get_session(bob_session.session_id)
            == bob_session
        )
        assert SqlAuditSink(session).get("platform-authority", 1) is None
