"""Resource-specific and generic Run admission transport (M4c Task 8).

Each resource endpoint fixes its own RunKind/version; ``POST /runs`` accepts only
``generic_runs_endpoint`` kinds. ``internal_only`` kinds (migrate/DR) are not
reachable here — they flow only through a trusted internal platform call. Every
endpoint returns ``202 RunAccepted``; asynchronous failures become RunFailure/Event
later, never a retroactive HTTP error.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status

from gameforge.apps.api.dependencies import (
    ApiDependencies,
    RunAdmissionPort,
    api_dependencies,
    require_actor,
)
from gameforge.contracts.api import (
    ConstraintProposeRequestV1,
    GenerationProposeRequestV1,
    PatchRepairRequestV1,
    PlaytestRunRequestV1,
    RunAcceptedV1,
    RunSubmissionRequestV1,
    TaskSuiteDeriveRequestV1,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import DependencyUnavailable, RequestSchemaInvalid
from gameforge.contracts.identity import ActorContext
from gameforge.platform.runs.admission import AdmissionRequestContext

_IDEMPOTENCY_HEADER = "Idempotency-Key"


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


def _require_admission_headers(
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias=_IDEMPOTENCY_HEADER, min_length=1, max_length=512),
    ],
) -> None:
    del idempotency_key
    exact = _single_header(request, _IDEMPOTENCY_HEADER, max_length=512)
    request.state.run_admission_idempotency_key = exact


def _admission_context(
    request: Request,
    *,
    operation: str,
    payload: object,
) -> AdmissionRequestContext:
    idempotency_key = getattr(request.state, "run_admission_idempotency_key", None)
    if not isinstance(idempotency_key, str):
        raise RequestSchemaInvalid("run admission idempotency key is unavailable")
    request_hash = canonical_sha256(
        {
            "request_hash_schema_version": "run-admission-request-hash@1",
            "api_version": "v1",
            "operation": operation,
            "method": request.method,
            "path": request.url.path,
            "payload": payload.model_dump(mode="json", by_alias=True),  # type: ignore[attr-defined]
        }
    )
    return AdmissionRequestContext(
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        trace_id=getattr(request.state, "trace_id", None),
    )


def _port(dependencies: ApiDependencies) -> RunAdmissionPort:
    port = dependencies.run_admission
    if port is None:
        raise DependencyUnavailable(
            "run admission authority is unavailable",
            component="run_admission",
        )
    return port


def _set_run_headers(response: Response, accepted: RunAcceptedV1) -> None:
    response.headers["Location"] = accepted.status_url
    response.headers["Cache-Control"] = "private, no-cache"


def run_admission_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["runs"],
        dependencies=[Depends(_require_admission_headers)],
    )

    @router.post("/runs", response_model=RunAcceptedV1, status_code=status.HTTP_202_ACCEPTED)
    def submit_run(
        payload: RunSubmissionRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> RunAcceptedV1:
        accepted = _port(dependencies).admit_generic_run(
            params=payload.params,
            actor=actor,
            server=_admission_context(request, operation="run.submit", payload=payload),
            llm_execution_mode=payload.llm_execution_mode,
            seed=payload.seed,
            execution_version_plan=payload.execution_version_plan,
            cassette_artifact_id=payload.cassette_artifact_id,
        )
        _set_run_headers(response, accepted)
        return accepted

    @router.post(
        "/generations:propose",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def propose_generation(
        payload: GenerationProposeRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> RunAcceptedV1:
        accepted = _port(dependencies).admit_generation(
            base_snapshot_artifact_id=payload.base_snapshot_artifact_id,
            constraint_snapshot_artifact_id=payload.constraint_snapshot_artifact_id,
            findings=payload.findings,
            objective_goal_text=payload.objective_goal_text,
            domain_scope=payload.domain_scope,
            target=payload.target,
            generation_policy=payload.generation_policy,
            candidate_export_profiles=payload.candidate_export_profiles,
            actor=actor,
            server=_admission_context(request, operation="generation.propose", payload=payload),
            llm_execution_mode=payload.llm_execution_mode,
            execution_version_plan=payload.execution_version_plan,
            cassette_artifact_id=payload.cassette_artifact_id,
        )
        _set_run_headers(response, accepted)
        return accepted

    @router.post(
        "/constraints:propose",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def propose_constraint(
        payload: ConstraintProposeRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> RunAcceptedV1:
        accepted = _port(dependencies).admit_constraint_proposal(
            source_artifact_ids=payload.source_artifact_ids,
            base_constraint_snapshot_artifact_id=payload.base_constraint_snapshot_artifact_id,
            authoring_goal_text=payload.authoring_goal_text,
            domain_scope=payload.domain_scope,
            dsl_grammar_version=payload.dsl_grammar_version,
            extraction_policy=payload.extraction_policy,
            actor=actor,
            server=_admission_context(request, operation="constraint.propose", payload=payload),
            llm_execution_mode=payload.llm_execution_mode,
            execution_version_plan=payload.execution_version_plan,
            cassette_artifact_id=payload.cassette_artifact_id,
        )
        _set_run_headers(response, accepted)
        return accepted

    @router.post(
        "/patches/{artifact_id}:repair",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def repair_patch(
        artifact_id: str,
        payload: PatchRepairRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> RunAcceptedV1:
        # The path {artifact_id} binds the repair subject. The typed params also carry
        # the subject patch id; they must agree, so the URL cannot disagree with the
        # admitted run's subject (the path is never silently discarded).
        if artifact_id != payload.params.subject_patch_artifact_id:
            raise RequestSchemaInvalid(
                "repair path artifact_id must match params.subject_patch_artifact_id"
            )
        accepted = _port(dependencies).admit_resource_run(
            params=payload.params,
            actor=actor,
            server=_admission_context(request, operation="patch.repair", payload=payload),
            llm_execution_mode=payload.llm_execution_mode,
            seed=payload.seed,
            execution_version_plan=payload.execution_version_plan,
            cassette_artifact_id=payload.cassette_artifact_id,
        )
        _set_run_headers(response, accepted)
        return accepted

    @router.post(
        "/task-suites:derive",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def derive_task_suite(
        payload: TaskSuiteDeriveRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> RunAcceptedV1:
        accepted = _port(dependencies).admit_resource_run(
            params=payload.params,
            actor=actor,
            server=_admission_context(request, operation="task_suite.derive", payload=payload),
            llm_execution_mode="not_applicable",
            seed=None,
            execution_version_plan=None,
            cassette_artifact_id=None,
        )
        _set_run_headers(response, accepted)
        return accepted

    @router.post(
        "/playtest:run",
        response_model=RunAcceptedV1,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def run_playtest(
        payload: PlaytestRunRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> RunAcceptedV1:
        accepted = _port(dependencies).admit_resource_run(
            params=payload.params,
            actor=actor,
            server=_admission_context(request, operation="playtest.run", payload=payload),
            llm_execution_mode=payload.llm_execution_mode,
            seed=payload.seed,
            execution_version_plan=payload.execution_version_plan,
            cassette_artifact_id=payload.cassette_artifact_id,
        )
        _set_run_headers(response, accepted)
        return accepted

    return router


__all__ = ["run_admission_router"]
