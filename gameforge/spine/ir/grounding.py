"""Deterministic grounding retrieval — the relevant slice of a graph, not all of it.

An extraction prompt used to carry the whole content graph: every entity with its
full ``attrs`` and every relation, unbounded, re-sent on every model call.  Two
things follow from that and neither is hypothetical.  Signal is diluted — a
planning document about one region competes with every other region for the
model's attention.  And size is a hard failure: an oversized prompt message is an
``IntegrityViolation`` at routing, so a large enough game cannot be extracted at
all.

This module answers "which part of the graph is this text about" **without a
model**.  A person declaring 岩王帝君 ≡ 钟离 is what bridges names that share no
characters, and that declaration is already recorded — so the semantic step a
vector index would be reached for is here as an exact, auditable fact instead.
Nothing here is stochastic, so a retrieved slice is reproducible from the same
artifacts forever.

Everything below is a pure function of ``(snapshot, query, declared aliases,
budget)``.  Retained prompt bytes must stay reconstructible, so no hidden state
of any kind may enter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from gameforge.contracts.ir import EdgeType, Entity, Relation
from gameforge.spine.identity_normalization import (
    build_identity_alias_index,
    canonical_identity_reference,
)
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph
from gameforge.spine.text_fold import fold_for_match

GROUNDING_RETRIEVAL_POLICY_VERSION = "grounding-retrieval@1"

# A one-character surface form matches almost any prose, which would make every
# entity a seed and defeat the point.  This threshold belongs to the policy
# version, not to a profile: changing it changes what "relevant" means.
MIN_SURFACE_FORM_CHARS = 2


@dataclass(frozen=True, slots=True)
class GroundingProjectionBudget:
    """How much of a focus neighbourhood one context may carry."""

    max_focus_entities: int
    max_incident_relations: int
    max_neighbor_entities: int
    max_catalog_ids_per_type: int


@dataclass(frozen=True, slots=True)
class GroundingBudget:
    """The projection caps plus the byte ceiling the rendered slice must fit."""

    projection: GroundingProjectionBudget
    max_grounding_bytes: int


@dataclass(frozen=True, slots=True)
class GroundingSlice:
    """One rendered grounding context and an honest account of what it omits."""

    focus_nodes: tuple[Mapping[str, object], ...]
    incident_relations: tuple[Mapping[str, object], ...]
    neighbor_nodes: tuple[Mapping[str, object], ...]
    entity_catalog: Mapping[str, tuple[str, ...]]
    edge_types: tuple[str, ...]
    matched_entity_ids: tuple[str, ...]
    omitted: Mapping[str, int]
    complete: bool
    policy_version: str = GROUNDING_RETRIEVAL_POLICY_VERSION

    def to_prompt_json(self) -> str:
        """Render the exact bytes a prompt embeds.

        ``ensure_ascii=False`` because this product's content is Chinese and
        escaping every character to ``\\uXXXX`` triples the grounding cost for no
        reader, human or model.  Any byte accounting must therefore measure the
        UTF-8 encoding, never ``len`` of the string.
        """

        return json.dumps(self._payload(), sort_keys=True, ensure_ascii=False, default=str)

    def _payload(self) -> dict[str, object]:
        return {
            "focus_nodes": list(self.focus_nodes),
            "incident_relations": list(self.incident_relations),
            "neighbor_nodes": list(self.neighbor_nodes),
            "entity_catalog": {key: list(value) for key, value in self.entity_catalog.items()},
            "edge_types": list(self.edge_types),
            "complete": self.complete,
        }


def _focus_node(entity: Entity) -> dict[str, object]:
    return {"id": entity.id, "type": entity.type.value, "attrs": entity.attrs}


def _neighbor_node(entity: Entity) -> dict[str, object]:
    """A neighbour is context, not content: enough to name it, not to copy it."""

    return {"id": entity.id, "type": entity.type.value, "name": entity.attrs.get("name")}


def _relation_view(relation: Relation) -> dict[str, object]:
    return {
        "id": relation.id,
        "type": relation.type.value,
        "src_id": relation.src_id,
        "dst_id": relation.dst_id,
    }


def project_focus_context(
    graph: IRGraph,
    focus_ids: Sequence[str],
    *,
    max_catalog_ids_per_type: int,
) -> dict[str, object]:
    """Project one focus set and its 1-hop neighbourhood.

    ``focus_ids`` order is authoritative — the caller ranks, this projects — so a
    byte ceiling can drop the least relevant focus first.
    """

    focus_set = {entity_id for entity_id in focus_ids if graph.get_node(entity_id) is not None}
    focus_nodes: list[dict[str, object]] = []
    for entity_id in focus_ids:
        node = graph.get_node(entity_id)
        if node is not None:
            focus_nodes.append(_focus_node(node))

    incident_relations: list[dict[str, object]] = []
    neighbor_ids: set[str] = set()
    for relation in graph.all_relations():
        if relation.src_id in focus_set or relation.dst_id in focus_set:
            incident_relations.append(_relation_view(relation))
            neighbor_ids.add(relation.src_id)
            neighbor_ids.add(relation.dst_id)
    neighbor_ids -= focus_set

    neighbor_nodes: list[dict[str, object]] = []
    for neighbor_id in sorted(neighbor_ids):
        node = graph.get_node(neighbor_id)
        if node is not None:
            neighbor_nodes.append(_neighbor_node(node))

    entity_catalog: dict[str, list[str]] = {}
    for entity in graph.all_entities():
        bucket = entity_catalog.setdefault(entity.type.value, [])
        if len(bucket) < max_catalog_ids_per_type:
            bucket.append(entity.id)

    return {
        "focus_nodes": focus_nodes,
        "incident_relations": incident_relations,
        "neighbor_nodes": neighbor_nodes,
        "entity_catalog": entity_catalog,
        "edge_types": [edge_type.value for edge_type in EdgeType],
    }


@dataclass(frozen=True, slots=True)
class _SurfaceForm:
    folded: str
    entity_id: str
    declared: bool


@dataclass(frozen=True, slots=True)
class _Candidate:
    entity_id: str
    declared: bool
    best_length: int
    occurrences: int


class GroundingRetriever:
    """One snapshot indexed once; every query reads the same immutable index.

    The index is built in sorted-id order so a truncated catalog is a function of
    the graph's CONTENT, not of the order a dict happened to be populated in.  Two
    snapshots holding the same entities therefore ground identically.
    """

    __slots__ = ("_graph", "_surface_forms", "_sorted_entity_ids")

    def __init__(
        self,
        snapshot: Snapshot,
        *,
        declared_aliases: Mapping[str, str] | None = None,
    ) -> None:
        graph = IRGraph()
        # Deliberately not `Snapshot.to_graph()`: that deep-copies every entity
        # and relation, and the caller builds one retriever per Run but queries it
        # once per material chunk.  Retrieval only ever reads.
        self._sorted_entity_ids = tuple(sorted(snapshot.entities))
        for entity_id in self._sorted_entity_ids:
            graph.add_entity(snapshot.entities[entity_id])
        for relation_id in sorted(snapshot.relations):
            graph.add_relation(snapshot.relations[relation_id])
        self._graph = graph
        self._surface_forms = self._build_surface_forms(snapshot, declared_aliases)

    @staticmethod
    def _build_surface_forms(
        snapshot: Snapshot,
        declared_aliases: Mapping[str, str] | None,
    ) -> tuple[_SurfaceForm, ...]:
        # Its own index: `normalize_typed_ops` mutates the one it is handed, and a
        # shared index would make one chunk's grounding depend on the previous
        # chunk's model output.
        index = build_identity_alias_index(snapshot, declared_aliases=declared_aliases)
        declared_by_entity: dict[str, set[str]] = {}
        for alias, entity_id in (declared_aliases or {}).items():
            declared_by_entity.setdefault(entity_id, set()).add(alias)

        forms: dict[tuple[str, str], bool] = {}

        def offer(raw: str, entity_id: str, *, declared: bool) -> None:
            folded = fold_for_match(raw)
            if len(folded) < MIN_SURFACE_FORM_CHARS:
                return
            key = (folded, entity_id)
            forms[key] = forms.get(key, False) or declared

        for entity_id in sorted(index.existing_ids):
            entity = snapshot.entities[entity_id]
            offer(entity_id, entity_id, declared=False)
            offer(
                canonical_identity_reference(entity_id).rsplit(":", 1)[-1],
                entity_id,
                declared=False,
            )
            name = entity.attrs.get("name")
            if isinstance(name, str):
                offer(name, entity_id, declared=False)
            for alias in sorted(declared_by_entity.get(entity_id, ())):
                offer(alias, entity_id, declared=True)

        return tuple(
            _SurfaceForm(folded=folded, entity_id=entity_id, declared=declared)
            for (folded, entity_id), declared in sorted(forms.items())
        )

    def retrieve(self, query_text: str, *, budget: GroundingBudget) -> GroundingSlice:
        """Return the slice of this snapshot that ``query_text`` is about."""

        focus_ids, matched = self._seeds(query_text, budget.projection.max_focus_entities)
        context = project_focus_context(
            self._graph,
            focus_ids,
            max_catalog_ids_per_type=budget.projection.max_catalog_ids_per_type,
        )
        if not focus_ids:
            # Nothing matched: an empty graph, or material describing content that
            # does not exist yet. Show real ids and real names anyway, so the model
            # extends the taxonomy already in use instead of inventing a parallel one.
            context["neighbor_nodes"] = [
                _neighbor_node(self._graph.get_node(entity_id))  # type: ignore[arg-type]
                for entity_id in self._sorted_entity_ids[: budget.projection.max_neighbor_entities]
            ]
        return self._fit(context, matched=matched, budget=budget)

    def _seeds(self, query_text: str, limit: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
        folded_query = fold_for_match(query_text)
        if not folded_query:
            return (), ()
        best: dict[str, _Candidate] = {}
        for form in self._surface_forms:
            occurrences = folded_query.count(form.folded)
            if occurrences == 0:
                continue
            current = best.get(form.entity_id)
            candidate = _Candidate(
                entity_id=form.entity_id,
                declared=form.declared or (current.declared if current else False),
                best_length=max(len(form.folded), current.best_length if current else 0),
                occurrences=(
                    occurrences
                    if current is None or len(form.folded) > current.best_length
                    else current.occurrences
                ),
            )
            best[form.entity_id] = candidate

        ranked = sorted(
            best.values(),
            # A person said these two names are one thing. That outranks every
            # lexical signal, which is why `declared` leads. `entity_id` is unique,
            # so the order is total and the slice is reproducible.
            key=lambda item: (
                not item.declared,
                -item.best_length,
                -item.occurrences,
                item.entity_id,
            ),
        )
        matched = tuple(item.entity_id for item in ranked)
        return matched[:limit], matched

    def _fit(
        self,
        context: dict[str, object],
        *,
        matched: tuple[str, ...],
        budget: GroundingBudget,
    ) -> GroundingSlice:
        focus = list(context["focus_nodes"])  # type: ignore[arg-type]
        relations = list(context["incident_relations"])  # type: ignore[arg-type]
        neighbors = list(context["neighbor_nodes"])  # type: ignore[arg-type]
        catalog = {
            key: tuple(value)
            for key, value in sorted(context["entity_catalog"].items())  # type: ignore[union-attr]
        }
        edge_types = tuple(context["edge_types"])  # type: ignore[arg-type]

        omitted: dict[str, int] = {}

        def drop(section: str, count: int) -> None:
            if count > 0:
                omitted[section] = omitted.get(section, 0) + count

        caps = budget.projection
        drop("incident_relations", max(0, len(relations) - caps.max_incident_relations))
        relations = relations[: caps.max_incident_relations]
        drop("neighbor_nodes", max(0, len(neighbors) - caps.max_neighbor_entities))
        neighbors = neighbors[: caps.max_neighbor_entities]
        drop("matched_entity_ids", max(0, len(matched) - caps.max_focus_entities))

        # Count caps cannot bound bytes: one focus node carries unbounded attrs.
        # Without this ladder the unbounded prompt would only have moved, not gone.
        while True:
            candidate = _slice_of(focus, relations, neighbors, catalog, edge_types, omitted)
            if len(candidate.to_prompt_json().encode("utf-8")) <= budget.max_grounding_bytes:
                return _slice_of(
                    focus, relations, neighbors, catalog, edge_types, omitted, matched=matched
                )
            if neighbors:
                neighbors.pop()
                drop("neighbor_nodes", 1)
            elif relations:
                relations.pop()
                drop("incident_relations", 1)
            elif focus:
                # Lowest-ranked first — this is why focus arrives in rank order.
                focus.pop()
                drop("focus_nodes", 1)
            else:
                # The catalog alone is over budget. It is the last honest signal of
                # what exists, so it is never dropped; the caller's ceiling is too
                # small for this graph and must say so rather than lie.
                return _slice_of(
                    focus, relations, neighbors, catalog, edge_types, omitted, matched=matched
                )


def _slice_of(
    focus: list[Mapping[str, object]],
    relations: list[Mapping[str, object]],
    neighbors: list[Mapping[str, object]],
    catalog: Mapping[str, tuple[str, ...]],
    edge_types: tuple[str, ...],
    omitted: Mapping[str, int],
    *,
    matched: tuple[str, ...] = (),
) -> GroundingSlice:
    return GroundingSlice(
        focus_nodes=tuple(focus),
        incident_relations=tuple(relations),
        neighbor_nodes=tuple(neighbors),
        entity_catalog=catalog,
        edge_types=edge_types,
        matched_entity_ids=matched,
        omitted=dict(omitted),
        complete=not omitted,
    )


__all__ = [
    "GROUNDING_RETRIEVAL_POLICY_VERSION",
    "GroundingBudget",
    "GroundingProjectionBudget",
    "GroundingRetriever",
    "GroundingSlice",
    "MIN_SURFACE_FORM_CHARS",
    "project_focus_context",
]
