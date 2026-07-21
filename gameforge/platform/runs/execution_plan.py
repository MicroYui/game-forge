"""Exact retained-authority validation for LLM execution version plans."""

from __future__ import annotations

from contextlib import AbstractContextManager
from collections.abc import Callable
from typing import Literal, Protocol

from gameforge.contracts.errors import Conflict, DependencyUnavailable, IntegrityViolation
from gameforge.contracts.execution_graphs import AgentExecutionGraphV1
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    execution_version_plan_digest,
)
from gameforge.contracts.routing import ModelCatalogSnapshotV1, RoutingPolicyV1
from gameforge.platform.cost_policy.routing import RoutingPolicyService


class ExecutionPlanAuthority(Protocol):
    """Retained exact history needed to admit an execution plan."""

    def get_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None: ...

    def get_routing_policy(
        self,
        policy_version: int,
        routing_policy_digest: str,
    ) -> RoutingPolicyV1 | None: ...


class LegacyExecutionPlanAuthority(Protocol):
    """Retained authority a verified legacy import can truthfully prove.

    A legacy import has an exact historical model catalog and an imported routing
    decision, but it did not execute under an M4 ``RoutingPolicyV1``.  Requiring a
    native policy here would fabricate historical rule/tier/budget semantics.
    """

    def get_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None: ...


ExecutionPlanAuthorityScope = Callable[
    [],
    AbstractContextManager[ExecutionPlanAuthority],
]


def validate_execution_plan_domain_coverage(
    authority: ExecutionPlanAuthority,
    plan: ExecutionVersionPlanV1,
    *,
    domain_scope: DomainScope,
) -> None:
    """Require one reachable rule per plan node for the full resolved domain.

    Budget predicates are intentionally outside this admission-time check because
    the worker evaluates them against the frozen remaining budget. Domain coverage
    is already known here and must not be deferred until worker routing.
    """

    if not isinstance(domain_scope, DomainScope):
        raise TypeError("execution-plan domain coverage requires an exact DomainScope")
    policy = authority.get_routing_policy(
        plan.routing_policy_version,
        plan.routing_policy_digest,
    )
    if not isinstance(policy, RoutingPolicyV1):
        raise IntegrityViolation("execution plan exact policy history is unavailable")
    if (
        policy.policy_version != plan.routing_policy_version
        or policy.routing_policy_digest != plan.routing_policy_digest
    ):
        raise IntegrityViolation("routing policy authority returned a non-exact plan binding")

    requested_domains = set(domain_scope.domain_ids)
    uncovered_nodes = sorted(
        node.agent_node_id
        for node in plan.nodes
        if not any(
            rule.task_kind == node.agent_node_id
            and (rule.domain_scope is None or requested_domains.issubset(rule.domain_scope))
            for rule in policy.rules
        )
    )
    if uncovered_nodes:
        raise Conflict(
            "execution routing policy does not cover the resolved resource domain",
            agent_node_ids=uncovered_nodes,
            domain_scope=domain_scope.model_dump(mode="json"),
            routing_policy_version=policy.policy_version,
        )


def build_execution_version_plan(
    *,
    graph: AgentExecutionGraphV1,
    catalog: ModelCatalogSnapshotV1,
    policy: RoutingPolicyV1,
) -> ExecutionVersionPlanV1:
    """Construct the exact plan boundary selected by one graph and routing policy.

    A plan allowlist contains every model reachable from every policy rule for the
    corresponding Agent node.  It does not evaluate a request-time route or select a
    preferred model; the worker remains responsible for that deterministic policy
    decision inside the frozen boundary.
    """

    planned_nodes: list[PlannedAgentNodeVersionV1] = []
    for node in graph.nodes:
        reachable_models = tuple(
            sorted(
                {
                    model_snapshot
                    for rule in policy.rules
                    if rule.task_kind == node.agent_node_id
                    for model_snapshot in (
                        rule.primary_model_snapshot,
                        *rule.allowed_fallback_chain,
                    )
                }
            )
        )
        if not reachable_models:
            raise IntegrityViolation(
                "configured routing policy has no rule for an Agent graph node",
                agent_node_id=node.agent_node_id,
            )
        planned_nodes.append(
            PlannedAgentNodeVersionV1(
                agent_node_id=node.agent_node_id,
                prompt_version=node.prompt_version,
                tool_version=node.tool_version,
                allowed_model_snapshots=reachable_models,
            )
        )
    body = {
        "agent_graph_version": graph.agent_graph_version,
        "nodes": tuple(planned_nodes),
        "model_catalog_version": catalog.catalog_version,
        "model_catalog_digest": catalog.catalog_digest,
        "routing_policy_version": policy.policy_version,
        "routing_policy_digest": policy.routing_policy_digest,
    }
    return ExecutionVersionPlanV1(
        **body,
        plan_digest=execution_version_plan_digest(body),
    )


