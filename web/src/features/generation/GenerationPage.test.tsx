import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";
import { CursorExpiredError } from "../../api/pagination";
import { createQueryClient } from "../../api/query-client";
import { GenerationPage } from "./GenerationPage";
import type { ExecutionOptionView, GenerationApi, GenerationEventStreamCallbacks } from "./api";

type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];

const domainScope: components["schemas"]["DomainScope"] = { domain_ids: ["domain:economy"] };

function artifact(
  artifactId: string,
  kind: ArtifactSummary["kind"],
  payloadSchemaId: string,
): ArtifactSummary {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-20T00:00:00Z",
    domain_scope: domainScope,
    kind,
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [],
    payload_hash: "a".repeat(64),
    payload_schema_id: payloadSchemaId,
    summary_schema_version: "artifact-summary@1",
    version_tuple: { tool_version: "generation@1" },
  };
}

const spec: components["schemas"]["SpecViewV1"] = {
  artifact: artifact("artifact:spec:economy", "ir_snapshot", "ir-core@1"),
  ref_name: "refs/specs/economy",
  ref_value: { artifact_id: "artifact:spec:economy", revision: 8 },
  schema_registry_version: "registry@3",
  snapshot_id: "snapshot:economy",
  view_schema_version: "spec-view@1",
};

const constraint: components["schemas"]["ConstraintSnapshotViewV1"] = {
  artifact: artifact("artifact:constraint:economy", "constraint_snapshot", "constraint-snapshot@1"),
  constraints: [],
  dsl_grammar_version: "dsl@1",
  view_schema_version: "constraint-snapshot-view@1",
};

function profile(
  id: string,
  kind: ExecutionProfile["profile_kind"],
  runKind: string,
  targetEnvironment: components["schemas"]["ProfileRefV1"] | null = null,
): ExecutionProfile {
  return {
    compatible_run_kinds: [{ kind: runKind, version: 1 }],
    display_name: id,
    domain_scope: domainScope,
    env_contract_version: kind === "config_export" || kind === "environment" ? "aureus@1" : null,
    input_schema_ids: ["generation-propose@1"],
    output_schema_ids: ["patch@2"],
    profile: { profile_id: id, version: 1 },
    profile_kind: kind,
    profile_payload_hash: id.padEnd(64, "0").slice(0, 64),
    required_capabilities: [],
    status: "active",
    stochastic: kind === "generation",
    target_environment_profile: targetEnvironment,
  };
}

const generationProfile = profile("builtin.generation", "generation", "generation.propose");
const environmentProfile = profile("builtin.aureus_env", "environment", "playtest.run");
const exportProfile = profile(
  "builtin.aureus_csv",
  "config_export",
  "generation.propose",
  environmentProfile.profile,
);

const executionPlan: components["schemas"]["ExecutionVersionPlanV1"] = {
  agent_graph_version: "generation@1",
  model_catalog_digest: "b".repeat(64),
  model_catalog_version: 1,
  nodes: [
    {
      agent_node_id: "generate",
      allowed_model_snapshots: ["openai/gpt-5.6-sol/m4@1"],
      prompt_version: "generation@1",
      tool_version: "typed-patch@1",
    },
  ],
  plan_digest: "c".repeat(64),
  plan_schema_version: "execution-version-plan@1",
  routing_policy_digest: "d".repeat(64),
  routing_policy_version: 1,
};

const resolvedOption: ExecutionOptionView = {
  cassette_artifact_id: "artifact:cassette:generation",
  domain_scope: domainScope,
  execution_version_plan: executionPlan,
  llm_execution_mode: "replay",
  option_id: "option:generation:1",
  option_schema_version: "execution-option@1",
  prospective_request_hash: "e".repeat(64),
  resolved_profile_binding_digests: ["f".repeat(64)],
  resolved_request_hash: "1".repeat(64),
  resource_operation_id: "propose_generation_api_v1_generation_propose_post",
  run_kind: { kind: "generation.propose", version: 1 },
  source_run_id: "run:cassette:source",
};

