from __future__ import annotations

from copy import deepcopy

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.platform.diff.ir_rebase import compile_rebased_patch
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import apply_patch


def _snapshot(*, reward: int = 10, include_old: bool = True) -> Snapshot:
    entities = [
        Entity(id="quest", type=NodeType.QUEST, attrs={"reward": reward, "obsolete": True}),
        Entity(id="step", type=NodeType.QUEST_STEP, attrs={"ordinal": 1}),
    ]
    if include_old:
        entities.append(Entity(id="old", type=NodeType.ITEM, attrs={"value": 1}))
    relations = [
        Relation(
            id="has-step",
            type=EdgeType.HAS_STEP,
            src_id="quest",
            dst_id="step",
            attrs={"ordinal": 1},
        )
    ]
    return Snapshot.from_entities_relations(entities, relations)


def _source_patch(base: Snapshot, *, preconditions: list[dict] | None = None) -> PatchV2:
    return PatchV2(
        revision=2,
        supersedes_artifact_id="artifact:patch:1",
        base_snapshot_id=base.snapshot_id,
        target_snapshot_id="snapshot:old-preview",
        expected_to_fix=["finding:1"],
        preconditions=preconditions or [],
        side_effect_risk="low",
        ops=[],
        produced_by="human",
        producer_run_id=None,
        rationale="repair quest progression",
    )


def test_compile_rebased_patch_is_deterministic_and_exactly_reproduces_resolved_ir() -> None:
    current = _snapshot(reward=20)
    resolved = deepcopy(current.content_payload)
    resolved["entities"].pop("old")
    resolved["entities"]["quest"]["attrs"] = {"reward": 30}
    resolved["entities"]["new"] = {
        "type": "ITEM",
        "attrs": {"value": 9},
        "schema_version": "ir@1",
    }
    resolved["relations"]["has-step"]["attrs"] = {"ordinal": 2}

    first = compile_rebased_patch(
        source_patch_artifact_id="artifact:patch:2",
        source_patch=_source_patch(current),
        current=current,
        resolved_view=resolved,
    )
    second = compile_rebased_patch(
        source_patch_artifact_id="artifact:patch:2",
        source_patch=_source_patch(current),
        current=current,
        resolved_view=resolved,
    )

    assert first.patch == second.patch
    assert first.preview.snapshot_id == second.preview.snapshot_id
    assert first.preview.content_payload == second.preview.content_payload
    assert first.patch.revision == 3
    assert first.patch.supersedes_artifact_id == "artifact:patch:2"
    assert first.patch.base_snapshot_id == current.snapshot_id
    assert first.patch.target_snapshot_id == first.preview.snapshot_id
    assert [operation.op for operation in first.patch.ops] == [
        "replace_subgraph",
        "delete_entity",
    ]
    assert apply_patch(current, first.patch).content_payload == first.preview.content_payload
    assert first.preview.content_payload == resolved


def test_compile_rebased_patch_retargets_relation_before_deleting_old_endpoint() -> None:
    current = _snapshot(reward=20)
    resolved = deepcopy(current.content_payload)
    resolved["entities"].pop("quest")
    resolved["entities"]["quest-v2"] = {
        "type": "QUEST",
        "attrs": {"reward": 20, "obsolete": False},
        "schema_version": "ir@1",
    }
    resolved["relations"]["has-step"]["src_id"] = "quest-v2"

    compiled = compile_rebased_patch(
        source_patch_artifact_id="artifact:patch:2",
        source_patch=_source_patch(current),
        current=current,
        resolved_view=resolved,
    )

    assert [operation.op for operation in compiled.patch.ops] == [
        "replace_subgraph",
        "delete_entity",
    ]
    assert apply_patch(current, compiled.patch).content_payload == resolved
    assert compiled.preview.content_payload == resolved


def test_compile_rebased_patch_preserves_and_rechecks_source_preconditions() -> None:
    current = _snapshot(reward=20)
    source = _source_patch(
        current,
        preconditions=[
            {"kind": "attr_equals", "target": "quest.reward", "value": 10}
        ],
    )

    with pytest.raises(IntegrityViolation, match="retained exact-base guards"):
        compile_rebased_patch(
            source_patch_artifact_id="artifact:patch:2",
            source_patch=source,
            current=current,
            resolved_view=current.content_payload,
        )


def test_compile_rebased_patch_rejects_schema_migration_and_noncanonical_ir() -> None:
    current = _snapshot()
    migrated = deepcopy(current.content_payload)
    migrated["meta_schema_version"] = "meta@future"
    with pytest.raises(IntegrityViolation, match="meta_schema_version"):
        compile_rebased_patch(
            source_patch_artifact_id="artifact:patch:2",
            source_patch=_source_patch(current),
            current=current,
            resolved_view=migrated,
        )

    malformed = deepcopy(current.content_payload)
    malformed["entities"]["quest"]["unexpected"] = True
    with pytest.raises(IntegrityViolation, match="invalid IR objects"):
        compile_rebased_patch(
            source_patch_artifact_id="artifact:patch:2",
            source_patch=_source_patch(current),
            current=current,
            resolved_view=malformed,
        )
