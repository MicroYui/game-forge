from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from typing import Any, BinaryIO, Iterator, Sequence

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from gameforge.contracts.errors import (
    CursorExpired,
    CursorInvalid,
    IntegrityViolation,
    RetentionActive,
)
from gameforge.contracts.lineage import ObjectLocation, ObjectRef, object_ref_for_bytes
from gameforge.contracts.storage import (
    GcCandidate,
    ObjectStat,
    PageCursorV1,
    PageV1,
    StoredObject,
)
from gameforge.platform.storage.object_gc import (
    NoRecoveryPins,
    ObjectGcService,
    ObjectReferenceState,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store.local import LocalObjectStore
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base, ObjectBindingRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
_EXPIRES = "2026-07-13T12:05:00Z"
_QUERY_HASH = "a" * 64


def _stat(
    payload: bytes,
    *,
    generation: str,
    verified_at: str = "2026-07-13T09:00:00Z",
    retention_until: str | None = None,
) -> ObjectStat:
    ref = object_ref_for_bytes(payload)
    return ObjectStat(
        ref=ref,
        location=ObjectLocation(
            store_id="local:test",
            key=ref.key,
            backend_generation=generation,
            etag=ref.sha256,
            storage_class="local",
        ),
        verified_at=verified_at,
        retention_until=retention_until,
    )


def _cursor(snapshot_id: str, position: int) -> PageCursorV1:
    return PageCursorV1(
        snapshot_id=snapshot_id,
        position=str(position),
        page_size=2,
        query_hash=_QUERY_HASH,
        opaque_signature="signed",
    )


def _page(
    snapshot_id: str,
    items: Sequence[ObjectStat],
    *,
    next_position: int | None = None,
) -> PageV1[ObjectStat]:
    return PageV1[ObjectStat](
        read_snapshot_id=snapshot_id,
        items=tuple(items),
        next_cursor=(None if next_position is None else _cursor(snapshot_id, next_position)),
        expires_at=_EXPIRES,
    )


def _key(stat: ObjectStat) -> tuple[str, str, str]:
    return (
        stat.location.store_id,
        stat.location.key,
        stat.location.backend_generation,
    )


class _FakeObjects:
    def __init__(self, pages: Sequence[PageV1[ObjectStat]], uow: "_FakeUow") -> None:
        self._pages = list(pages)
        self._uow = uow
        self._stats = {_key(stat): stat for page in pages for stat in page.items}
        self.deleted: list[ObjectLocation] = []
        self.delete_result = True
        self.delete_failure: BaseException | None = None

    def list_versions(self, cursor: PageCursorV1 | None = None) -> PageV1[ObjectStat]:
        index = 0 if cursor is None else int(cursor.position)
        return self._pages[index]

    def stat(self, location: ObjectLocation) -> ObjectStat:
        try:
            return self._stats[(location.store_id, location.key, location.backend_generation)]
        except KeyError as exc:
            raise FileNotFoundError(location.backend_generation) from exc

    def delete_if_generation(self, location: ObjectLocation) -> bool:
        assert self._uow.active, "GC must hold the DB write transaction through delete"
        self.deleted.append(location)
        if self.delete_failure is not None:
            raise self.delete_failure
        if not self.delete_result:
            return False
        self._stats.pop((location.store_id, location.key, location.backend_generation), None)
        return True


class _FakeBindings:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str, str], ObjectReferenceState] = {}

    def reference_states(
        self,
        stats: Sequence[ObjectStat],
    ) -> dict[tuple[str, str, str], ObjectReferenceState]:
        return {_key(stat): self.states.get(_key(stat), ObjectReferenceState()) for stat in stats}


@dataclass
class _FakeTransaction:
    object_bindings: _FakeBindings


class _FakeUow:
    def __init__(self, bindings: _FakeBindings) -> None:
        self._transaction = _FakeTransaction(bindings)
        self.active = False

    @contextmanager
    def begin(self) -> Iterator[_FakeTransaction]:
        assert not self.active
        self.active = True
        try:
            yield self._transaction
        finally:
            self.active = False


class _RecoveryPins:
    def __init__(self) -> None:
        self.pinned: set[tuple[str, str, str]] = set()
        self.failure: BaseException | None = None

    def is_pinned(self, ref: ObjectRef, location: ObjectLocation) -> bool:
        if self.failure is not None:
            raise self.failure
        return (location.store_id, ref.key, location.backend_generation) in self.pinned


