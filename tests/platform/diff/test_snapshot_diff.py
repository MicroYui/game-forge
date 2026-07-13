from __future__ import annotations

from collections.abc import Iterable, Mapping

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.diff import SnapshotDiffEntry
from gameforge.platform.diff import (
    CollectionIdentity,
    iter_snapshot_diff_entries,
)


def _wire(entries: Iterable[SnapshotDiffEntry]) -> list[dict[str, object]]:
    return [entry.model_dump(mode="json") for entry in entries]


def test_diff_compares_every_field_of_complete_canonical_objects() -> None:
    base = {
        "meta_schema_version": "meta@1",
        "entities": {
            "quest/intro~draft": {
                "type": "QUEST",
                "schema_version": "ir@1",
                "source_ref": {
                    "adapter": "aureus",
                    "file": "quests.csv",
                    "sheet": "Quests",
                    "row": 7,
                },
                "attrs": {"reward": 10},
            }
        },
        "relations": {
            "step-edge": {
                "type": "HAS_STEP",
                "schema_version": "ir@1",
                "src_id": "quest/intro~draft",
                "dst_id": "step-1",
                "source_ref": {"adapter": "aureus", "file": "quests.csv"},
                "attrs": {"ordinal": 1},
            }
        },
    }
    target = {
        "meta_schema_version": "meta@2",
        "entities": {
            "quest/intro~draft": {
                "type": "EVENT",
                "schema_version": "ir@2",
                "source_ref": {
                    "adapter": "flare",
                    "file": "quests.txt",
                    "sheet": "Quests",
                    "row": 8,
                },
                "attrs": {"reward": 10},
            }
        },
        "relations": {
            "step-edge": {
                "type": "PRECEDES",
                "schema_version": "ir@2",
                "src_id": "step-0",
                "dst_id": "step-2",
                "source_ref": {"adapter": "flare", "file": "quests.txt"},
                "attrs": {"ordinal": 1},
            }
        },
    }

    entries = tuple(iter_snapshot_diff_entries(base, target))

    assert [entry.path for entry in entries] == sorted(entry.path for entry in entries)
    assert [entry.path for entry in entries] == [
        "/entities/quest~1intro~0draft/schema_version",
        "/entities/quest~1intro~0draft/source_ref/adapter",
        "/entities/quest~1intro~0draft/source_ref/file",
        "/entities/quest~1intro~0draft/source_ref/row",
        "/entities/quest~1intro~0draft/type",
        "/meta_schema_version",
        "/relations/step-edge/dst_id",
        "/relations/step-edge/schema_version",
        "/relations/step-edge/source_ref/adapter",
        "/relations/step-edge/source_ref/file",
        "/relations/step-edge/src_id",
        "/relations/step-edge/type",
    ]


def test_diff_distinguishes_missing_from_explicit_json_null() -> None:
    entries = tuple(iter_snapshot_diff_entries({}, {"optional": None}))

    assert _wire(entries) == [
        {
            "path": "/optional",
            "before": {"presence": "missing"},
            "after": {"presence": "present", "value": None},
        }
    ]


@pytest.mark.parametrize(
    ("base", "target"),
    [(False, 0), (True, 1), (1, 1.0), (1.0, "f:1"), (-0.0, 0.0)],
)
def test_diff_preserves_canonical_json_scalar_type_changes(
    base: object,
    target: object,
) -> None:
    entries = tuple(iter_snapshot_diff_entries({"value": base}, {"value": target}))

    assert len(entries) == 1
    assert entries[0].path == "/value"
    assert type(entries[0].before.value) is type(base)
    assert entries[0].before.value == base
    assert type(entries[0].after.value) is type(target)
    assert entries[0].after.value == target


def test_dict_insertion_order_does_not_change_diff_or_output_order() -> None:
    base_a = {"z": {"second": 2, "first": 1}, "a": 0}
    base_b = {"a": 0, "z": {"first": 1, "second": 2}}
    target_a = {"z": {"second": 20, "first": 10}, "a": 0}
    target_b = {"a": 0, "z": {"first": 10, "second": 20}}

    first = _wire(iter_snapshot_diff_entries(base_a, target_a))
    second = _wire(iter_snapshot_diff_entries(base_b, target_b))

    assert first == second
    assert [entry["path"] for entry in first] == ["/z/first", "/z/second"]


def test_one_sided_subtree_values_have_byte_stable_recursive_map_order() -> None:
    first = tuple(iter_snapshot_diff_entries({}, {"new": {"z": 1, "a": {"y": 2, "b": 3}}}))
    second = tuple(iter_snapshot_diff_entries({}, {"new": {"a": {"b": 3, "y": 2}, "z": 1}}))

    assert first[0].model_dump_json() == second[0].model_dump_json()
    assert list(first[0].after.value) == ["a", "z"]
    assert list(first[0].after.value["a"]) == ["b", "y"]


