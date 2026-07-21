import { useQuery } from "@tanstack/react-query";
import {
  BarChart3,
  BookOpenCheck,
  Boxes,
  Clock3,
  Database,
  FileWarning,
  Gauge,
  ShieldCheck,
} from "lucide-react";
import type { ReactNode } from "react";

import { ApiProblemError } from "../../api/problem";
import { HorizontalBarChart, RingChart } from "../../components/charts";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { evalApi, type BenchReportRead, type EvalApi } from "./api";
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
  type BdrMetricView,
  type BinaryMetricView,
  type BenchReportData,
  type DistributionMetricView,
  type EvidenceView,
} from "./model";
import "./eval.css";

type MetricStatus = "pending" | "measured" | "underpowered" | "inconclusive" | "failed";

const integerFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });
const numberFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 });
const percentFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 1,
  style: "percent",
});

const partitionMeta = {
  deterministic: {
    description: "图 / ASP / SMT 的可判定结构与数值检查；不与 LLM 指标合并。",
    label: "确定性 BDR",
    tone: "deterministic",
  },
  simulation: {
    description: "Monte-Carlo / ABM 的描述性经济证据，独立于确定性判定。",
    label: "仿真 BDR",
    tone: "simulation",
  },
  "llm-assisted": {
    description: "LLM 辅助叙事检查；结论仍需按协议解释，不进入确定性检出率。",
    label: "LLM 辅助 BDR",
    tone: "suggestion",
  },
} as const;

const agentOutcomeChartLabels: Readonly<Record<string, string>> = {
  fix_pass_rate: "Fix pass",
  playtest_completion_flat: "Playtest · flat",
  playtest_completion_layered: "Playtest · layered",
  playtest_completion_mem_on: "Playtest · memory",
};

function displayRate(value: number | null | undefined): string {
  return value === null || value === undefined ? "not_measured" : percentFormatter.format(value);
}

function StatusChip({ status }: { status: MetricStatus }) {
  const tone =
    status === "measured"
      ? "ok"
      : status === "failed"
        ? "danger"
        : status === "pending"
          ? "info"
          : "suggestion";
  return <span className={`u-status u-status--${tone}`}>{status}</span>;
}

function MissingChip({ children }: { children: ReactNode }) {
  return <span className="u-status u-status--suggestion gf-eval__missing-chip">{children}</span>;
}

function EvidenceInline({ evidence }: { evidence: EvidenceView }) {
  if (evidence.reference === null) {
    return (
      <span className="gf-eval__evidence-inline" data-evidence="missing">
        <MissingChip>evidence_missing</MissingChip>
        <span>无 evidence_ref</span>
      </span>
    );
  }
  return (
    <span className="gf-eval__evidence-inline" data-evidence={evidence.status}>
      <code>{evidence.reference}</code>
      {evidence.status === "available" ? (
        <span className="u-status u-status--ok">available</span>
      ) : (
        <MissingChip>evidence_missing</MissingChip>
      )}
      {evidence.artifact && (
        <span className="gf-eval__evidence-meta">
          <code>{evidence.artifact.path}</code>
          {evidence.artifact.sha256 && <code>{evidence.artifact.sha256}</code>}
        </span>
      )}
    </span>
  );
}

function ScrollTable({ children }: { children: ReactNode }) {
  return (
    <div className="gf-eval__table-scroll" tabIndex={0}>
      {children}
    </div>
  );
}

function SectionHeading({
  description,
  icon: Icon,
  title,
}: {
  description: string;
  icon: typeof BarChart3;
  title: string;
}) {
  return (
    <header className="gf-eval__section-heading">
      <Icon aria-hidden="true" size={20} />
      <div>
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
    </header>
  );
}

