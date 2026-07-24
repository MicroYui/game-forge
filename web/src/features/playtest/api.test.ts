import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import { storeCsrfToken } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import type {
  ArtifactPayloadView,
  ConstraintSnapshotView,
  ExecutionOptionResolveRequest,
  PlaytestRunRequest,
  RunView,
  SpecView,
  TaskSuiteArtifactView,
  TaskSuiteDerivationBindingView,
  TaskSuiteDeriveRequest,
} from "./api";
import { createPlaytestApi } from "./api";

function response<T>(data: T) {
  return { data, response: Response.json(data) };
}

function page<T>(items: T[], nextCursor: string | null) {
  return {
    expires_at: "2026-07-20T08:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "page@1",
    read_snapshot_id: "read-snapshot:playtest",
  } as const;
}

const taskSuite = {
  artifact: { artifact_id: "artifact:task-suite:1" },
  task_suite: { task_suite_schema_version: "task-suite@1" },
  view_schema_version: "task-suite-artifact-view@1",
} as unknown as TaskSuiteArtifactView;

const derivationBinding = {
  binding_schema_version: "task-suite-derivation-binding@1",
  completion_oracle_registry_ref: { digest: "a".repeat(64), registry_version: 1 },
  derivation_profile: { profile_id: "builtin.task-suite", version: 1 },
  max_scenarios: 64,
  max_total_prepared_artifact_bytes: 1_048_576,
  profile_payload_hash: "b".repeat(64),
  run_kind: { kind: "task_suite.derive", version: 1 },
  target_environment_profile: { profile_id: "builtin.aureus-env", version: 1 },
} as TaskSuiteDerivationBindingView;

const spec = { artifact: { artifact_id: "artifact:preview:1" } } as unknown as SpecView;
const constraint = {
  artifact: { artifact_id: "artifact:constraint:1" },
} as unknown as ConstraintSnapshotView;
const artifact = {
  artifact: { artifact_id: "artifact:config:1" },
} as unknown as ArtifactPayloadView;
const run = { run_id: "run:playtest:1", status: "running" } as unknown as RunView;
const failedReplaySource = {
  failure_artifact_id: "artifact:failure:playtest-source",
  result_artifact_id: null,
  run_id: "run:playtest:failed-source",
  status: "failed",
  terminal_cassette_artifact_id: "artifact:cassette:playtest-failed",
} as RunView;
const replayFailureManifest = {
  artifact: {
    artifact_id: "artifact:failure:playtest-source",
    created_at: "2026-07-23T03:47:50Z",
    kind: "run_failure",
    payload_schema_id: "run-failure@1",
  },
  payload: {
    cause_code: "step_limit_exhausted",
    failure_schema_version: "run-failure@1",
    run_id: failedReplaySource.run_id,
    run_kind: { kind: "playtest.run", version: 1 },
  },
} as unknown as ArtifactPayloadView;
const successWithoutCassette = {
  run_id: "run:playtest:no-cassette",
  status: "succeeded",
  terminal_cassette_artifact_id: null,
} as RunView;

const deriveRequest: TaskSuiteDeriveRequest = {
  params: {
    completion_oracle_registry_ref: { digest: "a".repeat(64), registry_version: 1 },
    config_artifact_id: "artifact:config:1",
    constraint_snapshot_artifact_id: "artifact:constraint:1",
    derivation_profile: { profile_id: "builtin.task-suite", version: 1 },
    environment_profile: { profile_id: "builtin.aureus-env", version: 1 },
    schema_version: "task-suite-derive@1",
    source_preview_artifact_id: "artifact:preview:1",
  },
  request_schema_version: "task-suite-derive-request@1",
};

const prospectivePlaytestRequest: components["schemas"]["ProspectivePlaytestRunRequestV1"] = {
  cassette_artifact_id: null,
  execution_version_plan: null,
  llm_execution_mode: "replay",
  params: {
    config_artifact_id: "artifact:config:1",
    constraint_snapshot_artifact_id: "artifact:constraint:1",
    environment_profile: { profile_id: "builtin.aureus-env", version: 1 },
    episodes: [
      {
        episode_id: "episode:quest:1",
        scenario_spec_artifact_id: "artifact:scenario:1",
      },
    ],
    interaction_mode: "autonomous",
    max_steps_per_episode: 80,
    planner_policy: { profile_id: "builtin.playtest-planner", version: 2 },
    schema_version: "playtest-run@1",
    task_suite_artifact_id: "artifact:task-suite:1",
  },
  request_schema_version: "playtest-run-request@1",
  seed: 17,
};

