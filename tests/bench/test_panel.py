"""M3c: minimal static HTML view of a BenchReport."""
from __future__ import annotations

from gameforge.bench.metrics import FPReport, Metric
from gameforge.bench.panel import render_html
from gameforge.bench.power import PowerRow
from gameforge.bench.report import BenchMeta, BenchReport, ExternalReport
from gameforge.bench.taxonomy import DefectClass


def _sample() -> BenchReport:
    return BenchReport(
        seeded=[
            Metric("bdr", "dangling_reference", 82, 82, 1.0, 0.95, 1.0, "deterministic"),
            Metric("bdr", "economy_collapse", 82, 80, 0.976, 0.92, 0.99, "simulation"),
            Metric("bdr", "spoiler", 0, 0, 0.0, 0.0, 1.0, "llm_assisted"),
        ],
        oracle_fp=FPReport(1, 0, 0.0, 0.0, 0.79),
        constraint_fp=FPReport(900, 0, 0.0, 0.0, 0.004),
        agent=[Metric("playtest_completion_layered", None, 20, 14, 0.7, 0.48, 0.85, "agent")],
        power=[PowerRow(DefectClass.dangling_reference, 82, 0.04, True),
               PowerRow(DefectClass.spoiler, 20, 0.19, False)],
        meta=BenchMeta(seed=0, corpus_size=982, model_snapshot="opus4.8@s1"),
        external=ExternalReport(source="flare-game", n_real_entities=7,
                                clean_deterministic_findings=3,
                                clean_findings_by_class={"isolated_node": 3},
                                note="adapter-completeness artifact; deferred"),
    )


def test_render_html_is_self_contained_and_covers_all_sections():
    doc = render_html(_sample())
    assert doc.startswith("<!doctype html>") and doc.endswith("</html>")
    assert "<script" not in doc.lower()  # no JS — minimal, static (full UI is M4)
    # every section present
    for token in ("dangling_reference", "economy_collapse", "spoiler",
                  "oracle-FP", "constraint-FP", "power", "External validity",
                  "flare-game", "isolated_node", "playtest_completion_layered"):
        assert token in doc, token


def test_render_html_flags_oracle_fp_ok_and_underpowered_class():
    doc = render_html(_sample())
    assert "class='num ok'>0/1" in doc          # oracle-FP=0 marked ok
    assert "UNDER-POWERED" in doc                # spoiler n=20 flagged
    # buckets kept visually separate
    assert "bucket-deterministic" in doc and "bucket-llm_assisted" in doc


def test_render_html_escapes_content():
    r = _sample()
    r.meta.model_snapshot = "<script>alert(1)</script>"
    doc = render_html(r)
    assert "<script>alert(1)</script>" not in doc
    assert "&lt;script&gt;" in doc  # escaped
