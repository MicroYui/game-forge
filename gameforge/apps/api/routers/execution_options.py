"""Read-only resolution of exact Agent execution authority for the web console."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from gameforge.apps.api.dependencies import (
    ApiDependencies,
    ExecutionOptionResolvePort,
    api_dependencies,
    require_actor,
)
from gameforge.contracts.api import ExecutionOptionResolveRequestV1, ExecutionOptionViewV1
from gameforge.contracts.errors import DependencyUnavailable
from gameforge.contracts.identity import ActorContext


def _port(dependencies: ApiDependencies) -> ExecutionOptionResolvePort:
    port = dependencies.execution_options
    if port is None:
        raise DependencyUnavailable(
            "execution option resolver is unavailable",
            component="execution_options",
        )
    return port


def execution_option_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["execution-options"])

    @router.post(
        "/execution-options:resolve",
        response_model=ExecutionOptionViewV1,
    )
    def resolve_execution_option(
        payload: ExecutionOptionResolveRequestV1,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> ExecutionOptionViewV1:
        resolved = _port(dependencies).resolve_execution_option(
            request=payload,
            actor=actor,
        )
        response.headers["Cache-Control"] = "private, no-store"
        return resolved

    return router


__all__ = ["execution_option_router"]
