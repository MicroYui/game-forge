from gameforge.contracts.ir import EdgeType, Relation
from gameforge.spine.ir.loader import load_scenario
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.checkers.structural import StructuralChecker


def test_clean_scenario_no_findings():
    snap = load_scenario("scenarios/caravan.yaml")
    assert StructuralChecker().check(snap) == []


def test_dangling_talk_target_flagged():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    g.add_relation(Relation(id="r_bad", type=EdgeType.TALKS_TO,
                            src_id="step:talk_lincheng", dst_id="npc:ghost"))
    findings = StructuralChecker().check(Snapshot.from_graph(g))
    assert any(f.defect_class == "dangling_reference" for f in findings)
    assert all(f.oracle_type == "deterministic" and f.source == "checker" for f in findings)


def test_collect_without_source_flagged():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    # remove the GRANTS edge(s) that make item:broken_emblem obtainable
    for rid in [r.id for r in g.all_relations()
                if r.type is EdgeType.GRANTS and r.dst_id == "item:broken_emblem"]:
        g.remove_relation(rid)
    findings = StructuralChecker().check(Snapshot.from_graph(g))
    hit = [f for f in findings if f.defect_class == "missing_drop_source"]
    assert hit and hit[0].severity == "critical"
    assert hit[0].minimal_repro.get("entity") == "step:collect_emblem"


def test_cycle_flagged():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    g.add_relation(Relation(id="r_cycle", type=EdgeType.PRECEDES,
                            src_id="step:turn_in", dst_id="step:talk_lincheng"))
    findings = StructuralChecker().check(Snapshot.from_graph(g))
    assert any(f.defect_class == "cyclic_dependency" for f in findings)


def test_findings_are_wellformed():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    # remove the collect source -> produces a missing_drop_source finding
    for rid in [r.id for r in g.all_relations()
                if r.type is EdgeType.GRANTS and r.dst_id == "item:broken_emblem"]:
        g.remove_relation(rid)
    findings = StructuralChecker().check(Snapshot.from_graph(g))
    assert findings
    for f in findings:
        assert f.snapshot_id and f.producer_id == "structural"
        assert f.status == "confirmed"
