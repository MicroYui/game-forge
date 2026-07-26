"""HTTP transport for project-first authoring resources."""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from gameforge.apps.api.dependencies import (
    ApiDependencies,
    WorkflowCommand,
    WorkflowCommandMetadata,
    api_dependencies,
    require_actor,
)
from gameforge.contracts.api import PatchArtifactReadViewV1, compute_resource_etag
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import (
    DependencyUnavailable,
    InvalidStateTransition,
    RequestSchemaInvalid,
    WorkflowGuard,
)
from gameforge.contracts.identity import ActorContext
from gameforge.contracts.projects import (
    MaterialSourceFormat,
    ProjectArchiveRequestV1,
    ProjectCreateRequestV1,
    ProjectExtractionCreateRequestV1,
    ProjectExtractionDiscardRequestV1,
    ProjectExtractionPageV1,
    ProjectExtractionV1,
    ProjectGraphDraftRequestV1,
    ProjectMaterialPageV1,
    ProjectMaterialRenameRequestV1,
    ProjectMaterialTextRequestV1,
    ProjectMaterialV1,
    ProjectPageV1,
    ProjectUpdateRequestV1,
    GameProjectV1,
)
from gameforge.platform.projects import ProjectCommandContext
from gameforge.runtime.observability.context import TraceCarrier, current_trace_context


_IDEMPOTENCY_HEADER = "Idempotency-Key"
_IF_MATCH_HEADER = "If-Match"
_FILE_NAME_HEADER = "X-GameForge-File-Name"
_SOURCE_FORMAT_HEADER = "X-GameForge-Source-Format"
_UPLOAD_FORMATS = frozenset(
    {"plain_text", "markdown", "html", "feishu_blocks_json", "docx", "xlsx", "csv"}
)
ApiResourceId = Annotated[str, Path(min_length=1, max_length=512)]


def _single_header(
    request: Request,
    name: str,
    *,
    max_length: int,
    visible_text: bool = False,
) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise RequestSchemaInvalid(f"{name} must be supplied exactly once")
    value = values[0]
    minimum = 0x20 if visible_text else 0x21
    if (
        not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < minimum or ord(character) == 0x7F for character in value)
    ):
        raise RequestSchemaInvalid(f"{name} is invalid")
    return value


def _strong_etag(value: str) -> bool:
    return bool(
        len(value) >= 3
        and value.startswith('"')
        and value.endswith('"')
        and not value.startswith("W/")
        and "," not in value
    )


def _require_idempotency(
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias=_IDEMPOTENCY_HEADER, min_length=1, max_length=512),
    ],
) -> None:
    del idempotency_key
    request.state.project_idempotency_key = _single_header(
        request,
        _IDEMPOTENCY_HEADER,
        max_length=512,
    )


def _require_mutation_headers(
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias=_IDEMPOTENCY_HEADER, min_length=1, max_length=512),
    ],
    if_match: Annotated[
        str,
        Header(alias=_IF_MATCH_HEADER, min_length=3, max_length=512),
    ],
) -> None:
    del idempotency_key, if_match
    _require_idempotency(request, _single_header(request, _IDEMPOTENCY_HEADER, max_length=512))
    exact = _single_header(request, _IF_MATCH_HEADER, max_length=512)
    if not _strong_etag(exact):
        raise RequestSchemaInvalid("If-Match must contain one strong quoted entity tag")
    request.state.project_if_match = exact


def _port(dependencies: ApiDependencies) -> Any:
    port = dependencies.project_authoring
    if port is None:
        raise DependencyUnavailable(
            "project authoring authority is unavailable",
            component="project_authoring",
        )
    return port


def _context(
    request: Request,
    actor: ActorContext,
    *,
    operation: str,
    payload: object,
    include_if_match: bool = False,
) -> ProjectCommandContext:
    idempotency_key = getattr(request.state, "project_idempotency_key", None)
    if not isinstance(idempotency_key, str):
        raise RequestSchemaInvalid("project idempotency key is unavailable")
    if_match = getattr(request.state, "project_if_match", None) if include_if_match else None
    request_hash = canonical_sha256(
        {
            "request_hash_schema_version": "project-command-request-hash@1",
            "api_version": "v1",
            "operation": operation,
            "method": request.method,
            "path": request.url.path,
            "payload": payload,
            **({"if_match": if_match} if include_if_match else {}),
        }
    )
    return ProjectCommandContext(
        actor=actor,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        request_id=request.state.request_id,
        trace_id=getattr(request.state, "trace_id", None),
        if_match=if_match,
    )


