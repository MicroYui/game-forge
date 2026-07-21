import type { GameForgeOpenApiClient } from "../../api/client";
import { unwrapApiResponse } from "../../api/client";
import type { components } from "../../api/generated/openapi";
import { cursorQuery, requireExplicitCursorRestart } from "../../api/pagination";
import { gameForgeApi } from "../../api/runtime";

export type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
export type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
export type LineagePage = components["schemas"]["OpaquePageV1_LineageEntryV1_"];

export interface ArtifactDetailSnapshot {
  artifact: ArtifactSummary;
  lineagePage: LineagePage;
}

export interface ArtifactDetailApi {
  load(artifactId: string): Promise<ArtifactDetailSnapshot>;
  loadLineagePage(artifactId: string, cursor: string | null): Promise<LineagePage>;
}

export function createArtifactDetailApi(
  client: GameForgeOpenApiClient = gameForgeApi.client,
): ArtifactDetailApi {
  const api: ArtifactDetailApi = {
    async load(artifactId) {
      const [artifactView, lineagePage] = await Promise.all([
        unwrapApiResponse<ArtifactPayloadView>(
          await client.GET("/api/v1/artifacts/{artifact_id}", {
            params: { path: { artifact_id: artifactId } },
          }),
        ),
        api.loadLineagePage(artifactId, null),
      ]);
      return { artifact: artifactView.artifact, lineagePage };
    },

    async loadLineagePage(artifactId, cursor) {
      try {
        return await unwrapApiResponse<LineagePage>(
          await client.GET("/api/v1/artifacts/{artifact_id}/lineage", {
            params: { path: { artifact_id: artifactId }, query: cursorQuery(cursor) },
          }),
        );
      } catch (error) {
        throw requireExplicitCursorRestart(error, cursor);
      }
    },
  };
  return api;
}

export const artifactDetailApi = createArtifactDetailApi();
