"""In-memory typed-property-graph store + logical query interface (contract §2.5).

Physical storage is in-memory for M0a; the full logical query interface is
implemented (not cut). A DB/graph backend is deferred to M0b. `path_exists` may
consult a spatial nav derived view (contract §2.3: `path_to` is a derived view)
via an injected `NavProvider` — Region-level IR edges are too coarse to prove
real reachability.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation


@runtime_checkable
class NavProvider(Protocol):
    """Spatial derived view injected by game/aureus (structural duck-typing)."""

    def reachable(self, src_pos: tuple[int, int], dst_pos: tuple[int, int]) -> bool: ...

    def reachable_positions(
        self,
        src_pos: tuple[int, int],
        positions: Iterable[tuple[int, int]],
    ) -> set[tuple[int, int]]: ...

    def pos_of(self, entity_id: str) -> tuple[int, int] | None: ...


class GraphDiff(BaseModel):
    added_entities: list[str] = Field(default_factory=list)
    removed_entities: list[str] = Field(default_factory=list)
    changed_entities: list[str] = Field(default_factory=list)
    added_relations: list[str] = Field(default_factory=list)
    removed_relations: list[str] = Field(default_factory=list)
    changed_relations: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [
                self.added_entities,
                self.removed_entities,
                self.changed_entities,
                self.added_relations,
                self.removed_relations,
                self.changed_relations,
            ]
        )


def _entity_content(e: Entity) -> str:
    return canonical_json(e.model_dump(exclude_none=True, exclude={"id"}))


def _relation_content(r: Relation) -> str:
    return canonical_json(r.model_dump(exclude_none=True, exclude={"id"}))


class IRGraph:
    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        self._relations: dict[str, Relation] = {}
        self._out: dict[str, list[str]] = {}  # src_id -> [relation_id]
        self._in: dict[str, list[str]] = {}  # dst_id -> [relation_id]

    # --- mutation ---
    def add_entity(self, e: Entity) -> None:
        self._entities[e.id] = e
        self._out.setdefault(e.id, [])
        self._in.setdefault(e.id, [])

    def add_relation(self, r: Relation) -> None:
        self._relations[r.id] = r
        self._out.setdefault(r.src_id, []).append(r.id)
        self._in.setdefault(r.dst_id, []).append(r.id)

    def remove_relation(self, relation_id: str) -> None:
        r = self._relations.pop(relation_id, None)
        if r is None:
            return
        if relation_id in self._out.get(r.src_id, []):
            self._out[r.src_id].remove(relation_id)
        if relation_id in self._in.get(r.dst_id, []):
            self._in[r.dst_id].remove(relation_id)

    def remove_entity(self, entity_id: str) -> None:
        for rid in [
            r.id for r in self._relations.values() if r.src_id == entity_id or r.dst_id == entity_id
        ]:
            self.remove_relation(rid)
        self._entities.pop(entity_id, None)
        self._out.pop(entity_id, None)
        self._in.pop(entity_id, None)

    # --- query interface (contract §2.5) ---
    def get_node(self, entity_id: str) -> Entity | None:
        return self._entities.get(entity_id)

    def get_relation(self, relation_id: str) -> Relation | None:
        return self._relations.get(relation_id)

    def neighbors(
        self, entity_id: str, edge_type: EdgeType | None = None, direction: str = "out"
    ) -> list[Relation]:
        index = self._out if direction == "out" else self._in
        rels = [self._relations[rid] for rid in index.get(entity_id, [])]
        if edge_type is not None:
            rels = [r for r in rels if r.type is edge_type]
        return rels

    def nodes_of_type(self, node_type: NodeType) -> list[Entity]:
        return [e for e in self._entities.values() if e.type is node_type]

    def all_entities(self) -> Iterable[Entity]:
        return self._entities.values()

    def all_relations(self) -> Iterable[Relation]:
        return self._relations.values()

    def subgraph(self, types: set[NodeType]) -> "IRGraph":
        sub = IRGraph()
        keep = {e.id for e in self._entities.values() if e.type in types}
        for eid in keep:
            sub.add_entity(self._entities[eid])
        for r in self._relations.values():
            if r.src_id in keep and r.dst_id in keep:
                sub.add_relation(r)
        return sub

    def path_exists(
        self,
        src: str,
        dst: str,
        via: EdgeType | None = None,
        nav: NavProvider | None = None,
    ) -> bool:
        # Spatial reachability via the derived nav view when both endpoints have positions.
        if nav is not None:
            sp, dp = nav.pos_of(src), nav.pos_of(dst)
            if sp is not None and dp is not None:
                return nav.reachable(sp, dp)
        # Otherwise BFS over IR edges (optionally filtered by edge type).
        if src not in self._entities or dst not in self._entities:
            return False
        seen = {src}
        q: deque[str] = deque([src])
        while q:
            cur = q.popleft()
            if cur == dst:
                return True
            for r in self.neighbors(cur, via, direction="out"):
                if r.dst_id not in seen:
                    seen.add(r.dst_id)
                    q.append(r.dst_id)
        return False

    def diff(self, other: "IRGraph") -> GraphDiff:
        """Diff self against `other` (self is 'new', other is 'base')."""
        d = GraphDiff()
        for eid, e in self._entities.items():
            if eid not in other._entities:
                d.added_entities.append(eid)
            elif _entity_content(e) != _entity_content(other._entities[eid]):
                d.changed_entities.append(eid)
        for eid in other._entities:
            if eid not in self._entities:
                d.removed_entities.append(eid)
        for rid, r in self._relations.items():
            if rid not in other._relations:
                d.added_relations.append(rid)
            elif _relation_content(r) != _relation_content(other._relations[rid]):
                d.changed_relations.append(rid)
        for rid in other._relations:
            if rid not in self._relations:
                d.removed_relations.append(rid)
        for lst in (
            d.added_entities,
            d.removed_entities,
            d.changed_entities,
            d.added_relations,
            d.removed_relations,
            d.changed_relations,
        ):
            lst.sort()
        return d
