from __future__ import annotations

from datetime import datetime, timezone

from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.event_lifecycle import project_active_content
from gameforge.spine.ir.snapshot import Snapshot


def _snapshot(*, schedule_kind: str = "absolute") -> Snapshot:
    availability: dict[str, object]
    if schedule_kind == "absolute":
        availability = {
            "availability_schema_version": "event-availability@1",
            "schedule_kind": "absolute",
            "start_at": "2026-08-01T02:00:00Z",
            "gameplay_end_at": "2026-08-15T02:00:00Z",
            "reward_claim_end_at": "2026-08-18T02:00:00Z",
            "timezone": "Asia/Shanghai",
            "expiration_policy": "hide_from_active_content",
        }
    else:
        availability = {
            "availability_schema_version": "event-availability@1",
            "schedule_kind": "relative",
            "duration_days": 14,
            "reward_claim_grace_days": 3,
            "timezone": None,
            "expiration_policy": "hide_from_active_content",
        }
    entities = [
        Entity(
            id="event:dream-letters",
            type=NodeType.EVENT,
            attrs={
                "name": "梦中未寄出的信",
                "scope_kind": "event",
                "availability": availability,
            },
        ),
        Entity(
            id="battle:event-stage",
            type=NodeType.BATTLE_ENCOUNTER,
            attrs={
                "name": "梦境重构",
                "scope_kind": "event",
                "scope_owner_id": "event:dream-letters",
                "availability_phase": "gameplay",
            },
        ),
        Entity(
            id="event:deduction-module",
            type=NodeType.EVENT,
            attrs={
                "name": "梦境推演",
                "scope_kind": "event",
                "scope_role": "member",
                "scope_owner_id": "event:dream-letters",
                "availability_phase": "gameplay",
            },
        ),
        Entity(
            id="shop:event-shop",
            type=NodeType.SHOP,
            attrs={
                "name": "活动商店",
                "scope_kind": "event",
                "scope_owner_id": "event:dream-letters",
                "availability_phase": "reward_claim",
            },
        ),
        Entity(
            id="status:event-lucid",
            type=NodeType.STATUS_EFFECT,
            attrs={
                "name": "清醒",
                "scope_kind": "event",
                "scope_owner_id": "event:dream-letters",
                "availability_phase": "gameplay",
            },
        ),
        Entity(
            id="character:nahida",
            type=NodeType.CHARACTER,
            attrs={"name": "纳西妲", "scope_kind": "permanent"},
        ),
    ]
    relations = [
        Relation(
            id="relation:event-stage",
            type=EdgeType.CONTAINS,
            src_id="event:deduction-module",
            dst_id="battle:event-stage",
        ),
        Relation(
            id="relation:event-shop",
            type=EdgeType.CONTAINS,
            src_id="event:dream-letters",
            dst_id="shop:event-shop",
        ),
        Relation(
            id="relation:event-module",
            type=EdgeType.CONTAINS,
            src_id="event:dream-letters",
            dst_id="event:deduction-module",
        ),
        Relation(
            id="relation:event-status",
            type=EdgeType.APPLIES_EFFECT,
            src_id="event:deduction-module",
            dst_id="status:event-lucid",
        ),
        Relation(
            id="relation:event-character",
            type=EdgeType.REFERENCES,
            src_id="event:dream-letters",
            dst_id="character:nahida",
        ),
    ]
    return Snapshot.from_entities_relations(entities, relations)


def test_active_projection_hides_gameplay_then_all_event_content_without_deleting_history() -> None:
    source = _snapshot()

    active = project_active_content(
        source,
        at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    claim_only = project_active_content(
        source,
        at=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )
    expired = project_active_content(
        source,
        at=datetime(2026, 8, 19, tzinfo=timezone.utc),
    )

    assert active.event_phases == (("event:dream-letters", "active"),)
    assert set(active.snapshot.entities) == set(source.entities)
    assert set(claim_only.snapshot.entities) == {
        "event:dream-letters",
        "shop:event-shop",
        "character:nahida",
    }
    assert expired.event_phases == (("event:dream-letters", "expired"),)
    assert set(expired.snapshot.entities) == {"character:nahida"}
    assert expired.snapshot.relations == {}
    assert len(source.entities) == 6
    assert len(source.relations) == 5


def test_relative_schedule_is_retained_for_authoring_but_hidden_from_runtime_until_bound() -> None:
    source = _snapshot(schedule_kind="relative")

    findings = GraphChecker().check(source)
    projection = project_active_content(
        source,
        at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )

    assert [(finding.defect_class, finding.severity) for finding in findings] == [
        ("unbound_event_schedule", "major")
    ]
    assert projection.event_phases == (("event:dream-letters", "unscheduled"),)
    assert set(projection.snapshot.entities) == {"character:nahida"}
    assert "event:dream-letters" in source.entities


def test_permanent_content_cannot_depend_on_event_owned_content() -> None:
    source = _snapshot()
    relations = list(source.relations.values()) + [
        Relation(
            id="relation:permanent-requires-event",
            type=EdgeType.REQUIRES,
            src_id="character:nahida",
            dst_id="battle:event-stage",
        )
    ]
    candidate = Snapshot.from_entities_relations(source.entities.values(), relations)

    findings = GraphChecker().check(candidate)

    assert [(finding.defect_class, finding.entities) for finding in findings] == [
        (
            "permanent_depends_on_limited_content",
            ["character:nahida", "battle:event-stage"],
        )
    ]


def test_event_gate_is_a_valid_ownership_path_for_its_participation_condition() -> None:
    source = _snapshot()
    entities = list(source.entities.values()) + [
        Entity(
            id="unlock:event-participation",
            type=NodeType.UNLOCK_CONDITION,
            attrs={
                "name": "活动参与条件",
                "scope_kind": "event",
                "scope_role": "member",
                "scope_owner_id": "event:dream-letters",
                "availability_phase": "gameplay",
            },
        )
    ]
    relations = list(source.relations.values()) + [
        Relation(
            id="relation:event-gated-by-participation",
            type=EdgeType.GATED_BY,
            src_id="event:dream-letters",
            dst_id="unlock:event-participation",
        )
    ]
    candidate = Snapshot.from_entities_relations(entities, relations)

    assert GraphChecker().check(candidate) == []
