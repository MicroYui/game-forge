from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cassette import CassetteRecordV1, CassetteRecordV2
from gameforge.contracts.cassette_import import (
    CassetteBundleV1,
    LegacyCassetteCallImportEvidenceV1,
    LegacyCassetteInputBindingV1,
    LegacyCassetteProfileBindingV1,
    LegacyCassetteSchemaBindingV1,
    LegacyImportRoutingDecisionV1,
    LegacyImportVerificationPolicyRefV1,
    LegacyImportVerificationPolicyRegistryV1,
    LegacyImportVerificationPolicyV1,
    build_legacy_import_manifest,
    compute_legacy_profile_binding_digest,
    original_wire_sha256,
)
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    VersionTransitionPolicyRefV1,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    FailureClassifierRefV1,
    GraphSelectionV1,
    PlannedAgentNodeVersionV1,
    RetryDecisionV1,
    RetryPolicyRefV1,
    ReviewRunPayloadV1,
    RunIntermediateArtifactLinkV1,
    RunManifestParentBindingV1,
    RunAttempt,
    RunPayloadEnvelope,
    RunRecord,
    RunManifestVersionProjectionV1,
    RunFailureV1,
    RunResultSummaryV1,
    RunResultV1,
    RunSchemaBindingV1,
    canonical_payload_hash,
    execution_version_plan_digest,
    referenced_input_artifact_ids,
)
from gameforge.contracts.lineage import (
    ArtifactKind,
    ArtifactV2,
    AuditActor,
    ExecutionIdentityV1,
    InvocationVersionBindingV1,
    VersionTuple,
    artifact_id_v2_for,
    build_execution_identity,
    object_ref_for_bytes,
)
from gameforge.contracts.model_router import (
    Message,
    ModelRequestV1,
    ModelRequestV2,
    ModelResponse,
    ModelSnapshot,
    request_hash,
)
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingDecisionV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
)
from gameforge.platform.runs.admission import AdmissionReadPort, _AdmissionReplayReader
from gameforge.platform.runs.replay import ReplayAdmissionValidator
from gameforge.runtime.cassette.legacy_import import (
    InMemoryLegacyImportAuthority,
    InMemoryLegacyImportDecisionRepository,
)


HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64
NOW = "2026-07-16T00:00:00Z"
SOURCE_RUN_ID = "run:recorded:1"


@dataclass
class MemoryReplayReader:
    artifacts: dict[str, ArtifactV2] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    runs: dict[str, RunRecord] = field(default_factory=dict)
    attempts: dict[tuple[str, int], RunAttempt] = field(default_factory=dict)
    routing_decisions: dict[str, RoutingDecisionV1] = field(default_factory=dict)
    prompt_links: dict[tuple[str, int, int], RunIntermediateArtifactLinkV1] = field(
        default_factory=dict
    )

    def get_artifact(self, artifact_id: str) -> ArtifactV2 | None:
        return self.artifacts.get(artifact_id)

    def read_artifact_bytes(self, artifact_id: str) -> bytes:
        return self.blobs[artifact_id]

    def get_run(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None:
        return self.attempts.get((run_id, attempt_no))

    def get_prompt_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
    ) -> RunIntermediateArtifactLinkV1 | None:
        return self.prompt_links.get((run_id, attempt_no, call_ordinal))

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None:
        return self.routing_decisions.get(decision_id)


class _ReplayBindingAuthority:
    def __init__(self) -> None:
        self._by_digest: dict[str, str] = {}

    def bind(self, artifact: ArtifactV2) -> None:
        self._by_digest[artifact.object_ref.sha256] = artifact.artifact_id

    def resolve(self, object_ref: object) -> object:
        digest = getattr(object_ref, "sha256")
        artifact_id = self._by_digest[digest]
        return SimpleNamespace(location=artifact_id)


class _ReplayArtifactAuthority:
    def __init__(self, reader: MemoryReplayReader) -> None:
        self._reader = reader

    def get(self, artifact_id: str) -> ArtifactV2 | None:
        return self._reader.get_artifact(artifact_id)


class _ReplayRunAuthority:
    def __init__(self, reader: MemoryReplayReader) -> None:
        self._reader = reader

    def get(self, run_id: str) -> RunRecord | None:
        return self._reader.get_run(run_id)

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None:
        return self._reader.get_attempt(run_id, attempt_no)

    def get_intermediate_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
    ) -> RunIntermediateArtifactLinkV1 | None:
        return self._reader.get_prompt_link(run_id, attempt_no, call_ordinal)


class _ReplayRoutingAuthority:
    def __init__(self, reader: MemoryReplayReader) -> None:
        self._reader = reader

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None:
        return self._reader.get_routing_decision(decision_id)


class _ReplayObjectStore:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    def open(self, location: object) -> BytesIO:
        return BytesIO(self._blobs[str(location)])


def _plan(
    *,
    agent_graph_version: str = "review-graph@1",
    model_snapshot: str = "openai/gpt-test/replay@1",
    model_catalog_version: int = 1,
    model_catalog_digest: str = HEX_A,
) -> ExecutionVersionPlanV1:
    node = PlannedAgentNodeVersionV1(
        agent_node_id="review-triage",
        prompt_version="review-prompt@1",
        tool_version="review-triage@1",
        allowed_model_snapshots=(model_snapshot,),
    )
    payload = {
        "plan_schema_version": "execution-version-plan@1",
        "agent_graph_version": agent_graph_version,
        "nodes": [node.model_dump(mode="json")],
        "model_catalog_version": model_catalog_version,
        "model_catalog_digest": model_catalog_digest,
        "routing_policy_version": 1,
        "routing_policy_digest": HEX_B,
    }
    return ExecutionVersionPlanV1(
        **payload,
        plan_digest=execution_version_plan_digest(payload),
    )


def _profile(
    *,
    version: int = 1,
    catalog_version: int = 1,
    catalog_digest: str = HEX_A,
) -> ResolvedExecutionProfileBindingV1:
    return ResolvedExecutionProfileBindingV1(
        field_path="/params/llm_triage_policy",
        profile=ProfileRefV1(profile_id="review-triage", version=version),
        expected_profile_kind="llm_triage",
        profile_payload_hash=HEX_C,
        catalog_version=catalog_version,
        catalog_digest=catalog_digest,
    )


