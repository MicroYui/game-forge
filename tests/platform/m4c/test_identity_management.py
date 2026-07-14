from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.auth import (
    ApiKeyRecordV1,
    LoginNameNormalizationPolicyV1,
    PasswordCredentialRecordV1,
    PasswordHashPolicyV1,
    SecretText,
    SessionRecordV1,
    compute_login_name_normalization_policy_digest,
)
from gameforge.contracts.errors import (
    Conflict,
    CredentialDisabled,
    CredentialExpired,
    Forbidden,
    IntegrityViolation,
    SessionExpired,
    SessionRevoked,
)
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
    Permission,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.identity.management import (
    IdentityManagementCapabilities,
    IdentityManagementService,
)
from gameforge.runtime.auth.passwords import Argon2PasswordRuntime
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


T0 = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
AUDIT_CHAIN_ID = "identity-authority"


@dataclass
class _Clock:
    current: datetime = T0

    def now_utc(self) -> datetime:
        return self.current


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'identity-management.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(
            domain_id="quest",
            display_name="Quest",
            tags=(),
            status="active",
        ),
        DomainDefinitionV1(
            domain_id="legacy-quest",
            display_name="Legacy Quest",
            tags=(),
            status="deprecated",
        ),
    )
    return DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )


def _role_policy(registry: DomainRegistryV1) -> RolePolicy:
    ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants = {
        "identity_admin": (
            Permission(
                action="identity.manage",
                resource_kind="identity",
                domain_scope=None,
            ),
        ),
        "tooling": (),
    }
    effective_from = "2026-07-14T00:00:00Z"
    return RolePolicy(
        policy_version="roles@1",
        domain_registry_ref=ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            "roles@1",
            ref,
            grants,
            effective_from,
        ),
    )


def _normalization_policy() -> LoginNameNormalizationPolicyV1:
    payload = {
        "policy_version": "normalization@1",
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


def _hash_policy() -> PasswordHashPolicyV1:
    return PasswordHashPolicyV1(
        policy_version="argon2id@1",
        algorithm="argon2id",
        memory_kib=8192,
        iterations=1,
        parallelism=1,
        salt_bytes=16,
        rehash_on_login=True,
        effective_from="2026-07-14T00:00:00Z",
    )


def _password_runtime() -> Argon2PasswordRuntime:
    return Argon2PasswordRuntime(random_bytes=lambda size: b"s" * size)


def _capabilities(clock: _Clock):
    def build(session: Session) -> TransactionCapabilities:
        return TransactionCapabilities(
            refs=None,
            audit=SqlAuditSink(session),
            approvals=None,
            lineage=None,
            object_bindings=None,
            runs=None,
            cost=None,
            identity=SqlIdentityRepository(session, clock=clock),
            auth=SqlAuthRepository(session, clock=clock),
            policies=SqlPolicySnapshotRepository(session, clock=clock),
        )

    return build


def _bind(clock: _Clock):
    def bind(transaction: Any) -> IdentityManagementCapabilities:
        return IdentityManagementCapabilities(
            identity=transaction.identity,
            auth=transaction.auth,
            policies=transaction.policies,
            audit=AuditGate(sink=transaction.audit, clock=clock),
        )

    return bind


@dataclass
class _Harness:
    engine: Engine
    clock: _Clock
    policy: RolePolicy
    actor: ActorContext
    service: IdentityManagementService


def _build_harness(engine: Engine) -> _Harness:
    clock = _Clock()
    registry = _registry()
    policy = _role_policy(registry)
    with Session(engine) as session, session.begin():
        policies = SqlPolicySnapshotRepository(session, clock=clock)
        policies.put_domain_registry(registry)
        policies.put_role_policy(policy)
        policies.put_login_name_normalization_policy(_normalization_policy())
        policies.put_password_hash_policy(_hash_policy())
        identities = SqlIdentityRepository(session, clock=clock)
        created = identities.create(
            principal_id="human:admin",
            kind="human",
            display_name="Admin",
        )
        identities.grant(
            assignment_id="role:admin:identity",
            principal_id=created.principal_id,
            role="identity_admin",
            scope=None,
            granted_by=AuditActor(
                principal_id="system:bootstrap",
                principal_kind="system",
            ),
            expected_principal_revision=created.revision,
        )
        principal = identities.project(created.principal_id)
        assert principal is not None
        auth = SqlAuthRepository(session, clock=clock)
        auth.create_password(
            _password_record(
                credential_id="password:admin",
                principal_id="human:admin",
                normalized_login_name="admin",
                password="admin-password",
            )
        )
        auth.create_session(
            _session_record(
                session_id="session:admin",
                principal_id="human:admin",
                source_credential_id="password:admin",
                token_digest="a" * 64,
                csrf_secret_digest="b" * 64,
            )
        )

    actor = ActorContext(
        principal=principal,
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:admin",
        ),
        session_id="session:admin",
        request_id="request:identity:1",
    )
    service = IdentityManagementService(
        unit_of_work=SqliteUnitOfWork(engine, _capabilities(clock)),
        bind_capabilities=_bind(clock),
        current_role_policy_version=policy.policy_version,
        current_role_policy_digest=policy.policy_digest,
        password_encoding_verifier=_password_runtime(),
        clock=clock,
        audit_chain_id=AUDIT_CHAIN_ID,
    )
    return _Harness(
        engine=engine,
        clock=clock,
        policy=policy,
        actor=actor,
        service=service,
    )


