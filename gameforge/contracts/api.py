"""Versioned transport DTOs for the M4c HTTP, SSE, and WebSocket boundary."""

from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from gameforge.contracts.auth import *  # noqa: F403 - deliberate compatibility surface
from gameforge.contracts.auth import __all__ as _auth_exports
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.diff import SnapshotDiff, SnapshotDiffEntry
from gameforge.contracts.execution_profiles import ExecutionProfileViewV1
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import DomainScope, DomainScopeValue, Role
from gameforge.contracts.ir import Entity, Relation
from gameforge.contracts.jobs import (
    MAX_COLLECTION_ITEMS,
    MAX_JSON_BYTES,
    Problem,
    RunCommandAckV1,
    RunCommandProblemV1,
    RunEventEnvelope,
    RunStatus,
)
from gameforge.contracts.lineage import ArtifactKind, VersionTuple
from gameforge.contracts.playtest import TaskSuiteV1
from gameforge.contracts.review import ReviewReport
from gameforge.contracts.storage import MAX_PAGE_ITEMS, RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalStatus,
    ConstraintProposalV1,
    RollbackRequestV1,
)


BoundedId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
BoundedUrl = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
MAX_APPROVAL_REQUIREMENTS = 1024
OpaquePageCursor = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
T = TypeVar("T")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class OpaquePageV1(_FrozenModel, Generic[T]):
    """HTTP page envelope; internal signed cursor fields stay opaque to clients."""

    page_schema_version: Literal["page@1"] = "page@1"
    read_snapshot_id: BoundedId
    items: tuple[T, ...] = Field(max_length=MAX_PAGE_ITEMS)
    next_cursor: OpaquePageCursor | None = None
    expires_at: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class ArtifactSummaryV1(_FrozenModel):
    """Safe immutable Artifact projection without object-store coordinates or free-form meta."""

    summary_schema_version: Literal["artifact-summary@1"] = "artifact-summary@1"
    artifact_id: BoundedId
    lineage_schema_version: Literal["lineage@1", "lineage@2"]
    kind: ArtifactKind
    version_tuple: VersionTuple
    parent_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    payload_hash: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    payload_schema_id: BoundedId | None = None
    domain_scope: DomainScopeValue
    created_at: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None

    @model_validator(mode="after")
    def _canonical_parents(self) -> "ArtifactSummaryV1":
        parents = tuple(sorted(set(self.parent_artifact_ids)))
        if parents != self.parent_artifact_ids:
            raise ValueError("parent_artifact_ids must be stable-unique and sorted")
        if self.lineage_schema_version == "lineage@2" and (
            self.payload_hash is None
            or len(self.payload_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.payload_hash)
        ):
            raise ValueError("lineage@2 payload_hash must be a lowercase SHA-256 digest")
        return self


