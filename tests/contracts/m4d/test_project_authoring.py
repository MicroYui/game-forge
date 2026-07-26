from __future__ import annotations

from pydantic import ValidationError
import pytest

from gameforge.contracts.api import GenerationProposeRequestV1
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    GenerationProposePayloadV1,
    PromptGoalBindingV1,
    RefReadBindingV1,
)
from gameforge.contracts.projects import (
    GameProjectV1,
    IdentityAliasGroupV1,
    IdentityConflictCandidateV1,
    IdentityConflictV1,
    IdentityNormalizationSummaryV1,
    ProjectCreateRequestV1,
    ProjectExtractionDiscardRequestV1,
    ProjectExtractionIssueV1,
    ProjectExtractionPageV1,
    ProjectExtractionV1,
    ProjectGraphDraftRequestV1,
    ProjectMaterialTextRequestV1,
)
from gameforge.contracts.storage import RefValue


DOMAIN = DomainScope(domain_ids=("domain:narrative",))


def _profile() -> ProfileRefV1:
    return ProfileRefV1(profile_id="generation-default", version=1)


def _target() -> RefReadBindingV1:
    return RefReadBindingV1(ref_name="projects/project:p/content/head", expected_ref=None)


def test_project_contract_derives_stable_project_refs_and_keeps_first_content_unpublished() -> None:
    project = GameProjectV1(
        project_id="project:01J00000000000000000000000",
        project_key="sky-harbor",
        display_name="天空港",
        description="一款浮空城经营 RPG",
        genre="RPG",
        status="draft",
        domain_scope=DOMAIN,
        bootstrap_snapshot_artifact_id="artifact:bootstrap",
        content_ref_name="projects/project:01J00000000000000000000000/content/head",
        constraint_ref_name="projects/project:01J00000000000000000000000/constraints/head",
        current_content_ref=None,
        current_constraint_ref=None,
        created_by="human:admin",
        created_at="2026-07-24T00:00:00Z",
        updated_at="2026-07-24T00:00:00Z",
        revision=1,
    )

    assert project.status == "draft"
    assert project.current_content_ref is None
    assert project.content_ref_name.endswith("/content/head")


def test_project_contract_rejects_noncanonical_key_and_ref_namespace() -> None:
    with pytest.raises(ValidationError):
        ProjectCreateRequestV1(
            project_key="Sky Harbor",
            display_name="天空港",
            description="",
            genre="RPG",
            domain_scope=DOMAIN,
        )

    with pytest.raises(ValidationError):
        GameProjectV1(
            project_id="project:p",
            project_key="sky-harbor",
            display_name="天空港",
            description="",
            genre="RPG",
            status="active",
            domain_scope=DOMAIN,
            bootstrap_snapshot_artifact_id="artifact:bootstrap",
            content_ref_name="content-head",
            constraint_ref_name="constraints-head",
            current_content_ref=RefValue(artifact_id="artifact:content", revision=1),
            current_constraint_ref=None,
            created_by="human:admin",
            created_at="2026-07-24T00:00:00Z",
            updated_at="2026-07-24T00:00:00Z",
            revision=2,
        )


def test_text_material_contract_is_bounded_and_canonical() -> None:
    request = ProjectMaterialTextRequestV1(
        display_name="世界观草案",
        source_format="markdown",
        text="# 天空港\r\n\r\n空气质量字段为 air.quality。\r\n",
    )
    assert request.text == "# 天空港\n\n空气质量字段为 air.quality。\n"

    with pytest.raises(ValidationError):
        ProjectMaterialTextRequestV1(
            display_name="空材料",
            source_format="plain_text",
            text="   \r\n",
        )


def test_generation_request_and_payload_bind_sorted_unique_material_sources() -> None:
    request = GenerationProposeRequestV1(
        base_snapshot_artifact_id="artifact:bootstrap",
        source_artifact_ids=("artifact:rendered-b", "artifact:rendered-a", "artifact:rendered-a"),
        findings=(),
        objective_goal_text="从材料提取完整实体和关系。",
        domain_scope=DOMAIN,
        target=_target(),
        generation_policy=_profile(),
        candidate_export_profiles=(),
    )
    assert request.source_artifact_ids == ("artifact:rendered-a", "artifact:rendered-b")

    payload = GenerationProposePayloadV1(
        base_snapshot_artifact_id="artifact:bootstrap",
        source_artifact_ids=("artifact:rendered-b", "artifact:rendered-a"),
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal",
            expected_payload_hash="0" * 64,
        ),
        domain_scope=DOMAIN,
        target=_target(),
        generation_policy=_profile(),
        candidate_export_profiles=(),
    )
    assert payload.source_artifact_ids == ("artifact:rendered-a", "artifact:rendered-b")


