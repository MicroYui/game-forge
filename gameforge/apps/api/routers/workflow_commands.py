"""Versioned synchronous workflow command transport.

The router owns HTTP-only concerns. The injected port remains responsible for
resource authorization, optimistic concurrency, idempotency authority, and the
transactional workflow transition.
"""

from __future__ import annotations

from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, Header, Path, Request, Response, status
from pydantic import BaseModel

from gameforge.apps.api.dependencies import (
    ApiDependencies,
    WorkflowCommand,
    WorkflowCommandMetadata,
    WorkflowCommandOperation,
    api_dependencies,
    require_actor,
)
from gameforge.contracts.api import (
    ApprovalDecisionRequestV1,
    ApprovalViewV1,
    BoundedId,
    ConstraintProposalReadViewV1,
    ConstraintValidationAdmissionRequestV1,
    HumanConstraintDraftRequestV1,
    HumanConstraintRevisionRequestV1,
    HumanPatchDraftRequestV1,
    HumanSpecUploadRequestV1,
    PatchArtifactReadViewV1,
    PatchRebaseRequestV1,
    PatchValidationAdmissionRequestV1,
    ResolveConflictsRequestV1,
    RollbackDraftRequestV1,
    RollbackRequestReadViewV1,
    RollbackValidationAdmissionRequestV1,
    RunAcceptedV1,
    SpecViewV1,
    SubmitForApprovalRequestV1,
    WorkflowApplyRequestV1,
    WorkflowApplyResultV1,
    WorkflowCommandResponseV1,
    compute_resource_etag,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.diff import RebaseResult
from gameforge.contracts.errors import (
    DependencyUnavailable,
    InvalidStateTransition,
    RequestSchemaInvalid,
    WorkflowGuard,
)
from gameforge.contracts.identity import ActorContext
from gameforge.runtime.observability.context import TraceCarrier, current_trace_context


_PayloadT = TypeVar("_PayloadT", bound=BaseModel)
_IDEMPOTENCY_HEADER = "Idempotency-Key"
_IF_MATCH_HEADER = "If-Match"


def _valid_header_value(value: str, *, max_length: int) -> bool:
    return bool(
        value
        and value == value.strip()
        and len(value) <= max_length
        and not any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    )


def _is_strong_entity_tag(value: str) -> bool:
    return bool(
        len(value) >= 3
        and value[0] == '"'
        and value[-1] == '"'
        and "," not in value
        and not value.startswith("W/")
    )


def _single_header(request: Request, name: str, *, max_length: int) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise RequestSchemaInvalid(f"{name} must be supplied exactly once")
    value = values[0]
    if not _valid_header_value(value, max_length=max_length):
        raise RequestSchemaInvalid(f"{name} is invalid")
    return value


def _command_metadata(
    request: Request,
    *,
    actor: ActorContext,
    operation: WorkflowCommandOperation,
    payload: BaseModel,
) -> WorkflowCommandMetadata:
    headers = getattr(request.state, "workflow_command_headers", None)
    if not isinstance(headers, tuple) or len(headers) != 2:
        raise RequestSchemaInvalid("workflow command headers are unavailable")
    idempotency_key, if_match = headers
    hash_payload = {
        "request_hash_schema_version": "workflow-command-request-hash@1",
        "api_version": "v1",
        "operation": operation,
        "method": request.method,
        "path": request.url.path,
        "payload": payload.model_dump(mode="json", by_alias=True),
    }
    hash_if_match = if_match
    if hash_if_match is None:
        legacy_hash_if_match = getattr(
            request.state,
            "workflow_create_hash_if_match",
            None,
        )
        if isinstance(legacy_hash_if_match, str):
            hash_if_match = legacy_hash_if_match
    if hash_if_match is not None:
        hash_payload["if_match"] = hash_if_match
    request_hash = canonical_sha256(hash_payload)
    trace_context = current_trace_context()
    return WorkflowCommandMetadata(
        actor=actor,
        request_id=request.state.request_id,
        trace_id=getattr(request.state, "trace_id", None),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        if_match=if_match,
        dispatch_trace_carrier=(
            None if trace_context is None else TraceCarrier.inject(trace_context)
        ),
    )


def _require_command_headers(
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias=_IDEMPOTENCY_HEADER, min_length=1, max_length=512),
    ],
    if_match: Annotated[
        str,
        Header(alias=_IF_MATCH_HEADER, min_length=1, max_length=512),
    ],
) -> None:
    del idempotency_key, if_match
    exact_idempotency_key = _single_header(
        request,
        _IDEMPOTENCY_HEADER,
        max_length=512,
    )
    exact_if_match = _single_header(request, _IF_MATCH_HEADER, max_length=512)
    if not _is_strong_entity_tag(exact_if_match):
        raise RequestSchemaInvalid("If-Match must contain one strong quoted entity tag")
    request.state.workflow_command_headers = (
        exact_idempotency_key,
        exact_if_match,
    )


