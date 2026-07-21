from __future__ import annotations

import copy
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from gameforge.contracts.errors import (
    Conflict,
    Forbidden,
    IntegrityViolation,
    InvalidStateTransition,
)
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRouteRule,
    DomainScope,
    Permission,
    Principal,
    RoleAssignmentV1,
    RolePolicy,
    compute_domain_registry_digest,
    compute_domain_route_policy_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    AuditCorrelation,
    AuditSubject,
    ObjectBinding,
    ObjectLocation,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
    object_key_for_sha256,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.jobs import (
    GenerationProposePayloadV1,
    PromptGoalBindingV1,
    RefReadBindingV1,
)
from gameforge.contracts.workflow import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalPolicyV1,
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyProofBindingV1,
    PatchTargetBindingV1,
    ConstraintTargetBindingV1,
    RollbackRequestV1,
    RollbackTargetBindingV1,
    SubjectHead,
    compute_approval_policy_digest,
)
from gameforge.platform.approvals import build_approval_requirements
from gameforge.platform.approvals.commands import (
    ApprovalCommandCapabilities,
    ApprovalCommandContext,
    ApprovalCommandService,
    ApprovalDecisionRequest,
    DraftPublicationResult,
    DraftSubjectFacts,
    EvidenceStateProjection,
    PreparedDraft,
    PreparedObjectBinding,
    PreparedTerminalDraft,
    PreparedValidationStart,
    ValidationStartResult,
)
from gameforge.platform.publication.effects import (
    AgentDraftWorkflowRequest,
    ApprovalCommandAgentDraftWorkflowPort,
)
from gameforge.platform.registry.defaults import build_builtin_registry
from tests.platform.m4c.handler_support import (
    WORKER,
    build_envelope,
    build_run_record,
)

NOW_DT = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-14T12:00:00Z"


def _registry() -> DomainRegistryV1:
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


def _route(
    registry: DomainRegistryV1,
    *,
    min_approvals: int = 1,
) -> DomainRoutePolicy:
    rules = (
        DomainRouteRule(
            rule_id="route:economy",
            domain_selector=DomainScope(domain_ids=("economy",)),
            subject_kinds=("patch", "constraint_proposal", "rollback_request"),
            route_role="numeric_designer",
            required_action="approval.decide",
            resource_kind="approval",
            min_approvals=min_approvals,
        ),
    )
    ref = _domain_ref(registry)
    return DomainRoutePolicy(
        route_version="routes@1",
        domain_registry_ref=ref,
        rules=rules,
        effective_from=NOW,
        route_digest=compute_domain_route_policy_digest("routes@1", ref, rules, NOW),
    )


def _roles(registry: DomainRegistryV1) -> RolePolicy:
    ref = _domain_ref(registry)
    grants = {
        "numeric_designer": (
            Permission(
                action="propose",
                resource_kind="patch",
                domain_scope=DomainScope(domain_ids=("economy",)),
            ),
            Permission(
                action="propose",
                resource_kind="constraint_proposal",
                domain_scope=DomainScope(domain_ids=("economy",)),
            ),
            Permission(
                action="propose",
                resource_kind="rollback_request",
                domain_scope=DomainScope(domain_ids=("economy",)),
            ),
            Permission(
                action="approval.decide",
                resource_kind="approval",
                domain_scope=DomainScope(domain_ids=("economy",)),
            ),
        )
    }
    return RolePolicy(
        policy_version="roles@1",
        domain_registry_ref=ref,
        grants=grants,
        effective_from=NOW,
        policy_digest=compute_role_policy_digest("roles@1", ref, grants, NOW),
    )


def _approval_policy() -> ApprovalPolicyV1:
    fields = {
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
        **fields,
        policy_digest=compute_approval_policy_digest(**fields),
    )


def _artifact(
    kind: str,
    digest_char: str,
    *parents: str,
    ir_snapshot_id: str | None = "snapshot:base",
    constraint_snapshot_id: str | None = None,
) -> ArtifactV2:
    digest = digest_char * 64
    ref = ObjectRef(
        key=object_key_for_sha256(digest),
        sha256=digest,
        size_bytes=10,
    )
    return build_artifact_v2(
        kind=kind,  # type: ignore[arg-type]
        version_tuple=VersionTuple(
            ir_snapshot_id=ir_snapshot_id,
            constraint_snapshot_id=constraint_snapshot_id,
            tool_version="workflow-test@1",
        ),
        lineage=parents,
        payload_hash=digest,
        object_ref=ref,
    )


def _location(artifact: ArtifactV2) -> ObjectLocation:
    return ObjectLocation(
        store_id="local",
        key=artifact.object_ref.key,
        backend_generation=f"generation:{artifact.payload_hash[:8]}",
    )


def _patch() -> PatchV2:
    return PatchV2(
        revision=1,
        base_snapshot_id="snapshot:base",
        target_snapshot_id="snapshot:preview",
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=[],
        produced_by="human",
        rationale="author revision",
    )


def _principal(principal_id: str) -> Principal:
    assignment = RoleAssignmentV1(
        assignment_id=f"assignment:{principal_id}",
        principal_id=principal_id,
        role="numeric_designer",
        scope=DomainScope(domain_ids=("economy",)),
        status="active",
        revision=1,
        granted_at=NOW,
        granted_by=AuditActor(principal_id="human:admin", principal_kind="human"),
    )
    return Principal(
        id=principal_id,
        kind="human",
        display_name=principal_id,
        status="active",
        revision=1,
        credential_epoch=0,
        authz_revision=1,
        roles=(assignment,),
    )


@dataclass
class _State:
    approvals: dict[str, ApprovalItem] = field(default_factory=dict)
    heads: dict[str, SubjectHead] = field(default_factory=dict)
    artifacts: dict[str, ArtifactV2] = field(default_factory=dict)
    bindings: dict[tuple[str, str], ObjectBinding] = field(default_factory=dict)
    idempotency: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = field(
        default_factory=dict
    )
    audit: list[tuple[str, AuditSubject]] = field(default_factory=list)
    cancellations: list[str] = field(default_factory=list)
    started_runs: list[str] = field(default_factory=list)
    refs: dict[str, RefValue] = field(default_factory=dict)
    ref_history: dict[tuple[str, int], RefValue] = field(default_factory=dict)


class _ApprovalRepo:
    def __init__(self, state: _State) -> None:
        self.state = state

    def insert_draft(self, item: ApprovalItem) -> ApprovalItem:
        current = self.state.approvals.get(item.approval_id)
        if current is not None and current != item:
            raise IntegrityViolation("approval collision")
        self.state.approvals[item.approval_id] = item
        return item

    def get(self, approval_id: str) -> ApprovalItem | None:
        return self.state.approvals.get(approval_id)

    def compare_and_set(
        self,
        approval_id: str,
        expected_workflow_revision: int,
        replacement: ApprovalItem,
    ) -> ApprovalItem:
        current = self.state.approvals.get(approval_id)
        if current is None or current.workflow_revision != expected_workflow_revision:
            raise Conflict("approval CAS")
        self.state.approvals[approval_id] = replacement
        return replacement

    def append_decision_and_compare_and_set(
        self,
        approval_id: str,
        expected_workflow_revision: int,
        decision: ApprovalDecision,
        replacement: ApprovalItem,
    ) -> ApprovalItem:
        return self.compare_and_set(
            approval_id,
            expected_workflow_revision,
            replacement,
        )

    def get_subject_head(self, subject_series_id: str) -> SubjectHead | None:
        return self.state.heads.get(subject_series_id)

    def compare_and_set_subject_head(
        self,
        subject_series_id: str,
        expected: SubjectHead | None,
        replacement: SubjectHead,
    ) -> SubjectHead:
        if self.state.heads.get(subject_series_id) != expected:
            raise Conflict("head CAS")
        self.state.heads[subject_series_id] = replacement
        return replacement

    def current(self, subject_series_id: str) -> tuple[SubjectHead, ApprovalItem] | None:
        head = self.state.heads.get(subject_series_id)
        if head is None:
            return None
        return head, self.state.approvals[head.current_approval_id]

    def apply_preflighted_insert_draft(self, item: ApprovalItem) -> ApprovalItem:
        if item.approval_id in self.state.approvals:
            raise Conflict("preflighted approval insert")
        self.state.approvals[item.approval_id] = item
        return item

    def apply_preflighted_compare_and_set(
        self,
        current: ApprovalItem,
        replacement: ApprovalItem,
    ) -> ApprovalItem:
        if self.state.approvals.get(current.approval_id) != current:
            raise Conflict("preflighted approval CAS")
        self.state.approvals[current.approval_id] = replacement
        return replacement

    def apply_preflighted_validation_completion(
        self,
        current: ApprovalItem,
        replacement: ApprovalItem,
    ) -> ApprovalItem:
        return self.apply_preflighted_compare_and_set(current, replacement)

    def apply_preflighted_subject_head(
        self,
        *,
        expected: SubjectHead | None,
        expected_item: ApprovalItem | None,
        replacement: SubjectHead,
        replacement_item: ApprovalItem,
    ) -> SubjectHead:
        del expected_item, replacement_item
        if self.state.heads.get(replacement.subject_series_id) != expected:
            raise Conflict("preflighted SubjectHead CAS")
        self.state.heads[replacement.subject_series_id] = replacement
        return replacement


class _Refs:
    def __init__(self, state: _State) -> None:
        self.state = state

    def get(self, name: str) -> RefValue | None:
        return self.state.refs.get(name)

    def get_history_entry(self, name: str, revision: int) -> RefValue | None:
        return self.state.ref_history.get((name, revision))


class _Artifacts:
    def __init__(self, state: _State) -> None:
        self.state = state

    def get(self, artifact_id: str) -> ArtifactV2 | None:
        return self.state.artifacts.get(artifact_id)

    def put(self, artifact: ArtifactV2) -> ArtifactV2:
        current = self.get(artifact.artifact_id)
        if current is not None and current != artifact:
            raise IntegrityViolation("artifact collision")
        self.state.artifacts[artifact.artifact_id] = artifact
        return artifact


