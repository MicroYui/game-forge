"""Shared statistics helpers for the deterministic trunk (M3a Task 1).

`wilson_ci` originated in `gameforge.agents.playtest_harness` (M2b-1 Task 7);
it moves here so both `agents` (playtest/repair harnesses) and `bench`
(GameForge-Bench, M3a) share ONE implementation instead of two copies
drifting apart. Pure stdlib, no LLM/business imports — this module lives on
the `spine` side of the dependency direction (hard rule 4), so `bench`'s
seeded core can depend on it without ever reaching into `agents`.
"""

from __future__ import annotations

import math


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% (by default) Wilson score interval for `k` successes of `n` trials.

    `n <= 0` → `(0.0, 1.0)`: with zero trials there is no information at all,
    so the honest interval is the full `[0, 1]` range, not a degenerate point
    at 0 (which would silently overstate confidence and also lets callers
    avoid a division-by-zero guard of their own).

    The bounds are clamped to `[0, 1]` and pinned to bracket the point
    estimate p̂ (a Wilson interval always contains p̂ mathematically; the pin
    removes floating-point noise at the p̂∈{0,1} extremes, where the exact
    bounds are 0.0 and 1.0 respectively).
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    low = (center - spread) / denom
    high = (center + spread) / denom
    return (max(0.0, min(low, p)), min(1.0, max(high, p)))
