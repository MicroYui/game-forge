"""End-to-end tests for the generic terminal publication engine (M4c Task 9).

These exercise ``TerminalPublisher`` — the concrete
``RunLifecyclePublicationGateway`` — against the real registry policy objects from
``gameforge.platform.registry.defaults`` and in-memory transaction-bound ports that
mirror the production write pattern (Artifact rows / findings / links / audit).
"""

from __future__ import annotations

import json
from copy import copy
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import pytest

import gameforge.platform.publication.publisher as publisher_module
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding, FindingPayloadV1, FindingRevisionV1
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
    AuditCorrelation,
    ObjectLocation,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.platform.publication import TerminalPublisher
from gameforge.platform.terminal_staging import (
    PreverifiedAbsentArtifactBinding,
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
    build_builtin_registry,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.runs.admission import RunAdmissionEngine
from gameforge.platform.runs.lifecycle import (
    AttemptFailurePublication,
    RunFailurePublication,
    TerminalAuthorityDrift,
    select_outcome_policy,
)
from gameforge.platform.run_handlers.validation_common import content_addressed_artifact_id
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

    def put_staged(self, artifact, receipt, retained_binding=None):
        del retained_binding
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


class _BatchArtifacts(_Artifacts):
    def __init__(self) -> None:
        super().__init__()
        self.batch_events: list[str] = []

    @staticmethod
    def preflight_binding(artifact):
        return PreverifiedAbsentArtifactBinding(object_ref=artifact.object_ref)

    def preflight_staged_many(self, writes):
        self.batch_events.append("preflight")
        return tuple(writes)

    def put_preflighted_many(self, writes):
        self.batch_events.append("write")
        return tuple(self.put(artifact) for artifact, _receipt, _binding in writes)

    def put_staged(self, artifact, receipt, retained_binding=None):  # pragma: no cover
        del artifact, receipt, retained_binding
        raise AssertionError("planned production commit used scalar Artifact publication")


class _MismatchingBatchArtifacts(_BatchArtifacts):
    def put_preflighted_many(self, writes):
        self.batch_events.append("write")
        return tuple(
            artifact.model_copy(update={"meta": {"tampered": True}})
            for artifact, _receipt, _binding in writes
        )


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


class _PersistentLedger(_Ledger):
    def terminal_authority_digest(self, run_id):
        assert run_id == "run:1"
        return "d" * 64

    def terminal_attempt_authority_digest(self, run_id):
        assert run_id == "run:1"
        return "d" * 64


class _ScopedAuthorityLedger(_Ledger):
    def __init__(self) -> None:
        super().__init__()
        self.full_calls = 0
        self.attempt_calls = 0
        self.fresh_attempt_calls = 0

    def terminal_authority_digest(self, run_id):
        assert run_id == "run:1"
        self.full_calls += 1
        return "f" * 64

    def terminal_attempt_authority_digest(self, run_id):
        assert run_id == "run:1"
        self.attempt_calls += 1
        return "a" * 64

    def fresh_terminal_attempt_authority_digest(self, run_id):
        assert run_id == "run:1"
        self.fresh_attempt_calls += 1
        return "a" * 64


class _Audit:
    def __init__(self, *, fail_preflight: bool = False) -> None:
        self.records: list[dict] = []
        self.batch_events: list[str] = []
        self.preflighted: list[tuple[object, ...]] = []
        self._fail_preflight = fail_preflight

    def record(self, **kwargs) -> None:
        self.records.append(kwargs)

    def preflight_records(self, records):
        self.batch_events.append("preflight")
        if self._fail_preflight:
            raise IntegrityViolation("audit authority unavailable")
        retained = tuple(records)
        self.preflighted.append(retained)
        return retained

    def apply_preflighted_records(self, prepared) -> None:
        self.batch_events.append("write")
        self.records.extend(
            {
                "action": record.action,
                "run": record.run,
                "artifact_id": record.artifact_id,
                "actor": record.actor,
                "occurred_at": record.occurred_at,
            }
            for record in prepared
        )


class _ScalarOnlyAudit:
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


def _checker_embedded_finding() -> Finding:
    prepared = _checker_finding()
    return Finding(
        id=prepared.finding_id,
        **prepared.payload.model_dump(mode="python", exclude={"payload_schema_version"}),
    )


def _checker_artifact(
    blobs: _Blobs,
    *,
    findings: tuple[Finding, ...] = (),
) -> PreparedArtifact:
    blob = canonical_json(
        {
            "payload_schema_version": "checker-report@1",
            "checker_profile": {"profile_id": "checker", "version": 1},
            "constraint_snapshot_binding_status": "not_applicable",
            "snapshot_id": "snapshot:input",
            "checker_ids": ["graph"],
            "defect_classes": ["dangling_ref"],
            "constraint_application": [],
            "findings": [finding.model_dump(mode="json") for finding in findings],
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
            producer_id="graph",
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


def _exact_profile_binding(registry, catalog, *, field_path, profile, profile_kind):
    return registry.resolve_execution_profile(
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        field_path=field_path,
        profile=profile,
        expected_profile_kind=profile_kind,
    )


def _bind_context_to_exact_catalog(context, catalog, bindings):
    envelope = context.payload.model_copy(
        update={
            "execution_profile_catalog_version": catalog.catalog_version,
            "execution_profile_catalog_digest": catalog.catalog_digest,
            "resolved_profiles": tuple(bindings),
        }
    )
    return replace(
        context,
        payload=envelope,
        run=context.run.model_copy(
            update={"payload": envelope, "payload_hash": canonical_payload_hash(envelope)}
        ),
    )


def _publish_validation_handler_outcome(
    monkeypatch,
    *,
    registry,
    catalog,
    context,
    outcome,
    store,
    input_artifacts,
):
    """Drive a real handler result through Terminal planning, reseal and commit."""

    from gameforge.platform.publication import publisher as publisher_module

    definition = registry.get_run_kind(context.run.kind)
    assert definition is not None
    snapshots = RunAdmissionEngine._resolve_policy_snapshots(  # noqa: SLF001
        object(),
        params=context.payload.params,
        definition=definition,
        resolved_profiles=context.payload.resolved_profiles,
        catalog=catalog,
    )
    envelope = context.payload.model_copy(update={"resolved_policy_snapshots": snapshots})
    retry = registry.get_retry_policy(definition.retry_policy)
    assert retry is not None
    run = context.run.model_copy(
        update={
            "payload": envelope,
            "payload_hash": canonical_payload_hash(envelope),
            "run_kind_definition_digest": run_kind_definition_digest(definition),
            "outcome_policy_set_digest": outcome_policy_set_digest(
                context.run.kind, definition.outcome_policies
            ),
            "failure_classifier": definition.failure_classifier,
            "retry_policy": definition.retry_policy,
            "max_attempts": retry.max_attempts,
        }
    )
    artifacts, blobs, findings, ledger, audit = (
        _Artifacts(),
        _Blobs(),
        _Findings(),
        _Ledger(),
        _Audit(),
    )
    assert {item.artifact_id for item in input_artifacts} == set(run.payload.input_artifact_ids)
    for artifact in input_artifacts:
        artifacts.add(artifact)
        blobs._by_key[artifact.object_ref.key] = store.read_bytes(artifact.artifact_id)
    for artifact in outcome.artifacts:
        blobs._by_key[artifact.object_ref.key] = store.read_prepared(artifact.object_ref)
        blobs._locations[artifact.object_ref.key] = artifact.location

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

    # These tests target the real Terminal Artifact/Finding path. Workflow CAS is
    # independently exercised with real repositories in
    # test_validation_completion_effect.py.
    class _SkippedPreparedWorkflow:
        @staticmethod
        def canonical_projection():
            return {"prepared_schema_version": "test-skipped-workflow@1"}

    monkeypatch.setattr(
        publisher_module,
        "prepare_workflow_effect",
        lambda *_args, **_kw: _SkippedPreparedWorkflow(),
    )
    monkeypatch.setattr(
        publisher_module,
        "commit_prepared_workflow_effect",
        lambda *_args, **_kw: None,
    )
    published = _publisher(registry, artifacts, blobs, findings, ledger, audit).publish_run_result(
        run=run,
        attempt=context.attempt,
        prepared=outcome,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    return published, findings, ledger, artifacts, blobs


def _reseal_prepared_payload(store, artifact, payload):
    blob = canonical_json(payload).encode()
    object_ref, location = store.put_prepared(blob)
    return artifact.model_copy(
        update={
            "object_ref": object_ref,
            "location": location,
            "payload_hash": object_ref.sha256,
        }
    )


def _replace_validation_companion_payload(
    *,
    store,
    outcome,
    requirement_id: str,
    replacement_payload: dict[str, object],
    replacement_requirement_kind: str | None = None,
):
    artifacts = list(outcome.artifacts)
    companion_index = next(
        index
        for index, artifact in enumerate(artifacts)
        if artifact.meta.get("requirement_id") == requirement_id
    )
    companion = artifacts[companion_index]
    old_companion_id = content_addressed_artifact_id(companion)
    replacement = _reseal_prepared_payload(store, companion, replacement_payload)
    new_companion_id = content_addressed_artifact_id(replacement)
    artifacts[companion_index] = replacement

    primary = artifacts[outcome.primary_index]
    primary_payload = json.loads(store.read_prepared(primary.object_ref))
    primary_payload["supporting_artifact_ids"] = sorted(
        new_companion_id if artifact_id == old_companion_id else artifact_id
        for artifact_id in primary_payload["supporting_artifact_ids"]
    )
    for requirement in primary_payload["requirements"]:
        if requirement.get("evidence_artifact_id") == old_companion_id:
            requirement["evidence_artifact_id"] = new_companion_id
            if replacement_requirement_kind is not None:
                requirement["kind"] = replacement_requirement_kind
    artifacts[outcome.primary_index] = _reseal_prepared_payload(
        store,
        primary,
        primary_payload,
    )
    return outcome.model_copy(update={"artifacts": tuple(artifacts)})


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


def test_publication_collector_resolves_blob_slots_without_rescanning_materials() -> None:
    """Artifact attachment is O(1) after the blob slot has been allocated."""

    from gameforge.platform.publication import publisher as publisher_module

    class _NoIterationList(list):
        def __iter__(self):
            raise AssertionError("add_artifact must not rescan every prior blob material")

    collector = publisher_module._PublicationCollector(  # noqa: SLF001
        publication_kind="run_result",
        run_id="run:collector",
        attempt_no=1,
        occurred_at=NOW,
    )
    object_ref = collector.add_blob(b"collector payload")
    collector.materials = _NoIterationList(collector.materials)
    artifact = build_artifact_v2(
        kind="checker_run",
        version_tuple=VersionTuple(tool_version="checker@1"),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"payload_schema_id": "checker-report@1"},
        created_at=NOW,
    )

    assert collector.add_artifact(artifact) == artifact


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
    checker = _checker_artifact(blobs, findings=(_checker_embedded_finding(),))
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


@pytest.mark.parametrize(
    ("embedded_findings", "prepared_findings"),
    (
        ((), (_checker_finding(),)),
        ((_checker_embedded_finding(),), ()),
    ),
)
def test_checker_report_and_run_finding_links_must_close_exactly(
    embedded_findings: tuple[Finding, ...],
    prepared_findings: tuple[PreparedFindingV1, ...],
) -> None:
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    prepared = _prepared_success(
        artifacts=(_checker_artifact(blobs, findings=embedded_findings),),
        findings=prepared_findings,
    )
    publisher = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _Ledger(),
        _Audit(),
    )

    with pytest.raises(IntegrityViolation, match="embedded Findings differ"):
        publisher.publish_run_result(
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=_success_policy(definition),
            occurred_at=NOW,
            actor=WORKER,
        )


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
                "assert": "true",
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
            "checker_profile": {"profile_id": "checker", "version": 1},
            "constraint_snapshot_binding_status": "bound",
            "constraint_snapshot_artifact_id": constraint_artifact.artifact_id,
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
    resealed_finding = _checker_finding().model_copy(
        update={
            "finding_id": "finding:resealed",
            "payload": _checker_finding().payload.model_copy(
                update={
                    "producer_id": "checker:graph",
                    "message": "publisher reseal plumbing",
                }
            ),
        }
    )
    prepared = _prepared_success(
        artifacts=(_checker_artifact(blobs),),
        findings=(resealed_finding,),
    )
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
                "producer_id": "checker:graph",
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
        "commit_prepared_workflow_effect",
        lambda _prepared, context: effect_payloads.append(context.published_primary_payload),
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


def test_planned_failure_aggregate_preflights_audits_once_in_publication_order():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _BatchArtifacts(), _Blobs()
    _input_snapshot(artifacts)
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
    audit = _Audit()
    harness = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _PersistentLedger(),
        audit,
    )
    publisher = harness._publisher  # noqa: SLF001 - explicit aggregate boundary
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
    staged = tuple(harness._stage(draft) for draft in drafts)  # noqa: SLF001
    sealed = tuple(
        draft.seal_for_commit(staged_publication)
        for draft, staged_publication in zip(drafts, staged, strict=True)
    )

    publisher.commit_planned_active_failure_aggregate(
        sealed,
        staged,
        run=run,
        attempt=attempt,
        prepared=prepared,
        retry_decision=decision,
        attempt_policy=attempt_policy,
        run_policy=run_policy,
        occurred_at=NOW,
        actor=WORKER,
    )

    assert audit.batch_events == ["preflight", "write"]
    assert len(audit.preflighted) == 1
    assert tuple(record.action for record in audit.preflighted[0]) == (
        "run.attempt_failure",
        "run.failure",
        "run.attempt_closed",
        "run.terminal",
    )


def test_retry_uses_attempt_digest_while_final_aggregate_shares_one_full_fresh_digest():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    retry_prepared = PreparedRunFailure(
        run_id=run.run_id,
        attempt_no=attempt.attempt_no,
        run_kind=run.kind,
        artifacts=(),
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
    retry_decision = RetryDecisionV1(
        cause_code=retry_prepared.cause_code,
        failure_class=retry_prepared.failure_class,
        intrinsic_retry_eligible=True,
        decision="retry",
        reason_code="transient_eligible",
        retry_not_before_utc="2026-07-14T12:00:01Z",
        classifier=definition.failure_classifier,
        retry_policy=definition.retry_policy,
        evaluated_at_utc=NOW,
    )
    retry_policy = select_outcome_policy(
        definition=definition,
        outcome_code=retry_prepared.cause_code,
        prepared_outcome="failure",
        publication_scope="attempt",
        run_status="retry_wait",
        attempt_status="failed",
        failure_class=retry_prepared.failure_class,
        retry_disposition="retry",
    )
    retry_artifacts, retry_blobs = _BatchArtifacts(), _Blobs()
    _input_snapshot(retry_artifacts)
    retry_ledger = _ScopedAuthorityLedger()
    retry_harness = _publisher(
        registry,
        retry_artifacts,
        retry_blobs,
        _Findings(),
        retry_ledger,
        _Audit(),
    )
    retry_publisher = retry_harness._publisher  # noqa: SLF001
    retry_drafts = retry_publisher.plan_active_failure_aggregate(
        run=run,
        attempt=attempt,
        prepared=retry_prepared,
        retry_decision=retry_decision,
        attempt_policy=retry_policy,
        run_policy=None,
        occurred_at=NOW,
        actor=WORKER,
    )
    assert (retry_ledger.attempt_calls, retry_ledger.fresh_attempt_calls) == (1, 0)
    assert retry_ledger.full_calls == 0
    retry_staged = tuple(retry_harness._stage(draft) for draft in retry_drafts)  # noqa: SLF001
    retry_sealed = tuple(
        draft.seal_for_commit(staged)
        for draft, staged in zip(retry_drafts, retry_staged, strict=True)
    )
    retry_publisher.commit_planned_active_failure_aggregate(
        retry_sealed,
        retry_staged,
        run=run,
        attempt=attempt,
        prepared=retry_prepared,
        retry_decision=retry_decision,
        attempt_policy=retry_policy,
        run_policy=None,
        occurred_at=NOW,
        actor=WORKER,
    )
    assert (retry_ledger.attempt_calls, retry_ledger.fresh_attempt_calls) == (1, 1)
    assert retry_ledger.full_calls == 0

    final_prepared = _execution_failure(definition)
    final_decision = _terminal_decision(definition)
    final_attempt_policy = select_outcome_policy(
        definition=definition,
        outcome_code=final_prepared.cause_code,
        prepared_outcome="failure",
        publication_scope="attempt",
        run_status="failed",
        attempt_status="failed",
        failure_class=final_prepared.failure_class,
        retry_disposition="terminal",
    )
    final_run_policy = select_outcome_policy(
        definition=definition,
        outcome_code=final_prepared.cause_code,
        prepared_outcome="failure",
        publication_scope="run",
        run_status="failed",
        attempt_status="failed",
        failure_class=final_prepared.failure_class,
        retry_disposition="terminal",
    )
    final_artifacts, final_blobs = _BatchArtifacts(), _Blobs()
    _input_snapshot(final_artifacts)
    final_ledger = _ScopedAuthorityLedger()
    final_harness = _publisher(
        registry,
        final_artifacts,
        final_blobs,
        _Findings(),
        final_ledger,
        _Audit(),
    )
    final_publisher = final_harness._publisher  # noqa: SLF001
    final_drafts = final_publisher.plan_active_failure_aggregate(
        run=run,
        attempt=attempt,
        prepared=final_prepared,
        retry_decision=final_decision,
        attempt_policy=final_attempt_policy,
        run_policy=final_run_policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    assert final_ledger.attempt_calls == final_ledger.fresh_attempt_calls == 0
    assert final_ledger.full_calls == 2
    final_staged = tuple(final_harness._stage(draft) for draft in final_drafts)  # noqa: SLF001
    final_sealed = tuple(
        draft.seal_for_commit(staged)
        for draft, staged in zip(final_drafts, final_staged, strict=True)
    )
    final_publisher.commit_planned_active_failure_aggregate(
        final_sealed,
        final_staged,
        run=run,
        attempt=attempt,
        prepared=final_prepared,
        retry_decision=final_decision,
        attempt_policy=final_attempt_policy,
        run_policy=final_run_policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    assert final_ledger.attempt_calls == final_ledger.fresh_attempt_calls == 0
    assert final_ledger.full_calls == 3


@pytest.mark.parametrize(
    ("publication_kinds", "expected_actions"),
    [
        (("run_result",), ("run.terminal",)),
        (("attempt_failure",), ("run.attempt_closed",)),
        (
            ("attempt_failure", "run_failure"),
            ("run.attempt_closed", "run.terminal"),
        ),
        (("run_failure",), ("run.terminal",)),
    ],
)
def test_terminal_lifecycle_audit_projection_covers_every_closure_branch(
    publication_kinds: tuple[str, ...],
    expected_actions: tuple[str, ...],
) -> None:
    assert (
        publisher_module._terminal_lifecycle_audit_actions(publication_kinds)  # noqa: SLF001
        == expected_actions
    )


def test_terminal_lifecycle_audit_projection_rejects_unknown_aggregate() -> None:
    with pytest.raises(IntegrityViolation, match="no lifecycle Audit projection"):
        publisher_module._terminal_lifecycle_audit_actions(("unknown",))  # noqa: SLF001


def test_inactive_command_audits_join_publication_and_lifecycle_in_one_batch() -> None:
    _registry, definition = _registry_and_definition()
    run = _run_record(definition)
    publication = publisher_module._AuditWrite(  # noqa: SLF001
        action="run.failure",
        run=run,
        artifact_id="artifact:failure",
        actor=HUMAN,
        occurred_at=NOW,
    )
    correlation = AuditCorrelation(
        request_id="request:cancel",
        run_id=run.run_id,
        trace_id="trace:cancel",
    )

    intents = publisher_module._terminal_audit_intents(  # noqa: SLF001
        publication_kinds=("run_failure",),
        audit_operations_by_publication=((publication,),),
        command_audit_correlation=correlation,
    )

    assert tuple(intent.action for intent in intents) == (
        "run.failure",
        "run.command_submitted",
        "run.terminal",
    )
    assert tuple(intent.deferred for intent in intents) == (False, True, True)
    assert tuple((intent.request_id, intent.trace_id) for intent in intents) == (
        ("request:cancel", "trace:cancel"),
        ("request:cancel", "trace:cancel"),
        ("request:cancel", "trace:cancel"),
    )


def test_command_audit_correlation_is_rejected_outside_single_run_failure() -> None:
    _registry, definition = _registry_and_definition()
    run = _run_record(definition)
    publication = publisher_module._AuditWrite(  # noqa: SLF001
        action="publish-checker@1",
        run=run,
        artifact_id="artifact:result",
        actor=HUMAN,
        occurred_at=NOW,
    )
    correlation = AuditCorrelation(request_id=None, run_id=run.run_id, trace_id=None)

    with pytest.raises(IntegrityViolation, match="single inactive Run failure"):
        publisher_module._terminal_audit_intents(  # noqa: SLF001
            publication_kinds=("run_result",),
            audit_operations_by_publication=((publication,),),
            command_audit_correlation=correlation,
        )


def test_terminal_audit_preflight_and_consume_preserve_non_null_trace() -> None:
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt().model_copy(update={"trace_id": "trace:attempt"})
    publication = publisher_module._AuditWrite(  # noqa: SLF001
        action="publish-checker@1",
        run=run,
        artifact_id="artifact:result",
        actor=WORKER,
        occurred_at=NOW,
        trace_id=attempt.trace_id,
    )
    intents = publisher_module._terminal_audit_intents(  # noqa: SLF001
        publication_kinds=("run_result",),
        audit_operations_by_publication=((publication,),),
    )
    assert tuple(intent.trace_id for intent in intents) == (
        "trace:attempt",
        "trace:attempt",
    )

    audit = _Audit()
    publisher = _publisher(
        registry,
        _Artifacts(),
        _Blobs(),
        _Findings(),
        _Ledger(),
        audit,
    )._publisher  # noqa: SLF001 - exact terminal Audit adapter boundary
    publisher.record_run_terminal(
        run=run,
        attempt=attempt,
        event=SimpleNamespace(occurred_at=NOW, trace_id=None),
        actor=WORKER,
    )
    assert audit.records[-1]["trace_id"] == "trace:attempt"
    publisher.record_run_terminal(
        run=run,
        attempt=attempt,
        event=SimpleNamespace(occurred_at=NOW, trace_id="trace:event"),
        actor=WORKER,
    )
    assert audit.records[-1]["trace_id"] == "trace:event"


def test_draft_getter_does_not_expose_nested_operation_state():
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
    with pytest.raises(TypeError, match="immutable"):
        workflow.context.published_primary_payload["snapshot_id"] = "snapshot:mutated"

    committed = publisher.commit(draft, staged)

    assert committed.result_artifact_id in artifacts.by_id


def test_planned_commit_seal_uses_alias_free_nested_state():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    harness = _publisher(registry, artifacts, blobs, _Findings(), _Ledger(), _Audit())
    draft = harness.plan_run_result(
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
    with pytest.raises(TypeError, match="immutable"):
        workflow.context.published_primary_payload["snapshot_id"] = "snapshot:mutated"

    sealed = draft.seal_for_commit(staged)

    assert sealed.is_commit_sealed()
    with pytest.raises(IntegrityViolation, match="already consumed"):
        draft.seal_for_commit(staged)
    assert set(artifacts.by_id) == {"artifact:input"}


def test_planned_commit_requires_artifact_batch_preflight_and_apply() -> None:
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    harness = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _PersistentLedger(),
        _Audit(),
    )
    publisher = harness._publisher  # noqa: SLF001 - explicit production-capability test
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    policy = _success_policy(definition)
    draft = publisher.plan_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    staged = harness._stage(draft)  # noqa: SLF001

    with pytest.raises(IntegrityViolation, match="Artifact batch preflight/apply"):
        publisher.commit_planned_run_result(
            draft.seal_for_commit(staged),
            staged,
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )


def test_planned_commit_does_not_reproject_large_operations_under_write_lock(
    monkeypatch,
):
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _BatchArtifacts(), _Blobs()
    _input_snapshot(artifacts)
    harness = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _PersistentLedger(),
        _Audit(),
    )
    publisher = harness._publisher  # noqa: SLF001 - explicit three-phase test
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    policy = _success_policy(definition)
    draft = publisher.plan_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    staged = harness._stage(draft)  # noqa: SLF001
    sealed = draft.seal_for_commit(staged)
    assert type(sealed).__slots__ == ("__weakref__",)
    with pytest.raises(AttributeError):
        object.__setattr__(sealed, "operations", ())
    exposed_material = sealed.materials[0]
    object.__setattr__(exposed_material, "slot", "blob:tampered")
    assert all(material.slot != "blob:tampered" for material in sealed.materials)
    exposed_operation = next(
        operation for operation in sealed.operations if hasattr(operation, "slot")
    )
    object.__setattr__(exposed_operation, "slot", "blob:tampered")
    assert all(
        getattr(operation, "slot", None) != "blob:tampered" for operation in sealed.operations
    )
    exposed_result = sealed.result
    object.__setattr__(exposed_result, "result_artifact_id", "artifact:tampered")
    assert sealed.result.result_artifact_id != "artifact:tampered"
    exposed_projection = sealed.canonical_projection()
    dict.__setitem__(exposed_projection["operations"][0], "operation", "tampered")
    dict.__setitem__(exposed_projection["result"], "result_artifact_id", "artifact:tampered")
    fresh_projection = sealed.canonical_projection()
    assert fresh_projection["operations"][0]["operation"] != "tampered"
    assert fresh_projection["result"]["result_artifact_id"] != "artifact:tampered"
    for unregistered in (replace(sealed), copy(sealed)):
        with pytest.raises(TerminalAuthorityDrift, match="immutable plan"):
            publisher.commit_planned_run_result(
                unregistered,
                staged,
                run=run,
                attempt=attempt,
                prepared=prepared,
                policy=policy,
                occurred_at=NOW,
                actor=WORKER,
            )
    assert set(artifacts.by_id) == {"artifact:input"}
    sealed_workflow = next(
        operation
        for operation in sealed.operations
        if getattr(operation, "context", None) is not None
    )
    with pytest.raises(TypeError, match="immutable"):
        sealed_workflow.context.published_primary_payload["snapshot_id"] = "snapshot:mutated"
    with pytest.raises(TypeError, match="immutable"):
        sealed_workflow.context.published_primary_payload |= {
            "snapshot_id": "snapshot:mutated-by-ior"
        }

    def fail_if_reprojected(_operation):
        raise AssertionError("planned write-lock commit reprojected a sealed operation")

    monkeypatch.setattr(
        "gameforge.platform.publication.publisher._operation_projection",
        fail_if_reprojected,
    )
    committed = publisher.commit_planned_run_result(
        sealed,
        staged,
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )

    assert committed == sealed.result
    assert committed.result_artifact_id in artifacts.by_id
    published_ids = set(artifacts.by_id)
    with pytest.raises(TerminalAuthorityDrift, match="immutable plan"):
        publisher.commit_planned_run_result(
            sealed,
            staged,
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )
    assert set(artifacts.by_id) == published_ids


def test_planning_subject_digest_never_serializes_prepared_collection_payloads(
    monkeypatch,
):
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _Artifacts(), _Blobs()
    _input_snapshot(artifacts)
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    policy = _success_policy(definition)

    def fail_full_prepared_projection(*_args, **_kwargs):
        raise AssertionError("terminal selector serialized the full prepared outcome")

    monkeypatch.setattr(type(prepared), "model_dump", fail_full_prepared_projection)
    digest = publisher_module._planning_subject_digest(  # noqa: SLF001
        "run_result",
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        retry_decision=None,
        attempt_failure_artifact_id=None,
        occurred_at=NOW,
        actor=WORKER,
    )

    assert len(digest) == 64


def test_planned_commit_preflights_workflow_before_first_artifact_write(monkeypatch):
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _BatchArtifacts(), _Blobs()
    _input_snapshot(artifacts)
    harness = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _PersistentLedger(),
        _Audit(),
    )
    publisher = harness._publisher  # noqa: SLF001 - explicit three-phase test
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    policy = _success_policy(definition)
    draft = publisher.plan_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    staged = harness._stage(draft)  # noqa: SLF001
    sealed = draft.seal_for_commit(staged)

    def drift_before_first_write(*_args, **_kwargs):
        raise TerminalAuthorityDrift("workflow changed after staging")

    monkeypatch.setattr(
        publisher_module,
        "preflight_prepared_workflow_effect",
        drift_before_first_write,
    )
    with pytest.raises(TerminalAuthorityDrift, match="workflow changed"):
        publisher.commit_planned_run_result(
            sealed,
            staged,
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )

    assert artifacts.batch_events == ["preflight"]
    assert set(artifacts.by_id) == {"artifact:input"}


def test_planned_commit_uses_batch_artifact_publication():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _BatchArtifacts(), _Blobs()
    _input_snapshot(artifacts)
    audit = _Audit()
    harness = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _PersistentLedger(),
        audit,
    )
    publisher = harness._publisher  # noqa: SLF001 - explicit three-phase test
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    policy = _success_policy(definition)
    draft = publisher.plan_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    staged = harness._stage(draft)  # noqa: SLF001
    sealed = draft.seal_for_commit(staged)

    result = publisher.commit_planned_run_result(
        sealed,
        staged,
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )

    assert result == sealed.result
    assert artifacts.batch_events == ["preflight", "write"]
    assert audit.batch_events == ["preflight", "write"]
    assert len(audit.preflighted) == 1
    assert tuple(record.action for record in audit.preflighted[0]) == (
        "publish-checker@1",
        "run.terminal",
    )


def test_planned_commit_preflights_audit_authority_before_first_artifact_write():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _BatchArtifacts(), _Blobs()
    _input_snapshot(artifacts)
    audit = _Audit(fail_preflight=True)
    harness = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _PersistentLedger(),
        audit,
    )
    publisher = harness._publisher  # noqa: SLF001 - explicit three-phase test
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    policy = _success_policy(definition)
    draft = publisher.plan_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    staged = harness._stage(draft)  # noqa: SLF001
    sealed = draft.seal_for_commit(staged)

    with pytest.raises(IntegrityViolation, match="audit authority unavailable"):
        publisher.commit_planned_run_result(
            sealed,
            staged,
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )

    assert artifacts.batch_events == ["preflight"]
    assert audit.batch_events == ["preflight"]
    assert set(artifacts.by_id) == {"artifact:input"}