def _require_create_headers(
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias=_IDEMPOTENCY_HEADER, min_length=1, max_length=512),
    ],
) -> None:
    del idempotency_key
    exact_idempotency_key = _single_header(
        request,
        _IDEMPOTENCY_HEADER,
        max_length=512,
    )
    request.state.workflow_command_headers = (exact_idempotency_key, None)
    # Pre-D2 creates persisted a valid strong If-Match in the @1 idempotency hash.
    # It remains hash-only during the upgrade; create OCC never consumes this header.
    legacy_if_match = request.headers.getlist(_IF_MATCH_HEADER)
    if len(legacy_if_match) == 1:
        candidate = legacy_if_match[0]
        if _valid_header_value(candidate, max_length=512) and _is_strong_entity_tag(candidate):
            request.state.workflow_create_hash_if_match = candidate


def _set_command_headers(response: Response, result: object) -> None:
    resource_kind = getattr(result, "resource_kind", None)
    resource_id = getattr(result, "resource_id", None)
    revision = getattr(result, "revision", None)
    if (
        not isinstance(resource_kind, str)
        or not isinstance(resource_id, str)
        or not isinstance(revision, int)
    ):
        raise TypeError("workflow command port returned an invalid result")
    response.headers["ETag"] = compute_resource_etag(
        resource_kind=resource_kind,
        resource_id=resource_id,
        revision=revision,
    )
    response.headers["X-Resource-Revision"] = str(revision)
    response.headers["Cache-Control"] = "private, no-cache"


def _execute(
    *,
    dependencies: ApiDependencies,
    request: Request,
    response: Response,
    actor: ActorContext,
    operation: WorkflowCommandOperation,
    resource_kind: str,
    resource_id: str,
    payload: _PayloadT,
) -> WorkflowCommandResponseV1:
    port = dependencies.workflow_commands
    if port is None:
        raise DependencyUnavailable(
            "workflow command authority is unavailable",
            component="workflow_command_authority",
        )
    command = WorkflowCommand(
        operation=operation,
        resource_kind=resource_kind,
        resource_id=resource_id,
        payload=payload,
        metadata=_command_metadata(
            request,
            actor=actor,
            operation=operation,
            payload=payload,
        ),
    )
    try:
        result = port.execute(command)
    except InvalidStateTransition as error:
        raise WorkflowGuard("workflow transition is not permitted") from error
    _set_command_headers(response, result)
    return result.value


