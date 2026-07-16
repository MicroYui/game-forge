"""Task 9 terminal ExecutionIdentity/cassette authority closure."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cassette import CassetteRecordV2
from gameforge.contracts.cassette_import import CassetteBundleV1
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    RunAttempt,
    RunIntermediateArtifactLinkV1,
    RunPayloadEnvelope,
    canonical_payload_hash,
    execution_version_plan_digest,
)
from gameforge.contracts.lineage import (
    InvocationVersionBindingV1,
    VersionTuple,
    build_artifact_v2,
    build_execution_identity,
)
from gameforge.contracts.model_router import Message, ModelRequestV2, ModelSnapshot, request_hash
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.platform.publication import TerminalPublisher
from gameforge.platform.runs.lifecycle import select_outcome_policy
from tests.platform.m4.test_run_create_claim import _payload
from tests.platform.m4c.test_terminal_publisher import (
    NOW,
    WORKER,
    _Artifacts,
    _Audit,
    _Blobs,
    _checker_artifact,
    _DirectPublisherHarness,
    _Findings,
    _attempt,
    _execution_failure,
    _input_snapshot,
    _prepared_success,
    _registry_and_definition,
    _run_record,
    _success_policy,
    _terminal_decision,
)


MODEL_DESCRIPTOR = ModelSnapshot(provider="openai", model="gpt-test", snapshot_tag="runtime-1")
MODEL = canonical_model_snapshot_id(MODEL_DESCRIPTOR)
NODE = "checker-agent"
PROMPT = "checker-prompt@1"
TOOL = "checker-agent@1"
GRAPH = "checker-graph@1"


def _plan() -> ExecutionVersionPlanV1:
    node = PlannedAgentNodeVersionV1(
        agent_node_id=NODE,
        prompt_version=PROMPT,
        tool_version=TOOL,
        allowed_model_snapshots=(MODEL,),
    )
    body = {
        "plan_schema_version": "execution-version-plan@1",
        "agent_graph_version": GRAPH,
        "nodes": [node.model_dump(mode="json")],
        "model_catalog_version": 1,
        "model_catalog_digest": "a" * 64,
        "routing_policy_version": 1,
        "routing_policy_digest": "b" * 64,
    }
    return ExecutionVersionPlanV1(
        **body,
        plan_digest=execution_version_plan_digest(body),
    )


class _RuntimeArtifacts(_Artifacts):
    def __init__(self, blobs: _Blobs) -> None:
        super().__init__()
        self._blobs = blobs

    def read_bytes(self, artifact_id: str) -> bytes:
        artifact = self.by_id[artifact_id]
        return self._blobs.read(artifact.object_ref)


@dataclass
class _RuntimeLedger:
    prompts: tuple[RunIntermediateArtifactLinkV1, ...] = ()
    attempt_identity: object | None = None
    run_identity: object | None = None
    shards: tuple[tuple[int, int, str], ...] = ()
    attempt_bundle_id: str | None = None
    run_bundle_id: str | None = None
    replay_id: str | None = None
    attempts: dict[int, RunAttempt] = field(default_factory=dict)
    routing_decisions: dict[str, RoutingDecisionV1] = field(default_factory=dict)
    links: list[object] = field(default_factory=list)
    alternate_attempt_bundle_id: str | None = None
    alternate_run_bundle_id: str | None = None
    attempt_bundle_reads: int = 0
    run_bundle_reads: int = 0

    def prompt_links(self, run_id: str, *, attempt_no: int | None):
        assert run_id == "run:1"
        if attempt_no is None:
            return self.prompts
        return tuple(link for link in self.prompts if link.attempt_no == attempt_no)

    def closed_attempt_failures(self, run_id: str):
        assert run_id == "run:1"
        return ()

    def put_finding_link(self, link):
        self.links.append(link)
        return link

    def execution_identity(self, run_id: str, *, attempt_no: int | None):
        assert run_id == "run:1"
        identity = self.run_identity if attempt_no is None else self.attempt_identity
        if identity is None:
            raise AssertionError("identity must not be requested for not_applicable")
        return identity

    def get_attempt(self, run_id: str, attempt_no: int):
        assert run_id == "run:1"
        return self.attempts.get(attempt_no)

    def get_routing_decision(self, decision_id: str):
        return self.routing_decisions.get(decision_id)

    def record_shard_links(self, run_id: str, *, attempt_no: int | None):
        assert run_id == "run:1"
        if attempt_no is None:
            return self.shards
        return tuple(row for row in self.shards if row[0] == attempt_no)

    def attempt_cassette_bundle(self, run_id: str, *, attempt_no: int):
        assert run_id == "run:1" and attempt_no == 1
        self.attempt_bundle_reads += 1
        if self.attempt_bundle_reads > 1 and self.alternate_attempt_bundle_id is not None:
            return self.alternate_attempt_bundle_id
        return self.attempt_bundle_id

    def run_cassette_bundle(self, run_id: str):
        assert run_id == "run:1"
        self.run_bundle_reads += 1
        if self.run_bundle_reads > 1 and self.alternate_run_bundle_id is not None:
            return self.alternate_run_bundle_id
        return self.run_bundle_id

    def replay_input_cassette(self, run_id: str):
        assert run_id == "run:1"
        return self.replay_id


def _binding(
    *, source: str, prompt: str = PROMPT, decision_id: str = "decision:1"
) -> InvocationVersionBindingV1:
    return InvocationVersionBindingV1(
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        transport_attempt=None if source != "online" else 1,
        routing_decision_kind="native",
        routing_decision_id=decision_id,
        agent_node_id=NODE,
        prompt_version=prompt,
        model_snapshot=MODEL,
        tool_version=TOOL,
        execution_source=source,
        response_consumed=True,
    )


def _routing_decision(
    *,
    request_hash_value: str,
    source: str = "online",
    reason_code: str = "primary_rule",
) -> RoutingDecisionV1:
    plan = _plan()
    return RoutingDecisionV1.create(
        run_id="run:1",
        attempt_no=1,
        request_hash=f"sha256:{request_hash_value}",
        rule_id="checker-default",
        model_snapshot=MODEL,
        tier="best",
        reason_code=reason_code,
        budget_set_snapshot_id="budget-set:1",
        fallback_from=None,
        fallback_index=0,
        policy_version=plan.routing_policy_version,
        routing_policy_digest=plan.routing_policy_digest,
        catalog_version=plan.model_catalog_version,
        catalog_digest=plan.model_catalog_digest,
        execution_source=source,
        decided_at=datetime(2026, 7, 16, tzinfo=UTC),
    )


def _source_rendered(artifacts: _RuntimeArtifacts, blobs: _Blobs, *, kind="source_rendered"):
    payload = canonical_json({"request": "exact"}).encode()
    object_ref = blobs.register(payload)
    artifact = build_artifact_v2(
        kind=kind,
        version_tuple=VersionTuple(
            prompt_version=PROMPT,
            model_snapshot=MODEL,
            agent_graph_version=GRAPH,
            tool_version="renderer@1",
        ),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"payload_schema_id": "source-rendered@1"},
        created_at=NOW,
    )
    artifacts.add(artifact)
    return artifact


def _bundle(
    artifacts: _RuntimeArtifacts,
    blobs: _Blobs,
    *,
    payload: CassetteBundleV1,
    identity,
    lineage: tuple[str, ...] | None = None,
):
    encoded = canonical_json(payload.model_dump(mode="json")).encode()
    object_ref = blobs.register(encoded)
    artifact = build_artifact_v2(
        kind="cassette_bundle",
        version_tuple=VersionTuple(
            prompt_version=identity.prompt_projection.tuple_value,
            model_snapshot=identity.model_projection.tuple_value,
            agent_graph_version=identity.agent_graph_version,
            tool_version="cassette@1",
            cassette_id=f"sha256:{object_ref.sha256}",
        ),
        lineage=payload.child_bundle_artifact_ids if lineage is None else lineage,
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={
            "payload_schema_id": (
                "cassette-record-shard@1"
                if payload.scope == "record_shard"
                else "cassette-bundle@1"
            ),
            "execution_identity": identity,
            "replayability": "cassette_replay",
        },
        created_at=NOW,
    )
    artifacts.add(artifact)
    return artifact


def _native_record(
    *,
    run_id: str = "run:1",
    message: str = "exact rendered request",
) -> tuple[ModelRequestV2, CassetteRecordV2, InvocationVersionBindingV1]:
    plan = _plan()
    rendered = ModelRequestV2(
        model_snapshot=MODEL_DESCRIPTOR,
        messages=[Message(role="user", content=message)],
        params={},
        tool_schemas=[],
        agent_node_id=NODE,
        prompt_version=PROMPT,
    )
    rendered_hash = request_hash(rendered)
    decision = RoutingDecisionV1.create(
        run_id=run_id,
        attempt_no=1,
        request_hash=rendered_hash,
        rule_id="checker-default",
        model_snapshot=MODEL,
        tier="best",
        reason_code="primary_rule",
        budget_set_snapshot_id="budget-set:1",
        fallback_from=None,
        fallback_index=0,
        policy_version=plan.routing_policy_version,
        routing_policy_digest=plan.routing_policy_digest,
        catalog_version=plan.model_catalog_version,
        catalog_digest=plan.model_catalog_digest,
        execution_source="online",
        decided_at=datetime(2026, 7, 16, tzinfo=UTC),
    )
    record = CassetteRecordV2(
        request_hash=rendered_hash,
        agent_node_id=NODE,
        model_snapshot=MODEL_DESCRIPTOR,
        routing_decision=decision,
        response_normalized="checked",
        raw_response={"id": "response:1", "content": "checked"},
        latency=LatencyObservationV1(status="reported", provider_latency_ms=10),
        token_usage=TokenUsageObservationV1(
            status="reported",
            input_tokens=3,
            output_tokens=1,
            total_tokens=4,
        ),
        provider_prefix_cache=CacheHitObservationV1(status="reported", hit=False),
        finish_reason="stop",
        tool_calls=(),
        transport_attempt_count=1,
        transport_retry_count=0,
        recorded_at=datetime(2026, 7, 16, tzinfo=UTC),
    )
    binding = InvocationVersionBindingV1(
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        transport_attempt=1,
        routing_decision_kind="native",
        routing_decision_id=decision.decision_id,
        agent_node_id=NODE,
        prompt_version=PROMPT,
        model_snapshot=MODEL,
        tool_version=TOOL,
        execution_source="online",
        response_consumed=True,
    )
    return rendered, record, binding


def _publish_record_attempt(
    *,
    another_record: bool = False,
    wrong_prompt_lineage: bool = False,
    wrong_prompt_fence: bool = False,
    retained_decision: str = "exact",
):
    registry, definition = _registry_and_definition()
    blobs = _Blobs()
    artifacts = _RuntimeArtifacts(blobs)
    run = _mode_run(definition, mode="record")
    rendered, record, binding = _native_record()
    authoritative_decision = record.routing_decision
    rendered_bytes = canonical_json(rendered.model_dump(mode="json")).encode()
    rendered_ref = blobs.register(rendered_bytes)
    prompt = build_artifact_v2(
        kind="source_rendered",
        version_tuple=VersionTuple(
            prompt_version=PROMPT,
            model_snapshot=MODEL,
            agent_graph_version=GRAPH,
            tool_version="renderer@1",
        ),
        lineage=(),
        payload_hash=rendered_ref.sha256,
        object_ref=rendered_ref,
        meta={"payload_schema_id": "source-rendered@1"},
        created_at=NOW,
    )
    artifacts.add(prompt)
    prompt_link = RunIntermediateArtifactLinkV1(
        run_id=run.run_id,
        attempt_no=1,
        call_ordinal=1,
        artifact_id=prompt.artifact_id,
        role="prompt_rendered",
        request_hash=request_hash(rendered).removeprefix("sha256:"),
        fencing_token=2 if wrong_prompt_fence else 1,
        published_at=NOW,
    )
    if another_record:
        _, record, _ = _native_record(message="record from another rendered request")
    shard_identity = build_execution_identity(
        scope="record_shard", bindings=(binding,), agent_graph_version=GRAPH
    )
    attempt_identity = build_execution_identity(
        scope="attempt", bindings=(binding,), agent_graph_version=GRAPH
    )
    shard = _bundle(
        artifacts,
        blobs,
        payload=CassetteBundleV1(
            scope="record_shard",
            run_id=run.run_id,
            attempt_no=1,
            ordinal=1,
            records=(record,),
        ),
        identity=shard_identity,
        lineage=() if wrong_prompt_lineage else (prompt.artifact_id,),
    )
    attempt_bundle = _bundle(
        artifacts,
        blobs,
        payload=CassetteBundleV1(
            scope="attempt",
            run_id=run.run_id,
            attempt_no=1,
            child_bundle_artifact_ids=(shard.artifact_id,),
        ),
        identity=attempt_identity,
    )
    terminal_attempt = _attempt().model_copy(update={"next_call_ordinal": 2})
    if retained_decision == "exact":
        routing_decisions = {binding.routing_decision_id: authoritative_decision}
    elif retained_decision == "absent":
        routing_decisions = {}
    elif retained_decision == "substituted":
        _, substituted, _ = _native_record(message="substituted retained route")
        routing_decisions = {binding.routing_decision_id: substituted.routing_decision}
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(f"unknown retained decision fixture: {retained_decision}")
    ledger = _RuntimeLedger(
        prompts=(prompt_link,),
        attempt_identity=attempt_identity,
        shards=((1, 1, shard.artifact_id),),
        attempt_bundle_id=attempt_bundle.artifact_id,
        attempts={1: terminal_attempt},
        routing_decisions=routing_decisions,
    )
    publisher = _DirectPublisherHarness(
        TerminalPublisher(
            registry=registry,
            artifacts=artifacts,
            blobs=blobs,
            findings=_Findings(),
            ledger=ledger,
            audit=_Audit(),
        ),
        blobs,
    )
    policy = select_outcome_policy(
        definition=definition,
        outcome_code="execution_failed",
        prepared_outcome="failure",
        publication_scope="attempt",
        run_status="failed",
        attempt_status="failed",
        failure_class="execution",
        retry_disposition="terminal",
    )
    return publisher.publish_attempt_failure(
        run=run,
        attempt=terminal_attempt,
        prepared=_execution_failure(definition),
        retry_decision=_terminal_decision(definition),
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )


def _mode_run(definition, *, mode: str, cassette=None):
    base = _payload()
    plan = None if mode == "not_applicable" else _plan()
    version_tuple = base.version_tuple
    input_ids = base.input_artifact_ids
    if plan is not None:
        version_tuple = version_tuple.model_copy(
            update={
                "prompt_version": PROMPT,
                "model_snapshot": MODEL,
                "agent_graph_version": GRAPH,
                "cassette_id": (None if cassette is None else cassette.version_tuple.cassette_id),
            }
        )
    if cassette is not None:
        input_ids = tuple(sorted((*input_ids, cassette.artifact_id)))
    payload = RunPayloadEnvelope(
        **base.model_dump(
            mode="python",
            exclude={
                "input_artifact_ids",
                "version_tuple",
                "execution_version_plan",
                "llm_execution_mode",
                "cassette_artifact_id",
            },
        ),
        input_artifact_ids=input_ids,
        version_tuple=version_tuple,
        execution_version_plan=plan,
        llm_execution_mode=mode,
        cassette_artifact_id=(None if cassette is None else cassette.artifact_id),
    )
    return _run_record(definition).model_copy(
        update={"payload": payload, "payload_hash": canonical_payload_hash(payload)}
    )


def _record_run_authority():
    """Build exact attempt + run RECORD authority for run-scope terminal tests."""

    registry, definition = _registry_and_definition()
    blobs = _Blobs()
    artifacts = _RuntimeArtifacts(blobs)
    _input_snapshot(artifacts)
    run = _mode_run(definition, mode="record")
    rendered, record, binding = _native_record()
    rendered_bytes = canonical_json(rendered.model_dump(mode="json")).encode()
    rendered_ref = blobs.register(rendered_bytes)
    prompt = build_artifact_v2(
        kind="source_rendered",
        version_tuple=VersionTuple(
            prompt_version=PROMPT,
            model_snapshot=MODEL,
            agent_graph_version=GRAPH,
            tool_version="renderer@1",
        ),
        lineage=(),
        payload_hash=rendered_ref.sha256,
        object_ref=rendered_ref,
        meta={"payload_schema_id": "source-rendered@1"},
        created_at=NOW,
    )
    artifacts.add(prompt)
    prompt_link = RunIntermediateArtifactLinkV1(
        run_id=run.run_id,
        attempt_no=1,
        call_ordinal=1,
        artifact_id=prompt.artifact_id,
        role="prompt_rendered",
        request_hash=request_hash(rendered).removeprefix("sha256:"),
        fencing_token=1,
        published_at=NOW,
    )
    shard_identity = build_execution_identity(
        scope="record_shard",
        bindings=(binding,),
        agent_graph_version=GRAPH,
    )
    attempt_identity = build_execution_identity(
        scope="attempt",
        bindings=(binding,),
        agent_graph_version=GRAPH,
    )
    run_identity = build_execution_identity(
        scope="run",
        bindings=(binding,),
        agent_graph_version=GRAPH,
    )
    shard = _bundle(
        artifacts,
        blobs,
        payload=CassetteBundleV1(
            scope="record_shard",
            run_id=run.run_id,
            attempt_no=1,
            ordinal=1,
            records=(record,),
        ),
        identity=shard_identity,
        lineage=(prompt.artifact_id,),
    )
    attempt_bundle = _bundle(
        artifacts,
        blobs,
        payload=CassetteBundleV1(
            scope="attempt",
            run_id=run.run_id,
            attempt_no=1,
            child_bundle_artifact_ids=(shard.artifact_id,),
        ),
        identity=attempt_identity,
    )
    run_bundle = _bundle(
        artifacts,
        blobs,
        payload=CassetteBundleV1(
            scope="run",
            run_id=run.run_id,
            child_bundle_artifact_ids=(attempt_bundle.artifact_id,),
        ),
        identity=run_identity,
    )
    terminal_attempt = _attempt().model_copy(update={"next_call_ordinal": 2})
    ledger = _RuntimeLedger(
        prompts=(prompt_link,),
        attempt_identity=attempt_identity,
        run_identity=run_identity,
        shards=((1, 1, shard.artifact_id),),
        attempt_bundle_id=attempt_bundle.artifact_id,
        run_bundle_id=run_bundle.artifact_id,
        attempts={1: terminal_attempt},
        routing_decisions={binding.routing_decision_id: record.routing_decision},
    )
    publisher = TerminalPublisher(
        registry=registry,
        artifacts=artifacts,
        blobs=blobs,
        findings=_Findings(),
        ledger=ledger,
        audit=_Audit(),
    )
    return (
        definition,
        run,
        terminal_attempt,
        artifacts,
        blobs,
        publisher,
        _DirectPublisherHarness(publisher, blobs),
        attempt_bundle,
        run_bundle,
    )


def _draft_manifest(draft, artifact_id: str):
    return next(
        operation.artifact
        for operation in draft.operations
        if getattr(operation, "artifact", None) is not None
        and operation.artifact.artifact_id == artifact_id
    )


def _publish_attempt(
    *,
    mode: str,
    forged_prompt: str | None = None,
    wrong_kind: bool = False,
    zero_live_call: bool = False,
    tampered_replay_child: bool = False,
    replay_source_call: bool = False,
    replay_omit_shard: bool = False,
    replay_terminal_consumed: bool = False,
    record_prompt_without_response: bool = False,
    retained_decision: str = "exact",
    flip_attempt_bundle_on_second_read: bool = False,
    include_context: bool = False,
):
    registry, definition = _registry_and_definition()
    blobs = _Blobs()
    artifacts = _RuntimeArtifacts(blobs)
    source_identity = build_execution_identity(scope="run", bindings=(), agent_graph_version=GRAPH)
    replay_record: CassetteRecordV2 | None = None
    replay_source_binding: InvocationVersionBindingV1 | None = None
    replay = None
    if mode == "replay":
        if tampered_replay_child and replay_source_call:
            raise AssertionError("replay fixture modes are mutually exclusive")
        children: tuple[str, ...] = ()
        if replay_source_call:
            _, replay_record, replay_source_binding = _native_record(run_id="source:run")
            shard = _bundle(
                artifacts,
                blobs,
                payload=CassetteBundleV1(
                    scope="record_shard",
                    run_id="source:run",
                    attempt_no=1,
                    ordinal=1,
                    records=(replay_record,),
                ),
                identity=build_execution_identity(
                    scope="record_shard",
                    bindings=(replay_source_binding,),
                    agent_graph_version=GRAPH,
                ),
            )
            child = _bundle(
                artifacts,
                blobs,
                payload=CassetteBundleV1(
                    scope="attempt",
                    run_id="source:run",
                    attempt_no=1,
                    child_bundle_artifact_ids=(() if replay_omit_shard else (shard.artifact_id,)),
                ),
                identity=build_execution_identity(
                    scope="attempt",
                    bindings=(replay_source_binding,),
                    agent_graph_version=GRAPH,
                ),
            )
            children = (child.artifact_id,)
            source_identity = build_execution_identity(
                scope="run",
                bindings=(replay_source_binding,),
                agent_graph_version=GRAPH,
            )
        elif tampered_replay_child:
            child = _bundle(
                artifacts,
                blobs,
                payload=CassetteBundleV1(
                    scope="attempt",
                    run_id="source:run",
                    attempt_no=1,
                ),
                identity=build_execution_identity(
                    scope="attempt",
                    bindings=(),
                    agent_graph_version="forged-graph@1",
                ),
            )
            children = (child.artifact_id,)
        replay = _bundle(
            artifacts,
            blobs,
            payload=CassetteBundleV1(
                scope="run",
                run_id="source:run",
                child_bundle_artifact_ids=children,
            ),
            identity=source_identity,
        )
    run = _mode_run(definition, mode=mode, cassette=replay)
    prompt_links: tuple[RunIntermediateArtifactLinkV1, ...] = ()
    bindings: tuple[InvocationVersionBindingV1, ...] = ()
    routing_decisions: dict[str, RoutingDecisionV1] = {}
    if mode == "live" and not zero_live_call:
        source = _source_rendered(
            artifacts,
            blobs,
            kind="checker_run" if wrong_kind else "source_rendered",
        )
        prompt_links = (
            RunIntermediateArtifactLinkV1(
                run_id=run.run_id,
                attempt_no=1,
                call_ordinal=1,
                artifact_id=source.artifact_id,
                role="prompt_rendered",
                request_hash="c" * 64,
                fencing_token=1,
                published_at=NOW,
            ),
        )
        decision = _routing_decision(request_hash_value="c" * 64)
        bindings = (
            _binding(
                source="online",
                prompt=forged_prompt or PROMPT,
                decision_id=decision.decision_id,
            ),
        )
        if retained_decision == "exact":
            routing_decisions[decision.decision_id] = decision
        elif retained_decision == "substituted":
            routing_decisions[decision.decision_id] = _routing_decision(
                request_hash_value="c" * 64,
                reason_code="substituted_route",
            )
        elif retained_decision != "absent":  # pragma: no cover - helper misuse
            raise AssertionError(f"unknown retained decision fixture: {retained_decision}")
    elif mode == "record" and record_prompt_without_response:
        source = _source_rendered(artifacts, blobs)
        prompt_links = (
            RunIntermediateArtifactLinkV1(
                run_id=run.run_id,
                attempt_no=1,
                call_ordinal=1,
                artifact_id=source.artifact_id,
                role="prompt_rendered",
                request_hash="d" * 64,
                fencing_token=1,
                published_at=NOW,
            ),
        )
    elif mode == "replay" and replay_source_call:
        assert replay_record is not None and replay_source_binding is not None
        source = _source_rendered(artifacts, blobs)
        prompt_links = (
            RunIntermediateArtifactLinkV1(
                run_id=run.run_id,
                attempt_no=1,
                call_ordinal=1,
                artifact_id=source.artifact_id,
                role="prompt_rendered",
                request_hash=replay_record.request_hash.removeprefix("sha256:"),
                fencing_token=1,
                published_at=NOW,
            ),
        )
        if replay_terminal_consumed:
            replay_decision = _routing_decision(
                request_hash_value=replay_record.request_hash.removeprefix("sha256:"),
                source="cassette_replay",
            )
            bindings = (
                replay_source_binding.model_copy(
                    update={
                        "transport_attempt": None,
                        "routing_decision_id": replay_decision.decision_id,
                        "execution_source": "cassette_replay",
                    }
                ),
            )
            routing_decisions[replay_decision.decision_id] = replay_decision
    attempt_identity = (
        None
        if mode == "not_applicable"
        else build_execution_identity(scope="attempt", bindings=bindings, agent_graph_version=GRAPH)
    )
    attempt_bundle = (
        _bundle(
            artifacts,
            blobs,
            payload=CassetteBundleV1(scope="attempt", run_id=run.run_id, attempt_no=1),
            identity=attempt_identity,
        )
        if mode == "record"
        else None
    )
    terminal_attempt = _attempt().model_copy(update={"next_call_ordinal": 2 if prompt_links else 1})
    ledger = _RuntimeLedger(
        prompts=prompt_links,
        attempt_identity=attempt_identity,
        attempt_bundle_id=(None if attempt_bundle is None else attempt_bundle.artifact_id),
        replay_id=(None if replay is None else replay.artifact_id),
        attempts=({1: terminal_attempt} if prompt_links else {}),
        routing_decisions=routing_decisions,
        alternate_attempt_bundle_id=(
            "artifact:forged-second-read" if flip_attempt_bundle_on_second_read else None
        ),
    )
    publisher = _DirectPublisherHarness(
        TerminalPublisher(
            registry=registry,
            artifacts=artifacts,
            blobs=blobs,
            findings=_Findings(),
            ledger=ledger,
            audit=_Audit(),
        ),
        blobs,
    )
    policy = select_outcome_policy(
        definition=definition,
        outcome_code="execution_failed",
        prepared_outcome="failure",
        publication_scope="attempt",
        run_status="failed",
        attempt_status="failed",
        failure_class="execution",
        retry_disposition="terminal",
    )
    publication = publisher.publish_attempt_failure(
        run=run,
        attempt=terminal_attempt,
        prepared=_execution_failure(definition),
        retry_decision=_terminal_decision(definition),
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    result = (
        artifacts.by_id[publication.failure_artifact_id],
        attempt_identity,
        attempt_bundle,
        replay,
    )
    if include_context:
        return (*result, publication, ledger)
    return result


@pytest.mark.parametrize(
    ("mode", "replayability"),
    (
        ("not_applicable", "deterministic_recompute"),
        ("live", "online_only"),
        ("record", "cassette_replay"),
        ("replay", "cassette_replay"),
    ),
)
def test_four_modes_publish_exact_identity_transition_and_replayability(mode, replayability):
    manifest, identity, attempt_bundle, replay = _publish_attempt(mode=mode)
    assert manifest.meta["replayability"] == replayability
    assert manifest.meta.get("execution_identity") == identity
    if mode == "record":
        assert manifest.version_tuple.cassette_id == attempt_bundle.version_tuple.cassette_id
        assert manifest.version_tuple.prompt_version is None
        assert manifest.version_tuple.agent_graph_version == GRAPH
    elif mode == "replay":
        assert manifest.version_tuple.cassette_id == replay.version_tuple.cassette_id
    elif mode == "live":
        assert manifest.version_tuple.prompt_version == PROMPT
        assert manifest.version_tuple.model_snapshot == MODEL
    else:
        assert manifest.version_tuple.prompt_version is None
        assert manifest.version_tuple.agent_graph_version is None


def test_record_run_success_projects_only_the_run_bundle_as_cassette_parent():
    (
        definition,
        run,
        attempt,
        artifacts,
        blobs,
        publisher,
        harness,
        attempt_bundle,
        run_bundle,
    ) = _record_run_authority()
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))

    draft = publisher.plan_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=_success_policy(definition),
        occurred_at=NOW,
        actor=WORKER,
    )

    result = draft.result
    assert result.attempt_cassette_artifact_id == attempt_bundle.artifact_id
    assert result.terminal_cassette_artifact_id == run_bundle.artifact_id
    manifest = _draft_manifest(draft, result.result_artifact_id)
    assert run_bundle.artifact_id in manifest.lineage
    assert attempt_bundle.artifact_id not in manifest.lineage
    assert manifest.version_tuple.cassette_id == run_bundle.version_tuple.cassette_id

    committed = publisher.commit(draft, harness._stage(draft))  # noqa: SLF001
    assert committed == result
    assert result.result_artifact_id in artifacts.by_id


def test_record_run_failure_aggregate_projects_attempt_then_run_bundle_by_scope():
    (
        definition,
        run,
        attempt,
        artifacts,
        _,
        publisher,
        harness,
        attempt_bundle,
        run_bundle,
    ) = _record_run_authority()
    attempt_policy = select_outcome_policy(
        definition=definition,
        outcome_code="execution_failed",
        prepared_outcome="failure",
        publication_scope="attempt",
        run_status="failed",
        attempt_status="failed",
        failure_class="execution",
        retry_disposition="terminal",
    )
    run_policy = select_outcome_policy(
        definition=definition,
        outcome_code="execution_failed",
        prepared_outcome="failure",
        publication_scope="run",
        run_status="failed",
        attempt_status="failed",
        failure_class="execution",
        retry_disposition="terminal",
    )

    drafts = publisher.plan_active_failure_aggregate(
        run=run,
        attempt=attempt,
        prepared=_execution_failure(definition),
        retry_decision=_terminal_decision(definition),
        attempt_policy=attempt_policy,
        run_policy=run_policy,
        occurred_at=NOW,
        actor=WORKER,
    )

    attempt_result, run_result = (draft.result for draft in drafts)
    assert attempt_result.cassette_bundle_artifact_id == attempt_bundle.artifact_id
    assert run_result.terminal_cassette_artifact_id == run_bundle.artifact_id
    attempt_manifest = _draft_manifest(drafts[0], attempt_result.failure_artifact_id)
    run_manifest = _draft_manifest(drafts[1], run_result.failure_artifact_id)
    assert attempt_bundle.artifact_id in attempt_manifest.lineage
    assert run_bundle.artifact_id in run_manifest.lineage
    assert attempt_bundle.artifact_id not in run_manifest.lineage
    assert attempt_result.failure_artifact_id in run_manifest.lineage

    staged = tuple(harness._stage(draft) for draft in drafts)  # noqa: SLF001
    committed = publisher.commit_many(tuple(zip(drafts, staged, strict=True)))
    assert committed == (attempt_result, run_result)
    assert run_result.failure_artifact_id in artifacts.by_id


def test_identity_outside_frozen_plan_fails_closed():
    with pytest.raises(IntegrityViolation, match="node/prompt/model/tool"):
        _publish_attempt(mode="live", forged_prompt="forged-prompt@1")


@pytest.mark.parametrize("retained_decision", ("absent", "substituted"))
def test_live_identity_requires_exact_retained_routing_decision(retained_decision):
    with pytest.raises(IntegrityViolation, match="retained RoutingDecision authority"):
        _publish_attempt(mode="live", retained_decision=retained_decision)


def test_live_zero_call_keeps_graph_but_clears_prompt_and_model():
    manifest, identity, _, _ = _publish_attempt(mode="live", zero_live_call=True)
    assert identity.bindings == ()
    assert manifest.version_tuple.prompt_version is None
    assert manifest.version_tuple.model_snapshot is None
    assert manifest.version_tuple.agent_graph_version == GRAPH


def test_runtime_parent_kind_is_reread_not_trusted_from_prompt_link():
    with pytest.raises(IntegrityViolation, match="runtime parent matched no unique"):
        _publish_attempt(mode="live", wrong_kind=True)


def test_replay_ledger_must_equal_payload_cassette():
    registry, definition = _registry_and_definition()
    blobs = _Blobs()
    artifacts = _RuntimeArtifacts(blobs)
    identity = build_execution_identity(scope="run", bindings=(), agent_graph_version=GRAPH)
    replay = _bundle(
        artifacts,
        blobs,
        payload=CassetteBundleV1(scope="run", run_id="source:run"),
        identity=identity,
    )
    run = _mode_run(definition, mode="replay", cassette=replay)
    ledger = _RuntimeLedger(
        attempt_identity=build_execution_identity(
            scope="attempt", bindings=(), agent_graph_version=GRAPH
        ),
        replay_id="artifact:wrong",
    )
    publisher = _DirectPublisherHarness(
        TerminalPublisher(
            registry=registry,
            artifacts=artifacts,
            blobs=blobs,
            findings=_Findings(),
            ledger=ledger,
            audit=_Audit(),
        ),
        blobs,
    )
    policy = select_outcome_policy(
        definition=definition,
        outcome_code="execution_failed",
        prepared_outcome="failure",
        publication_scope="attempt",
        run_status="failed",
        attempt_status="failed",
        failure_class="execution",
        retry_disposition="terminal",
    )
    with pytest.raises(IntegrityViolation, match="ledger cassette differs"):
        publisher.publish_attempt_failure(
            run=run,
            attempt=_attempt(),
            prepared=_execution_failure(definition),
            retry_decision=_terminal_decision(definition),
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )


def test_replay_child_identity_must_equal_root_subset():
    with pytest.raises(IntegrityViolation, match="attempt cassette identity"):
        _publish_attempt(mode="replay", tampered_replay_child=True)


def test_replay_failure_can_close_after_prompt_before_response_consumption():
    manifest, identity, _, replay = _publish_attempt(
        mode="replay",
        replay_source_call=True,
        replay_terminal_consumed=False,
    )
    assert identity.bindings == ()
    assert replay.artifact_id in manifest.lineage


def test_replay_root_tree_must_not_omit_a_consumed_source_shard():
    with pytest.raises(IntegrityViolation, match="record-shard tree"):
        _publish_attempt(
            mode="replay",
            replay_source_call=True,
            replay_omit_shard=True,
        )


def test_replay_consumed_prefix_closes_against_source_tree():
    manifest, identity, _, replay = _publish_attempt(
        mode="replay",
        replay_source_call=True,
        replay_terminal_consumed=True,
    )
    assert len(identity.bindings) == 1 and identity.bindings[0].response_consumed
    assert replay.artifact_id in manifest.lineage


def test_record_prompt_without_provider_response_closes_empty_bundle_once():
    manifest, identity, bundle, _, publication, ledger = _publish_attempt(
        mode="record",
        record_prompt_without_response=True,
        flip_attempt_bundle_on_second_read=True,
        include_context=True,
    )
    assert identity.bindings == ()
    assert bundle.lineage == ()
    assert publication.cassette_bundle_artifact_id == bundle.artifact_id
    assert manifest.version_tuple.cassette_id == bundle.version_tuple.cassette_id
    assert ledger.attempt_bundle_reads == 1


def test_record_shard_rejects_record_from_another_rendered_request():
    with pytest.raises(IntegrityViolation, match="rendered request differs"):
        _publish_record_attempt(another_record=True)


def test_record_shard_closes_exact_record_request_route_transport_and_lineage():
    publication = _publish_record_attempt()
    assert publication.failure_artifact_id


def test_record_shard_lineage_must_point_to_exact_rendered_prompt():
    with pytest.raises(IntegrityViolation, match="lineage differs from its prompt"):
        _publish_record_attempt(wrong_prompt_lineage=True)


@pytest.mark.parametrize("retained_decision", ("absent", "substituted"))
def test_record_shard_requires_exact_retained_routing_decision(retained_decision):
    with pytest.raises(IntegrityViolation, match="retained RoutingDecision authority"):
        _publish_record_attempt(retained_decision=retained_decision)


def test_record_prompt_link_must_match_retained_attempt_fence():
    with pytest.raises(IntegrityViolation, match="fenced RunAttempt authority"):
        _publish_record_attempt(wrong_prompt_fence=True)
