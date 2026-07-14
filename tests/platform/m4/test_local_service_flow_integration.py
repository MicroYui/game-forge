from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from gameforge.contracts.canonical import canonical_json, compute_snapshot_id
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileCatalogSnapshotV1,
    ExecutionProfileDefinitionV1,
    ExecutionProfileLifecycleV1,
    GenericProfileDetailsV1,
    ProfileRefV1,
    RunKindRef,
    ValidationProfileDetailsV1,
    canonical_config_hash,
    execution_profile_catalog_digest,
)
from gameforge.contracts.findings import PatchV2, TypedOp
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRoutePolicyRefV1,
    DomainScope,
    Principal,
)
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.jobs import (
    FailureClassifierRefV1,
    PatchValidationPayloadV1,
    RefReadBindingV1,
    RetryPolicyRefV1,
    RollbackValidationPayloadV1,
    RunEvent,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunPayloadEnvelope,
    RunQueuedDataV1,
    RunRecord,
    RunSucceededDataV1,
    ValidationSubjectBindingV1,
    VersionTransitionPolicyRefV1,
    canonical_payload_hash,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    AuditCorrelation,
    AuditSubject,
    VersionTuple,
    build_artifact_v2,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalPolicyRegistryV1,
    EvidenceSet,
    PatchTargetBindingV1,
    RollbackRequestV1,
    RollbackTargetBindingV1,
    compute_approval_policy_registry_digest,
)
from gameforge.platform.approvals import build_approval_requirements
from gameforge.platform.approvals.apply import (
    ApprovedApplyCapabilities,
    ApprovedApplyRequest,
    ApprovedApplyService,
    ExactRollbackExecutionVerifier,
    VerifiedTargetPayload,
)
from gameforge.platform.approvals.commands import (
    ApprovalCommandCapabilities,
    ApprovalCommandContext,
    ApprovalCommandService,
    DraftSubjectFacts,
    EvidenceStateProjection,
    PreparedDraft,
    PreparedObjectBinding,
    PreparedValidationStart,
)
from gameforge.platform.approvals.validation import (
    PreparedValidationCompletion,
    ResolvedValidationProfiles,
    ValidationCompletionCapabilities,
    ValidationCompletionService,
    ValidationRunBinding,
    ValidationRunTerminalResult,
)
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.diff import SnapshotDiffService
from gameforge.platform.lineage.store import SqlArtifactStore
from gameforge.platform.lineage.validation import (
    ProducerValidationContext,
    validate_artifact_producer,
)
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.platform.storage.object_gc import NoRecoveryPins, ObjectGcService
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import ArtifactRow, AuditRow, Base, RefTransitionRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.ref_transitions import SqlRefTransitionRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import apply_patch
from tests.platform.m4 import apply_testkit


NOW_DT = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-14T12:00:00Z"
RUN_CREATED = "2026-07-14T11:59:59Z"
RUN_STARTED = "2026-07-14T11:59:59.100000Z"
RUN_ENDED = "2026-07-14T11:59:59.200000Z"
RUN_LEASE_EXPIRES = "2026-07-14T12:00:59Z"
RUN_ATTEMPT_DEADLINE = "2026-07-14T12:00:00.100000Z"
RUN_OVERALL_DEADLINE = "2026-07-14T12:09:59Z"
AUDIT_CHAIN_ID = "platform-authority"
REF_NAME = "content/head"
CURSOR_KEY = b"m4a-local-service-flow-cursor-key"
OBJECT_CURSOR_KEY = b"m4a-local-service-flow-object-cursor-key"
HASH_A = "a" * 64
HASH_B = "b" * 64

PATCH_PROFILE = ProfileRefV1(profile_id="validation.patch", version=1)
ROLLBACK_PROFILE = ProfileRefV1(profile_id="rollback.content", version=1)
COMPATIBILITY_PROFILE = ProfileRefV1(
    profile_id="schema-compatibility.content",
    version=1,
)


@dataclass(frozen=True)
class _StoredArtifact:
    artifact: ArtifactV2
    payload: bytes
    binding: PreparedObjectBinding


@dataclass(frozen=True)
class _PreparedRunResult:
    stored: _StoredArtifact
    projection: RunManifestVersionProjectionV1


def _payload_bytes(value: object) -> bytes:
    wire = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return canonical_json(wire).encode("utf-8")


def _producer_context(artifact: ArtifactV2) -> ProducerValidationContext:
    expected: dict[str, object] = {}
    if artifact.version_tuple.doc_version is not None:
        expected["doc_version"] = artifact.version_tuple.doc_version
    if artifact.version_tuple.ir_snapshot_id is not None:
        expected["ir_snapshot_id"] = artifact.version_tuple.ir_snapshot_id
    if artifact.version_tuple.constraint_snapshot_id is not None:
        expected["constraint_snapshot_id"] = artifact.version_tuple.constraint_snapshot_id
    return ProducerValidationContext(expected_versions=expected)


def _prepare_artifact(
    objects: LocalObjectStore,
    *,
    kind: str,
    payload: bytes,
    version_tuple: VersionTuple,
    lineage: tuple[str, ...] = (),
    producer_context: ProducerValidationContext | None = None,
) -> _StoredArtifact:
    stored = objects.put_verified(payload)
    artifact = build_artifact_v2(
        kind=kind,  # type: ignore[arg-type]
        version_tuple=version_tuple,
        lineage=lineage,
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        created_at=NOW,
    )
    report = validate_artifact_producer(
        artifact,
        producer_context or _producer_context(artifact),
    )
    assert report.status == "valid"
    return _StoredArtifact(
        artifact=artifact,
        payload=payload,
        binding=PreparedObjectBinding(
            object_ref=stored.ref,
            location=stored.location,
            expected_revision=None,
        ),
    )


