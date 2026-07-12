from __future__ import annotations

import math
import statistics

import pytest

from gameforge.bench.stats import percentile, percentile_bootstrap_ci


def test_percentile_uses_sorted_linear_interpolation() -> None:
    assert percentile([10.0, 0.0], 0.25) == 2.5
    assert percentile([4.0], 0.75) == 4.0


def test_percentile_bootstrap_is_seeded_and_uses_frozen_protocol() -> None:
    values = [1.0, 2.0, 4.0, 8.0]

    first = percentile_bootstrap_ci(values, statistics.mean)
    second = percentile_bootstrap_ci(values, statistics.mean)

    assert first == second
    assert first.method == "percentile-bootstrap95"
    assert first.seed == 20260712
    assert first.resamples == 10_000
    assert first.low <= statistics.mean(values) <= first.high


@pytest.mark.parametrize(
    "values, quantile",
    [([], 0.5), ([1.0], -0.01), ([1.0], 1.01), ([math.inf], 0.5)],
)
def test_percentile_rejects_empty_nonfinite_or_out_of_range_inputs(
    values: list[float],
    quantile: float,
) -> None:
    with pytest.raises(ValueError):
        percentile(values, quantile)


@pytest.mark.parametrize("values", [[], [math.nan], [math.inf]])
def test_bootstrap_rejects_empty_or_nonfinite_samples(values: list[float]) -> None:
    with pytest.raises(ValueError):
        percentile_bootstrap_ci(values, statistics.mean)
