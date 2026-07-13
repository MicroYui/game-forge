from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from gameforge.contracts.errors import (
    Conflict,
    Forbidden,
    IntegrityViolation,
    InvalidStateTransition,
)
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRouteRule,
    DomainScope,
    Permission,
    Principal,
    RoleAssignmentV1,
    RolePolicy,
    compute_domain_registry_digest,
    compute_domain_route_policy_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalPolicyV1,
    PatchTargetBindingV1,
    compute_approval_policy_digest,
)
from gameforge.platform.approvals import (
    apply_approval_decision,
    build_approval_requirements,
)


NOW = "2026-07-14T10:00:00Z"


def _registry() -> DomainRegistryV1:
    definitions = tuple(
        DomainDefinitionV1(
            domain_id=domain_id,
            display_name=domain_id.title(),
            status="active",
        )
        for domain_id in ("economy", "narrative")
    )
    return DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )


def _registry_ref(registry: DomainRegistryV1) -> DomainRegistryRefV1:
    return DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _route_policy(
    registry: DomainRegistryV1,
    *,
    economy_min: int = 1,
    distinct: bool = False,
    include_narrative: bool = True,
) -> DomainRoutePolicy:
    rules = [
        DomainRouteRule(
            rule_id="route:economy",
            domain_selector=DomainScope(domain_ids=("economy",)),
            subject_kinds=("patch", "constraint_proposal", "rollback_request"),
            route_role="numeric_designer",
            required_action="approval.decide",
            resource_kind="approval",
            min_approvals=economy_min,
            distinct_from_rule_ids=("route:narrative",) if distinct else (),
        )
    ]
    if include_narrative:
        rules.append(
            DomainRouteRule(
                rule_id="route:narrative",
                domain_selector=DomainScope(domain_ids=("narrative",)),
                subject_kinds=("patch", "constraint_proposal", "rollback_request"),
                route_role="content_designer",
                required_action="approval.decide",
                resource_kind="approval",
                min_approvals=1,
                distinct_from_rule_ids=("route:economy",) if distinct else (),
            )
        )
    ref = _registry_ref(registry)
    return DomainRoutePolicy(
        route_version="routes@1",
        domain_registry_ref=ref,
        rules=tuple(rules),
        effective_from=NOW,
        route_digest=compute_domain_route_policy_digest(
            "routes@1", ref, rules, NOW
        ),
    )


def _role_policy(registry: DomainRegistryV1) -> RolePolicy:
    ref = _registry_ref(registry)
    grants = {
        "numeric_designer": (
            Permission(
                action="approval.decide",
                resource_kind="approval",
                domain_scope=DomainScope(domain_ids=("economy",)),
            ),
        ),
        "content_designer": (
            Permission(
                action="approval.decide",
                resource_kind="approval",
                domain_scope=DomainScope(domain_ids=("narrative",)),
            ),
        ),
        "qa": (
            Permission(
                action="approval.decide",
                resource_kind="approval",
                domain_scope="all",
            ),
        ),
    }
    return RolePolicy(
        policy_version="roles@1",
        domain_registry_ref=ref,
        grants=grants,
        effective_from=NOW,
        policy_digest=compute_role_policy_digest("roles@1", ref, grants, NOW),
    )


def _approval_policy() -> ApprovalPolicyV1:
    fields = {
        "policy_version": "approval-policy@1",
        "subject_kinds": ("patch", "constraint_proposal", "rollback_request"),
        "maker_checker_required": True,
        "human_approver_required": True,
        "reauthorize_on_decision": True,
        "reauthorize_on_apply": True,
        "rollback_requires_approval": True,
        "terminal_revision_immutable": True,
    }
    return ApprovalPolicyV1(
        **fields,
        policy_digest=compute_approval_policy_digest(**fields),
    )


