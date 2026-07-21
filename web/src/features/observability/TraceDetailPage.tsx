import { useInfiniteQuery, useQuery, type InfiniteData } from "@tanstack/react-query";
import { Braces, Network, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";

import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { TraceWaterfall } from "../../components/charts";
import { LogExplorer } from "../../components/logs";
import {
  CopyableText,
  CursorTable,
  type CursorPaginationState,
  type CursorTableColumn,
} from "../../components/tables";
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
          <p className="gf-observability__kicker">Safe span projection</p>
          <h2 id="span-inspector-heading">Span inspector</h2>
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
          <dt>Name</dt>
          <dd>{inspector.name}</dd>
        </div>
        <div>
          <dt>Span ID</dt>
          <dd>
            <CopyableText copyLabel="复制 Span ID" value={inspector.spanId} />
          </dd>
        </div>
        <div>
          <dt>Parent</dt>
          <dd>
            <code>{inspector.parentSpanId ?? "root"}</code>
          </dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{inspector.status}</dd>
        </div>
        <div>
          <dt>Started / ended</dt>
          <dd>
            {inspector.startedAt} → {inspector.endedAt}
          </dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{inspector.durationNs} ns</dd>
        </div>
      </dl>
      {inspector.error && (
        <div className="gf-observability__span-error">
          <strong>{inspector.error.error_type}</strong>
          <span>{inspector.error.message}</span>
          {inspector.error.stack_fingerprint && <code>{inspector.error.stack_fingerprint}</code>}
        </div>
      )}
      <div className="gf-observability__inspector-columns">
        <SafeFields label="Attributes" rows={inspector.attributes} />
        <SafeFields label="Resource" rows={inspector.resource} />
      </div>
      <section className="gf-observability__events" aria-label="Span events">
        <h3>Events</h3>
        {inspector.events.length === 0 ? (
          <p>没有事件。</p>
        ) : (
          <ol>
            {inspector.events.map((event, index) => (
              <li key={`${event.occurredAt}:${event.name}:${index}`}>
                <header>
                  <strong>{event.name}</strong>
                  <time dateTime={event.occurredAt}>{event.occurredAt}</time>
                </header>
                <SafeFields label={`${event.name} attributes`} rows={event.attributes} />
              </li>
            ))}
          </ol>
        )}
      </section>
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
      header: "Span",
      id: "span",
      render: (view) => (
        <div className="gf-observability__table-primary">
          <CopyableText copyLabel="复制 Span ID" value={view.span.span_id} />
          <button className="gf-link-button" onClick={() => onSelect(view.span.span_id)} type="button">
            检查 {view.span.name}
          </button>
        </div>
      ),
    },
    {
      header: "Parent",
      id: "parent",
      render: (view) => <code>{view.span.parent_span_id ?? "root"}</code>,
    },
    {
      header: "Status",
      id: "status",
      render: (view) => <span>{view.span.status}</span>,
    },
    {
      header: "Duration",
      id: "duration",
      render: (view) => <span>{view.span.duration_ns} ns</span>,
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
          description={`正在读取 ${traceId}`}
          headingLevel={1}
          state="loading"
          title="正在读取 Trace"
        />
      </div>
    );
  }
  if (summaryQuery.isError) {
    return (
      <div className="gf-page gf-observability">
        <header className="gf-page-header">
          <h1>Trace 详情</h1>
        </header>
        <ReadError error={summaryQuery.error} onRetry={() => void summaryQuery.refetch()} />
      </div>
    );
  }
  if (!summary) {
    return (
      <div className="gf-page gf-observability">
        <StatePanel
          description="Trace summary 响应为空；页面没有推断缺失内容。"
          headingLevel={1}
          state="error"
          title="Trace summary 不可用"
        />
      </div>
    );
  }

  return (
    <div className="gf-page gf-observability" data-layout="editorial-trace-detail">
      <header className="gf-observability__hero gf-observability__hero--trace">
        <div>
          <p className="gf-observability__kicker">Exact trace · bounded spans · safe inspection</p>
          <h1>Trace 详情</h1>
          <CopyableText copyLabel="复制 Trace ID" value={summary.trace_id} />
          <p>
            {summary.span_count} spans · {summary.service_names.join(" / ") || "service 未报告"} ·{" "}
            {summary.duration_ns == null ? "duration unavailable" : `${summary.duration_ns} ns`}
          </p>
        </div>
        <div className="gf-observability__hero-mark" aria-hidden="true">
          <Braces size={30} />
          <span>TRACE</span>
        </div>
      </header>
      {summary.truncated && <TruncatedNotice scope="Trace summary" />}

      <section className="gf-observability__trace-summary" aria-label="Trace summary">
        <div>
          <span>Status</span>
          <strong className={`u-status u-status--${traceSummaryTone(summary.status)}`}>
            {summary.status}
          </strong>
        </div>
        <div>
          <span>Root span</span>
          <code>{summary.root_span_id ?? "unavailable"}</code>
        </div>
        <div>
          <span>Time range</span>
          <span>
            {summary.started_at} → {summary.ended_at ?? "open"}
          </span>
        </div>
        <div>
          <span>Run bindings</span>
          <span className="gf-observability__run-links">
            {summary.run_ids.map((runId) => (
              <a href={`/runs/${encodeURIComponent(runId)}`} key={runId}>
                {runId}
              </a>
            ))}
          </span>
        </div>
      </section>

      {spansQuery.isPending ? (
        <StatePanel description="正在读取 bounded Span page。" state="loading" title="正在读取 Span" />
      ) : spansQuery.isError && spanPages.length === 0 ? (
        <ReadError error={spansQuery.error} onRetry={() => void spansQuery.refetch()} />
      ) : (
        <>
          {spanPages.some((page) => page.truncated) && <TruncatedNotice scope="Span page" />}
          <TraceWaterfall
            spans={traceWaterfallSpans(spans)}
            summary={`${spans.length} / ${summary.span_count} spans loaded · duration uses Span monotonic duration`}
            title="Trace waterfall"
          />
          <CursorTable
            caption="Trace spans"
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
            <h2>Trace 日志</h2>
            <p>查询绑定 exact trace_id 与 Trace 自身时间窗；日志仍由 LogExplorer 执行字段级安全呈现。</p>
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
                实际 coverage · {logPages[0].coverage_start} → {logPages[0].coverage_end}
              </p>
            )}
            <LogExplorer items={logs} title="Trace 日志记录" />
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
