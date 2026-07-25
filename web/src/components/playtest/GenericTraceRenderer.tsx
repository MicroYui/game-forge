import { AlertTriangle, CheckCircle2, CircleDotDashed, Link2, Repeat2 } from "lucide-react";
import { useEffect, useId, useMemo, useState, type ReactNode, type SyntheticEvent } from "react";

import { TechnicalDetails } from "../identity";
import type { TraceMarker, TraceMarkerKind, TracePlayback } from "./model";

const TIMELINE_BATCH_SIZE = 100;

const markerLabels: Record<TraceMarkerKind, string> = {
  completion: "完成",
  failure: "失败",
  step_limit: "步数上限",
  stuck: "卡死",
  loop: "循环",
};

function MarkerIcon({ kind }: { kind: TraceMarkerKind }) {
  if (kind === "completion") return <CheckCircle2 aria-hidden="true" size={14} />;
  if (kind === "loop") return <Repeat2 aria-hidden="true" size={14} />;
  if (kind === "stuck") return <CircleDotDashed aria-hidden="true" size={14} />;
  return <AlertTriangle aria-hidden="true" size={14} />;
}

function TextBlock({ children, label }: { children: ReactNode; label: string }) {
  return (
    <section className="gf-trace__json-block" aria-label={label}>
      <h3>{label}</h3>
      <div className="gf-trace__text-block">{children}</div>
    </section>
  );
}

function recordKind(value: unknown): string | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const kind = (value as Record<string, unknown>).kind;
  return typeof kind === "string" && kind.length > 0 ? kind : null;
}

function actionSummary(action: unknown): string {
  const labels: Readonly<Record<string, string>> = {
    attack: "发起攻击",
    interact: "与目标互动",
    navigate_to: "前往目标",
    observe: "观察环境",
    use_item: "使用道具",
  };
  const kind = recordKind(action);
  return kind ? (labels[kind] ?? "执行游戏操作") : "执行游戏操作";
}

function resultSummary(result: unknown): string {
  if (typeof result !== "string") return "已记录操作结果";
  const labels: Readonly<Record<string, string>> = {
    arrived: "已到达",
    blocked: "操作受阻",
    ok: "操作成功",
    agent_stopped: "自动试玩已停止",
    attack_resolved: "攻击已结算",
    interacted: "互动已完成",
    observed: "观察已完成",
    quest_accepted: "已接取任务",
    quest_completed: "任务已完成",
    quest_progressed: "任务已推进",
  };
  return labels[result] ?? "已记录操作结果";
}

function technicalJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "null";
}

