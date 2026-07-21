import { useId } from "react";

import type { components } from "../../api/generated/openapi";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";

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

export function RunProgress({ run, events, commands = [], traceHref }: RunProgressProps) {
  const titleId = useId();
  const progress = latestProgress(events);
  const traceId = events.find(({ event }) => event.trace_id)?.event.trace_id;

  return (
    <section aria-labelledby={titleId} className="gf-run-progress">
      <h2 id={titleId}>运行进度</h2>
      <dl>
        <div>
          <dt>运行</dt>
          <dd>{run.run_id}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{run.status}</dd>
        </div>
        <div>
          <dt>修订</dt>
          <dd>{run.revision}</dd>
        </div>
      </dl>

      {progress && (
        <div>
          <progress
            aria-label={progress.phase_code}
            max={progress.total_units ?? undefined}
            value={progress.total_units === null ? undefined : progress.completed_units}
          />
          <span>
            已完成 {progress.completed_units}
            {progress.total_units === null ? "" : ` / ${progress.total_units}`}
          </span>
        </div>
      )}

      <nav aria-label="运行证据">
        {run.result_artifact_id && <a href={artifactHref(run.result_artifact_id)}>结果工件</a>}
        {run.failure_artifact_id && <a href={artifactHref(run.failure_artifact_id)}>失败清单</a>}
        {run.terminal_cassette_artifact_id && (
          <a href={artifactHref(run.terminal_cassette_artifact_id)}>终态 cassette</a>
        )}
        {traceId && traceHref && <a href={traceHref}>追踪 {traceId}</a>}
      </nav>

      <h3>事件</h3>
      <ol>
        {events.map(({ cursor, event }) => (
          <li key={`${event.run_id}:${cursor}`}>
            {event.event_type} · {event.occurred_at}
          </li>
        ))}
      </ol>

      <h3>命令</h3>
      <ol>
        {commands.map((command) => (
          <li key={command.command_id}>
            {command.command_id} · {command.status}
          </li>
        ))}
      </ol>
    </section>
  );
}