class ArtifactPayloadViewV1(_FrozenModel):
    """Verified, schema-bound payload for an already authorized Artifact."""

    view_schema_version: Literal["artifact-payload-view@1"] = "artifact-payload-view@1"
    artifact: ArtifactSummaryV1
    resource_revision: Literal[1] = 1
    payload: JsonValue

    @model_validator(mode="after")
    def _bounded_payload(self) -> "ArtifactPayloadViewV1":
        if len(canonical_json(self.payload).encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("artifact payload exceeds the API JSON bound")
        return self


class SpecViewV1(_FrozenModel):
    view_schema_version: Literal["spec-view@1"] = "spec-view@1"
    artifact: ArtifactSummaryV1
    snapshot_id: BoundedId
    schema_registry_version: BoundedId
    ref_name: BoundedId | None = None
    ref_value: RefValue | None = None

    @model_validator(mode="after")
    def _spec_binding(self) -> "SpecViewV1":
        if self.artifact.kind != "ir_snapshot":
            raise ValueError("SpecViewV1 requires an ir_snapshot Artifact")
        if (self.ref_name is None) != (self.ref_value is None):
            raise ValueError("ref_name and ref_value must be supplied together")
        if self.ref_value is not None and self.ref_value.artifact_id != self.artifact.artifact_id:
            raise ValueError("Spec ref_value must resolve to the projected Artifact")
        return self


class GraphItemV1(_FrozenModel):
    item_schema_version: Literal["graph-item@1"] = "graph-item@1"
    item_kind: Literal["entity", "relation"]
    item_id: BoundedId
    entity: Entity | None = None
    relation: Relation | None = None

    @model_validator(mode="after")
    def _closed_item(self) -> "GraphItemV1":
        selected = self.entity if self.item_kind == "entity" else self.relation
        other = self.relation if self.item_kind == "entity" else self.entity
        if selected is None or other is not None or selected.id != self.item_id:
            raise ValueError("graph item discriminator and payload must agree")
        return self


class SchemaRegistryDocumentV1(_FrozenModel):
    registry_schema_version: Literal["schema-registry-document@1"] = "schema-registry-document@1"
    registry_version: BoundedId
    schemas: dict[BoundedId, JsonValue] = Field(max_length=MAX_COLLECTION_ITEMS)
    registry_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def _bounded_registry(self) -> "SchemaRegistryDocumentV1":
        if len(canonical_json(self.schemas).encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("schema registry document exceeds the API JSON bound")
        return self


class ConstraintSnapshotViewV1(_FrozenModel):
    view_schema_version: Literal["constraint-snapshot-view@1"] = "constraint-snapshot-view@1"
    artifact: ArtifactSummaryV1
    dsl_grammar_version: BoundedId
    constraints: tuple[JsonValue, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @model_validator(mode="after")
    def _constraint_artifact(self) -> "ConstraintSnapshotViewV1":
        if self.artifact.kind != "constraint_snapshot":
            raise ValueError("ConstraintSnapshotViewV1 requires a constraint_snapshot Artifact")
        if len(canonical_json(self.constraints).encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("constraint snapshot exceeds the API JSON bound")
        return self


class ReviewArtifactViewV1(_FrozenModel):
    view_schema_version: Literal["review-artifact-view@1"] = "review-artifact-view@1"
    artifact: ArtifactSummaryV1
    report: ReviewReport

    @model_validator(mode="after")
    def _review_artifact(self) -> "ReviewArtifactViewV1":
        if self.artifact.kind != "review_report":
            raise ValueError("ReviewArtifactViewV1 requires a review_report Artifact")
        if (
            len(canonical_json(self.report.model_dump(mode="json")).encode("utf-8"))
            > MAX_JSON_BYTES
        ):
            raise ValueError("review report exceeds the API JSON bound")
        return self


class TaskSuiteArtifactViewV1(_FrozenModel):
    view_schema_version: Literal["task-suite-artifact-view@1"] = "task-suite-artifact-view@1"
    artifact: ArtifactSummaryV1
    task_suite: TaskSuiteV1

    @model_validator(mode="after")
    def _task_suite_artifact(self) -> "TaskSuiteArtifactViewV1":
        if self.artifact.kind != "task_suite":
            raise ValueError("TaskSuiteArtifactViewV1 requires a task_suite Artifact")
        return self


class ConstraintProposalReadViewV1(_FrozenModel):
    view_schema_version: Literal["constraint-proposal-read-view@1"] = (
        "constraint-proposal-read-view@1"
    )
    artifact: ArtifactSummaryV1
    proposal: ConstraintProposalV1
    workflow_revision: PositiveInt
    approval_status: BoundedId


class PatchArtifactReadViewV1(_FrozenModel):
    """Workflow Patch projection with its stable Artifact identity."""

    view_schema_version: Literal["patch-artifact-read-view@1"] = "patch-artifact-read-view@1"
    artifact: ArtifactSummaryV1
    patch: PatchV2
    validation_status: BoundedId
    regression_status: BoundedId
    approval_status: ApprovalStatus
    workflow_revision: PositiveInt

    @model_validator(mode="after")
    def _patch_artifact(self) -> "PatchArtifactReadViewV1":
        if (
            self.artifact.lineage_schema_version != "lineage@2"
            or self.artifact.kind != "patch"
            or self.artifact.payload_schema_id != "patch@2"
        ):
            raise ValueError("Patch read view requires a patch@2 ArtifactV2")
        return self


class RollbackRequestReadViewV1(_FrozenModel):
    """Rollback workflow projection with its stable Artifact identity."""

    view_schema_version: Literal["rollback-request-read-view@1"] = "rollback-request-read-view@1"
    artifact: ArtifactSummaryV1
    request: RollbackRequestV1
    workflow_revision: PositiveInt
    approval_status: ApprovalStatus

    @model_validator(mode="after")
    def _rollback_artifact(self) -> "RollbackRequestReadViewV1":
        if (
            self.artifact.lineage_schema_version != "lineage@2"
            or self.artifact.kind != "rollback_request"
            or self.artifact.payload_schema_id != "rollback-request@1"
        ):
            raise ValueError("Rollback read view requires a rollback-request@1 ArtifactV2")
        return self


class SnapshotDiffHttpPageV1(_FrozenModel):
    page_schema_version: Literal["snapshot-diff-http-page@1"] = "snapshot-diff-http-page@1"
    diff: SnapshotDiff
    page: OpaquePageV1[SnapshotDiffEntry]


class LineageEntryV1(_FrozenModel):
    entry_schema_version: Literal["lineage-entry@1"] = "lineage-entry@1"
    artifact: ArtifactSummaryV1
    depth: NonNegativeInt


class RefHistoryEntryV1(_FrozenModel):
    entry_schema_version: Literal["ref-history-entry@1"] = "ref-history-entry@1"
    ref_name: BoundedId
    value: RefValue


class ExecutionProfileReadViewV1(_FrozenModel):
    view_schema_version: Literal["execution-profile-read-view@1"] = "execution-profile-read-view@1"
    profile: ExecutionProfileViewV1
    catalog_version: PositiveInt
    catalog_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


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
        distinct = {
            requirement_id: set(requirement.distinct_from_requirement_ids)
            for requirement_id, requirement in requirements.items()
        }
        for requirement_id, requirement in requirements.items():
            for other_id in requirement.distinct_from_requirement_ids:
                distinct[other_id].add(requirement_id)
        for item in progress:
            requirement = requirements[item.requirement_id]
            if (
                item.domain_scope != requirement.domain_scope
                or item.route_role != requirement.route_role
                or item.min_approvals != requirement.min_approvals
            ):
                raise ValueError("approval progress differs from its frozen requirement")
            unknown_distinct = (
                set(item.unmet_distinct_from_requirement_ids) - distinct[item.requirement_id]
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
    "ArtifactPayloadViewV1",
    "ArtifactSummaryV1",
    "ConstraintProposalReadViewV1",
    "ConstraintSnapshotViewV1",
    "ExecutionProfileReadViewV1",
    "GraphItemV1",
    "LineageEntryV1",
    "OpaquePageCursor",
    "OpaquePageV1",
    "PatchArtifactReadViewV1",
    "Problem",
    "RefHistoryEntryV1",
    "RollbackRequestReadViewV1",
    "ReviewArtifactViewV1",
    "RunAcceptedV1",
    "RunCommandAckV1",
    "RunCommandProblemV1",
    "RunCommandServerFrame",
    "RunEventEnvelope",
    "RunViewV1",
    "SchemaRegistryDocumentV1",
    "SnapshotDiffHttpPageV1",
    "SpecViewV1",
    "TaskSuiteArtifactViewV1",
    "encode_sse_event",
]
