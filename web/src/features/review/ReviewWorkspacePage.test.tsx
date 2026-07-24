import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { CursorExpiredError } from "../../api/pagination";
import { createQueryClient } from "../../api/query-client";
import type { components } from "../../api/generated/openapi";
import type {
  ConstraintSnapshotView,
  ExecutionOptionView,
  ExecutionProfilePage,
  ReviewApi,
  ReviewArtifactView,
  ReviewPage,
  RunSubmissionRequest,
} from "./api";
import { ReviewWorkspacePage } from "./ReviewWorkspacePage";

type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type ExecutionProfile = ExecutionProfilePage["items"][number];

const generationContext = {
  constraint: "artifact:constraint:generation",
  snapshot: "artifact:preview:generation",
  sourceRun: "run:generation:7",
};

function executionProfile(
  profileId: string,
  profileKind: ExecutionProfile["profile_kind"],
  status: ExecutionProfile["status"] = "active",
  stochastic = false,
): ExecutionProfile {
  return {
    compatible_run_kinds: [{ kind: "review.run", version: 1 }],
    display_name: profileId,
    domain_scope: { domain_ids: ["domain:narrative"] },
    env_contract_version: null,
    input_schema_ids: ["ir-core@1"],
    output_schema_ids: ["review@1"],
    profile: { profile_id: profileId, version: 1 },
    profile_kind: profileKind,
    profile_payload_hash: "a".repeat(64),
    required_capabilities: [],
    status,
    stochastic,
    target_environment_profile: null,
  };
}

const reviewProfile = executionProfile("builtin.review", "review");
const checkerProfile = executionProfile("builtin.checker", "checker");
const simulationProfile = executionProfile("builtin.simulation", "simulation");
const triageProfile = executionProfile("builtin.llm_triage", "llm_triage");
const stochasticTriageProfile = executionProfile(
  "builtin.llm_triage.stochastic",
  "llm_triage",
  "active",
  true,
);

const executionPlan: components["schemas"]["ExecutionVersionPlanV1"] = {
  agent_graph_version: "review-triage@1",
  model_catalog_digest: "b".repeat(64),
  model_catalog_version: 1,
  nodes: [
    {
      agent_node_id: "review-triage",
      allowed_model_snapshots: ["openai/gpt-5.6-sol/m4@1"],
      prompt_version: "review-triage@1",
      tool_version: "review-triage@1",
    },
  ],
  plan_digest: "c".repeat(64),
  plan_schema_version: "execution-version-plan@1",
  routing_policy_digest: "d".repeat(64),
  routing_policy_version: 1,
};

const executionOption: ExecutionOptionView = {
  cassette_artifact_id: "artifact:cassette:review",
  domain_scope: { domain_ids: ["domain:narrative"] },
  execution_version_plan: executionPlan,
  llm_execution_mode: "replay",
  option_id: "option:review:1",
  option_schema_version: "execution-option@1",
  prospective_request_hash: "e".repeat(64),
  resolved_profile_binding_digests: ["f".repeat(64)],
  resolved_request_hash: "1".repeat(64),
  resource_operation_id: "submit_run_api_v1_runs_post",
  run_kind: { kind: "review.run", version: 1 },
  source_run_id: "run:review:record-source",
};

function constraintView(
  constraints: ConstraintSnapshotView["constraints"] = [],
  artifactId = generationContext.constraint,
): ConstraintSnapshotView {
  return {
    artifact: {
      artifact_id: artifactId,
      created_at: "2026-07-20T02:00:00Z",
      domain_scope: { domain_ids: ["domain:narrative"] },
      kind: "constraint_snapshot",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: [],
      payload_hash: "9".repeat(64),
      payload_schema_id: "constraint-snapshot@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: { constraint_snapshot_id: "constraint:exact" },
    },
    constraints,
    dsl_grammar_version: "dsl@1",
    view_schema_version: "constraint-snapshot-view@1",
  };
}

function artifact(artifactId: string, toolVersion: string, parentArtifactIds: string[]): ArtifactSummary {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-20T02:00:00Z",
    domain_scope: { domain_ids: ["domain:narrative"] },
    kind: "review_report",
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [...parentArtifactIds].sort(),
    payload_hash: artifactId.endsWith("1") ? "1".repeat(64) : "2".repeat(64),
    payload_schema_id: "review@1",
    summary_schema_version: "artifact-summary@1",
    version_tuple: {
      ir_snapshot_id: "snapshot:shared",
      tool_version: toolVersion,
    },
  };
}

