"""Versioned synchronous workflow command transport.

The router owns HTTP-only concerns. The injected port remains responsible for
resource authorization, optimistic concurrency, idempotency authority, and the
transactional workflow transition.
"""

from __future__ import annotations

from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, Header, Request, Response, status
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


_PayloadT = TypeVar("_PayloadT", bound=BaseModel)
_IDEMPOTENCY_HEADER = "Idempotency-Key"
_IF_MATCH_HEADER = "If-Match"


def _single_header(request: Request, name: str, *, max_length: int) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise RequestSchemaInvalid(f"{name} must be supplied exactly once")
    value = values[0]
    if (
        not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    ):
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
    request_hash = canonical_sha256(
        {
            "request_hash_schema_version": "workflow-command-request-hash@1",
            "api_version": "v1",
            "operation": operation,
            "method": request.method,
            "path": request.url.path,
            "if_match": if_match,
            "payload": payload.model_dump(mode="json", by_alias=True),
        }
    )
    return WorkflowCommandMetadata(
        actor=actor,
        request_id=request.state.request_id,
        trace_id=getattr(request.state, "trace_id", None),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        if_match=if_match,
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
    if (
        len(exact_if_match) < 3
        or exact_if_match[0] != '"'
        or exact_if_match[-1] != '"'
        or "," in exact_if_match
        or exact_if_match.startswith("W/")
    ):
        raise RequestSchemaInvalid("If-Match must contain one strong quoted entity tag")
    request.state.workflow_command_headers = (
        exact_idempotency_key,
        exact_if_match,
    )


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
    router = APIRouter(
        prefix="/api/v1",
        tags=["workflow-commands"],
        dependencies=[Depends(_require_command_headers)],
    )

    @router.post("/specs", response_model=SpecViewV1, status_code=status.HTTP_201_CREATED)
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

    @router.post(
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

    @router.post(
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

    @router.post(
        "/constraint-proposals/{artifact_id}:revise",
        response_model=ConstraintProposalReadViewV1,
        status_code=status.HTTP_201_CREATED,
    )
    def revise_constraint(
        artifact_id: str,
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

    @router.post(
        "/patches/{artifact_id}:validate",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def validate_patch(
        artifact_id: str,
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

    @router.post(
        "/constraint-proposals/{artifact_id}:validate",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def validate_constraint(
        artifact_id: str,
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

    @router.post(
        "/rollback-requests/{artifact_id}:validate",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def validate_rollback(
        artifact_id: str,
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
        artifact_id: str,
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

    @router.post("/patches/{artifact_id}:submit-for-approval", response_model=ApprovalViewV1)
    def submit_patch(
        artifact_id: str,
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

    @router.post(
        "/constraint-proposals/{artifact_id}:submit-for-approval",
        response_model=ApprovalViewV1,
    )
    def submit_constraint(
        artifact_id: str,
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

    @router.post(
        "/rollback-requests/{artifact_id}:submit-for-approval",
        response_model=ApprovalViewV1,
    )
    def submit_rollback(
        artifact_id: str,
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
        approval_id: str,
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

    @router.post("/approvals/{approval_id}:approve", response_model=ApprovalViewV1)
    def approve(
        approval_id: str,
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

    @router.post("/approvals/{approval_id}:reject", response_model=ApprovalViewV1)
    def reject(
        approval_id: str,
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

    @router.post("/approvals/{approval_id}:request_changes", response_model=ApprovalViewV1)
    def request_changes(
        approval_id: str,
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
        artifact_id: str,
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

    @router.post("/patches/{artifact_id}:apply", response_model=WorkflowApplyResultV1)
    def apply_patch(
        artifact_id: str,
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

    @router.post(
        "/constraint-proposals/{artifact_id}:publish",
        response_model=WorkflowApplyResultV1,
    )
    def publish_constraint(
        artifact_id: str,
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

    @router.post(
        "/rollback-requests/{artifact_id}:apply",
        response_model=WorkflowApplyResultV1,
    )
    def apply_rollback(
        artifact_id: str,
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

    @router.post("/patches/{artifact_id}:rebase", response_model=RebaseResult)
    def rebase_patch(
        artifact_id: str,
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

    @router.post("/patches/{artifact_id}:resolve-conflicts", response_model=RebaseResult)
    def resolve_patch_conflicts(
        artifact_id: str,
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

    @router.post(
        "/refs/{ref_name}/rollback-requests",
        response_model=RollbackRequestReadViewV1,
        status_code=status.HTTP_201_CREATED,
    )
    def draft_rollback(
        ref_name: str,
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

    return router


__all__ = ["workflow_command_router"]
