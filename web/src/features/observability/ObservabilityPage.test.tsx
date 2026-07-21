import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { CursorExpiredError } from "../../api/pagination";
import { createQueryClient } from "../../api/query-client";
import type {
  LogPage,
  MetricDescriptorRegistry,
  MetricPage,
  ObservabilityApi,
  RunCostView,
  RunPage,
  RunView,
  TraceSummaryPage,
} from "./api";
import { ObservabilityPage } from "./ObservabilityPage";

const traceId = "1".repeat(32);
const run: RunView = {
  attempt_no: 2,
  events_url: "/api/v1/runs/run:7/events",
  failure_artifact_id: null,
  result_artifact_id: "artifact:result:7",
  revision: 5,
  run_id: "run:7",
  status: "succeeded",
  status_url: "/api/v1/runs/run:7",
  terminal_cassette_artifact_id: null,
  view_schema_version: "run-view@1",
};

const runPage: RunPage = {
  expires_at: "2026-07-20T03:00:00Z",
  items: [run],
  next_cursor: null,
  page_schema_version: "page@1",
  read_snapshot_id: "read:runs:1",
};

const tracePage: TraceSummaryPage = {
  coverage_end: "2026-07-20T02:00:00Z",
  coverage_start: "2026-07-20T01:00:00Z",
  items: [
    {
      duration_ns: 1_250_000_000,
      ended_at: "2026-07-20T01:10:01.250Z",
      root_span_id: "2".repeat(16),
      run_ids: [run.run_id],
      service_names: ["api", "worker"],
      span_count: 5,
      started_at: "2026-07-20T01:10:00Z",
      status: "ok",
      trace_id: traceId,
      trace_schema_version: "trace-summary@1",
      truncated: true,
    },
  ],
  next_cursor: null,
  page_schema_version: "trace-summary-page@1",
  truncated: true,
};

const logPage: LogPage = {
  coverage_end: "2026-07-20T02:00:00Z",
  coverage_start: "2026-07-20T01:00:00Z",
  items: [
    {
      record: {
        event_name: "run.completed",
        fields: { outcome: "succeeded", rawResponse: "must never render" },
        level: "info",
        log_id: "log:7",
        log_schema_version: "log-record@1",
        message: "Run completed",
        run_id: run.run_id,
        service: "worker",
        trace_id: traceId,
        ts_utc: "2026-07-20T01:10:01Z",
      },
      redacted_fields: ["rawResponse"],
    },
  ],
  next_cursor: null,
  page_schema_version: "log-page@1",
  truncated: true,
};

const descriptorRegistry: MetricDescriptorRegistry = {
  descriptors: [
    {
      descriptor_digest: "a".repeat(64),
      descriptor_schema_version: "metric-descriptor@1",
      descriptor_version: 1,
      histogram_bucket_bounds: [],
      label_keys: ["method", "status_class"],
      metric_name: "gameforge.api.request.count",
      metric_type: "counter",
      series_limit: 16,
      unit: "request",
      unit_schema_version: "metric-units@1",
    },
  ],
  global_series_limit: 64,
  registry_digest: "b".repeat(64),
  registry_schema_version: "metric-descriptor-registry@1",
  registry_version: 1,
};

const metricPage: MetricPage = {
  coverage_end: "2026-07-20T02:00:00Z",
  coverage_start: "2026-07-20T01:00:00Z",
  effective_resolution_s: 60,
  next_cursor: null,
  page_schema_version: "metric-page@1",
  series: [
    {
      descriptor: {
        descriptor_digest: "a".repeat(64),
        descriptor_version: 1,
        metric_name: "gameforge.api.request.count",
      },
      labels: { method: "GET", status_class: "2xx" },
      metric_name: "gameforge.api.request.count",
      metric_type: "counter",
      scalar_points: [{ ts_utc: "2026-07-20T01:10:00Z", value: 3 }],
      unit: "request",
    },
  ],
  truncated: true,
};

