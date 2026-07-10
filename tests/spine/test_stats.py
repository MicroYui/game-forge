"""M3a Task 1: `gameforge.spine.stats.wilson_ci` — the shared implementation
moved out of `gameforge.agents.playtest_harness` so `bench` can use it without
reaching into `agents` (hard rule 4 / dependency direction).
"""
from __future__ import annotations

from gameforge.spine.stats import wilson_ci


def test_wilson_ci_bounds_and_monotonic():
    lo0, hi0 = wilson_ci(0, 20)
    assert 0.0 <= lo0 <= hi0 <= 1.0
    prev = (lo0, hi0)
    for k in range(1, 21):
        lo, hi = wilson_ci(k, 20)
        assert lo >= prev[0] and hi >= prev[1] and 0.0 <= lo <= hi <= 1.0
        prev = (lo, hi)
    assert wilson_ci(0, 0) == (0.0, 1.0)  # n=0 → full interval, no div-by-zero


def test_wilson_ci_negative_n_also_guarded():
    # defensive: a negative n (should never happen upstream) must not raise
    # either — same full-interval contract as n == 0.
    assert wilson_ci(0, -1) == (0.0, 1.0)


def test_wilson_ci_custom_z_still_brackets_point_estimate():
    low, high = wilson_ci(5, 10, z=2.58)  # ~99% interval, wider than default
    assert low <= 0.5 <= high
    low95, high95 = wilson_ci(5, 10)
    assert low <= low95 and high >= high95  # a wider z widens the interval
