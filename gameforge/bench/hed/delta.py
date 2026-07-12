"""Canonical semantic deltas over source-neutral Spec-IR snapshots."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.ir import Entity
from gameforge.spine.ir.snapshot import Snapshot


SEMANTIC_DELTA_VERSION = "semantic-ir-delta@1"
DISTANCE_METRIC = "semantic-jaccard-symmetric-difference@1"
_OPAQUE_COMPONENT = re.compile(r"(?:^|:)[0-9a-f]{64}(?::|$)")
_NON_SEMANTIC_ATTRS = frozenset(
    {
        "source_chunk_b64",
        "source_kind",
        "source_name",
        "source_order",
        "reader_version",
    }
)

DeltaKind = Literal[
    "add_entity",
    "delete_entity",
    "set_entity_attr",
    "add_relation",
    "delete_relation",
]


@dataclass(frozen=True, order=True)
class AtomicDelta:
    kind: DeltaKind
    target: str
    field: str | None
    old_json: str | None
    new_json: str | None


def _semantic_attrs(attrs: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (attrs or {}).items()
        if key not in _NON_SEMANTIC_ATTRS
    }


def _entity_payload(entity: Entity, *, include_id: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": entity.type.value,
        "attrs": _semantic_attrs(entity.attrs),
        "tags": entity.tags,
        "schema_version": entity.schema_version,
    }
    if include_id:
        payload["id"] = entity.id
    return payload


def _is_opaque_id(entity_id: str) -> bool:
    return _OPAQUE_COMPONENT.search(entity_id) is not None


def _digest(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _entity_label(entity: Entity) -> str:
    if not _is_opaque_id(entity.id):
        return f"id:{entity.id}"
    payload_json = canonical_json(_entity_payload(entity, include_id=False))
    return f"opaque:{_digest(payload_json)}"


def _json_value(mapping: dict[str, Any], key: str) -> str | None:
    if key not in mapping:
        return None
    return canonical_json(mapping[key])


def _stable_entity_deltas(
    before: dict[str, Entity],
    after: dict[str, Entity],
) -> list[AtomicDelta]:
    deltas: list[AtomicDelta] = []
    for entity_id in sorted(set(before) | set(after)):
        old = before.get(entity_id)
        new = after.get(entity_id)
        if old is None and new is not None:
            deltas.append(
                AtomicDelta(
                    "add_entity",
                    entity_id,
                    None,
                    None,
                    canonical_json(_entity_payload(new, include_id=True)),
                )
            )
            continue
        if new is None and old is not None:
            deltas.append(
                AtomicDelta(
                    "delete_entity",
                    entity_id,
                    None,
                    canonical_json(_entity_payload(old, include_id=True)),
                    None,
                )
            )
            continue
        if old is None or new is None:
            raise AssertionError("entity union produced an impossible empty pair")

        scalar_fields = {
            "type": (old.type.value, new.type.value),
            "tags": (old.tags, new.tags),
            "schema_version": (old.schema_version, new.schema_version),
        }
        for field, (old_value, new_value) in scalar_fields.items():
            if old_value != new_value:
                deltas.append(
                    AtomicDelta(
                        "set_entity_attr",
                        entity_id,
                        field,
                        canonical_json(old_value),
                        canonical_json(new_value),
                    )
                )
        old_attrs = _semantic_attrs(old.attrs)
        new_attrs = _semantic_attrs(new.attrs)
        for key in sorted(set(old_attrs) | set(new_attrs)):
            old_json = _json_value(old_attrs, key)
            new_json = _json_value(new_attrs, key)
            if old_json != new_json:
                deltas.append(
                    AtomicDelta(
                        "set_entity_attr",
                        entity_id,
                        f"attrs.{key}",
                        old_json,
                        new_json,
                    )
                )
    return deltas


def _counter_deltas(
    before: Counter[str],
    after: Counter[str],
    *,
    target_prefix: str,
    add_kind: Literal["add_entity", "add_relation"],
    delete_kind: Literal["delete_entity", "delete_relation"],
) -> list[AtomicDelta]:
    deltas: list[AtomicDelta] = []
    for payload_json in sorted(set(before) | set(after)):
        shared = min(before[payload_json], after[payload_json])
        digest = _digest(payload_json)
        for ordinal in range(shared, before[payload_json]):
            deltas.append(
                AtomicDelta(
                    delete_kind,
                    f"{target_prefix}:{digest}:{ordinal}",
                    None,
                    payload_json,
                    None,
                )
            )
        for ordinal in range(shared, after[payload_json]):
            deltas.append(
                AtomicDelta(
                    add_kind,
                    f"{target_prefix}:{digest}:{ordinal}",
                    None,
                    None,
                    payload_json,
                )
            )
    return deltas


def _opaque_entities(snapshot: Snapshot) -> Counter[str]:
    return Counter(
        canonical_json(_entity_payload(entity, include_id=False))
        for entity in snapshot.entities.values()
        if _is_opaque_id(entity.id)
    )


def _stable_entities(snapshot: Snapshot) -> dict[str, Entity]:
    return {
        entity_id: entity
        for entity_id, entity in snapshot.entities.items()
        if not _is_opaque_id(entity_id)
    }


def _relation_facts(snapshot: Snapshot) -> Counter[str]:
    labels = {
        entity_id: _entity_label(entity)
        for entity_id, entity in snapshot.entities.items()
    }
    facts: Counter[str] = Counter()
    for relation in snapshot.relations.values():
        payload = {
            "type": relation.type.value,
            "src": labels.get(relation.src_id, f"missing:{relation.src_id}"),
            "dst": labels.get(relation.dst_id, f"missing:{relation.dst_id}"),
            "attrs": _semantic_attrs(relation.attrs),
        }
        facts[canonical_json(payload)] += 1
    return facts


def _sort_key(delta: AtomicDelta) -> tuple[str, str, str, str, str]:
    return (
        delta.kind,
        delta.target,
        delta.field or "",
        delta.old_json or "",
        delta.new_json or "",
    )


def semantic_delta(before: Snapshot, after: Snapshot) -> tuple[AtomicDelta, ...]:
    """Return source-neutral atomic changes from ``before`` to ``after``."""

    deltas = _stable_entity_deltas(
        _stable_entities(before),
        _stable_entities(after),
    )
    deltas.extend(
        _counter_deltas(
            _opaque_entities(before),
            _opaque_entities(after),
            target_prefix="opaque-entity",
            add_kind="add_entity",
            delete_kind="delete_entity",
        )
    )
    deltas.extend(
        _counter_deltas(
            _relation_facts(before),
            _relation_facts(after),
            target_prefix="relation",
            add_kind="add_relation",
            delete_kind="delete_relation",
        )
    )
    return tuple(sorted(deltas, key=_sort_key))


def symmetric_difference_distance(
    first: tuple[AtomicDelta, ...],
    second: tuple[AtomicDelta, ...],
) -> tuple[int, float]:
    """Return raw and Jaccard-normalized symmetric-difference distance."""

    first_set = set(first)
    second_set = set(second)
    if len(first_set) != len(first) or len(second_set) != len(second):
        raise ValueError("semantic delta inputs must not contain duplicates")
    raw = len(first_set ^ second_set)
    union = len(first_set | second_set)
    return raw, (raw / union if union else 0.0)


__all__ = [
    "DISTANCE_METRIC",
    "SEMANTIC_DELTA_VERSION",
    "AtomicDelta",
    "semantic_delta",
    "symmetric_difference_distance",
]
