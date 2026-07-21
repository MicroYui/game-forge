import type { components } from "../../api/generated/openapi";

export type BenchReportData = components["schemas"]["BenchReport"];
export type BinaryMetric = components["schemas"]["BinaryMetric"];
export type DistributionMetric = components["schemas"]["DistributionMetric"];
export type EvidenceArtifactRef = components["schemas"]["EvidenceArtifactRef"];
export type PowerMetric = components["schemas"]["PowerMetric"];
export type AgentCostWorkload = components["schemas"]["AgentCostWorkload"];

export type BdrPartition = "deterministic" | "simulation" | "llm-assisted";
export type NamedMissingState = "evidence_missing" | "not_measured" | "pending_human_evidence";

export interface EvidenceView {
  artifact: EvidenceArtifactRef | null;
  reference: string | null;
  status: "available" | "evidence_missing";
}

export interface BinaryMetricView {
  estimate: string | null;
  evaluatedN: number;
  evidence: EvidenceView;
  interval: string | null;
  metric: BinaryMetric;
  plannedN: number;
  protocolId: string | null;
}

export interface DistributionMetricView {
  estimate: string | null;
  evaluatedN: number;
  evidence: EvidenceView;
  interval: string | null;
  metric: DistributionMetric;
  plannedN: number;
  protocolId: string | null;
}

export interface BdrMetricView extends BinaryMetricView {
  defectClass: BinaryMetric["defect_class"];
  partition: BdrPartition;
  power: PowerMetric | null;
  powerEvidence: EvidenceView;
}

export interface KeyMetricViews {
  constraintFp: BinaryMetricView | null;
  fixPassRate: BinaryMetricView | null;
  oracleFp: BinaryMetricView | null;
}

export interface QaEvidenceState {
  acceptanceCode: "qa.evidence_missing" | null;
  evidence: EvidenceArtifactRef | null;
  evidenceStatus: EvidenceView;
  missingStates: NamedMissingState[];
  plannedCatalogEvidence: EvidenceArtifactRef | null;
}

export interface CostTransportView {
  knownAttempts: number;
  knownRetries: number;
  state: "complete" | "has_unknown_records";
  unknownAttemptRecords: number;
}

export interface CostWorkloadView {
  evidence: EvidenceView;
  monetaryState: "not_measured";
  requestLatency: DistributionMetricView;
  tokensPerSample: DistributionMetricView;
  transport: CostTransportView;
  workload: AgentCostWorkload;
}

const percentFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 1,
  style: "percent",
});

const numberFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 3,
});

function uniqueEvidence(report: BenchReportData, reference: string): EvidenceArtifactRef | null {
  const matches = report.evidence.filter((item) => item.evidence_id === reference);
  return matches.length === 1 ? matches[0]! : null;
}

export function evidenceView(report: BenchReportData, reference: string | null | undefined): EvidenceView {
  if (!reference) return { artifact: null, reference: null, status: "evidence_missing" };
  const artifact = uniqueEvidence(report, reference);
  return {
    artifact,
    reference,
    status: artifact?.available === true ? "available" : "evidence_missing",
  };
}

function metricInterval(
  ciLow: number | null | undefined,
  ciHigh: number | null | undefined,
  ciMethod: string | null | undefined,
): string | null {
  if (ciLow === null || ciLow === undefined || ciHigh === null || ciHigh === undefined || !ciMethod) {
    return null;
  }
  return `${ciMethod} [${numberFormatter.format(ciLow)}, ${numberFormatter.format(ciHigh)}]`;
}

export function binaryMetricView(report: BenchReportData, metric: BinaryMetric): BinaryMetricView {
  const rate = metric.rate;
  return {
    estimate:
      rate === null || rate === undefined
        ? null
        : `${metric.k}/${metric.evaluated_n} (${percentFormatter.format(rate)})`,
    evaluatedN: metric.evaluated_n,
    evidence: evidenceView(report, metric.evidence_ref),
    interval: metricInterval(metric.ci_low, metric.ci_high, metric.ci_method),
    metric,
    plannedN: metric.planned_n,
    protocolId: metric.protocol_id ?? null,
  };
}

export function distributionMetricView(
  report: BenchReportData,
  metric: DistributionMetric,
): DistributionMetricView {
  const estimate =
    metric.mean === null || metric.mean === undefined
      ? null
      : [
          `mean ${numberFormatter.format(metric.mean)}`,
          `median ${numberFormatter.format(metric.median!)}`,
          `p95 ${numberFormatter.format(metric.p95!)}`,
          metric.unit,
        ].join(" · ");
  return {
    estimate,
    evaluatedN: metric.evaluated_n,
    evidence: evidenceView(report, metric.evidence_ref),
    interval: metricInterval(metric.ci_low, metric.ci_high, metric.ci_method),
    metric,
    plannedN: metric.planned_n,
    protocolId: metric.protocol_id ?? null,
  };
}

