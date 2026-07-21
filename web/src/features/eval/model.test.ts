import { describe, expect, it } from "vitest";

import canonicalReport from "../../../../scenarios/bench/bench-report.json";
import type { components } from "../../api/generated/openapi";
import {
  binaryMetricView,
  distributionMetricView,
  evidenceView,
  selectBdrMetrics,
  selectCostWorkloads,
  selectFalsePositiveMetrics,
  selectKeyMetrics,
  selectQaEvidenceState,
  selectReportAgentMetrics,
  selectUniqueMetricByName,
} from "./model";

type BenchReportData = components["schemas"]["BenchReport"];
type BinaryMetric = components["schemas"]["BinaryMetric"];

function decodeCanonicalFloats(value: unknown): unknown {
  if (typeof value === "string" && value.startsWith("f:")) return Number(value.slice(2));
  if (Array.isArray(value)) return value.map(decodeCanonicalFloats);
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, decodeCanonicalFloats(item)]));
  }
  return value;
}

function measuredReport(): BenchReportData {
  return decodeCanonicalFloats(structuredClone(canonicalReport)) as BenchReportData;
}

function pendingReport(): BenchReportData {
  const pending = measuredReport();
  pending.evidence = pending.evidence.map((item) =>
    item.evidence_id === "qa" ? { ...item, available: false, sha256: null } : item,
  );
  pending.qa = {
    ...pending.qa,
    assisted_success: {
      ...pending.qa.assisted_success,
      ci_high: null,
      ci_low: null,
      ci_method: null,
      evaluated_n: 0,
      evidence_ref: null,
      k: 0,
      rate: null,
      status: "pending",
    },
    conclusion: "pending",
    evidence_ref: null,
    manual_success: {
      ...pending.qa.manual_success,
      ci_high: null,
      ci_low: null,
      ci_method: null,
      evaluated_n: 0,
      evidence_ref: null,
      k: 0,
      rate: null,
      status: "pending",
    },
    paired_saved_fraction: {
      ...pending.qa.paired_saved_fraction,
      ci_high: null,
      ci_low: null,
      ci_method: null,
      evaluated_n: 0,
      evidence_ref: null,
      mean: null,
      median: null,
      p95: null,
      primary_estimate: null,
      status: "pending",
    },
    paired_saved_minutes: {
      ...pending.qa.paired_saved_minutes,
      ci_high: null,
      ci_low: null,
      ci_method: null,
      evaluated_n: 0,
      evidence_ref: null,
      mean: null,
      median: null,
      p95: null,
      primary_estimate: null,
      status: "pending",
    },
  };
  return pending;
}

function metric(report: BenchReportData, name: string): BinaryMetric {
  const found = [...report.seeded, ...report.narrative.bdr, ...report.false_positives, ...report.agent].find(
    (item) => item.name === name,
  );
  if (!found) throw new Error(`fixture metric ${name} is missing`);
  return found;
}

