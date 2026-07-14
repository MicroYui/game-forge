"""Actor-authorized identity, credential, and session management commands."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from gameforge.contracts.auth import (
    ApiKeyRecordV1,
    LoginNameNormalizationPolicyV1,
    PasswordCredentialRecordV1,
    PasswordHashPolicyV1,
    SessionRecordV1,
)
from gameforge.contracts.errors import (
    AuthFailed,
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
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
    DomainScopeValue,
    Permission,
    Principal,
    PrincipalKind,
    PrincipalRecordV1,
    Role,
    RoleAssignmentV1,
    RolePolicy,
)
from gameforge.contracts.lineage import AuditActor, AuditCorrelation, AuditSubject
from gameforge.contracts.storage import UtcClock
from gameforge.platform.rbac.authorization import AuthorizationDecision, authorize
from gameforge.runtime.auth.passwords import normalize_login_name


IDENTITY_MANAGE_PERMISSION = Permission(
    action="identity.manage",
    resource_kind="identity",
    domain_scope=None,
)


class IdentityManagementRepository(Protocol):
    def get(self, principal_id: str) -> PrincipalRecordV1 | None: ...

    def project(self, principal_id: str) -> Principal | None: ...

    def create(
        self,
        *,
        principal_id: str,
        kind: PrincipalKind,
        display_name: str,
    ) -> PrincipalRecordV1: ...

    def disable(
        self,
        principal_id: str,
        *,
        disabled_reason: str,
        expected_revision: int,
    ) -> PrincipalRecordV1: ...

    def grant(
        self,
        *,
        assignment_id: str,
        principal_id: str,
        role: Role,
        scope: DomainScopeValue,
        granted_by: AuditActor,
        expected_principal_revision: int,
    ) -> RoleAssignmentV1: ...

    def revoke(
        self,
        *,
        assignment_id: str,
        revoked_by: AuditActor,
        revoke_reason: str,
        expected_principal_revision: int,
        expected_assignment_revision: int,
    ) -> RoleAssignmentV1: ...

    def bump_credential_epoch(
        self,
        principal_id: str,
        *,
        expected_revision: int,
    ) -> PrincipalRecordV1: ...


class IdentityAuthRepository(Protocol):
    def create_password(
        self,
        record: PasswordCredentialRecordV1,
    ) -> PasswordCredentialRecordV1: ...

    def get_password(self, credential_id: str) -> PasswordCredentialRecordV1 | None: ...

    def compare_and_set_password(
        self,
        record: PasswordCredentialRecordV1,
        *,
        expected_revision: int,
    ) -> PasswordCredentialRecordV1: ...

    def disable_password(
        self,
        credential_id: str,
        *,
        expected_revision: int,
    ) -> PasswordCredentialRecordV1: ...

    def create_api_key(self, record: ApiKeyRecordV1) -> ApiKeyRecordV1: ...

    def get_api_key(self, api_key_id: str) -> ApiKeyRecordV1 | None: ...

    def revoke_api_key(
        self,
        api_key_id: str,
        *,
        expected_revision: int,
    ) -> ApiKeyRecordV1: ...

    def create_session(self, record: SessionRecordV1) -> SessionRecordV1: ...

    def get_session(self, session_id: str) -> SessionRecordV1 | None: ...

    def revoke_session(
        self,
        session_id: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> SessionRecordV1: ...


class IdentityPolicyRepository(Protocol):
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

    def get_domain_registry(
        self,
        ref: DomainRegistryRefV1,
    ) -> DomainRegistryV1 | None: ...


class PasswordEncodingPolicyVerifier(Protocol):
    def needs_rehash(
        self,
        encoded_hash: str,
        policy: PasswordHashPolicyV1,
    ) -> bool: ...


class IdentityAuditWriter(Protocol):
    def append(
        self,
        *,
        chain_id: str,
        actor: AuditActor,
        initiated_by: AuditActor | None,
        action: str,
        subject: AuditSubject,
        correlation: AuditCorrelation,
    ) -> object: ...


@dataclass(slots=True)
class IdentityManagementCapabilities:
    identity: IdentityManagementRepository | None
    auth: IdentityAuthRepository | None
    policies: IdentityPolicyRepository | None
    audit: IdentityAuditWriter | None


class IdentityManagementUnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...


IdentityManagementCapabilityBinder = Callable[
    [Any],
    IdentityManagementCapabilities,
]


def _required[T](value: T | None, name: str) -> T:
    if value is None:
        raise IntegrityViolation(f"identity management {name} capability is unavailable")
    return value


def _require_exact_model[T: BaseModel](value: object, model_type: type[T], *, label: str) -> T:
    if type(value) is not model_type:
        raise IntegrityViolation(f"{label} must be an exact canonical contract")
    try:
        canonical = model_type.model_validate(value.model_dump(mode="json"))
    except (AttributeError, TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"{label} must be an exact canonical contract") from exc
    if canonical != value:
        raise IntegrityViolation(f"{label} must be an exact canonical contract")
    return canonical


def _utc_now(clock: UtcClock) -> datetime:
    try:
        value = clock.now_utc()
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("identity management clock must return UTC") from exc
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("identity management clock must return UTC")
    return value.astimezone(timezone.utc)


def _parse_utc(value: str, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IntegrityViolation(f"stored {field_name} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise IntegrityViolation(f"stored {field_name} is not canonical UTC") from exc
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if parsed.utcoffset() != timedelta(0) or canonical != value:
        raise IntegrityViolation(f"stored {field_name} is not canonical UTC")
    return parsed.astimezone(timezone.utc)


def _require_record_revision(
    *,
    label: str,
    record_id: str,
    actual: int,
    expected: int,
) -> None:
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        raise ValueError("expected revision must be a positive integer")
    if actual != expected:
        raise Conflict(
            f"{label} revision did not match",
            record_id=record_id,
            expected_revision=expected,
            actual_revision=actual,
        )


class IdentityManagementService:
    """Apply every identity mutation through one authorized authoritative UoW."""

    def __init__(
        self,
        *,
        unit_of_work: IdentityManagementUnitOfWork,
        bind_capabilities: IdentityManagementCapabilityBinder,
        current_role_policy_version: str,
        current_role_policy_digest: str,
        password_encoding_verifier: PasswordEncodingPolicyVerifier,
        clock: UtcClock,
        audit_chain_id: str,
    ) -> None:
        if not current_role_policy_version:
            raise ValueError("current_role_policy_version must be non-empty")
        if len(current_role_policy_digest) != 64 or any(
            character not in "0123456789abcdef" for character in current_role_policy_digest
        ):
            raise ValueError("current_role_policy_digest must be lowercase SHA-256")
        if not audit_chain_id:
            raise ValueError("audit_chain_id must be non-empty")
        if not callable(getattr(password_encoding_verifier, "needs_rehash", None)):
            raise ValueError("password_encoding_verifier must validate encoded hash policy")
        _utc_now(clock)
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._role_policy_version = current_role_policy_version
        self._role_policy_digest = current_role_policy_digest
        self._password_encoding_verifier = password_encoding_verifier
        self._clock = clock
        self._audit_chain_id = audit_chain_id

    def create_principal(
        self,
        *,
        actor: ActorContext,
        principal_id: str,
        kind: PrincipalKind,
        display_name: str,
    ) -> PrincipalRecordV1:
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            if kind == "system" and current_actor.authentication.mechanism != "trusted_internal":
                raise Forbidden("system principal creation requires a trusted internal actor")
            identity = _required(capabilities.identity, "identity")
            created = identity.create(
                principal_id=principal_id,
                kind=kind,
                display_name=display_name,
            )
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.principal_created",
                resource_kind="principal",
                resource_id=created.principal_id,
            )
            return created

    def disable_principal(
        self,
        *,
        actor: ActorContext,
        principal_id: str,
        expected_revision: int,
        reason: str,
    ) -> PrincipalRecordV1:
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            disabled = identity.disable(
                principal_id,
                disabled_reason=reason,
                expected_revision=expected_revision,
            )
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.principal_disabled",
                resource_kind="principal",
                resource_id=disabled.principal_id,
            )
            return disabled

    def grant_role(
        self,
        *,
        actor: ActorContext,
        assignment_id: str,
        principal_id: str,
        role: Role,
        scope: DomainScopeValue,
        expected_principal_revision: int,
    ) -> RoleAssignmentV1:
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, registry = self._authorize(
                transaction,
                actor,
            )
            self._require_known_scope(scope, registry)
            identity = _required(capabilities.identity, "identity")
            assignment = identity.grant(
                assignment_id=assignment_id,
                principal_id=principal_id,
                role=role,
                scope=scope,
                granted_by=audit_actor,
                expected_principal_revision=expected_principal_revision,
            )
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.role_granted",
                resource_kind="role_assignment",
                resource_id=assignment.assignment_id,
            )
            return assignment

    def revoke_role(
        self,
        *,
        actor: ActorContext,
        assignment_id: str,
        expected_principal_revision: int,
        expected_assignment_revision: int,
        reason: str,
    ) -> RoleAssignmentV1:
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            assignment = identity.revoke(
                assignment_id=assignment_id,
                revoked_by=audit_actor,
                revoke_reason=reason,
                expected_principal_revision=expected_principal_revision,
                expected_assignment_revision=expected_assignment_revision,
            )
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.role_revoked",
                resource_kind="role_assignment",
                resource_id=assignment.assignment_id,
            )
            return assignment

    def issue_password(
        self,
        *,
        actor: ActorContext,
        record: PasswordCredentialRecordV1,
        expected_principal_revision: int,
    ) -> PasswordCredentialRecordV1:
        record = _require_exact_model(record, PasswordCredentialRecordV1, label="password record")
        if record.status != "active" or record.revision != 1 or record.credential_version != 1:
            raise IntegrityViolation(
                "new password credential must be active at revision and version 1"
            )
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            auth = _required(capabilities.auth, "auth")
            principal = self._require_target_principal(
                identity,
                record.principal_id,
                expected_revision=expected_principal_revision,
                expected_kind="human",
            )
            self._validate_password_policy_binding(capabilities, record)
            retained = auth.create_password(record)
            if retained != record:
                raise IntegrityViolation("password repository retained different issue content")
            self._bump_credential_epoch(identity, principal)
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.password_issued",
                resource_kind="password_credential",
                resource_id=retained.credential_id,
            )
            return retained

    def rotate_password(
        self,
        *,
        actor: ActorContext,
        replacement: PasswordCredentialRecordV1,
        expected_principal_revision: int,
        expected_credential_revision: int,
    ) -> PasswordCredentialRecordV1:
        replacement = _require_exact_model(
            replacement,
            PasswordCredentialRecordV1,
            label="password replacement",
        )
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            auth = _required(capabilities.auth, "auth")
            current = auth.get_password(replacement.credential_id)
            if current is None:
                raise Conflict(
                    "password credential does not exist",
                    credential_id=replacement.credential_id,
                )
            _require_record_revision(
                label="password credential",
                record_id=current.credential_id,
                actual=current.revision,
                expected=expected_credential_revision,
            )
            principal = self._require_target_principal(
                identity,
                current.principal_id,
                expected_revision=expected_principal_revision,
                expected_kind="human",
            )
            if (
                replacement.principal_id != current.principal_id
                or replacement.status != "active"
                or replacement.revision != current.revision + 1
                or replacement.credential_version != current.credential_version + 1
            ):
                raise IntegrityViolation(
                    "password rotation must preserve identity and advance both versions once"
                )
            self._validate_password_policy_binding(capabilities, replacement)
            retained = auth.compare_and_set_password(
                replacement,
                expected_revision=expected_credential_revision,
            )
            if retained != replacement:
                raise IntegrityViolation("password repository retained different rotation content")
            self._bump_credential_epoch(identity, principal)
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.password_rotated",
                resource_kind="password_credential",
                resource_id=retained.credential_id,
            )
            return retained

    def revoke_password(
        self,
        *,
        actor: ActorContext,
        credential_id: str,
        expected_principal_revision: int,
        expected_credential_revision: int,
    ) -> PasswordCredentialRecordV1:
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            auth = _required(capabilities.auth, "auth")
            current = auth.get_password(credential_id)
            if current is None:
                raise Conflict("password credential does not exist", credential_id=credential_id)
            _require_record_revision(
                label="password credential",
                record_id=current.credential_id,
                actual=current.revision,
                expected=expected_credential_revision,
            )
            principal = self._require_target_principal(
                identity,
                current.principal_id,
                expected_revision=expected_principal_revision,
                expected_kind="human",
            )
            retained = auth.disable_password(
                current.credential_id,
                expected_revision=expected_credential_revision,
            )
            self._bump_credential_epoch(identity, principal)
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.password_revoked",
                resource_kind="password_credential",
                resource_id=retained.credential_id,
            )
            return retained

    def issue_api_key(
        self,
        *,
        actor: ActorContext,
        record: ApiKeyRecordV1,
        expected_principal_revision: int,
    ) -> ApiKeyRecordV1:
        record = _require_exact_model(record, ApiKeyRecordV1, label="API-key record")
        if record.status != "active" or record.revision != 1 or record.credential_version != 1:
            raise IntegrityViolation("new API key must be active at revision and version 1")
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            auth = _required(capabilities.auth, "auth")
            principal = self._require_target_principal(
                identity,
                record.principal_id,
                expected_revision=expected_principal_revision,
                expected_kind="service",
            )
            retained = auth.create_api_key(record)
            if retained != record:
                raise IntegrityViolation("API-key repository retained different issue content")
            self._bump_credential_epoch(identity, principal)
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.api_key_issued",
                resource_kind="api_key",
                resource_id=retained.api_key_id,
            )
            return retained

    def rotate_api_key(
        self,
        *,
        actor: ActorContext,
        replacement: ApiKeyRecordV1,
        replaces_api_key_id: str,
        expected_principal_revision: int,
        expected_replaced_revision: int,
    ) -> ApiKeyRecordV1:
        replacement = _require_exact_model(
            replacement,
            ApiKeyRecordV1,
            label="API-key replacement",
        )
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            auth = _required(capabilities.auth, "auth")
            current = auth.get_api_key(replaces_api_key_id)
            if current is None:
                raise Conflict("API key does not exist", api_key_id=replaces_api_key_id)
            _require_record_revision(
                label="API key",
                record_id=current.api_key_id,
                actual=current.revision,
                expected=expected_replaced_revision,
            )
            principal = self._require_target_principal(
                identity,
                current.principal_id,
                expected_revision=expected_principal_revision,
                expected_kind="service",
            )
            if (
                replacement.api_key_id == current.api_key_id
                or replacement.principal_id != current.principal_id
                or replacement.status != "active"
                or replacement.revision != 1
                or replacement.credential_version != 1
            ):
                raise IntegrityViolation(
                    "API-key rotation requires a new active version-1 revision-1 key "
                    "for the same service"
                )
            retained = auth.create_api_key(replacement)
            if retained != replacement:
                raise IntegrityViolation("API-key repository retained different rotation content")
            revoked = auth.revoke_api_key(
                current.api_key_id,
                expected_revision=expected_replaced_revision,
            )
            if revoked.api_key_id != current.api_key_id or revoked.status != "revoked":
                raise IntegrityViolation("API-key repository returned an invalid replaced key")
            self._bump_credential_epoch(identity, principal)
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.api_key_revoked",
                resource_kind="api_key",
                resource_id=revoked.api_key_id,
            )
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.api_key_rotated",
                resource_kind="api_key",
                resource_id=retained.api_key_id,
            )
            return retained

    def revoke_api_key(
        self,
        *,
        actor: ActorContext,
        api_key_id: str,
        expected_principal_revision: int,
        expected_api_key_revision: int,
    ) -> ApiKeyRecordV1:
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            auth = _required(capabilities.auth, "auth")
            current = auth.get_api_key(api_key_id)
            if current is None:
                raise Conflict("API key does not exist", api_key_id=api_key_id)
            _require_record_revision(
                label="API key",
                record_id=current.api_key_id,
                actual=current.revision,
                expected=expected_api_key_revision,
            )
            principal = self._require_target_principal(
                identity,
                current.principal_id,
                expected_revision=expected_principal_revision,
                expected_kind="service",
            )
            retained = auth.revoke_api_key(
                current.api_key_id,
                expected_revision=expected_api_key_revision,
            )
            self._bump_credential_epoch(identity, principal)
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.api_key_revoked",
                resource_kind="api_key",
                resource_id=retained.api_key_id,
            )
            return retained

    def issue_session(
        self,
        *,
        actor: ActorContext,
        record: SessionRecordV1,
        expected_principal_revision: int,
    ) -> SessionRecordV1:
        record = _require_exact_model(record, SessionRecordV1, label="session record")
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            auth = _required(capabilities.auth, "auth")
            self._require_target_principal(
                identity,
                record.principal_id,
                expected_revision=expected_principal_revision,
                expected_kind="human",
            )
            self._require_session_source(auth, record)
            retained = auth.create_session(record)
            if retained != record:
                raise IntegrityViolation("session repository retained different issue content")
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.session_issued",
                resource_kind="session",
                resource_id=retained.session_id,
            )
            return retained

    def rotate_session(
        self,
        *,
        actor: ActorContext,
        replacement: SessionRecordV1,
        replaces_session_id: str,
        expected_principal_revision: int,
        expected_replaced_revision: int,
        reason: str,
    ) -> SessionRecordV1:
        replacement = _require_exact_model(
            replacement,
            SessionRecordV1,
            label="session replacement",
        )
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            auth = _required(capabilities.auth, "auth")
            current = auth.get_session(replaces_session_id)
            if current is None:
                raise Conflict("session does not exist", session_id=replaces_session_id)
            _require_record_revision(
                label="session",
                record_id=current.session_id,
                actual=current.revision,
                expected=expected_replaced_revision,
            )
            self._require_target_principal(
                identity,
                current.principal_id,
                expected_revision=expected_principal_revision,
                expected_kind="human",
            )
            if (
                replacement.session_id == current.session_id
                or replacement.principal_id != current.principal_id
                or replacement.revision != 1
                or replacement.revoked_at is not None
            ):
                raise IntegrityViolation(
                    "session rotation requires a new active revision-1 session for the same human"
                )
            self._require_session_source(auth, replacement)
            retained = auth.create_session(replacement)
            if retained != replacement:
                raise IntegrityViolation("session repository retained different rotation content")
            revoked = auth.revoke_session(
                current.session_id,
                expected_revision=expected_replaced_revision,
                reason=reason,
            )
            if revoked.session_id != current.session_id or revoked.revoked_at is None:
                raise IntegrityViolation("session repository returned an invalid replaced session")
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.session_revoked",
                resource_kind="session",
                resource_id=revoked.session_id,
            )
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.session_rotated",
                resource_kind="session",
                resource_id=retained.session_id,
            )
            return retained

    def revoke_session(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        expected_principal_revision: int,
        expected_session_revision: int,
        reason: str,
    ) -> SessionRecordV1:
        with self._unit_of_work.begin() as transaction:
            capabilities, current_actor, audit_actor, _ = self._authorize(
                transaction,
                actor,
            )
            identity = _required(capabilities.identity, "identity")
            auth = _required(capabilities.auth, "auth")
            current = auth.get_session(session_id)
            if current is None:
                raise Conflict("session does not exist", session_id=session_id)
            _require_record_revision(
                label="session",
                record_id=current.session_id,
                actual=current.revision,
                expected=expected_session_revision,
            )
            self._require_target_principal(
                identity,
                current.principal_id,
                expected_revision=expected_principal_revision,
                expected_kind="human",
            )
            retained = auth.revoke_session(
                current.session_id,
                expected_revision=expected_session_revision,
                reason=reason,
            )
            self._audit(
                capabilities,
                current_actor,
                audit_actor,
                action="identity.session_revoked",
                resource_kind="session",
                resource_id=retained.session_id,
            )
            return retained

    def _authorize(
        self,
        transaction: Any,
        actor: ActorContext,
    ) -> tuple[
        IdentityManagementCapabilities,
        ActorContext,
        AuditActor,
        DomainRegistryV1,
    ]:
        actor = _require_exact_model(actor, ActorContext, label="actor context")
        capabilities = self._bind_capabilities(transaction)
        if type(capabilities) is not IdentityManagementCapabilities:
            raise IntegrityViolation(
                "identity management capability binder returned an invalid bundle"
            )
        identity = _required(capabilities.identity, "identity")
        policies = _required(capabilities.policies, "policies")
        _required(capabilities.audit, "audit")

        projected = identity.project(actor.principal.id)
        if projected is None or projected.status != "active":
            raise CredentialDisabled("current actor is unavailable or disabled")
        if projected.kind != actor.principal.kind:
            raise IntegrityViolation("current actor kind differs from identity authority")
        self._require_current_authentication(
            actor=actor,
            projected=projected,
            auth=_required(capabilities.auth, "auth"),
        )

        role_policy = policies.get_role_policy(
            self._role_policy_version,
            self._role_policy_digest,
        )
        if type(role_policy) is not RolePolicy:
            raise IntegrityViolation("current exact role policy is unavailable")
        if (
            role_policy.policy_version != self._role_policy_version
            or role_policy.policy_digest != self._role_policy_digest
        ):
            raise IntegrityViolation("role policy authority returned a different exact policy")
        registry = policies.get_domain_registry(role_policy.domain_registry_ref)
        if type(registry) is not DomainRegistryV1:
            raise IntegrityViolation("current exact domain registry is unavailable")
        if (
            registry.registry_version != role_policy.domain_registry_ref.registry_version
            or registry.registry_digest != role_policy.domain_registry_ref.registry_digest
        ):
            raise IntegrityViolation(
                "domain registry authority returned a different exact registry"
            )

        if (
            authorize(
                principal=projected,
                role_policy=role_policy,
                requested_permission=IDENTITY_MANAGE_PERMISSION,
                domain_registry=registry,
            )
            is not AuthorizationDecision.ALLOW
        ):
            raise Forbidden("current actor lacks exact identity.manage permission")

        current_actor = ActorContext(
            principal=projected,
            authentication=actor.authentication,
            session_id=actor.session_id,
            request_id=actor.request_id,
        )
        audit_actor = AuditActor(
            principal_id=projected.id,
            principal_kind=projected.kind,
        )
        return capabilities, current_actor, audit_actor, registry

    @staticmethod
    def _require_known_scope(scope: DomainScopeValue, registry: DomainRegistryV1) -> None:
        if isinstance(scope, DomainScope):
            definitions = {definition.domain_id: definition for definition in registry.definitions}
            known = set(definitions)
            unknown = sorted(set(scope.domain_ids) - known)
            if unknown:
                raise IntegrityViolation(
                    "role assignment scope references unknown domains",
                    unknown_domain_ids=unknown,
                )
            deprecated = sorted(
                domain_id
                for domain_id in scope.domain_ids
                if definitions[domain_id].status == "deprecated"
            )
            if deprecated:
                raise IntegrityViolation(
                    "role assignment scope references deprecated domains",
                    deprecated_domain_ids=deprecated,
                )

    def _validate_password_policy_binding(
        self,
        capabilities: IdentityManagementCapabilities,
        record: PasswordCredentialRecordV1,
    ) -> None:
        policies = _required(capabilities.policies, "policies")
        normalization = policies.get_login_name_normalization_policy(
            policy_version=record.normalization_policy_version,
            policy_digest=record.normalization_policy_digest,
        )
        if type(normalization) is not LoginNameNormalizationPolicyV1:
            raise IntegrityViolation(
                "password credential exact normalization policy is unavailable"
            )
        if (
            normalization.policy_version != record.normalization_policy_version
            or normalization.policy_digest != record.normalization_policy_digest
        ):
            raise IntegrityViolation(
                "normalization policy authority returned a different exact policy"
            )
        try:
            normalized = normalize_login_name(record.normalized_login_name, normalization)
        except AuthFailed as exc:
            raise IntegrityViolation(
                "password credential login name violates its exact normalization policy"
            ) from exc
        if normalized != record.normalized_login_name:
            raise IntegrityViolation("password credential login name is not already canonical")

        hash_policy = policies.get_password_hash_policy(record.hash_policy_version)
        if type(hash_policy) is not PasswordHashPolicyV1:
            raise IntegrityViolation("password credential exact hash policy is unavailable")
        if hash_policy.policy_version != record.hash_policy_version:
            raise IntegrityViolation("hash policy authority returned a different exact policy")
        if self._password_encoding_verifier.needs_rehash(record.password_hash, hash_policy):
            raise IntegrityViolation(
                "password hash encoding differs from its exact retained policy"
            )

    def _require_current_authentication(
        self,
        *,
        actor: ActorContext,
        projected: Principal,
        auth: IdentityAuthRepository,
    ) -> None:
        if projected.kind == "human":
            session_id = actor.session_id
            if session_id is None:
                raise SessionRevoked("current actor session is unavailable")
            session = auth.get_session(session_id)
            if session is None or session.revoked_at is not None:
                raise SessionRevoked("current actor session is unavailable or revoked")
            if (
                session.principal_id != projected.id
                or session.source_credential_id != actor.authentication.credential_id
            ):
                raise IntegrityViolation(
                    "current actor session binding differs from identity authority"
                )
            now = _utc_now(self._clock)
            if now >= _parse_utc(
                session.absolute_expires_at,
                field_name="session absolute expiry",
            ) or now >= _parse_utc(
                session.idle_expires_at,
                field_name="session idle expiry",
            ):
                raise SessionExpired("current actor session is expired")
            credential = auth.get_password(session.source_credential_id)
            if credential is None:
                raise CredentialDisabled("current actor password credential is unavailable")
            if (
                credential.principal_id != projected.id
                or credential.status != "active"
                or credential.credential_version != session.credential_version
            ):
                raise CredentialDisabled(
                    "current actor password credential binding is not active and exact"
                )
            return

        if projected.kind == "service":
            credential_id = actor.authentication.credential_id
            if credential_id is None:
                raise CredentialDisabled("current actor API key is unavailable")
            credential = auth.get_api_key(credential_id)
            if credential is None or credential.principal_id != projected.id:
                raise CredentialDisabled("current actor API key is unavailable")
            if credential.status == "expired":
                raise CredentialExpired("current actor API key is expired")
            if credential.status != "active":
                raise CredentialDisabled("current actor API key is not active")
            if credential.expires_at is not None and _utc_now(self._clock) >= _parse_utc(
                credential.expires_at,
                field_name="API key expiry",
            ):
                raise CredentialExpired("current actor API key is expired")
            return

        if actor.authentication.mechanism != "trusted_internal":
            raise CredentialDisabled("system actor is not trusted internal")

    @staticmethod
    def _require_target_principal(
        identity: IdentityManagementRepository,
        principal_id: str,
        *,
        expected_revision: int,
        expected_kind: PrincipalKind,
    ) -> PrincipalRecordV1:
        principal = identity.get(principal_id)
        if principal is None:
            raise Conflict("principal does not exist", principal_id=principal_id)
        _require_record_revision(
            label="principal",
            record_id=principal.principal_id,
            actual=principal.revision,
            expected=expected_revision,
        )
        if principal.status != "active":
            raise Conflict("principal is not active", principal_id=principal.principal_id)
        if principal.kind != expected_kind:
            raise Conflict(
                f"credential requires an active {expected_kind} principal",
                principal_id=principal.principal_id,
                actual_kind=principal.kind,
            )
        return principal

    @staticmethod
    def _bump_credential_epoch(
        identity: IdentityManagementRepository,
        principal: PrincipalRecordV1,
    ) -> PrincipalRecordV1:
        retained = identity.bump_credential_epoch(
            principal.principal_id,
            expected_revision=principal.revision,
        )
        if (
            retained.principal_id != principal.principal_id
            or retained.kind != principal.kind
            or retained.status != principal.status
            or retained.revision != principal.revision + 1
            or retained.credential_epoch != principal.credential_epoch + 1
            or retained.authz_revision != principal.authz_revision
        ):
            raise IntegrityViolation(
                "identity repository returned an invalid credential epoch transition"
            )
        return retained

    @staticmethod
    def _require_session_source(
        auth: IdentityAuthRepository,
        session: SessionRecordV1,
    ) -> PasswordCredentialRecordV1:
        credential = auth.get_password(session.source_credential_id)
        if credential is None:
            raise Conflict(
                "session source password credential does not exist",
                credential_id=session.source_credential_id,
            )
        if (
            credential.principal_id != session.principal_id
            or credential.status != "active"
            or credential.credential_version != session.credential_version
        ):
            raise Conflict(
                "session source password credential binding is not active and exact",
                credential_id=credential.credential_id,
            )
        return credential

    def _audit(
        self,
        capabilities: IdentityManagementCapabilities,
        actor: ActorContext,
        audit_actor: AuditActor,
        *,
        action: str,
        resource_kind: str,
        resource_id: str,
    ) -> None:
        audit = _required(capabilities.audit, "audit")
        audit.append(
            chain_id=self._audit_chain_id,
            actor=audit_actor,
            initiated_by=None,
            action=action,
            subject=AuditSubject(
                resource_kind=resource_kind,
                resource_id=resource_id,
            ),
            correlation=AuditCorrelation(request_id=actor.request_id),
        )


__all__ = [
    "IDENTITY_MANAGE_PERMISSION",
    "IdentityAuditWriter",
    "IdentityAuthRepository",
    "IdentityManagementCapabilities",
    "IdentityManagementCapabilityBinder",
    "IdentityManagementRepository",
    "IdentityManagementService",
    "IdentityManagementUnitOfWork",
    "IdentityPolicyRepository",
    "PasswordEncodingPolicyVerifier",
]
