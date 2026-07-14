"""M4c API application factory.

Routers and middleware are installed in their ordered TDD tasks. Keeping the
factory side-effect free ensures importing the API never starts a worker.
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    return FastAPI(
        title="GameForge API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )


__all__ = ["create_app"]
