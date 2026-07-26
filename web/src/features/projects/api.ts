import type { FetchOptions } from "openapi-fetch";

import type { GameForgeOpenApiClient } from "../../api/client";
import { responseEtag, unwrapApiResponse } from "../../api/client";
import {
  headersForIdempotentMutation,
  headersForVersionedMutation,
  type MutationIntent,
} from "../../api/csrf";
import type { components, paths } from "../../api/generated/openapi";
import { cursorQuery, readCursorPage } from "../../api/pagination";
import { gameForgeApi } from "../../api/runtime";

export type Project = components["schemas"]["GameProjectV1"];
export type ProjectPage = components["schemas"]["ProjectPageV1"];
export type ProjectCreateRequest = components["schemas"]["ProjectCreateRequestV1"];
export type ProjectMaterial = components["schemas"]["ProjectMaterialV1"];
export type ProjectMaterialPage = components["schemas"]["ProjectMaterialPageV1"];
export type GraphPage = components["schemas"]["OpaquePageV1_GraphItemV1_"];
export type ProjectMaterialTextRequest = components["schemas"]["ProjectMaterialTextRequestV1"];
export type ProjectMaterialRenameRequest = components["schemas"]["ProjectMaterialRenameRequestV1"];
export type ProjectExtraction = components["schemas"]["ProjectExtractionV1"];
export type ProjectExtractionCreateRequest = components["schemas"]["ProjectExtractionCreateRequestV1"];
export type ProjectExtractionDiscardRequest = components["schemas"]["ProjectExtractionDiscardRequestV1"];
export type ProjectExtractionPage = components["schemas"]["ProjectExtractionPageV1"];
export type ProjectGraphDraftRequest = components["schemas"]["ProjectGraphDraftRequestV1"];
export type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
export type PatchArtifactReadView = components["schemas"]["PatchArtifactReadViewV1"];
export type MaterialSourceFormat = ProjectMaterial["source_format"];

export interface VersionedResource<T> {
  etag: string;
  value: T;
}

export interface ProjectDraftResult {
  patch: PatchArtifactReadView;
  projectRevision: number | null;
}

export interface ProjectsApi {
  listProjects(): Promise<ProjectPage>;
  getProject(projectId: string): Promise<VersionedResource<Project>>;
  createProject(request: ProjectCreateRequest, intent: MutationIntent): Promise<VersionedResource<Project>>;
  listMaterials(projectId: string): Promise<ProjectMaterialPage>;
  addTextMaterial(
    projectId: string,
    request: ProjectMaterialTextRequest,
    intent: MutationIntent,
  ): Promise<ProjectMaterial>;
  uploadMaterial(
    projectId: string,
    file: File,
    sourceFormat: MaterialSourceFormat,
    intent: MutationIntent,
  ): Promise<ProjectMaterial>;
  startExtraction(
    projectId: string,
    request: ProjectExtractionCreateRequest,
    intent: MutationIntent,
  ): Promise<ProjectExtraction>;
  listExtractions(projectId: string): Promise<ProjectExtractionPage>;
  getExtraction(projectId: string, extractionId: string): Promise<VersionedResource<ProjectExtraction>>;
  discardExtraction(
    projectId: string,
    extractionId: string,
    request: ProjectExtractionDiscardRequest,
    intent: MutationIntent,
    extractionEtag: string,
  ): Promise<ProjectExtraction>;
  getMaterial(projectId: string, materialId: string): Promise<VersionedResource<ProjectMaterial>>;
  renameMaterial(
    projectId: string,
    materialId: string,
    request: ProjectMaterialRenameRequest,
    intent: MutationIntent,
    materialEtag: string,
  ): Promise<ProjectMaterial>;
  listContentGraph(artifactId: string, cursor: string | null): Promise<GraphPage>;
  getArtifact(artifactId: string): Promise<ArtifactPayloadView>;
  createContentDraft(
    projectId: string,
    request: ProjectGraphDraftRequest,
    intent: MutationIntent,
    projectEtag: string,
  ): Promise<ProjectDraftResult>;
}

type ApiResponse<T> = { data?: T; error?: unknown; response: Response };

async function unwrapVersioned<T>(result: ApiResponse<T>): Promise<VersionedResource<T>> {
  const value = await unwrapApiResponse<T>(result);
  const etag = responseEtag(result.response);
  if (etag === null) throw new Error("The project response did not include the required ETag.");
  return { etag, value };
}

const extensionFormats: Readonly<Record<string, MaterialSourceFormat>> = {
  csv: "csv",
  docx: "docx",
  html: "html",
  htm: "html",
  json: "feishu_blocks_json",
  md: "markdown",
  markdown: "markdown",
  txt: "plain_text",
  xlsx: "xlsx",
};

export function sourceFormatForFile(file: Pick<File, "name">): MaterialSourceFormat | null {
  const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
  return extensionFormats[extension] ?? null;
}