class _DeleteGateObjectStore:
    """Pause a real local deletion after ObjectGc's transactional recheck."""

    def __init__(
        self,
        delegate: LocalObjectStore,
        *,
        delete_entered: Event,
        allow_delete: Event,
    ) -> None:
        self._delegate = delegate
        self._delete_entered = delete_entered
        self._allow_delete = allow_delete

    def put_verified(self, source: bytes | BinaryIO) -> StoredObject:
        return self._delegate.put_verified(source)

    def open(self, location: ObjectLocation) -> BinaryIO:
        return self._delegate.open(location)

    def stat(self, location: ObjectLocation) -> ObjectStat:
        return self._delegate.stat(location)

    def list_versions(
        self,
        cursor: PageCursorV1 | None = None,
    ) -> PageV1[ObjectStat]:
        return self._delegate.list_versions(cursor)

    def delete_if_generation(self, location: ObjectLocation) -> bool:
        self._delete_entered.set()
        if not self._allow_delete.wait(timeout=10):
            raise AssertionError("timed out waiting to release the GC deletion")
        return self._delegate.delete_if_generation(location)


def _service(
    pages: Sequence[PageV1[ObjectStat]],
    *,
    bindings: _FakeBindings | None = None,
    pins: _RecoveryPins | None = None,
) -> tuple[ObjectGcService, _FakeObjects, _FakeBindings, _RecoveryPins]:
    binding_index = bindings or _FakeBindings()
    recovery_pins = pins or _RecoveryPins()
    uow = _FakeUow(binding_index)
    objects = _FakeObjects(pages, uow)
    service = ObjectGcService(
        objects=objects,
        unit_of_work=uow,
        recovery_pins=recovery_pins,
        clock=FrozenUtcClock(_NOW),
        minimum_safe_age=timedelta(hours=1),
    )
    return service, objects, binding_index, recovery_pins


def test_plan_uses_strict_safe_before_boundary_and_one_bounded_store_page() -> None:
    before = _stat(b"before", generation="g1", verified_at="2026-07-13T09:59:59Z")
    equal = _stat(b"equal", generation="g2", verified_at="2026-07-13T10:00:00Z")
    after = _stat(b"after", generation="g3", verified_at="2026-07-13T10:00:01Z")
    service, _, _, _ = _service([_page("scan-1", [before, equal, after])])

    planned = service.plan(None, "2026-07-13T10:00:00Z")

    assert [item.location for item in planned.items] == [before.location]


@pytest.mark.parametrize(
    "safe_before",
    ["not-a-time", "2026-07-13T10:00:00", "2026-07-13T13:00:00+01:00"],
)
def test_plan_rejects_invalid_or_non_utc_cutoff(safe_before: str) -> None:
    service, _, _, _ = _service([_page("scan-1", [])])
    with pytest.raises(ValueError, match="UTC"):
        service.plan(None, safe_before)


def test_plan_rejects_cutoff_inside_minimum_safe_age() -> None:
    service, _, _, _ = _service([_page("scan-1", [])])
    with pytest.raises(ValueError, match="minimum safe age"):
        service.plan(None, "2026-07-13T11:00:01Z")


def test_plan_preserves_empty_filtered_page_and_binds_cutoff_to_snapshot() -> None:
    protected = _stat(b"protected", generation="g1")
    orphan = _stat(b"orphan", generation="g2")
    first = _page("scan-1", [protected], next_position=1)
    second = _page("scan-1", [orphan])
    bindings = _FakeBindings()
    bindings.states[_key(protected)] = ObjectReferenceState(exact_active_binding=True)
    service, _, _, _ = _service([first, second], bindings=bindings)

    page_one = service.plan(None, "2026-07-13T10:00:00Z")
    assert page_one.items == ()
    assert page_one.next_cursor is not None

    with pytest.raises(CursorInvalid, match="cutoff"):
        service.plan(page_one.next_cursor, "2026-07-13T09:59:59Z")

    page_two = service.plan(page_one.next_cursor, "2026-07-13T10:00:00Z")
    assert [item.location for item in page_two.items] == [orphan.location]


def test_plan_rejects_unknown_continuation_snapshot() -> None:
    service, _, _, _ = _service([_page("scan-1", [])])
    with pytest.raises(CursorExpired, match="snapshot"):
        service.plan(_cursor("unknown", 1), "2026-07-13T10:00:00Z")


def test_plan_distinguishes_active_referenced_and_redundant_generations() -> None:
    active = _stat(b"active", generation="active-g")
    damaged = _stat(b"damaged", generation="only-g")
    redundant = _stat(b"redundant", generation="old-g")
    bindings = _FakeBindings()
    bindings.states[_key(active)] = ObjectReferenceState(
        exact_active_binding=True,
        artifact_referenced=True,
        any_active_binding_for_ref=True,
    )
    bindings.states[_key(damaged)] = ObjectReferenceState(
        artifact_referenced=True,
        any_active_binding_for_ref=False,
    )
    bindings.states[_key(redundant)] = ObjectReferenceState(
        artifact_referenced=True,
        any_active_binding_for_ref=True,
    )
    service, _, _, _ = _service(
        [_page("scan-1", [active, damaged, redundant])],
        bindings=bindings,
    )

    planned = service.plan(None, "2026-07-13T10:00:00Z")

    assert [item.location for item in planned.items] == [redundant.location]


