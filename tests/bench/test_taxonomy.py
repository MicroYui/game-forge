"""M3a Task 1: `gameforge.bench.taxonomy` — the 15-class defect taxonomy +
oracle/bucket metadata (design §2).
"""
from __future__ import annotations

from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass

_STRUCTURAL = {
    DefectClass.dangling_reference,
    DefectClass.missing_drop_source,
    DefectClass.unreachable_target,
    DefectClass.cyclic_dependency,
    DefectClass.dead_quest,
    DefectClass.unsatisfiable_completion,
}
_NUMERIC = {
    DefectClass.reward_out_of_range,
    DefectClass.prob_sum_ne_1,
    DefectClass.non_monotonic_curve,
    DefectClass.gacha_expectation_violation,
}
_NARRATIVE = {
    DefectClass.character_violation,
    DefectClass.spoiler,
    DefectClass.faction_violation,
    DefectClass.uniqueness_violation,
}


def test_15_classes_each_have_meta_and_bucket():
    assert len(DefectClass) == 15
    for dc in DefectClass:
        assert dc in CLASS_META
        assert CLASS_META[dc].bucket in Bucket
    # narrative classes are llm-assisted; economy_collapse is simulation; rest deterministic
    assert CLASS_META[DefectClass.economy_collapse].bucket is Bucket.simulation
    assert CLASS_META[DefectClass.character_violation].bucket is Bucket.llm_assisted
    assert CLASS_META[DefectClass.dangling_reference].bucket is Bucket.deterministic


def test_exact_membership_of_the_15_classes():
    all_classes = _STRUCTURAL | _NUMERIC | _NARRATIVE | {DefectClass.economy_collapse}
    assert all_classes == set(DefectClass)
    assert len(all_classes) == 15


def test_bucket_partition_matches_design_table():
    for dc in _STRUCTURAL:
        assert CLASS_META[dc].bucket is Bucket.deterministic, dc
    for dc in _NUMERIC:
        assert CLASS_META[dc].bucket is Bucket.deterministic, dc
    assert CLASS_META[DefectClass.economy_collapse].bucket is Bucket.simulation
    for dc in _NARRATIVE:
        assert CLASS_META[dc].bucket is Bucket.llm_assisted, dc
    # deterministic + simulation + llm_assisted never overlap in membership
    det_and_sim = {dc for dc, meta in CLASS_META.items() if meta.bucket is not Bucket.llm_assisted}
    assert det_and_sim.isdisjoint(_NARRATIVE)


def test_every_meta_has_a_non_empty_oracle_string():
    for dc in DefectClass:
        assert CLASS_META[dc].oracle  # non-empty, truthy
