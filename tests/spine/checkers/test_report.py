"""build_review_report (M1 Task 11): run each checker, concat with
sim_findings, partition via `ReviewReport.partition`.

This module is a thin fan-in — it doesn't decide anything itself (no defect
detection logic lives here), so the tests only anchor: (a) every checker's
findings show up in the report, (b) sim_findings are concatenated in
unmodified, (c) the strict deterministic/llm-assisted/simulation/unproven
partition (contract §6) is preserved end-to-end.
"""

from __future__ import annotations

from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import Entity, EdgeType, NodeType, Relation
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.ir.snapshot import Snapshot


def _snap(entities, relations=()):
    return Snapshot.from_entities_relations(list(entities), list(relations))


def _dangling_snapshot():
    ents = [Entity(id="q", type=NodeType.QUEST)]
    rels = [Relation(id="r1", type=EdgeType.HAS_STEP, src_id="q", dst_id="missing")]
    return _snap(ents, rels)


def _sim_finding(snapshot_id: str) -> Finding:
    return Finding(
        id="sim#0", source="sim", producer_id="economy_sim", producer_run_id="sim@x",
        oracle_type="simulation", defect_class="economy_collapse", severity="critical",
        snapshot_id=snapshot_id, status="confirmed", message="collapse",
    )


def test_build_review_report_runs_checkers_and_partitions_findings():
    snap = _dangling_snapshot()
    report = build_review_report(snap, [GraphChecker()])
    assert report.snapshot_id == snap.snapshot_id
    assert any(f.defect_class == "dangling_reference" for f in report.deterministic_findings)
    assert report.llm_assisted_findings == []
    assert report.simulation_findings == []
    assert report.unproven_findings == []


def test_build_review_report_concatenates_sim_findings():
    # a single ITEM entity (not one of GraphChecker's quest-lifecycle checks'
    # entry points, and not a "key node type" with zero relations triggering
    # isolated_node -- wait, ITEM *is* a key node type, so use CURRENCY, which
    # GraphChecker never inspects at all) keeps GraphChecker silent, isolating
    # the sim_findings concatenation from any deterministic checker noise.
    snap = _snap([Entity(id="gold", type=NodeType.CURRENCY)])
    sim_finding = _sim_finding(snap.snapshot_id)
    report = build_review_report(snap, [GraphChecker()], sim_findings=[sim_finding])
    assert report.simulation_findings == [sim_finding]
    assert report.deterministic_findings == []


def test_build_review_report_with_no_checkers_and_no_sim_findings_is_empty():
    snap = _snap([Entity(id="q", type=NodeType.QUEST)])
    report = build_review_report(snap, [])
    assert report.deterministic_findings == []
    assert report.llm_assisted_findings == []
    assert report.simulation_findings == []
    assert report.unproven_findings == []
    assert report.by_defect_class == []
