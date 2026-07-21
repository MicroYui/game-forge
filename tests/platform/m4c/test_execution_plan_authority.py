from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.execution_graphs import (
    AgentExecutionGraphV1,
    AgentExecutionNodeV1,
    agent_execution_graph_digest,
)
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    execution_version_plan_digest,
)
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingBudgetPredicateV1,
    RoutingPolicyV1,
    RoutingRuleV1,
    compute_model_catalog_digest,
    compute_routing_policy_digest,
)
from gameforge.platform.runs.execution_plan import (
    ExecutionVersionPlanResolver,
    ExecutionVersionPlanAuthorityValidator,
    LegacyExecutionVersionPlanAuthorityValidator,
)


NOW = datetime(2026, 7, 16, tzinfo=UTC)


class _Authority:
    def __init__(
        self,
        *,
        catalogs: tuple[ModelCatalogSnapshotV1, ...] = (),
        policies: tuple[RoutingPolicyV1, ...] = (),
    ) -> None:
        self.catalogs = {(item.catalog_version, item.catalog_digest): item for item in catalogs}
        self.policies = {
            (item.policy_version, item.routing_policy_digest): item for item in policies
        }
        self.calls: list[tuple[str, int, str]] = []

    def get_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None:
        self.calls.append(("catalog", catalog_version, catalog_digest))
        return self.catalogs.get((catalog_version, catalog_digest))

    def get_routing_policy(
        self,
        policy_version: int,
        routing_policy_digest: str,
    ) -> RoutingPolicyV1 | None:
        self.calls.append(("policy", policy_version, routing_policy_digest))
        return self.policies.get((policy_version, routing_policy_digest))


def _descriptor(
    name: str,
    *,
    status: str = "active",
) -> ModelDescriptorV1:
    return ModelDescriptorV1(
        provider="openai",
        model_snapshot=f"openai:{name}",
        tier="best",
        capabilities=("reasoning",),
        context_limit=200_000,
        max_output_tokens=32_000,
        prompt_cache_support=True,
        status=status,
    )


def _catalog(
    *descriptors: ModelDescriptorV1,
    version: int = 7,
) -> ModelCatalogSnapshotV1:
    payload = {
        "catalog_version": version,
        "models": descriptors,
        "created_at": NOW,
    }
    return ModelCatalogSnapshotV1(
        **payload,
        catalog_digest=compute_model_catalog_digest(payload),
    )


def _rule(
    node_id: str,
    primary: str,
    *,
    fallbacks: tuple[str, ...] = (),
    rule_id: str | None = None,
    domain_scope: tuple[str, ...] | None = None,
    budget_predicates: tuple[RoutingBudgetPredicateV1, ...] = (),
) -> RoutingRuleV1:
    return RoutingRuleV1(
        rule_id=rule_id or node_id,
        task_kind=node_id,
        domain_scope=domain_scope,
        required_capabilities=("reasoning",),
        primary_model_snapshot=primary,
        allowed_fallback_chain=fallbacks,
        budget_predicates=budget_predicates,
    )


def _policy(
    catalog: ModelCatalogSnapshotV1,
    *rules: RoutingRuleV1,
    version: int = 11,
) -> RoutingPolicyV1:
    payload = {
        "policy_version": version,
        "catalog_version": catalog.catalog_version,
        "catalog_digest": catalog.catalog_digest,
        "rules": rules,
        "failure_classifier_version": "failure-classifier@1",
    }
    return RoutingPolicyV1(
        **payload,
        routing_policy_digest=compute_routing_policy_digest(payload),
    )


def _node(
    node_id: str,
    *allowed_models: str,
) -> PlannedAgentNodeVersionV1:
    return PlannedAgentNodeVersionV1(
        agent_node_id=node_id,
        prompt_version=f"{node_id}-prompt@1",
        tool_version=f"{node_id}-tool@1",
        allowed_model_snapshots=allowed_models,
    )


