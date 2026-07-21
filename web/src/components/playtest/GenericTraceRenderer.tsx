import { AlertTriangle, CheckCircle2, CircleDotDashed, Link2, Repeat2 } from "lucide-react";
import { useEffect, useId, useMemo, useState, type ReactNode, type SyntheticEvent } from "react";

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

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <section className="gf-trace__json-block" aria-label={label}>
      <h3>{label}</h3>
      <pre aria-label={`${label} 滚动区`} tabIndex={0}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </section>
  );
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
  return recordKind(action) ?? "结构化动作";
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
                {finding.findingId} · r{finding.revision}
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
      <summary>完整 PlaytestTraceV1 原始 JSON</summary>
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
      <section className="gf-trace__inspection" aria-label="当前轨迹帧契约数据">
        {currentFrame ? (
          <>
            <JsonBlock label="动作 JSON" value={currentFrame.action} />
            <JsonBlock label="动作结果" value={currentFrame.lastActionResult} />
            <TextBlock label="状态">
              <p>此契约未提供</p>
              <span>playtest-trace@1 只记录动作后的 state_hash，不包含 Observation/state JSON。</span>
            </TextBlock>
            <TextBlock label="事件">
              <p>此契约未提供</p>
              <span>playtest-trace@1 没有事件数组；界面不会从动作或结果推测事件。</span>
            </TextBlock>
          </>
        ) : (
          <p className="gf-trace__empty">这条 episode 没有动作记录；原始载荷仍可在下方检查。</p>
        )}
      </section>

      <section className="gf-trace__timeline" aria-labelledby={timelineTitleId}>
        <div className="gf-trace__section-heading">
          <div>
            <p>PlaytestActionRecordV1</p>
            <h3 id={timelineTitleId}>有界动作时间轴</h3>
          </div>
          <span>
            已呈现 {visibleIndices.length} / {trace.frames.length} 帧
          </span>
        </div>
        {trace.frames.length === 0 ? (
          <p className="gf-trace__empty">暂无动作记录。</p>
        ) : (
          <ol aria-label="有界动作时间轴">
            {visibleIndices.map((index) => {
              const frame = trace.frames[index];
              const frameMarkers = markersAt(index);
              return (
                <li key={frame.frameId} data-current={index === currentIndex || undefined}>
                  <button
                    type="button"
                    aria-current={index === currentIndex}
                    aria-label={`第 ${index + 1} 帧，Tick ${frame.tick}`}
                    onClick={() => onSeek(index)}
                  >
                    <span className="gf-trace__timeline-index">{String(index + 1).padStart(2, "0")}</span>
                    <span className="gf-trace__timeline-main">
                      <strong>Tick {frame.tick}</strong>
                      <code>{frame.stateHash}</code>
                      <span className="gf-trace__timeline-payload">
                        动作：{actionSummary(frame.action)} · 事件：此契约未提供
                      </span>
                    </span>
                    <span className="gf-trace__timeline-result">{frame.lastActionResult}</span>
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
            aria-label={`再加载 ${Math.min(TIMELINE_BATCH_SIZE, trace.frames.length - visibleLimit)} 帧`}
            onClick={() =>
              setVisibleLimit((current) => Math.min(trace.frames.length, current + TIMELINE_BATCH_SIZE))
            }
          >
            再加载 {Math.min(TIMELINE_BATCH_SIZE, trace.frames.length - visibleLimit)} 帧
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
