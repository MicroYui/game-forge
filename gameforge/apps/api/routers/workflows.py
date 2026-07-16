"""Authorized bounded workflow read endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response

from gameforge.apps.api.dependencies import require_actor
from gameforge.apps.api.pagination import OpaquePageCursorCodec, to_opaque_page
from gameforge.contracts.api import (
    ApprovalViewV1,
    OpaquePageV1,
    RunViewV1,
    compute_resource_etag,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.diff import MergeConflict
from gameforge.contracts.findings import FindingRevisionV1
from gameforge.contracts.identity import ActorContext
from gameforge.contracts.jobs import RunCommandViewV1, RunStatus
from gameforge.contracts.storage import PageCursorV1
from gameforge.platform.read_models.workflows import WorkflowReadService


def _cursor(token: str | None, codec: OpaquePageCursorCodec) -> PageCursorV1 | None:
    return None if token is None else codec.decode(token)


def _set_resource_headers(
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


def _set_page_headers(response: Response, snapshot_id: str) -> None:
    digest = canonical_sha256(
        {
            "etag_schema_version": "read-snapshot-etag@1",
            "read_snapshot_id": snapshot_id,
        }
    )
    response.headers["ETag"] = f'"{digest}"'
    response.headers["Cache-Control"] = "private, no-cache"


def workflow_read_router(
    service: WorkflowReadService,
    *,
    cursor_codec: OpaquePageCursorCodec | None = None,
) -> APIRouter:
    """Build routers around an explicitly injected, transaction-aware read service."""

    if not isinstance(service, WorkflowReadService):
        raise TypeError("service must be WorkflowReadService")
    codec = cursor_codec or OpaquePageCursorCodec()
    if not isinstance(codec, OpaquePageCursorCodec):
        raise TypeError("cursor_codec must be OpaquePageCursorCodec")
    router = APIRouter(prefix="/api/v1", tags=["workflow-reads"])

    @router.get("/approvals", response_model=OpaquePageV1[ApprovalViewV1])
    def approvals(
        response: Response,
        assignee: Literal["me"] | None = None,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[ApprovalViewV1]:
        page = service.list_approvals(
            actor.principal,
            assignee=assignee,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        projected = to_opaque_page(page, codec=codec)
        _set_page_headers(response, projected.read_snapshot_id)
        return projected

    @router.get("/approvals/{approval_id}", response_model=ApprovalViewV1)
    def approval(
        approval_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> ApprovalViewV1:
        view = service.get_approval(actor.principal, approval_id)
        _set_resource_headers(
            response,
            resource_kind="approval",
            resource_id=view.approval.approval_id,
            revision=view.approval.workflow_revision,
        )
        return view

    @router.get("/runs", response_model=OpaquePageV1[RunViewV1])
    def runs(
        response: Response,
        status: RunStatus | None = None,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[RunViewV1]:
        page = service.list_runs(
            actor.principal,
            status=status,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        projected = to_opaque_page(page, codec=codec)
        _set_page_headers(response, projected.read_snapshot_id)
        return projected

    @router.get("/runs/{run_id}", response_model=RunViewV1)
    def run(
        run_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> RunViewV1:
        view = service.get_run(actor.principal, run_id)
        _set_resource_headers(
            response,
            resource_kind="run",
            resource_id=view.run_id,
            revision=view.revision,
        )
        return view

    @router.get(
        "/runs/{run_id}/findings",
        response_model=OpaquePageV1[FindingRevisionV1],
    )
    def run_findings(
        run_id: str,
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[FindingRevisionV1]:
        page = service.list_run_findings(
            actor.principal,
            run_id,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        projected = to_opaque_page(page, codec=codec)
        _set_page_headers(response, projected.read_snapshot_id)
        return projected

    @router.get(
        "/runs/{run_id}/commands",
        response_model=OpaquePageV1[RunCommandViewV1],
    )
    def run_commands(
        run_id: str,
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[RunCommandViewV1]:
        page = service.list_run_commands(
            actor.principal,
            run_id,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        projected = to_opaque_page(page, codec=codec)
        _set_page_headers(response, projected.read_snapshot_id)
        return projected

    @router.get("/findings", response_model=OpaquePageV1[FindingRevisionV1])
    def findings(
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[FindingRevisionV1]:
        page = service.list_findings(
            actor.principal,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        projected = to_opaque_page(page, codec=codec)
        _set_page_headers(response, projected.read_snapshot_id)
        return projected

    @router.get(
        "/findings/{finding_id}/revisions/{revision}",
        response_model=FindingRevisionV1,
    )
    def exact_finding(
        finding_id: str,
        revision: int,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> FindingRevisionV1:
        value = service.get_finding(actor.principal, finding_id, revision=revision)
        _set_resource_headers(
            response,
            resource_kind="finding",
            resource_id=value.finding_id,
            revision=value.revision,
        )
        return value

    @router.get("/findings/{finding_id}", response_model=FindingRevisionV1)
    def latest_finding(
        finding_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> FindingRevisionV1:
        value = service.get_finding(actor.principal, finding_id)
        _set_resource_headers(
            response,
            resource_kind="finding",
            resource_id=value.finding_id,
            revision=value.revision,
        )
        return value

    @router.get(
        "/conflict-sets/{conflict_set_id}/conflicts",
        response_model=OpaquePageV1[MergeConflict],
    )
    def conflicts(
        conflict_set_id: str,
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[MergeConflict]:
        page = service.list_conflicts(
            actor.principal,
            conflict_set_id,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        projected = to_opaque_page(page, codec=codec)
        _set_page_headers(response, projected.read_snapshot_id)
        return projected

    return router


__all__ = ["workflow_read_router"]
