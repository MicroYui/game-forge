from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

import gameforge.apps.worker.app as worker_app
from gameforge.apps.worker.app import WorkerConfigurationError
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    compute_model_catalog_digest,
)
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.cost import SqlCostRepository
from gameforge.runtime.persistence.engine import get_engine


MODEL_ACTIVE = "openai:model-active@1"
MODEL_DISABLED = "openai:model-disabled@1"
MODEL_LATE = "openai:model-late@1"


def _descriptor(model_snapshot: str, *, status: str) -> ModelDescriptorV1:
    return ModelDescriptorV1(
        provider="openai",
        model_snapshot=model_snapshot,
        tier="test",
        capabilities=("text",),
        context_limit=4096,
        max_output_tokens=1024,
        prompt_cache_support=False,
        status=status,
    )


def _catalog(
    version: int,
    *descriptors: ModelDescriptorV1,
) -> ModelCatalogSnapshotV1:
    payload = {
        "catalog_schema_version": "model-catalog@1",
        "catalog_version": version,
        "models": descriptors,
        "created_at": datetime(2026, 7, 18, tzinfo=UTC),
    }
    return ModelCatalogSnapshotV1(
        **payload,
        catalog_digest=compute_model_catalog_digest(payload),
    )


@pytest.fixture
def engine(tmp_path) -> Engine:
    database_url = f"sqlite:///{tmp_path / 'worker-model-readiness.sqlite3'}"
    migrations_api.upgrade(database_url, "head")
    retained = get_engine(database_url)
    try:
        yield retained
    finally:
        retained.dispose()


def _persist(engine: Engine, *catalogs: ModelCatalogSnapshotV1) -> None:
    with Session(engine) as session, session.begin():
        repository = SqlCostRepository(session)
        for catalog in catalogs:
            repository.put_model_catalog(catalog)


def _validate(
    engine: Engine,
    *,
    snapshot_ids: tuple[str, ...],
    breaker_ids: tuple[str, ...],
) -> None:
    worker_app._validate_worker_model_authority_closure(  # noqa: SLF001
        engine,
        snapshot_ids=snapshot_ids,
        breaker_ids=breaker_ids,
    )


def test_readiness_pages_every_retained_catalog_without_a_total_history_cap(
    engine: Engine,
) -> None:
    descriptor = _descriptor(MODEL_ACTIVE, status="active")
    _persist(engine, *(_catalog(version, descriptor) for version in range(1, 1026)))

    _validate(
        engine,
        snapshot_ids=(MODEL_ACTIVE,),
        breaker_ids=(MODEL_ACTIVE,),
    )


def test_readiness_checks_active_models_beyond_the_first_bounded_page(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_app, "_MODEL_CATALOG_READINESS_PAGE_SIZE", 1)
    _persist(
        engine,
        _catalog(1, _descriptor(MODEL_ACTIVE, status="active")),
        _catalog(2, _descriptor(MODEL_DISABLED, status="disabled")),
        _catalog(3, _descriptor(MODEL_LATE, status="active")),
    )

    with pytest.raises(WorkerConfigurationError, match=MODEL_LATE):
        _validate(
            engine,
            snapshot_ids=(MODEL_ACTIVE, MODEL_DISABLED, MODEL_LATE),
            breaker_ids=(MODEL_ACTIVE,),
        )


def test_disabled_only_history_needs_no_hot_snapshot_or_breaker_authority(
    engine: Engine,
) -> None:
    _persist(
        engine,
        _catalog(
            1,
            _descriptor(MODEL_ACTIVE, status="active"),
            _descriptor(MODEL_DISABLED, status="disabled"),
        ),
    )

    _validate(
        engine,
        snapshot_ids=(MODEL_ACTIVE,),
        breaker_ids=(MODEL_ACTIVE,),
    )


def test_every_breaker_dependency_has_a_structured_snapshot_preimage(
    engine: Engine,
) -> None:
    _persist(engine, _catalog(1, _descriptor(MODEL_ACTIVE, status="active")))

    with pytest.raises(WorkerConfigurationError, match="preimage"):
        _validate(
            engine,
            snapshot_ids=(MODEL_ACTIVE,),
            breaker_ids=(MODEL_ACTIVE, "openai:orphan-breaker@1"),
        )
