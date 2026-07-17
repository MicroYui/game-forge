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

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.findings import FindingPayloadV1, FindingRevisionV1
from gameforge.contracts.identity import Permission
from gameforge.contracts.jobs import (
    DependencyFailureV1,
    FailureClassifierRefV1,
    PreparedArtifact,
    PreparedFindingV1,
    PreparedRunFailure,
    PreparedRunResult,
    PreparedRunResultSummaryV1,
    RequirementDispositionV1,
    RetryDecisionV1,
    RetryPolicyRefV1,
    RunAttempt,
    RunKindDefinition,
    RunFailureV1,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
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
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.platform.publication import TerminalPublisher
from gameforge.platform.terminal_staging import (
    StagedReceipt,
    StagedTerminalPublication,
)
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
from gameforge.platform.runs.lifecycle import (
    AttemptFailurePublication,
    RunFailurePublication,
    select_outcome_policy,
)
from tests.platform.m4.test_run_create_claim import _payload, _retry_policy


WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")
HUMAN = AuditActor(principal_id="human:a", principal_kind="human")
NOW = "2026-07-14T12:00:00Z"


# --------------------------------------------------------------------------- fakes
class _Blobs:
    def __init__(self) -> None:
        self._by_key: dict[str, bytes] = {}
        self._locations: dict[str, ObjectLocation] = {}

    def register(self, blob: bytes):
        ref = object_ref_for_bytes(blob)
        self._by_key[ref.key] = blob
        self._locations[ref.key] = ObjectLocation(
            store_id="s3", key=ref.key, backend_generation="g1"
        )
        return ref

    def read(self, object_ref, location=None):
        if location is not None and self._locations.get(object_ref.key) != location:
            raise IntegrityViolation("prepared blob location is not the registered location")
        return self._by_key[object_ref.key]

    def put(self, payload: bytes):
        ref = object_ref_for_bytes(payload)
        self._by_key[ref.key] = payload
        self._locations[ref.key] = ObjectLocation(
            store_id="s3", key=ref.key, backend_generation="g1"
        )
        return ref


class _Artifacts:
    def __init__(self) -> None:
        self.by_id: dict[str, object] = {}
        self.payloads_by_id: dict[str, bytes] = {}
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

    def put_staged(self, artifact, receipt):
        assert receipt.ref == artifact.object_ref
        return self.put(artifact)

    def read_bytes(self, artifact_id: str) -> bytes:
        blobs = getattr(self, "_blobs", None)
        if blobs is None:
            raise KeyError(artifact_id)
        artifact = self.by_id[artifact_id]
        object_ref = getattr(artifact, "object_ref", None)
        if object_ref is None:
            return self.payloads_by_id[artifact_id]
        return blobs.read(object_ref)


class _ReplacingArtifacts(_Artifacts):
    def put(self, artifact):
        if artifact.kind == "checker_run":
            return ArtifactV1(
                artifact_id="artifact:replacement",
                kind="checker_run",
                version_tuple=artifact.version_tuple,
                lineage=[],
                payload_hash=None,
                meta={"payload_schema_id": "checker-report@1"},
            )
        return super().put(artifact)


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

    def tool_intermediate_links(self, run_id, *, attempt_no):
        return ()

    def closed_attempt_failures(self, run_id):
        return self.closed

    def put_finding_link(self, link):
        self.links.append(link)
        return link


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


def _closed_attempt_failure(
    artifacts: _Artifacts,
    blobs: _Blobs,
    definition: RunKindDefinition,
    *,
    attempt_no: int,
) -> str:
    run = _run_record(definition)
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
    projection = RunManifestVersionProjectionV1(
        manifest_scope="attempt",
        attempt_no=attempt_no,
        run_kind=run.kind,
        run_payload_hash=run.payload_hash,
        frozen_input_version_tuple=run.payload.version_tuple,
        terminal_version_tuple=run.payload.version_tuple,
        version_transition_policy_ref=policy.version_transition_policy_ref,
        parents=(
            RunManifestParentBindingV1(
                artifact_id="artifact:input",
                role="input",
                publication="existing",
            ),
        ),
    )
    failure = RunFailureV1(
        run_id=run.run_id,
        attempt_no=attempt_no,
        run_kind=run.kind,
        cause_code="execution_failed",
        failure_class="execution",
        retryable=False,
        retry_decision=_terminal_decision(definition),
        redacted_message="prior attempt failed",
        evidence_artifact_ids=(),
        requirement_dispositions=(),
        occurred_at=NOW,
        version_projection=projection,
    )
    blob = canonical_json(failure.model_dump(mode="json")).encode()
    object_ref = blobs.register(blob)
    artifact = build_artifact_v2(
        kind="run_failure",
        version_tuple=projection.terminal_version_tuple,
        lineage=("artifact:input",),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={
            "manifest_scope": "attempt",
            "attempt_no": attempt_no,
            "payload_schema_id": "run-failure@1",
            "replayability": "deterministic_recompute",
        },
        created_at=NOW,
    )
    artifacts.add(artifact)
    return artifact.artifact_id


def _checker_artifact(blobs: _Blobs) -> PreparedArtifact:
    blob = canonical_json(
        {
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot:input",
            "checker_ids": ["graph"],
            "defect_classes": ["dangling_ref"],
            "constraint_application": [],
            "findings": [],
        }
    ).encode()
    object_ref = blobs.register(blob)
    return PreparedArtifact(
        kind="checker_run",
        payload_schema_id="checker-report@1",
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:input", tool_version="checker@1"),
        lineage=("artifact:input",),
        payload_hash=object_ref.sha256,
        meta={"payload_schema_id": "checker-report@1"},
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


class _DirectPublisherHarness:
    """Unit-only explicit plan -> fake stage -> commit driver."""

    def __init__(self, publisher: TerminalPublisher, blobs: _Blobs) -> None:
        self._publisher = publisher
        self._blobs = blobs

    def __getattr__(self, name):
        return getattr(self._publisher, name)

    def _stage(self, draft):
        receipts = []
        for material in draft.materials:
            ref = self._blobs.put(material.payload)
            assert ref == material.expected_ref
            receipts.append(
                StagedReceipt(
                    slot=material.slot,
                    ref=ref,
                    location=self._blobs._locations[ref.key],  # noqa: SLF001
                )
            )
        return StagedTerminalPublication(
            projection_digest=draft.projection_digest,
            receipts=tuple(receipts),
        )

    def publish_run_result(self, **kwargs):
        draft = self._publisher.plan_run_result(**kwargs)
        return self._publisher.commit(draft, self._stage(draft))

    def publish_attempt_failure(self, **kwargs):
        draft = self._publisher.plan_attempt_failure(**kwargs)
        return self._publisher.commit(draft, self._stage(draft))

    def publish_run_failure(self, **kwargs):
        draft = self._publisher.plan_run_failure(**kwargs)
        return self._publisher.commit(draft, self._stage(draft))


def _publisher(
    registry,
    artifacts,
    blobs,
    findings,
    ledger,
    audit,
    **publisher_kwargs,
) -> _DirectPublisherHarness:
    artifacts._blobs = blobs
    return _DirectPublisherHarness(
        TerminalPublisher(
            registry=registry,
            artifacts=artifacts,
            blobs=blobs,
            findings=findings,
            ledger=ledger,
            audit=audit,
            **publisher_kwargs,
        ),
        blobs,
    )


# ---------------------------------------------------------------------------- tests
def test_non_validation_outcome_preflight_is_identity():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())

    assert (
        publisher.preflight_outcome(
            run=run,
            attempt=attempt,
            prepared=prepared,
        )
        is prepared
    )


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


def test_checker_publication_rejects_forged_constraint_id_against_exact_parent_blob():
    registry, definition = _registry_and_definition()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)

    constraint_payload = {
        "dsl_grammar_version": "dsl@1",
        "constraints": [
            {
                "id": "constraint:real",
                "dsl_grammar_version": "dsl@1",
                "kind": "structural",
                "oracle": "deterministic",
                "predicates": [],
                "assert_": "true",
                "severity": "major",
            }
        ],
    }
    constraint_ref = blobs.register(canonical_json(constraint_payload).encode())
    constraint_snapshot_id = "constraint:snapshot:1"
    constraint_artifact = build_artifact_v2(
        kind="constraint_snapshot",
        version_tuple=VersionTuple(constraint_snapshot_id=constraint_snapshot_id),
        lineage=(),
        payload_hash=constraint_ref.sha256,
        object_ref=constraint_ref,
        meta={"payload_schema_id": "constraint-snapshot@1"},
        created_at=NOW,
    )
    artifacts.add(constraint_artifact)

    base_run = _run_record(definition)
    params = base_run.payload.params.model_copy(
        update={"constraint_snapshot_artifact_id": constraint_artifact.artifact_id}
    )
    envelope = base_run.payload.model_copy(
        update={
            "input_artifact_ids": ("artifact:input", constraint_artifact.artifact_id),
            "version_tuple": base_run.payload.version_tuple.model_copy(
                update={"constraint_snapshot_id": constraint_snapshot_id}
            ),
            "params": params,
        }
    )
    run = base_run.model_copy(
        update={"payload": envelope, "payload_hash": canonical_payload_hash(envelope)}
    )

    checker_blob = canonical_json(
        {
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot:input",
            "checker_ids": ["graph"],
            "defect_classes": ["dangling_ref"],
            "constraint_application": [
                {
                    "constraint_id": "constraint:forged",
                    "checker_id": "graph",
                    "status": "executed",
                }
            ],
            "findings": [],
        }
    ).encode()
    checker_ref = blobs.register(checker_blob)
    checker = PreparedArtifact(
        kind="checker_run",
        payload_schema_id="checker-report@1",
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:input",
            constraint_snapshot_id=constraint_snapshot_id,
            tool_version="checker@1",
        ),
        lineage=("artifact:input", constraint_artifact.artifact_id),
        payload_hash=checker_ref.sha256,
        meta={"payload_schema_id": "checker-report@1"},
        object_ref=checker_ref,
        location=ObjectLocation(store_id="s3", key=checker_ref.key, backend_generation="g1"),
    )
    prepared = _prepared_success(artifacts=(checker,))
    policy = _success_policy(definition)
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())

    with pytest.raises(IntegrityViolation, match="authoritative semantic binding"):
        publisher.publish_run_result(
            run=run,
            attempt=_attempt(),
            prepared=prepared,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )
    assert artifacts.put_order == []


