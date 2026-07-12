"""Independent source-native predicates for frozen Endless Sky cases.

These predicates establish benchmark ground truth directly from the lossless
token tree. They deliberately do not consume Adapter output or checker
findings, so qualification has two independent evidence paths.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

from gameforge.bench.external_cases.contracts import PredicateEvidence, TargetLocator
from gameforge.spine.ingestion.endless_sky_reader import (
    READER_VERSION,
    DataNode,
    EndlessSkyTree,
    TopLevelChunk,
    top_level_chunks,
)


PREDICATE_VERSION = "endless-sky-predicates@1"

_MISSION_STATE = re.compile(r"^(.+): (offered|done)$")
_OFFER_TRIGGERS = frozenset(
    {"landing", "job", "assisting", "boarding", "entering", "spaceport"}
)
_DIALOGUE_TERMINALS = frozenset(
    {"accept", "decline", "depart", "die", "explode", "launch"}
)


@dataclass(frozen=True)
class _Record:
    target: TargetLocator
    chunk: TopLevelChunk


@dataclass(frozen=True)
class _Edge:
    source: str
    target: str
    path: str
    line: int
    repeatable: bool


def _values(node: DataNode) -> tuple[str, ...]:
    return tuple(token.value for token in node.tokens)


def _descendants(node: DataNode) -> Iterable[DataNode]:
    for child in node.children:
        yield child
        yield from _descendants(child)


def _walk(node: DataNode) -> Iterable[DataNode]:
    yield node
    yield from _descendants(node)


def _direct_children(node: DataNode, token: str) -> tuple[DataNode, ...]:
    return tuple(child for child in node.children if _values(child)[:1] == (token,))


def _goto_nodes(node: DataNode) -> tuple[tuple[str, DataNode], ...]:
    return tuple(
        (values[1], candidate)
        for candidate in _walk(node)
        if len(values := _values(candidate)) >= 2 and values[0] == "goto"
    )


def _contains_terminal(node: DataNode) -> bool:
    return any(
        bool(values) and values[0] in _DIALOGUE_TERMINALS
        for candidate in _walk(node)
        if (values := _values(candidate))
    )


def _issue(
    record: _Record | None,
    *,
    reason: str,
    node: DataNode | None = None,
    **details: Any,
) -> dict[str, Any]:
    if record is None:
        path = details.pop("path", "<tree>")
        line = details.pop("line", 1)
        payload: dict[str, Any] = {"path": path, "line": line, "reason": reason}
    else:
        payload = {
            "path": record.target.path,
            "record_kind": record.target.record_kind,
            "record_name": record.target.record_name,
            "line": (
                node.source_span.start_line
                if node is not None
                else record.chunk.source_span.start_line
            ),
            "reason": reason,
        }
    payload.update(details)
    return payload


def _record_index(tree: EndlessSkyTree) -> dict[tuple[str, str, str], list[TopLevelChunk]]:
    index: dict[tuple[str, str, str], list[TopLevelChunk]] = {}
    for data_file in tree.files:
        for chunk in top_level_chunks(data_file):
            index.setdefault((chunk.path, chunk.kind, chunk.name), []).append(chunk)
    return index


def _resolve_targets(
    tree: EndlessSkyTree,
    targets: tuple[TargetLocator, ...],
) -> tuple[list[_Record], list[dict[str, Any]]]:
    index = _record_index(tree)
    records: list[_Record] = []
    unproven: list[dict[str, Any]] = []
    for target in targets:
        matches = index.get((target.path, target.record_kind, target.record_name), [])
        if not matches:
            unproven.append(
                {
                    "path": target.path,
                    "record_kind": target.record_kind,
                    "record_name": target.record_name,
                    "line": 1,
                    "reason": "target_missing",
                }
            )
            continue
        if len(matches) != 1 or matches[0].node is None:
            unproven.append(
                {
                    "path": target.path,
                    "record_kind": target.record_kind,
                    "record_name": target.record_name,
                    "line": matches[0].source_span.start_line,
                    "reason": "target_ambiguous",
                }
            )
            continue
        records.append(_Record(target=target, chunk=matches[0]))
    return records, unproven


def _context_resources(
    context: Mapping[str, object],
) -> tuple[frozenset[tuple[str, str]], list[dict[str, Any]]]:
    raw = context.get("resources", [])
    if not isinstance(raw, list | tuple):
        return frozenset(), [
            {"path": "<context>", "line": 1, "reason": "invalid_resources_context"}
        ]
    resources: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            return frozenset(), [
                {"path": "<context>", "line": 1, "reason": "invalid_resources_context"}
            ]
        kind = item.get("kind")
        name = item.get("name")
        if not isinstance(kind, str) or not kind or not isinstance(name, str) or not name:
            return frozenset(), [
                {"path": "<context>", "line": 1, "reason": "invalid_resources_context"}
            ]
        resources.add((kind, name))
    return frozenset(resources), []


def _label_index(
    record: _Record,
    conversation: DataNode,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    labels: dict[str, int] = {}
    unproven: list[dict[str, Any]] = []
    for index, child in enumerate(conversation.children):
        values = _values(child)
        if len(values) < 2 or values[0] != "label":
            continue
        label = values[1]
        if label in labels:
            unproven.append(
                _issue(
                    record,
                    reason="duplicate_dialogue_label",
                    node=child,
                    label=label,
                )
            )
        else:
            labels[label] = index
    return labels, unproven


def _choice_reference_violations(
    record: _Record,
    conversation: DataNode,
    labels: Mapping[str, int],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    children = list(conversation.children)
    for choice_index, choice in enumerate(children):
        if _values(choice)[:1] != ("choice",):
            continue
        explicit_targets = {
            target
            for option in choice.children
            for target, _ in _goto_nodes(option)
        }
        collisions: dict[str, list[DataNode]] = {}
        for option in choice.children:
            gotos = _goto_nodes(option)
            if gotos or _contains_terminal(option):
                continue

            target: str | None = None
            fell_into_label = False
            reached_terminal = False
            for sibling in children[choice_index + 1 :]:
                sibling_values = _values(sibling)
                if len(sibling_values) >= 2 and sibling_values[0] == "label":
                    target = sibling_values[1]
                    fell_into_label = True
                    break
                sibling_gotos = _goto_nodes(sibling)
                if sibling_gotos:
                    target = sibling_gotos[0][0]
                    break
                if _contains_terminal(sibling):
                    reached_terminal = True
                    break

            if target is None:
                if not reached_terminal:
                    violations.append(
                        _issue(
                            record,
                            reason="unterminated_choice_path",
                            node=option,
                        )
                    )
                continue
            if target not in labels:
                violations.append(
                    _issue(
                        record,
                        reason="unresolved_dialogue_label",
                        node=option,
                        label=target,
                    )
                )
                continue
            if fell_into_label and target in explicit_targets:
                collisions.setdefault(target, []).append(option)

        for target, options in sorted(collisions.items()):
            if len(options) >= 2:
                violations.append(
                    _issue(
                        record,
                        reason="ambiguous_choice_merge",
                        node=options[0],
                        label=target,
                        implicit_path_count=len(options),
                    )
                )
    return violations


def _reference_resolves(
    records: Sequence[_Record],
    context: Mapping[str, object],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resources, unproven = _context_resources(context)
    violations: list[dict[str, Any]] = []
    for record in records:
        node = record.chunk.node
        if node is None:
            unproven.append(_issue(record, reason="target_has_no_token_tree"))
            continue
        if record.target.record_kind == "effect":
            for candidate in _descendants(node):
                values = _values(candidate)
                if len(values) >= 2 and values[0] == "sound":
                    name = values[1]
                    if ("sound", name) not in resources:
                        violations.append(
                            _issue(
                                record,
                                reason="resource_missing",
                                node=candidate,
                                resource_kind="sound",
                                resource_name=name,
                            )
                        )
            continue
        if record.target.record_kind != "mission":
            unproven.append(_issue(record, reason="unsupported_target_kind"))
            continue

        for conversation in _descendants(node):
            if _values(conversation)[:1] != ("conversation",):
                continue
            labels, label_issues = _label_index(record, conversation)
            unproven.extend(label_issues)
            for target, goto_node in (
                item for child in conversation.children for item in _goto_nodes(child)
            ):
                if target not in labels:
                    violations.append(
                        _issue(
                            record,
                            reason="unresolved_dialogue_label",
                            node=goto_node,
                            label=target,
                        )
                    )
            violations.extend(
                _choice_reference_violations(record, conversation, labels)
            )
    return violations, unproven


def _mission_state_dependencies(node: DataNode) -> tuple[tuple[str, DataNode], ...]:
    dependencies: list[tuple[str, DataNode]] = []
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
                dependencies.append((match.group(1), condition))
    return tuple(dependencies)


def _guard_flags(nodes: Sequence[DataNode]) -> set[str]:
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


def _label_set_flags(
    conversation: DataNode,
    labels: Mapping[str, int],
) -> dict[str, set[str]]:
    children = list(conversation.children)
    ordered = sorted(labels.items(), key=lambda item: item[1])
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


def _dialogue_edges(
    record: _Record,
    conversation: DataNode,
) -> tuple[list[_Edge], list[dict[str, Any]]]:
    labels, unproven = _label_index(record, conversation)
    if unproven:
        return [], unproven
    children = list(conversation.children)
    set_flags = _label_set_flags(conversation, labels)
    prefix = f"conversation:{conversation.source_span.start_line}:"
    entry = f"{prefix}entry"
    current_source = entry
    edges: list[_Edge] = []

    def add_edge(target: str, node: DataNode, path_nodes: Sequence[DataNode]) -> None:
        if target not in labels:
            unproven.append(
                _issue(
                    record,
                    reason="unresolved_dialogue_label",
                    node=node,
                    label=target,
                )
            )
            return
        repeatable = not bool(_guard_flags(path_nodes) & set_flags.get(target, set()))
        edges.append(
            _Edge(
                source=current_source,
                target=f"{prefix}label:{target}",
                path=record.target.path,
                line=node.source_span.start_line,
                repeatable=repeatable,
            )
        )

    for choice_index, child in enumerate(children):
        values = _values(child)
        if not values:
            continue
        if len(values) >= 2 and values[0] == "label":
            current_source = f"{prefix}label:{values[1]}"
            continue
        if values[0] == "choice":
            for option in child.children:
                gotos = _goto_nodes(option)
                if gotos:
                    for target, goto_node in gotos:
                        add_edge(target, goto_node, (option,))
                    continue
                if _contains_terminal(option):
                    continue
                target: str | None = None
                target_node: DataNode | None = None
                path_nodes: list[DataNode] = [option]
                for sibling in children[choice_index + 1 :]:
                    sibling_values = _values(sibling)
                    if len(sibling_values) >= 2 and sibling_values[0] == "label":
                        target = sibling_values[1]
                        target_node = sibling
                        break
                    path_nodes.append(sibling)
                    sibling_gotos = _goto_nodes(sibling)
                    if sibling_gotos:
                        target, target_node = sibling_gotos[0]
                        break
                    if _contains_terminal(sibling):
                        break
                if target is not None and target_node is not None:
                    add_edge(target, target_node, tuple(path_nodes))
            continue
        for target, goto_node in _goto_nodes(child):
            add_edge(target, goto_node, (child,))

    blocks: list[tuple[int, str]] = [(-1, entry)]
    blocks.extend(
        (index, f"{prefix}label:{label}")
        for label, index in sorted(labels.items(), key=lambda item: item[1])
    )
    for block_index, (start, source) in enumerate(blocks[:-1]):
        next_start, next_target = blocks[block_index + 1]
        segment = tuple(children[start + 1 : next_start])
        if any(_values(node)[:1] == ("choice",) for node in segment):
            continue
        if any(_goto_nodes(node) or _contains_terminal(node) for node in segment):
            continue
        target_label = _values(children[next_start])[1]
        repeatable = not bool(_guard_flags(segment) & set_flags.get(target_label, set()))
        edges.append(
            _Edge(
                source=source,
                target=next_target,
                path=record.target.path,
                line=children[next_start].source_span.start_line,
                repeatable=repeatable,
            )
        )
    return edges, unproven


def _first_cycle(edges: Sequence[_Edge]) -> tuple[_Edge, ...] | None:
    adjacency: dict[str, list[_Edge]] = {}
    for edge in edges:
        if edge.repeatable:
            adjacency.setdefault(edge.source, []).append(edge)
            adjacency.setdefault(edge.target, [])
    for outgoing in adjacency.values():
        outgoing.sort(key=lambda edge: (edge.target, edge.path, edge.line))

    colors: dict[str, int] = {node: 0 for node in adjacency}
    node_stack: list[str] = []
    edge_stack: list[_Edge] = []

    def visit(node: str) -> tuple[_Edge, ...] | None:
        colors[node] = 1
        node_stack.append(node)
        for edge in adjacency[node]:
            target_color = colors[edge.target]
            if target_color == 0:
                edge_stack.append(edge)
                cycle = visit(edge.target)
                if cycle is not None:
                    return cycle
                edge_stack.pop()
            elif target_color == 1:
                start = node_stack.index(edge.target)
                return tuple(edge_stack[start:] + [edge])
        node_stack.pop()
        colors[node] = 2
        return None

    for node in sorted(adjacency):
        if colors[node] == 0:
            cycle = visit(node)
            if cycle is not None:
                return cycle
    return None


def _all_missions(tree: EndlessSkyTree) -> dict[str, list[TopLevelChunk]]:
    missions: dict[str, list[TopLevelChunk]] = {}
    for data_file in tree.files:
        for chunk in top_level_chunks(data_file):
            if chunk.kind == "mission" and chunk.node is not None:
                missions.setdefault(chunk.name, []).append(chunk)
    return missions


def _dependency_acyclic(
    tree: EndlessSkyTree,
    records: Sequence[_Record],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    violations: list[dict[str, Any]] = []
    unproven: list[dict[str, Any]] = []
    mission_records = [record for record in records if record.target.record_kind == "mission"]
    if len(mission_records) != len(records):
        unproven.extend(
            _issue(record, reason="unsupported_target_kind")
            for record in records
            if record.target.record_kind != "mission"
        )

    missions = _all_missions(tree)
    mission_edges: list[_Edge] = []
    for name, chunks in sorted(missions.items()):
        if len(chunks) != 1:
            if any(record.target.record_name == name for record in mission_records):
                record = next(
                    record for record in mission_records if record.target.record_name == name
                )
                unproven.append(_issue(record, reason="mission_name_ambiguous"))
            continue
        node = chunks[0].node
        if node is None:
            continue
        for dependency, condition in _mission_state_dependencies(node):
            dependency_chunks = missions.get(dependency, [])
            if len(dependency_chunks) == 1:
                mission_edges.append(
                    _Edge(
                        source=name,
                        target=dependency,
                        path=chunks[0].path,
                        line=condition.source_span.start_line,
                        repeatable=True,
                    )
                )

    cycle = _first_cycle(mission_edges)
    target_names = {record.target.record_name for record in mission_records}
    if cycle is not None and target_names & {
        endpoint for edge in cycle for endpoint in (edge.source, edge.target)
    }:
        record = next(record for record in mission_records if record.target.record_name in target_names)
        violations.append(
            _issue(
                record,
                reason="dependency_cycle",
                line=cycle[0].line,
                cycle=[cycle[0].source] + [edge.target for edge in cycle],
                edge_kind="mission_state",
            )
        )

    for record in mission_records:
        node = record.chunk.node
        if node is None:
            unproven.append(_issue(record, reason="target_has_no_token_tree"))
            continue
        for conversation in _descendants(node):
            if _values(conversation)[:1] != ("conversation",):
                continue
            edges, dialogue_issues = _dialogue_edges(record, conversation)
            unproven.extend(dialogue_issues)
            dialogue_cycle = _first_cycle(edges)
            if dialogue_cycle is not None:
                violations.append(
                    _issue(
                        record,
                        reason="dependency_cycle",
                        line=dialogue_cycle[0].line,
                        cycle=[dialogue_cycle[0].source]
                        + [edge.target for edge in dialogue_cycle],
                        edge_kind="dialogue_transition",
                    )
                )
    return violations, unproven


def _restricted_destinations(
    context: Mapping[str, object],
) -> tuple[frozenset[str], list[dict[str, Any]]]:
    raw = context.get("restricted_destinations")
    if not isinstance(raw, list | tuple) or not raw or any(
        not isinstance(item, str) or not item for item in raw
    ):
        return frozenset(), [
            {
                "path": "<context>",
                "line": 1,
                "reason": "invalid_restricted_destinations_context",
            }
        ]
    return frozenset(raw), []


def _landing_access_conditions(node: DataNode) -> frozenset[str]:
    destinations: set[str] = set()
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
                destinations.add(condition_values[1].removeprefix("landing access: "))
    return frozenset(destinations)


def _target_reachable(
    records: Sequence[_Record],
    context: Mapping[str, object],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    restricted, unproven = _restricted_destinations(context)
    violations: list[dict[str, Any]] = []
    for record in records:
        node = record.chunk.node
        if node is None:
            unproven.append(_issue(record, reason="target_has_no_token_tree"))
            continue
        if record.target.record_kind != "mission":
            unproven.append(_issue(record, reason="unsupported_target_kind"))
            continue
        destinations = tuple(
            child
            for child in _direct_children(node, "destination")
            if len(_values(child)) >= 2
        )
        if not destinations:
            unproven.append(_issue(record, reason="destination_missing"))
            continue
        clearance = any(_values(child) == ("clearance",) for child in node.children)
        access = _landing_access_conditions(node)
        checked_restricted = False
        for destination_node in destinations:
            destination = _values(destination_node)[1]
            if destination not in restricted:
                continue
            checked_restricted = True
            if not clearance and destination not in access:
                violations.append(
                    _issue(
                        record,
                        reason="restricted_destination_without_access",
                        node=destination_node,
                        destination=destination,
                    )
                )
        if not checked_restricted:
            unproven.append(
                _issue(
                    record,
                    reason="target_destination_not_registered_restricted",
                    node=destinations[0],
                    destinations=[_values(item)[1] for item in destinations],
                )
            )
    return violations, unproven


def _mission_offerable(
    records: Sequence[_Record],
    _context: Mapping[str, object],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    violations: list[dict[str, Any]] = []
    unproven: list[dict[str, Any]] = []
    for record in records:
        node = record.chunk.node
        if node is None:
            unproven.append(_issue(record, reason="target_has_no_token_tree"))
            continue
        if record.target.record_kind != "mission":
            unproven.append(_issue(record, reason="unsupported_target_kind"))
            continue
        directives = {_values(child)[0] for child in node.children if _values(child)}
        if "source" not in directives and not directives & _OFFER_TRIGGERS:
            violations.append(_issue(record, reason="mission_has_no_offer_origin", node=node))
    return violations, unproven


_Predicate = Callable[
    [Sequence[_Record], Mapping[str, object]],
    tuple[list[dict[str, Any]], list[dict[str, Any]]],
]


def evaluate_predicate(
    predicate_id: str,
    tree: EndlessSkyTree,
    targets: Sequence[TargetLocator],
    context: Mapping[str, object],
) -> PredicateEvidence:
    """Evaluate one frozen source predicate without using GameForge IR/findings."""

    bound_targets = tuple(targets)
    if predicate_id not in {
        "reference_resolves",
        "dependency_acyclic",
        "target_reachable",
        "mission_offerable",
    }:
        return PredicateEvidence(
            predicate_id=predicate_id,
            status="unproven",
            target_locators=bound_targets,
            evidence={
                "predicate_version": PREDICATE_VERSION,
                "checked_targets": [],
                "violations": [],
                "unproven": [
                    {"path": "<predicate>", "line": 1, "reason": "unknown_predicate"}
                ],
            },
        )

    if tree.reader_version != READER_VERSION:
        return PredicateEvidence(
            predicate_id=predicate_id,
            status="unproven",
            target_locators=bound_targets,
            evidence={
                "predicate_version": PREDICATE_VERSION,
                "checked_targets": [],
                "violations": [],
                "unproven": [
                    {
                        "path": "<tree>",
                        "line": 1,
                        "reason": "unsupported_reader_version",
                    }
                ],
            },
        )

    records, unproven = _resolve_targets(tree, bound_targets)
    evaluators: dict[str, _Predicate] = {
        "reference_resolves": _reference_resolves,
        "target_reachable": _target_reachable,
        "mission_offerable": _mission_offerable,
    }
    if predicate_id == "dependency_acyclic":
        violations, predicate_unproven = _dependency_acyclic(tree, records)
    else:
        violations, predicate_unproven = evaluators[predicate_id](records, context)
    unproven.extend(predicate_unproven)
    violations.sort(key=lambda item: (item["path"], item["line"], item["reason"]))
    unproven.sort(key=lambda item: (item["path"], item["line"], item["reason"]))
    status = "unproven" if unproven else "violation" if violations else "clear"
    return PredicateEvidence(
        predicate_id=predicate_id,
        status=status,
        target_locators=bound_targets,
        evidence={
            "predicate_version": PREDICATE_VERSION,
            "checked_targets": [
                record.target.model_dump(mode="json") for record in records
            ],
            "violations": violations,
            "unproven": unproven,
        },
    )


__all__ = ["PREDICATE_VERSION", "evaluate_predicate"]
