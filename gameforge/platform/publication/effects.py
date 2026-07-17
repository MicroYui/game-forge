"""Trusted workflow-effect registry for terminal publication.

``OutcomeArtifactPolicyV1.workflow_effect_key`` is resolved **only** through this
composition-root map into a registered handler — never a client callable or import
path (M4 design line 1116).  Unknown keys fail closed.

The handlers registered here fall into three families:

* the ``no_workflow_change`` / ``no_workflow_subject`` family that leaves
  SubjectHead / ApprovalItem state untouched (Review/Checker/Simulation/Playtest/
  TaskSuite success and every non-success close), plus the evidence-only reject and
  terminal-only failure effects; and
* the transaction-bound asynchronous draft effects, delegated to the same Task-7
  Approval command authority that builds governance bindings and performs
  SubjectHead CAS; and
* the **validation-completion** effects (M4c Task 17b) — ``set_patch_validated@1``
  and friends, plus the ``validating→draft`` revert ``restore_current_draft@1``.
  Per §5.5 (spec line 117: ONE UoW — "插 immutable EvidenceSet … 再 CAS validated"),
  these run INSIDE ``TerminalPublisher``'s terminal UoW: the publisher already
  published the EvidenceSet + booked the Run terminal + wrote the terminal audit;
  the effect does ONLY the ApprovalItem compare-and-set, reusing the single
  validation-completion core in ``platform.approvals.validation`` (no duplicated
  ``_validate_evidence`` logic) against the just-published EvidenceSet payload.

There are no string/deferred effect sentinels in the executable worker registry:
every active ``workflow_effect_key`` resolves to a callable, and any missing
transaction-bound authority port fails the enclosing terminal UoW closed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.jobs import (
    ConstraintProposalProposePayloadV1,
    ConstraintValidationPayloadV1,
    GenerationProposePayloadV1,
    OutcomeArtifactPolicyV1,
    PatchRepairPayloadV1,
    PatchValidationPayloadV1,
    RollbackValidationPayloadV1,
    RunRecord,
)
from gameforge.contracts.lineage import ArtifactV2, AuditActor
from gameforge.contracts.workflow import (
    ApprovalItem,
    AutoApplyProofBindingV1,
    AutoApplyProofV1,
    ConstraintProposalV1,
    EvidenceSet,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.approvals.commands import (
    ApprovalCommandCapabilities,
    ApprovalCommandContext,
    ApprovalCommandService,
    DraftPublicationResult,
    PreparedDraft,
)
from gameforge.platform.approvals.state import ValidationResetReason, validate_status_transition
from gameforge.platform.approvals.validation import (
    ValidationCompletionApprovalRepository,
    build_validation_completion_replacement,
    build_auto_apply_proof_binding,
    build_validation_revert_replacement,
    payload_subject_kind,
    regression_evidence_ids_from_set,
    validate_constraint_evidence_candidate_binding,
    validate_current_subject_binding,
    validate_evidence_subject,
    validate_immutable_subject_binding,
    validate_patch_evidence_binding,
    validate_rollback_evidence_binding,
    validate_strict_superseded_subject_binding,
)


_ValidationPayload = (
    PatchValidationPayloadV1 | ConstraintValidationPayloadV1 | RollbackValidationPayloadV1
)

AgentDraftEffectKey = Literal[
    "create_patch_subject_head_and_draft@1",
    "supersede_patch_head_create_draft@1",
    "create_constraint_subject_head_and_draft@1",
]


@dataclass(frozen=True, slots=True)
class AgentDraftWorkflowRequest:
    """Exact, already-published inputs handed to the Task-7 draft authority.

    The port is transaction-bound by construction: the lifecycle binder creates it
    from the same UnitOfWork capabilities as ``TerminalPublisher``.  Implementations
    must use the shared ``ApprovalCommandService`` draft authority core, including
    governance resolution, SubjectHead CAS, supersede/cancel semantics and audit;
    this request deliberately carries no client-authored ApprovalItem.
    """

    effect_key: AgentDraftEffectKey
    run: RunRecord
    policy: OutcomeArtifactPolicyV1
    initiated_by: AuditActor
    executed_by: AuditActor
    subject_artifact_id: str
    artifacts_by_rule: Mapping[str, tuple[ArtifactV2, ...]]
    artifact_ids_by_rule: Mapping[str, tuple[str, ...]]
    payloads_by_rule: Mapping[str, tuple[Mapping[str, object], ...]]
    expected_subject_head_revision: int | None
    expected_workflow_revision: int | None
    expected_current_approval: ApprovalItem | None
    expected_current_subject_head: SubjectHead | None
    occurred_at: str


class AgentDraftWorkflowPort(Protocol):
    def publish_agent_draft(self, request: AgentDraftWorkflowRequest) -> DraftPublicationResult: ...


class AgentDraftPreparedAssembler(Protocol):
    """Build a complete ``PreparedDraft`` from final retained Artifact authority."""

    def prepare(self, request: AgentDraftWorkflowRequest) -> PreparedDraft: ...


@dataclass(frozen=True, slots=True)
class ApprovalCommandAgentDraftWorkflowPort:
    """Transaction-bound adapter into the canonical Task-7 draft command core.

    Task 10 supplies the governance-aware assembler and capabilities bound to the
    terminal UoW.  This adapter owns the non-client idempotency/audit context and
    invokes :class:`ApprovalCommandService`'s in-transaction entry point; it never
    implements ApprovalItem/SubjectHead transitions itself.
    """

    commands: ApprovalCommandService
    capabilities: ApprovalCommandCapabilities
    assembler: AgentDraftPreparedAssembler

    def publish_agent_draft(self, request: AgentDraftWorkflowRequest) -> DraftPublicationResult:
        if request.initiated_by != request.run.initiated_by:
            raise IntegrityViolation("agent draft initiator differs from its immutable Run")
        if request.executed_by.principal_kind not in {"service", "system"}:
            raise IntegrityViolation("agent draft terminal publisher must be service/system")
        prepared = self.assembler.prepare(request)
        primary = request.artifacts_by_rule.get("primary", ())
        if len(primary) != 1 or prepared.subject_artifact != primary[0]:
            raise IntegrityViolation(
                "agent draft assembler did not preserve the final subject Artifact"
            )
        expected_companions = tuple(
            sorted(
                (
                    *request.artifacts_by_rule.get("preview", ()),
                    *request.artifacts_by_rule.get("config-export", ()),
                ),
                key=lambda artifact: artifact.artifact_id,
            )
        )
        if prepared.companion_artifacts != expected_companions:
            raise IntegrityViolation(
                "agent draft assembler did not preserve the final preview/config Artifacts"
            )
        current_item = request.expected_current_approval
        current_head = request.expected_current_subject_head
        if (current_item is None) != (current_head is None):
            raise IntegrityViolation("agent draft current ApprovalItem/SubjectHead is partial")
        if current_item is None:
            if (
                request.expected_subject_head_revision is not None
                or request.expected_workflow_revision is not None
                or prepared.expected_subject_head is not None
                or prepared.expected_previous_workflow_revision is not None
            ):
                raise IntegrityViolation("initial agent draft carries repair CAS authority")
        elif (
            prepared.expected_subject_head != current_head
            or prepared.expected_previous_workflow_revision != current_item.workflow_revision
            or request.expected_subject_head_revision != current_head.revision
            or request.expected_workflow_revision != current_item.workflow_revision
            or prepared.approval_item.subject_series_id != current_item.subject_series_id
            or prepared.approval_item.domain_scope != current_item.domain_scope
            or prepared.approval_item.supersedes_approval_id != current_item.approval_id
        ):
            raise IntegrityViolation(
                "repair draft assembler differs from current workflow/domain CAS authority"
            )
        if prepared.approval_item.proposer != request.initiated_by:
            raise IntegrityViolation("agent draft proposer differs from immutable Run initiator")
        request_payload = {
            "effect_key": request.effect_key,
            "run_id": request.run.run_id,
            "policy_id": request.policy.policy_id,
            "policy_version": request.policy.policy_version,
            "subject_artifact_id": request.subject_artifact_id,
            "artifact_ids_by_rule": {
                key: list(values) for key, values in sorted(request.artifact_ids_by_rule.items())
            },
            "expected_subject_head_revision": request.expected_subject_head_revision,
            "expected_workflow_revision": request.expected_workflow_revision,
        }
        identity = sha256_lowerhex(canonical_json(request_payload).encode("utf-8"))
        context = ApprovalCommandContext(
            actor=request.executed_by,
            initiated_by=request.initiated_by,
            request_id=f"terminal-workflow:{request.run.run_id}",
            run_id=request.run.run_id,
            idempotency_scope=f"run:{request.run.run_id}",
            idempotency_key=f"{request.effect_key}:{identity}",
            request_hash=identity,
        )
        return self.commands.publish_draft_in_transaction(
            prepared=prepared,
            context=context,
            capabilities=self.capabilities,
        )


@dataclass(frozen=True, slots=True)
class AutoApplyValidationRequest:
    """Same-UoW proof inputs for the deterministic auto-apply completion guard."""

    run: RunRecord
    policy: OutcomeArtifactPolicyV1
    current_item: ApprovalItem
    projected_item: ApprovalItem
    evidence: EvidenceSet
    evidence_artifact: ArtifactV2
    evidence_artifact_id: str
    proof: AutoApplyProofV1
    proof_artifact: ArtifactV2
    proof_artifact_id: str
    regression_artifacts: tuple[ArtifactV2, ...]
    regression_artifact_ids: tuple[str, ...]
    occurred_at: str


class AutoApplyValidationPort(Protocol):
    def validate_completion(self, request: AutoApplyValidationRequest) -> None: ...


# The run-final validation failure outcome codes that revert ``validating→draft``.
# Every terminal that leaves the ApprovalItem stranded in ``validating`` maps to a
# reset reason ``validate_status_transition`` accepts; dependency/quota/lease/
# integrity terminals fold into ``execution_failed`` per spec §"validating→draft"
# ("仅执行/依赖失败、用户取消或超时"). ``subject_superseded`` is absent: a superseded
# subject is caught by the current-head guard and never reverted.
_REVERT_RESET_REASON: Mapping[str, ValidationResetReason] = MappingProxyType(
    {
        "execution_failed": "execution_failed",
        "cancelled": "cancelled",
        "timed_out": "timed_out",
        "queue_timed_out": "timed_out",
        "lease_expired": "execution_failed",
        "dependency_unavailable": "execution_failed",
        "permanent_dependency_failed": "execution_failed",
        "quota_exceeded": "execution_failed",
        "integrity_violation": "execution_failed",
    }
)


@dataclass(frozen=True, slots=True)
class WorkflowEffectContext:
    """Everything a workflow effect may read/write inside the terminal UoW."""

    run: RunRecord
    policy: OutcomeArtifactPolicyV1
    scope: str
    published_primary_artifact_id: str | None
    published_output_artifact_ids: tuple[str, ...]
    approvals: ValidationCompletionApprovalRepository | None
    actor: AuditActor
    occurred_at: str
    published_primary_payload: Mapping[str, object] | None = None
    published_artifact_ids_by_rule: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    published_payloads_by_rule: Mapping[str, tuple[Mapping[str, object], ...]] = field(
        default_factory=dict
    )
    published_artifacts_by_rule: Mapping[str, tuple[ArtifactV2, ...]] = field(default_factory=dict)
    agent_drafts: AgentDraftWorkflowPort | None = None
    auto_apply: AutoApplyValidationPort | None = None


WorkflowEffect = Callable[[WorkflowEffectContext], None]


def _no_workflow_mutation(context: WorkflowEffectContext) -> None:
    """A genuine no-op: this outcome never advances SubjectHead/ApprovalItem."""

    return None


def _validation_payload(run: RunRecord) -> _ValidationPayload:
    params = run.payload.params
    if not isinstance(
        params,
        (PatchValidationPayloadV1, ConstraintValidationPayloadV1, RollbackValidationPayloadV1),
    ):
        raise IntegrityViolation(
            "validation workflow effect requires a validation Run payload",
            run_id=run.run_id,
        )
    return params


def _require_approvals(
    context: WorkflowEffectContext,
) -> ValidationCompletionApprovalRepository:
    if context.approvals is None:
        raise IntegrityViolation(
            "workflow effect requires a transaction-bound approvals capability",
            run_id=context.run.run_id,
        )
    return context.approvals


_AGENT_DRAFT_SELECTORS: Mapping[AgentDraftEffectKey, tuple[str, type[object], str]] = (
    MappingProxyType(
        {
            "create_patch_subject_head_and_draft@1": (
                "generation.propose",
                GenerationProposePayloadV1,
                "patch",
            ),
            "supersede_patch_head_create_draft@1": (
                "patch.repair",
                PatchRepairPayloadV1,
                "patch",
            ),
            "create_constraint_subject_head_and_draft@1": (
                "constraint_proposal.propose",
                ConstraintProposalProposePayloadV1,
                "constraint_proposal",
            ),
        }
    )
)


def _agent_draft_effect(effect_key: AgentDraftEffectKey) -> WorkflowEffect:
    """Delegate one exact asynchronous draft mutation to the Task-7 authority.

    The generic publisher owns Artifact identity and the terminal UoW; the port
    owns ApprovalItem construction/governance and SubjectHead mutation.  Keeping
    that split explicit prevents either side from fabricating the other's facts.
    """

    def _effect(context: WorkflowEffectContext) -> None:
        expected_kind, expected_payload_type, expected_subject_kind = _AGENT_DRAFT_SELECTORS[
            effect_key
        ]
        if (
            context.scope != "run"
            or context.policy.prepared_outcome != "success"
            or context.policy.workflow_effect_key != effect_key
            or context.run.kind.kind != expected_kind
            or context.run.kind.version != 1
            or not isinstance(context.run.payload.params, expected_payload_type)
            or context.actor.principal_kind not in {"service", "system"}
        ):
            raise IntegrityViolation(
                "agent draft workflow effect differs from its exact Run/policy selector",
                workflow_effect_key=effect_key,
                run_id=context.run.run_id,
            )
        port = context.agent_drafts
        if port is None:
            raise IntegrityViolation(
                "agent draft workflow effect has no transaction-bound authority port",
                workflow_effect_key=effect_key,
                run_id=context.run.run_id,
            )
        rule_ids = tuple(rule.rule_id for rule in context.policy.artifact_rules)
        if (
            set(context.published_artifact_ids_by_rule) != set(rule_ids)
            or set(context.published_payloads_by_rule) != set(rule_ids)
            or set(context.published_artifacts_by_rule) != set(rule_ids)
        ):
            raise IntegrityViolation(
                "agent draft workflow effect lacks the exact publication rule closure",
                workflow_effect_key=effect_key,
            )
        for rule_id in rule_ids:
            artifacts = context.published_artifacts_by_rule[rule_id]
            ids = context.published_artifact_ids_by_rule[rule_id]
            payloads = context.published_payloads_by_rule[rule_id]
            if tuple(artifact.artifact_id for artifact in artifacts) != ids or len(payloads) != len(
                artifacts
            ):
                raise IntegrityViolation(
                    "agent draft final Artifact/payload mapping differs",
                    workflow_effect_key=effect_key,
                    outcome_rule_id=rule_id,
                )
        primary_ids = context.published_artifact_ids_by_rule.get("primary", ())
        primary_payloads = context.published_payloads_by_rule.get("primary", ())
        if (
            len(primary_ids) != 1
            or len(primary_payloads) != 1
            or context.published_primary_artifact_id != primary_ids[0]
            or context.published_primary_payload != primary_payloads[0]
        ):
            raise IntegrityViolation(
                "agent draft workflow effect lacks one exact published subject",
                workflow_effect_key=effect_key,
            )
        params = context.run.payload.params
        if expected_subject_kind == "patch":
            subject = PatchV2.model_validate(primary_payloads[0])
        else:
            subject = ConstraintProposalV1.model_validate(primary_payloads[0])
        current_item: ApprovalItem | None = None
        current_head: SubjectHead | None = None
        if isinstance(params, PatchRepairPayloadV1):
            approvals = _require_approvals(context)
            old_approval_id = f"approval:patch:{params.subject_patch_artifact_id}"
            current_item = approvals.get(old_approval_id)
            if current_item is None:
                raise IntegrityViolation(
                    "repair workflow current ApprovalItem is missing",
                    approval_id=old_approval_id,
                )
            current_head = approvals.get_subject_head(current_item.subject_series_id)
            current_target = current_item.target_binding
            if (
                current_head is None
                or current_item.subject_kind != "patch"
                or current_item.subject_artifact_id != params.subject_patch_artifact_id
                or current_item.status != "validation_failed"
                or current_item.workflow_revision != params.expected_workflow_revision
                or current_item.last_validation_failure_artifact_id is not None
                or current_item.evidence_set_artifact_id != params.validation_evidence_artifact_id
                or current_head.current_approval_id != current_item.approval_id
                or current_head.current_subject_artifact_id != current_item.subject_artifact_id
                or current_head.revision != params.expected_subject_head_revision
                or current_head.revision != current_item.subject_revision
                or subject.revision != current_item.subject_revision + 1
                or subject.supersedes_artifact_id != current_item.subject_artifact_id
                or not isinstance(current_target, PatchTargetBindingV1)
                or current_target.target_artifact_id != params.preview_snapshot_artifact_id
                or current_target.ref_name != params.target.ref_name
                or current_target.expected_ref != params.target.expected_ref
                or params.target.expected_ref is None
                or params.target.expected_ref.artifact_id != params.base_snapshot_artifact_id
            ):
                raise IntegrityViolation(
                    "repair workflow differs from current ApprovalItem/SubjectHead CAS",
                    run_id=context.run.run_id,
                )

        request = AgentDraftWorkflowRequest(
            effect_key=effect_key,
            run=context.run,
            policy=context.policy,
            initiated_by=context.run.initiated_by,
            executed_by=context.actor,
            subject_artifact_id=primary_ids[0],
            artifacts_by_rule=MappingProxyType(
                {key: tuple(values) for key, values in context.published_artifacts_by_rule.items()}
            ),
            artifact_ids_by_rule=MappingProxyType(
                {
                    key: tuple(values)
                    for key, values in context.published_artifact_ids_by_rule.items()
                }
            ),
            payloads_by_rule=MappingProxyType(
                {
                    key: tuple(dict(payload) for payload in values)
                    for key, values in context.published_payloads_by_rule.items()
                }
            ),
            expected_subject_head_revision=(
                context.run.payload.params.expected_subject_head_revision
                if isinstance(context.run.payload.params, PatchRepairPayloadV1)
                else None
            ),
            expected_workflow_revision=(
                context.run.payload.params.expected_workflow_revision
                if isinstance(context.run.payload.params, PatchRepairPayloadV1)
                else None
            ),
            expected_current_approval=current_item,
            expected_current_subject_head=current_head,
            occurred_at=context.occurred_at,
        )
        result = port.publish_agent_draft(request)
        item = result.approval_item
        head = result.subject_head
        expected_head_revision = (
            params.expected_subject_head_revision + 1
            if isinstance(params, PatchRepairPayloadV1)
            else 1
        )
        expected_series_id = (
            current_item.subject_series_id
            if current_item is not None
            else f"series:{expected_subject_kind}:{primary_ids[0]}"
        )
        expected_supersedes = (
            f"approval:patch:{params.subject_patch_artifact_id}"
            if isinstance(params, PatchRepairPayloadV1)
            else None
        )
        expected_domain_scope = (
            current_item.domain_scope if current_item is not None else params.domain_scope
        )
        target_ok = item.target_binding is None
        if expected_subject_kind == "patch":
            preview_ids = context.published_artifact_ids_by_rule.get("preview", ())
            preview_artifacts = context.published_artifacts_by_rule.get("preview", ())
            target = item.target_binding
            target_ok = (
                len(preview_ids) == 1
                and len(preview_artifacts) == 1
                and isinstance(target, PatchTargetBindingV1)
                and preview_artifacts[0].artifact_id == preview_ids[0]
                and preview_artifacts[0].kind == "ir_snapshot"
                and target.target_artifact_id == preview_ids[0]
                and target.target_snapshot_id == subject.target_snapshot_id
                and target.target_digest == preview_artifacts[0].payload_hash
                and target.ref_name == params.target.ref_name
                and target.expected_ref == params.target.expected_ref
            )
        if (
            item.subject_kind != expected_subject_kind
            or item.subject_artifact_id != primary_ids[0]
            or item.approval_id != f"approval:{expected_subject_kind}:{primary_ids[0]}"
            or item.subject_series_id != expected_series_id
            or item.subject_revision != subject.revision
            or item.supersedes_approval_id != expected_supersedes
            or not target_ok
            or item.proposer != context.run.initiated_by
            or item.domain_scope != expected_domain_scope
            or item.status != "draft"
            or item.workflow_revision != 1
            or item.created_at != context.occurred_at
            or head.subject_series_id != item.subject_series_id
            or head.current_subject_artifact_id != primary_ids[0]
            or head.current_approval_id != item.approval_id
            or head.revision != expected_head_revision
        ):
            raise IntegrityViolation(
                "agent draft workflow authority returned another draft/head projection",
                workflow_effect_key=effect_key,
                run_id=context.run.run_id,
            )

    return _effect


def _load_published_evidence(context: WorkflowEffectContext) -> EvidenceSet:
    payload = context.published_primary_payload
    if payload is None:
        raise IntegrityViolation(
            "validation completion has no published EvidenceSet payload",
            run_id=context.run.run_id,
        )
    return EvidenceSet.model_validate(payload)


def _validate_overall_status(evidence: EvidenceSet, target_status: str) -> None:
    if target_status == "validated":
        if evidence.overall_status != "passed":
            raise IntegrityViolation(
                "validated completion requires a passed EvidenceSet",
                overall_status=evidence.overall_status,
            )
    elif evidence.overall_status not in {"failed", "unproven"}:
        raise IntegrityViolation(
            "validation_failed completion requires a failed or unproven EvidenceSet",
            overall_status=evidence.overall_status,
        )


def _validate_kind_binding(
    evidence: EvidenceSet,
    item: object,
    run: RunRecord,
    payload: _ValidationPayload,
    subject_kind: str,
) -> None:
    if subject_kind == "patch":
        assert isinstance(payload, PatchValidationPayloadV1)
        validate_patch_evidence_binding(evidence, item, payload)
    elif subject_kind == "rollback_request":
        assert isinstance(payload, RollbackValidationPayloadV1)
        matches = tuple(
            binding
            for binding in run.payload.resolved_profiles
            if binding.field_path == "/params/rollback_profile"
        )
        if len(matches) != 1:
            raise IntegrityViolation(
                "rollback validation Run lacks one exact rollback profile binding",
                run_id=run.run_id,
            )
        validate_rollback_evidence_binding(
            evidence,
            item,
            payload,
            profile_binding=matches[0],
        )
    else:  # constraint_proposal
        assert isinstance(payload, ConstraintValidationPayloadV1)
        validate_constraint_evidence_candidate_binding(evidence, payload)


def _make_validation_completion_effect(
    *,
    subject_kind: str,
    target_status: str,
    require_auto_apply_proof: bool = False,
) -> WorkflowEffect:
    """Build a ``validated``/``validation_failed`` completion effect.

    The effect runs inside the publisher's terminal UoW: it reads the just-published
    EvidenceSet + the frozen validation payload, reuses the shared completion core to
    re-verify the subject/current-binding/evidence, then CASes the ApprovalItem. It
    NEVER re-publishes artifacts, re-books the Run terminal, or writes a second audit
    record (the publisher already did those).  Subject supersede must have been
    converted into a typed terminal failure by the publisher preflight before any
    EvidenceSet was published; observing it here is an atomicity invariant breach.
    """

    def _effect(context: WorkflowEffectContext) -> None:
        payload = _validation_payload(context.run)
        if payload_subject_kind(payload) != subject_kind:
            raise IntegrityViolation(
                "validation effect key does not match the Run payload subject kind",
                run_id=context.run.run_id,
            )
        approvals = _require_approvals(context)
        subject = payload.subject
        item = approvals.get(subject.approval_id)
        if item is None:
            raise IntegrityViolation(
                "validation completion ApprovalItem is missing",
                approval_id=subject.approval_id,
            )
        head = approvals.get_subject_head(item.subject_series_id)
        if head is None:
            raise IntegrityViolation("validation subject series has no SubjectHead")
        if head.current_approval_id != item.approval_id:
            raise IntegrityViolation(
                "validation success reached its workflow effect after subject supersede",
                approval_id=item.approval_id,
                run_id=context.run.run_id,
            )

        validate_immutable_subject_binding(item, subject, subject_kind)
        validate_current_subject_binding(item, head, subject, context.run.run_id)

        evidence = _load_published_evidence(context)
        if evidence.validation_run_id != context.run.run_id:
            raise IntegrityViolation(
                "published EvidenceSet validation_run_id differs from the Run",
                run_id=context.run.run_id,
            )
        _validate_overall_status(evidence, target_status)
        validate_evidence_subject(evidence, item)
        _validate_kind_binding(evidence, item, context.run, payload, subject_kind)

        validate_status_transition(
            current="validating", target=target_status, subject_kind=item.subject_kind
        )
        target_binding = (
            evidence.target_binding
            if subject_kind == "constraint_proposal"
            else item.target_binding
        )
        evidence_artifact_id = context.published_primary_artifact_id
        if evidence_artifact_id is None:
            raise IntegrityViolation(
                "validation completion has no published EvidenceSet artifact id",
                run_id=context.run.run_id,
            )
        regression_evidence_artifact_ids = tuple(
            sorted(context.published_artifact_ids_by_rule.get("regression", ()))
        )
        if regression_evidence_artifact_ids != regression_evidence_ids_from_set(evidence):
            raise IntegrityViolation(
                "published regression evidence differs from the final EvidenceSet"
            )
        proof_binding: AutoApplyProofBindingV1 | None = None
        auto_apply_request: (
            tuple[
                AutoApplyValidationPort,
                AutoApplyProofV1,
                ArtifactV2,
                ArtifactV2,
                tuple[ArtifactV2, ...],
                tuple[str, ...],
            ]
            | None
        ) = None
        if require_auto_apply_proof:
            if (
                subject_kind != "patch"
                or target_status != "validated"
                or context.policy.workflow_effect_key != "set_patch_validated_with_auto_proof@1"
            ):
                raise IntegrityViolation("auto-apply proof effect selector is inconsistent")
            auto_apply = context.auto_apply
            if auto_apply is None:
                raise IntegrityViolation(
                    "auto-apply validation has no transaction-bound deterministic guard",
                    run_id=context.run.run_id,
                )
            rule_ids = {rule.rule_id for rule in context.policy.artifact_rules}
            if (
                set(context.published_artifact_ids_by_rule) != rule_ids
                or set(context.published_payloads_by_rule) != rule_ids
                or set(context.published_artifacts_by_rule) != rule_ids
            ):
                raise IntegrityViolation(
                    "auto-eligible validation lacks exact final rule closure",
                    run_id=context.run.run_id,
                )
            primary_ids = context.published_artifact_ids_by_rule.get("primary", ())
            primary_artifacts = context.published_artifacts_by_rule.get("primary", ())
            if (
                primary_ids != (evidence_artifact_id,)
                or len(primary_artifacts) != 1
                or primary_artifacts[0].artifact_id != evidence_artifact_id
                or primary_artifacts[0].kind != "validation_evidence"
                or primary_artifacts[0].meta.get("payload_schema_id") != "evidence-set@1"
            ):
                raise IntegrityViolation(
                    "auto-eligible validation primary Artifact differs from EvidenceSet"
                )
            proof_ids = context.published_artifact_ids_by_rule.get("auto-apply-proof", ())
            proof_payloads = context.published_payloads_by_rule.get("auto-apply-proof", ())
            proof_artifacts = context.published_artifacts_by_rule.get("auto-apply-proof", ())
            regression_artifacts = tuple(
                sorted(
                    context.published_artifacts_by_rule.get("regression", ()),
                    key=lambda artifact: artifact.artifact_id,
                )
            )
            regression_ids = regression_evidence_artifact_ids
            if (
                len(proof_ids) != 1
                or len(proof_payloads) != 1
                or len(proof_artifacts) != 1
                or proof_artifacts[0].artifact_id != proof_ids[0]
                or proof_artifacts[0].kind != "validation_evidence"
                or proof_artifacts[0].meta.get("payload_schema_id") != "auto-apply-proof@1"
                or tuple(sorted(artifact.artifact_id for artifact in regression_artifacts))
                != regression_ids
            ):
                raise IntegrityViolation(
                    "auto-eligible validation lacks one exact final proof Artifact",
                    run_id=context.run.run_id,
                )
            proof = AutoApplyProofV1.model_validate(proof_payloads[0])
            proof_binding = build_auto_apply_proof_binding(
                proof=proof,
                proof_artifact_id=proof_ids[0],
                evidence_artifact_id=evidence_artifact_id,
                regression_artifact_ids=regression_ids,
                item=item,
            )
            auto_apply_request = (
                auto_apply,
                proof,
                primary_artifacts[0],
                proof_artifacts[0],
                regression_artifacts,
                regression_ids,
            )
        replacement = build_validation_completion_replacement(
            item,
            target_status=target_status,
            evidence_set_artifact_id=evidence_artifact_id,
            regression_evidence_artifact_ids=regression_evidence_artifact_ids,
            target_binding=target_binding,
            auto_apply_proof=proof_binding,
        )
        if auto_apply_request is not None:
            (
                auto_apply,
                proof,
                evidence_artifact,
                proof_artifact,
                regression_artifacts,
                regression_ids,
            ) = auto_apply_request
            auto_apply.validate_completion(
                AutoApplyValidationRequest(
                    run=context.run,
                    policy=context.policy,
                    current_item=item,
                    projected_item=replacement,
                    evidence=evidence,
                    evidence_artifact=evidence_artifact,
                    evidence_artifact_id=evidence_artifact_id,
                    proof=proof,
                    proof_artifact=proof_artifact,
                    proof_artifact_id=proof_artifact.artifact_id,
                    regression_artifacts=regression_artifacts,
                    regression_artifact_ids=regression_ids,
                    occurred_at=context.occurred_at,
                )
            )
        approvals.compare_and_set_validation_completion(
            item.approval_id, item.workflow_revision, replacement
        )

    return _effect


def _restore_current_draft(context: WorkflowEffectContext) -> None:
    """Revert ``validating→draft`` after a validation run-final failure.

    Only a strictly proven supersede is an allowed no-op. Every other drift fails
    closed so a stale/already-reverted/re-validating item cannot be hidden behind a
    successfully published terminal manifest.
    """

    payload = _validation_payload(context.run)
    approvals = _require_approvals(context)
    subject = payload.subject
    item = approvals.get(subject.approval_id)
    if item is None:
        raise IntegrityViolation(
            "validation revert ApprovalItem is missing",
            approval_id=subject.approval_id,
        )
    validate_immutable_subject_binding(item, subject, payload_subject_kind(payload))
    head = approvals.get_subject_head(item.subject_series_id)
    if head is None:
        raise IntegrityViolation("validation revert subject series has no SubjectHead")
    if head.current_approval_id != item.approval_id:
        validate_strict_superseded_subject_binding(item, head, subject)
        return
    validate_current_subject_binding(item, head, subject, context.run.run_id)

    reset_reason = _REVERT_RESET_REASON.get(context.policy.outcome_code)
    if reset_reason is None:
        raise IntegrityViolation(
            "validation revert has no reset reason for the terminal outcome",
            outcome_code=context.policy.outcome_code,
        )
    validate_status_transition(
        current="validating",
        target="draft",
        subject_kind=item.subject_kind,
        validation_reset_reason=reset_reason,
    )
    replacement = build_validation_revert_replacement(
        item, failure_artifact_id=context.published_primary_artifact_id
    )
    approvals.compare_and_set(item.approval_id, item.workflow_revision, replacement)


# The composition-root registry.  Adding a draft-mutating key here is the only
# sanctioned way to enable it; nothing outside this module may inject a handler.
WORKFLOW_EFFECTS: Mapping[str, WorkflowEffect] = MappingProxyType(
    {
        "no_workflow_change@1": _no_workflow_mutation,
        "no_workflow_subject@1": _no_workflow_mutation,
        "terminal_only@1": _no_workflow_mutation,
        "close_attempt_for_terminal@1": _no_workflow_mutation,
        "close_attempt_for_retry@1": _no_workflow_mutation,
        "leave_patch_head_unchanged@1": _no_workflow_mutation,
        # ── asynchronous draft publication ───────────────────────────────────
        "create_patch_subject_head_and_draft@1": _agent_draft_effect(
            "create_patch_subject_head_and_draft@1"
        ),
        "supersede_patch_head_create_draft@1": _agent_draft_effect(
            "supersede_patch_head_create_draft@1"
        ),
        "create_constraint_subject_head_and_draft@1": _agent_draft_effect(
            "create_constraint_subject_head_and_draft@1"
        ),
        # ── validation completion (Task 17b) ──────────────────────────────────
        "set_patch_validated@1": _make_validation_completion_effect(
            subject_kind="patch", target_status="validated"
        ),
        # Completion retains the exact proof binding on ``validated``.  The later
        # submit command reruns eligibility and performs validated ->
        # auto_apply_eligible; absence of either the proof or deterministic guard
        # fails this terminal UoW closed instead of silently downgrading.
        "set_patch_validated_with_auto_proof@1": _make_validation_completion_effect(
            subject_kind="patch",
            target_status="validated",
            require_auto_apply_proof=True,
        ),
        "set_patch_validation_failed@1": _make_validation_completion_effect(
            subject_kind="patch", target_status="validation_failed"
        ),
        "set_rollback_validated@1": _make_validation_completion_effect(
            subject_kind="rollback_request", target_status="validated"
        ),
        "set_rollback_validation_failed@1": _make_validation_completion_effect(
            subject_kind="rollback_request", target_status="validation_failed"
        ),
        "set_exact_binding_and_validated@1": _make_validation_completion_effect(
            subject_kind="constraint_proposal", target_status="validated"
        ),
        "set_exact_binding_and_validation_failed@1": _make_validation_completion_effect(
            subject_kind="constraint_proposal", target_status="validation_failed"
        ),
        "leave_binding_null_and_validation_failed@1": _make_validation_completion_effect(
            subject_kind="constraint_proposal", target_status="validation_failed"
        ),
        "restore_current_draft@1": _restore_current_draft,
    }
)


def resolve_workflow_effect(key: str) -> WorkflowEffect:
    """Resolve a workflow-effect key through the trusted registry, fail-closed."""

    effect = WORKFLOW_EFFECTS.get(key)
    if effect is None:
        raise IntegrityViolation("workflow effect key is not registered", workflow_effect_key=key)
    return effect


def apply_workflow_effect(key: str, context: WorkflowEffectContext) -> None:
    resolve_workflow_effect(key)(context)


__all__ = [
    "AgentDraftWorkflowPort",
    "AgentDraftWorkflowRequest",
    "AgentDraftPreparedAssembler",
    "ApprovalCommandAgentDraftWorkflowPort",
    "AutoApplyValidationPort",
    "AutoApplyValidationRequest",
    "WORKFLOW_EFFECTS",
    "WorkflowEffect",
    "WorkflowEffectContext",
    "apply_workflow_effect",
    "resolve_workflow_effect",
]
