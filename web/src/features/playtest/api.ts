import type { FetchOptions } from "openapi-fetch";

import type { GameForgeOpenApiClient } from "../../api/client";
import { unwrapApiResponse } from "../../api/client";
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
import { projectReplaySourcePage, type ReplaySourceRunPage } from "../runs/replaySources";

export type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
export type ArtifactSummaryPage = components["schemas"]["OpaquePageV1_ArtifactSummaryV1_"];
export type ConstraintSnapshotView = components["schemas"]["ConstraintSnapshotViewV1"];
export type ExecutionOptionResolveRequest = components["schemas"]["ExecutionOptionResolveRequestV1"];
export type ExecutionOptionView = components["schemas"]["ExecutionOptionViewV1"];
export type ExecutionProfilePage = components["schemas"]["OpaquePageV1_ExecutionProfileViewV1_"];
export type PlaytestRunRequest = NonNullable<FetchOptions<paths["/api/v1/playtest:run"]["post"]>["body"]>;
export type ProspectivePlaytestRunRequest = components["schemas"]["ProspectivePlaytestRunRequestV1"];
export type RunAccepted = components["schemas"]["RunAcceptedV1"];
export type RunCommandPage = components["schemas"]["OpaquePageV1_RunCommandViewV1_"];
export type RunFindingLinkPage = components["schemas"]["OpaquePageV1_RunFindingLinkViewV1_"];
export type RunPage = components["schemas"]["OpaquePageV1_RunViewV1_"];
export type RunView = components["schemas"]["RunViewV1"];
export type SpecView = components["schemas"]["SpecViewV1"];
export type TaskSuiteArtifactView = components["schemas"]["TaskSuiteArtifactViewV1"];
export type TaskSuiteDerivationBindingView = components["schemas"]["TaskSuiteDerivationBindingViewV1"];
export type TaskSuiteDeriveRequest = NonNullable<
  FetchOptions<paths["/api/v1/task-suites:derive"]["post"]>["body"]
>;
export type TaskSuitePage = components["schemas"]["OpaquePageV1_TaskSuiteArtifactViewV1_"];

type TaskSuiteQuery = NonNullable<paths["/api/v1/task-suites"]["get"]["parameters"]["query"]>;
type ExecutionProfileQuery = NonNullable<paths["/api/v1/execution-profiles"]["get"]["parameters"]["query"]>;

export type TaskSuiteListFilters = Omit<TaskSuiteQuery, "cursor">;
export type ExecutionProfileListFilters = Omit<ExecutionProfileQuery, "cursor">;

export interface PlaytestEventStreamCallbacks {
  onEvent(event: RunEvent, cursor: string): void;
  onStateChange(state: RunEventStreamState): void;
}

export interface PlaytestEventStreamHandle {
  close(): void;
  restart(): Promise<void>;
  start(): Promise<void>;
}

export interface PlaytestApi {
  listConfigExports(cursor: string | null): Promise<ArtifactSummaryPage>;
  listTaskSuites(filters: TaskSuiteListFilters, cursor: string | null): Promise<TaskSuitePage>;
  getTaskSuite(artifactId: string): Promise<TaskSuiteArtifactView>;
  getTaskSuiteDerivationBinding(profileId: string, version: number): Promise<TaskSuiteDerivationBindingView>;
  listExecutionProfiles(
    filters: ExecutionProfileListFilters,
    cursor: string | null,
  ): Promise<ExecutionProfilePage>;
  getSpec(artifactId: string): Promise<SpecView>;
  getConstraint(artifactId: string): Promise<ConstraintSnapshotView>;
  getArtifact(artifactId: string): Promise<ArtifactPayloadView>;
  deriveTaskSuite(request: TaskSuiteDeriveRequest, intent: MutationIntent): Promise<RunAccepted>;
  resolveExecutionOption(request: ExecutionOptionResolveRequest): Promise<ExecutionOptionView>;
  runPlaytest(request: PlaytestRunRequest, intent: MutationIntent): Promise<RunAccepted>;
  getRun(runId: string): Promise<RunView>;
  listReplaySourceRuns(cursor: string | null): Promise<ReplaySourceRunPage>;
  getPlaytestResult(runId: string): Promise<ArtifactPayloadView>;
  listRunFindingLinks(runId: string, cursor: string | null): Promise<RunFindingLinkPage>;
  listRunCommands(runId: string, cursor: string | null): Promise<RunCommandPage>;
  createEventStream(callbacks: PlaytestEventStreamCallbacks & { runId: string }): PlaytestEventStreamHandle;
}

