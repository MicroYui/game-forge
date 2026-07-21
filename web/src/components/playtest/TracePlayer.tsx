import { ChevronLeft, ChevronRight, Pause, Play, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useReducer, type KeyboardEvent } from "react";

import { Aureus2DRenderer } from "./Aureus2DRenderer";
import { GenericTraceRenderer } from "./GenericTraceRenderer";
import type { TracePlayback } from "./model";
import {
  resolveTraceRenderer,
  type TraceRendererFallbackReason,
  type TraceRendererRequest,
} from "./registry";
import "./playtest.css";

export type TracePlaybackSpeed = 0.5 | 1 | 2;

export interface TracePlayerState {
  currentIndex: number;
  isPlaying: boolean;
  speed: TracePlaybackSpeed;
}

export type TracePlayerAction =
  | { type: "play"; frameCount: number }
  | { type: "pause" }
  | { type: "toggle"; frameCount: number }
  | { type: "tick"; frameCount: number }
  | { type: "step-forward"; frameCount: number }
  | { type: "step-backward"; frameCount: number }
  | { type: "seek"; index: number; frameCount: number }
  | { type: "set-speed"; speed: TracePlaybackSpeed }
  | { type: "reset"; frameCount: number };

function maximumIndex(frameCount: number): number {
  return Math.max(0, frameCount - 1);
}

function clampedIndex(index: number, frameCount: number): number {
  return Math.min(maximumIndex(frameCount), Math.max(0, index));
}

export function createTracePlayerState(frameCount: number): TracePlayerState {
  return { currentIndex: clampedIndex(0, frameCount), isPlaying: false, speed: 1 };
}

export function tracePlayerReducer(state: TracePlayerState, action: TracePlayerAction): TracePlayerState {
  switch (action.type) {
    case "play":
      return { ...state, isPlaying: action.frameCount > 0 };
    case "pause":
      return { ...state, isPlaying: false };
    case "toggle":
      return { ...state, isPlaying: action.frameCount > 0 && !state.isPlaying };
    case "tick": {
      const nextIndex = clampedIndex(state.currentIndex + 1, action.frameCount);
      return {
        ...state,
        currentIndex: nextIndex,
        isPlaying: action.frameCount > 0 && nextIndex < maximumIndex(action.frameCount),
      };
    }
    case "step-forward":
      return {
        ...state,
        currentIndex: clampedIndex(state.currentIndex + 1, action.frameCount),
        isPlaying: false,
      };
    case "step-backward":
      return {
        ...state,
        currentIndex: clampedIndex(state.currentIndex - 1, action.frameCount),
        isPlaying: false,
      };
    case "seek":
      return {
        ...state,
        currentIndex: clampedIndex(action.index, action.frameCount),
        isPlaying: false,
      };
    case "set-speed":
      return { ...state, speed: action.speed };
    case "reset":
      return {
        currentIndex: clampedIndex(0, action.frameCount),
        isPlaying: false,
        speed: state.speed,
      };
  }
}

const fallbackMessages: Record<TraceRendererFallbackReason, string> = {
  "invalid-registry": "渲染器注册表校验失败，已切换到通用检查视图。",
  unknown: "未知渲染器，已切换到通用检查视图。",
  disabled: "请求的渲染器已停用，已切换到通用检查视图。",
  "environment-incompatible": "渲染器与环境契约不兼容，已切换到通用检查视图。",
  "schema-incompatible": "渲染器与轨迹 Schema 不兼容，已切换到通用检查视图。",
  "capability-mismatch": "环境未声明渲染器所需能力，已切换到通用检查视图。",
  "invalid-payload": "空间渲染载荷未通过边界校验，已切换到通用检查视图。",
};

export interface TracePlayerProps {
  rendererPayload?: unknown;
  rendererRequest?: TraceRendererRequest;
  tickDurationMs?: number;
  trace: TracePlayback;
}

