"""Bounded, payload-free Artifact catalog reads."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from gameforge.apps.api.dependencies import require_actor
from gameforge.apps.api.pagination import (
    OpaquePageCursorCodec,
    OpaquePageCursorParameter,
    PageLimitParameter,
    to_opaque_page,
)
from gameforge.contracts.api import ArtifactSummaryV1, OpaquePageV1
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.identity import ActorContext
from gameforge.contracts.lineage import ArtifactKind
from gameforge.contracts.storage import PageCursorV1
from gameforge.platform.read_models.content import ContentReadService


def _cursor(token: str | None, codec: OpaquePageCursorCodec) -> PageCursorV1 | None:
    return None if token is None else codec.decode(token)


def _set_page_headers(response: Response, snapshot_id: str) -> None:
    digest = canonical_sha256(
        {
            "etag_schema_version": "read-snapshot-etag@1",
            "read_snapshot_id": snapshot_id,
        }
    )
    response.headers["ETag"] = f'"{digest}"'
    response.headers["Cache-Control"] = "private, no-cache"


def artifact_catalog_router(
    service: ContentReadService,
    *,
    cursor_codec: OpaquePageCursorCodec | None = None,
) -> APIRouter:
    """Build the read-only Artifact catalog around retained content authority."""

    if not isinstance(service, ContentReadService):
        raise TypeError("service must be ContentReadService")
    codec = cursor_codec or OpaquePageCursorCodec()
    if not isinstance(codec, OpaquePageCursorCodec):
        raise TypeError("cursor_codec must be OpaquePageCursorCodec")
    router = APIRouter(prefix="/api/v1", tags=["content-reads"])

    @router.get("/artifacts", response_model=OpaquePageV1[ArtifactSummaryV1])
    def artifact_catalog(
        kind: ArtifactKind,
        response: Response,
        cursor: OpaquePageCursorParameter | None = None,
        limit: PageLimitParameter = 100,
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[ArtifactSummaryV1]:
        page = service.list_artifacts(
            actor.principal,
            kind=kind,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    return router


__all__ = ["artifact_catalog_router"]
