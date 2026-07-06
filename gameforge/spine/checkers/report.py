"""build_review_report (M1 Task 11): Checker fan-in -> ReviewReport (contract §6).

This is a thin composition point, not a decision-maker: it runs each supplied
deterministic `Checker` (Graph/ASP/SMT/CompiledChecker/LlmRoutedChecker — any
object satisfying `spine.checkers.base.Checker`) against `snapshot`, appends
`sim_findings` (already-built `Finding`s from `spine.sim.economy.to_findings`,
`oracle_type="simulation"`) verbatim, and hands the concatenation to
`ReviewReport.partition` for the strict deterministic/llm-assisted/simulation/
unproven split. No defect-detection logic lives here.
"""

from __future__ import annotations

from gameforge.contracts.findings import Finding
from gameforge.contracts.review import ReviewReport
from gameforge.spine.checkers.base import Checker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider


def build_review_report(
    snapshot: Snapshot,
    checkers: list[Checker],
    sim_findings: tuple[Finding, ...] = (),
    nav: NavProvider | None = None,
) -> ReviewReport:
    findings: list[Finding] = []
    for checker in checkers:
        findings.extend(checker.check(snapshot, nav=nav))
    findings.extend(sim_findings)
    return ReviewReport.partition(snapshot.snapshot_id, findings)
