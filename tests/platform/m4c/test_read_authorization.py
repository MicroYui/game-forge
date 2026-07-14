from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import Forbidden, IntegrityViolation
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
from gameforge.platform.read_models.authorization import ReadAuthorizationService


QUERY_HASH = "1" * 64


def _registry(*domain_ids: str, version: str = "domains@1") -> DomainRegistryV1:
    definitions = tuple(
        DomainDefinitionV1(
            domain_id=domain_id,
            display_name=domain_id.title(),
            status="active",
        )
        for domain_id in domain_ids
    )
    return DomainRegistryV1(
        registry_version=version,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(version, definitions),
    )


def _policy(
    registry: DomainRegistryV1,
    *,
    grants: Mapping[str, tuple[Permission, ...]],
    version: str = "roles@1",
) -> RolePolicy:
    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    effective_from = "2026-07-14T00:00:00Z"
    return RolePolicy(
        policy_version=version,
        domain_registry_ref=registry_ref,
        grants=dict(grants),
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            version,
            registry_ref,
            grants,
            effective_from,
        ),
    )


def _assignment(
    *,
    assignment_id: str = "assignment:reviewer",
    role: str = "qa",
    scope: DomainScope | str | None = "all",
    revision: int = 1,
) -> RoleAssignmentV1:
    return RoleAssignmentV1(
        assignment_id=assignment_id,
        principal_id="human:reviewer",
        role=role,
        scope=scope,
        status="active",
        revision=revision,
        granted_at="2026-07-14T00:00:00Z",
        granted_by=AuditActor(
            principal_id="human:admin",
            principal_kind="human",
        ),
    )


def _principal(
    *roles: RoleAssignmentV1,
    revision: int = 3,
    authz_revision: int = 5,
) -> Principal:
    return Principal(
        id="human:reviewer",
        kind="human",
        display_name="Reviewer",
        status="active",
        revision=revision,
        credential_epoch=2,
        authz_revision=authz_revision,
        roles=roles or (_assignment(),),
    )


def _permission(
    scope: DomainScope | str | None,
    *,
    action: str = "read",
    resource_kind: str = "finding",
) -> Permission:
    return Permission(
        action=action,
        resource_kind=resource_kind,
        domain_scope=scope,
    )


class _Policies:
    def __init__(
        self,
        policy: RolePolicy | None,
        registry: DomainRegistryV1 | None,
    ) -> None:
        self.policy = policy
        self.registry = registry
        self.role_lookups: list[tuple[str, str]] = []
        self.registry_lookups: list[DomainRegistryRefV1] = []

    def get_role_policy(self, policy_version: str, policy_digest: str) -> RolePolicy | None:
        self.role_lookups.append((policy_version, policy_digest))
        return self.policy

    def get_domain_registry(
        self,
        ref: DomainRegistryRefV1,
    ) -> DomainRegistryV1 | None:
        self.registry_lookups.append(ref)
        return self.registry


def _service(
    *,
    registry: DomainRegistryV1 | None = None,
    policy: RolePolicy | None = None,
) -> tuple[ReadAuthorizationService, _Policies, DomainRegistryV1, RolePolicy]:
    exact_registry = registry or _registry("gacha", "narrative", "numeric")
    exact_policy = policy or _policy(
        exact_registry,
        grants={"qa": (_permission("all"),)},
    )
    policies = _Policies(exact_policy, exact_registry)
    return (
        ReadAuthorizationService(
            policy_repository=policies,
            role_policy_version=exact_policy.policy_version,
            role_policy_digest=exact_policy.policy_digest,
        ),
        policies,
        exact_registry,
        exact_policy,
    )