def _artifact(
    *,
    kind: ArtifactKind,
    payload: object,
    version_tuple: VersionTuple,
    lineage: tuple[str, ...] = (),
    payload_schema_id: str,
    meta_extra: dict[str, object] | None = None,
) -> tuple[ArtifactV2, bytes]:
    value = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    blob = canonical_json(value).encode("utf-8")
    object_ref = object_ref_for_bytes(blob)
    meta = {"payload_schema_id": payload_schema_id, **(meta_extra or {})}
    artifact_id = artifact_id_v2_for(
        kind=kind,
        version_tuple=version_tuple,
        lineage=lineage,
        payload_hash=object_ref.sha256,
        meta=meta,
    )
    return (
        ArtifactV2(
            artifact_id=artifact_id,
            kind=kind,
            version_tuple=version_tuple,
            lineage=lineage,
            payload_hash=object_ref.sha256,
            object_ref=object_ref,
            meta=meta,
        ),
        blob,
    )


def _bundle_artifact(
    bundle: CassetteBundleV1,
    *,
    lineage: tuple[str, ...],
    identity: ExecutionIdentityV1 | None = None,
    tool_version: str = "cassette-bundle@1",
) -> tuple[ArtifactV2, bytes]:
    value = bundle.model_dump(mode="json")
    blob = canonical_json(value).encode("utf-8")
    digest = object_ref_for_bytes(blob).sha256
    schema = "cassette-record-shard@1" if bundle.scope == "record_shard" else "cassette-bundle@1"
    return _artifact(
        kind="cassette_bundle",
        payload=bundle,
        version_tuple=VersionTuple(
            prompt_version=None if identity is None else identity.prompt_projection.tuple_value,
            model_snapshot=None if identity is None else identity.model_projection.tuple_value,
            agent_graph_version=None if identity is None else identity.agent_graph_version,
            tool_version=tool_version,
            cassette_id=f"sha256:{digest}",
        ),
        lineage=lineage,
        payload_schema_id=schema,
        meta_extra=None if identity is None else {"execution_identity": identity},
    )


def _params(snapshot_artifact_id: str) -> ReviewRunPayloadV1:
    return ReviewRunPayloadV1(
        snapshot_artifact_id=snapshot_artifact_id,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=ProfileRefV1(profile_id="review", version=1),
        checker_profiles=(),
        simulation_profiles=(),
        llm_triage_policy=ProfileRefV1(profile_id="review-triage", version=1),
    )


def _payload(
    *,
    params: ReviewRunPayloadV1,
    plan: ExecutionVersionPlanV1,
    profile: ResolvedExecutionProfileBindingV1,
    mode: str,
    cassette: ArtifactV2 | None = None,
    schema_bindings: tuple[RunSchemaBindingV1, ...] | None = None,
    tool_version: str = "review@1",
) -> RunPayloadEnvelope:
    cassette_id = None if cassette is None else cassette.artifact_id
    inputs = [*referenced_input_artifact_ids(params)]
    if cassette_id is not None:
        inputs.append(cassette_id)
    return RunPayloadEnvelope(
        payload_schema_version=params.schema_version,
        input_artifact_ids=tuple(inputs),
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:review@1",
            prompt_version=plan.nodes[0].prompt_version,
            model_snapshot=plan.nodes[0].allowed_model_snapshots[0],
            agent_graph_version=plan.agent_graph_version,
            tool_version=tool_version,
            cassette_id=(None if cassette is None else f"sha256:{cassette.payload_hash}"),
        ),
        execution_version_plan=plan,
        policy_bindings=(),
        schema_bindings=(
            (
                RunSchemaBindingV1(
                    binding_key="run_payload",
                    schema_id=params.schema_version,
                ),
            )
            if schema_bindings is None
            else schema_bindings
        ),
        execution_profile_catalog_version=plan.model_catalog_version,
        execution_profile_catalog_digest=plan.model_catalog_digest,
        resolved_profiles=(profile,),
        resolved_policy_snapshots=(),
        budget_set_snapshot_id="budget-set:replay",
        seed=None,
        llm_execution_mode=mode,
        cassette_artifact_id=cassette_id,
        params=params,
    )


def _source_run(
    payload: RunPayloadEnvelope,
    *,
    cassette_artifact_id: str,
    result_artifact_id: str,
) -> RunRecord:
    return RunRecord(
        run_id=SOURCE_RUN_ID,
        kind=RunKindRef(kind="review.run", version=1),
        status="succeeded",
        revision=8,
        idempotency_scope="principal:human:author",
        idempotency_key="record-review",
        request_hash=HEX_A,
        payload=payload,
        payload_hash=canonical_payload_hash(payload),
        run_kind_definition_digest=HEX_B,
        outcome_policy_set_digest=HEX_C,
        failure_classifier=FailureClassifierRefV1(
            classifier_version=1,
            classifier_digest=HEX_A,
        ),
        initiated_by=AuditActor(principal_id="human:author", principal_kind="human"),
        queue_deadline_utc="2026-07-16T00:10:00Z",
        attempt_timeout_ns=30_000_000_000,
        overall_deadline_utc="2026-07-16T01:00:00Z",
        current_attempt_no=1,
        next_attempt_no=2,
        next_fencing_token=2,
        next_event_seq=9,
        budget_set_snapshot_id=payload.budget_set_snapshot_id,
        run_budget_hold_group_id="hold:record-review",
        retry_policy=RetryPolicyRefV1(
            retry_policy_id="agent-run",
            retry_policy_version=1,
            retry_policy_digest=HEX_B,
        ),
        max_attempts=2,
        result_artifact_id=result_artifact_id,
        terminal_cassette_artifact_id=cassette_artifact_id,
        created_at=NOW,
        updated_at=NOW,
    )


