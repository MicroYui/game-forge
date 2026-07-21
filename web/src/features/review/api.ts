import type { FetchOptions } from "openapi-fetch";

import type { GameForgeOpenApiClient } from "../../api/client";
import { unwrapApiResponse } from "../../api/client";
import {
  headersForCsrfProtectedRequest,
  headersForIdempotentMutation,
  type MutationIntent,
} from "../../api/csrf";
import type { components, paths } from "../../api/generated/openapi";
import { cursorQuery, requireExplicitCursorRestart } from "../../api/pagination";
import { gameForgeApi } from "../../api/runtime";

export type ConstraintSnapshotView = components["schemas"]["ConstraintSnapshotViewV1"];
export type FindingRevision = components["schemas"]["FindingRevisionV1"];
export type ExecutionOptionResolveRequest = components["schemas"]["ExecutionOptionResolveRequestV1"];
export type ExecutionOptionView = components["schemas"]["ExecutionOptionViewV1"];
export type ExecutionProfilePage = components["schemas"]["OpaquePageV1_ExecutionProfileViewV1_"];
export type LineagePage = components["schemas"]["OpaquePageV1_LineageEntryV1_"];
export type ProspectiveReviewRunRequest = components["schemas"]["ProspectiveGenericAgentRunRequestV1"];
export type ReviewArtifactView = components["schemas"]["ReviewArtifactViewV1"];
export type ReviewPage = components["schemas"]["OpaquePageV1_ReviewArtifactViewV1_"];
export type ReviewProducerBindingView = components["schemas"]["ReviewProducerBindingViewV1"];
export type RunFindingLinkView = components["schemas"]["RunFindingLinkViewV1"];
export type RunFindingLinkPage = components["schemas"]["OpaquePageV1_RunFindingLinkViewV1_"];
export type RunAccepted = components["schemas"]["RunAcceptedV1"];
export type RunSubmissionRequest = NonNullable<FetchOptions<paths["/api/v1/runs"]["post"]>["body"]>;
export type SpecView = components["schemas"]["SpecViewV1"];

export interface ReviewApi {
  listReviews(cursor: string | null): Promise<ReviewPage>;
  listReviewProfiles(cursor: string | null): Promise<ExecutionProfilePage>;
  resolveExecutionOption(request: ExecutionOptionResolveRequest): Promise<ExecutionOptionView>;
  submitRun(request: RunSubmissionRequest, intent: MutationIntent): Promise<RunAccepted>;
  getReview(artifactId: string): Promise<ReviewArtifactView>;
  getReviewProducerBinding(artifactId: string, runId: string): Promise<ReviewProducerBindingView>;
  listLineage(artifactId: string, cursor: string | null): Promise<LineagePage>;
  listRunFindingLinks(runId: string, cursor: string | null): Promise<RunFindingLinkPage>;
  getFinding(findingId: string, revision: number): Promise<FindingRevision>;
  getSpec(artifactId: string): Promise<SpecView>;
  getConstraint(artifactId: string): Promise<ConstraintSnapshotView>;
}

async function readCursorPage<T>(cursor: string | null, read: () => Promise<T>): Promise<T> {
  try {
    return await read();
  } catch (error) {
    throw requireExplicitCursorRestart(error, cursor);
  }
}

export function createReviewApi(client: GameForgeOpenApiClient = gameForgeApi.client): ReviewApi {
  return {
    listReviews(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ReviewPage>(
          await client.GET("/api/v1/reviews", {
            params: { query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    listReviewProfiles(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ExecutionProfilePage>(
          await client.GET("/api/v1/execution-profiles", {
            params: {
              query: {
                ...cursorQuery(cursor),
                run_kind: "review.run",
                run_kind_version: 1,
                status: "active",
              },
            },
          }),
        ),
      );
    },

    async resolveExecutionOption(request) {
      return unwrapApiResponse<ExecutionOptionView>(
        await client.POST("/api/v1/execution-options:resolve", {
          // openapi-fetch Writable<T> strips required null-only fields as its
          // internal read marker. The generated component remains authoritative.
          body: request as unknown as NonNullable<
            FetchOptions<paths["/api/v1/execution-options:resolve"]["post"]>["body"]
          >,
          params: { header: headersForCsrfProtectedRequest() },
        }),
      );
    },

    async submitRun(request, intent) {
      return unwrapApiResponse<RunAccepted>(
        await client.POST("/api/v1/runs", {
          body: request,
          params: { header: headersForIdempotentMutation(intent) },
        }),
      );
    },

    async getReview(artifactId) {
      return unwrapApiResponse<ReviewArtifactView>(
        await client.GET("/api/v1/reviews/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    async getReviewProducerBinding(artifactId, runId) {
      return unwrapApiResponse<ReviewProducerBindingView>(
        await client.GET("/api/v1/reviews/{artifact_id}/producer-binding", {
          params: {
            path: { artifact_id: artifactId },
            query: { run_id: runId },
          },
        }),
      );
    },

    listLineage(artifactId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<LineagePage>(
          await client.GET("/api/v1/artifacts/{artifact_id}/lineage", {
            params: {
              path: { artifact_id: artifactId },
              query: cursorQuery(cursor),
            },
          }),
        ),
      );
    },

    listRunFindingLinks(runId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<RunFindingLinkPage>(
          await client.GET("/api/v1/runs/{run_id}/finding-links", {
            params: { path: { run_id: runId }, query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    async getFinding(findingId, revision) {
      return unwrapApiResponse<FindingRevision>(
        await client.GET("/api/v1/findings/{finding_id}/revisions/{revision}", {
          params: { path: { finding_id: findingId, revision } },
        }),
      );
    },

    async getSpec(artifactId) {
      return unwrapApiResponse<SpecView>(
        await client.GET("/api/v1/specs/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    async getConstraint(artifactId) {
      return unwrapApiResponse<ConstraintSnapshotView>(
        await client.GET("/api/v1/constraints/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },
  };
}

export const reviewApi = createReviewApi();
