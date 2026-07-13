from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.identity import Principal
from gameforge.contracts.lineage import ArtifactV2, AuditActor
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRegistryV1,
    SubjectHead,
    compute_approval_policy_registry_digest,
)
from gameforge.platform.approvals.apply import (
    ApprovedApplyCapabilities,
    ApprovedApplyService,
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
    AuditHeadRow,
    AuditRow,
    Base,
    IdempotencyRecordRow,
    RefHistoryRow,
    RefTransitionRow,
)
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.ref_transitions import SqlRefTransitionRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from tests.platform.m4 import apply_testkit


_AUDIT_CHAIN_ID = "platform-authority"
_CURSOR_KEY = b"approved-apply-sqlite-integration-cursor-key"


def _replace_item(item: ApprovalItem, **updates: object) -> ApprovalItem:
    payload = item.model_dump(mode="python")
    payload.update(updates)
    return ApprovalItem.model_validate(payload)


class _SqlApplyCatalog:
    """Group real transaction-bound readers that have no public UoW slot yet."""

    def __init__(self, session: Session, *, clock: FrozenUtcClock) -> None:
        self._artifacts = SqlArtifactRepository(
            session,
            binding_repository=None,
            cursor_signer=CursorSigner(signing_key=_CURSOR_KEY, clock=clock),
            clock=clock,
            snapshot_ttl=timedelta(minutes=5),
        )
        self._policies = SqlPolicySnapshotRepository(session, clock=clock)
        self._idempotency = SqlIdempotencyRepository(session, clock=clock)

    def get(self, artifact_id: str) -> ArtifactV2 | None:
        retained = self._artifacts.get(artifact_id)
        return retained if isinstance(retained, ArtifactV2) else None

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


class _SqlPrincipalProjection:
    def __init__(self, session: Session, *, clock: FrozenUtcClock) -> None:
        self._identities = SqlIdentityRepository(session, clock=clock)

    def get(self, principal_id: str) -> Principal | None:
        return self._identities.project(principal_id)


@dataclass
class _AuditInjection:
    appended_records: int = 0


class _FailAfterRealAuditAppend:
    def __init__(self, gate: AuditGate, injection: _AuditInjection) -> None:
        self._gate = gate
        self._injection = injection

    def append(self, **kwargs: Any) -> object:
        self._gate.append(**kwargs)
        self._injection.appended_records += 1
        raise IntegrityViolation("injected failure after real audit append")


def _seed_artifacts(
    session: Session,
    *,
    object_store: LocalObjectStore,
    state: apply_testkit.ApplyState,
    clock: FrozenUtcClock,
) -> None:
    bindings = SqlObjectBindingRepository(
        session,
        object_store=object_store,
        default_store_id="local",
    )
    artifacts = SqlArtifactRepository(
        session,
        binding_repository=bindings,
        cursor_signer=CursorSigner(signing_key=_CURSOR_KEY, clock=clock),
        clock=clock,
        snapshot_ttl=timedelta(minutes=5),
    )
    for artifact_id in sorted(state.artifacts):
        artifact = state.artifacts[artifact_id]
        stored = object_store.put_verified(state.payloads[artifact_id])
        assert stored.ref == artifact.object_ref
        bindings.bind_verified(stored.ref, stored.location, expected_revision=None)
        assert artifacts.put(artifact) == artifact


def _seed_policies(session: Session, *, clock: FrozenUtcClock) -> None:
    registry = apply_testkit._registry()
    route = apply_testkit._route(registry)
    roles = apply_testkit._roles(registry)
    approval = apply_testkit._approval_policy()
    approval_registry = ApprovalPolicyRegistryV1(
        policies=(approval,),
        registry_digest=compute_approval_policy_registry_digest((approval,)),
    )
    policies = SqlPolicySnapshotRepository(session, clock=clock)
    policies.put_domain_registry(registry)
    policies.put_role_policy(roles)
    policies.put_domain_route_policy(route)
    policies.put_approval_policy_registry(approval_registry)