def _terminal_result_artifacts(
    *,
    payload: RunPayloadEnvelope,
    cassette: ArtifactV2,
    outcome_code: str = "review_completed",
) -> tuple[tuple[ArtifactV2, bytes], tuple[ArtifactV2, bytes]]:
    primary = _artifact(
        kind="review_report",
        payload={"status": "completed"},
        version_tuple=payload.version_tuple.model_copy(
            update={"cassette_id": cassette.version_tuple.cassette_id}
        ),
        payload_schema_id="review-report@1",
    )
    terminal_tuple = payload.version_tuple.model_copy(
        update={"cassette_id": cassette.version_tuple.cassette_id}
    )
    projection = RunManifestVersionProjectionV1(
        manifest_scope="run",
        attempt_no=1,
        run_kind=RunKindRef(kind="review.run", version=1),
        run_payload_hash=canonical_payload_hash(payload),
        frozen_input_version_tuple=payload.version_tuple,
        terminal_version_tuple=terminal_tuple,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="run-manifest-transition",
            policy_version=1,
            digest=HEX_A,
        ),
        parents=(
            RunManifestParentBindingV1(
                artifact_id=primary[0].artifact_id,
                role="output",
                publication="run_published",
            ),
            RunManifestParentBindingV1(
                artifact_id=cassette.artifact_id,
                role="intermediate",
                publication="run_published",
                cassette_scope="run_bundle",
            ),
        ),
    )
    result_payload = RunResultV1(
        run_id=SOURCE_RUN_ID,
        attempt_no=1,
        run_kind=RunKindRef(kind="review.run", version=1),
        primary_artifact_id=primary[0].artifact_id,
        produced_artifact_ids=(primary[0].artifact_id, cassette.artifact_id),
        finding_count=0,
        outcome_code=outcome_code,
        summary=RunResultSummaryV1(
            outcome_code=outcome_code,
            primary_artifact_kind="review_report",
            produced_artifact_count=2,
            finding_count=0,
        ),
        requirement_dispositions=(),
        version_projection=projection,
    )
    result = _artifact(
        kind="run_result",
        payload=result_payload,
        version_tuple=terminal_tuple,
        lineage=(primary[0].artifact_id, cassette.artifact_id),
        payload_schema_id="run-result@1",
    )
    return primary, result


@dataclass
class NativeFixture:
    reader: MemoryReplayReader
    validator: ReplayAdmissionValidator
    source: RunRecord
    source_payload: RunPayloadEnvelope
    replay_payload: RunPayloadEnvelope
    root: ArtifactV2
    root_bundle: CassetteBundleV1
    attempt: ArtifactV2
    input_artifact: ArtifactV2


@dataclass
class LegacyFixture:
    reader: MemoryReplayReader
    authority: InMemoryLegacyImportAuthority
    decisions: InMemoryLegacyImportDecisionRepository
    validator: ReplayAdmissionValidator
    replay_payload: RunPayloadEnvelope
    plan: ExecutionVersionPlanV1
    root: ArtifactV2


@dataclass
class ZeroAttemptNativeFixture:
    reader: MemoryReplayReader
    validator: ReplayAdmissionValidator
    source: RunRecord
    replay_payload: RunPayloadEnvelope
    root: ArtifactV2


def _zero_attempt_native_fixture(
    status: str,
) -> ZeroAttemptNativeFixture:
    reader = MemoryReplayReader()
    input_artifact, input_blob = _artifact(
        kind="ir_snapshot",
        payload={"snapshot_id": "snapshot:review@1"},
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:review@1",
            tool_version="snapshot-writer@1",
        ),
        payload_schema_id="ir-snapshot@1",
    )
    reader.artifacts[input_artifact.artifact_id] = input_artifact
    reader.blobs[input_artifact.artifact_id] = input_blob

    plan = _plan()
    run_identity = build_execution_identity(
        scope="run",
        bindings=(),
        agent_graph_version=plan.agent_graph_version,
    )
    failure_class = {
        "failed": "execution",
        "cancelled": "cancelled",
        "timed_out": "timeout",
    }[status]
    cause_code = {
        "failed": "execution_failed",
        "cancelled": "cancelled",
        "timed_out": "timed_out",
    }[status]
    root_bundle = CassetteBundleV1(
        scope="run",
        run_id=SOURCE_RUN_ID,
        outcome_code=cause_code,
    )
    root, root_blob = _bundle_artifact(
        root_bundle,
        lineage=(),
        identity=run_identity,
    )
    reader.artifacts[root.artifact_id] = root
    reader.blobs[root.artifact_id] = root_blob

    params = _params(input_artifact.artifact_id)
    source_payload = _payload(
        params=params,
        plan=plan,
        profile=_profile(),
        mode="record",
    )
    replay_payload = _payload(
        params=params,
        plan=plan,
        profile=_profile(),
        mode="replay",
        cassette=root,
    )
    terminal_tuple = source_payload.version_tuple.model_copy(
        update={"cassette_id": root.version_tuple.cassette_id}
    )
    classifier = FailureClassifierRefV1(
        classifier_version=1,
        classifier_digest=HEX_A,
    )
    retry_policy = RetryPolicyRefV1(
        retry_policy_id="agent-run",
        retry_policy_version=1,
        retry_policy_digest=HEX_B,
    )
    projection = RunManifestVersionProjectionV1(
        manifest_scope="run",
        attempt_no=None,
        run_kind=RunKindRef(kind="review.run", version=1),
        run_payload_hash=canonical_payload_hash(source_payload),
        frozen_input_version_tuple=source_payload.version_tuple,
        terminal_version_tuple=terminal_tuple,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="run-manifest-transition",
            policy_version=1,
            digest=HEX_A,
        ),
        parents=(
            RunManifestParentBindingV1(
                artifact_id=root.artifact_id,
                role="intermediate",
                publication="run_published",
                cassette_scope="run_bundle",
            ),
        ),
    )
    failure_payload = RunFailureV1(
        run_id=SOURCE_RUN_ID,
        attempt_no=None,
        run_kind=RunKindRef(kind="review.run", version=1),
        cause_code=cause_code,
        failure_class=failure_class,
        retryable=False,
        retry_decision=RetryDecisionV1(
            cause_code=cause_code,
            failure_class=failure_class,
            intrinsic_retry_eligible=False,
            decision="terminal",
            reason_code="not_retry_eligible",
            classifier=classifier,
            retry_policy=retry_policy,
            evaluated_at_utc=NOW,
        ),
        redacted_message="Run ended before its first attempt.",
        evidence_artifact_ids=(root.artifact_id,),
        requirement_dispositions=(),
        occurred_at=NOW,
        version_projection=projection,
    )
    failure, failure_blob = _artifact(
        kind="run_failure",
        payload=failure_payload,
        version_tuple=terminal_tuple,
        lineage=(root.artifact_id,),
        payload_schema_id="run-failure@1",
    )
    reader.artifacts[failure.artifact_id] = failure
    reader.blobs[failure.artifact_id] = failure_blob
    source = RunRecord(
        run_id=SOURCE_RUN_ID,
        kind=RunKindRef(kind="review.run", version=1),
        status=status,
        revision=4,
        idempotency_scope="principal:human:author",
        idempotency_key="record-review-no-attempt",
        request_hash=HEX_A,
        payload=source_payload,
        payload_hash=canonical_payload_hash(source_payload),
        run_kind_definition_digest=HEX_B,
        outcome_policy_set_digest=HEX_C,
        failure_classifier=classifier,
        initiated_by=AuditActor(principal_id="human:author", principal_kind="human"),
        queue_deadline_utc="2026-07-16T00:10:00Z",
        attempt_timeout_ns=30_000_000_000,
        overall_deadline_utc="2026-07-16T01:00:00Z",
        current_attempt_no=None,
        next_attempt_no=1,
        next_fencing_token=1,
        next_event_seq=5,
        budget_set_snapshot_id=source_payload.budget_set_snapshot_id,
        run_budget_hold_group_id="hold:record-review",
        retry_policy=retry_policy,
        max_attempts=2,
        failure_artifact_id=failure.artifact_id,
        terminal_cassette_artifact_id=root.artifact_id,
        created_at=NOW,
        updated_at=NOW,
    )
    reader.runs[source.run_id] = source
    return ZeroAttemptNativeFixture(
        reader=reader,
        validator=ReplayAdmissionValidator(reader),
        source=source,
        replay_payload=replay_payload,
        root=root,
    )