def test_workflow_effect_receives_the_final_resealed_primary_payload(monkeypatch):
    from gameforge.platform.publication import publisher as publisher_module

    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
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
    real_bind = publisher_module.bind_final_payload_references

    def reseal_with_schema_valid_content(**kwargs):
        payload = real_bind(**kwargs)
        payload["findings"] = [
            {
                "id": "finding:resealed",
                "finding_schema_version": "finding@1",
                "source": "checker",
                "producer_id": "checker@1",
                "producer_run_id": run.run_id,
                "oracle_type": "deterministic",
                "defect_class": "dangling_ref",
                "severity": "major",
                "snapshot_id": "snapshot:input",
                "entities": [],
                "relations": [],
                "evidence": {},
                "minimal_repro": {},
                "status": "confirmed",
                "message": "publisher reseal plumbing",
            }
        ]
        return payload

    effect_payloads: list[dict[str, object] | None] = []
    monkeypatch.setattr(
        publisher_module,
        "bind_final_payload_references",
        reseal_with_schema_valid_content,
    )
    monkeypatch.setattr(
        publisher_module,
        "apply_workflow_effect",
        lambda _key, context: effect_payloads.append(context.published_primary_payload),
    )

    published = _publisher(
        registry, artifacts, blobs, _Findings(), _Ledger(), _Audit()
    ).publish_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    manifest = artifacts.by_id[published.result_artifact_id]
    result = json.loads(blobs.read(manifest.object_ref))
    primary = artifacts.by_id[result["primary_artifact_id"]]
    final_blob_payload = json.loads(blobs.read(primary.object_ref))

    assert effect_payloads == [final_blob_payload]
    assert final_blob_payload != json.loads(blobs.read(prepared.artifacts[0].object_ref))


