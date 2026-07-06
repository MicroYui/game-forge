"""Aureus economy system (M0b): shop buy/sell, item use, equip.

Pure state-mutating functions over a `player` dict (`stats`, `inventory`,
`equipped`, ...). No rng is involved here — economy transactions are
deterministic arithmetic over content data (`ShopSpec`/`EquipmentSpec`), so
unlike combat/gacha there is no `CountingRandom` dependency in this module.

Every method returns a `last_action_result`-style outcome string; callers
(the kernel) surface it verbatim on `Observation.last_action_result`.
"""

from __future__ import annotations

from gameforge.contracts.world import EquipmentSpec, ShopEntry, ShopSpec

__all__ = ["EconomySystem"]


class EconomySystem:
    def buy(self, player: dict, shop: ShopSpec, item_id: str, count: int) -> str:
        entry = self._entry(shop, item_id)
        if entry is None or count <= 0:
            return "unknown_item"
        cost = entry.price * count
        gold = player["stats"].get(entry.currency, 0)
        if gold < cost:
            return "insufficient_funds"
        player["stats"][entry.currency] = gold - cost
        player["inventory"][item_id] = player["inventory"].get(item_id, 0) + count
        return "bought"

    def sell(self, player: dict, shop: ShopSpec, item_id: str, count: int) -> str:
        have = player["inventory"].get(item_id, 0)
        if count <= 0 or have < count:
            return "insufficient_items"
        entry = self._entry(shop, item_id)
        price = entry.price if entry else 0
        currency = entry.currency if entry else "gold"
        remaining = have - count
        if remaining > 0:
            player["inventory"][item_id] = remaining
        else:
            player["inventory"].pop(item_id, None)
        player["stats"][currency] = player["stats"].get(currency, 0) + price * count
        return "sold"

    def use(self, player: dict, item_id: str) -> str:
        have = player.get("inventory", {}).get(item_id, 0)
        if have <= 0:
            return "no_item"
        remaining = have - 1
        if remaining > 0:
            player["inventory"][item_id] = remaining
        else:
            player["inventory"].pop(item_id, None)
        return "used"

    def equip(
        self, player: dict, equipment: EquipmentSpec, previous: EquipmentSpec | None = None,
    ) -> str:
        """Move `equipment.equipment_id` from inventory into
        `player["equipped"][slot]`, applying its `stat_mods` onto
        `player["stats"]`. If `previous` (the spec currently occupying that
        slot, looked up by the kernel) is given and differs from the new
        equipment, its stat_mods are reverted and the old item is returned to
        inventory first — a deterministic swap, not a leak of stacked mods.
        If `previous` is the same equipment already occupying the slot, this
        is a no-op (`"already_equipped"`): no inventory consumed, no mods
        re-applied — re-equipping the same item must not double-stack stats
        or silently destroy a copy of the item.
        """
        if previous is not None and previous.equipment_id == equipment.equipment_id:
            return "already_equipped"
        have = player["inventory"].get(equipment.equipment_id, 0)
        if have <= 0:
            return "no_item"
        if previous is not None:
            for stat, mod in previous.stat_mods.items():
                player["stats"][stat] = player["stats"].get(stat, 0) - mod
            player["inventory"][previous.equipment_id] = (
                player["inventory"].get(previous.equipment_id, 0) + 1
            )
        remaining = have - 1
        if remaining > 0:
            player["inventory"][equipment.equipment_id] = remaining
        else:
            player["inventory"].pop(equipment.equipment_id, None)
        player["equipped"][equipment.slot] = equipment.equipment_id
        for stat, mod in equipment.stat_mods.items():
            player["stats"][stat] = player["stats"].get(stat, 0) + mod
        return "equipped"

    @staticmethod
    def _entry(shop: ShopSpec, item_id: str) -> ShopEntry | None:
        return next((e for e in shop.entries if e.item == item_id), None)
