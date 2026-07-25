import { useInfiniteQuery, useQuery, type InfiniteData } from "@tanstack/react-query";
import { Braces, Network, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";

import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { TraceWaterfall } from "../../components/charts";
import { compactDateTime, TechnicalDetails } from "../../components/identity";
import { LogExplorer } from "../../components/logs";
import { CursorTable, type CursorPaginationState, type CursorTableColumn } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { observabilityApi, type LogPage, type ObservabilityApi, type SpanPage } from "./api";
import {
  requireSpanPageOwner,
  requireTraceOwner,
  safeSpanInspector,
  traceSummaryTone,
  traceWaterfallSpans,
} from "./model";
import "./observability.css";

type SpanView = SpanPage["items"][number];

const traceStatusLabels: Readonly<Record<string, string>> = {
  error: "失败",
  ok: "正常",
  unset: "状态未知",
};

function durationLabel(durationNs: number | null): string {
  if (durationNs == null) return "未报告耗时";
  const milliseconds = durationNs / 1_000_000;
  return milliseconds < 1 ? `${milliseconds.toFixed(2)} 毫秒` : `${milliseconds.toFixed(1)} 毫秒`;
}

function ReadError({ error, onRetry }: { error: Error; onRetry(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRetry} type="button">
          重试读取
        </button>
      }
      description="Trace 读模型不可用；未显示底层异常。"
      state="error"
      title="无法读取 Trace"
    />
  );
}

function paginationState(query: { error: Error | null; isFetchingNextPage: boolean }): CursorPaginationState {
  if (query.error instanceof CursorExpiredError) return "expired";
  if (query.error) return "error";
  return query.isFetchingNextPage ? "loading" : "ready";
}

function TruncatedNotice({ scope }: { scope: string }) {
  return (
    <p className="gf-observability__truncated">
      <Network aria-hidden="true" size={15} />
      {scope} 已截断
    </p>
  );
}

function SpanInspector({ view }: { view: SpanView }) {
  const inspector = useMemo(() => safeSpanInspector(view), [view]);
  return (
    <section className="gf-observability__inspector" aria-labelledby="span-inspector-heading">
      <header>
        <div>
          <p className="gf-observability__kicker">安全诊断视图</p>
          <h2 id="span-inspector-heading">步骤详情</h2>
        </div>
        {inspector.redactedCount > 0 && (
          <span className="gf-observability__redacted">
            <ShieldCheck aria-hidden="true" size={15} />
            {inspector.redactedCount} 个字段已脱敏
          </span>
        )}
      </header>
      <dl className="gf-observability__inspector-meta">
        <div>
          <dt>步骤</dt>
          <dd>{inspector.name}</dd>
        </div>
        <div>
          <dt>层级</dt>
          <dd>{inspector.parentSpanId ? "子步骤" : "起始步骤"}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{traceStatusLabels[inspector.status] ?? inspector.status}</dd>
        </div>
        <div>
          <dt>执行时间</dt>
          <dd>
            {compactDateTime(inspector.startedAt)} → {compactDateTime(inspector.endedAt)}
          </dd>
        </div>
        <div>
          <dt>耗时</dt>
          <dd>{durationLabel(inspector.durationNs)}</dd>
        </div>
      </dl>
      <TechnicalDetails
        items={[
          { label: "步骤 ID", value: inspector.spanId },
          { label: "父步骤 ID", value: inspector.parentSpanId ?? "root" },
          { label: "状态代码", value: inspector.status },
          { label: "原始耗时（纳秒）", value: String(inspector.durationNs) },
        ]}
        summary="查看步骤技术信息"
      />
      {inspector.error && (
        <div className="gf-observability__span-error">
          <strong>此步骤执行失败</strong>
          <span>{inspector.error.message}</span>
          <TechnicalDetails
            items={[
              { label: "错误类型", value: inspector.error.error_type },
              ...(inspector.error.stack_fingerprint
                ? [{ label: "错误指纹", value: inspector.error.stack_fingerprint }]
                : []),
            ]}
            summary="查看错误技术信息"
          />
        </div>
      )}
      <details>
        <summary>查看步骤诊断数据</summary>
        <div className="gf-observability__inspector-columns">
          <SafeFields label="步骤属性" rows={inspector.attributes} />
          <SafeFields label="运行资源" rows={inspector.resource} />
        </div>
        <section className="gf-observability__events" aria-label="步骤事件">
          <h3>步骤事件</h3>
          {inspector.events.length === 0 ? (
            <p>没有事件。</p>
          ) : (
            <ol>
              {inspector.events.map((event, index) => (
                <li key={`${event.occurredAt}:${event.name}:${index}`}>
                  <header>
                    <strong>{event.name}</strong>
                    <time dateTime={event.occurredAt}>{compactDateTime(event.occurredAt)}</time>
                  </header>
                  <SafeFields label={`${event.name} 属性`} rows={event.attributes} />
                </li>
              ))}
            </ol>
          )}
        </section>
      </details>
    </section>
  );
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null) return "null";
  return JSON.stringify(value);
}

