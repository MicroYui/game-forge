from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRoutePolicyRefV1,
    DomainRouteRule,
    DomainScope,
    Permission,
    RolePolicy,
    compute_domain_registry_digest,
    compute_domain_route_policy_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    ObjectLocation,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalPolicyRegistryV1,
    ApprovalPolicyV1,
    PatchTargetBindingV1,
    SubjectHead,
    compute_approval_policy_digest,
    compute_approval_policy_registry_digest,
)
from gameforge.platform.approvals import build_approval_requirements
from gameforge.platform.approvals.commands import (
    ApprovalCommandCapabilities,
    ApprovalCommandContext,
    ApprovalCommandService,
    DraftSubjectFacts,
    PreparedDraft,
    PreparedObjectBinding,
    PreparedValidationStart,
)
from gameforge.platform.audit.gate import AuditGate
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import (
    ApprovalDecisionRow,
    ApprovalItemRow,
    ArtifactRow,
    AuditHeadRow,
    AuditRow,
    Base,
    IdempotencyRecordRow,
    ObjectBindingRow,
    SubjectHeadRow,
)
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


NOW_DT = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-14T12:00:00Z"
AUDIT_CHAIN_ID = "platform-authority"


def _domain_registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(
            domain_id="economy",
            display_name="Economy",
            status="active",
        ),
    )
    return DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )


def _domain_ref(registry: DomainRegistryV1) -> DomainRegistryRefV1:
    return DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _route_policy(registry: DomainRegistryV1) -> DomainRoutePolicy:
    rules = (
        DomainRouteRule(
            rule_id="route:economy",
            domain_selector=DomainScope(domain_ids=("economy",)),
            subject_kinds=("patch", "constraint_proposal", "rollback_request"),
            route_role="numeric_designer",
            required_action="approval.decide",
            resource_kind="approval",
            min_approvals=1,
        ),
    )
    registry_ref = _domain_ref(registry)
    return DomainRoutePolicy(
        route_version="routes@1",
        domain_registry_ref=registry_ref,
        rules=rules,
        effective_from=NOW,
        route_digest=compute_domain_route_policy_digest(
            "routes@1",
            registry_ref,
            rules,
            NOW,
        ),
    )


def _role_policy(registry: DomainRegistryV1) -> RolePolicy:
    registry_ref = _domain_ref(registry)
    grants = {
        "numeric_designer": (
            Permission(
                action="propose",
                resource_kind="patch",
                domain_scope=DomainScope(domain_ids=("economy",)),
            ),
            Permission(
                action="approval.decide",
                resource_kind="approval",
                domain_scope=DomainScope(domain_ids=("economy",)),
            ),
        ),
    }
    return RolePolicy(
        policy_version="roles@1",
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=NOW,
        policy_digest=compute_role_policy_digest(
            "roles@1",
            registry_ref,
            grants,
            NOW,
        ),
    )


def _approval_policy() -> ApprovalPolicyV1:
    values = {
        "policy_version": "approval-policy@1",
        "subject_kinds": ("patch", "constraint_proposal", "rollback_request"),
        "maker_checker_required": True,
        "human_approver_required": True,
        "reauthorize_on_decision": True,
        "reauthorize_on_apply": True,
        "rollback_requires_approval": True,
        "terminal_revision_immutable": True,
    }
    return ApprovalPolicyV1(
        **values,
        policy_digest=compute_approval_policy_digest(**values),
    )


def _artifact(
    *,
    kind: str,
    stored_ref: ObjectRef,
    lineage: tuple[str, ...] = (),
    ir_snapshot_id: str = "snapshot:base",
) -> ArtifactV2:
    return build_artifact_v2(
        kind=kind,  # type: ignore[arg-type]
        version_tuple=VersionTuple(
            ir_snapshot_id=ir_snapshot_id,
            tool_version="approval-sqlite-test@1",
        ),
        lineage=lineage,
        payload_hash=stored_ref.sha256,
        object_ref=stored_ref,
    )


