import { describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import type { SelectableModel, SelectableModelPage } from "./api";
import { createModelsApi } from "./api";

function model(overrides: Partial<SelectableModel> = {}): SelectableModel {
  return {
    context_limit: 1_050_000,
    display_name: "GPT-5.6 Sol",
    is_default: true,
    max_output_tokens: 128_000,
    model: "gpt-5.6-sol",
    model_catalog_digest: "a".repeat(64),
    model_catalog_version: 1,
    model_schema_version: "selectable-model@1",
    model_snapshot_id: "openai:sha256:" + "b".repeat(64),
    preview: false,
    routing_policy_digest: "c".repeat(64),
    routing_policy_version: 10_015,
    tier: "powerful",
    vendor: "OpenAI",
    ...overrides,
  };
}

describe("modelsApi", () => {
  it("reads what the deployment can start a run on", async () => {
    const page: SelectableModelPage = {
      items: [model(), model({ display_name: "Claude Opus 5", is_default: false, model: "claude-opus-5" })],
      page_schema_version: "selectable-model-page@1",
    };
    const client = {
      GET: vi.fn().mockResolvedValue({ data: page, response: Response.json(page) }),
    } as unknown as GameForgeOpenApiClient;

    const models = await createModelsApi(client).listSelectableModels();

    expect(client.GET).toHaveBeenCalledWith("/api/v1/models");
    expect(models.map((item) => item.model)).toEqual(["gpt-5.6-sol", "claude-opus-5"]);
    expect(models.filter((item) => item.is_default)).toHaveLength(1);
  });

  it("surfaces a deployment without a gateway instead of pretending it has no models", async () => {
    const problem = { detail: "selectable model reader is unavailable", status: 503 };
    const client = {
      GET: vi.fn().mockResolvedValue({
        error: problem,
        response: Response.json(problem, { status: 503 }),
      }),
    } as unknown as GameForgeOpenApiClient;

    await expect(createModelsApi(client).listSelectableModels()).rejects.toBeDefined();
  });
});