function BdrTable({ rows, title }: { rows: readonly BdrMetricView[]; title: string }) {
  return (
    <ScrollTable>
      <table aria-label={title} className="gf-eval__metric-table gf-eval__metric-table--bdr">
        <thead>
          <tr>
            <th scope="col">缺陷类</th>
            <th scope="col">Oracle 分区</th>
            <th scope="col">n / planned</th>
            <th scope="col">k</th>
            <th scope="col">Rate</th>
            <th scope="col">Confidence interval</th>
            <th scope="col">Power</th>
            <th scope="col">Status</th>
            <th scope="col">Protocol</th>
            <th scope="col">Evidence</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.partition}:${row.defectClass}`}>
              <th scope="row">
                <code>{row.defectClass ?? "defect_class_missing"}</code>
              </th>
              <td>
                <span className={`gf-eval__partition gf-eval__partition--${row.partition}`}>
                  {row.partition}
                </span>
              </td>
              <td>
                {integerFormatter.format(row.evaluatedN)} / {integerFormatter.format(row.plannedN)}
              </td>
              <td>{integerFormatter.format(row.metric.k)}</td>
              <td>{displayRate(row.metric.rate)}</td>
              <td>{row.interval ?? "not_measured"}</td>
              <td>
                {row.power ? (
                  <span className="gf-eval__power">
                    <span>
                      {numberFormatter.format(row.power.achieved_half_width)} achieved /{" "}
                      {numberFormatter.format(row.power.target_half_width)} target
                    </span>
                    <StatusChip status={row.power.status} />
                    {row.powerEvidence.reference !== row.evidence.reference && (
                      <EvidenceInline evidence={row.powerEvidence} />
                    )}
                  </span>
                ) : (
                  <MissingChip>evidence_missing</MissingChip>
                )}
              </td>
              <td>
                <StatusChip status={row.metric.status} />
              </td>
              <td>
                <code>{row.protocolId ?? "evidence_missing"}</code>
              </td>
              <td>
                <EvidenceInline evidence={row.evidence} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </ScrollTable>
  );
}

function BinaryMetricTable({
  includeDefectClass = false,
  rows,
  title,
}: {
  includeDefectClass?: boolean;
  rows: readonly BinaryMetricView[];
  title: string;
}) {
  return (
    <ScrollTable>
      <table aria-label={title} className="gf-eval__metric-table">
        <thead>
          <tr>
            <th scope="col">Metric</th>
            {includeDefectClass && <th scope="col">Defect class</th>}
            <th scope="col">Bucket</th>
            <th scope="col">n / planned</th>
            <th scope="col">k</th>
            <th scope="col">Rate</th>
            <th scope="col">Confidence interval</th>
            <th scope="col">Status</th>
            <th scope="col">Protocol</th>
            <th scope="col">Evidence</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row.metric.name}:${row.metric.bucket}:${row.metric.defect_class ?? "all"}:${index}`}>
              <th scope="row">
                <code>{row.metric.name}</code>
              </th>
              {includeDefectClass && (
                <td>
                  <code>{row.metric.defect_class ?? "all"}</code>
                </td>
              )}
              <td>
                <code>{row.metric.bucket}</code>
              </td>
              <td>
                {integerFormatter.format(row.evaluatedN)} / {integerFormatter.format(row.plannedN)}
              </td>
              <td>{integerFormatter.format(row.metric.k)}</td>
              <td>{displayRate(row.metric.rate)}</td>
              <td>{row.interval ?? "not_measured"}</td>
              <td>
                <StatusChip status={row.metric.status} />
              </td>
              <td>
                <code>{row.protocolId ?? "evidence_missing"}</code>
              </td>
              <td>
                <EvidenceInline evidence={row.evidence} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </ScrollTable>
  );
}

