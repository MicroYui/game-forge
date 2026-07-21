import type { FetchOptions } from "openapi-fetch";

import type { GameForgeOpenApiClient } from "../../api/client";
import { responseEtag, unwrapApiResponse } from "../../api/client";
import {
  headersForCsrfProtectedRequest,
  headersForIdempotentMutation,
  type MutationIntent,
} from "../../api/csrf";
import type { components, paths } from "../../api/generated/openapi";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";
import { cursorQuery, requireExplicitCursorRestart } from "../../api/pagination";
import { clearAuthorizedSessionState, gameForgeApi } from "../../api/runtime";
import { RunEventStream, type RunEventStreamState } from "../../api/sse";

export type ApprovalView = components["schemas"]["ApprovalViewV1"];
export type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
export type ConstraintPage = components["schemas"]["OpaquePageV1_ConstraintSnapshotViewV1_"];
export type ConstraintSnapshotView = components["schemas"]["ConstraintSnapshotViewV1"];
export type ExecutionOptionResolveRequest = components["schemas"]["ExecutionOptionResolveRequestV1"];
export type ExecutionOptionView = components["schemas"]["ExecutionOptionViewV1"];
export type ExecutionProfilePage = components["schemas"]["OpaquePageV1_ExecutionProfileViewV1_"];
export type ExecutionProfileReadView = components["schemas"]["ExecutionProfileViewV1"];
export type GenerationProposeRequest = NonNullable<
  FetchOptions<paths["/api/v1/generation:propose"]["post"]>["body"]
>;
export type PatchArtifactReadView = components["schemas"]["PatchArtifactReadViewV1"];
export type ProspectiveGenerationProposeRequest =
  components["schemas"]["ProspectiveGenerationProposeRequestV1"];
export type RunAccepted = components["schemas"]["RunAcceptedV1"];
export type RunView = components["schemas"]["RunViewV1"];
export type SnapshotDiffPage = components["schemas"]["SnapshotDiffHttpPageV1"];
export type SpecPage = components["schemas"]["OpaquePageV1_SpecViewV1_"];
export type SpecView = components["schemas"]["SpecViewV1"];
export type SubjectApprovalBindingView = components["schemas"]["SubjectApprovalBindingViewV1"];

export interface VersionedResource<T> {
  etag: string;
  value: T;
}

export interface GenerationEventStreamCallbacks {
  onEvent(event: RunEvent, cursor: string): void;
  onStateChange(state: RunEventStreamState): void;
}

export interface GenerationEventStreamHandle {
  close(): void;
  restart(): Promise<void>;
  start(): Promise<void>;
}

export interface GenerationApi {
  listSpecs(cursor: string | null): Promise<SpecPage>;
  getSpec(artifactId: string): Promise<SpecView>;
  listConstraints(cursor: string | null): Promise<ConstraintPage>;
  getConstraint(artifactId: string): Promise<ConstraintSnapshotView>;
  listExecutionProfiles(cursor: string | null): Promise<ExecutionProfilePage>;
  getExecutionProfile(profileId: string, version: number): Promise<ExecutionProfileReadView>;
  resolveExecutionOption(request: ExecutionOptionResolveRequest): Promise<ExecutionOptionView>;
  proposeGeneration(request: GenerationProposeRequest, intent: MutationIntent): Promise<RunAccepted>;
  getRun(runId: string): Promise<RunView>;
  createEventStream(
    callbacks: GenerationEventStreamCallbacks & { runId: string },
  ): GenerationEventStreamHandle;
  getArtifact(artifactId: string): Promise<ArtifactPayloadView>;
  getPatch(artifactId: string): Promise<VersionedResource<PatchArtifactReadView>>;
  getApprovalBinding(artifactId: string): Promise<SubjectApprovalBindingView>;
  getApproval(approvalId: string): Promise<VersionedResource<ApprovalView>>;
  getSnapshotDiff(
    baseSnapshotId: string,
    targetSnapshotId: string,
    cursor: string | null,
  ): Promise<SnapshotDiffPage>;
}

