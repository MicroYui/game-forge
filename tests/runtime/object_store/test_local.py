from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import ObjectLocation, object_key_for_sha256
from gameforge.contracts.storage import ObjectStat, StoredObject
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store.local import LocalObjectStore


_STORE_ID = "local:test"
_CURSOR_KEY = b"local-object-store-test-cursor-key"
_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _store(
    root: Path,
    *,
    page_size: int = 2,
    file_ops: object | None = None,
) -> LocalObjectStore:
    return LocalObjectStore(
        root,
        store_id=_STORE_ID,
        clock=FrozenUtcClock(_NOW),
        cursor_signing_key=_CURSOR_KEY,
        page_size=page_size,
        snapshot_ttl=timedelta(minutes=5),
        file_ops=file_ops,
    )


def _generation_directory(root: Path, stored: StoredObject) -> Path:
    return root / stored.ref.key / stored.location.backend_generation


class _RecordingFileOps:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def fsync(self, fd: int) -> None:
        file_stat = os.fstat(fd)
        if stat.S_ISREG(file_stat.st_mode):
            self.events.append(("file-fsync", file_stat.st_size))
        elif stat.S_ISDIR(file_stat.st_mode):
            self.events.append(("directory-fsync", file_stat.st_dev, file_stat.st_ino))
        else:  # pragma: no cover - local adapter must only fsync files/directories
            self.events.append(("other-fsync", file_stat.st_mode))
        os.fsync(fd)

    def rename(self, source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        source_stat = source_path.stat()
        destination_parent_device = destination_path.parent.stat().st_dev
        self.events.append(
            (
                "rename",
                source_path,
                destination_path,
                source_stat.st_dev,
                destination_parent_device,
                source_stat.st_ino,
            )
        )
        os.rename(source_path, destination_path)


class _ShortReadStream(io.RawIOBase):
    """Non-seekable stream whose successful reads are deliberately short."""

    def __init__(self, payload: bytes, *, chunk_size: int = 3) -> None:
        self._payload = payload
        self._position = 0
        self._chunk_size = chunk_size
        self.read_sizes: list[int] = []

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            raise AssertionError("put_verified must request bounded stream chunks")
        if self._position == len(self._payload):
            return b""
        count = min(size, self._chunk_size, len(self._payload) - self._position)
        result = self._payload[self._position : self._position + count]
        self._position += count
        return result

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        raise AssertionError("put_verified must not seek its input stream")


@pytest.mark.parametrize("payload", [b"", b"bytes payload\x00with binary data"])
def test_put_verified_accepts_bytes_and_binds_hash_size_and_content(
    tmp_path: Path,
    payload: bytes,
) -> None:
    stored = _store(tmp_path).put_verified(payload)
    digest = hashlib.sha256(payload).hexdigest()

    assert stored.ref.sha256 == digest
    assert stored.ref.size_bytes == len(payload)
    assert stored.ref.key == object_key_for_sha256(digest)
    assert stored.location.store_id == _STORE_ID
    assert stored.location.key == stored.ref.key
    assert stored.location.backend_generation
    generation_directory = _generation_directory(tmp_path, stored)
    assert generation_directory.joinpath("data").read_bytes() == payload
    assert generation_directory.joinpath("metadata.json").is_file()


def test_put_verified_consumes_a_non_seekable_stream_until_eof(tmp_path: Path) -> None:
    payload = b"short reads are legal for binary streams"
    source = _ShortReadStream(payload)

    stored = _store(tmp_path).put_verified(source)

    assert stored.ref.sha256 == hashlib.sha256(payload).hexdigest()
    assert stored.ref.size_bytes == len(payload)
    assert _generation_directory(tmp_path, stored).joinpath("data").read_bytes() == payload
    assert len(source.read_sizes) > 2


def test_same_content_is_idempotent_across_bytes_stream_and_reopen(tmp_path: Path) -> None:
    payload = b"immutable object"
    first_store = _store(tmp_path)

    first = first_store.put_verified(payload)
    second = first_store.put_verified(_ShortReadStream(payload, chunk_size=2))
    reopened = _store(tmp_path).put_verified(payload)

    assert second == first
    assert reopened == first
    assert _generation_directory(tmp_path, first).joinpath("data").read_bytes() == payload


def test_reuse_racing_exact_delete_never_returns_a_deleted_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"reuse must remain live through its return boundary"
    initial = _store(tmp_path).put_verified(payload)
    reuse_store = _store(tmp_path)
    scanned_existing = threading.Event()
    allow_return = threading.Event()
    original_scan = reuse_store._verified_generations_for_ref

    def scan_then_block(ref: object) -> tuple[object, ...]:
        existing = original_scan(ref)
        if existing:
            scanned_existing.set()
            if not allow_return.wait(timeout=5):
                raise TimeoutError("test did not release existing-generation reuse")
        return existing

    monkeypatch.setattr(reuse_store, "_verified_generations_for_ref", scan_then_block)
    results: list[StoredObject] = []
    failures: list[BaseException] = []

    def reuse() -> None:
        try:
            results.append(reuse_store.put_verified(payload))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = threading.Thread(target=reuse)
    thread.start()
    assert scanned_existing.wait(timeout=5)
    assert _store(tmp_path).delete_if_generation(initial.location) is True
    allow_return.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert failures == []
    assert len(results) == 1
    returned = results[0]
    assert returned.ref == initial.ref
    assert _store(tmp_path).stat(returned.location).location == returned.location
    with _store(tmp_path).open(returned.location) as opened:
        assert opened.read() == payload


def test_reuse_final_verification_cannot_return_after_direct_generation_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"the final verified generation can disappear before Python returns"
    initial = _store(tmp_path).put_verified(payload)
    reuse_store = _store(tmp_path)
    final_verification_returned = threading.Event()
    allow_put_return = threading.Event()
    original_verify = reuse_store._verify_generation
    matching_calls = 0

    def verify_then_block(key: str, generation: str) -> object:
        nonlocal matching_calls
        verified = original_verify(key, generation)
        if key == initial.ref.key and generation == initial.location.backend_generation:
            matching_calls += 1
            if matching_calls == 2:
                final_verification_returned.set()
                if not allow_put_return.wait(timeout=5):
                    raise TimeoutError("test did not release the final reuse verification")
        return verified

    monkeypatch.setattr(reuse_store, "_verify_generation", verify_then_block)
    results: list[StoredObject] = []
    failures: list[BaseException] = []

    def reuse() -> None:
        try:
            results.append(reuse_store.put_verified(payload))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = threading.Thread(target=reuse)
    thread.start()
    assert final_verification_returned.wait(timeout=5)
    shutil.rmtree(_generation_directory(tmp_path, initial))
    allow_put_return.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    if failures:
        assert results == []
        assert len(failures) == 1
        assert isinstance(failures[0], (FileNotFoundError, IntegrityViolation))
    else:
        assert len(results) == 1
        returned = results[0]
        assert _store(tmp_path).stat(returned.location).location == returned.location


@pytest.mark.parametrize(
    "replacement",
    [
        b"jmmutable object",  # same size, different digest
        b"immutable object with an invalid extra suffix",  # different size
    ],
)
def test_existing_logical_key_with_different_content_or_size_fails_closed(
    tmp_path: Path,
    replacement: bytes,
) -> None:
    payload = b"immutable object"
    store = _store(tmp_path)
    stored = store.put_verified(payload)
    data_path = _generation_directory(tmp_path, stored) / "data"
    data_path.write_bytes(replacement)

    with pytest.raises(IntegrityViolation):
        store.put_verified(payload)

    assert data_path.read_bytes() == replacement


def test_open_and_stat_require_the_exact_store_key_and_generation(tmp_path: Path) -> None:
    payload = b"object opened by concrete backend generation"
    store = _store(tmp_path)
    stored = store.put_verified(payload)

    with store.open(stored.location) as opened:
        assert opened.read() == payload
    object_stat = store.stat(stored.location)
    assert object_stat.ref == stored.ref
    assert object_stat.location == stored.location
    assert object_stat.verified_at == "2026-07-13T12:00:00Z"

    wrong_locations = (
        stored.location.model_copy(update={"store_id": "local:other"}),
        stored.location.model_copy(update={"key": object_key_for_sha256("f" * 64)}),
        stored.location.model_copy(update={"backend_generation": "stale-generation"}),
    )
    for wrong in wrong_locations:
        with pytest.raises(FileNotFoundError):
            store.open(wrong)
        with pytest.raises(FileNotFoundError):
            store.stat(wrong)


def _all_versions(store: LocalObjectStore) -> tuple[ObjectStat, ...]:
    items: list[ObjectStat] = []
    cursor = None
    snapshot_id = None
    while True:
        page = store.list_versions(cursor)
        assert len(page.items) <= 2
        snapshot_id = snapshot_id or page.read_snapshot_id
        assert page.read_snapshot_id == snapshot_id
        items.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return tuple(items)


def test_list_versions_is_bounded_stably_sorted_and_snapshot_consistent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, page_size=2)
    initial = tuple(store.put_verified(payload) for payload in (b"third", b"first", b"second"))

    first_page = store.list_versions()
    assert len(first_page.items) == 2
    assert first_page.next_cursor is not None
    assert first_page.next_cursor.page_size == 2
    assert first_page.expires_at == "2026-07-13T12:05:00Z"

    late = store.put_verified(b"created after the read snapshot")
    second_page = store.list_versions(first_page.next_cursor)
    original_snapshot_items = first_page.items + second_page.items

    assert second_page.read_snapshot_id == first_page.read_snapshot_id
    assert second_page.next_cursor is None
    assert {item.location for item in original_snapshot_items} == {
        item.location for item in initial
    }
    assert late.location not in {item.location for item in original_snapshot_items}
    assert list(original_snapshot_items) == sorted(
        original_snapshot_items,
        key=lambda item: (item.location.key, item.location.backend_generation),
    )

    fresh_items = _all_versions(store)
    assert {item.location for item in fresh_items} == {
        *(item.location for item in initial),
        late.location,
    }


