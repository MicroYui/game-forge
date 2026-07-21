import { useId, type CSSProperties } from "react";

import type { TracePlayback } from "./model";
import type { AureusSpatial2DPayload, AureusSpatialPoint } from "./registry";
import { GenericTraceRenderer } from "./GenericTraceRenderer";

function position(point: AureusSpatialPoint, width: number, height: number): CSSProperties {
  return {
    left: `${((point.x + 0.5) / width) * 100}%`,
    top: `${((point.y + 0.5) / height) * 100}%`,
  };
}

interface Aureus2DRendererProps {
  currentIndex: number;
  onSeek(index: number): void;
  payload: AureusSpatial2DPayload;
  trace: TracePlayback;
}

export function Aureus2DRenderer({ currentIndex, onSeek, payload, trace }: Aureus2DRendererProps) {
  const mapTitleId = useId();
  const traceFrame = trace.frames[currentIndex];
  const spatialFrame = traceFrame
    ? payload.frames.find((frame) => frame.frame_id === traceFrame.frameId)
    : undefined;
  const { width, height } = payload.map;
  const mapMaxWidth = Math.min(780, (420 * width) / height);

  return (
    <div className="gf-trace__aureus">
      <section className="gf-trace__map-panel" aria-labelledby={mapTitleId}>
        <div className="gf-trace__section-heading">
          <div>
            <p>内置 fixture · spatial_2d</p>
            <h3 id={mapTitleId}>Aureus 2D 独立展示</h3>
          </div>
          <code>
            {width} × {height}
          </code>
        </div>
        <div
          className="gf-trace__map"
          role="img"
          aria-label={`Aureus 独立 2D 展示，地图 ${width} × ${height}`}
          style={{
            backgroundSize: `${100 / width}% ${100 / height}%`,
            aspectRatio: `${width} / ${height}`,
            maxWidth: `${mapMaxWidth}px`,
          }}
        >
          {payload.map.blocked.map((point) => (
            <span
              aria-hidden="true"
              className="gf-trace__map-blocked"
              key={`${point.x}:${point.y}`}
              style={position(point, width, height)}
            />
          ))}
          {spatialFrame ? (
            <>
              <span
                className="gf-trace__map-player"
                style={position(spatialFrame.player, width, height)}
                title={`玩家 (${spatialFrame.player.x}, ${spatialFrame.player.y})`}
              >
                玩家
              </span>
              {spatialFrame.entities.map((entity) => (
                <span
                  className="gf-trace__map-entity"
                  data-entity-kind={entity.kind}
                  key={entity.id}
                  style={position(entity, width, height)}
                  title={`${entity.id} (${entity.x}, ${entity.y})`}
                >
                  {entity.label ?? entity.id}
                </span>
              ))}
            </>
          ) : (
            <span className="gf-trace__map-unavailable">当前帧没有 spatial_2d 状态</span>
          )}
        </div>
        <p className="gf-trace__map-note">
          这是独立展示载荷 aureus-spatial-2d@1 fixture，不属于 playtest-trace@1；下方仍保留真实动作记录。
        </p>
      </section>
      <GenericTraceRenderer currentIndex={currentIndex} onSeek={onSeek} trace={trace} />
    </div>
  );
}
