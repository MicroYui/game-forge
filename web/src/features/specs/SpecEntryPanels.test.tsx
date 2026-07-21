import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ReauthenticationRequiredError } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError, type SafeProblem } from "../../api/problem";
import { createQueryClient } from "../../api/query-client";
import { SpecEntryPanels, type SpecEntryPanelsApi } from "./SpecEntryPanels";
import type {
  ConstraintProposalReadView,
  ExecutionOptionView,
  ExecutionProfilePage,
  RunAccepted,
  SpecView,
} from "./api";

const domainScope: components["schemas"]["DomainScope"] = {
  domain_ids: ["domain:economy"],
};

function artifact(
  artifactId: string,
  kind: components["schemas"]["ArtifactSummaryV1"]["kind"],
): components["schemas"]["ArtifactSummaryV1"] {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-19T08:00:00Z",
    domain_scope: domainScope,
    kind,
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [],
    payload_hash: "a".repeat(64),
    payload_schema_id: `${kind}@1`,
    summary_schema_version: "artifact-summary@1",
    version_tuple: { tool_version: "workspace@1" },
  };
}

const extractionProfile: components["schemas"]["ExecutionProfileViewV1"] = {
  compatible_run_kinds: [{ kind: "constraint_proposal.propose", version: 1 }],
  display_name: "Economy constraint extraction",
  domain_scope: domainScope,
  env_contract_version: null,
  input_schema_ids: ["source@1"],
  output_schema_ids: ["constraint-proposal@1"],
  profile: { profile_id: "builtin.constraint_extraction", version: 4 },
  profile_kind: "constraint_extraction",
  profile_payload_hash: "b".repeat(64),
  required_capabilities: [],
  status: "active",
  stochastic: true,
  target_environment_profile: null,
};

const profilePage: ExecutionProfilePage = {
  expires_at: "2026-07-19T09:00:00Z",
  items: [
    extractionProfile,
    { ...extractionProfile, profile: { profile_id: "disabled.extractor", version: 1 }, status: "disabled" },
    {
      ...extractionProfile,
      profile: { profile_id: "wrong.kind", version: 1 },
      profile_kind: "generation",
    },
  ],
  next_cursor: null,
  page_schema_version: "page@1",
  read_snapshot_id: "read:profiles",
};

const executionPlan: components["schemas"]["ExecutionVersionPlanV1"] = {
  agent_graph_version: "constraint-extraction@4",
  model_catalog_digest: "c".repeat(64),
  model_catalog_version: 3,
  nodes: [
    {
      agent_node_id: "extract",
      allowed_model_snapshots: ["openai/gpt-5.6-sol/m4@1"],
      prompt_version: "constraint-extract@4",
      tool_version: "typed-proposal@1",
    },
  ],
  plan_digest: "d".repeat(64),
  plan_schema_version: "execution-version-plan@1",
  routing_policy_digest: "e".repeat(64),
  routing_policy_version: 2,
};

const resolvedOption: ExecutionOptionView = {
  cassette_artifact_id: "artifact:cassette:resolved",
  domain_scope: domainScope,
  execution_version_plan: executionPlan,
  llm_execution_mode: "record",
  option_id: "option:constraint:1",
  option_schema_version: "execution-option@1",
  prospective_request_hash: "f".repeat(64),
  resolved_profile_binding_digests: ["1".repeat(64)],
  resolved_request_hash: "2".repeat(64),
  resource_operation_id: "propose_constraint_api_v1_constraint_proposals_propose_post",
  run_kind: { kind: "constraint_proposal.propose", version: 1 },
  source_run_id: null,
};

const proposalResult: ConstraintProposalReadView = {
  approval_status: "draft",
  artifact: artifact("artifact:proposal:human", "constraint_proposal"),
  proposal: {
    base_constraint_snapshot_id: "artifact:constraint:base",
    constraints: [],
    domain_scope: domainScope,
    dsl_grammar_version: "dsl@1",
    produced_by: "human",
    producer_run_id: null,
    proposal_schema_version: "constraint-proposal@1",
    rationale: "Keep gold inflation bounded.",
    revision: 1,
    source_bindings: [],
    supersedes_artifact_id: null,
  },
  view_schema_version: "constraint-proposal-read-view@1",
  workflow_revision: 1,
};