def test_recovery_pin_excludes_plan_and_is_rechecked_at_collect() -> None:
    stat = _stat(b"pinned", generation="g1")
    pins = _RecoveryPins()
    pins.pinned.add(_key(stat))
    service, objects, _, _ = _service([_page("scan-1", [stat])], pins=pins)
    assert service.plan(None, "2026-07-13T10:00:00Z").items == ()

    candidate = GcCandidate(
        location=stat.location, object_ref=stat.ref, observed_at=stat.verified_at
    )
    assert service.collect(candidate) == "retained_referenced"
    assert objects.deleted == []


def test_collect_rechecks_reference_after_plan_and_never_deletes_live_object() -> None:
    stat = _stat(b"becomes-live", generation="g1")
    service, objects, bindings, _ = _service([_page("scan-1", [stat])])
    candidate = service.plan(None, "2026-07-13T10:00:00Z").items[0]
    bindings.states[_key(stat)] = ObjectReferenceState(exact_active_binding=True)

    assert service.collect(candidate) == "retained_referenced"
    assert objects.deleted == []


def test_collect_fails_closed_when_pin_lookup_fails() -> None:
    stat = _stat(b"pin-error", generation="g1")
    pins = _RecoveryPins()
    pins.failure = RuntimeError("catalog unavailable")
    service, objects, _, _ = _service([_page("scan-1", [stat])], pins=pins)
    candidate = GcCandidate(
        location=stat.location, object_ref=stat.ref, observed_at=stat.verified_at
    )

    with pytest.raises(RuntimeError, match="catalog unavailable"):
        service.collect(candidate)
    assert objects.deleted == []


@pytest.mark.parametrize(
    ("verified_at", "retention_until"),
    [
        ("2026-07-13T11:00:00Z", None),
        ("2026-07-13T09:00:00Z", "2026-07-13T12:00:01Z"),
    ],
)
def test_collect_rechecks_safe_age_and_backend_retention(
    verified_at: str,
    retention_until: str | None,
) -> None:
    stat = _stat(
        b"retained",
        generation="g1",
        verified_at=verified_at,
        retention_until=retention_until,
    )
    service, objects, _, _ = _service([_page("scan-1", [stat])])
    candidate = GcCandidate(
        location=stat.location, object_ref=stat.ref, observed_at=stat.verified_at
    )

    assert service.collect(candidate) == "retention_active"
    assert objects.deleted == []


def test_collect_detects_generation_change_and_candidate_corruption() -> None:
    stat = _stat(b"changed", generation="g1")
    service, objects, _, _ = _service([_page("scan-1", [stat])])
    candidate = GcCandidate(
        location=stat.location, object_ref=stat.ref, observed_at=stat.verified_at
    )
    objects._stats.clear()
    assert service.collect(candidate) == "retained_generation_changed"

    objects._stats[_key(stat)] = stat
    wrong_ref = ObjectRef(
        key=stat.ref.key,
        sha256=stat.ref.sha256,
        size_bytes=stat.ref.size_bytes + 1,
    )
    corrupt = GcCandidate(
        location=stat.location, object_ref=wrong_ref, observed_at=stat.verified_at
    )
    with pytest.raises(IntegrityViolation, match="candidate"):
        service.collect(corrupt)


def test_collect_deletes_exact_generation_once_inside_uow() -> None:
    stat = _stat(b"delete", generation="g1")
    service, objects, _, _ = _service([_page("scan-1", [stat])])
    candidate = GcCandidate(
        location=stat.location, object_ref=stat.ref, observed_at=stat.verified_at
    )

    assert service.collect(candidate) == "deleted"
    assert service.collect(candidate) == "retained_generation_changed"
    assert objects.deleted == [stat.location]


def test_collect_maps_generation_cas_and_backend_retention_results() -> None:
    stat = _stat(b"conditional-delete", generation="g1")
    service, objects, _, _ = _service([_page("scan-1", [stat])])
    candidate = GcCandidate(
        location=stat.location,
        object_ref=stat.ref,
        observed_at=stat.verified_at,
    )

    objects.delete_result = False
    assert service.collect(candidate) == "retained_generation_changed"

    objects.delete_failure = RetentionActive("locked")
    assert service.collect(candidate) == "retention_active"


def test_explicit_no_recovery_pins_policy_never_pins() -> None:
    stat = _stat(b"no-pins", generation="g1")
    assert NoRecoveryPins().is_pinned(stat.ref, stat.location) is False


