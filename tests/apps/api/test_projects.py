from __future__ import annotations

from fastapi.testclient import TestClient

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import (
    ApiDependencies,
    WorkflowCommandResult,
    require_actor,
)
from gameforge.contracts.api import (
    ArtifactSummaryV1,
    HumanPatchDraftRequestV1,
    PatchArtifactReadViewV1,
)
from gameforge.contracts.findings import PatchV2, TypedOp
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainScope,
    Principal,
)
from gameforge.contracts.projects import (
    GameProjectV1,
    ProjectExtractionPageV1,
    ProjectExtractionV1,
    IdentityNormalizationSummaryV1,
    ProjectMaterialPageV1,
    ProjectMaterialV1,
    ProjectPageV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.platform.projects import ProjectContentDraftPreparation


NOW = "2026-07-24T00:00:00Z"
SCOPE = DomainScope(domain_ids=("game-content",))


def _actor() -> ActorContext:
    return ActorContext(
        principal=Principal(
            id="human:maker",
            kind="human",
            display_name="Maker",
            status="active",
            revision=1,
            credential_epoch=1,
            authz_revision=1,
            roles=(),
        ),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="credential:maker",
        ),
        session_id="session:maker",
        request_id="request:maker",
    )


def _project(revision: int = 1) -> GameProjectV1:
    return GameProjectV1(
        project_id="project:0123456789abcdef",
        project_key="sky-harbor",
        display_name="天空港",
        status="draft",
        domain_scope=SCOPE,
        bootstrap_snapshot_artifact_id="artifact:bootstrap",
        content_ref_name="projects/project:0123456789abcdef/content/head",
        constraint_ref_name="projects/project:0123456789abcdef/constraints/head",
        created_by="human:maker",
        created_at=NOW,
        updated_at=NOW,
        revision=revision,
    )


def _material(source_format: str = "markdown") -> ProjectMaterialV1:
    return ProjectMaterialV1(
        material_id="material:0123456789abcdef",
        project_id=_project().project_id,
        display_name="世界观.md",
        media_type="text/markdown",
        source_format=source_format,  # type: ignore[arg-type]
        original_source_artifact_id="artifact:raw",
        rendered_source_artifact_id="artifact:rendered",
        parser_id=f"planning-material-{source_format}",
        parser_version="1",
        parse_status="ready",
        byte_size=12,
        text_char_count=6,
        created_by="human:maker",
        created_at=NOW,
        status="active",
        revision=1,
    )


def _extraction() -> ProjectExtractionV1:
    return ProjectExtractionV1(
        extraction_id="extraction:0123456789abcdef",
        project_id=_project().project_id,
        planning_scope="auto",
        material_ids=(_material().material_id,),
        source_artifact_ids=(_material().rendered_source_artifact_id,),
        base_snapshot_artifact_id=_project().bootstrap_snapshot_artifact_id,
        run_id="run:project-extraction",
        status="queued",
        created_by="human:maker",
        created_at=NOW,
        updated_at=NOW,
        revision=1,
    )


def _content_preparation() -> ProjectContentDraftPreparation:
    return ProjectContentDraftPreparation(
        workflow_request=HumanPatchDraftRequestV1(
            base_snapshot_artifact_id="artifact:bootstrap",
            ref_name=_project().content_ref_name,
            expected_ref=None,
            expected_to_fix=(),
            preconditions=(),
            side_effect_risk="low",
            ops=(
                TypedOp(
                    op_id="graph-op:1",
                    op="add_entity",
                    target="item:air_quality",
                    new_value={"type": "ITEM", "attrs": {"label": "空气质量"}},
                ),
            ),
            rationale="确认首版",
            candidate_export_profiles=(),
        ),
        alias_groups=(),
        normalization_summary=IdentityNormalizationSummaryV1(
            input_operation_count=1,
            output_operation_count=1,
            alias_group_count=0,
            auto_merge_count=0,
            blocking_conflict_count=0,
        ),
        expected_project_revision=1,
        source_extraction_id=_extraction().extraction_id,
        expected_source_extraction_revision=_extraction().revision,
    )