@pytest.fixture
def harness(engine: Engine) -> _Harness:
    return _build_harness(engine)


def _identity(harness: _Harness, principal_id: str):
    with Session(harness.engine) as session:
        return SqlIdentityRepository(session, clock=harness.clock).get(principal_id)


def _assignment(harness: _Harness, assignment_id: str):
    with Session(harness.engine) as session:
        return SqlIdentityRepository(session, clock=harness.clock).get_assignment(assignment_id)


def _password(harness: _Harness, credential_id: str):
    with Session(harness.engine) as session:
        return SqlAuthRepository(session, clock=harness.clock).get_password(credential_id)


def _api_key(harness: _Harness, api_key_id: str):
    with Session(harness.engine) as session:
        return SqlAuthRepository(session, clock=harness.clock).get_api_key(api_key_id)


def _session(harness: _Harness, session_id: str):
    with Session(harness.engine) as session:
        return SqlAuthRepository(session, clock=harness.clock).get_session(session_id)


def _audit(harness: _Harness, seq: int):
    with Session(harness.engine) as session:
        return SqlAuditSink(session).get(AUDIT_CHAIN_ID, seq)


def _password_record(
    *,
    credential_id: str = "password:alice",
    principal_id: str = "human:alice",
    credential_version: int = 1,
    revision: int = 1,
    normalized_login_name: str = "alice",
    password: str = "alice-password",
    changed_at: str = "2026-07-14T08:00:00Z",
) -> PasswordCredentialRecordV1:
    normalization = _normalization_policy()
    hash_policy = _hash_policy()
    return PasswordCredentialRecordV1(
        credential_id=credential_id,
        principal_id=principal_id,
        normalized_login_name=normalized_login_name,
        normalization_policy_version=normalization.policy_version,
        normalization_policy_digest=normalization.policy_digest,
        password_hash=_password_runtime().hash_password(SecretText(password), hash_policy),
        hash_policy_version=hash_policy.policy_version,
        credential_version=credential_version,
        status="active",
        changed_at=changed_at,
        revision=revision,
    )


def _api_key_record(
    *,
    api_key_id: str = "api-key:worker:1",
    principal_id: str = "service:worker",
    key_digest: str = "2" * 64,
    credential_version: int = 1,
) -> ApiKeyRecordV1:
    return ApiKeyRecordV1(
        api_key_id=api_key_id,
        principal_id=principal_id,
        key_prefix="gfk_test",
        key_digest=key_digest,
        credential_version=credential_version,
        status="active",
        created_at="2026-07-14T08:00:00Z",
        expires_at="2026-07-15T08:00:00Z",
        revision=1,
    )


