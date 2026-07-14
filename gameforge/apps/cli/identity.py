"""Trusted composition for the local ``gameforge identity`` CLI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
import uuid

from sqlalchemy.orm import Session

from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import UtcClock
from gameforge.platform.identity.bootstrap import (
    BootstrapCapabilities,
    BootstrapIdFactory,
    BootstrapService,
)
from gameforge.runtime.auth.passwords import Argon2PasswordRuntime
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.engine import DATABASE_URL_ENV, DEFAULT_URL, get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


LOGIN_NORMALIZATION_POLICY_VERSION_ENV = "GAMEFORGE_IDENTITY_LOGIN_NORMALIZATION_POLICY_VERSION"
LOGIN_NORMALIZATION_POLICY_DIGEST_ENV = "GAMEFORGE_IDENTITY_LOGIN_NORMALIZATION_POLICY_DIGEST"
PASSWORD_HASH_POLICY_VERSION_ENV = "GAMEFORGE_IDENTITY_PASSWORD_HASH_POLICY_VERSION"
ROLE_POLICY_VERSION_ENV = "GAMEFORGE_IDENTITY_ROLE_POLICY_VERSION"
ROLE_POLICY_DIGEST_ENV = "GAMEFORGE_IDENTITY_ROLE_POLICY_DIGEST"
AUDIT_CHAIN_ID_ENV = "GAMEFORGE_IDENTITY_AUDIT_CHAIN_ID"
BOOTSTRAP_ACTOR_ID_ENV = "GAMEFORGE_IDENTITY_BOOTSTRAP_ACTOR_ID"


class IdentityBootstrapConfigurationError(ValueError):
    """The trusted CLI lacks an exact retained-policy binding."""


def _required(source: Mapping[str, str], name: str) -> str:
    value = source.get(name)
    if not isinstance(value, str) or not value:
        raise IdentityBootstrapConfigurationError(f"{name} is required")
    return value


def _lower_sha256(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise IdentityBootstrapConfigurationError(f"{name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class IdentityBootstrapConfig:
    database_url: str
    login_normalization_policy_version: str
    login_normalization_policy_digest: str
    password_hash_policy_version: str
    role_policy_version: str
    role_policy_digest: str
    audit_chain_id: str = "identity"
    bootstrap_actor_id: str = "system:identity-bootstrap"

    def __post_init__(self) -> None:
        text_values = {
            "database_url": self.database_url,
            "login_normalization_policy_version": self.login_normalization_policy_version,
            "password_hash_policy_version": self.password_hash_policy_version,
            "role_policy_version": self.role_policy_version,
            "audit_chain_id": self.audit_chain_id,
            "bootstrap_actor_id": self.bootstrap_actor_id,
        }
        if any(not isinstance(value, str) or not value for value in text_values.values()):
            raise IdentityBootstrapConfigurationError(
                "identity bootstrap configuration values must be non-empty"
            )
        _lower_sha256(
            self.login_normalization_policy_digest,
            name="login_normalization_policy_digest",
        )
        _lower_sha256(self.role_policy_digest, name="role_policy_digest")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> IdentityBootstrapConfig:
        source = os.environ if environment is None else environment
        return cls(
            database_url=source.get(DATABASE_URL_ENV, DEFAULT_URL),
            login_normalization_policy_version=_required(
                source,
                LOGIN_NORMALIZATION_POLICY_VERSION_ENV,
            ),
            login_normalization_policy_digest=_required(
                source,
                LOGIN_NORMALIZATION_POLICY_DIGEST_ENV,
            ),
            password_hash_policy_version=_required(
                source,
                PASSWORD_HASH_POLICY_VERSION_ENV,
            ),
            role_policy_version=_required(source, ROLE_POLICY_VERSION_ENV),
            role_policy_digest=_required(source, ROLE_POLICY_DIGEST_ENV),
            audit_chain_id=source.get(AUDIT_CHAIN_ID_ENV, "identity"),
            bootstrap_actor_id=source.get(
                BOOTSTRAP_ACTOR_ID_ENV,
                "system:identity-bootstrap",
            ),
        )


def _new_bootstrap_id(kind: str) -> str:
    return f"{kind}:{uuid.uuid4().hex}"


def _bind_bootstrap_capabilities(transaction: object) -> BootstrapCapabilities:
    return BootstrapCapabilities(
        identity=getattr(transaction, "identity"),
        auth=getattr(transaction, "auth"),
        policies=getattr(transaction, "policies"),
        audit=getattr(transaction, "audit"),
    )


def build_bootstrap_service(
    config: IdentityBootstrapConfig,
    *,
    clock: UtcClock | None = None,
    id_factory: BootstrapIdFactory | None = None,
) -> BootstrapService:
    """Bind the platform bootstrap service to one local SQLite UnitOfWork."""

    if type(config) is not IdentityBootstrapConfig:
        raise IdentityBootstrapConfigurationError(
            "identity bootstrap requires an exact configuration"
        )
    runtime_clock = SystemUtcClock() if clock is None else clock
    engine = get_engine(config.database_url)

    def capabilities(session: Session) -> TransactionCapabilities:
        return TransactionCapabilities(
            refs=None,
            audit=SqlAuditSink(session),
            approvals=None,
            lineage=None,
            object_bindings=None,
            runs=None,
            cost=None,
            identity=SqlIdentityRepository(session, clock=runtime_clock),
            auth=SqlAuthRepository(session, clock=runtime_clock),
            policies=SqlPolicySnapshotRepository(session, clock=runtime_clock),
        )

    return BootstrapService(
        unit_of_work=SqliteUnitOfWork(engine, capabilities),
        bind_capabilities=_bind_bootstrap_capabilities,
        clock=runtime_clock,
        bootstrap_actor=AuditActor(
            principal_id=config.bootstrap_actor_id,
            principal_kind="system",
        ),
        normalization_policy_version=config.login_normalization_policy_version,
        normalization_policy_digest=config.login_normalization_policy_digest,
        password_hash_policy_version=config.password_hash_policy_version,
        role_policy_version=config.role_policy_version,
        role_policy_digest=config.role_policy_digest,
        password_runtime=Argon2PasswordRuntime(),
        id_factory=_new_bootstrap_id if id_factory is None else id_factory,
        audit_chain_id=config.audit_chain_id,
    )


def build_bootstrap_service_from_environment(
    environment: Mapping[str, str] | None = None,
) -> BootstrapService:
    return build_bootstrap_service(IdentityBootstrapConfig.from_environment(environment))


__all__ = [
    "AUDIT_CHAIN_ID_ENV",
    "BOOTSTRAP_ACTOR_ID_ENV",
    "IdentityBootstrapConfig",
    "IdentityBootstrapConfigurationError",
    "LOGIN_NORMALIZATION_POLICY_DIGEST_ENV",
    "LOGIN_NORMALIZATION_POLICY_VERSION_ENV",
    "PASSWORD_HASH_POLICY_VERSION_ENV",
    "ROLE_POLICY_DIGEST_ENV",
    "ROLE_POLICY_VERSION_ENV",
    "build_bootstrap_service",
    "build_bootstrap_service_from_environment",
]