def test_deterministic_republication_reuses_immutable_artifacts_across_timestamps():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())

    first = publisher.publish_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=_success_policy(definition),
        occurred_at=NOW,
        actor=WORKER,
    )
    second = publisher.publish_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=_success_policy(definition),
        occurred_at="2026-07-14T12:00:01Z",
        actor=WORKER,
    )

    assert second == first
    assert artifacts.by_id[first.result_artifact_id].created_at == NOW


def test_finding_snapshot_must_match_its_exact_evidence_artifact():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    checker = _checker_artifact(blobs)
    forged = _checker_finding().model_copy(
        update={
            "payload": _checker_finding().payload.model_copy(
                update={"snapshot_id": "snapshot:forged"}
            )
        }
    )
    prepared = _prepared_success(artifacts=(checker,), findings=(forged,))
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=_success_policy(definition),
            occurred_at=NOW,
            actor=WORKER,
        )


def test_retry_then_success_aggregates_every_prior_attempt_failure():
    registry, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(
        update={"current_attempt_no": 2, "next_attempt_no": 3, "next_fencing_token": 3}
    )
    attempt = _attempt().model_copy(update={"attempt_no": 2, "fencing_token": 2})
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    checker = _checker_artifact(blobs)
    prepared = _prepared_success(artifacts=(checker,)).model_copy(update={"attempt_no": 2})
    prior_failure_id = _closed_attempt_failure(artifacts, blobs, definition, attempt_no=1)
    ledger = _Ledger(closed=((1, prior_failure_id),))

    published = _publisher(
        registry, artifacts, blobs, _Findings(), ledger, _Audit()
    ).publish_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=_success_policy(definition),
        occurred_at=NOW,
        actor=WORKER,
    )

    manifest = artifacts.by_id[published.result_artifact_id]
    payload = json.loads(blobs.read(manifest.object_ref).decode())
    assert payload["produced_artifact_ids"].count(prior_failure_id) == 1
    assert manifest.lineage.count(prior_failure_id) == 1


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


