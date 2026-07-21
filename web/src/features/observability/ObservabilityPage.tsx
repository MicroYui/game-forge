import { useInfiniteQuery, useQuery, type InfiniteData } from "@tanstack/react-query";
import { Activity, Coins, Gauge, Logs, Network, TimerReset } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { AreaSparkChart, CostBarChart, type CostBarDatum } from "../../components/charts";
import { LogExplorer } from "../../components/logs";
import {
  CopyableText,
  CursorTable,
  type CursorPaginationState,
  type CursorTableColumn,
} from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  observabilityApi,
  type MetricDescriptor,
  type MetricPage,
  type ObservabilityApi,
  type RunCostView,
  type RunPage,
  type RunView,
  type TimeWindow,
  type TraceSummary,
  type TraceSummaryPage,
  type LogPage,
} from "./api";
import {
  descriptorRef,
  observationValue,
  requireExactMetricSeries,
  requireRunCostOwner,
  requireRunOwner,
  requireRunTracePageOwner,
  traceSummaryTone,
} from "./model";
import "./observability.css";

function defaultWindow(now: Date): TimeWindow {
  return {
    endUtc: now.toISOString(),
    startUtc: new Date(now.getTime() - 60 * 60 * 1000).toISOString(),
  };
}

function paginationState(query: { error: Error | null; isFetchingNextPage: boolean }): CursorPaginationState {
  if (query.error instanceof CursorExpiredError) return "expired";
  if (query.error) return "error";
  return query.isFetchingNextPage ? "loading" : "ready";
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
      description="当前读模型不可用；已载入的其它观测内容不受影响。"
      state="error"
      title="观测数据读取失败"
    />
  );
}

function TruncatedNotice({ children = "结果已截断" }: { children?: ReactNode }) {
  return (
    <p className="gf-observability__truncated">
      <Activity aria-hidden="true" size={15} />
      {children}
    </p>
  );
}

function statusTone(status: RunView["status"]): string {
  if (status === "succeeded") return "ok";
  if (["failed", "cancelled", "timed_out"].includes(status)) return "danger";
  return "info";
}

const runColumns: readonly CursorTableColumn<RunView>[] = [
  {
    header: "Run",
    id: "run",
    render: (run) => (
      <div className="gf-observability__table-primary">
        <CopyableText copyLabel="复制 Run ID" scrollable value={run.run_id} />
        <a href={`/observability?run=${encodeURIComponent(run.run_id)}`}>以此 Run 为上下文</a>
      </div>
    ),
  },
  {
    header: "状态",
    id: "status",
    render: (run) => <span className={`u-status u-status--${statusTone(run.status)}`}>{run.status}</span>,
  },
  {
    header: "Attempt / revision",
    id: "revision",
    render: (run) => (
      <span className="gf-observability__table-nowrap">
        attempt {run.attempt_no ?? "未分配"} · revision {run.revision}
      </span>
    ),
  },
  {
    header: "终态清单",
    id: "terminal",
    render: (run) => (
      <code className="gf-observability__terminal-id" tabIndex={0}>
        {run.result_artifact_id ?? run.failure_artifact_id ?? "尚无终态清单"}
      </code>
    ),
  },
];

const traceColumns: readonly CursorTableColumn<TraceSummary>[] = [
  {
    header: "Trace",
    id: "trace",
    render: (trace) => (
      <div className="gf-observability__table-primary">
        <CopyableText copyLabel="复制 Trace ID" value={trace.trace_id} />
        <a href={`/observability/traces/${encodeURIComponent(trace.trace_id)}`}>打开 {trace.trace_id}</a>
      </div>
    ),
  },
  {
    header: "状态",
    id: "status",
    render: (trace) => (
      <span className={`u-status u-status--${traceSummaryTone(trace.status)}`}>{trace.status}</span>
    ),
  },
  {
    header: "Span / services",
    id: "coverage",
    render: (trace) => (
      <span>
        {trace.span_count} spans · {trace.service_names.join(" / ") || "service 未报告"}
      </span>
    ),
  },
  {
    header: "时间",
    id: "time",
    render: (trace) => (
      <span>
        {trace.started_at} · {trace.duration_ns == null ? "duration unavailable" : `${trace.duration_ns} ns`}
      </span>
    ),
  },
];

