"""Versioned, bounded content and catalog read endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import ValidationError

from gameforge.apps.api.dependencies import require_actor
from gameforge.apps.api.pagination import OpaquePageCursorCodec, to_opaque_page
from gameforge.bench.report_contracts import BenchReport
from gameforge.contracts.api import (
    ArtifactPayloadViewV1,
    ConstraintProposalReadViewV1,
    ConstraintSnapshotViewV1,
    GraphItemV1,
    LineageEntryV1,
    OpaquePageV1,
    PatchArtifactReadViewV1,
    RefHistoryEntryV1,
    ReviewArtifactViewV1,
    RollbackRequestReadViewV1,
    SchemaRegistryDocumentV1,
    SnapshotDiffHttpPageV1,
    SpecViewV1,
    TaskSuiteArtifactViewV1,
    compute_resource_etag,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation, RequestSchemaInvalid
from gameforge.contracts.execution_profiles import (
    ExecutionProfileKindV1,
    ExecutionProfileViewV1,
    RunKindRef,
)
from gameforge.contracts.identity import ActorContext
from gameforge.contracts.storage import PageCursorV1, PageV1
from gameforge.platform.read_models.content import ContentReadService


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


def content_read_router(
    service: ContentReadService,
    *,
    cursor_codec: OpaquePageCursorCodec | None = None,
) -> APIRouter:
    """Build content routers around an explicitly injected read service."""

    if not isinstance(service, ContentReadService):
        raise TypeError("service must be ContentReadService")
    codec = cursor_codec or OpaquePageCursorCodec()
    if not isinstance(codec, OpaquePageCursorCodec):
        raise TypeError("cursor_codec must be OpaquePageCursorCodec")
    router = APIRouter(prefix="/api/v1", tags=["content-reads"])

    @router.get("/artifacts/{artifact_id}", response_model=ArtifactPayloadViewV1)
    def artifact(
        artifact_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> ArtifactPayloadViewV1:
        value = service.get_artifact(actor.principal, artifact_id)
        _set_resource_headers(
            response,
            resource_kind="artifact",
            resource_id=value.artifact.artifact_id,
            revision=value.resource_revision,
        )
        return value

    @router.get("/specs", response_model=OpaquePageV1[SpecViewV1])
    def specs(
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[SpecViewV1]:
        page = service.list_specs(
            actor.principal,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get("/specs/{artifact_id}/graph", response_model=OpaquePageV1[GraphItemV1])
    def graph(
        artifact_id: str,
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[GraphItemV1]:
        page = service.list_graph(
            actor.principal,
            artifact_id,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get("/specs/{artifact_id}", response_model=SpecViewV1)
    def spec(
        artifact_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> SpecViewV1:
        value = service.get_spec(actor.principal, artifact_id)
        _set_resource_headers(
            response,
            resource_kind="spec",
            resource_id=value.artifact.artifact_id,
            revision=1 if value.ref_value is None else value.ref_value.revision,
        )
        return value

    @router.get(
        "/schema-registry/{version}",
        response_model=SchemaRegistryDocumentV1,
    )
    def schema_registry(
        version: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> SchemaRegistryDocumentV1:
        value = service.get_schema_registry(actor.principal, version)
        _set_resource_headers(
            response,
            resource_kind="schema_registry",
            resource_id=value.registry_version,
            revision=1,
        )
        return value

    @router.get(
        "/constraints",
        response_model=OpaquePageV1[ConstraintSnapshotViewV1],
    )
    def constraints(
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[ConstraintSnapshotViewV1]:
        page = service.list_constraints(
            actor.principal,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get("/constraints/{artifact_id}", response_model=ConstraintSnapshotViewV1)
    def constraint(
        artifact_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> ConstraintSnapshotViewV1:
        value = service.get_constraint(actor.principal, artifact_id)
        _set_resource_headers(
            response,
            resource_kind="constraint",
            resource_id=value.artifact.artifact_id,
            revision=1,
        )
        return value

    @router.get(
        "/constraint-proposals",
        response_model=OpaquePageV1[ConstraintProposalReadViewV1],
    )
    def constraint_proposals(
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[ConstraintProposalReadViewV1]:
        page = service.list_constraint_proposals(
            actor.principal,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get(
        "/constraint-proposals/{artifact_id}",
        response_model=ConstraintProposalReadViewV1,
    )
    def constraint_proposal(
        artifact_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> ConstraintProposalReadViewV1:
        value = service.get_constraint_proposal(actor.principal, artifact_id)
        _set_resource_headers(
            response,
            resource_kind="constraint_proposal",
            resource_id=value.artifact.artifact_id,
            revision=value.workflow_revision,
        )
        return value

    @router.get("/patches", response_model=OpaquePageV1[PatchArtifactReadViewV1])
    def patches(
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[PatchArtifactReadViewV1]:
        page = service.list_patches(
            actor.principal,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get("/patches/{artifact_id}", response_model=PatchArtifactReadViewV1)
    def patch(
        artifact_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> PatchArtifactReadViewV1:
        value = service.get_patch(actor.principal, artifact_id)
        _set_resource_headers(
            response,
            resource_kind="patch",
            resource_id=value.artifact.artifact_id,
            revision=value.workflow_revision,
        )
        return value

    @router.get(
        "/rollback-requests",
        response_model=OpaquePageV1[RollbackRequestReadViewV1],
    )
    def rollback_requests(
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[RollbackRequestReadViewV1]:
        page = service.list_rollback_requests(
            actor.principal,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get(
        "/rollback-requests/{artifact_id}",
        response_model=RollbackRequestReadViewV1,
    )
    def rollback_request(
        artifact_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> RollbackRequestReadViewV1:
        value = service.get_rollback_request(actor.principal, artifact_id)
        _set_resource_headers(
            response,
            resource_kind="rollback_request",
            resource_id=value.artifact.artifact_id,
            revision=value.workflow_revision,
        )
        return value

    @router.get("/reviews", response_model=OpaquePageV1[ReviewArtifactViewV1])
    def reviews(
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[ReviewArtifactViewV1]:
        page = service.list_reviews(
            actor.principal,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get("/reviews/{artifact_id}", response_model=ReviewArtifactViewV1)
    def review(
        artifact_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> ReviewArtifactViewV1:
        value = service.get_review(actor.principal, artifact_id)
        _set_resource_headers(
            response,
            resource_kind="review",
            resource_id=value.artifact.artifact_id,
            revision=1,
        )
        return value

    @router.get("/task-suites", response_model=OpaquePageV1[TaskSuiteArtifactViewV1])
    def task_suites(
        response: Response,
        config_artifact_id: str | None = None,
        constraint_artifact_id: str | None = None,
        environment_profile_id: str | None = None,
        environment_profile_version: int | None = None,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[TaskSuiteArtifactViewV1]:
        if (environment_profile_id is None) != (environment_profile_version is None):
            raise RequestSchemaInvalid(
                "environment_profile_id and environment_profile_version must be supplied together"
            )
        if environment_profile_version is not None and environment_profile_version < 1:
            raise RequestSchemaInvalid("environment_profile_version must be positive")
        page = service.list_task_suites(
            actor.principal,
            config_artifact_id=config_artifact_id,
            constraint_artifact_id=constraint_artifact_id,
            environment_profile_id=environment_profile_id,
            environment_profile_version=environment_profile_version,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get("/task-suites/{artifact_id}", response_model=TaskSuiteArtifactViewV1)
    def task_suite(
        artifact_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> TaskSuiteArtifactViewV1:
        value = service.get_task_suite(actor.principal, artifact_id)
        _set_resource_headers(
            response,
            resource_kind="task_suite",
            resource_id=value.artifact.artifact_id,
            revision=1,
        )
        return value

    @router.get("/playtest/{run_id}/result", response_model=ArtifactPayloadViewV1)
    def playtest_result(
        run_id: str,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> ArtifactPayloadViewV1:
        value = service.get_playtest_result(actor.principal, run_id)
        _set_resource_headers(
            response,
            resource_kind="playtest_result",
            resource_id=value.artifact.artifact_id,
            revision=value.resource_revision,
        )
        return value

    @router.get("/diff", response_model=SnapshotDiffHttpPageV1)
    def diff(
        response: Response,
        base: str,
        target: str,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> SnapshotDiffHttpPageV1:
        metadata, page = service.diff(
            actor.principal,
            base_snapshot_id=base,
            target_snapshot_id=target,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        opaque = to_opaque_page(page, codec=codec)
        _set_page_headers(response, opaque.read_snapshot_id)
        return SnapshotDiffHttpPageV1(diff=metadata, page=opaque)

    @router.get(
        "/artifacts/{artifact_id}/lineage",
        response_model=OpaquePageV1[LineageEntryV1],
    )
    def lineage(
        artifact_id: str,
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[LineageEntryV1]:
        page = service.lineage(
            actor.principal,
            artifact_id,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get(
        "/refs/{ref_name}/history",
        response_model=OpaquePageV1[RefHistoryEntryV1],
    )
    def ref_history(
        ref_name: str,
        response: Response,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[RefHistoryEntryV1]:
        page = service.ref_history(
            actor.principal,
            ref_name,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get("/bench/report", response_model=BenchReport)
    def bench_report(
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> BenchReport:
        payload = service.get_bench_report(actor.principal)
        try:
            value = BenchReport.model_validate(payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation(
                "selected BenchReport payload violates BenchReport v2"
            ) from exc
        _set_resource_headers(
            response,
            resource_kind="bench_report",
            resource_id=canonical_sha256(value.model_dump(mode="json")),
            revision=1,
        )
        return value

    @router.get(
        "/execution-profiles",
        response_model=OpaquePageV1[ExecutionProfileViewV1],
    )
    def execution_profiles(
        response: Response,
        profile_kind: ExecutionProfileKindV1 | None = None,
        run_kind: str | None = None,
        run_kind_version: int | None = None,
        domain_id: str | None = None,
        status: Literal["active", "replay_only", "disabled"] | None = None,
        cursor: str | None = None,
        limit: int = Query(default=100),
        actor: ActorContext = Depends(require_actor),
    ) -> OpaquePageV1[ExecutionProfileViewV1]:
        if (run_kind is None) != (run_kind_version is None):
            raise RequestSchemaInvalid("run_kind and run_kind_version must be supplied together")
        if domain_id == "":
            raise RequestSchemaInvalid("domain_id must be non-empty")
        try:
            run_ref = (
                None if run_kind is None else RunKindRef(kind=run_kind, version=run_kind_version)
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise RequestSchemaInvalid("run_kind and run_kind_version are invalid") from exc
        internal_page = service.list_execution_profiles(
            actor.principal,
            profile_kind=profile_kind,
            run_kind=run_ref,
            domain_id=domain_id,
            status=status,
            cursor=_cursor(cursor, codec),
            limit=limit,
        )
        page = PageV1[ExecutionProfileViewV1](
            read_snapshot_id=internal_page.read_snapshot_id,
            items=tuple(item.profile for item in internal_page.items),
            next_cursor=internal_page.next_cursor,
            expires_at=internal_page.expires_at,
        )
        value = to_opaque_page(page, codec=codec)
        _set_page_headers(response, value.read_snapshot_id)
        return value

    @router.get(
        "/execution-profiles/{profile_id}/versions/{version}",
        response_model=ExecutionProfileViewV1,
    )
    def execution_profile(
        profile_id: str,
        version: int,
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> ExecutionProfileViewV1:
        if version < 1:
            raise RequestSchemaInvalid("execution profile version must be positive")
        internal = service.get_execution_profile(
            actor.principal,
            profile_id=profile_id,
            version=version,
        )
        _set_resource_headers(
            response,
            resource_kind="execution_profile",
            resource_id=f"{profile_id}:version:{version}",
            revision=internal.catalog_version,
        )
        return internal.profile

    return router


__all__ = ["content_read_router"]
