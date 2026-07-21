import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { ApiProblemError, type SafeProblem } from "../../api/problem";
import { createQueryClient } from "../../api/query-client";
import { ConstraintProposalPage, type ConstraintProposalApi } from "./ConstraintProposalPage";
import type {
  ApprovalView,
  ArtifactPayloadView,
  ConstraintProposalReadView,
  ExecutionProfilePage,
  SubjectApprovalBindingView,
} from "./api";

const hash = "a".repeat(64);
const domainScope: components["schemas"]["DomainScope"] = {
  domain_ids: ["domain:economy"],
};

function artifact(
  artifactId: string,
  kind: components["schemas"]["ArtifactSummaryV1"]["kind"],
  payloadSchemaId: string,
): components["schemas"]["ArtifactSummaryV1"] {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-19T08:00:00Z",
    domain_scope: domainScope,
    kind,
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [],
    payload_hash: hash,
    payload_schema_id: payloadSchemaId,
    summary_schema_version: "artifact-summary@1",
    version_tuple: { tool_version: "constraint-workflow@1" },
  };
}

const constraint: components["schemas"]["Constraint"] = {
  assert: "reward_gold <= 75",
  dsl_grammar_version: "dsl@1",
  id: "constraint:reward-cap",
  kind: "numeric",
  note: "控制金币通胀",
  oracle: "deterministic",
  severity: "major",
};

function proposal(
  producedBy: "agent" | "human" = "agent",
  artifactId = "artifact:proposal:1",
  revision = 1,
  approvalStatus = "draft",
): ConstraintProposalReadView {
  const summary = artifact(artifactId, "constraint_proposal", "constraint-proposal@1");
  summary.parent_artifact_ids = [
    "artifact:constraint:base",
    "artifact:source:design",
    ...(revision > 1 ? ["artifact:proposal:1"] : []),
  ];
  return {
    approval_status: approvalStatus,
    artifact: summary,
    proposal: {
      base_constraint_snapshot_id: "constraint-snapshot:base",
      constraints: [constraint],
      domain_scope: domainScope,
      dsl_grammar_version: "dsl@1",
      produced_by: producedBy,
      producer_run_id: producedBy === "agent" ? "run:agent-propose" : null,
      proposal_schema_version: "constraint-proposal@1",
      rationale: "将金币奖励上限编译为确定性约束。",
      revision,
      source_bindings: [
        {
          provenance_hash: "b".repeat(64),
          source_artifact_id: "artifact:source:design",
          source_ref: null,
        },
      ],
      supersedes_artifact_id: revision > 1 ? "artifact:proposal:1" : null,
    },
    view_schema_version: "constraint-proposal-read-view@1",
    workflow_revision: 7,
  };
}

const targetBinding: components["schemas"]["ConstraintTargetBindingV1"] = {
  binding_schema_version: "approval-target-binding@1",
  expected_ref: { artifact_id: "artifact:constraint:base", revision: 4 },
  ref_name: "refs/constraints/economy",
  subject_kind: "constraint_proposal",
  target_artifact_id: "artifact:constraint:candidate",
  target_artifact_kind: "constraint_snapshot",
  target_digest: "c".repeat(64),
  target_snapshot_id: "constraint-snapshot:candidate",
};

const requirement: components["schemas"]["ApprovalRequirement"] = {
  assignee_principal_ids: [],
  distinct_from_requirement_ids: [],
  domain_scope: domainScope,
  min_approvals: 1,
  required_permission: {
    action: "approve",
    domain_scope: domainScope,
    resource_kind: "constraint_proposal",
  },
  requirement_id: "requirement:constraint-admin",
  route_role: "constraint_admin",
};

