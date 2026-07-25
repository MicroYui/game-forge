"""Deterministic lifecycle validation and active-content projection for events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import ValidationError

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.event_lifecycle import (
    AbsoluteEventAvailabilityV1,
    EVENT_AVAILABILITY_ADAPTER,
    RelativeEventAvailabilityV1,
    parse_aware_datetime,
)
from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import EdgeType, Entity, NodeType
from gameforge.spine.ir.snapshot import Snapshot


EventPhase = Literal[
    "scheduled",
    "active",
    "reward_claim",
    "expired",
    "unscheduled",
    "invalid",
]
_DEPENDENCY_TYPES = frozenset({EdgeType.REQUIRES, EdgeType.GATED_BY})
_OWNERSHIP_PATH_TYPES = frozenset(
    {
        EdgeType.APPLIES_EFFECT,
        EdgeType.CONTAINS,
        EdgeType.GATED_BY,
        EdgeType.GRANTS,
        EdgeType.HAS_STEP,
        EdgeType.REWARDS,
    }
)


@dataclass(frozen=True, slots=True)
class EventAvailabilityProjection:
    """One immutable active view; the source authoring snapshot remains unchanged."""

    snapshot: Snapshot
    event_phases: tuple[tuple[str, EventPhase], ...]


def _limited_events(snapshot: Snapshot) -> tuple[Entity, ...]:
    return tuple(
        sorted(
            (
                entity
                for entity in snapshot.entities.values()
                if entity.type is NodeType.EVENT
                and (
                    entity.attrs.get("scope_role") == "owner"
                    or "availability" in entity.attrs
                )
            ),
            key=lambda item: item.id,
        )
    )


def _event_phase(entity: Entity, at: datetime) -> EventPhase:
    raw = entity.attrs.get("availability")
    if not isinstance(raw, dict):
        return "invalid"
    try:
        availability = EVENT_AVAILABILITY_ADAPTER.validate_python(raw)
    except ValidationError:
        return "invalid"
    if isinstance(availability, RelativeEventAvailabilityV1):
        return "unscheduled"
    assert isinstance(availability, AbsoluteEventAvailabilityV1)
    start = parse_aware_datetime(availability.start_at, field_name="start_at")
    gameplay_end = parse_aware_datetime(
        availability.gameplay_end_at,
        field_name="gameplay_end_at",
    )
    reward_claim_end = parse_aware_datetime(
        availability.reward_claim_end_at,
        field_name="reward_claim_end_at",
    )
    if at < start:
        return "scheduled"
    if at < gameplay_end:
        return "active"
    if at < reward_claim_end:
        return "reward_claim"
    return "expired"


def project_active_content(snapshot: Snapshot, *, at: datetime) -> EventAvailabilityProjection:
    """Project content visible at one explicit instant without deleting history."""

    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("active-content projection time must include a UTC offset")
    events = _limited_events(snapshot)
    phases = {event.id: _event_phase(event, at) for event in events}
    limited_event_ids = set(phases)
    visible_ids: set[str] = set()
    for entity in snapshot.entities.values():
        if entity.id in limited_event_ids:
            if phases[entity.id] in {"active", "reward_claim"}:
                visible_ids.add(entity.id)
            continue
        if entity.attrs.get("scope_kind") != "event":
            visible_ids.add(entity.id)
            continue
        owner_id = entity.attrs.get("scope_owner_id")
        if not isinstance(owner_id, str) or owner_id not in phases:
            continue
        phase = phases[owner_id]
        if phase == "active":
            visible_ids.add(entity.id)
        elif phase == "reward_claim" and entity.attrs.get("availability_phase") == "reward_claim":
            visible_ids.add(entity.id)

    entities = [snapshot.entities[entity_id] for entity_id in sorted(visible_ids)]
    relations = [
        relation
        for relation in snapshot.relations.values()
        if relation.src_id in visible_ids and relation.dst_id in visible_ids
    ]
    return EventAvailabilityProjection(
        snapshot=Snapshot.from_entities_relations(entities, relations),
        event_phases=tuple(sorted(phases.items())),
    )


class EventLifecycleChecker:
    """Validate event windows, ownership, and permanent-to-ephemeral dependencies."""

    id = "event-lifecycle"

    def check(self, snapshot: Snapshot) -> list[Finding]:
        findings: list[Finding] = []
        events = _limited_events(snapshot)
        event_ids = {event.id for event in events}

        def emit(
            defect_class: str,
            severity: Literal["critical", "major", "minor", "info"],
            entities: list[str],
            evidence: dict[str, object],
            message: str,
        ) -> None:
            digest = canonical_sha256(
                {
                    "checker": self.id,
                    "snapshot_id": snapshot.snapshot_id,
                    "defect_class": defect_class,
                    "entities": entities,
                    "evidence": evidence,
                }
            )
            findings.append(
                Finding(
                    id=f"finding:event-lifecycle:{digest}",
                    source="checker",
                    producer_id=self.id,
                    producer_run_id=f"event-lifecycle@{snapshot.snapshot_id[:24]}",
                    oracle_type="deterministic",
                    defect_class=defect_class,
                    severity=severity,
                    snapshot_id=snapshot.snapshot_id,
                    entities=entities,
                    evidence=evidence,
                    minimal_repro={"entity": entities[0]},
                    status="confirmed",
                    message=message,
                )
            )

        for event in events:
            raw = event.attrs.get("availability")
            if not isinstance(raw, dict):
                emit(
                    "invalid_event_lifecycle",
                    "critical",
                    [event.id],
                    {"reason": "availability_missing"},
                    f"Limited event {event.id} has no typed availability window",
                )
                continue
            try:
                availability = EVENT_AVAILABILITY_ADAPTER.validate_python(raw)
            except ValidationError as exc:
                emit(
                    "invalid_event_lifecycle",
                    "critical",
                    [event.id],
                    {
                        "reason": "availability_invalid",
                        "error_types": sorted({item["type"] for item in exc.errors()}),
                    },
                    f"Limited event {event.id} has an invalid availability window",
                )
                continue
            if isinstance(availability, RelativeEventAvailabilityV1):
                emit(
                    "unbound_event_schedule",
                    "major",
                    [event.id],
                    {
                        "duration_days": availability.duration_days,
                        "reward_claim_grace_days": availability.reward_claim_grace_days,
                    },
                    f"Limited event {event.id} has durations but no absolute launch window",
                )

        event_owned: dict[str, str] = {}
        ownership_adjacency: dict[str, set[str]] = {}
        for relation in snapshot.relations.values():
            if relation.type in _OWNERSHIP_PATH_TYPES:
                ownership_adjacency.setdefault(relation.src_id, set()).add(relation.dst_id)

        def reachable_from(owner_id: str) -> set[str]:
            visited = {owner_id}
            frontier = [owner_id]
            while frontier:
                source_id = frontier.pop()
                for target_id in sorted(ownership_adjacency.get(source_id, ())):
                    if target_id in visited:
                        continue
                    visited.add(target_id)
                    frontier.append(target_id)
            return visited

        ownership_reachability = {
            event_id: reachable_from(event_id) for event_id in sorted(event_ids)
        }
        for entity in sorted(snapshot.entities.values(), key=lambda item: item.id):
            if entity.id in event_ids or entity.attrs.get("scope_kind") != "event":
                continue
            owner_id = entity.attrs.get("scope_owner_id")
            if not isinstance(owner_id, str) or owner_id not in event_ids:
                emit(
                    "event_scope_owner_missing",
                    "critical",
                    [entity.id],
                    {"scope_owner_id": owner_id},
                    f"Event-owned content {entity.id} has no valid owning event",
                )
                continue
            event_owned[entity.id] = owner_id
            if entity.id not in ownership_reachability[owner_id]:
                emit(
                    "event_scope_membership_missing",
                    "major",
                    [owner_id, entity.id],
                    {"scope_owner_id": owner_id},
                    f"Event-owned content {entity.id} is not contained by {owner_id}",
                )

        limited_ids = event_ids | set(event_owned)
        for relation in sorted(snapshot.relations.values(), key=lambda item: item.id):
            if relation.type not in _DEPENDENCY_TYPES:
                continue
            if relation.src_id in limited_ids or relation.dst_id not in limited_ids:
                continue
            emit(
                "permanent_depends_on_limited_content",
                "critical",
                [relation.src_id, relation.dst_id],
                {"relation": relation.id, "edge_type": relation.type.value},
                f"Permanent content {relation.src_id} depends on limited content {relation.dst_id}",
            )

        return sorted(findings, key=lambda finding: finding.id)


__all__ = [
    "EventAvailabilityProjection",
    "EventLifecycleChecker",
    "EventPhase",
    "project_active_content",
]
