"""M3a Task 7: the metrics engine — detection matching + BDR/FP aggregation."""
from __future__ import annotations

from gameforge.bench.corpus import build_corpus
from gameforge.bench.inject import GroundTruth, inject
from gameforge.bench.metrics import Metric, default_constraints, detects, score_seeded
from gameforge.bench.taxonomy import Bucket, DefectClass
from gameforge.bench.bases import clean_base
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.dsl.compile import compile_all


def _report_for(snapshot, needs_nav=False):
    checkers = compile_all(default_constraints())
    # run the economy sim too, so simulation-bucket classes (economy_collapse)
    # are scorable — mirrors metrics._run_pipeline.
    from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings
    model = EconomyModel.from_snapshot(snapshot)
    sim = EconomySimulator().run(model, seed=0, n_agents=50, n_ticks=200)
    sim_findings = to_findings(sim, snapshot.snapshot_id, model=model)
    nav = None
    if needs_nav:
        from gameforge.apps.cli.ir_to_world import snapshot_to_world
        from gameforge.game.aureus.kernel import AureusEnv
        nav = AureusEnv(snapshot_to_world(snapshot)).nav_provider()
    return build_review_report(snapshot, checkers, sim_findings=sim_findings, nav=nav)


def test_detects_true_when_class_and_entity_match():
    s = inject(clean_base(), DefectClass.dangling_reference, seed=1)
    assert detects(_report_for(s.snapshot), s.ground_truth) is True


def test_detects_false_on_clean_snapshot_for_every_class():
    rep = _report_for(clean_base())
    for dc in DefectClass:
        gt = GroundTruth(defect_class=dc, injected_entities=["quest:outpost"], note="")
        assert detects(rep, gt) is False


def test_detects_false_when_class_matches_but_entity_does_not():
    s = inject(clean_base(), DefectClass.reward_out_of_range, seed=1)
    # right class, but ground-truth points at an entity the finding does not touch
    wrong = GroundTruth(defect_class=DefectClass.reward_out_of_range,
                        injected_entities=["entity:not-the-one"], note="")
    assert detects(_report_for(s.snapshot), wrong) is False


def test_every_deterministic_and_simulation_class_is_detected_end_to_end():
    # Locks per-class detectability for ALL 11 det/sim classes through the real
    # checker/sim pipeline — a silent BDR→0 regression (a checker renaming its
    # defect_class, clean-base drift, an entity-naming change) would fail here
    # even though the narrower tests above stay green. "Soundness is the selling
    # point", so every headline class carries its own end-to-end assertion.
    from gameforge.bench.taxonomy import CLASS_META

    det_sim = [dc for dc in DefectClass
               if CLASS_META[dc].bucket in (Bucket.deterministic, Bucket.simulation)]
    assert len(det_sim) == 11
    for dc in det_sim:
        s = inject(clean_base(), dc, seed=1)
        assert detects(_report_for(s.snapshot, needs_nav=s.needs_nav), s.ground_truth) is True, dc


def test_score_seeded_perfect_bdr_and_zero_oracle_fp_on_small_corpus():
    corpus = build_corpus(
        seed=0, n_clean=3,
        per_class_n={dc: (3 if dc in (DefectClass.dangling_reference,
                                      DefectClass.prob_sum_ne_1,
                                      DefectClass.economy_collapse) else 0)
                     for dc in DefectClass},
    )
    result = score_seeded(corpus, default_constraints())
    by_class = {m.defect_class: m for m in result.bdr}
    # deterministic + simulation classes score real BDR = 1.0 (all detected)
    assert by_class["dangling_reference"].rate == 1.0
    assert by_class["prob_sum_ne_1"].rate == 1.0
    assert by_class["economy_collapse"].rate == 1.0
    for m in result.bdr:
        assert m.k <= m.n and 0.0 <= m.ci_low <= m.ci_high <= 1.0
        assert isinstance(m, Metric)
    # oracle-FP (deterministic findings on clean) must be 0 — the headline KPI.
    # Reported over DISTINCT clean configs (the corpus repeats one clean base).
    assert result.oracle_fp.count == 0
    assert result.oracle_fp.n == 1


def test_score_seeded_skips_llm_assisted_classes_in_deterministic_pass():
    corpus = build_corpus(seed=0, n_clean=1,
                          per_class_n={dc: (2 if dc is DefectClass.spoiler else 0)
                                       for dc in DefectClass})
    result = score_seeded(corpus, default_constraints())
    # narrative (llm_assisted) BDR is NOT scored by the deterministic sweep
    assert all(m.bucket != Bucket.llm_assisted.value for m in result.bdr)
