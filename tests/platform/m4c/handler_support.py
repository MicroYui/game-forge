"""Shared doubles + builders for the M4c Run-handler unit tests (Task 11a).

These construct real ``RunRecord`` / ``RunAttempt`` / ``RunPayloadEnvelope`` /
``ExecutorContext`` objects (so the handlers exercise the true frozen contracts)
around in-memory artifact-store and model-bridge doubles that mirror the
production read/write ports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from gameforge.apps.worker.executor import ExecutorContext
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.ir import Entity, Relation
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    FailureClassifierRefV1,
    PlannedAgentNodeVersionV1,
    RetryPolicyRefV1,
    ResolvedArtifactRequirementV1,
    RunAttempt,
    RunKindPayload,
    RunPayloadEnvelope,
    RunRecord,
    ResolvedPolicySnapshotV1,
    canonical_payload_hash,
    execution_version_plan_digest,
    referenced_input_artifact_ids,
    resolved_policy_snapshot_digest,
)
from gameforge.contracts.lineage import (
    AuditActor,
    ObjectLocation,
    VersionTuple,
    object_ref_for_bytes,
)
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.spine.ir.snapshot import Snapshot

HUMAN = AuditActor(principal_id="human:a", principal_kind="human")
WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")
NOW = "2026-07-15T12:00:00Z"
_HEX = "a" * 64


class FakeArtifactStore:
    """In-memory ``ArtifactBlobReader`` + ``PreparedArtifactStore`` double."""

    def __init__(self) -> None:
        self._by_artifact_id: dict[str, bytes] = {}
        self._by_key: dict[str, bytes] = {}
        self.put_count = 0

    def register(self, artifact_id: str, payload: object) -> str:
        blob = payload if isinstance(payload, (bytes, bytearray)) else _canon(payload)
        self._by_artifact_id[artifact_id] = bytes(blob)
        return artifact_id

    def read_bytes(self, artifact_id: str) -> bytes:
        return self._by_artifact_id[artifact_id]

    def put_prepared(self, payload: bytes):
        self.put_count += 1
        object_ref = object_ref_for_bytes(payload)
        self._by_key[object_ref.key] = bytes(payload)
        location = ObjectLocation(store_id="mem", key=object_ref.key, backend_generation="g1")
        return object_ref, location

    def read_prepared(self, object_ref) -> bytes:
        return self._by_key[object_ref.key]


@dataclass
class FakeBridgeResult:
    response: object
    decision: object
    link: object
    replayed: bool


@dataclass
class _Response:
    response_normalized: str
    execution_source: str = "cassette_replay"
    finish_reason: str = "stop"
    raw_response: dict = field(default_factory=dict)
    tool_calls: tuple = ()
    latency: object = None
    token_usage: object = None


@dataclass
class _Observation:
    status: str = "unavailable"


@dataclass
class _Decision:
    decision_id: str


@dataclass
class _Link:
    call_ordinal: int


class FakeModelBridge:
    """Ordered, replay-only ``WorkerModelBridgePort`` double.

    Serves canned normalized responses in order and records each request so a
    test can assert the ordered, run-scoped call sequence (one ordered cassette).
    """

    def __init__(
        self,
        responses: tuple[str, ...] = (),
        *,
        model_snapshots: dict[str, ModelSnapshot] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._model_snapshots = dict(model_snapshots or {})
        self.requests: list[object] = []

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ) -> ModelSnapshot:
        del catalog_version, catalog_digest
        retained = self._model_snapshots.get(model_snapshot_id)
        if retained is not None:
            return retained.model_copy(deep=True)
        # Existing handler fixtures predate opaque catalog IDs. Keep their explicit
        # test-only provider/model/tag binding here; production code never parses a
        # planned opaque identity and the dedicated bridge test uses the exact
        # catalog resolver.
        parts = model_snapshot_id.split("/")
        if len(parts) != 3 or not all(parts):
            raise ValueError("fake model bridge has no structured snapshot binding")
        return ModelSnapshot(provider=parts[0], model=parts[1], snapshot_tag=parts[2])

    def call_model(self, request: object) -> FakeBridgeResult:
        ordinal = len(self.requests) + 1
        self.requests.append(request)
        text = self._responses[ordinal - 1] if ordinal - 1 < len(self._responses) else "{}"
        response = _Response(
            response_normalized=text,
            latency=_Observation(status="unavailable"),
            token_usage=_Observation(status="unavailable"),
        )
        return FakeBridgeResult(
            response=response,
            decision=_Decision(decision_id=f"decision:{ordinal}"),
            link=_Link(call_ordinal=ordinal),
            replayed=True,
        )


def _canon(payload: object) -> bytes:
    return canonical_json(payload).encode("utf-8")


def snapshot_bytes(entities: list[Entity], relations: list[Relation]) -> bytes:
    """Canonical ``ir_snapshot`` payload bytes for the given IR objects.

    Built through a real :class:`Snapshot` so the exact canonical content payload
    the platform stores is what the handler's ``load_snapshot`` round-trips.
    """

    snapshot = Snapshot(
        {entity.id: entity for entity in entities},
        {relation.id: relation for relation in relations},
    )
    return canonical_json(snapshot.content_payload).encode("utf-8")


def resolved_binding(
    field_path: str,
    *,
    profile_id: str,
    version: int,
    kind: str,
) -> ResolvedExecutionProfileBindingV1:
    return ResolvedExecutionProfileBindingV1(
        field_path=field_path,
        profile=ProfileRefV1(profile_id=profile_id, version=version),
        expected_profile_kind=kind,
        profile_payload_hash=_HEX,
        catalog_version=1,
        catalog_digest=_HEX,
    )


def resolved_policy_snapshot(
    resolved_policy_id: str,
    source_profile_field_path: str,
    requirements: tuple[ResolvedArtifactRequirementV1, ...],
) -> ResolvedPolicySnapshotV1:
    body = {
        "resolved_policy_id": resolved_policy_id,
        "source_profile_field_path": source_profile_field_path,
        "source_profile_payload_hash": _HEX,
        "requirements": [item.model_dump(mode="json") for item in requirements],
    }
    return ResolvedPolicySnapshotV1(
        **body,
        digest=resolved_policy_snapshot_digest(body),
    )


def execution_plan(nodes: dict[str, str]) -> ExecutionVersionPlanV1:
    """Build a valid execution plan mapping ``agent_node_id -> model reference``."""

    planned = tuple(
        PlannedAgentNodeVersionV1(
            agent_node_id=node_id,
            prompt_version="p@1",
            tool_version="t@1",
            allowed_model_snapshots=(reference,),
        )
        for node_id, reference in nodes.items()
    )
    body = {
        "plan_schema_version": "execution-version-plan@1",
        "agent_graph_version": "graph@1",
        "nodes": [node.model_dump(mode="json") for node in planned],
        "model_catalog_version": 1,
        "model_catalog_digest": _HEX,
        "routing_policy_version": 1,
        "routing_policy_digest": _HEX,
    }
    digest = execution_version_plan_digest(body)
    return ExecutionVersionPlanV1(
        agent_graph_version="graph@1",
        nodes=planned,
        model_catalog_version=1,
        model_catalog_digest=_HEX,
        routing_policy_version=1,
        routing_policy_digest=_HEX,
        plan_digest=digest,
    )


def build_envelope(
    *,
    params: RunKindPayload,
    resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...] = (),
    resolved_policy_snapshots: tuple[ResolvedPolicySnapshotV1, ...] = (),
    seed: int | None = None,
    llm_execution_mode: str = "not_applicable",
    plan: ExecutionVersionPlanV1 | None = None,
    cassette_artifact_id: str | None = None,
) -> RunPayloadEnvelope:
    inputs = list(referenced_input_artifact_ids(params))
    if cassette_artifact_id is not None and cassette_artifact_id not in inputs:
        inputs.append(cassette_artifact_id)
    return RunPayloadEnvelope(
        payload_schema_version=params.schema_version,
        input_artifact_ids=tuple(inputs),
        version_tuple=VersionTuple(tool_version="handler@1"),
        execution_version_plan=plan,
        policy_bindings=(),
        schema_bindings=(),
        execution_profile_catalog_version=1,
        execution_profile_catalog_digest=_HEX,
        resolved_profiles=resolved_profiles,
        resolved_policy_snapshots=resolved_policy_snapshots,
        budget_set_snapshot_id="budget-set:1",
        seed=seed,
        llm_execution_mode=llm_execution_mode,
        cassette_artifact_id=cassette_artifact_id,
        params=params,
    )


def build_run_record(
    envelope: RunPayloadEnvelope, kind: RunKindRef, *, run_id: str = "run:1"
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        kind=kind,
        status="running",
        revision=3,
        idempotency_scope="principal:human:a",
        idempotency_key="request:1",
        request_hash=_HEX,
        payload=envelope,
        payload_hash=canonical_payload_hash(envelope),
        run_kind_definition_digest="b" * 64,
        outcome_policy_set_digest="c" * 64,
        failure_classifier=FailureClassifierRefV1(classifier_version=1, classifier_digest="d" * 64),
        initiated_by=HUMAN,
        queue_deadline_utc="2026-07-15T12:10:00Z",
        attempt_timeout_ns=30_000_000_000,
        overall_deadline_utc="2026-07-15T13:00:00Z",
        current_attempt_no=1,
        next_attempt_no=2,
        next_fencing_token=2,
        next_event_seq=5,
        budget_set_snapshot_id=envelope.budget_set_snapshot_id,
        run_budget_hold_group_id="hold:1",
        concurrency_permit_group_id="permit:1",
        retry_policy=RetryPolicyRefV1(
            retry_policy_id="deterministic_job",
            retry_policy_version=1,
            retry_policy_digest="e" * 64,
        ),
        max_attempts=3,
        created_at=NOW,
        updated_at=NOW,
    )


def build_attempt(*, run_id: str = "run:1", attempt_no: int = 1) -> RunAttempt:
    return RunAttempt(
        run_id=run_id,
        attempt_no=attempt_no,
        status="running",
        fencing_token=1,
        worker_principal_id=WORKER.principal_id,
        next_call_ordinal=1,
        started_at=NOW,
        attempt_deadline_utc="2026-07-15T12:30:00Z",
    )


def build_context(
    *,
    params: RunKindPayload,
    kind: RunKindRef,
    resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...] = (),
    resolved_policy_snapshots: tuple[ResolvedPolicySnapshotV1, ...] = (),
    seed: int | None = None,
    llm_execution_mode: str = "not_applicable",
    plan: ExecutionVersionPlanV1 | None = None,
    cassette_artifact_id: str | None = None,
    model_bridge: object | None = None,
) -> ExecutorContext:
    envelope = build_envelope(
        params=params,
        resolved_profiles=resolved_profiles,
        resolved_policy_snapshots=resolved_policy_snapshots,
        seed=seed,
        llm_execution_mode=llm_execution_mode,
        plan=plan,
        cassette_artifact_id=cassette_artifact_id,
    )
    run = build_run_record(envelope, kind)
    attempt = build_attempt()
    return ExecutorContext(
        run=run,
        attempt=attempt,
        payload=envelope,
        deadline_utc=datetime(2026, 7, 15, 12, 30, tzinfo=UTC),
        model_bridge=model_bridge if model_bridge is not None else FakeModelBridge(),
    )


__all__ = [
    "FakeArtifactStore",
    "FakeBridgeResult",
    "FakeModelBridge",
    "HUMAN",
    "NOW",
    "WORKER",
    "build_attempt",
    "build_context",
    "build_envelope",
    "build_run_record",
    "execution_plan",
    "resolved_binding",
    "resolved_policy_snapshot",
    "snapshot_bytes",
]
