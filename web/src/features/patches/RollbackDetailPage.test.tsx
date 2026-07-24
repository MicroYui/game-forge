import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { createQueryClient } from "../../api/query-client";
import type {
  ApprovalView,
  PatchWorkflowApi,
  RollbackRequestReadView,
  SubjectApprovalBindingView,
} from "./api";
import { RollbackDetailPage } from "./RollbackDetailPage";

type ApprovalStatus = components["schemas"]["ApprovalItem"]["status"];
type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];

const ROLLBACK_ID = "artifact:rollback:detail";
const APPROVAL_ID = "approval:rollback:detail";
const REF_NAME = "spec/main";
const TARGET_ID = "artifact:spec:baseline";
const CURRENT_ID = "artifact:spec:current";
const TARGET_SNAPSHOT = "snapshot:baseline";
const CURRENT_SNAPSHOT = "snapshot:current";
const SUBJECT_DIGEST = "a".repeat(64);
const TARGET_DIGEST = "b".repeat(64);

const rollbackProfileBinding = {
  catalog_digest: "c".repeat(64),
  catalog_version: 4,
  expected_profile_kind: "rollback" as const,
  field_path: "/params/rollback_profile",
  profile: { profile_id: "builtin.rollback", version: 3 },
  profile_payload_hash: "d".repeat(64),
};

function artifactSummary(
  artifactId: string,
  kind: components["schemas"]["ArtifactSummaryV1"]["kind"],
  payloadSchemaId: string,
) {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-20T06:00:00Z",
    domain_scope: { domain_ids: ["domain:economy"] },
    kind,
    lineage_schema_version: "lineage@2" as const,
    parent_artifact_ids: [],
    payload_hash:
      kind === "rollback_request"
        ? SUBJECT_DIGEST
        : artifactId === TARGET_ID
          ? TARGET_DIGEST
          : "e".repeat(64),
    payload_schema_id: payloadSchemaId,
    summary_schema_version: "artifact-summary@1" as const,
    version_tuple: {
      ir_snapshot_id: artifactId === CURRENT_ID ? CURRENT_SNAPSHOT : TARGET_SNAPSHOT,
    },
  };
}

function rollbackView(status: ApprovalStatus): RollbackRequestReadView {
  return {
    approval_status: status,
    artifact: {
      ...artifactSummary(ROLLBACK_ID, "rollback_request", "rollback-request@1"),
      parent_artifact_ids: [CURRENT_ID, TARGET_ID],
    },
    request: {
      expected_current_ref: { artifact_id: CURRENT_ID, revision: 2 },
      reason: "Restore the reviewed economy baseline.",
      ref_name: REF_NAME,
      reverses_approval_id: "approval:patch:bad",
      rollback_profile_binding: rollbackProfileBinding,
      rollback_schema_version: "rollback-request@1",
      target_artifact_id: TARGET_ID,
      target_history_revision: 1,
    },
    view_schema_version: "rollback-request-read-view@1",
    workflow_revision: status === "applied" ? 6 : 5,
  };
}

function binding(status: ApprovalStatus): SubjectApprovalBindingView {
  return {
    approval_id: APPROVAL_ID,
    approval_status: status,
    is_current_head: true,
    subject_artifact_id: ROLLBACK_ID,
    subject_digest: SUBJECT_DIGEST,
    subject_head_revision: 1,
    subject_kind: "rollback_request",
    subject_revision: 1,
    subject_series_id: "rollback-series:detail",
    workflow_revision: status === "applied" ? 6 : 5,
  };
}