export function TracePlayer({
  rendererPayload,
  rendererRequest,
  tickDurationMs = 900,
  trace,
}: TracePlayerProps) {
  const [state, dispatch] = useReducer(tracePlayerReducer, trace.frames.length, createTracePlayerState);
  const frameCount = trace.frames.length;
  const currentFrame = trace.frames[state.currentIndex];
  const resolution = useMemo(
    () =>
      resolveTraceRenderer(
        rendererRequest ?? {
          rendererId: "generic.timeline",
          rendererVersion: 1,
          environmentContractVersion: trace.environmentContractVersion,
          tracePayloadSchemaId: trace.tracePayloadSchemaId,
          capabilities: [],
        },
        rendererPayload,
      ),
    [rendererPayload, rendererRequest, trace.environmentContractVersion, trace.tracePayloadSchemaId],
  );

  useEffect(() => {
    dispatch({ type: "reset", frameCount });
  }, [frameCount, trace.traceId]);

  useEffect(() => {
    if (!state.isPlaying) return;
    const timer = window.setTimeout(
      () => dispatch({ type: "tick", frameCount }),
      tickDurationMs / state.speed,
    );
    return () => window.clearTimeout(timer);
  }, [frameCount, state.currentIndex, state.isPlaying, state.speed, tickDurationMs]);

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget) return;
    if (event.key === " ") {
      event.preventDefault();
      dispatch({ type: "toggle", frameCount });
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      dispatch({ type: "step-forward", frameCount });
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      dispatch({ type: "step-backward", frameCount });
    } else if (event.key === "Home") {
      event.preventDefault();
      dispatch({ type: "seek", index: 0, frameCount });
    } else if (event.key === "End") {
      event.preventDefault();
      dispatch({ type: "seek", index: maximumIndex(frameCount), frameCount });
    }
  };

  return (
    <section
      className="gf-trace"
      aria-label="Playtest 轨迹播放器"
      onKeyDown={onKeyDown}
      role="region"
      tabIndex={0}
    >
      <header className="gf-trace__header">
        <div>
          <p className="gf-trace__eyebrow">Playtest Trace</p>
          <h2>可复现轨迹回放</h2>
          <code>{trace.traceId}</code>
        </div>
        <dl>
          <div>
            <dt>环境契约</dt>
            <dd>{trace.environmentContractVersion}</dd>
          </div>
          <div>
            <dt>轨迹 Schema</dt>
            <dd>{trace.tracePayloadSchemaId}</dd>
          </div>
          <div>
            <dt>初始 state_hash</dt>
            <dd>{trace.initialStateHash}</dd>
          </div>
          <div>
            <dt>最终 state_hash</dt>
            <dd>{trace.finalStateHash}</dd>
          </div>
        </dl>
      </header>

      {resolution.fallbackReason && (
        <p className="gf-trace__fallback" role="status">
          {fallbackMessages[resolution.fallbackReason]}
        </p>
      )}

      <div className="gf-trace__transport" aria-label="轨迹播放控制">
        <div className="gf-trace__transport-buttons">
          <button
            type="button"
            aria-label="回到开头"
            data-tooltip="回到开头 (Home)"
            disabled={frameCount === 0 || state.currentIndex === 0}
            onClick={() => dispatch({ type: "seek", index: 0, frameCount })}
          >
            <RotateCcw aria-hidden="true" size={16} />
          </button>
          <button
            type="button"
            aria-label="后退一步"
            data-tooltip="后退一步 (←)"
            disabled={frameCount === 0 || state.currentIndex === 0}
            onClick={() => dispatch({ type: "step-backward", frameCount })}
          >
            <ChevronLeft aria-hidden="true" size={18} />
          </button>
          <button
            className="gf-trace__play"
            type="button"
            aria-label={state.isPlaying ? "暂停" : "播放"}
            data-tooltip={state.isPlaying ? "暂停 (空格)" : "播放 (空格)"}
            disabled={frameCount === 0}
            onClick={() => dispatch({ type: "toggle", frameCount })}
          >
            {state.isPlaying ? <Pause aria-hidden="true" size={17} /> : <Play aria-hidden="true" size={17} />}
          </button>
          <button
            type="button"
            aria-label="前进一步"
            data-tooltip="前进一步 (→)"
            disabled={frameCount === 0 || state.currentIndex >= maximumIndex(frameCount)}
            onClick={() => dispatch({ type: "step-forward", frameCount })}
          >
            <ChevronRight aria-hidden="true" size={18} />
          </button>
        </div>

        <div className="gf-trace__transport-position">
          <strong>{frameCount === 0 ? "0 / 0" : `${state.currentIndex + 1} / ${frameCount}`}</strong>
          {currentFrame ? (
            <>
              <span>Tick {currentFrame.tick}</span>
              <code>{currentFrame.stateHash}</code>
            </>
          ) : (
            <span>无动作帧</span>
          )}
        </div>

        <label className="gf-trace__speed">
          <span>播放速度</span>
          <select
            aria-label="播放速度"
            value={String(state.speed)}
            onChange={(event) =>
              dispatch({
                type: "set-speed",
                speed: Number(event.currentTarget.value) as TracePlaybackSpeed,
              })
            }
          >
            <option value="0.5">0.5×</option>
            <option value="1">1×</option>
            <option value="2">2×</option>
          </select>
        </label>
      </div>

      <div className="gf-trace__renderer">
        {resolution.componentKey === "aureus-2d" && resolution.validatedPayload ? (
          <Aureus2DRenderer
            currentIndex={state.currentIndex}
            onSeek={(index) => dispatch({ type: "seek", index, frameCount })}
            payload={resolution.validatedPayload}
            trace={trace}
          />
        ) : (
          <GenericTraceRenderer
            currentIndex={state.currentIndex}
            onSeek={(index) => dispatch({ type: "seek", index, frameCount })}
            trace={trace}
          />
        )}
      </div>

      <p className="gf-trace__keyboard-help">
        键盘：播放器获得焦点后，用空格播放或暂停，左右方向键逐步检查，Home / End 跳转首尾。
      </p>
    </section>
  );
}

export {
  adaptPlaytestEpisodeTrace,
  type PlaytestEpisodeTraceSelection,
  type TraceFindingLink,
  type TraceFrame,
  type TraceMarker,
  type TracePlayback,
} from "./model";
