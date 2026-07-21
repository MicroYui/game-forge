import type { GameForgeOpenApiClient } from "../../api/client";
import { unwrapApiResponse } from "../../api/client";
import type { components } from "../../api/generated/openapi";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";
import { cursorQuery, requireExplicitCursorRestart } from "../../api/pagination";
import { clearAuthorizedSessionState, gameForgeApi } from "../../api/runtime";
import { RunEventStream, type RunEventStreamState } from "../../api/sse";

export type RunView = components["schemas"]["RunViewV1"];
export type FindingRevision = components["schemas"]["FindingRevisionV1"];
export type RunCommandView = components["schemas"]["RunCommandViewV1"];
export type TraceSummary = components["schemas"]["TraceSummaryV1"];
export type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
export type FindingRevisionPage = components["schemas"]["OpaquePageV1_FindingRevisionV1_"];
export type RunCommandPage = components["schemas"]["OpaquePageV1_RunCommandViewV1_"];
export type TraceSummaryPage = components["schemas"]["TraceSummaryPageV1"];

export interface RunDetailSnapshot {
  run: RunView;
  findingsPage: FindingRevisionPage;
  commandsPage: RunCommandPage;
  tracesPage: TraceSummaryPage;
  resultManifest: ArtifactPayloadView | null;
  failureManifest: ArtifactPayloadView | null;
}

export interface RunEventStreamCallbacks {
  onEvent(event: RunEvent, cursor: string): void;
  onStateChange(state: RunEventStreamState): void;
}

export interface RunEventStreamHandle {
  start(): Promise<void>;
  restart(): Promise<void>;
  close(): void;
}

export interface RunDetailApi {
  load(runId: string): Promise<RunDetailSnapshot>;
  loadFindingsPage(runId: string, cursor: string | null): Promise<FindingRevisionPage>;
  loadCommandsPage(runId: string, cursor: string | null): Promise<RunCommandPage>;
  loadTracesPage(runId: string, cursor: string | null): Promise<TraceSummaryPage>;
  createEventStream(callbacks: RunEventStreamCallbacks & { runId: string }): RunEventStreamHandle;
}

async function readCursorPage<T>(cursor: string | null, read: () => Promise<T>): Promise<T> {
  try {
    return await read();
  } catch (error) {
    throw requireExplicitCursorRestart(error, cursor);
  }
}

export function createRunDetailApi(client: GameForgeOpenApiClient = gameForgeApi.client): RunDetailApi {
  async function loadArtifact(artifactId: string): Promise<ArtifactPayloadView> {
    return unwrapApiResponse<ArtifactPayloadView>(
      await client.GET("/api/v1/artifacts/{artifact_id}", {
        params: { path: { artifact_id: artifactId } },
      }),
    );
  }

  const api: RunDetailApi = {
    createEventStream({ runId, onEvent, onStateChange }) {
      return new RunEventStream({
        runId,
        onEvent,
        onSessionBoundary: clearAuthorizedSessionState,
        onStateChange,
      });
    },

    loadFindingsPage(runId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<FindingRevisionPage>(
          await client.GET("/api/v1/runs/{run_id}/findings", {
            params: { path: { run_id: runId }, query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    loadCommandsPage(runId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<RunCommandPage>(
          await client.GET("/api/v1/runs/{run_id}/commands", {
            params: { path: { run_id: runId }, query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    loadTracesPage(runId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<TraceSummaryPage>(
          await client.GET("/api/v1/runs/{run_id}/traces", {
            params: { path: { run_id: runId }, query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    async load(runId) {
      const run = await unwrapApiResponse<RunView>(
        await client.GET("/api/v1/runs/{run_id}", {
          params: { path: { run_id: runId } },
        }),
      );
      const [findingsPage, commandsPage, tracesPage, resultManifest, failureManifest] = await Promise.all([
        api.loadFindingsPage(runId, null),
        api.loadCommandsPage(runId, null),
        api.loadTracesPage(runId, null),
        run.result_artifact_id ? loadArtifact(run.result_artifact_id) : Promise.resolve(null),
        run.failure_artifact_id ? loadArtifact(run.failure_artifact_id) : Promise.resolve(null),
      ]);

      return {
        commandsPage,
        failureManifest,
        findingsPage,
        resultManifest,
        run,
        tracesPage,
      };
    },
  };

  return api;
}

export const runDetailApi = createRunDetailApi();
