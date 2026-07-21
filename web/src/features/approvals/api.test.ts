import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import { storeCsrfToken } from "../../api/csrf";
import type { ApprovalViewData } from "./api";
import { createApprovalsApi } from "./api";

function response<T>(data: T, headers?: HeadersInit) {
  return { data, response: Response.json(data, { headers }) };
}

const approval = {
  approval: {
    approval_id: "approval:multi-domain:7",
    workflow_revision: 12,
  },
} as unknown as ApprovalViewData;

const intent = { idempotencyKey: "11111111-1111-4111-8111-111111111111" } as const;

describe("Approvals API", () => {
  beforeEach(() => {
    sessionStorage.clear();
    storeCsrfToken("csrf:approvals");
  });

  it("reads only the current actor queue and exact versioned detail", async () => {
    const cursor = "opaque.approvals+/=%2Ftail";
    const get = vi.fn(async (path: string) => {
      if (path === "/api/v1/approvals") {
        return response({ items: [], next_cursor: null, read_snapshot_id: "snapshot:approval-list" });
      }
      return response(approval, { ETag: '"approval:opaque-12"' });
    });
    const api = createApprovalsApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await api.listMine(cursor);
    await expect(api.getApproval("approval:multi-domain:7")).resolves.toEqual({
      etag: '"approval:opaque-12"',
      value: approval,
    });

    expect(get).toHaveBeenCalledWith("/api/v1/approvals", {
      params: { query: { assignee: "me", cursor, limit: 100 } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/approvals/{approval_id}", {
      params: { path: { approval_id: "approval:multi-domain:7" } },
    });
  });

  it("requires the detail ETag instead of inventing workflow authority", async () => {
    const get = vi.fn(async () => response(approval));
    const api = createApprovalsApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.getApproval("approval:multi-domain:7")).rejects.toThrow(
      "The server response did not include the required ETag.",
    );
  });

  it("rejects a detail or decision response owned by another approval route", async () => {
    const wrong = {
      ...approval,
      approval: { ...approval.approval, approval_id: "approval:other" },
    };
    const get = vi.fn(async () => response(wrong, { ETag: '"approval:other"' }));
    const post = vi.fn(async () => response(wrong, { ETag: '"approval:other"' }));
    const api = createApprovalsApi({ GET: get, POST: post } as unknown as GameForgeOpenApiClient);

    await expect(api.getApproval("approval:multi-domain:7")).rejects.toThrow(
      "does not belong to the requested approval",
    );
    await expect(
      api.decide(
        { etag: '"approval:opaque-12"', value: approval },
        {
          action: "approve",
          comment: null,
          reasonCode: "reviewed",
          requirementIds: ["requirement:economy"],
        },
        intent,
      ),
    ).rejects.toThrow("does not belong to the requested approval");
  });

  it.each(["approve", "reject", "request_changes"] as const)(
    "submits %s with selected requirements, exact revision, ETag, and one caller-owned intent",
    async (action) => {
      const post = vi.fn(async () =>
        response(
          {
            ...approval,
            approval: { ...approval.approval, workflow_revision: 13 },
          },
          { ETag: '"approval:opaque-13"' },
        ),
      );
      const api = createApprovalsApi({ POST: post } as unknown as GameForgeOpenApiClient);
      const current = { etag: '"approval:opaque-12"', value: approval };

      await expect(
        api.decide(
          current,
          {
            action,
            comment: "逐项复核完成。",
            reasonCode: `human_${action}`,
            requirementIds: ["requirement:narrative", "requirement:economy"],
          },
          intent,
        ),
      ).resolves.toMatchObject({ etag: '"approval:opaque-13"' });

      expect(post).toHaveBeenCalledOnce();
      expect(post).toHaveBeenCalledWith(`/api/v1/approvals/{approval_id}:${action}`, {
        body: {
          comment: "逐项复核完成。",
          decision: action,
          expected_workflow_revision: 12,
          reason_code: `human_${action}`,
          request_schema_version: "approval-decision-request@1",
          requirement_ids: ["requirement:narrative", "requirement:economy"],
        },
        params: {
          header: {
            "Idempotency-Key": intent.idempotencyKey,
            "If-Match": '"approval:opaque-12"',
            "X-CSRF-Token": "csrf:approvals",
          },
          path: { approval_id: "approval:multi-domain:7" },
        },
      });
    },
  );

  it.each([
    [403, "forbidden"],
    [409, "revision_conflict"],
  ])("preserves %i/%s without an automatic retry", async (status, code) => {
    const problem = {
      code,
      conflict_set_id: null,
      detail: `${code}: current role or workflow authority changed`,
      earliest_cursor: null,
      instance: "/api/v1/approvals/approval:multi-domain:7:approve",
      request_id: `request:${status}`,
      retry_after_s: null,
      run_id: null,
      status,
      title: code,
      trace_id: null,
      type: "about:blank",
    };
    const post = vi.fn(async () => ({
      error: problem,
      response: Response.json(problem, { status }),
    }));
    const api = createApprovalsApi({ POST: post } as unknown as GameForgeOpenApiClient);

    await expect(
      api.decide(
        { etag: '"approval:opaque-12"', value: approval },
        {
          action: "approve",
          comment: null,
          reasonCode: "human_approved",
          requirementIds: ["requirement:economy"],
        },
        intent,
      ),
    ).rejects.toMatchObject({ name: "ApiProblemError", problem });
    expect(post).toHaveBeenCalledOnce();
  });

  it("keeps a stale page cursor as an explicit restart boundary", async () => {
    const staleCursor = "stale.approvals+/=";
    const problem = {
      code: "cursor_expired",
      detail: "approval queue read snapshot expired",
      instance: "/api/v1/approvals",
      request_id: "request:approval-cursor",
      status: 410,
      title: "Cursor expired",
      type: "about:blank",
    };
    const get = vi.fn(async () => ({
      error: problem,
      response: Response.json(problem, { status: 410 }),
    }));
    const api = createApprovalsApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.listMine(staleCursor)).rejects.toMatchObject({
      name: "CursorExpiredError",
      staleCursor,
    });
  });
});