const specResult: SpecView = {
  artifact: artifact("artifact:spec:uploaded", "ir_snapshot"),
  ref_name: "refs/specs/economy",
  ref_value: { artifact_id: "artifact:spec:uploaded", revision: 1 },
  schema_registry_version: "registry@3",
  snapshot_id: "snapshot:uploaded",
  view_schema_version: "spec-view@1",
};

const acceptedRun: RunAccepted = {
  accepted_schema_version: "run-accepted@1",
  events_url: "/api/v1/runs/run%3Aconstraint%3A1/events",
  run_id: "run:constraint:1",
  status_url: "/api/v1/runs/run%3Aconstraint%3A1",
};

function api(overrides: Partial<SpecEntryPanelsApi> = {}): SpecEntryPanelsApi {
  return {
    draftConstraint: vi.fn(async () => proposalResult),
    listExecutionProfiles: vi.fn(async () => profilePage),
    proposeConstraint: vi.fn(async () => acceptedRun),
    resolveExecutionOption: vi.fn(async () => resolvedOption),
    uploadSpec: vi.fn(async () => specResult),
    ...overrides,
  };
}

function renderPanels(entryApi: SpecEntryPanelsApi) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <SpecEntryPanels api={entryApi} />
    </QueryClientProvider>,
  );
}

function deferred<T>() {
  let resolvePromise!: (value: T) => void;
  const promise = new Promise<T>((resolveValue) => {
    resolvePromise = resolveValue;
  });
  return { promise, resolve: resolvePromise };
}

async function fillHumanDraft(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Human ref name"), "refs/constraints/economy");
  await user.click(screen.getByLabelText("No current ref"));
  await user.type(
    screen.getByLabelText("Human base ConstraintSnapshot Artifact ID"),
    "artifact:constraint:base",
  );
  await user.type(screen.getByLabelText("Human domain IDs"), "domain:economy, domain:rewards");
  await user.type(screen.getByLabelText("Human DSL grammar"), "dsl@1");
  await user.type(screen.getByLabelText("Human source Artifact IDs"), "artifact:source:design");
  await user.type(screen.getByLabelText("Human rationale"), "Keep gold inflation bounded.");
  fireEvent.change(screen.getByLabelText("Typed constraints JSON"), {
    target: {
      value: JSON.stringify([
        {
          assert: "reward_gold <= 75",
          dsl_grammar_version: "dsl@1",
          id: "constraint:reward-cap",
          kind: "numeric",
          oracle: "deterministic",
          severity: "major",
        },
      ]),
    },
  });
}

async function fillAgentDraft(
  user: ReturnType<typeof userEvent.setup>,
  mode: "live" | "record" | "replay" = "record",
) {
  const profileSelect = await screen.findByLabelText("Agent execution profile");
  await user.type(screen.getByLabelText("Agent source Artifact IDs"), "artifact:source:design");
  await user.type(
    screen.getByLabelText("Agent base ConstraintSnapshot Artifact ID"),
    "artifact:constraint:base",
  );
  await user.type(screen.getByLabelText("Agent domain IDs"), "domain:economy");
  await user.type(screen.getByLabelText("Agent DSL grammar"), "dsl@1");
  await user.type(screen.getByLabelText("Agent authoring goal"), "Extract a deterministic reward cap.");
  await user.selectOptions(profileSelect, "builtin.constraint_extraction@4");
  await user.selectOptions(screen.getByLabelText("LLM execution mode"), mode);
  if (mode === "replay") {
    await user.type(screen.getByLabelText("Replay source Run"), "run:replay:source");
  }
}

