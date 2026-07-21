import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import { storeCsrfToken } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import type {
  ApprovalView,
  ArtifactPayloadView,
  ConstraintSnapshotView,
  ExecutionOptionResolveRequest,
  ExecutionProfileReadView,
  GenerationProposeRequest,
  PatchArtifactReadView,
  RunView,
  SpecView,
} from "./api";
import { createGenerationApi } from "./api";

function response<T>(data: T, headers?: HeadersInit) {
  return {
    data,
    response: Response.json(data, { headers }),
  };
}

const spec = { artifact: { artifact_id: "artifact:spec:base" } } as unknown as SpecView;
const constraint = {
  artifact: { artifact_id: "artifact:constraint:live" },
} as unknown as ConstraintSnapshotView;
const profile = {
  profile: { profile_id: "builtin.generation", version: 3 },
} as unknown as ExecutionProfileReadView;
const run = { revision: 4, run_id: "run:generation:1", status: "succeeded" } as unknown as RunView;
const artifact = {
  artifact: { artifact_id: "artifact:run-result:1" },
} as unknown as ArtifactPayloadView;
const patch = {
  artifact: { artifact_id: "artifact:patch:1" },
  workflow_revision: 2,
} as unknown as PatchArtifactReadView;
const approval = {
  approval: { approval_id: "approval:patch:1" },
} as unknown as ApprovalView;

const executionPlan: components["schemas"]["ExecutionVersionPlanV1"] = {
  agent_graph_version: "generation@3",
  model_catalog_digest: "a".repeat(64),
  model_catalog_version: 4,
  nodes: [
    {
      agent_node_id: "generate",
      allowed_model_snapshots: ["openai/gpt-5.6-sol/m4@1"],
      prompt_version: "generation@3",
      tool_version: "patch-drafter@2",
    },
  ],
  plan_digest: "b".repeat(64),
  plan_schema_version: "execution-version-plan@1",
  routing_policy_digest: "c".repeat(64),
  routing_policy_version: 2,
};

const prospectiveRequest: components["schemas"]["ProspectiveGenerationProposeRequestV1"] = {
  base_snapshot_artifact_id: "artifact:spec:base",
  candidate_export_profiles: [{ profile_id: "builtin.aureus_csv_export", version: 2 }],
  cassette_artifact_id: null,
  constraint_snapshot_artifact_id: "artifact:constraint:live",
  domain_scope: { domain_ids: ["domain:narrative"] },
  execution_version_plan: null,
  findings: [],
  generation_policy: { profile_id: "builtin.generation", version: 3 },
  llm_execution_mode: "record",
  objective_goal_text: "为前哨任务生成可验证的内容补丁。",
  request_schema_version: "generation-propose-request@1",
  target: {
    expected_ref: { artifact_id: "artifact:spec:base", revision: 7 },
    ref_name: "refs/specs/aureus",
  },
};

const resolveRequest: ExecutionOptionResolveRequest = {
  llm_execution_mode: "record",
  prospective_request: prospectiveRequest,
  replay_source_run_id: null,
  request_schema_version: "execution-option-resolve-request@1",
  resource_operation_id: "propose_generation_api_v1_generation_propose_post",
  run_kind: { kind: "generation.propose", version: 1 },
};

const generationRequest: GenerationProposeRequest = {
  ...prospectiveRequest,
  cassette_artifact_id: "artifact:cassette:generation:1",
  execution_version_plan: executionPlan,
};

const intent = Object.freeze({
  idempotencyKey: "11111111-1111-4111-8111-111111111111",
});

