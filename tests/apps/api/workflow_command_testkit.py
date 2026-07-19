"""Shared real-SQLite harness for the M4c synchronous workflow-command tests.

Reuses the M4a local-service integration scaffolding (real ``SqliteUnitOfWork`` +
``LocalObjectStore`` + governance seed + validation-completion machinery) and wires
the Task-7 :class:`WorkflowCommandService`/adapter behind a real ``create_app`` +
``TestClient``. No network; every authority is the real SQLite/object-store stack.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import ApiDependencies, require_actor
from gameforge.apps.api.workflow_command_port import WorkflowCommandAdapter
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainScope,
    Principal,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import RefValue
from gameforge.platform.approvals.apply import (
    ApprovedApplyCapabilities,
    ApprovedApplyService,
    ExactRollbackExecutionVerifier,
)
from gameforge.platform.approvals.commands import (
    ApprovalCommandCapabilities,
    ApprovalCommandService,
)
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.diff.rebase import (
    RebaseWorkflowCapabilities,
    RebaseWorkflowService,
)
from gameforge.platform.read_models.workflows import CurrentApprovalProgressProjector
from gameforge.platform.workflow.readers import (
    WorkflowDraftLineageVerifier,
    WorkflowTypedReaders,
)
from gameforge.platform.workflow.service import (
    WorkflowCommandService,
    WorkflowGovernance,
    WorkflowReadPort,
)
from gameforge.platform.workflow.spec import (
    SpecUploadCapabilities,
    SpecUploadService,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.conflicts import SqlConflictSetRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from tests.platform.m4 import apply_testkit
from tests.platform.m4.test_local_service_flow_integration import (
    CURSOR_KEY,
    NOW_DT,
    OBJECT_CURSOR_KEY,
    REF_NAME,
    _DeterministicPublicationVerifier,
    _prepare_validation_completion,
    _profile_catalog,
    _RunRepositoryView,
    _seed_governance,
    _SqlAuthorityCatalog,
    _SqlPrincipalProjection,
    _SqlRuntimeGateways,
    _TransitionRepositoryView,
)

AUDIT_CHAIN_ID = "platform-authority"


class AdvancingUtcClock:
    """UTC clock that advances by a fixed step on every read.

    Deterministic yet non-frozen: successive ``now_utc`` calls return strictly
    increasing timestamps, so an idempotent replay whose re-assembly stamps a fresh
    ``created_at`` cannot silently match the committed value. This is the clock a
    real deployment (``SystemUtcClock``) approximates; the frozen clock masks the
    replay-under-real-clock defect.
    """

    def __init__(self, start: datetime, *, step: timedelta = timedelta(seconds=1)) -> None:
        if start.tzinfo is None or start.utcoffset() != timedelta(0):
            raise ValueError("AdvancingUtcClock start must be timezone-aware UTC")
        self._now = start
        self._step = step

    def now_utc(self) -> datetime:
        current = self._now
        self._now = self._now + self._step
        return current


class _FixedScopeResolver:
    def __init__(self, scope: Any) -> None:
        self._scope = scope

    def resolve_patch_scope(self, *, base_artifact: Any, patch: Any) -> Any:
        return self._scope

    def resolve_rollback_scope(self, *, target_artifact: Any, request: Any) -> Any:
        return self._scope


class _CannedAdmission:
    """Task-8 stand-in proving the port routes + forwards server metadata."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def admit(self, *, operation, resource_id, request, actor, server):
        from gameforge.contracts.api import RunAcceptedV1

        run_id = f"run:{operation}:{resource_id}"
        self.calls.append(
            {
                "operation": operation,
                "resource_id": resource_id,
                "request": request,
                "actor": actor,
                "idempotency_key": server.idempotency_key,
                "request_hash": server.request_hash,
            }
        )
        return RunAcceptedV1(
            run_id=run_id,
            status_url=f"/api/v1/runs/{run_id}",
            events_url=f"/api/v1/runs/{run_id}/events",
        )


@dataclass
class WorkflowHarness:
    app: FastAPI
    service: WorkflowCommandService
    engine: Engine
    objects: LocalObjectStore
    clock: Any
    uow: Any
    commands: ApprovalCommandService
    validation: Any
    applies: ApprovedApplyService
    governance: WorkflowGovernance
    admission: _CannedAdmission
    run_results: dict[str, Any]
    _actor_holder: dict[str, ActorContext]
    base_artifact_id: str = ""
    base_ref: RefValue | None = None
    base_snapshot: Any = None

    def use_actor(self, actor: ActorContext) -> None:
        self._actor_holder["actor"] = actor

    def approval_repository(self, session: Session) -> SqlApprovalRepository:
        return SqlApprovalRepository(session)

    def load_item(self, approval_id: str) -> Any:
        with Session(self.engine) as session:
            return SqlApprovalRepository(session).get(approval_id)


