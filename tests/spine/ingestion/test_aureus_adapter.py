from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.contracts.ir import NodeType, EdgeType

def _wb():
    return {
        "regions": [{"region_id": "region:r", "name": "R", "grid": {"width": 4, "height": 4, "blocked": []},
                     "start_pos": [0, 0], "scenario_id": "sc"}],
        "npcs": [{"npc_id": "npc:a", "name": "A", "region": "region:r", "pos": [1, 0]}],
        "items": [{"item_id": "item:x", "name": "X"}],
        "quests": [{"quest_id": "q", "title": "Q", "region": "region:r", "giver": "npc:a", "reward": {"gold": 10}}],
        "quest_steps": [
            {"step_id": "s1", "quest_id": "q", "order": 0, "kind": "talk", "target": "npc:a", "item": None, "count": 1, "encounter": None},
            {"step_id": "s2", "quest_id": "q", "order": 1, "kind": "turn_in", "target": "npc:a", "item": None, "count": 1, "encounter": None},
        ],
    }

def test_to_ir_builds_typed_entities_and_has_step_edges():
    snap = AureusCsvAdapter().to_ir(_wb(), file_ref="outpost")
    g = snap.to_graph()
    assert g.get_node("npc:a").type is NodeType.NPC
    assert {e.id for e in g.nodes_of_type(NodeType.QUEST_STEP)} == {"s1", "s2"}
    assert len(g.neighbors("q", EdgeType.HAS_STEP)) == 2
    prec = g.neighbors("s1", EdgeType.PRECEDES)
    assert prec and prec[0].dst_id == "s2"
    assert g.get_node("npc:a").source_ref.sheet == "npcs"

def test_from_ir_reconstructs_workbook_field_level():
    adapter = AureusCsvAdapter()
    wb = _wb()
    back = adapter.from_ir(adapter.to_ir(wb, file_ref="outpost"))
    assert back == wb  # contract §2 anchor: from_ir(to_ir(x)) == x, field level
