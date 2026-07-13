"""Pure maker-checker decision evaluation over exact policy snapshots."""

from __future__ import annotations

from gameforge.contracts.errors import (
    Conflict,
    Forbidden,
    IntegrityViolation,
    InvalidStateTransition,
)
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRoutePolicyRefV1,
    Principal,
    RolePolicy,
)
from gameforge.contracts.workflow import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalPolicyV1,
    ApprovalRequirement,
)
from gameforge.platform.approvals.routing import build_approval_requirements
from gameforge.platform.approvals.state import (
    next_workflow_revision,
    validate_status_transition,
)
from gameforge.platform.rbac import AuthorizationDecision, authorize


def validate_approval_policy_bindings(
    *,
    item: ApprovalItem,
    domain_registry: DomainRegistryV1,
    route_policy: DomainRoutePolicy,
    role_policy: RolePolicy,
    approval_policy: ApprovalPolicyV1,
) -> None:
    domain_ref = DomainRegistryRefV1(
        registry_version=domain_registry.registry_version,
        registry_digest=domain_registry.registry_digest,
    )
    if item.domain_registry_ref != domain_ref:
        raise IntegrityViolation("ApprovalItem domain registry snapshot does not match")
    if role_policy.domain_registry_ref != domain_ref:
        raise IntegrityViolation("role policy domain registry snapshot does not match")
    if (
        item.role_policy_version != role_policy.policy_version
        or item.role_policy_digest != role_policy.policy_digest
    ):
        raise IntegrityViolation("ApprovalItem role policy snapshot does not match")

    route_ref = DomainRoutePolicyRefV1(
        route_version=route_policy.route_version,
        route_digest=route_policy.route_digest,
        domain_registry_ref=route_policy.domain_registry_ref,
    )
    if item.route_policy != route_ref:
        raise IntegrityViolation("ApprovalItem route policy snapshot does not match")

    approval_ref = ApprovalPolicyRefV1(
        policy_version=approval_policy.policy_version,
        policy_digest=approval_policy.policy_digest,
    )
    if item.approval_policy != approval_ref:
        raise IntegrityViolation("ApprovalItem approval policy snapshot does not match")
    if item.subject_kind not in approval_policy.subject_kinds:
        raise IntegrityViolation("approval policy does not support the subject kind")

    assignees = {
        requirement.requirement_id: requirement.assignee_principal_ids
        for requirement in item.requirements
    }
    expected_requirements = build_approval_requirements(
        registry=domain_registry,
        policy=route_policy,
        subject_kind=item.subject_kind,
        domain_scope=item.domain_scope,
        assignee_principal_ids_by_rule=assignees,
    )
    if item.requirements != expected_requirements:
        raise IntegrityViolation("ApprovalItem requirements do not match route policy")


def _require_current_actor(
    *,
    item: ApprovalItem,
    decision: ApprovalDecision,
    principal: Principal,
) -> None:
    if (
        decision.actor.principal_id != principal.id
        or decision.actor.principal_kind != principal.kind
    ):
        raise Forbidden("decision actor does not match the current principal")
    if principal.kind != "human" or principal.status != "active":
        raise Forbidden("approval decisions require an active human principal")
    if principal.id == item.proposer.principal_id:
        raise Forbidden("maker-checker forbids the proposer from deciding")


def _principal_with_route_role(
    principal: Principal,
    requirement: ApprovalRequirement,
) -> Principal:
    assignments = tuple(
        assignment
        for assignment in principal.roles
        if assignment.role == requirement.route_role
    )
    if not assignments:
        raise Forbidden(
            "approver does not hold the configured route role",
            requirement_id=requirement.requirement_id,
            route_role=requirement.route_role,
        )
    payload = principal.model_dump(mode="python")
    payload["roles"] = assignments
    return Principal.model_validate(payload)


def _require_current_permission(
    *,
    principal: Principal,
    requirement: ApprovalRequirement,
    role_policy: RolePolicy,
    domain_registry: DomainRegistryV1,
) -> None:
    if requirement.assignee_principal_ids and (
        principal.id not in requirement.assignee_principal_ids
    ):
        raise Forbidden(
            "principal is not assigned to the approval requirement",
            requirement_id=requirement.requirement_id,
        )
    routed_principal = _principal_with_route_role(principal, requirement)
    if (
        authorize(
            principal=routed_principal,
            role_policy=role_policy,
            requested_permission=requirement.required_permission,
            domain_registry=domain_registry,
        )
        is not AuthorizationDecision.ALLOW
    ):
        raise Forbidden(
            "approver lacks the current permission for the requirement",
            requirement_id=requirement.requirement_id,
        )


def _distinct_requirement_ids(
    requirement_id: str,
    requirements: dict[str, ApprovalRequirement],
) -> set[str]:
    result = set(requirements[requirement_id].distinct_from_requirement_ids)
    result.update(
        candidate.requirement_id
        for candidate in requirements.values()
        if requirement_id in candidate.distinct_from_requirement_ids
    )
    return result


