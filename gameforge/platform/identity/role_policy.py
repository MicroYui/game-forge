"""The one default RolePolicy a GameForge deployment installs.

Every deployment used to hand-write its own grants, so a new product surface (or a
new bootstrap requirement) silently left administrators without the permission the
UI needs — the observability page reported "没有操作权限" for a platform admin.
``PRODUCT_PERMISSIONS`` is the exact set the product authorizes today; a
``platform_admin`` holds all of it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRegistryV1,
    Permission,
    RolePolicy,
    compute_role_policy_digest,
)

PRODUCT_PERMISSIONS: tuple[Permission, ...] = (
    Permission(action="apply", resource_kind="patch", domain_scope="all"),
    Permission(action="approval.decide", resource_kind="approval", domain_scope=None),
    Permission(action="approval.decide", resource_kind="approval", domain_scope="all"),
    Permission(action="approval.decide", resource_kind="patch", domain_scope="all"),
    Permission(action="approval.read", resource_kind="approval", domain_scope="all"),
    Permission(action="approval.route_override", resource_kind="approval", domain_scope="all"),
    Permission(action="approval.self_decide", resource_kind="approval", domain_scope="all"),
    Permission(action="archive", resource_kind="material", domain_scope="all"),
    Permission(action="create", resource_kind="extraction", domain_scope="all"),
    Permission(action="create", resource_kind="material", domain_scope="all"),
    Permission(action="create", resource_kind="project", domain_scope="all"),
    Permission(action="derive", resource_kind="task_suite", domain_scope="all"),
    Permission(action="drill", resource_kind="operations", domain_scope=None),
    Permission(action="identity.manage", resource_kind="identity", domain_scope=None),
    Permission(action="migrate", resource_kind="artifact", domain_scope="all"),
    Permission(action="propose", resource_kind="constraint_proposal", domain_scope="all"),
    Permission(action="propose", resource_kind="patch", domain_scope="all"),
    Permission(action="propose", resource_kind="rollback_request", domain_scope="all"),
    Permission(action="propose", resource_kind="spec", domain_scope="all"),
    Permission(action="publish", resource_kind="constraint_proposal", domain_scope="all"),
    Permission(action="read", resource_kind="approval", domain_scope="all"),
    Permission(action="read", resource_kind="artifact", domain_scope="all"),
    Permission(action="read", resource_kind="bench_report", domain_scope="all"),
    Permission(action="read", resource_kind="conflict_set", domain_scope="all"),
    Permission(action="read", resource_kind="constraint", domain_scope="all"),
    Permission(action="read", resource_kind="constraint_proposal", domain_scope="all"),
    Permission(action="read", resource_kind="cost", domain_scope="all"),
    Permission(action="read", resource_kind="execution_profile", domain_scope="all"),
    Permission(action="read", resource_kind="extraction", domain_scope="all"),
    Permission(action="read", resource_kind="finding", domain_scope="all"),
    Permission(action="read", resource_kind="log", domain_scope=None),
    Permission(action="read", resource_kind="log", domain_scope="all"),
    Permission(action="read", resource_kind="material", domain_scope="all"),
    Permission(action="read", resource_kind="metric", domain_scope=None),
    Permission(action="read", resource_kind="patch", domain_scope="all"),
    Permission(action="read", resource_kind="platform_status", domain_scope=None),
    Permission(action="read", resource_kind="playtest_result", domain_scope="all"),
    Permission(action="read", resource_kind="project", domain_scope="all"),
    Permission(action="read", resource_kind="ref", domain_scope="all"),
    Permission(action="read", resource_kind="review", domain_scope="all"),
    Permission(action="read", resource_kind="rollback_request", domain_scope="all"),
    Permission(action="read", resource_kind="run", domain_scope="all"),
    Permission(action="read", resource_kind="schema_registry", domain_scope=None),
    Permission(action="read", resource_kind="task_suite", domain_scope="all"),
    Permission(action="read", resource_kind="trace", domain_scope="all"),
    Permission(action="replay", resource_kind="run", domain_scope="all"),
    Permission(action="rollback", resource_kind="ref", domain_scope="all"),
    Permission(action="run", resource_kind="bench", domain_scope="all"),
    Permission(action="run", resource_kind="checker", domain_scope="all"),
    Permission(action="run", resource_kind="playtest", domain_scope="all"),
    Permission(action="run", resource_kind="review", domain_scope="all"),
    Permission(action="run", resource_kind="simulation", domain_scope="all"),
    Permission(action="update", resource_kind="project", domain_scope="all"),
    Permission(action="validate", resource_kind="constraint_proposal", domain_scope="all"),
    Permission(action="validate", resource_kind="patch", domain_scope="all"),
    Permission(action="validate", resource_kind="rollback_request", domain_scope="all"),
)

# The tool identity runs deterministic work and reads what a Run needs; it never
# decides approvals and never manages identities.
_TOOLING_ACTIONS = frozenset({"read", "run", "replay", "derive", "propose", "validate"})
_TOOLING_EXCLUDED_KINDS = frozenset({"identity", "operations", "metric", "platform_status"})

TOOLING_PERMISSIONS: tuple[Permission, ...] = tuple(
    permission
    for permission in PRODUCT_PERMISSIONS
    if permission.action in _TOOLING_ACTIONS
    and permission.resource_kind not in _TOOLING_EXCLUDED_KINDS
)

# Bootstrap requires identity management plus global metric reads so the first
# administrator can see whether the deployment is healthy.
IDENTITY_ADMIN_PERMISSIONS: tuple[Permission, ...] = (
    Permission(action="identity.manage", resource_kind="identity", domain_scope=None),
    Permission(action="read", resource_kind="metric", domain_scope=None),
)


# Platform-wide roles: their authority is the deployment, not one content domain.
# A role assignment that scopes these to a domain cannot satisfy the global reads
# the observability surface performs.
GLOBAL_ROLES = frozenset({"identity_admin", "platform_admin", "tooling"})


def role_assignment_scopes(role: str) -> tuple[object, ...]:
    """The scope shapes one role assignment set must carry.

    ``authorize`` matches an assignment scope against a grant scope of the same
    shape: a ``None`` assignment only satisfies non-domain grants and an ``"all"``
    assignment only satisfies domain grants. A platform-wide role therefore needs
    BOTH assignments, or the holder silently loses half of its authority.
    """

    return (None, "all") if role in GLOBAL_ROLES else ("all",)


def builtin_role_policy(
    registry: DomainRegistryV1,
    *,
    policy_version: str,
    effective_from: str,
    extra_grants: Mapping[str, Sequence[Permission]] | None = None,
) -> RolePolicy:
    """Build the default policy: platform_admin holds every product permission."""

    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants: dict[str, tuple[Permission, ...]] = {
        "identity_admin": IDENTITY_ADMIN_PERMISSIONS,
        "tooling": TOOLING_PERMISSIONS,
    }
    for role, permissions in (extra_grants or {}).items():
        grants[role] = tuple(permissions)
    delegated = tuple(
        permission for permissions in grants.values() for permission in permissions
    )
    seen: dict[tuple[str, str, str], Permission] = {}
    for permission in (*PRODUCT_PERMISSIONS, *delegated):
        scope = permission.domain_scope
        key = (
            permission.action,
            permission.resource_kind,
            "global" if scope is None else ("all" if scope == "all" else repr(scope)),
        )
        seen.setdefault(key, permission)
    grants["platform_admin"] = tuple(seen[key] for key in sorted(seen))
    return RolePolicy(
        policy_version=policy_version,
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            policy_version, registry_ref, grants, effective_from
        ),
    )


__all__ = [
    "GLOBAL_ROLES",
    "IDENTITY_ADMIN_PERMISSIONS",
    "role_assignment_scopes",
    "PRODUCT_PERMISSIONS",
    "TOOLING_PERMISSIONS",
    "builtin_role_policy",
]