function approvalView(
  status: components["schemas"]["ApprovalItem"]["status"] = "draft",
  overrides: Partial<components["schemas"]["ApprovalItem"]> = {},
): ApprovalView {
  const registry = { registry_digest: "d".repeat(64), registry_version: "domains@1" };
  return {
    approval: {
      approval_id: "approval:server-bound",
      approval_policy: { policy_digest: "e".repeat(64), policy_version: "approval@1" },
      approval_schema_version: "approval@1",
      created_at: "2026-07-19T08:01:00Z",
      decisions: [],
      domain_registry_ref: registry,
      domain_scope: domainScope,
      evidence_set_artifact_id:
        status === "validated" || status === "pending_approval" || status === "approved"
          ? "artifact:evidence:1"
          : null,
      last_validation_failure_artifact_id: null,
      proposer: { principal_id: "human:maker", principal_kind: "human" },
      regression_evidence_artifact_ids: [],
      requirements: [requirement],
      role_policy_digest: "f".repeat(64),
      role_policy_version: "roles@1",
      route_policy: {
        domain_registry_ref: registry,
        route_digest: "1".repeat(64),
        route_version: "routes@1",
      },
      status,
      subject_artifact_id: "artifact:proposal:1",
      subject_digest: "2".repeat(64),
      subject_kind: "constraint_proposal",
      subject_revision: 1,
      subject_series_id: "series:constraint:1",
      target_binding:
        status === "validated" || status === "pending_approval" || status === "approved"
          ? targetBinding
          : null,
      workflow_revision: 7,
      ...overrides,
    },
    current_actor_allowed_requirement_ids: [],
    requirement_progress: [
      {
        decision_eligibility: [
          {
            decision: "approve",
            eligible: false,
            reason_codes: [status === "pending_approval" ? "maker_checker_conflict" : "workflow_not_pending"],
          },
          {
            decision: "reject",
            eligible: false,
            reason_codes: [status === "pending_approval" ? "maker_checker_conflict" : "workflow_not_pending"],
          },
          {
            decision: "request_changes",
            eligible: false,
            reason_codes: [status === "pending_approval" ? "maker_checker_conflict" : "workflow_not_pending"],
          },
        ],
        domain_scope: domainScope,
        eligible_for_current_actor: false,
        min_approvals: 1,
        requirement_id: requirement.requirement_id,
        route_role: requirement.route_role,
        satisfied: status === "approved",
        unmet_distinct_from_requirement_ids: [],
        valid_approval_count: status === "approved" ? 1 : 0,
      },
    ],
    view_schema_version: "approval-view@1",
  };
}

function approvalBinding(
  status: SubjectApprovalBindingView["approval_status"] = "draft",
  overrides: Partial<SubjectApprovalBindingView> = {},
): SubjectApprovalBindingView {
  return {
    approval_id: "approval:server-bound",
    approval_status: status,
    is_current_head: true,
    subject_artifact_id: "artifact:proposal:1",
    subject_digest: "2".repeat(64),
    subject_head_revision: 1,
    subject_kind: "constraint_proposal",
    subject_revision: 1,
    subject_series_id: "series:constraint:1",
    workflow_revision: 7,
    ...overrides,
  };
}

const compilerProfile: components["schemas"]["ExecutionProfileViewV1"] = {
  compatible_run_kinds: [{ kind: "constraint_proposal.validate", version: 1 }],
  display_name: "Builtin constraint compiler",
  domain_scope: domainScope,
  env_contract_version: null,
  input_schema_ids: ["constraint-validation@1"],
  output_schema_ids: ["constraint-snapshot@1"],
  profile: { profile_id: "builtin.constraint_compiler", version: 1 },
  profile_kind: "constraint_compiler",
  profile_payload_hash: "3".repeat(64),
  required_capabilities: [],
  status: "active",
  stochastic: false,
  target_environment_profile: null,
};

const validationProfile: components["schemas"]["ExecutionProfileViewV1"] = {
  ...compilerProfile,
  display_name: "Deterministic validation",
  profile: { profile_id: "builtin.validation", version: 3 },
  profile_kind: "validation",
  profile_payload_hash: "4".repeat(64),
};

const profilePage: ExecutionProfilePage = {
  expires_at: "2026-07-19T09:00:00Z",
  items: [compilerProfile, validationProfile],
  next_cursor: null,
  page_schema_version: "page@1",
  read_snapshot_id: "read:profiles",
};

