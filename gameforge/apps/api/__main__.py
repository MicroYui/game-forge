"""Uvicorn entry point for the API process only."""

from __future__ import annotations

import uvicorn


def run_api(*, host: str = "127.0.0.1", port: int = 8000) -> None:
    uvicorn.run(
        "gameforge.apps.api.app:create_app",
        factory=True,
        host=host,
        port=port,
    )


def main() -> None:
    run_api()


if __name__ == "__main__":
    main()
