import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  TracePlayer,
  adaptPlaytestEpisodeTrace,
  createTracePlayerState,
  tracePlayerReducer,
  type TracePlayback,
} from "./TracePlayer";

const hash = (digit: string) => `sha256:${digit.repeat(64)}`;

const realTracePayload = {
  playtest_trace_schema_version: "playtest-trace@1",
  config_artifact_id: "artifact:config-exact-payload",
  constraint_snapshot_artifact_id: "artifact:constraints",
  task_suite_artifact_id: "artifact:suite",
  environment_profile: { profile_id: "environment:aureus", version: 1 },
  planner_policy: { profile_id: "planner:layered", version: 1 },
  env_contract_version: "env@1",
  interaction_mode: "autonomous",
  seed: 7,
  requested_max_steps_per_episode: 20,
  planner_memory_mode: "off",
  execution_envelope: { total_action_count: 6 },
  episodes: [
    {
      episode_id: "episode:quest-outpost",
      scenario_spec_artifact_id: "artifact:scenario-outpost",
      seed: 11,
      initial_state_hash: hash("0"),
      final_state_hash: hash("3"),
      action_trace: [
        {
          action: { kind: "navigate_to", target: "npc:lincheng" },
          last_action_result: "ok",
          tick: 4,
          state_hash: hash("1"),
        },
        {
          action: { kind: "interact", target: "npc:lincheng" },
          last_action_result: "blocked",
          tick: 5,
          state_hash: hash("2"),
        },
        {
          action: { kind: "interact", target: "npc:lincheng" },
          last_action_result: "blocked",
          tick: 6,
          state_hash: hash("1"),
        },
        {
          action: { kind: "wait", ticks: 1 },
          last_action_result: "blocked",
          tick: 7,
          state_hash: hash("3"),
        },
        {
          action: { kind: "wait", ticks: 1 },
          last_action_result: "blocked",
          tick: 8,
          state_hash: hash("3"),
        },
        {
          action: { kind: "wait", ticks: 1 },
          last_action_result: "blocked",
          tick: 9,
          state_hash: hash("3"),
        },
      ],
      markers: [
        {
          kind: "stuck",
          step_index: 5,
          state_hash: hash("3"),
          detail: "连续动作没有取得进展",
        },
        {
          kind: "loop",
          step_index: 2,
          state_hash: hash("1"),
          detail: "权威状态在中间步骤后重复",
        },
        {
          kind: "failure",
          step_index: 5,
          state_hash: hash("3"),
          detail: "agent_stopped",
        },
      ],
    },
  ],
};

const adaptedTrace = adaptPlaytestEpisodeTrace(realTracePayload, {
  episodeId: "episode:quest-outpost",
  traceId: "artifact:playtest-trace",
});
if (adaptedTrace === null) throw new Error("real PlaytestTraceV1 fixture must adapt");

const trace: TracePlayback = {
  ...adaptedTrace,
  markers: adaptedTrace.markers.map((marker) =>
    marker.kind === "loop"
      ? {
          ...marker,
          findings: [
            {
              findingId: "finding:quest-loop",
              revision: 3,
              href: "/findings/finding%3Aquest-loop?revision=3",
            },
          ],
        }
      : marker,
  ),
};

describe("adaptPlaytestEpisodeTrace", () => {
  it("maps only fields actually provided by PlaytestActionRecordV1", () => {
    expect(adaptedTrace).toMatchObject({
      environmentContractVersion: "env@1",
      initialStateHash: hash("0"),
      finalStateHash: hash("3"),
      tracePayloadSchemaId: "playtest-trace@1",
    });
    expect(adaptedTrace.frames[0]).toEqual({
      action: { kind: "navigate_to", target: "npc:lincheng" },
      frameId: "episode:quest-outpost:step:0",
      lastActionResult: "ok",
      stateHash: hash("1"),
      tick: 4,
    });
    expect(adaptedTrace.frames[0]).not.toHaveProperty("state");
    expect(adaptedTrace.frames[0]).not.toHaveProperty("events");
  });

  it("returns null for an unknown episode or malformed action record", () => {
    expect(
      adaptPlaytestEpisodeTrace(realTracePayload, {
        episodeId: "episode:missing",
        traceId: "artifact:trace",
      }),
    ).toBeNull();
    expect(
      adaptPlaytestEpisodeTrace(
        {
          ...realTracePayload,
          episodes: [
            {
              ...realTracePayload.episodes[0],
              action_trace: [{ action: {}, tick: 0, state_hash: hash("0") }],
            },
          ],
        },
        { episodeId: "episode:quest-outpost", traceId: "artifact:trace" },
      ),
    ).toBeNull();
  });
});

