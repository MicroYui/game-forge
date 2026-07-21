import type { FetchOptions } from "openapi-fetch";

import type { GameForgeOpenApiClient } from "../../api/client";
import { responseEtag, unwrapApiResponse } from "../../api/client";
import {
  headersForCsrfProtectedRequest,
  headersForIdempotentMutation,
  headersForVersionedMutation,
  type MutationIntent,
} from "../../api/csrf";
import type { components, paths } from "../../api/generated/openapi";
import { cursorQuery, requireExplicitCursorRestart } from "../../api/pagination";
import { gameForgeApi } from "../../api/runtime";

export type ApprovalView = components["schemas"]["ApprovalViewV1"];
export type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
export type ConflictPage = components["schemas"]["OpaquePageV1_MergeConflict_"];
export type ExecutionOptionResolveRequest = components["schemas"]["ExecutionOptionResolveRequestV1"];
export type ExecutionOptionView = components["schemas"]["ExecutionOptionViewV1"];
export type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];
export type ExecutionProfilePage = components["schemas"]["OpaquePageV1_ExecutionProfileViewV1_"];
type ExecutionProfileQuery = NonNullable<paths["/api/v1/execution-profiles"]["get"]["parameters"]["query"]>;
export type ExecutionProfileListFilters = Omit<ExecutionProfileQuery, "cursor">;
export type HumanPatchDraftRequest = NonNullable<FetchOptions<paths["/api/v1/patches"]["post"]>["body"]>;
export type LineagePage = components["schemas"]["OpaquePageV1_LineageEntryV1_"];
export type PatchArtifactReadView = components["schemas"]["PatchArtifactReadViewV1"];
export type PatchPage = components["schemas"]["OpaquePageV1_PatchArtifactReadViewV1_"];
export type PatchRebaseRequest = NonNullable<
  FetchOptions<paths["/api/v1/patches/{artifact_id}:rebase"]["post"]>["body"]
>;
export type PatchRepairRequest = NonNullable<
  FetchOptions<paths["/api/v1/patches/{artifact_id}:repair"]["post"]>["body"]
>;
export type PatchValidationAdmissionRequest = NonNullable<
  FetchOptions<paths["/api/v1/patches/{artifact_id}:validate"]["post"]>["body"]
>;
export type RebaseResult = components["schemas"]["RebaseResult"];
export type RefHistoryEntry = components["schemas"]["RefHistoryEntryV1"];
export type RefHistoryPageResponse = components["schemas"]["OpaquePageV1_RefHistoryEntryV1_"];
export type RefValue = components["schemas"]["RefValue"];
export type ResolveConflictsRequest = NonNullable<
  FetchOptions<paths["/api/v1/patches/{artifact_id}:resolve-conflicts"]["post"]>["body"]
>;
export type RollbackDraftRequest = NonNullable<
  FetchOptions<paths["/api/v1/refs/{ref_name}/rollback-requests"]["post"]>["body"]
>;
export type RollbackRequestPage = components["schemas"]["OpaquePageV1_RollbackRequestReadViewV1_"];
export type RollbackRequestReadView = components["schemas"]["RollbackRequestReadViewV1"];
export type RollbackValidationAdmissionRequest = NonNullable<
  FetchOptions<paths["/api/v1/rollback-requests/{artifact_id}:validate"]["post"]>["body"]
>;
export type RunAccepted = components["schemas"]["RunAcceptedV1"];
export type SnapshotDiffPage = components["schemas"]["SnapshotDiffHttpPageV1"];
export type SpecView = components["schemas"]["SpecViewV1"];
export type SubjectApprovalBindingView = components["schemas"]["SubjectApprovalBindingViewV1"];
export type PatchSubmitForApprovalRequest = NonNullable<
  FetchOptions<paths["/api/v1/patches/{artifact_id}:submit-for-approval"]["post"]>["body"]
>;
export type RollbackSubmitForApprovalRequest = NonNullable<
  FetchOptions<paths["/api/v1/rollback-requests/{artifact_id}:submit-for-approval"]["post"]>["body"]
>;
export type PatchWorkflowApplyRequest = NonNullable<
  FetchOptions<paths["/api/v1/patches/{artifact_id}:apply"]["post"]>["body"]
>;
export type RollbackWorkflowApplyRequest = NonNullable<
  FetchOptions<paths["/api/v1/rollback-requests/{artifact_id}:apply"]["post"]>["body"]
>;
export type SubmitForApprovalRequest = components["schemas"]["SubmitForApprovalRequestV1"];
export type WorkflowApplyRequest = components["schemas"]["WorkflowApplyRequestV1"];
export type WorkflowApplyResult = components["schemas"]["WorkflowApplyResultV1"];