class _SqlRepositories:
    """Expose three real repositories through one transaction capability slot."""

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
            cursor_signer=CursorSigner(
                signing_key=b"approval-command-sqlite-cursor-key",
                clock=clock,
            ),
            clock=clock,
            snapshot_ttl=timedelta(minutes=5),
        )
        self._policies = SqlPolicySnapshotRepository(session, clock=clock)
        self._idempotency = SqlIdempotencyRepository(session, clock=clock)
        self._refs = SqlRefStore(
            session,
            cursor_signer=CursorSigner(
                signing_key=b"approval-command-sqlite-ref-cursor-key",
                clock=clock,
            ),
            clock=clock,
        )

    def get(self, identifier: str) -> ArtifactV2 | RefValue | None:
        if identifier == "content/head":
            return self._refs.get(identifier)
        retained = self._artifacts.get(identifier)
        return retained if isinstance(retained, ArtifactV2) else None

    def get_history_entry(self, name: str, revision: int) -> RefValue | None:
        return self._refs.get_history_entry(name, revision)

    def put(self, artifact: ArtifactV2) -> ArtifactV2:
        retained = self._artifacts.put(artifact)
        if not isinstance(retained, ArtifactV2):  # pragma: no cover - repository invariant
            raise AssertionError("ArtifactV2 repository returned a legacy Artifact")
        return retained

    def get_domain_registry(
        self,
        ref: DomainRegistryRefV1,
    ) -> DomainRegistryV1 | None:
        return self._policies.get_domain_registry(ref)

    def get_domain_route_policy(
        self,
        ref: DomainRoutePolicyRefV1,
    ) -> DomainRoutePolicy | None:
        return self._policies.get_domain_route_policy(ref)

    def get_role_policy(self, version: str, digest: str) -> RolePolicy | None:
        return self._policies.get_role_policy(version, digest)

    def get_approval_policy(
        self,
        ref: ApprovalPolicyRefV1,
    ) -> ApprovalPolicyV1 | None:
        return self._policies.get_approval_policy(ref)

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


class _SubjectGateway:
    def __init__(self, subject_id: str) -> None:
        self._subject_id = subject_id

    def inspect_draft_subject(self, artifact: ArtifactV2) -> DraftSubjectFacts:
        if artifact.artifact_id != self._subject_id:
            raise IntegrityViolation("unexpected draft subject")
        return DraftSubjectFacts(
            subject_kind="patch",
            subject_revision=1,
            produced_by="human",
            producer_run_id=None,
            supersedes_artifact_id=None,
            target_artifact_id=None,
            target_snapshot_id="snapshot:preview",
        )

    def load_patch(self, artifact: ArtifactV2) -> PatchV2:
        del artifact
        raise AssertionError("publish_draft must not load a Patch payload")


class _LineageGateway:
    def validate_draft_publication(
        self,
        *,
        prepared: PreparedDraft,
        retained_parent_ids: tuple[str, ...],
    ) -> None:
        del prepared
        if retained_parent_ids:
            raise IntegrityViolation("unexpected retained lineage parent")


class _RunGateway:
    def verify_producer_membership(
        self,
        *,
        run_id: str,
        artifact_id: str,
        initiated_by: AuditActor,
    ) -> None:
        del run_id, artifact_id, initiated_by
        raise AssertionError("human draft must not verify an Agent producer")

    def start_validation(
        self,
        *,
        prepared: PreparedValidationStart,
        item: ApprovalItem,
        initiated_by: AuditActor,
    ) -> str:
        del prepared, item, initiated_by
        raise AssertionError("publish_draft must not start validation")

    def request_validation_cancel(
        self,
        *,
        run_id: str,
        reason: str,
        requested_by: AuditActor,
    ) -> None:
        del run_id, reason, requested_by
        raise AssertionError("initial draft must not cancel validation")


class _UnusedCapability:
    pass


class _PrincipalProjection:
    def __init__(self, identities: Any) -> None:
        self._identities = identities

    def get(self, principal_id: str):
        return self._identities.project(principal_id)


@dataclass
class _AuditInjection:
    fail_after_append: bool = False


class _InjectedAuditWriter:
    def __init__(self, gate: AuditGate, injection: _AuditInjection) -> None:
        self._gate = gate
        self._injection = injection

    def append(self, **kwargs: Any) -> object:
        record = self._gate.append(**kwargs)
        if self._injection.fail_after_append:
            raise IntegrityViolation("injected audit failure after append")
        return record


@dataclass
class _Harness:
    engine: Engine
    object_store: LocalObjectStore
    service: ApprovalCommandService
    prepared: PreparedDraft
    context: ApprovalCommandContext
    payloads: dict[ObjectLocation, bytes]
    audit_injection: _AuditInjection


def _seed_policies(
    engine: Engine,
    *,
    clock: FrozenUtcClock,
    registry: DomainRegistryV1,
    route: DomainRoutePolicy,
    roles: RolePolicy,
    approval: ApprovalPolicyV1,
) -> None:
    approval_registry = ApprovalPolicyRegistryV1(
        policies=(approval,),
        registry_digest=compute_approval_policy_registry_digest((approval,)),
    )
    with Session(engine) as session, session.begin():
        repository = SqlPolicySnapshotRepository(session, clock=clock)
        repository.put_domain_registry(registry)
        repository.put_role_policy(roles)
        repository.put_domain_route_policy(route)
        repository.put_approval_policy_registry(approval_registry)


