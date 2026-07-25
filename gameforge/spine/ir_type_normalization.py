"""Deterministic IR type normalization for model-proposed operations.

The model may vary casing or use a small, frozen set of common design-document
labels.  This module is the only authority allowed to translate those labels to
the closed Design-Spec IR enums.  Unknown labels fail the whole proposal; callers
must never keep a partially normalized patch.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
import unicodedata
from typing import Any

from gameforge.contracts.findings import TypedOp
from gameforge.contracts.ir import EdgeType, NodeType


IR_TYPE_NORMALIZATION_POLICY_VERSION = "ir-type-normalization@1"

_SEPARATORS = re.compile(r"[.\-/\\\s]+", re.UNICODE)
_UNDERSCORES = re.compile(r"_+")


class UnsupportedIrType(ValueError):
    """A proposed type cannot be mapped without semantic guessing."""


def _type_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = _SEPARATORS.sub("_", normalized)
    return _UNDERSCORES.sub("_", normalized).strip("_")


_NODE_TYPES = {_type_token(item.value): item.value for item in NodeType}
_EDGE_TYPES = {_type_token(item.value): item.value for item in EdgeType}

# These aliases are deliberately narrow and versioned.  Each maps a common
# document label to one unambiguous IR concept; broad labels such as "mode" or
# "reward" remain unsupported and require the model or a human to be explicit.
_NODE_TYPE_ALIASES = {
    "limited_time_event": NodeType.EVENT.value,
    "organization": NodeType.FACTION.value,
    "organisation": NodeType.FACTION.value,
    "location": NodeType.REGION.value,
    "device": NodeType.INTERACTABLE.value,
    "story": NodeType.QUEST.value,
    "story_quest": NodeType.QUEST.value,
    "story_quest_series": NodeType.QUEST.value,
    "story_act": NodeType.QUEST_STEP.value,
}


def _resolve_type(
    value: object,
    *,
    allowed: Mapping[str, str],
    aliases: Mapping[str, str],
    operation: TypedOp,
    field: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UnsupportedIrType(
            f"{operation.op_id} {field} has no non-empty IR type"
        )
    token = _type_token(value)
    resolved = allowed.get(token) or aliases.get(token)
    if resolved is None:
        raise UnsupportedIrType(
            f"{operation.op_id} {field} uses unsupported IR type {value!r}"
        )
    return resolved


def _mapping(value: object, *, operation: TypedOp, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UnsupportedIrType(f"{operation.op_id} {field} is not an object")
    return dict(value)


def _normalize_entity_payload(payload: object, *, operation: TypedOp, field: str) -> dict[str, Any]:
    rendered = _mapping(payload, operation=operation, field=field)
    rendered["type"] = _resolve_type(
        rendered.get("type"),
        allowed=_NODE_TYPES,
        aliases=_NODE_TYPE_ALIASES,
        operation=operation,
        field=f"{field}.type",
    )
    return rendered


def _normalize_relation_payload(
    payload: object,
    *,
    operation: TypedOp,
    field: str,
) -> dict[str, Any]:
    rendered = _mapping(payload, operation=operation, field=field)
    rendered["type"] = _resolve_type(
        rendered.get("type"),
        allowed=_EDGE_TYPES,
        aliases={},
        operation=operation,
        field=f"{field}.type",
    )
    return rendered


def _normalize_subgraph(operation: TypedOp) -> dict[str, Any]:
    payload = _mapping(operation.new_value, operation=operation, field="new_value")
    entities = payload.get("entities", [])
    relations = payload.get("relations", [])
    if not isinstance(entities, list) or not isinstance(relations, list):
        raise UnsupportedIrType(
            f"{operation.op_id} replace_subgraph entities/relations must be arrays"
        )
    payload["entities"] = [
        _normalize_entity_payload(
            item,
            operation=operation,
            field=f"new_value.entities[{index}]",
        )
        for index, item in enumerate(entities)
    ]
    payload["relations"] = [
        _normalize_relation_payload(
            item,
            operation=operation,
            field=f"new_value.relations[{index}]",
        )
        for index, item in enumerate(relations)
    ]
    return payload


def normalize_typed_op_ir_types(operations: Iterable[TypedOp]) -> tuple[TypedOp, ...]:
    """Return an all-or-nothing canonical IR-type projection."""

    normalized: list[TypedOp] = []
    for operation in tuple(operations):
        if operation.op == "add_entity":
            payload = _normalize_entity_payload(
                operation.new_value,
                operation=operation,
                field="new_value",
            )
            normalized.append(operation.model_copy(update={"new_value": payload}))
        elif operation.op == "add_relation":
            payload = _normalize_relation_payload(
                operation.new_value,
                operation=operation,
                field="new_value",
            )
            normalized.append(operation.model_copy(update={"new_value": payload}))
        elif operation.op == "replace_subgraph":
            normalized.append(
                operation.model_copy(update={"new_value": _normalize_subgraph(operation)})
            )
        else:
            normalized.append(operation)
    return tuple(normalized)


__all__ = [
    "IR_TYPE_NORMALIZATION_POLICY_VERSION",
    "UnsupportedIrType",
    "normalize_typed_op_ir_types",
]