export interface VersionedResource<T> {
  etag: string;
  value: T;
}

type ApiResponse<T> = {
  data?: T;
  error?: unknown;
  response: Response;
};

export interface PatchWorkflowApi {
  listPatches(cursor: string | null): Promise<PatchPage>;
  getPatch(artifactId: string): Promise<VersionedResource<PatchArtifactReadView>>;
  listRollbackRequests(cursor: string | null): Promise<RollbackRequestPage>;
  getRollbackRequest(artifactId: string): Promise<VersionedResource<RollbackRequestReadView>>;
  getApprovalBinding(subjectId: string): Promise<SubjectApprovalBindingView>;
  getApproval(approvalId: string): Promise<VersionedResource<ApprovalView>>;
  getSpec(artifactId: string): Promise<SpecView>;
  getArtifact(artifactId: string): Promise<ArtifactPayloadView>;
  listLineage(artifactId: string, cursor: string | null): Promise<LineagePage>;
  listExecutionProfiles(
    filters: ExecutionProfileListFilters,
    cursor: string | null,
  ): Promise<ExecutionProfilePage>;
  getExecutionProfile(profileId: string, version: number): Promise<ExecutionProfile>;
  listRefHistory(refName: string, cursor: string | null): Promise<RefHistoryPageResponse>;
  getSnapshotDiff(
    baseSnapshotId: string,
    targetSnapshotId: string,
    cursor: string | null,
  ): Promise<SnapshotDiffPage>;
  listConflicts(conflictSetId: string, cursor: string | null): Promise<ConflictPage>;
  draftPatch(request: HumanPatchDraftRequest, intent: MutationIntent): Promise<PatchArtifactReadView>;
  rebasePatch(
    current: VersionedResource<PatchArtifactReadView>,
    request: PatchRebaseRequest,
    intent: MutationIntent,
  ): Promise<RebaseResult>;
  resolvePatchConflicts(
    current: VersionedResource<PatchArtifactReadView>,
    request: ResolveConflictsRequest,
    intent: MutationIntent,
  ): Promise<RebaseResult>;
  repairPatch(request: PatchRepairRequest, intent: MutationIntent): Promise<RunAccepted>;
  validatePatch(
    current: VersionedResource<PatchArtifactReadView>,
    request: PatchValidationAdmissionRequest,
    intent: MutationIntent,
  ): Promise<RunAccepted>;
  submitPatchForApproval(
    current: VersionedResource<PatchArtifactReadView>,
    request: PatchSubmitForApprovalRequest,
    intent: MutationIntent,
  ): Promise<ApprovalView>;
  applyPatch(
    current: VersionedResource<PatchArtifactReadView>,
    request: PatchWorkflowApplyRequest,
    intent: MutationIntent,
  ): Promise<WorkflowApplyResult>;
  resolveExecutionOption(request: ExecutionOptionResolveRequest): Promise<ExecutionOptionView>;
  draftRollback(
    refName: string,
    request: RollbackDraftRequest,
    intent: MutationIntent,
  ): Promise<RollbackRequestReadView>;
  validateRollback(
    current: VersionedResource<RollbackRequestReadView>,
    request: RollbackValidationAdmissionRequest,
    intent: MutationIntent,
  ): Promise<RunAccepted>;
  submitRollbackForApproval(
    current: VersionedResource<RollbackRequestReadView>,
    request: RollbackSubmitForApprovalRequest,
    intent: MutationIntent,
  ): Promise<ApprovalView>;
  applyRollback(
    current: VersionedResource<RollbackRequestReadView>,
    request: RollbackWorkflowApplyRequest,
    intent: MutationIntent,
  ): Promise<WorkflowApplyResult>;
}

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