def test_singular_require_loads_exact_authority_and_returns_stable_bindings() -> None:
    service, policies, _, policy = _service()
    principal = _principal()
    requested = _permission(DomainScope(domain_ids=("narrative",)))

    binding = service.require_singular(
        principal=principal,
        permission=requested,
        query_hash=QUERY_HASH,
    )

    assert policies.role_lookups == [(policy.policy_version, policy.policy_digest)]
    assert policies.registry_lookups == [policy.domain_registry_ref]
    assert binding.principal_binding == canonical_sha256(
        {
            "principal_binding_schema_version": "principal-binding@1",
            "principal_id": principal.id,
            "principal_kind": principal.kind,
        }
    )
    assert len(binding.authz_fingerprint) == 64

    same = service.require_singular(
        principal=principal,
        permission=requested,
        query_hash=QUERY_HASH,
    )
    assert same == binding

    changed_principal = principal.model_copy(
        update={"revision": principal.revision + 1},
    )
    changed = service.require_singular(
        principal=changed_principal,
        permission=requested,
        query_hash=QUERY_HASH,
    )
    assert changed.principal_binding == binding.principal_binding
    assert changed.authz_fingerprint != binding.authz_fingerprint

    changed_query = service.require_singular(
        principal=principal,
        permission=requested,
        query_hash="2" * 64,
    )
    assert changed_query.authz_fingerprint != binding.authz_fingerprint

    changed_scope = service.require_singular(
        principal=principal,
        permission=_permission(DomainScope(domain_ids=("numeric",))),
        query_hash=QUERY_HASH,
    )
    assert changed_scope.authz_fingerprint != binding.authz_fingerprint


def test_fingerprint_binds_authz_revision_and_full_active_assignments() -> None:
    service, _, _, _ = _service()
    requested = _permission(DomainScope(domain_ids=("narrative",)))
    principal = _principal()
    baseline = service.require_singular(
        principal=principal,
        permission=requested,
        query_hash=QUERY_HASH,
    )

    authz_changed = service.require_singular(
        principal=principal.model_copy(
            update={"authz_revision": principal.authz_revision + 1},
        ),
        permission=requested,
        query_hash=QUERY_HASH,
    )
    assignment_changed = service.require_singular(
        principal=_principal(_assignment(revision=2)),
        permission=requested,
        query_hash=QUERY_HASH,
    )

    assert authz_changed.principal_binding == baseline.principal_binding
    assert assignment_changed.principal_binding == baseline.principal_binding
    assert authz_changed.authz_fingerprint != baseline.authz_fingerprint
    assert assignment_changed.authz_fingerprint != baseline.authz_fingerprint


def test_singular_require_raises_forbidden_for_exact_denial() -> None:
    registry = _registry("narrative", "numeric")
    policy = _policy(
        registry,
        grants={"qa": (_permission(DomainScope(domain_ids=("narrative",))),)},
    )
    service, _, _, _ = _service(registry=registry, policy=policy)

    with pytest.raises(Forbidden, match="lacks exact read permission"):
        service.require_singular(
            principal=_principal(),
            permission=_permission(DomainScope(domain_ids=("numeric",))),
            query_hash=QUERY_HASH,
        )


def test_explicit_non_domain_permission_is_distinct_from_unproved_scope() -> None:
    registry = _registry("narrative")
    policy = _policy(
        registry,
        grants={"qa": (_permission(None, resource_kind="platform_status"),)},
    )
    service, _, _, _ = _service(registry=registry, policy=policy)

    binding = service.require_singular(
        principal=_principal(_assignment(scope=None)),
        permission=_permission(None, resource_kind="platform_status"),
        query_hash=QUERY_HASH,
    )

    assert len(binding.authz_fingerprint) == 64


@pytest.mark.parametrize(
    "permission",
    [
        None,
        _permission(DomainScope(domain_ids=("unknown",))),
    ],
)
def test_unknown_or_unproved_singular_domain_fails_closed(
    permission: Permission | None,
) -> None:
    service, _, _, _ = _service()

    with pytest.raises(IntegrityViolation, match="domain"):
        service.require_singular(
            principal=_principal(),
            permission=permission,
            query_hash=QUERY_HASH,
        )