def _assignment(
    principal_id: str,
    role: str,
    domain_ids: Sequence[str],
    *,
    assignment_id: str | None = None,
) -> RoleAssignmentV1:
    return RoleAssignmentV1(
        assignment_id=assignment_id or f"assignment:{principal_id}:{role}",
        principal_id=principal_id,
        role=role,  # type: ignore[arg-type]
        scope=DomainScope(domain_ids=tuple(domain_ids)),
        status="active",
        revision=1,
        granted_at=NOW,
        granted_by=AuditActor(principal_id="human:admin", principal_kind="human"),
    )


def _principal(
    principal_id: str,
    *assignments: RoleAssignmentV1,
    kind: str = "human",
    status: str = "active",
) -> Principal:
    return Principal(
        id=principal_id,
        kind=kind,  # type: ignore[arg-type]
        display_name=principal_id,
        status=status,  # type: ignore[arg-type]
        revision=1,
        credential_epoch=0,
        authz_revision=1,
        roles=assignments,
    )


def _item(
    *,
    registry: DomainRegistryV1,
    route_policy: DomainRoutePolicy,
    role_policy: RolePolicy,
    approval_policy: ApprovalPolicyV1,
    domain_ids: Sequence[str] = ("economy",),
    assignees: Mapping[str, Sequence[str]] | None = None,
) -> ApprovalItem:
    scope = DomainScope(domain_ids=tuple(domain_ids))
    requirements = build_approval_requirements(
        registry=registry,
        policy=route_policy,
        subject_kind="patch",
        domain_scope=scope,
        assignee_principal_ids_by_rule=assignees,
    )
    return ApprovalItem(
        approval_id="approval:patch-1",
        subject_series_id="patch-series:1",
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id="artifact:patch-1",
        subject_digest="1" * 64,
        status="pending_approval",
        workflow_revision=3,
        proposer=AuditActor(principal_id="human:maker", principal_kind="human"),
        domain_scope=scope,
        domain_registry_ref=_registry_ref(registry),
        route_policy={
            "route_version": route_policy.route_version,
            "route_digest": route_policy.route_digest,
            "domain_registry_ref": route_policy.domain_registry_ref,
        },
        role_policy_version=role_policy.policy_version,
        role_policy_digest=role_policy.policy_digest,
        approval_policy=ApprovalPolicyRefV1(
            policy_version=approval_policy.policy_version,
            policy_digest=approval_policy.policy_digest,
        ),
        requirements=requirements,
        decisions=(),
        regression_evidence_artifact_ids=(),
        evidence_set_artifact_id="artifact:evidence",
        target_binding=PatchTargetBindingV1(
            target_artifact_id="artifact:preview",
            target_snapshot_id="snapshot:preview",
            target_digest="2" * 64,
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=4),
        ),
        created_at=NOW,
        submitted_at=NOW,
    )


def _decision(
    item: ApprovalItem,
    principal_id: str,
    *requirement_ids: str,
    decision_id: str | None = None,
    decision: str = "approve",
) -> ApprovalDecision:
    return ApprovalDecision(
        decision_id=decision_id or f"decision:{principal_id}:{item.workflow_revision}",
        requirement_ids=requirement_ids,
        decision=decision,  # type: ignore[arg-type]
        actor=AuditActor(principal_id=principal_id, principal_kind="human"),
        expected_workflow_revision=item.workflow_revision,
        reason_code="reviewed",
        occurred_at=NOW,
    )


def _apply(
    item: ApprovalItem,
    decision: ApprovalDecision,
    principal: Principal,
    registry: DomainRegistryV1,
    route_policy: DomainRoutePolicy,
    role_policy: RolePolicy,
    approval_policy: ApprovalPolicyV1,
) -> ApprovalItem:
    return apply_approval_decision(
        item=item,
        decision=decision,
        principal=principal,
        domain_registry=registry,
        route_policy=route_policy,
        role_policy=role_policy,
        approval_policy=approval_policy,
    )


