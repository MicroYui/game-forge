from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

from gameforge.contracts.api import ExecutionOptionResolveRequestV1
from gameforge.contracts.errors import Conflict
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import RefReadBindingV1, RunKindRef
from gameforge.contracts.routing import (
    RoutingPolicyV1,
    compute_routing_policy_digest,
)
from gameforge.platform.runs.execution_plan import ExecutionVersionPlanResolver
from gameforge.runtime.cost.ledger import SqlCostLedger
from tests.platform.m4c import test_run_admission as kit


def _install_scoped_routing_policy(
    harness: kit.Harness,
    *,
    generation_scopes: tuple[tuple[str, ...] | None, ...],
) -> ExecutionVersionPlanResolver:
    _catalog, retained = kit._model_authorities()
    generation_rule = next(rule for rule in retained.rules if rule.task_kind == "generation")
    rules = tuple(rule for rule in retained.rules if rule.task_kind != "generation") + tuple(
        generation_rule.model_copy(
            update={
                "rule_id": f"route:generation:{index}",
                "domain_scope": scope,
            }
        )
        for index, scope in enumerate(generation_scopes, start=1)
    )
    body = {
        "policy_version": 2,
        "catalog_version": retained.catalog_version,
        "catalog_digest": retained.catalog_digest,
        "rules": rules,
        "failure_classifier_version": retained.failure_classifier_version,
    }
    policy = RoutingPolicyV1(
        **body,
        routing_policy_digest=compute_routing_policy_digest(body),
    )
    with Session(harness.engine) as session, session.begin():
        SqlCostLedger(session, clock=harness.clock).put_routing_policy(policy)

    @contextmanager
    def authority_scope():
        with Session(harness.engine) as session:
            yield SqlCostLedger(session, clock=harness.clock)

    resolver = ExecutionVersionPlanResolver(
        authority_scope=authority_scope,
        routing_policy_version=policy.policy_version,
        routing_policy_digest=policy.routing_policy_digest,
    )
    harness.engine_admission._execution_version_plans = resolver  # noqa: SLF001
    return resolver


def _generation_option_request(
    harness: kit.Harness,
    *,
    domain_scope: DomainScope,
) -> ExecutionOptionResolveRequestV1:
    base = harness.seed_artifact(
        kind="ir_snapshot",
        tool_version="generation-domain-coverage-base@1",
        domain_scope=domain_scope,
    )
    return ExecutionOptionResolveRequestV1.model_validate(
        {
            "request_schema_version": "execution-option-resolve-request@1",
            "resource_operation_id": "propose_generation_api_v1_generation_propose_post",
            "run_kind": {"kind": "generation.propose", "version": 1},
            "llm_execution_mode": "record",
            "prospective_request": {
                "request_schema_version": "generation-propose-request@1",
                "base_snapshot_artifact_id": base,
                "constraint_snapshot_artifact_id": None,
                "findings": [],
                "objective_goal_text": "Propose a bounded cross-domain adjustment.",
                "domain_scope": domain_scope.model_dump(mode="json"),
                "target": {"ref_name": "content/head", "expected_ref": None},
                "generation_policy": kit.GENERATION_PROFILE.model_dump(mode="json"),
                "candidate_export_profiles": [],
                "llm_execution_mode": "record",
                "execution_version_plan": None,
                "cassette_artifact_id": None,
            },
            "replay_source_run_id": None,
        }
    )


def test_execution_option_requires_one_rule_to_cover_the_entire_domain_scope(
    tmp_path,
) -> None:
    harness = kit.Harness(tmp_path)
    _install_scoped_routing_policy(
        harness,
        generation_scopes=(("economy",), ("narrative",)),
    )
    scope = DomainScope(domain_ids=("economy", "narrative"))
    request = _generation_option_request(harness, domain_scope=scope)

    with pytest.raises(Conflict, match="routing policy does not cover"):
        harness.engine_admission.resolve_execution_option(
            request=request,
            actor=kit._tooling_actor(),
        )


def test_execution_option_accepts_one_rule_covering_the_entire_domain_scope(
    tmp_path,
) -> None:
    harness = kit.Harness(tmp_path)
    _install_scoped_routing_policy(
        harness,
        generation_scopes=(("economy", "narrative"),),
    )
    scope = DomainScope(domain_ids=("economy", "narrative"))
    request = _generation_option_request(harness, domain_scope=scope)

    option = harness.engine_admission.resolve_execution_option(
        request=request,
        actor=kit._tooling_actor(),
    )

    assert option.domain_scope == scope
    assert option.execution_version_plan.routing_policy_version == 2


def test_final_admission_uses_the_same_domain_coverage_guard(tmp_path) -> None:
    harness = kit.Harness(tmp_path)
    resolver = _install_scoped_routing_policy(
        harness,
        generation_scopes=(("economy",),),
    )
    kind = RunKindRef(kind="generation.propose", version=1)
    graph = next(
        graph
        for graph in harness.registry.list_agent_execution_graphs_for_run_kind(kind)
        if graph.status == "active"
    )
    plan = resolver.resolve(
        graph=graph,
        llm_execution_mode="record",
    )
    narrative = DomainScope(domain_ids=("narrative",))
    base = harness.seed_artifact(
        kind="ir_snapshot",
        tool_version="generation-final-domain-coverage-base@1",
        domain_scope=narrative,
    )
    key = "generation-final-domain-coverage"

    with pytest.raises(Conflict, match="routing policy does not cover"):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=base,
            constraint_snapshot_artifact_id=None,
            findings=(),
            objective_goal_text="Propose a bounded narrative adjustment.",
            domain_scope=narrative,
            target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
            generation_policy=kit.GENERATION_PROFILE,
            candidate_export_profiles=(),
            actor=kit._tooling_actor(),
            server=kit._server(key),
            llm_execution_mode="record",
            execution_version_plan=plan,
        )

    kit._assert_no_admission_side_effects(harness, key=key)