def _session_record(
    *,
    session_id: str = "session:alice:1",
    principal_id: str = "human:alice",
    source_credential_id: str = "password:alice",
    token_digest: str = "3" * 64,
    csrf_secret_digest: str = "4" * 64,
) -> SessionRecordV1:
    return SessionRecordV1(
        session_id=session_id,
        principal_id=principal_id,
        source_credential_id=source_credential_id,
        credential_version=1,
        token_digest=token_digest,
        csrf_secret_digest=csrf_secret_digest,
        signing_key_id="session-key@1",
        issued_at="2026-07-14T08:00:00Z",
        absolute_expires_at="2026-07-15T08:00:00Z",
        idle_expires_at="2026-07-14T10:00:00Z",
        last_seen_at="2026-07-14T08:00:00Z",
        revision=1,
    )


def test_actor_is_reprojected_and_exact_current_permission_is_authoritative(
    harness: _Harness,
) -> None:
    stale_without_roles = harness.actor.model_copy(
        update={"principal": harness.actor.principal.model_copy(update={"roles": ()})}
    )

    created = harness.service.create_principal(
        actor=stale_without_roles,
        principal_id="human:alice",
        kind="human",
        display_name="Alice",
    )

    assert created == _identity(harness, "human:alice")
    audit = _audit(harness, 1)
    assert audit is not None
    assert audit.actor == AuditActor(
        principal_id="human:admin",
        principal_kind="human",
    )
    assert audit.correlation.request_id == harness.actor.request_id

    with Session(harness.engine) as session, session.begin():
        identities = SqlIdentityRepository(session, clock=harness.clock)
        identities.revoke(
            assignment_id="role:admin:identity",
            revoked_by=audit.actor,
            revoke_reason="access_removed",
            expected_principal_revision=2,
            expected_assignment_revision=1,
        )

    with pytest.raises(Forbidden):
        harness.service.create_principal(
            actor=harness.actor,
            principal_id="human:forbidden",
            kind="human",
            display_name="Forbidden",
        )
    assert _identity(harness, "human:forbidden") is None
    assert _audit(harness, 2) is None


def test_human_actor_cannot_create_system_principal(harness: _Harness) -> None:
    with pytest.raises(Forbidden, match="trusted internal"):
        harness.service.create_principal(
            actor=harness.actor,
            principal_id="system:worker",
            kind="system",
            display_name="System Worker",
        )

    assert _identity(harness, "system:worker") is None
    assert _audit(harness, 1) is None


def test_disable_and_role_changes_apply_exact_principal_and_assignment_cas(
    harness: _Harness,
) -> None:
    alice = harness.service.create_principal(
        actor=harness.actor,
        principal_id="human:alice",
        kind="human",
        display_name="Alice",
    )
    assignment = harness.service.grant_role(
        actor=harness.actor,
        assignment_id="role:alice:qa",
        principal_id=alice.principal_id,
        role="qa",
        scope=DomainScope(domain_ids=("quest",)),
        expected_principal_revision=alice.revision,
    )
    with pytest.raises(Conflict, match="principal revision"):
        harness.service.revoke_role(
            actor=harness.actor,
            assignment_id=assignment.assignment_id,
            expected_principal_revision=alice.revision,
            expected_assignment_revision=assignment.revision,
            reason="stale",
        )

    current = _identity(harness, alice.principal_id)
    assert current is not None
    revoked = harness.service.revoke_role(
        actor=harness.actor,
        assignment_id=assignment.assignment_id,
        expected_principal_revision=current.revision,
        expected_assignment_revision=assignment.revision,
        reason="rotation",
    )
    current = _identity(harness, alice.principal_id)
    assert current is not None
    disabled = harness.service.disable_principal(
        actor=harness.actor,
        principal_id=alice.principal_id,
        expected_revision=current.revision,
        reason="offboarded",
    )

    assert revoked == _assignment(harness, assignment.assignment_id)
    assert disabled.status == "disabled"
    assert (
        disabled.revision,
        disabled.authz_revision,
        disabled.credential_epoch,
    ) == (4, 3, 1)
    assert [_audit(harness, seq).action for seq in range(1, 5)] == [
        "identity.principal_created",
        "identity.role_granted",
        "identity.role_revoked",
        "identity.principal_disabled",
    ]


