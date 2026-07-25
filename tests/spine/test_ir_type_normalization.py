from __future__ import annotations

import pytest

from gameforge.contracts.findings import TypedOp
from gameforge.spine.ir_type_normalization import (
    IR_TYPE_NORMALIZATION_POLICY_VERSION,
    UnsupportedIrType,
    normalize_typed_op_ir_types,
)


def test_generation_ir_types_normalize_exact_case_and_frozen_semantic_aliases() -> None:
    operations = (
        TypedOp(
            op_id="entity:event",
            op="add_entity",
            target="event:letters",
            new_value={"type": "limited_time_event", "attrs": {"name": "梦中未寄出的信"}},
        ),
        TypedOp(
            op_id="entity:location",
            op="add_entity",
            target="location:sumeru_city",
            new_value={"type": "location", "attrs": {"name": "须弥城"}},
        ),
        TypedOp(
            op_id="relation:located",
            op="add_relation",
            target="relation:event-location",
            new_value={
                "type": "located_in",
                "src_id": "event:letters",
                "dst_id": "location:sumeru_city",
            },
        ),
    )

    normalized = normalize_typed_op_ir_types(operations)

    assert IR_TYPE_NORMALIZATION_POLICY_VERSION == "ir-type-normalization@1"
    assert [item.new_value["type"] for item in normalized] == [
        "EVENT",
        "REGION",
        "LOCATED_IN",
    ]


def test_generation_ir_types_normalize_replace_subgraph_atomically() -> None:
    operation = TypedOp(
        op_id="subgraph:event",
        op="replace_subgraph",
        target="event-plan",
        new_value={
            "entities": [
                {"id": "story:event", "type": "story_quest_series", "attrs": {}},
                {"id": "device:dream", "type": "device", "attrs": {}},
            ],
            "relations": [
                {
                    "id": "relation:trigger",
                    "type": "triggered_by",
                    "src_id": "story:event",
                    "dst_id": "device:dream",
                }
            ],
        },
    )

    normalized = normalize_typed_op_ir_types((operation,))

    payload = normalized[0].new_value
    assert [item["type"] for item in payload["entities"]] == ["QUEST", "INTERACTABLE"]
    assert payload["relations"][0]["type"] == "TRIGGERED_BY"


def test_generation_ir_types_reject_unknown_type_without_returning_partial_ops() -> None:
    operations = (
        TypedOp(
            op_id="valid",
            op="add_entity",
            target="event:letters",
            new_value={"type": "EVENT", "attrs": {}},
        ),
        TypedOp(
            op_id="invalid",
            op="add_entity",
            target="marketing:campaign",
            new_value={"type": "marketing_campaign", "attrs": {}},
        ),
    )

    with pytest.raises(UnsupportedIrType, match="marketing_campaign"):
        normalize_typed_op_ir_types(operations)
