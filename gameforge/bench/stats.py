"""Deterministic distribution statistics shared by benchmark evidence."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal


BOOTSTRAP_SEED = 20260712
BOOTSTRAP_RESAMPLES = 10_000


@dataclass(frozen=True)
class BootstrapInterval:
    low: float
    high: float
    method: Literal["percentile-bootstrap95"] = "percentile-bootstrap95"
    seed: int = BOOTSTRAP_SEED
    resamples: int = BOOTSTRAP_RESAMPLES


def _finite_sample(values: Sequence[float]) -> tuple[float, ...]:
    sample = tuple(float(value) for value in values)
    if not sample:
        raise ValueError("statistical sample must not be empty")
    if any(not math.isfinite(value) for value in sample):
        raise ValueError("statistical sample must contain only finite values")
    return sample


def percentile(values: Sequence[float], quantile: float) -> float:
    """Return a linearly interpolated percentile over a finite sample."""

    sample = sorted(_finite_sample(values))
    if not math.isfinite(quantile) or not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be finite and within [0, 1]")
    if len(sample) == 1:
        return sample[0]
    position = quantile * (len(sample) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sample[lower]
    fraction = position - lower
    return sample[lower] + (sample[upper] - sample[lower]) * fraction


def percentile_bootstrap_ci(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float],
) -> BootstrapInterval:
    """Frozen two-sided 95% percentile bootstrap used by pre-M4 evidence."""

    sample = _finite_sample(values)
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(sample)
    estimates: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        resample = [sample[rng.randrange(n)] for _ in range(n)]
        estimate = float(statistic(resample))
        if not math.isfinite(estimate):
            raise ValueError("bootstrap statistic returned a non-finite value")
        estimates.append(estimate)
    estimates.sort()
    lower_index = math.floor(0.025 * (BOOTSTRAP_RESAMPLES - 1))
    upper_index = math.ceil(0.975 * (BOOTSTRAP_RESAMPLES - 1))
    return BootstrapInterval(
        low=estimates[lower_index],
        high=estimates[upper_index],
    )


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "BootstrapInterval",
    "percentile",
    "percentile_bootstrap_ci",
]
