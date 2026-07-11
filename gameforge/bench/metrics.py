"""The GameForge-Bench metrics engine (M3a Task 7 / design §5).

Runs the M1 review pipeline (checkers + economy sim + nav) over each seeded
sample and scores it against the injector's `GroundTruth`: a sample is
"detected" iff the `ReviewReport` — in the sample's own bucket partition
(deterministic / simulation / llm-assisted, strictly separated per contract
§6) — carries a Finding of the matching `defect_class` that touches an injected
entity. Reports per-class Bug-Detection-Rate + oracle-FP (deterministic
findings on the clean base, target 0) + constraint-FP (cross-class false
detections on injected samples), all with Wilson CIs.

`bench` is the deterministic trunk: this module imports checkers/sim/nav but
NEVER `gameforge.agents` or an LLM SDK. The 4 narrative (llm-assisted) classes
are NOT scored here — their judgment is the M2 Consistency quorum, aggregated
separately in `bench/agent_metrics.py` (REPLAY, bounded).
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass

from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.bench.corpus import Corpus
from gameforge.bench.inject import GroundTruth
from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass
from gameforge.contracts.dsl import Constraint
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.spine.checkers.report import ReviewReport, build_review_report
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings
from gameforge.spine.stats import wilson_ci

_CONSTRAINTS_DIR = "scenarios/constraints"
_TAXONOMY_CLASSES = {dc.value for dc in DefectClass}


@dataclass
class Metric:
    name: str
    defect_class: str | None
    n: int
    k: int
    rate: float
    ci_low: float
    ci_high: float
    bucket: str


@dataclass
class FPReport:
    n: int
    count: int
    rate: float
    ci_low: float
    ci_high: float


@dataclass
class SeededScore:
    bdr: list[Metric]
    oracle_fp: FPReport
    constraint_fp: FPReport


def default_constraints(path: str = _CONSTRAINTS_DIR) -> list[Constraint]:
    """Load the project's constraint library (`scenarios/constraints/*.yaml`) —
    the same set the M1 review CLI compiles. Independent of the injector
    taxonomy (anti-circularity): the checker never sees a `GroundTruth`."""
    cons: list[Constraint] = []
    for p in sorted(glob.glob(os.path.join(path, "*.yaml"))):
        with open(p, encoding="utf-8") as fh:
            cons.extend(Constraint.from_yaml(fh.read()))
    return cons


def _run_pipeline(snapshot: Snapshot, checkers, needs_nav: bool) -> ReviewReport:
    """The M1 review pipeline for one snapshot: economy sim findings + (only
    when the sample needs it) a nav provider from the built Aureus world."""
    model = EconomyModel.from_snapshot(snapshot)
    sim = EconomySimulator().run(model, seed=0, n_agents=50, n_ticks=200)
    sim_findings = to_findings(sim, snapshot.snapshot_id, model=model)
    nav = AureusEnv(snapshot_to_world(snapshot)).nav_provider() if needs_nav else None
    return build_review_report(snapshot, checkers, sim_findings=sim_findings, nav=nav)


def _bucket_findings(report: ReviewReport, bucket: Bucket):
    if bucket is Bucket.deterministic:
        return report.deterministic_findings
    if bucket is Bucket.simulation:
        return report.simulation_findings
    return report.llm_assisted_findings


def detects(report: ReviewReport, gt: GroundTruth) -> bool:
    """True iff `report`, in `gt`'s bucket partition, has a Finding of the
    matching `defect_class` touching an injected entity (class match alone is
    not enough — the Finding must implicate the entity that was mutated)."""
    gt_ents = set(gt.injected_entities)
    for f in _bucket_findings(report, CLASS_META[gt.defect_class].bucket):
        if f.defect_class == gt.defect_class.value and (set(f.entities) & gt_ents):
            return True
    return False


def score_seeded(corpus: Corpus, constraints: list[Constraint]) -> SeededScore:
    """Score every deterministic/simulation sample for BDR, and compute the two
    false-positive rates. llm-assisted (narrative) classes are skipped (scored
    via `agent_metrics`). Runs the full checker/sim pipeline per sample — call
    with a bounded corpus in tests; the full ≥500 run lives in `run_bench`."""
    checkers = compile_all(constraints)
    per_class: dict[DefectClass, list[int]] = {}
    cross_fp_count = 0
    cross_fp_n = 0

    for sample in corpus.samples:
        dc = sample.ground_truth.defect_class
        if CLASS_META[dc].bucket is Bucket.llm_assisted:
            continue
        report = _run_pipeline(sample.snapshot, checkers, sample.needs_nav)
        agg = per_class.setdefault(dc, [0, 0])
        agg[0] += int(detects(report, sample.ground_truth))
        agg[1] += 1

        # constraint-FP: a deterministic taxonomy-class Finding of a DIFFERENT
        # class than the injected one, not touching the injected entity, is a
        # false positive (the checker flagged something that was not injected).
        gt_ents = set(sample.ground_truth.injected_entities)
        cross_fp_n += 1
        for f in report.deterministic_findings:
            if (
                f.defect_class in _TAXONOMY_CLASSES
                and f.defect_class != dc.value
                and not (set(f.entities) & gt_ents)
            ):
                cross_fp_count += 1
                break

    bdr: list[Metric] = []
    for dc, (k, n) in per_class.items():
        low, high = wilson_ci(k, n)
        bdr.append(Metric(
            name="bdr", defect_class=dc.value, n=n, k=k,
            rate=(k / n if n else 0.0), ci_low=low, ci_high=high,
            bucket=CLASS_META[dc].bucket.value,
        ))

    # oracle-FP: any deterministic OR unproven Finding on a clean snapshot is a
    # checker-algorithm false positive (the headline KPI — must be 0). Dedupe
    # the clean list by snapshot_id first: `build_corpus` repeats ONE clean
    # config, so scoring it N times would report a fake-tight CI over N
    # "independent" samples. We report over DISTINCT clean configs (honest n);
    # a tighter CI needs more distinct clean bases (design §3 "多 base"; M3b's
    # External corpora add real ones).
    distinct_clean = {c.snapshot_id: c for c in corpus.clean}
    ofp = 0
    for clean in distinct_clean.values():
        report = _run_pipeline(clean, checkers, needs_nav=False)
        if report.deterministic_findings or report.unproven_findings:
            ofp += 1
    n_clean = len(distinct_clean)
    olow, ohigh = wilson_ci(ofp, n_clean)
    oracle_fp = FPReport(n=n_clean, count=ofp,
                         rate=(ofp / n_clean if n_clean else 0.0), ci_low=olow, ci_high=ohigh)

    clow, chigh = wilson_ci(cross_fp_count, cross_fp_n)
    constraint_fp = FPReport(n=cross_fp_n, count=cross_fp_count,
                             rate=(cross_fp_count / cross_fp_n if cross_fp_n else 0.0),
                             ci_low=clow, ci_high=chigh)

    return SeededScore(bdr=bdr, oracle_fp=oracle_fp, constraint_fp=constraint_fp)
