import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import { storeCsrfToken } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import type {
  ArtifactPayloadView,
  ConstraintSnapshotView,
  ExecutionOptionResolveRequest,
  FindingRevision,
  ReviewArtifactView,
  RunView,
  RunSubmissionRequest,
  SpecView,
} from "./api";
import { createReviewApi } from "./api";

function response<T>(data: T) {
  return { data, response: Response.json(data) };
}

const review = {
  artifact: { artifact_id: "artifact:review:7" },
} as unknown as ReviewArtifactView;

const producerBinding = {
  review_artifact_id: "artifact:review:7",
  run_id: "run:review:3",
} as unknown as import("./api").ReviewProducerBindingView;

const finding = {
  finding_id: "finding:quest:dead-end",
  revision: 7,
} as unknown as FindingRevision;

const spec = { artifact: { artifact_id: "artifact:preview:7" } } as unknown as SpecView;
const constraint = {
  artifact: { artifact_id: "artifact:constraint:7" },
} as unknown as ConstraintSnapshotView;
const failedReplaySource = {
  failure_artifact_id: "artifact:failure:review-source",
  result_artifact_id: null,
  run_id: "run:review:failed-source",
  status: "failed",
  terminal_cassette_artifact_id: "artifact:cassette:review-failed",
} as RunView;
const replayFailureManifest = {
  artifact: {
    artifact_id: "artifact:failure:review-source",
    created_at: "2026-07-23T03:47:50Z",
    kind: "run_failure",
    payload_schema_id: "run-failure@1",
  },
  payload: {
    cause_code: "review_source_failed",
    failure_schema_version: "run-failure@1",
    run_id: failedReplaySource.run_id,
    run_kind: { kind: "review.run", version: 1 },
  },
} as unknown as ArtifactPayloadView;
const successWithoutCassette = {
  run_id: "run:review:no-cassette",
  status: "succeeded",
  terminal_cassette_artifact_id: null,
} as RunView;

const prospectiveRequest: components["schemas"]["ProspectiveGenericAgentRunRequestV1"] = {
  cassette_artifact_id: null,
  execution_version_plan: null,
  llm_execution_mode: "replay",
  params: {
    checker_profiles: [],
    constraint_snapshot_artifact_id: "artifact:constraint:7",
    llm_triage_policy: { profile_id: "builtin.llm_triage", version: 1 },
    review_profile: { profile_id: "builtin.review", version: 1 },
    schema_version: "review-run@1",
    selection: { entity_ids: [], mode: "full", relation_ids: [] },
    simulation_profiles: [],
    snapshot_artifact_id: "artifact:preview:7",
  },
  request_schema_version: "run-submission-request@1",
  seed: 13,
};

const resolveRequest: ExecutionOptionResolveRequest = {
  llm_execution_mode: "replay",
  prospective_request: prospectiveRequest,
  replay_source_run_id: "run:review:source",
  request_schema_version: "execution-option-resolve-request@1",
  resource_operation_id: "submit_run_api_v1_runs_post",
  run_kind: { kind: "review.run", version: 1 },
};

const runRequest: RunSubmissionRequest = {
  ...prospectiveRequest,
  cassette_artifact_id: "artifact:cassette:review",
  execution_version_plan: {
    agent_graph_version: "review@1",
    model_catalog_digest: "a".repeat(64),
    model_catalog_version: 1,
    nodes: [],
    plan_digest: "b".repeat(64),
    plan_schema_version: "execution-version-plan@1",
    routing_policy_digest: "c".repeat(64),
    routing_policy_version: 1,
  },
};

const intent = Object.freeze({ idempotencyKey: "11111111-1111-4111-8111-111111111111" });

