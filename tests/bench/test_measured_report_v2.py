"""Acceptance for the committed BenchReport v2 JSON/text/HTML bundle."""

from __future__ import annotations

from pathlib import Path

from gameforge.bench.acceptance import (
    load_m3_evidence_bundle,
    validate_m3_acceptance,
)
from gameforge.bench.panel import render_html
from gameforge.bench.report import format_text, report_projection
from gameforge.bench.report_contracts import (
    canonical_report_bytes,
    load_bench_report,
)
from gameforge.bench.run_bench import main as run_bench_main
from gameforge.bench.taxonomy import DefectClass

_ROOT = Path(__file__).parents[2]
_REPORT = _ROOT / "scenarios/bench/bench-report.json"
_TEXT = _ROOT / "scenarios/bench/bench-report.txt"
_HTML = _ROOT / "scenarios/bench/bench-report.html"
_QA = _ROOT / "scenarios/external_cases/endless_sky/qa-evidence.json"


def test_measured_report_views_are_exact_projections_of_one_v2_model():
    report = load_bench_report(_REPORT)

    assert _REPORT.read_bytes() == canonical_report_bytes(report)
    assert _TEXT.read_text(encoding="utf-8") == format_text(report) + "\n"
    assert _HTML.read_text(encoding="utf-8") == render_html(report)
    rows = report_projection(report)
    text = _TEXT.read_text(encoding="utf-8")
    html = _HTML.read_text(encoding="utf-8")
    assert all(item.row_id in text for item in rows)
    assert all(f'data-row-id="{item.row_id}"' in html for item in rows)


def test_measured_report_has_complete_sections_and_truthful_model_versions():
    report = load_bench_report(_REPORT)

    classes = {
        item.defect_class for item in (*report.seeded, *report.narrative.bdr)
    }
    assert classes == set(DefectClass)
    assert report.meta.corpus_size >= 500
    assert report.narrative.model_snapshot.model == "gpt-5.6-sol"
    assert report.hed.model_snapshot.model == "gpt-5.6-sol"
    versions = {item.component: item.version for item in report.versions}
    assert versions["model.current"] == "openai/gpt-5.6-sol/pre-m4@1"
    assert "anthropic/claude-opus-4-8/m2a@1" in {
        value for key, value in versions.items() if key.startswith("model.historical")
    }
    assert len(report.cost_latency.agent.workloads) == 6
    assert len(report.external.verification) == 4
    assert len(report.hed.dispositions) == 4


def test_current_real_bundle_fails_only_the_explicit_qa_gate():
    report = load_bench_report(_REPORT)
    evidence = load_m3_evidence_bundle(
        report,
        report_path=_REPORT,
        repo_root=_ROOT,
    )

    failures = validate_m3_acceptance(report, evidence)

    assert not _QA.exists()
    assert [item.code for item in failures] == ["qa.evidence_missing"]


def test_report_cli_revalidates_existing_views_without_rebuilding():
    assert (
        run_bench_main(
            [
                "--validate-bundle",
                str(_REPORT.parent),
                "--repo-root",
                str(_ROOT),
            ]
        )
        == 0
    )
