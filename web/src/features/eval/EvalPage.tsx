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
import { TechnicalDetails } from "../../components/identity";
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

const integerFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 0,
});
const numberFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 3,
});
const percentFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 1,
  style: "percent",
});

const partitionMeta = {
  deterministic: {
    description: "由规则图和数学求解器直接判定结构与数值问题，不与 AI 建议混算。",
    label: "确定性问题检出率",
    tone: "deterministic",
  },
  simulation: {
    description: "通过多轮经济仿真发现系统性风险，与确定性检查分开统计。",
    label: "仿真问题检出率",
    tone: "simulation",
  },
  "llm-assisted": {
    description: "AI 辅助发现叙事与设定问题，结果仍需按评测方案解释，不冒充确定性判定。",
    label: "AI 辅助问题检出率",
    tone: "suggestion",
  },
} as const;

const agentOutcomeChartLabels: Readonly<Record<string, string>> = {
  fix_pass_rate: "修复通过率",
  playtest_completion_flat: "直接试玩",
  playtest_completion_layered: "分层规划试玩",
  playtest_completion_mem_on: "启用记忆试玩",
};

const metricLabels: Readonly<Record<string, string>> = {
  assisted_success: "工具辅助成功率",
  constraint_fp: "约束检查误报率",
  deterministic_pipeline_runtime_ms: "确定性检查耗时",
  external_after_oracle_fp: "外部病例误报率",
  external_bdr: "外部病例检出率",
  fix_pass_rate: "修复通过率",
  hed_edited: "需要人工编辑",
  hed_normalized_distance: "标准化编辑距离",
  hed_protocol_failure: "评测流程无效",
  hed_raw_distance: "原始编辑量",
  hed_unchanged: "无需人工编辑",
  hed_unusable: "结果无法使用",
  manual_success: "纯人工成功率",
  narrative_clean_fp: "叙事检查误报率",
  oracle_fp: "确定性检查误报率",
  paired_saved_fraction: "相对节省时间",
  paired_saved_minutes: "每组节省分钟数",
  playtest_completion_flat: "直接试玩完成率",
  playtest_completion_layered: "分层规划试玩完成率",
  playtest_completion_mem_on: "启用记忆试玩完成率",
  request_latency_ms: "模型响应耗时",
  tokens_per_sample: "每个样本的模型用量",
};

const defectClassLabels: Readonly<Record<string, string>> = {
  character_violation: "角色设定冲突",
  cyclic_dependency: "内容依赖成环",
  dangling_reference: "引用了不存在的内容",
  dead_quest: "任务无法完成",
  economy_collapse: "经济系统可能失衡",
  faction_violation: "阵营设定冲突",
  gacha_expectation_violation: "抽卡期望不符合规则",
  missing_drop_source: "掉落物缺少来源",
  non_monotonic_curve: "成长曲线不单调",
  prob_sum_ne_1: "概率总和不等于 100%",
  reward_out_of_range: "奖励数值超出范围",
  spoiler: "存在剧透风险",
  uniqueness_violation: "唯一性规则冲突",
  unreachable_target: "目标无法到达",
  unsatisfiable_completion: "完成条件互相冲突",
};

const bucketLabels: Readonly<Record<string, string>> = {
  agent: "智能助手",
  agent_cost: "智能助手用量",
  agent_latency: "模型响应",
  constraint_fp: "约束检查",
  deterministic: "确定性检查",
  deterministic_fp: "确定性检查",
  deterministic_runtime: "确定性检查耗时",
  external_development: "外部病例开发组",
  external_fp: "外部病例误报",
  external_verification: "外部病例验证组",
  hed: "人工编辑距离",
  llm_assisted: "AI 辅助检查",
  llm_assisted_fp: "AI 辅助误报",
  qa: "真人使用评测",
  simulation: "经济仿真",
};

const workloadLabels: Readonly<Record<string, string>> = {
  "external-hed": "外部病例人工编辑评测",
  "narrative-verification": "叙事检查验证",
  "playtest-flat": "直接试玩",
  "playtest-layered": "分层规划试玩",
  "playtest-memory-on": "启用记忆试玩",
  "repair-search": "自动修复搜索",
  "seeded-checker-sim-pipeline": "确定性检查与仿真",
};