def test_collect_serializes_real_sqlite_binding_with_generation_delete(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'gc-race.db'}"
    gc_engine = get_engine(database_url)
    binding_engine = get_engine(database_url)
    Base.metadata.create_all(gc_engine)

    def no_wait_while_gc_holds_the_write_lock(
        dbapi_connection: Any,
        connection_record: object,
    ) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=0")
        finally:
            cursor.close()

    event.listen(
        binding_engine,
        "connect",
        no_wait_while_gc_holds_the_write_lock,
    )

    object_store = LocalObjectStore(
        tmp_path / "objects",
        store_id="local:test",
        clock=FrozenUtcClock(datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)),
        cursor_signing_key=b"g" * 32,
    )
    stored = object_store.put_verified(b"race payload")
    stat = object_store.stat(stored.location)

    delete_entered = Event()
    allow_delete = Event()
    retry_after_gc = Event()
    first_binding_finished = Event()
    binding_finished = Event()
    gc_finished = Event()
    gated_store = _DeleteGateObjectStore(
        object_store,
        delete_entered=delete_entered,
        allow_delete=allow_delete,
    )

    def capabilities(session: Session) -> TransactionCapabilities:
        unused = object()
        return TransactionCapabilities(
            refs=unused,
            audit=unused,
            approvals=unused,
            lineage=unused,
            object_bindings=SqlObjectBindingRepository(
                session=session,
                object_store=gated_store,
                default_store_id="local:test",
            ),
            runs=unused,
            cost=unused,
        )

    gc_service = ObjectGcService(
        objects=gated_store,
        unit_of_work=SqliteUnitOfWork(gc_engine, capabilities),
        recovery_pins=NoRecoveryPins(),
        clock=FrozenUtcClock(_NOW),
        minimum_safe_age=timedelta(hours=1),
    )
    candidate = GcCandidate(
        location=stat.location,
        object_ref=stat.ref,
        observed_at=stat.verified_at,
    )
    binding_uow = SqliteUnitOfWork(binding_engine, capabilities)
    gc_outcome: list[object] = []
    first_binding_outcome: list[object] = []
    retry_binding_outcome: list[object] = []

    def collect_candidate() -> None:
        try:
            gc_outcome.append(gc_service.collect(candidate))
        except BaseException as exc:
            gc_outcome.append(exc)
        finally:
            gc_finished.set()

    def bind_candidate() -> None:
        try:
            with binding_uow.begin() as transaction:
                transaction.object_bindings.bind_verified(
                    stored.ref,
                    stored.location,
                    expected_revision=None,
                )
        except BaseException as exc:
            first_binding_outcome.append(exc)
        else:
            first_binding_outcome.append("committed")
        finally:
            first_binding_finished.set()

        if not retry_after_gc.wait(timeout=10):
            retry_binding_outcome.append(
                AssertionError("timed out waiting to retry the binding after GC")
            )
            binding_finished.set()
            return
        try:
            with binding_uow.begin() as transaction:
                transaction.object_bindings.bind_verified(
                    stored.ref,
                    stored.location,
                    expected_revision=None,
                )
        except BaseException as exc:
            retry_binding_outcome.append(exc)
        else:
            retry_binding_outcome.append("committed")
        finally:
            binding_finished.set()

    gc_thread = Thread(target=collect_candidate, name="object-gc")
    binding_thread = Thread(target=bind_candidate, name="object-binding")
    try:
        gc_thread.start()
        assert delete_entered.wait(timeout=10), "GC did not reach generation deletion"

        binding_thread.start()
        assert first_binding_finished.wait(timeout=10), "binding attempt did not finish"

        allow_delete.set()
        assert gc_finished.wait(timeout=10), "GC did not finish"
        gc_thread.join(timeout=10)

        retry_after_gc.set()
        assert binding_finished.wait(timeout=10), "binding retry did not finish"
        binding_thread.join(timeout=10)
    finally:
        allow_delete.set()
        retry_after_gc.set()
        gc_thread.join(timeout=10)
        if binding_thread.ident is not None:
            binding_thread.join(timeout=10)
        event.remove(
            binding_engine,
            "connect",
            no_wait_while_gc_holds_the_write_lock,
        )

    assert gc_thread.is_alive() is False
    assert binding_thread.is_alive() is False
    assert gc_outcome == ["deleted"]
    assert len(first_binding_outcome) == 1
    assert isinstance(first_binding_outcome[0], OperationalError)
    assert "database is locked" in str(first_binding_outcome[0]).lower()
    assert len(retry_binding_outcome) == 1
    assert isinstance(retry_binding_outcome[0], IntegrityViolation)
    assert "not available" in str(retry_binding_outcome[0])

    with Session(gc_engine) as session:
        assert (
            session.get(
                ObjectBindingRow,
                (stored.ref.key, stored.location.store_id),
            )
            is None
        )
    with pytest.raises(FileNotFoundError):
        object_store.stat(stored.location)

    gc_engine.dispose()
    binding_engine.dispose()
