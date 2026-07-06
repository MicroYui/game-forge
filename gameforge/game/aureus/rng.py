"""Seeded counting RNG (Aureus M0b).

Wraps `random.Random(seed)` so every draw — regardless of which method is
used — increments a public `draws` counter. Determinism is seed + call
order: replaying the same sequence of calls against a fresh instance with
the same seed reproduces identical results (contract §4.4 state_hash covers
`rng: {seed, draws}`).
"""

from __future__ import annotations

import random


class CountingRandom:
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

    def roll(self, prob: float) -> bool:
        """True with probability `prob` (draws exactly one random value)."""
        return self.random() < prob

    def weighted_choice(self, items: list, weights: list[int]):
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
