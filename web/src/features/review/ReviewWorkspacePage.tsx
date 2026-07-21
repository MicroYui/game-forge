import { useQuery } from "@tanstack/react-query";
import { FileCheck2, GitCompareArrows, ScanSearch } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import {
  CopyableText,
  CursorTable,
  type CursorPaginationState,
  type CursorTableColumn,
} from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { findingBuckets } from "./authority";
import { reviewApi, type ReviewApi, type ReviewArtifactView } from "./api";
import { ReviewLaunchCard, type ReviewGenerationContext } from "./ReviewLaunchCard";
import "./review.css";

interface ReviewPageState {
  error?: Error;
  items: ReviewArtifactView[];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

function toPageState(page: Awaited<ReturnType<ReviewApi["listReviews"]>>): ReviewPageState {
  return {
    items: page.items,
    loading: false,
    nextCursor: page.next_cursor ?? null,
    readSnapshotId: page.read_snapshot_id,
  };
}

function requirePageAuthority(
  page: Awaited<ReturnType<ReviewApi["listReviews"]>>,
): Awaited<ReturnType<ReviewApi["listReviews"]>> {
  for (const review of page.items) findingBuckets(review.report);
  return page;
}

function paginationState(state: ReviewPageState): CursorPaginationState {
  if (state.error instanceof CursorExpiredError) return "expired";
  if (state.error) return "error";
  return state.loading ? "loading" : "ready";
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("Review 分页读取失败。");
}

function countLabel(review: ReviewArtifactView): string {
  const buckets = findingBuckets(review.report);
  return `${buckets.deterministic.length} 确定性 · ${buckets.simulation.length} 仿真 · ${buckets.suggestion.length} LLM · ${buckets.unproven.length} 未证明`;
}

function reviewDetailHref(
  artifactId: string,
  sourceRunId: string | null,
  snapshotContext: string | null,
): string {
  const params = new URLSearchParams();
  if (sourceRunId) params.set("sourceRun", sourceRunId);
  if (snapshotContext) params.set("snapshot", snapshotContext);
  const query = params.toString();
  return `/reviews/${encodeURIComponent(artifactId)}${query ? `?${query}` : ""}`;
}

function columns(
  sourceRunId: string | null,
  snapshotContext: string | null,
): readonly CursorTableColumn<ReviewArtifactView>[] {
  return [
    {
      header: "Review Artifact",
      id: "artifact",
      render: (item) => (
        <div className="gf-review__table-primary">
          <CopyableText copyLabel="复制 Review Artifact ID" value={item.artifact.artifact_id} />
          <a href={reviewDetailHref(item.artifact.artifact_id, sourceRunId, snapshotContext)}>
            打开 {item.artifact.artifact_id}
          </a>
        </div>
      ),
    },
    {
      header: "精确快照",
      id: "snapshot",
      render: (item) => <CopyableText copyLabel="复制 Review snapshot ID" value={item.report.snapshot_id} />,
    },
    {
      header: "Finding 分区",
      id: "counts",
      render: (item) => <span>{countLabel(item)}</span>,
    },
    {
      header: "冻结工具身份",
      id: "tool",
      render: (item) => <code>{item.artifact.version_tuple.tool_version ?? "不适用 (N/A)"}</code>,
    },
    {
      header: "请求上下文",
      id: "context",
      render: (item) =>
        snapshotContext === null ? (
          <span className="gf-review__muted">无 preview 上下文；未推断当前候选</span>
        ) : item.artifact.parent_artifact_ids.includes(snapshotContext) ? (
          <span className="gf-review__context-match">direct parent 包含请求的 preview Artifact</span>
        ) : (
          <span className="gf-review__context-miss">direct parent 不含请求的 preview；未隐藏该报告</span>
        ),
    },
  ];
}

function WorkspaceError({ error, onRetry }: { error: Error; onRetry(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRetry} type="button">
          重试
        </button>
      }
      description="Review 列表读取失败；未显示底层异常。"
      state="error"
      title="无法读取审查报告"
    />
  );
}