def _principal(engine: Engine, clock: FrozenUtcClock, principal_id: str) -> Principal:
    with Session(engine) as session:
        principal = SqlIdentityRepository(session, clock=clock).project(principal_id)
    assert principal is not None, principal_id
    return principal


def _actor_context(principal: Principal) -> ActorContext:
    return ActorContext(
        principal=principal,
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id=f"credential:{principal.id}",
        ),
        session_id=f"session:{principal.id}",
        request_id=f"request:{principal.id}",
    )


def actor_context(harness: WorkflowHarness, principal_id: str) -> ActorContext:
    return _actor_context(_principal(harness.engine, harness.clock, principal_id))


def build_harness(
    tmp_path: Path,
    *,
    admission: bool = True,
    clock: Any = None,
    execution_profile_catalog: Any = None,
    config_exporter: Any = None,
) -> WorkflowHarness:
    clock = clock if clock is not None else FrozenUtcClock(NOW_DT)
    engine = get_engine(f"sqlite:///{tmp_path / 'workflow-commands.db'}")
    Base.metadata.create_all(engine)
    objects = LocalObjectStore(
        tmp_path / "objects",
        store_id="local",
        clock=clock,
        cursor_signing_key=OBJECT_CURSOR_KEY,
    )
    catalog = execution_profile_catalog or _profile_catalog()
    registry = apply_testkit._registry()
    route = apply_testkit._route(registry)
    roles = apply_testkit._roles(registry)
    approval_policy = apply_testkit._approval_policy()
    _seed_governance(engine, clock=clock, catalog=catalog)
    run_results: dict[str, Any] = {}

    def capability_factory(session: Session) -> TransactionCapabilities:
        bindings = SqlObjectBindingRepository(
            session, object_store=objects, default_store_id="local"
        )
        authority = _SqlAuthorityCatalog(session, bindings=bindings, clock=clock)
        readers = WorkflowTypedReaders(artifacts=authority, bindings=bindings, objects=objects)
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
            conflicts=SqlConflictSetRepository(
                session,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=clock),
                clock=clock,
            ),
        )

    uow = SqliteUnitOfWork(engine, capability_factory)
    draft_verifier = WorkflowDraftLineageVerifier()
    completion_verifier = _DeterministicPublicationVerifier()

    def _readers(transaction: Any) -> WorkflowTypedReaders:
        return WorkflowTypedReaders(
            artifacts=transaction.lineage,
            bindings=transaction.object_bindings,
            objects=objects,
        )

    def command_capabilities(transaction: Any) -> ApprovalCommandCapabilities:
        readers = _readers(transaction)
        return ApprovalCommandCapabilities(
            approvals=transaction.approvals,
            policies=transaction.lineage,
            artifacts=transaction.lineage,
            object_bindings=transaction.object_bindings,
            idempotency=transaction.lineage,
            audit=AuditGate(sink=transaction.audit, clock=clock),
            runs=transaction.runs,
            subjects=readers,
            lineage=draft_verifier,
            evidence=readers,
            refs=transaction.refs,
            principals=transaction.cost,
        )

    def validation_capabilities(transaction: Any):
        from gameforge.platform.approvals.validation import ValidationCompletionCapabilities

        readers = _readers(transaction)
        return ValidationCompletionCapabilities(
            approvals=transaction.approvals,
            artifacts=transaction.lineage,
            object_bindings=transaction.object_bindings,
            idempotency=transaction.lineage,
            profiles=transaction.lineage,
            verifier=completion_verifier,
            runs=transaction.runs,
            audit=AuditGate(sink=transaction.audit, clock=clock),
            subjects=readers,
        )

    def apply_capabilities(transaction: Any) -> ApprovedApplyCapabilities:
        readers = _readers(transaction)
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

    def spec_capabilities(transaction: Any) -> SpecUploadCapabilities:
        return SpecUploadCapabilities(
            refs=transaction.refs,
            artifacts=transaction.lineage,
            object_bindings=transaction.object_bindings,
            audit=AuditGate(sink=transaction.audit, clock=clock),
            idempotency=transaction.lineage,
            policies=transaction.lineage,
            principals=transaction.cost,
        )

    commands = ApprovalCommandService(
        unit_of_work=uow,
        bind_capabilities=command_capabilities,
        clock=clock,
        audit_chain_id=AUDIT_CHAIN_ID,
    )
    from gameforge.platform.approvals.validation import ValidationCompletionService

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
    spec_service = SpecUploadService(
        unit_of_work=uow,
        bind_capabilities=spec_capabilities,
        clock=clock,
        audit_chain_id=AUDIT_CHAIN_ID,
        role_policy_version=roles.policy_version,
        role_policy_digest=roles.policy_digest,
    )

    def _read_readers(session: Session) -> WorkflowTypedReaders:
        bindings = SqlObjectBindingRepository(
            session, object_store=objects, default_store_id="local"
        )
        artifacts = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=clock),
            clock=clock,
        )
        return WorkflowTypedReaders(artifacts=artifacts, bindings=bindings, objects=objects)

    class _RebasePayloads:
        def load_patch(self, artifact):
            with Session(engine) as session:
                return _read_readers(session).load_patch(artifact)

        def load_snapshot(self, artifact):
            with Session(engine) as session:
                return _read_readers(session).load_snapshot(artifact)

    def rebase_capabilities(transaction: Any) -> RebaseWorkflowCapabilities:
        return RebaseWorkflowCapabilities(
            approval=command_capabilities(transaction),
            conflicts=transaction.conflicts,
        )

    rebase_service = RebaseWorkflowService(
        unit_of_work=uow,
        bind_capabilities=rebase_capabilities,
        approval_commands=commands,
        payloads=_RebasePayloads(),
        clock=clock,
        audit_chain_id=AUDIT_CHAIN_ID,
    )

    governance = WorkflowGovernance(
        registry=registry,
        route=route,
        roles=roles,
        approval=approval_policy,
    )

    @contextmanager
    def read_scope() -> Iterator[WorkflowReadPort]:
        with Session(engine) as session:
            bindings = SqlObjectBindingRepository(
                session, object_store=objects, default_store_id="local"
            )
            artifacts = SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=clock),
                clock=clock,
            )
            refs = SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=clock),
                clock=clock,
            )
            approvals = SqlApprovalRepository(session)
            policies = SqlPolicySnapshotRepository(session, clock=clock)
            identities = SqlIdentityRepository(session, clock=clock)
            readers = WorkflowTypedReaders(artifacts=artifacts, bindings=bindings, objects=objects)
            yield WorkflowReadPort(
                artifacts=artifacts,
                refs=refs,
                approvals=approvals,
                policies=policies,
                readers=readers,
                progress_projector=CurrentApprovalProgressProjector(
                    policy_repository=policies,
                    role_policy_version=roles.policy_version,
                    role_policy_digest=roles.policy_digest,
                    principal_resolver=identities.project,
                ),
            )

    admission_port = _CannedAdmission()
    service = WorkflowCommandService(
        clock=clock,
        object_store=objects,
        read_scope=read_scope,
        approval_commands=commands,
        apply_service=applies,
        rebase_service=rebase_service,
        spec_service=spec_service,
        governance=governance,
        scope_resolver=_FixedScopeResolver(DomainScope(domain_ids=("economy",))),
        admission=admission_port if admission else None,
        execution_profile_catalog=catalog,
        config_exporter=config_exporter,
    )
    adapter = WorkflowCommandAdapter(service)

    actor_holder: dict[str, ActorContext] = {}
    app = create_app(
        ApiDependencies(
            workflow_commands=adapter,
            request_id_factory=lambda: "request:test",
        )
    )
    app.dependency_overrides[require_actor] = lambda: actor_holder["actor"]

    harness = WorkflowHarness(
        app=app,
        service=service,
        engine=engine,
        objects=objects,
        clock=clock,
        uow=uow,
        commands=commands,
        validation=validation,
        applies=applies,
        governance=governance,
        admission=admission_port,
        run_results=run_results,
        _actor_holder=actor_holder,
    )
    return harness


