from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

import gameforge.apps.worker.__main__ as worker_main
import gameforge.apps.worker.app as worker_app
from gameforge.apps.worker.__main__ import main
from gameforge.apps.worker.app import (
    LocalWorkerConfig,
    WorkerConfigurationError,
    WorkerRuntime,
    build_executor_resolver,
    build_reaper_scan,
    build_worker_runtime,
    validate_worker_readiness,
)
import gameforge.apps.worker.dispatch as worker_dispatch
from gameforge.apps.worker.artifact_replay_bridge import ArtifactReplayModelBridge
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.apps.worker.replay import LegacyArtifactReplaySource
from gameforge.apps.worker.dispatcher import RunDispatcher
from gameforge.apps.worker.executor import ExecutorContext
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    PreparedRunFailure,
    RunAttempt,
    RunKindRef,
    RunLease,
    canonical_payload_hash,
    outcome_policy_set_digest,
    run_kind_definition_digest,
)
from gameforge.platform.registry import TrustedComponentMaps
from gameforge.platform.run_handlers.deferred import DEFERRED_EXECUTORS
from gameforge.runtime.persistence import migrations_api
from tests.platform.m4c.test_replay_admission import _legacy_verified_fixture
from tests.platform.m4c.test_terminal_publisher import _registry_and_definition, _run_record


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


def test_build_process_closes_composed_resources_when_readiness_fails(monkeypatch) -> None:
    class Process:
        runtime = object()

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    process = Process()
    monkeypatch.setattr(
        worker_main.LocalWorkerConfig,
        "from_environment",
        staticmethod(lambda: object()),
    )
    monkeypatch.setattr(worker_main, "build_worker_process", lambda config: process)

    def reject(runtime) -> None:
        assert runtime is process.runtime
        raise WorkerConfigurationError("readiness rejected")

    monkeypatch.setattr(worker_main, "validate_worker_readiness", reject)
    with pytest.raises(WorkerConfigurationError, match="readiness rejected"):
        worker_main.build_process()
    assert process.closed is True


def test_build_process_preserves_readiness_failure_when_cleanup_also_fails(monkeypatch) -> None:
    class Process:
        runtime = object()

        def close(self) -> None:
            raise RuntimeError("cleanup secret must not replace readiness")

    process = Process()
    monkeypatch.setattr(
        worker_main.LocalWorkerConfig,
        "from_environment",
        staticmethod(lambda: object()),
    )
    monkeypatch.setattr(worker_main, "build_worker_process", lambda config: process)

    def reject(runtime) -> None:
        assert runtime is process.runtime
        raise WorkerConfigurationError("readiness rejected")

    monkeypatch.setattr(worker_main, "validate_worker_readiness", reject)
    with pytest.raises(WorkerConfigurationError, match="readiness rejected") as captured:
        worker_main.build_process()
    assert any("cleanup" in note for note in captured.value.__notes__)


def test_main_preserves_worker_failure_when_shutdown_cleanup_also_fails(monkeypatch) -> None:
    class Process:
        def close(self) -> None:
            raise RuntimeError("cleanup secret")

    process = Process()
    monkeypatch.setattr(worker_main, "build_process", lambda: process)

    def reject_drive(coroutine) -> None:
        coroutine.close()
        raise IntegrityViolation("primary worker failure")

    monkeypatch.setattr(worker_main.asyncio, "run", reject_drive)

    with pytest.raises(IntegrityViolation, match="primary worker failure") as captured:
        worker_main.main()

    assert any("RuntimeError" in note for note in captured.value.__notes__)
    assert all("cleanup secret" not in note for note in captured.value.__notes__)


def test_build_worker_process_requires_exact_model_authorities_for_readiness(
    tmp_path: Path,
) -> None:
    # The full trusted composition genuinely closes platform readiness (all 14 active
    # RunKinds across the six component maps) and yields a real fenced dispatch loop.
    config = _config(tmp_path)
    migrations_api.upgrade(config.database_url, "head")
    process = build_worker_process(config)
    try:
        with pytest.raises(WorkerConfigurationError, match="model execution authority"):
            validate_worker_readiness(process.runtime)
        assert isinstance(process.dispatcher, RunDispatcher)
        assert len(process.components.executors) == 14
        assert "checker_runner@1" in process.components.executors
    finally:
        process.close()


