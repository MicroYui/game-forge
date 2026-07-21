import { FlaskConical, MessageSquareWarning, ShieldCheck } from "lucide-react";
import { useId } from "react";

import type { components } from "../../api/generated/openapi";
import { CopyableText } from "../tables";
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
  critical: "严重 · critical",
  major: "主要 · major",
  minor: "次要 · minor",
} as const;

const oracleMeta = {
  deterministic: { icon: ShieldCheck, label: "确定性预言机" },
  "llm-assisted": { icon: MessageSquareWarning, label: "LLM 建议（需人确认）" },
  simulation: { icon: FlaskConical, label: "仿真证据（描述性）" },
} as const;

const statusLabels = {
  accepted_risk: "已接受风险 · accepted_risk",
  confirmed: "已确认 · confirmed",
  dismissed: "已驳回 · dismissed",
  fixed: "已修复 · fixed",
  unproven: "未证明 · unproven",
} as const;

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
        <h3 id={titleId}>{finding.payload.message}</h3>
        {detailHref && (
          <a className="gf-finding-card__detail-link" href={detailHref}>
            查看 exact Finding 修订
          </a>
        )}
      </header>

      <dl className="gf-finding-card__facts">
        <div>
          <dt>Finding ID</dt>
          <dd>
            <CopyableText copyLabel="复制 Finding ID" value={finding.finding_id} />
          </dd>
        </div>
        <div>
          <dt>修订</dt>
          <dd>不可变修订 {finding.revision}</dd>
        </div>
        <div>
          <dt>缺陷类别</dt>
          <dd>{finding.payload.defect_class}</dd>
        </div>
        <div>
          <dt>精确快照</dt>
          <dd>
            <CopyableText copyLabel="复制快照 ID" value={finding.payload.snapshot_id} />
          </dd>
        </div>
        <div>
          <dt>source_ref</dt>
          <dd>{sourceRef ? sourceRefLabel(sourceRef) : "未提供"}</dd>
        </div>
        <div>
          <dt>生产 Run</dt>
          <dd>
            <CopyableText copyLabel="复制生产 Run ID" value={finding.payload.producer_run_id} />
          </dd>
        </div>
        {authorityBinding && (
          <>
            <div>
              <dt>Run link ordinal</dt>
              <dd>
                attempt {authorityBinding.attemptNo} · ordinal {authorityBinding.ordinal}
              </dd>
            </div>
            <div>
              <dt>Finding digest</dt>
              <dd>
                <CopyableText copyLabel="复制 Finding digest" value={authorityBinding.findingDigest} />
              </dd>
            </div>
            <div>
              <dt>Evidence Artifact</dt>
              <dd>
                <a href={`/artifacts/${encodeURIComponent(authorityBinding.evidenceArtifactId)}`}>
                  {authorityBinding.evidenceArtifactId}
                </a>
              </dd>
            </div>
          </>
        )}
      </dl>

      <section className="gf-finding-card__repro" aria-label="最小复现">
        <h4>最小复现</h4>
        <pre tabIndex={0}>{jsonText(finding.payload.minimal_repro ?? {})}</pre>
      </section>

      <section className="gf-finding-card__evidence" aria-label="Finding evidence payload">
        <h4>证据 payload</h4>
        {finding.payload.evidence === undefined ? (
          <p className="gf-finding-card__empty">未提供 evidence payload</p>
        ) : (
          <pre tabIndex={0}>{jsonText(finding.payload.evidence)}</pre>
        )}
      </section>
    </article>
  );
}
