"""Uvicorn entry point for the API process only."""

from __future__ import annotations

import uvicorn


def run_api(*, host: str = "127.0.0.1", port: int = 8000) -> None:
    uvicorn.run(
        "gameforge.apps.api.local:create_local_app",
        factory=True,
        host=host,
        port=port,
        ws_max_size=16_384,
    )


def main() -> None:
    run_api()


if __name__ == "__main__":
    main()
