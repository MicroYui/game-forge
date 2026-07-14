"""Side-effect-free M4c API composition root."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI
from starlette.middleware import Middleware

from gameforge.apps.api.dependencies import ApiDependencies
from gameforge.apps.api.errors import install_error_handlers
from gameforge.apps.api.health import health_router
from gameforge.apps.api.middleware import (
    AuthenticationMiddleware,
    ProblemMiddleware,
    RequestContextMiddleware,
)
from gameforge.apps.api.routers.auth import auth_router


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
            Middleware(AuthenticationMiddleware, dependencies=selected),
        ],
    )
    app.state.dependencies = selected
    install_error_handlers(app)
    app.include_router(health_router(selected.readiness))
    app.include_router(auth_router())
    return app


__all__ = ["create_app"]