class _Bindings:
    def __init__(self, state: _State) -> None:
        self.state = state

    def bind_verified(
        self,
        ref: ObjectRef,
        location: ObjectLocation,
        expected_revision: int | None,
    ) -> ObjectBinding:
        key = (ref.key, location.store_id)
        current = self.state.bindings.get(key)
        if current is not None:
            return current
        binding = ObjectBinding(
            object_ref=ref,
            location=location,
            status="active",
            revision=1,
            verified_at=NOW,
        )
        self.state.bindings[key] = binding
        return binding


class _Policies:
    def __init__(
        self,
        registry: DomainRegistryV1,
        route: DomainRoutePolicy,
        roles: RolePolicy,
        approval: ApprovalPolicyV1,
    ) -> None:
        self.registry = registry
        self.route = route
        self.roles = roles
        self.approval = approval

    def get_domain_registry(self, ref: DomainRegistryRefV1) -> DomainRegistryV1 | None:
        return self.registry if ref == _domain_ref(self.registry) else None

    def get_domain_route_policy(self, ref: Any) -> DomainRoutePolicy | None:
        return self.route if ref.route_digest == self.route.route_digest else None

    def get_role_policy(self, version: str, digest: str) -> RolePolicy | None:
        return (
            self.roles
            if (version, digest) == (self.roles.policy_version, self.roles.policy_digest)
            else None
        )

    def get_approval_policy(self, ref: ApprovalPolicyRefV1) -> ApprovalPolicyV1 | None:
        expected = ApprovalPolicyRefV1(
            policy_version=self.approval.policy_version,
            policy_digest=self.approval.policy_digest,
        )
        return self.approval if ref == expected else None


class _Principals:
    def __init__(self) -> None:
        self.values: dict[str, Principal] = {}

    def get(self, principal_id: str) -> Principal | None:
        return self.values.get(principal_id)


