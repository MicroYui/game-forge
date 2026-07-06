"""Aureus gacha system (M0b): pity-aware weighted pulls over a seeded rng.

Every pull spends `pool.cost` currency and increments the pool's pity
counter on `player["gacha_pity"]`; hitting `pity_threshold` force-awards
`pity_item` and resets the counter — otherwise the pull draws from
`CountingRandom.weighted_choice` (contract §4.4: rng draws must flow through
the shared counting rng, so replay-determinism holds — a pity-forced pull
consumes no rng draw at all, which is fine since it is fully determined by
the counter, itself part of authoritative state).

Gacha is reached via the `buy` atomic (contract §4.2 — no Env-contract
change): the kernel routes `Buy(shop_id=pool_id)` here instead of to
`EconomySystem.buy` when `shop_id` names a gacha pool.
"""

from __future__ import annotations

from gameforge.contracts.world import GachaPoolSpec
from gameforge.game.aureus.rng import CountingRandom

__all__ = ["GachaSystem"]


class GachaSystem:
    def pull(self, player: dict, pool: GachaPoolSpec, rng: CountingRandom, count: int) -> list[str]:
        if count <= 0:
            return []
        cost = pool.cost * count
        gold = player["stats"].get(pool.currency, 0)
        if gold < cost:
            return []
        player["stats"][pool.currency] = gold - cost

        pity = player.setdefault("gacha_pity", {})
        counter = pity.get(pool.gacha_pool_id, 0)
        items = [e.item for e in pool.entries]
        weights = [e.weight for e in pool.entries]

        results: list[str] = []
        for _ in range(count):
            counter += 1
            if pool.pity_threshold > 0 and counter >= pool.pity_threshold:
                results.append(pool.pity_item)
                counter = 0
            else:
                results.append(rng.weighted_choice(items, weights))
        pity[pool.gacha_pool_id] = counter
        return results
