import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import { storeCsrfToken } from "../../api/csrf";
import type {
  ApprovalView,
  ConstraintProposeRequest,
  ConstraintProposalReadView,
  ConstraintSnapshotView,
  ConstraintValidationAdmissionRequest,
  ExecutionOptionResolveRequest,
  HumanConstraintDraftRequest,
  HumanConstraintRevisionRequest,
  HumanPatchDraftRequest,
  HumanSpecUploadRequest,
  SpecView,
  SubmitForApprovalRequest,
  WorkflowApplyRequest,
} from "./api";
import { createSpecWorkflowApi } from "./api";

function response<T>(data: T, headers?: HeadersInit) {
  return {
    data,
    response: Response.json(data, { headers }),
  };
}

const spec = {
  artifact: { artifact_id: "artifact:spec-1" },
} as unknown as SpecView;

const proposal = {
  artifact: { artifact_id: "artifact:proposal-1" },
  workflow_revision: 7,
} as unknown as ConstraintProposalReadView;

const constraint = {
  artifact: { artifact_id: "artifact:constraint-1" },
} as unknown as ConstraintSnapshotView;

const approval = {
  approval: { id: "approval:1" },
} as unknown as ApprovalView;

const intent = { idempotencyKey: "11111111-1111-4111-8111-111111111111" } as const;