class _SqlAuthorityCatalog:
    """Real Artifact, policy, profile, and idempotency repositories in one UoW slot."""

    def __init__(
        self,
        session: Session,
        *,
        bindings: SqlObjectBindingRepository,
        clock: FrozenUtcClock,
    ) -> None:
        self._artifacts = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=clock),
            clock=clock,
            snapshot_ttl=timedelta(minutes=5),
        )
        self._policies = SqlPolicySnapshotRepository(session, clock=clock)
        self._idempotency = SqlIdempotencyRepository(session, clock=clock)

    def get(self, artifact_id: str) -> ArtifactV2 | None:
        artifact = self._artifacts.get(artifact_id)
        return artifact if isinstance(artifact, ArtifactV2) else None

    def put(self, artifact: ArtifactV2) -> ArtifactV2:
        retained = self._artifacts.put(artifact)
        if not isinstance(retained, ArtifactV2):  # pragma: no cover - repository invariant
            raise IntegrityViolation("ArtifactV2 repository returned legacy content")
        return retained

    def get_domain_registry(self, ref: object) -> object | None:
        return self._policies.get_domain_registry(ref)  # type: ignore[arg-type]

    def get_domain_route_policy(self, ref: object) -> object | None:
        return self._policies.get_domain_route_policy(ref)  # type: ignore[arg-type]

    def get_role_policy(self, version: str, digest: str) -> object | None:
        return self._policies.get_role_policy(version, digest)

    def get_approval_policy(self, ref: object) -> object | None:
        return self._policies.get_approval_policy(ref)  # type: ignore[arg-type]

    def get_result(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        return self._idempotency.get_result(
            scope=scope,
            operation=operation,
            key=key,
            request_hash=request_hash,
        )

    def put_result(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        request_hash: str,
        resource_kind: str,
        resource_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        return self._idempotency.put_result(
            scope=scope,
            operation=operation,
            key=key,
            request_hash=request_hash,
            resource_kind=resource_kind,
            resource_id=resource_id,
            response=response,
        )

    def resolve(self, execution: ValidationRunBinding) -> ResolvedValidationProfiles:
        # M4a exposes the protocol but not a production composition root. This
        # test-local typed catalog projection resolves every binding from the real,
        # immutable SqlPolicySnapshotRepository rather than accepting fixture data.
        payload = execution.payload
        if isinstance(payload, PatchValidationPayloadV1):
            field_path = "/params/validation_policy"
            policy_version = "patch-validation-policy@1"
        else:
            field_path = "/params/rollback_profile"
            policy_version = "rollback-validation-policy@1"
        matches = tuple(
            binding for binding in execution.resolved_profiles if binding.field_path == field_path
        )
        if len(matches) != 1:
            raise IntegrityViolation("validation Run has no exact primary profile")
        for binding in execution.resolved_profiles:
            self._policies.resolve_execution_profile_binding(binding)
        return ResolvedValidationProfiles(
            evidence_policy_version=policy_version,
            primary=matches[0],
        )

    def resolve_execution_profile_binding(self, binding: object) -> tuple[object, object]:
        return self._policies.resolve_execution_profile_binding(binding)  # type: ignore[arg-type]


class _SqlPrincipalProjection:
    def __init__(self, session: Session, *, clock: FrozenUtcClock) -> None:
        self._identities = SqlIdentityRepository(session, clock=clock)

    def get(self, principal_id: str) -> Principal | None:
        return self._identities.project(principal_id)


class _TypedObjectReaders:
    """Test composition for M4c-owned typed readers; SQLite/ObjectStore stay authoritative."""

    def __init__(
        self,
        *,
        artifacts: _SqlAuthorityCatalog,
        bindings: SqlObjectBindingRepository,
        objects: LocalObjectStore,
    ) -> None:
        self._artifacts = artifacts
        self._bindings = bindings
        self._objects = objects

    def _read(self, artifact: ArtifactV2) -> bytes:
        retained = self._artifacts.get(artifact.artifact_id)
        if retained is not None and retained != artifact:
            raise IntegrityViolation("typed reader Artifact differs from persistence")
        try:
            location = self._bindings.resolve(artifact.object_ref).location
        except FileNotFoundError:
            if retained is not None:
                raise IntegrityViolation(
                    "persisted Artifact has no authoritative ObjectBinding"
                ) from None
            # Draft bytes are verified before the DB publication transaction. This
            # M4c-owned reader seam locates that exact generation in the real store;
            # it never substitutes an in-memory payload or authoritative binding.
            cursor = None
            location = None
            while True:
                page = self._objects.list_versions(cursor)
                match = next(
                    (stat.location for stat in page.items if stat.ref == artifact.object_ref),
                    None,
                )
                if match is not None:
                    location = match
                    break
                cursor = page.next_cursor
                if cursor is None:
                    break
            if location is None:
                raise IntegrityViolation("preverified draft object generation is unavailable")
        with self._objects.open(location) as source:
            payload = source.read()
        if (
            len(payload) != artifact.object_ref.size_bytes
            or hashlib.sha256(payload).hexdigest() != artifact.object_ref.sha256
        ):
            raise IntegrityViolation("typed reader ObjectRef verification failed")
        return payload

    def _json(self, artifact: ArtifactV2) -> Any:
        try:
            return json.loads(self._read(artifact))
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("typed Artifact payload is not JSON") from exc

    def inspect_draft_subject(self, artifact: ArtifactV2) -> DraftSubjectFacts:
        if artifact.kind == "patch":
            patch = PatchV2.model_validate(self._json(artifact))
            return DraftSubjectFacts(
                subject_kind="patch",
                subject_revision=patch.revision,
                produced_by=patch.produced_by,
                producer_run_id=patch.producer_run_id,
                supersedes_artifact_id=patch.supersedes_artifact_id,
                target_artifact_id=None,
                target_snapshot_id=patch.target_snapshot_id,
            )
        if artifact.kind == "rollback_request":
            request = RollbackRequestV1.model_validate(self._json(artifact))
            target = self._artifacts.get(request.target_artifact_id)
            if target is None:
                raise IntegrityViolation("rollback typed reader cannot resolve target")
            return DraftSubjectFacts(
                subject_kind="rollback_request",
                subject_revision=None,
                produced_by="human",
                producer_run_id=None,
                supersedes_artifact_id=None,
                target_artifact_id=target.artifact_id,
                target_snapshot_id=target.version_tuple.ir_snapshot_id,
                rollback_request=request,
            )
        raise IntegrityViolation("unsupported local-flow subject kind")

    def load_patch(self, artifact: ArtifactV2) -> PatchV2:
        return PatchV2.model_validate(self._json(artifact))

    def load_evidence_set(self, artifact: ArtifactV2) -> EvidenceSet:
        if artifact.kind != "validation_evidence":
            raise IntegrityViolation("evidence reader received another Artifact kind")
        return EvidenceSet.model_validate(self._json(artifact))

    def validate_submission(
        self,
        *,
        item: ApprovalItem,
        subject_artifact: ArtifactV2,
        target_artifact: ArtifactV2,
        evidence_artifact: ArtifactV2,
        regression_artifacts: tuple[ArtifactV2, ...],
    ) -> EvidenceStateProjection:
        if regression_artifacts:
            raise IntegrityViolation("local flow did not freeze regression evidence")
        self.inspect_draft_subject(subject_artifact)
        self.read_verified(target_artifact)
        evidence = self.load_evidence_set(evidence_artifact)
        if (
            evidence.subject_artifact_id != item.subject_artifact_id
            or evidence.subject_digest != item.subject_digest
            or evidence.target_binding != item.target_binding
        ):
            raise IntegrityViolation("typed EvidenceSet differs from ApprovalItem")
        return EvidenceStateProjection(
            validation_status=evidence.overall_status,
            regression_status="not_applicable",
        )

    def project_state(self, *, item: ApprovalItem) -> EvidenceStateProjection:
        if item.evidence_set_artifact_id is None:
            return EvidenceStateProjection(
                validation_status=(
                    "running" if item.active_validation_run_id is not None else "not_started"
                ),
                regression_status="not_started",
            )
        evidence = self._artifacts.get(item.evidence_set_artifact_id)
        if evidence is None:
            raise IntegrityViolation("ApprovalItem evidence Artifact is unavailable")
        return EvidenceStateProjection(
            validation_status=self.load_evidence_set(evidence).overall_status,
            regression_status="not_applicable",
        )

    def read_verified(self, artifact: ArtifactV2) -> VerifiedTargetPayload:
        payload = self._read(artifact)
        wire = json.loads(payload)
        snapshot_id = None
        schema_id = "json@1"
        if artifact.kind == "ir_snapshot":
            snapshot_id = compute_snapshot_id(wire)
            schema_id = "ir-snapshot@1"
            if snapshot_id != artifact.version_tuple.ir_snapshot_id:
                raise IntegrityViolation("snapshot payload differs from VersionTuple")
        return VerifiedTargetPayload(
            artifact=artifact,
            payload_bytes=payload,
            payload_schema_id=schema_id,
            snapshot_id=snapshot_id,
        )


class _ObjectBackedSnapshotViews:
    """Resolve snapshot IDs to canonical payloads without caching payload bytes."""

    def __init__(
        self,
        *,
        readers: _TypedObjectReaders,
        artifacts: tuple[ArtifactV2, ...],
    ) -> None:
        self._readers = readers
        self._artifacts: dict[str, ArtifactV2] = {}
        for artifact in artifacts:
            snapshot_id = artifact.version_tuple.ir_snapshot_id
            if artifact.kind != "ir_snapshot" or snapshot_id is None:
                raise IntegrityViolation("snapshot diff view requires ir_snapshot Artifacts")
            if snapshot_id in self._artifacts:
                raise IntegrityViolation("snapshot diff view IDs must be unique")
            self._artifacts[snapshot_id] = artifact

    def load_canonical_view(self, snapshot_id: str) -> Mapping[str, Any] | None:
        artifact = self._artifacts.get(snapshot_id)
        if artifact is None:
            return None
        verified = self._readers.read_verified(artifact)
        if verified.snapshot_id != snapshot_id:
            raise IntegrityViolation("snapshot diff view resolved another snapshot")
        try:
            payload = json.loads(verified.payload_bytes)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("snapshot diff view is not JSON") from exc
        if not isinstance(payload, Mapping):
            raise IntegrityViolation("snapshot diff view is not a canonical object")
        return payload


class _DeterministicPublicationVerifier:
    """Bridge service protocols to the real M4a producer matrix and exact DAG rules."""

    @staticmethod
    def _validate(artifact: ArtifactV2) -> None:
        if validate_artifact_producer(artifact, _producer_context(artifact)).status != "valid":
            raise IntegrityViolation("Artifact producer validation did not pass")

    def validate_draft_publication(
        self,
        *,
        prepared: PreparedDraft,
        retained_parent_ids: tuple[str, ...],
    ) -> None:
        subject = prepared.subject_artifact
        for artifact in prepared.artifacts:
            self._validate(artifact)
        if subject.kind == "patch":
            if len(subject.lineage) != 1 or len(prepared.companion_artifacts) != 1:
                raise IntegrityViolation("Patch draft requires its exact base and preview")
            preview = prepared.companion_artifacts[0]
            if set(preview.lineage) != {subject.lineage[0], subject.artifact_id}:
                raise IntegrityViolation("preview direct parents must be base plus Patch")
            expected_retained = subject.lineage
        else:
            if subject.kind != "rollback_request" or len(subject.lineage) != 2:
                raise IntegrityViolation("RollbackRequest must bind current and target Artifacts")
            expected_retained = subject.lineage
        if retained_parent_ids != tuple(sorted(expected_retained)):
            raise IntegrityViolation("draft retained parents differ from exact lineage")

    def validate_publication(
        self,
        *,
        prepared: PreparedValidationCompletion,
        item: ApprovalItem,
        retained_parent_ids: tuple[str, ...],
    ) -> None:
        evidence = prepared.evidence_set_artifact
        if evidence is None or item.target_binding is None:
            raise IntegrityViolation("validation publication has no exact evidence target")
        self._validate(evidence)
        expected = tuple(sorted((item.subject_artifact_id, item.target_binding.target_artifact_id)))
        if evidence.lineage != expected or retained_parent_ids != expected:
            raise IntegrityViolation("EvidenceSet direct lineage is not exact")


class _SqlRuntimeGateways:
    """Test-only M4c orchestration seams backed by the real fenced Run repository."""

    def __init__(
        self,
        session: Session,
        *,
        catalog: ExecutionProfileCatalogSnapshotV1,
        authority: _SqlAuthorityCatalog,
        bindings: SqlObjectBindingRepository,
        readers: _TypedObjectReaders,
        run_results: dict[str, _PreparedRunResult],
    ) -> None:
        self._runs = SqlRunRepository(session)
        self._transitions = SqlRefTransitionRepository(session)
        self._profiles = SqlPolicySnapshotRepository(
            session,
            clock=FrozenUtcClock(NOW_DT),
        )
        self._catalog = catalog
        self._authority = authority
        self._bindings = bindings
        self._readers = readers
        self._run_results = run_results

    def get_run(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def get_transition(self, transition_id: str) -> object | None:
        return self._transitions.get(transition_id)

    def put_transition(self, transition: object) -> object:
        return self._transitions.put(transition)  # type: ignore[arg-type]

    def verify_producer_membership(self, **_: object) -> None:
        raise IntegrityViolation("human local-flow subjects have no producer Run membership")

    def request_validation_cancel(self, **_: object) -> None:
        raise IntegrityViolation("local flow does not cancel validation")

    def _resolved(
        self,
        *,
        field_path: str,
        profile: ProfileRefV1,
        kind: str,
    ) -> object:
        return self._profiles.resolve_execution_profile(
            catalog_version=self._catalog.catalog_version,
            catalog_digest=self._catalog.catalog_digest,
            field_path=field_path,
            profile=profile,
            expected_profile_kind=kind,  # type: ignore[arg-type]
        )

    def start_validation(
        self,
        *,
        prepared: PreparedValidationStart,
        item: ApprovalItem,
        initiated_by: AuditActor,
    ) -> str:
        if item.target_binding is None:
            raise IntegrityViolation("validation Run requires an exact target binding")
        subject = ValidationSubjectBindingV1(
            approval_id=item.approval_id,
            expected_workflow_revision=item.workflow_revision + 1,
            subject_head_revision=item.subject_revision,
            subject_artifact_id=item.subject_artifact_id,
            subject_digest=item.subject_digest,
            active_validation_run_id=prepared.run_id,
        )
        if isinstance(item.target_binding, PatchTargetBindingV1):
            primary = self._resolved(
                field_path="/params/validation_policy",
                profile=PATCH_PROFILE,
                kind="validation",
            )
            params = PatchValidationPayloadV1(
                subject=subject,
                base_snapshot_artifact_id=item.target_binding.expected_ref.artifact_id,  # type: ignore[union-attr]
                preview_snapshot_artifact_id=item.target_binding.target_artifact_id,
                candidate_config_export_artifact_ids=(),
                target=RefReadBindingV1(
                    ref_name=item.target_binding.ref_name,
                    expected_ref=item.target_binding.expected_ref,
                ),
                validation_policy=PATCH_PROFILE,
                checker_profiles=(),
                simulation_profiles=(),
                findings=(),
                review_artifact_ids=(),
                playtest_trace_artifact_ids=(),
                regression_suite_artifact_ids=(),
            )
            run_kind = RunKindRef(kind="patch.validate", version=1)
            resolved_profiles = (primary,)
            schema_id = "patch-validation@1"
        else:
            facts = self._readers.inspect_draft_subject(
                self._authority.get(item.subject_artifact_id)  # type: ignore[arg-type]
            )
            request = facts.rollback_request
            if request is None:
                raise IntegrityViolation("rollback Run starter could not parse request")
            primary = self._resolved(
                field_path="/params/rollback_profile",
                profile=request.rollback_profile_binding.profile,
                kind="rollback",
            )
            compatibility = self._resolved(
                field_path="/params/schema_compatibility_policy",
                profile=COMPATIBILITY_PROFILE,
                kind="schema_compatibility",
            )
            params = RollbackValidationPayloadV1(
                subject=subject,
                ref_name=request.ref_name,
                expected_current_ref=request.expected_current_ref,
                target_artifact_id=request.target_artifact_id,
                target_history_revision=request.target_history_revision,
                rollback_profile=request.rollback_profile_binding.profile,
                schema_compatibility_policy=COMPATIBILITY_PROFILE,
                impact_profiles=(),
                regression_suite_artifact_ids=(),
            )
            run_kind = RunKindRef(kind="rollback.validate", version=1)
            resolved_profiles = tuple(
                sorted((primary, compatibility), key=lambda value: value.field_path)
            )
            schema_id = "rollback-validation@1"

        input_artifact_ids = tuple(
            sorted(
                {
                    item.subject_artifact_id,
                    item.target_binding.target_artifact_id,
                    *(
                        ()
                        if item.target_binding.expected_ref is None
                        else (item.target_binding.expected_ref.artifact_id,)
                    ),
                }
            )
        )
        payload = RunPayloadEnvelope(
            payload_schema_version=schema_id,
            input_artifact_ids=input_artifact_ids,
            version_tuple=VersionTuple(
                ir_snapshot_id=item.target_binding.target_snapshot_id,
                tool_version="local-validation@1",
            ),
            policy_bindings=(),
            schema_bindings=(),
            execution_profile_catalog_version=self._catalog.catalog_version,
            execution_profile_catalog_digest=self._catalog.catalog_digest,
            resolved_profiles=resolved_profiles,
            resolved_policy_snapshots=(),
            budget_set_snapshot_id="budget-set:local-validation",
            llm_execution_mode="not_applicable",
            params=params,
        )
        run = RunRecord(
            run_id=prepared.run_id,
            kind=run_kind,
            status="queued",
            revision=1,
            idempotency_scope=f"approval:{item.approval_id}",
            idempotency_key=f"validation:{item.approval_id}",
            request_hash=hashlib.sha256(prepared.run_id.encode()).hexdigest(),
            payload=payload,
            payload_hash=canonical_payload_hash(payload),
            run_kind_definition_digest=HASH_A,
            outcome_policy_set_digest=HASH_B,
            failure_classifier=FailureClassifierRefV1(
                classifier_version=1,
                classifier_digest=HASH_A,
            ),
            initiated_by=initiated_by,
            queue_deadline_utc=RUN_LEASE_EXPIRES,
            attempt_timeout_ns=1_000_000_000,
            overall_deadline_utc=RUN_OVERALL_DEADLINE,
            next_attempt_no=1,
            next_fencing_token=1,
            next_event_seq=2,
            budget_set_snapshot_id=payload.budget_set_snapshot_id,
            run_budget_hold_group_id=f"budget-hold:{prepared.run_id}",
            retry_policy=RetryPolicyRefV1(
                retry_policy_id="retry:none",
                retry_policy_version=1,
                retry_policy_digest=HASH_B,
            ),
            max_attempts=1,
            created_at=RUN_CREATED,
            updated_at=RUN_CREATED,
        )
        queued = RunEvent(
            run_id=run.run_id,
            seq=1,
            event_type="run.queued",
            occurred_at=RUN_CREATED,
            data_schema_version="run-queued@1",
            data=RunQueuedDataV1(
                run_kind=run.kind,
                queue_deadline_utc=run.queue_deadline_utc,
                overall_deadline_utc=run.overall_deadline_utc,
            ),
        )
        self._runs.create_queued(run, queued)
        claim = self._runs.claim(
            run_id=run.run_id,
            expected_revision=1,
            worker_principal_id="service:local-validator",
            lease_id=f"lease:{run.run_id}",
            acquired_at=RUN_CREATED,
            expires_at=RUN_LEASE_EXPIRES,
            permit_group_id=f"permit:{run.run_id}",
        )
        self._runs.start_attempt(
            run_id=run.run_id,
            attempt_no=claim.attempt.attempt_no,
            expected_run_revision=claim.run.revision,
            lease_id=claim.lease.lease_id,
            fencing_token=claim.attempt.fencing_token,
            started_at=RUN_STARTED,
            attempt_deadline_utc=RUN_ATTEMPT_DEADLINE,
        )
        return run.run_id

    def publish_terminal(
        self,
        *,
        execution: ValidationRunBinding,
        outcome_code: str,
        approval_id: str,
        published_artifact_ids: tuple[str, ...],
        actor: AuditActor,
        initiated_by: AuditActor | None,
    ) -> ValidationRunTerminalResult:
        del approval_id, actor, initiated_by
        run = self._runs.get(execution.run_id)
        if (
            run is None
            or run.status != "running"
            or run.revision != execution.expected_run_revision
            or run.payload.params != execution.payload
            or run.payload.resolved_profiles != execution.resolved_profiles
            or len(published_artifact_ids) != 1
        ):
            raise IntegrityViolation("validation terminal differs from persisted fenced Run")
        attempt = self._runs.get_attempt(run.run_id, execution.attempt_no)
        lease = self._runs.get_current_lease(run.run_id)
        if (
            attempt is None
            or lease is None
            or lease.lease_id != execution.lease_id
            or attempt.fencing_token != execution.fencing_token
        ):
            raise IntegrityViolation("validation terminal lease/fencing identity changed")

        prepared_result = self._run_results.pop(run.run_id, None)
        if prepared_result is None:
            raise IntegrityViolation("validation terminal has no preverified run_result")
        result = prepared_result.stored
        validate_artifact_producer(
            result.artifact,
            ProducerValidationContext(
                run_manifest_projection=prepared_result.projection,
                expected_run_manifest_projection=prepared_result.projection,
            ),
        )
        self._bindings.bind_verified(
            result.binding.object_ref,
            result.binding.location,
            result.binding.expected_revision,
        )
        self._authority.put(result.artifact)
        success = RunEvent(
            run_id=run.run_id,
            seq=run.next_event_seq,
            event_type="run.succeeded",
            attempt_no=attempt.attempt_no,
            occurred_at=RUN_ENDED,
            data_schema_version="run-succeeded@1",
            data=RunSucceededDataV1(
                attempt_no=attempt.attempt_no,
                result_artifact_id=result.artifact.artifact_id,
            ),
            trace_id=attempt.trace_id,
        )
        terminal = self._runs.complete_attempt_success(
            fence=AttemptWriteFence(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                expected_run_revision=run.revision,
                lease_id=lease.lease_id,
                fencing_token=attempt.fencing_token,
            ),
            ended_at=RUN_ENDED,
            result_artifact_id=result.artifact.artifact_id,
            attempt_cassette_artifact_id=None,
            terminal_cassette_artifact_id=None,
            event=success,
        )
        if terminal.run.status != "succeeded":  # pragma: no cover - repository invariant
            raise IntegrityViolation("validation Run did not reach succeeded")
        return ValidationRunTerminalResult(
            outcome_code=outcome_code,
            failure_artifact_id=None,
        )


class _RunRepositoryView:
    """Route the fixed UoW slot to its real Run repository without name collisions."""

    def __init__(self, capability: Any) -> None:
        self._capability = capability

    def get(self, run_id: str) -> RunRecord | None:
        return self._capability.get_run(run_id)


class _TransitionRepositoryView:
    """Route the same fixed UoW slot to its real RefTransition repository."""

    def __init__(self, capability: Any) -> None:
        self._capability = capability

    def get(self, transition_id: str) -> object | None:
        return self._capability.get_transition(transition_id)

    def put(self, transition: object) -> object:
        return self._capability.put_transition(transition)


def _context(
    actor: AuditActor,
    key: str,
    *,
    run_id: str | None = None,
    initiated_by: AuditActor | None = None,
) -> ApprovalCommandContext:
    return ApprovalCommandContext(
        actor=actor,
        initiated_by=initiated_by,
        request_id=f"request:{key}",
        run_id=run_id,
        idempotency_scope=f"principal:{actor.principal_id}",
        idempotency_key=key,
        request_hash=hashlib.sha256(key.encode()).hexdigest(),
    )


def _profile_definition(
    *,
    profile: ProfileRefV1,
    profile_kind: str,
    run_kind: str,
) -> ExecutionProfileDefinitionV1:
    config: dict[str, object] = {}
    details = (
        ValidationProfileDetailsV1(subject_kinds=("patch",))
        if profile_kind == "validation"
        else GenericProfileDetailsV1()
    )
    return ExecutionProfileDefinitionV1(
        profile=profile,
        profile_kind=profile_kind,  # type: ignore[arg-type]
        compatible_run_kinds=(RunKindRef(kind=run_kind, version=1),),
        domain_scope=DomainScope(domain_ids=("economy",)),
        stochastic=False,
        input_schema_ids=(f"{profile_kind}-input@1",),
        output_schema_ids=("validation-evidence@1",),
        required_capabilities=("artifact.read",),
        display_name=profile.profile_id,
        handler_key=f"{profile.profile_id}@1",
        config_schema_id=f"{profile_kind}-config@1",
        config=config,
        config_hash=canonical_config_hash(config),
        details=details,
    )


def _profile_catalog() -> ExecutionProfileCatalogSnapshotV1:
    definitions = (
        _profile_definition(
            profile=PATCH_PROFILE,
            profile_kind="validation",
            run_kind="patch.validate",
        ),
        _profile_definition(
            profile=ROLLBACK_PROFILE,
            profile_kind="rollback",
            run_kind="rollback.validate",
        ),
        _profile_definition(
            profile=COMPATIBILITY_PROFILE,
            profile_kind="schema_compatibility",
            run_kind="rollback.validate",
        ),
    )
    lifecycle = tuple(
        ExecutionProfileLifecycleV1(
            profile=definition.profile,
            state="active",
            revision=1,
            changed_at=NOW,
        )
        for definition in definitions
    )
    payload = {
        "catalog_schema_version": "execution-profile-catalog@1",
        "catalog_version": 1,
        "definitions": definitions,
        "lifecycle": lifecycle,
    }
    return ExecutionProfileCatalogSnapshotV1(
        **payload,
        catalog_digest=execution_profile_catalog_digest(payload),
    )


def _seed_governance(
    engine: Engine,
    *,
    clock: FrozenUtcClock,
    catalog: ExecutionProfileCatalogSnapshotV1,
) -> None:
    registry = apply_testkit._registry()
    route = apply_testkit._route(registry)
    roles = apply_testkit._roles(registry)
    approval = apply_testkit._approval_policy()
    approval_registry = ApprovalPolicyRegistryV1(
        policies=(approval,),
        registry_digest=compute_approval_policy_registry_digest((approval,)),
    )
    with Session(engine) as session, session.begin():
        policies = SqlPolicySnapshotRepository(session, clock=clock)
        policies.put_domain_registry(registry)
        policies.put_domain_route_policy(route)
        policies.put_role_policy(roles)
        policies.put_approval_policy_registry(approval_registry)
        policies.put_execution_profile_catalog(catalog)

        identities = SqlIdentityRepository(session, clock=clock)
        identities.create(
            principal_id="human:maker",
            kind="human",
            display_name="Maker",
        )
        for principal_id in ("human:reviewer", "human:operator"):
            principal = identities.create(
                principal_id=principal_id,
                kind="human",
                display_name=principal_id,
            )
            identities.grant(
                assignment_id=f"assignment:{principal_id}:economy",
                principal_id=principal_id,
                role="numeric_designer",
                scope=DomainScope(domain_ids=("economy",)),
                granted_by=AuditActor(
                    principal_id="human:admin",
                    principal_kind="human",
                ),
                expected_principal_revision=principal.revision,
            )


def _approval_item(
    *,
    approval_id: str,
    series_id: str,
    subject_kind: str,
    subject: ArtifactV2,
    target_binding: PatchTargetBindingV1 | RollbackTargetBindingV1,
    maker: AuditActor,
) -> ApprovalItem:
    registry = apply_testkit._registry()
    route = apply_testkit._route(registry)
    roles = apply_testkit._roles(registry)
    approval = apply_testkit._approval_policy()
    scope = DomainScope(domain_ids=("economy",))
    requirements = build_approval_requirements(
        registry=registry,
        policy=route,
        subject_kind=subject_kind,  # type: ignore[arg-type]
        domain_scope=scope,
        assignee_principal_ids_by_rule={"route:economy": ("human:reviewer",)},
    )
    domain_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    return ApprovalItem(
        approval_id=approval_id,
        subject_series_id=series_id,
        subject_revision=1,
        subject_kind=subject_kind,  # type: ignore[arg-type]
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        status="draft",
        workflow_revision=1,
        proposer=maker,
        domain_scope=scope,
        domain_registry_ref=domain_ref,
        route_policy=DomainRoutePolicyRefV1(
            route_version=route.route_version,
            route_digest=route.route_digest,
            domain_registry_ref=route.domain_registry_ref,
        ),
        role_policy_version=roles.policy_version,
        role_policy_digest=roles.policy_digest,
        approval_policy=ApprovalPolicyRefV1(
            policy_version=approval.policy_version,
            policy_digest=approval.policy_digest,
        ),
        requirements=requirements,
        decisions=(),
        regression_evidence_artifact_ids=(),
        target_binding=target_binding,
        created_at=NOW,
    )


def _prepare_validation_completion(
    engine: Engine,
    objects: LocalObjectStore,
    *,
    approval_id: str,
    run_results: dict[str, _PreparedRunResult],
) -> PreparedValidationCompletion:
    with Session(engine) as session:
        approvals = SqlApprovalRepository(session)
        item = approvals.get(approval_id)
        if item is None or item.target_binding is None:
            raise AssertionError("validation ApprovalItem is unavailable")
        run = SqlRunRepository(session).get(item.active_validation_run_id or "")
        if run is None or run.current_attempt_no is None:
            raise AssertionError("validation Run is unavailable")
        run_repository = SqlRunRepository(session)
        attempt = run_repository.get_attempt(run.run_id, run.current_attempt_no)
        lease = run_repository.get_current_lease(run.run_id)
        if attempt is None or lease is None:
            raise AssertionError("validation Run fence is unavailable")

    policy_version = (
        "patch-validation-policy@1"
        if item.subject_kind == "patch"
        else "rollback-validation-policy@1"
    )
    evidence = EvidenceSet(
        subject_artifact_id=item.subject_artifact_id,
        subject_digest=item.subject_digest,
        policy_version=policy_version,
        validation_run_id=run.run_id,
        target_binding=item.target_binding,
        supporting_artifact_ids=(),
        finding_bindings=(),
        requirements=(),
        overall_status="passed",
    )
    evidence_stored = _prepare_artifact(
        objects,
        kind="validation_evidence",
        payload=_payload_bytes(evidence),
        version_tuple=VersionTuple(
            ir_snapshot_id=item.target_binding.target_snapshot_id,
            tool_version="local-validation@1",
        ),
        lineage=(item.subject_artifact_id, item.target_binding.target_artifact_id),
    )
    result_payload = _payload_bytes(
        {
            "run_id": run.run_id,
            "evidence_artifact_id": evidence_stored.artifact.artifact_id,
            "outcome": "passed",
        }
    )
    result_version = run.payload.version_tuple
    result_ref = objects.put_verified(result_payload)
    result_artifact = build_artifact_v2(
        kind="run_result",
        version_tuple=result_version,
        lineage=(evidence_stored.artifact.artifact_id,),
        payload_hash=result_ref.ref.sha256,
        object_ref=result_ref.ref,
        created_at=NOW,
    )
    projection = RunManifestVersionProjectionV1(
        manifest_scope="run",
        attempt_no=attempt.attempt_no,
        run_kind=run.kind,
        run_payload_hash=run.payload_hash,
        frozen_input_version_tuple=run.payload.version_tuple,
        terminal_version_tuple=result_version,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="local-validation-transition",
            policy_version=1,
            digest=HASH_A,
        ),
        parents=(
            RunManifestParentBindingV1(
                artifact_id=evidence_stored.artifact.artifact_id,
                role="output",
                publication="run_published",
                attempt_no=attempt.attempt_no,
            ),
        ),
    )
    result_stored = _StoredArtifact(
        artifact=result_artifact,
        payload=result_payload,
        binding=PreparedObjectBinding(
            object_ref=result_ref.ref,
            location=result_ref.location,
            expected_revision=None,
        ),
    )
    assert (
        validate_artifact_producer(
            result_artifact,
            ProducerValidationContext(
                run_manifest_projection=projection,
                expected_run_manifest_projection=projection,
            ),
        ).status
        == "valid"
    )
    run_results[run.run_id] = _PreparedRunResult(
        stored=result_stored,
        projection=projection,
    )
    payload = run.payload.params
    assert isinstance(payload, (PatchValidationPayloadV1, RollbackValidationPayloadV1))
    return PreparedValidationCompletion(
        execution=ValidationRunBinding(
            run_id=run.run_id,
            expected_run_revision=run.revision,
            attempt_no=attempt.attempt_no,
            lease_id=lease.lease_id,
            fencing_token=attempt.fencing_token,
            payload=payload,
            resolved_profiles=run.payload.resolved_profiles,
        ),
        outcome="passed",
        outcome_code=(
            "patch_validation_passed"
            if item.subject_kind == "patch"
            else "rollback_validation_passed"
        ),
        evidence_set=evidence,
        evidence_set_artifact=evidence_stored.artifact,
        object_bindings=(evidence_stored.binding,),
    )


def _approve(
    service: ApprovalCommandService,
    engine: Engine,
    *,
    item: ApprovalItem,
    reviewer: AuditActor,
    prefix: str,
) -> ApprovalItem:
    submitted = service.submit_for_approval(
        approval_id=item.approval_id,
        expected_workflow_revision=item.workflow_revision,
        context=_context(
            AuditActor(principal_id="human:maker", principal_kind="human"),
            f"{prefix}:submit",
        ),
    )
    with Session(engine) as session:
        principal = SqlIdentityRepository(
            session,
            clock=FrozenUtcClock(NOW_DT),
        ).project(reviewer.principal_id)
    assert principal is not None
    return service.decide(
        approval_id=submitted.approval_id,
        decision=ApprovalDecision(
            decision_id=f"decision:{prefix}",
            requirement_ids=tuple(
                requirement.requirement_id for requirement in submitted.requirements
            ),
            decision="approve",
            actor=reviewer,
            expected_workflow_revision=submitted.workflow_revision,
            reason_code="independent_review_passed",
            occurred_at=NOW,
        ),
        principal=principal,
        context=_context(reviewer, f"{prefix}:decide"),
    )


def _apply_request(item: ApprovalItem, *, key: str) -> ApprovedApplyRequest:
    binding = item.target_binding
    if binding is None:
        raise AssertionError("approved item has no target")
    return ApprovedApplyRequest(
        approval_id=item.approval_id,
        expected_workflow_revision=item.workflow_revision,
        subject_artifact_id=item.subject_artifact_id,
        subject_digest=item.subject_digest,
        target_artifact_id=binding.target_artifact_id,
        target_digest=binding.target_digest,
        ref_name=binding.ref_name,
        expected_ref=binding.expected_ref,
        context=_context(
            AuditActor(principal_id="human:operator", principal_kind="human"),
            key,
        ),
    )


def test_local_service_flow_persists_apply_and_approved_rollback_authority(
    tmp_path: Path,
) -> None:
    clock = FrozenUtcClock(NOW_DT)
    engine = get_engine(f"sqlite:///{tmp_path / 'local-service-flow.db'}")
    Base.metadata.create_all(engine)
    objects = LocalObjectStore(
        tmp_path / "objects",
        store_id="local",
        clock=clock,
        cursor_signing_key=OBJECT_CURSOR_KEY,
    )
    catalog = _profile_catalog()
    _seed_governance(engine, clock=clock, catalog=catalog)
    run_results: dict[str, _PreparedRunResult] = {}

    def capability_factory(session: Session) -> TransactionCapabilities:
        bindings = SqlObjectBindingRepository(
            session,
            object_store=objects,
            default_store_id="local",
        )
        authority = _SqlAuthorityCatalog(
            session,
            bindings=bindings,
            clock=clock,
        )
        readers = _TypedObjectReaders(
            artifacts=authority,
            bindings=bindings,
            objects=objects,
        )
        runtime = _SqlRuntimeGateways(
            session,
            catalog=catalog,
            authority=authority,
            bindings=bindings,
            readers=readers,
            run_results=run_results,
        )
        return TransactionCapabilities(
            refs=SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=clock),
                clock=clock,
            ),
            audit=SqlAuditSink(session),
            approvals=SqlApprovalRepository(session),
            lineage=authority,
            object_bindings=bindings,
            runs=runtime,
            cost=_SqlPrincipalProjection(session, clock=clock),
        )

    uow = SqliteUnitOfWork(engine, capability_factory)
    verifier = _DeterministicPublicationVerifier()

    def command_capabilities(transaction: Any) -> ApprovalCommandCapabilities:
        readers = _TypedObjectReaders(
            artifacts=transaction.lineage,
            bindings=transaction.object_bindings,
            objects=objects,
        )
        return ApprovalCommandCapabilities(
            approvals=transaction.approvals,
            policies=transaction.lineage,
            artifacts=transaction.lineage,
            object_bindings=transaction.object_bindings,
            idempotency=transaction.lineage,
            audit=AuditGate(sink=transaction.audit, clock=clock),
            runs=transaction.runs,
            subjects=readers,
            lineage=verifier,
            evidence=readers,
            refs=transaction.refs,
        )

    def validation_capabilities(transaction: Any) -> ValidationCompletionCapabilities:
        readers = _TypedObjectReaders(
            artifacts=transaction.lineage,
            bindings=transaction.object_bindings,
            objects=objects,
        )
        return ValidationCompletionCapabilities(
            approvals=transaction.approvals,
            artifacts=transaction.lineage,
            object_bindings=transaction.object_bindings,
            idempotency=transaction.lineage,
            profiles=transaction.lineage,
            verifier=verifier,
            runs=transaction.runs,
            audit=AuditGate(sink=transaction.audit, clock=clock),
            subjects=readers,
        )

    def apply_capabilities(transaction: Any) -> ApprovedApplyCapabilities:
        readers = _TypedObjectReaders(
            artifacts=transaction.lineage,
            bindings=transaction.object_bindings,
            objects=objects,
        )
        return ApprovedApplyCapabilities(
            approvals=transaction.approvals,
            policies=transaction.lineage,
            principals=transaction.cost,
            artifacts=transaction.lineage,
            refs=transaction.refs,
            transitions=_TransitionRepositoryView(transaction.runs),
            idempotency=transaction.lineage,
            audit=AuditGate(sink=transaction.audit, clock=clock),
            subjects=readers,
            evidence=readers,
            targets=readers,
            rollback_execution=ExactRollbackExecutionVerifier(
                runs=_RunRepositoryView(transaction.runs),
                profiles=transaction.lineage,
            ),
        )

    commands = ApprovalCommandService(
        unit_of_work=uow,
        bind_capabilities=command_capabilities,
        clock=clock,
        audit_chain_id=AUDIT_CHAIN_ID,
    )
    validation = ValidationCompletionService(
        unit_of_work=uow,
        bind_capabilities=validation_capabilities,
        audit_chain_id=AUDIT_CHAIN_ID,
    )
    applies = ApprovedApplyService(
        unit_of_work=uow,
        bind_capabilities=apply_capabilities,
        clock=clock,
        audit_chain_id=AUDIT_CHAIN_ID,
    )

    maker = AuditActor(principal_id="human:maker", principal_kind="human")
    reviewer = AuditActor(principal_id="human:reviewer", principal_kind="human")
    worker = AuditActor(principal_id="service:local-validator", principal_kind="service")
    base_snapshot = Snapshot.from_entities_relations(
        [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})],
        [],
    )
    desired_snapshot = Snapshot.from_entities_relations(
        [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 80})],
        [],
    )
    patch = PatchV2(
        revision=1,
        base_snapshot_id=base_snapshot.snapshot_id,
        target_snapshot_id=desired_snapshot.snapshot_id,
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=[
            TypedOp(
                op_id="set-reward-gold",
                op="set_entity_attr",
                target="q:1.reward_gold",
                old_value=120,
                new_value=80,
            )
        ],
        produced_by="human",
        producer_run_id=None,
        rationale="Keep quest rewards within the approved economy envelope.",
    )
    preview_snapshot = apply_patch(base_snapshot, patch)
    assert preview_snapshot.snapshot_id == desired_snapshot.snapshot_id

    base = _prepare_artifact(
        objects,
        kind="ir_snapshot",
        payload=_payload_bytes(base_snapshot.content_payload),
        version_tuple=VersionTuple(
            ir_snapshot_id=base_snapshot.snapshot_id,
            tool_version="local-flow@1",
        ),
    )
    with uow.begin() as transaction:
        transaction.object_bindings.bind_verified(
            base.binding.object_ref,
            base.binding.location,
            base.binding.expected_revision,
        )
        transaction.lineage.put(base.artifact)
        base_ref = transaction.refs.compare_and_set(REF_NAME, None, base.artifact.artifact_id)
        AuditGate(sink=transaction.audit, clock=clock).append(
            chain_id=AUDIT_CHAIN_ID,
            actor=maker,
            initiated_by=None,
            action="artifact.base_published",
            subject=AuditSubject(
                resource_kind="artifact",
                resource_id=base.artifact.artifact_id,
                artifact_id=base.artifact.artifact_id,
            ),
            correlation=AuditCorrelation(request_id="request:base-publish"),
        )
    assert base_ref == RefValue(artifact_id=base.artifact.artifact_id, revision=1)

    patch_stored = _prepare_artifact(
        objects,
        kind="patch",
        payload=_payload_bytes(patch),
        version_tuple=VersionTuple(
            ir_snapshot_id=base_snapshot.snapshot_id,
            tool_version="local-flow@1",
        ),
        lineage=(base.artifact.artifact_id,),
    )
    preview = _prepare_artifact(
        objects,
        kind="ir_snapshot",
        payload=_payload_bytes(preview_snapshot.content_payload),
        version_tuple=VersionTuple(
            ir_snapshot_id=preview_snapshot.snapshot_id,
            tool_version="local-flow@1",
        ),
        lineage=(base.artifact.artifact_id, patch_stored.artifact.artifact_id),
    )
    with Session(engine) as session:
        bindings = SqlObjectBindingRepository(
            session,
            object_store=objects,
            default_store_id="local",
        )
        authority = _SqlAuthorityCatalog(
            session,
            bindings=bindings,
            clock=clock,
        )
        readers = _TypedObjectReaders(
            artifacts=authority,
            bindings=bindings,
            objects=objects,
        )
        diff_service = SnapshotDiffService(
            _ObjectBackedSnapshotViews(
                readers=readers,
                artifacts=(base.artifact, preview.artifact),
            )
        )
        diff_summary = diff_service.diff_snapshots(
            base_snapshot.snapshot_id,
            preview_snapshot.snapshot_id,
        )
        diff_page = diff_service.page_entries(
            base_snapshot.snapshot_id,
            preview_snapshot.snapshot_id,
            after_path=None,
            limit=1,
        )
    assert diff_summary.entry_count == 1
    assert diff_page.diff == diff_summary
    assert diff_page.next_after_path is None
    assert len(diff_page.entries) == 1
    diff_entry = diff_page.entries[0]
    assert diff_entry.path == "/entities/q:1/attrs/reward_gold"
    assert diff_entry.before.presence == "present" and diff_entry.before.value == 120
    assert diff_entry.after.presence == "present" and diff_entry.after.value == 80

    patch_item = _approval_item(
        approval_id="approval:patch:1",
        series_id="series:patch:reward-gold",
        subject_kind="patch",
        subject=patch_stored.artifact,
        target_binding=PatchTargetBindingV1(
            target_artifact_id=preview.artifact.artifact_id,
            target_snapshot_id=preview_snapshot.snapshot_id,
            target_digest=preview.artifact.payload_hash,
            ref_name=REF_NAME,
            expected_ref=base_ref,
        ),
        maker=maker,
    )
    patch_draft = commands.publish_draft(
        prepared=PreparedDraft(
            subject_artifact=patch_stored.artifact,
            companion_artifacts=(preview.artifact,),
            object_bindings=(patch_stored.binding, preview.binding),
            approval_item=patch_item,
            expected_subject_head=None,
        ),
        context=_context(maker, "patch:draft"),
    )
    patch_run_id = "run:patch-validation:1"
    patch_started = commands.start_validation(
        prepared=PreparedValidationStart(
            run_id=patch_run_id,
            approval_id=patch_item.approval_id,
            subject_artifact_id=patch_item.subject_artifact_id,
            subject_digest=patch_item.subject_digest,
            expected_workflow_revision=patch_draft.approval_item.workflow_revision,
        ),
        context=_context(maker, "patch:start-validation"),
    )
    assert patch_started.approval_item.status == "validating"
    patch_completion = validation.complete(
        prepared=_prepare_validation_completion(
            engine,
            objects,
            approval_id=patch_item.approval_id,
            run_results=run_results,
        ),
        context=_context(
            worker,
            "patch:complete-validation",
            run_id=patch_run_id,
            initiated_by=maker,
        ),
    )
    assert patch_completion.approval_item.status == "validated"
    patch_approved = _approve(
        commands,
        engine,
        item=patch_completion.approval_item,
        reviewer=reviewer,
        prefix="patch",
    )
    assert patch_approved.proposer != reviewer
    patch_applied = applies.apply(_apply_request(patch_approved, key="patch:apply"))
    assert patch_applied.approval_item.status == "applied"
    assert patch_applied.ref_value == RefValue(
        artifact_id=preview.artifact.artifact_id,
        revision=2,
    )
    assert patch_applied.ref_transition is None

    with Session(engine) as session:
        profile_repository = SqlPolicySnapshotRepository(session, clock=clock)
        rollback_binding = profile_repository.resolve_execution_profile(
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.catalog_digest,
            field_path="/params/rollback_profile",
            profile=ROLLBACK_PROFILE,
            expected_profile_kind="rollback",
        )
    rollback_request = RollbackRequestV1(
        ref_name=REF_NAME,
        expected_current_ref=patch_applied.ref_value,
        target_artifact_id=base.artifact.artifact_id,
        target_history_revision=1,
        rollback_profile_binding=rollback_binding,
        reason="Restore the independently approved historical reward snapshot.",
        reverses_approval_id=patch_item.approval_id,
    )
    rollback_stored = _prepare_artifact(
        objects,
        kind="rollback_request",
        payload=_payload_bytes(rollback_request),
        version_tuple=VersionTuple(
            ir_snapshot_id=base_snapshot.snapshot_id,
            tool_version="local-flow@1",
        ),
        lineage=(base.artifact.artifact_id, preview.artifact.artifact_id),
    )
    rollback_item = _approval_item(
        approval_id="approval:rollback:1",
        series_id="series:rollback:reward-gold",
        subject_kind="rollback_request",
        subject=rollback_stored.artifact,
        target_binding=RollbackTargetBindingV1(
            target_artifact_kind="ir_snapshot",
            target_artifact_id=base.artifact.artifact_id,
            target_snapshot_id=base_snapshot.snapshot_id,
            target_digest=base.artifact.payload_hash,
            ref_name=REF_NAME,
            expected_ref=patch_applied.ref_value,
            rollback_profile_binding=rollback_binding,
        ),
        maker=maker,
    )
    rollback_draft = commands.publish_draft(
        prepared=PreparedDraft(
            subject_artifact=rollback_stored.artifact,
            companion_artifacts=(),
            object_bindings=(rollback_stored.binding,),
            approval_item=rollback_item,
            expected_subject_head=None,
        ),
        context=_context(maker, "rollback:draft"),
    )
    rollback_run_id = "run:rollback-validation:1"
    rollback_started = commands.start_validation(
        prepared=PreparedValidationStart(
            run_id=rollback_run_id,
            approval_id=rollback_item.approval_id,
            subject_artifact_id=rollback_item.subject_artifact_id,
            subject_digest=rollback_item.subject_digest,
            expected_workflow_revision=rollback_draft.approval_item.workflow_revision,
        ),
        context=_context(maker, "rollback:start-validation"),
    )
    assert rollback_started.approval_item.status == "validating"
    rollback_completion = validation.complete(
        prepared=_prepare_validation_completion(
            engine,
            objects,
            approval_id=rollback_item.approval_id,
            run_results=run_results,
        ),
        context=_context(
            worker,
            "rollback:complete-validation",
            run_id=rollback_run_id,
            initiated_by=maker,
        ),
    )
    rollback_approved = _approve(
        commands,
        engine,
        item=rollback_completion.approval_item,
        reviewer=reviewer,
        prefix="rollback",
    )

    with Session(engine) as session:
        artifacts_before_rollback_apply = tuple(
            session.execute(
                select(ArtifactRow.artifact_id, ArtifactRow.lineage).order_by(
                    ArtifactRow.artifact_id
                )
            ).all()
        )
    rollback_applied = applies.apply(_apply_request(rollback_approved, key="rollback:apply"))
    assert rollback_applied.approval_item.status == "applied"
    assert rollback_applied.reversed_approval_item is not None
    assert rollback_applied.reversed_approval_item.status == "rolled_back"
    assert rollback_applied.ref_value == RefValue(
        artifact_id=base.artifact.artifact_id,
        revision=3,
    )
    transition = rollback_applied.ref_transition
    assert transition is not None
    assert transition.from_ref == patch_applied.ref_value
    assert transition.to_ref == rollback_applied.ref_value

    with Session(engine) as session:
        refs = SqlRefStore(
            session,
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=clock),
            clock=clock,
        )
        assert tuple(refs.get_history_entry(REF_NAME, revision) for revision in range(1, 4)) == (
            base_ref,
            patch_applied.ref_value,
            rollback_applied.ref_value,
        )
        assert tuple(session.scalars(select(RefTransitionRow.transition_id))) == (
            transition.transition_id,
        )
        assert SqlRefTransitionRepository(session).get(transition.transition_id) == transition
        approvals = SqlApprovalRepository(session)
        assert approvals.get(patch_item.approval_id) == (rollback_applied.reversed_approval_item)
        assert approvals.get(rollback_item.approval_id) == rollback_applied.approval_item
        runs = SqlRunRepository(session)
        for run_id in (patch_run_id, rollback_run_id):
            retained_run = runs.get(run_id)
            assert retained_run is not None and retained_run.status == "succeeded"
            assert retained_run.result_artifact_id is not None
            result_row = session.get(ArtifactRow, retained_run.result_artifact_id)
            assert result_row is not None and result_row.kind == "run_result"
        artifacts_after_rollback_apply = tuple(
            session.execute(
                select(ArtifactRow.artifact_id, ArtifactRow.lineage).order_by(
                    ArtifactRow.artifact_id
                )
            ).all()
        )
        audit = SqlAuditSink(session)
        assert audit.verify_chain(AUDIT_CHAIN_ID) is True
        audit_rows = session.scalars(select(AuditRow).order_by(AuditRow.chain_seq)).all()
        assert audit_rows and all(row.audit_schema_version == "audit@2" for row in audit_rows)
        assert tuple(row.action for row in audit_rows) == (
            "artifact.base_published",
            "approval.draft_published",
            "approval.validation_started",
            "approval.validation_completed",
            "approval.submitted",
            "approval.approved",
            "approval.applied",
            "approval.draft_published",
            "approval.validation_started",
            "approval.validation_completed",
            "approval.submitted",
            "approval.approved",
            "approval.rollback_applied",
        )
    assert artifacts_after_rollback_apply == artifacts_before_rollback_apply

    lineage = SqlArtifactStore(
        sessionmaker(bind=engine),
        object_store=objects,
        default_store_id="local",
    )
    preview_ancestors = set(lineage.ancestors(preview.artifact.artifact_id))
    assert preview_ancestors == {
        base.artifact.artifact_id,
        patch_stored.artifact.artifact_id,
    }
    assert rollback_stored.artifact.artifact_id not in preview_ancestors

    orphan = objects.put_verified(b'{"orphan":"eligible-after-safe-window"}')
    later_clock = FrozenUtcClock(datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc))
    later_objects = LocalObjectStore(
        tmp_path / "objects",
        store_id="local",
        clock=later_clock,
        cursor_signing_key=OBJECT_CURSOR_KEY,
    )

    def gc_capabilities(session: Session) -> TransactionCapabilities:
        unused = object()
        return TransactionCapabilities(
            refs=unused,
            audit=unused,
            approvals=unused,
            lineage=unused,
            object_bindings=SqlObjectBindingRepository(
                session,
                object_store=later_objects,
                default_store_id="local",
            ),
            runs=unused,
            cost=unused,
        )

    gc = ObjectGcService(
        objects=later_objects,
        unit_of_work=SqliteUnitOfWork(engine, gc_capabilities),
        recovery_pins=NoRecoveryPins(),
        clock=later_clock,
        minimum_safe_age=timedelta(hours=1),
    )
    candidates = gc.plan(None, safe_before="2026-07-15T00:00:00Z").items
    assert tuple(candidate.object_ref for candidate in candidates) == (orphan.ref,)
    assert gc.collect(candidates[0]) == "deleted"
    with pytest.raises(FileNotFoundError):
        later_objects.stat(orphan.location)

    with Session(engine) as session:
        artifact_refs = tuple(
            SqlArtifactRepository(
                session,
                binding_repository=SqlObjectBindingRepository(
                    session,
                    object_store=later_objects,
                    default_store_id="local",
                ),
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=later_clock),
                clock=later_clock,
            ).get(artifact_id)
            for artifact_id in session.scalars(
                select(ArtifactRow.artifact_id).order_by(ArtifactRow.artifact_id)
            )
        )
    assert all(isinstance(artifact, ArtifactV2) for artifact in artifact_refs)
    for artifact in artifact_refs:
        assert isinstance(artifact, ArtifactV2)
        with Session(engine) as session:
            resolved = SqlObjectBindingRepository(
                session,
                object_store=later_objects,
                default_store_id="local",
            ).resolve(artifact.object_ref)
        assert later_objects.stat(resolved.location).ref == artifact.object_ref

    assert later_objects.stat(base.binding.location).ref == base.artifact.object_ref
    assert later_objects.stat(preview.binding.location).ref == preview.artifact.object_ref
    engine.dispose()
