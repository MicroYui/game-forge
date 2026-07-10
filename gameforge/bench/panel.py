"""Minimal static HTML view of a BenchReport (M3c / design §7).

The Eval "panel" for M3c is deliberately minimal: a single self-contained HTML
file (inline CSS, NO JavaScript, NO chart libraries) rendering the BenchReport's
tables — per-class BDR with CIs (grouped by bucket, deterministic/simulation/
llm-assisted kept visually separate), the two false-positive rates, the bounded
agent metrics, the power table (under-powered classes flagged), and the external
Flare cross-validation. The rich interactive React dashboard is deferred to M4
(`前端全页面`); this proves the JSON contract renders and gives a shareable
artifact now.
"""
from __future__ import annotations

import html

from gameforge.bench.report import BenchReport

_CSS = """
body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:2rem;color:#1a1a1a;background:#fafafa}
h1{font-size:1.4rem} h2{font-size:1.05rem;margin-top:1.6rem;border-bottom:1px solid #ddd;padding-bottom:.3rem}
table{border-collapse:collapse;margin:.4rem 0;width:100%;max-width:820px}
th,td{text-align:left;padding:.3rem .6rem;border-bottom:1px solid #eee}
th{background:#f0f0f0;font-weight:600}
.num{text-align:right;font-variant-numeric:tabular-nums}
.warn{color:#b00020;font-weight:600}
.ok{color:#0a7d2c;font-weight:600}
.muted{color:#777}
.bucket-deterministic{border-left:3px solid #2b6cb0}
.bucket-simulation{border-left:3px solid #6b46c1}
.bucket-llm_assisted{border-left:3px solid #b7791f}
"""


def _esc(x: object) -> str:
    return html.escape(str(x))


def _metric_rows(metrics) -> str:
    out = []
    for m in metrics:
        label = m.defect_class or m.name
        pending = " <span class='muted'>(pending)</span>" if m.n == 0 else ""
        out.append(
            f"<tr class='bucket-{_esc(m.bucket)}'><td>{_esc(label)}{pending}</td>"
            f"<td class='num'>{m.rate:.1%}</td><td class='num'>{m.k}/{m.n}</td>"
            f"<td class='num'>[{m.ci_low:.2f}, {m.ci_high:.2f}]</td>"
            f"<td>{_esc(m.bucket)}</td></tr>"
        )
    return "".join(out)


def render_html(report: BenchReport) -> str:
    r = report
    parts: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>GameForge-Bench</title><style>", _CSS, "</style></head><body>",
        "<h1>GameForge-Bench Report</h1>",
        f"<p class='muted'>corpus_size={r.meta.corpus_size} &middot; seed={r.meta.seed} "
        f"&middot; model={_esc(r.meta.model_snapshot)}</p>",
    ]

    for bucket, title in (("deterministic", "Deterministic BDR (Graph/ASP/SMT)"),
                          ("simulation", "Simulation BDR (economy)"),
                          ("llm_assisted", "LLM-assisted BDR (narrative — human-confirmed)")):
        rows = [m for m in r.seeded if m.bucket == bucket]
        if rows:
            parts.append(f"<h2>{_esc(title)}</h2><table><tr><th>class</th><th>BDR</th>"
                         f"<th>k/n</th><th>95% CI</th><th>bucket</th></tr>"
                         f"{_metric_rows(rows)}</table>")

    of, cf = r.oracle_fp, r.constraint_fp
    fp_cls = "ok" if of.count == 0 else "warn"
    parts.append(
        "<h2>False positives</h2><table>"
        f"<tr><td>oracle-FP (deterministic on clean, target 0)</td>"
        f"<td class='num {fp_cls}'>{of.count}/{of.n} = {of.rate:.1%}</td>"
        f"<td class='num'>[{of.ci_low:.2f}, {of.ci_high:.2f}]</td></tr>"
        f"<tr><td>constraint-FP (cross-class on injected)</td>"
        f"<td class='num'>{cf.count}/{cf.n} = {cf.rate:.1%}</td><td></td></tr></table>"
    )

    if r.agent:
        parts.append("<h2>Agent metrics (bounded REPLAY subset)</h2><table>"
                     "<tr><th>metric</th><th>rate</th><th>k/n</th><th>95% CI</th><th></th></tr>"
                     f"{_metric_rows(r.agent)}</table>")

    parts.append("<h2>Statistical power (target CI half-width &le; 0.05)</h2>"
                 "<table><tr><th>class</th><th>n</th><th>achieved half-width</th><th>met?</th></tr>")
    for pr in r.power:
        cls = "ok" if pr.target_met else "warn"
        flag = "yes" if pr.target_met else "UNDER-POWERED"
        parts.append(f"<tr><td>{_esc(pr.defect_class.value)}</td><td class='num'>{pr.n}</td>"
                     f"<td class='num'>{pr.achieved_half_width:.3f}</td>"
                     f"<td class='{cls}'>{flag}</td></tr>")
    parts.append("</table>")

    if r.external is not None:
        e = r.external
        parts.append(f"<h2>External validity ({_esc(e.source)})</h2>"
                     f"<p>real entities imported: {e.n_real_entities}<br>"
                     f"deterministic findings on real clean content: "
                     f"{e.clean_deterministic_findings} {_esc(dict(e.clean_findings_by_class))}</p>"
                     f"<p class='muted'>{_esc(e.note)}</p>")

    parts.append("</body></html>")
    return "".join(parts)
