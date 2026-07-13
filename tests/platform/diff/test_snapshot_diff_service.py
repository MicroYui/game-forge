from __future__ import annotations

from collections.abc import Mapping

import pytest

from gameforge.contracts.storage import MAX_PAGE_ITEMS
from gameforge.platform.diff import CollectionIdentity, SnapshotDiffService


class FakeSnapshotRepository:
    def __init__(self, views: dict[str, Mapping[str, object]]) -> None:
        self.views = views
        self.loads: list[str] = []

    def load_canonical_view(self, snapshot_id: str) -> Mapping[str, object] | None:
        self.loads.append(snapshot_id)
        return self.views.get(snapshot_id)


def test_repository_facade_loads_by_id_and_returns_only_bounded_stable_pages() -> None:
    repository = FakeSnapshotRepository(
        {
            "snapshot:base": {"values": {"d": 0, "b": 0, "a": 0, "c": 0}},
            "snapshot:target": {"values": {"d": 4, "b": 2, "a": 1, "c": 3}},
        }
    )
    service = SnapshotDiffService(repository)

    summary = service.diff_snapshots("snapshot:base", "snapshot:target")
    first = service.page_entries(
        "snapshot:base",
        "snapshot:target",
        after_path=None,
        limit=2,
    )
    second = service.page_entries(
        "snapshot:base",
        "snapshot:target",
        after_path=first.next_after_path,
        limit=2,
    )

    assert summary.model_dump(mode="json") == {
        "diff_schema_version": "snapshot-diff@1",
        "base_snapshot_id": "snapshot:base",
        "target_snapshot_id": "snapshot:target",
        "entry_count": 4,
    }
    assert [entry.path for entry in first.entries] == ["/values/a", "/values/b"]
    assert first.next_after_path == "/values/b"
    assert [entry.path for entry in second.entries] == ["/values/c", "/values/d"]
    assert second.next_after_path is None
    assert first.diff == second.diff == summary
    assert repository.loads == [
        "snapshot:base",
        "snapshot:target",
        "snapshot:base",
        "snapshot:target",
        "snapshot:base",
        "snapshot:target",
    ]


def test_repository_facade_freezes_one_shot_identity_declarations() -> None:
    repository = FakeSnapshotRepository(
        {
            "snapshot:base": {"items": [{"id": "b"}, {"id": "a"}]},
            "snapshot:target": {"items": [{"id": "a"}, {"id": "b"}]},
        }
    )
    declarations = (item for item in (CollectionIdentity(path="/items", identity_key="id"),))
    service = SnapshotDiffService(repository, collection_identities=declarations)

    assert service.diff_snapshots("snapshot:base", "snapshot:target").entry_count == 0
    assert service.diff_snapshots("snapshot:base", "snapshot:target").entry_count == 0


def test_repository_facade_raises_for_missing_snapshot_without_concrete_store_dependency() -> None:
    service = SnapshotDiffService(FakeSnapshotRepository({"snapshot:base": {}}))

    with pytest.raises(KeyError, match="snapshot:missing"):
        service.diff_snapshots("snapshot:base", "snapshot:missing")


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_ITEMS + 1])
def test_repository_facade_enforces_shared_page_bound(limit: int) -> None:
    service = SnapshotDiffService(
        FakeSnapshotRepository({"snapshot:base": {}, "snapshot:target": {}})
    )

    with pytest.raises(ValueError, match="limit"):
        service.page_entries(
            "snapshot:base",
            "snapshot:target",
            after_path=None,
            limit=limit,
        )


def test_repository_facade_rejects_invalid_keyset_position() -> None:
    service = SnapshotDiffService(
        FakeSnapshotRepository({"snapshot:base": {}, "snapshot:target": {}})
    )

    with pytest.raises(ValueError, match="JSON Pointer"):
        service.page_entries(
            "snapshot:base",
            "snapshot:target",
            after_path="not/a/pointer",
            limit=10,
        )
