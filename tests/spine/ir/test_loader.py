from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.spine.ir.loader import load_scenario


def test_loads_expected_nodes_and_has_step_edges():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    assert g.get_node("npc:lincheng").type is NodeType.NPC
    assert {e.id for e in g.nodes_of_type(NodeType.QUEST_STEP)} == {
        "step:talk_lincheng", "step:collect_emblem", "step:turn_in"
    }
    has_step = g.neighbors("quest:missing_caravan", EdgeType.HAS_STEP)
    assert len(has_step) == 3
    # ordering encoded as PRECEDES edges, not only Quest.attrs.steps (contract §2.3)
    prec = g.neighbors("step:talk_lincheng", EdgeType.PRECEDES)
    assert prec and prec[0].dst_id == "step:collect_emblem"


def test_collect_source_chain_present():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    # spawn -> interactable, interactable -> item (gather source)
    assert [r.dst_id for r in g.neighbors("spawn:emblem_pile", EdgeType.SPAWNS)] == [
        "interact:emblem_pile"
    ]
    grants = g.neighbors("interact:emblem_pile", EdgeType.GRANTS)
    assert any(r.dst_id == "item:broken_emblem" for r in grants)


def test_talk_step_targets_npc():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    talks = g.neighbors("step:talk_lincheng", EdgeType.TALKS_TO)
    assert any(r.dst_id == "npc:lincheng" for r in talks)


def test_source_ref_populated():
    snap = load_scenario("scenarios/caravan.yaml")
    sr = snap.to_graph().get_node("npc:lincheng").source_ref
    assert sr.adapter == "m0a-yaml" and sr.file.endswith("caravan.yaml")


def test_loader_accepts_dict():
    data = {
        "scenario_id": "mini",
        "grid": {"width": 3, "height": 3, "blocked": []},
        "start_pos": [0, 0],
        "npcs": [{"id": "npc:x", "region": "region:r", "pos": [1, 1]}],
        "regions": [{"id": "region:r"}],
        "quests": [],
    }
    snap = load_scenario(data)
    assert snap.to_graph().get_node("npc:x").attrs["pos"] == [1, 1]
