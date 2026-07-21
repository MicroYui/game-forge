import { Check, Copy, ExternalLink, ShieldCheck } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import type { components } from "../../api/generated/openapi";

import "./logs.css";

type LogRecordView = components["schemas"]["LogRecordViewV1"];
type JsonValue = components["schemas"]["JsonValue"];

const LEVEL_LABELS: Record<LogRecordView["record"]["level"], string> = {
  critical: "严重",
  debug: "调试",
  error: "错误",
  info: "信息",
  warning: "警告",
};

const SENSITIVE_FIELD_MARKERS = [
  "access_token",
  "api_key",
  "authorization",
  "client_secret",
  "credential",
  "debug",
  "handler_config",
  "id_token",
  "password",
  "prompt",
  "prompt_text",
  "raw_prompt",
  "raw_payload",
  "raw_response",
  "refresh_token",
  "rendered_prompt",
  "response_body",
  "session_token",
  "secret",
  "system_prompt",
  "user_prompt",
] as const;
const MAX_SAFE_FIELD_DEPTH = 8;
const MAX_SAFE_COLLECTION_ITEMS = 128;

export interface LogExplorerProps {
  emptyMessage?: string;
  items: readonly LogRecordView[];
  title?: string;
  traceHref?: (traceId: string) => string;
}

interface SafeLogView {
  fields: readonly [string, JsonValue][];
  redactedCount: number;
  view: LogRecordView;
}

function isSensitiveField(key: string): boolean {
  const normalized = compactSensitiveKey(key);
  return (
    normalized === "prompt" ||
    normalized === "response" ||
    normalized === "secret" ||
    SENSITIVE_FIELD_MARKERS.some((marker) => normalized.includes(compactSensitiveKey(marker)))
  );
}

function compactSensitiveKey(value: string): string {
  return value.toLocaleLowerCase("en-US").replace(/[-._]/g, "");
}

function safeLog(view: LogRecordView): SafeLogView {
  const redacted = new Set(view.redacted_fields);
  const fields: [string, JsonValue][] = [];

  const entries = Object.entries(view.record.fields ?? {}).sort(([left], [right]) =>
    left < right ? -1 : left > right ? 1 : 0,
  );
  for (const [key, value] of entries) {
    if (key === "redacted_fields") continue;
    if (redacted.has(key) || isSensitiveField(key)) {
      redacted.add(key);
      continue;
    }
    fields.push([key, safeFieldValue(value, redacted, 0)]);
  }

  return { fields, redactedCount: redacted.size, view };
}

function safeFieldValue(value: JsonValue, redacted: Set<string>, depth: number): JsonValue {
  if (value === null || typeof value !== "object") return value;
  if (depth >= MAX_SAFE_FIELD_DEPTH) return "[bounded]";
  if (Array.isArray(value)) {
    const items = value
      .slice(0, MAX_SAFE_COLLECTION_ITEMS)
      .map((item) => safeFieldValue(item, redacted, depth + 1));
    if (value.length > MAX_SAFE_COLLECTION_ITEMS) items.push("[truncated]");
    return items;
  }
  const safe: Record<string, JsonValue> = {};
  for (const [key, item] of Object.entries(value)
    .slice(0, MAX_SAFE_COLLECTION_ITEMS)
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))) {
    if (redacted.has(key) || isSensitiveField(key)) {
      redacted.add(key);
      continue;
    }
    safe[key] = safeFieldValue(item, redacted, depth + 1);
  }
  return safe;
}

function valueText(value: JsonValue): string {
  if (typeof value === "string") return value;
  if (value === null) return "null";
  return JSON.stringify(value);
}

function safeLogText({ fields, view }: SafeLogView): string {
  const { record } = view;
  const lines = [`${record.ts_utc} ${record.level} ${record.service} ${record.event_name}`, record.message];
  for (const [label, value] of [
    ["log_id", record.log_id],
    ["request_id", record.request_id],
    ["run_id", record.run_id],
    ["trace_id", record.trace_id],
    ["span_id", record.span_id],
    ["producer_run_id", record.producer_run_id],
  ] as const) {
    if (value) lines.push(`${label}=${value}`);
  }
  if (record.error) {
    lines.push(`error_type=${record.error.error_type}`);
    lines.push(`error_message=${record.error.message}`);
    if (record.error.stack_fingerprint) {
      lines.push(`stack_fingerprint=${record.error.stack_fingerprint}`);
    }
  }
  for (const [key, value] of fields) lines.push(`${key}=${valueText(value)}`);
  return lines.join("\n");
}