def maker_actor(harness: WorkflowHarness) -> ActorContext:
    return actor_context(harness, "human:maker")


def reviewer_actor(harness: WorkflowHarness) -> ActorContext:
    return actor_context(harness, "human:reviewer")


def operator_actor(harness: WorkflowHarness) -> ActorContext:
    return actor_context(harness, "human:operator")


def maker_audit() -> AuditActor:
    return AuditActor(principal_id="human:maker", principal_kind="human")


def resource_etag(*, resource_kind: str, resource_id: str, revision: int) -> str:
    from gameforge.contracts.api import compute_resource_etag

    return compute_resource_etag(
        resource_kind=resource_kind,
        resource_id=resource_id,
        revision=revision,
    )


def headers(*, key: str, if_match: str = '"etag:1"') -> dict[str, str]:
    return {"Idempotency-Key": key, "If-Match": if_match}


def publish_base(
    harness: WorkflowHarness,
    *,
    entities: list[Any],
    ref_name: str = REF_NAME,
    doc_version: str | None = None,
) -> Any:
    """Publish an ir_snapshot Artifact + set ``ref_name`` to it; return the Snapshot."""

    from gameforge.contracts.lineage import (
        AuditCorrelation,
        AuditSubject,
        VersionTuple,
    )
    from gameforge.spine.ir.snapshot import Snapshot
    from tests.platform.m4.test_local_service_flow_integration import (
        _payload_bytes,
        _prepare_artifact,
    )

    snapshot = Snapshot.from_entities_relations(entities, [])
    base = _prepare_artifact(
        harness.objects,
        kind="ir_snapshot",
        payload=_payload_bytes(snapshot.content_payload),
        version_tuple=VersionTuple(
            doc_version=doc_version,
            ir_snapshot_id=snapshot.snapshot_id,
            tool_version="local-flow@1",
        ),
    )
    with harness.uow.begin() as transaction:
        transaction.object_bindings.bind_verified(
            base.binding.object_ref,
            base.binding.location,
            base.binding.expected_revision,
        )
        transaction.lineage.put(base.artifact)
        ref = transaction.refs.compare_and_set(ref_name, None, base.artifact.artifact_id)
        AuditGate(sink=transaction.audit, clock=harness.clock).append(
            chain_id=AUDIT_CHAIN_ID,
            actor=maker_audit(),
            initiated_by=None,
            action="artifact.base_published",
            subject=AuditSubject(
                resource_kind="artifact",
                resource_id=base.artifact.artifact_id,
                artifact_id=base.artifact.artifact_id,
            ),
            correlation=AuditCorrelation(request_id="request:base-publish"),
        )
    harness.base_artifact_id = base.artifact.artifact_id
    harness.base_ref = ref
    harness.base_snapshot = snapshot
    return snapshot


