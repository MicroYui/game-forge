from __future__ import annotations

import base64
from pathlib import Path

import pytest

from gameforge.apps.worker.__main__ import main
from gameforge.apps.worker.app import (
    LocalWorkerConfig,
    WorkerConfigurationError,
    build_executor_resolver,
    build_reaper_scan,
    build_worker_runtime,
    validate_worker_readiness,
)
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.apps.worker.dispatcher import RunDispatcher
from gameforge.apps.worker.executor import ExecutorContext
from gameforge.contracts.jobs import PreparedRunFailure, RunKindRef
from gameforge.platform.registry import TrustedComponentMaps
from gameforge.platform.run_handlers.deferred import DEFERRED_EXECUTORS


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


def test_build_worker_process_composes_a_ready_fenced_dispatch_loop(tmp_path: Path) -> None:
    # The full trusted composition genuinely closes platform readiness (all 14 active
    # RunKinds across the six component maps) and yields a real fenced dispatch loop.
    process = build_worker_process(_config(tmp_path))
    try:
        validate_worker_readiness(process.runtime)  # does not raise -> registry closes
        assert isinstance(process.dispatcher, RunDispatcher)
        assert len(process.components.executors) == 14
        assert "checker_runner@1" in process.components.executors
    finally:
        process.close()


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


def test_heartbeat_interval_at_or_above_half_the_lease_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(WorkerConfigurationError, match="heartbeat_interval"):
        LocalWorkerConfig(
            database_url=f"sqlite:///{tmp_path / 'w.db'}",
            object_store_root=tmp_path / "objects",
            object_store_id="local:default",
            telemetry_db_path=tmp_path / "telemetry.sqlite3",
            worker_principal_id="service:worker:1",
            reaper_principal_id="system:reaper",
            root_secret=b"0" * 32,
            lease_duration_ns=10_000_000_000,  # 10s lease
            heartbeat_interval_s=8.0,  # 8s interval self-expires the lease
        )


def test_runtime_composes_both_execution_lanes(tmp_path: Path) -> None:
    runtime = build_worker_runtime(_config(tmp_path))
    try:
        assert runtime.executor_pool is not runtime.control_pool
    finally:
        runtime.close()


def test_deferred_executor_is_dispatchable_through_the_generic_resolver(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from gameforge.contracts.jobs import FailureClassifierRefV1

    runtime = build_worker_runtime(
        _config(tmp_path),
        trusted_components=TrustedComponentMaps(executors=dict(DEFERRED_EXECUTORS)),
    )
    try:
        resolver = build_executor_resolver(runtime.registry, runtime.components)
        run = SimpleNamespace(
            run_id="run:1",
            kind=RunKindRef(kind="artifact.migrate", version=1),
            failure_classifier=FailureClassifierRefV1(
                classifier_version=1, classifier_digest="a" * 64
            ),
        )
        attempt = SimpleNamespace(attempt_no=1)
        executor = resolver(run)  # deferred executor, adapted to the generic shape
        context = ExecutorContext(
            run=run, attempt=attempt, payload=None, deadline_utc=None, model_bridge=None
        )
        outcome = executor(context)
        assert isinstance(outcome, PreparedRunFailure)
        assert outcome.run_id == "run:1"
        assert outcome.run_kind == RunKindRef(kind="artifact.migrate", version=1)
    finally:
        runtime.close()