def test_password_authority_changes_are_human_only_and_bump_epoch_once(
    harness: _Harness,
) -> None:
    alice = harness.service.create_principal(
        actor=harness.actor,
        principal_id="human:alice",
        kind="human",
        display_name="Alice",
    )
    issued = harness.service.issue_password(
        actor=harness.actor,
        record=_password_record(),
        expected_principal_revision=alice.revision,
    )
    after_issue = _identity(harness, alice.principal_id)
    assert after_issue is not None
    assert (after_issue.revision, after_issue.credential_epoch) == (2, 1)

    harness.clock.current = T1
    rotated = harness.service.rotate_password(
        actor=harness.actor,
        replacement=_password_record(
            credential_version=2,
            revision=2,
            password="rotated-password",
            changed_at="2026-07-14T09:00:00Z",
        ),
        expected_principal_revision=after_issue.revision,
        expected_credential_revision=issued.revision,
    )
    after_rotate = _identity(harness, alice.principal_id)
    assert after_rotate is not None
    assert rotated.credential_version == 2
    assert (after_rotate.revision, after_rotate.credential_epoch) == (3, 2)

    revoked = harness.service.revoke_password(
        actor=harness.actor,
        credential_id=rotated.credential_id,
        expected_principal_revision=after_rotate.revision,
        expected_credential_revision=rotated.revision,
    )
    after_revoke = _identity(harness, alice.principal_id)
    assert after_revoke is not None
    assert revoked.status == "disabled"
    assert (after_revoke.revision, after_revoke.credential_epoch) == (4, 3)

    worker = harness.service.create_principal(
        actor=harness.actor,
        principal_id="service:worker",
        kind="service",
        display_name="Worker",
    )
    with pytest.raises(Conflict, match="human principal"):
        harness.service.issue_password(
            actor=harness.actor,
            record=_password_record(
                credential_id="password:worker",
                principal_id=worker.principal_id,
            ),
            expected_principal_revision=worker.revision,
        )
    assert _password(harness, "password:worker") is None


def test_api_key_issue_rotate_revoke_is_service_only_and_bumps_epoch_once(
    harness: _Harness,
) -> None:
    worker = harness.service.create_principal(
        actor=harness.actor,
        principal_id="service:worker",
        kind="service",
        display_name="Worker",
    )
    first = harness.service.issue_api_key(
        actor=harness.actor,
        record=_api_key_record(),
        expected_principal_revision=worker.revision,
    )
    after_issue = _identity(harness, worker.principal_id)
    assert after_issue is not None

    second = harness.service.rotate_api_key(
        actor=harness.actor,
        replacement=_api_key_record(
            api_key_id="api-key:worker:2",
            key_digest="5" * 64,
        ),
        replaces_api_key_id=first.api_key_id,
        expected_principal_revision=after_issue.revision,
        expected_replaced_revision=first.revision,
    )
    after_rotate = _identity(harness, worker.principal_id)
    assert after_rotate is not None
    assert _api_key(harness, first.api_key_id).status == "revoked"
    assert second == _api_key(harness, second.api_key_id)
    assert [
        (_audit(harness, seq).action, _audit(harness, seq).subject.resource_id) for seq in (3, 4)
    ] == [
        ("identity.api_key_revoked", first.api_key_id),
        ("identity.api_key_rotated", second.api_key_id),
    ]

    revoked = harness.service.revoke_api_key(
        actor=harness.actor,
        api_key_id=second.api_key_id,
        expected_principal_revision=after_rotate.revision,
        expected_api_key_revision=second.revision,
    )
    after_revoke = _identity(harness, worker.principal_id)
    assert after_revoke is not None
    assert revoked.status == "revoked"
    assert (after_revoke.revision, after_revoke.credential_epoch) == (4, 3)

    human = harness.service.create_principal(
        actor=harness.actor,
        principal_id="human:bob",
        kind="human",
        display_name="Bob",
    )
    with pytest.raises(Conflict, match="service principal"):
        harness.service.issue_api_key(
            actor=harness.actor,
            record=_api_key_record(
                api_key_id="api-key:bob",
                principal_id=human.principal_id,
                key_digest="6" * 64,
            ),
            expected_principal_revision=human.revision,
        )