def workflow_command_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["workflow-commands"])
    create_router = APIRouter(dependencies=[Depends(_require_create_headers)])
    versioned_router = APIRouter(dependencies=[Depends(_require_command_headers)])

    @create_router.post("/specs", response_model=SpecViewV1, status_code=status.HTTP_201_CREATED)
    def upload_spec(
        payload: HumanSpecUploadRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation="spec.upload",
            resource_kind="spec_ref",
            resource_id=payload.ref_name,
            payload=payload,
        )

    @create_router.post(
        "/patches",
        response_model=PatchArtifactReadViewV1,
        status_code=status.HTTP_201_CREATED,
    )
    def draft_patch(
        payload: HumanPatchDraftRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation="patch.draft",
            resource_kind="patch_series",
            resource_id=payload.base_snapshot_artifact_id,
            payload=payload,
        )

    @create_router.post(
        "/constraint-proposals",
        response_model=ConstraintProposalReadViewV1,
        status_code=status.HTTP_201_CREATED,
    )
    def draft_constraint(
        payload: HumanConstraintDraftRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation="constraint.draft",
            resource_kind="constraint_ref",
            resource_id=payload.ref_name,
            payload=payload,
        )

    @versioned_router.post(
        "/constraint-proposals/{artifact_id}:revise",
        response_model=ConstraintProposalReadViewV1,
        status_code=status.HTTP_201_CREATED,
    )
    def revise_constraint(
        artifact_id: BoundedId,
        payload: HumanConstraintRevisionRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation="constraint.revise",
            resource_kind="constraint_proposal",
            resource_id=artifact_id,
            payload=payload,
        )

    @versioned_router.post(
        "/patches/{artifact_id}:validate",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def validate_patch(
        artifact_id: BoundedId,
        payload: PatchValidationAdmissionRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation="patch.validate",
            resource_kind="patch",
            resource_id=artifact_id,
            payload=payload,
        )

    @versioned_router.post(
        "/constraint-proposals/{artifact_id}:validate",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def validate_constraint(
        artifact_id: BoundedId,
        payload: ConstraintValidationAdmissionRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation="constraint.validate",
            resource_kind="constraint_proposal",
            resource_id=artifact_id,
            payload=payload,
        )

    @versioned_router.post(
        "/rollback-requests/{artifact_id}:validate",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def validate_rollback(
        artifact_id: BoundedId,
        payload: RollbackValidationAdmissionRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation="rollback.validate",
            resource_kind="rollback_request",
            resource_id=artifact_id,
            payload=payload,
        )

    def _submit(
        *,
        operation: WorkflowCommandOperation,
        resource_kind: str,
        artifact_id: BoundedId,
        payload: SubmitForApprovalRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext,
        dependencies: ApiDependencies,
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation=operation,
            resource_kind=resource_kind,
            resource_id=artifact_id,
            payload=payload,
        )

    @versioned_router.post(
        "/patches/{artifact_id}:submit-for-approval", response_model=ApprovalViewV1
    )
    def submit_patch(
        artifact_id: BoundedId,
        payload: SubmitForApprovalRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _submit(
            operation="patch.submit",
            resource_kind="patch",
            artifact_id=artifact_id,
            payload=payload,
            request=request,
            response=response,
            actor=actor,
            dependencies=dependencies,
        )

    @versioned_router.post(
        "/constraint-proposals/{artifact_id}:submit-for-approval",
        response_model=ApprovalViewV1,
    )
    def submit_constraint(
        artifact_id: BoundedId,
        payload: SubmitForApprovalRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _submit(
            operation="constraint.submit",
            resource_kind="constraint_proposal",
            artifact_id=artifact_id,
            payload=payload,
            request=request,
            response=response,
            actor=actor,
            dependencies=dependencies,
        )

    @versioned_router.post(
        "/rollback-requests/{artifact_id}:submit-for-approval",
        response_model=ApprovalViewV1,
    )
    def submit_rollback(
        artifact_id: BoundedId,
        payload: SubmitForApprovalRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _submit(
            operation="rollback.submit",
            resource_kind="rollback_request",
            artifact_id=artifact_id,
            payload=payload,
            request=request,
            response=response,
            actor=actor,
            dependencies=dependencies,
        )

    def _decision(
        *,
        operation: WorkflowCommandOperation,
        expected_decision: str,
        approval_id: BoundedId,
        payload: ApprovalDecisionRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext,
        dependencies: ApiDependencies,
    ) -> WorkflowCommandResponseV1:
        if payload.decision != expected_decision:
            raise RequestSchemaInvalid("approval decision does not match the command endpoint")
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation=operation,
            resource_kind="approval",
            resource_id=approval_id,
            payload=payload,
        )

    @versioned_router.post("/approvals/{approval_id}:approve", response_model=ApprovalViewV1)
    def approve(
        approval_id: BoundedId,
        payload: ApprovalDecisionRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _decision(
            operation="approval.approve",
            expected_decision="approve",
            approval_id=approval_id,
            payload=payload,
            request=request,
            response=response,
            actor=actor,
            dependencies=dependencies,
        )

    @versioned_router.post("/approvals/{approval_id}:reject", response_model=ApprovalViewV1)
    def reject(
        approval_id: BoundedId,
        payload: ApprovalDecisionRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _decision(
            operation="approval.reject",
            expected_decision="reject",
            approval_id=approval_id,
            payload=payload,
            request=request,
            response=response,
            actor=actor,
            dependencies=dependencies,
        )

    @versioned_router.post(
        "/approvals/{approval_id}:request_changes", response_model=ApprovalViewV1
    )
    def request_changes(
        approval_id: BoundedId,
        payload: ApprovalDecisionRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _decision(
            operation="approval.request_changes",
            expected_decision="request_changes",
            approval_id=approval_id,
            payload=payload,
            request=request,
            response=response,
            actor=actor,
            dependencies=dependencies,
        )

    def _apply(
        *,
        operation: WorkflowCommandOperation,
        resource_kind: str,
        artifact_id: BoundedId,
        payload: WorkflowApplyRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext,
        dependencies: ApiDependencies,
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation=operation,
            resource_kind=resource_kind,
            resource_id=artifact_id,
            payload=payload,
        )

    @versioned_router.post("/patches/{artifact_id}:apply", response_model=WorkflowApplyResultV1)
    def apply_patch(
        artifact_id: BoundedId,
        payload: WorkflowApplyRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _apply(
            operation="patch.apply",
            resource_kind="patch",
            artifact_id=artifact_id,
            payload=payload,
            request=request,
            response=response,
            actor=actor,
            dependencies=dependencies,
        )

    @versioned_router.post(
        "/constraint-proposals/{artifact_id}:publish",
        response_model=WorkflowApplyResultV1,
    )
    def publish_constraint(
        artifact_id: BoundedId,
        payload: WorkflowApplyRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _apply(
            operation="constraint.publish",
            resource_kind="constraint_proposal",
            artifact_id=artifact_id,
            payload=payload,
            request=request,
            response=response,
            actor=actor,
            dependencies=dependencies,
        )

    @versioned_router.post(
        "/rollback-requests/{artifact_id}:apply",
        response_model=WorkflowApplyResultV1,
    )
    def apply_rollback(
        artifact_id: BoundedId,
        payload: WorkflowApplyRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _apply(
            operation="rollback.apply",
            resource_kind="rollback_request",
            artifact_id=artifact_id,
            payload=payload,
            request=request,
            response=response,
            actor=actor,
            dependencies=dependencies,
        )

    @versioned_router.post("/patches/{artifact_id}:rebase", response_model=RebaseResult)
    def rebase_patch(
        artifact_id: BoundedId,
        payload: PatchRebaseRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation="patch.rebase",
            resource_kind="patch",
            resource_id=artifact_id,
            payload=payload,
        )

    @versioned_router.post("/patches/{artifact_id}:resolve-conflicts", response_model=RebaseResult)
    def resolve_patch_conflicts(
        artifact_id: BoundedId,
        payload: ResolveConflictsRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation="patch.resolve_conflicts",
            resource_kind="patch",
            resource_id=artifact_id,
            payload=payload,
        )

    @create_router.post(
        "/refs/{ref_name:path}/rollback-requests",
        response_model=RollbackRequestReadViewV1,
        status_code=status.HTTP_201_CREATED,
    )
    def draft_rollback(
        ref_name: Annotated[
            BoundedId,
            Path(
                description=(
                    "Exact ref name; slash-delimited refs are preserved as one path parameter."
                )
            ),
        ],
        payload: RollbackDraftRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> WorkflowCommandResponseV1:
        return _execute(
            dependencies=dependencies,
            request=request,
            response=response,
            actor=actor,
            operation="rollback.draft",
            resource_kind="ref",
            resource_id=ref_name,
            payload=payload,
        )

    router.include_router(create_router)
    router.include_router(versioned_router)
    return router


__all__ = ["workflow_command_router"]
