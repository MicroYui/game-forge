"""GraphChecker (M1 Task 4): 7 structural defect classes.

Per-defect pattern: a minimal dirty graph triggers exactly 1 Finding of that
defect_class; a clean counterpart triggers 0 (oracle-FP=0 seed).
"""

from __future__ import annotations

from gameforge.contracts.ir import Entity, NodeType, EdgeType, Relation
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ir.snapshot import Snapshot


def _snap(entities, relations):
    return Snapshot.from_entities_relations(entities, relations)


def _findings(entities, relations, defect_class, nav=None):
    return [
        f for f in GraphChecker().check(_snap(entities, relations), nav=nav)
        if f.defect_class == defect_class
    ]


class _FakeNav:
    """Minimal NavProvider test double (duck-types spine.ir.store.NavProvider)."""

    def __init__(self, positions: dict[str, tuple[int, int]], reachable_pairs: set):
        self._positions = positions
        self._reachable_pairs = reachable_pairs

    def pos_of(self, entity_id: str):
        return self._positions.get(entity_id)

    def reachable(self, src_pos, dst_pos) -> bool:
        return (src_pos, dst_pos) in self._reachable_pairs


# --- 1. dangling_reference ---

def test_dangling_reference_detected_and_clean_is_silent():
    ents = [Entity(id="q", type=NodeType.QUEST)]
    rels = [Relation(id="r1", type=EdgeType.HAS_STEP, src_id="q", dst_id="missing")]
    fs = [f for f in GraphChecker().check(_snap(ents, rels)) if f.defect_class == "dangling_reference"]
    assert len(fs) == 1 and "missing" in fs[0].evidence["missing"]
    assert fs[0].oracle_type == "deterministic"
    assert fs[0].status == "confirmed"
    assert fs[0].source == "checker"
    assert fs[0].producer_id == "graph"

    clean = GraphChecker().check(_snap([Entity(id="q", type=NodeType.QUEST)], []))
    assert [f for f in clean if f.defect_class == "dangling_reference"] == []


# --- 2. missing_drop_source ---

def test_missing_drop_source_detected_and_clean_is_silent():
    step = Entity(id="step:collect", type=NodeType.QUEST_STEP,
                  attrs={"kind": "collect", "item": "item:x"})
    item = Entity(id="item:x", type=NodeType.ITEM)
    dirty = _findings([step, item], [], "missing_drop_source")
    assert len(dirty) == 1
    assert dirty[0].evidence["item"] == "item:x"
    assert dirty[0].evidence["known_sources"] == []

    source = Entity(id="interact:gather", type=NodeType.INTERACTABLE)
    grant = Relation(id="g1", type=EdgeType.GRANTS, src_id="interact:gather", dst_id="item:x")
    clean = _findings([step, item, source], [grant], "missing_drop_source")
    assert clean == []


# --- 3. unreachable_target ---

def test_unreachable_target_detected_with_nav_and_clean_is_silent():
    quest = Entity(id="quest:q1", type=NodeType.QUEST)
    giver = Entity(id="npc:giver", type=NodeType.NPC)
    other = Entity(id="npc:other", type=NodeType.NPC)
    step = Entity(id="step:talk", type=NodeType.QUEST_STEP,
                  attrs={"kind": "talk", "target": "npc:other"})
    rels = [
        Relation(id="r_start", type=EdgeType.STARTS_AT, src_id="quest:q1", dst_id="npc:giver"),
        Relation(id="r_hasstep", type=EdgeType.HAS_STEP, src_id="quest:q1", dst_id="step:talk"),
        Relation(id="r_talk", type=EdgeType.TALKS_TO, src_id="step:talk", dst_id="npc:other"),
    ]
    nav = _FakeNav(
        positions={"npc:giver": (0, 0), "npc:other": (5, 5)},
        reachable_pairs=set(),  # nothing reachable -> unreachable
    )
    dirty = _findings([quest, giver, other, step], rels, "unreachable_target", nav=nav)
    assert len(dirty) == 1
    assert dirty[0].evidence["giver"] == "npc:giver"
    assert dirty[0].evidence["target"] == "npc:other"

    nav_ok = _FakeNav(
        positions={"npc:giver": (0, 0), "npc:other": (5, 5)},
        reachable_pairs={((0, 0), (5, 5))},
    )
    clean = _findings([quest, giver, other, step], rels, "unreachable_target", nav=nav_ok)
    assert clean == []

    # no nav supplied -> cannot prove unreachability -> silent (no false positive)
    no_nav = _findings([quest, giver, other, step], rels, "unreachable_target", nav=None)
    assert no_nav == []