def _resource_headers(
    response: Response,
    *,
    resource_kind: str,
    resource_id: str,
    revision: int,
) -> None:
    response.headers["ETag"] = compute_resource_etag(
        resource_kind=resource_kind,
        resource_id=resource_id,
        revision=revision,
    )
    response.headers["X-Resource-Revision"] = str(revision)
    response.headers["Cache-Control"] = "private, no-cache"


def _page_headers(response: Response, payload: object) -> None:
    digest = canonical_sha256(
        {
            "etag_schema_version": "project-page-etag@1",
            "payload": payload,
        }
    )
    response.headers["ETag"] = f'"{digest}"'
    response.headers["Cache-Control"] = "private, no-cache"


def project_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["projects"])

    @router.post(
        "/projects",
        response_model=GameProjectV1,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(_require_idempotency)],
    )
    def create_project(
        payload: ProjectCreateRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> GameProjectV1:
        result = cast(
            GameProjectV1,
            _port(dependencies).create_project(
                payload,
                context=_context(
                    request,
                    actor,
                    operation="project.create",
                    payload=payload.model_dump(mode="json"),
                ),
            ),
        )
        response.headers["Location"] = f"/api/v1/projects/{result.project_id}"
        _resource_headers(
            response,
            resource_kind="project",
            resource_id=result.project_id,
            revision=result.revision,
        )
        return result

    @router.get("/projects", response_model=ProjectPageV1)
    def list_projects(
        response: Response,
        project_status: Annotated[
            Literal["draft", "active", "archived"] | None,
            Query(alias="status"),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectPageV1:
        result = cast(
            ProjectPageV1,
            _port(dependencies).list_projects(
                actor=actor,
                limit=limit,
                status=project_status,
            ),
        )
        _page_headers(response, result.model_dump(mode="json"))
        return result

    @router.get("/projects/{project_id}", response_model=GameProjectV1)
    def get_project(
        project_id: ApiResourceId,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> GameProjectV1:
        result = cast(
            GameProjectV1,
            _port(dependencies).get_project(project_id, actor=actor),
        )
        _resource_headers(
            response,
            resource_kind="project",
            resource_id=result.project_id,
            revision=result.revision,
        )
        return result

    @router.patch(
        "/projects/{project_id}",
        response_model=GameProjectV1,
        dependencies=[Depends(_require_mutation_headers)],
    )
    def update_project(
        project_id: ApiResourceId,
        payload: ProjectUpdateRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> GameProjectV1:
        result = cast(
            GameProjectV1,
            _port(dependencies).update_project(
                project_id,
                payload,
                context=_context(
                    request,
                    actor,
                    operation="project.update",
                    payload=payload.model_dump(mode="json"),
                    include_if_match=True,
                ),
            ),
        )
        _resource_headers(
            response,
            resource_kind="project",
            resource_id=result.project_id,
            revision=result.revision,
        )
        return result

    @router.post(
        "/projects/{project_id}:archive",
        response_model=GameProjectV1,
        dependencies=[Depends(_require_mutation_headers)],
    )
    def archive_project(
        project_id: ApiResourceId,
        payload: ProjectArchiveRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> GameProjectV1:
        result = cast(
            GameProjectV1,
            _port(dependencies).archive_project(
                project_id,
                payload,
                context=_context(
                    request,
                    actor,
                    operation="project.archive",
                    payload=payload.model_dump(mode="json"),
                    include_if_match=True,
                ),
            ),
        )
        _resource_headers(
            response,
            resource_kind="project",
            resource_id=result.project_id,
            revision=result.revision,
        )
        return result

    @router.post(
        "/projects/{project_id}/materials:text",
        response_model=ProjectMaterialV1,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(_require_idempotency)],
    )
    def add_text_material(
        project_id: ApiResourceId,
        payload: ProjectMaterialTextRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectMaterialV1:
        result = cast(
            ProjectMaterialV1,
            _port(dependencies).add_text_material(
                project_id,
                payload,
                context=_context(
                    request,
                    actor,
                    operation="project.material.create_text",
                    payload=payload.model_dump(mode="json"),
                ),
            ),
        )
        response.headers["Location"] = (
            f"/api/v1/projects/{project_id}/materials/{result.material_id}"
        )
        _resource_headers(
            response,
            resource_kind="project_material",
            resource_id=result.material_id,
            revision=result.revision,
        )
        return result

    @router.post(
        "/projects/{project_id}/materials:upload",
        response_model=ProjectMaterialV1,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(_require_idempotency)],
    )
    async def upload_material(
        project_id: ApiResourceId,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectMaterialV1:
        payload = await request.body()
        display_name = _single_header(
            request,
            _FILE_NAME_HEADER,
            max_length=256,
            visible_text=True,
        )
        source_format_value = _single_header(
            request,
            _SOURCE_FORMAT_HEADER,
            max_length=64,
        )
        if source_format_value not in _UPLOAD_FORMATS:
            raise RequestSchemaInvalid("X-GameForge-Source-Format is unsupported")
        media_type = _single_header(
            request,
            "Content-Type",
            max_length=256,
            visible_text=True,
        )
        source_format = cast(MaterialSourceFormat, source_format_value)
        hash_payload = {
            "display_name": display_name,
            "media_type": media_type,
            "source_format": source_format,
            "payload_sha256": sha256(payload).hexdigest(),
            "payload_size": len(payload),
        }
        context = _context(
            request,
            actor,
            operation="project.material.upload",
            payload=hash_payload,
        )
        result = cast(
            ProjectMaterialV1,
            await run_in_threadpool(
                lambda: _port(dependencies).add_uploaded_material(
                    project_id,
                    payload=payload,
                    display_name=display_name,
                    media_type=media_type,
                    source_format=source_format,
                    context=context,
                )
            ),
        )
        response.headers["Location"] = (
            f"/api/v1/projects/{project_id}/materials/{result.material_id}"
        )
        _resource_headers(
            response,
            resource_kind="project_material",
            resource_id=result.material_id,
            revision=result.revision,
        )
        return result

    @router.get(
        "/projects/{project_id}/materials",
        response_model=ProjectMaterialPageV1,
    )
    def list_materials(
        project_id: ApiResourceId,
        response: Response,
        material_status: Annotated[
            Literal["active", "archived"] | None,
            Query(alias="status"),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectMaterialPageV1:
        result = cast(
            ProjectMaterialPageV1,
            _port(dependencies).list_materials(
                project_id,
                actor=actor,
                limit=limit,
                status=material_status,
            ),
        )
        _page_headers(response, result.model_dump(mode="json"))
        return result

    @router.get(
        "/projects/{project_id}/materials/{material_id}",
        response_model=ProjectMaterialV1,
    )
    def get_material(
        project_id: ApiResourceId,
        material_id: ApiResourceId,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectMaterialV1:
        result = cast(
            ProjectMaterialV1,
            _port(dependencies).get_material(project_id, material_id, actor=actor),
        )
        _resource_headers(
            response,
            resource_kind="project_material",
            resource_id=result.material_id,
            revision=result.revision,
        )
        return result

    @router.post(
        "/projects/{project_id}/materials/{material_id}:rename",
        response_model=ProjectMaterialV1,
        dependencies=[Depends(_require_mutation_headers)],
    )
    def rename_material(
        project_id: ApiResourceId,
        material_id: ApiResourceId,
        payload: ProjectMaterialRenameRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectMaterialV1:
        result = cast(
            ProjectMaterialV1,
            _port(dependencies).rename_material(
                project_id,
                material_id,
                payload,
                context=_context(
                    request,
                    actor,
                    operation="project.material.rename",
                    payload=payload.model_dump(mode="json"),
                    include_if_match=True,
                ),
            ),
        )
        _resource_headers(
            response,
            resource_kind="project_material",
            resource_id=result.material_id,
            revision=result.revision,
        )
        return result

    @router.post(
        "/projects/{project_id}/materials/{material_id}:archive",
        response_model=ProjectMaterialV1,
        dependencies=[Depends(_require_mutation_headers)],
    )
    def archive_material(
        project_id: ApiResourceId,
        material_id: ApiResourceId,
        payload: ProjectArchiveRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectMaterialV1:
        result = cast(
            ProjectMaterialV1,
            _port(dependencies).archive_material(
                project_id,
                material_id,
                payload,
                context=_context(
                    request,
                    actor,
                    operation="project.material.archive",
                    payload=payload.model_dump(mode="json"),
                    include_if_match=True,
                ),
            ),
        )
        _resource_headers(
            response,
            resource_kind="project_material",
            resource_id=result.material_id,
            revision=result.revision,
        )
        return result

    @router.post(
        "/projects/{project_id}/extractions",
        response_model=ProjectExtractionV1,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(_require_idempotency)],
    )
    def create_extraction(
        project_id: ApiResourceId,
        payload: ProjectExtractionCreateRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectExtractionV1:
        result = cast(
            ProjectExtractionV1,
            _port(dependencies).create_extraction(
                project_id,
                payload,
                context=_context(
                    request,
                    actor,
                    operation="project.extraction.create",
                    payload=payload.model_dump(mode="json"),
                ),
            ),
        )
        response.headers["Location"] = (
            f"/api/v1/projects/{project_id}/extractions/{result.extraction_id}"
        )
        response.headers["X-Run-Id"] = result.run_id
        response.headers["Link"] = f'</api/v1/runs/{result.run_id}/events>; rel="events"'
        _resource_headers(
            response,
            resource_kind="project_extraction",
            resource_id=result.extraction_id,
            revision=result.revision,
        )
        return result

    @router.get(
        "/projects/{project_id}/extractions",
        response_model=ProjectExtractionPageV1,
    )
    def list_extractions(
        project_id: ApiResourceId,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectExtractionPageV1:
        result = cast(
            ProjectExtractionPageV1,
            _port(dependencies).list_extractions(
                project_id,
                actor=actor,
                limit=limit,
            ),
        )
        _page_headers(response, result.model_dump(mode="json"))
        return result

    @router.get(
        "/projects/{project_id}/extractions/{extraction_id}",
        response_model=ProjectExtractionV1,
    )
    def get_extraction(
        project_id: ApiResourceId,
        extraction_id: ApiResourceId,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectExtractionV1:
        result = cast(
            ProjectExtractionV1,
            _port(dependencies).get_extraction(
                project_id,
                extraction_id,
                actor=actor,
            ),
        )
        response.headers["X-Run-Id"] = result.run_id
        _resource_headers(
            response,
            resource_kind="project_extraction",
            resource_id=result.extraction_id,
            revision=result.revision,
        )
        return result

    @router.post(
        "/projects/{project_id}/extractions/{extraction_id}:discard",
        response_model=ProjectExtractionV1,
        dependencies=[Depends(_require_mutation_headers)],
    )
    def discard_extraction(
        project_id: ApiResourceId,
        extraction_id: ApiResourceId,
        payload: ProjectExtractionDiscardRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ProjectExtractionV1:
        result = cast(
            ProjectExtractionV1,
            _port(dependencies).discard_extraction(
                project_id,
                extraction_id,
                payload,
                context=_context(
                    request,
                    actor,
                    operation="project.extraction.discard",
                    payload=payload.model_dump(mode="json"),
                    include_if_match=True,
                ),
            ),
        )
        response.headers["X-Run-Id"] = result.run_id
        _resource_headers(
            response,
            resource_kind="project_extraction",
            resource_id=result.extraction_id,
            revision=result.revision,
        )
        return result

    @router.post(
        "/projects/{project_id}/content-drafts",
        response_model=PatchArtifactReadViewV1,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(_require_mutation_headers)],
    )
    def create_content_draft(
        project_id: ApiResourceId,
        payload: ProjectGraphDraftRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> PatchArtifactReadViewV1:
        context = _context(
            request,
            actor,
            operation="project.content_draft.create",
            payload=payload.model_dump(mode="json"),
            include_if_match=True,
        )
        preparation = _port(dependencies).prepare_content_draft(
            project_id,
            payload,
            context=context,
        )
        workflow = dependencies.workflow_commands
        if workflow is None:
            raise DependencyUnavailable(
                "workflow command authority is unavailable",
                component="workflow_command_authority",
            )
        trace_context = current_trace_context()
        command = WorkflowCommand(
            operation="patch.draft",
            resource_kind="patch_series",
            resource_id=preparation.workflow_request.base_snapshot_artifact_id,
            payload=preparation.workflow_request,
            metadata=WorkflowCommandMetadata(
                actor=actor,
                request_id=context.request_id,
                trace_id=context.trace_id,
                idempotency_key=context.idempotency_key,
                request_hash=context.request_hash,
                if_match=None,
                dispatch_trace_carrier=(
                    None if trace_context is None else TraceCarrier.inject(trace_context)
                ),
            ),
        )
        try:
            result = workflow.execute(command)
        except InvalidStateTransition as error:
            raise WorkflowGuard("workflow transition is not permitted") from error
        view = result.value
        if not isinstance(view, PatchArtifactReadViewV1):
            raise TypeError("project content draft returned another workflow view")
        updated_project = _port(dependencies).record_content_draft(
            project_id,
            preparation=preparation,
            patch_artifact_id=view.artifact.artifact_id,
            context=context,
        )
        response.headers["Location"] = f"/api/v1/patches/{view.artifact.artifact_id}"
        response.headers["X-Project-Revision"] = str(updated_project.revision)
        response.headers["X-Identity-Alias-Groups"] = str(
            preparation.normalization_summary.alias_group_count
        )
        response.headers["X-Identity-Auto-Merges"] = str(
            preparation.normalization_summary.auto_merge_count
        )
        _resource_headers(
            response,
            resource_kind=result.resource_kind,
            resource_id=result.resource_id,
            revision=result.revision,
        )
        return view

    return router


__all__ = ["project_router"]
