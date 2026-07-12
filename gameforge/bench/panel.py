"""Self-contained static HTML renderer for the BenchReport v2 projection."""

from __future__ import annotations

import html

from gameforge.bench.report import SECTION_TITLES, report_projection
from gameforge.bench.report_contracts import BenchReport

_CSS = """
:root{color-scheme:light;--ink:#17201d;--muted:#5d6863;--line:#d8dfdb;--paper:#fff;--wash:#f4f7f5;--accent:#147d6f;--warn:#a64b16}
*{box-sizing:border-box;letter-spacing:0}
body{margin:0;background:var(--wash);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1440px;margin:0 auto;padding:24px}
h1{font-size:24px;line-height:1.2;margin:0 0 20px}
h2{font-size:16px;line-height:1.3;margin:28px 0 8px}
.table-wrap{overflow-x:auto;background:var(--paper);border:1px solid var(--line)}
table{width:100%;min-width:980px;border-collapse:collapse;table-layout:fixed}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{background:#e9efec;color:#39433f;font-size:12px;font-weight:650}
tr:last-child td{border-bottom:0}
th:nth-child(1),td:nth-child(1){width:24%}th:nth-child(2),td:nth-child(2){width:18%}
th:nth-child(3),td:nth-child(3){width:11%}th:nth-child(4),td:nth-child(4){width:20%}
th:nth-child(5),td:nth-child(5){width:14%}th:nth-child(6),td:nth-child(6){width:13%}
.row-id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}
.status{font-weight:650;color:var(--accent)}
.status-pending,.status-failed,.status-underpowered,.status-unavailable{color:var(--warn)}
.evidence{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}
@media(max-width:720px){main{padding:16px}h1{font-size:21px}}
"""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_html(report: BenchReport) -> str:
    rows = report_projection(report)
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>GameForge-Bench Report v2</title>",
        f"<style>{_CSS}</style></head><body><main>",
        "<h1>GameForge-Bench Report v2</h1>",
    ]
    active_section: str | None = None
    for row in rows:
        if row.section != active_section:
            if active_section is not None:
                parts.append("</tbody></table></div>")
            active_section = row.section
            parts.extend(
                (
                    f"<h2>{_escape(SECTION_TITLES[row.section])}</h2>",
                    '<div class="table-wrap"><table><thead><tr>',
                    "<th>Row</th><th>Metric</th><th>Status</th><th>Value</th>",
                    "<th>Denominator / interval</th><th>Evidence</th>",
                    "</tr></thead><tbody>",
                )
            )
        status_class = f"status status-{_escape(row.status)}"
        context = "<br>".join(
            item for item in (_escape(row.denominator), _escape(row.interval)) if item
        )
        parts.append(
            f'<tr data-row-id="{_escape(row.row_id)}">'
            f'<td class="row-id">{_escape(row.row_id)}</td>'
            f"<td>{_escape(row.label)}</td>"
            f'<td class="{status_class}">{_escape(row.status)}</td>'
            f"<td>{_escape(row.value)}</td>"
            f"<td>{context}</td>"
            f'<td class="evidence">{_escape(row.evidence_ref or "")}</td></tr>'
        )
    if active_section is not None:
        parts.append("</tbody></table></div>")
    parts.append("</main></body></html>\n")
    return "".join(parts)


__all__ = ["render_html"]
