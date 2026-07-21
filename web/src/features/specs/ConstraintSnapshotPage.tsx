import { useQuery } from "@tanstack/react-query";
import { BadgeCheck, CircleDotDashed, FileCheck2, GitBranch, Route, ShieldQuestion } from "lucide-react";

import type { components } from "../../api/generated/openapi";
import { ApiProblemError } from "../../api/problem";
import { CopyableText, CursorTable, type CursorTableColumn } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  specWorkflowApi,
  type ConstraintSnapshotView,
  type SpecWorkflowApi,
  type SubjectApprovalBindingView,
} from "./api";
import "./specs.css";

export type ConstraintSnapshotApi = Pick<SpecWorkflowApi, "getConstraintSnapshot">;

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
          <p className="gf-specs__authority-label">Authority</p>
          <h2>已由 ref 历史证明权威</h2>
          <p>Exact ref history 指向此 Artifact；revision {evidence.refValue.revision}。</p>
          <a href={`/refs/${encodeURIComponent(evidence.refName)}/history`}>检查 ref 历史</a>
        </div>
      </section>
    );
  }

  if (evidence.evidenceKind === "approval_target" && evidence.targetArtifactId === artifactId) {
    return (
      <section className="gf-specs__authority" data-authority="candidate">
        <CircleDotDashed aria-hidden="true" size={22} />
        <div>
          <p className="gf-specs__authority-label">Candidate</p>
          <h2>候选快照 · 尚未证明权威</h2>
          <p>
            批准状态 {evidence.approvalStatus} 仍不等于 ref 已发布；workflow revision{" "}
            {evidence.workflowRevision}。
          </p>
          <a href={`/approvals/${encodeURIComponent(evidence.approvalId)}`}>打开批准目标</a>
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
        <p className="gf-specs__authority-label">Unresolved</p>
        <h2>权威状态未证明</h2>
        <p>{reason}</p>
      </div>
    </section>
  );
}

const authoritySteps = [
  "人工修订 proposal",
  "确定性 compile / validate",
  "提交审批",
  "另一位 human 批准",
  "publish + constraint ref history",
] as const;

export function ConstraintSnapshotPage({
  api = specWorkflowApi,
  artifactId,
  authorityEvidence = {
    evidenceKind: "unresolved",
    reason: "未提供批准目标或 ref 历史证据。",
  },
}: {
  api?: ConstraintSnapshotApi;
  artifactId: string;
  authorityEvidence?: ConstraintSnapshotAuthorityEvidence;
}) {
  const detail = useQuery({
    queryFn: () => api.getConstraintSnapshot(artifactId),
    queryKey: ["constraint-snapshot", artifactId],
    retry: false,
  });

  if (detail.isPending) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          description="正在读取 constraint_snapshot Artifact 与显式权威证据。"
          headingLevel={1}
          state="loading"
          title="正在读取约束快照"
        />
      </div>
    );
  }

  if (detail.isError) {
    return (
      <div className="gf-page gf-specs">
        <header className="gf-page-header">
          <p className="gf-specs__kicker">Constraint snapshot · Detail</p>
          <h1>约束快照</h1>
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
            description="约束快照读取失败；未展示底层异常内容。"
            state="error"
            title="无法读取约束快照"
          />
        )}
      </div>
    );
  }

  const snapshot = detail.data;
  const constraints = constraintPreviews(snapshot);

  return (
    <div className="gf-page gf-specs gf-constraint-snapshot">
      <nav aria-label="约束快照导航" className="gf-specs__back-nav">
        <a href="/specs">返回规格工作台</a>
        <a href={`/artifacts/${encodeURIComponent(snapshot.artifact.artifact_id)}`}>查看安全 Artifact 摘要</a>
      </nav>

      <header className="gf-specs__hero gf-specs__hero--detail">
        <div>
          <p className="gf-specs__kicker">Constraint snapshot · Immutable Artifact</p>
          <h1>约束快照</h1>
          <p className="gf-specs__lede">
            快照内容与权威状态分开呈现：candidate 来自批准目标，authority 只来自 exact publish/ref 历史证据。
          </p>
        </div>
        <span className="gf-specs__status-mark">
          <FileCheck2 aria-hidden="true" size={17} />
          {snapshot.dsl_grammar_version}
        </span>
      </header>

      <AuthorityPanel artifactId={snapshot.artifact.artifact_id} evidence={authorityEvidence} />

      <dl className="gf-specs__facts" aria-label="约束快照身份">
        <div>
          <dt>Artifact ID</dt>
          <dd>
            <CopyableText copyLabel="复制约束快照 Artifact ID" value={snapshot.artifact.artifact_id} />
          </dd>
        </div>
        <div>
          <dt>Constraint snapshot ID</dt>
          <dd>
            <code>{snapshot.artifact.version_tuple.constraint_snapshot_id ?? "未绑定"}</code>
          </dd>
        </div>
        <div>
          <dt>Payload schema</dt>
          <dd>
            <code>{snapshot.artifact.payload_schema_id ?? "未公开"}</code>
          </dd>
        </div>
        <div>
          <dt>DSL grammar</dt>
          <dd>
            <code>{snapshot.dsl_grammar_version}</code>
          </dd>
        </div>
      </dl>

      <section className="gf-specs__authority-path" aria-labelledby="authority-path-title">
        <header>
          <Route aria-hidden="true" size={19} />
          <div>
            <h2 id="authority-path-title">约束权威化路径</h2>
            <p>流程说明不是当前状态机；当前状态只取自上方显式证据。</p>
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
            <h2 id="constraint-list-title">快照约束条目</h2>
            <p>仅在 exact constraint-snapshot@1 下解释 generated JsonValue 载荷。</p>
          </div>
        </header>
        {constraints === null ? (
          <StatePanel
            description="Schema ID 或必需字段与当前窄渲染契约不一致；原始 JsonValue 不会直接输出。"
            state="error"
            title="无法安全解释约束载荷"
          />
        ) : constraints.length === 0 ? (
          <StatePanel
            description="该 exact 快照包含零条约束；本页不会补造默认规则。"
            state="empty"
            title="快照中没有约束条目"
          />
        ) : (
          <CursorTable
            caption="约束条目（快照载荷）"
            columns={constraintColumns}
            getRowKey={(item) => item.id}
            items={constraints}
            toolbar={<span>{constraints.length} 条 exact payload entry</span>}
          />
        )}
      </section>
    </div>
  );
}
