from __future__ import annotations

from importlib.metadata import version

from fastapi import FastAPI

from gameforge.apps.api.app import create_app
from gameforge.apps.api.__main__ import run_api


def test_m4c_framework_versions_are_exactly_locked() -> None:
    assert version("fastapi") == "0.139.0"
    assert version("uvicorn") == "0.51.0"
    assert version("argon2-cffi") == "25.1.0"
    assert version("pydantic") == "2.13.4"


def test_api_factory_has_no_implicit_worker_runtime() -> None:
    app = create_app()

    assert isinstance(app, FastAPI)
    assert app.title == "GameForge API"
    assert not hasattr(app.state, "worker")


def test_api_cli_invokes_uvicorn_factory_without_worker(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(target: str, **kwargs: object) -> None:
        observed["target"] = target
        observed.update(kwargs)

    monkeypatch.setattr("gameforge.apps.api.__main__.uvicorn.run", fake_run)

    run_api(host="127.0.0.1", port=8123)

    assert observed == {
        "target": "gameforge.apps.api.local:create_local_app",
        "factory": True,
        "host": "127.0.0.1",
        "port": 8123,
    }
