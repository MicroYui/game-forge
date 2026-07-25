import { useId } from "react";

import type { components } from "../../api/generated/openapi";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";
import { compactDateTime, TechnicalDetails } from "../identity";

type RunView = components["schemas"]["RunViewV1"];
type RunCommandView = components["schemas"]["RunCommandViewV1"];

export interface RunProgressProps {
  run: RunView;
  events: readonly RunEventItem[];
  commands?: readonly RunCommandView[];
  traceHref?: string;
}

export interface RunEventItem {
  cursor: string;
  event: RunEvent;
}

function latestProgress(events: readonly RunEventItem[]) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]?.event;
    if (event?.event_type === "attempt.progress") return event.data;
  }
  return undefined;
}

function artifactHref(artifactId: string): string {
  return `/artifacts/${encodeURIComponent(artifactId)}`;
}

const runStatusLabels: Record<string, string> = {
  cancelled: "已取消",
  failed: "失败",
  queued: "等待开始",
  running: "进行中",
  succeeded: "已完成",
  timed_out: "已超时",
};

const eventLabels: Record<string, string> = {
  "attempt.completed": "本次尝试完成",
  "attempt.failed": "本次尝试失败",
  "attempt.progress": "进度更新",
  "attempt.started": "开始执行",
  "run.cancelled": "运行已取消",
  "run.failed": "运行失败",
  "run.queued": "已进入队列",
  "run.started": "运行已开始",
  "run.succeeded": "运行已完成",
  "run.timed_out": "运行已超时",
};

const commandLabels: Record<string, string> = {
  cancel: "取消运行",
  retry: "重新尝试",
};

const commandStatusLabels: Record<string, string> = {
  applied: "已执行",
  pending: "等待执行",
  rejected: "未执行",
};

function phaseLabel(value: string): string {
  return (
    {
      checking: "自动检查",
      discovering: "分析内容",
      "generation.preliminary_gate": "生成前检查",
    }[value] ?? "处理中"
  );
}

export function RunProgress({ run, events, commands = [], traceHref }: RunProgressProps) {
  const titleId = useId();
  const progress = latestProgress(events);
  const traceId = events.find(({ event }) => event.trace_id)?.event.trace_id;

  return (
    <section aria-labelledby={titleId} className="gf-run-progress">
      <h2 id={titleId}>运行进度</h2>
      <dl>
        <div>
          <dt>状态</dt>
          <dd>{runStatusLabels[run.status] ?? run.status}</dd>
        </div>
        <div>
          <dt>进度版本</dt>
          <dd>第 {run.revision} 版</dd>
        </div>
      </dl>
      <TechnicalDetails
        items={[
          { label: "运行 ID", value: run.run_id },
          { label: "运行视图版本", value: run.view_schema_version },
          { label: "状态资源", value: run.status_url },
          { label: "事件资源", value: run.events_url },
        ]}
        summary="查看运行技术信息"
      />

      {progress && (
        <div>
          <progress
            aria-label={phaseLabel(progress.phase_code)}
            max={progress.total_units ?? undefined}
            value={progress.total_units === null ? undefined : progress.completed_units}
          />
          <span>
            已完成 {progress.completed_units}
            {progress.total_units === null ? "" : ` / ${progress.total_units}`}
          </span>
          <TechnicalDetails
            items={[
              { label: "阶段代码", value: progress.phase_code },
              { label: "已完成单元", value: String(progress.completed_units) },
              {
                label: "总单元",
                value: progress.total_units === null ? "未提供" : String(progress.total_units),
              },
            ]}
            summary="查看进度技术信息"
          />
        </div>
      )}

      <nav aria-label="运行证据">
        {run.result_artifact_id && <a href={artifactHref(run.result_artifact_id)}>结果工件</a>}
        {run.failure_artifact_id && <a href={artifactHref(run.failure_artifact_id)}>失败清单</a>}
        {run.terminal_cassette_artifact_id && (
          <a href={artifactHref(run.terminal_cassette_artifact_id)}>终态 cassette</a>
        )}
        {traceId && traceHref && <a href={traceHref}>查看运行追踪</a>}
      </nav>

      <h3>事件</h3>
      <ol>
        {events.map(({ cursor, event }) => (
          <li key={`${event.run_id}:${cursor}`}>
            {eventLabels[event.event_type] ?? "运行状态更新"} · {compactDateTime(event.occurred_at)}
            <TechnicalDetails
              items={[
                { label: "事件类型", value: event.event_type },
                { label: "事件游标", value: cursor },
                { label: "运行 ID", value: event.run_id },
                ...(event.trace_id ? [{ label: "追踪 ID", value: event.trace_id }] : []),
              ]}
              summary="查看事件技术信息"
            />
          </li>
        ))}
      </ol>

      <h3>命令</h3>
      <ol>
        {commands.map((command) => (
          <li key={command.command_id}>
            {commandLabels[command.type] ?? "运行操作"}
            {" · "}
            {commandStatusLabels[command.status] ?? command.status}
            <TechnicalDetails
              items={[
                { label: "命令 ID", value: command.command_id },
                { label: "运行 ID", value: command.run_id },
                { label: "命令类型", value: command.type },
                { label: "命令状态", value: command.status },
              ]}
              summary="查看操作技术信息"
            />
          </li>
        ))}
      </ol>
    </section>
  );
}
