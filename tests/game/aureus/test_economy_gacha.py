from gameforge.game.aureus.economy import EconomySystem
from gameforge.game.aureus.gacha import GachaSystem
from gameforge.game.aureus.rng import CountingRandom
from gameforge.contracts.world import ShopSpec, ShopEntry, GachaPoolSpec, GachaEntry, EquipmentSpec


def test_buy_deducts_gold_and_adds_item():
    econ = EconomySystem()
    player = {"stats": {"gold": 50}, "inventory": {}, "equipped": {}}
    shop = ShopSpec(shop_id="s", entries=[ShopEntry(item="item:potion", price=10)])
    res = econ.buy(player, shop, "item:potion", 3)
    assert res == "bought" and player["stats"]["gold"] == 20 and player["inventory"]["item:potion"] == 3


def test_buy_insufficient_funds_rejected():
    econ = EconomySystem()
    player = {"stats": {"gold": 5}, "inventory": {}, "equipped": {}}
    shop = ShopSpec(shop_id="s", entries=[ShopEntry(item="item:potion", price=10)])
    assert econ.buy(player, shop, "item:potion", 1) == "insufficient_funds"
    assert player["stats"]["gold"] == 5


def test_equip_applies_stat_mods():
    econ = EconomySystem()
    player = {"stats": {"atk": 5}, "inventory": {"eq:blade": 1}, "equipped": {}}
    econ.equip(player, EquipmentSpec(equipment_id="eq:blade", slot="weapon", stat_mods={"atk": 5}))
    assert player["equipped"]["weapon"] == "eq:blade" and player["stats"]["atk"] == 10


def test_equip_same_item_again_is_noop():
    econ = EconomySystem()
    blade = EquipmentSpec(equipment_id="eq:blade", slot="weapon", stat_mods={"atk": 5})
    player = {"stats": {"atk": 10}, "inventory": {"eq:blade": 2}, "equipped": {}}
    first = econ.equip(player, blade)
    assert first == "equipped"
    assert player["stats"]["atk"] == 15
    assert player["inventory"]["eq:blade"] == 1
    second = econ.equip(player, blade, previous=blade)
    assert second == "already_equipped"
    assert player["stats"]["atk"] == 15  # not re-applied
    assert player["inventory"]["eq:blade"] == 1  # not consumed again
    assert player["equipped"]["weapon"] == "eq:blade"


def test_equip_swap_reverts_old_and_applies_new():
    econ = EconomySystem()
    old = EquipmentSpec(equipment_id="eq:sword", slot="weapon", stat_mods={"atk": 3})
    new = EquipmentSpec(equipment_id="eq:blade", slot="weapon", stat_mods={"atk": 5})
    player = {
        "stats": {"atk": 8},
        "inventory": {"eq:sword": 0, "eq:blade": 1},
        "equipped": {"weapon": "eq:sword"},
    }
    result = econ.equip(player, new, previous=old)
    assert result == "equipped"
    assert player["equipped"]["weapon"] == "eq:blade"
    assert player["stats"]["atk"] == 10  # 8 - 3 (revert old) + 5 (apply new)
    assert player["inventory"]["eq:sword"] == 1  # old item returned to inventory
    assert player["inventory"].get("eq:blade", 0) == 0  # new item consumed


def test_gacha_pity_guarantees_rare_and_is_seed_reproducible():
    pool = GachaPoolSpec(gacha_pool_id="gp", cost=10,
                         entries=[GachaEntry(item="item:common", weight=1)],
                         pity_threshold=3, pity_item="item:rare")
    def pull_ten(seed):
        g = GachaSystem()
        rng = CountingRandom(seed)
        player = {"stats": {"gold": 1000}, "inventory": {}, "gacha_pity": {}}
        return g.pull(player, pool, rng, count=3)
    got = pull_ten(5)
    assert "item:rare" in got            # pity forces the rare by the 3rd pull
    assert pull_ten(5) == pull_ten(5)    # seed-reproducible