function DistributionMetricTable({
  rows,
  title,
}: {
  rows: readonly DistributionMetricView[];
  title: string;
}) {
  return (
    <ScrollTable>
      <table aria-label={title} className="gf-eval__metric-table">
        <thead>
          <tr>
            <th scope="col">Metric</th>
            <th scope="col">Distribution</th>
            <th scope="col">n / planned</th>
            <th scope="col">Confidence interval</th>
            <th scope="col">Status</th>
            <th scope="col">Protocol</th>
            <th scope="col">Evidence</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.metric.name}:${row.metric.bucket}`}>
              <th scope="row">
                <code>{row.metric.name}</code>
              </th>
              <td>{row.estimate ?? "not_measured"}</td>
              <td>
                {integerFormatter.format(row.evaluatedN)} / {integerFormatter.format(row.plannedN)}
              </td>
              <td>{row.interval ?? "not_measured"}</td>
              <td>
                <StatusChip status={row.metric.status} />
              </td>
              <td>
                <code>{row.protocolId ?? "evidence_missing"}</code>
              </td>
              <td>
                <EvidenceInline evidence={row.evidence} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </ScrollTable>
  );
}

function MetricSummaryCard({
  label,
  metric,
  note,
}: {
  label: string;
  metric: BinaryMetricView | null;
  note: string;
}) {
  return (
    <article className="gf-eval__summary-card">
      <p className="gf-eval__eyebrow">{note}</p>
      <h3>{label}</h3>
      {metric ? (
        <>
          <strong>{metric.estimate ?? "not_measured"}</strong>
          <p>{metric.interval ?? "Confidence interval · not_measured"}</p>
          <div className="gf-eval__summary-meta">
            <StatusChip status={metric.metric.status} />
            <code>{metric.protocolId ?? "evidence_missing"}</code>
          </div>
          <EvidenceInline evidence={metric.evidence} />
        </>
      ) : (
        <>
          <MissingChip>evidence_missing</MissingChip>
          <p>同名指标缺失或不唯一；页面没有猜测一条作为权威。</p>
        </>
      )}
    </article>
  );
}

function NarrativeProvenance({ report }: { report: BenchReportData }) {
  const narrative = report.narrative;
  return (
    <aside aria-label="Narrative metric provenance" className="gf-eval__provenance-ribbon">
      <div>
        <span>Model snapshot</span>
        <code>
          {narrative.model_snapshot.provider} / {narrative.model_snapshot.model} /{" "}
          {narrative.model_snapshot.snapshot_tag}
        </code>
      </div>
      <div>
        <span>Protocol SHA-256</span>
        <code>{narrative.protocol_sha256}</code>
      </div>
      <div>
        <span>Corpus manifest SHA-256</span>
        <code>{narrative.corpus_manifest_sha256}</code>
      </div>
      <EvidenceInline evidence={evidenceView(report, narrative.evidence_ref)} />
    </aside>
  );
}

function ReportAuthority({ read }: { read: BenchReportRead }) {
  return (
    <section aria-label="报告权威" className="gf-eval__authority">
      <div>
        <span>BenchReport Artifact</span>
        {read.artifactId ? (
          <code tabIndex={0}>{read.artifactId}</code>
        ) : (
          <strong>X-Artifact-ID 缺失；未从报告内容猜测 Artifact 身份</strong>
        )}
      </div>
      <div>
        <span>Response ETag</span>
        <code>{read.etag}</code>
      </div>
      <div>
        <span>Schema / builder</span>
        <code>
          {read.report.schema_version} · {read.report.meta.report_builder_version}
        </code>
      </div>
      <div>
        <span>Corpus / seed / generated</span>
        <code>
          {integerFormatter.format(read.report.meta.corpus_size)} cases · seed{" "}
          {read.report.meta.seed ?? "not_applicable"} · {read.report.meta.generated_at ?? "not_recorded"}
        </code>
      </div>
      {read.artifactId && (
        <nav aria-label="BenchReport Artifact 导航" className="gf-eval__authority-links">
          <a href={`/artifacts/${encodeURIComponent(read.artifactId)}`}>打开 BenchReport Artifact</a>
          <a href={`/artifacts/${encodeURIComponent(read.artifactId)}/lineage`}>查看 BenchReport 血缘</a>
        </nav>
      )}
    </section>
  );
}

function BdrSection({ report }: { report: BenchReportData }) {
  const rows = selectBdrMetrics(report);
  return (
    <section aria-label="分缺陷类 BDR" className="gf-eval__section">
      <SectionHeading
        description="15 个缺陷类逐类报告 n、k、rate、CI、功效、协议与证据；不输出掩盖差异的总分。"
        icon={BarChart3}
        title="分缺陷类 Bug Detection Rate"
      />
      <div className="gf-eval__partition-stack">
        {(Object.keys(partitionMeta) as (keyof typeof partitionMeta)[]).map((partition) => {
          const meta = partitionMeta[partition];
          const partitionRows = rows.filter((row) => row.partition === partition);
          return (
            <article className="gf-eval__subsection" data-tone={meta.tone} key={partition}>
              <header>
                <div>
                  <h3>{meta.label}</h3>
                  <p>{meta.description}</p>
                </div>
                <span className="u-chip">{partitionRows.length} classes</span>
              </header>
              {partition === "llm-assisted" && <NarrativeProvenance report={report} />}
              {partition === "llm-assisted" && (
                <BinaryMetricTable
                  rows={[binaryMetricView(report, report.narrative.clean_fp)]}
                  title="Narrative clean FP"
                />
              )}
              <BdrTable rows={partitionRows} title={meta.label} />
            </article>
          );
        })}
      </div>
    </section>
  );
}

function HeadlineAndOutcomes({ report }: { report: BenchReportData }) {
  const keyMetrics = selectKeyMetrics(report);
  const agentMetrics = selectReportAgentMetrics(report);
  return (
    <>
      <section aria-labelledby="eval-key-metrics-heading" className="gf-eval__summary-grid">
        <h2 className="u-sr-only" id="eval-key-metrics-heading">
          关键指标
        </h2>
        <MetricSummaryCard label="Oracle FP" metric={keyMetrics.oracleFp} note="检查器算法误报 · 独立口径" />
        <MetricSummaryCard
          label="Constraint FP"
          metric={keyMetrics.constraintFp}
          note="约束质量误报 · 独立口径"
        />
        <MetricSummaryCard label="Fix Pass Rate" metric={keyMetrics.fixPassRate} note="复验 + 回归通过" />
      </section>

      <section aria-label="误报与 Agent 结果" className="gf-eval__section gf-eval__split-section">
        <SectionHeading
          description="所有已知与新增指标原样保留；Oracle FP、Constraint FP、LLM 与外部 FP 不合并。"
          icon={ShieldCheck}
          title="False-positive 与 Agent outcomes"
        />
        <article className="gf-eval__subsection">
          <header>
            <div>
              <h3>False-positive 指标</h3>
              <p>第一 KPI 的完整分栏视图。</p>
            </div>
          </header>
          <NarrativeProvenance report={report} />
          <BinaryMetricTable rows={selectFalsePositiveMetrics(report)} title="False-positive 指标" />
        </article>
        <article className="gf-eval__subsection">
          <header>
            <div>
              <h3>Agent outcomes</h3>
              <p>含聚合 Fix Pass 与 Playtest 对照，不遗漏未知扩展指标。</p>
            </div>
          </header>
          <div className="gf-eval__purposeful-chart">
            <HorizontalBarChart
              data={agentMetrics
                .filter((row) => row.metric.rate !== null && row.metric.rate !== undefined)
                .map((row) => ({
                  label: agentOutcomeChartLabels[row.metric.name] ?? row.metric.name,
                  value: row.metric.rate!,
                }))}
              summary="只画报告内已有 rate；完整 n/k/CI/协议/证据仍以下表为准。"
              title="Agent outcome rates"
              valueFormatter={(value) => percentFormatter.format(value)}
              valueLabel="Rate"
            />
          </div>
          <BinaryMetricTable rows={agentMetrics} title="Agent outcomes" />
        </article>
      </section>
    </>
  );
}

function ExternalSection({ report }: { report: BenchReportData }) {
  const external = report.external;
  return (
    <section aria-label="外部效度" className="gf-eval__section">
      <SectionHeading
        description="Development 与 verification 保持独立；小样本 underpowered 不被 100% rate 掩盖。"
        icon={Boxes}
        title="外部效度"
      />
      <dl className="gf-eval__fact-grid">
        <div>
          <dt>Source</dt>
          <dd>
            <code>{external.source_id}</code>
          </dd>
        </div>
        <div>
          <dt>Repository</dt>
          <dd>
            <code>{external.repository}</code>
          </dd>
        </div>
        <div>
          <dt>Qualified / total</dt>
          <dd>
            {external.qualified_cases} / {external.total_cases}
          </dd>
        </div>
        <div>
          <dt>Reader</dt>
          <dd>
            <code>{external.reader_version}</code>
          </dd>
        </div>
        <div>
          <dt>Adapter</dt>
          <dd>
            <code>{external.adapter_version}</code>
          </dd>
        </div>
        <div>
          <dt>Manifest SHA-256</dt>
          <dd>
            <code>{external.manifest_sha256}</code>
          </dd>
        </div>
        <div>
          <dt>Mapping spec SHA-256</dt>
          <dd>
            <code>{external.mapping_spec_sha256}</code>
          </dd>
        </div>
        <div>
          <dt>Evidence</dt>
          <dd>
            <EvidenceInline evidence={evidenceView(report, external.evidence_ref)} />
          </dd>
        </div>
      </dl>

      <article className="gf-eval__subsection">
        <header>
          <div>
            <h3>After-oracle FP</h3>
            <p>外部语料经过 oracle 后的独立误报观察。</p>
          </div>
        </header>
        <BinaryMetricTable rows={[binaryMetricView(report, external.after_oracle_fp)]} title="External FP" />
      </article>
      <div className="gf-eval__two-column">
        <article className="gf-eval__subsection">
          <header>
            <div>
              <h3>Development</h3>
              <p>用于能力开发观察，不冒充独立 verification。</p>
            </div>
          </header>
          <BinaryMetricTable
            includeDefectClass
            rows={external.development.map((metric) => binaryMetricView(report, metric))}
            title="External development"
          />
        </article>
        <article className="gf-eval__subsection">
          <header>
            <div>
              <h3>Verification</h3>
              <p>独立验证分区；功效不足仍以 underpowered 呈现。</p>
            </div>
          </header>
          <BinaryMetricTable
            includeDefectClass
            rows={external.verification.map((metric) => binaryMetricView(report, metric))}
            title="External verification"
          />
        </article>
      </div>
    </section>
  );
}

function HedSection({ report }: { report: BenchReportData }) {
  const hed = report.hed;
  const dispositions = hed.dispositions.map((metric) => binaryMetricView(report, metric));
  return (
    <section aria-label="Human-Edit-Distance" className="gf-eval__section">
      <SectionHeading
        description="距离分布与人工 disposition 分开呈现；均保留样本量、区间、协议和证据。"
        icon={BookOpenCheck}
        title="Human-Edit-Distance"
      />
      <div className="gf-eval__model-ribbon">
        <span>Measured model snapshot</span>
        <code>
          {hed.model_snapshot.provider} / {hed.model_snapshot.model} / {hed.model_snapshot.snapshot_tag}
        </code>
        <EvidenceInline evidence={evidenceView(report, hed.evidence_ref)} />
      </div>
      <article className="gf-eval__subsection">
        <header>
          <div>
            <h3>Distance distributions</h3>
            <p>Raw atomic changes 与 normalized distance 不互相替代。</p>
          </div>
        </header>
        <DistributionMetricTable
          rows={[
            distributionMetricView(report, hed.raw_distance),
            distributionMetricView(report, hed.normalized_distance),
          ]}
          title="HED distributions"
        />
      </article>
      <article className="gf-eval__subsection">
        <header>
          <div>
            <h3>Human dispositions</h3>
            <p>Unchanged、edited、unusable 与 protocol failure 独立报告。</p>
          </div>
        </header>
        <div className="gf-eval__purposeful-chart gf-eval__purposeful-chart--ring">
          <RingChart
            data={dispositions.map((row) => ({ label: row.metric.name, value: row.metric.k }))}
            summary="按人工 disposition 的 k 展示；精确区间、协议与 evidence 仍以下表为准。"
            title="HED disposition counts"
            valueLabel="cases"
          />
        </div>
        <BinaryMetricTable rows={dispositions} title="HED dispositions" />
      </article>
    </section>
  );
}

function QaSection({ report }: { report: BenchReportData }) {
  const qa = report.qa;
  const state = selectQaEvidenceState(report);
  const plannedCatalogEvidence = state.plannedCatalogEvidence;
  const rows = [
    {
      kind: "binary" as const,
      name: qa.manual_success.name,
      view: binaryMetricView(report, qa.manual_success),
    },
    {
      kind: "binary" as const,
      name: qa.assisted_success.name,
      view: binaryMetricView(report, qa.assisted_success),
    },
    {
      kind: "distribution" as const,
      name: qa.paired_saved_minutes.name,
      view: distributionMetricView(report, qa.paired_saved_minutes),
    },
    {
      kind: "distribution" as const,
      name: qa.paired_saved_fraction.name,
      view: distributionMetricView(report, qa.paired_saved_fraction),
    },
  ];
  const hasNotMeasured = rows.some((row) => row.view.estimate === null);
  const missingHumanEvidence = state.missingStates.length > 0 || hasNotMeasured;

  return (
    <section
      aria-label="真人 QA"
      className="gf-eval__section gf-eval__qa"
      data-evidence={state.evidenceStatus.status}
    >
      <SectionHeading
        description="八场实测使用隔离本地 QA Runner；正确场次按实际 active 计分，错误或超时场次按 8 分钟计分；原始 active 时长另行完整保留。"
        icon={FileWarning}
        title="真人 QA"
      />
      <div className="gf-eval__qa-status">
        <div>
          <span>Conclusion</span>
          <strong>{qa.conclusion}</strong>
        </div>
        <div className="gf-eval__missing-list" aria-label="QA evidence states">
          {state.missingStates.includes("pending_human_evidence") && (
            <MissingChip>pending_human_evidence</MissingChip>
          )}
          {state.missingStates.includes("evidence_missing") && <MissingChip>evidence_missing</MissingChip>}
          {hasNotMeasured && <MissingChip>not_measured</MissingChip>}
          {state.acceptanceCode && <MissingChip>{state.acceptanceCode}</MissingChip>}
          {!missingHumanEvidence && <span className="u-status u-status--ok">human evidence available</span>}
        </div>
        <p>
          Scope · <code>{qa.scope}</code>
        </p>
        <p>
          Protocol SHA-256 · <code>{qa.protocol_sha256}</code>
        </p>
        {qa.evidence_ref ? (
          <EvidenceInline evidence={state.evidenceStatus} />
        ) : (
          <div className="gf-eval__qa-binding">
            <MissingChip>evidence_missing</MissingChip>
            <span>Exact QA evidence_ref 未绑定。</span>
          </div>
        )}
        {plannedCatalogEvidence && (
          <aside className="gf-eval__planned-evidence">
            <strong>planned catalog entry / not bound</strong>
            <code>{plannedCatalogEvidence.path}</code>
            <code>{plannedCatalogEvidence.schema_version}</code>
            {plannedCatalogEvidence.sha256 && <code>{plannedCatalogEvidence.sha256}</code>}
          </aside>
        )}
      </div>

      <ScrollTable>
        <table aria-label="QA metrics" className="gf-eval__metric-table">
          <thead>
            <tr>
              <th scope="col">Metric</th>
              <th scope="col">Result</th>
              <th scope="col">Coverage</th>
              <th scope="col">Confidence interval</th>
              <th scope="col">Status</th>
              <th scope="col">Protocol</th>
              <th scope="col">Evidence</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const measured = row.view.estimate !== null;
              return (
                <tr key={row.name}>
                  <th scope="row">
                    <code>{row.name}</code>
                  </th>
                  <td>{measured ? row.view.estimate : "等待真人证据"}</td>
                  <td>
                    {measured
                      ? `${integerFormatter.format(row.view.evaluatedN)} / ${integerFormatter.format(row.view.plannedN)}`
                      : "等待真人证据"}
                  </td>
                  <td>{measured ? (row.view.interval ?? "not_measured") : "等待真人证据"}</td>
                  <td>
                    <StatusChip status={row.view.metric.status} />
                  </td>
                  <td>
                    <code>{row.view.protocolId ?? "evidence_missing"}</code>
                  </td>
                  <td>{measured ? <EvidenceInline evidence={row.view.evidence} /> : "等待真人证据"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </ScrollTable>
    </section>
  );
}

function TokenTotals({
  totals,
}: {
  totals: BenchReportData["cost_latency"]["agent"]["workloads"][number]["tokens"];
}) {
  return (
    <dl className="gf-eval__token-grid">
      <div>
        <dt>Input</dt>
        <dd>{integerFormatter.format(totals.input_tokens)}</dd>
      </div>
      <div>
        <dt>Output</dt>
        <dd>{integerFormatter.format(totals.output_tokens)}</dd>
      </div>
      <div>
        <dt>Cache read</dt>
        <dd>{integerFormatter.format(totals.cache_read_tokens)}</dd>
      </div>
      <div>
        <dt>Cache write</dt>
        <dd>{integerFormatter.format(totals.cache_write_tokens)}</dd>
      </div>
      <div>
        <dt>Reported total</dt>
        <dd>{integerFormatter.format(totals.reported_total_tokens)}</dd>
      </div>
    </dl>
  );
}

function CostSection({ report }: { report: BenchReportData }) {
  const workloads = selectCostWorkloads(report);
  const deterministic = report.cost_latency.deterministic;
  const deterministicRuntime = distributionMetricView(report, deterministic.per_sample_ms);
  return (
    <section aria-label="成本与延迟" className="gf-eval__section">
      <SectionHeading
        description="Agent provider 记录值与确定性流水线运行时分栏；回放速度不冒充原始调用延迟。"
        icon={Clock3}
        title="成本与延迟"
      />
      <aside className="gf-eval__latency-note">
        <Gauge aria-hidden="true" size={18} />
        <p>
          Agent latency 是 cassette/provider 的 <strong>Provider record-time latency</strong>；不是 E2E
          运行时，也不是 replay 播放耗时。Known attempts 为 0 时，仍必须结合 unknown records 解读。
        </p>
      </aside>

      <div className="gf-eval__workload-grid">
        {workloads.map((view) => {
          const workload = view.workload;
          return (
            <article
              aria-label={workload.workload_id}
              className="gf-eval__workload"
              data-testid="agent-workload"
              key={workload.workload_id}
            >
              <header>
                <div>
                  <p className="gf-eval__eyebrow">Agent workload</p>
                  <h3>{workload.workload_id}</h3>
                </div>
                <EvidenceInline evidence={view.evidence} />
              </header>
              <dl className="gf-eval__workload-facts">
                <div>
                  <dt>Provider / model / snapshot</dt>
                  <dd>
                    <code>
                      {workload.model_snapshot.provider} / {workload.model_snapshot.model} /{" "}
                      {workload.model_snapshot.snapshot_tag}
                    </code>
                  </dd>
                </div>
                <div>
                  <dt>Samples · evaluated / planned</dt>
                  <dd>
                    {integerFormatter.format(workload.evaluated_n)} /{" "}
                    {integerFormatter.format(workload.planned_n)}
                  </dd>
                </div>
                <div>
                  <dt>Logical / recorded requests</dt>
                  <dd>
                    {integerFormatter.format(workload.logical_requests)} /{" "}
                    {integerFormatter.format(workload.recorded_requests)}
                  </dd>
                </div>
                <div>
                  <dt>Session cache reuses</dt>
                  <dd>{integerFormatter.format(workload.session_cache_reuses)}</dd>
                </div>
                <div>
                  <dt>Known transport attempts / retries</dt>
                  <dd>
                    {integerFormatter.format(view.transport.knownAttempts)} /{" "}
                    {integerFormatter.format(view.transport.knownRetries)}
                  </dd>
                </div>
                <div data-transport-state={view.transport.state}>
                  <dt>Unknown transport-attempt records</dt>
                  <dd>
                    <strong>
                      {integerFormatter.format(view.transport.unknownAttemptRecords)} unknown records
                    </strong>
                    {view.transport.state === "has_unknown_records" && (
                      <span>Known 计数不能解释为完整 attempts/retries。</span>
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Tokens / sample</dt>
                  <dd>{view.tokensPerSample.estimate ?? "not_measured"}</dd>
                </div>
                <div>
                  <dt>Tokens / sample CI</dt>
                  <dd>{view.tokensPerSample.interval ?? "not_measured"}</dd>
                </div>
                <div>
                  <dt>Provider record-time latency</dt>
                  <dd>{view.requestLatency.estimate ?? "not_measured"}</dd>
                </div>
                <div>
                  <dt>Provider record-time latency CI</dt>
                  <dd>{view.requestLatency.interval ?? "not_measured"}</dd>
                </div>
                <div>
                  <dt>Monetary cost</dt>
                  <dd>
                    <MissingChip>{view.monetaryState}</MissingChip>
                    <span>PriceBook 未绑定，不从 token 估算货币。</span>
                  </dd>
                </div>
              </dl>
              <TokenTotals totals={workload.tokens} />
              <footer>
                <span>
                  Tokens/sample protocol ·{" "}
                  <code>{view.tokensPerSample.protocolId ?? "evidence_missing"}</code>
                </span>
                <span>
                  Provider-latency protocol ·{" "}
                  <code>{view.requestLatency.protocolId ?? "evidence_missing"}</code>
                </span>
              </footer>
            </article>
          );
        })}
      </div>

      <article aria-label="确定性运行时" className="gf-eval__deterministic-runtime">
        <header>
          <div>
            <p className="gf-eval__eyebrow">Deterministic runtime · separate authority</p>
            <h3>确定性运行时</h3>
          </div>
          <EvidenceInline evidence={evidenceView(report, deterministic.evidence_ref)} />
        </header>
        <dl className="gf-eval__fact-grid">
          <div>
            <dt>Workload</dt>
            <dd>
              <code>{deterministic.workload_id}</code>
            </dd>
          </div>
          <div>
            <dt>Per-sample runtime</dt>
            <dd>{deterministicRuntime.estimate ?? "not_measured"}</dd>
          </div>
          <div>
            <dt>Confidence interval</dt>
            <dd>{deterministicRuntime.interval ?? "not_measured"}</dd>
          </div>
          <div>
            <dt>Setup</dt>
            <dd>{numberFormatter.format(deterministic.setup_ms)} ms</dd>
          </div>
          <div>
            <dt>Protocol</dt>
            <dd>
              <code>{deterministicRuntime.protocolId ?? "evidence_missing"}</code>
            </dd>
          </div>
          <div>
            <dt>Environment SHA-256</dt>
            <dd>
              <code>{deterministic.environment_sha256}</code>
            </dd>
          </div>
        </dl>
      </article>
    </section>
  );
}

function CatalogSection({ report }: { report: BenchReportData }) {
  return (
    <section aria-label="版本与证据" className="gf-eval__section">
      <SectionHeading
        description="路径仅作为报告内 provenance 展示，不被浏览器误当成可访问链接；摘要原样保留。"
        icon={Database}
        title="版本与证据目录"
      />
      <div className="gf-eval__two-column">
        <article className="gf-eval__subsection">
          <header>
            <div>
              <h3>Version bindings</h3>
              <p>构建报告时冻结的组件版本与可用摘要。</p>
            </div>
          </header>
          <ScrollTable>
            <table aria-label="Version bindings" className="gf-eval__catalog-table">
              <thead>
                <tr>
                  <th scope="col">Component</th>
                  <th scope="col">Version</th>
                  <th scope="col">SHA-256</th>
                </tr>
              </thead>
              <tbody>
                {report.versions.map((version) => (
                  <tr key={`${version.component}:${version.version}`}>
                    <th scope="row">
                      <code>{version.component}</code>
                    </th>
                    <td>
                      <code>{version.version}</code>
                    </td>
                    <td>
                      <code>{version.sha256 ?? "not_applicable"}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollTable>
        </article>
        <article className="gf-eval__subsection">
          <header>
            <div>
              <h3>Evidence catalog</h3>
              <p>available 与 evidence_missing 由报告声明，不通过路径存在感猜测。</p>
            </div>
          </header>
          <ScrollTable>
            <table aria-label="Evidence catalog" className="gf-eval__catalog-table">
              <thead>
                <tr>
                  <th scope="col">Evidence ID</th>
                  <th scope="col">State</th>
                  <th scope="col">Schema</th>
                  <th scope="col">Path (provenance only)</th>
                  <th scope="col">SHA-256</th>
                </tr>
              </thead>
              <tbody>
                {report.evidence.map((evidence) => (
                  <tr key={evidence.evidence_id}>
                    <th scope="row">
                      <code>{evidence.evidence_id}</code>
                    </th>
                    <td>
                      {evidence.available ? (
                        <span className="u-status u-status--ok">available</span>
                      ) : (
                        <MissingChip>evidence_missing</MissingChip>
                      )}
                    </td>
                    <td>
                      <code>{evidence.schema_version}</code>
                    </td>
                    <td>
                      <code>{evidence.path}</code>
                    </td>
                    <td>
                      <code>{evidence.sha256 ?? "evidence_missing"}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollTable>
        </article>
      </div>
    </section>
  );
}

function EvalError({ error, onRetry }: { error: Error; onRetry(): void }) {
  return (
    <div className="gf-eval__error">
      {error instanceof ApiProblemError ? (
        <ProblemPanel problem={error.problem} />
      ) : (
        <StatePanel
          description="BenchReport 读取失败；页面未显示底层异常。"
          state="error"
          title="无法读取 Eval / Bench"
        />
      )}
      <button className="gf-secondary-button" onClick={onRetry} type="button">
        重试读取 BenchReport
      </button>
    </div>
  );
}

export function EvalPage({ api = evalApi }: { api?: EvalApi }) {
  const query = useQuery({
    queryFn: () => api.getBenchReport(),
    queryKey: ["eval", "bench-report"],
    retry: false,
  });

  if (query.isPending) {
    return (
      <div className="gf-page gf-eval">
        <StatePanel
          description="正在读取冻结的 BenchReport v2 与证据目录。"
          headingLevel={1}
          state="loading"
          title="正在读取 BenchReport"
        />
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="gf-page gf-eval">
        <header className="gf-page-header">
          <p className="gf-eval__kicker">Evaluation ledger · report authority</p>
          <h1>Eval / Bench</h1>
        </header>
        <EvalError error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const read = query.data;
  const report = read.report;
  return (
    <div className="gf-page gf-eval" data-layout="editorial-eval-report">
      <header className="gf-eval__hero">
        <div>
          <p className="gf-eval__kicker">GameForge Bench · evidence ledger</p>
          <h1>Eval / Bench</h1>
          <p>
            把检出、误报、修复、人工编辑、真人 QA
            与成本证据放在同一份可追溯报告里；缺失证据保留缺失，不用漂亮总分遮盖。
          </p>
        </div>
        <div aria-hidden="true" className="gf-eval__hero-mark">
          <BarChart3 size={30} />
          <span>BENCH</span>
          <strong>{integerFormatter.format(report.meta.corpus_size)}</strong>
          <small>corpus cases</small>
        </div>
      </header>

      <ReportAuthority read={read} />
      <HeadlineAndOutcomes report={report} />
      <BdrSection report={report} />
      <ExternalSection report={report} />
      <HedSection report={report} />
      <QaSection report={report} />
      <CostSection report={report} />
      <CatalogSection report={report} />
    </div>
  );
}