describe("tracePlayerReducer", () => {
  it("plays, pauses, steps, seeks and stops at the terminal frame", () => {
    let state = createTracePlayerState(3);
    expect(state).toEqual({ currentIndex: 0, isPlaying: false, speed: 1 });

    state = tracePlayerReducer(state, { type: "play", frameCount: 3 });
    expect(state.isPlaying).toBe(true);
    state = tracePlayerReducer(state, { type: "tick", frameCount: 3 });
    expect(state.currentIndex).toBe(1);
    state = tracePlayerReducer(state, { type: "set-speed", speed: 2 });
    expect(state.speed).toBe(2);
    state = tracePlayerReducer(state, { type: "tick", frameCount: 3 });
    expect(state).toEqual({ currentIndex: 2, isPlaying: false, speed: 2 });
    state = tracePlayerReducer(state, { type: "step-backward", frameCount: 3 });
    expect(state.currentIndex).toBe(1);
    state = tracePlayerReducer(state, { type: "seek", index: 99, frameCount: 3 });
    expect(state.currentIndex).toBe(2);
    state = tracePlayerReducer(state, { type: "pause" });
    expect(state.isPlaying).toBe(false);
  });

  it("resets safely for an empty or replaced trace", () => {
    const state = tracePlayerReducer(
      { currentIndex: 8, isPlaying: true, speed: 0.5 },
      { type: "reset", frameCount: 0 },
    );
    expect(state).toEqual({ currentIndex: 0, isPlaying: false, speed: 0.5 });
  });
});

