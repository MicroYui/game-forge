import pytest
from pydantic import ValidationError

from gameforge.contracts.ir import Entity, EdgeType, NodeType, Relation, SourceRef


def test_entity_defaults_schema_version():
    e = Entity(id="npc:lincheng", type=NodeType.NPC)
    assert e.schema_version == "ir-core@1"
    assert e.attrs == {}


def test_relation_requires_id_and_endpoints():
    r = Relation(id="r1", type=EdgeType.HAS_STEP, src_id="q1", dst_id="s1")
    assert r.type is EdgeType.HAS_STEP
    assert r.src_id == "q1" and r.dst_id == "s1"


def test_combat_economy_types_reserved_now():
    # combat-economy node types are declared in M0a (impl deferred to M0b), not cut
    assert NodeType.BATTLE_ENCOUNTER.value == "BATTLE_ENCOUNTER"
    assert NodeType.SKILL.value == "SKILL"
    assert NodeType.FORMULA.value == "FORMULA"


def test_source_ref_fields():
    sr = SourceRef(adapter="m0a-yaml", file="caravan.yaml", sheet="npcs", row=3)
    assert sr.adapter == "m0a-yaml" and sr.row == 3 and sr.column is None


def test_source_ref_forbids_extra():
    with pytest.raises(ValidationError):
        SourceRef(adapter="x", file="y", bogus="z")


def test_all_core_node_types_present():
    for name in [
        "FACTION", "CHARACTER", "NPC", "QUEST", "QUEST_STEP", "DIALOGUE_NODE",
        "REGION", "SPAWN_POINT", "INTERACTABLE", "ITEM", "MONSTER", "CURRENCY",
        "SHOP", "DROP_TABLE", "REWARD_TABLE", "GACHA_POOL", "EVENT", "UNLOCK_CONDITION",
    ]:
        assert hasattr(NodeType, name)


def test_all_edge_types_present():
    for name in [
        "HAS_STEP", "PRECEDES", "REQUIRES", "GATED_BY", "UNLOCKS", "STARTS_AT",
        "TALKS_TO", "TRIGGERED_BY", "LOCATED_IN", "CONTAINS", "SPAWNS", "PATH_TO",
        "DROPS_FROM", "GRANTS", "CONSUMES", "REWARDS", "SELLS", "USES_SKILL",
        "APPLIES_EFFECT", "HAS_STAT_CURVE", "HOSTILE_TO", "ALLY_WITH", "BELONGS_TO",
        "REVEALS", "REFERENCES",
    ]:
        assert hasattr(EdgeType, name)
