from __future__ import annotations

import copy
import hashlib
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import ResolvedExecutionProfileBindingV1
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
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
    object_key_for_sha256,
)
from gameforge.contracts.storage import RefTransitionV1, RefValue
from gameforge.contracts.workflow import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalPolicyV1,
    ConstraintTargetBindingV1,
    EvidenceRequirement,
    EvidenceSet,
    PatchTargetBindingV1,
    RollbackRequestV1,
    RollbackTargetBindingV1,
    SubjectHead,
    compute_approval_policy_digest,
)
from gameforge.platform.approvals import build_approval_requirements
from gameforge.platform.approvals.apply import (
    ApprovedApplyCapabilities,
    ApprovedApplyRequest,
    ApprovedApplyService,
    VerifiedTargetPayload,
)
from gameforge.platform.approvals.commands import (
    ApprovalCommandContext,
    DraftSubjectFacts,
    EvidenceStateProjection,
)
from gameforge.runtime.clock import FrozenUtcClock


NOW_DT = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-14T12:00:00Z"
HASH_1 = "1" * 64
HASH_2 = "2" * 64


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


def _route(registry: DomainRegistryV1) -> DomainRoutePolicy:
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
                action="approval.decide",
                resource_kind="approval",
                domain_scope=DomainScope(domain_ids=("economy",)),
            ),
            Permission(
                action="apply",
                resource_kind="patch",
                domain_scope=DomainScope(domain_ids=("economy",)),
            ),
            Permission(
                action="publish",
                resource_kind="constraint_proposal",
                domain_scope=DomainScope(domain_ids=("economy",)),
            ),
            Permission(
                action="rollback",
                resource_kind="ref",
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


def principal(principal_id: str, *, active: bool = True) -> Principal:
    roles = ()
    if active:
        roles = (
            RoleAssignmentV1(
                assignment_id=f"assignment:{principal_id}",
                principal_id=principal_id,
                role="numeric_designer",
                scope=DomainScope(domain_ids=("economy",)),
                status="active",
                revision=1,
                granted_at=NOW,
                granted_by=AuditActor(
                    principal_id="human:admin",
                    principal_kind="human",
                ),
            ),
        )
    return Principal(
        id=principal_id,
        kind="human",
        display_name=principal_id,
        status="active" if active else "disabled",
        revision=1,
        credential_epoch=0,
        authz_revision=1,
        roles=roles,
    )


@dataclass(frozen=True)
class StoredArtifact:
    artifact: ArtifactV2
    payload: bytes
    snapshot_id: str | None


def stored_artifact(
    kind: str,
    label: str,
    *,
    parents: tuple[str, ...] = (),
    snapshot_id: str | None = None,
    constraint_snapshot_id: str | None = None,
) -> StoredArtifact:
    payload = f'{{"label":"{label}"}}'.encode()
    digest = hashlib.sha256(payload).hexdigest()
    object_ref = ObjectRef(
        key=object_key_for_sha256(digest),
        sha256=digest,
        size_bytes=len(payload),
    )
    artifact = build_artifact_v2(
        kind=kind,  # type: ignore[arg-type]
        version_tuple=VersionTuple(
            ir_snapshot_id=snapshot_id,
            constraint_snapshot_id=constraint_snapshot_id,
            tool_version="apply-test@1",
        ),
        lineage=parents,
        payload_hash=digest,
        object_ref=object_ref,
    )
    return StoredArtifact(
        artifact=artifact,
        payload=payload,
        snapshot_id=constraint_snapshot_id or snapshot_id,
    )


@dataclass
class ApplyState:
    approvals: dict[str, ApprovalItem] = field(default_factory=dict)
    heads: dict[str, SubjectHead] = field(default_factory=dict)
    artifacts: dict[str, ArtifactV2] = field(default_factory=dict)
    payloads: dict[str, bytes] = field(default_factory=dict)
    snapshot_ids: dict[str, str | None] = field(default_factory=dict)
    facts: dict[str, DraftSubjectFacts] = field(default_factory=dict)
    evidence_sets: dict[str, EvidenceSet] = field(default_factory=dict)
    refs: dict[str, RefValue] = field(default_factory=dict)
    history: dict[tuple[str, int], RefValue] = field(default_factory=dict)
    transitions: dict[str, RefTransitionV1] = field(default_factory=dict)
    principals: dict[str, Principal] = field(default_factory=dict)
    idempotency: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = field(
        default_factory=dict
    )
    audit: list[tuple[str, AuditSubject]] = field(default_factory=list)


class ApprovalRepo:
    def __init__(self, state: ApplyState) -> None:
        self.state = state

    def get(self, approval_id: str) -> ApprovalItem | None:
        return self.state.approvals.get(approval_id)

    def current(self, subject_series_id: str) -> tuple[SubjectHead, ApprovalItem] | None:
        head = self.state.heads.get(subject_series_id)
        if head is None:
            return None
        return head, self.state.approvals[head.current_approval_id]

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


class Policies:
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
        if (version, digest) == (self.roles.policy_version, self.roles.policy_digest):
            return self.roles
        return None

    def get_approval_policy(self, ref: ApprovalPolicyRefV1) -> ApprovalPolicyV1 | None:
        expected = ApprovalPolicyRefV1(
            policy_version=self.approval.policy_version,
            policy_digest=self.approval.policy_digest,
        )
        return self.approval if ref == expected else None


class Artifacts:
    def __init__(self, state: ApplyState) -> None:
        self.state = state

    def get(self, artifact_id: str) -> ArtifactV2 | None:
        return self.state.artifacts.get(artifact_id)


class Refs:
    def __init__(self, state: ApplyState) -> None:
        self.state = state

    def get(self, name: str) -> RefValue | None:
        return self.state.refs.get(name)

    def get_history_entry(self, name: str, revision: int) -> RefValue | None:
        return self.state.history.get((name, revision))

    def compare_and_set(
        self,
        name: str,
        expected: RefValue | None,
        new_artifact_id: str,
    ) -> RefValue:
        if self.state.refs.get(name) != expected:
            raise Conflict("ref CAS")
        next_revision = 1 if expected is None else expected.revision + 1
        replacement = RefValue(artifact_id=new_artifact_id, revision=next_revision)
        self.state.refs[name] = replacement
        self.state.history[(name, next_revision)] = replacement
        return replacement


class Transitions:
    def __init__(self, state: ApplyState) -> None:
        self.state = state

    def get(self, transition_id: str) -> RefTransitionV1 | None:
        return self.state.transitions.get(transition_id)

    def put(self, transition: RefTransitionV1) -> RefTransitionV1:
        retained = self.get(transition.transition_id)
        if retained is not None and retained != transition:
            raise IntegrityViolation("transition collision")
        self.state.transitions[transition.transition_id] = transition
        return transition


class Idempotency:
    def __init__(self, state: ApplyState) -> None:
        self.state = state

    def get_result(
        self, *, scope: str, operation: str, key: str, request_hash: str
    ) -> dict[str, Any] | None:
        retained = self.state.idempotency.get((scope, operation, key))
        if retained is None:
            return None
        retained_hash, response = retained
        if retained_hash != request_hash:
            raise Conflict("different idempotent request")
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
        del resource_kind, resource_id
        retained = self.get_result(
            scope=scope,
            operation=operation,
            key=key,
            request_hash=request_hash,
        )
        if retained is not None:
            return retained
        self.state.idempotency[(scope, operation, key)] = (
            request_hash,
            copy.deepcopy(response),
        )
        return copy.deepcopy(response)


class Audit:
    def __init__(self, state: ApplyState) -> None:
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
        del chain_id, actor, initiated_by, correlation
        if self.fail:
            raise IntegrityViolation("audit unavailable")
        self.state.audit.append((action, subject))
        return object()


class Subjects:
    def __init__(self, state: ApplyState) -> None:
        self.state = state

    def inspect_draft_subject(self, artifact: ArtifactV2) -> DraftSubjectFacts:
        try:
            return self.state.facts[artifact.artifact_id]
        except KeyError as exc:
            raise IntegrityViolation("subject payload unavailable") from exc


class Evidence:
    def __init__(self, state: ApplyState) -> None:
        self.state = state
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
        del item, subject_artifact, target_artifact, evidence_artifact, regression_artifacts
        if self.fail:
            raise IntegrityViolation("evidence mismatch")
        return EvidenceStateProjection(
            validation_status="passed",
            regression_status="passed",
        )

    def load_evidence_set(self, artifact: ArtifactV2) -> EvidenceSet:
        try:
            return self.state.evidence_sets[artifact.artifact_id]
        except KeyError as exc:
            raise IntegrityViolation("EvidenceSet unavailable") from exc


class Targets:
    def __init__(self, state: ApplyState) -> None:
        self.state = state
        self.fail = False

    def read_verified(self, artifact: ArtifactV2) -> VerifiedTargetPayload:
        if self.fail:
            raise IntegrityViolation("target schema unreadable")
        return VerifiedTargetPayload(
            artifact=artifact,
            payload_bytes=self.state.payloads[artifact.artifact_id],
            payload_schema_id=f"{artifact.kind}@test",
            snapshot_id=self.state.snapshot_ids[artifact.artifact_id],
        )


class Principals:
    def __init__(self, state: ApplyState) -> None:
        self.state = state

    def get(self, principal_id: str) -> Principal | None:
        return self.state.principals.get(principal_id)


class AutoApply:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def validate_eligibility(self, *, item: ApprovalItem) -> None:
        del item
        self.calls += 1
        if self.fail:
            raise IntegrityViolation("auto-apply proof rejected")


class RollbackExecution:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def validate(
        self,
        *,
        item: ApprovalItem,
        request: RollbackRequestV1,
        evidence_set: EvidenceSet,
    ) -> None:
        del item, request, evidence_set
        self.calls += 1
        if self.fail:
            raise IntegrityViolation("rollback execution binding rejected")


class UowContext(AbstractContextManager[object]):
    def __init__(self, owner: Uow) -> None:
        self.owner = owner
        self.snapshot: dict[str, Any] | None = None

    def __enter__(self) -> object:
        self.snapshot = copy.deepcopy(self.owner.state.__dict__)
        return object()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        del exc, traceback
        if exc_type is not None:
            assert self.snapshot is not None
            self.owner.state.__dict__.clear()
            self.owner.state.__dict__.update(self.snapshot)
            self.owner.rollbacks += 1
        else:
            self.owner.commits += 1
        return False


class Uow:
    def __init__(self, state: ApplyState) -> None:
        self.state = state
        self.commits = 0
        self.rollbacks = 0

    def begin(self) -> UowContext:
        return UowContext(self)


@dataclass
class Scenario:
    item: ApprovalItem
    subject: ArtifactV2
    target: ArtifactV2
    evidence: ArtifactV2
    expected_ref: RefValue
    rollback_request: RollbackRequestV1 | None = None
    reversed_item: ApprovalItem | None = None


@dataclass
class Harness:
    state: ApplyState
    service: ApprovedApplyService
    uow: Uow
    evidence: Evidence
    targets: Targets
    auto_apply: AutoApply
    rollback_execution: RollbackExecution
    audit: Audit
    scenario: Scenario


def _put_stored(state: ApplyState, value: StoredArtifact) -> ArtifactV2:
    artifact = value.artifact
    state.artifacts[artifact.artifact_id] = artifact
    state.payloads[artifact.artifact_id] = value.payload
    state.snapshot_ids[artifact.artifact_id] = value.snapshot_id
    return artifact


def _approved_item(
    *,
    kind: Literal["patch", "constraint_proposal", "rollback_request"],
    subject: ArtifactV2,
    target_binding: PatchTargetBindingV1 | ConstraintTargetBindingV1 | RollbackTargetBindingV1,
    evidence: ArtifactV2,
    registry: DomainRegistryV1,
    route: DomainRoutePolicy,
    roles: RolePolicy,
    approval_policy: ApprovalPolicyV1,
    approval_id: str,
    series_id: str,
) -> ApprovalItem:
    scope = DomainScope(domain_ids=("economy",))
    requirements = build_approval_requirements(
        registry=registry,
        policy=route,
        subject_kind=kind,
        domain_scope=scope,
    )
    decision = ApprovalDecision(
        decision_id=f"decision:{approval_id}",
        requirement_ids=tuple(item.requirement_id for item in requirements),
        decision="approve",
        actor=AuditActor(principal_id="human:reviewer", principal_kind="human"),
        expected_workflow_revision=4,
        reason_code="review_passed",
        occurred_at=NOW,
    )
    return ApprovalItem(
        approval_id=approval_id,
        subject_series_id=series_id,
        subject_revision=1,
        subject_kind=kind,
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        status="approved",
        workflow_revision=5,
        proposer=AuditActor(principal_id="human:maker", principal_kind="human"),
        domain_scope=scope,
        domain_registry_ref=_domain_ref(registry),
        route_policy={
            "route_version": route.route_version,
            "route_digest": route.route_digest,
            "domain_registry_ref": route.domain_registry_ref,
        },
        role_policy_version=roles.policy_version,
        role_policy_digest=roles.policy_digest,
        approval_policy=ApprovalPolicyRefV1(
            policy_version=approval_policy.policy_version,
            policy_digest=approval_policy.policy_digest,
        ),
        requirements=requirements,
        decisions=(decision,),
        evidence_set_artifact_id=evidence.artifact_id,
        regression_evidence_artifact_ids=(),
        target_binding=target_binding,
        created_at=NOW,
        submitted_at=NOW,
        decided_at=NOW,
    )


def _profile_binding() -> ResolvedExecutionProfileBindingV1:
    return ResolvedExecutionProfileBindingV1(
        field_path="/params/rollback_profile",
        profile={"profile_id": "rollback.default", "version": 1},
        expected_profile_kind="rollback",
        profile_payload_hash=HASH_1,
        catalog_version=1,
        catalog_digest=HASH_2,
    )


def context(*, key: str = "apply:1", request_hash: str = HASH_1) -> ApprovalCommandContext:
    return ApprovalCommandContext(
        actor=AuditActor(principal_id="human:operator", principal_kind="human"),
        request_id=key,
        idempotency_scope="principal:human:operator",
        idempotency_key=key,
        request_hash=request_hash,
    )


def harness(
    kind: Literal["patch", "constraint_proposal", "rollback_request"] = "patch",
    *,
    with_reversed_item: bool = False,
) -> Harness:
    state = ApplyState()
    registry = _registry()
    route = _route(registry)
    roles = _roles(registry)
    approval_policy = _approval_policy()
    state.principals["human:reviewer"] = principal("human:reviewer")
    state.principals["human:operator"] = principal("human:operator")

    base = _put_stored(
        state,
        stored_artifact("ir_snapshot", "base", snapshot_id="snapshot:base"),
    )
    target_kind = "constraint_snapshot" if kind == "constraint_proposal" else "ir_snapshot"
    target = _put_stored(
        state,
        stored_artifact(
            target_kind,
            "target",
            snapshot_id=None if kind == "constraint_proposal" else "snapshot:target",
            constraint_snapshot_id=(
                "constraint-snapshot:target" if kind == "constraint_proposal" else None
            ),
        ),
    )
    current = _put_stored(
        state,
        stored_artifact("ir_snapshot", "current", snapshot_id="snapshot:current"),
    )
    prior_constraint = (
        _put_stored(
            state,
            stored_artifact(
                "constraint_proposal",
                "subject:constraint_proposal:prior",
            ),
        )
        if kind == "constraint_proposal"
        else None
    )
    subject = _put_stored(
        state,
        stored_artifact(
            kind,
            f"subject:{kind}",
            parents=(
                (current.artifact_id, target.artifact_id)
                if kind == "rollback_request"
                else (
                    (prior_constraint.artifact_id,)
                    if prior_constraint is not None
                    else ()
                )
            ),
        ),
    )
    expected_ref = RefValue(
        artifact_id=current.artifact_id if kind == "rollback_request" else base.artifact_id,
        revision=2,
    )
    profile = _profile_binding()
    rollback_request: RollbackRequestV1 | None = None
    reversed_item: ApprovalItem | None = None
    if kind == "patch":
        binding = PatchTargetBindingV1(
            target_artifact_id=target.artifact_id,
            target_snapshot_id="snapshot:target",
            target_digest=target.payload_hash,
            ref_name="content/head",
            expected_ref=expected_ref,
        )
    elif kind == "constraint_proposal":
        binding = ConstraintTargetBindingV1(
            target_artifact_id=target.artifact_id,
            target_snapshot_id="constraint-snapshot:target",
            target_digest=target.payload_hash,
            ref_name="constraints/head",
            expected_ref=expected_ref,
        )
    else:
        reverses_id = "approval:reversed" if with_reversed_item else None
        rollback_request = RollbackRequestV1(
            ref_name="content/head",
            expected_current_ref=expected_ref,
            target_artifact_id=target.artifact_id,
            target_history_revision=1,
            rollback_profile_binding=profile,
            reason="restore retained target",
            reverses_approval_id=reverses_id,
        )
        binding = RollbackTargetBindingV1(
            target_artifact_kind="ir_snapshot",
            target_artifact_id=target.artifact_id,
            target_snapshot_id="snapshot:target",
            target_digest=target.payload_hash,
            ref_name="content/head",
            expected_ref=expected_ref,
            rollback_profile_binding=profile,
        )
    evidence_payload = EvidenceSet(
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        policy_version="validation-policy@1",
        validation_run_id="run:validation:1",
        target_binding=binding,
        supporting_artifact_ids=(),
        finding_bindings=(),
        requirements=(
            EvidenceRequirement(
                requirement_id="validation",
                kind="deterministic_validation",
                applicability="required",
                status="passed",
                evidence_artifact_id="artifact:evidence:validation",
                tool_version="validator@1",
            ),
        ),
        overall_status="passed",
    )
    evidence = _put_stored(
        state,
        stored_artifact(
            "validation_evidence",
            "evidence",
            parents=(subject.artifact_id, target.artifact_id),
        ),
    )
    state.evidence_sets[evidence.artifact_id] = evidence_payload
    item = _approved_item(
        kind=kind,
        subject=subject,
        target_binding=binding,
        evidence=evidence,
        registry=registry,
        route=route,
        roles=roles,
        approval_policy=approval_policy,
        approval_id=f"approval:{kind}",
        series_id=f"series:{kind}",
    )
    if kind == "constraint_proposal":
        assert prior_constraint is not None
        predecessor_id = "approval:constraint_proposal:prior"
        predecessor = ApprovalItem.model_validate(
            {
                **item.model_dump(mode="python"),
                "approval_id": predecessor_id,
                "subject_revision": 1,
                "subject_artifact_id": prior_constraint.artifact_id,
                "subject_digest": prior_constraint.payload_hash,
                "status": "superseded",
                "workflow_revision": 2,
                "supersedes_approval_id": None,
                "decisions": (),
                "evidence_set_artifact_id": None,
                "regression_evidence_artifact_ids": (),
                "target_binding": None,
                "submitted_at": None,
                "decided_at": None,
                "applied_at": None,
            }
        )
        state.approvals[predecessor.approval_id] = predecessor
        item = item.model_copy(
            update={
                "subject_revision": 2,
                "supersedes_approval_id": predecessor.approval_id,
            }
        )
    state.approvals[item.approval_id] = item
    state.heads[item.subject_series_id] = SubjectHead(
        subject_series_id=item.subject_series_id,
        current_subject_artifact_id=item.subject_artifact_id,
        current_approval_id=item.approval_id,
        revision=1,
    )
    state.refs[binding.ref_name] = expected_ref
    state.history[(binding.ref_name, expected_ref.revision)] = expected_ref
    if rollback_request is not None:
        state.history[(binding.ref_name, rollback_request.target_history_revision)] = RefValue(
            artifact_id=target.artifact_id,
            revision=rollback_request.target_history_revision,
        )
    state.facts[subject.artifact_id] = DraftSubjectFacts(
        subject_kind=kind,
        subject_revision=(
            None if kind == "rollback_request" else (2 if kind == "constraint_proposal" else 1)
        ),
        produced_by="human",
        producer_run_id=None,
        supersedes_artifact_id=(
            prior_constraint.artifact_id if prior_constraint is not None else None
        ),
        target_artifact_id=(target.artifact_id if kind != "patch" else None),
        target_snapshot_id=(
            "constraint-snapshot:target" if kind == "constraint_proposal" else "snapshot:target"
        ),
        rollback_request=rollback_request,
    )

    if rollback_request is not None and rollback_request.reverses_approval_id is not None:
        reversed_subject = _put_stored(
            state,
            stored_artifact("patch", "reversed-subject"),
        )
        reversed_binding = PatchTargetBindingV1(
            target_artifact_id=current.artifact_id,
            target_snapshot_id="snapshot:current",
            target_digest=current.payload_hash,
            ref_name=binding.ref_name,
            expected_ref=RefValue(artifact_id=base.artifact_id, revision=1),
        )
        reversed_item = _approved_item(
            kind="patch",
            subject=reversed_subject,
            target_binding=reversed_binding,
            evidence=evidence,
            registry=registry,
            route=route,
            roles=roles,
            approval_policy=approval_policy,
            approval_id=rollback_request.reverses_approval_id,
            series_id="series:reversed",
        )
        reversed_item = ApprovalItem.model_validate(
            {
                **reversed_item.model_dump(mode="python"),
                "status": "applied",
                "workflow_revision": 6,
                "applied_at": NOW,
            }
        )
        state.approvals[reversed_item.approval_id] = reversed_item

    evidence_gateway = Evidence(state)
    targets = Targets(state)
    auto_apply = AutoApply()
    rollback_execution = RollbackExecution()
    audit = Audit(state)
    capabilities = ApprovedApplyCapabilities(
        approvals=ApprovalRepo(state),
        policies=Policies(registry, route, roles, approval_policy),
        principals=Principals(state),
        artifacts=Artifacts(state),
        refs=Refs(state),
        transitions=Transitions(state),
        idempotency=Idempotency(state),
        audit=audit,
        subjects=Subjects(state),
        evidence=evidence_gateway,
        targets=targets,
        auto_apply=auto_apply,
        rollback_execution=rollback_execution,
    )
    uow = Uow(state)
    service = ApprovedApplyService(
        unit_of_work=uow,
        bind_capabilities=lambda transaction: capabilities,
        clock=FrozenUtcClock(NOW_DT),
        audit_chain_id="authority",
    )
    return Harness(
        state=state,
        service=service,
        uow=uow,
        evidence=evidence_gateway,
        targets=targets,
        auto_apply=auto_apply,
        rollback_execution=rollback_execution,
        audit=audit,
        scenario=Scenario(
            item=item,
            subject=subject,
            target=target,
            evidence=evidence,
            expected_ref=expected_ref,
            rollback_request=rollback_request,
            reversed_item=reversed_item,
        ),
    )


def request(
    harness: Harness, *, context_value: ApprovalCommandContext | None = None
) -> ApprovedApplyRequest:
    scenario = harness.scenario
    binding = scenario.item.target_binding
    assert binding is not None
    return ApprovedApplyRequest(
        approval_id=scenario.item.approval_id,
        expected_workflow_revision=scenario.item.workflow_revision,
        subject_artifact_id=scenario.item.subject_artifact_id,
        subject_digest=scenario.item.subject_digest,
        target_artifact_id=binding.target_artifact_id,
        target_digest=binding.target_digest,
        ref_name=binding.ref_name,
        expected_ref=binding.expected_ref,
        context=context_value or context(),
    )


def authority_snapshot(state: ApplyState) -> dict[str, Any]:
    return copy.deepcopy(
        {
            "approvals": state.approvals,
            "refs": state.refs,
            "history": state.history,
            "transitions": state.transitions,
            "idempotency": state.idempotency,
            "audit": state.audit,
        }
    )


__all__ = [
    "Harness",
    "authority_snapshot",
    "context",
    "harness",
    "principal",
    "request",
]
