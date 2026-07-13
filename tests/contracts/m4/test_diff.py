from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.diff import (
    ConflictResolution,
    ConflictSet,
    JsonValueState,
    MergeConflict,
    RebaseResult,
    SnapshotDiff,
    SnapshotDiffEntry,
    SnapshotDiffEntryPage,
)
from gameforge.contracts.storage import PageV1


def test_json_value_state_distinguishes_missing_from_present_null() -> None:
    missing = JsonValueState.model_validate({"presence": "missing"})
    present_null = JsonValueState.model_validate({"presence": "present", "value": None})
    assert missing.model_dump(exclude_none=False) == {"presence": "missing"}
    assert present_null.model_dump(exclude_none=False) == {
        "presence": "present",
        "value": None,
    }
    with pytest.raises(ValidationError):
        JsonValueState.model_validate({"presence": "present"})
    with pytest.raises(ValidationError):
        JsonValueState.model_validate({"presence": "missing", "value": None})


def test_conflict_resolution_requires_custom_value_only_for_custom_choice() -> None:
    custom = ConflictResolution(conflict_id="conflict:1", choice="custom", custom_value=None)
    assert custom.custom_value is None
    assert ConflictResolution.model_validate(custom.model_dump(mode="json")) == custom
    keep = ConflictResolution(conflict_id="conflict:1", choice="keep_current")
    assert "custom_value" not in keep.model_dump(mode="json")
    assert ConflictResolution.model_validate(keep.model_dump(mode="json")) == keep
    with pytest.raises(ValidationError):
        ConflictResolution(conflict_id="conflict:1", choice="custom")
    with pytest.raises(ValidationError):
        ConflictResolution(conflict_id="conflict:1", choice="keep_current", custom_value=1)


def test_rebase_result_is_a_closed_status_union() -> None:
    clean = RebaseResult(status="clean", new_patch_artifact_id="patch:2")
    conflicted = RebaseResult(status="conflicted", conflict_set_id="conflicts:1")
    assert clean.new_patch_artifact_id == "patch:2"
    assert conflicted.conflict_set_id == "conflicts:1"
    with pytest.raises(ValidationError):
        RebaseResult(status="clean", conflict_set_id="conflicts:1")
    with pytest.raises(ValidationError):
        RebaseResult(status="conflicted", new_patch_artifact_id="patch:2")


def test_snapshot_diff_count_and_conflict_count_are_exact() -> None:
    entry = SnapshotDiffEntry(
        path="/entities/q~01/attrs/reward",
        before={"presence": "missing"},
        after={"presence": "present", "value": None},
    )
    diff = SnapshotDiff(
        diff_schema_version="snapshot-diff@1",
        base_snapshot_id="snapshot:a",
        target_snapshot_id="snapshot:b",
        entry_count=1,
    )
    entry_page = SnapshotDiffEntryPage(
        diff=diff,
        page=PageV1[SnapshotDiffEntry](
            read_snapshot_id="read:diff:1",
            items=(entry,),
            expires_at="2026-07-13T12:10:00Z",
        ),
    )
    conflict = MergeConflict(
        id="conflict:1",
        path=entry.path,
        kind="concurrent_change",
        base={"presence": "missing"},
        current={"presence": "present", "value": None},
        proposed={"presence": "present", "value": 3},
        allowed_resolutions=("keep_current", "take_proposed", "custom"),
    )
    conflict_set = ConflictSet(
        schema_version="conflict-set@1",
        id="conflicts:1",
        base_snapshot_id="snapshot:a",
        current_snapshot_id="snapshot:b",
        proposed_patch_artifact_id="patch:1",
        expected_ref_revision=4,
        conflict_count=1,
        non_conflicting_ops_digest="a" * 64,
        created_at="2026-07-13T12:00:00Z",
    )
    assert entry_page.page.items[0].before.presence == "missing"
    assert conflict.path == entry.path
    assert conflict_set.conflict_count == 1
    with pytest.raises(ValidationError):
        SnapshotDiffEntryPage.model_validate(
            {
                **entry_page.model_dump(mode="json"),
                "diff": {**diff.model_dump(mode="json"), "entry_count": 0},
            }
        )


def test_final_diff_page_does_not_confuse_page_length_with_total_count() -> None:
    final_entry = SnapshotDiffEntry(
        path="/second",
        before={"presence": "missing"},
        after={"presence": "present", "value": 2},
    )
    final_page = SnapshotDiffEntryPage(
        diff=SnapshotDiff(
            base_snapshot_id="snapshot:a",
            target_snapshot_id="snapshot:b",
            entry_count=2,
        ),
        page=PageV1[SnapshotDiffEntry](
            read_snapshot_id="read:diff:1",
            items=(final_entry,),
            expires_at="2026-07-13T12:10:00Z",
        ),
    )
    assert final_page.diff.entry_count == 2
    assert len(final_page.page.items) == 1


def test_diff_wire_records_round_trip_through_json_shaped_dicts() -> None:
    entry = SnapshotDiffEntry(
        path="/attrs/value",
        before={"presence": "present", "value": None},
        after={"presence": "present", "value": [1, 2]},
    )
    page = SnapshotDiffEntryPage(
        diff=SnapshotDiff(
            base_snapshot_id="snapshot:a",
            target_snapshot_id="snapshot:b",
            entry_count=1,
        ),
        page=PageV1[SnapshotDiffEntry](
            read_snapshot_id="read:diff:1",
            items=(entry,),
            expires_at="2026-07-13T12:10:00Z",
        ),
    )
    assert SnapshotDiffEntryPage.model_validate(page.model_dump(mode="json")) == page


def test_conflict_paths_must_be_json_pointers() -> None:
    with pytest.raises(ValidationError):
        SnapshotDiffEntry(
            path="entities/q1",
            before={"presence": "missing"},
            after={"presence": "present", "value": 1},
        )
