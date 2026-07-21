import { describe, expect, it } from "vitest";

import type {
  MetricDescriptor,
  MetricPage,
  RunCostView,
  RunView,
  SpanPage,
  TraceSummary,
  TraceSummaryPage,
} from "./api";
import {
  descriptorRef,
  observationValue,
  requireRunCostOwner,
  requireRunOwner,
  requireRunTracePageOwner,
  requireSpanPageOwner,
  requireTraceOwner,
  requireExactMetricSeries,
  safeSpanInspector,
  traceSummaryTone,
  traceWaterfallSpans,
} from "./model";

const traceId = "1".repeat(32);

function spanPage(): SpanPage {
  return {
    items: [
      {
        redacted_attribute_keys: ["already_redacted"],
        redacted_event_fields: [],
        span: {
          attributes: {
            already_redacted: "server-hidden",
            apiKey: "private-api-key",
            attempt: 1,
            handlerConfig: "private handler config",
            nested: {
              payload: {
                prompt: "nested private prompt",
                rawResponse: "nested private response",
              },
              safe: [{ debug: "nested private debug", phase: "verify" }],
            },
            prompt: "private prompt",
            password: "private-password",
            raw_response: "private response",
          },
          duration_ns: 20_000_000,
          ended_at: "2026-07-20T01:00:00.030Z",
          error: null,
          events: [
            {
              attributes: { debug_payload: "private debug payload", phase: "checker" },
              name: "phase.finished",
              occurred_at: "2026-07-20T01:00:00.025Z",
            },
          ],
          links: [],
          name: "child",
          parent_span_id: "a".repeat(16),
          resource: { "service.name": "worker" },
          span_id: "b".repeat(16),
          span_schema_version: "span-data@1",
          started_at: "2026-07-20T01:00:00.010Z",
          status: "ok",
          trace_id: traceId,
        },
      },
      {
        redacted_attribute_keys: [],
        redacted_event_fields: [],
        span: {
          attributes: {},
          duration_ns: 40_000_000,
          ended_at: "2026-07-20T01:00:00.040Z",
          error: null,
          events: [],
          links: [],
          name: "root",
          parent_span_id: null,
          resource: { "service.name": "api" },
          span_id: "a".repeat(16),
          span_schema_version: "span-data@1",
          started_at: "2026-07-20T01:00:00.000Z",
          status: "unset",
          trace_id: traceId,
        },
      },
    ],
    next_cursor: null,
    page_schema_version: "span-page@1",
    trace_id: traceId,
    truncated: false,
  };
}

describe("Observability view model", () => {
  it("maps exact span timing to the shared waterfall without inventing terminal status", () => {
    expect(traceWaterfallSpans(spanPage().items)).toEqual([
      expect.objectContaining({ durationMs: 20, name: "child", startMs: 10, status: "ok" }),
      expect.objectContaining({ durationMs: 40, name: "root", startMs: 0, status: "running" }),
    ]);
  });

  it("never exposes prompt, raw response, debug payload, handler config, or server-redacted fields", () => {
    const inspector = safeSpanInspector(spanPage().items[0]);
    const serialized = JSON.stringify(inspector);

    expect(inspector.attributes).toEqual([
      ["attempt", 1],
      ["nested", { payload: {}, safe: [{ phase: "verify" }] }],
    ]);
    expect(inspector.resource).toEqual([["service.name", "worker"]]);
    expect(inspector.events[0]?.attributes).toEqual([["phase", "checker"]]);
    expect(inspector.redactedCount).toBeGreaterThanOrEqual(7);
    expect(serialized).not.toContain("private prompt");
    expect(serialized).not.toContain("private response");
    expect(serialized).not.toContain("private debug payload");
    expect(serialized).not.toContain("private handler config");
    expect(serialized).not.toContain("private-api-key");
    expect(serialized).not.toContain("private-password");
    expect(serialized).not.toContain("server-hidden");
    expect(serialized).not.toContain("nested private prompt");
    expect(serialized).not.toContain("nested private response");
    expect(serialized).not.toContain("nested private debug");
  });

  it("accepts metric series only under the selected exact descriptor ref and semantics", () => {
    const descriptor: MetricDescriptor = {
      descriptor_digest: "a".repeat(64),
      descriptor_schema_version: "metric-descriptor@1",
      descriptor_version: 1,
      histogram_bucket_bounds: [],
      label_keys: ["outcome"],
      metric_name: "gameforge.run.completed",
      metric_type: "counter",
      series_limit: 8,
      unit: "count",
      unit_schema_version: "metric-units@1",
    };
    const page = {
      coverage_end: "2026-07-20T02:00:00Z",
      coverage_start: "2026-07-20T01:00:00Z",
      effective_resolution_s: 60,
      next_cursor: null,
      page_schema_version: "metric-page@1",
      series: [
        {
          descriptor: descriptorRef(descriptor),
          labels: { outcome: "succeeded" },
          metric_name: descriptor.metric_name,
          metric_type: descriptor.metric_type,
          scalar_points: [{ ts_utc: "2026-07-20T01:01:00Z", value: 1 }],
          unit: descriptor.unit,
        },
      ],
      truncated: false,
    } satisfies MetricPage;

    expect(requireExactMetricSeries([descriptor], [descriptorRef(descriptor)], page)).toBe(page.series);
    expect(() =>
      requireExactMetricSeries([descriptor], [descriptorRef(descriptor)], {
        ...page,
        series: [{ ...page.series[0], unit: "token" }],
      }),
    ).toThrow("selected exact descriptor");
  });

  it("keeps unavailable distinct from a reported zero", () => {
    expect(observationValue("unavailable", 0)).toBe("unavailable");
    expect(observationValue("reported", 0)).toBe("0");
    expect(observationValue("reported", null)).toBe("unavailable");
  });

  it("keeps an unset Trace neutral instead of presenting it as successful", () => {
    expect(traceSummaryTone("error")).toBe("danger");
    expect(traceSummaryTone("ok")).toBe("ok");
    expect(traceSummaryTone("unset")).toBe("info");
  });

  it("rejects cross-owner Run, Trace, Span, and cost responses before composition", () => {
    const run = { run_id: "run:other" } as RunView;
    const trace = { trace_id: "2".repeat(32) } as TraceSummary;
    const tracePage = {
      items: [{ run_ids: ["run:other"] }],
    } as TraceSummaryPage;
    const foreignSpanPage = {
      ...spanPage(),
      trace_id: "2".repeat(32),
    };
    const cost = {
      budget_set: { run_id: "run:other" },
      run_id: "run:other",
    } as RunCostView;

    expect(() => requireRunOwner(run, "run:expected")).toThrow("selected Run");
    expect(() => requireTraceOwner(trace, traceId)).toThrow("selected Trace");
    expect(() => requireRunTracePageOwner(tracePage, "run:expected")).toThrow("selected Run");
    expect(() => requireSpanPageOwner(foreignSpanPage, traceId)).toThrow("selected Trace");
    expect(() => requireRunCostOwner(cost, "run:expected")).toThrow("selected Run");
  });
});
