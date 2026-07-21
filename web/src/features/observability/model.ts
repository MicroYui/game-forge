import type { TraceWaterfallSpan } from "../../components/charts";
import type {
  MetricDescriptor,
  MetricDescriptorRef,
  MetricPage,
  RunCostView,
  RunView,
  SpanPage,
  TraceSummary,
  TraceSummaryPage,
} from "./api";

type SpanView = SpanPage["items"][number];
type SpanEvent = SpanView["span"]["events"][number];

const SENSITIVE_FIELD_MARKERS = [
  "accesstoken",
  "apikey",
  "authorization",
  "clientsecret",
  "credential",
  "debug",
  "handlerconfig",
  "idtoken",
  "password",
  "prompt",
  "rawpayload",
  "rawresponse",
  "refreshtoken",
  "responsebody",
  "secret",
  "sessiontoken",
] as const;
const MAX_SAFE_VALUE_DEPTH = 8;
const MAX_SAFE_COLLECTION_ITEMS = 128;

export interface SafeSpanEvent {
  attributes: readonly [string, unknown][];
  name: string;
  occurredAt: string;
}

export interface SafeSpanInspector {
  attributes: readonly [string, unknown][];
  durationNs: number;
  endedAt: string;
  error: SpanView["span"]["error"];
  events: readonly SafeSpanEvent[];
  name: string;
  parentSpanId: string | null;
  redactedCount: number;
  resource: readonly [string, unknown][];
  spanId: string;
  startedAt: string;
  status: SpanView["span"]["status"];
  traceId: string;
}

function compactFieldKey(value: string): string {
  return value.toLocaleLowerCase("en-US").replace(/[-._]/g, "");
}

function isSensitiveField(key: string): boolean {
  const compact = compactFieldKey(key);
  return SENSITIVE_FIELD_MARKERS.some((marker) => compact.includes(marker));
}

function safeEntries(fields: Record<string, unknown>, redacted: Set<string>): [string, unknown][] {
  const entries: [string, unknown][] = [];
  for (const [key, value] of Object.entries(fields).sort(([left], [right]) =>
    left < right ? -1 : left > right ? 1 : 0,
  )) {
    if (redacted.has(key) || isSensitiveField(key)) {
      redacted.add(key);
      continue;
    }
    entries.push([key, safeValue(value, redacted, 0)]);
  }
  return entries;
}

function safeValue(value: unknown, redacted: Set<string>, depth: number): unknown {
  if (value === null || typeof value !== "object") return value;
  if (depth >= MAX_SAFE_VALUE_DEPTH) return "[bounded]";
  if (Array.isArray(value)) {
    const items = value
      .slice(0, MAX_SAFE_COLLECTION_ITEMS)
      .map((item) => safeValue(item, redacted, depth + 1));
    if (value.length > MAX_SAFE_COLLECTION_ITEMS) items.push("[truncated]");
    return items;
  }
  const safe: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value).sort(([left], [right]) =>
    left < right ? -1 : left > right ? 1 : 0,
  )) {
    if (redacted.has(key) || isSensitiveField(key)) {
      redacted.add(key);
      continue;
    }
    safe[key] = safeValue(item, redacted, depth + 1);
  }
  return safe;
}

function safeEvent(event: SpanEvent, redacted: Set<string>): SafeSpanEvent {
  return {
    attributes: safeEntries(event.attributes ?? {}, redacted),
    name: event.name,
    occurredAt: event.occurred_at,
  };
}

export function safeSpanInspector(view: SpanView): SafeSpanInspector {
  const redacted = new Set([...view.redacted_attribute_keys, ...view.redacted_event_fields]);
  const attributes = safeEntries(view.span.attributes, redacted);
  const resource = safeEntries(view.span.resource, redacted);
  const events = view.span.events.map((event) => safeEvent(event, redacted));
  return {
    attributes,
    durationNs: view.span.duration_ns,
    endedAt: view.span.ended_at,
    error: view.span.error,
    events,
    name: view.span.name,
    parentSpanId: view.span.parent_span_id,
    redactedCount: redacted.size,
    resource,
    spanId: view.span.span_id,
    startedAt: view.span.started_at,
    status: view.span.status,
    traceId: view.span.trace_id,
  };
}

