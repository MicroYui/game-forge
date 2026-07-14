"""Trusted first-human bootstrap over one identity UnitOfWork.

This is the sole intentional pre-authentication provisioning path. It creates
no system credential and exposes no general identity-management bypass.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from gameforge.contracts.auth import (
    LoginNameNormalizationPolicyV1,
    PasswordCredentialRecordV1,
    PasswordHashPolicyV1,
    SecretText,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.identity import Principal, PrincipalKind, Role, RolePolicy
from gameforge.contracts.lineage import (
    AuditActor,
    AuditCorrelation,
    AuditSubject,
)
from gameforge.contracts.storage import UtcClock
from gameforge.platform.audit.gate import AuditGate, AuditGateStore
from gameforge.runtime.auth.passwords import Argon2PasswordRuntime, normalize_login_name


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=512)]
LoginName = Annotated[str, StringConstraints(min_length=1, max_length=256)]
PasswordValue = Annotated[SecretText, Field(min_length=1, max_length=4096)]
LowerHexSha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BootstrapIdKind = Literal[
    "principal",
    "password_credential",
    "identity_admin_assignment",
    "tooling_assignment",
    "request",
]
BootstrapIdFactory = Callable[[BootstrapIdKind], str]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class BootstrapAdminRequest(_FrozenModel):
    """Human-only first-admin request; principal kind is deliberately absent."""

    display_name: DisplayName
    login_name: LoginName
    password: PasswordValue


class BootstrapResult(_FrozenModel):
    principal_id: NonEmptyStr
    principal_revision: Annotated[int, Field(gt=0)]
    password_credential_id: NonEmptyStr
    roles: tuple[Role, ...]

    @field_validator("roles")
    @classmethod
    def _exact_bootstrap_roles(cls, value: tuple[Role, ...]) -> tuple[Role, ...]:
        canonical = tuple(sorted(set(value)))
        if canonical != ("identity_admin", "tooling"):
            raise ValueError("bootstrap result requires exactly identity_admin and tooling")
        return canonical


class BootstrapIdentityRepository(Protocol):
    def require_empty_for_bootstrap(self) -> None: ...

    def create(
        self,
        *,
        principal_id: str,
        kind: PrincipalKind,
        display_name: str,
    ) -> Any: ...

    def bump_credential_epoch(
        self,
        principal_id: str,
        *,
        expected_revision: int,
    ) -> Any: ...

    def grant(
        self,
        *,
        assignment_id: str,
        principal_id: str,
        role: Role,
        scope: str | None,
        granted_by: AuditActor,
        expected_principal_revision: int,
    ) -> Any: ...

    def project(self, principal_id: str) -> Principal | None: ...


class BootstrapAuthRepository(Protocol):
    def create_password(
        self,
        record: PasswordCredentialRecordV1,
    ) -> PasswordCredentialRecordV1: ...


class BootstrapPolicyRepository(Protocol):
    def get_login_name_normalization_policy(
        self,
        *,
        policy_version: str,
        policy_digest: str,
    ) -> LoginNameNormalizationPolicyV1 | None: ...

    def get_password_hash_policy(
        self,
        policy_version: str,
    ) -> PasswordHashPolicyV1 | None: ...

    def get_role_policy(
        self,
        policy_version: str,
        policy_digest: str,
    ) -> RolePolicy | None: ...


@dataclass(slots=True)
class BootstrapCapabilities:
    identity: BootstrapIdentityRepository | None
    auth: BootstrapAuthRepository | None
    policies: BootstrapPolicyRepository | None
    audit: AuditGateStore | None


class BootstrapUnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...


CapabilityBinder = Callable[[Any], BootstrapCapabilities]


def _required[T](value: T | None, label: str) -> T:
    if value is None:
        raise IntegrityViolation(f"{label} bootstrap capability is unavailable")
    return value


def _utc_text(clock: UtcClock) -> str:
    try:
        now = clock.now_utc()
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("identity bootstrap clock must return UTC") from exc
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("identity bootstrap clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _generated_id(factory: BootstrapIdFactory, kind: BootstrapIdKind) -> str:
    value = factory(kind)
    if not isinstance(value, str) or not value or len(value) > 512:
        raise IntegrityViolation(
            "identity bootstrap ID source returned an invalid ID", id_kind=kind
        )
    return value


def _require_bootstrap_role_policy(policy: RolePolicy) -> None:
    identity_grants = policy.grants.get("identity_admin")
    if identity_grants is None or not any(
        permission.action == "identity.manage"
        and permission.resource_kind == "identity"
        and permission.domain_scope is None
        for permission in identity_grants
    ):
        raise IntegrityViolation(
            "retained role policy does not grant identity.manage to identity_admin"
        )
    if "tooling" not in policy.grants:
        raise IntegrityViolation("retained role policy does not define tooling")


class BootstrapService:
    """Create the sole first human administrator and its password authority."""

    def __init__(
        self,
        *,
        unit_of_work: BootstrapUnitOfWork,
        bind_capabilities: CapabilityBinder,
        clock: UtcClock,
        bootstrap_actor: AuditActor,
        normalization_policy_version: str,
        normalization_policy_digest: LowerHexSha256,
        password_hash_policy_version: str,
        role_policy_version: str,
        role_policy_digest: LowerHexSha256,
        password_runtime: Argon2PasswordRuntime,
        id_factory: BootstrapIdFactory,
        audit_chain_id: str,
    ) -> None:
        if type(bootstrap_actor) is not AuditActor or bootstrap_actor.principal_kind != "system":
            raise ValueError("identity bootstrap requires a trusted system actor")
        text_values = {
            "normalization_policy_version": normalization_policy_version,
            "password_hash_policy_version": password_hash_policy_version,
            "role_policy_version": role_policy_version,
            "audit_chain_id": audit_chain_id,
        }
        if any(not isinstance(value, str) or not value for value in text_values.values()):
            raise ValueError("identity bootstrap versions and audit chain must be non-empty")
        for label, digest in (
            ("normalization_policy_digest", normalization_policy_digest),
            ("role_policy_digest", role_policy_digest),
        ):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._clock = clock
        self._bootstrap_actor = bootstrap_actor
        self._normalization_policy_version = normalization_policy_version
        self._normalization_policy_digest = normalization_policy_digest
        self._password_hash_policy_version = password_hash_policy_version
        self._role_policy_version = role_policy_version
        self._role_policy_digest = role_policy_digest
        self._password_runtime = password_runtime
        self._id_factory = id_factory
        self._audit_chain_id = audit_chain_id

    def bootstrap(self, request: BootstrapAdminRequest) -> BootstrapResult:
        if type(request) is not BootstrapAdminRequest:
            raise IntegrityViolation("identity bootstrap requires an exact request")
        principal_id = _generated_id(self._id_factory, "principal")
        credential_id = _generated_id(self._id_factory, "password_credential")
        identity_assignment_id = _generated_id(
            self._id_factory,
            "identity_admin_assignment",
        )
        tooling_assignment_id = _generated_id(self._id_factory, "tooling_assignment")
        request_id = _generated_id(self._id_factory, "request")
        if identity_assignment_id == tooling_assignment_id:
            raise IntegrityViolation("identity bootstrap assignment IDs must be distinct")

        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            identities = _required(capabilities.identity, "identity")
            auth = _required(capabilities.auth, "auth")
            policies = _required(capabilities.policies, "policy")
            audit_store = _required(capabilities.audit, "audit")

            identities.require_empty_for_bootstrap()
            normalization = policies.get_login_name_normalization_policy(
                policy_version=self._normalization_policy_version,
                policy_digest=self._normalization_policy_digest,
            )
            if normalization is None:
                raise IntegrityViolation("retained login normalization policy is unavailable")
            password_hash_policy = policies.get_password_hash_policy(
                self._password_hash_policy_version
            )
            if password_hash_policy is None:
                raise IntegrityViolation("retained password hash policy is unavailable")
            role_policy = policies.get_role_policy(
                self._role_policy_version,
                self._role_policy_digest,
            )
            if role_policy is None:
                raise IntegrityViolation("retained role policy is unavailable")
            _require_bootstrap_role_policy(role_policy)

            normalized_login = normalize_login_name(request.login_name, normalization)
            changed_at = _utc_text(self._clock)
            principal = identities.create(
                principal_id=principal_id,
                kind="human",
                display_name=request.display_name,
            )
            password = auth.create_password(
                PasswordCredentialRecordV1(
                    credential_id=credential_id,
                    principal_id=principal_id,
                    normalized_login_name=normalized_login,
                    normalization_policy_version=normalization.policy_version,
                    normalization_policy_digest=normalization.policy_digest,
                    password_hash=self._password_runtime.hash_password(
                        request.password,
                        password_hash_policy,
                    ),
                    hash_policy_version=password_hash_policy.policy_version,
                    credential_version=1,
                    status="active",
                    changed_at=changed_at,
                    revision=1,
                )
            )
            principal = identities.bump_credential_epoch(
                principal_id,
                expected_revision=principal.revision,
            )
            identities.grant(
                assignment_id=identity_assignment_id,
                principal_id=principal_id,
                role="identity_admin",
                scope=None,
                granted_by=self._bootstrap_actor,
                expected_principal_revision=principal.revision,
            )
            principal = identities.project(principal_id)
            if principal is None:
                raise IntegrityViolation("bootstrapped principal projection is unavailable")
            identities.grant(
                assignment_id=tooling_assignment_id,
                principal_id=principal_id,
                role="tooling",
                scope="all",
                granted_by=self._bootstrap_actor,
                expected_principal_revision=principal.revision,
            )
            principal = identities.project(principal_id)
            if principal is None:
                raise IntegrityViolation("bootstrapped principal projection is unavailable")
            if (
                principal.kind != "human"
                or principal.status != "active"
                or tuple(assignment.role for assignment in principal.roles)
                != ("identity_admin", "tooling")
            ):
                raise IntegrityViolation("bootstrapped principal projection is inconsistent")

            AuditGate(sink=audit_store, clock=self._clock).append(
                chain_id=self._audit_chain_id,
                actor=self._bootstrap_actor,
                initiated_by=None,
                action="identity.bootstrap",
                subject=AuditSubject(
                    resource_kind="principal",
                    resource_id=principal.id,
                ),
                correlation=AuditCorrelation(request_id=request_id),
            )
            return BootstrapResult(
                principal_id=principal.id,
                principal_revision=principal.revision,
                password_credential_id=password.credential_id,
                roles=tuple(assignment.role for assignment in principal.roles),
            )


__all__ = [
    "BootstrapAdminRequest",
    "BootstrapCapabilities",
    "BootstrapResult",
    "BootstrapService",
    "BootstrapUnitOfWork",
]
