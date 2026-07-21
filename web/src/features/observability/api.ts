import type { GameForgeOpenApiClient } from "../../api/client";
import { unwrapApiResponse } from "../../api/client";
import type { components } from "../../api/generated/openapi";
import { cursorQuery, requireExplicitCursorRestart } from "../../api/pagination";
import { gameForgeApi } from "../../api/runtime";

export type RunPage = components["schemas"]["OpaquePageV1_RunViewV1_"];
export type RunView = components["schemas"]["RunViewV1"];
export type TraceSummary = components["schemas"]["TraceSummaryV1"];
export type TraceSummaryPage = components["schemas"]["TraceSummaryPageV1"];
export type SpanPage = components["schemas"]["SpanPageV1"];
export type LogPage = components["schemas"]["LogPageV1"];
export type MetricDescriptor = components["schemas"]["MetricDescriptorV1"];
export type MetricDescriptorRef = components["schemas"]["MetricDescriptorRefV1"];
export type MetricDescriptorRegistry = components["schemas"]["MetricDescriptorRegistryV1"];
export type MetricPage = components["schemas"]["MetricPageV1"];
export type RunCostView = components["schemas"]["RunCostViewV2"];
type RunCostWireView = components["schemas"]["RunCostViewV1"] | components["schemas"]["RunCostViewV2"];

export interface TimeWindow {
  endUtc: string;
  startUtc: string;
}

export interface LogQuery extends TimeWindow {
  cursor: string | null;
  eventNames?: string[];
  levels?: components["schemas"]["LogRecordV1"]["level"][];
  limit?: number;
  producerRunId?: string;
  runId?: string;
  services?: string[];
  spanId?: string;
  traceId?: string;
}

export interface MetricQuery extends TimeWindow {
  cursor: string | null;
  descriptorRefs: readonly MetricDescriptorRef[];
  maxPoints: number;
  resolutionSeconds: number;
  seriesLimit: number;
}

export interface ObservabilityApi {
  getMetricDescriptors(): Promise<MetricDescriptorRegistry>;
  getRun(runId: string): Promise<RunView>;
  getRunCost(runId: string, cursor: string | null): Promise<RunCostView>;
  getTrace(traceId: string): Promise<TraceSummary>;
  listRuns(cursor: string | null): Promise<RunPage>;
  listRunTraces(runId: string, cursor: string | null): Promise<TraceSummaryPage>;
  listTraceSpans(traceId: string, cursor: string | null): Promise<SpanPage>;
  queryLogs(query: LogQuery): Promise<LogPage>;
  queryMetrics(query: MetricQuery): Promise<MetricPage>;
}

async function readCursorPage<T>(cursor: string | null, read: () => Promise<T>): Promise<T> {
  try {
    return await read();
  } catch (error) {
    throw requireExplicitCursorRestart(error, cursor);
  }
}

export function createObservabilityApi(
  client: GameForgeOpenApiClient = gameForgeApi.client,
): ObservabilityApi {
  return {
    async getMetricDescriptors() {
      return unwrapApiResponse<MetricDescriptorRegistry>(await client.GET("/api/v1/metrics/descriptors"));
    },

    async getRun(runId) {
      return unwrapApiResponse<RunView>(
        await client.GET("/api/v1/runs/{run_id}", {
          params: { path: { run_id: runId } },
        }),
      );
    },

    getRunCost(runId, cursor) {
      return readCursorPage(cursor, async () =>
        requireRunCostV2(
          await unwrapApiResponse<RunCostWireView>(
            await client.GET("/api/v1/cost/{run_id}", {
              params: {
                path: { run_id: runId },
                query: {
                  ...cursorQuery(cursor),
                  limit: 100,
                  view_schema_version: "run-cost-view@2",
                },
              },
            }),
          ),
        ),
      );
    },

    async getTrace(traceId) {
      return unwrapApiResponse<TraceSummary>(
        await client.GET("/api/v1/traces/{trace_id}", {
          params: { path: { trace_id: traceId } },
        }),
      );
    },

    listRuns(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<RunPage>(
          await client.GET("/api/v1/runs", {
            params: { query: { ...cursorQuery(cursor), limit: 50 } },
          }),
        ),
      );
    },

    listRunTraces(runId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<TraceSummaryPage>(
          await client.GET("/api/v1/runs/{run_id}/traces", {
            params: {
              path: { run_id: runId },
              query: { ...cursorQuery(cursor), limit: 100 },
            },
          }),
        ),
      );
    },

    listTraceSpans(traceId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<SpanPage>(
          await client.GET("/api/v1/traces/{trace_id}/spans", {
            params: {
              path: { trace_id: traceId },
              query: { ...cursorQuery(cursor), limit: 100 },
            },
          }),
        ),
      );
    },

    queryLogs(query) {
      return readCursorPage(query.cursor, async () =>
        unwrapApiResponse<LogPage>(
          await client.GET("/api/v1/logs/query", {
            params: {
              query: {
                ...cursorQuery(query.cursor),
                end_utc: query.endUtc,
                event_names: query.eventNames,
                levels: query.levels,
                limit: query.limit ?? 100,
                producer_run_id: query.producerRunId,
                run_id: query.runId,
                services: query.services,
                span_id: query.spanId,
                start_utc: query.startUtc,
                trace_id: query.traceId,
              },
            },
          }),
        ),
      );
    },

    queryMetrics(query) {
      return readCursorPage(query.cursor, async () =>
        unwrapApiResponse<MetricPage>(
          await client.GET("/api/v1/metrics/query", {
            params: {
              query: {
                ...cursorQuery(query.cursor),
                descriptor_refs: JSON.stringify(query.descriptorRefs),
                end_utc: query.endUtc,
                label_matchers: "[]",
                max_points: query.maxPoints,
                resolution_s: query.resolutionSeconds,
                series_limit: query.seriesLimit,
                start_utc: query.startUtc,
              },
            },
          }),
        ),
      );
    },
  };
}

function requireRunCostV2(view: RunCostWireView): RunCostView {
  if (view.view_schema_version !== "run-cost-view@2") {
    throw new Error("Run cost response did not honor the requested run-cost-view@2 projection.");
  }
  return view;
}

export const observabilityApi = createObservabilityApi();
