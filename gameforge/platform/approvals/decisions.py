"""Pure maker-checker decision evaluation over exact policy snapshots."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class CurrentApproveVoteEvaluation:
    """Current valid approve votes before and after distinct-voter enforcement."""

    valid_actors: tuple[tuple[str, frozenset[str]], ...]
    effective_actors: tuple[tuple[str, frozenset[str]], ...]

    def valid_for(self, requirement_id: str) -> frozenset[str]:
        return dict(self.valid_actors)[requirement_id]

    def effective_for(self, requirement_id: str) -> frozenset[str]:
        return dict(self.effective_actors)[requirement_id]


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


def current_requirement_authority_reason_code(
    *,
    principal: Principal,
    requirement: ApprovalRequirement,
    role_policy: RolePolicy,
    domain_registry: DomainRegistryV1,
) -> str | None:
    """Return the stable current-authority blocker for one frozen requirement."""

    if requirement.assignee_principal_ids and (
        principal.id not in requirement.assignee_principal_ids
    ):
        return "actor_not_assigned"
    assignments = tuple(
        assignment for assignment in principal.roles if assignment.role == requirement.route_role
    )
    if not assignments:
        return "route_role_missing"
    routed_principal = Principal.model_validate(
        {**principal.model_dump(mode="python"), "roles": assignments}
    )
    if (
        authorize(
            principal=routed_principal,
            role_policy=role_policy,
            requested_permission=requirement.required_permission,
            domain_registry=domain_registry,
        )
        is not AuthorizationDecision.ALLOW
    ):
        return "permission_denied"
    return None


def _require_current_permission(
    *,
    principal: Principal,
    requirement: ApprovalRequirement,
    role_policy: RolePolicy,
    domain_registry: DomainRegistryV1,
) -> None:
    reason_code = current_requirement_authority_reason_code(
        principal=principal,
        requirement=requirement,
        role_policy=role_policy,
        domain_registry=domain_registry,
    )
    if reason_code == "actor_not_assigned":
        raise Forbidden(
            "principal is not assigned to the approval requirement",
            requirement_id=requirement.requirement_id,
        )
    if reason_code == "route_role_missing":
        raise Forbidden(
            "approver does not hold the configured route role",
            requirement_id=requirement.requirement_id,
            route_role=requirement.route_role,
        )
    if reason_code == "permission_denied":
        raise Forbidden(
            "approver lacks the current permission for the requirement",
            requirement_id=requirement.requirement_id,
        )


def evaluate_current_approve_votes(
    *,
    item: ApprovalItem,
    principal_resolver: Callable[[str], Principal | None],
    role_policy: RolePolicy,
    domain_registry: DomainRegistryV1,
    additional_decision: ApprovalDecision | None = None,
) -> CurrentApproveVoteEvaluation:
    """Revalidate approve votes using current identities and the item's frozen policy."""

    if item.status == "pending_approval":
        terminal_decision = next(
            (decision for decision in item.decisions if decision.decision != "approve"),
            None,
        )
        if terminal_decision is not None:
            raise IntegrityViolation(
                "pending ApprovalItem contains a terminal decision",
                decision_id=terminal_decision.decision_id,
            )
    requirements = {requirement.requirement_id: requirement for requirement in item.requirements}
    valid = {requirement_id: set() for requirement_id in requirements}
    decisions = (
        item.decisions if additional_decision is None else (*item.decisions, additional_decision)
    )
    for decision in decisions:
        if decision.decision != "approve":
            continue
        unknown = set(decision.requirement_ids) - requirements.keys()
        if unknown:
            raise IntegrityViolation(
                "approval decision references an unknown requirement",
                decision_id=decision.decision_id,
                unknown_requirement_ids=sorted(unknown),
            )
        principal = principal_resolver(decision.actor.principal_id)
        if (
            type(principal) is not Principal
            or principal.id != decision.actor.principal_id
            or principal.kind != decision.actor.principal_kind
            or principal.kind != "human"
            or principal.status != "active"
            or principal.id == item.proposer.principal_id
        ):
            continue
        for requirement_id in decision.requirement_ids:
            requirement = requirements[requirement_id]
            if (
                current_requirement_authority_reason_code(
                    principal=principal,
                    requirement=requirement,
                    role_policy=role_policy,
                    domain_registry=domain_registry,
                )
                is None
            ):
                valid[requirement_id].add(principal.id)

    effective = {key: set(value) for key, value in valid.items()}
    for requirement_id in sorted(requirements):
        for distinct_id in sorted(_distinct_requirement_ids(requirement_id, requirements)):
            if requirement_id >= distinct_id:
                continue
            overlapping = valid[requirement_id].intersection(valid[distinct_id])
            effective[requirement_id].difference_update(overlapping)
            effective[distinct_id].difference_update(overlapping)

    return CurrentApproveVoteEvaluation(
        valid_actors=tuple(
            (requirement_id, frozenset(valid[requirement_id]))
            for requirement_id in sorted(requirements)
        ),
        effective_actors=tuple(
            (requirement_id, frozenset(effective[requirement_id]))
            for requirement_id in sorted(requirements)
        ),
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


def _require_actor_is_new_and_distinct(
    *,
    item: ApprovalItem,
    decision: ApprovalDecision,
    requirements: dict[str, ApprovalRequirement],
    current_votes: CurrentApproveVoteEvaluation,
) -> None:
    selected = set(decision.requirement_ids)
    actor_id = decision.actor.principal_id
    all_currently_satisfied = all_requirements_satisfied(
        requirements=requirements,
        current_votes=current_votes,
    )
    for requirement_id in selected:
        actor_already_approved = any(
            prior.decision == "approve"
            and prior.actor.principal_id == actor_id
            and requirement_id in prior.requirement_ids
            for prior in item.decisions
        )
        is_explicit_reconfirmation = (
            decision.decision == "approve"
            and all_currently_satisfied
            and actor_id in current_votes.effective_for(requirement_id)
        )
        if actor_already_approved and not is_explicit_reconfirmation:
            raise Forbidden(
                "actor already decided this requirement",
                requirement_id=requirement_id,
            )
        if decision.decision != "approve":
            continue
        requirement = requirements[requirement_id]
        if (
            len(current_votes.effective_for(requirement_id)) >= requirement.min_approvals
            and not is_explicit_reconfirmation
        ):
            raise Forbidden(
                "approval requirement is already satisfied",
                requirement_id=requirement_id,
            )
        distinct_ids = _distinct_requirement_ids(requirement_id, requirements)
        if selected.intersection(distinct_ids):
            raise Forbidden(
                "one actor cannot decide distinct requirements",
                requirement_id=requirement_id,
            )
        if any(actor_id in current_votes.valid_for(other_id) for other_id in distinct_ids):
            raise Forbidden(
                "actor already decided a distinct requirement",
                requirement_id=requirement_id,
            )


def all_requirements_satisfied(
    *,
    requirements: dict[str, ApprovalRequirement],
    current_votes: CurrentApproveVoteEvaluation,
) -> bool:
    for requirement in requirements.values():
        if len(current_votes.effective_for(requirement.requirement_id)) < requirement.min_approvals:
            return False
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
    principal_resolver: Callable[[str], Principal | None],
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

    requirements = {requirement.requirement_id: requirement for requirement in item.requirements}
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
        (prior for prior in item.decisions if prior.decision_id == decision.decision_id),
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
    current_votes = evaluate_current_approve_votes(
        item=item,
        principal_resolver=principal_resolver,
        role_policy=role_policy,
        domain_registry=domain_registry,
    )
    _require_actor_is_new_and_distinct(
        item=item,
        decision=decision,
        requirements=requirements,
        current_votes=current_votes,
    )

    if decision.decision == "reject":
        target_status = "rejected"
    elif decision.decision == "request_changes":
        target_status = "changes_requested"
    elif all_requirements_satisfied(
        requirements=requirements,
        current_votes=evaluate_current_approve_votes(
            item=item,
            principal_resolver=principal_resolver,
            role_policy=role_policy,
            domain_registry=domain_registry,
            additional_decision=decision,
        ),
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


def reauthorize_approved_item_for_apply(
    *,
    item: ApprovalItem,
    principal_resolver: Callable[[str], Principal | None],
    domain_registry: DomainRegistryV1,
    route_policy: DomainRoutePolicy,
    role_policy: RolePolicy,
    approval_policy: ApprovalPolicyV1,
) -> None:
    """Revalidate every retained human approval against current identities.

    This is a pure apply-time guard. It neither appends decisions nor advances the
    ApprovalItem; the caller performs the guarded state transition in its UnitOfWork.
    """

    if item.status != "approved":
        raise InvalidStateTransition(
            "apply reauthorization requires approved status",
            current_status=item.status,
        )
    validate_approval_policy_bindings(
        item=item,
        domain_registry=domain_registry,
        route_policy=route_policy,
        role_policy=role_policy,
        approval_policy=approval_policy,
    )

    requirements = {requirement.requirement_id: requirement for requirement in item.requirements}
    approved_actors: dict[str, set[str]] = {
        requirement_id: set() for requirement_id in requirements
    }
    decision_ids: set[str] = set()
    terminal_confirmation_candidates = tuple(
        decision
        for decision in item.decisions
        if decision.decision == "approve"
        and decision.expected_workflow_revision == item.workflow_revision - 1
        and decision.occurred_at == item.decided_at
    )
    terminal_confirmation = (
        terminal_confirmation_candidates[0] if len(terminal_confirmation_candidates) == 1 else None
    )
    prior_confirmation_votes: CurrentApproveVoteEvaluation | None = None
    prior_confirmation_satisfied = False
    if terminal_confirmation is not None:
        prior_item = item.model_copy(
            update={
                "decisions": tuple(
                    decision
                    for decision in item.decisions
                    if decision.decision_id != terminal_confirmation.decision_id
                )
            }
        )
        prior_confirmation_votes = evaluate_current_approve_votes(
            item=prior_item,
            principal_resolver=principal_resolver,
            role_policy=role_policy,
            domain_registry=domain_registry,
        )
        prior_confirmation_satisfied = all_requirements_satisfied(
            requirements=requirements,
            current_votes=prior_confirmation_votes,
        )

    for decision in sorted(
        item.decisions,
        key=lambda value: (value.expected_workflow_revision, value.decision_id),
    ):
        if decision.decision_id in decision_ids:
            raise IntegrityViolation(
                "approved history contains a duplicate decision_id",
                decision_id=decision.decision_id,
            )
        decision_ids.add(decision.decision_id)
        if decision.decision != "approve":
            raise IntegrityViolation(
                "approved history must contain only approve decisions",
                decision_id=decision.decision_id,
                decision=decision.decision,
            )

        unknown = set(decision.requirement_ids) - requirements.keys()
        if unknown:
            raise IntegrityViolation(
                "approval decision references an unknown requirement",
                decision_id=decision.decision_id,
                unknown_requirement_ids=sorted(unknown),
            )

        principal = principal_resolver(decision.actor.principal_id)
        if principal is None:
            raise Forbidden(
                "approval decision actor has no current principal",
                decision_id=decision.decision_id,
                principal_id=decision.actor.principal_id,
            )
        _require_current_actor(item=item, decision=decision, principal=principal)

        for requirement_id in decision.requirement_ids:
            requirement = requirements[requirement_id]
            _require_current_permission(
                principal=principal,
                requirement=requirement,
                role_policy=role_policy,
                domain_registry=domain_registry,
            )
            if principal.id in approved_actors[requirement_id]:
                is_terminal_reconfirmation = (
                    terminal_confirmation is not None
                    and decision.decision_id == terminal_confirmation.decision_id
                    and prior_confirmation_satisfied
                    and prior_confirmation_votes is not None
                    and principal.id in prior_confirmation_votes.effective_for(requirement_id)
                )
                if not is_terminal_reconfirmation:
                    raise IntegrityViolation(
                        "approved history contains duplicate actor coverage",
                        principal_id=principal.id,
                        requirement_id=requirement_id,
                    )
                continue
            approved_actors[requirement_id].add(principal.id)

    for requirement_id, requirement in requirements.items():
        actors = approved_actors[requirement_id]
        if len(actors) < requirement.min_approvals:
            raise IntegrityViolation(
                "approved history does not satisfy minimum approvals",
                requirement_id=requirement_id,
                required=requirement.min_approvals,
                actual=len(actors),
            )
        for distinct_id in _distinct_requirement_ids(requirement_id, requirements):
            shared_actors = actors.intersection(approved_actors[distinct_id])
            if shared_actors:
                raise IntegrityViolation(
                    "approved history violates distinct requirement constraints",
                    requirement_id=requirement_id,
                    distinct_requirement_id=distinct_id,
                    principal_ids=sorted(shared_actors),
                )


__all__ = [
    "CurrentApproveVoteEvaluation",
    "all_requirements_satisfied",
    "apply_approval_decision",
    "current_requirement_authority_reason_code",
    "evaluate_current_approve_votes",
    "reauthorize_approved_item_for_apply",
    "validate_approval_policy_bindings",
]