describe("generation API", () => {
  beforeEach(() => {
    sessionStorage.clear();
    storeCsrfToken("csrf:generation");
  });

  it("reads the exact Journey A resources and preserves every opaque cursor", async () => {
    const cursor = "opaque.generation+/=%2Ftail";
    const get = vi.fn(async (path: string) => {
      switch (path) {
        case "/api/v1/specs":
        case "/api/v1/constraints":
        case "/api/v1/execution-profiles":
        case "/api/v1/diff":
          return response({ items: [], next_cursor: null });
        case "/api/v1/specs/{artifact_id}":
          return response(spec);
        case "/api/v1/constraints/{artifact_id}":
          return response(constraint);
        case "/api/v1/execution-profiles/{profile_id}/versions/{version}":
          return response(profile);
        case "/api/v1/runs/{run_id}":
          return response(run);
        case "/api/v1/artifacts/{artifact_id}":
          return response(artifact);
        case "/api/v1/patches/{artifact_id}":
          return response(patch, { ETag: '"patch:2"' });
        case "/api/v1/workflow-subjects/{artifact_id}/approval-binding":
          return response({ approval_id: "approval:patch:1" });
        case "/api/v1/approvals/{approval_id}":
          return response(approval, { ETag: '"approval:5"' });
        default:
          throw new Error(`Unexpected GET ${path}`);
      }
    });
    const api = createGenerationApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await api.listSpecs(cursor);
    await api.getSpec("artifact:spec:base");
    await api.listConstraints(cursor);
    await api.getConstraint("artifact:constraint:live");
    await api.listExecutionProfiles(cursor);
    await api.getExecutionProfile("builtin.generation", 3);
    await api.getRun("run:generation:1");
    await api.getArtifact("artifact:run-result:1");
    const versionedPatch = await api.getPatch("artifact:patch:1");
    await api.getApprovalBinding("artifact:patch:1");
    const versionedApproval = await api.getApproval("approval:patch:1");
    await api.getSnapshotDiff("artifact:spec:base", "artifact:preview:1", cursor);

    expect(versionedPatch).toEqual({ etag: '"patch:2"', value: patch });
    expect(versionedApproval).toEqual({ etag: '"approval:5"', value: approval });
    expect(get).toHaveBeenCalledWith("/api/v1/specs", { params: { query: { cursor } } });
    expect(get).toHaveBeenCalledWith("/api/v1/specs/{artifact_id}", {
      params: { path: { artifact_id: "artifact:spec:base" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/constraints", { params: { query: { cursor } } });
    expect(get).toHaveBeenCalledWith("/api/v1/constraints/{artifact_id}", {
      params: { path: { artifact_id: "artifact:constraint:live" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/execution-profiles", {
      params: { query: { cursor } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/execution-profiles/{profile_id}/versions/{version}", {
      params: { path: { profile_id: "builtin.generation", version: 3 } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/runs/{run_id}", {
      params: { path: { run_id: "run:generation:1" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/artifacts/{artifact_id}", {
      params: { path: { artifact_id: "artifact:run-result:1" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/patches/{artifact_id}", {
      params: { path: { artifact_id: "artifact:patch:1" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/workflow-subjects/{artifact_id}/approval-binding", {
      params: { path: { artifact_id: "artifact:patch:1" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/approvals/{approval_id}", {
      params: { path: { approval_id: "approval:patch:1" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/diff", {
      params: {
        query: { base: "artifact:spec:base", cursor, target: "artifact:preview:1" },
      },
    });
  });

  it("turns every paged 410 into an explicit restart boundary without changing the cursor", async () => {
    const staleCursor = "stale.opaque+/=";
    const get = vi.fn(async () => ({
      error: {
        code: "cursor_expired",
        detail: "read snapshot expired",
        instance: "/api/v1/resources",
        request_id: "request:cursor",
        status: 410,
        title: "Cursor expired",
        type: "about:blank",
      },
      response: new Response(undefined, { status: 410 }),
    }));
    const api = createGenerationApi({ GET: get } as unknown as GameForgeOpenApiClient);

    for (const read of [
      () => api.listSpecs(staleCursor),
      () => api.listConstraints(staleCursor),
      () => api.listExecutionProfiles(staleCursor),
      () => api.getSnapshotDiff("artifact:base", "artifact:target", staleCursor),
    ]) {
      await expect(read()).rejects.toMatchObject({
        name: "CursorExpiredError",
        staleCursor,
      });
    }

    expect(get).toHaveBeenCalledTimes(4);
  });

  it("requires ETags for Patch and Approval workflow reads", async () => {
    const get = vi.fn(async (path: string) =>
      path === "/api/v1/patches/{artifact_id}" ? response(patch) : response(approval),
    );
    const api = createGenerationApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.getPatch("artifact:patch:1")).rejects.toThrow(
      "The server response did not include the required ETag.",
    );
    await expect(api.getApproval("approval:patch:1")).rejects.toThrow(
      "The server response did not include the required ETag.",
    );
  });

  it("resolves the exact prospective generation request with CSRF only", async () => {
    const option = { option_id: "option:generation:1" };
    const post = vi.fn(async (_path: string, _options: unknown) => response(option));
    const api = createGenerationApi({ POST: post } as unknown as GameForgeOpenApiClient);

    await expect(api.resolveExecutionOption(resolveRequest)).resolves.toBe(option);

    expect(post).toHaveBeenCalledWith("/api/v1/execution-options:resolve", {
      body: resolveRequest,
      params: { header: { "X-CSRF-Token": "csrf:generation" } },
    });
    const sent = post.mock.calls[0]?.[1] as { body: ExecutionOptionResolveRequest };
    expect(sent.body.prospective_request).toMatchObject({
      cassette_artifact_id: null,
      execution_version_plan: null,
    });
  });

  it("never retries generation automatically and lets the caller reuse one frozen body and intent", async () => {
    const unknownOutcome = new TypeError("network result unknown");
    const accepted = { run_id: "run:generation:accepted" };
    const post = vi.fn().mockRejectedValueOnce(unknownOutcome).mockResolvedValueOnce(response(accepted));
    const api = createGenerationApi({ POST: post } as unknown as GameForgeOpenApiClient);
    const frozenRequest = Object.freeze(generationRequest);

    await expect(api.proposeGeneration(frozenRequest, intent)).rejects.toBe(unknownOutcome);
    expect(post).toHaveBeenCalledTimes(1);

    await expect(api.proposeGeneration(frozenRequest, intent)).resolves.toBe(accepted);
    expect(post).toHaveBeenCalledTimes(2);
    for (const [, options] of post.mock.calls) {
      const sent = options as {
        body: GenerationProposeRequest;
        params: { header: Record<string, string> };
      };
      expect(sent.body).toBe(frozenRequest);
      expect(sent.params.header).toEqual({
        "Idempotency-Key": intent.idempotencyKey,
        "X-CSRF-Token": "csrf:generation",
      });
      expect(sent.params.header).not.toHaveProperty("If-Match");
    }
  });

  it("exposes the shared RunEventStream shape without copying SSE parsing", () => {
    const api = createGenerationApi({} as GameForgeOpenApiClient);
    const stream = api.createEventStream({
      onEvent: vi.fn(),
      onStateChange: vi.fn(),
      runId: "run:generation:1",
    });

    expect(stream.close).toEqual(expect.any(Function));
    expect(stream.restart).toEqual(expect.any(Function));
    expect(stream.start).toEqual(expect.any(Function));
    stream.close();
  });
});
