from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gameforge.contracts.auth import (
    LoginNameNormalizationPolicyV1,
    PasswordHashPolicyV1,
    SecretText,
    compute_login_name_normalization_policy_digest,
)
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    Permission,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.identity.bootstrap import (
    BootstrapAdminRequest,
    BootstrapCapabilities,
    BootstrapService,
)
from gameforge.runtime.auth.passwords import Argon2PasswordRuntime
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import (
    AuditRow,
    Base,
    PasswordCredentialRow,
    PrincipalRow,
    RoleAssignmentRow,
)
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


NOW = datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)
BOOTSTRAP_ACTOR = AuditActor(
    principal_id="system:identity-bootstrap",
    principal_kind="system",
)
IDS = {
    "principal": "human:bootstrap-admin",
    "password_credential": "password:bootstrap-admin:1",
    "identity_admin_assignment": "assignment:bootstrap-admin:identity-admin",
    "tooling_assignment": "assignment:bootstrap-admin:tooling",
    "request": "request:bootstrap-admin",
}


@dataclass(frozen=True)
class _Clock:
    def now_utc(self) -> datetime:
        return NOW


@pytest.fixture
def engine(tmp_path) -> Engine:
    database = get_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _normalization_policy() -> LoginNameNormalizationPolicyV1:
    payload = {
        "policy_version": "login-normalization@1",
        "unicode_normalization": "NFKC",
        "trim_unicode_whitespace": True,
        "case_mapping": "unicode_casefold",
        "reject_categories": ("control", "private_use", "surrogate"),
        "minimum_codepoints": 1,
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


def _domain_registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(
            domain_id="game-content",
            display_name="Game Content",
            tags=(),
            status="active",
        ),
    )
    return DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )


def _role_policy(
    registry: DomainRegistryV1,
    *,
    identity_resource_kind: str = "identity",
) -> RolePolicy:
    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants = {
        "identity_admin": (
            Permission(
                action="identity.manage",
                resource_kind=identity_resource_kind,
                domain_scope=None,
            ),
        ),
        "tooling": (
            Permission(
                action="run",
                resource_kind="tooling",
                domain_scope="all",
            ),
        ),
    }
    effective_from = "2026-07-14T00:00:00Z"
    return RolePolicy(
        policy_version="roles@1",
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            "roles@1",
            registry_ref,
            grants,
            effective_from,
        ),
    )


def _seed_policies(
    engine: Engine,
) -> tuple[
    LoginNameNormalizationPolicyV1,
    PasswordHashPolicyV1,
    RolePolicy,
]:
    normalization = _normalization_policy()
    password_hash = _hash_policy()
    registry = _domain_registry()
    roles = _role_policy(registry)
    with Session(engine) as session, session.begin():
        policies = SqlPolicySnapshotRepository(session, clock=_Clock())
        policies.put_login_name_normalization_policy(normalization)
        policies.put_password_hash_policy(password_hash)
        policies.put_domain_registry(registry)
        policies.put_role_policy(roles)
    return normalization, password_hash, roles


def _capability_factory(session: Session) -> TransactionCapabilities:
    clock = _Clock()
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


def _bind(transaction: Any) -> BootstrapCapabilities:
    return BootstrapCapabilities(
        identity=transaction.identity,
        auth=transaction.auth,
        policies=transaction.policies,
        audit=transaction.audit,
    )


def _service(
    engine: Engine,
    *,
    normalization: LoginNameNormalizationPolicyV1,
    password_hash: PasswordHashPolicyV1,
    roles: RolePolicy,
) -> BootstrapService:
    return BootstrapService(
        unit_of_work=SqliteUnitOfWork(engine, _capability_factory),
        bind_capabilities=_bind,
        clock=_Clock(),
        bootstrap_actor=BOOTSTRAP_ACTOR,
        normalization_policy_version=normalization.policy_version,
        normalization_policy_digest=normalization.policy_digest,
        password_hash_policy_version=password_hash.policy_version,
        role_policy_version=roles.policy_version,
        role_policy_digest=roles.policy_digest,
        password_runtime=Argon2PasswordRuntime(random_bytes=lambda size: b"s" * size),
        id_factory=IDS.__getitem__,
        audit_chain_id="identity",
    )