function WindowControls({ active, onApply }: { active: TimeWindow; onApply(window: TimeWindow): void }) {
  const [start, setStart] = useState(active.startUtc);
  const [end, setEnd] = useState(active.endUtc);
  const [error, setError] = useState<string | null>(null);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const startMs = Date.parse(start);
    const endMs = Date.parse(end);
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs >= endMs) {
      setError("请输入有效 UTC 时间，且结束时间必须晚于开始时间。后端仍会独立执行查询边界校验。");
      return;
    }
    setError(null);
    onApply({ endUtc: new Date(endMs).toISOString(), startUtc: new Date(startMs).toISOString() });
  }

  return (
    <form className="gf-observability__window" onSubmit={submit}>
      <label>
        <span>开始 UTC（ISO 8601）</span>
        <input onChange={(event) => setStart(event.target.value)} required value={start} />
      </label>
      <label>
        <span>结束 UTC（ISO 8601）</span>
        <input onChange={(event) => setEnd(event.target.value)} required value={end} />
      </label>
      <button className="gf-secondary-button" type="submit">
        应用同一时间窗
      </button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}

function exactValue(value: string | undefined): { exact: string | null; plot: number | null } {
  if (value === undefined) return { exact: null, plot: null };
  const plot = Number(value);
  return { exact: value, plot: Number.isFinite(plot) && plot >= 0 ? plot : null };
}

function budgetData(snapshot: RunCostView["budget_set"]["snapshots"][number]): CostBarDatum[] {
  return snapshot.limits.map((limit) => {
    const consumed = snapshot.consumed.find((item) => item.dimension === limit.dimension);
    const reserved = snapshot.reserved.find((item) => item.dimension === limit.dimension);
    return {
      consumed: exactValue(consumed?.value ?? "0"),
      label: limit.dimension,
      limit: exactValue(limit.value),
      reserved: exactValue(reserved?.value ?? "0"),
      unit: limit.currency ? `${limit.unit} ${limit.currency}` : limit.unit,
    };
  });
}

