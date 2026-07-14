"""Durable local immutable object storage with concrete backend generations."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import shutil
import stat as stat_module
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import BinaryIO, Iterator, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import CursorExpired, CursorInvalid, IntegrityViolation
from gameforge.contracts.lineage import ObjectLocation, ObjectRef, object_key_for_sha256
from gameforge.contracts.storage import (
    MAX_PAGE_ITEMS,
    ObjectStat,
    PageCursorV1,
    PageV1,
    ReadSnapshotV1,
    StoredObject,
    UtcClock,
    compute_page_query_hash,
)
from gameforge.runtime.persistence.cursor import CursorSigner


_CHUNK_SIZE = 1024 * 1024
_GENERATION_PATTERN = re.compile(r"local-[0-9a-f]{32}")
_KEY_PATTERN = re.compile(r"objects/v1/sha256/([0-9a-f]{2})/([0-9a-f]{64})")


@dataclass(frozen=True, slots=True)
class LocalFileOps:
    """The two durability operations injected by deterministic failure tests."""

    fsync: Callable[[int], None] = os.fsync
    rename: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.rename


class _LocalObjectMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metadata_schema_version: Literal["local-object-metadata@1"] = "local-object-metadata@1"
    store_id: str
    key: str
    backend_generation: str
    sha256: str
    size_bytes: int
    verified_at: str


@dataclass(frozen=True, slots=True)
class _VerifiedGeneration:
    metadata: _LocalObjectMetadata
    ref: ObjectRef
    location: ObjectLocation
    directory: Path

    def stored_object(self) -> StoredObject:
        return StoredObject(ref=self.ref, location=self.location)

    def object_stat(self) -> ObjectStat:
        return ObjectStat(
            ref=self.ref,
            location=self.location,
            verified_at=self.metadata.verified_at,
        )


@dataclass(frozen=True, slots=True)
class _ListSnapshot:
    record: ReadSnapshotV1
    items: tuple[ObjectStat, ...]
    expires_at: datetime


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("clock must provide a timezone-aware UTC timestamp")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_utc_text(value: str) -> None:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamp must be valid UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be valid UTC")


def _write_all(destination: BinaryIO, chunk: bytes | bytearray | memoryview) -> None:
    remaining = memoryview(chunk)
    while remaining:
        written = destination.write(remaining)
        if written is None or written <= 0:
            raise OSError("object-store write made no progress")
        remaining = remaining[written:]


class LocalObjectStore:
    """Content-addressed local blobs published as immutable generation directories."""

    def __init__(
        self,
        root: Path,
        *,
        store_id: str,
        clock: UtcClock,
        cursor_signing_key: bytes,
        page_size: int = 100,
        snapshot_ttl: timedelta = timedelta(minutes=5),
        file_ops: LocalFileOps | None = None,
    ) -> None:
        if not store_id:
            raise ValueError("store_id must be non-empty")
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_ITEMS:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_ITEMS}")
        if snapshot_ttl <= timedelta(0):
            raise ValueError("snapshot_ttl must be positive")

        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir():
            raise NotADirectoryError(self._root)
        self._file_ops = file_ops if file_ops is not None else LocalFileOps()
        self._staging_root = self._root / ".staging"
        self._trash_root = self._root / ".trash"
        self._locks_root = self._root / ".locks"
        self._ensure_directory(self._staging_root)
        self._ensure_directory(self._trash_root)
        self._ensure_directory(self._locks_root)
        self._initialization_lock_path = self._locks_root / "initialization.lock"
        self._mutation_lock_path = self._locks_root / "mutations.lock"
        self._catalog_token_path = self._locks_root / "catalog.token"
        self._initialize_coordination_files()

        self._store_id = store_id
        self._clock = clock
        self._page_size = page_size
        self._snapshot_ttl = snapshot_ttl
        self._cursor_signer = CursorSigner(
            signing_key=cursor_signing_key,
            clock=clock,
        )
        self._query_hash = compute_page_query_hash(
            api_version="storage@1",
            resource_kind="object_versions",
            filters={"store_id": store_id},
            stable_sort=("key:asc", "backend_generation:asc"),
            page_projection=("ref", "location", "verified_at", "retention_until"),
        )
        self._authz_fingerprint = canonical_sha256(
            {"scope": "local-object-store-internal", "store_id": store_id}
        )
        self._snapshots: dict[str, _ListSnapshot] = {}
        self._snapshot_lock = RLock()

    def check_ready(self) -> None:
        """Validate local coordination state without scanning or mutating blobs."""

        for directory in (
            self._root,
            self._staging_root,
            self._trash_root,
            self._locks_root,
        ):
            try:
                directory_stat = directory.lstat()
            except FileNotFoundError as exc:
                raise IntegrityViolation(
                    "object-store readiness directory is missing",
                    path=str(directory),
                ) from exc
            if not stat_module.S_ISDIR(directory_stat.st_mode) or stat_module.S_ISLNK(
                directory_stat.st_mode
            ):
                raise IntegrityViolation(
                    "object-store readiness path is not a regular directory",
                    path=str(directory),
                )
        for path in (self._initialization_lock_path, self._mutation_lock_path):
            try:
                path_stat = path.lstat()
            except FileNotFoundError as exc:
                raise IntegrityViolation(
                    "object-store coordination file is missing",
                    path=str(path),
                ) from exc
            if not stat_module.S_ISREG(path_stat.st_mode) or stat_module.S_ISLNK(path_stat.st_mode):
                raise IntegrityViolation(
                    "object-store coordination path is not a regular file",
                    path=str(path),
                )
        with self._coordination_lock(fcntl.LOCK_SH):
            self._read_catalog_token()

    def put_verified(self, source: bytes | BinaryIO) -> StoredObject:
        staging_directory = self._new_staging_directory("put")
        data_path = staging_directory / "data"
        digest = hashlib.sha256()
        size_bytes = 0

        with data_path.open("xb") as destination:
            if isinstance(source, bytes):
                chunks = (source,)
                for chunk in chunks:
                    _write_all(destination, chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
            else:
                read = getattr(source, "read", None)
                if not callable(read):
                    raise TypeError("source must be bytes or a readable binary stream")
                while True:
                    chunk = read(_CHUNK_SIZE)
                    if chunk == b"":
                        break
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise TypeError("binary stream read() must return bytes")
                    _write_all(destination, chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
            destination.flush()
            self._file_ops.fsync(destination.fileno())

        digest_hex = digest.hexdigest()
        ref = ObjectRef(
            key=object_key_for_sha256(digest_hex),
            sha256=digest_hex,
            size_bytes=size_bytes,
        )
        generation = f"local-{uuid.uuid4().hex}"
        verified_at = _utc_text(self._clock.now_utc())
        metadata = _LocalObjectMetadata(
            store_id=self._store_id,
            key=ref.key,
            backend_generation=generation,
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
            verified_at=verified_at,
        )
        metadata_bytes = canonical_json(metadata.model_dump(mode="json")).encode("utf-8")
        with (staging_directory / "metadata.json").open("xb") as metadata_file:
            _write_all(metadata_file, metadata_bytes)
            metadata_file.flush()
            self._file_ops.fsync(metadata_file.fileno())
        self._fsync_directory(staging_directory)

        staged = self._verify_object_directory(
            staging_directory,
            expected_key=ref.key,
            expected_generation=generation,
        )
        if staged.ref != ref:
            raise IntegrityViolation(
                "staged object differs from the streamed content",
                key=ref.key,
                backend_generation=generation,
            )

        key_directory = self._key_directory(ref.key)
        self._ensure_directory(key_directory)
        with self._mutation_guard():
            try:
                existing = self._verified_generations_for_ref(ref)
            except FileNotFoundError:
                existing = ()
            if existing:
                with self._key_guard(ref.key):
                    try:
                        current_existing = self._verified_generations_for_ref(ref)
                    except FileNotFoundError:
                        current_existing = ()
                    for candidate in sorted(
                        current_existing,
                        key=lambda item: (
                            item.location.key,
                            item.location.backend_generation,
                        ),
                    ):
                        try:
                            current = self._verify_generation(
                                candidate.location.key,
                                candidate.location.backend_generation,
                            )
                            current = self._verify_object_directory(
                                current.directory,
                                expected_key=current.ref.key,
                                expected_generation=current.location.backend_generation,
                            )
                        except FileNotFoundError:
                            continue
                        if current.ref != ref:
                            raise IntegrityViolation(
                                "content-addressed object differs from the requested bytes",
                                key=ref.key,
                                backend_generation=current.location.backend_generation,
                            )
                        self._remove_tree_best_effort(staging_directory)
                        return current.stored_object()

            target_directory = key_directory / generation
            try:
                self._file_ops.rename(staging_directory, target_directory)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                winner = self._verify_generation(ref.key, generation)
                if winner.ref != ref:
                    raise IntegrityViolation(
                        "concurrent object generation has different content",
                        key=ref.key,
                        backend_generation=generation,
                    ) from exc
                self._remove_tree_best_effort(staging_directory)
                return winner.stored_object()

            try:
                self._fsync_directory(key_directory)
                published = self._verify_generation(ref.key, generation)
            except BaseException:
                self._quarantine_generation(target_directory)
                raise
            return published.stored_object()

    def open(self, location: ObjectLocation) -> BinaryIO:
        verified = self._verified_for_location(location)
        try:
            return (verified.directory / "data").open("rb")
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise IntegrityViolation(
                "verified object data cannot be opened",
                key=location.key,
                backend_generation=location.backend_generation,
            ) from exc

    def stat(self, location: ObjectLocation) -> ObjectStat:
        return self._verified_for_location(location).object_stat()

    def list_versions(self, cursor: PageCursorV1 | None = None) -> PageV1[ObjectStat]:
        if cursor is None:
            snapshot = self._create_list_snapshot()
            position = 0
        else:
            self._cursor_signer.verify_signature(cursor)
            snapshot = self._get_list_snapshot(cursor.snapshot_id)
            self._cursor_signer.verify(
                cursor,
                expected_snapshot=snapshot.record,
                expected_query_hash=self._query_hash,
                requested_page_size=self._page_size,
                snapshot_is_retained=self._snapshot_is_retained,
            )
            try:
                position = int(cursor.position)
            except ValueError as exc:
                raise CursorInvalid("object-store cursor position is invalid") from exc
            if position < 0 or position > len(snapshot.items):
                raise CursorInvalid("object-store cursor position is out of range")

        end = min(position + self._page_size, len(snapshot.items))
        next_cursor = None
        if end < len(snapshot.items):
            next_cursor = self._cursor_signer.issue(
                snapshot=snapshot.record,
                position=str(end),
                page_size=self._page_size,
            )
        return PageV1[ObjectStat](
            read_snapshot_id=snapshot.record.snapshot_id,
            items=snapshot.items[position:end],
            next_cursor=next_cursor,
            expires_at=snapshot.record.expires_at,
        )

    def delete_if_generation(self, location: ObjectLocation) -> bool:
        if location.store_id != self._store_id:
            return False
        with self._mutation_guard():
            with self._key_guard(location.key):
                try:
                    self._validate_generation(location.backend_generation)
                    verified = self._verify_generation(
                        location.key,
                        location.backend_generation,
                    )
                except FileNotFoundError:
                    return False

                trash_directory = self._trash_root / (
                    f"{location.backend_generation}-{uuid.uuid4().hex}"
                )
                try:
                    self._file_ops.rename(verified.directory, trash_directory)
                except FileNotFoundError:
                    return False
                self._fsync_directory(verified.directory.parent)
                self._fsync_directory(self._trash_root)
                self._remove_tree_best_effort(trash_directory)
                self._fsync_directory(self._trash_root)
                return True

    def _create_list_snapshot(self) -> _ListSnapshot:
        items = self._materialize_consistent_versions()
        now = self._clock.now_utc()
        created_at = _utc_text(now)
        expires_at_value = now.astimezone(timezone.utc) + self._snapshot_ttl
        expires_at = _utc_text(expires_at_value)
        snapshot_id = f"local-object-list:{uuid.uuid4().hex}"
        record = ReadSnapshotV1(
            snapshot_id=snapshot_id,
            resource_kind="object_versions",
            query_hash=self._query_hash,
            authz_fingerprint=self._authz_fingerprint,
            stable_sort_schema_id="local-object-key-generation@1",
            strategy="materialized_view",
            materialized_item_count=len(items),
            created_at=created_at,
            expires_at=expires_at,
        )
        snapshot = _ListSnapshot(
            record=record,
            items=items,
            expires_at=expires_at_value,
        )
        with self._snapshot_lock:
            self._prune_snapshots(now)
            self._snapshots[snapshot_id] = snapshot
        return snapshot

    def _materialize_consistent_versions(self) -> tuple[ObjectStat, ...]:
        while True:
            token_before = self._read_catalog_token()
            scan_error: FileNotFoundError | IntegrityViolation | None = None
            try:
                items = tuple(
                    sorted(
                        self._scan_versions(),
                        key=lambda item: (
                            item.location.key,
                            item.location.backend_generation,
                        ),
                    )
                )
            except (FileNotFoundError, IntegrityViolation) as exc:
                items = ()
                scan_error = exc
            with self._catalog_validation_guard():
                token_after = self._read_catalog_token()
            if token_before != token_after:
                continue
            if scan_error is not None:
                raise scan_error
            return items

    def _get_list_snapshot(self, snapshot_id: str) -> _ListSnapshot:
        now = self._clock.now_utc()
        _utc_text(now)
        with self._snapshot_lock:
            self._prune_snapshots(now)
            snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None:
            raise CursorExpired("object-store read snapshot is no longer retained")
        return snapshot

    def _snapshot_is_retained(self, snapshot_id: str) -> bool:
        with self._snapshot_lock:
            return snapshot_id in self._snapshots

    def _prune_snapshots(self, now: datetime) -> None:
        expired = [
            snapshot_id
            for snapshot_id, snapshot in self._snapshots.items()
            if now >= snapshot.expires_at
        ]
        for snapshot_id in expired:
            del self._snapshots[snapshot_id]

    def _scan_versions(self) -> tuple[ObjectStat, ...]:
        sha_root = self._root / "objects" / "v1" / "sha256"
        if not self._validate_directory_chain(sha_root, missing_ok=True):
            return ()
        items: list[ObjectStat] = []
        for shard_directory in sorted(sha_root.iterdir(), key=lambda path: path.name):
            if not re.fullmatch(r"[0-9a-f]{2}", shard_directory.name):
                continue
            if not shard_directory.is_dir() or shard_directory.is_symlink():
                raise IntegrityViolation(
                    "object shard path is not a regular directory",
                    path=str(shard_directory),
                )
            for key_directory in sorted(shard_directory.iterdir(), key=lambda path: path.name):
                key = f"objects/v1/sha256/{shard_directory.name}/{key_directory.name}"
                try:
                    self._validate_key(key)
                except FileNotFoundError:
                    continue
                if not key_directory.is_dir() or key_directory.is_symlink():
                    raise IntegrityViolation("object key path is not a regular directory", key=key)
                for generation_directory in sorted(
                    key_directory.iterdir(), key=lambda path: path.name
                ):
                    if generation_directory.name.startswith("."):
                        continue
                    if (
                        not generation_directory.is_dir()
                        or generation_directory.is_symlink()
                        or _GENERATION_PATTERN.fullmatch(generation_directory.name) is None
                    ):
                        raise IntegrityViolation(
                            "published object generation path is invalid",
                            key=key,
                            backend_generation=generation_directory.name,
                        )
                    items.append(
                        self._verify_generation(
                            key,
                            generation_directory.name,
                        ).object_stat()
                    )
        return tuple(items)

    def _verified_generations_for_ref(
        self,
        ref: ObjectRef,
    ) -> tuple[_VerifiedGeneration, ...]:
        key_directory = self._key_directory(ref.key)
        if not self._validate_directory_chain(key_directory, missing_ok=True):
            return ()
        verified: list[_VerifiedGeneration] = []
        for generation_directory in sorted(key_directory.iterdir(), key=lambda path: path.name):
            if generation_directory.name.startswith("."):
                continue
            if (
                not generation_directory.is_dir()
                or generation_directory.is_symlink()
                or _GENERATION_PATTERN.fullmatch(generation_directory.name) is None
            ):
                raise IntegrityViolation(
                    "published object generation path is invalid",
                    key=ref.key,
                    backend_generation=generation_directory.name,
                )
            item = self._verify_generation(ref.key, generation_directory.name)
            if item.ref != ref:
                raise IntegrityViolation(
                    "content-addressed object differs from the requested bytes",
                    key=ref.key,
                    backend_generation=generation_directory.name,
                )
            verified.append(item)
        return tuple(verified)

    def _verified_for_location(self, location: ObjectLocation) -> _VerifiedGeneration:
        if location.store_id != self._store_id:
            raise FileNotFoundError("object location belongs to another store")
        self._validate_key(location.key)
        self._validate_generation(location.backend_generation)
        return self._verify_generation(location.key, location.backend_generation)

    def _verify_generation(self, key: str, generation: str) -> _VerifiedGeneration:
        self._validate_key(key)
        self._validate_generation(generation)
        key_directory = self._key_directory(key)
        if not self._validate_directory_chain(key_directory, missing_ok=True):
            raise FileNotFoundError("object key does not exist")
        return self._verify_object_directory(
            key_directory / generation,
            expected_key=key,
            expected_generation=generation,
        )

    def _verify_object_directory(
        self,
        directory: Path,
        *,
        expected_key: str,
        expected_generation: str,
    ) -> _VerifiedGeneration:
        self._validate_key(expected_key)
        self._validate_generation(expected_generation)
        try:
            directory_stat = directory.lstat()
        except FileNotFoundError:
            raise
        if not stat_module.S_ISDIR(directory_stat.st_mode) or stat_module.S_ISLNK(
            directory_stat.st_mode
        ):
            raise IntegrityViolation(
                "object generation is not a regular directory",
                key=expected_key,
                backend_generation=expected_generation,
            )

        metadata_path = directory / "metadata.json"
        data_path = directory / "data"
        try:
            metadata_stat = metadata_path.lstat()
        except FileNotFoundError as exc:
            raise IntegrityViolation(
                "published object metadata is missing",
                key=expected_key,
                backend_generation=expected_generation,
            ) from exc
        if not stat_module.S_ISREG(metadata_stat.st_mode) or stat_module.S_ISLNK(
            metadata_stat.st_mode
        ):
            raise IntegrityViolation(
                "published object metadata is not a regular file",
                key=expected_key,
                backend_generation=expected_generation,
            )
        try:
            metadata_bytes = metadata_path.read_bytes()
            metadata = _LocalObjectMetadata.model_validate_json(metadata_bytes)
        except FileNotFoundError as exc:
            raise IntegrityViolation(
                "published object metadata is missing",
                key=expected_key,
                backend_generation=expected_generation,
            ) from exc
        except (OSError, ValidationError, UnicodeError) as exc:
            raise IntegrityViolation(
                "published object metadata is invalid",
                key=expected_key,
                backend_generation=expected_generation,
            ) from exc
        expected_metadata = canonical_json(metadata.model_dump(mode="json")).encode("utf-8")
        if metadata_bytes != expected_metadata:
            raise IntegrityViolation(
                "published object metadata is not canonical",
                key=expected_key,
                backend_generation=expected_generation,
            )
        if (
            metadata.store_id != self._store_id
            or metadata.key != expected_key
            or metadata.backend_generation != expected_generation
        ):
            raise IntegrityViolation(
                "published object metadata identity does not match its path",
                key=expected_key,
                backend_generation=expected_generation,
            )
        try:
            _require_utc_text(metadata.verified_at)
        except ValueError as exc:
            raise IntegrityViolation(
                "published object verification timestamp is invalid",
                key=expected_key,
                backend_generation=expected_generation,
            ) from exc

        digest_from_key = self._validate_key(expected_key)
        try:
            data_stat = data_path.lstat()
        except FileNotFoundError as exc:
            raise IntegrityViolation(
                "published object data is missing",
                key=expected_key,
                backend_generation=expected_generation,
            ) from exc
        if not stat_module.S_ISREG(data_stat.st_mode) or stat_module.S_ISLNK(data_stat.st_mode):
            raise IntegrityViolation(
                "published object data is not a regular file",
                key=expected_key,
                backend_generation=expected_generation,
            )

        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with data_path.open("rb") as data_file:
                while True:
                    chunk = data_file.read(_CHUNK_SIZE)
                    if chunk == b"":
                        break
                    digest.update(chunk)
                    size_bytes += len(chunk)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise IntegrityViolation(
                "published object data cannot be verified",
                key=expected_key,
                backend_generation=expected_generation,
            ) from exc
        digest_hex = digest.hexdigest()
        if (
            digest_hex != digest_from_key
            or metadata.sha256 != digest_hex
            or metadata.size_bytes != size_bytes
        ):
            raise IntegrityViolation(
                "published object hash or size does not match metadata",
                key=expected_key,
                backend_generation=expected_generation,
            )
        ref = ObjectRef(key=expected_key, sha256=digest_hex, size_bytes=size_bytes)
        location = ObjectLocation(
            store_id=self._store_id,
            key=expected_key,
            backend_generation=expected_generation,
            etag=digest_hex,
            storage_class="local",
        )
        return _VerifiedGeneration(
            metadata=metadata,
            ref=ref,
            location=location,
            directory=directory,
        )

    def _initialize_coordination_files(self) -> None:
        self._create_coordination_file(self._initialization_lock_path, b"")
        with self._file_lock(
            self._initialization_lock_path,
            "object-store initialization lock",
            fcntl.LOCK_EX,
        ):
            self._create_coordination_file(self._mutation_lock_path, b"")
            self._create_coordination_file(
                self._catalog_token_path,
                b"catalog-token@1:initial\n",
            )
            self._read_catalog_token()

    def _create_coordination_file(self, path: Path, payload: bytes) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            path_stat = path.lstat()
            if not stat_module.S_ISREG(path_stat.st_mode) or stat_module.S_ISLNK(path_stat.st_mode):
                raise IntegrityViolation(
                    "object-store coordination path is not a regular file",
                    path=str(path),
                )
            return
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                _write_all(output, payload)
                output.flush()
                self._file_ops.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._fsync_directory(path.parent)

    @contextmanager
    def _mutation_guard(self) -> Iterator[None]:
        with self._coordination_lock(fcntl.LOCK_SH):
            self._advance_catalog_token()
            try:
                yield
            finally:
                self._advance_catalog_token()

    @contextmanager
    def _key_guard(self, key: str) -> Iterator[None]:
        digest = self._validate_key(key)
        lock_path = self._locks_root / f"key-{digest}.lock"
        self._create_coordination_file(lock_path, b"")
        with self._file_lock(
            lock_path,
            f"object-store key lock for {key}",
            fcntl.LOCK_EX,
        ):
            yield

    @contextmanager
    def _catalog_validation_guard(self) -> Iterator[None]:
        with self._coordination_lock(fcntl.LOCK_EX):
            yield

    @contextmanager
    def _coordination_lock(self, operation: int) -> Iterator[None]:
        with self._file_lock(
            self._mutation_lock_path,
            "object-store mutation lock",
            operation,
        ):
            yield

    @contextmanager
    def _file_lock(
        self,
        path: Path,
        label: str,
        operation: int,
    ) -> Iterator[None]:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            lock_stat = os.fstat(descriptor)
            if not stat_module.S_ISREG(lock_stat.st_mode):
                raise IntegrityViolation(f"{label} is not a regular file")
            fcntl.flock(descriptor, operation)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _read_catalog_token(self) -> bytes:
        try:
            token_stat = self._catalog_token_path.lstat()
        except FileNotFoundError as exc:
            raise IntegrityViolation("object-store catalog token is missing") from exc
        if not stat_module.S_ISREG(token_stat.st_mode) or stat_module.S_ISLNK(token_stat.st_mode):
            raise IntegrityViolation("object-store catalog token is not a regular file")
        token = self._catalog_token_path.read_bytes()
        if not token.startswith(b"catalog-token@1:") or not token.endswith(b"\n"):
            raise IntegrityViolation("object-store catalog token is invalid")
        return token

    def _advance_catalog_token(self) -> None:
        temporary = self._locks_root / f".catalog-{uuid.uuid4().hex}.tmp"
        token = f"catalog-token@1:{uuid.uuid4().hex}\n".encode("ascii")
        with temporary.open("xb") as output:
            _write_all(output, token)
            output.flush()
            self._file_ops.fsync(output.fileno())
        os.replace(temporary, self._catalog_token_path)
        self._fsync_directory(self._locks_root)

    def _quarantine_generation(self, directory: Path) -> None:
        trash_directory = self._trash_root / f"invalid-{uuid.uuid4().hex}"
        try:
            self._file_ops.rename(directory, trash_directory)
        except FileNotFoundError:
            return
        self._fsync_directory(directory.parent)
        self._fsync_directory(self._trash_root)
        self._remove_tree_best_effort(trash_directory)
        self._fsync_directory(self._trash_root)

    def _new_staging_directory(self, operation: str) -> Path:
        while True:
            path = self._staging_root / f"{operation}-{uuid.uuid4().hex}.tmp"
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                continue
            return path

    def _ensure_directory(self, directory: Path) -> None:
        current = self._root
        root_stat = current.lstat()
        if not stat_module.S_ISDIR(root_stat.st_mode) or stat_module.S_ISLNK(root_stat.st_mode):
            raise IntegrityViolation("object-store root is not a regular directory")
        for component in directory.relative_to(self._root).parts:
            path = current / component
            try:
                path_stat = path.lstat()
            except FileNotFoundError:
                try:
                    path.mkdir()
                except FileExistsError:
                    path_stat = path.lstat()
                else:
                    self._fsync_directory(current)
                    path_stat = path.lstat()
            if not stat_module.S_ISDIR(path_stat.st_mode) or stat_module.S_ISLNK(path_stat.st_mode):
                raise IntegrityViolation(
                    "object-store path component is not a regular directory",
                    path=str(path),
                )
            current = path

    def _validate_directory_chain(self, directory: Path, *, missing_ok: bool) -> bool:
        current = self._root
        root_stat = current.lstat()
        if not stat_module.S_ISDIR(root_stat.st_mode) or stat_module.S_ISLNK(root_stat.st_mode):
            raise IntegrityViolation("object-store root is not a regular directory")
        for component in directory.relative_to(self._root).parts:
            current /= component
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                if missing_ok:
                    return False
                raise
            if not stat_module.S_ISDIR(current_stat.st_mode) or stat_module.S_ISLNK(
                current_stat.st_mode
            ):
                raise IntegrityViolation(
                    "object-store path component is not a regular directory",
                    path=str(current),
                )
        return True

    def _key_directory(self, key: str) -> Path:
        self._validate_key(key)
        return self._root.joinpath(*key.split("/"))

    @staticmethod
    def _validate_key(key: str) -> str:
        match = _KEY_PATTERN.fullmatch(key)
        if match is None or match.group(1) != match.group(2)[:2]:
            raise FileNotFoundError("object location key is invalid")
        if object_key_for_sha256(match.group(2)) != key:
            raise FileNotFoundError("object location key is invalid")
        return match.group(2)

    @staticmethod
    def _validate_generation(generation: str) -> None:
        if _GENERATION_PATTERN.fullmatch(generation) is None:
            raise FileNotFoundError("object backend generation is invalid")

    def _fsync_directory(self, directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(directory, flags)
        try:
            self._file_ops.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove_tree_best_effort(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError:
            return
