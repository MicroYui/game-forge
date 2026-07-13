from __future__ import annotations

from itertools import product

import pytest

from gameforge.contracts.errors import Conflict, InvalidStateTransition
from gameforge.contracts.workflow import ApprovalStatus
from gameforge.platform.approvals import (
    ALLOWED_STATUS_TRANSITIONS,
    next_workflow_revision,
    validate_status_transition,
)


STATUSES: tuple[ApprovalStatus, ...] = (
    "draft",
    "validating",
    "validation_failed",
    "validated",
    "pending_approval",
    "auto_apply_eligible",
    "approved",
    "changes_requested",
    "rejected",
    "applied",
    "rolled_back",
    "superseded",
)


def test_allowed_transition_table_is_frozen() -> None:
    assert ALLOWED_STATUS_TRANSITIONS == frozenset(
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


@pytest.mark.parametrize(("current", "target"), tuple(product(STATUSES, repeat=2)))
def test_only_frozen_status_edges_are_accepted(
    current: ApprovalStatus,
    target: ApprovalStatus,
) -> None:
    kwargs: dict[str, str] = {}
    if (current, target) == ("validating", "draft"):
        kwargs["validation_reset_reason"] = "execution_failed"
    if "auto_apply_eligible" in (current, target):
        kwargs["subject_kind"] = "patch"
    if (current, target) == ("applied", "rolled_back"):
        kwargs["subject_kind"] = "patch"

    if (current, target) in ALLOWED_STATUS_TRANSITIONS:
        validate_status_transition(current=current, target=target, **kwargs)  # type: ignore[arg-type]
    else:
        with pytest.raises(InvalidStateTransition, match="not allowed"):
            validate_status_transition(current=current, target=target, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "reason",
    ["execution_failed", "cancelled", "timed_out"],
)
def test_validating_can_return_to_draft_only_for_execution_terminal_reasons(
    reason: str,
) -> None:
    validate_status_transition(
        current="validating",
        target="draft",
        validation_reset_reason=reason,  # type: ignore[arg-type]
    )

    with pytest.raises(InvalidStateTransition, match="reset reason"):
        validate_status_transition(current="validating", target="draft")


def test_auto_apply_and_rolled_back_edges_enforce_subject_kind() -> None:
    validate_status_transition(
        current="validated",
        target="auto_apply_eligible",
        subject_kind="patch",
    )
    with pytest.raises(InvalidStateTransition, match="patch"):
        validate_status_transition(
            current="validated",
            target="auto_apply_eligible",
            subject_kind="constraint_proposal",
        )
    with pytest.raises(InvalidStateTransition, match="rollback_request"):
        validate_status_transition(
            current="applied",
            target="rolled_back",
            subject_kind="rollback_request",
        )
    with pytest.raises(InvalidStateTransition, match="rollback_request"):
        validate_status_transition(current="applied", target="rolled_back")


def test_workflow_revision_precondition_is_checked_before_increment() -> None:
    assert next_workflow_revision(actual=7, expected=7) == 8

    with pytest.raises(Conflict, match="workflow revision") as captured:
        next_workflow_revision(actual=7, expected=6)
    assert captured.value.context == {"actual_revision": 7, "expected_revision": 6}