def _native_fixture() -> NativeFixture:
    reader = MemoryReplayReader()
    input_artifact, input_blob = _artifact(
        kind="ir_snapshot",
        payload={"snapshot_id": "snapshot:review@1"},
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:review@1",
            tool_version="snapshot-writer@1",
        ),
        payload_schema_id="ir-snapshot@1",
    )
    reader.artifacts[input_artifact.artifact_id] = input_artifact
    reader.blobs[input_artifact.artifact_id] = input_blob

    plan = _plan()
    attempt_identity = build_execution_identity(
        scope="attempt",
        bindings=(),
        agent_graph_version=plan.agent_graph_version,
    )
    run_identity = build_execution_identity(
        scope="run",
        bindings=(),
        agent_graph_version=plan.agent_graph_version,
    )
    attempt_bundle = CassetteBundleV1(
        scope="attempt",
        run_id=SOURCE_RUN_ID,
        attempt_no=1,
    )
    attempt, attempt_blob = _bundle_artifact(
        attempt_bundle,
        lineage=(),
        identity=attempt_identity,
    )
    root_bundle = CassetteBundleV1(
        scope="run",
        run_id=SOURCE_RUN_ID,
        child_bundle_artifact_ids=(attempt.artifact_id,),
        outcome_code="review_completed",
    )
    root, root_blob = _bundle_artifact(
        root_bundle,
        lineage=(attempt.artifact_id,),
        identity=run_identity,
    )
    reader.artifacts.update({attempt.artifact_id: attempt, root.artifact_id: root})
    reader.blobs.update({attempt.artifact_id: attempt_blob, root.artifact_id: root_blob})

    params = _params(input_artifact.artifact_id)
    source_payload = _payload(
        params=params,
        plan=plan,
        profile=_profile(),
        mode="record",
    )
    replay_payload = _payload(
        params=params,
        plan=plan,
        profile=_profile(),
        mode="replay",
        cassette=root,
    )
    primary, result = _terminal_result_artifacts(payload=source_payload, cassette=root)
    for artifact, blob in (primary, result):
        reader.artifacts[artifact.artifact_id] = artifact
        reader.blobs[artifact.artifact_id] = blob
    source = _source_run(
        source_payload,
        cassette_artifact_id=root.artifact_id,
        result_artifact_id=result[0].artifact_id,
    )
    reader.runs[source.run_id] = source
    reader.attempts[(source.run_id, 1)] = RunAttempt(
        run_id=source.run_id,
        attempt_no=1,
        status="succeeded",
        fencing_token=1,
        worker_principal_id="service:worker",
        next_call_ordinal=1,
        started_at=NOW,
        attempt_deadline_utc="2026-07-16T00:30:00Z",
        ended_at=NOW,
        cassette_bundle_artifact_id=attempt.artifact_id,
    )
    return NativeFixture(
        reader=reader,
        validator=ReplayAdmissionValidator(reader),
        source=source,
        source_payload=source_payload,
        replay_payload=replay_payload,
        root=root,
        root_bundle=root_bundle,
        attempt=attempt,
        input_artifact=input_artifact,
    )