function review(
  artifactId: string,
  toolVersion: string,
  parentArtifactIds: string[],
  deterministicCount: number,
): ReviewArtifactView {
  return {
    artifact: artifact(artifactId, toolVersion, parentArtifactIds),
    report: {
      by_defect_class:
        deterministicCount === 0
          ? []
          : [{ count: deterministicCount, defect_class: "quest_dead_end", severity: "critical" }],
      deterministic_findings: Array.from({ length: deterministicCount }, (_, index) => ({
        defect_class: "quest_dead_end",
        finding_schema_version: "finding@1",
        id: `${artifactId}:finding:${index}`,
        message: "死路",
        oracle_type: "deterministic",
        producer_id: "checker:graph",
        producer_run_id: "run:review:list",
        severity: "critical",
        snapshot_id: "snapshot:shared",
        source: "checker",
        status: "confirmed",
      })),
      llm_assisted_findings: [],
      review_schema_version: "review@1",
      simulation_findings: [],
      snapshot_id: "snapshot:shared",
      unproven_findings: [],
    },
    view_schema_version: "review-artifact-view@1",
  };
}

function page<T>(items: T[], nextCursor: string | null, snapshot = "read:reviews") {
  return {
    expires_at: "2026-07-20T03:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "page@1" as const,
    read_snapshot_id: snapshot,
  };
}

function reviewApi(overrides: Partial<ReviewApi>): ReviewApi {
  return {
    getConstraint: vi.fn().mockResolvedValue(constraintView()),
    getFinding: vi.fn(),
    getReview: vi.fn(),
    getReviewProducerBinding: vi.fn(),
    getSpec: vi.fn(),
    listReviewProfiles: vi.fn(),
    listReplaySourceRuns: vi.fn(async () =>
      page(
        [
          {
            attempt_no: 1,
            completedAt: "2026-07-23T03:47:50Z",
            events_url: "/api/v1/runs/run:review:record-source/events",
            failure_artifact_id: null,
            outcomeCode: "review_completed",
            result_artifact_id: "artifact:result:review-source",
            revision: 3,
            runKind: { kind: "review.run", version: 1 },
            run_id: "run:review:record-source",
            status: "succeeded" as const,
            status_url: "/api/v1/runs/run:review:record-source",
            terminal_cassette_artifact_id: "artifact:cassette:review-source",
            view_schema_version: "run-view@1" as const,
          },
        ],
        null,
        "read:review-source-runs",
      ),
    ),
    listLineage: vi.fn(),
    listReviews: vi.fn(),
    listRunFindingLinks: vi.fn(),
    resolveExecutionOption: vi.fn(),
    submitRun: vi.fn(),
    ...overrides,
  } as ReviewApi;
}

function renderWorkspace(api: ReviewApi, url = "/reviews") {
  const client = createQueryClient();
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[url]}>
          <ReviewWorkspacePage api={api} />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

async function selectReviewReplay(user: ReturnType<typeof userEvent.setup>) {
  await user.selectOptions(
    await screen.findByRole("combobox", { name: "Review profile" }),
    "builtin.review@1",
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "LLM triage profile" }),
    "builtin.llm_triage@1",
  );
  await user.selectOptions(screen.getByRole("combobox", { name: "LLM execution mode" }), "replay");
  await user.selectOptions(
    screen.getByRole("combobox", { name: "Replay source Run" }),
    "run:review:record-source",
  );
}

async function selectRequiredReviewReplay(user: ReturnType<typeof userEvent.setup>) {
  await selectReviewReplay(user);
  await user.click(screen.getByRole("checkbox", { name: "builtin.checker · builtin.checker@1" }));
}

