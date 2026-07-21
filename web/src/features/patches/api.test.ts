import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import { storeCsrfToken } from "../../api/csrf";
import type {
  ApprovalView,
  ExecutionOptionResolveRequest,
  HumanPatchDraftRequest,
  PatchArtifactReadView,
  PatchRebaseRequest,
  PatchRepairRequest,
  PatchValidationAdmissionRequest,
  ResolveConflictsRequest,
  RollbackDraftRequest,
  RollbackRequestReadView,
  RollbackValidationAdmissionRequest,
  SubmitForApprovalRequest,
  WorkflowApplyRequest,
} from "./api";
import { createPatchWorkflowApi } from "./api";

function response<T>(data: T, headers?: HeadersInit) {
  return { data, response: Response.json(data, { headers }) };
}

const patch = {
  artifact: { artifact_id: "artifact:patch:7" },
  workflow_revision: 9,
} as unknown as PatchArtifactReadView;

const rollback = {
  artifact: { artifact_id: "artifact:rollback:4" },
  workflow_revision: 5,
} as unknown as RollbackRequestReadView;

const approval = {
  approval: { approval_id: "approval:7" },
} as unknown as ApprovalView;

const intent = { idempotencyKey: "11111111-1111-4111-8111-111111111111" } as const;

describe("Patch and rollback workflow API", () => {
  beforeEach(() => {
    sessionStorage.clear();
    storeCsrfToken("csrf:patches");
  });

  it("reads the exact Patch, rollback, provenance, approval, and profile projections", async () => {
    const cursor = "opaque.patch+/=%2Ftail";
    const get = vi.fn(async (path: string) => {
      switch (path) {
        case "/api/v1/patches":
        case "/api/v1/rollback-requests":
        case "/api/v1/artifacts/{artifact_id}/lineage":
        case "/api/v1/execution-profiles":
        case "/api/v1/refs/{ref_name}/history":
        case "/api/v1/diff":
        case "/api/v1/conflict-sets/{conflict_set_id}/conflicts":
          return response({ items: [], next_cursor: null });
        case "/api/v1/patches/{artifact_id}":
          return response(patch, { ETag: '"patch:opaque-9"' });
        case "/api/v1/rollback-requests/{artifact_id}":
          return response(rollback, { ETag: '"rollback:opaque-5"' });
        case "/api/v1/workflow-subjects/{artifact_id}/approval-binding":
          return response({ approval_id: "approval:7" });
        case "/api/v1/approvals/{approval_id}":
          return response(approval, { ETag: '"approval:opaque-9"' });
        case "/api/v1/execution-profiles/{profile_id}/versions/{version}":
          return response({
            profile: { profile_id: "builtin.rollback", version: 3 },
            profile_kind: "rollback",
          });
        case "/api/v1/specs/{artifact_id}":
          return response({ artifact: { artifact_id: "artifact:snapshot:7" } });
        case "/api/v1/artifacts/{artifact_id}":
          return response({ artifact: { artifact_id: "artifact:patch:7" }, payload: {} });
        default:
          throw new Error(`Unexpected GET ${path}`);
      }
    });
    const api = createPatchWorkflowApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await api.listPatches(cursor);
    const versionedPatch = await api.getPatch("artifact:patch:7");
    await api.listRollbackRequests(cursor);
    const versionedRollback = await api.getRollbackRequest("artifact:rollback:4");
    await api.getApprovalBinding("artifact:patch:7");
    const versionedApproval = await api.getApproval("approval:7");
    await api.getSpec("artifact:snapshot:7");
    await api.getArtifact("artifact:patch:7");
    await api.listLineage("artifact:patch:7", cursor);
    await api.listExecutionProfiles(
      { profile_kind: "rollback", status: "active", domain_id: "domain:economy" },
      cursor,
    );
    await api.getExecutionProfile("builtin.rollback", 3);
    await api.listRefHistory("refs/design/live", cursor);
    await api.getSnapshotDiff("artifact:base", "artifact:target", cursor);
    await api.listConflicts("conflict-set:7", cursor);

    expect(versionedPatch).toEqual({ etag: '"patch:opaque-9"', value: patch });
    expect(versionedRollback).toEqual({ etag: '"rollback:opaque-5"', value: rollback });
    expect(versionedApproval).toEqual({ etag: '"approval:opaque-9"', value: approval });
    expect(get).toHaveBeenCalledWith("/api/v1/execution-profiles", {
      params: {
        query: {
          cursor,
          domain_id: "domain:economy",
          profile_kind: "rollback",
          status: "active",
        },
      },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/execution-profiles/{profile_id}/versions/{version}", {
      params: { path: { profile_id: "builtin.rollback", version: 3 } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/diff", {
      params: { query: { base: "artifact:base", cursor, target: "artifact:target" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/conflict-sets/{conflict_set_id}/conflicts", {
      params: { path: { conflict_set_id: "conflict-set:7" }, query: { cursor } },
    });
  });

  it("turns every paged 410 into an explicit restart boundary without changing the cursor", async () => {
    const staleCursor = "stale.patch+/=";
    const get = vi.fn(async () => ({
      error: {
        code: "cursor_expired",
        detail: "read snapshot expired",
        instance: "/api/v1/patches",
        request_id: "request:patch-cursor",
        status: 410,
        title: "Cursor expired",
        type: "about:blank",
      },
      response: new Response(undefined, { status: 410 }),
    }));
    const api = createPatchWorkflowApi({ GET: get } as unknown as GameForgeOpenApiClient);

    for (const read of [
      () => api.listPatches(staleCursor),
      () => api.listRollbackRequests(staleCursor),
      () => api.listLineage("artifact:patch:7", staleCursor),
      () => api.listExecutionProfiles({}, staleCursor),
      () => api.listRefHistory("refs/design/live", staleCursor),
      () => api.getSnapshotDiff("artifact:base", "artifact:target", staleCursor),
      () => api.listConflicts("conflict-set:7", staleCursor),
    ]) {
      await expect(read()).rejects.toMatchObject({
        name: "CursorExpiredError",
        staleCursor,
      });
    }

    expect(get).toHaveBeenCalledTimes(7);
  });

  it("requires opaque ETags for every versioned workflow resource", async () => {
    const get = vi.fn(async () => response(patch));
    const api = createPatchWorkflowApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.getPatch("artifact:patch:7")).rejects.toThrow(
      "The server response did not include the required ETag.",
    );
    await expect(api.getRollbackRequest("artifact:rollback:4")).rejects.toThrow(
      "The server response did not include the required ETag.",
    );
    await expect(api.getApproval("approval:7")).rejects.toThrow(
      "The server response did not include the required ETag.",
    );
  });

  it("uses the current Patch ETag for every versioned Patch command", async () => {
    const post = vi.fn(async (path: string, _options: unknown) => {
      if (path.endsWith(":validate")) return response({ run_id: "run:validate" });
      if (path.endsWith(":submit-for-approval")) return response(approval);
      if (path.endsWith(":apply")) return response({ ref_name: "refs/design/live" });
      return response({ status: "clean", new_patch_artifact_id: "artifact:patch:8" });
    });
    const api = createPatchWorkflowApi({ POST: post } as unknown as GameForgeOpenApiClient);
    const current = { etag: '"patch:opaque-9"', value: patch };

    await api.rebasePatch(current, {} as PatchRebaseRequest, intent);
    await api.resolvePatchConflicts(current, {} as ResolveConflictsRequest, intent);
    await api.validatePatch(current, {} as PatchValidationAdmissionRequest, intent);
    await api.submitPatchForApproval(current, {} as SubmitForApprovalRequest, intent);
    await api.applyPatch(current, {} as WorkflowApplyRequest, intent);

    for (const path of [
      "/api/v1/patches/{artifact_id}:rebase",
      "/api/v1/patches/{artifact_id}:resolve-conflicts",
      "/api/v1/patches/{artifact_id}:validate",
      "/api/v1/patches/{artifact_id}:submit-for-approval",
      "/api/v1/patches/{artifact_id}:apply",
    ]) {
      expect(post).toHaveBeenCalledWith(
        path,
        expect.objectContaining({
          params: {
            header: {
              "Idempotency-Key": intent.idempotencyKey,
              "If-Match": '"patch:opaque-9"',
              "X-CSRF-Token": "csrf:patches",
            },
            path: { artifact_id: "artifact:patch:7" },
          },
        }),
      );
    }
  });

  it("uses the current rollback ETag for every versioned rollback command", async () => {
    const post = vi.fn(async (path: string, _options: unknown) => {
      if (path.endsWith(":validate")) return response({ run_id: "run:rollback-validate" });
      if (path.endsWith(":submit-for-approval")) return response(approval);
      return response({ ref_name: "refs/design/live" });
    });
    const api = createPatchWorkflowApi({ POST: post } as unknown as GameForgeOpenApiClient);
    const current = { etag: '"rollback:opaque-5"', value: rollback };

    await api.validateRollback(current, {} as RollbackValidationAdmissionRequest, intent);
    await api.submitRollbackForApproval(current, {} as SubmitForApprovalRequest, intent);
    await api.applyRollback(current, {} as WorkflowApplyRequest, intent);

    for (const path of [
      "/api/v1/rollback-requests/{artifact_id}:validate",
      "/api/v1/rollback-requests/{artifact_id}:submit-for-approval",
      "/api/v1/rollback-requests/{artifact_id}:apply",
    ]) {
      expect(post).toHaveBeenCalledWith(
        path,
        expect.objectContaining({
          params: {
            header: {
              "Idempotency-Key": intent.idempotencyKey,
              "If-Match": '"rollback:opaque-5"',
              "X-CSRF-Token": "csrf:patches",
            },
            path: { artifact_id: "artifact:rollback:4" },
          },
        }),
      );
    }
  });

  it("keeps create and repair mutation identity independent of resource ETags", async () => {
    const post = vi.fn(async (path: string, _options: unknown) => {
      if (path.endsWith(":repair")) return response({ run_id: "run:repair" });
      if (path.includes("rollback-requests")) return response(rollback);
      return response(patch);
    });
    const api = createPatchWorkflowApi({ POST: post } as unknown as GameForgeOpenApiClient);
    const repairRequest = {
      params: { subject_patch_artifact_id: "artifact:patch:7" },
    } as PatchRepairRequest;
    const rollbackRequest = {} as RollbackDraftRequest;

    await api.draftPatch({} as HumanPatchDraftRequest, intent);
    await api.repairPatch(repairRequest, intent);
    await api.draftRollback("refs/design/live", rollbackRequest, intent);

    expect(post.mock.calls.map(([path]) => path)).toEqual([
      "/api/v1/patches",
      "/api/v1/patches/{artifact_id}:repair",
      "/api/v1/refs/{ref_name}/rollback-requests",
    ]);
    for (const [, options] of post.mock.calls) {
      const headers = (options as { params: { header: Record<string, string> } }).params.header;
      expect(headers).toEqual({
        "Idempotency-Key": intent.idempotencyKey,
        "X-CSRF-Token": "csrf:patches",
      });
      expect(headers).not.toHaveProperty("If-Match");
    }
    expect(post).toHaveBeenCalledWith("/api/v1/patches/{artifact_id}:repair", {
      body: repairRequest,
      params: {
        header: {
          "Idempotency-Key": intent.idempotencyKey,
          "X-CSRF-Token": "csrf:patches",
        },
        path: { artifact_id: "artifact:patch:7" },
      },
    });
    expect(post).toHaveBeenCalledWith("/api/v1/refs/{ref_name}/rollback-requests", {
      body: rollbackRequest,
      params: {
        header: {
          "Idempotency-Key": intent.idempotencyKey,
          "X-CSRF-Token": "csrf:patches",
        },
        path: { ref_name: "refs/design/live" },
      },
    });
  });

  it("resolves a frozen execution option with CSRF only", async () => {
    const resolved = { option_id: "option:repair:7" };
    const post = vi.fn(async () => response(resolved));
    const api = createPatchWorkflowApi({ POST: post } as unknown as GameForgeOpenApiClient);
    const request = {} as ExecutionOptionResolveRequest;

    await expect(api.resolveExecutionOption(request)).resolves.toBe(resolved);
    expect(post).toHaveBeenCalledWith("/api/v1/execution-options:resolve", {
      body: request,
      params: { header: { "X-CSRF-Token": "csrf:patches" } },
    });
  });
});