function approval(status: ApprovalStatus): ApprovalView {
  const hasEvidence = ["validated", "pending_approval", "approved", "applied"].includes(status);
  return {
    approval: {
      active_validation_run_id: null,
      applied_at: status === "applied" ? "2026-07-20T06:30:00Z" : null,
      approval_id: APPROVAL_ID,
      approval_policy: { policy_digest: "f".repeat(64), policy_version: "1" },
      approval_schema_version: "approval@1",
      auto_apply_proof: null,
      created_at: "2026-07-20T06:00:00Z",
      decided_at: status === "approved" || status === "applied" ? "2026-07-20T06:20:00Z" : null,
      decisions: [],
      domain_registry_ref: { registry_digest: "1".repeat(64), registry_version: "1" },
      domain_scope: { domain_ids: ["domain:economy"] },
      evidence_set_artifact_id: hasEvidence ? "artifact:evidence:rollback" : null,
      last_validation_failure_artifact_id: null,
      proposer: { principal_id: "principal:maker", principal_kind: "human" },
      regression_evidence_artifact_ids: hasEvidence ? ["artifact:regression:rollback"] : [],
      requirements: [],
      role_policy_digest: "2".repeat(64),
      role_policy_version: "1",
      route_policy: {
        domain_registry_ref: { registry_digest: "1".repeat(64), registry_version: "1" },
        route_digest: "3".repeat(64),
        route_version: "1",
      },
      status,
      subject_artifact_id: ROLLBACK_ID,
      subject_digest: SUBJECT_DIGEST,
      subject_kind: "rollback_request",
      subject_revision: 1,
      subject_series_id: "rollback-series:detail",
      submitted_at: ["pending_approval", "approved", "applied"].includes(status)
        ? "2026-07-20T06:15:00Z"
        : null,
      supersedes_approval_id: null,
      target_binding: {
        binding_schema_version: "approval-target-binding@1",
        expected_ref: { artifact_id: CURRENT_ID, revision: 2 },
        ref_name: REF_NAME,
        rollback_profile_binding: rollbackProfileBinding,
        subject_kind: "rollback_request",
        target_artifact_id: TARGET_ID,
        target_artifact_kind: "ir_snapshot",
        target_digest: TARGET_DIGEST,
        target_snapshot_id: TARGET_SNAPSHOT,
      },
      workflow_revision: status === "applied" ? 6 : 5,
    },
    current_actor_allowed_requirement_ids: [],
    requirement_progress: [],
    view_schema_version: "approval-view@1",
  };
}

function profile(kind: "schema_compatibility" | "impact_analysis"): ExecutionProfile {
  return {
    compatible_run_kinds: [{ kind: "rollback.validate", version: 1 }],
    display_name: `builtin.${kind}`,
    domain_scope: { domain_ids: ["domain:economy"] },
    env_contract_version: null,
    input_schema_ids: [],
    output_schema_ids: [],
    profile: { profile_id: `builtin.${kind}`, version: 1 },
    profile_kind: kind,
    profile_payload_hash: "4".repeat(64),
    required_capabilities: [],
    status: "active",
    stochastic: false,
    target_environment_profile: null,
  };
}

const frozenRollbackProfile: ExecutionProfile = {
  compatible_run_kinds: [{ kind: "rollback.validate", version: 1 }],
  display_name: "builtin.rollback",
  domain_scope: { domain_ids: ["domain:economy"] },
  env_contract_version: null,
  input_schema_ids: [],
  output_schema_ids: [],
  profile: { profile_id: "builtin.rollback", version: 3 },
  profile_kind: "rollback",
  profile_payload_hash: "d".repeat(64),
  required_capabilities: [],
  status: "active",
  stochastic: false,
  target_environment_profile: null,
};

function page<T>(items: T[], snapshot: string) {
  return {
    expires_at: "2026-07-20T08:00:00Z",
    items,
    next_cursor: null,
    page_schema_version: "page@1" as const,
    read_snapshot_id: snapshot,
  };
}

