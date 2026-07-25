import { useQuery } from "@tanstack/react-query";
import { BadgeCheck, CircleDotDashed, FileCheck2, GitBranch, Route, ShieldQuestion } from "lucide-react";
import { useState } from "react";

import type { components } from "../../api/generated/openapi";
import { ApiProblemError } from "../../api/problem";
import { TechnicalDetails } from "../../components/identity";
import { CopyableText, CursorTable, type CursorTableColumn } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  specWorkflowApi,
  type ConstraintSnapshotView,
  type SpecWorkflowApi,
  type SubjectApprovalBindingView,
} from "./api";
import { ConstraintSummaryList } from "./ConstraintSummary";
import "./specs.css";

export type ConstraintSnapshotApi = Pick<SpecWorkflowApi, "getConstraintSnapshot"> &
  Partial<Pick<SpecWorkflowApi, "listRefHistory">>;

type ApprovalStatus = SubjectApprovalBindingView["approval_status"];
type RefValue = components["schemas"]["RefValue"];

/** Server-evidence view state; never derive this from the snapshot Artifact kind. */
export type ConstraintSnapshotAuthorityEvidence =
  | {
      approvalId: string;
      approvalStatus: ApprovalStatus;
      evidenceKind: "approval_target";
      targetArtifactId: string;
      workflowRevision: number;
    }
  | {
      evidenceKind: "ref_history";
      refName: string;
      refValue: RefValue;
    }
  | {
      currentRefValue: RefValue;
      evidenceKind: "historical_ref";
      historicalRefValue: RefValue;
      refName: string;
    }
  | {
      evidenceKind: "unresolved";
      reason: string;
    };

interface ConstraintPreview {
  assert: string;
  id: string;
  kind: "structural" | "numeric" | "narrative";
  note: string | null;
  oracle: "deterministic" | "llm-assisted" | "mixed";
  severity: "critical" | "major" | "minor";
}

