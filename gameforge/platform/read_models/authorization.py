"""Exact, deterministic authorization bindings for M4 read models."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import re
from typing import Generic, Protocol, TypeVar

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import DependencyUnavailable, Forbidden, IntegrityViolation
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
    DomainScopeValue,
    Permission,
    Principal,
    RolePolicy,
)
from gameforge.platform.rbac import AuthorizationDecision, authorize


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_BINDING_TEXT = 512
T = TypeVar("T")


class ReadPolicyRepository(Protocol):
    """Exact retained policy authority needed by read-model authorization."""

    def get_role_policy(
        self,
        policy_version: str,
        policy_digest: str,
    ) -> RolePolicy | None: ...

    def get_domain_registry(
        self,
        ref: DomainRegistryRefV1,
    ) -> DomainRegistryV1 | None: ...


@dataclass(frozen=True, slots=True)
class ReadAuthorizationBinding:
    """Stable identity binding plus all mutable authorization inputs."""

    principal_binding: str
    authz_fingerprint: str


@dataclass(frozen=True, slots=True)
class AuthorizedReadCollection(Generic[T]):
    """Complete authorized subset and the binding used to derive it."""

    items: tuple[T, ...]
    binding: ReadAuthorizationBinding


def _bounded_text(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_BINDING_TEXT:
        raise ValueError(f"{label} must be a non-empty bounded string")
    return value


def _query_hash(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("query_hash must be a lowercase SHA-256 digest")
    return value


def principal_identity_binding(*, principal_id: str, principal_kind: str) -> str:
    """Bind stable identity parts without coupling them to mutable RBAC state."""

    _bounded_text(principal_id, label="principal_id")
    if principal_kind not in {"human", "service", "system"}:
        raise ValueError("principal_kind is unsupported")
    return canonical_sha256(
        {
            "principal_binding_schema_version": "principal-binding@1",
            "principal_id": principal_id,
            "principal_kind": principal_kind,
        }
    )


def principal_binding(principal: Principal) -> str:
    """Bind a cursor to stable identity without coupling it to mutable RBAC state."""

    if type(principal) is not Principal:
        raise TypeError("principal must be an exact Principal")
    return principal_identity_binding(
        principal_id=principal.id,
        principal_kind=principal.kind,
    )


def authorization_fingerprint(
    *,
    principal: Principal,
    role_policy: RolePolicy,
    domain_registry: DomainRegistryV1,
    permission: Permission,
    query_hash: str,
) -> str:
    """Hash every authority input that can change a retained read decision."""

    if type(principal) is not Principal:
        raise TypeError("principal must be an exact Principal")
    if type(role_policy) is not RolePolicy:
        raise TypeError("role_policy must be an exact RolePolicy")
    if type(domain_registry) is not DomainRegistryV1:
        raise TypeError("domain_registry must be an exact DomainRegistryV1")
    if type(permission) is not Permission:
        raise TypeError("permission must be an exact Permission")
    exact_query_hash = _query_hash(query_hash)
    return canonical_sha256(
        {
            "authz_fingerprint_schema_version": "read-authz-fingerprint@1",
            "principal": {
                "principal_id": principal.id,
                "principal_kind": principal.kind,
                "status": principal.status,
                "revision": principal.revision,
                "authz_revision": principal.authz_revision,
                "active_assignments": [
                    assignment.model_dump(mode="json") for assignment in principal.roles
                ],
            },
            "role_policy": {
                "policy_version": role_policy.policy_version,
                "policy_digest": role_policy.policy_digest,
            },
            "domain_registry": {
                "registry_version": domain_registry.registry_version,
                "registry_digest": domain_registry.registry_digest,
            },
            "permission": permission.model_dump(mode="json"),
            "query_hash": exact_query_hash,
        }
    )


class ReadAuthorizationService:
    """Load current exact policy authority and authorize singular or list reads."""

    def __init__(
        self,
        *,
        policy_repository: ReadPolicyRepository,
        role_policy_version: str,
        role_policy_digest: str,
        missing_authority_component: str | None = None,
    ) -> None:
        self._policies = policy_repository
        self._role_policy_version = _bounded_text(
            role_policy_version,
            label="role_policy_version",
        )
        if not isinstance(role_policy_digest, str) or _SHA256.fullmatch(role_policy_digest) is None:
            raise ValueError("role_policy_digest must be a lowercase SHA-256 digest")
        self._role_policy_digest = role_policy_digest
        self._missing_authority_component = (
            None
            if missing_authority_component is None
            else _bounded_text(missing_authority_component, label="missing_authority_component")
        )

    def require_singular(
        self,
        *,
        principal: Principal,
        permission: Permission | None,
        query_hash: str,
    ) -> ReadAuthorizationBinding:
        """Require one loaded resource's exact permission and return cursor bindings."""

        return self.require_singular_derived(
            principal=principal,
            permission_for=lambda _registry: permission,
            query_hash=query_hash,
        )

    def require_singular_derived(
        self,
        *,
        principal: Principal,
        permission_for: Callable[[DomainRegistryV1], Permission | None],
        query_hash: str,
    ) -> ReadAuthorizationBinding:
        """Load authority once when a resource permission depends on its registry."""

        if not callable(permission_for):
            raise TypeError("permission_for must be callable")
        exact_query_hash = _query_hash(query_hash)
        exact_principal = self._principal(principal)
        role_policy, registry = self._load_exact_authority(exact_principal)
        exact_permission = self._proved_permission(permission_for(registry), registry)
        if (
            authorize(
                principal=exact_principal,
                role_policy=role_policy,
                requested_permission=exact_permission,
                domain_registry=registry,
            )
            is not AuthorizationDecision.ALLOW
        ):
            raise Forbidden(
                "current principal lacks exact read permission",
                action=exact_permission.action,
                resource_kind=exact_permission.resource_kind,
            )
        return self._binding(
            principal=exact_principal,
            role_policy=role_policy,
            registry=registry,
            permission=exact_permission,
            query_hash=exact_query_hash,
        )

    def filter_collection(
        self,
        *,
        principal: Principal,
        candidates: Iterable[T],
        collection_permission: Permission | None,
        permission_for: Callable[[T], Permission | None],
        query_hash: str,
    ) -> AuthorizedReadCollection[T]:
        """Filter a complete bounded candidate set with the same pure RBAC decision."""

        exact_query_hash = _query_hash(query_hash)
        if not callable(permission_for):
            raise TypeError("permission_for must be callable")
        exact_principal = self._principal(principal)
        role_policy, registry = self._load_exact_authority(exact_principal)
        collection = self._proved_permission(collection_permission, registry)
        selected: list[T] = []
        for candidate in candidates:
            permission = self._proved_permission(permission_for(candidate), registry)
            if (
                permission.action != collection.action
                or permission.resource_kind != collection.resource_kind
            ):
                raise IntegrityViolation(
                    "collection item permission action and resource kind differ from query binding"
                )
            if not _scope_within(permission.domain_scope, collection.domain_scope):
                raise IntegrityViolation("collection item domain is outside the bound query domain")
            if (
                authorize(
                    principal=exact_principal,
                    role_policy=role_policy,
                    requested_permission=permission,
                    domain_registry=registry,
                )
                is AuthorizationDecision.ALLOW
            ):
                selected.append(candidate)
        return AuthorizedReadCollection(
            items=tuple(selected),
            binding=self._binding(
                principal=exact_principal,
                role_policy=role_policy,
                registry=registry,
                permission=collection,
                query_hash=exact_query_hash,
            ),
        )

    def require_collection_continuation(
        self,
        *,
        principal: Principal,
        collection_permission: Permission | None,
        query_hash: str,
    ) -> ReadAuthorizationBinding:
        """Reauthorize a retained collection before its next page is disclosed."""

        exact_query_hash = _query_hash(query_hash)
        exact_principal = self._principal(principal)
        role_policy, registry = self._load_exact_authority(exact_principal)
        collection = self._proved_permission(collection_permission, registry)
        candidates = [collection]
        if collection.domain_scope == "all":
            candidates.extend(
                Permission(
                    action=collection.action,
                    resource_kind=collection.resource_kind,
                    domain_scope=DomainScope(domain_ids=(definition.domain_id,)),
                )
                for definition in registry.definitions
            )
        if not any(
            authorize(
                principal=exact_principal,
                role_policy=role_policy,
                requested_permission=permission,
                domain_registry=registry,
            )
            is AuthorizationDecision.ALLOW
            for permission in candidates
        ):
            raise Forbidden(
                "current principal no longer has collection read permission",
                action=collection.action,
                resource_kind=collection.resource_kind,
            )
        return self._binding(
            principal=exact_principal,
            role_policy=role_policy,
            registry=registry,
            permission=collection,
            query_hash=exact_query_hash,
        )

    @staticmethod
    def _principal(principal: Principal) -> Principal:
        if type(principal) is not Principal:
            raise TypeError("principal must be an exact Principal")
        if principal.status != "active":
            raise Forbidden("current principal is not active")
        return principal

    def _load_exact_authority(
        self,
        principal: Principal,
    ) -> tuple[RolePolicy, DomainRegistryV1]:
        policy = self._policies.get_role_policy(
            self._role_policy_version,
            self._role_policy_digest,
        )
        if policy is None and self._missing_authority_component is not None:
            raise DependencyUnavailable(
                "current exact role policy is unavailable",
                component=self._missing_authority_component,
            )
        if type(policy) is not RolePolicy:
            raise IntegrityViolation("current exact role policy is unavailable")
        if (
            policy.policy_version != self._role_policy_version
            or policy.policy_digest != self._role_policy_digest
        ):
            raise IntegrityViolation("role policy authority returned a different exact policy")
        registry = self._policies.get_domain_registry(policy.domain_registry_ref)
        if registry is None and self._missing_authority_component is not None:
            raise DependencyUnavailable(
                "current exact domain registry is unavailable",
                component=self._missing_authority_component,
            )
        if type(registry) is not DomainRegistryV1:
            raise IntegrityViolation("current exact domain registry is unavailable")
        if (
            registry.registry_version != policy.domain_registry_ref.registry_version
            or registry.registry_digest != policy.domain_registry_ref.registry_digest
        ):
            raise IntegrityViolation(
                "domain registry authority returned a different exact registry"
            )
        self._require_known_authority_domains(principal, policy, registry)
        return policy, registry

    @staticmethod
    def _proved_permission(
        permission: Permission | None,
        registry: DomainRegistryV1,
    ) -> Permission:
        if permission is None:
            raise IntegrityViolation("resource domain scope is not proved")
        if type(permission) is not Permission:
            raise TypeError("permission must be an exact Permission or None")
        _require_known_scope(permission.domain_scope, registry, label="resource domain")
        return permission

    @staticmethod
    def _require_known_authority_domains(
        principal: Principal,
        role_policy: RolePolicy,
        registry: DomainRegistryV1,
    ) -> None:
        for assignment in principal.roles:
            _require_known_scope(
                assignment.scope,
                registry,
                label="role assignment domain",
            )
        for grants in role_policy.grants.values():
            for grant in grants:
                _require_known_scope(
                    grant.domain_scope,
                    registry,
                    label="role policy domain",
                )

    @staticmethod
    def _binding(
        *,
        principal: Principal,
        role_policy: RolePolicy,
        registry: DomainRegistryV1,
        permission: Permission,
        query_hash: str,
    ) -> ReadAuthorizationBinding:
        return ReadAuthorizationBinding(
            principal_binding=principal_binding(principal),
            authz_fingerprint=authorization_fingerprint(
                principal=principal,
                role_policy=role_policy,
                domain_registry=registry,
                permission=permission,
                query_hash=query_hash,
            ),
        )


def _require_known_scope(
    scope: DomainScopeValue,
    registry: DomainRegistryV1,
    *,
    label: str,
) -> None:
    if not isinstance(scope, DomainScope):
        return
    known = {definition.domain_id for definition in registry.definitions}
    unknown = sorted(set(scope.domain_ids) - known)
    if unknown:
        raise IntegrityViolation(
            f"{label} references unknown domains",
            unknown_domain_ids=unknown,
        )


def _scope_within(item: DomainScopeValue, query: DomainScopeValue) -> bool:
    if query == "all":
        return item is not None
    if query is None:
        return item is None
    if item == "all" or item is None:
        return False
    return set(item.domain_ids) <= set(query.domain_ids)


__all__ = [
    "AuthorizedReadCollection",
    "ReadAuthorizationBinding",
    "ReadAuthorizationService",
    "ReadPolicyRepository",
    "authorization_fingerprint",
    "principal_binding",
    "principal_identity_binding",
]