def _plan(
    catalog: ModelCatalogSnapshotV1,
    policy: RoutingPolicyV1,
    *nodes: PlannedAgentNodeVersionV1,
) -> ExecutionVersionPlanV1:
    payload = {
        "agent_graph_version": "agent-graph@1",
        "nodes": nodes,
        "model_catalog_version": catalog.catalog_version,
        "model_catalog_digest": catalog.catalog_digest,
        "routing_policy_version": policy.policy_version,
        "routing_policy_digest": policy.routing_policy_digest,
    }
    return ExecutionVersionPlanV1(
        **payload,
        plan_digest=execution_version_plan_digest(payload),
    )


def _valid_graph() -> tuple[
    ModelCatalogSnapshotV1,
    RoutingPolicyV1,
    ExecutionVersionPlanV1,
]:
    primary = _descriptor("primary")
    fallback = _descriptor("fallback")
    catalog = _catalog(primary, fallback)
    policy = _policy(
        catalog,
        _rule(
            "generation",
            primary.model_snapshot,
            fallbacks=(fallback.model_snapshot,),
        ),
    )
    plan = _plan(
        catalog,
        policy,
        _node("generation", primary.model_snapshot, fallback.model_snapshot),
    )
    return catalog, policy, plan


def _retained_graph(
    plan: ExecutionVersionPlanV1,
    *,
    graph_version: str | None = None,
    nodes: tuple[AgentExecutionNodeV1, ...] | None = None,
) -> AgentExecutionGraphV1:
    body = {
        "agent_graph_version": graph_version or plan.agent_graph_version,
        "run_kind": RunKindRef(kind="generation.propose", version=1),
        "executor_key": "generation_proposer@1",
        "status": "active",
        "profile_selector": None,
        "nodes": nodes
        or tuple(
            AgentExecutionNodeV1(
                agent_node_id=node.agent_node_id,
                prompt_version=node.prompt_version,
                tool_version=node.tool_version,
                required_capabilities=("reasoning",),
            )
            for node in plan.nodes
        ),
    }
    return AgentExecutionGraphV1(
        **body,
        graph_digest=agent_execution_graph_digest(body),
    )


def test_execution_plan_resolves_both_exact_authorities() -> None:
    catalog, policy, plan = _valid_graph()
    authority = _Authority(catalogs=(catalog,), policies=(policy,))

    result = ExecutionVersionPlanAuthorityValidator(authority).validate(plan)

    assert result is None
    assert authority.calls == [
        ("catalog", plan.model_catalog_version, plan.model_catalog_digest),
        ("policy", plan.routing_policy_version, plan.routing_policy_digest),
    ]


def _resolver(
    authority: _Authority,
    *,
    policy_version: int | None,
    policy_digest: str | None,
) -> ExecutionVersionPlanResolver:
    return ExecutionVersionPlanResolver(
        authority_scope=lambda: nullcontext(authority),
        routing_policy_version=policy_version,
        routing_policy_digest=policy_digest,
    )


def test_execution_plan_resolver_fails_closed_when_deployment_pointer_is_absent() -> None:
    catalog, policy, plan = _valid_graph()
    authority = _Authority(catalogs=(catalog,), policies=(policy,))
    resolver = _resolver(authority, policy_version=None, policy_digest=None)

    with pytest.raises(DependencyUnavailable, match="routing-policy pointer"):
        resolver.resolve(
            graph=_retained_graph(plan),
            llm_execution_mode="live",
        )

    assert authority.calls == []


@pytest.mark.parametrize("llm_execution_mode", ["live", "record"])
def test_execution_plan_resolver_builds_and_validates_from_exact_policy_pointer(
    llm_execution_mode: str,
) -> None:
    catalog, policy, expected = _valid_graph()
    authority = _Authority(catalogs=(catalog,), policies=(policy,))
    resolver = _resolver(
        authority,
        policy_version=policy.policy_version,
        policy_digest=policy.routing_policy_digest,
    )

    actual = resolver.resolve(
        graph=_retained_graph(expected),
        llm_execution_mode=llm_execution_mode,  # type: ignore[arg-type]
    )

    assert actual == expected
    assert authority.calls[0] == (
        "policy",
        policy.policy_version,
        policy.routing_policy_digest,
    )
    assert authority.calls[1] == (
        "catalog",
        policy.catalog_version,
        policy.catalog_digest,
    )