def test_planned_commit_rejects_scalar_only_audit_before_first_artifact_write():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _BatchArtifacts(), _Blobs()
    _input_snapshot(artifacts)
    audit = _ScalarOnlyAudit()
    harness = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _PersistentLedger(),
        audit,
    )
    publisher = harness._publisher  # noqa: SLF001 - explicit production boundary
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    policy = _success_policy(definition)
    draft = publisher.plan_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    staged = harness._stage(draft)  # noqa: SLF001
    sealed = draft.seal_for_commit(staged)

    with pytest.raises(IntegrityViolation, match="Audit batch preflight capability"):
        publisher.commit_planned_run_result(
            sealed,
            staged,
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )

    assert artifacts.batch_events == ["preflight"]
    assert audit.records == []
    assert set(artifacts.by_id) == {"artifact:input"}


def test_planned_commit_rejects_batch_artifact_content_mismatch():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    attempt = _attempt()
    artifacts, blobs = _MismatchingBatchArtifacts(), _Blobs()
    _input_snapshot(artifacts)
    harness = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _PersistentLedger(),
        _Audit(),
    )
    publisher = harness._publisher  # noqa: SLF001 - explicit three-phase test
    prepared = _prepared_success(artifacts=(_checker_artifact(blobs),))
    policy = _success_policy(definition)
    draft = publisher.plan_run_result(
        run=run,
        attempt=attempt,
        prepared=prepared,
        policy=policy,
        occurred_at=NOW,
        actor=WORKER,
    )
    staged = harness._stage(draft)  # noqa: SLF001
    sealed = draft.seal_for_commit(staged)

    with pytest.raises(IntegrityViolation, match="another immutable Artifact"):
        publisher.commit_planned_run_result(
            sealed,
            staged,
            run=run,
            attempt=attempt,
            prepared=prepared,
            policy=policy,
            occurred_at=NOW,
            actor=WORKER,
        )

    assert artifacts.batch_events == ["preflight", "write"]


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


