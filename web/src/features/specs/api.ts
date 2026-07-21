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
export type ConstraintProposalPage = components["schemas"]["OpaquePageV1_ConstraintProposalReadViewV1_"];
export type ConstraintProposalReadView = components["schemas"]["ConstraintProposalReadViewV1"];
export type ConstraintProposeRequest = NonNullable<
  FetchOptions<paths["/api/v1/constraint-proposals:propose"]["post"]>["body"]
>;
export type ConstraintSnapshotPage = components["schemas"]["OpaquePageV1_ConstraintSnapshotViewV1_"];
export type ConstraintSnapshotView = components["schemas"]["ConstraintSnapshotViewV1"];
export type ConstraintValidationAdmissionRequest = NonNullable<
  FetchOptions<paths["/api/v1/constraint-proposals/{artifact_id}:validate"]["post"]>["body"]
>;
export type ConstraintValidationCompilerBinding =
  components["schemas"]["ConstraintValidationCompilerBindingViewV1"];
export type ExecutionOptionResolveRequest = components["schemas"]["ExecutionOptionResolveRequestV1"];
export type ExecutionOptionView = components["schemas"]["ExecutionOptionViewV1"];
export type ExecutionProfilePage = components["schemas"]["OpaquePageV1_ExecutionProfileViewV1_"];
export type GraphPage = components["schemas"]["OpaquePageV1_GraphItemV1_"];
export type HumanConstraintDraftRequest = NonNullable<
  FetchOptions<paths["/api/v1/constraint-proposals"]["post"]>["body"]
>;
export type HumanConstraintRevisionRequest = NonNullable<
  FetchOptions<paths["/api/v1/constraint-proposals/{artifact_id}:revise"]["post"]>["body"]
>;
export type HumanPatchDraftRequest = NonNullable<FetchOptions<paths["/api/v1/patches"]["post"]>["body"]>;
export type HumanSpecUploadRequest = NonNullable<FetchOptions<paths["/api/v1/specs"]["post"]>["body"]>;
export type PatchArtifactReadView = components["schemas"]["PatchArtifactReadViewV1"];
export type RefHistoryPage = components["schemas"]["OpaquePageV1_RefHistoryEntryV1_"];
export type RunAccepted = components["schemas"]["RunAcceptedV1"];
export type SchemaRegistryDocument = components["schemas"]["SchemaRegistryDocumentV1"];
export type SnapshotDiffPage = components["schemas"]["SnapshotDiffHttpPageV1"];
export type SpecPage = components["schemas"]["OpaquePageV1_SpecViewV1_"];
export type SpecView = components["schemas"]["SpecViewV1"];
export type SubjectApprovalBindingView = components["schemas"]["SubjectApprovalBindingViewV1"];
export type SubmitForApprovalRequest = NonNullable<
  FetchOptions<paths["/api/v1/constraint-proposals/{artifact_id}:submit-for-approval"]["post"]>["body"]
>;
export type WorkflowApplyRequest = NonNullable<
  FetchOptions<paths["/api/v1/constraint-proposals/{artifact_id}:publish"]["post"]>["body"]
>;
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

