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
_DIALOGUE_TERMINALS = frozenset(
    {"accept", "decline", "depart", "die", "explode", "launch"}
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


def _resource_id(kind: str, name: str) -> str:
    return f"effect:endless-sky:resource:{kind}:{name}"


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


def _dialogue_entry_id(quest: str, chunk: TopLevelChunk, conversation: DataNode) -> str:
    return (
        "dialogue:endless-sky:entry:"
        f"{_digest(quest, chunk.path, conversation.source_span.start_line)}"
    )


def _dialogue_option_id(quest: str, chunk: TopLevelChunk, option: DataNode) -> str:
    return (
        "dialogue:endless-sky:option:"
        f"{_digest(quest, chunk.path, option.source_span.start_line)}"
    )


def _unresolved_merge_id(
    quest: str,
    chunk: TopLevelChunk,
    option: DataNode,
    target: str,
) -> str:
    return (
        "dialogue:endless-sky:unresolved-merge:"
        f"{_digest(quest, chunk.path, option.source_span.start_line, target)}"
    )


def _values(node: DataNode) -> tuple[str, ...]:
    return tuple(token.value for token in node.tokens)


def _descendants(node: DataNode):
    for child in node.children:
        yield child
        yield from _descendants(child)


def _walk(node: DataNode):
    yield node
    yield from _descendants(node)


def _goto_nodes(node: DataNode) -> tuple[tuple[str, DataNode], ...]:
    return tuple(
        (values[1], candidate)
        for candidate in _walk(node)
        if len(values := _values(candidate)) >= 2 and values[0] == "goto"
    )


def _contains_terminal(node: DataNode) -> bool:
    return any(
        (values := _values(candidate))
        and values[0] in _DIALOGUE_TERMINALS
        for candidate in _walk(node)
    )


def _guard_flags(nodes: tuple[DataNode, ...]) -> set[str]:
    flags: set[str] = set()
    for node in nodes:
        for candidate in _walk(node):
            values = _values(candidate)
            if len(values) < 2 or values[:2] != ("to", "display"):
                continue
            for condition in _descendants(candidate):
                condition_values = _values(condition)
                if len(condition_values) >= 2 and condition_values[0] == "not":
                    flags.add(condition_values[1])
    return flags


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
                available_missions=frozenset(missions_by_name),
                include_state_dependencies=key in target_keys,
            )

        known_resources = frozenset(
            (resource.kind, resource.name) for resource in context.resources
        )
        for key in sorted(selected_effects):
            self._map_effect(
                builder,
                by_key[key],
                known_resources=known_resources,
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
        available_missions: frozenset[str],
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
                if dependency not in available_missions:
                    continue
                builder.add_relation(
                    EdgeType.REQUIRES,
                    current_quest,
                    quest_id(dependency),
                    chunk=chunk,
                    node=condition,
                    role=f"mission-state:{dependency}",
                )
            for conversation in _descendants(node):
                if _values(conversation)[:1] == ("conversation",):
                    self._map_conversation(builder, chunk, current_quest, conversation)

    def _map_effect(
        self,
        builder: _GraphBuilder,
        chunk: TopLevelChunk,
        *,
        known_resources: frozenset[tuple[str, str]],
    ) -> None:
        node = chunk.node
        if node is None:
            raise EndlessSkyAdapterError(f"effect chunk {chunk.name!r} has no token tree")
        effect = _effect_id(chunk.name)
        for candidate in _descendants(node):
            values = _values(candidate)
            if len(values) < 2 or values[0] != "sound":
                continue
            resource_kind = "sound"
            resource_name = values[1]
            resource = _resource_id(resource_kind, resource_name)
            if (resource_kind, resource_name) in known_resources:
                builder.add_entity(
                    Entity(
                        id=resource,
                        type=NodeType.EFFECT,
                        attrs={
                            "resource_kind": resource_kind,
                            "resource_name": resource_name,
                        },
                        source_ref=_source_ref(chunk, candidate),
                    )
                )
            builder.add_relation(
                EdgeType.REFERENCES,
                effect,
                resource,
                chunk=chunk,
                node=candidate,
                role=f"resource:{resource_kind}:{resource_name}",
            )

    def _map_conversation(
        self,
        builder: _GraphBuilder,
        chunk: TopLevelChunk,
        quest: str,
        conversation: DataNode,
    ) -> None:
        children = list(conversation.children)
        entry = _dialogue_entry_id(quest, chunk, conversation)
        builder.add_entity(
            Entity(
                id=entry,
                type=NodeType.DIALOGUE_NODE,
                attrs={"kind": "entry", "quest": quest},
                source_ref=_source_ref(chunk, conversation),
            )
        )

        label_nodes: dict[str, DataNode] = {}
        label_indexes: dict[str, int] = {}
        for index, child in enumerate(children):
            values = _values(child)
            if len(values) < 2 or values[0] != "label":
                continue
            label = values[1]
            if label in label_nodes:
                raise EndlessSkyAdapterError(
                    f"conversation in {chunk.name!r} contains duplicate label {label!r}"
                )
            label_nodes[label] = child
            label_indexes[label] = index
            builder.add_entity(
                Entity(
                    id=dialogue_label_id(quest, label),
                    type=NodeType.DIALOGUE_NODE,
                    attrs={"kind": "label", "label": label, "quest": quest},
                    source_ref=_source_ref(chunk, child),
                )
            )

        set_flags = self._label_set_flags(children, label_indexes)
        current_source = entry
        for index, child in enumerate(children):
            values = _values(child)
            if not values:
                continue
            if values[0] == "label" and len(values) >= 2:
                current_source = dialogue_label_id(quest, values[1])
                continue
            if values[0] == "choice":
                self._map_choice(
                    builder,
                    chunk,
                    quest,
                    current_source,
                    children,
                    index,
                    child,
                    set_flags,
                )
                continue
            for target, goto_node in _goto_nodes(child):
                self._add_dialogue_transition(
                    builder,
                    chunk,
                    current_source,
                    quest,
                    target,
                    goto_node,
                    path_nodes=(child,),
                    set_flags=set_flags,
                    role=f"goto:{target}",
                )

        blocks: list[tuple[int, str]] = [(-1, entry)]
        blocks.extend(
            (index, dialogue_label_id(quest, label))
            for label, index in sorted(label_indexes.items(), key=lambda item: item[1])
        )
        for block_index, (start, source) in enumerate(blocks[:-1]):
            next_start, next_label_id = blocks[block_index + 1]
            segment = tuple(children[start + 1 : next_start])
            if any(_values(node)[:1] == ("choice",) for node in segment):
                continue
            if any(_goto_nodes(node) or _contains_terminal(node) for node in segment):
                continue
            next_label_node = children[next_start]
            next_label = _values(next_label_node)[1]
            self._add_dialogue_transition(
                builder,
                chunk,
                source,
                quest,
                next_label,
                next_label_node,
                path_nodes=segment,
                set_flags=set_flags,
                role=f"fallthrough:{next_label_id}",
            )

    def _map_choice(
        self,
        builder: _GraphBuilder,
        chunk: TopLevelChunk,
        quest: str,
        source: str,
        siblings: list[DataNode],
        choice_index: int,
        choice: DataNode,
        set_flags: dict[str, set[str]],
    ) -> None:
        explicit_targets = {
            target
            for option in choice.children
            for target, _ in _goto_nodes(option)
        }
        fallthrough_collisions: dict[str, list[DataNode]] = {}
        for option in choice.children:
            option_id = _dialogue_option_id(quest, chunk, option)
            builder.add_entity(
                Entity(
                    id=option_id,
                    type=NodeType.DIALOGUE_NODE,
                    attrs={"kind": "choice_option", "quest": quest},
                    source_ref=_source_ref(chunk, option),
                )
            )
            builder.add_relation(
                EdgeType.PRECEDES,
                source,
                option_id,
                chunk=chunk,
                node=option,
                role=f"choice-option:{option.source_span.start_line}",
            )

            gotos = _goto_nodes(option)
            if gotos:
                for target, goto_node in gotos:
                    self._add_dialogue_transition(
                        builder,
                        chunk,
                        option_id,
                        quest,
                        target,
                        goto_node,
                        path_nodes=(option,),
                        set_flags=set_flags,
                        role=f"option-goto:{target}",
                    )
                continue
            if _contains_terminal(option):
                continue

            target: str | None = None
            target_node: DataNode | None = None
            fell_into_label = False
            path_nodes: list[DataNode] = [option]
            for sibling in siblings[choice_index + 1 :]:
                sibling_values = _values(sibling)
                if len(sibling_values) >= 2 and sibling_values[0] == "label":
                    target = sibling_values[1]
                    target_node = sibling
                    fell_into_label = True
                    break
                path_nodes.append(sibling)
                sibling_gotos = _goto_nodes(sibling)
                if sibling_gotos:
                    target, target_node = sibling_gotos[0]
                    break
                if _contains_terminal(sibling):
                    break

            if target is None or target_node is None:
                continue
            self._add_dialogue_transition(
                builder,
                chunk,
                option_id,
                quest,
                target,
                target_node,
                path_nodes=tuple(path_nodes),
                set_flags=set_flags,
                role=f"option-path:{target}",
            )
            if fell_into_label and target in explicit_targets:
                fallthrough_collisions.setdefault(target, []).append(option)

        for target, options in sorted(fallthrough_collisions.items()):
            if len(options) < 2:
                continue
            option = options[0]
            option_id = _dialogue_option_id(quest, chunk, option)
            builder.add_relation(
                EdgeType.REFERENCES,
                option_id,
                _unresolved_merge_id(quest, chunk, option, target),
                chunk=chunk,
                node=option,
                role=f"missing-merge:{target}",
            )

    def _add_dialogue_transition(
        self,
        builder: _GraphBuilder,
        chunk: TopLevelChunk,
        source: str,
        quest: str,
        target: str,
        evidence_node: DataNode,
        *,
        path_nodes: tuple[DataNode, ...],
        set_flags: dict[str, set[str]],
        role: str,
    ) -> None:
        guarded_once = bool(_guard_flags(path_nodes) & set_flags.get(target, set()))
        builder.add_relation(
            EdgeType.PRECEDES,
            source,
            dialogue_label_id(quest, target),
            chunk=chunk,
            node=evidence_node,
            role=role,
            attrs={"repeatability": "once"} if guarded_once else None,
        )

    def _label_set_flags(
        self,
        children: list[DataNode],
        label_indexes: dict[str, int],
    ) -> dict[str, set[str]]:
        ordered = sorted(label_indexes.items(), key=lambda item: item[1])
        result: dict[str, set[str]] = {}
        for position, (label, start) in enumerate(ordered):
            end = ordered[position + 1][1] if position + 1 < len(ordered) else len(children)
            flags: set[str] = set()
            for node in children[start + 1 : end]:
                if _values(node)[:1] != ("action",):
                    continue
                for candidate in _descendants(node):
                    values = _values(candidate)
                    if len(values) >= 2 and values[0] == "set":
                        flags.add(values[1])
            result[label] = flags
        return result

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