def test_partial_model_authority_closure_keeps_worker_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gameforge.apps.worker.model_authority import (
        StaticCircuitBreakerAuthority,
        StaticStructuredModelSnapshotAuthority,
        StructuredModelSnapshotManifestV1,
        WorkerModelExecutionAuthorities,
    )
    from gameforge.contracts.model_router import ModelSnapshot
    from gameforge.contracts.reliability import CircuitBreakerConfigV1
    from gameforge.contracts.routing import canonical_model_snapshot_id
    from gameforge.runtime.clock import SystemUtcClock
    from gameforge.runtime.cost.price_book import UnavailablePriceBook
    from gameforge.runtime.persistence.cost import SqlCostRepository
    from sqlalchemy.orm import Session
    from gameforge.runtime.reliability.breaker import CircuitBreaker
    from tests.apps.worker.test_cost_bridge import _catalog_policy
    from tests.apps.worker.test_model_authority import _manifest
    from tests.apps.worker.test_prompt_rendering import _authority, _request

    class UnavailableTransport:
        def complete(self, request):
            del request
            raise RuntimeError("provider unavailable")

    snapshot = ModelSnapshot(provider="openai", model="gpt-test", snapshot_tag="ready@1")
    model_snapshot_id = canonical_model_snapshot_id(snapshot)
    breaker = CircuitBreaker(
        dependency_id=f"model-provider:{model_snapshot_id}",
        config=CircuitBreakerConfigV1(
            config_version="breaker@1",
            rolling_window_s=60,
            minimum_samples=2,
            failure_threshold=1,
            open_cooldown_s=10,
            half_open_max_concurrent_probes=1,
            half_open_success_threshold=1,
        ),
        clock=SystemUtcClock(),
    )
    authorities = WorkerModelExecutionAuthorities(
        transport=UnavailableTransport(),  # type: ignore[arg-type]
        snapshots=StaticStructuredModelSnapshotAuthority(
            StructuredModelSnapshotManifestV1.model_validate(_manifest(snapshot))
        ),
        prompt_renderer=_authority(),
        price_book=UnavailablePriceBook(),
        legacy_imports=_legacy_verified_fixture().authority,
        circuit_breaker_resolver=StaticCircuitBreakerAuthority({model_snapshot_id: breaker}),
    )
    config = _config(tmp_path)
    migrations_api.upgrade(config.database_url, "head")
    process = build_worker_process(config, model_execution_authorities=authorities)
    try:
        with pytest.raises(WorkerConfigurationError, match="misses frozen Agent graph"):
            validate_worker_readiness(process.runtime)
        required_prompt_keys = tuple(
            sorted(
                (node.agent_node_id, node.prompt_version, node.tool_version)
                for graph in process.runtime.registry.list_agent_execution_graphs()
                if graph.status in {"active", "replay_only"}
                for node in graph.nodes
            )
        )
        monkeypatch.setattr(
            type(authorities.prompt_renderer),
            "binding_plan_keys",
            property(lambda _: required_prompt_keys),
        )
        unrelated_catalog, _ = _catalog_policy(_request())
        with Session(process.runtime.engine) as session, session.begin():
            SqlCostRepository(session).put_model_catalog(unrelated_catalog)
        with pytest.raises(WorkerConfigurationError, match="misses retained catalog snapshots"):
            validate_worker_readiness(process.runtime)
        assert isinstance(process.dispatcher, RunDispatcher)
    finally:
        process.close()


