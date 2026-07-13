from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from threading import Barrier, Thread

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.lineage import (
    ObjectBinding,
    ObjectLocation,
    ObjectRef,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import ObjectStat
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import ArtifactRow, Base, ObjectBindingRow
from gameforge.runtime.persistence.object_bindings import (
    ObjectReferenceState,
    SqlObjectBindingRepository,
)
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


@dataclass
class _FakeObjectStore:
    stats: dict[tuple[str, str, str], ObjectStat]

    def __init__(self) -> None:
        self.stats = {}

    def register(
        self,
        ref: ObjectRef,
        location: ObjectLocation,
        *,
        verified_at: str = "2026-07-13T00:00:00Z",
    ) -> ObjectStat:
        stat = ObjectStat(ref=ref, location=location, verified_at=verified_at)
        self.stats[_location_key(location)] = stat
        return stat

    def stat(self, location: ObjectLocation) -> ObjectStat:
        try:
            return self.stats[_location_key(location)]
        except KeyError as exc:
            raise FileNotFoundError(location.backend_generation) from exc


def _location_key(location: ObjectLocation) -> tuple[str, str, str]:
    return (location.store_id, location.key, location.backend_generation)


def _location(
    ref: ObjectRef,
    generation: str,
    *,
    store_id: str = "local",
    etag: str | None = None,
) -> ObjectLocation:
    return ObjectLocation(
        store_id=store_id,
        key=ref.key,
        backend_generation=generation,
        etag=etag,
    )


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'bindings.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


@pytest.fixture
def object_store() -> _FakeObjectStore:
    return _FakeObjectStore()


def _repository(
    session: Session,
    object_store: _FakeObjectStore,
) -> SqlObjectBindingRepository:
    return SqlObjectBindingRepository(
        session=session,
        object_store=object_store,
        default_store_id="local",
    )


def test_bind_create_resolve_and_exact_retry_are_idempotent(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload")
    location = _location(ref, "generation-1", etag="etag-1")
    object_store.register(ref, location)

    with Session(engine) as session:
        repository = _repository(session, object_store)
        created = repository.bind_verified(ref, location, expected_revision=None)
        retried_without_revision = repository.bind_verified(
            ref,
            location,
            expected_revision=None,
        )
        retried_with_revision = repository.bind_verified(
            ref,
            location,
            expected_revision=1,
        )
        session.commit()

    assert created == ObjectBinding(
        object_ref=ref,
        location=location,
        status="active",
        revision=1,
        verified_at="2026-07-13T00:00:00Z",
    )
    assert retried_without_revision == created
    assert retried_with_revision == created

    with Session(engine) as session:
        repository = _repository(session, object_store)
        assert repository.resolve(ref) == created
        assert repository.resolve(ref, store_id="local") == created
        assert repository.has_active_binding(ref)
        assert session.scalar(select(func.count()).select_from(ObjectBindingRow)) == 1


def test_bind_requires_object_store_stat_to_match_ref_and_location_exactly(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload")
    location = _location(ref, "generation-1", etag="expected")
    mismatched_location = location.model_copy(update={"etag": "other"})
    object_store.stats[_location_key(location)] = ObjectStat(
        ref=ref,
        location=mismatched_location,
        verified_at="2026-07-13T00:00:00Z",
    )

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="stat"):
            _repository(session, object_store).bind_verified(
                ref,
                location,
                expected_revision=None,
            )
        assert session.scalar(select(func.count()).select_from(ObjectBindingRow)) == 0


def test_bind_rejects_missing_object_location_as_integrity_failure(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"missing")
    location = _location(ref, "missing-generation")

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="not available"):
            _repository(session, object_store).bind_verified(
                ref,
                location,
                expected_revision=None,
            )
        assert session.scalar(select(func.count()).select_from(ObjectBindingRow)) == 0


def test_remap_and_retire_use_revision_cas_and_do_not_silently_rebind(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload")
    first = _location(ref, "generation-1")
    second = _location(ref, "generation-2")
    third = _location(ref, "generation-3")
    for location in (first, second, third):
        object_store.register(ref, location)

    with Session(engine) as session:
        repository = _repository(session, object_store)
        original = repository.bind_verified(ref, first, expected_revision=None)

        with pytest.raises(Conflict):
            repository.bind_verified(ref, second, expected_revision=None)

        remapped = repository.bind_verified(ref, second, expected_revision=original.revision)
        assert remapped.location == second
        assert remapped.revision == 2

        with pytest.raises(Conflict):
            repository.bind_verified(ref, third, expected_revision=original.revision)

        with pytest.raises(Conflict):
            repository.retire(original, expected_revision=original.revision)

        retired = repository.retire(remapped, expected_revision=remapped.revision)
        assert retired.status == "retired"
        assert retired.revision == 3
        with pytest.raises(Conflict):
            repository.retire(retired, expected_revision=retired.revision)
        assert not repository.has_active_binding(ref)
        with pytest.raises(FileNotFoundError):
            repository.resolve(ref)

        with pytest.raises(Conflict):
            repository.bind_verified(ref, third, expected_revision=2)

        rebound = repository.bind_verified(ref, third, expected_revision=retired.revision)
        assert rebound.status == "active"
        assert rebound.revision == 4
        session.commit()


def test_retire_rejects_the_last_active_binding_for_a_committed_artifact(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"published payload")
    location = _location(ref, "generation-only")
    object_store.register(ref, location)

    with Session(engine) as session:
        repository = _repository(session, object_store)
        binding = repository.bind_verified(ref, location, expected_revision=None)
        session.add(_artifact_row("published-artifact", ref))
        session.commit()

    with Session(engine) as session:
        repository = _repository(session, object_store)
        with pytest.raises(Conflict, match="last active"):
            repository.retire(binding, expected_revision=binding.revision)
        session.rollback()

    with Session(engine) as session:
        row = session.get(ObjectBindingRow, (ref.key, location.store_id))
        assert row is not None
        assert row.status == "active"
        assert row.revision == binding.revision


def test_retire_allows_a_referenced_binding_when_an_active_replica_remains(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"replicated published payload")
    local = _location(ref, "generation-local")
    replica = _location(ref, "generation-replica", store_id="replica")
    object_store.register(ref, local)
    object_store.register(ref, replica)

    with Session(engine) as session:
        repository = _repository(session, object_store)
        local_binding = repository.bind_verified(ref, local, expected_revision=None)
        replica_binding = repository.bind_verified(ref, replica, expected_revision=None)
        session.add(_artifact_row("replicated-artifact", ref))
        session.commit()

    with Session(engine) as session:
        retired = _repository(session, object_store).retire(
            local_binding,
            expected_revision=local_binding.revision,
        )
        session.commit()

    assert retired.status == "retired"
    assert retired.revision == local_binding.revision + 1
    with Session(engine) as session:
        repository = _repository(session, object_store)
        assert repository.resolve(ref, store_id="replica") == replica_binding
        with pytest.raises(FileNotFoundError):
            repository.resolve(ref, store_id="local")


def test_retire_fails_closed_when_a_peer_binding_row_is_corrupt(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload with corrupt replica")
    local = _location(ref, "generation-local")
    replica = _location(ref, "generation-replica", store_id="replica")
    object_store.register(ref, local)
    object_store.register(ref, replica)

    with Session(engine) as session:
        repository = _repository(session, object_store)
        local_binding = repository.bind_verified(ref, local, expected_revision=None)
        repository.bind_verified(ref, replica, expected_revision=None)
        session.add(_artifact_row("artifact-with-corrupt-replica", ref))
        session.execute(
            update(ObjectBindingRow)
            .where(
                ObjectBindingRow.object_key == ref.key,
                ObjectBindingRow.store_id == "replica",
            )
            .values(binding_schema_version="object-binding@999")
        )
        session.commit()

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="stored ObjectBinding"):
            _repository(session, object_store).retire(
                local_binding,
                expected_revision=local_binding.revision,
            )
        session.rollback()

    with Session(engine) as session:
        local_row = session.get(ObjectBindingRow, (ref.key, "local"))
        assert local_row is not None
        assert local_row.status == "active"
        assert local_row.revision == local_binding.revision


def test_uow_rollback_restores_a_retired_referenced_binding(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"rollback retirement")
    local = _location(ref, "generation-local")
    replica = _location(ref, "generation-replica", store_id="replica")
    object_store.register(ref, local)
    object_store.register(ref, replica)

    def capabilities(session: Session) -> TransactionCapabilities:
        unused = object()
        return TransactionCapabilities(
            refs=unused,
            audit=unused,
            approvals=unused,
            lineage=unused,
            object_bindings=_repository(session, object_store),
            runs=unused,
            cost=unused,
        )

    uow = SqliteUnitOfWork(engine, capabilities)
    with uow.begin() as transaction:
        local_binding = transaction.object_bindings.bind_verified(ref, local, None)
        transaction.object_bindings.bind_verified(ref, replica, None)
    with Session(engine) as session, session.begin():
        session.add(_artifact_row("rollback-artifact", ref))

    with pytest.raises(RuntimeError, match="rollback retirement"):
        with uow.begin() as transaction:
            transaction.object_bindings.retire(
                local_binding,
                expected_revision=local_binding.revision,
            )
            raise RuntimeError("rollback retirement")

    with Session(engine) as session:
        row = session.get(ObjectBindingRow, (ref.key, local.store_id))
        assert row is not None
        assert row.status == "active"
        assert row.revision == local_binding.revision


def test_two_sqlite_retire_writers_cannot_remove_every_active_binding(
    tmp_path,
    object_store: _FakeObjectStore,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'retire-race.db'}"
    first_engine = get_engine(database_url)
    second_engine = get_engine(database_url)
    Base.metadata.create_all(first_engine)
    ref = object_ref_for_bytes(b"concurrent retirement")
    local = _location(ref, "generation-local")
    replica = _location(ref, "generation-replica", store_id="replica")
    object_store.register(ref, local)
    object_store.register(ref, replica)

    def capabilities(session: Session) -> TransactionCapabilities:
        unused = object()
        return TransactionCapabilities(
            refs=unused,
            audit=unused,
            approvals=unused,
            lineage=unused,
            object_bindings=_repository(session, object_store),
            runs=unused,
            cost=unused,
        )

    first_uow = SqliteUnitOfWork(first_engine, capabilities)
    second_uow = SqliteUnitOfWork(second_engine, capabilities)
    with first_uow.begin() as transaction:
        local_binding = transaction.object_bindings.bind_verified(ref, local, None)
        replica_binding = transaction.object_bindings.bind_verified(ref, replica, None)
    with Session(first_engine) as session, session.begin():
        session.add(_artifact_row("concurrent-artifact", ref))

    barrier = Barrier(3)
    outcomes: list[ObjectBinding | BaseException] = []

    def retire(
        unit_of_work: SqliteUnitOfWork,
        binding: ObjectBinding,
    ) -> None:
        barrier.wait(timeout=10)
        try:
            with unit_of_work.begin() as transaction:
                outcome = transaction.object_bindings.retire(
                    binding,
                    expected_revision=binding.revision,
                )
        except BaseException as exc:
            outcomes.append(exc)
        else:
            outcomes.append(outcome)

    first_thread = Thread(target=retire, args=(first_uow, local_binding))
    second_thread = Thread(target=retire, args=(second_uow, replica_binding))
    try:
        first_thread.start()
        second_thread.start()
        barrier.wait(timeout=10)
        first_thread.join(timeout=10)
        second_thread.join(timeout=10)
    finally:
        first_thread.join(timeout=10)
        second_thread.join(timeout=10)

    assert first_thread.is_alive() is False
    assert second_thread.is_alive() is False
    assert sum(isinstance(outcome, ObjectBinding) for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, Conflict)]
    assert len(conflicts) == 1
    assert "last active" in str(conflicts[0])

    with Session(first_engine) as session:
        rows = session.scalars(
            select(ObjectBindingRow).where(ObjectBindingRow.object_key == ref.key)
        ).all()
        assert sum(row.status == "active" for row in rows) == 1
        assert sum(row.status == "retired" for row in rows) == 1

    first_engine.dispose()
    second_engine.dispose()


def test_explicit_non_default_store_can_resolve_and_satisfy_active_binding(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"replicated payload")
    replica = _location(ref, "replica-generation", store_id="replica")
    object_store.register(ref, replica)

    with Session(engine) as session:
        repository = _repository(session, object_store)
        bound = repository.bind_verified(ref, replica, expected_revision=None)
        assert repository.resolve(ref, store_id="replica") == bound
        assert repository.has_active_binding(ref)
        with pytest.raises(FileNotFoundError):
            repository.resolve(ref)


def test_same_key_with_different_object_identity_is_integrity_failure(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload")
    location = _location(ref, "generation-1")
    object_store.register(ref, location)

    with Session(engine) as session:
        session.add(
            ObjectBindingRow(
                object_key=ref.key,
                store_id="local",
                binding_schema_version="object-binding@1",
                object_ref_schema_version="object-ref@1",
                location_schema_version="object-location@1",
                object_sha256=ref.sha256,
                object_size_bytes=ref.size_bytes + 1,
                backend_generation="generation-old",
                etag=None,
                storage_class=None,
                status="active",
                revision=1,
                verified_at="2026-07-13T00:00:00Z",
            )
        )
        session.commit()

    with Session(engine) as session:
        repository = _repository(session, object_store)
        with pytest.raises(IntegrityViolation, match="ObjectRef"):
            repository.bind_verified(ref, location, expected_revision=1)
        with pytest.raises(IntegrityViolation, match="ObjectRef"):
            repository.resolve(ref)
        with pytest.raises(IntegrityViolation, match="ObjectRef"):
            repository.has_active_binding(ref)


def test_active_lookup_does_not_ignore_a_corrupt_replica_row(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload")
    local = _location(ref, "generation-local")
    object_store.register(ref, local)

    with Session(engine) as session:
        repository = _repository(session, object_store)
        repository.bind_verified(ref, local, expected_revision=None)
        session.add(
            ObjectBindingRow(
                object_key=ref.key,
                store_id="replica",
                binding_schema_version="object-binding@999",
                object_ref_schema_version="object-ref@1",
                location_schema_version="object-location@1",
                object_sha256=ref.sha256,
                object_size_bytes=ref.size_bytes,
                backend_generation="generation-replica",
                etag=None,
                storage_class=None,
                status="active",
                revision=1,
                verified_at="2026-07-13T00:00:00Z",
            )
        )
        session.flush()

        with pytest.raises(IntegrityViolation, match="stored ObjectBinding"):
            repository.has_active_binding(ref)


def test_corrupt_binding_row_fails_closed(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload")
    location = _location(ref, "generation-1")
    object_store.register(ref, location)

    with Session(engine) as session:
        repository = _repository(session, object_store)
        repository.bind_verified(ref, location, expected_revision=None)
        session.execute(
            update(ObjectBindingRow)
            .where(ObjectBindingRow.object_key == ref.key)
            .values(status="corrupt")
        )
        session.commit()

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="stored ObjectBinding"):
            _repository(session, object_store).resolve(ref)


def test_two_stale_remappers_have_one_winner(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload")
    first = _location(ref, "generation-1")
    winner = _location(ref, "generation-winner")
    loser = _location(ref, "generation-loser")
    for location in (first, winner, loser):
        object_store.register(ref, location)

    with Session(engine) as session:
        original = _repository(session, object_store).bind_verified(
            ref,
            first,
            expected_revision=None,
        )
        session.commit()

    with Session(engine) as session:
        won = _repository(session, object_store).bind_verified(
            ref,
            winner,
            expected_revision=original.revision,
        )
        session.commit()
    assert won.revision == 2

    with Session(engine) as session:
        with pytest.raises(Conflict):
            _repository(session, object_store).bind_verified(
                ref,
                loser,
                expected_revision=original.revision,
            )
        session.rollback()

    with Session(engine) as session:
        assert _repository(session, object_store).resolve(ref) == won


def _artifact_row(artifact_id: str, ref: ObjectRef | None) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=artifact_id,
        lineage_schema_version="lineage@2" if ref is not None else "lineage@1",
        kind="run_result",
        version_tuple={},
        lineage=[],
        payload_hash=None if ref is None else ref.sha256,
        created_at=None,
        meta={},
        object_ref=None if ref is None else ref.model_dump(mode="json"),
    )


def test_reference_states_protect_all_artifact_refs_and_distinguish_binding_scope(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    historical_ref = object_ref_for_bytes(b"historical")
    unreferenced_ref = object_ref_for_bytes(b"unreferenced")
    historical_current = _location(historical_ref, "generation-current")
    historical_old = _location(historical_ref, "generation-old")
    historical_other_store = _location(
        historical_ref,
        "generation-replica",
        store_id="replica",
    )
    unreferenced = _location(unreferenced_ref, "generation-only")
    stats = tuple(
        object_store.register(ref, location)
        for ref, location in (
            (historical_ref, historical_current),
            (historical_ref, historical_old),
            (historical_ref, historical_other_store),
            (unreferenced_ref, unreferenced),
        )
    )

    with Session(engine) as session:
        repository = _repository(session, object_store)
        repository.bind_verified(
            historical_ref,
            historical_current,
            expected_revision=None,
        )
        session.add_all(
            [
                _artifact_row("historical-artifact", historical_ref),
                _artifact_row("legacy-artifact", None),
            ]
        )
        session.flush()

        states = repository.reference_states(stats)

    assert states[_location_key(historical_current)] == ObjectReferenceState(
        exact_active_binding=True,
        artifact_referenced=True,
        any_active_binding_for_ref=True,
    )
    assert states[_location_key(historical_old)] == ObjectReferenceState(
        exact_active_binding=False,
        artifact_referenced=True,
        any_active_binding_for_ref=True,
    )
    assert states[_location_key(historical_other_store)] == ObjectReferenceState(
        exact_active_binding=False,
        artifact_referenced=True,
        any_active_binding_for_ref=True,
    )
    assert states[_location_key(unreferenced)] == ObjectReferenceState(
        exact_active_binding=False,
        artifact_referenced=False,
        any_active_binding_for_ref=False,
    )


def test_reference_states_reject_unbounded_or_ambiguous_input(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload")
    location = _location(ref, "generation-1")
    stat = object_store.register(ref, location)
    conflicting_stat = stat.model_copy(
        update={"ref": stat.ref.model_copy(update={"size_bytes": stat.ref.size_bytes + 1})}
    )

    with Session(engine) as session:
        repository = _repository(session, object_store)
        with pytest.raises(IntegrityViolation, match="duplicate"):
            repository.reference_states((stat, conflicting_stat))
        with pytest.raises(ValueError, match="bounded"):
            repository.reference_states([stat] * 1001)


def test_reference_states_reject_active_generation_metadata_mismatch(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload")
    bound_location = _location(ref, "generation-1", etag="bound-etag")
    object_store.register(ref, bound_location)

    with Session(engine) as session:
        repository = _repository(session, object_store)
        repository.bind_verified(ref, bound_location, expected_revision=None)
        changed_stat = ObjectStat(
            ref=ref,
            location=bound_location.model_copy(update={"etag": "changed-etag"}),
            verified_at="2026-07-13T00:00:00Z",
        )
        with pytest.raises(IntegrityViolation, match="location differs"):
            repository.reference_states((changed_stat,))


def test_uow_rollback_removes_binding_write(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    ref = object_ref_for_bytes(b"payload")
    location = _location(ref, "generation-1")
    object_store.register(ref, location)

    def capabilities(session: Session) -> TransactionCapabilities:
        unused = object()
        return TransactionCapabilities(
            refs=unused,
            audit=unused,
            approvals=unused,
            lineage=unused,
            object_bindings=_repository(session, object_store),
            runs=unused,
            cost=unused,
        )

    with pytest.raises(RuntimeError, match="rollback"):
        with SqliteUnitOfWork(engine, capabilities).begin() as transaction:
            transaction.object_bindings.bind_verified(
                ref,
                location,
                expected_revision=None,
            )
            raise RuntimeError("rollback")

    with Session(engine) as session:
        assert session.get(ObjectBindingRow, (ref.key, "local")) is None


def test_default_store_id_must_be_explicit(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    with Session(engine) as session:
        with pytest.raises(ValueError, match="default_store_id"):
            SqlObjectBindingRepository(
                session=session,
                object_store=object_store,
                default_store_id="",
            )


def test_reference_states_accepts_sequence_contract(
    engine: Engine,
    object_store: _FakeObjectStore,
) -> None:
    """Keep the concrete GC handoff signature from drifting to an iterator."""

    ref = object_ref_for_bytes(b"payload")
    location = _location(ref, "generation-1")
    stat = object_store.register(ref, location)

    with Session(engine) as session:
        repository = _repository(session, object_store)
        argument: Sequence[ObjectStat] = (stat,)
        assert repository.reference_states(argument)[_location_key(location)] == (
            ObjectReferenceState(False, False, False)
        )
