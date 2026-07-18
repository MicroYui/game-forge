"""Real Agent-draft success retains one ordered, batched terminal Audit chain."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from gameforge.apps.api.local import build_local_api_resources
from gameforge.apps.worker.agent_prompt_context import (
    build_builtin_agent_prompt_context_authority,
)
from gameforge.apps.worker.app import WORKER_RUN_AUDIT_CHAIN_ID
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.apps.worker.model_authority import (
    StaticCircuitBreakerAuthority,
    StaticStructuredModelSnapshotAuthority,
    StructuredModelSnapshotManifestV1,
    WorkerModelExecutionAuthorities,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRoutePolicy,
    DomainRouteRule,
    DomainScope,
    Permission,
    RolePolicy,
    compute_domain_route_policy_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    execution_version_plan_digest,
)
from gameforge.contracts.lineage import VersionTuple, build_artifact_v2
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.reliability import CircuitBreakerConfigV1
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingPolicyV1,
    RoutingRuleV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
    compute_routing_policy_digest,
)
from gameforge.contracts.workflow import (
    ApprovalPolicyRegistryV1,
    compute_approval_policy_registry_digest,
)
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.runs.admission import AdmissionRequestContext
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.cost.price_book import UnavailablePriceBook
from gameforge.runtime.model_router.typed_transport import TransportResponseV2
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import ApprovalItemRow, AuditRow, SubjectHeadRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.reliability.breaker import CircuitBreaker
from tests.e2e.m4c.test_composition import (
    OBJECT_STORE_ID,
    _Harness,
    _tooling_actor,
)
from tests.platform.m4 import apply_testkit
from tests.platform.m4c.test_constraint_proposal_handler import _PROPOSALS


_DOMAIN = DomainScope(domain_ids=("builtin",))
_MODEL_SNAPSHOT = ModelSnapshot(provider="test", model="agent", snapshot_tag="v1")


class _ConstraintProposalTransport:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request) -> TransportResponseV2:
        return self.complete_with_timeout(request, timeout_s=30)

    def complete_with_timeout(self, request, *, timeout_s: float) -> TransportResponseV2:
        assert timeout_s > 0
        assert request.agent_node_id == "extraction"
        self.calls += 1
        return TransportResponseV2(
            response_normalized=_PROPOSALS,
            raw_response={"id": "response:constraint-proposal"},
            finish_reason="stop",
            tool_calls=(),
            latency=LatencyObservationV1(status="unavailable"),
            token_usage=TokenUsageObservationV1(status="unavailable"),
            provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
        )

    def close(self) -> None:
        return None


def _agent_governance(harness: _Harness) -> tuple[DomainRoutePolicy, RolePolicy, object]:
    registry_ref = DomainRegistryRefV1(
        registry_version=harness.domain_registry.registry_version,
        registry_digest=harness.domain_registry.registry_digest,
    )
    route_rules = (
        DomainRouteRule(
            rule_id="route:builtin-agent-drafts",
            domain_selector=_DOMAIN,
            subject_kinds=("patch", "constraint_proposal", "rollback_request"),
            route_role="tooling",
            required_action="approval.decide",
            resource_kind="approval",
            min_approvals=1,
        ),
    )
    route_version = "e2e-agent-routes@1"
    effective_from = "2026-07-14T00:00:00Z"
    route = DomainRoutePolicy(
        route_version=route_version,
        domain_registry_ref=registry_ref,
        rules=route_rules,
        effective_from=effective_from,
        route_digest=compute_domain_route_policy_digest(
            route_version,
            registry_ref,
            route_rules,
            effective_from,
        ),
    )
    grants = dict(harness.role_policy.grants)
    grants["tooling"] = (
        *grants["tooling"],
        Permission(action="approval.decide", resource_kind="approval", domain_scope=_DOMAIN),
        Permission(action="publish", resource_kind="constraint_proposal", domain_scope=_DOMAIN),
    )
    role_version = "e2e-agent-roles@1"
    roles = RolePolicy(
        policy_version=role_version,
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            role_version,
            registry_ref,
            grants,
            effective_from,
        ),
    )
    return route, roles, apply_testkit._approval_policy()


def _model_authorities() -> tuple[
    WorkerModelExecutionAuthorities,
    ModelCatalogSnapshotV1,
    RoutingPolicyV1,
]:
    model_id = canonical_model_snapshot_id(_MODEL_SNAPSHOT)
    descriptor = ModelDescriptorV1(
        provider="test",
        model_snapshot=model_id,
        tier="best",
        capabilities=("reasoning",),
        context_limit=100_000,
        max_output_tokens=10_000,
        prompt_cache_support=False,
        status="active",
    )
    catalog_body = {
        "catalog_version": 1,
        "models": (descriptor,),
        "created_at": datetime(2026, 7, 18, tzinfo=UTC),
    }
    catalog = ModelCatalogSnapshotV1(
        **catalog_body,
        catalog_digest=compute_model_catalog_digest(catalog_body),
    )
    registry = build_builtin_registry()
    plan_keys = tuple(
        sorted(
            {
                (node.agent_node_id, node.prompt_version, node.tool_version)
                for graph in registry.list_agent_execution_graphs()
                if graph.status in {"active", "replay_only"}
                for node in graph.nodes
            }
        )
    )
    node_ids = tuple(sorted({key[0] for key in plan_keys}))
    rules = tuple(
        RoutingRuleV1(
            rule_id=f"route:{node_id}",
            task_kind=node_id,
            required_capabilities=("reasoning",),
            primary_model_snapshot=model_id,
            allowed_fallback_chain=(),
            budget_predicates=(),
        )
        for node_id in node_ids
    )
    routing_body = {
        "policy_version": 1,
        "catalog_version": catalog.catalog_version,
        "catalog_digest": catalog.catalog_digest,
        "rules": rules,
        "failure_classifier_version": "failure-classifier@1",
    }
    routing = RoutingPolicyV1(
        **routing_body,
        routing_policy_digest=compute_routing_policy_digest(routing_body),
    )
    manifest_body: dict[str, object] = {
        "manifest_schema_version": "structured-model-snapshots@1",
        "authority_version": "e2e-models@1",
        "bindings": [
            {
                "model_snapshot_id": model_id,
                "snapshot": _MODEL_SNAPSHOT.model_dump(mode="json"),
            }
        ],
    }
    manifest = StructuredModelSnapshotManifestV1.model_validate(
        {**manifest_body, "manifest_digest": canonical_sha256(manifest_body)}
    )
    breaker = CircuitBreaker(
        dependency_id=f"model-provider:{model_id}",
        config=CircuitBreakerConfigV1(
            config_version="e2e-breaker@1",
            rolling_window_s=60,
            minimum_samples=2,
            failure_threshold=1,
            open_cooldown_s=10,
            half_open_max_concurrent_probes=1,
            half_open_success_threshold=1,
        ),
        clock=SystemUtcClock(),
    )
    return (
        WorkerModelExecutionAuthorities(
            transport=_ConstraintProposalTransport(),
            snapshots=StaticStructuredModelSnapshotAuthority(manifest),
            prompt_renderer=build_builtin_agent_prompt_context_authority(
                required_plan_keys=plan_keys
            ),
            price_book=UnavailablePriceBook(),
            legacy_imports=None,
            circuit_breaker_resolver=StaticCircuitBreakerAuthority({model_id: breaker}),
        ),
        catalog,
        routing,
    )


def _execution_plan(
    catalog: ModelCatalogSnapshotV1,
    routing: RoutingPolicyV1,
) -> ExecutionVersionPlanV1:
    graph = next(
        graph
        for graph in build_builtin_registry().list_agent_execution_graphs()
        if graph.run_kind.kind == "constraint_proposal.propose" and graph.status == "active"
    )
    body = {
        "agent_graph_version": graph.agent_graph_version,
        "nodes": tuple(
            PlannedAgentNodeVersionV1(
                agent_node_id=node.agent_node_id,
                prompt_version=node.prompt_version,
                tool_version=node.tool_version,
                allowed_model_snapshots=(canonical_model_snapshot_id(_MODEL_SNAPSHOT),),
            )
            for node in graph.nodes
        ),
        "model_catalog_version": catalog.catalog_version,
        "model_catalog_digest": catalog.catalog_digest,
        "routing_policy_version": routing.policy_version,
        "routing_policy_digest": routing.routing_policy_digest,
    }
    return ExecutionVersionPlanV1(
        **body,
        plan_digest=execution_version_plan_digest(body),
    )


def _seed_source(harness: _Harness) -> str:
    objects = LocalObjectStore(
        harness.object_root,
        store_id=OBJECT_STORE_ID,
        clock=harness.clock,
        cursor_signing_key=b"o" * 32,
    )
    stored = objects.put_verified(b"Side quests reward at most 80 gold.")
    artifact = build_artifact_v2(
        kind="source_raw",
        version_tuple=VersionTuple(doc_version="design@1", tool_version="source@1"),
        lineage=(),
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        meta={
            "payload_schema_id": "source-raw@1",
            "domain_scope": _DOMAIN.model_dump(mode="json"),
        },
        created_at="2026-07-18T00:00:00Z",
    )
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            bindings = SqlObjectBindingRepository(session, objects, OBJECT_STORE_ID)
            bindings.bind_verified(stored.ref, stored.location, None)
            SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=harness.clock),
                clock=harness.clock,
            ).put(artifact)
    finally:
        engine.dispose()
    return artifact.artifact_id


def _sql_operation(statement: str) -> str:
    return statement.lstrip().split(None, 1)[0].upper()


def _is_read(statement: str) -> bool:
    return _sql_operation(statement) in {"SELECT", "WITH"}


def _is_dml(statement: str) -> bool:
    return _sql_operation(statement) in {"INSERT", "UPDATE", "DELETE", "REPLACE"}


def _is_audit_read(statement: str) -> bool:
    lowered = statement.lower()
    return _is_read(statement) and ("audit_heads" in lowered or "from audit" in lowered)


def _is_audit_dml(statement: str) -> bool:
    lowered = statement.lower()
    return _is_dml(statement) and ("audit_heads" in lowered or "insert into audit (" in lowered)


def test_agent_draft_terminal_merges_all_audit_into_one_constant_cost_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path)
    source_id = _seed_source(harness)
    route, roles, approval = _agent_governance(harness)
    authorities, catalog, routing = _model_authorities()
    approval_registry = ApprovalPolicyRegistryV1(
        policies=(approval,),
        registry_digest=compute_approval_policy_registry_digest((approval,)),
    )
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            policies = SqlPolicySnapshotRepository(session, clock=harness.clock)
            policies.put_domain_route_policy(route)
            policies.put_role_policy(roles)
            policies.put_approval_policy_registry(approval_registry)
            costs = SqlCostLedger(session, clock=harness.clock)
            costs.put_model_catalog(catalog)
            costs.put_routing_policy(routing)
    finally:
        engine.dispose()

    resources = build_local_api_resources(harness.api_config())
    accepted = resources.dependencies.run_admission.admit_constraint_proposal(
        source_artifact_ids=(source_id,),
        base_constraint_snapshot_artifact_id=None,
        authoring_goal_text="Extract a deterministic gold reward cap.",
        domain_scope=_DOMAIN,
        dsl_grammar_version="dsl@1",
        extraction_policy=ProfileRefV1(
            profile_id="builtin.constraint_extraction",
            version=1,
        ),
        actor=_tooling_actor(harness),
        server=AdmissionRequestContext(
            idempotency_key="constraint-proposal:terminal-audit:1",
            request_hash="a" * 64,
            request_id="request:constraint-proposal:terminal-audit:1",
            trace_id=None,
        ),
        llm_execution_mode="record",
        execution_version_plan=_execution_plan(catalog, routing),
    )
    config = replace(
        harness.worker_config(),
        role_policy_version=roles.policy_version,
        role_policy_digest=roles.policy_digest,
        workflow_route_policy_version=route.route_version,
        workflow_route_policy_digest=route.route_digest,
        workflow_approval_policy_version=approval.policy_version,
        workflow_approval_policy_digest=approval.policy_digest,
    )
    process = build_worker_process(config, model_execution_authorities=authorities)
    runtime_engine = process.runtime.engine
    lifecycle = process.dispatcher._lifecycle  # noqa: SLF001 - production boundary probe
    stager = lifecycle._stage_publications  # noqa: SLF001 - start terminal SQL capture
    assert stager is not None
    original_stage = stager.stage
    capture_terminal = False
    audit_apply_started = False
    flushes_after_audit_apply = 0
    terminal_sql: list[tuple[str, bool, object]] = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        parameters,
        _context,
        executemany,
    ) -> None:
        nonlocal audit_apply_started
        if capture_terminal:
            terminal_sql.append((statement, executemany, parameters))
            if _is_audit_dml(statement) and "audit_heads" in statement.lower():
                audit_apply_started = True

    def capture_flush(*_args: object, **_kwargs: object) -> None:
        nonlocal flushes_after_audit_apply
        if capture_terminal and audit_apply_started:
            flushes_after_audit_apply += 1

    def stage_then_capture(drafts):
        nonlocal capture_terminal
        staged = original_stage(drafts)
        capture_terminal = True
        return staged

    monkeypatch.setattr(stager, "stage", stage_then_capture)
    event.listen(runtime_engine, "before_cursor_execute", capture_statement)
    event.listen(Session, "before_flush", capture_flush)
    try:
        assert asyncio.run(process.dispatcher.dispatch_once()) is True
    finally:
        event.remove(runtime_engine, "before_cursor_execute", capture_statement)
        event.remove(Session, "before_flush", capture_flush)
        process.close()
        resources.close()

    statements = tuple(item[0] for item in terminal_sql)
    first_dml = next(index for index, statement in enumerate(statements) if _is_dml(statement))
    audit_reads = tuple(
        index for index, statement in enumerate(statements) if _is_audit_read(statement)
    )
    assert len(audit_reads) == 2
    assert max(audit_reads) < first_dml
    assert all(not _is_read(statement) for statement in statements[first_dml + 1 :])
    audit_dml = tuple(item for item in terminal_sql if _is_audit_dml(item[0]))
    assert len(audit_dml) == 2
    assert "audit_heads" in audit_dml[0][0].lower()
    assert "insert into audit (" in audit_dml[1][0].lower()
    assert audit_dml[1][1] is True
    assert len(audit_dml[1][2]) == 3
    assert flushes_after_audit_apply == 0

    retained = harness.run_record(accepted.run_id)
    assert retained is not None and retained.status == "succeeded"
    assert isinstance(authorities.transport, _ConstraintProposalTransport)
    assert authorities.transport.calls == 1

    with Session(runtime_engine) as session:
        terminal_audit = tuple(
            reversed(
                session.scalars(
                    select(AuditRow)
                    .where(
                        AuditRow.audit_schema_version == "audit@2",
                        AuditRow.chain_id == WORKER_RUN_AUDIT_CHAIN_ID,
                    )
                    .order_by(AuditRow.chain_seq.desc())
                    .limit(3)
                ).all()
            )
        )
        approval_items = session.scalars(select(ApprovalItemRow)).all()
        subject_heads = session.scalars(select(SubjectHeadRow)).all()

    assert [row.action for row in terminal_audit] == [
        "approval.draft_published",
        "publish_constraint_proposal_draft@1",
        "run.terminal",
    ]
    assert len(approval_items) == 1
    assert len(subject_heads) == 1
    item = approval_items[0]
    head = subject_heads[0]
    assert item.subject_kind == "constraint_proposal"
    assert head.current_subject_artifact_id == item.subject_artifact_id
    assert head.current_approval_id == item.approval_id
    assert terminal_audit[0].subject["resource_kind"] == "approval"
    assert terminal_audit[0].subject["resource_id"] == item.approval_id
    assert terminal_audit[0].artifact_id == item.subject_artifact_id
    assert terminal_audit[1].subject["resource_kind"] == "run"
    assert terminal_audit[1].subject["resource_id"] == accepted.run_id
    assert terminal_audit[2].subject["resource_kind"] == "run"
    assert terminal_audit[2].subject["resource_id"] == accepted.run_id
    assert terminal_audit[2].artifact_id is None
    assert all(row.chain_id == WORKER_RUN_AUDIT_CHAIN_ID for row in terminal_audit)
