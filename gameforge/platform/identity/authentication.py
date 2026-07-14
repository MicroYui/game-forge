"""Platform orchestration for API-key and trusted-internal identities."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from gameforge.contracts.auth import (
    ApiKeyAuthenticator,
    ApiKeyAuthRequestV1,
    AuthenticationResultV1,
)
from gameforge.contracts.errors import CredentialDisabled, IntegrityViolation
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    Principal,
    PrincipalKind,
)


class AuthenticationUnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...


class IdentityProjectionRepository(Protocol):
    def project(self, principal_id: str) -> Principal | None: ...


@dataclass(frozen=True, slots=True)
class ApiKeyAuthenticationCapabilities:
    authenticator: ApiKeyAuthenticator
    identities: IdentityProjectionRepository


@dataclass(frozen=True, slots=True)
class IdentityProjectionCapabilities:
    identities: IdentityProjectionRepository


ApiKeyCapabilityBinder = Callable[[Any], ApiKeyAuthenticationCapabilities]
IdentityProjectionBinder = Callable[[Any], IdentityProjectionCapabilities]


def _require_request_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("request_id must be a non-empty bounded string")
    return value


def _project_active_principal(
    identities: IdentityProjectionRepository,
    *,
    principal_id: str,
    expected_kind: PrincipalKind,
) -> Principal:
    principal = identities.project(principal_id)
    if principal is None:
        raise IntegrityViolation(
            "authenticated credential references a missing principal",
            principal_id=principal_id,
        )
    if principal.id != principal_id or principal.kind != expected_kind:
        raise IntegrityViolation(
            f"authenticated credential must bind an exact {expected_kind} principal",
            principal_id=principal_id,
        )
    if principal.status != "active":
        raise CredentialDisabled("principal is disabled", principal_id=principal_id)
    return principal


def _require_authentication_result(
    result: AuthenticationResultV1,
    *,
    expected_kind: PrincipalKind,
) -> AuthenticationResultV1:
    if type(result) is not AuthenticationResultV1:
        raise IntegrityViolation("authenticator returned a noncanonical result")
    if result.principal_kind != expected_kind:
        raise IntegrityViolation(
            f"authenticator result must identify a {expected_kind} principal",
            principal_id=result.principal_id,
        )
    return result


class ApiKeyAuthenticationService:
    """Authenticate one service API key and rebuild current authority state."""

    def __init__(
        self,
        *,
        unit_of_work: AuthenticationUnitOfWork,
        bind_capabilities: ApiKeyCapabilityBinder,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities

    def authenticate(
        self,
        request: ApiKeyAuthRequestV1,
        *,
        request_id: str,
    ) -> ActorContext:
        selected_request_id = _require_request_id(request_id)
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            result = _require_authentication_result(
                capabilities.authenticator.authenticate(request),
                expected_kind="service",
            )
            principal = _project_active_principal(
                capabilities.identities,
                principal_id=result.principal_id,
                expected_kind="service",
            )
            return ActorContext(
                principal=principal,
                authentication=AuthenticationContext(
                    mechanism="api_key",
                    credential_id=result.credential_id,
                ),
                request_id=selected_request_id,
            )


_TRUSTED_COMPOSITION_ROOT_TOKEN = object()


class TrustedSystemActorFactory:
    """Create system actors only through an explicit composition-root boundary."""

    def __init__(
        self,
        *,
        unit_of_work: AuthenticationUnitOfWork,
        bind_capabilities: IdentityProjectionBinder,
        _construction_token: object | None = None,
    ) -> None:
        if _construction_token is not _TRUSTED_COMPOSITION_ROOT_TOKEN:
            raise TypeError(
                "TrustedSystemActorFactory must be created by a trusted composition root"
            )
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities

    @classmethod
    def from_trusted_composition_root(
        cls,
        *,
        unit_of_work: AuthenticationUnitOfWork,
        bind_capabilities: IdentityProjectionBinder,
    ) -> TrustedSystemActorFactory:
        return cls(
            unit_of_work=unit_of_work,
            bind_capabilities=bind_capabilities,
            _construction_token=_TRUSTED_COMPOSITION_ROOT_TOKEN,
        )

    def actor_for(self, *, principal_id: str, request_id: str) -> ActorContext:
        selected_request_id = _require_request_id(request_id)
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            principal = _project_active_principal(
                capabilities.identities,
                principal_id=principal_id,
                expected_kind="system",
            )
            return ActorContext(
                principal=principal,
                authentication=AuthenticationContext(mechanism="trusted_internal"),
                request_id=selected_request_id,
            )


__all__ = [
    "ApiKeyAuthenticationCapabilities",
    "ApiKeyAuthenticationService",
    "ApiKeyCapabilityBinder",
    "AuthenticationUnitOfWork",
    "IdentityProjectionBinder",
    "IdentityProjectionCapabilities",
    "IdentityProjectionRepository",
    "TrustedSystemActorFactory",
]