const resolveRequest: ExecutionOptionResolveRequest = {
  llm_execution_mode: "replay",
  prospective_request: prospectivePlaytestRequest,
  replay_source_run_id: "run:playtest:source",
  request_schema_version: "execution-option-resolve-request@1",
  resource_operation_id: "run_playtest_api_v1_playtest_run_post",
  run_kind: { kind: "playtest.run", version: 1 },
};

const executionPlan: components["schemas"]["ExecutionVersionPlanV1"] = {
  agent_graph_version: "playtest@2",
  model_catalog_digest: "c".repeat(64),
  model_catalog_version: 2,
  nodes: [
    {
      agent_node_id: "planner",
      allowed_model_snapshots: ["openai/gpt-5.6-sol/m4@1"],
      prompt_version: "playtest-planner@2",
      tool_version: "playtest-agent@2",
    },
  ],
  plan_digest: "d".repeat(64),
  plan_schema_version: "execution-version-plan@1",
  routing_policy_digest: "e".repeat(64),
  routing_policy_version: 1,
};

const playtestRequest: PlaytestRunRequest = {
  ...prospectivePlaytestRequest,
  cassette_artifact_id: "artifact:cassette:playtest:source",
  execution_version_plan: executionPlan,
};

const intent = Object.freeze({
  idempotencyKey: "11111111-1111-4111-8111-111111111111",
});
const playtestIntent = Object.freeze({
  idempotencyKey: "22222222-2222-4222-8222-222222222222",
});

