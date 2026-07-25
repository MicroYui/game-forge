import { useQuery } from "@tanstack/react-query";
import { FileCheck2, GitCompareArrows, ScanSearch } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { compactDateTime, ResourceIdentity, TechnicalDetails } from "../../components/identity";
import { CursorTable, type CursorPaginationState, type CursorTableColumn } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { findingBuckets } from "./authority";
import { reviewApi, type ReviewApi, type ReviewArtifactView } from "./api";
import { ReviewLaunchCard, type ReviewCandidateContext } from "./ReviewLaunchCard";
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
  return `${buckets.deterministic.length} 个确定性问题 · ${buckets.simulation.length} 个模拟问题 · ${buckets.suggestion.length} 条 AI 建议 · ${buckets.unproven.length} 项待确认`;
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
      header: "检查报告",
      id: "artifact",
      render: (item) => (
        <ResourceIdentity
          actionLabel="查看报告"
          description={compactDateTime(item.artifact.created_at)}
          details={[
            {
              copyLabel: "复制报告标识",
              label: "报告标识",
              value: item.artifact.artifact_id,
            },
            {
              copyLabel: "复制内容版本标识",
              label: "内容版本标识",
              value: item.report.snapshot_id,
            },
          ]}
          href={reviewDetailHref(item.artifact.artifact_id, sourceRunId, snapshotContext)}
          title="内容检查报告"
        />
      ),
    },
    {
      header: "检查对象",
      id: "snapshot",
      render: (item) => (
        <ResourceIdentity
          description="报告绑定的固定内容，不会随当前版本变化"
          details={[{ label: "内容版本标识", value: item.report.snapshot_id }]}
          title="固定内容版本"
        />
      ),
    },
    {
      header: "检查结果",
      id: "counts",
      render: (item) => <span>{countLabel(item)}</span>,
    },
    {
      header: "检查方式",
      id: "tool",
      render: (item) => (
        <ResourceIdentity
          details={[
            {
              label: "工具版本",
              value: item.artifact.version_tuple.tool_version ?? "不适用",
            },
          ]}
          title="确定性检查"
        />
      ),
    },
    {
      header: "请求上下文",
      id: "context",
      render: (item) =>
        snapshotContext === null ? (
          <span className="gf-review__muted">从报告历史打开</span>
        ) : item.artifact.parent_artifact_ids.includes(snapshotContext) ? (
          <span className="gf-review__context-match">与当前候选匹配</span>
        ) : (
          <span className="gf-review__context-miss">其他内容版本的报告</span>
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
  const sourceRunId = searchParams.get("sourceRun")?.trim() || null;
  const snapshotContext = searchParams.get("snapshot")?.trim() || null;
  const constraintContext = searchParams.get("constraint")?.trim() || null;
  const launchContext: ReviewCandidateContext | null =
    snapshotContext && constraintContext
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
      setPageState({
        ...current,
        error: normalizedError(error),
        loading: false,
      });
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
          title="正在读取内容检查"
        />
      </div>
    );
  }

  if (initial.isError) {
    return (
      <div className="gf-page gf-review">
        <header className="gf-page-header">
          <p className="gf-review__kicker">Review artifacts · immutable history</p>
          <h1>内容检查</h1>
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
          <p className="gf-review__kicker">固定规则检查 · 历史报告</p>
          <h1>内容检查</h1>
          <p>查看每次内容检查发现的问题、待确认项和使用的固定版本。</p>
        </div>
        <div className="gf-review__hero-mark" aria-hidden="true">
          <ScanSearch size={30} />
          <span>检查</span>
        </div>
      </header>

      {(sourceRunId || snapshotContext || constraintContext) && (
        <aside className="gf-review__context" aria-label="候选导航上下文">
          <GitCompareArrows aria-hidden="true" size={20} />
          <div>
            <h2>候选导航上下文</h2>
            {sourceRunId && (
              <p>
                你从一次内容生成来到这里。
                <a href={`/runs/${encodeURIComponent(sourceRunId)}`}>查看来源生成记录</a>
              </p>
            )}
            <TechnicalDetails
              items={[
                ...(sourceRunId ? [{ label: "来源运行标识", value: sourceRunId }] : []),
                ...(snapshotContext ? [{ label: "候选内容标识", value: snapshotContext }] : []),
                ...(constraintContext ? [{ label: "规则版本标识", value: constraintContext }] : []),
              ]}
            />
          </div>
        </aside>
      )}

      {launchContext && (
        <ReviewLaunchCard
          api={api}
          context={launchContext}
          key={`${launchContext.sourceRunId ?? ""}\u0000${launchContext.snapshotArtifactId}\u0000${launchContext.constraintArtifactId}`}
        />
      )}

      <section className="gf-review__index-panel" aria-labelledby="review-index-title">
        <header>
          <FileCheck2 aria-hidden="true" size={22} />
          <div>
            <h2 id="review-index-title">历史检查报告</h2>
            <p>每次检查都会单独保留，便于比较不同内容版本和检查结果。</p>
          </div>
        </header>
        <CursorTable
          caption="历史检查报告"
          columns={tableColumns}
          emptyLabel="当前没有可查看的检查报告"
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
