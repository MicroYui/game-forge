from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.lineage import AuditActor
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import Base, PrincipalRow, RoleAssignmentRow


T0 = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 14, 3, 0, tzinfo=timezone.utc)
ADMIN = AuditActor(principal_id="human:admin", principal_kind="human")


@dataclass
class _MutableUtcClock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


@pytest.fixture
def clock() -> _MutableUtcClock:
    return _MutableUtcClock(T0)


def _repository(session: Session, clock: _MutableUtcClock) -> SqlIdentityRepository:
    return SqlIdentityRepository(session, clock=clock)


def _create_alice(repository: SqlIdentityRepository):
    return repository.create(
        principal_id="human:alice",
        kind="human",
        display_name="Alice",
    )


def test_create_read_and_project_build_one_authoritative_identity(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session, clock)
        created = _create_alice(repository)

        assert repository.get(created.principal_id) == created
        projection = repository.project(created.principal_id)

    assert created.model_dump(mode="json") == {
        "principal_schema_version": "principal@1",
        "principal_id": "human:alice",
        "kind": "human",
        "display_name": "Alice",
        "status": "active",
        "credential_epoch": 0,
        "authz_revision": 0,
        "revision": 1,
        "created_at": "2026-07-14T01:00:00Z",
        "updated_at": "2026-07-14T01:00:00Z",
        "disabled_at": None,
        "disabled_reason": None,
    }
    assert projection is not None
    assert projection.model_dump(mode="json") == {
        "id": "human:alice",
        "kind": "human",
        "display_name": "Alice",
        "status": "active",
        "revision": 1,
        "credential_epoch": 0,
        "authz_revision": 0,
        "roles": [],
    }


