import { useInfiniteQuery, useQuery, type InfiniteData } from "@tanstack/react-query";
import { Activity, Coins, Gauge, Logs, Network, TimerReset } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { AreaSparkChart, CostBarChart, type CostBarDatum } from "../../components/charts";
import { compactDateTime, ResourceIdentity, TechnicalDetails } from "../../components/identity";
import { LogExplorer } from "../../components/logs";
import { CursorTable, type CursorPaginationState, type CursorTableColumn } from "../../components/tables";
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
import { PRODUCT_TIME_ZONE, timestampToLocalInput, zonedLocalToIso } from "../time";

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

const runStatusLabels: Record<RunView["status"], string> = {
  cancelled: "已取消",
  failed: "失败",
  leased: "已分配",
  queued: "排队中",
  retry_wait: "等待重试",
  running: "运行中",
  succeeded: "成功",
  timed_out: "已超时",
};

function traceStatusLabel(status: TraceSummary["status"]): string {
  return status === "ok" ? "正常" : status === "error" ? "异常" : "状态未知";
}

function durationLabel(durationNs: number | null | undefined): string {
  if (durationNs === null || durationNs === undefined) return "耗时未记录";
  const milliseconds = durationNs / 1_000_000;
  return milliseconds >= 1_000
    ? `${(milliseconds / 1_000).toFixed(2)} 秒`
    : `${milliseconds.toFixed(1)} 毫秒`;
}

const runColumns: readonly CursorTableColumn<RunView>[] = [
  {
    header: "运行记录",
    id: "run",
    render: (run) => (
      <ResourceIdentity
        actionLabel="查看这次运行"
        description={`第 ${run.attempt_no ?? 1} 次尝试`}
        details={[{ label: "Run ID", value: run.run_id }]}
        href={`/observability?run=${encodeURIComponent(run.run_id)}`}
        title={`运行记录 · 第 ${run.revision} 版`}
      />
    ),
  },
  {
    header: "状态",
    id: "status",
    render: (run) => (
      <span className={`u-status u-status--${statusTone(run.status)}`}>{runStatusLabels[run.status]}</span>
    ),
  },
  {
    header: "执行次数 / 内容版本",
    id: "revision",
    render: (run) => (
      <span className="gf-observability__table-nowrap">
        第 {run.attempt_no ?? "—"} 次尝试 · 第 {run.revision} 版
      </span>
    ),
  },
  {
    header: "终态清单",
    id: "terminal",
    render: (run) => {
      const terminalId = run.result_artifact_id ?? run.failure_artifact_id;
      if (!terminalId) return <span>运行中，暂无最终结果</span>;
      return (
        <ResourceIdentity
          details={[
            {
              label: run.result_artifact_id ? "结果 Artifact ID" : "失败 Artifact ID",
              value: terminalId,
            },
          ]}
          title={run.result_artifact_id ? "已生成结果" : "已记录失败原因"}
        />
      );
    },
  },
];

const traceColumns: readonly CursorTableColumn<TraceSummary>[] = [
  {
    header: "调用链",
    id: "trace",
    render: (trace) => (
      <ResourceIdentity
        actionLabel="查看调用链"
        description={compactDateTime(trace.started_at)}
        details={[{ label: "Trace ID", value: trace.trace_id }]}
        href={`/observability/traces/${encodeURIComponent(trace.trace_id)}`}
        title="调用链记录"
      />
    ),
  },
  {
    header: "状态",
    id: "status",
    render: (trace) => (
      <span className={`u-status u-status--${traceSummaryTone(trace.status)}`}>
        {traceStatusLabel(trace.status)}
      </span>
    ),
  },
  {
    header: "步骤 / 服务",
    id: "coverage",
    render: (trace) => (
      <span>
        {trace.span_count} 个步骤 · {trace.service_names.join(" / ") || "服务未报告"}
      </span>
    ),
  },
  {
    header: "时间",
    id: "time",
    render: (trace) => (
      <span>
        {compactDateTime(trace.started_at)} · {durationLabel(trace.duration_ns)}
      </span>
    ),
  },
];

