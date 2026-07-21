export interface TraceFindingLink {
  findingId: string;
  href: string;
  revision: number;
}

export type TraceMarkerKind = "completion" | "failure" | "step_limit" | "stuck" | "loop";

export interface TraceMarker {
  detail: string;
  findings: readonly TraceFindingLink[];
  frameIndex: number | null;
  kind: TraceMarkerKind;
  stateHash: string;
}

export interface TraceFrame {
  action: unknown;
  frameId: string;
  lastActionResult: string;
  stateHash: string;
  tick: number;
}

export interface TracePlayback {
  environmentContractVersion: string;
  finalStateHash: string;
  frames: readonly TraceFrame[];
  initialStateHash: string;
  markers: readonly TraceMarker[];
  rawPayload: unknown;
  traceId: string;
  tracePayloadSchemaId: string;
}

export interface PlaytestEpisodeTraceSelection {
  episodeId: string;
  traceId: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStateHash(value: unknown): value is string {
  return typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value);
}

const markerKinds = new Set<TraceMarkerKind>(["completion", "failure", "step_limit", "stuck", "loop"]);

/** Adapt one real PlaytestEpisodeTraceV1 without inventing unavailable observations/events. */
export function adaptPlaytestEpisodeTrace(
  payload: unknown,
  selection: PlaytestEpisodeTraceSelection,
): TracePlayback | null {
  if (
    !isRecord(payload) ||
    payload.playtest_trace_schema_version !== "playtest-trace@1" ||
    typeof payload.env_contract_version !== "string" ||
    !Array.isArray(payload.episodes)
  ) {
    return null;
  }
  const episode = payload.episodes.find(
    (candidate) => isRecord(candidate) && candidate.episode_id === selection.episodeId,
  );
  if (
    !isRecord(episode) ||
    !isStateHash(episode.initial_state_hash) ||
    !isStateHash(episode.final_state_hash) ||
    !Array.isArray(episode.action_trace) ||
    !Array.isArray(episode.markers)
  ) {
    return null;
  }

  const frames: TraceFrame[] = [];
  for (const [index, value] of episode.action_trace.entries()) {
    if (
      !isRecord(value) ||
      !Object.prototype.hasOwnProperty.call(value, "action") ||
      typeof value.last_action_result !== "string" ||
      !Number.isInteger(value.tick) ||
      (value.tick as number) < 0 ||
      !isStateHash(value.state_hash)
    ) {
      return null;
    }
    frames.push({
      action: value.action,
      frameId: `${selection.episodeId}:step:${index}`,
      lastActionResult: value.last_action_result,
      stateHash: value.state_hash,
      tick: value.tick as number,
    });
  }

  const markers: TraceMarker[] = [];
  for (const value of episode.markers) {
    if (
      !isRecord(value) ||
      typeof value.kind !== "string" ||
      !markerKinds.has(value.kind as TraceMarkerKind) ||
      !isStateHash(value.state_hash) ||
      typeof value.detail !== "string" ||
      !(
        value.step_index === null ||
        (Number.isInteger(value.step_index) && (value.step_index as number) >= 0)
      )
    ) {
      return null;
    }
    const frameIndex = value.step_index as number | null;
    if (
      frameIndex !== null &&
      (frameIndex >= frames.length || frames[frameIndex].stateHash !== value.state_hash)
    ) {
      return null;
    }
    markers.push({
      detail: value.detail,
      findings: [],
      frameIndex,
      kind: value.kind as TraceMarkerKind,
      stateHash: value.state_hash,
    });
  }

  return {
    environmentContractVersion: payload.env_contract_version,
    finalStateHash: episode.final_state_hash,
    frames,
    initialStateHash: episode.initial_state_hash,
    markers,
    rawPayload: payload,
    traceId: selection.traceId,
    tracePayloadSchemaId: "playtest-trace@1",
  };
}