def test_patch_llm_constraint_unproven_reaches_real_terminal(monkeypatch):
    from gameforge.apps.worker import components as worker_components
    from gameforge.contracts.dsl import Constraint, Predicate
    from tests.platform.m4c import test_patch_validation_handler as patch_mod

    registry = build_builtin_registry()
    catalog = max(registry.list_execution_profile_catalogs(), key=lambda item: item.catalog_version)
    validation_profile = ProfileRefV1(profile_id="builtin.validation", version=1)
    checker_profile = ProfileRefV1(profile_id="builtin.checker", version=1)
    bindings = (
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/validation_policy",
            profile=validation_profile,
            profile_kind="validation",
        ),
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/checker_profiles/0",
            profile=checker_profile,
            profile_kind="checker",
        ),
    )
    store = patch_mod._store(snapshot=patch_mod.snapshot_bytes([], []))
    base_blob = store.read_bytes(patch_mod.BASE_ID)
    preview_blob = store.read_bytes(patch_mod.PREVIEW_ID)
    base_snapshot = patch_mod.load_snapshot(store, patch_mod.BASE_ID)
    preview_snapshot = patch_mod.load_snapshot(store, patch_mod.PREVIEW_ID)
    constraint_snapshot_id = "constraint:llm-terminal:1"
    llm_constraint = Constraint(
        id="C_llm",
        kind="narrative",
        oracle="mixed",
        predicates=(Predicate(expr="semantic_consistency(story)", oracle="llm-assisted"),),
        **{"assert": "continuity_consistent"},
        severity="major",
    )
    base = store.register_exact_artifact(
        kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload=base_blob,
        version_tuple=VersionTuple(doc_version="doc@1", ir_snapshot_id=base_snapshot.snapshot_id),
    )
    preview = store.register_exact_artifact(
        kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload=preview_blob,
        version_tuple=VersionTuple(
            doc_version="doc@1", ir_snapshot_id=preview_snapshot.snapshot_id
        ),
    )
    constraints = store.register_exact_artifact(
        kind="constraint_snapshot",
        payload_schema_id="constraint-snapshot@1",
        payload={
            "dsl_grammar_version": "dsl@1",
            "constraints": [llm_constraint.model_dump(mode="json", by_alias=True)],
        },
        version_tuple=VersionTuple(constraint_snapshot_id=constraint_snapshot_id),
    )
    subject = store.register_exact_artifact(
        kind="patch",
        payload_schema_id="patch@2",
        payload={"patch_schema_version": "patch@2", "ops": []},
        version_tuple=VersionTuple(
            constraint_snapshot_id=constraint_snapshot_id,
            tool_version="patch@2",
        ),
    )
    payload = patch_mod._payload(
        constraint_snapshot_artifact_id=constraints.artifact_id,
        checker_profiles=(checker_profile,),
    ).model_copy(
        update={
            "subject": patch_mod._subject().model_copy(
                update={
                    "subject_artifact_id": subject.artifact_id,
                    "subject_digest": subject.payload_hash,
                }
            ),
            "base_snapshot_artifact_id": base.artifact_id,
            "preview_snapshot_artifact_id": preview.artifact_id,
            "constraint_snapshot_artifact_id": constraints.artifact_id,
            "target": patch_mod._payload().target.model_copy(
                update={
                    "expected_ref": patch_mod.RefValue(artifact_id=base.artifact_id, revision=1)
                }
            ),
            "validation_policy": validation_profile,
        }
    )
    context = _bind_context_to_exact_catalog(
        patch_mod._context(
            store,
            payload,
            constraint_snapshot_id=constraint_snapshot_id,
            resolved_profiles_override=bindings,
        ),
        catalog,
        bindings,
    )
    outcome = patch_mod._handler(
        store,
        checker_resolver=worker_components._build_patch_checker_resolver(registry),
    )(context)

    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert outcome.findings == ()
    published, findings, _ledger, artifacts, blobs = _publish_validation_handler_outcome(
        monkeypatch,
        registry=registry,
        catalog=catalog,
        context=context,
        outcome=outcome,
        store=store,
        input_artifacts=(base, constraints, preview, subject),
    )

    assert findings.revisions == []
    manifest = artifacts.by_id[published.result_artifact_id]
    result = json.loads(blobs.read(manifest.object_ref))
    companion = next(
        artifacts.by_id[artifact_id]
        for artifact_id in result["produced_artifact_ids"]
        if artifacts.by_id[artifact_id].meta.get("requirement_id") == "checker:builtin.checker@1"
    )
    companion_payload = json.loads(blobs.read(companion.object_ref))
    assert companion_payload["status"] == "unproven"
    assert companion_payload["detail"]["findings"][0]["producer_id"] == "llm-routed"