def _native_record_fixture() -> NativeFixture:
    reader = MemoryReplayReader()
    input_artifact, input_blob = _artifact(
        kind="ir_snapshot",
        payload={"snapshot_id": "snapshot:review@1"},
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:review@1",
            tool_version="snapshot-writer@1",
        ),
        payload_schema_id="ir-snapshot@1",
    )
    reader.artifacts[input_artifact.artifact_id] = input_artifact
    reader.blobs[input_artifact.artifact_id] = input_blob

    snapshot = ModelSnapshot(provider="openai", model="gpt-test", snapshot_tag="replay-1")
    model_id = canonical_model_snapshot_id(snapshot)
    plan = _plan(model_snapshot=model_id)
    rendered_request = ModelRequestV2(
        model_snapshot=snapshot,
        messages=[Message(role="user", content="review the snapshot")],
        params={},
        tool_schemas=[],
        agent_node_id=plan.nodes[0].agent_node_id,
        prompt_version=plan.nodes[0].prompt_version,
    )
    request_hash_value = request_hash(rendered_request)
    bare_request_hash = request_hash_value.removeprefix("sha256:")
    decision = RoutingDecisionV1.create(
        run_id=SOURCE_RUN_ID,
        attempt_no=1,
        request_hash=request_hash_value,
        rule_id="review-default",
        model_snapshot=model_id,
        tier="best",
        reason_code="primary_rule",
        budget_set_snapshot_id="budget-set:replay",
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
        request_hash=request_hash_value,
        agent_node_id=plan.nodes[0].agent_node_id,
        model_snapshot=snapshot,
        routing_decision=decision,
        response_normalized="reviewed",
        raw_response={"id": "response:1"},
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
    invocation = InvocationVersionBindingV1(
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        transport_attempt=1,
        routing_decision_kind="native",
        routing_decision_id=decision.decision_id,
        agent_node_id=plan.nodes[0].agent_node_id,
        prompt_version=plan.nodes[0].prompt_version,
        model_snapshot=model_id,
        tool_version=plan.nodes[0].tool_version,
        execution_source="online",
        response_consumed=True,
    )
    shard_identity = build_execution_identity(
        scope="record_shard",
        bindings=(invocation,),
        agent_graph_version=plan.agent_graph_version,
    )
    attempt_identity = build_execution_identity(
        scope="attempt",
        bindings=(invocation,),
        agent_graph_version=plan.agent_graph_version,
    )
    run_identity = build_execution_identity(
        scope="run",
        bindings=(invocation,),
        agent_graph_version=plan.agent_graph_version,
    )
    prompt, prompt_blob = _artifact(
        kind="source_rendered",
        payload=rendered_request,
        version_tuple=VersionTuple(
            prompt_version=plan.nodes[0].prompt_version,
            model_snapshot=model_id,
            agent_graph_version=plan.agent_graph_version,
            tool_version="prompt-renderer@1",
        ),
        payload_schema_id="source-rendered@1",
    )
    shard_bundle = CassetteBundleV1(
        scope="record_shard",
        run_id=SOURCE_RUN_ID,
        attempt_no=1,
        ordinal=1,
        records=(record,),
    )
    shard, shard_blob = _bundle_artifact(
        shard_bundle,
        lineage=(prompt.artifact_id,),
        identity=shard_identity,
    )
    attempt_bundle = CassetteBundleV1(
        scope="attempt",
        run_id=SOURCE_RUN_ID,
        attempt_no=1,
        child_bundle_artifact_ids=(shard.artifact_id,),
    )
    attempt, attempt_blob = _bundle_artifact(
        attempt_bundle,
        lineage=(shard.artifact_id,),
        identity=attempt_identity,
    )
    root_bundle = CassetteBundleV1(
        scope="run",
        run_id=SOURCE_RUN_ID,
        child_bundle_artifact_ids=(attempt.artifact_id,),
        outcome_code="review_completed",
    )
    root, root_blob = _bundle_artifact(
        root_bundle,
        lineage=(attempt.artifact_id,),
        identity=run_identity,
    )
    for artifact, blob in (
        (prompt, prompt_blob),
        (shard, shard_blob),
        (attempt, attempt_blob),
        (root, root_blob),
    ):
        reader.artifacts[artifact.artifact_id] = artifact
        reader.blobs[artifact.artifact_id] = blob
    reader.prompt_links[(SOURCE_RUN_ID, 1, 1)] = RunIntermediateArtifactLinkV1(
        run_id=SOURCE_RUN_ID,
        attempt_no=1,
        call_ordinal=1,
        artifact_id=prompt.artifact_id,
        role="prompt_rendered",
        request_hash=bare_request_hash,
        fencing_token=1,
        published_at=NOW,
    )
    params = _params(input_artifact.artifact_id)
    source_payload = _payload(
        params=params,
        plan=plan,
        profile=_profile(),
        mode="record",
    )
    replay_payload = _payload(
        params=params,
        plan=plan,
        profile=_profile(),
        mode="replay",
        cassette=root,
    )
    primary, result = _terminal_result_artifacts(payload=source_payload, cassette=root)
    for artifact, blob in (primary, result):
        reader.artifacts[artifact.artifact_id] = artifact
        reader.blobs[artifact.artifact_id] = blob
    source = _source_run(
        source_payload,
        cassette_artifact_id=root.artifact_id,
        result_artifact_id=result[0].artifact_id,
    )
    reader.runs[source.run_id] = source
    reader.attempts[(source.run_id, 1)] = RunAttempt(
        run_id=source.run_id,
        attempt_no=1,
        status="succeeded",
        fencing_token=1,
        worker_principal_id="service:worker",
        next_call_ordinal=2,
        started_at=NOW,
        attempt_deadline_utc="2026-07-16T00:30:00Z",
        ended_at=NOW,
        cassette_bundle_artifact_id=attempt.artifact_id,
    )
    reader.routing_decisions[decision.decision_id] = decision
    return NativeFixture(
        reader=reader,
        validator=ReplayAdmissionValidator(reader),
        source=source,
        source_payload=source_payload,
        replay_payload=replay_payload,
        root=root,
        root_bundle=root_bundle,
        attempt=attempt,
        input_artifact=input_artifact,
    )


def _legacy_verified_fixture() -> LegacyFixture:
    reader = MemoryReplayReader()
    input_artifact, input_blob = _artifact(
        kind="ir_snapshot",
        payload={"snapshot_id": "snapshot:review@1"},
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:review@1",
            tool_version="snapshot-writer@1",
        ),
        payload_schema_id="ir-snapshot@1",
    )
    reader.artifacts[input_artifact.artifact_id] = input_artifact
    reader.blobs[input_artifact.artifact_id] = input_blob

    model_snapshot = ModelSnapshot(
        provider="anthropic",
        model="claude-opus-4-8",
        snapshot_tag="m2a@1",
    )
    model_id = canonical_model_snapshot_id(model_snapshot)
    descriptor = ModelDescriptorV1(
        provider="anthropic",
        model_snapshot=model_id,
        tier="historical",
        capabilities=("text",),
        context_limit=200_000,
        max_output_tokens=8192,
        prompt_cache_support=False,
        status="active",
    )
    catalog_payload = {
        "catalog_schema_version": "model-catalog@1",
        "catalog_version": 7,
        "models": [descriptor.model_dump(mode="json")],
        "created_at": "2026-07-16T00:00:00Z",
    }
    catalog = ModelCatalogSnapshotV1(
        **catalog_payload,
        catalog_digest=compute_model_catalog_digest(catalog_payload),
    )
    plan = _plan(
        agent_graph_version="review-graph@1",
        model_snapshot=model_id,
        model_catalog_version=catalog.catalog_version,
        model_catalog_digest=catalog.catalog_digest,
    )
    request = ModelRequestV1(
        model_snapshot=model_snapshot,
        messages=[Message(role="user", content="review historical snapshot")],
        params={"temperature": 0},
        agent_node_id=plan.nodes[0].agent_node_id,
        prompt_version=plan.nodes[0].prompt_version,
    )
    rendered, rendered_blob = _artifact(
        kind="source_rendered",
        payload=request,
        version_tuple=VersionTuple(
            prompt_version=request.prompt_version,
            model_snapshot=model_id,
            agent_graph_version=plan.agent_graph_version,
            tool_version="legacy-renderer@1",
        ),
        payload_schema_id="source-rendered@1",
    )
    reader.artifacts[rendered.artifact_id] = rendered
    reader.blobs[rendered.artifact_id] = rendered_blob

    policy = LegacyImportVerificationPolicyV1.create(
        policy_id="legacy-review-import",
        policy_version=1,
        required_input_binding_keys=("snapshot",),
        required_profile_field_paths=("/params/llm_triage_policy",),
        required_policy_binding_keys=(),
        required_schema_binding_keys=("run_payload",),
        max_wire_bytes_per_call=32_768,
        max_calls_per_import=4,
    )
    policy_registry = LegacyImportVerificationPolicyRegistryV1.create(
        registry_version=1,
        policies=(policy,),
    )
    input_binding = LegacyCassetteInputBindingV1(
        binding_key="snapshot",
        artifact_id=input_artifact.artifact_id,
        payload_hash=input_artifact.payload_hash,
        version_tuple=input_artifact.version_tuple,
    )
    profile_binding = LegacyCassetteProfileBindingV1(
        field_path="/params/llm_triage_policy",
        profile_id="review-triage",
        profile_version=1,
        profile_payload_hash=HEX_C,
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    )
    schema_binding = LegacyCassetteSchemaBindingV1(
        binding_key="run_payload",
        schema_id="review-run@1",
    )
    record = CassetteRecordV1(
        request_hash=request_hash(request),
        agent_node_id=request.agent_node_id,
        model_snapshot=request.model_snapshot,
        response=ModelResponse(
            response_normalized="historical review",
            raw_response={"id": "legacy-response:1"},
            latency_ms=42,
            token_usage={"input_tokens": 4, "output_tokens": 2},
            finish_reason="stop",
        ),
        transport_attempts=1,
        transport_retries=0,
        recorded_at="2026-07-10T00:00:00Z",
    )
    original_wire = json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    import_decision = LegacyImportRoutingDecisionV1.create(
        source_wire_sha256=original_wire_sha256(original_wire),
        request_hash=request_hash(request),
        agent_node_id=request.agent_node_id,
        model_snapshot=model_id,
        execution_profile_binding_digests=(compute_legacy_profile_binding_digest(profile_binding),),
        model_catalog_version=catalog.catalog_version,
        model_catalog_digest=catalog.catalog_digest,
        verification_policy=policy.ref(),
    )
    invocation = InvocationVersionBindingV1(
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        transport_attempt=None,
        routing_decision_kind="legacy_import",
        routing_decision_id=import_decision.decision_id,
        agent_node_id=request.agent_node_id,
        prompt_version=request.prompt_version,
        model_snapshot=model_id,
        tool_version=plan.nodes[0].tool_version,
        execution_source="cassette_replay",
        response_consumed=True,
    )
    evidence = LegacyCassetteCallImportEvidenceV1.create(
        original_wire_utf8=original_wire,
        rendered_request_artifact_id=rendered.artifact_id,
        request_hash=request_hash(request),
        import_routing_decision=import_decision,
        invocation=invocation,
        source_suite_id="m2-review",
        source_case_id="review-case-1",
        source_call_ordinal=1,
        importer_tool_version="legacy-importer@1",
        verification_status="verified",
        missing_fields=(),
    )
    run_identity = build_execution_identity(
        scope="run",
        bindings=(invocation,),
        agent_graph_version=plan.agent_graph_version,
    )
    params = _params(input_artifact.artifact_id)
    resolved_profile = _profile(
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    )
    source_basis = _payload(
        params=params,
        plan=plan,
        profile=resolved_profile,
        mode="record",
        schema_bindings=(
            RunSchemaBindingV1(
                binding_key=schema_binding.binding_key,
                schema_id=schema_binding.schema_id,
            ),
        ),
        tool_version=plan.nodes[0].tool_version,
    )
    manifest = build_legacy_import_manifest(
        source_suite_id="m2-review",
        source_case_id="review-case-1",
        verification_policy=policy.ref(),
        input_artifact_bindings=(input_binding,),
        execution_profile_bindings=(profile_binding,),
        frozen_version_tuple=source_basis.version_tuple,
        policy_bindings=(),
        schema_bindings=(schema_binding,),
        ordered_call_evidence_digests=(evidence.evidence_digest,),
        execution_identity=run_identity,
        importer_tool_version="legacy-importer@1",
        status="verified",
    )
    shard_identity = build_execution_identity(
        scope="record_shard",
        bindings=(invocation,),
        agent_graph_version=plan.agent_graph_version,
    )
    attempt_identity = build_execution_identity(
        scope="attempt",
        bindings=(invocation,),
        agent_graph_version=plan.agent_graph_version,
    )
    shard_bundle = CassetteBundleV1(
        scope="record_shard",
        attempt_no=1,
        ordinal=1,
        records=(record,),
        legacy_call_import_evidence=evidence,
    )
    shard, shard_blob = _bundle_artifact(
        shard_bundle,
        lineage=(rendered.artifact_id,),
        identity=shard_identity,
        tool_version=manifest.importer_tool_version,
    )
    attempt_bundle = CassetteBundleV1(
        scope="attempt",
        attempt_no=1,
        child_bundle_artifact_ids=(shard.artifact_id,),
    )
    attempt, attempt_blob = _bundle_artifact(
        attempt_bundle,
        lineage=(shard.artifact_id,),
        identity=attempt_identity,
        tool_version=manifest.importer_tool_version,
    )
    root_bundle = CassetteBundleV1(
        scope="run",
        child_bundle_artifact_ids=(attempt.artifact_id,),
        legacy_run_import_manifest=manifest,
    )
    root, root_blob = _bundle_artifact(
        root_bundle,
        lineage=(attempt.artifact_id,),
        identity=run_identity,
        tool_version=manifest.importer_tool_version,
    )
    for artifact, blob in ((shard, shard_blob), (attempt, attempt_blob), (root, root_blob)):
        reader.artifacts[artifact.artifact_id] = artifact
        reader.blobs[artifact.artifact_id] = blob

    authority = InMemoryLegacyImportAuthority(
        verification_policy_registry=policy_registry,
        model_catalogs={(catalog.catalog_version, catalog.catalog_digest): catalog},
        input_bindings={(input_binding.binding_key, input_binding.artifact_id): input_binding},
        profile_bindings={
            (
                profile_binding.field_path,
                profile_binding.profile_id,
                profile_binding.profile_version,
            ): profile_binding
        },
        policy_bindings={},
        schema_bindings={(schema_binding.binding_key, schema_binding.schema_id): schema_binding},
        rendered_requests={rendered.artifact_id: request},
        frozen_version_tuples={
            (manifest.source_suite_id, manifest.source_case_id): source_basis.version_tuple
        },
        call_tool_versions={
            (manifest.source_suite_id, manifest.source_case_id, 1): plan.nodes[0].tool_version
        },
    )
    decisions = InMemoryLegacyImportDecisionRepository()
    decisions.put_legacy_import_routing_decision(import_decision)
    replay_payload = _payload(
        params=params,
        plan=plan,
        profile=resolved_profile,
        mode="replay",
        cassette=root,
        schema_bindings=source_basis.schema_bindings,
        tool_version=plan.nodes[0].tool_version,
    )
    return LegacyFixture(
        reader=reader,
        authority=authority,
        decisions=decisions,
        validator=ReplayAdmissionValidator(
            reader,
            legacy_authority=authority,
            legacy_decisions=decisions,
        ),
        replay_payload=replay_payload,
        plan=plan,
        root=root,
    )