const kinds = new Set<ConstraintPreview["kind"]>(["structural", "numeric", "narrative"]);
const oracles = new Set<ConstraintPreview["oracle"]>(["deterministic", "llm-assisted", "mixed"]);
const severities = new Set<ConstraintPreview["severity"]>(["critical", "major", "minor"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function constraintPreviews(snapshot: ConstraintSnapshotView): ConstraintPreview[] | null {
  if (snapshot.artifact.payload_schema_id !== "constraint-snapshot@1") return null;
  const previews: ConstraintPreview[] = [];
  const ids = new Set<string>();
  for (const value of snapshot.constraints) {
    if (!isRecord(value)) return null;
    const id = value.id;
    const assertion = value.assert;
    const grammar = value.dsl_grammar_version;
    const kind = value.kind;
    const oracle = value.oracle;
    const severity = value.severity;
    const note = value.note;
    if (
      typeof id !== "string" ||
      !id ||
      ids.has(id) ||
      typeof assertion !== "string" ||
      !assertion ||
      typeof grammar !== "string" ||
      grammar !== snapshot.dsl_grammar_version ||
      !kinds.has(kind as ConstraintPreview["kind"]) ||
      !oracles.has(oracle as ConstraintPreview["oracle"]) ||
      !severities.has(severity as ConstraintPreview["severity"]) ||
      (note !== undefined && note !== null && typeof note !== "string")
    ) {
      return null;
    }
    ids.add(id);
    previews.push({
      assert: assertion,
      id,
      kind: kind as ConstraintPreview["kind"],
      note: typeof note === "string" ? note : null,
      oracle: oracle as ConstraintPreview["oracle"],
      severity: severity as ConstraintPreview["severity"],
    });
  }
  return previews;
}

const constraintColumns: readonly CursorTableColumn<ConstraintPreview>[] = [
  {
    header: "Constraint ID",
    id: "id",
    render: (item) => <CopyableText copyLabel="复制 Constraint ID" value={item.id} />,
  },
  {
    header: "类别",
    id: "kind",
    render: (item) => <code>{item.kind}</code>,
  },
  {
    header: "断言",
    id: "assert",
    render: (item) => (
      <div className="gf-specs__constraint-assertion">
        <code>{item.assert}</code>
        {item.note && <span>{item.note}</span>}
      </div>
    ),
  },
  {
    header: "Oracle",
    id: "oracle",
    render: (item) => <code>{item.oracle}</code>,
  },
  {
    header: "Severity",
    id: "severity",
    render: (item) => (
      <span
        className={`u-status u-status--${
          item.severity === "critical" ? "danger" : item.severity === "major" ? "suggestion" : "info"
        }`}
      >
        {item.severity}
      </span>
    ),
  },
];

function AuthorityPanel({
  artifactId,
  evidence,
}: {
  artifactId: string;
  evidence: ConstraintSnapshotAuthorityEvidence;
}) {
  if (evidence.evidenceKind === "ref_history" && evidence.refValue.artifact_id === artifactId) {
    return (
      <section className="gf-specs__authority" data-authority="authoritative">
        <BadgeCheck aria-hidden="true" size={22} />
        <div>
          <p className="gf-specs__authority-label">当前使用中</p>
          <h2>这是当前生效的规则版本</h2>
          <p>版本历史确认当前内容正在使用第 {evidence.refValue.revision} 版规则。</p>
          <a href={`/refs/${encodeURIComponent(evidence.refName)}/history`}>查看规则版本历史</a>
        </div>
      </section>
    );
  }

  if (evidence.evidenceKind === "approval_target" && evidence.targetArtifactId === artifactId) {
    return (
      <section className="gf-specs__authority" data-authority="candidate">
        <CircleDotDashed aria-hidden="true" size={22} />
        <div>
          <p className="gf-specs__authority-label">待发布</p>
          <h2>这是候选规则，尚未应用</h2>
          <p>规则仍在审批流程中，不会影响当前正式内容。</p>
          <a href={`/approvals/${encodeURIComponent(evidence.approvalId)}`}>查看审批进度</a>
        </div>
      </section>
    );
  }

  if (evidence.evidenceKind === "historical_ref" && evidence.historicalRefValue.artifact_id === artifactId) {
    return (
      <section className="gf-specs__authority" data-authority="historical">
        <GitBranch aria-hidden="true" size={22} />
        <div>
          <p className="gf-specs__authority-label">历史版本</p>
          <h2>这是曾发布过的历史约束</h2>
          <p>
            这是第 {evidence.historicalRefValue.revision} 版；当前已经更新到第{" "}
            {evidence.currentRefValue.revision} 版。
          </p>
          <a href={`/refs/${encodeURIComponent(evidence.refName)}/history`}>查看完整版本历史</a>
        </div>
      </section>
    );
  }

  const reason =
    evidence.evidenceKind === "unresolved"
      ? evidence.reason
      : "提供的证据指向另一 Artifact，已拒绝据此标记 candidate 或 authority。";
  return (
    <section className="gf-specs__authority" data-authority="unresolved">
      <ShieldQuestion aria-hidden="true" size={22} />
      <div>
        <p className="gf-specs__authority-label">状态未知</p>
        <h2>无法确认这版规则是否生效</h2>
        <p>系统没有取得完整且匹配的发布记录，因此不会把这版规则标记为当前生效。</p>
        <details>
          <summary>查看核验技术原因</summary>
          <p>{reason}</p>
        </details>
      </div>
    </section>
  );
}

const authoritySteps = [
  "策划确认并修订草案",
  "系统自动编译并检查",
  "提交审批",
  "由另一位负责人批准",
  "发布并写入版本历史",
] as const;

export function ConstraintSnapshotPage({
  api = specWorkflowApi,
  artifactId,
  authorityEvidence = {
    evidenceKind: "unresolved",
    reason: "未提供批准目标或 ref 历史证据。",
  },
  refName = null,
}: {
  api?: ConstraintSnapshotApi;
  artifactId: string;
  authorityEvidence?: ConstraintSnapshotAuthorityEvidence;
  refName?: string | null;
}) {
  const [refInput, setRefInput] = useState(refName ?? "");
  const detail = useQuery({
    queryFn: () => api.getConstraintSnapshot(artifactId),
    queryKey: ["constraint-snapshot", artifactId],
    retry: false,
  });
  const refEvidence = useQuery({
    enabled: refName !== null,
    queryFn: async (): Promise<ConstraintSnapshotAuthorityEvidence> => {
      if (!refName || !api.listRefHistory) {
        return {
          evidenceKind: "unresolved",
          reason: "未提供可读取的 exact ref history。",
        };
      }
      const entries = [] as RefValue[];
      const seen = new Set<string>();
      let cursor: string | null = null;
      let readSnapshotId: string | null = null;
      for (let pageCount = 0; pageCount < 256; pageCount += 1) {
        const page = await api.listRefHistory(refName, cursor);
        if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
          throw new Error("Constraint ref history changed read snapshot.");
        }
        readSnapshotId = page.read_snapshot_id;
        entries.push(...page.items.map((entry) => entry.value));
        const next = page.next_cursor ?? null;
        if (next === null) {
          if (entries.length === 0) throw new Error("Constraint ref history is empty.");
          const current = entries.reduce((latest, value) =>
            value.revision > latest.revision ? value : latest,
          );
          if (current.artifact_id === artifactId) {
            return { evidenceKind: "ref_history", refName, refValue: current };
          }
          const historical = entries.find((value) => value.artifact_id === artifactId);
          return historical
            ? {
                currentRefValue: current,
                evidenceKind: "historical_ref",
                historicalRefValue: historical,
                refName,
              }
            : {
                evidenceKind: "unresolved",
                reason: `Ref ${refName} 从未指向此 Artifact。`,
              };
        }
        if (seen.has(next)) throw new Error("Constraint ref history returned a cursor cycle.");
        seen.add(next);
        cursor = next;
      }
      throw new Error("Constraint ref history exceeded its bounded page count.");
    },
    queryKey: ["constraint-snapshot", artifactId, "authority", refName],
    retry: false,
  });

  if (detail.isPending) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          description="正在读取规则内容和发布状态。"
          headingLevel={1}
          state="loading"
          title="正在读取规则版本"
        />
      </div>
    );
  }

  if (detail.isError) {
    return (
      <div className="gf-page gf-specs">
        <header className="gf-page-header">
          <p className="gf-specs__kicker">游戏规则版本</p>
          <h1>规则版本详情</h1>
        </header>
        {detail.error instanceof ApiProblemError ? (
          <ProblemPanel problem={detail.error.problem} />
        ) : (
          <StatePanel
            action={
              <button className="gf-secondary-button" onClick={() => void detail.refetch()} type="button">
                重试
              </button>
            }
            description="规则版本读取失败；未展示底层异常内容。"
            state="error"
            title="无法读取规则版本"
          />
        )}
      </div>
    );
  }

  const snapshot = detail.data;
  const constraints = constraintPreviews(snapshot);
  const resolvedAuthority = refName === null ? authorityEvidence : (refEvidence.data ?? authorityEvidence);

  return (
    <div className="gf-page gf-specs gf-constraint-snapshot">
      <nav aria-label="规则版本导航" className="gf-specs__back-nav">
        <a href="/specs">返回内容工作台</a>
        <a href={`/artifacts/${encodeURIComponent(snapshot.artifact.artifact_id)}`}>查看来源记录</a>
      </nav>

      <header className="gf-specs__hero gf-specs__hero--detail">
        <div>
          <p className="gf-specs__kicker">游戏规则版本</p>
          <h1>规则版本详情</h1>
          <p className="gf-specs__lede">查看这一版有哪些规则，以及它是待审批、当前生效还是历史版本。</p>
        </div>
        <span className="gf-specs__status-mark">
          <FileCheck2 aria-hidden="true" size={17} />
          已固定规则
        </span>
      </header>

      {refEvidence.isPending && refName !== null ? (
        <StatePanel description="正在读取完整版本历史。" state="loading" title="正在核对发布状态" />
      ) : refEvidence.isError ? (
        <StatePanel
          description="版本历史读取不完整；为避免误导，页面不会把这版规则标记为当前生效。"
          state="error"
          title="无法核对发布状态"
        />
      ) : (
        <AuthorityPanel artifactId={snapshot.artifact.artifact_id} evidence={resolvedAuthority} />
      )}

      {refName === null && (
        <details className="gf-specs__authority-check">
          <summary>高级：核验版本来源</summary>
          <section aria-label="核验规则发布位置">
            <div>
              <strong>按发布位置核对完整历史</strong>
              <p>仅在审计或排障时使用；日常查看无需填写。</p>
            </div>
            <label>
              发布位置名称
              <input onChange={(event) => setRefInput(event.target.value)} value={refInput} />
            </label>
            <a
              aria-disabled={!refInput.trim()}
              className="gf-secondary-button"
              href={refInput.trim() ? `?ref=${encodeURIComponent(refInput.trim())}` : undefined}
            >
              核对版本历史
            </a>
          </section>
        </details>
      )}

      <dl className="gf-specs__facts" aria-label="规则版本概览">
        <div>
          <dt>规则数量</dt>
          <dd>{constraints?.length ?? "无法读取"} 条</dd>
        </div>
      </dl>
      <TechnicalDetails
        items={[
          { label: "Artifact ID", value: snapshot.artifact.artifact_id },
          {
            label: "Constraint snapshot ID",
            value: snapshot.artifact.version_tuple.constraint_snapshot_id ?? "未绑定",
          },
          {
            label: "Payload schema",
            value: snapshot.artifact.payload_schema_id ?? "未公开",
          },
          { label: "DSL grammar", value: snapshot.dsl_grammar_version },
        ]}
        summary="查看规则版本技术信息"
      />

      <section className="gf-specs__authority-path" aria-labelledby="authority-path-title">
        <header>
          <Route aria-hidden="true" size={19} />
          <div>
            <h2 id="authority-path-title">规则发布流程</h2>
            <p>这是发布步骤说明；这版是否生效以上方状态为准。</p>
          </div>
        </header>
        <ol>
          {authoritySteps.map((step, index) => (
            <li key={step}>
              <span>{index + 1}</span>
              {step}
            </li>
          ))}
        </ol>
      </section>

      <section className="gf-specs__workspace-section" aria-labelledby="constraint-list-title">
        <header className="gf-specs__section-heading">
          <GitBranch aria-hidden="true" size={19} />
          <div>
            <h2 id="constraint-list-title">本版规则</h2>
            <p>先显示策划可读摘要；完整技术字段可按需展开。</p>
          </div>
        </header>
        {constraints === null ? (
          <StatePanel
            description="这版数据与当前阅读器不兼容；为避免误读，原始内容不会直接显示。"
            state="error"
            title="无法安全读取规则内容"
          />
        ) : constraints.length === 0 ? (
          <StatePanel
            description="这一版没有保存任何规则；页面不会自行补造内容。"
            state="empty"
            title="这一版没有规则"
          />
        ) : (
          <div className="gf-constraint-snapshot__content">
            <ConstraintSummaryList values={snapshot.constraints} />
            <details>
              <summary>查看技术字段表</summary>
              <CursorTable
                caption="约束条目（快照载荷）"
                columns={constraintColumns}
                getRowKey={(item) => item.id}
                items={constraints}
                toolbar={<span>{constraints.length} 条 exact payload entry</span>}
              />
            </details>
          </div>
        )}
      </section>
    </div>
  );
}
