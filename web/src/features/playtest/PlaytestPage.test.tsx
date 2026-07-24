import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { RunCommandClient } from "../../api/commands";
import { ReauthenticationRequiredError } from "../../api/csrf";
import { createQueryClient } from "../../api/query-client";
import type { components } from "../../api/generated/openapi";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import type {
  ArtifactPayloadView,
  ExecutionOptionView,
  PlaytestApi,
  PlaytestEventStreamCallbacks,
  RunAccepted,
  RunView,
  TaskSuiteArtifactView,
  TaskSuiteDerivationBindingView,
} from "./api";
import { collectRunCommands, PlaytestPage } from "./PlaytestPage";

type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];

const SOURCE_RUN_ID = "run:generation:playtest-source";
const PREVIEW_ID = "artifact:preview:playtest";
const CONFIG_ID = "artifact:config:playtest";
const CONSTRAINT_ID = "artifact:constraint:playtest";
const SUITE_ID = "artifact:suite:playtest";
const SUITE_2_ID = "artifact:suite:playtest:second";
const DERIVE_RUN_ID = "run:task-suite:derive";
const DERIVE_RESULT_ID = "artifact:run-result:task-suite";
const PLAYTEST_RUN_ID = "run:playtest:live";
const SNAPSHOT_ID = "snapshot:playtest";
const CONSTRAINT_SNAPSHOT_ID = "constraint-snapshot:playtest";
const ENV_PROFILE = { profile_id: "builtin.environment", version: 1 } as const;
const DERIVATION_PROFILE = { profile_id: "builtin.task_suite_derivation", version: 2 } as const;
const PLANNER_PROFILE = { profile_id: "builtin.playtest_planner", version: 2 } as const;
const ORACLE_REF = { digest: "a".repeat(64), registry_version: 1 } as const;

function summary(
  artifactId: string,
  kind: ArtifactSummary["kind"],
  payloadSchemaId: string,
  versionTuple: ArtifactSummary["version_tuple"],
  parents: string[] = [],
): ArtifactSummary {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-20T03:00:00Z",
    domain_scope: { domain_ids: ["domain:narrative"] },
    kind,
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [...parents].sort(),
    payload_hash: "b".repeat(64),
    payload_schema_id: payloadSchemaId,
    summary_schema_version: "artifact-summary@1",
    version_tuple: versionTuple,
  };
}

const episodes: TaskSuiteArtifactView["task_suite"]["episodes"] = [
  {
    completion_oracle: {
      oracle_id: "quest-completed",
      params: { quest_id: "quest:bridge" },
      params_schema_id: "aureus-quest-oracle-params@1",
      version: 1,
    },
    domain_scope: { domain_ids: ["domain:narrative"] },
    episode_id: "episode:bridge",
    reset_binding: {
      payload: { quest_id: "quest:bridge" },
      payload_hash: "c".repeat(64),
      reset_schema_id: "aureus-reset@1",
    },
    scenario_spec_artifact_id: "artifact:scenario:bridge",
    step_budget: 7,
  },
  {
    completion_oracle: {
      oracle_id: "quest-completed",
      params: { quest_id: "quest:signal" },
      params_schema_id: "aureus-quest-oracle-params@1",
      version: 1,
    },
    domain_scope: { domain_ids: ["domain:narrative"] },
    episode_id: "episode:signal",
    reset_binding: {
      payload: { quest_id: "quest:signal" },
      payload_hash: "d".repeat(64),
      reset_schema_id: "aureus-reset@1",
    },
    scenario_spec_artifact_id: "artifact:scenario:signal",
    step_budget: 11,
  },
];

const suite: TaskSuiteArtifactView = {
  artifact: summary(
    SUITE_ID,
    "task_suite",
    "task-suite@1",
    {
      constraint_snapshot_id: CONSTRAINT_SNAPSHOT_ID,
      env_contract_version: "aureus-env@1",
      ir_snapshot_id: SNAPSHOT_ID,
      tool_version: "task-suite@1",
    },
    [PREVIEW_ID, CONFIG_ID, CONSTRAINT_ID, ...episodes.map((item) => item.scenario_spec_artifact_id)],
  ),
  task_suite: {
    completion_oracle_registry_ref: ORACLE_REF,
    config_export_artifact_id: CONFIG_ID,
    constraint_snapshot_artifact_id: CONSTRAINT_ID,
    env_contract_version: "aureus-env@1",
    environment_profile: ENV_PROFILE,
    episodes,
    source_preview_artifact_id: PREVIEW_ID,
    suite_profile: DERIVATION_PROFILE,
    task_suite_schema_version: "task-suite@1",
  },
  view_schema_version: "task-suite-artifact-view@1",
};

const secondSuite: TaskSuiteArtifactView = {
  ...suite,
  artifact: { ...suite.artifact, artifact_id: SUITE_2_ID },
};

const multiDomainSuite: TaskSuiteArtifactView = {
  ...suite,
  artifact: {
    ...suite.artifact,
    domain_scope: { domain_ids: ["domain:economy", "domain:narrative"] },
  },
  task_suite: {
    ...suite.task_suite,
    episodes: suite.task_suite.episodes.map((episode, index) =>
      index === 0 ? { ...episode, domain_scope: { domain_ids: ["domain:economy"] } } : episode,
    ),
  },
};

function profile(
  ref: { profile_id: string; version: number },
  kind: ExecutionProfile["profile_kind"],
  runKind: string,
): ExecutionProfile {
  return {
    compatible_run_kinds: [{ kind: runKind, version: 1 }],
    display_name: ref.profile_id,
    domain_scope: { domain_ids: ["domain:narrative"] },
    env_contract_version: kind === "environment" ? "aureus-env@1" : null,
    input_schema_ids: [],
    output_schema_ids: [],
    profile: ref,
    profile_kind: kind,
    profile_payload_hash: "e".repeat(64),
    required_capabilities: [],
    status: "active",
    stochastic: kind === "playtest_planner",
    target_environment_profile: kind === "task_suite_derivation" ? ENV_PROFILE : null,
  };
}

const derivationProfile = profile(DERIVATION_PROFILE, "task_suite_derivation", "task_suite.derive");
const plannerProfile = profile(PLANNER_PROFILE, "playtest_planner", "playtest.run");

const derivationBinding: TaskSuiteDerivationBindingView = {
  binding_schema_version: "task-suite-derivation-binding@1",
  completion_oracle_registry_ref: ORACLE_REF,
  derivation_profile: DERIVATION_PROFILE,
  max_scenarios: 1024,
  max_total_prepared_artifact_bytes: 268_435_456,
  profile_payload_hash: derivationProfile.profile_payload_hash,
  run_kind: { kind: "task_suite.derive", version: 1 },
  target_environment_profile: ENV_PROFILE,
};

const executionPlan: components["schemas"]["ExecutionVersionPlanV1"] = {
  agent_graph_version: "playtest@1",
  model_catalog_digest: "1".repeat(64),
  model_catalog_version: 1,
  nodes: [
    {
      agent_node_id: "playtest.planner",
      allowed_model_snapshots: ["openai/gpt-5.6-sol/m4@1"],
      prompt_version: "playtest@1",
      tool_version: "playtest.planner@1",
    },
  ],
  plan_digest: "2".repeat(64),
  plan_schema_version: "execution-version-plan@1",
  routing_policy_digest: "3".repeat(64),
  routing_policy_version: 1,
};

const executionOption: ExecutionOptionView = {
  cassette_artifact_id: null,
  domain_scope: { domain_ids: ["domain:narrative"] },
  execution_version_plan: executionPlan,
  llm_execution_mode: "record",
  option_id: `execution-option:sha256:${"4".repeat(64)}`,
  option_schema_version: "execution-option@1",
  prospective_request_hash: "5".repeat(64),
  resolved_profile_binding_digests: ["6".repeat(64)],
  resolved_request_hash: "7".repeat(64),
  resource_operation_id: "run_playtest_api_v1_playtest_run_post",
  run_kind: { kind: "playtest.run", version: 1 },
  source_run_id: null,
};

function page<T>(items: T[], readSnapshotId: string) {
  return {
    expires_at: "2026-07-20T04:00:00Z",
    items,
    next_cursor: null,
    page_schema_version: "page@1" as const,
    read_snapshot_id: readSnapshotId,
  };
}

function run(runId: string, status: RunView["status"] = "queued"): RunView {
  return {
    attempt_no: status === "queued" ? null : 1,
    events_url: `/api/v1/runs/${runId}/events`,
    failure_artifact_id: null,
    result_artifact_id: null,
    revision: 1,
    run_id: runId,
    status,
    status_url: `/api/v1/runs/${runId}`,
    terminal_cassette_artifact_id: null,
    view_schema_version: "run-view@1",
  };
}

