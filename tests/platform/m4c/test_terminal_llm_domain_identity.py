"""Task 9 LLM domain Artifact terminal-identity authority tests."""

from __future__ import annotations

import pytest

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    GraphSelectionV1,
    PreparedArtifact,
    PreparedRunResult,
    PreparedRunResultSummaryV1,
    ReviewRunPayloadV1,
    RunIntermediateArtifactLinkV1,
    canonical_payload_hash,
    outcome_policy_set_digest,
    run_kind_definition_digest,
)
from gameforge.contracts.lineage import ArtifactV1, VersionTuple, build_execution_identity
from gameforge.contracts.review import ReviewReport
from gameforge.platform.publication import TerminalPublisher
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.runs.lifecycle import select_outcome_policy
from tests.platform.m4c.handler_support import build_envelope, build_run_record
from tests.platform.m4c.test_terminal_publisher import (
    NOW,
    WORKER,
    _Audit,
    _Blobs,
    _DirectPublisherHarness,
    _Findings,
    _attempt,
)
from tests.platform.m4c.test_terminal_runtime_identity import (
    GRAPH,
    MODEL,
    PROMPT,
    _RuntimeArtifacts,
    _RuntimeLedger,
    _binding,
    _plan,
    _routing_decision,
    _source_rendered,
)


def _fixture(*, prepared_prompt_version: str | None = None):
    registry = build_builtin_registry()
    kind = RunKindRef(kind="review.run", version=1)
    definition = registry.get_run_kind(kind)
    assert definition is not None
    params = ReviewRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        constraint_snapshot_artifact_id=None,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=ProfileRefV1(profile_id="builtin.review", version=1),
        checker_profiles=(),
        simulation_profiles=(),
        llm_triage_policy=ProfileRefV1(profile_id="builtin.llm_triage", version=1),
    )
    envelope = build_envelope(
        params=params,
        llm_execution_mode="live",
        plan=_plan(),
    )
    run = build_run_record(envelope, kind).model_copy(
        update={
            "payload_hash": canonical_payload_hash(envelope),
            "run_kind_definition_digest": run_kind_definition_digest(definition),
            "outcome_policy_set_digest": outcome_policy_set_digest(
                kind, definition.outcome_policies
            ),
            "failure_classifier": definition.failure_classifier,
            "retry_policy": definition.retry_policy,
            "max_attempts": registry.get_retry_policy(definition.retry_policy).max_attempts,
        }
    )

    blobs = _Blobs()
    artifacts = _RuntimeArtifacts(blobs)
    snapshot_id = "snapshot:review"
    artifacts.add(
        ArtifactV1(
            artifact_id="artifact:snapshot",
            kind="ir_snapshot",
            version_tuple=VersionTuple(ir_snapshot_id=snapshot_id),
            lineage=(),
            payload_hash=None,
            meta={
                "payload_schema_id": "ir-core@1",
                "domain_scope": DomainScope(domain_ids=("content",)),
            },
        )
    )
    prompt = _source_rendered(artifacts, blobs)
    prompt_link = RunIntermediateArtifactLinkV1(
        run_id=run.run_id,
        attempt_no=1,
        call_ordinal=1,
        artifact_id=prompt.artifact_id,
        role="prompt_rendered",
        request_hash="c" * 64,
        fencing_token=1,
        published_at=NOW,
    )
    decision = _routing_decision(request_hash_value="c" * 64)
    invocation = _binding(
        source="online",
        decision_id=decision.decision_id,
    )
    identity = build_execution_identity(
        scope="run",
        bindings=(invocation,),
        agent_graph_version=GRAPH,
    )
    attempt = _attempt().model_copy(update={"next_call_ordinal": 2})
    ledger = _RuntimeLedger(
        prompts=(prompt_link,),
        run_identity=identity,
        attempts={1: attempt},
        routing_decisions={decision.decision_id: decision},
    )

    payload = ReviewReport.partition(snapshot_id, []).model_dump(mode="json")
    blob = canonical_json(payload).encode("utf-8")
    object_ref = blobs.register(blob)
    prepared = PreparedArtifact(
        kind="review_report",
        payload_schema_id="review@1",
        version_tuple=VersionTuple(
            ir_snapshot_id=snapshot_id,
            prompt_version=prepared_prompt_version,
            tool_version="review@1",
        ),
        lineage=("artifact:snapshot",),
        payload_hash=object_ref.sha256,
        meta={
            "payload_schema_id": "review@1",
            "llm_execution_mode": "live",
            "llm_triage_applied": True,
        },
        object_ref=object_ref,
        location=blobs._locations[object_ref.key],  # noqa: SLF001 - exact fake binding
    )
    result = PreparedRunResult(
        run_id=run.run_id,
        attempt_no=1,
        run_kind=kind,
        primary_index=0,
        artifacts=(prepared,),
        findings=(),
        requirement_dispositions=(),
        summary=PreparedRunResultSummaryV1(
            outcome_code="review_completed",
            primary_artifact_kind="review_report",
            prepared_domain_artifact_count=1,
            prepared_finding_count=0,
        ),
    )
    policy = select_outcome_policy(
        definition=definition,
        outcome_code="review_completed",
        prepared_outcome="success",
        publication_scope="run",
        run_status="succeeded",
        attempt_status=None,
        failure_class=None,
        retry_disposition=None,
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
    return publisher, artifacts, run, attempt, result, policy


def test_llm_domain_artifact_mints_terminal_identity_from_retained_authorities() -> None:
    publisher, artifacts, run, attempt, prepared, policy = _fixture()

    publication = publisher.publish_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )

    manifest = artifacts.by_id[publication.result_artifact_id]
    domain = next(
        artifacts.by_id[parent]
        for parent in manifest.lineage
        if parent in artifacts.by_id and artifacts.by_id[parent].kind == "review_report"
    )
    assert domain.version_tuple.prompt_version == PROMPT
    assert domain.version_tuple.model_snapshot == MODEL
    assert domain.version_tuple.agent_graph_version == GRAPH
    assert domain.version_tuple.cassette_id is None
    assert domain.meta["execution_identity"].scope == "artifact"


def test_llm_prepared_artifact_cannot_self_report_terminal_identity() -> None:
    publisher, _, run, attempt, prepared, policy = _fixture(
        prepared_prompt_version="worker-forged@1"
    )

    with pytest.raises(IntegrityViolation, match="self-reports terminal execution identity"):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )
