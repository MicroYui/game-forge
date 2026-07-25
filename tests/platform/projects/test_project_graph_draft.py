from __future__ import annotations

import pytest

from gameforge.contracts.errors import Conflict
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.platform.projects.graph_draft import compile_project_graph_draft
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import apply_patch


def test_full_graph_draft_merges_aliases_and_replays_exactly() -> None:
    base = Snapshot(
        entities={
            "mob": Entity(id="mob", type=NodeType.MONSTER),
        },
        relations={},
    )
    entities = (
        Entity(id="mob", type=NodeType.MONSTER),
        Entity(id="air.quality", type=NodeType.ITEM, attrs={"label": "Air quality"}),
        Entity(id="AIR_QUALITY", type=NodeType.ITEM, attrs={"label": "Air quality"}),
    )
    relations = (
        Relation(
            id="air.drop",
            type=EdgeType.DROPS_FROM,
            src_id="mob",
            dst_id="air_quality",
        ),
    )

    compiled = compile_project_graph_draft(
        base=base,
        entities=entities,
        relations=relations,
    )

    assert set(compiled.target.entities) == {"mob", "item:air_quality"}
    assert set(compiled.target.relations) == {"rel:air_drop"}
    assert compiled.target.relations["rel:air_drop"].dst_id == "item:air_quality"
    assert compiled.normalization_summary.auto_merge_count == 1
    replay = apply_patch(
        base,
        PatchV2(
            revision=1,
            base_snapshot_id=base.snapshot_id,
            target_snapshot_id=compiled.target.snapshot_id,
            side_effect_risk="low",
            ops=list(compiled.ops),
            produced_by="human",
            rationale="test",
        ),
    )
    assert replay.content_payload == compiled.target.content_payload


def test_full_graph_draft_rejects_conflicting_alias_values() -> None:
    with pytest.raises(Conflict, match="require human resolution") as captured:
        compile_project_graph_draft(
            base=Snapshot(entities={}, relations={}),
            entities=(
                Entity(id="air.quality", type=NodeType.ITEM, attrs={"label": "Clean"}),
                Entity(id="air_quality", type=NodeType.ITEM, attrs={"label": "Polluted"}),
            ),
            relations=(),
        )

    assert captured.value.context["conflicts"][0]["canonical_identity"] == (
        "item:air_quality.label"
    )