describe("TracePlayer", () => {
  it("shows only authoritative action records and names unavailable state/event data", () => {
    render(<TracePlayer trace={trace} />);

    const controls = screen.getByLabelText("轨迹播放控制");
    expect(within(controls).getByText("Tick 4")).toBeInTheDocument();
    expect(within(controls).getByText(trace.frames[0].stateHash)).toBeInTheDocument();
    expect(screen.getAllByText(/navigate_to/)).not.toHaveLength(0);
    expect(screen.getByLabelText("动作结果")).toHaveTextContent("ok");
    expect(screen.getByLabelText("状态")).toHaveTextContent("此契约未提供");
    expect(screen.getByLabelText("事件")).toHaveTextContent("此契约未提供");
    expect(screen.getByRole("heading", { level: 3, name: "动作 JSON" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "动作结果" })).toBeInTheDocument();
    expect(screen.getByText("循环")).toBeInTheDocument();
    expect(screen.getByText("卡死")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /finding:quest-loop.*r3/ })).toHaveAttribute(
      "href",
      "/findings/finding%3Aquest-loop?revision=3",
    );
  });

  it("keeps raw JSON collapsed, lazy and keyboard-scrollable", async () => {
    const user = userEvent.setup();
    render(<TracePlayer trace={trace} />);

    expect(screen.queryByText(/artifact:config-exact-payload/)).not.toBeInTheDocument();
    await user.click(screen.getByText("完整 PlaytestTraceV1 原始 JSON"));
    expect(screen.getByText(/artifact:config-exact-payload/)).toBeInTheDocument();
    expect(screen.getByLabelText("完整轨迹原始 JSON")).toHaveAttribute("tabindex", "0");
    expect(screen.getByLabelText("动作 JSON 滚动区")).toHaveAttribute("tabindex", "0");
  });

  it("uses aria-label plus hover/focus tooltip data for every icon-only transport button", () => {
    render(<TracePlayer trace={trace} />);

    for (const label of ["回到开头", "后退一步", "播放", "前进一步"]) {
      const button = screen.getByRole("button", { name: label });
      expect(button).toHaveAttribute("data-tooltip");
      expect(button).not.toHaveAttribute("title");
    }
  });

  it("supports button and keyboard playback controls without a per-tick live region", async () => {
    const user = userEvent.setup();
    render(<TracePlayer trace={trace} />);
    const controls = screen.getByLabelText("轨迹播放控制");

    await user.click(screen.getByRole("button", { name: "前进一步" }));
    expect(within(controls).getByText("Tick 5")).toBeInTheDocument();

    const player = screen.getByRole("region", { name: "Playtest 轨迹播放器" });
    fireEvent.keyDown(player, { key: "ArrowRight" });
    expect(within(controls).getByText("Tick 6")).toBeInTheDocument();
    fireEvent.keyDown(player, { key: "Home" });
    expect(within(controls).getByText("Tick 4")).toBeInTheDocument();
    fireEvent.keyDown(player, { key: "End" });
    expect(within(controls).getByText("Tick 9")).toBeInTheDocument();
    fireEvent.keyDown(player, { key: " " });
    expect(screen.getByRole("button", { name: "暂停" })).toBeInTheDocument();

    await user.selectOptions(screen.getByRole("combobox", { name: "播放速度" }), "2");
    expect(screen.getByRole("combobox", { name: "播放速度" })).toHaveValue("2");
    expect(within(player).getByText("6 / 6")).toBeInTheDocument();
    expect(controls).not.toHaveAttribute("aria-live");
  });

  it("advances on the playback clock and pauses at the terminal frame", () => {
    vi.useFakeTimers();
    try {
      render(<TracePlayer trace={trace} tickDurationMs={100} />);
      const controls = screen.getByLabelText("轨迹播放控制");

      fireEvent.click(screen.getByRole("button", { name: "播放" }));
      act(() => vi.advanceTimersByTime(100));
      expect(within(controls).getByText("Tick 5")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "暂停" })).toBeInTheDocument();

      act(() => vi.advanceTimersByTime(100));
      expect(within(controls).getByText("Tick 6")).toBeInTheDocument();
      for (let index = 0; index < 3; index += 1) {
        act(() => vi.advanceTimersByTime(100));
      }
      expect(within(controls).getByText("Tick 9")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "播放" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("bounds a long timeline, loads explicit segments and keeps the current frame visible", async () => {
    const user = userEvent.setup();
    const longTrace: TracePlayback = {
      ...trace,
      traceId: "artifact:long-trace",
      markers: [],
      frames: Array.from({ length: 1_000 }, (_, index) => ({
        action: { kind: "wait", ticks: 1 },
        frameId: `episode:long:step:${index}`,
        lastActionResult: "ok",
        stateHash: hash(String(index % 10)),
        tick: index,
      })),
    };
    render(<TracePlayer trace={longTrace} />);
    const timeline = screen.getByRole("list", { name: "有界动作时间轴" });
    expect(within(timeline).getAllByRole("listitem")).toHaveLength(100);

    const player = screen.getByRole("region", { name: "Playtest 轨迹播放器" });
    fireEvent.keyDown(player, { key: "End" });
    expect(screen.getByRole("button", { name: /第 1000 帧.*Tick 999/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(within(timeline).getAllByRole("button")).toHaveLength(101);

    await user.click(screen.getByRole("button", { name: "再加载 100 帧" }));
    expect(within(timeline).getAllByRole("listitem")).toHaveLength(201);
  });

  it("keeps authoritative markers and their Finding links visible beyond the first timeline batch", () => {
    const frames = Array.from({ length: 150 }, (_, index) => ({
      action: { kind: "wait", ticks: 1 },
      frameId: `episode:marked:step:${index}`,
      lastActionResult: index === 149 ? "agent_stopped" : "ok",
      stateHash: hash(String(index % 10)),
      tick: index,
    }));
    const markedTrace: TracePlayback = {
      ...trace,
      frames,
      markers: [
        {
          detail: "agent_stopped",
          findings: [
            {
              findingId: "finding:terminal-failure",
              href: "/findings/finding%3Aterminal-failure/revisions/4",
              revision: 4,
            },
          ],
          frameIndex: 149,
          kind: "failure",
          stateHash: frames[149].stateHash,
        },
      ],
      traceId: "artifact:marked-long-trace",
    };

    render(<TracePlayer trace={markedTrace} />);

    const timeline = screen.getByRole("list", { name: "有界动作时间轴" });
    expect(within(timeline).getAllByRole("button")).toHaveLength(101);
    expect(screen.getByRole("button", { name: /第 150 帧.*Tick 149/ })).toBeVisible();
    expect(screen.getByRole("link", { name: /finding:terminal-failure.*r4/ })).toHaveAttribute(
      "href",
      "/findings/finding%3Aterminal-failure/revisions/4",
    );
  });

  it("wraps 512+ identifiers and a 4096-character action result inside bounded columns", () => {
    const longResult = "x".repeat(4096);
    const longTrace: TracePlayback = {
      ...trace,
      traceId: `artifact:${"i".repeat(520)}`,
      markers: [],
      frames: [{ ...trace.frames[0], lastActionResult: longResult }],
    };
    const { container } = render(<TracePlayer trace={longTrace} />);

    expect(screen.getByText(longTrace.traceId)).toBeInTheDocument();
    expect(screen.getAllByText(longResult)).not.toHaveLength(0);
    expect(container.querySelector(".gf-trace__timeline-result")).toHaveTextContent(longResult);
    expect(container.querySelector(".gf-trace__timeline-result")).toHaveClass("gf-trace__timeline-result");
  });

  it("renders an independently validated Aureus fixture without attributing it to the trace contract", () => {
    const aureusPayload = {
      renderer_payload_schema_id: "aureus-spatial-2d@1",
      map: { width: 8, height: 6, blocked: [{ x: 2, y: 2 }] },
      frames: [
        {
          frame_id: "episode:quest-outpost:step:0",
          player: { x: 1, y: 1 },
          entities: [{ id: "npc:lincheng", kind: "npc", x: 4, y: 3, label: "林澄" }],
        },
      ],
    };
    const rendererRequest = {
      rendererId: "aureus.spatial-2d",
      rendererVersion: 1,
      environmentContractVersion: "env@1",
      tracePayloadSchemaId: "playtest-trace@1",
      capabilities: ["spatial_2d"],
    } as const;
    render(<TracePlayer trace={trace} rendererPayload={aureusPayload} rendererRequest={rendererRequest} />);

    const map = screen.getByRole("img", { name: /Aureus 独立 2D 展示.*8 × 6/ });
    expect(map).toHaveStyle({ aspectRatio: "8 / 6", maxWidth: "560px" });
    expect(screen.getByText("林澄")).toBeInTheDocument();
    expect(screen.getByText(/独立展示载荷.*不属于 playtest-trace@1/)).toBeInTheDocument();
  });

  it.each([
    ["unknown.renderer", "env@1", "playtest-trace@1", ["spatial_2d"]],
    ["aureus.legacy-2d", "env@1", "playtest-trace@1", ["spatial_2d"]],
    ["aureus.spatial-2d", "other-env@9", "playtest-trace@1", ["spatial_2d"]],
    ["aureus.spatial-2d", "env@1", "other-trace@1", ["spatial_2d"]],
    ["aureus.spatial-2d", "env@1", "playtest-trace@1", []],
  ])(
    "keeps unknown, disabled or incompatible renderer %s inspectable",
    (rendererId, environmentContractVersion, tracePayloadSchemaId, capabilities) => {
      render(
        <TracePlayer
          trace={trace}
          rendererPayload={{ not: "a trusted spatial boundary" }}
          rendererRequest={{
            rendererId,
            rendererVersion: 1,
            environmentContractVersion,
            tracePayloadSchemaId,
            capabilities,
          }}
        />,
      );

      expect(screen.getByText(/已切换到通用检查视图/)).toBeInTheDocument();
      expect(screen.getAllByText(/navigate_to/)).not.toHaveLength(0);
      expect(screen.getByLabelText("状态")).toHaveTextContent("此契约未提供");
      expect(screen.getByLabelText("事件")).toHaveTextContent("此契约未提供");
    },
  );
});