def test_configured_routes_become_exact_scoped_requirements() -> None:
    registry = _registry()
    policy = _route_policy(registry, economy_min=2, distinct=True)

    requirements = build_approval_requirements(
        registry=registry,
        policy=policy,
        subject_kind="patch",
        domain_scope=DomainScope(domain_ids=("economy", "narrative")),
        assignee_principal_ids_by_rule={
            "route:economy": ("human:z", "human:a", "human:z"),
            "route:narrative": ("human:b",),
        },
    )

    assert [requirement.requirement_id for requirement in requirements] == [
        "route:economy",
        "route:narrative",
    ]
    economy, narrative = requirements
    assert economy.domain_scope == DomainScope(domain_ids=("economy",))
    assert economy.required_permission == Permission(
        action="approval.decide",
        resource_kind="approval",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )
    assert economy.route_role == "numeric_designer"
    assert economy.min_approvals == 2
    assert economy.assignee_principal_ids == ("human:a", "human:z")
    assert economy.distinct_from_requirement_ids == ("route:narrative",)
    assert narrative.distinct_from_requirement_ids == ("route:economy",)


def test_route_requirement_omits_distinct_rules_not_resolved_for_this_subject() -> None:
    registry = _registry()
    policy = _route_policy(registry, distinct=True)

    requirements = build_approval_requirements(
        registry=registry,
        policy=policy,
        subject_kind="patch",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )
    assert len(requirements) == 1
    assert requirements[0].distinct_from_requirement_ids == ()


def test_min_approvals_and_partial_approval_keep_item_pending() -> None:
    registry = _registry()
    route = _route_policy(registry, economy_min=2)
    role = _role_policy(registry)
    approval = _approval_policy()
    item = _item(
        registry=registry,
        route_policy=route,
        role_policy=role,
        approval_policy=approval,
    )
    alice = _principal(
        "human:alice",
        _assignment("human:alice", "numeric_designer", ("economy",)),
    )
    bob = _principal(
        "human:bob",
        _assignment("human:bob", "numeric_designer", ("economy",)),
    )

    after_alice = _apply(
        item,
        _decision(item, "human:alice", "route:economy"),
        alice,
        registry,
        route,
        role,
        approval,
    )
    assert after_alice.status == "pending_approval"
    assert after_alice.workflow_revision == 4
    assert after_alice.decided_at is None

    after_bob = _apply(
        after_alice,
        _decision(after_alice, "human:bob", "route:economy"),
        bob,
        registry,
        route,
        role,
        approval,
    )
    assert after_bob.status == "approved"
    assert after_bob.workflow_revision == 5
    assert after_bob.decided_at == NOW


def test_distinct_requirements_need_different_actors() -> None:
    registry = _registry()
    route = _route_policy(registry, distinct=True)
    role = _role_policy(registry)
    approval = _approval_policy()
    item = _item(
        registry=registry,
        route_policy=route,
        role_policy=role,
        approval_policy=approval,
        domain_ids=("economy", "narrative"),
    )
    alice = _principal(
        "human:alice",
        _assignment("human:alice", "numeric_designer", ("economy",)),
        _assignment("human:alice", "content_designer", ("narrative",)),
    )
    bob = _principal(
        "human:bob",
        _assignment("human:bob", "content_designer", ("narrative",)),
    )

    with pytest.raises(Forbidden, match="distinct"):
        _apply(
            item,
            _decision(
                item,
                "human:alice",
                "route:economy",
                "route:narrative",
            ),
            alice,
            registry,
            route,
            role,
            approval,
        )

    economy_done = _apply(
        item,
        _decision(item, "human:alice", "route:economy"),
        alice,
        registry,
        route,
        role,
        approval,
    )
    with pytest.raises(Forbidden, match="distinct"):
        _apply(
            economy_done,
            _decision(economy_done, "human:alice", "route:narrative"),
            alice,
            registry,
            route,
            role,
            approval,
        )

    approved = _apply(
        economy_done,
        _decision(economy_done, "human:bob", "route:narrative"),
        bob,
        registry,
        route,
        role,
        approval,
    )
    assert approved.status == "approved"


