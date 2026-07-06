from gameforge.contracts.review import ReviewReport
from gameforge.contracts.findings import Finding


def _f(fid, oracle_type, status="confirmed"):
    return Finding(id=fid, source="checker", producer_id="p", producer_run_id="r",
                   oracle_type=oracle_type, defect_class="x", severity="major",
                   snapshot_id="sha256:s", status=status, message="m")


def test_report_partitions_by_oracle_and_status():
    r = ReviewReport.partition("sha256:s", [
        _f("a", "deterministic"), _f("b", "llm-assisted"),
        _f("c", "deterministic", status="unproven"), _f("d", "simulation"),
    ])
    assert [f.id for f in r.deterministic_findings] == ["a"]
    assert [f.id for f in r.llm_assisted_findings] == ["b"]
    assert [f.id for f in r.unproven_findings] == ["c"]
    assert [f.id for f in r.simulation_findings] == ["d"]
    assert r.total_deterministic() == 1