def test_api_key_rotation_requires_a_new_version_one_credential(
    harness: _Harness,
) -> None:
    worker = harness.service.create_principal(
        actor=harness.actor,
        principal_id="service:worker",
        kind="service",
        display_name="Worker",
    )
    first = harness.service.issue_api_key(
        actor=harness.actor,
        record=_api_key_record(),
        expected_principal_revision=worker.revision,
    )
    after_issue = _identity(harness, worker.principal_id)
    assert after_issue is not None

    with pytest.raises(IntegrityViolation, match="version-1 revision-1"):
        harness.service.rotate_api_key(
            actor=harness.actor,
            replacement=_api_key_record(
                api_key_id="api-key:worker:2",
                key_digest="5" * 64,
                credential_version=2,
            ),
            replaces_api_key_id=first.api_key_id,
            expected_principal_revision=after_issue.revision,
            expected_replaced_revision=first.revision,
        )

    assert _api_key(harness, first.api_key_id) == first
    assert _api_key(harness, "api-key:worker:2") is None
    assert _identity(harness, worker.principal_id) == after_issue


def test_session_issue_rotate_revoke_uses_session_revision_without_epoch_bump(
    harness: _Harness,
) -> None:
    alice = harness.service.create_principal(
        actor=harness.actor,
        principal_id="human:alice",
        kind="human",
        display_name="Alice",
    )
    harness.service.issue_password(
        actor=harness.actor,
        record=_password_record(),
        expected_principal_revision=alice.revision,
    )
    current = _identity(harness, alice.principal_id)
    assert current is not None
    baseline = (current.revision, current.credential_epoch, current.authz_revision)

    with pytest.raises(ValueError, match="positive integer"):
        harness.service.issue_session(
            actor=harness.actor,
            record=_session_record(),
            expected_principal_revision=True,
        )
    assert _session(harness, "session:alice:1") is None

    first = harness.service.issue_session(
        actor=harness.actor,
        record=_session_record(),
        expected_principal_revision=current.revision,
    )
    second = harness.service.rotate_session(
        actor=harness.actor,
        replacement=_session_record(
            session_id="session:alice:2",
            token_digest="7" * 64,
        ),
        replaces_session_id=first.session_id,
        expected_principal_revision=current.revision,
        expected_replaced_revision=first.revision,
        reason="login_rotation",
    )
    revoked = harness.service.revoke_session(
        actor=harness.actor,
        session_id=second.session_id,
        expected_principal_revision=current.revision,
        expected_session_revision=second.revision,
        reason="logout",
    )

    assert _session(harness, first.session_id).revoked_at is not None
    assert revoked.revoked_at is not None
    assert [
        (_audit(harness, seq).action, _audit(harness, seq).subject.resource_id) for seq in (4, 5)
    ] == [
        ("identity.session_revoked", first.session_id),
        ("identity.session_rotated", second.session_id),
    ]
    after = _identity(harness, alice.principal_id)
    assert after is not None
    assert (after.revision, after.credential_epoch, after.authz_revision) == baseline


