from __future__ import annotations

import pytest

from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
    Permission,
    Principal,
    RoleAssignmentV1,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.rbac import AuthorizationDecision, authorize


def _registry(
    *domain_ids: str,
    version: str = "domains@1",
    deprecated: frozenset[str] = frozenset(),
) -> DomainRegistryV1:
    definitions = tuple(
        DomainDefinitionV1(
            domain_id=domain_id,
            display_name=domain_id.title(),
            tags=(),
            status="deprecated" if domain_id in deprecated else "active",
        )
        for domain_id in domain_ids
    )
    return DomainRegistryV1(
        registry_version=version,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(version, definitions),
    )


def _registry_ref(registry: DomainRegistryV1) -> DomainRegistryRefV1:
    return DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _permission(
    scope: DomainScope | str | None,
    *,
    action: str = "approval.decide",
    resource_kind: str = "approval",
) -> Permission:
    return Permission(
        action=action,
        resource_kind=resource_kind,
        domain_scope=scope,
    )


def _policy(
    registry: DomainRegistryV1,
    grants: dict[str, tuple[Permission, ...]],
    *,
    version: str = "roles@1",
) -> RolePolicy:
    registry_ref = _registry_ref(registry)
    effective_from = "2026-07-14T00:00:00Z"
    return RolePolicy(
        policy_version=version,
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            version,
            registry_ref,
            grants,
            effective_from,
        ),
    )


def _assignment(
    assignment_id: str,
    role: str,
    scope: DomainScope | str | None,
) -> RoleAssignmentV1:
    return RoleAssignmentV1(
        assignment_id=assignment_id,
        principal_id="human:reviewer",
        role=role,
        scope=scope,
        status="active",
        revision=1,
        granted_at="2026-07-14T00:00:00Z",
        granted_by=AuditActor(
            principal_id="human:admin",
            principal_kind="human",
        ),
    )


def _principal(
    *assignments: RoleAssignmentV1,
    status: str = "active",
) -> Principal:
    return Principal(
        id="human:reviewer",
        kind="human",
        display_name="Reviewer",
        status=status,
        revision=1,
        credential_epoch=0,
        authz_revision=1,
        roles=assignments,
    )


def _decide(
    principal: Principal,
    policy: RolePolicy,
    requested: Permission,
    registry: DomainRegistryV1,
) -> AuthorizationDecision:
    return authorize(
        principal=principal,
        role_policy=policy,
        requested_permission=requested,
        domain_registry=registry,
    )


def test_all_assignment_is_narrowed_by_policy_domain_scope() -> None:
    registry = _registry("narrative", "numeric")
    principal = _principal(_assignment("assignment:content", "content_designer", "all"))
    policy = _policy(
        registry,
        {"content_designer": (_permission(DomainScope(domain_ids=("narrative",))),)},
    )

    assert (
        _decide(
            principal,
            policy,
            _permission(DomainScope(domain_ids=("narrative",))),
            registry,
        )
        is AuthorizationDecision.ALLOW
    )
    assert (
        _decide(
            principal,
            policy,
            _permission(DomainScope(domain_ids=("numeric",))),
            registry,
        )
        is AuthorizationDecision.DENY
    )


def test_scoped_assignment_is_not_expanded_by_policy_all_grant() -> None:
    registry = _registry("narrative", "numeric")
    principal = _principal(
        _assignment(
            "assignment:content",
            "content_designer",
            DomainScope(domain_ids=("narrative",)),
        )
    )
    policy = _policy(registry, {"content_designer": (_permission("all"),)})

    assert (
        _decide(
            principal,
            policy,
            _permission(DomainScope(domain_ids=("narrative",))),
            registry,
        )
        is AuthorizationDecision.ALLOW
    )
    assert (
        _decide(
            principal,
            policy,
            _permission(DomainScope(domain_ids=("numeric",))),
            registry,
        )
        is AuthorizationDecision.DENY
    )


def test_domain_scope_intersection_and_multiple_roles_cover_all_requested_ids() -> None:
    registry = _registry("gacha", "narrative", "numeric")
    principal = _principal(
        _assignment(
            "assignment:content",
            "content_designer",
            DomainScope(domain_ids=("narrative", "numeric")),
        ),
        _assignment(
            "assignment:numeric",
            "numeric_designer",
            DomainScope(domain_ids=("numeric",)),
        ),
    )
    policy = _policy(
        registry,
        {
            "content_designer": (_permission(DomainScope(domain_ids=("gacha", "narrative"))),),
            "numeric_designer": (_permission(DomainScope(domain_ids=("numeric",))),),
        },
    )

    assert (
        _decide(
            principal,
            policy,
            _permission(DomainScope(domain_ids=("narrative", "numeric"))),
            registry,
        )
        is AuthorizationDecision.ALLOW
    )
    assert (
        _decide(
            principal,
            policy,
            _permission(DomainScope(domain_ids=("gacha", "narrative", "numeric"))),
            registry,
        )
        is AuthorizationDecision.DENY
    )