def test_platform_admin_is_a_first_class_role() -> None:
    from gameforge.contracts.identity import RoleAssignmentV1
    from gameforge.contracts.lineage import AuditActor

    assignment = RoleAssignmentV1(
        assignment_id="assignment:admin",
        principal_id="human:admin",
        role="platform_admin",
        scope="all",
        status="active",
        revision=1,
        granted_at="2026-07-24T00:00:00Z",
        granted_by=AuditActor(principal_id="system:bootstrap", principal_kind="system"),
    )
    assert assignment.role == "platform_admin"


def test_project_extraction_exposes_complete_identity_evidence_for_visual_resolution() -> None:
    summary = IdentityNormalizationSummaryV1(
        input_operation_count=3,
        output_operation_count=1,
        alias_group_count=1,
        auto_merge_count=1,
        blocking_conflict_count=1,
    )
    alias = IdentityAliasGroupV1(
        canonical_identity="item:air_quality",
        aliases=("AIR_QUALITY", "air.quality"),
    )
    conflict = IdentityConflictV1(
        conflict_id="identity-conflict:air-quality-state",
        code="attribute_value_conflict",
        canonical_identity="item:air_quality",
        candidates=(
            IdentityConflictCandidateV1(
                op_id="op:1",
                source_identity="air.quality",
                value="Clean",
            ),
            IdentityConflictCandidateV1(
                op_id="op:2",
                source_identity="AIR_QUALITY",
                value="Polluted",
            ),
        ),
    )
    extraction = ProjectExtractionV1(
        extraction_id="extraction:1",
        project_id="project:p",
        planning_scope="auto",
        material_ids=("material:1",),
        source_artifact_ids=("artifact:source",),
        base_snapshot_artifact_id="artifact:base",
        run_id="run:1",
        status="needs_resolution",
        normalization_summary=summary,
        alias_groups=(alias,),
        identity_conflicts=(conflict,),
        created_by="human:admin",
        created_at="2026-07-24T00:00:00Z",
        updated_at="2026-07-24T00:00:00Z",
        revision=1,
    )

    assert extraction.alias_groups == (alias,)
    assert extraction.identity_conflicts == (conflict,)
    with pytest.raises(ValidationError, match="counts"):
        ProjectExtractionV1.model_validate(
            {
                **extraction.model_dump(mode="json"),
                "identity_conflicts": [],
            }
        )


def test_failed_project_extraction_exposes_safe_terminal_status_details() -> None:
    extraction = ProjectExtractionV1(
        extraction_id="extraction:failed",
        project_id="project:p",
        planning_scope="auto",
        material_ids=("material:1",),
        source_artifact_ids=("artifact:source",),
        base_snapshot_artifact_id="artifact:base",
        run_id="run:failed",
        status="failed",
        failure_cause_code="generation_output_truncated",
        failure_message="AI 输出达到长度上限，系统已安全停止。",
        failure_retryable=True,
        created_by="human:admin",
        created_at="2026-07-24T00:00:00Z",
        updated_at="2026-07-24T00:01:00Z",
        revision=1,
    )

    assert extraction.failure_cause_code == "generation_output_truncated"
    assert extraction.failure_retryable is True

    with pytest.raises(ValidationError, match="terminal failure details"):
        ProjectExtractionV1.model_validate(
            {**extraction.model_dump(mode="json"), "status": "queued"}
        )