describe("SpecEntryPanels", () => {
  it("requires an explicit active extraction profile, resolves a prospective request, and copies the exact option into one Agent create", async () => {
    const entryApi = api();
    const user = userEvent.setup();
    renderPanels(entryApi);

    const profileSelect = await screen.findByLabelText("Agent execution profile");
    expect(profileSelect).toHaveValue("");
    expect(screen.queryByRole("option", { name: /disabled\.extractor/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /wrong\.kind/ })).not.toBeInTheDocument();

    await fillAgentDraft(user);
    await user.click(screen.getByRole("button", { name: "生成 Agent 候选" }));

    await waitFor(() => expect(entryApi.resolveExecutionOption).toHaveBeenCalledTimes(1));
    const resolveRequest = vi.mocked(entryApi.resolveExecutionOption).mock.calls[0][0];
    expect(resolveRequest).toEqual({
      llm_execution_mode: "record",
      prospective_request: {
        authoring_goal_text: "Extract a deterministic reward cap.",
        base_constraint_snapshot_artifact_id: "artifact:constraint:base",
        cassette_artifact_id: null,
        domain_scope: { domain_ids: ["domain:economy"] },
        dsl_grammar_version: "dsl@1",
        execution_version_plan: null,
        extraction_policy: { profile_id: "builtin.constraint_extraction", version: 4 },
        llm_execution_mode: "record",
        request_schema_version: "constraint-propose-request@1",
        source_artifact_ids: ["artifact:source:design"],
      },
      replay_source_run_id: null,
      request_schema_version: "execution-option-resolve-request@1",
      resource_operation_id: "propose_constraint_api_v1_constraint_proposals_propose_post",
      run_kind: { kind: "constraint_proposal.propose", version: 1 },
    });
    await waitFor(() => expect(entryApi.proposeConstraint).toHaveBeenCalledTimes(1));
    const [request, intent] = vi.mocked(entryApi.proposeConstraint).mock.calls[0];
    expect(request.execution_version_plan).toBe(executionPlan);
    expect(request.cassette_artifact_id).toBe("artifact:cassette:resolved");
    expect(Object.isFrozen(intent)).toBe(true);
    expect(screen.getByRole("link", { name: "打开 Run run:constraint:1" })).toHaveAttribute(
      "href",
      "/runs/run%3Aconstraint%3A1",
    );
    expect(screen.queryByText(/\/events/)).not.toBeInTheDocument();
  });

  it("submits a Human typed draft with an explicit null expected_ref and links the exact proposal", async () => {
    const entryApi = api();
    const user = userEvent.setup();
    renderPanels(entryApi);
    await screen.findByLabelText("Agent execution profile");
    expect(document.getElementById("human-constraints-hint")).not.toHaveAttribute("role");
    await fillHumanDraft(user);
    await user.click(screen.getByRole("button", { name: "创建 Human typed draft" }));

    await waitFor(() => expect(entryApi.draftConstraint).toHaveBeenCalledTimes(1));
    const [request, intent] = vi.mocked(entryApi.draftConstraint).mock.calls[0];
    expect(request).toEqual({
      base_constraint_snapshot_artifact_id: "artifact:constraint:base",
      constraints: [
        {
          assert: "reward_gold <= 75",
          dsl_grammar_version: "dsl@1",
          id: "constraint:reward-cap",
          kind: "numeric",
          oracle: "deterministic",
          severity: "major",
        },
      ],
      domain_scope: { domain_ids: ["domain:economy", "domain:rewards"] },
      dsl_grammar_version: "dsl@1",
      expected_ref: null,
      rationale: "Keep gold inflation bounded.",
      ref_name: "refs/constraints/economy",
      request_schema_version: "human-constraint-draft-request@1",
      source_artifact_ids: ["artifact:source:design"],
    });
    expect(Object.isFrozen(intent)).toBe(true);
    expect(screen.getByRole("link", { name: "打开 proposal artifact:proposal:human" })).toHaveAttribute(
      "href",
      "/constraint-proposals/artifact%3Aproposal%3Ahuman",
    );
  });

  it("uploads a schema-bound Human spec and links the exact Spec", async () => {
    const entryApi = api();
    const user = userEvent.setup();
    renderPanels(entryApi);
    await screen.findByLabelText("Agent execution profile");
    expect(document.getElementById("spec-content-hint")).not.toHaveAttribute("role");

    await user.type(screen.getByLabelText("Schema registry version"), "registry@3");
    await user.type(screen.getByLabelText("Meta schema version"), "meta@2");
    await user.type(screen.getByLabelText("Spec ref name"), "refs/specs/economy");
    await user.click(screen.getByLabelText("Spec has no current ref"));
    await user.type(screen.getByLabelText("Spec domain IDs"), "domain:economy");
    fireEvent.change(screen.getByLabelText("Spec content JSON"), {
      target: { value: JSON.stringify({ economy: { reward_cap: 75 } }) },
    });
    await user.click(screen.getByRole("button", { name: "上传 Human spec" }));

    await waitFor(() => expect(entryApi.uploadSpec).toHaveBeenCalledTimes(1));
    expect(vi.mocked(entryApi.uploadSpec).mock.calls[0][0]).toEqual({
      content_payload: { economy: { reward_cap: 75 } },
      domain_scope: { domain_ids: ["domain:economy"] },
      expected_ref: null,
      meta_schema_version: "meta@2",
      ref_name: "refs/specs/economy",
      request_schema_version: "human-spec-upload-request@1",
      schema_registry_version: "registry@3",
    });
    expect(screen.getByRole("link", { name: "打开 Spec artifact:spec:uploaded" })).toHaveAttribute(
      "href",
      "/specs/artifact%3Aspec%3Auploaded",
    );
  });

  it("does not retry an unknown create automatically and reuses the same frozen intent after explicit retry", async () => {
    const draftConstraint = vi
      .fn<SpecEntryPanelsApi["draftConstraint"]>()
      .mockRejectedValueOnce(new Error("connection reset after send"))
      .mockResolvedValueOnce(proposalResult);
    const entryApi = api({ draftConstraint });
    const user = userEvent.setup();
    renderPanels(entryApi);
    await screen.findByLabelText("Agent execution profile");
    await fillHumanDraft(user);
    await user.click(screen.getByRole("button", { name: "创建 Human typed draft" }));

    expect(await screen.findByRole("heading", { name: "创建结果未知" })).toBeVisible();
    expect(draftConstraint).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "创建 Human typed draft" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "使用同一 intent 明确重试" }));

    await waitFor(() => expect(draftConstraint).toHaveBeenCalledTimes(2));
    expect(draftConstraint.mock.calls[1][1]).toBe(draftConstraint.mock.calls[0][1]);
    expect(await screen.findByRole("link", { name: /打开 proposal/ })).toBeVisible();
  });

  it("retries an unknown Agent create with the first resolved body and the same frozen intent", async () => {
    const proposeConstraint = vi
      .fn<SpecEntryPanelsApi["proposeConstraint"]>()
      .mockRejectedValueOnce(new Error("connection reset after send"))
      .mockResolvedValueOnce(acceptedRun);
    const entryApi = api({ proposeConstraint });
    const user = userEvent.setup();
    renderPanels(entryApi);
    await fillAgentDraft(user);
    await user.click(screen.getByRole("button", { name: "生成 Agent 候选" }));

    expect(await screen.findByRole("heading", { name: "创建结果未知" })).toBeVisible();
    expect(entryApi.resolveExecutionOption).toHaveBeenCalledTimes(1);
    expect(proposeConstraint).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "生成 Agent 候选" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "使用同一 intent 明确重试" }));

    await waitFor(() => expect(proposeConstraint).toHaveBeenCalledTimes(2));
    expect(entryApi.resolveExecutionOption).toHaveBeenCalledTimes(1);
    expect(proposeConstraint.mock.calls[1][0]).toBe(proposeConstraint.mock.calls[0][0]);
    expect(proposeConstraint.mock.calls[1][1]).toBe(proposeConstraint.mock.calls[0][1]);
  });

  it.each([
    {
      name: "operation",
      option: {
        ...resolvedOption,
        resource_operation_id: "propose_generation_api_v1_generation_propose_post" as const,
      },
    },
    {
      name: "run kind",
      option: { ...resolvedOption, run_kind: { kind: "generation.propose", version: 1 } },
    },
    {
      name: "execution mode",
      option: { ...resolvedOption, llm_execution_mode: "live" as const },
    },
  ])("fails closed when the resolver changes the requested $name", async ({ option }) => {
    const entryApi = api({ resolveExecutionOption: vi.fn(async () => option) });
    const user = userEvent.setup();
    renderPanels(entryApi);
    await fillAgentDraft(user);
    await user.click(screen.getByRole("button", { name: "生成 Agent 候选" }));

    expect(await screen.findByRole("heading", { name: "创建结果未知" })).toBeVisible();
    expect(entryApi.proposeConstraint).not.toHaveBeenCalled();
  });

  it("fails closed when a replay option omits its cassette Artifact", async () => {
    const replayOption: ExecutionOptionView = {
      ...resolvedOption,
      cassette_artifact_id: null,
      llm_execution_mode: "replay",
      source_run_id: "run:replay:source",
    };
    const entryApi = api({
      resolveExecutionOption: vi.fn(async () => replayOption),
    });
    const user = userEvent.setup();
    renderPanels(entryApi);
    await fillAgentDraft(user, "replay");
    await user.click(screen.getByRole("button", { name: "生成 Agent 候选" }));

    expect(await screen.findByRole("heading", { name: "创建结果未知" })).toBeVisible();
    expect(entryApi.resolveExecutionOption).toHaveBeenCalledWith(
      expect.objectContaining({ replay_source_run_id: "run:replay:source" }),
    );
    expect(entryApi.proposeConstraint).not.toHaveBeenCalled();
  });

  it("renders profile loading and empty states without choosing a fallback", async () => {
    const pending = deferred<ExecutionProfilePage>();
    const entryApi = api({ listExecutionProfiles: vi.fn(() => pending.promise) });
    renderPanels(entryApi);

    expect(screen.getByRole("heading", { name: "正在读取 Agent profiles" })).toBeVisible();
    pending.resolve({ ...profilePage, items: [] });

    expect(await screen.findByRole("heading", { name: "没有可用的 Agent profile" })).toBeVisible();
    expect(screen.getByLabelText("Agent execution profile")).toHaveValue("");
  });

  it("requires an explicit restart after the profile catalog cursor expires", async () => {
    const listExecutionProfiles = vi
      .fn<SpecEntryPanelsApi["listExecutionProfiles"]>()
      .mockResolvedValueOnce({ ...profilePage, items: [], next_cursor: "opaque.profile+/=" })
      .mockRejectedValueOnce(
        new CursorExpiredError(
          {
            code: "cursor_expired",
            conflict_set_id: null,
            detail: "Cursor expired.",
            earliest_cursor: null,
            instance: "/api/v1/execution-profiles",
            request_id: "request:profile-cursor:1",
            retry_after_s: null,
            run_id: null,
            status: 410,
            title: "Cursor expired",
            trace_id: null,
            type: "about:blank",
          },
          "opaque.profile+/=",
        ),
      )
      .mockResolvedValueOnce(profilePage);
    const entryApi = api({ listExecutionProfiles });
    const user = userEvent.setup();
    renderPanels(entryApi);

    await user.click(await screen.findByRole("button", { name: "加载更多 Agent profiles" }));
    expect(await screen.findByRole("heading", { name: "Profile 游标已过期" })).toBeVisible();
    expect(listExecutionProfiles).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("Agent execution profile")).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "从 profile 目录首页重新开始" }));
    expect(await screen.findByRole("option", { name: /builtin\.constraint_extraction@4/ })).toBeVisible();
    expect(listExecutionProfiles).toHaveBeenLastCalledWith(null);
    expect(screen.getByLabelText("Agent execution profile")).toHaveValue("");
  });

  it.each([
    {
      error: new ReauthenticationRequiredError(),
      expected: "需要重新登录",
      kind: "reauth",
    },
    {
      error: new ApiProblemError({
        code: "domain_forbidden",
        conflict_set_id: null,
        detail: "当前身份无权写入该 domain。",
        earliest_cursor: null,
        instance: "/api/v1/constraint-proposals",
        request_id: "request:entry:1",
        retry_after_s: null,
        run_id: null,
        status: 403,
        title: "Domain forbidden",
        trace_id: null,
        type: "about:blank",
      } satisfies SafeProblem),
      expected: "Domain forbidden",
      kind: "problem",
    },
  ])("renders $kind create failures without raw exception leakage", async ({ error, expected, kind }) => {
    const entryApi = api({ draftConstraint: vi.fn(async () => Promise.reject(error)) });
    const user = userEvent.setup();
    renderPanels(entryApi);
    await screen.findByLabelText("Agent execution profile");
    await fillHumanDraft(user);
    await user.click(screen.getByRole("button", { name: "创建 Human typed draft" }));

    expect(await screen.findByRole("heading", { name: expected })).toBeVisible();
    if (kind === "reauth") {
      expect(screen.getByRole("link", { name: "重新登录" })).toHaveAttribute("href", "/login");
    }
  });
});