class _Idempotency:
    def __init__(self, state: _State) -> None:
        self.state = state

    def get_result(
        self, *, scope: str, operation: str, key: str, request_hash: str
    ) -> dict[str, Any] | None:
        retained = self.state.idempotency.get((scope, operation, key))
        if retained is None:
            return None
        retained_hash, response = retained
        if retained_hash != request_hash:
            raise Conflict("different request")
        return copy.deepcopy(response)

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
        self.state.idempotency[(scope, operation, key)] = (
            request_hash,
            copy.deepcopy(response),
        )
        return copy.deepcopy(response)

    def put_preflighted_result(
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
        del resource_kind, resource_id
        identity = (scope, operation, key)
        if identity in self.state.idempotency:
            raise Conflict("preflighted idempotency insert")
        self.state.idempotency[identity] = (
            request_hash,
            copy.deepcopy(response),
        )
        return copy.deepcopy(response)


@dataclass(frozen=True)
class _PreparedAuditBatch:
    intents: tuple[Any, ...]


class _Audit:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.fail = False

    def append(
        self,
        *,
        chain_id: str,
        actor: AuditActor,
        initiated_by: AuditActor | None,
        action: str,
        subject: AuditSubject,
        correlation: AuditCorrelation,
    ) -> object:
        if self.fail:
            raise IntegrityViolation("audit unavailable")
        self.state.audit.append((action, subject))
        return object()

    def prepare_batch(
        self,
        *,
        chain_id: str,
        intents: tuple[Any, ...],
    ) -> _PreparedAuditBatch:
        del chain_id
        return _PreparedAuditBatch(intents=intents)

    def apply_prepared_batch(self, prepared: _PreparedAuditBatch) -> None:
        if self.fail:
            raise IntegrityViolation("audit unavailable")
        self.state.audit.extend((intent.action, intent.subject) for intent in prepared.intents)


class _Runs:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.produced: set[tuple[str, str, str]] = set()

    def verify_producer_membership(
        self,
        *,
        run_id: str,
        artifact_id: str,
        initiated_by: AuditActor,
    ) -> None:
        identity = (run_id, artifact_id, initiated_by.principal_id)
        if identity not in self.produced:
            raise IntegrityViolation("producer membership is unavailable")

    def verify_prepared_terminal_producer_authority(
        self,
        *,
        run_id: str,
        initiated_by: AuditActor,
    ) -> None:
        if (run_id, initiated_by.principal_id) not in {
            (produced_run_id, principal_id)
            for produced_run_id, _artifact_id, principal_id in self.produced
        }:
            raise IntegrityViolation("prepared producer authority is unavailable")

    def start_validation(
        self,
        *,
        prepared: PreparedValidationStart,
        item: ApprovalItem,
        initiated_by: AuditActor,
    ) -> str:
        self.state.started_runs.append(prepared.run_id)
        return prepared.run_id

    def request_validation_cancel(
        self,
        *,
        run_id: str,
        reason: str,
        requested_by: AuditActor,
    ) -> None:
        self.state.cancellations.append(run_id)


class _Subjects:
    def __init__(self) -> None:
        self.facts: dict[str, DraftSubjectFacts] = {}
        self.patches: dict[str, PatchV2] = {}

    def inspect_draft_subject(self, artifact: ArtifactV2) -> DraftSubjectFacts:
        try:
            return self.facts[artifact.artifact_id]
        except KeyError as exc:
            raise IntegrityViolation("subject payload is unavailable") from exc

    def load_patch(self, artifact: ArtifactV2) -> PatchV2:
        try:
            return self.patches[artifact.artifact_id]
        except KeyError as exc:
            raise IntegrityViolation("patch payload is unavailable") from exc


class _Lineage:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def validate_draft_publication(
        self,
        *,
        prepared: PreparedDraft,
        retained_parent_ids: tuple[str, ...],
    ) -> None:
        self.calls += 1
        if self.fail:
            raise IntegrityViolation("lineage policy rejected draft")


class _Evidence:
    def __init__(self) -> None:
        self.projection = EvidenceStateProjection(
            validation_status="passed",
            regression_status="passed",
        )
        self.fail = False

    def validate_submission(
        self,
        *,
        item: ApprovalItem,
        subject_artifact: ArtifactV2,
        target_artifact: ArtifactV2,
        evidence_artifact: ArtifactV2,
        regression_artifacts: tuple[ArtifactV2, ...],
    ) -> EvidenceStateProjection:
        if self.fail:
            raise IntegrityViolation("evidence binding rejected")
        return self.projection

    def project_state(
        self,
        *,
        item: ApprovalItem,
    ) -> EvidenceStateProjection:
        return self.projection


class _AutoApply:
    def __init__(self) -> None:
        self.calls: list[ApprovalItem] = []
        self.fail = False

    def validate_eligibility(self, *, item: ApprovalItem) -> None:
        self.calls.append(item)
        if self.fail:
            raise IntegrityViolation("auto-apply guard rejected eligibility")


class _UowContext(AbstractContextManager[object]):
    def __init__(self, owner: "_Uow") -> None:
        self.owner = owner
        self.snapshot: dict[str, Any] | None = None

    def __enter__(self) -> object:
        self.snapshot = copy.deepcopy(self.owner.state.__dict__)
        return object()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is not None:
            assert self.snapshot is not None
            self.owner.state.__dict__.clear()
            self.owner.state.__dict__.update(self.snapshot)
            self.owner.rollbacks += 1
        else:
            self.owner.commits += 1
        return False


class _Uow:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0

    def begin(self) -> _UowContext:
        self.begins += 1
        return _UowContext(self)


@dataclass
class _CountingClock:
    value: datetime
    calls: int = 0

    def now_utc(self) -> datetime:
        self.calls += 1
        return self.value


@dataclass
class _DecisionIdFactory:
    calls: int = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"decision:server:{self.calls}"


@dataclass
class _Harness:
    state: _State
    uow: _Uow
    capabilities: ApprovalCommandCapabilities
    service: ApprovalCommandService
    subjects: _Subjects
    lineage: _Lineage
    evidence: _Evidence
    auto_apply: _AutoApply
    runs: _Runs
    audit: _Audit
    registry: DomainRegistryV1
    route: DomainRoutePolicy
    roles: RolePolicy
    approval_policy: ApprovalPolicyV1
    principals: _Principals
    clock: _CountingClock
    decision_ids: _DecisionIdFactory


def _harness(*, min_approvals: int = 1) -> _Harness:
    state = _State()
    state.refs["content/head"] = RefValue(artifact_id="artifact:base", revision=1)
    registry = _registry()
    route = _route(registry, min_approvals=min_approvals)
    roles = _roles(registry)
    approval = _approval_policy()
    subjects = _Subjects()
    lineage = _Lineage()
    evidence = _Evidence()
    auto_apply = _AutoApply()
    runs = _Runs(state)
    audit = _Audit(state)
    principals = _Principals()
    principals.values["human:maker"] = _principal("human:maker")
    clock = _CountingClock(NOW_DT)
    decision_ids = _DecisionIdFactory()
    capabilities = ApprovalCommandCapabilities(
        approvals=_ApprovalRepo(state),
        policies=_Policies(registry, route, roles, approval),
        artifacts=_Artifacts(state),
        object_bindings=_Bindings(state),
        idempotency=_Idempotency(state),
        audit=audit,
        runs=runs,
        subjects=subjects,
        lineage=lineage,
        evidence=evidence,
        auto_apply=auto_apply,
        refs=_Refs(state),
        principals=principals,
    )
    uow = _Uow(state)
    service = ApprovalCommandService(
        unit_of_work=uow,
        bind_capabilities=lambda transaction: capabilities,
        clock=clock,
        audit_chain_id="authority",
        decision_id_factory=decision_ids,
    )
    return _Harness(
        state=state,
        uow=uow,
        capabilities=capabilities,
        service=service,
        subjects=subjects,
        lineage=lineage,
        evidence=evidence,
        auto_apply=auto_apply,
        runs=runs,
        audit=audit,
        registry=registry,
        route=route,
        roles=roles,
        approval_policy=approval,
        principals=principals,
        clock=clock,
        decision_ids=decision_ids,
    )


def _context(
    actor: str = "human:maker",
    *,
    key: str = "request:1",
    request_hash: str = "9" * 64,
) -> ApprovalCommandContext:
    return ApprovalCommandContext(
        actor=AuditActor(principal_id=actor, principal_kind="human"),
        request_id=key,
        idempotency_scope=f"principal:{actor}",
        idempotency_key=key,
        request_hash=request_hash,
    )


def _draft(
    harness: _Harness,
    *,
    revision: int = 1,
    supersedes_artifact_id: str | None = None,
    supersedes_approval_id: str | None = None,
    expected_head: SubjectHead | None = None,
    preview_snapshot_id: str = "snapshot:preview",
) -> PreparedDraft:
    subject = _artifact(
        "patch",
        "1" if revision == 1 else "3",
        *(()) if revision == 1 else (supersedes_artifact_id or "",),
    )
    preview = _artifact(
        "ir_snapshot",
        "2" if revision == 1 else "4",
        subject.artifact_id,
        ir_snapshot_id=preview_snapshot_id,
    )
    scope = DomainScope(domain_ids=("economy",))
    requirements = build_approval_requirements(
        registry=harness.registry,
        policy=harness.route,
        subject_kind="patch",
        domain_scope=scope,
    )
    item = ApprovalItem(
        approval_id=f"approval:{revision}",
        subject_series_id="patch-series:1",
        subject_revision=revision,
        subject_kind="patch",
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        status="draft",
        workflow_revision=1,
        supersedes_approval_id=supersedes_approval_id,
        proposer=AuditActor(principal_id="human:maker", principal_kind="human"),
        domain_scope=scope,
        domain_registry_ref=_domain_ref(harness.registry),
        route_policy={
            "route_version": harness.route.route_version,
            "route_digest": harness.route.route_digest,
            "domain_registry_ref": harness.route.domain_registry_ref,
        },
        role_policy_version=harness.roles.policy_version,
        role_policy_digest=harness.roles.policy_digest,
        approval_policy=ApprovalPolicyRefV1(
            policy_version=harness.approval_policy.policy_version,
            policy_digest=harness.approval_policy.policy_digest,
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
    patch = _patch().model_copy(
        update={
            "revision": revision,
            "supersedes_artifact_id": supersedes_artifact_id,
        }
    )
    harness.subjects.facts[subject.artifact_id] = DraftSubjectFacts(
        subject_kind="patch",
        subject_revision=revision,
        produced_by="human",
        producer_run_id=None,
        supersedes_artifact_id=supersedes_artifact_id,
        target_artifact_id=None,
        target_snapshot_id="snapshot:preview",
    )
    harness.subjects.patches[subject.artifact_id] = patch
    return PreparedDraft(
        subject_artifact=subject,
        companion_artifacts=(preview,),
        object_bindings=tuple(
            PreparedObjectBinding(
                object_ref=artifact.object_ref,
                location=_location(artifact),
                expected_revision=None,
            )
            for artifact in (subject, preview)
        ),
        approval_item=item,
        expected_subject_head=expected_head,
    )


def _rollback_profile_binding() -> ResolvedExecutionProfileBindingV1:
    return ResolvedExecutionProfileBindingV1(
        field_path="/params/rollback_profile",
        profile=ProfileRefV1(profile_id="rollback.default", version=1),
        expected_profile_kind="rollback",
        profile_payload_hash="a" * 64,
        catalog_version=1,
        catalog_digest="b" * 64,
    )


def _rollback_draft(
    harness: _Harness,
) -> tuple[PreparedDraft, RollbackRequestV1]:
    current = _artifact(
        "ir_snapshot",
        "7",
        ir_snapshot_id="snapshot:current",
    )
    target = _artifact(
        "ir_snapshot",
        "8",
        ir_snapshot_id="snapshot:rollback-target",
    )
    subject = _artifact(
        "rollback_request",
        "9",
        current.artifact_id,
        target.artifact_id,
        ir_snapshot_id=None,
    )
    profile = _rollback_profile_binding()
    expected_ref = RefValue(artifact_id=current.artifact_id, revision=4)
    request = RollbackRequestV1(
        ref_name="content/head",
        expected_current_ref=expected_ref,
        target_artifact_id=target.artifact_id,
        target_history_revision=2,
        rollback_profile_binding=profile,
        reason="restore retained content",
    )
    scope = DomainScope(domain_ids=("economy",))
    requirements = build_approval_requirements(
        registry=harness.registry,
        policy=harness.route,
        subject_kind="rollback_request",
        domain_scope=scope,
    )
    item = ApprovalItem(
        approval_id="approval:rollback:1",
        subject_series_id="rollback-series:1",
        subject_revision=1,
        subject_kind="rollback_request",
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        status="draft",
        workflow_revision=1,
        proposer=AuditActor(principal_id="human:maker", principal_kind="human"),
        domain_scope=scope,
        domain_registry_ref=_domain_ref(harness.registry),
        route_policy={
            "route_version": harness.route.route_version,
            "route_digest": harness.route.route_digest,
            "domain_registry_ref": harness.route.domain_registry_ref,
        },
        role_policy_version=harness.roles.policy_version,
        role_policy_digest=harness.roles.policy_digest,
        approval_policy=ApprovalPolicyRefV1(
            policy_version=harness.approval_policy.policy_version,
            policy_digest=harness.approval_policy.policy_digest,
        ),
        requirements=requirements,
        decisions=(),
        regression_evidence_artifact_ids=(),
        target_binding=RollbackTargetBindingV1(
            target_artifact_kind="ir_snapshot",
            target_artifact_id=target.artifact_id,
            target_snapshot_id="snapshot:rollback-target",
            target_digest=target.payload_hash,
            ref_name=request.ref_name,
            expected_ref=request.expected_current_ref,
            rollback_profile_binding=profile,
        ),
        created_at=NOW,
    )
    harness.state.artifacts[current.artifact_id] = current
    harness.state.artifacts[target.artifact_id] = target
    harness.state.refs[request.ref_name] = expected_ref
    harness.state.ref_history[(request.ref_name, request.target_history_revision)] = RefValue(
        artifact_id=target.artifact_id,
        revision=request.target_history_revision,
    )
    harness.subjects.facts[subject.artifact_id] = DraftSubjectFacts(
        subject_kind="rollback_request",
        subject_revision=None,
        produced_by="human",
        producer_run_id=None,
        supersedes_artifact_id=None,
        target_artifact_id=target.artifact_id,
        target_snapshot_id="snapshot:rollback-target",
        rollback_request=request,
    )
    return (
        PreparedDraft(
            subject_artifact=subject,
            companion_artifacts=(),
            object_bindings=(
                PreparedObjectBinding(
                    object_ref=subject.object_ref,
                    location=_location(subject),
                    expected_revision=None,
                ),
            ),
            approval_item=item,
            expected_subject_head=None,
        ),
        request,
    )


def _publish(harness: _Harness) -> tuple[PreparedDraft, DraftPublicationResult]:
    prepared = _draft(harness)
    result = harness.service.publish_draft(prepared=prepared, context=_context())
    return prepared, result


def _replace_item(item: ApprovalItem, **updates: object) -> ApprovalItem:
    payload = item.model_dump(mode="python")
    payload.update(updates)
    return ApprovalItem.model_validate(payload)


def test_publish_draft_atomically_publishes_bindings_artifacts_item_head_and_audit() -> None:
    harness = _harness()
    prepared, result = _publish(harness)

    assert result.approval_item == prepared.approval_item
    assert result.subject_head == SubjectHead(
        subject_series_id="patch-series:1",
        current_subject_artifact_id=prepared.subject_artifact.artifact_id,
        current_approval_id=prepared.approval_item.approval_id,
        revision=1,
    )
    assert set(harness.state.artifacts) == {
        prepared.subject_artifact.artifact_id,
        prepared.companion_artifacts[0].artifact_id,
    }
    assert len(harness.state.bindings) == 2
    assert harness.lineage.calls == 1
    assert [action for action, _ in harness.state.audit] == ["approval.draft_published"]
    assert harness.uow.commits == 1

    replay = harness.service.publish_draft(prepared=prepared, context=_context())
    assert replay == result
    assert [action for action, _ in harness.state.audit] == ["approval.draft_published"]

    with pytest.raises(Conflict, match="different request"):
        harness.service.publish_draft(
            prepared=prepared,
            context=_context(request_hash="8" * 64),
        )


def test_prepared_terminal_agent_draft_apply_is_read_free(monkeypatch) -> None:
    harness = _harness()
    legacy = _draft(harness)
    run_id = "run:agent-draft:1"
    facts = DraftSubjectFacts(
        subject_kind="patch",
        subject_revision=legacy.approval_item.subject_revision,
        produced_by="agent",
        producer_run_id=run_id,
        supersedes_artifact_id=None,
        target_artifact_id=None,
        target_snapshot_id="snapshot:preview",
    )
    prepared = PreparedTerminalDraft.seal(
        subject_artifact=legacy.subject_artifact,
        companion_artifacts=legacy.companion_artifacts,
        subject_facts=facts,
        retained_parent_ids=(),
        approval_item=legacy.approval_item,
        expected_subject_head=None,
        expected_previous_workflow_revision=None,
    )
    context = ApprovalCommandContext(
        actor=AuditActor(principal_id="service:worker:1", principal_kind="service"),
        initiated_by=legacy.approval_item.proposer,
        request_id="terminal-workflow:run:agent-draft:1",
        run_id=run_id,
        idempotency_scope=f"run:{run_id}",
        idempotency_key="create_patch_subject_head_and_draft@1:request",
        request_hash="7" * 64,
    )
    harness.runs.produced.add(
        (
            run_id,
            legacy.subject_artifact.artifact_id,
            legacy.approval_item.proposer.principal_id,
        )
    )
    before = copy.deepcopy(harness.state)

    preflighted = harness.service.preflight_prepared_terminal_draft_in_transaction(
        prepared=prepared,
        context=context,
        capabilities=harness.capabilities,
        merge_audit_into_terminal_batch=True,
    )

    assert harness.state == before
    merged_audit = preflighted.audit_intents_for_terminal_merge(
        context=context,
        capabilities=harness.capabilities,
    )
    assert merged_audit.chain_id == "authority"
    assert len(merged_audit.intents) == 1
    assert merged_audit.intents[0].action == "approval.draft_published"
    assert merged_audit.intents[0].subject.resource_kind == "approval"
    assert merged_audit.intents[0].subject.resource_id == legacy.approval_item.approval_id
    assert merged_audit.intents[0].subject.artifact_id == legacy.subject_artifact.artifact_id
    expected_item = prepared.approval_item
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(preflighted, "prepared", prepared)
    with pytest.raises(IntegrityViolation, match="invalid"):
        harness.service.apply_preflighted_terminal_draft_in_transaction(
            preflighted=copy.copy(preflighted),
            context=context,
            capabilities=harness.capabilities,
        )
    retained_approvals = harness.capabilities.approvals
    harness.capabilities.approvals = None
    with pytest.raises(IntegrityViolation, match="invalid"):
        harness.service.apply_preflighted_terminal_draft_in_transaction(
            preflighted=preflighted,
            context=context,
            capabilities=harness.capabilities,
        )
    harness.capabilities.approvals = retained_approvals
    object.__setattr__(
        prepared,
        "approval_item",
        prepared.approval_item.model_copy(update={"approval_id": "approval:changed"}),
    )

    def _unexpected_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("prepared terminal apply issued a read")

    approvals = harness.capabilities.approvals
    policies = harness.capabilities.policies
    idempotency = harness.capabilities.idempotency
    refs = harness.capabilities.refs
    assert approvals is not None
    assert policies is not None
    assert idempotency is not None
    assert refs is not None
    for target, method_names in (
        (approvals, ("get", "current", "get_subject_head")),
        (
            policies,
            (
                "get_domain_registry",
                "get_domain_route_policy",
                "get_role_policy",
                "get_approval_policy",
            ),
        ),
        (idempotency, ("get_result",)),
        (refs, ("get", "get_history_entry")),
        (harness.runs, ("verify_prepared_terminal_producer_authority",)),
        (harness.audit, ("prepare_batch",)),
    ):
        for method_name in method_names:
            monkeypatch.setattr(target, method_name, _unexpected_read)

    result = harness.service.apply_preflighted_terminal_draft_in_transaction(
        preflighted=preflighted,
        context=context,
        capabilities=harness.capabilities,
    )

    assert result.approval_item == expected_item
    assert result.subject_head == harness.state.heads["patch-series:1"]
    assert harness.state.audit == []
    with pytest.raises(IntegrityViolation, match="reused"):
        harness.service.apply_preflighted_terminal_draft_in_transaction(
            preflighted=preflighted,
            context=context,
            capabilities=harness.capabilities,
        )


def test_publish_patch_draft_requires_ref_authority() -> None:
    harness = _harness()
    harness.capabilities.refs = None
    prepared = _draft(harness)
    before = copy.deepcopy(harness.state)

    with pytest.raises(IntegrityViolation, match="refs"):
        harness.service.publish_draft(prepared=prepared, context=_context())

    assert harness.state == before


@pytest.mark.parametrize(
    "actual_ref",
    [None, RefValue(artifact_id="artifact:concurrent", revision=2)],
)
def test_publish_initial_patch_draft_rejects_ref_drift_before_any_write(
    actual_ref: RefValue | None,
) -> None:
    harness = _harness()
    prepared = _draft(harness)
    if actual_ref is None:
        harness.state.refs.pop("content/head")
    else:
        harness.state.refs["content/head"] = actual_ref
    before = copy.deepcopy(harness.state)

    with pytest.raises(Conflict, match="draft ref precondition"):
        harness.service.publish_draft(prepared=prepared, context=_context())

    assert harness.state == before
    assert harness.uow.rollbacks == 1


def test_publish_draft_replay_survives_later_ref_movement() -> None:
    harness = _harness()
    prepared, first = _publish(harness)
    harness.state.refs["content/head"] = RefValue(
        artifact_id="artifact:later",
        revision=2,
    )

    replay = harness.service.publish_draft(prepared=prepared, context=_context())

    assert replay == first
    assert [action for action, _ in harness.state.audit] == ["approval.draft_published"]


@pytest.mark.parametrize("drift", ["current", "history_missing", "history_target"])
def test_publish_rollback_draft_rejects_current_or_history_drift(drift: str) -> None:
    harness = _harness()
    prepared, request = _rollback_draft(harness)
    if drift == "current":
        harness.state.refs[request.ref_name] = RefValue(
            artifact_id="artifact:later",
            revision=request.expected_current_ref.revision + 1,
        )
    elif drift == "history_missing":
        harness.state.ref_history.pop((request.ref_name, request.target_history_revision))
    else:
        harness.state.ref_history[(request.ref_name, request.target_history_revision)] = RefValue(
            artifact_id="artifact:other",
            revision=request.target_history_revision,
        )
    before = copy.deepcopy(harness.state)

    with pytest.raises(Conflict):
        harness.service.publish_draft(prepared=prepared, context=_context())

    assert harness.state == before
    assert harness.uow.rollbacks == 1


@pytest.mark.parametrize(
    "actual_ref",
    [
        RefValue(artifact_id="artifact:concurrent", revision=1),
        RefValue(artifact_id="artifact:base", revision=2),
    ],
)
def test_publish_rebased_draft_rejects_ref_drift_before_any_write(
    actual_ref: RefValue,
) -> None:
    harness = _harness()
    first, first_result = _publish(harness)
    second = _draft(
        harness,
        revision=2,
        supersedes_artifact_id=first.subject_artifact.artifact_id,
        supersedes_approval_id=first.approval_item.approval_id,
        expected_head=first_result.subject_head,
    )
    expected_ref = RefValue(artifact_id="artifact:base", revision=1)
    harness.state.refs["content/head"] = actual_ref
    before = copy.deepcopy(harness.state)

    with pytest.raises(Conflict, match="ref precondition"):
        harness.service.publish_rebased_draft(
            prepared=second,
            context=_context(key="rebase:2", request_hash="7" * 64),
            expected_ref=expected_ref,
        )

    assert harness.state == before
    assert harness.uow.rollbacks == 1


def test_publish_rebased_draft_checks_ref_and_supersedes_in_one_uow() -> None:
    harness = _harness()
    first, first_result = _publish(harness)
    expected_ref = RefValue(artifact_id="artifact:base", revision=1)
    harness.state.refs["content/head"] = expected_ref
    second = _draft(
        harness,
        revision=2,
        supersedes_artifact_id=first.subject_artifact.artifact_id,
        supersedes_approval_id=first.approval_item.approval_id,
        expected_head=first_result.subject_head,
    )
    context = _context(key="rebase:2", request_hash="7" * 64)

    result = harness.service.publish_rebased_draft(
        prepared=second,
        context=context,
        expected_ref=expected_ref,
    )

    assert harness.state.approvals[first.approval_item.approval_id].status == "superseded"
    assert result.approval_item == second.approval_item
    assert harness.state.heads[second.approval_item.subject_series_id] == result.subject_head
    assert [action for action, _ in harness.state.audit][-2:] == [
        "approval.superseded",
        "approval.draft_published",
    ]
    assert (
        context.idempotency_scope,
        "approval.publish_rebased_draft",
        context.idempotency_key,
    ) in harness.state.idempotency
    assert (
        context.idempotency_scope,
        "approval.publish_draft",
        context.idempotency_key,
    ) not in harness.state.idempotency


def test_publish_rebased_draft_participates_in_callers_transaction_without_nesting() -> None:
    harness = _harness()
    first, first_result = _publish(harness)
    expected_ref = RefValue(artifact_id="artifact:base", revision=1)
    harness.state.refs["content/head"] = expected_ref
    second = _draft(
        harness,
        revision=2,
        supersedes_artifact_id=first.subject_artifact.artifact_id,
        supersedes_approval_id=first.approval_item.approval_id,
        expected_head=first_result.subject_head,
    )

    assert harness.uow.begins == 1
    with harness.uow.begin() as transaction:
        result = harness.service.publish_rebased_draft_in_transaction(
            transaction=transaction,
            prepared=second,
            context=_context(key="rebase:outer", request_hash="6" * 64),
            expected_ref=expected_ref,
        )

        assert harness.uow.begins == 2
        assert harness.uow.commits == 1

    assert harness.uow.commits == 2
    assert result.approval_item == second.approval_item
    assert harness.state.heads[second.approval_item.subject_series_id] == result.subject_head


@pytest.mark.parametrize("preview_count", [0, 2])
def test_prepared_patch_requires_exactly_one_preview(preview_count: int) -> None:
    harness = _harness()
    prepared = _draft(harness)
    previews = list(prepared.companion_artifacts[:preview_count])
    if preview_count == 2:
        previews.append(
            _artifact(
                "ir_snapshot",
                "a",
                prepared.subject_artifact.artifact_id,
                ir_snapshot_id="snapshot:other-preview",
            )
        )
    artifacts = (prepared.subject_artifact, *previews)

    with pytest.raises(ValidationError, match="exactly one preview"):
        PreparedDraft(
            subject_artifact=prepared.subject_artifact,
            companion_artifacts=tuple(previews),
            object_bindings=tuple(
                PreparedObjectBinding(
                    object_ref=artifact.object_ref,
                    location=_location(artifact),
                    expected_revision=None,
                )
                for artifact in artifacts
            ),
            approval_item=prepared.approval_item,
            expected_subject_head=None,
        )


def test_publish_patch_rejects_preview_version_tuple_mismatch() -> None:
    harness = _harness()
    prepared = _draft(harness, preview_snapshot_id="snapshot:wrong-preview")

    with pytest.raises(IntegrityViolation, match="VersionTuple"):
        harness.service.publish_draft(prepared=prepared, context=_context())

    assert harness.state.artifacts == {}
    assert harness.state.heads == {}


def test_publish_rejects_subject_payload_revision_mismatch() -> None:
    harness = _harness()
    prepared = _draft(harness)
    subject_id = prepared.subject_artifact.artifact_id
    harness.subjects.facts[subject_id] = harness.subjects.facts[subject_id].model_copy(
        update={"subject_revision": 2}
    )

    with pytest.raises(IntegrityViolation, match="payload revision"):
        harness.service.publish_draft(prepared=prepared, context=_context())

    assert harness.state.artifacts == {}
    assert harness.state.heads == {}


def test_publish_rollback_binds_the_parsed_request_exactly() -> None:
    harness = _harness()
    prepared, _ = _rollback_draft(harness)

    result = harness.service.publish_draft(prepared=prepared, context=_context())

    assert result.approval_item == prepared.approval_item
    assert result.subject_head.current_subject_artifact_id == (
        prepared.subject_artifact.artifact_id
    )


@pytest.mark.parametrize(
    "mismatch",
    ["ref_name", "expected_current_ref", "target_artifact_id", "rollback_profile"],
)
def test_publish_rollback_rejects_request_binding_mismatch(mismatch: str) -> None:
    harness = _harness()
    prepared, request = _rollback_draft(harness)
    updates: dict[str, object]
    if mismatch == "ref_name":
        updates = {"ref_name": "other/head"}
    elif mismatch == "expected_current_ref":
        updates = {
            "expected_current_ref": request.expected_current_ref.model_copy(
                update={"revision": request.expected_current_ref.revision + 1}
            )
        }
    elif mismatch == "target_artifact_id":
        updates = {"target_artifact_id": "artifact:other-target"}
    else:
        updates = {
            "rollback_profile_binding": request.rollback_profile_binding.model_copy(
                update={"profile_payload_hash": "f" * 64}
            )
        }
    subject_id = prepared.subject_artifact.artifact_id
    harness.subjects.facts[subject_id] = harness.subjects.facts[subject_id].model_copy(
        update={"rollback_request": request.model_copy(update=updates)}
    )

    with pytest.raises(IntegrityViolation, match="rollback request"):
        harness.service.publish_draft(prepared=prepared, context=_context())

    assert prepared.approval_item.approval_id not in harness.state.approvals
    assert prepared.approval_item.subject_series_id not in harness.state.heads


@pytest.mark.parametrize("membership_registered", [False, True])
def test_agent_patch_publish_requires_exact_producer_membership(
    membership_registered: bool,
) -> None:
    harness = _harness()
    prepared = _draft(harness)
    subject_id = prepared.subject_artifact.artifact_id
    harness.subjects.facts[subject_id] = DraftSubjectFacts(
        subject_kind="patch",
        subject_revision=prepared.approval_item.subject_revision,
        produced_by="agent",
        producer_run_id="run:producer",
        supersedes_artifact_id=None,
        target_artifact_id=None,
        target_snapshot_id="snapshot:preview",
    )
    if membership_registered:
        harness.runs.produced.add(("run:producer", subject_id, "human:maker"))
    context = ApprovalCommandContext(
        actor=AuditActor(principal_id="service:worker", principal_kind="service"),
        initiated_by=AuditActor(
            principal_id="human:maker",
            principal_kind="human",
        ),
        request_id="agent-publish",
        run_id="run:producer",
        idempotency_scope="run:producer",
        idempotency_key="agent-publish",
        request_hash="c" * 64,
    )

    if not membership_registered:
        with pytest.raises(IntegrityViolation, match="producer membership"):
            harness.service.publish_draft(prepared=prepared, context=context)
        assert harness.state.heads == {}
        assert harness.state.artifacts == {}
        return

    result = harness.service.publish_draft(prepared=prepared, context=context)
    assert result.subject_head.current_subject_artifact_id == subject_id


def test_terminal_agent_draft_direct_commit_keeps_its_own_single_audit_batch() -> None:
    """Task 9's adapter delegates to the real Task-7 authority without nesting."""

    harness = _harness()
    prepared = _draft(harness)
    subject_id = prepared.subject_artifact.artifact_id
    preview = prepared.companion_artifacts[0]
    harness.subjects.facts[subject_id] = DraftSubjectFacts(
        subject_kind="patch",
        subject_revision=1,
        produced_by="agent",
        producer_run_id="run:producer",
        supersedes_artifact_id=None,
        target_artifact_id=None,
        target_snapshot_id="snapshot:preview",
    )
    params = GenerationProposePayloadV1(
        base_snapshot_artifact_id="artifact:base",
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal",
            expected_payload_hash="a" * 64,
        ),
        domain_scope=DomainScope(domain_ids=("economy",)),
        target=RefReadBindingV1(
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=1),
        ),
        generation_policy=ProfileRefV1(profile_id="generation.default", version=1),
        candidate_export_profiles=(),
    )
    run = build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="generation.propose", version=1),
        run_id="run:producer",
    ).model_copy(update={"initiated_by": prepared.approval_item.proposer})
    harness.runs.produced.add(
        ("run:producer", subject_id, prepared.approval_item.proposer.principal_id)
    )
    definition = build_builtin_registry().get_run_kind(run.kind)
    assert definition is not None
    policy = next(
        item for item in definition.outcome_policies if item.policy_id == "generation-gate-pass"
    )
    empty_by_rule = {rule.rule_id: () for rule in policy.artifact_rules}
    artifacts_by_rule = {
        **empty_by_rule,
        "primary": (prepared.subject_artifact,),
        "preview": (preview,),
    }
    payloads_by_rule = {
        **empty_by_rule,
        "primary": (
            harness.subjects.patches[subject_id]
            .model_copy(update={"produced_by": "agent", "producer_run_id": run.run_id})
            .model_dump(mode="json"),
        ),
        "preview": ({"snapshot_id": "snapshot:preview"},),
    }
    request = AgentDraftWorkflowRequest(
        effect_key="create_patch_subject_head_and_draft@1",
        run=run,
        policy=policy,
        initiated_by=run.initiated_by,
        executed_by=WORKER,
        subject_artifact_id=subject_id,
        artifacts_by_rule=artifacts_by_rule,
        artifact_ids_by_rule={
            key: tuple(artifact.artifact_id for artifact in artifacts)
            for key, artifacts in artifacts_by_rule.items()
        },
        payloads_by_rule=payloads_by_rule,
        expected_subject_head_revision=None,
        expected_workflow_revision=None,
        expected_current_approval=None,
        expected_current_subject_head=None,
        occurred_at=NOW,
    )

    class _Assembler:
        def prepare(self, actual: AgentDraftWorkflowRequest) -> PreparedDraft:
            assert actual is request
            return prepared

        def prepare_terminal(
            self,
            actual: AgentDraftWorkflowRequest,
        ) -> PreparedTerminalDraft:
            assert actual is request
            return PreparedTerminalDraft.seal(
                subject_artifact=prepared.subject_artifact,
                companion_artifacts=prepared.companion_artifacts,
                subject_facts=harness.subjects.facts[subject_id],
                retained_parent_ids=(),
                approval_item=prepared.approval_item,
                expected_subject_head=prepared.expected_subject_head,
                expected_previous_workflow_revision=(prepared.expected_previous_workflow_revision),
            )

    port = ApprovalCommandAgentDraftWorkflowPort(
        commands=harness.service,
        capabilities=harness.capabilities,
        assembler=_Assembler(),
    )
    with harness.uow.begin():
        terminal_prepared = port.prepare_agent_draft(request)
        result = port.commit_prepared_agent_draft(
            prepared=terminal_prepared,
            request=request,
        )

    assert result.approval_item == prepared.approval_item
    assert harness.uow.begins == 1  # only the caller-owned terminal UoW
    assert harness.state.heads[prepared.approval_item.subject_series_id] == result.subject_head
    assert [action for action, _ in harness.state.audit] == ["approval.draft_published"]


