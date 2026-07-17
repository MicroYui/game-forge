"""Task 11 handler -> real terminal-publication lineage regressions.

These tests deliberately start with the real Agent handlers' ``PreparedRunResult``
and then drive it through ``TerminalPublisher.plan_run_result`` -> stage -> commit.
The workflow-effect call is neutralized because workflow CAS has its own repository
integration suite; Artifact allocation, typed lineage, VersionTuple projection,
runtime identity, manifest construction, staging, and repository writes remain real.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    RunIntermediateArtifactLinkV1,
    canonical_payload_hash,
    execution_version_plan_digest,
    outcome_policy_set_digest,
    run_kind_definition_digest,
)
from gameforge.contracts.lineage import (
    ArtifactV1,
    InvocationVersionBindingV1,
    VersionTuple,
    build_artifact_v2,
    build_execution_identity,
)
from gameforge.contracts.model_router import ModelSnapshot, request_hash
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.platform.publication import TerminalPublisher
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.runs.lifecycle import select_outcome_policy
from tests.platform.m4c.handler_support import FakeModelBridge
from tests.platform.m4c.test_constraint_proposal_handler import (
    DOC_ID,
    GOAL_ID,
    _PROPOSALS,
    _context as constraint_context,
    _handler as constraint_handler,
    _store as constraint_store,
)
from tests.platform.m4c.test_repair_handler import (
    BASE_ID,
    BASE_SNAPSHOT_ID,
    CONSTRAINT_ID,
    EVIDENCE_ID,
    FINDING_EVIDENCE_ID,
    PREVIEW_ID,
    PREVIEW_SNAPSHOT_ID,
    SUBJECT_ID,
    SUITE_ID,
    _EnvironmentBoundPassingRegressionRunner,
    _FIX_OPS,
    _context as repair_context,
    _handler as repair_handler,
    _payload as repair_payload,
    _store as repair_store,
)
from tests.platform.m4c.test_terminal_publisher import (
    NOW,
    WORKER,
    _Artifacts,
    _Audit,
    _Blobs,
    _DirectPublisherHarness,
    _Findings,
)
from tests.platform.m4c.test_terminal_runtime_identity import _RuntimeLedger


_MODEL = ModelSnapshot(
    provider="anthropic",
    model="claude-opus-4-8",
    snapshot_tag="m2a@1",
)
_MODEL_ID = canonical_model_snapshot_id(_MODEL)


def _live_context(context, *, prompt_version: str):
    """Make the handler fixture use one exact LIVE plan instead of a fake cassette."""

    old_plan = context.payload.execution_version_plan
    assert old_plan is not None and len(old_plan.nodes) == 1
    node = old_plan.nodes[0].model_copy(
        update={
            "prompt_version": prompt_version,
            "allowed_model_snapshots": (_MODEL_ID,),
        }
    )
    body = old_plan.model_dump(mode="json", exclude={"plan_digest"})
    body["nodes"] = [node.model_dump(mode="json")]
    plan = ExecutionVersionPlanV1(
        **body,
        plan_digest=execution_version_plan_digest(body),
    )
    envelope = context.payload.model_copy(
        update={
            "input_artifact_ids": tuple(
                artifact_id
                for artifact_id in context.payload.input_artifact_ids
                if artifact_id != context.payload.cassette_artifact_id
            ),
            "execution_version_plan": plan,
            "llm_execution_mode": "live",
            "cassette_artifact_id": None,
        }
    )
    run = context.run.model_copy(
        update={
            "payload": envelope,
            "payload_hash": canonical_payload_hash(envelope),
        }
    )
    return replace(context, run=run, payload=envelope)


def _add_input(
    artifacts: _Artifacts,
    *,
    artifact_id: str,
    kind: str,
    schema: str,
    version_tuple: VersionTuple,
    payload: bytes,
) -> None:
    artifacts.add(
        ArtifactV1(
            artifact_id=artifact_id,
            kind=kind,
            version_tuple=version_tuple,
            lineage=[],
            payload_hash=sha256_lowerhex(payload),
            meta={"payload_schema_id": schema},
        )
    )
    artifacts.payloads_by_id[artifact_id] = bytes(payload)


def _runtime_authority(*, run, attempt, bridge, artifacts: _Artifacts, blobs: _Blobs):
    """Persist exact rendered-prompt/route/consumption authority for LIVE calls."""

    plan = run.payload.execution_version_plan
    assert plan is not None
    nodes = {node.agent_node_id: node for node in plan.nodes}
    links = []
    bindings = []
    decisions = {}
    for call_ordinal, bridge_request in enumerate(bridge.requests, start=1):
        model_request = bridge_request.model_request
        node = nodes[model_request.agent_node_id]
        exact_hash = request_hash(model_request)
        raw_hash = exact_hash.removeprefix("sha256:")
        payload = canonical_json(model_request.model_dump(mode="json")).encode("utf-8")
        object_ref = blobs.register(payload)
        rendered = build_artifact_v2(
            kind="source_rendered",
            version_tuple=VersionTuple(
                prompt_version=model_request.prompt_version,
                agent_graph_version=plan.agent_graph_version,
                tool_version="renderer@1",
            ),
            lineage=(),
            payload_hash=object_ref.sha256,
            object_ref=object_ref,
            meta={
                "payload_schema_id": "source-rendered@1",
                "renderer_version": "renderer@1",
                "agent_tool_version": node.tool_version,
                "producer_run_id": run.run_id,
                "producer_attempt_no": attempt.attempt_no,
                "logical_call_ordinal": call_ordinal,
                "route_ordinal": bridge_request.route_ordinal,
            },
            created_at=NOW,
        )
        artifacts.add(rendered)
        links.append(
            RunIntermediateArtifactLinkV1(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                call_ordinal=call_ordinal,
                route_ordinal=bridge_request.route_ordinal,
                artifact_id=rendered.artifact_id,
                role="prompt_rendered",
                request_hash=raw_hash,
                fencing_token=attempt.fencing_token,
                published_at=NOW,
            )
        )
        decision = RoutingDecisionV1.create(
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            request_hash=exact_hash,
            rule_id=f"task11-agent:{model_request.agent_node_id}",
            model_snapshot=node.allowed_model_snapshots[0],
            tier="best",
            reason_code="primary_rule",
            budget_set_snapshot_id=run.payload.budget_set_snapshot_id,
            fallback_from=None,
            fallback_index=0,
            policy_version=plan.routing_policy_version,
            routing_policy_digest=plan.routing_policy_digest,
            catalog_version=plan.model_catalog_version,
            catalog_digest=plan.model_catalog_digest,
            execution_source="online",
            decided_at=datetime(2026, 7, 18, tzinfo=UTC),
        )
        decisions[decision.decision_id] = decision
        bindings.append(
            InvocationVersionBindingV1(
                attempt_no=attempt.attempt_no,
                call_ordinal=call_ordinal,
                route_ordinal=bridge_request.route_ordinal,
                transport_attempt=1,
                routing_decision_kind="native",
                routing_decision_id=decision.decision_id,
                agent_node_id=model_request.agent_node_id,
                prompt_version=model_request.prompt_version,
                model_snapshot=node.allowed_model_snapshots[0],
                tool_version=node.tool_version,
                execution_source="online",
                response_consumed=True,
            )
        )
    identity = build_execution_identity(
        scope="run",
        bindings=tuple(bindings),
        agent_graph_version=plan.agent_graph_version,
    )
    retained_attempt = attempt.model_copy(update={"next_call_ordinal": len(links) + 1})
    return (
        retained_attempt,
        _RuntimeLedger(
            prompts=tuple(links),
            run_identity=identity,
            attempts={retained_attempt.attempt_no: retained_attempt},
            routing_decisions=decisions,
        ),
        tuple(link.artifact_id for link in links),
    )


def _publish_handler_result(
    monkeypatch,
    *,
    context,
    outcome,
    handler_store,
    bridge,
    inputs,
    config_exporter=None,
):
    from gameforge.platform.publication import publisher as publisher_module

    registry = build_builtin_registry()
    definition = registry.get_run_kind(context.run.kind)
    assert definition is not None
    retry = registry.get_retry_policy(definition.retry_policy)
    assert retry is not None
    run = context.run.model_copy(
        update={
            "run_kind_definition_digest": run_kind_definition_digest(definition),
            "outcome_policy_set_digest": outcome_policy_set_digest(
                context.run.kind,
                definition.outcome_policies,
            ),
            "failure_classifier": definition.failure_classifier,
            "retry_policy": definition.retry_policy,
            "max_attempts": retry.max_attempts,
        }
    )
    artifacts, blobs = _Artifacts(), _Blobs()
    artifacts._blobs = blobs
    for item in inputs:
        _add_input(artifacts, **item)
    assert set(run.payload.input_artifact_ids) == {item["artifact_id"] for item in inputs}
    for prepared in outcome.artifacts:
        blobs._by_key[prepared.object_ref.key] = handler_store.read_prepared(prepared.object_ref)
        blobs._locations[prepared.object_ref.key] = prepared.location

    attempt, ledger, prompt_ids = _runtime_authority(
        run=run,
        attempt=context.attempt,
        bridge=bridge,
        artifacts=artifacts,
        blobs=blobs,
    )
    policy = select_outcome_policy(
        definition=definition,
        outcome_code=outcome.summary.outcome_code,
        prepared_outcome="success",
        publication_scope="run",
        run_status="succeeded",
        attempt_status=None,
        failure_class=None,
        retry_disposition=None,
    )
    monkeypatch.setattr(publisher_module, "apply_workflow_effect", lambda *_a, **_kw: None)
    terminal = _DirectPublisherHarness(
        TerminalPublisher(
            registry=registry,
            artifacts=artifacts,
            blobs=blobs,
            findings=_Findings(),
            ledger=ledger,
            audit=_Audit(),
            config_exporter=config_exporter,
        ),
        blobs,
    )
    publication = terminal.publish_run_result(
        run=run,
        attempt=attempt,
        prepared=outcome,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    manifest = artifacts.by_id[publication.result_artifact_id]
    result = json.loads(blobs.read(manifest.object_ref))
    return artifacts, result, prompt_ids


def test_constraint_handler_result_reaches_terminal_with_exact_source_and_goal_roles(
    monkeypatch,
) -> None:
    store = constraint_store()
    bridge = FakeModelBridge(
        responses=(_PROPOSALS,),
        model_snapshots={_MODEL_ID: _MODEL},
    )
    context = _live_context(constraint_context(bridge), prompt_version="extraction@1")
    outcome = constraint_handler(store)(context)

    artifacts, result, prompt_ids = _publish_handler_result(
        monkeypatch,
        context=context,
        outcome=outcome,
        handler_store=store,
        bridge=bridge,
        inputs=(
            {
                "artifact_id": DOC_ID,
                "kind": "source_raw",
                "schema": "source-raw@1",
                "version_tuple": VersionTuple(doc_version="v1"),
                "payload": store.read_bytes(DOC_ID),
            },
            {
                "artifact_id": GOAL_ID,
                "kind": "source_raw",
                "schema": "source-raw@1",
                # Deliberately different: goal is not a design-document source.
                "version_tuple": VersionTuple(doc_version="goal-v9"),
                "payload": store.read_bytes(GOAL_ID),
            },
        ),
    )

    proposal = artifacts.by_id[result["primary_artifact_id"]]
    assert proposal.kind == "constraint_proposal"
    assert proposal.version_tuple.doc_version == "v1"
    assert {DOC_ID, GOAL_ID, *prompt_ids}.issubset(set(proposal.lineage))
    assert proposal.meta["execution_identity"].scope == "artifact"


def test_repair_handler_result_reaches_terminal_with_exact_base_preview_and_evidence_roles(
    monkeypatch,
) -> None:
    store = repair_store()
    bridge = FakeModelBridge(
        responses=(_FIX_OPS,),
        model_snapshots={_MODEL_ID: _MODEL},
    )
    payload = repair_payload().model_copy(update={"candidate_export_profiles": ()})
    context = _live_context(
        repair_context(bridge, payload=payload),
        prompt_version="repair@4",
    )
    outcome = repair_handler(
        store,
        regression_runner=_EnvironmentBoundPassingRegressionRunner(),
    )(context)

    artifacts, result, prompt_ids = _publish_handler_result(
        monkeypatch,
        context=context,
        outcome=outcome,
        handler_store=store,
        bridge=bridge,
        inputs=(
            {
                "artifact_id": BASE_ID,
                "kind": "ir_snapshot",
                "schema": "ir-core@1",
                "version_tuple": VersionTuple(
                    doc_version="base-doc@1",
                    ir_snapshot_id=BASE_SNAPSHOT_ID,
                ),
                "payload": store.read_bytes(BASE_ID),
            },
            {
                "artifact_id": PREVIEW_ID,
                "kind": "ir_snapshot",
                "schema": "ir-core@1",
                "version_tuple": VersionTuple(
                    doc_version="base-doc@1",
                    ir_snapshot_id=PREVIEW_SNAPSHOT_ID,
                ),
                "payload": store.read_bytes(PREVIEW_ID),
            },
            {
                "artifact_id": CONSTRAINT_ID,
                "kind": "constraint_snapshot",
                "schema": "constraint-snapshot@1",
                "version_tuple": VersionTuple(constraint_snapshot_id="constraint:semantic:1"),
                "payload": store.read_bytes(CONSTRAINT_ID),
            },
            {
                "artifact_id": SUBJECT_ID,
                "kind": "patch",
                "schema": "patch@2",
                "version_tuple": VersionTuple(
                    doc_version="base-doc@1",
                    ir_snapshot_id=BASE_SNAPSHOT_ID,
                    constraint_snapshot_id="constraint:semantic:1",
                ),
                "payload": store.read_bytes(SUBJECT_ID),
            },
            {
                "artifact_id": EVIDENCE_ID,
                "kind": "validation_evidence",
                "schema": "evidence-set@1",
                "version_tuple": VersionTuple(
                    doc_version="base-doc@1",
                    ir_snapshot_id=PREVIEW_SNAPSHOT_ID,
                    constraint_snapshot_id="constraint:semantic:1",
                ),
                "payload": store.read_bytes(EVIDENCE_ID),
            },
            {
                "artifact_id": FINDING_EVIDENCE_ID,
                "kind": "checker_run",
                "schema": "checker-report@1",
                "version_tuple": VersionTuple(
                    ir_snapshot_id=PREVIEW_SNAPSHOT_ID,
                    constraint_snapshot_id="constraint:semantic:1",
                ),
                "payload": b"{}",
            },
            {
                "artifact_id": SUITE_ID,
                "kind": "regression_suite",
                "schema": "regression-suite@1",
                "version_tuple": VersionTuple(env_contract_version="generic-agent-env@1"),
                "payload": store.read_bytes(SUITE_ID),
            },
        ),
    )

    patch = artifacts.by_id[result["primary_artifact_id"]]
    assert patch.kind == "patch"
    assert patch.version_tuple.doc_version == "base-doc@1"
    assert patch.version_tuple.ir_snapshot_id == BASE_SNAPSHOT_ID
    assert patch.version_tuple.constraint_snapshot_id == "constraint:semantic:1"
    assert {
        BASE_ID,
        PREVIEW_ID,
        SUBJECT_ID,
        EVIDENCE_ID,
        FINDING_EVIDENCE_ID,
        CONSTRAINT_ID,
        *prompt_ids,
    }.issubset(set(patch.lineage))
    assert patch.meta["execution_identity"].scope == "artifact"
