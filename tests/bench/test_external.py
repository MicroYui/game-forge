"""M3b: external-validity cross-check on real Flare content."""
from __future__ import annotations

import glob

from gameforge.bench.external import (
    build_external_report,
    clean_findings_on_real_content,
    generalizes_to_real_topology,
    load_flare_snapshot,
)
from gameforge.bench.report import ExternalReport
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.dsl import Constraint


def _constraints() -> list[Constraint]:
    cons: list[Constraint] = []
    for p in sorted(glob.glob("scenarios/constraints/*.yaml")):
        with open(p, encoding="utf-8") as fh:
            cons.extend(Constraint.from_yaml(fh.read()))
    return cons


def test_load_real_flare_content_yields_items_and_monsters_with_loot_edges():
    snap = load_flare_snapshot()
    assert len(snap.entities) >= 5           # real items + enemies
    assert len(snap.relations) >= 1          # real DROPS_FROM loot edges
    types = {e.type.value for e in snap.entities.values()}
    assert "ITEM" in types and "MONSTER" in types


def test_clean_cross_validation_surfaces_the_isolated_node_artifact():
    # The genuine, non-injected external signal: checkers over real content raise
    # isolated_node on items no enemy drops in the fragment — the seeded
    # oracle-FP=0 does NOT fully generalize (that is the whole point of M3b).
    counts = clean_findings_on_real_content(load_flare_snapshot(), _constraints())
    assert counts.get("isolated_node", 0) >= 1


def test_checker_generalizes_to_real_flare_topology():
    # Cross-domain generalization probe (injected-on-real, clearly NOT a real
    # defect): a structural defect injected into REAL Flare topology is still
    # detected — the checker isn't overfit to the synthetic Aureus base.
    snap = load_flare_snapshot()
    assert generalizes_to_real_topology(snap, _constraints(), DefectClass.dangling_reference) is True


def test_build_external_report_is_honest_and_serialises():
    r = build_external_report(_constraints())
    assert isinstance(r, ExternalReport)
    assert r.source.startswith("flare-game")
    assert r.n_real_entities >= 5
    assert r.clean_findings_by_class.get("isolated_node", 0) >= 1
    # honest about the deferred real-defect corpus, not faked
    assert r.n_defect_samples == 0
    assert "adapter-completeness artifact" in r.note
    assert "deferred" in r.note
    # round-trips inside a BenchReport
    assert ExternalReport.model_validate_json(r.model_dump_json()).source == r.source