describe("Review workspace", () => {
  it("keeps the ordinary immutable ledger free of candidate launch controls", async () => {
    const listReviewProfiles = vi.fn();
    renderWorkspace(
      reviewApi({
        listReviewProfiles,
        listReviews: vi.fn().mockResolvedValue(page([], null)),
      }),
    );

    expect(await screen.findByRole("heading", { level: 1, name: "审查报告" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "启动候选 Review" })).not.toBeInTheDocument();
    expect(listReviewProfiles).not.toHaveBeenCalled();
  });

  it("does not expose the launch card without an exact constraint", async () => {
    const listReviewProfiles = vi.fn();
    renderWorkspace(
      reviewApi({
        listReviewProfiles,
        listReviews: vi.fn().mockResolvedValue(page([], null)),
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}`,
    );

    expect(await screen.findByRole("heading", { level: 1, name: "审查报告" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "启动候选 Review" })).not.toBeInTheDocument();
    expect(listReviewProfiles).not.toHaveBeenCalled();
  });

  it("launches an exact human Patch candidate without fabricating a source Run link", async () => {
    const user = userEvent.setup();
    const resolveExecutionOption = vi.fn().mockResolvedValue({
      ...executionOption,
      cassette_artifact_id: null,
      llm_execution_mode: "record",
      source_run_id: null,
    });
    const submitRun = vi.fn().mockResolvedValue({
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:review:human/events",
      run_id: "run:review:human",
      status_url: "/api/v1/runs/run:review:human",
    });
    renderWorkspace(
      reviewApi({
        listReviewProfiles: vi
          .fn()
          .mockResolvedValue(
            page([reviewProfile, checkerProfile, triageProfile], null, "read:review-profiles"),
          ),
        listReviews: vi.fn().mockResolvedValue(page([], null)),
        resolveExecutionOption,
        submitRun,
      }),
      `/reviews?snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    expect(await screen.findByRole("heading", { name: "启动候选 Review" })).toBeVisible();
    const ledger = screen.getByRole("complementary", { name: "Review input 账页" });
    const sourceRunEntry = within(ledger).getByText("导航来源 Run（非提交输入）").closest("div");
    expect(sourceRunEntry).not.toBeNull();
    expect(within(sourceRunEntry!).getByText("无")).toBeVisible();
    expect(within(sourceRunEntry!).queryByRole("link")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByRole("combobox", { name: "Review profile" }), "builtin.review@1");
    await user.click(screen.getByRole("checkbox", { name: "builtin.checker · builtin.checker@1" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "LLM triage profile" }),
      "builtin.llm_triage@1",
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "LLM execution mode" }), "record");
    await user.click(screen.getByRole("button", { name: "启动 Review" }));

    await waitFor(() => expect(submitRun).toHaveBeenCalledOnce());
    expect(resolveExecutionOption.mock.calls[0][0]).toMatchObject({
      prospective_request: {
        params: {
          constraint_snapshot_artifact_id: generationContext.constraint,
          snapshot_artifact_id: generationContext.snapshot,
        },
      },
      replay_source_run_id: null,
    });
  });

  it("resolves and submits one exact Review docket with optional source Run context", async () => {
    const user = userEvent.setup();
    const listReviewProfiles = vi
      .fn()
      .mockResolvedValue(
        page<ExecutionProfile>(
          [
            reviewProfile,
            checkerProfile,
            simulationProfile,
            triageProfile,
            executionProfile("disabled.review", "review", "disabled"),
          ],
          null,
          "read:review-profiles",
        ),
      );
    const resolveExecutionOption = vi.fn().mockResolvedValue(executionOption);
    const submitRun = vi.fn().mockResolvedValue({
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:review:accepted/events",
      run_id: "run:review:accepted",
      status_url: "/api/v1/runs/run:review:accepted",
    });
    renderWorkspace(
      reviewApi({
        listReviewProfiles,
        listReviews: vi.fn().mockResolvedValue(page([], null)),
        resolveExecutionOption,
        submitRun,
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    expect(await screen.findByRole("heading", { name: "启动候选 Review" })).toBeVisible();
    expect(screen.getAllByText(generationContext.snapshot)).toHaveLength(2);
    expect(screen.getAllByText(generationContext.constraint)).toHaveLength(2);
    expect(screen.getByText("Exact Review inputs")).toBeVisible();
    expect(screen.getByText("导航来源 Run（非提交输入）")).toBeVisible();
    expect(screen.queryByRole("option", { name: /disabled\.review/ })).not.toBeInTheDocument();

    await user.selectOptions(screen.getByRole("combobox", { name: "Review profile" }), "builtin.review@1");
    await user.click(screen.getByRole("checkbox", { name: "builtin.checker · builtin.checker@1" }));
    await user.click(screen.getByRole("checkbox", { name: "builtin.simulation · builtin.simulation@1" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "LLM triage profile" }),
      "builtin.llm_triage@1",
    );
    expect(screen.queryByRole("spinbutton", { name: "Seed" })).not.toBeInTheDocument();
    expect(screen.getByText("Seed 不适用")).toBeVisible();
    expect(screen.getByText(/当前所选 profiles 均为确定性执行/)).toBeVisible();
    await user.selectOptions(screen.getByRole("combobox", { name: "LLM execution mode" }), "replay");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Replay source Run" }),
      "run:review:record-source",
    );
    await user.click(screen.getByRole("button", { name: "启动 Review" }));

    await waitFor(() => expect(resolveExecutionOption).toHaveBeenCalledOnce());
    const resolverRequest = resolveExecutionOption.mock.calls[0][0];
    expect(resolverRequest).toEqual({
      llm_execution_mode: "replay",
      prospective_request: {
        cassette_artifact_id: null,
        execution_version_plan: null,
        llm_execution_mode: "replay",
        params: {
          checker_profiles: [{ profile_id: "builtin.checker", version: 1 }],
          constraint_snapshot_artifact_id: generationContext.constraint,
          llm_triage_policy: { profile_id: "builtin.llm_triage", version: 1 },
          review_profile: { profile_id: "builtin.review", version: 1 },
          schema_version: "review-run@1",
          selection: { entity_ids: [], mode: "full", relation_ids: [] },
          simulation_profiles: [{ profile_id: "builtin.simulation", version: 1 }],
          snapshot_artifact_id: generationContext.snapshot,
        },
        request_schema_version: "run-submission-request@1",
        seed: null,
      },
      replay_source_run_id: "run:review:record-source",
      request_schema_version: "execution-option-resolve-request@1",
      resource_operation_id: "submit_run_api_v1_runs_post",
      run_kind: { kind: "review.run", version: 1 },
    });
    await waitFor(() => expect(submitRun).toHaveBeenCalledOnce());
    const [submittedRequest, submittedIntent] = submitRun.mock.calls[0] as [
      RunSubmissionRequest,
      { idempotencyKey: string },
    ];
    expect(submittedRequest).toEqual({
      ...resolverRequest.prospective_request,
      cassette_artifact_id: executionOption.cassette_artifact_id,
      execution_version_plan: executionPlan,
    });
    expect(submittedIntent.idempotencyKey).toEqual(expect.any(String));
    expect(await screen.findByRole("link", { name: "打开 Run run:review:accepted" })).toHaveAttribute(
      "href",
      "/runs/run%3Areview%3Aaccepted",
    );
  });

  it("requires a valid non-negative integer seed when any selected profile is stochastic", async () => {
    const user = userEvent.setup();
    const resolveExecutionOption = vi.fn().mockResolvedValue(executionOption);
    const submitRun = vi.fn().mockResolvedValue({
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:review:stochastic/events",
      run_id: "run:review:stochastic",
      status_url: "/api/v1/runs/run:review:stochastic",
    });
    renderWorkspace(
      reviewApi({
        listReviewProfiles: vi
          .fn()
          .mockResolvedValue(
            page([reviewProfile, checkerProfile, stochasticTriageProfile], null, "read:review-profiles"),
          ),
        listReviews: vi.fn().mockResolvedValue(page([], null)),
        resolveExecutionOption,
        submitRun,
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Review profile" }),
      "builtin.review@1",
    );
    await user.click(screen.getByRole("checkbox", { name: "builtin.checker · builtin.checker@1" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "LLM triage profile" }),
      "builtin.llm_triage.stochastic@1",
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "LLM execution mode" }), "replay");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Replay source Run" }),
      "run:review:record-source",
    );

    const seedInput = screen.getByRole("spinbutton", { name: "Seed" });
    expect(seedInput).toBeRequired();
    expect(screen.getByText(/所选 profile 包含随机执行/)).toBeVisible();
    await user.clear(seedInput);
    expect(screen.getByRole("button", { name: "启动 Review" })).toBeDisabled();
    await user.type(seedInput, "-1");
    expect(screen.getByRole("button", { name: "启动 Review" })).toBeDisabled();
    expect(resolveExecutionOption).not.toHaveBeenCalled();

    await user.clear(seedInput);
    await user.type(seedInput, "13");
    expect(screen.getByRole("button", { name: "启动 Review" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "启动 Review" }));

    await waitFor(() => expect(resolveExecutionOption).toHaveBeenCalledOnce());
    expect(resolveExecutionOption.mock.calls[0][0].prospective_request.seed).toBe(13);
    await waitFor(() => expect(submitRun).toHaveBeenCalledOnce());
  });

  it("allows an empty exact constraint to run with only a simulation profile", async () => {
    const user = userEvent.setup();
    const resolveExecutionOption = vi.fn().mockResolvedValue(executionOption);
    const submitRun = vi.fn().mockResolvedValue({
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:review:simulation-only/events",
      run_id: "run:review:simulation-only",
      status_url: "/api/v1/runs/run:review:simulation-only",
    });
    renderWorkspace(
      reviewApi({
        getConstraint: vi.fn().mockResolvedValue(constraintView()),
        listReviewProfiles: vi
          .fn()
          .mockResolvedValue(
            page(
              [reviewProfile, checkerProfile, simulationProfile, triageProfile],
              null,
              "read:review-profiles",
            ),
          ),
        listReviews: vi.fn().mockResolvedValue(page([], null)),
        resolveExecutionOption,
        submitRun,
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    await selectReviewReplay(user);
    await user.click(screen.getByRole("checkbox", { name: "builtin.simulation · builtin.simulation@1" }));

    expect(
      screen.getByText("当前 exact constraint 含 0 条约束；Checker 或 Simulation 至少选择一种。"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "启动 Review" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "启动 Review" }));

    await waitFor(() => expect(resolveExecutionOption).toHaveBeenCalledOnce());
    expect(resolveExecutionOption.mock.calls[0][0].prospective_request.params).toMatchObject({
      checker_profiles: [],
      constraint_snapshot_artifact_id: generationContext.constraint,
      simulation_profiles: [{ profile_id: "builtin.simulation", version: 1 }],
    });
    await waitFor(() => expect(submitRun).toHaveBeenCalledOnce());
  });

  it("keeps an empty exact constraint disabled when neither deterministic profile type is selected", async () => {
    const user = userEvent.setup();
    const resolveExecutionOption = vi.fn();
    renderWorkspace(
      reviewApi({
        getConstraint: vi.fn().mockResolvedValue(constraintView()),
        listReviewProfiles: vi
          .fn()
          .mockResolvedValue(
            page(
              [reviewProfile, checkerProfile, simulationProfile, triageProfile],
              null,
              "read:review-profiles",
            ),
          ),
        listReviews: vi.fn().mockResolvedValue(page([], null)),
        resolveExecutionOption,
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    await selectReviewReplay(user);

    expect(screen.getByRole("button", { name: "启动 Review" })).toBeDisabled();
    expect(resolveExecutionOption).not.toHaveBeenCalled();
  });

  it("requires a checker for a non-empty exact constraint even when a simulation is selected", async () => {
    const user = userEvent.setup();
    const resolveExecutionOption = vi.fn();
    renderWorkspace(
      reviewApi({
        getConstraint: vi.fn().mockResolvedValue(constraintView([{ id: "constraint:one" }])),
        listReviewProfiles: vi
          .fn()
          .mockResolvedValue(
            page(
              [reviewProfile, checkerProfile, simulationProfile, triageProfile],
              null,
              "read:review-profiles",
            ),
          ),
        listReviews: vi.fn().mockResolvedValue(page([], null)),
        resolveExecutionOption,
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    await selectReviewReplay(user);
    await user.click(screen.getByRole("checkbox", { name: "builtin.simulation · builtin.simulation@1" }));

    expect(
      screen.getByText("当前 exact constraint 含 1 条约束；必须选择至少一个 Checker，Simulation 可选。"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "启动 Review" })).toBeDisabled();
    expect(resolveExecutionOption).not.toHaveBeenCalled();
  });

  it("allows a non-empty exact constraint when a checker is selected", async () => {
    const user = userEvent.setup();
    renderWorkspace(
      reviewApi({
        getConstraint: vi.fn().mockResolvedValue(constraintView([{ id: "constraint:one" }])),
        listReviewProfiles: vi
          .fn()
          .mockResolvedValue(
            page(
              [reviewProfile, checkerProfile, simulationProfile, triageProfile],
              null,
              "read:review-profiles",
            ),
          ),
        listReviews: vi.fn().mockResolvedValue(page([], null)),
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    await selectRequiredReviewReplay(user);

    expect(screen.getByRole("button", { name: "启动 Review" })).toBeEnabled();
  });

  it("fails closed when the exact constraint cannot be read", async () => {
    const getConstraint = vi.fn().mockRejectedValue(new Error("constraint unavailable"));
    const resolveExecutionOption = vi.fn();
    renderWorkspace(
      reviewApi({
        getConstraint,
        listReviewProfiles: vi
          .fn()
          .mockResolvedValue(
            page(
              [reviewProfile, checkerProfile, simulationProfile, triageProfile],
              null,
              "read:review-profiles",
            ),
          ),
        listReviews: vi.fn().mockResolvedValue(page([], null)),
        resolveExecutionOption,
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    expect(await screen.findByText("无法读取 Review constraint")).toBeVisible();
    expect(getConstraint).toHaveBeenCalledWith(generationContext.constraint);
    expect(screen.queryByRole("button", { name: "启动 Review" })).not.toBeInTheDocument();
    expect(resolveExecutionOption).not.toHaveBeenCalled();
  });

  it("fails closed when the constraint read returns a different Artifact ID", async () => {
    const getConstraint = vi.fn().mockResolvedValue(constraintView([], "artifact:constraint:different"));
    const resolveExecutionOption = vi.fn();
    renderWorkspace(
      reviewApi({
        getConstraint,
        listReviewProfiles: vi
          .fn()
          .mockResolvedValue(
            page(
              [reviewProfile, checkerProfile, simulationProfile, triageProfile],
              null,
              "read:review-profiles",
            ),
          ),
        listReviews: vi.fn().mockResolvedValue(page([], null)),
        resolveExecutionOption,
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    expect(await screen.findByText("Review 启动 authority 不安全")).toBeVisible();
    expect(getConstraint).toHaveBeenCalledWith(generationContext.constraint);
    expect(screen.queryByRole("button", { name: "启动 Review" })).not.toBeInTheDocument();
    expect(resolveExecutionOption).not.toHaveBeenCalled();
  });

  it("retries an unknown submission with the same resolved body and intent", async () => {
    const user = userEvent.setup();
    const accepted = {
      accepted_schema_version: "run-accepted@1" as const,
      events_url: "/api/v1/runs/run:review:retry/events",
      run_id: "run:review:retry",
      status_url: "/api/v1/runs/run:review:retry",
    };
    const submitRun = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network result unknown"))
      .mockResolvedValueOnce(accepted);
    const reviewLaunchApi = reviewApi({
      listReviewProfiles: vi
        .fn()
        .mockResolvedValue(
          page([reviewProfile, checkerProfile, triageProfile], null, "read:review-profiles"),
        ),
      listReviews: vi.fn().mockResolvedValue(page([], null)),
      resolveExecutionOption: vi.fn().mockResolvedValue(executionOption),
      submitRun,
    });
    renderWorkspace(
      reviewLaunchApi,
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    await selectRequiredReviewReplay(user);
    await user.click(screen.getByRole("button", { name: "启动 Review" }));

    expect(await screen.findByText("Review 结果未知")).toBeVisible();
    expect(screen.getByRole("button", { name: "启动 Review" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Review profile" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "LLM triage profile" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "LLM execution mode" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Replay source Run" })).toBeDisabled();
    expect(screen.getByText("Seed 不适用")).toBeVisible();
    expect(screen.queryByRole("spinbutton", { name: "Seed" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "以同一 intent 重试" }));

    expect(await screen.findByRole("link", { name: "打开 Run run:review:retry" })).toBeVisible();
    expect(reviewLaunchApi.resolveExecutionOption).toHaveBeenCalledOnce();
    expect(submitRun).toHaveBeenCalledTimes(2);
    expect(submitRun.mock.calls[1][0]).toBe(submitRun.mock.calls[0][0]);
    expect(submitRun.mock.calls[1][1]).toBe(submitRun.mock.calls[0][1]);
  });

  it("never submits when the resolved execution option differs from the frozen Review docket", async () => {
    const user = userEvent.setup();
    const submitRun = vi.fn();
    renderWorkspace(
      reviewApi({
        listReviewProfiles: vi
          .fn()
          .mockResolvedValue(
            page([reviewProfile, checkerProfile, triageProfile], null, "read:review-profiles"),
          ),
        listReviews: vi.fn().mockResolvedValue(page([], null)),
        resolveExecutionOption: vi
          .fn()
          .mockResolvedValue({ ...executionOption, source_run_id: "run:review:different-source" }),
        submitRun,
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    await selectRequiredReviewReplay(user);
    await user.click(screen.getByRole("button", { name: "启动 Review" }));

    expect(await screen.findByText("Review 启动 authority 不安全")).toBeVisible();
    expect(submitRun).not.toHaveBeenCalled();
    expect(screen.getByRole("combobox", { name: "Review profile" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "启动 Review" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "以同一 intent 重试" })).not.toBeInTheDocument();
  });

  it("allows a fresh launch after an unknown resolver response before any Run submission", async () => {
    const user = userEvent.setup();
    const resolveExecutionOption = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("resolver response unknown"))
      .mockResolvedValueOnce(executionOption);
    const submitRun = vi.fn().mockResolvedValue({
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:review:resolver-retry/events",
      run_id: "run:review:resolver-retry",
      status_url: "/api/v1/runs/run:review:resolver-retry",
    });
    renderWorkspace(
      reviewApi({
        listReviewProfiles: vi
          .fn()
          .mockResolvedValue(
            page([reviewProfile, checkerProfile, triageProfile], null, "read:review-profiles"),
          ),
        listReviews: vi.fn().mockResolvedValue(page([], null)),
        resolveExecutionOption,
        submitRun,
      }),
      `/reviews?sourceRun=${encodeURIComponent(generationContext.sourceRun)}&snapshot=${encodeURIComponent(generationContext.snapshot)}&constraint=${encodeURIComponent(generationContext.constraint)}`,
    );

    await selectRequiredReviewReplay(user);
    await user.click(screen.getByRole("button", { name: "启动 Review" }));
    expect(await screen.findByText("Review 解析失败")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Review profile" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "以同一 intent 重试" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "启动 Review" }));

    expect(await screen.findByRole("link", { name: "打开 Run run:review:resolver-retry" })).toBeVisible();
    expect(resolveExecutionOption).toHaveBeenCalledTimes(2);
    expect(resolveExecutionOption.mock.calls[1][0]).toStrictEqual(resolveExecutionOption.mock.calls[0][0]);
    expect(submitRun).toHaveBeenCalledOnce();
  });

  it("keeps multiple immutable Review artifacts on the same snapshot as separate rows", async () => {
    const previewId = "artifact:preview:shared";
    const api = reviewApi({
      listReviews: vi
        .fn()
        .mockResolvedValueOnce(
          page(
            [
              review("artifact:review:1", "review@1", [previewId], 2),
              review("artifact:review:2", "review@2", ["artifact:preview:other"], 1),
            ],
            null,
          ),
        ),
    });

    renderWorkspace(
      api,
      `/reviews?sourceRun=${encodeURIComponent("run:generation:7")}&snapshot=${encodeURIComponent(previewId)}`,
    );

    expect(await screen.findByRole("heading", { level: 1, name: "审查报告" })).toBeVisible();
    expect(screen.getByRole("link", { name: "打开 artifact:review:1" })).toHaveAttribute(
      "href",
      "/reviews/artifact%3Areview%3A1?sourceRun=run%3Ageneration%3A7&snapshot=artifact%3Apreview%3Ashared",
    );
    expect(screen.getByRole("link", { name: "打开 artifact:review:2" })).toHaveAttribute(
      "href",
      "/reviews/artifact%3Areview%3A2?sourceRun=run%3Ageneration%3A7&snapshot=artifact%3Apreview%3Ashared",
    );
    expect(screen.getByText("review@1")).toBeVisible();
    expect(screen.getByText("review@2")).toBeVisible();
    expect(screen.getByText("2 确定性 · 0 仿真 · 0 LLM · 0 未证明")).toBeVisible();
    expect(screen.getByText("1 确定性 · 0 仿真 · 0 LLM · 0 未证明")).toBeVisible();
    expect(screen.getByText(/来源 Run 仅作为导航上下文/)).toBeVisible();
    expect(screen.getByText("direct parent 包含请求的候选 Artifact")).toBeVisible();
    expect(screen.getByText("direct parent 不含请求的候选 Artifact；未隐藏该报告")).toBeVisible();
  });

  it("fails closed instead of labeling a malformed Review partition", async () => {
    const invalid = review("artifact:review:invalid", "review@1", [], 1);
    invalid.report.simulation_findings = invalid.report.deterministic_findings;
    invalid.report.deterministic_findings = [];
    renderWorkspace(reviewApi({ listReviews: vi.fn().mockResolvedValue(page([invalid], null)) }));

    expect(await screen.findByText("无法读取审查报告")).toBeVisible();
    expect(screen.queryByRole("link", { name: "打开 artifact:review:invalid" })).not.toBeInTheDocument();
  });

  it("appends only the same read snapshot and preserves the opaque cursor", async () => {
    const user = userEvent.setup();
    const listReviews = vi
      .fn()
      .mockResolvedValueOnce(page([review("artifact:review:1", "review@1", [], 0)], "opaque+/="))
      .mockResolvedValueOnce(page([review("artifact:review:2", "review@2", [], 0)], null));
    renderWorkspace(reviewApi({ listReviews }));

    await user.click(await screen.findByRole("button", { name: "加载下一页" }));

    await waitFor(() => expect(listReviews).toHaveBeenLastCalledWith("opaque+/="));
    expect(screen.getByRole("link", { name: "打开 artifact:review:2" })).toBeVisible();
  });

  it("requires an explicit restart after a 410 and does not discard already-read rows", async () => {
    const user = userEvent.setup();
    const listReviews = vi
      .fn()
      .mockResolvedValueOnce(page([review("artifact:review:1", "review@1", [], 0)], "stale"))
      .mockRejectedValueOnce(
        new CursorExpiredError(
          {
            code: "cursor_expired",
            conflict_set_id: null,
            detail: "Review cursor expired.",
            earliest_cursor: null,
            instance: "/api/v1/reviews",
            request_id: "request:review-expired",
            retry_after_s: null,
            run_id: null,
            status: 410,
            title: "Cursor expired",
            trace_id: null,
            type: "about:blank",
          },
          "stale",
        ),
      )
      .mockResolvedValueOnce(page([review("artifact:review:fresh", "review@3", [], 0)], null, "read:fresh"));
    renderWorkspace(reviewApi({ listReviews }));

    await user.click(await screen.findByRole("button", { name: "加载下一页" }));
    expect(await screen.findByText(/分页游标已过期/)).toBeVisible();
    expect(screen.getByRole("link", { name: "打开 artifact:review:1" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "重新开始查询" }));
    expect(await screen.findByRole("link", { name: "打开 artifact:review:fresh" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "打开 artifact:review:1" })).not.toBeInTheDocument();
  });

  it("ignores an old next-page response after a fresh first-page refetch", async () => {
    const user = userEvent.setup();
    let resolveOldPage!: (value: ReviewPage) => void;
    const oldPage = new Promise<ReviewPage>((resolve) => {
      resolveOldPage = resolve;
    });
    const listReviews = vi
      .fn()
      .mockResolvedValueOnce(page([review("artifact:review:initial", "review@1", [], 0)], "old-cursor"))
      .mockReturnValueOnce(oldPage)
      .mockResolvedValueOnce(page([review("artifact:review:fresh", "review@1", [], 0)], null, "read:fresh"));
    const rendered = renderWorkspace(reviewApi({ listReviews }));

    await user.click(await screen.findByRole("button", { name: "加载下一页" }));
    await waitFor(() => expect(listReviews).toHaveBeenCalledWith("old-cursor"));
    await rendered.client.invalidateQueries({ queryKey: ["review-workspace"] });
    expect(await screen.findByRole("link", { name: "打开 artifact:review:fresh" })).toBeVisible();

    resolveOldPage(page([review("artifact:review:stale", "review@1", [], 0)], null));
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "打开 artifact:review:stale" })).not.toBeInTheDocument(),
    );
  });
});
