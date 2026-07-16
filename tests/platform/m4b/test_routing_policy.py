from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from gameforge.contracts.cost import CostAmountV1
from gameforge.contracts.errors import (
    DependencyUnavailable,
    IntegrityViolation,
    QuotaExceeded,
)
from gameforge.contracts.model_router import Message, ModelRequestV2, ModelSnapshot, request_hash
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingBudgetPredicateV1,
    RoutingPolicyV1,
    RoutingRuleV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
    compute_routing_policy_digest,
)
from gameforge.platform.cost_policy.routing import (
    RouteRequest,
    RoutingPolicyService,
)


NOW = datetime(2026, 7, 14, tzinfo=UTC)
SNAPSHOT_A = ModelSnapshot(provider="openai", model="gpt-5.6-sol", snapshot_tag="2026-07-14")
SNAPSHOT_B = ModelSnapshot(provider="openai", model="gpt-5.6-mini", snapshot_tag="2026-07-14")
SNAPSHOT_C = ModelSnapshot(provider="openai", model="gpt-5.6-fast", snapshot_tag="2026-07-14")
MODEL_A = canonical_model_snapshot_id(SNAPSHOT_A)
MODEL_B = canonical_model_snapshot_id(SNAPSHOT_B)
MODEL_C = canonical_model_snapshot_id(SNAPSHOT_C)


def _descriptor(
    model_snapshot: str,
    *,
    status: str = "active",
    capabilities: tuple[str, ...] = ("reasoning", "tools"),
    context_limit: int = 100_000,
    max_output_tokens: int = 8_000,
) -> ModelDescriptorV1:
    return ModelDescriptorV1(
        provider="openai",
        model_snapshot=model_snapshot,
        tier="best" if model_snapshot == MODEL_A else "standard",
        capabilities=capabilities,
        context_limit=context_limit,
        max_output_tokens=max_output_tokens,
        prompt_cache_support=True,
        status=status,
    )


def _catalog(*descriptors: ModelDescriptorV1) -> ModelCatalogSnapshotV1:
    payload = {
        "catalog_version": 1,
        "models": descriptors,
        "created_at": NOW,
    }
    return ModelCatalogSnapshotV1(
        **payload,
        catalog_digest=compute_model_catalog_digest(payload),
    )


def _rule(
    *,
    rule_id: str = "repair-default",
    predicate: RoutingBudgetPredicateV1 | None = None,
    primary: str = MODEL_A,
    fallbacks: tuple[str, ...] = (MODEL_B,),
    domain_scope: tuple[str, ...] | None = None,
) -> RoutingRuleV1:
    return RoutingRuleV1(
        rule_id=rule_id,
        task_kind="patch_repair",
        domain_scope=domain_scope,
        required_capabilities=("reasoning", "tools"),
        primary_model_snapshot=primary,
        allowed_fallback_chain=fallbacks,
        budget_predicates=() if predicate is None else (predicate,),
    )


def _policy(catalog: ModelCatalogSnapshotV1, *rules: RoutingRuleV1) -> RoutingPolicyV1:
    payload = {
        "policy_version": 1,
        "catalog_version": catalog.catalog_version,
        "catalog_digest": catalog.catalog_digest,
        "rules": rules,
        "failure_classifier_version": "classifier@1",
    }
    return RoutingPolicyV1(
        **payload,
        routing_policy_digest=compute_routing_policy_digest(payload),
    )


def _request(
    *,
    remaining: tuple[CostAmountV1, ...] = (),
    domain: str = "default",
    output_tokens: int = 1_000,
    context_tokens: int = 10_000,
) -> RouteRequest:
    return RouteRequest(
        run_id="run-1",
        attempt_no=1,
        task_kind="patch_repair",
        domain_scope=DomainScope(domain_ids=(domain,)),
        budget_set_snapshot_id="budget-set-1",
        remaining_budget=remaining,
        context_tokens=context_tokens,
        max_output_tokens=output_tokens,
    )


