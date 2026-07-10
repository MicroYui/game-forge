"""Statistical power for GameForge-Bench (M3a Task 5 / design §4).

PRD §13.4 sets a power target: each defect class needs enough seeded samples
that the Bug-Detection-Rate's 95% Wilson-score CI half-width is ≤ ±5%. This
module reverses that into `required_n(p_hat)` and reports the half-width a run
actually achieved (`achieved_half_width`) so an under-powered class is a
first-class, visible number rather than a silent weakness (answering the eval
critique "n=50 → ±14%").
"""
from __future__ import annotations

from dataclasses import dataclass

from gameforge.bench.taxonomy import DefectClass
from gameforge.spine.stats import wilson_ci


def achieved_half_width(k: int, n: int, z: float = 1.96) -> float:
    """Half the width of the Wilson score interval for `k` successes in `n`."""
    low, high = wilson_ci(k, n, z)
    return (high - low) / 2.0


def required_n(p_hat: float, half_width: float = 0.05, z: float = 1.96) -> int:
    """Smallest n≥1 whose Wilson-CI half-width at the observed rate `p_hat`
    is ≤ `half_width`. `p_hat=0.5` (max variance) gives the largest n, so it is
    the safe default when a class's true BDR is unknown. Linear scan from 1 —
    the target n is a few hundred at most, so this is cheap and exact (avoids a
    normal-approx off-by-one at the Wilson correction term)."""
    n = 1
    while n < 1_000_000:
        k = round(p_hat * n)
        if achieved_half_width(k, n, z) <= half_width:
            return n
        n += 1
    return n  # pragma: no cover — unreachable for sane half_width


@dataclass
class PowerRow:
    """Per-class power outcome, surfaced in the `BenchReport` (Task 9) so an
    under-powered class (`target_met=False`) is visible, not hidden."""

    defect_class: DefectClass
    n: int
    achieved_half_width: float
    target_met: bool