function api(initialStatus: ApprovalStatus, overrides: Partial<PatchWorkflowApi> = {}): PatchWorkflowApi {
  let status = initialStatus;
  let applied = initialStatus === "applied";
  const profileItems = [profile("schema_compatibility"), profile("impact_analysis")];
  const workflowApi = {
    getApproval: vi.fn(async () => ({ etag: '"approval:5"', value: approval(status) })),
    getApprovalBinding: vi.fn(async () => binding(status)),
    getArtifact: vi.fn(async (artifactId) => ({
      artifact:
        artifactId === TARGET_ID || artifactId === CURRENT_ID
          ? artifactSummary(artifactId, "ir_snapshot", "ir-core@1")
          : artifactSummary(
              artifactId,
              artifactId.includes("regression") ? "regression_evidence" : "validation_evidence",
              artifactId.includes("regression") ? "regression-evidence@1" : "evidence-set@1",
            ),
      payload:
        artifactId === TARGET_ID
          ? { economy: { reward_gold: 80 }, title: "稳定经济基线" }
          : artifactId === CURRENT_ID
            ? { economy: { reward_gold: 120 }, title: "当前高奖励版本" }
            : {},
      resource_revision: 1,
      view_schema_version: "artifact-payload-view@1" as const,
    })),
    getExecutionProfile: vi.fn(async () => frozenRollbackProfile),
    getSnapshotDiff: vi.fn(async (baseSnapshotId, targetSnapshotId) => ({
      diff: {
        base_snapshot_id: baseSnapshotId,
        diff_schema_version: "snapshot-diff@1" as const,
        entry_count: 1,
        target_snapshot_id: targetSnapshotId,
      },
      page: page(
        [
          {
            after: { presence: "present" as const, value: 80 },
            before: { presence: "present" as const, value: 120 },
            path: "/economy/reward_gold",
          },
        ],
        "diff:rollback",
      ),
      page_schema_version: "snapshot-diff-http-page@1" as const,
    })),
    getRollbackRequest: vi.fn(async () => ({
      etag: '"rollback:5"',
      value: rollbackView(status),
    })),
    listArtifacts: vi.fn(async (kind) =>
      page(
        kind === "regression_suite"
          ? [artifactSummary("artifact:regression-suite:1", "regression_suite", "regression-suite@1")]
          : [],
        `artifacts:${kind}`,
      ),
    ),
    listExecutionProfiles: vi.fn(async (filters) =>
      page(
        profileItems.filter((candidate) => candidate.profile_kind === filters.profile_kind),
        `profiles:${filters.profile_kind}`,
      ),
    ),
    listLineage: vi.fn(async () =>
      page(
        [
          {
            artifact: artifactSummary("artifact:source:baseline", "source_raw", "source@1"),
            depth: 1,
            entry_schema_version: "lineage-entry@1" as const,
          },
        ],
        "lineage:target",
      ),
    ),
    listRefHistory: vi.fn(async () =>
      page(
        [
          {
            entry_schema_version: "ref-history-entry@1" as const,
            ref_name: REF_NAME,
            value: { artifact_id: TARGET_ID, revision: 1 },
          },
          {
            entry_schema_version: "ref-history-entry@1" as const,
            ref_name: REF_NAME,
            value: { artifact_id: CURRENT_ID, revision: 2 },
          },
          ...(applied
            ? [
                {
                  entry_schema_version: "ref-history-entry@1" as const,
                  ref_name: REF_NAME,
                  value: { artifact_id: TARGET_ID, revision: 3 },
                },
              ]
            : []),
        ],
        applied ? "history:after" : "history:before",
      ),
    ),
    ...overrides,
  } as unknown as PatchWorkflowApi;
  if (!overrides.applyRollback) {
    workflowApi.applyRollback = vi.fn(async () => {
      status = "applied";
      applied = true;
      return {
        approval: approval("applied"),
        ref_name: REF_NAME,
        ref_transition_id: "ref-transition:sha256:rollback",
        ref_value: { artifact_id: TARGET_ID, revision: 3 },
        result_schema_version: "workflow-apply-result@1" as const,
        reversed_approval_id: "approval:patch:bad",
      };
    });
  }
  return workflowApi;
}