@pytest.mark.parametrize(
    ("variant", "message"),
    [
        ("noncanonical_login", "not already canonical"),
        ("missing_normalization", "normalization policy"),
        ("mismatched_hash", "hash encoding"),
        ("noninitial_version", "revision and version 1"),
    ],
)
def test_password_issue_requires_exact_retained_policies_and_canonical_encoding(
    harness: _Harness,
    variant: str,
    message: str,
) -> None:
    alice = harness.service.create_principal(
        actor=harness.actor,
        principal_id="human:alice",
        kind="human",
        display_name="Alice",
    )
    record = _password_record()
    if variant == "noncanonical_login":
        record = record.model_copy(update={"normalized_login_name": "Ａlice"})
    elif variant == "missing_normalization":
        record = record.model_copy(
            update={
                "normalization_policy_version": "normalization@missing",
                "normalization_policy_digest": "f" * 64,
            }
        )
    elif variant == "mismatched_hash":
        mismatched = _hash_policy().model_copy(update={"memory_kib": 16_384})
        record = record.model_copy(
            update={
                "password_hash": _password_runtime().hash_password(
                    SecretText("alice-password"),
                    mismatched,
                )
            }
        )
    else:
        record = record.model_copy(update={"credential_version": 2})

    with pytest.raises(IntegrityViolation, match=message):
        harness.service.issue_password(
            actor=harness.actor,
            record=record,
            expected_principal_revision=alice.revision,
        )

    assert _password(harness, record.credential_id) is None
    assert _identity(harness, alice.principal_id) == alice
    assert _audit(harness, 2) is None


@pytest.mark.parametrize(
    ("stale_authority", "error_type"),
    [
        ("session", SessionRevoked),
        ("password", CredentialDisabled),
    ],
)
def test_revoked_actor_credential_or_session_fails_inside_management_uow(
    harness: _Harness,
    stale_authority: str,
    error_type: type[Exception],
) -> None:
    with Session(harness.engine) as session, session.begin():
        auth = SqlAuthRepository(session, clock=harness.clock)
        if stale_authority == "session":
            auth.revoke_session(
                "session:admin",
                expected_revision=1,
                reason="revoked_before_command",
            )
        else:
            auth.disable_password("password:admin", expected_revision=1)

    with pytest.raises(error_type):
        harness.service.create_principal(
            actor=harness.actor,
            principal_id="human:stale-command",
            kind="human",
            display_name="Stale Command",
        )

    assert _identity(harness, "human:stale-command") is None
    assert _audit(harness, 1) is None


def test_expired_human_session_fails_inside_management_uow(harness: _Harness) -> None:
    harness.clock.current = datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc)

    with pytest.raises(SessionExpired):
        harness.service.create_principal(
            actor=harness.actor,
            principal_id="human:expired-session",
            kind="human",
            display_name="Expired Session",
        )

    assert _identity(harness, "human:expired-session") is None
    assert _audit(harness, 1) is None


def test_expired_service_api_key_fails_inside_management_uow(harness: _Harness) -> None:
    with Session(harness.engine) as session, session.begin():
        identities = SqlIdentityRepository(session, clock=harness.clock)
        created = identities.create(
            principal_id="service:identity-admin",
            kind="service",
            display_name="Identity Admin Service",
        )
        identities.grant(
            assignment_id="role:service:identity-admin",
            principal_id=created.principal_id,
            role="identity_admin",
            scope=None,
            granted_by=AuditActor(
                principal_id="human:admin",
                principal_kind="human",
            ),
            expected_principal_revision=created.revision,
        )
        principal = identities.project(created.principal_id)
        assert principal is not None
        SqlAuthRepository(session, clock=harness.clock).create_api_key(
            _api_key_record(
                api_key_id="api-key:identity-admin",
                principal_id=created.principal_id,
                key_digest="9" * 64,
            )
        )
    actor = ActorContext(
        principal=principal,
        authentication=AuthenticationContext(
            mechanism="api_key",
            credential_id="api-key:identity-admin",
        ),
        request_id="request:expired-api-key",
    )
    harness.clock.current = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)

    with pytest.raises(CredentialExpired):
        harness.service.create_principal(
            actor=actor,
            principal_id="human:expired-api-key",
            kind="human",
            display_name="Expired API Key",
        )

    assert _identity(harness, "human:expired-api-key") is None
    assert _audit(harness, 1) is None


