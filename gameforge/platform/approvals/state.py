"""Pure ApprovalItem status and workflow-revision guards."""

from __future__ import annotations

from typing import Literal

from gameforge.contracts.errors import Conflict, InvalidStateTransition
from gameforge.contracts.identity import SubjectKind
from gameforge.contracts.workflow import ApprovalStatus


ValidationResetReason = Literal["execution_failed", "cancelled", "timed_out"]

ALLOWED_STATUS_TRANSITIONS: frozenset[tuple[ApprovalStatus, ApprovalStatus]] = frozenset(
    {
        ("draft", "validating"),
        ("draft", "superseded"),
        ("validating", "validated"),
        ("validating", "validation_failed"),
        ("validating", "draft"),
        ("validating", "superseded"),
        ("validation_failed", "superseded"),
        ("validated", "pending_approval"),
        ("validated", "auto_apply_eligible"),
        ("validated", "superseded"),
        ("pending_approval", "pending_approval"),
        ("pending_approval", "approved"),
        ("pending_approval", "changes_requested"),
        ("pending_approval", "rejected"),
        ("pending_approval", "superseded"),
        ("auto_apply_eligible", "applied"),
        ("auto_apply_eligible", "superseded"),
        ("approved", "applied"),
        ("approved", "superseded"),
        ("changes_requested", "superseded"),
        ("rejected", "superseded"),
        ("applied", "rolled_back"),
    }
)


def next_workflow_revision(*, actual: int, expected: int) -> int:
    """Apply the workflow CAS precondition and return the next revision."""

    if actual != expected:
        raise Conflict(
            "workflow revision does not match",
            actual_revision=actual,
            expected_revision=expected,
        )
    return actual + 1


def validate_status_transition(
    *,
    current: ApprovalStatus,
    target: ApprovalStatus,
    subject_kind: SubjectKind | None = None,
    validation_reset_reason: ValidationResetReason | None = None,
) -> None:
    """Reject every ApprovalItem status edge not frozen by the M4 design."""

    edge = (current, target)
    if edge not in ALLOWED_STATUS_TRANSITIONS:
        raise InvalidStateTransition(
            f"ApprovalItem transition {current!r} -> {target!r} is not allowed",
            current_status=current,
            target_status=target,
        )

    if edge == ("validating", "draft"):
        if validation_reset_reason not in {
            "execution_failed",
            "cancelled",
            "timed_out",
        }:
            raise InvalidStateTransition(
                "validating -> draft requires an execution terminal reset reason",
                validation_reset_reason=validation_reset_reason,
            )
    elif validation_reset_reason is not None:
        raise InvalidStateTransition(
            "validation reset reason is valid only for validating -> draft",
            current_status=current,
            target_status=target,
        )

    if "auto_apply_eligible" in edge and subject_kind != "patch":
        raise InvalidStateTransition(
            "auto_apply_eligible is valid only for patch subjects",
            subject_kind=subject_kind,
        )

    if edge == ("applied", "rolled_back"):
        if subject_kind not in {"patch", "constraint_proposal"}:
            raise InvalidStateTransition(
                "rollback_request ApprovalItems cannot enter rolled_back",
                subject_kind=subject_kind,
            )


__all__ = [
    "ALLOWED_STATUS_TRANSITIONS",
    "ValidationResetReason",
    "next_workflow_revision",
    "validate_status_transition",
]