function SafeFields({ label, rows }: { label: string; rows: readonly [string, unknown][] }) {
  return (
    <section className="gf-observability__safe-fields" aria-label={label}>
      <h3>{label}</h3>
      {rows.length === 0 ? (
        <p>无可显示字段。</p>
      ) : (
        <dl>
          {rows.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>
                <code>{displayValue(value)}</code>
              </dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}

function LogCursor({
  error,
  isFetching,
  nextCursor,
  onLoadMore,
  onRestart,
}: {
  error: Error | null;
  isFetching: boolean;
  nextCursor: string | null;
  onLoadMore(): void;
  onRestart(): void;
}) {
  return (
    <div className="gf-observability__cursor-footer">
      {error instanceof CursorExpiredError ? (
        <>
          <p role="status">分页游标已过期；现有 Trace 日志保留，必须显式重开查询。</p>
          <button className="gf-secondary-button" onClick={onRestart} type="button">
            重新开始查询
          </button>
        </>
      ) : error ? (
        <>
          <p role="status">下一页读取失败；现有 Trace 日志保留。</p>
          {nextCursor && (
            <button className="gf-secondary-button" onClick={onLoadMore} type="button">
              重试下一页
            </button>
          )}
        </>
      ) : nextCursor ? (
        <button className="gf-secondary-button" disabled={isFetching} onClick={onLoadMore} type="button">
          {isFetching ? "正在加载…" : "加载下一页"}
        </button>
      ) : (
        <p>已到末页</p>
      )}
    </div>
  );
}

function spanColumns(onSelect: (spanId: string) => void): readonly CursorTableColumn<SpanView>[] {
  return [
    {
      header: "执行步骤",
      id: "span",
      render: (view) => (
        <div className="gf-observability__table-primary">
          <button className="gf-link-button" onClick={() => onSelect(view.span.span_id)} type="button">
            查看 {view.span.name}
          </button>
        </div>
      ),
    },
    {
      header: "层级",
      id: "parent",
      render: (view) => (view.span.parent_span_id ? "子步骤" : "起始步骤"),
    },
    {
      header: "状态",
      id: "status",
      render: (view) => <span>{traceStatusLabels[view.span.status] ?? view.span.status}</span>,
    },
    {
      header: "耗时",
      id: "duration",
      render: (view) => <span>{durationLabel(view.span.duration_ns)}</span>,
    },
  ];
}

export function TraceDetailPage({
  api = observabilityApi,
  now = () => new Date(),
  traceId,
}: {
  api?: ObservabilityApi;
  now?: () => Date;
  traceId: string;
}) {
  const [spanEpoch, setSpanEpoch] = useState(0);
  const [logEpoch, setLogEpoch] = useState(0);
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);
  const openTraceWindowEnd = useMemo(() => now().toISOString(), [traceId]);
  const summaryQuery = useQuery({
    queryFn: async () => requireTraceOwner(await api.getTrace(traceId), traceId),
    queryKey: ["observability", "trace", traceId],
    retry: false,
  });
  const spansQuery = useInfiniteQuery<
    SpanPage,
    Error,
    InfiniteData<SpanPage>,
    readonly unknown[],
    string | null
  >({
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      requireSpanPageOwner(await api.listTraceSpans(traceId, pageParam), traceId),
    queryKey: ["observability", "trace-spans", traceId, spanEpoch],
    retry: false,
  });
  const summary = summaryQuery.data;
  const logWindow = summary
    ? {
        endUtc: summary.ended_at ?? openTraceWindowEnd,
        startUtc: summary.started_at,
      }
    : null;
  const logsQuery = useInfiniteQuery<
    LogPage,
    Error,
    InfiniteData<LogPage>,
    readonly unknown[],
    string | null
  >({
    enabled: logWindow !== null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => api.queryLogs({ cursor: pageParam, ...logWindow!, traceId }),
    queryKey: ["observability", "trace-logs", traceId, logWindow, logEpoch],
    retry: false,
  });

  const spanPages = spansQuery.data?.pages ?? [];
  const spans = spanPages.flatMap((page) => page.items);
  const logPages = logsQuery.data?.pages ?? [];
  const logs = logPages.flatMap((page) => page.items);
  const nextSpanCursor = spanPages[spanPages.length - 1]?.next_cursor ?? null;
  const nextLogCursor = logPages[logPages.length - 1]?.next_cursor ?? null;
  const selectedSpan =
    spans.find((view) => view.span.span_id === selectedSpanId) ??
    spans.find((view) => view.span.span_id === summary?.root_span_id) ??
    spans[0];

  if (summaryQuery.isPending) {
    return (
      <div className="gf-page gf-observability">
        <StatePanel
          description="正在读取这次运行的执行步骤。"
          headingLevel={1}
          state="loading"
          title="正在读取运行追踪"
        />
      </div>
    );
  }
  if (summaryQuery.isError) {
    return (
      <div className="gf-page gf-observability">
        <header className="gf-page-header">
          <h1>运行追踪</h1>
        </header>
        <ReadError error={summaryQuery.error} onRetry={() => void summaryQuery.refetch()} />
      </div>
    );
  }
  if (!summary) {
    return (
      <div className="gf-page gf-observability">
        <StatePanel
          description="运行追踪摘要为空，页面不会猜测缺失内容。"
          headingLevel={1}
          state="error"
          title="运行追踪不可用"
        />
      </div>
    );
  }

  return (
    <div className="gf-page gf-observability" data-layout="editorial-trace-detail">
      <header className="gf-observability__hero gf-observability__hero--trace">
        <div>
          <p className="gf-observability__kicker">运行诊断</p>
          <h1>运行追踪</h1>
          <p>
            {summary.span_count} 个执行步骤 · {summary.service_names.join(" / ") || "未报告执行服务"} ·{" "}
            {durationLabel(summary.duration_ns ?? null)}
          </p>
        </div>
        <div className="gf-observability__hero-mark" aria-hidden="true">
          <Braces size={30} />
          <span>追踪</span>
        </div>
      </header>
      {summary.truncated && <TruncatedNotice scope="运行追踪摘要" />}

      <section className="gf-observability__trace-summary" aria-label="运行追踪摘要">
        <div>
          <span>状态</span>
          <strong className={`u-status u-status--${traceSummaryTone(summary.status)}`}>
            {traceStatusLabels[summary.status] ?? summary.status}
          </strong>
        </div>
        <div>
          <span>起始步骤</span>
          <span>{summary.root_span_id ? "已识别" : "未报告"}</span>
        </div>
        <div>
          <span>执行时间</span>
          <span>
            {compactDateTime(summary.started_at)} →{" "}
            {summary.ended_at ? compactDateTime(summary.ended_at) : "仍在进行"}
          </span>
        </div>
        <div>
          <span>关联运行</span>
          <span className="gf-observability__run-links">
            {summary.run_ids.map((runId, index) => (
              <a href={`/runs/${encodeURIComponent(runId)}`} key={runId}>
                查看关联运行 {index + 1}
              </a>
            ))}
          </span>
        </div>
      </section>
      <TechnicalDetails
        items={[
          { label: "追踪 ID", value: summary.trace_id },
          { label: "起始步骤 ID", value: summary.root_span_id ?? "未报告" },
          { label: "追踪数据版本", value: summary.trace_schema_version },
          ...summary.run_ids.map((runId) => ({ label: "关联运行 ID", value: runId })),
        ]}
        summary="查看追踪技术信息"
      />

      {spansQuery.isPending ? (
        <StatePanel description="正在读取 bounded Span page。" state="loading" title="正在读取 Span" />
      ) : spansQuery.isError && spanPages.length === 0 ? (
        <ReadError error={spansQuery.error} onRetry={() => void spansQuery.refetch()} />
      ) : (
        <>
          {spanPages.some((page) => page.truncated) && <TruncatedNotice scope="执行步骤列表" />}
          <TraceWaterfall
            spans={traceWaterfallSpans(spans)}
            summary={`已加载 ${spans.length} / ${summary.span_count} 个执行步骤`}
            title="执行步骤时间线"
          />
          <CursorTable
            caption="执行步骤"
            columns={spanColumns(setSelectedSpanId)}
            getRowKey={(view) => view.span.span_id}
            items={spans}
            nextCursor={nextSpanCursor}
            onLoadMore={() => void spansQuery.fetchNextPage()}
            onRestart={() => setSpanEpoch((value) => value + 1)}
            paginationState={paginationState(spansQuery)}
          />
        </>
      )}

      {selectedSpan && <SpanInspector view={selectedSpan} />}

      <section className="gf-observability__section" id="trace-logs">
        <header className="gf-observability__section-heading">
          <Network aria-hidden="true" size={21} />
          <div>
            <h2>运行日志</h2>
            <p>日志只显示这次追踪对应的安全字段；内部标识默认收起。</p>
          </div>
        </header>
        {logsQuery.isPending ? (
          <StatePanel description="正在读取 Trace 日志。" state="loading" title="正在读取日志" />
        ) : logsQuery.isError && logPages.length === 0 ? (
          <ReadError error={logsQuery.error} onRetry={() => void logsQuery.refetch()} />
        ) : (
          <>
            {logPages.some((page) => page.truncated) && <TruncatedNotice scope="Log page" />}
            {logPages[0] && (
              <p className="u-small">
                日志覆盖时间 · {compactDateTime(logPages[0].coverage_start)} →{" "}
                {compactDateTime(logPages[0].coverage_end)}
              </p>
            )}
            <LogExplorer items={logs} title="运行日志记录" />
            <LogCursor
              error={logsQuery.error}
              isFetching={logsQuery.isFetchingNextPage}
              nextCursor={nextLogCursor}
              onLoadMore={() => void logsQuery.fetchNextPage()}
              onRestart={() => setLogEpoch((value) => value + 1)}
            />
          </>
        )}
      </section>
    </div>
  );
}