def test_terminal_agent_draft_adapter_failure_rolls_back_callers_uow() -> None:
    harness = _harness()
    prepared = _draft(harness)

    class _Assembler:
        def prepare(self, request: AgentDraftWorkflowRequest) -> PreparedDraft:
            return prepared

    # The malformed request is rejected before the shared core mutates anything;
    # the outer terminal UoW remains the sole rollback owner.
    params = GenerationProposePayloadV1(
        base_snapshot_artifact_id="artifact:base",
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal", expected_payload_hash="a" * 64
        ),
        domain_scope=DomainScope(domain_ids=("economy",)),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
        generation_policy=ProfileRefV1(profile_id="generation.default", version=1),
        candidate_export_profiles=(),
    )
    run = build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="generation.propose", version=1),
        run_id="run:producer",
    ).model_copy(update={"initiated_by": prepared.approval_item.proposer})
    definition = build_builtin_registry().get_run_kind(run.kind)
    assert definition is not None
    policy = next(
        item for item in definition.outcome_policies if item.policy_id == "generation-gate-pass"
    )
    request = AgentDraftWorkflowRequest(
        effect_key="create_patch_subject_head_and_draft@1",
        run=run,
        policy=policy,
        initiated_by=run.initiated_by,
        executed_by=WORKER,
        subject_artifact_id=prepared.subject_artifact.artifact_id,
        artifacts_by_rule={"primary": (prepared.subject_artifact,)},
        artifact_ids_by_rule={"primary": (prepared.subject_artifact.artifact_id,)},
        payloads_by_rule={"primary": ({},)},
        expected_subject_head_revision=None,
        expected_workflow_revision=None,
        expected_current_approval=None,
        expected_current_subject_head=None,
        occurred_at=NOW,
    )
    port = ApprovalCommandAgentDraftWorkflowPort(
        commands=harness.service,
        capabilities=harness.capabilities,
        assembler=_Assembler(),
    )
    before = copy.deepcopy(harness.state)
    with pytest.raises(IntegrityViolation, match="preview/config"):
        with harness.uow.begin():
            port.publish_agent_draft(request)
    assert harness.state == before
    assert harness.uow.begins == 1
    assert harness.uow.rollbacks == 1