# --- 4. cyclic_dependency ---

def test_cycle_detected_with_path_evidence():
    ents = [Entity(id=f"s{i}", type=NodeType.QUEST_STEP) for i in (1, 2, 3)]
    rels = [Relation(id="a", type=EdgeType.PRECEDES, src_id="s1", dst_id="s2"),
            Relation(id="b", type=EdgeType.PRECEDES, src_id="s2", dst_id="s3"),
            Relation(id="c", type=EdgeType.PRECEDES, src_id="s3", dst_id="s1")]
    fs = [f for f in GraphChecker().check(_snap(ents, rels)) if f.defect_class == "cyclic_dependency"]
    assert len(fs) == 1 and set(fs[0].evidence["cycle_path"]) == {"s1", "s2", "s3"}

    clean_rels = [Relation(id="a", type=EdgeType.PRECEDES, src_id="s1", dst_id="s2"),
                  Relation(id="b", type=EdgeType.PRECEDES, src_id="s2", dst_id="s3")]
    clean = [f for f in GraphChecker().check(_snap(ents, clean_rels)) if f.defect_class == "cyclic_dependency"]
    assert clean == []


def test_self_requirement_is_a_dependency_cycle():
    quest = Entity(id="quest:q", type=NodeType.QUEST)
    requirement = Relation(
        id="requires-self",
        type=EdgeType.REQUIRES,
        src_id=quest.id,
        dst_id=quest.id,
    )

    findings = _findings([quest], [requirement], "cyclic_dependency")

    assert len(findings) == 1
    assert findings[0].entities == [quest.id]


def test_once_only_transition_does_not_form_repeatable_cycle():
    nodes = [
        Entity(id="dialogue:a", type=NodeType.DIALOGUE_NODE),
        Entity(id="dialogue:b", type=NodeType.DIALOGUE_NODE),
    ]
    relations = [
        Relation(
            id="forward",
            type=EdgeType.PRECEDES,
            src_id="dialogue:a",
            dst_id="dialogue:b",
        ),
        Relation(
            id="bounded-return",
            type=EdgeType.PRECEDES,
            src_id="dialogue:b",
            dst_id="dialogue:a",
            attrs={"repeatability": "once"},
        ),
    ]

    assert _findings(nodes, relations, "cyclic_dependency") == []


def test_gated_destination_requires_prior_access_or_quest_unlock():
    quest = Entity(id="quest:q", type=NodeType.QUEST)
    step = Entity(id="step:destination", type=NodeType.QUEST_STEP)
    region = Entity(id="region:restricted", type=NodeType.REGION)
    gate = Entity(id="gate:clearance", type=NodeType.UNLOCK_CONDITION)
    entities = [quest, step, region, gate]
    relations = [
        Relation(
            id="has-step",
            type=EdgeType.HAS_STEP,
            src_id=quest.id,
            dst_id=step.id,
        ),
        Relation(
            id="destination",
            type=EdgeType.LOCATED_IN,
            src_id=step.id,
            dst_id=region.id,
        ),
        Relation(
            id="access-gate",
            type=EdgeType.GATED_BY,
            src_id=region.id,
            dst_id=gate.id,
        ),
    ]

    findings = _findings(entities, relations, "unreachable_target")
    assert len(findings) == 1
    assert findings[0].evidence == {
        "quest": quest.id,
        "step": step.id,
        "region": region.id,
        "gate": gate.id,
        "access_proofs": [],
    }

    for edge_type in (EdgeType.REQUIRES, EdgeType.UNLOCKS):
        proof = Relation(
            id=f"proof:{edge_type.value}",
            type=edge_type,
            src_id=quest.id,
            dst_id=gate.id,
        )
        assert _findings(
            entities,
            [*relations, proof],
            "unreachable_target",
        ) == []


