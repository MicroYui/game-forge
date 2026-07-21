import { useQuery } from "@tanstack/react-query";
import { History, RotateCcw, Split } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

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

const patchColumns: readonly CursorTableColumn<PatchArtifactReadView>[] = [
  {
    header: "Patch Artifact",
    id: "patch",
    render: (item) => (
      <div className="gf-patches__table-primary">
        <CopyableText copyLabel="复制 Patch Artifact ID" value={item.artifact.artifact_id} />
        <a href={`/patches/${encodeURIComponent(item.artifact.artifact_id)}`}>
          打开 {item.artifact.artifact_id}
        </a>
      </div>
    ),
  },
  {
    header: "Immutable revision",
    id: "revision",
    render: (item) => <span>revision {item.patch.revision}</span>,
  },
  {
    header: "Workflow",
    id: "workflow",
    render: (item) => (
      <span className="gf-patches__workflow-cell">
        {item.approval_status} · workflow {item.workflow_revision}
      </span>
    ),
  },
  {
    header: "Snapshot transition",
    id: "transition",
    render: (item) => (
      <CopyableText
        copyLabel="复制 Snapshot transition"
        scrollable
        value={`${item.patch.base_snapshot_id} → ${item.patch.target_snapshot_id}`}
      />
    ),
  },
  {
    header: "Evidence state",
    id: "evidence",
    render: (item) => (
      <span>
        validation {item.validation_status} · regression {item.regression_status}
      </span>
    ),
  },
];

const rollbackColumns: readonly CursorTableColumn<RollbackRequestReadView>[] = [
  {
    header: "Rollback Artifact",
    id: "rollback",
    render: (item) => (
      <div className="gf-patches__table-primary">
        <CopyableText copyLabel="复制 Rollback Artifact ID" value={item.artifact.artifact_id} />
        <a href={`/rollback-requests/${encodeURIComponent(item.artifact.artifact_id)}`}>
          打开 {item.artifact.artifact_id}
        </a>
      </div>
    ),
  },
  { header: "Ref", id: "ref", render: (item) => <code>{item.request.ref_name}</code> },
  {
    header: "Historical target",
    id: "target",
    render: (item) => (
      <span>
        history revision {item.request.target_history_revision} · {item.request.target_artifact_id}
      </span>
    ),
  },
  {
    header: "Workflow",
    id: "workflow",
    render: (item) => (
      <span className="gf-patches__workflow-cell">
        {item.approval_status} · workflow {item.workflow_revision}
      </span>
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
        setPatches({ ...current, error: normalizedError(error), loading: false });
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
        setRollbacks({ ...current, error: normalizedError(error), loading: false });
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
          <p className="gf-patches__kicker">Immutable revisions · explicit authority · reversible refs</p>
          <h1>Patch / Diff</h1>
          <p>
            Patch 内容不可变；验证、审批和应用状态来自 retained workflow。回滚移动 ref，不伪造内容 lineage。
          </p>
        </div>
        <div className="gf-patches__hero-mark" aria-hidden="true">
          <Split size={30} />
          <span>PATCH</span>
        </div>
      </header>

      <section aria-labelledby="patch-ledger-title" className="gf-patches__ledger" role="region">
        <header>
          <History aria-hidden="true" size={21} />
          <div>
            <h2 id="patch-ledger-title">Patch revision ledger</h2>
            <p>每个 Artifact 是一个 immutable revision；列表不会折叠 series 或覆盖历史状态。</p>
          </div>
        </header>
        {patchQuery.isPending || currentPatches === null ? (
          <StatePanel description="正在读取 Patch read snapshot。" state="loading" title="正在读取 Patch" />
        ) : patchQuery.isError ? (
          <LedgerError error={patchQuery.error} onRestart={() => void patchQuery.refetch()} />
        ) : (
          <>
            <CursorTable
              caption="Patch revision ledger"
              columns={patchColumns}
              emptyLabel="当前权限范围内没有 Patch revision"
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
            <h2 id="rollback-ledger-title">Rollback request ledger</h2>
            <p>Rollback request 经过独立 validate → approve → apply；目标是明确的历史 revision。</p>
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
              caption="Rollback request ledger"
              columns={rollbackColumns}
              emptyLabel="当前权限范围内没有 RollbackRequest"
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