def test_duplicate_actor_does_not_count_twice_for_one_requirement() -> None:
    registry = _registry()
    route = _route_policy(registry, economy_min=2)
    role = _role_policy(registry)
    approval = _approval_policy()
    item = _item(
        registry=registry,
        route_policy=route,
        role_policy=role,
        approval_policy=approval,
    )
    alice = _principal(
        "human:alice",
        _assignment("human:alice", "numeric_designer", ("economy",)),
    )
    first = _apply(
        item,
        _decision(item, "human:alice", "route:economy"),
        alice,
        registry,
        route,
        role,
        approval,
    )

    with pytest.raises(Forbidden, match="already decided"):
        _apply(
            first,
            _decision(
                first,
                "human:alice",
                "route:economy",
                decision_id="decision:alice:second",
            ),
            alice,
            registry,
            route,
            role,
            approval,
        )


def test_maker_checker_human_assignee_route_role_and_current_permission_guards() -> None:
    registry = _registry()
    route = _route_policy(registry)
    role = _role_policy(registry)
    approval = _approval_policy()
    item = _item(
        registry=registry,
        route_policy=route,
        role_policy=role,
        approval_policy=approval,
        assignees={"route:economy": ("human:assigned",)},
    )
    cases = (
        (
            "maker",
            _principal(
                "human:maker",
                _assignment("human:maker", "numeric_designer", ("economy",)),
            ),
            "maker-checker",
        ),
        (
            "unassigned",
            _principal(
                "human:unassigned",
                _assignment("human:unassigned", "numeric_designer", ("economy",)),
            ),
            "assigned",
        ),
        (
            "wrong-role",
            _principal(
                "human:assigned",
                _assignment("human:assigned", "qa", ("economy",)),
            ),
            "route role",
        ),
        (
            "disabled",
            _principal(
                "human:assigned",
                _assignment("human:assigned", "numeric_designer", ("economy",)),
                status="disabled",
            ),
            "active human",
        ),
        (
            "service",
            _principal(
                "human:assigned",
                _assignment("human:assigned", "numeric_designer", ("economy",)),
                kind="service",
            ),
            "current principal",
        ),
    )
    principal_id = {
        "maker": "human:maker",
        "unassigned": "human:unassigned",
        "wrong-role": "human:assigned",
        "disabled": "human:assigned",
        "service": "human:assigned",
    }
    for name, principal, message in cases:
        with pytest.raises(Forbidden, match=message):
            _apply(
                item,
                _decision(item, principal_id[name], "route:economy"),
                principal,
                registry,
                route,
                role,
                approval,
            )

    no_grants = RolePolicy(
        policy_version=role.policy_version,
        domain_registry_ref=role.domain_registry_ref,
        grants={},
        effective_from=role.effective_from,
        policy_digest=compute_role_policy_digest(
            role.policy_version,
            role.domain_registry_ref,
            {},
            role.effective_from,
        ),
    )
    stale_item = item.model_copy(
        update={"role_policy_digest": no_grants.policy_digest}
    )
    assigned = _principal(
        "human:assigned",
        _assignment("human:assigned", "numeric_designer", ("economy",)),
    )
    with pytest.raises(Forbidden, match="current permission"):
        _apply(
            stale_item,
            _decision(stale_item, "human:assigned", "route:economy"),
            assigned,
            registry,
            route,
            no_grants,
            approval,
        )


@pytest.mark.parametrize(
    ("decision_kind", "expected_status"),
    [("reject", "rejected"), ("request_changes", "changes_requested")],
)
def test_reject_and_request_changes_are_immediate_terminal_decisions(
    decision_kind: str,
    expected_status: str,
) -> None:
    registry = _registry()
    route = _route_policy(registry, economy_min=2)
    role = _role_policy(registry)
    approval = _approval_policy()
    item = _item(
        registry=registry,
        route_policy=route,
        role_policy=role,
        approval_policy=approval,
    )
    reviewer = _principal(
        "human:reviewer",
        _assignment("human:reviewer", "numeric_designer", ("economy",)),
    )

    result = _apply(
        item,
        _decision(
            item,
            "human:reviewer",
            "route:economy",
            decision=decision_kind,
        ),
        reviewer,
        registry,
        route,
        role,
        approval,
    )
    assert result.status == expected_status
    assert result.decided_at == NOW

    with pytest.raises(InvalidStateTransition, match="pending_approval"):
        _apply(
            result,
            _decision(result, "human:reviewer", "route:economy"),
            reviewer,
            registry,
            route,
            role,
            approval,
        )