function page<T>(items: T[], readSnapshotId: string) {
  return {
    expires_at: "2026-07-20T12:00:00Z",
    items,
    next_cursor: null,
    page_schema_version: "page@1" as const,
    read_snapshot_id: readSnapshotId,
  };
}

function api(overrides: Partial<GenerationApi> = {}): GenerationApi {
  return {
    createEventStream: vi.fn((_callbacks: GenerationEventStreamCallbacks & { runId: string }) => {
      return { close: vi.fn(), restart: vi.fn(async () => undefined), start: vi.fn(async () => undefined) };
    }),
    getApproval: vi.fn(),
    getApprovalBinding: vi.fn(),
    getArtifact: vi.fn(),
    getConstraint: vi.fn(async () => constraint),
    getExecutionProfile: vi.fn(),
    getPatch: vi.fn(),
    getRun: vi.fn<GenerationApi["getRun"]>(async (runId) => ({
      attempt_no: null,
      events_url: `/api/v1/runs/${runId}/events`,
      failure_artifact_id: null,
      result_artifact_id: null,
      revision: 1,
      run_id: runId,
      status: "queued",
      status_url: `/api/v1/runs/${runId}`,
      terminal_cassette_artifact_id: null,
      view_schema_version: "run-view@1",
    })),
    getSnapshotDiff: vi.fn(),
    getSpec: vi.fn(async () => spec),
    listConstraints: vi.fn(async () => page([constraint], "read:constraints")),
    listExecutionProfiles: vi.fn(async () =>
      page([generationProfile, environmentProfile, exportProfile], "read:profiles"),
    ),
    listSpecs: vi.fn(async () => page([spec], "read:specs")),
    proposeGeneration: vi.fn<GenerationApi["proposeGeneration"]>(async () => ({
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:generation:1/events",
      run_id: "run:generation:1",
      status_url: "/api/v1/runs/run:generation:1",
    })),
    resolveExecutionOption: vi.fn(async () => resolvedOption),
    ...overrides,
  };
}

function renderPage(generationApi: GenerationApi, initialEntry = "/generation") {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <GenerationPage api={generationApi} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function fillExactGenerationForm(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("heading", { level: 1, name: "内容生成" });
  await user.selectOptions(
    await screen.findByRole("combobox", { name: "Base Spec / ref" }),
    spec.artifact.artifact_id,
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "Constraint snapshot" }),
    constraint.artifact.artifact_id,
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "Generation profile" }),
    "builtin.generation@1",
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "Environment profile" }),
    "builtin.aureus_env@1",
  );
  await user.click(screen.getByRole("checkbox", { name: "builtin.aureus_csv · builtin.aureus_csv@1" }));
  await user.type(screen.getByRole("textbox", { name: "Domain IDs" }), "domain:economy");
  await user.type(
    screen.getByRole("textbox", { name: "Authenticated authoring goal" }),
    "将前哨奖励限制在确定性经济约束内。",
  );
  await user.selectOptions(screen.getByRole("combobox", { name: "LLM execution mode" }), "replay");
  await user.type(screen.getByRole("textbox", { name: "Replay source Run" }), "run:cassette:source");
}

