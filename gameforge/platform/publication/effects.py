"""Trusted workflow-effect registry for terminal publication.

``OutcomeArtifactPolicyV1.workflow_effect_key`` is resolved **only** through this
composition-root map into a registered handler — never a client callable or import
path (M4 design line 1116).  Unknown keys fail closed.

The handlers registered here fall into two families:

* the ``no_workflow_change`` / ``no_workflow_subject`` family that leaves
  SubjectHead / ApprovalItem state untouched (Review/Checker/Simulation/Playtest/
  TaskSuite success and every non-success close), plus the evidence-only reject and
  terminal-only failure effects; and
* the **validation-completion** effects (M4c Task 17b) — ``set_patch_validated@1``
  and friends, plus the ``validating→draft`` revert ``restore_current_draft@1``.
  Per §5.5 (spec line 117: ONE UoW — "插 immutable EvidenceSet … 再 CAS validated"),
  these run INSIDE ``TerminalPublisher``'s terminal UoW: the publisher already
  published the EvidenceSet + booked the Run terminal + wrote the terminal audit;
  the effect does ONLY the ApprovalItem compare-and-set, reusing the single
  validation-completion core in ``platform.approvals.validation`` (no duplicated
  ``_validate_evidence`` logic) against the just-published EvidenceSet payload.

Draft-creating / SubjectHead-CAS effects (generation gate pass, repair verified,
constraint proposal create/supersede) remain intentionally unregistered; a policy
naming one resolves to a fail-closed error until its body lands.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    ConstraintValidationPayloadV1,
    OutcomeArtifactPolicyV1,
    PatchValidationPayloadV1,
    RollbackValidationPayloadV1,
    RunRecord,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.workflow import EvidenceSet
from gameforge.platform.approvals.state import ValidationResetReason, validate_status_transition
from gameforge.platform.approvals.validation import (
    ValidationCompletionApprovalRepository,
    build_validation_completion_replacement,
    build_validation_revert_replacement,
    payload_subject_kind,
    regression_evidence_ids_from_set,
    validate_constraint_evidence_candidate_binding,
    validate_current_subject_binding,
    validate_evidence_subject,
    validate_immutable_subject_binding,
    validate_patch_evidence_binding,
    validate_rollback_evidence_binding,
)


_ValidationPayload = (
    PatchValidationPayloadV1 | ConstraintValidationPayloadV1 | RollbackValidationPayloadV1
)

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
            "validation workflow effect requires a transaction-bound approvals capability",
            run_id=context.run.run_id,
        )
    return context.approvals


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
    payload: _ValidationPayload,
    subject_kind: str,
) -> None:
    if subject_kind == "patch":
        assert isinstance(payload, PatchValidationPayloadV1)
        validate_patch_evidence_binding(evidence, item, payload)
    elif subject_kind == "rollback_request":
        assert isinstance(payload, RollbackValidationPayloadV1)
        validate_rollback_evidence_binding(evidence, item, payload, profile_binding=None)
    else:  # constraint_proposal
        assert isinstance(payload, ConstraintValidationPayloadV1)
        validate_constraint_evidence_candidate_binding(evidence, payload)


def _make_validation_completion_effect(*, subject_kind: str, target_status: str) -> WorkflowEffect:
    """Build a ``validated``/``validation_failed`` completion effect.

    The effect runs inside the publisher's terminal UoW: it reads the just-published
    EvidenceSet + the frozen validation payload, reuses the shared completion core to
    re-verify the subject/current-binding/evidence, then CASes the ApprovalItem. It
    NEVER re-publishes artifacts, re-books the Run terminal, or writes a second audit
    record (the publisher already did those); a superseded subject is left untouched.
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
            # Subject superseded during validation: the publisher already finalized
            # the Run terminal; never mutate / revive a non-current item.
            return

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
        _validate_kind_binding(evidence, item, payload, subject_kind)

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
        replacement = build_validation_completion_replacement(
            item,
            target_status=target_status,
            evidence_set_artifact_id=evidence_artifact_id,
            regression_evidence_artifact_ids=regression_evidence_ids_from_set(evidence),
            target_binding=target_binding,
            auto_apply_proof=None,
        )
        approvals.compare_and_set_validation_completion(
            item.approval_id, item.workflow_revision, replacement
        )

    return _effect


def _restore_current_draft(context: WorkflowEffectContext) -> None:
    """Revert ``validating→draft`` after a validation run-final failure.

    Only reverts when the ApprovalItem is still the current head, still
    ``validating``, and still bound to THIS Run; a superseded / already-reverted /
    re-validating item is left untouched (the publisher already finalized the Run/
    Cost/Event/audit terminal).
    """

    payload = _validation_payload(context.run)
    approvals = _require_approvals(context)
    subject = payload.subject
    item = approvals.get(subject.approval_id)
    if item is None:
        return
    head = approvals.get_subject_head(item.subject_series_id)
    if head is None or head.current_approval_id != item.approval_id:
        return
    if item.status != "validating" or item.active_validation_run_id != context.run.run_id:
        return

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
        # ── validation completion (Task 17b) ──────────────────────────────────
        "set_patch_validated@1": _make_validation_completion_effect(
            subject_kind="patch", target_status="validated"
        ),
        # Auto-eligible shares the passed→validated body; recording the
        # ``auto_apply_proof`` binding + the ``validated→auto_apply_eligible``
        # unlock needs the separately-published proof Artifact threaded into the
        # effect context and is deferred (the default worker's evaluator never
        # picks this outcome). It is NOT fail-closed: the patch still validates.
        "set_patch_validated_with_auto_proof@1": _make_validation_completion_effect(
            subject_kind="patch", target_status="validated"
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
    "WORKFLOW_EFFECTS",
    "WorkflowEffect",
    "WorkflowEffectContext",
    "apply_workflow_effect",
    "resolve_workflow_effect",
]