function succeededEvent(runId: string, resultArtifactId: string): RunEvent {
  return {
    attempt_no: 1,
    data: {
      attempt_no: 1,
      data_schema_version: "run-succeeded@1",
      result_artifact_id: resultArtifactId,
    },
    data_schema_version: "run-succeeded@1",
    event_schema_version: "run-event@1",
    event_type: "run.succeeded",
    occurred_at: "2026-07-20T03:10:00Z",
    run_id: runId,
    seq: 2,
    trace_id: null,
  };
}

function progressEvent(runId: string, cursor: string, phaseCode: string): RunEvent {
  return {
    attempt_no: 1,
    data: {
      attempt_no: 1,
      completed_units: 1,
      data_schema_version: "attempt-progress@1",
      phase_code: phaseCode,
      total_units: 2,
    },
    data_schema_version: "attempt-progress@1",
    event_schema_version: "run-event@1",
    event_type: "attempt.progress",
    occurred_at: `2026-07-20T03:10:0${phaseCode === "first" ? "1" : "2"}Z`,
    run_id: runId,
    seq: Number(cursor),
    trace_id: null,
  };
}

function configArtifact(): ArtifactPayloadView {
  return {
    artifact: summary(CONFIG_ID, "config_export", "config-export-package@1", {
      constraint_snapshot_id: CONSTRAINT_SNAPSHOT_ID,
      env_contract_version: "aureus-env@1",
      ir_snapshot_id: SNAPSHOT_ID,
      tool_version: "config-export@1",
    }),
    payload: {
      constraint_snapshot_artifact_id: CONSTRAINT_ID,
      env_contract_version: "aureus-env@1",
      export_profile: { profile_id: "builtin.config_export", version: 1 },
      files: [{ relative_path: "quests.csv" }],
      format_schema_id: "aureus-csv@1",
      package_schema_version: "config-export-package@1",
      source_preview_artifact_id: PREVIEW_ID,
      target_environment_profile: ENV_PROFILE,
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function deriveResultArtifact(primaryArtifactId = SUITE_ID): ArtifactPayloadView {
  return {
    artifact: summary(
      DERIVE_RESULT_ID,
      "run_result",
      "run-result@1",
      {
        constraint_snapshot_id: CONSTRAINT_SNAPSHOT_ID,
        env_contract_version: "aureus-env@1",
        ir_snapshot_id: SNAPSHOT_ID,
        tool_version: "task-suite@1",
      },
      [primaryArtifactId],
    ),
    payload: {
      finding_count: 0,
      outcome_code: "task_suite_derived",
      primary_artifact_id: primaryArtifactId,
      produced_artifact_ids: [primaryArtifactId],
      result_schema_version: "run-result@1",
      run_id: DERIVE_RUN_ID,
      run_kind: { kind: "task_suite.derive", version: 1 },
      summary: {
        finding_count: 0,
        outcome_code: "task_suite_derived",
        primary_artifact_kind: "task_suite",
        produced_artifact_count: 1,
        summary_schema_version: "run-result-summary@1",
      },
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function api(overrides: Partial<PlaytestApi> = {}): PlaytestApi {
  const base: PlaytestApi = {
    createEventStream: vi.fn((_callbacks: PlaytestEventStreamCallbacks & { runId: string }) => ({
      close: vi.fn(),
      restart: vi.fn(async () => undefined),
      start: vi.fn(async () => undefined),
    })),
    deriveTaskSuite: vi.fn(async () => ({
      accepted_schema_version: "run-accepted@1" as const,
      events_url: `/api/v1/runs/${DERIVE_RUN_ID}/events`,
      run_id: DERIVE_RUN_ID,
      status_url: `/api/v1/runs/${DERIVE_RUN_ID}`,
    })),
    getArtifact: vi.fn(async () => configArtifact()),
    getConstraint: vi.fn(
      async (): Promise<Awaited<ReturnType<PlaytestApi["getConstraint"]>>> => ({
        artifact: summary(CONSTRAINT_ID, "constraint_snapshot", "constraint-snapshot@1", {
          constraint_snapshot_id: CONSTRAINT_SNAPSHOT_ID,
        }),
        constraints: [],
        dsl_grammar_version: "constraint-dsl@1",
        view_schema_version: "constraint-snapshot-view@1",
      }),
    ),
    getPlaytestResult: vi.fn(),
    getRun: vi.fn(async (runId) => run(runId)),
    getSpec: vi.fn(
      async (): Promise<Awaited<ReturnType<PlaytestApi["getSpec"]>>> => ({
        artifact: summary(PREVIEW_ID, "ir_snapshot", "ir-core@1", { ir_snapshot_id: SNAPSHOT_ID }),
        ref_name: null,
        ref_value: null,
        schema_registry_version: "ir-core@1",
        snapshot_id: SNAPSHOT_ID,
        view_schema_version: "spec-view@1",
      }),
    ),
    getTaskSuite: vi.fn(async () => suite),
    getTaskSuiteDerivationBinding: vi.fn(async () => derivationBinding),
    listExecutionProfiles: vi.fn(async (filters) =>
      page(
        filters.profile_kind === "task_suite_derivation"
          ? [derivationProfile]
          : filters.profile_kind === "playtest_planner"
            ? [plannerProfile]
            : [],
        `read:profiles:${filters.profile_kind ?? "all"}`,
      ),
    ),
    listConfigExports: vi.fn(async () => page([configArtifact().artifact], "read:config-exports")),
    listRunCommands: vi.fn(async () => page([], "read:commands")),
    listRunFindingLinks: vi.fn(async () => page([], "read:findings")),
    listReplaySourceRuns: vi.fn(async () =>
      page(
        [
          {
            attempt_no: 1,
            completedAt: "2026-07-23T03:47:50Z",
            events_url: "/api/v1/runs/run:playtest:source/events",
            failure_artifact_id: null,
            outcomeCode: "playtest_completed",
            result_artifact_id: "artifact:result:playtest-source",
            revision: 3,
            runKind: { kind: "playtest.run", version: 1 },
            run_id: "run:playtest:source",
            status: "succeeded" as const,
            status_url: "/api/v1/runs/run:playtest:source",
            terminal_cassette_artifact_id: "artifact:cassette:playtest-source",
            view_schema_version: "run-view@1" as const,
          },
        ],
        "read:playtest-source-runs",
      ),
    ),
    listTaskSuites: vi.fn(async () => page([suite], "read:suites")),
    resolveExecutionOption: vi.fn(async () => executionOption),
    runPlaytest: vi.fn(async () => ({
      accepted_schema_version: "run-accepted@1" as const,
      events_url: `/api/v1/runs/${PLAYTEST_RUN_ID}/events`,
      run_id: PLAYTEST_RUN_ID,
      status_url: `/api/v1/runs/${PLAYTEST_RUN_ID}`,
    })),
  };
  return { ...base, ...overrides };
}

const commandClient = {
  createCancelIntent: vi.fn(),
  submit: vi.fn(),
} as unknown as RunCommandClient;

function renderPage(playtestApi: PlaytestApi, path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <PlaytestPage api={playtestApi} commandClient={commandClient} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-probe">{`${location.pathname}${location.search}`}</output>;
}

function HistoryBackControl() {
  const navigate = useNavigate();
  return (
    <button onClick={() => navigate(-1)} type="button">
      返回上一条历史
    </button>
  );
}

function RouteTestControls() {
  const [searchParams, setSearchParams] = useSearchParams();

  function update(values: Record<string, string | null>) {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(values)) {
      if (value === null) next.delete(key);
      else next.set(key, value);
    }
    setSearchParams(next);
  }

  return (
    <>
      <button onClick={() => update({ marker: "fresh" })} type="button">
        写入新 query
      </button>
      <button onClick={() => update({ run: null, suite: SUITE_2_ID })} type="button">
        外部切换 suite owner
      </button>
      <button onClick={() => update({ run: "run:playtest:external" })} type="button">
        外部切换 Playtest Run
      </button>
      <button onClick={() => update({ deriveRun: "run:task-suite:external" })} type="button">
        外部切换派生 Run
      </button>
    </>
  );
}

function renderPageWithLocation(playtestApi: PlaytestApi, path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <PlaytestPage api={playtestApi} commandClient={commandClient} />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderPageWithRouteControls(playtestApi: PlaytestApi, path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <PlaytestPage api={playtestApi} commandClient={commandClient} />
        <RouteTestControls />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderPageWithHistory(playtestApi: PlaytestApi, path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={["/before-playtest", path]} initialIndex={1}>
        <PlaytestPage api={playtestApi} commandClient={commandClient} />
        <HistoryBackControl />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function UnmountablePlaytest({ playtestApi }: { playtestApi: PlaytestApi }) {
  const [mounted, setMounted] = useState(true);
  return (
    <>
      {mounted && <PlaytestPage api={playtestApi} commandClient={commandClient} />}
      <button onClick={() => setMounted(false)} type="button">
        卸载 Playtest 页面
      </button>
      <LocationProbe />
    </>
  );
}

function renderUnmountablePage(playtestApi: PlaytestApi, path: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <UnmountablePlaytest playtestApi={playtestApi} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const contextPath = `/playtest?sourceRun=${encodeURIComponent(SOURCE_RUN_ID)}&preview=${encodeURIComponent(PREVIEW_ID)}&config=${encodeURIComponent(CONFIG_ID)}&constraint=${encodeURIComponent(CONSTRAINT_ID)}`;

describe("Playtest page", () => {
  it("lets the ordinary entry select an exact config candidate without copying IDs or leaving the tab", async () => {
    const user = userEvent.setup();
    const listConfigExports = vi.fn(async () =>
      page([configArtifact().artifact], "read:config-exports:entry"),
    );
    const playtestApi = api({
      listConfigExports,
      listTaskSuites: vi.fn(async () => page([], "read:suites:empty-entry")),
    });
    renderPageWithLocation(playtestApi, "/playtest");

    const catalog = await screen.findByRole("region", { name: "选择待试玩候选" });
    expect(await within(catalog).findByText(CONFIG_ID)).toBeInTheDocument();
    expect(await within(catalog).findByText("builtin.environment@1")).toBeVisible();
    await user.click(within(catalog).getByRole("button", { name: `使用配置 ${CONFIG_ID}` }));

    await waitFor(() => {
      const location = screen.getByTestId("location-probe").textContent ?? "";
      expect(location).toContain(`preview=${encodeURIComponent(PREVIEW_ID)}`);
      expect(location).toContain(`config=${encodeURIComponent(CONFIG_ID)}`);
      expect(location).toContain(`constraint=${encodeURIComponent(CONSTRAINT_ID)}`);
      expect(location).toContain("action=derive");
    });
    expect(await screen.findByRole("region", { name: "候选绑定账本" })).toBeVisible();
    expect(await screen.findByRole("button", { name: "派生 exact TaskSuite" })).toBeEnabled();
    expect(listConfigExports).toHaveBeenCalledWith(null);
  });

  it("fails closed when a catalog candidate does not match its immutable ConfigExport payload", async () => {
    const mismatched = configArtifact();
    mismatched.artifact = { ...mismatched.artifact, artifact_id: "artifact:config:other" };
    const playtestApi = api({
      getArtifact: vi.fn(async () => mismatched),
      listConfigExports: vi.fn(async () => page([configArtifact().artifact], "read:config-exports:mismatch")),
      listTaskSuites: vi.fn(async () => page([], "read:suites:empty-mismatch")),
    });
    renderPageWithLocation(playtestApi, "/playtest");

    expect(await screen.findByRole("heading", { name: "无法发现候选配置" })).toBeVisible();
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/playtest");
    expect(screen.queryByRole("button", { name: /使用配置/ })).not.toBeInTheDocument();
  });

  it("fails closed on a cyclic RunCommand recovery cursor", async () => {
    const commandPage = {
      ...page([], "read:commands:cycle"),
      next_cursor: "cursor:commands:cycle",
    };
    const playtestApi = api({ listRunCommands: vi.fn(async () => commandPage) });

    await expect(collectRunCommands(playtestApi, PLAYTEST_RUN_ID)).rejects.toThrow(
      "Run command pagination returned a cursor cycle.",
    );
    expect(playtestApi.listRunCommands).toHaveBeenCalledTimes(2);
  });

  it("revalidates exact navigation context and discovers immutable matching suites", async () => {
    const playtestApi = api();
    renderPage(playtestApi, contextPath);

    expect(await screen.findByRole("heading", { level: 1, name: "自动试玩" })).toBeVisible();
    const context = await screen.findByRole("region", { name: "候选绑定账本" });
    expect(within(context).getByText(PREVIEW_ID)).toBeVisible();
    expect(within(context).getByText(CONFIG_ID)).toBeVisible();
    expect(within(context).getByText(CONSTRAINT_ID)).toBeVisible();
    expect(within(context).getByText("builtin.environment@1")).toBeVisible();
    expect(await screen.findByText(SUITE_ID)).toBeVisible();
    expect(screen.getByText("2 episodes")).toBeVisible();
    await waitFor(() =>
      expect(playtestApi.listTaskSuites).toHaveBeenCalledWith(
        {
          config_artifact_id: CONFIG_ID,
          constraint_artifact_id: CONSTRAINT_ID,
          environment_profile_id: ENV_PROFILE.profile_id,
          environment_profile_version: ENV_PROFILE.version,
          limit: 100,
        },
        null,
      ),
    );
  });

  it("keeps one read snapshot while following opaque TaskSuite pagination", async () => {
    const user = userEvent.setup();
    const playtestApi = api({
      listTaskSuites: vi.fn(async (_filters, cursor) =>
        cursor === null
          ? {
              ...page([suite], "read:suites:paged"),
              next_cursor: "cursor:suites:2",
            }
          : page([secondSuite], "read:suites:paged"),
      ),
    });
    renderPage(playtestApi, contextPath);

    expect(await screen.findByText(SUITE_ID)).toBeVisible();
    expect(screen.queryByText(SUITE_2_ID)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "加载更多 TaskSuite" }));
    expect(await screen.findByText(SUITE_2_ID)).toBeVisible();
    expect(playtestApi.listTaskSuites).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ config_artifact_id: CONFIG_ID, limit: 100 }),
      "cursor:suites:2",
    );
  });

  it("requires an explicit first-page restart when a TaskSuite cursor expires", async () => {
    const user = userEvent.setup();
    const expired = new CursorExpiredError(
      {
        code: "cursor_expired",
        conflict_set_id: null,
        detail: "TaskSuite read snapshot expired.",
        earliest_cursor: null,
        instance: "/api/v1/task-suites",
        request_id: "request:cursor-expired",
        retry_after_s: null,
        run_id: null,
        status: 410,
        title: "Cursor expired",
        trace_id: null,
        type: "about:blank",
      },
      "cursor:suites:2",
    );
    const listTaskSuites = vi.fn(async (_filters, cursor: string | null) => {
      if (cursor !== null) throw expired;
      return { ...page([suite], "read:suites:expiring"), next_cursor: "cursor:suites:2" };
    });
    const playtestApi = api({ listTaskSuites });
    renderPage(playtestApi, contextPath);

    await screen.findByText(SUITE_ID);
    await user.click(screen.getByRole("button", { name: "加载更多 TaskSuite" }));
    expect(await screen.findByRole("heading", { name: "TaskSuite 目录游标已过期" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "从第一页重新读取" }));

    await waitFor(() => expect(listTaskSuites).toHaveBeenLastCalledWith(expect.anything(), null));
    expect(await screen.findByText(SUITE_ID)).toBeVisible();
  });

  it("derives a suite only from the exact public profile binding", async () => {
    const user = userEvent.setup();
    const playtestApi = api();
    renderPage(playtestApi, `${contextPath}&action=derive`);

    await screen.findByRole("option", { name: /builtin\.task_suite_derivation@2/ });
    await user.click(screen.getByRole("button", { name: "派生 exact TaskSuite" }));

    await waitFor(() => expect(playtestApi.deriveTaskSuite).toHaveBeenCalledTimes(1));
    const [request] = vi.mocked(playtestApi.deriveTaskSuite).mock.calls[0];
    expect(request).toEqual({
      params: {
        completion_oracle_registry_ref: ORACLE_REF,
        config_artifact_id: CONFIG_ID,
        constraint_snapshot_artifact_id: CONSTRAINT_ID,
        derivation_profile: DERIVATION_PROFILE,
        environment_profile: ENV_PROFILE,
        schema_version: "task-suite-derive@1",
        source_preview_artifact_id: PREVIEW_ID,
      },
      request_schema_version: "task-suite-derive-request@1",
    });
    expect((await screen.findAllByText(DERIVE_RUN_ID))[0]).toBeVisible();
  });

  it("offers explicit route-preserving reauthentication when the current tab has no CSRF", async () => {
    const user = userEvent.setup();
    const playtestApi = api({
      deriveTaskSuite: vi.fn(async () => {
        throw new ReauthenticationRequiredError();
      }),
    });
    renderPage(playtestApi, `${contextPath}&action=derive`);

    await screen.findByText("builtin.task_suite_derivation@2");
    await user.click(screen.getByRole("button", { name: "派生 exact TaskSuite" }));

    expect(await screen.findByRole("heading", { name: "需要重新登录" })).toBeVisible();
    expect(screen.getByRole("link", { name: "重新登录" })).toHaveAttribute("href", "/login");
  });

  it("renders binding read failure and retries the exact derivation binding", async () => {
    const user = userEvent.setup();
    const getTaskSuiteDerivationBinding = vi
      .fn()
      .mockRejectedValueOnce(new Error("binding unavailable"))
      .mockResolvedValueOnce(derivationBinding);
    const playtestApi = api({ getTaskSuiteDerivationBinding });
    renderPage(playtestApi, contextPath);

    expect(await screen.findByRole("heading", { name: "派生 binding 不可用" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重试读取派生 binding" }));

    expect(await screen.findByText(`v${ORACLE_REF.registry_version} · ${ORACLE_REF.digest}`)).toBeVisible();
    expect(getTaskSuiteDerivationBinding).toHaveBeenCalledTimes(2);
  });

  it("shows an authority error when the derivation binding differs from its profile", async () => {
    const user = userEvent.setup();
    const playtestApi = api({
      getTaskSuiteDerivationBinding: vi.fn(async () => ({
        ...derivationBinding,
        profile_payload_hash: "f".repeat(64),
      })),
    });
    renderPage(playtestApi, contextPath);

    await screen.findByText(`v${ORACLE_REF.registry_version} · ${ORACLE_REF.digest}`);
    await user.click(screen.getByRole("button", { name: "派生 exact TaskSuite" }));

    expect(await screen.findByRole("heading", { name: "Playtest launch docket 无效" })).toBeVisible();
    expect(playtestApi.deriveTaskSuite).not.toHaveBeenCalled();
  });

  it("requires an explicit restart when execution-profile pagination expires", async () => {
    const user = userEvent.setup();
    const expired = new CursorExpiredError(
      {
        code: "cursor_expired",
        conflict_set_id: null,
        detail: "Profile read snapshot expired.",
        earliest_cursor: null,
        instance: "/api/v1/execution-profiles",
        request_id: "request:profile-cursor-expired",
        retry_after_s: null,
        run_id: null,
        status: 410,
        title: "Cursor expired",
        trace_id: null,
        type: "about:blank",
      },
      "cursor:profiles:2",
    );
    let expireOnce = true;
    const listExecutionProfiles = vi.fn(async (filters, cursor: string | null) => {
      if (filters.profile_kind === "task_suite_derivation") {
        if (cursor === null) {
          return { ...page([], "read:profiles:derivation"), next_cursor: "cursor:profiles:2" };
        }
        if (expireOnce) {
          expireOnce = false;
          throw expired;
        }
        return page([derivationProfile], "read:profiles:derivation");
      }
      return page(
        filters.status === "active" ? [plannerProfile] : [],
        `read:profiles:planner:${filters.status}`,
      );
    });
    const playtestApi = api({ listExecutionProfiles });
    renderPage(playtestApi, contextPath);

    expect(await screen.findByRole("heading", { name: "Profile 目录游标已过期" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "从第一页重新读取 profile 目录" }));

    expect(await screen.findByRole("button", { name: "派生 exact TaskSuite" })).toBeEnabled();
    expect(
      listExecutionProfiles.mock.calls.filter(
        ([filters, cursor]) => filters.profile_kind === "task_suite_derivation" && cursor === null,
      ),
    ).toHaveLength(2);
  });

  it("filters planner profiles that do not cover every exact TaskSuite domain", async () => {
    const playtestApi = api({
      getTaskSuite: vi.fn(async () => multiDomainSuite),
      listExecutionProfiles: vi.fn(async (filters) =>
        page(
          filters.profile_kind === "task_suite_derivation"
            ? [derivationProfile]
            : filters.status === "active"
              ? [plannerProfile]
              : [],
          `read:profiles:${filters.profile_kind}:${filters.status}`,
        ),
      ),
      listTaskSuites: vi.fn(async () => page([multiDomainSuite], "read:suites:multi-domain")),
    });
    renderPage(playtestApi, `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}`);

    expect(await screen.findByRole("heading", { name: "没有兼容的 Playtest planner" })).toBeVisible();
    expect(screen.queryByRole("option", { name: /builtin\.playtest_planner@2/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "解析并启动 Playtest" })).toBeDisabled();
  });

  it("filters derivation profiles that do not cover the exact candidate domains", async () => {
    const multiDomainConfig = configArtifact();
    multiDomainConfig.artifact = {
      ...multiDomainConfig.artifact,
      domain_scope: { domain_ids: ["domain:economy", "domain:narrative"] },
    };
    const playtestApi = api({
      getArtifact: vi.fn(async () => multiDomainConfig),
      listExecutionProfiles: vi.fn(async (filters) =>
        page(
          filters.profile_kind === "task_suite_derivation"
            ? [derivationProfile]
            : filters.status === "active"
              ? [plannerProfile]
              : [],
          `read:profiles:${filters.profile_kind}:${filters.status}`,
        ),
      ),
    });
    renderPage(playtestApi, contextPath);

    expect(
      await screen.findByText("没有覆盖 exact candidate domain 的 active derivation profile。"),
    ).toBeVisible();
    expect(playtestApi.getTaskSuiteDerivationBinding).not.toHaveBeenCalled();
  });

  it("offers replay_only planners only in replay mode", async () => {
    const user = userEvent.setup();
    const replayOnlyPlanner = { ...plannerProfile, status: "replay_only" as const };
    const playtestApi = api({
      listExecutionProfiles: vi.fn(async (filters) =>
        page(
          filters.profile_kind === "task_suite_derivation"
            ? [derivationProfile]
            : filters.status === "replay_only"
              ? [replayOnlyPlanner]
              : [],
          `read:profiles:${filters.profile_kind}:${filters.status}`,
        ),
      ),
    });
    renderPage(playtestApi, `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}`);

    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    expect(
      within(launch).queryByRole("option", { name: /builtin\.playtest_planner@2/ }),
    ).not.toBeInTheDocument();
    await user.selectOptions(within(launch).getByLabelText("LLM execution mode"), "replay");
    expect(await within(launch).findByRole("option", { name: /builtin\.playtest_planner@2/ })).toBeVisible();
    expect(within(launch).queryByRole("textbox", { name: "Replay source Run" })).not.toBeInTheDocument();
    expect(await within(launch).findByRole("option", { name: /自动试玩.*laytest:source/ })).toBeVisible();
    await user.selectOptions(within(launch).getByLabelText("LLM execution mode"), "record");
    expect(
      within(launch).queryByRole("option", { name: /builtin\.playtest_planner@2/ }),
    ).not.toBeInTheDocument();
    expect(within(launch).getByRole("button", { name: "解析并启动 Playtest" })).toBeDisabled();
  });

  it("recovers the exact TaskSuite from a succeeded derivation RunResult", async () => {
    const user = userEvent.setup();
    const playtestApi = api({
      getArtifact: vi.fn(async (artifactId) =>
        artifactId === DERIVE_RESULT_ID ? deriveResultArtifact() : configArtifact(),
      ),
      getRun: vi.fn(async (runId) =>
        runId === DERIVE_RUN_ID
          ? {
              ...run(runId, "succeeded"),
              result_artifact_id: DERIVE_RESULT_ID,
            }
          : run(runId),
      ),
      listTaskSuites: vi.fn(async () => page([], "read:suites:empty")),
    });
    renderPage(playtestApi, `${contextPath}&action=derive`);

    await screen.findByRole("option", { name: /builtin\.task_suite_derivation@2/ });
    await user.click(screen.getByRole("button", { name: "派生 exact TaskSuite" }));

    expect(await screen.findByRole("heading", { name: "Playtest launch docket" })).toBeVisible();
    expect(screen.getAllByText(SUITE_ID)[0]).toBeVisible();
    expect(playtestApi.getArtifact).toHaveBeenCalledWith(DERIVE_RESULT_ID);
    expect(playtestApi.getTaskSuite).toHaveBeenCalledWith(SUITE_ID);
    expect(screen.queryByRole("link", { name: "查看 accepted 派生 Run" })).not.toBeInTheDocument();
    expect(screen.queryByText(/候选上下文已变化/)).not.toBeInTheDocument();
  });

  it("replaces automatic derivation recovery history so Back can leave the normalized route", async () => {
    const user = userEvent.setup();
    const playtestApi = api({
      getArtifact: vi.fn(async (artifactId) =>
        artifactId === DERIVE_RESULT_ID ? deriveResultArtifact() : configArtifact(),
      ),
      getRun: vi.fn(async (runId) =>
        runId === DERIVE_RUN_ID
          ? { ...run(runId, "succeeded"), result_artifact_id: DERIVE_RESULT_ID }
          : run(runId),
      ),
    });
    renderPageWithHistory(playtestApi, `${contextPath}&deriveRun=${encodeURIComponent(DERIVE_RUN_ID)}`);

    expect(await screen.findByRole("region", { name: "Playtest launch docket" })).toHaveTextContent(SUITE_ID);
    await user.click(screen.getByRole("button", { name: "返回上一条历史" }));

    await waitFor(() => expect(screen.getByTestId("location-probe")).toHaveTextContent("/before-playtest"));
  });

  it("rejects derivation recovery when RunView identity differs from the route Run", async () => {
    const otherRunId = "run:task-suite:other";
    const otherManifest = deriveResultArtifact();
    otherManifest.payload = {
      ...(otherManifest.payload as Record<string, unknown>),
      run_id: otherRunId,
    };
    const getArtifact = vi.fn(async (artifactId: string) =>
      artifactId === DERIVE_RESULT_ID ? otherManifest : configArtifact(),
    );
    const playtestApi = api({
      getArtifact,
      getRun: vi.fn(async () => ({
        ...run(otherRunId, "succeeded"),
        result_artifact_id: DERIVE_RESULT_ID,
      })),
    });
    renderPage(playtestApi, `${contextPath}&deriveRun=${encodeURIComponent(DERIVE_RUN_ID)}`);

    expect(await screen.findByRole("heading", { name: "派生 TaskSuite authority 无法闭合" })).toBeVisible();
    expect(getArtifact).not.toHaveBeenCalledWith(DERIVE_RESULT_ID);
  });

  it("keeps a newly derived suite pending for explicit selection while launch is active", async () => {
    let resolveOption!: (value: ExecutionOptionView) => void;
    let deriveStreamCallbacks: PlaytestEventStreamCallbacks | null = null;
    let deriveRun = run(DERIVE_RUN_ID);
    const derivedManifest = deriveResultArtifact();
    derivedManifest.payload = {
      ...(derivedManifest.payload as Record<string, unknown>),
      primary_artifact_id: SUITE_2_ID,
      produced_artifact_ids: [SUITE_2_ID],
    };
    const playtestApi = api({
      createEventStream: vi.fn((callbacks) => {
        if (callbacks.runId === DERIVE_RUN_ID) deriveStreamCallbacks = callbacks;
        return {
          close: vi.fn(),
          restart: vi.fn(async () => undefined),
          start: vi.fn(async () => undefined),
        };
      }),
      getArtifact: vi.fn(async (artifactId) =>
        artifactId === DERIVE_RESULT_ID ? derivedManifest : configArtifact(),
      ),
      getRun: vi.fn(async (runId) => (runId === DERIVE_RUN_ID ? deriveRun : run(runId))),
      getTaskSuite: vi.fn(async (artifactId) => (artifactId === SUITE_2_ID ? secondSuite : suite)),
      resolveExecutionOption: vi.fn(
        () =>
          new Promise<ExecutionOptionView>((resolve) => {
            resolveOption = resolve;
          }),
      ),
    });
    renderPage(
      playtestApi,
      `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}&deriveRun=${encodeURIComponent(DERIVE_RUN_ID)}`,
    );

    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    fireEvent.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));
    deriveRun = {
      ...run(DERIVE_RUN_ID, "succeeded"),
      result_artifact_id: DERIVE_RESULT_ID,
    };
    await act(async () => {
      deriveStreamCallbacks?.onEvent?.(succeededEvent(DERIVE_RUN_ID, DERIVE_RESULT_ID), "2");
    });

    expect(await screen.findByText(/新派生的 TaskSuite 已就绪/)).toBeVisible();
    expect(screen.queryByText(/已作为明确选择载入/)).not.toBeInTheDocument();
    expect(within(launch).getByText(SUITE_ID)).toBeVisible();
    expect(screen.getByRole("button", { name: `选择新派生的 ${SUITE_2_ID}` })).toBeDisabled();

    resolveOption(executionOption);
    await waitFor(() => expect(playtestApi.runPlaytest).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: `选择新派生的 ${SUITE_2_ID}` })).toBeEnabled();
  });

  it("does not let an older derivation result replace a newly submitted derivation", async () => {
    const user = userEvent.setup();
    const newDeriveRunId = "run:task-suite:new";
    let deriveStreamCallbacks: PlaytestEventStreamCallbacks | null = null;
    let oldDeriveRun = run(DERIVE_RUN_ID);
    let resolveNewDerive!: (value: RunAccepted) => void;
    const deriveTaskSuite = vi.fn(
      () =>
        new Promise<RunAccepted>((resolve) => {
          resolveNewDerive = resolve;
        }),
    );
    const playtestApi = api({
      createEventStream: vi.fn((callbacks) => {
        if (callbacks.runId === DERIVE_RUN_ID) deriveStreamCallbacks = callbacks;
        return {
          close: vi.fn(),
          restart: vi.fn(async () => undefined),
          start: vi.fn(async () => undefined),
        };
      }),
      deriveTaskSuite,
      getArtifact: vi.fn(async (artifactId) =>
        artifactId === DERIVE_RESULT_ID ? deriveResultArtifact(SUITE_2_ID) : configArtifact(),
      ),
      getRun: vi.fn(async (runId) => (runId === DERIVE_RUN_ID ? oldDeriveRun : run(runId))),
      getTaskSuite: vi.fn(async (artifactId) => (artifactId === SUITE_2_ID ? secondSuite : suite)),
    });
    renderPageWithLocation(
      playtestApi,
      `${contextPath}&action=derive&deriveRun=${encodeURIComponent(DERIVE_RUN_ID)}`,
    );

    await screen.findByRole("option", { name: /builtin\.task_suite_derivation@2/ });
    await user.click(screen.getByRole("button", { name: "派生 exact TaskSuite" }));
    await waitFor(() => expect(deriveTaskSuite).toHaveBeenCalledTimes(1));
    oldDeriveRun = {
      ...run(DERIVE_RUN_ID, "succeeded"),
      result_artifact_id: DERIVE_RESULT_ID,
    };
    await act(async () => {
      deriveStreamCallbacks?.onEvent?.(succeededEvent(DERIVE_RUN_ID, DERIVE_RESULT_ID), "2");
    });

    const recovered = await screen.findByRole("button", {
      name: `选择新派生的 ${SUITE_2_ID}`,
    });
    expect(recovered).toBeDisabled();
    expect(screen.getByTestId("location-probe")).toHaveTextContent(
      `deriveRun=${encodeURIComponent(DERIVE_RUN_ID)}`,
    );
    expect(screen.getByTestId("location-probe")).not.toHaveTextContent(
      `suite=${encodeURIComponent(SUITE_2_ID)}`,
    );

    await act(async () => {
      resolveNewDerive({
        accepted_schema_version: "run-accepted@1",
        events_url: `/api/v1/runs/${newDeriveRunId}/events`,
        run_id: newDeriveRunId,
        status_url: `/api/v1/runs/${newDeriveRunId}`,
      });
    });
    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        `deriveRun=${encodeURIComponent(newDeriveRunId)}`,
      ),
    );
    expect(screen.queryByRole("link", { name: "查看 accepted 派生 Run" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: `选择新派生的 ${SUITE_2_ID}` }));
    expect(screen.getByTestId("location-probe")).toHaveTextContent(
      `deriveRun=${encodeURIComponent(newDeriveRunId)}`,
    );
    expect(screen.getByTestId("location-probe")).toHaveTextContent(`suite=${encodeURIComponent(SUITE_2_ID)}`);
    expect(screen.queryByRole("link", { name: "查看 accepted 派生 Run" })).not.toBeInTheDocument();
  });

  it("does not clear a tracked Playtest Run when a derivation result arrives later", async () => {
    const playtestApi = api({
      getArtifact: vi.fn(async (artifactId) =>
        artifactId === DERIVE_RESULT_ID ? deriveResultArtifact(SUITE_2_ID) : configArtifact(),
      ),
      getRun: vi.fn(async (runId) =>
        runId === DERIVE_RUN_ID
          ? { ...run(runId, "succeeded"), result_artifact_id: DERIVE_RESULT_ID }
          : run(runId),
      ),
      getTaskSuite: vi.fn(async (artifactId) => (artifactId === SUITE_2_ID ? secondSuite : suite)),
      listTaskSuites: vi.fn(async () => page([suite, secondSuite], "read:suites:late-derive")),
    });
    renderPage(
      playtestApi,
      `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}&deriveRun=${encodeURIComponent(DERIVE_RUN_ID)}&run=${encodeURIComponent(PLAYTEST_RUN_ID)}`,
    );

    expect(await screen.findByText(/新派生的 TaskSuite 已就绪/)).toBeVisible();
    expect(screen.getByLabelText(`Playtest Run ${PLAYTEST_RUN_ID}`)).toBeVisible();
    expect(screen.getByRole("region", { name: "Playtest launch docket" })).toHaveTextContent(SUITE_ID);
    expect(screen.getByRole("button", { name: `选择新派生的 ${SUITE_2_ID}` })).toBeEnabled();
  });

  it("consumes historical derivation recovery before a later explicit suite survives remount", async () => {
    const getRun = vi.fn(async (runId) =>
      runId === DERIVE_RUN_ID
        ? { ...run(runId, "succeeded"), result_artifact_id: DERIVE_RESULT_ID }
        : run(runId),
    );
    const playtestApi = api({
      getArtifact: vi.fn(async (artifactId) =>
        artifactId === DERIVE_RESULT_ID ? deriveResultArtifact(SUITE_2_ID) : configArtifact(),
      ),
      getRun,
      getTaskSuite: vi.fn(async (artifactId) => (artifactId === SUITE_2_ID ? secondSuite : suite)),
      listTaskSuites: vi.fn(async () => page([suite, secondSuite], "read:suites:consume-derive")),
    });
    const first = renderPageWithLocation(
      playtestApi,
      `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}&deriveRun=${encodeURIComponent(DERIVE_RUN_ID)}`,
    );

    await screen.findByText(/新派生的 TaskSuite 已就绪/);
    await userEvent.click(screen.getByRole("button", { name: `选择新派生的 ${SUITE_2_ID}` }));
    expect(await screen.findByRole("region", { name: "Playtest launch docket" })).toHaveTextContent(
      SUITE_2_ID,
    );
    await userEvent.click(screen.getByRole("button", { name: `选择 ${SUITE_ID}` }));
    expect(await screen.findByRole("region", { name: "Playtest launch docket" })).toHaveTextContent(SUITE_ID);
    const remountPath = screen.getByTestId("location-probe").textContent!;
    expect(remountPath).not.toContain("deriveRun=");

    first.unmount();
    getRun.mockClear();
    renderPage(playtestApi, remountPath);
    expect(await screen.findByRole("region", { name: "Playtest launch docket" })).toHaveTextContent(SUITE_ID);
    expect(getRun).not.toHaveBeenCalledWith(DERIVE_RUN_ID);
  });

  it("launches an explicit non-empty episode subset through the resolved execution option", async () => {
    const user = userEvent.setup();
    const playtestApi = api();
    renderPage(playtestApi, contextPath);

    await user.click(await screen.findByRole("button", { name: `选择 ${SUITE_ID}` }));
    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    const signal = within(launch).getByRole("checkbox", { name: /episode:signal/ });
    await user.click(signal);
    await user.clear(within(launch).getByLabelText("每 episode 最大步数"));
    await user.type(within(launch).getByLabelText("每 episode 最大步数"), "7");
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));

    await waitFor(() => expect(playtestApi.resolveExecutionOption).toHaveBeenCalledTimes(1));
    const [prospective] = vi.mocked(playtestApi.resolveExecutionOption).mock.calls[0];
    expect(prospective.prospective_request).toMatchObject({
      cassette_artifact_id: null,
      execution_version_plan: null,
      llm_execution_mode: "record",
      params: {
        config_artifact_id: CONFIG_ID,
        constraint_snapshot_artifact_id: CONSTRAINT_ID,
        episodes: [
          {
            episode_id: "episode:bridge",
            scenario_spec_artifact_id: "artifact:scenario:bridge",
          },
        ],
        interaction_mode: "autonomous",
        max_steps_per_episode: 7,
        planner_policy: PLANNER_PROFILE,
        task_suite_artifact_id: SUITE_ID,
      },
      request_schema_version: "playtest-run-request@1",
      seed: 1,
    });
    await waitFor(() => expect(playtestApi.runPlaytest).toHaveBeenCalledTimes(1));
    const [resolved] = vi.mocked(playtestApi.runPlaytest).mock.calls[0];
    expect(resolved.execution_version_plan).toEqual(executionPlan);
    expect(resolved.cassette_artifact_id).toBeNull();
    expect((await screen.findAllByText(PLAYTEST_RUN_ID))[0]).toBeVisible();
  });

  it("rejects a resolved execution option whose domain differs from the exact TaskSuite", async () => {
    const user = userEvent.setup();
    const playtestApi = api({
      resolveExecutionOption: vi.fn(async () => ({
        ...executionOption,
        domain_scope: { domain_ids: ["domain:economy"] },
      })),
    });
    renderPage(playtestApi, contextPath);

    await user.click(await screen.findByRole("button", { name: `选择 ${SUITE_ID}` }));
    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));

    expect(await screen.findByRole("heading", { name: "执行选项未解析" })).toBeVisible();
    expect(playtestApi.runPlaytest).not.toHaveBeenCalled();
  });

  it("keeps one resolve-to-mutation chain when launch is clicked twice before resolution", async () => {
    let resolveOption!: (value: ExecutionOptionView) => void;
    const resolveExecutionOption = vi.fn(
      () =>
        new Promise<ExecutionOptionView>((resolve) => {
          resolveOption = resolve;
        }),
    );
    const playtestApi = api({ resolveExecutionOption });
    renderPage(playtestApi, contextPath);

    fireEvent.click(await screen.findByRole("button", { name: `选择 ${SUITE_ID}` }));
    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    const submit = within(launch).getByRole("button", { name: "解析并启动 Playtest" });
    fireEvent.click(submit);
    fireEvent.click(submit);

    expect(resolveExecutionOption).toHaveBeenCalledTimes(1);
    expect(within(launch).getByRole("button", { name: "正在解析并提交…" })).toBeDisabled();
    expect(within(launch).getByLabelText("每 episode 最大步数")).toBeDisabled();

    resolveOption(executionOption);
    await waitFor(() => expect(playtestApi.runPlaytest).toHaveBeenCalledTimes(1));
  });

  it("merges an accepted Run into the latest query instead of an async stale render", async () => {
    const user = userEvent.setup();
    let resolveOption!: (value: ExecutionOptionView) => void;
    const playtestApi = api({
      resolveExecutionOption: vi.fn(
        () =>
          new Promise<ExecutionOptionView>((resolve) => {
            resolveOption = resolve;
          }),
      ),
    });
    renderPageWithRouteControls(playtestApi, contextPath);

    await user.click(await screen.findByRole("button", { name: `选择 ${SUITE_ID}` }));
    await user.click(screen.getByRole("button", { name: "解析并启动 Playtest" }));
    await user.click(screen.getByRole("button", { name: "写入新 query" }));
    resolveOption(executionOption);

    await waitFor(() => expect(playtestApi.runPlaytest).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("location-probe")).toHaveTextContent("marker=fresh"));
    expect(screen.getByTestId("location-probe")).toHaveTextContent(
      `run=${encodeURIComponent(PLAYTEST_RUN_ID)}`,
    );
  });

  it("does not mutate after execution resolution when the exact suite owner changed", async () => {
    const user = userEvent.setup();
    let resolveOption!: (value: ExecutionOptionView) => void;
    const playtestApi = api({
      getTaskSuite: vi.fn(async (artifactId) => (artifactId === SUITE_2_ID ? secondSuite : suite)),
      listTaskSuites: vi.fn(async () => page([suite, secondSuite], "read:suites:route-owner")),
      resolveExecutionOption: vi.fn(
        () =>
          new Promise<ExecutionOptionView>((resolve) => {
            resolveOption = resolve;
          }),
      ),
    });
    renderPageWithRouteControls(playtestApi, `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}`);

    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));
    await user.click(screen.getByRole("button", { name: "外部切换 suite owner" }));
    expect(await screen.findByRole("region", { name: "Playtest launch docket" })).toHaveTextContent(
      SUITE_2_ID,
    );
    resolveOption(executionOption);

    await waitFor(() => expect(playtestApi.resolveExecutionOption).toHaveBeenCalledTimes(1));
    expect(playtestApi.runPlaytest).not.toHaveBeenCalled();
    expect(screen.getByTestId("location-probe")).not.toHaveTextContent("run=");
  });

  it("keeps an explicitly selected Playtest Run when an earlier launch is accepted later", async () => {
    const user = userEvent.setup();
    let resolveRun!: (value: RunAccepted) => void;
    const runPlaytest = vi.fn(
      () =>
        new Promise<RunAccepted>((resolve) => {
          resolveRun = resolve;
        }),
    );
    const playtestApi = api({ runPlaytest });
    renderPageWithRouteControls(playtestApi, `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}`);

    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));
    await waitFor(() => expect(runPlaytest).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "外部切换 Playtest Run" }));
    resolveRun({
      accepted_schema_version: "run-accepted@1",
      events_url: `/api/v1/runs/${PLAYTEST_RUN_ID}/events`,
      run_id: PLAYTEST_RUN_ID,
      status_url: `/api/v1/runs/${PLAYTEST_RUN_ID}`,
    });

    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent("run=run%3Aplaytest%3Aexternal"),
    );
    expect(await screen.findByRole("link", { name: "查看 accepted Run" })).toHaveAttribute(
      "href",
      `/runs/${encodeURIComponent(PLAYTEST_RUN_ID)}`,
    );
  });

  it("keeps an explicitly selected derivation Run when an earlier derivation is accepted later", async () => {
    const user = userEvent.setup();
    let resolveRun!: (value: RunAccepted) => void;
    const deriveTaskSuite = vi.fn(
      () =>
        new Promise<RunAccepted>((resolve) => {
          resolveRun = resolve;
        }),
    );
    const playtestApi = api({ deriveTaskSuite });
    renderPageWithRouteControls(playtestApi, `${contextPath}&action=derive`);

    await screen.findByRole("option", { name: /builtin\.task_suite_derivation@2/ });
    await user.click(screen.getByRole("button", { name: "派生 exact TaskSuite" }));
    await waitFor(() => expect(deriveTaskSuite).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "外部切换派生 Run" }));
    resolveRun({
      accepted_schema_version: "run-accepted@1",
      events_url: `/api/v1/runs/${DERIVE_RUN_ID}/events`,
      run_id: DERIVE_RUN_ID,
      status_url: `/api/v1/runs/${DERIVE_RUN_ID}`,
    });

    await waitFor(() =>
      expect(screen.getByTestId("location-probe")).toHaveTextContent("deriveRun=run%3Atask-suite%3Aexternal"),
    );
    expect(await screen.findByRole("link", { name: "查看 accepted 派生 Run" })).toHaveAttribute(
      "href",
      `/runs/${encodeURIComponent(DERIVE_RUN_ID)}`,
    );
  });

  it("keeps an accepted Run visible while a newly selected suite finishes loading", async () => {
    const user = userEvent.setup();
    let resolveSecondSuite!: (value: TaskSuiteArtifactView) => void;
    let resolveRun!: (value: RunAccepted) => void;
    const getTaskSuite = vi.fn((artifactId: string) =>
      artifactId === SUITE_2_ID
        ? new Promise<TaskSuiteArtifactView>((resolve) => {
            resolveSecondSuite = resolve;
          })
        : Promise.resolve(suite),
    );
    const runPlaytest = vi.fn(
      () =>
        new Promise<RunAccepted>((resolve) => {
          resolveRun = resolve;
        }),
    );
    const playtestApi = api({ getTaskSuite, runPlaytest });
    renderPageWithRouteControls(playtestApi, `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}`);

    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));
    await waitFor(() => expect(runPlaytest).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "外部切换 suite owner" }));
    await waitFor(() => expect(getTaskSuite).toHaveBeenCalledWith(SUITE_2_ID));
    await act(async () => {
      resolveRun({
        accepted_schema_version: "run-accepted@1",
        events_url: `/api/v1/runs/${PLAYTEST_RUN_ID}/events`,
        run_id: PLAYTEST_RUN_ID,
        status_url: `/api/v1/runs/${PLAYTEST_RUN_ID}`,
      });
    });
    expect(await screen.findByRole("link", { name: "查看 accepted Run" })).toHaveAttribute(
      "href",
      `/runs/${encodeURIComponent(PLAYTEST_RUN_ID)}`,
    );

    await act(async () => {
      resolveSecondSuite(secondSuite);
    });
    expect(await screen.findByRole("region", { name: "Playtest launch docket" })).toHaveTextContent(
      SUITE_2_ID,
    );
    expect(screen.getByRole("link", { name: "查看 accepted Run" })).toHaveAttribute(
      "href",
      `/runs/${encodeURIComponent(PLAYTEST_RUN_ID)}`,
    );
  });

  it("keeps an accepted Run receipt when navigation returns to its original suite", async () => {
    const user = userEvent.setup();
    const playtestApi = api({
      getTaskSuite: vi.fn(async (artifactId) => (artifactId === SUITE_2_ID ? secondSuite : suite)),
      listTaskSuites: vi.fn(async () => page([suite, secondSuite], "read:suites:accepted-receipt")),
    });
    renderPage(playtestApi, `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}`);

    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));
    await screen.findByLabelText(`Playtest Run ${PLAYTEST_RUN_ID}`);
    await user.click(screen.getByRole("button", { name: `选择 ${SUITE_2_ID}` }));
    expect(await screen.findByRole("link", { name: "查看 accepted Run" })).toHaveAttribute(
      "href",
      `/runs/${encodeURIComponent(PLAYTEST_RUN_ID)}`,
    );

    await user.click(screen.getByRole("button", { name: `选择 ${SUITE_ID}` }));
    expect(await screen.findByRole("region", { name: "Playtest launch docket" })).toHaveTextContent(SUITE_ID);
    expect(screen.getByRole("link", { name: "查看 accepted Run" })).toHaveAttribute(
      "href",
      `/runs/${encodeURIComponent(PLAYTEST_RUN_ID)}`,
    );
  });

  it("does not write an accepted Run into the router after the page unmounts", async () => {
    const user = userEvent.setup();
    let resolveRun!: (value: RunAccepted) => void;
    const runPlaytest = vi.fn(
      () =>
        new Promise<RunAccepted>((resolve) => {
          resolveRun = resolve;
        }),
    );
    const playtestApi = api({ runPlaytest });
    renderUnmountablePage(playtestApi, `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}`);

    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));
    await waitFor(() => expect(runPlaytest).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "卸载 Playtest 页面" }));
    resolveRun({
      accepted_schema_version: "run-accepted@1",
      events_url: `/api/v1/runs/${PLAYTEST_RUN_ID}/events`,
      run_id: PLAYTEST_RUN_ID,
      status_url: `/api/v1/runs/${PLAYTEST_RUN_ID}`,
    });

    await act(async () => undefined);
    expect(screen.getByTestId("location-probe")).not.toHaveTextContent("run=");
  });

  it("treats an empty seed as invalid instead of coercing it to zero", async () => {
    const user = userEvent.setup();
    const playtestApi = api();
    renderPage(playtestApi, contextPath);

    await user.click(await screen.findByRole("button", { name: `选择 ${SUITE_ID}` }));
    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    await user.clear(within(launch).getByLabelText("Seed"));
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));

    expect(await screen.findByRole("heading", { name: "Playtest launch docket 无效" })).toBeVisible();
    expect(playtestApi.resolveExecutionOption).not.toHaveBeenCalled();
    expect(playtestApi.runPlaytest).not.toHaveBeenCalled();
  });

  it("freezes the Playtest request and intent after an unknown transport outcome", async () => {
    const user = userEvent.setup();
    const runPlaytest = vi
      .fn()
      .mockRejectedValueOnce(new Error("connection dropped"))
      .mockResolvedValueOnce({
        accepted_schema_version: "run-accepted@1",
        events_url: `/api/v1/runs/${PLAYTEST_RUN_ID}/events`,
        run_id: PLAYTEST_RUN_ID,
        status_url: `/api/v1/runs/${PLAYTEST_RUN_ID}`,
      });
    const playtestApi = api({ runPlaytest });
    renderPage(playtestApi, contextPath);

    await user.click(await screen.findByRole("button", { name: `选择 ${SUITE_ID}` }));
    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));

    expect(await within(launch).findByRole("button", { name: "重试同一 Playtest intent" })).toBeVisible();
    expect(within(launch).getByLabelText("每 episode 最大步数")).toBeDisabled();
    const [firstRequest, firstIntent] = runPlaytest.mock.calls[0];

    await user.click(within(launch).getByRole("button", { name: "重试同一 Playtest intent" }));

    await waitFor(() => expect(runPlaytest).toHaveBeenCalledTimes(2));
    expect(playtestApi.resolveExecutionOption).toHaveBeenCalledTimes(1);
    expect(runPlaytest.mock.calls[1][0]).toBe(firstRequest);
    expect(runPlaytest.mock.calls[1][1]).toBe(firstIntent);
  });

  it("freezes the TaskSuite derivation request and intent after an unknown transport outcome", async () => {
    const user = userEvent.setup();
    const deriveTaskSuite = vi
      .fn()
      .mockRejectedValueOnce(new Error("connection dropped"))
      .mockResolvedValueOnce({
        accepted_schema_version: "run-accepted@1",
        events_url: `/api/v1/runs/${DERIVE_RUN_ID}/events`,
        run_id: DERIVE_RUN_ID,
        status_url: `/api/v1/runs/${DERIVE_RUN_ID}`,
      });
    const playtestApi = api({ deriveTaskSuite });
    renderPage(playtestApi, `${contextPath}&action=derive`);

    await screen.findByText("builtin.task_suite_derivation@2");
    await user.click(screen.getByRole("button", { name: "派生 exact TaskSuite" }));

    expect(await screen.findByRole("button", { name: "重试同一派生 intent" })).toBeVisible();
    const [firstRequest, firstIntent] = deriveTaskSuite.mock.calls[0];
    await user.click(screen.getByRole("button", { name: "重试同一派生 intent" }));

    await waitFor(() => expect(deriveTaskSuite).toHaveBeenCalledTimes(2));
    expect(deriveTaskSuite.mock.calls[1][0]).toBe(firstRequest);
    expect(deriveTaskSuite.mock.calls[1][1]).toBe(firstIntent);
  });

  it("rejects an empty episode subset before execution resolution", async () => {
    const user = userEvent.setup();
    const playtestApi = api();
    renderPage(playtestApi, contextPath);

    await user.click(await screen.findByRole("button", { name: `选择 ${SUITE_ID}` }));
    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    for (const checkbox of within(launch).getAllByRole("checkbox")) {
      await user.click(checkbox);
    }
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));

    expect(await screen.findByRole("heading", { name: "Playtest launch docket 无效" })).toBeVisible();
    expect(playtestApi.resolveExecutionOption).not.toHaveBeenCalled();
    expect(playtestApi.runPlaytest).not.toHaveBeenCalled();
  });

  it("keeps stale_task_suite visible and never silently replaces the selected suite", async () => {
    const user = userEvent.setup();
    const stale = new ApiProblemError({
      code: "stale_task_suite",
      conflict_set_id: null,
      detail: "TaskSuite binds the previous config export.",
      earliest_cursor: null,
      instance: "/api/v1/playtest:run",
      request_id: "request:stale-suite",
      retry_after_s: null,
      run_id: null,
      status: 409,
      title: "Stale TaskSuite",
      trace_id: null,
      type: "about:blank",
    });
    const playtestApi = api({ runPlaytest: vi.fn().mockRejectedValue(stale) });
    renderPage(playtestApi, contextPath);

    await user.click(await screen.findByRole("button", { name: `选择 ${SUITE_ID}` }));
    await user.click(screen.getByRole("button", { name: "解析并启动 Playtest" }));

    expect(await screen.findByText("stale_task_suite")).toBeVisible();
    expect(screen.getAllByText(SUITE_ID)[0]).toBeVisible();
    const rederive = screen.getByRole("button", { name: "按当前候选重新派生 TaskSuite" });
    expect(rederive).toBeVisible();
    expect(playtestApi.deriveTaskSuite).not.toHaveBeenCalled();
    await user.click(rederive);
    expect(await screen.findByText(/已切换到显式重新派生/)).toBeVisible();
    expect(playtestApi.deriveTaskSuite).not.toHaveBeenCalled();
  });

  it("hydrates exact candidate reads before re-deriving a stale standalone suite", async () => {
    const user = userEvent.setup();
    const stale = new ApiProblemError({
      code: "stale_task_suite",
      conflict_set_id: null,
      detail: "TaskSuite binds the previous config export.",
      earliest_cursor: null,
      instance: "/api/v1/playtest:run",
      request_id: "request:standalone-stale-suite",
      retry_after_s: null,
      run_id: null,
      status: 409,
      title: "Stale TaskSuite",
      trace_id: null,
      type: "about:blank",
    });
    const playtestApi = api({ runPlaytest: vi.fn().mockRejectedValue(stale) });
    renderPage(playtestApi, `/playtest?suite=${encodeURIComponent(SUITE_ID)}`);

    const launch = await screen.findByRole("region", { name: "Playtest launch docket" });
    await user.click(within(launch).getByRole("button", { name: "解析并启动 Playtest" }));
    await user.click(await screen.findByRole("button", { name: "按当前候选重新派生 TaskSuite" }));

    await waitFor(() => expect(playtestApi.getSpec).toHaveBeenCalledWith(PREVIEW_ID));
    expect(playtestApi.getConstraint).toHaveBeenCalledWith(CONSTRAINT_ID);
    expect(playtestApi.getArtifact).toHaveBeenCalledWith(CONFIG_ID);
    expect(await screen.findByRole("heading", { name: "派生 TaskSuite" })).toBeVisible();
  });

  it("hard-blocks suite and Run authority when candidate URL context is partial", async () => {
    const playtestApi = api();
    renderPage(
      playtestApi,
      `/playtest?preview=${encodeURIComponent(PREVIEW_ID)}&suite=${encodeURIComponent(SUITE_ID)}&run=${encodeURIComponent(PLAYTEST_RUN_ID)}`,
    );

    expect(await screen.findByRole("heading", { name: "候选绑定无法闭合" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "Playtest launch docket" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(`Playtest Run ${PLAYTEST_RUN_ID}`)).not.toBeInTheDocument();
    expect(playtestApi.listTaskSuites).not.toHaveBeenCalled();
    expect(playtestApi.getTaskSuite).not.toHaveBeenCalled();
    expect(playtestApi.getRun).not.toHaveBeenCalled();
  });

  it("does not show a reconnect failure when terminal RunView closes an empty resumed stream", async () => {
    const playtestApi = api({
      createEventStream: vi.fn((callbacks) => ({
        close: vi.fn(),
        restart: vi.fn(async () => undefined),
        start: vi.fn(async () => {
          callbacks.onStateChange({ status: "disconnected" });
        }),
      })),
      getRun: vi.fn(async () => ({
        ...run(PLAYTEST_RUN_ID, "succeeded"),
        result_artifact_id: "artifact:result:playtest",
      })),
    });
    renderPage(playtestApi, `/playtest?run=${encodeURIComponent(PLAYTEST_RUN_ID)}`);

    expect(await screen.findByText("succeeded")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "事件流连接中断" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "使用已保存 cursor 重连" })).not.toBeInTheDocument();
  });

  it("shows a reconnect failure when a terminal RunView receives a partial suffix before EOF", async () => {
    const playtestApi = api({
      createEventStream: vi.fn((callbacks) => ({
        close: vi.fn(),
        restart: vi.fn(async () => undefined),
        start: vi.fn(async () => {
          callbacks.onStateChange({ status: "connecting" });
          callbacks.onEvent(progressEvent(PLAYTEST_RUN_ID, "2", "partial suffix"), "2");
          callbacks.onStateChange({ status: "disconnected" });
        }),
      })),
      getRun: vi.fn(async () => ({
        ...run(PLAYTEST_RUN_ID, "succeeded"),
        result_artifact_id: "artifact:result:playtest",
      })),
    });
    renderPage(playtestApi, `/playtest?run=${encodeURIComponent(PLAYTEST_RUN_ID)}`);

    expect(await screen.findByText("succeeded")).toBeVisible();
    expect(screen.getByRole("heading", { name: "事件流连接中断" })).toBeVisible();
    expect(screen.getByRole("button", { name: "使用已保存 cursor 重连" })).toBeVisible();
  });

  it("keeps distinct committed cursors whose numeric seq values lose precision", async () => {
    const firstCursor = "9007199254740992";
    const secondCursor = "9007199254740993";
    expect(Number(firstCursor)).toBe(Number(secondCursor));
    const playtestApi = api({
      createEventStream: vi.fn((callbacks) => ({
        close: vi.fn(),
        restart: vi.fn(async () => undefined),
        start: vi.fn(async () => {
          callbacks.onEvent(progressEvent(PLAYTEST_RUN_ID, firstCursor, "first"), firstCursor);
          callbacks.onEvent(progressEvent(PLAYTEST_RUN_ID, secondCursor, "second"), secondCursor);
        }),
      })),
    });
    renderPage(playtestApi, `/playtest?run=${encodeURIComponent(PLAYTEST_RUN_ID)}`);

    const tracked = await screen.findByLabelText(`Playtest Run ${PLAYTEST_RUN_ID}`);
    expect(await within(tracked).findAllByText(/attempt\.progress/)).toHaveLength(2);
    expect(within(tracked).getByRole("progressbar")).toHaveAccessibleName("second");
  });

  it("keeps a real stream error visible even when RunView is terminal", async () => {
    const playtestApi = api({
      createEventStream: vi.fn((callbacks) => ({
        close: vi.fn(),
        restart: vi.fn(async () => undefined),
        start: vi.fn(async () => {
          callbacks.onStateChange({ error: new Error("invalid terminal frame"), status: "error" });
        }),
      })),
      getRun: vi.fn(async () => ({
        ...run(PLAYTEST_RUN_ID, "succeeded"),
        result_artifact_id: "artifact:result:playtest",
      })),
    });
    renderPage(playtestApi, `/playtest?run=${encodeURIComponent(PLAYTEST_RUN_ID)}`);

    expect(await screen.findByText("succeeded")).toBeVisible();
    expect(screen.getByRole("heading", { name: "事件流连接中断" })).toBeVisible();
    expect(screen.getByRole("button", { name: "使用已保存 cursor 重连" })).toBeVisible();
  });

  it("keeps Run progress and command-control labelling unique when derive and Playtest Runs coexist", async () => {
    const playtestApi = api();
    renderPage(
      playtestApi,
      `${contextPath}&suite=${encodeURIComponent(SUITE_ID)}&deriveRun=${encodeURIComponent(DERIVE_RUN_ID)}&run=${encodeURIComponent(PLAYTEST_RUN_ID)}`,
    );

    await waitFor(() => expect(screen.getAllByRole("heading", { name: "运行进度" })).toHaveLength(2));
    const ids = [...document.querySelectorAll<HTMLElement>("[id]")].map((element) => element.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