def test_execution_plan_resolver_derives_catalog_from_policy_and_ignores_newer_history() -> None:
    selected_catalog, selected_policy, selected_plan = _valid_graph()
    newer_descriptor = _descriptor("newer")
    newer_catalog = _catalog(newer_descriptor, version=selected_catalog.catalog_version + 1)
    newer_policy = _policy(
        newer_catalog,
        _rule("generation", newer_descriptor.model_snapshot),
        version=selected_policy.policy_version + 1,
    )
    authority = _Authority(
        catalogs=(selected_catalog, newer_catalog),
        policies=(selected_policy, newer_policy),
    )
    resolver = _resolver(
        authority,
        policy_version=selected_policy.policy_version,
        policy_digest=selected_policy.routing_policy_digest,
    )

    actual = resolver.resolve(
        graph=_retained_graph(selected_plan),
        llm_execution_mode="live",
    )

    assert actual == selected_plan
    assert all(
        call[1:] != (newer_policy.policy_version, newer_policy.routing_policy_digest)
        for call in authority.calls
    )
    assert all(
        call[1:] != (newer_catalog.catalog_version, newer_catalog.catalog_digest)
        for call in authority.calls
    )


def test_execution_plan_resolver_rejects_wrong_configured_policy_digest() -> None:
    catalog, policy, plan = _valid_graph()
    authority = _Authority(catalogs=(catalog,), policies=(policy,))
    resolver = _resolver(
        authority,
        policy_version=policy.policy_version,
        policy_digest="f" * 64,
    )

    with pytest.raises(IntegrityViolation, match="configured exact routing policy"):
        resolver.resolve(
            graph=_retained_graph(plan),
            llm_execution_mode="record",
        )

    assert authority.calls == [("policy", policy.policy_version, "f" * 64)]


def test_execution_plan_resolver_replay_uses_source_plan_not_deployment_pointer() -> None:
    catalog, policy, source_plan = _valid_graph()
    authority = _Authority(catalogs=(catalog,), policies=(policy,))
    resolver = _resolver(
        authority,
        policy_version=policy.policy_version + 100,
        policy_digest="f" * 64,
    )

    actual = resolver.resolve(
        graph=_retained_graph(source_plan),
        llm_execution_mode="replay",
        replay_plan=source_plan,
    )

    assert actual == source_plan
    assert ("policy", policy.policy_version + 100, "f" * 64) not in authority.calls
    assert ("policy", policy.policy_version, policy.routing_policy_digest) in authority.calls


def test_execution_plan_closes_exact_retained_agent_graph() -> None:
    catalog, policy, plan = _valid_graph()
    graph = _retained_graph(plan)

    ExecutionVersionPlanAuthorityValidator(
        _Authority(catalogs=(catalog,), policies=(policy,))
    ).validate(plan, expected_graph=graph)


def test_legacy_execution_plan_does_not_require_a_native_routing_policy() -> None:
    catalog, _policy_snapshot, plan = _valid_graph()
    graph = _retained_graph(plan)
    authority = _Authority(catalogs=(catalog,), policies=())

    LegacyExecutionVersionPlanAuthorityValidator(authority).validate(
        plan,
        expected_graph=graph,
    )

    assert authority.calls == [
        ("catalog", plan.model_catalog_version, plan.model_catalog_digest),
    ]


def test_legacy_execution_plan_rejects_an_allowlisted_model_outside_catalog() -> None:
    catalog, policy, plan = _valid_graph()
    missing = _node("generation", "openai:missing")
    wrong_plan = _plan(catalog, policy, missing)
    graph = _retained_graph(wrong_plan)

    with pytest.raises(IntegrityViolation, match="allowed model is missing"):
        LegacyExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=())
        ).validate(wrong_plan, expected_graph=graph)


