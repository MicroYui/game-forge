"""Approved publication and rollback commands over one transaction-bound UoW."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from gameforge.contracts.api import compute_resource_etag
from gameforge.contracts.errors import (
    Conflict,
    Forbidden,
    IntegrityViolation,
    InvalidStateTransition,
)
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    ExecutionProfileLifecycleV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    execution_profile_payload_hash,
)
from gameforge.contracts.identity import (
    DomainRegistryV1,
    DomainRoutePolicy,
    Permission,
    Principal,
    RolePolicy,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditCorrelation,
    AuditSubject,
)
from gameforge.contracts.jobs import RollbackValidationPayloadV1, RunRecord
from gameforge.contracts.storage import RefTransitionV1, RefValue, UtcClock
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyV1,
    ConstraintTargetBindingV1,
    EvidenceSet,
    PatchTargetBindingV1,
    RollbackRequestV1,
    RollbackTargetBindingV1,
)
from gameforge.platform.approvals.commands import (
    ApprovalAuditWriter,
    ApprovalAutoApplyGateway,
    ApprovalCommandContext,
    ApprovalEvidenceGateway,
    ApprovalRepository,
    ArtifactRepository,
    GovernancePolicyRepository,
    IdempotencyRepository,
    SubjectPayloadGateway,
)
from gameforge.platform.approvals.decisions import (
    reauthorize_approved_item_for_apply,
    validate_approval_policy_bindings,
)
from gameforge.platform.approvals.state import (
    next_workflow_revision,
    validate_status_transition,
)
from gameforge.platform.rbac import AuthorizationDecision, authorize


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
LowerHexSha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(gt=0)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class VerifiedTargetPayload(_FrozenModel):
    """Payload returned only after ObjectBinding, ObjectStore, and schema decoding."""

    artifact: ArtifactV2
    payload_bytes: bytes
    payload_schema_id: NonEmptyStr
    snapshot_id: NonEmptyStr | None = None


class ApprovedApplyRequest(_FrozenModel):
    approval_id: NonEmptyStr
    expected_workflow_revision: PositiveInt
    subject_artifact_id: NonEmptyStr
    subject_digest: LowerHexSha256
    target_artifact_id: NonEmptyStr
    target_digest: LowerHexSha256
    ref_name: NonEmptyStr
    expected_ref: RefValue | None
    context: ApprovalCommandContext


class ApprovedApplyResult(_FrozenModel):
    approval_item: ApprovalItem
    ref_value: RefValue
    ref_transition: RefTransitionV1 | None = None
    reversed_approval_item: ApprovalItem | None = None

    @model_validator(mode="after")
    def _rollback_shape(self) -> ApprovedApplyResult:
        is_rollback = self.approval_item.subject_kind == "rollback_request"
        if is_rollback != (self.ref_transition is not None):
            raise ValueError("rollback apply result must contain exactly one RefTransition")
        if not is_rollback and self.reversed_approval_item is not None:
            raise ValueError("only rollback may mark another ApprovalItem rolled_back")
        if self.reversed_approval_item is not None:
            if self.reversed_approval_item.status != "rolled_back":
                raise ValueError("reversed ApprovalItem must be rolled_back")
            if self.ref_transition is None:
                raise ValueError("reversed ApprovalItem requires a RefTransition")
        return self


class ApplyPrincipalRepository(Protocol):
    def get(self, principal_id: str) -> Principal | None: ...


class ApplyRefStore(Protocol):
    def get(self, name: str) -> RefValue | None: ...

    def get_history_entry(self, name: str, revision: int) -> RefValue | None: ...

    def compare_and_set(
        self,
        name: str,
        expected: RefValue | None,
        new_artifact_id: str,
    ) -> RefValue: ...


class RefTransitionRepository(Protocol):
    def get(self, transition_id: str) -> RefTransitionV1 | None: ...

    def put(self, transition: RefTransitionV1) -> RefTransitionV1: ...


class ApplyEvidenceGateway(ApprovalEvidenceGateway, Protocol):
    def load_evidence_set(self, artifact: ArtifactV2) -> EvidenceSet: ...


class ApplyTargetGateway(Protocol):
    def read_verified(self, artifact: ArtifactV2) -> VerifiedTargetPayload: ...


class RollbackExecutionVerifier(Protocol):
    def validate(
        self,
        *,
        item: ApprovalItem,
        request: RollbackRequestV1,
        evidence_set: EvidenceSet,
    ) -> None: ...


class RollbackRunRepository(Protocol):
    def get(self, run_id: str) -> RunRecord | None: ...


class ExecutionProfileBindingResolver(Protocol):
    def resolve_execution_profile_binding(
        self,
        binding: ResolvedExecutionProfileBindingV1,
    ) -> tuple[ExecutionProfileDefinitionV1, ExecutionProfileLifecycleV1]: ...


class ExactRollbackExecutionVerifier:
    """Close rollback authority against the immutable validation Run and catalog."""

    def __init__(
        self,
        *,
        runs: RollbackRunRepository,
        profiles: ExecutionProfileBindingResolver,
    ) -> None:
        self._runs = runs
        self._profiles = profiles

    def validate(
        self,
        *,
        item: ApprovalItem,
        request: RollbackRequestV1,
        evidence_set: EvidenceSet,
    ) -> None:
        run = self._runs.get(evidence_set.validation_run_id)
        if not isinstance(run, RunRecord):
            raise IntegrityViolation("rollback validation Run is unavailable")
        expected_kind = RunKindRef(kind="rollback.validate", version=1)
        if run.status != "succeeded" or run.kind != expected_kind:
            raise IntegrityViolation("rollback authority requires a succeeded validation Run")
        payload = run.payload
        params = payload.params
        if not isinstance(params, RollbackValidationPayloadV1):
            raise IntegrityViolation("rollback validation Run has another payload type")
        binding = request.rollback_profile_binding
        if (
            payload.execution_profile_catalog_version != binding.catalog_version
            or payload.execution_profile_catalog_digest != binding.catalog_digest
        ):
            raise IntegrityViolation("rollback Run catalog projection differs from binding")
        matches = tuple(
            resolved
            for resolved in payload.resolved_profiles
            if resolved.field_path == "/params/rollback_profile"
        )
        if len(matches) != 1 or matches[0] != binding:
            raise IntegrityViolation("rollback Run does not contain the exact profile binding")
        if (
            params.subject.approval_id != item.approval_id
            or params.subject.subject_artifact_id != item.subject_artifact_id
            or params.subject.subject_digest != item.subject_digest
            or params.subject.active_validation_run_id != run.run_id
            or params.ref_name != request.ref_name
            or params.expected_current_ref != request.expected_current_ref
            or params.target_artifact_id != request.target_artifact_id
            or params.target_history_revision != request.target_history_revision
            or params.rollback_profile != binding.profile
        ):
            raise IntegrityViolation("rollback validation Run differs from RollbackRequest")
        definition, lifecycle = self._profiles.resolve_execution_profile_binding(binding)
        if (
            definition.profile != binding.profile
            or definition.profile_kind != "rollback"
            or execution_profile_payload_hash(definition) != binding.profile_payload_hash
            or expected_kind not in definition.compatible_run_kinds
            or not set(item.domain_scope.domain_ids).issubset(definition.domain_scope.domain_ids)
            or lifecycle.profile != binding.profile
        ):
            raise IntegrityViolation("rollback profile definition differs from Run binding")
        allowed_states = (
            {"active", "replay_only"} if payload.llm_execution_mode == "replay" else {"active"}
        )
        if lifecycle.state not in allowed_states:
            raise IntegrityViolation("rollback profile lifecycle forbids this validation Run")


@dataclass(slots=True)
class ApprovedApplyCapabilities:
    approvals: ApprovalRepository | None
    policies: GovernancePolicyRepository | None
    principals: ApplyPrincipalRepository | None
    artifacts: ArtifactRepository | None
    refs: ApplyRefStore | None
    transitions: RefTransitionRepository | None
    idempotency: IdempotencyRepository | None
    audit: ApprovalAuditWriter | None
    subjects: SubjectPayloadGateway | None
    evidence: ApplyEvidenceGateway | None
    targets: ApplyTargetGateway | None
    auto_apply: ApprovalAutoApplyGateway | None = None
    rollback_execution: RollbackExecutionVerifier | None = None


class ApprovedApplyUnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...


ApplyCapabilityBinder = Callable[[Any], ApprovedApplyCapabilities]


def _required[T](value: T | None, name: str) -> T:
    if value is None:
        raise IntegrityViolation(f"{name} approved apply capability is unavailable")
    return value


def _utc_text(clock: UtcClock) -> str:
    now = clock.now_utc()
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("approved apply clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _replace_item(item: ApprovalItem, **updates: object) -> ApprovalItem:
    payload = item.model_dump(mode="python")
    payload.update(updates)
    return ApprovalItem.model_validate(payload)


class ApprovedApplyService:
    """Move authority only after exact approval, evidence, and target revalidation."""

    def __init__(
        self,
        *,
        unit_of_work: ApprovedApplyUnitOfWork,
        bind_capabilities: ApplyCapabilityBinder,
        clock: UtcClock,
        audit_chain_id: str,
    ) -> None:
        if not audit_chain_id:
            raise ValueError("audit_chain_id must be non-empty")
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._clock = clock
        self._audit_chain_id = audit_chain_id

    @property
    def capability_names(self) -> frozenset[str]:
        """Expose the fixed write surface; notably there is no lineage capability."""

        return frozenset(field.name for field in fields(ApprovedApplyCapabilities))

    def apply(self, request: ApprovedApplyRequest) -> ApprovedApplyResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            approvals = _required(capabilities.approvals, "approvals")
            policies = _required(capabilities.policies, "policies")
            principals = _required(capabilities.principals, "principals")
            artifacts = _required(capabilities.artifacts, "artifacts")
            refs = _required(capabilities.refs, "refs")
            idempotency = _required(capabilities.idempotency, "idempotency")
            audit = _required(capabilities.audit, "audit")
            subjects = _required(capabilities.subjects, "subjects")
            evidence = _required(capabilities.evidence, "evidence")
            targets = _required(capabilities.targets, "targets")

            item = self._load_item(approvals, request.approval_id)
            self._validate_request_identity(request=request, item=item)
            replay = self._get_idempotent(idempotency, request.context)
            if replay is not None:
                return self._replay_result(
                    replay,
                    request=request,
                    current=item,
                )

            if request.context.if_match is not None:
                expected_etag = compute_resource_etag(
                    resource_kind=item.subject_kind,
                    resource_id=item.subject_artifact_id,
                    revision=item.workflow_revision,
                )
                if request.context.if_match != expected_etag:
                    raise Conflict(
                        "If-Match does not match the authoritative resource revision",
                        resource_kind=item.subject_kind,
                        resource_id=item.subject_artifact_id,
                        revision=item.workflow_revision,
                    )
            if item.workflow_revision != request.expected_workflow_revision:
                raise Conflict(
                    "ApprovalItem workflow revision differs",
                    expected_workflow_revision=request.expected_workflow_revision,
                    actual_workflow_revision=item.workflow_revision,
                )
            self._require_current_head(approvals, item)
            registry, route, role, approval_policy = self._resolve_policies(
                item=item,
                policies=policies,
            )
            if not approval_policy.reauthorize_on_apply:
                raise IntegrityViolation("approval policy disables mandatory apply reauthorization")
            self._authorize_apply_context(
                item=item,
                context=request.context,
                principals=principals,
                domain_registry=registry,
                role_policy=role,
            )
            if item.status == "approved":
                reauthorize_approved_item_for_apply(
                    item=item,
                    principal_resolver=principals.get,
                    domain_registry=registry,
                    route_policy=route,
                    role_policy=role,
                    approval_policy=approval_policy,
                )
            elif item.status == "auto_apply_eligible":
                if item.subject_kind != "patch":
                    raise IntegrityViolation("auto-apply eligibility is valid only for Patch")
                auto_apply = _required(capabilities.auto_apply, "auto_apply")
                auto_apply.validate_eligibility(item=item)
            else:
                raise InvalidStateTransition(
                    "approved apply requires approved or auto_apply_eligible status",
                    current_status=item.status,
                )

            binding = item.target_binding
            if binding is None:
                raise IntegrityViolation("approved ApprovalItem has no exact target binding")
            subject_artifact = self._load_artifact(
                artifacts,
                item.subject_artifact_id,
                expected_kind=item.subject_kind,
            )
            target_artifact = self._load_artifact(
                artifacts,
                binding.target_artifact_id,
                expected_kind=binding.target_artifact_kind,
            )
            evidence_artifact = self._load_artifact(
                artifacts,
                item.evidence_set_artifact_id,
                expected_kind="validation_evidence",
            )
            regression_artifacts = tuple(
                self._load_artifact(
                    artifacts,
                    artifact_id,
                    expected_kind="regression_evidence",
                )
                for artifact_id in item.regression_evidence_artifact_ids
            )
            facts = subjects.inspect_draft_subject(subject_artifact)
            self._validate_subject_facts(
                item=item,
                artifact=subject_artifact,
                facts=facts,
                approvals=approvals,
            )
            projection = evidence.validate_submission(
                item=item,
                subject_artifact=subject_artifact,
                target_artifact=target_artifact,
                evidence_artifact=evidence_artifact,
                regression_artifacts=regression_artifacts,
            )
            if projection.validation_status != "passed" or projection.regression_status not in {
                "passed",
                "not_applicable",
            }:
                raise InvalidStateTransition("approved apply evidence is not passed")
            evidence_set = evidence.load_evidence_set(evidence_artifact)
            self._validate_evidence_set(item=item, evidence_set=evidence_set)
            self._validate_target_payload(
                binding=binding,
                artifact=target_artifact,
                resolved=targets.read_verified(target_artifact),
            )

            current_ref = refs.get(binding.ref_name)
            if current_ref != binding.expected_ref:
                raise Conflict(
                    "approved apply ref precondition differs",
                    ref_name=binding.ref_name,
                    expected=(
                        None
                        if binding.expected_ref is None
                        else binding.expected_ref.model_dump(mode="json")
                    ),
                    actual=(None if current_ref is None else current_ref.model_dump(mode="json")),
                )

            rollback_request: RollbackRequestV1 | None = None
            reversed_item: ApprovalItem | None = None
            if isinstance(binding, RollbackTargetBindingV1):
                rollback_request = facts.rollback_request
                if rollback_request is None:
                    raise IntegrityViolation("rollback subject parser omitted RollbackRequest")
                self._validate_rollback_request(
                    item=item,
                    binding=binding,
                    request=rollback_request,
                    refs=refs,
                )
                rollback_execution = _required(
                    capabilities.rollback_execution,
                    "rollback_execution",
                )
                rollback_execution.validate(
                    item=item,
                    request=rollback_request,
                    evidence_set=evidence_set,
                )
                if rollback_request.reverses_approval_id is not None:
                    reversed_item = self._load_reversed_item(
                        approvals=approvals,
                        artifacts=artifacts,
                        approval_id=rollback_request.reverses_approval_id,
                        ref_name=binding.ref_name,
                        expected_current_ref=binding.expected_ref,
                    )

            occurred_at = _utc_text(self._clock)
            validate_status_transition(
                current=item.status,
                target="applied",
                subject_kind=item.subject_kind,
            )
            next_revision = next_workflow_revision(
                actual=item.workflow_revision,
                expected=request.expected_workflow_revision,
            )
            applied_item = _replace_item(
                item,
                status="applied",
                workflow_revision=next_revision,
                applied_at=occurred_at,
            )
            approvals.compare_and_set(
                item.approval_id,
                item.workflow_revision,
                applied_item,
            )
            ref_value = refs.compare_and_set(
                binding.ref_name,
                binding.expected_ref,
                binding.target_artifact_id,
            )

            transition: RefTransitionV1 | None = None
            reversed_replacement: ApprovalItem | None = None
            if rollback_request is not None:
                if binding.expected_ref is None:  # pragma: no cover - contract forbids it
                    raise IntegrityViolation("rollback requires a current ref")
                transitions = _required(capabilities.transitions, "transitions")
                transition = RefTransitionV1.create(
                    ref_name=binding.ref_name,
                    from_ref=binding.expected_ref,
                    to_ref=ref_value,
                    approval_item_id=item.approval_id,
                    actor=request.context.actor,
                    initiated_by=request.context.initiated_by,
                    request_id=request.context.request_id,
                    occurred_at=occurred_at,
                )
                written = transitions.put(transition)
                if written != transition:
                    raise IntegrityViolation("RefTransition repository returned another record")
                if reversed_item is not None:
                    validate_status_transition(
                        current=reversed_item.status,
                        target="rolled_back",
                        subject_kind=reversed_item.subject_kind,
                    )
                    reversed_replacement = _replace_item(
                        reversed_item,
                        status="rolled_back",
                        workflow_revision=reversed_item.workflow_revision + 1,
                    )
                    approvals.compare_and_set(
                        reversed_item.approval_id,
                        reversed_item.workflow_revision,
                        reversed_replacement,
                    )

            result = ApprovedApplyResult(
                approval_item=applied_item,
                ref_value=ref_value,
                ref_transition=transition,
                reversed_approval_item=reversed_replacement,
            )
            self._audit(
                audit=audit,
                context=request.context,
                action=(
                    "approval.rollback_applied"
                    if rollback_request is not None
                    else "approval.applied"
                ),
                item=applied_item,
            )
            response = result.model_dump(mode="json")
            stored = idempotency.put_result(
                scope=request.context.idempotency_scope,
                operation="approval.apply",
                key=request.context.idempotency_key,
                request_hash=request.context.request_hash,
                resource_kind="approval",
                resource_id=item.approval_id,
                response=response,
            )
            if dict(stored) != response:
                raise IntegrityViolation("idempotency repository stored another apply response")
            return result

    @staticmethod
    def _load_item(
        approvals: ApprovalRepository,
        approval_id: str,
    ) -> ApprovalItem:
        item = approvals.get(approval_id)
        if item is None:
            raise Conflict("ApprovalItem does not exist", approval_id=approval_id)
        return item

    @staticmethod
    def _require_current_head(
        approvals: ApprovalRepository,
        item: ApprovalItem,
    ) -> None:
        current = approvals.current(item.subject_series_id)
        if current is None or current[1] != item:
            raise Conflict("ApprovalItem is not the current SubjectHead")

    @staticmethod
    def _load_artifact(
        artifacts: ArtifactRepository,
        artifact_id: str | None,
        *,
        expected_kind: str,
    ) -> ArtifactV2:
        if artifact_id is None:
            raise IntegrityViolation("required ArtifactV2 id is unavailable")
        artifact = artifacts.get(artifact_id)
        if not isinstance(artifact, ArtifactV2):
            raise IntegrityViolation("required ArtifactV2 is unavailable", artifact_id=artifact_id)
        if artifact.kind != expected_kind:
            raise IntegrityViolation(
                "Artifact kind differs from approved binding",
                artifact_id=artifact_id,
                expected_kind=expected_kind,
                actual_kind=artifact.kind,
            )
        return artifact

    @staticmethod
    def _resolve_policies(
        *,
        item: ApprovalItem,
        policies: GovernancePolicyRepository,
    ) -> tuple[DomainRegistryV1, DomainRoutePolicy, RolePolicy, ApprovalPolicyV1]:
        registry = policies.get_domain_registry(item.domain_registry_ref)
        route = policies.get_domain_route_policy(item.route_policy)
        role = policies.get_role_policy(
            item.role_policy_version,
            item.role_policy_digest,
        )
        approval = policies.get_approval_policy(item.approval_policy)
        missing = [
            name
            for name, value in (
                ("domain registry", registry),
                ("route policy", route),
                ("role policy", role),
                ("approval policy", approval),
            )
            if value is None
        ]
        if missing:
            raise IntegrityViolation(
                "approved apply retained policy history is unavailable",
                missing=missing,
            )
        assert registry is not None and route is not None and role is not None
        assert approval is not None
        validate_approval_policy_bindings(
            item=item,
            domain_registry=registry,
            route_policy=route,
            role_policy=role,
            approval_policy=approval,
        )
        return registry, route, role, approval

    @staticmethod
    def _validate_request_identity(
        *,
        request: ApprovedApplyRequest,
        item: ApprovalItem,
    ) -> None:
        binding = item.target_binding
        if binding is None:
            raise IntegrityViolation("ApprovalItem has no exact target binding")
        if (
            request.subject_artifact_id != item.subject_artifact_id
            or request.subject_digest != item.subject_digest
            or request.target_artifact_id != binding.target_artifact_id
            or request.target_digest != binding.target_digest
            or request.ref_name != binding.ref_name
            or request.expected_ref != binding.expected_ref
        ):
            raise Conflict("apply request differs from the approved exact binding")

    @staticmethod
    def _validate_subject_facts(
        *,
        item: ApprovalItem,
        artifact: ArtifactV2,
        facts: Any,
        approvals: ApprovalRepository,
    ) -> None:
        if (
            artifact.artifact_id != item.subject_artifact_id
            or artifact.payload_hash != item.subject_digest
        ):
            raise IntegrityViolation("subject Artifact differs from ApprovalItem")
        if facts.subject_kind != item.subject_kind:
            raise IntegrityViolation("parsed subject kind differs from ApprovalItem")
        if (
            item.subject_kind != "rollback_request"
            and facts.subject_revision != item.subject_revision
        ):
            raise IntegrityViolation("parsed subject revision differs from ApprovalItem")
        if item.subject_kind == "constraint_proposal":
            predecessor = (
                None
                if item.supersedes_approval_id is None
                else approvals.get(item.supersedes_approval_id)
            )
            if (
                facts.produced_by != "human"
                or item.proposer.principal_kind != "human"
                or facts.subject_revision is None
                or facts.subject_revision <= 1
                or predecessor is None
                or predecessor.status != "superseded"
                or predecessor.subject_kind != item.subject_kind
                or predecessor.subject_series_id != item.subject_series_id
                or item.subject_revision != predecessor.subject_revision + 1
                or facts.supersedes_artifact_id != predecessor.subject_artifact_id
            ):
                raise IntegrityViolation(
                    "constraint proposal requires an exact superseding human author revision"
                )
        binding = item.target_binding
        if binding is None:
            raise IntegrityViolation("parsed subject has no ApprovalTargetBinding")
        if facts.target_artifact_id is not None and (
            facts.target_artifact_id != binding.target_artifact_id
        ):
            raise IntegrityViolation("parsed subject target artifact differs from binding")
        if facts.target_snapshot_id is not None and (
            facts.target_snapshot_id != binding.target_snapshot_id
        ):
            raise IntegrityViolation("parsed subject target snapshot differs from binding")

    @staticmethod
    def _authorize_apply_context(
        *,
        item: ApprovalItem,
        context: ApprovalCommandContext,
        principals: ApplyPrincipalRepository,
        domain_registry: DomainRegistryV1,
        role_policy: RolePolicy,
    ) -> None:
        action, resource_kind = {
            "patch": ("apply", "patch"),
            "constraint_proposal": ("publish", "constraint_proposal"),
            "rollback_request": ("rollback", "ref"),
        }[item.subject_kind]
        actors = [("executor", context.actor)]
        if context.initiated_by is not None and context.initiated_by != context.actor:
            actors.append(("initiator", context.initiated_by))
        for actor_label, actor in actors:
            principal = principals.get(actor.principal_id)
            if (
                principal is None
                or principal.kind != actor.principal_kind
                or principal.status != "active"
            ):
                raise Forbidden(f"approved apply {actor_label} is not a current active principal")
            decision = authorize(
                principal=principal,
                role_policy=role_policy,
                requested_permission=Permission(
                    action=action,
                    resource_kind=resource_kind,
                    domain_scope=item.domain_scope,
                ),
                domain_registry=domain_registry,
            )
            if decision is not AuthorizationDecision.ALLOW:
                raise Forbidden(
                    f"approved apply {actor_label} lacks the current resource permission",
                    action=action,
                    resource_kind=resource_kind,
                )

    @staticmethod
    def _validate_evidence_set(*, item: ApprovalItem, evidence_set: EvidenceSet) -> None:
        if (
            evidence_set.subject_artifact_id != item.subject_artifact_id
            or evidence_set.subject_digest != item.subject_digest
            or evidence_set.target_binding != item.target_binding
            or evidence_set.overall_status != "passed"
        ):
            raise IntegrityViolation("EvidenceSet differs from approved subject or target")

    @staticmethod
    def _validate_target_payload(
        *,
        binding: PatchTargetBindingV1 | ConstraintTargetBindingV1 | RollbackTargetBindingV1,
        artifact: ArtifactV2,
        resolved: VerifiedTargetPayload,
    ) -> None:
        if resolved.artifact != artifact:
            raise IntegrityViolation("verified target reader returned another Artifact")
        payload_hash = hashlib.sha256(resolved.payload_bytes).hexdigest()
        if (
            len(resolved.payload_bytes) != artifact.object_ref.size_bytes
            or payload_hash != artifact.object_ref.sha256
            or payload_hash != artifact.payload_hash
            or artifact.artifact_id != binding.target_artifact_id
            or artifact.kind != binding.target_artifact_kind
            or artifact.payload_hash != binding.target_digest
        ):
            raise IntegrityViolation("verified target payload differs from approved binding")
        if binding.target_snapshot_id is not None and (
            resolved.snapshot_id != binding.target_snapshot_id
        ):
            raise IntegrityViolation("verified target snapshot id differs from approved binding")
        version_snapshot = (
            artifact.version_tuple.constraint_snapshot_id
            if artifact.kind == "constraint_snapshot"
            else artifact.version_tuple.ir_snapshot_id
        )
        if (
            binding.target_snapshot_id is not None
            and version_snapshot != binding.target_snapshot_id
        ):
            raise IntegrityViolation("target VersionTuple differs from approved snapshot id")

    @staticmethod
    def _validate_rollback_request(
        *,
        item: ApprovalItem,
        binding: RollbackTargetBindingV1,
        request: RollbackRequestV1,
        refs: ApplyRefStore,
    ) -> None:
        if (
            request.ref_name != binding.ref_name
            or request.expected_current_ref != binding.expected_ref
            or request.target_artifact_id != binding.target_artifact_id
            or request.rollback_profile_binding != binding.rollback_profile_binding
        ):
            raise IntegrityViolation("RollbackRequest differs from exact target binding")
        historical = refs.get_history_entry(
            request.ref_name,
            request.target_history_revision,
        )
        expected_history = RefValue(
            artifact_id=request.target_artifact_id,
            revision=request.target_history_revision,
        )
        if historical != expected_history:
            raise Conflict(
                "rollback target is not the exact retained ref history member",
                ref_name=request.ref_name,
                target_history_revision=request.target_history_revision,
            )
        if item.subject_kind != "rollback_request":  # pragma: no cover - discriminated binding
            raise IntegrityViolation("rollback binding belongs to another subject kind")

    @classmethod
    def _load_reversed_item(
        cls,
        *,
        approvals: ApprovalRepository,
        artifacts: ArtifactRepository,
        approval_id: str,
        ref_name: str,
        expected_current_ref: RefValue,
    ) -> ApprovalItem:
        item = cls._load_item(approvals, approval_id)
        if item.status != "applied" or item.subject_kind == "rollback_request":
            raise InvalidStateTransition(
                "reversed ApprovalItem must be an applied Patch or constraint proposal"
            )
        binding = item.target_binding
        if (
            binding is None
            or binding.ref_name != ref_name
            or binding.target_artifact_id != expected_current_ref.artifact_id
        ):
            raise IntegrityViolation("reversed ApprovalItem does not publish the current ref")
        expected_published_revision = (
            1 if binding.expected_ref is None else binding.expected_ref.revision + 1
        )
        if expected_published_revision != expected_current_ref.revision:
            raise IntegrityViolation(
                "reversed ApprovalItem does not identify the current ref revision"
            )
        target = cls._load_artifact(
            artifacts,
            binding.target_artifact_id,
            expected_kind=binding.target_artifact_kind,
        )
        if target.payload_hash != binding.target_digest:
            raise IntegrityViolation("reversed ApprovalItem target digest differs")
        return item

    @staticmethod
    def _get_idempotent(
        repository: IdempotencyRepository,
        context: ApprovalCommandContext,
    ) -> dict[str, Any] | None:
        return repository.get_result(
            scope=context.idempotency_scope,
            operation="approval.apply",
            key=context.idempotency_key,
            request_hash=context.request_hash,
        )

    @classmethod
    def _replay_result(
        cls,
        response: Mapping[str, Any],
        *,
        request: ApprovedApplyRequest,
        current: ApprovalItem,
    ) -> ApprovedApplyResult:
        try:
            result = ApprovedApplyResult.model_validate(response)
        except ValidationError as exc:
            raise IntegrityViolation("apply idempotency response is malformed") from exc
        retained = result.approval_item
        expected_ref_value = RefValue(
            artifact_id=request.target_artifact_id,
            revision=(1 if request.expected_ref is None else request.expected_ref.revision + 1),
        )
        if (
            retained.approval_id != request.approval_id
            or retained.subject_series_id != current.subject_series_id
            or retained.subject_kind != current.subject_kind
            or retained.subject_artifact_id != request.subject_artifact_id
            or retained.subject_digest != request.subject_digest
            or retained.target_binding != current.target_binding
            or retained.evidence_set_artifact_id != current.evidence_set_artifact_id
            or retained.regression_evidence_artifact_ids != current.regression_evidence_artifact_ids
            or retained.domain_registry_ref != current.domain_registry_ref
            or retained.route_policy != current.route_policy
            or retained.role_policy_version != current.role_policy_version
            or retained.role_policy_digest != current.role_policy_digest
            or retained.approval_policy != current.approval_policy
            or retained.workflow_revision != request.expected_workflow_revision + 1
            or retained.status != "applied"
            or result.ref_value != expected_ref_value
        ):
            raise IntegrityViolation("apply idempotency response differs from the command")
        if result.ref_transition is not None:
            transition = result.ref_transition
            if (
                request.expected_ref is None
                or transition.ref_name != request.ref_name
                or transition.from_ref != request.expected_ref
                or transition.to_ref != expected_ref_value
                or transition.approval_item_id != request.approval_id
                or transition.actor != request.context.actor
                or transition.initiated_by != request.context.initiated_by
                or transition.request_id != request.context.request_id
            ):
                raise IntegrityViolation("rollback idempotency transition differs from the command")
        reversed_item = result.reversed_approval_item
        if reversed_item is not None:
            binding = reversed_item.target_binding
            if (
                request.expected_ref is None
                or reversed_item.subject_kind == "rollback_request"
                or binding is None
                or binding.ref_name != request.ref_name
                or binding.target_artifact_id != request.expected_ref.artifact_id
                or (1 if binding.expected_ref is None else binding.expected_ref.revision + 1)
                != request.expected_ref.revision
            ):
                raise IntegrityViolation(
                    "rollback idempotency reversed item differs from the command"
                )
        return result

    def _audit(
        self,
        *,
        audit: ApprovalAuditWriter,
        context: ApprovalCommandContext,
        action: str,
        item: ApprovalItem,
    ) -> None:
        audit.append(
            chain_id=self._audit_chain_id,
            actor=context.actor,
            initiated_by=context.initiated_by,
            action=action,
            subject=AuditSubject(
                resource_kind="approval",
                resource_id=item.approval_id,
                artifact_id=item.subject_artifact_id,
            ),
            correlation=AuditCorrelation(
                request_id=context.request_id,
                run_id=context.run_id,
                trace_id=context.trace_id,
            ),
        )


__all__ = [
    "ApplyCapabilityBinder",
    "ApplyEvidenceGateway",
    "ApplyPrincipalRepository",
    "ApplyRefStore",
    "ApplyTargetGateway",
    "ApprovedApplyCapabilities",
    "ApprovedApplyRequest",
    "ApprovedApplyResult",
    "ApprovedApplyService",
    "ApprovedApplyUnitOfWork",
    "ExactRollbackExecutionVerifier",
    "ExecutionProfileBindingResolver",
    "RefTransitionRepository",
    "RollbackExecutionVerifier",
    "RollbackRunRepository",
    "VerifiedTargetPayload",
]
