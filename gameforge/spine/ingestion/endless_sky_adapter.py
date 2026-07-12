"""Lossless Endless Sky DataFile adapter into the source-neutral Spec-IR."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass

from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation, SourceRef
from gameforge.spine.ingestion.endless_sky_reader import (
    READER_VERSION,
    DataNode,
    EndlessSkyTree,
    TopLevelChunk,
    top_level_chunks,
)
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph


ADAPTER_VERSION = "endless-sky-adapter@1"
_ADAPTER_ID = "endless_sky"
_OFFER_TRIGGERS = frozenset(
    {"landing", "job", "assisting", "boarding", "entering", "spaceport"}
)
_MISSION_STATE = re.compile(r"^(.+): (offered|done)$")


class EndlessSkyAdapterError(ValueError):
    """Raised when source records cannot be mapped or restored losslessly."""


@dataclass(frozen=True)
class EndlessSkyTarget:
    path: str
    record_kind: str
    record_name: str


@dataclass(frozen=True)
class EndlessSkyResource:
    kind: str
    name: str


@dataclass(frozen=True)
class EndlessSkyContext:
    resources: tuple[EndlessSkyResource, ...]
    restricted_destinations: tuple[str, ...]


def quest_id(name: str) -> str:
    return f"quest:endless-sky:{name}"


def region_id(name: str) -> str:
    return f"region:endless-sky:{name}"


def dialogue_label_id(quest: str, label: str) -> str:
    digest = _digest("dialogue-label", quest, label)
    return f"dialogue:endless-sky:{digest}"


def _effect_id(name: str) -> str:
    return f"effect:endless-sky:{name}"


def _gate_id(destination: str) -> str:
    return f"unlock-condition:endless-sky:landing-access:{destination}"


def _digest(*parts: object) -> str:
    payload = "\x00".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _raw_holder_id(path: str, row: int) -> str:
    return f"event:endless-sky:raw:{_digest(path, row)}"


def _step_id(chunk: TopLevelChunk) -> str:
    return f"quest-step:endless-sky:{_digest(chunk.path, chunk.index, chunk.name, 'lifecycle')}"


def _trigger_id(chunk: TopLevelChunk, node: DataNode, trigger: str) -> str:
    return (
        "event:endless-sky:offer-trigger:"
        f"{_digest(chunk.path, node.source_span.start_line, chunk.name, trigger)}"
    )


def _values(node: DataNode) -> tuple[str, ...]:
    return tuple(token.value for token in node.tokens)


def _descendants(node: DataNode):
    for child in node.children:
        yield child
        yield from _descendants(child)


def _direct_children(node: DataNode, token: str):
    return (child for child in node.children if _values(child)[:1] == (token,))


def _source_ref(chunk: TopLevelChunk, node: DataNode | None = None) -> SourceRef:
    return SourceRef(
        adapter=_ADAPTER_ID,
        file=chunk.path,
        sheet=chunk.kind,
        row=chunk.index,
        column=(f"line:{node.source_span.start_line}" if node is not None else None),
    )


def _raw_attrs(chunk: TopLevelChunk) -> dict[str, object]:
    return {
        "source_kind": chunk.kind,
        "source_name": chunk.name,
        "source_order": chunk.index,
        "source_chunk_b64": base64.b64encode(chunk.raw).decode("ascii"),
        "reader_version": READER_VERSION,
    }


def _mission_state_conditions(node: DataNode) -> tuple[tuple[str, DataNode], ...]:
    conditions: list[tuple[str, DataNode]] = []
    for offer in _direct_children(node, "to"):
        values = _values(offer)
        if len(values) < 2 or values[1] != "offer":
            continue
        for condition in _descendants(offer):
            condition_values = _values(condition)
            if len(condition_values) < 2 or condition_values[0] != "has":
                continue
            match = _MISSION_STATE.fullmatch(condition_values[1])
            if match is not None:
                conditions.append((match.group(1), condition))
    return tuple(conditions)


def _landing_access_conditions(node: DataNode) -> tuple[tuple[str, DataNode], ...]:
    conditions: list[tuple[str, DataNode]] = []
    for offer in _direct_children(node, "to"):
        values = _values(offer)
        if len(values) < 2 or values[1] != "offer":
            continue
        for condition in _descendants(offer):
            condition_values = _values(condition)
            if (
                len(condition_values) >= 2
                and condition_values[0] == "has"
                and condition_values[1].startswith("landing access: ")
            ):
                conditions.append(
                    (condition_values[1].removeprefix("landing access: "), condition)
                )
    return tuple(conditions)


class _GraphBuilder:
    def __init__(self) -> None:
        self.graph = IRGraph()
        self._relation_ids: set[str] = set()

    def add_entity(self, entity: Entity) -> None:
        existing = self.graph.get_node(entity.id)
        if existing is None:
            self.graph.add_entity(entity)
            return
        if existing.type is not entity.type:
            raise EndlessSkyAdapterError(
                f"entity id {entity.id!r} maps to both {existing.type.value} and {entity.type.value}"
            )

    def add_relation(
        self,
        edge_type: EdgeType,
        src_id: str,
        dst_id: str,
        *,
        chunk: TopLevelChunk,
        node: DataNode,
        role: str,
        attrs: dict[str, object] | None = None,
    ) -> None:
        base = f"rel:endless-sky:{_digest(edge_type.value, src_id, dst_id, chunk.path, node.source_span.start_line, role)}"
        relation_id = base
        ordinal = 1
        while relation_id in self._relation_ids:
            relation_id = f"{base}:{ordinal}"
            ordinal += 1
        self._relation_ids.add(relation_id)
        self.graph.add_relation(
            Relation(
                id=relation_id,
                type=edge_type,
                src_id=src_id,
                dst_id=dst_id,
                attrs=attrs,
                source_ref=_source_ref(chunk, node),
            )
        )


class EndlessSkyTxtAdapter:
    format_id = _ADAPTER_ID
    adapter_version = ADAPTER_VERSION

    def to_ir(
        self,
        tree: EndlessSkyTree,
        *,
        targets: tuple[EndlessSkyTarget, ...],
        context: EndlessSkyContext,
    ) -> Snapshot:
        if tree.reader_version != READER_VERSION:
            raise EndlessSkyAdapterError(f"unsupported reader version: {tree.reader_version}")

        chunks = tuple(
            chunk
            for data_file in tree.files
            for chunk in top_level_chunks(data_file)
        )
        by_key: dict[tuple[str, str, str], TopLevelChunk] = {}
        duplicate_keys: set[tuple[str, str, str]] = set()
        missions_by_name: dict[str, list[TopLevelChunk]] = {}
        for chunk in chunks:
            key = (chunk.path, chunk.kind, chunk.name)
            if key in by_key:
                duplicate_keys.add(key)
            else:
                by_key[key] = chunk
            if chunk.kind == "mission" and chunk.node is not None:
                missions_by_name.setdefault(chunk.name, []).append(chunk)

        target_keys = {
            (target.path, target.record_kind, target.record_name)
            for target in targets
        }
        if len(target_keys) != len(targets):
            raise EndlessSkyAdapterError("targets must be unique")
        missing_targets = sorted(target_keys - by_key.keys())
        if missing_targets:
            raise EndlessSkyAdapterError(f"target records are missing: {missing_targets}")
        ambiguous_targets = sorted(target_keys & duplicate_keys)
        if ambiguous_targets:
            raise EndlessSkyAdapterError(f"target records are ambiguous: {ambiguous_targets}")

        semantic_missions = {
            key for key in target_keys if key[1] == "mission"
        }
        dependency_missions: set[tuple[str, str, str]] = set()
        for key in sorted(semantic_missions):
            chunk = by_key[key]
            if chunk.node is None:
                continue
            for dependency_name, _ in _mission_state_conditions(chunk.node):
                matches = missions_by_name.get(dependency_name, [])
                if len(matches) > 1:
                    raise EndlessSkyAdapterError(
                        f"mission dependency {dependency_name!r} is ambiguous"
                    )
                if matches:
                    dependency = matches[0]
                    dependency_missions.add(
                        (dependency.path, dependency.kind, dependency.name)
                    )
        semantic_missions |= dependency_missions

        builder = _GraphBuilder()
        selected_effects = {key for key in target_keys if key[1] == "effect"}
        for chunk in chunks:
            key = (chunk.path, chunk.kind, chunk.name)
            if key in semantic_missions:
                entity_id = quest_id(chunk.name)
                node_type = NodeType.QUEST
            elif key in selected_effects:
                entity_id = _effect_id(chunk.name)
                node_type = NodeType.EFFECT
            else:
                entity_id = _raw_holder_id(chunk.path, chunk.index)
                node_type = NodeType.EVENT
            builder.add_entity(
                Entity(
                    id=entity_id,
                    type=node_type,
                    attrs=_raw_attrs(chunk),
                    source_ref=_source_ref(chunk),
                )
            )

        restricted = frozenset(context.restricted_destinations)
        for key in sorted(semantic_missions):
            chunk = by_key[key]
            self._map_mission(
                builder,
                chunk,
                restricted_destinations=restricted,
                include_state_dependencies=key in target_keys,
            )

        return Snapshot.from_graph(builder.graph)

    def from_ir(self, snapshot: Snapshot) -> dict[str, bytes]:
        grouped: dict[str, dict[int, bytes]] = {}
        for entity in snapshot.entities.values():
            attrs = entity.attrs
            if "source_chunk_b64" not in attrs:
                continue
            if attrs.get("reader_version") != READER_VERSION:
                raise EndlessSkyAdapterError(
                    f"raw holder {entity.id!r} has an unsupported reader version"
                )
            source_ref = entity.source_ref
            if source_ref is None or source_ref.adapter != _ADAPTER_ID or not source_ref.file:
                raise EndlessSkyAdapterError(
                    f"raw holder {entity.id!r} has no Endless Sky source file"
                )
            order = attrs.get("source_order")
            if isinstance(order, bool) or not isinstance(order, int) or order < 0:
                raise EndlessSkyAdapterError(f"raw holder {entity.id!r} has invalid source_order")
            try:
                encoded = attrs["source_chunk_b64"]
                if not isinstance(encoded, str):
                    raise TypeError
                raw = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError, TypeError, binascii.Error) as exc:
                raise EndlessSkyAdapterError(
                    f"raw holder {entity.id!r} has invalid source_chunk_b64"
                ) from exc
            file_chunks = grouped.setdefault(source_ref.file, {})
            if order in file_chunks:
                raise EndlessSkyAdapterError(
                    f"source file {source_ref.file!r} has duplicate chunk order {order}"
                )
            file_chunks[order] = raw

        rendered: dict[str, bytes] = {}
        for path, file_chunks in sorted(grouped.items()):
            orders = sorted(file_chunks)
            if orders != list(range(len(orders))):
                raise EndlessSkyAdapterError(
                    f"source file {path!r} has non-contiguous chunk ordering: {orders}"
                )
            rendered[path] = b"".join(file_chunks[order] for order in orders)
        return rendered

    def _map_mission(
        self,
        builder: _GraphBuilder,
        chunk: TopLevelChunk,
        *,
        restricted_destinations: frozenset[str],
        include_state_dependencies: bool,
    ) -> None:
        node = chunk.node
        if node is None:
            raise EndlessSkyAdapterError(f"mission chunk {chunk.name!r} has no token tree")
        current_quest = quest_id(chunk.name)
        lifecycle_step = _step_id(chunk)
        builder.add_entity(
            Entity(
                id=lifecycle_step,
                type=NodeType.QUEST_STEP,
                attrs={"kind": "lifecycle"},
                source_ref=_source_ref(chunk, node),
            )
        )
        builder.add_relation(
            EdgeType.HAS_STEP,
            current_quest,
            lifecycle_step,
            chunk=chunk,
            node=node,
            role="lifecycle",
        )

        for child in node.children:
            values = _values(child)
            if not values:
                continue
            directive = values[0]
            if directive == "source":
                if len(values) >= 2:
                    start_id = region_id(values[1])
                    builder.add_entity(
                        Entity(
                            id=start_id,
                            type=NodeType.REGION,
                            attrs={"name": values[1]},
                            source_ref=_source_ref(chunk, child),
                        )
                    )
                else:
                    start_id = _trigger_id(chunk, child, directive)
                    builder.add_entity(
                        Entity(
                            id=start_id,
                            type=NodeType.EVENT,
                            attrs={"kind": "mission_offer_source"},
                            source_ref=_source_ref(chunk, child),
                        )
                    )
                builder.add_relation(
                    EdgeType.STARTS_AT,
                    current_quest,
                    start_id,
                    chunk=chunk,
                    node=child,
                    role="source",
                )
            elif directive in _OFFER_TRIGGERS:
                start_id = _trigger_id(chunk, child, directive)
                builder.add_entity(
                    Entity(
                        id=start_id,
                        type=NodeType.EVENT,
                        attrs={"kind": "mission_offer_trigger", "trigger": directive},
                        source_ref=_source_ref(chunk, child),
                    )
                )
                builder.add_relation(
                    EdgeType.STARTS_AT,
                    current_quest,
                    start_id,
                    chunk=chunk,
                    node=child,
                    role=f"trigger:{directive}",
                )

        destinations: list[tuple[str, DataNode]] = []
        for destination_node in _direct_children(node, "destination"):
            values = _values(destination_node)
            if len(values) < 2:
                continue
            destination = values[1]
            destinations.append((destination, destination_node))
            destination_id = region_id(destination)
            builder.add_entity(
                Entity(
                    id=destination_id,
                    type=NodeType.REGION,
                    attrs={"name": destination},
                    source_ref=_source_ref(chunk, destination_node),
                )
            )
            builder.add_relation(
                EdgeType.LOCATED_IN,
                lifecycle_step,
                destination_id,
                chunk=chunk,
                node=destination_node,
                role="destination",
            )
            if destination in restricted_destinations:
                self._map_gate(builder, chunk, destination_node, destination)

        clearance_nodes = tuple(_direct_children(node, "clearance"))
        for destination, _ in destinations:
            for clearance_node in clearance_nodes:
                gate = self._ensure_gate(builder, chunk, clearance_node, destination)
                builder.add_relation(
                    EdgeType.UNLOCKS,
                    current_quest,
                    gate,
                    chunk=chunk,
                    node=clearance_node,
                    role=f"clearance:{destination}",
                )

        for destination, condition in _landing_access_conditions(node):
            gate = self._ensure_gate(builder, chunk, condition, destination)
            builder.add_relation(
                EdgeType.REQUIRES,
                current_quest,
                gate,
                chunk=chunk,
                node=condition,
                role=f"landing-access:{destination}",
            )

        if include_state_dependencies:
            for dependency, condition in _mission_state_conditions(node):
                builder.add_relation(
                    EdgeType.REQUIRES,
                    current_quest,
                    quest_id(dependency),
                    chunk=chunk,
                    node=condition,
                    role=f"mission-state:{dependency}",
                )

    def _ensure_gate(
        self,
        builder: _GraphBuilder,
        chunk: TopLevelChunk,
        node: DataNode,
        destination: str,
    ) -> str:
        gate = _gate_id(destination)
        builder.add_entity(
            Entity(
                id=gate,
                type=NodeType.UNLOCK_CONDITION,
                attrs={"kind": "landing_access", "destination": destination},
                source_ref=_source_ref(chunk, node),
            )
        )
        return gate

    def _map_gate(
        self,
        builder: _GraphBuilder,
        chunk: TopLevelChunk,
        node: DataNode,
        destination: str,
    ) -> None:
        gate = self._ensure_gate(builder, chunk, node, destination)
        builder.add_relation(
            EdgeType.GATED_BY,
            region_id(destination),
            gate,
            chunk=chunk,
            node=node,
            role=f"restricted-destination:{destination}",
        )


__all__ = [
    "ADAPTER_VERSION",
    "EndlessSkyAdapterError",
    "EndlessSkyContext",
    "EndlessSkyResource",
    "EndlessSkyTarget",
    "EndlessSkyTxtAdapter",
    "dialogue_label_id",
    "quest_id",
    "region_id",
]
