import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { createQueryClient } from "../../api/query-client";
import type {
  ConflictPage,
  ExecutionOptionView,
  PatchWorkflowApi,
  RefHistoryPageResponse,
  SnapshotDiffPage,
} from "./api";
import { PatchDetailPage } from "./PatchDetailPage";

type ApprovalStatus = components["schemas"]["ApprovalItem"]["status"];
type ApprovalView = components["schemas"]["ApprovalViewV1"];
type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];
type PatchView = components["schemas"]["PatchArtifactReadViewV1"];
type RunAccepted = components["schemas"]["RunAcceptedV1"];

const PATCH_ID = "artifact:patch:detail";
const BASE_ID = "artifact:spec:base";
const CURRENT_ID = "artifact:spec:current";
const PREVIEW_ID = "artifact:spec:preview";
const BASE_SNAPSHOT = "snapshot:base";
const CURRENT_SNAPSHOT = "snapshot:current";
const PREVIEW_SNAPSHOT = "snapshot:preview";
const APPROVAL_ID = "approval:patch:detail";
const SUBJECT_DIGEST = "a".repeat(64);
const TARGET_DIGEST = "b".repeat(64);
const REF_NAME = "spec/main";

function summary(
  artifactId: string,
  kind: components["schemas"]["ArtifactSummaryV1"]["kind"],
  payloadSchemaId: string,
  snapshotId: string,
) {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-20T06:00:00Z",
    domain_scope: { domain_ids: ["domain:economy"] },
    kind,
    lineage_schema_version: "lineage@2" as const,
    parent_artifact_ids: [],
    payload_hash: kind === "patch" ? SUBJECT_DIGEST : "c".repeat(64),
    payload_schema_id: payloadSchemaId,
    summary_schema_version: "artifact-summary@1" as const,
    version_tuple: { ir_snapshot_id: snapshotId },
  };
}

function patchView(status: ApprovalStatus): PatchView {
  const passed = status === "validated" || status === "approved" || status === "applied";
  return {
    approval_status: status,
    artifact: {
      ...summary(PATCH_ID, "patch", "patch@2", BASE_SNAPSHOT),
      parent_artifact_ids: [BASE_ID, PREVIEW_ID],
    },
    patch: {
      base_snapshot_id: BASE_SNAPSHOT,
      expected_to_fix: [],
      ops: [],
      patch_schema_version: "patch@2",
      preconditions: [],
      produced_by: "human",
      producer_run_id: null,
      rationale: "Bring the reward back under the deterministic sink rate.",
      revision: 1,
      side_effect_risk: "low",
      supersedes_artifact_id: null,
      target_snapshot_id: PREVIEW_SNAPSHOT,
    },
    regression_status: passed ? "passed" : "not_started",
    validation_status: status === "validation_failed" ? "failed" : passed ? "passed" : "not_started",
    view_schema_version: "patch-artifact-read-view@1",
    workflow_revision: status === "applied" ? 4 : 3,
  };
}

function approvalView(status: ApprovalStatus): ApprovalView {
  const passed = status === "validated" || status === "approved" || status === "applied";
  return {
    approval: {
      active_validation_run_id: null,
      applied_at: status === "applied" ? "2026-07-20T06:20:00Z" : null,
      approval_id: APPROVAL_ID,
      approval_policy: { policy_digest: "d".repeat(64), policy_version: "1" },
      approval_schema_version: "approval@1",
      auto_apply_proof: null,
      created_at: "2026-07-20T06:00:00Z",
      decided_at: status === "approved" || status === "applied" ? "2026-07-20T06:10:00Z" : null,
      decisions: [],
      domain_registry_ref: { registry_digest: "e".repeat(64), registry_version: "1" },
      domain_scope: { domain_ids: ["domain:economy"] },
      evidence_set_artifact_id: status === "validation_failed" || passed ? "artifact:evidence:patch" : null,
      last_validation_failure_artifact_id: status === "validation_failed" ? "artifact:failure:patch" : null,
      proposer: { principal_id: "principal:maker", principal_kind: "human" },
      regression_evidence_artifact_ids: passed ? ["artifact:regression:patch"] : [],
      requirements: [],
      role_policy_digest: "f".repeat(64),
      role_policy_version: "1",
      route_policy: {
        domain_registry_ref: { registry_digest: "e".repeat(64), registry_version: "1" },
        route_digest: "1".repeat(64),
        route_version: "1",
      },
      status,
      subject_artifact_id: PATCH_ID,
      subject_digest: SUBJECT_DIGEST,
      subject_kind: "patch",
      subject_revision: 1,
      subject_series_id: "patch-series:detail",
      submitted_at: null,
      supersedes_approval_id: null,
      target_binding: {
        binding_schema_version: "approval-target-binding@1",
        expected_ref: { artifact_id: BASE_ID, revision: 1 },
        ref_name: REF_NAME,
        subject_kind: "patch",
        target_artifact_id: PREVIEW_ID,
        target_artifact_kind: "ir_snapshot",
        target_digest: TARGET_DIGEST,
        target_snapshot_id: PREVIEW_SNAPSHOT,
      },
      workflow_revision: status === "applied" ? 4 : 3,
    },
    current_actor_allowed_requirement_ids: [],
    requirement_progress: [],
    view_schema_version: "approval-view@1",
  };
}