function MarkerDetails({ marker }: { marker: TraceMarker }) {
  return (
    <div className="gf-trace__marker-detail" data-marker-kind={marker.kind}>
      <strong>
        <MarkerIcon kind={marker.kind} />
        {markerLabels[marker.kind]}
      </strong>
      <span>{marker.detail || "无附加说明"}</span>
      {marker.findings.length > 0 && (
        <ul aria-label={`${markerLabels[marker.kind]}关联 Finding`}>
          {marker.findings.map((finding) => (
            <li key={`${finding.findingId}:${finding.revision}`}>
              <a href={finding.href}>
                <Link2 aria-hidden="true" size={13} />
                查看关联问题 · 第 {finding.revision} 版
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RawTraceDetails({ trace }: { trace: TracePlayback }) {
  const [isOpen, setIsOpen] = useState(false);
  const onToggle = (event: SyntheticEvent<HTMLDetailsElement>) => {
    setIsOpen(event.currentTarget.open);
  };

  return (
    <details className="gf-trace__raw" onToggle={onToggle}>
      <summary>查看完整轨迹原始数据（高级）</summary>
      {isOpen && (
        <pre aria-label="完整轨迹原始 JSON" tabIndex={0}>
          {JSON.stringify(trace.rawPayload, null, 2)}
        </pre>
      )}
    </details>
  );
}

interface GenericTraceRendererProps {
  currentIndex: number;
  onSeek(index: number): void;
  trace: TracePlayback;
}

export function GenericTraceRenderer({ currentIndex, onSeek, trace }: GenericTraceRendererProps) {
  const timelineTitleId = useId();
  const currentFrame = trace.frames[currentIndex];
  const [visibleLimit, setVisibleLimit] = useState(() => Math.min(TIMELINE_BATCH_SIZE, trace.frames.length));

  useEffect(() => {
    setVisibleLimit(Math.min(TIMELINE_BATCH_SIZE, trace.frames.length));
  }, [trace.frames.length, trace.traceId]);

  const visibleIndices = useMemo(() => {
    const boundedLimit = Math.min(visibleLimit, trace.frames.length);
    const indices = new Set(Array.from({ length: boundedLimit }, (_, index) => index));
    if (currentIndex >= boundedLimit && currentIndex < trace.frames.length) indices.add(currentIndex);
    for (const marker of trace.markers) {
      if (marker.frameIndex !== null && marker.frameIndex < trace.frames.length) {
        indices.add(marker.frameIndex);
      }
    }
    return [...indices].sort((left, right) => left - right);
  }, [currentIndex, trace.frames.length, trace.markers, visibleLimit]);
  const markersAt = (index: number) => trace.markers.filter((marker) => marker.frameIndex === index);

  return (
    <div className="gf-trace__generic">
      <section className="gf-trace__inspection" aria-label="当前步骤详情">
        {currentFrame ? (
          <>
            <TextBlock label="本步操作">
              <p>{actionSummary(currentFrame.action)}</p>
            </TextBlock>
            <TextBlock label="操作结果">
              <p>{resultSummary(currentFrame.lastActionResult)}</p>
            </TextBlock>
            <TextBlock label="状态">
              <p>已保存本步后的状态指纹</p>
              <span>如需排查底层状态，可展开本步技术信息。</span>
            </TextBlock>
            <TextBlock label="事件">
              <p>没有单独的事件明细</p>
              <span>界面不会根据动作或结果猜测未记录的事件。</span>
            </TextBlock>
            <TechnicalDetails
              items={[
                { label: "动作数据", value: technicalJson(currentFrame.action) },
                { label: "动作结果数据", value: technicalJson(currentFrame.lastActionResult) },
                { label: "状态指纹", value: currentFrame.stateHash },
                { label: "时间刻度", value: String(currentFrame.tick) },
              ]}
              summary="查看本步技术信息"
            />
          </>
        ) : (
          <p className="gf-trace__empty">这次试玩没有动作记录；原始数据仍可在下方检查。</p>
        )}
      </section>

      <section className="gf-trace__timeline" aria-labelledby={timelineTitleId}>
        <div className="gf-trace__section-heading">
          <div>
            <p>逐步操作</p>
            <h3 id={timelineTitleId}>操作时间轴</h3>
          </div>
          <span>
            已呈现 {visibleIndices.length} / {trace.frames.length} 步
          </span>
        </div>
        {trace.frames.length === 0 ? (
          <p className="gf-trace__empty">暂无动作记录。</p>
        ) : (
          <ol aria-label="操作时间轴">
            {visibleIndices.map((index) => {
              const frame = trace.frames[index];
              const frameMarkers = markersAt(index);
              return (
                <li key={frame.frameId} data-current={index === currentIndex || undefined}>
                  <button
                    type="button"
                    aria-current={index === currentIndex}
                    aria-label={`第 ${index + 1} 步`}
                    onClick={() => onSeek(index)}
                  >
                    <span className="gf-trace__timeline-index">{String(index + 1).padStart(2, "0")}</span>
                    <span className="gf-trace__timeline-main">
                      <strong>第 {index + 1} 步</strong>
                      <span className="gf-trace__timeline-payload">
                        操作：{actionSummary(frame.action)} · 结果：{resultSummary(frame.lastActionResult)}
                      </span>
                    </span>
                    <span className="gf-trace__timeline-result">{resultSummary(frame.lastActionResult)}</span>
                  </button>
                  {frameMarkers.map((marker) => (
                    <MarkerDetails
                      key={`${marker.kind}:${marker.frameIndex}:${marker.stateHash}`}
                      marker={marker}
                    />
                  ))}
                </li>
              );
            })}
          </ol>
        )}
        {visibleLimit < trace.frames.length && (
          <button
            className="gf-trace__load-more"
            type="button"
            aria-label={`再加载 ${Math.min(TIMELINE_BATCH_SIZE, trace.frames.length - visibleLimit)} 步`}
            onClick={() =>
              setVisibleLimit((current) => Math.min(trace.frames.length, current + TIMELINE_BATCH_SIZE))
            }
          >
            再加载 {Math.min(TIMELINE_BATCH_SIZE, trace.frames.length - visibleLimit)} 步
          </button>
        )}
      </section>

      {trace.markers.some((marker) => marker.frameIndex === null) && (
        <section className="gf-trace__unbound-markers" aria-label="无动作帧轨迹标记">
          {trace.markers
            .filter((marker) => marker.frameIndex === null)
            .map((marker) => (
              <MarkerDetails key={`${marker.kind}:${marker.stateHash}`} marker={marker} />
            ))}
        </section>
      )}

      <RawTraceDetails trace={trace} />
    </div>
  );
}
