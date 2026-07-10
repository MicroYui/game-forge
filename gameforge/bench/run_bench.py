"""GameForge-Bench entrypoint (M3a Task 10 / design §5+§7).

Assembles the seeded corpus, scores it, aggregates the bounded agent metrics,
computes the power table, and emits a `BenchReport`. `main()` prints the text
view; `--json` emits the JSON contract M3b/M3c consume.

The 11 deterministic/simulation classes get real per-class BDR from the
checker/sim sweep. The 4 narrative (llm-assisted) classes are scored by the M2
Consistency quorum, which is a separate cassette-bound record — until that
record they are reported at n=0 (PENDING), NOT as a fake 0/N detected: honest
about what has and has not been measured.
"""
from __future__ import annotations

import argparse
import sys

from gameforge.bench.agent_metrics import aggregate_agent_metrics
from gameforge.bench.corpus import build_corpus
from gameforge.bench.metrics import Metric, default_constraints, score_seeded
from gameforge.bench.power import PowerRow, achieved_half_width
from gameforge.bench.report import BenchMeta, BenchReport, format_text
from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass
from gameforge.spine.stats import wilson_ci


def _narrative_pending() -> list[Metric]:
    """One PENDING llm-assisted metric per narrative class (n=0), so the report
    lists all 15 classes without faking a narrative BDR the deterministic sweep
    cannot produce."""
    low, high = wilson_ci(0, 0)
    return [
        Metric("bdr", dc.value, 0, 0, 0.0, low, high, "llm_assisted")
        for dc in DefectClass
        if CLASS_META[dc].bucket is Bucket.llm_assisted
    ]


def _power_rows(bdr: list[Metric], per_class_n: dict[DefectClass, int]) -> list[PowerRow]:
    by_class = {m.defect_class: m for m in bdr}
    rows: list[PowerRow] = []
    for dc in DefectClass:
        m = by_class.get(dc.value)
        if m is not None:
            n, k = m.n, m.k
        else:  # narrative: sample size exists but BDR pending → conservative p=0.5
            n = per_class_n.get(dc, 0)
            k = n // 2
        hw = achieved_half_width(k, n) if n else 1.0
        rows.append(PowerRow(dc, n, hw, hw <= 0.05))
    return rows


def build_bench_report(
    seed: int = 0,
    with_agent: bool = True,
    per_class_n: dict[DefectClass, int] | None = None,
    n_clean: int = 40,
    constraints=None,
    model_snapshot: str | None = None,
) -> BenchReport:
    corpus = build_corpus(seed=seed, per_class_n=per_class_n, n_clean=n_clean)
    constraints = constraints if constraints is not None else default_constraints()
    score = score_seeded(corpus, constraints)
    seeded = score.bdr + _narrative_pending()
    agent = aggregate_agent_metrics() if with_agent else []
    power = _power_rows(score.bdr, corpus.per_class_n)
    meta = BenchMeta(seed=seed, corpus_size=len(corpus.samples), model_snapshot=model_snapshot)
    return BenchReport(
        seeded=seeded, oracle_fp=score.oracle_fp, constraint_fp=score.constraint_fp,
        agent=agent, power=power, meta=meta,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run GameForge-Bench (seeded).")
    parser.add_argument("--json", action="store_true", help="emit the JSON BenchReport")
    parser.add_argument("--no-agent", action="store_true", help="skip agent metrics (faster)")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    report = build_bench_report(seed=args.seed, with_agent=not args.no_agent)
    print(report.to_json() if args.json else format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
