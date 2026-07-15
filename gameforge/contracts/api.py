"""Versioned transport DTOs for the M4c HTTP, SSE, and WebSocket boundary."""

from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from gameforge.contracts.auth import *  # noqa: F403 - deliberate compatibility surface
from gameforge.contracts.auth import __all__ as _auth_exports
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.diff import (
    ConflictResolution,
    RebaseResult,
    SnapshotDiff,
    SnapshotDiffEntry,
)
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import ExecutionProfileViewV1, ProfileRefV1
from gameforge.contracts.findings import PatchV2, TypedOp
from gameforge.contracts.identity import DomainScope, DomainScopeValue, Role
from gameforge.contracts.ir import Entity, Relation
from gameforge.contracts.jobs import (
    MAX_COLLECTION_ITEMS,
    MAX_JSON_BYTES,
    ExecutionVersionPlanV1,
    FindingEvidenceBindingV1,
    PatchRepairPayloadV1,
    PlaytestRunPayloadV1,
    Problem,
    RefReadBindingV1,
    RunCommandAckV1,
    RunCommandProblemV1,
    RunEventEnvelope,
    RunKindPayload,
    RunStatus,
    SolverEngineRefV1,
    TaskSuiteDerivePayloadV1,
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


def _bounded_request_payload(value: BaseModel) -> None:
    if len(canonical_json(value.model_dump(mode="json")).encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("request payload exceeds the API JSON bound")


def _stable_unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _stable_unique_profiles(values: tuple[ProfileRefV1, ...]) -> tuple[ProfileRefV1, ...]:
    by_value = {canonical_json(value.model_dump(mode="json")): value for value in values}
    return tuple(by_value[key] for key in sorted(by_value))


class HumanSpecUploadRequestV1(_FrozenModel):
    request_schema_version: Literal["human-spec-upload-request@1"] = "human-spec-upload-request@1"
    ref_name: BoundedId
    expected_ref: RefValue | None
    schema_registry_version: BoundedId
    meta_schema_version: BoundedId
    domain_scope: DomainScope
    content_payload: dict[BoundedId, JsonValue] = Field(max_length=MAX_COLLECTION_ITEMS)

    @model_validator(mode="after")
    def _bounded(self) -> "HumanSpecUploadRequestV1":
        _bounded_request_payload(self)
        return self


class HumanPatchDraftRequestV1(_FrozenModel):
    request_schema_version: Literal["human-patch-draft-request@1"] = "human-patch-draft-request@1"
    base_snapshot_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId | None = None
    ref_name: BoundedId
    expected_ref: RefValue | None
    expected_to_fix: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    preconditions: tuple[dict[str, JsonValue], ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    side_effect_risk: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    ops: tuple[TypedOp, ...] = Field(min_length=1, max_length=MAX_COLLECTION_ITEMS)
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=16384)]
    candidate_export_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @model_validator(mode="after")
    def _canonical_and_bounded(self) -> "HumanPatchDraftRequestV1":
        object.__setattr__(self, "expected_to_fix", _stable_unique_strings(self.expected_to_fix))
        object.__setattr__(
            self,
            "candidate_export_profiles",
            _stable_unique_profiles(self.candidate_export_profiles),
        )
        operation_ids = [operation.op_id for operation in self.ops]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("patch operation ids must be unique")
        if self.candidate_export_profiles and self.constraint_snapshot_artifact_id is None:
            raise ValueError("candidate export profiles require a constraint snapshot")
        _bounded_request_payload(self)
        return self


class HumanConstraintDraftRequestV1(_FrozenModel):
    request_schema_version: Literal["human-constraint-draft-request@1"] = (
        "human-constraint-draft-request@1"
    )
    base_constraint_snapshot_artifact_id: BoundedId | None = None
    ref_name: BoundedId
    expected_ref: RefValue | None
    dsl_grammar_version: BoundedId
    domain_scope: DomainScope
    constraints: tuple[Constraint, ...] = Field(min_length=1, max_length=MAX_COLLECTION_ITEMS)
    source_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=16384)]

    @model_validator(mode="after")
    def _canonical_and_bounded(self) -> "HumanConstraintDraftRequestV1":
        constraint_ids = [constraint.id for constraint in self.constraints]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("constraint ids must be unique")
        if any(
            constraint.dsl_grammar_version != self.dsl_grammar_version
            for constraint in self.constraints
        ):
            raise ValueError("constraint grammar versions must match the request")
        object.__setattr__(
            self, "constraints", tuple(sorted(self.constraints, key=lambda item: item.id))
        )
        object.__setattr__(
            self,
            "source_artifact_ids",
            _stable_unique_strings(self.source_artifact_ids),
        )
        _bounded_request_payload(self)
        return self