# --- 5. dead_quest ---

def test_dead_quest_detected_and_clean_is_silent():
    quest = Entity(id="quest:q1", type=NodeType.QUEST)
    step = Entity(id="step:s1", type=NodeType.QUEST_STEP, attrs={"kind": "talk"})
    # no STARTS_AT edge -> no giver -> can never be started
    rels = [Relation(id="r1", type=EdgeType.HAS_STEP, src_id="quest:q1", dst_id="step:s1")]
    dirty = _findings([quest, step], rels, "dead_quest")
    assert len(dirty) == 1
    assert dirty[0].evidence["has_giver"] is False
    assert dirty[0].evidence["has_steps"] is True

    giver = Entity(id="npc:giver", type=NodeType.NPC)
    clean_rels = rels + [Relation(id="r2", type=EdgeType.STARTS_AT, src_id="quest:q1", dst_id="npc:giver")]
    clean = _findings([quest, step, giver], clean_rels, "dead_quest")
    assert clean == []


# --- 6. unsatisfiable_completion ---

def test_unsatisfiable_completion_detected_and_clean_is_silent():
    quest = Entity(id="quest:q1", type=NodeType.QUEST)
    giver = Entity(id="npc:giver", type=NodeType.NPC)
    step1 = Entity(id="step:collect", type=NodeType.QUEST_STEP, attrs={"kind": "collect"})
    turn_in = Entity(id="step:turn_in", type=NodeType.QUEST_STEP, attrs={"kind": "turn_in"})
    base_rels = [
        Relation(id="r_start", type=EdgeType.STARTS_AT, src_id="quest:q1", dst_id="npc:giver"),
        Relation(id="r_s1", type=EdgeType.HAS_STEP, src_id="quest:q1", dst_id="step:collect"),
        Relation(id="r_s2", type=EdgeType.HAS_STEP, src_id="quest:q1", dst_id="step:turn_in"),
    ]
    # dirty: no PRECEDES chain linking collect -> turn_in => turn_in disconnected/unreachable
    dirty = _findings([quest, giver, step1, turn_in], base_rels, "unsatisfiable_completion")
    assert len(dirty) == 1
    assert dirty[0].evidence["turn_in_step"] == "step:turn_in"

    # clean: PRECEDES chain connects collect -> turn_in
    clean_rels = base_rels + [
        Relation(id="r_prec", type=EdgeType.PRECEDES, src_id="step:collect", dst_id="step:turn_in"),
    ]
    clean = _findings([quest, giver, step1, turn_in], clean_rels, "unsatisfiable_completion")
    assert clean == []


# --- 7. isolated_node ---

def test_isolated_node_detected_and_clean_is_silent():
    lone_item = Entity(id="item:lonely", type=NodeType.ITEM)
    dirty = _findings([lone_item], [], "isolated_node")
    assert len(dirty) == 1
    assert dirty[0].evidence["entity"] == "item:lonely"

    interactable = Entity(id="interact:gather", type=NodeType.INTERACTABLE)
    grant = Relation(id="g1", type=EdgeType.GRANTS, src_id="interact:gather", dst_id="item:lonely")
    clean = _findings([lone_item, interactable], [grant], "isolated_node")
    assert clean == []