def _request() -> BootstrapAdminRequest:
    return BootstrapAdminRequest(
        display_name="Platform Admin",
        login_name="  ADMIN  ",
        password=SecretText("correct horse battery staple"),
    )


def test_bootstrap_creates_one_human_admin_password_and_exact_roles_atomically(
    engine: Engine,
) -> None:
    normalization, password_hash, roles = _seed_policies(engine)
    service = _service(
        engine,
        normalization=normalization,
        password_hash=password_hash,
        roles=roles,
    )

    result = service.bootstrap(_request())

    assert result.principal_id == IDS["principal"]
    assert result.password_credential_id == IDS["password_credential"]
    assert result.roles == ("identity_admin", "tooling")
    with Session(engine) as session:
        principal = SqlIdentityRepository(session, clock=_Clock()).project(result.principal_id)
        password = SqlAuthRepository(session, clock=_Clock()).get_password(
            result.password_credential_id
        )
        assert principal is not None
        assert principal.kind == "human"
        assert principal.revision == 4
        assert principal.credential_epoch == 1
        assert principal.authz_revision == 2
        assert tuple((item.role, item.scope) for item in principal.roles) == (
            ("identity_admin", None),
            ("tooling", "all"),
        )
        assert password is not None
        assert password.normalized_login_name == "admin"
        assert password.principal_id == principal.id
        assert password.hash_policy_version == password_hash.policy_version
        assert Argon2PasswordRuntime(random_bytes=lambda size: b"x" * size).verify_password(
            SecretText("correct horse battery staple"),
            password.password_hash,
            password_hash,
        )
        assert session.scalar(select(func.count()).select_from(PrincipalRow)) == 1
        assert session.scalar(select(func.count()).select_from(RoleAssignmentRow)) == 2
        assert session.scalar(select(func.count()).select_from(PasswordCredentialRow)) == 1
        assert session.scalar(select(func.count()).select_from(AuditRow)) == 1
        assert SqlAuditSink(session).verify_chain("identity") is True


def test_concurrent_bootstrap_attempts_have_exactly_one_winner(
    engine: Engine,
) -> None:
    normalization, password_hash, roles = _seed_policies(engine)
    service = _service(
        engine,
        normalization=normalization,
        password_hash=password_hash,
        roles=roles,
    )
    barrier = Barrier(2)

    def attempt() -> str:
        barrier.wait(timeout=5)
        try:
            service.bootstrap(_request())
        except Conflict:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _: attempt(), range(2)))

    assert sorted(outcomes) == ["conflict", "created"]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PrincipalRow)) == 1
        assert session.scalar(select(func.count()).select_from(RoleAssignmentRow)) == 2
        assert session.scalar(select(func.count()).select_from(PasswordCredentialRow)) == 1
        assert session.scalar(select(func.count()).select_from(AuditRow)) == 1


def test_bootstrap_fails_closed_when_exact_retained_role_policy_is_unavailable(
    engine: Engine,
) -> None:
    normalization, password_hash, roles = _seed_policies(engine)
    service = BootstrapService(
        unit_of_work=SqliteUnitOfWork(engine, _capability_factory),
        bind_capabilities=_bind,
        clock=_Clock(),
        bootstrap_actor=BOOTSTRAP_ACTOR,
        normalization_policy_version=normalization.policy_version,
        normalization_policy_digest=normalization.policy_digest,
        password_hash_policy_version=password_hash.policy_version,
        role_policy_version=roles.policy_version,
        role_policy_digest="0" * 64,
        password_runtime=Argon2PasswordRuntime(random_bytes=lambda size: b"s" * size),
        id_factory=IDS.__getitem__,
        audit_chain_id="identity",
    )

    with pytest.raises(IntegrityViolation, match="role policy"):
        service.bootstrap(_request())

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PrincipalRow)) == 0
        assert session.scalar(select(func.count()).select_from(RoleAssignmentRow)) == 0
        assert session.scalar(select(func.count()).select_from(PasswordCredentialRow)) == 0
        assert session.scalar(select(func.count()).select_from(AuditRow)) == 0


