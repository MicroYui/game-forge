"""The one retained RolePolicy shape every identity-bootstrapping test installs.

``BootstrapService`` fails closed unless the retained policy grants identity_admin
both identity management and global metric reads, defines tooling, and gives
platform_admin every delegated permission. Building that policy in one place keeps
a change to the bootstrap contract from silently missing a test fixture.
"""

from __future__ import annotations

from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRegistryV1,
    Permission,
    RolePolicy,
    compute_role_policy_digest,
)

PLATFORM_ADMIN_ONLY_GRANTS: tuple[Permission, ...] = (
    Permission(action="approval.decide", resource_kind="approval", domain_scope="all"),
    Permission(action="approval.self_decide", resource_kind="approval", domain_scope="all"),
    Permission(action="approval.route_override", resource_kind="approval", domain_scope="all"),
)


def bootstrap_role_policy(
    registry: DomainRegistryV1,
    *,
    policy_version: str = "roles@1",
    effective_from: str = "2026-07-14T00:00:00Z",
    identity_resource_kind: str = "identity",
    identity_admin_grants: tuple[Permission, ...] = (),
    tooling_grants: tuple[Permission, ...] = (),
) -> RolePolicy:
    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants: dict[str, tuple[Permission, ...]] = {
        "identity_admin": (
            Permission(
                action="identity.manage",
                resource_kind=identity_resource_kind,
                domain_scope=None,
            ),
            Permission(action="read", resource_kind="metric", domain_scope=None),
            *identity_admin_grants,
        ),
        "tooling": tooling_grants,
    }
    grants["platform_admin"] = (
        *grants["identity_admin"],
        *grants["tooling"],
        *PLATFORM_ADMIN_ONLY_GRANTS,
    )
    return RolePolicy(
        policy_version=policy_version,
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            policy_version,
            registry_ref,
            grants,
            effective_from,
        ),
    )