function binding(status: ApprovalStatus) {
  return {
    approval_id: APPROVAL_ID,
    approval_status: status,
    is_current_head: true,
    subject_artifact_id: PATCH_ID,
    subject_digest: SUBJECT_DIGEST,
    subject_head_revision: 1,
    subject_kind: "patch" as const,
    subject_revision: 1,
    subject_series_id: "patch-series:detail",
    view_schema_version: "subject-approval-binding-view@1" as const,
    workflow_revision: status === "applied" ? 4 : 3,
  };
}

function profile(kind: ExecutionProfile["profile_kind"], runKind: string): ExecutionProfile {
  return {
    compatible_run_kinds: [{ kind: runKind, version: 1 }],
    display_name: `builtin.${kind}`,
    domain_scope: { domain_ids: ["domain:economy"] },
    env_contract_version: null,
    input_schema_ids: [],
    output_schema_ids: [],
    profile: { profile_id: `builtin.${kind}`, version: 1 },
    profile_kind: kind,
    profile_payload_hash: "2".repeat(64),
    required_capabilities: [],
    status: "active",
    stochastic: kind === "patch_repair",
    target_environment_profile: null,
  };
}

function page<T>(items: T[], snapshot: string, nextCursor: string | null = null) {
  return {
    expires_at: "2026-07-20T07:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "page@1" as const,
    read_snapshot_id: snapshot,
  };
}

function expiredCursor(staleCursor: string): CursorExpiredError {
  return new CursorExpiredError(
    {
      code: "cursor_expired",
      conflict_set_id: null,
      detail: "The signed page cursor expired.",
      earliest_cursor: null,
      instance: "/api/v1/snapshot-diff",
      request_id: "request:cursor-expired",
      retry_after_s: null,
      run_id: null,
      status: 410,
      title: "Cursor expired",
      trace_id: null,
      type: "https://gameforge.dev/problems/cursor-expired",
    },
    staleCursor,
  );
}

function workflowProblem(code: string, status: number, detail: string): ApiProblemError {
  return new ApiProblemError({
    code,
    conflict_set_id: null,
    detail,
    earliest_cursor: null,
    instance: "/api/v1/patches/artifact:patch:detail:submit-for-approval",
    request_id: `request:${code}`,
    retry_after_s: null,
    run_id: null,
    status,
    title: "Workflow command rejected",
    trace_id: null,
    type: `https://gameforge.dev/problems/${code}`,
  });
}

function diffPage(
  items: SnapshotDiffPage["page"]["items"],
  snapshot: string,
  nextCursor: string | null = null,
): SnapshotDiffPage {
  return {
    diff: {
      base_snapshot_id: BASE_SNAPSHOT,
      diff_schema_version: "snapshot-diff@1",
      entry_count: items.length,
      target_snapshot_id: PREVIEW_SNAPSHOT,
    },
    page: page(items, snapshot, nextCursor),
    page_schema_version: "snapshot-diff-http-page@1",
  };
}

function artifact(artifactId: string, kind: "validation_evidence" | "run_failure") {
  return {
    artifact: {
      ...summary(
        artifactId,
        kind,
        kind === "validation_evidence" ? "evidence-set@1" : "run-failure@1",
        PREVIEW_SNAPSHOT,
      ),
      payload_hash: "3".repeat(64),
    },
    payload: {},
    resource_revision: 1 as const,
    view_schema_version: "artifact-payload-view@1" as const,
  };
}

