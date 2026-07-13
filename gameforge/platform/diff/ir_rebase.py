"""Deterministic compilation of one resolved canonical IR view into PatchV2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import ValidationError

from gameforge.contracts.canonical import sha256_lowerhex, typed_canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import PatchV2, TypedOp
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch


REBASE_TOOL_VERSION = "three-way-rebase@1"


@dataclass(frozen=True, slots=True)
class CompiledRebase:
    patch: PatchV2
    preview: Snapshot


def _wire_equal(left: Any, right: Any) -> bool:
    return typed_canonical_json(left) == typed_canonical_json(right)


def _parse_enum(value: Any, enum_type: type[NodeType] | type[EdgeType]) -> Any:
    if not isinstance(value, str):
        raise TypeError(f"{enum_type.__name__} must use its canonical string value")
    return enum_type(value)


def _parse_ir_object(
    *,
    object_id: str,
    payload: Mapping[str, Any],
    model_type: type[Entity] | type[Relation],
    enum_type: type[NodeType] | type[EdgeType],
) -> Entity | Relation:
    allowed_fields = set(model_type.model_fields) - {"id"}
    if not object_id or set(payload) - allowed_fields:
        raise ValueError("invalid canonical IR object fields")
    raw = {"id": object_id, **payload}
    raw["type"] = _parse_enum(raw.get("type"), enum_type)
    return model_type.model_validate(raw, strict=True)


def snapshot_from_canonical_view(view: Mapping[str, Any]) -> Snapshot:
    """Parse the complete canonical IR payload without coercive fallback behavior."""

    if not isinstance(view, Mapping):
        raise IntegrityViolation("resolved rebase view must be a JSON object")
    if set(view) != {"meta_schema_version", "entities", "relations"}:
        raise IntegrityViolation(
            "resolved rebase view must contain the complete canonical IR shape"
        )
    meta_schema_version = view["meta_schema_version"]
    entities = view["entities"]
    relations = view["relations"]
    if (
        not isinstance(meta_schema_version, str)
        or not meta_schema_version
        or not isinstance(entities, Mapping)
        or not isinstance(relations, Mapping)
    ):
        raise IntegrityViolation("resolved rebase view contains invalid IR top-level fields")

    try:
        parsed_entities = {
            entity_id: _parse_ir_object(
                object_id=entity_id,
                payload=payload,
                model_type=Entity,
                enum_type=NodeType,
            )
            for entity_id, payload in sorted(entities.items())
            if isinstance(entity_id, str) and isinstance(payload, Mapping)
        }
        parsed_relations = {
            relation_id: _parse_ir_object(
                object_id=relation_id,
                payload=payload,
                model_type=Relation,
                enum_type=EdgeType,
            )
            for relation_id, payload in sorted(relations.items())
            if isinstance(relation_id, str) and isinstance(payload, Mapping)
        }
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation("resolved rebase view contains invalid IR objects") from exc
    if len(parsed_entities) != len(entities) or len(parsed_relations) != len(relations):
        raise IntegrityViolation("resolved rebase view contains invalid IR object identities")

    snapshot = Snapshot(
        entities=parsed_entities,
        relations=parsed_relations,
        meta_schema_version=meta_schema_version,
    )
    if not _wire_equal(snapshot.content_payload, view):
        raise IntegrityViolation("resolved rebase view is not an exact canonical IR payload")
    return snapshot


def _op_id(ordinal: int, kind: str, payload: Mapping[str, Any]) -> str:
    digest = sha256_lowerhex(
        typed_canonical_json(
            {
                "op_schema_version": "rebase-op@1",
                "ordinal": ordinal,
                "kind": kind,
                "payload": payload,
            }
        ).encode("utf-8")
    )
    return f"rebase-op:{ordinal:04d}:{digest}"


def _model_wire(value: Entity | Relation) -> dict[str, Any]:
    return value.model_dump(mode="python")


def _compile_ops(current: Snapshot, target: Snapshot) -> list[TypedOp]:
    current_entities = current.entities
    target_entities = target.entities
    current_relations = current.relations
    target_relations = target.relations
    ops: list[TypedOp] = []

    for relation_id in sorted(set(current_relations) - set(target_relations)):
        old_value = _model_wire(current_relations[relation_id])
        payload = {"target": relation_id, "old_value": old_value}
        ops.append(
            TypedOp(
                op_id=_op_id(len(ops) + 1, "delete_relation", payload),
                op="delete_relation",
                target=relation_id,
                old_value=old_value,
            )
        )

    changed_entities = tuple(
        entity_id
        for entity_id in sorted(target_entities)
        if entity_id not in current_entities
        or not _wire_equal(
            _model_wire(current_entities[entity_id]),
            _model_wire(target_entities[entity_id]),
        )
    )
    changed_relations = tuple(
        relation_id
        for relation_id in sorted(target_relations)
        if relation_id not in current_relations
        or not _wire_equal(
            _model_wire(current_relations[relation_id]),
            _model_wire(target_relations[relation_id]),
        )
    )
    if changed_entities or changed_relations:
        new_value = {
            "entities": [_model_wire(target_entities[item]) for item in changed_entities],
            "relations": [_model_wire(target_relations[item]) for item in changed_relations],
        }
        old_value = {
            "entities": {
                item: _model_wire(current_entities[item])
                for item in changed_entities
                if item in current_entities
            },
            "relations": {
                item: _model_wire(current_relations[item])
                for item in changed_relations
                if item in current_relations
            },
        }
        payload = {"target": "resolved-subgraph", "old_value": old_value, "new_value": new_value}
        ops.append(
            TypedOp(
                op_id=_op_id(len(ops) + 1, "replace_subgraph", payload),
                op="replace_subgraph",
                target="resolved-subgraph",
                old_value=old_value,
                new_value=new_value,
            )
        )

    # Upsert retargeted relations before deleting their former endpoints. Entity
    # deletion cascades incident relations, so the opposite order would make the
    # replace_subgraph old_value guard observe a relation that has already vanished.
    for entity_id in sorted(set(current_entities) - set(target_entities)):
        old_value = _model_wire(current_entities[entity_id])
        payload = {"target": entity_id, "old_value": old_value}
        ops.append(
            TypedOp(
                op_id=_op_id(len(ops) + 1, "delete_entity", payload),
                op="delete_entity",
                target=entity_id,
                old_value=old_value,
            )
        )
    return ops


def compile_rebased_patch(
    *,
    source_patch_artifact_id: str,
    source_patch: PatchV2,
    current: Snapshot,
    resolved_view: Mapping[str, Any],
) -> CompiledRebase:
    """Build and prove a fresh human Patch revision against the exact current base."""

    if not source_patch_artifact_id:
        raise ValueError("source_patch_artifact_id must be non-empty")
    target = snapshot_from_canonical_view(resolved_view)
    if target.meta_schema_version != current.meta_schema_version:
        raise IntegrityViolation("rebase cannot change meta_schema_version")

    patch = PatchV2(
        revision=source_patch.revision + 1,
        supersedes_artifact_id=source_patch_artifact_id,
        base_snapshot_id=current.snapshot_id,
        target_snapshot_id=target.snapshot_id,
        expected_to_fix=list(source_patch.expected_to_fix),
        preconditions=[dict(value) for value in source_patch.preconditions],
        side_effect_risk=source_patch.side_effect_risk,
        ops=_compile_ops(current, target),
        produced_by="human",
        producer_run_id=None,
        rationale=source_patch.rationale,
    )
    try:
        preview = apply_patch(current, patch)
    except PatchRejected as exc:
        raise IntegrityViolation(
            "rebased Patch does not satisfy its retained exact-base guards",
            reason=exc.reason,
            op_id=exc.op_id,
        ) from exc
    if preview.snapshot_id != target.snapshot_id or not _wire_equal(
        preview.content_payload,
        target.content_payload,
    ):
        raise IntegrityViolation("rebased Patch does not reproduce the resolved preview")
    return CompiledRebase(patch=patch, preview=preview)


__all__ = [
    "CompiledRebase",
    "REBASE_TOOL_VERSION",
    "compile_rebased_patch",
    "snapshot_from_canonical_view",
]