@pytest.mark.parametrize(
    "prepared_update",
    (
        {"primary_index": 99},
        {
            "summary": PreparedRunResultSummaryV1(
                outcome_code="checker_completed",
                primary_artifact_kind="simulation_run",
                prepared_domain_artifact_count=1,
                prepared_finding_count=0,
            )
        },
    ),
)
def test_success_rejects_fabricated_primary_projection(prepared_update):
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),)).model_copy(
        update=prepared_update
    )
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())

    with pytest.raises(IntegrityViolation):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=_success_policy(definition),
            occurred_at=NOW,
            actor=WORKER,
        )


def test_success_rejects_fabricated_disposition_without_subset_policy():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    checker = _checker_artifact(blobs)
    prepared = _prepared_success(artifacts=(checker,)).model_copy(
        update={
            "requirement_dispositions": (
                RequirementDispositionV1(
                    resolved_policy_id="fabricated",
                    outcome_rule_id="checker",
                    requirement_id="ghost",
                    status="not_executed",
                    reason_code="search_exhausted",
                ),
            )
        }
    )
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=_success_policy(definition),
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


def test_success_rejects_fabricated_prepared_location():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    original = _checker_artifact(blobs)
    checker = original.model_copy(
        update={
            "location": ObjectLocation(
                store_id="forged-store",
                key=original.object_ref.key,
                backend_generation="forged-generation",
            )
        }
    )
    prepared = _prepared_success(artifacts=(checker,))
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=_success_policy(definition),
            occurred_at=NOW,
            actor=WORKER,
        )


