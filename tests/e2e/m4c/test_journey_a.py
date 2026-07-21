"""Task 18 — Journey A through the public API and persistent worker.

The RECORD runs in this module are cassette bootstrap only.  The product journey
uses distinct REPLAY Runs and distinct workflow subjects over the same persistent
SQLite/ObjectStore authority; bootstrap subjects are never counted as Journey A.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import socket

import pytest
from sqlalchemy.orm import Session

from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.apps.worker.model_authority import WorkerModelExecutionAuthorities
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import FindingRevisionV1, finding_revision_digest
from gameforge.contracts.identity import (
    DomainScope,
    Permission,
    RolePolicy,
    compute_role_policy_digest,
)
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    execution_version_plan_digest,
)
from gameforge.contracts.lineage import VersionTuple, build_artifact_v2
from gameforge.contracts.regression import (
    AgentEnvRegressionCaseV1,
    AgentEnvRegressionFindingTemplateV1,
    AgentEnvRegressionPayloadV1,
    AgentEnvRegressionStepV1,
    RegressionSuiteDispatchV1,
)
from gameforge.contracts.routing import canonical_model_snapshot_id
from gameforge.apps.worker.completion_oracles import ALL_QUESTS_COMPLETED_ORACLE
from gameforge.apps.worker.regression import AGENT_ENV_REPLAY_ADAPTER
from gameforge.platform.registry import build_builtin_registry
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.model_router.typed_transport import TransportResponseV2
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.spine.ir.loader import load_scenario
from gameforge.spine.ir.snapshot import Snapshot

from tests.e2e.m4c.test_agent_draft_terminal_audit import (
    _MODEL_SNAPSHOT,
    _model_authorities,
)
from tests.e2e.m4c.test_composition import OBJECT_STORE_ID
from tests.e2e.m4c.test_journey_b import (
    APPROVER_LOGIN,
    APPROVER_PASSWORD,
    DOMAIN,
    MAKER_LOGIN,
    MAKER_PASSWORD,
    REF_NAME,
    _Harness as JourneyBHarness,
    _approval,
    _drive,
    _headers,
    _login,
    _ref_history,
    _role_policy,
    _start_api,
    _stop_api,
)


_DOMAIN_JSON = {"domain_ids": [DOMAIN]}
_GENERATION_OPS = json.dumps(
    [
        {
            "op_id": "generation:emblem-count",
            "op": "set_entity_attr",
            "target": "step:collect_emblem.count",
            "old_value": 3,
            "new_value": 4,
        }
    ]
)
_REPAIR_OPS = json.dumps(
    [
        {
            "op_id": "repair:emblem-count",
            "op": "set_entity_attr",
            "target": "step:collect_emblem.count",
            "old_value": 4,
            "new_value": 3,
        }
    ]
)


def _journey_a_role_policy(registry) -> RolePolicy:
    base = _role_policy(registry)
    maker_grants = (
        *base.grants["content_designer"],
        Permission(
            action="replay",
            resource_kind="run",
            domain_scope=DomainScope(domain_ids=(DOMAIN,)),
        ),
        Permission(
            action="run",
            resource_kind="review",
            domain_scope=DomainScope(domain_ids=(DOMAIN,)),
        ),
        Permission(
            action="derive",
            resource_kind="task_suite",
            domain_scope=DomainScope(domain_ids=(DOMAIN,)),
        ),
        Permission(
            action="run",
            resource_kind="playtest",
            domain_scope=DomainScope(domain_ids=(DOMAIN,)),
        ),
        *(
            Permission(action="read", resource_kind=kind, domain_scope="all")
            for kind in (
                "constraint",
                "review",
                "task_suite",
                "playtest",
                "bench",
                "playtest_result",
                "bench_report",
            )
        ),
    )
    grants = {
        **base.grants,
        "content_designer": maker_grants,
    }
    policy_version = "journey-a-roles@1"
    return RolePolicy(
        policy_version=policy_version,
        domain_registry_ref=base.domain_registry_ref,
        grants=grants,
        effective_from=base.effective_from,
        policy_digest=compute_role_policy_digest(
            policy_version,
            base.domain_registry_ref,
            grants,
            base.effective_from,
        ),
    )


@pytest.fixture(autouse=True)
def _deny_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Journey A's RECORD/REPLAY no-egress claim executable."""

    def denied(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Journey A attempted external network access")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket.socket, "sendto", denied)
    monkeypatch.setattr(socket.socket, "sendmsg", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


class _JourneyTransport:
    """Hermetic provider used only to create native RECORD bootstrap bundles."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.mode = "generation"
        self._playtest_target = 0

    def select(self, mode: str) -> None:
        self.mode = mode
        self._playtest_target = 0

    def complete(self, request) -> TransportResponseV2:
        return self.complete_with_timeout(request, timeout_s=30)

    def complete_with_timeout(self, request, *, timeout_s: float) -> TransportResponseV2:
        assert timeout_s > 0
        self.calls.append(request.agent_node_id)
        response = self._response(request)
        return TransportResponseV2(
            response_normalized=response,
            raw_response={"id": f"journey-a:{len(self.calls)}"},
            finish_reason="stop",
            tool_calls=(),
            latency=LatencyObservationV1(status="unavailable"),
            token_usage=TokenUsageObservationV1(status="unavailable"),
            provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
        )

    def close(self) -> None:
        return None

    def _response(self, request) -> str:
        node = request.agent_node_id
        if node == "generation":
            if self.mode == "generation_reject":
                return json.dumps(
                    [
                        {
                            "op_id": "generation:dangling",
                            "op": "add_relation",
                            "target": "relation:dangling",
                            "new_value": {
                                "type": "DROPS_FROM",
                                "src_id": "ghost:missing",
                                "dst_id": "item:broken_emblem",
                            },
                        }
                    ]
                )
            return _GENERATION_OPS
        if node == "review-triage":
            return json.dumps({"suggestions": []})
        if node == "repair":
            if self.mode == "repair_unverified":
                return "[]"
            return _REPAIR_OPS
        if node == "playtest.planner":
            return json.dumps({"quest": None, "step_kind": "advance"})
        if node == "playtest.reflect":
            return json.dumps({"hint": "try another reachable target"})
        if node == "playtest.executor" and self.mode in {
            "playtest_fail",
            "playtest_pass",
        }:
            targets = ("npc:lincheng", "interact:emblem_pile", "npc:lincheng")
            if self._playtest_target >= len(targets):
                return json.dumps({"kind": "observe"})
            target = targets[self._playtest_target]
            available = next(
                (
                    line
                    for line in request.messages[-1].content.splitlines()
                    if line.startswith("available_interactions=")
                ),
                "",
            )
            if target in available:
                self._playtest_target += 1
                return json.dumps({"kind": "interact", "target": target})
            return json.dumps({"kind": "navigate_to", "target": target})
        if node == "playtest.executor":
            return json.dumps({"kind": "observe"})
        return "{}"


class _Harness(JourneyBHarness):
    """Journey-B composition reused with a real executable Aureus base."""

    def _seed_policies(self) -> None:
        catalogs = build_builtin_registry().list_execution_profile_catalogs()
        self.catalog = catalogs[0]
        self.role_policy = _journey_a_role_policy(self.registry)
        super()._seed_policies()
        engine = get_engine(self.database_url)
        try:
            with Session(engine) as session, session.begin():
                policies = SqlPolicySnapshotRepository(session, clock=self.clock)
                for catalog in catalogs[1:]:
                    policies.put_execution_profile_catalog(catalog)
        finally:
            engine.dispose()
        self.catalog = catalogs[-1]

    def worker_config(self):
        return replace(
            super().worker_config(),
            role_policy_version=self.role_policy.policy_version,
            role_policy_digest=self.role_policy.policy_digest,
            workflow_route_policy_version=self.route.route_version,
            workflow_route_policy_digest=self.route.route_digest,
            workflow_approval_policy_version=self.approval_policy.policy_version,
            workflow_approval_policy_digest=self.approval_policy.policy_digest,
        )

    def api_config(self):
        return replace(
            super().api_config(),
            selected_bench_report_artifact_id=self.bench_report_artifact_id,
        )

    def seed_authoring_inputs(self) -> tuple[str, str, dict]:
        source = load_scenario("scenarios/caravan.yaml")
        nearby_positions = {
            "npc:lincheng": [1, 0],
            "spawn:emblem_pile": [2, 0],
            "interact:emblem_pile": [2, 0],
        }
        snapshot = Snapshot.from_entities_relations(
            (
                entity.model_copy(
                    update={
                        "attrs": {
                            **entity.attrs,
                            "pos": nearby_positions[entity.id],
                        }
                    }
                )
                if entity.id in nearby_positions
                else entity
                for entity in source.entities.values()
            ),
            source.relations.values(),
        )
        base = self._publish_artifact(
            kind="ir_snapshot",
            payload=canonical_json(snapshot.content_payload).encode("utf-8"),
            version_tuple=VersionTuple(
                doc_version="caravan@1",
                ir_snapshot_id=snapshot.snapshot_id,
                tool_version="journey-a-base@1",
            ),
            payload_schema_id="ir-core@1",
        )
        constraint_snapshot_id = "constraint:journey-a@1"
        constraints = self._publish_artifact(
            kind="constraint_snapshot",
            payload=canonical_json({"dsl_grammar_version": "dsl@1", "constraints": []}).encode(
                "utf-8"
            ),
            version_tuple=VersionTuple(
                constraint_snapshot_id=constraint_snapshot_id,
                tool_version="journey-a-constraints@1",
            ),
            payload_schema_id="constraint-snapshot@1",
        )
        report_path = Path(__file__).parents[3] / "scenarios/bench/bench-report.json"
        report_payload = report_path.read_bytes()
        self.bench_report_artifact_id = self._publish_artifact(
            kind="bench_report",
            payload=report_payload,
            version_tuple=VersionTuple(
                doc_version="journey-a-bench@1",
                tool_version="bench-report@2",
            ),
            payload_schema_id="bench-report@2",
        ).artifact_id
        engine = get_engine(self.database_url)
        try:
            with Session(engine) as session, session.begin():
                ref = SqlRefStore(
                    session,
                    cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=self.clock),
                    clock=self.clock,
                ).compare_and_set(REF_NAME, None, base.artifact_id)
        finally:
            engine.dispose()
        return base.artifact_id, constraints.artifact_id, ref.model_dump(mode="json")

    def _publish_artifact(
        self,
        *,
        kind: str,
        payload: bytes,
        version_tuple: VersionTuple,
        payload_schema_id: str,
        lineage: tuple[str, ...] = (),
    ):
        objects = self._object_store()
        stored = objects.put_verified(payload)
        artifact = build_artifact_v2(
            kind=kind,
            version_tuple=version_tuple,
            lineage=lineage,
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": payload_schema_id,
                "domain_scope": _DOMAIN_JSON,
                **({"schema_registry_version": "registry@1"} if kind == "ir_snapshot" else {}),
            },
            created_at="2026-07-19T00:00:00Z",
        )
        engine = get_engine(self.database_url)
        try:
            with Session(engine) as session, session.begin():
                bindings = SqlObjectBindingRepository(session, objects, OBJECT_STORE_ID)
                bindings.bind_verified(stored.ref, stored.location, None)
                SqlArtifactRepository(
                    session,
                    binding_repository=bindings,
                    cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=self.clock),
                    clock=self.clock,
                ).put(artifact)
        finally:
            engine.dispose()
        return artifact


def _execution_plan(
    *,
    kind: RunKindRef,
    catalog,
    routing,
) -> ExecutionVersionPlanV1:
    graph = next(
        graph
        for graph in build_builtin_registry().list_agent_execution_graphs()
        if graph.run_kind == kind and graph.status == "active"
    )
    model_id = canonical_model_snapshot_id(_MODEL_SNAPSHOT)
    body = {
        "agent_graph_version": graph.agent_graph_version,
        "nodes": tuple(
            PlannedAgentNodeVersionV1(
                agent_node_id=node.agent_node_id,
                prompt_version=node.prompt_version,
                tool_version=node.tool_version,
                allowed_model_snapshots=(model_id,),
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


def _seed_model_authority(harness: _Harness):
    authorities, catalog, routing = _model_authorities()
    transport = _JourneyTransport()
    authorities = replace(authorities, transport=transport)
    assert isinstance(authorities, WorkerModelExecutionAuthorities)
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            costs = SqlCostLedger(session, clock=harness.clock)
            costs.put_model_catalog(catalog)
            costs.put_routing_policy(routing)
    finally:
        engine.dispose()
    return authorities, transport, catalog, routing


def _generation_body(
    *,
    base_artifact_id: str,
    constraint_artifact_id: str,
    expected_ref: dict,
    plan: ExecutionVersionPlanV1,
    mode: str,
    cassette_artifact_id: str | None,
) -> dict:
    return {
        "request_schema_version": "generation-propose-request@1",
        "base_snapshot_artifact_id": base_artifact_id,
        "constraint_snapshot_artifact_id": constraint_artifact_id,
        "findings": [],
        "objective_goal_text": "Raise the caravan emblem requirement from three to four.",
        "domain_scope": _DOMAIN_JSON,
        "target": {"ref_name": REF_NAME, "expected_ref": expected_ref},
        "generation_policy": {"profile_id": "builtin.generation", "version": 1},
        "candidate_export_profiles": [{"profile_id": "builtin.config_export", "version": 1}],
        "llm_execution_mode": mode,
        "execution_version_plan": plan.model_dump(mode="json"),
        "cassette_artifact_id": cassette_artifact_id,
    }


def _review_body(
    *,
    snapshot_artifact_id: str,
    constraint_artifact_id: str | None,
    plan: ExecutionVersionPlanV1,
) -> dict:
    return {
        "request_schema_version": "run-submission-request@1",
        "params": {
            "schema_version": "review-run@1",
            "snapshot_artifact_id": snapshot_artifact_id,
            "constraint_snapshot_artifact_id": constraint_artifact_id,
            "selection": {"mode": "full", "entity_ids": [], "relation_ids": []},
            "review_profile": {"profile_id": "builtin.review", "version": 1},
            "checker_profiles": [],
            "simulation_profiles": [{"profile_id": "builtin.simulation", "version": 1}],
            "llm_triage_policy": {
                "profile_id": "builtin.llm_triage",
                "version": 1,
            },
        },
        "seed": 13,
        "execution_version_plan": plan.model_dump(mode="json"),
    }


def _task_suite_body(
    *,
    preview_artifact_id: str,
    config_artifact_id: str,
    constraint_artifact_id: str,
) -> dict:
    registry = build_builtin_registry().list_completion_oracle_registries()[0]
    return {
        "request_schema_version": "task-suite-derive-request@1",
        "params": {
            "schema_version": "task-suite-derive@1",
            "source_preview_artifact_id": preview_artifact_id,
            "config_artifact_id": config_artifact_id,
            "constraint_snapshot_artifact_id": constraint_artifact_id,
            "derivation_profile": {
                "profile_id": "builtin.task_suite_derivation",
                "version": 2,
            },
            "environment_profile": {
                "profile_id": "builtin.environment",
                "version": 1,
            },
            "completion_oracle_registry_ref": {
                "registry_version": registry.registry_version,
                "digest": registry.registry_digest,
            },
        },
    }


def _playtest_body(
    *,
    config_artifact_id: str,
    constraint_artifact_id: str,
    suite_artifact_id: str,
    episodes: list[dict],
    max_steps: int,
    plan: ExecutionVersionPlanV1,
) -> dict:
    return {
        "request_schema_version": "playtest-run-request@1",
        "params": {
            "schema_version": "playtest-run@1",
            "config_artifact_id": config_artifact_id,
            "constraint_snapshot_artifact_id": constraint_artifact_id,
            "task_suite_artifact_id": suite_artifact_id,
            "episodes": episodes,
            "environment_profile": {
                "profile_id": "builtin.environment",
                "version": 1,
            },
            "planner_policy": {
                "profile_id": "builtin.playtest_planner",
                "version": 2,
            },
            "max_steps_per_episode": max_steps,
            "interaction_mode": "autonomous",
        },
        "seed": 11,
        "execution_version_plan": plan.model_dump(mode="json"),
    }


def _finding_binding(revision: FindingRevisionV1, evidence_artifact_id: str) -> dict:
    return {
        "finding_id": revision.finding_id,
        "finding_revision": revision.revision,
        "evidence_artifact_id": evidence_artifact_id,
        "finding_digest": finding_revision_digest(revision),
    }


def _validation_body(
    item,
    *,
    base_artifact_id: str,
    constraint_artifact_id: str,
    config_artifact_id: str,
    expected_ref: dict,
    review_artifact_id: str | None,
    playtest_trace_artifact_id: str,
    regression_suite_artifact_id: str,
    findings: list[dict],
    expected_findings: list[dict],
) -> dict:
    return {
        "request_schema_version": "patch-validation-admission-request@1",
        "approval_id": item.approval_id,
        "expected_subject_head_revision": item.subject_revision,
        "expected_workflow_revision": item.workflow_revision,
        "subject_digest": item.subject_digest,
        "base_snapshot_artifact_id": base_artifact_id,
        "preview_snapshot_artifact_id": item.target_binding.target_artifact_id,
        "constraint_snapshot_artifact_id": constraint_artifact_id,
        "candidate_config_export_artifact_ids": [config_artifact_id],
        "target": {"ref_name": REF_NAME, "expected_ref": expected_ref},
        "validation_policy": {"profile_id": "builtin.validation", "version": 1},
        # This quest journey has no navigation Artifact and its empty DSL snapshot
        # gives the economy simulator no executable constraint authority. Exact
        # Playtest completion plus AgentEnv regression are the applicable oracles.
        "checker_profiles": [],
        "simulation_profiles": [],
        "expected_findings": expected_findings,
        "findings": findings,
        "review_artifact_ids": ([] if review_artifact_id is None else [review_artifact_id]),
        "playtest_trace_artifact_ids": [playtest_trace_artifact_id],
        "regression_suite_artifact_ids": [regression_suite_artifact_id],
        "seed": 17,
    }


def _repair_body(
    item,
    *,
    patch_artifact_id: str,
    base_artifact_id: str,
    constraint_artifact_id: str,
    expected_ref: dict,
    finding: dict,
    regression_suite_artifact_id: str,
    plan: ExecutionVersionPlanV1,
) -> dict:
    return {
        "request_schema_version": "patch-repair-request@1",
        "params": {
            "schema_version": "patch-repair@1",
            "subject_patch_artifact_id": patch_artifact_id,
            "expected_subject_head_revision": item.subject_revision,
            "expected_workflow_revision": item.workflow_revision,
            "base_snapshot_artifact_id": base_artifact_id,
            "preview_snapshot_artifact_id": item.target_binding.target_artifact_id,
            "constraint_snapshot_artifact_id": constraint_artifact_id,
            "validation_evidence_artifact_id": item.evidence_set_artifact_id,
            "findings": [finding],
            "target": {"ref_name": REF_NAME, "expected_ref": expected_ref},
            "repair_policy": {
                "profile_id": "builtin.patch_repair",
                "version": 1,
            },
            "checker_profiles": [],
            "simulation_profiles": [],
            "regression_suite_artifact_ids": [regression_suite_artifact_id],
            "candidate_export_profiles": [{"profile_id": "builtin.config_export", "version": 1}],
        },
        "seed": 19,
        "execution_version_plan": plan.model_dump(mode="json"),
    }


def _publish_regression_suite(
    harness: _Harness,
    *,
    base_artifact_id: str,
    base_version_tuple: VersionTuple,
    target: FindingRevisionV1,
) -> str:
    registry = build_builtin_registry()
    catalog = registry.list_execution_profile_catalogs()[-1]
    environment = registry.resolve_execution_profile(
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        field_path="/environment_profile",
        profile=ProfileRefV1(profile_id="builtin.environment", version=1),
        expected_profile_kind="environment",
    )
    oracle_registry = registry.list_completion_oracle_registries()[0]
    template = AgentEnvRegressionFindingTemplateV1(
        defect_class=target.payload.defect_class,
        severity=target.payload.severity,
        entities=tuple(target.payload.entities),
        relations=tuple(target.payload.relations),
        constraint_id=target.payload.constraint_id,
        evidence=target.payload.evidence,
        minimal_repro=target.payload.minimal_repro,
        message=target.payload.message,
    )
    # Aureus navigation advances one grid cell per action. The fixture keeps the
    # real caravan quest but places its two interaction targets one cell apart so
    # the same exact bounded replay can prove both the failed and repaired Runs.
    actions = (
        {"kind": "navigate_to", "target": "npc:lincheng"},
        {"kind": "interact", "target": "npc:lincheng"},
        {"kind": "navigate_to", "target": "interact:emblem_pile"},
        {"kind": "interact", "target": "interact:emblem_pile"},
        {"kind": "navigate_to", "target": "npc:lincheng"},
        {"kind": "interact", "target": "npc:lincheng"},
    )
    adapter_payload = AgentEnvRegressionPayloadV1(
        completion_oracle_registry_ref={
            "registry_version": oracle_registry.registry_version,
            "digest": oracle_registry.registry_digest,
        },
        cases=(
            AgentEnvRegressionCaseV1(
                case_id="case:caravan-completion",
                scenario_id="scenario:quest:missing_caravan",
                steps=tuple(AgentEnvRegressionStepV1(action=action) for action in actions),
                completion_oracle=ALL_QUESTS_COMPLETED_ORACLE,
                expected_completed=True,
                failure_finding=template,
            ),
        ),
    )
    dispatch = RegressionSuiteDispatchV1(
        adapter=AGENT_ENV_REPLAY_ADAPTER,
        environment_profile=environment,
        env_contract_version="generic-agent-env@1",
        adapter_payload=adapter_payload.model_dump(mode="json"),
    )
    artifact = harness._publish_artifact(
        kind="regression_suite",
        payload=canonical_json(dispatch.model_dump(mode="json")).encode("utf-8"),
        version_tuple=VersionTuple(
            doc_version=base_version_tuple.doc_version,
            ir_snapshot_id=base_version_tuple.ir_snapshot_id,
            constraint_snapshot_id=base_version_tuple.constraint_snapshot_id,
            env_contract_version="generic-agent-env@1",
            tool_version="journey-a-regression@1",
        ),
        payload_schema_id="regression-suite@1",
        lineage=(base_artifact_id,),
    )
    return artifact.artifact_id


def _run_result(session, run_view) -> dict:
    response = session.client.get(f"/api/v1/artifacts/{run_view.result_artifact_id}")
    assert response.status_code == 200, response.text
    return response.json()["payload"]


def _run_failure(session, run_view) -> dict:
    assert run_view.failure_artifact_id is not None
    response = session.client.get(f"/api/v1/artifacts/{run_view.failure_artifact_id}")
    assert response.status_code == 200, response.text
    return response.json()["payload"]


def _business_evidence_ids(failure: dict) -> tuple[str, ...]:
    return tuple(
        parent["artifact_id"]
        for parent in failure["version_projection"]["parents"]
        if parent["publication"] == "run_published" and parent["role"] == "evidence"
    )


def _approval_ids(session) -> set[str]:
    response = session.client.get("/api/v1/approvals", params={"limit": 100})
    assert response.status_code == 200, response.text
    return {item["approval"]["approval_id"] for item in response.json()["items"]}


def _artifact(session, artifact_id: str) -> dict:
    response = session.client.get(f"/api/v1/artifacts/{artifact_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _outputs(session, run_view) -> dict[str, list[tuple[str, dict]]]:
    result = _run_result(session, run_view)
    outputs: dict[str, list[tuple[str, dict]]] = {}
    output_ids = {
        parent["artifact_id"]
        for parent in result["version_projection"]["parents"]
        if parent["role"] == "output"
    }
    for artifact_id in sorted(output_ids):
        value = _artifact(session, artifact_id)
        outputs.setdefault(value["artifact"]["kind"], []).append((artifact_id, value["payload"]))
    return outputs


def _agent_draft_artifacts(session, run_view) -> tuple[str, str, str]:
    """Resolve Patch/preview/config from the exact public RunResult closure."""

    result = _run_result(session, run_view)
    output_ids = {
        parent["artifact_id"]
        for parent in result["version_projection"]["parents"]
        if parent["role"] == "output"
    }
    patch_id = result["primary_artifact_id"]
    assert patch_id in output_ids
    siblings = output_ids - {patch_id}
    preview_ids = []
    for artifact_id in siblings:
        response = session.client.get(f"/api/v1/specs/{artifact_id}")
        if response.status_code == 200:
            preview_ids.append(artifact_id)
        else:
            assert response.status_code in {403, 404}, response.text
    assert len(preview_ids) == 1
    config_ids = siblings - set(preview_ids)
    assert len(config_ids) == 1
    return patch_id, preview_ids[0], config_ids.pop()


def _submit(
    worker,
    session,
    *,
    path: str,
    body: dict,
    key: str,
    expected_status: str = "succeeded",
):
    accepted = session.client.post(
        path,
        json=body,
        headers=_headers(session, idempotency_key=key),
    )
    assert accepted.status_code == 202, accepted.text
    terminal = asyncio.run(_drive(worker.dispatcher, session, accepted.json()["run_id"]))
    failure = None
    if terminal.failure_artifact_id is not None:
        failure_response = session.client.get(f"/api/v1/artifacts/{terminal.failure_artifact_id}")
        failure = (
            failure_response.json().get("payload")
            if failure_response.status_code == 200
            else failure_response.json()
        )
    assert terminal.status == expected_status, failure
    return terminal


def _record_replay(
    worker,
    session,
    *,
    path: str,
    body: dict,
    key: str,
):
    record_body = {**body, "llm_execution_mode": "record", "cassette_artifact_id": None}
    record = _submit(
        worker,
        session,
        path=path,
        body=record_body,
        key=f"{key}:record",
    )
    assert record.terminal_cassette_artifact_id is not None
    replay_body = {
        **body,
        "llm_execution_mode": "replay",
        "cassette_artifact_id": record.terminal_cassette_artifact_id,
    }
    replay = _submit(
        worker,
        session,
        path=path,
        body=replay_body,
        key=f"{key}:replay",
    )
    return record, replay


def test_journey_a_authoring_happy_path_uses_native_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    base_id, constraint_id, expected_ref = harness.seed_authoring_inputs()
    authorities, transport, catalog, routing = _seed_model_authority(harness)
    plan = _execution_plan(
        kind=RunKindRef(kind="generation.propose", version=1),
        catalog=catalog,
        routing=routing,
    )
    api = _start_api(harness.api_config())
    worker = build_worker_process(harness.worker_config(), model_execution_authorities=authorities)
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        approver = _login(api, APPROVER_LOGIN, APPROVER_PASSWORD)

        profile_page = maker.client.get(
            "/api/v1/execution-profiles",
            params={"limit": 100},
        )
        assert profile_page.status_code == 200, profile_page.text
        active_profiles = {
            (item["profile"]["profile_id"], item["profile"]["version"])
            for item in profile_page.json()["items"]
            if item["status"] == "active"
        }
        assert {
            ("builtin.generation", 1),
            ("builtin.config_export", 1),
            ("builtin.review", 1),
            ("builtin.simulation", 1),
            ("builtin.llm_triage", 1),
            ("builtin.task_suite_derivation", 2),
            ("builtin.environment", 1),
            ("builtin.playtest_planner", 2),
            ("builtin.validation", 1),
            ("builtin.patch_repair", 1),
        }.issubset(active_profiles)
        constraint_page = maker.client.get(
            "/api/v1/constraints",
            params={"limit": 100},
        )
        assert constraint_page.status_code == 200, constraint_page.text
        assert [
            item["artifact"]["artifact_id"] for item in constraint_page.json()["items"]
        ] == [constraint_id]

        record = maker.client.post(
            "/api/v1/generation:propose",
            json=_generation_body(
                base_artifact_id=base_id,
                constraint_artifact_id=constraint_id,
                expected_ref=expected_ref,
                plan=plan,
                mode="record",
                cassette_artifact_id=None,
            ),
            headers=_headers(maker, idempotency_key="journey-a:generation:record"),
        )
        assert record.status_code == 202, record.text
        record_terminal = asyncio.run(_drive(worker.dispatcher, maker, record.json()["run_id"]))
        assert record_terminal.status == "succeeded"
        assert record_terminal.terminal_cassette_artifact_id is not None
        record_result = _run_result(maker, record_terminal)
        record_approval_id = f"approval:patch:{record_result['primary_artifact_id']}"
        assert _approval(maker, record_approval_id).status == "draft"
        assert transport.calls == ["generation"]

        replay = maker.client.post(
            "/api/v1/generation:propose",
            json=_generation_body(
                base_artifact_id=base_id,
                constraint_artifact_id=constraint_id,
                expected_ref=expected_ref,
                plan=plan,
                mode="replay",
                cassette_artifact_id=record_terminal.terminal_cassette_artifact_id,
            ),
            headers=_headers(maker, idempotency_key="journey-a:generation:replay"),
        )
        assert replay.status_code == 202, replay.text
        replay_terminal = asyncio.run(_drive(worker.dispatcher, maker, replay.json()["run_id"]))
        assert replay_terminal.status == "succeeded"
        replay_result = _run_result(maker, replay_terminal)
        replay_approval_id = f"approval:patch:{replay_result['primary_artifact_id']}"

        assert replay_terminal.terminal_cassette_artifact_id == (
            record_terminal.terminal_cassette_artifact_id
        )
        assert replay_result["primary_artifact_id"] != record_result["primary_artifact_id"]
        assert replay_approval_id != record_approval_id
        assert _approval(maker, replay_approval_id).status == "draft"
        assert transport.calls == ["generation"], "REPLAY called the provider transport"

        events = maker.client.get(f"/api/v1/runs/{replay_terminal.run_id}/events")
        assert events.status_code == 200, events.text
        assert "event:attempt.progress" in events.text
        assert "generation.preliminary_gate" in events.text

        generation_outputs = _outputs(maker, replay_terminal)
        patch_id = replay_result["primary_artifact_id"]
        preview_id = generation_outputs["ir_snapshot"][0][0]
        config_id = generation_outputs["config_export"][0][0]
        item = _approval(maker, replay_approval_id)

        review_plan = _execution_plan(
            kind=RunKindRef(kind="review.run", version=1),
            catalog=catalog,
            routing=routing,
        )
        transport.select("review")
        _review_record, review_replay = _record_replay(
            worker,
            maker,
            path="/api/v1/runs",
            body=_review_body(
                snapshot_artifact_id=preview_id,
                constraint_artifact_id=constraint_id,
                plan=review_plan,
            ),
            key="journey-a:review:failed-candidate",
        )
        review_id = _run_result(maker, review_replay)["primary_artifact_id"]
        review_artifact = _artifact(maker, review_id)
        assert review_artifact["artifact"]["kind"] == "review_report"
        assert isinstance(review_artifact["payload"]["deterministic_findings"], list)
        assert isinstance(review_artifact["payload"]["llm_assisted_findings"], list)
        review_binding = maker.client.get(
            f"/api/v1/reviews/{review_id}/producer-binding",
            params={"run_id": review_replay.run_id},
        )
        assert review_binding.status_code == 200, review_binding.text
        review_authority = review_binding.json()
        assert review_authority["run_kind"] == {"kind": "review.run", "version": 1}
        assert review_authority["manifest_role"] == "output"
        review_finding_count = sum(
            len(review_artifact["payload"][key])
            for key in (
                "deterministic_findings",
                "llm_assisted_findings",
                "simulation_findings",
                "unproven_findings",
            )
        )
        assert review_authority["finding_authority"] == (
            "exact-run-links" if review_finding_count else "not-applicable"
        )

        task_suite = _submit(
            worker,
            maker,
            path="/api/v1/task-suites:derive",
            body=_task_suite_body(
                preview_artifact_id=preview_id,
                config_artifact_id=config_id,
                constraint_artifact_id=constraint_id,
            ),
            key="journey-a:task-suite:failed-candidate",
        )
        suite_id = _run_result(maker, task_suite)["primary_artifact_id"]
        suite_payload = _artifact(maker, suite_id)["payload"]
        episodes = [
            {
                "episode_id": episode["episode_id"],
                "scenario_spec_artifact_id": episode["scenario_spec_artifact_id"],
            }
            for episode in suite_payload["episodes"]
        ]

        playtest_plan = _execution_plan(
            kind=RunKindRef(kind="playtest.run", version=1),
            catalog=catalog,
            routing=routing,
        )
        transport.select("playtest_fail")
        _playtest_record, playtest_replay = _record_replay(
            worker,
            maker,
            path="/api/v1/playtest:run",
            body=_playtest_body(
                config_artifact_id=config_id,
                constraint_artifact_id=constraint_id,
                suite_artifact_id=suite_id,
                episodes=episodes,
                max_steps=7,
                plan=playtest_plan,
            ),
            key="journey-a:playtest:failed-candidate",
        )
        playtest_outputs = _outputs(maker, playtest_replay)
        failed_trace_id, failed_trace = playtest_outputs["playtest_trace"][0]
        assert failed_trace["episodes"][0]["completed"] is False
        playtest_read = maker.client.get(f"/api/v1/playtest/{playtest_replay.run_id}/result")
        assert playtest_read.status_code == 200, playtest_read.text
        assert playtest_read.json()["artifact"]["artifact_id"] == failed_trace_id

        finding_page = maker.client.get(
            f"/api/v1/runs/{playtest_replay.run_id}/findings",
            params={"limit": 100},
        )
        assert finding_page.status_code == 200, finding_page.text
        assert len(finding_page.json()["items"]) == 1
        failed_finding = FindingRevisionV1.model_validate(finding_page.json()["items"][0])
        assert failed_finding.payload.defect_class == "playtest_incomplete"
        failed_binding = _finding_binding(failed_finding, failed_trace_id)

        base_version_tuple = VersionTuple.model_validate(
            _artifact(maker, base_id)["artifact"]["version_tuple"]
        )
        regression_suite_id = _publish_regression_suite(
            harness,
            base_artifact_id=base_id,
            base_version_tuple=base_version_tuple,
            target=failed_finding,
        )
        item = _approval(maker, replay_approval_id)
        validation = maker.client.post(
            f"/api/v1/patches/{patch_id}:validate",
            json=_validation_body(
                item,
                base_artifact_id=base_id,
                constraint_artifact_id=constraint_id,
                config_artifact_id=config_id,
                expected_ref=expected_ref,
                review_artifact_id=review_id,
                playtest_trace_artifact_id=failed_trace_id,
                regression_suite_artifact_id=regression_suite_id,
                findings=[failed_binding],
                expected_findings=[],
            ),
            headers=_headers(
                maker,
                idempotency_key="journey-a:validate:failed",
                resource_kind="patch",
                resource_id=patch_id,
                revision=item.workflow_revision,
            ),
        )
        assert validation.status_code == 202, validation.text
        failed_validation = asyncio.run(
            _drive(worker.dispatcher, maker, validation.json()["run_id"])
        )
        assert failed_validation.status == "succeeded"
        failed_item = _approval(maker, replay_approval_id)
        assert failed_item.status == "validation_failed"
        assert failed_item.evidence_set_artifact_id is not None
        assert _ref_history(maker) == (expected_ref,)

        blocked_submit = maker.client.post(
            f"/api/v1/patches/{patch_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": replay_approval_id,
                "expected_workflow_revision": failed_item.workflow_revision,
            },
            headers=_headers(
                maker,
                idempotency_key="journey-a:failed:submit",
                resource_kind="patch",
                resource_id=patch_id,
                revision=failed_item.workflow_revision,
            ),
        )
        assert blocked_submit.status_code == 409
        blocked_apply = approver.client.post(
            f"/api/v1/patches/{patch_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": replay_approval_id,
                "expected_workflow_revision": failed_item.workflow_revision,
                "subject_digest": failed_item.subject_digest,
                "target_artifact_id": failed_item.target_binding.target_artifact_id,
                "target_digest": failed_item.target_binding.target_digest,
                "ref_name": REF_NAME,
                "expected_ref": expected_ref,
            },
            headers=_headers(
                approver,
                idempotency_key="journey-a:failed:apply",
                resource_kind="patch",
                resource_id=patch_id,
                revision=failed_item.workflow_revision,
            ),
        )
        assert blocked_apply.status_code == 409
        assert _ref_history(maker) == (expected_ref,)

        repair_plan = _execution_plan(
            kind=RunKindRef(kind="patch.repair", version=1),
            catalog=catalog,
            routing=routing,
        )
        repair_body = _repair_body(
            failed_item,
            patch_artifact_id=patch_id,
            base_artifact_id=base_id,
            constraint_artifact_id=constraint_id,
            expected_ref=expected_ref,
            finding=failed_binding,
            regression_suite_artifact_id=regression_suite_id,
            plan=repair_plan,
        )

        approval_ids_before_unverified = _approval_ids(maker)
        transport.select("repair_unverified")
        unverified_record = _submit(
            worker,
            maker,
            path=f"/api/v1/patches/{patch_id}:repair",
            body={
                **repair_body,
                "llm_execution_mode": "record",
                "cassette_artifact_id": None,
            },
            key="journey-a:repair:unverified:record",
            expected_status="failed",
        )
        assert unverified_record.terminal_cassette_artifact_id is not None
        unverified_replay = _submit(
            worker,
            maker,
            path=f"/api/v1/patches/{patch_id}:repair",
            body={
                **repair_body,
                "llm_execution_mode": "replay",
                "cassette_artifact_id": unverified_record.terminal_cassette_artifact_id,
            },
            key="journey-a:repair:unverified:replay",
            expected_status="failed",
        )
        unverified_failure = _run_failure(maker, unverified_replay)
        assert unverified_failure["cause_code"] == "repair_unverified"
        unverified_evidence_kinds = {
            _artifact(maker, artifact_id)["artifact"]["kind"]
            for artifact_id in _business_evidence_ids(unverified_failure)
        }
        assert not {
            "patch",
            "ir_snapshot",
            "config_export",
        }.intersection(unverified_evidence_kinds)
        assert _approval_ids(maker) == approval_ids_before_unverified
        assert _approval(maker, replay_approval_id) == failed_item
        assert _ref_history(maker) == (expected_ref,)

        unverified_submit = maker.client.post(
            f"/api/v1/patches/{patch_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": replay_approval_id,
                "expected_workflow_revision": failed_item.workflow_revision,
            },
            headers=_headers(
                maker,
                idempotency_key="journey-a:repair:unverified:submit",
                resource_kind="patch",
                resource_id=patch_id,
                revision=failed_item.workflow_revision,
            ),
        )
        assert unverified_submit.status_code == 409
        unverified_apply = approver.client.post(
            f"/api/v1/patches/{patch_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": replay_approval_id,
                "expected_workflow_revision": failed_item.workflow_revision,
                "subject_digest": failed_item.subject_digest,
                "target_artifact_id": failed_item.target_binding.target_artifact_id,
                "target_digest": failed_item.target_binding.target_digest,
                "ref_name": REF_NAME,
                "expected_ref": expected_ref,
            },
            headers=_headers(
                approver,
                idempotency_key="journey-a:repair:unverified:apply",
                resource_kind="patch",
                resource_id=patch_id,
                revision=failed_item.workflow_revision,
            ),
        )
        assert unverified_apply.status_code == 409

        transport.select("repair")
        from gameforge.apps.worker.config_export import AureusConfigExporter

        original_export = AureusConfigExporter.export
        fail_record_export = True

        def terminal_record_fault(self, **kwargs):
            if fail_record_export and kwargs["llm_execution_mode"] == "record":
                raise RuntimeError("journey-a post-model bootstrap fault")
            return original_export(self, **kwargs)

        monkeypatch.setattr(AureusConfigExporter, "export", terminal_record_fault)
        repair_record = _submit(
            worker,
            maker,
            path=f"/api/v1/patches/{patch_id}:repair",
            body={
                **repair_body,
                "llm_execution_mode": "record",
                "cassette_artifact_id": None,
            },
            key="journey-a:repair:record",
            expected_status="failed",
        )
        assert repair_record.terminal_cassette_artifact_id is not None
        unchanged = _approval(maker, replay_approval_id)
        assert unchanged == failed_item
        assert _ref_history(maker) == (expected_ref,)

        fail_record_export = False
        repair_replay = _submit(
            worker,
            maker,
            path=f"/api/v1/patches/{patch_id}:repair",
            body={
                **repair_body,
                "llm_execution_mode": "replay",
                "cassette_artifact_id": repair_record.terminal_cassette_artifact_id,
            },
            key="journey-a:repair:replay",
        )
        repair_outputs = _outputs(maker, repair_replay)
        repaired_patch_id = _run_result(maker, repair_replay)["primary_artifact_id"]
        repaired_preview_id = repair_outputs["ir_snapshot"][0][0]
        repaired_config_id = repair_outputs["config_export"][0][0]
        repaired_approval_id = f"approval:patch:{repaired_patch_id}"
        repaired_item = _approval(maker, repaired_approval_id)
        assert repaired_item.status == "draft"
        assert repaired_item.supersedes_approval_id == replay_approval_id
        assert repaired_item.evidence_set_artifact_id is None
        assert repaired_item.regression_evidence_artifact_ids == ()
        assert repaired_item.decisions == ()
        superseded_item = _approval(maker, replay_approval_id)
        assert superseded_item.status == "superseded"
        assert superseded_item.evidence_set_artifact_id == (failed_item.evidence_set_artifact_id)

        transport.select("review")
        _fixed_review_record, fixed_review_replay = _record_replay(
            worker,
            maker,
            path="/api/v1/runs",
            body=_review_body(
                snapshot_artifact_id=repaired_preview_id,
                constraint_artifact_id=constraint_id,
                plan=review_plan,
            ),
            key="journey-a:review:repaired",
        )
        fixed_review_id = _run_result(maker, fixed_review_replay)["primary_artifact_id"]

        stale_suite = maker.client.post(
            "/api/v1/playtest:run",
            json={
                **_playtest_body(
                    config_artifact_id=repaired_config_id,
                    constraint_artifact_id=constraint_id,
                    suite_artifact_id=suite_id,
                    episodes=episodes,
                    max_steps=7,
                    plan=playtest_plan,
                ),
                "llm_execution_mode": "replay",
                "cassette_artifact_id": (_playtest_record.terminal_cassette_artifact_id),
            },
            headers=_headers(
                maker,
                idempotency_key="journey-a:stale-task-suite",
            ),
        )
        assert stale_suite.status_code == 409, stale_suite.text
        assert stale_suite.json()["code"] == "stale_task_suite"

        fixed_suite = _submit(
            worker,
            maker,
            path="/api/v1/task-suites:derive",
            body=_task_suite_body(
                preview_artifact_id=repaired_preview_id,
                config_artifact_id=repaired_config_id,
                constraint_artifact_id=constraint_id,
            ),
            key="journey-a:task-suite:repaired",
        )
        fixed_suite_id = _run_result(maker, fixed_suite)["primary_artifact_id"]
        fixed_suite_payload = _artifact(maker, fixed_suite_id)["payload"]
        fixed_episodes = [
            {
                "episode_id": episode["episode_id"],
                "scenario_spec_artifact_id": episode["scenario_spec_artifact_id"],
            }
            for episode in fixed_suite_payload["episodes"]
        ]
        transport.select("playtest_pass")
        _fixed_playtest_record, fixed_playtest_replay = _record_replay(
            worker,
            maker,
            path="/api/v1/playtest:run",
            body=_playtest_body(
                config_artifact_id=repaired_config_id,
                constraint_artifact_id=constraint_id,
                suite_artifact_id=fixed_suite_id,
                episodes=fixed_episodes,
                max_steps=7,
                plan=playtest_plan,
            ),
            key="journey-a:playtest:repaired",
        )
        fixed_trace_id, fixed_trace = _outputs(maker, fixed_playtest_replay)["playtest_trace"][0]
        assert all(episode["completed"] for episode in fixed_trace["episodes"])
        fixed_playtest_read = maker.client.get(
            f"/api/v1/playtest/{fixed_playtest_replay.run_id}/result"
        )
        assert fixed_playtest_read.status_code == 200, fixed_playtest_read.text
        assert fixed_playtest_read.json()["artifact"]["artifact_id"] == fixed_trace_id

        repaired_item = _approval(maker, repaired_approval_id)
        repaired_validation = maker.client.post(
            f"/api/v1/patches/{repaired_patch_id}:validate",
            json=_validation_body(
                repaired_item,
                base_artifact_id=base_id,
                constraint_artifact_id=constraint_id,
                config_artifact_id=repaired_config_id,
                expected_ref=expected_ref,
                review_artifact_id=fixed_review_id,
                playtest_trace_artifact_id=fixed_trace_id,
                regression_suite_artifact_id=regression_suite_id,
                findings=[],
                expected_findings=[failed_binding],
            ),
            headers=_headers(
                maker,
                idempotency_key="journey-a:validate:repaired",
                resource_kind="patch",
                resource_id=repaired_patch_id,
                revision=repaired_item.workflow_revision,
            ),
        )
        assert repaired_validation.status_code == 202, repaired_validation.text
        repaired_validation_terminal = asyncio.run(
            _drive(
                worker.dispatcher,
                maker,
                repaired_validation.json()["run_id"],
            )
        )
        assert repaired_validation_terminal.status == "succeeded"
        validated = _approval(maker, repaired_approval_id)
        validation_evidence = _artifact(
            maker,
            validated.evidence_set_artifact_id,
        )["payload"]
        assert validated.status == "validated", (
            validation_evidence["overall_status"],
            tuple(
                (
                    requirement["requirement_id"],
                    requirement["status"],
                    requirement.get("reason_code"),
                )
                for requirement in validation_evidence["requirements"]
                if requirement["status"] != "passed"
            ),
        )
        assert validated.evidence_set_artifact_id != failed_item.evidence_set_artifact_id
        assert _approval(maker, replay_approval_id).status == "superseded"

        submit = maker.client.post(
            f"/api/v1/patches/{repaired_patch_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": repaired_approval_id,
                "expected_workflow_revision": validated.workflow_revision,
            },
            headers=_headers(
                maker,
                idempotency_key="journey-a:submit",
                resource_kind="patch",
                resource_id=repaired_patch_id,
                revision=validated.workflow_revision,
            ),
        )
        assert submit.status_code == 200, submit.text
        pending = _approval(maker, repaired_approval_id)
        assert pending.status == "pending_approval"
        approve = approver.client.post(
            f"/api/v1/approvals/{repaired_approval_id}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": [
                    requirement.requirement_id for requirement in pending.requirements
                ],
                "expected_workflow_revision": pending.workflow_revision,
                "reason_code": "journey_a_independent_review",
            },
            headers=_headers(
                approver,
                idempotency_key="journey-a:approve",
                resource_kind="approval",
                resource_id=repaired_approval_id,
                revision=pending.workflow_revision,
            ),
        )
        assert approve.status_code == 200, approve.text
        approved = _approval(approver, repaired_approval_id)
        assert approved.status == "approved"
        assert approved.target_binding.target_artifact_id == repaired_preview_id
        assert _ref_history(maker) == (expected_ref,)
        apply = approver.client.post(
            f"/api/v1/patches/{repaired_patch_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": repaired_approval_id,
                "expected_workflow_revision": approved.workflow_revision,
                "subject_digest": approved.subject_digest,
                "target_artifact_id": approved.target_binding.target_artifact_id,
                "target_digest": approved.target_binding.target_digest,
                "ref_name": REF_NAME,
                "expected_ref": expected_ref,
            },
            headers=_headers(
                approver,
                idempotency_key="journey-a:apply",
                resource_kind="patch",
                resource_id=repaired_patch_id,
                revision=approved.workflow_revision,
            ),
        )
        assert apply.status_code == 200, apply.text
        assert _approval(approver, repaired_approval_id).status == "applied"
        assert apply.json()["ref_value"]["artifact_id"] == repaired_preview_id

        traces = maker.client.get(
            f"/api/v1/runs/{repair_replay.run_id}/traces", params={"limit": 100}
        )
        assert traces.status_code == 200, traces.text
        assert traces.json()["items"]
        worker_trace = next(
            item for item in traces.json()["items"] if "gameforge-worker" in item["service_names"]
        )
        trace_id = worker_trace["trace_id"]
        spans = maker.client.get(
            f"/api/v1/traces/{trace_id}/spans",
            params={"limit": 100},
        )
        assert spans.status_code == 200, spans.text
        attempt_span = next(
            item["span"]
            for item in spans.json()["items"]
            if item["span"]["name"] == "worker.attempt"
            and item["span"]["attributes"].get("run_id") == repair_replay.run_id
        )
        assert attempt_span["duration_ns"] >= 0

        now = datetime.now(UTC)
        logs = maker.client.get(
            "/api/v1/logs/query",
            params={
                "start_utc": (now - timedelta(minutes=5)).isoformat(),
                "end_utc": (now + timedelta(minutes=5)).isoformat(),
                "services": "gameforge-worker",
                "event_names": "worker.attempt.started",
                "run_id": repair_replay.run_id,
                "limit": 100,
            },
        )
        assert logs.status_code == 200, logs.text
        attempt_log = next(item["record"] for item in logs.json()["items"])
        assert attempt_log["trace_id"] == trace_id
        assert attempt_log["span_id"] == attempt_span["span_id"]

        cost = maker.client.get(f"/api/v1/cost/{repair_replay.run_id}")
        assert cost.status_code == 200, cost.text
        assert cost.json()["run_id"] == repair_replay.run_id
        bench = maker.client.get("/api/v1/bench/report")
        assert bench.status_code == 200, bench.text
        bench_report = bench.json()
        assert bench_report["schema_version"] == "bench-report@2"
        assert bench_report["qa"]["time_scoring"] == "incorrect_uses_active_cap"
        assert bench_report["qa"]["conclusion"] == "savings"
        assert all(
            bench_report["qa"][name]["status"] == "measured"
            and bench_report["qa"][name]["evaluated_n"] == 4
            for name in (
                "paired_saved_minutes",
                "paired_saved_fraction",
                "manual_success",
                "assisted_success",
            )
        )
        assert bench_report["qa"]["evidence_ref"] == "qa"
        qa_evidence = next(item for item in bench_report["evidence"] if item["evidence_id"] == "qa")
        assert qa_evidence["available"] is True
    finally:
        worker.close()
        _stop_api(api)


def test_journey_a_gate_rejected_replay_is_evidence_only(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    base_id, constraint_id, expected_ref = harness.seed_authoring_inputs()
    authorities, transport, catalog, routing = _seed_model_authority(harness)
    plan = _execution_plan(
        kind=RunKindRef(kind="generation.propose", version=1),
        catalog=catalog,
        routing=routing,
    )
    api = _start_api(harness.api_config())
    worker = build_worker_process(harness.worker_config(), model_execution_authorities=authorities)
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        approver = _login(api, APPROVER_LOGIN, APPROVER_PASSWORD)
        transport.select("generation_reject")
        body = _generation_body(
            base_artifact_id=base_id,
            constraint_artifact_id=constraint_id,
            expected_ref=expected_ref,
            plan=plan,
            mode="record",
            cassette_artifact_id=None,
        )
        record = _submit(
            worker,
            maker,
            path="/api/v1/generation:propose",
            body=body,
            key="journey-a:gate-rejected:record",
            expected_status="failed",
        )
        assert record.terminal_cassette_artifact_id is not None

        replay = _submit(
            worker,
            maker,
            path="/api/v1/generation:propose",
            body={
                **body,
                "llm_execution_mode": "replay",
                "cassette_artifact_id": record.terminal_cassette_artifact_id,
            },
            key="journey-a:gate-rejected:replay",
            expected_status="failed",
        )
        failure = _run_failure(maker, replay)
        assert failure["cause_code"] == "generation_gate_rejected"
        assert failure["retryable"] is False
        assert not [
            parent
            for parent in failure["version_projection"]["parents"]
            if parent["role"] == "output"
        ]

        business_evidence_ids = _business_evidence_ids(failure)
        assert set(business_evidence_ids) < set(failure["evidence_artifact_ids"])
        evidence = {
            artifact_id: _artifact(maker, artifact_id) for artifact_id in business_evidence_ids
        }
        kinds = {value["artifact"]["kind"] for value in evidence.values()}
        assert {"patch", "ir_snapshot", "checker_run", "review_report"}.issubset(kinds)
        assert "config_export" not in kinds
        rejected_review_id = next(
            artifact_id
            for artifact_id, value in evidence.items()
            if value["artifact"]["kind"] == "review_report"
        )
        rejected_review_binding = maker.client.get(
            f"/api/v1/reviews/{rejected_review_id}/producer-binding",
            params={"run_id": replay.run_id},
        )
        assert rejected_review_binding.status_code == 200, rejected_review_binding.text
        rejected_review_authority = rejected_review_binding.json()
        assert rejected_review_authority["terminal_manifest_kind"] == "run_failure"
        assert rejected_review_authority["outcome_policy_id"] == "generation-gate-rejected"
        assert rejected_review_authority["outcome_rule_id"] == "review"
        assert rejected_review_authority["manifest_role"] == "evidence"
        rejected_patch_id = next(
            artifact_id
            for artifact_id, value in evidence.items()
            if value["artifact"]["kind"] == "patch"
        )
        rejected_preview_id = next(
            artifact_id
            for artifact_id, value in evidence.items()
            if value["artifact"]["kind"] == "ir_snapshot"
        )
        rejected_approval_id = f"approval:patch:{rejected_patch_id}"
        assert maker.client.get(f"/api/v1/approvals/{rejected_approval_id}").status_code == 404
        assert _approval_ids(maker) == set()
        assert _ref_history(maker) == (expected_ref,)

        submit = maker.client.post(
            f"/api/v1/patches/{rejected_patch_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": rejected_approval_id,
                "expected_workflow_revision": 1,
            },
            headers=_headers(
                maker,
                idempotency_key="journey-a:gate-rejected:submit",
                resource_kind="patch",
                resource_id=rejected_patch_id,
                revision=1,
            ),
        )
        assert submit.status_code in {404, 409}

        apply = approver.client.post(
            f"/api/v1/patches/{rejected_patch_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": rejected_approval_id,
                "expected_workflow_revision": 1,
                "subject_digest": "0" * 64,
                "target_artifact_id": rejected_preview_id,
                "target_digest": "1" * 64,
                "ref_name": REF_NAME,
                "expected_ref": expected_ref,
            },
            headers=_headers(
                approver,
                idempotency_key="journey-a:gate-rejected:apply",
                resource_kind="patch",
                resource_id=rejected_patch_id,
                revision=1,
            ),
        )
        assert apply.status_code in {404, 409}
        assert _ref_history(maker) == (expected_ref,)
        assert transport.calls == ["generation"]
    finally:
        worker.close()
        _stop_api(api)