function WindowControls({ active, onApply }: { active: TimeWindow; onApply(window: TimeWindow): void }) {
  // The window travels as UTC but a planner reads and edits it in local time.
  const [start, setStart] = useState(() => timestampToLocalInput(active.startUtc, PRODUCT_TIME_ZONE));
  const [end, setEnd] = useState(() => timestampToLocalInput(active.endUtc, PRODUCT_TIME_ZONE));
  const [error, setError] = useState<string | null>(null);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let startMs: number;
    let endMs: number;
    try {
      startMs = Date.parse(zonedLocalToIso(start, PRODUCT_TIME_ZONE));
      endMs = Date.parse(zonedLocalToIso(end, PRODUCT_TIME_ZONE));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "请填写完整的日期和时间。");
      return;
    }
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs >= endMs) {
      setError("请填写有效时间，且结束时间必须晚于开始时间。");
      return;
    }
    setError(null);
    onApply({
      endUtc: new Date(endMs).toISOString(),
      startUtc: new Date(startMs).toISOString(),
    });
  }

  return (
    <form className="gf-observability__window" onSubmit={submit}>
      <label>
        <span>开始时间</span>
        <input
          onChange={(event) => setStart(event.target.value)}
          required
          type="datetime-local"
          value={start}
        />
      </label>
      <label>
        <span>结束时间</span>
        <input onChange={(event) => setEnd(event.target.value)} required type="datetime-local" value={end} />
      </label>
      <button className="gf-secondary-button" type="submit">
        应用同一时间窗
      </button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}

function exactValue(value: string | undefined): {
  exact: string | null;
  plot: number | null;
} {
  if (value === undefined) return { exact: null, plot: null };
  const plot = Number(value);
  return {
    exact: value,
    plot: Number.isFinite(plot) && plot >= 0 ? plot : null,
  };
}

function budgetData(snapshot: RunCostView["budget_set"]["snapshots"][number]): CostBarDatum[] {
  return snapshot.limits.map((limit) => {
    const consumed = snapshot.consumed.find((item) => item.dimension === limit.dimension);
    const reserved = snapshot.reserved.find((item) => item.dimension === limit.dimension);
    return {
      consumed: exactValue(consumed?.value ?? "0"),
      label: budgetDimensionLabel(limit.dimension),
      limit: exactValue(limit.value),
      reserved: exactValue(reserved?.value ?? "0"),
      unit: limit.currency ? `${limit.unit} ${limit.currency}` : limit.unit,
    };
  });
}

function budgetDimensionLabel(dimension: string): string {
  const labels: Record<string, string> = {
    agent_step: "智能助手步骤",
    cache_read_token: "缓存读取量",
    cache_write_token: "缓存写入量",
    concurrent_run: "并发运行数",
    input_token: "输入量",
    monetary: "费用",
    output_token: "输出量",
    request: "请求次数",
    wall_time_ns: "运行时长",
  };
  return labels[dimension] ?? dimension;
}

function executionSourceLabel(source: RunCostView["usage"][number]["execution_source"]): string {
  if (source === "online") return "在线调用";
  if (source === "full_response_cache") return "结果缓存";
  return "固定回放";
}

function usageScopeLabel(scope: RunCostView["usage"][number]["scope"]): string {
  return scope === "attempt_call" ? "一次模型调用" : "一个智能助手步骤";
}

function settlementScopeLabel(scope: string): string {
  if (scope === "run_budget_hold") return "运行预算预留";
  if (scope === "attempt_call") return "模型调用";
  if (scope === "agent_step") return "智能助手步骤";
  return scope;
}

function settlementStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    conservatively_settled: "按上限保守结算",
    held_unknown: "等待用量确认",
    late_reconciled: "延迟数据已补算",
    reconciled: "已按实际用量结算",
    released: "已释放",
    reserved: "已预留",
  };
  return labels[status] ?? status;
}