def test_success_rejects_artifact_port_identity_substitution():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _ReplacingArtifacts(), _Blobs()
    _input_snapshot(artifacts)
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=_success_policy(definition),
            occurred_at=NOW,
            actor=WORKER,
        )


def test_two_scope_failure_aggregates_closed_attempts_without_business_evidence():
    registry, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(
        update={"current_attempt_no": 2, "next_attempt_no": 3, "next_fencing_token": 3}
    )
    attempt = _attempt().model_copy(update={"attempt_no": 2, "fencing_token": 2})
    artifacts, blobs, audit = _Artifacts(), _Blobs(), _Audit()
    # A prior attempt already published its own attempt-scope failure manifest.
    prior_failure_id = _closed_attempt_failure(artifacts, blobs, definition, attempt_no=1)
    ledger = _Ledger(closed=((1, prior_failure_id),))
    prepared = PreparedRunFailure(
        run_id="run:1",
        attempt_no=2,
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
    assert evidence.count(prior_failure_id) == 1
    assert evidence.count(attempt_pub.failure_artifact_id) == 1
    assert run_pub.failure_artifact_id not in run_manifest.lineage
    assert run_payload["version_projection"]["manifest_scope"] == "run"


def test_two_scope_failure_is_planned_and_committed_as_one_blob_first_aggregate():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    ledger = _Ledger()
    prepared = _execution_failure(definition)
    decision = _terminal_decision(definition)
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
    harness = _publisher(registry, artifacts, blobs, _Findings(), ledger, _Audit())
    publisher = harness._publisher  # noqa: SLF001 - explicit three-phase test

    drafts = publisher.plan_active_failure_aggregate(
        run=run,
        attempt=attempt,
        prepared=prepared,
        retry_decision=decision,
        attempt_policy=attempt_policy,
        run_policy=run_policy,
        occurred_at=NOW,
        actor=WORKER,
    )

    assert len(drafts) == 2
    attempt_result = drafts[0].result
    run_result = drafts[1].result
    assert isinstance(attempt_result, AttemptFailurePublication)
    assert isinstance(run_result, RunFailurePublication)
    assert attempt_result.failure_artifact_id not in artifacts.by_id
    run_manifest_write = next(
        operation
        for operation in drafts[1].operations
        if getattr(operation, "artifact", None) is not None
        and operation.artifact.artifact_id == run_result.failure_artifact_id
    )
    assert attempt_result.failure_artifact_id in run_manifest_write.artifact.lineage

    staged = tuple(harness._stage(draft) for draft in drafts)  # noqa: SLF001
    committed = publisher.commit_many(tuple(zip(drafts, staged, strict=True)))

    assert committed == (attempt_result, run_result)
    assert attempt_result.failure_artifact_id in artifacts.by_id
    assert run_result.failure_artifact_id in artifacts.by_id


def test_commit_rejects_nested_operation_mutation_after_draft_digest():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    harness = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())
    publisher = harness._publisher  # noqa: SLF001 - explicit three-phase test
    draft = publisher.plan_run_result(
        run=run,
        attempt=attempt,
        prepared=_prepared_success(artifacts=(_checker_artifact(blobs),)),
        policy=_success_policy(definition),
        occurred_at=NOW,
        actor=WORKER,
    )
    staged = harness._stage(draft)  # noqa: SLF001
    workflow = next(
        operation
        for operation in draft.operations
        if getattr(operation, "context", None) is not None
    )
    assert isinstance(workflow.context.published_primary_payload, dict)
    workflow.context.published_primary_payload["snapshot_id"] = "snapshot:mutated"

    with pytest.raises(IntegrityViolation, match="mutated after projection"):
        publisher.commit(draft, staged)

    assert set(artifacts.by_id) == {"artifact:input"}


