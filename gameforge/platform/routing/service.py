"""Pure domain-route resolution over exact versioned policy snapshots."""

from __future__ import annotations

from gameforge.contracts.errors import IntegrityViolation, InvalidStateTransition
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRouteRule,
    DomainScope,
    SubjectKind,
)


def resolve_domain_routes(
    *,
    registry: DomainRegistryV1,
    policy: DomainRoutePolicy,
    subject_kind: SubjectKind,
    domain_scope: DomainScope,
) -> tuple[DomainRouteRule, ...]:
    """Return every configured rule intersecting a new subject's domain scope."""

    expected_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    if policy.domain_registry_ref != expected_ref:
        raise IntegrityViolation("route policy and domain registry refs differ")

    definitions = {definition.domain_id: definition for definition in registry.definitions}
    for rule in policy.rules:
        if rule.domain_selector == "all":
            continue
        unknown_selector_ids = set(rule.domain_selector.domain_ids) - definitions.keys()
        if unknown_selector_ids:
            raise IntegrityViolation(
                "route policy references an unknown domain",
                unknown_domain_ids=sorted(unknown_selector_ids),
            )
    requested = set(domain_scope.domain_ids)
    unknown = requested - definitions.keys()
    if unknown:
        raise IntegrityViolation(
            "route request contains an unknown domain",
            unknown_domain_ids=sorted(unknown),
        )
    deprecated = sorted(
        domain_id for domain_id in requested if definitions[domain_id].status == "deprecated"
    )
    if deprecated:
        raise InvalidStateTransition(
            "deprecated domains cannot be selected for a new resource",
            deprecated_domain_ids=deprecated,
        )

    matched: list[DomainRouteRule] = []
    covered: set[str] = set()
    for rule in policy.rules:
        if subject_kind not in rule.subject_kinds:
            continue
        selected = (
            requested
            if rule.domain_selector == "all"
            else requested.intersection(rule.domain_selector.domain_ids)
        )
        if selected:
            matched.append(rule)
            covered.update(selected)

    if covered != requested:
        raise IntegrityViolation(
            "route policy does not cover the complete domain scope",
            uncovered_domain_ids=sorted(requested - covered),
        )
    return tuple(sorted(matched, key=lambda rule: rule.rule_id))


__all__ = ["resolve_domain_routes"]