def _build_harness(tmp_path: Path) -> _Harness:
    clock = FrozenUtcClock(NOW_DT)
    engine = get_engine(f"sqlite:///{tmp_path / 'approval-commands.db'}")
    Base.metadata.create_all(engine)
    object_store = LocalObjectStore(
        tmp_path / "objects",
        store_id="local",
        clock=clock,
        cursor_signing_key=b"approval-command-object-cursor-key",
    )

    registry = _domain_registry()
    route = _route_policy(registry)
    roles = _role_policy(registry)
    approval = _approval_policy()
    _seed_policies(
        engine,
        clock=clock,
        registry=registry,
        route=route,
        roles=roles,
        approval=approval,
    )
    with Session(engine) as session, session.begin():
        identities = SqlIdentityRepository(session, clock=clock)
        principal = identities.create(
            principal_id="human:maker",
            kind="human",
            display_name="Maker",
        )
        identities.grant(
            assignment_id="assignment:human:maker:numeric_designer",
            principal_id=principal.principal_id,
            role="numeric_designer",
            scope=DomainScope(domain_ids=("economy",)),
            granted_by=AuditActor(principal_id="human:admin", principal_kind="human"),
            expected_principal_revision=principal.revision,
        )
        SqlRefStore(
            session,
            cursor_signer=CursorSigner(
                signing_key=b"approval-command-sqlite-ref-cursor-key",
                clock=clock,
            ),
            clock=clock,
        ).compare_and_set("content/head", None, "artifact:base")

    stored = {
        payload: object_store.put_verified(payload)
        for payload in (b'{"patch":"candidate"}', b'{"snapshot":"preview"}')
    }
    subject_stored = stored[b'{"patch":"candidate"}']
    preview_stored = stored[b'{"snapshot":"preview"}']
    subject = _artifact(kind="patch", stored_ref=subject_stored.ref)
    preview = _artifact(
        kind="ir_snapshot",
        stored_ref=preview_stored.ref,
        lineage=(subject.artifact_id,),
        ir_snapshot_id="snapshot:preview",
    )

    scope = DomainScope(domain_ids=("economy",))
    requirements = build_approval_requirements(
        registry=registry,
        policy=route,
        subject_kind="patch",
        domain_scope=scope,
    )
    actor = AuditActor(principal_id="human:maker", principal_kind="human")
    item = ApprovalItem(
        approval_id="approval:sqlite:1",
        subject_series_id="patch-series:sqlite:1",
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        status="draft",
        workflow_revision=1,
        proposer=actor,
        domain_scope=scope,
        domain_registry_ref=_domain_ref(registry),
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
        target_binding=PatchTargetBindingV1(
            target_artifact_id=preview.artifact_id,
            target_snapshot_id="snapshot:preview",
            target_digest=preview.payload_hash,
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=1),
        ),
        created_at=NOW,
    )
    prepared = PreparedDraft(
        subject_artifact=subject,
        companion_artifacts=(preview,),
        object_bindings=(
            PreparedObjectBinding(
                object_ref=subject_stored.ref,
                location=subject_stored.location,
                expected_revision=None,
            ),
            PreparedObjectBinding(
                object_ref=preview_stored.ref,
                location=preview_stored.location,
                expected_revision=None,
            ),
        ),
        approval_item=item,
        expected_subject_head=None,
    )
    subjects = _SubjectGateway(subject.artifact_id)
    lineage = _LineageGateway()
    runs = _RunGateway()
    injection = _AuditInjection()

    def capability_factory(session: Session) -> TransactionCapabilities:
        bindings = SqlObjectBindingRepository(
            session,
            object_store=object_store,
            default_store_id="local",
        )
        return TransactionCapabilities(
            refs=_SqlRepositories(
                session,
                bindings=bindings,
                clock=clock,
            ),
            audit=SqlAuditSink(session),
            approvals=SqlApprovalRepository(session),
            lineage=lineage,
            object_bindings=bindings,
            runs=runs,
            cost=_UnusedCapability(),
            identity=SqlIdentityRepository(session, clock=clock),
        )

    def bind_capabilities(transaction: Any) -> ApprovalCommandCapabilities:
        gate = AuditGate(sink=transaction.audit, clock=clock)
        return ApprovalCommandCapabilities(
            approvals=transaction.approvals,
            policies=transaction.refs,
            artifacts=transaction.refs,
            object_bindings=transaction.object_bindings,
            idempotency=transaction.refs,
            audit=_InjectedAuditWriter(gate, injection),
            runs=transaction.runs,
            subjects=subjects,
            lineage=transaction.lineage,
            evidence=None,
            refs=transaction.refs,
            principals=_PrincipalProjection(transaction.identity),
        )

    service = ApprovalCommandService(
        unit_of_work=SqliteUnitOfWork(engine, capability_factory),
        bind_capabilities=bind_capabilities,
        clock=clock,
        audit_chain_id=AUDIT_CHAIN_ID,
    )
    return _Harness(
        engine=engine,
        object_store=object_store,
        service=service,
        prepared=prepared,
        context=ApprovalCommandContext(
            actor=actor,
            request_id="request:sqlite:1",
            idempotency_scope="principal:human:maker",
            idempotency_key="publish-draft:sqlite:1",
            request_hash="a" * 64,
        ),
        payloads={
            subject_stored.location: b'{"patch":"candidate"}',
            preview_stored.location: b'{"snapshot":"preview"}',
        },
        audit_injection=injection,
    )


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    value = _build_harness(tmp_path)
    yield value
    value.engine.dispose()