function CostUsage({ item }: { item: RunCostView["usage"][number] }) {
  const tokens = item.token_usage;
  const latency = item.latency;
  const monetary = item.monetary;
  return (
    <article className="gf-observability__usage" data-testid={`cost-usage-${item.usage_id}`}>
      <header>
        <CopyableText copyLabel="复制 Usage ID" value={item.usage_id} />
        <span className="u-status u-status--info">{item.execution_source}</span>
      </header>
      <dl>
        <div>
          <dt>Scope</dt>
          <dd>
            {item.scope} · attempt {item.attempt_no} · transport {item.transport_attempt ?? "N/A"}
          </dd>
        </div>
        <div>
          <dt>Token usage</dt>
          <dd>
            {tokens.status === "unavailable" ? (
              "Token usage unavailable"
            ) : (
              <span>
                Total token {observationValue(tokens.status, tokens.total_tokens)} · input{" "}
                {observationValue(tokens.status, tokens.input_tokens)} · output{" "}
                {observationValue(tokens.status, tokens.output_tokens)} · cache read{" "}
                {observationValue(tokens.status, tokens.cache_read_tokens)} · cache write{" "}
                {observationValue(tokens.status, tokens.cache_write_tokens)}
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt>Provider latency</dt>
          <dd>
            {latency.status === "reported"
              ? `${observationValue(latency.status, latency.provider_latency_ms)} ms`
              : "Latency unavailable"}
          </dd>
        </div>
        <div>
          <dt>Monetary</dt>
          <dd>
            {monetary.status === "reported"
              ? `Monetary ${observationValue(monetary.status, monetary.amount)} ${monetary.currency ?? "currency unavailable"}`
              : "Monetary unavailable"}
          </dd>
        </div>
        <div>
          <dt>Provider prefix cache</dt>
          <dd>
            {item.provider_prefix_cache.status === "reported"
              ? item.provider_prefix_cache.hit
                ? "reported hit"
                : "reported miss"
              : "unavailable"}
          </dd>
        </div>
        <div>
          <dt>Wall time</dt>
          <dd>{item.wall_time_ns} ns</dd>
        </div>
      </dl>
      {item.adjustment_of_usage_id && (
        <p>
          Late adjustment of <code>{item.adjustment_of_usage_id}</code>
        </p>
      )}
    </article>
  );
}

function CostSection({
  costPages,
  error,
  isFetchingNextPage,
  nextCursor,
  onLoadMore,
  onRestart,
}: {
  costPages: readonly RunCostView[];
  error: Error | null;
  isFetchingNextPage: boolean;
  nextCursor: string | null;
  onLoadMore(): void;
  onRestart(): void;
}) {
  const first = costPages[0];
  if (!first) return null;
  const usage = costPages.flatMap((page) => page.usage);
  const summary = first.settlement_summary;
  return (
    <section className="gf-observability__section" id="cost">
      <header className="gf-observability__section-heading">
        <Coins aria-hidden="true" size={21} />
        <div>
          <h2>冻结预算与成本结算</h2>
          <p>预算来自 Run 创建时冻结的 BudgetSetSnapshot；Usage observation 的 unavailable 绝不按 0 呈现。</p>
        </div>
      </header>

      <div className="gf-observability__authority-strip">
        <div>
          <span>Budget set</span>
          <CopyableText copyLabel="复制 Budget Set ID" value={first.budget_set.budget_set_snapshot_id} />
        </div>
        <div>
          <span>Selection policy</span>
          <code>{first.budget_set.selection_policy_version}</code>
        </div>
        <div>
          <span>Captured</span>
          <time dateTime={first.budget_set.captured_at}>{first.budget_set.captured_at}</time>
        </div>
      </div>

      <section className="gf-observability__settlement" aria-label="成本结算摘要">
        <div className="gf-observability__summary-grid">
          <article>
            <span>Usage evidence</span>
            <strong>{summary.usage_evidence_status}</strong>
            {summary.usage_evidence_status === "not_recorded" && <small>不等于 0 成本</small>}
          </article>
          <article>
            <span>Settlement groups</span>
            <strong>{summary.total_group_count}</strong>
          </article>
          <article data-tone={summary.held_unknown_group_count > 0 ? "warning" : "neutral"}>
            <span>Held unknown</span>
            <strong>{summary.held_unknown_group_count}</strong>
          </article>
          <article>
            <span>Late adjustments</span>
            <strong>{summary.late_adjustment_usage_count}</strong>
          </article>
          <article>
            <span>Usage entries</span>
            <strong>{summary.usage_entry_count}</strong>
          </article>
        </div>
        <div className="u-scroll-region" tabIndex={0}>
          <table>
            <caption className="u-sr-only">成本结算 group 状态</caption>
            <thead>
              <tr>
                <th scope="col">Scope</th>
                <th scope="col">Status</th>
                <th scope="col">Count</th>
              </tr>
            </thead>
            <tbody>
              {summary.group_counts.map((row) => (
                <tr key={`${row.scope}:${row.status}`}>
                  <td>{row.scope}</td>
                  <td>{row.status}</td>
                  <td>{row.group_count}</td>
                </tr>
              ))}
              {summary.group_counts.length === 0 && (
                <tr>
                  <td colSpan={3}>没有 settlement group；以 evidence status 解释，不补 0 usage。</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <div className="gf-observability__budget-grid">
        {first.budget_set.snapshots.map((snapshot) => (
          <CostBarChart
            data={budgetData(snapshot)}
            key={snapshot.snapshot_id}
            summary={`${snapshot.scope_kind}:${snapshot.scope_id} · ${snapshot.policy_version} · frozen revision ${snapshot.budget_revision_at_freeze}`}
            title={`预算 · ${snapshot.budget_id}`}
          />
        ))}
      </div>

      <div className="gf-observability__usage-list">
        {usage.map((item) => (
          <CostUsage item={item} key={item.usage_id} />
        ))}
        {usage.length === 0 && (
          <StatePanel
            description="Usage evidence 没有记录；这不是 0 token、0 latency 或 0 monetary 的声明。"
            state="empty"
            title="没有 Usage observation"
          />
        )}
      </div>
      <CursorFooter
        error={error}
        isFetching={isFetchingNextPage}
        nextCursor={nextCursor}
        onLoadMore={onLoadMore}
        onRestart={onRestart}
      />
    </section>
  );
}

function CursorFooter({
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
          <p role="status">分页游标已过期；已载入内容保留，继续读取前需要显式重开。</p>
          <button className="gf-secondary-button" onClick={onRestart} type="button">
            重新开始查询
          </button>
        </>
      ) : error ? (
        <>
          <p role="status">下一页读取失败；已载入内容保留。</p>
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

function MetricSeries({ descriptor, page }: { descriptor: MetricDescriptor; page: MetricPage }) {
  if (page.series.length === 0) {
    return (
      <StatePanel
        description="该 exact descriptor 在当前窗口没有点；空 bucket 不补零，也不插值。"
        state="empty"
        title="该时间窗没有 Metric 点"
      />
    );
  }
  return (
    <div className="gf-observability__metric-grid">
      {page.series.map((series, index) => {
        const points =
          series.metric_type === "histogram"
            ? (series.histogram_points ?? []).map((point) => ({
                label: point.ts_utc,
                value: point.count,
              }))
            : (series.scalar_points ?? []).map((point) => ({
                label: point.ts_utc,
                value: point.value,
              }));
        const labels = Object.entries(series.labels)
          .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
          .map(([key, value]) => `${key}=${value}`)
          .join(" · ");
        const seriesTitle = `${descriptor.metric_name} · ${index + 1}`;
        return (
          <div
            className="gf-observability__metric-series"
            key={`${series.descriptor.descriptor_digest}:${labels}:${index}`}
          >
            <AreaSparkChart
              data={points}
              summary={`${labels || "无 labels"} · exact descriptor v${series.descriptor.descriptor_version} · ${series.unit} · resolution ${page.effective_resolution_s}s · coverage ${page.coverage_start} → ${page.coverage_end}`}
              title={seriesTitle}
              valueLabel={series.metric_type === "histogram" ? "count" : series.unit}
            />
            {series.metric_type === "histogram" && (
              <HistogramDetails
                bounds={series.bucket_bounds ?? descriptor.histogram_bucket_bounds}
                metricName={descriptor.metric_name}
                points={series.histogram_points ?? []}
                seriesLabel={labels || `series ${index + 1}`}
                unit={series.unit}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

function HistogramDetails({
  bounds,
  metricName,
  points,
  seriesLabel,
  unit,
}: {
  bounds: readonly number[];
  metricName: string;
  points: NonNullable<MetricPage["series"][number]["histogram_points"]>;
  seriesLabel: string;
  unit: string;
}) {
  return (
    <details className="gf-observability__histogram">
      <summary>查看 exact histogram buckets · {seriesLabel}</summary>
      <div className="u-scroll-region" tabIndex={0}>
        <table aria-label={`${metricName} histogram buckets`}>
          <thead>
            <tr>
              <th scope="col">UTC bucket</th>
              <th scope="col">Count</th>
              <th scope="col">Sum ({unit})</th>
              {bounds.map((bound) => (
                <th key={bound} scope="col">
                  ≤ {bound} {unit}
                </th>
              ))}
              <th scope="col">+Inf</th>
            </tr>
          </thead>
          <tbody>
            {points.map((point) => (
              <tr key={point.ts_utc}>
                <th scope="row">{point.ts_utc}</th>
                <td>{point.count}</td>
                <td>{point.sum ?? "unavailable"}</td>
                {point.cumulative_bucket_counts.map((count, index) => (
                  <td key={index}>{count}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

export function ObservabilityPage({
  api = observabilityApi,
  now = () => new Date(),
}: {
  api?: ObservabilityApi;
  now?: () => Date;
}) {
  const [searchParams] = useSearchParams();
  const selectedRunId = searchParams.get("run");
  const [window, setWindow] = useState<TimeWindow>(() => defaultWindow(now()));
  const [resolutionSeconds, setResolutionSeconds] = useState(60);
  const [descriptorKey, setDescriptorKey] = useState<string | null>(null);
  const [runEpoch, setRunEpoch] = useState(0);
  const [traceEpoch, setTraceEpoch] = useState(0);
  const [logEpoch, setLogEpoch] = useState(0);
  const [metricEpoch, setMetricEpoch] = useState(0);
  const [costEpoch, setCostEpoch] = useState(0);

  const runsQuery = useInfiniteQuery<
    RunPage,
    Error,
    InfiniteData<RunPage>,
    readonly unknown[],
    string | null
  >({
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => api.listRuns(pageParam),
    queryKey: ["observability", "runs", runEpoch],
    retry: false,
  });
  const runQuery = useQuery({
    enabled: selectedRunId !== null,
    queryFn: async () => requireRunOwner(await api.getRun(selectedRunId!), selectedRunId!),
    queryKey: ["observability", "run", selectedRunId],
    retry: false,
  });
  const exactRunOwner = runQuery.data?.run_id === selectedRunId;
  const tracesQuery = useInfiniteQuery<
    TraceSummaryPage,
    Error,
    InfiniteData<TraceSummaryPage>,
    readonly unknown[],
    string | null
  >({
    enabled: exactRunOwner,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      requireRunTracePageOwner(await api.listRunTraces(selectedRunId!, pageParam), selectedRunId!),
    queryKey: ["observability", "traces", selectedRunId, traceEpoch],
    retry: false,
  });
  const logsQuery = useInfiniteQuery<
    LogPage,
    Error,
    InfiniteData<LogPage>,
    readonly unknown[],
    string | null
  >({
    enabled: exactRunOwner,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => api.queryLogs({ cursor: pageParam, ...window, runId: selectedRunId! }),
    queryKey: ["observability", "logs", selectedRunId, window, logEpoch],
    retry: false,
  });
  const costQuery = useInfiniteQuery<
    RunCostView,
    Error,
    InfiniteData<RunCostView>,
    readonly unknown[],
    string | null
  >({
    enabled: exactRunOwner,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) =>
      requireRunCostOwner(await api.getRunCost(selectedRunId!, pageParam), selectedRunId!),
    queryKey: ["observability", "cost", selectedRunId, costEpoch],
    retry: false,
  });
  const descriptorsQuery = useQuery({
    queryFn: () => api.getMetricDescriptors(),
    queryKey: ["observability", "metric-descriptors"],
    retry: false,
  });
  const descriptors = descriptorsQuery.data?.descriptors ?? [];
  const selectedDescriptor =
    descriptors.find((item) =>
      descriptorKey === null
        ? false
        : `${item.metric_name}:${item.descriptor_version}:${item.descriptor_digest}` === descriptorKey,
    ) ?? descriptors[0];
  const metricQuery = useInfiniteQuery<
    MetricPage,
    Error,
    InfiniteData<MetricPage>,
    readonly unknown[],
    string | null
  >({
    enabled: selectedDescriptor !== undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) => {
      const page = await api.queryMetrics({
        cursor: pageParam,
        descriptorRefs: [descriptorRef(selectedDescriptor!)],
        maxPoints: 240,
        resolutionSeconds,
        seriesLimit: Math.min(16, selectedDescriptor!.series_limit),
        ...window,
      });
      requireExactMetricSeries(descriptors, [descriptorRef(selectedDescriptor!)], page);
      return page;
    },
    queryKey: [
      "observability",
      "metrics",
      selectedDescriptor?.metric_name,
      selectedDescriptor?.descriptor_version,
      selectedDescriptor?.descriptor_digest,
      resolutionSeconds,
      window,
      metricEpoch,
    ],
    retry: false,
  });

  const runPages = runsQuery.data?.pages ?? [];
  const runSnapshotIds = new Set(runPages.map((page) => page.read_snapshot_id));
  const runSnapshotMismatch = runSnapshotIds.size > 1;
  const runs = runPages.flatMap((page) => page.items);
  const tracePages = tracesQuery.data?.pages ?? [];
  const traces = tracePages.flatMap((page) => page.items);
  const logPages = logsQuery.data?.pages ?? [];
  const logs = logPages.flatMap((page) => page.items);
  const costPages = costQuery.data?.pages ?? [];
  const metricPages = metricQuery.data?.pages ?? [];
  const nextRunCursor = runPages[runPages.length - 1]?.next_cursor ?? null;
  const nextTraceCursor = tracePages[tracePages.length - 1]?.next_cursor ?? null;
  const nextLogCursor = logPages[logPages.length - 1]?.next_cursor ?? null;
  const nextCostCursor = costPages[costPages.length - 1]?.next_cursor ?? null;
  const nextMetricCursor = metricPages[metricPages.length - 1]?.next_cursor ?? null;
  const run = runQuery.data;

  const descriptorSelectValue = selectedDescriptor
    ? `${selectedDescriptor.metric_name}:${selectedDescriptor.descriptor_version}:${selectedDescriptor.descriptor_digest}`
    : "";

  return (
    <div className="gf-page gf-observability" data-layout="editorial-observability">
      <header className="gf-observability__hero">
        <div>
          <p className="gf-observability__kicker">Run correlation · bounded reads · redacted evidence</p>
          <h1>可观测性</h1>
          <p>从已授权 Run 进入 Trace、Span、日志与冻结成本；Metric 保持系统时序语义，不伪装成单 Run 指标。</p>
        </div>
        <div className="gf-observability__hero-mark" aria-hidden="true">
          <Network size={30} />
          <span>OBSERVE</span>
        </div>
      </header>

      {runsQuery.isPending ? (
        <StatePanel description="正在读取已授权 Run 快照。" state="loading" title="正在读取 Run" />
      ) : runsQuery.isError && runPages.length === 0 ? (
        <ReadError error={runsQuery.error} onRetry={() => void runsQuery.refetch()} />
      ) : runSnapshotMismatch ? (
        <StatePanel
          description="分页返回了不同 read snapshot；页面已停止混合这些行。"
          state="error"
          title="Run 快照不一致"
        />
      ) : (
        <CursorTable
          caption="已授权 Run"
          columns={runColumns}
          getRowKey={(item) => item.run_id}
          headingLevel={2}
          items={runs}
          nextCursor={nextRunCursor}
          onLoadMore={() => void runsQuery.fetchNextPage()}
          onRestart={() => setRunEpoch((value) => value + 1)}
          paginationState={paginationState(runsQuery)}
          toolbar={
            <span className="u-small">read snapshot · {runPages[0]?.read_snapshot_id ?? "pending"}</span>
          }
        />
      )}

      <section className="gf-observability__section" id="run-context">
        <header className="gf-observability__section-heading">
          <Activity aria-hidden="true" size={21} />
          <div>
            <h2>当前 Run context</h2>
            <p>只有从已授权列表或精确 deep link 读取到的 Run 才成为本页上下文。</p>
          </div>
        </header>
        {selectedRunId === null ? (
          <StatePanel
            description="从上方列表选择一个 Run，再读取其 Trace、日志与成本。"
            state="empty"
            title="尚未选择 Run"
          />
        ) : runQuery.isPending ? (
          <StatePanel description={`正在读取 ${selectedRunId}`} state="loading" title="正在绑定 Run" />
        ) : runQuery.isError ? (
          <ReadError error={runQuery.error} onRetry={() => void runQuery.refetch()} />
        ) : run ? (
          <div className="gf-observability__run-context">
            <div>
              <span>Run ID</span>
              <CopyableText copyLabel="复制当前 Run ID" value={run.run_id} />
            </div>
            <div>
              <span>Status</span>
              <strong className={`u-status u-status--${statusTone(run.status)}`}>{run.status}</strong>
            </div>
            <div>
              <span>Attempt</span>
              <strong>{run.attempt_no ?? "未分配"}</strong>
            </div>
            <div>
              <span>Revision</span>
              <strong>{run.revision}</strong>
            </div>
            <a className="gf-secondary-button" href={`/runs/${encodeURIComponent(run.run_id)}`}>
              打开 Run 详情
            </a>
          </div>
        ) : null}
      </section>

      {run && (
        <section className="gf-observability__section" id="traces">
          <header className="gf-observability__section-heading">
            <Network aria-hidden="true" size={21} />
            <div>
              <h2>Run → Trace</h2>
              <p>
                Trace 列表由 <code>/runs/{"{run_id}"}/traces</code> 精确绑定，不按时间猜测所属 Run。
              </p>
            </div>
          </header>
          {tracesQuery.isPending ? (
            <StatePanel description="正在读取该 Run 的 Trace。" state="loading" title="正在读取 Trace" />
          ) : tracesQuery.isError && tracePages.length === 0 ? (
            <ReadError error={tracesQuery.error} onRetry={() => void tracesQuery.refetch()} />
          ) : (
            <>
              {tracePages.some((page) => page.truncated) && (
                <TruncatedNotice>Trace page 已截断</TruncatedNotice>
              )}
              <CursorTable
                caption="该 Run 的 Trace"
                columns={traceColumns}
                getRowKey={(item) => item.trace_id}
                items={traces}
                nextCursor={nextTraceCursor}
                onLoadMore={() => void tracesQuery.fetchNextPage()}
                onRestart={() => setTraceEpoch((value) => value + 1)}
                paginationState={paginationState(tracesQuery)}
                toolbar={
                  tracePages[0] ? (
                    <span className="u-small">
                      {tracePages[0].coverage_start} → {tracePages[0].coverage_end}
                    </span>
                  ) : null
                }
              />
            </>
          )}
        </section>
      )}

      <section className="gf-observability__section" id="time-window">
        <header className="gf-observability__section-heading">
          <TimerReset aria-hidden="true" size={21} />
          <div>
            <h2>查询时间窗</h2>
            <p>Run 日志与系统 Metric 使用同一显式 UTC `[start,end)` 时间窗；后端仍独立执行有界校验。</p>
          </div>
        </header>
        <WindowControls
          active={window}
          onApply={(next) => {
            setWindow(next);
            setLogEpoch((value) => value + 1);
            setMetricEpoch((value) => value + 1);
          }}
        />
      </section>

      {run && (
        <section className="gf-observability__section" id="logs">
          <header className="gf-observability__section-heading">
            <Logs aria-hidden="true" size={21} />
            <div>
              <h2>Run 日志</h2>
              <p>只显示服务端脱敏字段，并在 UI 再阻止 prompt/raw response 等敏感键。</p>
            </div>
          </header>
          {logsQuery.isPending ? (
            <StatePanel description="正在读取 Run 日志。" state="loading" title="正在读取日志" />
          ) : logsQuery.isError && logPages.length === 0 ? (
            <ReadError error={logsQuery.error} onRetry={() => void logsQuery.refetch()} />
          ) : (
            <>
              {logPages.some((page) => page.truncated) && <TruncatedNotice>Log page 已截断</TruncatedNotice>}
              {logPages[0] && (
                <p className="u-small">
                  实际 coverage · {logPages[0].coverage_start} → {logPages[0].coverage_end}
                </p>
              )}
              <LogExplorer items={logs} title="脱敏日志记录" />
              <CursorFooter
                error={logsQuery.error}
                isFetching={logsQuery.isFetchingNextPage}
                nextCursor={nextLogCursor}
                onLoadMore={() => void logsQuery.fetchNextPage()}
                onRestart={() => setLogEpoch((value) => value + 1)}
              />
            </>
          )}
        </section>
      )}

      <section className="gf-observability__section" id="metrics">
        <header className="gf-observability__section-heading">
          <Gauge aria-hidden="true" size={21} />
          <div>
            <h2>系统 Metric</h2>
            <p>
              这里是同一时间窗的系统运营指标。Descriptor 禁止 run/trace/span/artifact/principal 高基数 label，
              因此这些序列不能归因于当前 Run。
            </p>
          </div>
        </header>
        {descriptorsQuery.isPending ? (
          <StatePanel
            description="正在读取 exact descriptor registry。"
            state="loading"
            title="正在读取 Metric 描述符"
          />
        ) : descriptorsQuery.isError ? (
          <ReadError error={descriptorsQuery.error} onRetry={() => void descriptorsQuery.refetch()} />
        ) : descriptors.length === 0 ? (
          <StatePanel
            description="本地 descriptor registry 尚未就绪；页面没有猜测指标名称。"
            state="empty"
            title="没有可查询的 Metric descriptor"
          />
        ) : selectedDescriptor ? (
          <>
            <div className="gf-observability__metric-controls">
              <label>
                <span>Exact descriptor</span>
                <select
                  onChange={(event) => setDescriptorKey(event.target.value)}
                  value={descriptorSelectValue}
                >
                  {descriptors.map((descriptor) => {
                    const key = `${descriptor.metric_name}:${descriptor.descriptor_version}:${descriptor.descriptor_digest}`;
                    return (
                      <option key={key} value={key}>
                        {descriptor.metric_name} · v{descriptor.descriptor_version} · {descriptor.unit}
                      </option>
                    );
                  })}
                </select>
              </label>
              <label>
                <span>Resolution</span>
                <select
                  onChange={(event) => setResolutionSeconds(Number(event.target.value))}
                  value={resolutionSeconds}
                >
                  <option value={60}>60 s</option>
                  <option value={300}>300 s</option>
                  <option value={900}>900 s</option>
                </select>
              </label>
            </div>
            <div className="gf-observability__descriptor">
              <div>
                <span>Metric</span>
                <code>{selectedDescriptor.metric_name}</code>
              </div>
              <div>
                <span>Version / type / unit</span>
                <strong>
                  descriptor v{selectedDescriptor.descriptor_version} · {selectedDescriptor.metric_type} ·{" "}
                  {selectedDescriptor.unit}
                </strong>
              </div>
              <div>
                <span>Exact digest</span>
                <CopyableText
                  copyLabel="复制 descriptor digest"
                  value={selectedDescriptor.descriptor_digest}
                />
              </div>
              <div>
                <span>Labels</span>
                <code>{selectedDescriptor.label_keys.join(", ") || "none"}</code>
              </div>
            </div>
            {metricQuery.isPending ? (
              <StatePanel
                description="正在按 exact descriptor 查询。"
                state="loading"
                title="正在读取 Metric"
              />
            ) : metricQuery.isError && metricPages.length === 0 ? (
              <ReadError error={metricQuery.error} onRetry={() => void metricQuery.refetch()} />
            ) : (
              <>
                {metricPages.some((page) => page.truncated) && (
                  <TruncatedNotice>Metric page 已截断</TruncatedNotice>
                )}
                {metricPages.map((page, index) => (
                  <MetricSeries descriptor={selectedDescriptor} key={index} page={page} />
                ))}
                <CursorFooter
                  error={metricQuery.error}
                  isFetching={metricQuery.isFetchingNextPage}
                  nextCursor={nextMetricCursor}
                  onLoadMore={() => void metricQuery.fetchNextPage()}
                  onRestart={() => setMetricEpoch((value) => value + 1)}
                />
              </>
            )}
          </>
        ) : null}
      </section>

      {run &&
        (costQuery.isPending ? (
          <StatePanel description="正在读取冻结预算和成本结算。" state="loading" title="正在读取成本" />
        ) : costQuery.isError && costPages.length === 0 ? (
          <ReadError error={costQuery.error} onRetry={() => void costQuery.refetch()} />
        ) : (
          <CostSection
            costPages={costPages}
            error={costQuery.error}
            isFetchingNextPage={costQuery.isFetchingNextPage}
            nextCursor={nextCostCursor}
            onLoadMore={() => void costQuery.fetchNextPage()}
            onRestart={() => setCostEpoch((value) => value + 1)}
          />
        ))}
    </div>
  );
}