def test_execution_plan_rejects_another_retained_graph_version() -> None:
    catalog, policy, plan = _valid_graph()
    graph = _retained_graph(plan, graph_version="agent-graph@2")

    with pytest.raises(IntegrityViolation, match="graph version"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan, expected_graph=graph)


def test_execution_plan_rejects_node_set_drift_from_retained_graph() -> None:
    catalog, policy, plan = _valid_graph()
    graph = _retained_graph(
        plan,
        nodes=(
            AgentExecutionNodeV1(
                agent_node_id="repair",
                prompt_version="repair-prompt@1",
                tool_version="repair-tool@1",
                required_capabilities=("reasoning",),
            ),
            *_retained_graph(plan).nodes,
        ),
    )

    with pytest.raises(IntegrityViolation, match="node set differs"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan, expected_graph=graph)


@pytest.mark.parametrize("drift", ["prompt", "tool"])
def test_execution_plan_rejects_node_version_drift_from_retained_graph(
    drift: str,
) -> None:
    catalog, policy, plan = _valid_graph()
    planned = plan.nodes[0]
    graph = _retained_graph(
        plan,
        nodes=(
            AgentExecutionNodeV1(
                agent_node_id=planned.agent_node_id,
                prompt_version=("other-prompt@1" if drift == "prompt" else planned.prompt_version),
                tool_version="other-tool@1" if drift == "tool" else planned.tool_version,
                required_capabilities=("reasoning",),
            ),
        ),
    )

    with pytest.raises(IntegrityViolation, match="node versions differ"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan, expected_graph=graph)


def test_execution_plan_rejects_rule_missing_graph_node_capability() -> None:
    catalog, policy, plan = _valid_graph()
    planned = plan.nodes[0]
    graph = _retained_graph(
        plan,
        nodes=(
            AgentExecutionNodeV1(
                agent_node_id=planned.agent_node_id,
                prompt_version=planned.prompt_version,
                tool_version=planned.tool_version,
                required_capabilities=("reasoning", "vision"),
            ),
        ),
    )

    with pytest.raises(IntegrityViolation, match="omits capabilities"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan, expected_graph=graph)


@pytest.mark.parametrize("missing", ["catalog", "policy"])
def test_execution_plan_rejects_unavailable_exact_history(missing: str) -> None:
    catalog, policy, plan = _valid_graph()
    authority = _Authority(
        catalogs=() if missing == "catalog" else (catalog,),
        policies=() if missing == "policy" else (policy,),
    )

    with pytest.raises(IntegrityViolation, match=f"exact {missing} history is unavailable"):
        ExecutionVersionPlanAuthorityValidator(authority).validate(plan)


def test_execution_plan_rejects_policy_bound_to_another_catalog() -> None:
    catalog, _, _ = _valid_graph()
    other_catalog = _catalog(_descriptor("other"), version=8)
    policy = _policy(
        other_catalog,
        _rule("generation", other_catalog.models[0].model_snapshot),
    )
    plan = _plan(
        catalog,
        policy,
        _node("generation", catalog.models[0].model_snapshot),
    )

    with pytest.raises(IntegrityViolation, match="policy is bound to a different model catalog"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan)


def test_execution_plan_rejects_statically_overlapping_routing_selectors() -> None:
    descriptor = _descriptor("primary")
    catalog = _catalog(descriptor)
    policy = _policy(
        catalog,
        _rule(
            "generation",
            descriptor.model_snapshot,
            rule_id="low-budget",
            budget_predicates=(
                RoutingBudgetPredicateV1(
                    dimension="input_token",
                    operation="lt",
                    value=Decimal(100),
                ),
            ),
        ),
        _rule(
            "generation",
            descriptor.model_snapshot,
            rule_id="all-nonnegative-budget",
            budget_predicates=(
                RoutingBudgetPredicateV1(
                    dimension="input_token",
                    operation="gte",
                    value=Decimal(0),
                ),
            ),
        ),
    )
    plan = _plan(catalog, policy, _node("generation", descriptor.model_snapshot))

    with pytest.raises(IntegrityViolation, match="ambiguous rules at readiness"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan, expected_graph=_retained_graph(plan))