def _seed_apply_principals(session: Session, *, clock: FrozenUtcClock) -> None:
    identities = SqlIdentityRepository(session, clock=clock)
    for principal_id in ("human:reviewer", "human:operator"):
        principal = identities.create(
            principal_id=principal_id,
            kind="human",
            display_name=principal_id,
        )
        identities.grant(
            assignment_id=f"assignment:{principal_id}:economy",
            principal_id=principal.principal_id,
            role="numeric_designer",
            scope=apply_testkit.DomainScope(domain_ids=("economy",)),
            granted_by=AuditActor(
                principal_id="human:admin",
                principal_kind="human",
            ),
            expected_principal_revision=principal.revision,
        )


def _seed_approval(
    repository: SqlApprovalRepository,
    final: ApprovalItem,
    *,
    publish_head: bool,
) -> None:
    draft = _replace_item(
        final,
        status="draft",
        workflow_revision=1,
        decisions=(),
        active_validation_run_id=None,
        evidence_set_artifact_id=None,
        regression_evidence_artifact_ids=(),
        auto_apply_proof=None,
        submitted_at=None,
        decided_at=None,
        applied_at=None,
    )
    validating = _replace_item(
        draft,
        status="validating",
        workflow_revision=2,
        active_validation_run_id=f"run:validation:{final.approval_id}",
    )
    validated = _replace_item(
        validating,
        status="validated",
        workflow_revision=3,
        active_validation_run_id=None,
        evidence_set_artifact_id=final.evidence_set_artifact_id,
        regression_evidence_artifact_ids=final.regression_evidence_artifact_ids,
        auto_apply_proof=final.auto_apply_proof,
    )
    pending = _replace_item(
        validated,
        status="pending_approval",
        workflow_revision=4,
        submitted_at=final.submitted_at,
    )
    approved = _replace_item(
        pending,
        status="approved",
        workflow_revision=5,
        decisions=final.decisions,
        decided_at=final.decided_at,
    )

    repository.insert_draft(draft)
    repository.compare_and_set(final.approval_id, 1, validating)
    repository.compare_and_set_validation_completion(final.approval_id, 2, validated)
    repository.compare_and_set(final.approval_id, 3, pending)
    assert len(final.decisions) == 1
    repository.append_decision_and_compare_and_set(
        final.approval_id,
        4,
        final.decisions[0],
        approved,
    )
    if final.status == "applied":
        repository.compare_and_set(final.approval_id, 5, final)
    else:
        assert final == approved

    if publish_head:
        repository.compare_and_set_subject_head(
            final.subject_series_id,
            None,
            SubjectHead(
                subject_series_id=final.subject_series_id,
                current_subject_artifact_id=final.subject_artifact_id,
                current_approval_id=final.approval_id,
                revision=1,
            ),
        )


def _seed_database(
    engine: Engine,
    *,
    object_store: LocalObjectStore,
    memory: apply_testkit.Harness,
    clock: FrozenUtcClock,
) -> None:
    rollback = memory.scenario.rollback_request
    reversed_item = memory.scenario.reversed_item
    assert rollback is not None and reversed_item is not None

    with Session(engine) as session, session.begin():
        _seed_artifacts(
            session,
            object_store=object_store,
            state=memory.state,
            clock=clock,
        )
        _seed_policies(session, clock=clock)
        _seed_apply_principals(session, clock=clock)

        approvals = SqlApprovalRepository(session)
        _seed_approval(approvals, memory.scenario.item, publish_head=True)
        _seed_approval(approvals, reversed_item, publish_head=False)

        refs = SqlRefStore(
            session,
            cursor_signer=CursorSigner(signing_key=_CURSOR_KEY, clock=clock),
            clock=clock,
        )
        historical = refs.compare_and_set(
            rollback.ref_name,
            None,
            rollback.target_artifact_id,
        )
        current = refs.compare_and_set(
            rollback.ref_name,
            historical,
            rollback.expected_current_ref.artifact_id,
        )
        assert historical == RefValue(
            artifact_id=rollback.target_artifact_id,
            revision=rollback.target_history_revision,
        )
        assert current == rollback.expected_current_ref


