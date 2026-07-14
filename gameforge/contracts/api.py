"""Versioned transport DTOs for the M4c HTTP, SSE, and WebSocket boundary."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gameforge.contracts.auth import *  # noqa: F403 - deliberate compatibility surface
from gameforge.contracts.auth import __all__ as _auth_exports
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.identity import DomainScope, Role
from gameforge.contracts.jobs import (
    Problem,
    RunCommandAckV1,
    RunCommandProblemV1,
    RunEventEnvelope,
    RunStatus,
)
from gameforge.contracts.workflow import ApprovalItem


BoundedId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
BoundedUrl = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
MAX_APPROVAL_REQUIREMENTS = 1024


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RunAcceptedV1(_FrozenModel):
    accepted_schema_version: Literal["run-accepted@1"] = "run-accepted@1"
    run_id: BoundedId
    status_url: BoundedUrl
    events_url: BoundedUrl


class RunViewV1(_FrozenModel):
    view_schema_version: Literal["run-view@1"] = "run-view@1"
    run_id: BoundedId
    status: RunStatus
    revision: PositiveInt
    attempt_no: PositiveInt | None = None
    result_artifact_id: BoundedId | None = None
    failure_artifact_id: BoundedId | None = None
    terminal_cassette_artifact_id: BoundedId | None = None
    status_url: BoundedUrl
    events_url: BoundedUrl

    @model_validator(mode="after")
    def _terminal_projection(self) -> "RunViewV1":
        terminal = self.status in {"succeeded", "failed", "cancelled", "timed_out"}
        if self.status == "succeeded":
            if self.result_artifact_id is None or self.failure_artifact_id is not None:
                raise ValueError("succeeded Run requires only result_artifact_id")
            if self.attempt_no is None:
                raise ValueError("succeeded Run requires attempt_no")
        elif self.status in {"failed", "cancelled", "timed_out"}:
            if self.failure_artifact_id is None or self.result_artifact_id is not None:
                raise ValueError("non-success terminal Run requires only failure_artifact_id")
        elif self.result_artifact_id is not None or self.failure_artifact_id is not None:
            raise ValueError("nonterminal Run cannot expose a terminal Artifact")
        if not terminal and self.terminal_cassette_artifact_id is not None:
            raise ValueError("nonterminal Run cannot expose terminal_cassette_artifact_id")

        if self.status in {"leased", "running", "retry_wait"} and self.attempt_no is None:
            raise ValueError(f"{self.status} Run requires attempt_no")
        if self.status == "queued" and self.attempt_no is not None:
            raise ValueError("queued Run cannot expose an attempt number")
        return self


class ApprovalDecisionRequestV1(_FrozenModel):
    request_schema_version: Literal["approval-decision-request@1"] = "approval-decision-request@1"
    decision: Literal["approve", "reject", "request_changes"]
    requirement_ids: tuple[BoundedId, ...] = Field(
        min_length=1,
        max_length=MAX_APPROVAL_REQUIREMENTS,
    )
    expected_workflow_revision: PositiveInt
    reason_code: BoundedId
    comment: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None

    @model_validator(mode="after")
    def _canonical_requirements(self) -> "ApprovalDecisionRequestV1":
        canonical = tuple(sorted(set(self.requirement_ids)))
        object.__setattr__(self, "requirement_ids", canonical)
        return self


class ApprovalRequirementProgressV1(_FrozenModel):
    requirement_id: BoundedId
    domain_scope: DomainScope
    route_role: Role
    min_approvals: PositiveInt
    valid_approval_count: NonNegativeInt
    satisfied: bool
    eligible_for_current_actor: bool
    unmet_distinct_from_requirement_ids: tuple[BoundedId, ...] = Field(
        max_length=MAX_APPROVAL_REQUIREMENTS,
    )

    @model_validator(mode="after")
    def _canonical_projection(self) -> "ApprovalRequirementProgressV1":
        object.__setattr__(
            self,
            "unmet_distinct_from_requirement_ids",
            tuple(sorted(set(self.unmet_distinct_from_requirement_ids))),
        )
        if self.satisfied != (self.valid_approval_count >= self.min_approvals):
            raise ValueError("satisfied must match the valid approval threshold")
        return self


class ApprovalViewV1(_FrozenModel):
    view_schema_version: Literal["approval-view@1"] = "approval-view@1"
    approval: ApprovalItem
    requirement_progress: tuple[ApprovalRequirementProgressV1, ...] = Field(
        max_length=MAX_APPROVAL_REQUIREMENTS,
    )
    current_actor_allowed_requirement_ids: tuple[BoundedId, ...] = Field(
        max_length=MAX_APPROVAL_REQUIREMENTS,
    )

    @model_validator(mode="after")
    def _exact_projection(self) -> "ApprovalViewV1":
        progress = tuple(sorted(self.requirement_progress, key=lambda item: item.requirement_id))
        progress_ids = [item.requirement_id for item in progress]
        if len(progress_ids) != len(set(progress_ids)):
            raise ValueError("approval progress requirement ids must be unique")
        requirements = {item.requirement_id: item for item in self.approval.requirements}
        if set(progress_ids) != set(requirements):
            raise ValueError("approval progress must exactly cover frozen requirements")
        for item in progress:
            requirement = requirements[item.requirement_id]
            if (
                item.domain_scope != requirement.domain_scope
                or item.route_role != requirement.route_role
                or item.min_approvals != requirement.min_approvals
            ):
                raise ValueError("approval progress differs from its frozen requirement")
            unknown_distinct = set(item.unmet_distinct_from_requirement_ids) - set(
                requirement.distinct_from_requirement_ids
            )
            if unknown_distinct:
                raise ValueError("approval progress contains an unknown distinct requirement")

        allowed = tuple(sorted(set(self.current_actor_allowed_requirement_ids)))
        eligible = tuple(
            item.requirement_id for item in progress if item.eligible_for_current_actor
        )
        if allowed != eligible:
            raise ValueError("allowed requirement ids must match the eligibility projection")
        object.__setattr__(self, "requirement_progress", progress)
        object.__setattr__(self, "current_actor_allowed_requirement_ids", allowed)
        return self


RunCommandServerFrame: TypeAlias = RunCommandAckV1 | RunCommandProblemV1


def encode_sse_event(event: RunEventEnvelope) -> str:
    """Encode one committed RunEvent using the frozen canonical SSE framing."""

    return (
        f"id:{event.seq}\n"
        f"event:{event.event_type}\n"
        f"data:{canonical_json(event.model_dump(mode='json'))}\n\n"
    )


__all__ = [
    *_auth_exports,
    "ApprovalDecisionRequestV1",
    "ApprovalRequirementProgressV1",
    "ApprovalViewV1",
    "Problem",
    "RunAcceptedV1",
    "RunCommandAckV1",
    "RunCommandProblemV1",
    "RunCommandServerFrame",
    "RunEventEnvelope",
    "RunViewV1",
    "encode_sse_event",
]