interface CopyButtonProps {
  label: string;
  successLabel: string;
  value: string;
}

function CopyButton({ label, successLabel, value }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(value);
    setCopied(true);
  };

  return (
    <button
      aria-label={label}
      className="gf-log__copy"
      data-tooltip={copied ? successLabel : label}
      onClick={() => void copy()}
      type="button"
    >
      {copied ? (
        <Check aria-hidden="true" size={14} strokeWidth={1.8} />
      ) : (
        <Copy aria-hidden="true" size={14} strokeWidth={1.8} />
      )}
      <span>{copied ? successLabel : label}</span>
    </button>
  );
}

interface IdentifierProps {
  copyLabel: string;
  label: string;
  value: string;
  valueNode?: ReactNode;
}

function Identifier({ copyLabel, label, value, valueNode }: IdentifierProps) {
  return (
    <div className="gf-log__identifier">
      <dt>{label}</dt>
      <dd>
        {valueNode ?? <code>{value}</code>}
        <CopyButton label={copyLabel} successLabel="已复制" value={value} />
      </dd>
    </div>
  );
}

export function LogExplorer({
  emptyMessage = "当前范围没有日志。",
  items,
  title = "运行日志",
  traceHref = (traceId) => `/observability/traces/${encodeURIComponent(traceId)}`,
}: LogExplorerProps) {
  const safeItems = items.map(safeLog);
  const titleId = useId();

  return (
    <section className="gf-log" aria-labelledby={titleId}>
      <div className="gf-log__heading">
        <h2 id={titleId}>{title}</h2>
        <span>{items.length} 条</span>
      </div>

      {safeItems.length === 0 ? (
        <p className="gf-log__empty">{emptyMessage}</p>
      ) : (
        <ol className="gf-log__list">
          {safeItems.map((item) => {
            const { record } = item.view;
            return (
              <li key={record.log_id}>
                <article className="gf-log__entry">
                  <header className="gf-log__toolbar">
                    <div className="gf-log__primary-meta">
                      <span className="gf-log__level" data-level={record.level}>
                        {LEVEL_LABELS[record.level]} · {record.level}
                      </span>
                      <time dateTime={record.ts_utc}>{record.ts_utc}</time>
                    </div>
                    <CopyButton
                      label="复制安全日志"
                      successLabel="已复制安全日志"
                      value={safeLogText(item)}
                    />
                  </header>

                  <div className="gf-log__source">
                    <span>{record.service}</span>
                    <code>{record.event_name}</code>
                  </div>
                  <p className="gf-log__message">{record.message}</p>

                  {item.redactedCount > 0 && (
                    <p className="gf-log__redaction">
                      <ShieldCheck aria-hidden="true" size={15} strokeWidth={1.8} />
                      {item.redactedCount} 个字段已脱敏
                    </p>
                  )}

                  <dl className="gf-log__identifiers">
                    <Identifier copyLabel="复制日志 ID" label="日志 ID" value={record.log_id} />
                    {record.request_id && (
                      <Identifier copyLabel="复制请求 ID" label="请求 ID" value={record.request_id} />
                    )}
                    {record.run_id && (
                      <Identifier copyLabel="复制运行 ID" label="运行 ID" value={record.run_id} />
                    )}
                    {record.trace_id && (
                      <Identifier
                        copyLabel="复制追踪 ID"
                        label="追踪 ID"
                        value={record.trace_id}
                        valueNode={
                          <a aria-label={`查看追踪 ${record.trace_id}`} href={traceHref(record.trace_id)}>
                            <code>{record.trace_id}</code>
                            <ExternalLink aria-hidden="true" size={13} strokeWidth={1.8} />
                          </a>
                        }
                      />
                    )}
                    {record.span_id && (
                      <Identifier copyLabel="复制 Span ID" label="Span ID" value={record.span_id} />
                    )}
                    {record.producer_run_id && (
                      <Identifier
                        copyLabel="复制生产运行 ID"
                        label="生产运行 ID"
                        value={record.producer_run_id}
                      />
                    )}
                  </dl>

                  {record.error && (
                    <div className="gf-log__error">
                      <strong>{record.error.error_type}</strong>
                      <span>{record.error.message}</span>
                      {record.error.stack_fingerprint && <code>{record.error.stack_fingerprint}</code>}
                    </div>
                  )}

                  {item.fields.length > 0 && (
                    <dl className="gf-log__fields">
                      {item.fields.map(([key, value]) => (
                        <div key={key}>
                          <dt>{key}</dt>
                          <dd>{valueText(value)}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </article>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