def test_terminal_decision_can_cover_distinct_requirements() -> None:
    registry = _registry()
    route = _route_policy(registry, distinct=True)
    role = _role_policy(registry)
    approval = _approval_policy()
    item = _item(
        registry=registry,
        route_policy=route,
        role_policy=role,
        approval_policy=approval,
        domain_ids=("economy", "narrative"),
    )
    reviewer = _principal(
        "human:reviewer",
        _assignment("human:reviewer", "numeric_designer", ("economy",)),
        _assignment("human:reviewer", "content_designer", ("narrative",)),
    )

    result = _apply(
        item,
        _decision(
            item,
            "human:reviewer",
            "route:economy",
            "route:narrative",
            decision="reject",
        ),
        reviewer,
        registry,
        route,
        role,
        approval,
    )
    assert result.status == "rejected"


def test_decision_replay_is_idempotent_but_changed_payload_conflicts() -> None:
    registry = _registry()
    route = _route_policy(registry, economy_min=2)
    role = _role_policy(registry)
    approval = _approval_policy()
    item = _item(
        registry=registry,
        route_policy=route,
        role_policy=role,
        approval_policy=approval,
    )
    reviewer = _principal(
        "human:reviewer",
        _assignment("human:reviewer", "numeric_designer", ("economy",)),
    )
    decision = _decision(
        item,
        "human:reviewer",
        "route:economy",
        decision_id="decision:stable",
    )
    result = _apply(item, decision, reviewer, registry, route, role, approval)

    assert _apply(result, decision, reviewer, registry, route, role, approval) is result

    changed = decision.model_copy(update={"comment": "different payload"})
    with pytest.raises(Conflict, match="decision_id"):
        _apply(result, changed, reviewer, registry, route, role, approval)


def test_workflow_revision_and_exact_policy_snapshots_fail_closed() -> None:
    registry = _registry()
    route = _route_policy(registry)
    role = _role_policy(registry)
    approval = _approval_policy()
    item = _item(
        registry=registry,
        route_policy=route,
        role_policy=role,
        approval_policy=approval,
    )
    reviewer = _principal(
        "human:reviewer",
        _assignment("human:reviewer", "numeric_designer", ("economy",)),
    )

    stale = _decision(item, "human:reviewer", "route:economy").model_copy(
        update={"expected_workflow_revision": item.workflow_revision - 1}
    )
    with pytest.raises(Conflict, match="workflow revision"):
        _apply(item, stale, reviewer, registry, route, role, approval)

    mismatched_snapshots = (
        (
            registry.model_copy(update={"registry_version": "domains@other"}),
            route,
            role,
            approval,
            "domain registry",
        ),
        (
            registry,
            route.model_copy(update={"route_version": "routes@other"}),
            role,
            approval,
            "route policy",
        ),
        (
            registry,
            route,
            role.model_copy(update={"policy_version": "roles@other"}),
            approval,
            "role policy",
        ),
        (
            registry,
            route,
            role,
            approval.model_copy(update={"policy_version": "approval@other"}),
            "approval policy",
        ),
    )
    for bad_registry, bad_route, bad_role, bad_approval, message in mismatched_snapshots:
        with pytest.raises(IntegrityViolation, match=message):
            _apply(
                item,
                _decision(item, "human:reviewer", "route:economy"),
                reviewer,
                bad_registry,
                bad_route,
                bad_role,
                bad_approval,
            )