def _replace_payload(payload: RunPayloadEnvelope, **updates: Any) -> RunPayloadEnvelope:
    value = payload.model_dump(mode="python")
    value.update(updates)
    return RunPayloadEnvelope.model_validate(value)


def _replace_run(run: RunRecord, **updates: Any) -> RunRecord:
    value = run.model_dump(mode="python")
    value.update(updates)
    return RunRecord.model_validate(value)


def test_native_replay_closes_terminal_source_and_returns_permission_hook() -> None:
    fixture = _native_fixture()

    proof = fixture.validator.validate(
        kind=fixture.source.kind,
        payload=fixture.replay_payload,
    )

    assert proof.source_kind == "native"
    assert proof.source_run_id == SOURCE_RUN_ID
    assert proof.attempt_count == 1
    assert proof.record_count == 0
    permission = proof.required_permission(DomainScope(domain_ids=("economy",)))
    assert permission.action == "replay"
    assert permission.resource_kind == "run"
    assert permission.domain_scope == DomainScope(domain_ids=("economy",))


@pytest.mark.parametrize("status", ["failed", "cancelled", "timed_out"])
def test_native_replay_accepts_terminal_record_source_without_an_attempt(
    status: str,
) -> None:
    fixture = _zero_attempt_native_fixture(status)

    proof = fixture.validator.validate(
        kind=fixture.source.kind,
        payload=fixture.replay_payload,
    )

    assert proof.source_kind == "native"
    assert proof.source_run_id == SOURCE_RUN_ID
    assert proof.attempt_count == 0
    assert proof.record_count == 0


