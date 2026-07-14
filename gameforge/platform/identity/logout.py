"""Atomic owner logout with exact idempotent replay."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from gameforge.contracts.auth import (
    SecretText,
    SessionContextV1,
    SessionRecordV1,
    SessionToken,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation, SessionRevoked
from gameforge.contracts.lineage import AuditActor, AuditCorrelation, AuditSubject
from gameforge.platform.identity.authentication import (
    AuthenticationUnitOfWork,
    IdentityProjectionRepository,
    _project_active_principal,
    _require_request_id,
)
from gameforge.platform.identity.sessions import SessionAuditWriter, SessionRecordRepository


_OPERATION = "auth.logout@1"


class LogoutSessionRuntime(Protocol):
    def inspect_for_logout(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText,
    ) -> SessionRecordV1: ...

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
    ) -> SessionContextV1: ...


class LogoutIdempotencyRepository(Protocol):
    def get_result(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        request_hash: str,
    ) -> dict[str, Any] | None: ...

    def put_result(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        request_hash: str,
        resource_kind: str,
        resource_id: str,
        response: Mapping[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class LogoutCapabilities:
    session_runtime: LogoutSessionRuntime
    session_records: SessionRecordRepository
    identities: IdentityProjectionRepository
    idempotency: LogoutIdempotencyRepository
    audit: SessionAuditWriter


@dataclass(frozen=True, slots=True)
class LogoutResult:
    session_id: str
    revoked_revision: int
    replayed: bool


LogoutCapabilityBinder = Callable[[Any], LogoutCapabilities]


class LogoutCommandService:
    def __init__(
        self,
        *,
        unit_of_work: AuthenticationUnitOfWork,
        bind_capabilities: LogoutCapabilityBinder,
        audit_chain_id: str,
    ) -> None:
        if not audit_chain_id or len(audit_chain_id) > 512:
            raise ValueError("audit_chain_id must be non-empty and bounded")
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._audit_chain_id = audit_chain_id

    def logout(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText,
        idempotency_key: str,
        request_id: str,
    ) -> LogoutResult:
        selected_request_id = _require_request_id(request_id)
        if not isinstance(token, SessionToken):
            raise TypeError("logout token must be a SessionToken")
        if not isinstance(csrf_token, SecretText):
            raise TypeError("logout CSRF token must be SecretText")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 512
        ):
            raise ValueError("logout idempotency_key must be non-empty and bounded")

        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            inspected = capabilities.session_runtime.inspect_for_logout(
                token,
                csrf_token=csrf_token,
            )
            if type(inspected) is not SessionRecordV1:
                raise IntegrityViolation("logout inspection returned a noncanonical session")
            request_hash = canonical_sha256(
                {
                    "request_schema_version": "logout-command@1",
                    "session_id": inspected.session_id,
                    "token_digest": inspected.token_digest,
                }
            )
            scope = "auth.logout.session:" + canonical_sha256(inspected.session_id)
            replay = capabilities.idempotency.get_result(
                scope=scope,
                operation=_OPERATION,
                key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return self._replay(replay, inspected)
            if inspected.revoked_at is not None:
                raise SessionRevoked("session is revoked")

            context = capabilities.session_runtime.resolve(
                token,
                csrf_token=csrf_token,
                request_method="POST",
            )
            if (
                type(context) is not SessionContextV1
                or context.session_id != inspected.session_id
                or context.principal_id != inspected.principal_id
                or context.source_credential_id != inspected.source_credential_id
                or context.credential_version != inspected.credential_version
            ):
                raise IntegrityViolation("logout resolution differs from inspected session")
            principal = _project_active_principal(
                capabilities.identities,
                principal_id=context.principal_id,
                expected_kind="human",
            )
            current = capabilities.session_records.get_session(context.session_id)
            if current is None:
                raise IntegrityViolation("logout session disappeared inside its transaction")
            if (
                current.principal_id != inspected.principal_id
                or current.source_credential_id != inspected.source_credential_id
                or current.credential_version != inspected.credential_version
                or current.token_digest != inspected.token_digest
                or current.revoked_at is not None
            ):
                raise IntegrityViolation("logout session authority changed unexpectedly")
            revoked = capabilities.session_records.revoke_session(
                current.session_id,
                expected_revision=current.revision,
                reason="logout",
            )
            if (
                type(revoked) is not SessionRecordV1
                or revoked.session_id != current.session_id
                or revoked.revision != current.revision + 1
                or revoked.revoked_at is None
                or revoked.revoke_reason != "logout"
            ):
                raise IntegrityViolation("logout revoke returned an invalid transition")
            capabilities.audit.append(
                chain_id=self._audit_chain_id,
                actor=AuditActor(
                    principal_id=principal.id,
                    principal_kind=principal.kind,
                ),
                initiated_by=None,
                action="identity.session_revoked",
                subject=AuditSubject(
                    resource_kind="session",
                    resource_id=revoked.session_id,
                ),
                correlation=AuditCorrelation(request_id=selected_request_id),
            )
            response = {
                "result_schema_version": "logout-result@1",
                "session_id": revoked.session_id,
                "revoked_revision": revoked.revision,
            }
            retained = capabilities.idempotency.put_result(
                scope=scope,
                operation=_OPERATION,
                key=idempotency_key,
                request_hash=request_hash,
                resource_kind="session",
                resource_id=revoked.session_id,
                response=response,
            )
            if retained != response:
                raise IntegrityViolation("logout idempotency repository retained another result")
            return LogoutResult(
                session_id=revoked.session_id,
                revoked_revision=revoked.revision,
                replayed=False,
            )

    @staticmethod
    def _replay(response: Mapping[str, Any], inspected: SessionRecordV1) -> LogoutResult:
        if set(response) != {
            "result_schema_version",
            "session_id",
            "revoked_revision",
        }:
            raise IntegrityViolation("logout idempotency response is malformed")
        revision = response.get("revoked_revision")
        if (
            response.get("result_schema_version") != "logout-result@1"
            or response.get("session_id") != inspected.session_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 2
            or inspected.revoked_at is None
            or inspected.revision != revision
        ):
            raise IntegrityViolation("logout idempotency response differs from session authority")
        return LogoutResult(
            session_id=inspected.session_id,
            revoked_revision=revision,
            replayed=True,
        )


__all__ = [
    "LogoutCapabilities",
    "LogoutCapabilityBinder",
    "LogoutCommandService",
    "LogoutIdempotencyRepository",
    "LogoutResult",
    "LogoutSessionRuntime",
]