const cost: RunCostView = {
  budget_set: {
    budget_set_snapshot_id: "budget-set:run:7",
    captured_at: "2026-07-20T01:00:00Z",
    run_id: run.run_id,
    selection_policy_version: "budget-selection@1",
    set_schema_version: "budget-set-snapshot@1",
    snapshots: [
      {
        budget_id: "budget:run:7",
        budget_revision_at_freeze: 4,
        captured_at: "2026-07-20T01:00:00Z",
        consumed: [
          {
            amount_schema_version: "cost-amount@1",
            dimension: "request",
            unit: "request",
            value: "0",
          },
        ],
        limits: [
          {
            amount_schema_version: "cost-amount@1",
            dimension: "request",
            unit: "request",
            value: "10",
          },
        ],
        policy_version: "budget-policy@1",
        reserved: [],
        scope_id: run.run_id,
        scope_kind: "run",
        snapshot_id: "budget-snapshot:run:7",
        snapshot_schema_version: "budget-snapshot@1",
      },
    ],
  },
  next_cursor: null,
  run_id: run.run_id,
  settlement_summary: {
    group_counts: [
      {
        count_schema_version: "cost-settlement-group-count@1",
        group_count: 1,
        scope: "attempt_call",
        status: "held_unknown",
      },
      {
        count_schema_version: "cost-settlement-group-count@1",
        group_count: 1,
        scope: "attempt_call",
        status: "late_reconciled",
      },
    ],
    held_unknown_group_count: 1,
    late_adjustment_usage_count: 1,
    summary_schema_version: "cost-settlement-summary@1",
    total_group_count: 2,
    usage_entry_count: 2,
    usage_evidence_status: "recorded",
  },
  usage: [
    {
      adjustment_of_usage_id: null,
      attempt_no: 2,
      execution_source: "cassette_replay",
      latency: {
        observation_schema_version: "latency-observation@1",
        provider_latency_ms: null,
        status: "unavailable",
      },
      monetary: {
        amount: null,
        currency: null,
        observation_schema_version: "monetary-observation@1",
        price_book_version: null,
        quote_effective_at: null,
        status: "unavailable",
      },
      provider_prefix_cache: {
        hit: false,
        observation_schema_version: "cache-hit-observation@1",
        status: "reported",
      },
      recorded_at: "2026-07-20T01:10:01Z",
      retry_index: 0,
      scope: "attempt_call",
      token_usage: {
        cache_read_tokens: null,
        cache_write_tokens: null,
        input_tokens: null,
        observation_schema_version: "token-usage-observation@1",
        output_tokens: null,
        status: "unavailable",
        total_tokens: null,
      },
      transport_attempt: 1,
      usage_schema_version: "cost-usage-view@1",
      usage_id: "usage:unknown",
      wall_time_ns: 8_000_000,
    },
    {
      adjustment_of_usage_id: null,
      attempt_no: 2,
      execution_source: "full_response_cache",
      latency: {
        observation_schema_version: "latency-observation@1",
        provider_latency_ms: 0,
        status: "reported",
      },
      monetary: {
        amount: "0",
        currency: "USD",
        observation_schema_version: "monetary-observation@1",
        price_book_version: "prices@1",
        quote_effective_at: "2026-07-20T00:00:00Z",
        status: "reported",
      },
      provider_prefix_cache: {
        hit: false,
        observation_schema_version: "cache-hit-observation@1",
        status: "reported",
      },
      recorded_at: "2026-07-20T01:10:02Z",
      retry_index: 0,
      scope: "attempt_call",
      token_usage: {
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        input_tokens: 0,
        observation_schema_version: "token-usage-observation@1",
        output_tokens: 0,
        status: "reported",
        total_tokens: 0,
      },
      transport_attempt: 1,
      usage_schema_version: "cost-usage-view@1",
      usage_id: "usage:zero",
      wall_time_ns: 0,
    },
  ],
  view_schema_version: "run-cost-view@2",
};

function api(overrides: Partial<ObservabilityApi> = {}): ObservabilityApi {
  return {
    getMetricDescriptors: vi.fn().mockResolvedValue(descriptorRegistry),
    getRun: vi.fn().mockResolvedValue(run),
    getRunCost: vi.fn().mockResolvedValue(cost),
    getTrace: vi.fn(),
    listRuns: vi.fn().mockResolvedValue(runPage),
    listRunTraces: vi.fn().mockResolvedValue(tracePage),
    listTraceSpans: vi.fn(),
    queryLogs: vi.fn().mockResolvedValue(logPage),
    queryMetrics: vi.fn().mockResolvedValue(metricPage),
    ...overrides,
  };
}