def test_exact_current_role_policy_and_known_domain_scope_fail_closed(
    harness: _Harness,
) -> None:
    unavailable_policy = IdentityManagementService(
        unit_of_work=SqliteUnitOfWork(harness.engine, _capabilities(harness.clock)),
        bind_capabilities=_bind(harness.clock),
        current_role_policy_version=harness.policy.policy_version,
        current_role_policy_digest="f" * 64,
        password_encoding_verifier=_password_runtime(),
        clock=harness.clock,
        audit_chain_id=AUDIT_CHAIN_ID,
    )
    with pytest.raises(IntegrityViolation, match="digest"):
        unavailable_policy.create_principal(
            actor=harness.actor,
            principal_id="human:no-policy",
            kind="human",
            display_name="No Policy",
        )

    alice = harness.service.create_principal(
        actor=harness.actor,
        principal_id="human:alice",
        kind="human",
        display_name="Alice",
    )
    with pytest.raises(IntegrityViolation, match="unknown domains"):
        harness.service.grant_role(
            actor=harness.actor,
            assignment_id="role:alice:unknown",
            principal_id=alice.principal_id,
            role="qa",
            scope=DomainScope(domain_ids=("unknown",)),
            expected_principal_revision=alice.revision,
        )
    assert _assignment(harness, "role:alice:unknown") is None
    assert _audit(harness, 2) is None

    with pytest.raises(IntegrityViolation, match="deprecated domains"):
        harness.service.grant_role(
            actor=harness.actor,
            assignment_id="role:alice:deprecated",
            principal_id=alice.principal_id,
            role="qa",
            scope=DomainScope(domain_ids=("legacy-quest",)),
            expected_principal_revision=alice.revision,
        )
    assert _assignment(harness, "role:alice:deprecated") is None
    assert _audit(harness, 2) is None


def test_system_principal_cannot_receive_http_credentials(harness: _Harness) -> None:
    with Session(harness.engine) as session, session.begin():
        system = SqlIdentityRepository(session, clock=harness.clock).create(
            principal_id="system:worker",
            kind="system",
            display_name="System Worker",
        )

    with pytest.raises(Conflict, match="human principal"):
        harness.service.issue_password(
            actor=harness.actor,
            record=_password_record(
                credential_id="password:system",
                principal_id=system.principal_id,
                normalized_login_name="system",
            ),
            expected_principal_revision=system.revision,
        )
    with pytest.raises(Conflict, match="service principal"):
        harness.service.issue_api_key(
            actor=harness.actor,
            record=_api_key_record(
                api_key_id="api-key:system",
                principal_id=system.principal_id,
                key_digest="8" * 64,
            ),
            expected_principal_revision=system.revision,
        )
    assert _password(harness, "password:system") is None
    assert _api_key(harness, "api-key:system") is None
    assert _audit(harness, 1) is None


class _FailingAudit:
    def append(self, **_: object) -> None:
        raise RuntimeError("audit failure")


def test_audit_failure_rolls_back_credential_and_epoch_atomically(
    harness: _Harness,
) -> None:
    alice = harness.service.create_principal(
        actor=harness.actor,
        principal_id="human:alice",
        kind="human",
        display_name="Alice",
    )

    def bind_without_audit(transaction: Any) -> IdentityManagementCapabilities:
        return IdentityManagementCapabilities(
            identity=transaction.identity,
            auth=transaction.auth,
            policies=transaction.policies,
            audit=_FailingAudit(),
        )

    failing = IdentityManagementService(
        unit_of_work=SqliteUnitOfWork(harness.engine, _capabilities(harness.clock)),
        bind_capabilities=bind_without_audit,
        current_role_policy_version=harness.policy.policy_version,
        current_role_policy_digest=harness.policy.policy_digest,
        password_encoding_verifier=_password_runtime(),
        clock=harness.clock,
        audit_chain_id=AUDIT_CHAIN_ID,
    )
    with pytest.raises(RuntimeError, match="audit failure"):
        failing.issue_password(
            actor=harness.actor,
            record=_password_record(),
            expected_principal_revision=alice.revision,
        )

    assert _password(harness, "password:alice") is None
    assert _identity(harness, alice.principal_id) == alice
    assert _audit(harness, 2) is None