@pytest.mark.parametrize("status", ["missing", "disabled"])
def test_execution_plan_rejects_unexecutable_allowed_model(status: str) -> None:
    primary = _descriptor("primary")
    unavailable = _descriptor("disabled", status="disabled")
    catalog = _catalog(primary, *(() if status == "missing" else (unavailable,)))
    policy = _policy(catalog, _rule("generation", primary.model_snapshot))
    unavailable_id = "openai:missing" if status == "missing" else unavailable.model_snapshot
    plan = _plan(
        catalog,
        policy,
        _node("generation", primary.model_snapshot, unavailable_id),
    )

    with pytest.raises(IntegrityViolation, match=f"allowed model is {status}"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan)


@pytest.mark.parametrize("route_member", ["primary", "fallback"])
def test_execution_plan_rejects_route_outside_node_allowlist(route_member: str) -> None:
    primary = _descriptor("primary")
    fallback = _descriptor("fallback")
    catalog = _catalog(primary, fallback)
    rule = _rule(
        "generation",
        fallback.model_snapshot if route_member == "primary" else primary.model_snapshot,
        fallbacks=(fallback.model_snapshot,) if route_member == "fallback" else (),
    )
    policy = _policy(catalog, rule)
    plan = _plan(catalog, policy, _node("generation", primary.model_snapshot))

    with pytest.raises(IntegrityViolation, match="routing rule escapes its node model allowlist"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan)


def test_execution_plan_ignores_unrelated_global_routing_rules() -> None:
    catalog, policy, plan = _valid_graph()
    extra_policy = _policy(
        catalog,
        *policy.rules,
        _rule("repair", catalog.models[0].model_snapshot),
    )
    plan = _plan(catalog, extra_policy, *plan.nodes)

    ExecutionVersionPlanAuthorityValidator(
        _Authority(catalogs=(catalog,), policies=(extra_policy,))
    ).validate(plan)


def test_execution_plan_rejects_node_without_routing_rule() -> None:
    catalog, policy, plan = _valid_graph()
    plan = _plan(
        catalog,
        policy,
        *plan.nodes,
        _node("repair", catalog.models[0].model_snapshot),
    )

    with pytest.raises(IntegrityViolation, match="plan node has no matching routing rule"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan)


def test_routing_task_kind_is_the_exact_agent_node_id_not_the_run_kind() -> None:
    descriptor = _descriptor("primary")
    catalog = _catalog(descriptor)
    policy = _policy(
        catalog,
        _rule(
            "generation.propose",
            descriptor.model_snapshot,
            rule_id="run-kind-is-not-an-agent-node",
        ),
    )
    plan = _plan(catalog, policy, _node("generation", descriptor.model_snapshot))

    with pytest.raises(IntegrityViolation, match="node has no matching routing rule"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan, expected_graph=_retained_graph(plan))


def test_execution_plan_rejects_allowlisted_model_unreachable_from_policy() -> None:
    primary = _descriptor("primary")
    unreachable = _descriptor("unreachable")
    catalog = _catalog(primary, unreachable)
    policy = _policy(catalog, _rule("generation", primary.model_snapshot))
    plan = _plan(
        catalog,
        policy,
        _node("generation", primary.model_snapshot, unreachable.model_snapshot),
    )

    with pytest.raises(IntegrityViolation, match="model unreachable"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan)


def test_execution_plan_rejects_route_model_without_required_capability() -> None:
    descriptor = ModelDescriptorV1(
        **{
            **_descriptor("primary").model_dump(mode="python"),
            "capabilities": ("text",),
        }
    )
    catalog = _catalog(descriptor)
    policy = _policy(catalog, _rule("generation", descriptor.model_snapshot))
    plan = _plan(catalog, policy, _node("generation", descriptor.model_snapshot))

    with pytest.raises(IntegrityViolation, match="capabilities"):
        ExecutionVersionPlanAuthorityValidator(
            _Authority(catalogs=(catalog,), policies=(policy,))
        ).validate(plan)