class ExecutionVersionPlanAuthorityValidator:
    """Prove a plan's exact catalog, policy, node, and model closure."""

    def __init__(self, authority: ExecutionPlanAuthority) -> None:
        self._authority = authority

    def validate(
        self,
        plan: ExecutionVersionPlanV1,
        *,
        expected_graph: AgentExecutionGraphV1 | None = None,
    ) -> None:
        """Fail closed unless every plan routing authority is retained and exact.

        ``ExecutionVersionPlanV1``, ``ModelCatalogSnapshotV1``, and
        ``RoutingPolicyV1`` already verify their own canonical digests. This guard
        verifies the cross-object authority graph used by live, record, and replay
        Run admission; it never resolves a current alias.
        """

        if expected_graph is not None:
            self._validate_graph_binding(plan, expected_graph)

        catalog = self._authority.get_model_catalog(
            plan.model_catalog_version,
            plan.model_catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation("execution plan exact catalog history is unavailable")
        if (
            catalog.catalog_version != plan.model_catalog_version
            or catalog.catalog_digest != plan.model_catalog_digest
        ):
            raise IntegrityViolation("model catalog authority returned a non-exact plan binding")

        policy = self._authority.get_routing_policy(
            plan.routing_policy_version,
            plan.routing_policy_digest,
        )
        if policy is None:
            raise IntegrityViolation("execution plan exact policy history is unavailable")
        if (
            policy.policy_version != plan.routing_policy_version
            or policy.routing_policy_digest != plan.routing_policy_digest
        ):
            raise IntegrityViolation("routing policy authority returned a non-exact plan binding")
        if (
            policy.catalog_version != catalog.catalog_version
            or policy.catalog_digest != catalog.catalog_digest
        ):
            raise IntegrityViolation("routing policy is bound to a different model catalog")

        # Reuse the same pure readiness gate the runtime router executes against.
        # Pydantic closes canonical digests and exact duplicate selectors only;
        # this additionally rejects partially-overlapping domain/budget selectors
        # and any rule whose frozen model chain has no active capable member.
        RoutingPolicyService(catalog=catalog, policy=policy)

        nodes = {node.agent_node_id: node for node in plan.nodes}
        # ``RoutingRuleV1.task_kind`` is the model-call task discriminator.  The
        # worker's model request carries that discriminator as ``agent_node_id``;
        # binding them byte-for-byte is what makes a rule correspond to one
        # ExecutionVersionPlan node instead of merely to the enclosing Run kind.
        rules_by_node = {
            node_id: tuple(rule for rule in policy.rules if rule.task_kind == node_id)
            for node_id in nodes
        }
        nodes_without_rules = sorted(
            node_id for node_id, rules in rules_by_node.items() if not rules
        )
        if nodes_without_rules:
            raise IntegrityViolation(
                "plan node has no matching routing rule",
                agent_node_ids=nodes_without_rules,
            )

        descriptors = {item.model_snapshot: item for item in catalog.models}
        for node in plan.nodes:
            for model_snapshot in node.allowed_model_snapshots:
                descriptor = descriptors.get(model_snapshot)
                if descriptor is None:
                    raise IntegrityViolation(
                        "execution plan allowed model is missing from the exact catalog",
                        agent_node_id=node.agent_node_id,
                        model_snapshot=model_snapshot,
                    )
                if descriptor.status == "disabled":
                    raise IntegrityViolation(
                        "execution plan allowed model is disabled",
                        agent_node_id=node.agent_node_id,
                        model_snapshot=model_snapshot,
                    )

        for node_id, node in nodes.items():
            applicable = rules_by_node[node_id]
            graph_node = (
                None
                if expected_graph is None
                else next(item for item in expected_graph.nodes if item.agent_node_id == node_id)
            )
            reachable_models = {
                model_snapshot
                for rule in applicable
                for model_snapshot in (
                    rule.primary_model_snapshot,
                    *rule.allowed_fallback_chain,
                )
            }
            escaped = sorted(reachable_models.difference(node.allowed_model_snapshots))
            if escaped:
                raise IntegrityViolation(
                    "routing rule escapes its node model allowlist",
                    agent_node_id=node_id,
                    model_snapshots=escaped,
                )
            unreachable = sorted(set(node.allowed_model_snapshots).difference(reachable_models))
            if unreachable:
                raise IntegrityViolation(
                    "execution plan allows a model unreachable from its exact routing policy",
                    agent_node_id=node_id,
                    model_snapshots=unreachable,
                )
            for rule in applicable:
                if graph_node is not None:
                    missing_rule_capabilities = sorted(
                        set(graph_node.required_capabilities).difference(rule.required_capabilities)
                    )
                    if missing_rule_capabilities:
                        raise IntegrityViolation(
                            "routing rule omits capabilities required by its Agent node",
                            rule_id=rule.rule_id,
                            capabilities=missing_rule_capabilities,
                        )
                required = set(rule.required_capabilities)
                for model_snapshot in (
                    rule.primary_model_snapshot,
                    *rule.allowed_fallback_chain,
                ):
                    descriptor = descriptors.get(model_snapshot)
                    if descriptor is None:
                        raise IntegrityViolation(
                            "routing rule references a model absent from the exact catalog",
                            rule_id=rule.rule_id,
                            model_snapshot=model_snapshot,
                        )
                    missing = sorted(required.difference(descriptor.capabilities))
                    if missing:
                        raise IntegrityViolation(
                            "routing model lacks capabilities required by its exact rule",
                            rule_id=rule.rule_id,
                            model_snapshot=model_snapshot,
                            capabilities=missing,
                        )

    @staticmethod
    def _validate_graph_binding(
        plan: ExecutionVersionPlanV1,
        graph: AgentExecutionGraphV1,
    ) -> None:
        if plan.agent_graph_version != graph.agent_graph_version:
            raise IntegrityViolation(
                "execution plan does not bind the retained Agent graph version"
            )
        planned = {item.agent_node_id: item for item in plan.nodes}
        expected = {item.agent_node_id: item for item in graph.nodes}
        if set(planned) != set(expected):
            raise IntegrityViolation(
                "execution plan node set differs from the retained Agent graph",
                missing_agent_node_ids=sorted(set(expected).difference(planned)),
                extra_agent_node_ids=sorted(set(planned).difference(expected)),
            )
        for node_id, expected_node in expected.items():
            node = planned[node_id]
            if (
                node.prompt_version != expected_node.prompt_version
                or node.tool_version != expected_node.tool_version
            ):
                raise IntegrityViolation(
                    "execution plan node versions differ from the retained Agent graph",
                    agent_node_id=node_id,
                )


class ExecutionVersionPlanResolver:
    """Resolve a deploy-selected native plan or validate a source-bound REPLAY plan.

    The deployment pointer names only one exact routing policy.  Its model catalog
    is derived from that retained policy, so there is no second configurable pointer
    and no latest-version fallback.  An absent pointer keeps the API process
    constructible but makes LIVE/RECORD resolution fail closed at use time.
    """

    def __init__(
        self,
        *,
        authority_scope: ExecutionPlanAuthorityScope,
        routing_policy_version: int | None,
        routing_policy_digest: str | None,
    ) -> None:
        if not callable(authority_scope):
            raise TypeError("execution-plan authority_scope must be callable")
        if (routing_policy_version is None) != (routing_policy_digest is None):
            raise ValueError(
                "execution routing-policy version and digest must be configured together"
            )
        if routing_policy_version is not None and (
            isinstance(routing_policy_version, bool)
            or not isinstance(routing_policy_version, int)
            or routing_policy_version < 1
        ):
            raise ValueError("execution routing-policy version must be a positive integer")
        if routing_policy_digest is not None and (
            not isinstance(routing_policy_digest, str)
            or len(routing_policy_digest) != 64
            or any(character not in "0123456789abcdef" for character in routing_policy_digest)
        ):
            raise ValueError("execution routing-policy digest must be a lowercase SHA-256 digest")
        self._authority_scope = authority_scope
        self._routing_policy_version = routing_policy_version
        self._routing_policy_digest = routing_policy_digest

    def resolve(
        self,
        *,
        graph: AgentExecutionGraphV1,
        llm_execution_mode: Literal["live", "record", "replay"],
        replay_plan: ExecutionVersionPlanV1 | None = None,
    ) -> ExecutionVersionPlanV1:
        """Return one validated plan without resolving any mutable alias."""

        if type(graph) is not AgentExecutionGraphV1:
            raise TypeError("execution-plan resolution requires an exact Agent graph")
        if llm_execution_mode == "replay":
            if replay_plan is None:
                raise IntegrityViolation("REPLAY execution lacks its source-bound plan")
            if graph.status not in {"active", "replay_only"}:
                raise IntegrityViolation("Agent graph lifecycle does not permit REPLAY execution")
            with self._authority_scope() as authority:
                ExecutionVersionPlanAuthorityValidator(authority).validate(
                    replay_plan,
                    expected_graph=graph,
                )
            return replay_plan

        if llm_execution_mode not in {"live", "record"}:
            raise ValueError("execution-plan resolver received an unsupported mode")
        if replay_plan is not None:
            raise IntegrityViolation("LIVE/RECORD execution cannot reuse a source plan")
        if graph.status != "active":
            raise IntegrityViolation("Agent graph lifecycle does not permit LIVE/RECORD execution")
        if self._routing_policy_version is None or self._routing_policy_digest is None:
            raise DependencyUnavailable(
                "deployment execution routing-policy pointer is unavailable",
                component="execution_plan_routing_policy",
            )

        with self._authority_scope() as authority:
            policy = authority.get_routing_policy(
                self._routing_policy_version,
                self._routing_policy_digest,
            )
            if not isinstance(policy, RoutingPolicyV1):
                raise IntegrityViolation(
                    "configured exact routing policy is unavailable",
                    policy_version=self._routing_policy_version,
                )
            if (
                policy.policy_version != self._routing_policy_version
                or policy.routing_policy_digest != self._routing_policy_digest
            ):
                raise IntegrityViolation(
                    "configured routing policy authority returned a non-exact binding"
                )
            catalog = authority.get_model_catalog(
                policy.catalog_version,
                policy.catalog_digest,
            )
            if not isinstance(catalog, ModelCatalogSnapshotV1):
                raise IntegrityViolation(
                    "configured routing policy model catalog is unavailable",
                    catalog_version=policy.catalog_version,
                )
            if (
                catalog.catalog_version != policy.catalog_version
                or catalog.catalog_digest != policy.catalog_digest
            ):
                raise IntegrityViolation(
                    "configured routing policy model catalog is not retained exactly"
                )
            plan = build_execution_version_plan(
                graph=graph,
                catalog=catalog,
                policy=policy,
            )
            ExecutionVersionPlanAuthorityValidator(authority).validate(
                plan,
                expected_graph=graph,
            )
            return plan


class LegacyExecutionVersionPlanAuthorityValidator:
    """Validate a verified-import plan without inventing a native route policy."""

    def __init__(self, authority: LegacyExecutionPlanAuthority) -> None:
        self._authority = authority

    def validate(
        self,
        plan: ExecutionVersionPlanV1,
        *,
        expected_graph: AgentExecutionGraphV1,
    ) -> None:
        # The executor graph remains first-class retained authority: a legacy
        # cassette may exercise only a conditional subset of these nodes, but the
        # Run's allowed boundary still has to name the exact graph/node versions.
        ExecutionVersionPlanAuthorityValidator._validate_graph_binding(  # noqa: SLF001
            plan,
            expected_graph,
        )
        catalog = self._authority.get_model_catalog(
            plan.model_catalog_version,
            plan.model_catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation("execution plan exact catalog history is unavailable")
        if (
            catalog.catalog_version != plan.model_catalog_version
            or catalog.catalog_digest != plan.model_catalog_digest
        ):
            raise IntegrityViolation("model catalog authority returned a non-exact plan binding")

        descriptors = {item.model_snapshot: item for item in catalog.models}
        graph_nodes = {item.agent_node_id: item for item in expected_graph.nodes}
        for node in plan.nodes:
            required_capabilities = set(graph_nodes[node.agent_node_id].required_capabilities)
            for model_snapshot in node.allowed_model_snapshots:
                descriptor = descriptors.get(model_snapshot)
                if descriptor is None:
                    raise IntegrityViolation(
                        "execution plan allowed model is missing from the exact catalog",
                        agent_node_id=node.agent_node_id,
                        model_snapshot=model_snapshot,
                    )
                if descriptor.status == "disabled":
                    raise IntegrityViolation(
                        "execution plan allowed model is disabled",
                        agent_node_id=node.agent_node_id,
                        model_snapshot=model_snapshot,
                    )
                missing = sorted(required_capabilities.difference(descriptor.capabilities))
                if missing:
                    raise IntegrityViolation(
                        "legacy execution-plan model lacks Agent-node capabilities",
                        agent_node_id=node.agent_node_id,
                        model_snapshot=model_snapshot,
                        capabilities=missing,
                    )


__all__ = [
    "ExecutionPlanAuthority",
    "ExecutionPlanAuthorityScope",
    "ExecutionVersionPlanResolver",
    "ExecutionVersionPlanAuthorityValidator",
    "LegacyExecutionPlanAuthority",
    "LegacyExecutionVersionPlanAuthorityValidator",
    "build_execution_version_plan",
    "validate_execution_plan_domain_coverage",
]
