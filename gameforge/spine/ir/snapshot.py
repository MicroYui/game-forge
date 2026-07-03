"""Immutable, content-addressed IR snapshots (contract §2.4–§2.5).

snapshot_id = sha256(canonical_json(content_payload)); content_payload excludes
non-content fields (parent_id / created_at / author / snapshot_id). Unordered
collections (entities, relations) are keyed by id so canonical key-sorting makes
the hash order-independent.
"""

from __future__ import annotations

from gameforge.contracts.canonical import compute_snapshot_id
from gameforge.contracts.ir import Entity, Relation
from gameforge.contracts.versions import META_SCHEMA_VERSION
from gameforge.spine.ir.store import IRGraph


class Snapshot:
    def __init__(
        self,
        entities: dict[str, Entity],
        relations: dict[str, Relation],
        parent_id: str | None = None,
        meta_schema_version: str = META_SCHEMA_VERSION,
        created_at: str | None = None,
        author: str | None = None,
    ) -> None:
        self.entities = entities
        self.relations = relations
        self.parent_id = parent_id
        self.meta_schema_version = meta_schema_version
        self.created_at = created_at
        self.author = author
        self.snapshot_id = compute_snapshot_id(self.content_payload)

    @property
    def content_payload(self) -> dict:
        return {
            "meta_schema_version": self.meta_schema_version,
            "entities": {
                eid: e.model_dump(exclude_none=True, exclude={"id"})
                for eid, e in self.entities.items()
            },
            "relations": {
                rid: r.model_dump(exclude_none=True, exclude={"id"})
                for rid, r in self.relations.items()
            },
        }

    @classmethod
    def from_graph(
        cls,
        graph: IRGraph,
        parent_id: str | None = None,
        created_at: str | None = None,
        author: str | None = None,
    ) -> "Snapshot":
        entities = {e.id: e.model_copy(deep=True) for e in graph.all_entities()}
        relations = {r.id: r.model_copy(deep=True) for r in graph.all_relations()}
        return cls(entities, relations, parent_id=parent_id,
                   created_at=created_at, author=author)

    def to_graph(self) -> IRGraph:
        g = IRGraph()
        for e in self.entities.values():
            g.add_entity(e.model_copy(deep=True))
        for r in self.relations.values():
            g.add_relation(r.model_copy(deep=True))
        return g