def test_collection_filter_uses_same_exact_authorize_for_each_resource() -> None:
    registry = _registry("gacha", "narrative", "numeric")
    policy = _policy(
        registry,
        grants={"qa": (_permission(DomainScope(domain_ids=("narrative",))),)},
    )
    service, _, _, _ = _service(registry=registry, policy=policy)
    candidates = (
        _Resource("finding:narrative", "narrative"),
        _Resource("finding:gacha", "gacha"),
        _Resource("finding:numeric", "numeric"),
    )

    authorized = service.filter_collection(
        principal=_principal(),
        candidates=candidates,
        collection_permission=_permission("all"),
        permission_for=lambda item: _permission(DomainScope(domain_ids=(item.domain_id,))),
        query_hash=QUERY_HASH,
    )

    assert authorized.items == (candidates[0],)
    assert len(authorized.binding.authz_fingerprint) == 64


def test_collection_continuation_requires_current_access_to_at_least_one_domain() -> None:
    registry = _registry("gacha", "narrative")
    policy = _policy(
        registry,
        grants={"qa": (_permission(DomainScope(domain_ids=("narrative",))),)},
    )
    service, _, _, _ = _service(registry=registry, policy=policy)
    principal = _principal()

    binding = service.require_collection_continuation(
        principal=principal,
        collection_permission=_permission("all"),
        query_hash=QUERY_HASH,
    )

    assert len(binding.authz_fingerprint) == 64
    with pytest.raises(Forbidden, match="no longer has collection read permission"):
        service.require_collection_continuation(
            principal=principal.model_copy(update={"roles": (), "authz_revision": 6}),
            collection_permission=_permission("all"),
            query_hash=QUERY_HASH,
        )


def test_collection_filter_rejects_unproved_or_mismatched_item_authority() -> None:
    service, _, _, _ = _service()
    candidate = _Resource("finding:legacy", "legacy-unknown")

    with pytest.raises(IntegrityViolation, match="domain"):
        service.filter_collection(
            principal=_principal(),
            candidates=(candidate,),
            collection_permission=_permission("all"),
            permission_for=lambda _item: None,
            query_hash=QUERY_HASH,
        )

    with pytest.raises(IntegrityViolation, match="action and resource kind"):
        service.filter_collection(
            principal=_principal(),
            candidates=(candidate,),
            collection_permission=_permission("all"),
            permission_for=lambda _item: _permission(
                DomainScope(domain_ids=("narrative",)),
                action="approve",
            ),
            query_hash=QUERY_HASH,
        )


def test_missing_or_non_exact_policy_authority_fails_closed() -> None:
    service, policies, registry, policy = _service()
    policies.policy = None
    with pytest.raises(IntegrityViolation, match="role policy"):
        service.require_singular(
            principal=_principal(),
            permission=_permission(DomainScope(domain_ids=("narrative",))),
            query_hash=QUERY_HASH,
        )

    policies.policy = policy
    policies.registry = None
    with pytest.raises(IntegrityViolation, match="domain registry"):
        service.require_singular(
            principal=_principal(),
            permission=_permission(DomainScope(domain_ids=("narrative",))),
            query_hash=QUERY_HASH,
        )

    other_registry = _registry("narrative", version="domains@other")
    policies.registry = other_registry
    with pytest.raises(IntegrityViolation, match="different exact registry"):
        service.require_singular(
            principal=_principal(),
            permission=_permission(DomainScope(domain_ids=("narrative",))),
            query_hash=QUERY_HASH,
        )
    assert registry != other_registry


def test_invalid_query_hash_is_rejected_before_authority_lookup() -> None:
    service, policies, _, _ = _service()

    with pytest.raises(ValueError, match="query_hash"):
        service.require_singular(
            principal=_principal(),
            permission=_permission(DomainScope(domain_ids=("narrative",))),
            query_hash="not-a-digest",
        )

    assert policies.role_lookups == []


@dataclass(frozen=True)
class _Resource:
    resource_id: str
    domain_id: str
