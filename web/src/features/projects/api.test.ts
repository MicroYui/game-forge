import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import { storeCsrfToken } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import { createProjectsApi, sourceFormatForFile } from "./api";

type Project = components["schemas"]["GameProjectV1"];
type Material = components["schemas"]["ProjectMaterialV1"];
type Extraction = components["schemas"]["ProjectExtractionV1"];

function response<T>(data: T, headers?: HeadersInit) {
  return { data, response: Response.json(data, { headers }) };
}

const project = {
  project_id: "project:sky-harbor",
  revision: 3,
} as Project;
const material = { material_id: "material:world", revision: 1 } as Material;
const extraction = { extraction_id: "extraction:world", revision: 2 } as Extraction;

describe("projects API", () => {
  beforeEach(() => {
    sessionStorage.clear();
    storeCsrfToken("csrf:projects");
  });

  it("uses generated project paths and exact mutation headers", async () => {
    const get = vi.fn(async (path: string) => {
      if (path === "/api/v1/projects") return response({ items: [project] });
      if (path === "/api/v1/projects/{project_id}") return response(project, { ETag: '"project:3"' });
      if (path === "/api/v1/projects/{project_id}/materials") return response({ items: [material] });
      if (path === "/api/v1/projects/{project_id}/extractions") {
        return response({ items: [extraction] });
      }
      if (path === "/api/v1/projects/{project_id}/extractions/{extraction_id}") {
        return response(extraction, { ETag: '"extraction:2"' });
      }
      if (path === "/api/v1/artifacts/{artifact_id}") {
        return response({ artifact: { artifact_id: "artifact:preview" }, payload: {} });
      }
      throw new Error(`Unexpected GET ${path}`);
    });
    const post = vi.fn(async (path: string) => {
      if (path === "/api/v1/projects") return response(project, { ETag: '"project:1"' });
      if (path.endsWith("/materials:text") || path.endsWith("/materials:upload")) return response(material);
      if (path.endsWith("/extractions")) return response(extraction);
      if (path.endsWith("/extractions/{extraction_id}:discard")) return response(extraction);
      if (path.endsWith("/content-drafts")) {
        return response({ artifact: { artifact_id: "artifact:patch" } }, { "X-Project-Revision": "4" });
      }
      throw new Error(`Unexpected POST ${path}`);
    });
    const api = createProjectsApi({ GET: get, POST: post } as unknown as GameForgeOpenApiClient);
    const intent = { idempotencyKey: "intent:project" };

    expect((await api.getProject(project.project_id)).etag).toBe('"project:3"');
    await api.listProjects();
    await api.listMaterials(project.project_id);
    await api.listExtractions(project.project_id);
    await api.getExtraction(project.project_id, extraction.extraction_id);
    await api.getArtifact("artifact:preview");
    await api.createProject(
      {
        description: "浮空城经营",
        display_name: "天空港",
        domain_scope: { domain_ids: ["builtin"] },
        genre: "RPG",
        project_key: "sky-harbor",
        request_schema_version: "project-create-request@1",
      },
      intent,
    );
    await api.addTextMaterial(
      project.project_id,
      {
        display_name: "世界观",
        request_schema_version: "project-material-text-request@1",
        source_format: "plain_text",
        text: "一座会移动的城市。",
      },
      intent,
    );
    await api.startExtraction(
      project.project_id,
      {
        candidate_export_profiles: [],
        cassette_artifact_id: null,
        execution_version_plan: null,
        generation_policy: null,
        llm_execution_mode: "record",
        material_ids: [material.material_id],
        planning_scope: "auto",
        objective_goal_text: "提取实体和关系",
        request_schema_version: "project-extraction-create-request@1",
      },
      intent,
    );
    await api.discardExtraction(
      project.project_id,
      extraction.extraction_id,
      {
        expected_revision: extraction.revision,
        reason: "这版方向不合适，保留材料后重做。",
        request_schema_version: "project-extraction-discard-request@1",
      },
      intent,
      '"extraction:2"',
    );
    const draft = await api.createContentDraft(
      project.project_id,
      {
        candidate_export_profiles: [],
        entities: [],
        expected_source_extraction_revision: extraction.revision,
        expected_project_revision: project.revision,
        rationale: "确认首版",
        relations: [],
        request_schema_version: "project-graph-draft-request@1",
        side_effect_risk: "low",
        source_extraction_id: extraction.extraction_id,
      },
      intent,
      '"project:3"',
    );

    expect(draft.projectRevision).toBe(4);
    expect(post).toHaveBeenCalledWith(
      "/api/v1/projects/{project_id}/extractions/{extraction_id}:discard",
      expect.objectContaining({
        params: {
          header: {
            "Idempotency-Key": "intent:project",
            "If-Match": '"extraction:2"',
            "X-CSRF-Token": "csrf:projects",
          },
          path: {
            extraction_id: extraction.extraction_id,
            project_id: project.project_id,
          },
        },
      }),
    );
    expect(post).toHaveBeenCalledWith(
      "/api/v1/projects/{project_id}/content-drafts",
      expect.objectContaining({
        params: {
          header: {
            "Idempotency-Key": "intent:project",
            "If-Match": '"project:3"',
            "X-CSRF-Token": "csrf:projects",
          },
          path: { project_id: project.project_id },
        },
      }),
    );
  });

  it("maps supported planning files to deterministic parsers", () => {
    expect(sourceFormatForFile(new File(["# 设定"], "世界观.md"))).toBe("markdown");
    expect(sourceFormatForFile(new File(["{}"], "飞书导出.json"))).toBe("feishu_blocks_json");
    expect(sourceFormatForFile(new File(["x"], "未知.bin"))).toBeNull();
  });
});