def test_patch_review_finding_overlap_reaches_terminal_and_rejects_label_swap(
    monkeypatch,
):
    from tests.platform.m4c import test_patch_validation_handler as patch_mod

    registry = build_builtin_registry()
    catalog = max(registry.list_execution_profile_catalogs(), key=lambda item: item.catalog_version)
    validation_profile = ProfileRefV1(profile_id="builtin.validation", version=1)
    bindings = (
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/validation_policy",
            profile=validation_profile,
            profile_kind="validation",
        ),
    )
    store = patch_mod._store()
    base_blob = store.read_bytes(patch_mod.BASE_ID)
    preview_blob = store.read_bytes(patch_mod.PREVIEW_ID)
    base_snapshot = patch_mod.load_snapshot(store, patch_mod.BASE_ID)
    preview_snapshot = patch_mod.load_snapshot(store, patch_mod.PREVIEW_ID)
    base = store.register_exact_artifact(
        kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload=base_blob,
        version_tuple=VersionTuple(
            doc_version="doc@1",
            ir_snapshot_id=base_snapshot.snapshot_id,
        ),
    )
    preview = store.register_exact_artifact(
        kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload=preview_blob,
        version_tuple=VersionTuple(
            doc_version="doc@1",
            ir_snapshot_id=preview_snapshot.snapshot_id,
        ),
    )
    subject = store.register_exact_artifact(
        kind="patch",
        payload_schema_id="patch@2",
        payload={"patch_schema_version": "patch@2", "ops": []},
        version_tuple=VersionTuple(tool_version="patch@2"),
    )
    review = store.register_exact_artifact(
        kind="review_report",
        payload_schema_id="review@1",
        payload=patch_mod.ReviewReport(snapshot_id=preview_snapshot.snapshot_id).model_dump(
            mode="json"
        ),
        version_tuple=VersionTuple(
            ir_snapshot_id=preview_snapshot.snapshot_id,
            tool_version="review@1",
        ),
    )
    revision = patch_mod._finding_revision(
        patch_mod.Finding(
            id="review-suggestion",
            source="llm",
            producer_id="review.triage",
            producer_run_id="run:review-producer",
            oracle_type="llm-assisted",
            defect_class="narrative_consistency",
            severity="major",
            snapshot_id=preview_snapshot.snapshot_id,
            status="unproven",
            message="human review is required",
        ),
        finding_id="finding-series:review:1",
    )
    finding_binding = patch_mod._finding_binding(
        revision,
        evidence_artifact_id=review.artifact_id,
    )
    payload = patch_mod._payload(
        checker_profiles=(),
        findings=(finding_binding,),
        review_artifact_ids=(review.artifact_id,),
    ).model_copy(
        update={
            "subject": patch_mod._subject().model_copy(
                update={
                    "subject_artifact_id": subject.artifact_id,
                    "subject_digest": subject.payload_hash,
                }
            ),
            "base_snapshot_artifact_id": base.artifact_id,
            "preview_snapshot_artifact_id": preview.artifact_id,
            "target": patch_mod._payload().target.model_copy(
                update={
                    "expected_ref": patch_mod.RefValue(
                        artifact_id=base.artifact_id,
                        revision=1,
                    )
                }
            ),
            "validation_policy": validation_profile,
        }
    )
    context = _bind_context_to_exact_catalog(
        patch_mod._context(
            store,
            payload,
            resolved_profiles_override=bindings,
        ),
        catalog,
        bindings,
    )
    outcome = patch_mod._handler(
        store,
        finding_revision_loader=patch_mod._ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=review.artifact_id,
        ),
    )(context)

    assert outcome.summary.outcome_code == "patch_validation_unproven"
    _publish_validation_handler_outcome(
        monkeypatch,
        registry=registry,
        catalog=catalog,
        context=context,
        outcome=outcome,
        store=store,
        input_artifacts=(base, preview, review, subject),
    )

    requirement_id = f"review:{review.artifact_id}"
    companion = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.meta.get("requirement_id") == requirement_id
    )
    forged_payload = json.loads(store.read_prepared(companion.object_ref))
    forged_payload["dimension"] = "validation_input"
    forged_payload["detail"] = {"selected_dimension_count": 0}
    forged = _replace_validation_companion_payload(
        store=store,
        outcome=outcome,
        requirement_id=requirement_id,
        replacement_payload=forged_payload,
    )
    with pytest.raises(IntegrityViolation, match="semantic binding"):
        _publish_validation_handler_outcome(
            monkeypatch,
            registry=registry,
            catalog=catalog,
            context=context,
            outcome=forged,
            store=store,
            input_artifacts=(base, preview, review, subject),
        )


