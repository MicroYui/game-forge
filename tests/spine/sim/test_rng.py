"""Tests for `gameforge.spine.sim.rng.SimRandom` (M1 Task 8 Step 1)."""

from __future__ import annotations

import pytest

from gameforge.spine.sim.rng import SimRandom


def test_same_seed_same_sequence_regardless_of_method_mix():
    r1 = SimRandom(42)
    r2 = SimRandom(42)

    def draw(r: SimRandom) -> list:
        out = [r.randint(1, 100) for _ in range(5)]
        out += [r.random() for _ in range(3)]
        out += [r.weighted_choice(["a", "b", "c"], [1, 2, 3]) for _ in range(4)]
        return out

    seq1 = draw(r1)
    seq2 = draw(r2)
    assert seq1 == seq2
    assert r1.draws == r2.draws == 12


def test_different_seed_diverges():
    r1 = SimRandom(1)
    r2 = SimRandom(2)
    seq1 = [r1.randint(1, 1_000_000) for _ in range(10)]
    seq2 = [r2.randint(1, 1_000_000) for _ in range(10)]
    assert seq1 != seq2


def test_draws_counter_increments_exactly_once_per_call():
    r = SimRandom(7)
    assert r.draws == 0
    r.randint(1, 10)
    assert r.draws == 1
    r.random()
    assert r.draws == 2
    r.weighted_choice([1, 2], [1, 1])
    assert r.draws == 3
    r.randint(1, 10)
    r.randint(1, 10)
    assert r.draws == 5


def test_weighted_choice_rejects_mismatched_lengths():
    r = SimRandom(1)
    with pytest.raises(ValueError):
        r.weighted_choice(["a", "b"], [1])


def test_weighted_choice_rejects_nonpositive_total_weight():
    r = SimRandom(1)
    with pytest.raises(ValueError):
        r.weighted_choice(["a", "b"], [0, 0])
