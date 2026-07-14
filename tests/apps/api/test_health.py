from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import ApiDependencies
from gameforge.apps.api.health import (
    AuditVerificationCache,
    CostLedgerReadinessProbe,
    DatabaseReadinessProbe,
    LocalObjectStoreReadinessProbe,
    MigrationHeadReadinessProbe,
    ReadinessChecks,
    ReadinessService,
    RegistryReadinessProbe,
    SloRetentionReadinessProbe,
)
from gameforge.contracts.jobs import Problem
from gameforge.contracts.observability import SpanDataV1
from gameforge.platform.registry.model import PlatformReadinessReport
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.observability import AlwaysOffSampler, InMemoryExporter, Tracer
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.migrations_api import expected_heads, upgrade
from gameforge.runtime.persistence.models import BudgetRow


class _Ids:
    def new_trace_id(self) -> str:
        return "3" * 32

    def new_span_id(self) -> str:
        return "4" * 16


class _Probe:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.calls = 0
        self.failure = failure

    def __call__(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


class _FailIfCalledExporter:
    def __init__(self) -> None:
        self.calls = 0

    def export(self, spans: Sequence[SpanDataV1]) -> None:
        del spans
        self.calls += 1
        raise AssertionError("liveness must not export telemetry")


class _AuditVerifier:
    def __init__(self, *, verified: bool = True) -> None:
        self.calls: list[str] = []
        self.verified = verified

    def verify_chain(self, chain_id: str) -> bool:
        self.calls.append(chain_id)
        return self.verified


def _app(checks: ReadinessChecks):
    return create_app(
        ApiDependencies(
            tracer=Tracer(
                exporter=InMemoryExporter(capacity=1),
                id_generator=_Ids(),
                sampler=AlwaysOffSampler(),
            ),
            request_id_factory=lambda: "request:health:1",
            readiness=ReadinessService(checks),
        )
    )


def _probes() -> dict[str, _Probe]:
    return {
        "migration_head": _Probe(),
        "database": _Probe(),
        "object_store": _Probe(),
        "cost_ledger": _Probe(),
        "registry": _Probe(),
        "slo_retention": _Probe(),
        "audit_cache": _Probe(),
    }


def _checks(probes: dict[str, Callable[[], None]]) -> ReadinessChecks:
    return ReadinessChecks(**probes)


def test_liveness_touches_no_dependency() -> None:
    probes = {
        name: _Probe(failure=AssertionError(f"{name} must not be called")) for name in _probes()
    }
    exporter = _FailIfCalledExporter()
    request_ids = _Probe(failure=AssertionError("liveness must not allocate request context"))
    app = create_app(
        ApiDependencies(
            tracer=Tracer(exporter=exporter, id_generator=_Ids()),
            request_id_factory=request_ids,
            readiness=ReadinessService(_checks(probes)),
        )
    )

    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.get("/livez")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert all(probe.calls == 0 for probe in probes.values())
    assert request_ids.calls == 0
    assert exporter.calls == 0


def test_readiness_checks_every_required_component_once() -> None:
    probes = _probes()

    with TestClient(_app(_checks(probes)), base_url="https://gameforge.test") as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": [
            "migration_head",
            "database",
            "object_store",
            "cost_ledger",
            "registry",
            "slo_retention",
            "audit_cache",
        ],
    }
    assert all(probe.calls == 1 for probe in probes.values())


@pytest.mark.parametrize("failed_component", tuple(_probes()))
def test_readiness_fails_closed_and_redacts_probe_details(failed_component: str) -> None:
    probes = _probes()
    probes[failed_component].failure = RuntimeError("private infrastructure location")

    with TestClient(_app(_checks(probes)), base_url="https://gameforge.test") as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    problem = Problem.model_validate(response.json())
    assert problem.code == "dependency_unavailable"
    assert problem.errors == ({"component": failed_component},)
    assert "private infrastructure location" not in response.text


def test_readyz_reads_cached_audit_state_without_rescanning_chain() -> None:
    verifier = _AuditVerifier()
    cache = AuditVerificationCache()
    cache.refresh(chain_ids=("platform-authority", "run-authority"), verifier=verifier)
    probes = _probes()
    probes["audit_cache"] = cache.check_ready
    app = _app(_checks(probes))

    with TestClient(app, base_url="https://gameforge.test") as client:
        first = client.get("/readyz")
        second = client.get("/readyz")

    assert first.status_code == second.status_code == 200
    assert verifier.calls == ["platform-authority", "run-authority"]
    assert probes["audit_cache"] == cache.check_ready


def test_unknown_or_failed_cached_audit_state_is_not_ready() -> None:
    unknown = AuditVerificationCache()
    with pytest.raises(RuntimeError, match="audit verification has not completed"):
        unknown.check_ready()

    failed = AuditVerificationCache()
    failed.refresh(
        chain_ids=("platform-authority",),
        verifier=_AuditVerifier(verified=False),
    )
    with pytest.raises(RuntimeError, match="audit verification failed"):
        failed.check_ready()


def test_database_migration_and_cost_probes_are_read_only(tmp_path: Path) -> None:
    database_path = tmp_path / "readiness.db"
    url = f"sqlite:///{database_path}"
    upgrade(url)
    engine = get_engine(url)
    try:
        DatabaseReadinessProbe(engine)()
        MigrationHeadReadinessProbe(engine, expected_heads=expected_heads(url))()
        CostLedgerReadinessProbe(engine)()
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(BudgetRow)) == 0
    finally:
        engine.dispose()


def test_migration_probe_rejects_a_database_without_the_expected_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    engine = get_engine(url)
    try:
        with pytest.raises(RuntimeError, match="migration head"):
            MigrationHeadReadinessProbe(engine, expected_heads=expected_heads(url))()
    finally:
        engine.dispose()


def test_local_object_store_probe_does_not_scan_or_write_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalObjectStore(
        tmp_path / "objects",
        store_id="local:readiness",
        clock=FrozenUtcClock(datetime(2026, 7, 14, tzinfo=timezone.utc)),
        cursor_signing_key=b"readiness-cursor-key-that-is-long-enough",
    )
    monkeypatch.setattr(
        store,
        "list_versions",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not scan")),
    )
    monkeypatch.setattr(
        store,
        "put_verified",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not write")),
    )

    LocalObjectStoreReadinessProbe(store)()


def test_registry_and_slo_probes_delegate_to_the_authoritative_services() -> None:
    class Registry:
        calls = 0

        def validate(self) -> PlatformReadinessReport:
            self.calls += 1
            return PlatformReadinessReport(
                ready=True,
                active_run_kinds=(),
                checked_run_kind_count=14,
                deferred_executor_keys=(),
                reference_checks=1,
                component_key_counts=(),
            )

    class Slo:
        calls: list[int] = []

        def reconcile_retention(self, *, max_definitions: int) -> int:
            self.calls.append(max_definitions)
            return 0

    registry = Registry()
    slo = Slo()

    RegistryReadinessProbe(registry)()
    SloRetentionReadinessProbe(slo, max_definitions=321)()

    assert registry.calls == 1
    assert slo.calls == [321]
