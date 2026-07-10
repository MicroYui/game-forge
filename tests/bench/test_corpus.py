"""M3a Task 6: the ≥500-sample seeded corpus (design §4)."""
from __future__ import annotations

from gameforge.bench.corpus import Corpus, build_corpus
from gameforge.bench.taxonomy import Bucket, CLASS_META, DefectClass


def test_corpus_has_at_least_500_samples_covering_every_class():
    c = build_corpus(seed=0)
    assert isinstance(c, Corpus)
    assert len(c.samples) >= 500
    covered = {s.ground_truth.defect_class for s in c.samples}
    assert covered == set(DefectClass)  # all 15 classes represented


def test_clean_denominator_present():
    c = build_corpus(seed=0, n_clean=12)
    assert len(c.clean) == 12


def test_default_per_class_n_powers_deterministic_and_bounds_narrative():
    c = build_corpus(seed=0)
    det = c.per_class_n[DefectClass.dangling_reference]
    narr = c.per_class_n[DefectClass.spoiler]
    assert det >= 70   # power-driven for a high-BDR deterministic class
    assert narr == 20  # narrative is bounded (cassette-bound, honestly under-powered)
    assert CLASS_META[DefectClass.spoiler].bucket is Bucket.llm_assisted


def test_corpus_is_seeded_reproducible():
    a = [s.snapshot.snapshot_id for s in build_corpus(seed=0).samples]
    b = [s.snapshot.snapshot_id for s in build_corpus(seed=0).samples]
    assert a == b  # identical sequence across two builds


def test_per_class_samples_distinct_where_injector_varies():
    # reward_out_of_range varies its injected value by seed → all samples distinct.
    c = build_corpus(seed=0, per_class_n={dc: (5 if dc is DefectClass.reward_out_of_range else 1)
                                          for dc in DefectClass}, n_clean=1)
    reward = [s.snapshot.snapshot_id for s in c.samples
              if s.ground_truth.defect_class is DefectClass.reward_out_of_range]
    assert len(reward) == 5
    assert len(set(reward)) == 5  # value-varying injector → pairwise distinct
