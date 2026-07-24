import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { createQueryClient } from "../../api/query-client";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError, type SafeProblem } from "../../api/problem";
import { ToastProvider } from "../../app/providers";
import { compileStructuredOperations, createStructuredOperation } from "../specs/StructuredPatchEditor";
import { ApprovalDetailPage, ApprovalsPage } from "./ApprovalsPage";
import type {
  ApprovalAction,
  ApprovalArtifactPayload,
  ApprovalConstraintProposal,
  ApprovalPageData,
  ApprovalPatch,
  ApprovalRollbackRequest,
  ApprovalsApi,
  ApprovalViewData,
  VersionedApproval,
} from "./api";

function approvalView(): ApprovalViewData {
  return {
    approval: {
      approval_id: "approval:multi-domain:7",
      approval_policy: { policy_digest: "a".repeat(64), policy_version: "approval-policy@1" },
      approval_schema_version: "approval@1",
      created_at: "2026-07-20T02:00:00Z",
      decisions: [
        {
          actor: { principal_id: "human:charlie", principal_kind: "human" },
          comment: "经济回归证据已复核。",
          decision: "approve",
          decision_id: "decision:immutable:1",
          expected_workflow_revision: 11,
          occurred_at: "2026-07-20T02:30:00Z",
          reason_code: "evidence_reviewed",
          requirement_ids: ["requirement:economy"],
        },
      ],
      domain_registry_ref: { registry_digest: "b".repeat(64), registry_version: "domains@7" },
      domain_scope: { domain_ids: ["domain:economy", "domain:narrative"] },
      evidence_set_artifact_id: "artifact:evidence:7",
      last_validation_failure_artifact_id: null,
      proposer: { principal_id: "human:alice", principal_kind: "human" },
      regression_evidence_artifact_ids: ["artifact:regression:7"],
      requirements: [
        {
          assignee_principal_ids: ["human:bob"],
          distinct_from_requirement_ids: ["requirement:narrative"],
          domain_scope: { domain_ids: ["domain:economy"] },
          min_approvals: 2,
          required_permission: {
            action: "approve",
            domain_scope: { domain_ids: ["domain:economy"] },
            resource_kind: "patch",
          },
          requirement_id: "requirement:economy",
          route_role: "numeric_designer",
        },
        {
          assignee_principal_ids: [],
          distinct_from_requirement_ids: ["requirement:economy"],
          domain_scope: { domain_ids: ["domain:narrative"] },
          min_approvals: 1,
          required_permission: {
            action: "approve",
            domain_scope: { domain_ids: ["domain:narrative"] },
            resource_kind: "patch",
          },
          requirement_id: "requirement:narrative",
          route_role: "content_designer",
        },
      ],
      role_policy_digest: "c".repeat(64),
      role_policy_version: "roles@9",
      route_policy: {
        domain_registry_ref: { registry_digest: "b".repeat(64), registry_version: "domains@7" },
        route_digest: "d".repeat(64),
        route_version: "routes@4",
      },
      status: "pending_approval",
      subject_artifact_id: "artifact:patch:7",
      subject_digest: "e".repeat(64),
      subject_kind: "patch",
      subject_revision: 3,
      subject_series_id: "patch-series:7",
      submitted_at: "2026-07-20T02:15:00Z",
      target_binding: {
        binding_schema_version: "approval-target-binding@1",
        expected_ref: { artifact_id: "artifact:snapshot:6", revision: 6 },
        ref_name: "refs/design/live",
        subject_kind: "patch",
        target_artifact_id: "artifact:snapshot:7",
        target_artifact_kind: "ir_snapshot",
        target_digest: "f".repeat(64),
        target_snapshot_id: "snapshot:7",
      },
      workflow_revision: 12,
    },
    current_actor_allowed_requirement_ids: ["requirement:economy", "requirement:narrative"],
    requirement_progress: [
      {
        decision_eligibility: [
          { decision: "approve", eligible: true, reason_codes: [] },
          { decision: "reject", eligible: true, reason_codes: [] },
          { decision: "request_changes", eligible: true, reason_codes: [] },
        ],
        domain_scope: { domain_ids: ["domain:economy"] },
        eligible_for_current_actor: true,
        min_approvals: 2,
        requirement_id: "requirement:economy",
        route_role: "numeric_designer",
        satisfied: false,
        unmet_distinct_from_requirement_ids: ["requirement:narrative"],
        valid_approval_count: 1,
      },
      {
        decision_eligibility: [
          {
            decision: "approve",
            eligible: false,
            reason_codes: ["distinct_requirement_conflict"],
          },
          { decision: "reject", eligible: true, reason_codes: [] },
          { decision: "request_changes", eligible: true, reason_codes: [] },
        ],
        domain_scope: { domain_ids: ["domain:narrative"] },
        eligible_for_current_actor: true,
        min_approvals: 1,
        requirement_id: "requirement:narrative",
        route_role: "content_designer",
        satisfied: false,
        unmet_distinct_from_requirement_ids: ["requirement:economy"],
        valid_approval_count: 0,
      },
    ],
    view_schema_version: "approval-view@1",
  } as ApprovalViewData;
}

function patchSubject(view = approvalView()): ApprovalPatch {
  return {
    approval_status: view.approval.status,
    artifact: {
      artifact_id: view.approval.subject_artifact_id,
      created_at: "2026-07-20T02:00:00Z",
      domain_scope: view.approval.domain_scope,
      kind: "patch",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: ["artifact:snapshot:6"],
      payload_hash: view.approval.subject_digest,
      payload_schema_id: "patch@2",
      summary_schema_version: "artifact-summary@1",
      version_tuple: { ir_snapshot_id: "snapshot:7", tool_version: "patch@2" },
    },
    patch: {
      base_snapshot_id: "snapshot:6",
      expected_to_fix: ["金币奖励上限不一致"],
      ops: [
        {
          new_value: 80,
          old_value: 120,
          op: "set_entity_attr",
          op_id: "op:reward-cap",
          target: "quest:side-01.reward_gold",
        },
      ],
      patch_schema_version: "patch@2",
      preconditions: [{ path: "quest:side-01.reward_gold", value: 120 }],
      produced_by: "human",
      producer_run_id: null,
      rationale: "把支线任务奖励金币上限修正为 80。",
      revision: view.approval.subject_revision,
      side_effect_risk: "low",
      supersedes_artifact_id: "artifact:patch:6",
      target_snapshot_id: "snapshot:7",
    },
    regression_status: "passed",
    validation_status: "passed",
    view_schema_version: "patch-artifact-read-view@1",
    workflow_revision: view.approval.workflow_revision,
  };
}