export function traceWaterfallSpans(items: readonly SpanView[]): TraceWaterfallSpan[] {
  if (items.length === 0) return [];
  const origin = Math.min(...items.map((item) => Date.parse(item.span.started_at)));
  return items.map(({ span }) => ({
    durationMs: span.duration_ns / 1_000_000,
    id: span.span_id,
    name: span.name,
    parentId: span.parent_span_id ?? undefined,
    startMs: Math.max(0, Date.parse(span.started_at) - origin),
    status: span.status === "error" ? "error" : span.status === "ok" ? "ok" : "running",
  }));
}

function sameDescriptorRef(left: MetricDescriptorRef, right: MetricDescriptorRef): boolean {
  return (
    left.metric_name === right.metric_name &&
    left.descriptor_version === right.descriptor_version &&
    left.descriptor_digest === right.descriptor_digest
  );
}

export function descriptorRef(descriptor: MetricDescriptor): MetricDescriptorRef {
  return {
    descriptor_digest: descriptor.descriptor_digest,
    descriptor_version: descriptor.descriptor_version,
    metric_name: descriptor.metric_name,
  };
}

export function requireExactMetricSeries(
  descriptors: readonly MetricDescriptor[],
  requestedRefs: readonly MetricDescriptorRef[],
  page: MetricPage,
): MetricPage["series"] {
  for (const series of page.series) {
    const requested = requestedRefs.some((item) => sameDescriptorRef(item, series.descriptor));
    const descriptor = descriptors.find((item) => sameDescriptorRef(descriptorRef(item), series.descriptor));
    if (
      !requested ||
      descriptor === undefined ||
      series.metric_name !== descriptor.metric_name ||
      series.metric_type !== descriptor.metric_type ||
      series.unit !== descriptor.unit ||
      JSON.stringify(series.bucket_bounds ?? []) !== JSON.stringify(descriptor.histogram_bucket_bounds)
    ) {
      throw new Error("Metric response is not bound to the selected exact descriptor.");
    }
  }
  return page.series;
}

export function observationValue(
  status: "reported" | "unavailable",
  value: number | string | null | undefined,
): string {
  return status === "reported" && value !== null && value !== undefined ? String(value) : "unavailable";
}

export function requireRunOwner(run: RunView, expectedRunId: string): RunView {
  if (run.run_id !== expectedRunId) {
    throw new Error("Run response is not bound to the selected Run.");
  }
  return run;
}

export function requireRunTracePageOwner(page: TraceSummaryPage, expectedRunId: string): TraceSummaryPage {
  if (page.items.some((trace) => !trace.run_ids.includes(expectedRunId))) {
    throw new Error("Trace page is not bound to the selected Run.");
  }
  return page;
}

export function requireTraceOwner(trace: TraceSummary, expectedTraceId: string): TraceSummary {
  if (trace.trace_id !== expectedTraceId) {
    throw new Error("Trace response is not bound to the selected Trace.");
  }
  return trace;
}

export function requireSpanPageOwner(page: SpanPage, expectedTraceId: string): SpanPage {
  if (
    page.trace_id !== expectedTraceId ||
    page.items.some((view) => view.span.trace_id !== expectedTraceId)
  ) {
    throw new Error("Span page is not bound to the selected Trace.");
  }
  return page;
}

export function requireRunCostOwner(cost: RunCostView, expectedRunId: string): RunCostView {
  if (cost.run_id !== expectedRunId || cost.budget_set.run_id !== expectedRunId) {
    throw new Error("Cost response is not bound to the selected Run.");
  }
  return cost;
}

export function traceSummaryTone(status: TraceSummary["status"]): "danger" | "info" | "ok" {
  return status === "error" ? "danger" : status === "ok" ? "ok" : "info";
}