function api(status: ApprovalStatus = "draft", overrides: Partial<PatchWorkflowApi> = {}): PatchWorkflowApi {
  const view = patchView(status);
  const approval = approvalView(status);
  const profiles = [
    profile("validation", "patch.validate"),
    profile("patch_repair", "patch.repair"),
    profile("checker", "patch.validate"),
    profile("simulation", "patch.validate"),
    profile("config_export", "config.export"),
  ];
  return {
    getApproval: vi.fn(async () => ({ etag: '"approval:3"', value: approval })),
    getApprovalBinding: vi.fn(async () => binding(status)),
    getArtifact: vi.fn(async (artifactId) =>
      artifact(artifactId, artifactId === "artifact:failure:patch" ? "run_failure" : "validation_evidence"),
    ),
    getPatch: vi.fn(async () => ({ etag: '"patch:3"', value: view })),
    getSnapshotDiff: vi.fn(async () => ({
      diff: {
        base_snapshot_id: BASE_SNAPSHOT,
        diff_schema_version: "snapshot-diff@1",
        entry_count: 1,
        target_snapshot_id: PREVIEW_SNAPSHOT,
      },
      page: page(
        [
          {
            after: { presence: "present", value: 80 },
            before: { presence: "present", value: 120 },
            path: "/economy/reward",
          },
        ],
        "read:diff",
      ),
      page_schema_version: "snapshot-diff-http-page@1",
    })),
    getSpec: vi.fn(async (artifactId) => ({
      artifact: summary(
        artifactId,
        "ir_snapshot",
        "ir-core@1",
        artifactId === CURRENT_ID ? CURRENT_SNAPSHOT : BASE_SNAPSHOT,
      ),
      ref_name: REF_NAME,
      ref_value: { artifact_id: artifactId, revision: artifactId === CURRENT_ID ? 2 : 1 },
      schema_registry_version: "ir-core@1",
      snapshot_id: artifactId === CURRENT_ID ? CURRENT_SNAPSHOT : BASE_SNAPSHOT,
      view_schema_version: "spec-view@1",
    })),
    listConflicts: vi.fn(async () => page([], "read:conflicts")),
    listExecutionProfiles: vi.fn(async (filters) =>
      page(
        profiles.filter((candidate) => candidate.profile_kind === filters.profile_kind),
        `read:profiles:${filters.profile_kind}`,
      ),
    ),
    listRefHistory: vi.fn(async () =>
      page(
        [
          {
            entry_schema_version: "ref-history-entry@1",
            ref_name: REF_NAME,
            value: { artifact_id: BASE_ID, revision: 1 },
          },
        ],
        "read:history",
      ),
    ),
    ...overrides,
  } as unknown as PatchWorkflowApi;
}