def test_retry_wait_timeout_uses_closed_attempt_only_for_manifest_projection():
    """A latest closed attempt is not an attempt-status transition authority."""

    registry, definition = _registry_and_definition()
    active = _run_record(definition)
    run = RunRecord.model_validate(
        {
            **active.model_dump(mode="python"),
            "status": "retry_wait",
            "current_attempt_no": None,
            "concurrency_permit_group_id": None,
            "retry_not_before_utc": "2026-07-14T12:00:01Z",
        }
    )
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    prior_failure_id = _closed_attempt_failure(artifacts, blobs, definition, attempt_no=1)
    attempt = RunAttempt.model_validate(
        {
            **_attempt().model_dump(mode="python"),
            "status": "failed",
            "ended_at": NOW,
            "failure_class": "transient_dependency",
            "retryable": True,
            "failure_artifact_id": prior_failure_id,
        }
    )
    prepared = PreparedRunFailure(
        run_id=run.run_id,
        attempt_no=attempt.attempt_no,
        run_kind=run.kind,
        artifacts=(),
        requirement_dispositions=(),
        cause_code="timed_out",
        failure_class="timeout",
        intrinsic_retry_eligible=False,
        classifier=definition.failure_classifier,
        redacted_message="overall deadline exhausted",
    )
    decision = RetryDecisionV1(
        cause_code=prepared.cause_code,
        failure_class=prepared.failure_class,
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="overall_deadline_exhausted",
        classifier=definition.failure_classifier,
        retry_policy=definition.retry_policy,
        evaluated_at_utc=NOW,
    )
    policy = select_outcome_policy(
        definition=definition,
        outcome_code=prepared.cause_code,
        prepared_outcome="failure",
        publication_scope="run",
        run_status="timed_out",
        attempt_status=None,
        failure_class=prepared.failure_class,
        retry_disposition="terminal",
    )

    published = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _Ledger(closed=((1, prior_failure_id),)),
        _Audit(),
    ).publish_run_failure(
        run=run,
        attempt=attempt,
        prepared=prepared,
        retry_decision=decision,
        policy=policy,
        attempt_failure_artifact_id=None,
        occurred_at=NOW,
        actor=WORKER,
    )

    manifest = artifacts.by_id[published.failure_artifact_id]
    payload = json.loads(blobs.read(manifest.object_ref).decode())
    assert payload["attempt_no"] == 1
    assert payload["version_projection"]["attempt_no"] == 1
    assert payload["evidence_artifact_ids"].count(prior_failure_id) == 1


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


def test_success_rejects_meta_without_payload_schema_id():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    checker = _checker_artifact(blobs).model_copy(update={"meta": {}})
    prepared = _prepared_success(artifacts=(checker,))
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=_success_policy(definition),
            occurred_at=NOW,
            actor=WORKER,
        )


def test_success_rejects_blob_schema_disagreement():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    # Blob self-declares a different schema than the prepared payload_schema_id.
    blob = json.dumps({"payload_schema_version": "checker-report@2"}).encode()
    object_ref = blobs.register(blob)
    checker = PreparedArtifact(
        kind="checker_run",
        payload_schema_id="checker-report@1",
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:input", tool_version="checker@1"),
        lineage=("artifact:input",),
        payload_hash=object_ref.sha256,
        meta={"payload_schema_id": "checker-report@1"},
        object_ref=object_ref,
        location=ObjectLocation(store_id="s3", key=object_ref.key, backend_generation="g1"),
    )
    prepared = _prepared_success(artifacts=(checker,))
    publisher = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=_success_policy(definition),
            occurred_at=NOW,
            actor=WORKER,
        )


def _execution_failure(definition, *, run_id="run:1", run_kind=None, attempt_no=1):
    return PreparedRunFailure(
        run_id=run_id,
        attempt_no=attempt_no,
        run_kind=run_kind or RunKindRef(kind="checker.run", version=1),
        artifacts=(),
        requirement_dispositions=(),
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        classifier=definition.failure_classifier,
        redacted_message="boom",
    )


def _terminal_decision(definition):
    return RetryDecisionV1(
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="not_retry_eligible",
        classifier=definition.failure_classifier,
        retry_policy=definition.retry_policy,
        evaluated_at_utc=NOW,
    )


