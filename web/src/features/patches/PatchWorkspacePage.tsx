import { useQuery } from "@tanstack/react-query";
import { History, RotateCcw, Split } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { compactDateTime, ResourceIdentity } from "../../components/identity";
import { CursorTable, type CursorPaginationState, type CursorTableColumn } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  patchWorkflowApi,
  type PatchArtifactReadView,
  type PatchWorkflowApi,
  type RollbackRequestReadView,
} from "./api";
import "./patches.css";

interface LedgerState<T> {
  error: Error | null;
  items: T[];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("工作流目录读取失败。");
}

function paginationState<T>(state: LedgerState<T>): CursorPaginationState {
  if (state.error instanceof CursorExpiredError) return "expired";
  if (state.error) return "error";
  return state.loading ? "loading" : "ready";
}

function workflowStatusLabel(value: string): string {
  return (
    {
      applied: "已应用",
      approved: "已批准",
      changes_requested: "待修改",
      draft: "草稿",
      pending_approval: "待审批",
      rejected: "已驳回",
      rolled_back: "已回滚",
      superseded: "已被替代",
      validated: "已验证",
      validating: "验证中",
      validation_failed: "验证失败",
    }[value] ?? value
  );
}

function evidenceStatusLabel(value: string): string {
  return (
    {
      failed: "未通过",
      not_started: "未开始",
      passed: "已通过",
      running: "进行中",
    }[value] ?? value
  );
}

function patchRationaleLabel(value: string): string {
  if (/\p{Script=Han}/u.test(value)) return value;
  if (/generat|proposal/iu.test(value)) return "基于内容生成结果创建的修改草案";
  if (/reward|econom|sink|gold|currency/iu.test(value)) return "调整奖励与经济数值，使资源产出回到安全范围内";
  return "根据检查结果创建的内容修改";
}

/** What this change actually does to the game's content. */
function opSummaryLabel(ops: PatchArtifactReadView["patch"]["ops"]): string {
  const counts = { added: 0, changed: 0, removed: 0 };
  for (const op of ops) {
    if (op.op.startsWith("add_")) counts.added += 1;
    else if (op.op.startsWith("delete_") || op.op.startsWith("remove_")) counts.removed += 1;
    else counts.changed += 1;
  }
  const parts = [
    counts.added > 0 ? `新增 ${counts.added} 项` : null,
    counts.changed > 0 ? `修改 ${counts.changed} 项` : null,
    counts.removed > 0 ? `删除 ${counts.removed} 项` : null,
  ].filter((part): part is string => part !== null);
  return parts.length > 0 ? parts.join(" · ") : "没有内容改动";
}

function rollbackReasonLabel(value: string): string {
  return /\p{Script=Han}/u.test(value) ? value : "按已确认的历史版本恢复正式内容";
}

const patchColumns: readonly CursorTableColumn<PatchArtifactReadView>[] = [
  {
    header: "内容修改",
    id: "patch",
    render: (item) => (
      <ResourceIdentity
        actionLabel="查看修改"
        description={`第 ${item.patch.revision} 版 · ${compactDateTime(item.artifact.created_at)}`}
        details={[
          {
            copyLabel: "复制修改标识",
            label: "修改标识",
            value: item.artifact.artifact_id,
          },
          ...(patchRationaleLabel(item.patch.rationale) === item.patch.rationale
            ? []
            : [{ label: "原始修改理由", value: item.patch.rationale }]),
          { label: "流程版本", value: String(item.workflow_revision) },
        ]}
        href={`/patches/${encodeURIComponent(item.artifact.artifact_id)}`}
        // The title says what this change does; the revision is context, not identity.
        title={patchRationaleLabel(item.patch.rationale)}
      />
    ),
  },
  {
    header: "版本",
    id: "revision",
    render: (item) => <span>第 {item.patch.revision} 版（保留历史）</span>,
  },
  {
    header: "流程状态",
    id: "workflow",
    render: (item) => (
      <span className="gf-patches__workflow-cell">{workflowStatusLabel(item.approval_status)}</span>
    ),
  },
  {
    header: "改了什么",
    id: "transition",
    render: (item) => (
      <ResourceIdentity
        description="原内容保持不变，修改结果保存为新候选版本"
        details={[
          { label: "修改前版本标识", value: item.patch.base_snapshot_id },
          { label: "修改后版本标识", value: item.patch.target_snapshot_id },
        ]}
        title={opSummaryLabel(item.patch.ops)}
      />
    ),
  },
  {
    header: "验证结果",
    id: "evidence",
    render: (item) => (
      <span>
        内容验证：{evidenceStatusLabel(item.validation_status)} · 回归验证：
        {evidenceStatusLabel(item.regression_status)}
      </span>
    ),
  },
];