function codedLabel(value: string | null | undefined, labels: Readonly<Record<string, string>>): ReactNode {
  if (!value) return "未记录";
  return <span title={`技术代码：${value}`}>{labels[value] ?? "扩展指标"}</span>;
}

function metricLabel(value: string): ReactNode {
  return codedLabel(value, metricLabels);
}

function defectClassLabel(value: string | null | undefined): ReactNode {
  return codedLabel(value, defectClassLabels);
}

function bucketLabel(value: string): ReactNode {
  return codedLabel(value, bucketLabels);
}

function displayInterval(value: string | null | undefined): string {
  if (!value) return "未测量";
  return value.replace(/^wilson95 /u, "95% 置信区间 ").replace(/^percentile-bootstrap95 /u, "95% 置信区间 ");
}

function displayEstimate(value: string | null | undefined): string {
  if (!value) return "未测量";
  return value
    .replace(/^mean /u, "平均 ")
    .replace(/ · median /gu, " · 中位数 ")
    .replace(/ · p95 /gu, " · 95 分位 ")
    .replace(/ · atomic_changes$/u, " · 次原子修改")
    .replace(/ · normalized_distance$/u, " · 标准化距离")
    .replace(/ · milliseconds$/u, " · 毫秒")
    .replace(/ · minutes$/u, " · 分钟")
    .replace(/ · fraction$/u, " · 比例")
    .replace(/ · tokens$/u, " · 模型用量单位");
}

function modelSnapshotLabel(model: string): string {
  if (model === "gpt-5.6-sol") return "GPT-5.6（本次实测版本）";
  if (model === "claude-opus-4-8") return "Claude Opus 4.8（历史实测版本）";
  return "已固定的实测模型版本";
}

function displayRate(value: number | null | undefined): string {
  return value === null || value === undefined ? "未测量" : percentFormatter.format(value);
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
  const labels: Record<MetricStatus, string> = {
    failed: "未达标",
    inconclusive: "暂无法下结论",
    measured: "已测量",
    pending: "等待测量",
    underpowered: "样本量不足",
  };
  return <span className={`u-status u-status--${tone}`}>{labels[status]}</span>;
}

function MissingChip({ children }: { children: ReactNode }) {
  return <span className="u-status u-status--suggestion gf-eval__missing-chip">{children}</span>;
}

function EvidenceInline({ evidence }: { evidence: EvidenceView }) {
  if (evidence.reference === null) {
    return (
      <div className="gf-eval__evidence-inline" data-evidence="missing">
        <MissingChip>证据缺失</MissingChip>
        <span>未绑定证据</span>
      </div>
    );
  }
  return (
    <div className="gf-eval__evidence-inline" data-evidence={evidence.status}>
      <span>已绑定证据</span>
      {evidence.status === "available" ? (
        <span className="u-status u-status--ok">可用</span>
      ) : (
        <MissingChip>证据缺失</MissingChip>
      )}
      <TechnicalDetails
        items={[
          { label: "Evidence reference", value: evidence.reference },
          ...(evidence.artifact
            ? [
                { label: "Evidence path", value: evidence.artifact.path },
                ...(evidence.artifact.sha256
                  ? [
                      {
                        label: "Evidence SHA-256",
                        value: evidence.artifact.sha256,
                      },
                    ]
                  : []),
              ]
            : []),
        ]}
        summary="查看证据技术信息"
      />
    </div>
  );
}