def test_pointer_sort_is_global_when_one_object_key_prefixes_another() -> None:
    entries = tuple(
        iter_snapshot_diff_entries(
            {"a": {"nested": 0}, "a-": 0, "array": list(range(12))},
            {"a": {"nested": 1}, "a-": 1, "array": [value + 1 for value in range(12)]},
        )
    )

    assert [entry.path for entry in entries] == sorted(entry.path for entry in entries)
    assert [entry.path for entry in entries[:5]] == [
        "/a-",
        "/a/nested",
        "/array/0",
        "/array/1",
        "/array/10",
    ]


def test_arrays_are_ordered_unless_exact_path_declares_collection_identity() -> None:
    base = {"items": [{"id": "b", "value": 2}, {"id": "a", "value": 1}]}
    reordered = {"items": [{"id": "a", "value": 1}, {"id": "b", "value": 2}]}

    ordered = tuple(iter_snapshot_diff_entries(base, reordered))
    identity_sorted = tuple(
        iter_snapshot_diff_entries(
            base,
            reordered,
            collection_identities=(CollectionIdentity(path="/items", identity_key="id"),),
        )
    )

    assert [entry.path for entry in ordered] == [
        "/items/0/id",
        "/items/0/value",
        "/items/1/id",
        "/items/1/value",
    ]
    assert identity_sorted == ()


def test_collection_identity_is_path_specific_and_keeps_field_level_changes() -> None:
    base = {
        "groups": {
            "a/b": {
                "items": [
                    {"key~name": "second", "value": 2},
                    {"key~name": "first", "value": 1},
                ]
            }
        },
        "ordered": [{"key~name": "second"}, {"key~name": "first"}],
    }
    target = {
        "groups": {
            "a/b": {
                "items": [
                    {"key~name": "first", "value": 10},
                    {"key~name": "second", "value": 2},
                ]
            }
        },
        "ordered": [{"key~name": "first"}, {"key~name": "second"}],
    }

    entries = tuple(
        iter_snapshot_diff_entries(
            base,
            target,
            collection_identities=(
                CollectionIdentity(
                    path="/groups/a~1b/items",
                    identity_key="key~name",
                ),
            ),
        )
    )

    assert [entry.path for entry in entries] == [
        "/groups/a~1b/items/0/value",
        "/ordered/0/key~0name",
        "/ordered/1/key~0name",
    ]
    assert entries[0].before.value == 1
    assert entries[0].after.value == 10


@pytest.mark.parametrize("direction", ["add", "remove"])
def test_one_sided_declared_collection_is_emitted_in_identity_order(direction: str) -> None:
    collection = {
        "items": [
            {"id": "b", "payload": {"z": 1, "a": 2}},
            {"id": "a", "payload": {"y": 3, "b": 4}},
        ]
    }
    if direction == "add":
        base, target = {}, collection
    else:
        base, target = collection, {}

    (entry,) = iter_snapshot_diff_entries(
        base,
        target,
        collection_identities=(CollectionIdentity(path="/items", identity_key="id"),),
    )
    state = entry.after if direction == "add" else entry.before

    canonical_collection = {
        "items": [
            {"payload": {"b": 4, "y": 3}, "id": "a"},
            {"payload": {"a": 2, "z": 1}, "id": "b"},
        ]
    }
    if direction == "add":
        canonical_base, canonical_target = {}, canonical_collection
    else:
        canonical_base, canonical_target = canonical_collection, {}
    (canonical_entry,) = iter_snapshot_diff_entries(
        canonical_base,
        canonical_target,
        collection_identities=(CollectionIdentity(path="/items", identity_key="id"),),
    )

    assert entry.path == "/items"
    assert entry.model_dump_json() == canonical_entry.model_dump_json()
    assert [item["id"] for item in state.value] == ["a", "b"]
    assert list(state.value[0]["payload"]) == ["b", "y"]
    assert list(state.value[1]["payload"]) == ["a", "z"]


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([{"value": 1}], "missing identity"),
        ([{"id": "same"}, {"id": "same"}], "duplicate identity"),
        ([{"id": None}], "scalar identity"),
        ([{"id": ["not", "scalar"]}], "scalar identity"),
    ],
)
def test_collection_identity_fail_closed_for_noncanonical_members(
    items: list[dict[str, object]],
    message: str,
) -> None:
    with pytest.raises(IntegrityViolation, match=message):
        tuple(
            iter_snapshot_diff_entries(
                {"items": items},
                {"items": items},
                collection_identities=(CollectionIdentity(path="/items", identity_key="id"),),
            )
        )


def test_diff_rejects_non_json_canonical_views() -> None:
    class PretendsToBeJson(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            return object()

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield "bad"

        def __len__(self) -> int:
            return 1

    with pytest.raises(IntegrityViolation, match="JSON value"):
        tuple(iter_snapshot_diff_entries(PretendsToBeJson(), {}))
