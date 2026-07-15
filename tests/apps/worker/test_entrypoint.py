from __future__ import annotations

import base64
from pathlib import Path

import pytest

from gameforge.apps.worker.__main__ import main
from gameforge.apps.worker.app import (
    LocalWorkerConfig,
    WorkerConfigurationError,
    build_reaper_scan,
    build_worker_runtime,
)


def _config(tmp_path: Path) -> LocalWorkerConfig:
    return LocalWorkerConfig(
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        object_store_root=tmp_path / "objects",
        object_store_id="local:default",
        telemetry_db_path=tmp_path / "telemetry.sqlite3",
        worker_principal_id="service:worker:1",
        reaper_principal_id="system:lease-reaper",
        root_secret=b"0" * 32,
    )


def test_entrypoint_requires_real_configuration_not_a_placeholder(monkeypatch) -> None:
    # The placeholder "not configured" RuntimeError is gone: main() now performs
    # real composition and fails closed on missing configuration instead.
    for name in (
        "GAMEFORGE_WORKER_PRINCIPAL_ID",
        "GAMEFORGE_WORKER_REAPER_PRINCIPAL_ID",
        "GAMEFORGE_LOCAL_SECRET_BASE64",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(WorkerConfigurationError):
        main()


def test_from_environment_requires_worker_and_secret() -> None:
    with pytest.raises(WorkerConfigurationError):
        LocalWorkerConfig.from_environment({})


def test_build_worker_runtime_composes_shared_infrastructure(tmp_path: Path) -> None:
    runtime = build_worker_runtime(_config(tmp_path))
    try:
        assert runtime.engine.dialect.name == "sqlite"
        assert runtime.worker_actor.principal_kind == "service"
        assert runtime.reaper_actor.principal_kind == "system"
        # The bounded expired-lease scan is composable over the shared engine.
        scan = build_reaper_scan(runtime.engine)
        assert callable(scan)
    finally:
        runtime.close()


def test_from_environment_reads_a_full_local_deployment(tmp_path: Path) -> None:
    env = {
        "GAMEFORGE_DATABASE_URL": f"sqlite:///{tmp_path / 'w.db'}",
        "GAMEFORGE_OBJECT_STORE_ROOT": str(tmp_path / "objects"),
        "GAMEFORGE_TELEMETRY_DB_PATH": str(tmp_path / "telemetry.sqlite3"),
        "GAMEFORGE_WORKER_PRINCIPAL_ID": "service:worker:7",
        "GAMEFORGE_WORKER_REAPER_PRINCIPAL_ID": "system:reaper",
        "GAMEFORGE_LOCAL_SECRET_BASE64": base64.b64encode(b"1" * 32).decode(),
    }
    config = LocalWorkerConfig.from_environment(env)
    assert config.worker_principal_id == "service:worker:7"
    assert config.reaper_principal_id == "system:reaper"
    assert config.max_workers == 4
