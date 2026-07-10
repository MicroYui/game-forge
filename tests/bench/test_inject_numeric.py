"""M3a Task 3: property tests for the 5 numeric/economy defect injectors.

Each test verifies the injected defect via a DIRECT numeric assertion on the
mutated snapshot — never by running a checker/sim (that is `bench/metrics.py`'s
job at Task 7). This keeps the injectors provably independent of the oracle
that scores them (design §8 anti-circularity). Every injector is also checked
for seeded reproducibility and correct GroundTruth.
"""
from __future__ import annotations

from fractions import Fraction

from gameforge.bench.inject import inject
from gameforge.bench.taxonomy import Bucket, CLASS_META, DefectClass

from tests.bench.testbases import clean_base


def _entity(snapshot, eid):
    return snapshot.entities[eid]


def _drop_probs(snapshot, table_id) -> list:
    return [e["probability"] for e in _entity(snapshot, table_id).attrs["entries"]]


def _curve(snapshot, formula_id) -> list:
    return list(_entity(snapshot, formula_id).attrs["curve"])


def _quest_reward_gold(snapshot, quest_id) -> int:
    return _entity(snapshot, quest_id).attrs["reward"]["gold"]


def _monster_gold(snapshot, monster_id):
    a = _entity(snapshot, monster_id).attrs
    return a.get("gold_min"), a.get("gold_max")


def test_reward_out_of_range_exceeds_cap():
    s = inject(clean_base(), DefectClass.reward_out_of_range, seed=1)
    qid = s.ground_truth.injected_entities[0]
    # constraint is `reward.gold <= 150`; the injected reward must break it
    assert _quest_reward_gold(s.snapshot, qid) > 150
    assert _quest_reward_gold(clean_base(), qid) <= 150  # base is clean


def test_prob_sum_ne_1_breaks_the_exact_sum():
    s = inject(clean_base(), DefectClass.prob_sum_ne_1, seed=1)
    tbl = s.ground_truth.injected_entities[0]
    total = sum(Fraction(str(p)) for p in _drop_probs(s.snapshot, tbl))
    assert total != Fraction(1)
    base_total = sum(Fraction(str(p)) for p in _drop_probs(clean_base(), tbl))
    assert base_total == Fraction(1)  # base sums to exactly 1


def test_non_monotonic_curve_has_a_decrease():
    s = inject(clean_base(), DefectClass.non_monotonic_curve, seed=1)
    fid = s.ground_truth.injected_entities[0]
    powers = _curve(s.snapshot, fid)
    assert any(powers[i + 1] < powers[i] for i in range(len(powers) - 1))
    base_powers = _curve(clean_base(), fid)
    assert all(base_powers[i + 1] >= base_powers[i] for i in range(len(base_powers) - 1))


def test_gacha_expectation_violation_raises_expected_pulls_over_budget():
    s = inject(clean_base(), DefectClass.gacha_expectation_violation, seed=1)
    gid = s.ground_truth.injected_entities[0]
    a = _entity(s.snapshot, gid).attrs
    # a lower base_rate and/or higher pity pushes expected pulls above budget;
    # assert the raw knobs moved the wrong way vs the (clean) budget-respecting base
    base = _entity(clean_base(), gid).attrs
    assert a["base_rate"] < base["base_rate"] or a["pity_threshold"] > base["pity_threshold"]
    assert a["max_expected_pulls"] == base["max_expected_pulls"]  # budget unchanged


def test_economy_collapse_inflates_monster_gold():
    s = inject(clean_base(), DefectClass.economy_collapse, seed=1)
    mid = s.ground_truth.injected_entities[0]
    gmin, gmax = _monster_gold(s.snapshot, mid)
    assert gmin is not None and gmax is not None and gmax >= gmin
    assert gmax >= 200  # a runaway faucet, far above any sink price in the clean base


def test_numeric_injectors_seeded_reproducible():
    for dc in [
        DefectClass.reward_out_of_range, DefectClass.prob_sum_ne_1,
        DefectClass.non_monotonic_curve, DefectClass.gacha_expectation_violation,
        DefectClass.economy_collapse,
    ]:
        a = inject(clean_base(), dc, seed=7)
        b = inject(clean_base(), dc, seed=7)
        c = inject(clean_base(), dc, seed=8)
        assert a.snapshot.snapshot_id == b.snapshot.snapshot_id, dc
        assert a.snapshot.snapshot_id != c.snapshot.snapshot_id, dc  # value varies by seed


def test_numeric_injectors_ground_truth_and_bucket():
    for dc in [
        DefectClass.reward_out_of_range, DefectClass.prob_sum_ne_1,
        DefectClass.non_monotonic_curve, DefectClass.gacha_expectation_violation,
        DefectClass.economy_collapse,
    ]:
        s = inject(clean_base(), dc, seed=3)
        assert s.ground_truth.defect_class is dc
        assert s.ground_truth.injected_entities
    assert CLASS_META[DefectClass.economy_collapse].bucket is Bucket.simulation
    for dc in (DefectClass.reward_out_of_range, DefectClass.prob_sum_ne_1,
               DefectClass.non_monotonic_curve, DefectClass.gacha_expectation_violation):
        assert CLASS_META[dc].bucket is Bucket.deterministic
