import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/query-client";
import type { PatchArtifactReadView, PatchWorkflowApi, RollbackRequestReadView } from "./api";
import { PatchWorkspacePage } from "./PatchWorkspacePage";

const PATCH_ID = "artifact:patch:workspace";
const ROLLBACK_ID = "artifact:rollback:workspace";

const patch = {
  approval_status: "validated",
  artifact: {
    artifact_id: PATCH_ID,
    created_at: "2026-07-20T05:00:00Z",
    domain_scope: { domain_ids: ["domain:economy"] },
    kind: "patch",
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: ["artifact:base", "artifact:preview"],
    payload_hash: "a".repeat(64),
    payload_schema_id: "patch@2",
    summary_schema_version: "artifact-summary@1",
    version_tuple: { ir_snapshot_id: "snapshot:preview" },
  },
  patch: {
    base_snapshot_id: "snapshot:base",
    expected_to_fix: ["finding:gold"],
    ops: [],
    patch_schema_version: "patch@2",
    preconditions: [],
    produced_by: "human",
    producer_run_id: null,
    rationale: "Lower the reward.",
    revision: 3,
    side_effect_risk: "low",
    supersedes_artifact_id: "artifact:patch:workspace:2",
    target_snapshot_id: "snapshot:preview",
  },
  regression_status: "passed",
  validation_status: "passed",
  view_schema_version: "patch-artifact-read-view@1",
  workflow_revision: 7,
} satisfies PatchArtifactReadView;

const rollback = {
  approval_status: "pending_approval",
  artifact: {
    ...patch.artifact,
    artifact_id: ROLLBACK_ID,
    kind: "rollback_request",
    payload_hash: "b".repeat(64),
    payload_schema_id: "rollback-request@1",
    version_tuple: {},
  },
  request: {
    expected_current_ref: { artifact_id: "artifact:head", revision: 4 },
    reason: "Restore the last approved economy snapshot.",
    ref_name: "spec/main",
    reverses_approval_id: "approval:patch:head",
    rollback_profile_binding: {
      catalog_digest: "c".repeat(64),
      catalog_version: 1,
      expected_profile_kind: "rollback",
      field_path: "rollback_profile",
      profile: { profile_id: "builtin.rollback", version: 1 },
      profile_payload_hash: "d".repeat(64),
    },
    rollback_schema_version: "rollback-request@1",
    target_artifact_id: "artifact:base",
    target_history_revision: 1,
  },
  view_schema_version: "rollback-request-read-view@1",
  workflow_revision: 5,
} satisfies RollbackRequestReadView;

function page<T>(items: T[], snapshot: string, nextCursor: string | null = null) {
  return {
    expires_at: "2026-07-20T06:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "page@1" as const,
    read_snapshot_id: snapshot,
  };
}

function api(overrides: Partial<PatchWorkflowApi> = {}): PatchWorkflowApi {
  return {
    listPatches: vi.fn(async () => page([patch], "read:patches")),
    listRollbackRequests: vi.fn(async () => page([rollback], "read:rollbacks")),
    ...overrides,
  } as unknown as PatchWorkflowApi;
}

function renderPage(patchApi: PatchWorkflowApi) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <PatchWorkspacePage api={patchApi} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Patch workspace", () => {
  it("keeps immutable Patch revisions and rollback requests as separate ledgers", async () => {
    renderPage(api());

    const patchLedger = await screen.findByRole("region", { name: "Patch revision ledger" });
    expect(await within(patchLedger).findByText("revision 3")).toBeVisible();
    expect(within(patchLedger).getByText(/validated · workflow 7/)).toBeVisible();
    expect(within(patchLedger).getByRole("link", { name: `打开 ${PATCH_ID}` })).toHaveAttribute(
      "href",
      `/patches/${encodeURIComponent(PATCH_ID)}`,
    );

    const rollbackLedger = screen
      .getByRole("heading", { name: "Rollback request ledger", level: 2 })
      .closest("section");
    expect(rollbackLedger).not.toBeNull();
    expect(within(rollbackLedger!).getByText("spec/main")).toBeVisible();
    expect(within(rollbackLedger!).getByText(/history revision 1/)).toBeVisible();
    expect(within(rollbackLedger!).getByRole("link", { name: `打开 ${ROLLBACK_ID}` })).toHaveAttribute(
      "href",
      `/rollback-requests/${encodeURIComponent(ROLLBACK_ID)}`,
    );
  });

  it("keeps a bounded snapshot transition keyboard-scrollable instead of growing a tall row", async () => {
    const longBase = `snapshot:${"b".repeat(503)}`;
    const longTarget = `snapshot:${"t".repeat(503)}`;
    const longTransitionPatch = {
      ...patch,
      patch: {
        ...patch.patch,
        base_snapshot_id: longBase,
        target_snapshot_id: longTarget,
      },
    };

    renderPage(
      api({
        listPatches: vi.fn(async () => page([longTransitionPatch], "read:long-transition")),
      }),
    );

    const transition = await screen.findByText(`${longBase} → ${longTarget}`);
    expect(transition).toHaveClass("gf-copyable__value--scrollable");
    expect(transition).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("button", { name: "复制 Snapshot transition" })).toBeVisible();
  });

  it("keeps cursor pages on one read snapshot", async () => {
    const user = userEvent.setup();
    const listPatches = vi
      .fn()
      .mockResolvedValueOnce(page([patch], "read:patches", "cursor:2"))
      .mockResolvedValueOnce(
        page(
          [{ ...patch, artifact: { ...patch.artifact, artifact_id: "artifact:patch:older" } }],
          "read:patches",
        ),
      );
    renderPage(api({ listPatches }));

    await user.click(await screen.findByRole("button", { name: "加载下一页" }));

    const ledger = screen
      .getByRole("heading", { name: "Patch revision ledger", level: 2 })
      .closest("section");
    expect(ledger).not.toBeNull();
    expect(await within(ledger!).findByRole("link", { name: "打开 artifact:patch:older" })).toBeVisible();
    expect(listPatches).toHaveBeenNthCalledWith(2, "cursor:2");
  });
});