async function readCursorPage<T>(cursor: string | null, read: () => Promise<T>): Promise<T> {
  try {
    return await read();
  } catch (error) {
    throw requireExplicitCursorRestart(error, cursor);
  }
}

export function createPlaytestApi(client: GameForgeOpenApiClient = gameForgeApi.client): PlaytestApi {
  return {
    createEventStream({ runId, onEvent, onStateChange }) {
      return new RunEventStream({
        runId,
        onEvent,
        onSessionBoundary: clearAuthorizedSessionState,
        onStateChange,
      });
    },

    async deriveTaskSuite(request, intent) {
      return unwrapApiResponse<RunAccepted>(
        await client.POST("/api/v1/task-suites:derive", {
          body: request,
          params: { header: headersForIdempotentMutation(intent) },
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

    listConfigExports(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ArtifactSummaryPage>(
          await client.GET("/api/v1/artifacts", {
            params: { query: { ...cursorQuery(cursor), kind: "config_export", limit: 100 } },
          }),
        ),
      );
    },

    async getConstraint(artifactId) {
      return unwrapApiResponse<ConstraintSnapshotView>(
        await client.GET("/api/v1/constraints/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    async getPlaytestResult(runId) {
      return unwrapApiResponse<ArtifactPayloadView>(
        await client.GET("/api/v1/playtest/{run_id}/result", {
          params: { path: { run_id: runId } },
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

    listReplaySourceRuns(cursor) {
      return readCursorPage(cursor, async () => {
        const page = await unwrapApiResponse<RunPage>(
          await client.GET("/api/v1/runs", {
            params: { query: { ...cursorQuery(cursor) } },
          }),
        );
        return projectReplaySourcePage(page, { kind: "playtest.run", version: 1 }, async (artifactId) =>
          unwrapApiResponse<ArtifactPayloadView>(
            await client.GET("/api/v1/artifacts/{artifact_id}", {
              params: { path: { artifact_id: artifactId } },
            }),
          ),
        );
      });
    },

    async getSpec(artifactId) {
      return unwrapApiResponse<SpecView>(
        await client.GET("/api/v1/specs/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    async getTaskSuite(artifactId) {
      return unwrapApiResponse<TaskSuiteArtifactView>(
        await client.GET("/api/v1/task-suites/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    async getTaskSuiteDerivationBinding(profileId, version) {
      return unwrapApiResponse<TaskSuiteDerivationBindingView>(
        await client.GET(
          "/api/v1/execution-profiles/{profile_id}/versions/{version}/task-suite-derivation-binding",
          { params: { path: { profile_id: profileId, version } } },
        ),
      );
    },

    listExecutionProfiles(filters, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ExecutionProfilePage>(
          await client.GET("/api/v1/execution-profiles", {
            params: { query: { ...filters, ...cursorQuery(cursor) } },
          }),
        ),
      );
    },

    listRunCommands(runId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<RunCommandPage>(
          await client.GET("/api/v1/runs/{run_id}/commands", {
            params: { path: { run_id: runId }, query: cursorQuery(cursor) },
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

    listTaskSuites(filters, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<TaskSuitePage>(
          await client.GET("/api/v1/task-suites", {
            params: { query: { ...filters, ...cursorQuery(cursor) } },
          }),
        ),
      );
    },

    async resolveExecutionOption(request) {
      return unwrapApiResponse<ExecutionOptionView>(
        await client.POST("/api/v1/execution-options:resolve", {
          // openapi-fetch Writable<T> treats required null-only fields as its
          // internal read marker. The generated component remains the wire authority.
          body: request as unknown as NonNullable<
            FetchOptions<paths["/api/v1/execution-options:resolve"]["post"]>["body"]
          >,
          params: { header: headersForCsrfProtectedRequest() },
        }),
      );
    },

    async runPlaytest(request, intent) {
      return unwrapApiResponse<RunAccepted>(
        await client.POST("/api/v1/playtest:run", {
          body: request,
          params: { header: headersForIdempotentMutation(intent) },
        }),
      );
    },
  };
}

export const playtestApi = createPlaytestApi();
