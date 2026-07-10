"""The GameForge-Bench report (M3a Task 9 / design §7).

`BenchReport` is the JSON contract M3b (external Flare corpus → `external`) and
M3c/M4 (the Eval panel) consume. It keeps the deterministic seeded metrics, the
two false-positive rates, the bounded agent metrics, and the per-class power
rows STRICTLY separated (contract §6: deterministic vs llm-assisted are never
merged into one number). `format_text` is the minimal human view; the rich
interactive panel is deferred to M3c/M4 (interface-defined here via the JSON).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from gameforge.bench.metrics import FPReport, Metric
from gameforge.bench.power import PowerRow


class ExternalReport(BaseModel):
    """External-validity cross-check (M3b): detection on a real open-source
    game's non-injected defects. Interface-defined now, filled in M3b."""

    source: str
    n_samples: int
    detected: int
    rate: float
    ci_low: float
    ci_high: float


class BenchMeta(BaseModel):
    seed: int
    corpus_size: int
    model_snapshot: str | None = None
    generated_at: str | None = None  # stamped by the caller (deterministic core has no clock)


class BenchReport(BaseModel):
    # arbitrary_types_allowed lets the stdlib dataclasses (Metric/FPReport/
    # PowerRow) ride as pydantic fields; model_dump/validate round-trips them.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    seeded: list[Metric]
    oracle_fp: FPReport
    constraint_fp: FPReport
    agent: list[Metric]
    power: list[PowerRow]
    meta: BenchMeta
    external: ExternalReport | None = None

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


def _metric_line(m: Metric) -> str:
    label = m.defect_class or m.name
    return (f"  {label:30} rate={m.rate:6.1%}  n={m.n:<4} k={m.k:<4} "
            f"95%CI=[{m.ci_low:.2f},{m.ci_high:.2f}]")


def format_text(report: BenchReport) -> str:
    """Minimal human view — deterministic / simulation / llm-assisted / agent
    in SEPARATE sections (never one blended number), plus oracle-FP and the
    power table with under-powered classes flagged."""
    lines: list[str] = ["=== GameForge-Bench Report ==="]
    lines.append(f"corpus_size={report.meta.corpus_size} seed={report.meta.seed} "
                 f"model={report.meta.model_snapshot}")

    for bucket, title in (("deterministic", "Deterministic BDR (checker/ASP/SMT)"),
                          ("simulation", "Simulation BDR (economy)"),
                          ("llm_assisted", "LLM-assisted BDR (narrative — human-confirmed)")):
        rows = [m for m in report.seeded if m.bucket == bucket]
        if rows:
            lines.append(f"\n-- {title} --")
            lines.extend(_metric_line(m) for m in rows)

    of = report.oracle_fp
    cf = report.constraint_fp
    lines.append("\n-- False positives --")
    lines.append(f"  oracle-FP (deterministic on clean): {of.count}/{of.n} = {of.rate:.1%} "
                 f"95%CI=[{of.ci_low:.2f},{of.ci_high:.2f}]   [target 0]")
    lines.append(f"  constraint-FP (cross-class on injected): {cf.count}/{cf.n} = {cf.rate:.1%}")

    if report.agent:
        lines.append("\n-- Agent metrics (bounded REPLAY subset) --")
        lines.extend(_metric_line(m) for m in report.agent)

    lines.append("\n-- Statistical power (per class, target CI half-width ≤ 0.05) --")
    for pr in report.power:
        flag = "" if pr.target_met else "  <-- UNDER-POWERED"
        lines.append(f"  {pr.defect_class.value:30} n={pr.n:<4} "
                     f"half_width={pr.achieved_half_width:.3f} target_met={pr.target_met}{flag}")

    if report.external is not None:
        e = report.external
        lines.append(f"\n-- External validity ({e.source}) --")
        lines.append(f"  detected {e.detected}/{e.n_samples} = {e.rate:.1%} "
                     f"95%CI=[{e.ci_low:.2f},{e.ci_high:.2f}]")

    return "\n".join(lines)