def test_patch_regression_playtest_finding_reaches_real_terminal(monkeypatch):
    from tests.platform.m4c import test_patch_validation_handler as patch_mod

    registry = build_builtin_registry()
    catalog = max(registry.list_execution_profile_catalogs(), key=lambda item: item.catalog_version)
    validation_profile = ProfileRefV1(profile_id="builtin.validation", version=1)
    bindings = (
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/validation_policy",
            profile=validation_profile,
            profile_kind="validation",
        ),
    )
    store = patch_mod._store()
    base_blob = store.read_bytes(patch_mod.BASE_ID)
    preview_blob = store.read_bytes(patch_mod.PREVIEW_ID)
    base_snapshot = patch_mod.load_snapshot(store, patch_mod.BASE_ID)
    preview_snapshot = patch_mod.load_snapshot(store, patch_mod.PREVIEW_ID)
    base = store.register_exact_artifact(
        kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload=base_blob,
        version_tuple=VersionTuple(doc_version="doc@1", ir_snapshot_id=base_snapshot.snapshot_id),
    )
    preview = store.register_exact_artifact(
        kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload=preview_blob,
        version_tuple=VersionTuple(
            doc_version="doc@1", ir_snapshot_id=preview_snapshot.snapshot_id
        ),
    )
    subject = store.register_exact_artifact(
        kind="patch",
        payload_schema_id="patch@2",
        payload={"patch_schema_version": "patch@2", "ops": []},
        version_tuple=VersionTuple(tool_version="patch@2"),
    )
    suite = store.register_exact_artifact(
        kind="regression_suite",
        payload_schema_id="regression-suite@1",
        payload={"suite": "terminal-playtest"},
        version_tuple=VersionTuple(
            env_contract_version="suite-env@1",
            tool_version="regression-suite@1",
        ),
    )
    payload = patch_mod._payload(
        checker_profiles=(),
        regression_suite_artifact_ids=(suite.artifact_id,),
    ).model_copy(
        update={
            "subject": patch_mod._subject().model_copy(
                update={
                    "subject_artifact_id": subject.artifact_id,
                    "subject_digest": subject.payload_hash,
                }
            ),
            "base_snapshot_artifact_id": base.artifact_id,
            "preview_snapshot_artifact_id": preview.artifact_id,
            "target": patch_mod._payload().target.model_copy(
                update={
                    "expected_ref": patch_mod.RefValue(artifact_id=base.artifact_id, revision=1)
                }
            ),
            "validation_policy": validation_profile,
        }
    )
    context = _bind_context_to_exact_catalog(
        patch_mod._context(
            store,
            payload,
            seed=17,
            resolved_profiles_override=bindings,
        ),
        catalog,
        bindings,
    )
    outcome = patch_mod._handler(store, regression_runner=patch_mod._FailingRegressionRunner())(
        context
    )

    assert outcome.summary.outcome_code == "patch_validation_failed"
    assert len(outcome.findings) == 1
    _published, findings, ledger, _artifacts, _blobs = _publish_validation_handler_outcome(
        monkeypatch,
        registry=registry,
        catalog=catalog,
        context=context,
        outcome=outcome,
        store=store,
        input_artifacts=(base, preview, subject, suite),
    )
    assert len(findings.revisions) == 1
    assert findings.revisions[0].payload.source == "playtest"
    assert findings.revisions[0].payload.producer_id == "agent-env-action-replay@1"
    assert len(ledger.links) == 1