def publish_constraint_snapshot(
    harness: WorkflowHarness,
    *,
    constraints: list[Any],
    dsl_grammar_version: str = "dsl@1",
) -> Any:
    """Publish one immutable constraint_snapshot parent for Patch export tests."""

    from gameforge.contracts.canonical import canonical_sha256
    from gameforge.contracts.lineage import AuditCorrelation, AuditSubject, VersionTuple
    from tests.platform.m4.test_local_service_flow_integration import (
        _payload_bytes,
        _prepare_artifact,
    )

    payload = {
        "dsl_grammar_version": dsl_grammar_version,
        "constraints": [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in constraints
        ],
    }
    prepared = _prepare_artifact(
        harness.objects,
        kind="constraint_snapshot",
        payload=_payload_bytes(payload),
        version_tuple=VersionTuple(
            constraint_snapshot_id=canonical_sha256(payload),
            tool_version="constraint-test@1",
        ),
    )
    with harness.uow.begin() as transaction:
        transaction.object_bindings.bind_verified(
            prepared.binding.object_ref,
            prepared.binding.location,
            prepared.binding.expected_revision,
        )
        transaction.lineage.put(prepared.artifact)
        AuditGate(sink=transaction.audit, clock=harness.clock).append(
            chain_id=AUDIT_CHAIN_ID,
            actor=maker_audit(),
            initiated_by=None,
            action="artifact.constraint_test_published",
            subject=AuditSubject(
                resource_kind="artifact",
                resource_id=prepared.artifact.artifact_id,
                artifact_id=prepared.artifact.artifact_id,
            ),
            correlation=AuditCorrelation(request_id="request:constraint-test-publish"),
        )
    return prepared.artifact