@pytest.mark.parametrize(
    ("assignment_scope", "grant_scope", "requested_scope", "expected"),
    [
        (None, None, None, AuthorizationDecision.ALLOW),
        (None, "all", None, AuthorizationDecision.DENY),
        ("all", None, None, AuthorizationDecision.DENY),
        (None, None, DomainScope(domain_ids=("numeric",)), AuthorizationDecision.DENY),
        (
            DomainScope(domain_ids=("numeric",)),
            DomainScope(domain_ids=("numeric",)),
            None,
            AuthorizationDecision.DENY,
        ),
    ],
)
def test_null_only_matches_null_non_domain_permissions(
    assignment_scope: DomainScope | str | None,
    grant_scope: DomainScope | str | None,
    requested_scope: DomainScope | str | None,
    expected: AuthorizationDecision,
) -> None:
    registry = _registry("numeric")
    principal = _principal(_assignment("assignment:qa", "qa", assignment_scope))
    policy = _policy(registry, {"qa": (_permission(grant_scope),)})

    assert _decide(principal, policy, _permission(requested_scope), registry) is expected


def test_action_and_resource_kind_require_exact_match() -> None:
    registry = _registry("numeric")
    principal = _principal(_assignment("assignment:numeric", "numeric_designer", "all"))
    policy = _policy(registry, {"numeric_designer": (_permission("all"),)})

    assert (
        _decide(
            principal,
            policy,
            _permission("all", action="approval.read"),
            registry,
        )
        is AuthorizationDecision.DENY
    )
    assert (
        _decide(
            principal,
            policy,
            _permission("all", resource_kind="patch"),
            registry,
        )
        is AuthorizationDecision.DENY
    )


def test_requested_all_requires_an_effective_explicit_all_grant() -> None:
    registry = _registry("narrative", "numeric")
    scoped_principal = _principal(
        _assignment(
            "assignment:content",
            "content_designer",
            DomainScope(domain_ids=("narrative",)),
        ),
        _assignment(
            "assignment:numeric",
            "numeric_designer",
            DomainScope(domain_ids=("numeric",)),
        ),
    )
    scoped_policy = _policy(
        registry,
        {
            "content_designer": (_permission("all"),),
            "numeric_designer": (_permission("all"),),
        },
    )
    all_principal = _principal(_assignment("assignment:tooling", "tooling", "all"))
    all_policy = _policy(registry, {"tooling": (_permission("all"),)})

    assert _decide(scoped_principal, scoped_policy, _permission("all"), registry) is (
        AuthorizationDecision.DENY
    )
    assert _decide(all_principal, all_policy, _permission("all"), registry) is (
        AuthorizationDecision.ALLOW
    )


def test_disabled_principal_and_empty_roles_fail_closed() -> None:
    registry = _registry("numeric")
    policy = _policy(registry, {"numeric_designer": (_permission("all"),)})
    assignment = _assignment("assignment:numeric", "numeric_designer", "all")

    assert _decide(
        _principal(assignment, status="disabled"), policy, _permission("all"), registry
    ) is (AuthorizationDecision.DENY)
    assert _decide(_principal(), policy, _permission("all"), registry) is (
        AuthorizationDecision.DENY
    )


def test_role_policy_must_reference_the_exact_registry() -> None:
    requested_registry = _registry("numeric", version="domains@1")
    policy_registry = _registry("numeric", version="domains@other")
    principal = _principal(_assignment("assignment:numeric", "numeric_designer", "all"))
    policy = _policy(policy_registry, {"numeric_designer": (_permission("all"),)})

    assert _decide(principal, policy, _permission("all"), requested_registry) is (
        AuthorizationDecision.DENY
    )


@pytest.mark.parametrize("invalid_source", ["requested", "assignment", "policy"])
def test_unknown_domain_in_any_authorization_input_fails_closed(invalid_source: str) -> None:
    registry = _registry("numeric")
    requested_scope = DomainScope(domain_ids=("numeric",))
    assignment_scope: DomainScope | str = "all"
    grant_scope: DomainScope | str = "all"
    if invalid_source == "requested":
        requested_scope = DomainScope(domain_ids=("unknown",))
    elif invalid_source == "assignment":
        assignment_scope = DomainScope(domain_ids=("unknown",))
    else:
        grant_scope = DomainScope(domain_ids=("unknown",))

    principal = _principal(
        _assignment("assignment:numeric", "numeric_designer", assignment_scope),
        _assignment("assignment:tooling", "tooling", "all"),
    )
    policy = _policy(
        registry,
        {
            "numeric_designer": (_permission(grant_scope),),
            "tooling": (_permission("all"),),
        },
    )

    assert _decide(principal, policy, _permission(requested_scope), registry) is (
        AuthorizationDecision.DENY
    )


def test_deprecated_domain_remains_authorizable_for_historical_resources() -> None:
    registry = _registry("legacy-narrative", deprecated=frozenset({"legacy-narrative"}))
    scope = DomainScope(domain_ids=("legacy-narrative",))
    principal = _principal(_assignment("assignment:content", "content_designer", scope))
    policy = _policy(registry, {"content_designer": (_permission(scope),)})

    assert _decide(principal, policy, _permission(scope), registry) is (AuthorizationDecision.ALLOW)