def _count(session: Session, model: type[Any]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_publish_draft_commits_all_real_sqlite_repositories_in_one_uow(
    harness: _Harness,
) -> None:
    result = harness.service.publish_draft(
        prepared=harness.prepared,
        context=harness.context,
    )

    assert result.approval_item == harness.prepared.approval_item
    assert result.subject_head == SubjectHead(
        subject_series_id=harness.prepared.approval_item.subject_series_id,
        current_subject_artifact_id=harness.prepared.subject_artifact.artifact_id,
        current_approval_id=harness.prepared.approval_item.approval_id,
        revision=1,
    )
    with Session(harness.engine) as session:
        bindings = SqlObjectBindingRepository(
            session,
            object_store=harness.object_store,
            default_store_id="local",
        )
        artifacts = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(
                signing_key=b"approval-command-verify-cursor-key",
                clock=FrozenUtcClock(NOW_DT),
            ),
            clock=FrozenUtcClock(NOW_DT),
        )
        approvals = SqlApprovalRepository(session)
        idempotency = SqlIdempotencyRepository(
            session,
            clock=FrozenUtcClock(NOW_DT),
        )
        audit = SqlAuditSink(session)

        assert artifacts.get(harness.prepared.subject_artifact.artifact_id) == (
            harness.prepared.subject_artifact
        )
        assert (
            artifacts.get(harness.prepared.companion_artifacts[0].artifact_id)
            == (harness.prepared.companion_artifacts[0])
        )
        for binding in harness.prepared.object_bindings:
            assert bindings.resolve(binding.object_ref).location == binding.location
        assert approvals.get(result.approval_item.approval_id) == result.approval_item
        assert approvals.get_subject_head(result.subject_head.subject_series_id) == (
            result.subject_head
        )
        assert idempotency.get_result(
            scope=harness.context.idempotency_scope,
            operation="approval.publish_draft",
            key=harness.context.idempotency_key,
            request_hash=harness.context.request_hash,
        ) == result.model_dump(mode="json")
        record = audit.get(AUDIT_CHAIN_ID, 1)
        assert record is not None
        assert record.action == "approval.draft_published"
        assert audit.verify_chain(AUDIT_CHAIN_ID) is True

    for location, payload in harness.payloads.items():
        with harness.object_store.open(location) as source:
            assert source.read() == payload


def test_publish_draft_rolls_back_every_db_write_when_audit_append_fails(
    harness: _Harness,
) -> None:
    harness.audit_injection.fail_after_append = True

    with pytest.raises(IntegrityViolation, match="injected audit failure after append"):
        harness.service.publish_draft(
            prepared=harness.prepared,
            context=harness.context,
        )

    with Session(harness.engine) as session:
        for model in (
            ArtifactRow,
            ObjectBindingRow,
            ApprovalItemRow,
            ApprovalDecisionRow,
            SubjectHeadRow,
            IdempotencyRecordRow,
            AuditHeadRow,
        ):
            assert _count(session, model) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditRow)
                .where(AuditRow.audit_schema_version == "audit@2")
            )
            or 0
        ) == 0

    for location, payload in harness.payloads.items():
        assert harness.object_store.stat(location).location == location
        with harness.object_store.open(location) as source:
            assert source.read() == payload
