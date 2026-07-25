"""Project-first authoring wire contracts.

Projects are platform resources that bind immutable GameForge authorities.  They
never become IR entities and never replace Artifact/Ref/Run/ApprovalItem truth.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.ir import Entity, Relation
from gameforge.contracts.jobs import ExecutionVersionPlanV1
from gameforge.contracts.storage import RefValue


MAX_PROJECT_DESCRIPTION_CHARS = 4096
MAX_PROJECT_MATERIAL_BYTES = 8 * 1024 * 1024
MAX_PROJECT_MATERIAL_TEXT_CHARS = 1_048_576
MAX_PROJECT_MATERIALS_PER_EXTRACTION = 64
MAX_PROJECT_GRAPH_ITEMS = 32_768

BoundedId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
BoundedName = Annotated[str, StringConstraints(min_length=1, max_length=256)]
BoundedDescription = Annotated[str, StringConstraints(max_length=MAX_PROJECT_DESCRIPTION_CHARS)]
BoundedGenre = Annotated[str, StringConstraints(max_length=128)]
PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
ProjectKey = Annotated[
    str,
    StringConstraints(min_length=2, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]
MaterialSourceFormat = Literal[
    "plain_text",
    "markdown",
    "html",
    "feishu_blocks_json",
    "docx",
    "xlsx",
    "csv",
]
PlanningContentScope = Literal[
    "auto",
    "game_foundation",
    "permanent_feature",
    "limited_event",
    "live_update",
]
ProjectExtractionStatus = Literal[
    "queued",
    "running",
    "needs_resolution",
    "ready",
    "failed",
]

_PROJECT_ID_RE = re.compile(r"^project:[A-Za-z0-9][A-Za-z0-9._:-]{0,503}$")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class GameProjectV1(_FrozenModel):
    project_schema_version: Literal["game-project@1"] = "game-project@1"
    project_id: BoundedId
    project_key: ProjectKey
    display_name: BoundedName
    description: BoundedDescription = ""
    genre: BoundedGenre = ""
    status: Literal["draft", "active", "archived"]
    domain_scope: DomainScope
    bootstrap_snapshot_artifact_id: BoundedId
    content_ref_name: BoundedId
    constraint_ref_name: BoundedId
    current_content_ref: RefValue | None = None
    current_constraint_ref: RefValue | None = None
    latest_extraction_id: BoundedId | None = None
    latest_patch_artifact_id: BoundedId | None = None
    latest_approval_id: BoundedId | None = None
    created_by: BoundedId
    created_at: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    updated_at: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    revision: PositiveInt

    @model_validator(mode="after")
    def _authority_shape(self) -> "GameProjectV1":
        if _PROJECT_ID_RE.fullmatch(self.project_id) is None:
            raise ValueError("project_id must use the project: namespace")
        expected_content = f"projects/{self.project_id}/content/head"
        expected_constraints = f"projects/{self.project_id}/constraints/head"
        if self.content_ref_name != expected_content:
            raise ValueError("content_ref_name does not match the project namespace")
        if self.constraint_ref_name != expected_constraints:
            raise ValueError("constraint_ref_name does not match the project namespace")
        if self.status == "draft" and self.current_content_ref is not None:
            raise ValueError("draft project cannot project a published content ref")
        if self.status == "active" and self.current_content_ref is None:
            raise ValueError("active project requires a published content ref")
        if self.updated_at < self.created_at:
            raise ValueError("project updated_at cannot precede created_at")
        return self


class ProjectCreateRequestV1(_FrozenModel):
    request_schema_version: Literal["project-create-request@1"] = "project-create-request@1"
    project_key: ProjectKey
    display_name: BoundedName
    description: BoundedDescription = ""
    genre: BoundedGenre = ""
    domain_scope: DomainScope

    @field_validator("display_name")
    @classmethod
    def _trim_display_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("display_name cannot be blank")
        return stripped


class ProjectUpdateRequestV1(_FrozenModel):
    request_schema_version: Literal["project-update-request@1"] = "project-update-request@1"
    display_name: BoundedName
    description: BoundedDescription
    genre: BoundedGenre
    domain_scope: DomainScope
    expected_revision: PositiveInt


class ProjectArchiveRequestV1(_FrozenModel):
    request_schema_version: Literal["project-archive-request@1"] = "project-archive-request@1"
    expected_revision: PositiveInt
    reason: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


def _canonical_text(value: str) -> str:
    rendered = value.replace("\r\n", "\n").replace("\r", "\n")
    if not rendered.strip():
        raise ValueError("material text cannot be blank")
    return rendered


class ProjectMaterialTextRequestV1(_FrozenModel):
    request_schema_version: Literal["project-material-text-request@1"] = (
        "project-material-text-request@1"
    )
    display_name: BoundedName
    source_format: Literal["plain_text", "markdown", "html", "feishu_blocks_json"]
    text: Annotated[
        str, StringConstraints(min_length=1, max_length=MAX_PROJECT_MATERIAL_TEXT_CHARS)
    ]

    @field_validator("text")
    @classmethod
    def _normalize_newlines(cls, value: str) -> str:
        return _canonical_text(value)


class ProjectMaterialV1(_FrozenModel):
    material_schema_version: Literal["project-material@1"] = "project-material@1"
    material_id: BoundedId
    project_id: BoundedId
    display_name: BoundedName
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    source_format: MaterialSourceFormat
    original_source_artifact_id: BoundedId
    rendered_source_artifact_id: BoundedId
    parser_id: BoundedId
    parser_version: BoundedId
    parse_status: Literal["ready", "rejected"]
    parse_warnings: tuple[BoundedId, ...] = Field(default=(), max_length=1024)
    byte_size: Annotated[int, Field(ge=1, le=MAX_PROJECT_MATERIAL_BYTES)]
    text_char_count: NonNegativeInt
    created_by: BoundedId
    created_at: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    status: Literal["active", "archived"]
    revision: PositiveInt

    @field_validator("parse_warnings")
    @classmethod
    def _canonical_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class ProjectExtractionCreateRequestV1(_FrozenModel):
    request_schema_version: Literal["project-extraction-create-request@1"] = (
        "project-extraction-create-request@1"
    )
    material_ids: tuple[BoundedId, ...] = Field(
        min_length=1,
        max_length=MAX_PROJECT_MATERIALS_PER_EXTRACTION,
    )
    planning_scope: PlanningContentScope = "auto"
    objective_goal_text: Annotated[str, StringConstraints(min_length=1, max_length=16384)]
    generation_policy: ProfileRefV1 | None = None
    candidate_export_profiles: tuple[ProfileRefV1, ...] = Field(default=(), max_length=16)
    llm_execution_mode: Literal["live", "record", "replay"] = "record"
    execution_version_plan: ExecutionVersionPlanV1 | None = None
    cassette_artifact_id: BoundedId | None = None

    @model_validator(mode="after")
    def _canonical(self) -> "ProjectExtractionCreateRequestV1":
        object.__setattr__(self, "material_ids", tuple(sorted(set(self.material_ids))))
        profiles = {
            (profile.profile_id, profile.version): profile
            for profile in self.candidate_export_profiles
        }
        object.__setattr__(
            self,
            "candidate_export_profiles",
            tuple(profiles[key] for key in sorted(profiles)),
        )
        if (self.llm_execution_mode == "replay") != (self.cassette_artifact_id is not None):
            raise ValueError("replay requires exactly one cassette artifact binding")
        return self


class ProjectExtractionDiscardRequestV1(_FrozenModel):
    request_schema_version: Literal["project-extraction-discard-request@1"] = (
        "project-extraction-discard-request@1"
    )
    expected_revision: PositiveInt
    reason: Annotated[str, StringConstraints(min_length=1, max_length=1024)]

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, value: str) -> str:
        rendered = value.strip()
        if not rendered:
            raise ValueError("discard reason cannot be blank")
        return rendered


class ProjectExtractionIssueV1(_FrozenModel):
    """One deterministic gate issue translated for a planning workflow."""

    issue_schema_version: Literal["project-extraction-issue@1"] = "project-extraction-issue@1"
    issue_id: BoundedId
    source: Literal["structure", "economy"]
    severity: Literal["critical", "major", "minor", "info"]
    code: BoundedId
    title: BoundedName
    description: BoundedDescription
    resolution_hint: BoundedDescription
    affected_content: tuple[BoundedName, ...] = Field(default=(), max_length=64)

    @field_validator("affected_content")
    @classmethod
    def _canonical_affected_content(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class ProjectExtractionV1(_FrozenModel):
    extraction_schema_version: Literal["project-extraction@1"] = "project-extraction@1"
    extraction_id: BoundedId
    project_id: BoundedId
    planning_scope: PlanningContentScope = "auto"
    material_ids: tuple[BoundedId, ...]
    source_artifact_ids: tuple[BoundedId, ...]
    base_snapshot_artifact_id: BoundedId
    run_id: BoundedId
    status: ProjectExtractionStatus
    patch_artifact_id: BoundedId | None = None
    preview_snapshot_artifact_id: BoundedId | None = None
    approval_id: BoundedId | None = None
    publication_patch_artifact_id: BoundedId | None = None
    publication_approval_id: BoundedId | None = None
    failure_cause_code: BoundedId | None = None
    failure_message: BoundedDescription | None = None
    failure_retryable: bool | None = None
    normalization_summary: "IdentityNormalizationSummaryV1 | None" = None
    alias_groups: tuple["IdentityAliasGroupV1", ...] = Field(default=(), max_length=4096)
    identity_conflicts: tuple["IdentityConflictV1", ...] = Field(default=(), max_length=4096)
    validation_issues: tuple[ProjectExtractionIssueV1, ...] = Field(
        default=(),
        max_length=4096,
    )
    disposition: Literal["discarded"] | None = None
    discarded_by: BoundedId | None = None
    discarded_at: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    discard_reason: Annotated[str, StringConstraints(min_length=1, max_length=1024)] | None = None
    created_by: BoundedId
    created_at: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    updated_at: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    revision: PositiveInt

    @model_validator(mode="after")
    def _canonical_bindings(self) -> "ProjectExtractionV1":
        for field in ("material_ids", "source_artifact_ids"):
            values = getattr(self, field)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field} must be stable-unique and sorted")
        discard_metadata = (
            self.disposition,
            self.discarded_by,
            self.discarded_at,
            self.discard_reason,
        )
        if self.disposition == "discarded":
            if any(value is None for value in discard_metadata):
                raise ValueError("discarded extraction requires complete discard metadata")
            if self.discarded_at is not None and self.discarded_at < self.created_at:
                raise ValueError("discard metadata cannot precede extraction creation")
            if self.discarded_at is not None and self.updated_at < self.discarded_at:
                raise ValueError("discard metadata cannot follow extraction updated_at")
        elif any(value is not None for value in discard_metadata):
            raise ValueError("discard metadata requires discarded extraction disposition")
        if self.status == "ready" and (
            self.patch_artifact_id is None or self.preview_snapshot_artifact_id is None
        ):
            raise ValueError("ready extraction requires patch and preview artifacts")
        publication_binding = (
            self.publication_patch_artifact_id,
            self.publication_approval_id,
        )
        if any(value is None for value in publication_binding) and any(
            value is not None for value in publication_binding
        ):
            raise ValueError("publication draft binding requires both Patch and ApprovalItem")
        failure_details = (
            self.failure_cause_code,
            self.failure_message,
            self.failure_retryable,
        )
        if self.status not in {"failed", "needs_resolution"} and any(
            value is not None for value in failure_details
        ):
            raise ValueError(
                "terminal failure details require a failed or needs-resolution extraction"
            )
        if (
            self.status in {"failed", "needs_resolution"}
            and any(value is not None for value in failure_details)
            and any(value is None for value in failure_details)
        ):
            raise ValueError("terminal failure details must be complete when present")
        object.__setattr__(
            self,
            "alias_groups",
            tuple(sorted(self.alias_groups, key=lambda item: item.canonical_identity)),
        )
        object.__setattr__(
            self,
            "identity_conflicts",
            tuple(sorted(self.identity_conflicts, key=lambda item: item.conflict_id)),
        )
        object.__setattr__(
            self,
            "validation_issues",
            tuple(sorted(self.validation_issues, key=lambda item: item.issue_id)),
        )
        if self.validation_issues and self.status != "needs_resolution":
            raise ValueError("validation issues require needs_resolution status")
        if self.validation_issues and (
            self.patch_artifact_id is None or self.preview_snapshot_artifact_id is None
        ):
            raise ValueError("validation issues require an editable patch and preview")
        if self.normalization_summary is None:
            if self.alias_groups or self.identity_conflicts:
                raise ValueError("identity evidence requires a normalization summary")
        elif (
            len(self.alias_groups) != self.normalization_summary.alias_group_count
            or len(self.identity_conflicts) != self.normalization_summary.blocking_conflict_count
        ):
            raise ValueError("identity evidence counts differ from its summary")
        return self


class IdentityAliasGroupV1(_FrozenModel):
    alias_schema_version: Literal["identity-alias-group@1"] = "identity-alias-group@1"
    canonical_identity: BoundedId
    aliases: tuple[BoundedId, ...] = Field(min_length=1, max_length=4096)

    @field_validator("aliases")
    @classmethod
    def _canonical_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class IdentityConflictCandidateV1(_FrozenModel):
    op_id: BoundedId
    source_identity: BoundedId
    value: JsonValue


class IdentityConflictV1(_FrozenModel):
    conflict_schema_version: Literal["identity-conflict@1"] = "identity-conflict@1"
    conflict_id: BoundedId
    code: Literal[
        "attribute_value_conflict",
        "entity_type_conflict",
        "ambiguous_unqualified_alias",
        "relation_shape_conflict",
        "dangling_relation_endpoint",
        "malformed_operation",
    ]
    canonical_identity: BoundedId
    candidates: tuple[IdentityConflictCandidateV1, ...] = Field(min_length=1, max_length=4096)


class IdentityNormalizationSummaryV1(_FrozenModel):
    summary_schema_version: Literal["identity-normalization-summary@1"] = (
        "identity-normalization-summary@1"
    )
    policy_version: Literal["identity-normalization@1"] = "identity-normalization@1"
    input_operation_count: NonNegativeInt
    output_operation_count: NonNegativeInt
    alias_group_count: NonNegativeInt
    auto_merge_count: NonNegativeInt
    blocking_conflict_count: NonNegativeInt


class ProjectGraphDraftRequestV1(_FrozenModel):
    request_schema_version: Literal["project-graph-draft-request@1"] = (
        "project-graph-draft-request@1"
    )
    source_extraction_id: BoundedId
    expected_source_extraction_revision: PositiveInt
    expected_project_revision: PositiveInt
    entities: tuple[Entity, ...] = Field(max_length=MAX_PROJECT_GRAPH_ITEMS)
    relations: tuple[Relation, ...] = Field(max_length=MAX_PROJECT_GRAPH_ITEMS)
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=16384)]
    side_effect_risk: Annotated[str, StringConstraints(min_length=1, max_length=4096)] = "low"
    candidate_export_profiles: tuple[ProfileRefV1, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _canonical_graph(self) -> "ProjectGraphDraftRequestV1":
        entity_ids = [entity.id for entity in self.entities]
        relation_ids = [relation.id for relation in self.relations]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("draft entity ids must be unique")
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("draft relation ids must be unique")
        object.__setattr__(self, "entities", tuple(sorted(self.entities, key=lambda item: item.id)))
        object.__setattr__(
            self,
            "relations",
            tuple(sorted(self.relations, key=lambda item: item.id)),
        )
        return self


class ProjectPageV1(_FrozenModel):
    page_schema_version: Literal["project-page@1"] = "project-page@1"
    items: tuple[GameProjectV1, ...]
    next_cursor: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None


class ProjectMaterialPageV1(_FrozenModel):
    page_schema_version: Literal["project-material-page@1"] = "project-material-page@1"
    items: tuple[ProjectMaterialV1, ...]
    next_cursor: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None


class ProjectExtractionPageV1(_FrozenModel):
    page_schema_version: Literal["project-extraction-page@1"] = "project-extraction-page@1"
    items: tuple[ProjectExtractionV1, ...]
    next_cursor: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None


ProjectExtractionV1.model_rebuild()


__all__ = [
    "GameProjectV1",
    "IdentityAliasGroupV1",
    "IdentityConflictCandidateV1",
    "IdentityConflictV1",
    "IdentityNormalizationSummaryV1",
    "MAX_PROJECT_MATERIAL_BYTES",
    "MaterialSourceFormat",
    "PlanningContentScope",
    "ProjectArchiveRequestV1",
    "ProjectCreateRequestV1",
    "ProjectExtractionCreateRequestV1",
    "ProjectExtractionDiscardRequestV1",
    "ProjectExtractionIssueV1",
    "ProjectExtractionPageV1",
    "ProjectExtractionStatus",
    "ProjectExtractionV1",
    "ProjectGraphDraftRequestV1",
    "ProjectMaterialPageV1",
    "ProjectMaterialTextRequestV1",
    "ProjectMaterialV1",
    "ProjectPageV1",
    "ProjectUpdateRequestV1",
]