def test_bootstrap_rejects_role_policy_without_exact_identity_manage_permission(
    engine: Engine,
) -> None:
    normalization = _normalization_policy()
    password_hash = _hash_policy()
    registry = _domain_registry()
    roles = _role_policy(registry, identity_resource_kind="not-identity")
    with Session(engine) as session, session.begin():
        policies = SqlPolicySnapshotRepository(session, clock=_Clock())
        policies.put_login_name_normalization_policy(normalization)
        policies.put_password_hash_policy(password_hash)
        policies.put_domain_registry(registry)
        policies.put_role_policy(roles)
    service = _service(
        engine,
        normalization=normalization,
        password_hash=password_hash,
        roles=roles,
    )

    with pytest.raises(IntegrityViolation, match="identity.manage"):
        service.bootstrap(_request())

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PrincipalRow)) == 0
        assert session.scalar(select(func.count()).select_from(RoleAssignmentRow)) == 0
        assert session.scalar(select(func.count()).select_from(PasswordCredentialRow)) == 0
        assert session.scalar(select(func.count()).select_from(AuditRow)) == 0


class _LateFailingAuditSink:
    def __init__(self, session: Session) -> None:
        self._delegate = SqlAuditSink(session)

    def lock_head(self, chain_id: str):
        return self._delegate.lock_head(chain_id)

    def append(self, record):
        self._delegate.append(record)
        raise RuntimeError("late audit failure")

    def verify_chain(self, chain_id: str) -> bool:
        return self._delegate.verify_chain(chain_id)


def test_bootstrap_rolls_back_all_authority_when_late_audit_append_fails(
    engine: Engine,
) -> None:
    normalization, password_hash, roles = _seed_policies(engine)

    def capabilities(session: Session) -> TransactionCapabilities:
        clock = _Clock()
        return TransactionCapabilities(
            refs=None,
            audit=_LateFailingAuditSink(session),
            approvals=None,
            lineage=None,
            object_bindings=None,
            runs=None,
            cost=None,
            identity=SqlIdentityRepository(session, clock=clock),
            auth=SqlAuthRepository(session, clock=clock),
            policies=SqlPolicySnapshotRepository(session, clock=clock),
        )

    service = BootstrapService(
        unit_of_work=SqliteUnitOfWork(engine, capabilities),
        bind_capabilities=_bind,
        clock=_Clock(),
        bootstrap_actor=BOOTSTRAP_ACTOR,
        normalization_policy_version=normalization.policy_version,
        normalization_policy_digest=normalization.policy_digest,
        password_hash_policy_version=password_hash.policy_version,
        role_policy_version=roles.policy_version,
        role_policy_digest=roles.policy_digest,
        password_runtime=Argon2PasswordRuntime(random_bytes=lambda size: b"s" * size),
        id_factory=IDS.__getitem__,
        audit_chain_id="identity",
    )

    with pytest.raises(RuntimeError, match="late audit failure"):
        service.bootstrap(_request())

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PrincipalRow)) == 0
        assert session.scalar(select(func.count()).select_from(RoleAssignmentRow)) == 0
        assert session.scalar(select(func.count()).select_from(PasswordCredentialRow)) == 0
        assert session.scalar(select(func.count()).select_from(AuditRow)) == 0


def test_bootstrap_requires_a_trusted_system_actor(engine: Engine) -> None:
    normalization, password_hash, roles = _seed_policies(engine)

    with pytest.raises(ValueError, match="system actor"):
        BootstrapService(
            unit_of_work=SqliteUnitOfWork(engine, _capability_factory),
            bind_capabilities=_bind,
            clock=_Clock(),
            bootstrap_actor=AuditActor(
                principal_id="human:not-trusted",
                principal_kind="human",
            ),
            normalization_policy_version=normalization.policy_version,
            normalization_policy_digest=normalization.policy_digest,
            password_hash_policy_version=password_hash.policy_version,
            role_policy_version=roles.policy_version,
            role_policy_digest=roles.policy_digest,
            password_runtime=Argon2PasswordRuntime(random_bytes=lambda size: b"s" * size),
            id_factory=IDS.__getitem__,
            audit_chain_id="identity",
        )