def test_native_zero_attempt_replay_rejects_a_fabricated_attempt_head() -> None:
    fixture = _zero_attempt_native_fixture("cancelled")
    fixture.reader.runs[SOURCE_RUN_ID] = fixture.source.model_copy(
        update={"current_attempt_no": 1, "next_attempt_no": 2}
    )

    with pytest.raises(IntegrityViolation, match="zero-attempt cassette differs"):
        fixture.validator.validate(
            kind=fixture.source.kind,
            payload=fixture.replay_payload,
        )


def test_persisted_dict_execution_identity_is_revalidated() -> None:
    fixture = _native_fixture()
    identity = fixture.root.meta["execution_identity"]
    persisted = fixture.root.model_copy(
        update={
            "meta": {
                **fixture.root.meta,
                "execution_identity": identity.model_dump(mode="json"),
            }
        }
    )
    fixture.reader.artifacts[fixture.root.artifact_id] = persisted

    proof = fixture.validator.validate(
        kind=fixture.source.kind,
        payload=fixture.replay_payload,
    )

    assert proof.source_kind == "native"


def test_native_replay_closes_record_shard_prompt_route_and_identity() -> None:
    fixture = _native_record_fixture()

    proof = fixture.validator.validate(
        kind=fixture.source.kind,
        payload=fixture.replay_payload,
    )

    assert proof.source_kind == "native"
    assert proof.attempt_count == 1
    assert proof.record_count == 1


def test_admission_replay_adapter_exposes_attempt_and_routing_authorities() -> None:
    fixture = _native_record_fixture()
    bindings = _ReplayBindingAuthority()
    for artifact in fixture.reader.artifacts.values():
        bindings.bind(artifact)
    read = AdmissionReadPort(
        policies=None,
        approvals=None,
        artifacts=_ReplayArtifactAuthority(fixture.reader),
        refs=None,
        object_bindings=bindings,
        runs=_ReplayRunAuthority(fixture.reader),
        routing=_ReplayRoutingAuthority(fixture.reader),
    )
    validator = ReplayAdmissionValidator(
        _AdmissionReplayReader(
            read=read,
            objects=_ReplayObjectStore(fixture.reader.blobs),  # type: ignore[arg-type]
        )
    )

    proof = validator.validate(
        kind=RunKindRef(kind="review.run", version=1),
        payload=fixture.replay_payload,
    )

    assert proof.source_run_id == fixture.source.run_id
    assert proof.attempt_count == 1
    assert proof.record_count == 1


def test_native_replay_rejects_missing_retained_routing_decision() -> None:
    fixture = _native_record_fixture()
    fixture.reader.routing_decisions.clear()

    with pytest.raises(IntegrityViolation, match="retained RoutingDecision"):
        fixture.validator.validate(
            kind=fixture.source.kind,
            payload=fixture.replay_payload,
        )


def test_native_replay_rejects_attempt_authority_drift() -> None:
    fixture = _native_fixture()
    fixture.reader.attempts.clear()

    with pytest.raises(IntegrityViolation, match="RunAttempt authority"):
        fixture.validator.validate(
            kind=fixture.source.kind,
            payload=fixture.replay_payload,
        )


def test_fabricated_bundle_with_detached_lineage_is_rejected() -> None:
    fixture = _native_fixture()
    detached, detached_blob = _bundle_artifact(fixture.root_bundle, lineage=())
    fixture.reader.artifacts[detached.artifact_id] = detached
    fixture.reader.blobs[detached.artifact_id] = detached_blob
    fixture.reader.runs[SOURCE_RUN_ID] = _replace_run(
        fixture.source,
        terminal_cassette_artifact_id=detached.artifact_id,
    )
    replay = _payload(
        params=fixture.replay_payload.params,
        plan=fixture.replay_payload.execution_version_plan,
        profile=fixture.replay_payload.resolved_profiles[0],
        mode="replay",
        cassette=detached,
    )

    with pytest.raises(IntegrityViolation, match="lineage"):
        fixture.validator.validate(kind=fixture.source.kind, payload=replay)


def test_native_replay_rejects_wrong_terminal_source_binding() -> None:
    fixture = _native_fixture()
    fixture.reader.runs[SOURCE_RUN_ID] = _replace_run(
        fixture.source,
        terminal_cassette_artifact_id="artifact:other-cassette",
    )

    with pytest.raises(IntegrityViolation, match="terminal cassette"):
        fixture.validator.validate(
            kind=fixture.source.kind,
            payload=fixture.replay_payload,
        )


