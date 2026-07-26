"""Read-only resolution of exact Agent execution authority for the web console."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from gameforge.apps.api.dependencies import (
    ApiDependencies,
    ExecutionOptionResolvePort,
    SelectableModelPort,
    api_dependencies,
    require_actor,
)
from gameforge.contracts.api import (
    ExecutionOptionResolveRequestV1,
    ExecutionOptionViewV1,
    SelectableModelPageV1,
)
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


def _models(dependencies: ApiDependencies) -> SelectableModelPort:
    port = dependencies.selectable_models
    if port is None:
        raise DependencyUnavailable(
            "selectable model reader is unavailable",
            component="selectable_models",
        )
    return port


def execution_option_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["execution-options"])

    @router.get("/models", response_model=SelectableModelPageV1)
    def selectable_models(
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> SelectableModelPageV1:
        page = _models(dependencies).list_selectable_models(principal=actor.principal)
        # Read live from the gateway each time the picker opens; a cached list would
        # offer a model that is no longer served.
        response.headers["Cache-Control"] = "private, no-store"
        return page

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