describe("Playtest API", () => {
  beforeEach(() => {
    sessionStorage.clear();
    storeCsrfToken("csrf:playtest");
  });

  it("reads exact suite, authority, candidate, run, result, Finding, and command resources", async () => {
    const cursor = "opaque.playtest+/=%2Ftail";
    const get = vi.fn(async (path: string, options?: { params?: { path?: { artifact_id?: string } } }) => {
      switch (path) {
        case "/api/v1/task-suites":
          return response(page([taskSuite], null));
        case "/api/v1/artifacts":
          return response(page([artifact.artifact], null));
        case "/api/v1/task-suites/{artifact_id}":
          return response(taskSuite);
        case "/api/v1/execution-profiles":
        case "/api/v1/runs/{run_id}/finding-links":
        case "/api/v1/runs/{run_id}/commands":
          return response(page([], null));
        case "/api/v1/runs":
          return response(page([failedReplaySource, successWithoutCassette], null));
        case "/api/v1/execution-profiles/{profile_id}/versions/{version}/task-suite-derivation-binding":
          return response(derivationBinding);
        case "/api/v1/specs/{artifact_id}":
          return response(spec);
        case "/api/v1/constraints/{artifact_id}":
          return response(constraint);
        case "/api/v1/artifacts/{artifact_id}":
          if (options?.params?.path?.artifact_id === replayFailureManifest.artifact.artifact_id) {
            return response(replayFailureManifest);
          }
          return response(artifact);
        case "/api/v1/playtest/{run_id}/result":
          return response(artifact);
        case "/api/v1/runs/{run_id}":
          return response(run);
        default:
          throw new Error(`Unexpected GET ${path}`);
      }
    });
    const api = createPlaytestApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await api.listTaskSuites(
      {
        config_artifact_id: "artifact:config:1",
        constraint_artifact_id: "artifact:constraint:1",
        environment_profile_id: "builtin.aureus-env",
        environment_profile_version: 1,
        limit: 25,
      },
      cursor,
    );
    await api.getTaskSuite("artifact:task-suite:1");
    await api.getTaskSuiteDerivationBinding("builtin.task-suite", 1);
    await api.listExecutionProfiles(
      {
        domain_id: "domain:quest",
        limit: 50,
        profile_kind: "playtest_planner",
        run_kind: "playtest.run",
        run_kind_version: 1,
        status: "active",
      },
      cursor,
    );
    await api.getSpec("artifact:preview:1");
    await api.getConstraint("artifact:constraint:1");
    await api.getArtifact("artifact:config:1");
    await api.getRun("run:playtest:1");
    await api.getPlaytestResult("run:playtest:1");
    await api.listConfigExports(cursor);
    await api.listRunFindingLinks("run:playtest:1", cursor);
    await api.listRunCommands("run:playtest:1", cursor);
    const replaySources = await api.listReplaySourceRuns(cursor);

    expect(replaySources.items).toEqual([
      {
        ...failedReplaySource,
        completedAt: "2026-07-23T03:47:50Z",
        outcomeCode: "step_limit_exhausted",
        runKind: { kind: "playtest.run", version: 1 },
      },
    ]);

    expect(get).toHaveBeenCalledWith("/api/v1/task-suites", {
      params: {
        query: {
          config_artifact_id: "artifact:config:1",
          constraint_artifact_id: "artifact:constraint:1",
          cursor,
          environment_profile_id: "builtin.aureus-env",
          environment_profile_version: 1,
          limit: 25,
        },
      },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/artifacts", {
      params: { query: { cursor, kind: "config_export", limit: 100 } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/task-suites/{artifact_id}", {
      params: { path: { artifact_id: "artifact:task-suite:1" } },
    });
    expect(get).toHaveBeenCalledWith(
      "/api/v1/execution-profiles/{profile_id}/versions/{version}/task-suite-derivation-binding",
      { params: { path: { profile_id: "builtin.task-suite", version: 1 } } },
    );
    expect(get).toHaveBeenCalledWith("/api/v1/execution-profiles", {
      params: {
        query: {
          cursor,
          domain_id: "domain:quest",
          limit: 50,
          profile_kind: "playtest_planner",
          run_kind: "playtest.run",
          run_kind_version: 1,
          status: "active",
        },
      },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/runs", {
      params: { query: { cursor } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/specs/{artifact_id}", {
      params: { path: { artifact_id: "artifact:preview:1" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/constraints/{artifact_id}", {
      params: { path: { artifact_id: "artifact:constraint:1" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/artifacts/{artifact_id}", {
      params: { path: { artifact_id: "artifact:config:1" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/runs/{run_id}", {
      params: { path: { run_id: "run:playtest:1" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/playtest/{run_id}/result", {
      params: { path: { run_id: "run:playtest:1" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/runs/{run_id}/finding-links", {
      params: { path: { run_id: "run:playtest:1" }, query: { cursor } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/runs/{run_id}/commands", {
      params: { path: { run_id: "run:playtest:1" }, query: { cursor } },
    });
  });

  it("turns every paged 410 into an explicit restart boundary without changing the cursor", async () => {
    const staleCursor = "stale.playtest+/=";
    const get = vi.fn(async () => ({
      error: {
        code: "cursor_expired",
        detail: "read snapshot expired",
        instance: "/api/v1/task-suites",
        request_id: "request:playtest-cursor",
        status: 410,
        title: "Cursor expired",
        type: "about:blank",
      },
      response: new Response(undefined, { status: 410 }),
    }));
    const api = createPlaytestApi({ GET: get } as unknown as GameForgeOpenApiClient);

    for (const read of [
      () => api.listTaskSuites({}, staleCursor),
      () => api.listExecutionProfiles({}, staleCursor),
      () => api.listRunFindingLinks("run:playtest:1", staleCursor),
      () => api.listRunCommands("run:playtest:1", staleCursor),
    ]) {
      await expect(read()).rejects.toMatchObject({
        name: "CursorExpiredError",
        staleCursor,
      });
    }

    expect(get).toHaveBeenCalledTimes(4);
  });

  it("submits derivation and Playtest with caller-owned frozen intents and never retries transport", async () => {
    const unknownOutcome = new TypeError("network result unknown");
    const accepted = {
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run%3Aplaytest%3A1/events",
      run_id: "run:playtest:1",
      status_url: "/api/v1/runs/run%3Aplaytest%3A1",
    } as const;
    const post = vi
      .fn()
      .mockRejectedValueOnce(unknownOutcome)
      .mockResolvedValueOnce(response(accepted))
      .mockRejectedValueOnce(unknownOutcome)
      .mockResolvedValueOnce(response(accepted));
    const api = createPlaytestApi({ POST: post } as unknown as GameForgeOpenApiClient);
    const frozenDeriveRequest = Object.freeze(deriveRequest);
    const frozenPlaytestRequest = Object.freeze(playtestRequest);

    await expect(api.deriveTaskSuite(frozenDeriveRequest, intent)).rejects.toBe(unknownOutcome);
    expect(post).toHaveBeenCalledTimes(1);
    await expect(api.deriveTaskSuite(frozenDeriveRequest, intent)).resolves.toEqual(accepted);
    expect(post).toHaveBeenCalledTimes(2);

    await expect(api.runPlaytest(frozenPlaytestRequest, playtestIntent)).rejects.toBe(unknownOutcome);
    expect(post).toHaveBeenCalledTimes(3);
    await expect(api.runPlaytest(frozenPlaytestRequest, playtestIntent)).resolves.toEqual(accepted);
    expect(post).toHaveBeenCalledTimes(4);

    expect(post.mock.calls).toEqual([
      [
        "/api/v1/task-suites:derive",
        {
          body: frozenDeriveRequest,
          params: {
            header: {
              "Idempotency-Key": intent.idempotencyKey,
              "X-CSRF-Token": "csrf:playtest",
            },
          },
        },
      ],
      [
        "/api/v1/task-suites:derive",
        {
          body: frozenDeriveRequest,
          params: {
            header: {
              "Idempotency-Key": intent.idempotencyKey,
              "X-CSRF-Token": "csrf:playtest",
            },
          },
        },
      ],
      [
        "/api/v1/playtest:run",
        {
          body: frozenPlaytestRequest,
          params: {
            header: {
              "Idempotency-Key": playtestIntent.idempotencyKey,
              "X-CSRF-Token": "csrf:playtest",
            },
          },
        },
      ],
      [
        "/api/v1/playtest:run",
        {
          body: frozenPlaytestRequest,
          params: {
            header: {
              "Idempotency-Key": playtestIntent.idempotencyKey,
              "X-CSRF-Token": "csrf:playtest",
            },
          },
        },
      ],
    ]);
  });

  it("resolves the exact prospective Playtest request with CSRF only", async () => {
    const option = { option_id: "execution-option:playtest:1" };
    const post = vi.fn(async () => response(option));
    const api = createPlaytestApi({ POST: post } as unknown as GameForgeOpenApiClient);

    await expect(api.resolveExecutionOption(resolveRequest)).resolves.toBe(option);

    expect(post).toHaveBeenCalledWith("/api/v1/execution-options:resolve", {
      body: resolveRequest,
      params: { header: { "X-CSRF-Token": "csrf:playtest" } },
    });
  });

  it("surfaces stale_task_suite as the authoritative 409 and does not retry it", async () => {
    const post = vi.fn(async () => ({
      error: {
        code: "stale_task_suite",
        detail: "The suite does not match the repaired config.",
        instance: "/api/v1/playtest:run",
        request_id: "request:stale-suite",
        run_id: null,
        status: 409,
        title: "Task suite is stale",
        trace_id: null,
        type: "about:blank",
      },
      response: new Response(undefined, { status: 409 }),
    }));
    const api = createPlaytestApi({ POST: post } as unknown as GameForgeOpenApiClient);

    await expect(api.runPlaytest(playtestRequest, intent)).rejects.toMatchObject({
      name: "ApiProblemError",
      problem: { code: "stale_task_suite", status: 409 },
    });
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("exposes the shared resumable RunEventStream without copying SSE parsing", () => {
    const api = createPlaytestApi({} as GameForgeOpenApiClient);
    const stream = api.createEventStream({
      onEvent: vi.fn(),
      onStateChange: vi.fn(),
      runId: "run:playtest:1",
    });

    expect(stream.close).toEqual(expect.any(Function));
    expect(stream.restart).toEqual(expect.any(Function));
    expect(stream.start).toEqual(expect.any(Function));
    stream.close();
  });
});