const rollbackColumns: readonly CursorTableColumn<RollbackRequestReadView>[] = [
  {
    header: "回滚请求",
    id: "rollback",
    render: (item) => (
      <ResourceIdentity
        actionLabel="查看回滚"
        description={`${compactDateTime(item.artifact.created_at)} · ${rollbackReasonLabel(item.request.reason)}`}
        details={[
          {
            copyLabel: "复制回滚请求标识",
            label: "回滚请求标识",
            value: item.artifact.artifact_id,
          },
          ...(rollbackReasonLabel(item.request.reason) === item.request.reason
            ? []
            : [{ label: "原始回退原因", value: item.request.reason }]),
          { label: "流程版本", value: String(item.workflow_revision) },
        ]}
        href={`/rollback-requests/${encodeURIComponent(item.artifact.artifact_id)}`}
        title={`回滚请求 · 恢复到第 ${item.request.target_history_revision} 版`}
      />
    ),
  },
  {
    header: "发布位置",
    id: "ref",
    render: (item) => <span>{item.request.ref_name}</span>,
  },
  {
    header: "恢复目标",
    id: "target",
    render: (item) => (
      <ResourceIdentity
        details={[{ label: "历史内容标识", value: item.request.target_artifact_id }]}
        title={`恢复到第 ${item.request.target_history_revision} 版`}
      />
    ),
  },
  {
    header: "流程状态",
    id: "workflow",
    render: (item) => (
      <span className="gf-patches__workflow-cell">{workflowStatusLabel(item.approval_status)}</span>
    ),
  },
];

function LedgerError({ error, onRestart }: { error: Error; onRestart(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRestart} type="button">
          从第一页重新读取
        </button>
      }
      description="目录读取失败；页面没有合并不同 read snapshot。"
      state="error"
      title="无法读取工作流目录"
    />
  );
}

