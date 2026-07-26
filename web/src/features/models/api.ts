import type { GameForgeOpenApiClient } from "../../api/client";
import { unwrapApiResponse } from "../../api/client";
import type { components } from "../../api/generated/openapi";
import { gameForgeApi } from "../../api/runtime";

export type SelectableModel = components["schemas"]["SelectableModelV1"];
export type SelectableModelPage = components["schemas"]["SelectableModelPageV1"];

export interface ModelsApi {
  /** The models this deployment can start a run on, read live when a picker opens. */
  listSelectableModels(): Promise<SelectableModel[]>;
}

export function createModelsApi(client: GameForgeOpenApiClient = gameForgeApi.client): ModelsApi {
  return {
    async listSelectableModels() {
      const page = await unwrapApiResponse<SelectableModelPage>(await client.GET("/api/v1/models"));
      return [...page.items];
    },
  };
}

export const modelsApi: ModelsApi = createModelsApi();