describe("Review API", () => {
  beforeEach(() => {
    sessionStorage.clear();
    storeCsrfToken("csrf:review");
  });

  it("uses exact typed Review, lineage, Run Finding, and Finding revision reads", async () => {
    const cursor = "opaque.review+/=%2Ftail";
    const get = vi.fn(async (path: string) => {
      switch (path) {
        case "/api/v1/reviews":
        case "/api/v1/artifacts/{artifact_id}/lineage":
        case "/api/v1/runs/{run_id}/finding-links":
          return response({ items: [], next_cursor: null });
        case "/api/v1/runs":
          return response({ items: [failedReplaySource, successWithoutCassette], next_cursor: null });
        case "/api/v1/artifacts/{artifact_id}":
          return response(replayFailureManifest);
        case "/api/v1/reviews/{artifact_id}":
          return response(review);
        case "/api/v1/reviews/{artifact_id}/producer-binding":
          return response(producerBinding);
        case "/api/v1/specs/{artifact_id}":
          return response(spec);
        case "/api/v1/constraints/{artifact_id}":
          return response(constraint);
        case "/api/v1/findings/{finding_id}/revisions/{revision}":
          return response(finding);
        default:
          throw new Error(`Unexpected GET ${path}`);
      }
    });
    const api = createReviewApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await api.listReviews(cursor);
    await api.getReview("artifact:review:7");
    await api.getReviewProducerBinding("artifact:review:7", "run:review:3");
    await api.listLineage("artifact:review:7", cursor);
    await api.listRunFindingLinks("run:review:3", cursor);
    const replaySources = await api.listReplaySourceRuns(cursor);
    await api.getFinding("finding:quest:dead-end", 7);
    await api.getSpec("artifact:preview:7");
    await api.getConstraint("artifact:constraint:7");

    expect(replaySources.items).toEqual([
      {
        ...failedReplaySource,
        completedAt: "2026-07-23T03:47:50Z",
        outcomeCode: "review_source_failed",
        runKind: { kind: "review.run", version: 1 },
      },
    ]);

    expect(get).toHaveBeenCalledWith("/api/v1/reviews", {
      params: { query: { cursor } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/reviews/{artifact_id}", {
      params: { path: { artifact_id: "artifact:review:7" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/reviews/{artifact_id}/producer-binding", {
      params: {
        path: { artifact_id: "artifact:review:7" },
        query: { run_id: "run:review:3" },
      },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/artifacts/{artifact_id}/lineage", {
      params: {
        path: { artifact_id: "artifact:review:7" },
        query: { cursor },
      },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/runs/{run_id}/finding-links", {
      params: { path: { run_id: "run:review:3" }, query: { cursor } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/runs", {
      params: { query: { cursor } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/findings/{finding_id}/revisions/{revision}", {
      params: { path: { finding_id: "finding:quest:dead-end", revision: 7 } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/specs/{artifact_id}", {
      params: { path: { artifact_id: "artifact:preview:7" } },
    });
    expect(get).toHaveBeenCalledWith("/api/v1/constraints/{artifact_id}", {
      params: { path: { artifact_id: "artifact:constraint:7" } },
    });
  });

  it("turns every paged 410 into an explicit restart boundary without changing the cursor", async () => {
    const staleCursor = "stale.review+/=";
    const get = vi.fn(async () => ({
      error: {
        code: "cursor_expired",
        detail: "read snapshot expired",
        instance: "/api/v1/reviews",
        request_id: "request:review-cursor",
        status: 410,
        title: "Cursor expired",
        type: "about:blank",
      },
      response: new Response(undefined, { status: 410 }),
    }));
    const api = createReviewApi({ GET: get } as unknown as GameForgeOpenApiClient);

    for (const read of [
      () => api.listReviews(staleCursor),
      () => api.listReviewProfiles(staleCursor),
      () => api.listLineage("artifact:review:7", staleCursor),
      () => api.listRunFindingLinks("run:review:3", staleCursor),
    ]) {
      await expect(read()).rejects.toMatchObject({
        name: "CursorExpiredError",
        staleCursor,
      });
    }

    expect(get).toHaveBeenCalledTimes(4);
  });

  it("uses the active review-run profile catalog and typed resolver and mutation transports", async () => {
    const option = { option_id: "option:review:1" };
    const accepted = { run_id: "run:review:accepted" };
    const get = vi.fn(async () => response({ items: [], next_cursor: null }));
    const post = vi.fn(async (path: string) =>
      path === "/api/v1/execution-options:resolve" ? response(option) : response(accepted),
    );
    const api = createReviewApi({ GET: get, POST: post } as unknown as GameForgeOpenApiClient);

    await api.listReviewProfiles("opaque.review-profiles");
    await expect(api.resolveExecutionOption(resolveRequest)).resolves.toBe(option);
    await expect(api.submitRun(runRequest, intent)).resolves.toBe(accepted);

    expect(get).toHaveBeenCalledWith("/api/v1/execution-profiles", {
      params: {
        query: {
          cursor: "opaque.review-profiles",
          run_kind: "review.run",
          run_kind_version: 1,
          status: "active",
        },
      },
    });
    expect(post).toHaveBeenCalledWith("/api/v1/execution-options:resolve", {
      body: resolveRequest,
      params: { header: { "X-CSRF-Token": "csrf:review" } },
    });
    expect(post).toHaveBeenCalledWith("/api/v1/runs", {
      body: runRequest,
      params: {
        header: {
          "Idempotency-Key": intent.idempotencyKey,
          "X-CSRF-Token": "csrf:review",
        },
      },
    });
  });
});