def test_constraint_failed_with_candidate_playtest_finding_reaches_real_terminal(
    monkeypatch,
):
    from tests.platform.m4c import test_constraint_validation_handler as constraint_mod

    registry = build_builtin_registry()
    catalog = max(registry.list_execution_profile_catalogs(), key=lambda item: item.catalog_version)
    validation_profile = ProfileRefV1(profile_id="builtin.validation", version=1)
    compiler_profile = ProfileRefV1(profile_id="builtin.constraint_compiler", version=1)
    bindings = (
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/validation_policy",
            profile=validation_profile,
            profile_kind="validation",
        ),
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/compiler_profile",
            profile=compiler_profile,
            profile_kind="constraint_compiler",
        ),
    )
    store = constraint_mod._store(constraint_mod._MIXED)
    base_constraint_snapshot_id = "constraint:base-terminal:1"
    base = store.register_exact_artifact(
        kind="constraint_snapshot",
        payload_schema_id="constraint-snapshot@1",
        payload={"dsl_grammar_version": "dsl@1", "constraints": []},
        version_tuple=VersionTuple(
            ir_snapshot_id=constraint_mod.SOURCE_SNAPSHOT_ID,
            constraint_snapshot_id=base_constraint_snapshot_id,
        ),
    )
    proposal = store.register_exact_artifact(
        kind="constraint_proposal",
        payload_schema_id="constraint-proposal@1",
        payload=constraint_mod._proposal(constraint_mod._MIXED).model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=constraint_mod.SOURCE_SNAPSHOT_ID,
            constraint_snapshot_id=base_constraint_snapshot_id,
            tool_version="constraint-proposal@1",
        ),
    )
    suite = store.register_exact_artifact(
        kind="regression_suite",
        payload_schema_id="regression-suite@1",
        payload={"suite": "terminal-playtest"},
        version_tuple=VersionTuple(
            env_contract_version="suite-env@1",
            tool_version="regression-suite@1",
        ),
    )
    golden = store.register_exact_artifact(
        kind="golden_suite",
        payload_schema_id="golden-suite@1",
        payload={"suite": "terminal-golden"},
        version_tuple=VersionTuple(tool_version="golden-suite@1"),
    )
    payload = constraint_mod._payload(
        base=base.artifact_id,
        regression=(suite.artifact_id,),
        golden=golden.artifact_id,
    ).model_copy(
        update={
            "subject": constraint_mod._subject().model_copy(
                update={
                    "subject_artifact_id": proposal.artifact_id,
                    "subject_digest": proposal.payload_hash,
                }
            ),
            "target": constraint_mod._payload().target.model_copy(
                update={
                    "expected_ref": constraint_mod.RefValue(
                        artifact_id=base.artifact_id, revision=1
                    )
                }
            ),
            "validation_policy": validation_profile,
            "compiler_profile": compiler_profile,
        }
    )
    context = _bind_context_to_exact_catalog(
        constraint_mod._context(
            store,
            payload,
            seed=17,
            version_tuple=VersionTuple(
                ir_snapshot_id=constraint_mod.SOURCE_SNAPSHOT_ID,
                constraint_snapshot_id=base_constraint_snapshot_id,
                tool_version="constraint-proposal@1",
                seed=17,
            ),
        ),
        catalog,
        bindings,
    )
    outcome = constraint_mod._handler(
        store,
        golden_runner=constraint_mod._PassingGoldenRunner(),
        regression_runner=constraint_mod._FailingRegressionRunner(),
    )(context)

    assert outcome.summary.outcome_code == "constraint_validation_failed_with_candidate"
    assert len(outcome.findings) == 1
    prepared_compile = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.payload_schema_id == "constraint-compile-evidence@1"
    )
    assert golden.artifact_id not in prepared_compile.lineage
    assert all(
        golden.artifact_id not in artifact.lineage
        for artifact in outcome.artifacts
        if artifact is not prepared_compile
    )
    published, findings, ledger, published_artifacts, published_blobs = (
        _publish_validation_handler_outcome(
            monkeypatch,
            registry=registry,
            catalog=catalog,
            context=context,
            outcome=outcome,
            store=store,
            input_artifacts=(base, golden, proposal, suite),
        )
    )
    assert len(findings.revisions) == 1
    assert findings.revisions[0].payload.source == "playtest"
    assert findings.revisions[0].payload.producer_id == "agent-env-action-replay@1"
    assert len(ledger.links) == 1
    result_manifest = published_artifacts.by_id[published.result_artifact_id]
    result_payload = json.loads(published_blobs.read(result_manifest.object_ref))
    final_compile = next(
        published_artifacts.by_id[artifact_id]
        for artifact_id in result_payload["produced_artifact_ids"]
        if published_artifacts.by_id[artifact_id].meta.get("payload_schema_id")
        == "constraint-compile-evidence@1"
    )
    assert golden.artifact_id not in final_compile.lineage

    candidate_index = next(
        index
        for index, artifact in enumerate(outcome.artifacts)
        if artifact.payload_schema_id == "constraint-snapshot@1"
    )
    candidate_artifact = outcome.artifacts[candidate_index]
    forged_candidate_payload = json.loads(store.read_prepared(candidate_artifact.object_ref))
    next(
        constraint
        for constraint in forged_candidate_payload["constraints"]
        if constraint["id"] == "C_cap"
    )["assert"] = "reward_gold <= 999"
    forged_candidate_blob = canonical_json(forged_candidate_payload).encode()
    forged_candidate_ref, forged_candidate_location = store.put_prepared(forged_candidate_blob)
    candidate_artifacts = list(outcome.artifacts)
    candidate_artifacts[candidate_index] = candidate_artifact.model_copy(
        update={
            "object_ref": forged_candidate_ref,
            "location": forged_candidate_location,
            "payload_hash": forged_candidate_ref.sha256,
        }
    )
    with pytest.raises(IntegrityViolation):
        _publish_validation_handler_outcome(
            monkeypatch,
            registry=registry,
            catalog=catalog,
            context=context,
            outcome=outcome.model_copy(update={"artifacts": tuple(candidate_artifacts)}),
            store=store,
            input_artifacts=(base, golden, proposal, suite),
        )

    compile_index = next(
        index
        for index, artifact in enumerate(outcome.artifacts)
        if artifact.payload_schema_id == "constraint-compile-evidence@1"
    )
    compile_artifact = outcome.artifacts[compile_index]
    old_compile_id = constraint_mod.content_addressed_artifact_id(compile_artifact)
    primary_artifact = outcome.artifacts[outcome.primary_index]

    def forged_outcome(mutator):
        forged_payload = json.loads(store.read_prepared(compile_artifact.object_ref))
        mutator(forged_payload)
        forged_blob = canonical_json(forged_payload).encode()
        forged_ref, forged_location = store.put_prepared(forged_blob)
        forged_artifacts = list(outcome.artifacts)
        forged_compile_artifact = compile_artifact.model_copy(
            update={
                "object_ref": forged_ref,
                "location": forged_location,
                "payload_hash": forged_ref.sha256,
            }
        )
        forged_artifacts[compile_index] = forged_compile_artifact
        forged_compile_id = constraint_mod.content_addressed_artifact_id(forged_compile_artifact)
        forged_primary_payload = json.loads(store.read_prepared(primary_artifact.object_ref))
        forged_primary_payload["supporting_artifact_ids"] = sorted(
            forged_compile_id if artifact_id == old_compile_id else artifact_id
            for artifact_id in forged_primary_payload["supporting_artifact_ids"]
        )
        for requirement in forged_primary_payload["requirements"]:
            if requirement.get("evidence_artifact_id") == old_compile_id:
                requirement["evidence_artifact_id"] = forged_compile_id
        forged_primary_blob = canonical_json(forged_primary_payload).encode()
        forged_primary_ref, forged_primary_location = store.put_prepared(forged_primary_blob)
        forged_artifacts[outcome.primary_index] = primary_artifact.model_copy(
            update={
                "object_ref": forged_primary_ref,
                "location": forged_primary_location,
                "payload_hash": forged_primary_ref.sha256,
            }
        )
        return outcome.model_copy(update={"artifacts": tuple(forged_artifacts)})

    artifacts_with_extra_golden = list(outcome.artifacts)
    artifacts_with_extra_golden[compile_index] = compile_artifact.model_copy(
        update={"lineage": tuple(sorted((*compile_artifact.lineage, golden.artifact_id)))}
    )
    with pytest.raises(IntegrityViolation, match="lineage|parent"):
        _publish_validation_handler_outcome(
            monkeypatch,
            registry=registry,
            catalog=catalog,
            context=context,
            outcome=outcome.model_copy(update={"artifacts": tuple(artifacts_with_extra_golden)}),
            store=store,
            input_artifacts=(base, golden, proposal, suite),
        )

    def forge_engine_id(payload):
        next(stage for stage in payload["stages"] if stage["stage"] == "differential")[
            "engine_id"
        ] = "forged"

    def forge_noncanonical_engine_version(payload):
        next(stage for stage in payload["stages"] if stage["stage"] == "differential")[
            "engine_version"
        ] = "01"

    def forge_same_engine(payload):
        stages = [stage for stage in payload["stages"] if stage["stage"] == "differential"]
        stages[1]["engine_id"] = stages[0]["engine_id"]
        stages[1]["engine_version"] = "99"

    def forge_reason_code(payload):
        stage = next(
            stage
            for stage in payload["stages"]
            if stage["stage"] == "differential" and stage["status"] == "passed"
        )
        stage["status"] = "unproven"
        stage["reason_code"] = "forged_reason"
        payload["overall_status"] = "unproven"

    def forge_cross_engine_reason_code(payload):
        stage = next(
            stage
            for stage in payload["stages"]
            if stage["stage"] == "differential"
            and stage["engine_id"] == "clingo"
            and stage["status"] == "passed"
        )
        stage["status"] = "unproven"
        stage["reason_code"] = "z3_budget_exhausted"
        payload["overall_status"] = "unproven"

    def forge_golden_not_applicable(payload):
        golden_stage = next(stage for stage in payload["stages"] if stage["stage"] == "golden")
        golden_stage["status"] = "not_applicable"
        golden_stage["reason_code"] = "golden_suite_absent"

    for mutator in (
        forge_engine_id,
        forge_noncanonical_engine_version,
        forge_same_engine,
        forge_reason_code,
        forge_cross_engine_reason_code,
        forge_golden_not_applicable,
    ):
        with pytest.raises(IntegrityViolation, match="compile evidence"):
            _publish_validation_handler_outcome(
                monkeypatch,
                registry=registry,
                catalog=catalog,
                context=context,
                outcome=forged_outcome(mutator),
                store=store,
                input_artifacts=(base, golden, proposal, suite),
            )

    invalid_constraint = constraint_mod._constraint(
        "C_invalid_terminal",
        "__import__('os').system('forbidden')",
    )
    invalid_proposal = store.register_exact_artifact(
        kind="constraint_proposal",
        payload_schema_id="constraint-proposal@1",
        payload=constraint_mod._proposal((invalid_constraint,)).model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=constraint_mod.SOURCE_SNAPSHOT_ID,
            constraint_snapshot_id=base_constraint_snapshot_id,
            tool_version="constraint-proposal@1",
        ),
    )
    invalid_payload = constraint_mod._payload(base=base.artifact_id).model_copy(
        update={
            "subject": constraint_mod._subject().model_copy(
                update={
                    "subject_artifact_id": invalid_proposal.artifact_id,
                    "subject_digest": invalid_proposal.payload_hash,
                }
            ),
            "target": constraint_mod._payload().target.model_copy(
                update={
                    "expected_ref": constraint_mod.RefValue(
                        artifact_id=base.artifact_id,
                        revision=1,
                    )
                }
            ),
            "validation_policy": validation_profile,
            "compiler_profile": compiler_profile,
        }
    )
    invalid_context = _bind_context_to_exact_catalog(
        constraint_mod._context(
            store,
            invalid_payload,
            version_tuple=VersionTuple(
                ir_snapshot_id=constraint_mod.SOURCE_SNAPSHOT_ID,
                constraint_snapshot_id=base_constraint_snapshot_id,
                tool_version="constraint-proposal@1",
            ),
        ),
        catalog,
        bindings,
    )
    invalid_outcome = constraint_mod._handler(
        store,
    )(invalid_context)
    assert invalid_outcome.summary.outcome_code == (
        "constraint_validation_failed_without_candidate"
    )
    _publish_validation_handler_outcome(
        monkeypatch,
        registry=registry,
        catalog=catalog,
        context=invalid_context,
        outcome=invalid_outcome,
        store=store,
        input_artifacts=(base, invalid_proposal),
    )


