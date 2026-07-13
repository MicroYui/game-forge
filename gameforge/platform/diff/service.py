"""Repository-loading facade for bounded snapshot-diff reads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from gameforge.contracts.diff import SnapshotDiff, SnapshotDiffEntry
from gameforge.contracts.storage import MAX_PAGE_ITEMS
from gameforge.platform.diff.engine import (
    CollectionIdentityInput,
    _is_json_pointer,
    _normalize_identities,
    iter_snapshot_diff_entries,
)


@runtime_checkable
class SnapshotCanonicalViewRepository(Protocol):
    """Narrow read capability; adapters decide how immutable views are stored."""

    def load_canonical_view(self, snapshot_id: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class SnapshotDiffSlice:
    """One bounded keyset slice; transport adapters wrap positions in signed cursors."""

    diff: SnapshotDiff
    after_path: str | None
    entries: tuple[SnapshotDiffEntry, ...]
    next_after_path: str | None


class SnapshotDiffService:
    def __init__(
        self,
        repository: SnapshotCanonicalViewRepository,
        *,
        collection_identities: CollectionIdentityInput = (),
    ) -> None:
        self._repository = repository
        identities = _normalize_identities(collection_identities)
        self._collection_identities = tuple(identities[path] for path in sorted(identities))

    def _load_pair(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if not isinstance(base_snapshot_id, str) or not base_snapshot_id:
            raise ValueError("base_snapshot_id must be a non-empty string")
        if not isinstance(target_snapshot_id, str) or not target_snapshot_id:
            raise ValueError("target_snapshot_id must be a non-empty string")
        base = self._repository.load_canonical_view(base_snapshot_id)
        if base is None:
            raise KeyError(base_snapshot_id)
        target = self._repository.load_canonical_view(target_snapshot_id)
        if target is None:
            raise KeyError(target_snapshot_id)
        return base, target

    def _entries(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
    ):
        base, target = self._load_pair(base_snapshot_id, target_snapshot_id)
        return iter_snapshot_diff_entries(
            base,
            target,
            collection_identities=self._collection_identities,
        )

    def diff_snapshots(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
    ) -> SnapshotDiff:
        entry_count = sum(1 for _ in self._entries(base_snapshot_id, target_snapshot_id))
        return SnapshotDiff(
            base_snapshot_id=base_snapshot_id,
            target_snapshot_id=target_snapshot_id,
            entry_count=entry_count,
        )

    def page_entries(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
        *,
        after_path: str | None,
        limit: int,
    ) -> SnapshotDiffSlice:
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_ITEMS:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_ITEMS}")
        if after_path is not None and (
            not isinstance(after_path, str) or not _is_json_pointer(after_path)
        ):
            raise ValueError("after_path must be an RFC 6901 JSON Pointer")

        page: list[SnapshotDiffEntry] = []
        has_more = False
        entry_count = 0
        for entry in self._entries(base_snapshot_id, target_snapshot_id):
            entry_count += 1
            if after_path is not None and entry.path <= after_path:
                continue
            if len(page) < limit:
                page.append(entry)
            else:
                has_more = True

        diff = SnapshotDiff(
            base_snapshot_id=base_snapshot_id,
            target_snapshot_id=target_snapshot_id,
            entry_count=entry_count,
        )
        return SnapshotDiffSlice(
            diff=diff,
            after_path=after_path,
            entries=tuple(page),
            next_after_path=page[-1].path if has_more else None,
        )


__all__ = [
    "SnapshotCanonicalViewRepository",
    "SnapshotDiffService",
    "SnapshotDiffSlice",
]