function renderPage(api: PatchWorkflowApi) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <RollbackDetailPage api={api} artifactId={ROLLBACK_ID} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Rollback detail", () => {
  it("validates the frozen rollback target with exact schema and impact profiles", async () => {
    const user = userEvent.setup();
    const accepted = {
      accepted_schema_version: "run-accepted@1" as const,
      events_url: "/api/v1/runs/run:rollback-validation/events",
      run_id: "run:rollback-validation",
      status_url: "/api/v1/runs/run:rollback-validation",
    };
    const validateRollback = vi.fn<PatchWorkflowApi["validateRollback"]>(async () => accepted);
    const workflowApi = api("draft", { validateRollback });
    renderPage(workflowApi);

    await screen.findByRole("heading", { name: "Rollback validation" });
    expect(screen.getByRole("heading", { name: "回滚后会改变什么" })).toBeVisible();
    expect(screen.getByText("/economy/reward_gold")).toBeVisible();
    expect(screen.getByText(/120 → 80/)).toBeVisible();
    expect(workflowApi.getSnapshotDiff).toHaveBeenCalledWith(CURRENT_SNAPSHOT, TARGET_SNAPSHOT, null);
    expect(screen.getByRole("heading", { name: "Workflow evidence Artifact ledger" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "确定性预言机" })).not.toBeInTheDocument();
    await user.selectOptions(
      screen.getByLabelText("Schema compatibility policy"),
      "builtin.schema_compatibility@1",
    );
    await user.click(screen.getByRole("checkbox", { name: /builtin.impact_analysis@1/ }));
    await user.type(screen.getByRole("searchbox", { name: "搜索回归套件" }), "regression-suite");
    await user.click(screen.getByRole("checkbox", { name: /回归套件.*regression-suite@1/ }));
    await user.clear(screen.getByLabelText("Seed"));
    await user.type(screen.getByLabelText("Seed"), "17");
    await user.click(screen.getByRole("button", { name: "启动 rollback validation" }));

    await waitFor(() => expect(validateRollback).toHaveBeenCalledTimes(1));
    expect(validateRollback.mock.calls[0][1]).toEqual({
      approval_id: APPROVAL_ID,
      expected_current_ref: { artifact_id: CURRENT_ID, revision: 2 },
      expected_subject_head_revision: 1,
      expected_workflow_revision: 5,
      impact_profiles: [{ profile_id: "builtin.impact_analysis", version: 1 }],
      ref_name: REF_NAME,
      regression_suite_artifact_ids: ["artifact:regression-suite:1"],
      request_schema_version: "rollback-validation-admission-request@1",
      rollback_profile: { profile_id: "builtin.rollback", version: 3 },
      schema_compatibility_policy: { profile_id: "builtin.schema_compatibility", version: 1 },
      seed: 17,
      subject_digest: SUBJECT_DIGEST,
      target_artifact_id: TARGET_ID,
      target_history_revision: 1,
    });
    expect(await screen.findByRole("link", { name: "打开 accepted Run" })).toHaveAttribute(
      "href",
      "/runs/run%3Arollback-validation",
    );
  });

  it("sends seed=null when every resolved validation profile is deterministic", async () => {
    const user = userEvent.setup();
    const accepted = {
      accepted_schema_version: "run-accepted@1" as const,
      events_url: "/api/v1/runs/run:deterministic/events",
      run_id: "run:deterministic",
      status_url: "/api/v1/runs/run:deterministic",
    };
    const validateRollback = vi.fn<PatchWorkflowApi["validateRollback"]>(async () => accepted);
    renderPage(api("draft", { validateRollback }));

    await user.selectOptions(
      await screen.findByLabelText("Schema compatibility policy"),
      "builtin.schema_compatibility@1",
    );
    await user.click(screen.getByRole("button", { name: "启动 rollback validation" }));

    await waitFor(() => expect(validateRollback).toHaveBeenCalledTimes(1));
    expect(validateRollback.mock.calls[0][1].seed).toBeNull();
    const acceptedRun = await screen.findByRole("link", { name: "打开 accepted Run" });
    expect(acceptedRun.closest('[role="status"]')).not.toBeNull();
  });

  it("submits validated evidence to the independent approval workflow", async () => {
    const user = userEvent.setup();
    const submitRollbackForApproval = vi.fn<PatchWorkflowApi["submitRollbackForApproval"]>(async () =>
      approval("pending_approval"),
    );
    renderPage(api("validated", { submitRollbackForApproval }));

    await user.click(await screen.findByRole("button", { name: "提交独立人工审批" }));

    await waitFor(() => expect(submitRollbackForApproval).toHaveBeenCalledTimes(1));
    expect(submitRollbackForApproval.mock.calls[0][1]).toEqual({
      approval_id: APPROVAL_ID,
      expected_workflow_revision: 5,
      request_schema_version: "submit-for-approval-request@1",
    });
    expect(screen.getByRole("link", { name: "打开 Approval" })).toHaveAttribute(
      "href",
      "/approvals/approval%3Arollback%3Adetail",
    );
  });

  it("applies only the frozen binding, appends ref history, and does not invent content lineage", async () => {
    const user = userEvent.setup();
    const workflowApi = api("approved");
    renderPage(workflowApi);

    expect(await screen.findByText("artifact:source:baseline")).toBeVisible();
    expect(screen.getByText(/RefTransition is not a content-lineage edge/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Apply approved rollback" }));
    await user.click(screen.getByRole("button", { name: "确认 Apply rollback" }));

    await waitFor(() => expect(workflowApi.applyRollback).toHaveBeenCalledTimes(1));
    expect(vi.mocked(workflowApi.applyRollback).mock.calls[0][1]).toEqual({
      approval_id: APPROVAL_ID,
      expected_ref: { artifact_id: CURRENT_ID, revision: 2 },
      expected_workflow_revision: 5,
      ref_name: REF_NAME,
      request_schema_version: "workflow-apply-request@1",
      subject_digest: SUBJECT_DIGEST,
      target_artifact_id: TARGET_ID,
      target_digest: TARGET_DIGEST,
    });
    expect(await screen.findByText("Current · revision 3")).toBeVisible();
    const receiptHeading = screen.getByRole("heading", {
      name: "Rollback 已通过 ref transition 应用",
    });
    expect(receiptHeading.closest('[role="status"]')).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Independent approval & apply" })).toHaveFocus();
    expect(screen.getByText("ref-transition:sha256:rollback")).toBeVisible();
    expect(screen.getByText("artifact:source:baseline")).toBeVisible();
    expect(workflowApi.listLineage).toHaveBeenCalledTimes(2);
  });
});