def test_project_extraction_exposes_human_readable_validation_issues_only_for_resolution() -> None:
    issue = ProjectExtractionIssueV1(
        issue_id="finding:dead-quest:1",
        source="structure",
        severity="critical",
        code="dead_quest",
        title="任务缺少起点或步骤",
        description="“未寄之梦”缺少明确的任务发起方。",
        resolution_hint="补充发起角色，或把它改成非任务玩法。",
        affected_content=("未寄之梦",),
    )
    extraction = ProjectExtractionV1(
        extraction_id="extraction:review",
        project_id="project:p",
        planning_scope="limited_event",
        material_ids=("material:1",),
        source_artifact_ids=("artifact:source",),
        base_snapshot_artifact_id="artifact:base",
        run_id="run:review",
        status="needs_resolution",
        patch_artifact_id="artifact:patch",
        preview_snapshot_artifact_id="artifact:preview",
        failure_cause_code="generation_validation_needs_resolution",
        failure_message="已生成可编辑草案，但确定性检查发现需要确认的问题。",
        failure_retryable=False,
        validation_issues=(issue,),
        created_by="human:admin",
        created_at="2026-07-24T00:00:00Z",
        updated_at="2026-07-24T00:01:00Z",
        revision=1,
    )

    assert extraction.validation_issues == (issue,)
    assert "sha256:" not in extraction.validation_issues[0].description
    with pytest.raises(ValidationError, match="validation issues"):
        ProjectExtractionV1.model_validate(
            {
                **extraction.model_dump(mode="json"),
                "status": "ready",
                "failure_cause_code": None,
                "failure_message": None,
                "failure_retryable": None,
            }
        )


def test_project_extraction_discard_is_a_retained_auditable_planner_decision() -> None:
    request = ProjectExtractionDiscardRequestV1(
        expected_revision=2,
        reason="这次方向不符合活动主题，保留材料后重新提取。",
    )
    discarded = ProjectExtractionV1(
        extraction_id="extraction:discarded",
        project_id="project:p",
        planning_scope="limited_event",
        material_ids=("material:1", "material:2"),
        source_artifact_ids=("artifact:source:1", "artifact:source:2"),
        base_snapshot_artifact_id="artifact:base",
        run_id="run:discarded",
        status="ready",
        disposition="discarded",
        discarded_by="human:admin",
        discarded_at="2026-07-24T00:02:00Z",
        discard_reason=request.reason,
        patch_artifact_id="artifact:patch",
        preview_snapshot_artifact_id="artifact:preview",
        created_by="human:admin",
        created_at="2026-07-24T00:00:00Z",
        updated_at="2026-07-24T00:02:00Z",
        revision=3,
    )
    page = ProjectExtractionPageV1(items=(discarded,))

    assert page.items[0].material_ids == ("material:1", "material:2")
    assert page.items[0].discard_reason == request.reason
    assert page.items[0].patch_artifact_id == "artifact:patch"

    with pytest.raises(ValidationError, match="discard metadata"):
        ProjectExtractionV1.model_validate(
            {
                **discarded.model_dump(mode="json"),
                "discarded_by": None,
            }
        )

    with pytest.raises(ValidationError, match="discard metadata"):
        ProjectExtractionV1.model_validate(
            {
                **discarded.model_dump(mode="json"),
                "disposition": None,
            }
        )


def test_project_publication_draft_is_bound_to_one_exact_source_extraction() -> None:
    extraction = ProjectExtractionV1(
        extraction_id="extraction:liyue-story",
        project_id="project:p",
        material_ids=("material:liyue-story",),
        source_artifact_ids=("artifact:source:liyue-story",),
        base_snapshot_artifact_id="artifact:base",
        run_id="run:liyue-story",
        status="ready",
        patch_artifact_id="artifact:agent-patch:liyue-story",
        preview_snapshot_artifact_id="artifact:preview:liyue-story",
        publication_patch_artifact_id="artifact:human-patch:liyue-story",
        publication_approval_id="approval:patch:artifact:human-patch:liyue-story",
        created_by="human:admin",
        created_at="2026-07-24T00:00:00Z",
        updated_at="2026-07-24T00:01:00Z",
        revision=2,
    )

    assert extraction.publication_patch_artifact_id == "artifact:human-patch:liyue-story"
    with pytest.raises(ValidationError, match="publication draft binding"):
        ProjectExtractionV1.model_validate(
            {
                **extraction.model_dump(mode="json"),
                "publication_approval_id": None,
            }
        )

    request = ProjectGraphDraftRequestV1(
        source_extraction_id=extraction.extraction_id,
        expected_source_extraction_revision=extraction.revision,
        expected_project_revision=3,
        entities=(),
        relations=(),
        rationale="发布璃月剧情提案",
    )
    assert request.source_extraction_id == extraction.extraction_id
    with pytest.raises(ValidationError):
        ProjectGraphDraftRequestV1.model_validate(
            {
                key: value
                for key, value in request.model_dump(mode="json").items()
                if key != "source_extraction_id"
            }
        )
