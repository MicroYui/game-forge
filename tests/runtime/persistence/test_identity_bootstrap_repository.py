from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import Base


T0 = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 14, 5, 0, tzinfo=timezone.utc)


@dataclass
class _MutableUtcClock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current


@pytest.fixture
def engine(tmp_path) -> Engine:
    database = get_engine(f"sqlite:///{tmp_path / 'identity-bootstrap.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def test_bootstrap_empty_store_guard_fails_after_first_principal(
    engine: Engine,
) -> None:
    clock = _MutableUtcClock(T0)
    with Session(engine) as session, session.begin():
        repository = SqlIdentityRepository(session, clock=clock)
        repository.require_empty_for_bootstrap()
        repository.create(
            principal_id="human:first-admin",
            kind="human",
            display_name="First Admin",
        )

        with pytest.raises(Conflict, match="identity store is not empty"):
            repository.require_empty_for_bootstrap()


def test_bump_credential_epoch_is_an_exact_active_principal_cas(
    engine: Engine,
) -> None:
    clock = _MutableUtcClock(T0)
    with Session(engine) as session, session.begin():
        repository = SqlIdentityRepository(session, clock=clock)
        created = repository.create(
            principal_id="human:alice",
            kind="human",
            display_name="Alice",
        )
        clock.current = T1

        updated = repository.bump_credential_epoch(
            created.principal_id,
            expected_revision=created.revision,
        )

    assert updated.revision == 2
    assert updated.credential_epoch == 1
    assert updated.authz_revision == 0
    assert updated.status == "active"
    assert updated.updated_at == "2026-07-14T05:00:00Z"


def test_bump_credential_epoch_rejects_stale_missing_and_disabled_principals(
    engine: Engine,
) -> None:
    clock = _MutableUtcClock(T0)
    with Session(engine) as session:
        repository = SqlIdentityRepository(session, clock=clock)
        created = repository.create(
            principal_id="human:alice",
            kind="human",
            display_name="Alice",
        )
        session.commit()

        with pytest.raises(Conflict, match="principal revision"):
            repository.bump_credential_epoch(
                created.principal_id,
                expected_revision=created.revision + 1,
            )
        session.rollback()
        assert repository.get(created.principal_id) == created

        with pytest.raises(Conflict, match="principal does not exist"):
            repository.bump_credential_epoch(
                "human:missing",
                expected_revision=1,
            )
        session.rollback()

        disabled = repository.disable(
            created.principal_id,
            disabled_reason="left_company",
            expected_revision=created.revision,
        )
        session.commit()

        with pytest.raises(Conflict, match="disabled"):
            repository.bump_credential_epoch(
                created.principal_id,
                expected_revision=disabled.revision,
            )
        session.rollback()
        assert repository.get(created.principal_id) == disabled