def test_service_proposer_cannot_publish_a_human_authored_revision() -> None:
    harness = _harness()
    prepared = _draft(harness)
    service_actor = AuditActor(
        principal_id="service:publisher",
        principal_kind="service",
    )
    service_item = _replace_item(prepared.approval_item, proposer=service_actor)
    service_prepared = PreparedDraft.model_validate(
        {
            **prepared.model_dump(mode="python"),
            "approval_item": service_item,
        }
    )
    context = ApprovalCommandContext(
        actor=service_actor,
        request_id="service-human-publish",
        idempotency_scope="service:publisher",
        idempotency_key="service-human-publish",
        request_hash="e" * 64,
    )

    with pytest.raises(IntegrityViolation, match="direct publication"):
        harness.service.publish_draft(prepared=service_prepared, context=context)
    assert harness.state.artifacts == {}
    assert harness.state.heads == {}


def test_worker_cannot_publish_a_synchronous_human_draft_on_behalf_of_the_author() -> None:
    harness = _harness()
    prepared = _draft(harness)
    context = ApprovalCommandContext(
        actor=AuditActor(
            principal_id="service:publisher",
            principal_kind="service",
        ),
        initiated_by=prepared.approval_item.proposer,
        request_id="worker-human-publish",
        idempotency_scope="run:worker-human-publish",
        idempotency_key="worker-human-publish",
        request_hash="d" * 64,
    )

    with pytest.raises(IntegrityViolation, match="direct publication"):
        harness.service.publish_draft(prepared=prepared, context=context)
    assert harness.state.artifacts == {}
    assert harness.state.heads == {}


