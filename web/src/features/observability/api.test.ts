import { describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import { createObservabilityApi, type MetricDescriptorRef } from "./api";

function response<T>(data: T) {
  return { data, response: Response.json(data) };
}

describe("Observability API", () => {
  it("uses only the bounded typed Run, trace, span, log, metric, and cost reads", async () => {
    const cursor = "opaque.observability+/=%2Ftail";
    const descriptor: MetricDescriptorRef = {
      descriptor_digest: "a".repeat(64),
      descriptor_version: 3,
      metric_name: "gameforge.run.completed",
    };
    const get = vi.fn(async (path: string) => {
      if (path === "/api/v1/metrics/descriptors") return response({ descriptors: [] });
      if (path === "/api/v1/runs/{run_id}") return response({ run_id: "run:7" });
      if (path === "/api/v1/traces/{trace_id}") return response({ trace_id: "1".repeat(32) });
      if (path === "/api/v1/cost/{run_id}") {
        return response({
          budget_set: {},
          run_id: "run:7",
          settlement_summary: {},
          usage: [],
          view_schema_version: "run-cost-view@2",
        });
      }
      return response({ items: [], series: [], usage: [] });
    });
    const api = createObservabilityApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await api.listRuns(cursor);
    await api.getRun("run:7");
    await api.listRunTraces("run:7", cursor);
    await api.getTrace("1".repeat(32));
    await api.listTraceSpans("1".repeat(32), cursor);
    await api.queryLogs({
      cursor,
      endUtc: "2026-07-20T02:00:00Z",
      runId: "run:7",
      startUtc: "2026-07-20T01:00:00Z",
    });
    await api.getMetricDescriptors();
    await api.queryMetrics({
      cursor,
      descriptorRefs: [descriptor],
      endUtc: "2026-07-20T02:00:00Z",
      maxPoints: 240,
      resolutionSeconds: 60,
      seriesLimit: 8,
      startUtc: "2026-07-20T01:00:00Z",
    });
    await api.getRunCost("run:7", cursor);

    expect(get).toHaveBeenCalledWith("/api/v1/runs", {
      params: { query: { cursor, limit: 50 } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/runs/{run_id}", {
      params: { path: { run_id: "run:7" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/runs/{run_id}/traces", {
      params: { path: { run_id: "run:7" }, query: { cursor, limit: 100 } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/traces/{trace_id}", {
      params: { path: { trace_id: "1".repeat(32) } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/traces/{trace_id}/spans", {
      params: { path: { trace_id: "1".repeat(32) }, query: { cursor, limit: 100 } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/logs/query", {
      params: {
        query: {
          cursor,
          end_utc: "2026-07-20T02:00:00Z",
          event_names: undefined,
          levels: undefined,
          limit: 100,
          producer_run_id: undefined,
          run_id: "run:7",
          services: undefined,
          span_id: undefined,
          start_utc: "2026-07-20T01:00:00Z",
          trace_id: undefined,
        },
      },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/metrics/descriptors");
    expect(get).toHaveBeenCalledWith("/api/v1/metrics/query", {
      params: {
        query: {
          cursor,
          descriptor_refs: JSON.stringify([descriptor]),
          end_utc: "2026-07-20T02:00:00Z",
          label_matchers: "[]",
          max_points: 240,
          resolution_s: 60,
          series_limit: 8,
          start_utc: "2026-07-20T01:00:00Z",
        },
      },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/cost/{run_id}", {
      params: {
        path: { run_id: "run:7" },
        query: { cursor, limit: 100, view_schema_version: "run-cost-view@2" },
      },
    });
  });

  it("fails closed if the negotiated cost read returns the legacy projection", async () => {
    const get = vi.fn(async () =>
      response({
        budget_set: {},
        next_cursor: null,
        run_id: "run:7",
        usage: [],
        view_schema_version: "run-cost-view@1",
      }),
    );
    const api = createObservabilityApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.getRunCost("run:7", null)).rejects.toThrow("run-cost-view@2");
  });

  it("preserves every paged 410 as an explicit restart boundary", async () => {
    const staleCursor = "stale.observability+/=";
    const problem = {
      code: "cursor_expired",
      detail: "retention snapshot expired",
      instance: "/api/v1/traces",
      request_id: "request:observability-cursor",
      status: 410,
      title: "Cursor expired",
      type: "about:blank",
    };
    const get = vi.fn(async () => ({
      error: problem,
      response: Response.json(problem, { status: 410 }),
    }));
    const api = createObservabilityApi({ GET: get } as unknown as GameForgeOpenApiClient);

    const reads = [
      () => api.listRuns(staleCursor),
      () => api.listRunTraces("run:7", staleCursor),
      () => api.listTraceSpans("1".repeat(32), staleCursor),
      () =>
        api.queryLogs({
          cursor: staleCursor,
          endUtc: "2026-07-20T02:00:00Z",
          runId: "run:7",
          startUtc: "2026-07-20T01:00:00Z",
        }),
      () =>
        api.queryMetrics({
          cursor: staleCursor,
          descriptorRefs: [
            {
              descriptor_digest: "a".repeat(64),
              descriptor_version: 1,
              metric_name: "gameforge.run.completed",
            },
          ],
          endUtc: "2026-07-20T02:00:00Z",
          maxPoints: 20,
          resolutionSeconds: 60,
          seriesLimit: 4,
          startUtc: "2026-07-20T01:00:00Z",
        }),
      () => api.getRunCost("run:7", staleCursor),
    ];

    for (const read of reads) {
      await expect(read()).rejects.toMatchObject({
        name: "CursorExpiredError",
        staleCursor,
      });
    }
  });

  it.each([
    [404, "not_found"],
    [422, "query_too_broad"],
    [503, "dependency_unavailable"],
  ])("preserves %i/%s as a safe API problem", async (status, code) => {
    const problem = {
      code,
      conflict_set_id: null,
      detail: `${code} detail`,
      earliest_cursor: null,
      instance: "/api/v1/metrics/query",
      request_id: `request:${status}`,
      retry_after_s: null,
      run_id: "run:7",
      status,
      title: code,
      trace_id: null,
      type: "about:blank",
    };
    const get = vi.fn(async () => ({
      error: problem,
      response: Response.json(problem, { status }),
    }));
    const api = createObservabilityApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.getRun("run:7")).rejects.toMatchObject({ name: "ApiProblemError", problem });
  });
});