def test_first_list_snapshot_is_one_consistent_filesystem_state_across_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    early_payload = b"00-795"
    late_seed_payload = b"ff-89"
    late_new_payload = b"ff-104"
    controller = _store(tmp_path)
    early = controller.put_verified(early_payload)
    late_seed = controller.put_verified(late_seed_payload)
    assert early.ref.sha256.startswith("00")
    assert late_seed.ref.sha256.startswith("ff")
    assert hashlib.sha256(late_new_payload).hexdigest().startswith("ff")

    listing_store = _store(tmp_path)
    early_verified = threading.Event()
    continue_scan = threading.Event()
    original_verify = listing_store._verify_generation
    blocked = False

    def verify_and_block(key: str, generation: str) -> object:
        nonlocal blocked
        verified = original_verify(key, generation)
        if key == early.ref.key and not blocked:
            blocked = True
            early_verified.set()
            if not continue_scan.wait(timeout=5):
                raise TimeoutError("test did not release list materialization")
        return verified

    monkeypatch.setattr(listing_store, "_verify_generation", verify_and_block)
    listed: list[tuple[ObjectStat, ...]] = []
    failures: list[BaseException] = []

    def materialize() -> None:
        try:
            listed.append(_all_versions(listing_store))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = threading.Thread(target=materialize)
    thread.start()
    assert early_verified.wait(timeout=5)

    mutator = _store(tmp_path)
    assert mutator.delete_if_generation(early.location) is True
    late_new = mutator.put_verified(late_new_payload)
    continue_scan.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert failures == []
    assert len(listed) == 1
    observed = frozenset(item.location for item in listed[0])
    allowed_consistent_states = {
        frozenset((early.location, late_seed.location)),
        frozenset((late_seed.location,)),
        frozenset((late_seed.location, late_new.location)),
    }
    assert observed in allowed_consistent_states