describe("Eval BenchReport view model", () => {
  it("pairs all 15 BDR classes with power while preserving deterministic, simulation, and LLM partitions", () => {
    const report = measuredReport();
    const rows = selectBdrMetrics(report);

    expect(rows).toHaveLength(15);
    expect(new Set(rows.map((row) => row.defectClass))).toHaveLength(15);
    expect(rows.filter((row) => row.partition === "deterministic")).toHaveLength(10);
    expect(rows.filter((row) => row.partition === "simulation")).toHaveLength(1);
    expect(rows.filter((row) => row.partition === "llm-assisted")).toHaveLength(4);
    expect(rows.find((row) => row.defectClass === "economy_collapse")?.partition).toBe("simulation");
    expect(rows.find((row) => row.defectClass === "character_violation")?.partition).toBe("llm-assisted");

    for (const row of rows) {
      expect(row.power?.defect_class).toBe(row.defectClass);
      expect(row.metric.evaluated_n).toBeGreaterThan(0);
      expect(row.metric.ci_low).not.toBeNull();
      expect(row.metric.ci_high).not.toBeNull();
      expect(row.metric.protocol_id).toBeTruthy();
      expect(row.evidence.reference).toBe(row.metric.evidence_ref);
      expect(row.evidence.status).toBe("available");
    }
  });

  it.each([
    ["bucket", "deterministic"],
    ["evaluated_n", 83],
    ["achieved_half_width", 0.03],
    ["evidence_ref", "narrative"],
  ] as const)("does not attach power when %s differs from the BDR authority", (field, value) => {
    const report = measuredReport();
    const power = report.power.find((item) => item.defect_class === "economy_collapse")!;
    Object.assign(power, { [field]: value });

    const row = selectBdrMetrics(report).find((item) => item.defectClass === "economy_collapse");

    expect(row?.power).toBeNull();
    expect(row?.powerEvidence).toEqual({
      artifact: null,
      reference: null,
      status: "evidence_missing",
    });
  });

  it("does not attach power when BDR rows cross the seeded and narrative partitions", () => {
    const report = measuredReport();
    const seeded = report.seeded[0]!;
    const narrative = report.narrative.bdr[0]!;
    report.seeded[0] = narrative;
    report.narrative.bdr[0] = seeded;

    const rows = selectBdrMetrics(report);

    expect(rows.find((row) => row.defectClass === narrative.defect_class)?.power).toBeNull();
    expect(rows.find((row) => row.defectClass === seeded.defect_class)?.power).toBeNull();
  });

  it("finds the three headline metrics uniquely by name without dropping unknown report metrics", () => {
    const report = measuredReport();
    const unknownFalsePositive = {
      ...report.false_positives[0]!,
      bucket: "future_fp",
      name: "future_false_positive_metric",
    };
    const unknownAgent = {
      ...report.agent[0]!,
      bucket: "future_agent",
      name: "future_agent_metric",
    };
    const extended: BenchReportData = {
      ...report,
      agent: [...report.agent, unknownAgent],
      false_positives: [...report.false_positives, unknownFalsePositive],
    };

    const keys = selectKeyMetrics(extended);

    expect(keys.oracleFp?.metric.name).toBe("oracle_fp");
    expect(keys.constraintFp?.metric.name).toBe("constraint_fp");
    expect(keys.fixPassRate?.metric.name).toBe("fix_pass_rate");
    expect(selectFalsePositiveMetrics(extended).map((row) => row.metric.name)).toContain(
      "future_false_positive_metric",
    );
    expect(selectReportAgentMetrics(extended).map((row) => row.metric.name)).toContain("future_agent_metric");

    const duplicateOracle: BenchReportData = {
      ...extended,
      false_positives: [
        ...extended.false_positives,
        { ...extended.false_positives[0]!, bucket: "duplicate", evidence_ref: null },
      ],
    };
    expect(selectUniqueMetricByName(duplicateOracle.false_positives, "oracle_fp")).toBeNull();
    expect(selectKeyMetrics(duplicateOracle).oracleFp).toBeNull();
  });

  it("keeps pending and failed estimates absent rather than rendering them as zero", () => {
    const report = measuredReport();
    const source = metric(report, "oracle_fp");
    const pending: BinaryMetric = {
      ...source,
      ci_high: null,
      ci_low: null,
      ci_method: null,
      evaluated_n: 0,
      k: 0,
      rate: null,
      status: "pending",
    };
    const failed: BinaryMetric = { ...pending, status: "failed" };

    expect(binaryMetricView(report, pending)).toMatchObject({ estimate: null, interval: null });
    expect(binaryMetricView(report, failed)).toMatchObject({ estimate: null, interval: null });
    expect(binaryMetricView(report, source).estimate).toContain("0%");
    const pendingQa = pendingReport();
    expect(distributionMetricView(pendingQa, pendingQa.qa.paired_saved_minutes)).toMatchObject({
      estimate: null,
      interval: null,
    });
  });

  it("maps missing evidence, pending human evidence, and unmeasured monetary cost to named states", () => {
    const report = pendingReport();
    const qa = selectQaEvidenceState(report);

    expect(qa).toMatchObject({
      acceptanceCode: "qa.evidence_missing",
      evidence: null,
      evidenceStatus: {
        artifact: null,
        reference: null,
        status: "evidence_missing",
      },
      missingStates: ["pending_human_evidence", "evidence_missing"],
    });
    expect(qa.plannedCatalogEvidence?.evidence_id).toBe("qa");
    expect(qa.plannedCatalogEvidence?.available).toBe(false);
    expect(evidenceView(report, "seeded")).toMatchObject({
      reference: "seeded",
      status: "available",
    });
    expect(evidenceView(report, null)).toEqual({
      artifact: null,
      reference: null,
      status: "evidence_missing",
    });

    const workloads = selectCostWorkloads(report);
    expect(workloads).toHaveLength(6);
    expect(workloads.every((workload) => workload.monetaryState === "not_measured")).toBe(true);
  });

  it("never infers an exact QA binding from an available catalog entry", () => {
    const report = pendingReport();
    report.evidence = report.evidence.map((item) =>
      item.evidence_id === "qa" ? { ...item, available: true, sha256: "a".repeat(64) } : item,
    );

    expect(selectQaEvidenceState(report)).toMatchObject({
      acceptanceCode: "qa.evidence_missing",
      evidence: null,
      evidenceStatus: {
        artifact: null,
        reference: null,
        status: "evidence_missing",
      },
      missingStates: ["pending_human_evidence", "evidence_missing"],
      plannedCatalogEvidence: {
        available: true,
        evidence_id: "qa",
      },
    });
  });

  it("stops labeling the catalog entry as planned once QA and metric evidence bind exactly", () => {
    const report = measuredReport();
    report.evidence = report.evidence.map((item) =>
      item.evidence_id === "qa" ? { ...item, available: true, sha256: "a".repeat(64) } : item,
    );
    report.qa = {
      ...report.qa,
      evidence_ref: "qa",
      manual_success: { ...report.qa.manual_success, evidence_ref: "qa" },
      assisted_success: { ...report.qa.assisted_success, evidence_ref: "qa" },
      paired_saved_fraction: { ...report.qa.paired_saved_fraction, evidence_ref: "qa" },
      paired_saved_minutes: { ...report.qa.paired_saved_minutes, evidence_ref: "qa" },
    };

    expect(selectQaEvidenceState(report)).toMatchObject({
      acceptanceCode: null,
      evidenceStatus: {
        reference: "qa",
        status: "available",
      },
      plannedCatalogEvidence: null,
    });
  });

  it("keeps QA evidence missing when any measured metric lacks available evidence", () => {
    const report = measuredReport();
    report.evidence = report.evidence.map((item) =>
      item.evidence_id === "qa" ? { ...item, available: true, sha256: "a".repeat(64) } : item,
    );
    report.qa = {
      ...report.qa,
      conclusion: "savings",
      evidence_ref: "qa",
      manual_success: { ...report.qa.manual_success, evidence_ref: null },
      assisted_success: { ...report.qa.assisted_success, evidence_ref: "qa" },
      paired_saved_fraction: { ...report.qa.paired_saved_fraction, evidence_ref: "qa" },
      paired_saved_minutes: { ...report.qa.paired_saved_minutes, evidence_ref: "qa" },
    };

    expect(selectQaEvidenceState(report)).toMatchObject({
      acceptanceCode: "qa.evidence_missing",
      evidenceStatus: { status: "available" },
      missingStates: ["evidence_missing"],
    });
  });

  it("surfaces unknown transport-attempt records even when known attempts and retries are zero", () => {
    const report = measuredReport();
    const workloads = selectCostWorkloads(report);
    const repair = workloads.find((row) => row.workload.workload_id === "repair-search");

    expect(repair?.transport).toEqual({
      knownAttempts: 0,
      knownRetries: 0,
      state: "has_unknown_records",
      unknownAttemptRecords: 10,
    });
    expect(repair?.tokensPerSample.metric.evidence_ref).toBe("agent-cost");
    expect(repair?.requestLatency.metric.protocol_id).toBe("repair-search@1");
    expect(repair?.requestLatency.interval).not.toBeNull();
  });
});
