import { FlaskConical, MessageSquareWarning, ShieldCheck } from "lucide-react";
import { useId } from "react";

import type { components } from "../../api/generated/openapi";
import { TechnicalDetails } from "../identity";
import "./evidence.css";

type FindingRevision = components["schemas"]["FindingRevisionV1"];
type SourceRef = components["schemas"]["SourceRef"];

export interface FindingCardAuthorityBinding {
  attemptNo: number;
  evidenceArtifactId: string;
  findingDigest: string;
  ordinal: number;
}

const severityLabels = {
  critical: "严重",
  major: "重要",
  minor: "一般",
} as const;

const oracleMeta = {
  deterministic: { icon: ShieldCheck, label: "确定性预言机" },
  "llm-assisted": { icon: MessageSquareWarning, label: "AI 建议（需人工确认）" },
  simulation: { icon: FlaskConical, label: "仿真证据（描述性）" },
} as const;

const statusLabels = {
  accepted_risk: "已接受风险",
  confirmed: "已确认",
  dismissed: "已忽略",
  fixed: "已修复",
  unproven: "未证明",
} as const;

const defectClassLabels: Readonly<Record<string, string>> = {
  dead_quest: "任务无法完成",
  economy_collapse: "经济系统可能失衡",
  playtest_incomplete: "试玩未完成",
  quest_dead_end: "任务流程存在死路",
  reward_out_of_range: "数值超出允许范围",
  unreachable_target: "目标无法到达",
};

export function findingDisplayMessage(defectClass: string, message: string): string {
  if (/\p{Script=Han}/u.test(message)) return message;
  const label = defectClassLabels[defectClass] ?? "内容规则问题";
  if (/requires navigation ground truth/iu.test(message)) {
    return `${label}：当前内容缺少导航依据，暂时无法完成判断。`;
  }
  return `${label}：检查器发现了需要策划确认的问题。`;
}

function isSourceRef(value: unknown): value is SourceRef {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.adapter === "string" &&
    typeof candidate.file === "string" &&
    (candidate.sheet === undefined || candidate.sheet === null || typeof candidate.sheet === "string") &&
    (candidate.row === undefined || candidate.row === null || typeof candidate.row === "number") &&
    (candidate.column === undefined || candidate.column === null || typeof candidate.column === "string")
  );
}

function readSourceRef(finding: FindingRevision): SourceRef | null {
  const candidate = finding.payload.minimal_repro?.source_ref;
  return isSourceRef(candidate) ? candidate : null;
}

function sourceRefLabel(sourceRef: SourceRef): string {
  const parts = [`${sourceRef.adapter} · ${sourceRef.file}`];
  if (sourceRef.sheet) parts.push(sourceRef.sheet);
  if (sourceRef.row !== null && sourceRef.row !== undefined) parts.push(`第 ${sourceRef.row} 行`);
  if (sourceRef.column) parts.push(sourceRef.column);
  return parts.join(" / ");
}

function jsonText(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "null";
}

export function FindingCard({
  authorityBinding,
  detailHref,
  finding,
}: {
  authorityBinding?: FindingCardAuthorityBinding;
  detailHref?: string;
  finding: FindingRevision;
}) {
  const titleId = useId();
  const sourceRef = readSourceRef(finding);
  const oracle = oracleMeta[finding.payload.oracle_type];
  const OracleIcon = oracle.icon;
  const displayMessage = findingDisplayMessage(finding.payload.defect_class, finding.payload.message);
  const technicalItems = [
    { label: "问题 ID", value: finding.finding_id },
    { label: "内容快照 ID", value: finding.payload.snapshot_id },
    { label: "生成运行 ID", value: finding.payload.producer_run_id },
    { label: "缺陷类别代码", value: finding.payload.defect_class },
    { label: "问题数据版本", value: finding.payload.payload_schema_version },
    { label: "修订数据版本", value: finding.revision_schema_version },
    ...(displayMessage === finding.payload.message
      ? []
      : [{ label: "检查器原始说明", value: finding.payload.message }]),
    ...(authorityBinding
      ? [
          { label: "问题摘要", value: authorityBinding.findingDigest },
          { label: "证据记录 ID", value: authorityBinding.evidenceArtifactId },
        ]
      : []),
  ];

  return (
    <article
      aria-labelledby={titleId}
      className="gf-finding-card"
      data-oracle={finding.payload.oracle_type}
      data-severity={finding.payload.severity}
    >
      <header className="gf-finding-card__header">
        <div className="gf-finding-card__badges">
          <span className="u-status" data-severity-label={finding.payload.severity}>
            {severityLabels[finding.payload.severity]}
          </span>
          <span className="u-status" data-oracle-label={finding.payload.oracle_type}>
            <OracleIcon aria-hidden="true" size={14} />
            {oracle.label}
          </span>
          <span className="u-status" data-status-label={finding.payload.status}>
            {statusLabels[finding.payload.status]}
          </span>
        </div>
        <h3 id={titleId}>{displayMessage}</h3>
        {detailHref && (
          <a className="gf-finding-card__detail-link" href={detailHref}>
            查看此问题的历史版本
          </a>
        )}
      </header>

      <dl className="gf-finding-card__facts">
        <div>
          <dt>问题版本</dt>
          <dd>第 {finding.revision} 版</dd>
        </div>
        <div>
          <dt>问题类型</dt>
          <dd>{defectClassLabels[finding.payload.defect_class] ?? "其他规则问题"}</dd>
        </div>
        <div>
          <dt>来源位置</dt>
          <dd>{sourceRef ? sourceRefLabel(sourceRef) : "未提供"}</dd>
        </div>
        {authorityBinding && (
          <>
            <div>
              <dt>检查位置</dt>
              <dd>
                第 {authorityBinding.attemptNo} 次检查 · 第 {authorityBinding.ordinal} 条结果
              </dd>
            </div>
            <div>
              <dt>检查证据</dt>
              <dd>
                <a href={`/artifacts/${encodeURIComponent(authorityBinding.evidenceArtifactId)}`}>
                  查看检查证据
                </a>
              </dd>
            </div>
          </>
        )}
      </dl>
      <TechnicalDetails items={technicalItems} summary="查看问题技术信息" />

      <section className="gf-finding-card__repro" aria-label="最小复现">
        <h4>最小复现</h4>
        <p>系统已保存用于重复验证此问题的输入。</p>
        <details>
          <summary>查看原始复现数据</summary>
          <pre tabIndex={0}>{jsonText(finding.payload.minimal_repro ?? {})}</pre>
        </details>
      </section>

      <section className="gf-finding-card__evidence" aria-label="Finding evidence payload">
        <h4>证据 payload</h4>
        {finding.payload.evidence === undefined ? (
          <p className="gf-finding-card__empty">未提供检查证据数据</p>
        ) : (
          <details>
            <summary>查看原始证据数据</summary>
            <pre tabIndex={0}>{jsonText(finding.payload.evidence)}</pre>
          </details>
        )}
      </section>
    </article>
  );
}