def test_rollback_regression_playtest_finding_reaches_real_terminal(monkeypatch):
    from tests.platform.m4c import test_rollback_validation_handler as rollback_mod

    registry = build_builtin_registry()
    catalog = max(registry.list_execution_profile_catalogs(), key=lambda item: item.catalog_version)
    rollback_profile = ProfileRefV1(profile_id="builtin.rollback", version=1)
    schema_profile = ProfileRefV1(profile_id="builtin.schema_compatibility", version=1)
    impact_profile = ProfileRefV1(profile_id="builtin.impact_analysis", version=1)
    bindings = (
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/rollback_profile",
            profile=rollback_profile,
            profile_kind="rollback",
        ),
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/schema_compatibility_policy",
            profile=schema_profile,
            profile_kind="schema_compatibility",
        ),
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/impact_profiles/0",
            profile=impact_profile,
            profile_kind="impact_analysis",
        ),
    )
    store = rollback_mod._store()
    current = store.register_exact_artifact(
        kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload=rollback_mod._TARGET_SNAPSHOT.content_payload,
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:current-terminal"),
    )
    target = store.register_exact_artifact(
        kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload=rollback_mod._TARGET_SNAPSHOT.content_payload,
        version_tuple=VersionTuple(ir_snapshot_id=rollback_mod._TARGET_SNAPSHOT.snapshot_id),
    )
    current_ref = rollback_mod.RefValue(artifact_id=current.artifact_id, revision=5)
    subject_payload = rollback_mod._rollback_request().model_copy(
        update={
            "expected_current_ref": current_ref,
            "target_artifact_id": target.artifact_id,
            "rollback_profile_binding": bindings[0],
        }
    )
    subject = store.register_exact_artifact(
        kind="rollback_request",
        payload_schema_id="rollback-request@1",
        payload=subject_payload.model_dump(mode="json"),
        version_tuple=VersionTuple(tool_version="rollback-request@1"),
    )
    suite = store.register_exact_artifact(
        kind="regression_suite",
        payload_schema_id="regression-suite@1",
        payload={"suite": "terminal-playtest"},
        version_tuple=VersionTuple(
            env_contract_version="suite-env@1",
            tool_version="regression-suite@1",
        ),
    )
    payload = rollback_mod._payload(
        impact_profiles=(impact_profile,),
        regression=(suite.artifact_id,),
    ).model_copy(
        update={
            "subject": rollback_mod._subject().model_copy(
                update={
                    "subject_artifact_id": subject.artifact_id,
                    "subject_digest": subject.payload_hash,
                }
            ),
            "expected_current_ref": current_ref,
            "target_artifact_id": target.artifact_id,
            "rollback_profile": rollback_profile,
            "schema_compatibility_policy": schema_profile,
        }
    )
    context = _bind_context_to_exact_catalog(
        rollback_mod._context(store, payload, seed=17),
        catalog,
        bindings,
    )
    outcome = rollback_mod._handler(
        store, regression_runner=rollback_mod._FailingRegressionRunner()
    )(context)

    assert outcome.summary.outcome_code == "rollback_validation_failed"
    assert len(outcome.findings) == 1
    _published, findings, ledger, _artifacts, _blobs = _publish_validation_handler_outcome(
        monkeypatch,
        registry=registry,
        catalog=catalog,
        context=context,
        outcome=outcome,
        store=store,
        input_artifacts=(current, subject, suite, target),
    )
    assert len(findings.revisions) == 1
    assert findings.revisions[0].payload.source == "playtest"
    assert findings.revisions[0].payload.producer_id == "agent-env-action-replay@1"
    assert len(ledger.links) == 1


