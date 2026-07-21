import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/query-client";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError, type SafeProblem } from "../../api/problem";
import { ToastProvider } from "../../app/providers";
import { ApprovalDetailPage, ApprovalsPage } from "./ApprovalsPage";
import type {
  ApprovalAction,
  ApprovalPageData,
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

    expect(await screen.findByRole("heading", { level: 1, name: "Approvals" })).toBeVisible();
    expect(approvalsApi.listMine).toHaveBeenCalledWith(null);
    const table = await screen.findByRole("table", { name: "待我审批" });
    const row = within(table).getByRole("row", { name: /approval:multi-domain:7/ });
    expect(row).toHaveTextContent("human:alice");
    expect(row).toHaveTextContent("domain:economy");
    expect(row).toHaveTextContent("domain:narrative");
    expect(row).toHaveTextContent("pending_approval · revision 12");
    expect(row).toHaveTextContent("0 / 2 requirements satisfied");
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
    expect(await screen.findByText("当前没有可处理的审批 requirement。")).toBeVisible();
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
    for (const value of [
      "pending_approval · revision 12",
      "0 / 2 requirements satisfied",
      "批准 1 · 驳回 2 · 请修改 2",
    ]) {
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

    const policies = screen.getByRole("region", { name: "冻结策略" });
    expect(policies).toHaveTextContent("approval-policy@1");
    expect(policies).toHaveTextContent("roles@9");
    expect(policies).toHaveTextContent("routes@4");
    expect(policies).toHaveTextContent("domains@7");
    expect(policies).toHaveTextContent("a".repeat(64));

    const requirements = screen.getByRole("table", { name: "Requirement progress" });
    const economy = within(requirements)
      .getByRole("checkbox", { name: "选择 requirement:economy" })
      .closest("tr");
    expect(economy).not.toBeNull();
    expect(economy).toHaveTextContent("数值策划");
    expect(economy).toHaveTextContent("1 / 2");
    expect(economy).toHaveTextContent("requirement:narrative");
    expect(economy).toHaveTextContent("批准可用");
    const narrative = within(requirements)
      .getByRole("checkbox", { name: "选择 requirement:narrative" })
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
      "全部 requirement 的当前有效票均已满足，但 workflow 仍为 pending_approval",
    );
    expect(screen.getByRole("status", { name: "需要显式批准确认" })).toHaveTextContent(
      "当前身份可对已有有效票的 requirement 再确认一次",
    );
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

    await user.click(await screen.findByRole("checkbox", { name: "选择 requirement:economy" }));
    await user.type(screen.getByLabelText("决定原因代码"), "evidence_reviewed");
    await user.type(screen.getByLabelText("补充说明"), "经济 requirement 单独通过。");
    await user.click(screen.getByRole("button", { name: "提交批准" }));

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
    expect(await screen.findByText("部分批准已记录；其他 requirement 仍待处理。")).toBeVisible();
    expect(screen.getByText("pending_approval · workflow revision 13")).toBeVisible();
    expect(
      screen.getByRole("checkbox", { name: "选择 requirement:economy" }).closest("tr"),
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

    await user.click(await screen.findByRole("checkbox", { name: "选择 requirement:economy" }));
    await user.type(screen.getByLabelText("决定原因代码"), "evidence_reviewed");
    await user.click(screen.getByRole("button", { name: "提交批准" }));
    await user.click(screen.getByRole("radio", { name: "请修改" }));
    resolveDecision(versioned(partial, '"approval:opaque-13"'));

    expect(await screen.findByText("部分批准已记录；其他 requirement 仍待处理。")).toBeVisible();
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
      await user.click(screen.getByRole("checkbox", { name: "选择 requirement:narrative" }));
      await user.type(screen.getByLabelText("决定原因代码"), `${action}_after_review`);
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

    await user.click(await screen.findByRole("checkbox", { name: "选择 requirement:economy" }));
    await user.type(screen.getByLabelText("决定原因代码"), "stale_review");
    await user.type(screen.getByLabelText("补充说明"), "这些输入不能丢。 ");
    await user.click(screen.getByRole("button", { name: "提交批准" }));

    expect(await screen.findByText("revision_conflict")).toBeVisible();
    expect(getApproval).toHaveBeenCalledOnce();
    expect(decide).toHaveBeenCalledOnce();
    expect(screen.getByRole("checkbox", { name: "选择 requirement:economy" })).toBeChecked();
    expect(screen.getByLabelText("决定原因代码")).toHaveValue("stale_review");
    expect(screen.getByLabelText("补充说明")).toHaveValue("这些输入不能丢。 ");

    await user.click(screen.getByRole("button", { name: "刷新审批状态" }));
    await waitFor(() => expect(getApproval).toHaveBeenCalledTimes(2));
    expect(decide).toHaveBeenCalledOnce();
    expect(screen.getByRole("checkbox", { name: "选择 requirement:economy" })).toBeChecked();
    expect(screen.getByLabelText("决定原因代码")).toHaveValue("stale_review");
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

    await user.click(await screen.findByRole("checkbox", { name: "选择 requirement:economy" }));
    await user.type(screen.getByLabelText("决定原因代码"), "role_was_present");
    await user.click(screen.getByRole("button", { name: "提交批准" }));

    expect(await screen.findByText("forbidden")).toBeVisible();
    await waitFor(() => expect(getApproval).toHaveBeenCalledTimes(2));
    expect(decide).toHaveBeenCalledOnce();
    expect(screen.getByRole("checkbox", { name: "选择 requirement:economy" })).toBeChecked();
    expect(screen.getByLabelText("决定原因代码")).toHaveValue("role_was_present");
    expect(
      within(screen.getByRole("checkbox", { name: "选择 requirement:economy" }).closest("tr")!).getAllByText(
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
});