function renderPage(testApi = api()) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={["/observability?run=run%3A7"]}>
        <ObservabilityPage api={testApi} now={() => new Date("2026-07-20T02:00:00Z")} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ObservabilityPage", () => {
  it("correlates an authorized Run with traces, redacted logs, exact system metrics, and honest cost", async () => {
    const testApi = api();
    renderPage(testApi);

    expect(await screen.findByRole("heading", { level: 1, name: "可观测性" })).toBeVisible();
    expect((await screen.findAllByText("run:7"))[0]).toBeVisible();
    expect(screen.getAllByText("succeeded")[0]).toBeVisible();
    expect((await screen.findAllByRole("link", { name: new RegExp(traceId) }))[0]).toHaveAttribute(
      "href",
      `/observability/traces/${traceId}`,
    );
    const traceTruncation = await screen.findByText("Trace page 已截断");
    const logTruncation = screen.getByText("Log page 已截断");
    const metricTruncation = screen.getByText("Metric page 已截断");
    expect(traceTruncation).toBeVisible();
    expect(logTruncation).toBeVisible();
    expect(metricTruncation).toBeVisible();
    expect(traceTruncation).not.toHaveAttribute("role");
    expect(logTruncation).not.toHaveAttribute("role");
    expect(metricTruncation).not.toHaveAttribute("role");

    expect(screen.getByRole("heading", { name: "Run 日志" })).toBeVisible();
    expect(screen.getByText("Run completed")).toBeVisible();
    expect(screen.queryByText("must never render")).not.toBeInTheDocument();

    expect(screen.getByText(/同一时间窗的系统运营指标/)).toBeVisible();
    expect(screen.getByText(/不能归因于当前 Run/)).toBeVisible();
    expect(screen.getByText(/descriptor v1 · counter · request/)).toBeVisible();
    expect(screen.getByText("a".repeat(64))).toBeVisible();

    const unknownUsage = screen.getByTestId("cost-usage-usage:unknown");
    const zeroUsage = screen.getByTestId("cost-usage-usage:zero");
    expect(unknownUsage).toHaveTextContent("Token usage unavailable");
    expect(unknownUsage).toHaveTextContent("Monetary unavailable");
    expect(zeroUsage).toHaveTextContent("Total token 0");
    expect(zeroUsage).toHaveTextContent("Monetary 0 USD");
    expect(screen.getByText("budget-set:run:7")).toBeVisible();
    const budgetTable = screen.getByRole("table", { name: "预算 · budget:run:7数据表" });
    expect(within(budgetTable).getAllByText("0 request")).toHaveLength(2);

    expect(testApi.queryLogs).toHaveBeenCalledWith(
      expect.objectContaining({
        endUtc: "2026-07-20T02:00:00.000Z",
        runId: "run:7",
        startUtc: "2026-07-20T01:00:00.000Z",
      }),
    );
    expect(testApi.queryMetrics).toHaveBeenCalledWith(
      expect.objectContaining({
        descriptorRefs: [
          {
            descriptor_digest: "a".repeat(64),
            descriptor_version: 1,
            metric_name: "gameforge.api.request.count",
          },
        ],
      }),
    );
  });

  it("keeps loaded Trace rows on 410 and restarts only after an explicit choice", async () => {
    const user = userEvent.setup();
    const firstPage = { ...tracePage, next_cursor: "trace-page:2", truncated: false };
    const restartedPage = {
      ...tracePage,
      items: [{ ...tracePage.items[0], trace_id: "3".repeat(32), truncated: false }],
      next_cursor: null,
      truncated: false,
    };
    const listRunTraces = vi
      .fn()
      .mockResolvedValueOnce(firstPage)
      .mockRejectedValueOnce(
        new CursorExpiredError(
          {
            code: "cursor_expired",
            conflict_set_id: null,
            detail: "retention expired",
            earliest_cursor: null,
            instance: "/api/v1/runs/run:7/traces",
            request_id: "request:trace-cursor",
            retry_after_s: null,
            run_id: run.run_id,
            status: 410,
            title: "Cursor expired",
            trace_id: null,
            type: "about:blank",
          },
          "trace-page:2",
        ),
      )
      .mockResolvedValueOnce(restartedPage);
    renderPage(api({ listRunTraces }));

    const traceTable = await screen.findByRole("region", { name: "该 Run 的 Trace" });
    await user.click(within(traceTable).getByRole("button", { name: "加载下一页" }));
    expect(await within(traceTable).findByText(/分页游标已过期/)).toBeVisible();
    expect(within(traceTable).getByText(traceId)).toBeVisible();

    await user.click(within(traceTable).getByRole("button", { name: "重新开始查询" }));
    await waitFor(() => expect(listRunTraces).toHaveBeenLastCalledWith(run.run_id, null));
    const restartedTable = await screen.findByRole("region", { name: "该 Run 的 Trace" });
    expect(within(restartedTable).getByText("3".repeat(32))).toBeVisible();
    expect(within(restartedTable).queryByText(traceId)).not.toBeInTheDocument();
  });

  it("does not compose dependent trace, log, or cost reads before the exact Run owner succeeds", async () => {
    const testApi = api({
      getRun: vi.fn().mockRejectedValue(new Error("Run authority unavailable")),
    });
    renderPage(testApi);

    expect(await screen.findByText("观测数据读取失败")).toBeVisible();
    expect(testApi.listRunTraces).not.toHaveBeenCalled();
    expect(testApi.queryLogs).not.toHaveBeenCalled();
    expect(testApi.getRunCost).not.toHaveBeenCalled();
  });

  it("bounds 512-character Run and terminal identifiers inside the scrollable table", async () => {
    const longRunId = `run:${"r".repeat(508)}`;
    const longArtifactId = `artifact:${"a".repeat(503)}`;
    const longRun = { ...run, result_artifact_id: longArtifactId, run_id: longRunId };
    renderPage(
      api({
        listRuns: vi.fn().mockResolvedValue({ ...runPage, items: [longRun] }),
      }),
    );

    const table = await screen.findByRole("region", { name: "已授权 Run" });
    expect(within(table).getByText(longRunId)).toHaveAttribute("tabindex", "0");
    expect(within(table).getByText(longArtifactId)).toHaveAttribute("tabindex", "0");
    expect(within(table).getByText(/attempt 2 · revision 5/)).toHaveClass("gf-observability__table-nowrap");
  });

  it("renders exact histogram bounds, cumulative bucket counts, and unavailable sum", async () => {
    const user = userEvent.setup();
    const histogramDescriptor = {
      descriptor_digest: "c".repeat(64),
      descriptor_schema_version: "metric-descriptor@1",
      descriptor_version: 4,
      histogram_bucket_bounds: [10, 100],
      label_keys: ["operation"],
      metric_name: "gameforge.worker.duration",
      metric_type: "histogram",
      series_limit: 4,
      unit: "ms",
      unit_schema_version: "metric-units@1",
    } satisfies MetricDescriptorRegistry["descriptors"][number];
    const histogramPage = {
      ...metricPage,
      series: [
        {
          bucket_bounds: [10, 100],
          descriptor: {
            descriptor_digest: histogramDescriptor.descriptor_digest,
            descriptor_version: histogramDescriptor.descriptor_version,
            metric_name: histogramDescriptor.metric_name,
          },
          histogram_points: [
            {
              count: 3,
              cumulative_bucket_counts: [1, 2, 3],
              sum: null,
              ts_utc: "2026-07-20T01:10:00Z",
            },
          ],
          labels: { operation: "checker" },
          metric_name: histogramDescriptor.metric_name,
          metric_type: "histogram",
          unit: "ms",
        },
      ],
    } satisfies MetricPage;
    renderPage(
      api({
        getMetricDescriptors: vi.fn().mockResolvedValue({
          ...descriptorRegistry,
          descriptors: [histogramDescriptor],
        }),
        queryMetrics: vi.fn().mockResolvedValue(histogramPage),
      }),
    );

    await user.click(await screen.findByText(/查看 exact histogram buckets/));
    const histogramTable = await screen.findByRole("table", {
      name: "gameforge.worker.duration histogram buckets",
    });
    expect(within(histogramTable).getByRole("columnheader", { name: "≤ 10 ms" })).toBeVisible();
    expect(within(histogramTable).getByRole("columnheader", { name: "≤ 100 ms" })).toBeVisible();
    expect(within(histogramTable).getByRole("columnheader", { name: "+Inf" })).toBeVisible();
    expect(within(histogramTable).getByRole("cell", { name: "unavailable" })).toBeVisible();
    expect(within(histogramTable).getAllByRole("cell", { name: "3" })).toHaveLength(2);
  });
});
