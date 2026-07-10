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


def test_to_ir_derives_starts_at_and_rewards_from_quest_row():
    # quests.giver -> STARTS_AT(quest -> giver); quests.reward.item -> REWARDS
    # (quest -> item), mirroring the M0a loader (`spine/ir/loader.py`). Without
    # these, GraphChecker's dead_quest/isolated_node fire as false positives on
    # any quest ingested purely from CSV (contract §12A.1 oracle-FP=0 anchor).
    snap = AureusCsvAdapter().to_ir(_wb(), file_ref="outpost")
    g = snap.to_graph()
    starts = g.neighbors("q", EdgeType.STARTS_AT, direction="out")
    assert len(starts) == 1 and starts[0].dst_id == "npc:a"
    rewards = g.neighbors("q", EdgeType.REWARDS, direction="out")
    assert rewards == []  # this fixture's reward has no "item" key


def test_to_ir_derives_rewards_when_reward_has_item():
    wb = _wb()
    wb["quests"][0]["reward"] = {"gold": 10, "item": "item:x"}
    snap = AureusCsvAdapter().to_ir(wb, file_ref="outpost")
    g = snap.to_graph()
    rewards = g.neighbors("q", EdgeType.REWARDS, direction="out")
    assert len(rewards) == 1 and rewards[0].dst_id == "item:x"


def test_to_ir_skips_starts_at_when_giver_blank():
    wb = _wb()
    wb["quests"][0]["giver"] = ""
    snap = AureusCsvAdapter().to_ir(wb, file_ref="outpost")
    g = snap.to_graph()
    assert g.neighbors("q", EdgeType.STARTS_AT, direction="out") == []


def test_to_ir_derives_monster_currency_drops_from_gold_attrs():
    # Monster rows carrying economy-sim gold-drop attrs (`gold_min`/`gold_max`/
    # `currency`, all optional beyond the base monsters schema) derive a
    # DROPS_FROM(monster -> currency) edge — the direction EconomySimulator's
    # `EconomyModel.from_snapshot` expects (src=producer, dst=currency). This
    # is the OPPOSITE direction from the item-drop DROPS_FROM edges above
    # (src=item, dst=monster) -- DROPS_FROM is contract-wide overloaded for two
    # distinct "produces" relationships that never collide (item ids and
    # currency ids are disjoint namespaces).
    wb = _wb()
    wb["monsters"] = [{
        "monster_id": "m:wolf", "name": "Wolf",
        "stats": {"atk": 1, "def": 1, "hp": 1}, "skills": None,
        "drop_table_id": None, "ai": "aggressive",
        "gold_min": 5, "gold_max": 10, "currency": "gold",
    }]
    snap = AureusCsvAdapter().to_ir(wb, file_ref="outpost")
    g = snap.to_graph()
    drops = g.neighbors("m:wolf", EdgeType.DROPS_FROM, direction="out")
    assert len(drops) == 1 and drops[0].dst_id == "gold"


def test_to_ir_skips_monster_currency_drops_from_without_gold_attrs():
    wb = _wb()
    wb["monsters"] = [{
        "monster_id": "m:wolf", "name": "Wolf",
        "stats": {"atk": 1, "def": 1, "hp": 1}, "skills": None,
        "drop_table_id": None, "ai": "aggressive",
    }]
    snap = AureusCsvAdapter().to_ir(wb, file_ref="outpost")
    g = snap.to_graph()
    assert g.neighbors("m:wolf", EdgeType.DROPS_FROM, direction="out") == []


def test_to_ir_plumbs_sells_relation_attrs():
    wb = _wb()
    wb["shops"] = [{"shop_id": "shop:s", "entries": [
        {"currency": "gold", "item": "item:x", "price": 50, "buy_prob": 0.5}]}]
    snap = AureusCsvAdapter().to_ir(wb, file_ref="outpost")
    g = snap.to_graph()
    sells = g.neighbors("shop:s", EdgeType.SELLS, direction="out")
    assert len(sells) == 1
    assert sells[0].dst_id == "item:x"
    assert sells[0].attrs["price"] == 50
    assert sells[0].attrs["currency"] == "gold"
    assert sells[0].attrs["buy_prob"] == 0.5


def test_to_ir_sells_omits_buy_prob_when_absent():
    # buy_prob absent from config -> key omitted so from_snapshot applies its
    # own default (0.5); price/currency still plumbed.
    wb = _wb()
    wb["shops"] = [{"shop_id": "shop:s", "entries": [
        {"currency": "gold", "item": "item:x", "price": 50}]}]
    snap = AureusCsvAdapter().to_ir(wb, file_ref="outpost")
    g = snap.to_graph()
    sells = g.neighbors("shop:s", EdgeType.SELLS, direction="out")
    assert sells[0].attrs.get("price") == 50
    assert "buy_prob" not in sells[0].attrs


def test_from_ir_roundtrip_lossless_with_shop_buy_prob():
    # Relations are NOT read back by from_ir (rebuilt from entity attrs), so
    # adding relation attrs must not change from_ir(to_ir(x)) == x.
    wb = _wb()
    wb["shops"] = [{"shop_id": "shop:s", "entries": [
        {"currency": "gold", "item": "item:x", "price": 50, "buy_prob": 0.5}]}]
    adapter = AureusCsvAdapter()
    back = adapter.from_ir(adapter.to_ir(wb, file_ref="outpost"))
    assert back == wb
