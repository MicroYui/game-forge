import { useQuery } from "@tanstack/react-query";
import { Braces, FilePenLine, FileStack, GitBranch, LibraryBig, ShieldQuestion } from "lucide-react";
import { useEffect, useState } from "react";

import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import {
  CopyableText,
  CursorTable,
  type CursorPaginationState,
  type CursorTableColumn,
} from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  specWorkflowApi,
  type ConstraintProposalReadView,
  type ConstraintSnapshotView,
  type SpecView,
  type SpecWorkflowApi,
} from "./api";
import { SpecEntryPanels, type SpecEntryPanelsApi } from "./SpecEntryPanels";
import "./specs.css";

export type SpecWorkspaceApi = SpecEntryPanelsApi &
  Pick<SpecWorkflowApi, "listConstraintProposals" | "listConstraintSnapshots" | "listSpecs">;

interface CursorPageState<T> {
  error?: Error;
  items: T[];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

function toPageState<T>(page: {
  items: T[];
  next_cursor?: string | null;
  read_snapshot_id: string;
}): CursorPageState<T> {
  return {
    items: page.items,
    loading: false,
    nextCursor: page.next_cursor ?? null,
    readSnapshotId: page.read_snapshot_id,
  };
}

function paginationState<T>(state: CursorPageState<T>): CursorPaginationState {
  if (state.error instanceof CursorExpiredError) return "expired";
  if (state.error) return "error";
  return state.loading ? "loading" : "ready";
}

function normalizedPageError(error: unknown): Error {
  return error instanceof Error ? error : new Error("分页读取失败。");
}

function domainLabel(value: SpecView["artifact"]["domain_scope"]): string {
  if (value === "all") return "全部域";
  if (value === null) return "未公开域投影";
  return value.domain_ids.join(" · ") || "空域集合";
}

const specColumns: readonly CursorTableColumn<SpecView>[] = [
  {
    header: "规格 Artifact",
    id: "artifact",
    render: (item) => (
      <div className="gf-specs__table-primary">
        <CopyableText copyLabel="复制规格 Artifact ID" value={item.artifact.artifact_id} />
        <a href={`/specs/${encodeURIComponent(item.artifact.artifact_id)}`}>检查规格与图谱</a>
      </div>
    ),
  },
  {
    header: "Snapshot",
    id: "snapshot",
    render: (item) => <CopyableText copyLabel="复制 Snapshot ID" value={item.snapshot_id} />,
  },
  {
    header: "Schema registry",
    id: "schema",
    render: (item) => <code>{item.schema_registry_version}</code>,
  },
  {
    header: "Ref 绑定",
    id: "ref",
    render: (item) =>
      item.ref_name && item.ref_value ? (
        <div className="gf-specs__ref-binding">
          <GitBranch aria-hidden="true" size={14} />
          <a href={`/refs/${encodeURIComponent(item.ref_name)}/history`}>
            {item.ref_name} · revision {item.ref_value.revision}
          </a>
        </div>
      ) : (
        <span className="gf-specs__muted">未绑定 ref；工件存在不表示当前版本</span>
      ),
  },
  {
    header: "域",
    id: "domain",
    render: (item) => <span>{domainLabel(item.artifact.domain_scope)}</span>,
  },
];

const constraintColumns: readonly CursorTableColumn<ConstraintSnapshotView>[] = [
  {
    header: "ConstraintSnapshot Artifact",
    id: "artifact",
    render: (item) => (
      <div className="gf-specs__table-primary">
        <CopyableText copyLabel="复制约束快照 Artifact ID" value={item.artifact.artifact_id} />
        <a href={`/constraints/${encodeURIComponent(item.artifact.artifact_id)}`}>检查快照与权威证据</a>
      </div>
    ),
  },
  {
    header: "DSL grammar",
    id: "grammar",
    render: (item) => <code>{item.dsl_grammar_version}</code>,
  },
  {
    header: "条目",
    id: "count",
    render: (item) => `${item.constraints.length} 条`,
  },
  {
    header: "权威状态",
    id: "authority",
    render: () => (
      <span className="gf-specs__authority-unknown">
        <ShieldQuestion aria-hidden="true" size={14} />
        需由发布结果或 ref 历史另行证明
      </span>
    ),
  },
];

const proposalColumns: readonly CursorTableColumn<ConstraintProposalReadView>[] = [
  {
    header: "Proposal Artifact",
    id: "artifact",
    render: (item) => (
      <div className="gf-specs__table-primary">
        <CopyableText copyLabel="复制 Proposal Artifact ID" value={item.artifact.artifact_id} />
        <a href={`/constraint-proposals/${encodeURIComponent(item.artifact.artifact_id)}`}>
          检查 exact proposal
        </a>
      </div>
    ),
  },
  {
    header: "Produced by",
    id: "producer",
    render: (item) => (
      <div className="gf-specs__table-primary">
        <code>{item.proposal.produced_by}</code>
        {item.proposal.producer_run_id ? (
          <a href={`/runs/${encodeURIComponent(item.proposal.producer_run_id)}`}>
            {item.proposal.producer_run_id}
          </a>
        ) : (
          <span className="gf-specs__muted">Human direct draft · 无 producer Run</span>
        )}
      </div>
    ),
  },
  {
    header: "Proposal revision",
    id: "revision",
    render: (item) => <span>revision {item.proposal.revision}</span>,
  },
  {
    header: "Approval status",
    id: "approval",
    render: (item) => <code>{item.approval_status}</code>,
  },
];

function WorkspaceError({ error, onRetry }: { error: Error; onRetry(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRetry} type="button">
          重试
        </button>
      }
      description="规格与约束快照读取失败；未展示底层异常内容。"
      state="error"
      title="无法读取规格工作台"
    />
  );
}

