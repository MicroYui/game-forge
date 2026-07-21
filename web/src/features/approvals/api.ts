import type { GameForgeOpenApiClient } from "../../api/client";
import { responseEtag, unwrapApiResponse } from "../../api/client";
import { headersForVersionedMutation, type MutationIntent } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import { cursorQuery, requireExplicitCursorRestart } from "../../api/pagination";
import { gameForgeApi } from "../../api/runtime";

export type ApprovalAction = "approve" | "reject" | "request_changes";
export type ApprovalDecisionBody = components["schemas"]["ApprovalDecisionRequestV1"];
export type ApprovalPageData = components["schemas"]["OpaquePageV1_ApprovalViewV1_"];
export type ApprovalViewData = components["schemas"]["ApprovalViewV1"];

export interface VersionedApproval {
  etag: string;
  value: ApprovalViewData;
}

export interface ApprovalDecisionIntent {
  action: ApprovalAction;
  comment: string | null;
  reasonCode: string;
  requirementIds: readonly string[];
}

export interface ApprovalsApi {
  decide(
    current: VersionedApproval,
    decision: ApprovalDecisionIntent,
    intent: MutationIntent,
  ): Promise<VersionedApproval>;
  getApproval(approvalId: string): Promise<VersionedApproval>;
  listMine(cursor: string | null): Promise<ApprovalPageData>;
}

type ApiResponse<T> = {
  data?: T;
  error?: unknown;
  response: Response;
};

async function unwrapVersioned(
  result: ApiResponse<ApprovalViewData>,
  expectedApprovalId: string,
): Promise<VersionedApproval> {
  const value = await unwrapApiResponse<ApprovalViewData>(result);
  if (value.approval.approval_id !== expectedApprovalId) {
    throw new Error("The approval response does not belong to the requested approval.");
  }
  const etag = responseEtag(result.response);
  if (etag === null) throw new Error("The server response did not include the required ETag.");
  return { etag, value };
}

async function readCursorPage<T>(cursor: string | null, read: () => Promise<T>): Promise<T> {
  try {
    return await read();
  } catch (error) {
    throw requireExplicitCursorRestart(error, cursor);
  }
}

export function createApprovalsApi(client: GameForgeOpenApiClient = gameForgeApi.client): ApprovalsApi {
  return {
    listMine(cursor) {
      return readCursorPage(cursor, async () =>
        unwrapApiResponse<ApprovalPageData>(
          await client.GET("/api/v1/approvals", {
            params: { query: { assignee: "me", ...cursorQuery(cursor), limit: 100 } },
          }),
        ),
      );
    },

    async getApproval(approvalId) {
      return unwrapVersioned(
        await client.GET("/api/v1/approvals/{approval_id}", {
          params: { path: { approval_id: approvalId } },
        }),
        approvalId,
      );
    },

    async decide(current, decision, intent) {
      const approvalId = current.value.approval.approval_id;
      const body: ApprovalDecisionBody = {
        comment: decision.comment,
        decision: decision.action,
        expected_workflow_revision: current.value.approval.workflow_revision,
        reason_code: decision.reasonCode,
        request_schema_version: "approval-decision-request@1",
        requirement_ids: [...decision.requirementIds],
      };
      const params = {
        header: headersForVersionedMutation(intent, current.etag),
        path: { approval_id: approvalId },
      };

      if (decision.action === "approve") {
        return unwrapVersioned(
          await client.POST("/api/v1/approvals/{approval_id}:approve", { body, params }),
          approvalId,
        );
      }
      if (decision.action === "reject") {
        return unwrapVersioned(
          await client.POST("/api/v1/approvals/{approval_id}:reject", { body, params }),
          approvalId,
        );
      }
      return unwrapVersioned(
        await client.POST("/api/v1/approvals/{approval_id}:request_changes", { body, params }),
        approvalId,
      );
    },
  };
}

export const approvalsApi = createApprovalsApi();