function evidenceArtifact(status: "passed" | "failed" | "unproven"): ArtifactPayloadView {
  return {
    artifact: artifact("artifact:evidence:1", "validation_evidence", "evidence-set@1"),
    payload: {
      evidence_schema_version: "evidence-set@1",
      finding_bindings: [],
      overall_status: status,
      policy_version: "builtin.validation@3",
      requirements: [
        {
          applicability: "required",
          evidence_artifact_id: status === "unproven" ? null : "artifact:compile-evidence:1",
          kind: "constraint_compile",
          reason_code: status === "unproven" ? "solver_timeout" : null,
          requirement_id: "compile",
          status,
          tool_version: "builtin.constraint_compiler@1",
        },
      ],
      subject_artifact_id: "artifact:proposal:1",
      subject_digest: "2".repeat(64),
      supporting_artifact_ids: [],
      target_binding: status === "passed" ? targetBinding : null,
      validation_run_id: "run:validation:1",
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

const failureArtifact: ArtifactPayloadView = {
  artifact: artifact("artifact:failure:1", "run_failure", "run-failure@1"),
  payload: {
    cause_code: "compiler_dependency_failed",
    failure_schema_version: "run-failure@1",
    redacted_message: "Compiler dependency failed safely.",
    run_id: "run:validation:failed",
  },
  resource_revision: 1,
  view_schema_version: "artifact-payload-view@1",
};

const baseArtifact: ArtifactPayloadView = {
  artifact: {
    ...artifact("artifact:constraint:base", "constraint_snapshot", "constraint-snapshot@1"),
    version_tuple: {
      constraint_snapshot_id: "constraint-snapshot:base",
      tool_version: "constraint-compiler@1",
    },
  },
  payload: {},
  resource_revision: 1,
  view_schema_version: "artifact-payload-view@1",
};

function api(overrides: Partial<ConstraintProposalApi> = {}): ConstraintProposalApi {
  const current = proposal();
  const approval = approvalView();
  const compilerBinding: Awaited<
    ReturnType<ConstraintProposalApi["getConstraintValidationCompilerBinding"]>
  > = {
    binding_schema_version: "constraint-validation-compiler-binding@1",
    compiler_profile: compilerProfile.profile,
    differential_engines: [
      { engine_id: "clingo", version: 1 },
      { engine_id: "z3", version: 1 },
    ],
    profile_payload_hash: compilerProfile.profile_payload_hash,
    run_kind: { kind: "constraint_proposal.validate", version: 1 },
  };
  const publishResult: Awaited<ReturnType<ConstraintProposalApi["publishConstraint"]>> = {
    approval,
    ref_name: targetBinding.ref_name,
    ref_transition_id: "ref-transition:1",
    ref_value: { artifact_id: targetBinding.target_artifact_id, revision: 5 },
    result_schema_version: "workflow-apply-result@1",
    reversed_approval_id: null,
  };
  const accepted: Awaited<ReturnType<ConstraintProposalApi["validateConstraint"]>> = {
    accepted_schema_version: "run-accepted@1",
    events_url: "/api/v1/runs/run:validation:accepted/events",
    run_id: "run:validation:accepted",
    status_url: "/api/v1/runs/run:validation:accepted",
  };
  return {
    getApproval: vi.fn(async () => ({ etag: '"approval:7"', value: approval })),
    getApprovalBinding: vi.fn(async () => approvalBinding()),
    getArtifactPayload: vi.fn(async (artifactId) =>
      artifactId === "artifact:constraint:base" ? baseArtifact : evidenceArtifact("passed"),
    ),
    getConstraintProposal: vi.fn(async () => ({ etag: '"proposal:1"', value: current })),
    getConstraintValidationCompilerBinding: vi.fn(async () => compilerBinding),
    listExecutionProfiles: vi.fn(async () => profilePage),
    publishConstraint: vi.fn(async () => publishResult),
    reviseConstraint: vi.fn(async () => proposal("human", "artifact:proposal:2", 2)),
    submitConstraintForApproval: vi.fn(async () => approval),
    validateConstraint: vi.fn(async () => accepted),
    ...overrides,
  };
}

function renderPage(proposalApi: ConstraintProposalApi) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <ConstraintProposalPage api={proposalApi} artifactId="artifact:proposal:1" />
    </QueryClientProvider>,
  );
}

function problem(overrides: Partial<SafeProblem> = {}): SafeProblem {
  return {
    code: "workflow_guard",
    conflict_set_id: null,
    detail: "Workflow command was rejected.",
    earliest_cursor: null,
    instance: "/api/v1/constraint-proposals/artifact:proposal:1:validate",
    request_id: "request:1",
    retry_after_s: null,
    run_id: null,
    status: 409,
    title: "Workflow conflict",
    trace_id: null,
    type: "about:blank",
    ...overrides,
  };
}

async function fillRefBinding(user: ReturnType<typeof userEvent.setup>) {
  await user.clear(screen.getByRole("textbox", { name: "Ref name" }));
  await user.type(screen.getByRole("textbox", { name: "Ref name" }), "refs/constraints/economy");
  await user.type(
    screen.getByRole("textbox", { name: "Expected ref Artifact ID" }),
    "artifact:constraint:base",
  );
  await user.type(screen.getByRole("spinbutton", { name: "Expected ref revision" }), "4");
}

describe("ConstraintProposalPage", () => {
  it("loads the server-bound approval only, labels Agent provenance, and gates validation on human revision", async () => {
    const proposalApi = api();
    renderPage(proposalApi);

    expect(screen.getByRole("heading", { name: "正在读取约束候选" })).toBeVisible();
    expect(await screen.findByRole("heading", { level: 1, name: "约束候选" })).toBeVisible();
    expect(screen.getByText("Agent 候选 · 必须由 Human 修订")).toBeVisible();
    expect(screen.getByRole("button", { name: "开始确定性验证" })).toBeDisabled();
    expect(screen.getByRole("heading", { name: "人工接管与修订" })).toBeVisible();
    expect(proposalApi.getApproval).toHaveBeenCalledWith("approval:server-bound");
    expect(screen.getByRole("link", { name: "打开 exact approval" })).toHaveAttribute(
      "href",
      "/approvals/approval%3Aserver-bound",
    );
    expect(
      screen
        .getAllByRole("combobox")
        .every((select) => select)
        .valueOf(),
    ).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "Compiler profile" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "Validation profile" })).toHaveValue("");
  });

  it("requires an explicit confirmation before submitting expected_ref=null", async () => {
    const user = userEvent.setup();
    renderPage(api());
    await screen.findByRole("heading", { name: "人工接管与修订" });

    await user.type(screen.getByRole("textbox", { name: "Ref name" }), "refs/constraints/new");
    expect(screen.getByRole("button", { name: "提交人工修订" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "确认当前 ref 不存在（expected_ref=null）" }));
    expect(screen.getByRole("button", { name: "提交人工修订" })).toBeEnabled();
  });

  it("fails closed when binding and Approval revisions are composed from different reads", async () => {
    renderPage(
      api({
        getApprovalBinding: vi.fn(async () =>
          approvalBinding("draft", { subject_series_id: "series:stale", workflow_revision: 6 }),
        ),
      }),
    );

    expect(await screen.findByRole("heading", { name: "审批绑定不一致" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "开始确定性验证" })).not.toBeInTheDocument();
  });

  it("renders a retained superseded proposal when its current head has advanced", async () => {
    const historical = proposal("human", "artifact:proposal:1", 1, "superseded");
    renderPage(
      api({
        getApproval: vi.fn(async () => ({
          etag: '"approval:historical"',
          value: approvalView("superseded"),
        })),
        getApprovalBinding: vi.fn(async () =>
          approvalBinding("superseded", {
            is_current_head: false,
            subject_head_revision: 2,
          }),
        ),
        getConstraintProposal: vi.fn(async () => ({
          etag: '"proposal:historical"',
          value: historical,
        })),
      }),
    );

    expect(await screen.findByRole("heading", { level: 1, name: "约束候选" })).toBeVisible();
    expect(screen.getByText("head 2 · workflow 7 · superseded")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "审批绑定不一致" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "提交人工修订" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "开始确定性验证" })).toBeDisabled();
  });

  it("fails closed when an equal subject/head revision is marked non-current", async () => {
    renderPage(
      api({
        getApprovalBinding: vi.fn(async () =>
          approvalBinding("draft", {
            is_current_head: false,
            subject_head_revision: 1,
          }),
        ),
      }),
    );

    expect(await screen.findByRole("heading", { name: "审批绑定不一致" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "开始确定性验证" })).not.toBeInTheDocument();
  });

  it.each([
    ["zero", [], new Map<string, ArtifactPayloadView>()],
    [
      "non-matching",
      ["artifact:constraint:wrong"],
      new Map<string, ArtifactPayloadView>([
        [
          "artifact:constraint:wrong",
          {
            ...baseArtifact,
            artifact: {
              ...baseArtifact.artifact,
              artifact_id: "artifact:constraint:wrong",
              version_tuple: { constraint_snapshot_id: "constraint-snapshot:other" },
            },
          },
        ],
      ]),
    ],
    [
      "multiple",
      ["artifact:constraint:base", "artifact:constraint:base-duplicate"],
      new Map<string, ArtifactPayloadView>([
        ["artifact:constraint:base", baseArtifact],
        [
          "artifact:constraint:base-duplicate",
          {
            ...baseArtifact,
            artifact: {
              ...baseArtifact.artifact,
              artifact_id: "artifact:constraint:base-duplicate",
            },
          },
        ],
      ]),
    ],
  ] as const)("fails closed for a %s base Artifact match", async (_case, candidateIds, candidates) => {
    const human = proposal("human");
    human.artifact.parent_artifact_ids = ["artifact:source:design", ...candidateIds];
    const proposalApi = api({
      getArtifactPayload: vi.fn(async (artifactId) => {
        const candidate = candidates.get(artifactId);
        if (!candidate) throw new Error(`Unexpected candidate ${artifactId}`);
        return candidate;
      }),
      getConstraintProposal: vi.fn(async () => ({ etag: '"proposal:base-check"', value: human })),
    });
    const user = userEvent.setup();
    renderPage(proposalApi);

    expect(await screen.findByRole("heading", { name: "Base Constraint Artifact 未唯一解析" })).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Ref name" }), "refs/constraints/new");
    await user.click(screen.getByRole("checkbox", { name: "确认当前 ref 不存在（expected_ref=null）" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Compiler profile" }),
      "builtin.constraint_compiler@1",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Validation profile" }),
      "builtin.validation@3",
    );
    expect(screen.getByRole("button", { name: "提交人工修订" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "开始确定性验证" })).toBeDisabled();
  });

  it("submits a human revision from retained proposal data and reloads the superseding Artifact by GET", async () => {
    const agent = proposal();
    const revised = proposal("human", "artifact:proposal:2", 2);
    const getConstraintProposal = vi.fn(async (artifactId: string) =>
      artifactId === revised.artifact.artifact_id
        ? { etag: '"proposal:2"', value: revised }
        : { etag: '"proposal:1"', value: agent },
    );
    const getApprovalBinding = vi.fn(async (artifactId: string) =>
      approvalBinding("draft", {
        approval_id:
          artifactId === revised.artifact.artifact_id ? "approval:revised" : "approval:server-bound",
        subject_artifact_id: artifactId,
        subject_head_revision: artifactId === revised.artifact.artifact_id ? 2 : 1,
        subject_revision: artifactId === revised.artifact.artifact_id ? 2 : 1,
      }),
    );
    const getApproval = vi.fn(async (approvalId: string) => {
      const isRevised = approvalId === "approval:revised";
      return {
        etag: isRevised ? '"approval:revised"' : '"approval:7"',
        value: approvalView("draft", {
          approval_id: approvalId,
          subject_artifact_id: isRevised ? "artifact:proposal:2" : "artifact:proposal:1",
          subject_revision: isRevised ? 2 : 1,
        }),
      };
    });
    const proposalApi = api({ getApproval, getApprovalBinding, getConstraintProposal });
    const user = userEvent.setup();
    renderPage(proposalApi);
    await screen.findByRole("heading", { name: "人工接管与修订" });

    await fillRefBinding(user);
    await user.click(screen.getByRole("button", { name: "提交人工修订" }));

    expect(proposalApi.reviseConstraint).toHaveBeenCalledWith(
      { etag: '"proposal:1"', value: agent },
      {
        approval_id: "approval:server-bound",
        base_constraint_snapshot_artifact_id: "artifact:constraint:base",
        constraints: agent.proposal.constraints,
        domain_scope: agent.proposal.domain_scope,
        dsl_grammar_version: "dsl@1",
        expected_ref: { artifact_id: "artifact:constraint:base", revision: 4 },
        expected_subject_head_revision: 1,
        expected_workflow_revision: 7,
        rationale: agent.proposal.rationale,
        ref_name: "refs/constraints/economy",
        request_schema_version: "human-constraint-revision-request@1",
        source_artifact_ids: ["artifact:source:design"],
      },
      expect.objectContaining({ idempotencyKey: expect.any(String) }),
    );
    await waitFor(() => expect(getConstraintProposal).toHaveBeenCalledWith("artifact:proposal:2"));
    expect(await screen.findByText("Human 修订候选")).toBeVisible();
    expect(screen.getByRole("link", { name: "当前 revision canonical detail" })).toHaveAttribute(
      "href",
      "/constraint-proposals/artifact%3Aproposal%3A2",
    );
  });

  it("copies the frozen compiler tuple verbatim into validation and links only the accepted Run", async () => {
    const human = proposal("human");
    const binding = approvalBinding("draft");
    const compilerBinding = {
      binding_schema_version: "constraint-validation-compiler-binding@1" as const,
      compiler_profile: compilerProfile.profile,
      differential_engines: [
        { engine_id: "clingo", version: 1 },
        { engine_id: "graph-reference", version: 1 },
        { engine_id: "z3", version: 1 },
      ],
      profile_payload_hash: compilerProfile.profile_payload_hash,
      run_kind: { kind: "constraint_proposal.validate", version: 1 },
    };
    const proposalApi = api({
      getApprovalBinding: vi.fn(async () => binding),
      getConstraintProposal: vi.fn(async () => ({ etag: '"proposal:human"', value: human })),
      getConstraintValidationCompilerBinding: vi.fn(async () => compilerBinding),
    });
    const user = userEvent.setup();
    renderPage(proposalApi);
    await screen.findByText("Human 修订候选");

    await fillRefBinding(user);
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Compiler profile" }),
      "builtin.constraint_compiler@1",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Validation profile" }),
      "builtin.validation@3",
    );
    await user.click(screen.getByRole("button", { name: "开始确定性验证" }));

    expect(proposalApi.getConstraintValidationCompilerBinding).toHaveBeenCalledWith(
      "builtin.constraint_compiler",
      1,
    );
    expect(proposalApi.validateConstraint).toHaveBeenCalledWith(
      { etag: '"proposal:human"', value: human },
      {
        approval_id: binding.approval_id,
        base_constraint_snapshot_artifact_id: "artifact:constraint:base",
        compiler_profile: compilerBinding.compiler_profile,
        differential_engines: compilerBinding.differential_engines,
        dsl_grammar_version: human.proposal.dsl_grammar_version,
        expected_subject_head_revision: binding.subject_head_revision,
        expected_workflow_revision: binding.workflow_revision,
        golden_suite_artifact_id: null,
        regression_suite_artifact_ids: [],
        request_schema_version: "constraint-validation-admission-request@1",
        seed: null,
        subject_digest: binding.subject_digest,
        target: {
          expected_ref: { artifact_id: "artifact:constraint:base", revision: 4 },
          ref_name: "refs/constraints/economy",
        },
        validation_policy: validationProfile.profile,
      },
      expect.objectContaining({ idempotencyKey: expect.any(String) }),
    );
    expect(await screen.findByRole("link", { name: "打开 validation Run" })).toHaveAttribute(
      "href",
      "/runs/run%3Avalidation%3Aaccepted",
    );
    expect(proposalApi.getConstraintProposal).toHaveBeenCalledTimes(2);
  });

  it("does not offer another validation command while the current proposal is already validating", async () => {
    const human = proposal("human", "artifact:proposal:1", 1, "validating");
    const proposalApi = api({
      getApproval: vi.fn(async () => ({
        etag: '"approval:validating"',
        value: approvalView("validating", { active_validation_run_id: "run:validation:active" }),
      })),
      getApprovalBinding: vi.fn(async () => approvalBinding("validating")),
      getConstraintProposal: vi.fn(async () => ({ etag: '"proposal:validating"', value: human })),
    });
    const user = userEvent.setup();
    renderPage(proposalApi);
    await screen.findByText("Human 修订候选");

    await fillRefBinding(user);
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Compiler profile" }),
      "builtin.constraint_compiler@1",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Validation profile" }),
      "builtin.validation@3",
    );

    expect(screen.getByRole("button", { name: "开始确定性验证" })).toBeDisabled();
    expect(proposalApi.validateConstraint).not.toHaveBeenCalled();
  });

  it.each([
    ["failed", "确定性证据：failed"],
    ["unproven", "确定性证据：unproven"],
    ["passed", "确定性证据：validated"],
  ] as const)("renders %s only from a schema-guarded EvidenceSet", async (status, label) => {
    const approvalStatus = status === "passed" ? "validated" : "validation_failed";
    const approval = approvalView(approvalStatus, {
      evidence_set_artifact_id: "artifact:evidence:1",
      last_validation_failure_artifact_id: status === "failed" ? "artifact:failure:1" : null,
    });
    const getArtifactPayload = vi.fn(async (artifactId: string) => {
      if (artifactId === "artifact:constraint:base") return baseArtifact;
      return artifactId === "artifact:failure:1" ? failureArtifact : evidenceArtifact(status);
    });
    renderPage(
      api({
        getApproval: vi.fn(async () => ({ etag: '"approval:evidence"', value: approval })),
        getApprovalBinding: vi.fn(async () => approvalBinding(approvalStatus)),
        getArtifactPayload,
        getConstraintProposal: vi.fn(async () => ({
          etag: '"proposal:human"',
          value: proposal("human", "artifact:proposal:1", 1, approvalStatus),
        })),
      }),
    );

    expect(await screen.findByText(label)).toBeVisible();
    expect(screen.getByRole("link", { name: "打开证据 Run" })).toHaveAttribute(
      "href",
      "/runs/run%3Avalidation%3A1",
    );
    if (status === "failed") {
      expect(screen.getByText("Compiler dependency failed safely.")).toBeVisible();
      expect(screen.getByRole("link", { name: "打开失败 Run" })).toHaveAttribute(
        "href",
        "/runs/run%3Avalidation%3Afailed",
      );
    }
  });

  it("refuses to interpret evidence with a drifted payload schema", async () => {
    const approval = approvalView("validated");
    const unsafe = {
      ...evidenceArtifact("passed"),
      artifact: artifact("artifact:evidence:1", "validation_evidence", "evidence-set@2"),
      payload: { overall_status: "passed", raw_secret: "must-not-render" },
    } satisfies ArtifactPayloadView;
    renderPage(
      api({
        getApproval: vi.fn(async () => ({ etag: '"approval:unsafe"', value: approval })),
        getApprovalBinding: vi.fn(async () => approvalBinding("validated")),
        getArtifactPayload: vi.fn(async (artifactId) =>
          artifactId === "artifact:constraint:base" ? baseArtifact : unsafe,
        ),
        getConstraintProposal: vi.fn(async () => ({
          etag: '"proposal:human"',
          value: proposal("human", "artifact:proposal:1", 1, "validated"),
        })),
      }),
    );

    expect(await screen.findByText("证据载荷无法安全解释")).toBeVisible();
    expect(screen.queryByText("must-not-render")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "提交审批" })).toBeDisabled();
  });

  it("selects an exact approval requirement for the another-human handoff and submits typed state", async () => {
    const human = proposal("human", "artifact:proposal:1", 1, "validated");
    const approval = approvalView("validated");
    const binding = approvalBinding("validated");
    const proposalApi = api({
      getApproval: vi.fn(async () => ({ etag: '"approval:validated"', value: approval })),
      getApprovalBinding: vi.fn(async () => binding),
      getConstraintProposal: vi.fn(async () => ({ etag: '"proposal:validated"', value: human })),
    });
    const user = userEvent.setup();
    renderPage(proposalApi);
    await screen.findByText("确定性证据：validated");

    const submit = screen.getByRole("button", { name: "提交审批" });
    expect(submit).toBeDisabled();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Approval requirement" }),
      requirement.requirement_id,
    );
    expect(screen.getByText("constraint_admin")).toBeVisible();
    expect(screen.getByText("approve · constraint_proposal · domain:economy")).toBeVisible();
    expect(
      screen.getByText("此选择仅用于核对 server-frozen route，不进入 submit payload，也不会改变审批路由。"),
    ).toBeVisible();
    await user.click(submit);

    expect(proposalApi.submitConstraintForApproval).toHaveBeenCalledWith(
      { etag: '"proposal:validated"', value: human },
      {
        approval_id: binding.approval_id,
        expected_workflow_revision: binding.workflow_revision,
        request_schema_version: "submit-for-approval-request@1",
      },
      expect.objectContaining({ idempotencyKey: expect.any(String) }),
    );
    expect(screen.getByRole("link", { name: "交给另一位 Human 审批" })).toHaveAttribute(
      "href",
      "/approvals/approval%3Aserver-bound",
    );
    expect(proposalApi.getConstraintProposal).toHaveBeenCalledTimes(2);
  });

  it("publishes only the approved ConstraintTargetBinding and renders the returned ref authority", async () => {
    const human = proposal("human", "artifact:proposal:1", 1, "approved");
    const approval = approvalView("approved");
    const binding = approvalBinding("approved");
    const proposalApi = api({
      getApproval: vi.fn(async () => ({ etag: '"approval:approved"', value: approval })),
      getApprovalBinding: vi.fn(async () => binding),
      getConstraintProposal: vi.fn(async () => ({ etag: '"proposal:approved"', value: human })),
    });
    const user = userEvent.setup();
    renderPage(proposalApi);
    await screen.findByText("确定性证据：validated");
    await user.click(screen.getByRole("button", { name: "发布权威约束" }));

    expect(proposalApi.publishConstraint).toHaveBeenCalledWith(
      { etag: '"proposal:approved"', value: human },
      {
        approval_id: binding.approval_id,
        expected_ref: targetBinding.expected_ref,
        expected_workflow_revision: binding.workflow_revision,
        ref_name: targetBinding.ref_name,
        request_schema_version: "workflow-apply-request@1",
        subject_digest: binding.subject_digest,
        target_artifact_id: targetBinding.target_artifact_id,
        target_digest: targetBinding.target_digest,
      },
      expect.objectContaining({ idempotencyKey: expect.any(String) }),
    );
    expect(await screen.findByRole("heading", { name: "已发布为权威约束" })).toBeVisible();
    expect(screen.getByText("ref-transition:1")).toBeVisible();
    expect(screen.getByText(/revision 5/)).toBeVisible();
  });

  it("renders the exact conflict_set_id without fabricating a Patch route", async () => {
    const conflict = new ApiProblemError(
      problem({ conflict_set_id: "conflict:set/42", detail: "Exact ref changed." }),
    );
    const approval = approvalView("approved");
    const proposalApi = api({
      getApproval: vi.fn(async () => ({ etag: '"approval:approved"', value: approval })),
      getApprovalBinding: vi.fn(async () => approvalBinding("approved")),
      getConstraintProposal: vi.fn(async () => ({
        etag: '"proposal:approved"',
        value: proposal("human", "artifact:proposal:1", 1, "approved"),
      })),
      publishConstraint: vi.fn(async () => {
        throw conflict;
      }),
    });
    const user = userEvent.setup();
    renderPage(proposalApi);
    await screen.findByText("确定性证据：validated");
    await user.click(screen.getByRole("button", { name: "发布权威约束" }));

    expect(await screen.findByText("Exact ref changed.")).toBeVisible();
    expect(screen.getByText("conflict:set/42")).toBeVisible();
    expect(screen.getByRole("note")).toHaveTextContent("没有 exact Patch Artifact ID");
    expect(screen.queryByRole("link", { name: "交给 Patch 冲突处理" })).not.toBeInTheDocument();
  });

  it("locks stale mutation state after a transport error until an explicit server reread", async () => {
    const approval = approvalView("approved");
    const successful = {
      approval,
      ref_name: targetBinding.ref_name,
      ref_transition_id: "ref-transition:after-reread",
      ref_value: { artifact_id: targetBinding.target_artifact_id, revision: 6 },
      result_schema_version: "workflow-apply-result@1" as const,
      reversed_approval_id: null,
    };
    const publishConstraint = vi
      .fn<ConstraintProposalApi["publishConstraint"]>()
      .mockRejectedValueOnce(new Error("network timeout must-not-render"))
      .mockResolvedValueOnce(successful);
    const proposalApi = api({
      getApproval: vi.fn(async () => ({ etag: '"approval:approved"', value: approval })),
      getApprovalBinding: vi.fn(async () => approvalBinding("approved")),
      getConstraintProposal: vi.fn(async () => ({
        etag: '"proposal:approved"',
        value: proposal("human", "artifact:proposal:1", 1, "approved"),
      })),
      publishConstraint,
    });
    const user = userEvent.setup();
    renderPage(proposalApi);
    await screen.findByText("确定性证据：validated");

    const publish = screen.getByRole("button", { name: "发布权威约束" });
    await user.click(publish);
    expect(await screen.findByRole("heading", { name: "工作流命令失败" })).toBeVisible();
    expect(screen.queryByText(/network timeout/)).not.toBeInTheDocument();
    expect(publish).toBeDisabled();
    await user.click(publish);
    expect(publishConstraint).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "重新读取服务器状态" }));
    await waitFor(() => expect(publish).toBeEnabled());
    await user.click(publish);
    expect(publishConstraint).toHaveBeenCalledTimes(2);
    const firstIntent = publishConstraint.mock.calls[0]?.[2].idempotencyKey;
    const secondIntent = publishConstraint.mock.calls[1]?.[2].idempotencyKey;
    expect(firstIntent).not.toBe(secondIntent);
  });

  it("shows safe loading, Problem, and missing-binding states", async () => {
    const missing = new ApiProblemError(
      problem({ code: "not_found", detail: "No retained binding.", status: 404, title: "Not found" }),
    );
    renderPage(
      api({
        getApprovalBinding: vi.fn(async () => {
          throw missing;
        }),
      }),
    );
    expect(screen.getByRole("heading", { name: "正在读取约束候选" })).toBeVisible();
    expect(await screen.findByRole("heading", { name: "审批绑定缺失" })).toBeVisible();

    const problemApi = api({
      getConstraintProposal: vi.fn(async () => {
        throw new ApiProblemError(problem({ detail: "Readable safe detail.", status: 500 }));
      }),
    });
    const view = renderPage(problemApi);
    expect(await screen.findByText("Readable safe detail.")).toBeVisible();
    view.unmount();
  });
});
