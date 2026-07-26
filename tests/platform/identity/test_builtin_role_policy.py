"""The default deployment policy must cover what the product actually authorizes."""

from __future__ import annotations

from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryV1,
    Permission,
    compute_domain_registry_digest,
)
from gameforge.platform.identity.role_policy import (
    PRODUCT_PERMISSIONS,
    builtin_role_policy,
)


def _registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(domain_id="game-content", display_name="Game Content", status="active"),
        DomainDefinitionV1(domain_id="builtin", display_name="Built-in", status="active"),
    )
    return DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )


def _policy(**kwargs):
    return builtin_role_policy(
        _registry(),
        policy_version="roles@1",
        effective_from="2026-07-26T00:00:00Z",
        **kwargs,
    )


def test_platform_admin_holds_every_product_permission() -> None:
    grants = set(_policy().grants["platform_admin"])

    assert set(PRODUCT_PERMISSIONS) <= grants


def test_product_permissions_cover_the_surfaces_a_planner_opens() -> None:
    surfaces = {
        # observability page: system metrics, traces, logs and cost are global reads
        Permission(action="read", resource_kind="metric", domain_scope=None),
        Permission(action="read", resource_kind="platform_status", domain_scope=None),
        Permission(action="read", resource_kind="trace", domain_scope="all"),
        Permission(action="read", resource_kind="log", domain_scope="all"),
        Permission(action="read", resource_kind="cost", domain_scope="all"),
        # quality page
        Permission(action="read", resource_kind="bench_report", domain_scope="all"),
        # project-first authoring
        Permission(action="create", resource_kind="project", domain_scope="all"),
        Permission(action="create", resource_kind="material", domain_scope="all"),
        Permission(action="create", resource_kind="extraction", domain_scope="all"),
    }

    assert surfaces <= set(PRODUCT_PERMISSIONS)


def test_platform_admin_also_covers_every_delegated_role() -> None:
    extra = {
        "content_designer": (
            Permission(action="propose", resource_kind="patch", domain_scope="all"),
        ),
        "qa": (Permission(action="run", resource_kind="playtest", domain_scope="all"),),
    }
    policy = _policy(extra_grants=extra)
    platform = set(policy.grants["platform_admin"])

    for role, permissions in extra.items():
        assert set(permissions) <= platform, role


def test_policy_satisfies_the_bootstrap_contract() -> None:
    policy = _policy()
    identity_admin = policy.grants["identity_admin"]

    assert Permission(
        action="identity.manage", resource_kind="identity", domain_scope=None
    ) in identity_admin
    assert Permission(action="read", resource_kind="metric", domain_scope=None) in identity_admin
    assert "tooling" in policy.grants


def test_tooling_never_decides_approvals_or_manages_identities() -> None:
    tooling = _policy().grants["tooling"]

    assert all(not permission.action.startswith("approval.") for permission in tooling)
    assert all(permission.resource_kind != "identity" for permission in tooling)
