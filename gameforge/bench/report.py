"""The GameForge-Bench report (M3a Task 9 / design §7).

`BenchReport` is the JSON contract M3b (external corpus -> `external`) and
M3c/M4 (the Eval panel) consume. It keeps the deterministic seeded metrics, the
two false-positive rates, the bounded agent metrics, and the per-class power
rows STRICTLY separated (contract §6: deterministic vs llm-assisted are never
merged into one number). `format_text` is the minimal human view; the rich
interactive panel is deferred to M3c/M4 (interface-defined here via the JSON).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from gameforge.bench.metrics import FPReport, Metric
from gameforge.bench.power import PowerRow


class ExternalReport(BaseModel):
    """External-validity cross-check (M3b): the GameForge checkers run over REAL
    open-source game content, breaking the seeded self-circularity (a checker
    built independent of a taxonomy, validated on content nobody on this project
    authored). Carries BOTH detection on real NON-INJECTED defect samples (from
    mined bug-fix commits) AND the checker's false-positive behavior on real CLEAN
    content. The seeded oracle-FP=0 covers only the synthetic reference corpus."""

    source: str
    n_real_entities: int = 0
    # detection on real non-injected defect samples (mined pre-fix configs)
    n_defect_samples: int = 0
    detected: int = 0
    detection_rate: float = 0.0
    ci_low: float = 0.0
    ci_high: float = 1.0
    # the checkers' finding behaviour on real CLEAN content (external FP signal)
    clean_deterministic_findings: int = 0
    clean_findings_by_class: dict[str, int] = Field(default_factory=dict)
    note: str = ""


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
        lines.append(f"  real entities imported: {e.n_real_entities}")
        if e.n_defect_samples:
            lines.append(f"  detected {e.detected}/{e.n_defect_samples} real defects "
                         f"= {e.detection_rate:.1%} 95%CI=[{e.ci_low:.2f},{e.ci_high:.2f}]")
        lines.append(f"  deterministic findings on real CLEAN content: "
                     f"{e.clean_deterministic_findings} {dict(e.clean_findings_by_class)}")
        if e.note:
            lines.append(f"  note: {e.note}")

    return "\n".join(lines)
