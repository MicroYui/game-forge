import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { createQueryClient } from "../../api/query-client";
import type { PatchWorkflowApi, RollbackRequestReadView } from "./api";
import { RefHistoryPage } from "./RefHistoryPage";

const REF_NAME = "spec/main";
const TARGET_ID = "artifact:spec:baseline";
const CURRENT_ID = "artifact:spec:current";

function page<T>(items: T[], snapshot: string) {
  return {
    expires_at: "2026-07-20T08:00:00Z",
    items,
    next_cursor: null,
    page_schema_version: "page@1" as const,
    read_snapshot_id: snapshot,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

const rollbackProfile: components["schemas"]["ExecutionProfileViewV1"] = {
  compatible_run_kinds: [{ kind: "rollback.validate", version: 1 }],
  display_name: "Safe design rollback",
  domain_scope: { domain_ids: ["domain:economy"] },
  env_contract_version: null,
  input_schema_ids: [],
  output_schema_ids: [],
  profile: { profile_id: "builtin.rollback", version: 3 },
  profile_kind: "rollback",
  profile_payload_hash: "a".repeat(64),
  required_capabilities: [],
  status: "active",
  stochastic: false,
  target_environment_profile: null,
};

function renderPage(api: PatchWorkflowApi) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <RefHistoryPage api={api} refName={REF_NAME} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Ref history rollback draft", () => {
  it("freezes the selected historical revision and retries an unknown outcome with one intent", async () => {
    const user = userEvent.setup();
    const created = {
      artifact: { artifact_id: "artifact:rollback:new" },
      request: { ref_name: REF_NAME },
    } as unknown as RollbackRequestReadView;
    const draftRollback = vi
      .fn<PatchWorkflowApi["draftRollback"]>()
      .mockRejectedValueOnce(new Error("connection dropped"))
      .mockResolvedValueOnce(created);
    const api = {
      draftRollback,
      getArtifact: vi.fn(async (artifactId) => ({
        artifact: {
          artifact_id: artifactId,
          domain_scope: { domain_ids: ["domain:economy"] },
        },
      })),
      listExecutionProfiles: vi.fn(async () => page([rollbackProfile], "profiles:rollback")),
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
          ],
          "history:main",
        ),
      ),
    } as unknown as PatchWorkflowApi;
    renderPage(api);

    expect(await screen.findByText("Current · revision 2")).toBeVisible();
    await user.click(screen.getByRole("radio", { name: `revision 1 · ${TARGET_ID}` }));
    await screen.findByRole("option", { name: /Safe design rollback/ });
    await user.selectOptions(screen.getByLabelText("Rollback policy"), "builtin.rollback@3");
    await user.type(screen.getByLabelText("Rollback reason"), "Restore the reviewed economy baseline.");
    await user.click(screen.getByRole("button", { name: "创建 Rollback request" }));
    await user.click(await screen.findByRole("button", { name: "重试同一 intent" }));

    await waitFor(() => expect(draftRollback).toHaveBeenCalledTimes(2));
    expect(draftRollback.mock.calls[0][0]).toBe(REF_NAME);
    expect(draftRollback.mock.calls[0][1]).toEqual({
      expected_current_ref: { artifact_id: CURRENT_ID, revision: 2 },
      reason: "Restore the reviewed economy baseline.",
      request_schema_version: "rollback-draft-request@1",
      reverses_approval_id: null,
      rollback_profile: { profile_id: "builtin.rollback", version: 3 },
      target_artifact_id: TARGET_ID,
      target_history_revision: 1,
    });
    expect(draftRollback.mock.calls[1][1]).toBe(draftRollback.mock.calls[0][1]);
    expect(draftRollback.mock.calls[1][2]).toBe(draftRollback.mock.calls[0][2]);
    const createdLink = await screen.findByRole("link", { name: "打开 Rollback request" });
    expect(createdLink).toHaveAttribute("href", "/rollback-requests/artifact%3Arollback%3Anew");
    expect(createdLink.closest('[role="status"]')).not.toBeNull();
    expect(screen.getByText(/draft 创建不会移动 ref/)).toBeVisible();
  });

  it("keeps draft actions locked until an explicit authority reload succeeds", async () => {
    const user = userEvent.setup();
    const history = page(
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
      ],
      "history:main",
    );
    const refetch = deferred<typeof history>();
    const listRefHistory = vi
      .fn<PatchWorkflowApi["listRefHistory"]>()
      .mockResolvedValueOnce(history)
      .mockImplementationOnce(async () => refetch.promise);
    const api = {
      draftRollback: vi.fn(async () => {
        throw new Error("draft transport failed");
      }),
      getArtifact: vi.fn(async (artifactId) => ({
        artifact: {
          artifact_id: artifactId,
          domain_scope: { domain_ids: ["domain:economy"] },
        },
      })),
      listExecutionProfiles: vi.fn(async () => page([rollbackProfile], "profiles:rollback")),
      listRefHistory,
    } as unknown as PatchWorkflowApi;
    renderPage(api);

    await user.click(await screen.findByRole("radio", { name: `revision 1 · ${TARGET_ID}` }));
    await screen.findByRole("option", { name: /Safe design rollback/ });
    await user.selectOptions(screen.getByLabelText("Rollback policy"), "builtin.rollback@3");
    await user.type(screen.getByLabelText("Rollback reason"), "Restore the reviewed baseline.");
    const createButton = screen.getByRole("button", { name: "创建 Rollback request" });
    await user.click(createButton);
    await screen.findByText("draft transport failed");

    await user.click(screen.getByRole("button", { name: "重新读取 authority" }));
    expect(createButton).toBeDisabled();
    refetch.resolve(history);

    await waitFor(() => expect(createButton).toBeEnabled());
  });

  it("rejects a historical target whose domain scope cannot contain the current ref", async () => {
    const user = userEvent.setup();
    const draftRollback = vi.fn<PatchWorkflowApi["draftRollback"]>();
    const api = {
      draftRollback,
      getArtifact: vi.fn(async (artifactId) => ({
        artifact: {
          artifact_id: artifactId,
          domain_scope: {
            domain_ids: artifactId === CURRENT_ID ? ["domain:combat", "domain:economy"] : ["domain:economy"],
          },
        },
      })),
      listExecutionProfiles: vi.fn(async () => page([rollbackProfile], "profiles:rollback")),
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
          ],
          "history:cross-domain",
        ),
      ),
    } as unknown as PatchWorkflowApi;
    renderPage(api);

    await user.click(await screen.findByRole("radio", { name: `revision 1 · ${TARGET_ID}` }));

    expect(await screen.findByRole("heading", { name: "Rollback profiles 不可用" })).toBeVisible();
    expect(screen.getByRole("button", { name: "创建 Rollback request" })).toBeDisabled();
    expect(draftRollback).not.toHaveBeenCalled();
  });
});
