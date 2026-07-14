"""Bounded liveness and readiness composition for the API process."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from alembic.runtime.migration import MigrationContext
from fastapi import APIRouter
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from gameforge.contracts.errors import DependencyUnavailable
from gameforge.runtime.cost.ledger import SqlCostLedger


ReadinessProbe = Callable[[], object]
_CHECK_ORDER = (
    "migration_head",
    "database",
    "object_store",
    "cost_ledger",
    "registry",
    "slo_retention",
    "audit_cache",
)


class ReadinessPort(Protocol):
    def check(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class ReadinessChecks:
    migration_head: ReadinessProbe
    database: ReadinessProbe
    object_store: ReadinessProbe
    cost_ledger: ReadinessProbe
    registry: ReadinessProbe
    slo_retention: ReadinessProbe
    audit_cache: ReadinessProbe

    def __post_init__(self) -> None:
        for name in _CHECK_ORDER:
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} readiness probe must be callable")


class ReadinessService:
    def __init__(self, checks: ReadinessChecks) -> None:
        self._checks = checks

    def check(self) -> tuple[str, ...]:
        completed: list[str] = []
        for name in _CHECK_ORDER:
            probe = getattr(self._checks, name)
            try:
                result = probe()
                if result is False:
                    raise RuntimeError("readiness probe returned false")
            except Exception as exc:
                raise DependencyUnavailable(
                    "required readiness component is unavailable",
                    component=name,
                ) from exc
            completed.append(name)
        return tuple(completed)


class AuditChainVerifier(Protocol):
    def verify_chain(self, chain_id: str) -> bool: ...


class AuditVerificationCache:
    """Process-local result of startup/periodic full-chain verification."""

    def __init__(self) -> None:
        self._state = "unknown"
        self._lock = Lock()

    def refresh(
        self,
        *,
        chain_ids: Sequence[str],
        verifier: AuditChainVerifier,
    ) -> None:
        selected = tuple(sorted(set(chain_ids)))
        if not selected or any(not chain_id or len(chain_id) > 512 for chain_id in selected):
            raise ValueError("audit verification chain_ids must be non-empty and bounded")
        verified = True
        try:
            for chain_id in selected:
                if verifier.verify_chain(chain_id) is not True:
                    verified = False
                    break
        except Exception:
            verified = False
        with self._lock:
            self._state = "verified" if verified else "failed"

    def check_ready(self) -> None:
        with self._lock:
            state = self._state
        if state == "unknown":
            raise RuntimeError("audit verification has not completed")
        if state != "verified":
            raise RuntimeError("audit verification failed")


class DatabaseReadinessProbe:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self) -> None:
        with self._engine.connect() as connection:
            if connection.scalar(text("SELECT 1")) != 1:
                raise RuntimeError("database readiness query returned an invalid result")


class MigrationHeadReadinessProbe:
    def __init__(self, engine: Engine, *, expected_heads: Sequence[str]) -> None:
        selected = tuple(sorted(set(expected_heads)))
        if not selected or any(not head or len(head) > 512 for head in selected):
            raise ValueError("expected migration heads must be non-empty and bounded")
        self._engine = engine
        self._expected_heads = selected

    def __call__(self) -> None:
        with self._engine.connect() as connection:
            current = tuple(sorted(MigrationContext.configure(connection).get_current_heads()))
        if current != self._expected_heads:
            raise RuntimeError("database migration head does not match the application")


class LocalObjectStoreHealth(Protocol):
    def check_ready(self) -> None: ...


class LocalObjectStoreReadinessProbe:
    def __init__(self, store: LocalObjectStoreHealth) -> None:
        self._store = store

    def __call__(self) -> None:
        self._store.check_ready()


class CostLedgerReadinessProbe:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self) -> None:
        with Session(self._engine) as session:
            SqlCostLedger(session).get_budget("__gameforge_readiness__")


class RegistryValidator(Protocol):
    def validate(self) -> object: ...


class RegistryReadinessProbe:
    def __init__(self, validator: RegistryValidator) -> None:
        self._validator = validator

    def __call__(self) -> None:
        report = self._validator.validate()
        if getattr(report, "ready", None) is not True:
            raise RuntimeError("platform registry closure is not ready")


class SloRetentionService(Protocol):
    def reconcile_retention(self, *, max_definitions: int) -> int: ...


class SloRetentionReadinessProbe:
    def __init__(self, service: SloRetentionService, *, max_definitions: int = 10_000) -> None:
        if isinstance(max_definitions, bool) or max_definitions < 1:
            raise ValueError("SLO readiness limit must be positive")
        self._service = service
        self._max_definitions = max_definitions

    def __call__(self) -> None:
        reconciled = self._service.reconcile_retention(max_definitions=self._max_definitions)
        if isinstance(reconciled, bool) or not isinstance(reconciled, int) or reconciled < 0:
            raise RuntimeError("SLO retention reconciliation returned an invalid count")


def health_router(readiness: ReadinessPort | None) -> APIRouter:
    router = APIRouter()

    @router.get("/livez", include_in_schema=False)
    def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @router.get("/readyz", include_in_schema=False)
    def readiness_endpoint() -> dict[str, object]:
        if readiness is None:
            raise DependencyUnavailable(
                "API readiness is not configured",
                component="composition",
            )
        completed = readiness.check()
        return {"status": "ready", "checks": list(completed)}

    return router


__all__ = [
    "AuditChainVerifier",
    "AuditVerificationCache",
    "CostLedgerReadinessProbe",
    "DatabaseReadinessProbe",
    "LocalObjectStoreReadinessProbe",
    "MigrationHeadReadinessProbe",
    "ReadinessChecks",
    "ReadinessProbe",
    "ReadinessService",
    "RegistryReadinessProbe",
    "SloRetentionReadinessProbe",
    "health_router",
]
