import type { GameForgeOpenApiClient } from "../../api/client";
import { responseEtag, unwrapApiResponse } from "../../api/client";
import type { components } from "../../api/generated/openapi";
import { gameForgeApi } from "../../api/runtime";

export type BenchReportDto = components["schemas"]["BenchReport"];

export interface BenchReportRead {
  report: BenchReportDto;
  etag: string;
  artifactId: string | null;
}

export interface EvalApi {
  getBenchReport(): Promise<BenchReportRead>;
}

export function createEvalApi(client: GameForgeOpenApiClient = gameForgeApi.client): EvalApi {
  return {
    async getBenchReport() {
      const result = await client.GET("/api/v1/bench/report");
      const report = await unwrapApiResponse<BenchReportDto>(result);
      const etag = responseEtag(result.response);
      if (etag === null) {
        throw new Error("The server response did not include the required ETag.");
      }
      return {
        report,
        etag,
        artifactId: result.response.headers.get("X-Artifact-ID"),
      };
    },
  };
}

export const evalApi = createEvalApi();