export interface SpecWorkflowApi {
  listSpecs(cursor: string | null): Promise<SpecPage>;
  getSpec(artifactId: string): Promise<SpecView>;
  getArtifactPayload(artifactId: string): Promise<ArtifactPayloadView>;
  listSpecGraph(artifactId: string, cursor: string | null): Promise<GraphPage>;
  getSchemaRegistry(version: string): Promise<SchemaRegistryDocument>;
  getSnapshotDiff(
    baseArtifactId: string,
    targetArtifactId: string,
    cursor: string | null,
  ): Promise<SnapshotDiffPage>;
  listRefHistory(refName: string, cursor: string | null): Promise<RefHistoryPage>;
  listConstraintSnapshots(cursor: string | null): Promise<ConstraintSnapshotPage>;
  getConstraintSnapshot(artifactId: string): Promise<ConstraintSnapshotView>;
  listConstraintProposals(cursor: string | null): Promise<ConstraintProposalPage>;
  getConstraintProposal(artifactId: string): Promise<VersionedResource<ConstraintProposalReadView>>;
  getApprovalBinding(artifactId: string): Promise<SubjectApprovalBindingView>;
  getApproval(approvalId: string): Promise<VersionedResource<ApprovalView>>;
  listExecutionProfiles(cursor: string | null): Promise<ExecutionProfilePage>;
  getConstraintValidationCompilerBinding(
    profileId: string,
    version: number,
  ): Promise<ConstraintValidationCompilerBinding>;
  uploadSpec(request: HumanSpecUploadRequest, intent: MutationIntent): Promise<SpecView>;
  draftPatch(request: HumanPatchDraftRequest, intent: MutationIntent): Promise<PatchArtifactReadView>;
  draftConstraint(
    request: HumanConstraintDraftRequest,
    intent: MutationIntent,
  ): Promise<ConstraintProposalReadView>;
  resolveExecutionOption(request: ExecutionOptionResolveRequest): Promise<ExecutionOptionView>;
  proposeConstraint(request: ConstraintProposeRequest, intent: MutationIntent): Promise<RunAccepted>;
  reviseConstraint(
    current: VersionedResource<ConstraintProposalReadView>,
    request: HumanConstraintRevisionRequest,
    intent: MutationIntent,
  ): Promise<ConstraintProposalReadView>;
  validateConstraint(
    current: VersionedResource<ConstraintProposalReadView>,
    request: ConstraintValidationAdmissionRequest,
    intent: MutationIntent,
  ): Promise<RunAccepted>;
  submitConstraintForApproval(
    current: VersionedResource<ConstraintProposalReadView>,
    request: SubmitForApprovalRequest,
    intent: MutationIntent,
  ): Promise<ApprovalView>;
  publishConstraint(
    current: VersionedResource<ConstraintProposalReadView>,
    request: WorkflowApplyRequest,
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

export function createSpecWorkflowApi(client: GameForgeOpenApiClient = gameForgeApi.client): SpecWorkflowApi {
  return {
    listSpecs(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<SpecPage>(
          await client.GET("/api/v1/specs", { params: { query: cursorQuery(cursor) } }),
        ),
      );
    },

    async getSpec(artifactId) {
      return unwrapApiResponse<SpecView>(
        await client.GET("/api/v1/specs/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    async getArtifactPayload(artifactId) {
      return unwrapApiResponse<ArtifactPayloadView>(
        await client.GET("/api/v1/artifacts/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    listSpecGraph(artifactId, cursor) {
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

    async getSchemaRegistry(version) {
      return unwrapApiResponse<SchemaRegistryDocument>(
        await client.GET("/api/v1/schema-registry/{version}", {
          params: { path: { version } },
        }),
      );
    },

    getSnapshotDiff(baseArtifactId, targetArtifactId, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<SnapshotDiffPage>(
          await client.GET("/api/v1/diff", {
            params: {
              query: {
                base: baseArtifactId,
                target: targetArtifactId,
                ...cursorQuery(cursor),
              },
            },
          }),
        ),
      );
    },

    listRefHistory(refName, cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<RefHistoryPage>(
          await client.GET("/api/v1/refs/{ref_name}/history", {
            params: { path: { ref_name: refName }, query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    listConstraintSnapshots(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ConstraintSnapshotPage>(
          await client.GET("/api/v1/constraints", { params: { query: cursorQuery(cursor) } }),
        ),
      );
    },

    async getConstraintSnapshot(artifactId) {
      return unwrapApiResponse<ConstraintSnapshotView>(
        await client.GET("/api/v1/constraints/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
        }),
      );
    },

    listConstraintProposals(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ConstraintProposalPage>(
          await client.GET("/api/v1/constraint-proposals", {
            params: { query: cursorQuery(cursor) },
          }),
        ),
      );
    },

    async getConstraintProposal(artifactId) {
      return unwrapVersionedResponse<ConstraintProposalReadView>(
        await client.GET("/api/v1/constraint-proposals/{artifact_id}", {
          params: { path: { artifact_id: artifactId } },
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

    async getApproval(approvalId) {
      return unwrapVersionedResponse<ApprovalView>(
        await client.GET("/api/v1/approvals/{approval_id}", {
          params: { path: { approval_id: approvalId } },
        }),
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

    async getConstraintValidationCompilerBinding(profileId, version) {
      return unwrapApiResponse<ConstraintValidationCompilerBinding>(
        await client.GET(
          "/api/v1/execution-profiles/{profile_id}/versions/{version}/constraint-validation-binding",
          { params: { path: { profile_id: profileId, version } } },
        ),
      );
    },

    async uploadSpec(request, intent) {
      return unwrapApiResponse<SpecView>(
        await client.POST("/api/v1/specs", {
          body: request,
          params: { header: headersForIdempotentMutation(intent) },
        }),
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

    async draftConstraint(request, intent) {
      return unwrapApiResponse<ConstraintProposalReadView>(
        await client.POST("/api/v1/constraint-proposals", {
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

    async proposeConstraint(request, intent) {
      return unwrapApiResponse<RunAccepted>(
        await client.POST("/api/v1/constraint-proposals:propose", {
          body: request,
          params: { header: headersForIdempotentMutation(intent) },
        }),
      );
    },

    async reviseConstraint(current, request, intent) {
      return unwrapApiResponse<ConstraintProposalReadView>(
        await client.POST("/api/v1/constraint-proposals/{artifact_id}:revise", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },

    async validateConstraint(current, request, intent) {
      return unwrapApiResponse<RunAccepted>(
        await client.POST("/api/v1/constraint-proposals/{artifact_id}:validate", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },

    async submitConstraintForApproval(current, request, intent) {
      return unwrapApiResponse<ApprovalView>(
        await client.POST("/api/v1/constraint-proposals/{artifact_id}:submit-for-approval", {
          body: request,
          params: {
            header: headersForVersionedMutation(intent, current.etag),
            path: { artifact_id: current.value.artifact.artifact_id },
          },
        }),
      );
    },

    async publishConstraint(current, request, intent) {
      return unwrapApiResponse<WorkflowApplyResult>(
        await client.POST("/api/v1/constraint-proposals/{artifact_id}:publish", {
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

export const specWorkflowApi = createSpecWorkflowApi();