def test_human_cannot_directly_publish_an_agent_produced_draft() -> None:
    harness = _harness()
    prepared = _draft(harness)
    subject_id = prepared.subject_artifact.artifact_id
    harness.subjects.facts[subject_id] = DraftSubjectFacts(
        subject_kind="patch",
        subject_revision=prepared.approval_item.subject_revision,
        produced_by="agent",
        producer_run_id="run:producer",
        supersedes_artifact_id=None,
        target_artifact_id=None,
        target_snapshot_id="snapshot:preview",
    )
    harness.runs.produced.add(("run:producer", subject_id, "human:maker"))

    with pytest.raises(IntegrityViolation, match="service/system worker"):
        harness.service.publish_draft(prepared=prepared, context=_context())
    assert harness.state.artifacts == {}
    assert harness.state.heads == {}


def test_agent_publication_audit_must_bind_the_exact_producer_run() -> None:
    harness = _harness()
    prepared = _draft(harness)
    subject_id = prepared.subject_artifact.artifact_id
    harness.subjects.facts[subject_id] = DraftSubjectFacts(
        subject_kind="patch",
        subject_revision=prepared.approval_item.subject_revision,
        produced_by="agent",
        producer_run_id="run:producer",
        supersedes_artifact_id=None,
        target_artifact_id=None,
        target_snapshot_id="snapshot:preview",
    )
    harness.runs.produced.add(("run:producer", subject_id, "human:maker"))
    context = ApprovalCommandContext(
        actor=AuditActor(principal_id="service:worker", principal_kind="service"),
        initiated_by=prepared.approval_item.proposer,
        request_id="agent-wrong-run",
        run_id="run:other",
        idempotency_scope="run:producer",
        idempotency_key="agent-wrong-run",
        request_hash="1" * 64,
    )

    with pytest.raises(IntegrityViolation, match="producer Run"):
        harness.service.publish_draft(prepared=prepared, context=context)
    assert harness.state.artifacts == {}
    assert harness.state.heads == {}


@pytest.mark.parametrize("missing", ["subjects", "lineage"])
def test_publish_draft_fails_closed_without_required_verifier(missing: str) -> None:
    harness = _harness()
    prepared = _draft(harness)
    setattr(harness.capabilities, missing, None)

    with pytest.raises(IntegrityViolation, match=missing):
        harness.service.publish_draft(prepared=prepared, context=_context())
    assert harness.state.artifacts == {}


def test_superseding_draft_cancels_validation_and_does_not_inherit_state() -> None:
    harness = _harness()
    first, first_result = _publish(harness)
    old = _replace_item(
        first.approval_item,
        status="validating",
        workflow_revision=2,
        active_validation_run_id="run:old-validation",
    )
    harness.state.approvals[old.approval_id] = old
    second = _draft(
        harness,
        revision=2,
        supersedes_artifact_id=first.subject_artifact.artifact_id,
        supersedes_approval_id=old.approval_id,
        expected_head=first_result.subject_head,
    )

    result = harness.service.publish_draft(
        prepared=second,
        context=_context(key="request:2", request_hash="7" * 64),
    )

    superseded = harness.state.approvals[old.approval_id]
    assert superseded.status == "superseded"
    assert superseded.workflow_revision == 3
    assert superseded.active_validation_run_id is None
    assert harness.state.cancellations == ["run:old-validation"]
    assert result.approval_item.evidence_set_artifact_id is None
    assert result.approval_item.decisions == ()
    assert result.subject_head.revision == 2
    assert [action for action, _ in harness.state.audit][-2:] == [
        "approval.superseded",
        "approval.draft_published",
    ]


def test_start_validation_creates_run_and_item_transition_in_one_uow() -> None:
    harness = _harness()
    prepared, published = _publish(harness)
    start = PreparedValidationStart(
        run_id="run:validation-1",
        approval_id=published.approval_item.approval_id,
        subject_artifact_id=prepared.subject_artifact.artifact_id,
        subject_digest=prepared.subject_artifact.payload_hash,
        expected_workflow_revision=1,
    )

    result = harness.service.start_validation(
        prepared=start,
        context=_context(key="validate:1", request_hash="6" * 64),
    )

    assert isinstance(result, ValidationStartResult)
    assert result.run_id == "run:validation-1"
    assert result.approval_item.status == "validating"
    assert result.approval_item.active_validation_run_id == result.run_id
    assert result.approval_item.workflow_revision == 2
    assert harness.state.started_runs == [result.run_id]
    assert harness.state.audit[-1][0] == "approval.validation_started"

    progressed = _replace_item(
        result.approval_item,
        status="draft",
        workflow_revision=3,
        active_validation_run_id=None,
    )
    harness.state.approvals[progressed.approval_id] = progressed
    assert (
        harness.service.start_validation(
            prepared=start,
            context=_context(key="validate:1", request_hash="6" * 64),
        )
        == result
    )

    capabilities = harness.capabilities
    capabilities.runs = None
    with pytest.raises(IntegrityViolation, match="runs"):
        harness.service.start_validation(
            prepared=start.model_copy(update={"run_id": "run:other"}),
            context=_context(key="validate:2", request_hash="5" * 64),
        )


def test_malformed_validation_start_replay_fails_as_integrity_violation() -> None:
    harness = _harness()
    prepared, published = _publish(harness)
    start = PreparedValidationStart(
        run_id="run:validation-1",
        approval_id=published.approval_item.approval_id,
        subject_artifact_id=prepared.subject_artifact.artifact_id,
        subject_digest=prepared.subject_artifact.payload_hash,
        expected_workflow_revision=1,
    )
    context = _context(key="validate:malformed", request_hash="d" * 64)
    harness.service.start_validation(prepared=start, context=context)
    retained_hash, _ = harness.state.idempotency[
        (
            context.idempotency_scope,
            "approval.start_validation",
            context.idempotency_key,
        )
    ]
    harness.state.idempotency[
        (
            context.idempotency_scope,
            "approval.start_validation",
            context.idempotency_key,
        )
    ] = (retained_hash, {"approval_item": {}})

    with pytest.raises(IntegrityViolation, match="malformed"):
        harness.service.start_validation(prepared=start, context=context)


def _validated(harness: _Harness) -> tuple[PreparedDraft, ApprovalItem]:
    prepared, published = _publish(harness)
    evidence = _artifact("validation_evidence", "5", prepared.subject_artifact.artifact_id)
    regression = _artifact("regression_evidence", "6", prepared.subject_artifact.artifact_id)
    for artifact in (evidence, regression):
        harness.state.artifacts[artifact.artifact_id] = artifact
    item = _replace_item(
        published.approval_item,
        status="validated",
        workflow_revision=2,
        evidence_set_artifact_id=evidence.artifact_id,
        regression_evidence_artifact_ids=(regression.artifact_id,),
    )
    harness.state.approvals[item.approval_id] = item
    return prepared, item


def _with_auto_apply_proof(item: ApprovalItem) -> ApprovalItem:
    assert isinstance(item.target_binding, PatchTargetBindingV1)
    assert item.evidence_set_artifact_id is not None
    policy = AutoApplyPolicyRefV1(
        registry=AutoApplyPolicyRegistryRefV1(
            registry_version="auto-apply@1",
            registry_digest="7" * 64,
        ),
        policy_id="safe-structural-patch",
        policy_version="1",
        policy_digest="8" * 64,
    )
    return _replace_item(
        item,
        auto_apply_proof=AutoApplyProofBindingV1(
            proof_artifact_id="artifact:auto-proof:1",
            policy=policy,
            subject_digest=item.subject_digest,
            target_digest=item.target_binding.target_digest,
            expected_ref=item.target_binding.expected_ref,
            validation_evidence_artifact_id=item.evidence_set_artifact_id,
        ),
    )


def test_submit_revalidates_current_head_human_subject_evidence_and_policies() -> None:
    harness = _harness()
    _, item = _validated(harness)
    harness.capabilities.auto_apply = None

    submitted = harness.service.submit_for_approval(
        approval_id=item.approval_id,
        expected_workflow_revision=2,
        context=_context(key="submit:1", request_hash="4" * 64),
    )

    assert submitted.status == "pending_approval"
    assert submitted.workflow_revision == 3
    assert submitted.submitted_at == NOW
    assert harness.state.audit[-1][0] == "approval.submitted"


def test_submit_with_auto_proof_requires_guard_and_enters_eligible_state() -> None:
    harness = _harness()
    _, item = _validated(harness)
    item = _with_auto_apply_proof(item)
    harness.state.approvals[item.approval_id] = item
    context = _context(key="submit:auto", request_hash="1" * 64)

    submitted = harness.service.submit_for_approval(
        approval_id=item.approval_id,
        expected_workflow_revision=item.workflow_revision,
        context=context,
    )

    assert submitted.status == "auto_apply_eligible"
    assert harness.auto_apply.calls == [item]

    harness.capabilities.auto_apply = None
    assert (
        harness.service.submit_for_approval(
            approval_id=item.approval_id,
            expected_workflow_revision=item.workflow_revision,
            context=context,
        )
        == submitted
    )
    assert harness.auto_apply.calls == [item]


@pytest.mark.parametrize("missing", [False, True])
def test_submit_with_auto_proof_fails_closed_without_valid_guard(missing: bool) -> None:
    harness = _harness()
    _, item = _validated(harness)
    item = _with_auto_apply_proof(item)
    harness.state.approvals[item.approval_id] = item
    if missing:
        harness.capabilities.auto_apply = None
    else:
        harness.auto_apply.fail = True
    context = _context(key="submit:auto:rejected", request_hash="2" * 64)

    with pytest.raises(IntegrityViolation):
        harness.service.submit_for_approval(
            approval_id=item.approval_id,
            expected_workflow_revision=item.workflow_revision,
            context=context,
        )

    assert harness.state.approvals[item.approval_id] == item
    assert not harness.state.audit or harness.state.audit[-1][0] != "approval.submitted"
    assert (
        context.idempotency_scope,
        "approval.submit",
        context.idempotency_key,
    ) not in harness.state.idempotency