function ProtocolInline({ protocolId }: { protocolId: string | null }) {
  if (protocolId === null) {
    return (
      <div className="gf-eval__evidence-inline">
        <MissingChip>评测方案缺失</MissingChip>
      </div>
    );
  }
  return (
    <div className="gf-eval__evidence-inline">
      <span>固定评测方案</span>
      <TechnicalDetails
        items={[{ label: "Protocol ID", value: protocolId }]}
        summary="查看评测方案技术信息"
      />
    </div>
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
            <th scope="col">检查方式</th>
            <th scope="col">已评测 / 计划</th>
            <th scope="col">检出数</th>
            <th scope="col">检出率</th>
            <th scope="col">置信区间</th>
            <th scope="col">样本功效</th>
            <th scope="col">状态</th>
            <th scope="col">评测方案</th>
            <th scope="col">证据</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.partition}:${row.defectClass}`}>
              <th scope="row">{defectClassLabel(row.defectClass)}</th>
              <td>
                <span className={`gf-eval__partition gf-eval__partition--${row.partition}`}>
                  {partitionMeta[row.partition].label}
                </span>
              </td>
              <td>
                {integerFormatter.format(row.evaluatedN)} / {integerFormatter.format(row.plannedN)}
              </td>
              <td>{integerFormatter.format(row.metric.k)}</td>
              <td>{displayRate(row.metric.rate)}</td>
              <td>{displayInterval(row.interval)}</td>
              <td>
                {row.power ? (
                  <span className="gf-eval__power">
                    <span>
                      实际半宽 {numberFormatter.format(row.power.achieved_half_width)} / 目标半宽{" "}
                      {numberFormatter.format(row.power.target_half_width)}
                    </span>
                    <StatusChip status={row.power.status} />
                    {row.powerEvidence.reference !== row.evidence.reference && (
                      <EvidenceInline evidence={row.powerEvidence} />
                    )}
                  </span>
                ) : (
                  <MissingChip>证据缺失</MissingChip>
                )}
              </td>
              <td>
                <StatusChip status={row.metric.status} />
              </td>
              <td>
                <ProtocolInline protocolId={row.protocolId} />
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
            <th scope="col">指标</th>
            {includeDefectClass && <th scope="col">问题类型</th>}
            <th scope="col">评测分组</th>
            <th scope="col">已评测 / 计划</th>
            <th scope="col">成功数</th>
            <th scope="col">比率</th>
            <th scope="col">置信区间</th>
            <th scope="col">状态</th>
            <th scope="col">评测方案</th>
            <th scope="col">证据</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row.metric.name}:${row.metric.bucket}:${row.metric.defect_class ?? "all"}:${index}`}>
              <th scope="row">{metricLabel(row.metric.name)}</th>
              {includeDefectClass && (
                <td>{row.metric.defect_class ? defectClassLabel(row.metric.defect_class) : "全部问题"}</td>
              )}
              <td>{bucketLabel(row.metric.bucket)}</td>
              <td>
                {integerFormatter.format(row.evaluatedN)} / {integerFormatter.format(row.plannedN)}
              </td>
              <td>{integerFormatter.format(row.metric.k)}</td>
              <td>{displayRate(row.metric.rate)}</td>
              <td>{displayInterval(row.interval)}</td>
              <td>
                <StatusChip status={row.metric.status} />
              </td>
              <td>
                <ProtocolInline protocolId={row.protocolId} />
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
            <th scope="col">指标</th>
            <th scope="col">结果</th>
            <th scope="col">已评测 / 计划</th>
            <th scope="col">置信区间</th>
            <th scope="col">状态</th>
            <th scope="col">评测方案</th>
            <th scope="col">证据</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.metric.name}:${row.metric.bucket}`}>
              <th scope="row">{metricLabel(row.metric.name)}</th>
              <td>{displayEstimate(row.estimate)}</td>
              <td>
                {integerFormatter.format(row.evaluatedN)} / {integerFormatter.format(row.plannedN)}
              </td>
              <td>{displayInterval(row.interval)}</td>
              <td>
                <StatusChip status={row.metric.status} />
              </td>
              <td>
                <ProtocolInline protocolId={row.protocolId} />
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
          <strong>{displayEstimate(metric.estimate)}</strong>
          <p>{displayInterval(metric.interval)}</p>
          <div className="gf-eval__summary-meta">
            <StatusChip status={metric.metric.status} />
            <ProtocolInline protocolId={metric.protocolId} />
          </div>
          <EvidenceInline evidence={metric.evidence} />
        </>
      ) : (
        <>
          <MissingChip>证据缺失</MissingChip>
          <p>同名指标缺失或不唯一；页面没有猜测一条作为权威。</p>
        </>
      )}
    </article>
  );
}

function NarrativeProvenance({ report }: { report: BenchReportData }) {
  const narrative = report.narrative;
  return (
    <aside aria-label="叙事指标证据来源" className="gf-eval__provenance-ribbon">
      <div>
        <span>叙事指标证据</span>
        <strong>模型与评测材料已固定</strong>
      </div>
      <TechnicalDetails
        items={[
          {
            label: "Model snapshot",
            value: `${narrative.model_snapshot.provider} / ${narrative.model_snapshot.model} / ${narrative.model_snapshot.snapshot_tag}`,
          },
          { label: "Protocol SHA-256", value: narrative.protocol_sha256 },
          {
            label: "Corpus manifest SHA-256",
            value: narrative.corpus_manifest_sha256,
          },
        ]}
        summary="查看叙事指标技术信息"
      />
      <EvidenceInline evidence={evidenceView(report, narrative.evidence_ref)} />
    </aside>
  );
}

function ReportAuthority({ read }: { read: BenchReportRead }) {
  return (
    <section aria-label="报告权威" className="gf-eval__authority">
      <div>
        <span>报告来源</span>
        {read.artifactId ? <strong>已记录，可追溯</strong> : <strong>来源标识缺失，无法打开追溯记录</strong>}
      </div>
      <div>
        <span>评测样本</span>
        <strong>{integerFormatter.format(read.report.meta.corpus_size)} 个</strong>
      </div>
      <div>
        <span>报告生成时间</span>
        <strong>{read.report.meta.generated_at ?? "未记录"}</strong>
      </div>
      {read.artifactId && (
        <nav aria-label="质量报告追溯导航" className="gf-eval__authority-links">
          <a href={`/artifacts/${encodeURIComponent(read.artifactId)}`}>查看报告来源记录</a>
          <a href={`/artifacts/${encodeURIComponent(read.artifactId)}/lineage`}>查看报告血缘</a>
        </nav>
      )}
      <TechnicalDetails
        items={[
          ...(read.artifactId ? [{ label: "BenchReport Artifact ID", value: read.artifactId }] : []),
          { label: "Response ETag", value: read.etag },
          { label: "Report schema", value: read.report.schema_version },
          {
            label: "Report builder",
            value: read.report.meta.report_builder_version,
          },
          {
            label: "Seed",
            value: String(read.report.meta.seed ?? "not_applicable"),
          },
        ]}
        summary="查看报告技术信息"
      />
    </section>
  );
}

function BdrSection({ report }: { report: BenchReportData }) {
  const rows = selectBdrMetrics(report);
  return (
    <section aria-label="分缺陷类 BDR" className="gf-eval__section">
      <SectionHeading
        description="15 类问题分别展示样本量、检出数、检出率、置信区间、样本功效、评测方案与证据，不用一个总分掩盖差异。"
        icon={BarChart3}
        title="各类问题检出率"
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
                <span className="u-chip">{partitionRows.length} 类问题</span>
              </header>
              {partition === "llm-assisted" && <NarrativeProvenance report={report} />}
              {partition === "llm-assisted" && (
                <BinaryMetricTable
                  rows={[binaryMetricView(report, report.narrative.clean_fp)]}
                  title="叙事检查误报率"
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
        <MetricSummaryCard
          label="确定性检查误报率"
          metric={keyMetrics.oracleFp}
          note="检查器算法误报 · 独立口径"
        />
        <MetricSummaryCard
          label="约束检查误报率"
          metric={keyMetrics.constraintFp}
          note="约束质量误报 · 独立口径"
        />
        <MetricSummaryCard label="修复通过率" metric={keyMetrics.fixPassRate} note="复验 + 回归通过" />
      </section>

      <section aria-label="误报率与智能助手效果" className="gf-eval__section gf-eval__split-section">
        <SectionHeading
          description="固定规则检查、约束检查、AI 辅助检查和外部病例的误报分别统计；智能助手结果也独立展示。"
          icon={ShieldCheck}
          title="误报率与智能助手效果"
        />
        <article className="gf-eval__subsection">
          <header>
            <div>
              <h3>误报率指标</h3>
              <p>展示各类检查把正确内容误判为问题的比例。</p>
            </div>
          </header>
          <NarrativeProvenance report={report} />
          <BinaryMetricTable rows={selectFalsePositiveMetrics(report)} title="误报率指标" />
        </article>
        <article className="gf-eval__subsection">
          <header>
            <div>
              <h3>智能助手效果</h3>
              <p>对照自动修复与不同试玩方式的完成情况，扩展指标也会保留。</p>
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
              summary="图表只展示已有比率；样本量、置信区间、评测方案与证据见下表。"
              title="智能助手效果比率"
              valueFormatter={(value) => percentFormatter.format(value)}
              valueLabel="比率"
            />
          </div>
          <BinaryMetricTable rows={agentMetrics} title="智能助手效果" />
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
        description="开发组与独立验证组分开统计；即使检出率为 100%，样本不足时也会明确提示。"
        icon={Boxes}
        title="外部效度"
      />
      <dl className="gf-eval__fact-grid">
        <div>
          <dt>数据来源</dt>
          <dd>Endless Sky</dd>
        </div>
        <div>
          <dt>开源项目</dt>
          <dd>GitHub 上的 Endless Sky 开源项目</dd>
        </div>
        <div>
          <dt>有效病例 / 总病例</dt>
          <dd>
            {external.qualified_cases} / {external.total_cases}
          </dd>
        </div>
        <div>
          <dt>证据状态</dt>
          <dd>
            <EvidenceInline evidence={evidenceView(report, external.evidence_ref)} />
          </dd>
        </div>
      </dl>
      <TechnicalDetails
        items={[
          { label: "数据源代码", value: external.source_id },
          { label: "开源仓库地址", value: external.repository },
          { label: "Reader", value: external.reader_version },
          { label: "Adapter", value: external.adapter_version },
          { label: "Manifest SHA-256", value: external.manifest_sha256 },
          {
            label: "Mapping spec SHA-256",
            value: external.mapping_spec_sha256,
          },
        ]}
        summary="查看外部病例技术信息"
      />

      <article className="gf-eval__subsection">
        <header>
          <div>
            <h3>外部病例误报率</h3>
            <p>在开源游戏病例上独立观察检查器误报。</p>
          </div>
        </header>
        <BinaryMetricTable
          rows={[binaryMetricView(report, external.after_oracle_fp)]}
          title="外部病例误报率"
        />
      </article>
      <div className="gf-eval__two-column">
        <article className="gf-eval__subsection">
          <header>
            <div>
              <h3>开发组</h3>
              <p>用于开发期间观察能力，不作为独立验证结果。</p>
            </div>
          </header>
          <BinaryMetricTable
            includeDefectClass
            rows={external.development.map((metric) => binaryMetricView(report, metric))}
            title="外部病例开发组"
          />
        </article>
        <article className="gf-eval__subsection">
          <header>
            <div>
              <h3>独立验证组</h3>
              <p>与开发组隔离；样本量不足时会明确提示。</p>
            </div>
          </header>
          <BinaryMetricTable
            includeDefectClass
            rows={external.verification.map((metric) => binaryMetricView(report, metric))}
            title="外部病例独立验证组"
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
    <section aria-label="人工编辑距离" className="gf-eval__section">
      <SectionHeading
        description="编辑距离与人工处理结果分开呈现，并保留样本量、区间、评测方案和证据。"
        icon={BookOpenCheck}
        title="人工编辑距离"
      />
      <div className="gf-eval__model-ribbon">
        <span>实测模型版本</span>
        <strong>{modelSnapshotLabel(hed.model_snapshot.model)}</strong>
        <TechnicalDetails
          items={[
            {
              label: "模型技术版本",
              value: `${hed.model_snapshot.provider} / ${hed.model_snapshot.model} / ${hed.model_snapshot.snapshot_tag}`,
            },
          ]}
          summary="查看模型技术信息"
        />
        <EvidenceInline evidence={evidenceView(report, hed.evidence_ref)} />
      </div>
      <article className="gf-eval__subsection">
        <header>
          <div>
            <h3>编辑距离分布</h3>
            <p>原始修改量与标准化距离分别展示，避免互相替代。</p>
          </div>
        </header>
        <DistributionMetricTable
          rows={[
            distributionMetricView(report, hed.raw_distance),
            distributionMetricView(report, hed.normalized_distance),
          ]}
          title="编辑距离分布"
        />
      </article>
      <article className="gf-eval__subsection">
        <header>
          <div>
            <h3>人工处理结果</h3>
            <p>无需修改、需要编辑、无法使用和评测流程无效分别统计。</p>
          </div>
        </header>
        <div className="gf-eval__purposeful-chart gf-eval__purposeful-chart--ring">
          <RingChart
            data={dispositions.map((row) => ({
              label: metricLabels[row.metric.name] ?? "扩展结果",
              value: row.metric.k,
            }))}
            summary="按人工处理结果的病例数展示；精确区间、评测方案与证据见下表。"
            title="人工处理结果数量"
            valueLabel="病例数"
          />
        </div>
        <BinaryMetricTable rows={dispositions} title="人工处理结果" />
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
        description="八场实测使用隔离的本地测试工具；正确场次按实际操作时间计分，错误或超时场次按 8 分钟计分，原始操作时长仍完整保留。"
        icon={FileWarning}
        title="真人 QA"
      />
      <div className="gf-eval__qa-status">
        <div>
          <span>结论</span>
          <strong>{qa.conclusion === "savings" ? "工具能够节省策划时间" : qa.conclusion}</strong>
        </div>
        <div className="gf-eval__missing-list" aria-label="真人评测证据状态">
          {state.missingStates.includes("pending_human_evidence") && <MissingChip>等待真人证据</MissingChip>}
          {state.missingStates.includes("evidence_missing") && <MissingChip>证据缺失</MissingChip>}
          {hasNotMeasured && <MissingChip>尚未测量</MissingChip>}
          {state.acceptanceCode && <MissingChip>验收条件尚未满足</MissingChip>}
          {!missingHumanEvidence && <span className="u-status u-status--ok">真人证据可用</span>}
        </div>
        {qa.evidence_ref ? (
          <EvidenceInline evidence={state.evidenceStatus} />
        ) : (
          <div className="gf-eval__qa-binding">
            <MissingChip>证据缺失</MissingChip>
            <span>尚未绑定可核验的真人评测证据。</span>
          </div>
        )}
        {plannedCatalogEvidence && (
          <aside className="gf-eval__planned-evidence">
            <strong>有一条计划证据尚未绑定，不计入本次结果</strong>
          </aside>
        )}
        <TechnicalDetails
          items={[
            { label: "QA scope", value: qa.scope },
            { label: "Protocol SHA-256", value: qa.protocol_sha256 },
            ...(plannedCatalogEvidence
              ? [
                  {
                    label: "Planned evidence path",
                    value: plannedCatalogEvidence.path,
                  },
                  {
                    label: "Planned evidence schema",
                    value: plannedCatalogEvidence.schema_version,
                  },
                  ...(plannedCatalogEvidence.sha256
                    ? [
                        {
                          label: "Planned evidence SHA-256",
                          value: plannedCatalogEvidence.sha256,
                        },
                      ]
                    : []),
                ]
              : []),
          ]}
          summary="查看真人 QA 技术信息"
        />
      </div>

      <ScrollTable>
        <table aria-label="真人评测指标" className="gf-eval__metric-table">
          <thead>
            <tr>
              <th scope="col">指标</th>
              <th scope="col">结果</th>
              <th scope="col">已评测 / 计划</th>
              <th scope="col">置信区间</th>
              <th scope="col">状态</th>
              <th scope="col">评测方案</th>
              <th scope="col">证据</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const measured = row.view.estimate !== null;
              return (
                <tr key={row.name}>
                  <th scope="row">{metricLabel(row.name)}</th>
                  <td>{measured ? displayEstimate(row.view.estimate) : "等待真人证据"}</td>
                  <td>
                    {measured
                      ? `${integerFormatter.format(row.view.evaluatedN)} / ${integerFormatter.format(row.view.plannedN)}`
                      : "等待真人证据"}
                  </td>
                  <td>{measured ? displayInterval(row.view.interval) : "等待真人证据"}</td>
                  <td>
                    <StatusChip status={row.view.metric.status} />
                  </td>
                  <td>
                    <ProtocolInline protocolId={row.view.protocolId} />
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
        <dt>输入</dt>
        <dd>{integerFormatter.format(totals.input_tokens)}</dd>
      </div>
      <div>
        <dt>输出</dt>
        <dd>{integerFormatter.format(totals.output_tokens)}</dd>
      </div>
      <div>
        <dt>读取缓存</dt>
        <dd>{integerFormatter.format(totals.cache_read_tokens)}</dd>
      </div>
      <div>
        <dt>写入缓存</dt>
        <dd>{integerFormatter.format(totals.cache_write_tokens)}</dd>
      </div>
      <div>
        <dt>服务商报告总量</dt>
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
        description="模型服务商记录值与确定性检查耗时分开统计；固定回放速度不会冒充真实模型响应速度。"
        icon={Clock3}
        title="成本与延迟"
      />
      <aside className="gf-eval__latency-note">
        <Gauge aria-hidden="true" size={18} />
        <p>
          模型响应耗时来自录制时的服务商记录，不等于整条业务流程耗时，也不等于固定回放耗时。传输次数不完整时会明确提示。
        </p>
      </aside>

      <div className="gf-eval__workload-grid">
        {workloads.map((view) => {
          const workload = view.workload;
          return (
            <article
              aria-label={workloadLabels[workload.workload_id] ?? "扩展智能助手任务"}
              className="gf-eval__workload"
              data-testid="agent-workload"
              key={workload.workload_id}
            >
              <header>
                <div>
                  <p className="gf-eval__eyebrow">智能助手任务</p>
                  <h3>{workloadLabels[workload.workload_id] ?? "扩展任务"}</h3>
                </div>
                <EvidenceInline evidence={view.evidence} />
              </header>
              <dl className="gf-eval__workload-facts">
                <div>
                  <dt>服务商 / 模型 / 版本</dt>
                  <dd>
                    <strong>{modelSnapshotLabel(workload.model_snapshot.model)}</strong>
                    <TechnicalDetails
                      items={[
                        {
                          label: "模型技术版本",
                          value: `${workload.model_snapshot.provider} / ${workload.model_snapshot.model} / ${workload.model_snapshot.snapshot_tag}`,
                        },
                        { label: "任务代码", value: workload.workload_id },
                      ]}
                      summary="查看任务技术信息"
                    />
                  </dd>
                </div>
                <div>
                  <dt>已评测 / 计划样本</dt>
                  <dd>
                    {integerFormatter.format(workload.evaluated_n)} /{" "}
                    {integerFormatter.format(workload.planned_n)}
                  </dd>
                </div>
                <div>
                  <dt>业务请求 / 已记录调用</dt>
                  <dd>
                    {integerFormatter.format(workload.logical_requests)} /{" "}
                    {integerFormatter.format(workload.recorded_requests)}
                  </dd>
                </div>
                <div>
                  <dt>会话缓存复用</dt>
                  <dd>{integerFormatter.format(workload.session_cache_reuses)}</dd>
                </div>
                <div>
                  <dt>已知传输次数 / 重试次数</dt>
                  <dd>
                    {integerFormatter.format(view.transport.knownAttempts)} /{" "}
                    {integerFormatter.format(view.transport.knownRetries)}
                  </dd>
                </div>
                <div data-transport-state={view.transport.state}>
                  <dt>传输次数不完整的记录</dt>
                  <dd>
                    <strong>{integerFormatter.format(view.transport.unknownAttemptRecords)} 条</strong>
                    {view.transport.state === "has_unknown_records" && (
                      <span>已知计数不能当作完整的传输与重试次数。</span>
                    )}
                  </dd>
                </div>
                <div>
                  <dt>每个样本的模型用量</dt>
                  <dd>{displayEstimate(view.tokensPerSample.estimate)}</dd>
                </div>
                <div>
                  <dt>模型用量置信区间</dt>
                  <dd>{displayInterval(view.tokensPerSample.interval)}</dd>
                </div>
                <div>
                  <dt>模型响应耗时</dt>
                  <dd>{displayEstimate(view.requestLatency.estimate)}</dd>
                </div>
                <div>
                  <dt>响应耗时置信区间</dt>
                  <dd>{displayInterval(view.requestLatency.interval)}</dd>
                </div>
                <div>
                  <dt>货币成本</dt>
                  <dd>
                    <MissingChip>费用未测量</MissingChip>
                    <span>未绑定价格表，因此不会根据模型用量猜测费用。</span>
                  </dd>
                </div>
              </dl>
              <TokenTotals totals={workload.tokens} />
              <footer>
                <span>
                  模型用量评测方案 · <ProtocolInline protocolId={view.tokensPerSample.protocolId} />
                </span>
                <span>
                  响应耗时评测方案 · <ProtocolInline protocolId={view.requestLatency.protocolId} />
                </span>
              </footer>
            </article>
          );
        })}
      </div>

      <article aria-label="确定性运行时" className="gf-eval__deterministic-runtime">
        <header>
          <div>
            <p className="gf-eval__eyebrow">与模型耗时分开统计</p>
            <h3>确定性运行时</h3>
          </div>
          <EvidenceInline evidence={evidenceView(report, deterministic.evidence_ref)} />
        </header>
        <dl className="gf-eval__fact-grid">
          <div>
            <dt>评测任务</dt>
            <dd>{workloadLabels[deterministic.workload_id] ?? "确定性检查任务"}</dd>
          </div>
          <div>
            <dt>每个样本耗时</dt>
            <dd>{displayEstimate(deterministicRuntime.estimate)}</dd>
          </div>
          <div>
            <dt>置信区间</dt>
            <dd>{displayInterval(deterministicRuntime.interval)}</dd>
          </div>
          <div>
            <dt>准备耗时</dt>
            <dd>{numberFormatter.format(deterministic.setup_ms)} 毫秒</dd>
          </div>
          <div>
            <dt>评测方案</dt>
            <dd>
              <ProtocolInline protocolId={deterministicRuntime.protocolId} />
            </dd>
          </div>
        </dl>
        <TechnicalDetails
          items={[
            {
              label: "Environment SHA-256",
              value: deterministic.environment_sha256,
            },
          ]}
          summary="查看运行环境技术信息"
        />
      </article>
    </section>
  );
}

function CatalogSection({ report }: { report: BenchReportData }) {
  return (
    <section aria-label="版本与证据" className="gf-eval__section">
      <SectionHeading
        description="文件路径只作为报告中的证据来源展示，不会被浏览器误当成可访问链接；技术摘要仍完整保留。"
        icon={Database}
        title="版本与证据目录"
      />
      <details className="gf-technical-details gf-eval__technical-catalog">
        <summary>查看版本与证据技术目录</summary>
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
      </details>
    </section>
  );
}

function EvalError({ error, onRetry }: { error: Error; onRetry(): void }) {
  const unavailableReport =
    error instanceof ApiProblemError && error.problem.code === "dependency_unavailable";
  return (
    <div className="gf-eval__error">
      {unavailableReport ? (
        <>
          <StatePanel
            description="当前环境没有返回可验证的质量评测报告。请稍后重试；若持续出现，请联系管理员检查报告绑定与存储状态。"
            state="error"
            title="质量报告暂时不可读取"
          />
          <TechnicalDetails
            items={[
              { label: "问题代码", value: error.problem.code },
              { label: "请求标识", value: error.problem.request_id },
              ...(error.problem.trace_id ? [{ label: "追踪标识", value: error.problem.trace_id }] : []),
            ]}
            summary="查看报告读取技术信息"
          />
        </>
      ) : error instanceof ApiProblemError ? (
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
          <p className="gf-eval__kicker">产品质量报告</p>
          <h1>质量评测</h1>
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
          <p className="gf-eval__kicker">产品质量报告</p>
          <h1>质量评测</h1>
          <p>
            把检出、误报、修复、人工编辑、真人 QA
            与成本证据放在同一份可追溯报告里；缺失证据保留缺失，不用漂亮总分遮盖。
          </p>
        </div>
        <div aria-hidden="true" className="gf-eval__hero-mark">
          <BarChart3 size={30} />
          <span>评测样本</span>
          <strong>{integerFormatter.format(report.meta.corpus_size)}</strong>
          <small>个测试病例</small>
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