class HumanConstraintRevisionRequestV1(HumanConstraintDraftRequestV1):
    request_schema_version: Literal["human-constraint-revision-request@1"] = (
        "human-constraint-revision-request@1"
    )
    approval_id: BoundedId
    expected_subject_head_revision: PositiveInt
    expected_workflow_revision: PositiveInt


class PatchValidationAdmissionRequestV1(_FrozenModel):
    request_schema_version: Literal["patch-validation-admission-request@1"] = (
        "patch-validation-admission-request@1"
    )
    approval_id: BoundedId
    expected_subject_head_revision: PositiveInt
    expected_workflow_revision: PositiveInt
    subject_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    base_snapshot_artifact_id: BoundedId
    preview_snapshot_artifact_id: BoundedId
    candidate_config_export_artifact_ids: tuple[BoundedId, ...] = Field(
        max_length=MAX_COLLECTION_ITEMS
    )
    target: RefReadBindingV1
    validation_policy: ProfileRefV1
    checker_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    simulation_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    findings: tuple[FindingEvidenceBindingV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    review_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    playtest_trace_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    regression_suite_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @model_validator(mode="after")
    def _canonical_and_bounded(self) -> "PatchValidationAdmissionRequestV1":
        for field_name in (
            "candidate_config_export_artifact_ids",
            "review_artifact_ids",
            "playtest_trace_artifact_ids",
            "regression_suite_artifact_ids",
        ):
            object.__setattr__(self, field_name, _stable_unique_strings(getattr(self, field_name)))
        for field_name in ("checker_profiles", "simulation_profiles"):
            object.__setattr__(self, field_name, _stable_unique_profiles(getattr(self, field_name)))
        finding_keys = [(item.finding_id, item.finding_revision) for item in self.findings]
        if len(finding_keys) != len(set(finding_keys)):
            raise ValueError("finding bindings must identify unique revisions")
        object.__setattr__(
            self,
            "findings",
            tuple(sorted(self.findings, key=lambda item: (item.finding_id, item.finding_revision))),
        )
        _bounded_request_payload(self)
        return self


class ConstraintValidationAdmissionRequestV1(_FrozenModel):
    request_schema_version: Literal["constraint-validation-admission-request@1"] = (
        "constraint-validation-admission-request@1"
    )
    approval_id: BoundedId
    expected_subject_head_revision: PositiveInt
    expected_workflow_revision: PositiveInt
    subject_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    base_constraint_snapshot_artifact_id: BoundedId | None = None
    target: RefReadBindingV1
    dsl_grammar_version: BoundedId
    compiler_profile: ProfileRefV1
    differential_engines: tuple[SolverEngineRefV1, ...] = Field(
        min_length=2,
        max_length=MAX_COLLECTION_ITEMS,
    )
    golden_suite_artifact_id: BoundedId | None = None
    regression_suite_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    validation_policy: ProfileRefV1

    @model_validator(mode="after")
    def _canonical_and_bounded(self) -> "ConstraintValidationAdmissionRequestV1":
        engine_keys = [(item.engine_id, item.version) for item in self.differential_engines]
        if len(engine_keys) != len(set(engine_keys)):
            raise ValueError("differential engine refs must be unique")
        object.__setattr__(
            self,
            "differential_engines",
            tuple(
                sorted(self.differential_engines, key=lambda item: (item.engine_id, item.version))
            ),
        )
        object.__setattr__(
            self,
            "regression_suite_artifact_ids",
            _stable_unique_strings(self.regression_suite_artifact_ids),
        )
        _bounded_request_payload(self)
        return self


class RollbackValidationAdmissionRequestV1(_FrozenModel):
    request_schema_version: Literal["rollback-validation-admission-request@1"] = (
        "rollback-validation-admission-request@1"
    )
    approval_id: BoundedId
    expected_subject_head_revision: PositiveInt
    expected_workflow_revision: PositiveInt
    subject_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    ref_name: BoundedId
    expected_current_ref: RefValue
    target_artifact_id: BoundedId
    target_history_revision: PositiveInt
    rollback_profile: ProfileRefV1
    schema_compatibility_policy: ProfileRefV1
    impact_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    regression_suite_artifact_ids: tuple[BoundedId, ...] = Field(max_length=MAX_COLLECTION_ITEMS)

    @model_validator(mode="after")
    def _canonical_and_bounded(self) -> "RollbackValidationAdmissionRequestV1":
        object.__setattr__(self, "impact_profiles", _stable_unique_profiles(self.impact_profiles))
        object.__setattr__(
            self,
            "regression_suite_artifact_ids",
            _stable_unique_strings(self.regression_suite_artifact_ids),
        )
        _bounded_request_payload(self)
        return self


_BOUNDED_GOAL_TEXT = Annotated[str, StringConstraints(min_length=1, max_length=16384)]


class RunSubmissionRequestV1(_FrozenModel):
    """Generic ``POST /runs`` body. Only ``generic_runs_endpoint`` kinds are admitted.

    The typed ``params`` fixes the RunKind; resource-only or internal-only kinds are
    rejected by admission, never by a client-supplied kind string.
    """

    request_schema_version: Literal["run-submission-request@1"] = "run-submission-request@1"
    params: RunKindPayload
    llm_execution_mode: Literal["not_applicable", "live", "record", "replay"] = "not_applicable"
    seed: Annotated[int, Field(ge=0, le=18_446_744_073_709_551_615)] | None = None
    execution_version_plan: ExecutionVersionPlanV1 | None = None
    cassette_artifact_id: BoundedId | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "RunSubmissionRequestV1":
        _bounded_request_payload(self)
        return self


class GenerationProposeRequestV1(_FrozenModel):
    """``POST /generations:propose`` — fixes ``generation.propose@1``.

    The naked ``objective_goal_text`` is turned into an authenticated ``source_raw``
    Artifact by the composition root before the Run is created.
    """

    request_schema_version: Literal["generation-propose-request@1"] = "generation-propose-request@1"
    base_snapshot_artifact_id: BoundedId
    constraint_snapshot_artifact_id: BoundedId | None = None
    findings: tuple[FindingEvidenceBindingV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    objective_goal_text: _BOUNDED_GOAL_TEXT
    domain_scope: DomainScope
    target: RefReadBindingV1
    generation_policy: ProfileRefV1
    candidate_export_profiles: tuple[ProfileRefV1, ...] = Field(max_length=MAX_COLLECTION_ITEMS)
    llm_execution_mode: Literal["live", "record", "replay"] = "record"
    execution_version_plan: ExecutionVersionPlanV1 | None = None
    cassette_artifact_id: BoundedId | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "GenerationProposeRequestV1":
        object.__setattr__(
            self,
            "candidate_export_profiles",
            _stable_unique_profiles(self.candidate_export_profiles),
        )
        _bounded_request_payload(self)
        return self


class ConstraintProposeRequestV1(_FrozenModel):
    """``POST /constraints:propose`` — fixes ``constraint_proposal.propose@1``."""

    request_schema_version: Literal["constraint-propose-request@1"] = "constraint-propose-request@1"
    source_artifact_ids: tuple[BoundedId, ...] = Field(
        min_length=1, max_length=MAX_COLLECTION_ITEMS
    )
    base_constraint_snapshot_artifact_id: BoundedId | None = None
    authoring_goal_text: _BOUNDED_GOAL_TEXT
    domain_scope: DomainScope
    dsl_grammar_version: BoundedId
    extraction_policy: ProfileRefV1
    llm_execution_mode: Literal["live", "record", "replay"] = "record"
    execution_version_plan: ExecutionVersionPlanV1 | None = None
    cassette_artifact_id: BoundedId | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "ConstraintProposeRequestV1":
        object.__setattr__(
            self, "source_artifact_ids", _stable_unique_strings(self.source_artifact_ids)
        )
        _bounded_request_payload(self)
        return self


class PatchRepairRequestV1(_FrozenModel):
    """``POST /patches/{id}:repair`` — fixes ``patch.repair@1`` via typed params."""

    request_schema_version: Literal["patch-repair-request@1"] = "patch-repair-request@1"
    params: PatchRepairPayloadV1
    llm_execution_mode: Literal["live", "record", "replay"] = "record"
    seed: Annotated[int, Field(ge=0, le=18_446_744_073_709_551_615)] | None = None
    execution_version_plan: ExecutionVersionPlanV1 | None = None
    cassette_artifact_id: BoundedId | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "PatchRepairRequestV1":
        _bounded_request_payload(self)
        return self


class TaskSuiteDeriveRequestV1(_FrozenModel):
    """``POST /task-suites:derive`` — fixes ``task_suite.derive@1`` via typed params."""

    request_schema_version: Literal["task-suite-derive-request@1"] = "task-suite-derive-request@1"
    params: TaskSuiteDerivePayloadV1

    @model_validator(mode="after")
    def _bounded(self) -> "TaskSuiteDeriveRequestV1":
        _bounded_request_payload(self)
        return self


class PlaytestRunRequestV1(_FrozenModel):
    """``POST /playtest:run`` — fixes ``playtest.run@1`` via typed params."""

    request_schema_version: Literal["playtest-run-request@1"] = "playtest-run-request@1"
    params: PlaytestRunPayloadV1
    llm_execution_mode: Literal["live", "record", "replay"] = "record"
    seed: Annotated[int, Field(ge=0, le=18_446_744_073_709_551_615)]
    execution_version_plan: ExecutionVersionPlanV1 | None = None
    cassette_artifact_id: BoundedId | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "PlaytestRunRequestV1":
        _bounded_request_payload(self)
        return self


class SubmitForApprovalRequestV1(_FrozenModel):
    request_schema_version: Literal["submit-for-approval-request@1"] = (
        "submit-for-approval-request@1"
    )
    approval_id: BoundedId
    expected_workflow_revision: PositiveInt


class WorkflowApplyRequestV1(_FrozenModel):
    request_schema_version: Literal["workflow-apply-request@1"] = "workflow-apply-request@1"
    approval_id: BoundedId
    expected_workflow_revision: PositiveInt
    subject_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    target_artifact_id: BoundedId
    target_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    ref_name: BoundedId
    expected_ref: RefValue | None


class PatchRebaseRequestV1(_FrozenModel):
    request_schema_version: Literal["patch-rebase-request@1"] = "patch-rebase-request@1"
    approval_id: BoundedId
    expected_subject_head_revision: PositiveInt
    expected_workflow_revision: PositiveInt
    ref_name: BoundedId
    expected_ref: RefValue


class ResolveConflictsRequestV1(PatchRebaseRequestV1):
    request_schema_version: Literal["resolve-conflicts-request@1"] = "resolve-conflicts-request@1"
    conflict_set_id: BoundedId
    resolutions: tuple[ConflictResolution, ...] = Field(
        min_length=1,
        max_length=MAX_COLLECTION_ITEMS,
    )

    @model_validator(mode="after")
    def _canonical_resolutions(self) -> "ResolveConflictsRequestV1":
        ids = [item.conflict_id for item in self.resolutions]
        if len(ids) != len(set(ids)):
            raise ValueError("conflict resolutions must identify unique conflicts")
        object.__setattr__(
            self,
            "resolutions",
            tuple(sorted(self.resolutions, key=lambda item: item.conflict_id)),
        )
        _bounded_request_payload(self)
        return self


class RollbackDraftRequestV1(_FrozenModel):
    request_schema_version: Literal["rollback-draft-request@1"] = "rollback-draft-request@1"
    expected_current_ref: RefValue
    target_artifact_id: BoundedId
    target_history_revision: PositiveInt
    rollback_profile: ProfileRefV1
    reason: Annotated[str, StringConstraints(min_length=1, max_length=16384)]
    reverses_approval_id: BoundedId | None = None


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


class WorkflowApplyResultV1(_FrozenModel):
    result_schema_version: Literal["workflow-apply-result@1"] = "workflow-apply-result@1"
    approval: ApprovalViewV1
    ref_name: BoundedId
    ref_value: RefValue
    ref_transition_id: BoundedId | None = None
    reversed_approval_id: BoundedId | None = None

    @model_validator(mode="after")
    def _subject_shape(self) -> "WorkflowApplyResultV1":
        is_rollback = self.approval.approval.subject_kind == "rollback_request"
        if is_rollback != (self.ref_transition_id is not None):
            raise ValueError("rollback apply requires exactly one ref transition id")
        if not is_rollback and self.reversed_approval_id is not None:
            raise ValueError("only rollback apply may identify a reversed approval")
        return self


WorkflowCommandPayloadV1: TypeAlias = (
    HumanSpecUploadRequestV1
    | HumanPatchDraftRequestV1
    | HumanConstraintDraftRequestV1
    | HumanConstraintRevisionRequestV1
    | PatchValidationAdmissionRequestV1
    | ConstraintValidationAdmissionRequestV1
    | RollbackValidationAdmissionRequestV1
    | SubmitForApprovalRequestV1
    | ApprovalDecisionRequestV1
    | WorkflowApplyRequestV1
    | PatchRebaseRequestV1
    | ResolveConflictsRequestV1
    | RollbackDraftRequestV1
)

WorkflowCommandResponseV1: TypeAlias = (
    SpecViewV1
    | PatchArtifactReadViewV1
    | ConstraintProposalReadViewV1
    | RollbackRequestReadViewV1
    | RunAcceptedV1
    | ApprovalViewV1
    | WorkflowApplyResultV1
    | RebaseResult
)


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
    "ConstraintProposeRequestV1",
    "ConstraintSnapshotViewV1",
    "ExecutionProfileReadViewV1",
    "GenerationProposeRequestV1",
    "GraphItemV1",
    "HumanConstraintDraftRequestV1",
    "HumanConstraintRevisionRequestV1",
    "HumanPatchDraftRequestV1",
    "HumanSpecUploadRequestV1",
    "LineageEntryV1",
    "OpaquePageCursor",
    "OpaquePageV1",
    "PatchArtifactReadViewV1",
    "PatchRebaseRequestV1",
    "PatchRepairRequestV1",
    "PatchValidationAdmissionRequestV1",
    "PlaytestRunRequestV1",
    "Problem",
    "RefHistoryEntryV1",
    "ResolveConflictsRequestV1",
    "RollbackDraftRequestV1",
    "RollbackRequestReadViewV1",
    "RollbackValidationAdmissionRequestV1",
    "ReviewArtifactViewV1",
    "RunAcceptedV1",
    "RunCommandAckV1",
    "RunCommandProblemV1",
    "RunCommandServerFrame",
    "RunEventEnvelope",
    "RunSubmissionRequestV1",
    "RunViewV1",
    "SchemaRegistryDocumentV1",
    "SnapshotDiffHttpPageV1",
    "SpecViewV1",
    "SubmitForApprovalRequestV1",
    "TaskSuiteArtifactViewV1",
    "TaskSuiteDeriveRequestV1",
    "ConstraintValidationAdmissionRequestV1",
    "WorkflowApplyRequestV1",
    "WorkflowApplyResultV1",
    "WorkflowCommandPayloadV1",
    "WorkflowCommandResponseV1",
    "encode_sse_event",
]