def test_submit_fails_closed_without_evidence_but_allows_agent_patch_revision() -> None:
    harness = _harness()
    prepared, item = _validated(harness)
    harness.capabilities.evidence = None
    with pytest.raises(IntegrityViolation, match="evidence"):
        harness.service.submit_for_approval(
            approval_id=item.approval_id,
            expected_workflow_revision=2,
            context=_context(key="submit:missing", request_hash="3" * 64),
        )

    harness.capabilities.evidence = harness.evidence
    harness.subjects.facts[prepared.subject_artifact.artifact_id] = DraftSubjectFacts(
        subject_kind="patch",
        subject_revision=item.subject_revision,
        produced_by="agent",
        producer_run_id="run:agent",
        supersedes_artifact_id=None,
        target_artifact_id=None,
        target_snapshot_id="snapshot:preview",
    )
    harness.runs.produced.add(
        ("run:agent", prepared.subject_artifact.artifact_id, item.proposer.principal_id)
    )
    submitted = harness.service.submit_for_approval(
        approval_id=item.approval_id,
        expected_workflow_revision=2,
        context=_context(key="submit:agent", request_hash="2" * 64),
    )
    assert submitted.status == "pending_approval"


def _agent_constraint_item(
    harness: _Harness,
    *,
    validated: bool,
    produced_by: Literal["agent", "human"] = "agent",
    revision: int = 1,
) -> ApprovalItem:
    prior = _artifact("constraint_proposal", "b") if revision > 1 else None
    subject = _artifact(
        "constraint_proposal",
        "c",
        *((prior.artifact_id,) if prior is not None else ()),
    )
    scope = DomainScope(domain_ids=("economy",))
    requirements = build_approval_requirements(
        registry=harness.registry,
        policy=harness.route,
        subject_kind="constraint_proposal",
        domain_scope=scope,
    )
    target = _artifact(
        "constraint_snapshot",
        "d",
        subject.artifact_id,
        ir_snapshot_id=None,
        constraint_snapshot_id="constraint-snapshot:1",
    )
    evidence = _artifact("validation_evidence", "e", subject.artifact_id)
    item = ApprovalItem(
        approval_id=f"approval:constraint:{revision}",
        subject_series_id="constraint-series:1",
        subject_revision=revision,
        subject_kind="constraint_proposal",
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        status="validated" if validated else "draft",
        workflow_revision=2 if validated else 1,
        supersedes_approval_id=("approval:constraint:1" if revision > 1 else None),
        proposer=AuditActor(principal_id="human:maker", principal_kind="human"),
        domain_scope=scope,
        domain_registry_ref=_domain_ref(harness.registry),
        route_policy={
            "route_version": harness.route.route_version,
            "route_digest": harness.route.route_digest,
            "domain_registry_ref": harness.route.domain_registry_ref,
        },
        role_policy_version=harness.roles.policy_version,
        role_policy_digest=harness.roles.policy_digest,
        approval_policy=ApprovalPolicyRefV1(
            policy_version=harness.approval_policy.policy_version,
            policy_digest=harness.approval_policy.policy_digest,
        ),
        requirements=requirements,
        decisions=(),
        regression_evidence_artifact_ids=(),
        evidence_set_artifact_id=evidence.artifact_id if validated else None,
        target_binding=(
            ConstraintTargetBindingV1(
                target_artifact_id=target.artifact_id,
                target_snapshot_id="constraint-snapshot:1",
                target_digest=target.payload_hash,
                ref_name="constraints/head",
                expected_ref=None,
            )
            if validated
            else None
        ),
        created_at=NOW,
    )
    head = SubjectHead(
        subject_series_id=item.subject_series_id,
        current_subject_artifact_id=item.subject_artifact_id,
        current_approval_id=item.approval_id,
        revision=revision,
    )
    harness.state.approvals[item.approval_id] = item
    harness.state.heads[item.subject_series_id] = head
    if prior is not None:
        harness.state.artifacts[prior.artifact_id] = prior
    harness.state.artifacts[subject.artifact_id] = subject
    if validated:
        harness.state.artifacts[target.artifact_id] = target
        harness.state.artifacts[evidence.artifact_id] = evidence
    harness.subjects.facts[subject.artifact_id] = DraftSubjectFacts(
        subject_kind="constraint_proposal",
        subject_revision=item.subject_revision,
        produced_by=produced_by,
        producer_run_id=("run:constraint-agent" if produced_by == "agent" else None),
        supersedes_artifact_id=(prior.artifact_id if prior is not None else None),
        target_artifact_id=None,
        target_snapshot_id=None,
    )
    return item


def test_superseding_revision_cannot_change_subject_kind() -> None:
    harness = _harness()
    old = _agent_constraint_item(harness, validated=False)
    old_head = harness.state.heads[old.subject_series_id]
    patch = _draft(
        harness,
        revision=2,
        supersedes_artifact_id=old.subject_artifact_id,
        supersedes_approval_id=old.approval_id,
        expected_head=old_head,
    )
    replacement = _replace_item(
        patch.approval_item,
        subject_series_id=old.subject_series_id,
    )
    prepared = PreparedDraft(
        subject_artifact=patch.subject_artifact,
        companion_artifacts=patch.companion_artifacts,
        object_bindings=patch.object_bindings,
        approval_item=replacement,
        expected_subject_head=old_head,
    )

    with pytest.raises(IntegrityViolation, match="current revision"):
        harness.service.publish_draft(
            prepared=prepared,
            context=_context(key="cross-kind", request_hash="e" * 64),
        )

    assert harness.state.heads[old.subject_series_id] == old_head
    assert harness.state.approvals[old.approval_id] == old


def test_agent_constraint_requires_human_revision_before_validation_or_submit() -> None:
    harness = _harness()
    draft = _agent_constraint_item(harness, validated=False)
    with pytest.raises(InvalidStateTransition, match="human author revision"):
        harness.service.start_validation(
            prepared=PreparedValidationStart(
                run_id="run:constraint-validation",
                approval_id=draft.approval_id,
                subject_artifact_id=draft.subject_artifact_id,
                subject_digest=draft.subject_digest,
                expected_workflow_revision=draft.workflow_revision,
            ),
            context=_context(key="constraint:start", request_hash="1" * 64),
        )

    validated = _agent_constraint_item(harness, validated=True)
    with pytest.raises(InvalidStateTransition, match="human author revision"):
        harness.service.submit_for_approval(
            approval_id=validated.approval_id,
            expected_workflow_revision=validated.workflow_revision,
            context=_context(key="constraint:submit", request_hash="f" * 64),
        )


@pytest.mark.parametrize("validated", [False, True])
def test_human_constraint_revision_one_cannot_validate_or_submit(
    validated: bool,
) -> None:
    harness = _harness()
    item = _agent_constraint_item(
        harness,
        validated=validated,
        produced_by="human",
    )

    with pytest.raises(InvalidStateTransition, match="human author revision"):
        if validated:
            harness.service.submit_for_approval(
                approval_id=item.approval_id,
                expected_workflow_revision=item.workflow_revision,
                context=_context(key="constraint:human-v1-submit", request_hash="7" * 64),
            )
        else:
            harness.service.start_validation(
                prepared=PreparedValidationStart(
                    run_id="run:constraint-human-v1",
                    approval_id=item.approval_id,
                    subject_artifact_id=item.subject_artifact_id,
                    subject_digest=item.subject_digest,
                    expected_workflow_revision=item.workflow_revision,
                ),
                context=_context(key="constraint:human-v1-start", request_hash="8" * 64),
            )


def test_superseding_human_constraint_revision_can_validate_and_submit() -> None:
    start_harness = _harness()
    draft = _agent_constraint_item(
        start_harness,
        validated=False,
        produced_by="human",
        revision=2,
    )
    started = start_harness.service.start_validation(
        prepared=PreparedValidationStart(
            run_id="run:constraint-human-v2",
            approval_id=draft.approval_id,
            subject_artifact_id=draft.subject_artifact_id,
            subject_digest=draft.subject_digest,
            expected_workflow_revision=draft.workflow_revision,
        ),
        context=_context(key="constraint:human-v2-start", request_hash="9" * 64),
    )
    assert started.approval_item.status == "validating"

    submit_harness = _harness()
    validated = _agent_constraint_item(
        submit_harness,
        validated=True,
        produced_by="human",
        revision=2,
    )
    submitted = submit_harness.service.submit_for_approval(
        approval_id=validated.approval_id,
        expected_workflow_revision=validated.workflow_revision,
        context=_context(key="constraint:human-v2-submit", request_hash="a" * 64),
    )
    assert submitted.status == "pending_approval"


def test_constraint_human_revision_requires_a_human_accountable_proposer() -> None:
    harness = _harness()
    item = _agent_constraint_item(
        harness,
        validated=False,
        produced_by="human",
        revision=2,
    )
    service_actor = AuditActor(
        principal_id="service:constraint-author",
        principal_kind="service",
    )
    service_item = _replace_item(item, proposer=service_actor)
    harness.state.approvals[item.approval_id] = service_item
    harness.subjects.facts[item.subject_artifact_id] = DraftSubjectFacts(
        subject_kind="constraint_proposal",
        subject_revision=item.subject_revision,
        produced_by="human",
        producer_run_id=None,
        supersedes_artifact_id=harness.subjects.facts[
            item.subject_artifact_id
        ].supersedes_artifact_id,
        target_artifact_id=None,
        target_snapshot_id=None,
    )

    with pytest.raises(InvalidStateTransition, match="human author revision"):
        harness.service.start_validation(
            prepared=PreparedValidationStart(
                run_id="run:constraint-service-author",
                approval_id=service_item.approval_id,
                subject_artifact_id=service_item.subject_artifact_id,
                subject_digest=service_item.subject_digest,
                expected_workflow_revision=service_item.workflow_revision,
            ),
            context=_context(key="constraint:service", request_hash="0" * 64),
        )


def _pending_item(harness: _Harness) -> ApprovalItem:
    _, item = _validated(harness)
    return harness.service.submit_for_approval(
        approval_id=item.approval_id,
        expected_workflow_revision=item.workflow_revision,
        context=_context(key="submit:server-decision", request_hash="a" * 64),
    )


def _server_decision_request(item: ApprovalItem) -> ApprovalDecisionRequest:
    return ApprovalDecisionRequest(
        requirement_ids=("route:economy",),
        decision="approve",
        expected_workflow_revision=item.workflow_revision,
        reason_code="reviewed",
        comment="exact evidence reviewed",
    )


