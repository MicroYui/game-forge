"""Spine-local seeded RNG (M1 Task 8 / M1-D6).

Mirrors `gameforge.game.aureus.rng.CountingRandom`'s semantics (seeded
`random.Random` wrapper, a public `draws` counter incremented on every draw so
replay/state-hashing can cover RNG progress) but is *re-implemented* here
because `spine` may only depend on `gameforge.contracts` + `gameforge.spine.*`
and must never import `gameforge.game.*` (M1-D6 / import-linter contract).

Determinism contract: same seed + same call order (regardless of which method
is called) -> identical draw sequence. No wall-clock, no external entropy.
"""

from __future__ import annotations

import random


class SimRandom:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._rng = random.Random(seed)
        self.draws: int = 0

    def randint(self, a: int, b: int) -> int:
        self.draws += 1
        return self._rng.randint(a, b)

    def random(self) -> float:
        self.draws += 1
        return self._rng.random()

    def weighted_choice(self, items: list, weights: list):
        """Pick one item from `items` weighted by the parallel `weights` list."""
        if not items or len(items) != len(weights):
            raise ValueError("items and weights must be same non-empty length")
        total = sum(weights)
        if total <= 0:
            raise ValueError("weights must sum to a positive value")
        self.draws += 1
        r = self._rng.uniform(0, total)
        upto = 0.0
        for item, weight in zip(items, weights):
            upto += weight
            if r <= upto:
                return item
        return items[-1]  # float rounding fallback — deterministic tie-break