class _Projects:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def create_project(self, payload, *, context):
        self.calls.append(("create", (payload, context)))
        return _project()

    def list_projects(self, **kwargs):
        self.calls.append(("list", kwargs))
        return ProjectPageV1(items=(_project(),))

    def get_project(self, project_id, *, actor):
        self.calls.append(("get", (project_id, actor)))
        return _project()

    def update_project(self, project_id, payload, *, context):
        self.calls.append(("update", (project_id, payload, context)))
        return _project(revision=2)

    def archive_project(self, project_id, payload, *, context):
        self.calls.append(("archive", (project_id, payload, context)))
        return _project(revision=2).model_copy(update={"status": "archived"})

    def add_text_material(self, project_id, payload, *, context):
        self.calls.append(("material_text", (project_id, payload, context)))
        return _material(payload.source_format)

    def add_uploaded_material(self, project_id, **kwargs):
        self.calls.append(("material_upload", (project_id, kwargs)))
        return _material(kwargs["source_format"])

    def list_materials(self, project_id, **kwargs):
        self.calls.append(("material_list", (project_id, kwargs)))
        return ProjectMaterialPageV1(items=(_material(),))

    def get_material(self, project_id, material_id, *, actor):
        self.calls.append(("material_get", (project_id, material_id, actor)))
        return _material()

    def archive_material(self, project_id, material_id, payload, *, context):
        self.calls.append(("material_archive", (project_id, material_id, payload, context)))
        return _material().model_copy(update={"status": "archived", "revision": 2})

    def create_extraction(self, project_id, payload, *, context):
        self.calls.append(("extraction_create", (project_id, payload, context)))
        return _extraction()

    def get_extraction(self, project_id, extraction_id, *, actor):
        self.calls.append(("extraction_get", (project_id, extraction_id, actor)))
        return _extraction()

    def list_extractions(self, project_id, **kwargs):
        self.calls.append(("extraction_list", (project_id, kwargs)))
        return ProjectExtractionPageV1(items=(_extraction(),))

    def discard_extraction(self, project_id, extraction_id, payload, *, context):
        self.calls.append(("extraction_discard", (project_id, extraction_id, payload, context)))
        return _extraction().model_copy(
            update={
                "status": "failed",
                "disposition": "discarded",
                "discarded_by": "human:maker",
                "discarded_at": "2026-07-24T00:05:00Z",
                "discard_reason": payload.reason,
                "updated_at": "2026-07-24T00:05:00Z",
                "revision": 2,
            }
        )

    def prepare_content_draft(self, project_id, payload, *, context):
        self.calls.append(("content_prepare", (project_id, payload, context)))
        return _content_preparation()

    def record_content_draft(self, project_id, **kwargs):
        self.calls.append(("content_record", (project_id, kwargs)))
        return _project(revision=2).model_copy(
            update={
                "latest_patch_artifact_id": kwargs["patch_artifact_id"],
                "latest_approval_id": f"approval:patch:{kwargs['patch_artifact_id']}",
            }
        )


class _Workflow:
    def execute(self, command):
        request = command.payload
        patch = PatchV2(
            revision=1,
            base_snapshot_id="sha256:base",
            target_snapshot_id="sha256:preview",
            side_effect_risk=request.side_effect_risk,
            ops=list(request.ops),
            produced_by="human",
            rationale=request.rationale,
        )
        artifact_id = "artifact:project-human-patch"
        return WorkflowCommandResult(
            value=PatchArtifactReadViewV1(
                artifact=ArtifactSummaryV1(
                    artifact_id=artifact_id,
                    lineage_schema_version="lineage@2",
                    kind="patch",
                    version_tuple=VersionTuple(ir_snapshot_id="sha256:base"),
                    parent_artifact_ids=(request.base_snapshot_artifact_id,),
                    payload_hash="a" * 64,
                    payload_schema_id="patch@2",
                    domain_scope=SCOPE,
                    created_at=NOW,
                ),
                patch=patch,
                validation_status="not_started",
                regression_status="not_started",
                approval_status="draft",
                workflow_revision=1,
            ),
            resource_kind="patch",
            resource_id=artifact_id,
            revision=1,
        )


def _client(*, with_workflow: bool = False) -> tuple[TestClient, _Projects]:
    projects = _Projects()
    app = create_app(
        ApiDependencies(
            project_authoring=projects,
            workflow_commands=_Workflow() if with_workflow else None,
        )
    )
    app.dependency_overrides[require_actor] = _actor
    return TestClient(app), projects


