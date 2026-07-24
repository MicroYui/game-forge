"""Side-effect-free M4c API composition root."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from typing import Any

from fastapi import FastAPI
from starlette.middleware import Middleware

from gameforge.apps.api.dependencies import ApiDependencies
from gameforge.apps.api.errors import install_error_handlers
from gameforge.apps.api.health import health_router
from gameforge.apps.api.middleware import (
    AuthenticationMiddleware,
    MAX_HTTP_REQUEST_BODY_BYTES,
    ProblemMiddleware,
    RequestBodyLimitMiddleware,
    RequestContextMiddleware,
)
from gameforge.apps.api.routers.auth import auth_router
from gameforge.apps.api.routers.artifacts import artifact_catalog_router
from gameforge.apps.api.routers.content import content_read_router
from gameforge.apps.api.routers.observability import observability_router
from gameforge.apps.api.routers.runs import run_admission_router
from gameforge.apps.api.routers.execution_options import execution_option_router
from gameforge.apps.api.routers.workflows import workflow_read_router
from gameforge.apps.api.routers.workflow_commands import workflow_command_router
from gameforge.apps.api.commands import run_commands_router
from gameforge.apps.api.streaming import run_events_router
from gameforge.contracts.errors import DependencyUnavailable
from gameforge.platform.read_models.content import ContentReadService
from gameforge.platform.read_models.observability import ObservabilityReadService
from gameforge.platform.read_models.workflows import WorkflowReadService


def _unavailable_read_uow(component: str) -> Callable[[], Any]:
    """Keep the frozen API surface visible while missing composition fails closed."""

    @contextmanager
    def unavailable() -> Iterator[None]:
        raise DependencyUnavailable(
            "read authority is unavailable",
            component=component,
        )
        yield  # pragma: no cover - preserves the contextmanager contract

    return unavailable


def create_app(
    dependencies: ApiDependencies | None = None,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    selected = dependencies or ApiDependencies()
    app = FastAPI(
        title="GameForge API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        middleware=[
            Middleware(RequestContextMiddleware, dependencies=selected),
            Middleware(ProblemMiddleware),
            Middleware(
                RequestBodyLimitMiddleware,
                max_body_bytes=MAX_HTTP_REQUEST_BODY_BYTES,
            ),
            Middleware(AuthenticationMiddleware, dependencies=selected),
        ],
    )
    app.state.dependencies = selected
    install_error_handlers(app)
    app.include_router(health_router(selected.readiness))
    app.include_router(auth_router())
    content_reads = selected.content_reads or ContentReadService(
        uow_factory=_unavailable_read_uow("content_read_authority"),
        max_materialized_items=1,
    )
    workflow_reads = selected.workflow_reads or WorkflowReadService(
        unit_of_work=_unavailable_read_uow("workflow_read_authority"),
        max_materialized_items=1,
    )
    observability_reads = selected.observability_reads or ObservabilityReadService(
        unit_of_work=_unavailable_read_uow("observability_read_authority"),
    )
    app.include_router(artifact_catalog_router(content_reads))
    app.include_router(content_read_router(content_reads))
    app.include_router(workflow_read_router(workflow_reads))
    app.include_router(observability_router(observability_reads))
    app.include_router(workflow_command_router())
    app.include_router(run_admission_router())
    app.include_router(execution_option_router())
    app.include_router(run_events_router())
    app.include_router(run_commands_router())
    return app


__all__ = ["create_app"]
