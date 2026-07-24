import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/query-client";
import type { components } from "../../api/generated/openapi";
import type { PlaytestApi, PlaytestRunRequest, TaskSuiteArtifactView } from "./api";

const authorityMocks = vi.hoisted(() => ({
  bindFindingLinks: vi.fn(),
  bindTerminal: vi.fn(),
}));

vi.mock("./authority", async (importOriginal) => {
  const original = await importOriginal<typeof import("./authority")>();
  return {
    ...original,
    bindPlaytestFindingLinks: authorityMocks.bindFindingLinks,
    bindPlaytestTerminalAuthority: authorityMocks.bindTerminal,
  };
});

import { PlaytestTerminalPanel } from "./PlaytestTerminalPanel";

type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type RunFindingLink = components["schemas"]["RunFindingLinkViewV1"];
type RunView = components["schemas"]["RunViewV1"];

const RUN_ID = "run:playtest:terminal";
const TRACE_ID = "artifact:trace:terminal";
const RESULT_ID = "artifact:run-result:terminal";
const STATE_0 = `sha256:${"0".repeat(64)}`;
const STATE_1 = `sha256:${"1".repeat(64)}`;

const run: RunView = {
  attempt_no: 1,
  events_url: `/api/v1/runs/${RUN_ID}/events`,
  failure_artifact_id: null,
  result_artifact_id: RESULT_ID,
  revision: 4,
  run_id: RUN_ID,
  status: "succeeded",
  status_url: `/api/v1/runs/${RUN_ID}`,
  terminal_cassette_artifact_id: null,
  view_schema_version: "run-view@1",
};