def test_rollback_terminal_rejects_history_requirement_labeled_as_schema(monkeypatch):
    from tests.platform.m4c import test_rollback_validation_handler as rollback_mod

    registry = build_builtin_registry()
    catalog = max(registry.list_execution_profile_catalogs(), key=lambda item: item.catalog_version)
    rollback_profile = ProfileRefV1(profile_id="builtin.rollback", version=1)
    schema_profile = ProfileRefV1(profile_id="builtin.schema_compatibility", version=1)
    impact_profile = ProfileRefV1(profile_id="builtin.impact_analysis", version=1)
    bindings = (
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/rollback_profile",
            profile=rollback_profile,
            profile_kind="rollback",
        ),
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/schema_compatibility_policy",
            profile=schema_profile,
            profile_kind="schema_compatibility",
        ),
        _exact_profile_binding(
            registry,
            catalog,
            field_path="/params/impact_profiles/0",
            profile=impact_profile,
            profile_kind="impact_analysis",
        ),
    )
    store = rollback_mod._store()
    current = store.register_exact_artifact(
        kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload=rollback_mod._TARGET_SNAPSHOT.content_payload,
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:current-history-terminal"),
    )
    target = store.register_exact_artifact(
        kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload=rollback_mod._TARGET_SNAPSHOT.content_payload,
        version_tuple=VersionTuple(ir_snapshot_id=rollback_mod._TARGET_SNAPSHOT.snapshot_id),
    )
    current_ref = rollback_mod.RefValue(artifact_id=current.artifact_id, revision=5)
    subject_payload = rollback_mod._rollback_request().model_copy(
        update={
            "expected_current_ref": current_ref,
            "target_artifact_id": target.artifact_id,
            "rollback_profile_binding": bindings[0],
        }
    )
    subject = store.register_exact_artifact(
        kind="rollback_request",
        payload_schema_id="rollback-request@1",
        payload=subject_payload.model_dump(mode="json"),
        version_tuple=VersionTuple(tool_version="rollback-request@1"),
    )
    payload = rollback_mod._payload(
        impact_profiles=(impact_profile,),
        regression=(),
    ).model_copy(
        update={
            "subject": rollback_mod._subject().model_copy(
                update={
                    "subject_artifact_id": subject.artifact_id,
                    "subject_digest": subject.payload_hash,
                }
            ),
            "expected_current_ref": current_ref,
            "target_artifact_id": target.artifact_id,
            "rollback_profile": rollback_profile,
            "schema_compatibility_policy": schema_profile,
        }
    )
    context = _bind_context_to_exact_catalog(
        rollback_mod._context(store, payload),
        catalog,
        bindings,
    )
    outcome = rollback_mod._handler(store)(context)

    assert outcome.summary.outcome_code == "rollback_validation_passed"
    _publish_validation_handler_outcome(
        monkeypatch,
        registry=registry,
        catalog=catalog,
        context=context,
        outcome=outcome,
        store=store,
        input_artifacts=(current, subject, target),
    )

    history = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.meta.get("requirement_id") == "history"
    )
    forged_payload = json.loads(store.read_prepared(history.object_ref))
    forged_payload["dimension"] = "schema"
    forged_payload["detail"] = {
        **forged_payload["detail"],
        "schema_profile_binding": bindings[1].model_dump(mode="json"),
        "rollback_profile_binding": bindings[0].model_dump(mode="json"),
    }
    forged = _replace_validation_companion_payload(
        store=store,
        outcome=outcome,
        requirement_id="history",
        replacement_payload=forged_payload,
        replacement_requirement_kind="schema",
    )
    with pytest.raises(IntegrityViolation, match="semantic binding"):
        _publish_validation_handler_outcome(
            monkeypatch,
            registry=registry,
            catalog=catalog,
            context=context,
            outcome=forged,
            store=store,
            input_artifacts=(current, subject, target),
        )
