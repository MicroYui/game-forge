import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/query-client";
import type { LogPage, ObservabilityApi, SpanPage, TraceSummary } from "./api";
import { TraceDetailPage } from "./TraceDetailPage";

const traceId = "1".repeat(32);
const rootSpanId = "2".repeat(16);

const summary: TraceSummary = {
  duration_ns: 50_000_000,
  ended_at: "2026-07-20T01:00:00.050Z",
  root_span_id: rootSpanId,
  run_ids: ["run:7"],
  service_names: ["api", "worker"],
  span_count: 2,
  started_at: "2026-07-20T01:00:00Z",
  status: "error",
  trace_id: traceId,
  trace_schema_version: "trace-summary@1",
  truncated: true,
};

const spans: SpanPage = {
  items: [
    {
      redacted_attribute_keys: [],
      redacted_event_fields: [],
      span: {
        attributes: { attempt: 2, prompt: "must never render", raw_payload: "private payload" },
        duration_ns: 50_000_000,
        ended_at: "2026-07-20T01:00:00.050Z",
        error: { error_type: "ExecutionError", message: "sanitized failure" },
        events: [
          {
            attributes: { phase: "publish", debug: "private debug" },
            name: "attempt.failed",
            occurred_at: "2026-07-20T01:00:00.045Z",
          },
        ],
        links: [],
        name: "worker.attempt",
        parent_span_id: null,
        resource: { "service.name": "worker", handler_config: "private config" },
        span_id: rootSpanId,
        span_schema_version: "span-data@1",
        started_at: "2026-07-20T01:00:00Z",
        status: "error",
        trace_id: traceId,
      },
    },
  ],
  next_cursor: null,
  page_schema_version: "span-page@1",
  trace_id: traceId,
  truncated: true,
};

const logs: LogPage = {
  coverage_end: "2026-07-20T01:00:00.050Z",
  coverage_start: "2026-07-20T01:00:00Z",
  items: [],
  next_cursor: null,
  page_schema_version: "log-page@1",
  truncated: false,
};

function api(): ObservabilityApi {
  return {
    getMetricDescriptors: vi.fn(),
    getRun: vi.fn(),
    getRunCost: vi.fn(),
    getTrace: vi.fn().mockResolvedValue(summary),
    listRuns: vi.fn(),
    listRunTraces: vi.fn(),
    listTraceSpans: vi.fn().mockResolvedValue(spans),
    queryLogs: vi.fn().mockResolvedValue(logs),
    queryMetrics: vi.fn(),
  };
}

describe("TraceDetailPage", () => {
  it("renders exact summary, bounded spans, safe inspector, waterfall, and Trace-correlated logs", async () => {
    const testApi = api();
    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <TraceDetailPage api={testApi} traceId={traceId} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { level: 1, name: "Trace 详情" })).toBeVisible();
    expect(screen.getByText(traceId)).toBeVisible();
    expect(screen.getByRole("link", { name: /run:7/ })).toHaveAttribute("href", "/runs/run%3A7");
    expect(screen.getByRole("heading", { name: "Trace waterfall" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Span inspector" })).toBeVisible();
    expect(screen.getAllByText("worker.attempt")[0]).toBeVisible();
    const inspector = screen.getByRole("region", { name: "Span inspector" });
    expect(within(inspector).getByText("attempt")).toBeVisible();
    expect(within(inspector).getByText("2")).toBeVisible();
    expect(within(inspector).getByText("phase")).toBeVisible();
    expect(within(inspector).getByText("publish")).toBeVisible();
    const redactionNotice = within(inspector).getByText(/4 个字段已脱敏/);
    expect(redactionNotice).toBeVisible();
    expect(redactionNotice).not.toHaveAttribute("role");
    expect(screen.queryByText("must never render")).not.toBeInTheDocument();
    expect(screen.queryByText("private payload")).not.toBeInTheDocument();
    expect(screen.queryByText("private debug")).not.toBeInTheDocument();
    expect(screen.queryByText("private config")).not.toBeInTheDocument();
    const summaryTruncation = screen.getByText("Trace summary 已截断");
    const spanTruncation = screen.getByText("Span page 已截断");
    expect(summaryTruncation).toBeVisible();
    expect(spanTruncation).toBeVisible();
    expect(summaryTruncation).not.toHaveAttribute("role");
    expect(spanTruncation).not.toHaveAttribute("role");

    const spanTable = screen.getByRole("region", { name: "Trace spans" });
    expect(within(spanTable).getByText(rootSpanId)).toBeVisible();
    expect(screen.getByRole("heading", { name: "Trace 日志" })).toBeVisible();
    expect(testApi.queryLogs).toHaveBeenCalledWith({
      cursor: null,
      endUtc: summary.ended_at,
      startUtc: summary.started_at,
      traceId,
    });
  });

  it("keeps loaded Trace logs and retries a non-expired next-page cursor", async () => {
    const user = userEvent.setup();
    const firstPage: LogPage = { ...logs, next_cursor: "log-next" };
    const nextPageFailure = new Error("temporary read failure");
    const testApi = api();
    vi.mocked(testApi.queryLogs).mockImplementation(({ cursor }) =>
      cursor === null ? Promise.resolve(firstPage) : Promise.reject(nextPageFailure),
    );

    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <TraceDetailPage api={testApi} traceId={traceId} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const logsHeading = await screen.findByRole("heading", { name: "Trace 日志" });
    const logsSection = logsHeading.closest("section");
    expect(logsSection).not.toBeNull();
    await user.click(await within(logsSection!).findByRole("button", { name: "加载下一页" }));

    const retry = await within(logsSection!).findByRole("button", { name: "重试下一页" });
    expect(within(logsSection!).getByText("下一页读取失败；现有 Trace 日志保留。")).toBeVisible();
    await user.click(retry);

    expect(testApi.queryLogs).toHaveBeenLastCalledWith({
      cursor: "log-next",
      endUtc: summary.ended_at,
      startUtc: summary.started_at,
      traceId,
    });
    expect(testApi.queryLogs).toHaveBeenCalledTimes(3);
  });

  it("freezes one bounded log end for an open Trace across local rerenders", async () => {
    const user = userEvent.setup();
    const openSummary = { ...summary, ended_at: null };
    const testApi = api();
    vi.mocked(testApi.getTrace).mockResolvedValue(openSummary);
    const now = vi
      .fn<() => Date>()
      .mockReturnValueOnce(new Date("2026-07-20T02:00:00Z"))
      .mockReturnValue(new Date("2026-07-20T02:01:00Z"));

    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <TraceDetailPage api={testApi} now={now} traceId={traceId} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(testApi.queryLogs).toHaveBeenCalledTimes(1));
    await user.click(await screen.findByRole("button", { name: "检查 worker.attempt" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Span inspector" })).toBeVisible());
    expect(testApi.queryLogs).toHaveBeenCalledTimes(1);
    expect(testApi.queryLogs).toHaveBeenCalledWith({
      cursor: null,
      endUtc: "2026-07-20T02:00:00.000Z",
      startUtc: summary.started_at,
      traceId,
    });
  });
});
