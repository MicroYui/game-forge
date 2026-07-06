"""ReviewReport schema (contract §6 anchor) — strict partition of Findings.

Deterministic / llm-assisted / simulation / unproven Findings are counted in
separate buckets by construction (never mixed into one aggregate number) so
downstream consumers (CLI, eval, agent triage in M2) cannot accidentally treat
an unproven or llm-assisted Finding as a proven deterministic defect.
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, Field

from gameforge.contracts.findings import Finding, Severity
from gameforge.contracts.versions import REVIEW_SCHEMA_VERSION


class DefectClassCount(BaseModel):
    defect_class: str
    severity: Severity
    count: int


class ReviewReport(BaseModel):
    review_schema_version: str = REVIEW_SCHEMA_VERSION
    snapshot_id: str
    deterministic_findings: list[Finding] = Field(default_factory=list)
    llm_assisted_findings: list[Finding] = Field(default_factory=list)
    simulation_findings: list[Finding] = Field(default_factory=list)
    unproven_findings: list[Finding] = Field(default_factory=list)
    by_defect_class: list[DefectClassCount] = Field(default_factory=list)
    created_at: str | None = None

    def total_deterministic(self) -> int:
        return len(self.deterministic_findings)

    @classmethod
    def partition(cls, snapshot_id: str, findings: list[Finding]) -> "ReviewReport":
        deterministic: list[Finding] = []
        llm_assisted: list[Finding] = []
        simulation: list[Finding] = []
        unproven: list[Finding] = []

        for f in findings:
            if f.oracle_type == "llm-assisted":
                llm_assisted.append(f)
            elif f.status == "unproven":
                unproven.append(f)
            elif f.oracle_type == "simulation":
                simulation.append(f)
            else:
                deterministic.append(f)

        counts: Counter[tuple[str, Severity]] = Counter(
            (f.defect_class, f.severity) for f in findings
        )
        by_defect_class = [
            DefectClassCount(defect_class=defect_class, severity=severity, count=count)
            for (defect_class, severity), count in sorted(counts.items())
        ]

        return cls(
            snapshot_id=snapshot_id,
            deterministic_findings=deterministic,
            llm_assisted_findings=llm_assisted,
            simulation_findings=simulation,
            unproven_findings=unproven,
            by_defect_class=by_defect_class,
        )
