"""M3a Task 9: the BenchReport JSON contract + minimal text view."""
from __future__ import annotations

from gameforge.bench.metrics import FPReport, Metric
from gameforge.bench.power import PowerRow
from gameforge.bench.report import BenchMeta, BenchReport, ExternalReport, format_text
from gameforge.bench.taxonomy import DefectClass


def _sample() -> BenchReport:
    return BenchReport(
        seeded=[
            Metric("bdr", "dangling_reference", 80, 80, 1.0, 0.95, 1.0, "deterministic"),
            Metric("bdr", "economy_collapse", 80, 78, 0.975, 0.91, 0.99, "simulation"),
            Metric("bdr", "spoiler", 20, 0, 0.0, 0.0, 0.16, "llm_assisted"),
        ],
        oracle_fp=FPReport(40, 0, 0.0, 0.0, 0.087),
        constraint_fp=FPReport(800, 0, 0.0, 0.0, 0.005),
        agent=[Metric("playtest_completion_layered", None, 20, 14, 0.7, 0.48, 0.85, "agent")],
        power=[PowerRow(DefectClass.dangling_reference, 80, 0.04, True),
               PowerRow(DefectClass.spoiler, 20, 0.19, False)],
        meta=BenchMeta(seed=0, corpus_size=982, model_snapshot="opus4.8@s1"),
    )


def test_bench_report_round_trips_json():
    r = _sample()
    back = BenchReport.model_validate_json(r.to_json())
    assert back.oracle_fp.count == 0
    assert back.external is None  # M3b slot, interface-defined
    assert len(back.seeded) == 3
    assert back.power[1].target_met is False  # under-powered narrative class kept


def test_external_slot_accepts_m3b_report():
    r = _sample()
    r.external = ExternalReport(
        source="flare-game", n_real_entities=7, n_defect_samples=12, detected=9,
        detection_rate=0.75, ci_low=0.43, ci_high=0.93,
        clean_deterministic_findings=3, clean_findings_by_class={"isolated_node": 3},
    )
    back = BenchReport.model_validate_json(r.to_json())
    assert back.external is not None and back.external.source == "flare-game"
    assert back.external.clean_findings_by_class["isolated_node"] == 3


def test_format_text_separates_buckets_and_reports_oracle_fp_and_power():
    txt = format_text(_sample())
    assert "dangling_reference" in txt and "spoiler" in txt
    assert "oracle-fp" in txt.lower()
    # deterministic and llm-assisted reported in SEPARATE sections (never merged)
    det_idx = txt.lower().index("deterministic")
    llm_idx = txt.lower().index("llm-assisted")
    agent_idx = txt.lower().index("agent")
    assert det_idx < llm_idx < agent_idx
    # the under-powered class is visibly flagged
    assert "under-powered" in txt.lower() or "target_met=false" in txt.lower().replace(" ", "")


def test_format_text_assigns_measured_narrative_evidence_to_bench_report_v2():
    text = format_text(_sample())

    assert (
        "LLM-assisted BDR (narrative evidence is carried by BenchReport v2)"
        in text
    )
    assert "human-confirmed" not in text