function metricTypeLabel(metricType: string): string {
  if (metricType === "counter") return "累计计数";
  if (metricType === "gauge") return "即时数值";
  if (metricType === "histogram") return "区间分布";
  return metricType;
}

function metricUnitLabel(unit: string): string {
  const labels: Record<string, string> = {
    bytes: "字节",
    count: "个",
    currency: "货币单位",
    milliseconds: "毫秒",
    request: "次请求",
    seconds: "秒",
  };
  return labels[unit] ?? unit;
}

function CostUsage({ item }: { item: RunCostView["usage"][number] }) {
  const tokens = item.token_usage;
  const latency = item.latency;
  const monetary = item.monetary;
  return (
    <article className="gf-observability__usage" data-testid={`cost-usage-${item.usage_id}`}>
      <header>
        <strong>用量记录 · 第 {item.attempt_no} 次执行</strong>
        <span className="u-status u-status--info">{executionSourceLabel(item.execution_source)}</span>
      </header>
      <TechnicalDetails items={[{ label: "用量记录 ID", value: item.usage_id }]} summary="查看用量技术信息" />
      <dl>
        <div>
          <dt>计量范围</dt>
          <dd>
            {usageScopeLabel(item.scope)} · 第 {item.attempt_no} 次执行
            {item.transport_attempt === null || item.transport_attempt === undefined
              ? ""
              : ` · 第 ${item.transport_attempt} 次传输`}
          </dd>
        </div>
        <div>
          <dt>模型用量</dt>
          <dd>
            {tokens.status === "unavailable" ? (
              "服务商未提供用量"
            ) : (
              <span>
                总计 {observationValue(tokens.status, tokens.total_tokens)} · 输入{" "}
                {observationValue(tokens.status, tokens.input_tokens)} · 输出{" "}
                {observationValue(tokens.status, tokens.output_tokens)} · 读取缓存{" "}
                {observationValue(tokens.status, tokens.cache_read_tokens)} · 写入缓存{" "}
                {observationValue(tokens.status, tokens.cache_write_tokens)}
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt>模型服务耗时</dt>
          <dd>
            {latency.status === "reported"
              ? `${observationValue(latency.status, latency.provider_latency_ms)} 毫秒`
              : "服务商未提供耗时"}
          </dd>
        </div>
        <div>
          <dt>模型费用</dt>
          <dd>
            {monetary.status === "reported"
              ? `${observationValue(monetary.status, monetary.amount)} ${monetary.currency ?? "币种未提供"}`
              : "服务商未提供费用"}
          </dd>
        </div>
        <div>
          <dt>模型缓存</dt>
          <dd>
            {item.provider_prefix_cache.status === "reported"
              ? item.provider_prefix_cache.hit
                ? "已命中"
                : "未命中"
              : "服务商未提供"}
          </dd>
        </div>
        <div>
          <dt>总耗时</dt>
          <dd>{durationLabel(item.wall_time_ns)}</dd>
        </div>
      </dl>
      {item.adjustment_of_usage_id && (
        <TechnicalDetails
          items={[{ label: "被调整的 Usage ID", value: item.adjustment_of_usage_id }]}
          summary="查看延迟调整技术信息"
        />
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
          <p>预算以运行开始时的规则为准；服务商未提供的用量会明确标记，不会被误算成 0。</p>
        </div>
      </header>

      <div className="gf-observability__authority-strip">
        <div>
          <span>预算状态</span>
          <strong>已按运行开始时的规则冻结</strong>
        </div>
        <div>
          <span>预算选择规则</span>
          <code>{first.budget_set.selection_policy_version}</code>
        </div>
        <div>
          <span>冻结时间</span>
          <time dateTime={first.budget_set.captured_at}>{compactDateTime(first.budget_set.captured_at)}</time>
        </div>
        <TechnicalDetails
          items={[
            {
              label: "Budget Set ID",
              value: first.budget_set.budget_set_snapshot_id,
            },
            ...first.budget_set.snapshots.flatMap((snapshot, index) => [
              {
                label: `预算 ${index + 1} Snapshot ID`,
                value: snapshot.snapshot_id,
              },
              {
                label: `预算 ${index + 1} Budget ID`,
                value: snapshot.budget_id,
              },
              { label: `预算 ${index + 1} Scope ID`, value: snapshot.scope_id },
            ]),
          ]}
          summary="查看预算技术信息"
        />
      </div>

      <section className="gf-observability__settlement" aria-label="成本结算摘要">
        <div className="gf-observability__summary-grid">
          <article>
            <span>用量证据</span>
            <strong>{summary.usage_evidence_status === "recorded" ? "已记录" : "未记录"}</strong>
            {summary.usage_evidence_status === "not_recorded" && <small>不等于 0 成本</small>}
          </article>
          <article>
            <span>结算项目</span>
            <strong>{summary.total_group_count}</strong>
          </article>
          <article data-tone={summary.held_unknown_group_count > 0 ? "warning" : "neutral"}>
            <span>等待确认</span>
            <strong>{summary.held_unknown_group_count}</strong>
          </article>
          <article>
            <span>延迟补算</span>
            <strong>{summary.late_adjustment_usage_count}</strong>
          </article>
          <article>
            <span>用量记录</span>
            <strong>{summary.usage_entry_count}</strong>
          </article>
        </div>
        <div className="u-scroll-region" tabIndex={0}>
          <table>
            <caption className="u-sr-only">成本结算项目状态</caption>
            <thead>
              <tr>
                <th scope="col">范围</th>
                <th scope="col">状态</th>
                <th scope="col">数量</th>
              </tr>
            </thead>
            <tbody>
              {summary.group_counts.map((row) => (
                <tr key={`${row.scope}:${row.status}`}>
                  <td>{settlementScopeLabel(row.scope)}</td>
                  <td>{settlementStatusLabel(row.status)}</td>
                  <td>{row.group_count}</td>
                </tr>
              ))}
              {summary.group_counts.length === 0 && (
                <tr>
                  <td colSpan={3}>没有结算项目；请按用量证据状态理解，不会自动补成 0。</td>
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
            summary={`${snapshot.scope_kind} · ${snapshot.policy_version} · 第 ${snapshot.budget_revision_at_freeze} 版冻结规则`}
            title={`预算上限 · 第 ${snapshot.budget_revision_at_freeze} 版`}
          />
        ))}
      </div>

      <div className="gf-observability__usage-list">
        {usage.map((item) => (
          <CostUsage item={item} key={item.usage_id} />
        ))}
        {usage.length === 0 && (
          <StatePanel
            description="没有记录到用量证据；这不代表用量、耗时或费用为 0。"
            state="empty"
            title="没有用量记录"
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
        description="该指标在当前时间范围内没有数据；空白时段不会自动补零或推算。"
        state="empty"
        title="该时间范围没有指标数据"
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
              summary={`${labels || "无分组维度"} · 第 ${series.descriptor.descriptor_version} 版 · ${series.unit} · 每 ${page.effective_resolution_s} 秒汇总 · ${page.coverage_start} → ${page.coverage_end}`}
              title={seriesTitle}
              valueLabel={series.metric_type === "histogram" ? "数量" : series.unit}
            />
            {series.metric_type === "histogram" && (
              <HistogramDetails
                bounds={series.bucket_bounds ?? descriptor.histogram_bucket_bounds}
                metricName={descriptor.metric_name}
                points={series.histogram_points ?? []}
                seriesLabel={labels || `数据组 ${index + 1}`}
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
      <summary>查看详细分布区间 · {seriesLabel}</summary>
      <div className="u-scroll-region" tabIndex={0}>
        <table aria-label={`${metricName} 详细分布区间`}>
          <thead>
            <tr>
              <th scope="col">记录时间</th>
              <th scope="col">数量</th>
              <th scope="col">合计（{unit}）</th>
              {bounds.map((bound) => (
                <th key={bound} scope="col">
                  ≤ {bound} {unit}
                </th>
              ))}
              <th scope="col">超出上限</th>
            </tr>
          </thead>
          <tbody>
            {points.map((point) => (
              <tr key={point.ts_utc}>
                <th scope="row">{point.ts_utc}</th>
                <td>{point.count}</td>
                <td>{point.sum ?? "未提供"}</td>
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
          <p className="gf-observability__kicker">运行状态 · 日志 · 成本</p>
          <h1>运行监控</h1>
          <p>先选择一次系统运行，再查看调用链、日志和成本；技术标识仅在需要排障时展开。</p>
        </div>
        <div className="gf-observability__hero-mark" aria-hidden="true">
          <Network size={30} />
          <span>监控</span>
        </div>
      </header>

      {runsQuery.isPending ? (
        <StatePanel description="正在读取你有权查看的运行记录。" state="loading" title="正在读取运行记录" />
      ) : runsQuery.isError && runPages.length === 0 ? (
        <ReadError error={runsQuery.error} onRetry={() => void runsQuery.refetch()} />
      ) : runSnapshotMismatch ? (
        <StatePanel
          description="分页期间数据版本发生变化；页面已停止混合不同批次的记录。"
          state="error"
          title="运行记录版本不一致"
        />
      ) : (
        <CursorTable
          caption="可查看的运行记录"
          columns={runColumns}
          getRowKey={(item) => item.run_id}
          headingLevel={2}
          items={runs}
          nextCursor={nextRunCursor}
          onLoadMore={() => void runsQuery.fetchNextPage()}
          onRestart={() => setRunEpoch((value) => value + 1)}
          paginationState={paginationState(runsQuery)}
          toolbar={
            <TechnicalDetails
              items={[
                {
                  label: "读取快照",
                  value: runPages[0]?.read_snapshot_id ?? "pending",
                },
              ]}
              summary="查看列表技术信息"
            />
          }
        />
      )}

      <section className="gf-observability__section" id="run-context">
        <header className="gf-observability__section-heading">
          <Activity aria-hidden="true" size={21} />
          <div>
            <h2>当前查看的运行</h2>
            <p>从上方列表选择一项后，这里会显示该次运行的详细监控信息。</p>
          </div>
        </header>
        {selectedRunId === null ? (
          <StatePanel
            description="从上方列表选择一次运行，再查看它的调用链、日志与成本。"
            state="empty"
            title="尚未选择运行记录"
          />
        ) : runQuery.isPending ? (
          <StatePanel description="正在读取这次运行的详细信息。" state="loading" title="正在打开运行记录" />
        ) : runQuery.isError ? (
          <ReadError error={runQuery.error} onRetry={() => void runQuery.refetch()} />
        ) : run ? (
          <div className="gf-observability__run-context">
            <div>
              <span>运行记录</span>
              <strong>第 {run.revision} 版内容</strong>
            </div>
            <div>
              <span>状态</span>
              <strong className={`u-status u-status--${statusTone(run.status)}`}>
                {runStatusLabels[run.status]}
              </strong>
            </div>
            <div>
              <span>执行次数</span>
              <strong>{run.attempt_no ?? "未分配"}</strong>
            </div>
            <div>
              <span>内容版本</span>
              <strong>{run.revision}</strong>
            </div>
            <TechnicalDetails items={[{ label: "Run ID", value: run.run_id }]} summary="查看运行技术信息" />
            <a className="gf-secondary-button" href={`/runs/${encodeURIComponent(run.run_id)}`}>
              查看完整运行记录
            </a>
          </div>
        ) : null}
      </section>

      {run && (
        <section className="gf-observability__section" id="traces">
          <header className="gf-observability__section-heading">
            <Network aria-hidden="true" size={21} />
            <div>
              <h2>调用链</h2>
              <p>这里仅显示与当前运行精确关联的调用链，不会按时间范围猜测归属。</p>
            </div>
          </header>
          {tracesQuery.isPending ? (
            <StatePanel description="正在读取这次运行的调用链。" state="loading" title="正在读取调用链" />
          ) : tracesQuery.isError && tracePages.length === 0 ? (
            <ReadError error={tracesQuery.error} onRetry={() => void tracesQuery.refetch()} />
          ) : (
            <>
              {tracePages.some((page) => page.truncated) && (
                <TruncatedNotice>调用链结果已截断</TruncatedNotice>
              )}
              <CursorTable
                caption="这次运行的调用链"
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
            <p>运行日志与系统指标使用同一时间范围，便于对照定位问题。</p>
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
              <h2>运行日志</h2>
              <p>这里只显示已脱敏的信息；提示词、模型原始回复等敏感内容不会出现在页面中。</p>
            </div>
          </header>
          {logsQuery.isPending ? (
            <StatePanel description="正在读取本次运行的日志。" state="loading" title="正在读取日志" />
          ) : logsQuery.isError && logPages.length === 0 ? (
            <ReadError error={logsQuery.error} onRetry={() => void logsQuery.refetch()} />
          ) : (
            <>
              {logPages.some((page) => page.truncated) && (
                <TruncatedNotice>日志较长，当前仅显示一部分</TruncatedNotice>
              )}
              {logPages[0] && (
                <p className="u-small">
                  实际覆盖时间 · {logPages[0].coverage_start} → {logPages[0].coverage_end}
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
            <h2>系统指标</h2>
            <p>
              这里展示同一时间范围内的整体服务健康指标。为保护系统稳定性和用户信息，这些数据不会细分到某次运行或某位用户。
            </p>
          </div>
        </header>
        {descriptorsQuery.isPending ? (
          <StatePanel description="正在读取可用的系统指标。" state="loading" title="正在读取系统指标" />
        ) : descriptorsQuery.isError ? (
          <ReadError error={descriptorsQuery.error} onRetry={() => void descriptorsQuery.refetch()} />
        ) : descriptors.length === 0 ? (
          <StatePanel
            description="指标目录尚未就绪，页面不会猜测或拼接不存在的数据。"
            state="empty"
            title="暂时没有可查询的系统指标"
          />
        ) : selectedDescriptor ? (
          <>
            <div className="gf-observability__metric-controls">
              <label>
                <span>指标</span>
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
                <span>汇总间隔</span>
                <select
                  onChange={(event) => setResolutionSeconds(Number(event.target.value))}
                  value={resolutionSeconds}
                >
                  <option value={60}>1 分钟</option>
                  <option value={300}>5 分钟</option>
                  <option value={900}>15 分钟</option>
                </select>
              </label>
            </div>
            <div className="gf-observability__descriptor">
              <div>
                <span>指标名称</span>
                <code>{selectedDescriptor.metric_name}</code>
              </div>
              <div>
                <span>版本 / 类型 / 单位</span>
                <strong>
                  第 {selectedDescriptor.descriptor_version} 版 ·{" "}
                  {metricTypeLabel(selectedDescriptor.metric_type)} ·{" "}
                  {metricUnitLabel(selectedDescriptor.unit)}
                </strong>
              </div>
              <div>
                <span>分组维度</span>
                <code>{selectedDescriptor.label_keys.join(", ") || "无"}</code>
              </div>
              <TechnicalDetails
                items={[
                  {
                    label: "Descriptor digest",
                    value: selectedDescriptor.descriptor_digest,
                  },
                ]}
                summary="查看指标技术信息"
              />
            </div>
            {metricQuery.isPending ? (
              <StatePanel description="正在读取该指标的数据。" state="loading" title="正在读取指标" />
            ) : metricQuery.isError && metricPages.length === 0 ? (
              <ReadError error={metricQuery.error} onRetry={() => void metricQuery.refetch()} />
            ) : (
              <>
                {metricPages.some((page) => page.truncated) && (
                  <TruncatedNotice>指标数据较长，当前仅显示一部分</TruncatedNotice>
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