export function createProjectsApi(client: GameForgeOpenApiClient = gameForgeApi.client): ProjectsApi {
  return {
    async listProjects() {
      return unwrapApiResponse<ProjectPage>(
        await client.GET("/api/v1/projects", { params: { query: { limit: 100 } } }),
      );
    },

    async getProject(projectId) {
      return unwrapVersioned<Project>(
        await client.GET("/api/v1/projects/{project_id}", {
          params: { path: { project_id: projectId } },
        }),
      );
    },

    async createProject(request, intent) {
      return unwrapVersioned<Project>(
        await client.POST("/api/v1/projects", {
          body: request,
          params: { header: headersForIdempotentMutation(intent) },
        }),
      );
    },

    async listMaterials(projectId) {
      return unwrapApiResponse<ProjectMaterialPage>(
        await client.GET("/api/v1/projects/{project_id}/materials", {
          params: {
            path: { project_id: projectId },
            query: { limit: 100, status: "active" },
          },
        }),
      );
    },

    async addTextMaterial(projectId, request, intent) {
      return unwrapApiResponse<ProjectMaterial>(
        await client.POST("/api/v1/projects/{project_id}/materials:text", {
          body: request,
          params: {
            header: headersForIdempotentMutation(intent),
            path: { project_id: projectId },
          },
        }),
      );
    },

    async uploadMaterial(projectId, file, sourceFormat, intent) {
      type UploadOptions = FetchOptions<paths["/api/v1/projects/{project_id}/materials:upload"]["post"]>;
      const options = {
        body: file as unknown as string,
        bodySerializer: () => file,
        headers: { "Content-Type": file.type || "application/octet-stream" },
        params: {
          header: {
            ...headersForIdempotentMutation(intent),
            "X-GameForge-File-Name": file.name,
            "X-GameForge-Source-Format": sourceFormat,
          },
          path: { project_id: projectId },
        },
      } satisfies UploadOptions;
      return unwrapApiResponse<ProjectMaterial>(
        await client.POST("/api/v1/projects/{project_id}/materials:upload", options),
      );
    },

    async startExtraction(projectId, request, intent) {
      return unwrapApiResponse<ProjectExtraction>(
        await client.POST("/api/v1/projects/{project_id}/extractions", {
          body: request,
          params: {
            header: headersForIdempotentMutation(intent),
            path: { project_id: projectId },
          },
        }),
      );
    },

    async listExtractions(projectId) {
      return unwrapApiResponse<ProjectExtractionPage>(
        await client.GET("/api/v1/projects/{project_id}/extractions", {
          params: {
            path: { project_id: projectId },
            query: { limit: 100 },
          },
        }),
      );
    },

    async getExtraction(projectId, extractionId) {
      return unwrapVersioned<ProjectExtraction>(
        await client.GET("/api/v1/projects/{project_id}/extractions/{extraction_id}", {
          params: {
            path: { extraction_id: extractionId, project_id: projectId },
          },
        }),
      );
    },

    listContentGraph(artifactId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<GraphPage>(
          await client.GET("/api/v1/specs/{artifact_id}/graph", {
            params: {
              path: { artifact_id: artifactId },
              query: cursorQuery(cursor),
            },
          }),
        ),
      );
    },

    async getMaterial(projectId, materialId) {
      return unwrapVersioned<ProjectMaterial>(
        await client.GET("/api/v1/projects/{project_id}/materials/{material_id}", {
          params: { path: { material_id: materialId, project_id: projectId } },
        }),
      );
    },
    async renameMaterial(projectId, materialId, request, intent, materialEtag) {
      return unwrapApiResponse<ProjectMaterial>(
        await client.POST("/api/v1/projects/{project_id}/materials/{material_id}:rename", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, materialEtag),
            path: { material_id: materialId, project_id: projectId },
          },
        }),
      );
    },
    async discardExtraction(projectId, extractionId, request, intent, extractionEtag) {
      return unwrapApiResponse<ProjectExtraction>(
        await client.POST("/api/v1/projects/{project_id}/extractions/{extraction_id}:discard", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, extractionEtag),
            path: { extraction_id: extractionId, project_id: projectId },
          },
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

    async createContentDraft(projectId, request, intent, projectEtag) {
      const result = await client.POST("/api/v1/projects/{project_id}/content-drafts", {
        body: request,
        params: {
          header: headersForVersionedMutation(intent, projectEtag),
          path: { project_id: projectId },
        },
      });
      const patch = await unwrapApiResponse<PatchArtifactReadView>(result);
      const revision = result.response.headers.get("X-Project-Revision");
      return {
        patch,
        projectRevision: revision !== null && /^\d+$/u.test(revision) ? Number(revision) : null,
      };
    },
  };
}

export const projectsApi = createProjectsApi();