def _model_request(snapshot: ModelSnapshot) -> ModelRequestV2:
    return ModelRequestV2(
        model_snapshot=snapshot,
        messages=(Message(role="user", content="repair"),),
        agent_node_id="repair",
        prompt_version="repair@2",
    )


class _Decisions:
    def __init__(self) -> None:
        self.items = []

    def put_routing_decision(self, decision) -> None:
        self.items.append(decision)


def test_route_selects_one_rule_and_persists_decision_before_return() -> None:
    catalog = _catalog(_descriptor(MODEL_A), _descriptor(MODEL_B))
    service = RoutingPolicyService(catalog=catalog, policy=_policy(catalog, _rule()))
    repository = _Decisions()

    decision = service.decide_and_record(
        _request(),
        model_request=_model_request(SNAPSHOT_A),
        repository=repository,
        execution_source="online",
        decided_at=NOW,
    )

    assert repository.items == [decision]
    assert decision.model_snapshot == MODEL_A
    assert decision.request_hash == request_hash(_model_request(SNAPSHOT_A))
    assert decision.reason_code == "primary_rule"
    assert decision.catalog_digest == catalog.catalog_digest


def test_each_fallback_route_persists_its_own_model_bound_request_hash() -> None:
    catalog = _catalog(_descriptor(MODEL_A), _descriptor(MODEL_B))
    service = RoutingPolicyService(catalog=catalog, policy=_policy(catalog, _rule()))
    intent = _request()
    primary = service.select(intent)
    fallback = service.next_fallback(primary, request=intent)
    repository = _Decisions()

    first = service.decide_and_record(
        intent,
        model_request=_model_request(SNAPSHOT_A),
        repository=repository,
        execution_source="online",
        decided_at=NOW,
        selection=primary,
    )
    second = service.decide_and_record(
        intent,
        model_request=_model_request(SNAPSHOT_B),
        repository=repository,
        execution_source="online",
        decided_at=NOW,
        selection=fallback,
    )

    assert first.request_hash == request_hash(_model_request(SNAPSHOT_A))
    assert second.request_hash == request_hash(_model_request(SNAPSHOT_B))
    assert first.request_hash != second.request_hash
    assert repository.items == [first, second]

    with pytest.raises(IntegrityViolation, match="selected route model"):
        service.decide_and_record(
            intent,
            model_request=_model_request(SNAPSHOT_A),
            repository=repository,
            execution_source="online",
            decided_at=NOW,
            selection=fallback,
        )


def test_ambiguous_budget_predicates_fail_at_readiness() -> None:
    catalog = _catalog(_descriptor(MODEL_A), _descriptor(MODEL_B))
    low = _rule(
        rule_id="low",
        predicate=RoutingBudgetPredicateV1(
            dimension="input_token", operation="lt", value=Decimal(100)
        ),
    )
    broad = _rule(
        rule_id="broad",
        predicate=RoutingBudgetPredicateV1(
            dimension="input_token", operation="gte", value=Decimal(0)
        ),
    )
    with pytest.raises(IntegrityViolation, match="ambiguous.*readiness"):
        RoutingPolicyService(catalog=catalog, policy=_policy(catalog, low, broad))


def test_disjoint_budget_boundaries_are_valid_at_readiness() -> None:
    catalog = _catalog(_descriptor(MODEL_A), _descriptor(MODEL_B))
    below = _rule(
        rule_id="below",
        predicate=RoutingBudgetPredicateV1(
            dimension="input_token", operation="lt", value=Decimal(100)
        ),
    )
    at_or_above = _rule(
        rule_id="at-or-above",
        predicate=RoutingBudgetPredicateV1(
            dimension="input_token", operation="gte", value=Decimal(100)
        ),
    )
    service = RoutingPolicyService(
        catalog=catalog,
        policy=_policy(catalog, below, at_or_above),
    )

    selected = service.select(
        _request(remaining=(CostAmountV1(dimension="input_token", value=100, unit="token"),))
    )

    assert selected.rule.rule_id == "at-or-above"