class _BarrierRenameOps(_RecordingFileOps):
    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self._barrier = barrier

    def rename(self, source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        source_stat = source_path.stat()
        destination_parent_device = destination_path.parent.stat().st_dev
        self.events.append(
            (
                "rename",
                source_path,
                destination_path,
                source_stat.st_dev,
                destination_parent_device,
                source_stat.st_ino,
            )
        )
        self._barrier.wait(timeout=5)
        os.rename(source_path, destination_path)


def test_list_versions_enumerates_every_concurrently_completed_generation(
    tmp_path: Path,
) -> None:
    payload = b"two concurrent verified uploads may both complete"
    barrier = threading.Barrier(2)
    stores = (
        _store(tmp_path, file_ops=_BarrierRenameOps(barrier)),
        _store(tmp_path, file_ops=_BarrierRenameOps(barrier)),
    )
    results: list[StoredObject] = []
    failures: list[BaseException] = []

    def publish(store: LocalObjectStore) -> None:
        try:
            results.append(store.put_verified(payload))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = tuple(threading.Thread(target=publish, args=(store,)) for store in stores)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert len(results) == 2
    assert results[0].ref == results[1].ref
    assert results[0].location.backend_generation != results[1].location.backend_generation

    reopened = _store(tmp_path)
    retry = reopened.put_verified(payload)
    assert retry.location == min(
        (item.location for item in results),
        key=lambda location: (location.key, location.backend_generation),
    )

    listed = _all_versions(reopened)
    assert {item.location for item in listed} == {item.location for item in results}

    deleted, retained = results
    assert reopened.delete_if_generation(deleted.location) is True
    with reopened.open(retained.location) as opened:
        assert opened.read() == payload
    assert {item.location for item in _all_versions(reopened)} == {retained.location}


def test_delete_if_generation_is_exact_and_prevents_delete_recreate_aba(
    tmp_path: Path,
) -> None:
    payload = b"generation must change after delete and recreate"
    store = _store(tmp_path)
    first = store.put_verified(payload)
    stale = first.location.model_copy(update={"backend_generation": "not-current"})

    assert store.delete_if_generation(stale) is False
    with store.open(first.location) as opened:
        assert opened.read() == payload

    assert store.delete_if_generation(first.location) is True
    assert store.delete_if_generation(first.location) is False
    with pytest.raises(FileNotFoundError):
        store.open(first.location)
    with pytest.raises(FileNotFoundError):
        store.stat(first.location)

    recreated = store.put_verified(payload)
    assert recreated.ref == first.ref
    assert recreated.location.backend_generation != first.location.backend_generation
    assert store.delete_if_generation(first.location) is False
    with store.open(recreated.location) as opened:
        assert opened.read() == payload


def test_delete_if_generation_rejects_another_store_without_touching_object(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stored = store.put_verified(b"store identity is part of a concrete location")
    another_store = stored.location.model_copy(update={"store_id": "local:other"})

    assert store.delete_if_generation(another_store) is False
    assert store.stat(stored.location).location == stored.location


def test_delete_atomically_moves_only_the_exact_generation_and_fsyncs_its_parent(
    tmp_path: Path,
) -> None:
    file_ops = _RecordingFileOps()
    store = _store(tmp_path, file_ops=file_ops)
    stored = store.put_verified(b"delete one concrete generation")
    generation_directory = _generation_directory(tmp_path, stored)
    original_parent_stat = generation_directory.parent.stat()
    file_ops.events.clear()

    assert store.delete_if_generation(stored.location) is True

    rename_events = [event for event in file_ops.events if event[0] == "rename"]
    assert len(rename_events) == 1
    rename_event = rename_events[0]
    assert rename_event[1] == generation_directory
    assert Path(rename_event[2]).is_relative_to(tmp_path / ".trash")
    assert rename_event[3] == rename_event[4]
    rename_index = file_ops.events.index(rename_event)
    assert (
        "directory-fsync",
        original_parent_stat.st_dev,
        original_parent_stat.st_ino,
    ) in file_ops.events[rename_index + 1 :]
    assert not generation_directory.exists()


def test_publish_fsyncs_files_then_atomically_renames_on_same_filesystem_and_fsyncs_parent(
    tmp_path: Path,
) -> None:
    payload = b"durability-order-marker" * 8192
    digest = hashlib.sha256(payload).hexdigest()
    expected_key_directory = tmp_path / object_key_for_sha256(digest)
    file_ops = _RecordingFileOps()

    stored = _store(tmp_path, file_ops=file_ops).put_verified(payload)

    rename_indexes = [index for index, event in enumerate(file_ops.events) if event[0] == "rename"]
    assert len(rename_indexes) == 1
    rename_index = rename_indexes[0]
    rename_event = file_ops.events[rename_index]
    assert Path(rename_event[2]).parent == expected_key_directory
    assert rename_event[3] == rename_event[4]
    assert ("file-fsync", len(payload)) in file_ops.events[:rename_index]
    assert sum(event[0] == "file-fsync" for event in file_ops.events[:rename_index]) >= 2
    assert (
        "directory-fsync",
        rename_event[3],
        rename_event[5],
    ) in file_ops.events[:rename_index]

    expected_parent_stat = expected_key_directory.stat()
    assert (
        "directory-fsync",
        expected_parent_stat.st_dev,
        expected_parent_stat.st_ino,
    ) in file_ops.events[rename_index + 1 :]
    assert stored.ref.sha256 == digest
    assert _generation_directory(tmp_path, stored).joinpath("data").read_bytes() == payload


class _SimulatedCrash(BaseException):
    pass


class _CrashBeforeRenameOps(_RecordingFileOps):
    def rename(self, source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        self.events.append(("rename-crash", source_path, destination_path))
        raise _SimulatedCrash


class _TamperBeforePublishOps(_RecordingFileOps):
    def __init__(self, staging_root: Path) -> None:
        super().__init__()
        self._staging_root = staging_root

    def rename(self, source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if source_path.is_relative_to(self._staging_root):
            data_path = source_path / "data"
            original = data_path.read_bytes()
            data_path.write_bytes(b"X" * len(original))
        super().rename(source_path, destination)


def test_publish_detects_staging_tamper_without_exposing_a_final_generation(
    tmp_path: Path,
) -> None:
    store = _store(
        tmp_path,
        file_ops=_TamperBeforePublishOps(tmp_path / ".staging"),
    )

    with pytest.raises(IntegrityViolation):
        store.put_verified(b"verified bytes must not change before publication")

    assert store.list_versions().items == ()


class _FailParentFsyncAfterNamespaceRenameOps(_RecordingFileOps):
    def __init__(self) -> None:
        super().__init__()
        self.namespace_renamed = threading.Event()
        self.allow_fsync_failure = threading.Event()
        self._published_parent_identity: tuple[int, int] | None = None
        self._failure_injected = False

    def rename(self, source: str | Path, destination: str | Path) -> None:
        super().rename(source, destination)
        destination_parent_stat = Path(destination).parent.stat()
        self._published_parent_identity = (
            destination_parent_stat.st_dev,
            destination_parent_stat.st_ino,
        )
        self.namespace_renamed.set()

    def fsync(self, fd: int) -> None:
        descriptor_stat = os.fstat(fd)
        identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
        if (
            not self._failure_injected
            and self._published_parent_identity is not None
            and identity == self._published_parent_identity
        ):
            self._failure_injected = True
            if not self.allow_fsync_failure.wait(timeout=5):
                raise TimeoutError("test did not release the post-rename fsync failure")
            raise OSError("injected parent fsync failure after namespace rename")
        super().fsync(fd)


def test_failed_post_rename_fsync_cannot_validate_an_impossible_cross_shard_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    early_payload = b"00-795"
    late_payload = b"ff-104"
    early = _store(tmp_path).put_verified(early_payload)
    assert early.ref.sha256.startswith("00")
    assert hashlib.sha256(late_payload).hexdigest().startswith("ff")

    failing_ops = _FailParentFsyncAfterNamespaceRenameOps()
    put_store = _store(tmp_path, file_ops=failing_ops)
    listing_store = _store(tmp_path)
    scan_completed = threading.Event()
    original_scan = listing_store._scan_versions

    def scan_then_signal() -> tuple[ObjectStat, ...]:
        items = original_scan()
        scan_completed.set()
        return items

    monkeypatch.setattr(listing_store, "_scan_versions", scan_then_signal)
    put_results: list[StoredObject] = []
    put_failures: list[BaseException] = []
    list_results: list[tuple[ObjectStat, ...]] = []
    list_failures: list[BaseException] = []

    def publish_late() -> None:
        try:
            put_results.append(put_store.put_verified(late_payload))
        except BaseException as exc:  # pragma: no cover - asserted below
            put_failures.append(exc)

    def materialize() -> None:
        try:
            list_results.append(_all_versions(listing_store))
        except BaseException as exc:  # pragma: no cover - asserted below
            list_failures.append(exc)

    put_thread = threading.Thread(target=publish_late)
    put_thread.start()
    assert failing_ops.namespace_renamed.wait(timeout=5)

    list_thread = threading.Thread(target=materialize)
    list_thread.start()
    assert scan_completed.wait(timeout=5)
    failing_ops.allow_fsync_failure.set()
    put_thread.join(timeout=10)
    list_thread.join(timeout=10)

    assert not put_thread.is_alive()
    assert not list_thread.is_alive()
    assert put_results == []
    assert len(put_failures) == 1
    assert isinstance(put_failures[0], OSError)
    if list_failures:
        assert list_results == []
        assert len(list_failures) == 1
        assert isinstance(list_failures[0], IntegrityViolation)
    else:
        assert len(list_results) == 1
        assert {item.location for item in list_results[0]} == {early.location}


def test_crash_staging_is_invisible_and_is_not_cleaned_by_object_store(
    tmp_path: Path,
) -> None:
    payload = b"verified but not atomically published"
    digest = hashlib.sha256(payload).hexdigest()
    crash_ops = _CrashBeforeRenameOps()
    store = _store(tmp_path, file_ops=crash_ops)

    with pytest.raises(_SimulatedCrash):
        store.put_verified(payload)

    expected_key_directory = tmp_path / object_key_for_sha256(digest)
    staging_root = tmp_path / ".staging"
    staged_files_before = tuple(sorted(path for path in staging_root.rglob("*") if path.is_file()))
    crash_event = next(event for event in crash_ops.events if event[0] == "rename-crash")
    assert Path(crash_event[2]).parent == expected_key_directory
    assert not Path(crash_event[2]).exists()
    assert staged_files_before
    assert any(path.read_bytes() == payload for path in staged_files_before)
    assert store.list_versions().items == ()

    reopened = _store(tmp_path)
    assert reopened.list_versions().items == ()
    staged_files_after = tuple(sorted(path for path in staging_root.rglob("*") if path.is_file()))
    assert staged_files_after == staged_files_before


def test_list_versions_never_enumerates_manual_staging_leftovers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    completed = store.put_verified(b"completed")
    orphan = tmp_path / ".staging" / "crashed-upload"
    orphan.mkdir(parents=True)
    orphan.joinpath("data").write_bytes(b"not published")
    orphan.joinpath("metadata.json").write_text("{}", encoding="utf-8")

    items = _all_versions(store)

    assert tuple(item.location for item in items) == (completed.location,)
    assert orphan.is_dir()


def test_corrupt_published_data_fails_stat_and_open_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = store.put_verified(b"verified payload")
    _generation_directory(tmp_path, stored).joinpath("data").write_bytes(
        b"corrupted after publication"
    )

    with pytest.raises(IntegrityViolation):
        store.stat(stored.location)
    with pytest.raises(IntegrityViolation):
        store.open(stored.location)


def test_metadata_symlink_is_always_an_integrity_violation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = store.put_verified(b"metadata must be a regular file in its generation")
    metadata_path = _generation_directory(tmp_path, stored) / "metadata.json"
    external_metadata = tmp_path / "external-metadata.json"
    external_metadata.write_bytes(metadata_path.read_bytes())
    metadata_path.unlink()
    metadata_path.symlink_to(external_metadata)

    with pytest.raises(IntegrityViolation):
        store.stat(stored.location)


def test_open_returns_a_binary_stream(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored = store.put_verified(b"binary")

    opened: BinaryIO = store.open(stored.location)
    try:
        assert opened.read() == b"binary"
    finally:
        opened.close()


def test_unknown_content_addressed_location_is_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    digest = "0" * 64
    unknown = ObjectLocation(
        store_id=_STORE_ID,
        key=object_key_for_sha256(digest),
        backend_generation="missing-generation",
    )

    with pytest.raises(FileNotFoundError):
        store.open(unknown)
    with pytest.raises(FileNotFoundError):
        store.stat(unknown)
    assert store.delete_if_generation(unknown) is False