def test_project_create_and_read_expose_human_resource_headers() -> None:
    client, projects = _client()
    response = client.post(
        "/api/v1/projects",
        headers={"Idempotency-Key": "project-create-1"},
        json={
            "project_key": "sky-harbor",
            "display_name": "天空港",
            "description": "浮空城经营 RPG",
            "genre": "RPG",
            "domain_scope": {"domain_ids": ["game-content"]},
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["display_name"] == "天空港"
    assert response.headers["location"].endswith("/project:0123456789abcdef")
    assert response.headers["etag"].startswith('"')
    assert response.headers["x-resource-revision"] == "1"
    assert projects.calls[0][0] == "create"

    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["project_key"] == "sky-harbor"


def test_raw_upload_forwards_exact_bytes_and_feishu_format_headers() -> None:
    client, projects = _client()
    raw = b'{"blocks":[]}'

    response = client.post(
        "/api/v1/projects/project:0123456789abcdef/materials:upload",
        headers={
            "Idempotency-Key": "material-upload-1",
            "Content-Type": "application/json",
            "X-GameForge-File-Name": "Feishu export.json",
            "X-GameForge-Source-Format": "feishu_blocks_json",
        },
        content=raw,
    )

    assert response.status_code == 201, response.text
    assert response.json()["source_format"] == "feishu_blocks_json"
    _, (_, kwargs) = projects.calls[-1]
    assert kwargs["payload"] == raw
    assert kwargs["display_name"] == "Feishu export.json"
    assert kwargs["media_type"] == "application/json"


def test_update_requires_one_strong_if_match_header() -> None:
    client, projects = _client()
    payload = {
        "display_name": "天空港 2",
        "description": "升级版",
        "genre": "RPG",
        "domain_scope": {"domain_ids": ["game-content"]},
        "expected_revision": 1,
    }

    missing = client.patch(
        "/api/v1/projects/project:0123456789abcdef",
        headers={"Idempotency-Key": "project-update-1"},
        json=payload,
    )
    assert missing.status_code == 422

    weak = client.patch(
        "/api/v1/projects/project:0123456789abcdef",
        headers={
            "Idempotency-Key": "project-update-1",
            "If-Match": 'W/"weak"',
        },
        json=payload,
    )
    assert weak.status_code == 422
    assert not any(call[0] == "update" for call in projects.calls)


def test_project_extraction_starts_a_run_without_exposing_run_internals() -> None:
    client, projects = _client()

    response = client.post(
        "/api/v1/projects/project:0123456789abcdef/extractions",
        headers={"Idempotency-Key": "project-extraction-1"},
        json={
            "material_ids": ["material:0123456789abcdef"],
            "planning_scope": "auto",
            "objective_goal_text": "从策划材料提取实体和关系草案。",
        },
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    assert response.headers["x-run-id"] == "run:project-extraction"
    assert response.headers["link"].endswith('>; rel="events"')
    assert response.headers["location"].endswith("/extractions/extraction:0123456789abcdef")
    assert projects.calls[-1][0] == "extraction_create"

    loaded = client.get(
        "/api/v1/projects/project:0123456789abcdef/extractions/extraction:0123456789abcdef"
    )
    assert loaded.status_code == 200
    assert loaded.json()["run_id"] == "run:project-extraction"

    listed = client.get("/api/v1/projects/project:0123456789abcdef/extractions?limit=100")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["extraction_id"] == ("extraction:0123456789abcdef")

    discarded = client.post(
        "/api/v1/projects/project:0123456789abcdef/extractions/extraction:0123456789abcdef:discard",
        headers={
            "Idempotency-Key": "project-extraction-discard-1",
            "If-Match": '"project-extraction-revision-1"',
        },
        json={
            "expected_revision": 1,
            "reason": "这版方向不合适，保留材料后重新提取。",
        },
    )
    assert discarded.status_code == 200, discarded.text
    assert discarded.json()["status"] == "failed"
    assert discarded.json()["disposition"] == "discarded"
    assert discarded.json()["discard_reason"] == "这版方向不合适，保留材料后重新提取。"
    assert discarded.headers["x-run-id"] == "run:project-extraction"
    assert discarded.headers["x-resource-revision"] == "2"


def test_project_graph_draft_bridges_to_existing_patch_workflow() -> None:
    client, projects = _client(with_workflow=True)

    response = client.post(
        "/api/v1/projects/project:0123456789abcdef/content-drafts",
        headers={
            "Idempotency-Key": "project-content-draft-1",
            "If-Match": '"project-revision-1"',
        },
        json={
            "source_extraction_id": _extraction().extraction_id,
            "expected_source_extraction_revision": _extraction().revision,
            "expected_project_revision": 1,
            "entities": [
                {
                    "id": "item:air_quality",
                    "type": "ITEM",
                    "attrs": {"label": "空气质量"},
                }
            ],
            "relations": [],
            "rationale": "确认首版",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["approval_status"] == "draft"
    assert response.headers["x-project-revision"] == "2"
    assert response.headers["x-identity-auto-merges"] == "0"
    assert [call[0] for call in projects.calls[-2:]] == [
        "content_prepare",
        "content_record",
    ]