def _count(session: Session, model: type[Any]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_real_sqlite_uow_rolls_back_approved_rollback_after_audit_append_failure(
    tmp_path: Path,
) -> None:
    memory = apply_testkit.harness("rollback_request", with_reversed_item=True)
    rollback = memory.scenario.rollback_request
    reversed_before = memory.scenario.reversed_item
    assert rollback is not None and reversed_before is not None

    clock = FrozenUtcClock(apply_testkit.NOW_DT)
    engine = get_engine(f"sqlite:///{tmp_path / 'approved-apply.db'}")
    Base.metadata.create_all(engine)
    object_store = LocalObjectStore(
        tmp_path / "objects",
        store_id="local",
        clock=clock,
        cursor_signing_key=b"approved-apply-object-cursor-key",
    )
    _seed_database(
        engine,
        object_store=object_store,
        memory=memory,
        clock=clock,
    )
    injection = _AuditInjection()

    def capability_factory(session: Session) -> TransactionCapabilities:
        return TransactionCapabilities(
            refs=SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=_CURSOR_KEY, clock=clock),
                clock=clock,
            ),
            audit=SqlAuditSink(session),
            approvals=SqlApprovalRepository(session),
            lineage=_SqlApplyCatalog(session, clock=clock),
            object_bindings=_SqlPrincipalProjection(session, clock=clock),
            runs=SqlRefTransitionRepository(session),
            cost=object(),
        )

    def bind_capabilities(transaction: Any) -> ApprovedApplyCapabilities:
        return ApprovedApplyCapabilities(
            approvals=transaction.approvals,
            policies=transaction.lineage,
            principals=transaction.object_bindings,
            artifacts=transaction.lineage,
            refs=transaction.refs,
            transitions=transaction.runs,
            idempotency=transaction.lineage,
            audit=_FailAfterRealAuditAppend(
                AuditGate(sink=transaction.audit, clock=clock),
                injection,
            ),
            subjects=apply_testkit.Subjects(memory.state),
            evidence=apply_testkit.Evidence(memory.state),
            targets=apply_testkit.Targets(memory.state),
            rollback_execution=apply_testkit.RollbackExecution(),
        )

    service = ApprovedApplyService(
        unit_of_work=SqliteUnitOfWork(engine, capability_factory),
        bind_capabilities=bind_capabilities,
        clock=clock,
        audit_chain_id=_AUDIT_CHAIN_ID,
    )
    command = apply_testkit.request(memory)

    try:
        with pytest.raises(
            IntegrityViolation,
            match="injected failure after real audit append",
        ):
            service.apply(command)

        assert injection.appended_records == 1
        with Session(engine) as session:
            approvals = SqlApprovalRepository(session)
            refs = SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=_CURSOR_KEY, clock=clock),
                clock=clock,
            )
            idempotency = SqlIdempotencyRepository(session, clock=clock)

            assert approvals.get(memory.scenario.item.approval_id) == memory.scenario.item
            assert approvals.get(reversed_before.approval_id) == reversed_before
            assert refs.get(rollback.ref_name) == rollback.expected_current_ref
            assert refs.get_history_entry(rollback.ref_name, 1) == RefValue(
                artifact_id=rollback.target_artifact_id,
                revision=1,
            )
            assert refs.get_history_entry(rollback.ref_name, 2) == rollback.expected_current_ref
            assert refs.get_history_entry(rollback.ref_name, 3) is None
            assert _count(session, RefHistoryRow) == 2
            assert _count(session, RefTransitionRow) == 0
            assert _count(session, AuditRow) == 0
            assert _count(session, AuditHeadRow) == 0
            assert _count(session, IdempotencyRecordRow) == 0
            assert (
                idempotency.get_result(
                    scope=command.context.idempotency_scope,
                    operation="approval.apply",
                    key=command.context.idempotency_key,
                    request_hash=command.context.request_hash,
                )
                is None
            )
    finally:
        engine.dispose()
