"""Pure conversion from versioned domain routes to approval requirements."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.identity import (
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainScope,
    Permission,
    SubjectKind,
)
from gameforge.contracts.workflow import ApprovalRequirement
from gameforge.platform.routing import resolve_domain_routes


def build_approval_requirements(
    *,
    registry: DomainRegistryV1,
    policy: DomainRoutePolicy,
    subject_kind: SubjectKind,
    domain_scope: DomainScope,
    assignee_principal_ids_by_rule: Mapping[str, Sequence[str]] | None = None,
) -> tuple[ApprovalRequirement, ...]:
    """Resolve the exact configured requirements for one subject revision."""

    rules = resolve_domain_routes(
        registry=registry,
        policy=policy,
        subject_kind=subject_kind,
        domain_scope=domain_scope,
    )
    matched_rule_ids = {rule.rule_id for rule in rules}
    assignees = assignee_principal_ids_by_rule or {}
    unknown_assignee_rules = set(assignees) - matched_rule_ids
    if unknown_assignee_rules:
        raise IntegrityViolation(
            "assignees reference a route outside the resolved requirement set",
            unknown_rule_ids=sorted(unknown_assignee_rules),
        )

    requested_domain_ids = set(domain_scope.domain_ids)
    requirements: list[ApprovalRequirement] = []
    for rule in rules:
        selected_ids = (
            requested_domain_ids
            if rule.domain_selector == "all"
            else requested_domain_ids.intersection(rule.domain_selector.domain_ids)
        )
        requirement_scope = DomainScope(domain_ids=tuple(selected_ids))
        requirements.append(
            ApprovalRequirement(
                requirement_id=rule.rule_id,
                domain_scope=requirement_scope,
                required_permission=Permission(
                    action=rule.required_action,
                    resource_kind=rule.resource_kind,
                    domain_scope=requirement_scope,
                ),
                route_role=rule.route_role,
                min_approvals=rule.min_approvals,
                assignee_principal_ids=tuple(assignees.get(rule.rule_id, ())),
                distinct_from_requirement_ids=tuple(
                    rule_id
                    for rule_id in rule.distinct_from_rule_ids
                    if rule_id in matched_rule_ids
                ),
            )
        )
    return tuple(sorted(requirements, key=lambda item: item.requirement_id))


__all__ = ["build_approval_requirements"]