def _approved_actors_by_requirement(
    item: ApprovalItem,
) -> dict[str, set[str]]:
    approved: dict[str, set[str]] = {
        requirement.requirement_id: set() for requirement in item.requirements
    }
    for decision in item.decisions:
        if decision.decision != "approve":
            raise IntegrityViolation(
                "pending ApprovalItem contains a terminal decision",
                decision_id=decision.decision_id,
            )
        for requirement_id in decision.requirement_ids:
            approved[requirement_id].add(decision.actor.principal_id)
    return approved


def _require_actor_is_new_and_distinct(
    *,
    item: ApprovalItem,
    decision: ApprovalDecision,
    requirements: dict[str, ApprovalRequirement],
) -> None:
    approved = _approved_actors_by_requirement(item)
    selected = set(decision.requirement_ids)
    actor_id = decision.actor.principal_id
    for requirement_id in selected:
        if actor_id in approved[requirement_id]:
            raise Forbidden(
                "actor already decided this requirement",
                requirement_id=requirement_id,
            )
        if decision.decision != "approve":
            continue
        distinct_ids = _distinct_requirement_ids(requirement_id, requirements)
        if selected.intersection(distinct_ids):
            raise Forbidden(
                "one actor cannot decide distinct requirements",
                requirement_id=requirement_id,
            )
        if any(actor_id in approved[other_id] for other_id in distinct_ids):
            raise Forbidden(
                "actor already decided a distinct requirement",
                requirement_id=requirement_id,
            )


def _all_requirements_satisfied(
    *,
    item: ApprovalItem,
    new_decision: ApprovalDecision,
    requirements: dict[str, ApprovalRequirement],
) -> bool:
    approved = _approved_actors_by_requirement(item)
    if new_decision.decision == "approve":
        for requirement_id in new_decision.requirement_ids:
            approved[requirement_id].add(new_decision.actor.principal_id)

    for requirement in requirements.values():
        if len(approved[requirement.requirement_id]) < requirement.min_approvals:
            return False
        for distinct_id in _distinct_requirement_ids(
            requirement.requirement_id, requirements
        ):
            if approved[requirement.requirement_id].intersection(approved[distinct_id]):
                raise IntegrityViolation(
                    "approval history violates distinct requirement constraints",
                    requirement_id=requirement.requirement_id,
                    distinct_requirement_id=distinct_id,
                )
    return True


def apply_approval_decision(
    *,
    item: ApprovalItem,
    decision: ApprovalDecision,
    principal: Principal,
    domain_registry: DomainRegistryV1,
    route_policy: DomainRoutePolicy,
    role_policy: RolePolicy,
    approval_policy: ApprovalPolicyV1,
) -> ApprovalItem:
    """Evaluate and append one immutable decision, returning the next item state."""

    validate_approval_policy_bindings(
        item=item,
        domain_registry=domain_registry,
        route_policy=route_policy,
        role_policy=role_policy,
        approval_policy=approval_policy,
    )
    _require_current_actor(item=item, decision=decision, principal=principal)

    requirements = {
        requirement.requirement_id: requirement for requirement in item.requirements
    }
    unknown = set(decision.requirement_ids) - requirements.keys()
    if unknown:
        raise IntegrityViolation(
            "decision references an unknown approval requirement",
            unknown_requirement_ids=sorted(unknown),
        )
    for requirement_id in decision.requirement_ids:
        _require_current_permission(
            principal=principal,
            requirement=requirements[requirement_id],
            role_policy=role_policy,
            domain_registry=domain_registry,
        )

    prior_with_id = next(
        (
            prior
            for prior in item.decisions
            if prior.decision_id == decision.decision_id
        ),
        None,
    )
    if prior_with_id is not None:
        if prior_with_id == decision:
            return item
        raise Conflict(
            "decision_id already exists with a different payload",
            decision_id=decision.decision_id,
        )

    next_revision = next_workflow_revision(
        actual=item.workflow_revision,
        expected=decision.expected_workflow_revision,
    )
    if item.status != "pending_approval":
        raise InvalidStateTransition(
            "approval decisions require pending_approval status",
            current_status=item.status,
        )
    _require_actor_is_new_and_distinct(
        item=item,
        decision=decision,
        requirements=requirements,
    )

    if decision.decision == "reject":
        target_status = "rejected"
    elif decision.decision == "request_changes":
        target_status = "changes_requested"
    elif _all_requirements_satisfied(
        item=item,
        new_decision=decision,
        requirements=requirements,
    ):
        target_status = "approved"
    else:
        target_status = "pending_approval"
    validate_status_transition(
        current=item.status,
        target=target_status,
        subject_kind=item.subject_kind,
    )

    payload = item.model_dump(mode="python")
    payload.update(
        {
            "status": target_status,
            "workflow_revision": next_revision,
            "decisions": (*item.decisions, decision),
            "decided_at": (
                decision.occurred_at
                if target_status in {"approved", "rejected", "changes_requested"}
                else item.decided_at
            ),
        }
    )
    return ApprovalItem.model_validate(payload)


__all__ = ["apply_approval_decision", "validate_approval_policy_bindings"]