export function createPatchWorkflowApi(
  client: GameForgeOpenApiClient = gameForgeApi.client,
): PatchWorkflowApi {
  return {
    listPatches(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<PatchPage>(
          await client.GET("/api/v1/patches", { params: { query: cursorQuery(cursor) } }),
        ),
      );
    },

    async getPatch(artifactId) {
      return unwrapVersionedResponse<PatchArtifactReadView>(
        await client.GET("/api/v1/patches/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    listRollbackRequests(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<RollbackRequestPage>(
          await client.GET("/api/v1/rollback-requests", {
            params: { query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    async getRollbackRequest(artifactId) {
      return unwrapVersionedResponse<RollbackRequestReadView>(
        await client.GET("/api/v1/rollback-requests/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    async getApprovalBinding(subjectId) {
      return unwrapApiResponse<SubjectApprovalBindingView>(
        await client.GET("/api/v1/workflow-subjects/{artifact_id}/approval-binding", {
          params: { path: { artifact_id: subjectId } },
        }),
      );
    },

    async getApproval(approvalId) {
      return unwrapVersionedResponse<ApprovalView>(
        await client.GET("/api/v1/approvals/{approval_id}", {
          params: { path: { approval_id: approvalId } },
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

    async getArtifact(artifactId) {
      return unwrapApiResponse<ArtifactPayloadView>(
        await client.GET("/api/v1/artifacts/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
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

    listExecutionProfiles(filters, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ExecutionProfilePage>(
          await client.GET("/api/v1/execution-profiles", {
            params: { query: { ...filters, ...cursorQuery(cursor) } },
          }),
        ),
      );
    },

    async getExecutionProfile(profileId, version) {
      return unwrapApiResponse<ExecutionProfile>(
        await client.GET("/api/v1/execution-profiles/{profile_id}/versions/{version}", {
          params: { path: { profile_id: profileId, version } },
        }),
      );
    },

    listRefHistory(refName, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<RefHistoryPageResponse>(
          await client.GET("/api/v1/refs/{ref_name}/history", {
            params: { path: { ref_name: refName }, query: cursorQuery(cursor) },
          }),
        ),
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

    listConflicts(conflictSetId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ConflictPage>(
          await client.GET("/api/v1/conflict-sets/{conflict_set_id}/conflicts", {
            params: {
              path: { conflict_set_id: conflictSetId },
              query: cursorQuery(cursor),
            },
          }),
        ),
      );
    },

    async draftPatch(request, intent) {
      return unwrapApiResponse<PatchArtifactReadView>(
        await client.POST("/api/v1/patches", {
          body: request,
          params: { header: headersForIdempotentMutation(intent) },
        }),
      );
    },

    async rebasePatch(current, request, intent) {
      return unwrapApiResponse<RebaseResult>(
        await client.POST("/api/v1/patches/{artifact_id}:rebase", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },

    async resolvePatchConflicts(current, request, intent) {
      return unwrapApiResponse<RebaseResult>(
        await client.POST("/api/v1/patches/{artifact_id}:resolve-conflicts", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },

    async repairPatch(request, intent) {
      return unwrapApiResponse<RunAccepted>(
        await client.POST("/api/v1/patches/{artifact_id}:repair", {
          body: request,
          params: {
            header: headersForIdempotentMutation(intent),
            path: { artifact_id: request.params.subject_patch_artifact_id },
          },
        }),
      );
    },

    async validatePatch(current, request, intent) {
      return unwrapApiResponse<RunAccepted>(
        await client.POST("/api/v1/patches/{artifact_id}:validate", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },

    async submitPatchForApproval(current, request, intent) {
      return unwrapApiResponse<ApprovalView>(
        await client.POST("/api/v1/patches/{artifact_id}:submit-for-approval", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },

    async applyPatch(current, request, intent) {
      return unwrapApiResponse<WorkflowApplyResult>(
        await client.POST("/api/v1/patches/{artifact_id}:apply", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },

    async resolveExecutionOption(request) {
      return unwrapApiResponse<ExecutionOptionView>(
        await client.POST("/api/v1/execution-options:resolve", {
          // openapi-fetch Writable<T> evaluates NonNullable<null> as never, accidentally
          // matching its $Read marker and stripping required null-only fields.
          body: request as unknown as NonNullable<
            FetchOptions<paths["/api/v1/execution-options:resolve"]["post"]>["body"]
          >,
          params: { header: headersForCsrfProtectedRequest() },
        }),
      );
    },

    async draftRollback(refName, request, intent) {
      return unwrapApiResponse<RollbackRequestReadView>(
        await client.POST("/api/v1/refs/{ref_name}/rollback-requests", {
          body: request,
          params: {
            header: headersForIdempotentMutation(intent),
            path: { ref_name: refName },
          },
        }),
      );
    },

    async validateRollback(current, request, intent) {
      return unwrapApiResponse<RunAccepted>(
        await client.POST("/api/v1/rollback-requests/{artifact_id}:validate", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },

    async submitRollbackForApproval(current, request, intent) {
      return unwrapApiResponse<ApprovalView>(
        await client.POST("/api/v1/rollback-requests/{artifact_id}:submit-for-approval", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },

    async applyRollback(current, request, intent) {
      return unwrapApiResponse<WorkflowApplyResult>(
        await client.POST("/api/v1/rollback-requests/{artifact_id}:apply", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },
  };
}

export const patchWorkflowApi = createPatchWorkflowApi();