def test_domain_and_missing_budget_do_not_fall_through_to_arbitrary_rule() -> None:
    catalog = _catalog(_descriptor(MODEL_A), _descriptor(MODEL_B))
    rule = _rule(
        domain_scope=("gacha",),
        predicate=RoutingBudgetPredicateV1(
            dimension="input_token", operation="gte", value=Decimal(100)
        ),
    )
    service = RoutingPolicyService(catalog=catalog, policy=_policy(catalog, rule))

    with pytest.raises(IntegrityViolation, match="no routing rule"):
        service.select(_request(domain="quest"))
    with pytest.raises(QuotaExceeded, match="remaining budget"):
        service.select(_request(domain="gacha"))


def test_multi_domain_request_requires_one_rule_covering_the_complete_scope() -> None:
    catalog = _catalog(_descriptor(MODEL_A), _descriptor(MODEL_B))
    rule = _rule(
        rule_id="quest-and-economy",
        domain_scope=("economy", "quest"),
    )
    service = RoutingPolicyService(catalog=catalog, policy=_policy(catalog, rule))
    request = RouteRequest(
        run_id="run-1",
        attempt_no=1,
        task_kind="patch_repair",
        domain_scope=DomainScope(domain_ids=("quest", "economy")),
        budget_set_snapshot_id="budget-set-1",
        remaining_budget=(),
        context_tokens=10_000,
        max_output_tokens=1_000,
    )

    selected = service.select(request)

    assert selected.rule.rule_id == "quest-and-economy"


def test_model_bounds_capabilities_and_status_are_enforced_through_ordered_fallback() -> None:
    catalog = _catalog(
        _descriptor(MODEL_A, status="disabled"),
        _descriptor(MODEL_B, max_output_tokens=2_000),
        _descriptor(MODEL_C, capabilities=("reasoning",)),
    )
    service = RoutingPolicyService(
        catalog=catalog,
        policy=_policy(catalog, _rule(fallbacks=(MODEL_B, MODEL_C))),
    )

    selection = service.select(_request(output_tokens=1_500))
    assert selection.descriptor.model_snapshot == MODEL_B
    assert selection.fallback_index == 1
    assert selection.fallback_from == MODEL_A

    with pytest.raises(IntegrityViolation, match="exceeds every viable model"):
        service.select(_request(output_tokens=3_000))


def test_next_fallback_cannot_skip_or_leave_frozen_chain() -> None:
    catalog = _catalog(
        _descriptor(MODEL_A),
        _descriptor(MODEL_B),
        _descriptor(MODEL_C),
    )
    service = RoutingPolicyService(
        catalog=catalog,
        policy=_policy(catalog, _rule(fallbacks=(MODEL_B, MODEL_C))),
    )
    first = service.select(_request())
    second = service.next_fallback(first, request=_request())
    third = service.next_fallback(second, request=_request())
    assert [first.fallback_index, second.fallback_index, third.fallback_index] == [0, 1, 2]
    assert second.fallback_from == MODEL_A
    assert third.fallback_from == MODEL_B

    with pytest.raises(DependencyUnavailable, match="exhausted"):
        service.next_fallback(third, request=_request())


def test_next_fallback_reports_dependency_exhaustion_when_remaining_models_are_inviable() -> None:
    catalog = _catalog(
        _descriptor(MODEL_A),
        _descriptor(MODEL_B, status="disabled"),
        _descriptor(MODEL_C, max_output_tokens=10),
    )
    service = RoutingPolicyService(
        catalog=catalog,
        policy=_policy(catalog, _rule(fallbacks=(MODEL_B, MODEL_C))),
    )
    primary = service.select(_request())

    with pytest.raises(DependencyUnavailable, match="no available model"):
        service.next_fallback(primary, request=_request(output_tokens=1_000))


def test_policy_catalog_closure_and_model_capability_are_readiness_gates() -> None:
    catalog = _catalog(_descriptor(MODEL_A, capabilities=("reasoning",)))
    with pytest.raises(IntegrityViolation, match="capabilities"):
        RoutingPolicyService(
            catalog=catalog,
            policy=_policy(catalog, _rule(fallbacks=())),
        )