function bdrView(
  report: BenchReportData,
  metric: BinaryMetric,
  partition: BdrPartition,
  powerByClass: ReadonlyMap<NonNullable<BinaryMetric["defect_class"]>, PowerMetric>,
): BdrMetricView {
  const base = binaryMetricView(report, metric);
  const defectClass = metric.defect_class;
  const candidate = defectClass ? (powerByClass.get(defectClass) ?? null) : null;
  const achievedHalfWidth =
    metric.ci_low === null ||
    metric.ci_low === undefined ||
    metric.ci_high === null ||
    metric.ci_high === undefined
      ? null
      : (metric.ci_high - metric.ci_low) / 2;
  const partitionBucket = partition === "llm-assisted" ? "llm_assisted" : partition;
  const power =
    candidate !== null &&
    metric.bucket === partitionBucket &&
    metric.ci_method === "wilson95" &&
    achievedHalfWidth !== null &&
    candidate.bucket === metric.bucket &&
    candidate.evaluated_n === metric.evaluated_n &&
    Math.abs(candidate.achieved_half_width - achievedHalfWidth) <= 1e-12 &&
    (candidate.evidence_ref ?? null) === (metric.evidence_ref ?? null)
      ? candidate
      : null;
  return {
    ...base,
    defectClass,
    partition,
    power,
    powerEvidence: evidenceView(report, power?.evidence_ref),
  };
}

export function selectBdrMetrics(report: BenchReportData): BdrMetricView[] {
  const powerByClass = new Map(report.power.map((metric) => [metric.defect_class, metric]));
  return [
    ...report.seeded.map((metric) =>
      bdrView(report, metric, metric.bucket === "simulation" ? "simulation" : "deterministic", powerByClass),
    ),
    ...report.narrative.bdr.map((metric) => bdrView(report, metric, "llm-assisted", powerByClass)),
  ];
}

export function selectUniqueMetricByName(
  metrics: readonly BinaryMetric[],
  name: string,
): BinaryMetric | null {
  const matches = metrics.filter((metric) => metric.name === name);
  return matches.length === 1 ? matches[0]! : null;
}

function optionalMetricView(report: BenchReportData, metric: BinaryMetric | null): BinaryMetricView | null {
  return metric ? binaryMetricView(report, metric) : null;
}

export function selectFalsePositiveMetrics(report: BenchReportData): BinaryMetricView[] {
  return report.false_positives.map((metric) => binaryMetricView(report, metric));
}

export function selectReportAgentMetrics(report: BenchReportData): BinaryMetricView[] {
  return report.agent.map((metric) => binaryMetricView(report, metric));
}

export function selectKeyMetrics(report: BenchReportData): KeyMetricViews {
  return {
    constraintFp: optionalMetricView(
      report,
      selectUniqueMetricByName(report.false_positives, "constraint_fp"),
    ),
    fixPassRate: optionalMetricView(report, selectUniqueMetricByName(report.agent, "fix_pass_rate")),
    oracleFp: optionalMetricView(report, selectUniqueMetricByName(report.false_positives, "oracle_fp")),
  };
}

export function selectQaEvidenceState(report: BenchReportData): QaEvidenceState {
  const status = evidenceView(report, report.qa.evidence_ref);
  const metricEvidence = [
    report.qa.manual_success,
    report.qa.assisted_success,
    report.qa.paired_saved_minutes,
    report.qa.paired_saved_fraction,
  ].map((metric) => evidenceView(report, metric.evidence_ref));
  const hasMissingEvidence =
    status.status === "evidence_missing" ||
    metricEvidence.some((evidence) => evidence.status === "evidence_missing");
  const missingStates: NamedMissingState[] = [];
  if (report.qa.conclusion === "pending") missingStates.push("pending_human_evidence");
  if (hasMissingEvidence) missingStates.push("evidence_missing");
  return {
    acceptanceCode: hasMissingEvidence ? "qa.evidence_missing" : null,
    evidence: status.artifact,
    evidenceStatus: status,
    missingStates,
    plannedCatalogEvidence: report.qa.evidence_ref ? null : uniqueEvidence(report, "qa"),
  };
}

export function selectCostWorkloads(report: BenchReportData): CostWorkloadView[] {
  return report.cost_latency.agent.workloads.map((workload) => ({
    evidence: evidenceView(report, workload.evidence_ref),
    monetaryState: "not_measured",
    requestLatency: distributionMetricView(report, workload.request_latency_ms),
    tokensPerSample: distributionMetricView(report, workload.tokens_per_sample),
    transport: {
      knownAttempts: workload.known_transport_attempts,
      knownRetries: workload.known_transport_retries,
      state: workload.unknown_transport_attempt_records > 0 ? "has_unknown_records" : "complete",
      unknownAttemptRecords: workload.unknown_transport_attempt_records,
    },
    workload,
  }));
}
