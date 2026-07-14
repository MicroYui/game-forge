from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from gameforge.contracts.auth import (
    LoginNameNormalizationPolicyV1,
    PasswordCredentialRecordV1,
    PasswordHashPolicyV1,
    SessionPolicyV1,
    compute_login_name_normalization_policy_digest,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import AuditActor, AuditCorrelation, AuditSubject
from gameforge.platform.audit.gate import AuditGate
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import Base, PolicySnapshotRow
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
CLOCK = FrozenUtcClock(NOW)


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'auth-policies.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _normalization_policy(*, minimum: int = 3) -> LoginNameNormalizationPolicyV1:
    payload = {
        "policy_version": "login-normalization/1",
        "unicode_normalization": "NFKC",
        "trim_unicode_whitespace": True,
        "case_mapping": "unicode_casefold",
        "reject_categories": ("control", "surrogate", "private_use"),
        "minimum_codepoints": minimum,
        "maximum_codepoints": 128,
    }
    return LoginNameNormalizationPolicyV1(
        **payload,
        policy_digest=compute_login_name_normalization_policy_digest(payload),
    )


def _hash_policy(*, iterations: int = 2) -> PasswordHashPolicyV1:
    return PasswordHashPolicyV1(
        policy_version="argon2/1",
        algorithm="argon2id",
        memory_kib=8192,
        iterations=iterations,
        parallelism=1,
        salt_bytes=16,
        rehash_on_login=True,
        effective_from="2026-07-14T00:00:00Z",
    )


def _session_policy(*, idle_ttl_s: int = 600) -> SessionPolicyV1:
    return SessionPolicyV1(
        policy_version="session/1",
        absolute_ttl_s=3600,
        idle_ttl_s=idle_ttl_s,
        touch_interval_s=60,
        signing_key_set_version="keys/1",
        csrf_mode="synchronizer_token",
        same_site="strict",
        secure_cookie_required=True,
    )


def _repository(session: Session) -> SqlPolicySnapshotRepository:
    return SqlPolicySnapshotRepository(session, clock=CLOCK)


def test_auth_policies_round_trip_through_existing_snapshot_authority(
    engine: Engine,
) -> None:
    normalization = _normalization_policy()
    password_hash = _hash_policy()
    session_policy = _session_policy()
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        repository.put_login_name_normalization_policy(normalization)
        repository.put_password_hash_policy(password_hash)
        repository.put_session_policy(session_policy)

    with Session(engine) as session:
        repository = _repository(session)
        assert (
            repository.get_login_name_normalization_policy(
                policy_version=normalization.policy_version,
                policy_digest=normalization.policy_digest,
            )
            == normalization
        )
        assert repository.get_password_hash_policy(password_hash.policy_version) == password_hash
        assert repository.get_session_policy(session_policy.policy_version) == session_policy
        rows = session.scalars(select(PolicySnapshotRow)).all()

    assert len(rows) == 3
    by_kind = {row.document_kind: row for row in rows}
    assert by_kind["login_name_normalization_policy"].document_digest == (
        normalization.policy_digest
    )
    assert by_kind["password_hash_policy"].document_digest == canonical_sha256(
        password_hash.model_dump(mode="json")
    )
    assert by_kind["session_policy"].document_digest == canonical_sha256(
        session_policy.model_dump(mode="json")
    )


@pytest.mark.parametrize("policy_kind", ["normalization", "password_hash", "session"])
def test_auth_policy_version_cannot_be_rebound(
    engine: Engine,
    policy_kind: str,
) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        if policy_kind == "normalization":
            repository.put_login_name_normalization_policy(_normalization_policy())
            replacement = _normalization_policy(minimum=4)
            put = repository.put_login_name_normalization_policy
        elif policy_kind == "password_hash":
            repository.put_password_hash_policy(_hash_policy())
            replacement = _hash_policy(iterations=3)
            put = repository.put_password_hash_policy
        else:
            repository.put_session_policy(_session_policy())
            replacement = _session_policy(idle_ttl_s=900)
            put = repository.put_session_policy
        with pytest.raises(IntegrityViolation, match="immutable content"):
            put(replacement)


@pytest.mark.parametrize("policy_kind", ["normalization", "password_hash", "session"])
def test_auth_policy_reader_rejects_corrupt_retained_rows(
    engine: Engine,
    policy_kind: str,
) -> None:
    document_kind = {
        "normalization": "login_name_normalization_policy",
        "password_hash": "password_hash_policy",
        "session": "session_policy",
    }[policy_kind]
    normalization = _normalization_policy()
    password_hash = _hash_policy()
    session_policy = _session_policy()
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        repository.put_login_name_normalization_policy(normalization)
        repository.put_password_hash_policy(password_hash)
        repository.put_session_policy(session_policy)

    with Session(engine) as session, session.begin():
        row = session.scalar(
            select(PolicySnapshotRow).where(PolicySnapshotRow.document_kind == document_kind)
        )
        assert row is not None
        if policy_kind == "normalization":
            payload = dict(row.payload)
            payload["minimum_codepoints"] = 4
            row.payload = payload
        else:
            row.document_digest = "0" * 64

    with Session(engine) as session:
        repository = _repository(session)
        with pytest.raises(IntegrityViolation):
            if policy_kind == "normalization":
                repository.get_login_name_normalization_policy(
                    policy_version=normalization.policy_version,
                    policy_digest=normalization.policy_digest,
                )
            elif policy_kind == "password_hash":
                repository.get_password_hash_policy(password_hash.policy_version)
            else:
                repository.get_session_policy(session_policy.policy_version)


def _capabilities(session: Session) -> TransactionCapabilities:
    return TransactionCapabilities(
        refs=None,
        audit=SqlAuditSink(session),
        approvals=None,
        lineage=None,
        object_bindings=None,
        runs=None,
        cost=None,
        identity=SqlIdentityRepository(session, clock=CLOCK),
        auth=SqlAuthRepository(session, clock=CLOCK),
        policies=SqlPolicySnapshotRepository(session, clock=CLOCK),
        idempotency=SqlIdempotencyRepository(session, clock=CLOCK),
    )


def _write_authority(transaction: object) -> None:
    principal_id = "human:alice"
    normalization = _normalization_policy()
    transaction.identity.create(  # type: ignore[attr-defined]
        principal_id=principal_id,
        kind="human",
        display_name="Alice",
    )
    transaction.policies.put_login_name_normalization_policy(normalization)  # type: ignore[attr-defined]
    transaction.auth.create_password(  # type: ignore[attr-defined]
        PasswordCredentialRecordV1(
            credential_id="password:alice:1",
            principal_id=principal_id,
            normalized_login_name="alice",
            normalization_policy_version=normalization.policy_version,
            normalization_policy_digest=normalization.policy_digest,
            password_hash="$argon2id$test-only",
            hash_policy_version="argon2/1",
            credential_version=1,
            status="active",
            changed_at="2026-07-14T09:00:00Z",
            revision=1,
        )
    )
    transaction.identity.disable(  # type: ignore[attr-defined]
        principal_id,
        disabled_reason="credential-rotation-test",
        expected_revision=1,
    )
    transaction.idempotency.put_result(  # type: ignore[attr-defined]
        scope="identity",
        operation="credential.rotate",
        key="request:1",
        request_hash="a" * 64,
        resource_kind="principal",
        resource_id=principal_id,
        response={"principal_id": principal_id, "revision": 2},
    )
    AuditGate(sink=transaction.audit, clock=CLOCK).append(  # type: ignore[attr-defined]
        chain_id="platform-authority",
        actor=AuditActor(principal_id="system:identity", principal_kind="system"),
        initiated_by=AuditActor(principal_id=principal_id, principal_kind="human"),
        action="credential.rotate",
        subject=AuditSubject(resource_kind="principal", resource_id=principal_id),
        correlation=AuditCorrelation(request_id="request:1"),
    )


@pytest.mark.parametrize("commit", [True, False])
def test_auth_identity_policy_idempotency_and_audit_share_one_uow(
    engine: Engine,
    commit: bool,
) -> None:
    uow = SqliteUnitOfWork(engine, _capabilities)
    if commit:
        with uow.begin() as transaction:
            _write_authority(transaction)
    else:
        with pytest.raises(RuntimeError, match="rollback sentinel"):
            with uow.begin() as transaction:
                _write_authority(transaction)
                raise RuntimeError("rollback sentinel")

    with Session(engine) as session:
        identity = SqlIdentityRepository(session, clock=CLOCK)
        auth = SqlAuthRepository(session, clock=CLOCK)
        policies = SqlPolicySnapshotRepository(session, clock=CLOCK)
        idempotency = SqlIdempotencyRepository(session, clock=CLOCK)
        principal = identity.get("human:alice")
        if not commit:
            assert principal is None
            assert auth.get_password("password:alice:1") is None
            assert (
                policies.get_login_name_normalization_policy(
                    policy_version="login-normalization/1",
                    policy_digest=_normalization_policy().policy_digest,
                )
                is None
            )
            assert (
                idempotency.get_result(
                    scope="identity",
                    operation="credential.rotate",
                    key="request:1",
                    request_hash="a" * 64,
                )
                is None
            )
            assert SqlAuditSink(session).lock_head("platform-authority").seq == 0
            return

        assert principal is not None
        assert principal.revision == 2
        assert principal.credential_epoch == 1
        assert auth.get_password("password:alice:1") is not None
        assert (
            policies.get_login_name_normalization_policy(
                policy_version="login-normalization/1",
                policy_digest=_normalization_policy().policy_digest,
            )
            is not None
        )
        assert idempotency.get_result(
            scope="identity",
            operation="credential.rotate",
            key="request:1",
            request_hash="a" * 64,
        ) == {"principal_id": "human:alice", "revision": 2}
        assert AuditGate(sink=SqlAuditSink(session), clock=CLOCK).verify_chain("platform-authority")