export function ReviewWorkspacePage({ api = reviewApi }: { api?: ReviewApi }) {
  const [searchParams] = useSearchParams();
  const sourceRunId = searchParams.get("sourceRun");
  const snapshotContext = searchParams.get("snapshot");
  const constraintContext = searchParams.get("constraint");
  const launchContext: ReviewGenerationContext | null =
    sourceRunId && snapshotContext && constraintContext
      ? {
          constraintArtifactId: constraintContext,
          snapshotArtifactId: snapshotContext,
          sourceRunId,
        }
      : null;
  const initial = useQuery({
    queryFn: async () => requirePageAuthority(await api.listReviews(null)),
    queryKey: ["review-workspace"],
    retry: false,
  });
  const [pageState, setPageState] = useState<ReviewPageState | null>(null);
  const pageRequestEpoch = useRef(0);

  useEffect(() => {
    if (initial.data) {
      pageRequestEpoch.current += 1;
      setPageState(toPageState(initial.data));
    }
  }, [initial.data]);

  async function readPage(cursor: string | null, restart: boolean) {
    const current = pageState;
    if (!current) return;
    const requestEpoch = ++pageRequestEpoch.current;
    setPageState({ ...current, error: undefined, loading: true });
    try {
      const next = requirePageAuthority(await api.listReviews(cursor));
      if (requestEpoch !== pageRequestEpoch.current) return;
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("Review 分页快照发生变化，请重新开始查询。");
      }
      setPageState({
        ...toPageState(next),
        items: restart ? [...next.items] : [...current.items, ...next.items],
      });
    } catch (error) {
      if (requestEpoch !== pageRequestEpoch.current) return;
      setPageState({ ...current, error: normalizedError(error), loading: false });
    }
  }

  const tableColumns = useMemo(() => columns(sourceRunId, snapshotContext), [snapshotContext, sourceRunId]);

  if (initial.isPending) {
    return (
      <div className="gf-page gf-review">
        <StatePanel
          description="正在读取 immutable Review Artifact 快照。"
          headingLevel={1}
          state="loading"
          title="正在读取审查报告"
        />
      </div>
    );
  }

  if (initial.isError) {
    return (
      <div className="gf-page gf-review">
        <header className="gf-page-header">
          <p className="gf-review__kicker">Review artifacts · immutable history</p>
          <h1>审查报告</h1>
        </header>
        <WorkspaceError error={initial.error} onRetry={() => void initial.refetch()} />
      </div>
    );
  }

  const current = pageState ?? toPageState(initial.data);

  return (
    <div className="gf-page gf-review" data-layout="editorial-review-index">
      <header className="gf-review__hero">
        <div>
          <p className="gf-review__kicker">Correctness desk · Review history</p>
          <h1>审查报告</h1>
          <p>
            每份报告保留独立 Artifact 身份；同一快照的不同工具版本不会折叠，也不会把零 Finding 改写为通过。
          </p>
        </div>
        <div className="gf-review__hero-mark" aria-hidden="true">
          <ScanSearch size={30} />
          <span>REVIEW</span>
        </div>
      </header>

      {(sourceRunId || snapshotContext) && (
        <aside className="gf-review__context" aria-label="Generation 导航上下文">
          <GitCompareArrows aria-hidden="true" size={20} />
          <div>
            <h2>来自 Generation 的导航上下文</h2>
            {sourceRunId && (
              <p>
                Generation Run 仅作为导航上下文：
                <a href={`/runs/${encodeURIComponent(sourceRunId)}`}>{sourceRunId}</a>。详情页会通过 terminal
                manifest 验证它是否属于该 Review 的 producer occurrence。
              </p>
            )}
            {snapshotContext && (
              <p>
                请求 preview：<code>{snapshotContext}</code>。列表保留不匹配报告，详情再校验 exact lineage。
              </p>
            )}
            {constraintContext && (
              <p>
                请求 constraint：<code>{constraintContext}</code>。Review 启动会把它作为 exact input，而非读取
                current alias。
              </p>
            )}
          </div>
        </aside>
      )}

      {launchContext && (
        <ReviewLaunchCard
          api={api}
          context={launchContext}
          key={`${launchContext.sourceRunId}\u0000${launchContext.snapshotArtifactId}\u0000${launchContext.constraintArtifactId}`}
        />
      )}

      <section className="gf-review__index-panel" aria-labelledby="review-index-title">
        <header>
          <FileCheck2 aria-hidden="true" size={22} />
          <div>
            <h2 id="review-index-title">Immutable report ledger</h2>
            <p>列表唯一键是 Review Artifact ID；顺序和分页由服务端 read snapshot 冻结。</p>
          </div>
        </header>
        <CursorTable
          caption="Review Artifact 历史"
          columns={tableColumns}
          emptyLabel="当前权限范围内没有 Review Artifact"
          getRowKey={(item) => item.artifact.artifact_id}
          items={current.items}
          nextCursor={current.nextCursor}
          onLoadMore={(cursor) => void readPage(cursor, false)}
          onRestart={() => void readPage(null, true)}
          paginationState={paginationState(current)}
        />
      </section>
    </div>
  );
}