def test_native_replay_rejects_terminal_outcome_drift() -> None:
    fixture = _native_fixture()
    root_bundle = fixture.root_bundle.model_copy(update={"outcome_code": "forged-outcome"})
    identity = fixture.root.meta["execution_identity"]
    forged, forged_blob = _bundle_artifact(
        root_bundle,
        lineage=(fixture.attempt.artifact_id,),
        identity=identity,
    )
    fixture.reader.artifacts[forged.artifact_id] = forged
    fixture.reader.blobs[forged.artifact_id] = forged_blob
    fixture.reader.runs[SOURCE_RUN_ID] = _replace_run(
        fixture.source,
        terminal_cassette_artifact_id=forged.artifact_id,
    )
    replay = _payload(
        params=fixture.replay_payload.params,
        plan=fixture.replay_payload.execution_version_plan,
        profile=fixture.replay_payload.resolved_profiles[0],
        mode="replay",
        cassette=forged,
    )

    with pytest.raises(IntegrityViolation, match="outcome"):
        fixture.validator.validate(kind=fixture.source.kind, payload=replay)


def test_native_replay_rejects_tampered_input_hash() -> None:
    fixture = _native_fixture()
    fixture.reader.blobs[fixture.input_artifact.artifact_id] = b'{"tampered":true}'

    with pytest.raises(IntegrityViolation, match="input Artifact bytes"):
        fixture.validator.validate(
            kind=fixture.source.kind,
            payload=fixture.replay_payload,
        )


def test_replay_version_tuple_must_bind_root_payload_hash() -> None:
    fixture = _native_fixture()
    replay = _replace_payload(
        fixture.replay_payload,
        version_tuple=fixture.replay_payload.version_tuple.model_copy(
            update={"cassette_id": f"sha256:{HEX_B}"}
        ),
    )

    with pytest.raises(IntegrityViolation, match="exact cassette"):
        fixture.validator.validate(kind=fixture.source.kind, payload=replay)


def test_native_replay_rejects_wrong_execution_plan() -> None:
    fixture = _native_fixture()
    wrong_plan = _plan(agent_graph_version="review-graph@2")
    replay = _replace_payload(
        fixture.replay_payload,
        execution_version_plan=wrong_plan,
    )

    with pytest.raises(IntegrityViolation, match="execution plan"):
        fixture.validator.validate(kind=fixture.source.kind, payload=replay)


def test_native_replay_rejects_wrong_resolved_profile() -> None:
    fixture = _native_fixture()
    replay = _replace_payload(
        fixture.replay_payload,
        resolved_profiles=(_profile(version=2),),
    )

    with pytest.raises(IntegrityViolation, match="resolved profiles"):
        fixture.validator.validate(kind=fixture.source.kind, payload=replay)


def test_verified_legacy_replay_closes_authority_tree_and_plan() -> None:
    fixture = _legacy_verified_fixture()

    proof = fixture.validator.validate(
        kind=RunKindRef(kind="review.run", version=1),
        payload=fixture.replay_payload,
    )

    assert proof.source_kind == "legacy_import"
    assert proof.source_run_id is None
    assert proof.legacy_import_id is not None
    assert proof.attempt_count == 1
    assert proof.record_count == 1


def test_verified_legacy_replay_reports_missing_external_authority_as_dependency() -> None:
    fixture = _legacy_verified_fixture()
    validator = ReplayAdmissionValidator(
        fixture.reader,
        legacy_decisions=fixture.decisions,
    )

    with pytest.raises(DependencyUnavailable) as raised:
        validator.resolve_execution_profile_authority(
            kind=RunKindRef(kind="review.run", version=1),
            cassette_artifact_id=fixture.root.artifact_id,
        )

    assert raised.value.context == {"component": "legacy_import_authority"}


def test_verified_legacy_replay_allows_an_uninvoked_conditional_plan_node() -> None:
    fixture = _legacy_verified_fixture()
    extra = PlannedAgentNodeVersionV1(
        agent_node_id="unused-node",
        # A conditional graph node may share the same version projections while
        # remaining absent from this particular cassette's actual identity.
        prompt_version=fixture.plan.nodes[0].prompt_version,
        tool_version=fixture.plan.nodes[0].tool_version,
        allowed_model_snapshots=(fixture.plan.nodes[0].allowed_model_snapshots[0],),
    )
    plan_payload = fixture.plan.model_dump(mode="json", exclude={"plan_digest"})
    plan_payload["nodes"] = [
        fixture.plan.nodes[0].model_dump(mode="json"),
        extra.model_dump(mode="json"),
    ]
    conditional_plan = ExecutionVersionPlanV1(
        **plan_payload,
        plan_digest=execution_version_plan_digest(plan_payload),
    )
    replay = _replace_payload(
        fixture.replay_payload,
        execution_version_plan=conditional_plan,
    )

    proof = fixture.validator.validate(
        kind=RunKindRef(kind="review.run", version=1),
        payload=replay,
    )

    assert proof.source_kind == "legacy_import"
    assert proof.record_count == 1


def test_legacy_evidence_missing_bundle_is_never_executable() -> None:
    fixture = _native_fixture()
    policy_ref = LegacyImportVerificationPolicyRefV1(
        policy_id="legacy-import@1",
        policy_version=1,
        policy_digest=HEX_A,
    )
    manifest = build_legacy_import_manifest(
        source_suite_id="m2-review",
        source_case_id="review-case-1",
        verification_policy=policy_ref,
        input_artifact_bindings=(),
        execution_profile_bindings=(),
        frozen_version_tuple=None,
        policy_bindings=(),
        schema_bindings=(),
        ordered_call_evidence_digests=(),
        execution_identity=None,
        importer_tool_version="legacy-importer@1",
        status="evidence_missing",
    )
    attempt_bundle = CassetteBundleV1(scope="attempt", attempt_no=1)
    attempt, attempt_blob = _bundle_artifact(attempt_bundle, lineage=())
    root_bundle = CassetteBundleV1(
        scope="run",
        child_bundle_artifact_ids=(attempt.artifact_id,),
        legacy_run_import_manifest=manifest,
    )
    root, root_blob = _bundle_artifact(root_bundle, lineage=(attempt.artifact_id,))
    fixture.reader.artifacts.update({attempt.artifact_id: attempt, root.artifact_id: root})
    fixture.reader.blobs.update({attempt.artifact_id: attempt_blob, root.artifact_id: root_blob})
    replay = _payload(
        params=fixture.replay_payload.params,
        plan=fixture.replay_payload.execution_version_plan,
        profile=fixture.replay_payload.resolved_profiles[0],
        mode="replay",
        cassette=root,
    )

    with pytest.raises(IntegrityViolation, match="evidence_missing"):
        ReplayAdmissionValidator(fixture.reader).validate(
            kind=fixture.source.kind,
            payload=replay,
        )