def drive_to_validated(harness: WorkflowHarness, approval_id: str, *, run_id: str) -> Any:
    """Run start_validation + validation completion (Task 8/13 scaffolding)."""

    from tests.platform.m4.test_local_service_flow_integration import _context
    from gameforge.platform.approvals.commands import PreparedValidationStart

    item = harness.load_item(approval_id)
    harness.commands.start_validation(
        prepared=PreparedValidationStart(
            run_id=run_id,
            approval_id=item.approval_id,
            subject_artifact_id=item.subject_artifact_id,
            subject_digest=item.subject_digest,
            expected_workflow_revision=item.workflow_revision,
        ),
        context=_context(maker_audit(), f"{approval_id}:start-validation"),
    )
    worker = AuditActor(principal_id="service:local-validator", principal_kind="service")
    completion = harness.validation.complete(
        prepared=_prepare_validation_completion(
            harness.engine,
            harness.objects,
            approval_id=approval_id,
            run_results=harness.run_results,
        ),
        context=_context(
            worker,
            f"{approval_id}:complete-validation",
            run_id=run_id,
            initiated_by=maker_audit(),
        ),
    )
    return completion.approval_item


def _patch_body(
    harness, *, ref_name, new_value=None, old_value=120, key="p", ops=None, rationale=None
):
    if ops is None:
        ops = [
            {
                "op_id": "set-reward-gold",
                "op": "set_entity_attr",
                "target": "q:1.reward_gold",
                "old_value": old_value,
                "new_value": new_value,
            }
        ]
        rationale = rationale or f"Set reward to {new_value}."
    else:
        rationale = rationale or "Apply a targeted content change."
    return {
        "request_schema_version": "human-patch-draft-request@1",
        "base_snapshot_artifact_id": harness.base_artifact_id,
        "constraint_snapshot_artifact_id": None,
        "ref_name": ref_name,
        "expected_ref": harness.base_ref.model_dump(mode="json"),
        "expected_to_fix": [],
        "preconditions": [],
        "side_effect_risk": "low",
        "ops": ops,
        "rationale": rationale,
        "candidate_export_profiles": [],
    }


def apply_full_patch(
    harness, client, *, ref_name, new_value=None, key: str, ops=None, rationale=None
):
    """Draft→validate→submit→approve→apply a single-op patch; return the new ref value."""

    harness.use_actor(maker_actor(harness))
    draft = client.post(
        "/api/v1/patches",
        json=_patch_body(
            harness, ref_name=ref_name, new_value=new_value, ops=ops, rationale=rationale
        ),
        headers=headers(key=f"{key}:draft"),
    )
    assert draft.status_code == 201, draft.text
    artifact_id = draft.json()["artifact"]["artifact_id"]
    approval_id = f"approval:patch:{artifact_id}"
    drive_to_validated(harness, approval_id, run_id=f"run:patch-validation:{key}")
    item = harness.load_item(approval_id)
    client.post(
        f"/api/v1/patches/{artifact_id}:submit-for-approval",
        json={
            "request_schema_version": "submit-for-approval-request@1",
            "approval_id": approval_id,
            "expected_workflow_revision": item.workflow_revision,
        },
        headers=headers(
            key=f"{key}:submit",
            if_match=resource_etag(
                resource_kind="patch",
                resource_id=artifact_id,
                revision=item.workflow_revision,
            ),
        ),
    )
    pending = harness.load_item(approval_id)
    harness.use_actor(reviewer_actor(harness))
    client.post(
        f"/api/v1/approvals/{approval_id}:approve",
        json={
            "request_schema_version": "approval-decision-request@1",
            "decision": "approve",
            "requirement_ids": [r.requirement_id for r in pending.requirements],
            "expected_workflow_revision": pending.workflow_revision,
            "reason_code": "independent_review_passed",
        },
        headers=headers(
            key=f"{key}:approve",
            if_match=resource_etag(
                resource_kind="approval",
                resource_id=approval_id,
                revision=pending.workflow_revision,
            ),
        ),
    )
    approved = harness.load_item(approval_id)
    binding = approved.target_binding
    harness.use_actor(operator_actor(harness))
    apply = client.post(
        f"/api/v1/patches/{approved.subject_artifact_id}:apply",
        json={
            "request_schema_version": "workflow-apply-request@1",
            "approval_id": approval_id,
            "expected_workflow_revision": approved.workflow_revision,
            "subject_digest": approved.subject_digest,
            "target_artifact_id": binding.target_artifact_id,
            "target_digest": binding.target_digest,
            "ref_name": binding.ref_name,
            "expected_ref": binding.expected_ref.model_dump(mode="json"),
        },
        headers=headers(
            key=f"{key}:apply",
            if_match=resource_etag(
                resource_kind="patch",
                resource_id=approved.subject_artifact_id,
                revision=approved.workflow_revision,
            ),
        ),
    )
    assert apply.status_code == 200, apply.text
    return approval_id, apply.json()["ref_value"]