type ApiResponse<T> = {
  data?: T;
  error?: unknown;
  response: Response;
};

async function readCursorPage<T>(cursor: string | null, read: () => Promise<T>): Promise<T> {
  try {
    return await read();
  } catch (error) {
    throw requireExplicitCursorRestart(error, cursor);
  }
}

async function unwrapVersionedResponse<T>(result: ApiResponse<T>): Promise<VersionedResource<T>> {
  const value = await unwrapApiResponse<T>(result);
  const etag = responseEtag(result.response);
  if (etag === null) throw new Error("The server response did not include the required ETag.");
  return { etag, value };
}

export function createGenerationApi(client: GameForgeOpenApiClient = gameForgeApi.client): GenerationApi {
  return {
    createEventStream({ runId, onEvent, onStateChange }) {
      return new RunEventStream({
        runId,
        onEvent,
        onSessionBoundary: clearAuthorizedSessionState,
        onStateChange,
      });
    },

    async getApproval(approvalId) {
      return unwrapVersionedResponse<ApprovalView>(
        await client.GET("/api/v1/approvals/{approval_id}", {
          params: { path: { approval_id: approvalId } },
        }),
      );
    },

    async getApprovalBinding(artifactId) {
      return unwrapApiResponse<SubjectApprovalBindingView>(
        await client.GET("/api/v1/workflow-subjects/{artifact_id}/approval-binding", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    async getArtifact(artifactId) {
      return unwrapApiResponse<ArtifactPayloadView>(
        await client.GET("/api/v1/artifacts/{artifact_id}", {
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

    async getExecutionProfile(profileId, version) {
      return unwrapApiResponse<ExecutionProfileReadView>(
        await client.GET("/api/v1/execution-profiles/{profile_id}/versions/{version}", {
          params: { path: { profile_id: profileId, version } },
        }),
      );
    },

    async getPatch(artifactId) {
      return unwrapVersionedResponse<PatchArtifactReadView>(
        await client.GET("/api/v1/patches/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    async getRun(runId) {
      return unwrapApiResponse<RunView>(
        await client.GET("/api/v1/runs/{run_id}", {
          params: { path: { run_id: runId } },
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

    getSnapshotDiff(baseSnapshotId, targetSnapshotId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<SnapshotDiffPage>(
          await client.GET("/api/v1/diff", {
            params: {
              query: {
                base: baseSnapshotId,
                target: targetSnapshotId,
                ...cursorQuery(cursor),
              },
            },
          }),
        ),
      );
    },

    listConstraints(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ConstraintPage>(
          await client.GET("/api/v1/constraints", {
            params: { query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    listExecutionProfiles(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ExecutionProfilePage>(
          await client.GET("/api/v1/execution-profiles", {
            params: { query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    listSpecs(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<SpecPage>(
          await client.GET("/api/v1/specs", {
            params: { query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    async proposeGeneration(request, intent) {
      return unwrapApiResponse<RunAccepted>(
        await client.POST("/api/v1/generation:propose", {
          body: request,
          params: { header: headersForIdempotentMutation(intent) },
        }),
      );
    },

    async resolveExecutionOption(request) {
      return unwrapApiResponse<ExecutionOptionView>(
        await client.POST("/api/v1/execution-options:resolve", {
          // openapi-fetch Writable<T> evaluates NonNullable<null> as never, accidentally
          // matching its $Read marker and stripping these required null-only fields. The
          // generated wire component remains authoritative.
          body: request as unknown as NonNullable<
            FetchOptions<paths["/api/v1/execution-options:resolve"]["post"]>["body"]
          >,
          params: { header: headersForCsrfProtectedRequest() },
        }),
      );
    },
  };
}

export const generationApi = createGenerationApi();