export function SpecWorkspacePage({ api = specWorkflowApi }: { api?: SpecWorkspaceApi }) {
  const workspace = useQuery({
    queryFn: async () => {
      const [specs, constraintSnapshots, constraintProposals] = await Promise.all([
        api.listSpecs(null),
        api.listConstraintSnapshots(null),
        api.listConstraintProposals(null),
      ]);
      return { constraintProposals, constraintSnapshots, specs };
    },
    queryKey: ["spec-workspace"],
    retry: false,
  });
  const [specs, setSpecs] = useState<CursorPageState<SpecView> | null>(null);
  const [constraintSnapshots, setConstraintSnapshots] =
    useState<CursorPageState<ConstraintSnapshotView> | null>(null);
  const [constraintProposals, setConstraintProposals] =
    useState<CursorPageState<ConstraintProposalReadView> | null>(null);

  useEffect(() => {
    if (!workspace.data) return;
    setSpecs(toPageState(workspace.data.specs));
    setConstraintSnapshots(toPageState(workspace.data.constraintSnapshots));
    setConstraintProposals(toPageState(workspace.data.constraintProposals));
  }, [workspace.data]);

  async function readSpecsPage(cursor: string | null, restart: boolean) {
    const current = specs;
    if (!current) return;
    setSpecs({ ...current, error: undefined, loading: true });
    try {
      const next = await api.listSpecs(cursor);
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("规格分页快照发生变化，请重新开始查询。");
      }
      setSpecs({
        ...toPageState(next),
        items: restart ? next.items : [...current.items, ...next.items],
      });
    } catch (error) {
      setSpecs({ ...current, error: normalizedPageError(error), loading: false });
    }
  }

  async function readConstraintPage(cursor: string | null, restart: boolean) {
    const current = constraintSnapshots;
    if (!current) return;
    setConstraintSnapshots({ ...current, error: undefined, loading: true });
    try {
      const next = await api.listConstraintSnapshots(cursor);
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("约束快照分页发生变化，请重新开始查询。");
      }
      setConstraintSnapshots({
        ...toPageState(next),
        items: restart ? next.items : [...current.items, ...next.items],
      });
    } catch (error) {
      setConstraintSnapshots({
        ...current,
        error: normalizedPageError(error),
        loading: false,
      });
    }
  }

  async function readProposalPage(cursor: string | null, restart: boolean) {
    const current = constraintProposals;
    if (!current) return;
    setConstraintProposals({ ...current, error: undefined, loading: true });
    try {
      const next = await api.listConstraintProposals(cursor);
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("约束提案分页发生变化，请重新开始查询。");
      }
      setConstraintProposals({
        ...toPageState(next),
        items: restart ? next.items : [...current.items, ...next.items],
      });
    } catch (error) {
      setConstraintProposals({
        ...current,
        error: normalizedPageError(error),
        loading: false,
      });
    }
  }

  if (workspace.isPending) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          description="正在读取有界规格、constraint_snapshot Artifact 与 constraint proposal 页。"
          headingLevel={1}
          state="loading"
          title="正在读取规格工作台"
        />
      </div>
    );
  }

  if (workspace.isError) {
    return (
      <div className="gf-page gf-specs">
        <header className="gf-page-header">
          <p className="gf-specs__kicker">Design-Spec IR · Read workspace</p>
          <h1>规格与约束快照</h1>
        </header>
        <WorkspaceError error={workspace.error} onRetry={() => void workspace.refetch()} />
      </div>
    );
  }

  const currentSpecs = specs ?? toPageState(workspace.data.specs);
  const currentConstraints = constraintSnapshots ?? toPageState(workspace.data.constraintSnapshots);
  const currentProposals = constraintProposals ?? toPageState(workspace.data.constraintProposals);
  const empty =
    currentSpecs.items.length === 0 &&
    currentConstraints.items.length === 0 &&
    currentProposals.items.length === 0;

  return (
    <div className="gf-page gf-specs" data-layout="editorial-workspace">
      <header className="gf-specs__hero">
        <div>
          <p className="gf-specs__kicker">Design-Spec IR · Authoring workspace</p>
          <h1>规格与约束快照</h1>
          <p className="gf-specs__lede">
            从明确输入创建 Spec 或约束候选，并检查可版本化的 Spec-IR、schema registry 绑定与有界图谱。
          </p>
        </div>
        <dl className="gf-specs__edition" aria-label="当前有界读取摘要">
          <div>
            <dt>规格</dt>
            <dd>{currentSpecs.items.length}</dd>
          </div>
          <div>
            <dt>约束快照</dt>
            <dd>{currentConstraints.items.length}</dd>
          </div>
          <div>
            <dt>约束提案</dt>
            <dd>{currentProposals.items.length}</dd>
          </div>
        </dl>
      </header>

      <aside className="gf-specs__semantic-note" role="note">
        <Braces aria-hidden="true" size={20} />
        <div>
          <strong>/constraints 返回 constraint_snapshot Artifact，不是全局权威约束列表</strong>
          <p>Artifact 存在、可读取或由编译产生，都不能单独证明权威；需由发布结果或 ref 历史另行证明。</p>
        </div>
      </aside>

      <SpecEntryPanels api={api} />

      {empty ? (
        <StatePanel
          description="当前授权范围内没有规格、约束快照或提案；本视图不会虚构默认或当前版本。"
          state="empty"
          title="尚无可读取的规格、约束快照或提案"
        />
      ) : (
        <div className="gf-specs__workspace-grid">
          <section className="gf-specs__workspace-section" aria-labelledby="constraint-proposals-title">
            <header className="gf-specs__section-heading">
              <FilePenLine aria-hidden="true" size={19} />
              <div>
                <h2 id="constraint-proposals-title">约束提案目录</h2>
                <p>逐项显示 immutable Artifact、作者来源、proposal revision、审批状态与 producer Run。</p>
              </div>
            </header>
            <CursorTable
              caption="约束提案（候选 Artifact）"
              columns={proposalColumns}
              emptyLabel="当前授权范围内没有约束提案"
              getRowKey={(item) => item.artifact.artifact_id}
              items={currentProposals.items}
              nextCursor={currentProposals.nextCursor}
              onLoadMore={(cursor) => void readProposalPage(cursor, false)}
              onRestart={() => void readProposalPage(null, true)}
              paginationState={paginationState(currentProposals)}
              toolbar={
                <span className="gf-specs__snapshot-label">
                  <FileStack aria-hidden="true" size={14} />
                  read snapshot <code>{currentProposals.readSnapshotId}</code>
                </span>
              }
            />
          </section>

          <section className="gf-specs__workspace-section" aria-labelledby="spec-artifacts-title">
            <header className="gf-specs__section-heading">
              <LibraryBig aria-hidden="true" size={19} />
              <div>
                <h2 id="spec-artifacts-title">规格目录</h2>
                <p>Ref 绑定与 Artifact 身份分开显示，避免把历史快照误称为当前。</p>
              </div>
            </header>
            <CursorTable
              caption="规格工件"
              columns={specColumns}
              emptyLabel="当前授权范围内没有规格工件"
              getRowKey={(item) => item.artifact.artifact_id}
              items={currentSpecs.items}
              nextCursor={currentSpecs.nextCursor}
              onLoadMore={(cursor) => void readSpecsPage(cursor, false)}
              onRestart={() => void readSpecsPage(null, true)}
              paginationState={paginationState(currentSpecs)}
              toolbar={
                <span className="gf-specs__snapshot-label">
                  <FileStack aria-hidden="true" size={14} />
                  read snapshot <code>{currentSpecs.readSnapshotId}</code>
                </span>
              }
            />
          </section>

          <section className="gf-specs__workspace-section" aria-labelledby="constraint-artifacts-title">
            <header className="gf-specs__section-heading">
              <FileStack aria-hidden="true" size={19} />
              <div>
                <h2 id="constraint-artifacts-title">约束快照工件</h2>
                <p>该目录包含可审计快照；列表投影本身不裁决 candidate 或 authority。</p>
              </div>
            </header>
            <CursorTable
              caption="约束快照工件（Artifact）"
              columns={constraintColumns}
              emptyLabel="当前授权范围内没有约束快照工件"
              getRowKey={(item) => item.artifact.artifact_id}
              items={currentConstraints.items}
              nextCursor={currentConstraints.nextCursor}
              onLoadMore={(cursor) => void readConstraintPage(cursor, false)}
              onRestart={() => void readConstraintPage(null, true)}
              paginationState={paginationState(currentConstraints)}
              toolbar={
                <span className="gf-specs__snapshot-label">
                  <FileStack aria-hidden="true" size={14} />
                  read snapshot <code>{currentConstraints.readSnapshotId}</code>
                </span>
              }
            />
          </section>
        </div>
      )}
    </div>
  );
}