function renderPage(patchApi: PatchWorkflowApi, path = `/patches/${encodeURIComponent(PATCH_ID)}`) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <PatchDetailPage api={patchApi} artifactId={PATCH_ID} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Patch detail", () => {
  it("renders base/current/proposed and submits only explicit server-defined conflict choices", async () => {
    const user = userEvent.setup();
    const rebasePatch = vi.fn(async () => ({
      conflict_set_id: "conflict-set:patch",
      new_patch_artifact_id: null,
      status: "conflicted" as const,
    }));
    const resolvePatchConflicts = vi.fn<PatchWorkflowApi["resolvePatchConflicts"]>(async () => ({
      conflict_set_id: "conflict-set:patch",
      new_patch_artifact_id: null,
      status: "conflicted" as const,
    }));
    const conflicts: ConflictPage = page(
      [
        {
          allowed_resolutions: ["keep_current", "take_proposed", "custom"],
          base: { presence: "present", value: 120 },
          current: { presence: "present", value: 100 },
          id: "conflict:reward",
          kind: "replace_replace",
          path: "/economy/reward",
          proposed: { presence: "present", value: 80 },
        },
      ],
      "read:conflicts",
    );
    const history: RefHistoryPageResponse = page(
      [
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: BASE_ID, revision: 1 },
        },
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: CURRENT_ID, revision: 2 },
        },
      ],
      "read:history",
    );
    const patchApi = api("draft", {
      listConflicts: vi.fn(async () => conflicts),
      listRefHistory: vi.fn(async () => history),
      rebasePatch,
      resolvePatchConflicts,
    });
    renderPage(patchApi);

    expect(await screen.findByText(CURRENT_SNAPSHOT)).toBeVisible();
    expect(screen.getAllByText(PREVIEW_SNAPSHOT)).not.toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Rebase 到 exact current ref" }));
    const resolver = await screen.findByRole("heading", { name: "三方冲突解析" });
    const region = resolver.closest("section")!;
    await user.click(within(region).getByRole("radio", { name: "采用 Proposed" }));
    await user.click(screen.getByRole("button", { name: "提交全部显式 resolutions" }));

    await waitFor(() => expect(resolvePatchConflicts).toHaveBeenCalledTimes(1));
    expect(resolvePatchConflicts.mock.calls[0][1]).toMatchObject({
      approval_id: APPROVAL_ID,
      conflict_set_id: "conflict-set:patch",
      expected_ref: { artifact_id: CURRENT_ID, revision: 2 },
      expected_subject_head_revision: 1,
      expected_workflow_revision: 3,
      ref_name: REF_NAME,
      resolutions: [{ choice: "take_proposed", conflict_id: "conflict:reward" }],
    });
  });

  it("restarts an expired Diff pagination from cursor=null and replaces stale entries", async () => {
    const user = userEvent.setup();
    const getSnapshotDiff = vi
      .fn<PatchWorkflowApi["getSnapshotDiff"]>()
      .mockResolvedValueOnce(
        diffPage(
          [
            {
              after: { presence: "present", value: 80 },
              before: { presence: "present", value: 120 },
              path: "/economy/stale-page",
            },
          ],
          "read:diff:stale",
          "cursor:diff:2",
        ),
      )
      .mockRejectedValueOnce(expiredCursor("cursor:diff:2"))
      .mockResolvedValueOnce(
        diffPage(
          [
            {
              after: { presence: "present", value: 70 },
              before: { presence: "present", value: 100 },
              path: "/economy/fresh-page",
            },
          ],
          "read:diff:fresh",
        ),
      );
    renderPage(api("draft", { getSnapshotDiff }));

    await user.click(await screen.findByRole("button", { name: "加载更多 Diff entries" }));
    await user.click(await screen.findByRole("button", { name: "从第一页重新读取 Diff" }));

    await waitFor(() => expect(getSnapshotDiff).toHaveBeenCalledTimes(3));
    expect(getSnapshotDiff.mock.calls.map((call) => call[2])).toEqual([null, "cursor:diff:2", null]);
    expect(await screen.findByText("/economy/fresh-page")).toBeVisible();
    expect(screen.queryByText("/economy/stale-page")).not.toBeInTheDocument();
  });

  it("restarts an expired ConflictSet pagination from cursor=null", async () => {
    const user = userEvent.setup();
    const conflict: ConflictPage["items"][number] = {
      allowed_resolutions: ["keep_current", "take_proposed"],
      base: { presence: "present", value: 120 },
      current: { presence: "present", value: 100 },
      id: "conflict:cursor",
      kind: "replace_replace",
      path: "/economy/cursor",
      proposed: { presence: "present", value: 80 },
    };
    const listConflicts = vi
      .fn<PatchWorkflowApi["listConflicts"]>()
      .mockResolvedValueOnce(page([conflict], "read:conflict:stale", "cursor:conflict:2"))
      .mockRejectedValueOnce(expiredCursor("cursor:conflict:2"))
      .mockResolvedValueOnce(page([conflict], "read:conflict:fresh"));
    renderPage(
      api("draft", { listConflicts }),
      `/patches/${encodeURIComponent(PATCH_ID)}?conflictSet=conflict-set%3Acursor`,
    );

    await user.click(await screen.findByRole("button", { name: "从第一页重新读取冲突" }));

    expect(await screen.findByRole("heading", { name: "三方冲突解析" })).toBeVisible();
    expect(listConflicts.mock.calls.map((call) => call[1])).toEqual([null, "cursor:conflict:2", null]);
  });

  it("requires a fresh explicit choice when conflict authority switches to another set", async () => {
    const user = userEvent.setup();
    const conflict: ConflictPage["items"][number] = {
      allowed_resolutions: ["keep_current", "take_proposed"],
      base: { presence: "present", value: 120 },
      current: { presence: "present", value: 100 },
      id: "conflict:shared-id",
      kind: "replace_replace",
      path: "/economy/shared",
      proposed: { presence: "present", value: 80 },
    };
    const history: RefHistoryPageResponse = page(
      [
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: BASE_ID, revision: 1 },
        },
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: CURRENT_ID, revision: 2 },
        },
      ],
      "read:history",
    );
    const listConflicts = vi.fn<PatchWorkflowApi["listConflicts"]>(async () =>
      page([conflict], "read:conflicts"),
    );
    const resolvePatchConflicts = vi.fn<PatchWorkflowApi["resolvePatchConflicts"]>(async () => ({
      conflict_set_id: "conflict-set:B",
      new_patch_artifact_id: null,
      status: "conflicted",
    }));
    renderPage(
      api("draft", {
        listConflicts,
        listRefHistory: vi.fn(async () => history),
        rebasePatch: vi.fn<PatchWorkflowApi["rebasePatch"]>(async () => ({
          conflict_set_id: "conflict-set:A",
          new_patch_artifact_id: null,
          status: "conflicted",
        })),
        resolvePatchConflicts,
      }),
    );

    await user.click(await screen.findByRole("button", { name: "Rebase 到 exact current ref" }));
    const firstResolver = await screen.findByRole("heading", { name: "三方冲突解析" });
    await user.click(within(firstResolver.closest("section")!).getByRole("radio", { name: "采用 Proposed" }));
    await user.click(screen.getByRole("button", { name: "提交全部显式 resolutions" }));

    await waitFor(() => expect(listConflicts).toHaveBeenCalledWith("conflict-set:B", null));
    const secondResolver = screen.getByRole("heading", { name: "三方冲突解析" });
    expect(
      within(secondResolver.closest("section")!).getByRole("radio", { name: "采用 Proposed" }),
    ).not.toBeChecked();
    expect(screen.getByRole("button", { name: "提交全部显式 resolutions" })).toBeDisabled();
  });

  it("verifies a clean rebase as a fresh revision with no inherited authority", async () => {
    const user = userEvent.setup();
    const replacementId = "artifact:patch:rebased";
    const replacementApprovalId = "approval:patch:rebased";
    const previousSuperseded: PatchView = {
      ...patchView("superseded"),
      approval_status: "superseded",
    };
    const previousBinding = {
      ...binding("superseded"),
      approval_status: "superseded" as const,
      is_current_head: false,
      subject_head_revision: 2,
    };
    const previousApproval: ApprovalView = {
      ...approvalView("superseded"),
      approval: { ...approvalView("superseded").approval, status: "superseded" },
    };
    const replacement: PatchView = {
      ...patchView("draft"),
      artifact: {
        ...summary(replacementId, "patch", "patch@2", CURRENT_SNAPSHOT),
        parent_artifact_ids: [CURRENT_ID, PATCH_ID, PREVIEW_ID].sort(),
      },
      patch: {
        ...patchView("draft").patch,
        base_snapshot_id: CURRENT_SNAPSHOT,
        revision: 2,
        supersedes_artifact_id: PATCH_ID,
      },
      workflow_revision: 1,
    };
    const replacementApproval: ApprovalView = {
      ...approvalView("draft"),
      approval: {
        ...approvalView("draft").approval,
        approval_id: replacementApprovalId,
        subject_artifact_id: replacementId,
        subject_revision: 2,
        supersedes_approval_id: APPROVAL_ID,
        target_binding: {
          ...approvalView("draft").approval.target_binding!,
          expected_ref: { artifact_id: CURRENT_ID, revision: 2 },
        },
        workflow_revision: 1,
      },
    };
    const replacementBinding = {
      ...binding("draft"),
      approval_id: replacementApprovalId,
      subject_artifact_id: replacementId,
      subject_head_revision: 2,
      subject_revision: 2,
      workflow_revision: 1,
    };
    const history: RefHistoryPageResponse = page(
      [
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: BASE_ID, revision: 1 },
        },
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: CURRENT_ID, revision: 2 },
        },
      ],
      "read:history",
    );
    let rebased = false;
    const patchApi = api("draft", {
      getApproval: vi.fn<PatchWorkflowApi["getApproval"]>(async (approvalId) => ({
        etag: '"approval:replacement"',
        value:
          approvalId === replacementApprovalId
            ? replacementApproval
            : rebased
              ? previousApproval
              : approvalView("draft"),
      })),
      getApprovalBinding: vi.fn<PatchWorkflowApi["getApprovalBinding"]>(async (subjectId) =>
        subjectId === replacementId ? replacementBinding : rebased ? previousBinding : binding("draft"),
      ),
      getPatch: vi.fn<PatchWorkflowApi["getPatch"]>(async (subjectId) => ({
        etag: '"patch:replacement"',
        value: subjectId === replacementId ? replacement : rebased ? previousSuperseded : patchView("draft"),
      })),
      listRefHistory: vi.fn(async () => history),
      rebasePatch: vi.fn<PatchWorkflowApi["rebasePatch"]>(async () => {
        rebased = true;
        return {
          conflict_set_id: null,
          new_patch_artifact_id: replacementId,
          status: "clean",
        };
      }),
    });
    renderPage(patchApi);

    await user.click(await screen.findByRole("button", { name: "Rebase 到 exact current ref" }));

    const receiptHeading = await screen.findByRole("heading", { name: "已创建独立 Patch revision" });
    const receipt = receiptHeading.closest('[role="status"]') as HTMLElement;
    expect(receipt).not.toBeNull();
    expect(receipt).toHaveTextContent("旧验证、证据与审批决定不继承");
    expect(within(receipt).getByRole("link", { name: "打开新 Patch revision" })).toHaveAttribute(
      "href",
      "/patches/artifact%3Apatch%3Arebased",
    );
    expect(screen.getByRole("button", { name: "启动 exact validation" })).toBeDisabled();
  });

  it("never resolves a deep-linked ConflictSet from a terminal Patch revision", async () => {
    const conflict: ConflictPage["items"][number] = {
      allowed_resolutions: ["take_proposed"],
      base: { presence: "present", value: 120 },
      current: { presence: "present", value: 100 },
      id: "conflict:terminal",
      kind: "replace_replace",
      path: "/economy/terminal",
      proposed: { presence: "present", value: 80 },
    };
    renderPage(
      api("applied", {
        listConflicts: vi.fn(async () => page([conflict], "read:terminal")),
        listRefHistory: vi.fn(async () =>
          page(
            [
              {
                entry_schema_version: "ref-history-entry@1" as const,
                ref_name: REF_NAME,
                value: { artifact_id: BASE_ID, revision: 1 },
              },
              {
                entry_schema_version: "ref-history-entry@1" as const,
                ref_name: REF_NAME,
                value: { artifact_id: CURRENT_ID, revision: 2 },
              },
            ],
            "read:terminal-history",
          ),
        ),
      }),
      `/patches/${encodeURIComponent(PATCH_ID)}?conflictSet=conflict-set%3Aterminal`,
    );

    const resolver = await screen.findByRole("heading", { name: "三方冲突解析" });
    const radio = within(resolver.closest("section")!).getByRole("radio", { name: "采用 Proposed" });
    await userEvent.click(radio);
    expect(screen.getByRole("button", { name: "提交全部显式 resolutions" })).toBeDisabled();
  });

  it("starts validation with exact frozen workflow and upstream Artifact inputs", async () => {
    const user = userEvent.setup();
    const accepted: RunAccepted = {
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:validation/events",
      run_id: "run:validation",
      status_url: "/api/v1/runs/run:validation",
    };
    const validatePatch = vi.fn<PatchWorkflowApi["validatePatch"]>(async () => accepted);
    renderPage(api("draft", { validatePatch }));

    await screen.findByRole("heading", { name: "Exact validation inputs" });
    expect(screen.getByRole("heading", { name: "Workflow evidence Artifact ledger" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "确定性预言机" })).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Validation policy"), "builtin.validation@1");
    await user.type(screen.getByLabelText(/Candidate ConfigExport Artifact IDs/), "artifact:config:1");
    await user.type(screen.getByLabelText(/Review Artifact IDs/), "artifact:review:1");
    await user.type(screen.getByLabelText(/PlaytestTrace Artifact IDs/), "artifact:trace:1");
    const historicalFinding = {
      evidence_artifact_id: "artifact:evidence:old",
      finding_digest: "f".repeat(64),
      finding_id: "finding:economy:old",
      finding_revision: 2,
    };
    fireEvent.change(screen.getByLabelText(/Expected historical FindingEvidenceBindingV1/), {
      target: { value: JSON.stringify([historicalFinding]) },
    });
    await user.click(screen.getByRole("button", { name: "启动 exact validation" }));

    await waitFor(() => expect(validatePatch).toHaveBeenCalledTimes(1));
    expect(validatePatch.mock.calls[0][1]).toMatchObject({
      approval_id: APPROVAL_ID,
      base_snapshot_artifact_id: BASE_ID,
      candidate_config_export_artifact_ids: ["artifact:config:1"],
      expected_subject_head_revision: 1,
      expected_workflow_revision: 3,
      expected_findings: [historicalFinding],
      findings: [],
      playtest_trace_artifact_ids: ["artifact:trace:1"],
      preview_snapshot_artifact_id: PREVIEW_ID,
      review_artifact_ids: ["artifact:review:1"],
      seed: null,
      subject_digest: SUBJECT_DIGEST,
      target: { expected_ref: { artifact_id: BASE_ID, revision: 1 }, ref_name: REF_NAME },
      validation_policy: { profile_id: "builtin.validation", version: 1 },
    });
    const acceptedRun = await screen.findByRole("link", { name: "打开 accepted Run" });
    expect(acceptedRun).toHaveAttribute("href", "/runs/run%3Avalidation");
    expect(acceptedRun.closest('[role="status"]')).not.toBeNull();
  });

  it("validates a first-write Patch by resolving its base from exact direct lineage", async () => {
    const user = userEvent.setup();
    const targetBinding = approvalView("draft").approval.target_binding;
    if (targetBinding?.subject_kind !== "patch") throw new Error("fixture lacks Patch target binding");
    const absentApproval: ApprovalView = {
      ...approvalView("draft"),
      approval: {
        ...approvalView("draft").approval,
        target_binding: {
          ...targetBinding,
          expected_ref: null,
        },
      },
    };
    const absentPatch: PatchView = {
      ...patchView("draft"),
      artifact: {
        ...patchView("draft").artifact,
        parent_artifact_ids: [BASE_ID],
      },
    };
    const accepted = {
      accepted_schema_version: "run-accepted@1" as const,
      events_url: "/api/v1/runs/run:first-write/events",
      run_id: "run:first-write",
      status_url: "/api/v1/runs/run:first-write",
    };
    const validatePatch = vi.fn<PatchWorkflowApi["validatePatch"]>(async () => accepted);
    renderPage(
      api("draft", {
        getApproval: vi.fn(async () => ({ etag: '"approval:first-write"', value: absentApproval })),
        getPatch: vi.fn(async () => ({ etag: '"patch:first-write"', value: absentPatch })),
        listLineage: vi.fn(async () =>
          page(
            [
              {
                artifact: summary(BASE_ID, "ir_snapshot", "ir-core@1", BASE_SNAPSHOT),
                depth: 1,
                entry_schema_version: "lineage-entry@1" as const,
              },
            ],
            "read:first-write-lineage",
          ),
        ),
        listRefHistory: vi.fn(async () => {
          throw workflowProblem("not_found", 404, "The target ref does not exist.");
        }),
        validatePatch,
      }),
    );

    await user.selectOptions(await screen.findByLabelText("Validation policy"), "builtin.validation@1");
    await user.click(screen.getByRole("button", { name: "启动 exact validation" }));

    await waitFor(() => expect(validatePatch).toHaveBeenCalledTimes(1));
    expect(validatePatch.mock.calls[0][1]).toMatchObject({
      base_snapshot_artifact_id: BASE_ID,
      target: { expected_ref: null, ref_name: REF_NAME },
    });
  });

  it.each([
    ["forbidden", 403, "The actor cannot submit this Patch."],
    ["revision_conflict", 409, "The workflow revision changed before submission."],
  ])("keeps %s visible and fail-closed", async (code, status, detail) => {
    const user = userEvent.setup();
    const submitPatchForApproval = vi.fn<PatchWorkflowApi["submitPatchForApproval"]>(async () => {
      throw workflowProblem(code, status, detail);
    });
    renderPage(api("validated", { submitPatchForApproval }));

    const submit = await screen.findByRole("button", { name: "Submit for independent approval" });
    await user.click(submit);

    expect(await screen.findByText(detail)).toBeVisible();
    expect(screen.getByRole("alert")).toHaveAttribute("data-code", code);
    expect(submit).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新读取 exact server state" })).toBeVisible();
  });

  it("retries an unknown repair outcome with the same resolved request and intent", async () => {
    const user = userEvent.setup();
    const accepted: RunAccepted = {
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:repair/events",
      run_id: "run:repair",
      status_url: "/api/v1/runs/run:repair",
    };
    const repairPatch = vi
      .fn<PatchWorkflowApi["repairPatch"]>()
      .mockRejectedValueOnce(new Error("connection dropped"))
      .mockResolvedValueOnce(accepted);
    const executionOption: ExecutionOptionView = {
      cassette_artifact_id: null,
      domain_scope: { domain_ids: ["domain:economy"] },
      execution_version_plan: {
        agent_graph_version: "patch-repair@1",
        model_catalog_digest: "4".repeat(64),
        model_catalog_version: 1,
        nodes: [],
        plan_digest: "5".repeat(64),
        plan_schema_version: "execution-version-plan@1",
        routing_policy_digest: "6".repeat(64),
        routing_policy_version: 1,
      },
      llm_execution_mode: "record" as const,
      option_id: `execution-option:sha256:${"7".repeat(64)}`,
      option_schema_version: "execution-option@1" as const,
      prospective_request_hash: "8".repeat(64),
      resolved_profile_binding_digests: [],
      resolved_request_hash: "9".repeat(64),
      resource_operation_id: "repair_patch_api_v1_patches__artifact_id__repair_post",
      run_kind: { kind: "patch.repair", version: 1 },
      source_run_id: null,
    };
    const resolveExecutionOption = vi.fn<PatchWorkflowApi["resolveExecutionOption"]>(
      async () => executionOption,
    );
    renderPage(api("validation_failed", { repairPatch, resolveExecutionOption }));

    await screen.findByRole("heading", { name: "Exact validation inputs" });
    await user.selectOptions(screen.getByLabelText("Repair policy"), "builtin.patch_repair@1");
    await user.click(screen.getByRole("button", { name: "Resolve 并启动 repair" }));
    await user.click(await screen.findByRole("button", { name: "重试同一 intent" }));

    await waitFor(() => expect(repairPatch).toHaveBeenCalledTimes(2));
    expect(resolveExecutionOption).toHaveBeenCalledTimes(1);
    expect(repairPatch.mock.calls[1][0]).toBe(repairPatch.mock.calls[0][0]);
    expect(repairPatch.mock.calls[1][1]).toBe(repairPatch.mock.calls[0][1]);
  });

  it("applies only the server-frozen target binding after explicit confirmation", async () => {
    const user = userEvent.setup();
    let applied = false;
    const currentStatus = (): ApprovalStatus => (applied ? "applied" : "approved");
    const applyPatch = vi.fn<PatchWorkflowApi["applyPatch"]>(async () => {
      applied = true;
      return {
        approval: approvalView("applied"),
        ref_name: REF_NAME,
        ref_transition_id: null,
        ref_value: { artifact_id: PREVIEW_ID, revision: 2 },
        result_schema_version: "workflow-apply-result@1" as const,
        reversed_approval_id: null,
      };
    });
    renderPage(
      api("approved", {
        applyPatch,
        getApproval: vi.fn(async () => ({ etag: '"approval:apply"', value: approvalView(currentStatus()) })),
        getApprovalBinding: vi.fn(async () => binding(currentStatus())),
        getPatch: vi.fn(async () => ({ etag: '"patch:apply"', value: patchView(currentStatus()) })),
        getSpec: vi.fn<PatchWorkflowApi["getSpec"]>(async (artifactId) => ({
          artifact: summary(
            artifactId,
            "ir_snapshot",
            "ir-core@1",
            artifactId === PREVIEW_ID ? PREVIEW_SNAPSHOT : BASE_SNAPSHOT,
          ),
          ref_name: REF_NAME,
          ref_value: {
            artifact_id: artifactId,
            revision: artifactId === PREVIEW_ID ? 2 : 1,
          },
          schema_registry_version: "ir-core@1",
          snapshot_id: artifactId === PREVIEW_ID ? PREVIEW_SNAPSHOT : BASE_SNAPSHOT,
          view_schema_version: "spec-view@1",
        })),
        listRefHistory: vi.fn(async () =>
          page(
            [
              {
                entry_schema_version: "ref-history-entry@1" as const,
                ref_name: REF_NAME,
                value: { artifact_id: BASE_ID, revision: 1 },
              },
              ...(applied
                ? [
                    {
                      entry_schema_version: "ref-history-entry@1" as const,
                      ref_name: REF_NAME,
                      value: { artifact_id: PREVIEW_ID, revision: 2 },
                    },
                  ]
                : []),
            ],
            applied ? "read:history:after" : "read:history:before",
          ),
        ),
      }),
    );

    await user.click(await screen.findByRole("button", { name: "Apply approved Patch" }));
    await user.click(screen.getByRole("button", { name: "确认 Apply" }));

    await waitFor(() => expect(applyPatch).toHaveBeenCalledTimes(1));
    expect(applyPatch.mock.calls[0][1]).toEqual({
      approval_id: APPROVAL_ID,
      expected_ref: { artifact_id: BASE_ID, revision: 1 },
      expected_workflow_revision: 3,
      ref_name: REF_NAME,
      request_schema_version: "workflow-apply-request@1",
      subject_digest: SUBJECT_DIGEST,
      target_artifact_id: PREVIEW_ID,
      target_digest: TARGET_DIGEST,
    });
    const receiptHeading = await screen.findByRole("heading", {
      name: "Patch 已通过 ref transition 应用",
    });
    expect(receiptHeading.closest('[role="status"]')).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Submit / approval / apply" })).toHaveFocus();
  });
});
