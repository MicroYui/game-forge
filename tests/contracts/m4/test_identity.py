from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRoutePolicyRefV1,
    DomainRouteRule,
    DomainScope,
    Permission,
    Principal,
    PrincipalRecordV1,
    RoleAssignmentV1,
    RolePolicy,
    compute_domain_registry_digest,
    compute_domain_route_policy_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.lineage import AuditActor


def _actor(principal_id: str = "human:alice", kind: str = "human") -> AuditActor:
    return AuditActor(principal_id=principal_id, principal_kind=kind)


def _domain_registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(
            domain_id="narrative",
            display_name="Narrative",
            parent_domain_id="game",
            tags=("story",),
            status="active",
        ),
        DomainDefinitionV1(
            domain_id="game",
            display_name="Game",
            tags=("root",),
            status="active",
        ),
    )
    digest = compute_domain_registry_digest("domains@1", definitions)
    return DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=digest,
    )


def _domain_ref() -> DomainRegistryRefV1:
    registry = _domain_registry()
    return DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def test_domain_scope_is_nonempty_canonical_and_frozen() -> None:
    scope = DomainScope(domain_ids=("narrative", "game", "narrative"))

    assert scope.domain_ids == ("game", "narrative")
    with pytest.raises(ValidationError):
        DomainScope(domain_ids=())
    with pytest.raises(ValidationError):
        scope.domain_ids = ("game",)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DomainScope(domain_ids=("game",), unexpected=True)  # type: ignore[call-arg]


def test_domain_registry_sorts_definitions_and_validates_digest_and_graph() -> None:
    registry = _domain_registry()

    assert [item.domain_id for item in registry.definitions] == ["game", "narrative"]
    assert DomainRegistryV1.model_validate(registry.model_dump(mode="json")) == registry
    with pytest.raises(ValidationError, match="registry_digest"):
        DomainRegistryV1(
            registry_version=registry.registry_version,
            definitions=registry.definitions,
            registry_digest="0" * 64,
        )

    orphan = DomainDefinitionV1(
        domain_id="orphan",
        display_name="Orphan",
        parent_domain_id="missing",
        tags=(),
        status="active",
    )
    with pytest.raises(ValidationError, match="parent_domain_id"):
        DomainRegistryV1(
            registry_version="domains@bad",
            definitions=(orphan,),
            registry_digest=compute_domain_registry_digest("domains@bad", (orphan,)),
        )

    a = DomainDefinitionV1(
        domain_id="a", display_name="A", parent_domain_id="b", tags=(), status="active"
    )
    b = DomainDefinitionV1(
        domain_id="b", display_name="B", parent_domain_id="a", tags=(), status="active"
    )
    with pytest.raises(ValidationError, match="cycle"):
        DomainRegistryV1(
            registry_version="domains@cycle",
            definitions=(a, b),
            registry_digest=compute_domain_registry_digest("domains@cycle", (a, b)),
        )


def test_domain_registry_rejects_duplicate_ids() -> None:
    first = DomainDefinitionV1(domain_id="game", display_name="Game", tags=(), status="active")
    second = DomainDefinitionV1(
        domain_id="game", display_name="Duplicate", tags=(), status="deprecated"
    )
    with pytest.raises(ValidationError, match="duplicate domain_id"):
        DomainRegistryV1(
            registry_version="domains@dup",
            definitions=(first, second),
            registry_digest=compute_domain_registry_digest("domains@dup", (first, second)),
        )


def test_role_policy_canonicalizes_grants_and_detects_tamper() -> None:
    domain_ref = _domain_ref()
    narrative = Permission(
        action="patch.approve",
        resource_kind="approval",
        domain_scope=DomainScope(domain_ids=("narrative",)),
    )
    game = Permission(
        action="patch.approve",
        resource_kind="approval",
        domain_scope=DomainScope(domain_ids=("game",)),
    )
    grants = {"content_designer": (narrative, game, narrative)}
    digest = compute_role_policy_digest("roles@1", domain_ref, grants, "2026-07-13T00:00:00Z")
    policy = RolePolicy(
        policy_version="roles@1",
        domain_registry_ref=domain_ref,
        grants=grants,
        effective_from="2026-07-13T00:00:00Z",
        policy_digest=digest,
    )

    assert policy.grants["content_designer"] == (game, narrative)
    assert RolePolicy.model_validate(policy.model_dump(mode="json")) == policy
    with pytest.raises(ValidationError, match="policy_digest"):
        RolePolicy(
            policy_version=policy.policy_version,
            domain_registry_ref=policy.domain_registry_ref,
            grants=policy.grants,
            effective_from=policy.effective_from,
            policy_digest="f" * 64,
        )