function evidenceSubject(view = approvalView()): ApprovalArtifactPayload {
  return {
    artifact: {
      artifact_id: view.approval.evidence_set_artifact_id!,
      created_at: "2026-07-20T02:10:00Z",
      domain_scope: view.approval.domain_scope,
      kind: "validation_evidence",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: [view.approval.subject_artifact_id],
      payload_hash: "9".repeat(64),
      payload_schema_id: "evidence-set@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: { tool_version: "validation@1" },
    },
    payload: {
      evidence_schema_version: "evidence-set@1",
      finding_bindings: [],
      overall_status: "passed",
      policy_version: "validation@1",
      requirements: [
        {
          applicability: "required",
          evidence_artifact_id: "artifact:checker:7",
          kind: "checker",
          reason_code: null,
          requirement_id: "checker:deterministic",
          status: "passed",
          tool_version: "checker@1",
        },
      ],
      subject_artifact_id: view.approval.subject_artifact_id,
      subject_digest: view.approval.subject_digest,
      supporting_artifact_ids: [],
      target_binding: view.approval.target_binding,
      validation_run_id: "run:validation:7",
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function patchTargetSubject(view = approvalView()): ApprovalArtifactPayload {
  return {
    artifact: {
      artifact_id: view.approval.target_binding!.target_artifact_id,
      created_at: "2026-07-20T02:05:00Z",
      domain_scope: view.approval.domain_scope,
      kind: "ir_snapshot",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: ["artifact:snapshot:6"],
      payload_hash: view.approval.target_binding!.target_digest,
      payload_schema_id: "ir-core@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: { ir_snapshot_id: "snapshot:7", tool_version: "patch@2" },
    },
    payload: {
      entities: {
        "npc:lincheng": { attrs: { name: "林澈" }, schema_version: "ir-core@1", type: "NPC" },
        "npc:linyi": { attrs: { name: "林逸" }, schema_version: "ir-core@1", type: "NPC" },
      },
      meta_schema_version: "meta@1",
      relations: {},
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function constraintApprovalView(): ApprovalViewData {
  const view = approvalView();
  view.approval.subject_artifact_id = "artifact:constraint-proposal:7";
  view.approval.subject_digest = "7".repeat(64);
  view.approval.subject_kind = "constraint_proposal";
  view.approval.subject_revision = 2;
  view.approval.target_binding = {
    binding_schema_version: "approval-target-binding@1",
    expected_ref: null,
    ref_name: "constraints/head",
    subject_kind: "constraint_proposal",
    target_artifact_id: "artifact:constraint:snapshot:7",
    target_artifact_kind: "constraint_snapshot",
    target_digest: "8".repeat(64),
    target_snapshot_id: "constraint:snapshot:7",
  };
  return view;
}

function constraintSubject(view = constraintApprovalView()): ApprovalConstraintProposal {
  return {
    approval_status: view.approval.status,
    artifact: {
      ...patchSubject().artifact,
      artifact_id: view.approval.subject_artifact_id,
      kind: "constraint_proposal",
      payload_hash: view.approval.subject_digest,
      payload_schema_id: "constraint-proposal@1",
    },
    proposal: {
      base_constraint_snapshot_id: null,
      constraints: [
        {
          assert: "reward_gold <= 80",
          dsl_grammar_version: "dsl@1",
          id: "side_quest_reward_gold_cap",
          kind: "numeric",
          note: "支线任务奖励金币不得超过 80。",
          oracle: "deterministic",
          predicates: [],
          scope: { node_type: "QUEST", var: "q", where: {} },
          severity: "major",
        },
      ],
      domain_scope: { domain_ids: ["domain:economy"] },
      dsl_grammar_version: "dsl@1",
      produced_by: "human",
      producer_run_id: null,
      proposal_schema_version: "constraint-proposal@1",
      rationale: "限制支线任务金币奖励。",
      revision: view.approval.subject_revision,
      source_bindings: [],
      supersedes_artifact_id: "artifact:constraint-proposal:6",
    },
    view_schema_version: "constraint-proposal-read-view@1",
    workflow_revision: view.approval.workflow_revision,
  };
}

const rollbackProfileBinding = {
  catalog_digest: "3".repeat(64),
  catalog_version: 4,
  expected_profile_kind: "rollback" as const,
  field_path: "/params/rollback_profile",
  profile: { profile_id: "rollback.safe", version: 2 },
  profile_payload_hash: "4".repeat(64),
};

function rollbackApprovalView(): ApprovalViewData {
  const view = approvalView();
  view.approval.subject_artifact_id = "artifact:rollback:7";
  view.approval.subject_digest = "5".repeat(64);
  view.approval.subject_kind = "rollback_request";
  view.approval.subject_revision = 1;
  view.approval.subject_series_id = "rollback-series:7";
  view.approval.target_binding = {
    binding_schema_version: "approval-target-binding@1",
    expected_ref: { artifact_id: "artifact:snapshot:current", revision: 9 },
    ref_name: "refs/design/live",
    rollback_profile_binding: rollbackProfileBinding,
    subject_kind: "rollback_request",
    target_artifact_id: "artifact:snapshot:history-4",
    target_artifact_kind: "ir_snapshot",
    target_digest: "6".repeat(64),
    target_snapshot_id: "snapshot:history-4",
  };
  return view;
}

function rollbackSubject(view = rollbackApprovalView()): ApprovalRollbackRequest {
  const target = view.approval.target_binding!;
  if (target.subject_kind !== "rollback_request") throw new Error("rollback target expected");
  return {
    approval_status: view.approval.status,
    artifact: {
      artifact_id: view.approval.subject_artifact_id,
      created_at: "2026-07-20T02:00:00Z",
      domain_scope: view.approval.domain_scope,
      kind: "rollback_request",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: [target.expected_ref.artifact_id, target.target_artifact_id],
      payload_hash: view.approval.subject_digest,
      payload_schema_id: "rollback-request@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: { ir_snapshot_id: target.target_snapshot_id, tool_version: "rollback@1" },
    },
    request: {
      expected_current_ref: target.expected_ref,
      reason: "恢复经过验证的商队任务基线。",
      ref_name: target.ref_name,
      reverses_approval_id: "approval:patch:unsafe",
      rollback_profile_binding: rollbackProfileBinding,
      rollback_schema_version: "rollback-request@1",
      target_artifact_id: target.target_artifact_id,
      target_history_revision: 4,
    },
    view_schema_version: "rollback-request-read-view@1",
    workflow_revision: view.approval.workflow_revision,
  };
}

function rollbackSnapshot(view: ApprovalViewData, side: "current" | "target"): ApprovalArtifactPayload {
  const target = view.approval.target_binding!;
  if (target.subject_kind !== "rollback_request") throw new Error("rollback target expected");
  const current = side === "current";
  return {
    artifact: {
      artifact_id: current ? target.expected_ref.artifact_id : target.target_artifact_id,
      created_at: "2026-07-20T01:00:00Z",
      domain_scope: view.approval.domain_scope,
      kind: "ir_snapshot",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: [],
      payload_hash: current ? "7".repeat(64) : target.target_digest,
      payload_schema_id: "ir-core@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: {
        ir_snapshot_id: current ? "snapshot:current" : target.target_snapshot_id,
        tool_version: "ir@1",
      },
    },
    payload: current
      ? {
          entities: {
            "npc:lincheng": {
              attrs: { name: "林澈" },
              schema_version: "ir-core@1",
              type: "NPC",
            },
            "npc:linyi": {
              attrs: { name: "林逸" },
              schema_version: "ir-core@1",
              type: "NPC",
            },
            "quest:caravan": {
              attrs: { name: "失踪的商队", reward_gold: 120 },
              schema_version: "ir-core@1",
              type: "QUEST",
            },
          },
          meta_schema_version: "meta@1",
          relations: {
            "relation:quest-owner": {
              attrs: { label: "负责" },
              dst_id: "quest:caravan",
              id: "relation:quest-owner",
              schema_version: "ir-core@1",
              src_id: "npc:lincheng",
              type: "OWNS",
            },
          },
        }
      : {
          entities: {
            "npc:lincheng": {
              attrs: { name: "林澈" },
              schema_version: "ir-core@1",
              type: "NPC",
            },
            "npc:shenyue": {
              attrs: { name: "沈月" },
              schema_version: "ir-core@1",
              type: "NPC",
            },
            "quest:caravan": {
              attrs: { name: "失踪的商队", reward_gold: 80 },
              schema_version: "ir-core@1",
              type: "QUEST",
            },
          },
          meta_schema_version: "meta@1",
          relations: {
            "relation:quest-owner": {
              attrs: { label: "委托" },
              dst_id: "quest:caravan",
              id: "relation:quest-owner",
              schema_version: "ir-core@1",
              src_id: "npc:lincheng",
              type: "OWNS",
            },
          },
        },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function rollbackApi(
  view = rollbackApprovalView(),
  subject = rollbackSubject(view),
  current = rollbackSnapshot(view, "current"),
  target = rollbackSnapshot(view, "target"),
): ApprovalsApi {
  const evidence = evidenceSubject(view);
  const getArtifactPayload = vi.fn(async (artifactId: string) => {
    if (artifactId === view.approval.evidence_set_artifact_id) return evidence;
    if (artifactId === subject.request.expected_current_ref.artifact_id) return current;
    if (artifactId === view.approval.target_binding!.target_artifact_id) return target;
    throw new Error(`unexpected Artifact ${artifactId}`);
  });
  return api({
    getApproval: vi.fn(async () => versioned(view)),
    getArtifactPayload,
    getRollbackRequest: vi.fn(async () => subject),
  });
}

function versioned(value = approvalView(), etag = '"approval:opaque-12"'): VersionedApproval {
  return { etag, value };
}

function page(items: ApprovalViewData[] = [approvalView()]): ApprovalPageData {
  return {
    expires_at: "2026-07-20T03:00:00Z",
    items,
    next_cursor: null,
    page_schema_version: "page@1",
    read_snapshot_id: "snapshot:approval-list:1",
  };
}

function api(overrides: Partial<ApprovalsApi> = {}): ApprovalsApi {
  return {
    decide: vi.fn(async (current) => current),
    getApproval: vi.fn(async () => versioned()),
    getArtifactPayload: vi.fn(async (artifactId) =>
      artifactId === approvalView().approval.target_binding!.target_artifact_id
        ? patchTargetSubject()
        : evidenceSubject(),
    ),
    getConstraintProposal: vi.fn(),
    getPatch: vi.fn(async () => patchSubject()),
    getRollbackRequest: vi.fn(),
    listMine: vi.fn(async () => page()),
    ...overrides,
  };
}

function renderPage(ui: React.ReactNode) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <ToastProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function problem(status: 403 | 409, code: "forbidden" | "revision_conflict"): ApiProblemError {
  const value: SafeProblem = {
    code,
    conflict_set_id: null,
    detail:
      status === 403
        ? "The current principal no longer holds the frozen route role."
        : "The approval workflow revision changed.",
    earliest_cursor: null,
    instance: "/api/v1/approvals/approval:multi-domain:7:approve",
    request_id: `request:${status}`,
    retry_after_s: null,
    run_id: null,
    status,
    title: status === 403 ? "Forbidden" : "Revision conflict",
    trace_id: null,
    type: "about:blank",
  };
  return new ApiProblemError(value);
}

describe("ApprovalsPage", () => {
  it("lists only assignee=me authority with proposer, domain, workflow, progress, and action range", async () => {
    const approvalsApi = api();
    renderPage(<ApprovalsPage api={approvalsApi} />);

    expect(await screen.findByRole("heading", { level: 1, name: "审批队列" })).toBeVisible();
    expect(approvalsApi.listMine).toHaveBeenCalledWith(null);
    const table = await screen.findByRole("table", { name: "待我审批" });
    const row = within(table).getByRole("row", { name: /approval:multi-domain:7/ });
    expect(row).toHaveTextContent("human:alice");
    expect(row).toHaveTextContent("domain:economy");
    expect(row).toHaveTextContent("domain:narrative");
    expect(row).toHaveTextContent("待审批 · 流程版本 12");
    expect(row).toHaveTextContent("0 / 2 项职责已满足");
    expect(row).toHaveTextContent("批准 1 · 驳回 2 · 请修改 2");
    expect(within(row).getByRole("link", { name: "打开审批详情" })).toHaveAttribute(
      "href",
      "/approvals/approval%3Amulti-domain%3A7",
    );
  });

  it("keeps the loaded queue visible after a 410 and restarts only on explicit confirmation", async () => {
    const user = userEvent.setup();
    const staleCursor = "cursor:approval-list:stale";
    const cursorProblem: SafeProblem = {
      code: "cursor_expired",
      conflict_set_id: null,
      detail: "The approval queue read snapshot expired.",
      earliest_cursor: null,
      instance: "/api/v1/approvals",
      request_id: "request:approval-cursor",
      retry_after_s: null,
      run_id: null,
      status: 410,
      title: "Cursor expired",
      trace_id: null,
      type: "about:blank",
    };
    const firstPage = { ...page(), next_cursor: staleCursor };
    const restartedPage = {
      ...page([]),
      read_snapshot_id: "snapshot:approval-list:2",
    };
    const listMine = vi
      .fn<ApprovalsApi["listMine"]>()
      .mockResolvedValueOnce(firstPage)
      .mockRejectedValueOnce(new CursorExpiredError(cursorProblem, staleCursor))
      .mockResolvedValueOnce(restartedPage);
    renderPage(<ApprovalsPage api={api({ listMine })} />);

    const table = await screen.findByRole("table", { name: "待我审批" });
    await user.click(screen.getByRole("button", { name: "加载下一页" }));

    expect(await screen.findByText("分页游标已过期；现有行仅代表过期前已读取的快照。")).toBeVisible();
    expect(within(table).getByText("approval:multi-domain:7")).toBeVisible();
    expect(listMine).toHaveBeenNthCalledWith(1, null);
    expect(listMine).toHaveBeenNthCalledWith(2, staleCursor);

    await user.click(screen.getByRole("button", { name: "重新开始查询" }));
    await waitFor(() => expect(listMine).toHaveBeenNthCalledWith(3, null));
    expect(await screen.findByText("当前没有可处理的审批职责。")).toBeVisible();
  });

  it("bounds a 512-character Approval ID and keeps fixed queue columns keyboard-scrollable", async () => {
    const longApprovalId = `approval:${"x".repeat(512)}`;
    const longView = approvalView();
    longView.approval.approval_id = longApprovalId;
    renderPage(<ApprovalsPage api={api({ listMine: vi.fn(async () => page([longView])) })} />);

    const approvalId = await screen.findByText(longApprovalId);
    expect(approvalId).toHaveClass("gf-copyable__value--scrollable");
    expect(approvalId).toHaveAttribute("tabindex", "0");

    const row = approvalId.closest("tr");
    expect(row).not.toBeNull();
    for (const value of ["待审批 · 流程版本 12", "0 / 2 项职责已满足", "批准 1 · 驳回 2 · 请修改 2"]) {
      expect(within(row!).getByText(value)).toHaveClass("gf-approvals__nowrap");
      expect(within(row!).getByText(value)).toHaveAttribute("tabindex", "0");
    }
  });
});

describe("ApprovalDetailPage", () => {
  it("shows exact proposer, subject, frozen policies, requirement progress, and immutable decisions", async () => {
    renderPage(<ApprovalDetailPage api={api()} approvalId="approval:multi-domain:7" />);

    expect(await screen.findByRole("heading", { level: 1, name: "审批详情" })).toBeVisible();
    const authority = screen.getByRole("region", { name: "审批权威" });
    expect(authority).toHaveTextContent("human:alice");
    expect(authority).toHaveTextContent("artifact:patch:7");
    expect(within(authority).getByRole("link", { name: "打开受审对象" })).toHaveAttribute(
      "href",
      "/patches/artifact%3Apatch%3A7",
    );
    expect(authority).toHaveTextContent("domain:economy");
    expect(authority).toHaveTextContent("domain:narrative");

    const subjectReview = await screen.findByRole("region", { name: "受审内容与验证依据" });
    expect(subjectReview).toHaveTextContent("你正在批准什么");
    expect(subjectReview).toHaveTextContent("把支线任务奖励金币上限修正为 80。");
    expect(subjectReview).toHaveTextContent("未命名对象的金币奖励：120 → 80");
    expect(subjectReview).toHaveTextContent("quest:side-01.reward_gold");
    expect(subjectReview).toHaveTextContent("修改前");
    expect(subjectReview).toHaveTextContent("120");
    expect(subjectReview).toHaveTextContent("修改后");
    expect(subjectReview).toHaveTextContent("80");
    expect(within(subjectReview).getByRole("region", { name: "审批影响目标" })).toHaveTextContent(
      "refs/design/live",
    );
    expect(subjectReview).toHaveTextContent("确定性验证已通过");
    expect(subjectReview).toHaveTextContent("确定性检查");
    expect(subjectReview).toHaveTextContent("checker@1");

    const policies = screen.getByRole("region", { name: "冻结策略" });
    expect(policies).toHaveTextContent("approval-policy@1");
    expect(policies).toHaveTextContent("roles@9");
    expect(policies).toHaveTextContent("routes@4");
    expect(policies).toHaveTextContent("domains@7");
    expect(policies).toHaveTextContent("a".repeat(64));

    const requirements = screen.getByRole("table", { name: "审批职责进度" });
    const economy = within(requirements)
      .getByRole("checkbox", { name: "选择 数值策划 · 经济系统" })
      .closest("tr");
    expect(economy).not.toBeNull();
    expect(economy).toHaveTextContent("数值策划");
    expect(economy).toHaveTextContent("1 / 2");
    expect(economy).toHaveTextContent("requirement:narrative");
    expect(economy).toHaveTextContent("批准可用");
    const narrative = within(requirements)
      .getByRole("checkbox", { name: "选择 内容策划 · 叙事内容" })
      .closest("tr");
    expect(narrative).not.toBeNull();
    expect(narrative).toHaveTextContent("当前身份已覆盖与此 requirement 互斥的职责");
    expect(narrative).toHaveTextContent("驳回可用");
    expect(narrative).toHaveTextContent("请修改可用");

    const decisions = screen.getByRole("region", { name: "不可变决定记录" });
    expect(decisions).toHaveTextContent("decision:immutable:1");
    expect(decisions).toHaveTextContent("human:charlie");
    expect(decisions).toHaveTextContent("evidence_reviewed");
    expect(decisions).toHaveTextContent("经济回归证据已复核。");
  });

  it("summarizes new entities and relations in business language while retaining raw fields", async () => {
    const subject = patchSubject();
    subject.patch.ops = [
      {
        new_value: { attrs: { name: "林逸" }, type: "NPC" },
        op: "add_entity",
        op_id: "op:add-linyi",
        target: "npc:linyi",
      },
      {
        new_value: {
          attrs: { label: "好友", relationship_kind: "friend" },
          dst_id: "npc:lincheng",
          src_id: "npc:linyi",
          type: "ALLY_WITH",
        },
        op: "add_relation",
        op_id: "op:friend",
        target: "rel:friend",
      },
    ];
    renderPage(
      <ApprovalDetailPage
        api={api({ getPatch: vi.fn(async () => subject) })}
        approvalId="approval:multi-domain:7"
      />,
    );

    const review = await screen.findByRole("region", { name: "受审内容与验证依据" });
    expect(review).toHaveTextContent("新增 NPC「林逸」");
    expect(review).toHaveTextContent("好友：林逸 → 林澈");
    expect(within(review).getAllByText("查看原始字段")).toHaveLength(2);
    expect(within(review).getAllByText("查看技术标识")).toHaveLength(2);
  });

  it("names entity and relation field changes from the exact target snapshot", async () => {
    const subject = patchSubject();
    subject.patch.ops = [
      {
        new_value: 4,
        old_value: 3,
        op: "set_entity_attr",
        op_id: "op:step-count",
        target: "step:collect_emblem.count",
      },
      {
        new_value: 4,
        old_value: 3,
        op: "set_relation_attr",
        op_id: "op:travel-distance",
        target: "relation:step-location.distance",
      },
    ];
    const target = patchTargetSubject();
    target.payload = {
      entities: {
        "location:qingstone": {
          attrs: { name: "青石村" },
          schema_version: "ir-core@1",
          type: "LOCATION",
        },
        "step:collect_emblem": {
          attrs: { count: 4, name: "收集徽章步骤" },
          schema_version: "ir-core@1",
          type: "QUEST_STEP",
        },
      },
      meta_schema_version: "meta@1",
      relations: {
        "relation:step-location": {
          attrs: { distance: 4 },
          dst_id: "location:qingstone",
          id: "relation:step-location",
          schema_version: "ir-core@1",
          src_id: "step:collect_emblem",
          type: "LOCATED_IN",
        },
      },
    };
    renderPage(
      <ApprovalDetailPage
        api={api({
          getArtifactPayload: vi.fn(async (artifactId) =>
            artifactId === approvalView().approval.target_binding!.target_artifact_id
              ? target
              : evidenceSubject(),
          ),
          getPatch: vi.fn(async () => subject),
        })}
        approvalId="approval:multi-domain:7"
      />,
    );

    const review = await screen.findByRole("region", { name: "受审内容与验证依据" });
    const operations = within(review).getByRole("list", { name: "Patch 变更内容" });
    expect(operations).toHaveTextContent("收集徽章步骤的数量：3 → 4");
    expect(operations).toHaveTextContent("收集徽章步骤 位于 青石村的距离：3 → 4");
    const primarySummaries = within(operations)
      .getAllByRole("strong")
      .map((element) => element.textContent)
      .join(" ");
    expect(primarySummaries).not.toContain("step:collect_emblem.count");
    expect(primarySummaries).not.toContain("relation:step-location.distance");
    expect(within(operations).getByText("step:collect_emblem.count")).toBeInTheDocument();
    expect(within(operations).getByText("relation:step-location.distance")).toBeInTheDocument();
  });

  it("summarizes replace_subgraph affected objects and exact add/replace counts", async () => {
    const subject = patchSubject();
    subject.patch.ops = [
      {
        new_value: {
          entities: [
            { attrs: { name: "收集徽章任务", reward_gold: 80 }, id: "quest:emblem", type: "QUEST" },
            { attrs: { name: "林逸" }, id: "npc:linyi", type: "NPC" },
          ],
          relations: [
            {
              attrs: {},
              dst_id: "location:qingstone",
              id: "relation:quest-location",
              src_id: "quest:emblem",
              type: "LOCATED_IN",
            },
            {
              attrs: { label: "参与" },
              dst_id: "quest:emblem",
              id: "relation:linyi-quest",
              src_id: "npc:linyi",
              type: "PARTICIPATES_IN",
            },
          ],
        },
        old_value: {
          entities: {
            "quest:emblem": {
              attrs: { name: "收集徽章任务", reward_gold: 120 },
              id: "quest:emblem",
              type: "QUEST",
            },
          },
          relations: {
            "relation:quest-location": {
              attrs: { note: "旧入口" },
              dst_id: "location:qingstone",
              id: "relation:quest-location",
              src_id: "quest:emblem",
              type: "LOCATED_IN",
            },
          },
        },
        op: "replace_subgraph",
        op_id: "op:replace-emblem-subgraph",
        target: "subgraph:emblem-quest",
      },
    ];
    const target = patchTargetSubject();
    target.payload = {
      entities: {
        "location:qingstone": { attrs: { name: "青石村" }, type: "LOCATION" },
        "npc:linyi": { attrs: { name: "林逸" }, type: "NPC" },
        "quest:emblem": { attrs: { name: "收集徽章任务", reward_gold: 80 }, type: "QUEST" },
      },
      meta_schema_version: "meta@1",
      relations: {
        "relation:linyi-quest": {
          attrs: { label: "参与" },
          dst_id: "quest:emblem",
          id: "relation:linyi-quest",
          src_id: "npc:linyi",
          type: "PARTICIPATES_IN",
        },
        "relation:quest-location": {
          attrs: {},
          dst_id: "location:qingstone",
          id: "relation:quest-location",
          src_id: "quest:emblem",
          type: "LOCATED_IN",
        },
      },
    };
    renderPage(
      <ApprovalDetailPage
        api={api({
          getArtifactPayload: vi.fn(async (artifactId) =>
            artifactId === approvalView().approval.target_binding!.target_artifact_id
              ? target
              : evidenceSubject(),
          ),
          getPatch: vi.fn(async () => subject),
        })}
        approvalId="approval:multi-domain:7"
      />,
    );

    const review = await screen.findByRole("region", { name: "受审内容与验证依据" });
    const operations = within(review).getByRole("list", { name: "Patch 变更内容" });
    expect(operations).toHaveTextContent("受影响对象");
    expect(operations).toHaveTextContent("QUEST「收集徽章任务」");
    expect(operations).toHaveTextContent("NPC「林逸」");
    expect(operations).toHaveTextContent("位于：收集徽章任务 → 青石村");
    expect(operations).toHaveTextContent("实体：新增 1 · 删除 0 · 替换 1");
    expect(operations).toHaveTextContent("关系：新增 1 · 删除 0 · 替换 1");
  });

  it("accepts the exact before/after closure produced by the structured replace_subgraph editor", async () => {
    const baseQuest: components["schemas"]["GraphItemV1"] = {
      entity: {
        attrs: { name: "收集徽章任务", reward_gold: 120 },
        id: "quest:emblem",
        schema_version: "ir-core@1",
        tags: ["主线"],
        type: "QUEST",
      },
      item_id: "quest:emblem",
      item_kind: "entity",
      item_schema_version: "graph-item@1",
    };
    const operation = {
      ...createStructuredOperation("row-1"),
      attributes: [
        {
          key: "reward_gold",
          rowId: "attribute-1",
          value: { kind: "number" as const, text: "80" },
        },
      ],
      entityRef: "entity:quest:emblem",
      op: "replace_subgraph" as const,
      subgraphLabel: "徽章任务奖励",
      subgraphResourceKind: "entity" as const,
    };
    const compiled = compileStructuredOperations([operation], [baseQuest]);
    expect(compiled.error).toBeNull();

    const subject = patchSubject();
    subject.patch.ops = compiled.ops;
    const target = patchTargetSubject();
    target.payload = {
      entities: {
        "quest:emblem": {
          attrs: { name: "收集徽章任务", reward_gold: 80 },
          schema_version: "ir-core@1",
          tags: ["主线"],
          type: "QUEST",
        },
      },
      meta_schema_version: "meta@1",
      relations: {},
    };
    renderPage(
      <ApprovalDetailPage
        api={api({
          getArtifactPayload: vi.fn(async (artifactId) =>
            artifactId === approvalView().approval.target_binding!.target_artifact_id
              ? target
              : evidenceSubject(),
          ),
          getPatch: vi.fn(async () => subject),
        })}
        approvalId="approval:multi-domain:7"
      />,
    );

    const review = await screen.findByRole("region", { name: "受审内容与验证依据" });
    expect(within(review).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(review).getByRole("list", { name: "Patch 变更内容" })).toHaveTextContent(
      "实体：新增 0 · 删除 0 · 替换 1",
    );
  });

  it("fails closed when a field operation target cannot be safely split", async () => {
    const subject = patchSubject();
    subject.patch.ops = [
      {
        new_value: 4,
        old_value: 3,
        op: "set_entity_attr",
        op_id: "op:malformed-target",
        target: "missing-field-path",
      },
    ];
    renderPage(
      <ApprovalDetailPage
        api={api({ getPatch: vi.fn(async () => subject) })}
        approvalId="approval:multi-domain:7"
      />,
    );

    const review = await screen.findByRole("region", { name: "受审内容与验证依据" });
    expect(within(review).getByRole("alert")).toHaveTextContent("无法安全读取完整受审内容");
    expect(screen.getByRole("button", { name: "提交批准" })).toBeDisabled();
  });

  it("explains the narrow explicit reconfirmation state when current votes are all satisfied but workflow remains pending", async () => {
    const reconfirmation = approvalView();
    reconfirmation.requirement_progress = reconfirmation.requirement_progress.map((progress, index) => ({
      ...progress,
      decision_eligibility:
        index === 0
          ? [
              { decision: "approve", eligible: true, reason_codes: [] },
              {
                decision: "reject",
                eligible: false,
                reason_codes: ["actor_already_decided_requirement"],
              },
              {
                decision: "request_changes",
                eligible: false,
                reason_codes: ["actor_already_decided_requirement"],
              },
            ]
          : progress.decision_eligibility,
      satisfied: true,
      valid_approval_count: progress.min_approvals,
    }));

    renderPage(
      <ApprovalDetailPage
        api={api({ getApproval: vi.fn(async () => versioned(reconfirmation)) })}
        approvalId="approval:multi-domain:7"
      />,
    );

    expect(await screen.findByRole("status", { name: "需要显式批准确认" })).toHaveTextContent(
      "全部审批职责的当前有效票均已满足，但流程仍处于待审批状态",
    );
    expect(screen.getByRole("status", { name: "需要显式批准确认" })).toHaveTextContent(
      "当前身份可对已有有效票的审批职责再确认一次",
    );
  });

  it("shows the full typed constraint and scope before a constraint approval can be submitted", async () => {
    const view = constraintApprovalView();
    const evidence = evidenceSubject(view);
    const evidenceTarget = (evidence.payload as { target_binding: { expected_ref?: unknown } })
      .target_binding;
    delete evidenceTarget.expected_ref;
    renderPage(
      <ApprovalDetailPage
        api={api({
          getApproval: vi.fn(async () => versioned(view)),
          getArtifactPayload: vi.fn(async () => evidence),
          getConstraintProposal: vi.fn(async () => constraintSubject(view)),
        })}
        approvalId="approval:multi-domain:7"
      />,
    );

    const review = await screen.findByRole("region", { name: "受审内容与验证依据" });
    expect(review).toHaveTextContent("支线任务奖励金币不得超过 80。");
    expect(review).toHaveTextContent("reward_gold ≤ 80");
    expect(review).toHaveTextContent("QUEST · 变量 q");
    expect(within(review).getByRole("region", { name: "审批影响目标" })).toHaveTextContent(
      "constraints/head",
    );
    expect(review).toHaveTextContent("确定性验证已通过");
  });

  it("shows the business content that a rollback restores, deletes, and changes before approval", async () => {
    const view = rollbackApprovalView();
    const approvalsApi = rollbackApi(view);
    renderPage(<ApprovalDetailPage api={approvalsApi} approvalId="approval:multi-domain:7" />);

    const review = await screen.findByRole("region", { name: "受审内容与验证依据" });
    expect(review).toHaveTextContent("恢复经过验证的商队任务基线。");
    const changes = within(review).getByRole("list", { name: "回滚内容差异" });
    expect(changes).toHaveTextContent("回退后恢复");
    expect(changes).toHaveTextContent("NPC「沈月」");
    expect(changes).toHaveTextContent("回退后删除");
    expect(changes).toHaveTextContent("NPC「林逸」");
    expect(changes).toHaveTextContent("回退后改变");
    expect(changes).toHaveTextContent("QUEST「失踪的商队」");
    expect(changes).toHaveTextContent("关系「委托」");
    expect(changes).toHaveTextContent("修改前");
    expect(changes).toHaveTextContent("回退后");
    expect(changes).not.toHaveTextContent("artifact:snapshot:current");
    expect(changes).not.toHaveTextContent("artifact:snapshot:history-4");
    expect(within(review).getByText("查看回滚技术身份")).toBeVisible();
    expect(approvalsApi.getArtifactPayload).toHaveBeenCalledWith("artifact:snapshot:current");
    expect(approvalsApi.getArtifactPayload).toHaveBeenCalledWith("artifact:snapshot:history-4");
  });

  it.each([
    [
      "current Artifact identity",
      (_subject: ApprovalRollbackRequest, current: ApprovalArtifactPayload) => {
        current.artifact.artifact_id = "artifact:snapshot:wrong-current";
      },
    ],
    [
      "target digest",
      (
        _subject: ApprovalRollbackRequest,
        _current: ApprovalArtifactPayload,
        target: ApprovalArtifactPayload,
      ) => {
        target.artifact.payload_hash = "0".repeat(64);
      },
    ],
    [
      "frozen request target",
      (subject: ApprovalRollbackRequest) => {
        subject.request.target_artifact_id = "artifact:snapshot:not-the-frozen-target";
      },
    ],
  ] as const)("fails closed when rollback %s does not close", async (_label, mutate) => {
    const user = userEvent.setup();
    const view = rollbackApprovalView();
    const subject = rollbackSubject(view);
    const current = rollbackSnapshot(view, "current");
    const target = rollbackSnapshot(view, "target");
    mutate(subject, current, target);
    renderPage(
      <ApprovalDetailPage
        api={rollbackApi(view, subject, current, target)}
        approvalId="approval:multi-domain:7"
      />,
    );

    const review = await screen.findByRole("region", { name: "受审内容与验证依据" });
    expect(within(review).getByRole("alert")).toHaveTextContent("无法安全读取完整受审内容");
    await user.click(screen.getByRole("checkbox", { name: "选择 数值策划 · 经济系统" }));
    await user.selectOptions(screen.getByLabelText("决定原因"), "evidence_reviewed");
    expect(screen.getByRole("button", { name: "提交批准" })).toBeDisabled();
  });

  it("submits a partial approve for only selected eligible requirements and keeps the item pending", async () => {
    const user = userEvent.setup();
    const partial = approvalView();
    partial.approval.workflow_revision = 13;
    partial.requirement_progress[0] = {
      ...partial.requirement_progress[0]!,
      satisfied: true,
      valid_approval_count: 2,
      decision_eligibility: [
        {
          decision: "approve",
          eligible: false,
          reason_codes: ["requirement_already_satisfied"],
        },
        { decision: "reject", eligible: true, reason_codes: [] },
        { decision: "request_changes", eligible: true, reason_codes: [] },
      ],
    };
    const decide = vi.fn(async () => versioned(partial, '"approval:opaque-13"'));
    const approvalsApi = api({ decide });
    renderPage(<ApprovalDetailPage api={approvalsApi} approvalId="approval:multi-domain:7" />);

    await user.click(await screen.findByRole("checkbox", { name: "选择 数值策划 · 经济系统" }));
    await user.selectOptions(screen.getByLabelText("决定原因"), "evidence_reviewed");
    await user.type(screen.getByLabelText("补充说明"), "经济 requirement 单独通过。");
    await user.click(screen.getByRole("button", { name: "提交批准" }));
    expect(screen.getByRole("dialog", { name: "确认批准决定" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "确认批准" }));

    await waitFor(() => expect(decide).toHaveBeenCalledOnce());
    expect(decide).toHaveBeenCalledWith(
      expect.objectContaining({ etag: '"approval:opaque-12"' }),
      {
        action: "approve",
        comment: "经济 requirement 单独通过。",
        reasonCode: "evidence_reviewed",
        requirementIds: ["requirement:economy"],
      },
      expect.objectContaining({ idempotencyKey: expect.any(String) }),
    );
    expect(await screen.findByText("部分批准已记录；其他审批职责仍待处理。")).toBeVisible();
    expect(screen.getByText("待审批 · 流程版本 13")).toBeVisible();
    expect(
      screen.getByRole("checkbox", { name: "选择 数值策划 · 经济系统" }).closest("tr"),
    ).toHaveTextContent("2 / 2");
  });

  it("uses the submitted mutation action for the success message even if the visible action changes in flight", async () => {
    const user = userEvent.setup();
    let resolveDecision!: (value: VersionedApproval) => void;
    const decide = vi.fn(
      async () =>
        new Promise<VersionedApproval>((resolve) => {
          resolveDecision = resolve;
        }),
    );
    const partial = approvalView();
    partial.approval.workflow_revision = 13;
    renderPage(<ApprovalDetailPage api={api({ decide })} approvalId="approval:multi-domain:7" />);

    await user.click(await screen.findByRole("checkbox", { name: "选择 数值策划 · 经济系统" }));
    await user.selectOptions(screen.getByLabelText("决定原因"), "evidence_reviewed");
    await user.click(screen.getByRole("button", { name: "提交批准" }));
    await user.click(screen.getByRole("button", { name: "确认批准" }));
    await user.click(screen.getByRole("radio", { name: "请修改" }));
    resolveDecision(versioned(partial, '"approval:opaque-13"'));

    expect(await screen.findByText("部分批准已记录；其他审批职责仍待处理。")).toBeVisible();
    expect(screen.queryByText("请修改决定已记录。")).not.toBeInTheDocument();
  });

  it.each([
    ["reject", "驳回", "确认驳回决定"],
    ["request_changes", "请修改", "确认请修改决定"],
  ] as const)(
    "confirms terminal %s and submits only the selected requirement",
    async (action, label, title) => {
      const user = userEvent.setup();
      const decide = vi.fn<ApprovalsApi["decide"]>(async (current) => current);
      renderPage(<ApprovalDetailPage api={api({ decide })} approvalId="approval:multi-domain:7" />);

      await user.click(await screen.findByRole("radio", { name: label }));
      await user.click(screen.getByRole("checkbox", { name: "选择 内容策划 · 叙事内容" }));
      await user.selectOptions(screen.getByLabelText("决定原因"), "__custom__");
      await user.type(screen.getByLabelText("自定义原因代码（高级）"), `${action}_after_review`);
      await user.click(screen.getByRole("button", { name: `提交${label}` }));

      expect(screen.getByRole("dialog", { name: title })).toBeVisible();
      await user.click(screen.getByRole("button", { name: `确认${label}` }));
      await waitFor(() => expect(decide).toHaveBeenCalledOnce());
      expect(decide.mock.calls[0]?.[1]).toMatchObject({
        action,
        reasonCode: `${action}_after_review`,
        requirementIds: ["requirement:narrative"],
      });
    },
  );

  it("preserves form input on 409 and refreshes only after an explicit user action without replay", async () => {
    const user = userEvent.setup();
    const getApproval = vi.fn(async () => versioned());
    const decide = vi.fn(async () => {
      throw problem(409, "revision_conflict");
    });
    renderPage(
      <ApprovalDetailPage api={api({ decide, getApproval })} approvalId="approval:multi-domain:7" />,
    );

    await user.click(await screen.findByRole("checkbox", { name: "选择 数值策划 · 经济系统" }));
    await user.selectOptions(screen.getByLabelText("决定原因"), "__custom__");
    await user.type(screen.getByLabelText("自定义原因代码（高级）"), "stale_review");
    await user.type(screen.getByLabelText("补充说明"), "这些输入不能丢。 ");
    await user.click(screen.getByRole("button", { name: "提交批准" }));
    await user.click(screen.getByRole("button", { name: "确认批准" }));

    expect(await screen.findByText("revision_conflict")).toBeVisible();
    expect(getApproval).toHaveBeenCalledOnce();
    expect(decide).toHaveBeenCalledOnce();
    expect(screen.getByRole("checkbox", { name: "选择 数值策划 · 经济系统" })).toBeChecked();
    expect(screen.getByLabelText("自定义原因代码（高级）")).toHaveValue("stale_review");
    expect(screen.getByLabelText("补充说明")).toHaveValue("这些输入不能丢。 ");

    await user.click(screen.getByRole("button", { name: "刷新审批状态" }));
    await waitFor(() => expect(getApproval).toHaveBeenCalledTimes(2));
    expect(decide).toHaveBeenCalledOnce();
    expect(screen.getByRole("checkbox", { name: "选择 数值策划 · 经济系统" })).toBeChecked();
    expect(screen.getByLabelText("自定义原因代码（高级）")).toHaveValue("stale_review");
  });

  it("refreshes action eligibility after a 403 role loss, keeps input, and shows server reasons", async () => {
    const user = userEvent.setup();
    const refreshed = approvalView();
    refreshed.requirement_progress[0] = {
      ...refreshed.requirement_progress[0]!,
      decision_eligibility: [
        { decision: "approve", eligible: false, reason_codes: ["route_role_missing"] },
        { decision: "reject", eligible: false, reason_codes: ["route_role_missing"] },
        {
          decision: "request_changes",
          eligible: false,
          reason_codes: ["route_role_missing"],
        },
      ],
      eligible_for_current_actor: false,
    };
    refreshed.current_actor_allowed_requirement_ids = ["requirement:narrative"];
    const getApproval = vi
      .fn<() => Promise<VersionedApproval>>()
      .mockResolvedValueOnce(versioned())
      .mockResolvedValueOnce(versioned(refreshed, '"approval:opaque-role-loss"'));
    const decide = vi.fn(async () => {
      throw problem(403, "forbidden");
    });
    renderPage(
      <ApprovalDetailPage api={api({ decide, getApproval })} approvalId="approval:multi-domain:7" />,
    );

    await user.click(await screen.findByRole("checkbox", { name: "选择 数值策划 · 经济系统" }));
    await user.selectOptions(screen.getByLabelText("决定原因"), "__custom__");
    await user.type(screen.getByLabelText("自定义原因代码（高级）"), "role_was_present");
    await user.click(screen.getByRole("button", { name: "提交批准" }));
    await user.click(screen.getByRole("button", { name: "确认批准" }));

    expect(await screen.findByText("forbidden")).toBeVisible();
    await waitFor(() => expect(getApproval).toHaveBeenCalledTimes(2));
    expect(decide).toHaveBeenCalledOnce();
    expect(screen.getByRole("checkbox", { name: "选择 数值策划 · 经济系统" })).toBeChecked();
    expect(screen.getByLabelText("自定义原因代码（高级）")).toHaveValue("role_was_present");
    expect(
      within(screen.getByRole("checkbox", { name: "选择 数值策划 · 经济系统" }).closest("tr")!).getAllByText(
        "当前身份缺少冻结路由角色",
      ),
    ).toHaveLength(3);
    expect(screen.getByRole("button", { name: "提交批准" })).toBeDisabled();
  });

  it("renders maker-checker/self-decision restrictions from the server projection", async () => {
    const blocked = approvalView();
    blocked.current_actor_allowed_requirement_ids = [];
    blocked.requirement_progress = blocked.requirement_progress.map((progress) => ({
      ...progress,
      decision_eligibility: (["approve", "reject", "request_changes"] as ApprovalAction[]).map(
        (decision) => ({ decision, eligible: false, reason_codes: ["maker_checker_conflict"] }),
      ),
      eligible_for_current_actor: false,
    }));
    renderPage(
      <ApprovalDetailPage
        api={api({ getApproval: vi.fn(async () => versioned(blocked)) })}
        approvalId="approval:multi-domain:7"
      />,
    );

    expect(await screen.findAllByText("maker-checker：提议者不能决定自己的提议")).toHaveLength(6);
    expect(screen.getByRole("button", { name: "提交批准" })).toBeDisabled();
  });

  it("fails closed and disables every decision when the exact subject content does not match", async () => {
    const user = userEvent.setup();
    const mismatched = patchSubject();
    mismatched.artifact.payload_hash = "0".repeat(64);
    renderPage(
      <ApprovalDetailPage
        api={api({ getPatch: vi.fn(async () => mismatched) })}
        approvalId="approval:multi-domain:7"
      />,
    );

    const review = await screen.findByRole("region", { name: "受审内容与验证依据" });
    expect(within(review).getByRole("alert")).toHaveTextContent("无法安全读取完整受审内容");
    await user.click(screen.getByRole("checkbox", { name: "选择 数值策划 · 经济系统" }));
    await user.selectOptions(screen.getByLabelText("决定原因"), "__custom__");
    await user.type(screen.getByLabelText("自定义原因代码（高级）"), "cannot_review");
    expect(screen.getByRole("button", { name: "提交批准" })).toBeDisabled();
  });
});