describe("Spec and constraint workflow API", () => {
  beforeEach(() => {
    sessionStorage.clear();
    storeCsrfToken("csrf:specs");
  });

  it("reads every workspace projection through its exact typed endpoint and preserves opaque cursors", async () => {
    const cursor = "signed.opaque+/=%2Ftail";
    const get = vi.fn(async (path: string) => {
      switch (path) {
        case "/api/v1/specs":
        case "/api/v1/artifacts":
        case "/api/v1/specs/{artifact_id}/graph":
        case "/api/v1/constraints":
        case "/api/v1/constraint-proposals":
        case "/api/v1/execution-profiles":
        case "/api/v1/diff":
        case "/api/v1/refs/{ref_name}/history":
          return response({ items: [], next_cursor: null });
        case "/api/v1/specs/{artifact_id}":
          return response(spec);
        case "/api/v1/artifacts/{artifact_id}":
          return response({ artifact: spec.artifact, payload: { title: "Aureus" } });
        case "/api/v1/schema-registry/{version}":
          return response({ schema_registry_version: "registry@4" });
        case "/api/v1/constraints/{artifact_id}":
          return response(constraint);
        case "/api/v1/workflow-subjects/{artifact_id}/approval-binding":
          return response({ approval_id: "approval:1" });
        case "/api/v1/approvals/{approval_id}":
          return response(approval, { ETag: '"approval:9"' });
        case "/api/v1/execution-profiles/{profile_id}/versions/{version}/constraint-validation-binding":
          return response({
            binding_schema_version: "constraint-validation-compiler-binding@1",
            compiler_profile: { profile_id: "builtin.constraint_compiler", version: 1 },
            differential_engines: [
              { engine_id: "clingo", version: 1 },
              { engine_id: "graph-reference", version: 1 },
            ],
            profile_payload_hash: "a".repeat(64),
            run_kind: { kind: "constraint_proposal.validate", version: 1 },
          });
        default:
          throw new Error(`Unexpected GET ${path}`);
      }
    });
    const api = createSpecWorkflowApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await api.listSpecs(cursor);
    await api.listArtifacts("source_raw", cursor);
    await api.getSpec("artifact:spec-1");
    await api.getArtifactPayload("artifact:spec-1");
    await api.listSpecGraph("artifact:spec-1", cursor);
    await api.getSchemaRegistry("registry@4");
    await api.getSnapshotDiff("artifact:base", "artifact:target", cursor);
    await api.listRefHistory("refs/constraints/live", cursor);
    await api.listConstraintSnapshots(cursor);
    await api.getConstraintSnapshot("artifact:constraint-1");
    await api.listConstraintProposals(cursor);
    await api.getApprovalBinding("artifact:proposal-1");
    const versionedApproval = await api.getApproval("approval:1");
    await api.listExecutionProfiles(cursor);
    const compilerBinding = await api.getConstraintValidationCompilerBinding(
      "builtin.constraint_compiler",
      1,
    );

    expect(versionedApproval).toEqual({ etag: '"approval:9"', value: approval });
    expect(get).toHaveBeenCalledWith("/api/v1/specs", {
      params: { query: { cursor } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/artifacts", {
      params: { query: { cursor, kind: "source_raw" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/specs/{artifact_id}/graph", {
      params: { path: { artifact_id: "artifact:spec-1" }, query: { cursor } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/diff", {
      params: { query: { base: "artifact:base", cursor, target: "artifact:target" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/refs/{ref_name}/history", {
      params: { path: { ref_name: "refs/constraints/live" }, query: { cursor } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/execution-profiles", {
      params: { query: { cursor } },
    });
    expect(get).toHaveBeenCalledWith(
      "/api/v1/execution-profiles/{profile_id}/versions/{version}/constraint-validation-binding",
      {
        params: {
          path: { profile_id: "builtin.constraint_compiler", version: 1 },
        },
      },
    );
    expect(compilerBinding.differential_engines).toEqual([
      { engine_id: "clingo", version: 1 },
      { engine_id: "graph-reference", version: 1 },
    ]);
  });

  it("turns a 410 page response into an explicit restart boundary without changing the cursor", async () => {
    const staleCursor = "stale.opaque+/=";
    const get = vi.fn(async () => ({
      error: {
        code: "cursor_expired",
        detail: "read snapshot expired",
        instance: "/api/v1/specs",
        request_id: "request:1",
        status: 410,
        title: "Cursor expired",
        type: "about:blank",
      },
      response: new Response(undefined, { status: 410 }),
    }));
    const api = createSpecWorkflowApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.listSpecs(staleCursor)).rejects.toMatchObject({
      name: "CursorExpiredError",
      staleCursor,
    });
  });

  it("requires the opaque ETag from the proposal GET for every versioned command", async () => {
    const get = vi.fn(async () => response(proposal, { ETag: '"proposal:opaque-7"' }));
    const post = vi.fn(async (path: string, _options: unknown) => {
      if (path.endsWith(":validate")) return response({ run_id: "run:validate" });
      if (path.endsWith(":submit-for-approval")) return response(approval);
      if (path.endsWith(":publish")) return response({ artifact_id: "artifact:constraint-2" });
      return response(proposal);
    });
    const api = createSpecWorkflowApi({ GET: get, POST: post } as unknown as GameForgeOpenApiClient);
    const current = await api.getConstraintProposal("artifact:proposal-1");
    const revision = { expected_workflow_revision: 7 } as HumanConstraintRevisionRequest;
    const validation = { expected_workflow_revision: 7 } as ConstraintValidationAdmissionRequest;
    const submission = { expected_workflow_revision: 7 } as SubmitForApprovalRequest;
    const publication = { expected_workflow_revision: 7 } as WorkflowApplyRequest;

    await api.reviseConstraint(current, revision, intent);
    await api.validateConstraint(current, validation, intent);
    await api.submitConstraintForApproval(current, submission, intent);
    await api.publishConstraint(current, publication, intent);

    expect(current).toEqual({ etag: '"proposal:opaque-7"', value: proposal });
    for (const path of [
      "/api/v1/constraint-proposals/{artifact_id}:revise",
      "/api/v1/constraint-proposals/{artifact_id}:validate",
      "/api/v1/constraint-proposals/{artifact_id}:submit-for-approval",
      "/api/v1/constraint-proposals/{artifact_id}:publish",
    ]) {
      expect(post).toHaveBeenCalledWith(
        path,
        expect.objectContaining({
          params: {
            header: {
              "Idempotency-Key": intent.idempotencyKey,
              "If-Match": '"proposal:opaque-7"',
              "X-CSRF-Token": "csrf:specs",
            },
            path: { artifact_id: "artifact:proposal-1" },
          },
        }),
      );
    }
  });

  it("fails closed before a versioned command when the proposal GET omits ETag", async () => {
    const get = vi.fn(async () => response(proposal));
    const post = vi.fn();
    const api = createSpecWorkflowApi({ GET: get, POST: post } as unknown as GameForgeOpenApiClient);

    await expect(api.getConstraintProposal("artifact:proposal-1")).rejects.toThrow(
      "The server response did not include the required ETag.",
    );
    expect(post).not.toHaveBeenCalled();
  });

  it("sends create and agent-proposal mutations without If-Match", async () => {
    const post = vi.fn(async (path: string, _options: unknown) => {
      if (path === "/api/v1/specs") return response(spec);
      if (path === "/api/v1/patches") return response({ artifact: { artifact_id: "patch:1" } });
      if (path === "/api/v1/constraint-proposals:propose") return response({ run_id: "run:1" });
      return response(proposal);
    });
    const api = createSpecWorkflowApi({ POST: post } as unknown as GameForgeOpenApiClient);

    await api.uploadSpec({} as HumanSpecUploadRequest, intent);
    await api.draftPatch({} as HumanPatchDraftRequest, intent);
    await api.draftConstraint({} as HumanConstraintDraftRequest, intent);
    await api.proposeConstraint({} as ConstraintProposeRequest, intent);

    expect(post).toHaveBeenCalledTimes(4);
    expect(post.mock.calls.map(([path]) => path)).toEqual([
      "/api/v1/specs",
      "/api/v1/patches",
      "/api/v1/constraint-proposals",
      "/api/v1/constraint-proposals:propose",
    ]);
    for (const [, options] of post.mock.calls) {
      expect(options).toMatchObject({
        params: {
          header: {
            "Idempotency-Key": intent.idempotencyKey,
            "X-CSRF-Token": "csrf:specs",
          },
        },
      });
      expect((options as { params: { header: Record<string, string> } }).params.header).not.toHaveProperty(
        "If-Match",
      );
    }
  });

  it("resolves an exact agent execution option with CSRF only", async () => {
    const resolved = { option_id: "option:1" };
    const post = vi.fn(async (_path: string, _options: unknown) => response(resolved));
    const api = createSpecWorkflowApi({ POST: post } as unknown as GameForgeOpenApiClient);
    const request: ExecutionOptionResolveRequest = {
      llm_execution_mode: "record",
      prospective_request: {
        authoring_goal_text: "Extract deterministic economy constraints.",
        base_constraint_snapshot_artifact_id: null,
        cassette_artifact_id: null,
        domain_scope: { domain_ids: ["domain:economy"] },
        dsl_grammar_version: "dsl@1",
        execution_version_plan: null,
        extraction_policy: { profile_id: "builtin.constraint_extraction", version: 4 },
        llm_execution_mode: "record",
        request_schema_version: "constraint-propose-request@1",
        source_artifact_ids: ["artifact:source:design"],
      },
      replay_source_run_id: null,
      request_schema_version: "execution-option-resolve-request@1",
      resource_operation_id: "propose_constraint_api_v1_constraint_proposals_propose_post",
      run_kind: { kind: "constraint_proposal.propose", version: 1 },
    };

    await expect(api.resolveExecutionOption(request)).resolves.toBe(resolved);
    expect(post).toHaveBeenCalledWith("/api/v1/execution-options:resolve", {
      body: request,
      params: { header: { "X-CSRF-Token": "csrf:specs" } },
    });
    const sent = post.mock.calls[0][1] as { body: ExecutionOptionResolveRequest };
    expect(sent.body.prospective_request).toMatchObject({
      cassette_artifact_id: null,
      execution_version_plan: null,
    });
  });
});