def test_domain_route_policy_uses_subject_kind_and_canonical_rule_identity() -> None:
    domain_ref = _domain_ref()
    rules = (
        DomainRouteRule(
            rule_id="narrative",
            domain_selector=DomainScope(domain_ids=("narrative",)),
            subject_kinds=("constraint_proposal", "patch", "patch"),
            route_role="content_designer",
            required_action="approval.decide",
            resource_kind="approval",
            min_approvals=1,
            distinct_from_rule_ids=(),
        ),
        DomainRouteRule(
            rule_id="global",
            domain_selector="all",
            subject_kinds=("rollback_request",),
            route_role="tooling",
            required_action="rollback.approve",
            resource_kind="approval",
            min_approvals=2,
            distinct_from_rule_ids=("narrative",),
        ),
    )
    digest = compute_domain_route_policy_digest(
        "routes@1", domain_ref, rules, "2026-07-13T00:00:00Z"
    )
    policy = DomainRoutePolicy(
        route_version="routes@1",
        domain_registry_ref=domain_ref,
        rules=rules,
        effective_from="2026-07-13T00:00:00Z",
        route_digest=digest,
    )

    assert [rule.rule_id for rule in policy.rules] == ["global", "narrative"]
    assert policy.rules[1].subject_kinds == ("patch", "constraint_proposal")
    assert DomainRoutePolicy.model_validate(policy.model_dump(mode="json")) == policy
    with pytest.raises(ValidationError, match="unknown distinct_from_rule_id"):
        bad = DomainRouteRule(
            rule_id="bad",
            domain_selector="all",
            subject_kinds=("patch",),
            route_role="tooling",
            required_action="approval.decide",
            resource_kind="approval",
            min_approvals=1,
            distinct_from_rule_ids=("missing",),
        )
        DomainRoutePolicy(
            route_version="routes@bad",
            domain_registry_ref=domain_ref,
            rules=(bad,),
            effective_from="2026-07-13T00:00:00Z",
            route_digest=compute_domain_route_policy_digest(
                "routes@bad", domain_ref, (bad,), "2026-07-13T00:00:00Z"
            ),
        )


def test_assignment_and_principal_projection_are_identity_safe() -> None:
    active = RoleAssignmentV1(
        assignment_id="assignment:1",
        principal_id="human:alice",
        role="content_designer",
        scope=DomainScope(domain_ids=("narrative",)),
        status="active",
        revision=1,
        granted_at="2026-07-13T00:00:00Z",
        granted_by=_actor("human:admin"),
    )
    principal = Principal(
        id="human:alice",
        kind="human",
        display_name="Alice",
        status="active",
        revision=1,
        credential_epoch=0,
        authz_revision=1,
        roles=(active,),
    )

    assert principal.roles == (active,)
    with pytest.raises(ValidationError, match="principal_id"):
        Principal(
            id="human:bob",
            kind="human",
            display_name="Bob",
            status="active",
            revision=1,
            credential_epoch=0,
            authz_revision=1,
            roles=(active,),
        )
    revoked = RoleAssignmentV1(
        assignment_id="assignment:1",
        principal_id="human:alice",
        role="content_designer",
        scope=DomainScope(domain_ids=("narrative",)),
        status="revoked",
        revision=2,
        granted_at="2026-07-13T00:00:00Z",
        granted_by=_actor("human:admin"),
        revoked_at="2026-07-13T01:00:00Z",
        revoked_by=_actor("human:admin"),
        revoke_reason="role_changed",
    )
    with pytest.raises(ValidationError, match="active role assignments"):
        Principal(
            id="human:alice",
            kind="human",
            display_name="Alice",
            status="active",
            revision=2,
            credential_epoch=0,
            authz_revision=2,
            roles=(revoked,),
        )


def test_principal_record_and_authentication_context_reject_ambiguous_states() -> None:
    record = PrincipalRecordV1(
        principal_id="service:worker",
        kind="service",
        display_name="Worker",
        status="active",
        credential_epoch=0,
        authz_revision=0,
        revision=1,
        created_at="2026-07-13T00:00:00Z",
        updated_at="2026-07-13T00:00:00Z",
    )
    principal = Principal(
        id=record.principal_id,
        kind=record.kind,
        display_name=record.display_name,
        status=record.status,
        revision=record.revision,
        credential_epoch=record.credential_epoch,
        authz_revision=record.authz_revision,
        roles=(),
    )

    with pytest.raises(ValidationError, match="credential_id"):
        AuthenticationContext(mechanism="session")
    actor = ActorContext(
        principal=principal,
        authentication=AuthenticationContext(mechanism="api_key", credential_id="credential:key:1"),
        request_id="request:1",
    )
    assert actor.principal.kind == "service"
    with pytest.raises(ValidationError, match="session_id"):
        ActorContext(
            principal=principal,
            authentication=AuthenticationContext(
                mechanism="api_key", credential_id="credential:key:1"
            ),
            session_id="session:forbidden",
            request_id="request:1",
        )


def test_revoked_and_disabled_wire_details_remain_optional() -> None:
    revoked = RoleAssignmentV1(
        assignment_id="assignment:revoked",
        principal_id="human:alice",
        role="qa",
        scope=None,
        status="revoked",
        revision=2,
        granted_at="2026-07-13T00:00:00Z",
        granted_by=_actor("human:admin"),
    )
    disabled = PrincipalRecordV1(
        principal_id="human:alice",
        kind="human",
        display_name="Alice",
        status="disabled",
        credential_epoch=1,
        authz_revision=2,
        revision=2,
        created_at="2026-07-13T00:00:00Z",
        updated_at="2026-07-13T01:00:00Z",
    )

    assert revoked.revoked_at is None
    assert disabled.disabled_reason is None


def test_domain_route_policy_ref_carries_exact_registry_ref() -> None:
    ref = DomainRoutePolicyRefV1(
        route_version="routes@1",
        route_digest="1" * 64,
        domain_registry_ref=_domain_ref(),
    )
    assert ref.domain_registry_ref.registry_version == "domains@1"
