import { CircleAlert, CircleCheck, LoaderCircle } from "lucide-react";

import { ChartFrame } from "./ChartFrame";

export type TraceSpanStatus = "ok" | "error" | "running";

export interface TraceWaterfallSpan {
  durationMs: number;
  id: string;
  name: string;
  parentId?: string;
  startMs: number;
  status: TraceSpanStatus;
}

export interface TraceWaterfallProps {
  spans: readonly TraceWaterfallSpan[];
  summary: string;
  title: string;
}

const STATUS = {
  error: { Icon: CircleAlert, label: "错误" },
  ok: { Icon: CircleCheck, label: "成功" },
  running: { Icon: LoaderCircle, label: "进行中" },
} as const;

const formatMilliseconds = (value: number) => `${new Intl.NumberFormat("zh-CN").format(value)} ms`;

export function TraceWaterfall({ spans, summary, title }: TraceWaterfallProps) {
  const timelineEnd = Math.max(1, ...spans.map((span) => span.startMs + span.durationMs));

  return (
    <ChartFrame
      className="gf-chart--waterfall"
      columns={[
        { key: "name", label: "跨度" },
        { key: "id", label: "Span ID" },
        { key: "parent", label: "父 Span" },
        { key: "start", label: "开始" },
        { key: "duration", label: "耗时" },
        { key: "status", label: "状态" },
      ]}
      rows={spans.map((span) => ({
        duration: formatMilliseconds(span.durationMs),
        id: <code>{span.id}</code>,
        name: span.name,
        parent: span.parentId ? <code>{span.parentId}</code> : "根跨度",
        start: formatMilliseconds(span.startMs),
        status: STATUS[span.status].label,
      }))}
      summary={summary}
      title={title}
    >
      <div aria-label={`${title}时间轴`} className="gf-chart__plot gf-chart__plot--waterfall" tabIndex={0}>
        {spans.length === 0 ? (
          <p className="gf-chart__empty">暂无跨度</p>
        ) : (
          <ol className="gf-waterfall">
            {spans.map((span) => {
              const { Icon, label } = STATUS[span.status];
              const left = (span.startMs / timelineEnd) * 100;
              const width = Math.max((span.durationMs / timelineEnd) * 100, 1);
              return (
                <li className="gf-waterfall__row" key={span.id}>
                  <div className="gf-waterfall__identity">
                    <span className="gf-waterfall__name">{span.name}</span>
                    <span className="gf-waterfall__status" data-status={span.status}>
                      <Icon aria-hidden="true" size={14} strokeWidth={1.8} />
                      {label}
                    </span>
                  </div>
                  <div className="gf-waterfall__track" aria-hidden="true">
                    <span
                      className="gf-waterfall__bar"
                      data-status={span.status}
                      style={{ left: `${left}%`, width: `${width}%` }}
                    />
                  </div>
                  <span className="gf-waterfall__timing">
                    {formatMilliseconds(span.startMs)} → {formatMilliseconds(span.startMs + span.durationMs)}
                  </span>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </ChartFrame>
  );
}