def test_legacy_replay_factory_does_not_require_or_fabricate_native_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verified legacy replay stays on its retained import-decision branch."""

    config = _config(tmp_path)
    migrations_api.upgrade(config.database_url, "head")
    fixture = _legacy_verified_fixture()
    process = build_worker_process(config, legacy_import_authority=fixture.authority)
    try:
        review_kind = RunKindRef(kind="review.run", version=1)
        definition = process.runtime.registry.get_run_kind(review_kind)
        assert definition is not None
        _, checker_definition = _registry_and_definition()
        base = _run_record(checker_definition)
        payload = fixture.replay_payload
        retry = process.runtime.registry.get_retry_policy(definition.retry_policy)
        assert retry is not None
        run = base.model_copy(
            update={
                "run_id": "run:legacy-replay:production",
                "kind": review_kind,
                "payload": payload,
                "payload_hash": canonical_payload_hash(payload),
                "run_kind_definition_digest": run_kind_definition_digest(definition),
                "outcome_policy_set_digest": outcome_policy_set_digest(
                    review_kind, definition.outcome_policies
                ),
                "failure_classifier": definition.failure_classifier,
                "retry_policy": definition.retry_policy,
                "max_attempts": retry.max_attempts,
                "budget_set_snapshot_id": payload.budget_set_snapshot_id,
            }
        )
        attempt = RunAttempt(
            run_id=run.run_id,
            attempt_no=1,
            status="running",
            fencing_token=1,
            worker_principal_id=config.worker_principal_id,
            next_call_ordinal=1,
            started_at="2026-07-16T00:00:00Z",
            attempt_deadline_utc="2099-07-16T00:30:00Z",
        )
        lease = RunLease(
            lease_id="lease:legacy-replay",
            run_id=run.run_id,
            attempt_no=1,
            fencing_token=1,
            lease_version=1,
            owner_principal_id=config.worker_principal_id,
            acquired_at="2026-07-16T00:00:00Z",
            heartbeat_at="2026-07-16T00:00:00Z",
            expires_at="2099-07-16T00:30:00Z",
            status="active",
        )
        legacy_source = object.__new__(LegacyArtifactReplaySource)

        class Loader:
            def __init__(self, *args, **kwargs) -> None:
                del args, kwargs

            def load(self, selected_run):
                assert selected_run == run
                return legacy_source

        monkeypatch.setattr(worker_dispatch, "ArtifactReplayLoader", Loader)
        with process.runtime.engine.connect() as connection:
            before = connection.execute(text("SELECT count(*) FROM routing_policies")).scalar_one()
        factory = process.dispatcher._runner._model_bridge_factory
        bridge = factory(run=run, attempt=attempt, lease=lease)
        with process.runtime.engine.connect() as connection:
            after = connection.execute(text("SELECT count(*) FROM routing_policies")).scalar_one()

        assert isinstance(bridge, ArtifactReplayModelBridge)
        assert before == after == 0
    finally:
        process.close()


def test_production_provider_classifier_treats_http_429_as_quota_not_breaker() -> None:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://model.invalid/v1/responses"),
    )
    error = httpx.HTTPStatusError("quota", request=response.request, response=response)

    classified = worker_dispatch._ProviderFailureClassifier(
        version="provider-failures@1",
        honor_retry_after=True,
        clock=SimpleNamespace(now_utc=lambda: datetime(2026, 7, 16, tzinfo=UTC)),
    ).classify(error)

    assert classified.failure_kind == "quota"
    assert classified.retryable is False
    assert classified.counts_for_breaker is False


@pytest.mark.parametrize("status", [408, 503])
def test_production_provider_classifier_bounds_retry_after_and_keeps_408_transient(
    status: int,
) -> None:
    response = httpx.Response(
        status,
        headers={"retry-after": "9" * 10_000},
        request=httpx.Request("POST", "https://model.invalid/v1/responses"),
    )
    error = httpx.HTTPStatusError("transient", request=response.request, response=response)

    classified = worker_dispatch._ProviderFailureClassifier(
        version="provider-failures@1",
        honor_retry_after=True,
        clock=SimpleNamespace(now_utc=lambda: datetime(2026, 7, 16, tzinfo=UTC)),
    ).classify(error)

    assert classified.failure_kind == "transient_infrastructure"
    assert classified.retryable is True
    assert classified.retry_after_s == worker_dispatch._MAX_RETRY_AFTER_S


def test_production_provider_classifier_supports_http_date_retry_after() -> None:
    response = httpx.Response(
        503,
        headers={"retry-after": "Thu, 16 Jul 2026 00:00:05 GMT"},
        request=httpx.Request("POST", "https://model.invalid/v1/responses"),
    )
    error = httpx.HTTPStatusError("transient", request=response.request, response=response)

    classified = worker_dispatch._ProviderFailureClassifier(
        version="provider-failures@1",
        honor_retry_after=True,
        clock=SimpleNamespace(now_utc=lambda: datetime(2026, 7, 16, tzinfo=UTC)),
    ).classify(error)

    assert classified.retry_after_s == 5


def test_worker_readiness_rejects_an_unmigrated_database(tmp_path: Path) -> None:
    runtime = build_worker_runtime(_config(tmp_path))
    try:
        with pytest.raises(WorkerConfigurationError, match="migration head"):
            validate_worker_readiness(runtime)
    finally:
        runtime.close()


def test_worker_readiness_rejects_missing_model_route_authority_table(tmp_path: Path) -> None:
    config = _config(tmp_path)
    migrations_api.upgrade(config.database_url, "head")
    runtime = build_worker_runtime(config)
    try:
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE run_model_response_consumptions")
        with pytest.raises(WorkerConfigurationError, match="run_model_response_consumptions"):
            validate_worker_readiness(runtime)
    finally:
        runtime.close()


def test_from_environment_requires_worker_and_secret() -> None:
    with pytest.raises(WorkerConfigurationError):
        LocalWorkerConfig.from_environment({})


def test_worker_rejects_telemetry_and_business_sqlite_same_physical_file(
    tmp_path: Path,
) -> None:
    business = tmp_path / "business.sqlite3"
    business.touch()
    telemetry_alias = tmp_path / "telemetry-alias.sqlite3"
    telemetry_alias.symlink_to(business)

    with pytest.raises(WorkerConfigurationError, match="physically separate"):
        LocalWorkerConfig(
            database_url=f"sqlite:///{business}",
            object_store_root=tmp_path / "objects",
            object_store_id="local:default",
            telemetry_db_path=telemetry_alias,
            worker_principal_id="service:worker:1",
            reaper_principal_id="system:lease-reaper",
            root_secret=b"0" * 32,
        )


def test_worker_treats_file_prefix_as_literal_without_sqlite_uri_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(WorkerConfigurationError, match="physically separate"):
        LocalWorkerConfig(
            database_url="sqlite:///file:shared.sqlite3",
            object_store_root=tmp_path / "objects",
            object_store_id="local:default",
            telemetry_db_path=Path("file:shared.sqlite3"),
            worker_principal_id="service:worker:1",
            reaper_principal_id="system:lease-reaper",
            root_secret=b"0" * 32,
        )


def test_worker_matches_sqlalchemy_truthy_sqlite_uri_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(WorkerConfigurationError, match="physically separate"):
        LocalWorkerConfig(
            database_url="sqlite:///file:shared.sqlite3?uri=1",
            object_store_root=tmp_path / "objects",
            object_store_id="local:default",
            telemetry_db_path=Path("shared.sqlite3"),
            worker_principal_id="service:worker:1",
            reaper_principal_id="system:lease-reaper",
            root_secret=b"0" * 32,
        )


def test_worker_runtime_partial_build_closes_every_created_resource(
    tmp_path: Path,
    monkeypatch,
) -> None:
    closed: list[str] = []

    class Engine:
        dialect = SimpleNamespace(name="sqlite")

        def dispose(self) -> None:
            closed.append("engine")

    class Telemetry:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def close(self) -> None:
            closed.append("telemetry")

    class ExecutorPool:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def close(self) -> None:
            closed.append("executor")

    def reject_control_pool(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("control pool construction failed")

    monkeypatch.setattr(worker_app, "LocalTelemetryStore", Telemetry)
    monkeypatch.setattr(worker_app, "ThreadedBlockingExecutorPool", ExecutorPool)
    monkeypatch.setattr(worker_app, "ControlPlanePool", reject_control_pool)

    with pytest.raises(RuntimeError, match="control pool construction failed"):
        build_worker_runtime(
            _config(tmp_path),
            engine=Engine(),  # type: ignore[arg-type]
            object_store=object(),  # type: ignore[arg-type]
        )

    assert closed == ["executor", "telemetry", "engine"]


def test_worker_runtime_disposes_injected_engine_when_preflight_rejects_components(
    tmp_path: Path,
) -> None:
    class Engine:
        def __init__(self) -> None:
            self.dispose_count = 0

        def dispose(self) -> None:
            self.dispose_count += 1

    engine = Engine()

    with pytest.raises(WorkerConfigurationError, match="trusted_components"):
        build_worker_runtime(
            _config(tmp_path),
            trusted_components=object(),  # type: ignore[arg-type]
            engine=engine,  # type: ignore[arg-type]
        )

    assert engine.dispose_count == 1


def test_worker_runtime_close_attempts_all_resources_after_one_close_fails(
    tmp_path: Path,
) -> None:
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        def close(self) -> None:
            closed.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} failed")

    class Engine:
        def dispose(self) -> None:
            closed.append("engine")

    runtime = WorkerRuntime(
        config=_config(tmp_path),
        engine=Engine(),  # type: ignore[arg-type]
        object_store=object(),  # type: ignore[arg-type]
        telemetry_store=Resource("telemetry"),  # type: ignore[arg-type]
        tracer=object(),  # type: ignore[arg-type]
        executor_pool=Resource("executor", fail=True),  # type: ignore[arg-type]
        control_pool=Resource("control"),  # type: ignore[arg-type]
        heartbeat_pool=Resource("heartbeat"),  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        components=TrustedComponentMaps(),
        worker_actor=object(),  # type: ignore[arg-type]
        reaper_actor=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="executor failed"):
        runtime.close()

    assert closed == ["executor", "heartbeat", "control", "telemetry", "engine"]


def test_worker_process_partial_build_closes_runtime(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    closed: list[WorkerRuntime] = []
    original_close = WorkerRuntime.close

    def tracking_close(runtime: WorkerRuntime) -> None:
        closed.append(runtime)
        original_close(runtime)

    def reject_dispatch(**kwargs):
        del kwargs
        raise RuntimeError("dispatch construction failed")

    monkeypatch.setattr(WorkerRuntime, "close", tracking_close)
    monkeypatch.setattr(worker_dispatch, "build_worker_dispatch", reject_dispatch)

    with pytest.raises(RuntimeError, match="dispatch construction failed"):
        build_worker_process(config)

    assert len(closed) == 1


def test_worker_process_disposes_engine_when_runtime_preflight_rejects_components(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)

    class Engine:
        dialect = SimpleNamespace(name="sqlite")

        def __init__(self) -> None:
            self.dispose_count = 0

        def dispose(self) -> None:
            self.dispose_count += 1

    engine = Engine()
    monkeypatch.setattr(worker_dispatch, "get_engine", lambda url: engine)
    monkeypatch.setattr(worker_dispatch, "build_trusted_components", lambda **kwargs: object())

    with pytest.raises(WorkerConfigurationError, match="trusted_components"):
        build_worker_process(config)

    assert engine.dispose_count >= 1


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


@pytest.mark.parametrize("heartbeat_interval_s", [5.0, 8.0])
def test_heartbeat_interval_at_or_above_half_the_lease_is_rejected(
    tmp_path: Path,
    heartbeat_interval_s: float,
) -> None:
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
            heartbeat_interval_s=heartbeat_interval_s,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lease_duration_ns", 0),
        ("heartbeat_interval_s", float("nan")),
        ("poll_interval_s", float("inf")),
        ("reaper_limit", 1025),
        ("max_workers", 0),
        ("max_workers", 1025),
        ("max_concurrency", 0),
        ("max_concurrency", 1025),
        ("lease_duration_ns", 86_400_000_000_001),
        ("heartbeat_interval_s", 3_600.1),
        ("poll_interval_s", 3_600.1),
    ],
)
def test_direct_worker_configuration_rejects_unsafe_numeric_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    fields = {
        "database_url": f"sqlite:///{tmp_path / 'w.db'}",
        "object_store_root": tmp_path / "objects",
        "object_store_id": "local:default",
        "telemetry_db_path": tmp_path / "telemetry.sqlite3",
        "worker_principal_id": "service:worker:1",
        "reaper_principal_id": "system:reaper",
        "root_secret": b"0" * 32,
    }
    fields[field] = value
    with pytest.raises(WorkerConfigurationError):
        LocalWorkerConfig(**fields)


def test_poll_interval_must_leave_two_scans_per_lease(tmp_path: Path) -> None:
    with pytest.raises(WorkerConfigurationError, match="poll_interval"):
        LocalWorkerConfig(
            database_url=f"sqlite:///{tmp_path / 'w.db'}",
            object_store_root=tmp_path / "objects",
            object_store_id="local:default",
            telemetry_db_path=tmp_path / "telemetry.sqlite3",
            worker_principal_id="service:worker:1",
            reaper_principal_id="system:reaper",
            root_secret=b"0" * 32,
            lease_duration_ns=10_000_000_000,
            heartbeat_interval_s=1.0,
            poll_interval_s=5.0,
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
