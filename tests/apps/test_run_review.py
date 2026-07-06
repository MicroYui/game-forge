"""run_review (M1 Task 11): CLI-facing orchestration — read_workbook +
AureusCsvAdapter.to_ir (bypassing SchemaRegistry.validate, unlike
run_slice_workbook, since some defect scenarios are intentionally
schema-invalid, e.g. a blank required `giver` cell) -> Constraint.from_yaml
(every *.yaml under constraints_path) -> compile_all -> economy sim ->
build_review_report.
"""

from __future__ import annotations

from gameforge.apps.cli.run_review import run_review

_SCENARIOS = "scenarios/defects"
_CONSTRAINTS = "scenarios/constraints"


def test_run_review_clean_scenario_has_zero_deterministic_findings():
    report = run_review(f"{_SCENARIOS}/clean", _CONSTRAINTS)
    assert report.deterministic_findings == []


def test_run_review_detects_the_injected_defect():
    report = run_review(f"{_SCENARIOS}/dangling_reference", _CONSTRAINTS)
    classes = {f.defect_class for f in report.deterministic_findings}
    assert "dangling_reference" in classes


def test_run_review_partitions_llm_assisted_findings_separately():
    report = run_review(f"{_SCENARIOS}/clean", _CONSTRAINTS)
    assert report.llm_assisted_findings != []
    assert all(f.oracle_type == "llm-assisted" for f in report.llm_assisted_findings)
    assert all(f not in report.deterministic_findings for f in report.llm_assisted_findings)


def test_run_review_includes_economy_simulation_findings():
    report = run_review(f"{_SCENARIOS}/economy_collapse", _CONSTRAINTS, seed=0)
    assert any(f.defect_class == "economy_collapse" for f in report.simulation_findings)