describe("GenerationPage", () => {
  it("restarts a catalog only after the operator confirms an expired cursor", async () => {
    const listSpecs = vi
      .fn<GenerationApi["listSpecs"]>()
      .mockResolvedValueOnce({ ...page([], "read:specs"), next_cursor: "cursor:expired" })
      .mockRejectedValueOnce(
        new CursorExpiredError(
          {
            code: "cursor_expired",
            conflict_set_id: null,
            detail: "Cursor expired.",
            earliest_cursor: null,
            instance: "/api/v1/specs",
            request_id: "request:generation-cursor",
            retry_after_s: null,
            run_id: null,
            status: 410,
            title: "Cursor expired",
            trace_id: null,
            type: "about:blank",
          },
          "cursor:expired",
        ),
      )
      .mockResolvedValueOnce(page([spec], "read:specs:restarted"));
    const user = userEvent.setup();
    renderPage(api({ listSpecs }));

    await user.click(await screen.findByRole("button", { name: "加载更多 Base Specs" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Base Specs 游标已过期");
    expect(listSpecs).toHaveBeenCalledTimes(2);

    await user.click(screen.getByRole("button", { name: "从首屏重读 Base Specs" }));
    expect(await screen.findByRole("option", { name: /artifact:spec:economy/ })).toBeVisible();
    expect(listSpecs).toHaveBeenLastCalledWith(null);
  });

  it("fails closed when RunView identity differs from the URL", async () => {
    const generationApi = api({
      getRun: vi.fn<GenerationApi["getRun"]>(async () => ({
        attempt_no: 1,
        events_url: "/api/v1/runs/run:other/events",
        failure_artifact_id: null,
        result_artifact_id: "artifact:unexpected",
        revision: 2,
        run_id: "run:other",
        status: "succeeded",
        status_url: "/api/v1/runs/run:other",
        terminal_cassette_artifact_id: null,
        view_schema_version: "run-view@1",
      })),
    });
    renderPage(generationApi, "/generation?run=run%3Arequested");

    expect(await screen.findByRole("heading", { name: "Run identity mismatch" })).toBeVisible();
    expect(generationApi.getArtifact).not.toHaveBeenCalled();
  });

  it("shows preliminary gate progress from SSE and makes cursor restart explicit", async () => {
    let callbacks: (GenerationEventStreamCallbacks & { runId: string }) | null = null;
    const restart = vi.fn(async () => undefined);
    const generationApi = api({
      createEventStream: vi.fn((value) => {
        callbacks = value;
        return { close: vi.fn(), restart, start: vi.fn(async () => undefined) };
      }),
      getRun: vi.fn<GenerationApi["getRun"]>(async () => ({
        attempt_no: 1,
        events_url: "/api/v1/runs/run:generation:1/events",
        failure_artifact_id: null,
        result_artifact_id: null,
        revision: 2,
        run_id: "run:generation:1",
        status: "running",
        status_url: "/api/v1/runs/run:generation:1",
        terminal_cassette_artifact_id: null,
        view_schema_version: "run-view@1",
      })),
    });
    const user = userEvent.setup();
    renderPage(generationApi, "/generation?run=run%3Ageneration%3A1");
    await waitFor(() => expect(callbacks).not.toBeNull());

    const event: RunEvent = {
      attempt_no: 1,
      data: {
        attempt_no: 1,
        completed_units: 1,
        data_schema_version: "attempt-progress@1",
        detail_artifact_id: null,
        phase_code: "generation.preliminary_gate",
        total_units: 1,
      },
      data_schema_version: "attempt-progress@1",
      event_schema_version: "run-event@1",
      event_type: "attempt.progress",
      occurred_at: "2026-07-20T00:00:00Z",
      run_id: "run:generation:1",
      seq: 3,
      trace_id: null,
    };
    act(() => {
      callbacks?.onEvent(event, "3");
      callbacks?.onEvent(event, "3");
      callbacks?.onStateChange({ earliestCursor: "2", status: "expired" });
    });

    expect(await screen.findByRole("heading", { name: "Preliminary gate" })).toBeVisible();
    expect(screen.getByText("attempt.progress · 2026-07-20T00:00:00Z")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "从最早保留事件重新开始" }));
    expect(restart).toHaveBeenCalledOnce();
  });

  it("loads later catalog pages without changing the frozen read snapshot", async () => {
    const firstSpec = {
      ...spec,
      artifact: { ...spec.artifact, artifact_id: "artifact:spec:first" },
      ref_name: "refs/specs/first",
      ref_value: { artifact_id: "artifact:spec:first", revision: 1 },
      snapshot_id: "snapshot:first",
    };
    const listSpecs = vi.fn<GenerationApi["listSpecs"]>(async (cursor) =>
      cursor === null
        ? { ...page([firstSpec], "read:specs"), next_cursor: "cursor:specs:2" }
        : page([spec], "read:specs"),
    );
    const generationApi = api({ listSpecs });
    const user = userEvent.setup();
    renderPage(generationApi);

    await user.click(await screen.findByRole("button", { name: "加载更多 Base Specs" }));

    expect(await screen.findByRole("option", { name: /artifact:spec:economy/ })).toBeVisible();
    expect(listSpecs).toHaveBeenNthCalledWith(1, null);
    expect(listSpecs).toHaveBeenNthCalledWith(2, "cursor:specs:2");
  });

  it("requires exact catalog selections and resolves a complete Journey A request without hidden defaults", async () => {
    const generationApi = api();
    const user = userEvent.setup();
    renderPage(generationApi);

    expect(await screen.findByRole("button", { name: "开始生成" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Base Spec / ref" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "Generation profile" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "Environment profile" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "LLM execution mode" })).toHaveValue("");

    await fillExactGenerationForm(user);
    await user.click(screen.getByRole("button", { name: "开始生成" }));

    await waitFor(() => expect(generationApi.proposeGeneration).toHaveBeenCalledOnce());
    expect(generationApi.resolveExecutionOption).toHaveBeenCalledWith({
      llm_execution_mode: "replay",
      prospective_request: {
        base_snapshot_artifact_id: spec.artifact.artifact_id,
        candidate_export_profiles: [exportProfile.profile],
        cassette_artifact_id: null,
        constraint_snapshot_artifact_id: constraint.artifact.artifact_id,
        domain_scope: domainScope,
        execution_version_plan: null,
        findings: [],
        generation_policy: generationProfile.profile,
        llm_execution_mode: "replay",
        objective_goal_text: "将前哨奖励限制在确定性经济约束内。",
        request_schema_version: "generation-propose-request@1",
        target: { expected_ref: spec.ref_value, ref_name: spec.ref_name },
      },
      replay_source_run_id: "run:cassette:source",
      request_schema_version: "execution-option-resolve-request@1",
      resource_operation_id: "propose_generation_api_v1_generation_propose_post",
      run_kind: { kind: "generation.propose", version: 1 },
    });
    expect(generationApi.proposeGeneration).toHaveBeenCalledWith(
      {
        base_snapshot_artifact_id: spec.artifact.artifact_id,
        candidate_export_profiles: [exportProfile.profile],
        cassette_artifact_id: resolvedOption.cassette_artifact_id,
        constraint_snapshot_artifact_id: constraint.artifact.artifact_id,
        domain_scope: domainScope,
        execution_version_plan: executionPlan,
        findings: [],
        generation_policy: generationProfile.profile,
        llm_execution_mode: "replay",
        objective_goal_text: "将前哨奖励限制在确定性经济约束内。",
        request_schema_version: "generation-propose-request@1",
        target: { expected_ref: spec.ref_value, ref_name: spec.ref_name },
      },
      { idempotencyKey: expect.any(String) },
    );
    expect(await screen.findByRole("heading", { name: "运行 run:generation:1" })).toBeVisible();
  });

  it("retries an unknown generation outcome with the frozen resolved body and same intent", async () => {
    const accepted = {
      accepted_schema_version: "run-accepted@1" as const,
      events_url: "/api/v1/runs/run:generation:1/events",
      run_id: "run:generation:1",
      status_url: "/api/v1/runs/run:generation:1",
    };
    const proposeGeneration = vi
      .fn<GenerationApi["proposeGeneration"]>()
      .mockRejectedValueOnce(new TypeError("transport result unknown"))
      .mockResolvedValueOnce(accepted);
    const generationApi = api({ proposeGeneration });
    const user = userEvent.setup();
    renderPage(generationApi);

    await fillExactGenerationForm(user);
    await user.click(screen.getByRole("button", { name: "开始生成" }));
    expect(await screen.findByRole("heading", { name: "生成结果未知" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "以同一 intent 重试" }));

    await waitFor(() => expect(proposeGeneration).toHaveBeenCalledTimes(2));
    expect(proposeGeneration.mock.calls[1]).toEqual(proposeGeneration.mock.calls[0]);
    expect(generationApi.resolveExecutionOption).toHaveBeenCalledOnce();
  });
});
