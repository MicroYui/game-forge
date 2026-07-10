"""M3a Task 10: end-to-end GameForge-Bench report."""
from __future__ import annotations

from gameforge.bench.run_bench import build_bench_report
from gameforge.bench.taxonomy import DefectClass


def test_end_to_end_bench_report_all_classes_zero_oracle_fp():
    # small per-class n keeps the checker/sim sweep fast; the ≥500 corpus is
    # exercised by tests/bench/test_corpus.py (generation) — this proves the
    # full pipeline assembles a coherent report.
    r = build_bench_report(
        seed=0, with_agent=False,
        per_class_n={dc: 2 for dc in DefectClass}, n_clean=2,
    )
    # headline KPI: zero oracle false positives on the clean base
    assert r.oracle_fp.count == 0

    # every one of the 15 classes is reported (分缺陷类 BDR 报告齐全)
    classes = {m.defect_class for m in r.seeded}
    assert classes == {dc.value for dc in DefectClass}

    # deterministic, simulation, and llm-assisted buckets are reported SEPARATELY
    buckets = {m.bucket for m in r.seeded}
    assert {"deterministic", "simulation", "llm_assisted"} <= buckets

    # every metric carries a valid Wilson CI
    for m in r.seeded:
        assert 0.0 <= m.ci_low <= m.ci_high <= 1.0

    # power table has a row per class; narrative rows are under-powered (bounded n)
    assert len(r.power) == len(DefectClass)
    assert any(not pr.target_met for pr in r.power)

    # deterministic classes actually detect (BDR = 1.0 at n=2 all-detected)
    det = {m.defect_class: m for m in r.seeded if m.bucket == "deterministic"}
    assert det["dangling_reference"].rate == 1.0


def test_bench_report_json_serialises():
    r = build_bench_report(seed=0, with_agent=False,
                           per_class_n={DefectClass.prob_sum_ne_1: 2}, n_clean=1)
    assert r.to_json().strip().startswith("{")
