"""Deterministic typed-patch apply/reject engine (contract §6 old_value anchor).

A `Patch` (contract §6, `gameforge.contracts.findings.Patch`) is a proposed,
reviewable mutation of an IR snapshot expressed as an ordered list of typed
operations (`TypedOp`). This module is the *only* place that turns a `Patch`
into a new `Snapshot` — it never edits a snapshot in place and it never
"best-effort" applies an op it cannot verify: any anomaly is a hard reject
(`PatchRejected`), never a silent partial apply.

Two independent gates run before/while applying, both fail-closed:

1. **Preconditions** (`Patch.preconditions`, a small documented vocabulary):
   - `{"kind": "entity_exists", "id": <entity_id>}` — entity must currently
     exist in the graph.
   - `{"kind": "attr_equals", "target": "<entity_id>.<path>", "value": <v>}`
     — the (possibly dotted-nested) attr at `path` under `entity_id.attrs`
     must currently equal `value`.
   Any unrecognized `kind`, or any failing condition, rejects the whole patch
   before a single op is applied.

2. **old_value optimistic concurrency** (contract §6 anchor): for any
   `TypedOp` whose `old_value` is not `None`, the *current* value at
   `op.target` (see convention below) must equal `op.old_value` at the moment
   that op is applied (ops apply in list order, so an earlier op in the same
   patch may legitimately set up the value an later op's `old_value` expects).
   A mismatch means the world has moved since the patch was drafted — the
   patch is stale and must be rebased or rejected, never blindly applied.

Target convention (per `TypedOpKind`):
  - `set_entity_attr` — `target = "<entity_id>.<attr_key>"`, split on the
    FIRST `.` (entity ids use `:`, never `.`; the remainder after the first
    `.` may itself be a dotted nested path into `attrs`).
  - `set_relation_attr` — `target = "<relation_id>.<attr_key>"`, same split
    rule, nested path into the relation's `attrs`.
  - `add_entity` / `delete_entity` — `target` = the entity id;
    `new_value` = the payload dict for `add_entity` (a `Entity(**payload)`
    minus `id`, which defaults to `target`).
  - `add_relation` / `delete_relation` — `target` = the relation id;
    `new_value` = the payload dict for `add_relation` (a `Relation(**payload)`
    minus `id`, which defaults to `target`).
  - `replace_subgraph` — `target` is a free-form label (not used for lookup);
    `new_value = {"entities": [<entity dict>, ...], "relations": [<relation
    dict>, ...]}`. Every entity/relation in the payload is upserted (added if
    new, replaced in place if an id already exists) — anything not mentioned
    is left untouched (this is a scoped merge-replace, not a whole-graph
    replace).

`apply_patch` always operates on a private copy of the input snapshot's graph
(`Snapshot.to_graph()` already deep-copies); the input `Snapshot` is immutable
and is never mutated. The returned `Snapshot` is content-addressed afresh via
`Snapshot.from_graph` (contract §2.4 `compute_snapshot_id`).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from gameforge.contracts.findings import Patch, TypedOp
from gameforge.contracts.ir import Entity, Relation
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import GraphDiff, IRGraph


class PatchRejected(Exception):
    """Raised whenever a patch cannot be safely applied — rebase-or-reject.

    Never raised as a partial-apply signal: by construction, every check that
    can raise this happens either before any op is applied (preconditions) or
    immediately before the offending op is applied (old_value / malformed op),
    so the caller's copy-in-progress is simply discarded.
    """

    def __init__(self, reason: str, op_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.op_id = op_id


def _split_target(target: str) -> tuple[str, str]:
    """Split "<id>.<path>" on the FIRST '.' (ids use ':', never '.')."""
    entity_id, _, path = target.partition(".")
    return entity_id, path


def _get_nested(d: dict[str, Any], path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_nested(d: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _check_preconditions(graph: IRGraph, preconditions: list[dict[str, Any]]) -> None:
    for cond in preconditions:
        kind = cond.get("kind")
        if kind == "entity_exists":
            if graph.get_node(cond["id"]) is None:
                raise PatchRejected(
                    f"precondition failed: entity_exists({cond['id']!r})", None
                )
        elif kind == "attr_equals":
            entity_id, path = _split_target(cond["target"])
            node = graph.get_node(entity_id)
            if node is None:
                raise PatchRejected(
                    f"precondition failed: attr_equals target entity "
                    f"{entity_id!r} does not exist", None
                )
            current = _get_nested(node.attrs, path) if path else None
            if current != cond["value"]:
                raise PatchRejected(
                    f"precondition failed: attr_equals({cond['target']!r}) "
                    f"expected {cond['value']!r}, found {current!r}", None
                )
        else:
            raise PatchRejected(f"precondition failed: unknown kind {kind!r}", None)


def _current_value(graph: IRGraph, op: TypedOp) -> Any:
    if op.op == "set_entity_attr":
        entity_id, path = _split_target(op.target)
        node = graph.get_node(entity_id)
        return _get_nested(node.attrs, path) if node is not None and path else None
    if op.op == "set_relation_attr":
        relation_id, path = _split_target(op.target)
        rel = graph.get_relation(relation_id)
        if rel is None or rel.attrs is None or not path:
            return None
        return _get_nested(rel.attrs, path)
    if op.op in ("add_entity", "delete_entity"):
        node = graph.get_node(op.target)
        return node.model_dump() if node is not None else None
    if op.op in ("add_relation", "delete_relation"):
        rel = graph.get_relation(op.target)
        return rel.model_dump() if rel is not None else None
    if op.op == "replace_subgraph":
        payload = op.new_value or {}
        entity_ids = {e["id"] for e in payload.get("entities", [])}
        relation_ids = {r["id"] for r in payload.get("relations", [])}
        return {
            "entities": {
                eid: graph.get_node(eid).model_dump()
                for eid in sorted(entity_ids) if graph.get_node(eid) is not None
            },
            "relations": {
                rid: graph.get_relation(rid).model_dump()
                for rid in sorted(relation_ids) if graph.get_relation(rid) is not None
            },
        }
    raise PatchRejected(f"unknown op kind: {op.op!r}", op.op_id)


def _build_entity(entity_id: str, payload: dict[str, Any], op_id: str | None) -> Entity:
    data = dict(payload)
    data.setdefault("id", entity_id)
    try:
        return Entity(**data)
    except ValidationError as exc:
        raise PatchRejected(f"invalid entity payload for {entity_id!r}: {exc}", op_id) from exc


def _build_relation(relation_id: str, payload: dict[str, Any], op_id: str | None) -> Relation:
    data = dict(payload)
    data.setdefault("id", relation_id)
    try:
        return Relation(**data)
    except ValidationError as exc:
        raise PatchRejected(f"invalid relation payload for {relation_id!r}: {exc}", op_id) from exc


def _apply_op(graph: IRGraph, op: TypedOp) -> None:
    if op.op == "add_entity":
        if graph.get_node(op.target) is not None:
            raise PatchRejected(f"add_entity: entity already exists: {op.target!r}", op.op_id)
        graph.add_entity(_build_entity(op.target, op.new_value or {}, op.op_id))

    elif op.op == "delete_entity":
        if graph.get_node(op.target) is None:
            raise PatchRejected(f"delete_entity: entity not found: {op.target!r}", op.op_id)
        graph.remove_entity(op.target)

    elif op.op == "set_entity_attr":
        entity_id, path = _split_target(op.target)
        node = graph.get_node(entity_id)
        if node is None:
            raise PatchRejected(
                f"set_entity_attr: entity not found: {entity_id!r}", op.op_id
            )
        if not path:
            raise PatchRejected(
                f"set_entity_attr: target missing attr path: {op.target!r}", op.op_id
            )
        attrs = dict(node.attrs)
        _set_nested(attrs, path, op.new_value)
        graph.add_entity(node.model_copy(update={"attrs": attrs}))

    elif op.op == "add_relation":
        if graph.get_relation(op.target) is not None:
            raise PatchRejected(f"add_relation: relation already exists: {op.target!r}", op.op_id)
        graph.add_relation(_build_relation(op.target, op.new_value or {}, op.op_id))

    elif op.op == "delete_relation":
        if graph.get_relation(op.target) is None:
            raise PatchRejected(f"delete_relation: relation not found: {op.target!r}", op.op_id)
        graph.remove_relation(op.target)

    elif op.op == "set_relation_attr":
        relation_id, path = _split_target(op.target)
        rel = graph.get_relation(relation_id)
        if rel is None:
            raise PatchRejected(
                f"set_relation_attr: relation not found: {relation_id!r}", op.op_id
            )
        if not path:
            raise PatchRejected(
                f"set_relation_attr: target missing attr path: {op.target!r}", op.op_id
            )
        attrs = dict(rel.attrs or {})
        _set_nested(attrs, path, op.new_value)
        updated = rel.model_copy(update={"attrs": attrs})
        graph.remove_relation(relation_id)
        graph.add_relation(updated)

    elif op.op == "replace_subgraph":
        payload = op.new_value or {}
        for entity_payload in payload.get("entities", []):
            eid = entity_payload["id"]
            graph.add_entity(_build_entity(eid, entity_payload, op.op_id))
        for relation_payload in payload.get("relations", []):
            rid = relation_payload["id"]
            if graph.get_relation(rid) is not None:
                graph.remove_relation(rid)
            graph.add_relation(_build_relation(rid, relation_payload, op.op_id))

    else:
        raise PatchRejected(f"unknown op kind: {op.op!r}", op.op_id)


def apply_patch(snapshot: Snapshot, patch: Patch) -> Snapshot:
    """Apply `patch.ops` to a COPY of `snapshot`'s graph; return a new Snapshot.

    Never mutates `snapshot`. Raises `PatchRejected` (rebase-or-reject) if any
    precondition fails, any op's `old_value` no longer matches the current
    value, or any op is otherwise malformed/inapplicable.
    """
    graph = snapshot.to_graph()  # already a deep copy — `snapshot` stays untouched

    _check_preconditions(graph, patch.preconditions)

    for op in patch.ops:
        if op.old_value is not None:
            current = _current_value(graph, op)
            if current != op.old_value:
                raise PatchRejected(
                    f"old_value mismatch for op {op.op_id!r} ({op.op} {op.target!r}): "
                    f"expected {op.old_value!r}, found {current!r}",
                    op.op_id,
                )
        _apply_op(graph, op)

    return Snapshot.from_graph(graph, parent_id=snapshot.snapshot_id)


def dry_run(snapshot: Snapshot, patch: Patch) -> GraphDiff:
    """Apply `patch` to a copy and return the reviewable diff (contract §7.9).

    Lets `PatchRejected` propagate untouched; `snapshot` is never mutated.
    """
    old_graph = snapshot.to_graph()
    new_snapshot = apply_patch(snapshot, patch)
    return new_snapshot.to_graph().diff(old_graph)