function artifact(artifactId: string, kind: "run_result" | "playtest_trace"): ArtifactPayloadView {
  return {
    artifact: {
      artifact_id: artifactId,
      created_at: "2026-07-20T03:00:00Z",
      domain_scope: { domain_ids: ["quests"] },
      kind,
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: [],
      payload_hash: "a".repeat(64),
      payload_schema_id: kind === "run_result" ? "run-result@1" : "playtest-trace@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: { env_contract_version: "agent-env@2", ir_snapshot_id: "snapshot:terminal" },
    },
    payload: {},
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

const manifest = artifact(RESULT_ID, "run_result");
const trace = artifact(TRACE_ID, "playtest_trace");
const rawPayload = {
  env_contract_version: "agent-env@2",
  episodes: [
    {
      action_trace: [
        { action: { kind: "observe" }, last_action_result: "observed", state_hash: STATE_1, tick: 1 },
      ],
      episode_id: "episode:bridge",
      final_state_hash: STATE_1,
      initial_state_hash: STATE_0,
      markers: [{ detail: "oracle satisfied", kind: "completion", state_hash: STATE_1, step_index: 0 }],
    },
    {
      action_trace: [],
      episode_id: "episode:signal",
      final_state_hash: STATE_0,
      initial_state_hash: STATE_0,
      markers: [{ detail: "agent stopped", kind: "failure", state_hash: STATE_0, step_index: null }],
    },
  ],
  playtest_trace_schema_version: "playtest-trace@1",
};

const findingLink: RunFindingLink = {
  attempt_no: 1,
  evidence_artifact_id: TRACE_ID,
  finding: {
    created_at: "2026-07-20T03:01:00Z",
    finding_id: "finding:signal-incomplete",
    payload: {
      defect_class: "playtest_incomplete",
      entities: [],
      evidence: { episode_id: "episode:signal" },
      message: "Signal episode did not satisfy its oracle.",
      minimal_repro: { episode_id: "episode:signal" },
      oracle_type: "deterministic",
      payload_schema_version: "finding-payload@1",
      producer_id: "playtest.completion_oracle",
      producer_run_id: RUN_ID,
      relations: [],
      severity: "major",
      snapshot_id: "snapshot:terminal",
      source: "playtest",
      status: "confirmed",
    },
    revision: 2,
    revision_schema_version: "finding-revision@1",
    supersedes_revision: 1,
  },
  finding_digest: "b".repeat(64),
  ordinal: 1,
  run_id: RUN_ID,
  view_schema_version: "run-finding-link-view@1",
};

const request = {} as PlaytestRunRequest;
const suite = {
  artifact: { artifact_id: "artifact:suite:terminal", payload_hash: "c".repeat(64) },
} as TaskSuiteArtifactView;

function api(): PlaytestApi {
  return {
    createEventStream: vi.fn(),
    deriveTaskSuite: vi.fn(),
    getArtifact: vi.fn(async () => manifest),
    getConstraint: vi.fn(),
    getPlaytestResult: vi.fn(async () => trace),
    getRun: vi.fn(async () => run),
    getSpec: vi.fn(),
    getTaskSuite: vi.fn(),
    getTaskSuiteDerivationBinding: vi.fn(),
    listConfigExports: vi.fn(),
    listExecutionProfiles: vi.fn(),
    listRunCommands: vi.fn(),
    listRunFindingLinks: vi.fn(async () => ({
      expires_at: "2026-07-20T04:00:00Z",
      items: [findingLink],
      next_cursor: null,
      page_schema_version: "page@1" as const,
      read_snapshot_id: "read:terminal-findings",
    })),
    listReplaySourceRuns: vi.fn(),
    listTaskSuites: vi.fn(),
    resolveExecutionOption: vi.fn(),
    runPlaytest: vi.fn(),
  };
}

function successAuthority() {
  return {
    allEpisodesCompleted: false,
    attemptNo: 1,
    completedEpisodeCount: 1,
    findingCount: 1,
    kind: "succeeded",
    manifest,
    resultArtifact: trace,
    requestCandidateStatus: "visible_bindings_match",
    run,
    runStatus: "succeeded",
    selection: {},
    trace: {
      artifact: trace,
      episodes: [
        { completed: true, episodeId: "episode:bridge", terminalReason: "completion_oracle_satisfied" },
        { completed: false, episodeId: "episode:signal", terminalReason: "agent_stopped" },
      ],
      rawPayload,
    },
  };
}

describe("Playtest terminal panel", () => {
  it("separates Run success from episode completion and renders generic trace plus exact Finding links", async () => {
    const user = userEvent.setup();
    const playtestApi = api();
    authorityMocks.bindTerminal.mockReturnValue(successAuthority());
    authorityMocks.bindFindingLinks.mockReturnValue([findingLink]);

    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <PlaytestTerminalPanel api={playtestApi} request={request} runId={RUN_ID} suite={suite} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Run 已完成，任务未全部通过" })).toBeVisible();
    expect(screen.getByText("1 / 2 episodes completed")).toBeVisible();
    expect(screen.getByRole("region", { name: "Playtest 轨迹播放器" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "有界动作时间轴" })).toBeVisible();
    expect(screen.queryByText("Aureus 2D")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /episode:signal/ }));
    expect(screen.getAllByText("agent stopped")[0]).toBeVisible();
    expect(screen.getByRole("link", { name: "查看 exact Finding 修订" })).toHaveAttribute(
      "href",
      "/findings/finding%3Asignal-incomplete/revisions/2",
    );

    await waitFor(() => expect(playtestApi.getArtifact).toHaveBeenCalledWith(RESULT_ID));
    expect(playtestApi.getPlaytestResult).toHaveBeenCalledWith(RUN_ID);
    expect(playtestApi.listRunFindingLinks).toHaveBeenCalledWith(RUN_ID, null);
    expect(authorityMocks.bindTerminal).toHaveBeenCalledWith({
      expectedRunId: RUN_ID,
      manifest,
      requestCandidate: request,
      result: trace,
      run,
      suite,
    });
    expect(authorityMocks.bindFindingLinks).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "succeeded" }),
      [findingLink],
    );
  });

  it("uses the verified terminal result when the browser-local request candidate is absent", async () => {
    const playtestApi = api();
    const terminal = successAuthority();
    terminal.requestCandidateStatus = "not_provided";
    authorityMocks.bindTerminal.mockReturnValue(terminal);
    authorityMocks.bindFindingLinks.mockReturnValue([findingLink]);

    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <PlaytestTerminalPanel api={playtestApi} request={null} runId={RUN_ID} suite={suite} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Run 已完成，任务未全部通过" })).toBeVisible();
    expect(screen.getByText("未提供；终态由服务端结果闭合")).toBeVisible();
    expect(authorityMocks.bindTerminal).toHaveBeenCalledWith(
      expect.objectContaining({ requestCandidate: null }),
    );
  });

  it("refetches terminal authority when the exact suite owner changes", async () => {
    const playtestApi = api();
    authorityMocks.bindTerminal.mockReturnValue(successAuthority());
    authorityMocks.bindFindingLinks.mockReturnValue([findingLink]);
    const client = createQueryClient();
    const otherSuite = {
      artifact: { artifact_id: "artifact:suite:other", payload_hash: "d".repeat(64) },
    } as TaskSuiteArtifactView;

    const view = (selectedSuite: TaskSuiteArtifactView) => (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <PlaytestTerminalPanel api={playtestApi} request={null} runId={RUN_ID} suite={selectedSuite} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const rendered = render(view(suite));
    await screen.findByRole("heading", { name: "Run 已完成，任务未全部通过" });
    const firstCallCount = authorityMocks.bindTerminal.mock.calls.length;

    rendered.rerender(view(otherSuite));

    await waitFor(() =>
      expect(authorityMocks.bindTerminal.mock.calls.length).toBeGreaterThan(firstCallCount),
    );
    expect(authorityMocks.bindTerminal).toHaveBeenLastCalledWith(
      expect.objectContaining({ suite: otherSuite }),
    );
  });
});
