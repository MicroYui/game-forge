"""Pure text and static-HTML projections of BenchReport v2."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from gameforge.bench.panel import render_html
from gameforge.bench.report import (
    format_text,
    report_projection,
    validate_report_bundle,
    write_report_bundle,
)
from gameforge.bench.report_contracts import VersionRef, load_bench_report
from tests.bench.test_bench_report import _sample_report


def test_text_and_html_render_every_authoritative_projection_row():
    report = _sample_report()
    rows = report_projection(report)
    text = format_text(report)
    document = render_html(report)

    assert rows
    assert len({row.row_id for row in rows}) == len(rows)
    for row in rows:
        assert row.row_id in text
        assert f'data-row-id="{row.row_id}"' in document


def test_pending_values_render_as_unavailable_not_zero():
    report = _sample_report()
    text = format_text(report)
    document = render_html(report)

    assert "qa.paired_saved_minutes" in text
    assert "pending" in text
    assert "unavailable" in text
    assert "qa.paired_saved_minutes" in document
    assert ">0.000<" not in document


def test_projection_surfaces_every_product_evidence_section():
    rows = {row.row_id: row for row in report_projection(_sample_report())}

    assert "external.development.cyclic_dependency" in rows
    assert "external.verification.cyclic_dependency" in rows
    assert rows["hed.disposition.hed_unusable"].value == "2/8 (25.0%)"
    assert rows["qa.scope"].value == "single-participant-eight-session-case-study"
    assert rows["qa.time_scoring"].value == "incorrect_uses_active_cap"
    assert rows["cost.narrative-verification.tokens.input"].value == "80"
    assert rows["cost.narrative-verification.tokens.cache_read"].value == "0"
    assert rows["cost.narrative-verification.transport.unknown_records"].value == "8"
    assert "environment" in rows["runtime.environment_sha256"].label.lower()
    assert rows["power.character_violation"].status == "measured"
    assert rows["power.dangling_reference"].evidence_ref == "seeded"
    assert rows["narrative.model_snapshot"].value == "openai/gpt-5.6-sol/pre-m4@1"


def test_render_html_is_self_contained_static_and_escaped():
    report = _sample_report()
    versions = tuple(report.versions) + (
        VersionRef(component="test.escape", version='<script>alert("x")</script>'),
    )
    escaped_report = report.model_copy(update={"versions": versions})
    document = render_html(escaped_report)

    assert document.startswith("<!doctype html>")
    assert document.endswith("</html>\n")
    assert "<script" not in document.lower()
    assert "&lt;script&gt;" in document
    assert '<meta charset="utf-8">' in document
    for section in (
        "Seeded BDR",
        "False Positives",
        "Agent Outcomes",
        "Power",
        "External Development",
        "External Verification",
        "Narrative",
        "Human Edit Distance",
        "QA Study",
        "Agent Cost and Latency",
        "Deterministic Runtime",
        "Versions",
        "Evidence Artifacts",
    ):
        assert section in document


def test_write_report_bundle_uses_one_report_for_all_three_views(tmp_path: Path):
    report = _sample_report()
    json_path, text_path, html_path = write_report_bundle(report, tmp_path)

    assert load_bench_report(json_path) == report
    assert text_path.read_text(encoding="utf-8") == format_text(report) + "\n"
    assert html_path.read_text(encoding="utf-8") == render_html(report)


def test_validate_report_bundle_rejects_a_tampered_projection(tmp_path: Path):
    report = _sample_report()
    _, text_path, _ = write_report_bundle(report, tmp_path)

    assert validate_report_bundle(tmp_path) == report
    text_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authoritative JSON"):
        validate_report_bundle(tmp_path)


def test_panel_module_has_no_evidence_checker_or_agent_dependencies():
    root = Path(__file__).parents[2]
    path = root / "gameforge/bench/panel.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    assert not any(
        name.startswith(
            (
                "gameforge.agents",
                "gameforge.bench.external",
                "gameforge.bench.hed",
                "gameforge.bench.narrative",
                "gameforge.bench.qa",
                "gameforge.spine",
            )
        )
        for name in imports
    )