def test_attempt_failure_rejects_fabricated_identity():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
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
    publisher = _publisher(registry, _Artifacts(), _Blobs(), _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_attempt_failure(
            run=run,
            attempt=attempt,
            prepared=_execution_failure(definition, run_id="run:OTHER"),
            retry_decision=_terminal_decision(definition),
            policy=attempt_policy,
            occurred_at=NOW,
            actor=WORKER,
        )


def test_attempt_retry_rejects_business_artifacts_that_would_be_discarded():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    blobs = _Blobs()
    prepared = PreparedRunFailure(
        run_id=run.run_id,
        attempt_no=attempt.attempt_no,
        run_kind=run.kind,
        artifacts=(_checker_artifact(blobs),),
        requirement_dispositions=(),
        cause_code="dependency_unavailable",
        failure_class="transient_dependency",
        intrinsic_retry_eligible=True,
        classifier=definition.failure_classifier,
        dependency=DependencyFailureV1(
            dependency_kind="database",
            dependency_id="db:primary",
            operation_code="read",
            classifier_code="dependency_unavailable",
        ),
        redacted_message="dependency unavailable",
    )
    decision = RetryDecisionV1(
        cause_code=prepared.cause_code,
        failure_class=prepared.failure_class,
        intrinsic_retry_eligible=True,
        decision="retry",
        reason_code="transient_eligible",
        retry_not_before_utc="2026-07-14T12:00:01Z",
        classifier=definition.failure_classifier,
        retry_policy=definition.retry_policy,
        evaluated_at_utc=NOW,
    )
    policy = select_outcome_policy(
        definition=definition,
        outcome_code=prepared.cause_code,
        prepared_outcome="failure",
        publication_scope="attempt",
        run_status="retry_wait",
        attempt_status="failed",
        failure_class=prepared.failure_class,
        retry_disposition="retry",
    )
    publisher = _publisher(registry, _Artifacts(), blobs, _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_attempt_failure(
            run=run,
            attempt=attempt,
            prepared=prepared,
            retry_decision=decision,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )


def test_failure_rejects_wrong_policy_and_retry_decision_binding():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    prepared = _execution_failure(definition)
    decision = _terminal_decision(definition)
    wrong_policy = select_outcome_policy(
        definition=definition,
        outcome_code="cancelled",
        prepared_outcome="failure",
        publication_scope="attempt",
        run_status="cancelled",
        attempt_status="cancelled",
        failure_class="cancelled",
        retry_disposition="terminal",
    )
    publisher = _publisher(registry, _Artifacts(), _Blobs(), _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_attempt_failure(
            run=run,
            attempt=attempt,
            prepared=prepared,
            retry_decision=decision,
            policy=wrong_policy,
            occurred_at=NOW,
            actor=WORKER,
        )

    forged_decision = decision.model_copy(
        update={
            "classifier": decision.classifier.model_copy(update={"classifier_digest": "f" * 64})
        }
    )
    correct_policy = select_outcome_policy(
        definition=definition,
        outcome_code=prepared.cause_code,
        prepared_outcome="failure",
        publication_scope="attempt",
        run_status="failed",
        attempt_status="failed",
        failure_class=prepared.failure_class,
        retry_disposition="terminal",
    )
    with pytest.raises(IntegrityViolation):
        publisher.publish_attempt_failure(
            run=run,
            attempt=attempt,
            prepared=prepared,
            retry_decision=forged_decision,
            policy=correct_policy,
            occurred_at=NOW,
            actor=WORKER,
        )


def test_run_failure_rejects_fabricated_attempt_no():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
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
    publisher = _publisher(registry, _Artifacts(), _Blobs(), _Findings(), _Ledger(), _Audit())
    with pytest.raises(IntegrityViolation):
        publisher.publish_run_failure(
            run=run,
            attempt=attempt,
            prepared=_execution_failure(definition, attempt_no=2),  # attempt.attempt_no is 1
            retry_decision=_terminal_decision(definition),
            policy=run_policy,
            attempt_failure_artifact_id="artifact:attempt-failure",
            occurred_at=NOW,
            actor=WORKER,
        )