def test_create_is_absent_cas_and_different_content_for_same_id_fails_closed(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        repository = _repository(session, clock)
        original = _create_alice(repository)
        session.commit()

        with pytest.raises(Conflict, match="already exists"):
            _create_alice(repository)
        session.rollback()

        with pytest.raises(IntegrityViolation, match="different identity content"):
            repository.create(
                principal_id=original.principal_id,
                kind="service",
                display_name="Other",
            )
        session.rollback()


def test_grant_increments_only_principal_revision_and_authz_revision(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    scope = DomainScope(domain_ids=("narrative", "quest"))
    with Session(engine) as session, session.begin():
        repository = _repository(session, clock)
        created = _create_alice(repository)
        clock.current = T1

        granted = repository.grant(
            assignment_id="assignment:alice:content",
            principal_id=created.principal_id,
            role="content_designer",
            scope=scope,
            granted_by=ADMIN,
            expected_principal_revision=created.revision,
        )
        current = repository.get(created.principal_id)
        projection = repository.project(created.principal_id)

    assert granted.revision == 1
    assert granted.status == "active"
    assert granted.scope == DomainScope(domain_ids=("narrative", "quest"))
    assert granted.granted_at == "2026-07-14T02:00:00Z"
    assert current is not None
    assert (current.revision, current.authz_revision, current.credential_epoch) == (2, 1, 0)
    assert current.updated_at == "2026-07-14T02:00:00Z"
    assert projection is not None
    assert projection.roles == (granted,)


def test_grant_rejects_stale_principal_revision_without_partial_write(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        repository = _repository(session, clock)
        created = _create_alice(repository)
        session.commit()

        with pytest.raises(Conflict, match="principal revision"):
            repository.grant(
                assignment_id="assignment:stale",
                principal_id=created.principal_id,
                role="qa",
                scope=None,
                granted_by=ADMIN,
                expected_principal_revision=created.revision + 1,
            )
        session.rollback()

        assert repository.get_assignment("assignment:stale") is None
        assert repository.get(created.principal_id) == created


def test_active_assignment_identity_is_unique_but_revoked_history_does_not_block_regrant(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    scope = DomainScope(domain_ids=("narrative",))
    with Session(engine) as session:
        repository = _repository(session, clock)
        created = _create_alice(repository)
        first = repository.grant(
            assignment_id="assignment:first",
            principal_id=created.principal_id,
            role="content_designer",
            scope=scope,
            granted_by=ADMIN,
            expected_principal_revision=created.revision,
        )
        session.commit()

        with pytest.raises(Conflict, match="active role assignment"):
            repository.grant(
                assignment_id="assignment:duplicate-active",
                principal_id=created.principal_id,
                role="content_designer",
                scope=scope,
                granted_by=ADMIN,
                expected_principal_revision=2,
            )
        session.rollback()

        clock.current = T1
        revoked = repository.revoke(
            assignment_id=first.assignment_id,
            revoked_by=ADMIN,
            revoke_reason="responsibility_changed",
            expected_principal_revision=2,
            expected_assignment_revision=first.revision,
        )
        current = repository.get(created.principal_id)
        assert current is not None
        second = repository.grant(
            assignment_id="assignment:second",
            principal_id=created.principal_id,
            role="content_designer",
            scope=scope,
            granted_by=ADMIN,
            expected_principal_revision=current.revision,
        )
        session.commit()

        assert repository.get_assignment(first.assignment_id) == revoked
        assert repository.get_assignment(second.assignment_id) == second
        projection = repository.project(created.principal_id)

    assert revoked.status == "revoked"
    assert revoked.revision == 2
    assert projection is not None
    assert projection.roles == (second,)


def test_same_assignment_id_with_different_content_fails_closed(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        repository = _repository(session, clock)
        created = _create_alice(repository)
        repository.grant(
            assignment_id="assignment:stable-id",
            principal_id=created.principal_id,
            role="qa",
            scope=None,
            granted_by=ADMIN,
            expected_principal_revision=created.revision,
        )
        session.commit()

        clock.current = T1
        with pytest.raises(Conflict, match="already exists"):
            repository.grant(
                assignment_id="assignment:stable-id",
                principal_id=created.principal_id,
                role="qa",
                scope=None,
                granted_by=ADMIN,
                expected_principal_revision=2,
            )
        session.rollback()

        with pytest.raises(IntegrityViolation, match="different assignment content"):
            repository.grant(
                assignment_id="assignment:stable-id",
                principal_id=created.principal_id,
                role="tooling",
                scope="all",
                granted_by=ADMIN,
                expected_principal_revision=2,
            )
        session.rollback()

    with Session(engine) as session:
        principal = _repository(session, clock).get(created.principal_id)
        assert principal is not None
        assert (principal.revision, principal.authz_revision) == (2, 1)


def test_revoke_uses_both_revisions_and_preserves_credential_epoch(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        repository = _repository(session, clock)
        created = _create_alice(repository)
        granted = repository.grant(
            assignment_id="assignment:qa",
            principal_id=created.principal_id,
            role="qa",
            scope=None,
            granted_by=ADMIN,
            expected_principal_revision=created.revision,
        )
        session.commit()

        with pytest.raises(Conflict, match="assignment revision"):
            repository.revoke(
                assignment_id=granted.assignment_id,
                revoked_by=ADMIN,
                revoke_reason="stale",
                expected_principal_revision=2,
                expected_assignment_revision=2,
            )
        session.rollback()

        with pytest.raises(Conflict, match="principal revision"):
            repository.revoke(
                assignment_id=granted.assignment_id,
                revoked_by=ADMIN,
                revoke_reason="stale",
                expected_principal_revision=1,
                expected_assignment_revision=1,
            )
        session.rollback()

        clock.current = T1
        revoked = repository.revoke(
            assignment_id=granted.assignment_id,
            revoked_by=ADMIN,
            revoke_reason="rotation",
            expected_principal_revision=2,
            expected_assignment_revision=1,
        )
        principal = repository.get(created.principal_id)
        session.commit()

    assert revoked.status == "revoked"
    assert revoked.revision == 2
    assert revoked.revoked_at == "2026-07-14T02:00:00Z"
    assert principal is not None
    assert (principal.revision, principal.authz_revision, principal.credential_epoch) == (3, 2, 0)


def test_disable_increments_all_three_principal_revisions_with_injected_utc(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with Session(engine) as session:
        repository = _repository(session, clock)
        created = _create_alice(repository)
        clock.current = T2
        disabled = repository.disable(
            created.principal_id,
            disabled_reason="offboarded",
            expected_revision=created.revision,
        )
        session.commit()

        with pytest.raises(Conflict, match="principal revision"):
            repository.disable(
                created.principal_id,
                disabled_reason="duplicate",
                expected_revision=created.revision,
            )
        session.rollback()

    assert disabled.status == "disabled"
    assert disabled.disabled_at == "2026-07-14T03:00:00Z"
    assert disabled.updated_at == disabled.disabled_at
    assert disabled.disabled_reason == "offboarded"
    assert (disabled.revision, disabled.authz_revision, disabled.credential_epoch) == (2, 1, 1)


def test_repository_never_commits_its_own_transaction(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with Session(engine) as session, session.begin():
            repository = _repository(session, clock)
            created = _create_alice(repository)
            repository.grant(
                assignment_id="assignment:rolled-back",
                principal_id=created.principal_id,
                role="qa",
                scope=None,
                granted_by=ADMIN,
                expected_principal_revision=created.revision,
            )
            raise RuntimeError("rollback sentinel")

    with Session(engine) as session:
        assert session.scalars(select(PrincipalRow)).all() == []
        assert session.scalars(select(RoleAssignmentRow)).all() == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda: (
                update(PrincipalRow)
                .where(PrincipalRow.principal_id == "human:alice")
                .values(revision=0)
            ),
            "stored principal row",
        ),
        (
            lambda: (
                update(RoleAssignmentRow)
                .where(RoleAssignmentRow.assignment_id == "assignment:qa")
                .values(scope_key="2:corrupt")
            ),
            "stored role assignment row",
        ),
    ],
)
def test_corrupt_rows_fail_closed_on_read_and_projection(
    engine: Engine,
    clock: _MutableUtcClock,
    mutation,
    message: str,
) -> None:
    with Session(engine) as session:
        repository = _repository(session, clock)
        created = _create_alice(repository)
        repository.grant(
            assignment_id="assignment:qa",
            principal_id=created.principal_id,
            role="qa",
            scope=None,
            granted_by=ADMIN,
            expected_principal_revision=created.revision,
        )
        session.commit()
        session.execute(mutation())
        session.commit()

        with pytest.raises(IntegrityViolation, match=message):
            repository.project(created.principal_id)


def test_clock_must_return_timezone_aware_utc(
    engine: Engine,
    clock: _MutableUtcClock,
) -> None:
    clock.current = datetime(2026, 7, 14, 1, 0)
    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="clock must return UTC"):
            _create_alice(_repository(session, clock))
