"""Deterministic selection over an exact model catalog and routing policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import combinations
from typing import Literal, Protocol

from gameforge.contracts.cost import CostAmountV1, CostDimension
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.model_router import ModelRequestV2, request_hash
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingBudgetPredicateV1,
    RoutingDecisionV1,
    RoutingPolicyV1,
    RoutingRuleV1,
    canonical_model_snapshot_id,
    validate_policy_catalog_closure,
)


class RoutingDecisionRepository(Protocol):
    def put_routing_decision(self, decision: RoutingDecisionV1) -> None: ...


@dataclass(frozen=True, slots=True)
class RouteRequest:
    run_id: str
    attempt_no: int
    task_kind: str
    domain: str | None
    budget_set_snapshot_id: str
    remaining_budget: tuple[CostAmountV1, ...]
    context_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        for name in ("run_id", "task_kind", "budget_set_snapshot_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        if self.attempt_no <= 0:
            raise ValueError("attempt_no must be positive")
        if self.context_tokens < 0 or self.max_output_tokens <= 0:
            raise ValueError("context tokens must be nonnegative and output tokens positive")
        dimensions = [item.dimension for item in self.remaining_budget]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("remaining budget dimensions must be unique")
        object.__setattr__(
            self,
            "remaining_budget",
            tuple(sorted(self.remaining_budget, key=lambda item: item.dimension)),
        )


@dataclass(frozen=True, slots=True)
class RouteSelection:
    request: RouteRequest
    rule: RoutingRuleV1
    descriptor: ModelDescriptorV1
    fallback_from: str | None
    fallback_index: int
    reason_code: str


class RoutingPolicyService:
    """Select and persist only choices admitted by one frozen policy/catalog pair."""

    def __init__(
        self,
        *,
        catalog: ModelCatalogSnapshotV1,
        policy: RoutingPolicyV1,
    ) -> None:
        try:
            validate_policy_catalog_closure(policy, catalog)
        except ValueError as exc:
            raise IntegrityViolation("routing policy/catalog closure is invalid") from exc
        self._catalog = catalog
        self._policy = policy
        self._models = {item.model_snapshot: item for item in catalog.models}
        for rule in policy.rules:
            chain = (rule.primary_model_snapshot, *rule.allowed_fallback_chain)
            if not any(self._statically_viable(self._models[item], rule) for item in chain):
                raise IntegrityViolation(
                    "routing rule has no active model with required capabilities",
                    rule_id=rule.rule_id,
                )
        for left, right in combinations(policy.rules, 2):
            if _rules_overlap(left, right):
                raise IntegrityViolation(
                    "routing policy has ambiguous rules at readiness",
                    rule_ids=(left.rule_id, right.rule_id),
                )

    @property
    def catalog(self) -> ModelCatalogSnapshotV1:
        return self._catalog

    @property
    def policy(self) -> RoutingPolicyV1:
        return self._policy

    def select(self, request: RouteRequest) -> RouteSelection:
        matching = tuple(rule for rule in self._policy.rules if self._matches(rule, request))
        if not matching:
            raise DependencyUnavailable(
                "no routing rule matches the frozen request",
                task_kind=request.task_kind,
                domain=request.domain,
                policy_version=self._policy.policy_version,
            )
        if len(matching) != 1:
            raise IntegrityViolation(
                "routing policy produced an ambiguous match",
                rule_ids=tuple(rule.rule_id for rule in matching),
            )
        return self._select_from_index(
            matching[0],
            request,
            start_index=0,
            reason="primary_rule",
        )

    def next_fallback(
        self,
        selection: RouteSelection,
        *,
        request: RouteRequest,
    ) -> RouteSelection:
        if selection.request != request:
            raise IntegrityViolation("fallback request differs from the original route request")
        known = next(
            (rule for rule in self._policy.rules if rule.rule_id == selection.rule.rule_id),
            None,
        )
        if known != selection.rule:
            raise IntegrityViolation("fallback selection rule is not from the frozen policy")
        chain = (known.primary_model_snapshot, *known.allowed_fallback_chain)
        start = selection.fallback_index + 1
        if start >= len(chain):
            raise DependencyUnavailable(
                "routing fallback chain is exhausted",
                rule_id=known.rule_id,
            )
        return self._select_from_index(
            known,
            request,
            start_index=start,
            reason="fallback_after_failure",
        )

    def decide_and_record(
        self,
        request: RouteRequest,
        *,
        model_request: ModelRequestV2,
        repository: RoutingDecisionRepository,
        execution_source: Literal["online", "full_response_cache", "cassette_replay"],
        decided_at: datetime,
        selection: RouteSelection | None = None,
    ) -> RoutingDecisionV1:
        chosen = selection or self.select(request)
        if chosen.request != request:
            raise IntegrityViolation("routing selection request binding differs")
        selected_model = canonical_model_snapshot_id(model_request.model_snapshot)
        if selected_model != chosen.descriptor.model_snapshot:
            raise IntegrityViolation(
                "rendered model request differs from the selected route model",
                selected_model=chosen.descriptor.model_snapshot,
                rendered_model=selected_model,
            )
        decision = RoutingDecisionV1.create(
            run_id=request.run_id,
            attempt_no=request.attempt_no,
            request_hash=request_hash(model_request),
            rule_id=chosen.rule.rule_id,
            model_snapshot=chosen.descriptor.model_snapshot,
            tier=chosen.descriptor.tier,
            reason_code=chosen.reason_code,
            budget_set_snapshot_id=request.budget_set_snapshot_id,
            fallback_from=chosen.fallback_from,
            fallback_index=chosen.fallback_index,
            policy_version=self._policy.policy_version,
            routing_policy_digest=self._policy.routing_policy_digest,
            catalog_version=self._catalog.catalog_version,
            catalog_digest=self._catalog.catalog_digest,
            execution_source=execution_source,
            decided_at=_require_utc(decided_at),
        )
        repository.put_routing_decision(decision)
        return decision

    def _select_from_index(
        self,
        rule: RoutingRuleV1,
        request: RouteRequest,
        *,
        start_index: int,
        reason: str,
    ) -> RouteSelection:
        chain = (rule.primary_model_snapshot, *rule.allowed_fallback_chain)
        for index in range(start_index, len(chain)):
            descriptor = self._models[chain[index]]
            if not self._viable(descriptor, rule, request):
                continue
            if index == 0:
                fallback_from = None
                reason_code = reason
            else:
                fallback_from = chain[index - 1]
                reason_code = "fallback_model_unavailable" if start_index == 0 else reason
            return RouteSelection(
                request=request,
                rule=rule,
                descriptor=descriptor,
                fallback_from=fallback_from,
                fallback_index=index,
                reason_code=reason_code,
            )
        raise DependencyUnavailable(
            "routing rule has no viable model within the frozen fallback chain",
            rule_id=rule.rule_id,
            start_index=start_index,
        )

    def _matches(self, rule: RoutingRuleV1, request: RouteRequest) -> bool:
        if rule.task_kind != request.task_kind:
            return False
        if rule.domain_scope is not None and request.domain not in rule.domain_scope:
            return False
        amounts = {item.dimension: item for item in request.remaining_budget}
        return all(_matches_predicate(predicate, amounts) for predicate in rule.budget_predicates)

    @staticmethod
    def _statically_viable(
        descriptor: ModelDescriptorV1,
        rule: RoutingRuleV1,
    ) -> bool:
        return descriptor.status == "active" and set(rule.required_capabilities).issubset(
            descriptor.capabilities
        )

    def _viable(
        self,
        descriptor: ModelDescriptorV1,
        rule: RoutingRuleV1,
        request: RouteRequest,
    ) -> bool:
        return (
            self._statically_viable(descriptor, rule)
            and request.max_output_tokens <= descriptor.max_output_tokens
            and request.context_tokens + request.max_output_tokens <= descriptor.context_limit
        )


def _matches_predicate(
    predicate: RoutingBudgetPredicateV1,
    amounts: dict[CostDimension, CostAmountV1],
) -> bool:
    amount = amounts.get(predicate.dimension)
    if amount is None or amount.currency != predicate.currency:
        return False
    left = Decimal(amount.value)
    right = predicate.value
    if predicate.operation == "lt":
        return left < right
    if predicate.operation == "lte":
        return left <= right
    if predicate.operation == "eq":
        return left == right
    if predicate.operation == "gte":
        return left >= right
    return left > right


def _rules_overlap(left: RoutingRuleV1, right: RoutingRuleV1) -> bool:
    if left.task_kind != right.task_kind:
        return False
    if not _domain_scopes_overlap(left.domain_scope, right.domain_scope):
        return False
    predicates = (*left.budget_predicates, *right.budget_predicates)
    by_dimension: dict[CostDimension, list[RoutingBudgetPredicateV1]] = {}
    for predicate in predicates:
        by_dimension.setdefault(predicate.dimension, []).append(predicate)
    return all(_predicate_range_is_satisfiable(items) for items in by_dimension.values())


def _domain_scopes_overlap(
    left: tuple[str, ...] | None,
    right: tuple[str, ...] | None,
) -> bool:
    if left is None or right is None:
        return True
    return bool(set(left).intersection(right))


def _predicate_range_is_satisfiable(
    predicates: list[RoutingBudgetPredicateV1],
) -> bool:
    currencies = {item.currency for item in predicates}
    if len(currencies) > 1:
        return False

    lower = Decimal(0)
    lower_inclusive = True
    upper: Decimal | None = None
    upper_inclusive = False
    for predicate in predicates:
        value = predicate.value
        if predicate.operation in {"gt", "gte", "eq"}:
            inclusive = predicate.operation != "gt"
            if value > lower:
                lower, lower_inclusive = value, inclusive
            elif value == lower:
                lower_inclusive = lower_inclusive and inclusive
        if predicate.operation in {"lt", "lte", "eq"}:
            inclusive = predicate.operation != "lt"
            if upper is None or value < upper:
                upper, upper_inclusive = value, inclusive
            elif value == upper:
                upper_inclusive = upper_inclusive and inclusive

    if upper is None or lower < upper:
        return True
    return lower == upper and lower_inclusive and upper_inclusive


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("routing decision timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


__all__ = [
    "RouteRequest",
    "RouteSelection",
    "RoutingDecisionRepository",
    "RoutingPolicyService",
]
