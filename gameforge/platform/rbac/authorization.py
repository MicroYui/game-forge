"""Deterministic RBAC authorization without persistence or workflow concerns."""

from __future__ import annotations

from enum import Enum

from gameforge.contracts.identity import (
    DomainRegistryV1,
    DomainScope,
    DomainScopeValue,
    Permission,
    Principal,
    RolePolicy,
)


class AuthorizationDecision(str, Enum):
    """A closed authorization result suitable for platform guards."""

    ALLOW = "allow"
    DENY = "deny"


def _references_only_known_domains(
    scope: DomainScopeValue,
    known_domain_ids: frozenset[str],
) -> bool:
    return not isinstance(scope, DomainScope) or set(scope.domain_ids) <= known_domain_ids


def authorize(
    *,
    principal: Principal,
    role_policy: RolePolicy,
    requested_permission: Permission,
    domain_registry: DomainRegistryV1,
) -> AuthorizationDecision:
    """Authorize one exact permission against the current principal and policy.

    Assignment and policy scopes are intersected before matching the request. Domain
    requests use all-of coverage and may combine coverage from multiple assignments.
    Any inconsistent registry binding or unknown domain denies the whole decision.
    """

    if principal.status != "active":
        return AuthorizationDecision.DENY

    if (
        role_policy.domain_registry_ref.registry_version != domain_registry.registry_version
        or role_policy.domain_registry_ref.registry_digest != domain_registry.registry_digest
    ):
        return AuthorizationDecision.DENY

    known_domain_ids = frozenset(definition.domain_id for definition in domain_registry.definitions)
    scopes = [requested_permission.domain_scope]
    scopes.extend(assignment.scope for assignment in principal.roles)
    scopes.extend(
        permission.domain_scope for grants in role_policy.grants.values() for permission in grants
    )
    if any(not _references_only_known_domains(scope, known_domain_ids) for scope in scopes):
        return AuthorizationDecision.DENY

    covers_all_domains = False
    covers_non_domain = False
    covered_domain_ids: set[str] = set()

    for assignment in principal.roles:
        for grant in role_policy.grants.get(assignment.role, ()):
            if (
                grant.action != requested_permission.action
                or grant.resource_kind != requested_permission.resource_kind
            ):
                continue

            assignment_scope = assignment.scope
            grant_scope = grant.domain_scope
            if assignment_scope is None or grant_scope is None:
                if assignment_scope is None and grant_scope is None:
                    covers_non_domain = True
                continue

            if assignment_scope == "all" and grant_scope == "all":
                covers_all_domains = True
            elif assignment_scope == "all":
                covered_domain_ids.update(grant_scope.domain_ids)
            elif grant_scope == "all":
                covered_domain_ids.update(assignment_scope.domain_ids)
            else:
                covered_domain_ids.update(
                    set(assignment_scope.domain_ids) & set(grant_scope.domain_ids)
                )

    requested_scope = requested_permission.domain_scope
    if requested_scope is None:
        allowed = covers_non_domain
    elif requested_scope == "all":
        allowed = covers_all_domains
    else:
        allowed = covers_all_domains or set(requested_scope.domain_ids) <= covered_domain_ids

    return AuthorizationDecision.ALLOW if allowed else AuthorizationDecision.DENY


__all__ = ["AuthorizationDecision", "authorize"]
