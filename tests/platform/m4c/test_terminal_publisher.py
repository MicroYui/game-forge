"""End-to-end tests for the generic terminal publication engine (M4c Task 9).

These exercise ``TerminalPublisher`` — the concrete
``RunLifecyclePublicationGateway`` — against the real registry policy objects from
``gameforge.platform.registry.defaults`` and in-memory transaction-bound ports that
mirror the production write pattern (Artifact rows / findings / links / audit).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.findings import FindingPayloadV1, FindingRevisionV1
from gameforge.contracts.identity import Permission
from gameforge.contracts.jobs import (
    FailureClassifierRefV1,
    PreparedArtifact,
    PreparedFindingV1,
    PreparedRunFailure,
    PreparedRunResult,
    PreparedRunResultSummaryV1,
    RetryDecisionV1,
    RetryPolicyRefV1,
    RunAttempt,
    RunKindDefinition,
    RunRecord,
    TerminalPublisherHooks,
    canonical_payload_hash,
    outcome_policy_set_digest,
    run_kind_definition_digest,
)
from gameforge.contracts.lineage import (
    ArtifactV1,
    AuditActor,
    ObjectLocation,
    VersionTuple,
    object_ref_for_bytes,
)
from gameforge.platform.publication import TerminalPublisher
from gameforge.platform.registry.defaults import (
    _common_failure_policies,
    _failure_classifier,
    _finding_policies,
    _finding_ref,
    _OutcomeBuilder,
    _runtime_parent_ref,
    _runtime_parent_rules,
    _simple_primary_policy,
    _transition_policy,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.runs.lifecycle import select_outcome_policy
from tests.platform.m4.test_run_create_claim import _payload, _retry_policy


WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")
HUMAN = AuditActor(principal_id="human:a", principal_kind="human")
NOW = "2026-07-14T12:00:00Z"


# --------------------------------------------------------------------------- fakes
class _Blobs:
    def __init__(self) -> None:
        self._by_key: dict[str, bytes] = {}

    def register(self, blob: bytes):
        ref = object_ref_for_bytes(blob)
        self._by_key[ref.key] = blob
        return ref

    def read(self, object_ref):
        return self._by_key[object_ref.key]

    def put(self, payload: bytes):
        ref = object_ref_for_bytes(payload)
        self._by_key[ref.key] = payload
        return ref


class _Artifacts:
    def __init__(self) -> None:
        self.by_id: dict[str, object] = {}
        self.put_order: list[str] = []

    def add(self, artifact) -> None:
        self.by_id[artifact.artifact_id] = artifact

    def get(self, artifact_id: str):
        return self.by_id.get(artifact_id)

    def put(self, artifact):
        existing = self.by_id.get(artifact.artifact_id)
        if existing is not None:
            return existing
        self.by_id[artifact.artifact_id] = artifact
        self.put_order.append(artifact.artifact_id)
        return artifact


class _Findings:
    def __init__(self) -> None:
        self.current: dict[str, int] = {}
        self.revisions: list[FindingRevisionV1] = []

    def put(self, revision: FindingRevisionV1, *, expected_current_revision):
        current = self.current.get(revision.finding_id)
        if (current or None) != (expected_current_revision or None):
            raise Conflict("finding head compare-and-set failed")
        self.current[revision.finding_id] = revision.revision
        self.revisions.append(revision)
        return revision


@dataclass
class _Ledger:
    closed: tuple[tuple[int, str], ...] = ()
    links: list = field(default_factory=list)

    def prompt_links(self, run_id, *, attempt_no):
        return ()

    def closed_attempt_failures(self, run_id):
        return self.closed

    def put_finding_link(self, link):
        self.links.append(link)


class _Audit:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, **kwargs) -> None:
        self.records.append(kwargs)


# ------------------------------------------------------------------------ fixtures
def _registry_and_definition() -> tuple[ImmutablePlatformRegistry, RunKindDefinition]:
    attempt_transition = _transition_policy(scope="attempt")
    run_transition = _transition_policy(scope="run")
    builder = _OutcomeBuilder(attempt_transition=attempt_transition, run_transition=run_transition)
    success = _simple_primary_policy(
        builder,
        policy_id="checker-completed",
        outcome_code="checker_completed",
        artifact_kind="checker_run",
        payload_schema_id="checker-report@1",
    )
    failures = _common_failure_policies(builder, validation_workflow=False)
    runtime = _runtime_parent_rules()
    finding_policies = _finding_policies()
    checker_findings = next(p for p in finding_policies if p.policy_id == "checker-findings")
    classifier = _failure_classifier()
    retry = _retry_policy()

    definition = RunKindDefinition(
        kind="checker.run",
        version=1,
        status="active",
        payload_schema_id="checker-run@1",
        prepared_result_schema_id="prepared-run-result@1",
        prepared_failure_schema_id="prepared-run-failure@1",
        result_schema_id="run-result@1",
        failure_schema_id="run-failure@1",
        outcome_policies=(success, *failures),
        runtime_parent_rule_set=_runtime_parent_ref(runtime),
        finding_output_policy_ref=_finding_ref(checker_findings),
        allowed_command_schema_ids=(),
        creation_mode="generic_runs_endpoint",
        allowed_llm_execution_modes=("not_applicable",),
        seed_policy="forbidden",
        required_permission=Permission(action="run.create", resource_kind="run", domain_scope=None),
        executor_key="checker@1",
        terminal_hooks=TerminalPublisherHooks(
            on_success="publish-checker@1",
            on_failure="publish-failure@1",
            on_cancel="publish-cancel@1",
            on_timeout="publish-timeout@1",
        ),
        failure_classifier=FailureClassifierRefV1(
            classifier_version=classifier.classifier_version,
            classifier_digest=classifier.classifier_digest,
        ),
        retry_policy=RetryPolicyRefV1(
            retry_policy_id=retry.retry_policy_id,
            retry_policy_version=retry.retry_policy_version,
            retry_policy_digest=retry.retry_policy_digest,
        ),
    )
    registry = ImmutablePlatformRegistry(
        run_kinds=(definition,),
        retry_policies=(retry,),
        failure_classifiers=(classifier,),
        lineage_policies=tuple(builder.lineage_policies.values()),
        version_transition_policies=(attempt_transition, run_transition),
        runtime_parent_rule_sets=(runtime,),
        finding_output_policies=finding_policies,
        run_event_registries=(),
        completion_oracle_registries=(),
    )
    return registry, definition


def _run_record(definition: RunKindDefinition) -> RunRecord:
    payload = _payload()
    run_kind = RunKindRef(kind="checker.run", version=1)
    return RunRecord(
        run_id="run:1",
        kind=run_kind,
        status="running",
        revision=3,
        idempotency_scope="principal:human:a",
        idempotency_key="request:1",
        request_hash="a" * 64,
        payload=payload,
        payload_hash=canonical_payload_hash(payload),
        run_kind_definition_digest=run_kind_definition_digest(definition),
        outcome_policy_set_digest=outcome_policy_set_digest(run_kind, definition.outcome_policies),
        failure_classifier=definition.failure_classifier,
        initiated_by=HUMAN,
        queue_deadline_utc="2026-07-14T12:10:00Z",
        attempt_timeout_ns=30_000_000_000,
        overall_deadline_utc="2026-07-14T13:00:00Z",
        current_attempt_no=1,
        next_attempt_no=2,
        next_fencing_token=2,
        next_event_seq=5,
        budget_set_snapshot_id=payload.budget_set_snapshot_id,
        run_budget_hold_group_id="hold:1",
        concurrency_permit_group_id="permit:1",
        retry_policy=definition.retry_policy,
        max_attempts=3,
        created_at=NOW,
        updated_at=NOW,
    )


def _attempt() -> RunAttempt:
    return RunAttempt(
        run_id="run:1",
        attempt_no=1,
        status="running",
        fencing_token=1,
        worker_principal_id=WORKER.principal_id,
        next_call_ordinal=1,
        started_at=NOW,
        attempt_deadline_utc="2026-07-14T12:30:00Z",
    )


def _input_snapshot(artifacts: _Artifacts) -> None:
    artifacts.add(
        ArtifactV1(
            artifact_id="artifact:input",
            kind="ir_snapshot",
            version_tuple=VersionTuple(ir_snapshot_id="snapshot:input"),
            lineage=[],
            payload_hash=None,
            meta={"payload_schema_id": "ir-core@1"},
        )
    )


def _checker_artifact(blobs: _Blobs) -> PreparedArtifact:
    blob = json.dumps({"payload_schema_version": "checker-report@1", "findings": []}).encode()
    object_ref = blobs.register(blob)
    return PreparedArtifact(
        kind="checker_run",
        payload_schema_id="checker-report@1",
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:input", tool_version="checker@1"),
        lineage=("artifact:input",),
        payload_hash=object_ref.sha256,
        meta={},
        object_ref=object_ref,
        location=ObjectLocation(store_id="s3", key=object_ref.key, backend_generation="g1"),
    )


def _checker_finding() -> PreparedFindingV1:
    return PreparedFindingV1(
        finding_id="finding:1",
        expected_previous_revision=None,
        evidence_artifact_index=0,
        payload=FindingPayloadV1(
            source="checker",
            producer_id="checker@1",
            producer_run_id="run:1",
            oracle_type="deterministic",
            defect_class="dangling_ref",
            severity="major",
            snapshot_id="snapshot:input",
            status="confirmed",
            message="dangling reference",
        ),
    )


def _prepared_success(*, artifacts, findings=()) -> PreparedRunResult:
    return PreparedRunResult(
        run_id="run:1",
        attempt_no=1,
        run_kind=RunKindRef(kind="checker.run", version=1),
        primary_index=0,
        artifacts=artifacts,
        findings=findings,
        requirement_dispositions=(),
        summary=PreparedRunResultSummaryV1(
            outcome_code="checker_completed",
            primary_artifact_kind="checker_run",
            prepared_domain_artifact_count=len(artifacts),
            prepared_finding_count=len(findings),
        ),
    )


def _publisher(registry, artifacts, blobs, findings, ledger, audit) -> TerminalPublisher:
    return TerminalPublisher(
        registry=registry,
        artifacts=artifacts,
        blobs=blobs,
        findings=findings,
        ledger=ledger,
        audit=audit,
    )


# ---------------------------------------------------------------------------- tests
def test_success_publishes_artifact_finding_link_and_manifest():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs, findings, ledger, audit = (
        _Artifacts(),
        _Blobs(),
        _Findings(),
        _Ledger(),
        _Audit(),
    )
    _input_snapshot(artifacts)
    checker = _checker_artifact(blobs)
    prepared = _prepared_success(artifacts=(checker,), findings=(_checker_finding(),))
    policy = select_outcome_policy(
        definition=definition,
        outcome_code="checker_completed",
        prepared_outcome="success",
        publication_scope="run",
        run_status="succeeded",
        attempt_status=None,
        failure_class=None,
        retry_disposition=None,
    )

    publisher = _publisher(registry, artifacts, blobs, findings, ledger, audit)
    published = publisher.publish_run_result(
        run=run, attempt=attempt, prepared=prepared, policy=policy, occurred_at=NOW, actor=WORKER
    )

    # A domain checker Artifact + the run_result manifest were both published.
    kinds = {artifact.kind for artifact in artifacts.by_id.values()}
    assert "checker_run" in kinds
    manifest = artifacts.by_id[published.result_artifact_id]
    assert manifest.kind == "run_result"
    result_payload = json.loads(blobs.read(manifest.object_ref).decode())
    assert result_payload["outcome_code"] == "checker_completed"
    assert result_payload["finding_count"] == 1
    assert result_payload["primary_artifact_id"] in result_payload["produced_artifact_ids"]
    assert len(result_payload["produced_artifact_ids"]) == 1
    # The domain artifact is a run_published output parent of the manifest.
    assert result_payload["primary_artifact_id"] in manifest.lineage
    assert "artifact:input" in manifest.lineage
    # Finding revision + link were written in the same UoW.
    assert findings.current == {"finding:1": 1}
    assert [link.finding_id for link in ledger.links] == ["finding:1"]
    assert ledger.links[0].ordinal == 1
    assert ledger.links[0].evidence_artifact_id == result_payload["primary_artifact_id"]
    assert published.terminal_cassette_artifact_id is None
    # Audit recorded the on_success terminal hook.
    assert any(record["action"] == "publish-checker@1" for record in audit.records)


def test_success_rejects_fabricated_prepared_count():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    checker = _checker_artifact(blobs)
    prepared = _prepared_success(artifacts=(checker,)).model_copy(
        update={
            "summary": PreparedRunResultSummaryV1(
                outcome_code="checker_completed",
                primary_artifact_kind="checker_run",
                prepared_domain_artifact_count=2,  # lies about the count
                prepared_finding_count=0,
            ),
        }
    )
    policy = _success_policy(definition)
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )


def test_success_rejects_tampered_blob_hash():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    ref_a = blobs.register(json.dumps({"payload_schema_version": "checker-report@1"}).encode())
    ref_b = blobs.register(
        json.dumps({"payload_schema_version": "checker-report@1", "x": 1}).encode()
    )
    # Declared payload_hash points at a different blob than the stored object.
    checker = PreparedArtifact(
        kind="checker_run",
        payload_schema_id="checker-report@1",
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:input", tool_version="checker@1"),
        lineage=("artifact:input",),
        payload_hash=ref_b.sha256,
        meta={},
        object_ref=ref_a,
        location=ObjectLocation(store_id="s3", key=ref_a.key, backend_generation="g1"),
    )
    prepared = _prepared_success(artifacts=(checker,))
    policy = _success_policy(definition)
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )


def test_two_scope_failure_aggregates_closed_attempts_without_business_evidence():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs, audit = _Artifacts(), _Blobs(), _Audit()
    # A prior attempt already published its own attempt-scope failure manifest.
    ledger = _Ledger(closed=((1, "artifact:prior-attempt-failure"),))
    prepared = PreparedRunFailure(
        run_id="run:1",
        attempt_no=1,
        run_kind=RunKindRef(kind="checker.run", version=1),
        artifacts=(),
        requirement_dispositions=(),
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        classifier=definition.failure_classifier,
        redacted_message="boom",
    )
    retry_decision = RetryDecisionV1(
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="not_retry_eligible",
        classifier=definition.failure_classifier,
        retry_policy=definition.retry_policy,
        evaluated_at_utc=NOW,
    )
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

    publisher = _publisher(registry, artifacts, blobs, _Findings(), ledger, audit)
    attempt_pub = publisher.publish_attempt_failure(
        run=run,
        attempt=attempt,
        prepared=prepared,
        retry_decision=retry_decision,
        policy=attempt_policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    # Attempt-scope failure carries no business evidence.
    attempt_manifest = artifacts.by_id[attempt_pub.failure_artifact_id]
    attempt_payload = json.loads(blobs.read(attempt_manifest.object_ref).decode())
    assert attempt_payload["evidence_artifact_ids"] == []
    assert attempt_payload["version_projection"]["manifest_scope"] == "attempt"

    run_pub = publisher.publish_run_failure(
        run=run,
        attempt=attempt,
        prepared=prepared,
        retry_decision=retry_decision,
        policy=run_policy,
        attempt_failure_artifact_id=attempt_pub.failure_artifact_id,
        occurred_at=NOW,
        actor=WORKER,
    )
    run_manifest = artifacts.by_id[run_pub.failure_artifact_id]
    run_payload = json.loads(blobs.read(run_manifest.object_ref).decode())
    # The run aggregate references both the prior and the current attempt manifest
    # exactly once, and never itself.
    evidence = run_payload["evidence_artifact_ids"]
    assert evidence.count("artifact:prior-attempt-failure") == 1
    assert evidence.count(attempt_pub.failure_artifact_id) == 1
    assert run_pub.failure_artifact_id not in run_manifest.lineage
    assert run_payload["version_projection"]["manifest_scope"] == "run"


def test_run_failure_rejects_double_aggregating_current_attempt():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    # The ledger already lists the same failure id the caller passes as current.
    ledger = _Ledger(closed=((1, "artifact:dupe-failure"),))
    prepared = PreparedRunFailure(
        run_id="run:1",
        attempt_no=1,
        run_kind=RunKindRef(kind="checker.run", version=1),
        artifacts=(),
        requirement_dispositions=(),
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        classifier=definition.failure_classifier,
        redacted_message="boom",
    )
    retry_decision = RetryDecisionV1(
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="not_retry_eligible",
        classifier=definition.failure_classifier,
        retry_policy=definition.retry_policy,
        evaluated_at_utc=NOW,
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
    publisher = _publisher(registry, _Artifacts(), _Blobs(), _Findings(), ledger, _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_run_failure(
            run=run,
            attempt=attempt,
            prepared=prepared,
            retry_decision=retry_decision,
            policy=run_policy,
            attempt_failure_artifact_id="artifact:dupe-failure",
            occurred_at=NOW,
            actor=WORKER,
        )


def _success_policy(definition: RunKindDefinition):
    return select_outcome_policy(
        definition=definition,
        outcome_code="checker_completed",
        prepared_outcome="success",
        publication_scope="run",
        run_status="succeeded",
        attempt_status=None,
        failure_class=None,
        retry_disposition=None,
    )
