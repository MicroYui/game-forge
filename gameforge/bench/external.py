"""External-validity cross-check for GameForge-Bench (M3b / design §8, PRD §13.3).

Runs the GameForge deterministic checkers over REAL open-source game content —
Flare (flareteam/flare-game), via the M1-proven-lossless `flare_adapter` — so
the checker's real-world behaviour is reported on content nobody on this
project authored. This is the anti-circularity break (R2): the seeded corpus is
built from our own clean Aureus base, so its oracle-FP=0 could be self-serving;
real content is the independent witness.

Two independent signals:
  1. CLEAN cross-validation (genuine, non-injected): run the checkers over the
     real Flare content unchanged and report what they flag. On the vendored
     sample this surfaces `isolated_node` findings on items that no enemy drops
     in the fragment — an ADAPTER-COMPLETENESS artifact (the referencing
     loot-table / shop / quest edges live in Flare files the round-trip adapter
     does not import), i.e. the seeded oracle-FP=0 does NOT fully generalize.
  2. Cross-domain generalization (clearly labelled injected-on-real): apply a
     structural injector to the REAL Flare IR topology and confirm the checker
     still detects it — proving the checker generalizes beyond the synthetic
     Aureus base's shape. This is NOT a real defect; it is a generalization
     probe, reported separately from (1).

A rich corpus of REAL non-injected defects (mined Flare bug-fix commits) needs a
fuller Flare adapter (quests / loot-tables / campaign) + Flare-specific numeric
constraints than M1's items+enemies round-trip provides; that is interface-
defined here (`ExternalReport.n_defect_samples`/`detected`) and deferred, not
faked.

Deterministic, zero-LLM: `bench.external` imports only `contracts`/`spine`/
stdlib (checked by the seeded-core AST guard — external.py is seeded core).
"""
from __future__ import annotations

from gameforge.bench.inject import inject
from gameforge.bench.metrics import detects
from gameforge.bench.report import ExternalReport
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.dsl import Constraint
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ingestion.flare_adapter import FlareTxtAdapter, read_flare_dir
from gameforge.spine.ir.snapshot import Snapshot

_FLARE_SOURCE = "flare-game (flareteam/flare-game)"
_FLARE_SAMPLE_DIR = "scenarios/flare_sample"
# `isolated_node` is completeness-sensitive: on a fragment (items+enemies with
# no imported quest/loot-table/shop cross-refs) legitimately-referenced items
# read as isolated. Reported, but flagged as an adapter-completeness artifact
# rather than a checker-soundness FP.
_COMPLETENESS_SENSITIVE = {"isolated_node"}


def load_flare_snapshot(directory: str = _FLARE_SAMPLE_DIR) -> Snapshot:
    """Real Flare content → GameForge IR (via the M1 lossless adapter)."""
    return FlareTxtAdapter().to_ir(read_flare_dir(directory), file_ref=directory)


def clean_findings_on_real_content(
    snapshot: Snapshot, constraints: list[Constraint]
) -> dict[str, int]:
    """Deterministic findings the checkers raise on the REAL (unchanged) content,
    counted per defect_class. This is the genuine, non-injected external signal."""
    report = build_review_report(snapshot, compile_all(constraints))
    counts: dict[str, int] = {}
    for f in report.deterministic_findings:
        counts[f.defect_class] = counts.get(f.defect_class, 0) + 1
    return counts


def generalizes_to_real_topology(
    snapshot: Snapshot, constraints: list[Constraint], defect: DefectClass, seed: int = 1
) -> bool:
    """Cross-domain generalization probe (injected-on-real, NOT a real defect):
    inject `defect` into the REAL Flare IR and confirm the checker detects it on
    real content topology, not just the synthetic Aureus base. Returns False if
    the injector cannot apply to this content's structure (e.g. a quest-step
    defect on item/enemy-only content) — the probe is "not applicable", not a
    detection failure."""
    try:
        sample = inject(snapshot, defect, seed)
    except ValueError:
        return False
    report = build_review_report(sample.snapshot, compile_all(constraints))
    return detects(report, sample.ground_truth)


# Only classes injectable into item/enemy topology (Flare content has no quest
# steps) — dangling_reference works via the real DROPS_FROM loot edges.
_GENERALIZATION_PROBES = (DefectClass.dangling_reference,)


def build_external_report(
    constraints: list[Constraint], directory: str = _FLARE_SAMPLE_DIR
) -> ExternalReport:
    """Assemble the M3b external-validity `ExternalReport` from real Flare
    content: the genuine clean-content findings + a labelled cross-domain
    generalization probe + an honest note on what is measured vs deferred."""
    snapshot = load_flare_snapshot(directory)
    clean = clean_findings_on_real_content(snapshot, constraints)
    n_clean_det = sum(clean.values())
    gen = {
        dc.value: generalizes_to_real_topology(snapshot, constraints, dc)
        for dc in _GENERALIZATION_PROBES
    }
    n_artifact = sum(v for k, v in clean.items() if k in _COMPLETENESS_SENSITIVE)
    note = (
        f"clean findings on real content = {clean}; {n_artifact} are isolated_node "
        f"(adapter-completeness artifact — items referenced by loot-tables/shops/quests "
        f"the M1 round-trip adapter does not import, so the seeded oracle-FP=0 does NOT "
        f"fully generalize). Cross-domain generalization (injected-on-real, NOT real "
        f"defects): {gen}. Real non-injected defect corpus (mined Flare bug-fix commits) "
        f"deferred — needs a fuller Flare adapter (quests/loot-tables/campaign) + "
        f"Flare-specific numeric constraints; interface-defined here, not faked."
    )
    return ExternalReport(
        source=_FLARE_SOURCE,
        n_real_entities=len(snapshot.entities),
        n_defect_samples=0, detected=0, detection_rate=0.0, ci_low=0.0, ci_high=1.0,
        clean_deterministic_findings=n_clean_det,
        clean_findings_by_class=clean,
        note=note,
    )