def test_decide_current_owns_identity_time_and_id_and_replays_before_generation() -> None:
    harness = _harness()
    pending = _pending_item(harness)
    reviewer = _principal("human:reviewer")
    harness.principals.values[reviewer.id] = reviewer
    request = _server_decision_request(pending)
    context = _context(
        actor=reviewer.id,
        key="decision:server-owned",
        request_hash="b" * 64,
    )
    clock_calls = harness.clock.calls

    approved = harness.service.decide_current(
        approval_id=pending.approval_id,
        request=request,
        context=context,
    )

    assert approved.status == "approved"
    assert approved.workflow_revision == pending.workflow_revision + 1
    assert approved.decisions == (
        ApprovalDecision(
            decision_id="decision:server:1",
            requirement_ids=request.requirement_ids,
            decision=request.decision,
            actor=context.actor,
            expected_workflow_revision=request.expected_workflow_revision,
            reason_code=request.reason_code,
            comment=request.comment,
            occurred_at=NOW,
        ),
    )
    assert harness.decision_ids.calls == 1
    assert harness.clock.calls == clock_calls + 1

    progressed = _replace_item(
        approved,
        status="applied",
        workflow_revision=approved.workflow_revision + 1,
        applied_at=NOW,
    )
    harness.state.approvals[progressed.approval_id] = progressed
    harness.capabilities.principals = None
    harness.capabilities.policies = None
    harness.capabilities.audit = None

    assert (
        harness.service.decide_current(
            approval_id=pending.approval_id,
            request=request,
            context=context,
        )
        == approved
    )
    assert harness.decision_ids.calls == 1
    assert harness.clock.calls == clock_calls + 1

    with pytest.raises(Conflict, match="different request"):
        harness.service.decide_current(
            approval_id=pending.approval_id,
            request=request,
            context=context.model_copy(update={"request_hash": "c" * 64}),
        )
    assert harness.decision_ids.calls == 1
    assert harness.clock.calls == clock_calls + 1


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("decision_id", "decision:client"),
        (
            "actor",
            {"principal_id": "human:attacker", "principal_kind": "human"},
        ),
        ("occurred_at", NOW),
        ("status", "approved"),
        (
            "proposer",
            {"principal_id": "human:attacker", "principal_kind": "human"},
        ),
    ],
)
def test_decision_request_rejects_server_owned_fields(
    field_name: str,
    value: object,
) -> None:
    payload = {
        "requirement_ids": ["route:economy"],
        "decision": "approve",
        "expected_workflow_revision": 3,
        "reason_code": "reviewed",
        field_name: value,
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ApprovalDecisionRequest.model_validate(payload)


def test_decide_current_supports_partial_requirement_approval() -> None:
    harness = _harness(min_approvals=2)
    pending = _pending_item(harness)
    reviewer = _principal("human:reviewer")
    harness.principals.values[reviewer.id] = reviewer

    partial = harness.service.decide_current(
        approval_id=pending.approval_id,
        request=_server_decision_request(pending),
        context=_context(
            actor=reviewer.id,
            key="decision:partial",
            request_hash="d" * 64,
        ),
    )

    assert partial.status == "pending_approval"
    assert partial.workflow_revision == pending.workflow_revision + 1
    assert partial.decisions[0].requirement_ids == ("route:economy",)
    assert harness.state.audit[-1][0] == "approval.partially_approved"


def test_decide_current_revalidates_historical_votes_before_final_approval() -> None:
    harness = _harness(min_approvals=2)
    pending = _pending_item(harness)
    alice = _principal("human:alice")
    bob = _principal("human:bob")
    harness.principals.values.update({alice.id: alice, bob.id: bob})

    partial = harness.service.decide_current(
        approval_id=pending.approval_id,
        request=_server_decision_request(pending),
        context=_context(
            actor=alice.id,
            key="decision:alice",
            request_hash="8" * 64,
        ),
    )
    harness.principals.values[alice.id] = alice.model_copy(
        update={"roles": (), "authz_revision": alice.authz_revision + 1}
    )

    rechecked = harness.service.decide_current(
        approval_id=partial.approval_id,
        request=_server_decision_request(partial),
        context=_context(
            actor=bob.id,
            key="decision:bob",
            request_hash="9" * 64,
        ),
    )

    assert rechecked.status == "pending_approval"
    assert len(rechecked.decisions) == 2
    assert harness.state.audit[-1][0] == "approval.partially_approved"

    harness.principals.values[alice.id] = alice
    confirmed = harness.service.decide_current(
        approval_id=rechecked.approval_id,
        request=_server_decision_request(rechecked),
        context=_context(
            actor=alice.id,
            key="decision:alice-reconfirmed",
            request_hash="a" * 64,
        ),
    )

    assert confirmed.status == "approved"
    assert confirmed.workflow_revision == rechecked.workflow_revision + 1
    assert len(confirmed.decisions) == 3
    assert harness.state.audit[-1][0] == "approval.approved"


@pytest.mark.parametrize(
    "current_identity",
    ["missing", "roles_revoked", "disabled", "nonhuman"],
)
def test_decide_current_reloads_current_human_roles_fail_closed(
    current_identity: str,
) -> None:
    harness = _harness()
    pending = _pending_item(harness)
    reviewer = _principal("human:reviewer")
    if current_identity == "roles_revoked":
        reviewer = reviewer.model_copy(update={"roles": ()})
    elif current_identity == "disabled":
        reviewer = reviewer.model_copy(update={"status": "disabled"})
    elif current_identity == "nonhuman":
        reviewer = reviewer.model_copy(update={"kind": "service"})
    if current_identity != "missing":
        harness.principals.values[reviewer.id] = reviewer
    before = harness.state.approvals[pending.approval_id]

    with pytest.raises(Forbidden):
        harness.service.decide_current(
            approval_id=pending.approval_id,
            request=_server_decision_request(pending),
            context=_context(
                actor=reviewer.id,
                key=f"decision:{current_identity}",
                request_hash="e" * 64,
            ),
        )

    assert harness.state.approvals[pending.approval_id] == before
    assert not harness.state.approvals[pending.approval_id].decisions


def test_decide_current_requires_exact_retained_policy_snapshot() -> None:
    harness = _harness()
    pending = _pending_item(harness)
    reviewer = _principal("human:reviewer")
    harness.principals.values[reviewer.id] = reviewer
    policies = harness.capabilities.policies
    assert isinstance(policies, _Policies)
    policies.roles = policies.roles.model_copy(update={"policy_digest": "f" * 64})

    with pytest.raises(IntegrityViolation, match="exact retained governance policy"):
        harness.service.decide_current(
            approval_id=pending.approval_id,
            request=_server_decision_request(pending),
            context=_context(
                actor=reviewer.id,
                key="decision:missing-policy",
                request_hash="f" * 64,
            ),
        )

    assert harness.state.approvals[pending.approval_id] == pending


def test_decide_current_rolls_back_decision_cas_when_audit_fails() -> None:
    harness = _harness()
    pending = _pending_item(harness)
    reviewer = _principal("human:reviewer")
    harness.principals.values[reviewer.id] = reviewer
    context = _context(
        actor=reviewer.id,
        key="decision:audit-failure",
        request_hash="1" * 64,
    )
    harness.audit.fail = True

    with pytest.raises(IntegrityViolation, match="audit unavailable"):
        harness.service.decide_current(
            approval_id=pending.approval_id,
            request=_server_decision_request(pending),
            context=context,
        )

    assert harness.state.approvals[pending.approval_id] == pending
    assert (
        context.idempotency_scope,
        "approval.decide",
        context.idempotency_key,
    ) not in harness.state.idempotency


def test_decide_uses_atomic_repository_primitive_idempotency_and_audit() -> None:
    harness = _harness()
    _, item = _validated(harness)
    pending = harness.service.submit_for_approval(
        approval_id=item.approval_id,
        expected_workflow_revision=2,
        context=_context(key="submit:decision", request_hash="a" * 64),
    )
    reviewer = _principal("human:reviewer")
    decision = ApprovalDecision(
        decision_id="decision:1",
        requirement_ids=("route:economy",),
        decision="approve",
        actor=AuditActor(principal_id=reviewer.id, principal_kind="human"),
        expected_workflow_revision=pending.workflow_revision,
        reason_code="reviewed",
        occurred_at=NOW,
    )
    context = _context(
        actor=reviewer.id,
        key="decision:request-1",
        request_hash="b" * 64,
    )

    approved = harness.service.decide(
        approval_id=pending.approval_id,
        decision=decision,
        principal=reviewer,
        context=context,
    )
    assert approved.status == "approved"
    assert approved.workflow_revision == pending.workflow_revision + 1
    assert harness.state.audit[-1][0] == "approval.approved"
    audit_count = len(harness.state.audit)

    progressed = _replace_item(
        approved,
        status="applied",
        workflow_revision=approved.workflow_revision + 1,
        applied_at=NOW,
    )
    harness.state.approvals[progressed.approval_id] = progressed

    assert (
        harness.service.decide(
            approval_id=pending.approval_id,
            decision=decision,
            principal=reviewer,
            context=context,
        )
        == approved
    )
    assert len(harness.state.audit) == audit_count


def test_patch_state_projection_is_derived_from_item_and_evidence_adapter() -> None:
    harness = _harness()
    prepared, item = _validated(harness)
    view = harness.service.project_patch_state(item.approval_id)

    assert view.patch == harness.subjects.patches[prepared.subject_artifact.artifact_id]
    assert view.validation_status == "passed"
    assert view.regression_status == "passed"
    assert view.approval_status == "validated"
    assert view.workflow_revision == item.workflow_revision


def test_audit_failure_rolls_back_every_authoritative_write() -> None:
    harness = _harness()
    prepared = _draft(harness)
    harness.audit.fail = True

    with pytest.raises(IntegrityViolation, match="audit unavailable"):
        harness.service.publish_draft(prepared=prepared, context=_context())

    assert harness.state.approvals == {}
    assert harness.state.heads == {}
    assert harness.state.artifacts == {}
    assert harness.state.bindings == {}
    assert harness.state.idempotency == {}
    assert harness.uow.rollbacks == 1
