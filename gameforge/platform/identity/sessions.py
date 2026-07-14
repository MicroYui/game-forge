"""Transactional password-login and browser-session orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from gameforge.contracts.auth import (
    IdentityAuthenticator,
    PasswordAuthRequestV1,
    SecretText,
    SessionContextV1,
    SessionIssueRequestV1,
    SessionIssueV1,
    SessionRecordV1,
    SessionToken,
)
from gameforge.contracts.errors import (
    AuthError,
    AuthFailed,
    Conflict,
    Forbidden,
    IntegrityViolation,
)
from gameforge.contracts.identity import ActorContext, AuthenticationContext
from gameforge.contracts.lineage import AuditActor, AuditCorrelation, AuditSubject
from gameforge.platform.identity.authentication import (
    AuthenticationUnitOfWork,
    IdentityProjectionRepository,
    _project_active_principal,
    _require_authentication_result,
    _require_request_id,
)


class SessionIssueResolvePort(Protocol):
    def issue(self, request: SessionIssueRequestV1) -> SessionIssueV1: ...

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
    ) -> SessionContextV1: ...


class SessionRecordRepository(Protocol):
    def get_session(self, session_id: str) -> SessionRecordV1 | None: ...

    def revoke_session(
        self,
        session_id: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> SessionRecordV1: ...


class SessionAuditWriter(Protocol):
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


@dataclass(frozen=True, slots=True)
class SessionAuthenticationCapabilities:
    password_authenticator: IdentityAuthenticator
    session_runtime: SessionIssueResolvePort
    identities: IdentityProjectionRepository
    audit: SessionAuditWriter


@dataclass(frozen=True, slots=True)
class SessionManagerCapabilities:
    session_runtime: SessionIssueResolvePort
    session_records: SessionRecordRepository
    identities: IdentityProjectionRepository
    audit: SessionAuditWriter


SessionCapabilityBinder = Callable[[Any], SessionAuthenticationCapabilities]
SessionManagerCapabilityBinder = Callable[[Any], SessionManagerCapabilities]


def _require_session_issue(value: SessionIssueV1) -> SessionIssueV1:
    if type(value) is not SessionIssueV1:
        raise IntegrityViolation("session runtime returned a noncanonical issue")
    return value


def _require_session_context(value: SessionContextV1) -> SessionContextV1:
    if type(value) is not SessionContextV1:
        raise IntegrityViolation("session runtime returned a noncanonical context")
    return value


def _append_session_audit(
    audit: SessionAuditWriter,
    *,
    chain_id: str,
    actor: AuditActor,
    action: str,
    session_id: str,
    correlation: AuditCorrelation,
) -> None:
    audit.append(
        chain_id=chain_id,
        actor=actor,
        initiated_by=None,
        action=action,
        subject=AuditSubject(resource_kind="session", resource_id=session_id),
        correlation=correlation,
    )


class TransactionalSessionManager:
    """Exact local SessionManager facade over transaction-bound capabilities."""

    def __init__(
        self,
        *,
        unit_of_work: AuthenticationUnitOfWork,
        bind_capabilities: SessionManagerCapabilityBinder,
        audit_chain_id: str,
    ) -> None:
        if not isinstance(audit_chain_id, str) or not audit_chain_id:
            raise ValueError("audit_chain_id must be non-empty")
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._audit_chain_id = audit_chain_id

    def issue(self, request: SessionIssueRequestV1) -> SessionIssueV1:
        if type(request) is not SessionIssueRequestV1:
            raise IntegrityViolation("session issue request must be canonical")
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            principal = _project_active_principal(
                capabilities.identities,
                principal_id=request.principal_id,
                expected_kind="human",
            )
            issue = _require_session_issue(capabilities.session_runtime.issue(request))
            _append_session_audit(
                capabilities.audit,
                chain_id=self._audit_chain_id,
                actor=AuditActor(
                    principal_id=principal.id,
                    principal_kind=principal.kind,
                ),
                action="identity.session_issued",
                session_id=issue.session_id,
                correlation=AuditCorrelation(),
            )
            return issue

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
    ) -> SessionContextV1:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            session = _require_session_context(
                capabilities.session_runtime.resolve(
                    token,
                    csrf_token=csrf_token,
                    request_method=request_method,
                )
            )
            _project_active_principal(
                capabilities.identities,
                principal_id=session.principal_id,
                expected_kind="human",
            )
            return session

    def revoke(
        self,
        session_id: str,
        *,
        expected_revision: int,
        reason: str,
        actor: AuditActor,
    ) -> SessionRecordV1:
        if type(actor) is not AuditActor:
            raise IntegrityViolation("session revoke actor must be canonical")
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            projected_actor = _project_active_principal(
                capabilities.identities,
                principal_id=actor.principal_id,
                expected_kind=actor.principal_kind,
            )
            current = capabilities.session_records.get_session(session_id)
            if current is None:
                raise Conflict("session does not exist", session_id=session_id)
            if projected_actor.kind != "human" or current.principal_id != projected_actor.id:
                raise Forbidden("session revoke requires the owning human principal")
            retained = capabilities.session_records.revoke_session(
                session_id,
                expected_revision=expected_revision,
                reason=reason,
            )
            if (
                type(retained) is not SessionRecordV1
                or retained.session_id != session_id
                or retained.revision != expected_revision + 1
                or retained.revoked_at is None
                or retained.revoke_reason != reason
            ):
                raise IntegrityViolation("session repository returned an invalid revoke transition")
            _append_session_audit(
                capabilities.audit,
                chain_id=self._audit_chain_id,
                actor=AuditActor(
                    principal_id=projected_actor.id,
                    principal_kind=projected_actor.kind,
                ),
                action="identity.session_revoked",
                session_id=retained.session_id,
                correlation=AuditCorrelation(),
            )
            return retained


class SessionAuthenticationService:
    """Issue and resolve human sessions inside one authoritative UnitOfWork."""

    def __init__(
        self,
        *,
        unit_of_work: AuthenticationUnitOfWork,
        bind_capabilities: SessionCapabilityBinder,
        session_policy_version: str,
        audit_chain_id: str,
    ) -> None:
        if not isinstance(session_policy_version, str) or not session_policy_version:
            raise ValueError("session_policy_version must be non-empty")
        if not isinstance(audit_chain_id, str) or not audit_chain_id:
            raise ValueError("audit_chain_id must be non-empty")
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._session_policy_version = session_policy_version
        self._audit_chain_id = audit_chain_id

    def login(
        self,
        request: PasswordAuthRequestV1,
        *,
        request_id: str,
    ) -> SessionIssueV1:
        selected_request_id = _require_request_id(request_id)
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            try:
                authentication = _require_authentication_result(
                    capabilities.password_authenticator.verify_password(request),
                    expected_kind="human",
                )
                principal = _project_active_principal(
                    capabilities.identities,
                    principal_id=authentication.principal_id,
                    expected_kind="human",
                )
                issue = _require_session_issue(
                    capabilities.session_runtime.issue(
                        SessionIssueRequestV1(
                            principal_id=authentication.principal_id,
                            source_credential_id=authentication.credential_id,
                            credential_version=authentication.credential_version,
                            session_policy_version=self._session_policy_version,
                        )
                    )
                )
            except AuthError as exc:
                raise AuthFailed("password authentication failed") from exc

            _append_session_audit(
                capabilities.audit,
                chain_id=self._audit_chain_id,
                actor=AuditActor(
                    principal_id=principal.id,
                    principal_kind=principal.kind,
                ),
                action="identity.session_issued",
                session_id=issue.session_id,
                correlation=AuditCorrelation(request_id=selected_request_id),
            )
            return issue

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
        request_id: str,
    ) -> ActorContext:
        selected_request_id = _require_request_id(request_id)
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            session = _require_session_context(
                capabilities.session_runtime.resolve(
                    token,
                    csrf_token=csrf_token,
                    request_method=request_method,
                )
            )
            principal = _project_active_principal(
                capabilities.identities,
                principal_id=session.principal_id,
                expected_kind="human",
            )
            return ActorContext(
                principal=principal,
                authentication=AuthenticationContext(
                    mechanism="session",
                    credential_id=session.source_credential_id,
                ),
                session_id=session.session_id,
                request_id=selected_request_id,
            )


__all__ = [
    "SessionAuditWriter",
    "SessionAuthenticationCapabilities",
    "SessionAuthenticationService",
    "SessionCapabilityBinder",
    "SessionIssueResolvePort",
    "SessionManagerCapabilities",
    "SessionManagerCapabilityBinder",
    "SessionRecordRepository",
    "TransactionalSessionManager",
]