export function PatchWorkspacePage({ api = patchWorkflowApi }: { api?: PatchWorkflowApi }) {
  const patchQuery = useQuery({
    queryFn: () => api.listPatches(null),
    queryKey: ["patch-workspace", "patches"],
    retry: false,
  });
  const rollbackQuery = useQuery({
    queryFn: () => api.listRollbackRequests(null),
    queryKey: ["patch-workspace", "rollbacks"],
    retry: false,
  });
  const [patches, setPatches] = useState<LedgerState<PatchArtifactReadView> | null>(null);
  const [rollbacks, setRollbacks] = useState<LedgerState<RollbackRequestReadView> | null>(null);
  const patchEpoch = useRef(0);
  const rollbackEpoch = useRef(0);

  useEffect(() => {
    if (!patchQuery.data) return;
    patchEpoch.current += 1;
    setPatches({
      error: null,
      items: patchQuery.data.items,
      loading: false,
      nextCursor: patchQuery.data.next_cursor ?? null,
      readSnapshotId: patchQuery.data.read_snapshot_id,
    });
  }, [patchQuery.data]);
  useEffect(() => {
    if (!rollbackQuery.data) return;
    rollbackEpoch.current += 1;
    setRollbacks({
      error: null,
      items: rollbackQuery.data.items,
      loading: false,
      nextCursor: rollbackQuery.data.next_cursor ?? null,
      readSnapshotId: rollbackQuery.data.read_snapshot_id,
    });
  }, [rollbackQuery.data]);

  async function loadMorePatches(cursor: string | null, restart: boolean) {
    const current = patches;
    if (!current) return;
    const epoch = ++patchEpoch.current;
    setPatches({ ...current, error: null, loading: true });
    try {
      const next = await api.listPatches(cursor);
      if (patchEpoch.current !== epoch) return;
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("Patch read snapshot changed.");
      }
      setPatches({
        error: null,
        items: restart ? next.items : [...current.items, ...next.items],
        loading: false,
        nextCursor: next.next_cursor ?? null,
        readSnapshotId: next.read_snapshot_id,
      });
    } catch (error) {
      if (patchEpoch.current === epoch) {
        setPatches({
          ...current,
          error: normalizedError(error),
          loading: false,
        });
      }
    }
  }

  async function loadMoreRollbacks(cursor: string | null, restart: boolean) {
    const current = rollbacks;
    if (!current) return;
    const epoch = ++rollbackEpoch.current;
    setRollbacks({ ...current, error: null, loading: true });
    try {
      const next = await api.listRollbackRequests(cursor);
      if (rollbackEpoch.current !== epoch) return;
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("Rollback read snapshot changed.");
      }
      setRollbacks({
        error: null,
        items: restart ? next.items : [...current.items, ...next.items],
        loading: false,
        nextCursor: next.next_cursor ?? null,
        readSnapshotId: next.read_snapshot_id,
      });
    } catch (error) {
      if (rollbackEpoch.current === epoch) {
        setRollbacks({
          ...current,
          error: normalizedError(error),
          loading: false,
        });
      }
    }
  }

  const currentPatches = useMemo(() => {
    if (patches) return patches;
    if (!patchQuery.data) return null;
    return {
      error: null,
      items: patchQuery.data.items,
      loading: false,
      nextCursor: patchQuery.data.next_cursor ?? null,
      readSnapshotId: patchQuery.data.read_snapshot_id,
    } satisfies LedgerState<PatchArtifactReadView>;
  }, [patchQuery.data, patches]);
  const currentRollbacks = useMemo(() => {
    if (rollbacks) return rollbacks;
    if (!rollbackQuery.data) return null;
    return {
      error: null,
      items: rollbackQuery.data.items,
      loading: false,
      nextCursor: rollbackQuery.data.next_cursor ?? null,
      readSnapshotId: rollbackQuery.data.read_snapshot_id,
    } satisfies LedgerState<RollbackRequestReadView>;
  }, [rollbackQuery.data, rollbacks]);

  return (
    <div className="gf-page gf-patches" data-layout="editorial-patch-workspace">
      <header className="gf-patches__hero">
        <div>
          <p className="gf-patches__kicker">修改、验证与恢复</p>
          <h1>修改与版本</h1>
          <p>查看 AI 或人工提出的内容修改，确认验证状态，并在需要时恢复历史版本。</p>
        </div>
        <div className="gf-patches__hero-mark" aria-hidden="true">
          <Split size={30} />
          <span>修改</span>
        </div>
      </header>

      <section aria-labelledby="patch-ledger-title" className="gf-patches__ledger" role="region">
        <header>
          <History aria-hidden="true" size={21} />
          <div>
            <h2 id="patch-ledger-title">内容修改记录</h2>
            <p>每次修改都单独保留，不会覆盖之前的版本。</p>
          </div>
        </header>
        {patchQuery.isPending || currentPatches === null ? (
          <StatePanel description="正在读取最新的内容修改记录。" state="loading" title="正在读取修改记录" />
        ) : patchQuery.isError ? (
          <LedgerError error={patchQuery.error} onRestart={() => void patchQuery.refetch()} />
        ) : (
          <>
            <CursorTable
              caption="内容修改记录"
              columns={patchColumns}
              emptyLabel="当前没有内容修改记录"
              getRowKey={(item) => item.artifact.artifact_id}
              items={currentPatches.items}
              nextCursor={currentPatches.nextCursor}
              onLoadMore={(cursor) => void loadMorePatches(cursor, false)}
              onRestart={() => void loadMorePatches(null, true)}
              paginationState={paginationState(currentPatches)}
            />
            {currentPatches.error && !(currentPatches.error instanceof CursorExpiredError) && (
              <LedgerError error={currentPatches.error} onRestart={() => void loadMorePatches(null, true)} />
            )}
          </>
        )}
      </section>

      <section aria-labelledby="rollback-ledger-title" className="gf-patches__ledger" role="region">
        <header>
          <RotateCcw aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-ledger-title">版本恢复请求</h2>
            <p>恢复请求需要先验证、再审批、最后应用，并始终指向一个明确的历史版本。</p>
          </div>
        </header>
        {rollbackQuery.isPending || currentRollbacks === null ? (
          <StatePanel
            description="正在读取 RollbackRequest read snapshot。"
            state="loading"
            title="正在读取回滚请求"
          />
        ) : rollbackQuery.isError ? (
          <LedgerError error={rollbackQuery.error} onRestart={() => void rollbackQuery.refetch()} />
        ) : (
          <>
            <CursorTable
              caption="版本恢复请求"
              columns={rollbackColumns}
              emptyLabel="当前没有版本恢复请求"
              getRowKey={(item) => item.artifact.artifact_id}
              items={currentRollbacks.items}
              nextCursor={currentRollbacks.nextCursor}
              onLoadMore={(cursor) => void loadMoreRollbacks(cursor, false)}
              onRestart={() => void loadMoreRollbacks(null, true)}
              paginationState={paginationState(currentRollbacks)}
            />
            {currentRollbacks.error && !(currentRollbacks.error instanceof CursorExpiredError) && (
              <LedgerError
                error={currentRollbacks.error}
                onRestart={() => void loadMoreRollbacks(null, true)}
              />
            )}
          </>
        )}
      </section>
    </div>
  );
}
